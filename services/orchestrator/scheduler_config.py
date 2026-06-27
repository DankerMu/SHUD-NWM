from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

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
        database_url_raw = None if self.database_url is None else str(self.database_url)
        database_url = database_url_raw.strip() if database_url_raw and database_url_raw.strip() else None
        object.__setattr__(self, "database_url", database_url)
        object.__setattr__(self, "database_url_configured", bool(self.database_url_configured or database_url_raw))
        object.__setattr__(self, "scheduler_db_free_required", bool(self.scheduler_db_free_required))
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
                "selected": _evidence_scalar(getattr(self, attr)),
                "required_value": "file",
            }
            for attr, env, _legacy_default in _DB_FREE_SELECTOR_SPECS
        }
        paths = {
            env: {
                "configured": getattr(self, attr) not in (None, ""),
                "path": _evidence_scalar(getattr(self, attr)),
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


def _db_free_selector_check(env: str, value: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    configured = value is not None
    selected = None if value in (None, "") else str(value)
    check = {
        "env": env,
        "configured": configured,
        "selected": selected,
        "required_value": "file",
        "file_selected": selected == "file",
    }
    if value is None:
        return check, _db_free_blocker("db_free_selector_missing", env, "missing")
    if value == "":
        return check, _db_free_blocker("db_free_selector_blank", env, "blank")
    if value in _DB_FREE_DB_BACKEND_VALUES:
        return check, _db_free_blocker("db_free_selector_db_backed", env, "db_backed")
    if value != "file":
        return check, _db_free_blocker("db_free_selector_non_file", env, "non_file")
    return check, None


def _db_free_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in (
        *config.allowed_storage_roots,
        config.workspace_root,
        config.object_store_root,
        config.published_artifact_root,
        config.runtime_root,
        config.temp_root,
    ):
        if value in (None, ""):
            continue
        root = Path(value).expanduser().resolve(strict=False)
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
        "path": _evidence_scalar(text),
        "kind": kind,
    }
    if text is None:
        return check, _db_free_blocker("db_free_required_path_missing", env, "missing")
    if text == "":
        return check, _db_free_blocker("db_free_required_path_blank", env, "blank")
    parsed = urlparse(text)
    if parsed.scheme:
        if parsed.scheme in _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES and kind == "file":
            check["object_uri"] = True
            check["supported_object_uri"] = True
            return check, None
        check["object_uri"] = True
        check["supported_object_uri"] = False
        return check, _db_free_blocker("db_free_required_path_unsupported_uri", env, "unsupported_uri")
    path = Path(text).expanduser()
    if not path.is_absolute():
        check.update({"absolute": False, "contained": False})
        return check, _db_free_blocker("db_free_required_path_relative", env, "relative", path=str(path))
    resolved = path.resolve(strict=False)
    contained = any(_path_is_relative_to(resolved, root) for root in allowed_roots)
    check.update({"absolute": True, "resolved_path": str(resolved), "contained": contained})
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
    return check, None


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
        blocker["path"] = redact_payload(path)
    if error_type is not None:
        blocker["error_type"] = error_type
    return blocker


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
