"""#2451: the segment read must keep the segment key in its index condition.

What this module is
-------------------

It started as a one-cell hypothesis test — "does a MISSING chunk statistic make
the planner pick ``river_ts_run_discovery_key_idx``?" — and it answered yes
(``ANALYZE`` as the only variable; per-node ratio 2999 -> 0, 22.254 ms ->
0.255 ms, archived as
``openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/stats-proof-2451.json``).

It was then the MEASUREMENT HARNESS ``tasks.md`` §2.1 ran against the C1 and C2
candidates (three variants in one process, selected through an environment
switch). §2.2 selected C1 on those measurements, the switch was deleted and C1
became the unconditional rendering, so this module is now a single-pass GATE over
the SHIPPED code. It asserts that every measured cell of the condition cross
product satisfies ``design.md``'s three pass criteria; whether a cell reproduces
#2451 is recorded as DATA (``defect_reproduced``) and is never asserted.

Every failing cell is listed in one message; one red cell must not hide the
others.

The cross product (design.md, "How the selection is made")
----------------------------------------------------------

predicate shape x statistics state x store branch x chunk compression state.

* shape — run-bound (``rt.run_key = (SELECT …)``) and ``issue_time=latest``
  (``rt.run_key = ANY(%(pushdown_run_keys)s)``). F8 shows they flip DIFFERENT
  chunks in production, so one shape's result does not carry to the other. The
  ``latest`` shape puts TWO statements through a ``hydro.river_timeseries``
  filter — ``_per_source_latest_cycles`` (``forecast_store.py:743``) and the
  cycle-window segment read (``:898``) — and the one measured here is the second,
  selected by its ``pushdown_run_keys`` binding. The first is recorded (its
  shared hits are must-preserve #5's baseline) but is §4.3's business.
* statistics — ``absent``, ``stale`` in design.md F9b's shape (analyse, THEN
  write the target run, never re-analyse), and ``fresh`` after ``ANALYZE``.
* branch — narrow and legacy. One template renders both (F1c) and the legacy
  table still carries ``river_ts_selected_identity_key_valid_time_idx``, same
  column order, same missing segment key, never dropped (F1b).
* chunk — uncompressed and compressed.

Criterion 1 is judged PER BRANCH — ``river_segment_key`` on a narrow node,
``river_segment_id`` on a legacy one. See ``tests/river_ts_plan_criteria`` for
why, and ``tests/test_river_ts_plan_criteria`` for the offline proof that all
three criteria bite (including the synthetic "segment key late in the Index
Cond" plan of ``tasks.md`` 1.5, which passes criteria 1 and 2).

Ways this harness could be green for the wrong reason — named, not hidden
-------------------------------------------------------------------------

1. A cell whose extract is EMPTY would satisfy every criterion vacuously. An
   empty extract is a failure (``evaluate_cell``), and the compressed cells are
   the ones at risk: their access shows up under a ``compress_hyper_*`` relation
   name, mapped back to the measured chunk structurally.
2. Criterion 3's baseline comes from the SAME cell's post-``ANALYZE``
   measurement. A baseline taken from a plan that itself failed criteria 1 or 2
   would be inflated and would silence criterion 3, so it is only used when the
   ``fresh`` cell passed 1 and 2; otherwise criterion 3 records ``None``.
1b. An EXPECTED-OPEN cell would be a way for the gate to be green about a cell it
   never judged. ``EXPECTED_OPEN_CELLS`` excuses exactly one cell's PLAN
   criteria — never its row identity — the cell is still measured, still
   judged, still printed in the table with its real verdict and still in
   ``matrix.json``, and a cell that starts PASSING while listed there is a
   failure too, so the allowlist cannot rot into silence.
3. ``ANALYZE`` refreshes ``reltuples``/``relpages`` as well as column
   statistics. Residual, not mitigated.
4. Buffer-cache warmth cannot explain an index CHOICE — the planner does not
   consult the buffer cache — but it moves ``Shared Hit Blocks``, which
   criterion 3 reads. Each EXPLAIN is run twice and the warm plan is kept.
5. If the candidate index paths cost the SAME without statistics, the winner
   follows path/OID order rather than selectivity. ``Plan Rows``,
   ``Total Cost`` and each chunk's indexes in OID order are in the evidence so
   that reading stays available.
6. Every scenario has ONE basin version and ONE river-network version, which is
   what makes the discovery index look maximally selective without statistics.
   Production's newest chunk has the same shape, so this is faithful — but a
   chunk holding several basin versions would not, and a candidate selected here
   would not have been tested against that. Residual, carried deliberately.

Row identity (must-preserve #1)
------------------------------

Each cell's row digest is compared against ``RECORDED_ROW_DIGESTS`` — the twelve
values the 2026-09-18 node-27 runs measured, which is the ONLY comparison left
once the three-variant loop is gone. They are a recorded fact and not a
convenience: the same twelve appeared in two independent throwaway databases and
under all three variants, so they are a deterministic property of the fixture's
seed rather than of one run. A cell whose rows changed shows ``F`` in the table —
not merely a footnote — because a reader compares rows, not findings.

Run on node-27 against a throwaway database:

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        NHMS_STATS_PROOF_OUTPUT=/home/nwm/tmp/2451/matrix.json \\
        uv run pytest -q tests/test_river_timeseries_stats_index_choice_integration.py
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from packages.common.forecast_store import PsycopgForecastStore
from packages.common.node27_pgdata_workload_plan import evaluate_explain_json_plan
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)
from tests.river_ts_plan_criteria import (
    DEFAULT_SHARED_HIT_MULTIPLE,
    SHARED_HIT_ABSOLUTE_FLOOR,
    cell_row,
    evaluate_cell,
    walk_plan,
)
from tests.river_ts_stats_matrix_seed import (
    EXPECTED_SERIES_BY_SHAPE,
    EXPECTED_UNCONSTRUCTIBLE,
    SCENARIOS,
    SHAPES,
    STEP_COUNT,
    Scenario,
    analyze_relation,
    column_statistics,
    fetch_all,
    relation_statistics,
    seed_matrix,
)
from tests.test_display_coverage_residual_debt_integration import _connect

pytestmark = pytest.mark.integration

#: The #2417 slot this issue is about, as it renders into the NARROW branch of
#: ``_SEGMENT_ROWS_SOURCE_SQL`` (``packages/common/forecast_store.py:73-77``).
_RUN_PUSHDOWN_MARKER = "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)"
#: ``_RESOLVED_RUN_PUSHDOWN_SQL`` (``forecast_store.py:82-85``), which only the
#: cycle-window segment read carries — never ``_per_source_latest_cycles``.
_LATEST_PUSHDOWN_MARKER = "pushdown_run_keys"

_SHAPE_MARKERS = {"run_bound": _RUN_PUSHDOWN_MARKER, "latest": _LATEST_PUSHDOWN_MARKER}

#: D11's own ceiling, read off the gate rather than copied, so this test goes red
#: if ``packages/common/node27_pgdata_workload_plan.py`` moves the bound.
_FILTER_RATIO_LIMIT: int = inspect.signature(evaluate_explain_json_plan).parameters["filter_ratio_limit"].default

_EXPLAIN_ROUNDS = 2
_MCV_COLUMNS = ("run_key", "river_segment_key", "run_id", "river_segment_id")

#: Must-preserve #1's expectation, keyed by (scenario key, predicate shape) —
#: the two things the returned rows are a function of. Recorded from node-27,
#: 2026-09-18: ``/home/nwm/tmp/2451/matrix-baseline-20260918.json`` (the unfixed
#: single-pass run) and ``/home/nwm/tmp/2451/matrix.json`` (the three-variant
#: run). All twelve agree across those two THROWAWAY DATABASES and across
#: ``base`` / ``c1`` / ``c2``, which is what makes them a property of the seed
#: rather than of one run — and what makes them usable as the expectation now
#: that there is no base variant left to compare against.
#:
#: A cell's digest is not keyed by statistics state: the same rows must come back
#: whatever the planner did, and ``_judge`` checks that separately.
#:
#: If the SEED changes — segment counts, step count, insertion order, the runs —
#: these move legitimately. The only correct response is to RE-RECORD them from a
#: measured node-27 run and say so on the commit. Editing one to match a red run
#: is how must-preserve #1 gets lost.
RECORDED_ROW_DIGESTS: dict[tuple[str, str], str] = {
    ("narrow/uncompressed/absent", "run_bound"): "63ad1ec274bc9d36",
    ("narrow/uncompressed/absent", "latest"): "7dca11cf83f1bfaa",
    ("narrow/uncompressed/stale", "run_bound"): "08dc5f9c0402e861",
    ("narrow/uncompressed/stale", "latest"): "f0c49f0cc1d44e7a",
    ("narrow/compressed/absent", "run_bound"): "7d1371a978fc3756",
    ("narrow/compressed/absent", "latest"): "d7c1c629924ece79",
    ("legacy/uncompressed/absent", "run_bound"): "d96f189e0f695766",
    ("legacy/uncompressed/absent", "latest"): "7d743734a52c6cc9",
    ("legacy/uncompressed/stale", "run_bound"): "74998225bc345f25",
    ("legacy/uncompressed/stale", "latest"): "bcc9f6c16fb0bfe1",
    ("legacy/compressed/absent", "run_bound"): "27557bb07f0263fa",
    ("legacy/compressed/absent", "latest"): "13de2f519e437f3a",
}

#: The one cell design.md §2.2 leaves OPEN by construction, tracked as **#2471**,
#: excused from the gate's assertion and from nothing else.
#:
#: ``run_bound/stale/legacy/uncompressed`` is reached through the legacy TEXT
#: twin ``river_timeseries_mvt_selected_identity_valid_time_discovery_idx``
#: (design.md F1b, corrected) — matched by the legacy rendering's text aid
#: conjuncts, not by ``run_key``. C1 moves ``basin_version_key`` and
#: ``river_network_version_key``, which are not columns of that index, so it has
#: no lever on this cell: base, C1 and C2 all measured it red at ratio 999.0.
#: ``tasks.md`` §2.2's bound says explicitly that a candidate leaving this cell
#: red has not thereby failed, and §6.3 files it as its own issue rather than
#: folding it in — that issue is **#2471**. It expires with #1988's DROP, which
#: removes the text twin this entry exists for.
#:
#: What this does NOT excuse: row identity and non-vacuity — see
#: ``_NEVER_EXCUSED_PREFIXES``. Neither has anything to do with which index the
#: planner took.
EXPECTED_OPEN_CELLS: dict[str, str] = {
    "run_bound/stale/legacy/uncompressed": (
        "#2471 (design.md §2.2 / tasks.md §6.3): reached through the legacy TEXT twin, "
        "which C1 has no lever on; expires with #1988's DROP"
    ),
}

#: A failure this prefix opens is a ROW-identity failure, never a plan one.
_ROW_IDENTITY_PREFIX = "ROW IDENTITY"

#: What ``EXPECTED_OPEN_CELLS`` never excuses. Row identity because must-preserve
#: #1 is independent of which index the planner took; NON-VACUITY because a cell
#: that returned the wrong number of rows measured the wrong thing, and "the plan
#: is known-bad here" is not a reason to stop checking that.
_NEVER_EXCUSED_PREFIXES = (_ROW_IDENTITY_PREFIX, "NON-VACUITY")


# ---------------------------------------------------------------------------
# Capturing the REAL statement
# ---------------------------------------------------------------------------


class _RecordingCursor:
    """Pass-through cursor recording every statement, binding AND returned row.

    Same shape as the node-27 probe's ``_RecCursor``
    (``.../receipts/2026-09-17-i8-explain-gate/probe1987.py:51-72``), reproduced
    here rather than imported: that directory is archived evidence, not a
    library. Two details are load-bearing and neither is cosmetic.

    ``execute`` branches on ``isinstance(parameters, Mapping)``: the store binds
    named placeholders with a mapping (``_fetch_all``,
    ``packages/common/forecast_store.py:2778-2789``) but binds
    ``_validate_series_target``'s statements with POSITIONAL TUPLES (``:2772``,
    called at ``:608`` and ``:621``). ``dict(parameters)`` on a tuple raises, so
    the branch is what keeps the capture alive on the real call path.

    ``fetchall`` attaches the returned rows to the statement that produced them,
    which is what makes must-preserve #1's digest possible: the receipt digests
    the raw FACT ROWS (``probe1987.py`` ``digest(st["rows"])``), not the API
    response. Returning ``dict`` rows rather than ``RealDictRow`` is what the
    probe does too and is invisible to the store, which already wraps every row
    in ``dict`` (``forecast_store.py:2789``).
    """

    def __init__(self, cursor: Any, sink: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._sink = sink
        self._last: dict[str, Any] | None = None

    def execute(self, statement: Any, parameters: Any = None) -> None:
        if isinstance(parameters, Mapping):
            captured: Any = dict(parameters)
        elif parameters is None:
            captured = {}
        else:
            captured = list(parameters)
        self._last = {"sql": str(statement), "params": captured}
        self._sink.append(self._last)
        self._cursor.execute(statement, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._cursor.fetchall()]
        if self._last is not None:
            self._last["rows"] = rows
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _RecordingForecastStore(PsycopgForecastStore):
    """The real store, driven over a caller-owned connection, with a tap on it."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        super().__init__("recording://issue-2451")
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_sink", sink)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        cursor = self._connection.cursor()
        try:
            yield _RecordingCursor(cursor, self._sink)
        finally:
            cursor.close()


