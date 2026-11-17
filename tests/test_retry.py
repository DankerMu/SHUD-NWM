from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator import retry as retry_module
from services.orchestrator import scheduler_state_types
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from services.orchestrator.retry import (
    MANUAL_RETRY_DURABLE_SUCCESS_STATUSES,
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    RetryConfig,
    RetryError,
    RetryService,
    _local_runtime_root_safety,
    _resolve_runtime_root_candidate,
    _retry_submission_manifest,
    _RetrySubmissionJob,
    auto_retry_skipped_details,
    classify_failure,
    compute_backoff_seconds,
    failure_classifier,
    is_retryable_failure,
    is_transient_error,
)
from services.orchestrator.scheduler_state_types import DOWNSTREAM_RESTART_STAGES, TRANSIENT_RETRY_REASON_CODES

_JOB_RETRY_SPEC_PATH = Path(__file__).resolve().parents[1] / "openspec" / "specs" / "job-retry-mechanism" / "spec.md"
_SERVICES_ROOT = Path(__file__).resolve().parents[1] / "services"
_NON_TRANSIENT_SCENARIO_HEADER = "#### Scenario: Non-transient error codes block auto-retry"
_SPEC_BULLET_CODE_PATTERN = re.compile(r"^\s+-\s+`([^`]+)`")
# A code on neither classification list, so the guard must default it non-transient.
_UNKNOWN_ERROR_CODE = "MYSTERY_SUBSYSTEM_FAILURE"
_RETRY_MODULE_LOGGER = "services.orchestrator.retry"
# Codes the orchestrator classifies non-transient that the spec's scenario list does
# not enumerate.  They are named here (rather than parsed) precisely because no
# independent source carries them: without this literal, dropping one from
# NON_TRANSIENT_ERROR_CODES would silently shrink every set-parameterized test's
# case list instead of failing it.
_CODE_ONLY_NON_TRANSIENT_ERROR_CODES = frozenset(
    {"MALFORMED_INPUT", "POLICY_BLOCKED", "WARM_START_CHECKPOINT_RETRY"}
)
# Production catch-all codes that sit on NEITHER classification list, so the guard
# defaults them non-transient and warns.  See the pinning test below.  `SHUD_FAILED`
# left this tuple when openspec change retry-stage-failure-classification (#1462) moved
# the SHUD runtime family onto the classified list, so the tuple now guards the
# `SLURM_JOB_FAILED` ruling alone.
_UNLISTED_PRODUCTION_ERROR_CODES = ("SLURM_JOB_FAILED",)


def _unknown_error_code_warning(error_code: str) -> str:
    return f"unknown error_code '{error_code}' defaulted to non-transient — add to classification list"


def _auto_retry_skipped_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _RETRY_MODULE_LOGGER and record.levelno == logging.WARNING
    ]


def _spec_non_transient_error_codes() -> list[str]:
    lines = _JOB_RETRY_SPEC_PATH.read_text(encoding="utf-8").splitlines()
    header_positions = [index for index, line in enumerate(lines) if line.strip() == _NON_TRANSIENT_SCENARIO_HEADER]
    assert len(header_positions) == 1, (
        f"expected exactly one {_NON_TRANSIENT_SCENARIO_HEADER!r} header in {_JOB_RETRY_SPEC_PATH}, "
        f"found {len(header_positions)}"
    )
    codes: list[str] = []
    for line in lines[header_positions[0] + 1 :]:
        stripped = line.lstrip()
        # Terminate on the THEN bullets (normal shape) AND on the next heading, so a
        # reformat of the THEN bullets (e.g. into an ordered list) cannot silently
        # extend the window into the following scenario's transient code list.
        if stripped.startswith("- **THEN**") or stripped.startswith("#"):
            break
        match = _SPEC_BULLET_CODE_PATTERN.match(line)
        if match is not None:
            codes.append(match.group(1))
    assert codes, f"no error-code bullets parsed under {_NON_TRANSIENT_SCENARIO_HEADER!r} in {_JOB_RETRY_SPEC_PATH}"
    return codes


def test_spec_and_code_classify_out_of_memory_as_non_transient() -> None:
    spec_codes = _spec_non_transient_error_codes()

    assert "OUT_OF_MEMORY" in spec_codes
    assert "OUT_OF_MEMORY" in NON_TRANSIENT_ERROR_CODES
    assert "OUT_OF_MEMORY" not in TRANSIENT_ERROR_CODES
    assert "OUT_OF_MEMORY" not in TRANSIENT_RETRY_REASON_CODES


def test_non_transient_classification_set_is_a_documented_superset_of_the_spec_list() -> None:
    """The spec list is a SUBSET of the code set on purpose, and the gap is enumerated.

    Equality would be wrong: `job-retry-mechanism` names the nineteen codes whose
    non-transient handling it governs (six original + the thirteen classified by
    openspec change retry-stage-failure-classification, #1462), while the orchestrator
    additionally blocks three codes of its own — that code-only remainder is unchanged
    by #1462.  Pinning `spec ⊆ code` plus the exact extras keeps both
    drift directions loud — a spec code dropped from the set, and a set member
    quietly removed (which would otherwise just shrink the parameterized cases).
    """

    spec_codes = set(_spec_non_transient_error_codes())

    assert spec_codes <= NON_TRANSIENT_ERROR_CODES
    assert NON_TRANSIENT_ERROR_CODES - spec_codes == set(_CODE_ONLY_NON_TRANSIENT_ERROR_CODES)
    assert NON_TRANSIENT_ERROR_CODES & TRANSIENT_ERROR_CODES == set()


