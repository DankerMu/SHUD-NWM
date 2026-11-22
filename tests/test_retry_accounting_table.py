"""Retry/attempt accounting, pinned in one table as it behaves today (#3264, T1).

Every row calls the real production function or method and compares it with a
literal expected value.  Divergences between sites are pinned as divergences
(two rows, same input where possible, different expected values), never
reconciled here: this table is the baseline the T2 slices (tasks 9.1-9.7 of
``openspec/changes/redesign-orchestration-core``) must keep green unchanged.

Sections:

1. ``retry_identity`` and chain pure rows (Appendix A rows C, F, L, M, N, Q).
2. Id formatting (``retry_identity.retry_job_id`` and the ``_pipeline_retry_job_id``
   delegate).
3. Stage attempt reads (``scheduler_state_rows`` and ``_stage_job_sort_key``),
   and the scheduler-side manual new attempt.
4. Limits (defaults and both env overrides; owner-held default limit and status sets).
5. Stub-driven rows (A/E mint, N precedence, the ``failure_number - 1`` formula,
   the two ``rsplit`` increments, the journal non-master path, the master vs
   non-master pair).
6. Status sets (the two inline sets vs ``retry.py``; owner-held default limit and
   status sets).
7. Integration index (rows A-limit, B, C-sibling, G-K, O, P, journal manual
   path): input, expected outcome and the existing pin, asserted to exist by
   node id.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from packages.common import status_vocab
from services.orchestrator import chain as _chain
from services.orchestrator import chain_forecast_execution, retry_identity, scheduler_runtime_roots
from services.orchestrator.chain import ForecastOrchestrator
from services.orchestrator.chain_runtime_utils import (
    _pipeline_retry_job_id,
    _restart_stage_from_basins,
    _retry_attempt_from_basins,
    _stage_job_sort_key,
)
from services.orchestrator.chain_types import StageDefinition, StageRunResult
from services.orchestrator.file_journal_retry_helpers import _next_current_master_retry_identity
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import (
    FAILED_RETRY_STATUSES,
    MANUAL_RETRY_SOURCE_STATUSES,
    RetryConfig,
)
from services.orchestrator.retry_identity import (
    cohort_retry_attempt_floors,
    effective_retry_attempt,
    member_charged_retry_attempt,
    retry_attempt_floor,
    retry_job_id,
    retry_suffix_attempt,
    split_retry_job_identity,
)
from services.orchestrator.scheduler_config.config import ProductionSchedulerConfig
from services.orchestrator.scheduler_state_manual_retry import _manual_retry_markers, _manual_retry_new_attempt
from services.orchestrator.scheduler_state_rows import (
    _job_retry_attempt,
    _state_retry_attempt,
    _state_retry_limit,
    stage_retry_attempt_floors,
)
from services.orchestrator.scheduler_state_types import DEFAULT_RETRY_LIMIT
from services.slurm_gateway.config import SlurmGatewaySettings

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "cycle_gfs_2026050100"
CYCLE_ID = "gfs_2026050100"
CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)
FORECAST_BASE = "job_cycle_gfs_2026050100_forecast"
FORCING_BASE = "job_cycle_gfs_2026050100_forcing"

FORECAST_STAGE = StageDefinition(
    stage="forecast",
    job_type="run_shud_forecast_array",
    template_name="unused.sbatch",
    success_cycle_status="unused",
    failure_cycle_status="unused",
    is_array=True,
)
FORCING_STAGE = StageDefinition(
    stage="forcing",
    job_type="produce_forcing_array",
    template_name="unused.sbatch",
    success_cycle_status="unused",
    failure_cycle_status="unused",
    is_array=True,
)


def _floor_evidence(stage: str, attempt: Any) -> dict[str, Any]:
    return {"retry_attempt_floor": {"stage": stage, "attempt": attempt}}


# ---------------------------------------------------------------------------
# 1. retry_identity and chain pure rows (Appendix A rows C, F, M, N, Q)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state_evidence", "stage", "expected"),
    [
        pytest.param(_floor_evidence("forecast", 2), "forecast", 2, id="C-matching-stage"),
        pytest.param(_floor_evidence("forecast", 0), "forecast", 0, id="C-zero-floor"),
        pytest.param(_floor_evidence("forecast", 2), "forcing", None, id="C-other-stage"),
        pytest.param(_floor_evidence("forecast", True), "forecast", None, id="C-bool-attempt"),
        pytest.param(_floor_evidence("forecast", "2"), "forecast", None, id="C-str-attempt"),
        pytest.param(_floor_evidence("forecast", -1), "forecast", None, id="C-negative-attempt"),
        pytest.param({"decision": "retry_transient_failure"}, "forecast", None, id="C-no-floor-field"),
        pytest.param(None, "forecast", None, id="C-no-evidence"),
    ],
)
def test_c_retry_attempt_floor(state_evidence: Any, stage: str, expected: int | None) -> None:
    assert retry_attempt_floor(state_evidence, stage) == expected


@pytest.mark.parametrize(
    ("basins", "expected"),
    [
        pytest.param(
            [
                {"model_id": "m_b", "state_evidence": _floor_evidence("forecast", 2)},
                {"model_id": "m_a", "state_evidence": _floor_evidence("forecast", 1)},
                {"model_id": "m_c"},
            ],
            [{"model_id": "m_a", "attempt": 1}, {"model_id": "m_b", "attempt": 2}],
            id="F-floored-members-sorted-unfloored-skipped",
        ),
        pytest.param(
            [{"model_id": "m_a", "state_evidence": _floor_evidence("forcing", 3)}],
            [],
            id="F-other-stage-floor-excluded",
        ),
        pytest.param(
            [
                {"model_id": "m_a", "state_evidence": _floor_evidence("forecast", 1)},
                {"model_id": "m_a", "state_evidence": _floor_evidence("forecast", 4)},
            ],
            [{"model_id": "m_a", "attempt": 4}],
            id="F-repeated-model-keeps-highest",
        ),
        pytest.param(None, [], id="F-no-basins"),
    ],
)
def test_f_cohort_retry_attempt_floors(basins: Any, expected: list[dict[str, Any]]) -> None:
    assert cohort_retry_attempt_floors(basins, "forecast") == expected


_F_FLOORS = [{"model_id": "m_a", "attempt": 1}, {"model_id": "m_b", "attempt": 3}]


@pytest.mark.parametrize(
    ("floors", "model_id", "effective", "expected"),
    [
        pytest.param([], "m_a", 5, 5, id="F-no-floors-shared-charge"),
        pytest.param(_F_FLOORS, "m_a", 4, 2, id="F-low-floor-member-own-charge"),
        pytest.param(_F_FLOORS, "m_b", 4, 4, id="F-max-floor-member"),
        pytest.param(_F_FLOORS, "m_a", 5, 3, id="F-extra-master-attempt-charged-on-top"),
        pytest.param(_F_FLOORS, "m_c", 4, 4, id="F-unrecorded-member-shared-charge"),
        pytest.param(_F_FLOORS, "m_a", 3, 3, id="F-master-not-above-max-floor-shared-charge"),
    ],
)
def test_f_member_charged_retry_attempt(floors: Any, model_id: str, effective: int, expected: int) -> None:
    assert member_charged_retry_attempt(floors, model_id, effective) == expected


@pytest.mark.parametrize(
    ("job_id", "expected"),
    [
        pytest.param("job_x", ("job_x", 0), id="M-no-suffix"),
        pytest.param("job_x_retry_1", ("job_x", 1), id="M-flat"),
        pytest.param("job_x_retry_1_retry_2", ("job_x_retry_1", 2), id="M-stacked-last-suffix"),
        pytest.param("job_x_retry_abc", ("job_x_retry_abc", 0), id="M-malformed-tail"),
        pytest.param("job_x_retry_1_retry_", ("job_x_retry_1_retry_", 0), id="M-empty-tail"),
        pytest.param("job_x_retry_-2", ("job_x", 0), id="M-negative-tail-clamped"),
        pytest.param(None, ("", 0), id="M-none"),
    ],
)
def test_m_split_retry_job_identity(job_id: str | None, expected: tuple[str, int]) -> None:
    assert split_retry_job_identity(job_id) == expected


@pytest.mark.parametrize(
    ("job_id", "expected"),
    [
        pytest.param("job_x_retry_1_retry_2", 2, id="M-stacked"),
        pytest.param("job_x_retry_7", 7, id="M-flat"),
        pytest.param("job_x_retry_abc", 0, id="M-malformed-tail"),
        pytest.param("job_x", 0, id="M-no-suffix"),
    ],
)
def test_m_retry_suffix_attempt(job_id: str, expected: int) -> None:
    assert retry_suffix_attempt(job_id) == expected


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        pytest.param([], 1, id="M-no-rows"),
        pytest.param([{"job_id": f"{FORECAST_BASE}_retry_1_retry_2", "stage": "forecast"}], 3, id="M-stacked"),
        pytest.param(
            [
                {"job_id": f"{FORECAST_BASE}_retry_1", "stage": "forecast"},
                {"job_id": f"{FORECAST_BASE}_retry_2", "stage": "forecast"},
            ],
            3,
            id="M-flat",
        ),
        pytest.param([{"job_id": f"{FORECAST_BASE}_retry_abc", "stage": "forecast"}], 1, id="M-malformed-tail-is-0"),
        pytest.param(
            [
                {"job_id": f"{FORECAST_BASE}_retry_abc", "stage": "forecast"},
                {"job_id": f"{FORECAST_BASE}_retry_2", "stage": "forecast"},
            ],
            3,
            id="M-malformed-tail-beside-flat",
        ),
        pytest.param(
            [{"job_id": f"{FORECAST_BASE}_retry_4", "stage": "forcing", "job_type": "produce_forcing_array"}],
            1,
            id="M-other-stage-row-excluded",
        ),
        pytest.param([{"job_id": "job_other_forecast_retry_4", "stage": "forecast"}], 1, id="M-other-prefix-excluded"),
        pytest.param(
            [{"job_id": f"{FORECAST_BASE}_retry_5", "job_type": "run_shud_forecast_array"}],
            6,
            id="M-job-type-match",
        ),
        pytest.param(
            [{"job_id": f"{FORECAST_BASE}_retry_1", "stage": "forecast", "retry_count": 9}],
            2,
            id="M-suffix-only-not-retry-count",
        ),
    ],
)
def test_m_next_retry_attempt_for_stage(jobs: list[dict[str, Any]], expected: int) -> None:
    assert _chain._next_retry_attempt_for_stage(jobs, base_job_id=FORECAST_BASE, stage=FORECAST_STAGE) == expected


@pytest.mark.parametrize(
    ("job_ids", "base_job_id", "expected"),
    [
        pytest.param(["B_retry_1_retry_2"], "B", 3, id="stacked-last-suffix"),
        pytest.param(["B_retry_2", "B_retry_5"], "B", 6, id="max-plus-one"),
        pytest.param(["B_retry_abc"], "B", 1, id="malformed-tail-is-0"),
        pytest.param([None, ""], "B", 1, id="none-and-empty-ids"),
        pytest.param(["C_retry_4"], "B", 1, id="other-prefix-excluded"),
        pytest.param(["B_retry_0"], "B", 1, id="zero-suffix-excluded"),
        pytest.param(["B_retry_-2"], "B", 1, id="negative-suffix-excluded"),
        pytest.param([], "B", 1, id="no-ids"),
    ],
)
def test_next_stage_attempt(job_ids: list[Any], base_job_id: str, expected: int) -> None:
    assert retry_identity.next_stage_attempt(job_ids, base_job_id) == expected


@pytest.mark.parametrize(
    ("floors", "occupied", "expected", "calls"),
    [
        pytest.param([1], set(), 2, None, id="floor-plus-one"),
        pytest.param([1, 3], set(), 4, None, id="max-floor-plus-one"),
        pytest.param([0], set(), 1, None, id="zero-floor"),
        pytest.param([2], {3}, 4, None, id="occupied-skipped"),
        pytest.param([2], {3, 4}, 5, [3, 4, 5], id="occupied-run-skipped-in-order"),
    ],
)
def test_mint_attempt(floors: list[int], occupied: set[int], expected: int, calls: list[int] | None) -> None:
    probed: list[int] = []

    def is_occupied(attempt: int) -> bool:
        probed.append(attempt)
        return attempt in occupied

    assert retry_identity.mint_attempt(floors, is_occupied) == expected
    if calls is not None:
        assert probed == calls


@pytest.mark.parametrize(
    ("retry_count", "suffix_attempt", "failure_number", "expected"),
    [
        pytest.param(5, 1, 1, 6, id="retry-count"),
        pytest.param(0, 4, 1, 5, id="suffix-attempt"),
        pytest.param(0, 0, 7, 7, id="failure-number-minus-one"),
        pytest.param(2, 2, 1, 3, id="master-strips-base"),
        pytest.param(0, 0, 0, 1, id="all-zero"),
    ],
)
def test_inline_retry_attempt(retry_count: int, suffix_attempt: int, failure_number: int, expected: int) -> None:
    assert retry_identity.inline_retry_attempt(retry_count, suffix_attempt, failure_number) == expected


STAGE_NAMES = ("convert", "forcing", "forecast", "parse", "state_save_qc", "publish")


@pytest.mark.parametrize(
    ("restart_stages", "expected"),
    [
        pytest.param(["parse", None, "forecast"], "forecast", id="L-earliest-marker-markerless-ignored"),
        pytest.param(["forecast"], "forecast", id="L-ordinary-restart-kept"),
        pytest.param(["publish", "convert"], "convert", id="L-earliest-of-two"),
        pytest.param([None, None], None, id="L-all-markerless"),
        pytest.param([], None, id="L-no-basins"),
    ],
)
def test_l_restart_stage_from_basins(restart_stages: list[str | None], expected: str | None) -> None:
    assert retry_identity.restart_stage_from_basins(restart_stages, STAGE_NAMES) == expected


@pytest.mark.parametrize(
    ("basins", "expected"),
    [
        pytest.param(
            [{"restart_stage": "parse"}, {}, {"restart_stage": "forecast"}], "forecast", id="L-delegate-earliest"
        ),
        pytest.param([{"restart_stage": "parse_output"}], "parse", id="L-delegate-alias"),
        pytest.param([{"restart_stage": "download"}], None, id="L-delegate-unknown-stage"),
        pytest.param([{"restart_stage": ""}], None, id="L-delegate-empty"),
        pytest.param([{}], None, id="L-delegate-markerless"),
        pytest.param(
            [{"restart_stage": "publish_tiles"}, {"restart_stage": "convert"}],
            "convert",
            id="L-delegate-alias-vs-earlier",
        ),
    ],
)
def test_l_restart_stage_from_basins_delegate(basins: list[dict[str, Any]], expected: str | None) -> None:
    assert _restart_stage_from_basins(basins) == expected


@pytest.mark.parametrize(
    ("basins", "expected"),
    [
        pytest.param([], None, id="N-no-basins"),
        pytest.param([{"retry_attempt": 2}], 2, id="N-direct-retry-attempt"),
        pytest.param([{"manual_retry_attempt": "3"}], 3, id="N-direct-manual-attempt-coerced"),
        pytest.param([{"retry_attempt": 0}], None, id="N-zero-not-positive"),
        pytest.param([{"retry_attempt": 2}, {"retry_attempt": 5}], 5, id="N-max-across-basins"),
        pytest.param(
            [{"state_evidence": {"decision": "manual_retry", "manual_retry": {"new_attempt": 4}}}],
            4,
            id="N-marker-claim-under-active-decision",
        ),
        pytest.param(
            [{"state_evidence": {"reason": "manual_retry_requested", "manual_retry": {"retry_count": 6}}}],
            6,
            id="N-marker-claim-under-active-reason",
        ),
        pytest.param(
            [{"state_evidence": {"decision": "retry_transient_failure", "manual_retry": {"new_attempt": 4}}}],
            None,
            id="N-marker-claim-without-active-decision-ignored",
        ),
    ],
)
def test_n_retry_attempt_from_basins(basins: list[dict[str, Any]], expected: int | None) -> None:
    assert _retry_attempt_from_basins(basins) == expected


@pytest.mark.parametrize(
    ("job_id", "recorded", "expected"),
    [
        pytest.param("X_retry_3", 0, 3, id="Q-count-0-suffix-N"),
        pytest.param("X_retry_3", None, 3, id="Q-count-absent-suffix-N"),
        pytest.param("X_retry_1_retry_2", 0, 2, id="Q-stacked-last-suffix"),
        pytest.param("X_retry_1", 4, 4, id="Q-count-wins"),
        pytest.param("X_retry_abc", 2, 2, id="Q-malformed-tail-count"),
        pytest.param("X", "bad", 0, id="Q-non-int-count"),
        pytest.param("X", -5, 0, id="Q-negative-count-clamped"),
    ],
)
def test_q_effective_retry_attempt(job_id: str, recorded: Any, expected: int) -> None:
    assert effective_retry_attempt(job_id, recorded) == expected


@pytest.mark.parametrize(
    ("job_id", "recorded", "expected"),
    [
        pytest.param("X_retry_3", 0, 4, id="Q-next-suffix-over-zero-count"),
        pytest.param("X_retry_1", 5, 6, id="Q-next-count-over-suffix"),
        pytest.param("X", "bad", 1, id="Q-next-non-int-count"),
        pytest.param(None, None, 1, id="Q-next-none"),
    ],
)
def test_q_next_effective_attempt(job_id: Any, recorded: Any, expected: int) -> None:
    assert retry_identity.next_effective_attempt(job_id, recorded) == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        pytest.param({"job_id": "X", "retry_count": 0}, ("X_retry_1", 1), id="Q-master-first-retry"),
        pytest.param({"job_id": "X_retry_1", "retry_count": 1}, ("X_retry_2", 2), id="Q-master-strips-base"),
        pytest.param({"job_id": "X_retry_3", "retry_count": 0}, ("X_retry_4", 4), id="Q-master-suffix-over-zero-count"),
        pytest.param(
            {"job_id": "X_retry_1_retry_2", "retry_count": None},
            ("X_retry_1_retry_3", 3),
            id="Q-master-stacked-strips-last-only",
        ),
        pytest.param({"job_id": "X_retry_1", "retry_count": 5}, ("X_retry_6", 6), id="Q-master-count-over-suffix"),
    ],
)
def test_q_next_current_master_retry_identity(current: dict[str, Any], expected: tuple[str, int]) -> None:
    assert retry_identity.next_after_failure_master(current["job_id"], current.get("retry_count")) == expected


@pytest.mark.parametrize(
    "current",
    [
        {"job_id": "X", "retry_count": 0},
        {"job_id": "X_retry_1", "retry_count": 1},
        {"job_id": "X_retry_3", "retry_count": 0},
        {"job_id": "X_retry_1_retry_2", "retry_count": None},
        {"job_id": "X_retry_1", "retry_count": 5},
    ],
)
def test_next_current_master_retry_identity_delegates_to_next_after_failure_master(current: dict[str, Any]) -> None:
    assert _next_current_master_retry_identity(current) == retry_identity.next_after_failure_master(
        current.get("job_id"), current.get("retry_count")
    )


@pytest.mark.parametrize(
    ("job_id", "retry_count", "expected"),
    [
        pytest.param("job_1_retry_1", 1, ("job_1_retry_1_retry_2", 2), id="Q-non-master-stacks"),
        pytest.param("job_1_retry_1", -1, ("job_1_retry_1_retry_0", 0), id="Q-non-master-negative-unclamped"),
        pytest.param("job_1_retry_1", 0, ("job_1_retry_1_retry_1", 1), id="Q-non-master-zero"),
    ],
)
def test_q_next_after_failure_non_master(job_id: str, retry_count: Any, expected: tuple[str, int]) -> None:
    assert retry_identity.next_after_failure_non_master(job_id, retry_count) == expected


# ---------------------------------------------------------------------------
# 2. Id formatting (9.1): ``retry_identity.retry_job_id`` owns the format;
#    ``_pipeline_retry_job_id`` is its delegate (parity rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        pytest.param(0, "base_retry_1", id="attempt-0-clamped"),
        pytest.param(-1, "base_retry_1", id="attempt-negative-clamped"),
        pytest.param(1, "base_retry_1", id="attempt-1"),
        pytest.param(3, "base_retry_3", id="attempt-3"),
    ],
)
def test_pipeline_retry_job_id(attempt: int, expected: str) -> None:
    assert retry_job_id("base", attempt) == expected


@pytest.mark.parametrize(
    ("base", "attempt", "expected"),
    [
        pytest.param("X_retry_1", 2, "X_retry_1_retry_2", id="stacked-base-kept"),
        pytest.param("X_retry_3", 1, "X_retry_3_retry_1", id="base-not-stripped"),
        pytest.param("base", "3", "base_retry_3", id="int-coercion"),
        pytest.param("base", 88, "base_retry_88", id="large-attempt"),
    ],
)
def test_retry_job_id_direct(base: str, attempt: Any, expected: str) -> None:
    assert retry_job_id(base, attempt) == expected


def test_retry_job_id_non_int_attempt_raises() -> None:
    with pytest.raises(ValueError):
        retry_job_id("base", "x")


@pytest.mark.parametrize("attempt", [-1, 0, 1, 3])
def test_pipeline_retry_job_id_delegates_to_retry_job_id(attempt: int) -> None:
    assert _pipeline_retry_job_id("base", attempt) == retry_job_id("base", attempt)


# ---------------------------------------------------------------------------
# 3. Stage attempt reads (9.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "stage", "expected"),
    [
        pytest.param(
            {"retry_count": 2, "pipeline_jobs": [{"job_id": "j_forecast_retry_5", "stage": "forecast"}]},
            None,
            2,
            id="stage-none-flat-count-wins",
        ),
        pytest.param(
            {
                "pipeline_jobs": [
                    {"job_id": "j_forecast_retry_5", "stage": "forecast", "retry_count": 1},
                    {"job_id": "j_forcing", "stage": "forcing", "retry_count": 3},
                ]
            },
            None,
            3,
            id="stage-none-no-flat-recorded-counts-only",
        ),
        pytest.param(
            {
                "retry_count": 9,
                "pipeline_jobs": [{"job_id": "j_forecast_retry_2", "stage": "forecast", "retry_count": 0}],
            },
            "forecast",
            2,
            id="stage-set-flat-excluded-suffix-counts",
        ),
        pytest.param(
            {
                "retry_count": 9,
                "pipeline_jobs": [{"job_id": "j_forecast_retry_1", "stage": "forecast"}],
                "stage_retry_attempt_floors": {"forecast": 4},
            },
            "forecast",
            4,
            id="stage-set-floor-counts",
        ),
        pytest.param(
            {
                "retry_count": 9,
                "pipeline_jobs": [{"job_id": "j_forcing_retry_7", "stage": "forcing", "retry_count": 7}],
            },
            "forecast",
            0,
            id="stage-set-non-matching-row-adds-0",
        ),
        # Stage-less read: recorded counts only, unclamped (negatives pass through).
        pytest.param({"pipeline_jobs": [{"job_id": "j", "retry_count": -3}]}, None, -3, id="stage-none-negative-count"),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j", "retry_count": -3}, {"job_id": "k", "retry_count": -1}]},
            None,
            -1,
            id="stage-none-negative-counts-max",
        ),
        pytest.param(
            {"retry_attempt": 4, "attempt": 5, "retry_count": 6}, None, 4, id="stage-none-flat-retry-attempt-first"
        ),
        pytest.param({"attempt": 5, "retry_count": 6}, None, 5, id="stage-none-flat-attempt-second"),
        pytest.param({"retry_attempt": "", "retry_count": 6}, None, 6, id="stage-none-flat-empty-skipped"),
        pytest.param({"retry_count": "x"}, None, 0, id="stage-none-flat-non-int-0"),
        pytest.param({"retry_count": -2}, None, -2, id="stage-none-flat-negative"),
        pytest.param({}, None, 0, id="stage-none-empty-state"),
        pytest.param(
            {"retry_count": 7, "pipeline_jobs": [{"job_id": "j_forecast_retry_5", "stage": "forecast"}]},
            "",
            7,
            id="stage-empty-string-reads-flat",
        ),
        # Canonical (stage-scoped) read: effective attempt per matching row, floored.
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_parse_retry_3", "stage": "parse"}]},
            "parse_output",
            3,
            id="stage-alias-parse-output",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_retry_3", "stage": "run_shud_forecast"}]},
            "forecast",
            3,
            id="row-stage-alias-run-shud-forecast",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_forecast_retry_1_retry_4", "stage": "forecast"}]},
            "forecast",
            4,
            id="stage-set-stacked-last-suffix",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_forecast_retry_abc", "stage": "forecast", "retry_count": "x"}]},
            "forecast",
            0,
            id="stage-set-malformed-suffix-non-int-count-0",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_forecast", "stage": "forecast", "retry_count": -4}]},
            "forecast",
            0,
            id="stage-set-negative-count-floored-0",
        ),
        pytest.param({"stage_retry_attempt_floors": {"forecast": -2}}, "forecast", 0, id="stage-set-negative-floor-0"),
        pytest.param(
            {
                "pipeline_jobs": [{"job_id": "j_forecast_retry_1", "stage": "forecast"}],
                "stage_retry_attempt_floors": {"forecast": "x"},
            },
            "forecast",
            1,
            id="stage-set-non-int-floor-0",
        ),
        pytest.param(
            {
                "pipeline_jobs": [{"job_id": "j_forecast_retry_1", "stage": "forecast"}],
                "stage_retry_attempt_floors": [5],
            },
            "forecast",
            1,
            id="stage-set-non-mapping-floors-0",
        ),
        pytest.param(
            {"stage_retry_attempt_floors": {"forecast": "6"}}, "forecast", 6, id="stage-set-no-rows-str-floor"
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_retry_2", "job_type": "forecast"}]},
            "forecast",
            2,
            id="stage-set-legacy-job-type",
        ),
        pytest.param(
            {
                "pipeline_jobs": [
                    {
                        "job_id": "job_cycle_gfs_2026050100_forecast_retry_3",
                        "stage": "forecast",
                        "run_id": "cycle_gfs_2026050100",
                    }
                ]
            },
            "forecast",
            3,
            id="stage-set-cycle-scope-row-counted",
        ),
        pytest.param(
            {
                "job_limit": 1,
                "pipeline_jobs": [
                    {"job_id": "a", "stage": "forecast"},
                    {"job_id": "b_retry_5", "stage": "forecast"},
                ],
            },
            "forecast",
            0,
            id="stage-set-job-limit-window",
        ),
        # Non-canonical read: candidate-scope rows at the literal stage, no floor.
        pytest.param(
            {
                "retry_count": 9,
                "pipeline_jobs": [
                    {
                        "job_id": "j_download_retry_3",
                        "stage": "download",
                        "model_id": "m",
                        "run_id": "fcst_gfs_2026050100_m",
                        "retry_count": 1,
                    }
                ],
            },
            "download",
            3,
            id="non-canonical-flat-excluded",
        ),
        pytest.param(
            {
                "pipeline_jobs": [
                    {
                        "job_id": "job_cycle_gfs_2026050100_download_retry_3",
                        "stage": "download",
                        "run_id": "cycle_gfs_2026050100",
                    }
                ]
            },
            "download",
            0,
            id="non-canonical-cycle-scope-subtracted",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "x_download_retry_3", "stage": "download", "run_id": "fcst_gfs_x"}]},
            "download",
            3,
            id="non-canonical-model-less-not-cycle-grammar",
        ),
        pytest.param({"retry_count": 9}, "download", 0, id="non-canonical-no-rows-0"),
        pytest.param(
            {
                "pipeline_jobs": [
                    {"job_id": "x_download_retry_2", "stage": "download", "model_id": "m", "retry_count": "x"}
                ]
            },
            "download",
            2,
            id="non-canonical-non-int-count-suffix",
        ),
        pytest.param(
            {"pipeline_jobs": [{"job_id": "j_forcing", "stage": "forcing", "retry_count": float("inf")}]},
            "download",
            0,
            id="non-canonical-matches-before-coercing",
        ),
    ],
)
def test_state_retry_attempt(state: dict[str, Any], stage: str | None, expected: int) -> None:
    assert _state_retry_attempt(state, stage=stage) == expected


@pytest.mark.parametrize(
    ("row_attempts", "floor", "expected"),
    [
        pytest.param([], None, 0, id="no-rows-no-floor-0"),
        pytest.param([-3, -1], None, -1, id="no-floor-negatives-unclamped"),
        pytest.param([2, 0, 5], None, 5, id="no-floor-max"),
        pytest.param([], 4, 4, id="no-rows-floor"),
        pytest.param([1], 4, 4, id="floor-above-rows"),
        pytest.param([3], -2, 3, id="negative-floor-rows-win"),
        pytest.param([], -2, 0, id="negative-floor-no-rows-0"),
        pytest.param([0, 0], 0, 0, id="all-zero"),
    ],
)
def test_stage_attempt(row_attempts: list[int], floor: int | None, expected: int) -> None:
    assert retry_identity.stage_attempt(row_attempts, floor) == expected


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(
            {"pipeline_jobs": [{"job_id": "a", "stage": "forcing", "retry_count": float("inf")}]},
            id="non-matching-row-count-coerced",
        ),
        pytest.param({"stage_retry_attempt_floors": {"forecast": float("inf")}}, id="floor-coerced"),
    ],
)
def test_state_retry_attempt_coerces_every_row_before_matching(state: dict[str, Any]) -> None:
    with pytest.raises(OverflowError):
        _state_retry_attempt(state, stage="forecast")


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        pytest.param(
            {"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": 5, "stage": "forecast"}, (1, 5), id="count-wins"
        ),
        pytest.param(
            {"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": 0, "stage": "forecast"}, (1, 0), id="zero-count-wins"
        ),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "stage": "forecast"}, (1, 2), id="count-missing-suffix"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_abc", "stage": "forecast"}, (1, 0), id="malformed-tail-0"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_-3"}, (1, -3), id="negative-tail-no-clamp"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_0"}, (1, 0), id="retry-0-base-matches-stage"),
        pytest.param({"job_id": f"{FORCING_BASE}_retry_1"}, (0, 1), id="other-stage-no-match"),
        # No ``stage`` / ``job_type``: ``stage_match`` comes only from the parsed base.
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": -1}, (1, 2), id="negative-count-suffix"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": "x"}, (1, 2), id="non-int-count-suffix"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": "3"}, (1, 3), id="str-count-wins"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": True}, (1, 1), id="bool-count-wins"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": 2.9}, (1, 2), id="float-count-truncated"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_1_retry_4"}, (0, 4), id="stacked-only-last-layer-stripped"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_"}, (1, 0), id="empty-tail-0"),
        pytest.param({"job_id": FORECAST_BASE}, (1, 0), id="no-suffix-0"),
        pytest.param({"job_id": None}, (0, 0), id="none-job-id"),
        pytest.param({}, (0, 0), id="empty-row"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_0", "retry_count": 2}, (1, 2), id="retry-0-count-wins"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_ 7"}, (1, 7), id="space-padded-tail-int"),
        pytest.param(
            {"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": 1}, (1, 1), id="count-present-base-stripped"
        ),
    ],
)
def test_stage_job_sort_key(job: dict[str, Any], expected: tuple[int, int]) -> None:
    assert _stage_job_sort_key(job, FORECAST_STAGE)[:2] == expected


def test_stage_job_sort_key_inf_count_raises() -> None:
    with pytest.raises(OverflowError):
        _stage_job_sort_key({"job_id": f"{FORECAST_BASE}_retry_2", "retry_count": float("inf")}, FORECAST_STAGE)


@pytest.mark.parametrize(
    ("job_id", "persisted_attempt", "expected"),
    [
        pytest.param("X_retry_2", 5, ("X", 5), id="count-wins"),
        pytest.param("X_retry_2", 0, ("X", 0), id="zero-count-wins"),
        pytest.param("X_retry_2", -1, ("X", 2), id="no-count-suffix"),
        pytest.param("X_retry_abc", -1, ("X", 0), id="malformed-tail-0"),
        pytest.param("X_retry_-3", -1, ("X", -3), id="negative-tail-no-clamp"),
        pytest.param("X_retry_1_retry_4", -1, ("X_retry_1", 4), id="stacked-last-layer"),
        pytest.param("X_retry_1_retry_4", 2, ("X_retry_1", 2), id="stacked-count-wins"),
        pytest.param("X_retry_", -1, ("X", 0), id="empty-tail-0"),
        pytest.param("X", -1, ("X", 0), id="no-suffix-no-count-0"),
        pytest.param("", -1, ("", 0), id="empty-id"),
        pytest.param("X_retry_0", 2, ("X", 2), id="retry-0-count-wins"),
        pytest.param("X_retry_ 7", -1, ("X", 7), id="space-padded-tail-int"),
        pytest.param("X", 3, ("X", 3), id="no-suffix-count"),
    ],
)
def test_stage_job_sort_attempt(job_id: str, persisted_attempt: int, expected: tuple[str, int]) -> None:
    assert retry_identity.stage_job_sort_attempt(job_id, persisted_attempt) == expected


@pytest.mark.parametrize(
    ("job", "sort_key_attempt", "effective_attempt"),
    [
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_3", "retry_count": 0}, 0, 3, id="count-wins-vs-max"),
        pytest.param({"job_id": f"{FORECAST_BASE}_retry_-3"}, -3, 0, id="negative-tail-vs-clamp"),
    ],
)
def test_stage_job_sort_key_diverges_from_effective_retry_attempt(
    job: dict[str, Any], sort_key_attempt: int, effective_attempt: int
) -> None:
    observed_sort_key = _stage_job_sort_key(job, FORECAST_STAGE)[1]
    observed_effective = effective_retry_attempt(job["job_id"], job.get("retry_count"))
    assert observed_sort_key == sort_key_attempt
    assert observed_effective == effective_attempt
    assert observed_sort_key != observed_effective


@pytest.mark.parametrize(
    ("job", "canonical_stage", "expected"),
    [
        pytest.param({"job_id": "x_retry_5", "retry_count": 2}, None, 2, id="stage-none-recorded-only"),
        pytest.param({"job_id": "x_retry_5", "stage": "forecast", "retry_count": 2}, "forecast", 5, id="matching-max"),
        pytest.param(
            {"job_id": "x_retry_1", "stage": "forecast", "retry_count": 4}, "forecast", 4, id="matching-count"
        ),
        pytest.param({"job_id": "x_retry_5", "stage": "forcing", "retry_count": 4}, "forecast", 0, id="non-matching-0"),
    ],
)
def test_job_retry_attempt(job: dict[str, Any], canonical_stage: str | None, expected: int) -> None:
    assert _job_retry_attempt(job, canonical_stage) == expected


def test_stage_retry_attempt_floors() -> None:
    jobs = [
        {"job_id": "a_forecast_retry_2", "stage": "forecast"},
        {"job_id": "b_forecast", "stage": "forecast", "retry_count": 3},
        {"job_id": "c_forecast_retry_3", "stage": "forecast"},
        {"job_id": "d_forcing_retry_1", "stage": "forcing"},
        {"job_id": "e_download_retry_4", "stage": "download"},
        {"job_id": "f_parse", "stage": "parse", "retry_count": 0},
    ]

    floors, sources = stage_retry_attempt_floors(jobs)

    assert floors == {"forecast": 3, "forcing": 1}
    assert [source["job_id"] for source in sources["forecast"]] == ["b_forecast", "c_forecast_retry_3"]
    assert [source["job_id"] for source in sources["forcing"]] == ["d_forcing_retry_1"]
    assert set(sources) == {"forecast", "forcing"}


@pytest.mark.parametrize(
    ("jobs", "expected_floors", "expected_sources"),
    [
        pytest.param(
            [{"job_id": "p_retry_2", "stage": "parse"}, {"job_id": "f_retry_1", "stage": "forecast"}],
            [("parse", 2), ("forecast", 1)],
            {"parse": ["p_retry_2"], "forecast": ["f_retry_1"]},
            id="key-order-first-positive-insertion",
        ),
        pytest.param(
            [{"job_id": "a_retry_1", "stage": "forecast"}, {"job_id": "b_retry_3", "stage": "forecast"}],
            [("forecast", 3)],
            {"forecast": ["b_retry_3"]},
            id="higher-attempt-resets-contributors",
        ),
        pytest.param(
            [{"job_id": "x", "stage": "download", "retry_count": float("inf")}],
            [],
            {},
            id="non-canonical-inf-count-never-read",
        ),
    ],
)
def test_stage_retry_attempt_floors_order(
    jobs: list[dict[str, Any]], expected_floors: list[tuple[str, int]], expected_sources: dict[str, list[str]]
) -> None:
    floors, sources = stage_retry_attempt_floors(jobs)

    assert list(floors.items()) == expected_floors
    assert {stage: [source["job_id"] for source in rows] for stage, rows in sources.items()} == expected_sources
    assert list(sources) == [stage for stage, _attempt in expected_floors]


def test_stage_retry_attempt_floors_canonical_inf_count_raises() -> None:
    with pytest.raises(OverflowError):
        stage_retry_attempt_floors([{"job_id": "x", "stage": "forecast", "retry_count": float("inf")}])


@pytest.mark.parametrize(
    ("previous_attempt", "expected"),
    [
        pytest.param(0, 1, id="zero"),
        pytest.param(3, 4, id="positive"),
        pytest.param(-2, -1, id="negative-unclamped"),
        pytest.param(-3, -2, id="negative-stays-negative"),
    ],
)
def test_next_manual_attempt(previous_attempt: int, expected: int) -> None:
    assert retry_identity.next_manual_attempt(previous_attempt) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param({"manual_retry": {"marker": True}, "retry_count": 2}, [3], id="L122-flat-count"),
        pytest.param(
            {"manual_retry": {"marker": True}, "pipeline_jobs": [{"job_id": "j", "retry_count": -3}]},
            [-2],
            id="L122-rows-negative-unclamped",
        ),
        pytest.param(
            {"manual_retry": {"marker": True, "new_attempt": "x"}, "retry_count": 2}, [3], id="L122-non-int-pin-default"
        ),
        pytest.param({"manual_retry": {"requested": True}, "retry_count": "x"}, [1], id="L122-requested-non-int-flat"),
        pytest.param(
            {"manual_retry": {"marker": True, "new_attempt": 7}, "retry_count": 2}, [7], id="L122-pinned-value-wins"
        ),
        pytest.param({"manual_retry": True, "retry_count": 2}, [3], id="L132-flat-count"),
        pytest.param({"manual_retry": "yes"}, [1], id="L132-empty-state"),
        pytest.param({"manual_retry": True, "retry_count": -1}, [0], id="L132-negative-flat-unclamped"),
        pytest.param({"manual_retry_marker": True, "attempt": 4}, [5], id="L132-marker-key-flat-attempt"),
    ],
)
def test_manual_retry_marker_default_attempt(state: dict[str, Any], expected: list[int]) -> None:
    assert [marker["attempt"] for marker in _manual_retry_markers(state)] == expected


def test_manual_retry_marker_default_attempt_is_eager() -> None:
    def state(retry_count: Any) -> dict[str, Any]:
        return {
            "manual_retry": {"marker": True, "new_attempt": 7},
            "pipeline_jobs": [{"job_id": "j", "retry_count": retry_count}],
        }

    # :122 -- the pinned ``new_attempt`` wins, yet the default is still computed.
    assert [marker["attempt"] for marker in _manual_retry_markers(state(0))] == [7]
    with pytest.raises(OverflowError):
        _manual_retry_markers(state(float("inf")))


_MANUAL_CYC = {
    "job_id": "job_cycle_gfs_2026070100_download",
    "run_id": "cycle_gfs_2026070100",
    "model_id": None,
    "stage": "download",
    "status": "failed",
    "retry_count": 4,
    "updated_at": "2026-07-01T00:00:00Z",
}
_MANUAL_OWN = {
    "job_id": "job_model_a_forecast",
    "run_id": "fcst_gfs_2026070100_model_a",
    "model_id": "model_a",
    "stage": "forecast",
    "status": "failed",
    "retry_count": 0,
    "updated_at": "2026-07-01T00:00:00Z",
}


def _manual_event(retry_count: Any) -> dict[str, Any]:
    return {
        "event_id": 9,
        "entity_type": "pipeline_job",
        "entity_id": "job_cycle_gfs_2026070100_download",
        "event_type": "retry",
        "created_at": "2026-07-01T01:00:00Z",
        "details": {
            "trigger": "manual",
            "manual_retry_marker": True,
            "retry_count": retry_count,
            "created_at": "2026-07-01T01:00:00Z",
            "previous_job_id": "job_cycle_gfs_2026070100_download",
        },
    }


def _manual_state(failed_stage: str, retry_count: Any, **extra: Any) -> dict[str, Any]:
    return {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": failed_stage,
        "pipeline_jobs": [dict(_MANUAL_CYC), dict(_MANUAL_OWN)],
        "pipeline_events": [_manual_event(retry_count)],
        **extra,
    }


_MANUAL_LIVE = {
    "pipeline_jobs": [
        {
            "job_id": "job_fcst_gfs_2026050100_m_forecast_retry_5",
            "stage": "forecast",
            "status": "cancelled",
            "model_id": "m",
            "run_id": "fcst_gfs_2026050100_m",
        }
    ]
}
_MANUAL_LIVE2 = {
    "pipeline_jobs": [
        {
            "job_id": "a_forecast_retry_2",
            "stage": "forecast",
            "status": "cancelled",
            "model_id": "m",
            "run_id": "fcst_gfs_2026050100_m",
        },
        {
            "job_id": "a_convert_retry_4",
            "stage": "convert",
            "status": "cancelled",
            "model_id": "m",
            "run_id": "fcst_gfs_2026050100_m",
        },
    ]
}
_MANUAL_NAMED = {
    "failed_stage": "forecast",
    "pipeline_jobs": [
        {
            "job_id": "j_forecast_retry_9",
            "stage": "forecast",
            "status": "failed",
            "model_id": "m",
            "run_id": "fcst_gfs_2026050100_m",
        }
    ],
}
_MANUAL_NULL_PIN = {"manual_retry": {"marker": True, "new_attempt": None}}


@pytest.mark.parametrize(
    ("state", "previous_attempt", "expected"),
    [
        pytest.param({"manual_retry": {"marker": True, "new_attempt": "x"}}, 3, 4, id="L1021-non-int-pin-default"),
        pytest.param({"manual_retry": {"marker": True, "new_attempt": 5}}, 0, 5, id="L1021-pinned-value-wins"),
        pytest.param(_manual_state("download", ""), 3, 4, id="L1035-empty-count"),
        pytest.param(_manual_state("download", ""), -2, 1, id="L1035-empty-count-negative-previous"),
        pytest.param(_manual_state("forecast", 5), 3, 4, id="L1037-non-pinning"),
        pytest.param(_manual_state("forecast", 5), -2, -1, id="L1037-non-pinning-negative-previous"),
        pytest.param(_manual_state("download", "x", **_MANUAL_NULL_PIN), 3, 4, id="L1038-non-int-pin-default"),
        pytest.param(_manual_state("download", 7, **_MANUAL_NULL_PIN), 0, 7, id="L1038-pinned-value-wins"),
        pytest.param({}, 3, 4, id="L1039-empty-state"),
        pytest.param({}, -2, -1, id="L1039-negative-previous-unclamped"),
        pytest.param({}, 0, 1, id="L1039-zero-previous"),
        pytest.param(_MANUAL_LIVE, 0, 6, id="L1039-family-floor-wins"),
        pytest.param(_MANUAL_LIVE, 7, 8, id="L1039-previous-wins"),
        pytest.param(_MANUAL_LIVE2, 0, 5, id="L1039-family-max"),
        pytest.param(_MANUAL_NAMED, 3, 4, id="L1039-named-failed-stage-previous"),
    ],
)
def test_manual_retry_new_attempt_sites(state: dict[str, Any], previous_attempt: int, expected: int) -> None:
    assert _manual_retry_new_attempt(state, previous_attempt=previous_attempt) == expected


def test_manual_retry_new_attempt_pin_default_is_eager_at_1021() -> None:
    def state(retry_count: Any) -> dict[str, Any]:
        return {
            "manual_retry": {"marker": True, "new_attempt": 7},
            "pipeline_jobs": [{**_MANUAL_LIVE["pipeline_jobs"][0], "retry_count": retry_count}],
        }

    assert _manual_retry_new_attempt(state(0), previous_attempt=0) == 7
    with pytest.raises(OverflowError):
        _manual_retry_new_attempt(state(float("inf")), previous_attempt=0)


def test_manual_retry_new_attempt_pin_default_is_eager_at_1038() -> None:
    def state(own_retry_count: Any) -> dict[str, Any]:
        st = _manual_state("download", 7, **_MANUAL_NULL_PIN)
        st["pipeline_jobs"][1]["retry_count"] = own_retry_count
        return st

    assert _manual_retry_new_attempt(state(0), previous_attempt=0) == 7
    with pytest.raises(OverflowError):
        _manual_retry_new_attempt(state(float("inf")), previous_attempt=0)


# ---------------------------------------------------------------------------
# 4. Limits (9.4): owner-held default limit and status sets
# ---------------------------------------------------------------------------

SCHEDULER_LIMIT_ENV = "NHMS_SCHEDULER_RETRY_LIMIT"
GATEWAY_LIMIT_ENV = "SLURM_GATEWAY_MAX_RETRIES"


def _clear_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCHEDULER_LIMIT_ENV, raising=False)
    monkeypatch.delenv(GATEWAY_LIMIT_ENV, raising=False)


def _scheduler_retry_limit(tmp_path: Path) -> int:
    return ProductionSchedulerConfig(workspace_root=tmp_path / "workspace").retry_limit


def _gateway_max_retries() -> int:
    return RetryConfig.from_settings(SlurmGatewaySettings()).max_retries


def test_default_limit_constants() -> None:
    assert DEFAULT_RETRY_LIMIT == 3
    assert RetryConfig().max_retries == 3


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param({"retry_limit": 2, "max_retries": 5}, 2, id="retry-limit-wins"),
        pytest.param({"max_retries": 5}, 5, id="max-retries-fallback"),
        pytest.param({}, 3, id="empty-default"),
        pytest.param({"retry_limit": "", "max_retries": None}, 3, id="blank-values-default"),
        pytest.param({"retry_limit": "abc"}, 0, id="non-int-0"),
        pytest.param({"max_retries": "abc"}, 0, id="non-int-max-retries-0"),
    ],
)
def test_state_retry_limit(state: dict[str, Any], expected: int) -> None:
    assert _state_retry_limit(state) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, 3, id="unset"),
        pytest.param("", 3, id="empty"),
        pytest.param("abc", 3, id="non-int"),
        pytest.param("5", 5, id="five"),
        pytest.param("-1", -1, id="negative-accepted"),
    ],
)
def test_env_int_reads_the_scheduler_retry_limit(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    _clear_limit_env(monkeypatch)
    if value is not None:
        monkeypatch.setenv(SCHEDULER_LIMIT_ENV, value)
    assert scheduler_runtime_roots._env_int(SCHEDULER_LIMIT_ENV, DEFAULT_RETRY_LIMIT) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, 3, id="unset"),
        pytest.param("", 3, id="empty"),
        pytest.param("abc", 3, id="non-int"),
        pytest.param("5", 5, id="five"),
        # ``_env_int`` returns -1; ``ProductionSchedulerConfig.__post_init__``
        # then clamps ``retry_limit`` to ``max(.., 0)``.
        pytest.param("-1", 0, id="negative-accepted-clamped-to-0"),
    ],
)
def test_scheduler_config_retry_limit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    _clear_limit_env(monkeypatch)
    if value is not None:
        monkeypatch.setenv(SCHEDULER_LIMIT_ENV, value)
    assert _scheduler_retry_limit(tmp_path) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, 3, id="unset"),
        pytest.param("5", 5, id="five"),
    ],
)
def test_gateway_max_retries_env(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int) -> None:
    _clear_limit_env(monkeypatch)
    if value is not None:
        monkeypatch.setenv(GATEWAY_LIMIT_ENV, value)
    assert _gateway_max_retries() == expected


def test_gateway_max_retries_env_negative_is_a_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_limit_env(monkeypatch)
    monkeypatch.setenv(GATEWAY_LIMIT_ENV, "-1")
    with pytest.raises(ValidationError):
        SlurmGatewaySettings()


def test_divergence_non_int_limit_state_reads_0_env_reads_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_limit_env(monkeypatch)
    monkeypatch.setenv(SCHEDULER_LIMIT_ENV, "abc")
    state_limit = _state_retry_limit({"retry_limit": "abc"})
    env_limit = scheduler_runtime_roots._env_int(SCHEDULER_LIMIT_ENV, DEFAULT_RETRY_LIMIT)
    assert (state_limit, env_limit) == (0, 3)
    assert state_limit != env_limit


def test_divergence_negative_limit_scheduler_accepts_gateway_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_limit_env(monkeypatch)
    monkeypatch.setenv(SCHEDULER_LIMIT_ENV, "-1")
    monkeypatch.setenv(GATEWAY_LIMIT_ENV, "-1")
    assert scheduler_runtime_roots._env_int(SCHEDULER_LIMIT_ENV, DEFAULT_RETRY_LIMIT) == -1
    assert _scheduler_retry_limit(tmp_path) == 0
    with pytest.raises(ValidationError):
        SlurmGatewaySettings()


def test_limit_envs_are_independent_scheduler_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_limit_env(monkeypatch)
    monkeypatch.setenv(SCHEDULER_LIMIT_ENV, "5")
    assert (_scheduler_retry_limit(tmp_path), _gateway_max_retries()) == (5, 3)


def test_limit_envs_are_independent_gateway_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_limit_env(monkeypatch)
    monkeypatch.setenv(GATEWAY_LIMIT_ENV, "5")
    assert (_scheduler_retry_limit(tmp_path), _gateway_max_retries()) == (3, 5)


# Owner-held default limit (9.4).  ``retry_identity`` owns ``DEFAULT_RETRY_LIMIT``;
# ``scheduler_state_types`` re-exports it and ``RetryConfig.max_retries`` defaults
# to it.  The gateway keeps its literal (it must not import the orchestrator), so
# the parity rows below are what bind it to the owner.


def test_owner_default_retry_limit() -> None:
    assert retry_identity.DEFAULT_RETRY_LIMIT == 3
    assert type(retry_identity.DEFAULT_RETRY_LIMIT) is int


def test_retry_config_default_max_retries() -> None:
    assert RetryConfig.__dataclass_fields__["max_retries"].default == 3
    assert RetryConfig().max_retries == 3


@pytest.mark.parametrize(
    "read_default",
    [
        pytest.param(lambda: SlurmGatewaySettings.model_fields["max_retries"].default, id="field-default"),
        pytest.param(lambda: SlurmGatewaySettings().max_retries, id="constructed"),
    ],
)
def test_gateway_default_equals_owner_limit(monkeypatch: pytest.MonkeyPatch, read_default: Any) -> None:
    _clear_limit_env(monkeypatch)
    assert read_default() == retry_identity.DEFAULT_RETRY_LIMIT


def _module_tree(relative_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def _imports_owner_limit(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "services.orchestrator.retry_identity"
        and any(alias.name == "DEFAULT_RETRY_LIMIT" for alias in node.names)
        for node in tree.body
    )


def _assert_retry_config_default_reads_the_owner(tree: ast.Module) -> None:
    (retry_config,) = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RetryConfig"]
    (max_retries,) = [
        node
        for node in retry_config.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "max_retries"
    ]
    assert isinstance(max_retries.value, ast.Name)
    assert max_retries.value.id == "DEFAULT_RETRY_LIMIT"
    assert _imports_owner_limit(tree)


def _assert_state_types_reexports_the_owner(tree: ast.Module) -> None:
    assigned = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    ]
    assert "DEFAULT_RETRY_LIMIT" not in assigned
    assert _imports_owner_limit(tree)


@pytest.mark.parametrize(
    ("relative_path", "check"),
    [
        pytest.param("services/orchestrator/retry.py", _assert_retry_config_default_reads_the_owner, id="retry"),
        pytest.param(
            "services/orchestrator/scheduler_state_types.py",
            _assert_state_types_reexports_the_owner,
            id="scheduler_state_types",
        ),
    ],
)
def test_limit_defaults_read_the_owner(relative_path: str, check: Any) -> None:
    check(_module_tree(relative_path))


def _orchestrator_import_targets(path: Path, package: str) -> list[str]:
    targets: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
            module = ".".join(part for part in (base, node.module or "") if part)
            targets.extend(f"{module}.{alias.name}" for alias in node.names)
    owner = "services.orchestrator"
    return [target for target in targets if target == owner or target.startswith(f"{owner}.")]


def test_gateway_config_does_not_import_the_orchestrator() -> None:
    path = REPO_ROOT / "services" / "slurm_gateway" / "config.py"
    assert _orchestrator_import_targets(path, "services.slurm_gateway") == []


# ---------------------------------------------------------------------------
# 5. Stub-driven rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("floors", "existing_ids", "repository_ids", "job_id", "expected"),
    [
        pytest.param([1], [], [], f"{FORECAST_BASE}_retry_1", f"{FORECAST_BASE}_retry_2", id="A-floor-plus-one"),
        pytest.param([1, 3], [], [], f"{FORECAST_BASE}_retry_1", f"{FORECAST_BASE}_retry_4", id="A-cohort-max-floor"),
        pytest.param([0], [], [], None, f"{FORECAST_BASE}_retry_1", id="A-none-job-id-with-floor"),
        pytest.param([], [], [], f"{FORECAST_BASE}_retry_1", f"{FORECAST_BASE}_retry_1", id="A-no-floor-unchanged"),
        pytest.param([], [], [], None, None, id="A-no-floor-none-stays-none"),
        pytest.param([2], [], [], f"{FORECAST_BASE}_retry_5", f"{FORECAST_BASE}_retry_5", id="A-suffix-above-floor"),
        pytest.param([2], [], [], f"{FORECAST_BASE}_retry_2", f"{FORECAST_BASE}_retry_3", id="A-suffix-equal-floor"),
        pytest.param(
            [2],
            [f"{FORECAST_BASE}_retry_3"],
            [],
            f"{FORECAST_BASE}_retry_1",
            f"{FORECAST_BASE}_retry_4",
            id="E-snapshot-occupied-skips",
        ),
        pytest.param(
            [2],
            [f"{FORECAST_BASE}_retry_4"],
            [f"{FORECAST_BASE}_retry_3"],
            f"{FORECAST_BASE}_retry_1",
            f"{FORECAST_BASE}_retry_5",
            id="E-journal-and-snapshot-occupied-skip",
        ),
    ],
)
def test_ae_floored_cycle_stage_retry_job_id(
    floors: list[int], existing_ids: list[str], repository_ids: list[str], job_id: str | None, expected: str | None
) -> None:
    stored = {stored_id: {"job_id": stored_id} for stored_id in repository_ids}
    stub_self = SimpleNamespace(repository=SimpleNamespace(get_pipeline_job=stored.get))
    context = SimpleNamespace(
        run_id=RUN_ID,
        active_basins=[{"state_evidence": _floor_evidence("forecast", floor)} for floor in floors]
        + [{"state_evidence": None}],
    )
    existing_jobs = [{"job_id": existing_id, "stage": "forecast"} for existing_id in existing_ids]

    observed = chain_forecast_execution._floored_cycle_stage_retry_job_id(
        stub_self, context, FORECAST_STAGE, existing_jobs, job_id
    )

    assert observed == expected


def test_e_floored_mint_probes_the_snapshot_before_the_journal() -> None:
    lookups: list[str] = []

    def get_pipeline_job(candidate: str) -> None:
        lookups.append(candidate)
        return None

    stub_self = SimpleNamespace(repository=SimpleNamespace(get_pipeline_job=get_pipeline_job))
    context = SimpleNamespace(run_id=RUN_ID, active_basins=[{"state_evidence": _floor_evidence("forecast", 2)}])
    existing_jobs = [{"job_id": f"{FORECAST_BASE}_retry_3", "stage": "forecast"}]

    observed = chain_forecast_execution._floored_cycle_stage_retry_job_id(
        stub_self, context, FORECAST_STAGE, existing_jobs, f"{FORECAST_BASE}_retry_1"
    )

    assert observed == f"{FORECAST_BASE}_retry_4"
    assert lookups == [f"{FORECAST_BASE}_retry_4"]


_STACKED_FORECAST_ROWS = [
    {"job_id": f"{FORECAST_BASE}_retry_1", "stage": "forecast"},
    {"job_id": f"{FORECAST_BASE}_retry_1_retry_2", "stage": "forecast"},
]


@pytest.mark.parametrize(
    ("retry_attempt", "existing_jobs", "expected"),
    [
        pytest.param(5, _STACKED_FORECAST_ROWS, f"{FORECAST_BASE}_retry_5", id="N-positive-claim-wins"),
        pytest.param(1, _STACKED_FORECAST_ROWS, f"{FORECAST_BASE}_retry_1", id="N-claim-below-rows-wins"),
        pytest.param(0, _STACKED_FORECAST_ROWS, f"{FORECAST_BASE}_retry_3", id="N-zero-falls-through"),
        pytest.param(None, _STACKED_FORECAST_ROWS, f"{FORECAST_BASE}_retry_3", id="N-absent-falls-through"),
        pytest.param(None, [], f"{FORECAST_BASE}_retry_1", id="N-absent-no-rows"),
        pytest.param(-2, _STACKED_FORECAST_ROWS, f"{FORECAST_BASE}_retry_1", id="N-negative-clamped"),
    ],
)
def test_n_retry_cycle_stage_job_id_precedence(
    retry_attempt: int | None, existing_jobs: list[dict[str, Any]], expected: str
) -> None:
    orchestrator = object.__new__(ForecastOrchestrator)
    context = SimpleNamespace(run_id=RUN_ID, retry_attempt=retry_attempt)

    assert orchestrator._retry_cycle_stage_job_id(context, FORECAST_STAGE, existing_jobs) == expected


def _forecast_master_record(job_id: str, retry_count: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": RUN_ID,
        "cycle_id": CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": "failed",
        "slurm_job_id": "4000",
        "candidate_id": RUN_ID,
        "retry_count": retry_count,
        "error_code": "NODE_FAILURE",
        "accepted_submit_contract_version": "nhms.accepted_submit.v1",
        "slurm_comment": "nhms:unused",
    }


@pytest.mark.parametrize(
    ("record_job_id", "record_retry_count", "result_job_id", "failure_number", "expected"),
    [
        # retry_count alone: the read side folds the suffix into it, so 5 > 1, 0.
        pytest.param(
            f"{FORECAST_BASE}_retry_1", 5, f"{FORECAST_BASE}_retry_1", 1, f"{FORECAST_BASE}_retry_6", id="D-retry-count"
        ),
        # suffix_attempt alone: the stored row carries no suffix and no count, so
        # ``job.retry_count`` is 0 and only the result id's suffix (4) is above it.
        pytest.param(
            FORECAST_BASE, 0, f"{FORECAST_BASE}_retry_4", 1, f"{FORECAST_BASE}_retry_5", id="D-suffix-attempt"
        ),
        # failure_number - 1 alone.
        pytest.param(FORECAST_BASE, 0, FORECAST_BASE, 7, f"{FORECAST_BASE}_retry_7", id="D-failure-number"),
        # The production shape (record id == result id): the master strips the base.
        pytest.param(
            f"{FORECAST_BASE}_retry_2",
            None,
            f"{FORECAST_BASE}_retry_2",
            1,
            f"{FORECAST_BASE}_retry_3",
            id="D-master-strips-base",
        ),
    ],
)
def test_d_schedule_cycle_stage_retry_failure_number_formula(
    record_job_id: str, record_retry_count: Any, result_job_id: str, failure_number: int, expected: str
) -> None:
    current = _forecast_master_record(record_job_id, record_retry_count)
    orchestrator = object.__new__(ForecastOrchestrator)
    orchestrator.repository = SimpleNamespace(get_pipeline_job=lambda _job_id: current)
    orchestrator.retry_service = SimpleNamespace(should_auto_retry=lambda _job: True)
    orchestrator.retry_config = RetryConfig(backoff_schedule=[0])
    result = StageRunResult(
        stage="forecast",
        job_type="run_shud_forecast_array",
        pipeline_job_id=result_job_id,
        slurm_job_id="4000",
        status="failed",
        error_code="NODE_FAILURE",
    )

    assert orchestrator._schedule_cycle_stage_retry(result, failure_number) == expected


class _CapturingReservationRepository:
    supports_accepted_submit_reconcile = True

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.records.append(dict(record))
        return dict(record)


def _reserved_submission_attempt(branch: str, pipeline_job_id: str, retry_attempt: int | None) -> int:
    repository = _CapturingReservationRepository()
    orchestrator = object.__new__(ForecastOrchestrator)
    orchestrator.repository = repository
    orchestrator.config = SimpleNamespace(reconcile_slurm_user="scheduler", reconcile_slurm_account="account")
    basins = [{"model_id": "model_0", "basin_id": "basin_0"}]
    context = SimpleNamespace(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        cycle_id=CYCLE_ID,
        run_id=RUN_ID,
        all_basins=basins,
        active_basins=basins,
        retry_attempt=retry_attempt,
        inherited_retry_attempt_floors=None,
    )
    if branch == "forecast":
        orchestrator._reserve_cycle_stage(FORECAST_STAGE, context, pipeline_job_id, f"{RUN_ID}:forecast")
    else:
        tasks = [
            {
                "task_id": 0,
                "candidate_id": "candidate_0",
                "run_id": "fcst_run_0",
                "model_id": "model_0",
                "basin_id": "basin_0",
                "scenario_id": "forecast_gfs_deterministic",
            }
        ]
        orchestrator._reserve_cycle_stage(
            FORCING_STAGE, context, pipeline_job_id, f"{RUN_ID}:forcing", forcing_member_tasks=tasks
        )
    (record,) = repository.records
    return record["submission_attempt"]


_INCREMENT_ROWS = [
    pytest.param("", None, 1, id="no-suffix"),
    pytest.param("_retry_2", None, 3, id="flat-suffix"),
    pytest.param("_retry_1_retry_2", None, 3, id="stacked-suffix"),
    pytest.param("_retry_abc", None, 1, id="malformed-tail"),
    pytest.param("_retry_abc", 4, 4, id="malformed-tail-context-attempt"),
    pytest.param("_retry_2", 6, 6, id="context-attempt-larger"),
    pytest.param("_retry_2", 0, 3, id="context-attempt-zero"),
]


@pytest.mark.parametrize(("suffix", "retry_attempt", "expected"), _INCREMENT_ROWS)
def test_forecast_reservation_submission_attempt_increment(
    suffix: str, retry_attempt: int | None, expected: int
) -> None:
    assert _reserved_submission_attempt("forecast", f"{FORECAST_BASE}{suffix}", retry_attempt) == expected


@pytest.mark.parametrize(("suffix", "retry_attempt", "expected"), _INCREMENT_ROWS)
def test_forcing_reservation_submission_attempt_increment(
    suffix: str, retry_attempt: int | None, expected: int
) -> None:
    assert _reserved_submission_attempt("forcing", f"{FORCING_BASE}{suffix}", retry_attempt) == expected


@pytest.mark.parametrize(
    ("suffix", "retry_attempt", "expected"),
    [*_INCREMENT_ROWS, pytest.param("_retry_-3", None, 1, id="negative-tail")],
)
def test_reservation_submission_attempt(suffix: str, retry_attempt: int | None, expected: int) -> None:
    assert retry_identity.reservation_submission_attempt(f"{FORECAST_BASE}{suffix}", retry_attempt) == expected


def test_reservation_submission_attempt_non_int_claim_raises() -> None:
    with pytest.raises(ValueError):
        retry_identity.reservation_submission_attempt("X", "x")


_JOURNAL_RUN_ID = "fcst_gfs_2026062800_model_a"
_JOURNAL_CYCLE_ID = "gfs_2026062800"


def _seed_non_master_failed_job(tmp_path: Path, *, job_id: str, retry_count: int) -> tuple[Any, dict[str, Any]]:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = {
        "job_id": job_id,
        "run_id": _JOURNAL_RUN_ID,
        "cycle_id": _JOURNAL_CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "reserved",
        "stage": "forecast",
        "idempotency_key": f"gfs:{_JOURNAL_CYCLE_ID}:basin_a:{job_id}",
        "candidate_id": "candidate_a",
        "retry_count": retry_count,
    }
    assert repository.reserve_pipeline_job(record) is not None
    repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="123")
    repository.update_pipeline_job_status(
        job_id,
        "failed",
        error_code="SLURM_TIMEOUT",
        error_message="SLURM_TIMEOUT failed",
        finished_at=datetime(2026, 6, 28, tzinfo=UTC),
    )
    durable = repository.get_pipeline_job(job_id)
    assert durable is not None
    return repository, durable


@pytest.mark.parametrize(
    ("durable_retry_count", "snapshot_retry_count", "expected_job_id", "expected_retry_count"),
    [
        pytest.param(0, 1, "job_1_retry_1_retry_2", 2, id="snapshot-count-read-not-durable"),
        pytest.param(0, 3, "job_1_retry_1_retry_4", 4, id="snapshot-count-larger"),
        pytest.param(5, 0, "job_1_retry_1_retry_1", 1, id="durable-count-ignored"),
        pytest.param(0, -1, "job_1_retry_1_retry_0", 0, id="snapshot-negative-unclamped"),
    ],
)
def test_journal_non_master_auto_retry_reads_the_caller_snapshot_and_stacks(
    tmp_path: Path,
    durable_retry_count: int,
    snapshot_retry_count: int,
    expected_job_id: str,
    expected_retry_count: int,
) -> None:
    repository, durable = _seed_non_master_failed_job(tmp_path, job_id="job_1_retry_1", retry_count=durable_retry_count)
    assert durable["retry_count"] == durable_retry_count
    snapshot = {**durable, "retry_count": snapshot_retry_count}

    retry = FileJournalRetryService(repository).schedule_auto_retry(snapshot)

    assert (retry.job_id, retry.retry_count) == (expected_job_id, expected_retry_count)


def test_master_and_non_master_paths_diverge_on_the_same_input(tmp_path: Path) -> None:
    same_input = {"job_id": "job_1_retry_1", "retry_count": 1}

    master_job_id, master_retry_count = _next_current_master_retry_identity(same_input)
    repository, durable = _seed_non_master_failed_job(tmp_path, job_id="job_1_retry_1", retry_count=1)
    non_master = FileJournalRetryService(repository).schedule_auto_retry({**durable, **same_input})

    assert (master_job_id, master_retry_count) == ("job_1_retry_2", 2)
    assert (non_master.job_id, non_master.retry_count) == ("job_1_retry_1_retry_2", 2)
    assert master_job_id != non_master.job_id


# ---------------------------------------------------------------------------
# 6. Status sets (9.4 / 9.6): owner-held default limit and status sets
# ---------------------------------------------------------------------------

STATUS_VOCAB_UNION = frozenset(
    status_vocab.PIPELINE_JOB_STATUSES | status_vocab.PIPELINE_JOB_INTERNAL_STATUSES | status_vocab.PIPELINE_JOB_SUCCESS
)
MANUAL_RETRY_INLINE_SET = frozenset(
    {"failed", "submission_failed", "permanently_failed", "cancelled", "partially_failed"}
)
UPSTREAM_REFRESH_INLINE_SET = frozenset({"failed", "submission_failed", "partially_failed"})


def _manual_retry_statuses() -> frozenset[str]:
    context = SimpleNamespace(retry_attempt=1, active_basins=(), restart_stage=None)
    return frozenset(
        status
        for status in STATUS_VOCAB_UNION
        if ForecastOrchestrator._terminal_stage_needs_manual_retry(
            context, {"status": status, "slurm_job_id": "4000", "stage": "forecast"}
        )
    )


def _upstream_refresh_statuses() -> frozenset[str]:
    refreshed = datetime(2026, 5, 1, 6, tzinfo=UTC)
    return frozenset(
        status
        for status in STATUS_VOCAB_UNION
        if ForecastOrchestrator._terminal_stage_can_retry_after_upstream_refresh(
            {"status": status}, refreshed_upstream_finished_at=refreshed
        )
    )


def test_status_vocab_union_covers_both_sets() -> None:
    assert STATUS_VOCAB_UNION >= MANUAL_RETRY_INLINE_SET | UPSTREAM_REFRESH_INLINE_SET


def test_manual_retry_inline_status_set_equals_manual_retry_source_statuses() -> None:
    observed = _manual_retry_statuses()
    assert observed == MANUAL_RETRY_INLINE_SET
    assert observed == MANUAL_RETRY_SOURCE_STATUSES


def test_upstream_refresh_inline_status_set_equals_failed_minus_permanently_failed() -> None:
    observed = _upstream_refresh_statuses()
    assert observed == UPSTREAM_REFRESH_INLINE_SET
    assert observed == FAILED_RETRY_STATUSES - {"permanently_failed"}


def test_the_two_inline_status_sets_differ() -> None:
    manual, refresh = _manual_retry_statuses(), _upstream_refresh_statuses()
    assert manual != refresh
    assert manual - refresh == {"permanently_failed", "cancelled"}
    assert refresh - manual == set()


# Owner-held status sets (9.4).  ``retry_identity`` owns both sets; the two
# predicate sites read them by name.  ``retry.py``'s sets stay separate (#3319).


def test_owner_manual_retryable_statuses() -> None:
    assert retry_identity.MANUAL_RETRYABLE_STATUSES == MANUAL_RETRY_INLINE_SET
    assert type(retry_identity.MANUAL_RETRYABLE_STATUSES) is frozenset


def test_owner_upstream_refresh_retryable_statuses() -> None:
    assert retry_identity.UPSTREAM_REFRESH_RETRYABLE_STATUSES == UPSTREAM_REFRESH_INLINE_SET
    assert type(retry_identity.UPSTREAM_REFRESH_RETRYABLE_STATUSES) is frozenset


def test_owner_manual_retryable_statuses_equal_manual_retry_source_statuses() -> None:
    assert retry_identity.MANUAL_RETRYABLE_STATUSES == MANUAL_RETRY_SOURCE_STATUSES


def test_owner_upstream_refresh_retryable_statuses_equal_failed_minus_permanently_failed() -> None:
    assert retry_identity.UPSTREAM_REFRESH_RETRYABLE_STATUSES == FAILED_RETRY_STATUSES - {"permanently_failed"}


_CYCLE_MODULE = "services.orchestrator.chain_forecast_orchestrator_cycle"


def _manual_retry_verdict(status: str) -> bool:
    context = SimpleNamespace(retry_attempt=1, active_basins=(), restart_stage=None)
    return ForecastOrchestrator._terminal_stage_needs_manual_retry(
        context, {"status": status, "slurm_job_id": "4000", "stage": "forecast"}
    )


def _upstream_refresh_verdict(status: str) -> bool:
    return ForecastOrchestrator._terminal_stage_can_retry_after_upstream_refresh(
        {"status": status}, refreshed_upstream_finished_at=datetime(2026, 5, 1, 6, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        pytest.param("MANUAL_RETRYABLE_STATUSES", _manual_retry_verdict, id="manual"),
        pytest.param("UPSTREAM_REFRESH_RETRYABLE_STATUSES", _upstream_refresh_verdict, id="upstream-refresh"),
    ],
)
def test_terminal_stage_predicates_read_the_owner_names(
    monkeypatch: pytest.MonkeyPatch, name: str, verdict: Any
) -> None:
    # Replacing the module global flips the verdict, so the site reads the
    # owner name (an equal inline literal would keep the old verdict).
    monkeypatch.setattr(f"{_CYCLE_MODULE}.{name}", frozenset({"succeeded"}))
    assert verdict("succeeded") is True
    assert verdict("failed") is False


# Status-set delegate rows (9.4): the sets are owner-held, but the status
# coercion (``str(job.get("status") or "")``) and the evaluation order stay at
# the two predicate sites.  Values observed on the pre-move code.

_MANUAL_JOB = {"slurm_job_id": "4000", "stage": "forecast"}


@pytest.mark.parametrize(
    ("job", "retry_attempt", "expected"),
    [
        pytest.param({**_MANUAL_JOB, "status": "failed"}, 0, True, id="failed-attempt-0"),
        pytest.param({**_MANUAL_JOB, "status": "cancelled"}, 1, True, id="cancelled"),
        pytest.param({**_MANUAL_JOB, "status": "permanently_failed"}, 1, True, id="permanently-failed"),
        pytest.param({**_MANUAL_JOB, "status": "failed"}, None, False, id="failed-attempt-none"),
        pytest.param({**_MANUAL_JOB, "status": "succeeded"}, 1, False, id="succeeded"),
        pytest.param({**_MANUAL_JOB, "status": None}, 1, False, id="status-none"),
        pytest.param({**_MANUAL_JOB, "status": "FAILED"}, 1, False, id="upper-case"),
        pytest.param({**_MANUAL_JOB, "status": " failed"}, 1, False, id="leading-space"),
        pytest.param(dict(_MANUAL_JOB), 1, False, id="status-missing"),
        pytest.param({**_MANUAL_JOB, "status": 5}, 1, False, id="status-int"),
        pytest.param({**_MANUAL_JOB, "status": "partially_failed"}, None, False, id="partially-failed-attempt-none"),
        pytest.param({"status": "reservation_lost", "stage": "forecast"}, 1, False, id="reservation-lost-no-slurm-id"),
    ],
)
def test_terminal_stage_needs_manual_retry_delegate(
    job: dict[str, Any], retry_attempt: int | None, expected: bool
) -> None:
    context = SimpleNamespace(retry_attempt=retry_attempt, active_basins=(), restart_stage=None)
    assert ForecastOrchestrator._terminal_stage_needs_manual_retry(context, job) is expected


_REFRESHED_AT = datetime(2026, 5, 1, 6, tzinfo=UTC)
_AT_REFRESH = "2026-05-01T06:00:00+00:00"
_BEFORE_REFRESH = "2026-05-01T05:00:00+00:00"
_AFTER_REFRESH = "2026-05-01T06:00:01+00:00"


@pytest.mark.parametrize(
    ("job", "refreshed", "expected"),
    [
        pytest.param({"status": "failed"}, _REFRESHED_AT, True, id="failed-no-time"),
        pytest.param({"status": "failed", "finished_at": _AT_REFRESH}, _REFRESHED_AT, True, id="failed-at-refresh"),
        pytest.param({"status": "failed", "finished_at": _BEFORE_REFRESH}, _REFRESHED_AT, True, id="failed-before"),
        pytest.param(
            {"status": "partially_failed", "finished_at": _BEFORE_REFRESH}, _REFRESHED_AT, True, id="partial-before"
        ),
        pytest.param({"status": "failed"}, None, False, id="refreshed-none"),
        pytest.param({"status": "failed", "finished_at": _AFTER_REFRESH}, _REFRESHED_AT, False, id="failed-after"),
        pytest.param(
            {"status": "submission_failed", "finished_at": _AFTER_REFRESH}, _REFRESHED_AT, False, id="submission-after"
        ),
        pytest.param(
            {"status": "permanently_failed", "finished_at": _BEFORE_REFRESH},
            _REFRESHED_AT,
            False,
            id="permanently-failed-before",
        ),
        pytest.param({"status": "cancelled"}, _REFRESHED_AT, False, id="cancelled-no-time"),
        pytest.param({"status": None}, _REFRESHED_AT, False, id="status-none"),
        pytest.param({}, _REFRESHED_AT, False, id="status-missing"),
        pytest.param({"status": "FAILED"}, _REFRESHED_AT, False, id="upper-case"),
        pytest.param({"status": " failed"}, _REFRESHED_AT, False, id="leading-space"),
        pytest.param({"status": 5}, _REFRESHED_AT, False, id="status-int"),
        pytest.param({"status": ["failed"]}, _REFRESHED_AT, False, id="status-list"),
        # The status check runs before the terminal-time parse.
        pytest.param({"status": "cancelled", "finished_at": "x"}, _REFRESHED_AT, False, id="cancelled-unparseable"),
        # The refreshed-time check runs before both.
        pytest.param({"status": "failed", "finished_at": "x"}, None, False, id="refreshed-none-unparseable"),
    ],
)
def test_terminal_stage_can_retry_after_upstream_refresh_delegate(
    job: dict[str, Any], refreshed: datetime | None, expected: bool
) -> None:
    result = ForecastOrchestrator._terminal_stage_can_retry_after_upstream_refresh(
        job, refreshed_upstream_finished_at=refreshed
    )
    assert result is expected


def test_terminal_stage_can_retry_after_upstream_refresh_unparseable_time_raises() -> None:
    with pytest.raises(ValueError):
        ForecastOrchestrator._terminal_stage_can_retry_after_upstream_refresh(
            {"status": "failed", "finished_at": "x"}, refreshed_upstream_finished_at=_REFRESHED_AT
        )


# ---------------------------------------------------------------------------
# 7. Integration index (8.2): rows pinned by existing integration tests
# ---------------------------------------------------------------------------

INDEX: tuple[dict[str, Any], ...] = (
    {
        "row": "A-limit",
        "input": "#2404: failed row under ..._full_<m> at suffix 1, stage re-minted under ..._forecast_<m>",
        "expected": "limit 3 blocks on time (the budget counts across prefixes)",
        "pins": (
            "tests/test_retry_mint_floor.py::test_prefix_switch_does_not_reset_the_minted_attempt_and_the_budget_blocks_on_time",
            "tests/test_retry_mint_floor.py::test_failure_lane_prefix_switch_stays_within_the_limit",
        ),
    },
    {
        "row": "B",
        "input": "#2404: the cohort digest changes between attempts",
        "expected": "no escape from the budget",
        "pins": (
            "tests/test_retry_mint_floor.py::test_cohort_digest_changes_do_not_escape_the_budget",
            "tests/test_retry_mint_floor.py::test_failure_lane_cohort_digest_change_stays_within_the_limit",
        ),
    },
    {
        "row": "C-sibling",
        "input": "#2404: sibling-model retry rows exist",
        "expected": "they do not raise this model's floor",
        "pins": ("tests/test_retry_mint_floor.py::test_sibling_model_retry_rows_do_not_raise_this_models_floor",),
    },
    {
        "row": "G",
        "input": "#1845: a sibling model-set's terminal forcing row under the shared base id",
        "expected": "not resumed",
        "pins": (
            "tests/test_chain_cross_stage_state.py::test_sibling_model_set_terminal_forcing_row_under_shared_base_id_is_not_resumed",
        ),
    },
    {
        "row": "H",
        "input": "#1845: this model-set's own terminal forcing row",
        "expected": "resumed verbatim",
        "pins": ("tests/test_chain_cross_stage_state.py::test_own_model_set_terminal_forcing_row_is_resumed_verbatim",),
    },
    {
        "row": "I",
        "input": "#1845: re-entry after a nested partial retry",
        "expected": "the mint lands past the excluded subset row",
        "pins": (
            "tests/test_chain_cross_stage_state.py::test_reentry_after_nested_partial_retry_mints_past_the_excluded_subset_row",
        ),
    },
    {
        "row": "J",
        "input": "#1845: as I, with the budget spent",
        "expected": "a durable decline",
        "pins": (
            "tests/test_chain_cross_stage_state.py::test_reentry_after_nested_partial_retry_with_spent_budget_declines_durably",
        ),
    },
    {
        "row": "K",
        "input": "#1845: a concurrent pass after a fresh submit",
        "expected": "the suffix does not advance",
        "pins": (
            "tests/test_chain_cross_stage_state.py::test_concurrent_retry_reservation_after_fresh_submit_does_not_advance_the_suffix",
        ),
    },
    {
        "row": "O",
        "input": "#1201: a manual attempt claim with no active manual-retry decision",
        "expected": "the claim is ignored",
        "pins": (
            "tests/test_orchestration_chain.py::test_manual_retry_claim_judgement_three_states",
            "tests/test_production_scheduler.py::test_candidate_basin_manifest_mints_retry_attempt_only_under_active_manual_retry_decision",
        ),
    },
    {
        "row": "P",
        "input": "#1356: retry identity from the selected-jobs snapshot",
        "expected": "derived from the snapshot only (no dedicated pin; the nearest edit site is listed)",
        "pins": (
            "tests/test_orchestration_chain.py::test_file_journal_post_window_concurrent_public_cycles_submit_one_retry",
        ),
    },
    {
        "row": "journal-manual",
        "input": "file journal manual repair of a suffix-only target",
        "expected": "the next durable attempt is effective_retry_attempt(...) + 1",
        "pins": (
            "tests/test_file_orchestration_journal.py::test_file_journal_manual_repair_suffix_only_target_emits_next_durable_attempt",
        ),
    },
)

INDEX_PINS = [pytest.param(pin, id=f"{entry['row']}::{pin.split('::')[1]}") for entry in INDEX for pin in entry["pins"]]


@cache
def _top_level_test_names(relative_path: str) -> frozenset[str]:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"), filename=relative_path)
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_")
    )


def test_index_rows_are_complete() -> None:
    assert [entry["row"] for entry in INDEX] == [
        "A-limit",
        "B",
        "C-sibling",
        "G",
        "H",
        "I",
        "J",
        "K",
        "O",
        "P",
        "journal-manual",
    ]
    for entry in INDEX:
        assert entry["input"] and entry["expected"] and entry["pins"]


@pytest.mark.parametrize("node_id", INDEX_PINS)
def test_index_pin_exists(node_id: str) -> None:
    relative_path, name = node_id.split("::")
    assert name in _top_level_test_names(relative_path), f"{node_id} is not a top-level test function"
