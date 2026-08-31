"""Read-only topology, path, and quiescence observations for the installer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_cold_tablespace_container import (
    ContainerSnapshot,
    diff_container_config,
    normalize_raw_inspect,
)
from packages.common.node27_cold_tablespace_evidence import PathObservation, assess_fresh_path, assess_install_capacity
from packages.common.node27_cold_tablespace_identity import ColdTablespaceIdentity
from packages.common.node27_cold_tablespace_receipt import optional_int, optional_string, path_payload
from packages.common.node27_cold_tablespace_types import InstallConfig, InstallDependencies

WRITER_TIMER_UNITS = (
    "nhms-node27-autopipe.service",
    "nhms-node27-autopipe.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-retention.service",
    "nhms-node27-timeseries-retention.timer",
)


def rows(connection: Any, sql: str, params: tuple[object, ...] = ()) -> list[Mapping[str, Any]]:
    result = connection.execute(sql, params)
    if not isinstance(result, Sequence):
        raise RuntimeError("database inspector returned malformed rows")
    return [row for row in result if isinstance(row, Mapping)]


def catalog_topology(connection: Any, identity: ColdTablespaceIdentity) -> tuple[str, str | None, str | None]:
    catalog = rows(
        connection,
        "SELECT pg_tablespace_location(oid) AS location FROM pg_tablespace WHERE spcname = %s",
        (identity.tablespace,),
    )
    pg_tblspc = rows(
        connection,
        "SELECT pg_tablespace_location(space.oid) AS target FROM pg_tablespace AS space WHERE space.spcname = %s",
        (identity.tablespace,),
    )
    location = optional_string(catalog[0].get("location")) if len(catalog) == 1 else None
    target = optional_string(pg_tblspc[0].get("target")) if len(pg_tblspc) == 1 else None
    if not catalog and not pg_tblspc:
        return "absent", None, None
    if (
        len(catalog) == len(pg_tblspc) == 1
        and location == identity.container_path
        and target == identity.container_path
    ):
        return "expected", location, target
    return "drifted", location, target


def external_pg_tblspc_targets(connection: Any) -> tuple[str, ...]:
    observed = rows(
        connection,
        "SELECT pg_tablespace_location(oid) AS target "
        "FROM pg_tablespace WHERE pg_tablespace_location(oid) <> '' ORDER BY target",
    )
    targets: list[str] = []
    for row in observed:
        target = optional_string(row.get("target"))
        if target is None or not target.startswith("/"):
            raise RuntimeError("external pg_tblspc inventory is malformed")
        targets.append(target)
    if len(set(targets)) != len(targets):
        raise RuntimeError("external pg_tblspc inventory contains duplicate targets")
    return tuple(targets)


def has_cold_bind(snapshot: ContainerSnapshot, identity: ColdTablespaceIdentity) -> bool:
    return identity.cold_bind in snapshot.binds


def validate_after_container(
    before: ContainerSnapshot, raw_after: Mapping[str, Any], identity: ColdTablespaceIdentity
) -> tuple[ContainerSnapshot, tuple[str, ...]]:
    after = normalize_raw_inspect(raw_after)
    diff = diff_container_config(before, after, identity=identity)
    if not diff.approved:
        raise RuntimeError("post-recreate container config differs beyond the cold bind")
    return after, diff.changed_fields


def path_observation(observed: Mapping[str, Any]) -> PathObservation:
    return PathObservation(
        exists=bool(observed.get("exists")),
        is_symlink=bool(observed.get("is_symlink")),
        is_directory=bool(observed.get("is_directory")),
        entry_count=optional_int(observed.get("entry_count")),
        uid=optional_int(observed.get("uid")),
        gid=optional_int(observed.get("gid")),
        mode=optional_int(observed.get("mode")),
        mount_device=optional_string(observed.get("mount_device")),
        device_identity=optional_string(observed.get("device_identity")),
        free_bytes=optional_int(observed.get("free_bytes")),
        path_identity=optional_string(observed.get("path_identity")),
    )


def path_decision(config: InstallConfig, observed: Mapping[str, Any]):
    return assess_fresh_path(
        path_observation(observed),
        expected_uid=config.expected_uid,
        expected_gid=config.expected_gid,
        expected_mode=config.expected_mode,
        expected_device_identity=config.expected_device_identity,
    )


def missing_path_is_creatable(config: InstallConfig, observed: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    path = path_observation(observed)
    if path.exists:
        decision = path_decision(config, observed)
        return decision.approved, decision.blockers
    blockers: list[str] = []
    if path.is_symlink or path.is_directory or path.entry_count is not None:
        blockers.append("absent host path observation is malformed")
    if path.device_identity != config.expected_device_identity:
        blockers.append("host path device identity differs")
    if path.mount_device is None:
        blockers.append("host path mount identity is unavailable")
    if path.free_bytes is None or path.free_bytes < 0:
        blockers.append("host path capacity observation is unavailable")
    return not blockers, tuple(blockers)


def quiescence_blockers(deps: InstallDependencies) -> tuple[str, ...]:
    if deps.inspect_quiescence is None:
        return ("writer/timer quiescence evidence is unavailable",)
    try:
        observed = deps.inspect_quiescence()
    except Exception:
        return ("writer/timer quiescence evidence is unavailable",)
    units = observed.get("units") if isinstance(observed, Mapping) else None
    if not isinstance(units, Mapping):
        return ("writer/timer quiescence evidence is malformed",)
    blockers: list[str] = []
    for unit in WRITER_TIMER_UNITS:
        state = units.get(unit)
        if not isinstance(state, Mapping):
            blockers.append(f"writer/timer {unit} state is unavailable")
            continue
        if state.get("active_state") != "inactive" or state.get("sub_state") not in {"dead", "waiting"}:
            blockers.append(f"writer/timer {unit} is active or not drained")
        if state.get("result") not in {"success", "n/a"}:
            blockers.append(f"writer/timer {unit} result is not healthy")
    return tuple(dict.fromkeys(blockers))


def require_fresh_quiescence(deps: InstallDependencies) -> None:
    if quiescence_blockers(deps):
        raise RuntimeError("rollback/enforcement quiescence gate failed")


def require_ready_sql_read_path(deps: InstallDependencies) -> None:
    """Require the injected readiness boundary after a container transition."""

    if deps.wait_ready is None:
        raise RuntimeError("container readiness/SQL read-path boundary is unavailable")
    deps.wait_ready()


def installer_current_bind_reference(identity: ColdTablespaceIdentity, reference: str) -> bool:
    """Accept only the exact current bind representation owned by this install.

    Disposable test boundaries report the owned name.  The production Docker
    boundary adds the immutable host/container pair so an inventory record can
    identify the matching mount without relying on a Docker instance ID.
    """

    return reference in {
        identity.container_name,
        f"{identity.container_name}:{identity.host_path}:{identity.container_path}",
    }


def attached_to_cold(connection: Any, identity: ColdTablespaceIdentity) -> bool:
    attached = False
    for schema, name in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")):
        observed = rows(
            connection,
            "SELECT space.tablespace_name FROM _timescaledb_catalog.tablespace AS space "
            "JOIN _timescaledb_catalog.hypertable AS hypertable ON hypertable.id = space.hypertable_id "
            "WHERE hypertable.schema_name = %s AND hypertable.table_name = %s",
            (schema, name),
        )
        attached = attached or any(row.get("tablespace_name") == identity.tablespace for row in observed)
    return attached


def business_hypertable_default(connection: Any) -> str | None:
    """Prove both business hypertables retain their ordinary default placement."""

    observed = rows(
        connection,
        "SELECT CASE WHEN bool_and(c.reltablespace = 0) THEN 'pg_default' ELSE NULL END AS tablespace "
        "FROM (VALUES (%s, %s), (%s, %s)) AS expected(schema_name, relation_name) "
        "JOIN pg_namespace AS n ON n.nspname = expected.schema_name "
        "JOIN pg_class AS c ON c.relnamespace = n.oid AND c.relname = expected.relation_name",
        ("hydro", "river_timeseries", "met", "forcing_station_timeseries"),
    )
    return optional_string(observed[0].get("tablespace")) if observed else None


def new_chunk_default(connection: Any) -> str | None:
    observed = rows(
        connection,
        "WITH candidate_relations AS ("
        " SELECT chunk_schema AS schema_name, chunk_name AS relation_name FROM timescaledb_information.chunks "
        " WHERE (hypertable_schema, hypertable_name) IN ((%s, %s), (%s, %s)) UNION "
        " SELECT sibling.schema_name, sibling.table_name FROM _timescaledb_catalog.chunk AS origin "
        " JOIN _timescaledb_catalog.chunk AS sibling ON sibling.id = origin.compressed_chunk_id "
        " JOIN _timescaledb_catalog.hypertable AS hypertable ON hypertable.id = origin.hypertable_id "
        " WHERE NOT origin.dropped AND NOT sibling.dropped "
        " AND (hypertable.schema_name, hypertable.table_name) IN ((%s, %s), (%s, %s))"
        "), parents AS (SELECT n.nspname AS schema_name, c.relname AS relation_name FROM pg_class AS c "
        " JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        " WHERE (n.nspname, c.relname) IN ((%s, %s), (%s, %s))) "
        "SELECT CASE WHEN bool_and(c.reltablespace = 0) THEN 'pg_default' ELSE NULL END AS tablespace "
        "FROM (SELECT * FROM candidate_relations UNION SELECT * FROM parents) AS expected "
        "JOIN pg_namespace AS n ON n.nspname = expected.schema_name "
        "JOIN pg_class AS c ON c.relnamespace = n.oid AND c.relname = expected.relation_name",
        ("hydro", "river_timeseries", "met", "forcing_station_timeseries") * 3,
    )
    return optional_string(observed[0].get("tablespace")) if observed else None


def readback(connection: Any, deps: InstallDependencies, identity: ColdTablespaceIdentity) -> dict[str, Any]:
    _topology, location, target = catalog_topology(connection, identity)
    observed = dict(deps.inspect_target())
    writable = observed.get("writable") if isinstance(observed.get("writable"), bool) else None
    return {
        "approved": bool(
            location == identity.container_path
            and target == identity.container_path
            and observed.get("container_name") == identity.container_name
            and observed.get("container_bind") == str(identity.host_path)
            and observed.get("host_path") == str(identity.host_path)
            and observed.get("device_identity")
            and writable is True
            and not attached_to_cold(connection, identity)
            and new_chunk_default(connection) == "pg_default"
        ),
        "catalog_location": location,
        "pg_tblspc_target": target,
        "container_bind": optional_string(observed.get("container_bind")),
        "device_identity": optional_string(observed.get("device_identity")),
        "container_writable": writable,
        "hypertable_attached": attached_to_cold(connection, identity),
        "new_chunk_tablespace": new_chunk_default(connection),
    }


def inspect_preconditions(
    config: InstallConfig, deps: InstallDependencies, receipt: dict[str, Any], connection: Any
) -> tuple[ContainerSnapshot, Mapping[str, Any], tuple[str, ...]]:
    identity = config.identity
    path = dict(deps.inspect_path())
    snapshot = normalize_raw_inspect(dict(deps.inspect_container()))
    targets = tuple(sorted(set((*external_pg_tblspc_targets(connection), identity.container_path))))
    health = dict(deps.inspect_health())
    try:
        backup = dict(deps.inspect_backup(targets))
    except TypeError:
        backup = dict(deps.inspect_backup())
    receipt["path"] = path_payload(identity, path)
    receipt["container_snapshot"] = {
        "config_digest": snapshot.config_digest,
        "environment_names": snapshot.public_payload()["environment_names"],
    }
    capacity = assess_install_capacity(
        free_bytes=optional_int(path.get("free_bytes")) or -1,
        install_required_bytes=config.install_required_bytes,
        rollback_headroom_bytes=config.rollback_headroom_bytes,
    )
    receipt["evidence"] = {
        "health": health,
        "backup": backup,
        "capacity": {
            "free_bytes": capacity.free_bytes,
            "install_required_bytes": capacity.install_required_bytes,
            "rollback_headroom_bytes": capacity.rollback_headroom_bytes,
            "required_bytes": capacity.required_bytes,
            "approved": capacity.approved,
            "blockers": list(capacity.blockers),
        },
    }
    blockers: list[str] = []
    decision = path_decision(config, path)
    if not decision.approved:
        if config.enforce and not path_observation(path).exists:
            creatable, path_blockers = missing_path_is_creatable(config, path)
            if not creatable:
                blockers.extend(path_blockers)
        else:
            blockers.extend(decision.blockers)
    for observed, label in ((health, "root storage health is not proven"), (backup, "backup coverage is incomplete")):
        if observed.get("healthy" if observed is health else "complete") is not True:
            values = observed.get("blockers")
            if isinstance(values, list):
                blockers.extend(str(item) for item in values if isinstance(item, str))
            if not values:
                blockers.append(label)
    if not capacity.approved:
        blockers.extend(capacity.blockers)
    if snapshot.name != identity.container_name or not snapshot.image:
        blockers.append("container identity differs from the immutable contract")
    return snapshot, path, tuple(dict.fromkeys(blockers))


def topology_blockers(
    connection: Any,
    deps: InstallDependencies,
    snapshot: ContainerSnapshot,
    identity: ColdTablespaceIdentity,
    receipt: dict[str, Any],
) -> tuple[tuple[str, ...], str]:
    topology, _location, _target = catalog_topology(connection, identity)
    bind = has_cold_bind(snapshot, identity)
    current_refs = tuple(deps.current_bind_references())
    stopped_refs = tuple(deps.stopped_bind_references())
    blockers: list[str] = []
    # Attachment is a catalog topology fact independent of whether the shared
    # tablespace catalog row still exists.  Observe it in every dry-run/enforce
    # pass rather than only after a matching target is found.
    if attached_to_cold(connection, identity):
        blockers.append("business hypertable is attached to the cold tablespace")
    if business_hypertable_default(connection) != "pg_default":
        blockers.append("business hypertable default placement is not proven as pg_default")
    if stopped_refs:
        blockers.append("stopped container has a stale cold bind")
    unexpected_current = tuple(ref for ref in current_refs if not installer_current_bind_reference(identity, ref))
    if unexpected_current:
        blockers.append("another current container has a cold bind")
    if topology == "expected" and bind:
        receipt["readback"] = readback(connection, deps, identity)
        if not receipt["readback"]["approved"]:
            blockers.append("complete topology readback drifted")
    elif topology != "absent" or bind:
        blockers.append("partial or drifted topology is present")
    if topology == "absent" and tuple(deps.pg_tblspc_references()):
        blockers.append("dangling pg_tblspc reference is present")
    if topology == "absent" and tuple(deps.current_bind_references()):
        blockers.append("dangling current container bind is present")
    return tuple(dict.fromkeys(blockers)), topology


def rollback_path_state(
    deps: InstallDependencies,
    *,
    expected_device_identity: str | None,
) -> tuple[bool, bool]:
    """Freshly prove the recorded host path is still the removable object."""

    try:
        observed = dict(deps.inspect_host_path_for_rollback())
    except Exception:
        return False, False
    return (
        bool(
            expected_device_identity
            and observed.get("device_identity") == expected_device_identity
            and observed.get("exists") is True
            and observed.get("is_symlink") is False
            and observed.get("is_directory") is True
        ),
        observed.get("entry_count") == 0,
    )


def reference_blockers(connection: Any, deps: InstallDependencies, identity: ColdTablespaceIdentity) -> tuple[str, ...]:
    topology, _location, _target = catalog_topology(connection, identity)
    blockers: list[str] = []
    if topology != "absent":
        blockers.append("catalog or pg_tblspc still references cold state")
    if tuple(deps.current_bind_references()):
        blockers.append("live container still references host state")
    if tuple(deps.stopped_bind_references()):
        blockers.append("stopped container has a stale host bind")
    if tuple(deps.pg_tblspc_references()):
        blockers.append("pg_tblspc still references host state")
    return tuple(dict.fromkeys(blockers))
