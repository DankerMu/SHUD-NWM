"""Current-master typed runtime transitions: zero-write compatibility
guards, retry identity, cycle sync and cancellation.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)


def test_current_master_public_compatibility_mutations_are_zero_write_rejected(
    tmp_path: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path / "existing", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    before = repository.get_pipeline_job(job_id)

    def journal_bytes() -> bytes:
        return b"".join(
            path.read_bytes()
            for path in sorted(repository.root.glob("journal/**/*.jsonl"))
        )

    before_journal = journal_bytes()
    forbidden_calls = (
        lambda: repository.bind_pipeline_job_reservation(key, slurm_job_id="99001"),
        lambda: repository.bind_reservation(key, slurm_job_id="99001"),
        lambda: repository.transition_pipeline_job_submit_evidence(
            job_id,
            AcceptedSubmitTransition.timeout(),
        ),
        lambda: repository.permit_pipeline_job_retry(job_id),
        lambda: repository.record_pipeline_job_reconciliation(
            job_id,
            status="running",
        ),
        lambda: repository.update_pipeline_job_status(job_id, "running"),
        lambda: repository.update_job_status(job_id, "running"),
        lambda: repository.upsert_pipeline_job({**before, "status": "running"}),
    )
    for call in forbidden_calls:
        with pytest.raises(FileOrchestrationJournalError):
            call()
        assert repository.get_pipeline_job(job_id) == before
        assert journal_bytes() == before_journal

    for method_name in ("reserve_pipeline_job", "upsert_pipeline_job", "append_historical_pipeline_job"):
        empty = FileOrchestrationJournalRepository(tmp_path / method_name / "journal")
        method = getattr(empty, method_name)
        with pytest.raises(FileOrchestrationJournalError):
            method(dict(before))
        assert empty.get_pipeline_job(job_id) is None
        assert not tuple(empty.root.glob("journal/**/*.jsonl"))


@pytest.mark.parametrize(
    "transition_factory",
    [
        pytest.param(lambda transition: transition.begin_attempt(), id="begin_attempt"),
        pytest.param(lambda transition: transition.accepted(status="submitted"), id="accepted"),
        pytest.param(lambda transition: transition.rejected(), id="rejected"),
        pytest.param(
            lambda transition: transition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="88001",
                status="submitted",
            ),
            id="matched_bound",
        ),
        pytest.param(
            lambda transition: transition.accounting(
                "absence_retry_permitted",
                submit_outcome="submit_result_ambiguous",
                status="reservation_lost",
            ),
            id="absence_retry_permitted",
        ),
    ],
)
def test_round8_generic_versioned_submit_api_rejects_dedicated_authority_transitions_zero_write(
    tmp_path: Any,
    transition_factory: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.transition_pipeline_job_submit_evidence(
            job_id,
            transition_factory(AcceptedSubmitTransition),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
    assert error.value.field == "transition"
    assert {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize(
    "missing_cas",
    ["attempt", "statuses", "unbound"],
)
def test_round8_generic_versioned_nonbinding_transition_requires_complete_cas(
    tmp_path: Any,
    missing_cas: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1)
    kwargs = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": 1,
        "expected_statuses": ("reserved",),
        "require_unbound": True,
    }
    if missing_cas == "attempt":
        kwargs["expected_submission_attempt"] = None
    elif missing_cas == "statuses":
        kwargs["expected_statuses"] = None
    else:
        kwargs["require_unbound"] = False
    with pytest.raises(FileOrchestrationJournalError, match="requires_cas"):
        repository.transition_pipeline_job_submit_evidence(
            "job_cycle_gfs_2026071200_forecast_fixture_forecast",
            AcceptedSubmitTransition.accounting(
                "absence_deferred",
                submit_outcome="submit_result_ambiguous",
                status="reserved",
            ),
            **kwargs,
        )


@pytest.mark.parametrize(
    ("current_status", "target_status", "expected_outcome"),
    [
        ("submitted", "pending", "applied"),
        ("submitted", "running", "applied"),
        ("pending", "running", "applied"),
        ("queued", "running", "applied"),
        ("running", "reconcile_unverified", "applied"),
        ("running", "pending", "stale"),
        ("reconcile_unverified", "running", "stale"),
        ("cancellation_pending", "running", "stale"),
        ("submitted", "invented", "stale"),
    ],
)
def test_round8_runtime_transition_graph_is_closed_monotonic_and_zero_writes_stale_edges(
    tmp_path: Any,
    current_status: str,
    target_status: str,
    expected_outcome: str,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    commit_status = (
        current_status
        if current_status in {"submitted", "pending", "queued", "running"}
        else "submitted"
    )
    _bind_current_file_cohort(repository, key, slurm_job_id="88101", status=commit_status)
    if current_status == "reconcile_unverified":
        assert repository.transition_pipeline_job_runtime_status(
            job_id,
            "reconcile_unverified",
            expected_statuses=("submitted",),
        ).committed
    elif current_status == "cancellation_pending":
        assert repository.request_pipeline_job_cancellation(
            job_id,
            expected_statuses=("submitted",),
            reason="test",
        ).committed
    before = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    result = repository.transition_pipeline_job_runtime_status(
        job_id,
        target_status,
        expected_statuses=(current_status,),
    )
    assert result.outcome == expected_outcome
    if expected_outcome == "stale":
        assert {
            str(path.relative_to(repository.root)): path.read_bytes()
            for path in repository.root.rglob("*")
            if path.is_file()
        } == before


def test_round8_runtime_and_cancel_transitions_require_accepted_real_bound_master(
    tmp_path: Any,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)
    runtime = repository.transition_pipeline_job_runtime_status(job_id, "running")
    cancel = repository.request_pipeline_job_cancellation(
        job_id,
        expected_statuses=("reserved",),
        reason="operator",
    )
    assert (runtime.outcome, cancel.outcome) == ("stale", "stale")
    assert repository.get_pipeline_job(job_id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retry_count", 1),
        ("manual_retry_marker", True),
        ("previous_job_id", "previous-master"),
        ("candidate_projections", "valid_projection"),
        ("reconciliation_reason_class", "process_unavailable"),
    ],
)
def test_round8_clean_reservation_guard_rejects_each_retry_and_authority_field_zero_write(
    tmp_path: Any,
    field: str,
    value: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    template = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
            "retry_count": 0,
            "manual_retry_marker": False,
            "previous_job_id": None,
        }
    )
    if field == "candidate_projections":
        clean[field] = [
            {
                **{
                    key: clean["cohort_members"][0][key]
                    for key in ("array_task_id", "candidate_id", "run_id", "model_id")
                },
                "array_task_outcome": "failed",
                "restart_stage": "forecast",
                "native_shud_resubmitted": False,
            }
        ]
    else:
        clean[field] = value
    repository = FileOrchestrationJournalRepository(tmp_path / field / "journal")
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.reserve_pipeline_job(clean)
    assert error.value.field == field
    assert repository.get_pipeline_job(job_id) is None
    assert not tuple(repository.root.glob("journal/**/*.jsonl"))
    assert not tuple(repository.root.glob("pipeline-jobs/*.json"))
    assert not tuple(repository.root.glob("reconcile-inventory/*.json"))


def test_round8_cancellation_intent_is_sticky_and_completion_is_idempotent_after_reopen(
    tmp_path: Any,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="88102", status="running")
    intent = repository.request_pipeline_job_cancellation(
        job_id,
        expected_statuses=("running",),
        reason="operator",
    )
    assert intent.outcome == "applied"
    reopened = type(repository)(repository.root)
    stale_poll = reopened.transition_pipeline_job_runtime_status(
        job_id,
        "running",
        expected_statuses=("cancellation_pending",),
    )
    assert stale_poll.outcome == "stale"
    finished_at = datetime(2026, 7, 12, 0, 3, tzinfo=UTC)
    first = reopened.complete_pipeline_job_cancellation(
        job_id,
        finished_at=finished_at,
        exit_code=0,
        error_code=None,
        error_message=None,
        log_uri=None,
    )
    second = type(repository)(repository.root).complete_pipeline_job_cancellation(
        job_id,
        finished_at=finished_at,
        exit_code=0,
        error_code=None,
        error_message=None,
        log_uri=None,
    )
    assert (first.outcome, second.outcome) == ("applied", "idempotent")


def test_marker_free_master_compatibility_mutations_remain_available(tmp_path: Any) -> None:
    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        versioned=False,
        submit_outcome=None,
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    bound = repository.bind_pipeline_job_reservation(key, slurm_job_id="99002")
    assert bound is not None
    previous, updated = repository.update_pipeline_job_status(job_id, "running")
    assert previous == "submitted"
    assert updated["status"] == "running"


@pytest.mark.parametrize(
    ("master_status", "task_outcomes"),
    [
        ("failed", ("failed", "failed")),
        ("partially_failed", ("succeeded", "failed")),
    ],
)
def test_current_master_retry_never_persists_marker_free_clone(
    tmp_path: Any,
    master_status: str,
    task_outcomes: tuple[str, str],
) -> None:
    from types import SimpleNamespace

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        _next_current_master_retry_identity,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99003")
    members = repository.get_pipeline_job(job_id)["cohort_members"]
    repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="99003",
        projections=[
            {
                **member,
                "array_task_outcome": task_outcomes[index],
                "task_slurm_job_id": f"99003_{index}",
                "restart_stage": "state_save_qc",
                "native_shud_resubmitted": False,
                "error_code": "SLURM_TIMEOUT" if task_outcomes[index] == "failed" else None,
            }
            for index, member in enumerate(members)
        ],
        complete=True,
        master_status=master_status,
        master_error_code="SLURM_TIMEOUT",
        reconciliation_decision="matched_bound",
    )
    failed = repository.get_pipeline_job(job_id)
    failed["error_code"] = "SLURM_TIMEOUT"

    pending = FileJournalRetryService(repository).handle_failed_job(SimpleNamespace(**failed))

    assert pending.job_id == f"{job_id}_retry_1"
    assert pending.status == "pending"
    assert _next_current_master_retry_identity(
        {"job_id": pending.job_id, "retry_count": 0}
    ) == (f"{job_id}_retry_2", 2)
    assert repository.get_pipeline_job(pending.job_id) is None
    durable_rows = repository.query_pipeline_jobs_by_cycle("gfs_2026071200")
    assert all(
        row.get("accepted_submit_contract_version") is not None
        for row in durable_rows
        if row.get("job_id", "").startswith("job_cycle_gfs_2026071200_forecast_fixture")
    )


def test_cycle_sync_uses_typed_runtime_transition_and_defers_terminal_master_truth(
    tmp_path: Any,
) -> None:
    from services.orchestrator.chain_forecast_control import sync_cycle_statuses

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99004")

    class Client:
        status = "running"

        def get_job_status(self, _slurm_job_id: str) -> dict[str, Any]:
            return {"status": self.status, "started_at": "2026-07-12T00:01:00Z", "exit_code": 0}

    class Harness:
        def __init__(self) -> None:
            self.repository = repository
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

        def _display_log_publication_for_pipeline_job(self, _job: Any) -> None:
            return None

        def _try_publish_log_for_advertise(self, *_args: Any) -> None:
            return None

        def _raise_publish_error_after_durable_update(self, attempt: Any) -> None:
            assert attempt is None

    harness = Harness()
    updates = sync_cycle_statuses(harness, "gfs_2026071200")
    assert [row["status"] for row in updates] == ["running"]
    assert repository.get_pipeline_job(job_id)["status"] == "running"

    harness.slurm_client.status = "succeeded"
    assert sync_cycle_statuses(harness, "gfs_2026071200") == []
    assert repository.get_pipeline_job(job_id)["status"] == "running"
    assert [job.job_id for job in type(repository)(repository.root).query_inflight_jobs()] == [job_id]


@pytest.mark.parametrize("gateway_fails", [False, True])
def test_cycle_cancel_persists_typed_intent_before_gateway_and_reopens_safely(
    tmp_path: Any,
    gateway_fails: bool,
) -> None:
    from services.orchestrator.chain import SlurmClientError
    from services.orchestrator.chain_forecast_control import cancel_active_cycle_jobs

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99005")
    repository.transition_pipeline_job_runtime_status(
        job_id,
        "running",
        expected_statuses=("submitted",),
    )
    observed_statuses: list[str] = []

    class Client:
        def cancel_job(self, _slurm_job_id: str) -> dict[str, Any]:
            observed_statuses.append(type(repository)(repository.root).get_pipeline_job(job_id)["status"])
            if gateway_fails:
                raise SlurmClientError("SLURM_GATEWAY_UNAVAILABLE", "cancel failed")
            return {
                "status": "cancelled",
                "finished_at": "2026-07-12T00:02:00Z",
                "exit_code": 0,
            }

    class Harness:
        def __init__(self) -> None:
            self.repository = repository
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

    harness = Harness()
    if gateway_fails:
        with pytest.raises(SlurmClientError, match="cancel failed"):
            cancel_active_cycle_jobs(harness, "gfs_2026071200", reason="operator_requested")
        expected_status = "cancellation_pending"
    else:
        cancelled = cancel_active_cycle_jobs(
            harness,
            "gfs_2026071200",
            reason="operator_requested",
        )
        assert [row["status"] for row in cancelled] == ["reconcile_unverified"]
        expected_status = "reconcile_unverified"

    assert observed_statuses == ["cancellation_pending"]
    reopened = type(repository)(repository.root)
    assert reopened.get_pipeline_job(job_id)["status"] == expected_status
    assert [job.job_id for job in reopened.query_inflight_jobs()] == [job_id]
