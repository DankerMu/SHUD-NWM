from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta, timezone

from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import SlurmGateway, SlurmGatewayError
from services.slurm_gateway.models import (
    TERMINAL_STATUSES,
    ArraySubmitJobRequest,
    ResetRequest,
    ResetResponse,
    SlurmHealthResponse,
    SlurmJobRecord,
    SlurmJobStatus,
    SlurmLogsResponse,
    SubmitJobRequest,
)


class MockSlurmGateway(SlurmGateway):
    def __init__(self, settings: SlurmGatewaySettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, SlurmJobRecord] = {}
        self._next_job_number = 1000
        self._rng = random.Random(settings.failure_seed)

    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        run_id = request.resolved_run_id()
        model_id = request.resolved_model_id()
        missing_fields = []
        if not run_id:
            missing_fields.append("run_id")
        if not model_id:
            missing_fields.append("model_id")
        if missing_fields:
            raise SlurmGatewayError(
                422,
                "INVALID_MANIFEST",
                "Job manifest is missing required fields.",
                {"missing_fields": missing_fields},
            )

        now = self._now()
        with self._lock:
            self._refresh_all_locked(now)
            for job in self._jobs.values():
                if job.run_id == run_id and job.status not in TERMINAL_STATUSES:
                    raise SlurmGatewayError(
                        409,
                        "DUPLICATE_RUN",
                        f"An active job already exists for run_id {run_id}.",
                        {"run_id": run_id, "job_id": job.job_id, "status": job.status.value},
                    )

            self._next_job_number += 1
            job_id = f"mock_{self._next_job_number}"
            job = SlurmJobRecord(
                job_id=job_id,
                run_id=run_id,
                model_id=model_id,
                status=SlurmJobStatus.SUBMITTED,
                submitted_at=now,
                updated_at=now,
                manifest=request.normalized_manifest(),
            )
            self._jobs[job_id] = job
            self._refresh_job_locked(job, now)
            return job.model_copy(deep=True)

    def submit_job_array(self, request: ArraySubmitJobRequest | dict | SubmitJobRequest) -> SlurmJobRecord:
        if isinstance(request, SubmitJobRequest):
            manifest = request.normalized_manifest()
        elif isinstance(request, ArraySubmitJobRequest):
            manifest = dict(request.manifest)
            manifest["job_type"] = request.job_type
            manifest["cycle_id"] = request.cycle_id
            if request.stage_name is not None:
                manifest["stage_name"] = request.stage_name
            manifest["tasks"] = request.tasks
        else:
            manifest = dict(request.get("manifest") or request)
            if "job_type" in request:
                manifest["job_type"] = request["job_type"]
            if "stage_name" in request:
                manifest["stage_name"] = request["stage_name"]
            if "tasks" in request:
                manifest["tasks"] = request["tasks"]

        tasks = list(manifest.get("tasks") or manifest.get("basins") or [])
        if not tasks:
            raise SlurmGatewayError(
                422,
                "VALIDATION_ERROR",
                "Cannot submit array job with 0 tasks",
                {"missing_fields": ["tasks"]},
            )
        first_task = dict(tasks[0])
        run_id = str(manifest.get("run_id") or first_task.get("run_id") or "")
        model_id = str(manifest.get("model_id") or first_task.get("model_id") or "")
        return self.submit_job(
            SubmitJobRequest(
                run_id=run_id,
                model_id=model_id,
                job_type=str(manifest.get("job_type") or manifest.get("stage_name") or "array"),
                manifest=manifest,
            )
        )

    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        now = self._now()
        with self._lock:
            job = self._get_job_locked(job_id, now)
            if job.status in TERMINAL_STATUSES:
                raise SlurmGatewayError(
                    409,
                    "JOB_ALREADY_TERMINAL",
                    f"Job {job_id} is already terminal with status {job.status.value}.",
                    {"job_id": job_id, "status": job.status.value},
                )
            job.status = SlurmJobStatus.CANCELLED
            job.finished_at = now
            job.updated_at = now
            job.exit_code = -1
            return job.model_copy(deep=True)

    def get_job_status(self, job_id: str) -> SlurmJobRecord:
        now = self._now()
        with self._lock:
            return self._get_job_locked(job_id, now).model_copy(deep=True)

    def get_array_task_results(self, job_id: str) -> list[dict[str, int | str | None]]:
        now = self._now()
        with self._lock:
            job = self._get_job_locked(job_id, now)
            tasks = list(job.manifest.get("tasks") or job.manifest.get("basins") or [])
            exit_code = job.exit_code
            return [
                {
                    "task_id": index,
                    "job_id": f"{job_id}_{index}",
                    "status": job.status.value,
                    "exit_code": exit_code,
                }
                for index, _task in enumerate(tasks)
            ]

    def list_jobs(
        self,
        limit: int,
        offset: int,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[SlurmJobRecord]:
        now = self._now()
        with self._lock:
            self._refresh_all_locked(now)
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: (job.submitted_at, job.job_id),
                reverse=True,
            )
            return [job.model_copy(deep=True) for job in jobs[offset : offset + limit]]

    def fetch_logs(self, job_id: str) -> SlurmLogsResponse:
        now = self._now()
        with self._lock:
            job = self._get_job_locked(job_id, now)
            complete = job.status in TERMINAL_STATUSES
            return SlurmLogsResponse(
                job_id=job.job_id,
                run_id=job.run_id,
                logs=self._build_logs(job),
                complete=complete,
            )

    def reset(self, request: ResetRequest | None = None) -> ResetResponse:
        request = request or ResetRequest()
        with self._lock:
            cleared = len(self._jobs)
            if request.restore_defaults:
                self.settings = SlurmGatewaySettings()
            if request.delay_to_running_seconds is not None:
                self.settings.delay_to_running_seconds = request.delay_to_running_seconds
            if request.delay_to_succeeded_seconds is not None:
                self.settings.delay_to_succeeded_seconds = request.delay_to_succeeded_seconds
            if request.failure_rate is not None:
                self.settings.failure_rate = request.failure_rate
            if request.failure_seed is not None:
                self.settings.failure_seed = request.failure_seed
            if request.force_fail_run_ids is not None:
                self.settings.force_fail_run_ids = request.force_fail_run_ids

            self._jobs.clear()
            self._next_job_number = 1000
            self._rng = random.Random(self.settings.failure_seed)
            return ResetResponse(status="ok", cleared=cleared, next_job_id="mock_1001")

    def health(self) -> SlurmHealthResponse:
        return SlurmHealthResponse(backend="mock", version=self.settings.version, status="ok")

    def _get_job_locked(self, job_id: str, now: datetime) -> SlurmJobRecord:
        job = self._jobs.get(job_id)
        if job is None:
            raise SlurmGatewayError(
                404,
                "JOB_NOT_FOUND",
                f"Job {job_id} was not found.",
                {"job_id": job_id},
            )
        self._refresh_job_locked(job, now)
        return job

    def _refresh_all_locked(self, now: datetime) -> None:
        for job in self._jobs.values():
            self._refresh_job_locked(job, now)

    def _refresh_job_locked(self, job: SlurmJobRecord, now: datetime) -> None:
        if job.status in TERMINAL_STATUSES:
            return

        running_at = job.submitted_at + timedelta(seconds=self.settings.delay_to_running_seconds)
        finished_at = running_at + timedelta(seconds=self.settings.delay_to_succeeded_seconds)

        if job.status == SlurmJobStatus.SUBMITTED and now >= running_at:
            job.status = SlurmJobStatus.RUNNING
            job.started_at = running_at
            job.updated_at = running_at

        if job.status == SlurmJobStatus.RUNNING and now >= finished_at:
            forced_failure = job.run_id in set(self.settings.force_fail_run_ids)
            simulated_failure = False if forced_failure else self._rng.random() < self.settings.failure_rate
            job.finished_at = finished_at
            job.updated_at = finished_at
            if forced_failure or simulated_failure:
                job.status = SlurmJobStatus.FAILED
                job.exit_code = 1
                job.error_code = "FORCED_FAILURE" if forced_failure else "SIMULATED_FAILURE"
                job.error_message = "Mock Slurm gateway simulated a job failure."
            else:
                job.status = SlurmJobStatus.SUCCEEDED
                job.exit_code = 0

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _format_dt(value: datetime | None) -> str:
        return value.isoformat() if value else "not reached"

    def _build_logs(self, job: SlurmJobRecord) -> str:
        lines = [
            f"NHMS mock Slurm job {job.job_id} for run {job.run_id}",
            f"submitted_at={self._format_dt(job.submitted_at)}",
        ]
        if job.started_at:
            lines.append(f"started_at={self._format_dt(job.started_at)}")
            lines.append("Executing SHUD model (mock)")
        elif job.status == SlurmJobStatus.SUBMITTED:
            lines.append("Waiting in mock Slurm queue")

        if job.status == SlurmJobStatus.SUCCEEDED:
            lines.append("Job completed successfully with exit code 0")
        elif job.status == SlurmJobStatus.FAILED:
            lines.append(f"ERROR {job.error_code}: {job.error_message}")
            lines.append("Job failed with exit code 1")
        elif job.status == SlurmJobStatus.CANCELLED:
            lines.append("Job cancelled by request with exit code -1")
        else:
            lines.append(f"Current state: {job.status.value}")
        return "\n".join(lines)
