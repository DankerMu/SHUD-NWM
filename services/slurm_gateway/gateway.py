from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.models import (
    ResetRequest,
    ResetResponse,
    SlurmHealthResponse,
    SlurmJobRecord,
    SlurmLogsResponse,
    SubmitJobRequest,
)


class SlurmGatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class SlurmGateway(ABC):
    @abstractmethod
    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def get_job_status(self, job_id: str) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(self, limit: int, offset: int) -> list[SlurmJobRecord]:
        raise NotImplementedError

    @abstractmethod
    def fetch_logs(self, job_id: str) -> SlurmLogsResponse:
        raise NotImplementedError

    @abstractmethod
    def reset(self, request: ResetRequest | None = None) -> ResetResponse:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> SlurmHealthResponse:
        raise NotImplementedError


def create_gateway(settings: SlurmGatewaySettings | None = None) -> SlurmGateway:
    settings = settings or get_settings()
    if settings.backend == "mock":
        from services.slurm_gateway.mock_backend import MockSlurmGateway

        return MockSlurmGateway(settings)
    if settings.backend == "slurm":
        raise NotImplementedError("Real Slurm backend is not implemented; available in M3")
    raise ValueError(f"Unsupported Slurm gateway backend: {settings.backend}")

