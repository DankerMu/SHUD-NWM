"""R2b: DB-free is the only scheduler mode (#3255, redesign-orchestration-core tasks 3.1-3.4).

``NHMS_SCHEDULER_DB_FREE_REQUIRED`` no longer selects the scheduler's mode:

* the config reports DB-free whatever the env or the constructor keyword says;
* the backend selectors still fail closed before the lock (no default flips to ``file``);
* every run manifest carries ``scheduler_db_free_required="true"``, so the gateway
  exports the DB-free worker switches even when the scheduler process lacks the
  variable;
* the scheduler pass lease is always the file lease, whatever the lock backend or
  ``database_url`` say;
* a scheduler with no journal repository refuses with ``SchedulerModeInvariantError``
  instead of falling back to a SQL repository.

R2c-1 (#3256) deleted the SQL-factory arms and the other non-DB-free mode branches,
R2c-2 (#3257) the SQL reconcile store and R2c-3 (#3258) the PostgreSQL scheduler lease,
so no non-DB-free arm is left: the one remaining typed refusal is
``scheduler_core:_default_orchestrator_for``.
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from packages.common.contracts.time_normalization import format_cycle_time
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_candidate_runtime as scheduler_candidate_runtime_module
from services.orchestrator import scheduler_lease as scheduler_lease_module
from services.orchestrator import scheduler_runtime as scheduler_runtime_module
from services.orchestrator.chain import CycleOrchestrationContext, OrchestratorConfig, StageDefinition
from services.orchestrator.chain_manifests import build_cycle_stage_manifest
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.scheduler import ProductionSchedulerConfig
from services.orchestrator.scheduler_adapters import NfsRawManifestCycleDiscoveryAdapter
from services.orchestrator.scheduler_file_providers import FileSchedulerModelRegistry
from services.orchestrator.scheduler_lease import FileSchedulerLease
from tests.test_job_array import _production_gateway
from tests.test_production_scheduler import (
    FakeRegistry,
    _config,
    _dt,
    _gfs_default_forecast_hours,
    _RealProductionScheduler,
    _set_db_free_scheduler_env,
    _write_db_free_file_provider_fixtures,
    _write_db_free_raw_manifest_fixture,
)

_FLAG_ENV = "NHMS_SCHEDULER_DB_FREE_REQUIRED"
_MODE_ERROR_NAME = "SchedulerModeInvariantError"
_MODE_ERROR_CODE = "scheduler_non_db_free_arm_reached"
_SELECTOR_ENVS = (
    "NHMS_SCHEDULER_STATE_BACKEND",
    "NHMS_SCHEDULER_LOCK_BACKEND",
    "NHMS_SCHEDULER_REGISTRY_BACKEND",
    "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
    "NHMS_SCHEDULER_JOURNAL_BACKEND",
    "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
)


def _set_flag_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(_FLAG_ENV, value)


def _forbid_db_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every SQL-path factory the non-DB-free arms used to call fails the test if reached."""

    def _fail(name: str) -> Callable[..., Any]:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            pytest.fail(f"DB-free scheduler reached the DB factory {name}")

        return _raise

    monkeypatch.setattr(
        scheduler_module.PsycopgModelRegistryStore, "from_env", staticmethod(_fail("PsycopgModelRegistryStore"))
    )
    monkeypatch.setattr(scheduler_module.StateManager, "from_env", staticmethod(_fail("StateManager.from_env")))


# --- 3.1 / 3.4: the env and the keyword no longer select the mode ------------------------


@pytest.mark.parametrize(
    ("flag_env", "keyword"),
    [
        pytest.param(None, None, id="flag-unset"),
        pytest.param("false", None, id="flag-false"),
        pytest.param("true", None, id="flag-true"),
        pytest.param(None, False, id="flag-unset-keyword-false"),
        pytest.param("false", False, id="flag-false-keyword-false"),
    ],
)
def test_from_env_builds_file_collaborators_whatever_the_flag_says(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag_env: str | None,
    keyword: bool | None,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=cycle_time,
    )
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    _set_flag_env(monkeypatch, flag_env)
    _forbid_db_factories(monkeypatch)
    kwargs: dict[str, Any] = {"now": cycle_time}
    if keyword is not None:
        kwargs["scheduler_db_free_required"] = keyword

    config = ProductionSchedulerConfig(**kwargs)
    scheduler = _RealProductionScheduler.from_env(config)

    assert config.scheduler_db_free_required is True
    assert config.db_free_required is True
    assert isinstance(scheduler.registry, FileSchedulerModelRegistry)
    assert isinstance(scheduler.active_repository, FileOrchestrationJournalRepository)
    assert set(scheduler.adapters) == set(config.sources)
    assert all(isinstance(adapter, NfsRawManifestCycleDiscoveryAdapter) for adapter in scheduler.adapters.values())
    assert isinstance(scheduler._build_scheduler_lease(), FileSchedulerLease)
    orchestrator = scheduler._default_orchestrator_for("gfs", state_manager=None)
    assert orchestrator.repository is scheduler.active_repository
    assert isinstance(orchestrator.retry_service, FileJournalRetryService)

    result = scheduler.run_once()

    assert result.status == "planned"
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["runtime_config"]["scheduler_db_free_required"] is True
    assert result.evidence["runtime_config"]["db_free_runtime"]["required"] is True
    assert result.evidence["runtime_config"]["db_free_runtime"]["required_env"] == _FLAG_ENV
    assert result.evidence["model_discovery"]["registry"]["status"] == "ready"


