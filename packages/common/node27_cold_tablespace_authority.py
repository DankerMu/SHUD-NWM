"""Durable private recovery authority for the cold-tablespace installer.

This authority is deliberately separate from the public receipt.  It contains
reconstructible secret-bearing Docker configuration and is readable only through
a bounded no-follow durable read.  Terminal removal proves pathname absence
through a parent-directory fsync before a caller may admit a new install.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    prove_named_entry_absent_durable,
    read_bytes_durable_no_follow,
    stat_no_follow,
    unlink_no_follow_durable,
)

MAX_AUTHORITY_BYTES = 512 * 1024
_AUTHORITY_SCHEMA_VERSION = "1.0"
_PHASES = frozenset(
    {
        "prepared",
        "path_created",
        "prior_stopped",
        "prior_renamed",
        "replacement_created",
        "ddl_created",
        "terminal_pending_cleanup",
    }
)
_REQUIRED_OWNERSHIP = frozenset(
    {
        "host_path_created",
        "prior_stopped",
        "prior_renamed",
        "installer_container_created",
        "catalog_created",
    }
)


class AuthorityError(RuntimeError):
    """The private recovery authority is unavailable, corrupt, or unsafe."""


def _canonical(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(document), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def private_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return the canonical private Docker-snapshot identity retained in authority."""

    return hashlib.sha256(_canonical(snapshot)).hexdigest()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorityError("recovery authority timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuthorityError("recovery authority timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorityError("recovery authority timestamp is invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _phase_ownership_is_consistent(phase: str, ownership: Mapping[str, Any]) -> bool:
    return (
        (phase != "path_created" or ownership["host_path_created"])
        and (phase != "prior_stopped" or ownership["prior_stopped"])
        and (phase not in {"prior_renamed", "replacement_created", "ddl_created"} or ownership["prior_renamed"])
        and (phase not in {"replacement_created", "ddl_created"} or ownership["installer_container_created"])
        and (phase != "ddl_created" or ownership["catalog_created"])
    )


def _validate(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise AuthorityError("recovery authority is malformed")
    value = dict(document)
    if value.get("schema_version") != _AUTHORITY_SCHEMA_VERSION:
        raise AuthorityError("recovery authority schema version differs")
    if value.get("phase") not in _PHASES:
        raise AuthorityError("recovery authority phase is invalid")
    head = value.get("head_sha")
    if head is not None and (not isinstance(head, str) or len(head) != 40):
        raise AuthorityError("recovery authority head identity is invalid")
    timestamps = [_timestamp(value.get(key)) for key in ("created_at", "updated_at")]
    if timestamps[1] < timestamps[0]:
        raise AuthorityError("recovery authority timestamps are not monotonic")
    prior = value.get("prior")
    expected = value.get("expected")
    path = value.get("path")
    ownership = value.get("ownership")
    if not all(isinstance(item, Mapping) for item in (prior, expected, path, ownership)):
        raise AuthorityError("recovery authority identity sections are missing")
    if not isinstance(value.get("prior_name"), str) or not value["prior_name"]:
        raise AuthorityError("recovery authority prior container identity is invalid")
    identity = value.get("identity")
    if not isinstance(identity, Mapping):
        raise AuthorityError("recovery authority identity contract is missing")
    required_identity = {
        "kind",
        "container_name",
        "prior_container_name",
        "host_path",
        "container_path",
        "tablespace",
        "docker_bin",
        "host_port",
        "work_root",
        "image_id",
        "image_ref",
    }
    if set(identity) != required_identity:
        raise AuthorityError("recovery authority identity contract is malformed")
    if identity.get("prior_container_name") != value["prior_name"]:
        raise AuthorityError("recovery authority prior name differs from identity contract")
    try:
        from packages.common.node27_cold_tablespace_identity import identity_from_public_payload

        identity_from_public_payload(dict(identity))
    except ValueError as error:
        raise AuthorityError("recovery authority identity contract is unsafe") from error
    if not isinstance(prior.get("container_id"), str) or not prior["container_id"]:
        raise AuthorityError("recovery authority prior container ID is invalid")
    private_snapshot = prior.get("private_snapshot")
    if not isinstance(private_snapshot, Mapping):
        raise AuthorityError("recovery authority private prior snapshot is missing")
    if private_snapshot_digest(private_snapshot) != prior.get("private_snapshot_digest"):
        raise AuthorityError("recovery authority private prior snapshot digest differs")
    if not isinstance(expected.get("cold_bind"), str) or not expected["cold_bind"]:
        raise AuthorityError("recovery authority cold bind identity is invalid")
    path_identity_is_valid = all(
        isinstance(path.get(key), int) and not isinstance(path[key], bool) and path[key] >= 0
        for key in ("uid", "gid", "mode")
    )
    if not path_identity_is_valid:
        raise AuthorityError("recovery authority path ownership identity is invalid")
    if not _REQUIRED_OWNERSHIP.issubset(ownership):
        raise AuthorityError("recovery authority ownership is incomplete")
    if not all(type(ownership[key]) is bool for key in _REQUIRED_OWNERSHIP):
        raise AuthorityError("recovery authority ownership is malformed")
    if not _phase_ownership_is_consistent(value["phase"], ownership):
        raise AuthorityError("recovery authority phase and ownership are inconsistent")
    if not isinstance(prior.get("config_digest"), str) or len(prior["config_digest"]) != 64:
        raise AuthorityError("recovery authority prior config digest is invalid")
    if not isinstance(expected.get("config_digest"), str) or len(expected["config_digest"]) != 64:
        raise AuthorityError("recovery authority replacement config digest is invalid")
    if not isinstance(path.get("device_identity"), str) or not path["device_identity"]:
        raise AuthorityError("recovery authority path identity is invalid")
    return value


def authority_exists(path: Path) -> bool:
    try:
        info = stat_no_follow(path)
    except FileNotFoundError:
        return False
    except SafeFilesystemError as error:
        raise AuthorityError("recovery authority is unavailable or unsafe") from error
    if not _is_private_regular_mode(info.st_mode):
        raise AuthorityError("recovery authority mode is not 0600")
    return True


def _is_private_regular_mode(mode: int) -> bool:
    return stat.S_ISREG(mode) and mode & 0o777 == 0o600


def write_authority(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    validated = _validate(document)
    try:
        atomic_write_bytes_no_follow(path, _canonical(validated), mode=0o600, require_durable_replace=True)
    except SafeFilesystemError as error:
        raise AuthorityError("recovery authority publication failed") from error
    return validated


def read_authority(path: Path) -> dict[str, Any]:
    try:
        info = stat_no_follow(path)
        if not _is_private_regular_mode(info.st_mode):
            raise AuthorityError("recovery authority mode is not 0600")
        raw = read_bytes_durable_no_follow(path, max_bytes=MAX_AUTHORITY_BYTES)
        document = json.loads(raw.decode("utf-8"))
    except AuthorityError:
        raise
    except (SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorityError("recovery authority is unavailable or malformed") from error
    return _validate(document)


def advance_authority(document: Mapping[str, Any], *, phase: str, **ownership_update: bool) -> dict[str, Any]:
    current = _validate(document)
    if phase not in _PHASES:
        raise AuthorityError("recovery authority phase is invalid")
    ownership = dict(current["ownership"])
    for key, value in ownership_update.items():
        if key not in _REQUIRED_OWNERSHIP or type(value) is not bool:
            raise AuthorityError("recovery authority ownership update is invalid")
        ownership[key] = value
    if not _phase_ownership_is_consistent(phase, ownership):
        raise AuthorityError("recovery authority phase and ownership are inconsistent")
    current["phase"] = phase
    current["ownership"] = ownership
    now = datetime.now(UTC)
    created = _timestamp(current["created_at"])
    current["updated_at"] = _iso(now if now >= created else created)
    return _validate(current)


def remove_authority(path: Path) -> None:
    """Durably unlink the authority and prove its no-follow pathname absent."""

    try:
        unlink_no_follow_durable(path, missing_ok=False)
        prove_named_entry_absent_durable(path)
    except FileNotFoundError as error:
        raise AuthorityError("recovery authority disappeared during terminal removal") from error
    except SafeFilesystemError as error:
        raise AuthorityError("recovery authority removal durability is unproven") from error
