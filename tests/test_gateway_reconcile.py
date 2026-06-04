"""Tests for restart reconcile-by-identity of in-flight Slurm jobs.

Reconcile MUST read job ids from the durable ``pipeline_job`` table (not gateway
memory), verify candidate identity via ``sacct``, and never resubmit a
still-running or already-terminal candidate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator.persistence import Base, PipelineStore
from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
)


def _store() -> PipelineStore:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return PipelineStore(Session(engine))


def _make_inflight_job(
    store: PipelineStore,
    *,
    job_id: str,
    slurm_job_id: str,
    stage: str = "run_shud_forecast_array",
    status: str = "running",
    run_id: str = "run_1",
    model_id: str = "model_1",
) -> None:
    store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id="cycle_1",
        job_type=stage,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
    )


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


def test_reconcile_unknown_to_accounting_is_unverified_not_resubmitted() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_unknown", slurm_job_id="3003")

    # sacct knows nothing about this job id.
    sacct = _fake_sacct({"3003": None})

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_unknown").status == RECONCILE_UNVERIFIED_STATUS


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
