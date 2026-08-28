from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from packages.common.redaction import redact_payload
from services.orchestrator import scheduler as _scheduler
from services.orchestrator import source_cycle_raw_manifest
from services.orchestrator.scheduler_config.db_free import (
    _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
    _db_free_allowed_roots_and_blockers,
    _db_free_blocker,
    _db_free_path_check,
    _db_free_path_evidence_scalar,
    _db_free_path_identity,
    _db_free_raw_manifest_prefix_check,
    _db_free_selector_check,
    _db_free_selector_evidence_scalar,
)
from services.orchestrator.scheduler_config.path_modes import (
    _config_path_preserve_final_component_for_mode,
    _config_path_relative_to_preserve_final_for_mode,
    _confined_path_for_mode,
    _optional_config_path_for_mode,
    _optional_config_path_relative_to_preserve_final_for_mode,
    _optional_raw_config_path_relative_to_preserve_components,
    _raw_config_path_preserve_components,
    _raw_config_path_relative_to_preserve_components,
    _require_safe_directory_final_component_for_mode,
    _require_under_workspace_for_mode,
    _resolve_config_path_for_mode,
    _resolve_optional_config_path_for_mode,
)
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES

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

_DB_FREE_RAW_MANIFEST_ROOT_ENV = "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT"
_DB_FREE_CANONICAL_RAW_AUTHORITY_ENV = "NHMS_OBJECT_STORE_COPYBACK_ROOT"