def test_stage_failure_codes_track_the_canonical_downstream_stage_domain() -> None:
    """A stage added to the canonical domain must not mint an unclassified code.

    The claim is deliberately narrow (openspec change
    retry-stage-failure-classification): a runtime assertion cannot tell a derived
    comprehension from a hand-copied literal, so this pins set INCLUSION of the whole
    minted family plus the import edge from `retry` back to the domain constant.  The
    substance rides on the inclusion assertion — appending a stage to
    `DOWNSTREAM_RESTART_STAGES` reds this test unless its `{STAGE}_FAILED` code is
    classified too.
    """

    stage_family = {f"{stage.upper()}_FAILED" for stage in DOWNSTREAM_RESTART_STAGES}

    assert stage_family <= NON_TRANSIENT_ERROR_CODES
    assert retry_module.DOWNSTREAM_RESTART_STAGES is scheduler_state_types.DOWNSTREAM_RESTART_STAGES


def test_transient_classification_surfaces_carry_the_same_codes() -> None:
    """Transient membership lives on two surfaces and they must not diverge.

    `retry.TRANSIENT_ERROR_CODES` drives auto-retry and downstream resume;
    `scheduler_state_types.TRANSIENT_RETRY_REASON_CODES` drives the exhausted-budget
    reason and the recompute channel.  A code on only one is a half-transient code,
    so the sets are pinned equal — a future divergence has to be a deliberate edit.
    """

    assert TRANSIENT_ERROR_CODES == TRANSIENT_RETRY_REASON_CODES
    assert TRANSIENT_ERROR_CODES & NON_TRANSIENT_ERROR_CODES == set()


def test_durable_success_sets_stay_split_by_exactly_complete() -> None:
    """Two durable-success sets, two questions, one deliberate membership gap.

    `retry.MANUAL_RETRY_DURABLE_SUCCESS_STATUSES` refuses a manual retry;
    `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` rules the pipeline durably
    done and additionally holds `"complete"`.  They carried the same name until
    openspec change durable-status-name-split.  The judging power sits in the first two
    assertions, which pin each side against drift.  The scheduler side is pinned
    separately because collapsing it to three members changes what the FILE-JOURNAL lane
    decides: `hydro.run_status` is a closed enum, so `"complete"` is dead on the database
    lane, but the journal never validates `hydro_run.status` and its construction face
    writes `hydro_status="complete"` (#1581 design D5).  Which of those two lanes a given
    caller is on is not something this test knows, so it pins membership rather than
    claiming an absolute about "scheduler behavior".  The third assertion is logically
    implied by the first two and is kept as executable documentation of the relationship:
    the gap is exactly `"complete"`.  The last one guards the other escape hatch, a
    re-added alias under the old name on `retry`; a plain rename-back needs no guard, the
    module-level from-import fails on its own.  The parity of every hydro status set
    across its consumer modules lives in tests/test_hydro_status_set_parity.py.
    """

    assert MANUAL_RETRY_DURABLE_SUCCESS_STATUSES == {"succeeded", "parsed", "published"}
    assert scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES == {"succeeded", "parsed", "published", "complete"}
    assert MANUAL_RETRY_DURABLE_SUCCESS_STATUSES == scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"}
    assert not hasattr(retry_module, "DURABLE_HYDRO_SUCCESS_STATUSES")


def test_slurm_deadline_is_transient_on_every_classification_surface() -> None:
    assert "SLURM_DEADLINE" in TRANSIENT_ERROR_CODES
    assert "SLURM_DEADLINE" in TRANSIENT_RETRY_REASON_CODES
    assert is_transient_error("SLURM_DEADLINE") is True
    assert is_retryable_failure("SLURM_DEADLINE") is True
    assert failure_classifier("SLURM_DEADLINE") == "transient_slurm_runtime"

    failure = classify_failure("SLURM_DEADLINE", attempt=1, retry_limit=3)

    assert failure["permanent"] is False
    assert failure["retryable"] is True


def test_slurm_job_failed_stays_deliberately_unregistered() -> None:
    """The gateway catch-all is a true unknown, not a classification gap.

    Registering it anywhere would either spin auto-retry on deterministic
    application failures or replace the `unknown_error_code_defaulted_non_transient`
    audit reason with `non_transient_error`, losing the "needs operator adjudication"
    signal.  See openspec change slurm-error-code-transient-coverage.
    """

    assert "SLURM_JOB_FAILED" not in TRANSIENT_ERROR_CODES
    assert "SLURM_JOB_FAILED" not in NON_TRANSIENT_ERROR_CODES
    assert "SLURM_JOB_FAILED" not in TRANSIENT_RETRY_REASON_CODES
    assert failure_classifier("SLURM_JOB_FAILED") == "unknown_failure"
    assert classify_failure("SLURM_JOB_FAILED", attempt=0, retry_limit=3)["permanent"] is True
    assert auto_retry_skipped_details("SLURM_JOB_FAILED") == {
        "auto_retry_skipped": True,
        "reason": "unknown_error_code_defaulted_non_transient",
        "error_code": "SLURM_JOB_FAILED",
    }


