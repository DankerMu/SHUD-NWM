"""Read-only production inspector for the cold tablespace bind and runtime identity.

Issue #1929: the writability probe must measure the principal that actually writes
the tablespace — the container's numeric runtime UID/GID — never an image user name.
One bounded, inert ``docker inspect`` projection observes ``.Mounts`` plus
``.Config.User``; only after the observed pair equals the explicitly configured
pair may ``test -w`` run as that exact ``uid:gid``. There is no ``postgres``, root,
image-default or UID-only fallback on any path.
"""

from __future__ import annotations

import json
import os
import re
import select
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow

LIVE_CONTAINER_NAME = "nhms-db"
CONTAINER_COLD_PATH = "/home/postgres/pgdata/tablespaces/nhms_cold"
HOST_COLD_PATH = "/data/GHDC/nhms-cold-tablespace"
TRUSTED_DOCKER_BIN = "/usr/bin/docker"
INSPECT_TIMEOUT_SECONDS = 5
INSPECT_OUTPUT_MAX_BYTES = 64 * 1024
# `uid_t`/`gid_t` are 32-bit on the pinned image; 0 is root and 2**32-1 is the
# (uid_t)-1 sentinel, so the representable non-root domain is 1..2**32-2.
CONTAINER_EXEC_ID_MIN = 1
CONTAINER_EXEC_ID_MAX = 2**32 - 2
# Width of the largest accepted decimal token (10). Derived from the bound, never
# a literal, so the two cannot drift apart.
CONTAINER_EXEC_ID_DIGITS_MAX = len(str(CONTAINER_EXEC_ID_MAX))

# A small projection, not the full inspect document: the only two fields the
# target preflight consumes, so the 64-KiB ceiling stays meaningful.
#
# This is a Go text/template literal, which is what `docker inspect --format`
# actually evaluates. It must NOT use `dict`: that is a sprig/Helm helper, is
# not part of Docker's template function set, and fails client-side before any
# daemon lookup (Docker 29.1.3: exit 64 `template parsing error: ... function
# "dict" not defined`). `{{json X}}` emits valid JSON for each value, so the
# surrounding literals keep the whole line parseable as one JSON object.
INSPECT_FORMAT = '{"Mounts":{{json .Mounts}},"User":{{json .Config.User}}}'

_CANONICAL_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_NUMERIC_PAIR_RE = re.compile(r"^([0-9]+):([0-9]+)$")

DockerRunner = Callable[..., subprocess.CompletedProcess[str]]
HostInspectFn = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class HostIdentity:
    device_identity: str
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ContainerExecIdentity:
    """Strictly canonical numeric ``Config.User`` observed from the container."""

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
    container_exec_uid: int
    container_exec_gid: int


class ContainerExecIdentityError(ColdRuntimeError):
    """Refusal carrying the config/inspector error class and stage."""


def validate_container_exec_id(value: Any, *, name: str) -> int:
    """Canonical decimal integer in ``1..4294967294``; no bool, name, or fallback.

    ``bool`` is rejected despite subclassing ``int``: a JSON ``true`` or a Python
    ``True`` is never an observed principal.
    """

    if isinstance(value, bool):
        raise ContainerExecIdentityError(
            f"{name} must be an integer, not a boolean",
            error_class="config",
            stage="config",
        )
    if not isinstance(value, int):
        raise ContainerExecIdentityError(
            f"{name} must be an integer",
            error_class="config",
            stage="config",
        )
    if value < CONTAINER_EXEC_ID_MIN or value > CONTAINER_EXEC_ID_MAX:
        raise ContainerExecIdentityError(
            container_exec_id_message(name, value),
            error_class="config",
            stage="config",
        )
    return int(value)


def require_container_exec_pair(uid: Any, gid: Any, *, uid_name: str, gid_name: str) -> tuple[int, int]:
    """Validate a half-pair-complete principal; one component alone is refused."""

    return (
        validate_container_exec_id(uid, name=uid_name),
        validate_container_exec_id(gid, name=gid_name),
    )


def is_canonical_decimal(text: str) -> bool:
    """``0``, ``007``, ``+7``, ``" 7 "``, ``7_0`` and names are all non-canonical."""

    return _CANONICAL_DECIMAL_RE.fullmatch(text) is not None


