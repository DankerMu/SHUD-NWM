from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from errno import EACCES, EEXIST, EISDIR, ELOOP, ENOTDIR, EPERM
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.redaction import redact_payload
from packages.common.slurm_env import (
    iter_secret_manifest_findings,
    reserved_slurm_env_reason,
    secret_bearing_url_reason,
    secret_manifest_key_reason,
    secret_manifest_value_reason,
)
from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain import ForecastOrchestrator, OrchestratorConfig, PipelineResult, scenario_for_source
from services.orchestrator.production_contract import (
    PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
    PRODUCTION_IDENTITY_FIELDS,
    ProductionContractError,
    production_contract_matrix,
    production_identity_contract_evidence,
    production_stage_for,
    production_status_for,
    validate_compatible_production_identity,
)
from services.orchestrator.retry import classify_failure
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from services.slurm_gateway.gateway import ConfigurationError
from services.slurm_gateway.resource_validation import ResourceProfileValidationError, validate_resource_profile
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time

DEFAULT_PRODUCTION_SOURCES = ("gfs", "IFS")
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_CYCLE_LAG_HOURS = 0
DEFAULT_MAX_CYCLES_PER_SOURCE = 1
DEFAULT_LOCK_TTL_SECONDS = 3600
MAX_LOOKBACK_HOURS = 168
MAX_SOURCES = 4
MAX_CYCLES_PER_SOURCE = 16
MAX_DISCOVERED_MODELS = 1000
MAX_DISCOVERED_CYCLES = 10000
MAX_CANDIDATES = 10000
MAX_REGISTRY_PAGES = 20
MAX_EVIDENCE_BYTES = 5_000_000
MAX_LOCK_PAYLOAD_BYTES = 16_384
MAX_CONTINUOUS_JSON_PASSES = 100
MAX_MODEL_RUN_STAGE_TASK_ROWS = 16
CANDIDATE_STATE_TASK_RESULT_LIMIT = MAX_MODEL_RUN_STAGE_TASK_ROWS
MAX_SLURM_ENV_VALUE_LENGTH = 1024
DEFAULT_RETRY_LIMIT = 3
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
STATE_M23_COMPARISON_FIELDS = (
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "canonical_product_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
STATE_CANDIDATE_SCOPED_PROOF_FIELDS = (
    "run_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
LOCK_OWNER = "production_scheduler"
LOCK_SCHEMA_VERSION = 1
SCHEDULER_EVIDENCE_SCHEMA_VERSION = "nhms.production_scheduler.pass_evidence.v1"
MODEL_RUN_EVIDENCE_SCHEMA_VERSION = "nhms.production_scheduler.model_run_evidence.v1"
SCHEDULER_EVIDENCE_CONTRACT_ID = "runtime-evidence-and-operations.scheduler-evidence.v1"
SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE = "m20-production-multibasin-continuous-automation"
SCHEDULER_EVIDENCE_GITHUB_ISSUE = 196
SLURM_ARRAY_STAGE_NAMES = {"forcing", "forecast", "parse", "frequency"}
SAFE_SLURM_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_SLURM_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
LOCALHOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "::",
}
DATABASE_HOST_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS = {
    "partition",
    "account",
    "nodes",
    "ntasks",
    "cpus_per_task",
    "memory_gb",
    "walltime",
    "max_concurrent",
    "shud_threads",
}
SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS = {
    "run_id",
    "workspace_dir",
    "stage_name",
    "cycle_id",
    "object_store_root",
    "object_store_prefix",
    "manifest_index_path",
}
TASK_RESULT_CANDIDATE_IDENTITY_FIELDS = ("candidate_id", "run_id", "forcing_version_id", "model_id")
TASK_RESULT_INDEX_IDENTITY_FIELDS = ("task_id", "array_task_id", "original_task_id")
ACTIVE_PIPELINE_STATUSES = {"pending", "queued", "submitted", "running"}
ACTIVE_HYDRO_STATUSES = {"created", "staged", "pending", "submitted", "running"}
DURABLE_HYDRO_SUCCESS_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "permanently_failed",
}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
DOWNSTREAM_RESTART_STAGES = ("parse", "frequency", "publish")
DOWNSTREAM_STAGE_ALIASES = {
    "parse": "parse",
    "parse_output": "parse",
    "frequency": "frequency",
    "compute_frequency": "frequency",
    "publish": "publish",
    "publish_tiles": "publish",
}
NATIVE_SHUD_STAGE_ALIASES = {"forecast", "run_shud_forecast", "forecast_run", "analysis_run"}
PIPELINE_TERMINAL_SUCCESS_STAGES = {"parse", "frequency", "publish", "parse_output", "publish_tiles"}
TRANSIENT_RETRY_REASON_CODES = {
    "SLURM_TIMEOUT",
    "SLURM_JOB_TIMEOUT",
    "NODE_FAILURE",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "STORAGE_WRITE_FAILED",
    "SBATCH_SUBMISSION_FAILED",
    "SLURM_UNAVAILABLE",
    "SOURCE_CYCLE_UNAVAILABLE",
    "SOURCE_UNAVAILABLE",
    "ADAPTER_UNAVAILABLE",
}
UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"


class SchedulerResourceLimitError(ValueError):
    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


class UnsafeSchedulerLockError(OSError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SchedulerEvidenceWriteError(OSError):
    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


class ModelRegistryReader(Protocol):
    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        raise NotImplementedError


class CycleDiscoveryAdapter(Protocol):
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class ActiveCandidateRepository(Protocol):
    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        raise NotImplementedError

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> Mapping[str, Any] | None:
        raise NotImplementedError


class ProductionOrchestratorFactory(Protocol):
    def __call__(self, source_id: str) -> ForecastOrchestrator:
        raise NotImplementedError


@dataclass(frozen=True)
class ProductionSchedulerConfig:
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str | None = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT"))
    published_artifact_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT")
    )
    log_root: Path | str | None = field(
        default_factory=lambda: os.getenv("SLURM_SHARED_LOG_ROOT") or os.getenv("LOG_ROOT")
    )
    runtime_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_RUNTIME_ROOT")
        or os.getenv("NHMS_RUNTIME_ROOT")
        or os.getenv("RUN_WORKSPACE_ROOT")
        or os.getenv("SHUD_RUNTIME_ROOT")
    )
    temp_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_TEMP_ROOT")
        or os.getenv("NHMS_TEMP_ROOT")
        or os.getenv("TMPDIR")
    )
    scheduler_lock_root: Path | str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_ROOT"))
    scheduler_evidence_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_EVIDENCE_ROOT")
    )
    service_role: str | None = field(default_factory=lambda: os.getenv("NHMS_SERVICE_ROLE"))
    require_runtime_roots: bool = field(default_factory=lambda: _env_flag("NHMS_SCHEDULER_REQUIRE_ROOTS"))
    database_url: str | None = field(
        default_factory=lambda: os.getenv("DATABASE_URL")
    )
    slurm_execution_enabled: bool = field(
        default_factory=lambda: _env_flag("NHMS_PRODUCTION_SLURM_ENABLED")
        or _env_flag("SLURM_EXECUTION_ENABLED")
    )
    allowed_storage_roots: tuple[Path | str, ...] = field(
        default_factory=lambda: _env_path_list("NHMS_SCHEDULER_ALLOWED_ROOTS")
    )
    slurm_job_type_templates: Mapping[str, str] | None = None
    slurm_env: Mapping[str, str] = field(default_factory=dict)
    cancel_active_slurm: bool = False
    sources: tuple[str, ...] = DEFAULT_PRODUCTION_SOURCES
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    cycle_lag_hours: int = DEFAULT_CYCLE_LAG_HOURS
    max_cycles_per_source: int = DEFAULT_MAX_CYCLES_PER_SOURCE
    model_ids: tuple[str, ...] = ()
    basin_ids: tuple[str, ...] = ()
    dry_run: bool = True
    continuous: bool = False
    interval_seconds: float = 300.0
    retry_limit: int = field(default_factory=lambda: _env_int("NHMS_SCHEDULER_RETRY_LIMIT", DEFAULT_RETRY_LIMIT))
    candidate_state_job_limit: int = field(
        default_factory=lambda: _env_int("NHMS_CANDIDATE_STATE_JOB_LIMIT", DEFAULT_CANDIDATE_STATE_JOB_LIMIT)
    )
    candidate_state_event_limit: int = field(
        default_factory=lambda: _env_int("NHMS_CANDIDATE_STATE_EVENT_LIMIT", DEFAULT_CANDIDATE_STATE_EVENT_LIMIT)
    )
    lock_path: Path | str | None = None
    evidence_dir: Path | str | None = None
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
    now: datetime | None = None
    source_exclusions: tuple[dict[str, Any], ...] = field(init=False, default=())
    _workspace_root_preflight_path: Path = field(init=False, repr=False, compare=False)
    _object_store_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _published_artifact_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _runtime_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _temp_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _lock_root_preflight_path: Path = field(init=False, repr=False, compare=False)
    _evidence_root_preflight_path: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _reject_blank_config_path(self.workspace_root, "workspace_root")
        _reject_blank_config_path(self.lock_path, "lock_path")
        _reject_blank_config_path(self.evidence_dir, "evidence_dir")
        workspace_root_preflight_path = _config_path_preserve_final_component(self.workspace_root)
        workspace_root = workspace_root_preflight_path.resolve()
        object.__setattr__(self, "_workspace_root_preflight_path", workspace_root_preflight_path)
        object.__setattr__(self, "workspace_root", workspace_root)
        object_store_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.object_store_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "object_store_root",
            _resolve_optional_config_path(object_store_root_preflight_path),
        )
        object.__setattr__(
            self,
            "_object_store_root_preflight_path",
            object_store_root_preflight_path,
        )
        published_artifact_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.published_artifact_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "published_artifact_root",
            _resolve_optional_config_path(published_artifact_root_preflight_path),
        )
        object.__setattr__(
            self,
            "_published_artifact_root_preflight_path",
            published_artifact_root_preflight_path,
        )
        log_root_preflight_path = _optional_config_path_relative_to_preserve_final(self.log_root, workspace_root)
        object.__setattr__(self, "log_root", _resolve_optional_config_path(log_root_preflight_path))
        runtime_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.runtime_root,
            workspace_root,
        )
        object.__setattr__(self, "runtime_root", _resolve_optional_config_path(runtime_root_preflight_path))
        object.__setattr__(self, "_runtime_root_preflight_path", runtime_root_preflight_path)
        temp_root_preflight_path = _optional_config_path_relative_to_preserve_final(self.temp_root, workspace_root)
        object.__setattr__(self, "temp_root", _resolve_optional_config_path(temp_root_preflight_path))
        object.__setattr__(self, "_temp_root_preflight_path", temp_root_preflight_path)
        scheduler_lock_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.scheduler_lock_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_lock_root",
            _resolve_optional_config_path(scheduler_lock_root_preflight_path),
        )
        scheduler_evidence_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.scheduler_evidence_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_evidence_root",
            _resolve_optional_config_path(scheduler_evidence_root_preflight_path),
        )
        object.__setattr__(self, "service_role", str(self.service_role).strip() if self.service_role else None)
        object.__setattr__(self, "database_url", str(self.database_url).strip() if self.database_url else None)
        allowed_roots = tuple(_optional_config_path(root) for root in self.allowed_storage_roots if root)
        object.__setattr__(self, "allowed_storage_roots", allowed_roots)
        templates = dict(self.slurm_job_type_templates or DEFAULT_JOB_TYPE_TEMPLATES)
        object.__setattr__(self, "slurm_job_type_templates", templates)
        object.__setattr__(self, "slurm_env", {str(key): str(value) for key, value in dict(self.slurm_env).items()})
        if len(self.sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        sources, source_exclusions = _normalize_sources(self.sources)
        if len(sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "source_exclusions", tuple(source_exclusions))
        lookback_hours = max(int(self.lookback_hours), 0)
        if lookback_hours > MAX_LOOKBACK_HOURS:
            raise ValueError(f"production scheduler lookback_hours exceeds limit {MAX_LOOKBACK_HOURS}")
        object.__setattr__(self, "lookback_hours", lookback_hours)
        object.__setattr__(self, "cycle_lag_hours", max(int(self.cycle_lag_hours), 0))
        max_cycles_per_source = int(self.max_cycles_per_source)
        if max_cycles_per_source < 1:
            raise ValueError("production scheduler max_cycles_per_source must be at least 1")
        if max_cycles_per_source > MAX_CYCLES_PER_SOURCE:
            raise ValueError(f"production scheduler max_cycles_per_source exceeds limit {MAX_CYCLES_PER_SOURCE}")
        object.__setattr__(self, "max_cycles_per_source", max_cycles_per_source)
        object.__setattr__(self, "model_ids", tuple(str(model_id) for model_id in self.model_ids if model_id))
        object.__setattr__(self, "basin_ids", tuple(str(basin_id) for basin_id in self.basin_ids if basin_id))
        object.__setattr__(self, "interval_seconds", max(float(self.interval_seconds), 1.0))
        object.__setattr__(self, "retry_limit", max(int(self.retry_limit), 0))
        object.__setattr__(self, "candidate_state_job_limit", max(int(self.candidate_state_job_limit), 1))
        object.__setattr__(self, "candidate_state_event_limit", max(int(self.candidate_state_event_limit), 1))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        if self.lock_path is None:
            lock_root = (
                Path(self.scheduler_lock_root)
                if self.scheduler_lock_root is not None
                else workspace_root / "scheduler"
            )
            lock_root_preflight_path = (
                scheduler_lock_root_preflight_path
                if scheduler_lock_root_preflight_path is not None
                else workspace_root / "scheduler"
            )
            lock_path = _confined_path(
                lock_root / "production-scheduler.lock",
                workspace_root,
                "lock_path",
            )
            object.__setattr__(self, "_lock_root_preflight_path", lock_root_preflight_path)
            object.__setattr__(self, "lock_path", lock_path)
        else:
            lock_path_preflight_path = _config_path_relative_to_preserve_final(self.lock_path, workspace_root)
            lock_path = _confined_path(self.lock_path, workspace_root, "lock_path")
            _require_under_workspace(lock_path, workspace_root, "lock_path")
            object.__setattr__(self, "_lock_root_preflight_path", lock_path_preflight_path.parent)
            object.__setattr__(self, "lock_path", lock_path)
        if self.evidence_dir is None:
            evidence_root = (
                Path(self.scheduler_evidence_root)
                if self.scheduler_evidence_root is not None
                else workspace_root / "scheduler" / "evidence"
            )
            evidence_root_preflight_path = (
                scheduler_evidence_root_preflight_path
                if scheduler_evidence_root_preflight_path is not None
                else workspace_root / "scheduler" / "evidence"
            )
            evidence_dir = _confined_path(
                evidence_root,
                workspace_root,
                "evidence_dir",
            )
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_root_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        else:
            evidence_dir_preflight_path = _config_path_relative_to_preserve_final(self.evidence_dir, workspace_root)
            evidence_dir = _confined_path(self.evidence_dir, workspace_root, "evidence_dir")
            _require_under_workspace(evidence_dir, workspace_root, "evidence_dir")
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_dir_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        if self.now is not None:
            object.__setattr__(self, "now", _ensure_utc(self.now))


@dataclass(frozen=True)
class SchedulerPassResult:
    pass_id: str
    status: str
    evidence: dict[str, Any]
    artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.evidence)
        if self.artifact_path is not None:
            payload.setdefault("artifact_path", str(self.artifact_path))
        return payload


@dataclass(frozen=True)
class RegisteredSchedulerModel:
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    model_package_uri: str
    shud_code_version: str
    resource_profile: Mapping[str, Any]
    resource_profile_summary: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "model_package_uri": _redact_secret_manifest_for_evidence(self.model_package_uri, "model_package_uri"),
            "shud_code_version": self.shud_code_version,
            "resource_profile": _resource_profile_evidence(self.resource_profile_summary),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
        }


@dataclass(frozen=True)
class SchedulerCandidate:
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    model_package_uri: str
    resource_profile: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]
    horizon: Mapping[str, Any]
    scenario_id: str
    run_id: str
    forcing_version_id: str
    status: str
    reason: str | None = None
    state_evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        contract_identity = _candidate_production_identity(self)
        payload = {
            "production_identity_contract": production_identity_contract_evidence(contract_identity),
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "source": self.source_id,
            "cycle_id": self.cycle_id,
            "cycle_time_utc": _format_utc(self.cycle_time_utc),
            "cycle_time": _format_utc(self.cycle_time_utc),
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "model_package_uri": _redact_secret_manifest_for_evidence(self.model_package_uri, "model_package_uri"),
            "resource_profile": _resource_profile_evidence(self.resource_profile),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
            "horizon": dict(self.horizon),
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "canonical_product_id": contract_identity["canonical_product_id"],
            "forcing_version_id": self.forcing_version_id,
            "hydro_run_id": contract_identity["hydro_run_id"],
            "published_manifest_id": contract_identity["published_manifest_id"],
            "status": self.status,
            "reason": self.reason,
        }
        if contract_identity.get("pipeline_job_id") not in (None, ""):
            payload["pipeline_job_id"] = contract_identity["pipeline_job_id"]
        if self.state_evidence:
            payload["state_evidence"] = _evidence_safe(self.state_evidence)
        return payload


@dataclass(frozen=True)
class CandidateStateDecision:
    action: str
    reason: str | None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerSourceCycle:
    discovery: CycleDiscovery
    horizon: Mapping[str, Any]


class _BlockedModelRegistry:
    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        del basin_version_id, active, limit, offset
        raise RuntimeError("blocked scheduler root preflight must not query model registry")

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        del model_id
        raise RuntimeError("blocked scheduler root preflight must not query model registry")


