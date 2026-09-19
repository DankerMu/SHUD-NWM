"""The #2451 harness's capture and digest helpers, against the REAL producer.

Why this module exists
----------------------

The first node-27 run of the harness failed on EVERY cell with
``AttributeError: 'list' object has no attribute 'items'`` and judged nothing.
The cause was not a measurement: ``_response_digest`` did
``sorted(point.items())`` over ``series["points"]``, but ``forecast_series``
builds a forecast point as a two-element LIST
(``packages/common/forecast_store.py:4202``). Only the STATION series uses
mapping-shaped points (``:4263-4269``). The digest could never have worked
against a real response.

The offline dry-run that was supposed to catch it did not, because it fed the
harness hand-made measurements whose points were mappings — a check built from
the same wrong assumption as the code it checked, i.e. a check that could not
fail. So every case below builds its input by CALLING THE REAL PRODUCER
(``PsycopgForecastStore.forecast_series``) over a stub cursor, rather than by
hand. If the response or the binding shape drifts, these reddens here, with no
PostgreSQL and no TimescaleDB.

What the stub cursor is and is not
----------------------------------

It answers by SQL substring and returns canned rows. It is NOT a database and
proves nothing about plans or indexes — that is the integration module's job.
What it does prove is that the harness survives contact with the shapes the real
store produces: positional-tuple bindings from ``_validate_series_target``
(``forecast_store.py:608``, ``:621``), mapping bindings from ``_fetch_all``
(``:2778-2789``), two fact statements in the ``latest`` shape, one in the
run-bound shape, and list-shaped response points.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest

from packages.common import forecast_store as forecast_store_module
from packages.common.forecast_store import PsycopgForecastStore
from tests.river_ts_plan_criteria import cell_row
from tests.river_ts_stats_matrix_seed import (
    EXPECTED_UNCONSTRUCTIBLE,
    SCENARIOS,
    SHAPES,
    STEP_COUNT,
    _parse_array_literal,
)
from tests.test_river_timeseries_stats_index_choice_integration import (
    EXPECTED_OPEN_CELLS,
    RECORDED_ROW_DIGESTS,
    _capture_fact_statement,
    _cell,
    _compare_digests_against_recorded,
    _judge,
    _measure,
    _RecordingForecastStore,
    _relation_census,
    _reported_cell_failures,
    _summarise,
    response_point_count,
    statement_digest,
)

_NARROW_UNCOMPRESSED_ABSENT = SCENARIOS[0]
_CYCLE = _NARROW_UNCOMPRESSED_ABSENT.day
_TARGET_SCENARIO = "forecast_gfs_deterministic"
_DECOY_SCENARIO = "forecast_ifs_deterministic"


class _StubCursor:
    """Answers by SQL substring; records what it was asked and how it was bound."""

    def __init__(self, answers: Sequence[tuple[str, list[dict[str, Any]]]]) -> None:
        self._answers = list(answers)
        self._rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self.unmatched: list[str] = []

    def execute(self, statement: Any, parameters: Any = None) -> None:
        text = " ".join(str(statement).split())
        self.executed.append((text, parameters))
        for needle, rows in self._answers:
            if needle in text:
                self._rows = [dict(row) for row in rows]
                return
        self.unmatched.append(text[:160])
        self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class _StubConnection:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _StubCursor:
        return self._cursor


def _fact_rows(scenario_ids: Sequence[str]) -> list[dict[str, Any]]:
    """The columns the segment read actually SELECTs (``forecast_store.py:939-949``)."""
    rows: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        for step in range(STEP_COUNT):
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "model_id": "it126_model",
                    "source_id": "gfs",
                    "cycle_time": _CYCLE,
                    "run_end_time": _CYCLE + timedelta(hours=STEP_COUNT - 1),
                    "forcing_version_id": "it126_forcing_v1",
                    "river_network_version_id": "it126_rnv_v1",
                    "valid_time": _CYCLE + timedelta(hours=step),
                    "value": 1.0 + step,
                    "unit": "m3/s",
                }
            )
    return rows


def _answers(shape: str) -> list[tuple[str, list[dict[str, Any]]]]:
    """Ordered most-specific first: `core.basin_version` also appears INSIDE the
    fact statement's authority sub-select, so the fact statements must match
    before the validation ones do."""
    scenario_ids = [_TARGET_SCENARIO, _DECOY_SCENARIO] if shape == "latest" else [_TARGET_SCENARIO]
    return [
        ("pushdown_run_keys", _fact_rows(scenario_ids)),
        ("MAX(h.cycle_time) AS cycle_time", [{"scenario_id": name, "cycle_time": _CYCLE} for name in scenario_ids]),
        # #2417's run-identity resolution. It projected `h.run_key, h.run_id`
        # while the reader still bound a `pushdown_run_ids` text twin; #1342's
        # contract (task 6.3) deleted the twin, so only the key is selected.
        ("SELECT h.run_key FROM hydro.hydro_run h", [{"run_key": 11}]),
        ("rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id =", _fact_rows(scenario_ids)),
        ("FROM met.forcing_version", [{"forcing_version_id": "it126_forcing_v1", "lineage_json": None}]),
        ("SELECT basin_version_id FROM core.basin_version", [{"basin_version_id": "it126_basin_v1"}]),
        (
            "FROM core.river_segment rs",
            [
                {
                    "river_segment_id": _NARROW_UNCOMPRESSED_ABSENT.target_segment_id,
                    "properties_json": None,
                    "river_network_version_id": "it126_rnv_v1",
                }
            ],
        ),
    ]


def _capture(shape: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], _StubCursor]:
    cursor = _StubCursor(_answers(shape))
    connection = _StubConnection(cursor)
    statement, companions, response = _capture_fact_statement(connection, _NARROW_UNCOMPRESSED_ABSENT, shape)
    return statement, companions, response, cursor


# ---------------------------------------------------------------------------
# The bug that reached node-27
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["run_bound", "latest"])
def test_a_real_forecast_point_is_a_two_element_list_not_a_mapping(shape: str) -> None:
    """The producer's shape, asserted where the harness can see it.

    This is the assertion the previous offline dry-run was missing: it built its
    points by hand as mappings, so ``sorted(point.items())`` looked fine.
    """
    _, _, response, _ = _capture(shape)
    points = [point for series in response["series"] for point in series["points"]]
    assert points, "the stubbed read returned no points; the cases below would be vacuous"
    for point in points:
        assert isinstance(point, list), f"a forecast point is a list, got {type(point).__name__}: {point!r}"
        assert len(point) == 2
        assert isinstance(point[0], int)
        assert isinstance(point[1], float)
        assert not isinstance(point, Mapping)


@pytest.mark.parametrize("shape", ["run_bound", "latest"])
def test_the_digest_is_taken_over_the_recorded_fact_rows(shape: str) -> None:
    """Must-preserve #1, in ``probe1987.py``'s exact spelling, on real rows.

    Recomputed here from the rows the recording cursor captured, so the test
    fails if the harness ever digests a different object — including the API
    response, which is the mistake this file exists for.
    """
    statement, _, _, _ = _capture(shape)
    digest = statement_digest(statement)
    expected_rows = STEP_COUNT * (2 if shape == "latest" else 1)
    assert digest["digest_rows"] == expected_rows
    blob = "\n".join(repr(sorted(row.items())) for row in statement["rows"])
    assert digest["digest"] == hashlib.sha256(blob.encode()).hexdigest()[:16]
    assert len(digest["digest"]) == 16


@pytest.mark.parametrize("shape", ["run_bound", "latest"])
def test_the_response_point_count_never_looks_inside_a_point(shape: str) -> None:
    """The cross-check must not be the thing that breaks on a shape change."""
    _, _, response, _ = _capture(shape)
    assert response_point_count(response) == STEP_COUNT * (2 if shape == "latest" else 1)


def test_the_digest_reports_a_statement_that_was_never_fetched_instead_of_raising() -> None:
    assert statement_digest({"sql": "SELECT 1", "params": {}})["digest"] is None


# ---------------------------------------------------------------------------
# The neighbouring shape assumption: how the store BINDS its parameters
# ---------------------------------------------------------------------------


def test_the_capture_survives_the_positional_tuple_bindings_the_store_really_uses() -> None:
    """``_validate_series_target`` binds POSITIONAL TUPLES, not mappings.

    ``dict(parameters)`` on a tuple raises, which is the same class of bug as
    the digest one and the reason ``probe1987.py:56-67`` branches. Asserted
    against what the real store emitted, not against a hand-made call.
    """
    _, _, _, cursor = _capture("run_bound")
    positional = [
        (sql, parameters)
        for sql, parameters in cursor.executed
        if parameters is not None and not isinstance(parameters, Mapping)
    ]
    assert positional, (
        "the real store emitted no positional binding on this path; if that is now true the branch in "
        "_RecordingCursor.execute is dead code, but check before deleting it"
    )
    assert any("core.basin_version" in sql for sql, _ in positional)


def test_every_statement_the_store_issued_was_answered_by_the_stub() -> None:
    """Otherwise a silently-empty answer could make a case pass for nothing."""
    for shape in ("run_bound", "latest"):
        _, _, _, cursor = _capture(shape)
        assert not cursor.unmatched, f"{shape}: unanswered statements {cursor.unmatched}"


# ---------------------------------------------------------------------------
# The other shape the harness consumes but never made: PostgreSQL's own
# array output syntax, which is how `most_common_vals` reaches the F9b
# staleness precondition.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        # `SELECT most_common_vals::text` renders an anyarray with PostgreSQL's
        # array output syntax: braces, comma-separated, quoted only when the
        # element needs it. Integer surrogate keys are the unquoted form; the
        # quoted form is what a text column (`run_id`) produces.
        ("{11,12,13}", ["11", "12", "13"]),
        ('{"it2451_s0_target","it2451_s0_decoy"}', ["it2451_s0_target", "it2451_s0_decoy"]),
        ("{11}", ["11"]),
        ("{}", []),
        # A column with no MCV list has `most_common_vals IS NULL`, which psycopg2
        # hands over as None. It must read as "no members", never crash the
        # precondition that is trying to report a missing statistic.
        (None, []),
        ("", []),
    ],
)
def test_the_mcv_parse_matches_postgresql_array_output_syntax(literal: str | None, expected: list[str]) -> None:
    assert _parse_array_literal(literal) == expected


def test_the_mcv_parse_does_not_confuse_a_prefix_for_a_member() -> None:
    """The F9b precondition asks "is the target run_key IN the MCV list".

    Substring containment would say yes for ``1`` against ``{11,12}`` and the
    stale cell would silently be judged as fresh. Membership is on parsed
    elements for exactly that reason.
    """
    members = _parse_array_literal("{11,12}")
    assert "1" not in members
    assert "11" in members


# ---------------------------------------------------------------------------
# Statement selection, on the real statement set
# ---------------------------------------------------------------------------


def test_the_run_bound_shape_captures_the_bound_run_pushdown_statement() -> None:
    statement, companions, _, _ = _capture("run_bound")
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)" in statement["sql"]
    assert companions == []


def test_the_latest_shape_has_two_fact_statements_and_picks_the_cycle_window_one() -> None:
    """tasks.md 1.1: ``_per_source_latest_cycles`` and the cycle-window segment
    read both inline ``_segment_rows_source_sql``. Selecting "the only one"
    would raise; selecting the first would measure the wrong statement."""
    statement, companions, _, _ = _capture("latest")
    assert "%(pushdown_run_keys)s" in statement["sql"]
    assert "pushdown_window_start" in statement["sql"]
    assert len(companions) == 1
    assert "MAX(h.cycle_time)" in companions[0]["sql"]
    assert "pushdown_run_keys" not in companions[0]["sql"]


def test_the_latest_shape_records_the_companion_rows_too() -> None:
    """Must-preserve #5's baseline needs the companion, not just the measured one."""
    _, companions, _, _ = _capture("latest")
    assert statement_digest(companions[0])["digest_rows"] == 2


