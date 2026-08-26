from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from errno import EACCES, ELOOP, ENOENT, ENOTDIR, EPERM
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
    allowed_roots, allowed_roots_unsafe_blockers = _scheduler._scheduler_allowed_roots_and_blockers(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler._scheduler_allowed_roots_policy_check(
        config,
        allowed_roots,
        evidence_safe_paths=evidence_safe_paths,
    )
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    # Cause before consequence: "this root was dropped" precedes "no root left".
    blockers: list[dict[str, Any]] = list(allowed_roots_unsafe_blockers)
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
        config,
        checks,
        blockers,
        evidence_safe_paths=evidence_safe_paths,
        allowed_roots=allowed_roots,
    )


def _scheduler_runtime_root_preflight(config: Any) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler._scheduler_root_preflight_not_required(config)
    evidence_safe_paths = bool(
        getattr(config, "db_free_required", False)
        or getattr(config, "repair_missing_forcing", False)
    )
    allowed_roots, allowed_roots_unsafe_blockers = _scheduler._scheduler_allowed_roots_and_blockers(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler._scheduler_allowed_roots_policy_check(
        config,
        allowed_roots,
        evidence_safe_paths=evidence_safe_paths,
    )
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    # Cause before consequence: "this root was dropped" precedes "no root left".
    blockers: list[dict[str, Any]] = list(allowed_roots_unsafe_blockers)
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
        config,
        checks,
        blockers,
        evidence_safe_paths=evidence_safe_paths,
        allowed_roots=allowed_roots,
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
    allowed_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Render one preflight arm's verdict.

    ``allowed_roots`` must be the very roots the arm adjudicated: re-deriving
    them here would be a second adjudication whose product can diverge from the
    first under filesystem races, yielding a self-contradictory payload (a
    blocker saying a root was dropped next to a top-level ``allowed_roots`` that
    still lists it). ``None`` keeps the historical self-derivation for callers
    that have no adjudication of their own.
    """

    effective_roots = _scheduler._scheduler_allowed_roots(config) if allowed_roots is None else allowed_roots
    return {
        "status": "blocked" if blockers else "ready",
        "required": True,
        "blockers": [dict(blocker) for blocker in blockers],
        "checks": dict(checks),
        "allowed_roots": (
            ["[local-path]" for _root in effective_roots]
            if evidence_safe_paths
            else [str(root) for root in effective_roots]
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
    """Effective approved containment bases (read-only view of the adjudication).

    The verdict itself lives in ``_scheduler_allowed_roots_and_blockers``; this
    signature is kept for the payload/not-required evidence surfaces, which show
    the same product without owning a blocker channel.
    """

    return _scheduler._scheduler_allowed_roots_and_blockers(config)[0]


def _scheduler_allowed_roots_and_blockers(config: Any) -> tuple[tuple[Path, ...], list[dict[str, Any]]]:
    """Adjudicate the configured allowed storage roots and report unsafe ones.

    Strict resolution + errno split, the same paradigm as
    ``_preflight_allowed_roots``: a symlink loop no longer raises from
    non-strict resolution on CPython 3.13+, and strict ``Path.resolve()`` raises
    an errno-less ``RuntimeError`` on <=3.12, so the verdict has to come from
    the kernel errno of ``os.path.realpath(..., strict=True)``. ENOENT is NOT
    unsafe: a merely missing root (not created yet, NFS not mounted) keeps the
    historical "admitted" semantics on both runtime modes and never produces a
    blocker. Any other errno is tolerated lexically on db-free runtimes (PR #831)
    and, on database-backed runtimes, drops the root from the effective allowed
    roots so it can never serve as a phantom containment base, with a structured
    ``SCHEDULER_ROOT_ALLOWED_ROOTS_<REASON>`` blocker explaining why.
    """

    db_free_required = bool(getattr(config, "db_free_required", False))
    evidence_safe_paths = bool(db_free_required or getattr(config, "repair_missing_forcing", False))
    roots: list[Path] = []
    blockers: list[dict[str, Any]] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        expanded = Path(value).expanduser()
        try:
            root = Path(os.path.realpath(expanded, strict=True))
        except OSError as error:
            if getattr(error, "errno", None) == ENOENT:
                # Non-strict os.path.realpath() never raises on 3.11-3.14 and
                # reproduces the historical non-strict Path.resolve() product,
                # including `<missing>/../<loop>` shapes.
                root = Path(os.path.realpath(expanded))
            elif db_free_required:
                # PR #831 lexical-fallback tolerance, kept verbatim: on db-free
                # runtimes an unresolvable root is legitimate configuration.
                root = expanded
                if not root.is_absolute():
                    root = Path.cwd() / root
            else:
                blockers.append(
                    _scheduler._scheduler_root_blocker(
                        "allowed_roots",
                        _scheduler._scheduler_root_os_error_reason(error),
                        "[local-path]" if evidence_safe_paths else str(expanded),
                    )
                )
                continue
        if root not in roots:
            roots.append(root)
    return tuple(roots), blockers


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


def _canonical_parent(path: Path) -> Path:
    """Canonicalise a path's parent segment on every supported CPython.

    Same paradigm as _optional_config_path below: strict os.path.realpath, one
    non-strict os.path.realpath fallback for every strict failure, and no
    verdict of its own -- the caller's containment check or the storage
    preflight classifies what comes back.  Path.resolve() cannot be used here
    because a symlink loop in the parent segment makes it raise an errno-less
    RuntimeError up to 3.12 (strict=False does not help; GH-113838 lands in
    3.13), aborting configuration construction on every production interpreter
    while 3.13+ walks on to a structured verdict.  The non-strict fallback
    reproduces the old Path.resolve() product verbatim -- POSIX order, symlinks
    first and `..` afterwards -- so loop-free and ENOENT inputs are unchanged
    (design D1/D2 of #1423).
    """

    parent = path.parent
    try:
        return Path(os.path.realpath(parent, strict=True))
    except OSError:
        return Path(os.path.realpath(parent))


def _canonical_path(path: Path) -> Path:
    """Canonicalise a whole path on every supported CPython.

    Same paradigm as _canonical_parent above, applied to the full path instead
    of its parent segment: strict os.path.realpath, one non-strict
    os.path.realpath fallback for every strict failure, and no verdict of its
    own.  Path.resolve() cannot be used because a symlink loop makes it raise an
    errno-less RuntimeError up to 3.12 while 3.13+ silently adopts the loop as
    the result (GH-113838), so the two interpreters disagree on the same input.
    The non-strict fallback reproduces the old Path.resolve() product verbatim
    -- POSIX order, symlinks first and `..` afterwards -- so loop-free and
    ENOENT inputs are unchanged (#1546).
    """

    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError:
        return Path(os.path.realpath(path))


def _expanduser_or_verbatim(value: Path | str) -> Path:
    """Expand a leading ``~``, keeping the raw value when no home can be determined.

    Path.expanduser() throws a bare, errno-less RuntimeError for
    ``~<unknown user>`` (and for ``~`` with no passwd entry and HOME unset).
    Config construction must not abort on that: classification of an unusable
    root belongs to the storage preflight, and an abort here produces no
    structured blocker at all.  Swallowing the throw and handing the raw value
    on to the caller's own anchoring is exactly what the database-free arm does
    in scheduler_config._expanduser_for_mode(..., db_free_required=True), so the
    two database arms come out byte-identical on such input (#1549).

    This is deliberately the opposite direction from packages/common/safe_fs.py,
    which narrows the same bare throw into a structured refusal: that module is
    a write-side primitive where a mis-anchored path is a filesystem side
    effect, while here the value is only carried down to a classifier.
    """

    path = Path(value)
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _confined_path(value: Path | str, workspace_root: Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    resolved_parent = _canonical_parent(path)
    candidate = resolved_parent / path.name
    _scheduler._require_under_workspace(resolved_parent, workspace_root, field_name)
    return candidate


def _reject_blank_config_path(value: Path | str | None, field_name: str) -> None:
    if isinstance(value, str) and value.strip() == "":
        raise ValueError(f"production scheduler {field_name} must not be blank")


def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    # A tilde whose home cannot be determined is kept verbatim rather than
    # aborting construction, for the same reason the strict failure below is
    # handed on: classification belongs to the preflight (#1549).
    expanded = _expanduser_or_verbatim(value)
    try:
        return Path(os.path.realpath(expanded, strict=True))
    except OSError:
        # Classification belongs to the storage preflight, not to config
        # construction: hand the canonicalised value down so
        # _preflight_allowed_roots drops an unresolvable root and reports
        # SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH on every supported
        # CPython, instead of aborting the whole process on <=3.12.
        #
        # A single non-strict os.path.realpath() covers every strict failure:
        # it never raises on 3.11-3.14 and reproduces the product of the old
        # non-strict Path.resolve() verbatim -- POSIX order, symlinks first and
        # `..` afterwards. Splitting on errno would buy nothing, because both
        # would-be lanes converge on this same product; a lexical pass-through
        # instead re-opens a 3.13+ vs <=3.12 divergence on `<file>/../<dir>`
        # shapes, and normpath folding would erase symlink redirection and
        # fabricate a root the operator never approved (design D2).
        return Path(os.path.realpath(expanded))


def _config_path_preserve_final_component(value: Path | str) -> Path:
    path = _expanduser_or_verbatim(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _canonical_parent(path) / path.name


def _config_path_relative_to_preserve_final(value: Path | str, base: Path) -> Path:
    path = _expanduser_or_verbatim(value)
    if not path.is_absolute():
        path = base / path
    return _canonical_parent(path) / path.name


def _optional_config_path_relative_to_preserve_final(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    return _scheduler._config_path_relative_to_preserve_final(value, base)


def _resolve_optional_config_path(value: Path | None) -> Path | None:
    if value is None:
        return None
    return _canonical_path(value)


def _optional_config_path_relative_to(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    # Two families cross in this one helper: the bare expanduser is the #1549
    # lane (keep the value, do not throw) and the bare resolve is the #1546 lane
    # (canonicalise identically on every interpreter).
    path = _expanduser_or_verbatim(value)
    if not path.is_absolute():
        path = base / path
    return _canonical_path(path)


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


def _symlink_loop_refusal(field_name: str, path: Path, attribution: str) -> ValueError:
    """Build the loop refusal shared by both real-path arms of the guard below.

    One sentence and one path presentation for both arms; the *attribution*
    deliberately differs, because the two arms blame different knobs -- forcing
    a single wording would make the final-component arm point at workspace_root
    even when the loop is in the root the operator actually configured, which is
    the same misattribution this change exists to remove (#1545 design D1).

    The path is carried verbatim, like this guard's sibling refusals: the guard
    receives neither an evidence-redaction flag nor a config handle, so it
    cannot reproduce the preflight lane's ``[local-path]`` treatment.
    """

    return ValueError(
        f"production scheduler {field_name} must not resolve through a symlink loop: {path} ({attribution})"
    )


def _require_safe_directory_final_component(path: Path, workspace_root: Path, field_name: str) -> None:
    # Parent-segment arm, same paradigm as _confined_path: a loop ABOVE the
    # final component must reach the lstat() verdict below on every CPython
    # instead of aborting here on <=3.12 (#1520).
    _scheduler._require_under_workspace(_canonical_parent(path), workspace_root, field_name)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == ELOOP:
            # A loop somewhere above the final component.  This arm is reached
            # both when workspace_root itself loops -- and then this field is
            # merely derived from it, so the bare field name used to send
            # operators to a knob they never set -- and when the loop sits
            # inside a root they did configure, so name both (#1545).
            raise _symlink_loop_refusal(
                field_name,
                path,
                f"the loop lies above the final component; check {field_name} "
                f"and workspace_root {workspace_root}",
            ) from error
        # Every other lstat failure keeps its wording verbatim: the non-loop
        # geometries on this arm (workspace_root is a regular file, or its mode
        # denies traversal) are pinned elsewhere, hence an errno split here
        # rather than a rewrite of the arm.
        raise ValueError(f"production scheduler {field_name} must be a safe directory") from error
    if stat.S_ISLNK(path_stat.st_mode):
        # Strict real-path is the only source of a verdict that both interpreter
        # arms agree on: non-strict resolution raises an errno-less RuntimeError
        # on <=3.12 and silently adopts the loop on 3.13+ (#1544).
        try:
            resolved = Path(os.path.realpath(path, strict=True))
        except OSError as error:
            if error.errno == ELOOP:
                # ELOOP fires for a cycle ANYWHERE in the resolution, not only
                # in the final component: this link's target may itself
                # traverse a loop that lives elsewhere, and the lstat arm above
                # only cleared the parent segment of *path*.  The kernel names
                # the looping component in error.filename -- byte-identically
                # on every supported CPython -- so the attribution follows it
                # instead of asserting a geometry that may not hold; naming the
                # wrong link is the misattribution #1545 exists to remove.
                raise _symlink_loop_refusal(
                    field_name,
                    path,
                    f"the symlink loop is at {error.filename or path}",
                ) from error
            # Every other strict failure falls back to the non-strict product
            # this arm has always used, so only the loop changes verdict.  The
            # fallback is load-bearing and wider than ENOENT alone: a dangling
            # link raises ENOENT, a target reached through a file raises
            # ENOTDIR, and a target under an unreadable directory raises EACCES
            # -- all three are accepted today, and refusing them here would
            # silently break configurations that work.
            resolved = Path(os.path.realpath(path))
        _scheduler._require_under_workspace(resolved, workspace_root, field_name)
        _classify_resolved_directory_target(resolved, field_name)
        return
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")


def _classify_resolved_directory_target(target: Path, field_name: str) -> None:
    """Classify a contained, realpath-resolved final target by explicit metadata.

    The pre-change gate read ``Path.exists() and not Path.is_dir()``, which
    swallows different ``OSError`` sets on different CPython versions: EACCES on
    a denied traversal is ``False`` from 3.12 on but raises ``PermissionError``
    on 3.11, so the two interpreters reached opposite verdicts on the same
    geometry (#1623).  Every verdict here comes from one errno-aware metadata
    lookup instead:

    - ``ENOENT``/``ENOTDIR``: provably absent -- accepted as before.
    - directory: accepted.
    - non-directory: keeps ``must be a directory``.
    - ``EACCES``/``EPERM``: the traversal is denied, so the guard cannot prove
      the configured target is a directory -- fail closed with the guard's
      structured ``ValueError`` family, never a raw ``PermissionError`` and
      never silent acceptance as a missing target.
    - any other metadata failure: keep the directory-verdict wording, which is
      what the pre-change ``is_dir()`` produced for every non-EACCES swallow.
    """

    try:
        mode = target.stat().st_mode
    except FileNotFoundError:
        return
    except NotADirectoryError:
        return
    except PermissionError as error:
        raise ValueError(
            f"production scheduler {field_name} must be a safe directory: "
            f"cannot verify {target} is a directory because traversal is denied"
        ) from error
    except OSError as error:
        if error.errno in (EACCES, EPERM):
            raise ValueError(
                f"production scheduler {field_name} must be a safe directory: "
                f"cannot verify {target} is a directory because traversal is denied"
            ) from error
        raise ValueError(f"production scheduler {field_name} must be a directory") from error
    if not stat.S_ISDIR(mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")
