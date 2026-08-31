"""Read-only complete topology contracts for installer dry-run."""

from __future__ import annotations

from pathlib import Path

from packages.common.node27_cold_tablespace_install import run_install
from tests.test_node27_cold_tablespace_install import FakeConnection, _config, _dependencies, _inspect


def test_dry_run_requires_catalog_observation_and_never_issues_docker_or_ddl(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.connect_readonly = lambda: (_ for _ in ()).throw(RuntimeError("dsn unavailable"))

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert result.receipt["state"] == "blocked"
    assert not deps.action_log
    assert connection.calls == []
    assert "postgresql" not in str(result.receipt).lower()


def test_dry_run_catches_catalog_drift_without_docker_mutation_or_ddl(tmp_path: Path) -> None:
    connection = FakeConnection(topology="drifted")
    deps = _dependencies(connection)

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert any("topology" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log
    assert not any(sql.startswith(("CREATE TABLESPACE", "DROP TABLESPACE")) for sql, _params in connection.calls)


def test_dry_run_catches_hypertable_attachment_drift_without_mutation(tmp_path: Path) -> None:
    class AttachedConnection(FakeConnection):
        def execute(self, sql: str, params: object = None) -> list[dict]:
            if "FROM _timescaledb_catalog.tablespace AS space" in sql:
                return [{"tablespace_name": "nhms_cold"}]
            return super().execute(sql, params)

    connection = AttachedConnection()
    deps = _dependencies(connection)

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert any("attached" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log


def test_dry_run_requires_business_hypertable_default_placement_observation(tmp_path: Path) -> None:
    class UnprovenPlacementConnection(FakeConnection):
        def execute(self, sql: str, params: object = None) -> list[dict]:
            if "FROM (VALUES" in sql:
                return [{"tablespace": None}]
            return super().execute(sql, params)

    connection = UnprovenPlacementConnection()
    deps = _dependencies(connection)

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert any("default placement" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log


def test_dry_run_catches_partial_current_bind_without_catalog_or_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.inspect_container = lambda: _inspect(cold_bind=True)

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert any("partial" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log


def test_dry_run_catches_dangling_current_and_stopped_binds_without_mutation(tmp_path: Path) -> None:
    connection = FakeConnection()
    deps = _dependencies(connection)
    deps.current_bind_references = lambda: ("unowned-current-cold-bind",)
    deps.stopped_bind_references = lambda: ("stopped-owned-cold-bind",)
    deps.pg_tblspc_references = lambda: ("dangling-pg-tblspc",)

    result = run_install(_config(tmp_path), deps)

    assert result.outcome == "no_go"
    assert any("stopped" in blocker for blocker in result.receipt["blockers"])
    assert any("current" in blocker for blocker in result.receipt["blockers"])
    assert any("pg_tblspc" in blocker for blocker in result.receipt["blockers"])
    assert not deps.action_log
