"""Strict dual-device accounting and receipt publication for cold tablespace governance.

The module owns the public receipt contract.  Collection remains outside this
pure layer so the operational script can use bounded host/DB boundaries while
tests use independent fixed observations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.node27_cold_governance_history import build_trend, trend_payload
from packages.common.redaction import redact_payload
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow

COLD_TABLESPACE = "nhms_cold"
COLD_HOST_PATH = "/data/GHDC/nhms-cold-tablespace"
COLD_CONTAINER_PATH = "/home/postgres/pgdata/tablespaces/nhms_cold"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/node27_cold_governance_receipt.schema.json"
_FORMAT_CHECKER = jsonschema.FormatChecker()


@dataclass(frozen=True)
class GovernanceConfig:
    receipt_path: Path
    head_sha: str | None
    home_residual_minimum_bytes: int = 0
    cold_residual_minimum_bytes: int = 0
    prior_receipt_path: Path | None = None
    prior_receipt_max_age_seconds: int | None = None


@dataclass(frozen=True)
class Reconciliation:
    approved: bool
    filesystems: dict[str, dict[str, Any]]
    blockers: tuple[str, ...]


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _normalized(sample: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    required = (
        "path",
        "observed_at",
        "identity",
        "total_bytes",
        "free_bytes",
        "used_bytes",
        "pgdata_bytes",
        "nhms_cold_relation_bytes",
        "object_store_bytes",
    )
    missing = [key for key in required if key not in sample]
    if missing:
        raise ValueError(f"{label} sample is missing required fields")
    values: dict[str, Any] = {key: sample[key] for key in required}
    if not all(
        isinstance(values[key], int) and not isinstance(values[key], bool) and values[key] >= 0 for key in required[3:]
    ):
        raise ValueError(f"{label} byte fields must be non-negative integers")
    if not all(isinstance(values[key], str) and values[key] for key in required[:3]):
        raise ValueError(f"{label} identity fields are invalid")
    known = values["pgdata_bytes"] + values["nhms_cold_relation_bytes"] + values["object_store_bytes"]
    values["residual_bytes"] = values["used_bytes"] - known
    return values


def reconcile_filesystems(
    home: Mapping[str, Any],
    cold: Mapping[str, Any],
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
    home_residual_minimum_bytes: int = 0,
    cold_residual_minimum_bytes: int = 0,
) -> Reconciliation:
    """Compute category residuals arithmetically; never walk a shared root."""

    if home_residual_minimum_bytes < 0 or cold_residual_minimum_bytes < 0:
        raise ValueError("residual thresholds must be non-negative")
    home_value = _normalized(home, label="home")
    cold_value = _normalized(cold, label="cold")
    blockers: list[str] = []
    if home_value["path"] != "/home":
        blockers.append("home sample path differs")
    if cold_value["path"] != "/data/GHDC":
        blockers.append("cold sample path differs")
    if home_value["identity"] == cold_value["identity"]:
        blockers.append("home and cold filesystem identities must be separately observed")
    for label, value, minimum in (
        ("home", home_value, home_residual_minimum_bytes),
        ("cold", cold_value, cold_residual_minimum_bytes),
    ):
        if value["total_bytes"] != value["used_bytes"] + value["free_bytes"]:
            blockers.append(f"{label} filesystem capacity is unreconcilable")
        if value["residual_bytes"] < 0:
            blockers.append(f"{label} residual is negative or categories overlap")
        elif value["residual_bytes"] < minimum:
            blockers.append(f"{label} residual is below the measured governance threshold")
    if started_at is not None and finished_at is not None:
        start = _parse(started_at)
        finish = _parse(finished_at)
        if finish < start:
            blockers.append("audit interval is invalid")
        for label, value in (("home", home_value), ("cold", cold_value)):
            observed = _parse(value["observed_at"])
            if observed < start or observed > finish:
                blockers.append(f"{label} sample lies outside audit interval")
    return Reconciliation(
        approved=not blockers,
        filesystems={"home": home_value, "cold": cold_value},
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _descriptor_identity(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    sha = value.get("sha256")
    return isinstance(sha, str) and len(sha) == 64 and all(character in "0123456789abcdef" for character in sha)


def _evidence_blockers(evidence: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    health = evidence.get("health")
    if not isinstance(health, Mapping) or health.get("healthy") is not True:
        blockers.append("root health evidence is missing or unhealthy")
    else:
        raid = health.get("raid")
        raid_identity = raid.get("file_identity") if isinstance(raid, Mapping) else None
        if not _descriptor_identity(raid_identity):
            blockers.append("root health RAID evidence lacks descriptor identity")
        else:
            smart = health.get("smart")
            if not isinstance(smart, list) or len(smart) != 2:
                blockers.append("root health requires exactly two SMART observations")
            elif any(
                not isinstance(item, Mapping)
                or item.get("status") != "PASS"
                or not _descriptor_identity(item.get("file_identity"))
                for item in smart
            ):
                blockers.append("root health SMART evidence is incomplete or non-passing")
    backup = evidence.get("backup")
    if not isinstance(backup, Mapping) or backup.get("complete") is not True:
        blockers.append("backup coverage is incomplete")
    elif not _descriptor_identity(backup.get("file_identity")):
        blockers.append("backup evidence lacks descriptor-bound inventory identity")
    mounts = evidence.get("mount_inventory")
    if not isinstance(mounts, Mapping):
        blockers.append("mount inventory is missing")
    else:
        current = mounts.get("current")
        stopped = mounts.get("stopped")
        if not isinstance(current, list) or not isinstance(stopped, list):
            blockers.append("mount inventory is malformed")
        elif stopped:
            blockers.append("stopped container has stale cold bind")
    catalog = evidence.get("catalog")
    if not isinstance(catalog, Mapping) or catalog.get("tablespace") != COLD_TABLESPACE:
        blockers.append("cold catalog evidence is missing or drifted")
    else:
        location = catalog.get("location")
        relations = catalog.get("relations")
        if location != COLD_CONTAINER_PATH:
            blockers.append("cold catalog location differs from fixed container target")
        if not isinstance(relations, list):
            blockers.append("cold catalog relation-by-tablespace inventory is missing")
    current = mounts.get("current") if isinstance(mounts, Mapping) else None
    if isinstance(current, list) and len(current) > 1:
        blockers.append("more than one running container has the cold bind")
    return blockers


def _catalog_is_observed(catalog: Mapping[str, Any]) -> bool:
    """Distinguish a real absence observation from a synthetic empty fixture."""

    return catalog.get("catalog_observed") is True


def _topology_blockers(evidence: Mapping[str, Any]) -> list[str]:
    """Reject catalog/bind/path divergence without inventing absent observations."""

    catalog = evidence.get("catalog")
    mounts = evidence.get("mount_inventory")
    host = evidence.get("host_path")
    if not isinstance(catalog, Mapping) or not isinstance(mounts, Mapping):
        return []
    running = mounts.get("current")
    if not isinstance(running, list):
        return []
    if not _catalog_is_observed(catalog):
        return []
    has_catalog = catalog.get("location") == COLD_CONTAINER_PATH
    has_bind = bool(running)
    blockers: list[str] = []
    if has_catalog != has_bind:
        blockers.append("catalog and current cold bind diverge")
    if host is not None:
        if not isinstance(host, Mapping) or host.get("host_path") != COLD_HOST_PATH or not host.get("device_identity"):
            blockers.append("cold host path identity is missing or drifted")
    return blockers


def build_cold_governance_receipt(
    *,
    config: GovernanceConfig,
    started_at: str,
    finished_at: str,
    home: Mapping[str, Any],
    cold: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    reconciliation = reconcile_filesystems(
        home,
        cold,
        started_at=started_at,
        finished_at=finished_at,
        home_residual_minimum_bytes=config.home_residual_minimum_bytes,
        cold_residual_minimum_bytes=config.cold_residual_minimum_bytes,
    )
    topology = _topology_blockers(evidence)
    trend = build_trend(
        current_filesystems=reconciliation.filesystems,
        current_started_at=started_at,
        prior_path=config.prior_receipt_path,
        max_age_seconds=config.prior_receipt_max_age_seconds,
    )
    blockers = [*reconciliation.blockers, *_evidence_blockers(evidence), *topology, *trend.blockers]
    outcome = "healthy" if not blockers else "drift" if reconciliation.blockers or topology else "refusal"
    safe_evidence = redact_payload(dict(evidence))
    receipt = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "head_sha": config.head_sha,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": finished_at,
        "filesystems": reconciliation.filesystems,
        "evidence": safe_evidence,
        "trend": trend_payload(trend),
        "thresholds": {
            "home_residual_bytes": reconciliation.filesystems["home"]["residual_bytes"],
            "cold_residual_bytes": reconciliation.filesystems["cold"]["residual_bytes"],
            "home_residual_minimum_bytes": config.home_residual_minimum_bytes,
            "cold_residual_minimum_bytes": config.cold_residual_minimum_bytes,
        },
        "blockers": list(dict.fromkeys(blockers)),
    }
    return redact_payload(receipt), _schema()


def write_cold_governance_receipt(path: Path, receipt: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    document = json.loads(json.dumps(redact_payload(dict(receipt)), sort_keys=True, separators=(",", ":")))
    jsonschema.validate(document, schema, format_checker=_FORMAT_CHECKER)
    try:
        atomic_write_bytes_no_follow(
            path,
            (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
            require_durable_replace=True,
        )
    except SafeFilesystemError as error:
        raise RuntimeError("governance receipt publication failed") from error