def statement_digest(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Must-preserve #1's digest, over the FACT ROWS, in the receipt's spelling.

    ``sha256("\\n".join(repr(sorted(row.items()))))[:16]`` over the rows the
    measured statement returned — literally ``probe1987.py``'s ``digest()``
    applied to ``st["rows"]``, so the number is comparable with
    ``explain-1987.json`` / ``explain-1987-latest.json`` and is the one
    ``tasks.md`` 3.4 asks for.

    NOT over the API response. ``forecast_series`` builds a forecast point as a
    two-element LIST (``forecast_store.py:4202``,
    ``[_timestamp_ms(valid_time), float(value)]``); only the STATION series uses
    mapping-shaped points (``:4263-4269``). ``sorted(point.items())`` on a
    response point therefore raises ``AttributeError`` against every real
    response — which is exactly what happened on node-27, before any cell was
    judged. ``tests/test_river_ts_stats_harness_offline.py`` builds the response
    with the real producer so that shape cannot drift away from this module
    unnoticed again.

    One property of the captured rows is deliberate rather than incidental:
    ``_attach_forcing_lineage`` (``forecast_store.py:2791-2808``) sets
    ``row["lineage_json"]`` IN PLACE on the very dicts recorded here, so the
    digest covers the rows as the store finally left them — the same treatment
    ``probe1987.py`` gives them. It is stable: the statement carries
    ``ORDER BY h.scenario_id, rt.valid_time`` and the lineage value is per
    forcing version. The offline suite proves the stability by recapturing
    independently and requiring an identical digest.
    """
    rows = statement.get("rows")
    if rows is None:
        return {"digest": None, "digest_rows": None, "error": "the statement recorded no fetchall()"}
    payload = "\n".join(repr(sorted(row.items())) for row in rows)
    return {
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "digest_rows": len(rows),
    }


def response_point_count(response: Mapping[str, Any]) -> int:
    """How many points the public response carries, whatever a point is made of.

    Deliberately shape-agnostic: it counts points and never reads inside one, so
    it cannot be the thing that breaks when the point representation changes.
    It is a cross-check on the row digest above, not a second digest.
    """
    return sum(len(series.get("points") or []) for series in response.get("series") or [])


def _capture_fact_statement(
    connection: Any,
    scenario: Scenario,
    shape: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Drive the public read for one shape and return (measured, companions, response).

    ``_capture``'s old "exactly one statement mentions hydro.river_timeseries"
    rule does not hold for the ``latest`` shape, which puts two through that
    filter. The selection is by PUSHDOWN MARKER instead, which names the
    statement design.md F8 measured rather than whichever came first.
    """
    sink: list[dict[str, Any]] = []
    store = _RecordingForecastStore(connection, sink)
    common: dict[str, Any] = {
        "basin_version_id": BASIN_VERSION_ID,
        "segment_id": scenario.target_segment_id,
        "river_network_version_id": RIVER_NETWORK_VERSION_ID,
        "variables": ["q_down"],
        # Both scenarios, so `issue_time=latest` resolves the target AND the
        # decoy and pushes a TWO-element `rt.run_key = ANY(...)` — design.md F8's
        # production shape. The run-bound call pins `run_id` and still reads one.
        "scenarios": ["GFS", "IFS"],
        "include_analysis": False,
        "run_types": ["forecast"],
        "model_id": MODEL_ID,
    }
    if shape == "run_bound":
        response = store.forecast_series(issue_time=scenario.issue_time, run_id=scenario.target_run_id, **common)
    elif shape == "latest":
        response = store.forecast_series(issue_time="latest", **common)
    else:
        raise ValueError(f"unknown predicate shape {shape!r}")

    marker = _SHAPE_MARKERS[shape]
    fact_statements = [
        statement
        for statement in sink
        if "hydro.river_timeseries" in statement["sql"] and isinstance(statement["params"], dict)
    ]
    measured = [statement for statement in fact_statements if marker in statement["sql"]]
    companions = [statement for statement in fact_statements if marker not in statement["sql"]]
    if len(measured) != 1:
        raise AssertionError(
            f"{scenario.key}/{shape}: expected exactly one captured statement carrying {marker!r}, "
            f"got {len(measured)} of {len(fact_statements)} statements against hydro.river_timeseries: "
            + " | ".join(" ".join(statement["sql"].split())[:120] for statement in fact_statements)
        )
    return measured[0], companions, response


# ---------------------------------------------------------------------------
# EXPLAIN
# ---------------------------------------------------------------------------


def _explain(connection: Any, statement: Mapping[str, Any]) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) the captured statement, warm.

    Two rounds, keeping the second: the planner never consults the buffer cache,
    so this cannot change which index is chosen — it only makes the cells'
    ``Shared Hit Blocks`` comparable, which criterion 3 depends on.
    """
    plan: Mapping[str, Any] = {}
    root: Mapping[str, Any] = {}
    for _ in range(_EXPLAIN_ROUNDS):
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement["sql"], statement["params"])
            row = cursor.fetchone()
        payload = next(iter(row.values())) if isinstance(row, Mapping) else row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        root = payload[0]
        plan = root["Plan"]
    return {
        "execution_time_ms": root.get("Execution Time"),
        "planning_time_ms": root.get("Planning Time"),
        "root_shared_hit_blocks": plan.get("Shared Hit Blocks"),
        "root_shared_read_blocks": plan.get("Shared Read Blocks"),
        "root_actual_rows": plan.get("Actual Rows"),
        "relation_census": _relation_census(plan),
        "plan": plan,
    }


def _relation_census(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every scan node in the plan, chunk or not.

    The criteria are scoped to one cell's chunk, so this census is what makes a
    surprise legible: the ``UNION ALL``'s other branch, the empty hypertable
    parent PostgreSQL's inheritance expansion may keep, and any chunk that chunk
    pruning failed to exclude all show up here.
    """
    return [
        {
            "depth": depth,
            "node_type": node.get("Node Type"),
            "relation": node.get("Relation Name"),
            "index_name": node.get("Index Name"),
            "actual_rows": node.get("Actual Rows"),
            "rows_removed_by_filter": node.get("Rows Removed by Filter"),
            "shared_hit_blocks": node.get("Shared Hit Blocks"),
        }
        for depth, node in walk_plan(plan)
        if node.get("Relation Name")
    ]