class ProductionScheduler:
    def __init__(
        self,
        config: ProductionSchedulerConfig | None = None,
        *,
        registry: ModelRegistryReader | None = None,
        adapters: Mapping[str, CycleDiscoveryAdapter] | None = None,
        active_repository: ActiveCandidateRepository | None = None,
        orchestrator_factory: ProductionOrchestratorFactory | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or ProductionSchedulerConfig()
        self.registry = registry if registry is not None else PsycopgModelRegistryStore.from_env()
        self.adapters = dict(adapters if adapters is not None else _default_adapters())
        self.active_repository = active_repository
        self.orchestrator_factory = orchestrator_factory
        self.sleep = sleep or _sleep

    @classmethod
    def from_env(cls, config: ProductionSchedulerConfig | None = None) -> ProductionScheduler:
        config = config or ProductionSchedulerConfig()
        if config.require_runtime_roots and _scheduler_lock_evidence_root_preflight(config)["status"] == "blocked":
            return cls(config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None)
        if config.require_runtime_roots and _scheduler_runtime_root_preflight(config)["status"] == "blocked":
            return cls(config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None)
        return cls(
            config=config,
            active_repository=_active_repository_from_env(),
        )

    def run_once(self) -> SchedulerPassResult:
        started_at = _now(self.config)
        pass_id = f"scheduler_{format_cycle_time(started_at)}_{uuid4().hex[:12]}"
        root_preflight = _scheduler_lock_evidence_root_preflight(self.config)
        if root_preflight["status"] == "blocked":
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "preflight_blocked",
                    "finished_at": _format_utc(_now(self.config)),
                    "lock": {
                        "acquired": False,
                        "contention": False,
                        "lock_path": str(self.config.lock_path),
                        "reason": "scheduler_root_preflight_blocked",
                    },
                    "root_preflight": root_preflight,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "model_run_evidence": [],
                    "slurm_cancellation_evidence": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "scheduler_root_preflight_blocked",
                }
            )
            artifact_path = self._write_prelock_blocked_evidence(pass_id, evidence, root_preflight)
            return SchedulerPassResult(
                pass_id=pass_id,
                status="preflight_blocked",
                evidence=evidence,
                artifact_path=artifact_path,
            )
        lock = FileSchedulerLease(
            Path(self.config.lock_path),
            ttl_seconds=self.config.lock_ttl_seconds,
            workspace_root=Path(self.config.workspace_root),
        )
        lock_result = lock.acquire(pass_id=pass_id, started_at=started_at)
        if not lock_result["acquired"]:
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "lock_contended",
                    "finished_at": _format_utc(_now(self.config)),
                    "lock": lock_result,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "lock_contended")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )

        try:
            root_preflight = _scheduler_runtime_root_preflight(self.config)
            if root_preflight["status"] == "blocked":
                finished_at = _now(self.config)
                evidence = self._base_evidence(pass_id, started_at)
                evidence.update(
                    {
                        "status": "preflight_blocked",
                        "finished_at": _format_utc(finished_at),
                        "lock": lock_result,
                        "root_preflight": root_preflight,
                        "counts": _empty_counts(),
                        "candidates": [],
                        "blocked_candidates": [],
                        "skipped_candidates": [],
                        "duplicate_exclusions": list(self.config.source_exclusions),
                        "model_discovery": _empty_model_discovery(),
                        "source_cycles": [],
                        "model_run_evidence": [],
                        "slurm_cancellation_evidence": [],
                        "no_mutation_proof": _no_mutation_proof(),
                        "execution_boundary": "scheduler_root_preflight_blocked",
                    }
                )
                artifact_path = self._write_evidence(pass_id, evidence)
                status = _evidence_status(evidence, "preflight_blocked")
                return SchedulerPassResult(
                    pass_id=pass_id,
                    status=status,
                    evidence=evidence,
                    artifact_path=artifact_path,
                )
            models, model_evidence = self._discover_models()
            cycles, source_cycle_evidence = self._discover_cycles(started_at)
            (
                candidates,
                blocked_candidates,
                skipped_candidates,
                candidate_duplicate_exclusions,
                slurm_status_sync_evidence,
            ) = self._build_candidates(models=models, cycles=cycles)
            cancellation_evidence: list[dict[str, Any]] = []
            pending_cancel_candidates = [
                candidate
                for candidate in skipped_candidates
                if candidate.get("reason") == "cancel_requested_active_slurm"
            ]
            cancel_active_slurm_requested = (
                self.config.cancel_active_slurm
                and not self.config.dry_run
                and bool(pending_cancel_candidates)
            )
            execution_evidence: list[dict[str, Any]] = []
            submitted_count = 0
            failed_count = 0
            partial_count = 0
            execution_boundary = "planning_only"
            pass_status = "planned"
            no_mutation_proof = _no_mutation_proof()
            execution_write_proof = _execution_write_proof()
            slurm_preflight_evidence: dict[str, Any] | None = None
            evidence_reservation: dict[str, Any] = {"status": "not_required"}
            pending_status_sync_candidates = [
                candidate
                for candidate in skipped_candidates
                if candidate.get("reason") == "active_slurm_status_sync_deferred"
            ]
            slurm_status_sync_proof = _slurm_status_sync_proof(sync_required=bool(pending_status_sync_candidates))
            slurm_cancellation_proof = _slurm_cancellation_proof()
            mutation_candidate_count = len(candidates) + len(pending_cancel_candidates) + len(
                pending_status_sync_candidates
            )
            if not self.config.dry_run and mutation_candidate_count:
                evidence_reservation = self._reserve_pre_execution_evidence(
                    pass_id,
                    started_at,
                    mutation_candidate_count,
                )
                if evidence_reservation["status"] == "blocked":
                    execution_evidence = [
                        _candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in candidates
                    ]
                    execution_write_proof = _execution_write_proof_from_evidence(
                        execution_evidence,
                        reservation=evidence_reservation,
                    )
                    execution_evidence.extend(
                        _sync_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in pending_status_sync_candidates
                    )
                    cancellation_evidence = [
                        _cancel_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in pending_cancel_candidates
                    ]
                    execution_boundary = "evidence_preflight_blocked"
                    pass_status = "preflight_blocked"
                    slurm_status_sync_proof = _slurm_status_sync_proof(
                        sync_required=bool(pending_status_sync_candidates),
                        reservation=evidence_reservation,
                        blocked=True,
                    )
                    slurm_cancellation_proof = _slurm_cancellation_proof(
                        cancellation_required=bool(pending_cancel_candidates),
                        reservation=evidence_reservation,
                        blocked=True,
                    )
                else:
                    if pending_status_sync_candidates:
                        (
                            candidates,
                            blocked_candidates,
                            skipped_candidates,
                            candidate_duplicate_exclusions,
                            slurm_status_sync_evidence,
                        ) = self._build_candidates(
                            models=models,
                            cycles=cycles,
                            allow_slurm_status_sync=True,
                        )
                        pending_cancel_candidates = [
                            candidate
                            for candidate in skipped_candidates
                            if candidate.get("reason") == "cancel_requested_active_slurm"
                        ]
                        cancel_active_slurm_requested = (
                            self.config.cancel_active_slurm
                            and not self.config.dry_run
                            and bool(pending_cancel_candidates)
                        )
                    slurm_status_sync_proof = _slurm_status_sync_proof_from_candidates(
                        slurm_status_sync_evidence,
                        reservation=evidence_reservation,
                    )
                    if _slurm_status_sync_failed(slurm_status_sync_proof):
                        pass_status = "slurm_status_sync_failed"
                        execution_boundary = "slurm_status_sync"
                    else:
                        if cancel_active_slurm_requested:
                            cancellation_evidence = self._cancel_requested_active_slurm(skipped_candidates)
                            slurm_cancellation_proof = _slurm_cancellation_proof_from_evidence(
                                cancellation_evidence,
                                reservation=evidence_reservation,
                            )
                        if candidates:
                            slurm_preflight = _slurm_preflight(self.config)
                            if slurm_preflight["status"] != "not_required":
                                slurm_preflight_evidence = redact_payload(slurm_preflight)
                            if slurm_preflight["status"] == "blocked":
                                execution_evidence = [
                                    _candidate_slurm_preflight_blocked_evidence(candidate, slurm_preflight)
                                    for candidate in candidates
                                ]
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                execution_boundary = "slurm_preflight_blocked"
                                pass_status = "preflight_blocked"
                            elif self.orchestrator_factory is None and not self.config.slurm_execution_enabled:
                                execution_evidence = [
                                    _candidate_preflight_blocked_evidence(candidate, config=self.config)
                                    for candidate in candidates
                                ]
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                execution_boundary = "preflight_blocked"
                                pass_status = "preflight_blocked"
                                no_mutation_proof = _no_mutation_proof()
                            else:
                                execution_evidence = self._execute_candidates(candidates)
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                submitted_count = sum(
                                    1 for item in execution_evidence if item.get("submitted") is True
                                )
                                execution_boundary = (
                                    "slurm_gateway_orchestration"
                                    if self.config.slurm_execution_enabled
                                    else "production_orchestration"
                                )
                if execution_evidence:
                    pass_status = _scheduler_pass_status_from_execution(execution_evidence)
                if cancellation_evidence and not execution_evidence:
                    pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                    execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
                elif cancellation_evidence and pass_status == "planned":
                    pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                    execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
                if (
                    pass_status == "planned"
                    and execution_boundary == "planning_only"
                    and _slurm_status_sync_mutated(slurm_status_sync_proof)
                ):
                    pass_status = "slurm_status_synced"
                    execution_boundary = "slurm_status_sync"
                scheduler_mutation_proof = _scheduler_mutation_proof(
                    execution_write_proof=execution_write_proof,
                    slurm_status_sync_proof=slurm_status_sync_proof,
                    slurm_cancellation_proof=slurm_cancellation_proof,
                )
                no_mutation_proof = {
                    "adapter_download_called": False,
                    "slurm_submit_called": scheduler_mutation_proof["slurm_submit_called"],
                    "slurm_status_sync_called": slurm_status_sync_proof.get("sync_called") is True,
                    "slurm_cancellation_called": slurm_cancellation_proof.get("cancel_called") is True,
                    "shud_runtime_called": False,
                    "hydro_result_table_writes": scheduler_mutation_proof["hydro_result_table_writes"],
                    "met_result_table_writes": scheduler_mutation_proof["met_result_table_writes"],
                    "pipeline_status_writes": scheduler_mutation_proof["pipeline_status_writes"],
                    "pipeline_event_writes": scheduler_mutation_proof["pipeline_event_writes"],
                }
                failed_count = _scheduler_failed_count_from_execution(execution_evidence)
                partial_count = _scheduler_partial_count_from_execution(execution_evidence)
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence["operator_filters"].update(model_evidence["operator_filters"])
            evidence["filters"] = dict(evidence["operator_filters"])
            duplicate_exclusions = [
                *self.config.source_exclusions,
                *[item for item in source_cycle_evidence if item.get("status") == "excluded"],
                *candidate_duplicate_exclusions,
            ]
            total_candidate_count = len(candidates) + len(blocked_candidates) + len(skipped_candidates)
            evidence.update(
                {
                    "status": pass_status,
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "model_discovery": model_evidence,
                    "source_cycles": source_cycle_evidence,
                    "candidates": [candidate.to_dict() for candidate in candidates],
                    "blocked_candidates": [candidate.to_dict() for candidate in blocked_candidates],
                    "skipped_candidates": skipped_candidates,
                    "duplicate_exclusions": duplicate_exclusions,
                    "counts": {
                        "candidate_count": total_candidate_count,
                        "blocked_candidate_count": len(blocked_candidates),
                        "skipped_candidate_count": len(skipped_candidates),
                        "selected_model_count": len(models),
                        "source_cycle_count": len(cycles),
                        "submitted_count": submitted_count,
                        "failed_count": failed_count,
                        "partial_count": partial_count,
                        "slurm_status_sync_count": _slurm_status_sync_count(slurm_status_sync_proof),
                        "slurm_status_sync_unknown_count": _slurm_status_sync_unknown_count(
                            slurm_status_sync_proof,
                        ),
                        "slurm_cancelled_count": _slurm_cancelled_count(cancellation_evidence),
                        "slurm_cancellation_blocked_count": _slurm_cancellation_blocked_count(
                            cancellation_evidence,
                        ),
                        "slurm_cancellation_unknown_count": _slurm_cancellation_unknown_count(
                            slurm_cancellation_proof,
                        ),
                    },
                    "model_run_evidence": execution_evidence,
                    "execution_write_proof": execution_write_proof,
                    "slurm_cancellation_evidence": cancellation_evidence,
                    "slurm_status_sync_proof": slurm_status_sync_proof,
                    "slurm_cancellation_proof": slurm_cancellation_proof,
                    "no_mutation_proof": no_mutation_proof,
                    "execution_boundary": execution_boundary,
                }
            )
            if slurm_preflight_evidence is not None:
                evidence["slurm_preflight"] = slurm_preflight_evidence
            if (
                not self.config.dry_run
                and mutation_candidate_count
                and evidence_reservation["status"] != "not_required"
            ):
                evidence["evidence_pre_execution"] = evidence_reservation
            if root_preflight["status"] != "not_required":
                evidence["root_preflight"] = root_preflight
            try:
                artifact_path = self._write_evidence(pass_id, evidence)
            except (OSError, SchedulerEvidenceWriteError) as error:
                if evidence_reservation.get("status") != "blocked":
                    raise
                evidence["evidence_write_error"] = _evidence_write_error_payload(error)
                artifact_path = None
            status = _evidence_status(evidence, pass_status)
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        except SchedulerResourceLimitError as error:
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "resource_limit_blocked",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "limit": {"reason": error.reason, **error.details},
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "planning_only",
                }
            )
            if root_preflight["status"] != "not_required":
                evidence["root_preflight"] = root_preflight
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "resource_limit_blocked")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        finally:
            lock.release(pass_id=pass_id)

    def _write_prelock_blocked_evidence(
        self,
        pass_id: str,
        evidence: dict[str, Any],
        root_preflight: Mapping[str, Any],
    ) -> Path | None:
        checks = root_preflight.get("checks")
        evidence_check = checks.get("evidence_root") if isinstance(checks, Mapping) else None
        if not isinstance(evidence_check, Mapping) or evidence_check.get("writable") is not True:
            return None
        try:
            return self._write_evidence(pass_id, evidence)
        except SchedulerEvidenceWriteError as error:
            evidence["evidence_write_error"] = {"reason": error.reason, **error.details}
            return None
        except OSError as error:
            evidence["evidence_write_error"] = {"reason": "evidence_write_failed", "error": str(error)}
            return None

    def _reserve_pre_execution_evidence(
        self,
        pass_id: str,
        started_at: datetime,
        candidate_count: int,
    ) -> dict[str, Any]:
        evidence_dir = Path(self.config.evidence_dir)
        workspace_root = Path(self.config.workspace_root)
        artifact_name = f"{pass_id}.pre_execution.json"
        artifact_path = evidence_dir / artifact_name
        payload = {
            "schema_version": "nhms.production_scheduler.pre_execution_evidence_reservation.v1",
            "pass_id": pass_id,
            "started_at": _format_utc(started_at),
            "reserved_at": _format_utc(_now(self.config)),
            "status": "reserved",
            "candidate_count": candidate_count,
            "artifact_path": str(artifact_path),
            "final_evidence_artifact": str(evidence_dir / f"{pass_id}.json"),
            "proof": "scheduler_evidence_directory_write_before_production_mutation",
        }
        try:
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            _require_under_workspace(artifact_path.parent.resolve(), workspace_root, "evidence_dir")
            serialized = json.dumps(_evidence_safe(payload), indent=2, sort_keys=True)
            evidence_dir_fd = _open_evidence_directory(evidence_dir, workspace_root)
            try:
                _require_evidence_artifact_available(
                    f"{pass_id}.json",
                    dir_fd=evidence_dir_fd,
                    artifact_path=evidence_dir / f"{pass_id}.json",
                )
                _write_new_regular_file(
                    artifact_name,
                    serialized,
                    dir_fd=evidence_dir_fd,
                    artifact_path=artifact_path,
                )
            finally:
                os.close(evidence_dir_fd)
        except SchedulerEvidenceWriteError as error:
            return _evidence_reservation_blocked_payload(
                pass_id=pass_id,
                artifact_path=artifact_path,
                reason=error.reason,
                details=error.details,
            )
        except OSError as error:
            return _evidence_reservation_blocked_payload(
                pass_id=pass_id,
                artifact_path=artifact_path,
                reason="evidence_write_failed",
                details={"error": str(error)},
            )
        return payload

    def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
        if max_passes is not None:
            max_passes = int(max_passes)
            if max_passes < 1:
                raise ValueError("production scheduler max_passes must be at least 1")
            if max_passes > MAX_CONTINUOUS_JSON_PASSES:
                raise ValueError(
                    "production scheduler max_passes exceeds finite JSON output limit "
                    f"{MAX_CONTINUOUS_JSON_PASSES}"
                )
        results: list[SchedulerPassResult] = []
        completed = 0
        while max_passes is None or completed < max_passes:
            result = self.run_once()
            if max_passes is None:
                results[:] = [result]
            else:
                results.append(result)
            completed += 1
            if max_passes is not None and completed >= max_passes:
                break
            self.sleep(self.config.interval_seconds)
        return results

    def _execute_candidates(self, candidates: Sequence[SchedulerCandidate]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, datetime], list[SchedulerCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault((candidate.source_id, candidate.cycle_time_utc), []).append(candidate)

        evidence: list[dict[str, Any]] = []
        for (source_id, cycle_time), cycle_candidates in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], item[0][1], [candidate.model_id for candidate in item[1]]),
        ):
            cycle_id = cycle_id_for(source_id, cycle_time)
            for cohort_key, cohort_candidates in _restart_compatible_candidate_cohorts(cycle_candidates):
                for execution_candidates, cohort_run_id in _candidate_execution_cohorts(
                    source_id,
                    cycle_time,
                    cohort_key,
                    cohort_candidates,
                ):
                    evidence.extend(
                        self._execute_candidate_cohort(
                            source_id,
                            cycle_time,
                            cycle_id,
                            execution_candidates,
                            orchestration_run_id=cohort_run_id,
                        )
                    )
        return evidence

    def _execute_candidate_cohort(
        self,
        source_id: str,
        cycle_time: datetime,
        cycle_id: str,
        cycle_candidates: Sequence[SchedulerCandidate],
        *,
        orchestration_run_id: str | None,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        orchestrator = self._orchestrator_for(source_id)
        basins: list[dict[str, Any]] = []
        submitted_candidates: list[SchedulerCandidate] = []
        candidate_output_uris: dict[str, str] = {}
        for candidate in cycle_candidates:
            output_uri = _candidate_output_uri(candidate, getattr(orchestrator, "object_store", None))
            if output_uri is None:
                evidence.append(
                    {
                        **_candidate_identity_evidence(candidate),
                        "status": "blocked",
                        "submitted": False,
                        "mutation_occurred": False,
                        "cycle_id": cycle_id,
                        "error_code": "OUTPUT_URI_UNAVAILABLE",
                        "error_message": (
                            "Production orchestration requires an absolute deterministic output_uri "
                            "before runtime handoff."
                        ),
                        **_candidate_model_run_review_evidence(
                            candidate,
                            output_uri=output_uri,
                            outcome=None,
                            status="blocked",
                            stage_statuses=[],
                        ),
                        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
                        "qhh_script_invoked": False,
                    }
                )
                continue
            candidate_output_uris[candidate.candidate_id] = output_uri
            submitted_candidates.append(candidate)
            basin_manifest = _candidate_basin_manifest(
                candidate,
                output_uri=output_uri,
                orchestration_run_id=orchestration_run_id,
            )
            if self.config.slurm_execution_enabled and self.config.slurm_env:
                basin_manifest["slurm_env"] = dict(self.config.slurm_env)
            basins.append(basin_manifest)
        if not basins:
            return evidence
        if self.config.slurm_execution_enabled:
            safe_pairs: list[tuple[SchedulerCandidate, dict[str, Any]]] = []
            for candidate, basin_manifest in zip(submitted_candidates, basins, strict=True):
                env_value = basin_manifest.get("slurm_env") or {}
                if env_value:
                    env_check, env_blockers = _slurm_env_check(env_value)
                    if env_blockers:
                        evidence.append(
                            _candidate_slurm_preflight_blocked_evidence(
                                candidate,
                                {
                                    "status": "blocked",
                                    "enabled": True,
                                    "blockers": env_blockers,
                                    "checks": {"environment": env_check},
                                },
                            )
                        )
                        continue
                findings = iter_secret_manifest_findings(basin_manifest, "manifest")
                if findings:
                    evidence.append(
                        _candidate_secret_manifest_blocked_evidence(candidate, findings=findings)
                    )
                    continue
                resource_profile_blockers = _slurm_resource_profile_blockers(candidate.resource_profile)
                if resource_profile_blockers:
                    evidence.append(
                        _candidate_slurm_preflight_blocked_evidence(
                            candidate,
                            {
                                "status": "blocked",
                                "enabled": True,
                                "blockers": resource_profile_blockers,
                                "checks": {"resource_profile": {"valid": False}},
                            },
                        )
                    )
                    continue
                safe_pairs.append((candidate, basin_manifest))
            submitted_candidates = [candidate for candidate, _basin_manifest in safe_pairs]
            basins = [basin_manifest for _candidate, basin_manifest in safe_pairs]
            if not basins:
                return evidence
        try:
            result = orchestrator.orchestrate_cycle(source_id, cycle_time, basins)
        except Exception as error:
            safe_error_message = _evidence_safe(getattr(error, "message", str(error)))
            error_code = str(getattr(error, "error_code", "PRODUCTION_ORCHESTRATION_FAILED"))
            for candidate in submitted_candidates:
                output_uri = candidate_output_uris.get(candidate.candidate_id)
                evidence.append(
                    {
                        **_candidate_identity_evidence(candidate, output_uri=output_uri),
                        "status": "submission_failed",
                        "submitted": False,
                        "slurm_submit_called": UNKNOWN_AFTER_ATTEMPT,
                        "execution_attempted": True,
                        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                        "mutation_occurred": UNKNOWN_AFTER_ATTEMPT,
                        "cycle_id": cycle_id,
                        "error_code": error_code,
                        "error_message": safe_error_message,
                        **_candidate_model_run_review_evidence(
                            candidate,
                            output_uri=output_uri,
                            outcome=None,
                            status="submission_failed",
                            stage_statuses=[],
                        ),
                        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
                        "qhh_script_invoked": False,
                        "pipeline_status_write": UNKNOWN_AFTER_ATTEMPT,
                        "pipeline_event_write": UNKNOWN_AFTER_ATTEMPT,
                        "pipeline_status_writes_proven_absent": False,
                        "pipeline_event_writes_proven_absent": False,
                        "residual_blockers": [
                            {
                                "code": error_code,
                                "state": "blocked",
                                "quality_flag": "production_orchestration_failed",
                                "residual_risk": (
                                    "Production orchestration raised after the downstream orchestration method "
                                    "was called; production write outcome is unknown."
                                ),
                            }
                        ],
                    }
                )
            return evidence
        evidence.extend(
            _candidate_execution_evidence(
                result,
                submitted_candidates,
                output_uris=candidate_output_uris,
            )
        )
        return evidence

    def _cancel_requested_active_slurm(self, skipped_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in skipped_candidates:
            if candidate.get("reason") != "cancel_requested_active_slurm":
                continue
            source_id = str(candidate.get("source_id") or "")
            cycle_time_text = candidate.get("cycle_time_utc")
            if not source_id or not cycle_time_text:
                continue
            grouped.setdefault((source_id, str(cycle_time_text)), candidate)

        evidence: list[dict[str, Any]] = []
        for (source_id, cycle_time_text), skipped in sorted(grouped.items()):
            cycle_time = _ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
            cycle_id = cycle_id_for(source_id, cycle_time)
            orchestrator = self._orchestrator_for(source_id)
            cancel = getattr(orchestrator, "cancel_active_cycle_jobs", None)
            if not callable(cancel):
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "blocked",
                        "error_code": "SLURM_CANCEL_UNSUPPORTED",
                        "cancel_attempted": False,
                        "mutation_occurred": False,
                        "replacement_submitted": False,
                    }
                )
                continue
            try:
                cancelled = _bounded_active_slurm_jobs(
                    [dict(item) for item in cancel(cycle_id, reason="scheduler_cancel_requested")],
                    max_jobs=self.config.candidate_state_job_limit,
                )
            except Exception as error:
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "failed",
                        "error_code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                        "error_message": _evidence_safe(getattr(error, "message", str(error))),
                        "cancel_attempted": True,
                        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                        "replacement_submitted": False,
                        "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
                        "residual_blockers": [
                            {
                                "code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                                "state": "blocked",
                                "quality_flag": "slurm_cancellation_failed",
                                "residual_risk": (
                                    "Slurm cancellation raised after the downstream cancellation method was called; "
                                    "mutation outcome is unknown."
                                ),
                            }
                        ],
                    }
                )
                continue
            cancellation_status = _scheduler_cancellation_status(cancelled)
            cancellation_item: dict[str, Any] = {
                "source_id": source_id,
                "cycle_id": cycle_id,
                "cycle_time_utc": cycle_time_text,
                "status": cancellation_status,
                "cancelled_jobs": _evidence_safe(cancelled),
                "cancel_attempted": True,
                "mutation_occurred": cancellation_status in {"cancelled", "partially_cancelled"},
                "replacement_submitted": False,
                "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
            }
            pipeline_status_write = any(_cancelled_job_pipeline_status_write(item) for item in cancelled)
            pipeline_event_write = any(_cancelled_job_pipeline_event_write(item) for item in cancelled)
            if pipeline_status_write:
                cancellation_item["pipeline_status_write"] = True
            if pipeline_event_write:
                cancellation_item["pipeline_event_write"] = True
            if cancellation_status != "cancelled":
                cancellation_item["error_code"] = "SLURM_CANCELLATION_GAP"
                cancellation_item["cancellation_proven"] = False
            evidence.append(
                cancellation_item
            )
        return evidence

    def _orchestrator_for(self, source_id: str) -> ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        config = OrchestratorConfig.from_env()
        if self.config.slurm_execution_enabled:
            config = OrchestratorConfig(
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root or config.object_store_root,
                object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", config.object_store_prefix),
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=config.source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=config.scenario_id if config.scenario_id_explicit else None,
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                slurm_job_type_templates=dict(self.config.slurm_job_type_templates or {}),
                slurm_env=dict(self.config.slurm_env),
            )
        if config.source_id != source_id:
            config = OrchestratorConfig(
                workspace_root=config.workspace_root,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=scenario_for_source(source_id),
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                slurm_job_type_templates=config.slurm_job_type_templates,
                slurm_env=config.slurm_env,
            )
        return ForecastOrchestrator(
            config=config,
            repository=_orchestrator_repository_from_env(),
            state_manager=None,
        )

    def _base_evidence(self, pass_id: str, started_at: datetime) -> dict[str, Any]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        execution_mode = "dry_run" if self.config.dry_run else "production_orchestration"
        readiness_interpretation = (
            "deterministic_review_only" if self.config.dry_run else "non_final_scheduler_evidence"
        )
        operator_filters = {
            "model_ids": list(self.config.model_ids),
            "basin_ids": list(self.config.basin_ids),
            "expression": _filter_expression(self.config.model_ids, self.config.basin_ids),
            "excluded_runnable_count": 0,
        }
        return {
            "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
            "review_contract": {
                "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
                "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
                "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
                "scope": "scheduler_pass_evidence",
            },
            "production_contract": production_contract_matrix(),
            "pass_id": pass_id,
            "started_at": _format_utc(started_at),
            "execution_mode": execution_mode,
            "readiness_interpretation": readiness_interpretation,
            "dry_run": self.config.dry_run,
            "sources": list(self.config.sources),
            "duplicate_exclusions": list(self.config.source_exclusions),
            "cycle_window": {
                "start_time_utc": _format_utc(start_time),
                "end_time_utc": _format_utc(end_time),
                "lookback_hours": self.config.lookback_hours,
                "cycle_lag_hours": self.config.cycle_lag_hours,
                "max_cycles_per_source": self.config.max_cycles_per_source,
            },
            "operator_filters": dict(operator_filters),
            "filters": dict(operator_filters),
            "readiness": {
                "schema_version": "nhms.production_readiness.scheduler_input.v1",
                "interpretation": readiness_interpretation,
                "deterministic_fixture": self.config.dry_run,
                "scheduler_evidence_accepted_for_review": True,
                "live_receipts": [],
                "production_ready": False,
                "final_production_readiness_claimed": False,
                "can_claim_final_production_readiness": False,
                "reason": "scheduler evidence requires accepted live proof receipts for final readiness",
            },
            "resolved_runtime_roots": _scheduler_resolved_runtime_roots(self.config),
            "runtime_config": _scheduler_runtime_config_evidence(self.config),
        }

    def _discover_models(self) -> tuple[list[RegisteredSchedulerModel], dict[str, Any]]:
        rows = _fetch_active_model_details(self.registry)
        exclusions: list[dict[str, Any]] = []
        runnable: list[RegisteredSchedulerModel] = []

        model_counts = Counter(str(row.get("model_id") or "") for row in rows)
        duplicate_model_ids = {model_id for model_id, count in model_counts.items() if model_id and count > 1}

        for row in rows:
            model_id = str(row.get("model_id") or "")
            if model_id in duplicate_model_ids:
                exclusions.append(_model_exclusion(row, "duplicate_active_model_identity"))
                continue
            model = _coerce_registered_model(row)
            if isinstance(model, RegisteredSchedulerModel):
                runnable.append(model)
            else:
                exclusions.append(model)

        runnable.sort(key=lambda item: item.model_id)
        selected: list[RegisteredSchedulerModel] = []
        filter_excluded = 0
        for model in runnable:
            if not _matches_filters(model, model_ids=self.config.model_ids, basin_ids=self.config.basin_ids):
                filter_excluded += 1
                exclusions.append(
                    {
                        "model_id": model.model_id,
                        "basin_id": model.basin_id,
                        "basin_version_id": model.basin_version_id,
                        "reason": "operator_filter_excluded",
                    }
                )
                continue
            selected.append(model)

        evidence = {
            "active_model_count": len(rows),
            "runnable_model_count": len(runnable),
            "selected_model_count": len(selected),
            "excluded_model_count": len(exclusions),
            "models": [model.to_dict() for model in selected],
            "exclusions": exclusions,
        }
        evidence["operator_filters"] = {
            "expression": _filter_expression(self.config.model_ids, self.config.basin_ids),
            "excluded_runnable_count": filter_excluded,
        }
        return selected, evidence

    def _discover_cycles(self, started_at: datetime) -> tuple[list[SchedulerSourceCycle], list[dict[str, Any]]]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        source_cycles: list[SchedulerSourceCycle] = []
        evidence: list[dict[str, Any]] = []
        seen_cycles: set[tuple[str, str]] = set()
        source_order = {source_id: index for index, source_id in enumerate(self.config.sources)}

        for source_id in self.config.sources:
            adapter = self.adapters.get(source_id)
            if adapter is None:
                source_evidence = {
                    "source_id": source_id,
                    "available": False,
                    "status": "blocked",
                    "reason": "source_adapter_unavailable",
                    "cycle_id": None,
                    "cycle_time_utc": None,
                }
                evidence.append(source_evidence)
                continue

            discoveries = self._discover_source_window(
                adapter,
                source_id=source_id,
                start_time=start_time,
                end_time=end_time,
            )
            discoveries = [
                discovery
                for discovery in discoveries
                if discovery.source_id == source_id and start_time <= _ensure_utc(discovery.cycle_time) <= end_time
            ]
            discoveries.sort(key=lambda discovery: discovery.cycle_time, reverse=True)
            selected_for_source: list[CycleDiscovery] = []
            for discovery in discoveries:
                cycle_key = (source_id, cycle_id_for(source_id, discovery.cycle_time))
                if cycle_key in seen_cycles:
                    evidence.append(_duplicate_cycle_evidence(discovery, reason="duplicate_source_cycle"))
                    continue
                seen_cycles.add(cycle_key)
                if len(selected_for_source) < self.config.max_cycles_per_source:
                    selected_for_source.append(discovery)
            for discovery in selected_for_source:
                horizon = _source_horizon_metadata(discovery, adapter)
                source_cycles.append(SchedulerSourceCycle(discovery=discovery, horizon=horizon))
                evidence.append(_source_cycle_evidence(discovery, horizon=horizon))

        source_cycles.sort(
            key=lambda item: (
                source_order.get(item.discovery.source_id, 999),
                item.discovery.cycle_time,
                item.discovery.cycle_hour,
            )
        )
        return source_cycles, evidence

    def _discover_source_window(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        discoveries: list[CycleDiscovery] = []
        current_date = start_time.date()
        while current_date <= end_time.date():
            try:
                daily = adapter.discover_cycles(current_date)
            except TypeError:
                daily = adapter.discover_cycles(current_date, None)
            if len(discoveries) + len(daily) > MAX_DISCOVERED_CYCLES:
                raise SchedulerResourceLimitError(
                    "cycle_discovery_limit_exceeded",
                    {
                        "max_discovered_cycles": MAX_DISCOVERED_CYCLES,
                        "discovered_cycle_count": len(discoveries) + len(daily),
                        "source_id": source_id,
                        "cycle_date": current_date.isoformat(),
                    },
                )
            discoveries.extend(daily)
            current_date += timedelta(days=1)
        return discoveries

    def _build_candidates(
        self,
        *,
        models: Sequence[RegisteredSchedulerModel],
        cycles: Sequence[SchedulerSourceCycle],
        allow_slurm_status_sync: bool = False,
    ) -> tuple[
        list[SchedulerCandidate],
        list[SchedulerCandidate],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        candidates: list[SchedulerCandidate] = []
        blocked: list[SchedulerCandidate] = []
        skipped: list[dict[str, Any]] = []
        duplicate_exclusions: list[dict[str, Any]] = []
        slurm_status_sync_evidence: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        active_orchestration_provider = (
            getattr(self.active_repository, "has_active_orchestration", None)
            if self.active_repository is not None
            else None
        )
        completed_provider = (
            getattr(self.active_repository, "has_completed_pipeline", None)
            if self.active_repository is not None
            else None
        )
        state_provider = (
            getattr(self.active_repository, "candidate_state", None)
            if self.active_repository is not None
            else None
        )
        active_slurm_jobs_provider = (
            getattr(self.active_repository, "active_slurm_jobs", None)
            if self.active_repository is not None
            else None
        )
        for cycle in cycles:
            discovery = cycle.discovery
            has_active_orchestration: bool | None = None
            for model in models:
                if len(candidates) + len(blocked) + len(skipped) >= MAX_CANDIDATES:
                    raise SchedulerResourceLimitError(
                        "candidate_limit_exceeded",
                        {
                            "max_candidates": MAX_CANDIDATES,
                            "source_cycle_count": len(cycles),
                            "selected_model_count": len(models),
                        },
                    )
                candidate = _candidate_for(discovery=discovery, model=model, horizon=cycle.horizon)
                if candidate.candidate_id in seen_candidate_ids:
                    exclusion = {
                        **candidate.to_dict(),
                        "status": "excluded",
                        "reason": "duplicate_candidate_identity",
                    }
                    skipped.append(exclusion)
                    duplicate_exclusions.append({"type": "candidate", **exclusion})
                    continue
                seen_candidate_ids.add(candidate.candidate_id)
                if not discovery.available:
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            "source_cycle_unavailable",
                            state_evidence=_source_unavailable_retry_evidence(candidate),
                        )
                    )
                    continue
                state_decision = (
                    _candidate_state_decision(
                        candidate,
                        _call_candidate_state_provider(
                            state_provider,
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                            model_id=model.model_id,
                            run_id=candidate.run_id,
                            forcing_version_id=candidate.forcing_version_id,
                            candidate_id=candidate.candidate_id,
                            retry_limit=self.config.retry_limit,
                            job_limit=self.config.candidate_state_job_limit,
                            event_limit=self.config.candidate_state_event_limit,
                        ),
                    )
                    if callable(state_provider)
                    else None
                )
                if state_decision is not None and _candidate_state_has_identity_mismatch(state_decision.evidence):
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            "production_identity_mismatch",
                            state_evidence=state_decision.evidence,
                        )
                    )
                    continue
                if state_decision is not None and state_decision.action == "blocked":
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            state_decision.reason or "candidate_state_blocked",
                            state_evidence=state_decision.evidence,
                        )
                    )
                    continue
                if state_decision is not None and state_decision.action == "retry":
                    candidate = _candidate_with_state_evidence(candidate, state_decision.evidence)
                if has_active_orchestration is None:
                    has_active_orchestration = bool(
                        callable(active_orchestration_provider)
                        and active_orchestration_provider(
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                        )
                    )
                active_slurm_jobs = (
                    list(
                        _call_active_slurm_jobs_provider(
                            active_slurm_jobs_provider,
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                            model_id=model.model_id,
                            limit=self.config.candidate_state_job_limit,
                        )
                    )
                    if callable(active_slurm_jobs_provider)
                    else []
                )
                active_slurm_jobs = _bounded_active_slurm_jobs(
                    active_slurm_jobs,
                    max_jobs=self.config.candidate_state_job_limit,
                )
                slurm_state_sync: dict[str, Any] | None = None
                if active_slurm_jobs and not self.config.cancel_active_slurm and not self.config.dry_run:
                    cycle_id = cycle_id_for(discovery.source_id, discovery.cycle_time)
                    sync = None
                    if allow_slurm_status_sync:
                        sync = getattr(self._orchestrator_for(discovery.source_id), "sync_cycle_statuses", None)
                    if allow_slurm_status_sync and callable(sync):
                        try:
                            synced_updates = _bounded_active_slurm_jobs(
                                [dict(item) for item in sync(cycle_id)],
                                max_jobs=self.config.candidate_state_job_limit,
                            )
                        except Exception as error:
                            slurm_state_sync = _slurm_status_sync_failed_evidence(
                                candidate,
                                cycle_id=cycle_id,
                                active_slurm_jobs=active_slurm_jobs,
                                error=error,
                            )
                            slurm_status_sync_evidence.append(slurm_state_sync)
                            skipped.append(
                                {
                                    **candidate.to_dict(),
                                    "reason": "active_slurm_status_sync_failed",
                                    "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                    "sync_required": True,
                                    "sync_attempted": True,
                                    "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                                    "state_evidence": {"slurm_state_sync": slurm_state_sync},
                                }
                            )
                            continue
                        slurm_state_sync = {
                            "cycle_id": cycle_id,
                            "status": "synced",
                            "updates": synced_updates,
                            "terminal_updates": [
                                item
                                for item in synced_updates
                                if str(item.get("status") or "") in TERMINAL_PIPELINE_STATUSES
                            ],
                        }
                        slurm_status_sync_evidence.append(slurm_state_sync)
                        if synced_updates:
                            state_decision = (
                                _candidate_state_decision(
                                    candidate,
                                    _call_candidate_state_provider(
                                        state_provider,
                                        source_id=discovery.source_id,
                                        cycle_time=discovery.cycle_time,
                                        model_id=model.model_id,
                                        run_id=candidate.run_id,
                                        forcing_version_id=candidate.forcing_version_id,
                                        candidate_id=candidate.candidate_id,
                                        retry_limit=self.config.retry_limit,
                                        job_limit=self.config.candidate_state_job_limit,
                                        event_limit=self.config.candidate_state_event_limit,
                                    ),
                                )
                                if callable(state_provider)
                                else state_decision
                            )
                            if state_decision is not None:
                                state_decision = CandidateStateDecision(
                                    action=state_decision.action,
                                    reason=state_decision.reason,
                                    evidence={
                                        **dict(state_decision.evidence),
                                        "slurm_state_sync": slurm_state_sync,
                                    },
                                )
                            if state_decision is not None and _candidate_state_has_identity_mismatch(
                                state_decision.evidence,
                            ):
                                blocked.append(
                                    _blocked_candidate(
                                        candidate,
                                        "production_identity_mismatch",
                                        state_evidence=state_decision.evidence,
                                    )
                                )
                                continue
                            active_slurm_jobs = (
                                _bounded_active_slurm_jobs(
                                    list(
                                        _call_active_slurm_jobs_provider(
                                            active_slurm_jobs_provider,
                                            source_id=discovery.source_id,
                                            cycle_time=discovery.cycle_time,
                                            model_id=model.model_id,
                                            limit=self.config.candidate_state_job_limit,
                                        )
                                    ),
                                    max_jobs=self.config.candidate_state_job_limit,
                                )
                                if callable(active_slurm_jobs_provider)
                                else []
                            )
                            if state_decision is not None and state_decision.action == "retry":
                                candidate = _candidate_with_state_evidence(candidate, state_decision.evidence)
                            elif state_decision is None:
                                candidate = _candidate_with_state_evidence(
                                    candidate,
                                    {"slurm_state_sync": slurm_state_sync},
                                )
                    elif not allow_slurm_status_sync:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "active_slurm_status_sync_deferred",
                                "cycle_id": cycle_id,
                                "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                                "sync_required": True,
                                "sync_attempted": False,
                                "mutation_occurred": False,
                            }
                        )
                        continue
                if state_decision is not None and state_decision.action == "blocked":
                    blocked.append(
                        _blocked_candidate(
                            candidate,
                            state_decision.reason or "candidate_state_blocked",
                            state_evidence=state_decision.evidence,
                        )
                    )
                    continue
                if (
                    state_decision is not None
                    and state_decision.action == "skip"
                    and not (self.config.cancel_active_slurm and state_decision.reason == "active_slurm_job")
                ):
                    skipped.append(
                        {
                            **candidate.to_dict(),
                            "reason": state_decision.reason,
                            "state_evidence": _evidence_safe(state_decision.evidence),
                        }
                    )
                    continue
                cycle_active_blocks_candidate = has_active_orchestration and not (
                    self.config.cancel_active_slurm and active_slurm_jobs
                )
                if cycle_active_blocks_candidate and _candidate_state_is_candidate_scoped_retry(
                    state_decision,
                ):
                    cycle_active_blocks_candidate = False
                if cycle_active_blocks_candidate:
                    skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                    continue
                if active_slurm_jobs:
                    active_slurm_skip: dict[str, Any]
                    if self.config.cancel_active_slurm:
                        active_slurm_skip = {
                            **candidate.to_dict(),
                            "reason": "cancel_requested_active_slurm",
                            "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                            "replacement_submitted": False,
                        }
                    else:
                        active_slurm_skip = {
                            **candidate.to_dict(),
                            "reason": "active_slurm_job",
                            "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
                        }
                    if slurm_state_sync is not None:
                        skip_evidence = dict(active_slurm_skip.get("state_evidence") or {})
                        skip_evidence["slurm_state_sync"] = slurm_state_sync
                        active_slurm_skip["state_evidence"] = _evidence_safe(skip_evidence)
                    skipped.append(active_slurm_skip)
                    continue
                if state_decision is not None and state_decision.action == "skip":
                    skip_evidence = dict(state_decision.evidence)
                    if slurm_state_sync is not None:
                        skip_evidence["slurm_state_sync"] = slurm_state_sync
                    skipped.append(
                        {
                            **candidate.to_dict(),
                            "reason": state_decision.reason,
                            "state_evidence": _evidence_safe(skip_evidence),
                        }
                    )
                    continue
                if self.active_repository is not None and self.active_repository.has_active_pipeline(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                ):
                    skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                    continue
                if callable(completed_provider) and completed_provider(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                ):
                    skipped.append({**candidate.to_dict(), "reason": "completed_duplicate_pipeline"})
                    continue
                candidates.append(candidate)
        return candidates, blocked, skipped, duplicate_exclusions, slurm_status_sync_evidence

    def _write_evidence(self, pass_id: str, evidence: Mapping[str, Any]) -> Path | None:
        evidence_dir = Path(self.config.evidence_dir)
        workspace_root = Path(self.config.workspace_root)
        _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
        artifact_path = evidence_dir / f"{pass_id}.json"
        _require_under_workspace(artifact_path.parent.resolve(), workspace_root, "evidence_dir")
        payload = _evidence_safe(dict(evidence))
        if not isinstance(payload, dict):
            payload = {}
        payload["artifact_path"] = str(artifact_path)
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        bounded_payload: dict[str, Any] | None = None
        if len(serialized.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            bounded_payload = _bounded_evidence_payload(payload, reason="evidence_size_limit_exceeded")
            serialized = json.dumps(bounded_payload, indent=2, sort_keys=True)
        evidence_dir_fd = _open_evidence_directory(evidence_dir, workspace_root)
        try:
            _write_new_regular_file(
                f"{pass_id}.json",
                serialized,
                dir_fd=evidence_dir_fd,
                artifact_path=artifact_path,
            )
        finally:
            os.close(evidence_dir_fd)
        if isinstance(evidence, dict):
            if bounded_payload is not None:
                evidence.clear()
                evidence.update(bounded_payload)
            else:
                evidence.clear()
                evidence.update(payload)
            evidence.setdefault("artifact_path", str(artifact_path))
        return artifact_path


class FileSchedulerLease:
    def __init__(self, lock_path: Path, *, ttl_seconds: int, workspace_root: Path | None = None) -> None:
        self.lock_path = lock_path
        self.ttl_seconds = ttl_seconds
        self.workspace_root = workspace_root
        self.acquired = False
        self.lease_token: str | None = None

    def acquire(self, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        token = uuid4().hex
        payload = {
            "owner": LOCK_OWNER,
            "schema_version": LOCK_SCHEMA_VERSION,
            "pass_id": pass_id,
            "lease_token": token,
            "pid": os.getpid(),
            "started_at": _format_utc(started_at),
            "lock_path": str(self.lock_path),
        }
        try:
            with self._guarded() as parent_fd:
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
        except UnsafeSchedulerLockError as error:
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "reason": error.reason,
                "existing_lock": {"raw": None},
            }

    def _acquire_locked(
        self,
        *,
        pass_id: str,
        started_at: datetime,
        payload: Mapping[str, Any],
        parent_fd: int,
    ) -> dict[str, Any]:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError:
            state = self._existing_lock_state(started_at, parent_fd=parent_fd)
            if state["unsafe"]:
                return {
                    "acquired": False,
                    "contention": True,
                    "lock_path": str(self.lock_path),
                    "reason": state["reason"],
                    "existing_lock": state["existing_lock"],
                }
            if state["stale"]:
                _unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "existing_lock": state["existing_lock"],
            }
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_symlink") from error
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = True
        self.lease_token = str(payload.get("lease_token"))
        return {"acquired": True, "contention": False, "lock_path": str(self.lock_path), "lease": dict(payload)}

    def release(self, *, pass_id: str) -> None:
        if not self.acquired:
            return
        try:
            with self._guarded() as parent_fd:
                existing = self._read_existing_lock(parent_fd=parent_fd)
                if existing.get("pass_id") == pass_id and existing.get("lease_token") == self.lease_token:
                    _unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
        except UnsafeSchedulerLockError:
            pass
        self.acquired = False
        self.lease_token = None

    @contextmanager
    def _guarded(self) -> Any:
        import fcntl

        parent_fd = _open_lock_parent_directory(self.lock_path.parent, self.workspace_root)
        try:
            guard_fd = _open_regular_guard_file(f"{self.lock_path.name}.guard", dir_fd=parent_fd)
        except Exception:
            os.close(parent_fd)
            raise
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            yield parent_fd
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)
            os.close(parent_fd)

    def _existing_lock_state(self, now: datetime | None = None, *, parent_fd: int) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        try:
            lock_stat = os.stat(self.lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"unsafe": False, "stale": False, "reason": None, "existing_lock": {}}
        if stat.S_ISLNK(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_symlink",
                "existing_lock": {"raw": None},
            }
        if not stat.S_ISREG(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_not_regular_file",
                "existing_lock": {"raw": None},
            }
        if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_too_large",
                "existing_lock": {
                    "raw": None,
                    "size_bytes": lock_stat.st_size,
                    "max_bytes": MAX_LOCK_PAYLOAD_BYTES,
                },
            }
        existing = self._read_existing_lock(parent_fd=parent_fd)
        scheduler_owned = (
            existing.get("owner") == LOCK_OWNER
            and existing.get("schema_version") == LOCK_SCHEMA_VERSION
            and existing.get("lease_token") not in (None, "")
            and existing.get("pass_id") not in (None, "")
        )
        mtime = datetime.fromtimestamp(lock_stat.st_mtime, tz=UTC)
        stale = (now - mtime).total_seconds() > self.ttl_seconds
        if stale and not scheduler_owned:
            return {
                "unsafe": True,
                "stale": True,
                "reason": "unsafe_lock_not_scheduler_owned",
                "existing_lock": existing,
            }
        return {"unsafe": False, "stale": stale, "reason": None, "existing_lock": existing}

    def _read_existing_lock(self, *, parent_fd: int) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, dir_fd=parent_fd)
        except OSError:
            return {"raw": None}
        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                return {"raw": None}
            if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            raw = os.read(fd, MAX_LOCK_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            value = json.loads(raw.decode("utf-8"))
        except UnsafeSchedulerLockError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": None}
        finally:
            os.close(fd)
        return dict(value) if isinstance(value, Mapping) else {"raw": value}


def _fetch_active_model_details(registry: ModelRegistryReader) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    offset = 0
    limit = 500
    pages = 0
    while True:
        pages += 1
        if pages > MAX_REGISTRY_PAGES:
            raise SchedulerResourceLimitError(
                "registry_page_limit_exceeded",
                {"max_registry_pages": MAX_REGISTRY_PAGES, "model_count": len(rows)},
            )
        page = registry.list_models(basin_version_id=None, active=True, limit=limit, offset=offset)
        items = list(page.get("items") or [])
        for item in items:
            if len(rows) >= MAX_DISCOVERED_MODELS:
                raise SchedulerResourceLimitError(
                    "model_limit_exceeded",
                    {"max_discovered_models": MAX_DISCOVERED_MODELS, "model_count": len(rows)},
                )
            model_id = str(item.get("model_id") or "")
            rows.append(registry.get_model(model_id) if model_id else item)
        total = int(page.get("total") or len(rows))
        offset += len(items)
        if len(items) == 0 or offset >= total:
            break
    return rows


def _coerce_registered_model(row: Mapping[str, Any]) -> RegisteredSchedulerModel | dict[str, Any]:
    resource_profile = row.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        resource_profile = {}
    lifecycle_state = str(row.get("lifecycle_state") or ("active" if row.get("active_flag") else "inactive"))
    required = {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id") or resource_profile.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "river_network_version_id": row.get("river_network_version_id"),
        "model_package_uri": row.get("model_package_uri"),
        "shud_code_version": row.get("shud_code_version"),
    }
    if row.get("active_flag") is False or lifecycle_state != "active":
        return _model_exclusion(row, "inactive_model")
    if resource_profile.get("runnable") is False:
        return _model_exclusion(row, "not_runnable")
    if not required["shud_code_version"]:
        return _model_exclusion(row, "not_shud_model")
    missing = sorted(key for key, value in required.items() if value in (None, ""))
    if missing:
        return {**_model_exclusion(row, "incomplete_model_metadata"), "missing_fields": missing}

    segment_count = row.get("segment_count")
    return RegisteredSchedulerModel(
        model_id=str(required["model_id"]),
        basin_id=str(required["basin_id"]),
        basin_version_id=str(required["basin_version_id"]),
        river_network_version_id=str(required["river_network_version_id"]),
        segment_count=int(segment_count) if segment_count not in (None, "") else None,
        model_package_uri=str(required["model_package_uri"]),
        shud_code_version=str(required["shud_code_version"]),
        resource_profile=dict(resource_profile),
        resource_profile_summary=_resource_profile_summary(resource_profile),
        display_capabilities=_mapping_value(resource_profile.get("display_capabilities")),
        frequency_capabilities=_mapping_value(resource_profile.get("frequency_capabilities")),
    )


def _resource_profile_summary(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "resource_profile_id",
        "cpu",
        "memory_gb",
        "walltime",
        "max_concurrent",
        "shud_threads",
        "station_count",
        "station_ids",
        "forcing_station_metadata",
        "manifest_uri",
        "output_uri",
        "display_capabilities",
        "frequency_capabilities",
    )
    return {key: resource_profile[key] for key in keys if key in resource_profile}


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _model_exclusion(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "reason": reason,
    }


def _matches_filters(
    model: RegisteredSchedulerModel,
    *,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
) -> bool:
    if model_ids and model.model_id not in set(model_ids):
        return False
    return not (basin_ids and model.basin_id not in set(basin_ids) and model.basin_version_id not in set(basin_ids))


def _filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    parts: list[str] = []
    if model_ids:
        parts.append("model_id in [" + ",".join(model_ids) + "]")
    if basin_ids:
        parts.append("basin_id in [" + ",".join(basin_ids) + "]")
    return " and ".join(parts) if parts else None


def _source_cycle_evidence(discovery: CycleDiscovery, *, horizon: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(discovery.available)
    return {
        "source_id": discovery.source_id,
        "cycle_id": discovery.cycle_id,
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": discovery.cycle_hour,
        "horizon": dict(horizon),
        "available": available,
        "status": discovery.status or ("discovered" if available else "unavailable"),
        "reason": None if available else "source_cycle_unavailable",
        "db_cycle_status_written": None,
        "cycle_status_candidate": "discovered" if available else "unavailable",
    }


def _source_unavailable_retry_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    return {
        "decision": "blocked_retryable",
        "reason": "source_cycle_unavailable",
        "failure": {
            "classifier": "source_unavailable",
            "reason_code": "SOURCE_CYCLE_UNAVAILABLE",
            "retryable": True,
            "permanent": False,
            "attempt": 0,
            "retry_limit": None,
        },
        "retry_policy": {
            "automatic_retry_allowed": True,
            "enum_safe_storage": "scheduler_evidence",
            "unsupported_db_enum_written": False,
        },
        "storage": {
            "met_forecast_cycle_status_written": None,
            "ops_pipeline_event_details": True,
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
        },
    }


def _duplicate_cycle_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return {
        "type": "source_cycle",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": discovery.cycle_hour,
        "available": discovery.available,
        "status": "excluded",
        "reason": reason,
    }


def _candidate_state_decision(
    candidate: SchedulerCandidate,
    raw_state: Mapping[str, Any] | None,
) -> CandidateStateDecision | None:
    if raw_state is None:
        return None
    state = _bounded_candidate_state(raw_state)
    evidence = _candidate_state_evidence(candidate, state)
    if _candidate_state_has_identity_mismatch(evidence):
        return CandidateStateDecision(
            "blocked",
            "production_identity_mismatch",
            {
                **evidence,
                "decision": "blocked_identity_mismatch",
                "replacement_submitted": False,
            },
        )
    decision_state = _candidate_state_decision_state(state, evidence)
    active_jobs = _state_active_jobs(decision_state)
    if active_jobs:
        return CandidateStateDecision(
            "skip",
            "active_slurm_job",
            {
                **evidence,
                "decision": "skip_active",
                "active_slurm_jobs": active_jobs,
                "replacement_submitted": False,
            },
        )

    hydro_status = _state_status(decision_state, "hydro_status", "hydro_run_status")
    pipeline_status = _state_status(decision_state, "pipeline_status", "job_status", "status")
    if hydro_status in ACTIVE_HYDRO_STATUSES or pipeline_status in ACTIVE_PIPELINE_STATUSES:
        return CandidateStateDecision(
            "skip",
            "active_duplicate_pipeline",
            {
                **evidence,
                "decision": "skip_active",
                "active_status": hydro_status or pipeline_status,
                "replacement_submitted": False,
            },
        )
    active_truth = _latest_manual_retry_blocker(decision_state)
    if active_truth is not None and active_truth.get("active") is True:
        return CandidateStateDecision(
            "skip",
            "active_duplicate_pipeline",
            {
                **evidence,
                "decision": "skip_active",
                "active_status": active_truth.get("status"),
                "active_truth": _evidence_safe(active_truth),
                "replacement_submitted": False,
            },
        )

    if hydro_status in DURABLE_HYDRO_SUCCESS_STATUSES and _terminal_hydro_truth_supersedes_failure(decision_state):
        return CandidateStateDecision(
            "skip",
            "terminal_hydro_success",
            {
                **evidence,
                "decision": "skip_terminal",
                "terminal_source": "hydro_run",
                "terminal_status": hydro_status,
                "durable_hydro_status": hydro_status,
                "durable_output_reused": bool(_state_output_uri(decision_state)),
                "native_shud_resubmitted": False,
                "parse_resubmitted": False,
                "frequency_resubmitted": False,
                "publish_resubmitted": False,
            },
        )

    if pipeline_status in TERMINAL_PIPELINE_SUCCESS_STATUSES and _pipeline_terminal_success_is_candidate_scoped(
        candidate,
        decision_state,
    ):
        return CandidateStateDecision(
            "skip",
            "terminal_pipeline_success",
            {
                **evidence,
                "decision": "skip_terminal",
                "terminal_source": "pipeline_job",
                "terminal_status": pipeline_status,
                "native_shud_resubmitted": False,
            },
        )

    if _manual_retry_requested(decision_state):
        return CandidateStateDecision(
            "retry",
            "manual_retry_requested",
            _manual_retry_state_evidence(candidate, decision_state, evidence),
        )

    downstream_retry = _downstream_retry_evidence(candidate, decision_state, evidence)
    if downstream_retry is not None:
        return CandidateStateDecision("retry", "resume_downstream_after_durable_shud", downstream_retry)

    permanent = _permanent_failure_evidence(candidate, decision_state, evidence)
    if permanent is not None:
        return CandidateStateDecision(
            "blocked",
            str(permanent.get("reason") or "permanent_failure_guard"),
            permanent,
        )

    cancelled = _cancelled_state_evidence(candidate, decision_state, evidence)
    if cancelled is not None:
        return CandidateStateDecision(
            "blocked",
            str(cancelled.get("reason") or "manual_retry_required_after_cancelled"),
            cancelled,
        )

    if pipeline_status in FAILED_PIPELINE_STATUSES or hydro_status == "failed":
        return CandidateStateDecision(
            "retry",
            "retry_failed_candidate",
            _retry_failure_evidence(candidate, decision_state, evidence),
        )

    return None


def _call_candidate_state_provider(
    provider: Callable[..., Mapping[str, Any] | None],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    retry_limit: int,
    job_limit: int,
    event_limit: int,
) -> Mapping[str, Any] | None:
    kwargs: dict[str, Any] = {
        "source_id": source_id,
        "cycle_time": cycle_time,
        "model_id": model_id,
        "run_id": run_id,
        "forcing_version_id": forcing_version_id,
        "candidate_id": candidate_id,
        "retry_limit": retry_limit,
        "job_limit": job_limit,
        "event_limit": event_limit,
    }
    try:
        state = provider(**kwargs)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
        state = provider(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            candidate_id=candidate_id,
        )
    if state is None:
        return None
    payload = dict(state)
    payload.setdefault("retry_limit", retry_limit)
    payload.setdefault("job_limit", job_limit)
    payload.setdefault("event_limit", event_limit)
    return _bounded_candidate_state(payload)


def _candidate_state_is_candidate_scoped_retry(decision: CandidateStateDecision | None) -> bool:
    if decision is None or decision.action != "retry":
        return False
    evidence = decision.evidence
    if not isinstance(evidence, Mapping):
        return False
    identity = evidence.get("identity")
    if not isinstance(identity, Mapping):
        identity = evidence.get("candidate_identity")
    restart_stage = evidence.get("restart_stage")
    task_identity = evidence.get("task_identity")
    return bool(
        isinstance(identity, Mapping)
        and identity.get("candidate_id")
        and identity.get("run_id")
        and (restart_stage not in (None, "") or (isinstance(task_identity, Mapping) and bool(task_identity)))
    )


def _call_active_slurm_jobs_provider(
    provider: Callable[..., Sequence[Mapping[str, Any]]],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    limit: int,
) -> Sequence[Mapping[str, Any]]:
    try:
        return provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id, limit=limit)
    except TypeError as error:
        if "unexpected keyword" not in str(error):
            raise
    return provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id)


