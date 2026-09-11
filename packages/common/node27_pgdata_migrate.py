"""Single manually advanced offline migration owner with durable write boundary.

No action deletes either data directory. An incomplete intent is never a success
predecessor. In particular an unrecorded Docker create is not owned by its name.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg2

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID
from packages.common.node27_cold_tablespace_container import normalize_raw_inspect, serialize_container_argv
from packages.common.node27_pgdata_host import (
    DOCKER,
    ROLE,
    Host,
    MigrationError,
    atomic_private,
    covered_tree,
    digest,
    path_identity,
    private_read,
    require,
)
from packages.common.node27_timeseries_lifecycle_lock import timeseries_lifecycle_lock
from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow, stat_no_follow

DEFAULTS = {
    "source_container": "nhms-db",
    "source_pgdata": "/home/nwm/nhms-pgdata",
    "target_pgdata": "/data/GHDC/nhms-primary/pgdata",
    "reserve_bytes": None,
    "database": "nhms",
    "admin_role": "nhms",
    "drain_timeout": 900,
    "reader_dsn_file": None,
    "writer_dsn_file": None,
    "mdadm_evidence": None,
    "smart_evidence": {},
    "disposable_root": None,
}
PRE_RELEASE = frozenset(
    {
        "prepare_intent",
        "prepared",
        "copy_intent",
        "copy_verified",
        "activate_intent",
        "activated_readonly",
        "rollback_intent",
    }
)
STATES = PRE_RELEASE | {"release_intent", "released", "rolled_back"}
PGDATA = "/home/postgres/pgdata/data"


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _config(overrides: dict[str, Any], frozen: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(DEFAULTS if frozen is None else frozen)
    require(set(overrides) <= set(DEFAULTS), "unknown migration option")
    for key, value in overrides.items():
        if value is None or (key == "smart_evidence" and value == {}):
            continue
        require(frozen is None or value == frozen[key], "option conflicts with frozen workspace identity")
        result[key] = value
    require(
        all(
            isinstance(result[key], str) and result[key]
            for key in ("source_container", "source_pgdata", "target_pgdata", "database", "admin_role")
        )
        and all(
            result[key] is None or isinstance(result[key], str)
            for key in ("reader_dsn_file", "writer_dsn_file", "mdadm_evidence", "disposable_root")
        )
        and (result["reserve_bytes"] is None or type(result["reserve_bytes"]) is int)
        and type(result["drain_timeout"]) is int
        and isinstance(result["smart_evidence"], dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in result["smart_evidence"].items()),
        "migration option shape is invalid",
    )
    return result


def _disposable_identity(workspace: Path, config: dict[str, Any]) -> Path | None:
    if not config.get("disposable_root"):
        return None
    root = Path(config["disposable_root"])
    require(
        root.is_absolute() and re.fullmatch(r"nhms-pgdata-oracle-[0-9a-f]{32}", root.name),
        "disposable root is not uniquely owned",
    )
    require(config["source_container"] == root.name + "-db", "disposable production identity refused")
    for path in (workspace, Path(config["source_pgdata"]), Path(config["target_pgdata"])):
        require(
            path.is_relative_to(root) and path != root and ".." not in path.parts,
            "disposable paths escape owned root",
        )
    identity = path_identity(root)
    require(identity[2] == os.getuid() and identity[4] == 0o700, "disposable root ownership is unsafe")
    return root


def _validate_paths(workspace: Path, config: dict[str, Any], raw: dict[str, Any]) -> None:
    _disposable_identity(workspace, config)
    source, target = (Path(config[key]) for key in ("source_pgdata", "target_pgdata"))
    for path in (workspace, source, target):
        require(
            path.is_absolute()
            and str(path) == os.path.normpath(str(path))
            and not any(char in str(path) for char in "\n\r\x00,: %"),
            "unsafe path spelling",
        )
        require(path != Path("/") and len(path.parts) >= 3, "path is too broad")
    for left, right in ((source, target), (source, workspace), (target, workspace)):
        require(not left.is_relative_to(right) and not right.is_relative_to(left), "migration paths overlap")
    path_identity(source)
    parent = path_identity(target.parent)
    require(parent[2] == os.getuid(), "target parent is not operator-owned")
    snapshot = normalize_raw_inspect(raw)
    require(snapshot.name == config["source_container"], "source name does not match inspected identity")
    require(
        snapshot.resolved_image_id == PINNED_IMAGE_ID, "source is not the exact approved PG15.2/Timescale2.10.2 image"
    )
    require(
        not snapshot.privileged
        and not snapshot.auto_remove
        and not snapshot.volumes_from
        and not snapshot.devices
        and not snapshot.device_requests
        and not snapshot.tmpfs
        and snapshot.network_mode in {"default", "bridge"},
        "unsupported container isolation/topology",
    )
    matches = [item for item in snapshot.binds if item.split(":")[1] == PGDATA]
    require(matches == [f"{source}:{PGDATA}:rw"], "source PGDATA bind is not exact and writable")
    mounts = raw.get("Mounts", [])
    require(len(mounts) == len(snapshot.binds), "anonymous or unrecorded mount exists")
    for mount in mounts:
        require(mount.get("Type") == "bind", "non-bind mount is not supported")
        destination = Path(mount["Destination"])
        require(
            destination == Path(PGDATA)
            or (not destination.is_relative_to(PGDATA) and not Path(PGDATA).is_relative_to(destination)),
            "mount shadows PGDATA or its parent",
        )
        require(
            "nhms_cold" not in str(destination) and "nhms-cold-tablespace" not in mount["Source"],
            "cold identity/bind is incompatible with whole-PGDATA relocation",
        )
        require(
            f"{mount['Source']}:{mount['Destination']}:{'rw' if mount.get('RW') else 'ro'}" in snapshot.binds,
            "effective mount differs from serialized bind",
        )
    # Serialize before any fence to reject an unsupported effective configuration.
    serialize_container_argv(snapshot, name=snapshot.name, environment_file="/private/admission-only", create_only=True)
    for value in snapshot.environment:
        require(
            "\n" not in value and "\r" not in value and "=" in value,
            "environment cannot be represented by a private Docker env file",
        )
    disposable = config.get("disposable_root")
    if disposable:
        root = Path(disposable)
        require(re.fullmatch(r"nhms-pgdata-oracle-[0-9a-f]{32}", root.name), "disposable root is not uniquely owned")
        identity = path_identity(root)
        require(identity[2] == os.getuid() and identity[4] == 0o700, "disposable root ownership is unsafe")
        require(
            all(path.is_relative_to(root) and path != root for path in (source, target, workspace)),
            "disposable paths escape owned root",
        )
        require(snapshot.name == root.name + "-db", "disposable container identity differs")
        require(
            len(snapshot.ports) == 1 and snapshot.ports[0][0] == "5432/tcp" and len(snapshot.ports[0][1]) == 1,
            "disposable port is not isolated",
        )
        host, port = snapshot.ports[0][1][0]
        require(
            host == "127.0.0.1" and port.isdigit() and 1024 < int(port) <= 65535 and int(port) != 55432,
            "disposable port can address production",
        )
        for mount in mounts:
            require(Path(mount["Source"]).is_relative_to(root), "disposable mount escapes owned root")
    else:
        require(
            snapshot.name == "nhms-db"
            and source == Path("/home/nwm/nhms-pgdata")
            and target.is_relative_to("/data/GHDC")
            and target != Path("/data/GHDC"),
            "production source/target identity differs",
        )
    require(type(config["reserve_bytes"]) is int and config["reserve_bytes"] > 0, "reserve must be positive")
    require(
        type(config["drain_timeout"]) is int and 1 <= config["drain_timeout"] <= 86400, "drain timeout is out of bounds"
    )
    require(
        ROLE.fullmatch(config["database"]) and ROLE.fullmatch(config["admin_role"]),
        "database/admin identifier is not representable",
    )


def _credentials(config: dict[str, Any], snapshot) -> dict[str, Any]:
    from psycopg2.extensions import parse_dsn

    result = {}
    for kind in ("reader", "writer"):
        require(config.get(kind + "_dsn_file"), "private reader and writer DSN files are required")
        dsn = private_read(Path(config[kind + "_dsn_file"]), 16384).decode().strip()
        options = parse_dsn(dsn)
        require(
            set(options) <= {"host", "port", "dbname", "user", "password", "sslmode"},
            "business DSN must explicitly bind a single local endpoint",
        )
        require(
            all(options.get(key) for key in ("host", "port", "dbname", "user", "password")),
            "business DSN identity/credential is incomplete",
        )
        require(
            options["host"] == "127.0.0.1"
            and options["dbname"] == config["database"]
            and ROLE.fullmatch(options["user"]),
            "business DSN is not a local database principal",
        )
        bindings = dict(snapshot.ports).get("5432/tcp", ())
        require(
            any(host in {"127.0.0.1", "0.0.0.0", ""} and port == options["port"] for host, port in bindings),
            "business DSN port does not address source",
        )
        if not config.get("disposable_root"):
            require(
                options["user"] == {"reader": "nhms_display_ro", "writer": "nhms_ingest_rw"}[kind],
                "production business principal differs",
            )
        result[kind] = {key: options[key] for key in ("host", "port", "dbname", "user")}
        result[kind]["digest"] = digest(dsn.encode())
    require(
        result["reader"]["user"] != result["writer"]["user"]
        and config["admin_role"] not in {result["reader"]["user"], result["writer"]["user"]},
        "maintenance and business identities must be distinct",
    )
    return result


def _catalog(host: Host, state: dict[str, Any], *, candidate: bool = False) -> dict[str, Any]:
    reader = state["credentials"]["reader"]["user"]
    # Names have passed the bounded identifier grammar; no SQL comes from files.
    sql = f"""WITH reachable AS (
      SELECT oid FROM pg_roles WHERE rolname='{reader}' OR pg_has_role('{reader}',oid,'MEMBER')
    ) SELECT json_build_object(
      'system_id', (SELECT system_identifier::text FROM pg_control_system()),
      'data', current_setting('data_directory'), 'hba', current_setting('hba_file'),
      'config', current_setting('config_file'), 'ident', current_setting('ident_file'),
      'config_sources', (SELECT coalesce(json_agg(DISTINCT sourcefile)
        FILTER (WHERE sourcefile IS NOT NULL), '[]') FROM pg_file_settings),
      'tablespaces', (SELECT json_agg(spcname ORDER BY spcname) FROM pg_tablespace),
      'roles', (SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles WHERE rolcanlogin),
      'admin', (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
      'reader_unsafe', (
        EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role('{reader}',r.oid,'MEMBER')
          AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls))
        OR EXISTS (SELECT 1 FROM reachable r CROSS JOIN pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT LIKE 'pg_temp_%' AND c.relkind IN ('r','p','v','m','f')
          AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
            OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE'))
          AND NOT (c.oid='pg_catalog.pg_settings'::regclass AND c.relkind='v'
            AND EXISTS (SELECT 1 FROM pg_roles owner WHERE owner.oid=c.relowner AND owner.rolsuper)
            AND NOT has_table_privilege(r.oid,c.oid,'INSERT,DELETE,TRUNCATE,TRIGGER')
            AND NOT has_any_column_privilege(r.oid,c.oid,'INSERT')))
        OR EXISTS (SELECT 1 FROM reachable r CROSS JOIN pg_namespace n
          WHERE has_schema_privilege(r.oid,n.oid,'CREATE'))
        OR EXISTS (SELECT 1 FROM reachable r CROSS JOIN pg_database d
          WHERE has_database_privilege(r.oid,d.oid,'CREATE'))
        OR EXISTS (SELECT 1 FROM reachable r CROSS JOIN pg_proc p
          WHERE p.prosecdef AND has_function_privilege(r.oid,p.oid,'EXECUTE')
          AND NOT (p.provolatile IN ('s','i')
            AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='c')
            AND p.probin='$libdir/postgis-3'
            AND EXISTS (SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
              JOIN pg_roles owner ON owner.oid=e.extowner
              WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e'
                AND d.refclassid='pg_extension'::regclass AND e.extname='postgis'
                AND e.extversion='3.3.2' AND owner.rolsuper AND p.proowner=e.extowner)))
      ),
      'sessions', (SELECT coalesce(json_agg(usename), '[]') FROM pg_stat_activity
        WHERE backend_type='client backend' AND pid<>pg_backend_pid()),
      'ssl_files', (SELECT coalesce(json_agg(setting), '[]') FROM pg_settings
        WHERE name IN ('ssl_cert_file','ssl_key_file','ssl_ca_file','ssl_crl_file')
          AND setting<>'' AND current_setting('ssl')='on')
    )"""
    value = host.admin(state, sql, candidate=candidate)
    require(value["admin"] and not value["reader_unsafe"], "admin/readonly role capability proof failed")
    require(
        value["data"] == PGDATA and value["tablespaces"] == ["pg_default", "pg_global"],
        "cluster data directory/tablespaces are not wholly covered",
    )
    paths = [value[key] for key in ("hba", "config", "ident")] + value["config_sources"] + value["ssl_files"]
    for name in paths:
        if not name:
            continue
        path = Path(name)
        if not path.is_absolute():
            path = Path(PGDATA) / path
        require(
            path.is_relative_to(PGDATA) and ".." not in path.parts, "external PostgreSQL configuration is uncovered"
        )
    roles = value["roles"]
    require(
        isinstance(roles, list) and 2 <= len(roles) <= 256 and all(ROLE.fullmatch(role) for role in roles),
        "LOGIN inventory is not bounded/representable",
    )
    require(
        all(state["credentials"][kind]["user"] in roles for kind in ("reader", "writer"))
        and state["config"]["admin_role"] in roles,
        "required LOGIN role is absent",
    )
    return value


def _no_writers(state: dict[str, Any], catalog: dict[str, Any], *, allow_reader: bool = False) -> None:
    allowed = {state["credentials"]["reader"]["user"]} if allow_reader else set()
    require(set(catalog["sessions"]) <= allowed, "unexpected connected client; drain it without killing")


def _hba_overlay(state: dict[str, Any], original: bytes) -> bytes:
    reader = state["credentials"]["reader"]["user"]
    admin = state["config"]["admin_role"]
    rows = ["# PGDATA relocation temporary admission fence " + state["operation"]]
    for role in state["catalog"]["roles"]:
        if role != admin:
            rows.append(f'local all "{role}" reject')
        if role != reader:
            rows.extend(
                (
                    f'host all "{role}" 0.0.0.0/0 reject',
                    f'host all "{role}" ::0/0 reject',
                    f'host replication "{role}" 0.0.0.0/0 reject',
                    f'host replication "{role}" ::0/0 reject',
                )
            )
    # Unknown/new roles are refused by role-inventory gates; no session survives
    # the clean stop. Tail rules/authentication for the verified reader are exact.
    return ("\n".join(rows) + "\n").encode() + original


class Migration:
    def __init__(self, workspace: Path, *, host: Host | None = None):
        self.workspace = workspace
        self.host = host or Host()
        self.state: dict[str, Any] | None = None

    def workspace_identity(self, config: dict[str, Any]) -> list[int]:
        if _disposable_identity(self.workspace, config) is None:
            return self.host.durable_workspace(self.workspace)
        identity = path_identity(self.workspace)
        require(identity[2] == os.getuid() and identity[4] == 0o700, "disposable workspace is not private")
        return identity

    def load(self, overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        path = self.workspace / "state.json"
        if not path.exists() and not path.is_symlink():
            self.workspace_identity(_config(overrides or {}))
            return None
        value = json.loads(private_read(path, 4 * 1024 * 1024))
        require(
            isinstance(value, dict)
            and isinstance(value.get("config"), dict)
            and set(value["config"]) == set(DEFAULTS)
            and isinstance(value.get("stage"), str)
            and isinstance(value.get("operation"), str),
            "private state shape is invalid",
        )
        require(
            value.get("schema_version") == 1
            and value.get("stage") in STATES
            and type(value.get("writes_released")) is bool
            and value.get("workspace") == str(self.workspace),
            "private state contract is invalid",
        )
        self.workspace_identity(_config(overrides or {}, value["config"]))
        require(value.get("workspace_identity") == path_identity(self.workspace), "workspace identity drifted")
        require(re.fullmatch(r"[0-9a-f]{32}", value.get("operation", "")), "operation identity is invalid")
        self.state = value
        return value

    def save(self, stage: str | None = None) -> None:
        state = self.state
        require(state is not None, "no admitted operation")
        require(path_identity(self.workspace) == state["workspace_identity"], "workspace identity changed")
        path = self.workspace / "state.json"
        if path.exists():
            previous = json.loads(private_read(path, 4 * 1024 * 1024))
            require(previous["operation"] == state["operation"], "operation authority changed")
            require(not previous["writes_released"] or state["writes_released"], "writes_released is monotonic")
        if stage:
            require(stage in STATES, "invalid transition result")
            state["stage"] = stage
        atomic_private(path, _json(state))

    def plan(self, overrides: dict[str, Any]) -> dict[str, Any]:
        # No lifecycle lock, workspace creation, helper container, unit mutation,
        # SQL connection or journal write occurs on the default path.
        state = self.load(overrides) if self.workspace.exists() else None
        config = _config(overrides, None if state is None else state["config"])
        _disposable_identity(self.workspace, config)
        observations: dict[str, Any] = {
            "action": "plan",
            "mutation": False,
            "stage": None if state is None else state["stage"],
            "writes_released": False if state is None else state["writes_released"],
            "scope": "isolated-mechanics-only" if config["disposable_root"] else "production-planning",
            "blockers": [],
        }
        try:
            raw = self.host.inspect(config["source_container"] if state is None else state["original_id"])
            observations["source_running"] = raw["State"]["Running"]
            observations["resolved_image"] = raw["Image"]
            if state is None:
                _validate_paths(self.workspace, config, raw)
                if self.workspace.exists():
                    self.workspace_identity(config)
                self.host.units_snapshot(config)
            observations["health"] = self.host.health(config)
            observations["target_free_bytes"] = shutil.disk_usage(Path(config["target_pgdata"]).parent).free
            require(
                config.get("reader_dsn_file") and config.get("writer_dsn_file"),
                "private business connection files are required for prepare",
            )
        except (MigrationError, OSError, ValueError, SafeFilesystemError, psycopg2.Error) as error:
            observations["blockers"].append(
                str(error) if isinstance(error, MigrationError) else "observation unavailable; no mutation performed"
            )
        return observations

    def run(
        self, action: str = "plan", *, enforce: bool = False, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        overrides = overrides or {}
        if action == "plan":
            return self.plan(overrides)
        require(action in {"prepare", "copy", "activate", "rollback", "release"}, "unknown action")
        require(enforce, "mutating action requires --enforce")
        initial = self.load(overrides)
        initial_config = _config(overrides, None if initial is None else initial["config"])
        root = _disposable_identity(self.workspace, initial_config)
        lock = timeseries_lifecycle_lock() if root is None else timeseries_lifecycle_lock(root / "lifecycle.lock")
        with lock:
            state = self.load(overrides)
            require(state == initial, "operation state changed before lifecycle lock")
            config = _config(overrides, None if state is None else state["config"])
            if action == "prepare":
                require(state is None, "prepare requires a new workspace; interrupted intent cannot replay")
                self.prepare(config)
            else:
                require(state is not None, "action requires durable operation state")
                stage = state["stage"]
                allowed = (
                    (action == "copy" and stage == "prepared" and not state["writes_released"])
                    or (action == "activate" and stage == "copy_verified" and not state["writes_released"])
                    or (action == "rollback" and stage in PRE_RELEASE and not state["writes_released"])
                    or (
                        action == "release"
                        and (
                            (stage == "activated_readonly" and not state["writes_released"])
                            or (stage == "release_intent" and state["writes_released"])
                        )
                    )
                )
                require(allowed, "state/action pair refused; marked release is forward-only")
                getattr(self, action)()
            state = self.state
            return {
                "action": action,
                "stage": state["stage"],
                "operation": state["operation"],
                "writes_released": state["writes_released"],
                "scope": "isolated-mechanics-only" if config["disposable_root"] else "migration-mechanics",
                "old_copy_retained": True,
                "production_rollout_approved": False,
            }

    def prepare(self, config: dict[str, Any]) -> None:
        host = self.host
        raw = host.inspect(config["source_container"])
        _validate_paths(self.workspace, config, raw)
        source, target = Path(config["source_pgdata"]), Path(config["target_pgdata"])
        require(
            not target.exists() and not target.is_symlink(), "target must be absent; existing/partial data is retained"
        )
        snapshot = normalize_raw_inspect(raw)
        require(snapshot.running is True, "new preparation requires the running original")
        covered_tree(source)
        # Exact observed PG process account, not an image-user convention.
        numeric = host.command([DOCKER, "exec", raw["Id"], "stat", "-c", "%u:%g", PGDATA]).stdout.strip()
        require(re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", numeric), "numeric PostgreSQL ownership is unproved")
        require(snapshot.user == numeric, "Docker runtime account differs from PGDATA ownership")
        identity = path_identity(source)
        require(numeric == f"{identity[2]}:{identity[3]}", "host/container numeric ownership differs")
        state = {
            "schema_version": 1,
            "stage": "prepare_intent",
            "writes_released": False,
            "operation": uuid.uuid4().hex,
            "workspace": str(self.workspace),
            "workspace_identity": self.workspace_identity(config),
            "config": config,
            "original": raw,
            "original_id": raw["Id"],
            "original_config_digest": snapshot.config_digest,
            "image": snapshot.resolved_image_id,
            "numeric_user": numeric,
            "source_identity": identity,
            "target_parent_identity": path_identity(target.parent),
            "credentials": _credentials(config, snapshot),
            "units": host.units_snapshot(config),
            "host_identity": host.identity(),
            "health": host.health(config),
            "candidate_id": None,
            "display_unfenced": False,
        }
        state["backup_name"] = snapshot.name + "-pgdata-original-" + state["operation"]
        require(host.inspect(state["backup_name"], absent_ok=True) is None, "backup name is occupied")
        catalog = _catalog(host, state)
        # Authenticate the actual writer before the stop so an arbitrary bad
        # password cannot masquerade as the later admission rejection proof.
        with closing(host.connection(state, "writer")) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user, current_database()")
                require(
                    cursor.fetchone() == (state["credentials"]["writer"]["user"], config["database"]),
                    "writer credential identity differs",
                )
        host.read_proof(state)
        sizes = sum(
            info.st_size
            for root, _, names in os.walk(source, followlinks=False)
            for name in names
            for info in [os.stat(Path(root) / name, follow_symlinks=False)]
        )
        require(
            shutil.disk_usage(target.parent).free >= sizes + config["reserve_bytes"],
            "target capacity plus reserve is insufficient",
        )
        state["source_bytes"] = sizes
        state["catalog"] = catalog
        self.state = state
        self.save()  # All admission precedes the first fence/stop side effect.
        host.install_fences(state)
        # Freeze LOGIN inventory in this final quiescent *running* read phase.
        state["catalog"] = _catalog(host, state)
        _no_writers(state, state["catalog"])
        state["baseline"] = host.read_proof(state)
        self.save()
        state["original_restart_disable_intent"] = True
        self.save()
        host.command([DOCKER, "update", "--restart=no", state["original_id"]])
        require(self.original(stopped=False).restart_policy == ("no", 0), "original restart was not disabled")
        # Wait outside the container: its shutdown kills an exec'ed pg_ctl waiter.
        # An unlimited daemon grace period prevents escalation to SIGKILL.
        host.command(
            [DOCKER, "stop", "--signal", "SIGINT", "--timeout", "-1", state["original_id"]],
            timeout=150,
        )
        deadline = time.monotonic() + 30
        while host.inspect(state["original_id"])["State"]["Running"]:
            require(time.monotonic() < deadline, "source did not stop cleanly; retained for recovery")
            time.sleep(1)
        self.original(stopped=True)
        state["control"] = host.control(state)
        require(state["control"] == state["catalog"]["system_id"], "stopped control identity differs")
        covered_tree(source)
        state["source_tree"] = host.fingerprint(state)
        self.save("prepared")

    def original(self, *, stopped: bool = True, rollback: bool = False):
        state = self.state
        raw = self.host.inspect(state["original_id"])
        snapshot = normalize_raw_inspect(raw)
        original = normalize_raw_inspect(state["original"])
        allowed = set()
        if state.get("original_restart_disable_intent"):
            allowed.add(replace(original, restart_policy=("no", 0)).config_digest)
        if state["stage"] == "prepare_intent" or (
            rollback and state["stage"] == "rollback_intent" and state.get("original_policy_restore_intent")
        ):
            allowed.add(original.config_digest)
        require(
            snapshot.container_id == state["original_id"] and snapshot.config_digest in allowed,
            "original Docker ID/configuration drifted; recovery required",
        )
        names = {original.name}
        if state.get("original_rename_intent"):
            names.add(state["backup_name"])
        if state.get("candidate_id") and not state.get("original_restore_name_intent"):
            names = {state["backup_name"]}
        require(snapshot.name in names, "original name is outside recorded identities")
        require(not stopped or snapshot.running is False, "frozen source is running")
        require(
            path_identity(Path(state["config"]["source_pgdata"])) == state["source_identity"],
            "original data path identity drifted",
        )
        return snapshot

    def common(self, *, fences: bool = True) -> None:
        state = self.state
        self.host.verify_units(state, required_fences=fences)
        require(self.host.identity() == state["host_identity"], "host observation changed")
        require(
            _credentials(state["config"], normalize_raw_inspect(state["original"])) == state["credentials"],
            "business credential files changed",
        )
        require(
            path_identity(Path(state["config"]["target_pgdata"]).parent) == state["target_parent_identity"],
            "target parent identity drifted",
        )
        helpers = self.host.command(
            [DOCKER, "ps", "-q", "--filter", "label=nhms.pgdata.operation=" + state["operation"]]
        ).stdout.strip()
        require(not helpers, "operation helper still running; preserve state and wait for recovery")

    def source_proof(self, *, rollback: bool = False) -> None:
        state = self.state
        self.original(rollback=rollback)
        require(self.host.control(state) == state["control"], "stopped control identity changed")
        require(self.host.fingerprint(state) == state["source_tree"], "stopped source tree changed")

    def copy(self) -> None:
        state = self.state
        self.common()
        self.source_proof()
        state["health"] = self.host.health(state["config"])
        target = Path(state["config"]["target_pgdata"])
        require(not target.exists() and not target.is_symlink(), "partial/foreign target exists; copy cannot replay")
        require(
            shutil.disk_usage(target.parent).free >= state["source_bytes"] + state["config"]["reserve_bytes"],
            "copy capacity plus reserve is insufficient",
        )
        self.save("copy_intent")
        parent_fd = open_directory_no_follow(target.parent)
        try:
            os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        state["target_identity"] = path_identity(target)
        self.save()
        uid, gid, mode = state["source_identity"][2:]
        self.host.helper(
            state,
            f"cp -a /source/. /destination/; chown {uid}:{gid} /destination; "
            f"chmod {mode:o} /destination; sync -f /destination",
            timeout=86400,
        )
        # fields may change after creating the operation-owned destination.
        after = path_identity(target)
        require(
            after[:2] == state["target_identity"][:2] and after[2:] == state["source_identity"][2:],
            "copied directory metadata/identity differs",
        )
        state["target_identity"] = after
        self.save()
        covered_tree(target)
        target_hash = self.host.fingerprint(state, target=True)
        require(
            target_hash == state["source_tree"] and self.host.fingerprint(state) == target_hash,
            "whole-tree copy proof differs; partial data retained",
        )
        state["copy_tree"] = target_hash
        self.save("copy_verified")

    def expected_candidate(self):
        state = self.state
        original = normalize_raw_inspect(state["original"])
        old_bind = f"{state['config']['source_pgdata']}:{PGDATA}:rw"
        new_bind = f"{state['config']['target_pgdata']}:{PGDATA}:rw"
        return replace(
            original,
            image=original.resolved_image_id,
            restart_policy=("no", 0),
            binds=tuple(sorted(new_bind if item == old_bind else item for item in original.binds)),
        )

    def candidate(self, *, running: bool | None = True):
        state = self.state
        require(state.get("candidate_id"), "candidate Docker ID was not durably recorded; recovery required")
        raw = self.host.inspect(state["candidate_id"])
        snapshot = normalize_raw_inspect(raw)
        expected = self.expected_candidate()
        names = {expected.name}
        policies = {expected.config_digest}
        if state["stage"] == "rollback_intent" and state.get("rejected_name"):
            names.add(state["rejected_name"])
        if state["stage"] == "release_intent" and state.get("candidate_policy_restore_intent"):
            policies.add(
                replace(expected, restart_policy=normalize_raw_inspect(state["original"]).restart_policy).config_digest
            )
        require(
            snapshot.container_id == state["candidate_id"]
            and snapshot.name in names
            and snapshot.config_digest in policies,
            "candidate Docker ID/configuration differs",
        )
        require(running is None or snapshot.running == running, "candidate running state differs")
        require(
            path_identity(Path(state["config"]["target_pgdata"])) == state["target_identity"],
            "candidate data path identity drifted",
        )
        return snapshot

    def hba_bytes(self) -> bytes:
        state = self.state
        relative = state["hba_relative"]
        value = self.host.helper(state, f"base64 -w0 /source/{relative}", target=True)
        return base64.b64decode(value, validate=True)

    def set_hba(self, *, restore: bool) -> None:
        state = self.state
        original = private_read(self.workspace / "original-hba")
        overlay = private_read(self.workspace / "fenced-hba")
        require(
            digest(original) == state["hba_original_digest"] and digest(overlay) == state["hba_owned_digest"],
            "private HBA evidence drifted",
        )
        current = self.hba_bytes()
        expected = original if restore else overlay
        require(current in (original, overlay), "HBA is neither original nor operation-owned; recovery required")
        require(not restore or state["writes_released"], "HBA restoration requires durable write-release marker")
        if current == expected:
            return
        payload = self.workspace / ("original-hba" if restore else "fenced-hba")
        relative = state["hba_relative"]
        uid, gid, mode = state["hba_metadata"]
        self.host.helper(
            state,
            f"cp /payload /destination/{relative}.relocation-{state['operation']}; "
            f"chown {uid}:{gid} /destination/{relative}.relocation-{state['operation']}; "
            f"chmod {mode:o} /destination/{relative}.relocation-{state['operation']}; "
            f"sync -f /destination/{relative}.relocation-{state['operation']}; "
            f"mv -T /destination/{relative}.relocation-{state['operation']} /destination/{relative}; "
            "sync -f /destination",
            payload=payload,
        )
        require(self.hba_bytes() == expected, "HBA write proof failed")

    def readonly_proof(self) -> None:
        state = self.state
        self.candidate()
        require(digest(self.hba_bytes()) == state["hba_owned_digest"], "candidate admission fence changed")
        catalog = _catalog(self.host, state, candidate=True)
        require(
            catalog["system_id"] == state["control"]
            and catalog["roles"] == state["catalog"]["roles"]
            and catalog["hba"] == state["catalog"]["hba"],
            "candidate catalog identity drifted",
        )
        _no_writers(state, catalog, allow_reader=True)
        self.host.read_proof(state)
        self.host.writer_rejected(state)
        errors = self.host.admin(
            state,
            "SELECT json_build_object('errors',count(*)) FROM pg_hba_file_rules WHERE error IS NOT NULL",
            candidate=True,
        )
        require(errors["errors"] == 0, "candidate HBA contains invalid rules")

    def activate(self) -> None:
        state = self.state
        self.common()
        self.source_proof()
        self.host.health(state["config"])
        require(self.host.fingerprint(state, target=True) == state["copy_tree"], "verified target changed")
        original = self.original()
        require(
            original.name == state["config"]["source_container"]
            and self.host.inspect(state["backup_name"], absent_ok=True) is None,
            "original/backup name ownership changed",
        )
        relative = str(Path(state["catalog"]["hba"]).relative_to(PGDATA))
        require(
            re.fullmatch(r"[A-Za-z0-9_./-]+", relative) and ".." not in Path(relative).parts,
            "HBA path cannot be represented safely",
        )
        path = Path(state["config"]["target_pgdata"]) / relative
        info = stat_no_follow(path)
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 256 * 1024,
            "HBA is not a bounded, covered regular file",
        )
        state["hba_relative"] = relative
        state["hba_metadata"] = [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
        original_hba = self.hba_bytes()
        overlay = _hba_overlay(state, original_hba)
        state["hba_original_digest"] = digest(original_hba)
        state["hba_owned_digest"] = digest(overlay)
        atomic_private(self.workspace / "original-hba", original_hba)
        atomic_private(self.workspace / "fenced-hba", overlay)
        atomic_private(self.workspace / "container.env", ("\n".join(original.environment) + "\n").encode())
        self.save("activate_intent")
        self.set_hba(restore=False)
        state["original_rename_intent"] = True
        self.save()
        self.host.command([DOCKER, "rename", state["original_id"], state["backup_name"]])
        self.original()
        require(self.host.inspect(original.name, absent_ok=True) is None, "candidate name is occupied; not owned")
        argv = serialize_container_argv(
            self.expected_candidate(),
            name=original.name,
            environment_file=str(self.workspace / "container.env"),
            create_only=True,
        )
        candidate_id = self.host.command(argv).stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{64}", candidate_id), "create result did not prove a Docker ID")
        state["candidate_id"] = candidate_id
        self.save()  # A crash before this record makes the created container unowned.
        self.candidate(running=False)
        self.host.command([DOCKER, "start", candidate_id])
        deadline = time.monotonic() + 120
        while True:
            try:
                self.readonly_proof()
                break
            except (MigrationError, psycopg2.OperationalError):
                require(time.monotonic() < deadline, "candidate readiness failed; pre-write rollback remains available")
                time.sleep(1)
        state["display_unfenced"] = True
        self.save()  # Intent allows recovery to reconcile either display-fence state.
        self.host.restore_units(state, display_only=True)
        self.readonly_proof()
        self.save("activated_readonly")

    def rollback(self) -> None:
        state = self.state
        require(not state["writes_released"], "old snapshot is stale; rollback forbidden")
        self.common(fences=False)
        original = self.original(stopped=False, rollback=True)
        named = self.host.inspect(state["config"]["source_container"], absent_ok=True)
        if named is not None:
            require(
                named["Id"] in {state["original_id"], state.get("candidate_id")},
                "unrecorded candidate is preserved; manual recovery required",
            )
        if state.get("candidate_id"):
            candidate = self.candidate(running=None)
            require(not (original.running and candidate.running), "two-primary risk")
            require(
                not original.running or state.get("original_start_intent"),
                "original unexpectedly running",
            )
        if state.get("control") and not original.running and not state.get("original_start_intent"):
            self.source_proof(rollback=True)
        state["original_policy_restore_intent"] = True
        self.save("rollback_intent")
        if state.get("candidate_id"):
            # Re-fence display before stopping an owned candidate; business units
            # were never unfenced. Do not kill any running oneshot.
            state["display_unfenced"] = False
            self.save()
            self.host.install_fences(state)
            candidate_raw = self.host.inspect(state["candidate_id"])
            if candidate_raw["State"]["Running"]:
                self.host.command(
                    [DOCKER, "stop", "--signal", "SIGINT", "--timeout", "-1", state["candidate_id"]], timeout=180
                )
            # Preserve the stopped candidate container too; rename rather than
            # remove it. This makes interrupted rollback forward-reconcilable.
            rejected_name = state["config"]["source_container"] + "-pgdata-rejected-" + state["operation"]
            state["rejected_name"] = rejected_name
            self.save()
            if self.candidate(running=False).name != rejected_name:
                self.host.command([DOCKER, "rename", state["candidate_id"], rejected_name])
        if original.name != state["config"]["source_container"]:
            require(
                self.host.inspect(state["config"]["source_container"], absent_ok=True) is None,
                "original name occupied after candidate stop",
            )
            state["original_restore_name_intent"] = True
            self.save()
            self.host.command([DOCKER, "rename", state["original_id"], state["config"]["source_container"]])
        restart, retries = normalize_raw_inspect(state["original"]).restart_policy
        restart = f"on-failure:{retries}" if restart == "on-failure" else restart or "no"
        state["original_policy_restore_intent"] = True
        self.save()
        if original.restart_policy != normalize_raw_inspect(state["original"]).restart_policy:
            self.host.command([DOCKER, "update", "--restart=" + restart, state["original_id"]])
        if not original.running:
            state["original_start_intent"] = True
            self.save()
            self.host.command([DOCKER, "start", state["original_id"]])
        restored = self.original(stopped=False, rollback=True)
        require(
            restored.running and restored.config_digest == state["original_config_digest"],
            "exact original restoration failed",
        )
        deadline = time.monotonic() + 120
        while True:
            try:
                catalog = _catalog(self.host, state)
                require(
                    catalog["system_id"] == state["catalog"]["system_id"],
                    "restored database identity differs",
                )
                self.host.read_proof(state)
                break
            except (MigrationError, psycopg2.OperationalError):
                require(time.monotonic() < deadline, "original database readiness failed; writers remain fenced")
                time.sleep(1)
        state["display_unfenced"] = True
        self.save()
        self.host.restore_units(state, display_only=True)
        self.host.display_ready(state)
        self.host.restore_units(state)
        self.save("rolled_back")

    def release(self) -> None:
        state = self.state
        self.common(fences=not state["writes_released"])
        self.original()
        self.candidate()
        if not state["writes_released"]:
            self.readonly_proof()
            self.host.release_callers_ready(state)
            state["writes_released"] = True
            self.save("release_intent")  # Sole durable stale-snapshot boundary.
        # A marked retry accepts original OR owned HBA, but never a third state.
        self.set_hba(restore=True)
        value = self.host.admin(state, "SELECT to_json(pg_reload_conf())", candidate=True)
        require(value is True, "candidate HBA reload failed; release must continue forward")
        restart, retries = normalize_raw_inspect(state["original"]).restart_policy
        restart = f"on-failure:{retries}" if restart == "on-failure" else restart or "no"
        state["candidate_policy_restore_intent"] = True
        self.save()
        if self.candidate().restart_policy != normalize_raw_inspect(state["original"]).restart_policy:
            self.host.command([DOCKER, "update", "--restart=" + restart, state["candidate_id"]])
        self.host.restore_units(state)
        self.save("released")
