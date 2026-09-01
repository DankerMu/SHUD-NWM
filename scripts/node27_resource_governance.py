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
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.common.node27_cold_governance import (
    GovernanceConfig,
    build_cold_governance_receipt,
    write_cold_governance_receipt,
)
from packages.common.node27_cold_governance_cli import (
    add_cold_governance_arguments,
    validate_cold_governance_arguments,
)
from packages.common.node27_cold_governance_collection import (
    bytes_pretty as _bytes_pretty,
)
from packages.common.node27_cold_governance_collection import (
    cold_governance_sample as _cold_governance_sample,
)
from packages.common.node27_cold_governance_collection import (
    collect_filesystem,
    collect_postgres,
)
from packages.common.node27_cold_governance_collection import (
    run_command as _run_command,
)
from packages.common.node27_cold_governance_runtime import ColdGovernanceRuntimeConfig, cold_governance_evidence

SCHEMA_VERSION = "nhms.node27_resource_governance.audit.v1"

DEFAULT_SERVICES = (
    "nhms-display-api.service",
    "nhms-node27-autopipe.service",
    "nhms-node27-autopipe.timer",
    "nhms-node27-download.service",
    "nhms-node27-download.timer",
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
    index_ratio_warn: float = 2.0
    index_ratio_critical: float = 4.0
    temp_bytes_warn: int = 50 * GIB
    wal_warn_bytes: int = 10 * GIB
    dead_tuple_warn_pct: float = 10.0


@dataclass(frozen=True)
class AuditConfig:
    repo_root: Path
    object_store_root: Path
    pgdata_root: Path | None
    database_url: str | None
    summary_path: Path | None
    services: tuple[str, ...]
    thresholds: AuditThresholds
    cold_governance_receipt_path: Path | None = None
    cold_governance_head_sha: str | None = None
    cold_governance_home_residual_minimum_bytes: int = 0
    cold_governance_cold_residual_minimum_bytes: int = 0
    cold_governance_evidence_hostname: str | None = None
    cold_governance_array_device: str = "/dev/md0"
    cold_governance_evidence_max_age_seconds: int | None = None
    cold_governance_evidence_owner_uid: int = 0
    cold_governance_evidence_approved_modes: tuple[int, ...] = ()
    cold_governance_mdadm_evidence_path: Path | None = None
    cold_governance_smart_evidence_paths: tuple[tuple[str, Path], ...] = ()
    cold_governance_backup_evidence_path: Path | None = None
    cold_governance_mdadm_bin: str = "/usr/sbin/mdadm"
    cold_governance_smartctl_bin: str = "/usr/sbin/smartctl"
    cold_governance_backup_inventory_bin: str = "/usr/local/sbin/nhms-backup-inventory"
    cold_governance_prior_receipt_path: Path | None = None
    cold_governance_prior_receipt_max_age_seconds: int | None = None


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


def _cold_runtime_config(config: AuditConfig) -> ColdGovernanceRuntimeConfig:
    return ColdGovernanceRuntimeConfig(
        pgdata_root=config.pgdata_root,
        evidence_hostname=config.cold_governance_evidence_hostname,
        array_device=config.cold_governance_array_device,
        evidence_max_age_seconds=config.cold_governance_evidence_max_age_seconds,
        evidence_owner_uid=config.cold_governance_evidence_owner_uid,
        evidence_approved_modes=config.cold_governance_evidence_approved_modes,
        mdadm_evidence_path=config.cold_governance_mdadm_evidence_path,
        smart_evidence_paths=config.cold_governance_smart_evidence_paths,
        backup_evidence_path=config.cold_governance_backup_evidence_path,
        mdadm_bin=config.cold_governance_mdadm_bin,
        smartctl_bin=config.cold_governance_smartctl_bin,
        backup_inventory_bin=config.cold_governance_backup_inventory_bin,
    )


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
    return recommendations


def build_receipt(config: AuditConfig) -> dict[str, Any]:
    started_at = _utc_now()
    filesystem = collect_filesystem(config)
    postgres = collect_postgres(config.database_url)
    systemd = collect_systemd(config.services)
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
        },
        "filesystem": filesystem,
        "postgres": postgres,
        "systemd": systemd,
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
    if config.cold_governance_receipt_path is not None:
        audit_reference = datetime.now(UTC)
        evidence = cold_governance_evidence(
            _cold_runtime_config(config), postgres, observed_at=audit_reference
        )
        cold_receipt, cold_schema = build_cold_governance_receipt(
            config=GovernanceConfig(
                receipt_path=config.cold_governance_receipt_path,
                head_sha=config.cold_governance_head_sha,
                home_residual_minimum_bytes=config.cold_governance_home_residual_minimum_bytes,
                cold_residual_minimum_bytes=config.cold_governance_cold_residual_minimum_bytes,
                prior_receipt_path=config.cold_governance_prior_receipt_path,
                prior_receipt_max_age_seconds=config.cold_governance_prior_receipt_max_age_seconds,
            ),
            started_at=started_at,
            finished_at=receipt["finished_at"],
            home=_cold_governance_sample(filesystem, postgres, path="/home", observed_at=receipt["finished_at"]),
            cold=_cold_governance_sample(filesystem, postgres, path="/data/GHDC", observed_at=receipt["finished_at"]),
            evidence=evidence,
        )
        write_cold_governance_receipt(config.cold_governance_receipt_path, cold_receipt, cold_schema)
        receipt["cold_tablespace_governance"] = {
            "outcome": cold_receipt["outcome"],
            "receipt_path": str(config.cold_governance_receipt_path),
        }
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


