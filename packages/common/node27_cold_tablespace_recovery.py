"""Durable recovery, rollback, and terminal authority closure for the installer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_residency import quote_ident
from packages.common.node27_cold_tablespace_authority import (
    AuthorityError,
    advance_authority,
    authority_exists,
    private_snapshot_digest,
    read_authority,
    remove_authority,
    write_authority,
)
from packages.common.node27_cold_tablespace_container import ContainerSnapshot, normalize_raw_inspect
from packages.common.node27_cold_tablespace_identity import ColdTablespaceIdentity
from packages.common.node27_cold_tablespace_receipt import (
    container_snapshot_payload,
    no_go,
    path_payload,
    publish_with_dependencies,
    render,
    set_authority_receipt,
)
from packages.common.node27_cold_tablespace_topology import (
    catalog_topology,
    has_cold_bind,
    installer_current_bind_reference,
    readback,
    reference_blockers,
    require_fresh_quiescence,
    require_ready_sql_read_path,
    rollback_path_state,
)
from packages.common.node27_cold_tablespace_types import (
    InstallConfig,
    InstallDependencies,
    InstallInterrupted,
    InstallResult,
)
from packages.common.redaction import redact_text


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
    return expected.get("config_digest") == snapshot.config_digest and expected.get("cold_bind") == identity.cold_bind


def inspect_named(deps: InstallDependencies, name: str, identity: ColdTablespaceIdentity) -> ContainerSnapshot:
    """Freshly inspect an explicit name; never alias renamed prior to current."""

    if deps.inspect_named_container is not None:
        return normalize_raw_inspect(dict(deps.inspect_named_container(name)))
    if name == identity.container_name:
        return normalize_raw_inspect(dict(deps.inspect_container()))
    raise RuntimeError("named container inspection boundary is unavailable")


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
        advance_authority(authority, phase="terminal_pending_cleanup", **ownership_update),
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


def rollback(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    connection: Any,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
) -> InstallResult:
    """Remove only installer-owned cleanup subjects, then re-observe before delete."""

    identity = config.identity
    ownership = authority["ownership"]
    blockers: list[str] = []
    prior_restored = False
    host_path_removed = False
    container_transitioned = False
    restored_snapshot: ContainerSnapshot | None = None
    try:
        require_fresh_quiescence(deps)
        topology, _location, _target = catalog_topology(connection, identity)
        if bool(ownership["catalog_created"]):
            if topology != "expected" or deps.catalog_dependents() != 0:
                raise RuntimeError("installer-created catalog is mixed or has dependents")
            require_fresh_quiescence(deps)
            connection.execute(f"DROP TABLESPACE {quote_ident(identity.tablespace)}")
            authority = _record_rollback_progress(config, authority, catalog_created=False)
            ownership = authority["ownership"]
        elif topology != "absent":
            raise RuntimeError("unowned or uncertain catalog blocks rollback")

        # Before deleting the replacement, an installer-owned current cold bind
        # is expected cleanup state, not a blanket NO-GO.  Every completed action
        # changes the durable authority before the next mutation, so a process
        # loss cannot make a later recovery replay an already-finished step.
        if bool(ownership["installer_container_created"]):
            require_fresh_quiescence(deps)
            deps.docker((identity.docker_bin, "rm", "-f", identity.container_name))
            container_transitioned = True
            authority = _record_rollback_progress(config, authority, installer_container_created=False)
            ownership = authority["ownership"]
        elif bool(ownership["prior_renamed"]):
            raise RuntimeError("replacement ownership is missing after prior rename")

        if bool(ownership["prior_renamed"]):
            prior = inspect_named(deps, identity.prior_container_name, identity)
            if not _named_prior_matches(authority, prior, identity):
                raise RuntimeError("renamed prior configuration is not reconstructible")
            require_fresh_quiescence(deps)
            deps.docker((identity.docker_bin, "rename", identity.prior_container_name, identity.container_name))
            container_transitioned = True
            # Rename preserves the observed instance identity, so the freshly
            # observed prior remains truthful for the renamed-back current name;
            # the restart branch below replaces it with a post-start observation.
            restored_snapshot = prior
            authority = _record_rollback_progress(config, authority, prior_renamed=False)
            ownership = authority["ownership"]

        if bool(ownership["prior_stopped"]):
            prior = inspect_named(deps, identity.container_name, identity)
            if not prior_config_matches(authority, prior, identity):
                raise RuntimeError("stopped prior configuration differs")
            require_fresh_quiescence(deps)
            deps.docker((identity.docker_bin, "start", identity.container_name))
            container_transitioned = True
            require_ready_sql_read_path(deps)
            restored = inspect_named(deps, identity.container_name, identity)
            if not prior_config_matches(authority, restored, identity):
                raise RuntimeError("restored prior container config/read path differs")
            restored_snapshot = restored
            prior_restored = True
            authority = _record_rollback_progress(config, authority, prior_stopped=False)
            ownership = authority["ownership"]

        if container_transitioned:
            connection = _reconnect_after_container_transition(connection, deps)
        refs = reference_blockers(connection, deps, identity)
        try:
            connection.close()
        except Exception:
            pass
        identity_matches, empty = rollback_path_state(
            deps, expected_device_identity=authority["path"].get("device_identity")
        )
        if bool(ownership["host_path_created"]):
            if refs:
                blockers.extend(refs)
            elif not identity_matches:
                blockers.append("host path identity is uncertain")
            elif not empty:
                blockers.append("host path is not empty")
            elif deps.remove_host_path is None:
                blockers.append("host path removal boundary is unavailable")
            else:
                require_fresh_quiescence(deps)
                host_path_removed = deps.remove_host_path() is True
                if host_path_removed:
                    authority = _record_rollback_progress(config, authority, host_path_created=False)
                    ownership = authority["ownership"]
                else:
                    blockers.append("installer-owned host path could not be removed")
        elif refs:
            blockers.extend(refs)

        receipt.update(
            {
                "rollback": {
                    "attempted": True,
                    "prior_restored": prior_restored,
                    "host_path_removed": host_path_removed,
                    "blockers": list(dict.fromkeys(blockers)),
                },
                "blockers": list(dict.fromkeys((*receipt["blockers"], *blockers))),
            }
        )
        if restored_snapshot is not None:
            receipt["container_snapshot"] = container_snapshot_payload(restored_snapshot)
        if blockers:
            set_authority_receipt(receipt, authority, state="pending_cleanup")
            receipt.update({"outcome": "pending_cleanup", "state": "pending_cleanup"})
            return InstallResult(
                "pending_cleanup", publish_with_dependencies(config.receipt_path, receipt, schema, deps), dict(schema)
            )
        return terminal_close(
            config=config,
            schema=schema,
            deps=deps,
            receipt=receipt,
            authority=authority,
            outcome="rollback",
            state="rollback",
        )
    except Exception as error:
        receipt.update(
            {
                "outcome": "no_go",
                "state": "blocked",
                "error": {
                    "class": "rollback",
                    "stage": "restore",
                    "reason": redact_text(str(error)) or redact_text(type(error).__name__),
                },
                "blockers": list(dict.fromkeys((*receipt["blockers"], *blockers, "rollback state is uncertain"))),
                "rollback": {
                    "attempted": True,
                    "prior_restored": prior_restored,
                    "host_path_removed": host_path_removed,
                    "blockers": list(dict.fromkeys((*blockers, "rollback state is uncertain"))),
                },
            }
        )
        set_authority_receipt(receipt, authority)
        return InstallResult(
            "no_go", publish_with_dependencies(config.receipt_path, receipt, schema, deps), dict(schema)
        )


def _early_path_cleanup(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now: datetime,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    connection: Any | None,
    prior_restored: bool,
    restored_snapshot: ContainerSnapshot | None = None,
) -> InstallResult:
    """Remove only the recorded fresh path after a fresh post-restore survey."""

    if restored_snapshot is not None:
        receipt["container_snapshot"] = container_snapshot_payload(restored_snapshot)
    if connection is None:
        if bool(authority["ownership"]["host_path_created"]):
            raise RuntimeError("fresh recovery catalog observation is unavailable")
        receipt["rollback"] = {
            "attempted": True,
            "prior_restored": prior_restored,
            "host_path_removed": False,
            "blockers": [],
        }
        return terminal_close(
            config=config,
            schema=schema,
            deps=deps,
            receipt=receipt,
            authority=authority,
            outcome="rollback",
            state="rollback",
        )
    identity = config.identity
    blockers = list(reference_blockers(connection, deps, identity))
    if blockers:
        set_authority_receipt(receipt, authority, state="pending_cleanup")
        return no_go(config=config, schema=schema, now=now, receipt=receipt, blockers=blockers, deps=deps)
    if not bool(authority["ownership"]["host_path_created"]):
        receipt["rollback"] = {
            "attempted": True,
            "prior_restored": prior_restored,
            "host_path_removed": False,
            "blockers": [],
        }
        return terminal_close(
            config=config,
            schema=schema,
            deps=deps,
            receipt=receipt,
            authority=authority,
            outcome="rollback",
            state="rollback",
        )
    identity_matches, empty = rollback_path_state(
        deps, expected_device_identity=authority["path"].get("device_identity")
    )
    if not identity_matches:
        blockers.append("installer-created path identity is uncertain")
    if not empty:
        blockers.append("installer-created path is not empty")
    if deps.remove_host_path is None:
        blockers.append("host path removal boundary is unavailable")
    if blockers:
        set_authority_receipt(receipt, authority, state="pending_cleanup")
        return no_go(config=config, schema=schema, now=now, receipt=receipt, blockers=blockers, deps=deps)
    require_fresh_quiescence(deps)
    if deps.remove_host_path() is not True:
        set_authority_receipt(receipt, authority, state="pending_cleanup")
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["installer-created host path could not be removed"],
            deps=deps,
        )
    authority = _record_rollback_progress(config, authority, host_path_created=False)
    receipt["rollback"] = {
        "attempted": True,
        "prior_restored": prior_restored,
        "host_path_removed": True,
        "blockers": [],
    }
    return terminal_close(
        config=config,
        schema=schema,
        deps=deps,
        receipt=receipt,
        authority=authority,
        outcome="rollback",
        state="rollback",
    )


def _retain_early_authority(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now: datetime,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    blocker: str,
) -> InstallResult:
    """Publish an unresolved early authority without implying terminal cleanup."""

    set_authority_receipt(receipt, authority)
    return no_go(config=config, schema=schema, now=now, receipt=receipt, blockers=[blocker], deps=deps)


def _early_recovery(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now: datetime,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    connection: Any | None,
    phase_override: str | None = None,
) -> InstallResult:
    identity = config.identity
    phase = phase_override or authority["phase"]
    if phase == "prepared":
        if connection is None:
            raise RuntimeError("recovery catalog observation is unavailable")
        topology, _location, _target = catalog_topology(connection, identity)
        current = inspect_named(deps, identity.container_name, identity)
        if (
            topology != "absent"
            or has_cold_bind(current, identity)
            or not prior_config_matches(authority, current, identity)
        ):
            return _retain_early_authority(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                blocker="prepared prior container or topology cannot be safely confirmed",
            )
        receipt["rollback"] = {"attempted": True, "prior_restored": False, "host_path_removed": False, "blockers": []}
        receipt["container_snapshot"] = container_snapshot_payload(current)
        return terminal_close(
            config=config,
            schema=schema,
            deps=deps,
            receipt=receipt,
            authority=authority,
            outcome="rollback",
            state="rollback",
        )
    if connection is None and phase == "path_created":
        raise RuntimeError("recovery catalog observation is unavailable")
    if phase == "path_created":
        return _early_path_cleanup(
            config=config,
            schema=schema,
            deps=deps,
            now=now,
            receipt=receipt,
            authority=authority,
            connection=connection,
            prior_restored=False,
        )
    if phase == "prior_stopped":
        current = inspect_named(deps, identity.container_name, identity)
        if has_cold_bind(current, identity) or not prior_config_matches(authority, current, identity):
            return _retain_early_authority(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                blocker="stopped prior cannot be safely restored",
            )
        require_fresh_quiescence(deps)
        deps.docker((identity.docker_bin, "start", identity.container_name))
        require_ready_sql_read_path(deps)
        authority = _record_rollback_progress(config, authority, prior_stopped=False)
        connection = _connect_observation(deps)
        topology, _location, _target = catalog_topology(connection, identity)
        restored = inspect_named(deps, identity.container_name, identity)
        if topology != "absent" or not prior_config_matches(authority, restored, identity):
            return _retain_early_authority(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                blocker="started prior config/read path differs",
            )
        return _early_path_cleanup(
            config=config,
            schema=schema,
            deps=deps,
            now=now,
            receipt=receipt,
            authority=authority,
            connection=connection,
            prior_restored=True,
            restored_snapshot=restored,
        )
    if phase == "prior_renamed":
        prior = inspect_named(deps, identity.prior_container_name, identity)
        if has_cold_bind(prior, identity) or not _named_prior_matches(authority, prior, identity):
            return _retain_early_authority(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                blocker="renamed prior cannot be safely restored",
            )
        require_fresh_quiescence(deps)
        deps.docker((identity.docker_bin, "rename", identity.prior_container_name, identity.container_name))
        authority = _record_rollback_progress(config, authority, prior_renamed=False)
        require_fresh_quiescence(deps)
        deps.docker((identity.docker_bin, "start", identity.container_name))
        require_ready_sql_read_path(deps)
        authority = _record_rollback_progress(config, authority, prior_stopped=False)
        connection = _connect_observation(deps)
        topology, _location, _target = catalog_topology(connection, identity)
        restored = inspect_named(deps, identity.container_name, identity)
        if topology != "absent" or not prior_config_matches(authority, restored, identity):
            return _retain_early_authority(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                blocker="restored prior config/read path differs",
            )
        return _early_path_cleanup(
            config=config,
            schema=schema,
            deps=deps,
            now=now,
            receipt=receipt,
            authority=authority,
            connection=connection,
            prior_restored=True,
            restored_snapshot=restored,
        )
    raise RuntimeError("recovery early phase is invalid")


def _pending_cleanup(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now: datetime,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    connection: Any,
) -> InstallResult:
    identity = config.identity
    ownership = authority["ownership"]
    snapshot = inspect_named(deps, identity.container_name, identity)
    topology, _location, _target = catalog_topology(connection, identity)
    if (
        topology == "expected"
        and has_cold_bind(snapshot, identity)
        and expected_snapshot_matches(authority, snapshot, identity)
    ):
        receipt["readback"] = readback(connection, deps, identity)
        if receipt["readback"]["approved"]:
            receipt["container_snapshot"] = container_snapshot_payload(snapshot)
            return terminal_close(
                config=config,
                schema=schema,
                deps=deps,
                receipt=receipt,
                authority=authority,
                outcome="installed",
                state="installed",
            )
    if topology == "absent" and not bool(ownership["prior_renamed"]) and not bool(ownership["prior_stopped"]):
        if prior_config_matches(authority, snapshot, identity):
            return _early_path_cleanup(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                connection=connection,
                prior_restored=True,
                restored_snapshot=snapshot,
            )
    set_authority_receipt(receipt, authority, state="pending_cleanup")
    return no_go(
        config=config,
        schema=schema,
        now=now,
        receipt=receipt,
        blockers=["terminal pending-cleanup topology is not freshly proven"],
        deps=deps,
    )


def _connect_observation(deps: InstallDependencies) -> Any:
    connection = deps.connect_readonly() if deps.connect_readonly is not None else deps.connect()
    if connection is None:
        raise RuntimeError("read-only catalog observation is unavailable")
    return connection


def reconcile(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now: datetime,
    receipt: dict[str, Any],
) -> InstallResult | None:
    """Reconcile authority before ordinary inspection or a new install attempt."""

    try:
        present = authority_exists(config.recovery_path)
    except AuthorityError as error:
        raise RuntimeError("private recovery authority is unavailable") from error
    if not present:
        return None
    try:
        authority = _read_recovery(config.recovery_path)
    except RuntimeError:
        # The pathname itself is authoritative even if its contents are corrupt.
        # Never inspect/connect/mutate past it, and never claim its absence.
        receipt["authority"] = {"state": "sidecar", "phase": None, "path_present": True}
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["private recovery authority is unavailable or malformed"],
            state="recovery_required" if not config.enforce else "blocked",
            deps=deps,
        )
    if not authority_matches_identity(authority, config.identity):
        set_authority_receipt(receipt, authority)
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["recovery authority identity differs from this installer contract"],
            state="recovery_required" if not config.enforce else "blocked",
            deps=deps,
        )
    set_authority_receipt(receipt, authority)
    if not config.enforce:
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["recovery_required: dry-run cannot alter an interrupted installation"],
            state="recovery_required",
            deps=deps,
        )
    connection: Any | None = None
    try:
        phase = authority["phase"]
        ownership = authority["ownership"]
        # A stopped or renamed prior has no PostgreSQL listener.  Restore its
        # container/read path first; only then open the required fresh catalog
        # observation.  A terminal-pending authority can retain either flag if
        # a rollback was interrupted after an earlier durable cleanup step.
        early_phase = phase if phase in {"prior_stopped", "prior_renamed"} else None
        if phase == "terminal_pending_cleanup":
            if bool(ownership["prior_renamed"]):
                early_phase = "prior_renamed"
            elif bool(ownership["prior_stopped"]):
                early_phase = "prior_stopped"
        if early_phase is not None:
            # The pre-transition receipt carries the path identity used for any
            # later removal check.  Do not replace it with a fresh path payload
            # here: on a real interrupted deployment the host path can be
            # absent/mounted differently until the restored container is live.
            return _early_recovery(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                connection=None,
                phase_override=early_phase,
            )
        if phase == "replacement_created":
            require_ready_sql_read_path(deps)
        connection = _connect_observation(deps)
        if phase == "prepared":
            return _early_recovery(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                connection=connection,
            )
        receipt["path"] = path_payload(config.identity, dict(deps.inspect_path()))
        if phase == "terminal_pending_cleanup":
            return _pending_cleanup(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                connection=connection,
            )
        if phase == "path_created":
            return _early_recovery(
                config=config,
                schema=schema,
                deps=deps,
                now=now,
                receipt=receipt,
                authority=authority,
                connection=connection,
            )
        snapshot = inspect_named(deps, config.identity.container_name, config.identity)
        topology, _location, _target = catalog_topology(connection, config.identity)
        if (
            topology == "expected"
            and has_cold_bind(snapshot, config.identity)
            and expected_snapshot_matches(authority, snapshot, config.identity)
        ):
            receipt["readback"] = readback(connection, deps, config.identity)
            if receipt["readback"]["approved"]:
                receipt["container_snapshot"] = container_snapshot_payload(snapshot)
                return terminal_close(
                    config=config,
                    schema=schema,
                    deps=deps,
                    receipt=receipt,
                    authority=authority,
                    outcome="installed",
                    state="installed",
                )
        if phase == "replacement_created" and topology != "absent":
            raise RuntimeError("replacement-created authority sees unrecorded catalog state")
        if phase == "ddl_created" and topology not in {"expected", "absent"}:
            raise RuntimeError("DDL-created authority sees mixed catalog state")
        # Unknown external references are not cleanup subjects.  The expected
        # live replacement bind is allowed as a cleanup subject; every other
        # current/stopped reference blocks before destructive work.
        current_refs = tuple(deps.current_bind_references())
        stopped_refs = tuple(deps.stopped_bind_references())
        ownership = authority["ownership"]
        expected_current = (
            bool(ownership["installer_container_created"])
            and len(current_refs) == 1
            and installer_current_bind_reference(config.identity, current_refs[0])
        )
        if stopped_refs or (current_refs and not expected_current):
            set_authority_receipt(receipt, authority)
            return no_go(
                config=config,
                schema=schema,
                now=now,
                receipt=receipt,
                blockers=["mixed current or stopped cold-bind topology blocks rollback"],
                deps=deps,
            )
        return rollback(
            config=config, schema=schema, deps=deps, connection=connection, receipt=receipt, authority=authority
        )
    except InstallInterrupted:
        raise
    except Exception as error:
        receipt["error"] = {"class": "recovery", "stage": "startup", "reason": redact_text(type(error).__name__)}
        set_authority_receipt(receipt, authority)
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["unresolved private recovery authority blocks a new installation"],
            deps=deps,
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