def _chunk_indexes(connection: Any, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The chunk's indexes with their OIDs, in creation order (wrong-reason 5)."""
    return fetch_all(
        connection,
        """
        SELECT i.relname AS index_name, i.oid AS index_oid, pg_get_indexdef(i.oid) AS definition
        FROM pg_index x
        JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(chunk_schema)s AND c.relname = %(chunk_name)s
        ORDER BY i.oid
        """,
        {"chunk_schema": chunk["chunk_schema"], "chunk_name": chunk["chunk_name"]},
    )


@contextmanager
def _evidence_dump(evidence: dict[str, Any]) -> Iterator[None]:
    """Write EVERY cell to ``NHMS_STATS_PROOF_OUTPUT``, if set.

    In a ``finally`` so a REFUTATION is archived too — that is the outcome whose
    evidence is worth the most. Skipped, not failed, when the variable is unset,
    and a write error is recorded rather than raised so it cannot mask the
    assertion actually being reported. No assertion below reads it.
    """
    try:
        yield
    finally:
        target = os.getenv("NHMS_STATS_PROOF_OUTPUT", "").strip()
        if not target:
            evidence["dump_path"] = None
        else:
            try:
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
                evidence["dump_path"] = str(path)
            except OSError as error:
                evidence["dump_path"] = f"{type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_segment_read_binds_the_segment_key_across_the_condition_cross_product(
    throwaway_database_url: str,
) -> None:
    """Every measured cell must satisfy design.md's three pass criteria."""
    apply_migrations_from_zero(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)

    evidence: dict[str, Any] = {
        "issue": 2451,
        "filter_ratio_limit": _FILTER_RATIO_LIMIT,
        "shared_hit_multiple": DEFAULT_SHARED_HIT_MULTIPLE,
        "shared_hit_absolute_floor": SHARED_HIT_ABSOLUTE_FLOOR,
        "explain_rounds": _EXPLAIN_ROUNDS,
        "expected_unconstructible": sorted("/".join(triple) for triple in EXPECTED_UNCONSTRUCTIBLE),
        "expected_open_cells": EXPECTED_OPEN_CELLS,
    }
    with _evidence_dump(evidence):
        cells, findings = _measure_matrix(throwaway_database_url, evidence)

    # Read back out of the evidence rather than recomputed here: the dump is
    # written in `_evidence_dump`'s `finally`, so anything computed AFTER that
    # block would be in this message and never in matrix.json — and the parent
    # reads the JSON, while this message only exists when the gate is red.
    summary: dict[str, int] = evidence["summary"]
    table = "\n".join("  " + cell_row(cell) for cell in cells)
    summary_line = (
        f"  {summary['passed']} passed / {summary['failed']} failed of {summary['measured']} measured"
        f" (digest mismatches vs the recorded expectation: {summary['digest_mismatch']};"
        f" expected-open cells excused: {summary['expected_open_excused']})"
    )
    reported = findings + _reported_cell_failures(cells)
    assert not reported, (
        f"#2451 gate: {len(reported) - len(findings)} measured cell(s) of {len(cells)} failed, "
        f"{len(findings)} finding(s).\n"
        + "\n".join(f"  - {line}" for line in reported)
        + f"\n\nsummary:\n{summary_line}"
        + f"\n\nper-cell table (criteria = 1,2,3; P pass / F fail / - not evaluated):\n{table}"
        + f"\n\nevidence: {evidence.get('dump_path')}"
    )


