"""Bounded statvfs/du/PostgreSQL sample collection for cold governance.

The operational script remains CLI/orchestration.  This owner observes disk
capacity, path sizes, and PostgreSQL relation inventory, and refuses to
account unobserved cold-relation bytes as zero.

The working-set ingest rate (fixture decision 25) is read from ONE chunk per
canonical hypertable: the latest-by-``range_end`` chunk whose
``compression_status`` is ``Compressed``, ``before_compression_total_bytes``
divided by that chunk's own width.  It is deliberately NOT read from the
uncompressed set.  ``hydro.river_timeseries`` is partitioned on forecast
``valid_time`` with a 168 h horizon, so the newest chunk is written before the
display watermark ever reaches it: at 71bc5265 the node-27 receipt divided a
324 GB chunk whose ``range_start`` equalled the watermark by the covered age
and reported ``daily_ingest_bytes = 8_370_462_645_101`` (8.4 TB/day) against
the ≈78 GB/day the catalog can actually measure, plus a false critical
``PROJECTED_PEAK_EXCEEDS_HOME_FREE`` (peak 17.2 TB vs 721 GB free).  Any rate
taken from uncompressed bytes over covered time is front-loaded by
construction; a compressed chunk has received every write it will ever get.

Recorded limits, not compensated: the rate is lag + chunk width old (≈9 days
today, ≈3 days after the one-day-chunk expand), so roster growth under-reports
until the next compression, and D8's peak formula assumes that rate is uniform
over the projected span.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.display_watermark import DisplayWatermarkError, fetch_display_watermark
from packages.common.node27_timeseries_hypertable_discovery import (
    CANONICAL_HYPERTABLES,
    DISCOVERY_SQL,
    candidate_in_list_sql,
    discovery_set,
    is_legacy,
    present_from_rows,
    qualified,
)

DEFAULT_REPO_RELATIVE_SIZE_TARGETS = (
    "data",
    ".nhms-runs",
    ".nhms-work",
    ".pgdata",
    "artifacts",
    ".venv",
    ".conda-pkgs",
    "apps/frontend/dist.bak-20260615-234427",
    "apps/frontend/dist.bak-20260615-235046",
)
DEFAULT_OBJECT_STORE_RELATIVE_SIZE_TARGETS = (
    "raw",
    "runs",
    "forcing",
    "states",
    "scheduler",
    ".reset-quarantine",
    ".reset-receipts",
)


def bytes_pretty(value: int | float | None) -> str | None:
    if value is None:
        return None
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TiB"


def safe_resolve(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser()


def filesystem_identity(path: Path) -> str | None:
    """Return a local statvfs/device identity without scanning a shared root."""

    try:
        info = path.stat()
        usage = os.statvfs(path)
    except OSError:
        return None
    return f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}:{usage.f_fsid}"


def disk_usage(path: Path) -> dict[str, Any]:
    resolved = safe_resolve(path)
    if resolved is None:
        return {"path": str(path), "status": "unavailable"}
    try:
        usage = os.statvfs(resolved)
        info = resolved.stat()
    except OSError as error:
        return {"path": str(resolved), "status": "unavailable", "error": str(error)}
    total = usage.f_blocks * usage.f_frsize
    free = usage.f_bavail * usage.f_frsize
    used = (usage.f_blocks - usage.f_bfree) * usage.f_frsize
    reserved = max(usage.f_bfree - usage.f_bavail, 0) * usage.f_frsize
    return {
        "path": str(resolved),
        "status": "ok",
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "reserved_bytes": reserved,
        "used_pct": round(100.0 * used / total, 3) if total else None,
        "total_pretty": bytes_pretty(total),
        "used_pretty": bytes_pretty(used),
        "free_pretty": bytes_pretty(free),
        "device_identity": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}:{usage.f_fsid}",
    }


def run_command(args: Sequence[str], *, timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return {"status": "unavailable", "error": str(error), "args": list(args)}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "args": list(args), "timeout_sec": timeout}
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "args": list(args),
    }


def du_bytes(path: Path) -> dict[str, Any]:
    resolved = safe_resolve(path)
    if resolved is None:
        return {"path": str(path), "status": "unavailable"}
    if not resolved.exists():
        return {"path": str(resolved), "status": "missing"}
    first = run_command(["du", "-s", "-B1", str(resolved)])
    if first["status"] == "ok" and first.get("stdout"):
        try:
            bytes_value = int(str(first["stdout"]).split()[0])
        except (IndexError, ValueError):
            bytes_value = None
        if bytes_value is not None:
            return {
                "path": str(resolved),
                "status": "ok",
                "bytes": bytes_value,
                "pretty": bytes_pretty(bytes_value),
            }
    fallback = run_command(["du", "-sk", str(resolved)])
    if fallback["status"] == "ok" and fallback.get("stdout"):
        try:
            kib_value = int(str(fallback["stdout"]).split()[0])
        except (IndexError, ValueError):
            kib_value = None
        if kib_value is not None:
            bytes_value = kib_value * 1024
            return {
                "path": str(resolved),
                "status": "ok",
                "bytes": bytes_value,
                "pretty": bytes_pretty(bytes_value),
            }
    return {
        "path": str(resolved),
        "status": "unavailable",
        "error": fallback.get("stderr") or first.get("stderr") or "du_failed",
    }


def collect_filesystem(config: Any) -> dict[str, Any]:
    filesystems = {
        "root": disk_usage(Path("/")),
        "home": disk_usage(Path("/home")),
        "repo_root_fs": disk_usage(config.repo_root),
        "object_store_fs": disk_usage(config.object_store_root),
        "cold": disk_usage(Path("/data/GHDC")),
    }
    path_sizes: dict[str, Any] = {
        "repo_root": du_bytes(config.repo_root),
        "object_store_root": du_bytes(config.object_store_root),
    }
    if config.pgdata_root is not None:
        path_sizes["pgdata_root"] = du_bytes(config.pgdata_root)
        path_sizes["pg_wal"] = du_bytes(config.pgdata_root / "pg_wal")
    for relative in DEFAULT_REPO_RELATIVE_SIZE_TARGETS:
        path_sizes[f"repo/{relative}"] = du_bytes(config.repo_root / relative)
    for relative in DEFAULT_OBJECT_STORE_RELATIVE_SIZE_TARGETS:
        path_sizes[f"object-store/{relative}"] = du_bytes(config.object_store_root / relative)
    for label, path in {
        "autopipe_logs": Path("/home/nwm/autopipe-logs"),
        "download_logs": Path("/home/nwm/node27-download-logs"),
        "raw_retention_logs": Path("/home/nwm/node27-raw-retention-logs"),
        "autopipe_work": Path("/home/nwm/autopipe-work"),
        "tmp": Path("/tmp"),
    }.items():
        path_sizes[label] = du_bytes(path)
    return {
        "filesystems": filesystems,
        "path_sizes": path_sizes,
        "inode_usage": run_command(["df", "-ih", "/", "/home"]),
        "journal_disk_usage": run_command(["journalctl", "--disk-usage"]),
    }


# ---------------------------------------------------------------------------
# Uncompressed working set and next-compression peak (#1985, design D8)
# ---------------------------------------------------------------------------

#: Governance's own copy of the compression lag. It is read from
#: ``NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS`` in the GOVERNANCE lane's env
#: file — the compression template carries a write-role DSN and is explicitly
#: not synced — and this default is cross-pinned to that template's assignment
#: by a unit test so the two cannot drift apart in the repository. Drift of the
#: DEPLOYED node-27 values is outside any unit test's reach; the receipt echoes
#: ``compression_lag_seconds`` precisely so the rollout receipt can compare it
#: with the live compression env.
DEFAULT_COMPRESSION_LAG_SECONDS = 172_800

#: Seconds the audit will wait for its read-only PostgreSQL connection before
#: giving up, same value and same rationale as the lifecycle runners'
#: ``_CONNECT_TIMEOUT_SECONDS`` (#1985 decision 21). The governance oneshot
#: runs with ``TimeoutStartSec=0``, so nothing above this call would ever kill
#: an unbounded ``connect``: a wedged TCP handshake would hold the unit in
#: ``activating`` forever and every later tick would be skipped as "already
#: running" -- a capacity guard that stops watching without ever failing. A
#: bounded connect turns that into ``connection_failed`` -> the critical
#: ``POSTGRES_UNAVAILABLE`` (decision 23) on the very first tick.
_CONNECT_TIMEOUT_SECONDS = 10

WORKING_SET_DISCOVERY_SQL = DISCOVERY_SQL

# Catalog-only by construction: chunk identities and sizes come from
# ``timescaledb_information.chunks`` and ``pg_total_relation_size``, never from
# a row scan of the fact tables. A 600 GB scan of the very volume this audit
# exists to protect would BE the incident it is meant to predict.
WORKING_SET_CHUNKS_SQL = f"""
SELECT c.hypertable_schema, c.hypertable_name, c.chunk_schema, c.chunk_name,
       c.range_start, c.range_end,
       pg_total_relation_size(
           format('%I.%I', c.chunk_schema, c.chunk_name)::regclass
       ) AS total_bytes
