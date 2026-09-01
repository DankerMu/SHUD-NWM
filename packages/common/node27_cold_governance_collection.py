"""Bounded statvfs/du/PostgreSQL sample collection for cold governance.

The operational script remains CLI/orchestration.  This owner observes disk
capacity, path sizes, and PostgreSQL relation inventory, and refuses to
account unobserved cold-relation bytes as zero.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

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
        return {"status": "blocked", "reason": "psycopg2_unavailable", "error": str(error)}
    try:
        connection = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
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
    except Exception as error:
        result = {"status": "blocked", "reason": "query_failed", "error": str(error)}
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
