"""Bounded host boundaries for offline PGDATA relocation, not cold-tier identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import stat
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.compressed_chunk_cold_target import run_bounded_command
from packages.common.node27_cold_tablespace_container import normalize_raw_inspect
from packages.common.node27_cold_tablespace_evidence import EvidencePolicy, verify_root_storage_evidence
from packages.common.safe_fs import (
    atomic_write_bytes_no_follow,
    directory_identity_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    read_bytes_durable_no_follow,
    stat_no_follow,
    unlink_no_follow_durable,
)

DOCKER = "/usr/bin/docker"
DISPLAY = "nhms-display-api.service"
UNITS = tuple(
    f"nhms-node27-{lane}.{kind}"
    for lane in (
        "autopipe",
        "download",
        "frontier-alert",
        "raw-retention",
        "resource-governance",
        "timeseries-compression",
        "timeseries-retention",
    )
    for kind in ("service", "timer")
) + ("nhms-node27-timeseries-compression-replay.service", DISPLAY)
FENCE = "90-nhms-pgdata-relocation.conf"
OLD_RUNTIME = "/home/nwm/NWM-reslice-original-5a86841c"
ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
TREE_HASH = (
    "tar -C /source --sort=name --numeric-owner --format=posix "
    "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime "
    "--acls --xattrs -cf - . | sha256sum"
)


class MigrationError(RuntimeError):
    """A refused gate; command output and secrets are deliberately not included."""


def require(ok: object, message: str) -> None:
    if not ok:
        raise MigrationError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_read(path: Path, limit: int = 1024 * 1024) -> bytes:
    info = stat_no_follow(path)
    require(
        info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
        "private file must be owned by operator and mode 0600",
    )
    require(info.st_nlink == 1, "private file has unexpected hard links")
    return read_bytes_durable_no_follow(path, max_bytes=limit)


def atomic_private(path: Path, data: bytes) -> None:
    atomic_write_bytes_no_follow(path, data, mode=0o600, require_durable_replace=True)


def path_identity(path: Path) -> list[int]:
    fd = open_directory_no_follow(path)
    try:
        value = os.fstat(fd)
        require(stat.S_IMODE(value.st_mode) & 0o022 == 0, "directory is group/world writable")
        return [value.st_dev, value.st_ino, value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode)]
    finally:
        os.close(fd)


def _unit_environment(value: str | None) -> dict[str, str]:
    try:
        assignments = shlex.split(value or "")
    except ValueError:
        raise MigrationError("unit environment serialization is invalid") from None
    result = {}
    for assignment in assignments:
        name, separator, content = assignment.partition("=")
        require(
            separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in result,
            "unit environment assignment is ambiguous",
        )
        result[name] = content
    return result


def _exec_start_configuration(value: str | None) -> str | None:
    """Keep native command configuration, excluding its trailing execution result."""
    if not value or not value.startswith("{ path="):
        return value
    prefix, separator, result = value.rpartition(" ; start_time=")
    require(
        separator
        and re.fullmatch(
            r"\[[^\]\n]*\] ; stop_time=\[[^\]\n]*\] ; pid=[0-9]+ ; code=[^;{}\n]+ ; status=[^;{}\n]+ }",
            result,
        )
        and "} ; { path=" not in prefix
        and "} { path=" not in prefix,
        "unsupported native ExecStart serialization",
    )
    return prefix + " }"


def _runtime_virtualenv_link(root: Path, host: Host) -> dict[str, Any] | None:
    """Observe only the conventional environment link, never its package tree."""

    def link_identity(info: os.stat_result) -> list[int]:
        return [
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]

    def directory_identity(info: os.stat_result) -> list[int]:
        return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]

    parent_fd = open_directory_no_follow(root)
    try:
        parent = os.fstat(parent_fd)
        try:
            before = os.stat(".venv", dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(before.st_mode):
            return None
        require(
            not host.command(["/usr/bin/git", "-C", str(root), "ls-files", "--cached", "-z", "--", ".venv"]).stdout,
            "runtime virtualenv link must be untracked",
        )
        destination = os.readlink(".venv", dir_fd=parent_fd)
        target = Path(destination)
        require(
            destination and ".." not in target.parts and target.anchor in {"", "/"},
            "unsupported runtime virtualenv link destination",
        )
        if not target.is_absolute():
            target = root / target
        # A direct directory destination is supported; no additional links in
        # its ancestry are followed. Virtualenv permissions are not secret-file
        # permissions, and its live directory timestamps are not stable identity.
        target_fd = open_directory_no_follow(target)
        try:
            target_identity = directory_identity(os.fstat(target_fd))
            require(
                os.readlink(".venv", dir_fd=parent_fd) == destination
                and link_identity(os.stat(".venv", dir_fd=parent_fd, follow_symlinks=False)) == link_identity(before),
                "runtime virtualenv link changed during capture",
            )
            check_fd = open_directory_no_follow(target)
            try:
                require(
                    directory_identity(os.fstat(check_fd)) == target_identity
                    and directory_identity(os.fstat(target_fd)) == target_identity,
                    "runtime virtualenv target changed during capture",
                )
            finally:
                os.close(check_fd)
            require(
                directory_identity_no_follow(root) == (parent.st_dev, parent.st_ino),
                "runtime virtualenv parent changed during capture",
            )
            return {
                "link": link_identity(before),
                "destination": destination,
                "target_directory": target_identity,
            }
        finally:
            os.close(target_fd)
    finally:
        os.close(parent_fd)


def covered_tree(path: Path) -> None:
    """Stream directory entries, refusing links, mount crossings and special files.

    This deliberately admits a self-contained regular-file cluster only. Even an
    internal symlink requires independent operator reconciliation, never copying
    a link whose actual coverage we would otherwise have to guess.
    """
    device = directory_identity_no_follow(path)[0]

    def visit(parent: Path, depth: int) -> None:
        require(depth < 64, "cluster directory depth exceeds coverage bound")
        fd = open_directory_no_follow(parent)
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    require(info.st_dev == device, "cluster contains an external filesystem")
                    require(
                        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                        "cluster contains a symlink or special file; coverage unproved",
                    )
                    if stat.S_ISDIR(info.st_mode):
                        visit(parent / entry.name, depth + 1)
        finally:
            os.close(fd)

    visit(path, 0)


class Host:
    """Real process, filesystem and SQL boundaries; tests replace only IO."""

    def command(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        timeout: int = 30,
        allow_failure: bool = False,
        max_bytes: int = 1024 * 1024,
    ):
        try:
            result = run_bounded_command(argv, timeout=timeout, max_bytes=max_bytes)
        except (ColdRuntimeError, OSError):
            raise MigrationError("host command unavailable, timed out or exceeded output bound") from None
        require(allow_failure or result.returncode == 0, "host command failed; recovery may be required")
        return result

    def inspect(self, identity: str, *, absent_ok: bool = False) -> dict[str, Any] | None:
        result = self.command([DOCKER, "inspect", "--type", "container", identity], allow_failure=True)
        if result.returncode:
            # Distinguish daemon/unavailable errors from a proved absent name.
            require(absent_ok and "No such container:" in result.stderr, "container identity inspection failed")
            return None
        raw = json.loads(result.stdout)
        require(isinstance(raw, list) and len(raw) == 1, "ambiguous container identity")
        document = raw[0]
        require(isinstance(document, dict), "container inspect is not an object")
        normalize_raw_inspect(document)
        return document

    def filesystem(self, path: Path) -> dict[str, Any]:
        value = json.loads(
            self.command(
                ["/usr/bin/findmnt", "--json", "--target", str(path), "--output", "SOURCE,FSTYPE,TARGET,OPTIONS"]
            ).stdout
        )
        rows = value.get("filesystems", [])
        require(len(rows) == 1, "filesystem identity is ambiguous")
        return rows[0]

    def durable_workspace(self, path: Path) -> list[int]:
        require(
            path.is_absolute() and not any(part in {"..", "."} for part in path.parts),
            "workspace must be canonical absolute path",
        )
        require(
            not any(
                path == root or path.is_relative_to(root) for root in (Path("/tmp"), Path("/run"), Path("/var/tmp"))
            ),
            "workspace must not be in ephemeral storage",
        )
        identity = path_identity(path)
        require(
            identity[2] == os.getuid() and identity[4] == 0o700,
            "workspace must already exist, owned by operator, mode 0700",
        )
        fs = self.filesystem(path)
        require(fs["fstype"] not in {"tmpfs", "ramfs", "overlay", "aufs"}, "workspace filesystem is not durable")
        return identity

    def health(self, config: dict[str, Any]) -> dict[str, Any]:
        if config.get("disposable_root"):
            return {"scope": "isolated-mechanics-only", "hardware_health": "not-applicable"}
        require(
            config.get("mdadm_evidence") and len(config.get("smart_evidence", {})) == 2,
            "fresh root mdadm and both SMART evidence files are required",
        )
        policy = EvidencePolicy(
            socket.gethostname(),
            "/dev/md0",
            86400,
            0,
            (0o600, 0o640),
            ("/usr/sbin/mdadm", "--detail", "/dev/md0"),
            ("/usr/sbin/smartctl",),
            (),
            config["source_pgdata"],
        )
        result = verify_root_storage_evidence(
            Path(config["mdadm_evidence"]),
            {key: Path(value) for key, value in config["smart_evidence"].items()},
            policy=policy,
            now=datetime.now(UTC),
        )
        require(result.healthy, "root storage health is not PASS")
        fs = self.filesystem(Path(config["target_pgdata"]).parent)
        require(
            fs["source"] == "/dev/md0" and fs["target"] == "/data/GHDC",
            "target is not the evidenced GHDC RAID filesystem",
        )
        return {
            "scope": "root-storage-health",
            "members": list(result.members),
            "raid": result.raid.file_identity,
            "smart": [item.file_identity for item in result.smart],
        }

    def helper(
        self,
        state: dict[str, Any],
        script: str,
        *,
        target: bool = False,
        payload: Path | None = None,
        timeout: int = 120,
    ) -> str:
        config = state["config"]
        source = Path(config["target_pgdata"] if target else config["source_pgdata"])
        expected = state["target_identity"] if target else state["source_identity"]
        require(path_identity(source) == expected, "bound source path identity drifted")
        argv = [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/bash",
            "--label",
            "nhms.pgdata.operation=" + state["operation"],
            "--mount",
            f"type=bind,src={source},dst=/source,readonly",
        ]
        checks = f'test "$(stat -c %d:%i /source)" = "{expected[0]}:{expected[1]}"; '
        if payload is not None:
            argv += ["--mount", f"type=bind,src={payload},dst=/payload,readonly"]
        if target:
            # Reading helpers remain read-only. HBA edits explicitly use /destination.
            pass
        if "/destination" in script:
            destination = Path(config["target_pgdata"])
            identity = state["target_identity"]
            require(path_identity(destination) == identity, "bound target path identity drifted")
            argv += ["--mount", f"type=bind,src={destination},dst=/destination"]
            checks += f'test "$(stat -c %d:%i /destination)" = "{identity[0]}:{identity[1]}"; '
        argv += [state["image"], "-euo", "pipefail", "-c", checks + script]
        return self.command(argv, timeout=timeout).stdout.strip()

    def control(self, state: dict[str, Any]) -> str:
        text = self.helper(state, "LC_ALL=C pg_controldata /source")
        lines = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        require(
            lines.get("Database cluster state", "").strip() == "shut down",
            "source control state is not cleanly shut down",
        )
        identity = lines.get("Database system identifier", "").strip()
        require(identity.isdigit(), "source system identifier unavailable")
        return identity

    def fingerprint(self, state: dict[str, Any], *, target: bool = False) -> str:
        value = self.helper(state, TREE_HASH, target=target, timeout=86400).split()[0]
        require(re.fullmatch(r"[0-9a-f]{64}", value), "invalid complete-tree proof")
        return value

    def admin(self, state: dict[str, Any], sql: str, *, candidate: bool = False) -> Any:
        config = state["config"]
        container = state["candidate_id"] if candidate else state["original_id"]
        value = self.command(
            [
                DOCKER,
                "exec",
                "--user",
                state["numeric_user"],
                container,
                "psql",
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                config["admin_role"],
                "-d",
                config["database"],
                "-c",
                sql,
            ]
        ).stdout.strip()
        return json.loads(value)

    def connection(self, state: dict[str, Any], kind: str):
        import psycopg2
        from psycopg2.extensions import parse_dsn

        dsn = private_read(Path(state["config"][kind + "_dsn_file"]), 16384).decode().strip()
        require(digest(dsn.encode()) == state["credentials"][kind]["digest"], "credential file changed")
        options = parse_dsn(dsn)
        # No service indirection, multi-host fallback or ambient endpoint selection.
        require(
            set(options) <= {"host", "port", "dbname", "user", "password", "sslmode"},
            "DSN uses unsupported connection indirection/options",
        )
        expected = state["credentials"][kind]
        require(
            all(options.get(key) == expected[key] for key in ("host", "port", "dbname", "user")),
            "business connection identity drifted",
        )
        return psycopg2.connect(
            **options,
            connect_timeout=5,
            options="-c statement_timeout=20000 -c lock_timeout=2000",
            application_name="nhms-pgdata-relocation-proof",
        )

    def read_proof(self, state: dict[str, Any]) -> dict[str, Any]:
        with closing(self.connection(state, "reader")) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user, current_database(), pg_is_in_recovery()")
                role, database, recovery = cursor.fetchone()
                require(
                    role == state["credentials"]["reader"]["user"]
                    and database == state["config"]["database"]
                    and not recovery,
                    "read proof connected to an unexpected principal/database",
                )
                cursor.execute("SELECT count(*) FROM pg_catalog.pg_class")
                count = cursor.fetchone()[0]
                require(count > 0, "read proof failed")
        return {"principal": role, "database": database, "read": True}

    def writer_rejected(self, state: dict[str, Any]) -> None:
        import psycopg2

        try:
            connection = self.connection(state, "writer")
        except psycopg2.OperationalError as error:
            # HBA rejection must be explicit, not password failure, DNS or timeout.
            require("pg_hba.conf rejects connection" in str(error), "writer rejection was not an HBA denial")
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN READ WRITE")
            raise MigrationError("business writer connected through the admission fence")
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def unit(self, name: str) -> dict[str, str]:
        properties = (
            "Id",
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "Type",
            "WorkingDirectory",
            "ExecStart",
            "FragmentPath",
            "DropInPaths",
            "EnvironmentFiles",
            "Environment",
        )
        result = self.command(["/usr/bin/systemctl", "--user", "show", name, "--property=" + ",".join(properties)])
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def unit_files(self, value: dict[str, str]) -> dict[str, str]:
        paths = [value.get("FragmentPath", ""), *value.get("DropInPaths", "").split()]
        return {
            path: digest(read_bytes_durable_no_follow(Path(path), max_bytes=256 * 1024))
            for path in paths
            if path and Path(path).name != FENCE
        }

    def environment_files(self, value: dict[str, str]) -> dict[str, dict[str, Any]]:
        text = value.get("EnvironmentFiles", "")
        matches = re.findall(r"(\S+) \(ignore_errors=(?:yes|no)\)", text)
        sourced = re.findall(r"(?:\.\s+|source\s+)(/[^\s;'\"]+)", value.get("ExecStart", ""))
        require(not text or matches, "environment file list is not representable")
        primary = []
        unit = value.get("Id", "")
        if unit in UNITS and unit.startswith("nhms-node27-") and unit.endswith(".service"):
            stem = unit.removeprefix("nhms-node27-").removesuffix(".service")
            key = f"NODE27_{stem.upper().replace('-', '_')}_ENV_FILE"
            environment = _unit_environment(value.get("Environment"))
            if key in environment:
                require(environment[key], "primary caller environment file is empty")
                primary.append(environment[key])
        result = {}
        for name in dict.fromkeys([*matches, *sourced, *primary]):
            path = Path(name)
            require(path.is_absolute(), "environment file is not absolute")
            data = read_bytes_durable_no_follow(path, max_bytes=256 * 1024)
            info = stat_no_follow(path)
            require(
                info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                "caller environment file must be private",
            )
            lines = data.splitlines(keepends=True)
            keys = [i for i, line in enumerate(lines) if line.startswith(b"NODE27_GOVERNANCE_PGDATA_ROOT=")]
            require(len(keys) <= 1, "ambiguous governance PGDATA setting")
            setting = None
            if keys:
                index = keys[0]
                setting = lines[index].split(b"=", 1)[1].strip().decode()
                lines[index] = b"NODE27_GOVERNANCE_PGDATA_ROOT=<bound>\n"
            result[name] = {"digest": digest(data), "stable": digest(b"".join(lines)), "pgdata": setting}
        return result

    def units_snapshot(self, config: dict[str, Any]) -> dict[str, Any]:
        if config.get("disposable_root"):
            return {}
        for kind in ("service", "timer"):
            cold = self.unit("nhms-node27-cold-residency." + kind)
            require(
                cold.get("ActiveState") not in {"active", "activating"}
                and cold.get("UnitFileState") not in {"enabled", "enabled-runtime"},
                "incompatible cold-residency lane is active/enabled",
            )
        result = {}
        runtimes = {}
        for name in UNITS:
            value = self.unit(name)
            if value.get("LoadState") == "not-found":
                result[name] = {"absent": True}
                continue
            require(value.get("LoadState") in {"loaded", "masked"}, "unit is not safely observable")
            require(
                value.get("ActiveState") in {"active", "inactive", "failed"}
                or (
                    value.get("ActiveState") == "activating"
                    and name.endswith(".service")
                    and name != DISPLAY
                    and value.get("Type") == "oneshot"
                ),
                "unit is transitioning unexpectedly",
            )
            require(not self.fence_path(name).exists(), "preexisting migration fence requires recovery")
            _exec_start_configuration(value.get("ExecStart"))
            _unit_environment(value.get("Environment"))
            if name == DISPLAY or name == "nhms-node27-autopipe.service":
                require(
                    value.get("WorkingDirectory") == OLD_RUNTIME and OLD_RUNTIME in value.get("ExecStart", ""),
                    "actual application runtime is not the approved old checkout",
                )
            root = value.get("WorkingDirectory", "")
            runtime = None
            if root:
                require(Path(root).is_absolute(), "caller runtime directory is not absolute")
                if root not in runtimes:
                    runtimes[root] = self.runtime_identity(Path(root))
                runtime = runtimes[root]
            result[name] = {
                "observed": value,
                "files": self.unit_files(value),
                "runtime": runtime,
                "environment": self.environment_files(value),
            }
        return result

    def fence_path(self, name: str) -> Path:
        require(name in UNITS, "unit is outside the fixed fence set")
        return Path.home() / ".config/systemd/user" / (name + ".d") / FENCE

    def fence_bytes(self, state: dict[str, Any]) -> bytes:
        # An operation-owned never-created path, not a PID or ephemeral lock.
        path = Path(state["workspace"]) / ("resume-forbidden-" + state["operation"])
        require(not path.exists() and not path.is_symlink(), "fence condition unexpectedly satisfied")
        return f"[Unit]\nConditionPathExists={path}\n".encode()

    def verify_units(self, state: dict[str, Any], *, required_fences: bool = False) -> None:
        runtimes = {}
        for name, frozen in state["units"].items():
            current = self.unit(name)
            if frozen.get("absent"):
                require(current.get("LoadState") == "not-found", "previously absent unit appeared")
                continue
            require(self.unit_files(current) == frozen["files"], "foreign runtime/unit dropins changed")
            for key in ("Id", "WorkingDirectory", "EnvironmentFiles"):
                require(current.get(key) == frozen["observed"].get(key), "effective caller configuration changed")
            require(
                _exec_start_configuration(current.get("ExecStart"))
                == _exec_start_configuration(frozen["observed"].get("ExecStart")),
                "effective caller command changed",
            )
            require(
                _unit_environment(current.get("Environment"))
                == _unit_environment(frozen["observed"].get("Environment")),
                "effective caller environment changed",
            )
            if frozen.get("runtime") is not None:
                root = current["WorkingDirectory"]
                if root not in runtimes:
                    runtimes[root] = self.runtime_identity(Path(root))
                require(
                    runtimes[root] == frozen["runtime"],
                    "actual runtime Git/code identity changed",
                )
            environment = self.environment_files(current)
            require(environment.keys() == frozen["environment"].keys(), "caller environment paths changed")
            for path, before in frozen["environment"].items():
                after = environment[path]
                if after == before:
                    continue
                require(
                    name == "nhms-node27-resource-governance.service"
                    and before["pgdata"] == state["config"]["source_pgdata"]
                    and after["pgdata"] == state["config"]["target_pgdata"]
                    and after["stable"] == before["stable"]
                    and state["stage"] in {"activated_readonly", "release_intent"},
                    "caller environment changed outside the governance PGDATA exception",
                )
            require(
                current.get("UnitFileState") == frozen["observed"].get("UnitFileState"),
                "unit enablement changed outside relocation",
            )
            path = self.fence_path(name)
            if path.exists():
                require(private_read(path) == self.fence_bytes(state), "persistent fence ownership differs")
            elif required_fences and not (name == DISPLAY and state.get("display_unfenced")):
                raise MigrationError("required persistent fence is absent")

    def install_fences(self, state: dict[str, Any]) -> None:
        for name, frozen in state["units"].items():
            if not frozen.get("absent"):
                ensure_directory_no_follow(self.fence_path(name).parent)
                atomic_private(self.fence_path(name), self.fence_bytes(state))
        self.command(["/usr/bin/systemctl", "--user", "daemon-reload"])
        for name, frozen in state["units"].items():
            if not frozen.get("absent") and name.endswith(".timer"):
                if self.unit(name).get("ActiveState") in {"active", "activating", "deactivating"}:
                    self.command(["/usr/bin/systemctl", "--user", "stop", name])
        deadline = time.monotonic() + state["config"]["drain_timeout"]
        while True:
            pending = []
            for name, frozen in state["units"].items():
                if frozen.get("absent") or not name.endswith(".service") or name == DISPLAY:
                    continue
                value = self.unit(name)
                if value.get("ActiveState") in {"active", "activating", "deactivating"}:
                    require(value.get("Type") == "oneshot", "unknown persistent writer cannot be drained")
                    pending.append(name)
            if not pending:
                break
            require(time.monotonic() < deadline, "writer drain timed out; services were not killed")
            time.sleep(1)
        if state["units"] and not state["units"][DISPLAY].get("absent"):
            # Leave failed/inactive units alone; stopping a failed unit can clear
            # its failure state even though there is no daemon left to fence.
            if self.unit(DISPLAY).get("ActiveState") in {"active", "activating", "deactivating"}:
                self.command(["/usr/bin/systemctl", "--user", "stop", DISPLAY])
        self.verify_units(state, required_fences=True)

    def release_callers_ready(self, state: dict[str, Any]) -> None:
        governance = state["units"].get("nhms-node27-resource-governance.service", {})
        if governance and not governance.get("absent"):
            current = self.environment_files(self.unit("nhms-node27-resource-governance.service"))
            settings = [item["pgdata"] for item in current.values() if item["pgdata"] is not None]
            require(settings == [state["config"]["target_pgdata"]], "governance PGDATA target is not updated")

    def restore_units(self, state: dict[str, Any], *, display_only: bool = False) -> None:
        self.verify_units(state)
        if state["writes_released"] and not display_only:
            self.release_callers_ready(state)
        for name, frozen in state["units"].items():
            if frozen.get("absent") or (display_only and name != DISPLAY):
                continue
            path = self.fence_path(name)
            if path.exists():
                require(private_read(path) == self.fence_bytes(state), "foreign fence cannot be removed")
                unlink_no_follow_durable(path)
        if state["units"]:
            self.command(["/usr/bin/systemctl", "--user", "daemon-reload"])
        for name, frozen in state["units"].items():
            if frozen.get("absent") or (display_only and name != DISPLAY):
                continue
            value = frozen["observed"]
            # Never replay a completed oneshot or clear an originally failed unit.
            if value.get("ActiveState") == "active" and (name.endswith(".timer") or value.get("Type") != "oneshot"):
                self.command(["/usr/bin/systemctl", "--user", "start", name])

    def identity(self) -> dict[str, Any]:
        return {"hostname": socket.gethostname(), "operator_uid": os.getuid(), "operator_gid": os.getgid()}

    def runtime_identity(self, root: Path) -> dict[str, Any]:
        identity = path_identity(root)
        virtualenv_link = _runtime_virtualenv_link(root, self)
        head = self.command(["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{40}", head), "runtime Git identity is unavailable")
        paths = self.command(
            ["/usr/bin/git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        ).stdout.split("\x00")
        files = {}
        for name in paths:
            if not name:
                continue
            path = root / name
            require(path.is_relative_to(root) and ".." not in Path(name).parts, "unsafe runtime path")
            if name == ".venv" and virtualenv_link is not None:
                continue
            files[name] = digest(read_bytes_durable_no_follow(path, max_bytes=64 * 1024 * 1024))
        require(files, "runtime tracked code is absent")
        require(
            _runtime_virtualenv_link(root, self) == virtualenv_link and path_identity(root) == identity,
            "runtime virtualenv or directory changed during capture",
        )
        result = {
            "directory": identity,
            "head": head,
            "tree_digest": digest(json.dumps(files, sort_keys=True).encode()),
        }
        if virtualenv_link is not None:
            result["virtualenv_link"] = virtualenv_link
        return result

    def display_ready(self, state: dict[str, Any]) -> None:
        frozen = state["units"].get(DISPLAY, {})
        if frozen.get("absent") or not frozen:
            return
        if frozen["observed"].get("ActiveState") != "active":
            return
        command = frozen["observed"].get("ExecStart", "")
        match = re.search(r"NHMS_DISPLAY_API_PORT:-([0-9]+)|--port\s+([0-9]+)", command)
        port = next((group for group in match.groups() if group), None) if match else None
        for name in frozen["environment"]:
            data = read_bytes_durable_no_follow(Path(name), max_bytes=256 * 1024).decode()
            for line in data.splitlines():
                if line.startswith("NHMS_DISPLAY_API_PORT="):
                    port = line.partition("=")[2].strip().strip("'\"")
        require(port and port.isdigit() and 1 <= int(port) <= 65535, "display readiness endpoint is unproved")
        deadline = time.monotonic() + 120
        while True:
            healthy = False
            if self.unit(DISPLAY).get("ActiveState") == "active":
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
                        body = json.loads(response.read(65536))
                        healthy = (
                            response.status == 200
                            and isinstance(body, dict)
                            and body.get("status") == "ok"
                            and body.get("service") == "nhms-api"
                        )
                except (OSError, URLError, ValueError):
                    pass
            if healthy:
                return
            require(time.monotonic() < deadline, "restored display readiness failed; writers remain fenced")
            time.sleep(1)
