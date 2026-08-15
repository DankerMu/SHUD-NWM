#!/usr/bin/env python3
"""Bounded, receipted identity-column backfill runner for node-27 (issue #1339).

Fills the seven normalized columns that migration 000050 added to
``hydro.river_timeseries`` (``run_key``, ``river_network_version_key``,
``basin_version_key``, ``river_segment_key``, ``variable_e``, ``unit_e``,
``quality_flag_e``) from the four authority tables and the three native enums.

Dry-run by default; ``--enforce`` mutates; ``--probe`` runs one real timed
batch and rolls it back. Emits a
``schemas/river_identity_backfill_receipt.schema.json``-conformant receipt via
the shared no-follow atomic write helper, and holds an ``fcntl`` flock for
single-instance exclusion (house style
``scripts/node27_timeseries_compression.py``).

Two chunk-level invariants, neither of them negotiable:

* **Never UPDATE a compressed chunk.** TimescaleDB 2.10 supports no DML at all
  against compressed storage. Detection is delegated to the shared write guard
  (``packages.common.timescale_write_guard.assert_chunk_uncompressed``) rather
  than reimplemented here — design D5 of #851 forbids per-path guard copies.
  Recovery is the existing decompression-replay + compression runners, driven
  from the runbook; this runner deliberately embeds no decompression.
* **Never UPDATE the active chunk** (unless ``--final-sweep``). The ingest
  writer is doing DELETE + INSERT inside it right now; backfilling it would
  contend for row locks with ingest and would never converge, because ingest
  keeps producing new NULL rows. "Active" is not a criterion this runner
  invents: it is the #1069 compression lane's terminal/lag criterion
  (``range_end < now - lag``), reading the same lag configuration so the two
  lanes cannot drift apart.

Batches are **ctid block ranges**, not ``ctid IN (SELECT ... LIMIT n)``. With
no index on the sentinel column, the LIMIT-subquery shape rescans the
already-backfilled prefix from page 0 on every batch — O(n^2) over a 460M-row
table, and combined with the duration wall it converges to a permanent halt
while making early probe samples look deceptively fast. Block ranges scan each
page exactly once and make the halving retry meaningful: halving the range
halves the work.

Design reference: openspec/changes/river-identity-normalization-backfill/design.md
decisions D3, D6, D7.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from packages.common.evidence_io import (
    BoundedEvidenceError,
    assert_paths_disjoint,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
)
from packages.common.timescale_write_guard import (
    CompressedChunkGuardError,
    CompressedChunkWriteError,
    assert_chunk_uncompressed,
)

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "node27-river-identity-backfill/1"

HYPERTABLE_SCHEMA = "hydro"
HYPERTABLE_NAME = "river_timeseries"
HYPERTABLE_KEY = f"{HYPERTABLE_SCHEMA}.{HYPERTABLE_NAME}"

SENTINEL_COLUMN = "run_key"
NORMALIZED_COLUMNS: tuple[str, ...] = (
    "run_key",
    "river_network_version_key",
    "basin_version_key",
    "river_segment_key",
    "variable_e",
    "unit_e",
    "quality_flag_e",
)

_CONNECT_TIMEOUT_SECONDS = 10
_CATALOG_TIMEOUT_MS = 60_000
_DEFAULT_BATCH_PAGES = 1_250  # ~100k rows at ~80 rows/page on this row width
_DEFAULT_DURATION_WALL_MS = 30_000
_DEFAULT_BATCH_SLEEP_MS = 250
_DEFAULT_MAX_BATCHES = 500
_DEFAULT_QUIESCENCE_SECONDS = 60
_MIN_DURATION_WALL_MS = 1_000
_MAX_CHUNKS = 10_000
_DISK_MOUNT = "/home"
_COMPRESSION_TIMER_UNIT = "nhms-node27-timeseries-compression.timer"

# Backfilling rewrites every row, so the heap roughly doubles until VACUUM
# reclaims the dead tuples, and the indexes churn alongside. One times the
# chunk's current total size is the conservative planning figure the runbook
# uses.
_BLOAT_FACTOR = 1.0


class BackfillConfigError(RuntimeError):
    """Fail-closed configuration parse error, raised before any DB call."""


class BackfillStop(RuntimeError):
    """Fail-closed halt with a receipt-recordable reason."""

    def __init__(self, stage: str, reason: str, **detail: Any) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.detail = {key: value for key, value in detail.items() if value is not None}


# ---------------------------------------------------------------------------
# SQL. Module-level so tests can assert on the exact text rather than on a
# paraphrase, and so a silent drift between the UPDATE and the candidate count
# (which MUST select the same rows) is visible in one place.
# ---------------------------------------------------------------------------

# The candidate count and the UPDATE share the identical range + sentinel
# predicate. If they ever diverge, the shortfall detector below turns into a
# false-alarm generator, so they are written adjacent and asserted equal-shaped
# by tests.
_CANDIDATE_COUNT_SQL = """
SELECT count(*)
FROM ONLY {chunk} AS t
WHERE t.ctid >= %(first_page_tid)s::tid
  AND t.ctid < %(last_page_tid)s::tid
  AND t.run_key IS NULL
"""

# Every join predicate is written out; nothing is left to a USING clause or to
# a natural join. The enum legs join against the type's own label set rather
# than casting `t.variable::hydro.river_variable` directly: a direct cast on an
# unmappable value raises and destroys the whole batch, whereas an unmatched
# label leg simply leaves the row alone, where the shortfall detector finds and
# reports it.
_BATCH_UPDATE_SQL = """
UPDATE ONLY {chunk} AS t
SET run_key = hr.run_key,
    river_network_version_key = rnv.river_network_version_key,
    basin_version_key = bv.basin_version_key,
    river_segment_key = rs.river_segment_key,
    variable_e = ve.label,
    unit_e = ue.label,
    quality_flag_e = qe.label