FROM timescaledb_information.chunks c
WHERE (c.hypertable_schema, c.hypertable_name) IN (
{candidate_in_list_sql()}
)
  AND c.is_compressed = false
ORDER BY c.range_end ASC, c.chunk_schema, c.chunk_name
"""

# Catalog-only as well: ``chunk_compression_stats`` is a TimescaleDB catalog
# FUNCTION over compression statistics, not a scan of the hypertable it names.
# Executed once per canonical hypertable, the parameter being the
# ``'<schema>.<table>'`` literal the function coerces to ``regclass`` (measured
# on node-27, TSDB 2.10.2, role ``nhms_display_ro``: 16 ms, EXECUTE granted).
#
# Three clauses carry the whole rule and each is pinned by a behavioural test:
#
# * ``compression_status = 'Compressed'`` -- the function also returns
#   ``Uncompressed`` rows, and an uncompressed chunk is exactly the front-loaded
#   reading this query exists to avoid;
# * ``ORDER BY c.range_end DESC`` -- LATEST, not largest: an older, bigger chunk
#   is a stale rate;
# * ``LIMIT 1`` -- one reference chunk per table; the width comes from the join
#   (``range_end - range_start``), never from a constant, so the same code reads
#   today's 7-day chunks and the post-expand 1-day chunks.
#
# 2.10.2 column names: ``chunk_schema``, ``chunk_name``, ``compression_status``,
# ``before_compression_total_bytes``. The ``after_*`` columns are NOT used: the
# rate is what was written, not what it compressed down to.
WORKING_SET_COMPRESSED_REFERENCE_SQL = """
SELECT c.chunk_schema, c.chunk_name, c.range_start, c.range_end,
       s.before_compression_total_bytes
