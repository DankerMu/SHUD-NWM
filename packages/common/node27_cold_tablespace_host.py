"""Real host boundaries for the fixed node-27 cold-tablespace installer.

All Docker input is bounded inert JSON and every Docker invocation is an argv
list rooted at ``/usr/bin/docker``.  This module deliberately has no shell
entrypoint and no database credentials.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_target import run_bounded_command
from packages.common.node27_cold_tablespace_container import MAX_INSPECT_BYTES
from packages.common.node27_cold_tablespace_evidence import (
    EvidencePolicy,
    parse_backup_inventory,
    verify_root_storage_evidence,
)
from packages.common.node27_cold_tablespace_identity import (
    PRODUCTION_IDENTITY,
    ColdTablespaceIdentity,
    validate_identity_for_action,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    list_directory_no_follow_limited,
    open_directory_no_follow,
    read_bytes_durable_no_follow,
)


class ColdHostError(RuntimeError):
    """A required host, Docker, or mount observation is unavailable."""


@dataclass(frozen=True)
class EvidencePaths:
    """Root-produced evidence files supplied by the operator, never shell-sourced."""

    mdadm: Path
    smart: Mapping[str, Path]
    backup: Path


class SystemdBoundary:
    """Bounded argv-only user-unit inspector for installer quiescence gates."""

    def __init__(self, *, runner: Any = None) -> None:
        self._runner = runner

    def inspect_quiescence(self, units: Sequence[str]) -> Mapping[str, Any]:
        observed: dict[str, dict[str, str]] = {}
        for unit in units:
            if not unit or not unit.endswith((".service", ".timer")):
                raise ColdHostError("quiescence unit identity is invalid")
            argv = (
                "/usr/bin/systemctl",
                "--user",
                "--no-pager",
                "show",
                unit,
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "Result",
            )
            result = (
                run_bounded_command(argv, max_bytes=MAX_INSPECT_BYTES)
                if self._runner is None
                else self._runner(argv, max_bytes=MAX_INSPECT_BYTES)
            )
            if result.returncode != 0:
                raise ColdHostError("writer/timer state inspection failed")
            fields: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"ActiveState", "SubState", "Result"}:
                    fields[key] = value
            if set(fields) != {"ActiveState", "SubState", "Result"}:
                raise ColdHostError("writer/timer state inspection is malformed")
            observed[unit] = {
                "active_state": fields["ActiveState"],
                "sub_state": fields["SubState"],
                "result": fields["Result"],
            }
        return {"units": observed}


class DockerBoundary:
    """Bounded argv-only Docker boundary bound to one issued identity contract."""

    def __init__(
        self,
        *,
        identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
        docker_bin: str | None = None,
    ) -> None:
        try:
            self._identity = validate_identity_for_action(identity)
        except ValueError as error:
            raise ColdHostError("Docker identity contract is unsafe") from error
        if docker_bin is not None and docker_bin != self._identity.docker_bin:
            raise ColdHostError("Docker binary must be the trusted absolute path")
        self._docker_bin = self._identity.docker_bin

    @property
    def identity(self) -> ColdTablespaceIdentity:
        return self._identity

    def inspect(self, container: str) -> Mapping[str, Any]:
        document = self._json((self._docker_bin, "inspect", container))
        if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], Mapping):
            raise ColdHostError("docker inspect did not return exactly one object")
        return document[0]

    def action(self, argv: tuple[str, ...]) -> Mapping[str, Any]:
        if not argv or argv[0] != self._docker_bin:
            raise ColdHostError("Docker action did not use the trusted absolute path")
        result = run_bounded_command(argv, max_bytes=MAX_INSPECT_BYTES)
        if result.returncode != 0:
            raise ColdHostError("Docker action failed")
        return {"returncode": result.returncode}

    def current_and_stopped_cold_binds(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        result = run_bounded_command(
            (self._docker_bin, "ps", "-a", "--format", "{{.Names}}"), max_bytes=MAX_INSPECT_BYTES
        )
        if result.returncode != 0:
            raise ColdHostError("Docker container inventory is unavailable")
        current: list[str] = []
        stopped: list[str] = []
        for name in (line.strip() for line in result.stdout.splitlines()):
            if not name:
                continue
            inspect = self.inspect(name)
            state = inspect.get("State")
            running = bool(state.get("Running")) if isinstance(state, Mapping) else False
            mounts = inspect.get("Mounts")
            if not isinstance(mounts, list):
                raise ColdHostError("Docker inspect mount inventory is malformed")
            for mount in mounts:
                if not isinstance(mount, Mapping):
                    raise ColdHostError("Docker inspect mount inventory is malformed")
                if (
                    mount.get("Source") != str(self._identity.host_path)
                    or mount.get("Destination") != self._identity.container_path
                ):
                    continue
                identity = f"{name}:{self._identity.host_path}:{self._identity.container_path}"
                (current if running else stopped).append(identity)
        return tuple(current), tuple(stopped)

    def _json(self, argv: Sequence[str]) -> Any:
        result = run_bounded_command(tuple(argv), max_bytes=MAX_INSPECT_BYTES)
        if result.returncode != 0:
            raise ColdHostError("Docker inspection failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ColdHostError("Docker inspection returned malformed JSON") from error


def _mount_identity(path: Path, device: int) -> tuple[str, str | None]:
    """Return a Linux mount/device identity without recursively scanning storage."""

    mountinfo = Path("/proc/self/mountinfo")
    try:
        raw = read_bytes_durable_no_follow(mountinfo, max_bytes=1024 * 1024).decode("utf-8", errors="strict")
    except (OSError, SafeFilesystemError, UnicodeDecodeError) as error:
        raise ColdHostError("mount identity is unavailable") from error
    normalized = os.path.normpath(os.fspath(path))
    best: tuple[int, str, str, str] | None = None
    for line in raw.splitlines():
        fields = line.split(" ")
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 2 >= len(fields) or len(fields) < 5:
            continue
        mount_id, major_minor, _root, mountpoint = fields[:4]
        source = fields[separator + 2]
        decoded_mountpoint = mountpoint.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
        try:
            matches = os.path.commonpath((normalized, decoded_mountpoint)) == decoded_mountpoint
        except ValueError:
            matches = False
        if not matches:
            continue
        candidate = (len(decoded_mountpoint), mount_id, major_minor, source)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise ColdHostError("mount identity did not cover the host path")
    _length, mount_id, major_minor, source = best
    major, minor = os.major(device), os.minor(device)
    if major_minor != f"{major}:{minor}":
        raise ColdHostError("mount identity differs from path device")
    return f"{major_minor}:{mount_id}:{source}", source


def create_fresh_host_path(
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_device_identity: str,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> dict[str, Any]:
    """Create only an issued identity's absent child through a pinned parent."""

    try:
        identity = validate_identity_for_action(identity)
    except ValueError as error:
        raise ColdHostError("host identity contract is unsafe") from error
    path = identity.host_path
    try:
        parent_fd = open_directory_no_follow(path.parent)
    except (OSError, SafeFilesystemError) as error:
        raise ColdHostError("cold tablespace parent is unavailable or unsafe") from error
    try:
        try:
            os.mkdir(path.name, expected_mode, dir_fd=parent_fd)
        except FileExistsError as error:
            raise ColdHostError("cold tablespace host path already exists") from error
        try:
            os.chown(path.name, expected_uid, expected_gid, dir_fd=parent_fd, follow_symlinks=False)
            child_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                os.fchmod(child_fd, expected_mode)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise ColdHostError("cold tablespace host path ownership or mode could not be established") from error
    finally:
        os.close(parent_fd)
    observed = inspect_host_path(path, identity=identity)
    if (
        observed.get("exists") is not True
        or observed.get("is_symlink") is not False
        or observed.get("is_directory") is not True
        or observed.get("entry_count") != 0
        or observed.get("uid") != expected_uid
        or observed.get("gid") != expected_gid
        or observed.get("mode") != expected_mode
        or observed.get("device_identity") != expected_device_identity
    ):
        raise ColdHostError("created cold tablespace host path does not satisfy the fixed contract")
    return observed


