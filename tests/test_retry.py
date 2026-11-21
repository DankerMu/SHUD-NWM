from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator import retry as retry_module
from services.orchestrator import scheduler_state_types
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from services.orchestrator.retry import (
    MANUAL_RETRY_DURABLE_SUCCESS_STATUSES,
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    RetryConfig,
    RetryError,
    auto_retry_skipped_details,
    classify_failure,
    compute_backoff_seconds,
    failure_classifier,
    is_retryable_failure,
    is_transient_error,
)
from services.orchestrator.scheduler_state_types import DOWNSTREAM_RESTART_STAGES, TRANSIENT_RETRY_REASON_CODES
from tests.pipeline_job_orm_helpers import seed_pipeline_job

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
    job = seed_pipeline_job(
        store.session,
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


# --- File-lane retry service (#3261) ------------------------------------------------
#
# The SQL retry service and its tests were deleted by #3261 (R4). Each case below is
# the same geometry driven through the surviving production retry service,
# ``FileJournalRetryService`` over ``FileOrchestrationJournalRepository``, on a plain
# (non accepted-submit master) row. The SQL cases whose exact geometry an existing
# file-lane test already pins were deleted without a twin; the disposition table lives
# in the #3261 PR body.

_JOURNAL_CYCLE_TIME = datetime(2026, 6, 28, tzinfo=UTC)
_JOURNAL_CYCLE_ID = "gfs_2026062800"
_JOURNAL_RUN_ID = "fcst_gfs_2026062800_model_a"


def _journal_seed_job(
    repository: FileOrchestrationJournalRepository,
    *,
    job_id: str = "job_1",
    status: str = "failed",
    error_code: str | None = "SLURM_TIMEOUT",
    retry_count: int = 0,
    slurm_job_id: str | None = "123",
) -> dict[str, Any]:
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
    if slurm_job_id is not None:
        repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id=slurm_job_id)
    repository.update_pipeline_job_status(
        job_id,
        status,
        error_code=error_code,
        error_message=f"{error_code} failed" if error_code else None,
        finished_at=_JOURNAL_CYCLE_TIME,
    )
    job = repository.get_pipeline_job(job_id)
    assert job is not None
    return job


def _journal_failed_job(
    tmp_path: Path,
    **fields: Any,
) -> tuple[FileOrchestrationJournalRepository, dict[str, Any]]:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    return repository, _journal_seed_job(repository, **fields)


def _journal_service(repository: FileOrchestrationJournalRepository, **config: Any) -> FileJournalRetryService:
    return FileJournalRetryService(repository, RetryConfig(**{"max_retries": 3, **config}))


def _journal_events(repository: FileOrchestrationJournalRepository) -> list[dict[str, Any]]:
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_JOURNAL_CYCLE_TIME,
        model_id="model_a",
        run_id=_JOURNAL_RUN_ID,
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id=f"gfs:{_JOURNAL_CYCLE_TIME.isoformat()}:model_a:forecast_gfs_deterministic",
    )
    assert state is not None
    return [dict(event) for event in state["pipeline_events"]]


def _journal_events_of(repository: FileOrchestrationJournalRepository, event_type: str) -> list[dict[str, Any]]:
    return [event for event in _journal_events(repository) if event["event_type"] == event_type]


def _journal_job_ids(repository: FileOrchestrationJournalRepository) -> list[str]:
    return sorted(str(job["job_id"]) for job in repository.query_pipeline_jobs_by_cycle(_JOURNAL_CYCLE_ID))


@pytest.mark.parametrize(
    ("error_code", "retry_count", "expected"),
    [
        ("SLURM_TIMEOUT", 0, True),
        ("SLURM_JOB_TIMEOUT", 0, True),
        ("INVALID_MANIFEST", 0, False),
        ("MALFORMED_INPUT", 0, False),
        ("POLICY_BLOCKED", 0, False),
        ("SLURM_TIMEOUT", 3, False),
    ],
)
def test_file_lane_should_auto_retry_follows_the_retry_policy_and_writes_nothing(
    tmp_path: Path,
    error_code: str,
    retry_count: int,
    expected: bool,
) -> None:
    """Transient codes retry, non-transient codes and an exhausted budget do not."""

    repository, job = _journal_failed_job(tmp_path, error_code=error_code, retry_count=retry_count)
    events_before = _journal_events(repository)
    service = _journal_service(repository)

    assert service.should_auto_retry(job) is expected
    assert repository.get_pipeline_job("job_1")["status"] == "failed"
    assert _journal_events(repository) == events_before
    assert _journal_job_ids(repository) == ["job_1"]


