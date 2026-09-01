"""Write-ahead pending-action recovery contracts for the installer seam."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from packages.common.node27_cold_tablespace_authority import read_authority
from packages.common.node27_cold_tablespace_install import InstallConfig, InstallInterrupted, run_install
from packages.common.node27_cold_tablespace_observation import NamedObservationError, inspect_named_optional
from tests.test_node27_cold_tablespace_install import (
    FakeConnection,
    _config,
    _dependencies,
    _inspect,
    _path_observation,
)
from tests.test_node27_cold_tablespace_recovery_contract import _authority, _write_authority


def _interrupt_after(original, *, match: tuple[str, ...]):
    def wrapped(argv: tuple[str, ...]) -> dict:
        result = original(argv)
        if argv[1 : 1 + len(match)] == match:
            raise InstallInterrupted(f"after {match[0]}")
        return result

    return wrapped


@pytest.mark.parametrize(
    ("match", "pending"),
    (
        (("stop", "nhms-db"), "stop_prior"),
        (("rename", "nhms-db"), "rename_prior"),
        (("run",), "create_replacement"),
    ),
)
def test_interruption_after_install_docker_action_before_confirm_recovers_without_ownership_leak(
    tmp_path: Path, match: tuple[str, ...], pending: str
) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    deps.docker = _interrupt_after(deps.docker, match=match)

    with pytest.raises(InstallInterrupted):
        run_install(config, deps)

    authority = read_authority(config.recovery_path)
    assert authority["pending_action"] == pending
    first_actions = [argv[1] for _kind, argv in deps.action_log]
    assert match[0] in first_actions

    recovered_deps = _dependencies(connection)
    recovered_deps.synthetic_container_state.update(deps.synthetic_container_state)  # type: ignore[attr-defined]
    recovered = run_install(config, recovered_deps)
    if recovered.outcome == "pending_cleanup":
        recovered = run_install(config, recovered_deps)

    assert recovered.outcome == "rollback", recovered.receipt
    assert not config.recovery_path.exists()
    assert recovered_deps.synthetic_container_state["current"] == "nhms-db"  # type: ignore[attr-defined]
    assert recovered_deps.synthetic_container_state["prior"] is None  # type: ignore[attr-defined]
    assert recovered_deps.synthetic_container_state["running"] is True  # type: ignore[attr-defined]
    assert recovered_deps.synthetic_container_state["cold_bind"] is False  # type: ignore[attr-defined]
    assert not any(argv[1] == "run" for _kind, argv in recovered_deps.action_log)
    jsonschema.validate(recovered.receipt, InstallConfig.load_schema())


def test_interruption_after_path_create_before_confirm_does_not_leak_owned_path(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    missing = _path_observation()
    missing.update(
        {"exists": False, "is_directory": False, "entry_count": None, "uid": None, "gid": None, "mode": None}
    )
    created = _path_observation()
    created_state = {"exists": False}

    def inspect_path() -> dict:
        return created if created_state["exists"] else missing

    def ensure_host_path() -> dict:
        created_state["exists"] = True
        raise InstallInterrupted("after path create")

    deps.inspect_path = inspect_path
    deps.inspect_host_path_for_rollback = inspect_path
    deps.ensure_host_path = ensure_host_path
    deps.remove_host_path = lambda: created_state.update(exists=False) or True
    config = _config(tmp_path, enforce=True)

    with pytest.raises(InstallInterrupted):
        run_install(config, deps)

    authority = read_authority(config.recovery_path)
    assert authority["pending_action"] == "create_host_path"
    assert created_state["exists"] is True

    recovered = run_install(config, deps)
    if recovered.outcome == "pending_cleanup":
        recovered = run_install(config, deps)
    assert recovered.outcome == "rollback", recovered.receipt
    assert created_state["exists"] is False
    assert not config.recovery_path.exists()
    assert not any(argv[1] == "run" for _kind, argv in deps.action_log)


def test_interruption_after_create_tablespace_adopts_or_rolls_back_without_ddl_replay(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    original = connection.execute

    def execute(sql: str, params: object = None) -> list[dict]:
        result = original(sql, params)
        if sql.startswith("CREATE TABLESPACE"):
            raise InstallInterrupted("after create tablespace")
        return result

    connection.execute = execute  # type: ignore[method-assign]

    with pytest.raises(InstallInterrupted):
        run_install(config, deps)

    authority = read_authority(config.recovery_path)
    assert authority["pending_action"] == "create_catalog"
    assert connection.topology == "expected"
    create_count = sum(1 for sql, _params in connection.calls if str(sql).startswith("CREATE TABLESPACE"))
    assert create_count == 1

    connection.execute = original  # type: ignore[method-assign]
    recovered = run_install(config, deps)
    if recovered.outcome == "pending_cleanup":
        recovered = run_install(config, deps)
    replayed = sum(1 for sql, _params in connection.calls if str(sql).startswith("CREATE TABLESPACE"))
    assert replayed == 1
    assert recovered.outcome == "installed", recovered.receipt
    assert not config.recovery_path.exists()


@pytest.mark.parametrize(
    ("pending", "phase", "ownership", "topology", "cold_bind", "state", "post", "expected_action"),
    (
        (
            "drop_catalog",
            "ddl_created",
            {
                "prior_stopped": True,
                "prior_renamed": True,
                "installer_container_created": True,
                "catalog_created": True,
            },
            "expected",
            True,
            {"current": "nhms-db", "prior": "nhms-db-before", "running": True, "cold_bind": True},
            False,
            "rm",
        ),
        (
            "drop_catalog",
            "ddl_created",
            {
                "prior_stopped": True,
                "prior_renamed": True,
                "installer_container_created": True,
                "catalog_created": True,
            },
            "absent",
            True,
            {"current": "nhms-db", "prior": "nhms-db-before", "running": True, "cold_bind": True},
            True,
            "rm",
        ),
        (
            "remove_replacement",
            "replacement_created",
            {
                "prior_stopped": True,
                "prior_renamed": True,
                "installer_container_created": True,
                "catalog_created": False,
            },
            "absent",
            True,
            {"current": "nhms-db", "prior": "nhms-db-before", "running": True, "cold_bind": True},
            False,
            "rm",
        ),
        (
            "remove_replacement",
            "replacement_created",
            {
                "prior_stopped": True,
                "prior_renamed": True,
                "installer_container_created": True,
                "catalog_created": False,
            },
            "absent",
            False,
            {"current": None, "prior": "nhms-db-before", "running": False, "cold_bind": False},
            True,
            "rename",
        ),
        (
            "rename_prior_back",
            "terminal_pending_cleanup",
            {"prior_stopped": True, "prior_renamed": True, "installer_container_created": False},
            "absent",
            False,
            {"current": None, "prior": "nhms-db-before", "running": False, "cold_bind": False},
            False,
            "rename",
        ),
        (
            "rename_prior_back",
            "terminal_pending_cleanup",
            {"prior_stopped": True, "prior_renamed": True, "installer_container_created": False},
            "absent",
            False,
            {"current": "nhms-db", "prior": None, "running": False, "cold_bind": False},
            True,
            "start",
        ),
        (
            "start_prior",
            "terminal_pending_cleanup",
            {"prior_stopped": True, "prior_renamed": False},
            "absent",
            False,
            {"current": "nhms-db", "prior": None, "running": False, "cold_bind": False},
            False,
            "start",
        ),
        (
            "start_prior",
            "terminal_pending_cleanup",
            {"prior_stopped": True, "prior_renamed": False},
            "absent",
            False,
            {"current": "nhms-db", "prior": None, "running": True, "cold_bind": False},
            True,
            None,
        ),
        (
            "remove_host_path",
            "terminal_pending_cleanup",
            {"host_path_created": True},
            "absent",
            False,
            {"current": "nhms-db", "prior": None, "running": True, "cold_bind": False},
            False,
            None,
        ),
        (
            "remove_host_path",
            "terminal_pending_cleanup",
            {"host_path_created": True},
            "absent",
            False,
            {"current": "nhms-db", "prior": None, "running": True, "cold_bind": False},
            True,
            None,
        ),
    ),
)
def test_rollback_pending_pre_and_post_conditions_converge(
    tmp_path: Path,
    pending: str,
    phase: str,
    ownership: dict[str, bool],
    topology: str,
    cold_bind: bool,
    state: dict[str, object],
    post: bool,
    expected_action: str | None,
) -> None:
    connection = FakeConnection(topology=topology)
    deps = _dependencies(connection, cold_bind=cold_bind)
    deps.synthetic_container_state.update(state)  # type: ignore[attr-defined]
    path_exists = {"v": pending != "remove_host_path" or not post}
    observed = _path_observation()
    if pending == "remove_host_path":
        if post:
            observed.update(
                {"exists": False, "is_directory": False, "entry_count": None, "uid": None, "gid": None, "mode": None}
            )
        deps.inspect_path = lambda: observed
        deps.inspect_host_path_for_rollback = lambda: observed
        deps.remove_host_path = lambda: path_exists.update(v=False) or True
    config = _config(tmp_path, enforce=True)
    document = _authority(phase=phase, **ownership)
    document["pending_action"] = pending
    _write_authority(config, document)

    planted = read_authority(config.recovery_path)
    assert planted["pending_action"] == pending
    recovered = run_install(config, deps)
    if recovered.outcome == "pending_cleanup":
        recovered = run_install(config, deps)

    assert recovered.outcome == "rollback", recovered.receipt
    assert not config.recovery_path.exists()
    if expected_action is not None and not post:
        assert any(argv[1] == expected_action for _kind, argv in deps.action_log)
    if pending == "remove_host_path":
        assert path_exists["v"] is False or post is True
    jsonschema.validate(recovered.receipt, InstallConfig.load_schema())


def test_all_true_terminal_pending_complete_target_closes_with_zero_docker_actions(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="terminal_pending_cleanup",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "installed", result.receipt
    assert deps.action_log == []
    assert not config.recovery_path.exists()


def test_all_true_terminal_pending_absent_target_fails_closed_and_retains_authority(tmp_path: Path) -> None:
    connection = FakeConnection(topology="absent")
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="terminal_pending_cleanup",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert deps.action_log == []
    assert not any(str(sql).startswith("DROP TABLESPACE") for sql, _params in connection.calls)
    persisted = read_authority(config.recovery_path)
    assert persisted["phase"] == "terminal_pending_cleanup"
    assert persisted["ownership"] == {
        "host_path_created": True,
        "prior_stopped": True,
        "prior_renamed": True,
        "installer_container_created": True,
        "catalog_created": True,
    }
    assert persisted.get("pending_action") is None
    jsonschema.validate(result.receipt, InstallConfig.load_schema())


def test_inspect_named_optional_missing_seam_is_uncertainty_not_absence() -> None:
    from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY

    deps = _dependencies(FakeConnection())
    deps.inspect_named_container_optional = None
    with pytest.raises(NamedObservationError):
        inspect_named_optional(deps, "nhms-db", PRODUCTION_IDENTITY)


def test_inspect_named_optional_timeout_text_containing_no_such_container_is_uncertainty() -> None:
    from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY

    def _timeout(_name: str) -> dict:
        raise TimeoutError("docker inspect timed out: No such container nhms-db")

    deps = _dependencies(FakeConnection())
    deps.inspect_named_container = _timeout
    deps.inspect_named_container_optional = _timeout
    with pytest.raises(NamedObservationError):
        inspect_named_optional(deps, "nhms-db", PRODUCTION_IDENTITY)


def test_inspect_named_optional_none_from_seam_is_proven_absence() -> None:
    from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY

    deps = _dependencies(FakeConnection())
    deps.inspect_named_container_optional = lambda _name: None
    assert inspect_named_optional(deps, "nhms-db", PRODUCTION_IDENTITY) is None


def test_readiness_retries_within_finite_wall_then_succeeds(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    attempts = {"n": 0}
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def wait_ready() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("postgresql is starting")

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    deps.wait_ready = wait_ready
    deps.sleep = sleep
    deps.monotonic = lambda: clock["now"]
    deps.ready_timeout_seconds = 90.0
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "installed", result.receipt
    assert attempts["n"] >= 3


def test_post_transition_permanent_readiness_failure_never_surveys_stale_connection(tmp_path: Path) -> None:
    stale = FakeConnection()
    deps = _dependencies(stale)
    post_transition: list[str] = []
    original = stale.execute
    transitioned = {"v": False}

    original_docker = deps.docker

    def docker_with_mark(argv: tuple[str, ...]) -> dict:
        result = original_docker(argv)
        if argv[1] in {"stop", "rename", "run"}:
            transitioned["v"] = True
        return result

    def execute(sql: str, params: object = None) -> list[dict]:
        if transitioned["v"]:
            post_transition.append(sql)
        return original(sql, params)

    def wait_ready() -> None:
        raise RuntimeError("replacement SQL path is permanently unavailable")

    stale.execute = execute  # type: ignore[method-assign]
    deps.docker = docker_with_mark
    deps.wait_ready = wait_ready
    deps.ready_timeout_seconds = 0.0
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert result.receipt["state"] == "blocked"
    persisted = read_authority(config.recovery_path)
    assert persisted["phase"] == "replacement_created"
    assert persisted.get("pending_action") is None
    assert persisted["ownership"] == {
        "host_path_created": False,
        "prior_stopped": True,
        "prior_renamed": True,
        "installer_container_created": True,
        "catalog_created": False,
    }
    assert [argv[1] for _kind, argv in deps.action_log] == ["stop", "rename", "run"]
    assert post_transition == []
    assert not any(str(sql).startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in stale.calls)
    assert result.receipt["authority"]["path_present"] is True
    jsonschema.validate(result.receipt, InstallConfig.load_schema())


def test_unavailable_named_inspect_cannot_adopt_remove_replacement_postcondition(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection, cold_bind=True)
    def _inspect_unavailable(_name: str) -> dict:
        raise RuntimeError("docker inspect timed out")

    deps.inspect_named_container = _inspect_unavailable
    deps.inspect_named_container_optional = _inspect_unavailable
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        {
            **_authority(
                phase="replacement_created",
                prior_stopped=True,
                prior_renamed=True,
                installer_container_created=True,
            ),
            "pending_action": "remove_replacement",
        },
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    persisted = read_authority(config.recovery_path)
    assert persisted["pending_action"] == "remove_replacement"
    assert persisted["ownership"]["installer_container_created"] is True
    assert deps.action_log == []


def test_inventory_proven_absence_adopts_remove_replacement_postcondition(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.synthetic_container_state["current"] = None  # type: ignore[attr-defined]
    deps.synthetic_container_state["prior"] = "nhms-db-before"  # type: ignore[attr-defined]
    deps.synthetic_container_state["cold_bind"] = False  # type: ignore[attr-defined]
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        {
            **_authority(
                phase="replacement_created",
                prior_stopped=True,
                prior_renamed=True,
                installer_container_created=True,
            ),
            "pending_action": "remove_replacement",
        },
    )

    result = run_install(config, deps)
    if result.outcome == "pending_cleanup":
        result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert not any(argv[1] == "rm" for _kind, argv in deps.action_log)
    assert not config.recovery_path.exists()


def test_same_name_drifted_current_never_starts_or_removes(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    drifted = _inspect()
    drifted["HostConfig"]["Memory"] = 1
    def _inspect_named(name: str) -> dict:
        if name != "nhms-db":
            raise RuntimeError(name)
        return drifted

    deps.inspect_named_container = _inspect_named
    deps.inspect_named_container_optional = deps.inspect_named_container
    deps.inspect_container = lambda: drifted
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        {
            **_authority(phase="terminal_pending_cleanup", prior_stopped=True),
            "pending_action": "start_prior",
        },
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert not any(argv[1] == "start" for _kind, argv in deps.action_log)
    persisted = read_authority(config.recovery_path)
    assert persisted["pending_action"] == "start_prior"


def test_missing_running_state_is_mixed_not_stopped(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    raw = _inspect(running=True)
    raw.pop("State")
    def _inspect_named(name: str) -> dict:
        if name != "nhms-db":
            raise RuntimeError(name)
        return raw

    deps.inspect_named_container = _inspect_named
    deps.inspect_named_container_optional = _inspect_named
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        {
            **_authority(phase="path_created", host_path_created=True),
            "pending_action": "stop_prior",
        },
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert not any(argv[1] == "stop" for _kind, argv in deps.action_log)
    persisted = read_authority(config.recovery_path)
    assert persisted["pending_action"] == "stop_prior"


def test_path_pending_wrong_owner_or_mode_retains_authority(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    observed = _path_observation()
    observed["uid"] = 1
    deps.inspect_path = lambda: observed
    deps.inspect_host_path_for_rollback = lambda: observed
    deps.remove_host_path = lambda: True
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        {
            **_authority(phase="terminal_pending_cleanup", host_path_created=True),
            "pending_action": "remove_host_path",
        },
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    persisted = read_authority(config.recovery_path)
    assert persisted["pending_action"] == "remove_host_path"
    assert persisted["ownership"]["host_path_created"] is True


def test_terminal_pending_exact_current_with_readiness_error_retains_authority(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    deps.wait_ready = lambda: (_ for _ in ()).throw(RuntimeError("readiness unavailable"))
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="terminal_pending_cleanup",
            host_path_created=True,
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert deps.action_log == []
    assert config.recovery_path.exists()


def test_terminal_pending_inspect_unavailable_retains_authority_without_mutation(tmp_path: Path) -> None:
    connection = FakeConnection(topology="ready")
    deps = _dependencies(connection, cold_bind=True)
    deps.inspect_named_container = lambda _name: (_ for _ in ()).throw(RuntimeError("inspect unavailable"))
    deps.inspect_named_container_optional = deps.inspect_named_container
    config = _config(tmp_path, enforce=True)
    _write_authority(
        config,
        _authority(
            phase="terminal_pending_cleanup",
            prior_stopped=True,
            prior_renamed=True,
            installer_container_created=True,
            catalog_created=True,
        ),
    )

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert deps.action_log == []
    assert config.recovery_path.exists()


def test_tag_only_production_image_refuses_before_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    raw = _inspect()
    raw["Config"]["Image"] = "timescale/timescaledb-ha:pg15-latest"
    deps.inspect_container = lambda: raw
    deps.inspect_named_container = lambda _name: raw
    config = _config(tmp_path, enforce=True)

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert deps.action_log == []
    assert not config.recovery_path.exists()


def test_injected_non_runtime_error_after_in_progress_publishes_schema_valid_receipt(tmp_path: Path) -> None:
    class AdapterError(Exception):
        pass

    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    original = connection.execute

    def execute(sql: str, params: object = None) -> list[dict]:
        if sql.startswith("CREATE TABLESPACE"):
            raise AdapterError("password=super-secret ddl failed")
        return original(sql, params)

    connection.execute = execute  # type: ignore[method-assign]

    result = run_install(config, deps)

    assert result.outcome == "no_go", result.receipt
    assert result.receipt["state"] == "blocked"
    persisted = read_authority(config.recovery_path)
    assert persisted["phase"] == "replacement_created"
    assert persisted["pending_action"] == "create_catalog"
    assert persisted["ownership"] == {
        "host_path_created": False,
        "prior_stopped": True,
        "prior_renamed": True,
        "installer_container_created": True,
        "catalog_created": False,
    }
    assert [argv[1] for _kind, argv in deps.action_log] == ["stop", "rename", "run"]
    assert not any(str(sql).startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in connection.calls)
    rendered = json.dumps(result.receipt)
    assert "super-secret" not in rendered
    assert "password=" not in rendered
    jsonschema.validate(result.receipt, InstallConfig.load_schema())
    assert result.receipt["authority"]["path_present"] is True


def test_post_preflight_device_mismatch_rolls_back_or_refuses(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    config = _config(tmp_path, enforce=True)
    target = deps.inspect_target()
    target["device_identity"] = "8:11:9"
    deps.inspect_target = lambda: target

    result = run_install(config, deps)

    assert result.outcome == "rollback", result.receipt
    assert result.receipt["state"] == "rollback"
    assert result.receipt["authority"] == {"state": "closed", "phase": None, "path_present": False}
    assert not config.recovery_path.exists()
    assert result.receipt["readback"]["approved"] is False
    assert result.receipt["readback"]["device_identity"] == "8:11:9"
    sql = [str(call[0]) for call in connection.calls]
    create_at = next(index for index, statement in enumerate(sql) if statement.startswith("CREATE TABLESPACE"))
    drop_at = next(index for index, statement in enumerate(sql) if statement.startswith("DROP TABLESPACE"))
    assert create_at < drop_at
    assert [argv[1] for _kind, argv in deps.action_log] == ["stop", "rename", "run", "rm", "rename", "start"]
