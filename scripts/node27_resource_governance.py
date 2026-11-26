#!/usr/bin/env python
"""Emit a node-27 resource-governance audit receipt.

The script is intentionally read-only. It measures the production resource
surface, highlights policy gaps, and writes a bounded JSON receipt that can be
used before any destructive cleanup or database retention/compression change.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.common.node27_resource_governance_collection import (
    bytes_pretty as _bytes_pretty,
)
from packages.common.node27_resource_governance_collection import (
    collect_filesystem,
    collect_postgres,
    collect_working_set,
)
from packages.common.node27_resource_governance_collection import (
    run_command as _run_command,
)
from packages.common.redaction import redact_payload

SCHEMA_VERSION = "nhms.node27_resource_governance.audit.v1"

DEFAULT_SERVICES = (
    "nhms-display-api.service",
    "nhms-node27-autopipe.service",
    "nhms-node27-autopipe.timer",
    "nhms-node27-coverage-freshness-alert.service",
    "nhms-node27-coverage-freshness-alert.timer",
    "nhms-node27-download@GFS.service",
    "nhms-node27-download@GFS.timer",
    "nhms-node27-download@IFS.service",
    "nhms-node27-download@IFS.timer",
    "nhms-node27-frontier-alert.service",
    "nhms-node27-frontier-alert.timer",
    "nhms-node27-raw-retention.service",
    "nhms-node27-raw-retention.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-retention.service",
    "nhms-node27-timeseries-retention.timer",
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
    safety_margin_bytes: int = 100 * GIB
    runway_critical_days: float = 3.0
    working_set_warn_bytes: int = 400 * GIB
    index_ratio_warn: float = 2.0
    index_ratio_critical: float = 4.0
    temp_bytes_warn: int = 50 * GIB
    wal_warn_bytes: int = 10 * GIB
    dead_tuple_warn_pct: float = 10.0
    maintenance_stale_multiplier: float = 10.0
    maintenance_stale_age_seconds: float = 86400.0


@dataclass(frozen=True)
class AuditConfig:
    repo_root: Path
    object_store_root: Path
    pgdata_root: Path | None
    database_url: str | None
    summary_path: Path | None
    services: tuple[str, ...]
    thresholds: AuditThresholds


def _utc_now_seconds_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


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
                "-p",
                "LoadState",
                "-p",
                "UnitFileState",
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


RUNWAY_CRITICAL_CODE = "PGDATA_INGEST_RUNWAY_BELOW_CRITICAL"
# Catalog statuses after which the working-set fields (rate included) were read.
CATALOG_OBSERVED_STATUSES = frozenset({"ok", "no_uncompressed_chunk", "watermark_unavailable"})


def _capacity_valid(working_set: Mapping[str, Any]) -> bool:
    """The configured PGDATA filesystem was observed directly and unambiguously."""
    binding = working_set.get("working_set_filesystem") or {}
    available = working_set.get("working_set_free_bytes")
    return (
        binding.get("status") == "ok"
        and bool(binding.get("path"))
        and bool(binding.get("device_identity"))
        and binding.get("blockers") == []
        and isinstance(available, int)
        and not isinstance(available, bool)
        and available >= 0
    )


def _arrival_rate(working_set: Mapping[str, Any]) -> float | None:
    """`daily_arrival_bytes` as a finite non-negative number, else None (absent/bool/NaN/inf/negative)."""
    rate = _number(working_set.get("daily_arrival_bytes"))
    return rate if rate is not None and rate >= 0 else None


def _ingest_runway_days(working_set: Mapping[str, Any], thresholds: AuditThresholds) -> float | None:
    """Days of ingest the PGDATA device takes before it is down to the margin; may be negative.

    None when the catalog observation failed, capacity is not valid, or the rate is zero or unusable.
    """
    if working_set.get("projection_status") not in CATALOG_OBSERVED_STATUSES or not _capacity_valid(working_set):
        return None
    rate = _arrival_rate(working_set)
    if rate is None or rate == 0:
        return None
    return (working_set["working_set_free_bytes"] - thresholds.safety_margin_bytes) / rate


def _recommendations(receipt: Mapping[str, Any], thresholds: AuditThresholds) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    working_set = receipt.get("working_set", {})
    status = working_set.get("projection_status")
    available = working_set.get("working_set_free_bytes")
    capacity_valid = _capacity_valid(working_set)
    if not capacity_valid:
        recommendations.append(
            {
                "severity": "critical",
                "area": "filesystem",
                "code": "WORKING_SET_FILESYSTEM_UNAVAILABLE",
                "evidence": dict(working_set),
                "action": "Restore configured PGDATA filesystem capacity and device observations.",
            }
        )
    usage = ((receipt.get("filesystem") or {}).get("path_sizes") or {}).get("pgdata_root", {})
    usage_bytes = usage.get("bytes")
    if (
        usage.get("status") != "ok"
        or not isinstance(usage_bytes, int)
        or isinstance(usage_bytes, bool)
        or usage_bytes < 0
    ):
        recommendations.append(
            {
                "severity": "critical",
                "area": "filesystem",
                "code": "PGDATA_USAGE_UNAVAILABLE",
                "evidence": dict(usage),
                "action": "Restore the existing configured PGDATA usage audit.",
            }
        )
    if status in {"watermark_unavailable", "catalog_unavailable"}:
        recommendations.append(
            {
                "severity": "critical",
                "area": "postgres",
                "code": "WATERMARK_UNAVAILABLE" if status == "watermark_unavailable" else "WORKING_SET_UNAVAILABLE",
                "evidence": dict(working_set),
                "action": "Restore working-set catalog and display watermark observations before assessing capacity.",
            }
        )
    uncompressed = working_set.get("uncompressed_bytes")
    if isinstance(uncompressed, int | float) and uncompressed > thresholds.working_set_warn_bytes:
        recommendations.append(
            {
                "severity": "warning",
                "area": "postgres",
                "code": "WORKING_SET_ABOVE_WARNING",
                "evidence": dict(working_set),
                "action": "Review the uncompressed working set and compression cadence.",
            }
        )
    # #3379: the critical is the ingest runway of the PGDATA device, not the
    # projected peak (evidence only, read by no rule). It does not wait for
    # the watermark; a zero rate leaves `free < margin` by the same expression.
    if status in CATALOG_OBSERVED_STATUSES:
        rate = _arrival_rate(working_set)
        if rate is None:
            recommendations.append(
                {
                    "severity": "critical",
                    "area": "postgres",
                    "code": "WORKING_SET_UNAVAILABLE",
                    "evidence": dict(working_set),
                    "action": (
                        "daily_arrival_bytes is missing, negative or not finite after a successful catalog "
                        "observation; restore the uncompressed-chunk read before assessing the ingest runway."
                    ),
                }
            )
        elif capacity_valid and available - thresholds.safety_margin_bytes < thresholds.runway_critical_days * rate:
            recommendations.append(
                {
                    "severity": "critical",
                    "area": "postgres",
                    "code": RUNWAY_CRITICAL_CODE,
                    "evidence": {
                        **working_set,
                        "safety_margin_bytes": thresholds.safety_margin_bytes,
                        "runway_critical_days": thresholds.runway_critical_days,
                    },
                    "action": (
                        "Free space on the PGDATA filesystem or restore compression: at the measured arrival "
                        "rate it is down to the safety margin in fewer days than runway_critical_days."
                    ),
                }
            )
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
                severity = "info"
                code = "DATABASE_SIZE_ABOVE_CRITICAL"
            elif db_bytes >= thresholds.database_warn_bytes:
                severity = "info"
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
        recommendations.extend(_maintenance_output_recommendations(postgres, thresholds))
    return recommendations


_MISSING = object()


def _number(value: Any) -> float | None:
    """A finite JSON/DB number, else None (bool, str and NaN are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _age(row: Mapping[str, Any], key: str) -> float | None | object:
    """Age in seconds, None when the event never happened, `_MISSING` when malformed."""
    value = row.get(key, _MISSING)
    if value is None:
        return None
    number = _number(value)
    return _MISSING if number is None else number


def _output_is_stale(ages: Iterable[float | None], stale_age_seconds: float) -> bool:
    """The newer of the observed outputs is absent or older than the stale age."""
    present = [age for age in ages if age is not None]
    return not present or min(present) > stale_age_seconds


def _maintenance_output_recommendations(
    postgres: Mapping[str, Any], thresholds: AuditThresholds
) -> list[dict[str, Any]]:
    section = postgres.get("maintenance_output")
    summary = section.get("summary") if isinstance(section, Mapping) else None
    rows = section.get("rows") if isinstance(section, Mapping) else None
    summary_ages = (
        {key: _age(summary, key) for key in ("max_last_autoanalyze_age_seconds", "max_last_autovacuum_age_seconds")}
        if isinstance(summary, Mapping)
        else {}
    )
    if (
        not isinstance(section, Mapping)
        or section.get("status") != "ok"
        or not isinstance(rows, list)
        or not summary_ages
        or _MISSING in summary_ages.values()
    ):
        return [
            {
                "severity": "warning",
                "area": "postgres",
                "code": "MAINTENANCE_OUTPUT_UNAVAILABLE",
                "evidence": {
                    "status": section.get("status") if isinstance(section, Mapping) else None,
                    "error": section.get("error") if isinstance(section, Mapping) else None,
                },
                "action": "Restore the autovacuum output probe; unobserved maintenance output is not healthy.",
            }
        ]

    multiplier = thresholds.maintenance_stale_multiplier
    stale_age = thresholds.maintenance_stale_age_seconds
    stale: list[dict[str, Any]] = []
    zero_statistics: list[dict[str, Any]] = []
    analyze_stale = 0
    vacuum_stale = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        schema, relation = row.get("schema"), row.get("relation")
        if not isinstance(schema, str) or not isinstance(relation, str):
            continue
        name = f"{schema}.{relation}"
        ages = {
            key: _age(row, key)
            for key in (
                "last_autoanalyze_age_seconds",
                "last_analyze_age_seconds",
                "last_autovacuum_age_seconds",
                "last_vacuum_age_seconds",
            )
        }
        if _MISSING in ages.values():
            continue
        for kind, count_key, threshold_key, age_keys in (
            ("analyze", "n_mod_since_analyze", "analyze_threshold", ("last_autoanalyze", "last_analyze")),
            ("vacuum", "n_dead_tup", "vacuum_threshold", ("last_autovacuum", "last_vacuum")),
        ):
            count, threshold = _number(row.get(count_key)), _number(row.get(threshold_key))
            if count is None or threshold is None or threshold < 0:
                continue
            output_ages = [ages[f"{key}_age_seconds"] for key in age_keys]
            if count <= multiplier * threshold or not _output_is_stale(output_ages, stale_age):
                continue
            if kind == "analyze":
                analyze_stale += 1
            else:
                vacuum_stale += 1
            stale.append(
                {
                    "severity": "warning",
                    "area": "postgres",
                    "code": "TABLE_STATISTICS_STALE" if kind == "analyze" else "TABLE_VACUUM_DEBT_STALE",
                    "evidence": {
                        "relation": name,
                        count_key: int(count),
                        threshold_key: threshold,
                        "ratio": round(count / threshold, 1) if threshold > 0 else None,
                        **{f"{key}_age_seconds": ages[f"{key}_age_seconds"] for key in age_keys},
                    },
                    "action": (
                        "Autoanalyze is not keeping up with this relation; check autovacuum workers and counters."
                        if kind == "analyze"
                        else "Autovacuum is not keeping up with this relation; check workers, xmin horizon and locks."
                    ),
                }
            )
        relpages, reltuples, live = (_number(row.get(key)) for key in ("relpages", "reltuples", "n_live_tup"))
        if (
            relpages == 0
            and reltuples is not None
            and reltuples < 0
            and live is not None
            and live > 0
            and ages["last_autoanalyze_age_seconds"] is None
            and ages["last_analyze_age_seconds"] is None
        ):
            zero_statistics.append(
                {
                    "severity": "info",
                    "area": "postgres",
                    "code": "TABLE_ZERO_STATISTICS",
                    "evidence": {
                        "relation": name,
                        "relpages": int(relpages),
                        "reltuples": reltuples,
                        "n_live_tup": int(live),
                        "n_mod_since_analyze": row.get("n_mod_since_analyze"),
                    },
                    "action": "Planner has no statistics yet; autoanalyze runs once modifications pass the threshold.",
                }
            )

    findings = list(stale)
    max_autoanalyze_age = summary_ages["max_last_autoanalyze_age_seconds"]
    max_autovacuum_age = summary_ages["max_last_autovacuum_age_seconds"]
    stalled_outputs = []
    if analyze_stale and _output_is_stale([max_autoanalyze_age], stale_age):
        stalled_outputs.append("autoanalyze")
    if vacuum_stale and _output_is_stale([max_autovacuum_age], stale_age):
        stalled_outputs.append("autovacuum")
    if stalled_outputs:
        findings.append(
            {
                "severity": "critical",
                "area": "postgres",
                "code": "AUTOVACUUM_OUTPUT_STALLED",
                "evidence": {
                    "stalled_outputs": stalled_outputs,
                    "statistics_stale_relations": analyze_stale,
                    "vacuum_debt_stale_relations": vacuum_stale,
                    "max_last_autoanalyze_age_seconds": max_autoanalyze_age,
                    "max_last_autovacuum_age_seconds": max_autovacuum_age,
                    "stale_age_seconds": stale_age,
                },
                "action": (
                    "Database-wide autovacuum output is silent while tables demand it: check the launcher, "
                    "track_counts, and whether a crash restart wiped the statistics counters."
                ),
            }
        )
    findings.extend(zero_statistics)
    return findings


def build_receipt(config: AuditConfig) -> dict[str, Any]:
    started_at = _utc_now_seconds_text()
    filesystem = collect_filesystem(config)
    postgres = collect_postgres(config.database_url)
    systemd = collect_systemd(config.services)
    working_set = collect_working_set(
        config.database_url,
        filesystem,
    )
    uncompressed = working_set["uncompressed_bytes"]
    working_set["projected_peak_bytes"] = None
    if working_set["projection_status"] == "no_uncompressed_chunk":
        working_set["projected_peak_bytes"] = uncompressed
    elif working_set["projection_status"] == "ok":
        days = max(
            0.0,
            (
                datetime.fromisoformat(working_set["next_compressible_at"])
                - datetime.fromisoformat(working_set["watermark"])
            ).total_seconds()
            / 86400,
        )
        working_set["projected_peak_bytes"] = uncompressed + working_set["daily_ingest_bytes"] * days
    working_set["ingest_runway_days"] = _ingest_runway_days(working_set, config.thresholds)
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "execution_mode": "read_only_audit",
        "started_at": started_at,
        "finished_at": _utc_now_seconds_text(),
        "paths": {
            "repo_root": str(config.repo_root),
            "object_store_root": str(config.object_store_root),
            "pgdata_root": str(config.pgdata_root) if config.pgdata_root is not None else None,
        },
        "filesystem": filesystem,
        "postgres": postgres,
        "systemd": systemd,
        "working_set": working_set,
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
    return redact_payload(receipt)


def _write_summary(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise ValueError(f"summary path must be absolute: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(redact_payload(payload), indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _positive_bytes(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer byte count") from error
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    return value


def _nonnegative_bytes(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer byte count") from error
    if value < 0:
        raise argparse.ArgumentTypeError(f"{label} must be non-negative")
    return value


def _positive_finite_days(raw: str, *, label: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be a number of days") from error
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be finite and positive")
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
    parser.add_argument(
        "--safety-margin-bytes",
        type=lambda raw: _nonnegative_bytes(raw, label="safety-margin-bytes"),
        default=AuditThresholds.safety_margin_bytes,
    )
    parser.add_argument(
        "--runway-critical-days",
        type=lambda raw: _positive_finite_days(raw, label="runway-critical-days"),
        default=AuditThresholds.runway_critical_days,
    )
    parser.add_argument(
        "--working-set-warn-bytes",
        type=lambda raw: _nonnegative_bytes(raw, label="working-set-warn-bytes"),
        default=AuditThresholds.working_set_warn_bytes,
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print the full receipt to stdout.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> AuditConfig:
    thresholds = AuditThresholds(
        root_free_warn_bytes=args.root_free_warn_bytes,
        root_free_critical_bytes=args.root_free_critical_bytes,
        home_free_warn_bytes=args.home_free_warn_bytes,
        database_warn_bytes=args.database_warn_bytes,
        database_critical_bytes=args.database_critical_bytes,
        safety_margin_bytes=args.safety_margin_bytes,
        runway_critical_days=args.runway_critical_days,
        working_set_warn_bytes=args.working_set_warn_bytes,
    )
    pgdata_root = Path(args.pgdata_root).expanduser() if args.pgdata_root else None
    summary_path = Path(args.summary_path).expanduser() if args.summary_path else None
    return AuditConfig(
        repo_root=Path(args.repo_root).expanduser(),
        object_store_root=Path(args.object_store_root).expanduser(),
        pgdata_root=pgdata_root,
        database_url=args.database_url,
        summary_path=summary_path,
        services=tuple(args.services or DEFAULT_SERVICES),
        thresholds=thresholds,
    )


# #1765: the audit measured `/` below its own critical threshold every day
# while the root volume filled, and still exited 0 with no `OnFailure=` on its
# unit — the signal existed and was structurally unable to reach anyone. This
# is the stderr anchor the unit routes to the journal and the alert handler
# quotes; the receipt is NOT changed (`status` stays `completed`: the audit
# did complete, it is the finding that is critical).
CRITICAL_DIAGNOSTIC_PREFIX = "RESOURCE_GOVERNANCE_CRITICAL:"


def _critical_codes(receipt: Mapping[str, Any]) -> list[str]:
    """Codes of every `critical` recommendation, in receipt order."""
    recommendations = receipt.get("recommendations")
    if not isinstance(recommendations, list):
        return []
    return [
        str(item.get("code"))
        for item in recommendations
        if isinstance(item, Mapping) and item.get("severity") == "critical"
    ]


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
    if receipt.get("status") != "completed":
        return 1
    # Receipt first, exit code second: a critical finding must never cost the
    # evidence. `--quiet` suppresses stdout only — the wrapper runs with it, so
    # this stderr line is the only thing the journal (and therefore the
    # `OnFailure=` mail body) can quote.
    critical_codes = _critical_codes(receipt)
    recommendations = receipt.get("recommendations")
    for item in recommendations if isinstance(recommendations, list) else []:
        if not isinstance(item, Mapping) or item.get("severity") != "critical":
            continue
        code = str(item.get("code"))
        print(f"{CRITICAL_DIAGNOSTIC_PREFIX}{code}", file=sys.stderr)
        for line in _critical_detail_lines(code, item, receipt):
            print(line, file=sys.stderr)
    return 1 if critical_codes else 0


def _diagnostic_fields(label: str, values: Mapping[str, Any], names: tuple[str, ...]) -> str:
    safe = redact_payload({name: values.get(name) for name in names})
    return label + " ".join(f"{name}={safe[name]}" for name in names)


def _critical_detail_lines(code: str, item: Mapping[str, Any], receipt: Mapping[str, Any]) -> list[str]:
    """The numbers an operator needs in the OnFailure mail, for the capacity criticals only."""
    working_set = receipt.get("working_set") or {}
    if code == RUNWAY_CRITICAL_CODE:
        evidence = item.get("evidence")
        lines = [
            _diagnostic_fields(
                "ingest runway: ",
                evidence if isinstance(evidence, Mapping) else {},
                (
                    "ingest_runway_days",
                    "daily_arrival_bytes",
                    "working_set_free_bytes",
                    "safety_margin_bytes",
                    "uncompressed_bytes",
                ),
            )
        ]
    elif code in {"WORKING_SET_FILESYSTEM_UNAVAILABLE", "PGDATA_USAGE_UNAVAILABLE"}:
        usage = ((receipt.get("filesystem") or {}).get("path_sizes") or {}).get("pgdata_root") or {}
        values = {
            "working_set_free_bytes": working_set.get("working_set_free_bytes"),
            "pgdata_usage_status": usage.get("status"),
        }
        lines = [_diagnostic_fields("working-set capacity: ", values, tuple(values))]
    else:
        return []
    filesystem = redact_payload(working_set.get("working_set_filesystem", {}))
    lines.append("working-set filesystem: " + json.dumps(filesystem, sort_keys=True))
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