def _nonnegative_bytes(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer byte count") from error
    if value < 0:
        raise argparse.ArgumentTypeError(f"{label} must be non-negative")
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
        "--cold-governance-receipt-path",
        default=os.getenv("NODE27_COLD_GOVERNANCE_RECEIPT_PATH"),
        help="Optional strict cold-tablespace governance receipt path.",
    )
    parser.add_argument("--cold-governance-head-sha", default=os.getenv("NODE27_COLD_GOVERNANCE_HEAD_SHA"))
    parser.add_argument(
        "--cold-governance-home-residual-minimum-bytes",
        type=lambda raw: _nonnegative_bytes(raw, label="cold-governance-home-residual-minimum-bytes"),
        default=_nonnegative_bytes(
            os.getenv("NODE27_COLD_GOVERNANCE_HOME_RESIDUAL_MINIMUM_BYTES", "0"),
            label="NODE27_COLD_GOVERNANCE_HOME_RESIDUAL_MINIMUM_BYTES",
        ),
    )
    parser.add_argument(
        "--cold-governance-cold-residual-minimum-bytes",
        type=lambda raw: _nonnegative_bytes(raw, label="cold-governance-cold-residual-minimum-bytes"),
        default=_nonnegative_bytes(
            os.getenv("NODE27_COLD_GOVERNANCE_COLD_RESIDUAL_MINIMUM_BYTES", "0"),
            label="NODE27_COLD_GOVERNANCE_COLD_RESIDUAL_MINIMUM_BYTES",
        ),
    )
    add_cold_governance_arguments(parser)
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


def config_from_args(args: argparse.Namespace) -> AuditConfig:
    validate_cold_governance_arguments(args)
    thresholds = AuditThresholds(
        root_free_warn_bytes=args.root_free_warn_bytes,
        root_free_critical_bytes=args.root_free_critical_bytes,
        home_free_warn_bytes=args.home_free_warn_bytes,
        database_warn_bytes=args.database_warn_bytes,
        database_critical_bytes=args.database_critical_bytes,
    )
    pgdata_root = Path(args.pgdata_root).expanduser() if args.pgdata_root else None
    summary_path = Path(args.summary_path).expanduser() if args.summary_path else None
    cold_governance_receipt_path = (
        Path(args.cold_governance_receipt_path).expanduser() if args.cold_governance_receipt_path else None
    )
    if cold_governance_receipt_path is not None and not cold_governance_receipt_path.is_absolute():
        raise ValueError("cold governance receipt path must be absolute")
    return AuditConfig(
        repo_root=Path(args.repo_root).expanduser(),
        object_store_root=Path(args.object_store_root).expanduser(),
        pgdata_root=pgdata_root,
        database_url=args.database_url,
        summary_path=summary_path,
        services=tuple(args.services or DEFAULT_SERVICES),
        thresholds=thresholds,
        cold_governance_receipt_path=cold_governance_receipt_path,
        cold_governance_head_sha=args.cold_governance_head_sha,
        cold_governance_home_residual_minimum_bytes=args.cold_governance_home_residual_minimum_bytes,
        cold_governance_cold_residual_minimum_bytes=args.cold_governance_cold_residual_minimum_bytes,
        cold_governance_evidence_hostname=args.cold_governance_evidence_hostname,
        cold_governance_array_device=args.cold_governance_array_device,
        cold_governance_evidence_max_age_seconds=args.cold_governance_evidence_max_age_seconds,
        cold_governance_evidence_owner_uid=args.cold_governance_evidence_owner_uid,
        cold_governance_evidence_approved_modes=tuple(args.cold_governance_evidence_approved_mode),
        cold_governance_mdadm_evidence_path=args.cold_governance_mdadm_evidence_path,
        cold_governance_smart_evidence_paths=tuple(args.cold_governance_smart_evidence),
        cold_governance_backup_evidence_path=args.cold_governance_backup_evidence_path,
        cold_governance_mdadm_bin=args.cold_governance_mdadm_bin,
        cold_governance_smartctl_bin=args.cold_governance_smartctl_bin,
        cold_governance_backup_inventory_bin=args.cold_governance_backup_inventory_bin,
        cold_governance_prior_receipt_path=args.cold_governance_prior_receipt_path,
        cold_governance_prior_receipt_max_age_seconds=args.cold_governance_prior_receipt_max_age_seconds,
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
