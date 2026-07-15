#!/usr/bin/env python3
"""Bounded, receipted TimescaleDB terminal-chunk compression runner for node-27.

Task 4.2 of the ``tier-node27-timeseries-storage`` OpenSpec change
(issue #851). Selects terminal chunks — those whose ``range_end`` is older
than a configurable lag (default 7 days, one chunk width) — on the two
detail hypertables ``hydro.river_timeseries`` and
``met.forcing_station_timeseries`` and calls ``compress_chunk`` on at most
``per_tick_bound`` of them per invocation. Never writes to the active
chunk. Dry-run by default; ``--enforce`` performs mutation. Emits a
``schemas/timeseries_compression_receipt.schema.json``-conformant JSON
receipt via the shared no-follow atomic write helper.

Design references: openspec/changes/tier-node27-timeseries-storage/design.md
decisions D3, D7, and the "Workflow Fixture: Issue #851" section.
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
)

SCHEMA_VERSION = "2.0"
TOOL_VERSION = "node27-timeseries-compression/2"

# The two detail hypertables gated by D3. Ordering here is the tie-break in
# chunk selection and per-table totals — do not reorder without matching the
# schema example.
HYPERTABLES: tuple[tuple[str, str], ...] = (
    ("hydro", "river_timeseries"),
    ("met", "forcing_station_timeseries"),
)

# Statement timeouts. Chunk-catalog lookups against
# ``timescaledb_information.chunks`` are catalog-only (no hypertable row
# scan) so 60 s is generous; ``compress_chunk`` on a 7-day chunk of the
# river/forcing hypertables takes minutes, so 5 min per call is the ceiling
# we accept before erroring the individual chunk without corrupting the
# overall run.
_QUERY_TIMEOUT_MS = 60_000
_COMPRESS_TIMEOUT_MS = 300_000


class CompressionConfigError(RuntimeError):
    """Fail-closed configuration parse error before any DB call."""


def _current_head_sha(*, require_clean: bool = False) -> str:
    """Freeze the exact repository HEAD before selection or mutation begins."""
    repo_root = Path(__file__).resolve().parents[1]
    if require_clean:
        cleanliness = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--"],
            cwd=repo_root,
            check=False,
        )
        if cleanliness.returncode != 0:
            raise CompressionConfigError("runner worktree differs from repository HEAD")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    head_sha = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise CompressionConfigError("cannot bind receipt to repository HEAD")
    return head_sha


@dataclass(frozen=True)
class CompressionConfig:
    database_url: str
    lag_seconds: int
    per_tick_bound: int
    receipt_path: Path
    lock_path: Path
    enforce: bool


@dataclass(frozen=True)
class ChunkRow:
    hypertable_schema: str
    hypertable_name: str
    chunk_schema: str
    chunk_name: str
    range_start: datetime
    range_end: datetime
    is_compressed: bool

    @property
    def hypertable_key(self) -> str:
        return f"{self.hypertable_schema}.{self.hypertable_name}"

    @property
    def qualified_chunk(self) -> str:
        return f'"{self.chunk_schema}"."{self.chunk_name}"'


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


def _parse_positive_int(raw: str | None, *, name: str, minimum: int) -> int:
    if raw is None or raw == "":
        raise CompressionConfigError(f"{name} must be set")
    stripped = raw.strip()
    if stripped == "" or stripped != raw:
        raise CompressionConfigError(f"{name} must not contain leading/trailing whitespace")
    try:
        value = int(stripped)
    except ValueError as error:
        raise CompressionConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise CompressionConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def config_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> CompressionConfig:
    """Strict env + CLI parse. No truthiness fallback. Fails closed on bad shape."""
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise CompressionConfigError("DATABASE_URL must be set")
    lag_seconds = _parse_positive_int(
        env.get("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS"),
        name="NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS",
        minimum=1,
    )
    per_tick_bound = _parse_positive_int(
        env.get("NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND"),
        name="NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND",
        minimum=1,
    )
    receipt_raw = (
        args.receipt_path if args.receipt_path is not None else env.get("NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH")
    )
    lock_raw = (
        args.lock_path if args.lock_path is not None else env.get("NODE27_TIMESERIES_COMPRESSION_LOCK_PATH")
    )
    if not receipt_raw:
        raise CompressionConfigError(
            "receipt path must be set via --receipt-path or "
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"
        )
    if not lock_raw:
        raise CompressionConfigError(
            "lock path must be set via --lock-path or "
            "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH"
        )
    receipt_path = Path(str(receipt_raw))
    lock_path = Path(str(lock_raw))
    if not receipt_path.is_absolute():
        raise CompressionConfigError("receipt path must be absolute")
    if not lock_path.is_absolute():
        raise CompressionConfigError("lock path must be absolute")
    return CompressionConfig(
        database_url=database_url,
        lag_seconds=lag_seconds,
        per_tick_bound=per_tick_bound,
        receipt_path=receipt_path,
        lock_path=lock_path,
        enforce=bool(args.enforce),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true", help="actually invoke compress_chunk")
    parser.add_argument("--receipt-path", type=str, default=None)
    parser.add_argument("--lock-path", type=str, default=None)
    return parser


def acquire_lock(path: Path) -> int | None:
    """Take a nonblocking flock on a mode-0600 lock file. Return None on contention."""
    if not path.is_absolute():
        raise CompressionConfigError("lock path must be absolute")
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
            raise CompressionConfigError("lock file must be a mode-0600 regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except CompressionConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise CompressionConfigError(f"cannot acquire lock file: {error}") from error
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# DB interaction
# ---------------------------------------------------------------------------

# SQL: chunk-selection lookup against timescaledb_information.chunks only.
# Deliberately catalog-only; MUST NOT reference the detail hypertables.
# Filter for the two D3 hypertables. Ordering keeps the receipt deterministic
# and the (selected, deferred) partition stable across ties.
# is_compressed = false is an explicit stale-state guard so re-running the
# runner over an already-compressed chunk is a no-op (see design "Workflow
# Fixture: Issue #851" boundary-surface checklist).
_CHUNK_QUERY = """
SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
       range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE (hypertable_schema, hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('met', 'forcing_station_timeseries')
)
  AND is_compressed = false