def _reported_cell_failures(cells: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which cell failures the gate asserts on, after ``EXPECTED_OPEN_CELLS``.

    An expected-open cell is excused for its PLAN criteria and for nothing else:
    every failure opening with a ``_NEVER_EXCUSED_PREFIXES`` prefix is reported
    like any other cell's.

    An expected-open cell that PASSES is itself reported. Otherwise the allowlist
    would outlive the reason for it — #1988's DROP removes the text twin this
    entry exists for — and the gate would go on quietly excusing a cell that no
    longer needs it.
    """
    reported: list[str] = []
    for cell in cells:
        key = str(cell["cell_key"])
        reason = EXPECTED_OPEN_CELLS.get(key)
        if reason is None:
            if not cell["passed"]:
                reported.append(f"{key}: " + "; ".join(cell["failures"]))
            continue
        if cell["passed"]:
            reported.append(
                f"EXPECTED-OPEN CELL NOW PASSES {key}: it is listed in EXPECTED_OPEN_CELLS ({reason}) but "
                "satisfied every criterion; remove the entry rather than leaving a stale excuse in place"
            )
            continue
        never_excused = [
            failure for failure in cell["failures"] if failure.startswith(_NEVER_EXCUSED_PREFIXES)
        ]
        if never_excused:
            reported.append(f"{key}: " + "; ".join(never_excused))
    return reported


def _summarise(cells: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """The pass/fail counts the gate's message and ``matrix.json`` both read.

    ``expected_open_excused`` counts cells that were ACTUALLY excused — failed,
    allowlisted, and contributed nothing to the reported list. A cell that is
    allowlisted but failed on row identity is reported, so counting it as excused
    would say the opposite of what happened.
    """
    summary = {"measured": 0, "passed": 0, "failed": 0, "digest_mismatch": 0, "expected_open_excused": 0}
    for cell in cells:
        summary["measured"] += 1
        summary["passed" if cell["passed"] else "failed"] += 1
        if cell.get("digest_matches_recorded") is False:
            summary["digest_mismatch"] += 1
        if not cell["passed"] and not _reported_cell_failures([cell]):
            summary["expected_open_excused"] += 1
    return summary


def _measure_matrix(
    database_url: str,
    evidence: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Seed, measure every cell, then judge — with every connection closed first.

    ``throwaway_database_url`` drops the database in its teardown, which a live
    connection would block, so nothing here outlives the function.
    """
    # Two connections because the seed has two phases: a transactional one that
    # lands rows, chunks and their autovacuum opt-out atomically, and an
    # autocommit one for `compress_chunk` / `ANALYZE` / the post-ANALYZE write.
    # See `seed_matrix`.
    seed_connection = _connect(database_url)
    seed_connection.autocommit = False
    post_connection = _connect(database_url)
    try:
        evidence["seed"] = seed_matrix(seed_connection, post_connection)
    finally:
        seed_connection.close()
        post_connection.close()

    findings: list[str] = []
    measurements: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    usable: list[Scenario] = []

    connection = _connect(database_url)
    try:
        evidence["scenarios"] = {}
        for scenario in SCENARIOS:
            state = _scenario_state(connection, scenario, evidence["seed"]["scenarios"][scenario.key])
            evidence["scenarios"][scenario.key] = state
            if state["status"] != "usable":
                if scenario.triple not in EXPECTED_UNCONSTRUCTIBLE:
                    findings.append(
                        f"REQUIRED SCENARIO UNAVAILABLE {scenario.key}: {state['status']} — {state['reason']}"
                    )
                continue
            usable.append(scenario)
            findings.extend(_precondition_findings(scenario, state))

        # The natural (absent / stale) statistics state is measured BEFORE the
        # single ANALYZE below, and `fresh` after it. Do not reorder: once a
        # relation is analysed the absent-and-stale state cannot be recovered
        # without re-seeding, and every later cell would be a `fresh` one wearing
        # an `absent` label — the cross product's whole point is that code
        # holding only after ANALYZE has fixed nothing.
        for scenario in usable:
            for shape in SHAPES:
                _record(measurements, findings, connection, scenario, shape, scenario.statistics, evidence)

        evidence["analyze"] = _analyze_everything(connection, usable, evidence["scenarios"])
        for scenario in usable:
            state = evidence["scenarios"][scenario.key]
            relation = state["statistics_relation"]
            after = relation_statistics(connection, relation["chunk_schema"], relation["chunk_name"])
            state["statistics_after_analyze"] = after
            if not int(after["pg_statistic_rows"] or 0) > 0:
                # Its own finding, its own message: if ANALYZE does not reach
                # this relation on this server, every `fresh` cell below is a
                # repeat of the unanalysed one and criterion 3 loses its
                # baseline. That is a server finding, not a refutation.
                findings.append(
                    f"ANALYZE did not populate statistics for {relation['qualified']} ({scenario.key}): "
                    f"either TimescaleDB did not recurse into this relation, or the connected role does not own "
                    f"{scenario.hypertable} (000059 sets the owner to nhms_ingest_rw)"
                )

        for scenario in usable:
            for shape in SHAPES:
                _record(measurements, findings, connection, scenario, shape, "fresh", evidence)
    finally:
        connection.close()

    return _judge(measurements, evidence, findings)


def _record(
    measurements: dict[tuple[str, str, str], dict[str, Any]],
    findings: list[str],
    connection: Any,
    scenario: Scenario,
    shape: str,
    statistics: str,
    evidence: Mapping[str, Any],
) -> None:
    """Measure one cell, recording a failure as a finding instead of aborting.

    One cell that cannot be measured must not hide the other fifteen — the whole
    point of the aggregate assertion.
    """
    chunk_relation = str(evidence["scenarios"][scenario.key]["chunk"]["chunk_name"])
    try:
        measurements[(scenario.key, shape, statistics)] = _measure(connection, scenario, shape, chunk_relation)
    except Exception as error:  # noqa: BLE001 - a cell that cannot be measured is a finding
        findings.append(f"CELL NOT MEASURED {shape}/{statistics}/{scenario.key}: {type(error).__name__}: {error}")


def _scenario_state(connection: Any, scenario: Scenario, construction: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one scenario's chunk, statistics relation and pre-measurement state."""
    state: dict[str, Any] = {
        "key": scenario.key,
        "construction": construction,
        "status": "usable",
        "reason": "",
    }
    if construction.get("status") != "seeded" or not construction.get("chunk"):
        state["status"] = "unseeded"
        state["reason"] = str(construction.get("error") or "the scenario did not seed")
        return state
    if construction.get("target_facts_error"):
        state["status"] = "target_rows_missing"
        state["reason"] = str(construction["target_facts_error"])
        return state

    chunk = dict(construction["chunk"])
    relation = dict(construction["statistics_relation"])
    state["chunk"] = chunk
    state["statistics_relation"] = relation
    state["chunk_indexes"] = _chunk_indexes(connection, chunk)
    if scenario.compressed and not chunk.get("is_compressed"):
        state["status"] = "not_compressed"
        state["reason"] = f"chunk {chunk['qualified']} is not compressed; the cell would measure the wrong access path"
        return state
    if not scenario.compressed and chunk.get("is_compressed"):
        state["status"] = "unexpectedly_compressed"
        state["reason"] = f"chunk {chunk['qualified']} is compressed; this cell is the uncompressed one"
        return state

    state["run_keys"] = {
        str(row["run_id"]): int(row["run_key"])
        for row in fetch_all(
            connection,
            "SELECT run_id, run_key FROM hydro.hydro_run WHERE run_id = ANY(%(run_ids)s)",
            {"run_ids": [scenario.target_run_id, scenario.decoy_run_id]},
        )
    }
    state["statistics_before"] = relation_statistics(connection, relation["chunk_schema"], relation["chunk_name"])
    state["column_statistics_before"] = column_statistics(
        connection, relation["chunk_schema"], relation["chunk_name"], _MCV_COLUMNS
    )
    return state


def _precondition_findings(scenario: Scenario, state: Mapping[str, Any]) -> list[str]:
    """Assert the statistics state is the one the cell claims, never assume it."""
    findings: list[str] = []
    relation = state["statistics_relation"]
    before = state["statistics_before"]
    if scenario.statistics == "absent":
        if int(before["pg_statistic_rows"] or 0) != 0:
            findings.append(
                f"PRECONDITION {scenario.key}: {relation['qualified']} already carries "
                f"{before['pg_statistic_rows']} pg_statistic rows before the first EXPLAIN "
                f"(reloptions={before['reloptions']}); this is not the absent-statistics state"
            )
        if before["last_analyze"] is not None or before["last_autoanalyze"] is not None:
            findings.append(
                f"PRECONDITION {scenario.key}: {relation['qualified']} was already analyzed "
                f"(last_analyze={before['last_analyze']}, last_autoanalyze={before['last_autoanalyze']})"
            )
        return findings

    # stale, in design.md F9b's shape: analysed, and then the target run written.
    if int(before["pg_statistic_rows"] or 0) == 0:
        findings.append(
            f"PRECONDITION {scenario.key}: the pre-write ANALYZE did not reach {relation['qualified']}, "
            "so this cell is the absent state wearing the stale label"
        )
        return findings
    run_key_stats = state["column_statistics_before"].get("run_key") or {}
    members = set(run_key_stats.get("members") or [])
    target_key = str(state["run_keys"].get(scenario.target_run_id))
    decoy_key = str(state["run_keys"].get(scenario.decoy_run_id))
    if target_key in members:
        findings.append(
            f"PRECONDITION {scenario.key}: the target run_key {target_key} IS in {relation['qualified']}'s "
            f"run_key MCV list {run_key_stats.get('most_common_vals')!r}; design.md F9b's staleness is that it "
            "is absent, so this cell measures fresh statistics under a stale label"
        )
    if decoy_key not in members:
        findings.append(
            f"PRECONDITION {scenario.key}: the decoy run_key {decoy_key} is NOT in {relation['qualified']}'s "
            f"run_key MCV list {run_key_stats.get('most_common_vals')!r}; the pre-write ANALYZE recorded nothing "
            "about run_key, so 'stale' here is indistinguishable from 'absent'"
        )
    return findings


def _measure(
    connection: Any,
    scenario: Scenario,
    shape: str,
    chunk_relation: str,
    explain: Any = None,
) -> dict[str, Any]:
    """Capture the real statement for one cell and EXPLAIN it warm.

    ``explain`` is an injection seam, not configuration: it is the ONLY step
    here that needs a real PostgreSQL, so substituting an archived plan lets
    ``tests/test_river_ts_stats_harness_offline.py`` run this exact body — the
    capture, the digest, the companion handling and the keys ``_cell`` reads —
    without a database. The node-27 failure was in this body and no offline
    check reached it.
    """
    explain = _explain if explain is None else explain
    measurement: dict[str, Any] = {
        "branch": scenario.branch,
        "chunk_state": scenario.chunk_state,
        "chunk_relation": chunk_relation,
    }
    statement, companions, response = _capture_fact_statement(connection, scenario, shape)
    measurement["statement_head"] = " ".join(statement["sql"].split())[:400]
    measurement["statement_params"] = statement["params"]
    measurement["rows"] = statement_digest(statement)
    measurement["response_point_count"] = response_point_count(response)
    measurement["explain"] = explain(connection, statement)
    # Must-preserve #5: `_per_source_latest_cycles` is 98.8 % of the `latest`
    # shape's production cost. Recorded per cell as the baseline §4.3 compares
    # against; it is not gated here, which is what makes it §4.3's business.
    measurement["companions"] = [
        {
            "statement_head": " ".join(companion["sql"].split())[:200],
            "rows": statement_digest(companion),
            "root_shared_hit_blocks": explain(connection, companion)["root_shared_hit_blocks"],
        }
        for companion in companions
    ]
    return measurement


def _analyze_everything(
    connection: Any,
    scenarios: list[Scenario],
    scenario_states: Mapping[str, Any],
) -> dict[str, Any]:
    """``ANALYZE`` both hypertables, then each compressed relation explicitly.

    A compressed chunk's planner statistics live on its ``compress_hyper_*``
    relation, and ``ANALYZE`` on the hypertable is not relied on to reach it.
    """
    outcomes = {
        hypertable: analyze_relation(connection, hypertable)
        for hypertable in ("hydro.river_timeseries", "hydro.river_timeseries_legacy")
    }
    for scenario in scenarios:
        relation = scenario_states[scenario.key]["statistics_relation"]
        if relation["role"] == "compressed_relation":
            outcomes[relation["qualified"]] = analyze_relation(connection, relation["qualified"])
    return outcomes


def _judge(
    measurements: Mapping[tuple[str, str, str], Mapping[str, Any]],
    evidence: dict[str, Any],
    findings: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn the raw measurements into the per-cell table the gate reports.

    The ``fresh`` cells are judged on criteria 1 and 2 only and then supply
    criterion 3's baseline to their own scenario+shape — but only if they passed
    those two, so a broken ``fresh`` plan cannot inflate the bound into silence.
    """
    cells: list[dict[str, Any]] = []
    baselines: dict[tuple[str, str], int | None] = {}
    for scenario in SCENARIOS:
        for shape in SHAPES:
            measurement = measurements.get((scenario.key, shape, "fresh"))
            if measurement is None:
                continue
            # `fresh@absent` / `fresh@stale`: both scenarios of a branch x chunk
            # pair become `fresh` after ANALYZE, but they are DIFFERENT chunks
            # holding different rows, and each supplies criterion 3's baseline to
            # its own scenario. Collapsing them to one label would overwrite one
            # baseline with the other's.
            cell = _cell(scenario, shape, f"fresh@{scenario.statistics}", measurement, baseline=None)
            cells.append(cell)
            usable = cell["criterion_1_segment_identity_bound"] and cell["criterion_2_filter_ratio"]
            baselines[(scenario.key, shape)] = cell["max_shared_hit_blocks"] if usable else None
            if not usable:
                findings.append(
                    f"BASELINE LOST {cell['cell_key']}: the post-ANALYZE plan itself failed criterion "
                    f"{'1' if not cell['criterion_1_segment_identity_bound'] else '2'}, so criterion 3 has no "
                    "trustworthy bound for this scenario and is recorded as not evaluated"
                )

    for scenario in SCENARIOS:
        for shape in SHAPES:
            measurement = measurements.get((scenario.key, shape, scenario.statistics))
            if measurement is None:
                continue
            cells.append(
                _cell(
                    scenario,
                    shape,
                    scenario.statistics,
                    measurement,
                    baseline=baselines.get((scenario.key, shape)),
                )
            )

    # Coverage, asserted rather than assumed: a harness that measured NOTHING
    # would satisfy every criterion above vacuously, which is the module-level
    # version of the empty-extract trap.
    present = {cell["cell_key"] for cell in cells}
    for scenario in SCENARIOS:
        if scenario.triple in EXPECTED_UNCONSTRUCTIBLE:
            continue
        for shape in SHAPES:
            for statistics in (scenario.statistics, f"fresh@{scenario.statistics}"):
                key = f"{shape}/{statistics}/{scenario.branch}/{scenario.chunk_state}"
                if key not in present:
                    findings.append(
                        f"CELL MISSING {key}: a required cell of design.md's condition cross product "
                        "was not measured, so the gate would be green for a condition it never tested"
                    )

    # Must-preserve #1, within a scenario: the rows one shape returns may not
    # depend on the statistics state. If they do, the plan comparison between the
    # two states is a comparison of two different questions. Grouped per SCENARIO
    # because each scenario owns its own segments and runs, so digests are
    # expected to differ ACROSS scenarios. This is a SEPARATE question from the
    # recorded-expectation comparison below — it would still catch a
    # statistics-dependent read whose two states happened to share a wrong digest
    # with the recording, and it needs no recorded value to do it.
    digests: dict[str, set[str]] = {}
    for cell in cells:
        digests.setdefault(f"{cell['scenario']}/{cell['shape']}", set()).add(cell["digest"])
    for group, values in sorted(digests.items()):
        if len(values) > 1:
            findings.append(
                f"{_ROW_IDENTITY_PREFIX} {group}: statistics states returned different digests {sorted(values)}"
            )

    findings.extend(_compare_digests_against_recorded(cells))

    cells.sort(key=lambda cell: cell["cell_key"])
    evidence["cells"] = cells
    evidence["cell_table"] = [cell_row(cell) for cell in cells]
    # Computed HERE, inside the dump context, so the counts reach matrix.json on
    # a red run as well as a green one.
    evidence["summary"] = _summarise(cells)
    evidence["findings"] = findings
    return cells, findings


def _compare_digests_against_recorded(cells: list[dict[str, Any]]) -> list[str]:
    """Must-preserve #1: the shipped read may not change the rows it returns.

    Against ``RECORDED_ROW_DIGESTS`` — a fact measured on node-27 — rather than
    against a variant measured in the same process, which is what the
    three-variant spike compared and is no longer available. The recorded form is
    the stronger of the two: it holds the rows to a value from OUTSIDE this run,
    so a change that moved every cell equally would still be caught.

    Disqualifying regardless of plan quality, so it lands on the CELL — the cell
    row shows ``F`` — not only in the findings list. A scenario+shape with no
    recorded value is reported as UNVERIFIED rather than passed by default: a
    missing expectation is not evidence of row identity.
    """
    reported: list[str] = []
    for cell in cells:
        key = (str(cell["scenario"]), str(cell["shape"]))
        expected = RECORDED_ROW_DIGESTS.get(key)
        if expected is None:
            cell["digest_matches_recorded"] = None
            cell["failures"].append(
                f"{_ROW_IDENTITY_PREFIX}: no recorded digest for {key[0]}/{key[1]}, so this cell's row identity is "
                "unverified; record it from a measured node-27 run rather than leaving the cell unchecked"
            )
            cell["passed"] = False
            reported.append(f"{_ROW_IDENTITY_PREFIX} EXPECTATION MISSING {cell['cell_key']} ({key[0]}/{key[1]})")
            continue
        cell["digest_matches_recorded"] = cell["digest"] == expected
        if not cell["digest_matches_recorded"]:
            cell["failures"].append(
                f"{_ROW_IDENTITY_PREFIX}: digest {cell['digest']} differs from the recorded {expected}; "
                "a change that alters the returned rows is disqualified regardless of plan quality"
            )
            cell["passed"] = False
            reported.append(
                f"{_ROW_IDENTITY_PREFIX} {cell['cell_key']}: {cell['digest']} != recorded {expected} "
                f"(RECORDED_ROW_DIGESTS[{key!r}])"
            )
    return reported


def _cell(
    scenario: Scenario,
    shape: str,
    statistics: str,
    measurement: Mapping[str, Any],
    *,
    baseline: int | None,
) -> dict[str, Any]:
    chunk_relation = str(measurement.get("chunk_relation") or "")
    cell = evaluate_cell(
        measurement["explain"]["plan"],
        chunk_relation=chunk_relation,
        branch=scenario.branch,
        filter_ratio_limit=_FILTER_RATIO_LIMIT,
        shared_hit_baseline=baseline,
    )
    cell.update(
        {
            "cell_key": f"{shape}/{statistics}/{scenario.branch}/{scenario.chunk_state}",
            "shape": shape,
            "statistics": statistics,
            "chunk_state": scenario.chunk_state,
            "scenario": scenario.key,
            "digest": measurement["rows"]["digest"],
            "digest_rows": measurement["rows"]["digest_rows"],
            "point_count": measurement["response_point_count"],
            "execution_time_ms": measurement["explain"]["execution_time_ms"],
            "root_shared_hit_blocks": measurement["explain"]["root_shared_hit_blocks"],
            "companions": measurement["companions"],
            "statement_head": measurement["statement_head"],
        }
    )
    expected_rows = STEP_COUNT * EXPECTED_SERIES_BY_SHAPE[shape]
    # Gated on the MEASURED statement's own row count, which is what the plan
    # above describes, with the public response counted beside it: if the two
    # ever disagree the harness is digesting one thing and explaining another.
    for label, observed in (
        ("the measured statement returned", cell["digest_rows"]),
        ("the public response carried", cell["point_count"]),
    ):
        if observed != expected_rows:
            cell["failures"].append(
                f"NON-VACUITY: {label} {observed} rows, expected {expected_rows}; "
                "the plan above is measuring the wrong rows"
            )
            cell["passed"] = False
    return cell
