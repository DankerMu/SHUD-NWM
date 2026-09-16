"""Descriptor-bound DSN, readonly session, and no-clobber evidence IO."""

from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packages.common.node27_pgdata_workload_types import PgdataWorkloadError, refuse
from packages.common.redaction import redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    read_bytes_durable_no_follow,
    stat_no_follow,
    unlink_no_follow,
)
from packages.common.safe_fs_publication import (
    move_regular_file_no_follow_exclusive,
    write_bytes_no_follow_exclusive,
)

APPLICATION_NAME = "nhms-pgdata-workload"
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT = "2s"
READONLY_ROLE = "nhms_display_ro"
ALLOWED_DSN_KEYS = {"host", "port", "dbname", "user", "password", "sslmode"}
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
LOCAL_ORIGIN_RE = re.compile(r"^http://127\.0\.0\.1:(?:[1-9][0-9]{0,4})$")
MAX_DSN_BYTES = 16384
MAX_OUTPUT_BYTES = 262_144
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
OUTPUT_FILE_MODE = 0o600
GIT_TIMEOUT_SECONDS = 10
MAX_HEAD_OUTPUT_BYTES = 128
# A repository root is a path, not a digest: the head ceiling would reject ordinary
# deep checkout paths, so bound it separately.
MAX_TOPLEVEL_OUTPUT_BYTES = 4096
# Variables that let an inherited environment make git answer for a different
# repository than the directory we are asking about.
REDIRECTING_GIT_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def format_refusal(error: Exception) -> str:
    if isinstance(error, PgdataWorkloadError):
        return redact_text(f"{error.code}: {error}")
    return redact_text("SQL_CONNECT_FAILED: readonly workload connection failed")


def validate_sha(value: str, *, label: str) -> str:
    del label
    text = str(value or "").strip()
    if HEAD_RE.fullmatch(text) is None:
        refuse("reviewed SHA must be a lowercase 40-hex digest", code="INPUT_SHA_INVALID", stage="input")
    return text