ORDER BY hypertable_schema, hypertable_name, range_end ASC
"""


# TimescaleDB 2.10 exposes compression state in
# ``timescaledb_information.chunks`` but does not expose the compressed
# sibling relation name there.  Resolve the origin -> sibling mapping from
# the extension's chunk catalog instead.  The node-27 live oracle runs 2.10.2;
# querying non-existent ``compressed_chunk_schema/name`` information-view
# columns would fail only after ``compress_chunk`` had already mutated data.
_COMPRESSED_SIBLING_QUERY = """
SELECT sibling.schema_name, sibling.table_name
FROM _timescaledb_catalog.chunk AS origin
JOIN _timescaledb_catalog.chunk AS sibling
  ON sibling.id = origin.compressed_chunk_id
WHERE origin.schema_name = %s
  AND origin.table_name = %s
  AND NOT origin.dropped
  AND NOT sibling.dropped
"""


def _row_to_chunk(row: Mapping[str, Any]) -> ChunkRow:
    range_start = row["range_start"]
    range_end = row["range_end"]
    if isinstance(range_start, str):
        range_start = datetime.fromisoformat(range_start.replace("Z", "+00:00"))
    if isinstance(range_end, str):
        range_end = datetime.fromisoformat(range_end.replace("Z", "+00:00"))
    if range_start.tzinfo is None:
        range_start = range_start.replace(tzinfo=UTC)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=UTC)
    return ChunkRow(
        hypertable_schema=str(row["hypertable_schema"]),
        hypertable_name=str(row["hypertable_name"]),
        chunk_schema=str(row["chunk_schema"]),
        chunk_name=str(row["chunk_name"]),
        range_start=range_start.astimezone(UTC),
        range_end=range_end.astimezone(UTC),
        is_compressed=bool(row["is_compressed"]),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Function signatures for DB interaction — injectable so unit tests can
# replace them without a live database. ``measure_chunk_bytes`` accepts an
# ``after`` keyword: when True, the default implementation resolves the
# compressed relation name from ``timescaledb_information.chunks`` (which
# ``compress_chunk`` populates) and measures THAT relation. Measuring the
# origin chunk after compression would report ~0 bytes because the origin
# is truncated when its rows are moved to the compressed sibling — that is
# the semantic bug cand-A hardens against.
FetchChunks = Callable[[str], list[ChunkRow]]
MeasureChunkBytes = Callable[..., int]
CompressChunk = Callable[[str, ChunkRow], None]
ReconcileChunkState = Callable[[str, ChunkRow], bool]


def _default_fetch_chunks(database_url: str) -> list[ChunkRow]:
    import psycopg2  # type: ignore[import-untyped]
    import psycopg2.extras  # type: ignore[import-untyped]

    connection = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {_QUERY_TIMEOUT_MS}")
                cursor.execute(_CHUNK_QUERY)
                return [_row_to_chunk(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _default_measure_chunk_bytes(
    database_url: str, chunk: ChunkRow, *, after: bool = False
) -> int:
    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {_QUERY_TIMEOUT_MS}")
                target_schema = chunk.chunk_schema
                target_name = chunk.chunk_name
                if after:
                    # Re-query the catalog for the compressed sibling that
                    # compress_chunk() populated. cand-G: refuse to fall back
                    # to the origin chunk — post-compress its rows have been
                    # moved to the sibling and it now measures near-zero,
                    # which would silently misreport savings. Raise instead so
                    # the outer per-chunk try/except records ``after_bytes =
                    # null`` in the descriptor and marks outcome=partial.
                    cursor.execute(
                        _COMPRESSED_SIBLING_QUERY,
                        (chunk.chunk_schema, chunk.chunk_name),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError(
                            f"compressed sibling not visible for {chunk.chunk_schema}.{chunk.chunk_name}"
                        )
                    compressed_schema, compressed_name = row
                    if not compressed_schema or not compressed_name:
                        raise RuntimeError(
                            f"compressed sibling not visible for {chunk.chunk_schema}.{chunk.chunk_name}"
                        )
                    target_schema = compressed_schema
                    target_name = compressed_name
                cursor.execute(
                    "SELECT pg_total_relation_size(%s::regclass)",
                    (f"{target_schema}.{target_name}",),
                )
                (bytes_value,) = cursor.fetchone()
                return int(bytes_value or 0)
    finally:
        connection.close()


def _default_compress_chunk(database_url: str, chunk: ChunkRow) -> None:
    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {_COMPRESS_TIMEOUT_MS}")
                cursor.execute(
                    "SELECT compress_chunk(%s::regclass)",
                    (f"{chunk.chunk_schema}.{chunk.chunk_name}",),
                )
                cursor.fetchone()
    finally:
        connection.close()


def _default_reconcile_chunk_state(database_url: str, chunk: ChunkRow) -> bool:
    """Read the exact target through a fresh catalog connection after uncertainty."""

    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(database_url)
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {_QUERY_TIMEOUT_MS}")
            cursor.execute(
                """
                SELECT is_compressed
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = %s
                  AND hypertable_name = %s
                  AND chunk_schema = %s
                  AND chunk_name = %s
                """,
                (
                    chunk.hypertable_schema,
                    chunk.hypertable_name,
                    chunk.chunk_schema,
                    chunk.chunk_name,
                ),
            )
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], bool):
                raise RuntimeError("exact target catalog state unavailable")
            return row[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Runner core
# ---------------------------------------------------------------------------


def _classify(
    all_chunks: Sequence[ChunkRow],
    *,
    now_utc: datetime,
    lag_seconds: int,
    per_tick_bound: int,
) -> tuple[list[ChunkRow], list[ChunkRow], list[ChunkRow]]:
    """Partition chunks into (selected, deferred, skipped_inside_lag)."""
    lag = timedelta(seconds=lag_seconds)
    cutoff = now_utc - lag
    eligible: list[ChunkRow] = []
    skipped: list[ChunkRow] = []
    for chunk in all_chunks:
        if chunk.is_compressed:
            # Query filter should exclude these, but keep the safety net.
            continue
        if chunk.range_end < cutoff:
            eligible.append(chunk)
        else:
            skipped.append(chunk)
    selected = eligible[:per_tick_bound]
    deferred = eligible[per_tick_bound:]
    return selected, deferred, skipped


def _blank_totals() -> dict[str, dict[str, Any]]:
    return {
        f"{schema_}.{name}": {"before_bytes": 0, "after_bytes": 0, "chunks_compressed": 0}
        for schema_, name in HYPERTABLES
    }


def _descriptor(chunk: ChunkRow, *, before: int, after: int | None) -> dict[str, Any]:
    return {
        "hypertable_schema": chunk.hypertable_schema,
        "hypertable_name": chunk.hypertable_name,
        "chunk_schema": chunk.chunk_schema,
        "chunk_name": chunk.chunk_name,
        "range_start": _iso(chunk.range_start),
        "range_end": _iso(chunk.range_end),
        "before_bytes": before,
        "after_bytes": after,
        "mutation_state": "not_applicable",
    }


def _safe_failure(operation: str, error: Exception) -> str:
    """Describe failure class without copying credential-bearing exception text."""

    return f"{operation} failed ({type(error).__name__})"


def build_receipt(
    config: CompressionConfig,
    *,
    now_utc: datetime,
    fetch_chunks: FetchChunks,
    measure_chunk_bytes: MeasureChunkBytes,
    compress_chunk: CompressChunk,
    reconcile_chunk_state: ReconcileChunkState = _default_reconcile_chunk_state,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Perform the selection + (optionally) compression and return the receipt."""
    frozen_head_sha = head_sha or _current_head_sha()
    if re.fullmatch(r"[0-9a-f]{40}", frozen_head_sha) is None:
        raise CompressionConfigError("receipt head_sha must be a lowercase 40-hex Git SHA")
    chunks = fetch_chunks(config.database_url)
    selected_rows, deferred_rows, skipped_rows = _classify(
        chunks,
        now_utc=now_utc,
        lag_seconds=config.lag_seconds,
        per_tick_bound=config.per_tick_bound,
    )
    totals = _blank_totals()
    # Track whether any per-table totals became meaningfully aware of
    # ``after_bytes``. If nothing was compressed we keep the dry-run
    # convention of ``after_bytes = null`` (schema allows it).
    saw_after = {key: False for key in totals}
    # Universal per-table invariant (rounds 1-3 closure). For every hypertable T:
    #   chunks_compressed = count of chunks that reached the compressed state
    #                       (compress_chunk succeeded), regardless of whether
    #                       after-measurement succeeded.
    #   before_bytes      = sum(chunk.before for those same chunks) — never
    #                       includes chunks whose before-measure or
    #                       compress_chunk failed (those chunks never reached
    #                       the compressed state and MUST NOT inflate the
    #                       denominator of a (before-after)/before ratio).
    #   after_bytes       = sum(chunk.after) if every chunk in the table
    #                       succeeded end-to-end, else null. Any failure on
    #                       any chunk in the table (before / compress / after)
    #                       poisons after_bytes to null so a partial sum can
    #                       never masquerade as the true compressed footprint.
    after_poisoned = {key: False for key in totals}
    selected_descriptors: list[dict[str, Any]] = []
    any_errors = False
    for chunk in selected_rows:
        # Symmetrical per-chunk isolation. Any failure on this chunk is
        # recorded in the descriptor, poisons the table's after_bytes, and the
        # top-level outcome becomes ``partial`` — but does not abort the loop.
        try:
            before = int(measure_chunk_bytes(config.database_url, chunk))
        except Exception as error:
            descriptor = _descriptor(chunk, before=0, after=None)
            if config.enforce:
                descriptor["mutation_state"] = "failed_before_mutation"
            descriptor["error"] = _safe_failure("measure_chunk_bytes(before)", error)
            any_errors = True
            # Chunk never reached compressed state — do NOT contribute to any
            # totals — but poison after_bytes so successful siblings in the
            # same table cannot masquerade as the whole table's savings.
            after_poisoned[chunk.hypertable_key] = True
            selected_descriptors.append(descriptor)
            continue
        descriptor: dict[str, Any]
        if not config.enforce:
            descriptor = _descriptor(chunk, before=before, after=None)
        else:
            try:
                compress_chunk(config.database_url, chunk)
            except Exception as error:  # per-chunk isolation per issue spec
                try:
                    reconciled_compressed = reconcile_chunk_state(config.database_url, chunk)
                except Exception:
                    descriptor = _descriptor(chunk, before=before, after=None)
                    descriptor["mutation_state"] = "indeterminate"
                    descriptor["error"] = "compress_chunk result indeterminate; exact-target reconciliation unavailable"
                    any_errors = True
                    after_poisoned[chunk.hypertable_key] = True
                    selected_descriptors.append(descriptor)
                    continue
                if not reconciled_compressed:
                    descriptor = _descriptor(chunk, before=before, after=None)
                    descriptor["mutation_state"] = "failed_before_mutation"
                    descriptor["error"] = _safe_failure(
                        "compress_chunk before mutation", error
                    )
                    any_errors = True
                    after_poisoned[chunk.hypertable_key] = True
                    selected_descriptors.append(descriptor)
                    continue
                # The commit happened even though its acknowledgement was
                # lost. Preserve committed truth and continue to a fresh size
                # measurement; the top-level result remains partial because
                # the invocation itself did not complete normally.
                any_errors = True
                totals[chunk.hypertable_key]["before_bytes"] += before
                totals[chunk.hypertable_key]["chunks_compressed"] += 1
                try:
                    after = int(measure_chunk_bytes(config.database_url, chunk, after=True))
                except Exception:
                    descriptor = _descriptor(chunk, before=before, after=None)
                    descriptor["mutation_state"] = "committed"
                    descriptor["error"] = "compression committed; post-measurement unavailable"
                    after_poisoned[chunk.hypertable_key] = True
                    selected_descriptors.append(descriptor)
                    continue
                descriptor = _descriptor(chunk, before=before, after=after)
                descriptor["mutation_state"] = "committed"
                descriptor["error"] = "compression committed after lost acknowledgement"
                totals[chunk.hypertable_key]["after_bytes"] += after
                saw_after[chunk.hypertable_key] = True
                selected_descriptors.append(descriptor)
                continue
            try:
                after = int(measure_chunk_bytes(config.database_url, chunk, after=True))
            except Exception as error:
                descriptor = _descriptor(chunk, before=before, after=None)
                descriptor["mutation_state"] = "committed"
                descriptor["error"] = _safe_failure("measure_chunk_bytes(after)", error)
                any_errors = True
                # The compression itself did succeed, so the chunk reached the
                # compressed state — record chunks_compressed + before_bytes.
                # Only the after side is unknown, so poison the table's
                # after_bytes to null (partial sum would misreport footprint).
                totals[chunk.hypertable_key]["before_bytes"] += before
                totals[chunk.hypertable_key]["chunks_compressed"] += 1
                after_poisoned[chunk.hypertable_key] = True
                selected_descriptors.append(descriptor)
                continue
            descriptor = _descriptor(chunk, before=before, after=after)
            descriptor["mutation_state"] = "committed"
            key = chunk.hypertable_key
            totals[key]["chunks_compressed"] += 1
            totals[key]["before_bytes"] += before
            totals[key]["after_bytes"] += after
            saw_after[key] = True
            selected_descriptors.append(descriptor)
            continue
        totals[chunk.hypertable_key]["before_bytes"] += before
        selected_descriptors.append(descriptor)

    # Nullify after_bytes for tables where nothing was compressed (matches
    # dry-run convention: absence of measurement, not zero-size) OR where ANY
    # chunk in the table hit ANY failure (before-fail, compress-fail, or
    # after-fail — sticky poison preserving the invariant that per-table
    # after_bytes is either the exact aggregate over ``chunks_compressed`` or
    # null, never a partial sum).
    for key in totals:
        if not saw_after[key] or after_poisoned[key]:
            totals[key]["after_bytes"] = None

    if config.enforce:
        outcome = "partial" if any_errors else "clean"
    else:
        outcome = "clean"

    deferred_descriptors = [
        {
            **{
                key: value
                for key, value in _descriptor(chunk, before=0, after=None).items()
                if key != "mutation_state"
            },
            "defer_reason": "per-tick bound reached",
        }
        for chunk in deferred_rows
    ]
    skipped_descriptors = [
        {
            **{
                key: value
                for key, value in _descriptor(chunk, before=0, after=None).items()
                if key != "mutation_state"
            },
            "skip_reason": "range_end inside lag window",
        }
        for chunk in skipped_rows
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "head_sha": frozen_head_sha,
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "lag_seconds": config.lag_seconds,
        "per_tick_bound": config.per_tick_bound,
        "mode": "enforce" if config.enforce else "dry-run",
        "outcome": outcome,
        "selected": selected_descriptors,
        "deferred": deferred_descriptors,
        "skipped": skipped_descriptors,
        "per_table_totals": totals,
    }


