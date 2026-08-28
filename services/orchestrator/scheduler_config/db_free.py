from __future__ import annotations

import os
import re
from errno import ENOENT
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from services.orchestrator import scheduler as _scheduler
from services.orchestrator import source_cycle_raw_manifest

from .path_modes import _expanduser_for_mode

if TYPE_CHECKING:
    from .config import ProductionSchedulerConfig

_DB_FREE_RAW_MANIFEST_PREFIX_ENV = "NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX"

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
    """Containment bases for the db-free lane (read-only view of the verdict).

    Retained per D4 as the roots-only reader view of the adjudication; it
    currently has no callers -- the pair function is the live surface.
    """

    return _db_free_allowed_roots_and_blockers(config)[0]


def _db_free_allowed_roots_and_blockers(
    config: ProductionSchedulerConfig,
) -> tuple[tuple[Path, ...], list[dict[str, Any]]]:
    """Adjudicate this lane's containment bases and report unsafe ones.

    Strict resolution + errno split, the same paradigm as the storage preflight:
    non-strict resolution stopped raising on symlink loops in CPython 3.13+, and
    strict ``Path.resolve()`` raises an errno-less ``RuntimeError`` on <=3.12.
    ENOENT keeps the historical "admitted" semantics. Other errnos stay
    lexically tolerated on db-free runtimes (PR #831), but this preflight also
    runs on database-backed ``repair_missing_forcing`` passes, where it performs
    real containment checks -- there the root is dropped and a blocker records
    why, so an unresolvable root can never become a phantom containment base.
    """

    db_free_required = bool(config.scheduler_db_free_required)
    roots: list[Path] = []
    blockers: list[dict[str, Any]] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        expanded = _expanduser_for_mode(value, db_free_required=True)
        try:
            root = Path(os.path.realpath(expanded, strict=True))
        except OSError as error:
            if getattr(error, "errno", None) == ENOENT:
                root = Path(os.path.realpath(expanded))
            elif db_free_required:
                root = expanded
                if not root.is_absolute():
                    root = Path.cwd() / root
            else:
                blockers.append(
                    _db_free_blocker(
                        "db_free_allowed_root_unsafe",
                        "NHMS_SCHEDULER_ALLOWED_ROOTS",
                        _scheduler._scheduler_root_os_error_reason(error).lower(),
                        path=str(expanded),
                    )
                )
                continue
        if root not in roots:
            roots.append(root)
    return tuple(roots), blockers


def _db_free_path_identity(value: str | Path | None) -> Path | None:
    # Non-strict os.path.realpath, not Path.resolve(strict=False): the latter
    # returns the unresolved path on <=3.12 (it raises an errno-less
    # RuntimeError on a loop, which the old except arm swallowed) and the folded
    # form on 3.13+, so the identity verdicts this helper feeds
    # (ProductionSchedulerConfig's topology comparisons) were
    # interpreter-dependent. The realpath form folds on every supported version.
    # This helper has no rejection channel and its callers compare only its own
    # products, so the pre-existing ValueError escape on an unrepresentable path
    # string is retained as-is -- folding it would need a sentinel this lane
    # does not define.
    if value in (None, ""):
        return None
    path = _expanduser_for_mode(str(value).strip(), db_free_required=True)
    return Path(os.path.realpath(path))


def _db_free_loop_filtered_realpath(path: Path) -> tuple[Path | None, OSError | ValueError | None]:
    """Normalize a db-free required path, or report why it cannot be normalized.

    Strict resolution + errno split + loop-filtered ENOENT admit, the same
    paradigm the retry lane's selector adjudicators carry: ``Path.resolve()`` is
    not a usable loop predicate on the supported interpreter range (the
    non-strict form stopped raising on symlink loops in CPython 3.13+, the
    strict form raises an errno-less ``RuntimeError`` on <=3.12), while strict
    ``os.path.realpath`` raises ``OSError`` carrying an errno everywhere.

    ENOENT keeps the historical admitted semantics -- a required path whose
    final components do not exist yet is adjudicated by the downstream
    missing-parent / not-found blockers, not by this step -- but the admission
    is loop-filtered: the non-strict fallback is re-resolved strictly, so a
    fallback that still lands on a symlink loop is reported rather than folded
    lexically and then mis-attributed as "not created".
    """

    try:
        return Path(os.path.realpath(path, strict=True)), None
    except (OSError, ValueError) as error:
        if getattr(error, "errno", None) != ENOENT:
            return None, error
    try:
        fallback = os.path.realpath(path)
    except (OSError, ValueError) as fallback_error:
        return None, fallback_error
    try:
        os.path.realpath(fallback, strict=True)
    except (OSError, ValueError) as recheck_error:
        if getattr(recheck_error, "errno", None) != ENOENT:
            return None, recheck_error
    return Path(fallback), None


