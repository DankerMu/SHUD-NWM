"""Named-container observation and pending-action adoption for recovery.

Absence is inventory-proven only when the optional inspect seam returns
``None``.  Exception text is never treated as a missing container.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common.node27_cold_tablespace_authority import (
    AuthorityError,
    advance_authority,
    private_snapshot_digest,
    read_authority,
    remove_authority,
    write_authority,
)
from packages.common.node27_cold_tablespace_container import ContainerSnapshot, normalize_raw_inspect
from packages.common.node27_cold_tablespace_identity import ColdTablespaceIdentity
from packages.common.node27_cold_tablespace_pending import classify_pending
from packages.common.node27_cold_tablespace_receipt import (
    publish_with_dependencies,
    render,
    set_authority_receipt,
)
from packages.common.node27_cold_tablespace_topology import has_cold_bind
from packages.common.node27_cold_tablespace_types import (
    InstallConfig,
    InstallDependencies,
    InstallResult,
)


class NamedObservationError(RuntimeError):
    """Named-container observation failed; absence is unproven."""


def authority_payload(
    *, config: InstallConfig, now_iso: str, snapshot: ContainerSnapshot, path_observed: Mapping[str, Any]
) -> dict[str, Any]:
    identity = config.identity
    return {
        "schema_version": "1.0",
        "phase": "prepared",
        "created_at": now_iso,
        "updated_at": now_iso,
        "head_sha": config.head_sha,
        "identity": identity.public_payload(),
        "prior_name": identity.prior_container_name,
        "prior": {
            "container_id": snapshot.container_id,
            "config_digest": snapshot.config_digest,
            "private_snapshot": snapshot.private_payload(),
            "private_snapshot_digest": private_snapshot_digest(snapshot.private_payload()),
        },
        "expected": {
            "cold_bind": identity.cold_bind,
            "config_digest": snapshot.with_cold_bind(identity=identity).config_digest,
            "resolved_image_id": snapshot.resolved_image_id,
        },
        "path": {
            "device_identity": path_observed.get("device_identity"),
            "uid": config.expected_uid,
            "gid": config.expected_gid,
            "mode": config.expected_mode,
        },
        "ownership": {
            "host_path_created": False,
            "prior_stopped": False,
            "prior_renamed": False,
            "installer_container_created": False,
            "catalog_created": False,
        },
        "pending_action": None,
    }


def write_recovery(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return write_authority(path, document)
    except AuthorityError as error:
        raise RuntimeError("private recovery authority publication failed") from error


def _read_recovery(path: Path) -> dict[str, Any]:
    try:
        return read_authority(path)
    except AuthorityError as error:
        raise RuntimeError("private recovery authority is unavailable") from error


def remove_recovery(path: Path, deps: InstallDependencies) -> None:
    try:
        if deps.remove_recovery is None:
            remove_authority(path)
        else:
            deps.remove_recovery(path)
    except AuthorityError as error:
        raise RuntimeError("private recovery authority removal failed") from error


def phase_hook(deps: InstallDependencies, phase: str) -> None:
    if deps.after_phase is not None:
        deps.after_phase(phase)


def authority_matches_identity(authority: Mapping[str, Any], identity: ColdTablespaceIdentity) -> bool:
    return (
        authority.get("identity") == identity.public_payload()
        and authority.get("prior_name") == identity.prior_container_name
    )


def _private_config(authority: Mapping[str, Any]) -> dict[str, Any] | None:
    prior = authority.get("prior") if isinstance(authority.get("prior"), Mapping) else {}
    private = prior.get("private_snapshot")
    if not isinstance(private, Mapping) or prior.get("private_snapshot_digest") != private_snapshot_digest(private):
        return None
    expected = dict(private)
    expected.pop("container_id", None)
    expected.pop("name", None)
    return expected


def prior_config_matches(
    authority: Mapping[str, Any], snapshot: ContainerSnapshot, identity: ColdTablespaceIdentity
) -> bool:
    prior = authority.get("prior") if isinstance(authority.get("prior"), Mapping) else {}
    expected = _private_config(authority)
    return bool(
        expected is not None
        and prior.get("config_digest") == snapshot.config_digest
        and expected == snapshot.config_payload()
        and snapshot.name == identity.container_name
        and not has_cold_bind(snapshot, identity)
    )


def expected_snapshot_matches(
    authority: Mapping[str, Any], snapshot: ContainerSnapshot, identity: ColdTablespaceIdentity
) -> bool:
    expected = authority.get("expected") if isinstance(authority.get("expected"), Mapping) else {}
    return (
        expected.get("config_digest") == snapshot.config_digest
        and expected.get("cold_bind") == identity.cold_bind
        and expected.get("resolved_image_id") == snapshot.resolved_image_id
    )


def inspect_named(deps: InstallDependencies, name: str, identity: ColdTablespaceIdentity) -> ContainerSnapshot:
    """Freshly inspect an explicit name; never alias renamed prior to current."""

    if deps.inspect_named_container is not None:
        raw = deps.inspect_named_container(name)
        if raw is None:
            raise RuntimeError("named container inspection returned absence instead of an object")
        return normalize_raw_inspect(dict(raw))
    if name == identity.container_name:
        return normalize_raw_inspect(dict(deps.inspect_container()))
    raise RuntimeError("named container inspection boundary is unavailable")


def inspect_named_optional(
    deps: InstallDependencies, name: str, identity: ColdTablespaceIdentity
) -> ContainerSnapshot | None:
    """Return a snapshot, proven absence (None), or raise if observation is unavailable."""

    if deps.inspect_named_container_optional is None:
        raise NamedObservationError(
            f"named container optional inspection boundary is unavailable for {name}"
            f" ({identity.container_name})"
        )
    try:
        raw = deps.inspect_named_container_optional(name)
    except NamedObservationError:
        raise
    except Exception as error:
        raise NamedObservationError(str(error)) from error
    if raw is None:
        return None
    try:
        return normalize_raw_inspect(dict(raw))
    except Exception as error:
        raise NamedObservationError(str(error)) from error


def _record_rollback_progress(
    config: InstallConfig,
    authority: Mapping[str, Any],
    **ownership_update: bool,
) -> dict[str, Any]:
    """Durably record each completed rollback mutation before the next one.

    ``terminal_pending_cleanup`` is the only phase that admits every truthful
    subset of remaining installer-owned resources.  A later retry therefore
    resumes only what remains instead of treating an already-removed path or
    replacement as evidence of an external race.
    """

    return write_recovery(
        config.recovery_path,
        advance_authority(
            authority, phase="terminal_pending_cleanup", pending_action=None, **ownership_update
        ),
    )


def _arm_action(config: InstallConfig, authority: Mapping[str, Any], action: str) -> dict[str, Any]:
    phase = str(authority["phase"])
    if action in {"drop_catalog", "remove_replacement", "rename_prior_back", "start_prior", "remove_host_path"}:
        phase = "terminal_pending_cleanup"
    return write_recovery(
        config.recovery_path,
        advance_authority(authority, phase=phase, pending_action=action),
    )


def _path_observation(deps: InstallDependencies) -> Mapping[str, Any] | None:
    try:
        return dict(deps.inspect_host_path_for_rollback())
    except Exception:
        try:
            return dict(deps.inspect_path())
        except Exception:
            return None


def _path_identity_matches(authority: Mapping[str, Any], path: Mapping[str, Any] | None) -> bool:
    expected = authority.get("path") if isinstance(authority.get("path"), Mapping) else {}
    if path is None:
        return False
    return bool(
        path.get("device_identity") == expected.get("device_identity")
        and path.get("uid") == expected.get("uid")
        and path.get("gid") == expected.get("gid")
        and path.get("mode") == expected.get("mode")
    )


def _classify_authority_pending(
    *,
    config: InstallConfig,
    deps: InstallDependencies,
    authority: Mapping[str, Any],
    topology: str | None,
) -> str:
    identity = config.identity
    current = inspect_named_optional(deps, identity.container_name, identity)
    prior = inspect_named_optional(deps, identity.prior_container_name, identity)
    path = _path_observation(deps)
    return classify_pending(
        str(authority.get("pending_action")),
        current=current,
        prior=prior,
        topology=topology,
        path_exists=None if path is None else (path.get("exists") if isinstance(path.get("exists"), bool) else None),
        path_matches=_path_identity_matches(authority, path),
        path_empty=bool(path) and path.get("entry_count") == 0,
        identity_container=identity.container_name,
        identity_prior=identity.prior_container_name,
        prior_matches_current=bool(current) and prior_config_matches(authority, current, identity),
        prior_matches_prior=bool(prior) and _named_prior_matches(authority, prior, identity),
        expected_matches_current=bool(current) and expected_snapshot_matches(authority, current, identity),
        has_cold_bind_current=bool(current) and has_cold_bind(current, identity),
    )


def _adopt_pending_post(config: InstallConfig, authority: Mapping[str, Any], action: str) -> dict[str, Any]:
    updates: dict[str, bool] = {}
    phase = str(authority["phase"])
    if action == "create_host_path":
        phase, updates = "path_created", {"host_path_created": True}
    elif action == "stop_prior":
        phase, updates = "prior_stopped", {"prior_stopped": True}
    elif action == "rename_prior":
        phase, updates = "prior_renamed", {"prior_renamed": True}
    elif action == "create_replacement":
        phase, updates = "replacement_created", {"installer_container_created": True}
    elif action == "create_catalog":
        phase, updates = "ddl_created", {"catalog_created": True}
    elif action == "drop_catalog":
        phase, updates = "terminal_pending_cleanup", {"catalog_created": False}
    elif action == "remove_replacement":
        phase, updates = "terminal_pending_cleanup", {"installer_container_created": False}
    elif action == "rename_prior_back":
        phase, updates = "terminal_pending_cleanup", {"prior_renamed": False}
    elif action == "start_prior":
        phase, updates = "terminal_pending_cleanup", {"prior_stopped": False}
    elif action == "remove_host_path":
        phase, updates = "terminal_pending_cleanup", {"host_path_created": False}
    return write_recovery(
        config.recovery_path,
        advance_authority(authority, phase=phase, pending_action=None, **updates),
    )


def terminal_close(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    outcome: str,
    state: str,
) -> InstallResult:
    """Publish terminal-sidecar evidence, then prove durable unlink, then close.

    The first receipt cannot lie about authority absence.  If unlink or final
    closed publication fails, a schema-valid pending-cleanup receipt retains the
    sidecar authority for a future no-mutation closure pass.
    """

    pending = write_recovery(config.recovery_path, advance_authority(authority, phase="terminal_pending_cleanup"))
    set_authority_receipt(receipt, pending)
    receipt.update({"outcome": outcome, "state": state})
    try:
        first = publish_with_dependencies(config.receipt_path, receipt, schema, deps)
    except Exception:
        # The durable authority is already terminal-pending.  Return a
        # schema-valid in-memory pending receipt instead of trying rollback or
        # claiming closure when the first terminal publication is unavailable.
        pending_receipt = dict(render(receipt, schema))
        pending_receipt.update({"outcome": "pending_cleanup", "state": "pending_cleanup"})
        pending_receipt["authority"] = {
            "state": "pending_cleanup",
            "phase": "terminal_pending_cleanup",
            "path_present": True,
        }
        pending_receipt["ownership"] = {**pending_receipt["ownership"], "recovery_authority": True}
        return InstallResult("pending_cleanup", pending_receipt, dict(schema))
    try:
        remove_recovery(config.recovery_path, deps)
    except Exception:
        pending_receipt = dict(first)
        pending_receipt.update({"outcome": "pending_cleanup", "state": "pending_cleanup"})
        pending_receipt["authority"] = {
            "state": "pending_cleanup",
            "phase": "terminal_pending_cleanup",
            "path_present": True,
        }
        try:
            published = publish_with_dependencies(config.receipt_path, pending_receipt, schema, deps)
        except Exception:
            published = first
        return InstallResult("pending_cleanup", published, dict(schema))
    closed = dict(first)
    closed["authority"] = {"state": "closed", "phase": None, "path_present": False}
    closed["ownership"] = {**closed["ownership"], "recovery_authority": False}
    try:
        published = publish_with_dependencies(config.receipt_path, closed, schema, deps)
    except Exception:
        # A final receipt failure must not leave a prior terminal receipt claiming
        # closure.  Restore the private authority first, then publish pending.
        write_recovery(config.recovery_path, pending)
        pending_receipt = dict(first)
        pending_receipt.update({"outcome": "pending_cleanup", "state": "pending_cleanup"})
        pending_receipt["authority"] = {
            "state": "pending_cleanup",
            "phase": "terminal_pending_cleanup",
            "path_present": True,
        }
        try:
            published = publish_with_dependencies(config.receipt_path, pending_receipt, schema, deps)
        except Exception:
            published = first
        return InstallResult("pending_cleanup", published, dict(schema))
    return InstallResult(outcome, published, dict(schema))


def _named_prior_matches(
    authority: Mapping[str, Any], prior: ContainerSnapshot, identity: ColdTablespaceIdentity
) -> bool:
    expected = _private_config(authority)
    return expected is not None and prior.name == identity.prior_container_name and prior.config_payload() == expected


def _reconnect_after_container_transition(connection: Any, deps: InstallDependencies) -> Any:
    """Discard a session killed by replacement removal/restore before readback."""

    try:
        connection.close()
    except Exception:
        pass
    fresh = deps.connect()
    if fresh is None:
        raise RuntimeError("fresh post-restore database observation is unavailable")
    return fresh