def publish_receipt(config: CompressionConfig, receipt: Mapping[str, Any]) -> None:
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes_no_follow(
        config.receipt_path, payload, mode=0o600, require_durable_replace=True
    )


def build_refused_lock_receipt(
    config: CompressionConfig, *, now_utc: datetime, head_sha: str | None = None
) -> dict[str, Any]:
    """Build the mutation-free terminal receipt for a contended runner lock.

    This path deliberately does not discover chunks: lock ownership is the
    boundary before every DB call.  Publishing the refusal replaces any stale
    success receipt so governance sees the current invocation's terminal
    state.
    """
    frozen_head_sha = head_sha or _current_head_sha()
    if re.fullmatch(r"[0-9a-f]{40}", frozen_head_sha) is None:
        raise CompressionConfigError("receipt head_sha must be a lowercase 40-hex Git SHA")
    return {
        "schema_version": SCHEMA_VERSION,
        "head_sha": frozen_head_sha,
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "lag_seconds": config.lag_seconds,
        "per_tick_bound": config.per_tick_bound,
        "mode": "enforce" if config.enforce else "dry-run",
        "outcome": "refused_lock",
        "selected": [],
        "deferred": [],
        "skipped": [],
        "per_table_totals": {
            key: {
                "before_bytes": 0,
                "after_bytes": None,
                "chunks_compressed": 0,
            }
            for key in _blank_totals()
        },
    }