def test_transient_error_classification() -> None:
    for error_code in TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is True
    for error_code in NON_TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is False
    assert is_transient_error("UNKNOWN_ERROR") is False
    assert is_transient_error(None) is False


def test_backoff_calculation() -> None:
    assert compute_backoff_seconds(0) == 60
    assert compute_backoff_seconds(1) == 300
    assert compute_backoff_seconds(2) == 900
    assert compute_backoff_seconds(3) == 900
    assert compute_backoff_seconds(0, [5, 10]) == 5
    assert compute_backoff_seconds(4, [5, 10]) == 10


def test_should_auto_retry_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


def test_should_auto_retry_poll_timeout() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_JOB_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


@pytest.mark.parametrize("error_code", ["INVALID_MANIFEST", "MALFORMED_INPUT", "POLICY_BLOCKED"])
def test_should_auto_retry_non_transient(error_code: str) -> None:
    with _store() as store:
        job = _create_job(store, error_code=error_code)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_should_auto_retry_max_reached() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_out_of_memory_blocks_auto_retry_within_retry_limit() -> None:
    with _store() as store:
        job = _create_job(store, error_code="OUT_OF_MEMORY", retry_count=2)
        service = RetryService(store, RetryConfig(max_retries=3))

        policy = service.retry_policy_for_job(job)
        updated = service.handle_failed_job(job)

        assert policy["classifier"] == "resource_configuration"
        assert policy["retryable"] is False
        assert policy["permanent"] is True
        assert policy["auto_retry"] is False
        assert updated.job_id == job.job_id
        assert updated.status == "permanently_failed"
        assert updated.retry_count == 2
        assert [event.event_type for event in _events(store)] == ["permanently_failed"]
        event = _events(store)[0]
        assert event.details["last_error"] == "OUT_OF_MEMORY"
        assert event.details["failure"]["classifier"] == "resource_configuration"
        assert event.details["failure"]["retryable"] is False
        assert event.details["failure"]["permanent"] is True


def test_handle_failed_job_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.handle_failed_job(job)

        store.session.refresh(job)
        assert retry.job_id == "job_1_retry_1"
        assert retry.status == "pending"
        assert retry.retry_count == 1
        assert job.status == "failed"
        assert job.retry_count == 0


def test_handle_failed_job_non_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="INVALID_MANIFEST")
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_handle_failed_job_exhausted() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_out_of_memory_becomes_permanent_on_the_first_attempt() -> None:
    with _store() as store:
        job = _create_job(store, error_code="OUT_OF_MEMORY", retry_count=0)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        assert updated.retry_count == 0
        assert _jobs(store) == ["job_1"]
        event = _events(store)[0]
        assert event.event_type == "permanently_failed"
        assert event.details["final_retry_count"] == 0
        assert event.details["automatic_retry_stopped"] is True
        assert event.details["failure"]["classifier"] == "resource_configuration"
        assert event.details["failure"]["attempt"] == 0
        assert event.details["failure"]["retryable"] is False
        assert event.details["failure"]["permanent"] is True
        assert event.details["failure"]["limit_exhausted"] is False


def test_auto_retry_skipped_details_flags_non_transient_codes() -> None:
    for error_code in _spec_non_transient_error_codes():
        assert auto_retry_skipped_details(error_code) == {
            "auto_retry_skipped": True,
            "reason": "non_transient_error",
            "error_code": error_code,
        }