FROM hydro.hydro_run AS hr,
     core.river_network_version AS rnv,
     core.basin_version AS bv,
     core.river_segment AS rs,
     (SELECT unnest(enum_range(NULL::hydro.river_variable)) AS label) AS ve,
     (SELECT unnest(enum_range(NULL::hydro.river_unit)) AS label) AS ue,
     (SELECT unnest(enum_range(NULL::hydro.river_quality_flag)) AS label) AS qe
WHERE t.ctid >= %(first_page_tid)s::tid
  AND t.ctid < %(last_page_tid)s::tid
  AND t.run_key IS NULL
  AND hr.run_id = t.run_id
  AND rnv.river_network_version_id = t.river_network_version_id
  AND bv.basin_version_id = t.basin_version_id
  AND rs.river_segment_id = t.river_segment_id
  AND rs.river_network_version_id = t.river_network_version_id
  AND ve.label::text = t.variable
  AND ue.label::text = t.unit
  AND qe.label::text = t.quality_flag
"""

# Splits a shortfall into its two distinguishable causes. Both are counted over
# the same range, and a row can be in both (a row with an unknown run_id AND an
# unmappable unit), so the two counters are not required to sum to the
# shortfall — they are diagnostics that name the cause, not a partition.
_UNMATCHED_COUNT_SQL = """
SELECT count(*)
FROM ONLY {chunk} AS t
WHERE t.ctid >= %(first_page_tid)s::tid
  AND t.ctid < %(last_page_tid)s::tid
  AND t.run_key IS NULL
  AND (
       NOT EXISTS (SELECT 1 FROM hydro.hydro_run AS hr WHERE hr.run_id = t.run_id)
    OR NOT EXISTS (SELECT 1 FROM core.river_network_version AS rnv
                    WHERE rnv.river_network_version_id = t.river_network_version_id)
    OR NOT EXISTS (SELECT 1 FROM core.basin_version AS bv
                    WHERE bv.basin_version_id = t.basin_version_id)
    OR NOT EXISTS (SELECT 1 FROM core.river_segment AS rs
                    WHERE rs.river_segment_id = t.river_segment_id
                      AND rs.river_network_version_id = t.river_network_version_id)
  )
"""

_UNMAPPABLE_COUNT_SQL = """
SELECT count(*)
FROM ONLY {chunk} AS t
WHERE t.ctid >= %(first_page_tid)s::tid
  AND t.ctid < %(last_page_tid)s::tid
  AND t.run_key IS NULL
  AND (
       t.variable NOT IN (SELECT unnest(enum_range(NULL::hydro.river_variable))::text)
    OR t.unit NOT IN (SELECT unnest(enum_range(NULL::hydro.river_unit))::text)
    OR t.quality_flag NOT IN (SELECT unnest(enum_range(NULL::hydro.river_quality_flag))::text)
  )
"""

# Same predicate as hydro.verify_river_identity_normalization()'s
# equality-audit leg, scoped to one chunk. Detects rows the ingest writer's
# ON CONFLICT DO UPDATE branch refreshed on the text side after this runner had
# already resolved the surrogate side.
_EQUALITY_AUDIT_SQL = """
SELECT count(*)
FROM ONLY {chunk} AS t
WHERE t.run_key IS NOT NULL
  AND (
       t.variable_e::text IS DISTINCT FROM t.variable
    OR t.unit_e::text IS DISTINCT FROM t.unit
    OR t.quality_flag_e::text IS DISTINCT FROM t.quality_flag
    OR t.basin_version_key IS DISTINCT FROM (
         SELECT bv.basin_version_key FROM core.basin_version AS bv
         WHERE bv.basin_version_id = t.basin_version_id)
  )
"""

_PENDING_COUNT_SQL = """
SELECT count(*) FROM ONLY {chunk} AS t WHERE t.run_key IS NULL
"""

# NOTE: no ``format('%s.%s', ...)`` anywhere below. These statements are
# executed with a psycopg2 parameter mapping, and a bare ``%s`` inside the SQL
# is a placeholder to psycopg2 long before PostgreSQL ever sees it — mixing the
# two raises TypeError at bind time. ``quote_ident`` builds the same qualified
# name with no percent sign in sight.
_CHUNK_INVENTORY_SQL = """
SELECT c.chunk_schema,
       c.chunk_name,
       c.range_start,
       c.range_end,
       c.is_compressed,
       pg_total_relation_size(cls.oid) AS total_bytes,
       COALESCE(cls.relpages, 0) AS relpages,
       GREATEST(
         COALESCE(cls.relpages, 0),
         (pg_relation_size(cls.oid) / current_setting('block_size')::bigint)
       ) AS pages_planned,
       GREATEST(COALESCE(cls.reltuples, 0), 0)::bigint AS approx_rows
FROM timescaledb_information.chunks AS c
JOIN pg_class AS cls
  ON cls.oid = (quote_ident(c.chunk_schema) || '.' || quote_ident(c.chunk_name))::regclass
WHERE c.hypertable_schema = %(hypertable_schema)s
  AND c.hypertable_name = %(hypertable_name)s
ORDER BY c.range_start
"""

# Write-activity sampler for the --final-sweep quiescence assertion. Counter
# deltas over an observation window are the direct measurement of "is anything
# writing"; a max(created_at) probe would be a full scan of the busiest chunk
# in the database and would still only see committed inserts.
_WRITE_ACTIVITY_SQL = """
SELECT COALESCE(n_tup_ins, 0) + COALESCE(n_tup_upd, 0) + COALESCE(n_tup_del, 0)
FROM pg_stat_all_tables
WHERE relid = (quote_ident(%(chunk_schema)s) || '.' || quote_ident(%(chunk_name)s))::regclass
"""

_LOCK_WAIT_SQL = """
SELECT l.locktype, l.mode, l.granted, COALESCE(c.relname, '') AS relname
FROM pg_locks AS l
LEFT JOIN pg_class AS c ON c.oid = l.relation
WHERE l.pid = pg_backend_pid()
  AND l.locktype IN ('relation', 'tuple', 'transactionid')