def build_failed_receipt(
    config: CompressionConfig,
    *,
    now_utc: datetime,
    stage: str,
    head_sha: str | None,
    mutation_state: str = "failed_before_mutation",
) -> dict[str, Any]:
    """Build a non-secret terminal failure that replaces any stale success."""

    receipt: dict[str, Any] = {
        "schema_version": "2.0" if head_sha is not None else "1.0",
        "generated_at": _iso(datetime.now(UTC)),
        "now_utc": _iso(now_utc),
        "lag_seconds": config.lag_seconds,
        "per_tick_bound": config.per_tick_bound,
        "mode": "enforce" if config.enforce else "dry-run",
        "outcome": "failed",
        "selected": [],
        "deferred": [],
        "skipped": [],
        "per_table_totals": {
            key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0}
            for key in _blank_totals()
        },
        "failure": {"stage": stage, "mutation_state": mutation_state},
    }
    if head_sha is not None:
        receipt["head_sha"] = head_sha
    return receipt


def _replace_stale_with_failure(
    config: CompressionConfig,
    *,
    now_utc: datetime,
    stage: str,
    head_sha: str | None,
    mutation_state: str = "failed_before_mutation",
) -> None:
    try:
        publish_receipt(
            config,
            build_failed_receipt(
                config,
                now_utc=now_utc,
                stage=stage,
                head_sha=head_sha,
                mutation_state=mutation_state,
            ),
        )
    except SafeFilesystemError:
        # Publication failure is reported by the caller; no claim that the
        # stale destination was replaced is made.
        return