def remove_installer_owned_host_path(
    *,
    expected_device_identity: str,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> bool:
    """Remove only an issued contract's freshly empty path after reference gates."""

    try:
        identity = validate_identity_for_action(identity)
    except ValueError as error:
        raise ColdHostError("host identity contract is unsafe") from error
    observed = inspect_host_path(identity=identity)
    if (
        observed.get("exists") is not True
        or observed.get("is_symlink") is not False
        or observed.get("is_directory") is not True
        or observed.get("entry_count") != 0
        or observed.get("uid") != expected_uid
        or observed.get("gid") != expected_gid
        or observed.get("mode") != expected_mode
        or observed.get("device_identity") != expected_device_identity
    ):
        raise ColdHostError("host path identity or emptiness is uncertain")
    path = identity.host_path
    try:
        parent_fd = open_directory_no_follow(path.parent)
    except (OSError, SafeFilesystemError) as error:
        raise ColdHostError("cold tablespace parent is unavailable or unsafe") from error
    try:
        try:
            entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise ColdHostError("host path changed before removal")
            os.rmdir(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise ColdHostError("installer-owned host path could not be removed") from error
    finally:
        os.close(parent_fd)
    return True


def inspect_host_path(
    path: Path | None = None,
    *,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> dict[str, Any]:
    """Observe one issued host path through a no-follow directory descriptor."""

    try:
        identity = validate_identity_for_action(identity)
    except ValueError as error:
        raise ColdHostError("host identity contract is unsafe") from error
    path = identity.host_path if path is None else path
    if path != identity.host_path:
        raise ColdHostError("host path must match the immutable identity contract")
    try:
        fd = open_directory_no_follow(path)
    except FileNotFoundError:
        parent = path.parent
        try:
            parent_fd = open_directory_no_follow(parent)
        except (OSError, SafeFilesystemError) as error:
            raise ColdHostError("cold tablespace parent is unavailable") from error
        try:
            parent_info = os.fstat(parent_fd)
            identity, mount_device = _mount_identity(parent, parent_info.st_dev)
            usage = os.statvfs(parent)
            return {
                "exists": False,
                "is_symlink": False,
                "is_directory": False,
                "entry_count": None,
                "uid": None,
                "gid": None,
                "mode": None,
                "mount_device": mount_device,
                "device_identity": identity,
                "free_bytes": usage.f_bavail * usage.f_frsize,
            }
        finally:
            os.close(parent_fd)
    except (OSError, SafeFilesystemError) as error:
        raise ColdHostError("cold tablespace host path is unavailable or unsafe") from error
    try:
        info = os.fstat(fd)
        identity, mount_device = _mount_identity(path, info.st_dev)
        usage = os.fstatvfs(fd)
        return {
            "exists": True,
            "is_symlink": False,
            "is_directory": stat.S_ISDIR(info.st_mode),
            "entry_count": len(list_directory_no_follow_limited(path, max_entries=1)),
            "uid": int(info.st_uid),
            "gid": int(info.st_gid),
            "mode": stat.S_IMODE(info.st_mode),
            "mount_device": mount_device,
            "device_identity": identity,
            "free_bytes": usage.f_bavail * usage.f_frsize,
        }
    finally:
        os.close(fd)


def inspect_storage_health(
    paths: EvidencePaths, *, policy: EvidencePolicy, now: datetime | None = None
) -> dict[str, Any]:
    """Parse only RAID and SMART evidence; backup scope comes from catalog."""

    observed_at = now or datetime.now(UTC)
    try:
        health = verify_root_storage_evidence(paths.mdadm, paths.smart, policy=policy, now=observed_at)
    except ValueError as error:
        raise ColdHostError("descriptor-bound storage health evidence is unavailable or invalid") from error
    return {
        "healthy": health.healthy,
        "members": list(health.members),
        "raid": {
            "file_identity": health.raid.file_identity,
            "captured_at": health.raid.captured_at,
            "state": health.raid.state,
            "healthy": health.raid.healthy,
            "blockers": list(health.raid.blockers),
        },
        "smart": [
            {
                "device": item.device,
                "status": item.status,
                "captured_at": item.captured_at,
                "file_identity": item.file_identity,
                "blockers": list(item.blockers),
            }
            for item in health.smart
        ],
        "blockers": list(health.blockers),
    }


def inspect_storage_evidence(
    paths: EvidencePaths,
    *,
    policy: EvidencePolicy,
    external_targets: tuple[str, ...],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse root evidence after catalog discovery binds backup scope.

    Files are read by descriptor-pinned parsers.  This boundary only turns their
    immutable parsed results into public, secret-free dictionaries.
    """

    observed_at = now or datetime.now(UTC)
    health = inspect_storage_health(paths, policy=policy, now=observed_at)
    try:
        backup = parse_backup_inventory(
            paths.backup,
            policy=policy,
            external_targets=external_targets,
            now=observed_at,
        )
    except ValueError as error:
        raise ColdHostError("descriptor-bound backup evidence is unavailable or invalid") from error
    return (
        health,
        {
            "complete": backup.complete,
            "covered_paths": list(backup.covered_paths),
            "missing_targets": list(backup.missing_targets),
            "file_identity": backup.file_identity,
            "blockers": list(backup.blockers),
        },
    )


def inspect_running_target(docker: DockerBoundary) -> dict[str, Any]:
    """Read one contract's current bind plus in-container writability."""

    identity = docker.identity
    inspect = docker.inspect(identity.container_name)
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise ColdHostError("current Docker mount inventory is malformed")
    matches = [
        mount
        for mount in mounts
        if isinstance(mount, Mapping)
        and mount.get("Source") == str(identity.host_path)
        and mount.get("Destination") == identity.container_path
    ]
    if len(matches) != 1:
        raise ColdHostError("current container does not have exactly one cold bind")
    host = inspect_host_path(identity=identity)
    writable = docker.action(
        (
            identity.docker_bin,
            "exec",
            "--user",
            "postgres",
            identity.container_name,
            "test",
            "-w",
            identity.container_path,
        )
    )
    return {
        "container_name": identity.container_name,
        "container_bind": str(identity.host_path),
        "host_path": str(identity.host_path),
        "device_identity": host["device_identity"],
        "writable": writable["returncode"] == 0,
        "host_mode": host["mode"],
        "host_uid": host["uid"],
        "host_gid": host["gid"],
    }