def _candidate_state_decision_state(state: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    validation = evidence.get("production_identity_validation")
    if not isinstance(validation, Mapping):
        return dict(state)
    legacy_sources = {str(source) for source in validation.get("legacy_non_authoritative", [])}
    if not legacy_sources:
        return dict(state)
    filtered = dict(state)
    if "candidate_state" in legacy_sources:
        _strip_top_level_candidate_state_decision_fields(filtered)
    for key in ("hydro_run", "forcing_version", "forecast_cycle", "published_manifest", "canonical_product"):
        if key in legacy_sources:
            filtered.pop(key, None)
    if "hydro_run" in legacy_sources:
        _strip_top_level_hydro_decision_fields(filtered)
    for key in ("pipeline_job", "job"):
        if key in legacy_sources:
            filtered.pop(key, None)
            _strip_top_level_pipeline_decision_fields(filtered)
    jobs = _state_jobs(state)
    if jobs:
        filtered["pipeline_jobs"] = [
            dict(job) for index, job in enumerate(jobs) if f"pipeline_jobs[{index}]" not in legacy_sources
        ]
        filtered.pop("jobs", None)
    events = _state_events(state)
    if events:
        filtered["pipeline_events"] = [
            _candidate_state_decision_event(
                event,
                authoritative=f"pipeline_events[{index}]" not in legacy_sources,
                source=f"pipeline_events[{index}]",
                legacy_sources=legacy_sources,
            )
            for index, event in enumerate(events)
        ]
        filtered.pop("events", None)
    if filtered.get("pipeline_jobs") == []:
        _strip_top_level_pipeline_decision_fields(filtered)
    if filtered.get("pipeline_events") == [] and not filtered.get("pipeline_jobs"):
        _strip_top_level_pipeline_decision_fields(filtered)
    return filtered


def _candidate_state_source_has_authoritative_ancestor(source: str, authoritative_sources: set[str]) -> bool:
    current = source
    while "." in current:
        current = current.rsplit(".", 1)[0]
        if current in authoritative_sources:
            return True
    return False


def _candidate_state_source_allows_nested_authority(source: str) -> bool:
    return source != "candidate_state" and not re.fullmatch(r"pipeline_events\[\d+\]", source)


def _candidate_state_decision_event(
    event: Mapping[str, Any],
    *,
    authoritative: bool,
    source: str,
    legacy_sources: set[str],
) -> dict[str, Any]:
    if authoritative:
        return dict(event)
    sanitized: dict[str, Any] = {}
    for key in ("event_id", "entity_id", "created_at", "updated_at"):
        value = event.get(key)
        if value not in (None, ""):
            sanitized[key] = value
    details = event.get("details")
    if isinstance(details, Mapping):
        details_payload: dict[str, Any] = {}
        for key in ("stage", "job_type"):
            value = details.get(key)
            if value not in (None, ""):
                details_payload[key] = value
        for key in ("task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            nested_source = f"{source}.details.{key}"
            if isinstance(value, Mapping) and nested_source not in legacy_sources:
                details_payload[key] = value
        task_results = [
            task
            for task_index, task in enumerate(_bounded_task_result_rows(details))
            if f"{source}.details.task_results[{task_index}]" not in legacy_sources
        ]
        if task_results:
            details_payload["task_results"] = task_results
            details_payload["task_results_total"] = len(task_results)
            details_payload["task_results_included"] = len(task_results)
            details_payload["task_results_limit"] = CANDIDATE_STATE_TASK_RESULT_LIMIT
            details_payload["task_results_overflow"] = False
        if details_payload:
            sanitized["details"] = details_payload
    return sanitized


def _strip_top_level_candidate_state_decision_fields(state: dict[str, Any]) -> None:
    _strip_top_level_hydro_decision_fields(state)
    _strip_top_level_pipeline_decision_fields(state)
    for key in (
        "retry_limit",
        "max_retries",
        "cycle_status",
        "forecast_cycle_status",
        "forcing_status",
        "forcing_version_status",
    ):
        state.pop(key, None)


def _strip_top_level_hydro_decision_fields(state: dict[str, Any]) -> None:
    for key in (
        "hydro_status",
        "hydro_run_status",
        "output_uri",
        "durable_output_uri",
        "hydro_error_code",
        "hydro_error_message",
        "durable_shud_output_exists",
        "force_native_shud_rerun",
        "force_rerun",
        "force_shud_rerun",
    ):
        state.pop(key, None)


def _strip_top_level_pipeline_decision_fields(state: dict[str, Any]) -> None:
    for key in (
        "active_slurm_jobs",
        "pipeline_status",
        "job_status",
        "status",
        "failed_stage",
        "stage",
        "restart_stage",
        "error_code",
        "reason_code",
        "failure_reason",
        "last_error",
        "previous_error",
        "error_message",
        "message",
        "retry_attempt",
        "attempt",
        "retry_count",
        "manual_retry",
        "manual_retry_marker",
        "manual_retry_requested_by",
        "manual_retry_request_id",
        "manual_retry_reason",
        "manual_retry_created_at",
        "manual_retry_requested_at",
        "prior_failure_reason",
        "retryable",
        "permanent",
        "failure_classifier",
        "classifier",
        "array_task_id",
        "task_id",
        "original_task_id",
        "slurm_job_id",
        "successful_sibling_outputs_reused",
        "shared_cycle_aggregate",
        "shared_cycle_ambiguous_failure",
    ):
        state.pop(key, None)


def _pipeline_terminal_success_is_candidate_scoped(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
) -> bool:
    if state.get("shared_cycle_aggregate") is True:
        return False
    matching_jobs = [
        job
        for job in _state_jobs(state)
        if str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        in TERMINAL_PIPELINE_SUCCESS_STATUSES
    ]
    if not matching_jobs:
        return not _has_candidate_task_failure(state)
    for job in reversed(matching_jobs):
        run_id = str(job.get("run_id") or "")
        model_id = job.get("model_id")
        if run_id == candidate.run_id:
            return True
        if str(model_id or "") == candidate.model_id:
            return True
        if run_id.startswith("cycle_") and model_id in (None, ""):
            return False
    return False


def _terminal_hydro_truth_supersedes_failure(state: Mapping[str, Any]) -> bool:
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        hydro_truth_time = _first_state_datetime(hydro_run, "updated_at", "finished_at", "created_at")
    else:
        hydro_truth_time = None
    if hydro_truth_time is None:
        return not _state_has_failure_signal(state)
    failure_truth_time = _latest_failure_truth_timestamp(state)
    return failure_truth_time is None or hydro_truth_time >= failure_truth_time


def _latest_failure_truth_timestamp(state: Mapping[str, Any]) -> datetime | None:
    timestamps: list[datetime] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if status not in FAILED_PIPELINE_STATUSES and not job.get("error_code"):
            continue
        timestamp = _first_state_datetime(job, "updated_at", "finished_at", "submitted_at", "created_at")
        if timestamp is not None:
            timestamps.append(timestamp)
    for event in _state_events(state):
        if _event_is_manual_retry_marker(event):
            continue
        details = event.get("details")
        details_mapping = details if isinstance(details, Mapping) else {}
        status = str(
            event.get("status_to")
            or details_mapping.get("status_to")
            or details_mapping.get("status")
            or details_mapping.get("state")
            or ""
        )
        if status not in FAILED_PIPELINE_STATUSES and not details_mapping.get("error_code"):
            continue
        timestamp = _first_state_datetime(event, "created_at", "updated_at", "finished_at", "submitted_at")
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def _has_candidate_task_failure(state: Mapping[str, Any]) -> bool:
    for event in _state_events(state):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task in _bounded_task_result_rows(details):
            if str(task.get("status") or task.get("state") or "") not in {"", "succeeded"}:
                return True
    return False


def _bounded_active_slurm_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    max_jobs: int,
) -> list[dict[str, Any]]:
    bounded = [_evidence_safe(dict(job)) for job in list(jobs)[: max(int(max_jobs), 1)] if isinstance(job, Mapping)]
    total = len(jobs)
    if total > max_jobs:
        bounded.append(
            {
                "overflow": True,
                "reason": "active_slurm_job_limit_applied",
                "returned": len(bounded),
                "total": total,
                "limit": max_jobs,
            }
        )
    return bounded


def _candidate_state_evidence(candidate: SchedulerCandidate, state: Mapping[str, Any]) -> dict[str, Any]:
    state = _bounded_candidate_state(state)
    jobs = [_job_state_evidence(job) for job in _state_jobs(state)]
    events = [_evidence_safe(event) for event in _state_events(state)]
    identity_validation = _candidate_state_identity_validation(candidate, state)
    evidence = {
        "candidate_identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "canonical_product_id": _candidate_canonical_product_id(candidate),
            "forcing_version_id": candidate.forcing_version_id,
            "hydro_run_id": candidate.run_id,
            "published_manifest_id": _candidate_published_manifest_id(candidate),
            "source_id": candidate.source_id,
            "source": candidate.source_id,
            "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
            "cycle_time": _format_utc(candidate.cycle_time_utc),
            "model_id": candidate.model_id,
            "scenario_id": candidate.scenario_id,
            "basin_id": candidate.basin_id,
            "basin_version_id": candidate.basin_version_id,
            "river_network_version_id": candidate.river_network_version_id,
        },
        "production_identity_validation": identity_validation,
        "pipeline_jobs": jobs,
        "pipeline_events": events,
        "hydro_run": _optional_mapping_state(
            state.get("hydro_run"),
            defaults={
                "run_id": state.get("run_id") or candidate.run_id,
                "status": _state_status(state, "hydro_status", "hydro_run_status"),
                "output_uri": _state_output_uri(state),
                "error_code": state.get("hydro_error_code"),
                "error_message": state.get("hydro_error_message"),
            },
        ),
        "forcing_version": _optional_mapping_state(
            state.get("forcing_version"),
            defaults={
                "forcing_version_id": state.get("forcing_version_id") or candidate.forcing_version_id,
                "status": _state_status(state, "forcing_status", "forcing_version_status"),
            },
        ),
        "forecast_cycle": _optional_mapping_state(
            state.get("forecast_cycle"),
            defaults={
                "cycle_id": state.get("cycle_id") or candidate.cycle_id,
                "status": _state_status(state, "cycle_status", "forecast_cycle_status"),
            },
        ),
        "manual_retry": _manual_retry_payload(state),
        "retry": {
            "attempt": _state_retry_attempt(state),
            "retry_limit": _state_retry_limit(state),
        },
    }
    overflow = _state_overflow_evidence(state)
    if overflow:
        evidence["state_bounds"] = overflow
    return evidence