def test_an_unknown_shape_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown predicate shape"):
        _capture_fact_statement(_StubConnection(_StubCursor([])), _NARROW_UNCOMPRESSED_ABSENT, "sideways")


# ---------------------------------------------------------------------------
# The whole `_measure` -> `_cell` contract, offline
# ---------------------------------------------------------------------------

_MEASURED_CHUNK = "_hyper_6_2_chunk"

#: The 2026-09-17 throwaway measurement of the #2451 defect, transcribed from
#: ``.../receipts/2026-09-17-i8-explain-gate/stats-proof-2451.json``: the
#: discovery index chosen, ``river_segment_key`` demoted to a ``Filter``,
#: 71976 removed against 24 returned, 1581 shared hits against a 27 baseline.
_DEFECT_NODE = {
    "Node Type": "Index Scan",
    "Relation Name": _MEASURED_CHUNK,
    "Index Name": "_hyper_6_2_chunk_river_ts_run_discovery_key_idx",
    "Index Cond": "((run_key = $7) AND (basin_version_key = $4) AND (river_network_version_key = $6))",
    "Filter": "(river_segment_key = $5)",
    "Rows Removed by Filter": 71976,
    "Actual Rows": 24,
    "Actual Loops": 1,
    "Shared Hit Blocks": 1581,
    "Shared Read Blocks": 0,
    "Plan Rows": 1,
    "Startup Cost": 0.42,
    "Total Cost": 2.53,
}


