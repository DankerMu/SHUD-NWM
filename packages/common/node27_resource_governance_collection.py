"""Bounded filesystem, PostgreSQL, working-set and maintenance audit collection."""

from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.display_watermark import DisplayWatermarkError, fetch_display_watermark
from packages.common.node27_timeseries_discovery import RUNTIME_HYPERTABLES_SQL
from packages.common.redaction import redact_payload

COMPRESSION_LAG_SECONDS_ENV = "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS"
# Same lag as the compression lane (infra/env/node27-timeseries-compression.example).
# Do not invent a second lag.
COMPRESSION_LAG_DEFAULT_SECONDS = 172800

WORKING_SET_SQL = f"""
SELECT COALESCE(sum(pg_total_relation_size(
           format('%I.%I', chunk_schema, chunk_name)::regclass))
           FILTER (WHERE NOT is_compressed), 0) AS uncompressed_bytes,
       COALESCE(sum(pg_total_relation_size(
           format('%I.%I', chunk_schema, chunk_name)::regclass))
           FILTER (WHERE range_start >= CURRENT_TIMESTAMP - interval '7 days'
                     AND range_start < CURRENT_TIMESTAMP), 0) / 7.0 AS daily_ingest_bytes,
       min(range_end) FILTER (WHERE NOT is_compressed) AS oldest_uncompressed_range_end
FROM timescaledb_information.chunks
WHERE (hypertable_schema, hypertable_name) IN ({RUNTIME_HYPERTABLES_SQL})
"""


MAINTENANCE_OUTPUT_ROW_LIMIT = 50


def _reloption_or_setting(name: str) -> str:
    return (
        f"COALESCE((SELECT split_part(o, '=', 2) FROM unnest(c.reloptions) AS o "
        f"WHERE o LIKE '{name}=%' LIMIT 1)::float8, current_setting('{name}')::float8)"
    )


# Autovacuum output freshness (#1769): effective per-relation trigger thresholds
# (reloptions override, else cluster settings; negative reltuples count as 0),
# excluding relations autovacuum is told to skip (compressed-chunk phantom counters).
MAINTENANCE_RELATIONS_CTE = f"""
WITH rel AS (
  SELECT n.nspname AS schema,
         c.relname AS relation,
         c.relpages::bigint AS relpages,
         c.reltuples::float8 AS reltuples,
         s.n_live_tup::bigint AS n_live_tup,
         s.n_dead_tup::bigint AS n_dead_tup,
         s.n_mod_since_analyze::bigint AS n_mod_since_analyze,
         ({_reloption_or_setting("autovacuum_vacuum_threshold")}
          + {_reloption_or_setting("autovacuum_vacuum_scale_factor")}
            * greatest(c.reltuples::float8, 0))::float8 AS vacuum_threshold,
         ({_reloption_or_setting("autovacuum_analyze_threshold")}
          + {_reloption_or_setting("autovacuum_analyze_scale_factor")}
            * greatest(c.reltuples::float8, 0))::float8 AS analyze_threshold,
         s.last_autovacuum, s.last_vacuum, s.last_autoanalyze, s.last_analyze
  FROM pg_stat_all_tables AS s
  JOIN pg_class AS c ON c.oid = s.relid
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'm')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND n.nspname NOT LIKE 'pg\\_temp\\_%'
    AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%'
    AND NOT EXISTS (
      SELECT 1 FROM unnest(c.reloptions) AS o
      WHERE CASE WHEN o LIKE 'autovacuum\\_enabled=%' THEN NOT split_part(o, '=', 2)::boolean ELSE false END
    )
)
"""

MAINTENANCE_OUTPUT_ROWS_SQL = f"""
{MAINTENANCE_RELATIONS_CTE}
SELECT schema, relation, relpages, reltuples, n_live_tup, n_dead_tup, n_mod_since_analyze,
       vacuum_threshold, analyze_threshold,
       extract(epoch FROM now() - last_autovacuum)::float8 AS last_autovacuum_age_seconds,
       extract(epoch FROM now() - last_vacuum)::float8 AS last_vacuum_age_seconds,
       extract(epoch FROM now() - last_autoanalyze)::float8 AS last_autoanalyze_age_seconds,
       extract(epoch FROM now() - last_analyze)::float8 AS last_analyze_age_seconds
FROM rel
WHERE n_dead_tup > vacuum_threshold
   OR n_mod_since_analyze > analyze_threshold
   OR (relpages = 0 AND reltuples < 0 AND n_live_tup > 0
       AND last_autoanalyze IS NULL AND last_analyze IS NULL)
ORDER BY greatest(n_dead_tup / greatest(vacuum_threshold, 1), n_mod_since_analyze / greatest(analyze_threshold, 1))
         DESC NULLS LAST,
         schema, relation
LIMIT {MAINTENANCE_OUTPUT_ROW_LIMIT}
"""

MAINTENANCE_OUTPUT_SUMMARY_SQL = f"""
{MAINTENANCE_RELATIONS_CTE}
SELECT extract(epoch FROM now() - max(last_autovacuum))::float8 AS max_last_autovacuum_age_seconds,
       extract(epoch FROM now() - max(last_autoanalyze))::float8 AS max_last_autoanalyze_age_seconds,
       count(*) FILTER (WHERE n_dead_tup > vacuum_threshold)::bigint AS over_vacuum_threshold_count,
       count(*) FILTER (WHERE n_mod_since_analyze > analyze_threshold)::bigint AS over_analyze_threshold_count,
       count(*)::bigint AS relation_count
FROM rel
"""


