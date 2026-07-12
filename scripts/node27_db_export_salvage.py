#!/usr/bin/env python3
"""One-time DB-export salvage runner for node-27 (issue #850).

Task 3.1 of ``tier-node27-timeseries-storage``. Consumes the archive-
completeness receipt's ``salvage_selectors`` verbatim (hardcoded lists
refused), runs ``COPY (SELECT ... WHERE ...) TO STDOUT WITH (FORMAT CSV,
HEADER)`` per selector, zstd-compresses the CSV, and publishes an object
plus schema-valid ``manifest.json`` under
``NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/`` via
``atomic_write_bytes_no_follow``. Emits a receipt outside the archive root.
Dry-run by default. Never runs DDL; never deletes rows or archive objects.
Fails closed if the DSN's role can INSERT into either hypertable
(has_table_privilege OR rolled-back sentinel INSERT). See design D1, D6,
and ADR 0002 decision 3.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

import jsonschema

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
)

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "node27-db-export-salvage/1"

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_SCHEMA_PATH = _ROOT / "schemas/salvage_manifest.schema.json"
_RECEIPT_INPUT_SCHEMA_PATH = _ROOT / "schemas/archive_completeness_receipt.schema.json"

# Full DDL column set for each hypertable. The salvage lane exists so that
# a manual ``COPY FROM`` restore can rebuild the exact row shape — a PK+value
# subset would not be sufficient to reconstruct the row. See
# ``db/migrations/000005_met.sql`` and ``db/migrations/000006_hydro.sql``.
_COLUMNS_FORCING: tuple[str, ...] = (
    "forcing_version_id", "basin_version_id", "station_id", "valid_time",
    "source_id", "variable", "value", "unit", "native_resolution", "quality_flag",
)
_COLUMNS_RIVER: tuple[str, ...] = (
    "run_id", "basin_version_id", "river_network_version_id", "river_segment_id",
    "valid_time", "lead_time_hours", "variable", "value", "unit", "quality_flag",
    "created_at",
)

_TABLE_TO_LANE = {
    "met.forcing_station_timeseries": "forcing",
    "hydro.river_timeseries": "runs",
}
_TABLE_TO_COLUMNS = {
    "met.forcing_station_timeseries": _COLUMNS_FORCING,
    "hydro.river_timeseries": _COLUMNS_RIVER,
}
_TABLE_TO_IDENTITY_KEY = {
    "met.forcing_station_timeseries": "forcing_version_id",
    "hydro.river_timeseries": "run_id",
}

# safe_relative_path regex mirrors ``schemas/salvage_manifest.schema.json``.
# Enforced at runtime independently of manifest schema validation (defence
# in depth per fixture regression row).
_SAFE_RELATIVE_PATH_RE = re.compile(
    r"^(?!/)(?![A-Za-z]:/)(?!.*(?:^|/)(?:\.{1,2})(?:/|$))(?!.*//)"
    r"(?!.*[\\\x00-\x1f\x7f])[^/]+(?:/[^/]+)*$"
)
_DB_EXPORT_PATH_RE = re.compile(r"^db-export/(?:[^/]+/)*[^/]+\.csv\.zst$")

# Identity strings become path components. They must be safe on their own —
# no path separators, no traversal segments, no control chars.
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")


class SalvageConfigError(RuntimeError):
    """Fail-closed configuration parse error before any DB call."""

    def __init__(self, message: str, *, outcome: str = "refused_config") -> None:
        super().__init__(message)
        self.outcome = outcome


class SalvageRoleError(RuntimeError):
    """Refusal because the DB role is not effectively read-only."""


@dataclass(frozen=True)
class SalvageConfig:
    database_url: str
    archive_root: Path
    receipt_input_path: Path
    receipt_output_path: Path
    lock_path: Path
    per_tick_bound: int
    zstd_level: int
    statement_timeout_ms: int
    source_instance_id: str
    mode: str  # "dry-run" | "enforce"
    max_selector_bytes: int
    zstd_path: Path

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"


def _mask_dsn(dsn: str) -> str:
    """Return a DSN safe for stderr diagnostics — credentials stripped."""
    try:
        parts = urlsplit(dsn)
    except Exception:
        return "postgresql://***@***/***"
    netloc = parts.hostname or "***"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username is not None or parts.password is not None:
        netloc = f"***@{netloc}"
    return urlunsplit((parts.scheme or "postgresql", netloc, parts.path or "", "", ""))


def _parse_positive_int(raw: str | None, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if raw is None or raw == "":
        raise SalvageConfigError(f"{name} must be set")
    stripped = raw.strip()
    if stripped == "" or stripped != raw:
        raise SalvageConfigError(f"{name} must not contain leading/trailing whitespace")
    try:
        value = int(stripped)
    except ValueError as error:
        raise SalvageConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise SalvageConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SalvageConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _require_absolute(path: Path, *, name: str) -> Path:
    if not path.is_absolute():
        raise SalvageConfigError(f"{name} must be absolute, got {path}")
    return path


def _validate_zstd_path(path: Path) -> Path:
    """Mirror of the mover's `_validate_zstd` — refuse relative, symlink, or missing."""
    if not path.is_absolute():
        raise SalvageConfigError("NODE27_DB_EXPORT_SALVAGE_ZSTD must be absolute")
    try:
        info = path.lstat()
    except OSError as error:
        raise SalvageConfigError(
            f"NODE27_DB_EXPORT_SALVAGE_ZSTD binary is unavailable: {path}: {error}"
        ) from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not os.access(path, os.X_OK)
    ):
        raise SalvageConfigError(
            f"NODE27_DB_EXPORT_SALVAGE_ZSTD must be an executable regular non-symlink file: {path}"
        )
    return path