def _archived_explain(_connection: Any, _statement: Mapping[str, Any]) -> dict[str, Any]:
    plan = {"Node Type": "Sort", "Actual Rows": 24, "Actual Loops": 1, "Plans": [_DEFECT_NODE]}
    return {
        "execution_time_ms": 22.254,
        "planning_time_ms": 2.492,
        "root_shared_hit_blocks": 1734,
        "root_shared_read_blocks": 0,
        "root_actual_rows": 24,
        "relation_census": _relation_census(plan),
        "plan": plan,
    }


@pytest.mark.parametrize("shape", ["run_bound", "latest"])
def test_measure_and_cell_agree_on_every_key_they_exchange(shape: str) -> None:
    """The node-27 abort was a key/shape mismatch INSIDE ``_measure``.

    This runs ``_measure``'s real body — real capture, real digest, real
    companion handling — with only the EXPLAIN replaced by the archived plan,
    then feeds the result to the real ``_cell``. Any key ``_cell`` reads and
    ``_measure`` stops writing reddens here, offline.
    """
    cursor = _StubCursor(_answers(shape))
    measurement = _measure(
        _StubConnection(cursor),
        _NARROW_UNCOMPRESSED_ABSENT,
        shape,
        _MEASURED_CHUNK,
        explain=_archived_explain,
    )
    cell = _cell(_NARROW_UNCOMPRESSED_ABSENT, shape, "absent", measurement, baseline=27)

    expected_rows = STEP_COUNT * (2 if shape == "latest" else 1)
    assert cell["digest_rows"] == expected_rows
    assert cell["point_count"] == expected_rows
    # Independently recaptured: the digest must be a function of the rows, not
    # an artefact of the one capture that produced this measurement.
    assert cell["digest"] == statement_digest(_capture(shape)[0])["digest"]
    # The archived plan IS the defect, so all three criteria must say so.
    assert cell["criterion_1_segment_identity_bound"] is False
    assert cell["criterion_2_filter_ratio"] is False
    assert cell["criterion_3_shared_hits"] is False
    assert cell["defect_reproduced"] is True
    assert cell["passed"] is False
    assert cell["cell_key"] == f"{shape}/absent/narrow/uncompressed"
    assert len(cell["companions"]) == (1 if shape == "latest" else 0)
    assert cell_row(cell).startswith(f"{shape}/absent/narrow/uncompressed: criteria=FFF")