# --- 3.2 must-preserve: selectors and DATABASE_URL still fail closed before the lock -------


@pytest.mark.parametrize("selector_env", _SELECTOR_ENVS)
@pytest.mark.parametrize(
    ("selector_value", "expected_code"),
    [
        pytest.param(None, "db_free_selector_missing", id="unset"),
        pytest.param("", "db_free_selector_blank", id="blank"),
        pytest.param("postgres", "db_free_selector_db_backed", id="postgres"),
        pytest.param("psycopg", "db_free_selector_db_backed", id="psycopg"),
        pytest.param("sqlite", "db_free_selector_non_file", id="other"),
    ],
)
def test_selector_not_file_blocks_before_the_lock_with_the_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selector_env: str,
    selector_value: str | None,
    expected_code: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-root")
    _set_flag_env(monkeypatch, None)
    if selector_value is None:
        monkeypatch.delenv(selector_env)
    else:
        monkeypatch.setenv(selector_env, selector_value)
    _forbid_db_factories(monkeypatch)

    config = ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z"))
    result = _RealProductionScheduler.from_env(config).run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "db_free_runtime_preflight_blocked"
    assert result.evidence["lock"]["acquired"] is False
    assert result.evidence["lock"]["lock_type"] == "file"
    assert [(blocker["code"], blocker["field"]) for blocker in result.evidence["db_free_runtime"]["blockers"]] == [
        (expected_code, selector_env)
    ]
    assert not Path(config.lock_path).exists()