def test_file_lane_out_of_memory_blocks_auto_retry_within_retry_limit(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="OUT_OF_MEMORY", retry_count=2)
    service = _journal_service(repository)

    policy = service.retry_policy_for_job(job)
    updated = service.handle_failed_job(job)

    assert policy["classifier"] == "resource_configuration"
    assert policy["retryable"] is False
    assert policy["permanent"] is True
    assert policy["auto_retry"] is False
    assert updated.job_id == "job_1"
    assert updated.status == "permanently_failed"
    assert updated.retry_count == 2
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert _journal_events_of(repository, "retry") == []
    assert event["details"]["last_error"] == "OUT_OF_MEMORY"
    assert event["details"]["failure"]["classifier"] == "resource_configuration"
    assert event["details"]["failure"]["retryable"] is False
    assert event["details"]["failure"]["permanent"] is True


def test_file_lane_handle_failed_job_transient_leaves_the_source_row(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="SLURM_TIMEOUT")
    service = _journal_service(repository)

    retry = service.handle_failed_job(job)

    source = repository.get_pipeline_job("job_1")
    assert retry.job_id == "job_1_retry_1"
    assert retry.status == "pending"
    assert retry.retry_count == 1
    assert source["status"] == "failed"
    assert source["retry_count"] == 0


def test_file_lane_out_of_memory_becomes_permanent_on_the_first_attempt(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="OUT_OF_MEMORY", retry_count=0)
    service = _journal_service(repository)

    updated = service.handle_failed_job(job)

    assert updated.status == "permanently_failed"
    assert updated.retry_count == 0
    assert _journal_job_ids(repository) == ["job_1"]
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert event["details"]["final_retry_count"] == 0
    assert event["details"]["automatic_retry_stopped"] is True
    assert event["details"]["failure"]["classifier"] == "resource_configuration"
    assert event["details"]["failure"]["attempt"] == 0
    assert event["details"]["failure"]["retryable"] is False
    assert event["details"]["failure"]["permanent"] is True
    assert event["details"]["failure"]["limit_exhausted"] is False


def test_file_lane_permanently_failed_event_flags_an_unknown_error_code_and_warns_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code=_UNKNOWN_ERROR_CODE)
    service = _journal_service(repository)

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        updated = service.handle_failed_job(job)

    assert updated.status == "permanently_failed"
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert event["details"]["auto_retry_skipped"] is True
    assert event["details"]["reason"] == "unknown_error_code_defaulted_non_transient"
    assert event["details"]["error_code"] == _UNKNOWN_ERROR_CODE
    assert event["details"]["failure"]["retryable"] is False
    assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)]