def test_a_measurement_whose_statement_never_fetched_is_reported_not_crashed() -> None:
    """``statement_digest`` must degrade to a finding, never to a TypeError."""
    cursor = _StubCursor(_answers("run_bound"))
    measurement = _measure(
        _StubConnection(cursor),
        _NARROW_UNCOMPRESSED_ABSENT,
        "run_bound",
        _MEASURED_CHUNK,
        explain=_archived_explain,
    )
    measurement["rows"] = statement_digest({"sql": "SELECT 1", "params": {}})
    cell = _cell(_NARROW_UNCOMPRESSED_ABSENT, "run_bound", "absent", measurement, baseline=27)
    assert cell["passed"] is False
    assert any("NON-VACUITY" in failure for failure in cell["failures"])


# ---------------------------------------------------------------------------
# The gate's own bookkeeping — offline, because none of it needs a database and
# all of it can silently turn a red cell into a green run.
# ---------------------------------------------------------------------------


def _cell_stub(scenario: str, shape: str, digest: str | None, *, passed: bool = True) -> dict[str, Any]:
    return {
        "cell_key": f"{shape}/absent/{scenario.split('/')[0]}/{scenario.split('/')[1]}",
        "scenario": scenario,
        "shape": shape,
        "digest": digest,
        "passed": passed,
        "failures": [],
    }


