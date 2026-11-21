from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, MetaData, Text, false, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.common import status_vocab as _vocab
from packages.common.contracts.time_normalization import utc_now

TERMINAL_STATUS_GUARD = set(_vocab.PIPELINE_JOB_TERMINAL_GUARD)
TERMINAL_STATUS_OVERRIDES = set(_vocab.PIPELINE_JOB_TERMINAL_OVERRIDES)
ACTIVE_MANUAL_RETRY_STATUSES = _vocab.PIPELINE_JOB_ACTIVE_MANUAL_RETRY

# M24 §3A two-phase reservation.
RESERVED_STATUS = "reserved"
# A candidate whose idempotency_key already maps to one of these is considered
# already in-flight; an overlapping pass must NOT re-submit it.
RESERVATION_ACTIVE_STATUSES = _vocab.RESERVATION_ACTIVE_STATUSES


class Base(DeclarativeBase):
    metadata = MetaData(schema="ops")


class PipelineJob(Base):
    __tablename__ = "pipeline_job"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    cycle_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    slurm_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    array_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manual_retry_marker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class PipelineEvent(Base):
    __tablename__ = "pipeline_event"

    event_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


Index(
    "pipeline_job_active_manual_retry_guard_idx",
    PipelineJob.run_id,
    unique=True,
    sqlite_where=PipelineJob.manual_retry_marker.is_(True)
    & PipelineJob.run_id.is_not(None)
    & PipelineJob.status.in_(ACTIVE_MANUAL_RETRY_STATUSES),
    postgresql_where=PipelineJob.manual_retry_marker.is_(True)
    & PipelineJob.run_id.is_not(None)
    & PipelineJob.status.in_(ACTIVE_MANUAL_RETRY_STATUSES),
)


# At most one durable pipeline_job row per idempotency_key. Partial so legacy
# NULL rows (pre-reservation) do not collide. Mirrors migration 000029.
Index(
    "pipeline_job_idempotency_key_uidx",
    PipelineJob.idempotency_key,
    unique=True,
    sqlite_where=PipelineJob.idempotency_key.is_not(None),
    postgresql_where=PipelineJob.idempotency_key.is_not(None),
)


class PipelineStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> PipelineJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"pipeline_job not found: {job_id}")

        if job.status == "permanently_failed":
            return job

        if job.status in TERMINAL_STATUS_GUARD and status not in TERMINAL_STATUS_OVERRIDES:
            return job

        job.status = status
        if started_at is not None:
            job.started_at = started_at
        if finished_at is not None:
            job.finished_at = finished_at
        if exit_code is not None:
            job.exit_code = exit_code
        if status in _vocab.PIPELINE_JOB_SUCCESS and error_code is None:
            job.error_code = None
        if status in _vocab.PIPELINE_JOB_SUCCESS and error_message is None:
            job.error_message = None
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if log_uri is not None:
            job.log_uri = log_uri
        job.updated_at = utc_now()
        self.session.commit()
        self.session.refresh(job)
        return job

    def get_job(self, job_id: str) -> PipelineJob | None:
        return self.session.get(PipelineJob, job_id)

    def query_jobs_by_cycle(self, cycle_id: str) -> list[PipelineJob]:
        statement = (
            select(PipelineJob)
            .where(PipelineJob.cycle_id == cycle_id)
            .order_by(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def query_jobs_by_run(self, run_id: str) -> list[PipelineJob]:
        statement = (
            select(PipelineJob)
            .where(PipelineJob.run_id == run_id)
            .order_by(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def query_inflight_jobs(self) -> list[PipelineJob]:
        """Durable in-flight jobs for restart reconcile.

        Returns rows that were submitted/running and carry a ``slurm_job_id``,
        read from the durable ``pipeline_job`` table (never from gateway memory),
        so a restarted scheduler/gateway can recover job identity authoritatively.
        """

        statement = (
            select(PipelineJob)
            .where(
                PipelineJob.status.in_(("submitted", "running")),
                PipelineJob.slurm_job_id.is_not(None),
            )
            .order_by(PipelineJob.submitted_at.asc(), PipelineJob.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def query_reserved_unbound_jobs(self) -> list[PipelineJob]:
        """Reservations stuck in the submit-crash window.

        Rows written ``status='reserved'`` whose ``slurm_job_id`` was never bound
        (crash after sbatch accepted but before the durable bind). Recovery
        reconciles these by ``idempotency_key`` via Slurm accounting comment,
        never by blindly re-submitting.
        """

        statement = (
            select(PipelineJob)
            .where(
                PipelineJob.status == RESERVED_STATUS,
                PipelineJob.slurm_job_id.is_(None),
                PipelineJob.idempotency_key.is_not(None),
            )
            .order_by(PipelineJob.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def query_candidate_state(self, idempotency_key: str) -> PipelineJob | None:
        """The durable reservation/binding row for an idempotency_key, if any.

        Queryable before the pass lock is released so an overlapping pass sees
        ``reserved``/``submitted``/``running`` even in the window before the job
        surfaces in ``squeue``/``sacct``.
        """

        statement = select(PipelineJob).where(PipelineJob.idempotency_key == idempotency_key)
        return self.session.scalars(statement).first()

    def insert_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> PipelineEvent:
        event = PipelineEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            status_from=status_from,
            status_to=status_to,
            message=message,
            details=details or {},
        )
        self.session.add(event)
        if commit:
            self.session.commit()
            self.session.refresh(event)
        else:
            self.session.flush()
        return event