def safe_token_echo(text: str, *, limit: int = CONTAINER_EXEC_ID_DIGITS_MAX) -> str:
    """Bounded rendering of an untrusted token for use inside a refusal message.

    Receipt ``reason`` is capped at 256 characters by the shipping schema, so a
    refusal may never interpolate an unbounded value: echoing a 5000-character
    env token would produce a config tombstone that fails schema validation and
    is therefore never published — the very bypass this parse exists to close.
    """

    if len(text) <= limit:
        return text
    return f"a {len(text)}-character token"


def container_exec_id_from_decimal(text: str) -> int | None:
    """Bounded parse of one canonical decimal identity token.

    Returns ``None`` when the value is above ``CONTAINER_EXEC_ID_MAX``; below
    ``CONTAINER_EXEC_ID_MIN`` stays with the caller because the accepted floor is
    site-specific (0 is a legal uid_t but never a legal exec principal).

    ``int()`` is only ever reached on a token at most as long as the bound's own
    decimal width. CPython 3.11+ raises a bare ``ValueError: Exceeds the limit
    (4300 digits)`` for ``int('9' * 5000)``, and such an error carries no
    ``error_class``/``stage``, so it escapes both typed refusals. The value
    comparison after conversion is a backstop for a wider bound, not the escape
    hatch for this one.
    """

    if len(text) > CONTAINER_EXEC_ID_DIGITS_MAX:
        return None
    value = int(text)
    if value > CONTAINER_EXEC_ID_MAX:
        return None
    return value


def container_exec_id_message(label: str, given: str | int) -> str:
    """Range refusal text, bounded in size for both string and int inputs."""

    bound = f"{label} must be within {CONTAINER_EXEC_ID_MIN}..{CONTAINER_EXEC_ID_MAX}"
    if isinstance(given, str):
        return f"{bound}, got {safe_token_echo(given)}"
    # str() of an int with more than the interpreter's digit limit raises
    # ValueError inside the message, replacing the refusal; compare instead.
    if given >= 10**CONTAINER_EXEC_ID_DIGITS_MAX:
        return f"{bound}, got a number of more than {CONTAINER_EXEC_ID_DIGITS_MAX} digits"
    return f"{bound}, got {given}"


def _reject_observed_identity(reason: str) -> NoReturn:
    raise ContainerExecIdentityError(reason, error_class="target_identity", stage="target_identity")


def parse_container_exec_user(raw: Any) -> ContainerExecIdentity:
    """Parse strict canonical numeric ``<uid>:<gid>`` from ``Config.User``.

    Empty, missing, name-only, UID-only, whitespace-padded, non-canonical, root
    and above-bound values all refuse here — long before any writability
    command, and never with a fallback to an image user name.
    """

    if not isinstance(raw, str):
        _reject_observed_identity("container Config.User must be a canonical numeric uid:gid string")
    match = _NUMERIC_PAIR_RE.fullmatch(raw)
    if match is None:
        _reject_observed_identity("container Config.User is not a canonical numeric uid:gid pair")
    pair: list[int] = []
    for text, component in zip(match.groups(), ("uid", "gid"), strict=True):
        if _CANONICAL_DECIMAL_RE.fullmatch(text) is None:
            _reject_observed_identity(f"container Config.User {component} is not a canonical decimal integer")
        value = container_exec_id_from_decimal(text)
        if value is None or value < CONTAINER_EXEC_ID_MIN:
            _reject_observed_identity(
                container_exec_id_message(f"container Config.User {component}", text),
            )
        pair.append(value)
    return ContainerExecIdentity(uid=pair[0], gid=pair[1])


def container_writable_argv(
    *,
    uid: int,
    gid: int,
    container_name: str = LIVE_CONTAINER_NAME,
    container_path: str = CONTAINER_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
) -> tuple[str, ...]:
    """Exact numeric-principal writability probe: no shell, no user name."""

    validate_container_exec_id(uid, name="expected_container_exec_uid")
    validate_container_exec_id(gid, name="expected_container_exec_gid")
    _require_trusted_docker(docker_bin)
    return (
        docker_bin,
        "exec",
        "--user",
        f"{uid}:{gid}",
        container_name,
        "test",
        "-w",
        container_path,
    )


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


