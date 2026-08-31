"""Immutable identities for the node-27 cold-tablespace state machine.

Production never accepts an identity override.  The only alternate identity is
an intentionally narrow disposable contract constructed by
:func:`make_disposable_identity` for the isolated Docker oracle.  Keeping the
identity as data rather than scattered constants makes every mutation target
reviewable at the one public state-machine seam.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packages.common.compressed_chunk_cold_residency import (
    LIVE_CONTAINER_NAME,
    LIVE_PATH_PREFIXES,
    LIVE_PORT,
    PINNED_IMAGE_ID,
    PINNED_IMAGE_REF,
)

PRODUCTION_CONTAINER = "nhms-db"
PRODUCTION_PRIOR_CONTAINER = "nhms-db-before"
PRODUCTION_HOST_PATH = Path("/data/GHDC/nhms-cold-tablespace")
CONTAINER_COLD_PATH = "/home/postgres/pgdata/tablespaces/nhms_cold"
COLD_TABLESPACE = "nhms_cold"
TRUSTED_DOCKER_BIN = "/usr/bin/docker"
INTEGRATION_PREFIX = "nhms-1894-tablespace-"
_NAME_RE = re.compile(r"^nhms-1894-tablespace-[0-9a-f]{8,32}$")
_ISSUED_KINDS = frozenset({"production", "synthetic"})


class IdentityContractError(ValueError):
    """An installer identity is not the fixed production or owned test contract."""


@dataclass(frozen=True, init=False)
class ColdTablespaceIdentity:
    """All identity-bearing installer inputs, issued only by this module."""

    kind: Literal["production", "synthetic"]
    container_name: str
    prior_container_name: str
    host_path: Path
    container_path: str
    tablespace: str
    docker_bin: str
    host_port: int
    work_root: Path | None
    image_id: str | None
    image_ref: str | None

    @property
    def cold_bind(self) -> str:
        return f"{self.host_path}:{self.container_path}:rw"

    def public_payload(self) -> dict[str, object]:
        """Return non-secret identity evidence suitable for public receipts."""

        return {
            "kind": self.kind,
            "container_name": self.container_name,
            "prior_container_name": self.prior_container_name,
            "host_path": str(self.host_path),
            "container_path": self.container_path,
            "tablespace": self.tablespace,
            "docker_bin": self.docker_bin,
            "host_port": self.host_port,
            "work_root": None if self.work_root is None else str(self.work_root),
            "image_id": self.image_id,
            "image_ref": self.image_ref,
        }


def _issue(
    *,
    kind: Literal["production", "synthetic"],
    container_name: str,
    prior_container_name: str,
    host_path: Path,
    container_path: str,
    tablespace: str,
    docker_bin: str,
    host_port: int,
    work_root: Path | None,
    image_id: str | None,
    image_ref: str | None,
) -> ColdTablespaceIdentity:
    identity = object.__new__(ColdTablespaceIdentity)
    object.__setattr__(identity, "kind", kind)
    object.__setattr__(identity, "container_name", container_name)
    object.__setattr__(identity, "prior_container_name", prior_container_name)
    object.__setattr__(identity, "host_path", host_path)
    object.__setattr__(identity, "container_path", container_path)
    object.__setattr__(identity, "tablespace", tablespace)
    object.__setattr__(identity, "docker_bin", docker_bin)
    object.__setattr__(identity, "host_port", host_port)
    object.__setattr__(identity, "work_root", work_root)
    object.__setattr__(identity, "image_id", image_id)
    object.__setattr__(identity, "image_ref", image_ref)
    return identity


PRODUCTION_IDENTITY = _issue(
    kind="production",
    container_name=PRODUCTION_CONTAINER,
    prior_container_name=PRODUCTION_PRIOR_CONTAINER,
    host_path=PRODUCTION_HOST_PATH,
    container_path=CONTAINER_COLD_PATH,
    tablespace=COLD_TABLESPACE,
    docker_bin=TRUSTED_DOCKER_BIN,
    host_port=LIVE_PORT,
    work_root=None,
    image_id=None,
    image_ref=None,
)


def _absolute_normalized(path: Path, *, label: str) -> Path:
    expanded = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(expanded):
        raise IdentityContractError(f"{label} must be absolute")
    return Path(os.path.normpath(expanded))


def _is_under(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(path), os.fspath(parent))) == os.fspath(parent)
    except ValueError:
        return False


def _forbidden_host_prefixes() -> tuple[Path, ...]:
    checkout = Path(__file__).resolve().parents[2]
    values = {
        "/data/GHDC",
        "/home/nwm/NWM",
        "/home/nwm/nhms-pgdata",
        "/home/ghdc/nwm",
        os.fspath(checkout),
        *(prefix for prefix in LIVE_PATH_PREFIXES if prefix.startswith("/")),
    }
    # ``CONTAINER_COLD_PATH`` is a container pathname, not a permitted synthetic
    # host root.  It nevertheless remains in the generic #1892 live prefixes,
    # so excluding it here would only weaken the test contract.
    return tuple(sorted((_absolute_normalized(Path(value), label="forbidden path") for value in values), key=str))


def _reject_forbidden_host_path(path: Path, *, label: str) -> None:
    for prefix in _forbidden_host_prefixes():
        if path == prefix or _is_under(path, prefix):
            raise IdentityContractError(f"{label} overlaps a live or active-checkout path")
    if any(part.startswith("nhms-1892") for part in path.parts):
        raise IdentityContractError(f"{label} uses a #1892 ownership prefix")


def _validate_synthetic(identity: ColdTablespaceIdentity) -> None:
    if identity.kind != "synthetic":
        raise IdentityContractError("synthetic identity kind is invalid")
    if not _NAME_RE.fullmatch(identity.container_name):
        raise IdentityContractError("disposable container name lacks the unique #1894 ownership prefix")
    if identity.prior_container_name != f"{identity.container_name}-before" or not _NAME_RE.fullmatch(
        identity.prior_container_name.removesuffix("-before")
    ):
        raise IdentityContractError("disposable prior container name is not the owned companion identity")
    if identity.container_name in {LIVE_CONTAINER_NAME, PRODUCTION_CONTAINER} or identity.prior_container_name in {
        PRODUCTION_PRIOR_CONTAINER,
        LIVE_CONTAINER_NAME,
    }:
        raise IdentityContractError("disposable identity names a live container")
    if identity.container_name.startswith("nhms-1892") or identity.prior_container_name.startswith("nhms-1892"):
        raise IdentityContractError("disposable identity uses a #1892 ownership prefix")
    if (
        not isinstance(identity.host_port, int)
        or isinstance(identity.host_port, bool)
        or not 1024 <= identity.host_port <= 65535
    ):
        raise IdentityContractError("disposable PostgreSQL port is invalid")
    if identity.host_port == LIVE_PORT:
        raise IdentityContractError("disposable identity names the live PostgreSQL port")
    if identity.work_root is None:
        raise IdentityContractError("disposable work root is required")
    work_root = _absolute_normalized(identity.work_root, label="disposable work root")
    host_path = _absolute_normalized(identity.host_path, label="disposable host cold path")
    token = identity.container_name.removeprefix(INTEGRATION_PREFIX)
    if work_root.name != f"{INTEGRATION_PREFIX}{token}":
        raise IdentityContractError("disposable work root does not match the unique container ownership token")
    _reject_forbidden_host_path(work_root, label="disposable work root")
    _reject_forbidden_host_path(host_path, label="disposable host cold path")
    if host_path == work_root or not _is_under(host_path, work_root):
        raise IdentityContractError("disposable host cold path must be under its owned work root")
    if identity.container_path != CONTAINER_COLD_PATH:
        raise IdentityContractError("container cold path must remain the fixed contract path")
    if identity.tablespace != COLD_TABLESPACE:
        raise IdentityContractError("tablespace name must remain the fixed contract name")
    if identity.docker_bin != TRUSTED_DOCKER_BIN:
        raise IdentityContractError("Docker binary must be the trusted absolute path")
    if identity.image_id != PINNED_IMAGE_ID or identity.image_ref != PINNED_IMAGE_REF:
        raise IdentityContractError("disposable identity must use the exact #1892 pinned image authority")


def validate_identity_for_action(identity: ColdTablespaceIdentity) -> ColdTablespaceIdentity:
    """Reject an unissued or unsafe identity before external work begins."""

    if not isinstance(identity, ColdTablespaceIdentity) or identity.kind not in _ISSUED_KINDS:
        raise IdentityContractError("installer identity was not issued by the contract factory")
    if identity.kind == "production":
        if identity != PRODUCTION_IDENTITY:
            raise IdentityContractError("production installer identity differs from the fixed contract")
    else:
        _validate_synthetic(identity)
    return identity


def identity_from_public_payload(payload: object) -> ColdTablespaceIdentity:
    """Parse a serialized authority/receipt identity through the same factory."""

    if not isinstance(payload, dict):
        raise IdentityContractError("serialized identity must be an object")
    kind = payload.get("kind")
    if kind == "production":
        if payload != PRODUCTION_IDENTITY.public_payload():
            raise IdentityContractError("serialized production identity differs from the fixed contract")
        return PRODUCTION_IDENTITY
    if kind != "synthetic":
        raise IdentityContractError("serialized identity kind is invalid")
    work_root = payload.get("work_root")
    host_path = payload.get("host_path")
    container_name = payload.get("container_name")
    prior_container_name = payload.get("prior_container_name")
    host_port = payload.get("host_port")
    image_id = payload.get("image_id")
    image_ref = payload.get("image_ref")
    if (
        not all(
            isinstance(value, str)
            for value in (work_root, host_path, container_name, prior_container_name, image_id, image_ref)
        )
        or not isinstance(host_port, int)
        or isinstance(host_port, bool)
    ):
        raise IdentityContractError("serialized synthetic identity is malformed")
    identity = make_disposable_identity(
        container_name=container_name,
        prior_container_name=prior_container_name,
        host_port=host_port,
        work_root=Path(work_root),
        host_path=Path(host_path),
        image_id=image_id,
        image_ref=image_ref,
    )
    if payload != identity.public_payload():
        raise IdentityContractError("serialized synthetic identity differs from the fixed disposable contract")
    return identity


def assert_disposable_absent(
    identity: ColdTablespaceIdentity,
    *,
    path_exists: Callable[[Path], bool],
    container_exists: Callable[[str], bool],
    port_is_available: Callable[[int], bool],
) -> None:
    """Prove an issued synthetic identity owns currently absent action targets."""

    validate_identity_for_action(identity)
    if identity.kind != "synthetic" or identity.work_root is None:
        raise IdentityContractError("absence proof is only valid for a synthetic identity")
    if path_exists(identity.work_root) or path_exists(identity.host_path):
        raise IdentityContractError("disposable work root or host cold path already exists")
    if container_exists(identity.container_name) or container_exists(identity.prior_container_name):
        raise IdentityContractError("disposable Docker name already exists")
    if not port_is_available(identity.host_port):
        raise IdentityContractError("disposable PostgreSQL port is already bound")


def make_disposable_identity(
    *,
    container_name: str,
    prior_container_name: str,
    host_port: int,
    work_root: Path,
    host_path: Path,
    image_id: str,
    image_ref: str,
) -> ColdTablespaceIdentity:
    """Issue the sole synthetic identity accepted by imported test/oracle APIs.

    This performs only lexical validation.  A caller must still prove that the
    owned root and Docker names are absent before it creates them; that proof is
    intentionally part of the integration boundary, not an ambient CLI option.
    """

    identity = _issue(
        kind="synthetic",
        container_name=container_name,
        prior_container_name=prior_container_name,
        host_path=_absolute_normalized(host_path, label="disposable host cold path"),
        container_path=CONTAINER_COLD_PATH,
        tablespace=COLD_TABLESPACE,
        docker_bin=TRUSTED_DOCKER_BIN,
        host_port=host_port,
        work_root=_absolute_normalized(work_root, label="disposable work root"),
        image_id=image_id,
        image_ref=image_ref,
    )
    return validate_identity_for_action(identity)