def collect_maintenance_output(cursor: Any) -> dict[str, Any]:
    """Autovacuum/autoanalyze output probe; a failure is recorded, never raised."""
    try:
        rows = _psycopg_rows(cursor, MAINTENANCE_OUTPUT_ROWS_SQL)
        summary = _psycopg_rows(cursor, MAINTENANCE_OUTPUT_SUMMARY_SQL)
    except Exception as error:
        return {"status": "error", "error": type(error).__name__}
    return {"status": "ok", "summary": summary[0] if summary else {}, "rows": rows}


def compression_lag_seconds(env: Mapping[str, str] | None = None) -> int:
    """Prefer the compression-lane env, then the documented compression default."""
    values = os.environ if env is None else env
    raw = values.get(COMPRESSION_LAG_SECONDS_ENV)
    if raw is None or raw == "":
        return COMPRESSION_LAG_DEFAULT_SECONDS
    lag = int(raw)
    if lag < 1:
        raise ValueError("lag must be positive")
    return lag


def collect_working_set(database_url: str | None, filesystem: Mapping[str, Any]) -> dict[str, Any]:
    """Observe chunk sizes without scanning facts; never persist database errors."""
    target = (filesystem.get("filesystems") or {}).get("pgdata_root_fs", {})
    usage = (filesystem.get("path_sizes") or {}).get("pgdata_root", {})
    path = target.get("path")
    identity = target.get("device_identity")
    free = observation_int(target.get("free_bytes"))
    blockers = []
    if target.get("status") != "ok" or not isinstance(path, str) or not path:
        blockers.append("PGDATA_FILESYSTEM_UNAVAILABLE")
    if not isinstance(identity, str) or not identity:
        blockers.append("PGDATA_DEVICE_IDENTITY_UNAVAILABLE")
    if free is None:
        blockers.append("PGDATA_AVAILABLE_BYTES_UNAVAILABLE")
    binding_status = "unavailable" if blockers else "ok"
    usage_identity = usage.get("device_identity")
    if binding_status == "ok" and usage_identity and usage_identity != identity:
        binding_status = "ambiguous"
        blockers.append("PGDATA_DEVICE_IDENTITY_CONFLICT")
    binding = redact_payload(
        {
            "path": path,
            "device_identity": identity,
            "status": binding_status,
            "blockers": blockers,
        }
    )
    sample: dict[str, Any] = {
        "uncompressed_bytes": None,
        "daily_ingest_bytes": None,
        "next_compressible_at": None,
        "working_set_free_bytes": free if binding_status == "ok" else None,
        "working_set_filesystem": binding,
        "watermark": None,
        "projection_status": "catalog_unavailable",
    }
    connection = None
    try:
        import psycopg2
        import psycopg2.extras

        lag = compression_lag_seconds()
        connection = psycopg2.connect(database_url, connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor)
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '20s'")
            cursor.execute(WORKING_SET_SQL)
            row = cursor.fetchone()
        sample["uncompressed_bytes"] = int(row["uncompressed_bytes"])
        sample["daily_ingest_bytes"] = float(row["daily_ingest_bytes"])
        oldest_end = row["oldest_uncompressed_range_end"]
        if oldest_end is None:
            sample["projection_status"] = "no_uncompressed_chunk"
            return sample
        sample["next_compressible_at"] = (oldest_end + timedelta(seconds=lag)).isoformat()
    except Exception:
        return sample
    finally:
        if connection is not None:
            connection.close()
    try:
        sample["watermark"] = fetch_display_watermark(database_url).isoformat()
    except DisplayWatermarkError:
        sample["projection_status"] = "watermark_unavailable"
    else:
        sample["projection_status"] = "ok"
    return sample


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
    if getattr(info, "st_dev", None) is None or getattr(usage, "f_fsid", None) is None:
        return None
    return f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}:{usage.f_fsid}"


def disk_usage(path: Path) -> dict[str, Any]:
    resolved = safe_resolve(path)
    if resolved is None:
        return {"path": str(path), "status": "unavailable"}
    try:
        usage = os.statvfs(resolved)
        info = resolved.stat()
    except OSError:
        return {"path": str(resolved), "status": "unavailable", "error": "statvfs_or_stat_failed"}
    if getattr(info, "st_dev", None) is None or getattr(usage, "f_fsid", None) is None:
        return {"path": str(resolved), "status": "unavailable", "error": "device_identity_unavailable"}
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
                "device_identity": filesystem_identity(resolved),
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
                "device_identity": filesystem_identity(resolved),
            }
    return {
        "path": str(resolved),
        "status": "unavailable",
        "error": "du_failed",
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
        filesystems["pgdata_root_fs"] = disk_usage(config.pgdata_root)
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


def _psycopg_rows(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    return [dict(row) for row in cursor.fetchall()]


def collect_postgres(database_url: str | None) -> dict[str, Any]:
    if not database_url:
        return {"status": "skipped", "reason": "database_url_missing"}
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as error:  # pragma: no cover - environment dependent
        return {"status": "blocked", "reason": "psycopg2_unavailable", "error": type(error).__name__}
    try:
        connection = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as error:
        return {"status": "blocked", "reason": "connection_failed", "error": type(error).__name__}
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
            result["maintenance_output"] = collect_maintenance_output(cursor)
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
                result["timescale_status"] = {"status": "blocked", "error": type(error).__name__}
    except Exception as error:
        result = {"status": "blocked", "reason": "query_failed", "error": type(error).__name__}
    finally:
        connection.close()
    return result


def observation_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
