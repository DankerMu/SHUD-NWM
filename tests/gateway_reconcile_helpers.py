"""Shared fixtures for the split gateway-reconcile suites (#1809).

Not a collectible test module (pytest's ``python_files`` ignores this name);
the ``tests/test_gateway_reconcile_*.py`` partitions import their store,
cohort, reconcile and identity fixtures from here so no helper is duplicated
across the split. The idempotency attempt worker, ``_StoreRepo`` and the
SQLAlchemy ``Session`` live together in this module on purpose: the barrier
harness monkeypatches them here by dotted path
(``tests.gateway_reconcile_helpers.Session`` / ``._StoreRepo``) so the
worker's free-name lookup still hits the patch.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator.persistence import Base, PipelineJob, PipelineStore


def _authoritative_absence_query(
    _key: str,
    **kwargs: Any,
) -> Any:
    from services.orchestrator.reconcile import CommentAccountingResult

    anchor = kwargs.get("submission_attempt_started_at") or datetime(2026, 7, 12, tzinfo=UTC)
    return CommentAccountingResult(
        (),
        scope="global",
        coverage_start=anchor,
        coverage_end=anchor,
        coverage_complete=True,
    )


class _StoreRepo:
    """Repository-shaped wrapper over PipelineStore for reservation tests.

    Exposes the ``reserve_pipeline_job``/``bind_pipeline_job_reservation``/
    ``query_candidate_state`` surface the chain repository implements, backed by
    the in-memory store, so the durable two-phase protocol is exercised exactly
    as production would.
    """

    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def query_candidate_state(self, idempotency_key: str):
        job = self.store.query_candidate_state(idempotency_key)
        return _job_dict(job) if job is not None else None

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        # Mirror the production contract: INSERT ... ON CONFLICT DO NOTHING
        # RETURNING. A returned row == this caller won; None == a row already
        # existed. The unique idempotency_key index is the race backstop.
        job = self.store.reserve_job(
            job_id=record["job_id"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            job_type=record["job_type"],
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            status=record.get("status", "reserved"),
            idempotency_key=record["idempotency_key"],
            candidate_id=record.get("candidate_id"),
        )
        return _job_dict(job) if job is not None else None

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        # Mirror the production conditional UPDATE: only a DEAD reservation
        # (slurm_job_id IS NULL AND status IN submission_failed/reservation_lost)
        # is re-claimed back to 'reserved'; a live row never matches.
        job = self.store.reclaim_reservation(
            record["idempotency_key"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            candidate_id=record.get("candidate_id"),
        )
        return _job_dict(job) if job is not None else None

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ):
        job = self.store.bind_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            status=status,
            array_task_id=array_task_id,
        )
        return _job_dict(job) if job is not None else None


def _job_dict(job: PipelineJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
        "job_type": job.job_type,
        "slurm_job_id": job.slurm_job_id,
        "model_id": job.model_id,
        "status": job.status,
        "stage": job.stage,
        "idempotency_key": job.idempotency_key,
        "candidate_id": job.candidate_id,
    }


def _store_repo() -> _StoreRepo:
    return _StoreRepo(_store())


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


def _versioned_master_reservation_record(
    *,
    created_at: datetime | None = None,
    member_count: int = 18,
    expected_user: str | None = None,
    expected_account: str | None = None,
    corrupt_digest: bool = False,
    submit_outcome: str | None = "submit_result_ambiguous",
    versioned: bool = True,
    source_id: str = "gfs",
    init_state_identities: Any = None,
) -> dict[str, Any]:
    """The clean reservation payload ``_file_cohort_repository`` persists.

    Extracted so a test can hand ``reserve_pipeline_job`` the very same shape
    without laundering it through the public projection first (#1180 J6): on the
    insert path the contract marker and the row kind come from the incoming
    record, and the public view is not a valid write payload (design D-B2).
    """

    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        forecast_cohort_digest,
    )
    from services.orchestrator.chain_config import scenario_for_source

    cycle_time = datetime(2026, 7, 12, tzinfo=UTC)
    canonical_source_id = normalize_source_id(source_id)
    source_id = canonical_source_id.lower()
    scenario_id = scenario_for_source(canonical_source_id)
    record = {
            "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            "job_id": f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast",
            "run_id": f"cycle_{source_id}_2026071200_forecast_fixture",
            "source_id": canonical_source_id,
            "cycle_id": f"{source_id}_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": None,
            "stage": "forecast",
            "idempotency_key": f"cycle_{source_id}_2026071200_forecast_fixture:forecast",
            "slurm_comment": f"nhms_idem:cycle_{source_id}_2026071200_forecast_fixture:forecast",
            "submit_outcome": None if versioned else submit_outcome,
            "restart_stage": "forecast",
            "cohort_members": [
                {
                    "array_task_id": index,
                    "candidate_id": f"{canonical_source_id}:2026-07-12T00:00:00Z:model_{index}:{scenario_id}",
                    "run_id": f"fcst_{source_id}_2026071200_model_{index}",
                    "model_id": f"model_{index}",
                    "basin_id": f"basin_{index}",
                    "scenario_id": scenario_id,
                    "restart_stage": "forecast",
                }
                for index in range(member_count)
            ],
            "submission_attempt": 1,
            "submission_attempt_started_at": created_at or cycle_time,
            "expected_slurm_user": expected_user,
            "expected_slurm_account": expected_account,
            "slurm_ownership_required": bool(expected_user and expected_account),
            "created_at": created_at or cycle_time,
            "updated_at": created_at or cycle_time,
        }
    if init_state_identities is not None:
        record["init_state_identities"] = init_state_identities
    record["cohort_digest"] = forecast_cohort_digest(record)
    if not versioned:
        record.pop("accepted_submit_contract_version")
    if corrupt_digest:
        record["cohort_digest"] = "0" * 64
    return record


def _file_cohort_repository(
    tmp_path: Any,
    *,
    created_at: datetime | None = None,
    member_count: int = 18,
    expected_user: str | None = None,
    expected_account: str | None = None,
    corrupt_digest: bool = False,
    with_runtime_rows: bool = True,
    submit_outcome: str | None = "submit_result_ambiguous",
    versioned: bool = True,
    source_id: str = "gfs",
    init_state_identities: Any = None,
) -> Any:
    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    source_id = normalize_source_id(source_id).lower()
    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
        corrupt_digest=corrupt_digest,
        submit_outcome=submit_outcome,
        versioned=versioned,
        source_id=source_id,
        init_state_identities=init_state_identities,
    )
    repository.reserve_pipeline_job(record)
    if versioned and submit_outcome == "submit_result_ambiguous":
        from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

        repository.transition_pipeline_job_submit_evidence(
            record["job_id"],
            AcceptedSubmitTransition.timeout(),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
    if with_runtime_rows:
        _append_cohort_placeholders(repository, member_count, source_id=source_id)
    return repository


def _bind_current_file_cohort(
    repository: Any,
    idempotency_key: str,
    *,
    slurm_job_id: str,
    status: str = "submitted",
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    current = repository.query_candidate_state(idempotency_key)
    assert current is not None
    result = repository.commit_pipeline_job_submit_attempt(
        idempotency_key,
        pipeline_job_id=str(current["job_id"]),
        expected_submission_attempt=int(current.get("submission_attempt") or 1),
        slurm_job_id=slurm_job_id,
        transition=AcceptedSubmitTransition.accepted(status=status),
    )
    assert result.committed


def _append_cohort_placeholders(
    repository: Any,
    count: int = 18,
    *,
    source_id: str = "gfs",
    common_updates: dict[str, Any] | None = None,
    updates_by_index: dict[int, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Seed one ``hydro_run`` row per cohort member; return them by ``run_id``
    so a caller can assert on what was actually persisted."""
    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.chain_config import scenario_for_source

    written: dict[str, dict[str, Any]] = {}
    canonical_source_id = normalize_source_id(source_id)
    source_id = canonical_source_id.lower()
    scenario_id = scenario_for_source(canonical_source_id)
    for index in range(count):
        row = {
            "run_id": f"fcst_{source_id}_2026071200_model_{index}",
            "candidate_id": f"{canonical_source_id}:2026-07-12T00:00:00Z:model_{index}:{scenario_id}",
            "run_type": "forecast",
            "scenario_id": scenario_id,
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "array_task_id": index,
            "basin_version_id": f"basin_v{index}",
            "forcing_version_id": f"forc_{source_id}_2026071200_model_{index}",
            "init_state_id": f"state_{index}",
            "source_id": canonical_source_id,
            "cycle_time": "2026-07-12T00:00:00Z",
            "start_time": "2026-07-12T00:00:00Z",
            "end_time": "2026-07-12T18:00:00Z",
            "status": "failed",
            "submission_attempt": 1,
            "run_manifest_uri": f"s3://nhms/runs/model_{index}/run-manifest.json",
            "output_uri": f"s3://nhms/runs/model_{index}/output",
            "log_uri": f"s3://nhms/runs/model_{index}/logs",
            "error_code": "SLURM_GATEWAY_UNAVAILABLE",
            "error_message": "transport timeout",
        }
        row.update(common_updates or {})
        row.update((updates_by_index or {}).get(index) or {})
        written[str(row["run_id"])] = repository.append_historical_hydro_run(row) or row
    return written


def _seed_unrelated_history(repository: Any, *, count: int = 10) -> None:
    for index in range(count):
        cycle_time = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=index * 6)
        stamp = cycle_time.strftime("%Y%m%d%H")
        repository.append_historical_pipeline_job(
            {
                "job_id": f"job_fcst_gfs_{stamp}_history_model_{index}",
                "run_id": f"fcst_gfs_{stamp}_history_model_{index}",
                "cycle_id": f"gfs_{stamp}",
                "job_type": "run_shud_forecast_array",
                "model_id": f"history_model_{index}",
                "status": "succeeded",
                "stage": "forecast",
                "candidate_id": f"history_{index}",
            }
        )
    malformed_direct = repository.root / "pipeline-jobs" / "job_unrelated_malformed.json"
    malformed_direct.parent.mkdir(parents=True, exist_ok=True)
    malformed_direct.write_text("{not-json", encoding="utf-8")
    malformed_latest = repository.root / "latest" / "gfs" / "2025010100" / "bad.json"
    malformed_latest.parent.mkdir(parents=True, exist_ok=True)
    malformed_latest.write_text("[]", encoding="utf-8")
    malformed_journal = repository.root / "journal" / "gfs" / "2025010100.jsonl"
    malformed_journal.parent.mkdir(parents=True, exist_ok=True)
    malformed_journal.write_text("{not-json\n", encoding="utf-8")