@pytest.mark.parametrize("error_code", sorted(NON_TRANSIENT_ERROR_CODES))
def test_auto_retry_skipped_details_covers_every_non_transient_set_member(
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full-set coverage, including the three members the spec list does not name.

    The set-closure test above is what makes a removed member fail rather than
    silently drop its case from this parameterization.
    """

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        details = auto_retry_skipped_details(error_code)

    assert details == {
        "auto_retry_skipped": True,
        "reason": "non_transient_error",
        "error_code": error_code,
    }
    assert _auto_retry_skipped_warnings(caplog) == []


def test_auto_retry_skipped_details_defaults_unlisted_codes_to_non_transient() -> None:
    assert _UNKNOWN_ERROR_CODE not in (TRANSIENT_ERROR_CODES | NON_TRANSIENT_ERROR_CODES)
    assert auto_retry_skipped_details(_UNKNOWN_ERROR_CODE) == {
        "auto_retry_skipped": True,
        "reason": "unknown_error_code_defaulted_non_transient",
        "error_code": _UNKNOWN_ERROR_CODE,
    }


@pytest.mark.parametrize("error_code", sorted(TRANSIENT_ERROR_CODES) + [None, ""])
def test_auto_retry_skipped_details_is_none_without_a_classification_block(error_code: str | None) -> None:
    assert auto_retry_skipped_details(error_code) is None


def test_auto_retry_skipped_reason_literals_have_a_single_source() -> None:
    literals = (
        "non_transient_error",
        "unknown_error_code_defaulted_non_transient",
        "defaulted to non-transient",
    )
    counts = dict.fromkeys(literals, 0)
    for path in sorted(_SERVICES_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            counts[literal] += text.count(literal)
    assert counts == dict.fromkeys(literals, 1)


@pytest.mark.parametrize("error_code", _spec_non_transient_error_codes())
def test_permanently_failed_event_carries_auto_retry_skipped_for_non_transient_codes(
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _store() as store:
        job = _create_job(store, error_code=error_code)
        service = RetryService(store, RetryConfig(max_retries=3))

        with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
            updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert details["auto_retry_skipped"] is True
        assert details["reason"] == "non_transient_error"
        assert details["error_code"] == error_code
        assert details["failure"]["retryable"] is False
        assert _auto_retry_skipped_warnings(caplog) == []


def test_permanently_failed_event_flags_an_unknown_error_code_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _store() as store:
        job = _create_job(store, error_code=_UNKNOWN_ERROR_CODE)
        service = RetryService(store, RetryConfig(max_retries=3))

        with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
            updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert details["auto_retry_skipped"] is True
        assert details["reason"] == "unknown_error_code_defaulted_non_transient"
        assert details["error_code"] == _UNKNOWN_ERROR_CODE
        assert details["failure"]["retryable"] is False
        assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)]


@pytest.mark.parametrize("error_code", _UNLISTED_PRODUCTION_ERROR_CODES)
def test_unlisted_production_error_codes_default_to_the_unknown_reason_and_warn(
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Knowing acceptance: a real production code rides the unknown-default branch.

    `SLURM_JOB_FAILED` is the gateway's catch-all for terminal Slurm states, pinned
    onto neither classification list by tests/test_real_slurm_gateway.py, so it
    produces reason `unknown_error_code_defaulted_non_transient` and the "add to
    classification list" warning on every permanent failure — which is exactly what
    spec.md's unknown-code scenario prescribes, so #1314 accepts it rather than
    silencing it.  `SHUD_FAILED` shared this tuple until openspec change
    retry-stage-failure-classification (#1462) classified the SHUD runtime family and
    the canonical stage-failure family, so the tuple — and this test — now guard the
    `SLURM_JOB_FAILED` ruling alone; that ruling stands and cannot be reversed by
    accident.
    """

    assert error_code not in (TRANSIENT_ERROR_CODES | NON_TRANSIENT_ERROR_CODES)
    with _store() as store:
        job = _create_job(store, error_code=error_code)
        service = RetryService(store, RetryConfig(max_retries=3))

        with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
            updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert details["auto_retry_skipped"] is True
        assert details["reason"] == "unknown_error_code_defaulted_non_transient"
        assert details["error_code"] == error_code
        assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(error_code)]


def test_permanently_failed_event_omits_auto_retry_skipped_when_the_budget_is_exhausted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
            updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert "auto_retry_skipped" not in details
        assert details["failure"]["limit_exhausted"] is True
        assert _auto_retry_skipped_warnings(caplog) == []


def test_permanently_failed_event_omits_auto_retry_skipped_without_a_recorded_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _store() as store:
        job = _create_job(store, error_code=None)
        service = RetryService(store, RetryConfig(max_retries=3))

        with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
            updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert "auto_retry_skipped" not in details
        assert details["failure"]["reason_code"] == "UNKNOWN_FAILURE"
        assert details["failure"]["limit_exhausted"] is False
        assert _auto_retry_skipped_warnings(caplog) == []


def test_non_transient_code_carries_auto_retry_skipped_even_at_the_retry_limit() -> None:
    with _store() as store:
        job = _create_job(store, error_code="OUT_OF_MEMORY", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        details = _events(store)[0].details
        assert details["auto_retry_skipped"] is True
        assert details["reason"] == "non_transient_error"
        assert details["failure"]["limit_exhausted"] is True
        assert details["failure"]["retryable"] is False


def test_schedule_auto_retry() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        store.session.refresh(job)
        assert retry.job_id != job.job_id
        assert retry.job_id == "job_1_retry_1"
        assert retry.run_id == job.run_id
        assert retry.cycle_id == job.cycle_id
        assert retry.job_type == job.job_type
        assert retry.model_id == job.model_id
        assert retry.stage == job.stage
        assert retry.retry_count == 1
        assert retry.status == "pending"
        assert retry.slurm_job_id is None
        assert job.status == "failed"
        assert job.retry_count == 0
        assert job.slurm_job_id == "123"
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.status_from == "failed"
        assert event.status_to == "pending"


def test_schedule_auto_retry_reuses_submission_failed_retry_without_slurm_binding() -> None:
    with _store() as store:
        job = _create_job(store, error_code="NODE_FAILURE", retry_count=1)
        stale_retry = _create_job(
            store,
            job_id="job_1_retry_2",
            run_id=job.run_id,
            status="submission_failed",
            error_code="SUBMIT_INTERRUPTED",
            retry_count=2,
        )
        stale_retry.slurm_job_id = None
        stale_retry.array_task_id = None
        stale_retry.started_at = datetime(2026, 5, 1, 1, tzinfo=UTC)
        stale_retry.finished_at = datetime(2026, 5, 1, 2, tzinfo=UTC)
        stale_retry.exit_code = 1
        stale_retry.error_message = "submission interrupted"
        stale_retry.log_uri = "s3://logs/stale"
        store.session.add(stale_retry)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        assert retry.job_id == "job_1_retry_2"
        assert retry.status == "pending"
        assert retry.retry_count == 2
        assert retry.slurm_job_id is None
        assert retry.array_task_id is None
        assert retry.started_at is None
        assert retry.finished_at is None
        assert retry.exit_code is None
        assert retry.error_code is None
        assert retry.error_message is None
        assert retry.log_uri is None
        assert [candidate.job_id for candidate in store.query_jobs_by_run(job.run_id)] == [
            "job_1",
            "job_1_retry_2",
        ]
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.details["reused_existing_retry_job"] is True
        assert event.details["previous_job_id"] == "job_1"


def test_schedule_auto_retry_does_not_reuse_retry_with_slurm_binding() -> None:
    with _store() as store:
        job = _create_job(store, error_code="NODE_FAILURE", retry_count=1)
        existing_retry = _create_job(
            store,
            job_id="job_1_retry_2",
            run_id=job.run_id,
            status="submission_failed",
            error_code="SUBMIT_INTERRUPTED",
            retry_count=2,
        )
        existing_retry.slurm_job_id = "slurm_existing_retry"
        store.session.add(existing_retry)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryError) as exc_info:
            service.schedule_auto_retry(job)

        assert exc_info.value.code == "AUTO_RETRY_JOB_CONFLICT"
        assert exc_info.value.details["retry_job_id"] == "job_1_retry_2"
        assert exc_info.value.details["existing_slurm_job_id"] == "slurm_existing_retry"


