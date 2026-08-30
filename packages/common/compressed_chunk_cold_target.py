"""Read-only production inspector for the cold tablespace bind/device identity."""

from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow

LIVE_CONTAINER_NAME = "nhms-db"
CONTAINER_COLD_PATH = "/home/postgres/pgdata/tablespaces/nhms_cold"
HOST_COLD_PATH = "/data/GHDC/nhms-cold-tablespace"
TRUSTED_DOCKER_BIN = "/usr/bin/docker"
INSPECT_TIMEOUT_SECONDS = 5
INSPECT_OUTPUT_MAX_BYTES = 64 * 1024
CONTAINER_WRITABLE_ARGV = (
    "/usr/bin/docker",
    "exec",
    "--user",
    "postgres",
    "nhms-db",
    "test",
    "-w",
    "/home/postgres/pgdata/tablespaces/nhms_cold",
)

DockerRunner = Callable[..., subprocess.CompletedProcess[str]]
HostInspectFn = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class HostIdentity:
    device_identity: str
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ObservedTarget:
    container_name: str
    container_bind: str
    host_path: str
    device_identity: str
    writable: bool
    host_mode: int
    host_uid: int
    host_gid: int


def _require_trusted_docker(docker_bin: str) -> None:
    if docker_bin != TRUSTED_DOCKER_BIN:
        raise ColdRuntimeError(
            "docker binary must be the trusted absolute path",
            error_class="target_identity",
            stage="target_identity",
        )


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except Exception:
        pass


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def run_bounded_command(
    argv: Sequence[str],
    *,
    timeout: int = INSPECT_TIMEOUT_SECONDS,
    max_bytes: int = INSPECT_OUTPUT_MAX_BYTES,
    runner: DockerRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    if runner is not None:
        try:
            result = runner(list(argv), check=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ColdRuntimeError(
                "target inspector timed out",
                error_class="target_identity",
                stage="target_identity",
            ) from error
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if len(stdout.encode("utf-8")) > max_bytes or len(stderr.encode("utf-8")) > max_bytes:
            raise ColdRuntimeError(
                "target inspector output exceeds the byte ceiling",
                error_class="target_identity",
                stage="target_identity",
            )
        return result
    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as error:
        raise ColdRuntimeError(
            "target inspector unavailable",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_open = process.stdout is not None
    stderr_open = process.stderr is not None
    started = time.monotonic()
    try:
        while stdout_open or stderr_open:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _kill(process)
                raise ColdRuntimeError(
                    "target inspector timed out",
                    error_class="target_identity",
                    stage="target_identity",
                )
            watch: list[Any] = []
            if stdout_open and process.stdout is not None:
                watch.append(process.stdout)
            if stderr_open and process.stderr is not None:
                watch.append(process.stderr)
            ready, _writers, _errors = select.select(watch, [], [], min(0.1, remaining))
            if not ready:
                if process.poll() is not None and not watch:
                    break
                continue
            for pipe in ready:
                chunk = os.read(pipe.fileno(), 4096)
                target = stdout_buf if pipe is process.stdout else stderr_buf
                if not chunk:
                    if pipe is process.stdout:
                        stdout_open = False
                    else:
                        stderr_open = False
                    continue
                if len(target) + len(chunk) > max_bytes:
                    _kill(process)
                    raise ColdRuntimeError(
                        "target inspector output exceeds the byte ceiling",
                        error_class="target_identity",
                        stage="target_identity",
                    )
                target.extend(chunk)
        returncode = process.wait(timeout=1)
    except ColdRuntimeError:
        raise
    except subprocess.TimeoutExpired as error:
        _kill(process)
        raise ColdRuntimeError(
            "target inspector timed out",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    except OSError as error:
        _kill(process)
        raise ColdRuntimeError(
            "target inspector unavailable",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    finally:
        if process.poll() is None:
            _kill(process)
        _close_pipes(process)
    return subprocess.CompletedProcess(
        list(argv),
        returncode if returncode is not None else 1,
        stdout_buf.decode("utf-8", errors="replace"),
        stderr_buf.decode("utf-8", errors="replace"),
    )


def inspect_nhms_db_cold_bind(
    *,
    container_name: str = LIVE_CONTAINER_NAME,
    expected_container_path: str = CONTAINER_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
) -> str:
    _require_trusted_docker(docker_bin)
    result = run_bounded_command(
        [docker_bin, "inspect", "--format", "{{json .Mounts}}", container_name],
        runner=runner,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().split("\n")[0]
        raise ColdRuntimeError(
            f"target inspector could not inspect {container_name}: {stderr or 'unavailable'}",
            error_class="target_identity",
            stage="target_identity",
        )
    try:
        mounts = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise ColdRuntimeError(
            "target inspector returned malformed mount JSON",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    if not isinstance(mounts, list):
        raise ColdRuntimeError(
            "target inspector returned malformed mount JSON",
            error_class="target_identity",
            stage="target_identity",
        )
    matches: list[str] = []
    for mount in mounts:
        if not isinstance(mount, Mapping):
            continue
        destination = str(mount.get("Destination") or "")
        source = str(mount.get("Source") or "")
        if destination == expected_container_path and source:
            matches.append(source)
    if len(matches) != 1:
        raise ColdRuntimeError(
            "target inspector did not find exactly one cold tablespace bind",
            error_class="target_identity",
            stage="target_identity",
        )
    return matches[0]


def inspect_host_path(host_path: str) -> HostIdentity:
    path = Path(host_path)
    if not path.is_absolute():
        raise ColdRuntimeError(
            "target host path must be absolute",
            error_class="target_identity",
            stage="target_identity",
        )
    try:
        fd = open_directory_no_follow(path)
    except (OSError, SafeFilesystemError) as error:
        raise ColdRuntimeError(
            f"target host path is unavailable: {error}",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ColdRuntimeError(
                "target host path must be a directory",
                error_class="target_identity",
                stage="target_identity",
            )
        named = os.lstat(path)
        if stat.S_ISLNK(named.st_mode):
            raise ColdRuntimeError(
                "target host path must not be a symlink",
                error_class="target_identity",
                stage="target_identity",
            )
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise ColdRuntimeError(
                "target host path identity drifted during inspection",
                error_class="target_identity",
                stage="target_identity",
            )
        return HostIdentity(
            device_identity=f"{info.st_dev}:{info.st_ino}",
            mode=stat.S_IMODE(info.st_mode),
            uid=int(info.st_uid),
            gid=int(info.st_gid),
        )
    finally:
        os.close(fd)


def inspect_container_writable(
    *,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
) -> bool:
    _require_trusted_docker(docker_bin)
    result = run_bounded_command(CONTAINER_WRITABLE_ARGV, runner=runner)
    if result.returncode != 0:
        raise ColdRuntimeError(
            "container-side cold tablespace path is not writable",
            error_class="target_identity",
            stage="target_identity",
        )
    return True


def _observe_host_identity(host_bind: str, host_inspect: HostInspectFn | None) -> tuple[str, int, int, int]:
    if host_inspect is None:
        identity = inspect_host_path(host_bind)
        return identity.device_identity, identity.mode, identity.uid, identity.gid
    observed = dict(host_inspect(host_bind))
    device_identity = str(observed.get("device_identity") or "")
    host_mode = int(observed.get("mode") or 0)
    host_uid = int(observed.get("uid") or 0)
    host_gid = int(observed.get("gid") or 0)
    if not device_identity:
        raise ColdRuntimeError(
            "target inspector did not observe host identity",
            error_class="target_identity",
            stage="target_identity",
        )
    return device_identity, host_mode, host_uid, host_gid


def inspect_production_target(
    *,
    container_name: str = LIVE_CONTAINER_NAME,
    expected_container_path: str = CONTAINER_COLD_PATH,
    expected_host_path: str = HOST_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
    host_inspect: HostInspectFn | None = None,
) -> ObservedTarget:
    _require_trusted_docker(docker_bin)
    host_bind = inspect_nhms_db_cold_bind(
        container_name=container_name,
        expected_container_path=expected_container_path,
        docker_bin=docker_bin,
        runner=runner,
    )
    if host_bind != expected_host_path:
        raise ColdRuntimeError(
            "container bind source drifted from expected host path",
            error_class="target_identity",
            stage="target_identity",
        )
    before = _observe_host_identity(host_bind, host_inspect)
    writable = inspect_container_writable(docker_bin=docker_bin, runner=runner)
    after = _observe_host_identity(host_bind, host_inspect)
    if after != before:
        raise ColdRuntimeError(
            "target host path identity drifted after writable check",
            error_class="target_identity",
            stage="target_identity",
        )
    device_identity, host_mode, host_uid, host_gid = before
    return ObservedTarget(
        container_name=container_name,
        container_bind=host_bind,
        host_path=host_bind,
        device_identity=device_identity,
        writable=writable,
        host_mode=host_mode,
        host_uid=host_uid,
        host_gid=host_gid,
    )


def production_inspect_target(**kwargs: Any) -> Mapping[str, Any]:
    observed = inspect_production_target(**kwargs)
    return {
        "container_name": observed.container_name,
        "container_bind": observed.container_bind,
        "host_path": observed.host_path,
        "device_identity": observed.device_identity,
        "writable": observed.writable,
        "host_mode": observed.host_mode,
        "host_uid": observed.host_uid,
        "host_gid": observed.host_gid,
    }
