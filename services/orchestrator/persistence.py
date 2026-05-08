from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

TERMINAL_STATUS_GUARD = {"succeeded", "failed", "cancelled"}
TERMINAL_STATUS_OVERRIDES = {"partially_failed"}


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
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    ) -> PipelineJob:
        job = PipelineJob(
            job_id=job_id,
            run_id=run_id,
            cycle_id=cycle_id,
            job_type=job_type,
            slurm_job_id=slurm_job_id,
            model_id=model_id,
            stage=stage,
            status=status,
            submitted_at=_utcnow(),
        )
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
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

        if job.status in TERMINAL_STATUS_GUARD and status not in TERMINAL_STATUS_OVERRIDES:
            return job

        job.status = status
        if started_at is not None:
            job.started_at = started_at
        if finished_at is not None:
            job.finished_at = finished_at
        if exit_code is not None:
            job.exit_code = exit_code
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
        self.session.commit()
        self.session.refresh(event)
        return event