FROM chunk_compression_stats(%s) s
JOIN timescaledb_information.chunks c
  ON (c.chunk_schema, c.chunk_name) = (s.chunk_schema, s.chunk_name)
WHERE s.compression_status = 'Compressed'
ORDER BY c.range_end DESC
LIMIT 1
"""

PROJECTION_OK = "ok"
PROJECTION_NO_UNCOMPRESSED_CHUNK = "no_uncompressed_chunk"
#: A canonical hypertable holds uncompressed chunks but has no compressed chunk
#: to read a rate from -- the narrow table during its first lag + chunk width
#: after the expand migration, or a fresh install. There is no honest growth
#: claim to make, so ``daily_ingest_bytes`` is ``None`` (never a partial sum
#: over the tables that DO have a reference) and the peak is the stock as it
#: stands. Visible as the WARNING ``NO_COMPRESSED_REFERENCE``, never a critical:
#: the state is expected and resolves itself at the next compression tick.
PROJECTION_NO_COMPRESSED_REFERENCE = "no_compressed_reference"
PROJECTION_WATERMARK_UNAVAILABLE = "watermark_unavailable"
#: The working set could not be measured at all: the timescale probes raised, or
#: the catalog returned no row for a canonical hypertable (the shape a read-only
#: role takes when it loses ``timescaledb_information.*`` visibility). An empty
#: catalog must NEVER be read as "everything is compressed" -- that is the one
#: failure mode which silently turns the capacity alarm off.
PROJECTION_WORKING_SET_UNAVAILABLE = "working_set_unavailable"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def unavailable_working_set(
    *,
    lag_seconds: int = DEFAULT_COMPRESSION_LAG_SECONDS,
    watermark: datetime | None = None,
    hypertables: Sequence[str] = (),
) -> dict[str, Any]:
    """The working set nobody could measure: every number ``None``, status set.

    Same key set as a measured sample so the receipt shape never varies; the
    byte counts are ``None`` rather than ``0`` because an unobserved working set
    reported as zero is exactly the reading that would suppress
    ``PROJECTED_PEAK_EXCEEDS_HOME_FREE``.
    """

    return {
        "hypertables": sorted(hypertables),
        "uncompressed_bytes": None,
        "uncompressed_chunks": None,
        "daily_ingest_bytes": None,
        # Same convention as every other measured field here: present and
        # ``None``, so the receipt's key set never varies with the failure mode.
        "ingest_reference": None,
        "next_compressible_at": None,
        "compression_lag_seconds": int(lag_seconds),
        "watermark": None if watermark is None else _iso(watermark),
        "projection_status": PROJECTION_WORKING_SET_UNAVAILABLE,
    }


def collect_working_set(
    cursor: Any,
    *,
    watermark: datetime | None,
    lag_seconds: int = DEFAULT_COMPRESSION_LAG_SECONDS,
) -> dict[str, Any]:
    """Measure the uncompressed working set of the governed hypertables.

    The governed set is discovered per invocation through the shared lifecycle
    helper, so a transitional ``_legacy`` sibling is accounted from the moment
    the expand migration creates it. ``watermark`` is the display business-time
    watermark, fetched with the compression runner's own fetcher and its
    fail-closed semantics: ``None`` means the lane could not prove it, which is
    a lane fault, never a zero.

    A catalog that does not report BOTH canonical hypertables is a lane fault of
    the same class: the rows are missing, not the data, so the sample comes back
    ``working_set_unavailable`` instead of an empty -- and therefore reassuring
    -- ``no_uncompressed_chunk``.
    """

    discovery_rows = _psycopg_rows(cursor, WORKING_SET_DISCOVERY_SQL)
    present = present_from_rows(discovery_rows)
    if not set(CANONICAL_HYPERTABLES) <= set(present):
        return unavailable_working_set(
            lag_seconds=lag_seconds,
            watermark=watermark,
            hypertables=[qualified(*item) for item in present],
        )

    hypertables = discovery_set(discovery_rows)
    rows = _psycopg_rows(cursor, WORKING_SET_CHUNKS_SQL)
    governed = {qualified(*item) for item in hypertables}
    chunks = [
        row
        for row in rows
        if qualified(str(row["hypertable_schema"]), str(row["hypertable_name"])) in governed
    ]
    uncompressed_bytes = sum(int(row["total_bytes"] or 0) for row in chunks)

    canonical = [item for item in hypertables if not is_legacy(item[1])]
    with_uncompressed = {
        (str(row["hypertable_schema"]), str(row["hypertable_name"])) for row in chunks
    }

    ingest_reference: dict[str, Any] | None
    if watermark is None:
        projection_status = PROJECTION_WATERMARK_UNAVAILABLE
        daily_ingest_bytes: int | None = None
        ingest_reference = None
    elif not chunks:
        projection_status = PROJECTION_NO_UNCOMPRESSED_CHUNK
        daily_ingest_bytes = 0
        # Nothing to project for any table, so no table needs a reference: the
        # empty mapping, not a mapping full of nulls.
        ingest_reference = {}
    else:
        # PER CANONICAL TABLE, then summed (round-3 review, kept by decision
        # 25). A `_legacy` sibling is write-frozen from the rename onward: it
        # contributes zero and is NEVER borrowed from, because its rows are
        # roughly six times wider than the narrow canonical table's and its
        # pre-compression bytes would over-report right through the I8 window.
        #
        # KEYED BY NEED, not by roster: only a canonical table that HAS
        # uncompressed chunks is keyed here. An empty canonical table has
        # nothing to project, so it gets no entry at all -- which is what makes
        # a `null` value mean exactly one thing, "this table needs a reference
        # and has none", and lets the warning's `tables_without_reference` be
        # that null set without ever naming an empty table.
        ingest_reference = {}
        without_reference: list[str] = []
        rate_total = 0
        for item in canonical:
            key = qualified(*item)
            if item not in with_uncompressed:
                continue
            reference = _compressed_reference(cursor, *item)
            ingest_reference[key] = reference
            if reference is None:
                without_reference.append(key)
            else:
                rate_total += int(reference["daily_ingest_bytes"])
        if without_reference:
            # No partial sum: a rate covering half the governed tables reads as
            # a whole-lane rate and under-projects the peak.
            projection_status = PROJECTION_NO_COMPRESSED_REFERENCE
            daily_ingest_bytes = None
        else:
            projection_status = PROJECTION_OK
            # ROUNDING: `int()` per table (truncation), then summed -- so the
            # receipt's `daily_ingest_bytes` is exactly the sum of the per-table
            # rates echoed in `ingest_reference` and the arithmetic can be
            # rechecked from the JSON alone.
            daily_ingest_bytes = rate_total

    next_compressible_at: str | None = None
    if chunks:
        oldest_end = min(_as_datetime(row["range_end"]) for row in chunks)
        next_compressible_at = _iso(oldest_end + timedelta(seconds=int(lag_seconds)))

    return {
        "hypertables": sorted(governed),
        "uncompressed_bytes": uncompressed_bytes,
        "uncompressed_chunks": len(chunks),
        "daily_ingest_bytes": daily_ingest_bytes,
        "ingest_reference": ingest_reference,
        "next_compressible_at": next_compressible_at,
        "compression_lag_seconds": int(lag_seconds),
        "watermark": None if watermark is None else _iso(watermark),
        "projection_status": projection_status,
    }


def _compressed_reference(cursor: Any, schema: str, name: str) -> dict[str, Any] | None:
    """The one chunk ONE canonical hypertable's ingest rate is read from.

    ``None`` when the catalog has no compressed chunk for it (nothing to read a
    rate from) or reports a non-positive width (a degenerate row that would
    divide by zero). Both are the same answer to the caller: this table makes no
    growth claim.
    """

    rows = _psycopg_rows(
        cursor, WORKING_SET_COMPRESSED_REFERENCE_SQL, (qualified(schema, name),)
    )
    if not rows:
        return None
    row = rows[0]
    range_start = _as_datetime(row["range_start"])
    range_end = _as_datetime(row["range_end"])
    width_days = (range_end - range_start).total_seconds() / 86_400.0
    if width_days <= 0:
        return None
    before_bytes = int(row["before_compression_total_bytes"] or 0)
    return {
        "chunk": qualified(str(row["chunk_schema"]), str(row["chunk_name"])),
        "range_start": _iso(range_start),
        "range_end": _iso(range_end),
        "before_compression_total_bytes": before_bytes,
        "width_days": width_days,
        "daily_ingest_bytes": int(before_bytes / width_days),
    }


def finalize_working_set(
    sample: Mapping[str, Any], *, home_free_bytes: int | None
) -> dict[str, Any]:
    """Add ``home_free_bytes`` and the projected next-compression peak.

    ``projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes x
    max(0, days(next_compressible_at - watermark))``, the interval expressed in
    days and allowed to be fractional. Only the ``ok`` projection multiplies:
    with no uncompressed chunk there is nothing to wait for, and with no
    watermark there is no honest interval to measure, so both project the
    working set exactly as it stands rather than inventing growth.
    ``no_compressed_reference`` joins them for the same reason (decision 25):
    ``daily_ingest_bytes`` is ``None``, so the only honest peak is the stock.
    """

    working_set = dict(sample)
    working_set["home_free_bytes"] = home_free_bytes
    if working_set.get("projection_status") == PROJECTION_WORKING_SET_UNAVAILABLE:
        # Nothing was observed, so there is no peak to state. Reporting zero
        # here would read as "it fits".
        working_set["projected_peak_bytes"] = None
        return working_set
    uncompressed = int(working_set.get("uncompressed_bytes") or 0)
    projected = uncompressed
    if (
        working_set.get("projection_status") == PROJECTION_OK
        and working_set.get("next_compressible_at")
        and working_set.get("watermark")
        and working_set.get("daily_ingest_bytes") is not None
    ):
        span_days = (
            _as_datetime(working_set["next_compressible_at"])
            - _as_datetime(working_set["watermark"])
        ).total_seconds() / 86_400.0
        projected = uncompressed + int(
            int(working_set["daily_ingest_bytes"]) * max(0.0, span_days)
        )
    working_set["projected_peak_bytes"] = projected
    return working_set


def _psycopg_rows(
    cursor: Any, sql: str, params: Sequence[Any] | None = None
) -> list[dict[str, Any]]:
    if params is None:
        cursor.execute(sql)
    else:
        cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def collect_postgres(
    database_url: str | None,
    *,
    compression_lag_seconds: int = DEFAULT_COMPRESSION_LAG_SECONDS,
) -> dict[str, Any]:
    if not database_url:
        return {"status": "skipped", "reason": "database_url_missing"}
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as error:  # pragma: no cover - environment dependent
        return {"status": "blocked", "reason": "psycopg2_unavailable", "error": str(error)}
    try:
        connection = psycopg2.connect(
            database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as error:
        return {"status": "blocked", "reason": "connection_failed", "error": str(error)}
    result: dict[str, Any] = {"status": "ok"}
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '20s'")
            result["database_sizes"] = _psycopg_rows(
                cursor,
                """
                SELECT datname,
                       pg_database_size(datname) AS bytes,
                       pg_size_pretty(pg_database_size(datname)) AS pretty
                FROM pg_database
                ORDER BY pg_database_size(datname) DESC
                """,
            )
            result["settings"] = _psycopg_rows(
                cursor,
                """
                SELECT name, setting, unit
                FROM pg_settings
                WHERE name IN (
                  'shared_buffers','work_mem','maintenance_work_mem','effective_cache_size',
                  'max_connections','temp_buffers','wal_buffers','max_wal_size','min_wal_size',
                  'wal_keep_size','checkpoint_timeout','autovacuum','autovacuum_max_workers',
                  'autovacuum_vacuum_scale_factor','autovacuum_analyze_scale_factor',
                  'autovacuum_naptime','track_counts','log_temp_files'
                )
                ORDER BY name
                """,
            )
            result["connections_by_state"] = _psycopg_rows(
                cursor,
                """
                SELECT usename, state, count(*) AS count,
                       max(now() - state_change) AS max_state_age
                FROM pg_stat_activity
                GROUP BY usename, state
                ORDER BY count DESC, usename, state
                """,
            )
            result["stat_database"] = _psycopg_rows(
                cursor,
                """
                SELECT datname, numbackends, xact_commit, xact_rollback,
                       temp_files, temp_bytes, pg_size_pretty(temp_bytes) AS temp_bytes_pretty,
                       conflicts, deadlocks
                FROM pg_stat_database
                ORDER BY temp_bytes DESC
                """,
            )
            result["largest_relations"] = _psycopg_rows(
                cursor,
                """
                SELECT n.nspname AS schema, c.relname AS relation, c.relkind,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_pretty,
                       pg_relation_size(c.oid) AS table_bytes,
                       pg_indexes_size(c.oid) AS indexes_bytes,
                       COALESCE(s.n_live_tup, 0) AS n_live_tup,
                       COALESCE(s.n_dead_tup, 0) AS n_dead_tup,
                       s.last_autovacuum, s.last_autoanalyze, s.autovacuum_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
                WHERE c.relkind IN ('r','p','m')
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT 40
                """,
            )
            result["largest_indexes"] = _psycopg_rows(
                cursor,
                """
                SELECT ns.nspname AS schema, idx.relname AS index_name,
                       tbl_ns.nspname AS table_schema, tbl.relname AS table_name,
                       pg_relation_size(idx.oid) AS size_bytes,
                       pg_size_pretty(pg_relation_size(idx.oid)) AS size_pretty,
                       ix.indisunique, ix.indisprimary
                FROM pg_class idx
                JOIN pg_index ix ON ix.indexrelid = idx.oid
                JOIN pg_class tbl ON tbl.oid = ix.indrelid
                JOIN pg_namespace ns ON ns.oid = idx.relnamespace
                JOIN pg_namespace tbl_ns ON tbl_ns.oid = tbl.relnamespace
                WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
                ORDER BY pg_relation_size(idx.oid) DESC
                LIMIT 30
                """,
            )
            result["dead_tuple_hotspots"] = _psycopg_rows(
                cursor,
                """
                SELECT schemaname, relname, n_live_tup, n_dead_tup,
                       CASE WHEN n_live_tup+n_dead_tup > 0
                            THEN round(100.0*n_dead_tup/(n_live_tup+n_dead_tup), 2)
                            ELSE 0 END AS dead_pct,
                       pg_total_relation_size(relid) AS total_bytes,
                       pg_size_pretty(pg_total_relation_size(relid)) AS total_pretty,
                       last_autovacuum, autovacuum_count
                FROM pg_stat_user_tables
                WHERE n_dead_tup > 100000
                ORDER BY n_dead_tup DESC
                LIMIT 20
                """,
            )
            result["external_pg_tblspc_targets"] = _psycopg_rows(
                cursor,
                """
                SELECT pg_tablespace_location(oid) AS target
                FROM pg_tablespace
                WHERE pg_tablespace_location(oid) <> ''
                ORDER BY target
                """,
            )
            result["cold_tablespace"] = _psycopg_rows(
                cursor,
                """
                SELECT spcname AS tablespace, pg_tablespace_location(oid) AS location
                FROM pg_tablespace
                WHERE spcname = 'nhms_cold'
                """,
            )
            result["cold_relation_by_tablespace"] = _psycopg_rows(
                cursor,
                """
                SELECT n.nspname AS schema, c.relname AS relation, c.oid,
                       pg_total_relation_size(c.oid) AS bytes
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_tablespace AS s ON s.oid = c.reltablespace
                WHERE s.spcname = 'nhms_cold'
                ORDER BY c.oid
                """,
            )
            try:
                result["hypertables"] = _psycopg_rows(
                    cursor,
                    """
                    SELECT h.hypertable_schema, h.hypertable_name, h.num_chunks,
                           h.compression_enabled,
                           r.job_id AS retention_job_id,
                           r.config AS retention_config,
                           c.job_id AS compression_job_id,
                           c.config AS compression_config
                    FROM timescaledb_information.hypertables h
                    LEFT JOIN timescaledb_information.jobs r
                      ON r.hypertable_schema = h.hypertable_schema
                     AND r.hypertable_name = h.hypertable_name
                     AND r.proc_name = 'policy_retention'
                    LEFT JOIN timescaledb_information.jobs c
                      ON c.hypertable_schema = h.hypertable_schema
                     AND c.hypertable_name = h.hypertable_name
                     AND c.proc_name = 'policy_compression'
                    ORDER BY h.hypertable_schema, h.hypertable_name
                    """,
                )
                rel_expr = "((quote_ident(chunk_schema) || '.' || quote_ident(chunk_name))::regclass)"
                result["hypertable_size_breakdown"] = _psycopg_rows(
                    cursor,
                    f"""
                    SELECT hypertable_schema, hypertable_name, count(*) AS chunks,
                           sum(pg_relation_size({rel_expr})) AS table_bytes,
                           sum(pg_indexes_size({rel_expr})) AS indexes_bytes,
                           sum(pg_total_relation_size({rel_expr})) AS total_bytes,
                           pg_size_pretty(sum(pg_relation_size({rel_expr}))) AS table_pretty,
                           pg_size_pretty(sum(pg_indexes_size({rel_expr}))) AS indexes_pretty,
                           pg_size_pretty(sum(pg_total_relation_size({rel_expr}))) AS total_pretty,
                           min(range_start) AS min_range_start,
                           max(range_end) AS max_range_end
                    FROM timescaledb_information.chunks
                    GROUP BY hypertable_schema, hypertable_name
                    ORDER BY sum(pg_total_relation_size({rel_expr})) DESC NULLS LAST
                    """,
                )
                result["largest_chunks"] = _psycopg_rows(
                    cursor,
                    f"""
                    SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
                           pg_total_relation_size({rel_expr}) AS total_bytes,
                           pg_size_pretty(pg_total_relation_size({rel_expr})) AS total_pretty,
                           pg_relation_size({rel_expr}) AS table_bytes,
                           pg_indexes_size({rel_expr}) AS indexes_bytes,
                           range_start, range_end
                    FROM timescaledb_information.chunks
                    ORDER BY pg_total_relation_size({rel_expr}) DESC
                    LIMIT 20
                    """,
                )
            except Exception as error:
                result["timescale_status"] = {"status": "blocked", "error": str(error)}
            # #1985 working set, in its OWN try: a failure of the inventory
            # queries above must not delete the capacity projection from the
            # receipt. Losing the block entirely is indistinguishable from
            # "nothing to project" downstream, so a failure here still leaves a
            # working_set -- carrying the unavailable status that pages.
            try:
                try:
                    watermark: datetime | None = fetch_display_watermark(database_url)
                except DisplayWatermarkError:
                    watermark = None
                result["working_set"] = collect_working_set(
                    cursor,
                    watermark=watermark,
                    lag_seconds=compression_lag_seconds,
                )
            except Exception as error:
                result["working_set"] = unavailable_working_set(
                    lag_seconds=compression_lag_seconds
                )
                result["working_set_error"] = str(error)
    except Exception as error:
        # Same principle as the timescale block above (round-2 review, decision
        # 20): a query that raised anywhere in this function must not take the
        # capacity projection down with it. `query_failed` means the audit DID
        # reach the database, so a missing `working_set` here would be read as
        # "nothing to project" and exit 0. It carries the unavailable sample
        # instead, which pages.
        result = {
            "status": "blocked",
            "reason": "query_failed",
            "error": str(error),
            "working_set": unavailable_working_set(lag_seconds=compression_lag_seconds),
        }
    finally:
        connection.close()
    return result


def observation_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _cold_relation_bytes(postgres: Mapping[str, Any] | object) -> tuple[int | None, str | None]:
    """Account cold-relation bytes only from an exact observed PostgreSQL inventory."""

    if not isinstance(postgres, Mapping) or postgres.get("status") != "ok":
        return None, "cold relation inventory is unavailable"
    if "cold_relation_by_tablespace" not in postgres:
        return None, "cold relation inventory is unavailable"
    cold_rows = postgres.get("cold_relation_by_tablespace")
    if not isinstance(cold_rows, list):
        return None, "cold relation inventory is unavailable"
    total = 0
    for row in cold_rows:
        if not isinstance(row, Mapping) or "bytes" not in row:
            return None, "cold relation inventory is unavailable"
        value = row["bytes"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, "cold relation inventory is unavailable"
        total += value
    return total, None


def cold_governance_sample(
    filesystem: Mapping[str, Any], postgres: Mapping[str, Any], *, path: str, observed_at: str
) -> dict[str, Any]:
    """Build one bounded category sample without recursively scanning shared roots."""

    filesystems = filesystem.get("filesystems") if isinstance(filesystem.get("filesystems"), Mapping) else {}
    path_sizes = filesystem.get("path_sizes") if isinstance(filesystem.get("path_sizes"), Mapping) else {}
    source = filesystems.get("home") if path == "/home" else filesystems.get("cold")
    source = source if isinstance(source, Mapping) else {}
    blockers: list[str] = []
    if source.get("status") != "ok":
        blockers.append(f"{path} disk observation is unavailable")
    identity = source.get("device_identity")
    if not isinstance(identity, str) or not identity:
        blockers.append(f"{path} filesystem identity is unavailable")
        identity = None
    total = observation_int(source.get("total_bytes"))
    free = observation_int(source.get("free_bytes"))
    used = observation_int(source.get("used_bytes"))
    reserved = observation_int(source.get("reserved_bytes"))
    if None in {total, free, used, reserved}:
        blockers.append(f"{path} capacity observation is incomplete")
    pgdata = path_sizes.get("pgdata_root") if isinstance(path_sizes.get("pgdata_root"), Mapping) else {}
    object_store_value = path_sizes.get("object_store_root")
    object_store = object_store_value if isinstance(object_store_value, Mapping) else {}
    pgdata_bytes = 0
    if path == "/home":
        if pgdata.get("status") != "ok" or observation_int(pgdata.get("bytes")) is None:
            blockers.append("PGDATA du observation is unavailable")
        else:
            pgdata_bytes = int(pgdata["bytes"])
    object_store_path = str(object_store.get("path") or "")
    if object_store_path.startswith("/home/"):
        object_store_on = "/home"
    elif object_store_path.startswith("/data/GHDC/"):
        object_store_on = "/data/GHDC"
    else:
        object_store_on = None
    object_store_bytes = 0
    if object_store_on == path:
        if object_store.get("status") != "ok" or observation_int(object_store.get("bytes")) is None:
            blockers.append("object-store du observation is unavailable")
        else:
            object_store_bytes = int(object_store["bytes"])
    cold_bytes: int | None = 0
    if path == "/data/GHDC":
        cold_bytes, inventory_blocker = _cold_relation_bytes(postgres)
        if inventory_blocker is not None:
            blockers.append(inventory_blocker)
    unavailable = bool(blockers)
    return {
        "path": path,
        "observed_at": observed_at,
        "identity": None if unavailable else identity,
        "total_bytes": None if unavailable else total,
        "free_bytes": None if unavailable else free,
        "used_bytes": None if unavailable else used,
        "reserved_bytes": None if unavailable else reserved,
        "pgdata_bytes": None if unavailable else pgdata_bytes,
        "nhms_cold_relation_bytes": None if unavailable else cold_bytes,
        "object_store_bytes": None if unavailable else object_store_bytes,
        "status": "unavailable" if unavailable else "ok",
        "blockers": blockers,
    }