def config_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> SalvageConfig:
    """Strict env + CLI parse. Fail-closed on hardcoded selector list."""
    env = os.environ if env is None else env
    if getattr(args, "selectors", None):
        raise SalvageConfigError(
            "hardcoded --selectors list is refused; the archive-completeness "
            "receipt is the sole selector scope source"
        )
    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise SalvageConfigError("DATABASE_URL must be set")
    archive_root_raw = env.get("NHMS_ARCHIVE_ROOT")
    if not archive_root_raw or not archive_root_raw.strip():
        raise SalvageConfigError("NHMS_ARCHIVE_ROOT must be set")
    receipt_input_raw = env.get("NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH")
    if not receipt_input_raw or not receipt_input_raw.strip():
        raise SalvageConfigError("NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH must be set")
    receipt_out_raw = env.get("NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH")
    if not receipt_out_raw or not receipt_out_raw.strip():
        raise SalvageConfigError("NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH must be set")
    lock_raw = env.get("NODE27_DB_EXPORT_SALVAGE_LOCK_PATH")
    if not lock_raw or not lock_raw.strip():
        raise SalvageConfigError("NODE27_DB_EXPORT_SALVAGE_LOCK_PATH must be set")
    per_tick_bound = _parse_positive_int(
        env.get("NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND"),
        name="NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND",
        minimum=1,
    )
    zstd_level_raw = env.get("NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL", "3")
    zstd_level = _parse_positive_int(
        zstd_level_raw,
        name="NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL",
        minimum=1,
        maximum=22,
    )
    timeout_raw = env.get("NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS", "300000")
    statement_timeout_ms = _parse_positive_int(
        timeout_raw,
        name="NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS",
        minimum=1000,
    )
    source_instance_id = env.get("NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID")
    if not source_instance_id or not source_instance_id.strip():
        raise SalvageConfigError("NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID must be set")
    if source_instance_id.strip() != source_instance_id:
        raise SalvageConfigError(
            "NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID must not contain leading/trailing whitespace"
        )
    mode_raw = env.get("NODE27_DB_EXPORT_SALVAGE_MODE", "dry-run")
    if mode_raw not in {"dry-run", "enforce"}:
        raise SalvageConfigError(
            f"NODE27_DB_EXPORT_SALVAGE_MODE must be one of dry-run|enforce, got {mode_raw!r}"
        )
    # Byte cap gates the buffered COPY-then-compress flow so a single selector
    # cannot swamp memory. Default 2 GiB matches "single interactive salvage
    # tick" scope; operators can shrink it if their host is memory constrained.
    max_selector_bytes = _parse_positive_int(
        env.get("NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES", str(2 * 1024 * 1024 * 1024)),
        name="NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES",
        minimum=1,
    )
    zstd_raw = env.get("NODE27_DB_EXPORT_SALVAGE_ZSTD", "/usr/bin/zstd")
    if not zstd_raw or not zstd_raw.strip() or zstd_raw.strip() != zstd_raw:
        raise SalvageConfigError(
            "NODE27_DB_EXPORT_SALVAGE_ZSTD must not be empty or contain leading/trailing whitespace"
        )
    zstd_path = _validate_zstd_path(Path(zstd_raw))
    return SalvageConfig(
        database_url=database_url,
        archive_root=_require_absolute(Path(archive_root_raw), name="NHMS_ARCHIVE_ROOT"),
        receipt_input_path=_require_absolute(
            Path(receipt_input_raw), name="NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"
        ),
        receipt_output_path=_require_absolute(
            Path(receipt_out_raw), name="NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"
        ),
        lock_path=_require_absolute(Path(lock_raw), name="NODE27_DB_EXPORT_SALVAGE_LOCK_PATH"),
        per_tick_bound=per_tick_bound,
        zstd_level=zstd_level,
        statement_timeout_ms=statement_timeout_ms,
        source_instance_id=source_instance_id,
        mode=mode_raw,
        max_selector_bytes=max_selector_bytes,
        zstd_path=zstd_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # ``--selectors`` exists solely so the runner can refuse it explicitly.
    # The archive-completeness receipt is the sole scope source (design D6).
    parser.add_argument(
        "--selectors",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def acquire_lock(path: Path) -> int | None:
    """Take a nonblocking flock on a mode-0600 lock file. Return None on contention."""
    if not path.is_absolute():
        raise SalvageConfigError("lock path must be absolute")
    ensure_directory_no_follow(path.parent)
    common_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = open_directory_no_follow(path.parent)
    fd: int | None = None
    try:
        try:
            fd = os.open(path.name, common_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(path.name, common_flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise SalvageConfigError("lock file must be a mode-0600 regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except SalvageConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise SalvageConfigError(f"cannot acquire lock file: {error}") from error
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Injectable DB / compression callables
# ---------------------------------------------------------------------------

# Returns None if the role is confirmed read-only. Returns a human-readable
# reason string if the role can write to either target hypertable — the
# caller MUST refuse in that case. The default implementation performs BOTH
# checks the fixture demands (has_table_privilege + rolled-back sentinel
# INSERT); tests parametrize the failure modes.
CheckWritePrivileges = Callable[[str], str | None]
FetchRowCount = Callable[[str, str, Mapping[str, Any], int], int]
PerformCopyExport = Callable[[str, str, Sequence[str], Mapping[str, Any], int], bytes]
# Compresses ``data`` at ``level`` via the given absolute zstd binary path.
# The path is injected so the runner can refuse relative / symlink / missing
# binaries at boot rather than at compress-time.
CompressBytes = Callable[[bytes, int, Path], bytes]


class SalvageOversizeError(RuntimeError):
    """Raised when a selector's exported bytes exceed the configured cap."""


def _count_csv_rows(csv_bytes: bytes) -> int:
    """Row count = newline count minus one for the CSV header row.

    Deriving the exported_row_count from the CSV bytes we actually shipped
    guarantees the manifest count and the compressed object come from the
    same MVCC snapshot (a separate SELECT COUNT(*) sits in its own
    connection under READ COMMITTED and can drift under concurrent writes,
    which the display_ro role cannot itself prevent).
    """
    if not csv_bytes:
        return 0
    newlines = csv_bytes.count(b"\n")
    # COPY ... WITH HEADER always emits a header line, and psycopg2's
    # copy_expert writes a trailing newline for every row (including the
    # last). If the header is present but the body is empty, newlines == 1
    # and row_count == 0. If bytes don't end in newline (never in practice
    # from COPY, but defensive), we still treat the header as one line.
    row_count = newlines - 1
    return max(row_count, 0)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _identity_of(selector: Mapping[str, Any]) -> str:
    key = _TABLE_TO_IDENTITY_KEY[selector["table"]]
    return str(selector["identity"][key])


def _predicate_for(selector: Mapping[str, Any]) -> tuple[str, tuple[Any, ...]]:
    key = _TABLE_TO_IDENTITY_KEY[selector["table"]]
    identity = str(selector["identity"][key])
    start = str(selector["window"]["start"])
    end = str(selector["window"]["end"])
    return f"{key} = %s AND valid_time >= %s AND valid_time < %s", (identity, start, end)


def _default_check_write_privileges(dsn: str) -> str | None:
    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT "
                    "has_table_privilege(current_user, 'met.forcing_station_timeseries', 'INSERT'), "
                    "has_table_privilege(current_user, 'hydro.river_timeseries', 'INSERT')"
                )
                row = cursor.fetchone()
                if row is None:
                    return "has_table_privilege returned no row"
                forcing_writable, river_writable = bool(row[0]), bool(row[1])
                if forcing_writable or river_writable:
                    which = []
                    if forcing_writable:
                        which.append("met.forcing_station_timeseries")
                    if river_writable:
                        which.append("hydro.river_timeseries")
                    return f"role can INSERT into {','.join(which)}"
        # Sentinel INSERT (rolled back). If the role is truly read-only,
        # psycopg2 raises InsufficientPrivilege (a subclass of ProgrammingError)
        # and we swallow that as OK. Any other outcome — success OR a
        # non-permission error such as FK/unique violation — proves the role
        # can write, so we refuse.
        for table, identity_col in (
            ("met.forcing_station_timeseries", "forcing_version_id"),
            ("hydro.river_timeseries", "run_id"),
        ):
            reason = _sentinel_insert_check(dsn, table, identity_col)
            if reason is not None:
                return reason
        return None
    finally:
        connection.close()


def _sentinel_insert_check(dsn: str, table: str, identity_col: str) -> str | None:
    """Sentinel INSERT probe. Returns None ONLY when the role is provably read-only.

    Any other outcome — success, constraint violation, connection error,
    timeout — returns a non-None refusal reason so the caller fails closed.
    Connection / timeout / syntax errors are labelled as an unavailable
    privilege probe rather than as "role can INSERT" so operators do not
    misdiagnose a transient DB issue as a real write-privilege leak.
    """
    import psycopg2  # type: ignore[import-untyped]
    from psycopg2 import errors as pg_errors  # type: ignore[import-untyped]

    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            try:
                # Deliberately impossible row: all-NULL to fail on NOT NULL if
                # the role is writable. We do NOT commit — the outer BEGIN is
                # implicit, ROLLBACK forced in finally. If the exception is a
                # permission error, the role is read-only.
                cursor.execute(
                    f"INSERT INTO {table} ({identity_col}) VALUES (%s)",
                    (f"__salvage_write_probe_{os.getpid()}",),
                )
            except pg_errors.InsufficientPrivilege:
                return None
            except pg_errors.SyntaxError:
                return f"role privilege probe unavailable for {table}: syntax error"
            except (pg_errors.OperationalError, pg_errors.QueryCanceled) as error:
                # Connection loss or statement_timeout — we cannot prove the
                # role is read-only, so fail closed with an accurate label.
                return (
                    f"role privilege probe unavailable for {table}: "
                    f"{type(error).__name__}"
                )
            except Exception as error:
                # NotNullViolation, ForeignKeyViolation, CheckViolation, etc.
                # If the role weren't writable, PG would have raised
                # InsufficientPrivilege before evaluating constraints.
                return f"role can INSERT into {table}: {type(error).__name__}"
            else:
                # INSERT succeeded — role is definitely writable.
                return f"role can INSERT into {table}"
    finally:
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()


def _default_fetch_row_count(
    dsn: str, table: str, selector: Mapping[str, Any], timeout_ms: int
) -> int:
    import psycopg2  # type: ignore[import-untyped]

    predicate, params = _predicate_for(selector)
    connection = psycopg2.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {int(timeout_ms)}")
                cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params)
                (count,) = cursor.fetchone()
                return int(count)
    finally:
        connection.close()


def _default_perform_copy_export(
    dsn: str,
    table: str,
    columns: Sequence[str],
    selector: Mapping[str, Any],
    timeout_ms: int,
) -> bytes:
    import io as _io

    import psycopg2  # type: ignore[import-untyped]

    predicate, params = _predicate_for(selector)
    column_list = ", ".join(columns)
    connection = psycopg2.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {int(timeout_ms)}")
                # COPY (SELECT ...) TO STDOUT — mogrify the WHERE parameters
                # first so copy_expert doesn't need parameterization support.
                where_sql = cursor.mogrify(predicate, params).decode("utf-8")
                sql = (
                    f"COPY (SELECT {column_list} FROM {table} WHERE {where_sql}) "
                    f"TO STDOUT WITH (FORMAT CSV, HEADER)"
                )
                buffer = _io.BytesIO()
                cursor.copy_expert(sql, buffer)
                return buffer.getvalue()
    finally:
        connection.close()


def _default_compress_bytes(data: bytes, level: int, zstd_path: Path) -> bytes:
    """Compress via ``<zstd_path> -<level> -q -c`` subprocess (mover-validated binary)."""
    result = subprocess.run(
        [str(zstd_path), f"-{int(level)}", "-q", "-c"],
        input=data,
        capture_output=True,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Receipt input loading
# ---------------------------------------------------------------------------


def _load_input_receipt(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SalvageConfigError(f"cannot read archive-completeness receipt {path}: {error}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SalvageConfigError(f"archive-completeness receipt is not valid JSON: {error}") from error
    try:
        schema = json.loads(_RECEIPT_INPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as error:  # pragma: no cover — packaged with repo
        raise SalvageConfigError(f"cannot read receipt input schema: {error}") from error
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as error:
        raise SalvageConfigError(
            f"archive-completeness receipt failed schema validation: {error.message}"
        ) from error
    if not isinstance(data.get("salvage_selectors"), list):
        raise SalvageConfigError("salvage_selectors must be an array")
    return data


def _load_manifest_schema() -> dict[str, Any]:
    return json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------


def _refuse_unsafe_identity(identity: str) -> None:
    if not _SAFE_IDENTITY_RE.match(identity):
        raise SalvageConfigError(
            f"identity {identity!r} contains characters unsafe for a filesystem path segment"
        )


def _refuse_unsafe_relative_path(relative_path: str) -> None:
    if not _SAFE_RELATIVE_PATH_RE.match(relative_path):
        raise SalvageConfigError(f"unsafe relative path {relative_path!r}")
    if not _DB_EXPORT_PATH_RE.match(relative_path):
        raise SalvageConfigError(f"relative path {relative_path!r} does not match db-export pattern")


def _paths_for_selector(
    archive_root: Path, selector: Mapping[str, Any]
) -> tuple[str, Path, Path]:
    table = selector["table"]
    lane = _TABLE_TO_LANE[table]
    identity = _identity_of(selector)
    _refuse_unsafe_identity(identity)
    relative = f"db-export/{lane}/{identity}/data.csv.zst"
    _refuse_unsafe_relative_path(relative)
    object_path = archive_root / relative
    manifest_path = archive_root / f"db-export/{lane}/{identity}/manifest.json"
    return relative, object_path, manifest_path


# ---------------------------------------------------------------------------
# Idempotency + manifest construction
# ---------------------------------------------------------------------------


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of_file(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            digest = hashlib.sha256()
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _load_existing_manifest(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _existing_object_verifies(
    object_path: Path,
    manifest_path: Path,
    selector: Mapping[str, Any],
    manifest_schema: Mapping[str, Any],
    row_count_fn: Callable[[], int],
) -> tuple[bool, int | None, str | None, int | None]:
    """Return (verified, exported_row_count, sha256, size_bytes) if object exists and matches."""
    manifest = _load_existing_manifest(manifest_path)
    if manifest is None:
        return False, None, None, None
    try:
        jsonschema.validate(manifest, manifest_schema)
    except jsonschema.ValidationError:
        return False, None, None, None
    exports = manifest.get("exports") or []
    if len(exports) != 1:
        return False, None, None, None
    export = exports[0]
    if export.get("selector") != selector:
        return False, None, None, None
    manifest_sha = export.get("object", {}).get("sha256")
    manifest_size = export.get("object", {}).get("size_bytes")
    exported_row_count = export.get("exported_row_count")
    if not isinstance(exported_row_count, int):
        return False, None, None, None
    actual_sha = _sha256_of_file(object_path)
    if actual_sha is None or actual_sha != manifest_sha:
        return False, None, None, None
    try:
        actual_size = object_path.stat().st_size
    except OSError:
        return False, None, None, None
    if actual_size != manifest_size:
        return False, None, None, None
    try:
        db_row_count = row_count_fn()
    except Exception:
        return False, None, None, None
    if db_row_count != exported_row_count:
        return False, None, None, None
    return True, exported_row_count, actual_sha, actual_size


def _build_manifest(
    *,
    selector: Mapping[str, Any],
    exported_row_count: int,
    columns: Sequence[str],
    relative_path: str,
    sha256_hex: str,
    size_bytes: int,
    generated_at: str,
    source_database: str,
    source_instance_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": "db-export",
        "generated_at": generated_at,
        "source_database": {
            "database": source_database,
            "instance_id": source_instance_id,
        },
        "exports": [
            {
                "selector": dict(selector),
                "exported_row_count": exported_row_count,
                "columns": list(columns),
                "object": {
                    "path": relative_path,
                    "sha256": sha256_hex,
                    "size_bytes": size_bytes,
                },
            }
        ],
    }


def _source_database_from_dsn(dsn: str) -> str:
    try:
        parts = urlsplit(dsn)
    except Exception:
        return ""
    path = parts.path or ""
    if path.startswith("/"):
        path = path[1:]
    return path or ""


# ---------------------------------------------------------------------------
# Runner core
# ---------------------------------------------------------------------------


def _emit_stderr_diagnostic(outcome: str, reason: str, dsn: str | None = None) -> None:
    payload: dict[str, Any] = {"status": "failed", "outcome": outcome, "reason": reason}
    if dsn is not None:
        payload["dsn"] = _mask_dsn(dsn)
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _blank_totals() -> dict[str, int]:
    return {
        "exported": 0,
        "skipped_verified": 0,
        "skipped_dry_run": 0,
        "error": 0,
        "row_count": 0,
        "compressed_bytes": 0,
    }


def _publish_object_and_manifest(
    *,
    object_path: Path,
    manifest_path: Path,
    csv_bytes: bytes,
    compressed_bytes: bytes,
    manifest: Mapping[str, Any],
    manifest_schema: Mapping[str, Any],
    archive_root: Path,
) -> None:
    # Schema-validate the manifest BEFORE we write anything. If it fails,
    # neither the object nor the manifest hit disk.
    jsonschema.validate(manifest, manifest_schema)
    ensure_directory_no_follow(object_path.parent, containment_root=archive_root)
    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes_no_follow(
        object_path, compressed_bytes, mode=0o600, require_durable_replace=True
    )
    atomic_write_bytes_no_follow(
        manifest_path, payload, mode=0o600, require_durable_replace=True
    )
    # csv_bytes is retained by the caller for accounting; not written.
    del csv_bytes


def build_receipt(
    config: SalvageConfig,
    *,
    now_utc: datetime,
    input_receipt: Mapping[str, Any],
    check_write_privileges: CheckWritePrivileges,
    fetch_row_count: FetchRowCount,
    perform_copy_export: PerformCopyExport,
    compress_bytes: CompressBytes,
) -> dict[str, Any]:
    """Perform selector export (or dry-run classification) and return receipt."""
    # Fail closed on writable role BEFORE any selector loop.
    role_reason = check_write_privileges(config.database_url)
    if role_reason is not None:
        raise SalvageRoleError(role_reason)

    selectors = list(input_receipt.get("salvage_selectors") or [])[: config.per_tick_bound]
    manifest_schema = _load_manifest_schema()
    totals = _blank_totals()
    descriptors: list[dict[str, Any]] = []
    any_errors = False
    any_success = False
    source_database = _source_database_from_dsn(config.database_url)
    generated_at = _iso(now_utc)

    for selector in selectors:
        table = selector.get("table")
        if table not in _TABLE_TO_LANE:
            descriptor = {
                "selector": dict(selector),
                "state": "error",
                "exported_row_count": None,
                "object": None,
                "error": f"selector table {table!r} is not a known salvage lane",
            }
            totals["error"] += 1
            any_errors = True
            descriptors.append(descriptor)
            continue
        try:
            relative_path, object_path, manifest_path = _paths_for_selector(
                config.archive_root, selector
            )
        except SalvageConfigError as error:
            descriptor = {
                "selector": dict(selector),
                "state": "error",
                "exported_row_count": None,
                "object": None,
                "error": str(error),
            }
            totals["error"] += 1
            any_errors = True
            descriptors.append(descriptor)
            continue

        columns = _TABLE_TO_COLUMNS[table]

        # Idempotency probe. If the existing pair verifies against the DB row
        # count, skip regardless of mode (dry-run and enforce both count this
        # as ``clean``).
        def _row_count_probe() -> int:
            return int(
                fetch_row_count(
                    config.database_url, table, selector, config.statement_timeout_ms
                )
            )

        verified, existing_row_count, existing_sha, existing_size = (
            _existing_object_verifies(
                object_path, manifest_path, selector, manifest_schema, _row_count_probe
            )
        )
        if verified:
            descriptors.append(
                {
                    "selector": dict(selector),
                    "state": "skipped_verified",
                    "exported_row_count": existing_row_count,
                    "object": {
                        "path": relative_path,
                        "sha256": existing_sha,
                        "size_bytes": existing_size,
                    },
                    "error": None,
                }
            )
            totals["skipped_verified"] += 1
            any_success = True
            continue

        if not config.enforce:
            descriptors.append(
                {
                    "selector": dict(selector),
                    "state": "skipped_dry_run",
                    "exported_row_count": None,
                    "object": {"path": relative_path, "sha256": None, "size_bytes": None},
                    "error": None,
                }
            )
            totals["skipped_dry_run"] += 1
            any_success = True
            continue

        # Enforce path: run the COPY, compress, hash, publish.
        # exported_row_count is derived from csv_bytes (newlines minus header)
        # so it always agrees with the shipped object; a second connection
        # SELECT COUNT(*) would sit in its own MVCC snapshot and can disagree
        # under concurrent writes that the display_ro role cannot itself
        # prevent (cand-A).
        try:
            csv_bytes = perform_copy_export(
                config.database_url, table, columns, selector, config.statement_timeout_ms
            )
            csv_size = len(csv_bytes)
            if csv_size > config.max_selector_bytes:
                raise SalvageOversizeError(
                    f"selector CSV size {csv_size} exceeds cap {config.max_selector_bytes} "
                    f"(NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES)"
                )
            db_row_count = _count_csv_rows(csv_bytes)
            compressed = compress_bytes(csv_bytes, config.zstd_level, config.zstd_path)
            sha_hex = _sha256_of(compressed)
            manifest = _build_manifest(
                selector=selector,
                exported_row_count=db_row_count,
                columns=columns,
                relative_path=relative_path,
                sha256_hex=sha_hex,
                size_bytes=len(compressed),
                generated_at=generated_at,
                source_database=source_database,
                source_instance_id=config.source_instance_id,
            )
            _publish_object_and_manifest(
                object_path=object_path,
                manifest_path=manifest_path,
                csv_bytes=csv_bytes,
                compressed_bytes=compressed,
                manifest=manifest,
                manifest_schema=manifest_schema,
                archive_root=config.archive_root,
            )
        except MemoryError as error:
            # MemoryError must NOT be swallowed by the broad Exception arm
            # below — an OOM inside the two-stage buffered COPY / compress
            # path is a resource-envelope failure, not a per-selector data
            # problem. Record a distinct label so downstream automation can
            # escalate (cand-C).
            descriptors.append(
                {
                    "selector": dict(selector),
                    "state": "error",
                    "exported_row_count": None,
                    "object": None,
                    "error": (
                        "out-of-memory during buffered COPY/compress; "
                        "shrink NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES "
                        f"or scope selector more tightly ({type(error).__name__})"
                    ),
                }
            )
            totals["error"] += 1
            any_errors = True
            continue
        except SalvageOversizeError as error:
            descriptors.append(
                {
                    "selector": dict(selector),
                    "state": "error",
                    "exported_row_count": None,
                    "object": None,
                    "error": str(error),
                }
            )
            totals["error"] += 1
            any_errors = True
            continue
        except (SafeFilesystemError, jsonschema.ValidationError, subprocess.CalledProcessError, Exception) as error:
            # Per-selector failure isolation. The failing selector records
            # an ``error`` state; the loop continues.
            descriptors.append(
                {
                    "selector": dict(selector),
                    "state": "error",
                    "exported_row_count": None,
                    "object": None,
                    "error": _mask_dsn_in_message(str(error), config.database_url),
                }
            )
            totals["error"] += 1
            any_errors = True
            continue

        descriptors.append(
            {
                "selector": dict(selector),
                "state": "exported",
                "exported_row_count": db_row_count,
                "object": {
                    "path": relative_path,
                    "sha256": sha_hex,
                    "size_bytes": len(compressed),
                },
                "error": None,
            }
        )
        totals["exported"] += 1
        totals["row_count"] += db_row_count
        totals["compressed_bytes"] += len(compressed)
        any_success = True

    if any_errors and any_success:
        outcome = "partial"
    elif any_errors and not any_success:
        # All selectors failed — distinct from "some succeeded, some
        # failed" so operators can see the difference at a glance
        # (cand-I).
        outcome = "all_failed"
    else:
        outcome = "clean"

    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": generated_at,
        "mode": config.mode,
        "outcome": outcome,
        "source_database": {
            "database": source_database,
            "instance_id": config.source_instance_id,
        },
        "receipt_input_path": str(config.receipt_input_path),
        "selected": descriptors,
        "per_selector_totals": totals,
    }


_LIBPQ_PASSWORD_KEYWORD_RE = re.compile(
    r"(?i)(?:(?<=\s)|(?<=^))password\s*=\s*\S+"
)


def _mask_dsn_in_message(message: str, dsn: str) -> str:
    """Scrub every plausible echo of the DSN's password from ``message``.

    Covers:
    - Verbatim DSN substring.
    - URL-encoded password (verbatim, still-quoted).
    - URL-decoded password (some client libraries echo the decoded form).
    - libpq keyword-form password (``... password=<literal> ...``) — a
      distinct DSN shape callers may compose independently. Only scrubs the
      value token, never the ``password=`` keyword itself.

    Hostname and username are intentionally left alone (they carry
    diagnostic value and are not secrets).
    """
    if dsn in message:
        message = message.replace(dsn, _mask_dsn(dsn))
    try:
        parts = urlsplit(dsn)
    except Exception:
        parts = None
    if parts is not None and parts.password:
        password_raw = parts.password
        if password_raw in message:
            message = message.replace(password_raw, "***")
        try:
            password_decoded = unquote(password_raw)
        except Exception:
            password_decoded = password_raw
        if (
            password_decoded
            and password_decoded != password_raw
            and password_decoded in message
        ):
            message = message.replace(password_decoded, "***")
    # libpq keyword form: even if the runner never composes such a DSN,
    # error messages produced by the DB driver may echo a caller-provided
    # DSN in that shape. Scrub the value defensively.
    message = _LIBPQ_PASSWORD_KEYWORD_RE.sub("password=***", message)
    return message


def publish_receipt(config: SalvageConfig, receipt: Mapping[str, Any]) -> None:
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes_no_follow(
        config.receipt_output_path, payload, mode=0o600, require_durable_replace=True
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    now_utc: datetime | None = None,
    check_write_privileges: CheckWritePrivileges | None = None,
    fetch_row_count: FetchRowCount | None = None,
    perform_copy_export: PerformCopyExport | None = None,
    compress_bytes: CompressBytes | None = None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code or 1)
    try:
        config = config_from_args(args)
    except SalvageConfigError as error:
        _emit_stderr_diagnostic(getattr(error, "outcome", "refused_config"), str(error))
        return 1

    try:
        input_receipt = _load_input_receipt(config.receipt_input_path)
    except SalvageConfigError as error:
        _emit_stderr_diagnostic("refused_config", str(error), dsn=config.database_url)
        return 1

    now = now_utc or datetime.now(UTC)

    try:
        lock_fd = acquire_lock(config.lock_path)
    except SalvageConfigError as error:
        _emit_stderr_diagnostic("refused_config", str(error), dsn=config.database_url)
        return 1
    if lock_fd is None:
        _emit_stderr_diagnostic("refused_lock", "lock-contended", dsn=config.database_url)
        return 1

    try:
        try:
            receipt = build_receipt(
                config,
                now_utc=now,
                input_receipt=input_receipt,
                check_write_privileges=check_write_privileges or _default_check_write_privileges,
                fetch_row_count=fetch_row_count or _default_fetch_row_count,
                perform_copy_export=perform_copy_export or _default_perform_copy_export,
                compress_bytes=compress_bytes or _default_compress_bytes,
            )
        except SalvageRoleError as error:
            _emit_stderr_diagnostic("refused_role", str(error), dsn=config.database_url)
            return 1
        except SalvageConfigError as error:
            _emit_stderr_diagnostic("refused_config", str(error), dsn=config.database_url)
            return 1
        except SafeFilesystemError as error:
            _emit_stderr_diagnostic(
                "partial",
                _mask_dsn_in_message(f"filesystem error: {error}", config.database_url),
                dsn=config.database_url,
            )
            return 1
        except Exception as error:  # pragma: no cover — defensive
            _emit_stderr_diagnostic(
                "partial",
                _mask_dsn_in_message(f"salvage runner error: {error}", config.database_url),
                dsn=config.database_url,
            )
            return 1
        try:
            publish_receipt(config, receipt)
        except SafeFilesystemError as error:
            _emit_stderr_diagnostic(
                "partial",
                _mask_dsn_in_message(f"receipt publication error: {error}", config.database_url),
                dsn=config.database_url,
            )
            return 1
        return 0 if receipt["outcome"] == "clean" else 1
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