def _candidate_state_identity_validation(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    state = _bounded_candidate_state(state)
    expected = _candidate_production_identity(candidate)
    containers: list[tuple[str, Mapping[str, Any]]] = [("candidate_state", state)]
    for key in ("hydro_run", "forcing_version", "forecast_cycle", "published_manifest", "canonical_product"):
        value = state.get(key)
        if isinstance(value, Mapping):
            containers.append((key, value))
    for key in ("pipeline_job", "job"):
        value = state.get(key)
        if isinstance(value, Mapping):
            containers.append((key, value))
    for index, job in enumerate(_state_jobs(state)):
        containers.append((f"pipeline_jobs[{index}]", job))
    for index, event in enumerate(_state_events(state)):
        containers.extend(_event_identity_containers(index, event))
    mismatches: list[dict[str, Any]] = []
    compared: dict[str, dict[str, Any]] = {}
    legacy_non_authoritative: list[str] = []
    records = [
        {
            "source": source,
            "payload": payload,
            "authoritative": _state_row_has_authoritative_candidate_proof(
                expected,
                payload,
                include_nested=_candidate_state_source_allows_nested_authority(source),
            ),
        }
        for source, payload in containers
    ]
    authoritative_sources = {str(record["source"]) for record in records if record["authoritative"] is True}
    for record in records:
        source = str(record["source"])
        payload = record["payload"]
        if not isinstance(payload, Mapping):
            continue
        authoritative = record["authoritative"] is True
        if authoritative or _state_row_has_m23_comparison_evidence(payload):
            validation_payload = _legacy_compatible_state_row(expected, payload)
            try:
                fields = validate_compatible_production_identity(expected, validation_payload)
            except ProductionContractError as exc:
                mismatches.append({"source": source, **exc.to_dict()})
                continue
            if fields:
                compared[source] = fields
        if (
            bool(payload)
            and not authoritative
            and not _candidate_state_source_has_authoritative_ancestor(source, authoritative_sources)
        ):
            legacy_non_authoritative.append(source)
    return {
        "schema_version": "nhms.production.identity_validation.v1",
        "status": "mismatch" if mismatches else "compatible",
        "checked_sources": [source for source, _payload in containers],
        "compared": compared,
        "legacy_non_authoritative": legacy_non_authoritative,
        "mismatches": mismatches,
    }


def _bounded_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(state)
    events = _state_events(bounded)
    if events:
        bounded["pipeline_events"] = [_bounded_candidate_event(event) for event in events]
        bounded.pop("events", None)
    return bounded


def _bounded_candidate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload


def _event_identity_containers(index: int, event: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [(f"pipeline_events[{index}]", event)]
    details = event.get("details")
    if not isinstance(details, Mapping):
        return containers
    identity = details.get("identity")
    if isinstance(identity, Mapping):
        containers.append((f"pipeline_events[{index}].details.identity", identity))
    containers.append((f"pipeline_events[{index}].details", details))
    for task_index, task in enumerate(_bounded_task_result_rows(details)):
        containers.append((f"pipeline_events[{index}].details.task_results[{task_index}]", task))
        task_identity = task.get("identity")
        if isinstance(task_identity, Mapping):
            containers.append(
                (f"pipeline_events[{index}].details.task_results[{task_index}].identity", task_identity)
            )
    for key in ("task_identity", "failed_task", "failed_task_identity"):
        value = details.get(key)
        if isinstance(value, Mapping):
            containers.append((f"pipeline_events[{index}].details.{key}", value))
    return containers


def _legacy_non_authoritative_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    return bool(row) and not _state_row_has_authoritative_candidate_proof(expected, row)


def _state_row_has_authoritative_candidate_proof(
    expected: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    include_nested: bool = True,
) -> bool:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if _state_values_have_authoritative_candidate_proof(row_values, expected_values):
        return True
    if not include_nested:
        return False
    for nested in _nested_state_identity_payloads(row):
        nested_values = _legacy_identity_values(nested)
        if _state_values_have_authoritative_candidate_proof(nested_values, expected_values):
            return True
    return False


def _state_values_have_authoritative_candidate_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    if _state_values_have_complete_m23_identity(row_values, expected_values):
        return True
    if _state_values_have_candidate_scoped_m23_proof(row_values, expected_values):
        return True
    return _legacy_values_prove_same_candidate(row_values, expected_values)


def _state_values_have_complete_m23_identity(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in PRODUCTION_IDENTITY_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if value in (None, "") or expected in (None, "") or value != expected:
            return False
    return True


def _state_values_have_candidate_scoped_m23_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    return any(
        row_values.get(field) not in (None, "") and row_values.get(field) == expected_values.get(field)
        for field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS
    )


def _legacy_values_prove_same_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    for identity_field in ("model_id", "source", "cycle_time"):
        value = row_values.get(identity_field)
        expected_value = expected_values.get(identity_field)
        if value not in (None, "") and expected_value not in (None, "") and value != expected_value:
            return False
    run_id = row_values.get("run_id")
    expected_run_id = expected_values.get("run_id")
    if run_id not in (None, ""):
        if run_id == expected_run_id:
            return True
        if not _stage_cycle_run_matches_candidate(run_id, expected_values):
            return False
        return True
    source = row_values.get("source")
    cycle_time = row_values.get("cycle_time")
    model_id = row_values.get("model_id")
    if source in (None, "") or cycle_time in (None, ""):
        return False
    if source != expected_values.get("source") or cycle_time != expected_values.get("cycle_time"):
        return False
    return model_id in (None, "", expected_values.get("model_id"))


def _state_row_has_m23_comparison_fields(values: Mapping[str, str]) -> bool:
    return any(
        field in values
        for field in (
            *STATE_M23_COMPARISON_FIELDS,
            *PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
        )
    )


def _state_row_has_m23_comparison_evidence(row: Mapping[str, Any]) -> bool:
    if _state_row_has_m23_comparison_fields(_legacy_identity_values(row)):
        return True
    return any(
        _state_row_has_m23_comparison_fields(_legacy_identity_values(nested))
        for nested in _nested_state_identity_payloads(row)
    )


def _nested_state_identity_payloads(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
        value = row.get(key)
        if isinstance(value, Mapping):
            payloads.append(value)
    details = row.get("details")
    if isinstance(details, Mapping):
        payloads.append(details)
        for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                payloads.append(value)
        for task in _bounded_task_result_rows(details):
            payloads.append(task)
            identity = task.get("identity")
            if isinstance(identity, Mapping):
                payloads.append(identity)
    return payloads


def _bounded_task_result_rows(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]


def _bounded_task_result_sample(
    details: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Any]] | None:
    task_results = details.get("task_results")
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return None
    task_rows: list[Mapping[str, Any]] = []
    observed_count = 0
    overflow = False
    for index, task in enumerate(task_results):
        observed_count = index + 1
        if index >= CANDIDATE_STATE_TASK_RESULT_LIMIT:
            overflow = True
            break
        if isinstance(task, Mapping):
            task_rows.append(dict(task))
    reported_total = _coerce_optional_nonnegative_int(details.get("task_results_total"))
    total = max(reported_total, observed_count) if reported_total is not None else observed_count
    included = len(task_rows)
    overflow = overflow or total > included
    metadata: dict[str, Any] = {
        "task_results_total": total,
        "task_results_included": included,
        "task_results_limit": CANDIDATE_STATE_TASK_RESULT_LIMIT,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata


def _legacy_compatible_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any]:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if not _stage_cycle_run_matches_candidate(row_values.get("run_id"), expected_values):
        return row
    payload = dict(row)
    payload.pop("run_id", None)
    identity = payload.get("identity")
    if isinstance(identity, Mapping):
        identity_payload = dict(identity)
        identity_payload.pop("run_id", None)
        payload["identity"] = identity_payload
    return payload


def _legacy_identity_values(payload: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    aliases: dict[str, tuple[tuple[str, ...], ...]] = {
        "run_id": (("run_id",), ("identity", "run_id")),
        "model_id": (("model_id",), ("identity", "model_id")),
        "basin_id": (("basin_id",), ("identity", "basin_id")),
        "source": (("source",), ("source_id",), ("identity", "source"), ("identity", "source_id")),
        "cycle_time": (
            ("cycle_time",),
            ("cycle_time_utc",),
            ("identity", "cycle_time"),
            ("identity", "cycle_time_utc"),
        ),
        "basin_version_id": (("basin_version_id",), ("identity", "basin_version_id")),
        "river_network_version_id": (("river_network_version_id",), ("identity", "river_network_version_id")),
        "canonical_product_id": (("canonical_product_id",), ("identity", "canonical_product_id")),
        "forcing_version_id": (("forcing_version_id",), ("identity", "forcing_version_id")),
        "hydro_run_id": (("hydro_run_id",), ("identity", "hydro_run_id")),
        "published_manifest_id": (("published_manifest_id",), ("identity", "published_manifest_id")),
        "pipeline_job_id": (("pipeline_job_id",), ("identity", "pipeline_job_id")),
        "pipeline_event_id": (("pipeline_event_id",), ("identity", "pipeline_event_id")),
    }
    for identity_field, field_aliases in aliases.items():
        value = _first_nested_state_value(payload, field_aliases)
        if value in (None, ""):
            continue
        if identity_field == "source":
            try:
                value = normalize_source_id(str(value))
            except ValueError:
                value = str(value).strip()
        elif identity_field == "cycle_time":
            try:
                value = _format_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                try:
                    value = _format_utc(datetime.strptime(str(value), "%Y%m%d%H").replace(tzinfo=UTC))
                except ValueError:
                    value = str(value).strip()
        else:
            value = str(value).strip()
        if value:
            values[identity_field] = value
    job_id = payload.get("job_id") or payload.get("entity_id")
    if "pipeline_job_id" not in values and job_id not in (None, "") and _looks_like_production_job_id(job_id):
        values["stage_job_id"] = str(job_id).strip()
    event_id = payload.get("event_id")
    if "pipeline_event_id" not in values and event_id not in (None, ""):
        values["stage_event_id"] = str(event_id).strip()
    return values


def _stage_cycle_run_matches_candidate(run_id: str | None, expected_values: Mapping[str, str]) -> bool:
    if run_id in (None, ""):
        return False
    source = str(expected_values.get("source") or "").lower()
    cycle_time = str(expected_values.get("cycle_time") or "")
    model_id = str(expected_values.get("model_id") or "")
    if not source or not cycle_time or not model_id:
        return False
    try:
        compact_cycle = format_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return False
    prefix = f"cycle_{source}_{compact_cycle}"
    text = str(run_id)
    return text == prefix or (
        text.startswith(f"{prefix}_") and (text.endswith(f"_{model_id}") or f"_{model_id}_" in text)
    )


def _first_nested_state_value(payload: Mapping[str, Any], aliases: Sequence[tuple[str, ...]]) -> Any:
    for path in aliases:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


def _looks_like_production_job_id(value: Any) -> bool:
    text = str(value or "")
    return text.startswith(("job_fcst_", "job_cycle_"))


def _candidate_state_has_identity_mismatch(evidence: Mapping[str, Any]) -> bool:
    validation = evidence.get("production_identity_validation")
    return isinstance(validation, Mapping) and validation.get("status") == "mismatch"


def _state_jobs(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_jobs") or state.get("jobs")
    max_jobs = _state_job_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_jobs]
    single = state.get("pipeline_job") or state.get("job")
    if isinstance(single, Mapping):
        return [dict(single)]
    fields = {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "pipeline_status",
        "job_status",
        "stage",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "log_uri",
    }
    if any(key in state for key in fields):
        return [dict(state)]
    return []


def _state_events(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_events") or state.get("events")
    max_events = _state_event_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_events]
    return []


def _state_job_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("job_limit"), default=DEFAULT_CANDIDATE_STATE_JOB_LIMIT), 1)


def _state_event_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("event_limit"), default=DEFAULT_CANDIDATE_STATE_EVENT_LIMIT), 1)


def _state_overflow_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "job_limit": _state_job_limit(state),
        "event_limit": _state_event_limit(state),
    }
    overflow = False
    for count_key, limit_key, output_key in (
        ("pipeline_jobs_total", "job_limit", "pipeline_jobs"),
        ("pipeline_events_total", "event_limit", "pipeline_events"),
    ):
        count = state.get(count_key)
        if count in (None, ""):
            continue
        count_value = _coerce_int(count, default=0)
        limit_value = int(evidence[limit_key])
        evidence[f"{output_key}_total"] = count_value
        evidence[f"{output_key}_returned"] = min(count_value, limit_value)
        if count_value > limit_value:
            evidence[f"{output_key}_overflow"] = True
            overflow = True
    if state.get("state_truncated") is True:
        overflow = True
        evidence["state_truncated"] = True
    if not overflow:
        return {}
    evidence["bounded"] = True
    evidence["overflow"] = True
    evidence["reason"] = "candidate_state_row_limit_applied"
    return evidence


def _job_state_evidence(job: Mapping[str, Any]) -> dict[str, Any]:
    kept = {
        key: job.get(key)
        for key in (
            "job_id",
            "pipeline_job_id",
            "pipeline_event_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "array_task_id",
            "model_id",
            "basin_id",
            "source",
            "source_id",
            "cycle_time",
            "basin_version_id",
            "river_network_version_id",
            "canonical_product_id",
            "forcing_version_id",
            "hydro_run_id",
            "published_manifest_id",
            "status",
            "stage",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
        )
        if key in job and job.get(key) is not None
    }
    return _evidence_safe(kept)


