from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from errno import EACCES, ELOOP, ENOTDIR, EPERM
from pathlib import Path
from typing import Any

from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler as _scheduler


def _scheduler_lock_evidence_root_preflight(config: Any) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler._scheduler_root_preflight_not_required(config)
    evidence_safe_paths = bool(
        getattr(config, "db_free_required", False)
        or getattr(config, "repair_missing_forcing", False)
    )
    allowed_roots = _scheduler._scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler._scheduler_allowed_roots_policy_check(
        config,
        allowed_roots,
        evidence_safe_paths=evidence_safe_paths,
    )
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    workspace_root_preflight_path = config._workspace_root_preflight_path
    for field_name, path, raw_path in (
        (
            "workspace_root",
            config._workspace_root_preflight_path,
            getattr(config, "_workspace_root_raw_preflight_path", config._workspace_root_preflight_path),
        ),
        (
            "lock_root",
            config._lock_root_preflight_path,
            getattr(config, "_lock_root_raw_preflight_path", config._lock_root_preflight_path),
        ),
        (
            "evidence_root",
            config._evidence_root_preflight_path,
            getattr(config, "_evidence_root_raw_preflight_path", config._evidence_root_preflight_path),
        ),
    ):
        check, blocker = _scheduler._scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=True,
            allow_create=False,
            require_approved_root=enforce_approved_roots and field_name == "workspace_root",
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=workspace_root_preflight_path,
            evidence_safe_paths=evidence_safe_paths,
            raw_value=raw_path,
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    return _scheduler._scheduler_root_preflight_payload(
        config, checks, blockers, evidence_safe_paths=evidence_safe_paths
    )


def _scheduler_runtime_root_preflight(config: Any) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler._scheduler_root_preflight_not_required(config)
    evidence_safe_paths = bool(
        getattr(config, "db_free_required", False)
        or getattr(config, "repair_missing_forcing", False)
    )
    allowed_roots = _scheduler._scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler._scheduler_allowed_roots_policy_check(
        config,
        allowed_roots,
        evidence_safe_paths=evidence_safe_paths,
    )
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    workspace_root_preflight_path = config._workspace_root_preflight_path
    for field_name, path, raw_path in (
        (
            "workspace_root",
            config._workspace_root_preflight_path,
            getattr(config, "_workspace_root_raw_preflight_path", config._workspace_root_preflight_path),
        ),
        (
            "object_store_root",
            config._object_store_root_preflight_path,
            getattr(config, "_object_store_root_raw_preflight_path", config._object_store_root_preflight_path),
        ),
        (
            "published_artifact_root",
            config._published_artifact_root_preflight_path,
            getattr(
                config,
                "_published_artifact_root_raw_preflight_path",
                config._published_artifact_root_preflight_path,
            ),
        ),
        (
            "runtime_root",
            config._runtime_root_preflight_path,
            getattr(config, "_runtime_root_raw_preflight_path", config._runtime_root_preflight_path),
        ),
        (
            "temp_root",
            config._temp_root_preflight_path,
            getattr(config, "_temp_root_raw_preflight_path", config._temp_root_preflight_path),
        ),
        (
            "lock_root",
            config._lock_root_preflight_path,
            getattr(config, "_lock_root_raw_preflight_path", config._lock_root_preflight_path),
        ),
        (
            "evidence_root",
            config._evidence_root_preflight_path,
            getattr(config, "_evidence_root_raw_preflight_path", config._evidence_root_preflight_path),
        ),
    ):
        # The published artifact root is a control-node display mount. Compute
        # stages write to object_store_root; the local publish stage creates and
        # mirrors artifacts into this root after Slurm work completes.
        allow_publish_root_create = field_name == "published_artifact_root"
        check, blocker = _scheduler._scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=not allow_publish_root_create,
            allow_create=allow_publish_root_create,
            require_approved_root=enforce_approved_roots and field_name not in {"lock_root", "evidence_root"},
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=workspace_root_preflight_path,
            evidence_safe_paths=evidence_safe_paths,
            raw_value=raw_path,
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    service_role_check, service_role_blocker = _scheduler._scheduler_service_role_check(config.service_role)
    checks["service_role"] = service_role_check
    if service_role_blocker is not None:
        blockers.append(service_role_blocker)
    return _scheduler._scheduler_root_preflight_payload(
        config, checks, blockers, evidence_safe_paths=evidence_safe_paths
    )


