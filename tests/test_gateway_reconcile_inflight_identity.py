"""Tests for restart reconcile-by-identity of in-flight Slurm jobs.

Reconcile MUST read job ids from the durable ``pipeline_job`` table (not gateway
memory), verify candidate identity via ``sacct``, and never resubmit a
still-running or already-terminal candidate.
"""

from __future__ import annotations

from typing import Any

from services.orchestrator.persistence import PipelineStore
from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
)
from tests.gateway_reconcile_helpers import _store


def _make_inflight_job(
    store: PipelineStore,
    *,
    job_id: str,
    slurm_job_id: str,
    stage: str = "run_shud_forecast_array",
    status: str = "running",
    run_id: str = "run_1",
    model_id: str = "model_1",
    array_task_id: int | None = None,
) -> None:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id="cycle_1",
        job_type=stage,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
    )
    if array_task_id is not None:
        job.array_task_id = array_task_id
        store.session.add(job)
        store.session.commit()


def _fake_sacct(records: dict[str, SacctRecord | None]):
    """Fake sacct querier backed by a dict; ``None`` => unknown to accounting."""

    def _query(slurm_job_id: str) -> SacctRecord | None:
        return records.get(str(slurm_job_id))

    return _query


def test_restart_reconcile_reads_pipeline_job_not_memory() -> None:
    # Durable in-flight job exists; gateway memory (_jobs) is irrelevant/empty.
    store = _store()
    _make_inflight_job(store, job_id="job_a", slurm_job_id="99001")

    sacct = _fake_sacct(
        {
            "99001": SacctRecord(
                slurm_job_id="99001",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
            )
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert len(outcomes) == 1
    assert outcomes[0].slurm_job_id == "99001"
    # State came from durable DB row + sacct, not any in-memory gateway map.
    assert store.get_job("job_a").status == "succeeded"


def test_reconcile_verifies_candidate_identity_via_sacct() -> None:
    store = _store()
    # Matching identity: sacct JobName carries the recorded stage token.
    _make_inflight_job(store, job_id="job_match", slurm_job_id="2001")
    # Mismatched identity: sacct returns a job for an unrelated stage.
    _make_inflight_job(store, job_id="job_mismatch", slurm_job_id="2002")

    sacct = _fake_sacct(
        {
            "2001": SacctRecord(
                slurm_job_id="2001",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
            ),
            "2002": SacctRecord(
                slurm_job_id="2002",
                raw_state="COMPLETED",
                job_name="nhms_some_other_basin_job",
            ),
        }
    )

    reconcile_inflight_jobs(store, sacct_query=sacct)

    assert store.get_job("job_match").status == "succeeded"
    # Mismatch is not accepted: typed unverified, NOT a terminal success.
    mismatch = store.get_job("job_mismatch")
    assert mismatch.status == RECONCILE_UNVERIFIED_STATUS
    assert mismatch.error_code == "SLURM_RECONCILE_UNVERIFIED"


def test_reconcile_generic_array_job_name_requires_manifest_task_identity() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_forecast_task_3",
        slurm_job_id="2103",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    _make_inflight_job(
        store,
        job_id="job_forecast_no_identity",
        slurm_job_id="2104",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_b",
        model_id="model_b",
        array_task_id=4,
    )

    sacct = _fake_sacct(
        {
            "2103_3": SacctRecord(
                slurm_job_id="2103_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                submitted_manifest={
                    "pipeline_job_id": "job_forecast_task_3",
                    "run_id": "fcst_gfs_2026062912_model_a",
                    "model_id": "model_a",
                    "stage": "run_shud_forecast_array",
                    "array_task_id": 3,
                },
                stdout_identity={
                    "run_id": "fcst_gfs_2026062912_model_a",
                    "model_id": "model_a",
                    "stage": "forecast",
                    "task_id": 3,
                },
            ),
            "2104_4": SacctRecord(
                slurm_job_id="2104_4",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert {outcome.job_id: outcome.action for outcome in outcomes} == {
        "job_forecast_task_3": "terminal",
        "job_forecast_no_identity": "unverified",
    }
    assert store.get_job("job_forecast_task_3").status == "succeeded"
    assert store.get_job("job_forecast_no_identity").status == RECONCILE_UNVERIFIED_STATUS


def test_reconcile_generic_terminal_comment_only_is_unverified() -> None:
    from services.orchestrator.reservation import slurm_comment_for

    store = _store()
    _make_inflight_job(
        store,
        job_id="job_forecast_comment_only",
        slurm_job_id="2105",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    job = store.get_job("job_forecast_comment_only")
    job.idempotency_key = "gfs:gfs_2026062912:basin_a:forecast"
    store.session.add(job)
    store.session.commit()
    sacct = _fake_sacct(
        {
            "2105_3": SacctRecord(
                slurm_job_id="2105_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                comment=slurm_comment_for(job.idempotency_key),
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_forecast_comment_only").status == RECONCILE_UNVERIFIED_STATUS


def test_reconcile_queries_array_task_when_durable_row_has_task_id() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_precise_task_3",
        slurm_job_id="12345",
        stage="run_shud_forecast_array",
        array_task_id=3,
    )
    queried: list[str] = []

    def sacct(slurm_job_id: str) -> SacctRecord | None:
        queried.append(slurm_job_id)
        if slurm_job_id == "12345_3":
            return SacctRecord(
                slurm_job_id="12345_3",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
                task_id=3,
                array_task_id=3,
            )
        return None

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert queried == ["12345_3"]
    assert outcomes[0].action == "terminal"
    assert store.get_job("job_precise_task_3").status == "succeeded"


def test_reconcile_generic_array_task_row_accepts_exact_task_identity() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_generic_task_3",
        slurm_job_id="12346",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    queried: list[str] = []

    def sacct(slurm_job_id: str) -> SacctRecord | None:
        queried.append(slurm_job_id)
        if slurm_job_id == "12346_3":
            return SacctRecord(
                slurm_job_id="12346_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                task_id=3,
                array_task_id=3,
            )
        return None

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert queried == ["12346_3"]
    assert outcomes[0].action == "terminal"
    assert store.get_job("job_generic_task_3").status == "succeeded"


def test_reconcile_legacy_non_db_free_precise_job_name_remains_compatible() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_legacy_non_db_free",
        slurm_job_id="2110",
        stage="run_shud_forecast_array",
        run_id="legacy_run_1",
        model_id="legacy_model",
    )
    sacct = _fake_sacct(
        {
            "2110": SacctRecord(
                slurm_job_id="2110",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "terminal"
    assert store.get_job("job_legacy_non_db_free").status == "succeeded"


def test_reconcile_unknown_to_accounting_is_unverified_not_resubmitted() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_unknown", slurm_job_id="3003")

    # sacct knows nothing about this job id.
    sacct = _fake_sacct({"3003": None})

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_unknown").status == RECONCILE_UNVERIFIED_STATUS


def test_pipeline_store_success_status_clears_previous_unverified_error() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_recovered",
        slurm_job_id="3004",
        status="running",
    )
    store.update_job_status(
        "job_recovered",
        RECONCILE_UNVERIFIED_STATUS,
        error_code="SLURM_RECONCILE_UNVERIFIED",
        error_message="sacct could not verify the candidate identity.",
    )
    store.update_job_status("job_recovered", "succeeded", exit_code=0)
    recovered = store.get_job("job_recovered")
    assert recovered.status == "succeeded"
    assert recovered.error_code is None
    assert recovered.error_message is None


def test_reconcile_no_duplicate_resubmit_for_running_or_terminal() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_running", slurm_job_id="4001")
    _make_inflight_job(store, job_id="job_done", slurm_job_id="4002")

    submit_calls: list[str] = []

    class _GuardStore:
        """Wrap the real store and trap any unexpected submit/create call."""

        def __init__(self, inner: PipelineStore) -> None:
            self._inner = inner

        def query_inflight_jobs(self):
            return self._inner.query_inflight_jobs()

        def update_job_status(self, *args: Any, **kwargs: Any):
            return self._inner.update_job_status(*args, **kwargs)

        def create_job(self, *args: Any, **kwargs: Any):
            submit_calls.append(kwargs.get("job_id", "?"))
            raise AssertionError("reconcile must not create/resubmit jobs")

    sacct = _fake_sacct(
        {
            "4001": SacctRecord(
                slurm_job_id="4001",
                raw_state="RUNNING",
                job_name="nhms_run_shud_forecast_array",
            ),
            "4002": SacctRecord(
                slurm_job_id="4002",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(_GuardStore(store), sacct_query=sacct)

    assert submit_calls == []
    actions = {o.job_id: o.action for o in outcomes}
    assert actions["job_running"] == "still_running"
    assert actions["job_done"] == "terminal"
    assert store.get_job("job_running").status == "running"
    assert store.get_job("job_done").status == "succeeded"


def test_reconcile_failed_job_records_error_code() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_fail", slurm_job_id="5005")

    sacct = _fake_sacct(
        {
            "5005": SacctRecord(
                slurm_job_id="5005",
                raw_state="TIMEOUT",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:1",
            )
        }
    )

    reconcile_inflight_jobs(store, sacct_query=sacct)

    job = store.get_job("job_fail")
    assert job.status == "failed"
    assert job.error_code == "SLURM_TIMEOUT"
