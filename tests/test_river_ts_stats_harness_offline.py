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
import os
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest

from packages.common.forecast_store import SEGMENT_SPIKE_ENV_VAR, PsycopgForecastStore
from tests.river_ts_plan_criteria import cell_row
from tests.river_ts_stats_matrix_seed import SCENARIOS, STEP_COUNT, _parse_array_literal
from tests.test_river_timeseries_stats_index_choice_integration import (
    SPIKE_VARIANTS,
    _capture_fact_statement,
    _cell,
    _compare_digests_against_base,
    _judge,
    _measure,
    _RecordingForecastStore,
    _relation_census,
    _spike_variant,
    _variant_summary,
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
        ("SELECT h.run_key, h.run_id", [{"run_key": 11, "run_id": "it2451_s0_target"}]),
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
    assert cell["variant"] == "base"
    assert len(cell["companions"]) == (1 if shape == "latest" else 0)
    # The variant leads the row: the table tasks.md §2.1 reads is grouped by
    # candidate, and a row that did not say which candidate produced it would be
    # unreadable once three variants share the file.
    assert cell_row(cell).startswith(f"base/{shape}/absent/narrow/uncompressed: criteria=FFF")


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
# The variant machinery (tasks.md 2.1) — offline, because none of it needs a
# database and all of it can silently corrupt the selection §2.2 makes.
# ---------------------------------------------------------------------------


def _variant_cell(variant: str, cell_key: str, digest: str | None, *, passed: bool = True) -> dict[str, Any]:
    return {
        "variant": variant,
        "cell_key": cell_key,
        "digest": digest,
        "passed": passed,
        "failures": [],
    }


def test_a_candidate_that_changes_the_returned_rows_is_disqualified_on_the_cell() -> None:
    """Must-preserve #1 across variants: the cell row must show F, not a footnote.

    A reader comparing two candidates' tables reads the rows, not the findings
    list, so a digest mismatch that only produced a top-level finding would leave
    a disqualified candidate looking green in the table it is chosen from.
    """
    cells = [
        _variant_cell("base", "latest/absent/narrow/uncompressed", "aaaaaaaaaaaaaaaa"),
        _variant_cell("c1", "latest/absent/narrow/uncompressed", "aaaaaaaaaaaaaaaa"),
        _variant_cell("c2", "latest/absent/narrow/uncompressed", "bbbbbbbbbbbbbbbb"),
    ]
    reported = _compare_digests_against_base(cells)

    assert cells[0]["digest_matches_base"] is True
    assert cells[1]["digest_matches_base"] is True and cells[1]["passed"] is True
    assert cells[2]["digest_matches_base"] is False
    assert cells[2]["passed"] is False
    assert any("disqualified regardless of plan quality" in failure for failure in cells[2]["failures"])
    assert reported == ["ROW IDENTITY c2/latest/absent/narrow/uncompressed: bbbbbbbbbbbbbbbb != base aaaaaaaaaaaaaaaa"]


def test_a_candidate_cell_with_no_base_measurement_is_unverified_not_passed() -> None:
    """Silence from an unmeasured base is not evidence of row identity."""
    cells = [_variant_cell("c1", "run_bound/stale/legacy/compressed", "aaaaaaaaaaaaaaaa")]
    reported = _compare_digests_against_base(cells)
    assert cells[0]["digest_matches_base"] is None
    assert cells[0]["passed"] is False
    assert reported == ["ROW IDENTITY BASE MISSING c1/run_bound/stale/legacy/compressed"]


def test_the_variant_summary_counts_every_variant_even_one_that_measured_nothing() -> None:
    """A candidate that rendered nothing must read as 0 measured, not be absent."""
    cells = [
        _variant_cell("base", "a", "d1"),
        _variant_cell("base", "b", "d2", passed=False),
        _variant_cell("c1", "a", "d1"),
    ]
    _compare_digests_against_base(cells)
    summary = _variant_summary(cells)
    assert set(summary) == {"base", "c1", "c2"}
    assert summary["base"] == {"measured": 2, "passed": 1, "failed": 1, "digest_mismatch": 0}
    assert summary["c1"] == {"measured": 1, "passed": 1, "failed": 0, "digest_mismatch": 0}
    assert summary["c2"] == {"measured": 0, "passed": 0, "failed": 0, "digest_mismatch": 0}


def test_the_per_variant_summary_is_written_into_the_evidence_not_only_the_message() -> None:
    """``matrix.json`` is what the parent reads; the assertion message is not.

    ``_evidence_dump`` writes the file in a ``finally``, so anything computed
    after that block would exist only in a red run's message. This asserts the
    summary is produced by ``_judge`` — which runs inside the dump context.
    """
    evidence: dict[str, Any] = {}
    cells, findings = _judge({}, evidence, [])
    assert cells == []
    assert findings, "an empty measurement set must report missing cells, not pass"
    assert set(evidence) == {"cells", "cell_table", "variant_summary", "findings"}
    assert set(evidence["variant_summary"]) == {"base", "c1", "c2"}
    # Every required cell of every variant is reported missing, not silently absent.
    assert all(any(f"CELL MISSING {variant}/" in finding for finding in findings) for variant in ("base", "c1", "c2"))


def test_base_is_first_so_every_other_variant_has_a_digest_to_compare_against() -> None:
    assert SPIKE_VARIANTS[0] == ("base", "")
    assert [label for label, _value in SPIKE_VARIANTS] == ["base", "c1", "c2"]
    assert [value for _label, value in SPIKE_VARIANTS] == ["", "c1", "c2"]


def test_the_spike_selection_is_restored_even_when_the_block_raises() -> None:
    """A leaked ``c2`` would make every later module render a candidate silently."""
    os.environ.pop(SEGMENT_SPIKE_ENV_VAR, None)
    with pytest.raises(RuntimeError), _spike_variant("c2"):
        assert os.environ[SEGMENT_SPIKE_ENV_VAR] == "c2"
        raise RuntimeError("boom")
    assert SEGMENT_SPIKE_ENV_VAR not in os.environ

    os.environ[SEGMENT_SPIKE_ENV_VAR] = "c1"
    try:
        with _spike_variant(""):
            assert os.environ[SEGMENT_SPIKE_ENV_VAR] == ""
        assert os.environ[SEGMENT_SPIKE_ENV_VAR] == "c1"
    finally:
        os.environ.pop(SEGMENT_SPIKE_ENV_VAR, None)


def test_the_selected_variant_reaches_the_sql_the_harness_captures() -> None:
    """The switch is only a measurement if the CAPTURED statement changes.

    Driven through the same ``_capture_fact_statement`` the harness uses, over
    the real store, so a variant that never reached the rendered SQL would be
    measured as three copies of base.
    """
    os.environ.pop(SEGMENT_SPIKE_ENV_VAR, None)
    base_sql = _capture("run_bound")[0]["sql"]
    with _spike_variant("c1"):
        c1_sql = _capture("run_bound")[0]["sql"]
    with _spike_variant("c2"):
        c2_sql = _capture("run_bound")[0]["sql"]

    assert "rt.basin_version_key = (" in base_sql
    assert "rt.basin_version_key IS NOT DISTINCT FROM (" in c1_sql
    assert "JOIN core.basin_version spike_bv" in c2_sql
    assert len({base_sql, c1_sql, c2_sql}) == 3


def test_the_recording_store_is_the_real_store() -> None:
    """Nothing above is a test of a mock: the class under test IS the product's.

    The stub replaces the CURSOR. Every statement, every binding and the whole
    response above are produced by ``PsycopgForecastStore.forecast_series``.
    """
    assert issubclass(_RecordingForecastStore, PsycopgForecastStore)
    assert _RecordingForecastStore.forecast_series is PsycopgForecastStore.forecast_series