def _db_free_resolution_failure_reason(error: OSError | ValueError) -> str:
    # _scheduler_root_os_error_reason returns UPPERCASE tokens and, unlike
    # _scheduler_root_blocker, _db_free_blocker does not lowercase what it is
    # handed -- hence the explicit .lower(), matching the call in
    # _db_free_allowed_roots_and_blockers above. A ValueError carries no errno
    # (the mapper reads error.errno unguarded), so an unrepresentable path
    # string takes the mapper's own catch-all token instead.
    if isinstance(error, OSError):
        return _scheduler._scheduler_root_os_error_reason(error).lower()
    return "unavailable"


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
    # A value that fails this step no longer reaches the containment comparison
    # or the parent-lstat gate below, so a loop reports the errno-derived unsafe
    # reason instead of being folded lexically and then attributed as
    # "not found" (inside the bases) or "outside boundary" (outside them). Both
    # of those were already rejections, so for a PERSISTENT fault -- a loop, an
    # unreadable parent -- the reported reason changes within the rejected class
    # only, and the verdict does not move.
    #
    # A TRANSIENT non-ENOENT errno is the exception, and it is a deliberate
    # fail-closed trade rather than an oversight. The pre-change form resolved
    # through Path.resolve(strict=False), whose non-strict realpath swallows
    # every errno internally, so no resolution-layer fault could produce a
    # blocker at all; a one-shot ESTALE/EIO on an otherwise healthy NFS path was
    # admitted here and then passed the later lstat/exists gates. It now blocks,
    # and db_free_runtime_preflight is pass-level (scheduler_runtime.py:650),
    # so the pass aborts with db_free_runtime_preflight_blocked before the lock
    # is taken. Self-healing on the next pass, and evidence is written -- but on
    # NFS-backed node-22 this is availability traded for containment safety.
    resolved, resolution_error = _db_free_loop_filtered_realpath(path)
    if resolution_error is not None:
        check.update({"absolute": True, "contained": False})
        return check, _db_free_blocker(
            "db_free_required_path_unsafe",
            env,
            _db_free_resolution_failure_reason(resolution_error),
            path=str(path),
            error_type=type(resolution_error).__name__,
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
    if kind in {"directory", "readable_directory"}:
        if not exists:
            return check, _db_free_blocker("db_free_required_path_not_found", env, "not_found", path=str(resolved))
        if path.is_symlink() or not path.is_dir():
            return check, _db_free_blocker("db_free_required_path_unsafe", env, "unsafe", path=str(resolved))
        if kind == "directory" and not _scheduler._directory_is_writable(path):
            return check, _db_free_blocker(
                "db_free_required_path_not_writable",
                env,
                "not_writable",
                path=str(resolved),
            )
        if kind == "directory":
            check["writable"] = True
        elif not os.access(path, os.R_OK | os.X_OK):
            return check, _db_free_blocker(
                "db_free_required_path_not_readable",
                env,
                "not_readable",
                path=str(resolved),
            )
        else:
            check["readable"] = True
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


def _db_free_raw_manifest_prefix_evidence(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    return {
        "env": _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
        "configured": bool(text),
        "value": "[object-prefix]" if text else None,
    }


def _db_free_raw_manifest_prefix_check(
    value: Any,
    *,
    require_canonical: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    text = str(value or "").strip().rstrip("/")
    check = _db_free_raw_manifest_prefix_evidence(text)
    if not text:
        return check, _db_free_blocker(
            "db_free_raw_manifest_prefix_missing",
            _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
            "missing",
        )
    try:
        parsed = _db_free_urlparse(text)
        unsafe_reason = _db_free_common_object_uri_unsafe_reason(text, parsed)
        if (
            parsed.scheme.lower() != "s3"
            or not parsed.netloc
            or unsafe_reason is not None
        ):
            raise ValueError(unsafe_reason or "unsupported_prefix")
        raw_path = str(parsed.path or "").strip("/")
        if raw_path:
            _db_free_safe_object_key(raw_path)
    except ValueError:
        check["value"] = "[invalid-object-prefix]"
        return check, _db_free_blocker(
            "db_free_raw_manifest_prefix_invalid",
            _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
            "invalid",
        )
    authority_matches = (
        text == source_cycle_raw_manifest.NODE22_CANONICAL_NFS_RAW_MANIFEST_PREFIX
        if require_canonical
        else None
    )
    check.update(
        {
            "scheme": "s3",
            "supported": True,
            "canonical_authority_required": require_canonical,
            "authority_matches": authority_matches,
        }
    )
    if authority_matches is False:
        return check, _db_free_blocker(
            "db_free_raw_manifest_prefix_authority_mismatch",
            _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
            "canonical_authority_mismatch",
        )
    return check, None


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
