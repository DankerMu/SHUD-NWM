"""Runtime collection for descriptor-bound cold-governance evidence.

The resource-governance script owns ordinary audit collection.  This module
keeps #1894's root-evidence configuration, topology binding, and refusal shape
out of that broad script so the public governance path remains independently
testable and under its source-size boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.node27_cold_governance import COLD_HOST_PATH
from packages.common.node27_cold_tablespace_evidence import EvidencePolicy
from packages.common.node27_cold_tablespace_host import (
    DockerBoundary,
    EvidencePaths,
    inspect_host_path,
    inspect_storage_evidence,
)


@dataclass(frozen=True)
class ColdGovernanceRuntimeConfig:
    pgdata_root: Path | None
    evidence_hostname: str | None
    array_device: str
    evidence_max_age_seconds: int | None
    evidence_owner_uid: int
    evidence_approved_modes: tuple[int, ...]
    mdadm_evidence_path: Path | None
    smart_evidence_paths: tuple[tuple[str, Path], ...]
    backup_evidence_path: Path | None
    mdadm_bin: str
    smartctl_bin: str
    backup_inventory_bin: str


def external_pg_tblspc_targets(postgres: Mapping[str, Any]) -> tuple[str, ...]:
    rows = postgres.get("external_pg_tblspc_targets") if isinstance(postgres, Mapping) else []
    if not isinstance(rows, list):
        return ()
    targets = [
        str(row.get("target"))
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("target"), str)
    ]
    return tuple(sorted(set(targets)))


def cold_governance_storage_evidence(
    config: ColdGovernanceRuntimeConfig,
    *,
    external_targets: tuple[str, ...],
    observed_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the installer parser; incomplete configuration is a truthful refusal."""

    if (
        not config.evidence_hostname
        or config.evidence_max_age_seconds is None
        or not config.evidence_approved_modes
        or config.mdadm_evidence_path is None
        or config.backup_evidence_path is None
        or not config.smart_evidence_paths
        or config.pgdata_root is None
    ):
        return (
            {"healthy": False, "reason": "descriptor-bound health evidence is not configured for governance"},
            {"complete": False, "reason": "descriptor-bound backup evidence is not configured for governance"},
        )
    try:
        health, backup = inspect_storage_evidence(
            EvidencePaths(
                mdadm=config.mdadm_evidence_path,
                smart=dict(config.smart_evidence_paths),
                backup=config.backup_evidence_path,
            ),
            policy=EvidencePolicy(
                expected_hostname=config.evidence_hostname,
                array_device=config.array_device,
                max_age_seconds=config.evidence_max_age_seconds,
                expected_uid=config.evidence_owner_uid,
                approved_modes=config.evidence_approved_modes,
                mdadm_argv=(config.mdadm_bin, "--detail", config.array_device),
                smartctl_prefix=(config.smartctl_bin,),
                backup_argv=(config.backup_inventory_bin, "--json"),
                expected_pgdata=str(config.pgdata_root),
            ),
            external_targets=external_targets,
            now=observed_at,
        )
        return health, backup
    except Exception:
        return (
            {"healthy": False, "reason": "descriptor-bound health evidence is unavailable or invalid"},
            {"complete": False, "reason": "descriptor-bound backup evidence is unavailable or invalid"},
        )


def cold_governance_evidence(
    config: ColdGovernanceRuntimeConfig,
    postgres: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    """Bind topology and descriptor evidence to a single governance observation."""

    try:
        docker = DockerBoundary()
        current, stopped = docker.current_and_stopped_cold_binds()
    except Exception:
        current, stopped = (), ("docker inventory unavailable",)
    try:
        host = {"host_path": COLD_HOST_PATH, **inspect_host_path()}
    except Exception:
        host = {"host_path": COLD_HOST_PATH, "device_identity": None}
    rows = postgres.get("cold_tablespace") if isinstance(postgres, Mapping) else []
    catalog_row = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], Mapping) else {}
    relations = list(postgres.get("cold_relation_by_tablespace", [])) if isinstance(postgres, Mapping) else []
    health, backup = cold_governance_storage_evidence(
        config,
        external_targets=external_pg_tblspc_targets(postgres),
        observed_at=observed_at,
    )
    return {
        "health": health,
        "backup": backup,
        "mount_inventory": {"current": list(current), "stopped": list(stopped)},
        "catalog": {
            "catalog_observed": isinstance(rows, list),
            "tablespace": "nhms_cold",
            "location": catalog_row.get("location"),
            "relations": relations,
        },
        "host_path": host,
    }
