"""One fail-closed cold-tablespace installer state machine.

Production CLI and the isolated Docker oracle both call :func:`run_install`.
All host/Docker/DB work is injected through ``InstallDependencies``; identity is
issued by the immutable production/disposable contract factory.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.common.compressed_chunk_cold_residency import quote_ident, quote_literal
from packages.common.node27_cold_tablespace_authority import advance_authority, authority_exists, read_authority
from packages.common.node27_cold_tablespace_container import build_recreate_argv
from packages.common.node27_cold_tablespace_identity import validate_identity_for_action
from packages.common.node27_cold_tablespace_receipt import (
    container_snapshot_payload,
    no_go,
    now_iso,
    publish_with_dependencies,
    receipt_template,
    set_authority_receipt,
)
from packages.common.node27_cold_tablespace_recovery import (
    authority_payload,
    phase_hook,
    reconcile,
    rollback,
    terminal_close,
    write_recovery,
)
from packages.common.node27_cold_tablespace_topology import (
    has_cold_bind,
    inspect_preconditions,
    path_admission_blockers,
    path_decision,
    quiescence_blockers,
    readback,
    require_fresh_quiescence,
    require_ready_sql_read_path,
    topology_blockers,
    validate_after_container,
)
from packages.common.node27_cold_tablespace_types import (
    InstallConfig,
    InstallDependencies,
    InstallInterrupted,
    InstallResult,
)
from packages.common.redaction import redact_text


def _arm(
    *, config: InstallConfig, authority: Mapping[str, Any], action: str
) -> dict[str, Any]:
    return write_recovery(
        config.recovery_path,
        advance_authority(authority, phase=str(authority["phase"]), pending_action=action),
    )


def _connect_observation(deps: InstallDependencies, *, readonly: bool) -> Any:
    connection = deps.connect_readonly() if readonly and deps.connect_readonly is not None else deps.connect()
    if connection is None:
        raise RuntimeError("read-only catalog observation is unavailable")
    return connection


def _advance(
    *,
    config: InstallConfig,
    authority: Mapping[str, Any],
    phase: str,
    deps: InstallDependencies,
    pending_action: str | None = None,
    **ownership: bool,
) -> dict[str, Any]:
    advanced = write_recovery(
        config.recovery_path,
        advance_authority(authority, phase=phase, pending_action=pending_action, **ownership),
    )
    phase_hook(deps, phase)
    return advanced


def _complete_existing(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    receipt: dict[str, Any],
) -> InstallResult:
    receipt.update({"outcome": "already_ready", "state": "ready"})
    return InstallResult(
        "already_ready",
        publish_with_dependencies(config.receipt_path, receipt, schema, deps),
        dict(schema),
    )


def _prepare_authority(
    *, config: InstallConfig, deps: InstallDependencies, now_value: Any, snapshot: Any, path_observed: Mapping[str, Any]
) -> dict[str, Any]:
    authority = write_recovery(
        config.recovery_path,
        authority_payload(
            config=config,
            now_iso=now_iso(now_value),
            snapshot=snapshot,
            path_observed=path_observed,
        ),
    )
    phase_hook(deps, "prepared")
    return authority


def _create_path_if_needed(
    *,
    config: InstallConfig,
    schema: Mapping[str, Any],
    deps: InstallDependencies,
    now_value: Any,
    receipt: dict[str, Any],
    authority: Mapping[str, Any],
    path_observed: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any]] | InstallResult:
    if path_observed.get("exists"):
        return dict(authority), path_observed
    if deps.ensure_host_path is None:
        set_authority_receipt(receipt, authority)
        return no_go(
            config=config,
            schema=schema,
            now=now_value,
            receipt=receipt,
            blockers=["fresh host directory creation boundary is unavailable"],
            deps=deps,
        )
    require_fresh_quiescence(deps)
    authority = _arm(config=config, authority=authority, action="create_host_path")
    created = dict(deps.ensure_host_path())
    from packages.common.node27_cold_tablespace_receipt import path_payload

    receipt["path"] = path_payload(config.identity, created)
    if not path_decision(config, created).approved:
        set_authority_receipt(receipt, authority)
        return no_go(
            config=config,
            schema=schema,
            now=now_value,
            receipt=receipt,
            blockers=["created host directory did not satisfy the fresh path contract"],
            deps=deps,
        )
    advanced = _advance(
        config=config,
        authority=authority,
        phase="path_created",
        deps=deps,
        host_path_created=True,
    )
    return advanced, created


def _replace_container(
    *,
    config: InstallConfig,
    deps: InstallDependencies,
    authority: Mapping[str, Any],
    snapshot: Any,
) -> dict[str, Any]:
    identity = config.identity
    require_fresh_quiescence(deps)
    authority = _arm(config=config, authority=authority, action="stop_prior")
    deps.docker((identity.docker_bin, "stop", identity.container_name))
    authority = _advance(config=config, authority=authority, phase="prior_stopped", deps=deps, prior_stopped=True)
    require_fresh_quiescence(deps)
    authority = _arm(config=config, authority=authority, action="rename_prior")
    deps.docker((identity.docker_bin, "rename", identity.container_name, identity.prior_container_name))
    authority = _advance(config=config, authority=authority, phase="prior_renamed", deps=deps, prior_renamed=True)
    require_fresh_quiescence(deps)
    authority = _arm(config=config, authority=authority, action="create_replacement")
    deps.docker(build_recreate_argv(snapshot, identity=identity))
    return _advance(
        config=config,
        authority=authority,
        phase="replacement_created",
        deps=deps,
        installer_container_created=True,
    )


def _install_catalog(
    *, config: InstallConfig, deps: InstallDependencies, connection: Any, authority: Mapping[str, Any]
) -> dict[str, Any]:
    require_fresh_quiescence(deps)
    identity = config.identity
    authority = _arm(config=config, authority=authority, action="create_catalog")
    connection.execute(
        f"CREATE TABLESPACE {quote_ident(identity.tablespace)} LOCATION {quote_literal(identity.container_path)}"
    )
    return _advance(config=config, authority=authority, phase="ddl_created", deps=deps, catalog_created=True)


def run_install(config: InstallConfig, deps: InstallDependencies) -> InstallResult:
    """Observe or install one fresh tablespace through the sole core sequence.

    A durable authority is always reconciled before ordinary inspection.  Without
    authority, dry-run and enforce both perform complete *read-only* catalog,
    ``pg_tblspc``, current/stopped bind, and placement observations.  Therefore
    a missing DSN or catalog observation is a schema-valid NO-GO, never a false
    clean dry-run.
    """

    schema = InstallConfig.load_schema()
    now_value = deps.now()
    receipt = receipt_template(config, outcome="dry_run", state="preflight")
    receipt["generated_at"] = now_iso(now_value)
    connection: Any | None = None
    authority: Mapping[str, Any] | None = None
    try:
        validate_identity_for_action(config.identity)
        recovered = reconcile(config=config, schema=schema, deps=deps, now=now_value, receipt=receipt)
        if recovered is not None:
            return recovered

        connection = _connect_observation(deps, readonly=not config.enforce)
        snapshot, path_observed, precondition_blockers = inspect_preconditions(config, deps, receipt, connection)
        topology_errors, topology, complete_ready = topology_blockers(
            connection,
            deps,
            snapshot,
            config.identity,
            receipt,
            expected_device_identity=config.expected_device_identity,
        )
        bind_present = has_cold_bind(snapshot, config.identity)
        path_blockers = path_admission_blockers(config, path_observed, complete_ready=complete_ready)
        blockers = tuple(dict.fromkeys((*precondition_blockers, *topology_errors, *path_blockers)))
        if complete_ready and not blockers:
            return _complete_existing(config=config, schema=schema, deps=deps, receipt=receipt)
        if blockers:
            return no_go(config=config, schema=schema, now=now_value, receipt=receipt, blockers=blockers, deps=deps)
        if not config.enforce:
            return InstallResult(
                "dry_run",
                publish_with_dependencies(config.receipt_path, receipt, schema, deps),
                dict(schema),
            )
        if topology != "absent" or bind_present:
            return no_go(
                config=config,
                schema=schema,
                now=now_value,
                receipt=receipt,
                blockers=["partial or drifted topology is present"],
                deps=deps,
            )

        quiescence = quiescence_blockers(deps)
        if quiescence:
            return no_go(config=config, schema=schema, now=now_value, receipt=receipt, blockers=quiescence, deps=deps)
        authority = _prepare_authority(
            config=config,
            deps=deps,
            now_value=now_value,
            snapshot=snapshot,
            path_observed=path_observed,
        )
        prepared = _create_path_if_needed(
            config=config,
            schema=schema,
            deps=deps,
            now_value=now_value,
            receipt=receipt,
            authority=authority,
            path_observed=path_observed,
        )
        if isinstance(prepared, InstallResult):
            return prepared
        authority, _path = prepared
        set_authority_receipt(receipt, authority)
        receipt.update({"outcome": "in_progress", "state": "in_progress"})
        publish_with_dependencies(config.receipt_path, receipt, schema, deps)

        authority = _replace_container(config=config, deps=deps, authority=authority, snapshot=snapshot)
        set_authority_receipt(receipt, authority)
        stale = connection
        connection = None
        try:
            stale.close()
        except Exception:
            pass
        require_ready_sql_read_path(deps)
        connection = _connect_observation(deps, readonly=False)
        after, _changed = validate_after_container(snapshot, dict(deps.inspect_container()), config.identity)
        if not has_cold_bind(after, config.identity):
            raise RuntimeError("recreated container omitted the required cold bind")
        target = dict(deps.inspect_target())
        if target.get("container_bind") != str(config.identity.host_path) or target.get("writable") is not True:
            receipt["readback"] = readback(
                connection, deps, config.identity, expected_device_identity=config.expected_device_identity
            )
            receipt["blockers"] = ["container cold bind is not ready or writable"]
            return rollback(
                config=config, schema=schema, deps=deps, connection=connection, receipt=receipt, authority=authority
            )

        authority = _install_catalog(config=config, deps=deps, connection=connection, authority=authority)
        set_authority_receipt(receipt, authority)
        receipt["readback"] = readback(
            connection, deps, config.identity, expected_device_identity=config.expected_device_identity
        )
        if not receipt["readback"]["approved"]:
            receipt["blockers"] = ["catalog/bind/path/writability/no-attach/default-placement readback failed"]
            return rollback(
                config=config, schema=schema, deps=deps, connection=connection, receipt=receipt, authority=authority
            )
        receipt["container_snapshot"] = container_snapshot_payload(after)
        return terminal_close(
            config=config,
            schema=schema,
            deps=deps,
            receipt=receipt,
            authority=authority,
            outcome="installed",
            state="installed",
        )
    except InstallInterrupted:
        # Test-only process-like interruption leaves the durable authority and
        # topology untouched for the next invocation to reconcile.
        raise
    except Exception as error:
        if authority is None:
            try:
                if authority_exists(config.recovery_path):
                    authority = read_authority(config.recovery_path)
            except Exception:
                authority = None
        else:
            try:
                if authority_exists(config.recovery_path):
                    authority = read_authority(config.recovery_path)
            except Exception:
                pass
        receipt["error"] = {
            "class": "installer",
            "stage": "enforce" if config.enforce else "inspection",
            "reason": redact_text(type(error).__name__),
        }
        receipt["blockers"] = list(
            dict.fromkeys((*receipt["blockers"], "installer precondition or enforcement failed"))
        )
        pending = authority.get("pending_action") if isinstance(authority, Mapping) else None
        if authority is not None:
            set_authority_receipt(receipt, authority)
        if config.enforce and authority is not None and not pending and connection is not None:
            return rollback(
                config=config, schema=schema, deps=deps, connection=connection, receipt=receipt, authority=authority
            )
        return no_go(
            config=config,
            schema=schema,
            now=now_value,
            receipt=receipt,
            blockers=receipt["blockers"],
            deps=deps,
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
