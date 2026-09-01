"""Installer orchestration tests at the CLI-facing fake boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common.node27_cold_tablespace_container import normalize_raw_inspect
from packages.common.node27_cold_tablespace_install import (
    COLD_CONTAINER_PATH,
    COLD_HOST_PATH,
    InstallConfig,
    InstallDependencies,
    run_install,
)
from scripts import node27_cold_tablespace_install as installer_cli

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
SHA = "a" * 40


def test_cli_uses_the_public_installer_config_and_state_machine() -> None:
    assert installer_cli.InstallConfig is InstallConfig
    assert installer_cli.run_install is run_install


class FakeConnection:
    def __init__(self, *, topology: str = "absent", writable: bool = True) -> None:
        self.topology = topology
        self.writable = writable
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def execute(self, sql: str, params: object = None) -> list[dict]:
        self.calls.append((sql, params))
        if "FROM pg_tablespace AS space" in sql:
            if self.topology == "absent":
                return []
            target = COLD_CONTAINER_PATH if self.topology in {"ready", "expected"} else "/wrong/target"
            return [{"target": target}]
        if "FROM pg_tablespace WHERE pg_tablespace_location" in sql:
            return []
        if "FROM pg_tablespace" in sql:
            if self.topology == "absent":
                return []
            location = COLD_CONTAINER_PATH if self.topology in {"ready", "expected"} else "/wrong/location"
            return [{"location": location}]
        if "FROM _timescaledb_catalog.tablespace AS space" in sql:
            return []
        if "bool_and(c.reltablespace = 0)" in sql:
            return [{"tablespace": "pg_default"}]
        if sql.startswith("CREATE TABLESPACE"):
            self.topology = "expected"
            return []
        if sql.startswith("DROP TABLESPACE"):
            self.topology = "absent"
            return []
        return []

    def close(self) -> None:
        self.closed = True


def _path_observation() -> dict:
    return {
        "exists": True,
        "is_symlink": False,
        "is_directory": True,
        "entry_count": 0,
        "uid": 999,
        "gid": 999,
        "mode": 0o700,
        "mount_device": "8:11",
        "device_identity": "8:11:1",
        "free_bytes": 1_000_000,
    }


def _health() -> dict:
    return {
        "healthy": True,
        "members": ["/dev/sdb1", "/dev/sdc1"],
        "raid": {"file_identity": {"sha256": "b" * 64}, "state": "clean", "captured_at": "2026-08-31T12:00:00Z"},
        "smart": [
            {"device": "/dev/sdb1", "status": "PASS", "file_identity": {"sha256": "c" * 64}},
            {"device": "/dev/sdc1", "status": "PASS", "file_identity": {"sha256": "d" * 64}},
        ],
        "blockers": [],
    }


def _backup(*, complete: bool = True) -> dict:
    return {
        "complete": complete,
        "covered_paths": ["/home/synthetic/pgdata", COLD_CONTAINER_PATH] if complete else ["/home/synthetic/pgdata"],
        "missing_targets": [] if complete else [COLD_CONTAINER_PATH],
        "file_identity": {"sha256": "e" * 64},
        "blockers": [] if complete else ["backup inventory omits cold target"],
    }


def _inspect(
    *,
    cold_bind: bool = False,
    name: str = "nhms-db",
    container_id: str = "sha256:old",
    running: bool = True,
) -> dict:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Image": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
        "State": {"Running": running},
        "Config": {
            "Image": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            "Env": ["POSTGRES_PASSWORD=do-not-leak", "POSTGRES_USER=nhms"],
            "Cmd": ["postgres"],
            "Entrypoint": None,
            "WorkingDir": "/",
            "User": "999:999",
            "Labels": {},
            "StopSignal": "SIGINT",
            "Healthcheck": None,
        },
        "HostConfig": {
            "Binds": [
                "/home/synthetic/pgdata:/home/postgres/pgdata/data:rw",
                *([f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw"] if cold_bind else []),
            ],
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NanoCpus": 0,
            "Memory": 0,
            "ShmSize": 0,
            "StopTimeout": 300,
            "ReadonlyRootfs": False,
            "CapAdd": [],
            "CapDrop": [],
            "SecurityOpt": [],
            "NetworkMode": "bridge",
            "Privileged": False,
            "PublishAllPorts": False,
            "AutoRemove": False,
            "VolumesFrom": [],
            "Devices": [],
            "DeviceRequests": [],
            "Tmpfs": {},
            "ExtraHosts": [],
        },
        "Mounts": [
            *(
                [{"Type": "bind", "Source": COLD_HOST_PATH, "Destination": COLD_CONTAINER_PATH, "RW": True}]
                if cold_bind
                else []
            ),
        ],
    }


def _dependencies(connection: FakeConnection, *, cold_bind: bool = False) -> InstallDependencies:
    actions: list[tuple[str, tuple[str, ...]]] = []
    state = {"current": "nhms-db", "prior": None, "cold_bind": cold_bind, "next_id": 1, "running": True}

    def inspect(name: str) -> dict:
        if name == state["current"]:
            return _inspect(
                cold_bind=bool(state["cold_bind"]),
                name=name,
                container_id=f"sha256:current-{state['next_id']}",
                running=bool(state["running"]),
            )
        if name == state["prior"]:
            return _inspect(cold_bind=False, name=name, container_id="sha256:old", running=False)
        raise RuntimeError(f"missing synthetic container {name}")

    def inspect_optional(name: str) -> dict | None:
        if name in {state["current"], state["prior"]} and name is not None:
            return inspect(name)
        return None

    def docker(argv: tuple[str, ...]) -> dict:
        actions.append(("docker", argv))
        command = argv[1]
        if command == "stop":
            state["running"] = False
            return {"returncode": 0}
        if command == "start":
            state["running"] = True
            return {"returncode": 0}
        if command == "rename":
            source, target = argv[2:4]
            if source == state["current"]:
                state["current"] = None
                state["prior"] = target
            elif source == state["prior"]:
                state["prior"] = None
                state["current"] = target
            return {"returncode": 0}
        if command == "run":
            state["current"] = "nhms-db"
            state["cold_bind"] = True
            state["running"] = True
            state["next_id"] += 1
            return {"returncode": 0}
        if command == "rm":
            state["current"] = None
            state["cold_bind"] = False
            return {"returncode": 0}
        return {"returncode": 0}

    def target() -> dict:
        return {
            "container_name": "nhms-db",
            "container_bind": COLD_HOST_PATH,
            "host_path": COLD_HOST_PATH,
            "device_identity": "8:11:1",
            "writable": True,
            "host_mode": 0o700,
            "host_uid": 999,
            "host_gid": 999,
        }

    dependencies = InstallDependencies(
        inspect_path=lambda: _path_observation(),
        inspect_health=lambda: _health(),
        inspect_backup=lambda *_targets: _backup(),
        inspect_container=lambda: inspect("nhms-db"),
        inspect_named_container=inspect,
        inspect_named_container_optional=inspect_optional,
        docker=docker,
        connect=lambda: connection,
        inspect_target=target,
        current_bind_references=lambda: (),
        stopped_bind_references=lambda: (),
        pg_tblspc_references=lambda: (),
        catalog_dependents=lambda: 0,
        inspect_host_path_for_rollback=lambda: _path_observation(),
        inspect_quiescence=lambda: {
            "units": {
                unit: {"active_state": "inactive", "sub_state": "dead", "result": "success"}
                for unit in (
                    "nhms-node27-autopipe.service",
                    "nhms-node27-autopipe.timer",
                    "nhms-node27-timeseries-compression.service",
                    "nhms-node27-timeseries-compression.timer",
                    "nhms-node27-timeseries-retention.service",
                    "nhms-node27-timeseries-retention.timer",
                )
            }
        },
        wait_ready=lambda: None,
        now=lambda: NOW,
        action_log=actions,
    )
    dependencies.synthetic_container_state = state  # type: ignore[attr-defined]
    return dependencies


def _config(tmp_path: Path, *, enforce: bool = False) -> InstallConfig:
    return InstallConfig(
        enforce=enforce,
        receipt_path=tmp_path / "installer-receipt.json",
        recovery_path=tmp_path / "installer-recovery.json",
        head_sha=SHA,
        expected_uid=999,
        expected_gid=999,
        expected_mode=0o700,
        expected_device_identity="8:11:1",
        install_required_bytes=100,
        rollback_headroom_bytes=200,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dry_run_default_never_connects_or_mutates_and_publishes_schema_valid_receipt(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path)

    result = run_install(config, deps)

    assert result.outcome == "dry_run"
    assert connection.calls  # read-only catalog/topology observation is mandatory
    assert not any(sql.startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in connection.calls)
    assert deps.action_log == []
    receipt = _read(config.receipt_path)
    assert receipt["outcome"] == "dry_run"
    assert stat_mode(config.receipt_path) == 0o600
    jsonschema.validate(receipt, result.schema)


def test_already_ready_topology_is_no_write_even_in_enforce_mode(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "already_ready", result.receipt
    assert not deps.action_log
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)
    assert not config.recovery_path.exists()


def test_already_ready_allows_postgres_version_subtree_with_no_write(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    resident = _path_observation()
    resident["entry_count"] = 1
    deps.inspect_path = lambda: resident
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "already_ready", result.receipt
    assert result.receipt["path"]["empty"] is False
    jsonschema.validate(_read(config.receipt_path), result.schema)
    assert not deps.action_log
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)
    assert not config.recovery_path.exists()


@pytest.mark.parametrize(
    "deviation",
    (
        {"uid": 998},
        {"gid": 998},
        {"mode": 0o755},
        {"device_identity": "8:11:2"},
        {"is_symlink": True, "is_directory": False},
        {"entry_count": None},
    ),
)
def test_already_ready_refuses_nonempty_resident_path_with_any_gate_violation(
    tmp_path: Path, deviation: dict[str, object]
) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    resident = _path_observation()
    resident["entry_count"] = 1
    resident.update(deviation)
    deps.inspect_path = lambda: resident
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert not deps.action_log


def test_absent_topology_with_nonempty_path_is_rejected_as_fresh(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    resident = _path_observation()
    resident["entry_count"] = 1
    deps.inspect_path = lambda: resident
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert any("empty" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log


def test_partial_topology_with_nonempty_path_is_no_go_without_mutation(tmp_path: Path) -> None:
    connection = FakeConnection(topology="drifted")
    deps = _dependencies(connection, cold_bind=True)
    resident = _path_observation()
    resident["entry_count"] = 1
    deps.inspect_path = lambda: resident
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert any("topology" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log


def test_dry_run_complete_resident_topology_is_no_write_already_ready(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    resident = _path_observation()
    resident["entry_count"] = 1
    deps.inspect_path = lambda: resident
    config = _config(tmp_path, enforce=False)

    result = run_install(config, deps)

    assert result.outcome == "already_ready", result.receipt
    assert result.receipt["path"]["empty"] is False
    assert not deps.action_log


def test_enforce_discovers_existing_external_tablespaces_before_accepting_backup_scope(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    observed_scopes: list[tuple[str, ...]] = []

    def backup(targets: tuple[str, ...] = ()) -> dict:
        observed_scopes.append(targets)
        return _backup()

    deps.inspect_backup = backup
    result = run_install(_config(tmp_path, enforce=True), deps)

    assert result.outcome == "installed"
    assert observed_scopes == [(COLD_CONTAINER_PATH,)]
    assert any("pg_tablespace_location(oid) <> ''" in sql for sql, _params in connection.calls)


def test_partial_or_drifted_topology_is_no_go_without_repair(tmp_path: Path) -> None:
    connection = FakeConnection(topology="drifted")
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    assert not deps.action_log
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)
    assert "topology" in " ".join(result.receipt["blockers"])


def test_enforce_can_create_only_an_absent_pinned_fresh_host_path(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    missing = _path_observation()
    missing.update(
        {"exists": False, "is_directory": False, "entry_count": None, "uid": None, "gid": None, "mode": None}
    )
    created = _path_observation()
    deps.inspect_path = lambda: missing
    deps.ensure_host_path = lambda: created

    result = run_install(_config(tmp_path, enforce=True), deps)

    assert result.outcome == "installed"
    assert result.receipt["ownership"]["host_path_created"] is True


def test_enforce_refuses_to_proceed_after_recreate_without_sql_readiness_boundary(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.wait_ready = None
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert result.receipt["authority"]["state"] == "sidecar"
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)
    assert any(argv[1] == "run" for _kind, argv in deps.action_log)
    assert config.recovery_path.exists()


def test_post_recreate_config_drift_rolls_back_before_ddl(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    calls = 0

    def inspect() -> dict:
        nonlocal calls
        calls += 1
        # The first observation is the original prior.  Only replacement
        # readback drifts; rollback then uses the stateful named-prior fixture.
        raw = _inspect(cold_bind=calls == 2)
        if calls == 2:
            raw["HostConfig"]["Memory"] = 1
        return raw

    deps.inspect_container = inspect
    result = run_install(_config(tmp_path, enforce=True), deps)

    assert result.outcome == "rollback"
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)
    assert any(argv[1:3] == ("rename", "nhms-db-before") for _kind, argv in deps.action_log)


def test_production_non_pinned_resolved_image_refuses_before_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    raw = _inspect()
    raw["Image"] = "sha256:" + "1" * 64
    deps.inspect_container = lambda: raw
    deps.inspect_named_container = lambda _name: raw
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert deps.action_log == []
    assert not any(sql.startswith("CREATE TABLESPACE") for sql, _params in connection.calls)


def test_enforce_persists_private_recovery_before_stop_rename_then_creates_catalog_after_bind_ready(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "installed", result.receipt
    assert not config.recovery_path.exists()
    public = _read(config.receipt_path)
    assert "do-not-leak" not in json.dumps(public)
    docker_actions = [argv for kind, argv in deps.action_log if kind == "docker"]
    assert docker_actions[0][:3] == ("/usr/bin/docker", "stop", "nhms-db")
    assert docker_actions[1][:4] == ("/usr/bin/docker", "rename", "nhms-db", "nhms-db-before")
    assert any(argv[:2] == ("/usr/bin/docker", "run") for argv in docker_actions)
    create_index = next(
        index for index, (sql, _params) in enumerate(connection.calls) if sql.startswith("CREATE TABLESPACE")
    )
    assert create_index > 0
    assert (
        "CREATE TABLESPACE \"nhms_cold\" LOCATION '/home/postgres/pgdata/tablespaces/nhms_cold'"
        in connection.calls[create_index][0]
    )


def test_recovery_authority_is_private_before_container_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    config = _config(tmp_path, enforce=True)
    observed_private: list[dict] = []
    actions: list[tuple[str, ...]] = []
    replacement_created = False
    deps = _dependencies(connection)

    def docker(argv: tuple[str, ...]) -> dict:
        nonlocal replacement_created
        actions.append(argv)
        if argv[1:3] == ("stop", "nhms-db"):
            observed_private.append(_read(config.recovery_path))
        if argv[1] == "run":
            replacement_created = True
        return {"returncode": 0}

    deps.docker = docker
    deps.inspect_container = lambda: _inspect(cold_bind=replacement_created)
    result = run_install(config, deps)

    assert result.outcome == "installed"
    assert actions[0][1:3] == ("stop", "nhms-db")
    assert observed_private[0]["phase"] == "prepared"
    assert "do-not-leak" in json.dumps(observed_private[0])
    assert "do-not-leak" not in json.dumps(_read(config.receipt_path))
    assert not config.recovery_path.exists()


def test_installed_receipt_reports_live_recreated_snapshot_and_in_progress_keeps_preimage(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    published: list[dict[str, Any]] = []
    deps.before_receipt_publish = lambda _path, receipt: published.append(
        {"outcome": receipt.get("outcome"), "snapshot": dict(receipt["container_snapshot"])}
    )

    result = run_install(config, deps)

    assert result.outcome == "installed", result.receipt
    after = normalize_raw_inspect(deps.inspect_container())
    before = normalize_raw_inspect(_inspect())
    progress = next(item for item in published if item["outcome"] == "in_progress")
    assert progress["snapshot"] == {
        "config_digest": before.config_digest,
        "environment_names": ["POSTGRES_PASSWORD", "POSTGRES_USER"],
        "resolved_image_id": before.resolved_image_id,
    }
    assert result.receipt["container_snapshot"] == {
        "config_digest": after.config_digest,
        "environment_names": ["POSTGRES_PASSWORD", "POSTGRES_USER"],
        "resolved_image_id": after.resolved_image_id,
    }
    assert result.receipt["container_snapshot"]["config_digest"] != before.config_digest
    assert "do-not-leak" not in json.dumps(result.receipt)


def test_already_ready_second_run_receipt_equals_the_installed_final_snapshot(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)

    first = run_install(config, deps)
    assert first.outcome == "installed", first.receipt
    final_snapshot = dict(first.receipt["container_snapshot"])
    final_live = normalize_raw_inspect(deps.inspect_container())

    second = run_install(config, deps)
    assert second.outcome == "already_ready", second.receipt
    assert second.receipt["container_snapshot"]["config_digest"] == final_live.config_digest
    assert second.receipt["container_snapshot"] == final_snapshot


def test_enforce_failing_host_precondition_never_opens_database_or_mutates(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.inspect_backup = lambda *_targets: _backup(complete=False)

    result = run_install(_config(tmp_path, enforce=True), deps)

    assert result.outcome == "no_go"
    assert connection.calls  # dry-run and enforce both require catalog observation
    assert not any(sql.startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in connection.calls)
    assert deps.action_log == []


def test_precondition_failure_replaces_stale_success_with_schema_valid_no_go_and_no_secret(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    config.receipt_path.write_text(
        json.dumps({"outcome": "installed", "leak": "postgresql://user:password@host/db"}), encoding="utf-8"
    )
    config.receipt_path.chmod(0o600)
    deps.inspect_backup = lambda: _backup(complete=False)

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    receipt = _read(config.receipt_path)
    assert receipt["outcome"] == "no_go"
    assert "password" not in json.dumps(receipt)
    jsonschema.validate(receipt, result.schema)
    assert not deps.action_log


def test_readback_requires_catalog_bind_device_writable_no_attach_and_pg_default_new_chunks(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    target = deps.inspect_target()
    target["writable"] = False
    deps.inspect_target = lambda: target

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert result.receipt["state"] == "rollback"
    assert result.receipt["authority"] == {"state": "closed", "phase": None, "path_present": False}
    assert not config.recovery_path.exists()
    assert result.receipt["readback"]["approved"] is False
    assert any("writable" in item for item in result.receipt["blockers"])
    assert not any(str(sql).startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in connection.calls)
    assert [argv[1] for _kind, argv in deps.action_log] == ["stop", "rename", "run", "rm", "rename", "start"]


def test_malformed_recovery_authority_blocks_a_new_enforce_run_without_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    config.recovery_path.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    config.recovery_path.chmod(0o600)
    deps.recovery_exists = config.recovery_path.exists

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    assert not deps.action_log
    assert connection.calls == []
    assert config.recovery_path.exists()
    assert "recovery" in " ".join(result.receipt["blockers"])


@pytest.mark.parametrize(
    ("active_state", "sub_state", "result"),
    [
        ("active", "running", "success"),
        ("inactive", "dead", "failed"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_enforce_rejects_nonquiescent_writer_or_timer_before_authority_or_mutation(
    tmp_path: Path, active_state: str, sub_state: str, result: str
) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    deps.inspect_quiescence = lambda: {
        "units": {
            unit: {"active_state": active_state, "sub_state": sub_state, "result": result}
            for unit in (
                "nhms-node27-autopipe.service",
                "nhms-node27-autopipe.timer",
                "nhms-node27-timeseries-compression.service",
                "nhms-node27-timeseries-compression.timer",
                "nhms-node27-timeseries-retention.service",
                "nhms-node27-timeseries-retention.timer",
            )
        }
    }

    result_value = run_install(config, deps)

    assert result_value.outcome == "no_go"
    assert not config.recovery_path.exists()
    assert not deps.action_log
    assert any("writer/timer" in blocker for blocker in result_value.receipt["blockers"])


def _recovery_authority(*, phase: str, **ownership: bool) -> dict:
    from packages.common.node27_cold_tablespace_authority import private_snapshot_digest
    from packages.common.node27_cold_tablespace_container import normalize_raw_inspect

    before = normalize_raw_inspect(_inspect())
    replacement = before.with_cold_bind()
    from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY

    return {
        "schema_version": "1.0",
        "phase": phase,
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "updated_at": NOW.isoformat().replace("+00:00", "Z"),
        "head_sha": SHA,
        "identity": PRODUCTION_IDENTITY.public_payload(),
        "prior_name": "nhms-db-before",
        "prior": {
            "container_id": before.container_id,
            "config_digest": before.config_digest,
            "private_snapshot": before.private_payload(),
            "private_snapshot_digest": private_snapshot_digest(before.private_payload()),
        },
        "expected": {
            "cold_bind": f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw",
            "config_digest": replacement.config_digest,
            "resolved_image_id": before.resolved_image_id,
        },
        "path": {"device_identity": "8:11:1", "uid": 999, "gid": 999, "mode": 0o700},
        "ownership": {
            "host_path_created": False,
            "prior_stopped": False,
            "prior_renamed": False,
            "installer_container_created": False,
            "catalog_created": False,
            **ownership,
        },
    }


def test_recovery_complete_target_is_idempotent_and_removes_authority_after_terminal_receipt(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)
    authority = _recovery_authority(
        phase="ddl_created",
        prior_stopped=True,
        prior_renamed=True,
        installer_container_created=True,
        catalog_created=True,
    )
    config.recovery_path.write_text(json.dumps(authority), encoding="utf-8")
    config.recovery_path.chmod(0o600)
    private_before = config.recovery_path.read_text(encoding="utf-8")

    result = run_install(config, deps)

    assert result.outcome == "installed"
    live = normalize_raw_inspect(deps.inspect_container())
    assert result.receipt["container_snapshot"] == {
        "config_digest": live.config_digest,
        "environment_names": ["POSTGRES_PASSWORD", "POSTGRES_USER"],
        "resolved_image_id": live.resolved_image_id,
    }
    assert "do-not-leak" in private_before
    assert "do-not-leak" not in json.dumps(result.receipt)
    assert not config.recovery_path.exists()
    assert not deps.action_log


def test_recovery_accepts_the_production_owned_current_bind_inventory_shape(tmp_path: Path) -> None:
    connection = FakeConnection(topology="absent")
    deps = _dependencies(connection)
    deps.synthetic_container_state["current"] = "nhms-db"  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    deps.synthetic_container_state["cold_bind"] = True  # type: ignore[attr-defined]
    deps.current_bind_references = lambda: (f"nhms-db:{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}",)
    config = _config(tmp_path, enforce=True)
    authority = _recovery_authority(
        phase="replacement_created", prior_stopped=True, prior_renamed=True, installer_container_created=True
    )
    config.recovery_path.write_text(json.dumps(authority), encoding="utf-8")
    config.recovery_path.chmod(0o600)

    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup", result.receipt
    assert any(argv[1] == "rm" for _kind, argv in deps.action_log)


def test_recovery_incomplete_owned_replacement_rolls_back_and_removes_authority(tmp_path: Path) -> None:
    connection = FakeConnection(topology="absent")
    deps = _dependencies(connection)
    deps.synthetic_container_state["current"] = "nhms-db"  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    deps.synthetic_container_state["cold_bind"] = True  # type: ignore[attr-defined]
    deps.current_bind_references = lambda: ("nhms-db",)
    config = _config(tmp_path, enforce=True)
    authority = _recovery_authority(
        phase="replacement_created", prior_stopped=True, prior_renamed=True, installer_container_created=True
    )
    config.recovery_path.write_text(json.dumps(authority), encoding="utf-8")
    config.recovery_path.chmod(0o600)

    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup"
    assert result.receipt["rollback"]["prior_restored"] is True
    assert config.recovery_path.exists()


def test_recovery_restore_requires_fresh_absent_catalog_and_exact_prior_snapshot(tmp_path: Path) -> None:
    connection = FakeConnection(topology="absent")
    deps = _dependencies(connection)
    deps.synthetic_container_state["current"] = "nhms-db"  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    deps.synthetic_container_state["cold_bind"] = True  # type: ignore[attr-defined]
    deps.current_bind_references = lambda: ("nhms-db",)
    config = _config(tmp_path, enforce=True)
    authority = _recovery_authority(
        phase="replacement_created", prior_stopped=True, prior_renamed=True, installer_container_created=True
    )
    config.recovery_path.write_text(json.dumps(authority), encoding="utf-8")
    config.recovery_path.chmod(0o600)
    original_docker = deps.docker

    def docker(argv: tuple[str, ...]) -> dict:
        response = original_docker(argv)
        if argv[1:3] == ("start", "nhms-db"):
            connection.topology = "drifted"
        return response

    deps.docker = docker
    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup"
    assert config.recovery_path.exists()


def test_recovery_mixed_reference_preserves_authority_and_refuses_repair(tmp_path: Path) -> None:
    connection = FakeConnection(topology="absent")
    deps = _dependencies(connection)
    deps.current_bind_references = lambda: (f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw",)
    config = _config(tmp_path, enforce=True)
    authority = _recovery_authority(
        phase="replacement_created", prior_stopped=True, prior_renamed=True, installer_container_created=True
    )
    config.recovery_path.write_text(json.dumps(authority), encoding="utf-8")
    config.recovery_path.chmod(0o600)

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    assert config.recovery_path.exists()
    assert not deps.action_log


def test_rollback_never_removes_a_preexisting_empty_host_path(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    removals: list[bool] = []
    deps.remove_host_path = lambda: removals.append(True) or True
    target = deps.inspect_target()
    target["writable"] = False
    deps.inspect_target = lambda: target

    result = run_install(_config(tmp_path, enforce=True), deps)

    assert result.outcome == "rollback"
    assert removals == []
    assert result.receipt["rollback"]["host_path_removed"] is False
    assert result.receipt["rollback"]["blockers"] == []


def test_rollback_requires_fresh_path_identity_and_emptiness_before_removal(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    unsafe = _path_observation()
    unsafe["entry_count"] = 1
    deps.inspect_host_path_for_rollback = lambda: unsafe
    target = deps.inspect_target()
    target["writable"] = False
    deps.inspect_target = lambda: target
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "rollback"
    assert result.receipt["rollback"]["host_path_removed"] is False
    assert result.receipt["rollback"]["blockers"] == []


def test_roll_back_refuses_host_path_deletion_when_stopped_container_has_stale_cold_bind(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.stopped_bind_references = lambda: (f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw",)
    target = deps.inspect_target()
    target["writable"] = False
    deps.inspect_target = lambda: target
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    assert result.receipt["authority"]["state"] == "closed"
    assert "stopped" in " ".join(result.receipt["blockers"])
    assert not deps.action_log


@pytest.mark.parametrize("outcome", ["dry_run", "already_ready", "no_go", "in_progress", "rollback", "error"])
def test_public_receipt_schema_admits_each_required_outcome(outcome: str) -> None:
    payload = InstallConfig.example_receipt(outcome=outcome, head_sha=SHA)
    schema = InstallConfig.load_schema()

    jsonschema.validate(payload, schema)


@pytest.mark.parametrize(
    "name",
    [
        "node27_cold_tablespace_install_receipt.example.json",
        "node27_cold_tablespace_install_receipt.already-ready.example.json",
        "node27_cold_tablespace_install_receipt.no-go.example.json",
        "node27_cold_tablespace_install_receipt.progress.example.json",
        "node27_cold_tablespace_install_receipt.pending-cleanup.example.json",
        "node27_cold_tablespace_install_receipt.rollback.example.json",
        "node27_cold_tablespace_install_receipt.error.example.json",
    ],
)
def test_installer_receipt_examples_are_schema_valid(name: str) -> None:
    path = Path(__file__).resolve().parents[1] / "schemas" / "examples" / name
    jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), InstallConfig.load_schema())


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
