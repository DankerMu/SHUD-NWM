from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from packages.common.redaction import redact_payload
from services.orchestrator import scheduler as _scheduler
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES

__all__ = ("ProductionSchedulerConfig",)

_DB_FREE_REQUIRED_ENV = "NHMS_SCHEDULER_DB_FREE_REQUIRED"
_DB_FREE_SELECTOR_SPECS = (
    ("scheduler_state_backend", "NHMS_SCHEDULER_STATE_BACKEND", "postgres"),
    ("scheduler_lock_backend", "NHMS_SCHEDULER_LOCK_BACKEND", "file"),
    ("scheduler_registry_backend", "NHMS_SCHEDULER_REGISTRY_BACKEND", "postgres"),
    ("scheduler_canonical_readiness_backend", "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND", "postgres"),
    ("scheduler_journal_backend", "NHMS_SCHEDULER_JOURNAL_BACKEND", "postgres"),
    ("scheduler_state_index_backend", "NHMS_SCHEDULER_STATE_INDEX_BACKEND", "postgres"),
)
_DB_FREE_PATH_SPECS = (
    ("scheduler_registry_manifest", "NHMS_SCHEDULER_REGISTRY_MANIFEST", "file"),
    ("scheduler_canonical_readiness_index", "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX", "file"),
    ("scheduler_journal_root", "NHMS_SCHEDULER_JOURNAL_ROOT", "directory"),
    ("scheduler_state_index", "NHMS_SCHEDULER_STATE_INDEX", "file"),
)
_DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES = frozenset({"s3", "published"})
_DB_FREE_DB_BACKEND_VALUES = frozenset({"postgres", "postgresql", "psycopg", "psycopg2", "pg"})
_DB_FREE_OBJECT_STORE_PREFIX_ENV = "OBJECT_STORE_PREFIX"
_DB_FREE_PUBLIC_OBJECT_PREFIXES = frozenset({"logs", "manifests", "products", "runs"})
_DB_FREE_SAFE_OBJECT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DB_FREE_ENCODED_FORBIDDEN_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_DB_FREE_CREDENTIAL_WORDS = (
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "session_key",
    "signature",
)


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
    database_url_configured: bool = field(default_factory=lambda: os.getenv("DATABASE_URL") is not None)
    scheduler_db_free_required: bool = field(default_factory=lambda: _scheduler._env_flag(_DB_FREE_REQUIRED_ENV))
    scheduler_state_backend: str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_STATE_BACKEND"))
    scheduler_registry_backend: str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_REGISTRY_BACKEND")
    )
    scheduler_registry_manifest: str | Path | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_REGISTRY_MANIFEST")
    )
    scheduler_canonical_readiness_backend: str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND")
    )
    scheduler_canonical_readiness_index: str | Path | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_CANONICAL_READINESS_INDEX")
    )
    scheduler_journal_backend: str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_JOURNAL_BACKEND"))
    scheduler_journal_root: str | Path | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_JOURNAL_ROOT"))
    scheduler_state_index_backend: str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND")
    )
    scheduler_state_index: str | Path | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_STATE_INDEX"))
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
    progress_guard_max_no_progress_steps: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SCHEDULER_PROGRESS_GUARD_MAX_NO_PROGRESS_STEPS", 256)
    )
    timing_level: str = field(
        default_factory=lambda: (os.environ.get("NHMS_SCHEDULER_TIMING_LEVEL") or "stage").strip().lower()
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
    scheduler_lock_backend: str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_BACKEND"))
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
    _workspace_root_raw_preflight_path: Path = field(init=False, repr=False, compare=False)
    _object_store_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _published_artifact_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _runtime_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _temp_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _lock_root_raw_preflight_path: Path = field(init=False, repr=False, compare=False)
    _evidence_root_raw_preflight_path: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        db_free_required = bool(self.scheduler_db_free_required)
        object.__setattr__(self, "scheduler_db_free_required", db_free_required)
        _scheduler._reject_blank_config_path(self.workspace_root, "workspace_root")
        _scheduler._reject_blank_config_path(self.lock_path, "lock_path")
        _scheduler._reject_blank_config_path(self.evidence_dir, "evidence_dir")
        workspace_root_raw_preflight_path = _raw_config_path_preserve_components(
            self.workspace_root,
            db_free_required=db_free_required,
        )
        workspace_root_preflight_path = _config_path_preserve_final_component_for_mode(
            self.workspace_root,
            db_free_required=db_free_required,
        )
        workspace_root = _resolve_config_path_for_mode(
            workspace_root_preflight_path,
            db_free_required=db_free_required,
        )
        object.__setattr__(self, "_workspace_root_raw_preflight_path", workspace_root_raw_preflight_path)
        object.__setattr__(self, "_workspace_root_preflight_path", workspace_root_preflight_path)
        object.__setattr__(self, "workspace_root", workspace_root)
        object_store_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.object_store_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object_store_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.object_store_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "object_store_root",
            _resolve_optional_config_path_for_mode(
                object_store_root_preflight_path,
                db_free_required=db_free_required,
            ),
        )
        object.__setattr__(self, "_object_store_root_raw_preflight_path", object_store_root_raw_preflight_path)
        object.__setattr__(self, "_object_store_root_preflight_path", object_store_root_preflight_path)
        published_artifact_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.published_artifact_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        published_artifact_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.published_artifact_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "published_artifact_root",
            _resolve_optional_config_path_for_mode(
                published_artifact_root_preflight_path,
                db_free_required=db_free_required,
            ),
        )
        object.__setattr__(
            self,
            "_published_artifact_root_raw_preflight_path",
            published_artifact_root_raw_preflight_path,
        )
        object.__setattr__(self, "_published_artifact_root_preflight_path", published_artifact_root_preflight_path)
        log_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.log_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "log_root",
            _resolve_optional_config_path_for_mode(log_root_preflight_path, db_free_required=db_free_required),
        )
        runtime_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.runtime_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        runtime_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.runtime_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "runtime_root",
            _resolve_optional_config_path_for_mode(runtime_root_preflight_path, db_free_required=db_free_required),
        )
        object.__setattr__(self, "_runtime_root_raw_preflight_path", runtime_root_raw_preflight_path)
        object.__setattr__(self, "_runtime_root_preflight_path", runtime_root_preflight_path)
        temp_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.temp_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        temp_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.temp_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "temp_root",
            _resolve_optional_config_path_for_mode(temp_root_preflight_path, db_free_required=db_free_required),
        )
        object.__setattr__(self, "_temp_root_raw_preflight_path", temp_root_raw_preflight_path)
        object.__setattr__(self, "_temp_root_preflight_path", temp_root_preflight_path)
        scheduler_lock_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.scheduler_lock_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        scheduler_lock_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.scheduler_lock_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "scheduler_lock_root",
            _resolve_optional_config_path_for_mode(
                scheduler_lock_root_preflight_path,
                db_free_required=db_free_required,
            ),
        )
        scheduler_evidence_root_raw_preflight_path = _optional_raw_config_path_relative_to_preserve_components(
            self.scheduler_evidence_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        scheduler_evidence_root_preflight_path = _optional_config_path_relative_to_preserve_final_for_mode(
            self.scheduler_evidence_root,
            workspace_root,
            db_free_required=db_free_required,
        )
        object.__setattr__(
            self,
            "scheduler_evidence_root",
            _resolve_optional_config_path_for_mode(
                scheduler_evidence_root_preflight_path,
                db_free_required=db_free_required,
            ),
        )
        object.__setattr__(self, "service_role", str(self.service_role).strip() if self.service_role else None)
        database_url_raw = None if self.database_url is None else str(self.database_url)
        database_url = database_url_raw.strip() if database_url_raw and database_url_raw.strip() else None
        object.__setattr__(self, "database_url", database_url)
        object.__setattr__(self, "database_url_configured", bool(self.database_url_configured or database_url_raw))
        object.__setattr__(
            self,
            "require_runtime_roots",
            bool(self.require_runtime_roots or db_free_required),
        )
        allowed_roots = tuple(
            _optional_config_path_for_mode(root, db_free_required=db_free_required)
            for root in self.allowed_storage_roots
            if root
        )
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
        object.__setattr__(
            self,
            "progress_guard_max_no_progress_steps",
            max(int(self.progress_guard_max_no_progress_steps), 0),
        )
        # NHMS_SCHEDULER_TIMING_LEVEL is a plain string here (case-insensitive,
        # lowercase-normalised); validation is deferred to run_once per D4 so an
        # unrecognised value does not crash the daemon at startup.
        timing_level_raw = self.timing_level if self.timing_level is not None else "stage"
        timing_level_normalised = str(timing_level_raw).strip().lower() or "stage"
        object.__setattr__(self, "timing_level", timing_level_normalised)
        object.__setattr__(self, "candidate_state_job_limit", max(int(self.candidate_state_job_limit), 1))
        object.__setattr__(self, "candidate_state_event_limit", max(int(self.candidate_state_event_limit), 1))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        self._normalize_scheduler_backend_fields()
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
            lock_root_raw_preflight_path = (
                scheduler_lock_root_raw_preflight_path
                if scheduler_lock_root_raw_preflight_path is not None
                else workspace_root / "scheduler"
            )
            lock_path = _confined_path_for_mode(
                lock_root / "production-scheduler.lock",
                workspace_root,
                "lock_path",
                db_free_required=db_free_required,
            )
            object.__setattr__(self, "_lock_root_raw_preflight_path", lock_root_raw_preflight_path)
            object.__setattr__(self, "_lock_root_preflight_path", lock_root_preflight_path)
            object.__setattr__(self, "lock_path", lock_path)
        else:
            lock_path_raw_preflight_path = _raw_config_path_relative_to_preserve_components(
                self.lock_path,
                workspace_root,
                db_free_required=db_free_required,
            )
            lock_path_preflight_path = _config_path_relative_to_preserve_final_for_mode(
                self.lock_path,
                workspace_root,
                db_free_required=db_free_required,
            )
            lock_path = _confined_path_for_mode(
                self.lock_path,
                workspace_root,
                "lock_path",
                db_free_required=db_free_required,
            )
            _require_under_workspace_for_mode(
                lock_path,
                workspace_root,
                "lock_path",
                db_free_required=db_free_required,
            )
            object.__setattr__(self, "_lock_root_raw_preflight_path", lock_path_raw_preflight_path.parent)
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
            evidence_root_raw_preflight_path = (
                scheduler_evidence_root_raw_preflight_path
                if scheduler_evidence_root_raw_preflight_path is not None
                else workspace_root / "scheduler" / "evidence"
            )
            evidence_dir = _confined_path_for_mode(
                evidence_root,
                workspace_root,
                "evidence_dir",
                db_free_required=db_free_required,
            )
            _require_safe_directory_final_component_for_mode(
                evidence_dir,
                workspace_root,
                "evidence_dir",
                db_free_required=db_free_required,
            )
            object.__setattr__(self, "_evidence_root_raw_preflight_path", evidence_root_raw_preflight_path)
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_root_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        else:
            evidence_dir_raw_preflight_path = _raw_config_path_relative_to_preserve_components(
                self.evidence_dir,
                workspace_root,
                db_free_required=db_free_required,
            )
            evidence_dir_preflight_path = _config_path_relative_to_preserve_final_for_mode(
                self.evidence_dir,
                workspace_root,
                db_free_required=db_free_required,
            )
            evidence_dir = _confined_path_for_mode(
                self.evidence_dir,
                workspace_root,
                "evidence_dir",
                db_free_required=db_free_required,
            )
            _require_under_workspace_for_mode(
                evidence_dir,
                workspace_root,
                "evidence_dir",
                db_free_required=db_free_required,
            )
            _require_safe_directory_final_component_for_mode(
                evidence_dir,
                workspace_root,
                "evidence_dir",
                db_free_required=db_free_required,
            )
            object.__setattr__(self, "_evidence_root_raw_preflight_path", evidence_dir_raw_preflight_path)
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_dir_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        if self.now is not None:
            object.__setattr__(self, "now", _scheduler._ensure_utc(self.now))

    @property
    def db_free_required(self) -> bool:
        return self.scheduler_db_free_required

    def _normalize_scheduler_backend_fields(self) -> None:
        for attr, _env, legacy_default in _DB_FREE_SELECTOR_SPECS:
            raw_value = getattr(self, attr)
            value = None if raw_value is None else str(raw_value).strip().lower()
            if self.scheduler_db_free_required:
                object.__setattr__(self, attr, value)
                continue
            normalized = value or legacy_default
            if attr == "scheduler_lock_backend" and normalized not in {"file", "postgres"}:
                raise ValueError("production scheduler scheduler_lock_backend must be 'file' or 'postgres'")
            object.__setattr__(self, attr, normalized)
        for attr, _env, _kind in _DB_FREE_PATH_SPECS:
            raw_value = getattr(self, attr)
            value = None if raw_value is None else str(raw_value).strip()
            object.__setattr__(self, attr, value)

    def db_free_runtime_evidence(self) -> dict[str, Any]:
        selectors = {
            env: {
                "configured": getattr(self, attr) is not None,
                "selected": _db_free_selector_evidence_scalar(getattr(self, attr)),
                "required_value": "file",
            }
            for attr, env, _legacy_default in _DB_FREE_SELECTOR_SPECS
        }
        paths = {
            env: {
                "configured": getattr(self, attr) not in (None, ""),
                "path": _db_free_path_evidence_scalar(getattr(self, attr)),
                "kind": kind,
            }
            for attr, env, kind in _DB_FREE_PATH_SPECS
        }
        return {
            "required": self.scheduler_db_free_required,
            "required_env": _DB_FREE_REQUIRED_ENV,
            "database_url_configured": bool(self.database_url_configured),
            "selectors": selectors,
            "paths": paths,
            "canonical_selector_fields": [env for _attr, env, _default in _DB_FREE_SELECTOR_SPECS],
            "canonical_path_fields": [env for _attr, env, _kind in _DB_FREE_PATH_SPECS],
        }

    def db_free_runtime_preflight(self) -> dict[str, Any]:
        if not self.scheduler_db_free_required:
            return {
                "status": "not_required",
                "required": False,
                "blockers": [],
                "checks": {},
            }
        checks: dict[str, Any] = {}
        blockers: list[dict[str, Any]] = []
        checks["database_url"] = {
            "env": "DATABASE_URL",
            "configured": bool(self.database_url_configured),
            "value_recorded": False,
        }
        if self.database_url_configured:
            blockers.append(
                {
                    "code": "database_url_forbidden",
                    "field": "DATABASE_URL",
                    "reason": "database_url_forbidden",
                    "message": "DB-free scheduler mode forbids scheduler DATABASE_URL before lock acquisition.",
                }
            )
        for attr, env, _legacy_default in _DB_FREE_SELECTOR_SPECS:
            value = getattr(self, attr)
            check, blocker = _db_free_selector_check(env, value)
            checks[env] = check
            if blocker is not None:
                blockers.append(blocker)
        allowed_roots = _db_free_allowed_roots(self)
        for attr, env, kind in _DB_FREE_PATH_SPECS:
            value = getattr(self, attr)
            check, blocker = _db_free_path_check(env, value, kind=kind, allowed_roots=allowed_roots)
            checks[env] = check
            if blocker is not None:
                blockers.append(blocker)
        return {
            "status": "blocked" if blockers else "ready",
            "required": True,
            "blockers": blockers,
            "checks": checks,
            "evidence": self.db_free_runtime_evidence(),
        }


def _evidence_scalar(value: Any) -> Any:
    if value in (None, ""):
        return None
    return redact_payload(str(value))


def _expanduser_for_mode(value: Path | str, *, db_free_required: bool) -> Path:
    path = Path(value)
    try:
        return path.expanduser()
    except RuntimeError:
        if not db_free_required:
            raise
        return path


def _raw_config_path_preserve_components(value: Path | str, *, db_free_required: bool = False) -> Path:
    path = _expanduser_for_mode(value, db_free_required=db_free_required)
    if not path.is_absolute():
        return Path.cwd() / path
    return path


def _raw_config_path_relative_to_preserve_components(
    value: Path | str,
    base: Path,
    *,
    db_free_required: bool = False,
) -> Path:
    path = _expanduser_for_mode(value, db_free_required=db_free_required)
    if not path.is_absolute():
        return base / path
    return path


def _optional_raw_config_path_relative_to_preserve_components(
    value: Path | str | None,
    base: Path,
    *,
    db_free_required: bool = False,
) -> Path | None:
    if value in (None, ""):
        return None
    return _raw_config_path_relative_to_preserve_components(
        value,
        base,
        db_free_required=db_free_required,
    )


def _config_path_preserve_final_component_for_mode(value: Path | str, *, db_free_required: bool) -> Path:
    if not db_free_required:
        return _scheduler._config_path_preserve_final_component(value)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _safe_preserve_final_component(path)


def _config_path_relative_to_preserve_final_for_mode(
    value: Path | str,
    base: Path,
    *,
    db_free_required: bool,
) -> Path:
    if not db_free_required:
        return _scheduler._config_path_relative_to_preserve_final(value, base)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = base / path
    return _safe_preserve_final_component(path)


def _optional_config_path_relative_to_preserve_final_for_mode(
    value: Path | str | None,
    base: Path,
    *,
    db_free_required: bool,
) -> Path | None:
    if value in (None, ""):
        return None
    return _config_path_relative_to_preserve_final_for_mode(
        value,
        base,
        db_free_required=db_free_required,
    )


def _safe_preserve_final_component(path: Path) -> Path:
    try:
        return path.parent.resolve(strict=False) / path.name
    except (OSError, RuntimeError):
        return path


def _resolve_config_path_for_mode(path: Path, *, db_free_required: bool) -> Path:
    if not db_free_required:
        return path.resolve()
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def _resolve_optional_config_path_for_mode(value: Path | None, *, db_free_required: bool) -> Path | None:
    if value is None:
        return None
    return _resolve_config_path_for_mode(value, db_free_required=db_free_required)


def _optional_config_path_for_mode(value: Path | str | None, *, db_free_required: bool) -> Path | None:
    if value in (None, ""):
        return None
    if not db_free_required:
        return _scheduler._optional_config_path(value)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _resolve_config_path_for_mode(path, db_free_required=True)


def _confined_path_for_mode(
    value: Path | str,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> Path:
    if not db_free_required:
        return _scheduler._confined_path(value, workspace_root, field_name)
    try:
        return _scheduler._confined_path(value, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        path = _expanduser_for_mode(value, db_free_required=True)
        if not path.is_absolute():
            path = workspace_root / path
        return _safe_preserve_final_component(path)


def _require_under_workspace_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> None:
    try:
        _scheduler._require_under_workspace(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        if not db_free_required:
            raise


def _require_safe_directory_final_component_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> None:
    try:
        _scheduler._require_safe_directory_final_component(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        if not db_free_required:
            raise


def _db_free_path_evidence_scalar(value: Any) -> Any:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = _db_free_urlparse(text)
    except ValueError:
        return "[invalid-uri]"
    if parsed.scheme:
        return "[object-uri]" if parsed.scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES else "[uri]"
    return "[local-path]"


def _db_free_selector_check(env: str, value: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    configured = value is not None
    selected = _db_free_selector_evidence_scalar(value)
    normalized_value = None if value in (None, "") else str(value)
    check = {
        "env": env,
        "configured": configured,
        "selected": selected,
        "required_value": "file",
        "file_selected": normalized_value == "file",
    }
    if value is None:
        return check, _db_free_blocker("db_free_selector_missing", env, "missing")
    if value == "":
        return check, _db_free_blocker("db_free_selector_blank", env, "blank")
    if _db_free_selector_text_is_db_like(value):
        return check, _db_free_blocker("db_free_selector_db_backed", env, "db_backed")
    if value != "file":
        return check, _db_free_blocker("db_free_selector_non_file", env, "non_file")
    return check, None


def _db_free_selector_evidence_scalar(value: Any) -> Any:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text == "file":
        return "file"
    try:
        parsed = _db_free_urlparse(text)
    except ValueError:
        return "[invalid-uri]"
    if parsed.scheme:
        scheme = _db_free_scheme_for_evidence(parsed.scheme)
        return scheme if scheme == "[db-like]" else "[uri]"
    if _db_free_selector_text_is_db_like(text):
        return "[db-like]"
    return "[non-file]"


def _db_free_selector_text_is_db_like(value: Any) -> bool:
    if value in (None, ""):
        return False
    text = str(value).strip().lower()
    return text in _DB_FREE_DB_BACKEND_VALUES or "postgres" in text or "psycopg" in text


def _db_free_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        try:
            root = _expanduser_for_mode(value, db_free_required=True).resolve(strict=False)
        except (OSError, RuntimeError):
            root = _expanduser_for_mode(value, db_free_required=True)
            if not root.is_absolute():
                root = Path.cwd() / root
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _db_free_path_check(
    env: str,
    value: str | Path | None,
    *,
    kind: str,
    allowed_roots: tuple[Path, ...],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    text = None if value is None else str(value).strip()
    check: dict[str, Any] = {
        "env": env,
        "configured": text not in (None, ""),
        "kind": kind,
    }
    if text is None:
        check["path"] = None
        return check, _db_free_blocker("db_free_required_path_missing", env, "missing")
    if text == "":
        check["path"] = None
        return check, _db_free_blocker("db_free_required_path_blank", env, "blank")
    try:
        parsed = _db_free_urlparse(text)
    except ValueError:
        check.update({"path": "[invalid-uri]", "uri": True, "object_uri": False, "scheme": "[invalid]"})
        return check, _db_free_blocker("db_free_required_path_malformed_uri", env, "malformed_uri")
    if parsed.scheme:
        check.update(_db_free_uri_evidence(parsed))
        if parsed.scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES and kind == "file":
            object_check, blocker = _db_free_object_uri_check(env, text, parsed)
            check.update(object_check)
            return check, blocker
        check["supported_object_uri"] = False
        return check, _db_free_blocker("db_free_required_path_unsupported_uri", env, "unsupported_uri")
    check["path"] = "[local-path]"
    path = _expanduser_for_mode(text, db_free_required=True)
    if not path.is_absolute():
        check.update({"absolute": False, "contained": False})
        return check, _db_free_blocker("db_free_required_path_relative", env, "relative", path=str(path))
    unsafe_component_reason = _db_free_local_path_component_reason(path)
    if unsafe_component_reason is not None:
        check.update({"absolute": True, "contained": False})
        return check, _db_free_blocker(
            "db_free_required_path_unsafe",
            env,
            unsafe_component_reason,
            path=str(path),
        )
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        check.update({"absolute": True, "contained": False})
        return check, _db_free_blocker(
            "db_free_required_path_unsafe",
            env,
            "unsafe",
            path=str(path),
            error_type=type(error).__name__,
        )
    contained = any(_path_is_relative_to(resolved, root) for root in allowed_roots)
    check.update({"absolute": True, "resolved_path": "[local-path]", "contained": contained})
    if not contained:
        return check, _db_free_blocker(
            "db_free_required_path_outside_boundary",
            env,
            "outside_boundary",
            path=str(resolved),
        )
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError:
        return check, _db_free_blocker("db_free_required_path_parent_missing", env, "parent_missing", path=str(parent))
    except OSError as error:
        return check, _db_free_blocker(
            "db_free_required_path_unsafe",
            env,
            "unsafe",
            path=str(parent),
            error_type=type(error).__name__,
        )
    if not parent_stat.st_mode:
        return check, _db_free_blocker("db_free_required_path_unsafe", env, "unsafe", path=str(parent))
    if parent.is_symlink() or not parent.is_dir():
        return check, _db_free_blocker("db_free_required_path_unsafe", env, "unsafe", path=str(parent))
    exists = path.exists()
    check["exists"] = exists
    if kind == "directory":
        if not exists:
            return check, _db_free_blocker("db_free_required_path_not_found", env, "not_found", path=str(resolved))
        if path.is_symlink() or not path.is_dir():
            return check, _db_free_blocker("db_free_required_path_unsafe", env, "unsafe", path=str(resolved))
        if not _scheduler._directory_is_writable(path):
            return check, _db_free_blocker(
                "db_free_required_path_not_writable",
                env,
                "not_writable",
                path=str(resolved),
            )
        check["writable"] = True
        return check, None
    if not exists:
        return check, _db_free_blocker("db_free_required_path_not_found", env, "not_found", path=str(resolved))
    if path.is_symlink() or not path.is_file():
        return check, _db_free_blocker("db_free_required_path_unsafe", env, "unsafe", path=str(resolved))
    if not _db_free_file_is_readable(path):
        return check, _db_free_blocker(
            "db_free_required_path_not_readable",
            env,
            "not_readable",
            path=str(resolved),
        )
    return check, None


def _db_free_file_is_readable(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    if path_stat.st_mode & 0o444 == 0:
        return False
    return os.access(path, os.R_OK)


def _db_free_local_path_component_reason(path: Path) -> str | None:
    for part in path.parts:
        if part in {"", ".", ".."}:
            return "traversal"
        lower = part.lower()
        if any(word in lower for word in _DB_FREE_CREDENTIAL_WORDS):
            return "credential_component"
    return None


def _db_free_uri_evidence(parsed: Any) -> dict[str, Any]:
    scheme = str(parsed.scheme or "").lower()
    return {
        "path": "[object-uri]" if scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES else "[uri]",
        "uri": True,
        "object_uri": scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES,
        "scheme": _db_free_scheme_for_evidence(scheme),
    }


def _db_free_urlparse(value: str) -> Any:
    try:
        return urlparse(value)
    except ValueError:
        if ":" in value or value.startswith("//"):
            raise
        return urlparse("")


def _db_free_scheme_for_evidence(scheme: str) -> str:
    normalized = scheme.lower()
    if normalized in _DB_FREE_DB_BACKEND_VALUES or "postgres" in normalized or "psycopg" in normalized:
        return "[db-like]"
    return normalized


def _db_free_object_uri_check(env: str, raw_uri: str, parsed: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    scheme = str(parsed.scheme or "").lower()
    check: dict[str, Any] = {
        "object_uri": True,
        "supported_object_uri": False,
        "path": "[object-uri]",
        "scheme": scheme,
    }
    unsafe_reason = _db_free_common_object_uri_unsafe_reason(raw_uri, parsed)
    if unsafe_reason is not None:
        return check, _db_free_blocker("db_free_required_path_unsafe_uri", env, unsafe_reason)
    try:
        if scheme == "s3":
            boundary = _db_free_s3_uri_boundary(raw_uri, parsed)
        elif scheme == "published":
            boundary = _db_free_published_uri_boundary(parsed)
        else:
            return check, _db_free_blocker("db_free_required_path_unsupported_uri", env, "unsupported_uri")
    except ValueError as error:
        return check, _db_free_blocker("db_free_required_path_unsafe_uri", env, str(error))
    check.update(boundary)
    check["supported_object_uri"] = True
    return check, None


def _db_free_common_object_uri_unsafe_reason(raw_uri: str, parsed: Any) -> str | None:
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_uri):
        return "control_character"
    try:
        _ = parsed.port
    except ValueError:
        return "malformed_port"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "credentials_query_or_fragment"
    return None


def _db_free_s3_uri_boundary(raw_uri: str, parsed: Any) -> dict[str, Any]:
    bucket = str(parsed.netloc or "")
    if not bucket:
        raise ValueError("missing_bucket")
    key = _db_free_safe_object_key(str(parsed.path or "").lstrip("/"))
    prefix = os.getenv(_DB_FREE_OBJECT_STORE_PREFIX_ENV, "").strip().rstrip("/")
    try:
        prefix_parsed = _db_free_urlparse(prefix) if prefix else None
    except ValueError as error:
        raise ValueError("object_uri_not_allowlisted") from error
    if prefix_parsed is None or prefix_parsed.scheme.lower() != "s3":
        raise ValueError("object_uri_not_allowlisted")
    if _db_free_common_object_uri_unsafe_reason(prefix, prefix_parsed) is not None or not prefix_parsed.netloc:
        raise ValueError("object_uri_not_allowlisted")
    allowed_bucket = str(prefix_parsed.netloc)
    if bucket != allowed_bucket:
        raise ValueError("object_uri_not_allowlisted")
    allowed_prefix = str(prefix_parsed.path or "").lstrip("/")
    if allowed_prefix:
        normalized_prefix = _db_free_safe_object_key(allowed_prefix)
        if key != normalized_prefix and not key.startswith(f"{normalized_prefix}/"):
            raise ValueError("object_uri_not_allowlisted")
    elif key.split("/", maxsplit=1)[0] not in _DB_FREE_PUBLIC_OBJECT_PREFIXES:
        raise ValueError("object_uri_not_allowlisted")
    return {
        "object_boundary": "s3",
        "bucket": "[object-bucket]",
        "namespace": "[object-prefix]",
    }


def _db_free_published_uri_boundary(parsed: Any) -> dict[str, Any]:
    namespace = f"{parsed.netloc}/{str(parsed.path or '').lstrip('/')}" if parsed.netloc else str(parsed.path or "")
    key = _db_free_safe_object_key(namespace.strip("/"))
    prefix = key.split("/", maxsplit=1)[0]
    if prefix not in _DB_FREE_PUBLIC_OBJECT_PREFIXES:
        raise ValueError("object_uri_not_allowlisted")
    return {
        "object_boundary": "published",
        "namespace": "[object-prefix]",
    }


def _db_free_safe_object_key(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or _DB_FREE_ENCODED_FORBIDDEN_RE.search(raw_path):
        raise ValueError("unsafe_object_path")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ValueError("unsafe_object_path")
    parts = PurePosixPath(decoded).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe_object_path")
    for part in parts:
        lower = part.lower()
        if any(word in lower for word in _DB_FREE_CREDENTIAL_WORDS):
            raise ValueError("unsafe_object_path")
        if not _DB_FREE_SAFE_OBJECT_SEGMENT_RE.fullmatch(part):
            raise ValueError("unsafe_object_path")
    return "/".join(parts)


def _db_free_blocker(
    code: str,
    field: str,
    reason: str,
    *,
    path: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    blocker = {
        "code": code,
        "field": field,
        "reason": reason,
        "message": f"DB-free scheduler runtime field {field} is not a safe all-file configuration.",
    }
    if path is not None:
        blocker["path"] = _db_free_blocker_path_evidence(path)
    if error_type is not None:
        blocker["error_type"] = error_type
    return blocker


def _db_free_blocker_path_evidence(path: str) -> str:
    try:
        parsed = _db_free_urlparse(str(path))
    except ValueError:
        return "[invalid-uri]"
    if parsed.scheme:
        return "[object-uri]" if parsed.scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES else "[uri]"
    return "[local-path]"


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
