from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from services.orchestrator import scheduler as _scheduler
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES

__all__ = ("ProductionSchedulerConfig",)


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
        default_factory=lambda: (
            os.getenv("NHMS_SCHEDULER_RUNTIME_ROOT")
            or os.getenv("NHMS_RUNTIME_ROOT")
            or os.getenv("RUN_WORKSPACE_ROOT")
            or os.getenv("SHUD_RUNTIME_ROOT")
        )
    )
    temp_root: Path | str | None = field(
        default_factory=lambda: (
            os.getenv("NHMS_SCHEDULER_TEMP_ROOT") or os.getenv("NHMS_TEMP_ROOT") or os.getenv("TMPDIR")
        )
    )
    scheduler_lock_root: Path | str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_ROOT"))
    scheduler_evidence_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_EVIDENCE_ROOT")
    )
    service_role: str | None = field(default_factory=lambda: os.getenv("NHMS_SERVICE_ROLE"))
    require_runtime_roots: bool = field(default_factory=lambda: _scheduler._env_flag("NHMS_SCHEDULER_REQUIRE_ROOTS"))
    database_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))
    slurm_execution_enabled: bool = field(
        default_factory=lambda: _scheduler._env_flag("NHMS_PRODUCTION_SLURM_ENABLED")
        or _scheduler._env_flag("SLURM_EXECUTION_ENABLED")
    )
    slurm_gateway_url: str = field(default_factory=lambda: os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"))
    service_port: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SERVICE_PORT", _scheduler.DEFAULT_SERVICE_PORT)
    )
    forcing_production_enabled: bool = field(
        default_factory=lambda: _scheduler._env_flag("NHMS_PRODUCTION_FORCING_ENABLED")
    )
    allowed_storage_roots: tuple[Path | str, ...] = field(
        default_factory=lambda: _scheduler._env_path_list("NHMS_SCHEDULER_ALLOWED_ROOTS")
    )
    slurm_job_type_templates: Mapping[str, str] | None = None
    slurm_env: Mapping[str, str] = field(default_factory=dict)
    cancel_active_slurm: bool = False
    sources: tuple[str, ...] = _scheduler.DEFAULT_PRODUCTION_SOURCES
    allowed_cycle_hours_utc: tuple[int, ...] = field(
        default_factory=lambda: _scheduler._env_allowed_cycle_hours_utc(
            "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC",
            _scheduler.DEFAULT_ALLOWED_CYCLE_HOURS_UTC,
        )
    )
    lookback_hours: int = _scheduler.DEFAULT_LOOKBACK_HOURS
    cycle_lag_hours: int = _scheduler.DEFAULT_CYCLE_LAG_HOURS
    max_cycles_per_source: int = _scheduler.DEFAULT_MAX_CYCLES_PER_SOURCE
    backfill_enabled: bool = field(default_factory=lambda: _scheduler._env_flag("NHMS_SCHEDULER_BACKFILL_ENABLED"))
    model_ids: tuple[str, ...] = ()
    basin_ids: tuple[str, ...] = ()
    dry_run: bool = True
    continuous: bool = False
    interval_seconds: float = 300.0
    retry_limit: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SCHEDULER_RETRY_LIMIT", _scheduler.DEFAULT_RETRY_LIMIT)
    )
    concurrent_submit_bound: int = field(
        default_factory=lambda: _scheduler._env_int(
            "NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND",
            _scheduler.DEFAULT_CONCURRENT_SUBMIT_BOUND,
        )
    )
    restart_reconcile_enabled: bool = field(
        default_factory=lambda: _scheduler._env_flag("NHMS_SCHEDULER_RESTART_RECONCILE", default=True)
    )
    candidate_state_job_limit: int = field(
        default_factory=lambda: _scheduler._env_int(
            "NHMS_CANDIDATE_STATE_JOB_LIMIT",
            _scheduler.DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
        )
    )
    candidate_state_event_limit: int = field(
        default_factory=lambda: _scheduler._env_int(
            "NHMS_CANDIDATE_STATE_EVENT_LIMIT",
            _scheduler.DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
        )
    )
    scheduler_lock_backend: str = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_BACKEND", "file"))
    lock_path: Path | str | None = None
    evidence_dir: Path | str | None = None
    lock_ttl_seconds: int = _scheduler.DEFAULT_LOCK_TTL_SECONDS
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
        _scheduler._reject_blank_config_path(self.workspace_root, "workspace_root")
        _scheduler._reject_blank_config_path(self.lock_path, "lock_path")
        _scheduler._reject_blank_config_path(self.evidence_dir, "evidence_dir")
        workspace_root_preflight_path = _scheduler._config_path_preserve_final_component(self.workspace_root)
        workspace_root = workspace_root_preflight_path.resolve()
        object.__setattr__(self, "_workspace_root_preflight_path", workspace_root_preflight_path)
        object.__setattr__(self, "workspace_root", workspace_root)
        object_store_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.object_store_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "object_store_root",
            _scheduler._resolve_optional_config_path(object_store_root_preflight_path),
        )
        object.__setattr__(self, "_object_store_root_preflight_path", object_store_root_preflight_path)
        published_artifact_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.published_artifact_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "published_artifact_root",
            _scheduler._resolve_optional_config_path(published_artifact_root_preflight_path),
        )
        object.__setattr__(self, "_published_artifact_root_preflight_path", published_artifact_root_preflight_path)
        log_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.log_root, workspace_root
        )
        object.__setattr__(self, "log_root", _scheduler._resolve_optional_config_path(log_root_preflight_path))
        runtime_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.runtime_root,
            workspace_root,
        )
        object.__setattr__(self, "runtime_root", _scheduler._resolve_optional_config_path(runtime_root_preflight_path))
        object.__setattr__(self, "_runtime_root_preflight_path", runtime_root_preflight_path)
        temp_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.temp_root, workspace_root
        )
        object.__setattr__(self, "temp_root", _scheduler._resolve_optional_config_path(temp_root_preflight_path))
        object.__setattr__(self, "_temp_root_preflight_path", temp_root_preflight_path)
        scheduler_lock_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.scheduler_lock_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_lock_root",
            _scheduler._resolve_optional_config_path(scheduler_lock_root_preflight_path),
        )
        scheduler_evidence_root_preflight_path = _scheduler._optional_config_path_relative_to_preserve_final(
            self.scheduler_evidence_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_evidence_root",
            _scheduler._resolve_optional_config_path(scheduler_evidence_root_preflight_path),
        )
        object.__setattr__(self, "service_role", str(self.service_role).strip() if self.service_role else None)
        object.__setattr__(self, "database_url", str(self.database_url).strip() if self.database_url else None)
        allowed_roots = tuple(_scheduler._optional_config_path(root) for root in self.allowed_storage_roots if root)
        object.__setattr__(self, "allowed_storage_roots", allowed_roots)
        templates = dict(self.slurm_job_type_templates or DEFAULT_JOB_TYPE_TEMPLATES)
        object.__setattr__(self, "slurm_job_type_templates", templates)
        object.__setattr__(self, "slurm_env", _scheduler._production_slurm_env(dict(self.slurm_env)))
        object.__setattr__(self, "slurm_gateway_url", str(self.slurm_gateway_url or "").strip())
        object.__setattr__(self, "service_port", int(self.service_port))
        if len(self.sources) > _scheduler.MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {_scheduler.MAX_SOURCES}")
        sources, source_exclusions = _scheduler._normalize_sources(self.sources)
        if len(sources) > _scheduler.MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {_scheduler.MAX_SOURCES}")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "source_exclusions", tuple(source_exclusions))
        object.__setattr__(
            self,
            "allowed_cycle_hours_utc",
            _scheduler._normalize_allowed_cycle_hours_utc(self.allowed_cycle_hours_utc),
        )
        lookback_hours = max(int(self.lookback_hours), 0)
        if lookback_hours > _scheduler.MAX_LOOKBACK_HOURS:
            raise ValueError(f"production scheduler lookback_hours exceeds limit {_scheduler.MAX_LOOKBACK_HOURS}")
        object.__setattr__(self, "lookback_hours", lookback_hours)
        object.__setattr__(self, "cycle_lag_hours", max(int(self.cycle_lag_hours), 0))
        max_cycles_per_source = int(self.max_cycles_per_source)
        if max_cycles_per_source < 1:
            raise ValueError("production scheduler max_cycles_per_source must be at least 1")
        if max_cycles_per_source > _scheduler.MAX_CYCLES_PER_SOURCE:
            raise ValueError(
                f"production scheduler max_cycles_per_source exceeds limit {_scheduler.MAX_CYCLES_PER_SOURCE}"
            )
        object.__setattr__(self, "max_cycles_per_source", max_cycles_per_source)
        object.__setattr__(self, "model_ids", tuple(str(model_id) for model_id in self.model_ids if model_id))
        object.__setattr__(self, "basin_ids", tuple(str(basin_id) for basin_id in self.basin_ids if basin_id))
        object.__setattr__(self, "interval_seconds", max(float(self.interval_seconds), 1.0))
        object.__setattr__(self, "retry_limit", max(int(self.retry_limit), 0))
        object.__setattr__(self, "concurrent_submit_bound", max(int(self.concurrent_submit_bound), 1))
        object.__setattr__(self, "candidate_state_job_limit", max(int(self.candidate_state_job_limit), 1))
        object.__setattr__(self, "candidate_state_event_limit", max(int(self.candidate_state_event_limit), 1))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        lock_backend = str(self.scheduler_lock_backend or "file").strip().lower()
        if lock_backend not in {"file", "postgres"}:
            raise ValueError("production scheduler scheduler_lock_backend must be 'file' or 'postgres'")
        object.__setattr__(self, "scheduler_lock_backend", lock_backend)
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
            lock_path = _scheduler._confined_path(lock_root / "production-scheduler.lock", workspace_root, "lock_path")
            object.__setattr__(self, "_lock_root_preflight_path", lock_root_preflight_path)
            object.__setattr__(self, "lock_path", lock_path)
        else:
            lock_path_preflight_path = _scheduler._config_path_relative_to_preserve_final(
                self.lock_path, workspace_root
            )
            lock_path = _scheduler._confined_path(self.lock_path, workspace_root, "lock_path")
            _scheduler._require_under_workspace(lock_path, workspace_root, "lock_path")
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
            evidence_dir = _scheduler._confined_path(evidence_root, workspace_root, "evidence_dir")
            _scheduler._require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_root_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        else:
            evidence_dir_preflight_path = _scheduler._config_path_relative_to_preserve_final(
                self.evidence_dir, workspace_root
            )
            evidence_dir = _scheduler._confined_path(self.evidence_dir, workspace_root, "evidence_dir")
            _scheduler._require_under_workspace(evidence_dir, workspace_root, "evidence_dir")
            _scheduler._require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_dir_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        if self.now is not None:
            object.__setattr__(self, "now", _scheduler._ensure_utc(self.now))
