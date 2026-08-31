"""Descriptor-bound admission evidence for the node-27 cold tablespace.

The installer consumes root-produced JSON envelopes as inert data.  This module
opens each evidence pathname once with ``O_NOFOLLOW``, checks owner/mode from
that same descriptor, reads/parses/hashes those exact bytes, and never treats a
self-described privilege flag or a successful mount as health evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.evidence_io import FileIdentity, normalized_absolute_path
from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow

MAX_EVIDENCE_BYTES = 256 * 1024
MAX_AGE_SECONDS = 24 * 60 * 60
_MEMBER_LINE = re.compile(r"^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?P<state>.+?)\s+(?P<device>/dev/\S+)\s*$")
_COUNT_LINE = re.compile(
    r"^\s*(?P<key>Raid Devices|Active Devices|Working Devices|Failed Devices|Spare Devices)\s*:\s*(?P<value>\d+)\s*$",
    re.MULTILINE,
)
_HEALTHY_ARRAY_STATES = frozenset({"clean", "active", "active idle"})
_UNHEALTHY_ARRAY_TOKENS = frozenset(
    {
        "degraded",
        "rebuild",
        "resync",
        "recover",
        "reshape",
        "missing",
        "removed",
        "spare",
        "faulty",
        "unknown",
        "mysterious",
    }
)


@dataclass(frozen=True)
class EvidencePolicy:
    """Pinned identity policy for root-produced evidence files.

    Production must pass ``expected_uid=0`` and a restrictive explicit mode.
    Tests may use their effective UID for disposable descriptor fixtures.
    """

    expected_hostname: str
    array_device: str
    max_age_seconds: int
    expected_uid: int
    approved_modes: tuple[int, ...]
    mdadm_argv: tuple[str, ...]
    smartctl_prefix: tuple[str, ...]
    backup_argv: tuple[str, ...]
    expected_pgdata: str


@dataclass(frozen=True)
class DescriptorEvidence:
    identity: FileIdentity
    uid: int
    gid: int
    mode: int
    document: Mapping[str, Any]


@dataclass(frozen=True)
class RaidEvidence:
    file_identity: dict[str, Any]
    captured_at: str
    members: tuple[str, ...]
    state: str
    healthy: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class SmartEvidence:
    file_identity: dict[str, Any]
    captured_at: str
    device: str
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RootStorageHealth:
    raid: RaidEvidence
    smart: tuple[SmartEvidence, ...]
    members: tuple[str, ...]
    healthy: bool
    blockers: tuple[str, ...]


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
    free_bytes: int
    install_required_bytes: int
    rollback_headroom_bytes: int
    required_bytes: int
    approved: bool
    blockers: tuple[str, ...]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _identity_payload(evidence: DescriptorEvidence) -> dict[str, Any]:
    identity = evidence.identity
    return {
        "path": str(identity.path),
        "normalized_path": str(identity.normalized_path),
        "device": identity.device,
        "inode": identity.inode,
        "bytes": identity.size,
        "sha256": identity.sha256,
        "uid": evidence.uid,
        "gid": evidence.gid,
        "mode": evidence.mode,
    }


def _read_exact_descriptor(path: Path, *, label: str, policy: EvidencePolicy) -> DescriptorEvidence:
    """Read evidence bytes, metadata and identity from one no-follow descriptor."""

    if policy.max_age_seconds <= 0 or policy.max_age_seconds > MAX_AGE_SECONDS:
        raise ValueError("evidence maximum age must be positive and bounded")
    if not policy.approved_modes:
        raise ValueError("at least one approved evidence mode is required")
    fd: int | None = None
    try:
        fd = open_file_no_follow(path)
        before = os.fstat(fd)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if before.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError(f"{label} exceeds the byte ceiling")
        if before.st_uid != policy.expected_uid:
            raise ValueError(f"{label} owner is not approved")
        if mode not in policy.approved_modes:
            raise ValueError(f"{label} mode is not approved")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(fd, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (
            len(raw) != before.st_size
            or os.read(fd, 1)
            or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise ValueError(f"{label} changed while being read")
        try:
            document = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{label} is not valid UTF-8 JSON") from error
        if not isinstance(document, Mapping):
            raise ValueError(f"{label} must be a JSON object")
        identity = FileIdentity(
            path=path,
            normalized_path=normalized_absolute_path(path),
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        return DescriptorEvidence(
            identity=identity,
            uid=int(before.st_uid),
            gid=int(before.st_gid),
            mode=mode,
            document=document,
        )
    except ValueError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise ValueError(f"{label} is unavailable or unsafe") from error
    finally:
        if fd is not None:
            os.close(fd)


def _verify_envelope(
    document: Mapping[str, Any],
    *,
    expected_argv: tuple[str, ...],
    expected_hostname: str,
    now: datetime,
    max_age_seconds: int,
    label: str,
) -> tuple[datetime, Mapping[str, Any], str]:
    if document.get("schema_version") != "1.0":
        raise ValueError(f"{label} schema version differs")
    if document.get("hostname") != expected_hostname:
        raise ValueError(f"{label} hostname identity differs")
    command = document.get("command")
    if not isinstance(command, Mapping) or tuple(command.get("argv") or ()) != expected_argv:
        raise ValueError(f"{label} command identity differs")
    subject = document.get("subject")
    if not isinstance(subject, Mapping):
        raise ValueError(f"{label} subject identity is missing")
    output = document.get("output")
    if not isinstance(output, str) or not output:
        raise ValueError(f"{label} output is missing")
    captured = _parse_timestamp(document.get("captured_at"), label=f"{label} captured_at")
    age_seconds = (now.astimezone(UTC) - captured).total_seconds()
    if age_seconds < 0 or age_seconds > max_age_seconds:
        raise ValueError(f"{label} captured_at is stale or from the future")
    return captured, subject, output


def _parse_members(output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    members: list[str] = []
    blockers: list[str] = []
    for line in output.splitlines():
        matched = _MEMBER_LINE.match(line)
        if not matched:
            continue
        device = matched.group("device")
        state = matched.group("state").lower()
        members.append(device)
        if "active sync" not in state or any(token in state for token in ("spare", "rebuild", "faulty")):
            blockers.append(f"member {device} is not an active synchronized array member")
    if len(members) != 2 or len(set(members)) != 2:
        blockers.append("array evidence must parse exactly two distinct member devices")
    return tuple(members), tuple(blockers)


def _array_count(output: str, name: str) -> int | None:
    for match in _COUNT_LINE.finditer(output):
        if match.group("key") == name:
            return int(match.group("value"))
    return None


def parse_mdadm_evidence(path: Path, *, policy: EvidencePolicy, now: datetime) -> RaidEvidence:
    descriptor = _read_exact_descriptor(path, label="mdadm evidence", policy=policy)
    captured, subject, output = _verify_envelope(
        descriptor.document,
        expected_argv=policy.mdadm_argv,
        expected_hostname=policy.expected_hostname,
        now=now,
        max_age_seconds=policy.max_age_seconds,
        label="mdadm evidence",
    )
    if subject.get("array_device") != policy.array_device:
        raise ValueError("mdadm evidence subject identity differs")
    state_match = re.search(r"^\s*State\s*:\s*(?P<state>.+?)\s*$", output, flags=re.MULTILINE)
    state = state_match.group("state").strip().lower() if state_match else "unknown"
    members, member_blockers = _parse_members(output)
    blockers = list(member_blockers)
    if not state_match or state not in _HEALTHY_ARRAY_STATES:
        blockers.append("array state is unhealthy or unknown")
    if any(token in state for token in _UNHEALTHY_ARRAY_TOKENS):
        blockers.append("array state reports degraded/recovery/rebuild/reshape/unknown condition")
    expected_counts = {
        "Raid Devices": 2,
        "Active Devices": 2,
        "Working Devices": 2,
        "Failed Devices": 0,
        "Spare Devices": 0,
    }
    for field, expected in expected_counts.items():
        if _array_count(output, field) != expected:
            blockers.append(f"array {field.lower()} is not {expected}")
    return RaidEvidence(
        file_identity=_identity_payload(descriptor),
        captured_at=_iso(captured),
        members=members,
        state=state,
        healthy=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def parse_smart_evidence(path: Path, *, device: str, policy: EvidencePolicy, now: datetime) -> SmartEvidence:
    descriptor = _read_exact_descriptor(path, label="SMART evidence", policy=policy)
    captured, subject, output = _verify_envelope(
        descriptor.document,
        expected_argv=(*policy.smartctl_prefix, "-H", device),
        expected_hostname=policy.expected_hostname,
        now=now,
        max_age_seconds=policy.max_age_seconds,
        label="SMART evidence",
    )
    if subject.get("device") != device:
        raise ValueError("SMART evidence subject identity differs")
    upper = output.upper()
    if "PASSED" in upper and "FAIL" not in upper:
        status = "PASS"
        blockers: tuple[str, ...] = ()
    elif "FAIL" in upper:
        status = "FAIL"
        blockers = (f"SMART health is not PASS for {device}",)
    else:
        status = "UNKNOWN"
        blockers = (f"SMART health is unknown for {device}",)
    return SmartEvidence(
        file_identity=_identity_payload(descriptor),
        captured_at=_iso(captured),
        device=device,
        status=status,
        blockers=blockers,
    )


def verify_root_storage_evidence(
    mdadm_path: Path,
    smart_paths: Mapping[str, Path],
    *,
    policy: EvidencePolicy,
    now: datetime,
) -> RootStorageHealth:
    raid = parse_mdadm_evidence(mdadm_path, policy=policy, now=now)
    smart: list[SmartEvidence] = []
    blockers = list(raid.blockers)
    for member in raid.members:
        path = smart_paths.get(member)
        if path is None:
            blockers.append(f"SMART evidence missing for {member}")
            continue
        try:
            observed = parse_smart_evidence(path, device=member, policy=policy, now=now)
        except ValueError as error:
            blockers.append(str(error))
            continue
        smart.append(observed)
        blockers.extend(observed.blockers)
    if len(smart) != 2:
        blockers.append("exactly two descriptor-bound SMART observations are required")
    return RootStorageHealth(
        raid=raid,
        smart=tuple(smart),
        members=raid.members,
        healthy=raid.healthy and len(smart) == 2 and all(item.status == "PASS" for item in smart) and not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def parse_backup_inventory(
    path: Path,
    *,
    policy: EvidencePolicy,
    external_targets: tuple[str, ...],
    now: datetime,
) -> BackupCoverage:
    descriptor = _read_exact_descriptor(path, label="backup inventory", policy=policy)
    _captured, subject, _output = _verify_envelope(
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
        file_identity=_identity_payload(descriptor),
        covered_paths=covered,
        missing_targets=missing,
        complete=not missing,
        blockers=blockers,
    )


def assess_fresh_path(
    observation: PathObservation,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_device_identity: str,
) -> PathDecision:
    blockers: list[str] = []
    if not observation.exists:
        blockers.append("host path is missing")
    if observation.is_symlink:
        blockers.append("host path must not be a symlink")
    if not observation.is_directory:
        blockers.append("host path must be a directory")
    if observation.entry_count != 0:
        blockers.append("fresh host path must be empty")
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
    return PathDecision(approved=not blockers, blockers=tuple(blockers))


def assess_install_capacity(
    *,
    free_bytes: int,
    install_required_bytes: int,
    rollback_headroom_bytes: int,
) -> CapacityDecision:
    if min(free_bytes, install_required_bytes, rollback_headroom_bytes) < 0:
        raise ValueError("capacity values must be non-negative")
    required = install_required_bytes + rollback_headroom_bytes
    blockers = () if free_bytes >= required else ("cold filesystem lacks install plus rollback headroom",)
    return CapacityDecision(
        free_bytes=free_bytes,
        install_required_bytes=install_required_bytes,
        rollback_headroom_bytes=rollback_headroom_bytes,
        required_bytes=required,
        approved=not blockers,
        blockers=blockers,
    )
