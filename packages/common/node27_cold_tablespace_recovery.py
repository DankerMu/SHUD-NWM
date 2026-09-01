"""Durable recovery, rollback, and terminal authority closure for the installer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import quote_ident
from packages.common.node27_cold_tablespace_authority import (
    AuthorityError,
    advance_authority,
    authority_exists,
    read_authority,
)
from packages.common.node27_cold_tablespace_container import ContainerSnapshot
from packages.common.node27_cold_tablespace_observation import (
    NamedObservationError,
    _adopt_pending_post,
    _arm_action,
    _classify_authority_pending,
    _named_prior_matches,
    _read_recovery,
    _reconnect_after_container_transition,
    _record_rollback_progress,
    authority_matches_identity,
    authority_payload,
    expected_snapshot_matches,
    inspect_named,
    inspect_named_optional,
    phase_hook,
    prior_config_matches,
    remove_recovery,
    terminal_close,
    write_recovery,
)
from packages.common.node27_cold_tablespace_pending import INSTALL_ACTIONS
from packages.common.node27_cold_tablespace_receipt import (
    container_snapshot_payload,
    no_go,
    path_payload,
    publish_with_dependencies,
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

__all__ = [
    "NamedObservationError",
    "authority_matches_identity",
    "authority_payload",
    "expected_snapshot_matches",
    "inspect_named",
    "inspect_named_optional",
    "phase_hook",
    "prior_config_matches",
    "reconcile",
    "remove_recovery",
    "rollback",
    "terminal_close",
    "write_recovery",
]


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
            authority = _arm_action(config, authority, "drop_catalog")
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
            authority = _arm_action(config, authority, "remove_replacement")
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
            authority = _arm_action(config, authority, "rename_prior_back")
            deps.docker((identity.docker_bin, "rename", identity.prior_container_name, identity.container_name))
            container_transitioned = True
            restored_snapshot = prior
            authority = _record_rollback_progress(config, authority, prior_renamed=False)
            ownership = authority["ownership"]

        if bool(ownership["prior_stopped"]):
            prior = inspect_named(deps, identity.container_name, identity)
            if not prior_config_matches(authority, prior, identity):
                raise RuntimeError("stopped prior configuration differs")
            require_fresh_quiescence(deps)
            authority = _arm_action(config, authority, "start_prior")
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
                authority = _arm_action(config, authority, "remove_host_path")
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
    except InstallInterrupted:
        raise
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
    authority = _arm_action(config, authority, "remove_host_path")
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
        authority = _arm_action(config, authority, "start_prior")
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
        authority = _arm_action(config, authority, "rename_prior_back")
        deps.docker((identity.docker_bin, "rename", identity.prior_container_name, identity.container_name))
        authority = _record_rollback_progress(config, authority, prior_renamed=False)
        require_fresh_quiescence(deps)
        authority = _arm_action(config, authority, "start_prior")
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
        receipt["readback"] = readback(
            connection, deps, identity, expected_device_identity=config.expected_device_identity
        )
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
        set_authority_receipt(receipt, authority, state="pending_cleanup")
        return no_go(
            config=config,
            schema=schema,
            now=now,
            receipt=receipt,
            blockers=["terminal pending-cleanup readiness or readback is unavailable"],
            deps=deps,
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
    if topology == "absent":
        return rollback(
            config=config,
            schema=schema,
            deps=deps,
            connection=connection,
            receipt=receipt,
            authority=authority,
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
        pending_action = authority.get("pending_action")
        if pending_action:
            topology_hint = None
            if pending_action in {"create_catalog", "drop_catalog"}:
                require_ready_sql_read_path(deps)
                connection = _connect_observation(deps)
                topology_hint, _location, _target = catalog_topology(connection, config.identity)
            try:
                pending_class = _classify_authority_pending(
                    config=config, deps=deps, authority=authority, topology=topology_hint
                )
            except NamedObservationError:
                set_authority_receipt(receipt, authority)
                return no_go(
                    config=config,
                    schema=schema,
                    now=now,
                    receipt=receipt,
                    blockers=["pending action observation is unavailable"],
                    deps=deps,
                )
            if pending_class == "mixed":
                set_authority_receipt(receipt, authority)
                return no_go(
                    config=config,
                    schema=schema,
                    now=now,
                    receipt=receipt,
                    blockers=["pending action topology is mixed or unknown"],
                    deps=deps,
                )
            if pending_class == "post":
                authority = _adopt_pending_post(config, authority, str(pending_action))
                phase = authority["phase"]
                ownership = authority["ownership"]
                pending_action = None
            elif pending_action in INSTALL_ACTIONS:
                if pending_action == "create_catalog":
                    authority = write_recovery(
                        config.recovery_path,
                        advance_authority(authority, phase=str(authority["phase"]), pending_action=None),
                    )
                    if connection is None:
                        connection = _connect_observation(deps)
                    return rollback(
                        config=config,
                        schema=schema,
                        deps=deps,
                        connection=connection,
                        receipt=receipt,
                        authority=authority,
                    )
                authority = write_recovery(
                    config.recovery_path,
                    advance_authority(authority, phase=str(authority["phase"]), pending_action=None),
                )
                phase = authority["phase"]
                if phase in {"prepared", "path_created", "prior_stopped", "prior_renamed"}:
                    return _early_recovery(
                        config=config,
                        schema=schema,
                        deps=deps,
                        now=now,
                        receipt=receipt,
                        authority=authority,
                        connection=None if phase in {"prior_stopped", "prior_renamed"} else _connect_observation(deps),
                    )
                if connection is None:
                    connection = _connect_observation(deps)
                return rollback(
                    config=config,
                    schema=schema,
                    deps=deps,
                    connection=connection,
                    receipt=receipt,
                    authority=authority,
                )
            else:
                if pending_action in {"rename_prior_back", "start_prior"}:
                    return _early_recovery(
                        config=config,
                        schema=schema,
                        deps=deps,
                        now=now,
                        receipt=receipt,
                        authority=authority,
                        connection=None,
                        phase_override="prior_renamed" if pending_action == "rename_prior_back" else "prior_stopped",
                    )
                if pending_action == "drop_catalog" and connection is None:
                    connection = _connect_observation(deps)
                return rollback(
                    config=config,
                    schema=schema,
                    deps=deps,
                    connection=connection if connection is not None else _connect_observation(deps),
                    receipt=receipt,
                    authority=authority,
                )
        # A stopped or renamed prior has no PostgreSQL listener.  Restore its
        # container/read path first; only then open the required fresh catalog
        # observation.  A terminal-pending authority can retain either flag if
        # a rollback was interrupted after an earlier durable cleanup step.
        early_phase = phase if phase in {"prior_stopped", "prior_renamed"} else None
        if phase == "terminal_pending_cleanup":
            try:
                current = inspect_named_optional(deps, config.identity.container_name, config.identity)
            except NamedObservationError:
                set_authority_receipt(receipt, authority)
                return no_go(
                    config=config,
                    schema=schema,
                    now=now,
                    receipt=receipt,
                    blockers=["terminal pending-cleanup observation is unavailable"],
                    deps=deps,
                )
            if (
                current is not None
                and expected_snapshot_matches(authority, current, config.identity)
                and has_cold_bind(current, config.identity)
            ):
                try:
                    require_ready_sql_read_path(deps)
                    connection = _connect_observation(deps)
                    return _pending_cleanup(
                        config=config,
                        schema=schema,
                        deps=deps,
                        now=now,
                        receipt=receipt,
                        authority=authority,
                        connection=connection,
                    )
                except Exception:
                    set_authority_receipt(receipt, authority)
                    return no_go(
                        config=config,
                        schema=schema,
                        now=now,
                        receipt=receipt,
                        blockers=["terminal pending-cleanup readiness or readback is unavailable"],
                        deps=deps,
                    )
            if current is None:
                if bool(ownership["prior_renamed"]):
                    early_phase = "prior_renamed"
                elif bool(ownership["prior_stopped"]):
                    early_phase = "prior_stopped"
                else:
                    return _early_path_cleanup(
                        config=config,
                        schema=schema,
                        deps=deps,
                        now=now,
                        receipt=receipt,
                        authority=authority,
                        connection=None,
                        prior_restored=False,
                    )
            elif prior_config_matches(authority, current, config.identity):
                if bool(ownership["prior_renamed"]):
                    set_authority_receipt(receipt, authority)
                    return no_go(
                        config=config,
                        schema=schema,
                        now=now,
                        receipt=receipt,
                        blockers=["terminal pending-cleanup topology is mixed or unknown"],
                        deps=deps,
                    )
                if bool(ownership["prior_stopped"]):
                    early_phase = "prior_stopped"
                else:
                    try:
                        require_ready_sql_read_path(deps)
                        connection = _connect_observation(deps)
                    except Exception:
                        set_authority_receipt(receipt, authority)
                        return no_go(
                            config=config,
                            schema=schema,
                            now=now,
                            receipt=receipt,
                            blockers=["terminal pending-cleanup readiness or readback is unavailable"],
                            deps=deps,
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
                        restored_snapshot=current,
                    )
            else:
                set_authority_receipt(receipt, authority)
                return no_go(
                    config=config,
                    schema=schema,
                    now=now,
                    receipt=receipt,
                    blockers=["terminal pending-cleanup topology is mixed or unknown"],
                    deps=deps,
                )
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
            receipt["readback"] = readback(
                connection, deps, config.identity, expected_device_identity=config.expected_device_identity
            )
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
        try:
            if authority_exists(config.recovery_path):
                authority = read_authority(config.recovery_path)
        except Exception:
            pass
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