def test_schedule_auto_retry_on_a_retry_row_stacks_the_suffix() -> None:
    """#2254 must-preserve: the DB producer appends ``_retry_<n>`` to the failed row's id.

    Retrying ``job_1_retry_1`` therefore yields the stacked ``job_1_retry_1_retry_2``;
    the chain's attempt numbering reads the last suffix of this shape.
    """

    with _store() as store:
        job = _create_job(store, job_id="job_1_retry_1", error_code="NODE_FAILURE", retry_count=1)
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        assert retry.job_id == "job_1_retry_1_retry_2"
        assert retry.retry_count == 2
        assert retry.status == "pending"


def test_mark_permanently_failed() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.mark_permanently_failed(job)

        assert updated.status == "permanently_failed"
        event = _events(store)[0]
        assert event.event_type == "permanently_failed"
        assert event.status_from == "failed"
        assert event.status_to == "permanently_failed"
        assert event.details["final_retry_count"] == 3
        assert event.details["last_error"] == "SLURM_TIMEOUT"
        assert event.details["automatic_retry_stopped"] is True
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"
        assert event.details["failure"]["limit_exhausted"] is True


def test_automatic_forecast_retry_preserves_db_free_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with _store() as store:
        failed = _create_job(
            store,
            job_id="job_fcst_gfs_2026062912_model_a_forecast",
            run_id="fcst_gfs_2026062912_model_a",
            error_code="NODE_FAILURE",
            retry_count=0,
            cycle_id="gfs_2026062912",
            job_type="run_shud_forecast_array",
            stage="forecast",
        )
        _insert_submission_event(store, failed, _db_free_retry_contract())
        service = RetryService(store, RetryConfig(max_retries=3))
        retry_job = service.schedule_auto_retry(failed)
        gateway = _RecordingGateway(job_id="slurm_auto_retry")

        submitted = service._submit_retry_job(
            _RetrySubmissionJob(
                job_id=retry_job.job_id,
                run_id=retry_job.run_id,
                cycle_id=retry_job.cycle_id,
                job_type=retry_job.job_type,
                model_id=retry_job.model_id,
                stage=retry_job.stage,
                retry_count=retry_job.retry_count,
                previous_job_id=failed.job_id,
                manual_retry_marker=False,
            ),
            gateway,
        )

        manifest = gateway.submissions[0].manifest
        assert submitted.payload["job_id"] == "slurm_auto_retry"
        assert manifest["scheduler_db_free_required"] == "true"
        assert manifest["scheduler_registry_backend"] == "file"
        assert manifest["scheduler_registry_manifest"] == "/srv/nhms/object-store/scheduler/registry/manifest-last.json"
        assert manifest["scheduler_canonical_readiness_backend"] == "file"
        assert manifest["scheduler_canonical_readiness_index"] == (
            "/srv/nhms/object-store/scheduler/canonical-readiness/index-last.json"
        )
        assert manifest["scheduler_state_index_backend"] == "file"
        assert manifest["scheduler_state_index"] == "/srv/nhms/object-store/scheduler/state-index/index-last.json"
        assert manifest["slurm_env"] == {"NHMS_SHUD_DB_FREE": "true"}
        assert manifest["previous_job_id"] == failed.job_id
        assert manifest["pipeline_job_id"] == retry_job.job_id
        assert manifest["manual_retry_marker"] is False
        assert manifest["retry_count"] == 1
        assert manifest["source_id"] == "gfs"
        assert manifest["cycle_time"] == "2026062912"
        assert "DATABASE_URL" not in json.dumps(manifest)
        evidence = submitted.runtime_root_resolution
        assert evidence is not None
        assert evidence["db_free_runtime"]["required"] is True
        assert evidence["db_free_runtime"]["missing"] == []