def _optional_mapping_state(value: Any, *, defaults: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = dict(value) if isinstance(value, Mapping) else {}
    for key, fallback in defaults.items():
        if fallback not in (None, ""):
            payload.setdefault(key, fallback)
    payload = {key: val for key, val in payload.items() if val not in (None, "")}
    return _evidence_safe(payload) if payload else None


def _state_status(state: Mapping[str, Any], *keys: str) -> str | None:
    explicit_key_seen = False
    for key in keys:
        explicit_key_seen = explicit_key_seen or key in state
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    if explicit_key_seen:
        return None
    for job in reversed(_state_jobs(state)):
        for key in keys:
            value = job.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _state_output_uri(state: Mapping[str, Any]) -> str | None:
    for container_key in ("hydro_run", "outputs", "runtime_outputs"):
        value = state.get(container_key)
        if isinstance(value, Mapping) and value.get("output_uri") not in (None, ""):
            return str(value["output_uri"])
    value = state.get("output_uri") or state.get("durable_output_uri")
    return str(value) if value not in (None, "") else None


def _state_active_jobs(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = state.get("active_slurm_jobs")
    if isinstance(explicit, Sequence) and not isinstance(explicit, str | bytes | bytearray):
        return [_evidence_safe(dict(job)) for job in explicit if isinstance(job, Mapping)]
    active: list[dict[str, Any]] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if job.get("slurm_job_id") and status in ACTIVE_PIPELINE_STATUSES:
            active.append(_job_state_evidence(job))
    return active


def _manual_retry_requested(state: Mapping[str, Any]) -> bool:
    marker = _latest_manual_retry_marker(state)
    if marker is None:
        return False
    blocker = _latest_manual_retry_blocker(state)
    if blocker is None:
        return True
    return _manual_retry_marker_overrides_blocker(marker, blocker)


def _manual_retry_markers(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    marker = state.get("manual_retry") or state.get("manual_retry_marker")
    if isinstance(marker, Mapping):
        if marker.get("marker") or marker.get("requested") or marker.get("enabled"):
            markers.append(
                _manual_retry_marker_record(
                    marker,
                    state=state,
                    source="state",
                    order=-1,
                    default_attempt=_state_retry_attempt(state) + 1,
                )
            )
    elif marker is not None and bool(marker):
        markers.append(
            _manual_retry_marker_record(
                {},
                state=state,
                source="state",
                order=-1,
                default_attempt=_state_retry_attempt(state) + 1,
            )
        )
    for order, event in enumerate(_state_events(state)):
        details = event.get("details")
        if event.get("event_type") in {"retry", "manual_retry"} and isinstance(details, Mapping):
            if details.get("trigger") == "manual" or details.get("manual_retry_marker") is True:
                markers.append(
                    _manual_retry_marker_record(
                        details,
                        state=event,
                        source="event",
                        order=order,
                        event_id=event.get("event_id"),
                        entity_id=event.get("entity_id"),
                    )
                )
    return markers


def _manual_retry_marker_record(
    payload: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    source: str,
    order: int,
    default_attempt: int | None = None,
    event_id: Any = None,
    entity_id: Any = None,
) -> dict[str, Any]:
    timestamp = _first_state_datetime(
        payload,
        "created_at",
        "requested_at",
        "updated_at",
        "submitted_at",
    ) or _first_state_datetime(
        state,
        "manual_retry_created_at",
        "manual_retry_requested_at",
        "created_at",
        "updated_at",
        "submitted_at",
    )
    attempt = _first_state_int(payload, "new_attempt", "retry_count", "attempt", default=default_attempt)
    return {
        "source": source,
        "timestamp": timestamp,
        "attempt": attempt,
        "previous_job_id": _first_nonempty(payload, "previous_job_id", "failed_job_id", "job_id"),
        "entity_id": entity_id,
        "event_id": event_id,
        "order": order,
    }


def _latest_manual_retry_marker(state: Mapping[str, Any]) -> dict[str, Any] | None:
    markers = _manual_retry_markers(state)
    if not markers:
        return None
    return max(markers, key=_state_truth_sort_key)


def _latest_manual_retry_blocker(state: Mapping[str, Any]) -> dict[str, Any] | None:
    blockers: list[dict[str, Any]] = []
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if _manual_retry_blocking_pipeline_status(pipeline_status):
        blockers.append(
            _manual_retry_blocker_record(
                state,
                status=pipeline_status,
                source="pipeline_state",
                order=-1,
                attempt=_state_retry_attempt(state),
                active=pipeline_status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if _manual_retry_blocking_hydro_status(hydro_status):
        blockers.append(
            _manual_retry_blocker_record(
                _coerce_mapping_for_state(state.get("hydro_run")) or state,
                status=hydro_status,
                source="hydro_state",
                order=-1,
                attempt=_state_retry_attempt(state),
                active=hydro_status in ACTIVE_HYDRO_STATUSES,
            )
        )
    for order, job in enumerate(_state_jobs(state)):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if not _manual_retry_blocking_pipeline_status(status):
            continue
        blockers.append(
            _manual_retry_blocker_record(
                job,
                status=status,
                source="pipeline_job",
                order=order,
                attempt=_coerce_int(job.get("retry_count"), default=0),
                active=status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    for order, event in enumerate(_state_events(state)):
        if _event_is_manual_retry_marker(event):
            continue
        details = event.get("details")
        details_mapping = details if isinstance(details, Mapping) else {}
        status = str(
            event.get("status_to")
            or details_mapping.get("status_to")
            or details_mapping.get("status")
            or details_mapping.get("state")
            or ""
        )
        if not _manual_retry_blocking_pipeline_status(status):
            continue
        blockers.append(
            _manual_retry_blocker_record(
                {**dict(details_mapping), **dict(event)},
                status=status,
                source="pipeline_event",
                order=order,
                attempt=_first_state_int(details_mapping, "final_retry_count", "retry_count", "attempt", default=0),
                active=status in ACTIVE_PIPELINE_STATUSES,
            )
        )
    if not blockers:
        return None
    return max(blockers, key=_state_truth_sort_key)


def _manual_retry_blocker_record(
    payload: Mapping[str, Any],
    *,
    status: str | None,
    source: str,
    order: int,
    attempt: int | None,
    active: bool,
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "active": active,
        "timestamp": _first_state_datetime(
            payload,
            "updated_at",
            "finished_at",
            "submitted_at",
            "started_at",
            "created_at",
            "event_created_at",
        ),
        "attempt": attempt,
        "job_id": _first_nonempty(payload, "job_id", "pipeline_job_id", "entity_id"),
        "event_id": payload.get("event_id"),
        "order": order,
    }


def _manual_retry_marker_overrides_blocker(marker: Mapping[str, Any], blocker: Mapping[str, Any]) -> bool:
    if blocker.get("active") is True:
        return False
    marker_timestamp = marker.get("timestamp")
    blocker_timestamp = blocker.get("timestamp")
    if isinstance(marker_timestamp, datetime) and isinstance(blocker_timestamp, datetime):
        if marker_timestamp > blocker_timestamp:
            return True
        if marker_timestamp == blocker_timestamp and _state_truth_sequence(marker) > _state_truth_sequence(blocker):
            return True
        return False
    if _manual_retry_marker_bound_to_blocker(marker, blocker):
        return True
    if isinstance(marker_timestamp, datetime) and blocker_timestamp is None:
        return True
    if marker_timestamp is None and blocker_timestamp is None:
        marker_attempt = marker.get("attempt")
        blocker_attempt = blocker.get("attempt")
        if marker_attempt is not None and blocker_attempt is not None:
            return _coerce_int(marker_attempt, default=-1) > _coerce_int(blocker_attempt, default=-1)
        return True
    return False


def _manual_retry_marker_bound_to_blocker(marker: Mapping[str, Any], blocker: Mapping[str, Any]) -> bool:
    if blocker.get("active") is True:
        return False
    marker_attempt = marker.get("attempt")
    blocker_attempt = blocker.get("attempt")
    if marker_attempt is None or blocker_attempt is None:
        return False
    if _coerce_int(marker_attempt, default=-1) <= _coerce_int(blocker_attempt, default=-1):
        return False
    previous_job_id = marker.get("previous_job_id")
    blocker_job_id = blocker.get("job_id")
    if previous_job_id not in (None, "") and blocker_job_id not in (None, ""):
        return str(previous_job_id) == str(blocker_job_id)
    return True


def _manual_retry_blocking_pipeline_status(status: str | None) -> bool:
    return status in ACTIVE_PIPELINE_STATUSES or status in FAILED_PIPELINE_STATUSES or status == "cancelled"


def _manual_retry_blocking_hydro_status(status: str | None) -> bool:
    return status in ACTIVE_HYDRO_STATUSES or status in {"failed", "cancelled", "permanently_failed"}


def _event_is_manual_retry_marker(event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
        return False
    return details.get("trigger") == "manual" or details.get("manual_retry_marker") is True


def _state_truth_sort_key(truth: Mapping[str, Any]) -> tuple[int, datetime, int, int, int]:
    timestamp = truth.get("timestamp")
    parsed = timestamp if isinstance(timestamp, datetime) else datetime.min.replace(tzinfo=UTC)
    return (
        1 if isinstance(timestamp, datetime) else 0,
        parsed,
        _coerce_int(truth.get("attempt"), default=-1),
        _coerce_int(truth.get("event_id"), default=-1),
        _coerce_int(truth.get("order"), default=-1),
    )


def _state_truth_sequence(truth: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _coerce_int(truth.get("event_id"), default=-1),
        _coerce_int(truth.get("order"), default=-1),
    )


def _first_state_datetime(payload: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        parsed = _parse_state_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_state_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _first_state_int(payload: Mapping[str, Any], *keys: str, default: int | None = None) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=default or 0)
    return default


def _first_nonempty(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _coerce_mapping_for_state(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _manual_retry_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    marker = state.get("manual_retry") or state.get("manual_retry_marker")
    payload = dict(marker) if isinstance(marker, Mapping) else {}
    if marker and not payload:
        payload["marker"] = True
    for key in ("requested_by", "request_id", "reason", "created_at"):
        value = state.get(f"manual_retry_{key}") or state.get(key)
        if value not in (None, ""):
            payload.setdefault(key, value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if event.get("event_type") in {"retry", "manual_retry"} and isinstance(details, Mapping):
            if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
                continue
            payload.setdefault("marker", True)
            payload.setdefault("requested", True)
            if details.get("retry_count") not in (None, ""):
                payload.setdefault("new_attempt", _coerce_int(details.get("retry_count"), default=0))
            for key in ("prior_failure_reason", "previous_error", "previous_job_id", "slurm_job_id"):
                value = details.get(key)
                if value not in (None, ""):
                    payload.setdefault(key, value)
            break
    return _evidence_safe(payload)


def _state_retry_attempt(state: Mapping[str, Any]) -> int:
    for key in ("retry_attempt", "attempt", "retry_count"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    jobs = _state_jobs(state)
    if jobs:
        return max(_coerce_int(job.get("retry_count"), default=0) for job in jobs)
    return 0


def _state_retry_limit(state: Mapping[str, Any]) -> int | None:
    for key in ("retry_limit", "max_retries"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    return DEFAULT_RETRY_LIMIT


def _failed_stage(state: Mapping[str, Any]) -> str | None:
    for key in ("failed_stage", "stage", "restart_stage"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for job in reversed(_state_jobs(state)):
        status = str(job.get("status") or "")
        if status in FAILED_PIPELINE_STATUSES and job.get("stage") not in (None, ""):
            return str(job["stage"])
    return None


def _canonical_downstream_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    normalized = DOWNSTREAM_STAGE_ALIASES.get(stage)
    if normalized in DOWNSTREAM_RESTART_STAGES:
        return normalized
    return None


def _durable_shud_output_exists(state: Mapping[str, Any]) -> bool:
    if state.get("durable_shud_output_exists") is not None:
        return bool(state.get("durable_shud_output_exists"))
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if hydro_status in {"succeeded", "parsed", "frequency_done", "published", "complete"}:
        return True
    if _state_output_uri(state):
        for job in _state_jobs(state):
            stage = str(job.get("stage") or job.get("job_type") or "")
            status = str(job.get("status") or "")
            if stage in NATIVE_SHUD_STAGE_ALIASES and status in TERMINAL_PIPELINE_SUCCESS_STATUSES:
                return True
    return False


def _force_native_shud_rerun(state: Mapping[str, Any]) -> bool:
    return bool(state.get("force_native_shud_rerun") or state.get("force_rerun") or state.get("force_shud_rerun"))


def _failure_policy_payload(
    state: Mapping[str, Any],
    *,
    default_error_code: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    error_code = _state_error_code(state) or default_error_code or "UNKNOWN_FAILURE"
    attempt = _state_retry_attempt(state)
    retry_limit = _state_retry_limit(state)
    classification = classify_failure(error_code, attempt=attempt, retry_limit=retry_limit, manual=manual)
    stage = _failed_stage(state)
    explicit_classifier = state.get("failure_classifier") or state.get("classifier")
    if explicit_classifier not in (None, ""):
        classification["classifier"] = str(explicit_classifier)
    if state.get("retryable") is True and not classification["limit_exhausted"]:
        classification["retryable"] = True
        classification["permanent"] = False
    if state.get("permanent") is True:
        classification["retryable"] = False
        classification["permanent"] = True
    return {
        **classification,
        "error_message": _state_error_message(state),
        "stage": stage,
        "task_identity": _state_task_identity(state),
    }


def _state_error_code(state: Mapping[str, Any]) -> str | None:
    for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_code", "reason_code", "failure_reason", "last_error", "previous_error"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(value)
    for job in reversed(_state_jobs(state)):
        value = job.get("error_code") or job.get("reason_code")
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("error_code") or details.get("last_error") or details.get("previous_error")
            if value not in (None, ""):
                return str(value)
    return None


def _state_error_message(state: Mapping[str, Any]) -> str | None:
    for key in ("error_message", "message"):
        value = state.get(key)
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    hydro_run = state.get("hydro_run")
    if isinstance(hydro_run, Mapping):
        for key in ("error_message", "message"):
            value = hydro_run.get(key)
            if value not in (None, ""):
                return str(_evidence_safe(str(value)))
    for job in reversed(_state_jobs(state)):
        value = job.get("error_message")
        if value not in (None, ""):
            return str(_evidence_safe(str(value)))
    return None


def _state_task_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
        value = state.get(key)
        if value not in (None, ""):
            identity[key] = value
    if identity:
        return _evidence_safe(identity)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for key in ("task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                for nested_key in ("task_id", "array_task_id", "original_task_id", "stage", "job_id", "slurm_job_id"):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        identity[nested_key] = nested_value
                if identity:
                    return _evidence_safe(identity)
        for task in _bounded_task_result_rows(details):
            status = str(task.get("status") or task.get("state") or "")
            if status in {"succeeded", ""}:
                continue
            identity["array_task_id"] = task.get("array_task_id", task.get("task_id"))
            identity["task_id"] = task.get("task_id", task.get("array_task_id"))
            if details.get("stage") not in (None, ""):
                identity["stage"] = details.get("stage")
            if task.get("slurm_job_id") not in (None, ""):
                identity["slurm_job_id"] = task.get("slurm_job_id")
            return _evidence_safe(identity)
    for job in reversed(_state_jobs(state)):
        for key in ("array_task_id", "stage", "job_id", "slurm_job_id"):
            value = job.get(key)
            if value not in (None, ""):
                identity[key] = value
        if identity:
            return _evidence_safe(identity)
    return {}


def _permanent_reason(state: Mapping[str, Any], failure: Mapping[str, Any]) -> str:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    if pipeline_status == "permanently_failed":
        return "permanent_failure_guard"
    if failure.get("classifier") == "policy_blocked":
        return "policy_blocked"
    if failure.get("limit_exhausted") and failure.get("retryable") is False:
        if str(failure.get("reason_code") or "") in TRANSIENT_RETRY_REASON_CODES:
            return "retry_limit_exhausted"
    return "permanent_failure_guard"


def _prior_failure_reason(state: Mapping[str, Any]) -> str | None:
    for key in ("prior_failure_reason", "previous_error", "last_error", "error_code"):
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if isinstance(details, Mapping):
            value = details.get("prior_failure_reason") or details.get("previous_error") or details.get("last_error")
            if value not in (None, ""):
                return str(value)
    return None


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _restart_compatible_candidate_cohorts(
    candidates: Sequence[SchedulerCandidate],
) -> list[tuple[tuple[int, str], list[SchedulerCandidate]]]:
    cohorts: dict[tuple[int, str], list[SchedulerCandidate]] = {}
    for candidate in candidates:
        restart_stage = _candidate_restart_stage(candidate)
        key = _candidate_restart_cohort_key(restart_stage)
        cohorts.setdefault(key, []).append(candidate)
    return sorted(
        cohorts.items(),
        key=lambda item: (item[0][0], item[0][1], [candidate.model_id for candidate in item[1]]),
    )


def _candidate_restart_stage(candidate: SchedulerCandidate) -> str | None:
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return None
    return _canonical_downstream_stage(
        str(state_evidence.get("restart_stage") or state_evidence.get("restart_from_stage") or "")
    )


def _candidate_restart_cohort_key(restart_stage: str | None) -> tuple[int, str]:
    if restart_stage is None:
        return (0, "full")
    stage_order = {stage: index for index, stage in enumerate(DOWNSTREAM_RESTART_STAGES, start=1)}
    return (stage_order.get(restart_stage, len(stage_order) + 1), restart_stage)


def _candidate_execution_cohort_run_id(source_id: str, cycle_time: datetime, cohort_key: tuple[int, str]) -> str:
    stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", cohort_key[1]).strip("._-") or "full"
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}_{stage}"


def _candidate_execution_cohorts(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidates: Sequence[SchedulerCandidate],
) -> list[tuple[list[SchedulerCandidate], str | None]]:
    if cohort_key[1] == "full":
        return [(list(candidates), None)]
    return [
        ([candidate], _candidate_execution_cohort_run_id_for_candidate(source_id, cycle_time, cohort_key, candidate))
        for candidate in candidates
    ]


def _candidate_execution_cohort_run_id_for_candidate(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidate: SchedulerCandidate,
) -> str:
    stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", cohort_key[1]).strip("._-") or "full"
    model_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate.model_id).strip("._-") or "candidate"
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}_{stage}_{model_id}"


def _downstream_retry_evidence(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _durable_shud_output_exists(state):
        return None
    failed_stage = _canonical_downstream_stage(_failed_stage(state))
    if failed_stage is None:
        return None
    if _force_native_shud_rerun(state):
        return None
    failure = _failure_policy_payload(state, default_error_code=f"{failed_stage.upper()}_FAILED")
    if _downstream_failure_restartable(failure):
        failure = {
            **failure,
            "retryable": True,
            "permanent": False,
            "limit_exhausted": False,
        }
    if failure["permanent"]:
        return None
    return {
        **base_evidence,
        "decision": "retry_downstream",
        "reason": "resume_downstream_after_durable_shud",
        "restart_stage": failed_stage,
        "restart_from_stage": failed_stage,
        "native_shud_resubmitted": False,
        "durable_shud_output_reused": True,
        "durable_output_uri": _state_output_uri(state),
        "force_native_shud_rerun": False,
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
    }


def _downstream_failure_restartable(failure: Mapping[str, Any]) -> bool:
    if failure.get("limit_exhausted") is True:
        return False
    if str(failure.get("classifier") or "") in {"malformed_input", "policy_blocked"}:
        return False
    reason_code = str(failure.get("reason_code") or "").upper()
    if reason_code in {"INVALID_MANIFEST", "MANIFEST_SCHEMA_INVALID", "MALFORMED_INPUT", "POLICY_BLOCKED"}:
        return False
    return True


def _retry_failure_evidence(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state)
    return {
        **base_evidence,
        "decision": "retry_failed",
        "reason": "retry_failed_candidate",
        "stage": _failed_stage(state),
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": failure["retryable"],
            "manual_retry_required": failure["permanent"],
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "reuse": {
            "successful_sibling_outputs_reused": bool(state.get("successful_sibling_outputs_reused")),
            "durable_output_reused": _durable_shud_output_exists(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _permanent_failure_evidence(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _state_has_failure_signal(state):
        return None
    failure = _failure_policy_payload(state)
    if not failure["permanent"]:
        return None
    return {
        **base_evidence,
        "decision": "permanent_failure",
        "reason": _permanent_reason(state, failure),
        "stage": _failed_stage(state),
        "task_identity": _state_task_identity(state),
        "failure": failure,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": failure["attempt"],
            "retry_limit": failure["retry_limit"],
        },
        "manual_retry_required": True,
        "prior_failure_reason": failure["reason_code"],
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _state_has_failure_signal(state: Mapping[str, Any]) -> bool:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status in FAILED_PIPELINE_STATUSES or hydro_status in {"failed", "permanently_failed"}:
        return True
    if pipeline_status is not None:
        return False
    if _failed_stage(state) is not None and _state_error_code(state) not in (None, ""):
        return True
    return False


def _cancelled_state_evidence(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    pipeline_status = _state_status(state, "pipeline_status", "job_status", "status")
    hydro_status = _state_status(state, "hydro_status", "hydro_run_status")
    if pipeline_status != "cancelled" and hydro_status != "cancelled":
        return None
    return {
        **base_evidence,
        "decision": "cancelled_manual_retry_required",
        "reason": "manual_retry_required_after_cancelled",
        "terminal_status": "cancelled",
        "cancelled": True,
        "replacement_submitted": False,
        "manual_retry_required": True,
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": _state_retry_attempt(state),
            "retry_limit": _state_retry_limit(state),
        },
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _manual_retry_state_evidence(
    candidate: SchedulerCandidate,
    state: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failure = _failure_policy_payload(state, manual=True)
    manual = _manual_retry_payload(state)
    prior_failure = _prior_failure_reason(state) or failure["reason_code"]
    previous_attempt = _state_retry_attempt(state)
    new_attempt = _manual_retry_new_attempt(state, previous_attempt=previous_attempt)
    return {
        **base_evidence,
        "decision": "manual_retry",
        "reason": "manual_retry_requested",
        "manual_retry": {
            **manual,
            "marker": True,
            "allowed": True,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "failure": {
            **failure,
            "prior_failure_reason": prior_failure,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
        },
        "retry_policy": {
            "automatic_retry_allowed": False,
            "manual_retry_required": False,
            "manual_retry_marker": True,
            "attempt": new_attempt,
            "previous_attempt": previous_attempt,
            "new_attempt": new_attempt,
            "retry_limit": failure["retry_limit"],
        },
        "prior_failure_reason": prior_failure,
        "identity": {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
        },
    }


def _manual_retry_new_attempt(state: Mapping[str, Any], *, previous_attempt: int) -> int:
    manual = _manual_retry_payload(state)
    for key in ("new_attempt", "retry_count"):
        value = manual.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=previous_attempt + 1)
    for event in reversed(_state_events(state)):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        if event.get("event_type") not in {"retry", "manual_retry"}:
            continue
        if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
            continue
        value = details.get("retry_count")
        if value not in (None, ""):
            return _coerce_int(value, default=previous_attempt + 1)
    return previous_attempt + 1


def _candidate_for(
    *,
    discovery: CycleDiscovery,
    model: RegisteredSchedulerModel,
    horizon: Mapping[str, Any],
) -> SchedulerCandidate:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    scenario_id = scenario_for_source(source_id)
    candidate_id = f"{source_id}:{_format_utc(cycle_time)}:{model.model_id}:{scenario_id}"
    return SchedulerCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, cycle_time),
        cycle_time_utc=cycle_time,
        model_id=model.model_id,
        basin_id=model.basin_id,
        basin_version_id=model.basin_version_id,
        river_network_version_id=model.river_network_version_id,
        segment_count=model.segment_count,
        model_package_uri=model.model_package_uri,
        resource_profile=model.resource_profile,
        display_capabilities=model.display_capabilities,
        frequency_capabilities=model.frequency_capabilities,
        horizon=horizon,
        scenario_id=scenario_id,
        run_id=f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        forcing_version_id=f"forc_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        status="selected",
    )


def _blocked_candidate(
    candidate: SchedulerCandidate,
    reason: str,
    *,
    state_evidence: Mapping[str, Any] | None = None,
) -> SchedulerCandidate:
    evidence = _merge_state_evidence(candidate.state_evidence, state_evidence)
    return replace(candidate, status="blocked", reason=reason, state_evidence=evidence)


def _candidate_with_state_evidence(
    candidate: SchedulerCandidate,
    state_evidence: Mapping[str, Any],
) -> SchedulerCandidate:
    return replace(
        candidate,
        state_evidence=_merge_state_evidence(candidate.state_evidence, state_evidence),
    )


def _merge_state_evidence(
    existing: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in dict(extra or {}).items():
        if key in merged and isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return _evidence_safe(merged)


def _candidate_basin_manifest(
    candidate: SchedulerCandidate,
    *,
    output_uri: str,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    resource_profile = dict(candidate.resource_profile)
    manifest = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time": format_cycle_time(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "model_package_uri": candidate.model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(candidate),
        "resource_profile": dict(candidate.resource_profile),
        "display_capabilities": dict(candidate.display_capabilities),
        "frequency_capabilities": dict(candidate.frequency_capabilities),
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
        "forecast_horizon_hours": candidate.horizon.get("forecast_horizon_hours")
        or candidate.horizon.get("max_lead_hours"),
        "max_lead_hours": candidate.horizon.get("max_lead_hours"),
        "station_count": _candidate_station_count(candidate),
        "station_ids": _candidate_station_ids(candidate),
        "frequency_curves_available": _nested_bool(
            candidate.frequency_capabilities,
            "curves_available",
            fallback=_nested_bool(candidate.frequency_capabilities, "return_periods"),
        ),
        "warning_thresholds_available": _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available"),
        "optional_weather_available": _nested_bool(candidate.display_capabilities, "optional_weather_available"),
        "output_key": _candidate_output_key(candidate),
        "output_uri": output_uri,
    }
    if orchestration_run_id not in (None, ""):
        manifest["orchestration_run_id"] = orchestration_run_id
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        manifest["pipeline_job_id"] = pipeline_job_id
    if candidate.state_evidence:
        state_evidence = _evidence_safe(candidate.state_evidence)
        manifest["state_evidence"] = state_evidence
        restart_stage = state_evidence.get("restart_stage") if isinstance(state_evidence, Mapping) else None
        if restart_stage:
            manifest["restart_stage"] = restart_stage
        if state_evidence.get("durable_shud_output_reused") is True:
            manifest["durable_shud_output_reused"] = True
            manifest["native_shud_resubmitted"] = False
    forcing_metadata = resource_profile.get("forcing_station_metadata")
    if isinstance(forcing_metadata, Mapping):
        manifest["forcing_station_metadata"] = dict(forcing_metadata)
    slurm_env = resource_profile.get("slurm_env")
    if isinstance(slurm_env, Mapping):
        manifest["slurm_env"] = {str(key): str(value) for key, value in slurm_env.items()}
    return manifest


def _candidate_execution_attempted(outcome: Mapping[str, Any] | None, submitted: bool) -> bool:
    if submitted and outcome is None:
        return True
    if not outcome:
        return False
    return any(
        outcome.get(field) not in (None, "")
        for field in ("slurm_job_id", "exit_code", "log_uri", "accounting", "task_id", "original_task_id")
    )


def _pipeline_result_slurm_submit_called(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "slurm_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("slurm_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("slurm_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_pipeline_status_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_status_write",
        absent_field="pipeline_status_writes_proven_absent",
    )


def _pipeline_result_pipeline_event_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_event_write",
        absent_field="pipeline_event_writes_proven_absent",
    )


def _pipeline_result_pipeline_write_value(
    result: PipelineResult,
    *,
    write_field: str,
    absent_field: str,
) -> bool | str:
    outcome_values = [
        _candidate_pipeline_write_value(outcome, write_field, fallback=None)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    ]
    if any(value is True for value in outcome_values):
        return True
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in outcome_values):
        return UNKNOWN_AFTER_ATTEMPT
    if _pipeline_result_has_pipeline_job_evidence(result):
        return True
    if outcome_values and all(value is False for value in outcome_values):
        return False
    if _pipeline_result_write_absence_proven(result, absent_field):
        return False
    return UNKNOWN_AFTER_ATTEMPT


def _pipeline_result_has_pipeline_job_evidence(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "pipeline_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("pipeline_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("pipeline_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_write_absence_proven(result: PipelineResult, absent_field: str) -> bool:
    outcomes = [
        outcome
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    ]
    if outcomes and all(outcome.get(absent_field) is True for outcome in outcomes):
        return True
    return False


def _candidate_slurm_submit_called(outcome: Mapping[str, Any] | None, fallback: bool) -> bool:
    if outcome and _nonempty_evidence_value(outcome.get("slurm_job_id")):
        return True
    return fallback


def _candidate_pipeline_write_value(
    outcome: Mapping[str, Any] | None,
    write_field: str,
    *,
    fallback: bool | str | None,
) -> bool | str | None:
    if outcome:
        value = outcome.get(write_field)
        if value == UNKNOWN_AFTER_ATTEMPT:
            return UNKNOWN_AFTER_ATTEMPT
        coerced = _nested_bool(outcome, write_field)
        if coerced is True:
            return True
        absent_field = f"{write_field}s_proven_absent"
        if outcome.get(absent_field) is True:
            return False
    return fallback


def _execution_mutation_value(*values: bool | str | None) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def _nonempty_evidence_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _candidate_identity_evidence(candidate: SchedulerCandidate, *, output_uri: str | None = None) -> dict[str, Any]:
    contract_identity = _candidate_production_identity(candidate)
    evidence = {
        "production_identity_contract": production_identity_contract_evidence(contract_identity),
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "source": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": contract_identity["canonical_product_id"],
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": contract_identity["hydro_run_id"],
        "published_manifest_id": contract_identity["published_manifest_id"],
        "model_package_uri": _redact_secret_manifest_for_evidence(candidate.model_package_uri, "model_package_uri"),
        "model_package_manifest_uri": _redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_key": _candidate_output_key(candidate),
    }
    if contract_identity.get("pipeline_job_id") not in (None, ""):
        evidence["pipeline_job_id"] = contract_identity["pipeline_job_id"]
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        evidence["output_uri"] = _redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    if candidate.state_evidence:
        evidence["state_evidence"] = _evidence_safe(candidate.state_evidence)
    return evidence


def _candidate_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    config: ProductionSchedulerConfig | None = None,
) -> dict[str, Any]:
    if config is not None and config.slurm_execution_enabled:
        preflight = _slurm_preflight(config)
        return _candidate_slurm_preflight_blocked_evidence(candidate, preflight)
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "unsupported_without_safe_preflight",
        "error_code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
        "error_message": (
            "Default non-dry-run production scheduling is blocked until the Slurm/database preflight "
            "from issue #194 is available or a deterministic orchestrator_factory is injected."
        ),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
                "state": "blocked",
                "quality_flag": "preflight_required",
                "residual_risk": "No scheduler mutation was attempted.",
            }
        ],
    }


def _candidate_slurm_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = list(preflight.get("blockers") or [])
    primary = blockers[0] if blockers else {
        "code": "SLURM_PREFLIGHT_BLOCKED",
        "message": "Slurm preflight blocked submission.",
    }
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "slurm_preflight": redact_payload(preflight),
        "error_code": str(primary.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
        "error_message": str(primary.get("message") or "Slurm preflight blocked submission."),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": str(blocker.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
                "field": blocker.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": blocker.get("message"),
            }
            for blocker in blockers
        ],
    }


def _candidate_evidence_write_blocked_evidence(
    candidate: SchedulerCandidate,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before production mutation.",
    }
    reason = reservation.get("reason")
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "evidence_preflight",
        "evidence_pre_execution": _evidence_safe(dict(reservation)),
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before production mutation.",
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [blocker],
    }


def _cancel_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    reason = reservation.get("reason")
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before Slurm cancellation mutation.",
    }
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    source_id = str(candidate.get("source_id") or "")
    cycle_time_text = str(candidate.get("cycle_time_utc") or "")
    item: dict[str, Any] = {
        "source_id": source_id,
        "cycle_time_utc": cycle_time_text,
        "status": "preflight_blocked",
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before Slurm cancellation mutation.",
        "replacement_submitted": False,
        "mutation_occurred": False,
        "cancel_attempted": False,
        "evidence_pre_execution": _evidence_safe(dict(reservation)),
        "active_slurm_jobs": _evidence_safe(candidate.get("active_slurm_jobs", [])),
        "residual_blockers": [blocker],
    }
    if source_id and cycle_time_text:
        cycle_time = _ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
        item["cycle_id"] = cycle_id_for(source_id, cycle_time)
    return item


def _sync_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    reason = reservation.get("reason")
    blocker = {
        "code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "state": "blocked",
        "quality_flag": "evidence_preflight_blocked",
        "residual_risk": "Scheduler evidence write proof failed before Slurm status sync mutation.",
    }
    if reason not in (None, ""):
        blocker["reason"] = str(reason)
    item = {
        **dict(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "evidence_preflight",
        "evidence_pre_execution": _evidence_safe(dict(reservation)),
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "error_message": "Scheduler evidence write proof failed before Slurm status sync mutation.",
        "sync_attempted": False,
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [blocker],
    }
    return _evidence_safe(item)


def _candidate_secret_manifest_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    findings: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "error_code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
        "error_message": "Slurm submission manifests reject secret-bearing fields and URL values.",
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
                "field": finding.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": "Secret-bearing manifest field or URL value was rejected before submission.",
            }
            for finding in findings
        ],
    }


def _slurm_resource_profile_blockers(resource_profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    collision_fields = sorted(SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS.intersection(resource_profile))
    if collision_fields:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": f"resource_profile.{field}",
                "message": "Slurm resource profile cannot override manifest or template identity fields.",
                "reason": "manifest_identity_collision",
            }
            for field in collision_fields
        ]
    directive_fields = {
        key: resource_profile[key]
        for key in SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS
        if key in resource_profile
    }
    if not directive_fields:
        return []
    try:
        validate_resource_profile(directive_fields, require_required=False)
    except ResourceProfileValidationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": exc.details.get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
                "reason": exc.details.get("reason") or exc.details.get("type"),
            }
        ]
    except ConfigurationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": (exc.details or {}).get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
            }
        ]
    return []


def _redact_secret_manifest_for_evidence(value: Any, path: str = "manifest") -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            field_path = f"{path}.{key_text}"
            if secret_manifest_key_reason(key_text) is not None:
                continue
            redacted[key_text] = _redact_secret_manifest_for_evidence(nested, field_path)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_secret_manifest_for_evidence(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, str) and secret_manifest_value_reason(value) is not None:
        return "[redacted]"
    return value


def _resource_profile_evidence(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    redacted = _redact_secret_manifest_for_evidence(dict(resource_profile), "resource_profile")
    if not isinstance(redacted, Mapping):
        return {}
    evidence = dict(redacted)
    invalid_fields = {
        str(blocker.get("field", "")).removeprefix("resource_profile.")
        for blocker in _slurm_resource_profile_blockers(resource_profile)
        if blocker.get("field")
    }
    for field_name in invalid_fields:
        if field_name in evidence:
            evidence[field_name] = "[unsafe]"
    return evidence


def _candidate_execution_evidence(
    result: PipelineResult,
    candidates: Sequence[SchedulerCandidate],
    *,
    output_uris: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    stage_names = [stage.stage for stage in result.stages]
    stage_statuses = [
        _stage_run_evidence(stage)
        for stage in result.stages
    ]
    slurm_submit_called = _pipeline_result_slurm_submit_called(result)
    pipeline_status_write = _pipeline_result_pipeline_status_write(result)
    pipeline_event_write = _pipeline_result_pipeline_event_write(result)
    outcomes_by_candidate = {
        str(outcome.get("candidate_id")): dict(outcome)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if outcome.get("candidate_id")
    }
    return [
        _candidate_execution_evidence_item(
            result,
            candidate,
            output_uri=(output_uris or {}).get(candidate.candidate_id),
            outcome=outcomes_by_candidate.get(candidate.candidate_id),
            slurm_submit_called=slurm_submit_called,
            pipeline_status_write=pipeline_status_write,
            pipeline_event_write=pipeline_event_write,
            stage_names=stage_names,
            stage_statuses=stage_statuses,
        )
        for candidate in candidates
    ]


def _candidate_execution_evidence_item(
    result: PipelineResult,
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    slurm_submit_called: bool,
    pipeline_status_write: bool | str,
    pipeline_event_write: bool | str,
    stage_names: Sequence[str],
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if outcome is None:
        status = result.status
        candidate_submitted = slurm_submit_called
        candidate_outcome: dict[str, Any] | None = None
        execution_attempted = True
    else:
        outcome_status = str(outcome.get("status") or "")
        status = _candidate_status_from_outcome(result.status, outcome_status)
        execution_attempted = True
        candidate_slurm_submit_called = _candidate_slurm_submit_called(outcome, slurm_submit_called)
        candidate_submitted = candidate_slurm_submit_called and (outcome_status == "active" or execution_attempted)
        candidate_outcome = dict(outcome)
    candidate_pipeline_status_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_status_write",
        fallback=pipeline_status_write,
    )
    candidate_pipeline_event_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_event_write",
        fallback=pipeline_event_write,
    )
    mutation_occurred = _execution_mutation_value(
        candidate_submitted,
        candidate_pipeline_status_write,
        candidate_pipeline_event_write,
    )
    review_evidence = _candidate_model_run_review_evidence(
        candidate,
        output_uri=output_uri,
        outcome=outcome,
        status=status,
        stage_statuses=stage_statuses,
    )
    item = {
        **review_evidence,
        "status": status,
        "submitted": candidate_submitted,
        "slurm_submit_called": candidate_submitted,
        "execution_attempted": execution_attempted,
        "final_candidate_success": (
            status == result.status and not _is_non_submitted_terminal_or_unavailable_status(status)
        ),
        "mutation_occurred": mutation_occurred,
        "pipeline_run_id": result.run_id,
        "standard_chain_shape": stage_names,
        "qhh_script_invoked": False,
    }
    if candidate_pipeline_status_write is True:
        item["pipeline_status_write"] = True
        item["pipeline_status_writes_proven_absent"] = False
    elif candidate_pipeline_status_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_status_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_status_writes_proven_absent"] = False
    else:
        item["pipeline_status_writes_proven_absent"] = True
    if candidate_pipeline_event_write is True:
        item["pipeline_event_write"] = True
        item["pipeline_event_writes_proven_absent"] = False
    elif candidate_pipeline_event_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_event_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_event_writes_proven_absent"] = False
    else:
        item["pipeline_event_writes_proven_absent"] = True
    if mutation_occurred == UNKNOWN_AFTER_ATTEMPT:
        item["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
    if candidate_outcome is not None:
        candidate_outcome = _evidence_safe(candidate_outcome)
        item["candidate_outcome"] = candidate_outcome
        if _is_partial_candidate_evidence(item):
            item["error_code"] = str(candidate_outcome.get("reason") or f"CANDIDATE_{status}").upper()
            item["error_message"] = (
                f"Candidate {candidate.candidate_id} was {status} in the partial multi-basin cycle."
            )
            if not any(blocker.get("code") == item["error_code"] for blocker in item["residual_blockers"]):
                item["residual_blockers"].append(
                    {
                        "code": item["error_code"],
                        "stage": candidate_outcome.get("stage") or candidate_outcome.get("failed_stage"),
                        "state": "blocked",
                        "quality_flag": "partial_candidate",
                        "residual_risk": item["error_message"],
                    }
                )
    return item


def _candidate_status_from_outcome(result_status: str, outcome_status: str) -> str:
    if outcome_status == "active":
        return result_status
    if _is_non_submitted_terminal_or_unavailable_status(outcome_status):
        return outcome_status
    return "unavailable"


def _candidate_model_run_review_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    status: str,
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stage_status_payload = _candidate_stage_evidence(candidate, stage_statuses, outcome=outcome)
    quality_states = _candidate_quality_states(candidate, outcome=outcome, status=status)
    artifact_refs = _candidate_artifact_refs(candidate, output_uri=output_uri)
    return {
        "schema_version": MODEL_RUN_EVIDENCE_SCHEMA_VERSION,
        "review_contract": {
            "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
            "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
            "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
            "scope": "model_run_evidence",
        },
        **_candidate_identity_evidence(candidate, output_uri=output_uri),
        "stage_statuses": stage_status_payload,
        "stage_evidence": stage_status_payload,
        "artifact_refs": artifact_refs,
        "artifact_locations": dict(artifact_refs),
        "resource_profile": _resource_profile_evidence(candidate.resource_profile),
        "resource_summary": _candidate_resource_summary(
            candidate,
            stage_statuses=stage_status_payload,
            outcome=outcome,
        ),
        "forcing": _candidate_forcing_evidence(candidate),
        "outputs": _candidate_output_evidence(candidate, output_uri=output_uri, outcome=outcome),
        "display": _candidate_display_evidence(candidate),
        "quality_states": quality_states,
        "residual_blockers": _candidate_residual_blockers(
            candidate,
            outcome=outcome,
            status=status,
            quality_states=quality_states,
        ),
    }


def _candidate_stage_evidence(
    candidate: SchedulerCandidate,
    stage_statuses: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        _candidate_stage_evidence_item(candidate, _evidence_safe(dict(stage)), outcome=outcome)
        for stage in stage_statuses
    ]


def _candidate_stage_evidence_item(
    candidate: SchedulerCandidate,
    stage: Mapping[str, Any],
    *,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stage_payload = dict(stage)
    stage_payload["production_stage"] = production_stage_for(
        stage_payload.get("stage") or stage_payload.get("job_type")
    )
    stage_payload["production_status"] = production_status_for(stage_payload.get("status"))
    task_results = _stage_task_results(stage_payload)
    total_count = len(task_results)
    status_counts = Counter(str(task.get("status") or task.get("state") or "unknown") for task in task_results)
    matched_tasks = [
        task
        for task in task_results
        if _task_result_matches_candidate(task, candidate, outcome=outcome)
    ]
    exact_match_available = _task_candidate_matching_available(task_results, outcome=outcome)
    if exact_match_available:
        selected_tasks = matched_tasks[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    else:
        selected_tasks = task_results[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    selected_count = len(selected_tasks)
    stage_payload["task_results"] = [_evidence_safe(dict(task)) for task in selected_tasks]
    stage_payload["task_results_summary"] = _evidence_safe(
        {
            "total_count": total_count,
            "included_count": selected_count,
            "omitted_count": max(total_count - selected_count, 0),
            "matched_count": len(matched_tasks),
            "matching": "candidate_identity" if exact_match_available else "bounded_sample",
            "limit": MAX_MODEL_RUN_STAGE_TASK_ROWS,
            "status_counts": dict(sorted(status_counts.items())),
        }
    )
    return _evidence_safe(stage_payload)


def _stage_run_evidence(stage: Any) -> dict[str, Any]:
    task_results = [
        _task_result_evidence(task)
        for task in tuple(getattr(stage, "task_results", ()) or ())
        if isinstance(task, Mapping)
    ]
    payload = {
        "stage": getattr(stage, "stage", None),
        "production_stage": production_stage_for(getattr(stage, "stage", None) or getattr(stage, "job_type", None)),
        "job_type": getattr(stage, "job_type", None),
        "pipeline_job_id": getattr(stage, "pipeline_job_id", None),
        "slurm_job_id": getattr(stage, "slurm_job_id", None),
        "status": getattr(stage, "status", None),
        "production_status": production_status_for(getattr(stage, "status", None)),
        "exit_code": getattr(stage, "exit_code", None),
        "error_code": getattr(stage, "error_code", None),
        "error_message": getattr(stage, "error_message", None),
        "log_uri": getattr(stage, "log_uri", None),
        "accounting": getattr(stage, "accounting", {}) or {},
        "resource_metrics": _resource_metrics_from_mapping(getattr(stage, "accounting", {}) or {}),
        "task_results": task_results,
    }
    if not payload["accounting"]:
        payload["accounting_gap"] = {
            "available": False,
            "reason": "accounting_unavailable",
            "fabricated_metrics": False,
        }
    return _evidence_safe(payload)


def _stage_task_results(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_results = stage.get("task_results") or []
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return []
    return [task for task in task_results if isinstance(task, Mapping)]


def _task_result_matches_candidate(
    task: Mapping[str, Any],
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    identity_fields = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "model_id": candidate.model_id,
    }
    for field_name, expected in identity_fields.items():
        if _normalized_identity(task.get(field_name)) == _normalized_identity(expected):
            return True
    identity = task.get("identity")
    if isinstance(identity, Mapping):
        for field_name, expected in identity_fields.items():
            if _normalized_identity(identity.get(field_name)) == _normalized_identity(expected):
                return True
    if outcome is None:
        return False
    for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS:
        task_value = _normalized_identity(task.get(field_name))
        outcome_value = _normalized_identity(outcome.get(field_name))
        if task_value is not None and task_value == outcome_value:
            return True
    outcome_task_ids = {
        _normalized_identity(outcome.get(field_name))
        for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    }
    outcome_task_ids.discard(None)
    task_ids = {
        _normalized_identity(task.get(field_name))
        for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    }
    task_ids.discard(None)
    return bool(task_ids.intersection(outcome_task_ids))


def _task_candidate_matching_available(
    tasks: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    for task in tasks:
        if any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS):
            return True
        identity = task.get("identity")
        if isinstance(identity, Mapping) and any(
            identity.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS
        ):
            return True
    if outcome is None:
        return False
    outcome_has_task_identity = any(
        outcome.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    )
    if not outcome_has_task_identity:
        return False
    return any(
        any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS)
        for task in tasks
    )


def _normalized_identity(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _task_result_evidence(task: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["accounting"] = dict(_mapping_value(payload.get("accounting")))
    metrics = _resource_metrics_from_mapping(payload.get("resource_metrics") or payload["accounting"])
    if metrics:
        payload["resource_metrics"] = metrics
    elif "resource_metrics" not in payload:
        payload["resource_metrics"] = {}
    return _evidence_safe(payload)


def _resource_metrics_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "elapsed": ("elapsed", "elapsed_time"),
        "max_rss": ("max_rss", "MaxRSS", "maxrss"),
        "ave_rss": ("ave_rss", "AveRSS", "averss"),
        "alloc_tres": ("alloc_tres", "AllocTRES", "tres"),
        "max_disk_read": ("max_disk_read", "MaxDiskRead"),
        "max_disk_write": ("max_disk_write", "MaxDiskWrite"),
    }
    metrics: dict[str, Any] = {}
    for normalized, keys in aliases.items():
        for key in keys:
            if key in value and value[key] not in (None, ""):
                metrics[normalized] = value[key]
                break
    return _evidence_safe(metrics)


def _candidate_artifact_refs(candidate: SchedulerCandidate, *, output_uri: str | None) -> dict[str, Any]:
    refs = {
        "model_package_uri": _redact_secret_manifest_for_evidence(candidate.model_package_uri, "model_package_uri"),
        "model_package_manifest_uri": _redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "output_key": _candidate_output_key(candidate),
    }
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        refs["output_uri"] = _redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    manifest_uri = candidate.resource_profile.get("manifest_uri")
    if manifest_uri not in (None, ""):
        refs["resource_manifest_uri"] = _redact_secret_manifest_for_evidence(
            str(manifest_uri),
            "resource_manifest_uri",
        )
    return _evidence_safe(refs)


def _candidate_resource_summary(
    candidate: SchedulerCandidate,
    *,
    stage_statuses: Sequence[Mapping[str, Any]],
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resource_profile = _resource_profile_evidence(candidate.resource_profile)
    stage_accounting = [
        {
            "stage": stage.get("stage"),
            "slurm_job_id": stage.get("slurm_job_id"),
            "accounting": stage.get("accounting") or {},
            "resource_metrics": stage.get("resource_metrics") or {},
            "accounting_gap": stage.get("accounting_gap"),
        }
        for stage in stage_statuses
    ]
    task_accounting: list[dict[str, Any]] = []
    for stage in stage_statuses:
        for task in stage.get("task_results") or []:
            if not isinstance(task, Mapping):
                continue
            task_accounting.append(
                {
                    "stage": stage.get("stage"),
                    "task_id": task.get("task_id"),
                    "array_task_id": task.get("array_task_id"),
                    "slurm_job_id": task.get("slurm_job_id"),
                    "status": task.get("status"),
                    "accounting": task.get("accounting") or {},
                    "resource_metrics": task.get("resource_metrics") or {},
                }
            )
    payload = {
        "resource_profile": resource_profile,
        "requested": {
            "memory_gb": resource_profile.get("memory_gb"),
            "cpu": resource_profile.get("cpu"),
            "cpus_per_task": resource_profile.get("cpus_per_task"),
            "walltime": resource_profile.get("walltime"),
            "max_concurrent": resource_profile.get("max_concurrent"),
            "shud_threads": resource_profile.get("shud_threads"),
        },
        "stage_accounting": stage_accounting,
        "task_accounting": task_accounting,
        "candidate_accounting": dict(_mapping_value(outcome.get("accounting") if outcome is not None else None)),
        "candidate_resource_metrics": _resource_metrics_from_mapping(
            (outcome.get("resource_metrics") or outcome.get("accounting")) if outcome is not None else {}
        ),
    }
    return _evidence_safe(payload)


def _candidate_forcing_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    metadata = candidate.resource_profile.get("forcing_station_metadata")
    station_count = _candidate_station_count(candidate)
    station_ids = _candidate_station_ids(candidate)
    payload = {
        "station_count": station_count,
        "station_ids": station_ids,
        "state": "ready" if station_count and station_count > 0 else "unavailable",
        "quality_flag": "ok" if station_count and station_count > 0 else "station_forcing_unavailable",
    }
    if isinstance(metadata, Mapping):
        payload["station_metadata"] = dict(metadata)
        if metadata.get("quality_flag") not in (None, ""):
            payload["quality_flag"] = metadata.get("quality_flag")
    return _evidence_safe(payload)


def _candidate_output_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    parsed_row_count = _first_present_int(
        outcome,
        candidate.resource_profile,
        "parsed_row_count",
        "canonical_product_count",
        "output_row_count",
    )
    segment_count = _first_present_int(outcome, candidate.resource_profile, "segment_count")
    if segment_count is None:
        segment_count = candidate.segment_count
    payload = {
        "output_uri": _redact_secret_manifest_for_evidence(
            resolved_output_uri,
            "output_uri",
        )
        if resolved_output_uri
        else None,
        "output_key": _candidate_output_key(candidate),
        "shud_output_uri": _redact_secret_manifest_for_evidence(
            _first_present_value(outcome, candidate.resource_profile, "shud_output_uri", "output_uri"),
            "shud_output_uri",
        ),
        "parsed_row_count": parsed_row_count,
        "segment_count": segment_count,
        "canonical_product_counts": _candidate_product_counts(candidate, outcome=outcome),
    }
    return _evidence_safe(payload)


def _candidate_display_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    tiles = _nested_bool(candidate.display_capabilities, "tiles", fallback=False)
    optional_weather_available = _nested_bool(candidate.display_capabilities, "optional_weather_available")
    unavailable_products: list[str] = []
    if tiles is False:
        unavailable_products.append("tiles")
    if optional_weather_available is False:
        unavailable_products.append("optional_weather_products")
    payload = {
        "state": "ready" if tiles else "unavailable",
        "tiles": tiles,
        "optional_weather_available": optional_weather_available,
        "unavailable_products": unavailable_products,
        "quality_flag": "ok" if not unavailable_products else "display_inputs_unavailable",
    }
    return _evidence_safe(payload)


def _candidate_quality_states(
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    forcing = _candidate_forcing_evidence(candidate)
    display = _candidate_display_evidence(candidate)
    frequency = _candidate_frequency_evidence(candidate)
    output = _candidate_output_evidence(candidate, output_uri=None, outcome=outcome)
    payload = {
        "candidate": {
            "state": status,
            "quality_flag": "ok" if not _is_non_submitted_terminal_or_unavailable_status(status) else "blocked",
        },
        "station_forcing": {
            "state": forcing.get("state"),
            "quality_flag": forcing.get("quality_flag"),
            "station_count": forcing.get("station_count"),
        },
        "output_river": {
            "state": "ready" if (output.get("segment_count") or 0) > 0 else "unavailable",
            "quality_flag": "ok" if (output.get("segment_count") or 0) > 0 else "output_river_unavailable",
            "segment_count": output.get("segment_count"),
        },
        "frequency": frequency,
        "display": display,
    }
    return _evidence_safe(payload)


def _candidate_frequency_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    return_periods = _nested_bool(candidate.frequency_capabilities, "return_periods", fallback=False)
    curves_available = _nested_bool(candidate.frequency_capabilities, "curves_available", fallback=return_periods)
    warning_thresholds_available = _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available")
    unavailable_products: list[str] = []
    if curves_available is False:
        unavailable_products.append("return_period_curves")
    if warning_thresholds_available is False:
        unavailable_products.append("warning_thresholds")
    return _evidence_safe(
        {
            "state": "ready" if not unavailable_products and return_periods else "unavailable",
            "return_periods": return_periods,
            "curves_available": curves_available,
            "warning_thresholds_available": warning_thresholds_available,
            "unavailable_products": unavailable_products,
            "quality_flag": "ok" if not unavailable_products else "frequency_inputs_unavailable",
        }
    )


def _candidate_residual_blockers(
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
    status: str,
    quality_states: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for key, state in quality_states.items():
        if not isinstance(state, Mapping):
            continue
        state_value = str(state.get("state") or "")
        if state_value not in {"blocked", "failed", "unavailable"}:
            continue
        blockers.append(
            {
                "code": str(state.get("quality_flag") or f"{key}_unavailable").upper(),
                "field": key,
                "state": state_value,
                "quality_flag": state.get("quality_flag"),
                "residual_risk": f"{key} is {state_value}; downstream readiness must keep this non-final.",
            }
        )
    if _is_non_submitted_terminal_or_unavailable_status(status):
        code = (
            str(outcome.get("reason") or outcome.get("error_code") or f"CANDIDATE_{status}").upper()
            if outcome is not None
            else f"CANDIDATE_{status}".upper()
        )
        blockers.append(
            {
                "code": code,
                "stage": (outcome.get("stage") or outcome.get("failed_stage")) if outcome is not None else None,
                "state": "blocked",
                "quality_flag": "candidate_not_successful",
                "residual_risk": f"Candidate {candidate.candidate_id} ended with status {status}.",
            }
        )
    return _evidence_safe(blockers)


def _candidate_product_counts(candidate: SchedulerCandidate, *, outcome: Mapping[str, Any] | None) -> dict[str, Any]:
    explicit = _first_present_value(outcome, candidate.resource_profile, "canonical_product_counts", "product_counts")
    if isinstance(explicit, Mapping):
        return _evidence_safe(dict(explicit))
    parsed = _first_present_int(outcome, candidate.resource_profile, "parsed_row_count", "output_row_count")
    counts: dict[str, Any] = {}
    if parsed is not None:
        counts["parsed_rows"] = parsed
    if candidate.segment_count is not None:
        counts["river_segments"] = candidate.segment_count
    station_count = _candidate_station_count(candidate)
    if station_count is not None:
        counts["forcing_stations"] = station_count
    return counts


def _candidate_production_identity(candidate: SchedulerCandidate) -> dict[str, Any]:
    identity = {
        "run_id": candidate.run_id,
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "source": candidate.source_id,
        "source_id": candidate.source_id,
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
    }
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        identity["pipeline_job_id"] = pipeline_job_id
    return identity


def _candidate_canonical_product_id(candidate: SchedulerCandidate) -> str:
    explicit = candidate.resource_profile.get("canonical_product_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"canon_{candidate.source_id.lower()}_{format_cycle_time(candidate.cycle_time_utc)}"


def _candidate_published_manifest_id(candidate: SchedulerCandidate) -> str:
    explicit = candidate.resource_profile.get("published_manifest_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"manifest_{candidate.run_id}"


def _candidate_contract_pipeline_job_id(candidate: SchedulerCandidate) -> str | None:
    explicit = candidate.resource_profile.get("pipeline_job_id")
    if explicit not in (None, ""):
        return str(explicit)
    return None


def _first_present_value(
    outcome: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
    *keys: str,
) -> Any:
    for source in (outcome, profile):
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_present_int(
    outcome: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
    *keys: str,
) -> int | None:
    value = _first_present_value(outcome, profile, *keys)
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _model_package_manifest_uri(candidate: SchedulerCandidate) -> str:
    resource_profile = dict(candidate.resource_profile)
    explicit = resource_profile.get("manifest_uri")
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = candidate.model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _candidate_output_key(candidate: SchedulerCandidate) -> str:
    return f"runs/{candidate.run_id}/output/"


def _candidate_output_uri(candidate: SchedulerCandidate, object_store: Any | None = None) -> str | None:
    explicit = candidate.resource_profile.get("output_uri")
    if explicit not in (None, "") and _has_uri_scheme(str(explicit)):
        return str(explicit).rstrip("/") + "/"
    if object_store is not None:
        uri_for_key = getattr(object_store, "uri_for_key", None)
        if callable(uri_for_key):
            return str(uri_for_key(_candidate_output_key(candidate))).rstrip("/") + "/"
    return None


def _has_uri_scheme(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value.strip()) is not None


def _candidate_station_count(candidate: SchedulerCandidate) -> int | None:
    value = candidate.resource_profile.get("station_count")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_count")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_station_ids(candidate: SchedulerCandidate) -> list[str]:
    value = candidate.resource_profile.get("station_ids")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_ids")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(item) for item in value]
    return []


def _slurm_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.slurm_execution_enabled:
        return {
            "status": "not_required",
            "enabled": False,
            "blockers": [],
            "checks": {},
        }

    blockers: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    database_url = config.database_url
    db_blocker = _database_url_blocker(database_url)
    checks["database"] = {
        "configured": bool(database_url),
        "host": _database_host(database_url),
        "compute_node_reachable": db_blocker is None,
    }
    if db_blocker is not None:
        blockers.append(db_blocker)

    roots = {
        "workspace_root": config.workspace_root,
        "object_store_root": config.object_store_root,
        "log_root": config.log_root,
        "runtime_root": config.runtime_root,
    }
    allowed_roots = _preflight_allowed_roots(config)
    root_checks: dict[str, Any] = {}
    for field_name, value in roots.items():
        root_check, blocker = _storage_root_check(field_name, value, allowed_roots)
        root_checks[field_name] = root_check
        if blocker is not None:
            blockers.append(blocker)
    checks["storage_roots"] = root_checks
    checks["allowed_roots"] = [str(root) for root in allowed_roots]

    template_check, template_blockers = _slurm_template_allowlist_check(config)
    checks["templates"] = template_check
    blockers.extend(template_blockers)

    env_check, env_blockers = _slurm_env_check(config.slurm_env)
    checks["environment"] = env_check
    blockers.extend(env_blockers)

    return {
        "status": "blocked" if blockers else "ready",
        "enabled": True,
        "blockers": blockers,
        "checks": checks,
    }


def _database_url_blocker(database_url: str | None) -> dict[str, Any] | None:
    if not database_url:
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_MISSING",
            "field": "DATABASE_URL",
            "message": "Slurm execution requires a compute-node reachable DATABASE_URL before submission.",
        }
    host = _database_host(database_url)
    if _database_host_is_unsafe(host):
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST",
            "field": "DATABASE_URL",
            "message": "Slurm execution rejects malformed or unsafe DATABASE_URL hosts.",
            "host": host,
        }
    if _database_host_is_local(host):
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST",
            "field": "DATABASE_URL",
            "message": "Slurm execution rejects localhost-only DATABASE_URL values.",
            "host": host,
        }
    return None


def _database_host(database_url: str | None) -> str | None:
    if not database_url:
        return None
    try:
        parsed = urlparse(database_url)
    except ValueError:
        return None
    if parsed.scheme == "sqlite":
        return "localhost"
    try:
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    return host


def _database_host_is_local(host: str | None) -> bool:
    if host is None:
        return True
    normalized = _normalize_database_host(host)
    if normalized in LOCALHOST_NAMES:
        return True
    if normalized.endswith(".localhost"):
        return True
    address = _database_host_ip_address(normalized)
    if address is None:
        return False
    return address.is_loopback or address.is_unspecified


def _database_host_is_unsafe(host: str | None) -> bool:
    if host is None:
        return True
    normalized = _normalize_database_host(host)
    if not normalized:
        return True
    if DATABASE_HOST_ALLOWED_RE.fullmatch(normalized) is None:
        return True
    address = _database_host_ip_address(normalized)
    if address is not None and address.is_link_local:
        return True
    if ":" in normalized:
        if address is None:
            return True
    return _is_unsafe_numeric_ipv4_like_host(normalized)


def _normalize_database_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized.rstrip(".")


def _database_host_ip_address(host: str) -> Any | None:
    try:
        return ip_address(host)
    except ValueError:
        return _parse_noncanonical_ipv4_address(host)


def _parse_noncanonical_ipv4_address(host: str) -> IPv4Address | None:
    if not _is_noncanonical_numeric_ipv4_host(host):
        return None
    parts = host.split(".")
    values: list[int] = []
    for part in parts:
        if part == "":
            return None
        try:
            values.append(int(part, 0))
        except ValueError:
            return None
    if len(values) == 1:
        value = values[0]
    elif len(values) == 2:
        value = (values[0] << 24) | values[1]
    elif len(values) == 3:
        value = (values[0] << 24) | (values[1] << 16) | values[2]
    elif len(values) == 4:
        value = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    else:
        return None
    if value < 0 or value > 0xFFFFFFFF:
        return None
    return IPv4Address(value)


def _is_noncanonical_numeric_ipv4_host(host: str) -> bool:
    if not host:
        return False
    if not _is_numeric_ipv4_like_host(host):
        return False
    parts = host.split(".")
    return len(parts) != 4 or any(_is_noncanonical_ipv4_part(part) for part in parts)


def _is_numeric_ipv4_like_host(host: str) -> bool:
    parts = host.split(".")
    return all(_is_ipv4_number_part(part) for part in parts)


def _is_ipv4_number_part(part: str) -> bool:
    if part == "":
        return False
    if part.lower().startswith("0x"):
        return len(part) > 2 and all(character in "0123456789abcdefABCDEF" for character in part[2:])
    return part.isdigit()


def _is_noncanonical_ipv4_part(part: str) -> bool:
    if part == "":
        return True
    if part.lower().startswith("0x"):
        return True
    return len(part) > 1 and part.startswith("0")


def _is_unsafe_numeric_ipv4_like_host(host: str) -> bool:
    if not _is_numeric_ipv4_like_host(host):
        return False
    return _database_host_ip_address(host) is None


def _preflight_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots = list(config.allowed_storage_roots) or [Path(config.workspace_root)]
    resolved: list[Path] = []
    for root in roots:
        candidate = root.expanduser().resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _storage_root_check(
    field_name: str,
    value: Path | str | None,
    allowed_roots: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value in (None, ""):
        return (
            {
                "configured": False,
                "path": None,
                "contained": False,
                "compute_node_visible": False,
            },
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_MISSING",
                "field": field_name,
                "message": f"Slurm execution requires configured {field_name}.",
            },
        )
    path = Path(value).expanduser()
    resolved = path.resolve()
    visible = path.exists() and path.is_dir()
    contained = _path_is_under_any(resolved, allowed_roots)
    check = {
        "configured": True,
        "path": str(resolved),
        "contained": contained,
        "compute_node_visible": visible,
    }
    if not contained:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_OUT_OF_ROOT",
                "field": field_name,
                "path": str(resolved),
                "message": f"Slurm {field_name} must stay under configured project or production roots.",
            },
        )
    if not visible:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_NOT_VISIBLE",
                "field": field_name,
                "path": str(resolved),
                "message": f"Slurm {field_name} must exist as a compute-node visible directory.",
            },
        )
    return check, None


def _path_is_under_any(path: Path, allowed_roots: Sequence[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _slurm_template_allowlist_check(config: ProductionSchedulerConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    templates = dict(config.slurm_job_type_templates or {})
    blockers: list[dict[str, Any]] = []
    expected_by_stage = {stage.stage: stage.job_type for stage in ForecastOrchestrator.stages}
    allowed_names = set(DEFAULT_JOB_TYPE_TEMPLATES.values())
    checks: dict[str, Any] = {}
    for stage_name, job_type in expected_by_stage.items():
        template_name = templates.get(job_type)
        check = {
            "job_type": job_type,
            "template_name": template_name,
            "allowlisted": template_name in allowed_names,
            "array_capable": stage_name in SLURM_ARRAY_STAGE_NAMES,
        }
        checks[stage_name] = check
        if template_name not in allowed_names:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_NOT_ALLOWLISTED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} must use an allowlisted sbatch template.",
                }
            )
        expected_template = DEFAULT_JOB_TYPE_TEMPLATES.get(job_type)
        if template_name in allowed_names and template_name != expected_template:
            check["expected_template_name"] = expected_template
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_MISMATCH",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "expected_template_name": expected_template,
                    "message": f"Slurm stage {stage_name} must use the template assigned to its job type.",
                }
            )
        if stage_name in SLURM_ARRAY_STAGE_NAMES and not str(template_name or "").endswith("_array.sbatch"):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ARRAY_TEMPLATE_REQUIRED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} requires an array-capable template.",
                }
            )
    return {"stage_templates": checks}, blockers


def _slurm_env_check(env: Mapping[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    sanitized: dict[str, str] = {}
    for key, value in env.items():
        key_text = str(key)
        value_text = str(value)
        key_secret_reason = secret_manifest_key_reason(key_text)
        if key_secret_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": "slurm_env.[redacted]",
                    "reason": key_secret_reason,
                    "message": "Slurm scheduler evidence and exports reject secret-shaped environment keys.",
                }
            )
            sanitized["[redacted]"] = "[redacted]"
            continue
        if not SAFE_SLURM_ENV_KEY_RE.fullmatch(key_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_KEY_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment keys must be uppercase shell identifiers.",
                }
            )
            continue
        reserved_reason = reserved_slurm_env_reason(key_text)
        if reserved_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "reason": reserved_reason,
                    "message": "Slurm exported environment cannot override reserved runtime variables.",
                }
            )
            sanitized[key_text] = "[reserved]"
            continue
        if len(value_text) > MAX_SLURM_ENV_VALUE_LENGTH:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_TOO_LONG",
                    "field": f"slurm_env.{key_text}",
                    "max_length": MAX_SLURM_ENV_VALUE_LENGTH,
                    "message": "Slurm exported environment values must be bounded.",
                }
            )
            sanitized[key_text] = value_text[:64] + "...[truncated]"
            continue
        secret_url_reason = secret_bearing_url_reason(value_text)
        if secret_url_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "reason": secret_url_reason,
                    "message": (
                        "Slurm exported environment values must not contain URL credentials "
                        "or secret query parameters."
                    ),
                }
            )
            sanitized[key_text] = "[redacted]"
            continue
        if SHELL_META_RE.search(value_text) or not SAFE_SLURM_ENV_VALUE_RE.fullmatch(value_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment values must be shell-safe.",
                }
            )
            sanitized[key_text] = "[unsafe]"
            continue
        sanitized[key_text] = value_text
    return {"count": len(env), "sanitized": sanitized}, blockers


