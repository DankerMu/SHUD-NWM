"""Exact-image root capability for the isolated #1894 Docker oracle.

This synthetic-only boundary proves the image, its default PostgreSQL identity,
and the host observer runtime identity before any owned work root exists.  Image
default uid/gid is evidence only.  Container runtime and host PGDATA/cold owners
must equal the proven host euid/egid.  When noninteractive sudo is not available,
it additionally proves that the exact image can execute an isolated root helper.
It never mounts a live path, checkout, Docker socket, or port.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packages.common.node27_cold_tablespace_identity import (
    INTEGRATION_PREFIX,
    ColdTablespaceIdentity,
    validate_identity_for_action,
)

_ROOT_HELPER_PREFIX = f"{INTEGRATION_PREFIX}root-"
_ROOT_HELPER_PURPOSES = ("identity", "runtime-identity", "root-probe", "root-action")
_POSITIVE_IDENTITY = re.compile(r"^[1-9][0-9]*\n[1-9][0-9]*\n?$")
KNOWN_WORK_ROOT_CHILDREN = frozenset({"pgdata", "cold", "evidence", "receipts", "postgres.env"})


class RootCapabilityError(RuntimeError):
    """The exact image cannot prove a safe disposable root capability."""


@dataclass(frozen=True)
class RootEvidenceCapability:
    """Measured root authority and exact-image evidence without secrets."""

    strategy: Literal["sudo", "pinned_image"]
    image_postgres_uid: int
    image_postgres_gid: int
    runtime_uid: int
    runtime_gid: int
    image_id: str
    image_ref: str
    image_default_user: str
    root_proof: Literal["sudo-noninteractive", "pinned-image-user-0:0"]


def _require_synthetic(identity: ColdTablespaceIdentity) -> None:
    validate_identity_for_action(identity)
    if (
        identity.kind != "synthetic"
        or identity.work_root is None
        or identity.image_id is None
        or identity.image_ref is None
    ):
        raise RootCapabilityError("root capability requires an issued synthetic identity")


def _run(
    runner: Callable[..., Any], argv: tuple[str, ...], *, timeout: int, label: str
) -> Any:
    try:
        result = runner(argv, timeout=timeout)
    except Exception as error:  # noqa: BLE001 - Docker process boundary
        raise RootCapabilityError(f"{label} could not be executed") from error
    if result.returncode != 0:
        raise RootCapabilityError(f"{label} failed")
    return result


def root_helper_name(identity: ColdTablespaceIdentity, *, purpose: str) -> str:
    _require_synthetic(identity)
    token = identity.container_name.removeprefix(INTEGRATION_PREFIX)
    if purpose not in _ROOT_HELPER_PURPOSES or not token:
        raise RootCapabilityError("root helper identity is invalid")
    return f"{_ROOT_HELPER_PREFIX}{purpose}-{token}"


def _require_helper_absent(
    identity: ColdTablespaceIdentity,
    *,
    purpose: str,
    runner: Callable[..., Any],
) -> None:
    name = root_helper_name(identity, purpose=purpose)
    try:
        result = runner((identity.docker_bin, "inspect", name), timeout=20)
    except Exception as error:  # noqa: BLE001 - Docker process boundary
        raise RootCapabilityError("could not prove owned root helper absence") from error
    if result.returncode == 0:
        raise RootCapabilityError("owned root capability helper name already exists")
    if result.returncode != 1:
        raise RootCapabilityError("could not prove owned root helper absence")


def assert_root_helpers_absent(identity: ColdTablespaceIdentity, *, runner: Callable[..., Any]) -> None:
    """Require absence of every ownership-prefixed image helper before cleanup."""

    for purpose in _ROOT_HELPER_PURPOSES:
        _require_helper_absent(identity, purpose=purpose, runner=runner)


def _image_id(identity: ColdTablespaceIdentity, image: str, *, runner: Callable[..., Any]) -> str:
    result = _run(
        runner,
        (identity.docker_bin, "image", "inspect", image, "--format", "{{.Id}}"),
        timeout=30,
        label="exact pinned image authority probe",
    )
    return str(result.stdout).strip()


def _image_default_user(identity: ColdTablespaceIdentity, *, runner: Callable[..., Any]) -> str:
    assert identity.image_id is not None
    result = _run(
        runner,
        (identity.docker_bin, "image", "inspect", identity.image_id, "--format", "{{.Config.User}}"),
        timeout=30,
        label="exact pinned image default-user probe",
    )
    return str(result.stdout).strip()


def _parse_positive_identity(output: str, *, label: str) -> tuple[int, int]:
    if not _POSITIVE_IDENTITY.fullmatch(output):
        raise RootCapabilityError(f"{label} is not a strict positive numeric uid/gid result")
    uid, gid = output.strip().splitlines()
    return int(uid), int(gid)


def _capability_argv(
    identity: ColdTablespaceIdentity,
    *,
    purpose: str,
    user: str | None,
    script: str,
) -> tuple[str, ...]:
    assert identity.image_id is not None
    prefix: tuple[str, ...] = (
        identity.docker_bin,
        "run",
        "--rm",
        "--name",
        root_helper_name(identity, purpose=purpose),
        "--network",
        "none",
    )
    if user is not None:
        prefix += ("--user", user)
    return (
        *prefix,
        "--entrypoint",
        "/bin/sh",
        identity.image_id,
        "-ceu",
        script,
    )


def _run_identity_helper(
    identity: ColdTablespaceIdentity,
    *,
    purpose: str,
    user: str | None,
    script: str,
    runner: Callable[..., Any],
    label: str,
) -> str:
    _require_helper_absent(identity, purpose=purpose, runner=runner)
    argv = _capability_argv(identity, purpose=purpose, user=user, script=script)
    try:
        result = _run(runner, argv, timeout=30, label=label)
    except RootCapabilityError:
        _require_helper_absent(identity, purpose=purpose, runner=runner)
        raise
    _require_helper_absent(identity, purpose=purpose, runner=runner)
    return str(result.stdout)


def _measure_default_postgres_identity(
    identity: ColdTablespaceIdentity, *, runner: Callable[..., Any]
) -> tuple[int, int]:
    output = _run_identity_helper(
        identity,
        purpose="identity",
        user=None,
        script='test "$(id -un)" = postgres; id -u; id -g',
        runner=runner,
        label="pinned image postgres identity helper",
    )
    return _parse_positive_identity(output, label="pinned image postgres identity")


def _host_runtime_identity() -> tuple[int, int]:
    uid = os.geteuid()
    gid = os.getegid()
    if min(uid, gid) <= 0:
        raise RootCapabilityError("host observer runtime identity is not a positive unprivileged pair")
    return uid, gid


def _prove_runtime_identity(
    identity: ColdTablespaceIdentity,
    *,
    runtime_uid: int,
    runtime_gid: int,
    runner: Callable[..., Any],
) -> None:
    output = _run_identity_helper(
        identity,
        purpose="runtime-identity",
        user=f"{runtime_uid}:{runtime_gid}",
        script="id -u; id -g",
        runner=runner,
        label="pinned image runtime identity helper",
    )
    measured = _parse_positive_identity(output, label="pinned image runtime identity")
    if measured != (runtime_uid, runtime_gid):
        raise RootCapabilityError("pinned image runtime identity differs")


def _prove_image_root(identity: ColdTablespaceIdentity, *, runner: Callable[..., Any]) -> None:
    output = _run_identity_helper(
        identity,
        purpose="root-probe",
        user="0:0",
        script="id -u; id -g",
        runner=runner,
        label="pinned image root capability helper",
    )
    if output != "0\n0\n":
        raise RootCapabilityError("pinned image root identity differs")


def probe_root_evidence_capability(
    identity: ColdTablespaceIdentity,
    *,
    runner: Callable[..., Any],
) -> RootEvidenceCapability:
    """Measure the exact image first; use sudo when available, otherwise prove image root."""

    _require_synthetic(identity)
    assert identity.image_id is not None and identity.image_ref is not None
    if _image_id(identity, identity.image_id, runner=runner) != identity.image_id or _image_id(
        identity, identity.image_ref, runner=runner
    ) != identity.image_id:
        raise RootCapabilityError("exact pinned image authority is unavailable or ambiguous")
    default_user = _image_default_user(identity, runner=runner)
    if default_user != "postgres":
        raise RootCapabilityError("pinned image default postgres identity differs")
    runtime_uid, runtime_gid = _host_runtime_identity()
    assert_root_helpers_absent(identity, runner=runner)
    image_postgres_uid, image_postgres_gid = _measure_default_postgres_identity(identity, runner=runner)
    _prove_runtime_identity(
        identity,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        runner=runner,
    )
    try:
        sudo = runner(("/usr/bin/sudo", "-n", "true"), timeout=15)
    except Exception as error:  # noqa: BLE001 - sudo process boundary
        raise RootCapabilityError("sudo capability probe could not be executed") from error
    if sudo.returncode == 0:
        assert_root_helpers_absent(identity, runner=runner)
        return RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=image_postgres_uid,
            image_postgres_gid=image_postgres_gid,
            runtime_uid=runtime_uid,
            runtime_gid=runtime_gid,
            image_id=identity.image_id,
            image_ref=identity.image_ref,
            image_default_user=default_user,
            root_proof="sudo-noninteractive",
        )
    _prove_image_root(identity, runner=runner)
    assert_root_helpers_absent(identity, runner=runner)
    return RootEvidenceCapability(
        strategy="pinned_image",
        image_postgres_uid=image_postgres_uid,
        image_postgres_gid=image_postgres_gid,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        image_id=identity.image_id,
        image_ref=identity.image_ref,
        image_default_user=default_user,
        root_proof="pinned-image-user-0:0",
    )


def _root_action_script(action: str) -> str:
    commands = {
        "prepare": (
            'chown "$1:$2" /nhms-owned/pgdata && chmod 0700 /nhms-owned/pgdata && '
            'chown "0:$3" /nhms-owned/evidence && chmod 0750 /nhms-owned/evidence && '
            'chown "0:$3" /nhms-owned/evidence/mdadm.json /nhms-owned/evidence/smart-sdb1.json '
            '/nhms-owned/evidence/smart-sdc1.json /nhms-owned/evidence/backup.json && '
            'chmod 0640 /nhms-owned/evidence/mdadm.json /nhms-owned/evidence/smart-sdb1.json '
            '/nhms-owned/evidence/smart-sdc1.json /nhms-owned/evidence/backup.json'
        ),
        "create-cold-path": (
            '[ ! -e /nhms-owned/cold ] && [ ! -L /nhms-owned/cold ] && '
            'mkdir /nhms-owned/cold && chown "$1:$2" /nhms-owned/cold && chmod 0700 /nhms-owned/cold'
        ),
        "cleanup": (
            'for entry in /nhms-owned/* /nhms-owned/.[!.]* /nhms-owned/..?*; do '
            '[ -e "$entry" ] || [ -L "$entry" ] || continue; case "${entry##*/}" in '
            'pgdata|cold|evidence|receipts|postgres.env) ;; *) exit 64 ;; esac; done; '
            'rm -rf /nhms-owned/pgdata /nhms-owned/cold /nhms-owned/evidence /nhms-owned/receipts '
            '/nhms-owned/postgres.env'
        ),
    }
    try:
        return commands[action]
    except KeyError as error:
        raise RootCapabilityError("pinned root helper action is invalid") from error


def _validate_owned_root_bind(work_root: Path) -> None:
    try:
        info = work_root.stat(follow_symlinks=False)
    except OSError as error:
        raise RootCapabilityError("pinned root helper owned work root is unavailable") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RootCapabilityError("pinned root helper owned work root is invalid")


def pinned_image_root_argv(
    identity: ColdTablespaceIdentity,
    *,
    capability: RootEvidenceCapability,
    work_root: Path,
    reader_gid: int,
    action: str,
) -> tuple[str, ...]:
    """Build the fixed root action with exactly one owned-root bind."""

    _require_synthetic(identity)
    _validate_owned_root_bind(work_root)
    if (
        capability.strategy != "pinned_image"
        or capability.image_id != identity.image_id
        or capability.image_ref != identity.image_ref
        or capability.image_default_user != "postgres"
        or capability.root_proof != "pinned-image-user-0:0"
        or min(
            capability.image_postgres_uid,
            capability.image_postgres_gid,
            capability.runtime_uid,
            capability.runtime_gid,
            reader_gid,
        )
        <= 0
        or identity.work_root != work_root
    ):
        raise RootCapabilityError("pinned root helper lacks the measured exact-image capability")
    assert identity.image_id is not None
    return (
        identity.docker_bin,
        "run",
        "--rm",
        "--name",
        root_helper_name(identity, purpose="root-action"),
        "--network",
        "none",
        "--user",
        "0:0",
        "-v",
        f"{work_root}:/nhms-owned:rw",
        "--entrypoint",
        "/bin/sh",
        identity.image_id,
        "-ceu",
        _root_action_script(action),
        "nhms-root-action",
        str(capability.runtime_uid),
        str(capability.runtime_gid),
        str(reader_gid),
    )