def test_a_change_that_alters_the_returned_rows_is_disqualified_on_the_cell() -> None:
    """Must-preserve #1: the cell row must show F, not a footnote.

    A reader reads the per-cell table, not the findings list, so a digest
    mismatch that only produced a top-level finding would leave a disqualified
    cell looking green in the table the result is read from.
    """
    scenario = "narrow/uncompressed/absent"
    recorded = RECORDED_ROW_DIGESTS[(scenario, "latest")]
    cells = [
        _cell_stub(scenario, "latest", recorded),
        _cell_stub(scenario, "run_bound", "bbbbbbbbbbbbbbbb"),
    ]
    reported = _compare_digests_against_recorded(cells)

    assert cells[0]["digest_matches_recorded"] is True and cells[0]["passed"] is True
    assert cells[1]["digest_matches_recorded"] is False
    assert cells[1]["passed"] is False
    assert any("disqualified regardless of plan quality" in failure for failure in cells[1]["failures"])
    assert len(reported) == 1
    assert reported[0].startswith("ROW IDENTITY run_bound/absent/narrow/uncompressed: bbbbbbbbbbbbbbbb != recorded ")


def test_a_cell_with_no_recorded_digest_is_unverified_not_passed() -> None:
    """A missing expectation is not evidence of row identity."""
    cells = [_cell_stub("narrow/compressed/stale", "run_bound", "aaaaaaaaaaaaaaaa")]
    reported = _compare_digests_against_recorded(cells)
    assert cells[0]["digest_matches_recorded"] is None
    assert cells[0]["passed"] is False
    assert reported == [
        "ROW IDENTITY EXPECTATION MISSING run_bound/absent/narrow/compressed (narrow/compressed/stale/run_bound)"
    ]


def test_the_recorded_digests_cover_every_constructible_scenario_and_shape() -> None:
    """The expectation table is a closure over the cross product, not a sample.

    A scenario+shape missing from it would make its cells UNVERIFIED — which the
    gate reports rather than passes — but the cheaper statement of the same
    requirement is here, where it costs no database.
    """
    expected = {
        (scenario.key, shape)
        for scenario in SCENARIOS
        if scenario.triple not in EXPECTED_UNCONSTRUCTIBLE
        for shape in SHAPES
    }
    assert set(RECORDED_ROW_DIGESTS) == expected
    # 6 since task 6.3 collapsed the branch axis to `narrow` (was 12: the same
    # three constructible scenarios on each of two branches).
    assert len(RECORDED_ROW_DIGESTS) == 6
    for key, digest in RECORDED_ROW_DIGESTS.items():
        assert len(digest) == 16, key
        assert set(digest) <= set("0123456789abcdef"), key


def test_the_summary_is_written_into_the_evidence_not_only_the_message() -> None:
    """``matrix.json`` is what the parent reads; the assertion message is not.

    ``_evidence_dump`` writes the file in a ``finally``, so anything computed
    after that block would exist only in a red run's message. This asserts the
    summary is produced by ``_judge`` — which runs inside the dump context.
    """
    evidence: dict[str, Any] = {}
    cells, findings = _judge({}, evidence, [])
    assert cells == []
    assert findings, "an empty measurement set must report missing cells, not pass"
    assert set(evidence) == {"cells", "cell_table", "summary", "findings"}
    assert evidence["summary"]["measured"] == 0
    # Every required cell is reported missing, not silently absent. 12 since
    # task 6.3 collapsed the branch axis to `narrow`: 3 constructible scenarios
    # x 2 shapes x (its own statistics label + the `fresh@` one).
    assert sum(1 for finding in findings if finding.startswith("CELL MISSING ")) == 12


def test_the_summary_counts_the_cells_the_gate_reports() -> None:
    cells = [
        _cell_stub("narrow/uncompressed/absent", "run_bound", "d1"),
        _cell_stub("narrow/uncompressed/absent", "latest", "d2", passed=False),
    ]
    cells[1]["digest_matches_recorded"] = False
    assert _summarise(cells) == {
        "measured": 2,
        "passed": 1,
        "failed": 1,
        "digest_mismatch": 1,
        "expected_open_excused": 0,
    }


