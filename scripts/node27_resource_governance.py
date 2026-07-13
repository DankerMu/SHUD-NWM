#!/usr/bin/env python
"""Emit a node-27 resource-governance audit receipt.

The script is intentionally read-only. It measures the production resource
surface, highlights policy gaps, and writes a bounded JSON receipt that can be
used before any destructive cleanup or database retention/compression change.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "nhms.node27_resource_governance.audit.v1"

DEFAULT_SERVICES = (
    "nhms-display-api.service",
    "nhms-node27-autopipe.service",
    "nhms-node27-autopipe.timer",
    "nhms-node27-download.service",
    "nhms-node27-download.timer",
    "nhms-node27-product-archive.service",
    "nhms-node27-product-archive.timer",
    "nhms-node27-raw-retention.service",
    "nhms-node27-raw-retention.timer",
    "nhms-node27-storage-inventory-audit.service",
    "nhms-node27-storage-inventory-audit.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-retention.service",
    "nhms-node27-timeseries-retention.timer",
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

GIB = 1024**3
MIB = 1024**2


@dataclass(frozen=True)
class AuditThresholds:
    root_free_warn_bytes: int = 20 * GIB
    root_free_critical_bytes: int = 10 * GIB
    home_free_warn_bytes: int = 300 * GIB
    database_warn_bytes: int = 300 * GIB
    database_critical_bytes: int = 500 * GIB
    index_ratio_warn: float = 2.0
    index_ratio_critical: float = 4.0
    temp_bytes_warn: int = 50 * GIB
    wal_warn_bytes: int = 10 * GIB
    dead_tuple_warn_pct: float = 10.0
    archive_free_warn_bytes: int | None = None
    archive_free_refuse_bytes: int | None = None


@dataclass(frozen=True)
class AuditConfig:
    repo_root: Path
    object_store_root: Path
    pgdata_root: Path | None
    database_url: str | None
    summary_path: Path | None
    services: tuple[str, ...]
    thresholds: AuditThresholds
    archive_root: Path | None = None


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _bytes_pretty(value: int | float | None) -> str | None:
    if value is None:
        return None
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _safe_resolve(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser()


def _disk_usage(path: Path) -> dict[str, Any]:
    resolved = _safe_resolve(path)
    if resolved is None:
        return {"path": str(path), "status": "unavailable"}
    try:
        usage = shutil.disk_usage(resolved)
    except OSError as error:
        return {"path": str(resolved), "status": "unavailable", "error": str(error)}
    return {
        "path": str(resolved),
        "status": "ok",
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_pct": round(100.0 * usage.used / usage.total, 3) if usage.total else None,
        "total_pretty": _bytes_pretty(usage.total),
        "used_pretty": _bytes_pretty(usage.used),
        "free_pretty": _bytes_pretty(usage.free),
    }


def _run_command(args: Sequence[str], *, timeout: int = 20) -> dict[str, Any]:
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


def _du_bytes(path: Path) -> dict[str, Any]:
    resolved = _safe_resolve(path)
    if resolved is None:
        return {"path": str(path), "status": "unavailable"}
    if not resolved.exists():
        return {"path": str(resolved), "status": "missing"}
    first = _run_command(["du", "-s", "-B1", str(resolved)])
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
                "pretty": _bytes_pretty(bytes_value),
            }
    fallback = _run_command(["du", "-sk", str(resolved)])
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
                "pretty": _bytes_pretty(bytes_value),
            }
    return {
        "path": str(resolved),
        "status": "unavailable",
        "error": fallback.get("stderr") or first.get("stderr") or "du_failed",
    }


def collect_archive_root(config: AuditConfig) -> dict[str, Any]:
    """Report archive-root shared-volume free space + on-disk footprint.

    Returns a benign "skipped" note when no archive root is configured so
    hosts without the archive lane still emit a full governance receipt.
    Free-space watermarks may independently be unset even when archive_root
    is configured — the receipt keeps the raw measurements and reports
    band="unconfigured" so future tuning can inspect production numbers
    before turning refusal on.
    """
    if config.archive_root is None:
        return {"status": "skipped", "reason": "archive_root_unset"}
    resolved = _safe_resolve(config.archive_root)
    if resolved is None:
        return {"path": str(config.archive_root), "status": "unavailable"}
    payload: dict[str, Any] = {"path": str(resolved), "status": "ok"}
    try:
        usage = shutil.disk_usage(resolved)
        payload["total_bytes"] = usage.total
        payload["free_bytes"] = usage.free
        payload["total_pretty"] = _bytes_pretty(usage.total)
        payload["free_pretty"] = _bytes_pretty(usage.free)
    except OSError as error:
        payload["status"] = "unavailable"
        payload["error"] = str(error)
        return payload
    du_result = _du_bytes(resolved)
    if du_result.get("status") == "ok":
        payload["used_bytes"] = du_result["bytes"]
        payload["used_pretty"] = du_result["pretty"]
    else:
        payload["used_bytes"] = None
        payload["used_status"] = du_result.get("status")
        if "error" in du_result:
            payload["used_error"] = du_result["error"]
    warn = config.thresholds.archive_free_warn_bytes
    refuse = config.thresholds.archive_free_refuse_bytes
    payload["warn_free_bytes"] = warn
    payload["refuse_free_bytes"] = refuse
    free = payload.get("free_bytes")
    if warn is not None and refuse is not None and isinstance(free, int):
        if free < refuse:
            payload["band"] = "refuse"
        elif free < warn:
            payload["band"] = "warn"
        else:
            payload["band"] = "clean"
    else:
        payload["band"] = "unconfigured"
    return payload


def collect_filesystem(config: AuditConfig) -> dict[str, Any]:
    filesystems = {
        "root": _disk_usage(Path("/")),
        "home": _disk_usage(Path("/home")),
        "repo_root_fs": _disk_usage(config.repo_root),
        "object_store_fs": _disk_usage(config.object_store_root),
    }
    path_sizes: dict[str, Any] = {
        "repo_root": _du_bytes(config.repo_root),
        "object_store_root": _du_bytes(config.object_store_root),
    }
    if config.pgdata_root is not None:
        path_sizes["pgdata_root"] = _du_bytes(config.pgdata_root)
        path_sizes["pg_wal"] = _du_bytes(config.pgdata_root / "pg_wal")
    for relative in DEFAULT_REPO_RELATIVE_SIZE_TARGETS:
        path_sizes[f"repo/{relative}"] = _du_bytes(config.repo_root / relative)
    for relative in DEFAULT_OBJECT_STORE_RELATIVE_SIZE_TARGETS:
        path_sizes[f"object-store/{relative}"] = _du_bytes(config.object_store_root / relative)
    for label, path in {
        "autopipe_logs": Path("/home/nwm/autopipe-logs"),
        "download_logs": Path("/home/nwm/node27-download-logs"),
        "raw_retention_logs": Path("/home/nwm/node27-raw-retention-logs"),
        "autopipe_work": Path("/home/nwm/autopipe-work"),
        "tmp": Path("/tmp"),
    }.items():
        path_sizes[label] = _du_bytes(path)
    return {
        "filesystems": filesystems,
        "path_sizes": path_sizes,
        "inode_usage": _run_command(["df", "-ih", "/", "/home"]),
        "journal_disk_usage": _run_command(["journalctl", "--disk-usage"]),
    }


def collect_systemd(services: Iterable[str]) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    for service in services:
        output = _run_command(
            [
                "systemctl",
                "--user",
                "--no-pager",
                "--plain",
                "show",
                service,
                "-p",
                "Id",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "Result",
                "-p",
                "ExecMainStatus",
                "-p",
                "MemoryCurrent",
                "-p",
                "NRestarts",
            ]
        )
        parsed: dict[str, str] = {}
        if output["status"] == "ok":
            for line in str(output.get("stdout", "")).splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    parsed[key] = value
        collected[service] = {"command": output, "properties": parsed}
    timers = _run_command(["systemctl", "--user", "list-timers", "--all", "--no-pager"])
    return {"services": collected, "timers": timers}


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


def _first_database_size(postgres: Mapping[str, Any], name: str = "nhms") -> int | None:
    for row in postgres.get("database_sizes", []) or []:
        if row.get("datname") == name:
            return int(row.get("bytes") or 0)
    return None


def _setting(postgres: Mapping[str, Any], name: str) -> str | None:
    for row in postgres.get("settings", []) or []:
        if row.get("name") == name:
            value = row.get("setting")
            return None if value is None else str(value)
    return None


def _temp_bytes(postgres: Mapping[str, Any], name: str = "nhms") -> int:
    for row in postgres.get("stat_database", []) or []:
        if row.get("datname") == name:
            return int(row.get("temp_bytes") or 0)
    return 0


def _recommendations(receipt: Mapping[str, Any], thresholds: AuditThresholds) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    fs = receipt.get("filesystem", {})
    root = (fs.get("filesystems") or {}).get("root", {})
    root_free = root.get("free_bytes")
    if isinstance(root_free, int | float):
        if root_free < thresholds.root_free_critical_bytes:
            recommendations.append(
                {
                    "severity": "critical",
                    "area": "filesystem",
                    "code": "ROOT_FREE_BELOW_CRITICAL",
                    "evidence": {"free_bytes": root_free, "free_pretty": _bytes_pretty(root_free)},
                    "action": "Root-only disk audit required before more large jobs run.",
                }
            )
        elif root_free < thresholds.root_free_warn_bytes:
            recommendations.append(
                {
                    "severity": "warning",
                    "area": "filesystem",
                    "code": "ROOT_FREE_BELOW_WARNING",
                    "evidence": {"free_bytes": root_free, "free_pretty": _bytes_pretty(root_free)},
                    "action": "Clean root-owned logs/tmp or expand root filesystem.",
                }
            )
    home = (fs.get("filesystems") or {}).get("home", {})
    home_free = home.get("free_bytes")
    if isinstance(home_free, int | float) and home_free < thresholds.home_free_warn_bytes:
        recommendations.append(
            {
                "severity": "warning",
                "area": "filesystem",
                "code": "HOME_FREE_BELOW_WARNING",
                "evidence": {"free_bytes": home_free, "free_pretty": _bytes_pretty(home_free)},
                "action": "Review repo runtime artifacts and database retention before backlog growth.",
            }
        )

    postgres = receipt.get("postgres", {})
    if postgres.get("status") == "ok":
        db_bytes = _first_database_size(postgres)
        if db_bytes is not None:
            if db_bytes >= thresholds.database_critical_bytes:
                severity = "critical"
                code = "DATABASE_SIZE_ABOVE_CRITICAL"
            elif db_bytes >= thresholds.database_warn_bytes:
                severity = "warning"
                code = "DATABASE_SIZE_ABOVE_WARNING"
            else:
                severity = None
                code = ""
            if severity is not None:
                recommendations.append(
                    {
                        "severity": severity,
                        "area": "postgres",
                        "code": code,
                        "evidence": {"database": "nhms", "bytes": db_bytes, "pretty": _bytes_pretty(db_bytes)},
                        "action": "Add Timescale retention/compression after validating display cold-read path.",
                    }
                )
        if _temp_bytes(postgres) > thresholds.temp_bytes_warn and _setting(postgres, "log_temp_files") == "-1":
            recommendations.append(
                {
                    "severity": "warning",
                    "area": "postgres",
                    "code": "TEMP_SPILL_LOGGING_DISABLED",
                    "evidence": {
                        "temp_bytes": _temp_bytes(postgres),
                        "temp_pretty": _bytes_pretty(_temp_bytes(postgres)),
                        "log_temp_files": "-1",
                    },
                    "action": "Enable bounded log_temp_files to identify spill-heavy queries.",
                }
            )
        for row in postgres.get("hypertables", []) or []:
            name = f"{row.get('hypertable_schema')}.{row.get('hypertable_name')}"
            if row.get("hypertable_name") in {"river_timeseries", "forcing_station_timeseries"}:
                if not row.get("retention_job_id"):
                    recommendations.append(
                        {
                            "severity": "warning",
                            "area": "postgres",
                            "code": "TIMESCALE_RETENTION_POLICY_MISSING",
                            "evidence": {"hypertable": name, "num_chunks": row.get("num_chunks")},
                            "action": "Define retention policy after verifying object-store replay evidence.",
                        }
                    )
                if not row.get("compression_enabled") or not row.get("compression_job_id"):
                    recommendations.append(
                        {
                            "severity": "warning",
                            "area": "postgres",
                            "code": "TIMESCALE_COMPRESSION_POLICY_MISSING",
                            "evidence": {
                                "hypertable": name,
                                "compression_enabled": row.get("compression_enabled"),
                                "compression_job_id": row.get("compression_job_id"),
                            },
                            "action": "Dry-run compression settings and query plans before enabling.",
                        }
                    )
        for row in postgres.get("hypertable_size_breakdown", []) or []:
            table_bytes = float(row.get("table_bytes") or 0)
            index_bytes = float(row.get("indexes_bytes") or 0)
            if table_bytes <= 0:
                continue
            ratio = index_bytes / table_bytes
            if ratio >= thresholds.index_ratio_critical:
                severity = "critical"
            elif ratio >= thresholds.index_ratio_warn:
                severity = "warning"
            else:
                continue
            recommendations.append(
                {
                    "severity": severity,
                    "area": "postgres",
                    "code": "HYPERTABLE_INDEX_RATIO_HIGH",
                    "evidence": {
                        "hypertable": f"{row.get('hypertable_schema')}.{row.get('hypertable_name')}",
                        "table_bytes": int(table_bytes),
                        "indexes_bytes": int(index_bytes),
                        "index_to_table_ratio": round(ratio, 3),
                    },
                    "action": "Audit overlapping display/MVT indexes with EXPLAIN before adding more indexes.",
                }
            )
        for row in postgres.get("dead_tuple_hotspots", []) or []:
            dead_pct = float(row.get("dead_pct") or 0)
            if dead_pct >= thresholds.dead_tuple_warn_pct:
                recommendations.append(
                    {
                        "severity": "warning",
                        "area": "postgres",
                        "code": "DEAD_TUPLE_HOTSPOT",
                        "evidence": {
                            "relation": f"{row.get('schemaname')}.{row.get('relname')}",
                            "dead_pct": dead_pct,
                            "n_dead_tup": row.get("n_dead_tup"),
                            "total_pretty": row.get("total_pretty"),
                        },
                        "action": "Let autovacuum finish or schedule manual VACUUM during a quiet window.",
                    }
                )
    archive = receipt.get("archive_root")
    if isinstance(archive, Mapping) and archive.get("status") == "ok":
        band = archive.get("band")
        free_bytes = archive.get("free_bytes")
        if band == "refuse":
            recommendations.append(
                {
                    "severity": "critical",
                    "area": "archive",
                    "code": "ARCHIVE_FREE_BELOW_REFUSE",
                    "evidence": {
                        "archive_root": archive.get("path"),
                        "free_bytes": free_bytes,
                        "free_pretty": archive.get("free_pretty"),
                        "refuse_free_bytes": archive.get("refuse_free_bytes"),
                    },
                    "action": (
                        "Archive-root shared volume free space is below the refuse "
                        "watermark; the mover will refuse enforce until space is freed."
                    ),
                }
            )
        elif band == "warn":
            recommendations.append(
                {
                    "severity": "warning",
                    "area": "archive",
                    "code": "ARCHIVE_FREE_BELOW_WARN",
                    "evidence": {
                        "archive_root": archive.get("path"),
                        "free_bytes": free_bytes,
                        "free_pretty": archive.get("free_pretty"),
                        "warn_free_bytes": archive.get("warn_free_bytes"),
                    },
                    "action": (
                        "Archive-root shared volume free space is below the warn "
                        "watermark; review retention/backlog before the refuse gate fires."
                    ),
                }
            )
    return recommendations


def build_receipt(config: AuditConfig) -> dict[str, Any]:
    started_at = _utc_now()
    filesystem = collect_filesystem(config)
    postgres = collect_postgres(config.database_url)
    systemd = collect_systemd(config.services)
    archive_root = collect_archive_root(config)
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "execution_mode": "read_only_audit",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "paths": {
            "repo_root": str(config.repo_root),
            "object_store_root": str(config.object_store_root),
            "pgdata_root": str(config.pgdata_root) if config.pgdata_root is not None else None,
            "archive_root": str(config.archive_root) if config.archive_root is not None else None,
        },
        "filesystem": filesystem,
        "postgres": postgres,
        "systemd": systemd,
        "archive_root": archive_root,
        "safety": {
            "database_url_redacted": bool(config.database_url),
            "destructive_actions_enabled": False,
            "notes": [
                "This receipt is read-only.",
                "It does not drop chunks, vacuum full, delete object-store artifacts, or modify systemd units.",
            ],
        },
    }
    receipt["recommendations"] = _recommendations(receipt, config.thresholds)
    return receipt


def _write_summary(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise ValueError(f"summary path must be absolute: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    temporary.replace(path)


def _positive_bytes(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer byte count") from error
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=os.getenv("NODE27_GOVERNANCE_REPO_ROOT", "/home/nwm/NWM"))
    parser.add_argument(
        "--object-store-root",
        default=os.getenv("NODE27_GOVERNANCE_OBJECT_STORE_ROOT")
        or os.getenv("OBJECT_STORE_ROOT")
        or "/home/ghdc/nwm/object-store",
    )
    parser.add_argument(
        "--pgdata-root",
        default=os.getenv("NODE27_GOVERNANCE_PGDATA_ROOT") or "/home/nwm/nhms-pgdata",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--summary-path", default=os.getenv("NODE27_GOVERNANCE_SUMMARY_PATH"))
    parser.add_argument(
        "--archive-root",
        default=None,
        help=(
            "Absolute archive root; env fallback NODE27_GOVERNANCE_ARCHIVE_ROOT "
            "or NHMS_ARCHIVE_ROOT."
        ),
    )
    parser.add_argument("--service", dest="services", action="append", default=[])
    parser.add_argument(
        "--root-free-warn-bytes",
        type=lambda raw: _positive_bytes(raw, label="root-free-warn-bytes"),
        default=AuditThresholds.root_free_warn_bytes,
    )
    parser.add_argument(
        "--root-free-critical-bytes",
        type=lambda raw: _positive_bytes(raw, label="root-free-critical-bytes"),
        default=AuditThresholds.root_free_critical_bytes,
    )
    parser.add_argument(
        "--home-free-warn-bytes",
        type=lambda raw: _positive_bytes(raw, label="home-free-warn-bytes"),
        default=AuditThresholds.home_free_warn_bytes,
    )
    parser.add_argument(
        "--database-warn-bytes",
        type=lambda raw: _positive_bytes(raw, label="database-warn-bytes"),
        default=AuditThresholds.database_warn_bytes,
    )
    parser.add_argument(
        "--database-critical-bytes",
        type=lambda raw: _positive_bytes(raw, label="database-critical-bytes"),
        default=AuditThresholds.database_critical_bytes,
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print the full receipt to stdout.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def _parse_archive_free_space_watermarks() -> tuple[int | None, int | None]:
    """Strict integer parse of archive free-space warn/refuse env watermarks.

    Missing (both env vars absent) means the archive-free-space report skips
    the band classification and returns (None, None). Every other invalid
    shape (partial configuration, empty string, non-integer, non-positive,
    refuse >= warn) is a fail-closed governance error rather than a silent
    accept — the audit must not fabricate green bands.
    """
    warn_raw = os.environ.get("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES")
    refuse_raw = os.environ.get("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES")
    if warn_raw is None and refuse_raw is None:
        return None, None
    if warn_raw is None or refuse_raw is None:
        raise ValueError(
            "archive free-space watermarks NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES and "
            "NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES must be set together"
        )
    try:
        warn = int(warn_raw)
        refuse = int(refuse_raw)
    except ValueError as error:
        raise ValueError("archive free-space watermarks must be integer byte counts") from error
    if warn <= 0 or refuse <= 0:
        raise ValueError("archive free-space watermarks must be positive")
    if refuse >= warn:
        raise ValueError("archive refuse watermark must be strictly less than warn watermark")
    return warn, refuse


def config_from_args(args: argparse.Namespace) -> AuditConfig:
    archive_warn, archive_refuse = _parse_archive_free_space_watermarks()
    thresholds = AuditThresholds(
        root_free_warn_bytes=args.root_free_warn_bytes,
        root_free_critical_bytes=args.root_free_critical_bytes,
        home_free_warn_bytes=args.home_free_warn_bytes,
        database_warn_bytes=args.database_warn_bytes,
        database_critical_bytes=args.database_critical_bytes,
        archive_free_warn_bytes=archive_warn,
        archive_free_refuse_bytes=archive_refuse,
    )
    pgdata_root = Path(args.pgdata_root).expanduser() if args.pgdata_root else None
    summary_path = Path(args.summary_path).expanduser() if args.summary_path else None
    archive_raw = (
        args.archive_root
        or os.getenv("NODE27_GOVERNANCE_ARCHIVE_ROOT")
        or os.getenv("NHMS_ARCHIVE_ROOT")
    )
    archive_root = Path(archive_raw).expanduser() if archive_raw else None
    return AuditConfig(
        repo_root=Path(args.repo_root).expanduser(),
        object_store_root=Path(args.object_store_root).expanduser(),
        pgdata_root=pgdata_root,
        database_url=args.database_url,
        summary_path=summary_path,
        services=tuple(args.services or DEFAULT_SERVICES),
        thresholds=thresholds,
        archive_root=archive_root,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
    except ValueError as error:
        print(
            json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    receipt = build_receipt(config)
    if config.summary_path is not None:
        _write_summary(config.summary_path, receipt)
    if not args.quiet:
        indent = 2 if args.pretty else None
        print(json.dumps(receipt, indent=indent, sort_keys=True, default=_json_default))
    return 0 if receipt.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