def test_automatic_forcing_retry_preserves_db_free_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with _store() as store:
        failed = _create_job(
            store,
            job_id="job_cycle_gfs_2026062912_forcing_model_a_forcing",
            run_id="cycle_gfs_2026062912_forcing_model_a",
            error_code="NODE_FAILURE",
            retry_count=0,
            cycle_id="gfs_2026062912",
            job_type="produce_forcing_array",
            stage="forcing",
        )
        _insert_submission_event(store, failed, _db_free_retry_contract())
        service = RetryService(store, RetryConfig(max_retries=3))
        retry_job = service.schedule_auto_retry(failed)
        gateway = _RecordingGateway(job_id="slurm_auto_forcing_retry")

        submitted = service._submit_retry_job(
            _RetrySubmissionJob(
                job_id=retry_job.job_id,
                run_id=retry_job.run_id,
                cycle_id=retry_job.cycle_id,
                job_type=retry_job.job_type,
                model_id=retry_job.model_id,
                stage=retry_job.stage,
                retry_count=retry_job.retry_count,
                previous_job_id=failed.job_id,
                manual_retry_marker=False,
            ),
            gateway,
        )

        manifest = gateway.submissions[0].manifest
        assert submitted.payload["job_id"] == "slurm_auto_forcing_retry"
        assert manifest["job_type"] == "produce_forcing_array"
        assert manifest["scheduler_db_free_required"] == "true"
        assert manifest["scheduler_registry_backend"] == "file"
        assert manifest["scheduler_registry_manifest"] == "/srv/nhms/object-store/scheduler/registry/manifest-last.json"
        assert manifest["scheduler_canonical_readiness_backend"] == "file"
        assert manifest["scheduler_canonical_readiness_index"] == (
            "/srv/nhms/object-store/scheduler/canonical-readiness/index-last.json"
        )
        assert manifest["scheduler_state_index_backend"] == "file"
        assert manifest["scheduler_state_index"] == "/srv/nhms/object-store/scheduler/state-index/index-last.json"
        assert manifest["slurm_env"] == {"NHMS_SHUD_DB_FREE": "true"}
        assert manifest["previous_job_id"] == failed.job_id
        assert manifest["pipeline_job_id"] == retry_job.job_id
        assert manifest["manual_retry_marker"] is False
        assert manifest["retry_count"] == 1
        assert "DATABASE_URL" not in json.dumps(manifest)
        evidence = submitted.runtime_root_resolution
        assert evidence is not None
        assert evidence["db_free_runtime"]["required"] is True
        assert evidence["db_free_runtime"]["missing"] == []


def test_retry_source_cycle_identity_keeps_uppercase_canonical_sources() -> None:
    manifest = _RetrySubmissionJob(
        job_id="job_cycle_era5_2026053106_download_retry_1",
        run_id="cycle_era5_2026053106",
        cycle_id="era5_2026053106",
        job_type="download_source_cycle",
        model_id=None,
        stage="download",
        retry_count=1,
        previous_job_id="job_cycle_era5_2026053106_download",
    )

    assert _retry_submission_manifest(manifest, model_id="model_a")["source_id"] == "ERA5"


def test_expire_stale_retries_fails_stale_pending_retry_rows() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed", error_code="NODE_FAILURE")
        pending = _create_job(
            store,
            job_id="job_pending",
            run_id="run_1",
            status="pending",
            error_code=None,
            retry_count=1,
        )
        pending.slurm_job_id = None
        pending.created_at = datetime(2026, 5, 1, tzinfo=UTC)
        store.session.add(pending)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        expired = service.expire_stale_retries(max_age_seconds=1)

        assert [job.job_id for job in expired] == ["job_pending"]
        assert expired[0].status == "failed"
        assert expired[0].error_code == "RETRY_STALE_PENDING"
        assert _events(store)[0].event_type == "retry_expired"


def test_expire_stale_retries_ignores_non_retry_pending_jobs() -> None:
    with _store() as store:
        pending = _create_job(store, job_id="job_pending", run_id="run_1", status="pending", error_code=None)
        pending.slurm_job_id = None
        pending.created_at = datetime(2026, 5, 1, tzinfo=UTC)
        store.session.add(pending)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        expired = service.expire_stale_retries(max_age_seconds=1)

        assert expired == []
        store.session.refresh(pending)
        assert pending.status == "pending"


def test_audit_event_auto() -> None:
    with _store() as store:
        job = _create_job(store, error_code="STORAGE_WRITE_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.schedule_auto_retry(job)

        event = _events(store)[0]
        assert event.details["trigger"] == "auto"
        assert event.details["retry_count"] == 1
        assert event.details["previous_error"] == "STORAGE_WRITE_FAILED"
        assert event.details["backoff_seconds"] == 60
        assert event.details["previous_job_id"] == job.job_id
        assert event.details["slurm_job_id"] is None
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"


def test_permanently_failed_override() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")

        updated = store.update_job_status("job_failed", "permanently_failed")

        assert updated.status == "permanently_failed"


def test_permanently_failed_is_sticky() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="permanently_failed")

        partial = store.update_job_status("job_failed", "partially_failed")
        running = store.update_job_status("job_failed", "running")

        assert partial.status == "permanently_failed"
        assert running.status == "permanently_failed"
        assert store.get_job("job_failed").status == "permanently_failed"