@pytest.mark.usefixtures("allowlisted")
def test_an_allowlisted_cell_failing_on_row_identity_is_not_counted_as_excused() -> None:
    """The summary must not say "excused" about a cell the gate reported."""
    excused = _open_cell(passed=False, failures=["criterion 2: ratio 999.0"])
    not_excused = _open_cell(passed=False, failures=["ROW IDENTITY: digest aaaa differs from the recorded bbbb"])
    assert _summarise([excused])["expected_open_excused"] == 1
    assert _summarise([not_excused])["expected_open_excused"] == 0
    assert len(_reported_cell_failures([not_excused])) == 1


# ---------------------------------------------------------------------------
# EXPECTED_OPEN_CELLS — empty since task 6.3, and the three ways an entry must
# not become a way to be green about something the gate never judged.
#
# The live table holds nothing (its one cell, `run_bound/stale/legacy/uncompressed`
# / #2471, left the cross product with the legacy branch). The MECHANISM still
# has to work for the next entry, so the cases below INJECT a synthetic entry
# with `monkeypatch` instead of leaning on a live one — which is what a live
# entry was doing before, and is why emptying the table would otherwise have
# deleted the coverage of the guard rather than of the excuse.
# ---------------------------------------------------------------------------

_OPEN_CELL = "run_bound/stale/narrow/uncompressed"
_OPEN_REASON = "synthetic entry owned by this test; the live allowlist is empty"


@pytest.fixture()
def allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put ONE synthetic entry on the gate's live allowlist, and take it back.

    ``setitem`` on the imported object rather than ``setattr`` on the gate
    module: the name imported here IS the module global ``_reported_cell_failures``
    reads, so mutating it is what the functions under test observe — and it
    avoids importing the gate module itself, which would make this suite an
    importer of ``tests/__init__.py`` and put it outside the selector's
    support-module routing closure.
    """
    monkeypatch.setitem(EXPECTED_OPEN_CELLS, _OPEN_CELL, _OPEN_REASON)


def _open_cell(*, passed: bool, failures: list[str]) -> dict[str, Any]:
    return {"cell_key": _OPEN_CELL, "passed": passed, "failures": failures}


def test_the_allowlist_is_empty_and_every_entry_must_name_a_measured_cell() -> None:
    """Nothing is excused today, and a future entry cannot name a dead cell.

    The old pin was an equality against ONE entry — the #2471 cell design.md §2.2
    left open. Task 6.3 removed the branch that cell lived on, so the entry is
    gone rather than stale. The equality is kept (a second entry added later must
    still be argued for, which is the failure mode an allowlist has) and is now
    joined by the check the old pin could not make: a key that no scenario/shape
    can produce would excuse a cell the gate never measures, and the CELL MISSING
    finding for it would be reported anyway.
    """
    assert EXPECTED_OPEN_CELLS == {}
    constructible = {
        f"{shape}/{statistics}/{scenario.branch}/{scenario.chunk_state}"
        for scenario in SCENARIOS
        if scenario.triple not in EXPECTED_UNCONSTRUCTIBLE
        for shape in SHAPES
        for statistics in (scenario.statistics, f"fresh@{scenario.statistics}")
    }
    assert set(EXPECTED_OPEN_CELLS) <= constructible
    # The fixture's synthetic key is one of them, so the cases below are not
    # excusing something the real cross product could never contain either.
    assert _OPEN_CELL in constructible


@pytest.mark.usefixtures("allowlisted")
def test_an_expected_open_cell_is_excused_for_its_plan_criteria() -> None:
    cell = _open_cell(passed=False, failures=["criterion 1: river_segment_key is not in the Index Cond"])
    assert _reported_cell_failures([cell]) == []


@pytest.mark.parametrize(
    "failure",
    [
        "ROW IDENTITY: digest aaaaaaaaaaaaaaaa differs from the recorded bbbbbbbbbbbbbbbb",
        "NON-VACUITY: the measured statement returned 0 rows, expected 24",
        # The allowlist excuses a cell whose PLAN is known-bad. An EMPTY EXTRACT
        # is not a bad plan — it is NO plan node for this chunk at all, so
        # criteria 1 and 2 are False having judged nothing, and every other check
        # on the cell (row identity, non-vacuity) still passes because the rows
        # come back correctly. Excusing it would print `criteria=FF-` and
        # `defect_reproduced=False` — which reads exactly like "still red as
        # expected" — while the gate went green having measured this cell not at
        # all. That is the module docstring's wrong-reason #1 reached THROUGH
        # wrong-reason #1b.
        "EMPTY EXTRACT: no plan node read _hyper_3_62_chunk; every criterion below would pass for nothing",
    ],
)
@pytest.mark.usefixtures("allowlisted")
def test_an_expected_open_cell_is_not_excused_for_what_the_allowlist_cannot_speak_to(failure: str) -> None:
    """The allowlist's reason covers the INDEX CHOICE and nothing else.

    Row identity and non-vacuity are independent of which index was taken. So is
    an empty extract, which reports that no index was taken here to have an
    opinion about.
    """
    cell = _open_cell(passed=False, failures=["criterion 1: river_segment_key is not in the Index Cond", failure])
    reported = _reported_cell_failures([cell])
    assert len(reported) == 1
    assert failure in reported[0]
    assert "criterion 1" not in reported[0]


@pytest.mark.usefixtures("allowlisted")
def test_an_expected_open_cell_that_starts_passing_is_itself_reported() -> None:
    """An allowlist must not outlive its reason — 6.3's entry is the precedent."""
    reported = _reported_cell_failures([_open_cell(passed=True, failures=[])])
    assert len(reported) == 1
    assert reported[0].startswith(f"EXPECTED-OPEN CELL NOW PASSES {_OPEN_CELL}")


