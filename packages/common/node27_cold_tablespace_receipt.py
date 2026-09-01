"""Schema-valid public receipt construction and durable publication."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.node27_cold_tablespace_container import ContainerSnapshot
from packages.common.node27_cold_tablespace_identity import (
    PRODUCTION_IDENTITY,
    ColdTablespaceIdentity,
    validate_identity_for_action,
)
from packages.common.node27_cold_tablespace_types import InstallConfig, InstallDependencies, InstallResult
from packages.common.redaction import redact_payload
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow

_FORMAT_CHECKER = jsonschema.FormatChecker()


def now_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical(payload: Mapping[str, Any]) -> bytes:
    safe = redact_payload(dict(payload))
    return (json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def empty_path(identity: ColdTablespaceIdentity) -> dict[str, Any]:
    return {
        "host_path": str(identity.host_path),
        "container_path": identity.container_path,
        "exists": False,
        "device_identity": None,
        "owner_uid": None,
        "owner_gid": None,
        "mode": None,
        "empty": None,
    }


def receipt_template(config: InstallConfig, *, outcome: str, state: str) -> dict[str, Any]:
    identity = validate_identity_for_action(config.identity)
    return {
        "schema_version": "1.0",
        "generated_at": "2026-08-31T12:00:00Z",
        "mode": "enforce" if config.enforce else "dry-run",
        "outcome": outcome,
        "state": state,
        "head_sha": config.head_sha,
        "tablespace": identity.tablespace,
        "container": identity.container_name,
        "identity": identity.public_payload(),
        "path": empty_path(identity),
        "container_snapshot": {"config_digest": None, "environment_names": [], "resolved_image_id": None},
        "evidence": {"health": {}, "backup": {}, "capacity": {}},
        "readback": {
            "approved": False,
            "catalog_location": None,
            "pg_tblspc_target": None,
            "container_bind": None,
            "device_identity": None,
            "container_writable": None,
            "hypertable_attached": False,
            "new_chunk_tablespace": None,
        },
        "ownership": {
            "prior_container": None,
            "installer_container_created": False,
            "catalog_created": False,
            "host_path_created": False,
            "recovery_authority": False,
        },
        "authority": {"state": "closed", "phase": None, "path_present": False},
        "rollback": {"attempted": False, "prior_restored": False, "host_path_removed": False, "blockers": []},
        "blockers": [],
        "error": None,
    }


def render(receipt: Mapping[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    """Redact and schema-validate a receipt before any publication attempt."""

    rendered = json.loads(canonical(receipt).decode("utf-8"))
    jsonschema.validate(rendered, schema, format_checker=_FORMAT_CHECKER)
    return rendered


def publish(path: Path, receipt: Mapping[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    rendered = render(receipt, schema)
    try:
        atomic_write_bytes_no_follow(path, canonical(rendered), mode=0o600, require_durable_replace=True)
    except SafeFilesystemError as error:
        raise RuntimeError("installer receipt publication failed") from error
    return rendered


def publish_with_dependencies(
    path: Path,
    receipt: Mapping[str, Any],
    schema: Mapping[str, Any],
    deps: InstallDependencies,
) -> dict[str, Any]:
    """Run a test-only pre-write fault hook, then use the fixed publisher."""

    if deps.before_receipt_publish is not None:
        deps.before_receipt_publish(path, receipt)
    return publish(path, receipt, schema)


def no_go(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    now: datetime,
    receipt: dict[str, Any],
    blockers: Sequence[str],
    state: str = "blocked",
    deps: InstallDependencies | None = None,
) -> InstallResult:
    receipt.update(
        {
            "generated_at": now_iso(now),
            "outcome": "no_go",
            "state": state,
            "blockers": list(dict.fromkeys((*receipt["blockers"], *blockers))),
        }
    )
    authority = receipt.get("authority")
    if isinstance(authority, Mapping) and authority.get("state") in {"sidecar", "pending_cleanup"}:
        receipt["ownership"] = {**receipt["ownership"], "recovery_authority": True}
    publisher = (
        publish
        if deps is None
        else lambda path, document, active_schema: publish_with_dependencies(path, document, active_schema, deps)
    )
    return InstallResult(
        outcome="no_go",
        receipt=publisher(config.receipt_path, receipt, schema),
        schema=dict(schema),
    )


def path_payload(identity: ColdTablespaceIdentity, observed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "host_path": str(identity.host_path),
        "container_path": identity.container_path,
        "exists": bool(observed.get("exists")),
        "device_identity": optional_string(observed.get("device_identity")),
        "owner_uid": optional_int(observed.get("uid")),
        "owner_gid": optional_int(observed.get("gid")),
        "mode": optional_int(observed.get("mode")),
        "empty": None if observed.get("entry_count") is None else observed.get("entry_count") == 0,
    }


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def container_snapshot_payload(snapshot: ContainerSnapshot) -> dict[str, Any]:
    """Render the public secret-free container identity for a receipt.

    Only the reconstructible config digest and the sorted environment variable
    names are published.  Environment values, image, labels, and the Docker
    instance ID stay in the private recovery authority.
    """

    public = snapshot.public_payload()
    return {
        "config_digest": snapshot.config_digest,
        "environment_names": public["environment_names"],
        "resolved_image_id": snapshot.resolved_image_id,
    }


def set_authority_receipt(receipt: dict[str, Any], authority: Mapping[str, Any], *, state: str = "sidecar") -> None:
    receipt["authority"] = {"state": state, "phase": authority.get("phase"), "path_present": True}
    receipt["ownership"] = {
        "prior_container": authority.get("prior_name"),
        "installer_container_created": bool(authority["ownership"]["installer_container_created"]),
        "catalog_created": bool(authority["ownership"]["catalog_created"]),
        "host_path_created": bool(authority["ownership"]["host_path_created"]),
        "recovery_authority": True,
    }


def example_receipt(*, outcome: str, head_sha: str) -> dict[str, Any]:
    state = {
        "dry_run": "preflight",
        "installed": "installed",
        "already_ready": "ready",
        "no_go": "blocked",
        "in_progress": "in_progress",
        "rollback": "rollback",
        "pending_cleanup": "pending_cleanup",
        "error": "error",
    }[outcome]
    config = InstallConfig(
        enforce=outcome in {"installed", "in_progress", "rollback", "pending_cleanup", "error"},
        receipt_path=Path("/tmp/receipt.json"),
        recovery_path=Path("/tmp/recovery.json"),
        head_sha=head_sha,
        expected_uid=999,
        expected_gid=999,
        expected_mode=0o700,
        expected_device_identity="example-device",
        install_required_bytes=0,
        rollback_headroom_bytes=0,
    )
    receipt = receipt_template(config, outcome=outcome, state=state)
    if outcome in {"installed", "already_ready"}:
        receipt["container_snapshot"]["resolved_image_id"] = (
            "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e"
        )
        receipt["readback"] = {
            "approved": True,
            "catalog_location": PRODUCTION_IDENTITY.container_path,
            "pg_tblspc_target": PRODUCTION_IDENTITY.container_path,
            "container_bind": str(PRODUCTION_IDENTITY.host_path),
            "device_identity": "example-device",
            "container_writable": True,
            "hypertable_attached": False,
            "new_chunk_tablespace": "pg_default",
        }
    if outcome == "installed":
        receipt["ownership"] = {
            "prior_container": PRODUCTION_IDENTITY.prior_container_name,
            "installer_container_created": True,
            "catalog_created": True,
            "host_path_created": False,
            "recovery_authority": False,
        }
    elif outcome == "in_progress":
        receipt["ownership"]["recovery_authority"] = True
        receipt["authority"] = {"state": "sidecar", "phase": "prepared", "path_present": True}
    elif outcome == "rollback":
        receipt["rollback"]["attempted"] = True
    elif outcome == "pending_cleanup":
        receipt["ownership"]["recovery_authority"] = True
        receipt["authority"] = {"state": "pending_cleanup", "phase": "terminal_pending_cleanup", "path_present": True}
    elif outcome == "no_go":
        receipt["blockers"] = ["example blocker"]
    elif outcome == "error":
        receipt["error"] = {"class": "example", "stage": "example", "reason": "example error"}
    return receipt
