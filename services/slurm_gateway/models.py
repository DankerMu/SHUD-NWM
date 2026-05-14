from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SlurmJobStatus(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    SlurmJobStatus.SUCCEEDED,
    SlurmJobStatus.FAILED,
    SlurmJobStatus.CANCELLED,
}


class SubmitJobRequest(BaseModel):
    """Job submission request.

    The gateway accepts either a compact body with run_id/model_id or a manifest-shaped body
    where model_id is nested under model.model_id.
    """

    run_id: str | None = None
    model_id: str | None = None
    job_type: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")

    def resolved_run_id(self) -> str | None:
        if self.run_id:
            return self.run_id
        if self.manifest:
            manifest_run_id = self.manifest.get("run_id")
            if isinstance(manifest_run_id, str) and manifest_run_id:
                return manifest_run_id
        extra_run_id = (self.model_extra or {}).get("run_id")
        if isinstance(extra_run_id, str) and extra_run_id:
            return extra_run_id
        return None

    def resolved_model_id(self) -> str | None:
        if self.model_id:
            return self.model_id

        candidates: list[Any] = []
        if self.manifest:
            candidates.append(self.manifest.get("model_id"))
            manifest_model = self.manifest.get("model")
            if isinstance(manifest_model, dict):
                candidates.append(manifest_model.get("model_id"))

        extra = self.model_extra or {}
        candidates.append(extra.get("model_id"))
        extra_model = extra.get("model")
        if isinstance(extra_model, dict):
            candidates.append(extra_model.get("model_id"))

        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    def resolved_job_type(self) -> str | None:
        if self.job_type:
            return self.job_type
        if self.manifest:
            manifest_job_type = self.manifest.get("job_type")
            if isinstance(manifest_job_type, str) and manifest_job_type:
                return manifest_job_type
            manifest_stage = self.manifest.get("stage_name") or self.manifest.get("stage")
            if isinstance(manifest_stage, str) and manifest_stage:
                return manifest_stage
        extra_job_type = (self.model_extra or {}).get("job_type")
        if isinstance(extra_job_type, str) and extra_job_type:
            return extra_job_type
        return None

    def normalized_manifest(self) -> dict[str, Any]:
        if self.manifest:
            payload = dict(self.manifest)
        else:
            payload = dict(self.model_extra or {})

        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.job_type is not None:
            payload["job_type"] = self.job_type
        return payload


class ArraySubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_type: str
    cycle_id: str
    stage_name: str | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)


class SlurmJobRecord(BaseModel):
    job_id: str
    run_id: str
    model_id: str
    status: SlurmJobStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    exit_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)


class SlurmLogsResponse(BaseModel):
    job_id: str
    run_id: str
    logs: str
    complete: bool
    truncated: bool = False
    metadata_complete: bool = True
    array_task_logs: list[dict[str, Any]] | None = None


class SlurmHealthResponse(BaseModel):
    backend: str
    version: str
    status: str
    error: str | None = None


class ResetRequest(BaseModel):
    restore_defaults: bool = False
    delay_to_running_seconds: float | None = Field(default=None, ge=0)
    delay_to_succeeded_seconds: float | None = Field(default=None, ge=0)
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    failure_seed: int | None = None
    force_fail_run_ids: list[str] | None = None


class ResetResponse(BaseModel):
    status: str
    cleared: int
    next_job_id: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    request_id: str
    status: str = "error"
    error: ErrorBody
