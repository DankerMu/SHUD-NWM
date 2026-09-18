"""#2451: the segment read must keep the segment key in its index condition.

What this module is
-------------------

It started as a one-cell hypothesis test — "does a MISSING chunk statistic make
the planner pick ``river_ts_run_discovery_key_idx``?" — and it answered yes
(``ANALYZE`` as the only variable; per-node ratio 2999 -> 0, 22.254 ms ->
0.255 ms, archived as
``openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/stats-proof-2451.json``).

It is now the MEASUREMENT HARNESS ``tasks.md`` §2.1 runs against each candidate
mechanism, and a GATE. It asserts that every measured cell of the condition
cross product satisfies ``design.md``'s three pass criteria; whether a cell
reproduces #2451 is recorded as DATA (``defect_reproduced``) and is never
asserted, so the module stays readable — and stays a regression guard — once a
candidate makes the cells green.

Against today's code it is expected to be RED: the absent/stale narrow cells are
the defect. Every failing cell is listed in one message; one red cell must not
hide the others.

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

Run on node-27 against a throwaway database:

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        NHMS_STATS_PROOF_OUTPUT=/home/nwm/tmp/2451-stats-proof.json \\
        uv run pytest -q tests/test_river_timeseries_stats_index_choice_integration.py
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Iterator, Mapping
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
from tests.river_ts_plan_criteria import DEFAULT_SHARED_HIT_MULTIPLE, cell_row, evaluate_cell, walk_plan
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


# ---------------------------------------------------------------------------
# Capturing the REAL statement
# ---------------------------------------------------------------------------


class _RecordingCursor:
    """Pass-through cursor that records every statement and parameter mapping.

    Same shape as the node-27 probe's ``_RecCursor``
    (``.../receipts/2026-09-17-i8-explain-gate/probe1987.py``), reproduced here
    rather than imported: that directory is archived evidence, not a library.
    """

    def __init__(self, cursor: Any, sink: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._sink = sink

    def execute(self, statement: Any, parameters: Any = None) -> None:
        if isinstance(parameters, Mapping):
            captured: Any = dict(parameters)
        elif parameters is None:
            captured = {}
        else:
            captured = list(parameters)
        self._sink.append({"sql": str(statement), "params": captured})
        self._cursor.execute(statement, parameters)

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


def _response_digest(response: Mapping[str, Any]) -> dict[str, Any]:
    """Must-preserve #1's digest, in the receipt's exact spelling."""
    points = [point for series in response.get("series") or [] for point in series.get("points") or []]
    payload = "\n".join(repr(sorted(point.items())) for point in points)
    return {
        "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "point_count": len(points),
    }


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
        "explain_rounds": _EXPLAIN_ROUNDS,
        "expected_unconstructible": sorted("/".join(triple) for triple in EXPECTED_UNCONSTRUCTIBLE),
    }
    with _evidence_dump(evidence):
        cells, findings = _measure_matrix(throwaway_database_url, evidence)

    table = "\n".join("  " + cell_row(cell) for cell in cells)
    failing = [cell for cell in cells if not cell["passed"]]
    reported = findings + [
        f"{cell['cell_key']}: " + "; ".join(cell["failures"]) for cell in failing
    ]
    assert not reported, (
        f"#2451 gate: {len(failing)} of {len(cells)} measured cells failed, {len(findings)} finding(s).\n"
        + "\n".join(f"  - {line}" for line in reported)
        + f"\n\nper-cell table (criteria = 1,2,3; P pass / F fail / - not evaluated):\n{table}"
        + f"\n\nevidence: {evidence.get('dump_path')}"
    )


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
    measurements: dict[tuple[str, str, str], dict[str, Any]] = {}
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


def _measure(connection: Any, scenario: Scenario, shape: str, chunk_relation: str) -> dict[str, Any]:
    """Capture the real statement for one cell and EXPLAIN it warm."""
    measurement: dict[str, Any] = {
        "branch": scenario.branch,
        "chunk_state": scenario.chunk_state,
        "chunk_relation": chunk_relation,
    }
    statement, companions, response = _capture_fact_statement(connection, scenario, shape)
    measurement["statement_head"] = " ".join(statement["sql"].split())[:400]
    measurement["statement_params"] = statement["params"]
    measurement["response"] = _response_digest(response)
    measurement["explain"] = _explain(connection, statement)
    # Must-preserve #5: `_per_source_latest_cycles` is 98.8 % of the `latest`
    # shape's production cost. Recorded per cell as the baseline §4.3 compares
    # against; it is not gated here, which is what makes it §4.3's business.
    measurement["companions"] = [
        {
            "statement_head": " ".join(companion["sql"].split())[:200],
            "root_shared_hit_blocks": _explain(connection, companion)["root_shared_hit_blocks"],
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
    """Turn the raw measurements into the per-cell table tasks.md §2.1 consumes.

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
                        f"CELL MISSING {key}: a required cell of design.md's condition cross product was not "
                        "measured, so the gate would be green for a condition it never tested"
                    )

    # Must-preserve #1, within a scenario: the rows one shape returns may not
    # depend on the statistics state. If they do, the plan comparison between the
    # two states is a comparison of two different questions. Grouped per SCENARIO
    # because each scenario owns its own segments and runs, so digests are
    # expected to differ ACROSS scenarios.
    digests: dict[str, set[str]] = {}
    for cell in cells:
        digests.setdefault(f"{cell['scenario']}/{cell['shape']}", set()).add(cell["digest"])
    for group, values in sorted(digests.items()):
        if len(values) > 1:
            findings.append(f"ROW IDENTITY {group}: statistics states returned different digests {sorted(values)}")

    cells.sort(key=lambda cell: cell["cell_key"])
    evidence["cells"] = cells
    evidence["cell_table"] = [cell_row(cell) for cell in cells]
    evidence["findings"] = findings
    return cells, findings


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
            "digest": measurement["response"]["digest"],
            "point_count": measurement["response"]["point_count"],
            "execution_time_ms": measurement["explain"]["execution_time_ms"],
            "root_shared_hit_blocks": measurement["explain"]["root_shared_hit_blocks"],
            "companions": measurement["companions"],
            "statement_head": measurement["statement_head"],
        }
    )
    expected_points = STEP_COUNT * EXPECTED_SERIES_BY_SHAPE[shape]
    if measurement["response"]["point_count"] != expected_points:
        cell["failures"].append(
            f"NON-VACUITY: the read returned {measurement['response']['point_count']} points, expected "
            f"{expected_points}; the plan above is measuring the wrong rows"
        )
        cell["passed"] = False
    return cell
