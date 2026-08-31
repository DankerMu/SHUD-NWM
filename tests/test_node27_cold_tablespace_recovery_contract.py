"""Natural recovery and authority-closure contracts for the installer seam."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from packages.common.node27_cold_tablespace_authority import private_snapshot_digest
from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY
from packages.common.node27_cold_tablespace_install import InstallConfig, run_install
from tests.test_node27_cold_tablespace_install import NOW, SHA, FakeConnection, _config, _dependencies, _inspect


def _authority(*, phase: str, **ownership: bool) -> dict:
    from packages.common.node27_cold_tablespace_container import normalize_raw_inspect

    before = normalize_raw_inspect(_inspect())
    replacement = before.with_cold_bind()
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
            "cold_bind": "/data/GHDC/nhms-cold-tablespace:/home/postgres/pgdata/tablespaces/nhms_cold:rw",
            "config_digest": replacement.config_digest,
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


def _write_authority(config, document: dict) -> None:
    config.recovery_path.write_text(json.dumps(document), encoding="utf-8")
    config.recovery_path.chmod(0o600)


@pytest.mark.parametrize(
    ("phase", "ownership", "expected_actions"),
    (
        ("prepared", {}, ()),
        ("path_created", {"host_path_created": True}, ()),
        ("prior_stopped", {"prior_stopped": True}, (("start", "nhms-db"),)),
        (
            "prior_renamed",
            {"prior_stopped": True, "prior_renamed": True},
            (("rename", "nhms-db-before"), ("start", "nhms-db")),
        ),
    ),
)
def test_recovery_closes_each_early_persisted_phase_without_replaying_install(
    tmp_path: Path, phase: str, ownership: dict[str, bool], expected_actions: tuple[tuple[str, str], ...]
) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    if phase == "path_created":
        deps.remove_host_path = lambda: True
    if phase == "prior_renamed":
        deps.synthetic_container_state["current"] = None  # type: ignore[attr-defined]
        deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    config = _config(tmp_path, enforce=True)
    _write_authority(config, _authority(phase=phase, **ownership))

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    actions = [argv[1:3] for _kind, argv in deps.action_log]
    for expected in expected_actions:
        assert expected in actions
    assert not any(action[0] == "run" for action in actions)
    assert not config.recovery_path.exists()


def test_dry_run_with_authority_checks_it_before_inspection_and_publishes_recovery_required(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path)
    _write_authority(config, _authority(phase="prepared"))

    result = run_install(config, deps)

    assert result.outcome == "no_go"
    assert result.receipt["state"] == "recovery_required"
    assert result.receipt["authority"]["state"] == "sidecar"
    assert connection.calls == []
    assert deps.action_log == []
    jsonschema.validate(result.receipt, InstallConfig.load_schema())


def test_replacement_recovery_requires_sql_readiness_before_catalog_observation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.synthetic_container_state["current"] = "nhms-db"  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    deps.synthetic_container_state["cold_bind"] = True  # type: ignore[attr-defined]
    deps.current_bind_references = lambda: ("nhms-db",)
    readiness = False

    def wait_ready() -> None:
        nonlocal readiness
        readiness = True

    def connect_readonly() -> FakeConnection:
        if not readiness:
            raise RuntimeError("replacement SQL path is not ready")
        return connection

    deps.wait_ready = wait_ready
    deps.connect_readonly = connect_readonly
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="replacement_created",
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
        ),
    )

    result = run_install(config, deps)

    assert readiness is True
    assert result.outcome == "pending_cleanup", result.receipt


def test_early_recovery_opens_catalog_observation_only_after_restarting_stopped_prior(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    started = False
    observations: list[bool] = []
    original_docker = deps.docker

    def docker(argv: tuple[str, ...]) -> dict:
        nonlocal started
        result = original_docker(argv)
        if argv[1:3] == ("start", "nhms-db"):
            started = True
        return result

    def connect_readonly() -> FakeConnection:
        observations.append(started)
        if not started:
            raise RuntimeError("prior PostgreSQL is stopped")
        return connection

    deps.docker = docker
    deps.connect_readonly = connect_readonly
    config = _config(tmp_path, enforce=True)
    _write_authority(config, _authority(phase="prior_stopped", prior_stopped=True))

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert observations == [True]
    assert not config.recovery_path.exists()


def test_early_recovery_removes_only_its_recorded_path_after_prior_restore(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    observed_path = {
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
    deps.inspect_host_path_for_rollback = lambda: observed_path
    removals: list[bool] = []
    deps.remove_host_path = lambda: removals.append(True) or True
    deps.synthetic_container_state["current"] = None  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="prior_renamed",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert result.receipt["rollback"] == {
        "attempted": True,
        "prior_restored": True,
        "host_path_removed": True,
        "blockers": [],
    }
    assert removals == [True]
    assert not config.recovery_path.exists()


def test_early_recovery_retains_authority_when_recorded_path_is_not_fresh_after_restore(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    unsafe = {
        "exists": True,
        "is_symlink": False,
        "is_directory": True,
        "entry_count": 1,
        "uid": 999,
        "gid": 999,
        "mode": 0o700,
        "mount_device": "8:11",
        "device_identity": "8:11:1",
        "free_bytes": 1_000_000,
    }
    deps.inspect_host_path_for_rollback = lambda: unsafe
    removals: list[bool] = []
    deps.remove_host_path = lambda: removals.append(True) or True
    deps.synthetic_container_state["current"] = None  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="prior_renamed",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert result.receipt["state"] == "blocked"
    assert result.receipt["authority"]["state"] == "pending_cleanup"
    assert removals == []
    assert config.recovery_path.exists()


def test_terminal_pending_cleanup_removes_a_recorded_fresh_path_before_closure(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    removals: list[bool] = []
    deps.remove_host_path = lambda: removals.append(True) or True
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="terminal_pending_cleanup",
            host_path_created=True,
            prior_stopped=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert result.receipt["rollback"]["host_path_removed"] is True
    assert removals == [True]
    assert not config.recovery_path.exists()


def test_missing_readiness_boundary_after_recovery_transition_retains_authority(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.wait_ready = None
    config = _config(tmp_path, enforce=True)
    _write_authority(config, _authority(phase="prior_stopped", prior_stopped=True))

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert result.receipt["authority"]["state"] == "sidecar"
    assert config.recovery_path.exists()
    assert any(argv[1:3] == ("start", "nhms-db") for _kind, argv in deps.action_log)


def test_first_terminal_receipt_failure_retains_authority_without_rollback(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="ddl_created",
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )
    deps.before_receipt_publish = lambda _path, _receipt: (_ for _ in ()).throw(RuntimeError("terminal write failed"))

    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup", result.receipt
    assert result.receipt["authority"] == {
        "state": "pending_cleanup",
        "phase": "terminal_pending_cleanup",
        "path_present": True,
    }
    assert result.receipt["ownership"]["recovery_authority"] is True
    assert config.recovery_path.exists()
    assert not deps.action_log


def test_terminal_final_receipt_failure_restores_authority_and_reports_pending_cleanup(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="ddl_created",
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )
    publications = 0

    def before_receipt_publish(_path, _receipt) -> None:
        nonlocal publications
        publications += 1
        if publications == 2:
            raise RuntimeError("final receipt write failed")

    deps.before_receipt_publish = before_receipt_publish
    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup", result.receipt
    assert result.receipt["authority"] == {
        "state": "pending_cleanup",
        "phase": "terminal_pending_cleanup",
        "path_present": True,
    }
    assert result.receipt["ownership"]["recovery_authority"] is True
    assert config.recovery_path.exists()


def test_terminal_authority_unlink_failure_is_truthful_pending_cleanup_not_false_closed(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="ddl_created",
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )
    deps.remove_recovery = lambda _path: (_ for _ in ()).throw(RuntimeError("unlink failed"))

    result = run_install(config, deps)

    assert result.outcome == "pending_cleanup"
    assert result.receipt["state"] == "pending_cleanup"
    assert result.receipt["authority"]["state"] == "pending_cleanup"
    assert config.recovery_path.exists()
    jsonschema.validate(result.receipt, InstallConfig.load_schema())


def test_rollback_persists_remaining_ownership_before_terminal_unlink_retry(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    state = deps.synthetic_container_state  # type: ignore[attr-defined]
    state["prior"] = "nhms-db-before"
    observed_path = {
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
    deps.inspect_path = lambda: observed_path
    deps.inspect_host_path_for_rollback = lambda: observed_path
    deps.current_bind_references = lambda: ("nhms-db",) if state["cold_bind"] else ()
    deps.inspect_target = lambda: {
        "container_name": "nhms-db",
        "container_bind": "/data/GHDC/nhms-cold-tablespace",
        "host_path": "/data/GHDC/nhms-cold-tablespace",
        "device_identity": "8:11:1",
        "writable": False,
    }
    deps.remove_host_path = lambda: observed_path.update(
        {"exists": False, "is_directory": False, "entry_count": None, "uid": None, "gid": None, "mode": None}
    ) is None
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="ddl_created",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )
    unlink_attempts = 0

    def remove_recovery(path: Path) -> None:
        nonlocal unlink_attempts
        unlink_attempts += 1
        if unlink_attempts == 1:
            raise RuntimeError("injected unlink failure")
        path.unlink()

    deps.remove_recovery = remove_recovery

    first = run_install(config, deps)

    assert first.outcome == "pending_cleanup", first.receipt
    persisted = json.loads(config.recovery_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "terminal_pending_cleanup"
    assert persisted["ownership"] == {
        "host_path_created": False,
        "prior_stopped": False,
        "prior_renamed": False,
        "installer_container_created": False,
        "catalog_created": False,
    }
    recovery_actions = list(deps.action_log)

    second = run_install(config, deps)

    assert second.outcome == "rollback", second.receipt
    assert not config.recovery_path.exists()
    assert deps.action_log == recovery_actions
    assert not any(argv[1] == "run" for _kind, argv in deps.action_log)