ORDER BY l.granted, l.locktype, relname
"""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillConfig:
    database_url: str
    receipt_path: Path
    lock_path: Path
    enforce: bool
    probe: bool
    final_sweep: bool
    batch_pages: int
    duration_wall_ms: int
    batch_sleep_ms: int
    max_batches: int
    lag_seconds: int
    quiescence_seconds: int

    @property
    def mode(self) -> str:
        if self.enforce:
            return "enforce"
        return "probe" if self.probe else "dry-run"


@dataclass(frozen=True)
class ChunkRow:
    chunk_schema: str
    chunk_name: str
    range_start: datetime
    range_end: datetime
    is_compressed: bool
    total_bytes: int
    relpages: int
    pages_planned: int
    approx_rows: int

    @property
    def qualified(self) -> str:
        return f'"{self.chunk_schema}"."{self.chunk_name}"'

    @property
    def key(self) -> str:
        return f"{self.chunk_schema}.{self.chunk_name}"


@dataclass
class BatchOutcome:
    first_page: int
    last_page: int
    candidate_rows: int
    updated_rows: int
    duration_ms: float
    unmatched_rows: int = 0
    unmappable_rows: int = 0
    lock_waits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def shortfall(self) -> int:
        return self.candidate_rows - self.updated_rows


class BatchDurationExceeded(RuntimeError):
    """The server cancelled the batch because it exceeded the duration wall."""


# ---------------------------------------------------------------------------
# Pure helpers (the unit-test surface)
# ---------------------------------------------------------------------------


def plan_batches(total_pages: int, batch_pages: int, *, first_page: int = 0) -> list[tuple[int, int]]:
    """Split ``[first_page, total_pages)`` into half-open ctid block ranges.

    Returns ``[(start, end), ...]`` where ``end`` is exclusive, matching the
    ``ctid >= '(start,0)' AND ctid < '(end,0)'`` predicate. An empty list means
    there is nothing left to scan — either the chunk is empty or the persisted
    cursor already passed the end.
    """
    if batch_pages < 1:
        raise BackfillConfigError("batch_pages must be >= 1")
    if total_pages <= first_page:
        return []
    return [
        (page, min(page + batch_pages, total_pages))
        for page in range(first_page, total_pages, batch_pages)
    ]


def is_active_chunk(chunk: ChunkRow, *, now_utc: datetime, lag_seconds: int) -> bool:
    """Active == NOT terminal, using the #1069 compression lane's criterion.

    A chunk is terminal once its ``range_end`` is further in the past than the
    configured lag (one chunk width by default). Anything else is the window
    ingest may still be writing into.
    """
    return chunk.range_end >= now_utc - timedelta(seconds=lag_seconds)


def classify_chunks(
    chunks: Sequence[ChunkRow],
    *,
    now_utc: datetime,
    lag_seconds: int,
    final_sweep: bool,
) -> tuple[list[ChunkRow], list[tuple[ChunkRow, str]]]:
    """Partition into (eligible, [(skipped, reason)]).

    Compressed always loses, including under ``--final-sweep``: the flag
    relaxes the active-chunk rule only. No flag makes DML against compressed
    storage legal on TimescaleDB 2.10.
    """
    eligible: list[ChunkRow] = []
    skipped: list[tuple[ChunkRow, str]] = []
    for chunk in chunks:
        if chunk.is_compressed:
            skipped.append((chunk, "compressed"))
            continue
        if is_active_chunk(chunk, now_utc=now_utc, lag_seconds=lag_seconds) and not final_sweep:
            skipped.append((chunk, "active"))
            continue
        eligible.append(chunk)
    return eligible, skipped


def estimate_bloat_bytes(chunks: Sequence[ChunkRow]) -> int:
    return int(sum(chunk.total_bytes for chunk in chunks) * _BLOAT_FACTOR)


def _page_tid(page: int) -> str:
    return f"({page},0)"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


def _parse_int(raw: str | None, *, name: str, default: int, minimum: int) -> int:
    if raw is None or raw == "":
        return default
    stripped = raw.strip()
    if stripped != raw:
        raise BackfillConfigError(f"{name} must not contain leading/trailing whitespace")
    try:
        value = int(stripped)
    except ValueError as error:
        raise BackfillConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise BackfillConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _current_head_sha() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise BackfillConfigError("cannot bind receipt to repository HEAD") from error
    head_sha = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise BackfillConfigError("cannot bind receipt to repository HEAD")
    return head_sha


def config_from_args(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> BackfillConfig:
    """Strict env + CLI parse. No truthiness fallback; fails closed on bad shape."""
    env = os.environ if env is None else env

    receipt_raw = args.receipt_path or env.get("NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH")
    if not receipt_raw:
        raise BackfillConfigError(
            "receipt path must be set via --receipt-path or "
            "NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH"
        )
    receipt_path = Path(str(receipt_raw))
    if not receipt_path.is_absolute():
        raise BackfillConfigError("receipt path must be absolute")

    lock_raw = args.lock_path or env.get("NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH")
    if not lock_raw:
        raise BackfillConfigError(
            "lock path must be set via --lock-path or NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH"
        )
    lock_path = Path(str(lock_raw))
    if not lock_path.is_absolute():
        raise BackfillConfigError("lock path must be absolute")
    try:
        assert_paths_disjoint(receipt_path, [lock_path], label="river identity backfill receipt")
    except BoundedEvidenceError as error:
        raise BackfillConfigError(str(error)) from error

    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise BackfillConfigError("DATABASE_URL must be set")

    # The active/terminal criterion is shared with the #1069 compression lane.
    # Falling back to that lane's variable is deliberate: two lanes disagreeing
    # about which chunk is active is exactly the drift that lets this runner
    # fight ingest for row locks.
    lag_raw = env.get("NODE27_RIVER_IDENTITY_BACKFILL_LAG_SECONDS") or env.get(
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS"
    )
    if not lag_raw:
        raise BackfillConfigError(
            "lag seconds must be set via NODE27_RIVER_IDENTITY_BACKFILL_LAG_SECONDS or "
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS (the #1069 compression lane's value, "
            "which this runner shares so the two cannot disagree about the active chunk)"
        )
    lag_seconds = _parse_int(lag_raw, name="lag seconds", default=0, minimum=1)

    if args.enforce and args.probe:
        raise BackfillConfigError("--enforce and --probe are mutually exclusive")

    return BackfillConfig(
        database_url=database_url,
        receipt_path=receipt_path,
        lock_path=lock_path,
        enforce=bool(args.enforce),
        probe=bool(args.probe),
        final_sweep=bool(args.final_sweep),
        batch_pages=_parse_int(
            env.get("NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES"),
            name="NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES",
            default=_DEFAULT_BATCH_PAGES,
            minimum=1,
        ),
        duration_wall_ms=_parse_int(
            env.get("NODE27_RIVER_IDENTITY_BACKFILL_DURATION_WALL_MS"),
            name="NODE27_RIVER_IDENTITY_BACKFILL_DURATION_WALL_MS",
            default=_DEFAULT_DURATION_WALL_MS,
            minimum=_MIN_DURATION_WALL_MS,
        ),
        batch_sleep_ms=_parse_int(
            env.get("NODE27_RIVER_IDENTITY_BACKFILL_BATCH_SLEEP_MS"),
            name="NODE27_RIVER_IDENTITY_BACKFILL_BATCH_SLEEP_MS",
            default=_DEFAULT_BATCH_SLEEP_MS,
            minimum=0,
        ),
        max_batches=_parse_int(
            env.get("NODE27_RIVER_IDENTITY_BACKFILL_MAX_BATCHES"),
            name="NODE27_RIVER_IDENTITY_BACKFILL_MAX_BATCHES",
            default=_DEFAULT_MAX_BATCHES,
            minimum=1,
        ),
        quiescence_seconds=_parse_int(
            env.get("NODE27_RIVER_IDENTITY_BACKFILL_QUIESCENCE_SECONDS"),
            name="NODE27_RIVER_IDENTITY_BACKFILL_QUIESCENCE_SECONDS",
            default=_DEFAULT_QUIESCENCE_SECONDS,
            minimum=1,
        ),
        lag_seconds=lag_seconds,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true", help="actually persist the backfill")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="run one real timed batch with lock observation, then ROLL BACK (persists nothing)",
    )
    parser.add_argument(
        "--final-sweep",
        action="store_true",
        help=(
            "also backfill the active chunk. Only legal once ingest is paused; the runner "
            "asserts write quiescence before touching it."
        ),
    )
    parser.add_argument("--receipt-path", type=str, default=None)
    parser.add_argument("--lock-path", type=str, default=None)
    return parser


def acquire_lock(path: Path) -> int | None:
    """Take a nonblocking flock on a mode-0600 lock file. Return None on contention."""
    if not path.is_absolute():
        raise BackfillConfigError("lock path must be absolute")
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
            raise BackfillConfigError("lock file must be a mode-0600 regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except BackfillConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise BackfillConfigError(f"cannot acquire lock file: {error}") from error
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Cursor-level operations. These take a cursor rather than a DSN so unit tests
# can drive them with a fake, which is how the #1069 runner's tests work.
# ---------------------------------------------------------------------------


def fetch_chunk_inventory(cursor: Any) -> list[ChunkRow]:
    cursor.execute(f"SET statement_timeout = {_CATALOG_TIMEOUT_MS}")
    cursor.execute(
        _CHUNK_INVENTORY_SQL,
        {"hypertable_schema": HYPERTABLE_SCHEMA, "hypertable_name": HYPERTABLE_NAME},
    )
    rows = cursor.fetchall()
    if len(rows) > _MAX_CHUNKS:
        raise BackfillConfigError("chunk inventory exceeds the bounded ceiling")
    return [_row_to_chunk(row) for row in rows]


def _row_to_chunk(row: Any) -> ChunkRow:
    values = row if isinstance(row, dict) else {
        key: row[index]
        for index, key in enumerate(
            (
                "chunk_schema",
                "chunk_name",
                "range_start",
                "range_end",
                "is_compressed",
                "total_bytes",
                "relpages",
                "pages_planned",
                "approx_rows",
            )
        )
    }
    return ChunkRow(
        chunk_schema=str(values["chunk_schema"]),
        chunk_name=str(values["chunk_name"]),
        range_start=_as_utc(values["range_start"]),
        range_end=_as_utc(values["range_end"]),
        is_compressed=bool(values["is_compressed"]),
        total_bytes=int(values["total_bytes"] or 0),
        relpages=int(values["relpages"] or 0),
        pages_planned=int(values["pages_planned"] or 0),
        approx_rows=int(values["approx_rows"] or 0),
    )


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _scalar(cursor: Any, sql: str, params: Mapping[str, Any] | None = None) -> int:
    cursor.execute(sql, params or {})
    row = cursor.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(next(iter(row.values())) or 0)
    return int(row[0] or 0)


def count_pending(cursor: Any, chunk: ChunkRow) -> int:
    return _scalar(cursor, _PENDING_COUNT_SQL.format(chunk=chunk.qualified))


def equality_audit(cursor: Any, chunk: ChunkRow) -> int:
    return _scalar(cursor, _EQUALITY_AUDIT_SQL.format(chunk=chunk.qualified))


def execute_batch(
    cursor: Any,
    chunk: ChunkRow,
    *,
    first_page: int,
    last_page: int,
    duration_wall_ms: int,
    collect_locks: bool = False,
) -> BatchOutcome:
    """Run one ctid-block batch inside the caller's transaction.

    The duration wall is enforced by the SERVER (``SET LOCAL
    statement_timeout``), not by a client-side stopwatch: a client timer cannot
    actually stop a statement that is already holding row locks, so a
    self-reported "wall" would be a comment rather than a bound. A cancellation
    surfaces as :class:`BatchDurationExceeded` for the caller's halving retry.

    Fail-closed on shortfall: if fewer rows were updated than the sentinel
    predicate matched, the difference is rows the four-way join could not
    resolve. An inner-join UPDATE reports no error for those — it simply leaves
    them NULL — so without this comparison they would stay NULL forever while
    every subsequent receipt reported a spurious non-zero pending count.
    """
    params = {
        "first_page_tid": _page_tid(first_page),
        "last_page_tid": _page_tid(last_page),
    }
    started = time.monotonic()
    try:
        cursor.execute(f"SET LOCAL statement_timeout = {int(duration_wall_ms)}")
        candidate_rows = _scalar(cursor, _CANDIDATE_COUNT_SQL.format(chunk=chunk.qualified), params)
        cursor.execute(_BATCH_UPDATE_SQL.format(chunk=chunk.qualified), params)
        updated_rows = int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)
    except Exception as error:
        if _is_query_cancelled(error):
            raise BatchDurationExceeded(
                f"batch {chunk.key} pages [{first_page},{last_page}) exceeded "
                f"{duration_wall_ms} ms"
            ) from error
        raise
    duration_ms = (time.monotonic() - started) * 1000.0

    outcome = BatchOutcome(
        first_page=first_page,
        last_page=last_page,
        candidate_rows=candidate_rows,
        updated_rows=updated_rows,
        duration_ms=duration_ms,
    )
    if outcome.shortfall > 0:
        outcome.unmatched_rows = _scalar(
            cursor, _UNMATCHED_COUNT_SQL.format(chunk=chunk.qualified), params
        )
        outcome.unmappable_rows = _scalar(
            cursor, _UNMAPPABLE_COUNT_SQL.format(chunk=chunk.qualified), params
        )
    if collect_locks:
        cursor.execute(_LOCK_WAIT_SQL)
        outcome.lock_waits = [_lock_row(row) for row in cursor.fetchall()]
    return outcome


def _lock_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        locktype, mode, granted, relname = (
            row["locktype"],
            row["mode"],
            row["granted"],
            row.get("relname"),
        )
    else:
        locktype, mode, granted, relname = row[0], row[1], row[2], row[3]
    return {
        "locktype": str(locktype),
        "mode": str(mode),
        "granted": bool(granted),
        "relation": str(relname) if relname else None,
    }


def _is_query_cancelled(error: Exception) -> bool:
    """True when PostgreSQL cancelled the statement (SQLSTATE 57014)."""
    pgcode = getattr(error, "pgcode", None)
    if pgcode == "57014":
        return True
    return "canceling statement due to statement timeout" in str(error).lower()


def sample_write_activity(cursor: Any, chunk: ChunkRow) -> int:
    return _scalar(
        cursor,
        _WRITE_ACTIVITY_SQL,
        {"chunk_schema": chunk.chunk_schema, "chunk_name": chunk.chunk_name},
    )


def assert_ingest_quiescent(
    cursor: Any,
    chunk: ChunkRow,
    *,
    quiescence_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Refuse ``--final-sweep`` unless the active chunk is receiving no writes.

    Samples the chunk's cumulative insert/update/delete counters, waits, and
    samples again. Any movement means ingest is still running, which is the one
    condition under which touching the active chunk is guaranteed to contend
    with the writer and never converge.
    """
    before = sample_write_activity(cursor, chunk)
    sleep(float(quiescence_seconds))
    after = sample_write_activity(cursor, chunk)
    if after != before:
        raise BackfillStop(
            "ingest_not_quiescent",
            (
                f"--final-sweep refused: {chunk.key} recorded {after - before} write(s) over a "
                f"{quiescence_seconds}s observation window; pause ingest before sweeping the "
                "active chunk"
            ),
            chunk_schema=chunk.chunk_schema,
            chunk_name=chunk.chunk_name,
        )