def _emit_stderr_diagnostic(status: str, reason: str, dsn: str | None = None) -> None:
    payload: dict[str, Any] = {"status": status, "reason": reason}
    if dsn is not None:
        payload["dsn"] = _mask_dsn(dsn)
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    now_utc: datetime | None = None,
    fetch_chunks: FetchChunks | None = None,
    measure_chunk_bytes: MeasureChunkBytes | None = None,
    compress_chunk: CompressChunk | None = None,
    reconcile_chunk_state: ReconcileChunkState | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
        config = config_from_args(args)
    except CompressionConfigError as error:
        _emit_stderr_diagnostic("failed", str(error))
        return 1
    now = now_utc or datetime.now(UTC)
    try:
        frozen_head_sha = _current_head_sha(require_clean=True)
    except CompressionConfigError as error:
        _replace_stale_with_failure(
            config,
            now_utc=now,
            stage="freeze_head",
            head_sha=None,
        )
        _emit_stderr_diagnostic("failed", str(error), dsn=config.database_url)
        return 1
    try:
        lock_fd = acquire_lock(config.lock_path)
    except CompressionConfigError as error:
        _replace_stale_with_failure(
            config,
            now_utc=now,
            stage="acquire_lock",
            head_sha=frozen_head_sha,
        )
        _emit_stderr_diagnostic("failed", str(error))
        return 1
    if lock_fd is None:
        receipt = build_refused_lock_receipt(
            config, now_utc=now, head_sha=frozen_head_sha
        )
        try:
            publish_receipt(config, receipt)
        except SafeFilesystemError as error:
            _emit_stderr_diagnostic(
                "failed",
                f"receipt publication error: {error}",
                dsn=config.database_url,
            )
            return 1
        _emit_stderr_diagnostic("refused_lock", "lock-contended", dsn=config.database_url)
        return 0
    try:
        try:
            receipt = build_receipt(
                config,
                now_utc=now,
                fetch_chunks=fetch_chunks or _default_fetch_chunks,
                measure_chunk_bytes=measure_chunk_bytes or _default_measure_chunk_bytes,
                compress_chunk=compress_chunk or _default_compress_chunk,
                reconcile_chunk_state=reconcile_chunk_state
                or _default_reconcile_chunk_state,
                head_sha=frozen_head_sha,
            )
        except CompressionConfigError as error:
            _replace_stale_with_failure(
                config,
                now_utc=now,
                stage="runner",
                head_sha=frozen_head_sha,
            )
            _emit_stderr_diagnostic("failed", str(error), dsn=config.database_url)
            return 1
        except SafeFilesystemError as error:
            _replace_stale_with_failure(
                config,
                now_utc=now,
                stage="runner",
                head_sha=frozen_head_sha,
            )
            _emit_stderr_diagnostic("failed", f"receipt publication error: {error}", dsn=config.database_url)
            return 1
        except Exception as error:
            _replace_stale_with_failure(
                config,
                now_utc=now,
                stage="runner",
                head_sha=frozen_head_sha,
                mutation_state="indeterminate" if config.enforce else "failed_before_mutation",
            )
            _emit_stderr_diagnostic(
                "failed",
                f"compression runner error ({type(error).__name__})",
                dsn=config.database_url,
            )
            return 1
        try:
            publish_receipt(config, receipt)
        except SafeFilesystemError as error:
            _emit_stderr_diagnostic("failed", f"receipt publication error: {error}", dsn=config.database_url)
            return 1
        return 0 if receipt["outcome"] == "clean" else 1
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
