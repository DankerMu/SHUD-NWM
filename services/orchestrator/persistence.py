from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, MetaData, Text, false, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

TERMINAL_STATUS_GUARD = {"succeeded", "failed", "cancelled", "permanently_failed"}
TERMINAL_STATUS_OVERRIDES = {"partially_failed", "permanently_failed"}
ACTIVE_MANUAL_RETRY_STATUSES = ("pending", "queued", "submitted", "running")

# M24 §3A two-phase reservation.
RESERVED_STATUS = "reserved"
# A candidate whose idempotency_key already maps to one of these is considered
# already in-flight; an overlapping pass must NOT re-submit it.
RESERVATION_ACTIVE_STATUSES = ("reserved", "submitted", "running", "queued", "pending")


# Sentinel so create_job can distinguish "caller wants NULL submitted_at" (a
# reservation) from "caller omitted it" (stamp now()).
_UNSET_SUBMITTED_AT = object()


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
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
        default=_utcnow,
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

    def create_job(
        self,
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
        # Default to now() for ordinary creates; reservations pass
        # ``submitted_at=None`` so the column stays NULL until phase-2 bind.
        if submitted_at is _UNSET_SUBMITTED_AT:
            submitted_at = _utcnow()
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
        self.session.add(job)
        if commit:
            self.session.commit()
            self.session.refresh(job)
        else:
            self.session.flush()
        return job

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
        if status in {"succeeded", "complete", "published"} and error_code is None:
            job.error_code = None
        if status in {"succeeded", "complete", "published"} and error_message is None:
            job.error_message = None
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if log_uri is not None:
            job.log_uri = log_uri
        job.updated_at = _utcnow()
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

    def query_jobs_by_slurm_id(self, slurm_job_id: str) -> PipelineJob | None:
        statement = select(PipelineJob).where(PipelineJob.slurm_job_id == slurm_job_id)
        return self.session.scalars(statement).first()

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

    def bind_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> PipelineJob | None:
        """Bind slurm_job_id onto a reservation; no-op if already bound.

        Read-then-write across two statements, NOT a single atomic conditional
        UPDATE, so it is not concurrency-safe on its own. Only the
        single-threaded reconcile session and tests call this; the production
        submit path uses the psycopg-level atomic conditional UPDATE.
        """

        job = self.query_candidate_state(idempotency_key)
        if job is None or job.slurm_job_id is not None:
            return None
        job.slurm_job_id = slurm_job_id
        job.status = status
        if array_task_id is not None:
            job.array_task_id = array_task_id
        if job.submitted_at is None:
            job.submitted_at = _utcnow()
        job.updated_at = _utcnow()
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def reserve_job(
        self,
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
        """Phase-1 reserve guarded by the unique idempotency_key index.

        Returns the new row when THIS call inserted it (won), or ``None`` when a
        row already existed under the same idempotency_key (lost). The partial
        unique index is the race backstop: a concurrent loser's INSERT raises
        ``IntegrityError`` which we translate to ``None`` so exactly one caller
        ever sees ``created=True``. ``submitted_at`` stays NULL until bind.
        """

        from sqlalchemy.exc import IntegrityError

        try:
            with self.session.begin_nested():
                job = self.create_job(
                    job_id=job_id,
                    run_id=run_id,
                    cycle_id=cycle_id,
                    job_type=job_type,
                    slurm_job_id=None,
                    model_id=model_id,
                    stage=stage,
                    status=status,
                    idempotency_key=idempotency_key,
                    candidate_id=candidate_id,
                    submitted_at=None,
                    commit=False,
                )
        except IntegrityError:
            return None
        self.session.commit()
        self.session.refresh(job)
        return job

    def reclaim_reservation(
        self,
        idempotency_key: str,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        cycle_id: str | None = None,
        model_id: str | None = None,
        stage: str | None = None,
        candidate_id: str | None = None,
    ) -> PipelineJob | None:
        """Take over a DEAD reservation back to ``reserved``.

        Read-then-write across two statements (NOT a single atomic conditional
        UPDATE), so it is not concurrency-safe on its own; only the
        single-threaded reconcile session and tests call this, while the
        production submit path uses the psycopg-level atomic conditional UPDATE.
        Only a row that is dead (``slurm_job_id IS NULL`` and ``status IN
        ('submission_failed', 'reservation_lost')``) is re-claimed. A live row
        (reserved/submitted/running) never matches, so a take-over can never
        steal an in-flight candidate. Identity columns are filled only when
        previously NULL.
        """

        job = self.query_candidate_state(idempotency_key)
        if job is not None:
            if job.slurm_job_id is not None or job.status not in ("submission_failed", "reservation_lost"):
                return None
        else:
            if job_id in (None, ""):
                return None
            statement = select(PipelineJob).where(PipelineJob.job_id == job_id)
            job = self.session.scalars(statement).first()
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
        job.updated_at = _utcnow()
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

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