def test_a_cell_that_is_not_allowlisted_is_reported_in_full() -> None:
    cell = {
        "cell_key": "run_bound/absent/narrow/uncompressed",
        "passed": False,
        "failures": ["criterion 2: ratio 999.0", "criterion 3: 5977 shared hits"],
    }
    reported = _reported_cell_failures([cell])
    assert reported == ["run_bound/absent/narrow/uncompressed: criterion 2: ratio 999.0; criterion 3: 5977 shared hits"]


def test_the_shipped_sql_carries_the_selected_c1_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.2 selected C1; §3.1 made it the rendering, with no switch left.

    Driven through the same ``_capture_fact_statement`` the harness uses, over the
    real store, so this is the statement the bench MEASURES and not a separately
    rendered template. The environment variable the spike used is asserted gone:
    if it ever comes back, the bench could be measuring something the product
    does not ship.
    """
    monkeypatch.setenv("NWM_RIVER_TS_SEGMENT_SPIKE", "c2")
    sql = _capture("run_bound")[0]["sql"]
    assert "rt.basin_version_key IS NOT DISTINCT FROM (" in sql
    assert "rt.river_network_version_key IS NOT DISTINCT FROM (" in sql
    assert "rt.basin_version_key = (" not in sql
    assert "rt.river_network_version_key = (" not in sql
    # The NULL guard is part of the measured statement, not a separate concern:
    # without it the two conjuncts diverge from `=` when both sides are NULL, and
    # the columns were declared nullable on the table this statement used to also
    # scan. The bench must measure the spelling that ships, guard included.
    #
    # ADJACENCY is the property; which keyword introduces the chain is not.
    # #1342's contract (task 6.3) deleted the store predicate that used to open
    # the WHERE clause, which promoted the guard from `AND` to `WHERE`.
    assert "rt.basin_version_key IS NOT NULL\n  AND rt.basin_version_key IS NOT DISTINCT FROM (\n" in sql
    assert (
        "rt.river_network_version_key IS NOT NULL\n"
        "  AND rt.river_network_version_key IS NOT DISTINCT FROM (\n"
    ) in sql
    # The conjunct #2451 exists to protect keeps its sargable `=`.
    assert "rt.river_segment_key = (" in sql
    # And no residue of the spike: neither the switch nor C2's outer layer.
    assert "spike_bv" not in sql
    assert not hasattr(forecast_store_module, "SEGMENT_SPIKE_ENV_VAR")


def test_the_recording_store_is_the_real_store() -> None:
    """Nothing above is a test of a mock: the class under test IS the product's.

    The stub replaces the CURSOR. Every statement, every binding and the whole
    response above are produced by ``PsycopgForecastStore.forecast_series``.
    """
    assert issubclass(_RecordingForecastStore, PsycopgForecastStore)
    assert _RecordingForecastStore.forecast_series is PsycopgForecastStore.forecast_series