# ---------------------------------------------------------------------------
# Environment observation
# ---------------------------------------------------------------------------


def observe_compression_timer(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Record the compression timer's state (design D6).

    An enforce window that leaves the timer running is a real hazard: a 04:25
    UTC tick lands mid-backfill, compresses a chunk that still holds NULL rows,
    and the only way back is decompressing hundreds of GB. The runner reports
    the state rather than deciding for the operator, because masking a systemd
    unit is not this process's business.
    """
    state: dict[str, Any] = {
        "observed": False,
        "is_active": None,
        "is_enabled": None,
        "unavailable_reason": None,
    }
    try:
        active = run(
            ["systemctl", "--user", "is-active", _COMPRESSION_TIMER_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        enabled = run(
            ["systemctl", "--user", "is-enabled", _COMPRESSION_TIMER_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        state["unavailable_reason"] = f"systemctl unavailable ({type(error).__name__})"
        return state
    state["observed"] = True
    state["is_active"] = (active.stdout or "").strip() or None
    state["is_enabled"] = (enabled.stdout or "").strip() or None
    return state


def observe_disk(estimated_bloat_bytes: int, *, mount: str = _DISK_MOUNT) -> dict[str, Any]:
    try:
        stats = os.statvfs(mount)
        avail = int(stats.f_bavail * stats.f_frsize)
    except OSError:
        return {
            "mount": mount,
            "avail_bytes": None,
            "estimated_bloat_bytes": estimated_bloat_bytes,
            "headroom_sufficient": None,
        }
    return {
        "mount": mount,
        "avail_bytes": avail,
        "estimated_bloat_bytes": estimated_bloat_bytes,
        "headroom_sufficient": avail > estimated_bloat_bytes,
    }


# ---------------------------------------------------------------------------
# Runner core
# ---------------------------------------------------------------------------


def _blank_totals() -> dict[str, Any]:
    return {
        "candidate_rows": 0,
        "updated_rows": 0,
        "pending_rows": 0,
        "batches_run": 0,
        "batches_halved": 0,
        "unmatched_rows": 0,
        "unmappable_rows": 0,
        "equality_audit_divergent": 0,
        "chunks_skipped_compressed": 0,
        "chunks_skipped_active": 0,
    }


def _chunk_descriptor(chunk: ChunkRow, state: str, **extra: Any) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "chunk_schema": chunk.chunk_schema,
        "chunk_name": chunk.chunk_name,
        "range_start": _iso(chunk.range_start),
        "range_end": _iso(chunk.range_end),
        "is_compressed": chunk.is_compressed,
        "state": state,
        "total_bytes": chunk.total_bytes,
        "relpages": chunk.relpages,
        "pages_planned": chunk.pages_planned,
        "approx_rows": chunk.approx_rows,
        "pending_rows": 0,
        "batches_planned": 0,
        "estimated_bloat_bytes": int(chunk.total_bytes * _BLOAT_FACTOR),
    }
    descriptor.update(extra)
    return descriptor


def process_chunk(
    connection: Any,
    chunk: ChunkRow,
    config: BackfillConfig,
    *,
    cursor_budget: list[int],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Plan and (when enforcing) execute the backfill of one uncompressed chunk.

    Every batch is its own transaction. That is what makes interruption safe:
    a committed batch stays committed, and a killed process leaves no partial
    batch behind. The persisted block cursor is an optimisation on top of that,
    never the correctness mechanism — the ``run_key IS NULL`` sentinel is.
    """
    with connection.cursor() as cursor:
        # Chunk-level compressed assertion via the SHARED guard. Belt and
        # braces against the inventory being a moment stale: the compression
        # timer may have fired between discovery and now.
        assert_chunk_uncompressed(
            cursor,
            hypertable_schema=HYPERTABLE_SCHEMA,
            hypertable_name=HYPERTABLE_NAME,
            chunk_schema=chunk.chunk_schema,
            chunk_name=chunk.chunk_name,
        )
        pending = count_pending(cursor, chunk)
        divergent = equality_audit(cursor, chunk)
    connection.rollback()

    batches = plan_batches(chunk.pages_planned, config.batch_pages)
    descriptor = _chunk_descriptor(
        chunk,
        "planned",
        pending_rows=pending,
        batches_planned=len(batches),
        equality_audit_divergent=divergent,
        batches_run=0,
        batches_halved=0,
        candidate_rows=0,
        updated_rows=0,
        unmatched_rows=0,
        unmappable_rows=0,
        next_page=None,
        duration_ms=None,
    )

    if pending == 0:
        descriptor["state"] = "skipped_no_pending"
        descriptor["skip_reason"] = "no rows with a NULL sentinel"
        return descriptor

    if config.probe:
        descriptor["probe"] = _run_probe(connection, chunk, config, batches)
        return descriptor

    if not config.enforce:
        return descriptor

    return _run_enforce(
        connection, chunk, config, batches, descriptor, cursor_budget=cursor_budget, sleep=sleep
    )


def _run_probe(
    connection: Any, chunk: ChunkRow, config: BackfillConfig, batches: list[tuple[int, int]]
) -> dict[str, Any]:
    """One real UPDATE, timed, lock-observed, then rolled back. Zero persistence.

    The rollback is in a ``finally`` so no exception path can leak a committed
    probe. The probe is bound by the same duration wall as a real batch, so it
    cannot become the one unbounded statement in the runner.
    """
    if not batches:
        return {
            "rolled_back": True,
            "candidate_rows": 0,
            "updated_rows": 0,
            "duration_ms": 0.0,
        }
    first_page, last_page = batches[0]
    try:
        with connection.cursor() as cursor:
            outcome = execute_batch(
                cursor,
                chunk,
                first_page=first_page,
                last_page=last_page,
                duration_wall_ms=config.duration_wall_ms,
                collect_locks=True,
            )
        return {
            "rolled_back": True,
            "candidate_rows": outcome.candidate_rows,
            "updated_rows": outcome.updated_rows,
            "duration_ms": round(outcome.duration_ms, 3),
            "first_page": first_page,
            "last_page": last_page,
            "lock_waits": outcome.lock_waits,
        }
    finally:
        connection.rollback()


def _run_enforce(
    connection: Any,
    chunk: ChunkRow,
    config: BackfillConfig,
    batches: list[tuple[int, int]],
    descriptor: dict[str, Any],
    *,
    cursor_budget: list[int],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    started = time.monotonic()
    pending_ranges = list(batches)
    while pending_ranges:
        if cursor_budget[0] <= 0:
            descriptor["state"] = "deferred"
            descriptor["skip_reason"] = "per-invocation batch budget exhausted"
            break
        first_page, last_page = pending_ranges.pop(0)
        outcome, halved = _run_one_batch_with_retry(connection, chunk, config, first_page, last_page)
        cursor_budget[0] -= 1
        descriptor["batches_run"] += 1
        descriptor["batches_halved"] += halved
        descriptor["candidate_rows"] += outcome.candidate_rows
        descriptor["updated_rows"] += outcome.updated_rows
        descriptor["next_page"] = outcome.last_page

        if outcome.shortfall > 0:
            connection.rollback()
            descriptor["state"] = "stopped"
            descriptor["unmatched_rows"] += outcome.unmatched_rows
            descriptor["unmappable_rows"] += outcome.unmappable_rows
            descriptor["duration_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            raise BackfillStop(
                "shortfall",
                (
                    f"{chunk.key} pages [{outcome.first_page},{outcome.last_page}): "
                    f"{outcome.candidate_rows} sentinel candidate(s) but only "
                    f"{outcome.updated_rows} updated -- {outcome.unmatched_rows} unresolvable "
                    f"against the authority tables, {outcome.unmappable_rows} unmappable to an "
                    "enum label. Refusing to record this as progress."
                ),
                chunk_schema=chunk.chunk_schema,
                chunk_name=chunk.chunk_name,
                first_page=outcome.first_page,
                last_page=outcome.last_page,
                candidate_rows=outcome.candidate_rows,
                updated_rows=outcome.updated_rows,
                unmatched_rows=outcome.unmatched_rows,
                unmappable_rows=outcome.unmappable_rows,
            )

        connection.commit()
        if config.batch_sleep_ms:
            sleep(config.batch_sleep_ms / 1000.0)

    if descriptor["state"] == "planned":
        descriptor["state"] = "processed"
    descriptor["duration_ms"] = round((time.monotonic() - started) * 1000.0, 3)

    with connection.cursor() as cursor:
        descriptor["pending_rows"] = count_pending(cursor, chunk)
        descriptor["equality_audit_divergent"] = equality_audit(cursor, chunk)
    connection.rollback()
    return descriptor


def _run_one_batch_with_retry(
    connection: Any,
    chunk: ChunkRow,
    config: BackfillConfig,
    first_page: int,
    last_page: int,
) -> tuple[BatchOutcome, int]:
    """Run a batch; on a duration-wall cancellation halve the RANGE once and retry.

    Halving the range is what distinguishes this from the rejected
    LIMIT-subquery shape: the scan cost of a block range is linear in its
    width, so a halved range really does half the work. A second cancellation
    means the per-page cost itself is beyond the wall, which is an operator
    decision (raise the wall, or lower batch_pages), not something to keep
    retrying against a live database.
    """
    try:
        with connection.cursor() as cursor:
            return (
                execute_batch(
                    cursor,
                    chunk,
                    first_page=first_page,
                    last_page=last_page,
                    duration_wall_ms=config.duration_wall_ms,
                ),
                0,
            )
    except BatchDurationExceeded:
        connection.rollback()

    midpoint = first_page + max(1, (last_page - first_page) // 2)
    if midpoint >= last_page:
        raise BackfillStop(
            "duration_wall",
            (
                f"{chunk.key} page {first_page} alone exceeded the {config.duration_wall_ms} ms "
                "duration wall; a single page cannot be halved further"
            ),
            chunk_schema=chunk.chunk_schema,
            chunk_name=chunk.chunk_name,
            first_page=first_page,
            last_page=last_page,
        )
    try:
        with connection.cursor() as cursor:
            return (
                execute_batch(
                    cursor,
                    chunk,
                    first_page=first_page,
                    last_page=midpoint,
                    duration_wall_ms=config.duration_wall_ms,
                ),
                1,
            )
    except BatchDurationExceeded as error:
        connection.rollback()
        raise BackfillStop(
            "duration_wall",
            (
                f"{chunk.key} pages [{first_page},{midpoint}) exceeded the "
                f"{config.duration_wall_ms} ms duration wall even after one halving; stopping "
                "fail-closed rather than retrying against a live database"
            ),
            chunk_schema=chunk.chunk_schema,
            chunk_name=chunk.chunk_name,
            first_page=first_page,
            last_page=midpoint,
        ) from error


def build_receipt(
    config: BackfillConfig,
    *,
    now_utc: datetime,
    connect: Callable[[str], Any],
    head_sha: str,
    timer_state: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Discover, plan, optionally mutate, and return the receipt."""
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "head_sha": head_sha,
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "hypertable": HYPERTABLE_KEY,
        "mode": config.mode,
        "outcome": "clean",
        "final_sweep": config.final_sweep,
        "bounds": {
            "batch_pages": config.batch_pages,
            "duration_wall_ms": config.duration_wall_ms,
            "batch_sleep_ms": config.batch_sleep_ms,
            "max_batches": config.max_batches,
            "lag_seconds": config.lag_seconds,
        },
        "compression_timer": dict(timer_state) if timer_state else observe_compression_timer(),
        "chunks": [],
        "totals": _blank_totals(),
        "cursor": {},
    }

    connection = connect(config.database_url)
    try:
        with connection.cursor() as cursor:
            chunks = fetch_chunk_inventory(cursor)
        connection.rollback()

        eligible, skipped = classify_chunks(
            chunks,
            now_utc=now_utc,
            lag_seconds=config.lag_seconds,
            final_sweep=config.final_sweep,
        )
        receipt["disk"] = observe_disk(estimate_bloat_bytes(eligible))

        descriptors: list[dict[str, Any]] = []
        totals = receipt["totals"]
        for chunk, reason in skipped:
            descriptors.append(
                _chunk_descriptor(
                    chunk,
                    f"skipped_{reason}",
                    skip_reason=_SKIP_REASONS[reason],
                )
            )
            totals[f"chunks_skipped_{reason}"] += 1

        cursor_budget = [config.max_batches]
        stop: BackfillStop | None = None
        for chunk in eligible:
            if config.final_sweep and is_active_chunk(
                chunk, now_utc=now_utc, lag_seconds=config.lag_seconds
            ):
                with connection.cursor() as cursor:
                    assert_ingest_quiescent(
                        cursor, chunk, quiescence_seconds=config.quiescence_seconds, sleep=sleep
                    )
                connection.rollback()
            try:
                descriptor = process_chunk(
                    connection, chunk, config, cursor_budget=cursor_budget, sleep=sleep
                )
            except BackfillStop as error:
                stop = error
                descriptor = _chunk_descriptor(chunk, "stopped", skip_reason=error.reason)
                descriptors.append(descriptor)
                break
            descriptors.append(descriptor)
            _accumulate(totals, descriptor)
            if descriptor.get("next_page") is not None:
                receipt["cursor"][chunk.key] = int(descriptor["next_page"])

        receipt["chunks"] = descriptors
        if stop is not None:
            receipt["outcome"] = "stopped"
            receipt["stop"] = {"stage": stop.stage, "reason": stop.reason, **stop.detail}
        return receipt
    finally:
        try:
            connection.close()
        except Exception:
            pass


_SKIP_REASONS = {
    "compressed": (
        "chunk is compressed; TimescaleDB 2.10 permits no DML against compressed storage. "
        "Recovery is the runbook decompress -> backfill -> recompress orchestration using the "
        "existing decompression-replay and compression runners."
    ),
    "active": (
        "chunk is active (not terminal by the shared compression-lane lag criterion); "
        "backfilling it would contend with ingest and never converge. It is picked up "
        "automatically once terminal, or explicitly via --final-sweep with ingest paused."
    ),
}


def _accumulate(totals: dict[str, Any], descriptor: Mapping[str, Any]) -> None:
    for key in (
        "candidate_rows",
        "updated_rows",
        "pending_rows",
        "batches_run",
        "batches_halved",
        "unmatched_rows",
        "unmappable_rows",
    ):
        totals[key] += int(descriptor.get(key) or 0)
    divergent = descriptor.get("equality_audit_divergent")
    if divergent is not None and totals["equality_audit_divergent"] is not None:
        totals["equality_audit_divergent"] += int(divergent)


def publish_receipt(config: BackfillConfig, receipt: Mapping[str, Any]) -> None:
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes_no_follow(config.receipt_path, payload, mode=0o600, require_durable_replace=True)


def build_refused_lock_receipt(
    config: BackfillConfig, *, now_utc: datetime, head_sha: str
) -> dict[str, Any]:
    """Terminal, mutation-free receipt for a contended lock.

    Deliberately performs no discovery: lock ownership is the boundary before
    every database call. Publishing the refusal replaces any stale success so
    governance never reads a previous run's receipt as this run's outcome.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "head_sha": head_sha,
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "hypertable": HYPERTABLE_KEY,
        "mode": config.mode,
        "outcome": "refused_lock",
        "final_sweep": config.final_sweep,
        "bounds": {
            "batch_pages": config.batch_pages,
            "duration_wall_ms": config.duration_wall_ms,
            "batch_sleep_ms": config.batch_sleep_ms,
            "max_batches": config.max_batches,
            "lag_seconds": config.lag_seconds,
        },
        "chunks": [],
        "totals": _blank_totals(),
        "cursor": {},
    }


def build_failed_receipt(
    config: BackfillConfig | None,
    *,
    now_utc: datetime,
    stage: str,
    head_sha: str | None,
    mutation_state: str = "failed_before_mutation",
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "mode": config.mode if config is not None else "dry-run",
        "outcome": "failed",
        "provenance_state": "bound" if head_sha is not None else "unavailable",
        "failure": {"stage": stage, "mutation_state": mutation_state},
    }
    if head_sha is not None:
        receipt["head_sha"] = head_sha
    return receipt


def _default_connect(database_url: str) -> Any:
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS)


def _emit(status: str, reason: str, dsn: str | None = None) -> None:
    payload: dict[str, Any] = {"status": status, "reason": reason}
    if dsn is not None:
        payload["dsn"] = _mask_dsn(dsn)
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    now_utc: datetime | None = None,
    connect: Callable[[str], Any] | None = None,
) -> int:
    now = now_utc or datetime.now(UTC)
    args = _parser().parse_args(argv)
    try:
        config = config_from_args(args)
    except BackfillConfigError as error:
        _emit("failed", str(error))
        return 1

    try:
        head_sha = _current_head_sha()
    except BackfillConfigError as error:
        _emit("failed", str(error))
        return 1

    try:
        lock_fd = acquire_lock(config.lock_path)
    except BackfillConfigError as error:
        _emit("failed", str(error))
        return 1

    if lock_fd is None:
        try:
            publish_receipt(config, build_refused_lock_receipt(config, now_utc=now, head_sha=head_sha))
        except SafeFilesystemError as error:
            _emit("failed", f"receipt publication error: {error}", dsn=config.database_url)
            return 1
        _emit("refused_lock", "lock-contended", dsn=config.database_url)
        return 0

    try:
        try:
            receipt = build_receipt(
                config,
                now_utc=now,
                connect=connect or _default_connect,
                head_sha=head_sha,
            )
        except BackfillStop as error:
            receipt = build_failed_receipt(
                config, now_utc=now, stage=error.stage, head_sha=head_sha
            )
            _try_publish(config, receipt)
            _emit("stopped", error.reason, dsn=config.database_url)
            return 1
        except (CompressedChunkWriteError, CompressedChunkGuardError) as error:
            _try_publish(
                config,
                build_failed_receipt(config, now_utc=now, stage="write_guard", head_sha=head_sha),
            )
            _emit("failed", str(error), dsn=config.database_url)
            return 1
        except Exception as error:
            _try_publish(
                config,
                build_failed_receipt(
                    config,
                    now_utc=now,
                    stage="runner",
                    head_sha=head_sha,
                    mutation_state="indeterminate" if config.enforce else "failed_before_mutation",
                ),
            )
            _emit("failed", f"backfill runner error ({type(error).__name__})", dsn=config.database_url)
            return 1

        try:
            publish_receipt(config, receipt)
        except SafeFilesystemError as error:
            _emit("failed", f"receipt publication error: {error}", dsn=config.database_url)
            return 1
        return 0 if receipt["outcome"] == "clean" else 1
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _try_publish(config: BackfillConfig, receipt: Mapping[str, Any]) -> None:
    try:
        publish_receipt(config, receipt)
    except SafeFilesystemError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