@pytest.mark.parametrize("error_code", _UNLISTED_PRODUCTION_ERROR_CODES)
def test_file_lane_unlisted_production_error_codes_default_to_the_unknown_reason_and_warn(
    tmp_path: Path,
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Knowing acceptance (#1314) on the surviving lane: `SLURM_JOB_FAILED` rides the unknown default.

    The gateway's catch-all is pinned onto neither classification list, so a permanent
    failure carrying it records reason `unknown_error_code_defaulted_non_transient` and
    emits the "add to classification list" warning once -- the spec's unknown-code
    scenario, which #1314 accepts rather than silences.
    """

    assert error_code not in (TRANSIENT_ERROR_CODES | NON_TRANSIENT_ERROR_CODES)
    repository, job = _journal_failed_job(tmp_path, error_code=error_code)
    service = _journal_service(repository)

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        updated = service.handle_failed_job(job)

    assert updated.status == "permanently_failed"
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert event["details"]["auto_retry_skipped"] is True
    assert event["details"]["reason"] == "unknown_error_code_defaulted_non_transient"
    assert event["details"]["error_code"] == error_code
    assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(error_code)]


def test_file_lane_non_transient_code_carries_auto_retry_skipped_even_at_the_retry_limit(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="OUT_OF_MEMORY", retry_count=3)
    service = _journal_service(repository)

    updated = service.handle_failed_job(job)

    assert updated.status == "permanently_failed"
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert event["details"]["auto_retry_skipped"] is True
    assert event["details"]["reason"] == "non_transient_error"
    assert event["details"]["failure"]["limit_exhausted"] is True
    assert event["details"]["failure"]["retryable"] is False


def test_file_lane_schedule_auto_retry_copies_identity_and_leaves_the_source_row(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="SLURM_TIMEOUT")
    service = _journal_service(repository)

    retry = service.schedule_auto_retry(job)

    source = repository.get_pipeline_job("job_1")
    assert retry.job_id != job["job_id"]
    assert retry.job_id == "job_1_retry_1"
    assert retry.run_id == job["run_id"]
    assert retry.cycle_id == job["cycle_id"]
    assert retry.job_type == job["job_type"]
    assert retry.model_id == job["model_id"]
    assert retry.stage == job["stage"]
    assert retry.retry_count == 1
    assert retry.status == "pending"
    assert retry.slurm_job_id is None
    assert source["status"] == "failed"
    assert source["retry_count"] == 0
    assert source["slurm_job_id"] == "123"
    (event,) = _journal_events_of(repository, "retry")
    assert event["status_from"] == "failed"
    assert event["status_to"] == "pending"


def test_file_lane_schedule_auto_retry_reuses_submission_failed_retry_without_slurm_binding(
    tmp_path: Path,
) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="NODE_FAILURE", retry_count=1)
    stale_retry = {
        **repository.get_pipeline_job("job_1"),
        "job_id": "job_1_retry_2",
        "status": "submission_failed",
        "idempotency_key": f"gfs:{_JOURNAL_CYCLE_ID}:basin_a:job_1_retry_2",
        "slurm_job_id": None,
        "array_task_id": None,
        "started_at": "2026-05-01T01:00:00Z",
        "finished_at": "2026-05-01T02:00:00Z",
        "exit_code": 1,
        "retry_count": 2,
        "error_code": "SUBMIT_INTERRUPTED",
        "error_message": "submission interrupted",
        "log_uri": "s3://logs/stale",
    }
    repository.upsert_pipeline_job(stale_retry)
    service = _journal_service(repository)

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
    assert _journal_job_ids(repository) == ["job_1", "job_1_retry_2"]
    (event,) = _journal_events_of(repository, "retry")
    assert event["details"]["reused_existing_retry_job"] is True
    assert event["details"]["previous_job_id"] == "job_1"


def test_file_lane_schedule_auto_retry_does_not_reuse_retry_with_slurm_binding(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="NODE_FAILURE", retry_count=1)
    existing_retry = {
        **repository.get_pipeline_job("job_1"),
        "job_id": "job_1_retry_2",
        "status": "submission_failed",
        "idempotency_key": f"gfs:{_JOURNAL_CYCLE_ID}:basin_a:job_1_retry_2",
        "slurm_job_id": "slurm_existing_retry",
        "retry_count": 2,
        "error_code": "SUBMIT_INTERRUPTED",
    }
    repository.upsert_pipeline_job(existing_retry)
    service = _journal_service(repository)

    with pytest.raises(RetryError) as exc_info:
        service.schedule_auto_retry(job)

    assert exc_info.value.code == "AUTO_RETRY_JOB_CONFLICT"
    assert exc_info.value.details["retry_job_id"] == "job_1_retry_2"
    assert exc_info.value.details["existing_slurm_job_id"] == "slurm_existing_retry"
    assert _journal_events_of(repository, "retry") == []


def test_file_lane_mark_permanently_failed_records_the_final_attempt(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="SLURM_TIMEOUT", retry_count=3)
    service = _journal_service(repository)

    updated = service.mark_permanently_failed(job)

    assert updated.status == "permanently_failed"
    (event,) = _journal_events_of(repository, "permanently_failed")
    assert event["status_from"] == "failed"
    assert event["status_to"] == "permanently_failed"
    assert event["details"]["final_retry_count"] == 3
    assert event["details"]["last_error"] == "SLURM_TIMEOUT"
    assert event["details"]["automatic_retry_stopped"] is True
    assert event["details"]["failure"]["classifier"] == "transient_slurm_runtime"
    assert event["details"]["failure"]["limit_exhausted"] is True


def test_file_lane_audit_event_auto(tmp_path: Path) -> None:
    repository, job = _journal_failed_job(tmp_path, error_code="STORAGE_WRITE_FAILED")
    service = _journal_service(repository)

    service.schedule_auto_retry(job)

    (event,) = _journal_events_of(repository, "retry")
    assert event["details"]["trigger"] == "auto"
    assert event["details"]["retry_count"] == 1
    assert event["details"]["previous_error"] == "STORAGE_WRITE_FAILED"
    assert event["details"]["backoff_seconds"] == 60
    assert event["details"]["previous_job_id"] == "job_1"
    assert event["details"]["slurm_job_id"] is None
    assert event["details"]["failure"]["classifier"] == "transient_slurm_runtime"