def _repair_missing_forcing_cycle_time(value: datetime | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                "production scheduler repair_missing_forcing_cycle_time must be an ISO-8601 UTC time"
            ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "production scheduler repair_missing_forcing_cycle_time must include a UTC offset"
        )
    return parsed.astimezone(UTC)

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
    reconcile_slurm_user: str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_RECONCILE_SLURM_USER")
    )
    reconcile_slurm_account: str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT")
    )
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
    slurm_array_concurrency_bound: int = field(
        default_factory=lambda: _scheduler._env_int(
            "NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND",
            32,
        )
    )
    object_store_copyback_root: Path | str | None = field(
        default_factory=lambda: os.getenv(_DB_FREE_CANONICAL_RAW_AUTHORITY_ENV)
    )
    nfs_raw_manifest_root: Path | str | None = field(
        default_factory=lambda: os.getenv(_DB_FREE_RAW_MANIFEST_ROOT_ENV)
    )
    nfs_raw_manifest_prefix: str = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX")
        or "s3://nhms"
    )
    require_direct_grid: bool = field(
        default_factory=lambda: _scheduler._env_flag("NHMS_SCHEDULER_REQUIRE_DIRECT_GRID")
    )
    repair_missing_forcing: bool = field(
        default_factory=lambda: _scheduler._env_flag("NHMS_SCHEDULER_REPAIR_MISSING_FORCING")
    )
    repair_missing_forcing_cycle_time: datetime | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_REPAIR_MISSING_FORCING_CYCLE_TIME")
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
    restart_reconcile_absence_seconds: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS", 300)
    )
    # Consecutive identity-mismatch reconcile passes tolerated before a
    # reserved-unbound row is released to ``reservation_lost``. ``<= 0``
    # disables the exit and keeps today's (wedging) behaviour: a bad value must
    # never turn into "release immediately".
    identity_blocked_streak_limit: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT", 3)
    )
    # Consecutive fully-observed passes reporting the same (subject, reason)
    # before the observe-only no-progress circuit marks it open (#1118). ``<= 0``
    # disables the marker completely: no tracker file, no evidence key, no log.
    no_progress_circuit_passes: int = field(
        default_factory=lambda: _scheduler._env_int("NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES", 3)
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
    # Constructor-time ``object_store_root`` value, captured BEFORE
    # ``__post_init__`` normalizes it (issue #1616 / design D1). ``__post_init__``
    # collapses an explicitly blank value into ``None`` and anchors a relative
    # value beneath the workspace root; retention must see the raw value so a
    # blank/relative ``OBJECT_STORE_ROOT`` is rejected as a deletion surface
    # instead of resolving to a derived one. Private by contract: every
    # non-retention scheduler path keeps consuming the normalized
    # ``object_store_root`` field.
    _object_store_root_raw: Path | str | None = field(init=False, repr=False, compare=False)
    _published_artifact_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _runtime_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _temp_root_raw_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _lock_root_raw_preflight_path: Path = field(init=False, repr=False, compare=False)
    _evidence_root_raw_preflight_path: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Capture the constructor-time raw value BEFORE normalization below
        # (issue #1616 / design D1). This runs first because every later branch
        # reads/writes the normalized form; the raw value feeds retention only.
        object.__setattr__(self, "_object_store_root_raw", self.object_store_root)
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
        object.__setattr__(self, "reconcile_slurm_user", _normalized_optional_identity(self.reconcile_slurm_user))
        object.__setattr__(
            self,
            "reconcile_slurm_account",
            _normalized_optional_identity(self.reconcile_slurm_account),
        )
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
            "slurm_array_concurrency_bound",
            max(int(self.slurm_array_concurrency_bound), 1),
        )
        object.__setattr__(self, "require_direct_grid", bool(self.require_direct_grid))
        object.__setattr__(self, "repair_missing_forcing", bool(self.repair_missing_forcing))
        repair_cycle_time = _repair_missing_forcing_cycle_time(
            self.repair_missing_forcing_cycle_time,
        )
        object.__setattr__(self, "repair_missing_forcing_cycle_time", repair_cycle_time)
        if self.repair_missing_forcing:
            if repair_cycle_time is None:
                raise ValueError(
                    "production scheduler repair_missing_forcing requires an exact cycle time"
                )
            if self.continuous:
                raise ValueError(
                    "production scheduler repair_missing_forcing cannot run continuously"
                )
            if self.backfill_enabled or int(self.max_cycles_per_source) != 1 or int(self.lookback_hours) != 0:
                raise ValueError(
                    "production scheduler repair_missing_forcing requires an exact-cycle, "
                    "single-cycle, backfill-disabled invocation"
                )
            if int(self.slurm_array_concurrency_bound) > 32:
                raise ValueError(
                    "production scheduler repair_missing_forcing Slurm array concurrency must not exceed 32"
                )
        elif repair_cycle_time is not None:
            raise ValueError(
                "production scheduler repair_missing_forcing_cycle_time requires repair_missing_forcing"
            )
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
        absence_seconds = int(self.restart_reconcile_absence_seconds)
        if absence_seconds < 30 or absence_seconds > 3600:
            raise ValueError(
                "production scheduler restart_reconcile_absence_seconds must be between 30 and 3600"
            )
        object.__setattr__(self, "restart_reconcile_absence_seconds", absence_seconds)
        object.__setattr__(
            self,
            "identity_blocked_streak_limit",
            int(self.identity_blocked_streak_limit),
        )
        object.__setattr__(
            self,
            "no_progress_circuit_passes",
            int(self.no_progress_circuit_passes),
        )
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
            "accepted_submit_ownership": {
                "required": bool(self.scheduler_db_free_required and self.slurm_execution_enabled),
                "user_configured": self.reconcile_slurm_user is not None,
                "account_configured": self.reconcile_slurm_account is not None,
            },
            "canonical_selector_fields": [env for _attr, env, _default in _DB_FREE_SELECTOR_SPECS],
            "canonical_path_fields": [env for _attr, env, _kind in _DB_FREE_PATH_SPECS],
        }

    def db_free_runtime_preflight(self) -> dict[str, Any]:
        repair_authority_required = bool(self.repair_missing_forcing)
        if not self.scheduler_db_free_required and not repair_authority_required:
            return {
                "status": "not_required",
                "required": False,
                "blockers": [],
                "checks": {},
            }
        checks: dict[str, Any] = {}
        blockers: list[dict[str, Any]] = []
        if self.scheduler_db_free_required:
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
            if self.slurm_execution_enabled:
                for field_name, env_name in (
                    ("reconcile_slurm_user", "NHMS_SCHEDULER_RECONCILE_SLURM_USER"),
                    ("reconcile_slurm_account", "NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT"),
                ):
                    value = getattr(self, field_name)
                    checks[env_name] = {
                        "env": env_name,
                        "configured": value is not None,
                        "value_recorded": False,
                    }
                    if value is None:
                        blockers.append(
                            {
                                "code": "accepted_submit_owner_missing",
                                "field": env_name,
                                "reason": "accepted_submit_owner_missing",
                                "message": "DB-free Slurm execution requires exact accepted-submit ownership.",
                            }
                        )
            for attr, env, _legacy_default in _DB_FREE_SELECTOR_SPECS:
                value = getattr(self, attr)
                check, blocker = _db_free_selector_check(env, value)
                checks[env] = check
                if blocker is not None:
                    blockers.append(blocker)
        allowed_roots, allowed_roots_blockers = _db_free_allowed_roots_and_blockers(self)
        blockers.extend(allowed_roots_blockers)
        if self.scheduler_db_free_required:
            for attr, env, kind in _DB_FREE_PATH_SPECS:
                value = getattr(self, attr)
                check, blocker = _db_free_path_check(env, value, kind=kind, allowed_roots=allowed_roots)
                checks[env] = check
                if blocker is not None:
                    blockers.append(blocker)
        canonical_root_check, canonical_root_blocker = _db_free_path_check(
            _DB_FREE_CANONICAL_RAW_AUTHORITY_ENV,
            self.object_store_copyback_root,
            kind="readable_directory",
            allowed_roots=allowed_roots,
        )
        checks[_DB_FREE_CANONICAL_RAW_AUTHORITY_ENV] = canonical_root_check
        if canonical_root_blocker is not None:
            blockers.append(canonical_root_blocker)
        raw_root_check, raw_root_blocker = _db_free_path_check(
            _DB_FREE_RAW_MANIFEST_ROOT_ENV,
            self.nfs_raw_manifest_root,
            kind="readable_directory",
            allowed_roots=allowed_roots,
        )
        checks[_DB_FREE_RAW_MANIFEST_ROOT_ENV] = raw_root_check
        if raw_root_blocker is not None:
            blockers.append(raw_root_blocker)
        topology_identity = _db_free_path_identity(
            source_cycle_raw_manifest.NODE22_CANONICAL_NFS_RAW_AUTHORITY_ROOT
        )
        canonical_topology_matches: bool | None = None
        if canonical_root_blocker is None:
            canonical_topology_matches = (
                _db_free_path_identity(self.object_store_copyback_root) == topology_identity
            )
            if not canonical_topology_matches:
                blockers.append(
                    _db_free_blocker(
                        "db_free_raw_authority_topology_mismatch",
                        _DB_FREE_CANONICAL_RAW_AUTHORITY_ENV,
                        "canonical_topology_mismatch",
                    )
                )
        canonical_root_check["topology_matches"] = canonical_topology_matches
        raw_topology_matches: bool | None = None
        if raw_root_blocker is None:
            raw_topology_matches = _db_free_path_identity(self.nfs_raw_manifest_root) == topology_identity
            if not raw_topology_matches:
                blockers.append(
                    _db_free_blocker(
                        "db_free_raw_authority_topology_mismatch",
                        _DB_FREE_RAW_MANIFEST_ROOT_ENV,
                        "canonical_topology_mismatch",
                    )
                )
        raw_root_check["topology_matches"] = raw_topology_matches
        authority_matches: bool | None = None
        if canonical_root_blocker is None and raw_root_blocker is None:
            authority_matches = _db_free_path_identity(
                self.object_store_copyback_root
            ) == _db_free_path_identity(self.nfs_raw_manifest_root)
            if not authority_matches:
                blockers.append(
                    _db_free_blocker(
                        "db_free_raw_authority_mismatch",
                        _DB_FREE_RAW_MANIFEST_ROOT_ENV,
                        "canonical_authority_mismatch",
                    )
                )
        raw_root_check["canonical_authority_configured"] = (
            self.object_store_copyback_root not in (None, "")
        )
        raw_root_check["authority_matches"] = authority_matches
        prefix_check, prefix_blocker = _db_free_raw_manifest_prefix_check(
            self.nfs_raw_manifest_prefix,
            require_canonical=repair_authority_required,
        )
        checks[_DB_FREE_RAW_MANIFEST_PREFIX_ENV] = prefix_check
        if prefix_blocker is not None:
            blockers.append(prefix_blocker)
        return {
            "status": "blocked" if blockers else "ready",
            "required": True,
            "blockers": blockers,
            "checks": checks,
            "evidence": self.db_free_runtime_evidence(),
        }

def _normalized_optional_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

def _evidence_scalar(value: Any) -> Any:
    if value in (None, ""):
        return None
    return redact_payload(str(value))