def test_database_url_still_forbidden_before_the_lock_with_the_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-root")
    _set_flag_env(monkeypatch, None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@db.invalid:5432/nhms")
    _forbid_db_factories(monkeypatch)

    config = ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z"))
    result = _RealProductionScheduler.from_env(config).run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "db_free_runtime_preflight_blocked"
    assert result.evidence["lock"]["acquired"] is False
    assert [blocker["code"] for blocker in result.evidence["db_free_runtime"]["blockers"]] == ["database_url_forbidden"]
    assert "secret" not in repr(result.evidence)
    assert not Path(config.lock_path).exists()


# --- 3.3: manifest -> gateway Slurm export env, with the variable absent everywhere --------


def test_gateway_exports_db_free_worker_switches_from_a_manifest_built_without_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in (
        _FLAG_ENV,
        "DATABASE_URL",
        "NHMS_CANONICAL_DB_FREE",
        "NHMS_FORCING_DB_FREE",
        "NHMS_SCHEDULER_REGISTRY_BACKEND",
        "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SLURM_SCHEDULER_STATE_INDEX",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SCHEDULER_STATE_INDEX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", str(tmp_path))
    workspace = tmp_path / "workspace"
    task = {
        "model_id": "model_001",
        "basin_version_id": "basin_0",
        "river_network_version_id": "river_0",
        "run_id": "fcst_gfs_2026050100_model_001",
        "source_id": "gfs",
        "cycle_time": "2026050100",
        "start_time": "2026-05-01T00:00:00Z",
        "end_time": "2026-05-08T00:00:00Z",
        "forcing_version_id": "forcing_0",
        "forcing_package_uri": "s3://nhms/forcing/basin_0.tar",
        "workspace_dir": str(workspace),
    }
    orchestrator = types.SimpleNamespace(
        config=OrchestratorConfig(
            workspace_root=workspace,
            object_store_root=tmp_path / "object-store",
            object_store_prefix="s3://nhms",
        ),
        _reindexed_manifest_entries=lambda basins: [dict(basin) for basin in basins],
        _forecast_scenario_id=lambda _source_id: "forecast_gfs_deterministic",
    )
    cycle_time = _dt("2026-05-01T00:00:00Z")
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id=f"gfs_{format_cycle_time(cycle_time)}",
        run_id="cycle_gfs_2026050100",
        all_basins=[task],
        active_basins=[task],
    )
    stage = StageDefinition(
        "forcing",
        "produce_forcing_array",
        "produce_forcing_array.sbatch",
        "forcing_ready",
        "failed_forcing",
        is_array=True,
    )

    manifest = build_cycle_stage_manifest(orchestrator, stage, context)
    gateway = _production_gateway(tmp_path)
    captured: dict[str, str] = {}

    def fake_sbatch(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 4242\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_sbatch)
    gateway.submit_job_array(
        stage.job_type,
        cycle_id=context.cycle_id,
        stage_name=stage.stage,
        tasks=manifest["manifest_index"],
        manifest=manifest,
    )
    script = captured["script"]

    assert "export NHMS_CANONICAL_DB_FREE=true" in script
    assert "export NHMS_FORCING_DB_FREE=true" in script
    assert "export NHMS_SCHEDULER_REGISTRY_BACKEND=file" in script
    assert f"export {_FLAG_ENV}=true" in script
    assert "export NHMS_CANONICAL_REPOSITORY_BACKEND=file" in script
    assert "export NHMS_FORCING_REPOSITORY_BACKEND=file" in script
    assert manifest["scheduler_db_free_required"] == "true"


def test_candidate_runtime_evidence_shim_reports_db_free_without_a_config(tmp_path: Path) -> None:
    artifact_path = tmp_path / "evidence" / "pass.json"

    payload = scheduler_candidate_runtime_module._evidence_reservation_blocked_payload(
        pass_id="scheduler_pass",
        artifact_path=artifact_path,
        reason="evidence_write_precheck_failed",
    )

    assert payload["artifact_path"] == "[local-path]"
    assert str(artifact_path) not in repr(payload)


# --- 3.2 / 4.3: the typed refusal and the file-only scheduler lease ---------------------


def test_scheduler_mode_invariant_error_is_a_direct_exception_subclass() -> None:
    from services.orchestrator.scheduler_mode_invariant import SchedulerModeInvariantError

    error = SchedulerModeInvariantError(site="scheduler_core:_default_orchestrator_for")

    assert SchedulerModeInvariantError.__bases__ == (Exception,)
    assert not isinstance(error, ValueError | RuntimeError | OSError)
    assert error.code == _MODE_ERROR_CODE
    assert error.site == "scheduler_core:_default_orchestrator_for"
    assert _MODE_ERROR_CODE in str(error)
    assert "scheduler_core:_default_orchestrator_for" in str(error)


def _db_free_scheduler(tmp_path: Path, *, config_kwargs: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    config = _config(tmp_path, db_free=True, scheduler_journal_root=str(tmp_path / "journal"), **(config_kwargs or {}))
    values: dict[str, Any] = {
        "registry": FakeRegistry([]),
        "adapters": {},
        "active_repository": FileOrchestrationJournalRepository(tmp_path / "journal"),
        "canonical_readiness_provider": object(),
    }
    values.update(kwargs)
    return _RealProductionScheduler(config, **values)


def _assert_mode_error(excinfo: pytest.ExceptionInfo[BaseException], site: str) -> None:
    error = excinfo.value
    assert type(error).__name__ == _MODE_ERROR_NAME, repr(error)
    assert type(error).__bases__ == (Exception,)
    assert getattr(error, "code", None) == _MODE_ERROR_CODE
    assert getattr(error, "site", None) == site


_DELETED_LEASE_NAMES = (
    "PostgresSchedulerLease",
    "_postgres_advisory_lock_key",
    "RECONCILE_DB_CONNECT_TIMEOUT_SECONDS",
)


def test_scheduler_lease_is_the_file_lease_even_for_a_postgres_lock_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # R2c-3 (#3258) deleted the PostgreSQL scheduler lease: a config that still names the
    # postgres lock backend and carries a database URL gets the file lease, and taking it
    # never imports a PostgreSQL driver.
    monkeypatch.delenv("NHMS_SCHEDULER_LEASE_GUARD_MODE", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE", raising=False)
    scheduler = _db_free_scheduler(
        tmp_path,
        config_kwargs={
            "database_url": "postgresql://nhms:secret@db.invalid:5432/nhms",
            "scheduler_lock_backend": "postgres",
        },
    )
    config = scheduler.config
    assert config.scheduler_lock_backend == "postgres"
    assert config.database_url == "postgresql://nhms:secret@db.invalid:5432/nhms"
    _forbid_db_factories(monkeypatch)
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    monkeypatch.setitem(sys.modules, "psycopg", None)

    lease = scheduler._build_scheduler_lease()

    assert type(lease) is FileSchedulerLease
    assert lease.lock_path == Path(config.lock_path)
    assert lease.ttl_seconds == config.lock_ttl_seconds
    assert lease.workspace_root == Path(config.workspace_root)
    assert lease.acquired is False
    assert not Path(config.lock_path).exists()

    acquired = lease.acquire(pass_id="pass-file-only", started_at=_dt("2026-05-21T12:00:00Z"))
    try:
        assert acquired["acquired"] is True
        assert acquired["lock_type"] == "file"
        assert Path(config.lock_path).is_file()
    finally:
        lease.release(pass_id="pass-file-only")
    assert not Path(config.lock_path).exists()

    for name in _DELETED_LEASE_NAMES:
        assert not hasattr(scheduler_module, name), name
        assert not hasattr(scheduler_lease_module, name), name
        assert name not in scheduler_lease_module.__all__


def test_db_free_default_orchestrator_without_a_repository_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Intended DB-free behaviour change (scheduler_core ``_default_orchestrator_for``):
    # a DB-free scheduler with no journal repository used to fall back to the SQL
    # orchestrator-repository factory (deleted by R3, #3260); it now refuses with the
    # typed error.
    scheduler = _db_free_scheduler(tmp_path)
    scheduler.active_repository = None
    _forbid_db_factories(monkeypatch)

    with pytest.raises(Exception) as excinfo:
        scheduler._default_orchestrator_for("gfs", state_manager=None)

    _assert_mode_error(excinfo, "scheduler_core:_default_orchestrator_for")


class _ReadOnlyJournal:
    supports_writes = False


class _WritableJournal:
    supports_writes = True


def test_db_free_journal_mutation_gate_keeps_its_db_free_outcomes(tmp_path: Path) -> None:
    config = _config(tmp_path, db_free=True)

    assert scheduler_runtime_module._db_free_journal_mutation_blocked(config, mutation_requested=False) is False
    assert (
        scheduler_runtime_module._db_free_journal_mutation_blocked(
            config, mutation_requested=False, repository=_ReadOnlyJournal()
        )
        is False
    )
    assert (
        scheduler_runtime_module._db_free_journal_mutation_blocked(
            config, mutation_requested=True, repository=_ReadOnlyJournal()
        )
        is True
    )
    assert (
        scheduler_runtime_module._db_free_journal_mutation_blocked(
            config, mutation_requested=True, repository=_WritableJournal()
        )
        is False
    )
    assert scheduler_runtime_module._db_free_journal_mutation_blocked(config, mutation_requested=True) is True


def test_db_free_lock_evidence_keeps_its_db_free_redaction(tmp_path: Path) -> None:
    config = _config(tmp_path, db_free=True)

    evidence = scheduler_runtime_module._db_free_lock_evidence(
        config,
        {"acquired": False, "contention": False, "lock_path": str(tmp_path / "x.lock"), "lock_type": "file"},
    )

    assert evidence == {"acquired": False, "contention": False, "lock_path": "[local-path]", "lock_type": "file"}


def test_db_free_lineage_provider_keeps_its_file_index_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scheduler = _db_free_scheduler(tmp_path)
    monkeypatch.setattr(
        "packages.common.state_manager.PsycopgStateSnapshotRepository.from_env",
        staticmethod(lambda: pytest.fail("DB-free lineage must not reach the SQL repository")),
    )

    provider = scheduler._lineage_provider()

    assert provider is scheduler._db_free_state_index_provider()


# --- R2c-1 (#3256, task 4.1): the SQL-path factories are deleted, not just unreachable -----

_DELETED_SQL_FACTORY_NAMES = (
    "_default_adapters",
    "_canonical_readiness_provider_from_env",
    "_forcing_producer_from_env",
)


@pytest.mark.parametrize("name", _DELETED_SQL_FACTORY_NAMES)
def test_deleted_sql_factories_are_gone_from_the_facade_and_owner_modules(name: str) -> None:
    from services.orchestrator import scheduler_adapters as scheduler_adapters_module
    from services.orchestrator import scheduler_core as scheduler_core_module

    assert not hasattr(scheduler_module, name)
    assert not hasattr(scheduler_adapters_module, name)
    assert not hasattr(scheduler_core_module, name)
    # The DB-free adapter factory is the one that stays.
    assert callable(scheduler_module._db_free_default_adapters)
    assert scheduler_module._db_free_default_adapters is scheduler_adapters_module._db_free_default_adapters