def _run_git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key not in REDIRECTING_GIT_ENVIRONMENT}
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        refuse("executing checkout HEAD query timed out", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    except OSError:
        refuse("executing checkout HEAD cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")


def resolve_repository_head(repo_root: Path) -> str:
    """Return the HEAD of a clean-tracked checkout, refusing rather than guessing.

    ``repo_root`` must itself be the repository git answers for. Git discovery
    otherwise walks upwards, so a deployed tree without its own ``.git`` unpacked
    inside another checkout would be handed the OUTER repository's HEAD and report
    clean, the nested tree being merely untracked there. The redirecting ``GIT_*``
    variables are dropped for the same reason: an inherited environment must not be
    able to answer for a checkout that is not the one running.

    Untracked files never refuse: an operator checkout routinely carries untracked
    evidence directories. A modified tracked file does refuse, because the receipt
    would otherwise claim a SHA that is not the code that ran.
    """

    head_result = _run_git(repo_root, "rev-parse", "HEAD")
    head = head_result.stdout.strip()
    if (
        head_result.returncode != 0
        or len(head_result.stdout) > MAX_HEAD_OUTPUT_BYTES
        or HEAD_RE.fullmatch(head) is None
    ):
        refuse("executing checkout HEAD cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    toplevel_result = _run_git(repo_root, "rev-parse", "--show-toplevel")
    toplevel = toplevel_result.stdout.strip()
    if (
        toplevel_result.returncode != 0
        or len(toplevel_result.stdout) > MAX_TOPLEVEL_OUTPUT_BYTES
        or not toplevel
        or not Path(toplevel).is_absolute()
    ):
        refuse("executing checkout root cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    try:
        answered_for_this_root = Path(toplevel).resolve() == Path(repo_root).resolve()
    except OSError:
        refuse("executing checkout root cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    if not answered_for_this_root:
        refuse("executing directory is not its own git checkout", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    clean_result = _run_git(repo_root, "diff", "--quiet", "HEAD", "--")
    if clean_result.returncode == 1:
        refuse("executing checkout has modified tracked files", code="INPUT_SHA_UNBOUND", stage="input")
    if clean_result.returncode != 0:
        refuse("executing checkout cleanliness cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    return head


def bind_reviewed_sha(reviewed_sha: str, *, head_resolver: Callable[[], str]) -> str:
    """Bind an already shape-checked reviewed SHA to the executing checkout's HEAD."""

    try:
        resolved = head_resolver()
    except PgdataWorkloadError:
        raise
    except Exception:
        refuse("executing checkout HEAD cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    head = str(resolved or "").strip()
    if HEAD_RE.fullmatch(head) is None:
        refuse("executing checkout HEAD cannot be determined", code="INPUT_SHA_HEAD_UNAVAILABLE", stage="input")
    if head != reviewed_sha:
        refuse("reviewed SHA is not the executing checkout HEAD", code="INPUT_SHA_UNBOUND", stage="input")
    return head


def require_runtime_anchored(repo_root: Path, module_names: Sequence[str]) -> None:
    """Refuse unless the modules that do the work resolve under the anchored checkout.

    The entrypoint's own location proves nothing on its own: published script bytes
    are routinely run against a separate ``PYTHONPATH`` checkout, and binding the
    entrypoint's HEAD would then attribute the samples to a tree that did not supply
    the code that captured, measured and published them.
    """

    try:
        root = Path(repo_root).resolve()
    except OSError:
        refuse("executing checkout root cannot be resolved", code="INPUT_RUNTIME_UNBOUND", stage="input")
    for name in module_names:
        try:
            module = importlib.import_module(name)
        except Exception:
            refuse("a workload module is not importable", code="INPUT_RUNTIME_UNBOUND", stage="input")
        origin = getattr(module, "__file__", None)
        if not origin:
            refuse("a workload module has no resolvable file", code="INPUT_RUNTIME_UNBOUND", stage="input")
        try:
            resolved = Path(str(origin)).resolve()
        except OSError:
            refuse("a workload module path cannot be resolved", code="INPUT_RUNTIME_UNBOUND", stage="input")
        if not resolved.is_relative_to(root):
            refuse(
                "workload modules do not resolve under the executing checkout",
                code="INPUT_RUNTIME_UNBOUND",
                stage="input",
            )


def validate_id(value: str, *, code: str) -> str:
    text = str(value or "").strip()
    if ID_RE.fullmatch(text) is None:
        refuse("identifier is empty or not a bounded token", code=code, stage="input")
    return text


def validate_origin(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if LOCAL_ORIGIN_RE.fullmatch(text) is None:
        refuse("API origin is not the local loopback contract", code="INPUT_ORIGIN_INVALID", stage="input")
    return text


def validate_source(value: str) -> str:
    text = str(value or "").strip().upper()
    if text not in {"GFS", "IFS"}:
        refuse("source is not a shipping GFS/IFS lane", code="QUERY_SOURCE_INVALID", stage="query")
    return text


def require_absolute_path(value: str | Path, *, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or "\x00" in str(path):
        refuse("path must be absolute", code=code, stage="io")
    return path


def read_private_dsn_file(path: Path) -> str:
    target = require_absolute_path(path, code="DSN_PATH_INVALID")
    try:
        info = stat_no_follow(target)
    except (OSError, SafeFilesystemError):
        refuse("reader DSN file is unavailable", code="DSN_FILE_UNAVAILABLE", stage="io")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        refuse("reader DSN file is not a regular non-symlink file", code="DSN_FILE_IDENTITY", stage="io")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        refuse("reader DSN file must be euid-owned mode-0600 nlink-1", code="DSN_FILE_IDENTITY", stage="io")
    try:
        raw = read_bytes_durable_no_follow(target, max_bytes=MAX_DSN_BYTES)
    except (OSError, SafeFilesystemError):
        refuse("reader DSN file is unavailable", code="DSN_FILE_UNAVAILABLE", stage="io")
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        refuse("reader DSN file is not valid UTF-8", code="DSN_FILE_INVALID", stage="io")
    if not text or "\n" in text or "\x00" in text:
        refuse("reader DSN file is empty or multiline", code="DSN_FILE_INVALID", stage="io")
    return text


def parse_readonly_dsn(dsn: str) -> dict[str, str]:
    from psycopg2.extensions import parse_dsn

    try:
        options = parse_dsn(dsn)
    except Exception:
        refuse("reader DSN is malformed", code="DSN_INVALID", stage="dsn")
    if set(options) - ALLOWED_DSN_KEYS:
        refuse("DSN uses unsupported connection indirection/options", code="DSN_OPTIONS_INVALID", stage="dsn")
    if not all(options.get(key) for key in ("host", "port", "dbname", "user", "password")):
        refuse("DSN identity/credential is incomplete", code="DSN_INCOMPLETE", stage="dsn")
    if options.get("host") != "127.0.0.1":
        refuse("DSN host is not local loopback", code="DSN_HOST_INVALID", stage="dsn")
    user = str(options.get("user") or "")
    if user != READONLY_ROLE:
        refuse("readonly DSN is not the nhms_display_ro role", code="DSN_ROLE_INVALID", stage="dsn")
    split = urlsplit(dsn) if "://" in dsn else None
    if split is not None and split.username not in {None, READONLY_ROLE}:
        refuse("readonly DSN is not the nhms_display_ro role", code="DSN_ROLE_INVALID", stage="dsn")
    return {key: str(options[key]) for key in options}


def _attributed_connect(dsn: str) -> Any:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    options = parse_readonly_dsn(dsn)
    return psycopg2.connect(
        host=options["host"],
        port=options["port"],
        dbname=options["dbname"],
        user=options["user"],
        password=options["password"],
        sslmode=options.get("sslmode", "prefer"),
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=APPLICATION_NAME,
        cursor_factory=RealDictCursor,
    )


def open_readonly_connection(
    dsn: str,
    *,
    connect: Any | None = None,
) -> Any:
    opener = connect if connect is not None else _attributed_connect
    connection: Any | None = None
    try:
        parse_readonly_dsn(dsn)
        connection = opener(dsn)
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
            cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            cursor.execute(f"SET LOCAL application_name = '{APPLICATION_NAME}'")
    except PgdataWorkloadError:
        if connection is not None:
            close_readonly_connection(connection)
        raise
    except Exception:
        if connection is not None:
            close_readonly_connection(connection)
        refuse("readonly workload connection failed", code="SQL_CONNECT_FAILED", stage="performance")
    return connection


def close_readonly_connection(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _fetchone(cursor: Any) -> Any:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    if isinstance(row, (list, tuple)) and row:
        return row[0]
    return row


def prove_readonly_session(connection: Any) -> dict[str, Any]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('transaction_read_only')")
            read_only = str(_fetchone(cursor) or "").strip().lower()
            cursor.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cursor.fetchone()
    except Exception:
        refuse("readonly session proof failed", code="SQL_READONLY_PROOF_FAILED", stage="performance")
    if read_only not in {"on", "true"}:
        refuse("transaction_read_only is not on", code="SQL_NOT_READONLY", stage="performance")
    if isinstance(row, Mapping):
        current_user = str(row.get("current_user") or "")
        rolsuper = bool(row.get("rolsuper"))
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        current_user = str(row[0])
        rolsuper = bool(row[1])
    else:
        refuse("readonly session identity is missing", code="SQL_READONLY_PROOF_FAILED", stage="performance")
    if current_user != READONLY_ROLE:
        refuse("session current_user is not nhms_display_ro", code="DSN_ROLE_INVALID", stage="dsn")
    if rolsuper:
        refuse("nhms_display_ro session is superuser", code="DSN_ROLE_NOT_READONLY", stage="dsn")
    return {"transaction_read_only": True, "current_user": current_user}


def utc_now() -> datetime:
    return datetime.now(UTC)


def encode_evidence(document: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        refuse("measurement output is not serializable", code="OUTPUT_JSON_INVALID", stage="io")
    if len(encoded) > MAX_OUTPUT_BYTES:
        refuse("measurement output exceeds the byte ceiling", code="OUTPUT_TOO_LARGE", stage="io")
    return encoded


def _stage_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = stat_no_follow(path)
    except (OSError, SafeFilesystemError):
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _unlink_owned_stage(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    current = _stage_identity(path)
    if current != identity:
        return
    try:
        unlink_no_follow(path, missing_ok=True)
    except (OSError, SafeFilesystemError):
        return


def publish_measurement_output(path: Path, document: Mapping[str, Any]) -> None:
    target = require_absolute_path(path, code="OUTPUT_PATH_INVALID")
    encoded = encode_evidence(document)
    stage = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
    staged_identity: tuple[int, int] | None = None
    try:
        write_bytes_no_follow_exclusive(
            stage,
            encoded,
            require_durable_create=True,
            mode=OUTPUT_FILE_MODE,
        )
    except FileExistsError:
        refuse("measurement output could not be published", code="OUTPUT_PUBLISH_FAILED", stage="io")
    except SafeFilesystemError as error:
        if error.kind == "indeterminate":
            refuse(
                "measurement output publication is indeterminate",
                code="OUTPUT_PUBLISH_INDETERMINATE",
                stage="io",
            )
        refuse("measurement output could not be published", code="OUTPUT_PUBLISH_FAILED", stage="io")
    staged_identity = _stage_identity(stage)
    try:
        move_regular_file_no_follow_exclusive(stage.parent, stage.name, target.parent, target.name)
    except FileExistsError:
        _unlink_owned_stage(stage, staged_identity)
        refuse("target already exists", code="OUTPUT_EXISTS", stage="io")
    except SafeFilesystemError as error:
        if error.kind == "indeterminate":
            refuse(
                "measurement output publication is indeterminate",
                code="OUTPUT_PUBLISH_INDETERMINATE",
                stage="io",
            )
        _unlink_owned_stage(stage, staged_identity)
        refuse("measurement output could not be published", code="OUTPUT_PUBLISH_FAILED", stage="io")


def replace_measurement_output(path: Path, document: Mapping[str, Any]) -> None:
    """Internal helper for tests that need a durable replace; CLI never clobbers."""

    target = require_absolute_path(path, code="OUTPUT_PATH_INVALID")
    encoded = encode_evidence(document)
    atomic_write_bytes_no_follow(target, encoded, mode=0o600, require_durable_replace=True)
