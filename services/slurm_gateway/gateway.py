from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.models import (
    ArraySubmitJobRequest,
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


class SlurmParseError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(502, "SLURM_PARSE_ERROR", message, details)


class SlurmTimeoutError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(504, "SLURM_TIMEOUT", message, details)


class SlurmCommandError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(502, "SLURM_COMMAND_ERROR", message, details)


class SlurmJobNotFoundError(SlurmGatewayError):
    def __init__(self, job_id: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(404, "JOB_NOT_FOUND", f"Job {job_id} was not found.", details or {"job_id": job_id})


class TemplateSecurityError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(400, "TEMPLATE_SECURITY_ERROR", message, details)


class TemplateNotFoundError(SlurmGatewayError):
    def __init__(self, job_type: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            404,
            "TEMPLATE_NOT_FOUND",
            f"No sbatch template is available for job_type {job_type}.",
            details or {"job_type": job_type},
        )


class ManifestValidationError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(422, "MANIFEST_VALIDATION_ERROR", message, details)


class ConfigurationError(SlurmGatewayError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(500, "CONFIGURATION_ERROR", message, details)


class SlurmValidationError(SlurmGatewayError, ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(422, "VALIDATION_ERROR", message, details)


ValidationError = SlurmValidationError


class SlurmGateway(ABC):
    @abstractmethod
    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def submit_job_array(self, request: ArraySubmitJobRequest | SubmitJobRequest | dict[str, Any]) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def get_job_status(self, job_id: str) -> SlurmJobRecord:
        raise NotImplementedError

    @abstractmethod
    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(
        self,
        limit: int,
        offset: int,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[SlurmJobRecord]:
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
        from services.slurm_gateway.real_backend import RealSlurmGateway

        return RealSlurmGateway(settings)
    raise ValueError(f"Unsupported Slurm gateway backend: {settings.backend}")