# --- runtime-root safety: symlink loops and other normalization faults (#1401) ---
#
# Exit-table coverage for _local_runtime_root_safety. The verdicts below are the
# same on every supported CPython version: before this change the non-strict
# Path.resolve() arm admitted a symlink loop verbatim on 3.13+ and raised an
# errno-less RuntimeError on <=3.12, so the same input produced two different
# outcomes and neither of them was the fail-closed rejection.

_UNRESOLVABLE_LOCAL_ROOT = (None, "unresolvable_local_root")


def _make_symlink_loop(directory: Path, name: str) -> Path:
    """Create a two-hop symlink loop under ``directory`` and return its entry path."""
    first = directory / f"{name}_a"
    second = directory / f"{name}_b"
    first.symlink_to(second)
    second.symlink_to(first)
    return first


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("relative/object-store", (None, "relative_local_root")),
        ("../object-store", (None, "parent_traversal_local_root")),
        ("relative/../object-store", (None, "parent_traversal_local_root")),
    ],
)
def test_local_runtime_root_safety_keeps_lexical_rejection_reasons(
    value: str,
    expected: tuple[str | None, str],
) -> None:
    assert _local_runtime_root_safety(value) == expected


def test_local_runtime_root_safety_admits_existing_absolute_root_byte_compatibly(tmp_path: Path) -> None:
    root = tmp_path / "object-store"
    root.mkdir()
    # Pre-change oracle: str(Path(value).resolve(strict=False)).
    expected = str(Path(str(root)).resolve(strict=False))

    assert _local_runtime_root_safety(str(root)) == (expected, "ok")


def test_local_runtime_root_safety_admits_not_yet_created_root_byte_compatibly(tmp_path: Path) -> None:
    root = tmp_path / "not-created" / "object-store"
    expected = str(Path(str(root)).resolve(strict=False))

    assert not root.exists()
    assert _local_runtime_root_safety(str(root)) == (expected, "ok")


def test_local_runtime_root_safety_admits_missing_component_pointing_at_existing_root(tmp_path: Path) -> None:
    real_root = tmp_path / "object-store"
    real_root.mkdir()
    value = f"{tmp_path / 'never-created'}/../object-store"
    expected = str(Path(value).resolve(strict=False))

    assert _local_runtime_root_safety(value) == (expected, "ok")
    assert expected == str(real_root.resolve())


def test_local_runtime_root_safety_rejects_symlink_loop_root(tmp_path: Path) -> None:
    loop_root = _make_symlink_loop(tmp_path, "loop")

    assert _local_runtime_root_safety(str(loop_root)) == _UNRESOLVABLE_LOCAL_ROOT


def test_local_runtime_root_safety_rejects_missing_component_pointing_at_symlink_loop(tmp_path: Path) -> None:
    """The loop-filtering re-check is the only discriminator for this phantom form.

    Strict resolution stops at the missing component with ENOENT, so without the
    re-check the non-strict fallback (which still carries the loop) would be
    admitted into the manifest -- a fail-open regression on <=3.12, where the
    pre-change helper raised instead.
    """
    _make_symlink_loop(tmp_path, "loop")
    value = f"{tmp_path / 'never-created'}/../loop_a"

    assert _local_runtime_root_safety(value) == _UNRESOLVABLE_LOCAL_ROOT


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses unreadable directories")
def test_local_runtime_root_safety_rejects_permission_fault_root(tmp_path: Path) -> None:
    guard = tmp_path / "guard"
    guard.mkdir()
    unreachable = guard / "object-store"
    unreachable.mkdir()
    guard.chmod(0o000)
    try:
        assert _local_runtime_root_safety(str(unreachable)) == _UNRESOLVABLE_LOCAL_ROOT
    finally:
        guard.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses unreadable directories")
def test_local_runtime_root_safety_rejects_missing_component_pointing_at_permission_fault(tmp_path: Path) -> None:
    guard = tmp_path / "guard"
    guard.mkdir()
    unreachable = guard / "object-store"
    unreachable.mkdir()
    value = f"{tmp_path / 'never-created'}/../guard/object-store"
    guard.chmod(0o000)
    try:
        assert _local_runtime_root_safety(value) == _UNRESOLVABLE_LOCAL_ROOT
    finally:
        guard.chmod(0o755)


def test_local_runtime_root_safety_rejects_unknown_user_tilde_without_raising() -> None:
    # os.path.expanduser leaves an unresolvable "~user" prefix verbatim, so the
    # value falls closed through the non-absolute arm; Path.expanduser would
    # raise RuntimeError straight out of the helper instead.
    assert _local_runtime_root_safety("~nhms_no_such_user_1401/object-store") == (None, "relative_local_root")


@pytest.mark.parametrize("value", ["/srv/nhms/object\x00store", "~\x00nhms/object-store"])
def test_local_runtime_root_safety_rejects_embedded_null_byte_without_raising(value: str) -> None:
    assert _local_runtime_root_safety(value) == _UNRESOLVABLE_LOCAL_ROOT