def inspect_container_identity_observation(
    *,
    container_name: str = LIVE_CONTAINER_NAME,
    expected_container_path: str = CONTAINER_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
) -> tuple[str, ContainerExecIdentity]:
    """One bounded inspect proving exactly one cold bind and numeric ``Config.User``.

    Both facts come from the same snapshot, so the pair that authorizes the
    writability probe is the pair that container was serving the bind with.
    """

    _require_trusted_docker(docker_bin)
    result = run_bounded_command(
        [docker_bin, "inspect", "--format", INSPECT_FORMAT, container_name],
        timeout=INSPECT_TIMEOUT_SECONDS,
        max_bytes=INSPECT_OUTPUT_MAX_BYTES,
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
        projection = json.loads(result.stdout or "")
    except ValueError as error:
        # ValueError, not just json.JSONDecodeError: the decoder converts numeric
        # literals with int(), which on CPython 3.11+ raises a bare
        # "Exceeds the limit (4300 digits)" for an over-width number that still
        # fits the 64-KiB ceiling. Either way the projection is unusable and must
        # refuse here, before any writability probe.
        raise ColdRuntimeError(
            "target inspector returned malformed mount JSON",
            error_class="target_identity",
            stage="target_identity",
        ) from error
    if not isinstance(projection, Mapping):
        raise ColdRuntimeError(
            "target inspector returned malformed mount JSON",
            error_class="target_identity",
            stage="target_identity",
        )
    mounts = projection.get("Mounts")
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
    return matches[0], parse_container_exec_user(projection.get("User"))


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
    uid: int,
    gid: int,
    container_name: str = LIVE_CONTAINER_NAME,
    container_path: str = CONTAINER_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
) -> bool:
    """``test -w`` as the exact numeric principal; a non-zero rc refuses."""

    argv = container_writable_argv(
        uid=uid,
        gid=gid,
        container_name=container_name,
        container_path=container_path,
        docker_bin=docker_bin,
    )
    result = run_bounded_command(
        argv,
        timeout=INSPECT_TIMEOUT_SECONDS,
        max_bytes=INSPECT_OUTPUT_MAX_BYTES,
        runner=runner,
    )
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
    expected_container_exec_uid: int,
    expected_container_exec_gid: int,
    container_name: str = LIVE_CONTAINER_NAME,
    expected_container_path: str = CONTAINER_COLD_PATH,
    expected_host_path: str = HOST_COLD_PATH,
    docker_bin: str = TRUSTED_DOCKER_BIN,
    runner: DockerRunner | None = None,
    host_inspect: HostInspectFn | None = None,
) -> ObservedTarget:
    _require_trusted_docker(docker_bin)
    expected = require_container_exec_pair(
        expected_container_exec_uid,
        expected_container_exec_gid,
        uid_name="expected_container_exec_uid",
        gid_name="expected_container_exec_gid",
    )
    host_bind, observed_user = inspect_container_identity_observation(
        container_name=container_name,
        expected_container_path=expected_container_path,
        docker_bin=docker_bin,
        runner=runner,
    )
    if (observed_user.uid, observed_user.gid) != expected:
        # Truthful refusal naming only the kind of drift, never a fallback.
        raise ColdRuntimeError(
            "container runtime identity drifted from expected numeric uid:gid",
            error_class="target_identity",
            stage="target_identity",
        )
    if host_bind != expected_host_path:
        raise ColdRuntimeError(
            "container bind source drifted from expected host path",
            error_class="target_identity",
            stage="target_identity",
        )
    before = _observe_host_identity(host_bind, host_inspect)
    writable = inspect_container_writable(
        uid=observed_user.uid,
        gid=observed_user.gid,
        container_name=container_name,
        container_path=expected_container_path,
        docker_bin=docker_bin,
        runner=runner,
    )
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
        container_exec_uid=observed_user.uid,
        container_exec_gid=observed_user.gid,
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
        "container_exec_uid": observed.container_exec_uid,
        "container_exec_gid": observed.container_exec_gid,
    }