def _scheduler_cancellation_status(cancelled_jobs: Sequence[Mapping[str, Any]]) -> str:
    if not cancelled_jobs:
        return "blocked"
    cancelled_count = 0
    for job in cancelled_jobs:
        status = str(job.get("status") or "").lower()
        if job.get("error_code") or job.get("cancellation_proven") is False or status != "cancelled":
            continue
        cancelled_count += 1
    if cancelled_count == len(cancelled_jobs):
        return "cancelled"
    if cancelled_count:
        return "partially_cancelled"
    return "blocked"


def _cancelled_job_pipeline_status_write(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return status == "cancelled" and job.get("cancellation_proven") is not False and not job.get("error_code")


def _cancelled_job_pipeline_event_write(job: Mapping[str, Any]) -> bool:
    if _cancelled_job_pipeline_status_write(job):
        return True
    return (
        job.get("cancellation_proven") is False
        and str(job.get("error_code") or "") == "JOB_ALREADY_TERMINAL"
    )


def _scheduler_pass_status_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planned"
    statuses = {str(item.get("status") or "") for item in cancellation_evidence}
    if statuses == {"cancelled"}:
        return "slurm_cancelled"
    if "cancelled" in statuses or "partially_cancelled" in statuses:
        return "slurm_partially_cancelled"
    if statuses == {"preflight_blocked"}:
        return "preflight_blocked"
    return "slurm_cancellation_blocked"


def _slurm_status_sync_failed_evidence(
    candidate: SchedulerCandidate,
    *,
    cycle_id: str,
    active_slurm_jobs: Sequence[Mapping[str, Any]],
    error: Exception,
) -> dict[str, Any]:
    error_code = str(getattr(error, "error_code", "SLURM_STATUS_SYNC_FAILED") or "SLURM_STATUS_SYNC_FAILED")
    return {
        "cycle_id": cycle_id,
        "source_id": candidate.source_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "candidate_id": candidate.candidate_id,
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "status": "failed",
        "sync_required": True,
        "sync_called": True,
        "sync_attempted": True,
        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
        "error_code": error_code,
        "error_message": _evidence_safe(getattr(error, "message", str(error))),
        "active_slurm_jobs": _evidence_safe(active_slurm_jobs),
        "residual_blockers": [
            {
                "code": error_code,
                "state": "blocked",
                "quality_flag": "slurm_status_sync_failed",
                "residual_risk": (
                    "Slurm status sync raised after the downstream sync method was called; "
                    "pipeline status/event mutation outcome is unknown."
                ),
            }
        ],
    }


def _scheduler_execution_boundary_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planning_only"
    if all(str(item.get("status") or "") == "preflight_blocked" for item in cancellation_evidence):
        return "evidence_preflight_blocked"
    return "slurm_cancellation"


def _slurm_status_sync_proof(
    *,
    sync_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "sync_required": sync_required,
        "sync_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif sync_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def _slurm_status_sync_proof_from_candidates(
    slurm_status_sync_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    sync_payloads = list(slurm_status_sync_evidence)
    failed_payloads = [item for item in sync_payloads if str(item.get("status") or "") == "failed"]
    update_count = sum(len(item.get("updates") or []) for item in sync_payloads)
    terminal_update_count = sum(len(item.get("terminal_updates") or []) for item in sync_payloads)
    unknown_after_attempt = any(item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT for item in failed_payloads)
    status = "failed" if failed_payloads else ("synced" if sync_payloads else "not_required")
    proof: dict[str, Any] = {
        "status": status,
        "sync_required": bool(sync_payloads),
        "sync_called": bool(sync_payloads),
        "mutation_occurred": update_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "synced_cycle_count": len({str(item.get("cycle_id") or "") for item in sync_payloads if item.get("cycle_id")}),
        "updated_job_count": update_count,
        "terminal_update_count": terminal_update_count,
    }
    if failed_payloads:
        proof.update(
            {
                "failed_sync_count": len(failed_payloads),
                "error_code": failed_payloads[0].get("error_code"),
                "error_message": failed_payloads[0].get("error_message"),
            }
        )
    if unknown_after_attempt:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    return proof


def _execution_write_proof(
    *,
    reservation: Mapping[str, Any] | None = None,
    execution_required: bool = False,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "execution_required": execution_required,
        "orchestration_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif execution_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def _execution_write_proof_from_evidence(
    execution_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    execution_payloads = list(execution_evidence)
    orchestration_called = any(item.get("execution_attempted") is True for item in execution_payloads)
    submitted_count = sum(1 for item in execution_payloads if item.get("submitted") is True)
    slurm_submit_count = sum(1 for item in execution_payloads if item.get("slurm_submit_called") is True)
    unknown_slurm_submit_count = sum(
        1 for item in execution_payloads if item.get("slurm_submit_called") == UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_status_write") is True
    )
    pipeline_event_write_count = sum(1 for item in execution_payloads if item.get("pipeline_event_write") is True)
    unknown_pipeline_status_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_status_write") == UNKNOWN_AFTER_ATTEMPT
    )
    unknown_pipeline_event_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_event_write") == UNKNOWN_AFTER_ATTEMPT
    )
    unknown_after_attempt_count = sum(
        1 for item in execution_payloads if item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT
    )
    preflight_blocked = bool(execution_payloads) and all(
        str(item.get("status") or "") == "preflight_blocked" for item in execution_payloads
    )
    if unknown_after_attempt_count:
        status = UNKNOWN_AFTER_ATTEMPT
    elif submitted_count:
        status = "submitted"
    elif preflight_blocked:
        status = "preflight_blocked"
    elif execution_payloads:
        status = "completed_no_submit"
    else:
        status = "not_required"
    slurm_submit_value: bool | str
    if unknown_slurm_submit_count:
        slurm_submit_value = UNKNOWN_AFTER_ATTEMPT
    else:
        slurm_submit_value = slurm_submit_count > 0
    hydro_result_table_write: bool | str = slurm_submit_value
    met_result_table_write: bool | str = slurm_submit_value
    pipeline_status_write: bool | str
    if unknown_pipeline_status_write_count:
        pipeline_status_write = UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_status_write = pipeline_status_write_count > 0
    pipeline_event_write: bool | str
    if unknown_pipeline_event_write_count:
        pipeline_event_write = UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_event_write = pipeline_event_write_count > 0
    proof: dict[str, Any] = {
        "status": status,
        "execution_required": bool(execution_payloads),
        "orchestration_called": orchestration_called,
        "mutation_occurred": _execution_mutation_value(
            slurm_submit_value,
            pipeline_status_write,
            pipeline_event_write,
        ),
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "submitted_count": submitted_count,
        "slurm_submit_called": slurm_submit_value,
        "slurm_submit_count": slurm_submit_count,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
    }
    if unknown_slurm_submit_count:
        proof["slurm_submit_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_slurm_submit_count"] = unknown_slurm_submit_count
        proof["slurm_submit_proven_absent"] = False
    else:
        proof["slurm_submit_proven_absent"] = slurm_submit_count == 0
    proof["hydro_result_table_writes_proven_absent"] = hydro_result_table_write is False
    proof["met_result_table_writes_proven_absent"] = met_result_table_write is False
    if unknown_pipeline_status_write_count:
        proof["pipeline_status_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_status_write_count"] = unknown_pipeline_status_write_count
        proof["pipeline_status_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
    if unknown_pipeline_event_write_count:
        proof["pipeline_event_write_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_event_write_count"] = unknown_pipeline_event_write_count
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_execution_count"] = unknown_after_attempt_count
        if hydro_result_table_write == UNKNOWN_AFTER_ATTEMPT:
            proof["hydro_result_table_writes_proven_absent"] = False
        if met_result_table_write == UNKNOWN_AFTER_ATTEMPT:
            proof["met_result_table_writes_proven_absent"] = False
        if unknown_pipeline_status_write_count or pipeline_status_write_count:
            proof["pipeline_status_writes_proven_absent"] = False
        if unknown_pipeline_event_write_count or pipeline_event_write_count:
            proof["pipeline_event_writes_proven_absent"] = False
    return proof


def _slurm_cancellation_proof(
    *,
    cancellation_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "cancellation_required": cancellation_required,
        "cancel_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif cancellation_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def _slurm_cancellation_proof_from_evidence(
    cancellation_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    cancel_called = any(item.get("cancel_attempted") is True for item in cancellation_evidence)
    cancelled_count = _slurm_cancelled_count(cancellation_evidence)
    blocked_count = _slurm_cancellation_blocked_count(cancellation_evidence)
    unknown_after_attempt_count = sum(
        1 for item in cancellation_evidence if item.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_status_write") is True)
    pipeline_event_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_event_write") is True)
    proof: dict[str, Any] = {
        "status": _scheduler_pass_status_from_cancellation(cancellation_evidence),
        "cancellation_required": bool(cancellation_evidence),
        "cancel_called": cancel_called,
        "mutation_occurred": cancelled_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "cancelled_job_count": cancelled_count,
        "blocked_cancellation_count": blocked_count,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
    }
    if pipeline_status_write_count or pipeline_event_write_count:
        proof["mutation_occurred"] = True
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = UNKNOWN_AFTER_ATTEMPT
        proof["unknown_cancellation_count"] = unknown_after_attempt_count
        proof["slurm_cancellation_proven_absent"] = False
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    return proof


def _slurm_status_sync_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("updated_job_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _slurm_status_sync_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("failed_sync_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _slurm_status_sync_mutated(proof: Mapping[str, Any]) -> bool:
    return proof.get("mutation_occurred") is True


def _slurm_status_sync_failed(proof: Mapping[str, Any]) -> bool:
    return str(proof.get("status") or "") == "failed" and proof.get("sync_called") is True


def _slurm_cancelled_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for item in cancellation_evidence:
        for job in item.get("cancelled_jobs") or []:
            if isinstance(job, Mapping) and str(job.get("status") or "").lower() == "cancelled":
                total += 1
    return total


def _slurm_cancellation_blocked_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in cancellation_evidence if str(item.get("status") or "") != "cancelled")


def _slurm_cancellation_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("unknown_cancellation_count")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 1 if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT else 0


def _scheduler_mutation_proof(
    *,
    execution_write_proof: Mapping[str, Any],
    slurm_status_sync_proof: Mapping[str, Any],
    slurm_cancellation_proof: Mapping[str, Any],
) -> dict[str, bool | str]:
    execution_slurm_submit = _slurm_submit_proof_value(execution_write_proof)
    hydro_result_table_write = _named_proof_value(
        execution_write_proof,
        "hydro_result_table_writes",
        "hydro_result_table_writes_proven_absent",
    )
    met_result_table_write = _named_proof_value(
        execution_write_proof,
        "met_result_table_writes",
        "met_result_table_writes_proven_absent",
    )
    sync_mutation = _proof_mutation_value(slurm_status_sync_proof)
    cancellation_mutation = _proof_mutation_value(slurm_cancellation_proof)
    pipeline_status_write = _merge_proof_values(
        _pipeline_status_write_proof_value(execution_write_proof),
        sync_mutation,
        _pipeline_status_write_proof_value(slurm_cancellation_proof),
    )
    pipeline_event_write = _merge_proof_values(
        _pipeline_event_write_proof_value(execution_write_proof),
        sync_mutation,
        _pipeline_event_write_proof_value(slurm_cancellation_proof),
    )
    return {
        "slurm_submit_called": execution_slurm_submit,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "slurm_status_sync_writes": sync_mutation,
        "slurm_cancellation_writes": cancellation_mutation,
    }


def _proof_mutation_value(proof: Mapping[str, Any]) -> bool | str:
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def _named_proof_value(proof: Mapping[str, Any], write_field: str, absent_field: str) -> bool | str:
    value = proof.get(write_field)
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get(absent_field) is True:
        return False
    if proof.get(absent_field) is False and proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return _proof_mutation_value(proof)


def _slurm_submit_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("slurm_submit_called")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("slurm_submit_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if _positive_count(proof.get("slurm_submit_count")):
        return True
    if proof.get("slurm_submit_proven_absent") is True:
        return False
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def _pipeline_status_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_status_writes")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_status_write_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if "pipeline_status_write_count" in proof:
        return _positive_count(proof.get("pipeline_status_write_count"))
    if proof.get("pipeline_status_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def _pipeline_event_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_event_writes")
    if value == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_event_write_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == UNKNOWN_AFTER_ATTEMPT:
        return UNKNOWN_AFTER_ATTEMPT
    if "pipeline_event_write_count" in proof:
        return _positive_count(proof.get("pipeline_event_write_count"))
    if proof.get("pipeline_event_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def _merge_proof_values(*values: bool | str) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def _positive_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nested_bool(mapping: Mapping[str, Any], key: str, *, fallback: bool | None = None) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "available", "ready", "yes", "1"}:
            return True
        if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
            return False
    return fallback


def _default_adapters() -> Mapping[str, CycleDiscoveryAdapter]:
    from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
    from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

    return {
        "gfs": GFSAdapter(config=GFSAdapterConfig(), repository=None),
        "IFS": IFSAdapter(config=IFSAdapterConfig(), repository=None),
    }


def _active_repository_from_env() -> ActiveCandidateRepository:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _orchestrator_repository_from_env() -> Any:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _empty_counts() -> dict[str, int]:
    return {
        "candidate_count": 0,
        "blocked_candidate_count": 0,
        "skipped_candidate_count": 0,
        "selected_model_count": 0,
        "source_cycle_count": 0,
        "submitted_count": 0,
        "failed_count": 0,
        "partial_count": 0,
        "slurm_status_sync_count": 0,
        "slurm_status_sync_unknown_count": 0,
        "slurm_cancelled_count": 0,
        "slurm_cancellation_blocked_count": 0,
        "slurm_cancellation_unknown_count": 0,
    }


def _no_mutation_proof() -> dict[str, bool]:
    return {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "slurm_status_sync_called": False,
        "slurm_cancellation_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
        "pipeline_status_writes": False,
        "pipeline_event_writes": False,
    }


def _scheduler_lock_evidence_root_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler_root_preflight_not_required(config)
    allowed_roots = _scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler_allowed_roots_policy_check(config, allowed_roots)
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    for field_name, path in (
        ("workspace_root", config._workspace_root_preflight_path),
        ("lock_root", config._lock_root_preflight_path),
        ("evidence_root", config._evidence_root_preflight_path),
    ):
        check, blocker = _scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=True,
            allow_create=False,
            require_approved_root=enforce_approved_roots and field_name == "workspace_root",
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=config._workspace_root_preflight_path.resolve(strict=False),
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    return _scheduler_root_preflight_payload(config, checks, blockers)


def _scheduler_runtime_root_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler_root_preflight_not_required(config)
    allowed_roots = _scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler_allowed_roots_policy_check(config, allowed_roots)
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    for field_name, path in (
        ("workspace_root", config._workspace_root_preflight_path),
        ("object_store_root", config._object_store_root_preflight_path),
        ("published_artifact_root", config._published_artifact_root_preflight_path),
        ("runtime_root", config._runtime_root_preflight_path),
        ("temp_root", config._temp_root_preflight_path),
        ("lock_root", config._lock_root_preflight_path),
        ("evidence_root", config._evidence_root_preflight_path),
    ):
        check, blocker = _scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=True,
            allow_create=False,
            require_approved_root=enforce_approved_roots and field_name not in {"lock_root", "evidence_root"},
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=config._workspace_root_preflight_path.resolve(strict=False),
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    service_role_check, service_role_blocker = _scheduler_service_role_check(config.service_role)
    checks["service_role"] = service_role_check
    if service_role_blocker is not None:
        blockers.append(service_role_blocker)
    return _scheduler_root_preflight_payload(config, checks, blockers)


def _scheduler_root_preflight_not_required(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "status": "not_required",
        "required": False,
        "blockers": [],
        "checks": {},
        "allowed_roots": [str(root) for root in _scheduler_allowed_roots(config)],
    }


def _scheduler_root_preflight_payload(
    config: ProductionSchedulerConfig,
    checks: Mapping[str, Any],
    blockers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "blocked" if blockers else "ready",
        "required": True,
        "blockers": [dict(blocker) for blocker in blockers],
        "checks": dict(checks),
        "allowed_roots": [str(root) for root in _scheduler_allowed_roots(config)],
    }


def _scheduler_root_check(
    field_name: str,
    value: Path | str | None,
    allowed_roots: Sequence[Path],
    *,
    required: bool,
    must_exist: bool,
    allow_create: bool,
    require_approved_root: bool = True,
    require_under_workspace: bool = False,
    workspace_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value in (None, ""):
        check = {
            "configured": False,
            "path": None,
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
        }
        if required:
            return check, _scheduler_root_blocker(field_name, "MISSING", None)
        return check, None
    path = Path(value).expanduser()
    if not path.is_absolute():
        check = {
            "configured": True,
            "path": str(path),
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
        }
        return check, _scheduler_root_blocker(field_name, "RELATIVE", str(path))
    resolved = path.resolve(strict=False)
    exists = False
    is_dir = False
    is_symlink = False
    writable = False
    unsafe_reason: str | None = None
    try:
        path_stat = path.lstat()
        exists = True
        is_symlink = stat.S_ISLNK(path_stat.st_mode)
        is_dir = stat.S_ISDIR(path_stat.st_mode)
        if is_dir and not is_symlink:
            writable = _directory_is_writable(path)
    except FileNotFoundError:
        exists = False
        if allow_create:
            parent = path.parent
            try:
                parent_stat = parent.lstat()
                parent_is_dir = stat.S_ISDIR(parent_stat.st_mode)
                parent_is_symlink = stat.S_ISLNK(parent_stat.st_mode)
                writable = parent_is_dir and not parent_is_symlink and _directory_is_writable(parent)
            except FileNotFoundError:
                writable = False
            except OSError as error:
                unsafe_reason = _scheduler_root_os_error_reason(error)
    except OSError as error:
        unsafe_reason = _scheduler_root_os_error_reason(error)
    contained = _path_is_under_any(resolved, allowed_roots) if require_approved_root else True
    under_workspace = True
    if require_under_workspace:
        if workspace_root is None:
            under_workspace = False
        else:
            try:
                resolved.relative_to(workspace_root)
            except ValueError:
                under_workspace = False
    check = {
        "configured": True,
        "path": str(resolved),
        "exists": exists,
        "is_dir": is_dir,
        "symlink": is_symlink,
        "contained": contained,
        "approved_root_required": require_approved_root,
        "writable": writable,
        "allow_create": allow_create,
    }
    if require_under_workspace:
        check["under_workspace"] = under_workspace
    if unsafe_reason is not None:
        check["unsafe_reason"] = unsafe_reason
        return check, _scheduler_root_blocker(field_name, unsafe_reason, str(resolved))
    if require_under_workspace and not under_workspace:
        return check, _scheduler_root_blocker(field_name, "OUT_OF_WORKSPACE", str(resolved))
    if is_symlink:
        return check, _scheduler_root_blocker(field_name, "SYMLINK", str(resolved))
    if require_approved_root and not contained:
        return check, _scheduler_root_blocker(field_name, "OUT_OF_APPROVED_ROOT", str(resolved))
    if must_exist and not exists:
        return check, _scheduler_root_blocker(field_name, "NOT_FOUND", str(resolved))
    if exists and not is_dir:
        return check, _scheduler_root_blocker(field_name, "NOT_DIRECTORY", str(resolved))
    if not writable:
        return check, _scheduler_root_blocker(field_name, "NOT_WRITABLE", str(resolved))
    return check, None


def _scheduler_root_blocker(field_name: str, reason: str, path: str | None) -> dict[str, Any]:
    code = f"SCHEDULER_ROOT_{field_name.upper()}_{reason}"
    blocker = {
        "code": code,
        "field": field_name,
        "reason": reason.lower(),
        "message": f"Production scheduler {field_name} is not a safe writable runtime root.",
    }
    if path is not None:
        blocker["path"] = path
    return blocker


def _scheduler_root_os_error_reason(error: OSError) -> str:
    if error.errno in {ELOOP, ENOTDIR}:
        return "UNSAFE_PATH"
    if error.errno in {EACCES, EPERM}:
        return "NOT_WRITABLE"
    return "UNAVAILABLE"


def _directory_is_writable(path: Path) -> bool:
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            return False
        if path_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0:
            return False
        if path_stat.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0:
            return False
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def _evidence_reservation_blocked_payload(
    *,
    pass_id: str,
    artifact_path: Path,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "nhms.production_scheduler.pre_execution_evidence_reservation.v1",
        "pass_id": pass_id,
        "status": "blocked",
        "artifact_path": str(artifact_path),
        "reason": reason,
        "error_code": "EVIDENCE_WRITE_PRECHECK_FAILED",
        "message": "Scheduler evidence write proof failed before production mutation.",
    }
    payload.update(dict(details or {}))
    return _evidence_safe(payload)


def _evidence_write_error_payload(error: OSError) -> dict[str, Any]:
    if isinstance(error, SchedulerEvidenceWriteError):
        return {"reason": error.reason, **error.details}
    return {"reason": "evidence_write_failed", "error": str(error)}


def _scheduler_service_role_check(service_role: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    role = (service_role or "").strip()
    check = {"configured": bool(role), "value": role or None, "compute_control": role == "compute_control"}
    if role != "compute_control":
        return (
            check,
            {
                "code": "SCHEDULER_ROOT_SERVICE_ROLE_NOT_COMPUTE_CONTROL",
                "field": "NHMS_SERVICE_ROLE",
                "message": "Production scheduler no-flag business validation must run as compute_control.",
            },
        )
    return check, None


def _scheduler_allowed_roots_policy_check(
    config: ProductionSchedulerConfig,
    allowed_roots: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    configured_roots = tuple(root for root in config.allowed_storage_roots if root not in (None, ""))
    check = {
        "env": "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "configured": bool(configured_roots),
        "non_empty": bool(allowed_roots),
        "allowed_roots": [str(root) for root in allowed_roots],
        "independent_policy_required": True,
    }
    if not allowed_roots:
        return check, _scheduler_root_blocker("allowed_roots", "MISSING", None)
    return check, None


def _scheduler_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        root = Path(value).expanduser().resolve(strict=False)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _scheduler_resolved_runtime_roots(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "workspace_root": _root_evidence_item(
            config.workspace_root,
            env="WORKSPACE_ROOT",
            required=config.require_runtime_roots,
        ),
        "object_store_root": _root_evidence_item(
            config.object_store_root,
            env="OBJECT_STORE_ROOT",
            required=config.require_runtime_roots,
        ),
        "published_artifact_root": _root_evidence_item(
            config.published_artifact_root,
            env="NHMS_PUBLISHED_ARTIFACT_ROOT",
            required=config.require_runtime_roots,
        ),
        "lock_root": _root_evidence_item(
            Path(config.lock_path).parent,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler",
            required=config.require_runtime_roots,
        ),
        "lock_path": _root_evidence_item(
            config.lock_path,
            env="NHMS_SCHEDULER_LOCK_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/production-scheduler.lock",
            required=config.require_runtime_roots,
        ),
        "evidence_root": _root_evidence_item(
            config.evidence_dir,
            env="NHMS_SCHEDULER_EVIDENCE_ROOT",
            fallback="WORKSPACE_ROOT/scheduler/evidence",
            required=config.require_runtime_roots,
        ),
        "runtime_root": _root_evidence_item(
            config.runtime_root,
            env="NHMS_SCHEDULER_RUNTIME_ROOT|NHMS_RUNTIME_ROOT|RUN_WORKSPACE_ROOT|SHUD_RUNTIME_ROOT",
            required=config.require_runtime_roots,
        ),
        "temp_root": _root_evidence_item(
            config.temp_root,
            env="NHMS_SCHEDULER_TEMP_ROOT|NHMS_TEMP_ROOT|TMPDIR",
            required=config.require_runtime_roots,
        ),
    }


def _root_evidence_item(
    value: Path | str | None,
    *,
    env: str,
    required: bool,
    fallback: str | None = None,
) -> dict[str, Any]:
    path = None if value in (None, "") else str(Path(value).expanduser().resolve(strict=False))
    payload = {
        "path": path,
        "configured": path is not None,
        "env": env,
        "required": required,
    }
    if fallback is not None:
        payload["fallback"] = fallback
    return payload


def _scheduler_runtime_config_evidence(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "service_role": config.service_role,
        "require_runtime_roots": config.require_runtime_roots,
        "dry_run": config.dry_run,
        "continuous": config.continuous,
        "interval_seconds": config.interval_seconds,
        "sources": list(config.sources),
        "model_ids": list(config.model_ids),
        "basin_ids": list(config.basin_ids),
        "max_cycles_per_source": config.max_cycles_per_source,
        "retry_limit": config.retry_limit,
    }


def _scheduler_pass_status_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not execution_evidence:
        return "planned"
    if all(str(item.get("status")) == "preflight_blocked" for item in execution_evidence):
        return "preflight_blocked"
    if any(item.get("submitted") is True for item in execution_evidence):
        if _scheduler_partial_count_from_execution(execution_evidence) > 0:
            return "submitted_partial"
        return "submitted"
    if any(str(item.get("status")) in {"blocked", "failed"} for item in execution_evidence):
        return "preflight_blocked"
    return str(execution_evidence[-1].get("status") or "planned")


def _scheduler_partial_count_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> int:
    if not any(item.get("submitted") is True or item.get("execution_attempted") is True for item in execution_evidence):
        return 0
    return sum(1 for item in execution_evidence if _is_partial_candidate_evidence(item))


def _scheduler_failed_count_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in execution_evidence if _is_failed_candidate_evidence(item))


def _is_failed_candidate_evidence(item: Mapping[str, Any]) -> bool:
    return _is_failed_model_run_status(str(item.get("status") or ""))


def _is_partial_candidate_evidence(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if item.get("submitted") is True:
        return _is_non_submitted_terminal_or_unavailable_status(status)
    return _is_non_submitted_terminal_or_unavailable_status(status) or status.endswith("_partial")


def _is_failed_model_run_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"failed", "permanently_failed", "submission_failed"} or normalized.endswith("_failed")


def _is_non_submitted_terminal_or_unavailable_status(status: str) -> bool:
    normalized = status.strip().lower()
    return (
        _is_failed_model_run_status(normalized)
        or normalized
        in {
            "blocked",
            "cancelled",
            "preflight_blocked",
            "unavailable",
        }
        or normalized.endswith(("_blocked", "_cancelled", "_unavailable"))
    )


def _empty_model_discovery() -> dict[str, Any]:
    return {
        "active_model_count": 0,
        "runnable_model_count": 0,
        "selected_model_count": 0,
        "excluded_model_count": 0,
        "models": [],
        "exclusions": [],
        "operator_filters": {"expression": None, "excluded_runnable_count": 0},
    }


def _evidence_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        manifest_redacted = _redact_secret_manifest_for_evidence(
            {str(key): _evidence_safe(nested) for key, nested in value.items()},
            "evidence",
        )
        return redact_payload(manifest_redacted)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_evidence_safe(item) for item in value]
    if isinstance(value, str):
        return redact_payload(value)
    return value


def _normalize_sources(sources: Sequence[str]) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    normalized: list[str] = []
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_source in sources:
        source_id = normalize_source_id(raw_source)
        if source_id in seen:
            exclusions.append(
                {
                    "type": "source",
                    "source_id": source_id,
                    "status": "excluded",
                    "reason": "duplicate_source",
                }
            )
            continue
        seen.add(source_id)
        normalized.append(source_id)
    return tuple(normalized), exclusions


def _confined_path(value: Path | str, workspace_root: Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    resolved_parent = path.parent.resolve()
    candidate = resolved_parent / path.name
    _require_under_workspace(resolved_parent, workspace_root, field_name)
    return candidate


def _reject_blank_config_path(value: Path | str | None, field_name: str) -> None:
    if isinstance(value, str) and value.strip() == "":
        raise ValueError(f"production scheduler {field_name} must not be blank")


def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).expanduser().resolve()


def _config_path_preserve_final_component(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve(strict=False) / path.name


def _config_path_relative_to_preserve_final(value: Path | str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.parent.resolve(strict=False) / path.name


def _optional_config_path_relative_to_preserve_final(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    return _config_path_relative_to_preserve_final(value, base)


def _resolve_optional_config_path(value: Path | None) -> Path | None:
    if value is None:
        return None
    return value.resolve()


def _optional_config_path_relative_to(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _env_path_list(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value in (None, ""):
        return ()
    return tuple(item.strip() for item in str(value).split(os.pathsep) if item.strip())


def _require_under_workspace(path: Path, workspace_root: Path, field_name: str) -> None:
    try:
        path.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError(f"production scheduler {field_name} must be under workspace_root") from error


def _require_safe_directory_final_component(path: Path, workspace_root: Path, field_name: str) -> None:
    _require_under_workspace(path.parent.resolve(), workspace_root, field_name)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"production scheduler {field_name} must be a safe directory") from error
    if stat.S_ISLNK(path_stat.st_mode):
        resolved = path.resolve(strict=False)
        _require_under_workspace(resolved, workspace_root, field_name)
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"production scheduler {field_name} must be a directory")
        return
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")


def _open_lock_parent_directory(lock_parent: Path, workspace_root: Path | None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    if workspace_root is None:
        lock_parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(lock_parent, directory_flags)
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
            raise

    workspace_root = workspace_root.resolve()
    _ensure_workspace_directory(workspace_root)
    try:
        relative_parent = lock_parent.relative_to(workspace_root)
    except ValueError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error

    try:
        root_fd = os.open(workspace_root, directory_flags)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise

    parent_fd = root_fd
    try:
        for component in relative_parent.parts:
            if component in ("", "."):
                continue
            if component == "..":
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {ELOOP, ENOTDIR}:
                    raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
                raise
            os.close(parent_fd)
            parent_fd = child_fd
    except Exception:
        os.close(parent_fd)
        raise
    return parent_fd


def _ensure_workspace_directory(workspace_root: Path) -> None:
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise
    try:
        root_stat = workspace_root.lstat()
    except FileNotFoundError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")


def _open_evidence_directory(evidence_dir: Path, workspace_root: Path) -> int:
    try:
        return _open_lock_parent_directory(evidence_dir, workspace_root)
    except UnsafeSchedulerLockError as error:
        raise SchedulerEvidenceWriteError("unsafe_evidence_directory") from error


def _write_new_regular_file(
    artifact_name: str,
    serialized: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(artifact_name, flags, 0o644, dir_fd=dir_fd)
    except FileExistsError as error:
        try:
            artifact_stat = os.stat(artifact_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            artifact_stat = None
        reason = (
            "evidence_artifact_exists"
            if artifact_stat is not None and stat.S_ISREG(artifact_stat.st_mode)
            else "unsafe_evidence_artifact"
        )
        raise SchedulerEvidenceWriteError(
            reason,
            {"artifact_path": str(artifact_path)},
        ) from error
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise SchedulerEvidenceWriteError(
                "unsafe_evidence_artifact",
                {"artifact_path": str(artifact_path)},
            ) from error
        raise
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with handle:
        handle.write(serialized)


def _require_evidence_artifact_available(
    artifact_name: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    try:
        artifact_stat = os.stat(artifact_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno in {EISDIR, ELOOP, ENOTDIR}:
            raise SchedulerEvidenceWriteError(
                "unsafe_evidence_artifact",
                {"artifact_path": str(artifact_path)},
            ) from error
        raise
    reason = "evidence_artifact_exists" if stat.S_ISREG(artifact_stat.st_mode) else "unsafe_evidence_artifact"
    raise SchedulerEvidenceWriteError(reason, {"artifact_path": str(artifact_path)})


def _open_regular_guard_file(guard_name: str, *, dir_fd: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(guard_name, os.O_CREAT | os.O_RDWR | nofollow, 0o644, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file") from error
        raise
    try:
        guard_stat = os.fstat(fd)
        if not stat.S_ISREG(guard_stat.st_mode):
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _unlink_lock_file(lock_name: str, *, parent_fd: int) -> None:
    try:
        os.unlink(lock_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _source_horizon_metadata(discovery: CycleDiscovery, adapter: CycleDiscoveryAdapter) -> dict[str, Any]:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    config = getattr(adapter, "config", None)
    max_lead_hours: int | None = None
    if config is not None and hasattr(config, "forecast_end_hour_for_cycle"):
        max_lead_hours = int(config.forecast_end_hour_for_cycle(cycle_time.hour))
    elif config is not None and hasattr(config, "forecast_end_hour"):
        max_lead_hours = int(getattr(config, "forecast_end_hour"))
    elif source_id == "IFS":
        max_lead_hours = 144 if cycle_time.hour in {6, 18} else 168
    elif source_id == "gfs":
        max_lead_hours = 168
    return {
        "max_lead_hours": max_lead_hours,
        "forecast_horizon_hours": max_lead_hours,
        "policy": "source_cycle",
    }


def _bounded_evidence_payload(payload: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version", SCHEDULER_EVIDENCE_SCHEMA_VERSION),
        "review_contract": payload.get(
            "review_contract",
            {
                "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
                "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
                "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
                "scope": "scheduler_pass_evidence",
            },
        ),
        "pass_id": payload.get("pass_id"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "status": "resource_limit_blocked",
        "execution_mode": payload.get("execution_mode"),
        "readiness_interpretation": payload.get("readiness_interpretation", "non_final_scheduler_evidence"),
        "readiness": payload.get(
            "readiness",
            {
                "schema_version": "nhms.production_readiness.scheduler_input.v1",
                "interpretation": "non_final_scheduler_evidence",
                "live_receipts": [],
                "production_ready": False,
                "final_production_readiness_claimed": False,
                "can_claim_final_production_readiness": False,
            },
        ),
        "limit": {"reason": reason, "max_evidence_bytes": MAX_EVIDENCE_BYTES},
        "counts": payload.get("counts", _empty_counts()),
        "resolved_runtime_roots": payload.get("resolved_runtime_roots"),
        "runtime_config": payload.get("runtime_config"),
        "root_preflight": payload.get("root_preflight"),
        "evidence_pre_execution": payload.get("evidence_pre_execution"),
        "candidates": [],
        "blocked_candidates": [],
        "skipped_candidates": [],
        "duplicate_exclusions": payload.get("duplicate_exclusions", []),
        "source_cycles": [],
        "model_discovery": _empty_model_discovery(),
        "artifact_path": payload.get("artifact_path"),
        "execution_boundary": payload.get("execution_boundary", "planning_only"),
        "execution_write_proof": payload.get("execution_write_proof"),
        "slurm_status_sync_proof": payload.get("slurm_status_sync_proof"),
        "slurm_cancellation_proof": payload.get("slurm_cancellation_proof"),
        "no_mutation_proof": payload.get("no_mutation_proof", _no_mutation_proof()),
    }


def _evidence_status(evidence: Mapping[str, Any], fallback: str) -> str:
    status = evidence.get("status")
    return str(status) if status not in (None, "") else fallback


def _now(config: ProductionSchedulerConfig) -> datetime:
    return config.now or datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
