"""Cold-only backup coverage, path admission, and installation capacity.

Backup inventory consumes the PGDATA owner's descriptor and envelope checks;
the installer path/capacity policy remains here until its own retirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common import node27_pgdata_evidence as evidence


@dataclass(frozen=True)
class BackupCoverage:
    file_identity: dict[str, Any]
    covered_paths: tuple[str, ...]
    missing_targets: tuple[str, ...]
    complete: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PathObservation:
    exists: bool
    is_symlink: bool
    is_directory: bool
    entry_count: int | None
    uid: int | None
    gid: int | None
    mode: int | None
    mount_device: str | None
    device_identity: str | None
    free_bytes: int | None
    path_identity: str | None = None


@dataclass(frozen=True)
class PathDecision:
    approved: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class CapacityDecision:
    free_bytes: int | None
    install_required_bytes: int
    rollback_headroom_bytes: int
    required_bytes: int
    approved: bool
    blockers: tuple[str, ...]


def parse_backup_inventory(
    path: Path,
    *,
    policy: evidence.EvidencePolicy,
    external_targets: tuple[str, ...],
    now: datetime,
) -> BackupCoverage:
    descriptor = evidence._read_exact_descriptor(path, label="backup inventory", policy=policy)
    _captured, subject, _output = evidence._verify_envelope(
        descriptor.document,
        expected_argv=policy.backup_argv,
        expected_hostname=policy.expected_hostname,
        now=now,
        max_age_seconds=policy.max_age_seconds,
        label="backup inventory",
    )
    subject_targets = subject.get("external_pg_tblspc_targets")
    if (
        subject.get("pgdata") != policy.expected_pgdata
        or not isinstance(subject_targets, list)
        or tuple(subject_targets) != external_targets
    ):
        raise ValueError("backup inventory subject identity differs")
    covered_raw = descriptor.document.get("covered_paths")
    if not isinstance(covered_raw, list) or not all(isinstance(item, str) for item in covered_raw):
        raise ValueError("backup inventory covered paths are malformed")
    covered = tuple(covered_raw)
    required = (policy.expected_pgdata, *external_targets)
    missing = tuple(item for item in required if item not in covered)
    blockers = tuple(f"backup inventory omits {item}" for item in missing)
    return BackupCoverage(
        file_identity=evidence._identity_payload(descriptor),
        covered_paths=covered,
        missing_targets=missing,
        complete=not missing,
        blockers=blockers,
    )


def _shared_path_gates(
    observation: PathObservation,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_device_identity: str,
) -> list[str]:
    blockers: list[str] = []
    if not observation.exists:
        blockers.append("host path is missing")
    if observation.is_symlink:
        blockers.append("host path must not be a symlink")
    if not observation.is_directory:
        blockers.append("host path must be a directory")
    if observation.uid != expected_uid or observation.gid != expected_gid:
        blockers.append("host path owner identity differs")
    if observation.mode != expected_mode:
        blockers.append("host path mode differs")
    if observation.device_identity != expected_device_identity:
        blockers.append("host path device identity differs")
    if observation.mount_device is None:
        blockers.append("host path mount identity is unavailable")
    if observation.free_bytes is None or observation.free_bytes < 0:
        blockers.append("host path capacity observation is unavailable")
    return blockers


def assess_fresh_path(
    observation: PathObservation,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_device_identity: str,
) -> PathDecision:
    blockers = _shared_path_gates(
        observation,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_mode=expected_mode,
        expected_device_identity=expected_device_identity,
    )
    if observation.entry_count != 0:
        blockers.append("fresh host path must be empty")
    return PathDecision(approved=not blockers, blockers=tuple(blockers))


def assess_resident_path(
    observation: PathObservation,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_device_identity: str,
) -> PathDecision:
    """Admit a PostgreSQL-owned nonempty host path only when it is a live resident.

    A fresh install requires an empty path; a complete ready topology owns the
    directory through PostgreSQL, which deterministically creates a version
    subtree (``PG_15_...``).  The entry count must still be observed as a
    non-negative number, but a nonzero count is not itself a failure.  Catalog,
    exact bind, and approved readback are the authority that selects this gate;
    this assessment never whitelists directory names.
    """

    blockers = _shared_path_gates(
        observation,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_mode=expected_mode,
        expected_device_identity=expected_device_identity,
    )
    if observation.entry_count is None:
        blockers.append("resident host path entry count was not observed")
    elif observation.entry_count < 0:
        blockers.append("resident host path entry count is negative")
    return PathDecision(approved=not blockers, blockers=tuple(blockers))


def assess_install_capacity(
    *,
    free_bytes: int | None,
    install_required_bytes: int,
    rollback_headroom_bytes: int,
) -> CapacityDecision:
    if min(install_required_bytes, rollback_headroom_bytes) < 0:
        raise ValueError("capacity values must be non-negative")
    if free_bytes is not None and free_bytes < 0:
        raise ValueError("capacity values must be non-negative")
    required = install_required_bytes + rollback_headroom_bytes
    if free_bytes is None:
        blockers = ("host path capacity observation is unavailable",)
    else:
        blockers = () if free_bytes >= required else ("cold filesystem lacks install plus rollback headroom",)
    return CapacityDecision(
        free_bytes=free_bytes,
        install_required_bytes=install_required_bytes,
        rollback_headroom_bytes=rollback_headroom_bytes,
        required_bytes=required,
        approved=not blockers,
        blockers=blockers,
    )
