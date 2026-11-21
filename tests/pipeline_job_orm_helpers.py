"""ORM seeding and ORM-backed reservation doubles for ``ops.pipeline_job`` (#3262).

Not a collectible test module. Tests that need ``pipeline_job`` rows in place,
or a repository double that keeps the two-phase reservation semantics, use
these helpers instead of the ``PipelineStore`` write methods ``create_job`` /
``reserve_job`` / ``reclaim_reservation`` / ``bind_reservation``, which R5
deleted (``openspec/changes/redesign-orchestration-core`` task 7).

Each function re-implements one of those methods with plain ORM statements:

- ``seed_pipeline_job`` mirrors ``create_job``: same defaults, ``submitted_at``
  stamped with ``utc_now()`` unless the caller passes it (``None`` keeps it
  NULL), commit + refresh, or only a flush when ``commit=False``.
- ``orm_reserve_job`` mirrors ``reserve_job``: the insert runs inside
  ``begin_nested()``; an ``IntegrityError`` (unique ``idempotency_key`` or
  primary key) returns ``None`` and leaves the session usable.
- ``orm_reclaim_reservation`` mirrors ``reclaim_reservation``: only a dead row
  (``slurm_job_id IS NULL`` and status ``submission_failed`` /
  ``reservation_lost``) is taken back to ``reserved``; with no row for the key,
  a ``pending``, unkeyed, unbound row named by ``job_id`` is adopted. Identity
  columns are filled only when NULL.
- ``orm_bind_reservation`` mirrors ``bind_reservation``: a no-op returning
  ``None`` when the row is missing or already bound; ``submitted_at`` is set
  only when NULL and ``array_task_id`` only when given.

``OrmPipelineStore`` is the store-shaped double for production code that calls
these method names on its ``store`` argument (``reconcile_reserved_unbound_jobs``
-> ``store.bind_reservation``). It is deliberately not a ``PipelineStore``
subclass: the four write methods go to the functions above, and only the
read/update methods R5 keeps are delegated, by name.

``tests/test_pipeline_job_orm_helpers.py`` pins each function to literal
outcomes, recorded from the original methods before R5-2 deleted them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.common.contracts.time_normalization import utc_now
from services.orchestrator.persistence import RESERVED_STATUS, PipelineEvent, PipelineJob, PipelineStore

_UNSET_SUBMITTED_AT: Any = object()
_DEAD_RESERVATION_STATUSES = ("submission_failed", "reservation_lost")


def seed_pipeline_job(
    session: Session,
    *,
    job_id: str,
    run_id: str | None,
    cycle_id: str | None,
    job_type: str,
    slurm_job_id: str | None,
    model_id: str | None,
    stage: str | None,
    status: str = "pending",
    retry_count: int = 0,
    manual_retry_marker: bool = False,
    idempotency_key: str | None = None,
    candidate_id: str | None = None,
    submitted_at: datetime | None = _UNSET_SUBMITTED_AT,
    commit: bool = True,
) -> PipelineJob:
    """Insert one ``pipeline_job`` row exactly as ``create_job`` wrote it."""

    if submitted_at is _UNSET_SUBMITTED_AT:
        submitted_at = utc_now()
    job = PipelineJob(
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type=job_type,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
        retry_count=retry_count,
        manual_retry_marker=manual_retry_marker,
        idempotency_key=idempotency_key,
        candidate_id=candidate_id,
        submitted_at=submitted_at,
    )
    session.add(job)
    if commit:
        session.commit()
        session.refresh(job)
    else:
        session.flush()
    return job


def _row_for_key(session: Session, idempotency_key: str) -> PipelineJob | None:
    statement = select(PipelineJob).where(PipelineJob.idempotency_key == idempotency_key)
    return session.scalars(statement).first()


def orm_reserve_job(
    session: Session,
    *,
    job_id: str,
    run_id: str | None,
    cycle_id: str | None,
    job_type: str,
    model_id: str | None,
    stage: str | None,
    status: str = RESERVED_STATUS,
    idempotency_key: str,
    candidate_id: str | None = None,
) -> PipelineJob | None:
    """Phase-1 reserve: the inserted row on a win, ``None`` on any conflict."""

    try:
        with session.begin_nested():
            job = PipelineJob(
                job_id=job_id,
                run_id=run_id,
                cycle_id=cycle_id,
                job_type=job_type,
                slurm_job_id=None,
                model_id=model_id,
                stage=stage,
                status=status,
                retry_count=0,
                manual_retry_marker=False,
                idempotency_key=idempotency_key,
                candidate_id=candidate_id,
                submitted_at=None,
            )
            session.add(job)
            session.flush()
    except IntegrityError:
        return None
    session.commit()
    session.refresh(job)
    return job


def orm_reclaim_reservation(
    session: Session,
    idempotency_key: str,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
    model_id: str | None = None,
    stage: str | None = None,
    candidate_id: str | None = None,
) -> PipelineJob | None:
    """Take a dead reservation (or an unkeyed pending row) back to ``reserved``."""

    job = _row_for_key(session, idempotency_key)
    if job is not None:
        if job.slurm_job_id is not None or job.status not in _DEAD_RESERVATION_STATUSES:
            return None
    else:
        if job_id in (None, ""):
            return None
        job = session.scalars(select(PipelineJob).where(PipelineJob.job_id == job_id)).first()
        if (
            job is None
            or job.idempotency_key is not None
            or job.slurm_job_id is not None
            or job.status != "pending"
        ):
            return None
        job.idempotency_key = idempotency_key
    job.status = RESERVED_STATUS
    job.slurm_job_id = None
    job.array_task_id = None
    job.submitted_at = None
    job.started_at = None
    job.finished_at = None
    job.exit_code = None
    job.error_code = None
    job.error_message = None
    if job.run_id is None:
        job.run_id = run_id
    if job.cycle_id is None:
        job.cycle_id = cycle_id
    if job.model_id is None:
        job.model_id = model_id
    if job.stage is None:
        job.stage = stage
    if job.candidate_id is None:
        job.candidate_id = candidate_id
    job.updated_at = utc_now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def orm_bind_reservation(
    session: Session,
    idempotency_key: str,
    *,
    slurm_job_id: str,
    status: str = "submitted",
    array_task_id: int | None = None,
) -> PipelineJob | None:
    """Phase-2 bind: ``None`` when the key has no row or the row is already bound."""

    job = _row_for_key(session, idempotency_key)
    if job is None or job.slurm_job_id is not None:
        return None
    job.slurm_job_id = slurm_job_id
    job.status = status
    if array_task_id is not None:
        job.array_task_id = array_task_id
    if job.submitted_at is None:
        job.submitted_at = utc_now()
    job.updated_at = utc_now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


class OrmPipelineStore:
    """Store-shaped double: ORM write methods, surviving reads delegated by name."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._kept = PipelineStore(session)

    # The four write methods R5 deleted from PipelineStore, re-implemented above.
    def create_job(self, **kwargs: Any) -> PipelineJob:
        return seed_pipeline_job(self.session, **kwargs)

    def reserve_job(self, **kwargs: Any) -> PipelineJob | None:
        return orm_reserve_job(self.session, **kwargs)

    def reclaim_reservation(self, idempotency_key: str, **kwargs: Any) -> PipelineJob | None:
        return orm_reclaim_reservation(self.session, idempotency_key, **kwargs)

    def bind_reservation(self, idempotency_key: str, **kwargs: Any) -> PipelineJob | None:
        return orm_bind_reservation(self.session, idempotency_key, **kwargs)

    # PipelineStore methods R5 keeps.
    def update_job_status(self, job_id: str, status: str, **kwargs: Any) -> PipelineJob:
        return self._kept.update_job_status(job_id, status, **kwargs)

    def get_job(self, job_id: str) -> PipelineJob | None:
        return self._kept.get_job(job_id)

    def query_jobs_by_cycle(self, cycle_id: str) -> list[PipelineJob]:
        return self._kept.query_jobs_by_cycle(cycle_id)

    def query_jobs_by_run(self, run_id: str) -> list[PipelineJob]:
        return self._kept.query_jobs_by_run(run_id)

    def query_inflight_jobs(self) -> list[PipelineJob]:
        return self._kept.query_inflight_jobs()

    def query_reserved_unbound_jobs(self) -> list[PipelineJob]:
        return self._kept.query_reserved_unbound_jobs()

    def query_candidate_state(self, idempotency_key: str) -> PipelineJob | None:
        return self._kept.query_candidate_state(idempotency_key)

    def insert_event(self, **kwargs: Any) -> PipelineEvent:
        return self._kept.insert_event(**kwargs)