def _past_grace_now(store: _StoreRepo, grace: Any) -> Any:
    """A tz-aware ``now`` just past ``grace`` for the sole reserved-unbound row.

    The reconcile grace guard anchors on ``updated_at`` (refreshed by reserve,
    reclaim, and bind), so the clock must be driven past grace relative to that
    anchor. SQLite returns naive timestamps; normalize to UTC so the injected
    clock is comparable with the reconcile guard's tz-aware arithmetic.
    """

    from datetime import UTC, timedelta

    anchor = store.store.query_reserved_unbound_jobs()[0].updated_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return anchor + grace + timedelta(seconds=1)


def _make_idempotency_attempt_worker(
    *,
    engine: Any,
    barrier: threading.Barrier,
    results: list[Any],
    errors: list[tuple[int, BaseException]],
    key: str,
    common: dict[str, Any],
    reserve: Callable[..., Any],
    pre_body: Callable[[int], None] | None = None,
    post_session: Callable[[int, Session], None] | None = None,
) -> Callable[[int], None]:
    """The ONE shipping worker body for the 8-party idempotency harness (#1645).

    Session identity is tracked SEPARATELY from the wrappers so a successfully
    created Session is closed even if ``PipelineStore``/``_StoreRepo``
    construction fails afterwards (design D3), and a ``session.close()``
    failure is captured into the indexed error channel ordered AFTER any body
    error instead of escaping only as ``PytestUnhandledThreadExceptionWarning``
    (task 5.3). ``pre_body`` and ``post_session`` are deterministic injection
    hooks so the failure-injection tests execute THIS exact worker logic rather
    than a copied twin (Phase 2 gaps 3/4).
    """

    def _attempt(index: int) -> None:
        session: Session | None = None
        try:
            if pre_body is not None:
                # A pre-arrival injected failure is captured by the SAME
                # catch-all as the shipped worker's own failures.
                pre_body(index)
            session = Session(engine)
            if post_session is not None:
                post_session(index, session)
            repo = _StoreRepo(PipelineStore(session))
            barrier.wait()  # release all threads into reserve at once.
            results[index] = reserve(
                repo, idempotency_key=key, job_id=f"job_{index}", **common
            )
        except BaseException as error:
            errors.append((index, error))
        finally:
            if session is not None:
                try:
                    session.close()
                except BaseException as cleanup_error:
                    # A close failure after a body error must stay indexed and
                    # ordered AFTER the body error, never escape only as a
                    # PytestUnhandledThreadExceptionWarning (task 5.3).
                    errors.append((index, cleanup_error))

    return _attempt