def _scheduler_root_preflight_not_required(config: Any) -> dict[str, Any]:
    return {
        "status": "not_required",
        "required": False,
        "blockers": [],
        "checks": {},
        "allowed_roots": [str(root) for root in _scheduler._scheduler_allowed_roots(config)],
    }


def _scheduler_root_preflight_payload(
    config: Any,
    checks: Mapping[str, Any],
    blockers: Sequence[Mapping[str, Any]],
    *,
    evidence_safe_paths: bool = False,
) -> dict[str, Any]:
    return {
        "status": "blocked" if blockers else "ready",
        "required": True,
        "blockers": [dict(blocker) for blocker in blockers],
        "checks": dict(checks),
        "allowed_roots": (
            ["[local-path]" for _root in _scheduler._scheduler_allowed_roots(config)]
            if evidence_safe_paths
            else [str(root) for root in _scheduler._scheduler_allowed_roots(config)]
        ),
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
    evidence_safe_paths: bool = False,
    raw_value: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    evidence_path = "[local-path]" if evidence_safe_paths else None
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
            return check, _scheduler._scheduler_root_blocker(field_name, "MISSING", None)
        return check, None
    path = Path(value).expanduser()
    if not path.is_absolute():
        check = {
            "configured": True,
            "path": evidence_path or str(path),
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
        }
        return check, _scheduler._scheduler_root_blocker(field_name, "RELATIVE", evidence_path or str(path))
    if evidence_safe_paths:
        raw_path = path if raw_value in (None, "") else Path(raw_value).expanduser()
        unsafe_component_reason = _scheduler_root_path_component_reason(raw_path)
        if unsafe_component_reason is not None:
            check = {
                "configured": True,
                "path": evidence_path or str(path),
                "exists": False,
                "is_dir": False,
                "contained": False,
                "approved_root_required": require_approved_root,
                "writable": False,
            }
            return check, _scheduler._scheduler_root_blocker(
                field_name, unsafe_component_reason, evidence_path or str(path)
            )
    try:
        resolved = path.resolve(strict=False)
    except OSError as error:
        unsafe_reason = _scheduler._scheduler_root_os_error_reason(error)
        check = {
            "configured": True,
            "path": evidence_path or str(path),
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
            "unsafe_reason": unsafe_reason,
        }
        return check, _scheduler._scheduler_root_blocker(field_name, unsafe_reason, evidence_path or str(path))
    except RuntimeError:
        unsafe_reason = "UNSAFE_PATH"
        check = {
            "configured": True,
            "path": evidence_path or str(path),
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
            "unsafe_reason": unsafe_reason,
        }
        return check, _scheduler._scheduler_root_blocker(field_name, unsafe_reason, evidence_path or str(path))
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
            writable = _scheduler._directory_is_writable(path)
    except FileNotFoundError:
        exists = False
        if allow_create:
            parent = path.parent
            try:
                parent_stat = parent.lstat()
                parent_is_dir = stat.S_ISDIR(parent_stat.st_mode)
                parent_is_symlink = stat.S_ISLNK(parent_stat.st_mode)
                writable = parent_is_dir and not parent_is_symlink and _scheduler._directory_is_writable(parent)
            except FileNotFoundError:
                writable = False
            except OSError as error:
                unsafe_reason = _scheduler._scheduler_root_os_error_reason(error)
    except OSError as error:
        unsafe_reason = _scheduler._scheduler_root_os_error_reason(error)
    contained = _scheduler._path_is_under_any(resolved, allowed_roots) if require_approved_root else True
    under_workspace = True
    if require_under_workspace:
        if workspace_root is None:
            under_workspace = False
        else:
            try:
                workspace_anchor = Path(workspace_root).expanduser().resolve(strict=False)
                resolved.relative_to(workspace_anchor)
            except (OSError, RuntimeError, ValueError):
                under_workspace = False
    check = {
        "configured": True,
        "path": evidence_path or str(resolved),
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
        return check, _scheduler._scheduler_root_blocker(field_name, unsafe_reason, evidence_path or str(resolved))
    if require_under_workspace and not under_workspace:
        return check, _scheduler._scheduler_root_blocker(
            field_name, "OUT_OF_WORKSPACE", evidence_path or str(resolved)
        )
    if is_symlink:
        return check, _scheduler._scheduler_root_blocker(field_name, "SYMLINK", evidence_path or str(resolved))
    if require_approved_root and not contained:
        return check, _scheduler._scheduler_root_blocker(
            field_name, "OUT_OF_APPROVED_ROOT", evidence_path or str(resolved)
        )
    if must_exist and not exists:
        return check, _scheduler._scheduler_root_blocker(field_name, "NOT_FOUND", evidence_path or str(resolved))
    if exists and not is_dir:
        return check, _scheduler._scheduler_root_blocker(field_name, "NOT_DIRECTORY", evidence_path or str(resolved))
    if not writable:
        return check, _scheduler._scheduler_root_blocker(field_name, "NOT_WRITABLE", evidence_path or str(resolved))
    return check, None


def _scheduler_root_path_component_reason(path: Path) -> str | None:
    for part in path.parts:
        if part in {"", ".", ".."}:
            return "UNSAFE_PATH"
        lower = part.lower()
        if any(
            word in lower
            for word in (
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
        ):
            return "UNSAFE_PATH"
    return None


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
    config: Any,
    allowed_roots: Sequence[Path],
    *,
    evidence_safe_paths: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    configured_roots = tuple(root for root in config.allowed_storage_roots if root not in (None, ""))
    check = {
        "env": "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "configured": bool(configured_roots),
        "non_empty": bool(allowed_roots),
        "allowed_roots": (
            ["[local-path]" for _root in allowed_roots]
            if evidence_safe_paths
            else [str(root) for root in allowed_roots]
        ),
        "independent_policy_required": True,
    }
    if not allowed_roots:
        return check, _scheduler._scheduler_root_blocker("allowed_roots", "MISSING", None)
    return check, None


def _scheduler_allowed_roots(config: Any) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        try:
            root = Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            if not bool(getattr(config, "db_free_required", False)):
                raise
            root = Path(value).expanduser()
            if not root.is_absolute():
                root = Path.cwd() / root
        if root not in roots:
            roots.append(root)
    return tuple(roots)


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
    _scheduler._require_under_workspace(resolved_parent, workspace_root, field_name)
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
    return _scheduler._config_path_relative_to_preserve_final(value, base)


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


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _env_allowed_cycle_hours_utc(name: str, default: Sequence[int]) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None:
        return _scheduler._normalize_allowed_cycle_hours_utc(default)
    return _scheduler._parse_allowed_cycle_hours_utc(value, name)


def _parse_allowed_cycle_hours_utc(value: str, name: str = "allowed_cycle_hours_utc") -> tuple[int, ...]:
    if value == "":
        raise ValueError(f"{name} must contain at least one UTC cycle hour")
    parsed: list[int] = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if token == "":
            raise ValueError(f"{name} must not contain empty cycle hour tokens")
        try:
            hour = int(token)
        except ValueError as error:
            raise ValueError(f"{name} must contain integer UTC cycle hours") from error
        parsed.append(hour)
    return _scheduler._normalize_allowed_cycle_hours_utc(parsed, field_name=name)


def _normalize_allowed_cycle_hours_utc(
    value: Sequence[int],
    *,
    field_name: str = "allowed_cycle_hours_utc",
) -> tuple[int, ...]:
    hours: set[int] = set()
    try:
        raw_hours = iter(value)
    except TypeError as error:
        raise ValueError(f"production scheduler {field_name} must contain integer UTC cycle hours") from error
    for raw_hour in raw_hours:
        if isinstance(raw_hour, bool) or not isinstance(raw_hour, int):
            raise ValueError(f"production scheduler {field_name} must contain integer UTC cycle hours")
        hour = raw_hour
        if hour < 0 or hour > 23:
            raise ValueError(f"production scheduler {field_name} must only contain values in 0..23")
        hours.add(hour)
    if not hours:
        raise ValueError(f"production scheduler {field_name} must contain at least one UTC cycle hour")
    return tuple(sorted(hours))


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
    _scheduler._require_under_workspace(path.parent.resolve(), workspace_root, field_name)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"production scheduler {field_name} must be a safe directory") from error
    if stat.S_ISLNK(path_stat.st_mode):
        resolved = path.resolve(strict=False)
        _scheduler._require_under_workspace(resolved, workspace_root, field_name)
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"production scheduler {field_name} must be a directory")
        return
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")