@pytest.mark.parametrize(
    "root_field",
    ["workspace_dir", "object_store_root", "published_artifact_root"],
)
def test_resolve_runtime_root_candidate_rejects_symlink_loop_in_every_local_field(
    root_field: str,
    tmp_path: Path,
) -> None:
    loop_root = _make_symlink_loop(tmp_path, "loop")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir()
    candidate = {
        "workspace_dir": str(workspace_root),
        "object_store_root": str(object_store_root),
        "object_store_prefix": "s3://nhms-prod",
        "published_artifact_root": str(tmp_path / "published"),
        root_field: str(loop_root),
    }

    resolution = _resolve_runtime_root_candidate("runtime_config:environment", candidate)

    assert resolution.unsafe_rejected is True
    assert root_field not in resolution.resolved
    assert [
        (item["field"], item["reason"]) for item in resolution.rejected
    ] == [(root_field, "unresolvable_local_root")]


def test_resolve_runtime_root_candidate_keeps_loop_roots_out_of_manifest_and_overlap_baseline(
    tmp_path: Path,
) -> None:
    """Two loop aliases used to be admitted as mutually unequal comparable roots.

    On 3.13+ the pre-change helper returned each loop path verbatim, so both
    roots entered the resolved set (and the submitted manifest) while the
    workspace/object-store overlap guard compared two unequal strings and stayed
    silent. Both roots must now be rejected outright instead.
    """
    _make_symlink_loop(tmp_path, "loop")
    candidate = {
        "workspace_dir": f"{tmp_path}/loop_a",
        "object_store_root": f"{tmp_path}/loop_b",
        "object_store_prefix": "s3://nhms-prod",
    }

    resolution = _resolve_runtime_root_candidate("runtime_config:environment", candidate)

    assert resolution.unsafe_rejected is True
    assert "workspace_dir" not in resolution.resolved
    assert "object_store_root" not in resolution.resolved
    assert resolution.missing == ["workspace_dir", "object_store_root"]
    assert [(item["field"], item["reason"]) for item in resolution.rejected] == [
        ("workspace_dir", "unresolvable_local_root"),
        ("object_store_root", "unresolvable_local_root"),
    ]


def _store() -> "_ClosingStore":
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_ops_schema(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return _ClosingStore(Session(engine))


class _ClosingStore(PipelineStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.session.close()


class _RecordingGateway:
    def __init__(self, *, job_id: str = "slurm_retry", error: Exception | None = None) -> None:
        self.job_id = job_id
        self.error = error
        self.submissions = []

    def submit_job(self, request):
        self.submissions.append(request)
        if self.error is not None:
            raise self.error
        return {
            "job_id": self.job_id,
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }


def _create_job(
    store: PipelineStore,
    *,
    job_id: str = "job_1",
    run_id: str = "run_1",
    status: str = "failed",
    error_code: str | None = "SLURM_TIMEOUT",
    retry_count: int = 0,
    cycle_id: str = "gfs_2026050100",
    job_type: str = "run_shud_analysis",
    stage: str = "run",
) -> PipelineJob:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type=job_type,
        slurm_job_id="123",
        model_id="model_a",
        stage=stage,
        status=status,
    )
    job.error_code = error_code
    job.error_message = f"{error_code} failed" if error_code else None
    job.retry_count = retry_count
    store.session.add(job)
    store.session.commit()
    store.session.refresh(job)
    return job


def _insert_submission_event(store: PipelineStore, job: PipelineJob, runtime_root_contract: dict[str, Any]) -> None:
    store.insert_event(
        entity_type="pipeline_job",
        entity_id=job.job_id,
        event_type="submission",
        status_from=None,
        status_to=job.status,
        details={
            "stage": job.stage,
            "job_type": job.job_type,
            "runtime_root_contract": runtime_root_contract,
        },
    )


def _db_free_retry_contract() -> dict[str, Any]:
    return {
        "workspace_dir": "/srv/nhms/workspace",
        "object_store_root": "/srv/nhms/object-store",
        "object_store_prefix": "s3://nhms-prod",
        "scheduler_db_free_required": "true",
        "scheduler_allowed_roots": "/srv/nhms/workspace:/srv/nhms/object-store",
        "scheduler_registry_backend": "file",
        "scheduler_registry_manifest": "/srv/nhms/object-store/scheduler/registry/manifest-last.json",
        "scheduler_canonical_readiness_backend": "file",
        "scheduler_canonical_readiness_index": "/srv/nhms/object-store/scheduler/canonical-readiness/index-last.json",
        "scheduler_state_index_backend": "file",
        "scheduler_state_index": "/srv/nhms/object-store/scheduler/state-index/index-last.json",
    }


def _clear_runtime_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_SCHEDULER_DB_FREE_REQUIRED",
        "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "NHMS_SCHEDULER_REGISTRY_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX",
        "NHMS_SLURM_SCHEDULER_STATE_INDEX",
    ):
        monkeypatch.delenv(key, raising=False)


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))


def _jobs(store: PipelineStore) -> list[str]:
    statement = select(PipelineJob.job_id).order_by(PipelineJob.job_id.asc())
    return [str(job_id) for job_id in store.session.scalars(statement)]
