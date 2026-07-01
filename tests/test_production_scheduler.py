from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import publish_state_snapshot_index
from services.orchestrator import chain_repository_state as chain_repository_state_module
from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_discovery as scheduler_discovery_module
from services.orchestrator import scheduler_evidence as scheduler_evidence_module
from services.orchestrator import scheduler_execution as scheduler_execution_module
from services.orchestrator import scheduler_lease as scheduler_lease_module
from services.orchestrator import scheduler_state as scheduler_state_module
from services.orchestrator.chain import (
    M3_STAGES,
    OrchestratorConfig,
    PipelineResult,
    StageRunResult,
    build_model_run_assembly,
)
from services.orchestrator.production_contract import (
    PRODUCTION_STAGE_TAXONOMY,
    PRODUCTION_STATUS_TAXONOMY,
    ProductionContractError,
    production_contract_matrix,
    production_stage_for,
    production_status_for,
    validate_display_artifact_evidence,
    validate_display_readable_uri,
    validate_same_production_identity,
)
from services.orchestrator.scheduler import (
    LOCK_OWNER,
    LOCK_SCHEMA_VERSION,
    MAX_CONTINUOUS_JSON_PASSES,
    MAX_DISCOVERED_CYCLES,
    MAX_LOCK_PAYLOAD_BYTES,
    MAX_MODEL_RUN_STAGE_TASK_ROWS,
    MODEL_RUN_EVIDENCE_SCHEMA_VERSION,
    SCHEDULER_EVIDENCE_GITHUB_ISSUE,
    SCHEDULER_EVIDENCE_SCHEMA_VERSION,
    FileSchedulerLease,
    PostgresSchedulerLease,
    ProductionSchedulerConfig,
    SchedulerEvidenceWriteError,
    SchedulerPassResult,
    _default_owner_liveness_probe,
    _LeaseHeartbeat,
)
from services.orchestrator.scheduler import (
    ProductionScheduler as _RealProductionScheduler,
)
from services.orchestrator.source_cycle_raw_manifest import nfs_raw_manifest_readiness
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from workers.canonical_converter.converter import GFS_REQUIRED_STANDARD_VARIABLES, evaluate_canonical_readiness
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time
from workers.shud_runtime import runtime as shud_runtime_module

_TEST_CANONICAL_READINESS_PROVIDER_UNSET = object()
_TEST_OBJECT_STORE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")).resolve() / "nhms-test-object-store"


class _NoopReconcileStore:
    def query_reserved_unbound_jobs(self) -> list[Any]:
        return []

    def query_inflight_jobs(self) -> list[Any]:
        return []

    def bind_reservation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("noop reconcile store must not bind reservations")

    def update_job_status(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("noop reconcile store must not update job status")


def _write_valid_shud_executable(directory: Path) -> Path:
    """Create a non-stub, executable, SHUD-identifying binary stand-in.

    Mirrors the real compiled SHUD binary: flags (--version/--help) report
    "Unknown option" with no token, and only a no-argument invocation prints the
    identity banner, so the shared preflight treats it as a real solver. ``ldd``
    on a shell script reports "not a dynamic executable" (Linux) or is absent
    (macOS), so the shared-library probe never produces a false blocker for it.
    """

    path = directory / "shud_omp"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -gt 0 ]; then\n'
        '  echo "Unknown option: $1" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "Simulator for Hydrologic Unstructured Domains v2.0  2022"\n'
        'echo "./shud [-0gv] [-p project_file] [-o output] <project_name>"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _valid_shud_executable_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: Any) -> None:
    """Default every scheduler test to a valid SHUD executable.

    The pre-submit SHUD preflight (#257) blocks stub/missing executables before
    Slurm submission. Tests exercising the happy submission path therefore need a
    real executable configured; the stub-rejection tests override this env var
    explicitly with monkeypatch.setenv.
    """

    bin_dir = tmp_path_factory.mktemp("shud_bin")
    executable = _write_valid_shud_executable(bin_dir)
    monkeypatch.setenv("SHUD_EXECUTABLE", str(executable))


class _AlwaysReadyCanonicalReadinessProvider:
    def canonical_readiness(self, **kwargs: Any) -> Mapping[str, Any]:
        return {
            "status": "canonical_ready",
            "ready": True,
            "reason": None,
            "source_id": kwargs["source_id"],
            "cycle_time": kwargs["cycle_time"],
            "forecast_hours": list(kwargs["forecast_hours"]),
            "policy_identity": dict(kwargs["policy_identity"]),
            "source_object_identity": dict(kwargs["source_object_identity"]),
            "canonical_product_id": kwargs["canonical_product_id"],
            "model_id": kwargs["model_id"],
            "basin_id": kwargs["basin_id"],
        }


def _noop_reconcile_comment_query(_idempotency_key: str) -> None:
    return None


def _noop_reconcile_sacct_query(_slurm_job_id: str) -> None:
    return None


class ProductionScheduler(_RealProductionScheduler):
    def __init__(
        self,
        config: ProductionSchedulerConfig | None = None,
        *,
        registry: Any | None = None,
        adapters: Mapping[str, Any] | None = None,
        active_repository: Any | None = None,
        canonical_readiness_provider: Any = _TEST_CANONICAL_READINESS_PROVIDER_UNSET,
        forcing_producer: Any | None = None,
        orchestrator_factory: Any | None = None,
        sleep: Any | None = None,
        reconcile_store: Any | None = None,
        reconcile_comment_query: Any | None = None,
        reconcile_sacct_query: Any | None = None,
    ) -> None:
        self._test_canonical_readiness_omitted = (
            canonical_readiness_provider is _TEST_CANONICAL_READINESS_PROVIDER_UNSET
        )
        if canonical_readiness_provider is _TEST_CANONICAL_READINESS_PROVIDER_UNSET:
            canonical_readiness_provider = _AlwaysReadyCanonicalReadinessProvider()
        if reconcile_store is None:
            reconcile_store = _NoopReconcileStore()
        if reconcile_comment_query is None:
            reconcile_comment_query = _noop_reconcile_comment_query
        if reconcile_sacct_query is None:
            reconcile_sacct_query = _noop_reconcile_sacct_query
        super().__init__(
            config,
            registry=registry,
            adapters=adapters,
            active_repository=active_repository,
            canonical_readiness_provider=canonical_readiness_provider,
            forcing_producer=forcing_producer,
            orchestrator_factory=orchestrator_factory,
            sleep=sleep,
            reconcile_store=reconcile_store,
            reconcile_comment_query=reconcile_comment_query,
            reconcile_sacct_query=reconcile_sacct_query,
        )

    def _canonical_readiness_for_candidate(self, candidate: Any, cycle: Any) -> dict[str, Any] | None:
        if self._test_canonical_readiness_omitted:
            return None
        return super()._canonical_readiness_for_candidate(candidate, cycle)


def _scheduler_inventory_text() -> str:
    return (
        Path(__file__).resolve().parents[1] / "docs" / "governance" / "SCHEDULER_COMPATIBILITY_INVENTORY.md"
    ).read_text(encoding="utf-8")


def _scheduler_inventory_governed_group_ids(inventory_text: str) -> tuple[str, ...]:
    section = inventory_text.split("## Governed Groups", 1)[1].split("## Guard Hook Seed", 1)[0]
    return tuple(line.split("`", 2)[1] for line in section.splitlines() if line.startswith("| `"))


def test_M3_STAGES_PipelineResult_StageRunResult_legacy_exports_preserve_identity() -> None:
    import services.orchestrator.chain as legacy_chain
    from services.orchestrator import chain_stages, chain_types

    assert M3_STAGES is legacy_chain.M3_STAGES
    assert M3_STAGES is chain_stages.M3_STAGES
    assert PipelineResult is legacy_chain.PipelineResult
    assert PipelineResult is chain_types.PipelineResult
    assert StageRunResult is legacy_chain.StageRunResult
    assert StageRunResult is chain_types.StageRunResult

    stage_result = StageRunResult(
        stage=M3_STAGES[0].stage,
        job_type=M3_STAGES[0].job_type,
        pipeline_job_id="pipeline-job-1",
        slurm_job_id="slurm-job-1",
        status="succeeded",
    )
    result = PipelineResult(
        run_id="run-1",
        cycle_id="gfs_2026050100",
        status="complete",
        stages=(stage_result,),
    )

    assert result.stages[0].stage == "convert"
    assert result.candidate_outcomes == ()


def test_scheduler_state_compat_reexport_names_match_owner_module_and_inventory() -> None:
    reexport_names = scheduler_module._SCHEDULER_STATE_COMPAT_REEXPORT_NAMES
    wrapper_names = scheduler_module._SCHEDULER_STATE_COMPAT_WRAPPER_NAMES
    direct_names = tuple(name for name in reexport_names if name not in wrapper_names)

    assert len(reexport_names) == len(set(reexport_names))
    assert set(wrapper_names).issubset(reexport_names)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_STATE_COMPAT_EXPORT_NAMES)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_STATE_COMPAT_ORIGINALS)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_STATE_COMPAT_WRAPPERS)
    assert set(reexport_names) == set(scheduler_module._SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS)
    assert set(reexport_names) == set(scheduler_module._SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS)
    assert scheduler_module._SCHEDULER_STATE_COMPAT_EXPORTS == tuple(
        scheduler_module._SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS[name] for name in reexport_names
    )

    for name in reexport_names:
        assert hasattr(scheduler_state_module, name)
        assert scheduler_module._SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS[name] is getattr(
            scheduler_state_module,
            name,
        )
        assert scheduler_module._SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS[name] is getattr(
            scheduler_module,
            name,
        )
    for name in wrapper_names:
        assert scheduler_module._SCHEDULER_STATE_COMPAT_ORIGINALS[name] is getattr(scheduler_state_module, name)
        assert scheduler_module._SCHEDULER_STATE_COMPAT_WRAPPERS[name] is getattr(scheduler_module, name)
    for name in direct_names:
        assert getattr(scheduler_module, name) is getattr(scheduler_state_module, name)

    inventory_text = _scheduler_inventory_text()
    assert _scheduler_inventory_governed_group_ids(inventory_text) == (
        "scheduler-state-monkeypatch-bindings",
        "candidate-state-reexports",
        "state-manager-facade-reexports",
        "scheduler-lease-reexports",
        "scheduler-types-reexport",
        "scheduler-runtime-roots-forwarders",
        "scheduler-config-reexport",
        "scheduler-adapter-provider-reexports",
        "discovery-compat-aliases",
        "scheduler-model-discovery-forwarders",
        "candidate-construction-compat-aliases",
        "execution-restart-cohort-wrappers",
        "scheduler-candidate-quality-forwarders",
        "cancellation-status-proof-wrappers",
        "scheduler-preflight-compat",
        "scheduler-gateway-forwarders",
    )
    for token in (
        "_SCHEDULER_STATE_COMPAT_REEXPORT_NAMES",
        "_SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS",
        "_SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS",
        "_SCHEDULER_STATE_COMPAT_WRAPPER_NAMES",
    ):
        assert token in inventory_text


def test_scheduler_lease_compat_reexport_names_match_owner_module_and_inventory() -> None:
    reexport_names = scheduler_module._SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES
    lookup_names = scheduler_module._SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES

    assert len(reexport_names) == len(set(reexport_names))
    assert len(lookup_names) == len(set(lookup_names))
    assert set(lookup_names).issubset(reexport_names)
    assert set(reexport_names) == set(scheduler_lease_module.__all__)
    assert set(reexport_names) == set(scheduler_module._SCHEDULER_LEASE_COMPAT_OWNER_REEXPORTS)
    assert set(reexport_names) == set(scheduler_module._SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS)
    assert scheduler_module._SCHEDULER_LEASE_COMPAT_EXPORTS == tuple(
        scheduler_module._SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS[name] for name in reexport_names
    )

    for name in reexport_names:
        assert hasattr(scheduler_lease_module, name)
        assert scheduler_module._SCHEDULER_LEASE_COMPAT_OWNER_REEXPORTS[name] is getattr(
            scheduler_lease_module,
            name,
        )
        assert scheduler_module._SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS[name] is getattr(
            scheduler_module,
            name,
        )
        assert getattr(scheduler_module, name) is getattr(scheduler_lease_module, name)
    for name in lookup_names:
        assert callable(getattr(scheduler_lease_module, name))

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES",
        "_SCHEDULER_LEASE_COMPAT_OWNER_REEXPORTS",
        "_SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS",
        "_SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES",
    ):
        assert token in inventory_text


def test_scheduler_lease_compat_lookup_names_resolve_scheduler_monkeypatches(
    monkeypatch: Any,
) -> None:
    for name in scheduler_module._SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES:
        fallback = getattr(scheduler_lease_module, name)

        def patched(*_args: Any, **_kwargs: Any) -> None:
            return None

        with monkeypatch.context() as patch_context:
            patch_context.setattr(scheduler_module, name, patched)
            assert scheduler_lease_module._scheduler_compat_function(name, fallback) is patched


def test_scheduler_discovery_compat_aliases_match_owner_module_and_inventory() -> None:
    alias_owner_names = scheduler_module._SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES
    alias_names = scheduler_module._SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES
    forwarder_names = scheduler_module._SCHEDULER_DISCOVERY_COMPAT_FORWARDER_NAMES

    assert len(alias_names) == len(set(alias_names))
    assert len(forwarder_names) == len(set(forwarder_names))
    assert tuple(alias_owner_names) == alias_names
    assert set(alias_names) == set(scheduler_module._SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES)
    assert set(alias_names) == set(scheduler_module._SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES)

    for facade_name, owner_name in alias_owner_names.items():
        assert hasattr(scheduler_discovery_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES[facade_name] is getattr(
            scheduler_discovery_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )
        assert getattr(scheduler_module, facade_name) is getattr(scheduler_discovery_module, owner_name)
    for method_name in forwarder_names:
        assert hasattr(scheduler_module.ProductionScheduler, method_name)

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES",
        "_SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES",
    ):
        assert token in inventory_text


def test_scheduler_discovery_compat_forwarders_delegate_to_owner_module(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    discovery = CycleDiscovery(
        cycle_id="gfs_2026052106",
        source_id="gfs",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
        cycle_hour=6,
        available=True,
        status="discovered",
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    contexts: list[scheduler_discovery_module.SchedulerDiscoveryContext] = []

    def fake_cycle_completion_status(
        context: scheduler_discovery_module.SchedulerDiscoveryContext,
        discovery_arg: CycleDiscovery,
        models: Sequence[Any],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        contexts.append(context)
        assert discovery_arg is discovery
        assert models == ()
        assert horizon == {"max_lead_hours": 168}
        return "complete"

    def fake_discover_source_window(
        adapter: Any,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        del adapter
        assert source_id == "gfs"
        assert start_time <= discovery.cycle_time <= end_time
        return [discovery]

    def fake_discover_cycles(
        context: scheduler_discovery_module.SchedulerDiscoveryContext,
        started_at: datetime,
        models: Sequence[Any] = (),
    ) -> tuple[list[scheduler_discovery_module.SchedulerSourceCycle], list[dict[str, Any]]]:
        contexts.append(context)
        assert started_at is now
        assert models == ()
        assert context.discover_source_window_provider is not None
        assert context.cycle_completion_status_provider is not None
        return [scheduler_discovery_module.SchedulerSourceCycle(discovery=discovery, horizon={})], [{"delegated": True}]

    monkeypatch.setattr(scheduler_discovery_module, "cycle_completion_status", fake_cycle_completion_status)
    assert (
        scheduler._cycle_completion_status(
            discovery,
            (),
            horizon={"max_lead_hours": 168},
        )
        == "complete"
    )

    monkeypatch.setattr(scheduler_discovery_module, "discover_source_window", fake_discover_source_window)
    assert scheduler._discover_source_window(
        scheduler.adapters["gfs"],
        source_id="gfs",
        start_time=_dt("2026-05-21T00:00:00Z"),
        end_time=now,
    ) == [discovery]

    monkeypatch.setattr(scheduler_discovery_module, "discover_cycles", fake_discover_cycles)
    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert cycles == [scheduler_discovery_module.SchedulerSourceCycle(discovery=discovery, horizon={})]
    assert evidence == [{"delegated": True}]
    assert contexts


def test_scheduler_candidate_compat_aliases_match_owner_module_and_inventory() -> None:
    alias_owner_names = scheduler_module._SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES
    alias_names = scheduler_module._SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES
    forwarder_names = scheduler_module._SCHEDULER_CANDIDATE_COMPAT_FORWARDER_NAMES

    assert len(alias_names) == len(set(alias_names))
    assert len(forwarder_names) == len(set(forwarder_names))
    assert tuple(alias_owner_names) == alias_names
    assert scheduler_module._SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING == ()
    assert scheduler_module._SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING == ()
    assert set(alias_names) == set(scheduler_module._SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES)
    assert set(alias_names) == set(scheduler_module._SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES)

    for facade_name, owner_name in alias_owner_names.items():
        assert hasattr(scheduler_candidates_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES[facade_name] is getattr(
            scheduler_candidates_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )
        assert getattr(scheduler_module, facade_name) is getattr(scheduler_candidates_module, owner_name)
    for method_name in forwarder_names:
        assert hasattr(scheduler_module.ProductionScheduler, method_name)

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING",
        "_SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING",
        "_SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES",
        "_SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES",
    ):
        assert token in inventory_text
    for token in (*alias_names, *forwarder_names):
        assert token in inventory_text


def test_scheduler_candidate_compat_forwarders_delegate_to_owner_module(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
    )
    model = scheduler_module._coerce_registered_model(_model("model_a", "basin_a"))
    cycle = scheduler_module.SchedulerSourceCycle(
        discovery=CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        horizon={"max_lead_hours": 24},
    )
    captured: dict[str, Any] = {}
    sentinel = (
        ["candidate-from-owner"],
        ["blocked-from-owner"],
        [{"skipped": True}],
        [{"duplicate": True}],
        [{"sync": True}],
    )

    def patched_decision(
        candidate: Any,
        state: Mapping[str, Any] | None,
    ) -> scheduler_module.CandidateStateDecision | None:
        del candidate, state
        return None

    def fake_build_candidates(
        context: scheduler_candidates_module.SchedulerCandidateConstructionContext,
        *,
        models: Sequence[Any],
        cycles: Sequence[Any],
        allow_slurm_status_sync: bool = False,
    ) -> tuple[list[Any], list[Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        captured["context"] = context
        captured["models"] = models
        captured["cycles"] = cycles
        captured["allow_slurm_status_sync"] = allow_slurm_status_sync
        return sentinel

    monkeypatch.setattr(scheduler_module, "MAX_CANDIDATES", 7)
    monkeypatch.setattr(scheduler_module, "_candidate_state_decision", patched_decision)
    monkeypatch.setattr(scheduler_candidates_module, "build_candidates", fake_build_candidates)

    result = scheduler._build_candidates(
        models=[model],
        cycles=[cycle],
        allow_slurm_status_sync=True,
    )

    assert result is sentinel
    assert captured["models"] == [model]
    assert captured["cycles"] == [cycle]
    assert captured["allow_slurm_status_sync"] is True
    context = captured["context"]
    assert isinstance(context, scheduler_candidates_module.SchedulerCandidateConstructionContext)
    assert context.config is scheduler.config
    assert context.active_repository is scheduler.active_repository
    assert context.max_candidates == 7
    assert context.candidate_state_decider is patched_decision


def test_scheduler_execution_compat_wrappers_match_owner_module_and_inventory() -> None:
    wrapper_owner_names = scheduler_module._SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES
    wrapper_names = scheduler_module._SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES
    forwarder_owner_names = scheduler_module._SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES
    forwarder_names = scheduler_module._SCHEDULER_EXECUTION_COMPAT_FORWARDER_NAMES

    assert len(wrapper_names) == len(set(wrapper_names))
    assert len(forwarder_names) == len(set(forwarder_names))
    assert tuple(wrapper_owner_names) == wrapper_names
    assert tuple(forwarder_owner_names) == forwarder_names
    assert scheduler_module._SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING == ()
    assert scheduler_module._SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING == ()
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_EXECUTION_COMPAT_OWNER_WRAPPERS)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_EXECUTION_COMPAT_FACADE_WRAPPERS)

    for facade_name, owner_name in wrapper_owner_names.items():
        assert hasattr(scheduler_execution_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_EXECUTION_COMPAT_OWNER_WRAPPERS[facade_name] is getattr(
            scheduler_execution_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_EXECUTION_COMPAT_FACADE_WRAPPERS[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )
    for method_name, owner_name in forwarder_owner_names.items():
        assert hasattr(scheduler_execution_module, owner_name)
        assert hasattr(scheduler_module.ProductionScheduler, method_name)

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING",
        "_SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING",
        "_SCHEDULER_EXECUTION_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_EXECUTION_COMPAT_FACADE_WRAPPERS",
    ):
        assert token in inventory_text
    for token in (*wrapper_names, *forwarder_names):
        assert token in inventory_text


def test_scheduler_execution_compat_forwarders_delegate_to_owner_module(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    cycle_time = _dt("2026-05-21T06:00:00Z")
    candidates = [object()]
    contexts: list[scheduler_execution_module.SchedulerExecutionContext] = []

    def fake_produce_forcing_for_candidates(
        context: scheduler_execution_module.SchedulerExecutionContext,
        candidates_arg: Sequence[Any],
    ) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
        contexts.append(context)
        assert candidates_arg is candidates
        return (["ready-from-owner"], ["blocked-from-owner"], [{"forcing": True}])

    def fake_execute_candidates(
        context: scheduler_execution_module.SchedulerExecutionContext,
        candidates_arg: Sequence[Any],
    ) -> list[dict[str, Any]]:
        contexts.append(context)
        assert candidates_arg is candidates
        return [{"executed": True}]

    def fake_execute_candidate_cohort(
        context: scheduler_execution_module.SchedulerExecutionContext,
        source_id: str,
        cycle_time_arg: datetime,
        cycle_id: str,
        cycle_candidates: Sequence[Any],
        *,
        orchestration_run_id: str | None,
    ) -> list[dict[str, Any]]:
        contexts.append(context)
        assert source_id == "gfs"
        assert cycle_time_arg is cycle_time
        assert cycle_id == "gfs_2026052106"
        assert cycle_candidates is candidates
        assert orchestration_run_id == "cycle-gfs-restart"
        return [{"cohort": True}]

    monkeypatch.setattr(
        scheduler_execution_module,
        "produce_forcing_for_candidates",
        fake_produce_forcing_for_candidates,
    )
    assert scheduler._produce_forcing_for_candidates(candidates) == (
        ["ready-from-owner"],
        ["blocked-from-owner"],
        [{"forcing": True}],
    )

    monkeypatch.setattr(scheduler_execution_module, "execute_candidates", fake_execute_candidates)
    assert scheduler._execute_candidates(candidates) == [{"executed": True}]

    monkeypatch.setattr(scheduler_execution_module, "execute_candidate_cohort", fake_execute_candidate_cohort)
    assert scheduler._execute_candidate_cohort(
        "gfs",
        cycle_time,
        "gfs_2026052106",
        candidates,
        orchestration_run_id="cycle-gfs-restart",
    ) == [{"cohort": True}]

    assert contexts
    assert all(isinstance(context, scheduler_execution_module.SchedulerExecutionContext) for context in contexts)
    assert all(context.config is scheduler.config for context in contexts)
    assert all(
        context.restart_compatible_candidate_cohorts is scheduler_module._restart_compatible_candidate_cohorts
        for context in contexts
    )
    assert all(
        context.candidate_execution_cohorts is scheduler_module._candidate_execution_cohorts for context in contexts
    )


def test_scheduler_execution_compat_wrappers_delegate_to_owner_module(monkeypatch: Any) -> None:
    candidate = object()
    candidates = [candidate]
    cycle_time = _dt("2026-05-21T06:00:00Z")

    with monkeypatch.context() as patch_context:

        def patched_stage(candidate_arg: Any) -> str:
            del candidate_arg
            return "forecast"

        def patched_key(restart_stage: str | None) -> tuple[int, str]:
            return (1, restart_stage or "full")

        def fake_restart_compatible_candidate_cohorts(
            candidates_arg: Sequence[Any],
            *,
            candidate_restart_stage: Any,
            candidate_restart_cohort_key: Any,
        ) -> list[tuple[tuple[int, str], list[Any]]]:
            assert candidates_arg is candidates
            assert candidate_restart_stage is patched_stage
            assert candidate_restart_cohort_key is patched_key
            return [((1, "forecast"), candidates)]

        patch_context.setattr(scheduler_module, "_candidate_restart_stage", patched_stage)
        patch_context.setattr(scheduler_module, "_candidate_restart_cohort_key", patched_key)
        patch_context.setattr(
            scheduler_execution_module,
            "restart_compatible_candidate_cohorts",
            fake_restart_compatible_candidate_cohorts,
        )
        assert scheduler_module._restart_compatible_candidate_cohorts(candidates) == [((1, "forecast"), candidates)]

    with monkeypatch.context() as patch_context:

        def patched_fresh(candidate_arg: Any) -> bool:
            del candidate_arg
            return False

        def patched_downstream(stage: str) -> str:
            del stage
            return "forecast"

        def fake_candidate_restart_stage(
            candidate_arg: Any,
            *,
            candidate_is_fresh_full_chain: Any,
            native_shud_stage_aliases: Any,
            canonical_downstream_stage: Any,
        ) -> str:
            assert candidate_arg is candidate
            assert candidate_is_fresh_full_chain is patched_fresh
            assert native_shud_stage_aliases is scheduler_module.NATIVE_SHUD_STAGE_ALIASES
            assert canonical_downstream_stage is patched_downstream
            return "forecast"

        patch_context.setattr(scheduler_module, "_candidate_is_fresh_full_chain", patched_fresh)
        patch_context.setattr(scheduler_module, "_canonical_downstream_stage", patched_downstream)
        patch_context.setattr(scheduler_execution_module, "candidate_restart_stage", fake_candidate_restart_stage)
        assert scheduler_module._candidate_restart_stage(candidate) == "forecast"

    def fake_candidate_restart_cohort_key(
        restart_stage: str | None,
        *,
        downstream_restart_stages: Sequence[str] = (),
    ) -> tuple[int, str]:
        assert restart_stage == "forecast"
        assert downstream_restart_stages == scheduler_module.DOWNSTREAM_RESTART_STAGES
        return (1, "forecast")

    monkeypatch.setattr(
        scheduler_execution_module,
        "candidate_restart_cohort_key",
        fake_candidate_restart_cohort_key,
    )
    assert scheduler_module._candidate_restart_cohort_key("forecast") == (1, "forecast")

    with monkeypatch.context() as patch_context:

        def patched_format_cycle_time(cycle_time_arg: datetime) -> str:
            del cycle_time_arg
            return "2026052106"

        def fake_candidate_execution_cohort_run_id(
            source_id: str,
            cycle_time_arg: datetime,
            cohort_key: tuple[int, str],
            *,
            format_cycle_time: Any,
        ) -> str:
            assert source_id == "gfs"
            assert cycle_time_arg is cycle_time
            assert cohort_key == (1, "forecast")
            assert format_cycle_time is patched_format_cycle_time
            return "cycle-gfs-forecast"

        patch_context.setattr(scheduler_module, "format_cycle_time", patched_format_cycle_time)
        patch_context.setattr(
            scheduler_execution_module,
            "candidate_execution_cohort_run_id",
            fake_candidate_execution_cohort_run_id,
        )
        assert (
            scheduler_module._candidate_execution_cohort_run_id("gfs", cycle_time, (1, "forecast"))
            == "cycle-gfs-forecast"
        )

    with monkeypatch.context() as patch_context:

        def patched_run_id_for_candidate(
            source_id: str,
            cycle_time_arg: datetime,
            cohort_key: tuple[int, str],
            candidate_arg: Any,
        ) -> str:
            del source_id, cycle_time_arg, cohort_key, candidate_arg
            return "run-for-candidate"

        def fake_candidate_execution_cohorts(
            source_id: str,
            cycle_time_arg: datetime,
            cohort_key: tuple[int, str],
            candidates_arg: Sequence[Any],
            *,
            run_id_for_candidate: Any,
        ) -> list[tuple[list[Any], str | None]]:
            assert source_id == "gfs"
            assert cycle_time_arg is cycle_time
            assert cohort_key == (1, "forecast")
            assert candidates_arg is candidates
            assert run_id_for_candidate is patched_run_id_for_candidate
            return [(candidates, "run-for-candidate")]

        patch_context.setattr(
            scheduler_module,
            "_candidate_execution_cohort_run_id_for_candidate",
            patched_run_id_for_candidate,
        )
        patch_context.setattr(
            scheduler_execution_module,
            "candidate_execution_cohorts",
            fake_candidate_execution_cohorts,
        )
        assert scheduler_module._candidate_execution_cohorts(
            "gfs",
            cycle_time,
            (1, "forecast"),
            candidates,
        ) == [(candidates, "run-for-candidate")]

    with monkeypatch.context() as patch_context:

        def patched_format_cycle_time(cycle_time_arg: datetime) -> str:
            del cycle_time_arg
            return "2026052106"

        def fake_candidate_execution_cohort_run_id_for_candidate(
            source_id: str,
            cycle_time_arg: datetime,
            cohort_key: tuple[int, str],
            candidate_arg: Any,
            *,
            format_cycle_time: Any,
        ) -> str:
            assert source_id == "gfs"
            assert cycle_time_arg is cycle_time
            assert cohort_key == (1, "forecast")
            assert candidate_arg is candidate
            assert format_cycle_time is patched_format_cycle_time
            return "cycle-gfs-forecast-model-a"

        patch_context.setattr(scheduler_module, "format_cycle_time", patched_format_cycle_time)
        patch_context.setattr(
            scheduler_execution_module,
            "candidate_execution_cohort_run_id_for_candidate",
            fake_candidate_execution_cohort_run_id_for_candidate,
        )
        assert (
            scheduler_module._candidate_execution_cohort_run_id_for_candidate(
                "gfs",
                cycle_time,
                (1, "forecast"),
                candidate,
            )
            == "cycle-gfs-forecast-model-a"
        )


def test_scheduler_evidence_compat_names_match_owner_module_and_inventory() -> None:
    direct_owner_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES
    direct_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES
    forwarder_owner_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES
    forwarder_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FORWARDER_NAMES
    wrapper_owner_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES
    wrapper_names = scheduler_module._SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES

    assert len(direct_names) == len(set(direct_names))
    assert len(forwarder_names) == len(set(forwarder_names))
    assert len(wrapper_names) == len(set(wrapper_names))
    assert tuple(direct_owner_names) == direct_names
    assert tuple(forwarder_owner_names) == forwarder_names
    assert tuple(wrapper_owner_names) == wrapper_names
    assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING == ()
    assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING == ()
    assert set(direct_names) == set(scheduler_module._SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS)
    assert set(direct_names) == set(scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_EVIDENCE_COMPAT_OWNER_WRAPPERS)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS)

    scheduler_source = Path(scheduler_module.__file__ or "").read_text(encoding="utf-8")
    scheduler_tree = ast.parse(scheduler_source)
    direct_assignments: dict[str, ast.expr] = {}
    for node in scheduler_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    direct_assignments[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            direct_assignments[node.target.id] = node.value

    for facade_name, owner_name in direct_owner_names.items():
        assert hasattr(scheduler_evidence_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assignment = direct_assignments[facade_name]
        assert isinstance(assignment, ast.Attribute)
        assert isinstance(assignment.value, ast.Name)
        assert assignment.value.id == "_scheduler_evidence"
        assert assignment.attr == owner_name
        assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS[facade_name] is getattr(
            scheduler_evidence_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )
        assert getattr(scheduler_module, facade_name) is getattr(scheduler_evidence_module, owner_name)
    for method_name, owner_name in forwarder_owner_names.items():
        assert hasattr(scheduler_evidence_module, owner_name)
        assert hasattr(scheduler_module.ProductionScheduler, method_name)
    for facade_name, owner_name in wrapper_owner_names.items():
        assert hasattr(scheduler_evidence_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_OWNER_WRAPPERS[facade_name] is getattr(
            scheduler_evidence_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS",
    ):
        assert token in inventory_text
    for token in (*direct_names, *forwarder_names, *wrapper_names):
        assert token in inventory_text


def test_scheduler_evidence_compat_forwarders_delegate_to_owner_module(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    started_at = _dt("2026-05-21T12:00:00Z")
    scheduler = ProductionScheduler(
        _config(tmp_path, now=started_at, dry_run=False),
        registry=FakeRegistry([]),
        adapters={},
    )
    pass_id = "scheduler_20260521120000_evidence_compat"
    evidence = {"pass_id": pass_id, "status": "planned"}
    root_preflight = {"checks": {"evidence_root": {"writable": True}}}
    contexts: list[scheduler_evidence_module.SchedulerEvidenceWriteContext] = []

    def fake_write_prelock_blocked_evidence(
        context: scheduler_evidence_module.SchedulerEvidenceWriteContext,
        pass_id_arg: str,
        evidence_arg: dict[str, Any],
        root_preflight_arg: Mapping[str, Any],
        *,
        write_evidence_callback: Any = None,
    ) -> Path:
        contexts.append(context)
        assert pass_id_arg == pass_id
        assert evidence_arg is evidence
        assert root_preflight_arg is root_preflight
        assert write_evidence_callback.__self__ is scheduler
        assert write_evidence_callback.__func__ is scheduler._write_evidence.__func__
        return tmp_path / "prelock.json"

    def fake_reserve_pre_execution_evidence(
        context: scheduler_evidence_module.SchedulerEvidenceWriteContext,
        pass_id_arg: str,
        started_at_arg: datetime,
        candidate_count: int,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        contexts.append(context)
        assert pass_id_arg == pass_id
        assert started_at_arg is started_at
        assert candidate_count == 3
        assert now is started_at
        return {"status": "reserved"}

    def fake_base_evidence(
        config: Any,
        pass_id_arg: str,
        started_at_arg: datetime,
        *,
        resolved_runtime_roots: Any,
        runtime_config_evidence: Any,
    ) -> dict[str, Any]:
        assert config is scheduler.config
        assert pass_id_arg == pass_id
        assert started_at_arg is started_at
        assert resolved_runtime_roots is scheduler_module._scheduler_resolved_runtime_roots
        assert runtime_config_evidence is scheduler_module._scheduler_runtime_config_evidence
        return {"base": True}

    def fake_write_evidence(
        context: scheduler_evidence_module.SchedulerEvidenceWriteContext,
        pass_id_arg: str,
        evidence_arg: Mapping[str, Any],
    ) -> Path:
        contexts.append(context)
        assert pass_id_arg == pass_id
        assert evidence_arg is evidence
        return tmp_path / "final.json"

    monkeypatch.setattr(
        scheduler_evidence_module,
        "write_prelock_blocked_evidence",
        fake_write_prelock_blocked_evidence,
    )
    assert scheduler._write_prelock_blocked_evidence(pass_id, evidence, root_preflight) == tmp_path / "prelock.json"

    monkeypatch.setattr(
        scheduler_evidence_module,
        "reserve_pre_execution_evidence",
        fake_reserve_pre_execution_evidence,
    )
    assert scheduler._reserve_pre_execution_evidence(pass_id, started_at, 3) == {"status": "reserved"}

    monkeypatch.setattr(scheduler_evidence_module, "base_evidence", fake_base_evidence)
    assert scheduler._base_evidence(pass_id, started_at) == {"base": True}

    monkeypatch.setattr(scheduler_evidence_module, "write_evidence", fake_write_evidence)
    assert scheduler._write_evidence(pass_id, evidence) == tmp_path / "final.json"

    context = scheduler._scheduler_evidence_write_context()
    assert isinstance(context, scheduler_evidence_module.SchedulerEvidenceWriteContext)
    assert context.config is scheduler.config
    assert context.max_evidence_bytes == scheduler_module.MAX_EVIDENCE_BYTES
    assert context.bounded_evidence_payload is scheduler_module._bounded_evidence_payload
    assert context.open_evidence_directory is scheduler_module._open_evidence_directory
    assert context.evidence_write_error_payload is scheduler_module._evidence_write_error_payload
    assert contexts
    assert all(item.config is scheduler.config for item in contexts)


def test_scheduler_evidence_compat_wrappers_delegate_to_owner_module(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    candidate = scheduler_module.SchedulerCandidate(
        candidate_id="candidate-1",
        source_id="gfs",
        cycle_id="gfs_2026052106",
        cycle_time_utc=_dt("2026-05-21T06:00:00Z"),
        model_id="model_a",
        basin_id="basin_a",
        basin_version_id="basin_a_v1",
        river_network_version_id="basin_a_rivnet_v1",
        segment_count=3,
        output_segment_count=2,
        model_package_uri="s3://nhms/models/model_a/package/",
        resource_profile={},
        display_capabilities={},
        horizon={},
        scenario_id="forecast_gfs_deterministic",
        run_id="fcst_gfs_2026052106_model_a",
        forcing_version_id="forc_gfs_2026052106_model_a",
        status="ready",
    )
    reservation = {"status": "reserved"}
    written: list[tuple[str, str, int, Path]] = []
    available: list[tuple[str, int, Path]] = []

    def fake_candidate_evidence_write_blocked_evidence(
        candidate_arg: Any,
        reservation_arg: Mapping[str, Any],
        *,
        candidate_model_run_review_evidence: Any,
        candidate_identity_evidence: Any,
        standard_chain_shape: Sequence[str],
        evidence_safe: Any,
    ) -> dict[str, Any]:
        assert candidate_arg is candidate
        assert reservation_arg is reservation
        assert candidate_model_run_review_evidence is scheduler_module._candidate_model_run_review_evidence
        assert candidate_identity_evidence is scheduler_module._candidate_identity_evidence
        assert standard_chain_shape == [stage.stage for stage in scheduler_module.ForecastOrchestrator.stages]
        assert evidence_safe is scheduler_module._evidence_safe
        return {"candidate_blocked": True}

    def fake_cancel_candidate_evidence_write_blocked_evidence(
        candidate_arg: Mapping[str, Any],
        reservation_arg: Mapping[str, Any],
        *,
        ensure_utc: Any,
        evidence_safe: Any,
    ) -> dict[str, Any]:
        assert candidate_arg == {"source_id": "gfs", "cycle_time_utc": "2026-05-21T06:00:00Z"}
        assert reservation_arg is reservation
        assert ensure_utc is scheduler_module._ensure_utc
        assert evidence_safe is scheduler_module._evidence_safe
        return {"cancel_blocked": True}

    def fake_sync_candidate_evidence_write_blocked_evidence(
        candidate_arg: Mapping[str, Any],
        reservation_arg: Mapping[str, Any],
        *,
        standard_chain_shape: Sequence[str],
        evidence_safe: Any,
    ) -> dict[str, Any]:
        assert candidate_arg == {"candidate_id": "candidate-1"}
        assert reservation_arg is reservation
        assert standard_chain_shape == [stage.stage for stage in scheduler_module.ForecastOrchestrator.stages]
        assert evidence_safe is scheduler_module._evidence_safe
        return {"sync_blocked": True}

    def fake_evidence_reservation_blocked_payload(
        *,
        config: Any,
        pass_id: str,
        artifact_path: Path,
        reason: str,
        details: Mapping[str, Any] | None,
        evidence_safe: Any,
    ) -> dict[str, Any]:
        assert config is not None
        assert pass_id == "pass-1"
        assert artifact_path == tmp_path / "blocked.json"
        assert reason == "blocked"
        assert details == {"detail": True}
        assert evidence_safe is scheduler_module._evidence_safe
        return {"reservation_blocked": True}

    def fake_write_new_regular_file(
        artifact_name: str,
        serialized: str,
        *,
        dir_fd: int,
        artifact_path: Path,
    ) -> None:
        written.append((artifact_name, serialized, dir_fd, artifact_path))

    def fake_require_evidence_artifact_available(
        artifact_name: str,
        *,
        dir_fd: int,
        artifact_path: Path,
    ) -> None:
        available.append((artifact_name, dir_fd, artifact_path))

    monkeypatch.setattr(
        scheduler_evidence_module,
        "candidate_evidence_write_blocked_evidence",
        fake_candidate_evidence_write_blocked_evidence,
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "cancel_candidate_evidence_write_blocked_evidence",
        fake_cancel_candidate_evidence_write_blocked_evidence,
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "sync_candidate_evidence_write_blocked_evidence",
        fake_sync_candidate_evidence_write_blocked_evidence,
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "evidence_reservation_blocked_payload",
        fake_evidence_reservation_blocked_payload,
    )
    monkeypatch.setattr(scheduler_evidence_module, "evidence_write_error_payload", lambda error: {"error": str(error)})
    monkeypatch.setattr(
        scheduler_evidence_module,
        "scheduler_resolved_runtime_roots",
        lambda config: {"config": config},
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "root_evidence_item",
        lambda value, *, env, required, fallback=None: {
            "value": value,
            "env": env,
            "required": required,
            "fallback": fallback,
        },
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "scheduler_runtime_config_evidence",
        lambda config: {"runtime_config": config},
    )
    monkeypatch.setattr(scheduler_evidence_module, "open_evidence_directory", lambda evidence_dir, workspace_root: 101)
    monkeypatch.setattr(scheduler_evidence_module, "write_new_regular_file", fake_write_new_regular_file)
    monkeypatch.setattr(
        scheduler_evidence_module,
        "require_evidence_artifact_available",
        fake_require_evidence_artifact_available,
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "bounded_evidence_payload",
        lambda payload, *, reason, max_evidence_bytes: {
            "payload": payload,
            "reason": reason,
            "max_evidence_bytes": max_evidence_bytes,
        },
    )
    monkeypatch.setattr(scheduler_evidence_module, "evidence_status", lambda evidence, fallback: "owner-status")
    monkeypatch.setattr(
        scheduler_evidence_module,
        "execution_write_proof",
        lambda *, reservation=None, execution_required=False, blocked=False: {
            "reservation": reservation,
            "execution_required": execution_required,
            "blocked": blocked,
        },
    )
    monkeypatch.setattr(
        scheduler_evidence_module,
        "execution_write_proof_from_evidence",
        lambda execution_evidence, *, reservation: {
            "execution_evidence": execution_evidence,
            "reservation": reservation,
        },
    )
    monkeypatch.setattr(scheduler_evidence_module, "no_mutation_proof", lambda: {"no_mutation": True})

    assert scheduler_module._candidate_evidence_write_blocked_evidence(candidate, reservation) == {
        "candidate_blocked": True
    }
    assert scheduler_module._cancel_candidate_evidence_write_blocked_evidence(
        {"source_id": "gfs", "cycle_time_utc": "2026-05-21T06:00:00Z"},
        reservation,
    ) == {"cancel_blocked": True}
    assert scheduler_module._sync_candidate_evidence_write_blocked_evidence(
        {"candidate_id": "candidate-1"},
        reservation,
    ) == {"sync_blocked": True}
    assert scheduler_module._evidence_reservation_blocked_payload(
        pass_id="pass-1",
        artifact_path=tmp_path / "blocked.json",
        reason="blocked",
        details={"detail": True},
    ) == {"reservation_blocked": True}
    assert scheduler_module._evidence_write_error_payload(OSError("boom")) == {"error": "boom"}
    assert scheduler_module._scheduler_resolved_runtime_roots("config") == {"config": "config"}
    assert scheduler_module._root_evidence_item("value", env="ENV", required=True, fallback="fallback") == {
        "value": "value",
        "env": "ENV",
        "required": True,
        "fallback": "fallback",
    }
    assert scheduler_module._scheduler_runtime_config_evidence("config") == {"runtime_config": "config"}
    assert scheduler_module._open_evidence_directory(tmp_path / "evidence", tmp_path) == 101
    scheduler_module._write_new_regular_file("artifact.json", "{}", dir_fd=7, artifact_path=tmp_path / "artifact.json")
    assert written == [("artifact.json", "{}", 7, tmp_path / "artifact.json")]
    scheduler_module._require_evidence_artifact_available(
        "artifact.json",
        dir_fd=7,
        artifact_path=tmp_path / "artifact.json",
    )
    assert available == [("artifact.json", 7, tmp_path / "artifact.json")]
    assert scheduler_module._bounded_evidence_payload({"a": 1}, reason="too_large", max_evidence_bytes=99) == {
        "payload": {"a": 1},
        "reason": "too_large",
        "max_evidence_bytes": 99,
    }
    assert scheduler_module._evidence_status({}, "fallback") == "owner-status"
    assert scheduler_module._execution_write_proof(reservation=reservation, execution_required=True) == {
        "reservation": reservation,
        "execution_required": True,
        "blocked": False,
    }
    assert scheduler_module._execution_write_proof_from_evidence([{"submitted": True}], reservation=reservation) == {
        "execution_evidence": [{"submitted": True}],
        "reservation": reservation,
    }
    assert scheduler_module._no_mutation_proof() == {"no_mutation": True}


def test_scheduler_cancellation_status_compat_names_match_owner_module_and_inventory() -> None:
    wrapper_owner_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES
    wrapper_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES
    candidate_alias_owner_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES
    candidate_alias_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES
    retained_method_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES
    retained_function_names = scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES

    assert len(wrapper_names) == len(set(wrapper_names))
    assert len(candidate_alias_names) == len(set(candidate_alias_names))
    assert len(retained_method_names) == len(set(retained_method_names))
    assert len(retained_function_names) == len(set(retained_function_names))
    assert tuple(wrapper_owner_names) == wrapper_names
    assert tuple(candidate_alias_owner_names) == candidate_alias_names
    assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING == ()
    assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING == ()
    assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP == ()
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_WRAPPERS)
    assert set(wrapper_names) == set(scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_WRAPPERS)
    assert set(candidate_alias_names) == set(
        scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES
    )
    assert set(candidate_alias_names) == set(
        scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES
    )

    for facade_name, owner_name in wrapper_owner_names.items():
        assert hasattr(scheduler_evidence_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_WRAPPERS[facade_name] is getattr(
            scheduler_evidence_module,
            owner_name,
        )
        assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_WRAPPERS[facade_name] is getattr(
            scheduler_module,
            facade_name,
        )
    for facade_name, owner_name in candidate_alias_owner_names.items():
        assert scheduler_module._SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES[facade_name] == owner_name
        assert hasattr(scheduler_candidates_module, owner_name)
        assert hasattr(scheduler_module, facade_name)
        assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES[facade_name] is getattr(
            scheduler_candidates_module, owner_name
        )
        assert scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES[facade_name] is getattr(
            scheduler_module, facade_name
        )
        assert getattr(scheduler_module, facade_name) is getattr(scheduler_candidates_module, owner_name)
    for method_name in retained_method_names:
        assert hasattr(scheduler_module.ProductionScheduler, method_name)
        assert method_name not in wrapper_names
        assert method_name not in candidate_alias_names
    for function_name in retained_function_names:
        assert hasattr(scheduler_module, function_name)
        assert function_name not in wrapper_names
        assert function_name not in candidate_alias_names
    for evidence_write_name in (
        "_execution_write_proof",
        "_execution_write_proof_from_evidence",
        "_no_mutation_proof",
    ):
        assert evidence_write_name not in wrapper_names
        assert evidence_write_name not in candidate_alias_names

    inventory_text = _scheduler_inventory_text()
    for token in (
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_WRAPPERS",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES",
    ):
        assert token in inventory_text
    for token in (*wrapper_names, *candidate_alias_names, *retained_method_names, *retained_function_names):
        assert token in inventory_text


def test_scheduler_cancellation_status_compat_wrappers_delegate_to_owner_module(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_owner(owner_name: str) -> Any:
        def fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append({"owner": owner_name, "args": args, "kwargs": kwargs})
            return {"owner": owner_name, "args": args, "kwargs": kwargs}

        return fake

    for owner_name in scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES.values():
        monkeypatch.setattr(scheduler_evidence_module, owner_name, fake_owner(owner_name))

    cancellation_evidence = [{"status": "cancelled", "cancelled_jobs": []}]
    sync_evidence = [{"status": "synced", "updates": []}]
    reservation = {"status": "reserved"}
    proof = {"mutation_occurred": True}
    execution_write_proof = {"slurm_submit_called": False}
    slurm_status_sync_proof = {"mutation_occurred": False}
    slurm_cancellation_proof = {"mutation_occurred": True}

    assert scheduler_module._scheduler_pass_status_from_cancellation(cancellation_evidence)["owner"] == (
        "scheduler_pass_status_from_cancellation"
    )
    assert calls[-1]["args"] == (cancellation_evidence,)
    assert scheduler_module._scheduler_execution_boundary_from_cancellation(cancellation_evidence)["owner"] == (
        "scheduler_execution_boundary_from_cancellation"
    )
    assert calls[-1]["args"] == (cancellation_evidence,)
    assert scheduler_module._slurm_status_sync_proof(sync_required=True, reservation=reservation)["owner"] == (
        "slurm_status_sync_proof"
    )
    assert calls[-1]["kwargs"] == {"sync_required": True, "reservation": reservation, "blocked": False}
    assert (
        scheduler_module._slurm_status_sync_proof_from_candidates(
            sync_evidence,
            reservation=reservation,
        )["owner"]
        == "slurm_status_sync_proof_from_candidates"
    )
    assert calls[-1]["args"] == (sync_evidence,)
    assert calls[-1]["kwargs"] == {"reservation": reservation}
    assert (
        scheduler_module._slurm_cancellation_proof(
            cancellation_required=True,
            reservation=reservation,
        )["owner"]
        == "slurm_cancellation_proof"
    )
    assert calls[-1]["kwargs"] == {"cancellation_required": True, "reservation": reservation, "blocked": False}
    assert (
        scheduler_module._slurm_cancellation_proof_from_evidence(
            cancellation_evidence,
            reservation=reservation,
        )["owner"]
        == "slurm_cancellation_proof_from_evidence"
    )
    assert calls[-1]["args"] == (cancellation_evidence,)
    assert calls[-1]["kwargs"] == {"reservation": reservation}
    assert scheduler_module._slurm_status_sync_count(proof)["owner"] == "slurm_status_sync_count"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._slurm_status_sync_unknown_count(proof)["owner"] == "slurm_status_sync_unknown_count"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._slurm_status_sync_mutated(proof)["owner"] == "slurm_status_sync_mutated"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._slurm_status_sync_failed(proof)["owner"] == "slurm_status_sync_failed"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._slurm_cancelled_count(cancellation_evidence)["owner"] == "slurm_cancelled_count"
    assert calls[-1]["args"] == (cancellation_evidence,)
    assert scheduler_module._slurm_cancellation_blocked_count(cancellation_evidence)["owner"] == (
        "slurm_cancellation_blocked_count"
    )
    assert calls[-1]["args"] == (cancellation_evidence,)
    assert scheduler_module._slurm_cancellation_unknown_count(proof)["owner"] == "slurm_cancellation_unknown_count"
    assert calls[-1]["args"] == (proof,)
    restart_reconcile_evidence = {"status": "completed", "reserved_unbound": {"outcomes": []}}
    assert scheduler_module._restart_reconcile_proof(restart_reconcile_evidence)["owner"] == (
        "restart_reconcile_proof"
    )
    assert calls[-1]["args"] == (restart_reconcile_evidence,)
    assert (
        scheduler_module._scheduler_mutation_proof(
            execution_write_proof=execution_write_proof,
            slurm_status_sync_proof=slurm_status_sync_proof,
            slurm_cancellation_proof=slurm_cancellation_proof,
        )["owner"]
        == "scheduler_mutation_proof"
    )
    assert calls[-1]["kwargs"] == {
        "execution_write_proof": execution_write_proof,
        "slurm_status_sync_proof": slurm_status_sync_proof,
        "slurm_cancellation_proof": slurm_cancellation_proof,
    }
    assert scheduler_module._proof_mutation_value(proof)["owner"] == "proof_mutation_value"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._named_proof_value(proof, "pipeline_status_writes", "absent")["owner"] == (
        "named_proof_value"
    )
    assert calls[-1]["args"] == (proof, "pipeline_status_writes", "absent")
    assert scheduler_module._slurm_submit_proof_value(proof)["owner"] == "slurm_submit_proof_value"
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._pipeline_status_write_proof_value(proof)["owner"] == ("pipeline_status_write_proof_value")
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._pipeline_event_write_proof_value(proof)["owner"] == ("pipeline_event_write_proof_value")
    assert calls[-1]["args"] == (proof,)
    assert scheduler_module._merge_proof_values(False, True, scheduler_module.UNKNOWN_AFTER_ATTEMPT)["owner"] == (
        "merge_proof_values"
    )
    assert calls[-1]["args"] == (False, True, scheduler_module.UNKNOWN_AFTER_ATTEMPT)
    assert scheduler_module._positive_count(2)["owner"] == "positive_count"
    assert calls[-1]["args"] == (2,)
    assert scheduler_module._empty_counts()["owner"] == "empty_counts"
    assert calls[-1]["args"] == ()
    assert set(call["owner"] for call in calls) == set(
        scheduler_module._SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES.values()
    )


def test_restart_reconcile_proof_treats_errors_as_unknown_after_attempt() -> None:
    proof = scheduler_module._restart_reconcile_proof(
        {
            "status": "error",
            "reserved_unbound_error": "durable bind failed",
        }
    )
    mutation = scheduler_module._scheduler_mutation_proof(
        execution_write_proof=scheduler_module._execution_write_proof(),
        slurm_status_sync_proof=scheduler_module._slurm_status_sync_proof(),
        slurm_cancellation_proof=scheduler_module._slurm_cancellation_proof(),
        restart_reconcile_proof=proof,
    )

    assert proof["status"] == "unknown_after_attempt"
    assert proof["mutation_outcome"] == "unknown_after_attempt"
    assert proof["mutation_occurred"] == "unknown_after_attempt"
    assert proof["pipeline_status_writes"] == "unknown_after_attempt"
    assert proof["pipeline_event_writes"] == "unknown_after_attempt"
    assert proof["pipeline_status_writes_proven_absent"] is False
    assert proof["pipeline_event_writes_proven_absent"] is False
    assert proof["error_fields"] == ["reserved_unbound_error"]
    assert scheduler_module._pipeline_status_write_proof_value(proof) == "unknown_after_attempt"
    assert scheduler_module._pipeline_event_write_proof_value(proof) == "unknown_after_attempt"
    assert mutation["pipeline_status_writes"] == "unknown_after_attempt"
    assert mutation["pipeline_event_writes"] == "unknown_after_attempt"
    assert mutation["restart_reconcile_writes"] == "unknown_after_attempt"


def test_registered_model_to_dict_preserves_shud_project_identity() -> None:
    model = scheduler_module.RegisteredSchedulerModel(
        model_id="basins_heihe_shud",
        basin_id="basins_heihe",
        basin_version_id="basins_heihe_vbasins",
        river_network_version_id="basins_heihe_rivnet_vbasins",
        segment_count=4759,
        output_segment_count=2352,
        model_package_uri="s3://nhms/models/basins_heihe_shud/package/",
        shud_code_version="shud",
        resource_profile={"project_name": "heihe", "shud_input_name": "heihe", "memory_gb": 8},
        resource_profile_summary={"memory_gb": 8},
        display_capabilities={},
    )

    payload = model.to_dict()

    assert payload["project_name"] == "heihe"
    assert payload["shud_input_name"] == "heihe"


def test_all_active_models_and_gfs_ifs_window_produce_stable_candidate_ids(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    config = _config(tmp_path, now=now, sources=("gfs", "IFS"), max_cycles_per_source=2)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={
            "gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True), ("2026-05-21T00:00:00Z", True)]),
            "IFS": FakeAdapter("IFS", [("2026-05-21T06:00:00Z", True), ("2026-05-21T00:00:00Z", True)]),
        },
    )

    first = scheduler.run_once()
    second = scheduler.run_once()

    first_candidates = _candidates(first.evidence)
    second_candidates = _candidates(second.evidence)
    assert len(first_candidates) == 8
    assert [(item["candidate_id"], item["run_id"], item["forcing_version_id"]) for item in first_candidates] == [
        (item["candidate_id"], item["run_id"], item["forcing_version_id"]) for item in second_candidates
    ]
    gfs_model_a = next(
        item
        for item in first_candidates
        if item["candidate_id"] == "gfs:2026-05-21T00:00:00Z:model_a:forecast_gfs_deterministic"
    )
    assert gfs_model_a["run_id"] == "fcst_gfs_2026052100_model_a"
    assert gfs_model_a["forcing_version_id"] == "forc_gfs_2026052100_model_a"
    assert gfs_model_a["river_network_version_id"] == "basin_a_rivnet_v1"
    assert gfs_model_a["model_package_uri"] == "s3://nhms/models/model_a/package/"
    assert gfs_model_a["resource_profile"]["memory_gb"] == 8
    assert gfs_model_a["display_capabilities"] == {"tiles": True}
    assert gfs_model_a["horizon"]["max_lead_hours"] == 168
    ifs_06z = next(
        item
        for item in first_candidates
        if item["source_id"] == "IFS" and item["cycle_time_utc"] == "2026-05-21T06:00:00Z"
    )
    assert ifs_06z["horizon"]["max_lead_hours"] == 144
    assert first.evidence["counts"]["submitted_count"] == 0


def test_production_contract_matrix_is_exposed_in_scheduler_pass_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    contract = result.evidence["production_contract"]
    candidate = result.evidence["candidates"][0]
    identity_contract = candidate["production_identity_contract"]
    assert contract == production_contract_matrix()
    assert identity_contract["schema_version"] == "nhms.production.identity_status_uri_contract.v1"
    assert identity_contract["complete"] is True
    assert identity_contract["identity"] == {
        "run_id": "fcst_gfs_2026052106_model_a",
        "model_id": "model_a",
        "basin_id": "basin_a",
        "source": "gfs",
        "cycle_time": "2026-05-21T06:00:00Z",
        "basin_version_id": "basin_a_v1",
        "river_network_version_id": "basin_a_rivnet_v1",
        "canonical_product_id": "canon_gfs_2026052106",
        "forcing_version_id": "forc_gfs_2026052106_model_a",
        "hydro_run_id": "fcst_gfs_2026052106_model_a",
        "published_manifest_id": "manifest_fcst_gfs_2026052106_model_a",
    }
    assert candidate["canonical_product_id"] == "canon_gfs_2026052106"
    assert candidate["published_manifest_id"] == "manifest_fcst_gfs_2026052106_model_a"


def test_canonical_incomplete_readiness_blocks_forcing_candidate_submission(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    readiness = FakeCanonicalReadinessProvider(
        {
            ("gfs", cycle_time): evaluate_canonical_readiness(
                source_id="gfs",
                cycle_time=cycle_time,
                products=_canonical_rows(
                    source_id="gfs",
                    cycle_time=cycle_time,
                    variables=GFS_REQUIRED_STANDARD_VARIABLES,
                    forecast_hours=(0, 3),
                    policy_identity=policy,
                    source_object_identity=source_object,
                    omit_pairs={("shortwave_down", 3)},
                ),
                forecast_hours=(0, 3),
                policy_identity=policy,
                source_object_identity=source_object,
                canonical_product_id=f"canon_gfs_{format_cycle_time(cycle_time)}",
                model_id="model_a",
                basin_id="basin_a",
            ).evidence
        }
    )
    adapter = FakeAdapter(
        "gfs",
        [("2026-05-21T06:00:00Z", True)],
        policy_identity=policy,
        source_object_identity=source_object,
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
        canonical_readiness_provider=readiness,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "missing_canonical_leads"
    canonical = blocked["state_evidence"]["canonical_readiness"]
    assert canonical["status"] == "canonical_incomplete"
    assert canonical["missing_leads"][0]["missing_variables"] == ["shortwave_down"]
    assert adapter.download_calls == 0


def test_warn_canonical_readiness_allows_candidate_selection_with_quality_evidence(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    rows = _canonical_rows(
        source_id="gfs",
        cycle_time=cycle_time,
        variables=GFS_REQUIRED_STANDARD_VARIABLES,
        forecast_hours=(0, 3),
        policy_identity=policy,
        source_object_identity=source_object,
    )
    rejected = next(row for row in rows if row["variable"] == "shortwave_down" and row["lead_time_hours"] == 3)
    rejected["quality_flag"] = "warn"
    readiness = FakeCanonicalReadinessProvider(
        {
            ("gfs", cycle_time): evaluate_canonical_readiness(
                source_id="gfs",
                cycle_time=cycle_time,
                products=rows,
                forecast_hours=(0, 3),
                policy_identity=policy,
                source_object_identity=source_object,
                canonical_product_id="canon_gfs_2026052106",
                model_id="model_a",
                basin_id="basin_a",
            ).evidence
        }
    )
    adapter = FakeAdapter(
        "gfs",
        [("2026-05-21T06:00:00Z", True)],
        policy_identity=policy,
        source_object_identity=source_object,
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
        canonical_readiness_provider=readiness,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["candidate_count"] == 1
    assert result.evidence["counts"]["submitted_count"] == 0
    assert len(result.evidence["candidates"]) == 1
    assert result.evidence["candidates"][0]["status"] == "selected"
    canonical = result.evidence["candidates"][0]["state_evidence"]["canonical_readiness"]
    assert canonical["status"] == "canonical_ready"
    assert canonical["ready"] is True
    assert canonical["rejected_quality_flags"] == {}
    assert canonical["present_variables"] == sorted(GFS_REQUIRED_STANDARD_VARIABLES)
    assert adapter.download_calls == 0


def test_checksum_missing_canonical_readiness_blocks_forcing_candidate_submission(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    rows = _canonical_rows(
        source_id="gfs",
        cycle_time=cycle_time,
        variables=GFS_REQUIRED_STANDARD_VARIABLES,
        forecast_hours=(0, 3),
        policy_identity=policy,
        source_object_identity=source_object,
    )
    rejected = next(row for row in rows if row["variable"] == "shortwave_down" and row["lead_time_hours"] == 3)
    rejected["checksum"] = ""
    readiness = FakeCanonicalReadinessProvider(
        {
            ("gfs", cycle_time): evaluate_canonical_readiness(
                source_id="gfs",
                cycle_time=cycle_time,
                products=rows,
                forecast_hours=(0, 3),
                policy_identity=policy,
                source_object_identity=source_object,
                canonical_product_id="canon_gfs_2026052106",
                model_id="model_a",
                basin_id="basin_a",
            ).evidence
        }
    )
    adapter = FakeAdapter(
        "gfs",
        [("2026-05-21T06:00:00Z", True)],
        policy_identity=policy,
        source_object_identity=source_object,
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
        canonical_readiness_provider=readiness,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "missing_canonical_leads"
    canonical = blocked["state_evidence"]["canonical_readiness"]
    assert canonical["status"] == "canonical_incomplete"
    assert canonical["checksum_missing_row_count"] == 1
    assert canonical["checksum_missing_samples"][0]["reason"] == "checksum_missing"
    assert canonical["checksum_missing_samples"][0]["variable"] == "shortwave_down"
    assert canonical["missing_leads"][0]["missing_variables"] == ["shortwave_down"]
    assert adapter.download_calls == 0


def test_scheduler_invokes_forcing_producer_before_orchestration_for_ready_canonical_candidate(
    tmp_path: Path,
) -> None:
    forcing_producer = FakeForcingProducer()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert len(forcing_producer.calls) == 1
    producer_call = forcing_producer.calls[0]
    assert producer_call["source_id"] == "gfs"
    assert producer_call["cycle_time"] == _dt("2026-05-21T06:00:00Z")
    assert producer_call["model_id"] == "model_a"
    assert producer_call["max_lead_hours"] == 168
    assert producer_call["basin_id"] == "basin_a"
    assert producer_call["basin_version_id"] == "basin_a_v1"
    assert producer_call["river_network_version_id"] == "basin_a_rivnet_v1"
    assert producer_call["canonical_product_id"] == "canon_gfs_2026052106"
    assert producer_call["canonical_identity"]["canonical_product_id"] == "canon_gfs_2026052106"
    assert producer_call["canonical_identity"]["policy_identity"]["source"] == "gfs"
    assert producer_call["canonical_identity"]["source_object_identity"]["source"] == "gfs"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["execution_write_proof"]["met_result_table_writes"] is True
    assert result.evidence["candidates"][0]["state_evidence"]["forcing_production"]["status"] == "forcing_ready"
    assert result.evidence["model_run_evidence"][0]["stage"] == "forcing"
    assert result.evidence["model_run_evidence"][0]["forcing"]["station_count"] == 2
    assert result.evidence["model_run_evidence"][0]["forcing"]["variable_count"] == 6
    assert result.evidence["model_run_evidence"][0]["forcing"]["manifest_checksum"] == "forcing-manifest-sha"
    assert orchestrator.calls
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["forcing_version_id"] == "forc_gfs_2026052106_model_a"
    assert submitted_basin["forcing_package_uri"].endswith("/forcing/gfs/2026052106/basin_a_v1/model_a/")
    assert submitted_basin["forcing_uri"].endswith("/forcing/gfs/2026052106/basin_a_v1/model_a/forcing.tsd.forc")
    assert submitted_basin["forcing_package_manifest_uri"].endswith(
        "/forcing/gfs/2026052106/basin_a_v1/model_a/forcing_package.json"
    )
    assert submitted_basin["forcing_manifest_checksum"] == "forcing-manifest-sha"


def test_scheduler_blocks_orchestration_when_forcing_producer_fails(tmp_path: Path) -> None:
    forcing_producer = FakeForcingProducer(error=RuntimeError("missing fixed stations"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert forcing_producer.calls
    assert orchestrator.calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["blocked_candidates"][0]["reason"] == "forcing_production_blocked"
    assert result.evidence["model_run_evidence"][0]["stage"] == "forcing"
    assert result.evidence["model_run_evidence"][0]["status"] == "blocked"
    assert result.evidence["model_run_evidence"][0]["slurm_submit_called"] is False
    assert result.evidence["no_mutation_proof"]["shud_runtime_called"] is False


def test_scheduler_propagates_produced_forcing_identity_to_orchestration(tmp_path: Path) -> None:
    forcing_producer = FakeForcingProducer(forcing_version_id="forc_reused_existing_ready")
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["candidates"][0]["forcing_version_id"] == "forc_reused_existing_ready"
    assert result.evidence["model_run_evidence"][0]["forcing_version_id"] == "forc_reused_existing_ready"
    assert orchestrator.calls[0]["basins"][0]["forcing_version_id"] == "forc_reused_existing_ready"
    assert orchestrator.calls[0]["basins"][0]["forcing_package_manifest_uri"].endswith("forcing_package.json")


def _fresh_zero_row_readiness_provider(
    cycle_time: datetime,
    *,
    policy: Mapping[str, Any],
    source_object: Mapping[str, Any],
) -> FakeCanonicalReadinessProvider:
    """Readiness provider for a brand-new cycle with zero canonical rows.

    ``evaluate_canonical_readiness`` over an empty product set yields
    ``ready=False`` with ``candidate_row_count == 0`` -> fresh full-chain
    ingestion (M23 §255), not a corrupt/partial canonical block.
    """

    return FakeCanonicalReadinessProvider(
        {
            ("gfs", cycle_time): evaluate_canonical_readiness(
                source_id="gfs",
                cycle_time=cycle_time,
                products=[],
                forecast_hours=(0, 3),
                policy_identity=dict(policy),
                source_object_identity=dict(source_object),
                canonical_product_id="canon_gfs_2026052106",
                model_id="model_a",
                basin_id="basin_a",
            ).evidence
        }
    )


def _raw_ready_state(cycle_time: datetime, *, source_id: str = "gfs") -> dict[str, Any]:
    cycle_id = cycle_id_for(source_id, cycle_time)
    cycle_text = format_cycle_time(cycle_time)
    manifest_uri = f"s3://nhms/raw/{source_id}/{cycle_text}/manifest.json"
    return {
        "forecast_cycle": {
            "cycle_id": cycle_id,
            "source_id": source_id,
            "cycle_time": cycle_time.isoformat().replace("+00:00", "Z"),
            "status": "raw_complete",
            "manifest_uri": manifest_uri,
        },
        "nfs_raw_manifest": {
            "status": "ready",
            "required": True,
            "source": "node27_nfs_raw_manifest",
            "source_id": source_id,
            "cycle_id": cycle_id,
            "cycle_time": cycle_time.isoformat().replace("+00:00", "Z"),
            "manifest_uri": manifest_uri,
            "entry_count": 4,
            "physical_file_count": 2,
        },
    }


def test_fresh_cycle_with_zero_canonical_blocks_without_node27_raw_manifest(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-05-21T12:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052112/manifest.json"}
    # A forcing producer that would raise if ever invoked: without node-27 raw,
    # production must block before any in-process or Slurm work.
    forcing_producer = FakeForcingProducer(error=RuntimeError("in-process forcing must be skipped for fresh ingestion"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "planned"
    assert forcing_producer.calls == []
    assert orchestrator.calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert len(result.evidence["blocked_candidates"]) == 1
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "nfs_raw_manifest_required"
    assert blocked["state_evidence"]["nfs_raw_manifest"] == {
        "status": "missing",
        "ready": False,
        "required": True,
        "source": "node27_nfs_raw_manifest",
        "reason": "production_download_retired",
    }
    assert "fresh_ingestion" not in blocked["state_evidence"]


def test_fresh_zero_canonical_with_nfs_raw_ready_restarts_at_convert(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    active_repository = FakeCandidateStateRepository(
        {
            "forecast_cycle": {
                "cycle_id": "gfs_2026052106",
                "source_id": "gfs",
                "cycle_time": "2026-05-21T06:00:00Z",
                "status": "raw_complete",
                "manifest_uri": "s3://nhms/raw/gfs/2026052106/manifest.json",
            },
            "nfs_raw_manifest": {
                "status": "ready",
                "required": True,
                "source": "node27_nfs_raw_manifest",
                "source_id": "gfs",
                "cycle_id": "gfs_2026052106",
                "cycle_time": "2026-05-21T06:00:00Z",
                "manifest_uri": "s3://nhms/raw/gfs/2026052106/manifest.json",
                "entry_count": 4,
                "physical_file_count": 2,
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["blocked_candidates"] == []
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["restart_stage"] == "convert"
    state_evidence = submitted_basin["state_evidence"]
    assert state_evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
    assert state_evidence["raw_manifest_reuse"]["source"] == "node27_nfs_raw_manifest"
    assert state_evidence["nfs_raw_manifest"]["status"] == "ready"
    assert state_evidence["canonical_readiness"]["candidate_row_count"] == 0


def test_nfs_raw_ready_candidate_stages_raw_before_convert_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-26T12:00:00Z")
    nfs_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    raw_key = "raw/gfs/2026062612/gfs.t12z.f000.bundle.grib2"
    raw_file = nfs_root / raw_key
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(b"node27-raw")
    manifest = {
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00+00:00",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "entries": [
            {
                "remote_url": "https://example.invalid/gfs",
                "local_key": raw_key,
                "variable": "prcp_rate_or_amount",
                "forecast_hour": 0,
            }
        ],
    }
    (nfs_root / "raw/gfs/2026062612/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    nfs_readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        object_store_root=nfs_root,
        object_store_prefix="s3://nhms",
        required=True,
    )
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026062612/manifest.json"}
    active_repository = FakeCandidateStateRepository(
        {
            "forecast_cycle": {
                "cycle_id": "gfs_2026062612",
                "source_id": "gfs",
                "cycle_time": "2026-06-26T12:00:00Z",
                "status": "raw_complete",
                "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
            },
            "nfs_raw_manifest": nfs_readiness,
        }
    )
    monkeypatch.setenv("NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_STAGE_ROOT", str(target_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-06-27T00:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-06-26T12:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert (target_root / raw_key).read_bytes() == b"node27-raw"
    assert json.loads((target_root / "raw/gfs/2026062612/manifest.json").read_text(encoding="utf-8")) == manifest
    staging = [
        item for item in result.evidence["model_run_evidence"] if item.get("type") == "nfs_raw_manifest_staging"
    ]
    assert staging and staging[0]["status"] == "staged"
    assert staging[0]["manifest_uri"] == "[object-uri]"
    assert orchestrator.calls[0]["basins"][0]["restart_stage"] == "convert"
    assert "s3://nhms/raw" not in json.dumps(staging, sort_keys=True)


def test_canonical_readiness_prefers_persisted_nfs_raw_source_object_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    nfs_root = tmp_path / "nfs"
    raw_key = "raw/gfs/2026052106/gfs.t06z.pgrb2.0p25.f000.bundle.grib2"
    raw_file = nfs_root / raw_key
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(b"node27-raw")
    persisted_source_object = {
        "source": "gfs",
        "manifest_object_key": "raw/gfs/2026052106/manifest.json",
        "manifest_digest": "persisted-nfs-manifest-digest",
        "raw_entry_digest": "persisted-raw-entry-digest",
    }
    persisted_policy = {
        "source": "gfs",
        "policy_schema_version": "nhms.gfs.source_policy.v3",
        "cycle_hours_utc": [0, 6, 12, 18],
        "forecast_hours": [0, 3],
    }
    manifest = {
        "source_id": "gfs",
        "cycle_time": "2026-05-21T06:00:00+00:00",
        "manifest_uri": "s3://nhms/raw/gfs/2026052106/manifest.json",
        "metadata": {
            "physical_file_count": 1,
            "source_object_identity": persisted_source_object,
            "source_policy": persisted_policy,
        },
        "entries": [
            {
                "remote_url": "https://example.invalid/gfs",
                "local_key": raw_key,
                "variable": "prcp_rate_or_amount",
                "forecast_hour": 0,
            }
        ],
    }
    (nfs_root / "raw/gfs/2026052106/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    nfs_readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        object_store_root=nfs_root,
        object_store_prefix="s3://nhms",
        required=True,
    )
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(nfs_root))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://nhms")

    class CapturingReadinessProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def canonical_readiness(self, **kwargs: Any) -> Mapping[str, Any]:
            self.calls.append(dict(kwargs))
            return {
                "status": "canonical_ready",
                "ready": True,
                "reason": None,
                "source_id": kwargs["source_id"],
                "cycle_time": kwargs["cycle_time"],
                "forecast_hours": list(kwargs["forecast_hours"]),
                "policy_identity": dict(kwargs["policy_identity"]),
                "source_object_identity": dict(kwargs["source_object_identity"]),
                "canonical_product_id": kwargs["canonical_product_id"],
                "model_id": kwargs["model_id"],
                "basin_id": kwargs["basin_id"],
            }

    provider = CapturingReadinessProvider()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity={"source": "gfs", "cycle_hours_utc": [0, 12], "forecast_hours": [0, 3]},
                source_object_identity={"source": "gfs", "manifest_digest": "adapter-digest"},
            )
        },
        active_repository=FakeCandidateStateRepository(
            {
                "forecast_cycle": {
                    "cycle_id": "gfs_2026052106",
                    "source_id": "gfs",
                    "cycle_time": "2026-05-21T06:00:00Z",
                    "status": "raw_complete",
                    "manifest_uri": "s3://nhms/raw/gfs/2026052106/manifest.json",
                },
                "nfs_raw_manifest": nfs_readiness,
            }
        ),
        canonical_readiness_provider=provider,
    )

    result = scheduler.run_once()

    assert result.status == "planned"
    assert provider.calls
    assert provider.calls[0]["source_object_identity"] == persisted_source_object
    assert provider.calls[0]["policy_identity"] == persisted_policy
    canonical = result.evidence["candidates"][0]["state_evidence"]["canonical_readiness"]
    assert canonical["source_object_identity"]["manifest_digest"] == "persisted-nfs-manifest-digest"
    assert canonical["policy_identity"]["cycle_hours_utc"] == [0, 6, 12, 18]


def test_required_nfs_raw_manifest_missing_blocks_fresh_download_fallback(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    active_repository = FakeCandidateStateRepository(
        {
            "nfs_raw_manifest": {
                "status": "missing",
                "required": True,
                "reason": "manifest_not_found",
                "source": "node27_nfs_raw_manifest",
                "source_id": "gfs",
                "cycle_id": "gfs_2026052106",
                "cycle_time": "2026-05-21T06:00:00Z",
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert len(result.evidence["blocked_candidates"]) == 1
    assert orchestrator.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "nfs_raw_manifest_manifest_not_found"
    assert blocked["state_evidence"]["nfs_raw_manifest"]["required"] is True
    assert "fresh_ingestion" not in blocked["state_evidence"]


def test_fresh_cycle_manual_retry_preserves_retry_state_and_reuses_raw_manifest(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    active_repository = FakeCandidateStateRepository(
        {
            **_raw_ready_state(cycle_time),
            "pipeline_status": "failed",
            "failed_stage": "convert",
            "error_code": "SLURM_JOB_FAILED",
            "retry_count": 0,
            "retry_limit": 3,
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_convert",
                    "status": "failed",
                    "stage": "convert",
                    "retry_count": 0,
                    "error_code": "SLURM_JOB_FAILED",
                    "updated_at": "2026-05-21T06:20:00Z",
                }
            ],
            "pipeline_events": [
                {
                    "event_id": 5,
                    "event_type": "retry",
                    "entity_id": "job_cycle_gfs_2026052106_convert",
                    "created_at": "2026-05-21T06:30:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 1,
                        "previous_job_id": "job_cycle_gfs_2026052106_convert",
                    },
                }
            ],
        }
    )
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert forcing_producer.calls == []
    assert result.evidence["blocked_candidates"] == []
    submitted_basin = orchestrator.calls[0]["basins"][0]
    state_evidence = submitted_basin["state_evidence"]
    assert submitted_basin["restart_stage"] == "convert"
    assert state_evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
    assert state_evidence["raw_manifest_reuse"]["source"] == "node27_nfs_raw_manifest"
    assert state_evidence["decision"] == "manual_retry"
    assert state_evidence["manual_retry"]["marker"] is True


def test_unsubmitted_auto_retry_placeholder_does_not_block_scheduler_retry(tmp_path: Path) -> None:
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "pending",
            "failed_stage": "forecast",
            "error_code": "NODE_FAILURE",
            "retry_count": 1,
            "retry_limit": 3,
            "forecast_cycle": {
                "cycle_id": "gfs_2026052106",
                "status": "failed_run",
                "manifest_uri": "published://tiles/hydro/gfs_2026052106/q-down/manifest.json",
            },
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "run_shud_forecast_array",
                    "status": "failed",
                    "stage": "forecast",
                    "retry_count": 0,
                    "error_code": "NODE_FAILURE",
                    "updated_at": "2026-05-21T06:20:00Z",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast_retry_1",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "run_shud_forecast_array",
                    "status": "pending",
                    "stage": "forecast",
                    "retry_count": 1,
                    "slurm_job_id": None,
                    "array_task_id": None,
                    "manual_retry_marker": False,
                    "candidate_id": None,
                    "idempotency_key": None,
                    "updated_at": "2026-05-21T06:21:00Z",
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    submitted_basin = orchestrator.calls[0]["basins"][0]
    state_evidence = submitted_basin["state_evidence"]
    assert state_evidence["decision"] == "retry_failed"
    assert state_evidence["restart_stage"] == "forecast"
    assert submitted_basin["restart_stage"] == "forecast"


def test_candidate_scoped_retry_bypasses_active_pipeline_guard(tmp_path: Path) -> None:
    class ActiveCandidateStateRepository(FakeCandidateStateRepository):
        def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

    active_repository = ActiveCandidateStateRepository(
        {
            "pipeline_status": "pending",
            "failed_stage": "forecast",
            "error_code": "NODE_FAILURE",
            "retry_count": 1,
            "retry_limit": 3,
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "run_shud_forecast_array",
                    "status": "failed",
                    "stage": "forecast",
                    "retry_count": 0,
                    "error_code": "NODE_FAILURE",
                    "updated_at": "2026-05-21T06:20:00Z",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast_retry_1",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "run_shud_forecast_array",
                    "status": "pending",
                    "stage": "forecast",
                    "retry_count": 1,
                    "slurm_job_id": None,
                    "array_task_id": None,
                    "manual_retry_marker": False,
                    "candidate_id": None,
                    "idempotency_key": None,
                    "updated_at": "2026-05-21T06:21:00Z",
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["state_evidence"]["decision"] == "retry_failed"
    assert submitted_basin["restart_stage"] == "forecast"


def test_fresh_cycle_basin_manifest_carries_identity_contract_fields(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeCandidateStateRepository(_raw_ready_state(cycle_time)),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert forcing_producer.calls == []
    submitted_basin = orchestrator.calls[0]["basins"][0]
    # Identity contract must survive the fresh full-chain path.
    assert submitted_basin["canonical_product_id"] == "canon_gfs_2026052106"
    assert submitted_basin["run_id"] == "fcst_gfs_2026052106_model_a"
    assert submitted_basin["forcing_version_id"] == "forc_gfs_2026052106_model_a"
    assert submitted_basin["hydro_run_id"] == "fcst_gfs_2026052106_model_a"
    assert submitted_basin["published_manifest_id"] == "manifest_fcst_gfs_2026052106_model_a"


def test_ready_nfs_raw_manifest_identity_mismatch_blocks_reuse(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    state = _raw_ready_state(cycle_time)
    state["nfs_raw_manifest"] = {
        **state["nfs_raw_manifest"],
        "cycle_id": "gfs_2026052100",
        "cycle_time": "2026-05-21T00:00:00Z",
        "manifest_uri": "s3://nhms/raw/gfs/2026052100/manifest.json",
    }
    forcing_producer = FakeForcingProducer(error=RuntimeError("mismatched raw manifest must block before forcing"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeCandidateStateRepository(state),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "nfs_raw_manifest_identity_mismatch"
    assert blocked["state_evidence"]["nfs_raw_manifest"]["status"] == "ready"
    assert "raw_manifest_reuse" not in blocked["state_evidence"]


@pytest.mark.parametrize(
    ("missing_fields", "field_updates"),
    [
        (("source_id",), {}),
        (("cycle_id",), {}),
        (("cycle_time",), {}),
        ((), {"source_id": "ifs"}),
        ((), {"cycle_id": "gfs_2026052100"}),
        ((), {"cycle_time": "not-a-time"}),
        ((), {"cycle_time": "2026-05-21T00:00:00Z"}),
    ],
)
def test_redacted_nfs_raw_manifest_identity_mismatch_blocks_reuse(
    tmp_path: Path,
    missing_fields: tuple[str, ...],
    field_updates: dict[str, Any],
) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    state = _raw_ready_state(cycle_time)
    nfs_raw_manifest = {
        **state["nfs_raw_manifest"],
        "manifest_uri": "[object-uri]",
        **field_updates,
    }
    for field_name in missing_fields:
        nfs_raw_manifest.pop(field_name, None)
    state["nfs_raw_manifest"] = nfs_raw_manifest
    forcing_producer = FakeForcingProducer(error=RuntimeError("redacted raw manifest mismatch must block"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeCandidateStateRepository(state),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "nfs_raw_manifest_identity_mismatch"
    assert blocked["state_evidence"]["nfs_raw_manifest"]["manifest_uri"] == "[object-uri]"
    assert "raw_manifest_reuse" not in blocked["state_evidence"]


def test_partial_canonical_still_blocks_not_fresh_full_chain(tmp_path: Path) -> None:
    # candidate_row_count > 0 but identity/variable incomplete must keep the
    # existing hard block; it is NOT a fresh full-chain ingestion.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    readiness_evidence = evaluate_canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        products=_canonical_rows(
            source_id="gfs",
            cycle_time=cycle_time,
            variables=GFS_REQUIRED_STANDARD_VARIABLES,
            forecast_hours=(0, 3),
            policy_identity=policy,
            source_object_identity=source_object,
            omit_pairs={("shortwave_down", 3)},
        ),
        forecast_hours=(0, 3),
        policy_identity=policy,
        source_object_identity=source_object,
        canonical_product_id="canon_gfs_2026052106",
        model_id="model_a",
        basin_id="basin_a",
    ).evidence
    assert readiness_evidence["candidate_row_count"] > 0
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    adapter = FakeAdapter(
        "gfs",
        [("2026-05-21T06:00:00Z", True)],
        policy_identity=policy,
        source_object_identity=source_object,
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=FakeCanonicalReadinessProvider({("gfs", cycle_time): readiness_evidence}),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "missing_canonical_leads"
    assert "fresh_ingestion" not in blocked["state_evidence"]


def test_fresh_cycle_with_active_slurm_job_does_not_double_submit(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    active_jobs = [{"job_id": "job_download", "slurm_job_id": "9001", "stage": "download", "status": "running"}]
    active_repository = CandidateAndActiveRepository(
        {"active_slurm_jobs": active_jobs},
        active_jobs=active_jobs,
    )
    forcing_producer = FakeForcingProducer(error=RuntimeError("must not run when an active job exists"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "active_slurm_job"
    assert skipped["active_slurm_jobs"][0]["slurm_job_id"] == "9001"


def test_fresh_cycle_ingestion_stage_failure_does_not_fabricate_success(tmp_path: Path) -> None:
    # The shared stage-evidence fixture emits a single chain stage that fails;
    # the scheduler must record that ingestion-stage failure honestly instead of
    # fabricating a terminal "submitted"/"complete" state.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestratorWithStageEvidence(
        result_status="failed",
        stage_status="failed",
        stage_error_message="ingestion stage failed: upstream object missing",
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeCandidateStateRepository(_raw_ready_state(cycle_time)),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert forcing_producer.calls == []
    assert orchestrator.calls
    # The chain ran but a stage failed: the scheduler must record the failure
    # honestly (no fabricated "complete"/"submitted" terminal state).
    assert result.status != "submitted"
    model_evidence = next(
        item for item in result.evidence["model_run_evidence"] if item.get("pipeline_run_id")
    )
    assert model_evidence["status"] == "failed"


def _no_expected_leads_readiness_provider(cycle_time: datetime) -> FakeCanonicalReadinessProvider:
    """Readiness for a broken horizon/policy: empty ``forecast_hours``.

    ``evaluate_canonical_readiness`` yields ready=False,
    candidate_row_count==0, status=canonical_incomplete,
    reason="no_expected_leads". This is a config defect (hard block), NOT a
    fresh zero-row cycle.
    """

    return FakeCanonicalReadinessProvider(
        {
            ("gfs", cycle_time): evaluate_canonical_readiness(
                source_id="gfs",
                cycle_time=cycle_time,
                products=[],
                forecast_hours=(),
                canonical_product_id="canon_gfs_2026052106",
                model_id="model_a",
                basin_id="basin_a",
            ).evidence
        }
    )


def test_no_expected_leads_readiness_is_not_fresh_zero_row() -> None:
    # MAJOR-1: a broken horizon (empty forecast_hours) reports zero canonical
    # rows but must keep the hard block, not be reclassified as fresh ingestion.
    # The discriminator is an empty ``expected_leads`` (broken horizon/policy
    # config), regardless of which downstream reason string the converter picks.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    evidence = evaluate_canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        products=[],
        forecast_hours=(),
        canonical_product_id="canon_gfs_2026052106",
        model_id="model_a",
        basin_id="basin_a",
    ).evidence
    assert evidence["ready"] is False
    assert evidence["candidate_row_count"] == 0
    assert not evidence["expected_leads"]
    # Real broken-horizon evidence is the only zero-row case the pre-fix guard
    # would have mislabeled fresh; the expected_leads gate now blocks it.
    assert scheduler_module._canonical_evidence_is_fresh_zero_row(evidence) is False
    # Explicit no_expected_leads reason (e.g. a source with no required
    # variables) is also blocked.
    assert (
        scheduler_module._canonical_evidence_is_fresh_zero_row(
            {
                "ready": False,
                "status": "canonical_incomplete",
                "candidate_row_count": 0,
                "expected_leads": (),
                "reason": "no_expected_leads",
            }
        )
        is False
    )


def test_no_expected_leads_cycle_stays_blocked_not_fresh_full_chain(tmp_path: Path) -> None:
    # MAJOR-1 end-to-end: empty-horizon readiness must hard-block; no
    # fresh_ingestion marker, no orchestrator submission.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(_raw_ready_state(cycle_time)),
        canonical_readiness_provider=_no_expected_leads_readiness_provider(cycle_time),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    # Empty horizon over an empty product set surfaces as a hard block (the
    # converter labels it missing_canonical_variables / no_expected_leads); the
    # crucial guarantee is it is NOT reclassified as fresh full-chain ingestion.
    assert blocked["reason"] in {"no_expected_leads", "missing_canonical_variables"}
    assert "fresh_ingestion" not in blocked["state_evidence"]
    readiness = blocked["state_evidence"]["canonical_readiness"]
    assert not readiness["expected_leads"]


def test_fresh_full_chain_candidate_forces_full_cohort_despite_residual_restart_stage() -> None:
    # MAJOR-2: a fresh full-chain candidate that also carries a residual
    # restart_stage (e.g. retention cleared canonical while a stale restart
    # marker lingered) must still route into the (0, "full") cohort, not be
    # diverted onto the restart path.
    candidate = scheduler_module._candidate_with_state_evidence(
        _scheduler_candidate_fixture(),
        {
            "fresh_ingestion": {"required": True, "mode": "full_chain"},
            "restart_stage": "parse",
        },
    )
    assert scheduler_module._candidate_is_fresh_full_chain(candidate) is True
    # Single source of truth: fresh full-chain forces restart_stage None.
    assert scheduler_module._candidate_restart_stage(candidate) is None
    cohorts = scheduler_module._restart_compatible_candidate_cohorts([candidate])
    assert len(cohorts) == 1
    ((cohort_key, cohort_candidates),) = cohorts
    assert cohort_key == (0, "full")
    assert cohort_candidates == [candidate]


def test_full_cohort_candidates_are_candidate_scoped_for_model_isolation() -> None:
    candidate_a = _scheduler_candidate_fixture()
    candidate_b = replace(
        candidate_a,
        candidate_id="gfs_2026052106_model_b",
        model_id="model_b",
        basin_id="basin_b",
        basin_version_id="basin_b_v1",
        river_network_version_id="basin_b_rivnet_v1",
        run_id="fcst_gfs_2026052106_model_b",
        forcing_version_id="forc_gfs_2026052106_model_b",
    )

    cohorts = scheduler_module._candidate_execution_cohorts(
        "gfs",
        _dt("2026-05-21T06:00:00Z"),
        (0, "full"),
        [candidate_a, candidate_b],
    )

    assert cohorts == [
        ([candidate_a], "cycle_gfs_2026052106_full_model_a"),
        ([candidate_b], "cycle_gfs_2026052106_full_model_b"),
    ]


def test_raw_manifest_reuse_overrides_residual_restart_stage(tmp_path: Path) -> None:
    # MAJOR-2 end-to-end: even when a retry state_decision merges a downstream
    # restart_stage into a fresh raw-ready candidate, the submitted basin restarts
    # from convert rather than following the stale downstream marker.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    # A retry state with a residual downstream restart stage, but retention has
    # cleared canonical (readiness provider reports zero rows -> fresh).
    active_repository = PerModelCandidateStateRepository(
        {
            "model_a": {
                **_raw_ready_state(cycle_time),
                "hydro_status": "succeeded",
                "durable_shud_output_exists": True,
                "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
                "pipeline_status": "failed",
                "failed_stage": "parse",
                "error_code": "FAILED_PARSE",
                "retry_count": 1,
                "retry_limit": 3,
            },
        }
    )
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=active_repository,
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert forcing_producer.calls == []
    assert len(orchestrator.calls) == 1
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["restart_stage"] == "convert"
    assert submitted_basin["orchestration_run_id"].endswith("_convert_model_a")
    assert submitted_basin["state_evidence"]["fresh_ingestion"] == {
        "required": False,
        "mode": "reuse_raw_then_convert",
    }
    assert submitted_basin["state_evidence"]["restart_stage"] == "convert"


def test_canonical_unavailable_evidence_is_not_fresh_zero_row() -> None:
    # Highest-risk guardrail: provider-unavailable / query-failed evidence (no
    # candidate_row_count key, or status=canonical_unavailable) must NEVER be
    # treated as fresh zero-row -> otherwise a DB outage would trigger full-chain
    # ingestion submissions.
    assert scheduler_module._canonical_evidence_is_fresh_zero_row(None) is False
    assert scheduler_module._canonical_evidence_is_fresh_zero_row({}) is False
    # Missing candidate_row_count key (e.g. provider failure evidence).
    assert (
        scheduler_module._canonical_evidence_is_fresh_zero_row(
            {"ready": False, "status": "canonical_unavailable", "reason": "provider_unavailable"}
        )
        is False
    )
    # Explicit canonical_unavailable status even with a zero row count.
    assert (
        scheduler_module._canonical_evidence_is_fresh_zero_row(
            {
                "ready": False,
                "status": "canonical_unavailable",
                "candidate_row_count": 0,
                "expected_leads": (0, 3),
            }
        )
        is False
    )
    # A genuine zero-row evaluation with real expected leads IS fresh.
    assert (
        scheduler_module._canonical_evidence_is_fresh_zero_row(
            {
                "ready": False,
                "status": "canonical_incomplete",
                "candidate_row_count": 0,
                "expected_leads": (0, 3),
            }
        )
        is True
    )


def test_canonical_unavailable_cycle_stays_blocked_no_fresh_marker(tmp_path: Path) -> None:
    # End-to-end guardrail: an unavailable-canonical readiness must hard-block
    # the candidate with no fresh_ingestion marker and no submission.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forcing_producer = FakeForcingProducer(error=RuntimeError("raw manifest reuse skips in-process forcing"))
    orchestrator = FakeProductionOrchestrator()
    unavailable_evidence = {
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "ready": False,
        "status": "canonical_unavailable",
        "reason": "canonical_unavailable",
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=FakeCanonicalReadinessProvider({("gfs", cycle_time): unavailable_evidence}),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert orchestrator.calls == []
    assert forcing_producer.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "canonical_unavailable"
    assert "fresh_ingestion" not in blocked["state_evidence"]


def test_multi_basin_raw_manifest_reuse_submits_per_model_convert_restart(tmp_path: Path) -> None:
    # Raw-ready zero-canonical candidates restart at convert. Non-full restart
    # cohorts intentionally get one idempotent orchestration run per model.
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy = {"source": "gfs", "forecast_hours": [0, 3]}
    source_object = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    forcing_producer = FakeForcingProducer()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("qhh", "qhh_basin"), _model("heihe", "heihe_basin")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T06:00:00Z", True)],
                policy_identity=policy,
                source_object_identity=source_object,
            )
        },
        active_repository=FakeCandidateStateRepository(_raw_ready_state(cycle_time)),
        canonical_readiness_provider=_fresh_zero_row_readiness_provider(
            cycle_time, policy=policy, source_object=source_object
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert forcing_producer.calls == []
    assert result.evidence["counts"]["submitted_count"] == 2
    assert len(orchestrator.calls) == 2
    basins = [call["basins"][0] for call in orchestrator.calls]
    assert {basin["model_id"] for basin in basins} == {"qhh", "heihe"}
    for basin in basins:
        assert basin["restart_stage"] == "convert"
        assert basin["orchestration_run_id"].endswith(f"_convert_{basin['model_id']}")
        assert basin["state_evidence"]["fresh_ingestion"] == {
            "required": False,
            "mode": "reuse_raw_then_convert",
        }


def test_canonical_readiness_provider_absent_blocks_candidate_with_unavailable_evidence(tmp_path: Path) -> None:
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        canonical_readiness_provider=None,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "canonical_readiness_provider_absent"
    canonical = blocked["state_evidence"]["canonical_readiness"]
    assert canonical["status"] == "canonical_unavailable"
    assert canonical["ready"] is False
    assert canonical["dependency"]["name"] == "canonical_readiness_provider"
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_omitted_canonical_readiness_provider_blocks_candidate_with_unavailable_evidence(tmp_path: Path) -> None:
    scheduler = _RealProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "canonical_readiness_provider_absent"
    assert blocked["state_evidence"]["canonical_readiness"]["ready"] is False


def test_canonical_readiness_query_error_blocks_and_redacts_dependency_details(tmp_path: Path) -> None:
    class FailingReadinessProvider:
        def canonical_readiness(self, **_kwargs: Any) -> Mapping[str, Any]:
            raise RuntimeError("DATABASE_URL=postgres://user:super-secret@example.test/db token=secret-token")

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        canonical_readiness_provider=FailingReadinessProvider(),
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "canonical_readiness_query_failed"
    rendered = json.dumps(blocked)
    assert "super-secret" not in rendered
    assert "secret-token" not in rendered
    assert "DATABASE_URL" not in rendered
    assert blocked["state_evidence"]["canonical_readiness"]["failure"]["error_type"] == "RuntimeError"


def test_completed_duplicate_is_skipped_before_not_ready_canonical_gate(tmp_path: Path) -> None:
    class FailingReadinessProvider:
        def canonical_readiness(self, **_kwargs: Any) -> Mapping[str, Any]:
            raise AssertionError("completed candidates must not query canonical readiness")

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False, completed=True),
        canonical_readiness_provider=FailingReadinessProvider(),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "completed_duplicate_pipeline"


def test_build_candidates_duplicate_candidate_identity_records_skipped_and_exclusion(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([]),
        adapters={},
    )
    model = scheduler_module.RegisteredSchedulerModel(
        model_id="model_a",
        basin_id="basin_a",
        basin_version_id="basin_a_v1",
        river_network_version_id="basin_a_rivnet_v1",
        segment_count=3,
        output_segment_count=3,
        model_package_uri="s3://nhms/models/model_a/package/",
        shud_code_version="2.0",
        resource_profile={},
        resource_profile_summary={},
        display_capabilities={},
    )
    cycles = [
        scheduler_module.SchedulerSourceCycle(
            discovery=CycleDiscovery(
                cycle_id="gfs_2026052106_primary",
                source_id="gfs",
                cycle_time=cycle_time,
                cycle_hour=6,
                available=True,
                status="discovered",
            ),
            horizon={},
        ),
        scheduler_module.SchedulerSourceCycle(
            discovery=CycleDiscovery(
                cycle_id="gfs_2026052106_duplicate",
                source_id="gfs",
                cycle_time=cycle_time,
                cycle_hour=6,
                available=True,
                status="discovered",
            ),
            horizon={"max_lead_hours": 24},
        ),
    ]

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[model],
        cycles=cycles,
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_id == skipped[0]["candidate_id"]
    assert blocked == []
    assert slurm_sync == []
    assert skipped[0]["reason"] == "duplicate_candidate_identity"
    assert skipped[0]["status"] == "excluded"
    assert duplicate_exclusions == [{"type": "candidate", **skipped[0]}]


def test_build_candidates_uses_scheduler_candidate_state_decision_monkeypatch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    patched_evidence = {"source": "scheduler.py monkeypatch"}

    def patched_decision(candidate: Any, state: Mapping[str, Any] | None) -> scheduler_module.CandidateStateDecision:
        del candidate, state
        return scheduler_module.CandidateStateDecision(
            action="blocked",
            reason="patched_candidate_state_block",
            evidence=patched_evidence,
        )

    monkeypatch.setattr(scheduler_module, "_candidate_state_decision", patched_decision)
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository({}),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(_model("model_a", "basin_a"))],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=_dt("2026-05-21T06:00:00Z"),
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert candidates == []
    assert len(blocked) == 1
    assert blocked[0].reason == "patched_candidate_state_block"
    assert blocked[0].state_evidence == patched_evidence
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []


def test_build_candidates_uses_scheduler_max_candidates_monkeypatch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(scheduler_module, "MAX_CANDIDATES", 0)
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    with pytest.raises(scheduler_module.SchedulerResourceLimitError) as exc_info:
        scheduler._build_candidates(
            models=[scheduler_module._coerce_registered_model(_model("model_a", "basin_a"))],
            cycles=[
                scheduler_module.SchedulerSourceCycle(
                    discovery=CycleDiscovery(
                        cycle_id="gfs_2026052106",
                        source_id="gfs",
                        cycle_time=_dt("2026-05-21T06:00:00Z"),
                        cycle_hour=6,
                        available=True,
                        status="discovered",
                    ),
                    horizon={},
                )
            ],
        )

    assert exc_info.value.reason == "candidate_limit_exceeded"
    assert exc_info.value.details["max_candidates"] == 0
    assert exc_info.value.details["source_cycle_count"] == 1
    assert exc_info.value.details["selected_model_count"] == 1


@pytest.mark.parametrize(
    ("status", "reason", "classifier", "retryable", "expected_cycle_status_candidate"),
    [
        ("unavailable", "source_cycle_unavailable", "unavailable", True, "unavailable"),
        ("forbidden", "source_cycle_forbidden", "forbidden", False, "unavailable"),
        ("stale", "source_cycle_stale", "stale", True, "unavailable"),
        ("policy_blocked", "source_cycle_policy_blocked", "policy_blocked", False, "unavailable"),
    ],
)
def test_source_blocker_preserves_adapter_classifier_and_redacts_probe_credentials(
    tmp_path: Path,
    status: str,
    reason: str,
    classifier: str,
    retryable: bool,
    expected_cycle_status_candidate: str,
) -> None:
    signed_probe = "https://provider.example.test/file?token=super-secret&X-Amz-Signature=secret-signature"
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    (
                        "2026-05-21T06:00:00Z",
                        False,
                        {
                            "status": status,
                            "reason": reason,
                            "classifier": classifier,
                            "retryable": retryable,
                            "probe_uri": signed_probe,
                            "evidence": {
                                "probe": {"uri": signed_probe, "Authorization": "Bearer super-secret"},
                            },
                        },
                    )
                ],
            )
        },
        canonical_readiness_provider=None,
    )

    result = scheduler.run_once()

    source_cycle = result.evidence["source_cycles"][0]
    assert source_cycle["status"] == status
    assert source_cycle["cycle_status_candidate"] == expected_cycle_status_candidate
    blocked = result.evidence["blocked_candidates"][0]
    state = blocked["state_evidence"]
    assert blocked["reason"] == reason
    assert state["failure"]["classifier"] == classifier
    assert state["failure"]["retryable"] is retryable
    assert state["failure"]["permanent"] is (not retryable)
    assert state["retry_policy"]["automatic_retry_allowed"] is retryable
    assert state["identity"]["source_id"] == "gfs"
    assert state["identity"]["cycle_id"] == "gfs_2026052106"
    rendered = json.dumps(blocked)
    assert "super-secret" not in rendered
    assert "secret-signature" not in rendered


def test_allowed_cycle_hours_floor_to_prior_00_12_boundary() -> None:
    assert scheduler_module._floor_to_source_cycle_boundary(
        _dt("2026-05-21T06:59:00Z"),
        ("gfs",),
        allowed_cycle_hours_utc=(0, 12),
    ) == _dt("2026-05-21T00:00:00Z")
    assert scheduler_module._floor_to_source_cycle_boundary(
        _dt("2026-05-21T18:01:00Z"),
        ("gfs",),
        allowed_cycle_hours_utc=(0, 12),
    ) == _dt("2026-05-21T12:00:00Z")


def test_disallowed_cycle_hours_do_not_reach_candidates_readiness_forcing_or_submit(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-22T00:00:00Z"),
        dry_run=False,
        allowed_cycle_hours_utc=(0, 12),
    )
    orchestrator = FakeProductionOrchestrator()
    forcing_producer = FakeForcingProducer()

    class StrictReadinessProvider:
        def canonical_readiness(self, **_kwargs: Any) -> Mapping[str, Any]:
            raise AssertionError("canonical readiness must not be called for disallowed cycle hours")

    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    ("2026-05-21T06:00:00Z", True),
                    ("2026-05-21T18:00:00Z", True),
                ],
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=StrictReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["candidate_count"] == 0
    assert result.evidence["counts"]["source_cycle_count"] == 0
    assert result.evidence["counts"]["submitted_count"] == 0
    assert forcing_producer.calls == []
    assert orchestrator.calls == []
    excluded = [
        item for item in result.evidence["source_cycles"] if item.get("selection_reason") == "cycle_hour_not_allowed"
    ]
    assert [item["cycle_time_utc"] for item in excluded] == [
        "2026-05-21T06:00:00Z",
        "2026-05-21T18:00:00Z",
    ]
    assert {item["selection_status"] for item in excluded} == {"excluded"}


def test_pass_source_cycle_evidence_redacts_forbidden_probe_credentials(tmp_path: Path) -> None:
    signed_probe = (
        "https://user:password@provider.example.test/file?token=super-secret&X-Amz-Signature=secret-signature"
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    (
                        "2026-05-21T06:00:00Z",
                        False,
                        {
                            "status": "forbidden",
                            "reason": "source_cycle_forbidden",
                            "classifier": "forbidden",
                            "retryable": False,
                            "probe_uri": signed_probe,
                            "evidence": {
                                "probe": {
                                    "uri": signed_probe,
                                    "Authorization": "Bearer super-secret",
                                    "env_name": "AWS_SECRET_ACCESS_KEY",
                                    "env_value": "super-secret",
                                    "headers": {"X-Api-Key": "secret-api-key"},
                                }
                            },
                        },
                    )
                ],
            )
        },
    )

    result = scheduler.run_once()

    rendered = json.dumps(result.evidence)
    assert "super-secret" not in rendered
    assert "secret-signature" not in rendered
    assert "password" not in rendered
    assert "Authorization" not in rendered
    assert "AWS_SECRET_ACCESS_KEY" not in rendered
    assert "secret-api-key" not in rendered


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("run_id", "fcst_gfs_2026052106_other"),
        ("model_id", "model_b"),
        ("basin_id", "basin_b"),
        ("source", "IFS"),
        ("cycle_time", "2026-05-21T12:00:00Z"),
        ("basin_version_id", "basin_other_v1"),
        ("river_network_version_id", "river_other_v1"),
        ("canonical_product_id", "canon_gfs_2026052112"),
        ("forcing_version_id", "forc_gfs_2026052106_other"),
        ("hydro_run_id", "fcst_gfs_2026052106_other_hydro"),
        ("published_manifest_id", "manifest_other"),
    ],
)
def test_same_run_evidence_rejects_each_m23_identity_mismatch(field_name: str, replacement: str) -> None:
    expected = _production_identity_fixture()
    actual = {**expected, field_name: replacement}

    with pytest.raises(ProductionContractError) as exc_info:
        validate_same_production_identity(expected, actual)

    assert exc_info.value.code == "PRODUCTION_IDENTITY_MISMATCH"
    assert exc_info.value.field == field_name


def test_scheduler_candidate_state_identity_mismatch_blocks_evidence_reuse_before_submit(tmp_path: Path) -> None:
    state = {
        "pipeline_status": "succeeded",
        "pipeline_job": {
            "run_id": "fcst_gfs_2026052106_model_a",
            "model_id": "model_a",
            "basin_id": "basin_other",
            "source": "gfs",
            "cycle_time": "2026-05-21T06:00:00Z",
            "basin_version_id": "basin_a_v1",
            "river_network_version_id": "basin_a_rivnet_v1",
            "canonical_product_id": "canon_gfs_2026052106",
            "forcing_version_id": "forc_gfs_2026052106_model_a",
            "hydro_run_id": "fcst_gfs_2026052106_model_a",
            "published_manifest_id": "manifest_fcst_gfs_2026052106_model_a",
            "pipeline_job_id": "job_fcst_gfs_2026052106_model_a_forecast",
            "status": "succeeded",
        },
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    mismatch = blocked["state_evidence"]["production_identity_validation"]["mismatches"][0]
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert blocked["reason"] == "production_identity_mismatch"
    assert mismatch["code"] == "PRODUCTION_IDENTITY_MISMATCH"
    assert mismatch["field"] == "basin_id"


@pytest.mark.parametrize(
    ("state_key", "row_factory", "expected_source"),
    [
        (
            "pipeline_jobs",
            lambda identity: {
                **identity,
                "job_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "status": "running",
                "stage": "forecast",
                "slurm_job_id": "7777",
                "basin_id": "basin_other",
            },
            "pipeline_jobs[0]",
        ),
        (
            "jobs",
            lambda identity: {
                **identity,
                "job_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "status": "succeeded",
                "stage": "forecast",
                "basin_id": "basin_other",
            },
            "pipeline_jobs[0]",
        ),
        (
            "pipeline_events",
            lambda identity: {
                "event_id": 7,
                "entity_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "event_type": "status_change",
                "status_to": "running",
                "details": {
                    "identity": {**identity, "basin_id": "basin_other"},
                    "stage": "forecast",
                    "pipeline_event_id": "event_7",
                },
            },
            "pipeline_events[0].details.identity",
        ),
        (
            "pipeline_events",
            lambda identity: {
                "event_id": 8,
                "entity_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "event_type": "status_change",
                "status_to": "partially_failed",
                "details": {
                    "stage": "forcing",
                    "task_results": [{**identity, "basin_id": "basin_other", "task_id": 0, "status": "failed"}],
                },
            },
            "pipeline_events[0].details.task_results[0]",
        ),
    ],
)
def test_scheduler_candidate_state_list_identity_mismatch_blocks_before_reuse(
    tmp_path: Path,
    state_key: str,
    row_factory: Any,
    expected_source: str,
) -> None:
    identity = _production_identity_fixture()
    state = {
        state_key: [row_factory(identity)],
        "pipeline_status": "running",
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    mismatch = blocked["state_evidence"]["production_identity_validation"]["mismatches"][0]
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert blocked["reason"] == "production_identity_mismatch"
    assert mismatch["source"] == expected_source
    assert mismatch["field"] == "basin_id"


def test_scheduler_candidate_state_legacy_rows_without_m23_identity_remain_compatible(
    tmp_path: Path,
) -> None:
    state = {
        "pipeline_jobs": [
            {
                "job_id": "legacy_job_1",
                "run_id": "legacy_sibling_run",
                "model_id": "legacy_model",
                "status": "running",
                "stage": "forecast",
                "slurm_job_id": "7777",
            }
        ],
        "pipeline_events": [
            {
                "event_id": 9,
                "event_type": "status_change",
                "status_to": "running",
                "details": {"stage": "forecast"},
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls
    candidate = scheduler_module._candidate_for(
        discovery=CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        model=scheduler_module.RegisteredSchedulerModel(
            model_id="model_a",
            basin_id="basin_a",
            basin_version_id="basin_a_v1",
            river_network_version_id="basin_a_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri="s3://nhms/models/model_a/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)
    assert validation["status"] == "compatible"
    assert "pipeline_jobs[0]" in validation["legacy_non_authoritative"]


@pytest.mark.parametrize(
    "state",
    [
        {
            "pipeline_status": "running",
            "pipeline_jobs": [
                {
                    "pipeline_job_id": "job_unrelated_optional_only",
                    "status": "running",
                    "stage": "forecast",
                    "slurm_job_id": "7777",
                }
            ],
        },
        {
            "pipeline_status": "succeeded",
            "pipeline_events": [
                {
                    "event_id": 10,
                    "pipeline_event_id": "event_unrelated_optional_only",
                    "event_type": "status_change",
                    "status_to": "running",
                    "details": {
                        "pipeline_event_id": "event_unrelated_optional_only",
                        "status": "running",
                    },
                }
            ],
        },
    ],
)
def test_scheduler_candidate_state_optional_correlation_only_rows_are_non_authoritative(
    tmp_path: Path,
    state: dict[str, Any],
) -> None:
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert validation["status"] == "compatible"
    assert validation["legacy_non_authoritative"]
    assert validation["compared"] == {}
    assert orchestrator.calls


@pytest.mark.parametrize(
    "field_name",
    ["basin_id", "basin_version_id", "river_network_version_id", "canonical_product_id"],
)
def test_partial_shared_m23_fields_in_job_rows_are_compatible_but_non_authoritative(
    tmp_path: Path,
    field_name: str,
) -> None:
    identity = _production_identity_fixture()
    state = {
        "pipeline_status": "running",
        "pipeline_jobs": [
            {
                field_name: identity[field_name],
                "status": "running",
                "stage": "forecast",
                "slurm_job_id": "7777",
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "pipeline_jobs[0]" in validation["legacy_non_authoritative"]
    assert validation["compared"]["pipeline_jobs[0]"] == {field_name: identity[field_name]}
    assert orchestrator.calls


@pytest.mark.parametrize(
    ("field_name", "actual_value"),
    [
        ("basin_id", "basin_other"),
        ("basin_version_id", "basin_other_v1"),
        ("river_network_version_id", "river_other_v1"),
        ("canonical_product_id", "canon_gfs_2026052112"),
    ],
)
def test_partial_shared_m23_field_mismatches_still_block(
    tmp_path: Path,
    field_name: str,
    actual_value: str,
) -> None:
    state = {
        "pipeline_jobs": [
            {
                field_name: actual_value,
                "status": "running",
                "stage": "forecast",
                "slurm_job_id": "7777",
            }
        ],
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    mismatch = blocked["state_evidence"]["production_identity_validation"]["mismatches"][0]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert blocked["reason"] == "production_identity_mismatch"
    assert mismatch["source"] == "pipeline_jobs[0]"
    assert mismatch["field"] == field_name
    assert mismatch["actual"] == actual_value


def test_partial_shared_m23_top_level_terminal_success_is_non_authoritative(
    tmp_path: Path,
) -> None:
    identity = _production_identity_fixture()
    state = {
        "basin_id": identity["basin_id"],
        "hydro_status": "succeeded",
        "output_uri": "s3://nhms/runs/stale_sibling/output/",
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "candidate_state" in validation["legacy_non_authoritative"]
    assert validation["compared"]["candidate_state"] == {"basin_id": identity["basin_id"]}
    assert orchestrator.calls


def test_partial_shared_m23_singleton_job_is_non_authoritative(
    tmp_path: Path,
) -> None:
    identity = _production_identity_fixture()
    state = {
        "pipeline_status": "running",
        "pipeline_job": {
            "river_network_version_id": identity["river_network_version_id"],
            "status": "running",
            "stage": "forecast",
            "slurm_job_id": "7777",
        },
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "pipeline_job" in validation["legacy_non_authoritative"]
    assert "pipeline_jobs[0]" in validation["legacy_non_authoritative"]
    assert orchestrator.calls


@pytest.mark.parametrize(
    "state",
    [
        {
            "pipeline_events": [
                {
                    "event_id": 11,
                    "event_type": "status_change",
                    "status_to": "running",
                    "details": {
                        "basin_version_id": "basin_a_v1",
                        "stage": "forecast",
                        "status": "running",
                    },
                }
            ],
        },
        {
            "pipeline_events": [
                {
                    "event_id": 12,
                    "event_type": "status_change",
                    "details": {
                        "stage": "forecast",
                        "status": "running",
                        "task_results": [
                            {
                                "canonical_product_id": "canon_gfs_2026052106",
                                "task_id": 0,
                                "status": "failed",
                            }
                        ],
                    },
                }
            ],
        },
    ],
)
def test_partial_shared_m23_event_details_and_tasks_are_non_authoritative(
    tmp_path: Path,
    state: dict[str, Any],
) -> None:
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "pipeline_events[0]" in validation["legacy_non_authoritative"]
    assert any(source.startswith("pipeline_events[0].details") for source in validation["legacy_non_authoritative"])
    assert orchestrator.calls


@pytest.mark.parametrize(
    "state",
    [
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "NODE_FAILURE",
            "pipeline_jobs": [
                {
                    "basin_id": "basin_a",
                    "status": "failed",
                    "stage": "forecast",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 1,
                }
            ],
        },
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "forecast",
            "error_code": "INVALID_MANIFEST",
            "pipeline_jobs": [
                {
                    "basin_version_id": "basin_a_v1",
                    "status": "permanently_failed",
                    "stage": "forecast",
                    "error_code": "INVALID_MANIFEST",
                    "retry_count": 3,
                }
            ],
        },
        {
            "pipeline_status": "cancelled",
            "pipeline_jobs": [
                {
                    "river_network_version_id": "basin_a_rivnet_v1",
                    "status": "cancelled",
                    "stage": "forecast",
                    "retry_count": 1,
                }
            ],
        },
        {
            "pipeline_events": [
                {
                    "event_id": 13,
                    "event_type": "retry",
                    "created_at": "2026-05-21T06:30:00Z",
                    "details": {
                        "canonical_product_id": "canon_gfs_2026052106",
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 4,
                    },
                }
            ],
        },
    ],
)
def test_partial_shared_m23_rows_do_not_drive_retry_block_cancel_or_manual_retry(
    tmp_path: Path,
    state: dict[str, Any],
) -> None:
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert validation["legacy_non_authoritative"]
    assert orchestrator.calls


@pytest.mark.parametrize(
    "proof_kind",
    [
        "full_tuple",
        "run_id",
        "forcing_version_id",
        "hydro_run_id",
        "published_manifest_id",
    ],
)
def test_full_tuple_and_candidate_scoped_m23_proofs_remain_authoritative(
    tmp_path: Path,
    proof_kind: str,
) -> None:
    identity = _production_identity_fixture()
    proof = identity if proof_kind == "full_tuple" else {proof_kind: identity[proof_kind]}
    state = {
        "pipeline_jobs": [
            {
                **proof,
                "status": "running",
                "stage": "forecast",
                "slurm_job_id": "7777",
            }
        ],
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()
    validation = scheduler_module._candidate_state_identity_validation(_scheduler_candidate_fixture(), state)

    skipped = result.evidence["skipped_candidates"][0]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert skipped["reason"] == "active_slurm_job"
    assert "pipeline_jobs[0]" not in validation["legacy_non_authoritative"]


def test_candidate_state_decision_ignores_local_jobs_as_active_slurm() -> None:
    candidate = _scheduler_candidate_fixture()

    decision = scheduler_module._candidate_state_decision(
        candidate,
        {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
            "active_slurm_jobs": [
                {
                    "job_id": "job_local_publish",
                    "slurm_job_id": "local",
                    "status": "running",
                    "stage": "publish",
                }
            ],
            "pipeline_jobs": [
                {
                    "job_id": "job_local_publish",
                    "slurm_job_id": "local",
                    "status": "running",
                    "stage": "publish",
                }
            ],
        },
    )

    assert decision is None


def test_candidate_state_decision_scheduler_monkeypatch_active_jobs_compat(monkeypatch: Any) -> None:
    candidate = _scheduler_candidate_fixture()
    patched_job = {
        "job_id": "job_patched_old_path",
        "slurm_job_id": "patched-old-path",
        "status": "running",
        "source": "scheduler.py monkeypatch",
    }

    def patched_active_jobs(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [patched_job]

    monkeypatch.setattr(scheduler_module, "_state_active_jobs", patched_active_jobs)

    decision = scheduler_module._candidate_state_decision(
        candidate,
        {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
            "pipeline_jobs": [],
        },
    )

    assert decision is not None
    assert decision.action == "skip"
    assert decision.reason == "active_slurm_job"
    assert decision.evidence["active_slurm_jobs"] == [patched_job]


def test_candidate_state_decision_scheduler_monkeypatch_raw_manifest_repair_compat(
    monkeypatch: Any,
) -> None:
    candidate = _scheduler_candidate_fixture()
    manifest_uri = "s3://nhms/raw/gfs/2026052106/manifest.json"
    calls: list[tuple[str, str]] = []

    def patched_manifest_missing(patched_candidate: Any, patched_manifest_uri: str) -> bool:
        calls.append((patched_candidate.candidate_id, patched_manifest_uri))
        return True

    monkeypatch.setattr(scheduler_module, "_object_manifest_is_missing", patched_manifest_missing)

    decision = scheduler_module._candidate_state_decision(
        candidate,
        {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
            "forecast_cycle": {
                "cycle_id": candidate.cycle_id,
                "source_id": candidate.source_id,
                "cycle_time": candidate.cycle_time_utc,
                "manifest_uri": manifest_uri,
            },
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_download",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": candidate.cycle_id,
                    "status": "succeeded",
                    "stage": "download",
                    "job_type": "download_source_cycle",
                    "updated_at": "2026-05-21T06:02:00Z",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_convert",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": candidate.cycle_id,
                    "status": "failed",
                    "stage": "convert",
                    "job_type": "convert_canonical",
                    "error_code": "INVALID_MANIFEST",
                    "retry_count": 3,
                    "updated_at": "2026-05-21T06:03:00Z",
                },
            ],
            "pipeline_status": "failed",
            "failed_stage": "convert",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "retry_limit": 3,
        },
    )

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "repair_missing_raw_manifest"
    assert decision.evidence["reason"] == "repair_missing_raw_manifest"
    assert decision.evidence["raw_manifest_repair"]["manifest_exists"] is False
    assert calls == [(candidate.candidate_id, manifest_uri)]


def test_completed_stage_retry_supersedes_stale_hydro_created_placeholder() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run": {
            "run_id": candidate.run_id,
            "status": "created",
        },
        "hydro_status": "created",
        "pipeline_status": "succeeded",
        "stage": "convert",
        "completed_stage_evidence": {
            "stage": "convert",
            "status": "succeeded",
            "restart_stage": "forcing",
        },
    }

    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "resume_after_completed_stage"
    assert decision.evidence["restart_stage"] == "forcing"


def test_candidate_state_rows_mark_completed_upstream_success_for_resume() -> None:
    candidate = _scheduler_candidate_fixture()
    state = chain_repository_state_module.candidate_state_from_rows(
        source_id=candidate.source_id,
        cycle_time=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        hydro_run={
            "run_id": candidate.run_id,
            "status": "created",
        },
        pipeline_jobs=[
            {
                "job_id": "job_cycle_gfs_2026052106_full_model_a_forecast",
                "run_id": "cycle_gfs_2026052106_full_model_a",
                "cycle_id": candidate.cycle_id,
                "model_id": candidate.model_id,
                "candidate_id": "cycle_gfs_2026052106_full_model_a",
                "status": "succeeded",
                "stage": "forecast",
                "job_type": "run_shud_forecast_array",
                "slurm_job_id": "3001",
                "updated_at": "2026-05-21T06:45:00Z",
            }
        ],
        pipeline_events=[],
        forcing_version=None,
        forecast_cycle=None,
    )

    assert state is not None
    assert state["completed_stage_evidence"]["stage"] == "forecast"
    assert state["completed_stage_evidence"]["restart_stage"] == "parse"
    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "resume_after_completed_stage"
    assert decision.evidence["restart_stage"] == "parse"


def test_candidate_state_decision_owner_module_matches_scheduler_facade() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
        "retry_count": 1,
        "pipeline_jobs": [
            {
                "job_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "candidate_id": candidate.candidate_id,
                "run_id": candidate.run_id,
                "forcing_version_id": candidate.forcing_version_id,
                "model_id": candidate.model_id,
                "basin_id": candidate.basin_id,
                "status": "failed",
                "stage": "forecast",
                "error_code": "NODE_FAILURE",
                "retry_count": 1,
            },
        ],
    }

    assert scheduler_module._candidate_state_decision(
        candidate,
        state,
    ) == scheduler_state_module._candidate_state_decision(candidate, state)


def test_nested_task_proof_does_not_authorize_parent_event_active_status(
    tmp_path: Path,
) -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "pipeline_events": [
            {
                "event_id": 101,
                "event_type": "status_change",
                "status_to": "running",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "stage": "forecast",
                    "task_results": [
                        {
                            "run_id": candidate.run_id,
                            "task_id": 0,
                            "status": "succeeded",
                        }
                    ],
                },
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "pipeline_events[0]" in validation["legacy_non_authoritative"]
    assert "pipeline_events[0].details.task_results[0]" not in validation["legacy_non_authoritative"]
    assert orchestrator.calls


def test_nested_task_proof_does_not_authorize_parent_event_manual_retry(
    tmp_path: Path,
) -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "pipeline_events": [
            {
                "event_id": 102,
                "event_type": "retry",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "task_results": [
                        {
                            "run_id": candidate.run_id,
                            "task_id": 0,
                            "status": "succeeded",
                        }
                    ],
                },
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert "pipeline_events[0]" in validation["legacy_non_authoritative"]
    assert "pipeline_events[0].details.task_results[0]" not in validation["legacy_non_authoritative"]
    assert orchestrator.calls


def test_sibling_array_task_result_does_not_block_current_candidate(
    tmp_path: Path,
) -> None:
    candidate = _scheduler_candidate_fixture()
    identity = _production_identity_fixture()
    sibling_identity = {
        **identity,
        "run_id": "fcst_gfs_2026052106_model_b",
        "model_id": "model_b",
        "basin_id": "basin_b",
        "forcing_version_id": "forc_gfs_2026052106_model_b",
        "hydro_run_id": "fcst_gfs_2026052106_model_b",
        "published_manifest_id": "manifest_fcst_gfs_2026052106_model_b",
    }
    state = {
        "pipeline_events": [
            {
                "event_id": 103,
                "event_type": "status_change",
                "status_to": "succeeded",
                "details": {
                    "stage": "forcing",
                    "task_results": [
                        {**sibling_identity, "task_id": 0, "status": "succeeded"},
                        {**identity, "task_id": 1, "status": "succeeded"},
                    ],
                },
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert decision is None
    assert validation["status"] == "compatible"
    assert "pipeline_events[0].details.task_results[0]" in validation["legacy_non_authoritative"]
    assert "pipeline_events[0].details.task_results[1]" not in validation["legacy_non_authoritative"]
    assert "pipeline_events[0].details.task_results[0]" not in validation["compared"]
    assert validation["compared"]["pipeline_events[0].details.task_results[1]"]["run_id"] == candidate.run_id
    assert orchestrator.calls


def test_nested_failed_task_identity_remains_available_when_failure_state_is_candidate_scoped() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
        "retry_count": 1,
        "pipeline_events": [
            {
                "event_id": 103,
                "event_type": "status_change",
                "status_to": "failed",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "stage": "forecast",
                    "task_results": [
                        {
                            "run_id": candidate.run_id,
                            "task_id": 2,
                            "array_task_id": 2,
                            "original_task_id": 12,
                            "status": "failed",
                            "error_code": "NODE_FAILURE",
                            "slurm_job_id": "slurm_task_2",
                        }
                    ],
                },
            }
        ],
    }

    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "retry_failed_candidate"
    assert decision.evidence["task_identity"]["array_task_id"] == 2
    assert decision.evidence["task_identity"]["task_id"] == 2
    assert decision.evidence["task_identity"]["stage"] == "forecast"
    assert "pipeline_events[0]" in validation["legacy_non_authoritative"]


def test_non_authoritative_task_results_do_not_populate_retry_task_identity() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
        "retry_count": 1,
        "pipeline_events": [
            {
                "event_id": 104,
                "event_type": "status_change",
                "status_to": "failed",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "stage": "forecast",
                    "task_results": [
                        {
                            "task_id": 9,
                            "array_task_id": 9,
                            "status": "failed",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                },
            }
        ],
    }

    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    decision_state = scheduler_module._candidate_state_decision_state(state, evidence)
    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = evidence["production_identity_validation"]

    assert "pipeline_events[0].details.task_results[0]" in validation["legacy_non_authoritative"]
    assert scheduler_module._state_task_identity(decision_state) == {}
    assert decision is not None
    assert decision.action == "retry"
    assert decision.evidence["task_identity"] == {}
    assert scheduler_module._candidate_state_is_candidate_scoped_retry(decision) is True


def test_candidate_state_evidence_preserves_repaired_stage_metadata_additively() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "repair_status": "repaired",
                "superseded_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                "active_blocker": False,
            },
            {
                "job_id": "job_cycle_gfs_2026052106_retry_active",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "succeeded",
                "repair_status": "repair_succeeded",
                "repairs_job_id": "job_cycle_gfs_2026052106_download",
            },
        ],
        "repaired_stage_evidence": {
            "status": "repaired",
            "repair_status": "repaired",
            "stage": "download",
            "job_type": "download_source_cycle",
            "original_failed_job_id": "job_cycle_gfs_2026052106_download",
            "repairing_retry_job_id": "job_cycle_gfs_2026052106_retry_active",
            "manifest_uri": "raw/gfs/2026052106/manifest.json",
            "forecast_cycle_status": "raw_complete",
        },
    }

    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is None
    assert evidence["repaired_stage_evidence"]["status"] == "repaired"
    assert evidence["repaired_stage_evidence"]["original_failed_job_id"] == "job_cycle_gfs_2026052106_download"
    assert evidence["pipeline_jobs"][0]["repair_status"] == "repaired"
    assert evidence["pipeline_jobs"][0]["active_blocker"] is False
    assert evidence["pipeline_jobs"][1]["repairs_job_id"] == "job_cycle_gfs_2026052106_download"


def test_candidate_state_decision_ignores_repaired_source_cycle_failure_with_retry_event() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_status": None,
        "failed_stage": None,
        "error_code": None,
        "retry_count": 0,
        "retry_limit": 3,
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "error_code": "NODE_FAILURE",
                "retry_count": 0,
                "updated_at": "2026-05-21T06:10:00Z",
                "repair_status": "repaired",
                "superseded_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                "repaired_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                "active_blocker": False,
            },
            {
                "job_id": "job_cycle_gfs_2026052106_retry_active",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "succeeded",
                "retry_count": 1,
                "updated_at": "2026-05-21T06:30:00Z",
                "repair_status": "repair_succeeded",
                "repairs_job_id": "job_cycle_gfs_2026052106_download",
            },
        ],
        "pipeline_events": [
            {
                "event_id": 501,
                "event_type": "retry",
                "run_id": "cycle_gfs_2026052106",
                "entity_id": "job_cycle_gfs_2026052106_retry_active",
                "created_at": "2026-05-21T06:20:00Z",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "previous_job_id": "job_cycle_gfs_2026052106_download",
                    "retry_job_id": "job_cycle_gfs_2026052106_retry_active",
                    "retry_count": 1,
                    "prior_failure_reason": "NODE_FAILURE",
                },
            }
        ],
        "repaired_stage_evidence": {
            "status": "repaired",
            "repair_status": "repaired",
            "stage": "download",
            "job_type": "download_source_cycle",
            "original_failed_job_id": "job_cycle_gfs_2026052106_download",
            "repairing_retry_job_id": "job_cycle_gfs_2026052106_retry_active",
            "manual_retry_event_id": 501,
            "manual_retry_marker": True,
            "manifest_uri": "raw/gfs/2026052106/manifest.json",
            "forecast_cycle_status": "raw_complete",
        },
    }

    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is None
    assert evidence["pipeline_jobs"][0]["repair_status"] == "repaired"
    assert evidence["pipeline_events"][0]["event_id"] == 501
    assert evidence["repaired_stage_evidence"]["manual_retry_event_id"] == 501


def test_candidate_state_shared_source_cycle_failure_blocks_without_candidate_scoped_row(
    tmp_path: Path,
) -> None:
    state = {
        "shared_cycle_aggregate": True,
        "pipeline_status": "permanently_failed",
        "failed_stage": "download",
        "error_code": "SLURM_JOB_FAILED",
        "retry_count": 0,
        "retry_limit": 3,
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "error_code": "SLURM_JOB_FAILED",
                "retry_count": 0,
                "updated_at": "2026-05-21T06:10:00Z",
            }
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)

    assert decision is not None
    assert decision.action in {"blocked", "retry"}
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["state_evidence"]["decision"] in {"permanent_failure", "retry_failed"}
    assert blocked["state_evidence"]["pipeline_jobs"][0]["job_id"] == "job_cycle_gfs_2026052106_download"


def test_candidate_state_truncated_latest_source_cycle_failure_blocks_scheduler(
    tmp_path: Path,
) -> None:
    state = {
        "shared_cycle_aggregate": True,
        "state_truncated": True,
        "pipeline_jobs_total": 4,
        "pipeline_status": "permanently_failed",
        "failed_stage": "download",
        "error_code": "LATER_UNREPAIRED",
        "retry_count": 3,
        "retry_limit": 3,
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "error_code": "NODE_FAILURE",
                "retry_count": 1,
                "updated_at": "2026-05-21T07:00:00Z",
                "repair_status": "repaired",
                "superseded_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                "repaired_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                "active_blocker": False,
            },
            {
                "job_id": "job_cycle_gfs_2026052106_retry_active",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "succeeded",
                "retry_count": 2,
                "updated_at": "2026-05-21T06:35:00Z",
                "repair_status": "repair_succeeded",
                "repairs_job_id": "job_cycle_gfs_2026052106_download",
            },
            {
                "job_id": "job_cycle_gfs_2026052106_retry_3",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "permanently_failed",
                "error_code": "LATER_UNREPAIRED",
                "retry_count": 3,
                "updated_at": "2026-05-21T08:00:00Z",
            },
        ],
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)

    assert decision is not None
    assert decision.action == "blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    candidate_state = blocked["state_evidence"]
    assert blocked["reason"] == "permanent_failure_guard"
    assert candidate_state["decision"] == "permanent_failure"
    assert candidate_state["stage"] == "download"
    assert candidate_state["failure"]["reason_code"] == "LATER_UNREPAIRED"
    assert "source_cycle_repair_evidence" not in candidate_state
    repaired_job = next(job for job in candidate_state["pipeline_jobs"] if job["repair_status"] == "repaired")
    latest_failed_job = next(
        job for job in candidate_state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026052106_retry_3"
    )
    assert repaired_job["active_blocker"] is False
    assert latest_failed_job["error_code"] == "LATER_UNREPAIRED"


def test_candidate_state_inconclusive_truncated_evidence_is_preserved_on_proceed(
    tmp_path: Path,
) -> None:
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": None,
            "failed_stage": None,
            "error_code": None,
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_download",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "download_source_cycle",
                    "stage": "download",
                    "status": "failed",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 0,
                    "updated_at": "2026-05-21T06:10:00Z",
                }
            ],
            "source_cycle_repair_evidence": {
                "status": "inconclusive_truncated",
                "truncated": True,
                "reason": "source_cycle_repair_window_truncated",
                "unresolved_failed_job_ids": ["job_cycle_gfs_2026052106_download"],
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    candidate_state = result.evidence["candidates"][0]["state_evidence"]["candidate_state"]
    submitted_state = orchestrator.calls[0]["basins"][0]["state_evidence"]["candidate_state"]
    for state in (candidate_state, submitted_state):
        assert "decision" not in state
        assert state["source_cycle_repair_evidence"]["status"] == "inconclusive_truncated"
        assert state["source_cycle_repair_evidence"]["unresolved_failed_job_ids"] == [
            "job_cycle_gfs_2026052106_download"
        ]
        assert state["pipeline_jobs"][0]["job_id"] == "job_cycle_gfs_2026052106_download"


def test_candidate_state_inconclusive_truncated_unresolved_job_does_not_drive_retry() -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_status": None,
        "failed_stage": None,
        "error_code": None,
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": "gfs_2026052106",
                "job_type": "download_source_cycle",
                "stage": "download",
                "status": "failed",
                "error_code": "NODE_FAILURE",
                "retry_count": 0,
                "updated_at": "2026-05-21T06:10:00Z",
            }
        ],
        "source_cycle_repair_evidence": {
            "status": "inconclusive_truncated",
            "truncated": True,
            "reason": "source_cycle_repair_window_truncated",
            "unresolved_failed_job_ids": ["job_cycle_gfs_2026052106_download"],
        },
    }

    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    decision_state = scheduler_module._candidate_state_decision_state(state, evidence)
    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is None
    assert decision_state["pipeline_jobs"] == []
    assert scheduler_module._state_has_failure_signal(decision_state) is False
    assert evidence["source_cycle_repair_evidence"]["status"] == "inconclusive_truncated"
    assert evidence["pipeline_jobs"][0]["job_id"] == "job_cycle_gfs_2026052106_download"


def test_candidate_state_inconclusive_truncated_unresolved_job_does_not_drive_manual_retry(
    tmp_path: Path,
) -> None:
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": None,
            "failed_stage": None,
            "error_code": None,
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_download",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "download_source_cycle",
                    "stage": "download",
                    "status": "failed",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 0,
                    "updated_at": "2026-05-21T06:10:00Z",
                }
            ],
            "pipeline_events": [
                {
                    "event_id": 501,
                    "event_type": "retry",
                    "run_id": "cycle_gfs_2026052106",
                    "entity_id": "job_cycle_gfs_2026052106_retry_active",
                    "created_at": "2026-05-21T06:20:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "previous_job_id": "job_cycle_gfs_2026052106_download",
                        "retry_job_id": "job_cycle_gfs_2026052106_retry_active",
                        "retry_count": 1,
                        "prior_failure_reason": "NODE_FAILURE",
                    },
                }
            ],
            "source_cycle_repair_evidence": {
                "status": "inconclusive_truncated",
                "truncated": True,
                "reason": "source_cycle_repair_window_truncated",
                "unresolved_failed_job_ids": ["job_cycle_gfs_2026052106_download"],
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    candidate_state = result.evidence["candidates"][0]["state_evidence"]["candidate_state"]
    assert "decision" not in candidate_state
    assert candidate_state["pipeline_events"][0]["event_id"] == 501
    assert candidate_state["source_cycle_repair_evidence"]["status"] == "inconclusive_truncated"


def test_scheduler_pass_candidate_evidence_carries_repaired_stage_metadata_for_operators(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_download",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "download_source_cycle",
                    "stage": "download",
                    "status": "permanently_failed",
                    "repair_status": "repaired",
                    "superseded_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                    "repaired_by_job_id": "job_cycle_gfs_2026052106_retry_active",
                    "active_blocker": False,
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_retry_active",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "download_source_cycle",
                    "stage": "download",
                    "status": "succeeded",
                    "repair_status": "repair_succeeded",
                    "repairs_job_id": "job_cycle_gfs_2026052106_download",
                },
            ],
            "pipeline_status": None,
            "failed_stage": None,
            "error_code": None,
            "pipeline_events": [
                {
                    "event_id": 501,
                    "event_type": "retry",
                    "run_id": "cycle_gfs_2026052106",
                    "entity_id": "job_cycle_gfs_2026052106_retry_active",
                    "created_at": "2026-05-21T06:20:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "previous_job_id": "job_cycle_gfs_2026052106_download",
                        "retry_job_id": "job_cycle_gfs_2026052106_retry_active",
                        "retry_count": 1,
                        "prior_failure_reason": "NODE_FAILURE",
                    },
                }
            ],
            "repaired_stage_evidence": {
                "status": "repaired",
                "repair_status": "repaired",
                "stage": "download",
                "job_type": "download_source_cycle",
                "original_failed_job_id": "job_cycle_gfs_2026052106_download",
                "repairing_retry_job_id": "job_cycle_gfs_2026052106_retry_active",
                "manifest_uri": "s3://nhms-prod/qhh/raw/gfs/2026052106/manifest.json",
                "forecast_cycle_status": "raw_complete",
                "source_id": "gfs",
                "cycle_id": "gfs_2026052106",
                "cycle_time": "2026-05-21T06:00:00Z",
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    candidate_state = result.evidence["candidates"][0]["state_evidence"]["candidate_state"]
    model_run_state = result.evidence["model_run_evidence"][0]["state_evidence"]["candidate_state"]
    submitted_state = orchestrator.calls[0]["basins"][0]["state_evidence"]["candidate_state"]
    for state in (candidate_state, model_run_state, submitted_state):
        assert "decision" not in state
        assert "failure" not in state
        assert state["repaired_stage_evidence"]["original_failed_job_id"] == "job_cycle_gfs_2026052106_download"
        assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == ("job_cycle_gfs_2026052106_retry_active")
        assert state["repaired_stage_evidence"]["manifest_uri"] == (
            "s3://nhms-prod/qhh/raw/gfs/2026052106/manifest.json"
        )
        failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026052106_download")
        assert failed_job["repair_status"] == "repaired"
        assert failed_job["active_blocker"] is False
        assert state["pipeline_events"][0]["event_id"] == 501
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []


def test_non_authoritative_task_results_preserve_candidate_scoped_retry(
    tmp_path: Path,
) -> None:
    candidate = _scheduler_candidate_fixture()
    state = {
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
        "retry_count": 1,
        "pipeline_events": [
            {
                "event_id": 105,
                "event_type": "status_change",
                "status_to": "failed",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "stage": "forecast",
                    "task_results": [
                        {
                            "task_id": 9,
                            "array_task_id": 9,
                            "status": "failed",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                },
            }
        ],
    }

    class ActiveCycleRawCandidateStateRepository(RawCandidateStateRepository):
        def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
            del source_id, cycle_time
            return True

    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=ActiveCycleRawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["state_evidence"]["decision"] == "retry_failed"
    assert submitted_basin["state_evidence"]["task_identity"] == {}
    assert submitted_basin["restart_stage"] == "forecast"


@pytest.mark.parametrize(
    ("event", "expected_action", "expected_reason"),
    [
        (
            {
                "event_id": 104,
                "event_type": "status_change",
                "run_id": "fcst_gfs_2026052106_model_a",
                "status_to": "running",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {"stage": "forecast"},
            },
            "skip",
            "active_duplicate_pipeline",
        ),
        (
            {
                "event_id": 105,
                "event_type": "retry",
                "run_id": "fcst_gfs_2026052106_model_a",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "prior_failure_reason": "NODE_FAILURE",
                },
            },
            "retry",
            "manual_retry_requested",
        ),
    ],
)
def test_parent_event_with_event_level_candidate_proof_remains_authoritative(
    event: dict[str, Any],
    expected_action: str,
    expected_reason: str,
) -> None:
    candidate = _scheduler_candidate_fixture()
    state = {"pipeline_events": [event]}

    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)

    assert decision is not None
    assert decision.action == expected_action
    assert decision.reason == expected_reason
    assert "pipeline_events[0]" not in validation["legacy_non_authoritative"]


def test_manual_retry_overrides_stale_created_hydro_placeholder_after_permanent_pipeline_failure() -> None:
    candidate = _scheduler_candidate_fixture()
    identity = _production_identity_fixture()
    failed_job_id = "job_cycle_gfs_2026052106_model_a_forecast_retry_3"
    state = {
        **identity,
        "candidate_id": candidate.candidate_id,
        "hydro_status": "created",
        "pipeline_status": "permanently_failed",
        "retry_limit": 3,
        "pipeline_jobs": [
            {
                **identity,
                "job_id": failed_job_id,
                "status": "permanently_failed",
                "stage": "forecast",
                "retry_count": 3,
                "error_code": "NODE_FAILURE",
                "updated_at": "2026-05-21T06:57:16Z",
            }
        ],
        "pipeline_events": [
            {
                "event_id": 101,
                "entity_id": failed_job_id,
                "event_type": "permanently_failed",
                "status_from": "failed",
                "status_to": "permanently_failed",
                "created_at": "2026-05-21T06:57:16Z",
                "details": {**identity, "final_retry_count": 3, "last_error": "NODE_FAILURE"},
            },
            {
                "event_id": 102,
                "entity_id": failed_job_id,
                "event_type": "retry",
                "status_from": "permanently_failed",
                "status_to": "manual_repair_requested",
                "created_at": "2026-05-21T07:06:07Z",
                "details": {
                    **identity,
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "previous_job_id": failed_job_id,
                    "prior_failure_reason": "NODE_FAILURE",
                },
            },
        ],
    }

    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "manual_retry_requested"
    assert decision.evidence["manual_retry"]["new_attempt"] == 4
    assert scheduler_module._candidate_state_is_candidate_scoped_retry(decision) is True


def test_manual_retry_candidate_bypasses_repository_active_placeholder(
    tmp_path: Path,
) -> None:
    class ActivePlaceholderManualRetryRepository(FakeCandidateStateRepository):
        def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

    candidate = _scheduler_candidate_fixture()
    identity = _production_identity_fixture()
    failed_job_id = "job_cycle_gfs_2026052106_model_a_forecast_retry_3"
    active_repository = ActivePlaceholderManualRetryRepository(
        {
            **identity,
            "candidate_id": candidate.candidate_id,
            "hydro_status": "created",
            "pipeline_status": "permanently_failed",
            "retry_limit": 3,
            "pipeline_jobs": [
                {
                    **identity,
                    "job_id": failed_job_id,
                    "status": "permanently_failed",
                    "stage": "forecast",
                    "retry_count": 3,
                    "error_code": "NODE_FAILURE",
                    "updated_at": "2026-05-21T06:57:16Z",
                }
            ],
            "pipeline_events": [
                {
                    "event_id": 101,
                    "entity_id": failed_job_id,
                    "event_type": "permanently_failed",
                    "status_from": "failed",
                    "status_to": "permanently_failed",
                    "created_at": "2026-05-21T06:57:16Z",
                    "details": {**identity, "final_retry_count": 3, "last_error": "NODE_FAILURE"},
                },
                {
                    "event_id": 102,
                    "entity_id": failed_job_id,
                    "event_type": "retry",
                    "status_from": "permanently_failed",
                    "status_to": "manual_repair_requested",
                    "created_at": "2026-05-21T07:06:07Z",
                    "details": {
                        **identity,
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 4,
                        "previous_job_id": failed_job_id,
                        "prior_failure_reason": "NODE_FAILURE",
                    },
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["candidates"][0]["state_evidence"]["decision"] == "manual_retry"
    assert orchestrator.calls[0]["basins"][0]["manual_retry_attempt"] == 4


def test_terminal_pipeline_success_overrides_stale_created_hydro_placeholder() -> None:
    candidate = _scheduler_candidate_fixture()
    identity = _production_identity_fixture()
    state = {
        **identity,
        "candidate_id": candidate.candidate_id,
        "hydro_status": "created",
        "pipeline_status": "succeeded",
        "pipeline_jobs": [
            {
                **identity,
                "job_id": "job_cycle_gfs_2026052106_forecast_model_a_state_save_qc",
                "status": "succeeded",
                "stage": "state_save_qc",
                "error_code": None,
                "updated_at": "2026-05-21T06:45:00Z",
            }
        ],
    }

    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is not None
    assert decision.action == "skip"
    assert decision.reason == "terminal_pipeline_success"
    assert decision.evidence["terminal_source"] == "pipeline_job"
    assert decision.evidence["terminal_status"] == "succeeded"


def test_terminal_state_save_event_overrides_stale_permanent_pipeline_failure() -> None:
    candidate = _scheduler_candidate_fixture()
    identity = _production_identity_fixture()
    state = {
        **identity,
        "candidate_id": candidate.candidate_id,
        "hydro_status": "failed",
        "pipeline_status": "permanently_failed",
        "failed_stage": "forecast",
        "error_code": "COLD_START_QUARANTINED",
        "pipeline_jobs": [
            {
                **identity,
                "job_id": "job_cycle_gfs_2026052106_forecast_model_a_forecast",
                "status": "permanently_failed",
                "stage": "forecast",
                "error_code": "COLD_START_QUARANTINED",
                "updated_at": "2026-05-21T06:50:00Z",
            }
        ],
        "pipeline_events": [
            {
                "event_id": 42,
                "event_type": "status_change",
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026052106_forecast_model_a_state_save_qc",
                "status_from": "pending",
                "status_to": "succeeded",
                "created_at": "2026-05-21T06:45:00Z",
                "details": {
                    "stage": "state_save_qc",
                    "task_results": [
                        {
                            **identity,
                            "candidate_id": candidate.candidate_id,
                            "status": "succeeded",
                            "state": "succeeded",
                            "error_code": None,
                            "task_id": 0,
                        }
                    ],
                },
            }
        ],
    }

    decision = scheduler_module._candidate_state_decision(candidate, state)

    assert decision is not None
    assert decision.action == "skip"
    assert decision.reason == "terminal_pipeline_success"
    assert decision.evidence["terminal_source"] == "pipeline_job"
    assert decision.evidence["terminal_status"] == "succeeded"


def test_strict_warm_start_match_uses_pipeline_candidate_state_before_stale_hydro_run() -> None:
    selected = {"init_state_id": "state_gfs_model_a_2026052106_gfs_2026052100_f006"}
    terminal_evidence = {
        "terminal_source": "pipeline_job",
        "terminal_status": "succeeded",
        "candidate_state": dict(selected),
        "hydro_run": {
            "status": "failed",
            "init_state_id": None,
            "error_code": "COLD_START_QUARANTINED",
        },
    }
    strict_evidence = {"candidate_state": dict(selected), "ready": True}

    assert scheduler_candidates_module._terminal_decision_matches_strict_warm_start(
        terminal_evidence,
        strict_evidence,
    )


def test_strict_warm_start_match_ignores_stale_cold_start_hydro_failure_after_pipeline_success() -> None:
    selected = {"init_state_id": "state_gfs_model_a_2026052106_gfs_2026052100_f006"}
    terminal_evidence = {
        "terminal_source": "pipeline_job",
        "terminal_status": "succeeded",
        "hydro_run": {
            "status": "failed",
            "init_state_id": None,
            "error_code": "COLD_START_QUARANTINED",
        },
    }
    strict_evidence = {"candidate_state": dict(selected), "ready": True}

    assert scheduler_candidates_module._terminal_decision_matches_strict_warm_start(
        terminal_evidence,
        strict_evidence,
    )


@pytest.mark.parametrize(
    "state",
    [
        {
            "hydro_status": "succeeded",
            "output_uri": "s3://nhms/runs/stale_sibling/output/",
        },
        {
            "pipeline_status": "running",
            "slurm_job_id": "7777",
        },
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "NODE_FAILURE",
            "retry_count": 1,
        },
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "forecast",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
        },
        {
            "pipeline_status": "cancelled",
            "hydro_status": "cancelled",
            "retry_count": 1,
        },
    ],
)
def test_top_level_legacy_candidate_state_without_identity_proof_does_not_drive_decisions(
    tmp_path: Path,
    state: dict[str, Any],
) -> None:
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    candidate = _scheduler_candidate_fixture()
    decision = scheduler_module._candidate_state_decision(candidate, state)
    validation = scheduler_module._candidate_state_identity_validation(candidate, state)
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert "candidate_state" in validation["legacy_non_authoritative"]
    assert decision is None
    assert orchestrator.calls


def test_top_level_legacy_candidate_state_with_old_same_candidate_proof_can_skip_terminal(
    tmp_path: Path,
) -> None:
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(
            {
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "source": "gfs",
                "cycle_time": "2026-05-21T06:00:00Z",
                "hydro_status": "succeeded",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
            }
        ),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    validation = skipped["state_evidence"]["production_identity_validation"]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert skipped["reason"] == "terminal_hydro_success"
    assert "candidate_state" not in validation["legacy_non_authoritative"]


def test_top_level_download_gfs_source_cycle_blocker_without_row_does_not_submit(
    tmp_path: Path,
) -> None:
    state = {
        "shared_cycle_aggregate": True,
        "source": "gfs",
        "cycle_time": "2026-05-21T06:00:00Z",
        "pipeline_status": "permanently_failed",
        "failed_stage": "download_gfs",
        "job_type": "download",
        "error_code": "SLURM_JOB_FAILED",
        "retry_count": 3,
        "retry_limit": 3,
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(state),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    decision = scheduler_module._candidate_state_decision(_scheduler_candidate_fixture(), state)

    assert decision is not None
    assert decision.action == "blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "permanent_failure_guard"
    assert blocked["state_evidence"]["stage"] == "download_gfs"


def test_scheduler_candidate_state_correlation_mismatch_still_blocks_when_expected_and_actual_present(
    tmp_path: Path,
) -> None:
    orchestrator = StrictNoSubmitOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry(
            [
                _model(
                    "model_a",
                    "basin_a",
                    resource_profile={"runnable": True, "pipeline_job_id": "expected_pipeline_job"},
                )
            ]
        ),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(
            {
                "pipeline_jobs": [
                    {
                        **_production_identity_fixture(),
                        "pipeline_job_id": "actual_pipeline_job",
                        "status": "running",
                        "stage": "forecast",
                    }
                ],
            }
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    mismatch = blocked["state_evidence"]["production_identity_validation"]["mismatches"][0]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert blocked["reason"] == "production_identity_mismatch"
    assert mismatch["field"] == "pipeline_job_id"
    assert mismatch["expected"] == "expected_pipeline_job"
    assert mismatch["actual"] == "actual_pipeline_job"


def test_production_identity_correlation_fields_compare_only_when_both_present() -> None:
    expected = _production_identity_fixture()
    validate_same_production_identity(expected, {**expected, "pipeline_job_id": "stage_job_1"})

    with pytest.raises(ProductionContractError) as exc_info:
        validate_same_production_identity(
            {**expected, "pipeline_event_id": "event_1"},
            {**expected, "pipeline_event_id": "event_2"},
        )

    assert exc_info.value.code == "PRODUCTION_IDENTITY_MISMATCH"
    assert exc_info.value.field == "pipeline_event_id"


def test_display_artifact_boundary_requires_same_identity_and_published_uri(tmp_path: Path) -> None:
    identity = _production_identity_fixture()
    published_root = tmp_path / "published"
    published_artifact = published_root / "manifests" / "GFS" / "2026052106" / identity["run_id"] / "manifest.json"
    published_artifact.parent.mkdir(parents=True)
    published_artifact.write_text("{}", encoding="utf-8")
    published_uri = f"published://manifests/GFS/2026052106/{identity['run_id']}/manifest.json"
    file_uri = published_artifact.as_uri()

    published = validate_display_artifact_evidence(
        {**identity, "uri": published_uri},
        identity,
        published_root=published_root,
    )
    file_result = validate_display_artifact_evidence(
        {**identity, "uri": file_uri},
        identity,
        published_root=published_root,
    )

    assert published["display_readable"] is True
    assert published["uri_boundary"]["kind"] == "published"
    assert published["uri_boundary"]["normalized_uri"] == published_uri
    assert file_result["display_readable"] is True
    assert file_result["uri_boundary"]["kind"] == "published_root_file"


def test_display_artifact_evidence_wrong_identity_wrapper_raises_identity_mismatch(tmp_path: Path) -> None:
    identity = _production_identity_fixture()
    evidence = {
        **identity,
        "basin_id": "basin_other",
        "uri": f"published://manifests/GFS/2026052106/{identity['run_id']}/manifest.json",
    }

    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_artifact_evidence(evidence, identity, published_root=tmp_path / "published")

    assert exc_info.value.code == "PRODUCTION_IDENTITY_MISMATCH"
    assert exc_info.value.field == "basin_id"


@pytest.mark.parametrize(
    "uri_template",
    [
        "published://manifests/GFS/2026052106/{sibling}/manifest.json",
        "{file_uri}",
        "s3://nhms/manifests/GFS/2026052106/{sibling}/manifest.json",
    ],
)
def test_display_artifact_boundary_rejects_run_id_substring_path_segments(
    tmp_path: Path,
    uri_template: str,
) -> None:
    identity = _production_identity_fixture()
    sibling = f"{identity['run_id']}_retry"
    published_root = tmp_path / "published"
    sibling_file = published_root / "manifests" / "GFS" / "2026052106" / sibling / "manifest.json"
    sibling_file.parent.mkdir(parents=True)
    sibling_file.write_text("{}", encoding="utf-8")
    uri = uri_template.format(sibling=sibling, file_uri=sibling_file.as_uri())

    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_artifact_evidence(
            {**identity, "uri": uri},
            identity,
            published_root=published_root,
            allowed_s3_bucket="nhms",
        )

    assert exc_info.value.code == "DISPLAY_URI_IDENTITY_MISMATCH"


def test_display_artifact_boundary_redacts_credential_bearing_uris(tmp_path: Path) -> None:
    identity = _production_identity_fixture()

    with pytest.raises(ProductionContractError) as userinfo_error:
        validate_display_artifact_evidence(
            {
                **identity,
                "uri": f"published://user:pass@logs/GFS/2026052106/{identity['run_id']}/job.out",
            },
            identity,
            published_root=tmp_path / "published",
        )

    with pytest.raises(ProductionContractError) as relative_error:
        validate_display_artifact_evidence(
            {**identity, "uri": "token_secret/logs/job.out"},
            identity,
            published_root=tmp_path / "published",
        )

    assert "user:pass" not in str(userinfo_error.value.to_dict())
    assert "token_secret" not in str(relative_error.value.to_dict())


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com:abc/log",
        "https://[::1/log",
        "https://user:pass@example.com:abc/token_secret/log",
    ],
)
def test_display_readable_uri_malformed_inputs_raise_typed_redacted_contract_error(uri: str) -> None:
    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_readable_uri(uri)

    payload = exc_info.value.to_dict()
    payload_text = str(payload)
    assert payload["code"] == "DISPLAY_URI_MALFORMED"
    assert "user:pass" not in payload_text
    assert "token_secret" not in payload_text
    assert "/log" not in payload_text


@pytest.mark.parametrize(
    "uri",
    [
        "/workspace/runs/fcst_gfs_2026052106_model_a/logs/slurm.out",
        "/scratch/frd_muziyao/NWM/.nhms-workspace/runs/fcst_gfs_2026052106_model_a/logs/slurm.out",
        "/var/spool/slurm/job-123.out",
        "published://logs/GFS/2026052106/fcst_gfs_2026052106_model_a/../job.out",
        "/opt/nhms/logs/fcst_gfs_2026052106_model_a/job.out",
    ],
)
def test_display_artifact_boundary_rejects_private_or_unallowlisted_paths(tmp_path: Path, uri: str) -> None:
    identity = _production_identity_fixture()

    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_artifact_evidence(
            {**identity, "uri": uri},
            identity,
            published_root=tmp_path / "published",
        )

    assert exc_info.value.code in {
        "DISPLAY_URI_PRIVATE_COMPUTE_PATH",
        "DISPLAY_URI_TRAVERSAL",
        "DISPLAY_URI_NOT_ALLOWLISTED",
    }


@pytest.mark.parametrize(
    ("configured_root", "uri"),
    [
        (
            Path("/scratch/nhms-published"),
            "file:///scratch/nhms-published/manifests/GFS/2026052106/{run_id}/manifest.json",
        ),
        (
            Path("/workspace/nhms-published"),
            "file:///workspace/nhms-published/manifests/GFS/2026052106/{run_id}/manifest.json",
        ),
        (
            Path("/var/spool/slurm/nhms-published"),
            "file:///var/spool/slurm/nhms-published/manifests/GFS/2026052106/{run_id}/manifest.json",
        ),
    ],
)
def test_display_artifact_boundary_rejects_private_configured_published_roots(
    configured_root: Path,
    uri: str,
) -> None:
    identity = _production_identity_fixture()

    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_artifact_evidence(
            {**identity, "uri": uri.format(run_id=identity["run_id"])},
            identity,
            published_root=configured_root,
        )

    assert exc_info.value.code == "DISPLAY_URI_PRIVATE_COMPUTE_PATH"
    assert exc_info.value.details["reason"] in {
        "scratch_private_path",
        "workspace_private_path",
        "slurm_private_path",
    }


def test_display_artifact_boundary_rejects_private_allowed_published_root() -> None:
    identity = _production_identity_fixture()

    with pytest.raises(ProductionContractError) as exc_info:
        validate_display_artifact_evidence(
            {
                **identity,
                "uri": f"file:///scratch/nhms-published/manifests/GFS/2026052106/{identity['run_id']}/manifest.json",
            },
            identity,
            published_root=Path("/var/lib/nhms/published"),
            allowed_published_roots=(Path("/scratch/nhms-published"),),
        )

    assert exc_info.value.code == "DISPLAY_URI_PRIVATE_COMPUTE_PATH"
    assert exc_info.value.details["reason"] == "scratch_private_path"


def test_production_stage_and_status_taxonomy_maps_known_legacy_values() -> None:
    assert set(PRODUCTION_STAGE_TAXONOMY) == {
        "download",
        "convert",
        "forcing",
        "forecast",
        "parse",
        "q_down_publish",
        "production_run",
    }
    assert set(PRODUCTION_STATUS_TAXONOMY) == {
        "pending",
        "ready",
        "running",
        "succeeded",
        "blocked",
        "unavailable",
        "partial",
        "failed",
        "cancelled",
        "superseded",
    }
    assert production_stage_for("download_gfs") == "download"
    assert production_stage_for("publish_tiles") == "q_down_publish"
    assert production_stage_for("unknown_stage") == "production_run"
    assert production_status_for("skipped") == "superseded"
    assert production_status_for("complete") == "succeeded"
    assert production_status_for("source_cycle_unavailable") == "unavailable"
    assert production_status_for("lock_contended") == "blocked"
    assert production_status_for("preflight_blocked") == "blocked"
    assert production_status_for("partially_failed") == "partial"
    assert production_status_for("unexpected_status") == "failed"
    assert "q_down_publish" in PRODUCTION_STAGE_TAXONOMY
    assert "superseded" in PRODUCTION_STATUS_TAXONOMY


def test_model_and_basin_filters_select_subset_and_record_excluded_runnable_count(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        model_ids=("model_a",),
        basin_ids=("basin_a",),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert [candidate["model_id"] for candidate in _candidates(result.evidence)] == ["model_a"]
    assert result.evidence["model_discovery"]["operator_filters"] == {
        "expression": "model_id in [model_a] and basin_id in [basin_a]",
        "excluded_runnable_count": 1,
    }
    assert result.evidence["operator_filters"] == {
        "model_ids": ["model_a"],
        "basin_ids": ["basin_a"],
        "expression": "model_id in [model_a] and basin_id in [basin_a]",
        "excluded_runnable_count": 1,
    }


def test_lock_contention_reports_without_candidates_or_submission(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lock_path.write_text(
        json.dumps(
            {
                "owner": LOCK_OWNER,
                "schema_version": LOCK_SCHEMA_VERSION,
                "lease_token": "existing-token",
                "pass_id": "existing",
            }
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), lock_path=lock_path)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["contention"] is True
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0


def test_oversized_existing_lock_is_rejected_without_full_read(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    with lock_path.open("wb") as handle:
        handle.truncate(MAX_LOCK_PAYLOAD_BYTES + 1)
    before_stat = lock_path.stat()
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), lock_path=lock_path)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    after_stat = lock_path.stat()
    assert result.status == "lock_contended"
    assert result.evidence["lock"]["contention"] is True
    assert result.evidence["lock"]["reason"] == "unsafe_lock_too_large"
    assert result.evidence["lock"]["existing_lock"] == {
        "raw": None,
        "size_bytes": MAX_LOCK_PAYLOAD_BYTES + 1,
        "max_bytes": MAX_LOCK_PAYLOAD_BYTES,
    }
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_dry_run_is_non_mutating_and_does_not_call_execution_clients(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    adapter = FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
    )

    result = scheduler.run_once()

    assert adapter.download_calls == 0
    assert result.evidence["execution_mode"] == "dry_run"
    assert result.evidence["source_cycles"][0]["db_cycle_status_written"] is None
    assert result.evidence["source_cycles"][0]["cycle_status_candidate"] == "discovered"
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_unavailable_ifs_cycle_is_evidence_only_not_db_enum_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), sources=("IFS",))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"IFS": FakeAdapter("IFS", [("2026-05-21T06:00:00Z", False)])},
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["blocked_candidates"][0]["reason"] == "source_cycle_unavailable"
    assert result.evidence["source_cycles"][0]["status"] == "unavailable"
    assert result.evidence["source_cycles"][0]["db_cycle_status_written"] is None


def test_duplicate_sources_and_cycles_emit_one_candidate_with_exclusion_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), sources=("gfs", "gfs"))
    duplicate_cycle = ("2026-05-21T06:00:00Z", True)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [duplicate_cycle, duplicate_cycle])},
    )

    result = scheduler.run_once()

    assert len(result.evidence["candidates"]) == 1
    reasons = {item["reason"] for item in result.evidence["duplicate_exclusions"]}
    assert reasons == {"duplicate_source", "duplicate_source_cycle"}
    assert result.evidence["sources"] == ["gfs"]


def test_explicit_paths_must_stay_under_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-scheduler.lock"

    with pytest.raises(ValueError, match="lock_path must be under workspace_root"):
        _config(tmp_path, lock_path=outside)
    with pytest.raises(ValueError, match="evidence_dir must be under workspace_root"):
        _config(tmp_path, evidence_dir=outside)


def test_fresh_default_workspace_runtime_paths_are_created_safely(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(config, registry=FakeRegistry([]), adapters={})

    result = scheduler.run_once()

    workspace_root = tmp_path / ".nhms-workspace"
    assert result.status == "planned"
    assert config.workspace_root == workspace_root.resolve()
    assert Path(config.lock_path) == workspace_root.resolve() / "scheduler" / "production-scheduler.lock"
    assert Path(config.evidence_dir) == workspace_root.resolve() / "scheduler" / "evidence"
    assert Path(result.artifact_path or "").is_file()
    assert (workspace_root / "scheduler" / "production-scheduler.lock.guard").is_file()


def test_plan_production_cli_uses_workspace_root_env_without_explicit_flag(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "configured-workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_INTERVAL_SECONDS", "17.5")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production"])

    assert rc == 0
    assert captured["config"].workspace_root == workspace_root.resolve()
    assert Path(captured["config"].lock_path) == workspace_root.resolve() / "scheduler" / "production-scheduler.lock"
    assert Path(captured["config"].evidence_dir) == workspace_root.resolve() / "scheduler" / "evidence"
    assert captured["config"].interval_seconds == 17.5
    assert captured["config"].require_runtime_roots is True


def test_plan_production_explicit_workspace_ignores_ambient_scheduler_lock_and_evidence_roots(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    explicit_workspace = tmp_path / "diagnostic-workspace"
    ambient_workspace = tmp_path / "ambient-production-workspace"
    explicit_workspace.mkdir()
    (ambient_workspace / "locks").mkdir(parents=True)
    (ambient_workspace / "evidence").mkdir()
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(ambient_workspace / "locks"))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(ambient_workspace / "evidence"))
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--workspace-root", str(explicit_workspace)])

    assert rc == 0
    assert captured["config"].require_runtime_roots is False
    assert Path(captured["config"].lock_path) == (
        explicit_workspace.resolve() / "scheduler" / "production-scheduler.lock"
    )
    assert Path(captured["config"].evidence_dir) == explicit_workspace.resolve() / "scheduler" / "evidence"


def test_plan_production_blank_workspace_root_shared_helper_rejected_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank workspace flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    with pytest.raises(ValueError, match="--workspace-root must not be blank"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root="",
            lock_path=None,
            evidence_dir=None,
        )


def test_plan_production_click_blank_workspace_root_exits_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank workspace flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    try:
        cli._click_main(["plan-production", "--workspace-root", ""])
    except SystemExit as error:
        rc = int(error.code or 0)
    else:
        rc = 0
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err == "plan-production --workspace-root must not be blank\n"


def test_plan_production_argparse_blank_workspace_root_exits_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank workspace flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    rc = cli._argparse_main(["plan-production", "--workspace-root", ""])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err == "plan-production --workspace-root must not be blank\n"


@pytest.mark.parametrize(
    ("field_name", "option_name"),
    [
        ("lock_path", "--lock-path"),
        ("evidence_dir", "--evidence-dir"),
    ],
)
def test_plan_production_blank_lock_and_evidence_shared_helper_rejected_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
    field_name: str,
    option_name: str,
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank scheduler path flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)
    kwargs = {
        "sources": ("gfs",),
        "lookback_hours": 24,
        "cycle_lag_hours": 0,
        "max_cycles_per_source": 1,
        "model_ids": (),
        "basin_ids": (),
        "dry_run": True,
        "continuous": False,
        "interval_seconds": 300.0,
        "max_passes": None,
        "workspace_root": None,
        "lock_path": None,
        "evidence_dir": None,
    }
    kwargs[field_name] = ""

    with pytest.raises(ValueError, match=f"{option_name} must not be blank"):
        cli._plan_production(**kwargs)


@pytest.mark.parametrize(
    ("args", "option_name"),
    [
        (["--lock-path", ""], "--lock-path"),
        (["--evidence-dir", ""], "--evidence-dir"),
    ],
)
def test_plan_production_click_blank_lock_and_evidence_exits_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    option_name: str,
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank scheduler path flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    try:
        cli._click_main(["plan-production", *args])
    except SystemExit as error:
        rc = int(error.code or 0)
    else:
        rc = 0
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err == f"plan-production {option_name} must not be blank\n"


@pytest.mark.parametrize(
    ("args", "option_name"),
    [
        (["--lock-path", ""], "--lock-path"),
        (["--evidence-dir", ""], "--evidence-dir"),
    ],
)
def test_plan_production_argparse_blank_lock_and_evidence_exits_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    option_name: str,
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("blank scheduler path flag must not construct scheduler")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    rc = cli._argparse_main(["plan-production", *args])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err == f"plan-production {option_name} must not be blank\n"


@pytest.mark.parametrize("field_name", ["workspace_root", "lock_path", "evidence_dir"])
def test_production_scheduler_config_rejects_blank_scheduler_paths(tmp_path: Path, field_name: str) -> None:
    kwargs: dict[str, Any] = {"workspace_root": tmp_path}
    kwargs[field_name] = ""

    with pytest.raises(ValueError, match=f"{field_name} must not be blank"):
        ProductionSchedulerConfig(**kwargs)


@pytest.mark.parametrize("env_value", ["0,12", "12, 0,12"])
def test_scheduler_allowed_cycle_hours_env_parses_dedupes_and_emits_runtime_evidence(
    tmp_path: Path,
    monkeypatch: Any,
    env_value: str,
) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", env_value)

    config = ProductionSchedulerConfig(workspace_root=tmp_path)
    evidence = scheduler_module._scheduler_runtime_config_evidence(config)

    assert config.allowed_cycle_hours_utc == (0, 12)
    assert evidence["allowed_cycle_hours_utc"] == [0, 12]


@pytest.mark.parametrize(
    ("env_value", "match"),
    [
        ("", "must contain at least one UTC cycle hour"),
        ("0,,12", "must not contain empty cycle hour tokens"),
        ("0,nope", "must contain integer UTC cycle hours"),
        ("0,24", "must only contain values in 0..23"),
        ("-1,12", "must only contain values in 0..23"),
    ],
)
def test_scheduler_allowed_cycle_hours_env_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
    env_value: str,
    match: str,
) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", env_value)

    with pytest.raises(ValueError, match=match):
        ProductionSchedulerConfig(workspace_root=tmp_path)


@pytest.mark.parametrize("allowed_cycle_hours_utc", [(12.9,), (True,), ("12",)])
def test_scheduler_allowed_cycle_hours_direct_config_requires_int_not_bool(
    tmp_path: Path,
    allowed_cycle_hours_utc: tuple[Any, ...],
) -> None:
    with pytest.raises(ValueError, match="allowed_cycle_hours_utc must contain integer UTC cycle hours"):
        ProductionSchedulerConfig(
            workspace_root=tmp_path,
            allowed_cycle_hours_utc=allowed_cycle_hours_utc,
        )


def test_scheduler_allowed_cycle_hours_default_is_00_12(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", raising=False)

    config = ProductionSchedulerConfig(workspace_root=tmp_path)

    assert config.allowed_cycle_hours_utc == (0, 12)


def test_default_evidence_dir_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-evidence"
    outside.mkdir()
    evidence_link = tmp_path / "scheduler" / "evidence"
    evidence_link.parent.mkdir()
    evidence_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="evidence_dir must be under workspace_root"):
        _config(tmp_path)

    assert list(outside.iterdir()) == []


def test_explicit_evidence_dir_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-explicit-outside-evidence"
    outside.mkdir()
    evidence_link = tmp_path / "evidence-link"
    evidence_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="evidence_dir must be under workspace_root"):
        _config(tmp_path, evidence_dir=evidence_link)

    assert list(outside.iterdir()) == []


def test_evidence_final_artifact_symlink_is_not_followed(tmp_path: Path) -> None:
    pass_id = "scheduler_20260521120000_fixed"
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(config, registry=FakeRegistry([]), adapters={})
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    outside_target = tmp_path.parent / f"{tmp_path.name}-outside-evidence-target.json"
    outside_target.write_text("keep", encoding="utf-8")
    artifact_path = evidence_dir / f"{pass_id}.json"
    artifact_path.symlink_to(outside_target)
    evidence = {"pass_id": pass_id, "status": "planned"}

    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler._write_evidence(pass_id, evidence)

    assert error.value.reason == "unsafe_evidence_artifact"
    assert artifact_path.is_symlink()
    assert outside_target.read_text(encoding="utf-8") == "keep"
    assert evidence == {"pass_id": pass_id, "status": "planned"}


def test_evidence_existing_artifact_file_is_not_overwritten(tmp_path: Path) -> None:
    pass_id = "scheduler_20260521120000_fixed"
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(config, registry=FakeRegistry([]), adapters={})
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    artifact_path = evidence_dir / f"{pass_id}.json"
    artifact_path.write_text("existing", encoding="utf-8")
    evidence = {"pass_id": pass_id, "status": "planned"}

    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler._write_evidence(pass_id, evidence)

    assert error.value.reason == "evidence_artifact_exists"
    assert artifact_path.read_text(encoding="utf-8") == "existing"
    assert evidence == {"pass_id": pass_id, "status": "planned"}


@pytest.mark.parametrize(
    "case",
    [
        "parent",
        "nested",
        "absolute",
    ],
)
def test_write_evidence_rejects_unsafe_artifact_names_before_escape(
    tmp_path: Path,
    case: str,
) -> None:
    from services.orchestrator import scheduler_evidence

    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    context = _scheduler_evidence_test_context(config)
    if case == "parent":
        pass_id = "../escaped_write"
        escaped_path = evidence_dir.parent / "escaped_write.json"
    elif case == "nested":
        pass_id = "nested/escaped_write"
        escaped_path = evidence_dir / "nested" / "escaped_write.json"
    else:
        escaped_path = tmp_path / "absolute_escaped_write.json"
        pass_id = str(escaped_path.with_suffix(""))
    evidence = {"pass_id": pass_id, "status": "planned"}
    original = dict(evidence)

    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler_evidence.write_evidence(context, pass_id, evidence)

    assert error.value.reason == "unsafe_evidence_artifact"
    assert not escaped_path.exists()
    assert list(evidence_dir.iterdir()) == []
    assert evidence == original


def test_scheduler_write_evidence_shim_rejects_traversal_artifact_name(tmp_path: Path) -> None:
    pass_id = "../escaped_scheduler_shim"
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(config, registry=FakeRegistry([]), adapters={})
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    escaped_path = tmp_path / "escaped_scheduler_shim.json"
    evidence = {"pass_id": pass_id, "status": "planned"}

    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler._write_evidence(pass_id, evidence)

    assert error.value.reason == "unsafe_evidence_artifact"
    assert not escaped_path.exists()
    assert list(evidence_dir.iterdir()) == []
    assert evidence == {"pass_id": pass_id, "status": "planned"}


@pytest.mark.parametrize(
    "case",
    [
        "parent",
        "nested",
        "absolute",
    ],
)
def test_reserve_pre_execution_evidence_rejects_unsafe_artifact_names_before_escape(
    tmp_path: Path,
    case: str,
) -> None:
    from services.orchestrator import scheduler_evidence

    started_at = _dt("2026-05-21T12:00:00Z")
    config = _config(tmp_path, now=started_at, dry_run=False)
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    context = _scheduler_evidence_test_context(config)
    if case == "parent":
        pass_id = "../escaped_pre_execution"
        escaped_path = evidence_dir.parent / "escaped_pre_execution.pre_execution.json"
    elif case == "nested":
        pass_id = "nested/escaped_pre_execution"
        escaped_path = evidence_dir / "nested" / "escaped_pre_execution.pre_execution.json"
    else:
        escaped_path = tmp_path / "absolute_escaped_pre_execution.pre_execution.json"
        pass_id = str(escaped_path.with_suffix("").with_suffix(""))

    blocked = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        started_at,
        1,
        now=started_at,
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "unsafe_evidence_artifact"
    assert blocked["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
    assert not escaped_path.exists()
    assert list(evidence_dir.iterdir()) == []


def test_scheduler_evidence_private_helper_compatibility_shims_delegate(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from services.orchestrator import scheduler_evidence

    calls: list[dict[str, Any]] = []
    original_bounded = scheduler_evidence.bounded_evidence_payload

    def recording_bounded(
        payload: Mapping[str, Any],
        *,
        reason: str,
        max_evidence_bytes: int = scheduler_evidence.MAX_EVIDENCE_BYTES,
    ) -> dict[str, Any]:
        calls.append({"payload": dict(payload), "reason": reason, "max_evidence_bytes": max_evidence_bytes})
        return original_bounded(payload, reason=reason, max_evidence_bytes=max_evidence_bytes)

    monkeypatch.setattr(scheduler_evidence, "bounded_evidence_payload", recording_bounded)
    payload = {
        "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        "pass_id": "scheduler_20260521120000_fixed",
        "started_at": "2026-05-21T12:00:00Z",
        "counts": {"candidate_count": 2},
        "root_preflight": {"status": "ready"},
        "runtime_config": {"dry_run": False},
        "evidence_pre_execution": {"status": "reserved"},
        "execution_write_proof": {"status": "submitted"},
        "slurm_status_sync_proof": {"status": "not_required"},
        "slurm_cancellation_proof": {"status": "not_required"},
        "no_mutation_proof": _expected_no_mutation_proof(),
        "candidates": [{"secret_token": "rawsecret"}],
    }

    shim_payload = scheduler_module._bounded_evidence_payload(payload, reason="compatibility_check")

    assert calls == [
        {
            "payload": payload,
            "reason": "compatibility_check",
            "max_evidence_bytes": scheduler_module.MAX_EVIDENCE_BYTES,
        }
    ]
    assert shim_payload == original_bounded(
        payload,
        reason="compatibility_check",
        max_evidence_bytes=scheduler_module.MAX_EVIDENCE_BYTES,
    )
    assert shim_payload["status"] == "resource_limit_blocked"
    assert shim_payload["candidates"] == []
    assert shim_payload["evidence_pre_execution"] == {"status": "reserved"}

    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(config, registry=FakeRegistry([]), adapters={})
    pass_id = "scheduler_20260521120000_compat"
    base_from_method = scheduler._base_evidence(pass_id, config.now or _dt("2026-05-21T12:00:00Z"))
    base_from_module = scheduler_evidence.base_evidence(
        config,
        pass_id,
        config.now or _dt("2026-05-21T12:00:00Z"),
        resolved_runtime_roots=scheduler_module._scheduler_resolved_runtime_roots,
        runtime_config_evidence=scheduler_module._scheduler_runtime_config_evidence,
    )
    assert base_from_method == base_from_module


def test_bounded_evidence_payload_shim_summarizes_large_retained_fields_within_limit() -> None:
    payload = _large_scheduler_evidence_payload("scheduler_20260521120000_bounded_shim")

    shim_payload = scheduler_module._bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=2_000,
    )
    serialized = json.dumps(shim_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    assert len(serialized) <= 2_000
    assert shim_payload["status"] == "resource_limit_blocked"
    assert shim_payload["limit"] == {
        "reason": "evidence_size_limit_exceeded",
        "max_evidence_bytes": 2_000,
    }
    assert shim_payload["pass_id"] == "scheduler_20260521120000_bounded_shim"
    assert "artifact_path" in shim_payload
    assert shim_payload["counts"]["candidate_count"] == 1
    assert shim_payload["readiness"]["schema_version"] == "nhms.production_readiness.scheduler_input.v1"
    assert shim_payload["duplicate_exclusions"]["status"] == "omitted"
    assert shim_payload["duplicate_exclusions"]["reason"] == "evidence_size_limit_exceeded"
    assert shim_payload["runtime_config"]["dry_run"] is False
    assert shim_payload["runtime_config"]["allowed_cycle_hours_utc"] == [0, 6, 12, 18]
    assert shim_payload["root_preflight"]["status"] == "ready"
    assert shim_payload["execution_write_proof"]["status"] == "submitted"
    assert shim_payload["slurm_status_sync_proof"]["status"] == "not_required"
    assert shim_payload["slurm_cancellation_proof"]["status"] == "not_required"
    assert shim_payload["execution_boundary"] == "planning_only"
    assert "slurm_submit_called" in shim_payload["no_mutation_proof"]


def test_db_free_bounded_evidence_preserves_runtime_selector_proof(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig(now=_dt("2026-06-27T16:00:00Z"))
    payload = _large_scheduler_evidence_payload("scheduler_2026062716_dbfree_bounded")
    payload["runtime_config"] = scheduler_module._scheduler_runtime_config_evidence(config)
    payload["db_free_runtime"] = config.db_free_runtime_preflight()
    payload["resolved_runtime_roots"] = scheduler_module._scheduler_resolved_runtime_roots(config)

    bounded = scheduler_module._bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=5_000,
    )
    rendered = json.dumps(bounded, separators=(",", ":"), sort_keys=True)

    assert len(rendered.encode("utf-8")) <= 5_000
    assert bounded["status"] == "resource_limit_blocked"
    runtime_config = bounded["runtime_config"]
    assert runtime_config["database_url_configured"] is False
    assert runtime_config["scheduler_db_free_required"] is True
    assert runtime_config["scheduler_state_backend"] == "file"
    assert runtime_config["scheduler_lock_backend"] == "file"
    assert runtime_config["scheduler_registry_backend"] == "file"
    assert runtime_config["scheduler_canonical_readiness_backend"] == "file"
    assert runtime_config["scheduler_journal_backend"] == "file"
    assert runtime_config["scheduler_state_index_backend"] == "file"
    db_free_runtime = runtime_config["db_free_runtime"]
    assert set(db_free_runtime["selectors"]) == set(_DB_FREE_SELECTOR_ENV_KEYS)
    assert set(db_free_runtime["paths"]) == set(_DB_FREE_PATH_ENV_KEYS)
    assert db_free_runtime["selectors"]["NHMS_SCHEDULER_REGISTRY_BACKEND"]["selected"] == "file"
    assert db_free_runtime["paths"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]["path"] == "[local-path]"
    assert bounded["db_free_runtime"]["status"] == "ready"
    assert bounded["db_free_runtime"]["checks"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]["path"] == "[local-path]"
    for path in paths.values():
        assert str(path) not in rendered
    assert "db-free-local-root" not in rendered


def test_retention_bounded_evidence_preserves_forced_dry_run_summary_without_paths() -> None:
    payload = _large_scheduler_evidence_payload("scheduler_20260521120000_retention_bounded")
    payload["retention"] = {
        "schema_version": "nhms.production_scheduler.retention.v1",
        "status": "completed",
        "enabled": True,
        "dry_run": True,
        "forced_dry_run_by_scheduler": True,
        "forced_dry_run_reason": "evidence_preflight_blocked",
        "retention_days": 14,
        "counts": {"planned": 2, "deleted": 0, "skipped": 1, "failed": 0},
        "planned": [
            {"key": "raw/gfs/2026050100/secret-token-file.nc", "path": "/private/secret-token-file.nc"},
            {"key": "runs/fcst_gfs_2026050100_model_a", "path": "/private/run-path"},
        ],
        "deleted": [],
        "skipped": [{"key": "tiles/protected", "path": "/private/tiles"}],
        "failed": [],
        "freed_bytes": 0,
    }

    bounded = scheduler_module._bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=2_800,
    )
    rendered = json.dumps(bounded, separators=(",", ":"), sort_keys=True)

    assert len(rendered.encode("utf-8")) <= 2_800
    retention = bounded["retention"]
    assert retention["status"] == "completed"
    assert retention["enabled"] is True
    assert retention["dry_run"] is True
    assert retention["forced_dry_run_by_scheduler"] is True
    assert retention["forced_dry_run_reason"] == "evidence_preflight_blocked"
    assert retention["counts"] == {"planned": 2, "deleted": 0, "skipped": 1, "failed": 0}
    assert retention["planned_count"] == 2
    assert retention["deleted_count"] == 0
    assert "planned" not in retention
    assert "deleted" not in retention
    assert "secret-token-file" not in rendered
    assert "/private" not in rendered


def test_retention_bounded_evidence_compacts_paths_before_initial_fit() -> None:
    payload = _large_scheduler_evidence_payload("scheduler_20260521120000_retention_initial_fit")
    payload["retention"] = {
        "schema_version": "nhms.production_scheduler.retention.v1",
        "status": "completed",
        "enabled": True,
        "dry_run": True,
        "forced_dry_run_by_scheduler": True,
        "forced_dry_run_reason": "evidence_preflight_blocked",
        "retention_days": 14,
        "counts": {"planned": 1, "deleted": 0, "skipped": 0, "failed": 0},
        "planned": [{"key": "raw/gfs/secret-token-cycle", "path": "/private/secret-token-cycle"}],
        "deleted": [],
        "skipped": [],
        "failed": [],
        "freed_bytes": 0,
    }

    bounded = scheduler_module._bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=scheduler_module.MAX_EVIDENCE_BYTES,
    )
    rendered = json.dumps(bounded, separators=(",", ":"), sort_keys=True)

    retention = bounded["retention"]
    assert retention["forced_dry_run_reason"] == "evidence_preflight_blocked"
    assert retention["planned_count"] == 1
    assert retention["deleted_count"] == 0
    assert "planned" not in retention
    assert "deleted" not in retention
    assert "secret-token-cycle" not in rendered
    assert "/private" not in rendered


def test_write_evidence_bounds_serialized_payload_before_artifact_creation(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_evidence

    pass_id = "scheduler_20260521120000_bounded_write"
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    context = _scheduler_evidence_test_context(config, max_evidence_bytes=2_200)
    evidence = _large_scheduler_evidence_payload(pass_id)

    artifact_path = scheduler_evidence.write_evidence(context, pass_id, evidence)
    serialized = Path(artifact_path or "").read_bytes()
    persisted = json.loads(serialized.decode("utf-8"))

    assert len(serialized) <= 2_200
    assert persisted["status"] == "resource_limit_blocked"
    assert persisted["limit"]["max_evidence_bytes"] == 2_200
    assert persisted["artifact_path"] == str(evidence_dir / f"{pass_id}.json")
    required_core_keys = {
        "schema_version",
        "pass_id",
        "status",
        "artifact_path",
        "limit",
        "counts",
        "readiness",
        "resolved_runtime_roots",
        "runtime_config",
        "root_preflight",
        "evidence_pre_execution",
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "no_mutation_proof",
    }
    assert required_core_keys <= set(persisted)
    for field_name in (
        "readiness",
        "resolved_runtime_roots",
        "runtime_config",
        "root_preflight",
        "evidence_pre_execution",
        "execution_write_proof",
        "slurm_status_sync_proof",
        "slurm_cancellation_proof",
        "no_mutation_proof",
    ):
        assert persisted[field_name].get("status") != "omitted"
        assert persisted[field_name].get("reason") != "evidence_size_limit_exceeded"
    assert persisted["readiness"]["schema_version"] == "nhms.production_readiness.scheduler_input.v1"
    assert persisted["counts"]["candidate_count"] == 1
    assert persisted["execution_boundary"] == "planning_only"
    assert "slurm_submit_called" in persisted["no_mutation_proof"]
    assert persisted["duplicate_exclusions"]["status"] == "omitted"
    assert persisted["runtime_config"]["dry_run"] is False
    assert persisted["runtime_config"]["allowed_cycle_hours_utc"] == [0, 6, 12, 18]
    assert persisted["evidence_pre_execution"]["status"] == "reserved"
    assert persisted["execution_write_proof"]["status"] == "submitted"
    assert persisted["slurm_status_sync_proof"]["status"] == "not_required"
    assert persisted["slurm_cancellation_proof"]["status"] == "not_required"
    assert evidence == persisted


def test_write_evidence_fails_before_artifact_creation_when_bounded_core_cannot_fit(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_evidence

    pass_id = "scheduler_20260521120000_tiny_limit"
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    context = _scheduler_evidence_test_context(config, max_evidence_bytes=1_200)
    evidence = _large_scheduler_evidence_payload(pass_id)
    original = json.loads(json.dumps(evidence))
    artifact_path = evidence_dir / f"{pass_id}.json"

    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler_evidence.write_evidence(context, pass_id, evidence)

    assert error.value.reason == "evidence_size_limit_exceeded"
    assert not artifact_path.exists()
    assert evidence == original


def test_scheduler_evidence_context_accepts_exported_keyword_callbacks(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_evidence

    started_at = _dt("2026-05-21T12:00:00Z")
    config = _config(tmp_path, now=started_at, dry_run=False)
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    context = scheduler_evidence.SchedulerEvidenceWriteContext(
        config=config,
        require_safe_directory_final_component=scheduler_module._require_safe_directory_final_component,
        require_under_workspace=scheduler_module._require_under_workspace,
        max_evidence_bytes=1_500,
        bounded_evidence_payload=scheduler_evidence.bounded_evidence_payload,
        write_new_regular_file=scheduler_evidence.write_new_regular_file,
        require_evidence_artifact_available=scheduler_evidence.require_evidence_artifact_available,
        reservation_blocked_payload=scheduler_evidence.evidence_reservation_blocked_payload,
    )
    pass_id = "scheduler_20260521120000_keyword_callbacks"

    reservation = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        started_at,
        1,
        now=started_at,
    )
    pre_execution_artifact = evidence_dir / f"{pass_id}.pre_execution.json"
    persisted_reservation = json.loads(pre_execution_artifact.read_text(encoding="utf-8"))

    evidence = {
        "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        "pass_id": pass_id,
        "started_at": "2026-05-21T12:00:00Z",
        "status": "submitted",
        "execution_mode": "production_orchestration",
        "readiness_interpretation": "non_final_scheduler_evidence",
        "readiness": {"production_ready": False},
        "counts": {"candidate_count": 1},
        "runtime_config": {"dry_run": False},
        "root_preflight": {"status": "ready"},
        "evidence_pre_execution": reservation,
        "execution_write_proof": {"status": "submitted"},
        "slurm_status_sync_proof": {"status": "not_required"},
        "slurm_cancellation_proof": {"status": "not_required"},
        "no_mutation_proof": _expected_no_mutation_proof(),
        "candidates": [{"payload": "x" * 2_000}],
    }

    artifact_path = scheduler_evidence.write_evidence(context, pass_id, evidence)
    persisted_final = json.loads(Path(artifact_path or "").read_text(encoding="utf-8"))

    assert reservation["status"] == "reserved"
    assert persisted_reservation["status"] == "reserved"
    assert persisted_reservation["proof"] == "scheduler_evidence_directory_write_before_production_mutation"
    assert persisted_final["status"] == "resource_limit_blocked"
    assert len(Path(artifact_path or "").read_bytes()) <= 1_500
    assert persisted_final["limit"] == {"reason": "evidence_size_limit_exceeded", "max_evidence_bytes": 1_500}
    assert persisted_final["evidence_pre_execution"]["status"] == "reserved"
    assert evidence["status"] == "resource_limit_blocked"
    assert evidence["artifact_path"] == str(evidence_dir / f"{pass_id}.json")

    blocked_pass_id = f"{pass_id}_blocked"
    blocked_pre_execution_artifact = evidence_dir / f"{blocked_pass_id}.pre_execution.json"
    blocked_pre_execution_artifact.write_text("existing pre-execution\n", encoding="utf-8")

    blocked = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        blocked_pass_id,
        started_at,
        1,
        now=started_at,
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "evidence_artifact_exists"
    assert blocked["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
    assert blocked["artifact_path"] == str(blocked_pre_execution_artifact)
    assert blocked_pre_execution_artifact.read_text(encoding="utf-8") == "existing pre-execution\n"


def test_scheduler_evidence_module_imports_without_scheduler_cycle() -> None:
    import importlib

    module = importlib.import_module("services.orchestrator.scheduler_evidence")

    assert module.__name__ == "services.orchestrator.scheduler_evidence"
    assert not hasattr(module, "ProductionScheduler")
    assert module.empty_counts() == scheduler_module._empty_counts()


def test_non_dry_run_blocks_before_candidate_execution_when_evidence_reservation_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    orchestrator = FakeProductionOrchestrator()
    original_write_new_regular_file = scheduler_module._write_new_regular_file

    def fail_pre_execution_artifact(
        artifact_name: str,
        serialized: str,
        *,
        dir_fd: int,
        artifact_path: Path,
    ) -> None:
        if artifact_name.endswith(".pre_execution.json"):
            raise SchedulerEvidenceWriteError(
                "forced_pre_execution_evidence_failure",
                {"artifact_path": str(artifact_path)},
            )
        original_write_new_regular_file(
            artifact_name,
            serialized,
            dir_fd=dir_fd,
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(scheduler_module, "_write_new_regular_file", fail_pre_execution_artifact)
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert orchestrator.calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "evidence_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert result.evidence["evidence_pre_execution"]["status"] == "blocked"
    assert result.evidence["evidence_pre_execution"]["reason"] == "forced_pre_execution_evidence_failure"
    assert result.evidence["model_run_evidence"][0]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"


def test_non_dry_run_blocks_before_candidate_execution_when_final_evidence_artifact_exists(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    orchestrator = FakeProductionOrchestrator()
    fixed_pass_started_at = _dt("2026-05-21T12:00:00Z")

    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": "abcdef1234567890"})())
    scheduler = ProductionScheduler(
        _config(tmp_path, now=fixed_pass_started_at, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    pass_id = "scheduler_2026052112_abcdef123456"
    evidence_dir = Path(scheduler.config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{pass_id}.json").write_text("existing\n", encoding="utf-8")

    result = scheduler.run_once()

    assert orchestrator.calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "evidence_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["evidence_pre_execution"]["reason"] == "evidence_artifact_exists"
    assert result.evidence["model_run_evidence"][0]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"


def test_pre_execution_blocked_final_runtime_error_returns_no_artifact(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    orchestrator = FakeProductionOrchestrator()
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "abc123def456"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    original_write_new_regular_file = scheduler_module._write_new_regular_file

    def fail_final_evidence_artifact(
        artifact_name: str,
        serialized: str,
        *,
        dir_fd: int,
        artifact_path: Path,
    ) -> None:
        if artifact_name == f"{pass_id}.json":
            raise RuntimeError("forced final evidence failure")
        original_write_new_regular_file(
            artifact_name,
            serialized,
            dir_fd=dir_fd,
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    monkeypatch.setattr(scheduler_module, "_write_new_regular_file", fail_final_evidence_artifact)
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.parent.mkdir(parents=True)
    reservation_path.write_text("existing reservation\n", encoding="utf-8")

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.artifact_path is None
    assert orchestrator.calls == []
    assert result.evidence["evidence_pre_execution"]["status"] == "blocked"
    assert result.evidence["evidence_pre_execution"]["reason"] == "evidence_artifact_exists"
    assert result.evidence["evidence_write_error"]["reason"] == "evidence_write_failed"
    assert result.evidence["evidence_write_error"]["error"] == "forced final evidence failure"


def test_db_free_evidence_reservation_blocker_masks_artifact_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    from services.orchestrator import scheduler_evidence

    started_at = _dt("2026-06-27T16:00:00Z")
    config = ProductionSchedulerConfig(dry_run=False, now=started_at)
    context = _scheduler_evidence_test_context(config)
    pass_id = "scheduler_2026062716_abcdef123456"
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"{pass_id}.json").write_text("existing\n", encoding="utf-8")

    blocked = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        started_at,
        1,
        now=started_at,
    )
    rendered = json.dumps(blocked, sort_keys=True)

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "evidence_artifact_exists"
    assert blocked["artifact_path"] == "[local-path]"
    assert str(evidence_dir) not in rendered
    assert "db-free-local-root" not in rendered


def test_db_free_evidence_reservation_generic_oserror_masks_error_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    from services.orchestrator import scheduler_evidence

    started_at = _dt("2026-06-27T16:00:00Z")
    config = ProductionSchedulerConfig(dry_run=False, now=started_at)
    sensitive_path = tmp_path / "db-free-local-root" / "secret-token" / "evidence.json"

    def _raise_sensitive_oserror(
        _artifact_name: str,
        _serialized: str,
        *,
        dir_fd: int,
        artifact_path: Path,
    ) -> None:
        raise OSError(f"permission denied writing {sensitive_path} via {artifact_path} fd={dir_fd}")

    context = _scheduler_evidence_test_context(config, write_new_regular_file=_raise_sensitive_oserror)
    pass_id = "scheduler_2026062716_abcdef123456"
    Path(config.evidence_dir).mkdir(parents=True, exist_ok=True)

    blocked = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        started_at,
        1,
        now=started_at,
    )
    rendered = json.dumps(blocked, sort_keys=True)

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "evidence_write_failed"
    assert blocked["artifact_path"] == "[local-path]"
    assert blocked["error_type"] == "OSError"
    assert "error" not in blocked
    assert str(sensitive_path) not in rendered
    assert str(config.evidence_dir) not in rendered
    assert "db-free-local-root" not in rendered
    assert "secret-token" not in rendered


def test_db_free_evidence_reservation_guard_error_masks_error_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    from services.orchestrator import scheduler_evidence

    started_at = _dt("2026-06-27T16:00:00Z")
    config = ProductionSchedulerConfig(dry_run=False, now=started_at)
    sensitive_path = tmp_path / "db-free-local-root" / "secret-token" / "evidence"

    def _raise_sensitive_guard_error(
        _path: Path,
        _workspace_root: Path,
        field_name: str,
    ) -> None:
        raise ValueError(f"{field_name} escaped via {sensitive_path}")

    context = _scheduler_evidence_test_context(config, require_under_workspace=_raise_sensitive_guard_error)
    pass_id = "scheduler_2026062716_abcdef123456"

    blocked = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        started_at,
        1,
        now=started_at,
    )
    rendered = json.dumps(blocked, sort_keys=True)

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "evidence_write_failed"
    assert blocked["artifact_path"] == "[local-path]"
    assert blocked["error_type"] == "ValueError"
    assert "error" not in blocked
    assert str(sensitive_path) not in rendered
    assert "db-free-local-root" not in rendered
    assert "secret-token" not in rendered


def test_db_free_prelock_evidence_write_failure_masks_artifact_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_BACKEND", "memory")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": "abcdef1234567890"})())
    config = ProductionSchedulerConfig(now=_dt("2026-06-27T16:00:00Z"))
    pass_id = "scheduler_2026062716_abcdef123456"
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"{pass_id}.json").write_text("existing\n", encoding="utf-8")

    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert result.artifact_path is None
    assert result.evidence["evidence_write_error"]["reason"] == "evidence_artifact_exists"
    assert result.evidence["evidence_write_error"]["artifact_path"] == "[local-path]"
    assert str(evidence_dir) not in rendered
    assert "db-free-local-root" not in rendered


def test_db_free_prelock_evidence_root_outside_workspace_skips_artifact_write(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    outside_evidence_root = tmp_path / "outside-evidence-secret-token"
    outside_evidence_root.mkdir()
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(outside_evidence_root))
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert result.artifact_path is None
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["reason"] == "scheduler_root_preflight_blocked"
    assert result.evidence["root_preflight"]["status"] == "blocked"
    blocker = next(
        blocker
        for blocker in result.evidence["root_preflight"]["blockers"]
        if blocker["field"] == "evidence_root"
    )
    assert blocker["path"] == "[local-path]"
    assert result.evidence["root_preflight"]["checks"]["evidence_root"]["path"] == "[local-path]"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert str(outside_evidence_root) not in rendered
    assert "outside-evidence-secret-token" not in rendered


def test_db_free_prelock_size_limit_failure_masks_artifact_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    from services.orchestrator import scheduler_evidence

    config = ProductionSchedulerConfig(now=_dt("2026-06-27T16:00:00Z"))
    context = _scheduler_evidence_test_context(config, max_evidence_bytes=1)
    evidence: dict[str, Any] = {
        "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        "pass_id": "scheduler_2026062716_abcdef123456",
        "status": "preflight_blocked",
        "payload": "x" * 1000,
    }

    artifact_path = scheduler_evidence.write_prelock_blocked_evidence(
        context,
        "scheduler_2026062716_abcdef123456",
        evidence,
        {"checks": {"evidence_root": {"writable": True}}},
    )
    rendered = json.dumps(evidence, sort_keys=True)

    assert artifact_path is None
    assert evidence["evidence_write_error"]["reason"] == "evidence_size_limit_exceeded"
    assert evidence["evidence_write_error"]["artifact_path"] == "[local-path]"
    assert "db-free-local-root" not in rendered
    assert str(config.evidence_dir) not in rendered


def test_normal_mutation_sees_pre_execution_reservation_before_forcing_and_submit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "444455556666"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    reservation_path = tmp_path / "scheduler" / "evidence" / f"{pass_id}.pre_execution.json"
    producer_observations: list[dict[str, Any]] = []
    submit_observations: list[dict[str, Any]] = []

    class ReservationCheckingForcingProducer(FakeForcingProducer):
        def produce(self, **kwargs: Any) -> Any:
            producer_observations.append(
                {
                    "reservation_exists": reservation_path.is_file(),
                    "reservation": json.loads(reservation_path.read_text(encoding="utf-8"))
                    if reservation_path.is_file()
                    else None,
                }
            )
            return super().produce(**kwargs)

    class ReservationCheckingOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            submit_observations.append(
                {
                    "reservation_exists": reservation_path.is_file(),
                    "reservation": json.loads(reservation_path.read_text(encoding="utf-8"))
                    if reservation_path.is_file()
                    else None,
                }
            )
            return super().orchestrate_cycle(source, cycle_time, basins)

    forcing_producer = ReservationCheckingForcingProducer()
    orchestrator = ReservationCheckingOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert result.status == "submitted"
    assert [item["reservation_exists"] for item in producer_observations] == [True]
    assert [item["reservation_exists"] for item in submit_observations] == [True]
    assert producer_observations[0]["reservation"]["pass_id"] == pass_id
    assert submit_observations[0]["reservation"]["status"] == "reserved"
    assert len(forcing_producer.calls) == 1
    assert len(orchestrator.calls) == 1
    for evidence in (result.evidence, persisted):
        assert evidence["evidence_pre_execution"]["status"] == "reserved"
        assert evidence["evidence_pre_execution"]["artifact_path"] == str(reservation_path)
        assert evidence["evidence_pre_execution"]["proof"] == (
            "scheduler_evidence_directory_write_before_production_mutation"
        )
        assert evidence["execution_write_proof"]["protected_by_pre_execution_evidence"] is True
        assert evidence["no_mutation_proof"]["met_result_table_writes"] is True
        assert evidence["no_mutation_proof"]["slurm_submit_called"] is True


def test_cancel_active_slurm_blocks_before_cancel_when_final_evidence_artifact_exists(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    fixed_pass_started_at = _dt("2026-05-21T12:00:00Z")
    orchestrator = FakeProductionOrchestrator()
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": "abcdef1234567890"})())
    scheduler = ProductionScheduler(
        _config(tmp_path, now=fixed_pass_started_at, dry_run=False, cancel_active_slurm=True),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(
            active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    pass_id = "scheduler_2026052112_abcdef123456"
    evidence_dir = Path(scheduler.config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{pass_id}.json").write_text("existing\n", encoding="utf-8")

    result = scheduler.run_once()

    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert orchestrator.cancel_calls == []
    assert orchestrator.calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "evidence_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert result.evidence["evidence_pre_execution"]["reason"] == "evidence_artifact_exists"
    assert result.evidence["model_run_evidence"] == []
    assert cancellation["status"] == "preflight_blocked"
    assert cancellation["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
    assert cancellation["cancel_attempted"] is False
    assert cancellation["mutation_occurred"] is False


def test_pre_execution_existing_regular_artifact_blocks_before_forcing_and_submit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "aaaabbbbcccc"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    forcing_producer = FakeForcingProducer()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.parent.mkdir(parents=True)
    reservation_path.write_text("existing reservation\n", encoding="utf-8")

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert reservation_path.read_text(encoding="utf-8") == "existing reservation\n"
    assert forcing_producer.calls == []
    assert orchestrator.calls == []
    assert orchestrator.cancel_calls == []
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "preflight_blocked"
        assert evidence["execution_boundary"] == "evidence_preflight_blocked"
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["no_mutation_proof"] == _expected_no_mutation_proof()
        assert evidence["evidence_pre_execution"]["status"] == "blocked"
        assert evidence["evidence_pre_execution"]["reason"] == "evidence_artifact_exists"
        assert evidence["evidence_pre_execution"]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert evidence["evidence_pre_execution"]["artifact_path"] == str(reservation_path)
        assert evidence["model_run_evidence"][0]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert evidence["model_run_evidence"][0]["submitted"] is False
        assert evidence["model_run_evidence"][0]["mutation_occurred"] is False


def test_pre_execution_evidence_block_forces_retention_dry_run_before_deletion(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "bbbbccccdddd"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    object_store_root = tmp_path / "object-store"
    old_cycle = format_cycle_time(now - timedelta(days=30))
    expired_file = object_store_root / "raw" / "gfs" / old_cycle / "gfs.f000.nc"
    expired_file.parent.mkdir(parents=True)
    expired_file.write_text("expired\n", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)
    forcing_producer = FakeForcingProducer()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False, object_store_root=object_store_root),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.parent.mkdir(parents=True)
    reservation_path.write_text("existing reservation\n", encoding="utf-8")

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert expired_file.parent.exists()
    assert forcing_producer.calls == []
    assert orchestrator.calls == []
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "preflight_blocked"
        assert evidence["execution_boundary"] == "evidence_preflight_blocked"
        assert evidence["no_mutation_proof"] == _expected_no_mutation_proof()
        assert evidence["evidence_pre_execution"]["status"] == "blocked"
        assert evidence["retention"]["status"] == "completed"
        assert evidence["retention"]["dry_run"] is True
        assert evidence["retention"]["forced_dry_run_by_scheduler"] is True
        assert evidence["retention"]["forced_dry_run_reason"] == "evidence_preflight_blocked"
        assert evidence["retention"]["planned"]
        assert evidence["retention"]["deleted"] == []


def test_pre_execution_symlink_artifact_blocks_before_status_sync_and_preserves_target(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "ddddeeeeffff"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    sync_calls: list[str] = []

    class SyncMustNotRunOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            sync_calls.append(cycle_id)
            return [{"job_id": "job_forcing", "cycle_id": cycle_id, "slurm_job_id": "7777", "status": "failed"}]

    orchestrator = SyncMustNotRunOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(
            active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.parent.mkdir(parents=True)
    outside_target = tmp_path.parent / f"{tmp_path.name}-pre-execution-outside-target.json"
    outside_target.write_text("keep outside\n", encoding="utf-8")
    reservation_path.symlink_to(outside_target)

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert reservation_path.is_symlink()
    assert outside_target.read_text(encoding="utf-8") == "keep outside\n"
    assert sync_calls == []
    assert orchestrator.calls == []
    assert orchestrator.cancel_calls == []
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "preflight_blocked"
        assert evidence["execution_boundary"] == "evidence_preflight_blocked"
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["slurm_status_sync_count"] == 0
        assert evidence["no_mutation_proof"] == _expected_no_mutation_proof()
        assert evidence["evidence_pre_execution"]["status"] == "blocked"
        assert evidence["evidence_pre_execution"]["reason"] == "unsafe_evidence_artifact"
        assert evidence["evidence_pre_execution"]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert evidence["evidence_pre_execution"]["artifact_path"] == str(reservation_path)
        assert evidence["slurm_status_sync_proof"]["status"] == "preflight_blocked"
        assert evidence["slurm_status_sync_proof"]["sync_called"] is False
        assert evidence["model_run_evidence"][0]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert evidence["model_run_evidence"][0]["sync_attempted"] is False
        assert evidence["model_run_evidence"][0]["mutation_occurred"] is False


def test_pre_execution_non_regular_artifact_blocks_before_cancel(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    suffix = "111122223333"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    forcing_producer = FakeForcingProducer()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False, cancel_active_slurm=True),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(
            active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
        ),
        forcing_producer=forcing_producer,
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.mkdir(parents=True)

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert reservation_path.is_dir()
    assert forcing_producer.calls == []
    assert orchestrator.calls == []
    assert orchestrator.cancel_calls == []
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "preflight_blocked"
        assert evidence["execution_boundary"] == "evidence_preflight_blocked"
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["slurm_cancelled_count"] == 0
        assert evidence["no_mutation_proof"] == _expected_no_mutation_proof()
        assert evidence["evidence_pre_execution"]["status"] == "blocked"
        assert evidence["evidence_pre_execution"]["reason"] == "unsafe_evidence_artifact"
        assert evidence["evidence_pre_execution"]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert evidence["evidence_pre_execution"]["artifact_path"] == str(reservation_path)
        assert evidence["model_run_evidence"] == []
        assert evidence["slurm_cancellation_proof"]["status"] == "preflight_blocked"
        cancellation = evidence["slurm_cancellation_evidence"][0]
        assert cancellation["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
        assert cancellation["cancel_attempted"] is False
        assert cancellation["mutation_occurred"] is False


def test_stale_unowned_lock_is_not_unlinked(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lock_path.write_text(json.dumps({"pass_id": "foreign"}), encoding="utf-8")
    os.utime(lock_path, (1, 1))
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        lock_path=lock_path,
        lock_ttl_seconds=1,
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["reason"] == "unsafe_lock_not_scheduler_owned"
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8")) == {"pass_id": "foreign"}


def test_stale_lock_symlink_is_not_unlinked(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("keep", encoding="utf-8")
    lock_path = tmp_path / "scheduler.lock"
    lock_path.symlink_to(target)
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        lock_path=lock_path,
        lock_ttl_seconds=1,
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["reason"] == "unsafe_lock_symlink"
    assert lock_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_lock_guard_symlink_is_not_opened_or_written(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    outside_guard = tmp_path.parent / f"{tmp_path.name}-outside-guard"
    guard_path = lock_path.with_name(f"{lock_path.name}.guard")
    guard_path.symlink_to(outside_guard)
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), lock_path=lock_path)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["reason"] == "unsafe_lock_guard_not_regular_file"
    assert not outside_guard.exists()
    assert guard_path.is_symlink()
    assert not lock_path.exists()


def test_lock_guard_open_failure_closes_parent_fd(monkeypatch: Any, tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=1, workspace_root=tmp_path)
    closed: list[int] = []
    real_close = os.close

    def failing_guard(_guard_name: str, *, dir_fd: int) -> int:
        raise RuntimeError(f"guard failed for {dir_fd}")

    def tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr("services.orchestrator.scheduler._open_regular_guard_file", failing_guard)
    monkeypatch.setattr(os, "close", tracking_close)

    with pytest.raises(RuntimeError, match="guard failed"):
        with lease._guarded():
            raise AssertionError("guarded body should not run")

    assert len(closed) == 1


def test_lock_parent_symlink_is_rejected_at_acquire_without_outside_files(tmp_path: Path) -> None:
    outside_locks = tmp_path.parent / f"{tmp_path.name}-outside-locks"
    outside_locks.mkdir()
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        evidence_dir=tmp_path / "evidence",
    )
    lock_path = Path(config.lock_path)
    lock_path.parent.mkdir()
    lock_path.parent.rmdir()
    lock_path.parent.symlink_to(outside_locks, target_is_directory=True)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["reason"] == "unsafe_lock_parent_directory"
    assert not (outside_locks / lock_path.name).exists()
    assert not (outside_locks / f"{lock_path.name}.guard").exists()


def test_stale_scheduler_lock_takeover_does_not_delete_fresh_contender_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lock_path.write_text(
        json.dumps(
            {
                "owner": LOCK_OWNER,
                "schema_version": LOCK_SCHEMA_VERSION,
                "lease_token": "stale-token",
                "pass_id": "stale",
            }
        ),
        encoding="utf-8",
    )
    os.utime(lock_path, (1, 1))
    first = FileSchedulerLease(lock_path, ttl_seconds=1)
    second = FileSchedulerLease(lock_path, ttl_seconds=1)

    first_result = first.acquire(pass_id="first", started_at=_dt("2026-05-21T12:00:00Z"))
    second_result = second.acquire(pass_id="second", started_at=_dt("2026-05-21T12:00:00Z"))

    assert first_result["acquired"] is True
    assert second_result["acquired"] is False
    assert second_result["existing_lock"]["pass_id"] == "first"
    first.release(pass_id="first")
    assert not lock_path.exists()


def test_scheduler_caps_reject_oversized_config_and_bound_candidate_work(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    with pytest.raises(ValueError, match="lookback_hours exceeds limit"):
        _config(tmp_path, lookback_hours=169)
    with pytest.raises(ValueError, match="source count exceeds limit"):
        _config(tmp_path, sources=("gfs", "IFS", "a", "b", "c"))

    monkeypatch.setattr(scheduler_module, "MAX_CANDIDATES", 10)
    config = _config(tmp_path, now=_dt("2026-05-21T18:00:00Z"), sources=("gfs",), max_cycles_per_source=2)
    models = [_model(f"model_{index:05d}", "basin_a") for index in range(11)]
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(models),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
            )
        },
    )

    result = scheduler.run_once()

    assert result.status == "resource_limit_blocked"
    assert result.evidence["limit"]["reason"] == "candidate_limit_exceeded"
    assert result.evidence["candidates"] == []


def test_cycle_discovery_limit_blocks_before_candidate_or_duplicate_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), lookback_hours=1)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": OverLimitAdapter("gfs", "2026-05-21T12:00:00Z")},
    )

    result = scheduler.run_once()

    assert result.status == "resource_limit_blocked"
    assert result.evidence["limit"]["reason"] == "cycle_discovery_limit_exceeded"
    assert result.evidence["limit"]["max_discovered_cycles"] == MAX_DISCOVERED_CYCLES
    assert result.evidence["limit"]["discovered_cycle_count"] == MAX_DISCOVERED_CYCLES + 1
    assert result.evidence["counts"]["source_cycle_count"] == 0
    assert result.evidence["source_cycles"] == []
    assert result.evidence["candidates"] == []
    assert result.evidence["duplicate_exclusions"] == []


def test_evidence_size_fallback_status_agrees_across_result_artifact_and_cli(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    max_evidence_bytes = 2_400
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", max_evidence_bytes)
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert result.status == "resource_limit_blocked"
    assert result.evidence["status"] == "resource_limit_blocked"
    assert persisted["status"] == "resource_limit_blocked"
    assert len(Path(result.artifact_path or "").read_bytes()) <= max_evidence_bytes
    assert result.evidence["limit"]["reason"] == "evidence_size_limit_exceeded"
    assert persisted["limit"]["reason"] == "evidence_size_limit_exceeded"

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
            assert max_passes == 1
            return [result]

    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=(),
        basin_ids=(),
        dry_run=True,
        continuous=True,
        interval_seconds=300.0,
        max_passes=1,
        workspace_root=str(tmp_path),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "resource_limit_blocked"
    assert payload["passes"][0]["status"] == "resource_limit_blocked"


def test_bounded_evidence_preserves_no_flag_root_runtime_and_preflight_proof(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 2_400)
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model("model_a", "basin_a")]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )
    persisted = json.loads(Path(payload["artifact_path"]).read_text(encoding="utf-8"))

    assert payload["status"] == "resource_limit_blocked"
    assert persisted["status"] == "resource_limit_blocked"
    for evidence in (payload, persisted):
        assert evidence["resolved_runtime_roots"]["workspace_root"]["path"] == str(roots["workspace_root"].resolve())
        assert evidence["resolved_runtime_roots"]["evidence_root"]["path"] == str(roots["evidence_root"].resolve())
        assert evidence["runtime_config"]["require_runtime_roots"] is True
        assert evidence["runtime_config"]["service_role"] == "compute_control"
        assert evidence["root_preflight"]["status"] == "ready"
        assert evidence["root_preflight"]["checks"]["allowed_roots_policy"]["non_empty"] is True


def test_no_flag_resource_limit_evidence_retains_runtime_root_preflight_proof(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.delenv("NHMS_SCHEDULER_MODEL_IDS")
    monkeypatch.delenv("NHMS_SCHEDULER_BASIN_IDS")
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr(scheduler_module, "MAX_CANDIDATES", 10)
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model(f"model_{index:05d}", "basin_a") for index in range(11)]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
            )
        },
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T18:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=2,
        model_ids=(),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )
    persisted = json.loads(Path(payload["artifact_path"]).read_text(encoding="utf-8"))

    assert payload["status"] == "resource_limit_blocked"
    assert payload["limit"]["reason"] == "candidate_limit_exceeded"
    for evidence in (payload, persisted):
        assert evidence["resolved_runtime_roots"]["workspace_root"]["path"] == str(roots["workspace_root"].resolve())
        assert evidence["resolved_runtime_roots"]["evidence_root"]["path"] == str(roots["evidence_root"].resolve())
        assert evidence["runtime_config"]["require_runtime_roots"] is True
        assert evidence["runtime_config"]["service_role"] == "compute_control"
        assert evidence["root_preflight"]["status"] == "ready"
        assert evidence["root_preflight"]["checks"]["allowed_roots_policy"]["non_empty"] is True


def test_bounded_evidence_preserves_pre_execution_reservation_proof(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 2_600)

    class SyncingRepository(CandidateAndActiveRepository):
        def __init__(self) -> None:
            self.synced = False
            super().__init__(
                {
                    "pipeline_status": "running",
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "status": "running",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                        }
                    ],
                },
                [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
            )

        def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
            if self.synced:
                return {
                    "pipeline_status": "failed",
                    "failed_stage": "forcing",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 0,
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": kwargs["run_id"],
                            "status": "failed",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                }
            return super().candidate_state(**kwargs)

        def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [] if self.synced else super().active_slurm_jobs(**kwargs)

    repository = SyncingRepository()

    class SyncingOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            repository.synced = True
            return [{"job_id": "job_forcing", "cycle_id": cycle_id, "slurm_job_id": "7777", "status": "failed"}]

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: SyncingOrchestrator(),
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert result.status == "resource_limit_blocked"
    assert persisted["status"] == "resource_limit_blocked"
    for evidence in (result.evidence, persisted):
        assert evidence["evidence_pre_execution"]["status"] == "reserved"
        assert evidence["evidence_pre_execution"]["proof"] == (
            "scheduler_evidence_directory_write_before_production_mutation"
        )
        assert evidence["slurm_status_sync_proof"]["protected_by_pre_execution_evidence"] is True
        assert evidence["resolved_runtime_roots"]["workspace_root"]["path"] == str(tmp_path.resolve())
        assert evidence["runtime_config"]["dry_run"] is False


def test_duplicate_active_model_identity_is_rejected_before_candidates(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_a", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert {item["reason"] for item in result.evidence["model_discovery"]["exclusions"]} == {
        "duplicate_active_model_identity"
    }


@pytest.mark.parametrize("duplicate_field", ["model_package_uri", "package_checksum"])
def test_duplicate_active_package_identity_is_rejected_before_candidates_and_submission(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    package_uri_a = "s3://nhms/models/shared/package/"
    package_uri_b = package_uri_a if duplicate_field == "model_package_uri" else "s3://nhms/models/other/package/"
    checksum_a = "shared-package-sha"
    checksum_b = checksum_a if duplicate_field == "package_checksum" else "other-package-sha"
    model_a = _model(
        "model_a",
        "basin_a",
        resource_profile={"runnable": True, "package_checksum": checksum_a, "lineage": "basins_registry_import"},
    )
    model_b = _model(
        "model_b",
        "basin_b",
        resource_profile={"runnable": True, "package_checksum": checksum_b, "lineage": "basins_registry_import"},
    )
    model_a["model_package_uri"] = package_uri_a
    model_b["model_package_uri"] = package_uri_b
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([model_a, model_b]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()

    exclusions = result.evidence["model_discovery"]["exclusions"]
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert {item["reason"] for item in exclusions} == {"duplicate_active_model_identity"}
    assert {item["duplicate_identity_field"] for item in exclusions} == {duplicate_field}
    assert {tuple(item["duplicate_model_ids"]) for item in exclusions} == {("model_a", "model_b")}


def test_duplicate_active_package_checksum_uses_internal_projection_without_public_leak(
    tmp_path: Path,
) -> None:
    model_a = _model(
        "model_a",
        "basin_a",
        resource_profile={
            "runnable": True,
            "package_checksum": "shared-package-sha",
            "lineage": "basins_registry_import",
        },
    )
    model_b = _model(
        "model_b",
        "basin_b",
        resource_profile={
            "runnable": True,
            "package_checksum": "shared-package-sha",
            "lineage": "basins_registry_import",
        },
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=RedactingRegistry([model_a, model_b]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()

    evidence_json = json.dumps(result.evidence, sort_keys=True)
    exclusions = result.evidence["model_discovery"]["exclusions"]
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert {item["duplicate_identity_field"] for item in exclusions} == {"package_checksum"}
    assert {item["duplicate_identity_value"] for item in exclusions} == {"[redacted]"}
    assert "shared-package-sha" not in evidence_json


def test_public_only_redacted_projection_cannot_checksum_dedupe_without_internal_path(
    tmp_path: Path,
) -> None:
    model_a = _model(
        "model_a",
        "basin_a",
        resource_profile={
            "runnable": True,
            "package_checksum": "shared-package-sha",
            "lineage": "basins_registry_import",
        },
    )
    model_b = _model(
        "model_b",
        "basin_b",
        resource_profile={
            "runnable": True,
            "package_checksum": "shared-package-sha",
            "lineage": "basins_registry_import",
        },
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z")),
        registry=PublicOnlyRedactingRegistry([model_a, model_b]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert {item["model_id"] for item in result.evidence["candidates"]} == {"model_a", "model_b"}


@pytest.mark.parametrize("missing_field", ["basin_version_id", "river_network_version_id", "model_package_uri"])
def test_incomplete_production_model_metadata_is_blocked_before_candidates(
    tmp_path: Path,
    missing_field: str,
) -> None:
    model = _model("model_a", "basin_a")
    model.pop(missing_field)
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    exclusion = result.evidence["model_discovery"]["exclusions"][0]
    assert exclusion["reason"] == "incomplete_model_metadata"
    assert exclusion["missing_fields"] == [missing_field]
    assert result.evidence["counts"]["selected_model_count"] == 0


def test_bootstrapped_qhh_model_is_scheduler_ready_without_metadata_exclusions(tmp_path: Path) -> None:
    model = {
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "segment_count": 2,
        "model_package_uri": "s3://nhms/models/basins_qhh_shud/vbasins-qhh-production/package/",
        "shud_code_version": "basins-shud",
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": {
            "runnable": True,
            "lineage": "qhh_production_bootstrap",
            "project_name": "qhh",
            "station_count": 2,
            "output_segment_count": 2,
            "display_capabilities": {"q_down": True, "tiles": True},
        },
    }
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), model_ids=("basins_qhh_shud",)),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    excluded_reasons = {item["reason"] for item in result.evidence["model_discovery"]["exclusions"]}
    assert "basins_qhh_shud" in {item["model_id"] for item in result.evidence["candidates"]}
    assert not {"not_shud_model", "not_runnable", "incomplete_model_metadata"} & excluded_reasons


def test_qhh_project_name_propagates_from_resource_profile_to_runtime_manifest(tmp_path: Path) -> None:
    model = {
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "segment_count": 5,
        "model_package_uri": "s3://nhms/models/basins_qhh_shud/vbasins-qhh-production/package/",
        "shud_code_version": "basins-shud",
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": {
            "runnable": True,
            "lineage": "qhh_production_bootstrap",
            "project_name": "qhh",
            "shud_input_name": "qhh",
            "station_count": 2,
            "output_segment_count": 2,
            "package_checksum": "package-sha",
            "source_inventory_checksum": "inventory-sha",
            "display_capabilities": {"q_down": True, "tiles": True},
        },
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, model_ids=("basins_qhh_shud",)),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    submitted_basin = orchestrator.calls[0]["basins"][0]
    assembly = build_model_run_assembly(
        submitted_basin,
        source_id="gfs",
        cycle_id="gfs_2026052106",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
        scenario_id="forecast_gfs_deterministic",
        workspace_root=tmp_path / "workspace",
        object_store=LocalObjectStore(tmp_path / "object-store", "s3://nhms"),
        default_forecast_horizon_hours=168,
    )
    manifest = {
        "model": {
            "model_id": submitted_basin["model_id"],
            "project_name": assembly.runtime["project_name"],
            "shud_input_name": submitted_basin["shud_input_name"],
        },
        "runtime": dict(assembly.runtime),
    }

    assert result.status == "submitted"
    assert submitted_basin["project_name"] == "qhh"
    assert submitted_basin["shud_input_name"] == "qhh"
    assert submitted_basin["package_checksum"] == "package-sha"
    assert submitted_basin["source_inventory_checksum"] == "inventory-sha"
    assert "package-sha" not in json.dumps(result.evidence["candidates"])
    assert "inventory-sha" not in json.dumps(result.evidence["candidates"])
    assert result.evidence["candidates"][0]["resource_profile"]["package_checksum"] == "[redacted]"
    assert result.evidence["candidates"][0]["resource_profile"]["source_inventory_checksum"] == "[redacted]"
    assert assembly.runtime["project_name"] == "qhh"
    assert shud_runtime_module._project_name(manifest) == "qhh"


def test_qhh_output_segment_count_propagates_separately_from_gis_segment_count(tmp_path: Path) -> None:
    model = {
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "segment_count": 5,
        "model_package_uri": "s3://nhms/models/basins_qhh_shud/vbasins-qhh-production/package/",
        "shud_code_version": "basins-shud",
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": {
            "runnable": True,
            "lineage": "qhh_production_bootstrap",
            "project_name": "qhh",
            "station_count": 2,
            "output_segment_count": 2,
            "display_capabilities": {"q_down": True, "tiles": True},
        },
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, model_ids=("basins_qhh_shud",)),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    candidate = result.evidence["candidates"][0]
    submitted_basin = orchestrator.calls[0]["basins"][0]
    model_evidence = result.evidence["model_run_evidence"][0]
    assert candidate["segment_count"] == 5
    assert candidate["output_segment_count"] == 2
    assert submitted_basin["segment_count"] == 5
    assert submitted_basin["output_segment_count"] == 2
    assert submitted_basin["output_river"]["segment_count"] == 2
    assert submitted_basin["output_river"]["output_segment_count"] == 2
    assert submitted_basin["output_river"]["gis_segment_count"] == 5
    assert model_evidence["segment_count"] == 5
    assert model_evidence["output_segment_count"] == 2
    assert model_evidence["outputs"]["segment_count"] == 2
    assert model_evidence["outputs"]["output_segment_count"] == 2
    assert model_evidence["outputs"]["gis_segment_count"] == 5
    assert model_evidence["quality_states"]["output_river"]["segment_count"] == 2


def test_runtime_manifest_assembly_uses_shud_output_count_not_gis_segment_count(tmp_path: Path) -> None:
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    basin = {
        "candidate_id": "gfs:2026-05-21T06:00:00Z:basins_qhh_shud:forecast_gfs_deterministic",
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "segment_count": 5,
        "output_segment_count": 2,
        "model_package_uri": "s3://nhms/models/basins_qhh_shud/vbasins-qhh-production/package/",
        "model_package_manifest_uri": "s3://nhms/models/basins_qhh_shud/vbasins-qhh-production/manifest.json",
        "run_id": "fcst_gfs_2026052106_basins_qhh_shud",
        "forcing_version_id": "forc_gfs_2026052106_basins_qhh_shud",
        "forcing_uri": "s3://nhms/forcing/gfs/2026052106/basins_qhh_vbasins/basins_qhh_shud/",
        "station_count": 2,
        "resource_profile": {"project_name": "qhh", "shud_input_name": "qhh"},
        "display_capabilities": {"tiles": True},
    }

    assembly = build_model_run_assembly(
        basin,
        source_id="gfs",
        cycle_id="gfs_2026052106",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
        scenario_id="forecast_gfs_deterministic",
        workspace_root=tmp_path / "workspace",
        object_store=object_store,
        default_forecast_horizon_hours=168,
    )
    manifest = {
        "identity": dict(assembly.identity),
        "model": {
            "model_id": "basins_qhh_shud",
            "basin_version_id": "basins_qhh_vbasins",
            "river_network_version_id": "basins_qhh_rivnet_vbasins",
            "model_package_uri": basin["model_package_uri"],
            "segment_count": basin["segment_count"],
            "output_segment_count": assembly.identity["segment_count"],
        },
        "runtime": dict(assembly.runtime),
        "outputs": dict(assembly.outputs),
    }

    assert assembly.identity["segment_count"] == 2
    assert assembly.runtime["project_name"] == "qhh"
    assert assembly.runtime["output_river"]["segment_count"] == 2
    assert assembly.runtime["output_river"]["output_segment_count"] == 2
    assert assembly.runtime["output_river"]["gis_segment_count"] == 5
    assert assembly.outputs["output_segment_count"] == 2
    assert assembly.outputs["gis_segment_count"] == 5
    assert shud_runtime_module._segment_count(manifest) == 2
    assert shud_runtime_module._project_name(manifest) == "qhh"


def test_active_duplicate_pipeline_is_skipped_before_submission(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    active_repository = FakeActiveRepository(active=True)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_duplicate_pipeline"
    assert result.evidence["counts"]["candidate_count"] == 1
    assert result.evidence["counts"]["skipped_candidate_count"] == 1
    assert result.evidence["counts"]["submitted_count"] == 0


@pytest.mark.parametrize(
    ("database_url", "expected_code"),
    [
        (None, "SLURM_PREFLIGHT_DATABASE_URL_MISSING"),
        ("postgresql://nhms:secret@localhost/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@localhost./nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@LOCALHOST/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@localhost.localdomain/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@localhost.localdomain./nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@ip6-localhost/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@ip6-loopback/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@foo.localhost/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@127.0.0.1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@127.1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@2130706433/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@127.000.000.001/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@0177.0.0.1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@[::1]/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@[0:0:0:0:0:0:0:1]/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@0.0.0.0/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@[::]/nhms", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
        ("postgresql://nhms:secret@169.254.1.1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@[fe80::1]/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@169.254.1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@169.254.257/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@0xa9fe0101/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@2851995905/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@169.254.0x101/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@bad::host/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@[::1/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@bad host/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("postgresql://nhms:secret@9999999999/nhms", "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST"),
        ("sqlite:///tmp/nhms.db", "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"),
    ],
)
def test_slurm_preflight_blocks_missing_or_localhost_database_before_submission(
    tmp_path: Path,
    database_url: str | None,
    expected_code: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url=database_url,
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence = result.evidence["model_run_evidence"][0]
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "slurm_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert evidence["status"] == "preflight_blocked"
    assert evidence["submitted"] is False
    assert evidence["error_code"] == expected_code
    assert expected_code in {blocker["code"] for blocker in evidence["slurm_preflight"]["blockers"]}
    assert "secret" not in json.dumps(evidence)
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    "host",
    [
        "localhost.",
        "LOCALHOST",
        "[::1]",
        "0:0:0:0:0:0:0:1",
        "ip6-localhost",
        "ip6-loopback",
        "foo.localhost.",
        "127.1",
        "2130706433",
        "0",
    ],
)
def test_database_host_local_classifier_normalizes_localhost_equivalents(host: str) -> None:
    assert scheduler_module._database_host_is_local(host) is True
    assert scheduler_module._database_host_is_unsafe(host) is False


@pytest.mark.parametrize(
    "host",
    [
        "127.000.000.001",
        "0177.0.0.1",
        "169.254.1.1",
        "fe80::1",
        "169.254.1",
        "169.254.257",
        "0xa9fe0101",
        "2851995905",
        "169.254.0x101",
        "bad host",
        "bad::host",
        "9999999999",
    ],
)
def test_database_host_classifier_conservatively_blocks_unsafe_numeric_or_malformed_hosts(host: str) -> None:
    assert scheduler_module._database_host_is_unsafe(host) is True


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nhms:secret@db.prod.example/nhms",
        "postgresql://nhms:secret@203.0.113.10/nhms",
        "postgresql://nhms:secret@10.0.0.5/nhms",
    ],
)
def test_slurm_preflight_accepts_remote_database_without_db_blocker(
    tmp_path: Path,
    database_url: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url=database_url,
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["slurm_preflight"]["status"] == "ready"
    assert not any(
        blocker["code"].startswith("SLURM_PREFLIGHT_DATABASE_URL")
        for blocker in result.evidence["slurm_preflight"]["blockers"]
    )
    assert result.evidence["counts"]["submitted_count"] == 1
    assert len(orchestrator.calls) == 1


def test_slurm_preflight_blocks_localhost_database_in_continuous_mode(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        continuous=True,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@foo.localhost/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    results = scheduler.run_continuous(max_passes=1)

    evidence = results[0].evidence["model_run_evidence"][0]
    assert results[0].status == "preflight_blocked"
    assert results[0].evidence["counts"]["submitted_count"] == 0
    assert evidence["error_code"] == "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST"
    assert orchestrator.calls == []


def test_slurm_preflight_requires_database_url_not_pipeline_database_url(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://nhms:secret@db.prod.example/nhms")
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence = result.evidence["model_run_evidence"][0]
    assert config.database_url is None
    assert result.status == "preflight_blocked"
    assert evidence["error_code"] == "SLURM_PREFLIGHT_DATABASE_URL_MISSING"
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    ("root_overrides", "expected_code"),
    [
        ({"object_store_root": None}, "SLURM_PREFLIGHT_OBJECT_STORE_ROOT_MISSING"),
        ({"object_store_root": "outside"}, "SLURM_PREFLIGHT_OBJECT_STORE_ROOT_OUT_OF_ROOT"),
        ({"log_root": "missing"}, "SLURM_PREFLIGHT_LOG_ROOT_NOT_VISIBLE"),
        ({"runtime_root": None}, "SLURM_PREFLIGHT_RUNTIME_ROOT_MISSING"),
    ],
)
def test_slurm_preflight_blocks_missing_out_of_root_or_not_visible_storage_roots(
    tmp_path: Path,
    root_overrides: dict[str, str | None],
    expected_code: str,
) -> None:
    allowed_root = tmp_path / "allowed"
    roots = _slurm_roots(allowed_root)
    outside = tmp_path / "outside-object-store"
    outside.mkdir()
    missing = allowed_root / "missing-logs"
    config_kwargs: dict[str, Any] = {
        "workspace_root": roots["workspace_root"],
        "object_store_root": roots["object_store_root"],
        "log_root": roots["log_root"],
        "runtime_root": roots["runtime_root"],
    }
    for field, value in root_overrides.items():
        if value == "outside":
            config_kwargs[field] = outside
        elif value == "missing":
            config_kwargs[field] = missing
        else:
            config_kwargs[field] = value

    orchestrator = FakeProductionOrchestrator()
    config = _config(
        config_kwargs.pop("workspace_root"),
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        allowed_storage_roots=(allowed_root,),
        **config_kwargs,
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence = result.evidence["model_run_evidence"][0]
    assert result.status == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert expected_code in {blocker["code"] for blocker in evidence["slurm_preflight"]["blockers"]}
    assert orchestrator.calls == []


def test_slurm_preflight_allows_safe_template_env_and_submits_through_orchestrator(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_env={"NHMS_PROFILE": "prod/gfs_00", "NHMS_RUN_LABEL": "prod_gfs_00"},
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["execution_boundary"] == "slurm_gateway_orchestration"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is True
    assert result.evidence["slurm_preflight"]["status"] == "ready"
    assert result.evidence["slurm_preflight"]["checks"]["environment"]["sanitized"] == {
        "NHMS_PROFILE": "prod/gfs_00",
        "NHMS_RUN_LABEL": "prod_gfs_00",
    }
    forcing_template = result.evidence["slurm_preflight"]["checks"]["templates"]["stage_templates"]["forcing"]
    assert forcing_template["template_name"] == "produce_forcing_array.sbatch"
    assert forcing_template["allowlisted"] is True
    assert len(orchestrator.calls) == 1
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["slurm_env"] == {
        "NHMS_PROFILE": "prod/gfs_00",
        "NHMS_RUN_LABEL": "prod_gfs_00",
    }


def test_slurm_preflight_passes_download_source_env_to_compute_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _slurm_roots(tmp_path)
    monkeypatch.setenv("IFS_OPEN_DATA_SOURCE", "aws")
    monkeypatch.setenv("IFS_OPEN_DATA_FALLBACK_SOURCES", "azure,google,ecmwf")
    monkeypatch.setenv("GFS_NOMADS_BASE_URL", "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_SOUTH", "8")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_NORTH", "64")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_WEST", "63")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_EAST", "145")
    monkeypatch.setenv("NHMS_GRIB_ENV_ROOT", str(tmp_path / "nhms-grib"))
    (tmp_path / "nhms-grib" / "bin").mkdir(parents=True)
    (tmp_path / "nhms-grib" / "lib").mkdir(parents=True)
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    expected = {
        "GFS_NOMADS_BASE_URL": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod",
        "IFS_OPEN_DATA_SOURCE": "aws",
        "IFS_OPEN_DATA_FALLBACK_SOURCES": "azure,google,ecmwf",
        "NHMS_DOWNLOAD_BBOX_SOUTH": "8",
        "NHMS_DOWNLOAD_BBOX_NORTH": "64",
        "NHMS_DOWNLOAD_BBOX_WEST": "63",
        "NHMS_DOWNLOAD_BBOX_EAST": "145",
        "NHMS_GRIB_ENV_ROOT": str(tmp_path / "nhms-grib"),
    }
    assert result.status == "submitted"
    sanitized = result.evidence["slurm_preflight"]["checks"]["environment"]["sanitized"]
    for key, value in expected.items():
        assert sanitized[key] == value
    submitted_basin = orchestrator.calls[0]["basins"][0]
    for key, value in expected.items():
        assert submitted_basin["slurm_env"][key] == value


def test_slurm_preflight_ready_without_factory_uses_default_orchestrator_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    constructed: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    class DefaultPathOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any, retry_service: Any = None) -> None:
            constructed.append(
                {
                    "config": config,
                    "repository": repository,
                    "state_manager": state_manager,
                    "retry_service": retry_service,
                }
            )
            self.config = config
            self.object_store = LocalObjectStore(config.object_store_root, config.object_store_prefix)

        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            stages = tuple(
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=f"default_job_{stage.stage}",
                    slurm_job_id=f"default_slurm_{stage.stage}",
                    status="succeeded",
                )
                for stage in M3_STAGES
            )
            return PipelineResult(
                run_id=f"default_cycle_{source}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=stages,
            )

    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms/default")
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://slurm-gateway.internal:8000")
    monkeypatch.setenv("FORECAST_SOURCE_ID", "IFS")
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setenv("NHMS_FORECAST_WARM_START_REQUIRED_FROM", "2026-06-27T00:00:00Z")
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", lambda: "repository-from-env")
    monkeypatch.setattr(scheduler_module, "_retry_service_from_env", lambda: "retry-service-from-env")
    monkeypatch.setattr(scheduler_module.StateManager, "from_env", staticmethod(lambda: "state-manager-from-env"))
    monkeypatch.setattr(scheduler_module, "ForecastOrchestrator", DefaultPathOrchestrator)
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert scheduler.orchestrator_factory is None
    assert result.status == "submitted"
    assert result.evidence["execution_boundary"] == "slurm_gateway_orchestration"
    assert constructed[0]["retry_service"] == "retry-service-from-env"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["slurm_preflight"]["status"] == "ready"
    assert len(constructed) == 1
    assert constructed[0]["repository"] == "repository-from-env"
    assert constructed[0]["state_manager"] == "state-manager-from-env"
    assert constructed[0]["config"].source_id == "gfs"
    assert constructed[0]["config"].workspace_root == roots["workspace_root"].resolve()
    assert constructed[0]["config"].object_store_root == roots["object_store_root"].resolve()
    assert constructed[0]["config"].require_forecast_warm_start is True
    assert constructed[0]["config"].forecast_warm_start_required_from == _dt("2026-06-27T00:00:00Z")
    assert constructed[0]["config"].terminal_stage == "forecast_state_save_qc"
    assert constructed[0]["config"].slurm_job_type_templates == dict(DEFAULT_JOB_TYPE_TEMPLATES)
    assert constructed[0]["config"].slurm_gateway_url == "http://slurm-gateway.internal:8000"
    assert calls[0]["source"] == "gfs"
    assert calls[0]["basins"][0]["output_uri"].startswith("s3://nhms/default/runs/")


def test_orchestrator_config_parses_strict_forecast_warm_start_env(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setenv("NHMS_FORECAST_WARM_START_REQUIRED_FROM", "2026-06-27T00:00:00Z")

    config = OrchestratorConfig.from_env()
    assert config.require_forecast_warm_start is True
    assert config.forecast_warm_start_required_from == _dt("2026-06-27T00:00:00Z")
    assert config.strict_forecast_warm_start_required_for(_dt("2026-06-27T00:00:00Z")) is True
    assert config.strict_forecast_warm_start_required_for(_dt("2026-06-26T12:00:00Z")) is False

    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    assert OrchestratorConfig.from_env().require_forecast_warm_start is False


def test_compute_env_example_enables_strict_forecast_warm_start() -> None:
    content = Path("infra/env/compute.example").read_text(encoding="utf-8")

    assert "NHMS_REQUIRE_FORECAST_WARM_START=true" in content
    assert "forbid cold starts" in content
    assert "prefer a compatible warm state" in content


@pytest.mark.parametrize(
    ("config_overrides", "expected_code"),
    [
        (
            {"slurm_job_type_templates": {"produce_forcing_array": "legacy_forcing.sbatch"}},
            "SLURM_PREFLIGHT_TEMPLATE_NOT_ALLOWLISTED",
        ),
        (
            {"slurm_job_type_templates": {"produce_forcing_array": "run_shud_forecast_array.sbatch"}},
            "SLURM_PREFLIGHT_TEMPLATE_MISMATCH",
        ),
        ({"slurm_env": {"NHMS_PROFILE": "prod;rm"}}, "SLURM_PREFLIGHT_ENV_VALUE_UNSAFE"),
        ({"slurm_env": {"NHMS_PROFILE": "x" * 1025}}, "SLURM_PREFLIGHT_ENV_VALUE_TOO_LONG"),
        ({"slurm_env": {"AWS_SECRET_ACCESS_KEY": "supersecret"}}, "SLURM_PREFLIGHT_ENV_SECRET_REJECTED"),
        ({"slurm_env": {"NHMS_MANIFEST_INDEX": "/tmp/evil.json"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"WORKSPACE_ROOT": "/tmp/evil-workspace"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"OBJECT_STORE_ROOT": "/tmp/evil-objects"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"NHMS_RUN_ID": "evil_run"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"NHMS_MODEL_ID": "evil_model"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"NHMS_CYCLE_ID": "evil_cycle"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"NHMS_JOB_TYPE": "evil_job"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"SHUD_THREADS": "1"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"OMP_NUM_THREADS": "1"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        ({"slurm_env": {"SLURM_ARRAY_TASK_ID": "99"}}, "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED"),
        (
            {"slurm_env": {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"}},
            "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
        ),
        (
            {"slurm_env": {"NHMS_PROFILE": "https://user:supersecret@example.com/profile"}},
            "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
        ),
        (
            {"slurm_env": {"OBJECT_STORE_PREFIX": "s3://bucket/prod?X-Amz-Signature=supersecret"}},
            "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED",
        ),
    ],
)
def test_slurm_preflight_rejects_unsafe_templates_and_environment_before_submission(
    tmp_path: Path,
    config_overrides: dict[str, Any],
    expected_code: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    templates = dict(DEFAULT_JOB_TYPE_TEMPLATES)
    templates.update(config_overrides.pop("slurm_job_type_templates", {}))
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=templates,
        **config_overrides,
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence_text = json.dumps(result.evidence)
    evidence = result.evidence["model_run_evidence"][0]
    assert result.status == "preflight_blocked"
    assert expected_code in {blocker["code"] for blocker in evidence["slurm_preflight"]["blockers"]}
    assert result.evidence["counts"]["submitted_count"] == 0
    assert "supersecret" not in evidence_text
    assert orchestrator.calls == []


def test_slurm_preflight_redacts_secret_url_values_in_evidence(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    secret_value = "s3://bucket/prod?token=supersecret"
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={"OBJECT_STORE_PREFIX": secret_value},
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence_text = json.dumps(result.evidence)
    environment_check = result.evidence["slurm_preflight"]["checks"]["environment"]

    assert result.status == "preflight_blocked"
    assert environment_check["sanitized"] == {"OBJECT_STORE_PREFIX": "[reserved]"}
    assert "supersecret" not in evidence_text
    assert secret_value not in evidence_text
    assert orchestrator.calls == []


def test_slurm_preflight_redacts_reserved_env_override_without_submission(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    reserved_value = "/tmp/evil-manifest-index.json"
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={"NHMS_MANIFEST_INDEX": reserved_value},
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    environment_check = result.evidence["slurm_preflight"]["checks"]["environment"]

    assert result.status == "preflight_blocked"
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert environment_check["sanitized"] == {"NHMS_MANIFEST_INDEX": "[reserved]"}
    assert reserved_value not in json.dumps(result.evidence)
    assert orchestrator.calls == []


def test_completed_duplicate_pipeline_is_skipped_before_submission(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeActiveRepository(active=False, completed=True)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "completed_duplicate_pipeline"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_active_slurm_job_skip_prevents_duplicate_submission(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeSlurmActiveRepository(
        active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    assert result.evidence["candidates"] == []
    assert skipped["reason"] == "active_slurm_job"
    assert skipped["active_slurm_jobs"][0]["slurm_job_id"] == "7777"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_cancel_active_slurm_calls_gateway_contract_without_replacement_submission(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    constructed: list[dict[str, Any]] = []
    cancel_calls: list[tuple[str, str]] = []
    reservation_seen_before_cancel: list[bool] = []

    class DefaultPathCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any, retry_service: Any = None) -> None:
            constructed.append(
                {
                    "config": config,
                    "repository": repository,
                    "state_manager": state_manager,
                    "retry_service": retry_service,
                }
            )

        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            reservation_seen_before_cancel.append(
                bool(list(roots["workspace_root"].glob("scheduler/evidence/*.pre_execution.json")))
            )
            cancel_calls.append((cycle_id, reason))
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "cancelled",
                    "replacement_submitted": False,
                }
            ]

    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms/default")
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://slurm-gateway.internal:8000")
    monkeypatch.setenv("FORECAST_SOURCE_ID", "IFS")
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", lambda: "repository-from-env")
    monkeypatch.setattr(scheduler_module, "ForecastOrchestrator", DefaultPathCancelOrchestrator)
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        cancel_active_slurm=True,
    )
    active_repository = FakeSlurmActiveRepository(
        active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert scheduler.orchestrator_factory is None
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert skipped["reason"] == "cancel_requested_active_slurm"
    assert skipped["replacement_submitted"] is False
    assert cancellation["status"] == "cancelled"
    assert cancellation["replacement_submitted"] is False
    assert cancellation["mutation_occurred"] is True
    assert cancellation["cancel_attempted"] is True
    assert cancellation["cancelled_jobs"][0]["slurm_job_id"] == "7777"
    assert cancellation["cancelled_jobs"][0]["replacement_submitted"] is False
    assert result.status == "slurm_cancelled"
    assert result.evidence["status"] == "slurm_cancelled"
    assert result.evidence["execution_boundary"] == "slurm_cancellation"
    assert result.evidence["counts"]["slurm_cancelled_count"] == 1
    assert result.evidence["counts"]["slurm_cancellation_blocked_count"] == 0
    assert result.evidence["slurm_cancellation_proof"]["cancel_called"] is True
    assert result.evidence["slurm_cancellation_proof"]["mutation_occurred"] is True
    assert result.evidence["slurm_cancellation_proof"]["protected_by_pre_execution_evidence"] is True
    assert result.evidence["no_mutation_proof"]["slurm_cancellation_called"] is True
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert reservation_seen_before_cancel == [True]
    assert result.evidence["evidence_pre_execution"]["status"] == "reserved"
    assert len(constructed) == 1
    assert constructed[0]["repository"] == "repository-from-env"
    assert constructed[0]["state_manager"] is None
    assert constructed[0]["config"].source_id == "gfs"
    assert constructed[0]["config"].object_store_root == roots["object_store_root"].resolve()
    assert constructed[0]["config"].slurm_gateway_url == "http://slurm-gateway.internal:8000"


def test_cancel_active_slurm_exception_after_attempt_uses_unknown_mutation_outcome(
    tmp_path: Path,
) -> None:
    class CancelError(Exception):
        error_code = "PIPELINE_EVENT_WRITE_FAILED"
        message = "Cancellation event write failed."

    class RaisingCancelOrchestrator(FakeProductionOrchestrator):
        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            self.cancel_calls.append((cycle_id, reason))
            raise CancelError("event failed after cancellation attempt")

    orchestrator = RaisingCancelOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, cancel_active_slurm=True),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(
            active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}],
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert orchestrator.cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert result.status == "slurm_cancellation_blocked"
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "slurm_cancellation_blocked"
        assert evidence["execution_boundary"] == "slurm_cancellation"
        assert evidence["evidence_pre_execution"]["status"] == "reserved"
        assert evidence["evidence_pre_execution"]["proof"] == (
            "scheduler_evidence_directory_write_before_production_mutation"
        )
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["slurm_cancelled_count"] == 0
        assert evidence["counts"]["slurm_cancellation_blocked_count"] == 1
        assert evidence["counts"]["slurm_cancellation_unknown_count"] == 1
        cancellation = evidence["slurm_cancellation_evidence"][0]
        assert cancellation["status"] == "failed"
        assert cancellation["cancel_attempted"] is True
        assert "mutation_occurred" not in cancellation
        assert cancellation["mutation_outcome"] == "unknown_after_attempt"
        assert cancellation["error_code"] == "PIPELINE_EVENT_WRITE_FAILED"
        proof = evidence["slurm_cancellation_proof"]
        assert proof["status"] == "slurm_cancellation_blocked"
        assert proof["cancel_called"] is True
        assert proof["protected_by_pre_execution_evidence"] is True
        assert proof["mutation_outcome"] == "unknown_after_attempt"
        assert proof["mutation_occurred"] == "unknown_after_attempt"
        assert proof["slurm_cancellation_proven_absent"] is False
        assert proof["pipeline_status_writes_proven_absent"] is False
        assert proof["pipeline_event_writes_proven_absent"] is False
        assert evidence["no_mutation_proof"]["slurm_cancellation_called"] is True
        assert evidence["no_mutation_proof"]["pipeline_status_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["pipeline_event_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["slurm_submit_called"] is False


def test_filtered_cancel_active_slurm_finds_cycle_level_array_job_with_different_stored_model(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)

    class FilteredCancelOrchestrator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.cancel_calls: list[tuple[str, str]] = []

        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            raise AssertionError("replacement orchestration must not be submitted while active Slurm job is cancelled")

        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            self.cancel_calls.append((cycle_id, reason))
            return [
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "8888",
                    "model_id": "model_a",
                    "stage": "forecast",
                    "status": "cancelled",
                    "replacement_submitted": False,
                }
            ]

    class FilteredCycleArrayRepository(FakeActiveRepository):
        def __init__(self) -> None:
            super().__init__(active=False, completed=False)
            self.queries: list[dict[str, Any]] = []

        def active_slurm_jobs(self, *, source_id: str, cycle_time: datetime, model_id: str) -> list[dict[str, Any]]:
            self.queries.append({"source_id": source_id, "cycle_time": cycle_time, "model_id": model_id})
            if model_id != "model_b":
                return []
            return [
                {
                    "job_id": "job_cycle_gfs_2026052106_forecast",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "job_type": "run_shud_forecast_array",
                    "slurm_job_id": "8888",
                    "model_id": "model_a",
                    "stage": "forecast",
                    "status": "running",
                }
            ]

    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        cancel_active_slurm=True,
        model_ids=("model_b",),
    )
    active_repository = FilteredCycleArrayRepository()
    orchestrator = FilteredCancelOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert active_repository.queries == [
        {"source_id": "gfs", "cycle_time": _dt("2026-05-21T06:00:00Z"), "model_id": "model_b"}
    ]
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert skipped["reason"] == "cancel_requested_active_slurm"
    assert skipped["active_slurm_jobs"][0]["model_id"] == "model_a"
    assert skipped["active_slurm_jobs"][0]["run_id"] == "cycle_gfs_2026052106"
    assert cancellation["cancelled_jobs"][0]["slurm_job_id"] == "8888"
    assert cancellation["replacement_submitted"] is False
    assert orchestrator.cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert orchestrator.calls == []


def test_cancel_active_slurm_runs_before_cycle_level_active_skip(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    cancel_calls: list[tuple[str, str]] = []
    constructed: list[dict[str, Any]] = []

    class DefaultPathCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any, retry_service: Any = None) -> None:
            constructed.append(
                {
                    "config": config,
                    "repository": repository,
                    "state_manager": state_manager,
                    "retry_service": retry_service,
                }
            )

        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            cancel_calls.append((cycle_id, reason))
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "cancelled",
                    "replacement_submitted": False,
                }
            ]

    class ActiveCycleAndSlurmRepository(FakeSlurmActiveRepository):
        def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
            del source_id, cycle_time
            return True

    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://slurm-gateway.internal:8000")
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", lambda: "repository-from-env")
    monkeypatch.setattr(scheduler_module, "_retry_service_from_env", lambda: "retry-service-from-env")
    monkeypatch.setattr(scheduler_module, "ForecastOrchestrator", DefaultPathCancelOrchestrator)
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        cancel_active_slurm=True,
    )
    active_repository = ActiveCycleAndSlurmRepository(
        active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "cancel_requested_active_slurm"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["slurm_cancellation_evidence"][0]["replacement_submitted"] is False
    assert cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert constructed[0]["retry_service"] == "retry-service-from-env"


def test_cancel_active_slurm_gap_blocks_top_level_cancelled_status(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)

    class GapCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any, retry_service: Any = None) -> None:
            del config, repository, state_manager, retry_service

        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            del reason
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "running",
                    "error_code": "JOB_ALREADY_TERMINAL",
                    "cancellation_proven": False,
                    "replacement_submitted": False,
                }
            ]

    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://slurm-gateway.internal:8000")
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", lambda: "repository-from-env")
    monkeypatch.setattr(scheduler_module, "_retry_service_from_env", lambda: "retry-service-from-env")
    monkeypatch.setattr(scheduler_module, "ForecastOrchestrator", GapCancelOrchestrator)
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        cancel_active_slurm=True,
    )
    active_repository = FakeSlurmActiveRepository(
        active_jobs=[{"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}]
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
    )

    result = scheduler.run_once()

    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert cancellation["status"] == "blocked"
    assert cancellation["error_code"] == "SLURM_CANCELLATION_GAP"
    assert cancellation["cancellation_proven"] is False
    assert cancellation["replacement_submitted"] is False
    assert cancellation["cancel_attempted"] is True
    assert cancellation["mutation_occurred"] is False
    assert cancellation["pipeline_event_write"] is True
    assert "pipeline_status_write" not in cancellation
    assert result.status == "slurm_cancellation_blocked"
    assert result.evidence["status"] == "slurm_cancellation_blocked"
    assert result.evidence["execution_boundary"] == "slurm_cancellation"
    assert result.evidence["counts"]["slurm_cancelled_count"] == 0
    assert result.evidence["counts"]["slurm_cancellation_blocked_count"] == 1
    assert result.evidence["slurm_cancellation_proof"]["cancel_called"] is True
    assert result.evidence["slurm_cancellation_proof"]["mutation_occurred"] is True
    assert result.evidence["slurm_cancellation_proof"]["cancelled_job_count"] == 0
    assert result.evidence["slurm_cancellation_proof"]["pipeline_status_write_count"] == 0
    assert result.evidence["slurm_cancellation_proof"]["pipeline_event_write_count"] == 1
    assert result.evidence["slurm_cancellation_proof"]["pipeline_status_writes_proven_absent"] is True
    assert result.evidence["slurm_cancellation_proof"]["pipeline_event_writes_proven_absent"] is False
    assert result.evidence["slurm_cancellation_proof"]["protected_by_pre_execution_evidence"] is True
    assert result.evidence["no_mutation_proof"]["slurm_cancellation_called"] is True
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert result.evidence["no_mutation_proof"]["pipeline_status_writes"] is False
    assert result.evidence["no_mutation_proof"]["pipeline_event_writes"] is True


def test_active_cycle_orchestration_without_hydro_state_skips_all_candidates(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeActiveCycleOrchestrationRepository()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert [item["reason"] for item in result.evidence["skipped_candidates"]] == [
        "active_duplicate_pipeline",
        "active_duplicate_pipeline",
    ]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert active_repository.orchestration_checks == [("gfs", _dt("2026-05-21T06:00:00Z"))]
    assert orchestrator.calls == []


def test_active_cycle_orchestration_with_model_state_only_skips_active_model(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)

    class ActiveCyclePerModelStateRepository(PerModelCandidateStateRepository):
        def __init__(self) -> None:
            super().__init__(
                {
                    "model_a": {
                        "pipeline_status": "pending",
                        "pipeline_jobs": [
                            {
                                "job_id": "job_model_a_pending",
                                "run_id": "fcst_gfs_2026052106_model_a",
                                "status": "pending",
                                "stage": "forcing",
                            }
                        ],
                    },
                    "model_b": None,
                }
            )
            self.orchestration_checks: list[tuple[str, datetime]] = []
            self.active_pipeline_checks: list[tuple[str, datetime, str]] = []

        def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
            self.orchestration_checks.append((source_id, cycle_time))
            return True

        def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            self.active_pipeline_checks.append((source_id, cycle_time, model_id))
            return model_id == "model_a"

    active_repository = ActiveCyclePerModelStateRepository()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert [item["model_id"] for item in result.evidence["skipped_candidates"]] == ["model_a"]
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_duplicate_pipeline"
    assert [item["model_id"] for item in result.evidence["candidates"]] == ["model_b"]
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls[0]["basins"][0]["model_id"] == "model_b"
    assert active_repository.orchestration_checks == [("gfs", _dt("2026-05-21T06:00:00Z"))]
    assert active_repository.active_pipeline_checks == [("gfs", _dt("2026-05-21T06:00:00Z"), "model_b")]


@pytest.mark.parametrize("hydro_status", ["succeeded", "parsed", "published", "published", "complete"])
def test_completed_hydro_state_is_skipped_as_completed_not_active(
    tmp_path: Path,
    hydro_status: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeHydroStateRepository(hydro_status=hydro_status)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "completed_duplicate_pipeline"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


@pytest.mark.parametrize("hydro_status", ["succeeded", "parsed", "published", "published"])
def test_candidate_state_terminal_hydro_success_records_durable_skip_reason(
    tmp_path: Path,
    hydro_status: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": hydro_status,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    state = skipped["state_evidence"]
    assert result.evidence["candidates"] == []
    assert skipped["reason"] == "terminal_hydro_success"
    assert state["decision"] == "skip_terminal"
    assert state["durable_hydro_status"] == hydro_status
    assert state["native_shud_resubmitted"] is False
    assert state["parse_resubmitted"] is False
    assert state["publish_resubmitted"] is False
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_terminal_candidate_state_is_recorded_before_not_ready_canonical_gate(tmp_path: Path) -> None:
    class NotReadyReadinessProvider:
        def __init__(self) -> None:
            self.calls = 0

        def canonical_readiness(self, **_kwargs: Any) -> Mapping[str, Any]:
            self.calls += 1
            return {"status": "canonical_unavailable", "ready": False, "reason": "canonical_missing"}

    provider = NotReadyReadinessProvider()
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "succeeded",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
        }
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        canonical_readiness_provider=provider,
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    assert provider.calls == 0
    assert result.evidence["blocked_candidates"] == []
    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "terminal_hydro_success"
    assert skipped["state_evidence"]["decision"] == "skip_terminal"
    assert "canonical_readiness" not in skipped["state_evidence"]


def test_active_slurm_state_is_recorded_before_not_ready_canonical_gate(tmp_path: Path) -> None:
    class NotReadyReadinessProvider:
        def __init__(self) -> None:
            self.calls = 0

        def canonical_readiness(self, **_kwargs: Any) -> Mapping[str, Any]:
            self.calls += 1
            return {"status": "canonical_unavailable", "ready": False, "reason": "canonical_missing"}

    provider = NotReadyReadinessProvider()
    cycle_time = _dt("2026-05-21T06:00:00Z")
    active_repository = CandidateAndActiveRepository(
        {
            "pipeline_jobs": [
                {
                    "job_id": "job_active",
                    "slurm_job_id": "7777",
                    "status": "running",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                }
            ]
        },
        [{"slurm_job_id": "7777", "status": "running", "model_id": "model_a"}],
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        canonical_readiness_provider=provider,
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    assert provider.calls == 0
    assert result.evidence["candidates"] == []
    assert result.evidence["blocked_candidates"] == []
    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "active_slurm_job"
    assert skipped["active_slurm_jobs"][0]["slurm_job_id"] == "7777"
    assert skipped["state_evidence"]["replacement_submitted"] is False
    assert active_repository.queries[0]["cycle_time"] == cycle_time


def test_source_object_identity_is_reused_across_models_for_scheduler_pass(tmp_path: Path) -> None:
    class CountingAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__("gfs", [("2026-05-21T06:00:00Z", True)])
            self.identity_calls = 0

        def source_object_identity(self, *_args: Any) -> dict[str, Any]:
            self.identity_calls += 1
            return {"source": "gfs", "object": "shared", "call": self.identity_calls}

    class ReadyProvider:
        def __init__(self) -> None:
            self.identities: list[dict[str, Any]] = []

        def canonical_readiness(self, **kwargs: Any) -> Mapping[str, Any]:
            self.identities.append(dict(kwargs["source_object_identity"]))
            return {"status": "canonical_ready", "ready": True}

    adapter = CountingAdapter()
    provider = ReadyProvider()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=True),
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": adapter},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=provider,
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["candidate_count"] == 2
    assert adapter.identity_calls == 1
    assert provider.identities == [
        {"source": "gfs", "object": "shared", "call": 1},
        {"source": "gfs", "object": "shared", "call": 1},
    ]


def test_candidate_state_parse_failure_after_shud_success_restarts_at_parse_without_native_rerun(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "failed",
            "durable_shud_output_exists": True,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
            "pipeline_jobs": [
                {
                    "job_id": "job_forecast",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "succeeded",
                    "stage": "forecast",
                    "slurm_job_id": "7001",
                },
                {
                    "job_id": "job_parse",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "failed",
                    "stage": "parse",
                    "error_code": "FAILED_PARSE",
                    "retry_count": 1,
                },
            ],
            "failed_stage": "parse",
            "error_code": "FAILED_PARSE",
            "retry_count": 1,
            "retry_limit": 3,
            "retryable": True,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    candidate = result.evidence["candidates"][0]
    state = candidate["state_evidence"]
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert state["decision"] == "retry_downstream"
    assert state["restart_stage"] == "parse"
    assert state["durable_shud_output_reused"] is True
    assert state["native_shud_resubmitted"] is False
    assert state["failure"]["classifier"] == "parse_failure"
    assert submitted_basin["restart_stage"] == "parse"
    assert submitted_basin["durable_shud_output_reused"] is True
    assert submitted_basin["native_shud_resubmitted"] is False
    assert result.evidence["counts"]["submitted_count"] == 1


@pytest.mark.parametrize(
    ("stage", "error_code", "expected_classifier"),
    [
        ("state_save_qc", "Q_DOWN_DISPLAY_NOT_READY", "unknown_failure"),
    ],
)
def test_db_shaped_downstream_failure_after_shud_success_restarts_without_retryable_flag(
    tmp_path: Path,
    stage: str,
    error_code: str,
    expected_classifier: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "succeeded",
            "durable_shud_output_exists": True,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
            "pipeline_status": "failed",
            "failed_stage": stage,
            "error_code": error_code,
            "retry_count": 1,
            "retry_limit": 3,
            "pipeline_jobs": [
                {
                    "job_id": f"job_{stage}",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "failed",
                    "stage": stage,
                    "error_code": error_code,
                    "retry_count": 1,
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert state["decision"] == "retry_downstream"
    assert state["restart_stage"] == stage
    assert state["failure"]["classifier"] == expected_classifier
    assert state["retry_policy"]["automatic_retry_allowed"] is True
    assert "retryable" not in active_repository.state
    assert submitted_basin["restart_stage"] == stage
    assert submitted_basin["native_shud_resubmitted"] is False
    assert result.evidence["counts"]["submitted_count"] == 1


def test_newer_terminal_hydro_success_skips_older_failed_parse_job(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "published",
            "hydro_run": {
                "run_id": "fcst_gfs_2026052106_model_a",
                "status": "published",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
                "updated_at": "2026-05-21T07:00:00Z",
            },
            "pipeline_status": "failed",
            "failed_stage": "parse",
            "error_code": "FAILED_PARSE",
            "pipeline_jobs": [
                {
                    "job_id": "job_parse_old",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "failed",
                    "stage": "parse",
                    "error_code": "FAILED_PARSE",
                    "updated_at": "2026-05-21T06:00:00Z",
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "terminal_hydro_success"
    assert skipped["state_evidence"]["durable_hydro_status"] == "published"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


@pytest.mark.parametrize("marker_created_at", [None, "2026-05-21T06:00:00Z", "2026-05-21T07:00:00Z"])
def test_terminal_pipeline_success_is_not_overridden_by_manual_retry_marker(
    tmp_path: Path,
    marker_created_at: str | None,
) -> None:
    events: list[dict[str, Any]] = []
    if marker_created_at is not None:
        events.append(
            {
                "event_id": 10,
                "event_type": "retry",
                "created_at": marker_created_at,
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 3,
                    "previous_job_id": "job_failed",
                },
            }
        )
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "published",
            "pipeline_jobs": [
                {
                    "job_id": "job_failed",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "status": "failed",
                    "stage": "parse",
                    "error_code": "FAILED_PARSE",
                    "updated_at": "2026-05-21T05:50:00Z",
                },
                {
                    "job_id": "job_publish_success",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "status": "published",
                    "stage": "publish",
                    "updated_at": "2026-05-21T06:30:00Z",
                },
            ],
            "pipeline_events": events,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "terminal_pipeline_success"
    assert skipped["state_evidence"]["decision"] == "skip_terminal"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_intermediate_pipeline_success_does_not_skip_terminal_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "succeeded",
            "pipeline_jobs": [
                {
                    "job_id": "job_forcing_success",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "status": "succeeded",
                    "stage": "forcing",
                    "updated_at": "2026-05-21T06:30:00Z",
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls


def test_terminal_hydro_success_is_not_overridden_by_manual_retry_marker(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "published",
            "hydro_run": {
                "run_id": "fcst_gfs_2026052106_model_a",
                "status": "published",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
                "updated_at": "2026-05-21T06:30:00Z",
            },
            "pipeline_events": [
                {
                    "event_id": 20,
                    "event_type": "retry",
                    "created_at": "2026-05-21T07:00:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 2,
                        "previous_job_id": "job_old_failed",
                    },
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == "terminal_hydro_success"
    assert skipped["state_evidence"]["durable_hydro_status"] == "published"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_mixed_restart_and_fresh_candidates_are_executed_in_restart_compatible_cohorts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = PerModelCandidateStateRepository(
        {
            "model_a": {
                "hydro_status": "succeeded",
                "durable_shud_output_exists": True,
                "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
                "pipeline_status": "failed",
                "failed_stage": "parse",
                "error_code": "FAILED_PARSE",
                "retry_count": 1,
                "retry_limit": 3,
            },
            "model_b": None,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 2
    assert len(orchestrator.calls) == 2
    calls_by_model = {call["basins"][0]["model_id"]: call for call in orchestrator.calls}
    assert calls_by_model["model_a"]["basins"][0]["restart_stage"] == "parse"
    assert calls_by_model["model_a"]["basins"][0]["orchestration_run_id"].endswith("_parse_model_a")
    assert "restart_stage" not in calls_by_model["model_b"]["basins"][0]
    assert calls_by_model["model_b"]["basins"][0]["orchestration_run_id"].endswith("_full_model_b")


def test_multi_candidate_restart_cohorts_are_candidate_scoped_and_second_scan_sees_active_truth(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    cycle_time = _dt("2026-05-21T06:00:00Z")
    restart_states = {
        "model_a": {
            "hydro_status": "succeeded",
            "durable_shud_output_exists": True,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
            "pipeline_status": "failed",
            "failed_stage": "parse",
            "error_code": "FAILED_PARSE",
            "retry_count": 1,
            "retry_limit": 3,
        },
        "model_b": {
            "hydro_status": "succeeded",
            "durable_shud_output_exists": True,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_b/output/",
            "pipeline_status": "failed",
            "failed_stage": "parse",
            "error_code": "FAILED_PARSE",
            "retry_count": 1,
            "retry_limit": 3,
        },
    }
    active_states = {
        model_id: {
            "pipeline_status": "running",
            "pipeline_jobs": [
                {
                    "job_id": f"job_cycle_gfs_2026052106_parse_{model_id}_parse",
                    "run_id": f"cycle_gfs_2026052106_parse_{model_id}",
                    "cycle_id": "gfs_2026052106",
                    "model_id": model_id,
                    "status": "running",
                    "stage": "parse",
                    "slurm_job_id": f"slurm_{model_id}",
                    "updated_at": "2026-05-21T06:20:00Z",
                }
            ],
        }
        for model_id in ("model_a", "model_b")
    }
    active_repository = SequencedPerModelCandidateStateRepository(
        first_states=restart_states,
        second_states={},
    )

    class PersistingRestartOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            for basin in basins:
                model_id = str(basin["model_id"])
                active_repository.second_states[model_id] = active_states[model_id]
                active_repository.second_states[model_id]["pipeline_jobs"][0]["run_id"] = str(
                    basin["orchestration_run_id"]
                )
                active_repository.second_states[model_id]["pipeline_jobs"][0]["model_id"] = model_id
            return super().orchestrate_cycle(source, cycle_time, basins)

    orchestrator = PersistingRestartOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [(cycle_time.isoformat(), True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    first = scheduler.run_once()
    active_repository.use_second_scan = True
    second = scheduler.run_once()

    assert first.evidence["counts"]["submitted_count"] == 2
    assert len(orchestrator.calls) == 2
    first_run_ids = [call["basins"][0]["orchestration_run_id"] for call in orchestrator.calls]
    assert first_run_ids == [
        "cycle_gfs_2026052106_parse_model_a",
        "cycle_gfs_2026052106_parse_model_b",
    ]
    assert all(call["basins"][0]["restart_stage"] == "parse" for call in orchestrator.calls)
    assert second.evidence["counts"]["submitted_count"] == 0
    assert [item["reason"] for item in second.evidence["skipped_candidates"]] == [
        "active_slurm_job",
        "active_slurm_job",
    ]
    assert len(orchestrator.calls) == 2


def test_sibling_active_restart_does_not_block_downstream_retry_candidate(tmp_path: Path) -> None:
    class SiblingActiveRestartRepository(PerModelCandidateStateRepository):
        def __init__(self) -> None:
            super().__init__(
                {
                    "model_a": {
                        "pipeline_status": "running",
                        "pipeline_jobs": [
                            {
                                "job_id": "job_cycle_gfs_2026052106_parse_model_a",
                                "run_id": "cycle_gfs_2026052106_parse_model_a",
                                "cycle_id": "gfs_2026052106",
                                "model_id": "model_a",
                                "status": "running",
                                "stage": "parse",
                                "slurm_job_id": "slurm_model_a",
                            }
                        ],
                    },
                    "model_b": {
                        "hydro_status": "succeeded",
                        "durable_shud_output_exists": True,
                        "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_b/output/",
                        "pipeline_status": "failed",
                        "failed_stage": "parse",
                        "error_code": "FAILED_PARSE",
                        "retry_count": 1,
                        "retry_limit": 3,
                    },
                }
            )
            self.orchestration_checks: list[tuple[str, datetime]] = []

        def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
            self.orchestration_checks.append((source_id, cycle_time))
            return True

    repository = SiblingActiveRestartRepository()
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["skipped_candidates"][0]["model_id"] == "model_a"
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_slurm_job"
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["model_id"] == "model_b"
    assert submitted_basin["restart_stage"] == "parse"
    assert submitted_basin["orchestration_run_id"] == "cycle_gfs_2026052106_parse_model_b"


def test_candidate_state_source_unavailable_is_retryable_enum_safe_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), sources=("IFS",))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"IFS": FakeAdapter("IFS", [("2026-05-21T06:00:00Z", False)])},
    )

    result = scheduler.run_once()

    state = result.evidence["blocked_candidates"][0]["state_evidence"]
    assert state["failure"]["classifier"] == "source_unavailable"
    assert state["failure"]["retryable"] is True
    assert state["storage"]["met_forecast_cycle_status_written"] is None
    assert state["retry_policy"]["unsupported_db_enum_written"] is False
    assert result.evidence["source_cycles"][0]["db_cycle_status_written"] is None


def test_candidate_state_ifs_probe_failed_stays_retryable_and_redacted(tmp_path: Path) -> None:
    signed_probe = (
        "ecmwf-opendata://user:password@aws/ifs/2026060800/"
        "ifs.t00z.f000.2t.grib2?token=super-secret&X-Amz-Signature=secret-signature"
    )
    attempted_sources = [
        {
            "source": source,
            "uri": signed_probe.replace("@aws/", f"@{source}/"),
            "status": "probe_failed",
            "error_class": "NetworkDownloadError",
            "error_message": (
                f"DNS failed for {source}: {signed_probe.replace('@aws/', f'@{source}/')} access_key=secret-access-key"
            ),
        }
        for source in ("aws", "azure", "google", "ecmwf")
    ]
    config = _config(tmp_path, now=_dt("2026-06-08T06:00:00Z"), sources=("IFS",))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "IFS": FakeAdapter(
                "IFS",
                [
                    (
                        "2026-06-08T00:00:00Z",
                        False,
                        {
                            "status": "probe_failed",
                            "reason": "source_cycle_probe_failed",
                            "classifier": "network_error",
                            "retryable": True,
                            "probe_uri": signed_probe,
                            "evidence": {
                                "probe": {"uri": signed_probe, "source": "aws"},
                                "attempted_sources": attempted_sources,
                            },
                        },
                    )
                ],
            )
        },
    )

    result = scheduler.run_once()

    source_cycle = result.evidence["source_cycles"][0]
    assert source_cycle["status"] == "probe_failed"
    assert source_cycle["reason"] == "source_cycle_probe_failed"
    assert source_cycle["classifier"] == "network_error"
    assert source_cycle["retryable"] is True
    assert source_cycle["cycle_status_candidate"] == "probe_failed"
    assert source_cycle["db_cycle_status_written"] is None
    attempts = source_cycle["discovery_evidence"]["attempted_sources"]
    assert [attempt["source"] for attempt in attempts] == ["aws", "azure", "google", "ecmwf"]
    assert {attempt["status"] for attempt in attempts} == {"probe_failed"}
    assert {attempt["error_class"] for attempt in attempts} == {"NetworkDownloadError"}
    assert all("DNS failed" in attempt["error_message"] for attempt in attempts)

    blocked = result.evidence["blocked_candidates"][0]
    state = blocked["state_evidence"]
    assert blocked["reason"] == "source_cycle_probe_failed"
    assert state["decision"] == "blocked_retryable"
    assert state["failure"]["classifier"] == "network_error"
    assert state["failure"]["status"] == "probe_failed"
    assert state["failure"]["reason_code"] == "SOURCE_CYCLE_PROBE_FAILED"
    assert state["failure"]["retryable"] is True
    assert state["failure"]["permanent"] is False
    assert state["retry_policy"]["automatic_retry_allowed"] is True
    assert state["storage"]["met_forecast_cycle_status_written"] is None
    assert state["retry_policy"]["unsupported_db_enum_written"] is False
    rendered = json.dumps(result.evidence)
    assert "super-secret" not in rendered
    assert "secret-signature" not in rendered
    assert "password" not in rendered
    assert "secret-access-key" not in rendered


def test_probe_failed_cycle_does_not_consume_source_budget_or_rewrite_as_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-06-08T12:00:00Z"), sources=("IFS",), max_cycles_per_source=1)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "IFS": FakeAdapter(
                "IFS",
                [
                    (
                        "2026-06-08T06:00:00Z",
                        False,
                        {
                            "status": "probe_failed",
                            "reason": "source_cycle_probe_failed",
                            "classifier": "network_error",
                            "retryable": True,
                            "evidence": {
                                "attempted_sources": [
                                    {
                                        "source": "aws",
                                        "uri": "ecmwf-opendata://aws/ifs/2026060806/ifs.t06z.f000.2t.grib2",
                                        "status": "probe_failed",
                                        "error_class": "NetworkDownloadError",
                                        "error_message": "temporary failure in name resolution",
                                    }
                                ]
                            },
                        },
                    ),
                    ("2026-06-08T00:00:00Z", True),
                ],
            )
        },
    )

    result = scheduler.run_once()

    selected = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "ifs_2026060800")
    deferred = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "ifs_2026060806")
    assert selected["cycle_id"] == "ifs_2026060800"
    assert result.evidence["candidates"][0]["cycle_id"] == "ifs_2026060800"
    assert deferred["status"] == "probe_failed"
    assert deferred["reason"] == "source_cycle_probe_failed"
    assert deferred["classifier"] == "network_error"
    assert deferred["selection_status"] == "not_selected"
    assert deferred["selection_reason"] == "source_cycle_probe_failed_does_not_consume_source_budget"


def test_rate_limited_cycle_does_not_consume_source_budget_or_rewrite_as_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-06-08T12:00:00Z"), sources=("IFS",), max_cycles_per_source=1)
    attempted_sources = [
        {
            "source": "aws",
            "uri": "ecmwf-opendata://aws/ifs/2026060806/ifs.t06z.f000.2t.grib2",
            "status": "rate_limited",
            "error_class": "RateLimitedSourceError",
            "error_message": "source mirror returned 429 Too Many Requests",
        }
    ]
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "IFS": FakeAdapter(
                "IFS",
                [
                    (
                        "2026-06-08T06:00:00Z",
                        False,
                        {
                            "status": "rate_limited",
                            "reason": "source_cycle_rate_limited",
                            "classifier": "rate_limited",
                            "retryable": True,
                            "evidence": {
                                "attempted_sources": attempted_sources,
                                "attempted_source_count": 1,
                                "emitted_attempt_count": 1,
                                "attempted_source_limit": 8,
                                "omitted_attempt_count": 0,
                            },
                        },
                    ),
                    ("2026-06-08T00:00:00Z", True),
                ],
            )
        },
    )

    result = scheduler.run_once()

    selected = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "ifs_2026060800")
    deferred = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "ifs_2026060806")
    assert selected["cycle_id"] == "ifs_2026060800"
    assert result.evidence["candidates"][0]["cycle_id"] == "ifs_2026060800"
    assert deferred["status"] == "rate_limited"
    assert deferred["reason"] == "source_cycle_rate_limited"
    assert deferred["classifier"] == "rate_limited"
    assert deferred["retryable"] is True
    assert deferred["cycle_status_candidate"] == "rate_limited"
    assert deferred["db_cycle_status_written"] is None
    assert deferred["selection_status"] == "not_selected"
    assert deferred["selection_reason"] == "source_cycle_rate_limited_does_not_consume_source_budget"
    assert deferred["discovery_evidence"]["attempted_sources"] == attempted_sources


@pytest.mark.parametrize("error_code", ["NODE_FAILURE", "OUT_OF_MEMORY"])
def test_candidate_state_transient_runtime_failure_retries_failed_scope_with_reuse_evidence(
    tmp_path: Path,
    error_code: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": error_code,
            "retry_count": 1,
            "retry_limit": 3,
            "array_task_id": 2,
            "successful_sibling_outputs_reused": True,
            "durable_shud_output_exists": False,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_failed"
    assert state["failure"]["classifier"] == "transient_slurm_runtime"
    assert state["failure"]["retryable"] is True
    assert state["task_identity"]["array_task_id"] == 2
    assert state["reuse"]["successful_sibling_outputs_reused"] is True
    assert result.evidence["counts"]["submitted_count"] == 1


def test_cold_start_quarantined_failure_recomputes_from_forecast(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "failed",
            "pipeline_status": "failed",
            "failed_stage": "state_save_qc",
            "error_code": "COLD_START_QUARANTINED",
            "error_message": "Cold-start products were quarantined after warm-start policy enforcement.",
            "retry_count": 3,
            "retry_limit": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls
    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_failed"
    assert state["restart_stage"] == "forecast"
    assert state["failure"]["reason_code"] == "COLD_START_QUARANTINED"
    assert state["failure"]["classifier"] == "cold_start_quarantine_recompute"
    assert state["failure"]["retryable"] is True
    assert state["failure"]["permanent"] is False
    assert state["retry_policy"]["automatic_retry_allowed"] is True
    assert state["retry_policy"]["manual_retry_required"] is False


def test_warm_start_checkpoint_repair_does_not_auto_retry_in_production(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "failed",
            "error_code": "WARM_START_CHECKPOINT_RETRY",
            "error_message": "Checkpoint capture policy changed; manual repair may rerun a full forecast long run.",
            "retry_count": 0,
            "retry_limit": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    state = blocked["state_evidence"]
    assert orchestrator.calls == []
    assert result.evidence["candidates"] == []
    assert blocked["reason"] == "permanent_failure_guard"
    assert state["decision"] == "permanent_failure"
    assert state["failure"]["classifier"] == "warm_start_checkpoint_repair"
    assert state["failure"]["retryable"] is False
    assert state["failure"]["permanent"] is True
    assert result.evidence["counts"]["submitted_count"] == 0


@pytest.mark.parametrize(
    ("error_code", "expected_reason"),
    [
        ("INVALID_MANIFEST", "permanent_failure_guard"),
        ("POLICY_BLOCKED", "policy_blocked"),
        ("SLURM_TIMEOUT", "retry_limit_exhausted"),
        ("OUT_OF_MEMORY", "retry_limit_exhausted"),
    ],
)
def test_candidate_state_permanent_or_exhausted_failure_blocks_auto_retry(
    tmp_path: Path,
    error_code: str,
    expected_reason: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": error_code,
            "retry_count": 3,
            "retry_limit": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    state = blocked["state_evidence"]
    assert result.evidence["candidates"] == []
    assert blocked["reason"] == expected_reason
    assert state["decision"] == "permanent_failure"
    assert state["retry_policy"]["automatic_retry_allowed"] is False
    assert state["manual_retry_required"] is True
    assert state["failure"]["permanent"] is True
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_model_package_refresh_allows_automatic_retry_after_retry_limit_exhausted(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    model = _model(
        "model_a",
        "basin_a",
        resource_profile={
            "runnable": True,
            "memory_gb": 8,
            "display_capabilities": {"tiles": True},
            "package_checksum": "new-package-sha",
        },
    )
    model["model_package_uri"] = "s3://nhms/models/model_a/new-package/package/"
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "SLURM_TIMEOUT",
            "retry_count": 3,
            "retry_limit": 3,
            "run_manifest_model_package": {
                "source": "run_manifest",
                "status": "loaded",
                "model_package_uri_sha256": hashlib.sha256(
                    b"s3://nhms/models/model_a/old-package/package/"
                ).hexdigest(),
                "model_package_checksum": "old-package-sha",
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_after_model_package_refresh"
    assert state["retry_policy"]["automatic_retry_allowed"] is True
    assert state["retry_policy"]["manual_retry_required"] is False
    assert state["model_package_refresh"]["changed_fields"] == ["model_package_checksum", "model_package_uri"]
    assert orchestrator.calls[0]["basins"][0]["model_package_uri"] == "s3://nhms/models/model_a/new-package/package/"


def test_same_model_package_still_blocks_after_retry_limit_exhausted(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    package_uri = "s3://nhms/models/model_a/package/"
    package_checksum = "same-package-sha"
    model = _model(
        "model_a",
        "basin_a",
        resource_profile={
            "runnable": True,
            "memory_gb": 8,
            "display_capabilities": {"tiles": True},
            "package_checksum": package_checksum,
        },
    )
    model["model_package_uri"] = package_uri
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "SLURM_TIMEOUT",
            "retry_count": 3,
            "retry_limit": 3,
            "run_manifest_model_package": {
                "source": "run_manifest",
                "status": "loaded",
                "model_package_uri_sha256": hashlib.sha256(package_uri.encode("utf-8")).hexdigest(),
                "model_package_checksum": package_checksum,
            },
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "retry_limit_exhausted"
    assert blocked["state_evidence"]["decision"] == "permanent_failure"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_missing_raw_manifest_after_successful_download_repairs_from_full_chain(tmp_path: Path) -> None:
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir()
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), sources=("IFS",), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "forecast_cycle": {
                "cycle_id": "ifs_2026052106",
                "source_id": "IFS",
                "cycle_time": _dt("2026-05-21T06:00:00Z"),
                "status": "discovered",
                "manifest_uri": "s3://nhms/raw/IFS/2026052106/manifest.json",
            },
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_ifs_2026052106_download",
                    "run_id": "cycle_ifs_2026052106",
                    "cycle_id": "ifs_2026052106",
                    "status": "succeeded",
                    "stage": "download",
                    "job_type": "download_source_cycle",
                    "updated_at": "2026-05-21T06:02:00Z",
                },
                {
                    "job_id": "job_cycle_ifs_2026052106_convert",
                    "run_id": "cycle_ifs_2026052106",
                    "cycle_id": "ifs_2026052106",
                    "status": "failed",
                    "stage": "convert",
                    "job_type": "convert_canonical",
                    "error_code": "INVALID_MANIFEST",
                    "retry_count": 3,
                    "updated_at": "2026-05-21T06:03:00Z",
                },
            ],
            "pipeline_status": "failed",
            "failed_stage": "convert",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "retry_limit": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(
            [
                _model(
                    "model_a",
                    "basin_a",
                    resource_profile={
                        "runnable": True,
                        "memory_gb": 8,
                        "display_capabilities": {"tiles": True},
                        "object_store_root": str(object_store_root),
                    },
                )
            ]
        ),
        adapters={"IFS": FakeAdapter("IFS", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_failed"
    assert state["reason"] == "repair_missing_raw_manifest"
    assert state["restart_from_stage"] == "download"
    assert state["fresh_ingestion"] == {"required": True, "mode": "full_chain"}
    assert state["raw_manifest_repair"]["manifest_exists"] is False
    assert state["failure"]["classifier"] == "recoverable_missing_raw_manifest"
    assert state["retry_policy"]["manual_retry_required"] is False
    assert orchestrator.calls


def test_repaired_raw_manifest_allows_stale_downstream_failure_retry(tmp_path: Path) -> None:
    object_store_root = tmp_path / "object-store"
    LocalObjectStore(str(object_store_root), object_store_prefix="s3://nhms").write_bytes_atomic(
        "raw/IFS/2026052106/manifest.json",
        b'{"source_id":"IFS"}',
    )
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), sources=("IFS",), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "forecast_cycle": {
                "cycle_id": "ifs_2026052106",
                "source_id": "IFS",
                "cycle_time": "2026-05-21T06:00:00Z",
                "status": "failed_convert",
                "manifest_uri": "s3://nhms/raw/IFS/2026052106/manifest.json",
                "error_code": "SLURM_JOB_FAILED",
                "error_message": "Stage convert ended with failed.",
            },
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_ifs_2026052106_download",
                    "run_id": "cycle_ifs_2026052106",
                    "cycle_id": "ifs_2026052106",
                    "status": "succeeded",
                    "stage": "download",
                    "job_type": "download_source_cycle",
                    "finished_at": "2026-05-21T06:02:00Z",
                },
                {
                    "job_id": "job_cycle_ifs_2026052106_convert",
                    "run_id": "cycle_ifs_2026052106",
                    "cycle_id": "ifs_2026052106",
                    "status": "failed",
                    "stage": "convert",
                    "job_type": "convert_canonical",
                    "error_code": "SLURM_JOB_FAILED",
                    "finished_at": "2026-05-21T06:03:00Z",
                },
                {
                    "job_id": "job_cycle_ifs_2026052106_download_retry_1",
                    "run_id": "cycle_ifs_2026052106",
                    "cycle_id": "ifs_2026052106",
                    "status": "succeeded",
                    "stage": "download",
                    "job_type": "download_source_cycle",
                    "finished_at": "2026-05-21T06:20:00Z",
                },
            ],
            "pipeline_status": "failed",
            "failed_stage": "convert",
            "error_code": "SLURM_JOB_FAILED",
            "retry_count": 0,
            "retry_limit": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(
            [
                _model(
                    "model_a",
                    "basin_a",
                    resource_profile={
                        "runnable": True,
                        "memory_gb": 8,
                        "display_capabilities": {"tiles": True},
                        "object_store_root": str(object_store_root),
                    },
                )
            ]
        ),
        adapters={"IFS": FakeAdapter("IFS", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_failed"
    assert state["reason"] == "retry_downstream_after_raw_repair"
    assert state["restart_from_stage"] == "download"
    assert state["raw_manifest_repair"]["manifest_exists"] is True
    assert state["failure"]["classifier"] == "recoverable_downstream_after_raw_repair"
    assert state["retry_policy"]["manual_retry_required"] is False
    assert orchestrator.calls


def test_candidate_state_manual_retry_marker_allows_blocked_candidate_and_preserves_prior_reason(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "forecast",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "retry_limit": 3,
            "manual_retry": {"marker": True, "requested_by": "operator"},
            "prior_failure_reason": "INVALID_MANIFEST",
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert result.evidence["blocked_candidates"] == []
    assert state["decision"] == "manual_retry"
    assert state["manual_retry"]["marker"] is True
    assert state["manual_retry"]["allowed"] is True
    assert state["prior_failure_reason"] == "INVALID_MANIFEST"
    assert state["failure"]["previous_attempt"] == 3
    assert state["failure"]["new_attempt"] == 4
    assert state["failure"]["manual_retry_marker"] is True
    assert state["retry_policy"]["previous_attempt"] == 3
    assert state["retry_policy"]["new_attempt"] == 4
    assert state["retry_policy"]["attempt"] == 4
    assert result.evidence["counts"]["submitted_count"] == 1


def test_db_shaped_transient_failure_uses_scheduler_retry_limit_without_state_retry_limit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, retry_limit=3)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "SLURM_TIMEOUT",
            "retry_count": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    state = blocked["state_evidence"]
    assert blocked["reason"] == "retry_limit_exhausted"
    assert state["retry_policy"]["retry_limit"] == 3
    assert state["retry_policy"]["automatic_retry_allowed"] is False
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


@pytest.mark.parametrize("error_code", ["POLICY_BLOCKED", "INVALID_MANIFEST", "SLURM_TIMEOUT"])
def test_durable_downstream_permanent_or_exhausted_failure_blocks_until_manual_retry(
    tmp_path: Path,
    error_code: str,
) -> None:
    retry_count = 3 if error_code == "SLURM_TIMEOUT" else 1
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, retry_limit=3)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "succeeded",
            "durable_shud_output_exists": True,
            "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
            "pipeline_status": "failed",
            "failed_stage": "parse",
            "error_code": error_code,
            "retry_count": retry_count,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["blocked_candidates"][0]["state_evidence"]
    assert state["decision"] == "permanent_failure"
    assert state["retry_policy"]["automatic_retry_allowed"] is False
    assert state["manual_retry_required"] is True
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_cancelled_candidate_requires_manual_retry_and_manual_marker_allows_retry(tmp_path: Path) -> None:
    cancelled_state = {
        "pipeline_status": "cancelled",
        "hydro_status": "cancelled",
        "retry_count": 1,
    }
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    blocked_orchestrator = FakeProductionOrchestrator()
    blocked_scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(cancelled_state),
        orchestrator_factory=lambda _source_id: blocked_orchestrator,
    )

    blocked = blocked_scheduler.run_once()

    assert blocked.evidence["blocked_candidates"][0]["reason"] == "manual_retry_required_after_cancelled"
    assert blocked.evidence["blocked_candidates"][0]["state_evidence"]["replacement_submitted"] is False
    assert blocked.evidence["counts"]["submitted_count"] == 0
    assert blocked_orchestrator.calls == []

    retry_orchestrator = FakeProductionOrchestrator()
    retry_scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeCandidateStateRepository(
            {**cancelled_state, "manual_retry": {"marker": True}, "prior_failure_reason": "cancelled"}
        ),
        orchestrator_factory=lambda _source_id: retry_orchestrator,
    )

    retried = retry_scheduler.run_once()

    assert retried.evidence["blocked_candidates"] == []
    assert retried.evidence["candidates"][0]["state_evidence"]["decision"] == "manual_retry"
    assert retried.evidence["counts"]["submitted_count"] == 1
    assert retry_orchestrator.calls


def test_candidate_state_cycle_aggregate_success_does_not_skip_failed_model_and_reuses_sibling(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    task_results = [
        {"task_id": 0, "array_task_id": 0, "model_id": "model_a", "status": "succeeded"},
        {
            "task_id": 1,
            "array_task_id": 1,
            "model_id": "model_b",
            "status": "failed",
            "error_code": "NODE_FAILURE",
            "error_message": "node lost",
        },
    ]
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_forcing",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "model_id": None,
                    "status": "partially_failed",
                    "stage": "forcing",
                    "error_code": "NODE_FAILURE",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_publish",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "model_id": None,
                    "status": "succeeded",
                    "stage": "publish",
                },
            ],
            "pipeline_events": [
                {
                    "event_type": "status_change",
                    "entity_id": "job_cycle_gfs_2026052106_forcing",
                    "status_to": "partially_failed",
                    "details": {
                        "stage": "forcing",
                        "job_type": "produce_forcing_array",
                        "task_results": task_results,
                    },
                }
            ],
            "pipeline_status": "failed",
            "failed_stage": "forcing",
            "error_code": "NODE_FAILURE",
            "array_task_id": 1,
            "original_task_id": 1,
            "successful_sibling_outputs_reused": True,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert state["decision"] == "retry_failed"
    assert state["task_identity"]["array_task_id"] == 1
    assert state["task_identity"]["original_task_id"] == 1
    assert state["reuse"]["successful_sibling_outputs_reused"] is True
    assert submitted_basin["state_evidence"]["task_identity"]["array_task_id"] == 1
    assert result.evidence["skipped_candidates"] == []


def test_ambiguous_array_task_events_do_not_drive_retry_or_sibling_reuse(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    task_results = [
        {"task_id": 0, "array_task_id": 0, "status": "succeeded"},
        {
            "task_id": 1,
            "array_task_id": 1,
            "status": "failed",
            "error_code": "NODE_FAILURE",
        },
    ]
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_forcing",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "model_id": None,
                    "status": "partially_failed",
                    "stage": "forcing",
                    "error_code": "NODE_FAILURE",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_publish",
                    "run_id": "cycle_gfs_2026052106",
                    "cycle_id": "gfs_2026052106",
                    "model_id": None,
                    "status": "succeeded",
                    "stage": "publish",
                },
            ],
            "pipeline_events": [
                {
                    "event_type": "status_change",
                    "entity_id": "job_cycle_gfs_2026052106_forcing",
                    "status_to": "partially_failed",
                    "details": {
                        "stage": "forcing",
                        "job_type": "produce_forcing_array",
                        "task_results": task_results,
                    },
                }
            ],
            "pipeline_status": None,
            "failed_stage": None,
            "error_code": None,
            "array_task_id": None,
            "original_task_id": None,
            "successful_sibling_outputs_reused": False,
            "shared_cycle_aggregate": True,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert "state_evidence" not in result.evidence["candidates"][0]
    assert "state_evidence" not in result.evidence["model_run_evidence"][0]
    assert "state_evidence" not in submitted_basin
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1


def test_manual_retry_event_in_candidate_state_preserves_prior_reason_and_attempts(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "parse",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "pipeline_events": [
                {
                    "event_type": "retry",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "source": "gfs",
                    "cycle_time": "2026-05-21T06:00:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 4,
                        "prior_failure_reason": "INVALID_MANIFEST",
                        "previous_error": "INVALID_MANIFEST",
                        "previous_job_id": "job_parse",
                    },
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "manual_retry"
    assert state["manual_retry"]["marker"] is True
    assert state["manual_retry"]["previous_attempt"] == 3
    assert state["manual_retry"]["new_attempt"] == 4
    assert state["manual_retry"]["prior_failure_reason"] == "INVALID_MANIFEST"
    assert state["prior_failure_reason"] == "INVALID_MANIFEST"
    assert state["retry_policy"]["attempt"] == 4
    assert result.evidence["counts"]["submitted_count"] == 1


def test_candidate_state_rows_and_events_are_bounded_before_evidence_amplification(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        candidate_state_job_limit=2,
        candidate_state_event_limit=1,
    )
    jobs = [
        {
            "job_id": f"job_{index}",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": "failed",
            "stage": "forecast",
            "error_code": "NODE_FAILURE",
        }
        for index in range(5)
    ]
    events = [
        {
            "event_type": "status_change",
            "details": {"stage": "forecast", "payload": "x" * 1000},
        }
        for _ in range(4)
    ]
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_jobs": jobs,
            "pipeline_events": events,
            "pipeline_jobs_total": len(jobs),
            "pipeline_events_total": len(events),
            "pipeline_status": "failed",
            "failed_stage": "forecast",
            "error_code": "NODE_FAILURE",
        }
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert len(state["pipeline_jobs"]) == 2
    assert len(state["pipeline_events"]) == 1
    assert state["state_bounds"]["overflow"] is True
    assert state["state_bounds"]["pipeline_jobs_total"] == 5
    assert state["state_bounds"]["pipeline_events_total"] == 4


def test_candidate_state_bounded_evidence_owner_module_matches_scheduler_facade() -> None:
    candidate = _scheduler_candidate_fixture()
    task_limit = scheduler_state_module.CANDIDATE_STATE_TASK_RESULT_LIMIT
    task_results = [
        {
            **_production_identity_fixture(),
            "task_id": index,
            "array_task_id": index,
            "status": "succeeded",
        }
        for index in range(task_limit + 3)
    ]
    state = {
        "job_limit": 2,
        "event_limit": 1,
        "pipeline_jobs": [
            {
                "job_id": f"job_{index}",
                "run_id": candidate.run_id,
                "candidate_id": candidate.candidate_id,
                "forcing_version_id": candidate.forcing_version_id,
                "model_id": candidate.model_id,
                "basin_id": candidate.basin_id,
                "status": "failed",
                "stage": "forecast",
                "error_code": "NODE_FAILURE",
            }
            for index in range(5)
        ],
        "pipeline_events": [
            {
                "event_id": index,
                "event_type": "status_change",
                "details": {
                    "stage": "forecast",
                    "task_results": task_results,
                    "task_results_total": len(task_results),
                },
            }
            for index in range(4)
        ],
        "pipeline_jobs_total": 5,
        "pipeline_events_total": 4,
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
    }

    facade_evidence = scheduler_module._candidate_state_evidence(candidate, state)
    owner_evidence = scheduler_state_module._candidate_state_evidence(candidate, state)

    assert facade_evidence == owner_evidence
    assert len(facade_evidence["pipeline_jobs"]) == 2
    assert len(facade_evidence["pipeline_events"]) == 1
    event_details = facade_evidence["pipeline_events"][0]["details"]
    assert len(event_details["task_results"]) == task_limit
    assert event_details["task_results_total"] == task_limit + 3
    assert event_details["task_results_included"] == task_limit
    assert event_details["task_results_limit"] == task_limit
    assert event_details["task_results_overflow"] is True
    assert event_details["task_results_omitted"] == 3
    assert facade_evidence["state_bounds"] == {
        "job_limit": 2,
        "event_limit": 1,
        "pipeline_jobs_total": 5,
        "pipeline_jobs_returned": 2,
        "pipeline_jobs_overflow": True,
        "pipeline_events_total": 4,
        "pipeline_events_returned": 1,
        "pipeline_events_overflow": True,
        "bounded": True,
        "overflow": True,
        "reason": "candidate_state_row_limit_applied",
    }


def test_candidate_state_nested_task_results_are_bounded_before_evidence_and_scanning(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        candidate_state_event_limit=1,
    )
    task_count = MAX_MODEL_RUN_STAGE_TASK_ROWS + 5
    task_results = [
        {
            **_production_identity_fixture(),
            "task_id": index,
            "array_task_id": index,
            "status": "succeeded",
            "large_payload": "x" * 100,
        }
        for index in range(task_count)
    ]
    task_results[0] = {
        **task_results[0],
        "basin_id": "basin_other",
        "status": "failed",
        "error_code": "NODE_FAILURE",
    }
    active_repository = RawCandidateStateRepository(
        {
            "pipeline_events": [
                {
                    "event_id": 20,
                    "event_type": "status_change",
                    "details": {
                        "stage": "forecast",
                        "task_results": task_results,
                    },
                }
            ],
        }
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()

    blocked = result.evidence["blocked_candidates"][0]
    event_details = blocked["state_evidence"]["pipeline_events"][0]["details"]
    mismatch = blocked["state_evidence"]["production_identity_validation"]["mismatches"][0]
    assert blocked["reason"] == "production_identity_mismatch"
    assert len(event_details["task_results"]) == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert event_details["task_results_total"] == MAX_MODEL_RUN_STAGE_TASK_ROWS + 1
    assert event_details["task_results_included"] == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert event_details["task_results_overflow"] is True
    assert event_details["task_results_omitted"] == 1
    assert mismatch["source"] == "pipeline_events[0].details.task_results[0]"
    assert mismatch["field"] == "basin_id"


def test_candidate_state_nested_task_results_do_not_scan_past_overflow_sentinel() -> None:
    task_results = BoundedReadSequence(
        [
            {
                **_production_identity_fixture(),
                "task_id": index,
                "array_task_id": index,
                "status": "succeeded",
            }
            for index in range(MAX_MODEL_RUN_STAGE_TASK_ROWS + 8)
        ],
        allowed_reads=MAX_MODEL_RUN_STAGE_TASK_ROWS + 1,
    )
    state = {
        "pipeline_events": [
            {
                "event_id": 22,
                "event_type": "status_change",
                "details": {
                    "stage": "forecast",
                    "task_results": task_results,
                    "task_results_total": 999,
                },
            }
        ],
    }

    evidence = scheduler_module._candidate_state_evidence(_scheduler_candidate_fixture(), state)

    event_details = evidence["pipeline_events"][0]["details"]
    assert len(event_details["task_results"]) == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert event_details["task_results_total"] == 999
    assert event_details["task_results_included"] == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert event_details["task_results_limit"] == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert event_details["task_results_overflow"] is True
    assert event_details["task_results_omitted"] == 999 - MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert task_results.read_count == MAX_MODEL_RUN_STAGE_TASK_ROWS + 1


def test_candidate_state_nested_task_results_outside_bound_do_not_drive_retry_or_evidence(
    tmp_path: Path,
) -> None:
    task_count = MAX_MODEL_RUN_STAGE_TASK_ROWS + 1
    task_results = [
        {
            "task_id": index,
            "array_task_id": index,
            "status": "succeeded",
        }
        for index in range(task_count)
    ]
    task_results[-1] = {
        **task_results[-1],
        "status": "failed",
        "error_code": "NODE_FAILURE",
        "slurm_job_id": "hidden_overflow_task",
    }
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=RawCandidateStateRepository(
            {
                "pipeline_status": "failed",
                "failed_stage": "forecast",
                "error_code": "NODE_FAILURE",
                "pipeline_events": [
                    {
                        "event_id": 21,
                        "event_type": "status_change",
                        "details": {"stage": "forecast", "task_results": task_results},
                    }
                ],
            }
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    assert orchestrator.calls


@pytest.mark.parametrize(
    ("latest_status", "expected_reason"),
    [
        ("permanently_failed", "permanent_failure_guard"),
        ("cancelled", "manual_retry_required_after_cancelled"),
        ("running", "active_slurm_job"),
    ],
)
def test_latest_bounded_candidate_state_row_wins_over_older_truncated_rows(
    tmp_path: Path,
    latest_status: str,
    expected_reason: str,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        candidate_state_job_limit=2,
    )
    jobs = [
        {
            "job_id": "job_old_failed",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": "failed",
            "stage": "forecast",
            "retry_count": 1,
            "error_code": "NODE_FAILURE",
            "submitted_at": "2026-05-21T06:00:00Z",
        },
        {
            "job_id": "job_old_retry",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": "failed",
            "stage": "forecast",
            "retry_count": 2,
            "error_code": "NODE_FAILURE",
            "submitted_at": "2026-05-21T06:10:00Z",
        },
        {
            "job_id": "job_latest",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": latest_status,
            "stage": "forecast",
            "retry_count": 3,
            "error_code": "INVALID_MANIFEST" if latest_status == "permanently_failed" else None,
            "slurm_job_id": "999" if latest_status == "running" else None,
            "submitted_at": "2026-05-21T06:20:00Z",
        },
    ]
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_jobs": jobs[-2:],
            "pipeline_jobs_total": len(jobs),
            "state_truncated": True,
            "pipeline_status": latest_status,
            "failed_stage": "forecast" if latest_status == "permanently_failed" else None,
            "error_code": "INVALID_MANIFEST" if latest_status == "permanently_failed" else None,
            "retry_count": 3,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped_or_blocked = [*result.evidence["blocked_candidates"], *result.evidence["skipped_candidates"]]
    assert skipped_or_blocked[0]["reason"] == expected_reason
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_latest_manual_retry_event_outside_oldest_first_cap_allows_candidate(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        candidate_state_event_limit=1,
    )
    older_events = [
        {
            "event_type": "status_change",
            "created_at": f"2026-05-21T06:0{index}:00Z",
            "details": {"stage": "forecast", "error_code": "INVALID_MANIFEST"},
        }
        for index in range(4)
    ]
    latest_manual_retry = {
        "event_type": "retry",
        "run_id": "fcst_gfs_2026052106_model_a",
        "model_id": "model_a",
        "source": "gfs",
        "cycle_time": "2026-05-21T06:00:00Z",
        "created_at": "2026-05-21T06:10:00Z",
        "details": {
            "trigger": "manual",
            "manual_retry_marker": True,
            "retry_count": 4,
            "prior_failure_reason": "INVALID_MANIFEST",
        },
    }
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "forecast",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "pipeline_events": [latest_manual_retry],
            "pipeline_events_total": len(older_events) + 1,
            "state_truncated": True,
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "manual_retry"
    assert state["manual_retry"]["new_attempt"] == 4
    assert state["state_bounds"]["pipeline_events_overflow"] is True
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls


@pytest.mark.parametrize(
    ("latest_status", "expected_reason"),
    [
        ("permanently_failed", "permanent_failure_guard"),
        ("cancelled", "manual_retry_required_after_cancelled"),
        ("queued", "active_duplicate_pipeline"),
        ("running", "active_slurm_job"),
    ],
)
def test_stale_manual_retry_marker_does_not_override_newer_blocking_truth(
    tmp_path: Path,
    latest_status: str,
    expected_reason: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    latest_job = {
        "job_id": "job_latest",
        "run_id": "fcst_gfs_2026052106_model_a",
        "status": latest_status,
        "stage": "forecast",
        "retry_count": 3,
        "error_code": "INVALID_MANIFEST" if latest_status == "permanently_failed" else None,
        "slurm_job_id": "999" if latest_status == "running" else None,
        "submitted_at": "2026-05-21T06:20:00Z",
        "updated_at": "2026-05-21T06:21:00Z",
    }
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": latest_status,
            "failed_stage": "forecast" if latest_status == "permanently_failed" else None,
            "error_code": "INVALID_MANIFEST" if latest_status == "permanently_failed" else None,
            "retry_count": 3,
            "pipeline_jobs": [latest_job],
            "pipeline_events": [
                {
                    "event_id": 1,
                    "event_type": "retry",
                    "created_at": "2026-05-21T06:10:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 3,
                        "previous_job_id": "job_old_failed",
                        "prior_failure_reason": "NODE_FAILURE",
                    },
                },
                {
                    "event_id": 2,
                    "event_type": "status_change",
                    "entity_id": "job_latest",
                    "status_to": latest_status,
                    "created_at": "2026-05-21T06:21:00Z",
                    "details": {
                        "stage": "forecast",
                        "error_code": "INVALID_MANIFEST" if latest_status == "permanently_failed" else None,
                    },
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped_or_blocked = [*result.evidence["blocked_candidates"], *result.evidence["skipped_candidates"]]
    assert skipped_or_blocked[0]["reason"] == expected_reason
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_newer_manual_retry_after_terminal_truth_allows_candidate_and_preserves_attempts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "pipeline_status": "permanently_failed",
            "failed_stage": "forecast",
            "error_code": "INVALID_MANIFEST",
            "retry_count": 3,
            "pipeline_jobs": [
                {
                    "job_id": "job_latest",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "permanently_failed",
                    "stage": "forecast",
                    "retry_count": 3,
                    "error_code": "INVALID_MANIFEST",
                    "updated_at": "2026-05-21T06:20:00Z",
                }
            ],
            "pipeline_events": [
                {
                    "event_id": 5,
                    "event_type": "retry",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "source": "gfs",
                    "cycle_time": "2026-05-21T06:00:00Z",
                    "entity_id": "job_retry",
                    "created_at": "2026-05-21T06:30:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 4,
                        "previous_job_id": "job_latest",
                        "prior_failure_reason": "INVALID_MANIFEST",
                    },
                }
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "manual_retry"
    assert state["manual_retry"]["previous_attempt"] == 3
    assert state["manual_retry"]["new_attempt"] == 4
    assert state["manual_retry"]["prior_failure_reason"] == "INVALID_MANIFEST"
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (
            {
                "pipeline_status": "running",
                "pipeline_jobs": [
                    {
                        "job_id": "job_running",
                        "run_id": "fcst_gfs_2026052106_model_a",
                        "status": "running",
                        "stage": "forecast",
                        "slurm_job_id": "999",
                        "updated_at": "2026-05-21T06:20:00Z",
                    }
                ],
            },
            "active_slurm_job",
        ),
        (
            {
                "pipeline_status": "queued",
                "pipeline_jobs": [
                    {
                        "job_id": "job_queued",
                        "run_id": "fcst_gfs_2026052106_model_a",
                        "status": "queued",
                        "stage": "forecast",
                        "updated_at": "2026-05-21T06:20:00Z",
                    }
                ],
            },
            "active_duplicate_pipeline",
        ),
        (
            {
                "pipeline_jobs": [
                    {
                        "job_id": "job_running_no_slurm",
                        "run_id": "fcst_gfs_2026052106_model_a",
                        "status": "running",
                        "stage": "forecast",
                        "updated_at": "2026-05-21T06:20:00Z",
                    }
                ],
            },
            "active_duplicate_pipeline",
        ),
        (
            {
                "pipeline_events": [
                    {
                        "event_id": 8,
                        "event_type": "status_change",
                        "run_id": "fcst_gfs_2026052106_model_a",
                        "model_id": "model_a",
                        "source": "gfs",
                        "cycle_time": "2026-05-21T06:00:00Z",
                        "entity_id": "job_event_only_running",
                        "status_to": "running",
                        "created_at": "2026-05-21T06:20:00Z",
                        "details": {"stage": "forecast"},
                    }
                ],
            },
            "active_duplicate_pipeline",
        ),
    ],
)
def test_newer_manual_retry_marker_does_not_override_active_truth(
    tmp_path: Path,
    state: dict[str, Any],
    expected_reason: str,
) -> None:
    state = dict(state)
    pipeline_events = list(state.pop("pipeline_events", []))
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            **state,
            "pipeline_events": [
                *pipeline_events,
                {
                    "event_id": 9,
                    "event_type": "retry",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "model_id": "model_a",
                    "source": "gfs",
                    "cycle_time": "2026-05-21T06:00:00Z",
                    "entity_id": "job_manual_retry",
                    "created_at": "2026-05-21T06:30:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 4,
                        "previous_job_id": "job_old_failed",
                    },
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    skipped = result.evidence["skipped_candidates"][0]
    assert skipped["reason"] == expected_reason
    assert skipped["state_evidence"]["decision"] == "skip_active"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_latest_unavailable_cycle_does_not_consume_source_budget_when_older_cycle_is_runnable(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T18:00:00Z"),
        dry_run=False,
        max_cycles_per_source=1,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    (
                        "2026-05-21T12:00:00Z",
                        False,
                        {"reason": "source_cycle_unavailable", "retryable": True},
                    ),
                    ("2026-05-21T06:00:00Z", True),
                ],
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls[0]["cycle_time"] == _dt("2026-05-21T06:00:00Z")
    unavailable = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "gfs_2026052112")
    assert unavailable["selection_status"] == "not_selected"
    assert unavailable["selection_reason"] == "source_cycle_unavailable_does_not_consume_source_budget"


def test_backfill_selects_oldest_incomplete_cycle_and_defers_later_gaps_for_warm_start(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T18:00:00Z"),
        dry_run=False,
        backfill_enabled=True,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    ("2026-05-21T00:00:00Z", True),
                    ("2026-05-21T06:00:00Z", True),
                    ("2026-05-21T12:00:00Z", True),
                ],
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["counts"]["source_cycle_count"] == 1
    assert orchestrator.calls[0]["cycle_time"] == _dt("2026-05-21T00:00:00Z")
    selected_cycles = [
        item["cycle_id"]
        for item in result.evidence["source_cycles"]
        if "cycle_id" in item and item.get("type") != "backfill_deferred" and item.get("status") != "excluded"
    ]
    assert selected_cycles == ["gfs_2026052100"]
    deferred = [item for item in result.evidence["source_cycles"] if item.get("type") == "backfill_deferred"]
    assert [item["cycle_id"] for item in deferred] == ["gfs_2026052106", "gfs_2026052112"]
    assert {item["reason"] for item in deferred} == {"backfill_deferred_waiting_for_prior_cycle"}
    audit = next(item for item in result.evidence["backfill"]["audit"] if item["source_id"] == "gfs")
    assert audit["gap_count"] == 3
    assert audit["available_gap_count"] == 3
    assert audit["unavailable_gap_count"] == 0
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 2


def test_backfill_floor_lookback_window_to_cycle_boundary_for_warm_start_order(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-06T16:24:00Z"),
        dry_run=False,
        backfill_enabled=True,
        lookback_hours=168,
        cycle_lag_hours=16,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    ("2026-05-30T00:00:00Z", True),
                    ("2026-05-30T06:00:00Z", True),
                ],
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls[0]["cycle_time"] == _dt("2026-05-30T00:00:00Z")
    selected = [
        item["cycle_id"]
        for item in result.evidence["source_cycles"]
        if "cycle_id" in item and item.get("type") != "backfill_deferred"
    ]
    deferred = [
        item["cycle_id"] for item in result.evidence["source_cycles"] if item.get("type") == "backfill_deferred"
    ]
    assert selected == ["gfs_2026053000"]
    assert deferred == ["gfs_2026053006"]


def test_backfill_selects_global_oldest_cycle_across_sources_and_defers_later_cycles(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-06T16:24:00Z"),
        sources=("gfs", "IFS"),
        dry_run=False,
        backfill_enabled=True,
        lookback_hours=168,
        cycle_lag_hours=16,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    ("2026-05-30T00:00:00Z", True),
                ],
            ),
            "IFS": FakeAdapter(
                "IFS",
                [
                    ("2026-05-30T00:00:00Z", True),
                ],
            ),
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 2
    assert [(call["source"], call["cycle_time"]) for call in orchestrator.calls] == [
        ("gfs", _dt("2026-05-30T00:00:00Z")),
        ("IFS", _dt("2026-05-30T00:00:00Z")),
    ]


def test_backfill_defers_later_source_cycle_until_global_oldest_cycle_is_done(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-06T16:24:00Z"),
        sources=("gfs", "IFS"),
        dry_run=False,
        backfill_enabled=True,
        lookback_hours=168,
        cycle_lag_hours=16,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter("gfs", [("2026-05-30T00:00:00Z", True)]),
            "IFS": FakeAdapter("IFS", [("2026-05-30T06:00:00Z", True)]),
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert [(call["source"], call["cycle_time"]) for call in orchestrator.calls] == [
        ("gfs", _dt("2026-05-30T00:00:00Z"))
    ]
    deferred = [
        item
        for item in result.evidence["source_cycles"]
        if item.get("type") == "backfill_deferred"
        and item.get("reason") == "backfill_deferred_waiting_for_global_prior_cycle"
    ]
    assert [item["cycle_id"] for item in deferred] == ["ifs_2026053006"]


def test_backfill_selects_earliest_durable_incomplete_cycle_before_later_download_gap(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-06T16:24:00Z"),
        dry_run=False,
        backfill_enabled=True,
        lookback_hours=168,
        cycle_lag_hours=16,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    model = _model("model_a", "basin_a")
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    ("2026-05-30T00:00:00Z", True),
                    ("2026-05-30T06:00:00Z", True),
                    ("2026-05-30T18:00:00Z", True),
                ],
            )
        },
        active_repository=PerCycleCandidateStateRepository(
            {
                "2026-05-30T00:00:00+00:00": {"hydro_status": "published"},
                "2026-05-30T06:00:00+00:00": {"forecast_cycle_status": "forcing_ready"},
            }
        ),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls[0]["cycle_time"] == _dt("2026-05-30T06:00:00Z")
    selected = [
        item["cycle_id"]
        for item in result.evidence["source_cycles"]
        if "cycle_id" in item and item.get("type") != "backfill_deferred"
    ]
    deferred = [
        item["cycle_id"] for item in result.evidence["source_cycles"] if item.get("type") == "backfill_deferred"
    ]
    assert selected == ["gfs_2026053006"]
    assert deferred == ["gfs_2026053018"]


def test_backfill_skips_unavailable_gap_and_selects_oldest_available_for_warm_start(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T18:00:00Z"),
        dry_run=False,
        backfill_enabled=True,
        max_cycles_per_source=8,
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [
                    (
                        "2026-05-21T00:00:00Z",
                        False,
                        {"reason": "source_cycle_unavailable", "retryable": True},
                    ),
                    ("2026-05-21T06:00:00Z", True),
                    ("2026-05-21T12:00:00Z", True),
                ],
            )
        },
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["counts"]["source_cycle_count"] == 1
    assert orchestrator.calls[0]["cycle_time"] == _dt("2026-05-21T06:00:00Z")
    unavailable = next(item for item in result.evidence["source_cycles"] if item["cycle_id"] == "gfs_2026052100")
    assert unavailable["selection_status"] == "not_selected"
    assert unavailable["selection_reason"] == "source_cycle_unavailable_does_not_consume_source_budget"
    deferred = [item for item in result.evidence["source_cycles"] if item.get("type") == "backfill_deferred"]
    assert [item["cycle_id"] for item in deferred] == ["gfs_2026052112"]
    audit = next(item for item in result.evidence["backfill"]["audit"] if item["source_id"] == "gfs")
    assert audit["gap_count"] == 3
    assert audit["available_gap_count"] == 2
    assert audit["unavailable_gap_count"] == 1
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 1


def test_backfill_probe_failed_gap_keeps_retryable_evidence_and_does_not_consume_budget(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-08T12:00:00Z"),
        sources=("IFS",),
        backfill_enabled=True,
        max_cycles_per_source=8,
    )
    attempted_sources = [
        {
            "source": "aws",
            "uri": "ecmwf-opendata://aws/ifs/2026060800/ifs.t00z.f000.2t.grib2",
            "status": "probe_failed",
            "error_class": "NetworkDownloadError",
            "error_message": "temporary failure in name resolution",
        }
    ]
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "IFS": FakeAdapter(
                "IFS",
                [
                    (
                        "2026-06-08T00:00:00Z",
                        False,
                        {
                            "status": "probe_failed",
                            "reason": "source_cycle_probe_failed",
                            "classifier": "network_error",
                            "retryable": True,
                            "evidence": {
                                "attempted_sources": attempted_sources,
                                "attempted_source_count": 1,
                                "emitted_attempt_count": 1,
                                "attempted_source_limit": 8,
                                "omitted_attempt_count": 0,
                            },
                        },
                    ),
                    ("2026-06-08T06:00:00Z", True),
                ],
            )
        },
    )

    result = scheduler.run_once()

    selected = next(item for item in result.evidence["source_cycles"] if item.get("cycle_id") == "ifs_2026060806")
    probe_failed = next(item for item in result.evidence["source_cycles"] if item.get("cycle_id") == "ifs_2026060800")
    assert selected["cycle_id"] == "ifs_2026060806"
    assert result.evidence["candidates"][0]["cycle_id"] == "ifs_2026060806"
    assert probe_failed["status"] == "probe_failed"
    assert probe_failed["reason"] == "source_cycle_probe_failed"
    assert probe_failed["classifier"] == "network_error"
    assert probe_failed["retryable"] is True
    assert probe_failed["cycle_status_candidate"] == "probe_failed"
    assert probe_failed["db_cycle_status_written"] is None
    assert probe_failed["selection_status"] == "not_selected"
    assert probe_failed["selection_reason"] == "source_cycle_probe_failed_does_not_consume_source_budget"
    assert probe_failed["discovery_evidence"]["attempted_sources"] == attempted_sources
    assert probe_failed["discovery_evidence"]["attempted_source_count"] == 1
    audit = next(item for item in result.evidence["backfill"]["audit"] if item["source_id"] == "IFS")
    assert audit["gap_count"] == 2
    assert audit["available_gap_count"] == 1
    assert audit["unavailable_gap_count"] == 1
    assert audit["selected_count"] == 1


def test_backfill_rate_limited_gap_keeps_retryable_evidence_and_does_not_consume_budget(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-06-08T12:00:00Z"),
        sources=("IFS",),
        backfill_enabled=True,
        max_cycles_per_source=8,
    )
    attempted_sources = [
        {
            "source": "aws",
            "uri": "ecmwf-opendata://aws/ifs/2026060800/ifs.t00z.f000.2t.grib2",
            "status": "rate_limited",
            "error_class": "RateLimitedSourceError",
            "error_message": "source mirror returned 429 Too Many Requests",
        }
    ]
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "IFS": FakeAdapter(
                "IFS",
                [
                    (
                        "2026-06-08T00:00:00Z",
                        False,
                        {
                            "status": "rate_limited",
                            "reason": "source_cycle_rate_limited",
                            "classifier": "rate_limited",
                            "retryable": True,
                            "evidence": {
                                "attempted_sources": attempted_sources,
                                "attempted_source_count": 1,
                                "emitted_attempt_count": 1,
                                "attempted_source_limit": 8,
                                "omitted_attempt_count": 0,
                            },
                        },
                    ),
                    ("2026-06-08T06:00:00Z", True),
                ],
            )
        },
    )

    result = scheduler.run_once()

    selected = next(item for item in result.evidence["source_cycles"] if item.get("cycle_id") == "ifs_2026060806")
    rate_limited = next(item for item in result.evidence["source_cycles"] if item.get("cycle_id") == "ifs_2026060800")
    assert selected["cycle_id"] == "ifs_2026060806"
    assert result.evidence["candidates"][0]["cycle_id"] == "ifs_2026060806"
    assert rate_limited["status"] == "rate_limited"
    assert rate_limited["reason"] == "source_cycle_rate_limited"
    assert rate_limited["classifier"] == "rate_limited"
    assert rate_limited["retryable"] is True
    assert rate_limited["cycle_status_candidate"] == "rate_limited"
    assert rate_limited["db_cycle_status_written"] is None
    assert rate_limited["selection_status"] == "not_selected"
    assert rate_limited["selection_reason"] == "source_cycle_rate_limited_does_not_consume_source_budget"
    assert rate_limited["discovery_evidence"]["attempted_sources"] == attempted_sources
    assert rate_limited["discovery_evidence"]["attempted_source_count"] == 1
    audit = next(item for item in result.evidence["backfill"]["audit"] if item["source_id"] == "IFS")
    assert audit["gap_count"] == 2
    assert audit["available_gap_count"] == 1
    assert audit["unavailable_gap_count"] == 1
    assert audit["selected_count"] == 1


def test_manual_retry_marker_override_helper_never_overrides_active_blocker() -> None:
    assert (
        scheduler_module._manual_retry_marker_overrides_blocker(
            {
                "timestamp": _dt("2026-05-21T06:30:00Z"),
                "attempt": 4,
                "previous_job_id": "job_running",
            },
            {
                "timestamp": _dt("2026-05-21T06:20:00Z"),
                "attempt": 3,
                "job_id": "job_running",
                "active": True,
            },
        )
        is False
    )


def test_manual_retry_marker_bound_to_terminal_blocker_overrides_newer_blocker_timestamp() -> None:
    assert (
        scheduler_module._manual_retry_marker_overrides_blocker(
            {
                "timestamp": _dt("2026-05-21T06:30:00Z"),
                "attempt": 2,
                "previous_job_id": "job_failed",
            },
            {
                "timestamp": _dt("2026-05-21T06:31:00Z"),
                "attempt": 0,
                "job_id": "job_failed",
                "active": False,
            },
        )
        is True
    )


def test_active_skip_and_cancel_evidence_redacts_secret_urls_and_error_messages(tmp_path: Path) -> None:
    secret_uri = "s3://bucket/logs/job.out?token=supersecret"
    secret_message = "failed callback https://user:pass@example.test/log?signature=abc token=rawsecret"
    active_jobs = [
        {
            "job_id": "job_forcing",
            "slurm_job_id": "7777",
            "stage": "forcing",
            "status": "running",
            "log_uri": secret_uri,
            "error_message": secret_message,
        }
    ]
    skip_scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(active_jobs=active_jobs),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
    )

    skipped = skip_scheduler.run_once()

    skipped_json = json.dumps(skipped.evidence)
    assert "supersecret" not in skipped_json
    assert "rawsecret" not in skipped_json
    assert "user:pass" not in skipped_json
    assert "s3://bucket/logs/job.out?token" not in skipped_json

    cancel_orchestrator = FakeProductionOrchestrator(
        cancel_payload=[
            {
                **active_jobs[0],
                "status": "cancelled",
                "replacement_submitted": False,
            }
        ]
    )
    cancel_scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, cancel_active_slurm=True),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(active_jobs=active_jobs),
        orchestrator_factory=lambda _source_id: cancel_orchestrator,
    )

    cancelled = cancel_scheduler.run_once()

    cancelled_json = json.dumps(cancelled.evidence)
    assert "supersecret" not in cancelled_json
    assert "rawsecret" not in cancelled_json
    assert "user:pass" not in cancelled_json
    assert "s3://bucket/logs/job.out?token" not in cancelled_json


def test_orchestrator_exception_evidence_and_artifact_redact_secret_text(tmp_path: Path) -> None:
    class SecretFailureOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            raise RuntimeError(
                "failed https://user:pass@example.test/log?signature=sig123 token=tok123 password=pass123"
            )

    orchestrator = SecretFailureOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    artifact_text = Path(result.artifact_path or "").read_text(encoding="utf-8")
    persisted = json.loads(artifact_text)
    evidence_text = json.dumps(result.evidence, sort_keys=True)
    assert orchestrator.calls
    assert result.status == "submission_failed"
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "submission_failed"
        assert evidence["execution_boundary"] == "production_orchestration"
        assert evidence["evidence_pre_execution"]["status"] == "reserved"
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["failed_count"] == 1
        assert evidence["counts"]["partial_count"] == 1
        model_run = evidence["model_run_evidence"][0]
        assert model_run["error_code"] == "PRODUCTION_ORCHESTRATION_FAILED"
        assert model_run["submitted"] is False
        assert model_run["execution_attempted"] is True
        assert model_run["mutation_outcome"] == "unknown_after_attempt"
        assert model_run["mutation_occurred"] == "unknown_after_attempt"
        assert model_run["pipeline_status_writes_proven_absent"] is False
        assert model_run["pipeline_event_writes_proven_absent"] is False
        proof = evidence["execution_write_proof"]
        assert proof["orchestration_called"] is True
        assert proof["protected_by_pre_execution_evidence"] is True
        assert proof["mutation_outcome"] == "unknown_after_attempt"
        assert proof["mutation_occurred"] == "unknown_after_attempt"
        assert proof["unknown_execution_count"] == 1
        assert proof["pipeline_status_writes_proven_absent"] is False
        assert proof["pipeline_event_writes_proven_absent"] is False
        assert evidence["no_mutation_proof"]["slurm_submit_called"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["hydro_result_table_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["met_result_table_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["pipeline_status_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["pipeline_event_writes"] == "unknown_after_attempt"
    for raw_secret in ("user:pass", "sig123", "tok123", "pass123", "signature=sig123", "token=tok123"):
        assert raw_secret not in evidence_text
        assert raw_secret not in artifact_text
    assert "[redacted]" in evidence_text
    assert "[redacted]" in artifact_text


@pytest.mark.parametrize("result_status", ["failed", "submission_failed"])
def test_returned_failed_pipeline_without_slurm_id_keeps_pipeline_write_proof(
    tmp_path: Path,
    result_status: str,
) -> None:
    class ReturnedFailureNoSlurmOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            basin = basins[0]
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status=result_status,
                stages=(
                    StageRunResult(
                        stage="forcing",
                        job_type="produce_forcing_array",
                        pipeline_job_id="job_forcing",
                        slurm_job_id="",
                        status="submission_failed",
                        error_code="SBATCH_SUBMISSION_FAILED",
                    ),
                ),
                candidate_outcomes=(
                    {
                        "candidate_id": basin["candidate_id"],
                        "run_id": basin["run_id"],
                        "model_id": basin["model_id"],
                        "status": "submission_failed",
                        "stage": "forcing",
                        "reason": "sbatch_submission_failed",
                        "pipeline_status_write": True,
                        "pipeline_event_write": True,
                        "slurm_job_id": "",
                    },
                ),
            )

    orchestrator = ReturnedFailureNoSlurmOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert orchestrator.calls
    assert result.status == "submission_failed"
    for evidence in (result.evidence, persisted):
        model_run = evidence["model_run_evidence"][0]
        proof = evidence["execution_write_proof"]
        no_mutation = evidence["no_mutation_proof"]

        assert model_run["submitted"] is False
        assert model_run["slurm_submit_called"] is False
        assert model_run["execution_attempted"] is True
        assert model_run["pipeline_status_write"] is True
        assert model_run["pipeline_event_write"] is True
        assert model_run["pipeline_status_writes_proven_absent"] is False
        assert model_run["pipeline_event_writes_proven_absent"] is False
        assert model_run["mutation_occurred"] is True

        assert proof["orchestration_called"] is True
        assert proof["slurm_submit_count"] == 0
        assert proof["slurm_submit_proven_absent"] is True
        assert proof["slurm_submit_called"] is False
        assert proof["hydro_result_table_writes"] is False
        assert proof["met_result_table_writes"] is False
        assert proof["pipeline_status_writes"] is True
        assert proof["pipeline_event_writes"] is True
        assert proof["pipeline_status_write_count"] == 1
        assert proof["pipeline_event_write_count"] == 1
        assert proof["pipeline_status_writes_proven_absent"] is False
        assert proof["pipeline_event_writes_proven_absent"] is False

        assert no_mutation["slurm_submit_called"] is False
        assert no_mutation["pipeline_status_writes"] is True
        assert no_mutation["pipeline_event_writes"] is True


def test_returned_pipeline_with_slurm_id_without_pipeline_write_proof_keeps_pipeline_writes_unknown(
    tmp_path: Path,
) -> None:
    class ReturnedSlurmWithoutPipelineWriteOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=(
                    StageRunResult(
                        stage="forcing",
                        job_type="produce_forcing_array",
                        pipeline_job_id="",
                        slurm_job_id="slurm_forcing_123",
                        status="succeeded",
                    ),
                ),
            )

    orchestrator = ReturnedSlurmWithoutPipelineWriteOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert orchestrator.calls
    for evidence in (result.evidence, persisted):
        model_run = evidence["model_run_evidence"][0]
        proof = evidence["execution_write_proof"]
        no_mutation = evidence["no_mutation_proof"]

        assert model_run["slurm_submit_called"] is True
        assert model_run["submitted"] is True
        assert model_run["pipeline_status_write"] == "unknown_after_attempt"
        assert model_run["pipeline_event_write"] == "unknown_after_attempt"

        assert proof["slurm_submit_called"] is True
        assert proof["pipeline_status_writes"] == "unknown_after_attempt"
        assert proof["pipeline_event_writes"] == "unknown_after_attempt"

        assert no_mutation["slurm_submit_called"] is True
        assert no_mutation["pipeline_status_writes"] == "unknown_after_attempt"
        assert no_mutation["pipeline_event_writes"] == "unknown_after_attempt"


def test_active_db_job_cancel_requested_calls_cancel_before_active_skip(tmp_path: Path) -> None:
    active_state = {
        "pipeline_jobs": [
            {
                "job_id": "job_forcing",
                "run_id": "fcst_gfs_2026052106_model_a",
                "status": "running",
                "stage": "forcing",
                "slurm_job_id": "7777",
            }
        ],
        "pipeline_status": "running",
    }
    active_jobs = [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}]
    active_repository = CandidateAndActiveRepository(active_state, active_jobs)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False, cancel_active_slurm=True),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"][0]["reason"] == "cancel_requested_active_slurm"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert orchestrator.calls == []


def test_stale_active_db_job_terminal_slurm_sync_does_not_skip_forever(tmp_path: Path) -> None:
    class SyncingRepository(CandidateAndActiveRepository):
        def __init__(self) -> None:
            self.synced = False
            super().__init__(
                {
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": "fcst_gfs_2026052106_model_a",
                            "status": "running",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                        }
                    ],
                    "pipeline_status": "running",
                },
                [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
            )

        def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
            if self.synced:
                return {
                    "pipeline_status": "failed",
                    "failed_stage": "forcing",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 0,
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": kwargs["run_id"],
                            "status": "failed",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                }
            return super().candidate_state(**kwargs)

        def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [] if self.synced else super().active_slurm_jobs(**kwargs)

    repository = SyncingRepository()

    class SyncingOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            repository.synced = True
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "failed",
                    "error_code": "NODE_FAILURE",
                }
            ]

    orchestrator = SyncingOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    state = result.evidence["candidates"][0]["state_evidence"]
    assert state["decision"] == "retry_failed"
    assert state["slurm_state_sync"]["terminal_updates"][0]["status"] == "failed"
    assert result.evidence["skipped_candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 1


def test_sync_cycle_statuses_blocks_before_sync_when_pre_execution_reservation_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class SyncingRepository(CandidateAndActiveRepository):
        def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
            return {
                **super().candidate_state(**kwargs),
                "pipeline_status": "running",
                "pipeline_jobs": [
                    {
                        "job_id": "job_forcing",
                        "run_id": kwargs["run_id"],
                        "status": "running",
                        "stage": "forcing",
                        "slurm_job_id": "7777",
                    }
                ],
            }

    class SyncMustNotRunOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            del cycle_id
            raise AssertionError("sync_cycle_statuses must not run before evidence reservation")

    now = _dt("2026-05-21T12:00:00Z")
    fixed_suffix = "reservation0"
    pass_id = f"scheduler_{format_cycle_time(now)}_{fixed_suffix}"
    evidence_dir = tmp_path / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{pass_id}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUuid", (), {"hex": fixed_suffix})())
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=SyncingRepository(
            {
                "pipeline_status": "running",
                "pipeline_jobs": [
                    {
                        "job_id": "job_forcing",
                        "status": "running",
                        "stage": "forcing",
                        "slurm_job_id": "7777",
                    }
                ],
            },
            [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
        ),
        orchestrator_factory=lambda _source_id: SyncMustNotRunOrchestrator(),
    )

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "evidence_preflight_blocked"
    assert result.evidence["evidence_pre_execution"]["status"] == "blocked"
    assert result.evidence["slurm_status_sync_proof"]["status"] == "preflight_blocked"
    assert result.evidence["slurm_status_sync_proof"]["sync_called"] is False
    assert result.evidence["no_mutation_proof"]["slurm_status_sync_called"] is False
    assert result.evidence["no_mutation_proof"]["pipeline_status_writes"] is False
    assert result.evidence["model_run_evidence"][0]["error_code"] == "EVIDENCE_WRITE_PRECHECK_FAILED"
    assert result.evidence["model_run_evidence"][0]["sync_attempted"] is False


def test_sync_cycle_statuses_sees_pre_execution_reservation_before_mutating(tmp_path: Path) -> None:
    class SyncingRepository(CandidateAndActiveRepository):
        def __init__(self) -> None:
            self.synced = False
            super().__init__(
                {
                    "pipeline_status": "running",
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "status": "running",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                        }
                    ],
                },
                [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
            )

        def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
            if self.synced:
                return {
                    "pipeline_status": "failed",
                    "failed_stage": "forcing",
                    "error_code": "NODE_FAILURE",
                    "retry_count": 0,
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": kwargs["run_id"],
                            "status": "failed",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                }
            return super().candidate_state(**kwargs)

        def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [] if self.synced else super().active_slurm_jobs(**kwargs)

    repository = SyncingRepository()
    reservation_seen_before_sync: list[bool] = []

    class SyncingOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            reservation_seen_before_sync.append(bool(list(tmp_path.glob("scheduler/evidence/*.pre_execution.json"))))
            repository.synced = True
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "failed",
                }
            ]

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: SyncingOrchestrator(),
    )

    result = scheduler.run_once()

    assert reservation_seen_before_sync == [True]
    assert result.evidence["slurm_status_sync_proof"]["status"] == "synced"
    assert result.evidence["slurm_status_sync_proof"]["protected_by_pre_execution_evidence"] is True
    assert result.evidence["slurm_status_sync_proof"]["sync_called"] is True
    assert result.evidence["slurm_status_sync_proof"]["mutation_occurred"] is True
    assert result.evidence["counts"]["slurm_status_sync_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_status_sync_called"] is True
    assert result.evidence["no_mutation_proof"]["pipeline_event_writes"] is True
    assert result.evidence["counts"]["submitted_count"] == 1


def test_sync_cycle_statuses_exception_after_attempt_persists_conservative_final_evidence(
    tmp_path: Path,
) -> None:
    class SyncError(Exception):
        error_code = "PUBLISHED_LOG_WRITE_FAILED"
        message = "Failed to publish gateway logs."

    class SyncingRepository(CandidateAndActiveRepository):
        def __init__(self) -> None:
            super().__init__(
                {
                    "pipeline_status": "running",
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": "fcst_gfs_2026052106_model_a",
                            "status": "running",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                        }
                    ],
                },
                [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
            )

    reservation_seen_before_sync: list[bool] = []
    sync_calls: list[str] = []

    class RaisingSyncOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            reservation_seen_before_sync.append(bool(list(tmp_path.glob("scheduler/evidence/*.pre_execution.json"))))
            sync_calls.append(cycle_id)
            raise SyncError("publish failed after durable sync")

    orchestrator = RaisingSyncOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=SyncingRepository(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert reservation_seen_before_sync == [True]
    assert sync_calls == ["gfs_2026052106"]
    assert orchestrator.calls == []
    assert result.status == "slurm_status_sync_failed"
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "slurm_status_sync_failed"
        assert evidence["execution_boundary"] == "slurm_status_sync"
        assert evidence["evidence_pre_execution"]["status"] == "reserved"
        assert evidence["evidence_pre_execution"]["proof"] == (
            "scheduler_evidence_directory_write_before_production_mutation"
        )
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["slurm_status_sync_count"] == 0
        assert evidence["counts"]["slurm_status_sync_unknown_count"] == 1
        assert evidence["model_run_evidence"] == []
        assert evidence["skipped_candidates"][0]["reason"] == "active_slurm_status_sync_failed"
        assert evidence["skipped_candidates"][0]["sync_attempted"] is True
        assert evidence["skipped_candidates"][0]["mutation_outcome"] == "unknown_after_attempt"
        proof = evidence["slurm_status_sync_proof"]
        assert proof["status"] == "failed"
        assert proof["sync_called"] is True
        assert proof["protected_by_pre_execution_evidence"] is True
        assert proof["mutation_outcome"] == "unknown_after_attempt"
        assert proof["mutation_occurred"] == "unknown_after_attempt"
        assert proof["pipeline_status_writes_proven_absent"] is False
        assert proof["pipeline_event_writes_proven_absent"] is False
        assert proof["error_code"] == "PUBLISHED_LOG_WRITE_FAILED"
        assert evidence["no_mutation_proof"]["slurm_status_sync_called"] is True
        assert evidence["no_mutation_proof"]["pipeline_status_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["pipeline_event_writes"] == "unknown_after_attempt"
        assert evidence["no_mutation_proof"]["slurm_submit_called"] is False
        assert evidence["no_mutation_proof"]["slurm_cancellation_called"] is False


def test_sync_cycle_statuses_terminal_skip_promotes_sync_only_scheduler_status(tmp_path: Path) -> None:
    class SyncingRepository(CandidateAndActiveRepository):
        def __init__(self) -> None:
            self.synced = False
            super().__init__(
                {
                    "pipeline_status": "running",
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": "fcst_gfs_2026052106_model_a",
                            "status": "running",
                            "stage": "forcing",
                            "slurm_job_id": "7777",
                        }
                    ],
                },
                [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
            )

        def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
            if self.synced:
                return {
                    "pipeline_status": "succeeded",
                    "pipeline_jobs": [
                        {
                            "job_id": "job_forcing",
                            "run_id": kwargs["run_id"],
                            "model_id": kwargs["model_id"],
                            "status": "succeeded",
                            "stage": "publish",
                            "slurm_job_id": "7777",
                        }
                    ],
                }
            return super().candidate_state(**kwargs)

        def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [] if self.synced else super().active_slurm_jobs(**kwargs)

    repository = SyncingRepository()

    class SyncingOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            repository.synced = True
            return [
                {
                    "job_id": "job_forcing",
                    "cycle_id": cycle_id,
                    "slurm_job_id": "7777",
                    "status": "succeeded",
                }
            ]

    orchestrator = SyncingOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert orchestrator.calls == []
    assert result.status == "slurm_status_synced"
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "slurm_status_synced"
        assert evidence["execution_boundary"] == "slurm_status_sync"
        assert evidence["counts"]["submitted_count"] == 0
        assert evidence["counts"]["slurm_status_sync_count"] == 1
        assert evidence["model_run_evidence"] == []
        assert evidence["slurm_cancellation_evidence"] == []
        assert evidence["slurm_status_sync_proof"]["status"] == "synced"
        assert evidence["slurm_status_sync_proof"]["sync_called"] is True
        assert evidence["slurm_status_sync_proof"]["mutation_occurred"] is True
        assert evidence["slurm_status_sync_proof"]["protected_by_pre_execution_evidence"] is True
        assert evidence["slurm_status_sync_proof"]["terminal_update_count"] == 1
        assert evidence["no_mutation_proof"]["slurm_status_sync_called"] is True
        assert evidence["no_mutation_proof"]["pipeline_status_writes"] is True
        assert evidence["no_mutation_proof"]["pipeline_event_writes"] is True
        assert evidence["no_mutation_proof"]["slurm_submit_called"] is False
        assert evidence["no_mutation_proof"]["slurm_cancellation_called"] is False
        assert evidence["skipped_candidates"][0]["reason"] == "terminal_pipeline_success"
        sync = evidence["skipped_candidates"][0]["state_evidence"]["slurm_state_sync"]
        assert sync["terminal_updates"][0]["status"] == "succeeded"


@pytest.mark.parametrize(
    "hydro_status",
    ["failed", "cancelled", "submission_failed", "permanently_failed"],
)
def test_terminal_failed_or_cancelled_hydro_state_remains_candidate(
    tmp_path: Path,
    hydro_status: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeHydroStateRepository(hydro_status=hydro_status)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert len(result.evidence["candidates"]) == 1
    assert result.evidence["counts"]["submitted_count"] == 1
    assert len(orchestrator.calls) == 1


@pytest.mark.parametrize("hydro_status", ["created", "staged", "submitted", "running"])
def test_active_hydro_state_is_skipped_as_active(
    tmp_path: Path,
    hydro_status: str,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeHydroStateRepository(hydro_status=hydro_status)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_duplicate_pipeline"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


@pytest.mark.parametrize("job_status", ["pending", "submitted", "running"])
def test_active_cycle_pipeline_job_is_skipped_as_active(tmp_path: Path, job_status: str) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeHydroStateRepository(hydro_status="failed", pipeline_status=job_status)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["candidates"] == []
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_duplicate_pipeline"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


def test_unsubmitted_auto_retry_placeholder_does_not_keep_hydro_created_active(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeCandidateStateRepository(
        {
            "hydro_status": "created",
            "pipeline_status": "pending",
            "pipeline_jobs": [
                {
                    "job_id": "job_cycle_gfs_2026052106_model_a_forecast_retry_1",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "failed",
                    "stage": "forecast",
                    "retry_count": 1,
                    "slurm_job_id": "1001",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_model_a_forecast_retry_1_retry_2",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "failed",
                    "stage": "forecast",
                    "retry_count": 2,
                    "slurm_job_id": "1002",
                },
                {
                    "job_id": "job_cycle_gfs_2026052106_model_a_forecast_retry_1_retry_2_retry_3",
                    "run_id": "fcst_gfs_2026052106_model_a",
                    "status": "pending",
                    "stage": "forecast",
                    "retry_count": 3,
                    "slurm_job_id": None,
                },
            ],
        }
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert [item["model_id"] for item in result.evidence["candidates"]] == ["model_a"]
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls[0]["basins"][0]["model_id"] == "model_a"


@pytest.mark.parametrize(
    "job_status",
    ["succeeded", "partially_failed", "failed", "cancelled", "submission_failed", "permanently_failed", None],
)
def test_terminal_or_missing_pipeline_job_is_not_active(tmp_path: Path, job_status: str | None) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    active_repository = FakeHydroStateRepository(hydro_status="failed", pipeline_status=job_status)
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=active_repository,
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.evidence["skipped_candidates"] == []
    assert len(result.evidence["candidates"]) == 1
    assert result.evidence["counts"]["submitted_count"] == 1
    assert len(orchestrator.calls) == 1


def test_default_non_dry_run_blocks_before_mutation_without_safe_preflight(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["status"] == "preflight_blocked"
    assert result.evidence["execution_mode"] == "production_orchestration"
    assert result.evidence["execution_boundary"] == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    evidence = result.evidence["model_run_evidence"][0]
    assert evidence["status"] == "preflight_blocked"
    assert evidence["submitted"] is False
    assert evidence["mutation_occurred"] is False
    assert evidence["error_code"] == "PRODUCTION_PREFLIGHT_UNSUPPORTED"
    assert "output_uri" not in evidence


def test_non_dry_run_qhh_candidate_executes_generic_m3_chain_without_qhh_scripts(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    orchestrator = FakeProductionOrchestrator()
    config = _config(tmp_path, now=now, dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(
            [
                _model(
                    "basins_qhh_shud",
                    "basins_qhh",
                    resource_profile={
                        "runnable": True,
                        "memory_gb": 128,
                        "station_count": 386,
                        "display_capabilities": {"tiles": True, "optional_weather_available": False},
                    },
                )
            ]
        ),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["status"] == "submitted"
    assert result.evidence["execution_mode"] == "production_orchestration"
    assert result.evidence["execution_boundary"] == "production_orchestration"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is True
    assert result.evidence["model_run_evidence"][0]["standard_chain_shape"] == [stage.stage for stage in M3_STAGES]
    assert result.evidence["model_run_evidence"][0]["qhh_script_invoked"] is False
    assert result.evidence["model_run_evidence"][0]["output_key"] == (
        "runs/fcst_gfs_2026052106_basins_qhh_shud/output/"
    )
    assert result.evidence["model_run_evidence"][0]["output_uri"] == (
        "s3://nhms/runs/fcst_gfs_2026052106_basins_qhh_shud/output/"
    )
    assert result.evidence["model_run_evidence"][0]["submitted"] is True
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["candidate_id"] == ("gfs:2026-05-21T06:00:00Z:basins_qhh_shud:forecast_gfs_deterministic")
    assert submitted_basin["run_id"] == "fcst_gfs_2026052106_basins_qhh_shud"
    assert submitted_basin["forcing_version_id"] == "forc_gfs_2026052106_basins_qhh_shud"
    assert submitted_basin["model_package_uri"] == "s3://nhms/models/basins_qhh_shud/package/"
    assert submitted_basin["station_count"] == 386
    assert submitted_basin["display_capabilities"]["tiles"] is True
    assert submitted_basin["display_capabilities"]["optional_weather_available"] is False
    assert submitted_basin["optional_weather_available"] is False
    assert submitted_basin["output_key"] == "runs/fcst_gfs_2026052106_basins_qhh_shud/output/"
    assert submitted_basin["output_uri"] == "s3://nhms/runs/fcst_gfs_2026052106_basins_qhh_shud/output/"


def test_non_dry_run_output_uri_unavailable_sibling_is_terminal_preflight_evidence(
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    submitted_model = _model("model_a", "basin_a")
    submitted_model["resource_profile"] = {
        **submitted_model["resource_profile"],
        "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
    }
    orchestrator = FakeProductionOrchestrator(expose_object_store=False)
    config = _config(tmp_path, now=now, dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([submitted_model, _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence = result.evidence["model_run_evidence"]
    evidence_counts = {item["candidate_id"]: 0 for item in result.evidence["candidates"]}
    for item in evidence:
        evidence_counts[item["candidate_id"]] += 1
    evidence_by_model = {item["model_id"]: item for item in evidence}
    submitted = evidence_by_model["model_a"]
    blocked = evidence_by_model["model_b"]
    assert len(evidence) == 2
    assert set(evidence_counts.values()) == {1}
    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    assert submitted["status"] == "complete"
    assert submitted["submitted"] is True
    assert submitted["mutation_occurred"] is True
    assert blocked["status"] == "blocked"
    assert blocked["submitted"] is False
    assert blocked["mutation_occurred"] is False
    assert blocked["error_code"] == "OUTPUT_URI_UNAVAILABLE"
    assert "pipeline_run_id" not in blocked
    assert len(orchestrator.calls) == 1
    assert [basin["model_id"] for basin in orchestrator.calls[0]["basins"]] == ["model_a"]


@pytest.mark.parametrize(
    ("resource_profile", "secret_text"),
    [
        ({"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"}, "supersecret"),
        ({"database_uri": "postgresql://nhms@db.prod.example/nhms"}, "database_uri"),
        ({"manifest_uri": "s3://bucket/manifests/model_a.json?token=supersecret"}, "supersecret"),
    ],
)
def test_slurm_scheduler_rejects_secret_candidate_manifest_before_orchestrator_submission(
    tmp_path: Path,
    resource_profile: dict[str, Any],
    secret_text: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["resource_profile"] = {
        **model["resource_profile"],
        **resource_profile,
    }
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert result.evidence["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED"
    assert secret_text not in json.dumps(result.evidence)
    assert orchestrator.calls == []


def test_slurm_scheduler_rejects_secret_output_uri_before_orchestrator_submission(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["resource_profile"] = {
        **model["resource_profile"],
        "output_uri": "s3://bucket/runs/model_a?token=supersecret",
    }
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence = result.evidence["model_run_evidence"][0]
    assert result.status == "preflight_blocked"
    assert evidence["error_code"] == "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED"
    assert "supersecret" not in json.dumps(result.evidence)
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    ("model_package_uri", "secret_text"),
    [
        (
            "s3://user:supersecret@bucket/models/model_a/package/",
            "s3://user:supersecret@bucket/models/model_a/package/",
        ),
        (
            "s3://bucket/models/model_a/package/?token=supersecret",
            "token=supersecret",
        ),
    ],
)
def test_slurm_scheduler_scans_raw_model_package_uri_before_orchestrator_submission(
    tmp_path: Path,
    model_package_uri: str,
    secret_text: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["model_package_uri"] = model_package_uri
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence_text = json.dumps(result.evidence)

    assert result.status == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED"
    assert result.evidence["model_run_evidence"][0]["model_package_uri"] == "[redacted]"
    assert result.evidence["model_run_evidence"][0]["model_package_manifest_uri"] == "[redacted]"
    assert secret_text not in evidence_text
    assert "supersecret" not in evidence_text
    assert orchestrator.calls == []


def test_slurm_scheduler_rejects_derived_secret_model_package_manifest_uri_before_orchestrator_submission(
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["model_package_uri"] = "s3://bucket/models/model_a/package?X-Amz-Signature=supersecret"
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence_text = json.dumps(result.evidence)

    assert result.status == "preflight_blocked"
    assert result.evidence["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED"
    assert result.evidence["model_run_evidence"][0]["model_package_uri"] == "[redacted]"
    assert result.evidence["model_run_evidence"][0]["model_package_manifest_uri"] == "[redacted]"
    assert "supersecret" not in evidence_text
    assert "X-Amz-Signature" not in evidence_text
    assert orchestrator.calls == []


def test_slurm_scheduler_rejects_resource_profile_secret_key_before_orchestrator_submission(
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    raw_key = "s3://bucket/path?token=supersecret"
    model = _model("model_a", "basin_a")
    model["resource_profile"] = {
        **model["resource_profile"],
        raw_key: "signed",
    }
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence_text = json.dumps(result.evidence)
    blockers = result.evidence["model_run_evidence"][0]["residual_blockers"]

    assert result.status == "preflight_blocked"
    assert result.evidence["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED"
    assert any(blocker["field"].endswith("[redacted]") for blocker in blockers)
    assert raw_key not in evidence_text
    assert "supersecret" not in evidence_text
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    "resource_profile_update",
    [
        {"partition": "compute --account=vip"},
        {"account": "friends --qos=high"},
        {"nodes": "1 --exclusive"},
        {"ntasks": "1 --exclusive"},
        {"cpus_per_task": "2 --hint=nomultithread"},
        {"memory_gb": "8 --mem-per-cpu=8G"},
        {"walltime": "01:00:00 --qos=high"},
        {"max_concurrent": "2 --array=0-999"},
        {"shud_threads": "8 --export=ALL"},
    ],
)
def test_slurm_scheduler_rejects_resource_profile_directive_injection_before_orchestrator_submission(
    tmp_path: Path,
    resource_profile_update: dict[str, Any],
) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["resource_profile"] = {
        **model["resource_profile"],
        **resource_profile_update,
    }
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence = result.evidence["model_run_evidence"][0]
    evidence_text = json.dumps(result.evidence)

    assert result.status == "preflight_blocked"
    assert evidence["error_code"] == "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert "--" not in evidence_text
    assert "exclusive" not in evidence_text
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    "collision_key",
    [
        "run_id",
        "workspace_dir",
        "stage_name",
        "cycle_id",
        "object_store_root",
        "object_store_prefix",
        "manifest_index_path",
    ],
)
def test_slurm_scheduler_rejects_resource_profile_identity_collision_before_orchestrator_submission(
    tmp_path: Path,
    collision_key: str,
) -> None:
    roots = _slurm_roots(tmp_path)
    model = _model("model_a", "basin_a")
    model["resource_profile"] = {
        **model["resource_profile"],
        collision_key: "profile_override",
    }
    orchestrator = StrictNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    evidence = result.evidence["model_run_evidence"][0]

    assert result.status == "preflight_blocked"
    assert evidence["error_code"] == "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert {
        "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
        "field": f"resource_profile.{collision_key}",
        "message": "Slurm resource profile cannot override manifest or template identity fields.",
        "reason": "manifest_identity_collision",
    } in evidence["slurm_preflight"]["blockers"]
    assert orchestrator.calls == []


def test_slurm_scheduler_preserves_safe_manifest_fields_and_allowed_env(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    safe_package_uri = "s3://nhms-safe/models/model_a/package/"
    safe_resource_profile = {
        "runnable": True,
        "memory_gb": 8,
        "station_count": 7,
        "output_uri": "s3://nhms-safe/runs/model_a/output/",
        "manifest_uri": "s3://nhms-safe/models/model_a/manifest.json",
        "display_capabilities": {"tiles": True},
        "custom_metadata": {"callback_uri": "https://example.com/notify", "safe_key": "safe/value"},
    }
    model = _model(
        "model_a",
        "basin_a",
        resource_profile=safe_resource_profile,
    )
    model["model_package_uri"] = safe_package_uri
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={"NHMS_PROFILE": "prod/gfs_00"},
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert result.status == "submitted"
    assert submitted_basin["station_count"] == 7
    assert submitted_basin["model_package_uri"] == safe_package_uri
    assert submitted_basin["model_package_manifest_uri"] == "s3://nhms-safe/models/model_a/manifest.json"
    assert submitted_basin["resource_profile"] == safe_resource_profile
    assert submitted_basin["output_uri"] == "s3://nhms-safe/runs/model_a/output/"
    assert submitted_basin["slurm_env"] == {"NHMS_PROFILE": "prod/gfs_00"}
    assert "DATABASE_URL" not in submitted_basin


def test_non_dry_run_partial_cycle_marks_failed_candidate_without_fanning_success(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    orchestrator = FakeProductionOrchestrator(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forcing",
            },
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_b",
                "model_id": "model_b",
                "status": "failed",
                "stage": "forcing",
                "reason": "forcing_task_failed",
                "slurm_job_id": "slurm_forcing_1",
                "exit_code": 1,
            },
        ),
        result_status="parsed_partial",
    )
    config = _config(tmp_path, now=now, dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence_by_model = {item["model_id"]: item for item in result.evidence["model_run_evidence"]}
    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["partial_count"] == 1
    assert evidence_by_model["model_a"]["status"] == "parsed_partial"
    assert evidence_by_model["model_a"]["submitted"] is True
    assert evidence_by_model["model_a"]["candidate_outcome"]["status"] == "active"
    assert evidence_by_model["model_b"]["status"] == "failed"
    assert evidence_by_model["model_b"]["submitted"] is True
    assert evidence_by_model["model_b"]["execution_attempted"] is True
    assert evidence_by_model["model_b"]["final_candidate_success"] is False
    assert evidence_by_model["model_b"]["mutation_occurred"] is True
    assert evidence_by_model["model_b"]["error_code"] == "FORCING_TASK_FAILED"
    assert evidence_by_model["model_b"]["candidate_outcome"] == {
        "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
        "run_id": "fcst_gfs_2026052106_model_b",
        "model_id": "model_b",
        "status": "failed",
        "stage": "forcing",
        "reason": "forcing_task_failed",
        "slurm_job_id": "slurm_forcing_1",
        "exit_code": 1,
    }


@pytest.mark.parametrize("outcome_status", ["submission_failed", "permanently_failed"])
def test_non_dry_run_partial_cycle_counts_failed_alias_candidate_as_failed(
    tmp_path: Path,
    outcome_status: str,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    sibling_reason = f"forcing_task_{outcome_status}"
    orchestrator = FakeProductionOrchestrator(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forcing",
            },
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_b",
                "model_id": "model_b",
                "status": outcome_status,
                "stage": "forcing",
                "reason": sibling_reason,
                "slurm_job_id": "slurm_forcing_1",
                "exit_code": 1,
            },
        ),
        result_status="parsed_partial",
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["submitted_count"] == 2
    assert result.evidence["counts"]["failed_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    evidence_by_model = {item["model_id"]: item for item in result.evidence["model_run_evidence"]}
    assert evidence_by_model["model_b"]["status"] == outcome_status
    assert evidence_by_model["model_b"]["submitted"] is True
    assert evidence_by_model["model_b"]["execution_attempted"] is True
    assert evidence_by_model["model_b"]["candidate_outcome"]["status"] == outcome_status
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))
    persisted_by_model = {item["model_id"]: item for item in persisted["model_run_evidence"]}
    assert persisted["counts"]["failed_count"] == 1
    assert persisted["counts"]["partial_count"] == 1
    assert persisted_by_model["model_b"]["status"] == outcome_status


@pytest.mark.parametrize("outcome_status", ["unavailable", "cancelled"])
def test_non_dry_run_partial_cycle_marks_unavailable_or_cancelled_candidate_as_partial(
    tmp_path: Path,
    outcome_status: str,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    sibling_reason = f"forcing_task_{outcome_status}"
    orchestrator = FakeProductionOrchestrator(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forcing",
            },
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_b",
                "model_id": "model_b",
                "status": outcome_status,
                "stage": "forcing",
                "reason": sibling_reason,
                "slurm_job_id": "slurm_forcing_1",
                "exit_code": 1,
            },
        ),
        result_status="parsed_partial",
    )
    config = _config(tmp_path, now=now, dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence_by_model = {item["model_id"]: item for item in result.evidence["model_run_evidence"]}
    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["submitted_count"] == 2
    assert result.evidence["counts"]["partial_count"] == 1
    assert evidence_by_model["model_a"]["status"] == "parsed_partial"
    assert evidence_by_model["model_a"]["submitted"] is True
    assert evidence_by_model["model_a"]["candidate_outcome"]["status"] == "active"
    assert evidence_by_model["model_b"]["status"] == outcome_status
    assert evidence_by_model["model_b"]["submitted"] is True
    assert evidence_by_model["model_b"]["execution_attempted"] is True
    assert evidence_by_model["model_b"]["final_candidate_success"] is False
    assert evidence_by_model["model_b"]["mutation_occurred"] is True
    assert evidence_by_model["model_b"]["error_code"] == sibling_reason.upper()
    assert evidence_by_model["model_b"]["candidate_outcome"]["status"] == outcome_status


def test_scheduler_evidence_redacts_signed_candidate_outcome_log_uri(tmp_path: Path) -> None:
    secret_log_uri = "s3://nhms/runs/cycle/logs/2003_0.out?X-Amz-Signature=supersecret"
    orchestrator = FakeProductionOrchestrator(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "failed",
                "stage": "forcing",
                "reason": "forcing_task_failed",
                "log_uri": secret_log_uri,
                "error_message": "failed token=rawsecret url=https://user:pass@example.test/log?signature=abc",
            },
        ),
        result_status="parsed_partial",
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    outcome = result.evidence["model_run_evidence"][0]["candidate_outcome"]
    evidence_text = json.dumps(result.evidence)
    assert outcome["log_uri"] == "s3://nhms/runs/cycle/logs/2003_0.out"
    assert "supersecret" not in evidence_text
    assert "rawsecret" not in evidence_text
    assert "user:pass" not in evidence_text


def test_scheduler_evidence_redacts_sensitive_runtime_payloads(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    secret_database_url = "postgresql://nhms:supersecret@db.prod.example/nhms"
    secret_slurm_value = "s3://bucket/prod?X-Amz-Signature=supersecret"
    orchestrator = FakeProductionOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url=secret_database_url,
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        slurm_env={
            "DATABASE_URL": secret_database_url,
            "OBJECT_STORE_PREFIX": secret_slurm_value,
            "AWS_SECRET_ACCESS_KEY": "supersecret",
        },
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    evidence_text = json.dumps(result.evidence)
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "slurm_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    assert "supersecret" not in evidence_text
    assert secret_database_url not in evidence_text
    assert secret_slurm_value not in evidence_text
    assert "AWS_SECRET_ACCESS_KEY" not in evidence_text


def test_issue_196_dry_run_evidence_has_stable_non_final_review_contract(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        now=_dt("2026-05-21T12:00:00Z"),
        model_ids=("model_a",),
        basin_ids=("basin_a",),
    )
    adapter = FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": adapter},
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert result.evidence["schema_version"] == SCHEDULER_EVIDENCE_SCHEMA_VERSION
    assert result.evidence["review_contract"]["github_issue"] == SCHEDULER_EVIDENCE_GITHUB_ISSUE
    assert result.evidence["execution_mode"] == "dry_run"
    assert result.evidence["readiness_interpretation"] == "deterministic_review_only"
    assert result.evidence["readiness"]["production_ready"] is False
    assert result.evidence["readiness"]["final_production_readiness_claimed"] is False
    assert len(result.evidence["candidates"]) == 1
    assert result.evidence["blocked_candidates"] == []
    assert result.evidence["skipped_candidates"] == []
    expected_counts = {
        "candidate_count": 1,
        "blocked_candidate_count": 0,
        "skipped_candidate_count": 0,
        "selected_model_count": 1,
        "source_cycle_count": 1,
        "submitted_count": 0,
        "failed_count": 0,
        "partial_count": 0,
    }
    counts = result.evidence["counts"]
    assert {name: counts[name] for name in expected_counts} == expected_counts
    assert counts["slurm_status_sync_count"] == 0
    assert counts["slurm_cancelled_count"] == 0
    assert counts["slurm_cancellation_blocked_count"] == 0
    assert result.evidence["filters"] == result.evidence["operator_filters"]
    assert result.evidence["candidates"][0]["source_id"] == "gfs"
    assert result.evidence["candidates"][0]["cycle_id"] == "gfs_2026052106"
    assert result.evidence["candidates"][0]["model_id"] == "model_a"
    assert result.evidence["artifact_path"] == str(result.artifact_path)
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert adapter.download_calls == 0
    assert persisted["schema_version"] == result.evidence["schema_version"]
    assert persisted["readiness"]["final_production_readiness_claimed"] is False


def test_issue_196_submitted_model_run_evidence_includes_artifacts_resources_and_quality(
    tmp_path: Path,
) -> None:
    orchestrator = FakeProductionOrchestratorWithStageEvidence(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forecast",
                "parsed_row_count": 21,
                "accounting": {"elapsed": "00:03:00", "max_rss": "3072K"},
            },
        )
    )
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(
            [
                _model(
                    "model_a",
                    "basin_a",
                    resource_profile={
                        "runnable": True,
                        "memory_gb": 16,
                        "station_count": 2,
                        "station_ids": ["sta_001", "sta_002"],
                        "parsed_row_count": 10,
                        "display_capabilities": {"tiles": True, "optional_weather_available": False},
                    },
                )
            ]
        ),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    model_evidence = result.evidence["model_run_evidence"][0]

    assert result.status == "submitted"
    assert model_evidence["schema_version"] == MODEL_RUN_EVIDENCE_SCHEMA_VERSION
    assert model_evidence["review_contract"]["github_issue"] == SCHEDULER_EVIDENCE_GITHUB_ISSUE
    assert model_evidence["artifact_refs"]["model_package_manifest_uri"] == "s3://nhms/models/model_a/manifest.json"
    assert model_evidence["artifact_refs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/"
    assert model_evidence["stage_statuses"][0]["accounting"]["elapsed"] == "00:03:00"
    assert model_evidence["stage_statuses"][0]["resource_metrics"]["max_rss"] == "3072K"
    assert model_evidence["resource_summary"]["candidate_resource_metrics"]["max_rss"] == "3072K"
    assert model_evidence["forcing"]["station_count"] == 2
    assert model_evidence["outputs"]["parsed_row_count"] == 21
    assert model_evidence["outputs"]["segment_count"] == 3
    assert model_evidence["display"]["unavailable_products"] == ["optional_weather_products"]
    assert model_evidence["quality_states"]["display"]["unavailable_products"] == ["optional_weather_products"]
    assert result.evidence["readiness"]["final_production_readiness_claimed"] is False


def test_issue_196_partial_and_blocked_model_run_evidence_redacts_secrets_and_records_blockers(
    tmp_path: Path,
) -> None:
    secret_log_uri = "s3://nhms/logs/forcing.out?X-Amz-Signature=supersecret"
    orchestrator = FakeProductionOrchestratorWithStageEvidence(
        stage_status="partially_failed",
        stage_error_message="failed token=rawsecret url=https://user:pass@example.test/log?signature=abc",
        stage_log_uri=secret_log_uri,
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forcing",
            },
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_b",
                "model_id": "model_b",
                "status": "failed",
                "stage": "forcing",
                "reason": "forcing_task_failed",
                "slurm_job_id": "slurm_forcing_1",
                "exit_code": 1,
                "log_uri": secret_log_uri,
                "error_message": "failed token=rawsecret",
            },
        ),
        result_status="parsed_partial",
    )
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    failed = next(item for item in result.evidence["model_run_evidence"] if item["model_id"] == "model_b")
    evidence_text = json.dumps(result.evidence)
    artifact_text = Path(result.artifact_path or "").read_text(encoding="utf-8")

    assert result.status == "submitted_partial"
    assert result.evidence["counts"]["partial_count"] == 1
    assert failed["status"] == "failed"
    assert failed["artifact_refs"]["output_uri"] == "s3://nhms/runs/fcst_gfs_2026052106_model_b/output/"
    assert failed["candidate_outcome"]["log_uri"] == "s3://nhms/logs/forcing.out"
    assert failed["stage_statuses"][0]["log_uri"] == "s3://nhms/logs/forcing.out"
    assert failed["stage_statuses"][0]["task_results_summary"] == {
        "total_count": 1,
        "included_count": 1,
        "omitted_count": 0,
        "matched_count": 1,
        "matching": "candidate_identity",
        "limit": MAX_MODEL_RUN_STAGE_TASK_ROWS,
        "status_counts": {"succeeded": 1},
    }
    assert failed["resource_summary"]["stage_accounting"][0]["accounting"]["max_rss"] == "3072K"
    assert len(failed["resource_summary"]["task_accounting"]) == 1
    assert failed["resource_summary"]["task_accounting"][0]["slurm_job_id"] == "slurm_forcing_0"
    assert any(blocker["code"] == "FORCING_TASK_FAILED" for blocker in failed["residual_blockers"])
    for raw_secret in ("supersecret", "rawsecret", "user:pass", "signature=abc", "X-Amz-Signature"):
        assert raw_secret not in evidence_text
        assert raw_secret not in artifact_text


def test_model_run_evidence_bounds_unmatched_large_array_task_rows(tmp_path: Path) -> None:
    class LargeArrayOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            task_results = tuple(
                {
                    "task_id": index,
                    "array_task_id": index,
                    "status": "succeeded",
                    "slurm_job_id": f"slurm_forcing_{index}",
                    "accounting": {"elapsed": "00:01:00", "max_rss": f"{1024 + index}K"},
                }
                for index in range(200)
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=(
                    StageRunResult(
                        stage="forcing",
                        job_type="produce_forcing_array",
                        pipeline_job_id="job_forcing",
                        slurm_job_id="slurm_forcing",
                        status="succeeded",
                        task_results=task_results,
                    ),
                ),
            )

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry(
            [
                _model("model_a", "basin_a"),
                _model("model_b", "basin_b"),
                _model("model_c", "basin_c"),
            ]
        ),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: LargeArrayOrchestrator(),
    )

    result = scheduler.run_once()

    model_evidence = result.evidence["model_run_evidence"]
    assert result.status == "submitted"
    assert len(model_evidence) == 3
    assert sum(len(item["stage_statuses"][0]["task_results"]) for item in model_evidence) == (
        3 * MAX_MODEL_RUN_STAGE_TASK_ROWS
    )
    assert sum(len(item["resource_summary"]["task_accounting"]) for item in model_evidence) == (
        3 * MAX_MODEL_RUN_STAGE_TASK_ROWS
    )
    for item in model_evidence:
        stage = item["stage_statuses"][0]
        summary = stage["task_results_summary"]
        assert len(stage["task_results"]) == MAX_MODEL_RUN_STAGE_TASK_ROWS
        assert summary["total_count"] == 200
        assert summary["included_count"] == MAX_MODEL_RUN_STAGE_TASK_ROWS
        assert summary["omitted_count"] == 200 - MAX_MODEL_RUN_STAGE_TASK_ROWS
        assert summary["matching"] == "bounded_sample"
        assert summary["limit"] == MAX_MODEL_RUN_STAGE_TASK_ROWS


def test_model_run_evidence_keeps_only_candidate_matched_large_array_task_rows(tmp_path: Path) -> None:
    class CandidateMatchedArrayOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            task_results = tuple(
                {
                    "task_id": index,
                    "array_task_id": index,
                    "candidate_id": basin["candidate_id"],
                    "run_id": basin["run_id"],
                    "model_id": basin["model_id"],
                    "status": "succeeded",
                    "slurm_job_id": f"slurm_forcing_{index}",
                    "accounting": {"elapsed": "00:01:00", "max_rss": f"{2048 + index}K"},
                }
                for index, basin in enumerate(basins)
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=(
                    StageRunResult(
                        stage="forcing",
                        job_type="produce_forcing_array",
                        pipeline_job_id="job_forcing",
                        slurm_job_id="slurm_forcing",
                        status="succeeded",
                        task_results=task_results,
                    ),
                ),
            )

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry(
            [
                _model("model_a", "basin_a"),
                _model("model_b", "basin_b"),
                _model("model_c", "basin_c"),
            ]
        ),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: CandidateMatchedArrayOrchestrator(),
    )

    result = scheduler.run_once()

    model_evidence = result.evidence["model_run_evidence"]
    assert len(model_evidence) == 3
    for item in model_evidence:
        stage = item["stage_statuses"][0]
        summary = stage["task_results_summary"]
        assert len(stage["task_results"]) == 1
        assert stage["task_results"][0]["candidate_id"] == item["candidate_id"]
        assert (
            item["resource_summary"]["task_accounting"][0]["slurm_job_id"] == (stage["task_results"][0]["slurm_job_id"])
        )
        assert summary["total_count"] == 1
        assert summary["matched_count"] == 1
        assert summary["matching"] == "candidate_identity"


def test_model_run_evidence_caps_all_candidate_matched_array_task_rows(tmp_path: Path) -> None:
    class ManyMatchedArrayOrchestrator(FakeProductionOrchestrator):
        def orchestrate_cycle(
            self,
            source: str,
            cycle_time: datetime,
            basins: list[dict[str, Any]],
        ) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            basin = basins[0]
            task_count = MAX_MODEL_RUN_STAGE_TASK_ROWS + 9
            task_results = tuple(
                {
                    "task_id": index,
                    "array_task_id": index,
                    "candidate_id": basin["candidate_id"],
                    "run_id": basin["run_id"],
                    "model_id": basin["model_id"],
                    "status": "succeeded",
                    "slurm_job_id": f"slurm_forcing_{index}",
                    "accounting": {"elapsed": "00:01:00", "max_rss": f"{2048 + index}K"},
                }
                for index in range(task_count)
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=(
                    StageRunResult(
                        stage="forcing",
                        job_type="produce_forcing_array",
                        pipeline_job_id="job_forcing",
                        slurm_job_id="slurm_forcing",
                        status="succeeded",
                        task_results=task_results,
                    ),
                ),
            )

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: ManyMatchedArrayOrchestrator(),
    )

    result = scheduler.run_once()

    model_evidence = result.evidence["model_run_evidence"][0]
    stage = model_evidence["stage_statuses"][0]
    summary = stage["task_results_summary"]
    total_matching_rows = MAX_MODEL_RUN_STAGE_TASK_ROWS + 9
    assert len(stage["task_results"]) == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert summary["total_count"] == total_matching_rows
    assert summary["included_count"] == MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert summary["matched_count"] == total_matching_rows
    assert summary["omitted_count"] == total_matching_rows - MAX_MODEL_RUN_STAGE_TASK_ROWS
    assert summary["matching"] == "candidate_identity"
    assert len(model_evidence["resource_summary"]["task_accounting"]) == MAX_MODEL_RUN_STAGE_TASK_ROWS


def test_issue_196_blocked_preflight_evidence_keeps_existing_consumers_stable(tmp_path: Path) -> None:
    roots = _slurm_roots(tmp_path)
    scheduler = ProductionScheduler(
        _config(
            roots["workspace_root"],
            now=_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
            slurm_execution_enabled=True,
            database_url=None,
            object_store_root=roots["object_store_root"],
            log_root=roots["log_root"],
            runtime_root=roots["runtime_root"],
            allowed_storage_roots=(tmp_path,),
        ),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: StrictNoSubmitOrchestrator(),
    )

    result = scheduler.run_once()
    payload = result.to_dict()
    model_evidence = payload["model_run_evidence"][0]

    assert payload["status"] == "preflight_blocked"
    assert payload["counts"]["submitted_count"] == 0
    assert payload["readiness"]["final_production_readiness_claimed"] is False
    assert model_evidence["schema_version"] == MODEL_RUN_EVIDENCE_SCHEMA_VERSION
    assert model_evidence["error_code"] == "SLURM_PREFLIGHT_DATABASE_URL_MISSING"
    assert model_evidence["submitted"] is False
    assert model_evidence["mutation_occurred"] is False
    assert model_evidence["artifact_refs"]["output_key"] == "runs/fcst_gfs_2026052106_model_a/output/"
    assert model_evidence["residual_blockers"][0]["quality_flag"] == "slurm_preflight_blocked"


def test_plan_production_public_slurm_path_rejects_pipeline_database_url_only(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    (roots["workspace_root"] / "scheduler" / "evidence").mkdir(parents=True)
    log_root = roots["runtime_root"] / "slurm-logs"
    log_root.mkdir()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://nhms:secret@db.prod.example/nhms")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ENABLED", "1")
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setenv("SLURM_SHARED_LOG_ROOT", str(log_root))
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://slurm-gateway.internal:8000")
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model("model_a", "basin_a")]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._canonical_readiness_provider_from_env",
        lambda: FakeCanonicalReadinessProvider(
            {
                ("gfs", _dt("2026-05-21T06:00:00Z")): {
                    "status": "canonical_ready",
                    "ready": True,
                }
            }
        ),
    )
    monkeypatch.setattr(
        scheduler_module,
        "_now",
        lambda _config: _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=(),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=(),
        basin_ids=(),
        dry_run=False,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=str(roots["workspace_root"]),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["counts"]["submitted_count"] == 0
    assert payload["runtime_config"]["require_runtime_roots"] is True
    assert payload["root_preflight"]["status"] == "ready"
    assert payload["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_DATABASE_URL_MISSING"


def test_plan_production_no_flag_uses_env_roots_and_records_runtime_evidence(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    monkeypatch.chdir(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model("model_a", "basin_a")]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )

    resolved_roots = payload["resolved_runtime_roots"]
    runtime_config = payload["runtime_config"]
    assert payload["status"] == "planned"
    assert payload["dry_run"] is True
    assert payload["execution_mode"] == "dry_run"
    assert payload["root_preflight"]["status"] == "ready"
    assert resolved_roots["workspace_root"]["path"] == str(roots["workspace_root"].resolve())
    assert resolved_roots["object_store_root"]["path"] == str(roots["object_store_root"].resolve())
    assert resolved_roots["published_artifact_root"]["path"] == str(roots["published_root"].resolve())
    assert resolved_roots["lock_root"]["path"] == str(roots["lock_root"].resolve())
    assert resolved_roots["evidence_root"]["path"] == str(roots["evidence_root"].resolve())
    assert resolved_roots["runtime_root"]["path"] == str(roots["runtime_root"].resolve())
    assert resolved_roots["temp_root"]["path"] == str(roots["temp_root"].resolve())
    assert runtime_config["service_role"] == "compute_control"
    assert runtime_config["require_runtime_roots"] is True
    assert runtime_config["dry_run"] is True
    assert runtime_config["sources"] == ["gfs"]
    assert runtime_config["model_ids"] == ["model_a"]
    assert runtime_config["basin_ids"] == ["basin_a"]
    assert runtime_config["lookback_hours"] == 24
    assert runtime_config["cycle_lag_hours"] == 0
    assert payload["counts"]["submitted_count"] == 0
    assert not (tmp_path / ".nhms-workspace").exists()


def test_plan_production_plan_flag_is_no_mutation_alias(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned", "dry_run": self.config.dry_run})

    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setenv("NHMS_SCHEDULER_SOURCES", "gfs")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--plan"])

    assert rc == 0
    assert captured["config"].dry_run is True
    assert captured["config"].require_runtime_roots is True


def test_plan_production_submit_flag_enables_mutation_path(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned", "dry_run": self.config.dry_run})

    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setenv("NHMS_SCHEDULER_SOURCES", "gfs")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--submit"])

    assert rc == 0
    assert captured["config"].dry_run is False
    assert captured["config"].require_runtime_roots is True


def test_docs_reserve_plan_for_no_mutation_and_use_submit_for_production_submission() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    production_sections = {
        "docs/VALIDATION.md": (
            "Production submission uses the same backend scheduler entrypoint",
            "Slurm mode rejects missing or localhost-only",
        ),
        "docs/runbooks/qhh-continuous.md": (
            "生产提交路径使用同一个 backend scheduler",
            "该生产路径负责所有 active runnable 注册模型",
        ),
        "docs/runbooks/qhh-mvp-production-like-e2e-checklist.md": (
            "### 10.1 生产模式 plan-production",
            "### 10.2 pipeline job 持久化",
        ),
    }

    for relative_path, (start_marker, end_marker) in production_sections.items():
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        section = text[start:end]

        assert "--submit" in section, relative_path
        assert "--plan" not in section, relative_path

    reservation_text = "\n".join(
        (repo_root / relative_path).read_text(encoding="utf-8") for relative_path in production_sections
    )
    assert "--plan" in reservation_text
    assert "dry-run/no-mutation" in reservation_text


def test_plan_production_missing_workspace_root_no_flag_errors_without_app_workspace(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    with pytest.raises(ValueError, match="WORKSPACE_ROOT"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=None,
            lock_path=None,
            evidence_dir=None,
        )

    assert not (tmp_path / ".nhms-workspace").exists()
    assert not (tmp_path / "scheduler").exists()


def test_no_flag_missing_allowed_roots_blocks_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.delenv("NHMS_SCHEDULER_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("missing scheduler allowlist must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["counts"]["submitted_count"] == 0
    assert payload["root_preflight"]["checks"]["allowed_roots_policy"] == {
        "env": "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "configured": False,
        "non_empty": False,
        "allowed_roots": [],
        "independent_policy_required": True,
    }
    assert "SCHEDULER_ROOT_ALLOWED_ROOTS_MISSING" in {
        blocker["code"] for blocker in payload["root_preflight"]["blockers"]
    }
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    assert Path(payload["artifact_path"]).is_file()


def test_explicit_workspace_submit_missing_allowed_roots_blocks_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    (roots["workspace_root"] / "scheduler" / "evidence").mkdir(parents=True)
    monkeypatch.delenv("NHMS_SCHEDULER_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("explicit-root submit root preflight must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=False,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=str(roots["workspace_root"]),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["runtime_config"]["dry_run"] is False
    assert payload["runtime_config"]["require_runtime_roots"] is True
    assert payload["root_preflight"]["checks"]["allowed_roots_policy"] == {
        "env": "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "configured": False,
        "non_empty": False,
        "allowed_roots": [],
        "independent_policy_required": True,
    }
    assert "SCHEDULER_ROOT_ALLOWED_ROOTS_MISSING" in {
        blocker["code"] for blocker in payload["root_preflight"]["blockers"]
    }
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    assert Path(payload["artifact_path"]).is_file()


@pytest.mark.parametrize(
    ("broken_root", "expected_code"),
    [
        ("workspace_root", "SCHEDULER_ROOT_WORKSPACE_ROOT_NOT_FOUND"),
        ("object_store_root", "SCHEDULER_ROOT_OBJECT_STORE_ROOT_NOT_FOUND"),
        ("runtime_root", "SCHEDULER_ROOT_RUNTIME_ROOT_NOT_FOUND"),
        ("temp_root", "SCHEDULER_ROOT_TEMP_ROOT_NOT_FOUND"),
        ("lock_root", "SCHEDULER_ROOT_LOCK_ROOT_NOT_FOUND"),
        ("evidence_root", "SCHEDULER_ROOT_EVIDENCE_ROOT_NOT_FOUND"),
    ],
)
def test_no_flag_invalid_env_roots_block_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
    broken_root: str,
    expected_code: str,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    broken_path = roots[broken_root]
    shutil.rmtree(broken_path)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("blocked scheduler root preflight must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    assert expected_code in {blocker["code"] for blocker in payload["root_preflight"]["blockers"]}
    if broken_root in {"workspace_root", "evidence_root"}:
        assert "artifact_path" not in payload
    else:
        assert Path(payload["artifact_path"]).is_file()


def test_no_flag_missing_published_artifact_root_is_created_by_control_publish_stage(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    shutil.rmtree(roots["published_root"])
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model("model_a", "basin_a")]),
    )
    adapter = FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {"gfs": adapter},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = _run_no_flag_plan()

    assert payload["status"] == "planned"
    published_check = payload["root_preflight"]["checks"]["published_artifact_root"]
    assert payload["root_preflight"]["status"] == "ready"
    assert payload["root_preflight"]["blockers"] == []
    assert published_check["exists"] is False
    assert published_check["allow_create"] is True
    assert published_check["writable"] is True
    assert "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_FOUND" not in {
        blocker["code"] for blocker in payload["root_preflight"]["blockers"]
    }
    assert payload["counts"]["submitted_count"] == 0
    assert payload["execution_boundary"] == "planning_only"
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    assert adapter.download_calls == 0
    assert not roots["published_root"].exists()


@pytest.mark.parametrize(
    ("root_key", "env_key", "expected_code", "safe_evidence"),
    [
        (
            "object_store_root",
            "OBJECT_STORE_ROOT",
            "SCHEDULER_ROOT_OBJECT_STORE_ROOT_OUT_OF_APPROVED_ROOT",
            True,
        ),
        (
            "published_root",
            "NHMS_PUBLISHED_ARTIFACT_ROOT",
            "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_OUT_OF_APPROVED_ROOT",
            True,
        ),
        (
            "runtime_root",
            "NHMS_SCHEDULER_RUNTIME_ROOT",
            "SCHEDULER_ROOT_RUNTIME_ROOT_OUT_OF_APPROVED_ROOT",
            True,
        ),
        (
            "temp_root",
            "NHMS_SCHEDULER_TEMP_ROOT",
            "SCHEDULER_ROOT_TEMP_ROOT_OUT_OF_APPROVED_ROOT",
            True,
        ),
        (
            "workspace_root",
            "WORKSPACE_ROOT",
            "SCHEDULER_ROOT_WORKSPACE_ROOT_OUT_OF_APPROVED_ROOT",
            True,
        ),
    ],
)
def test_no_flag_out_of_approved_roots_block_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
    root_key: str,
    env_key: str,
    expected_code: str,
    safe_evidence: bool,
) -> None:
    roots = _scheduler_env_roots(tmp_path / "approved")
    _set_scheduler_root_env(monkeypatch, roots)
    outside = tmp_path / "outside" / root_key
    outside.mkdir(parents=True)
    monkeypatch.setenv(env_key, str(outside))
    if root_key == "workspace_root":
        lock_root = outside / "locks"
        evidence_root = outside / "evidence"
        lock_root.mkdir()
        evidence_root.mkdir()
        monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(lock_root))
        monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(evidence_root))
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("out-of-approved scheduler root must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "preflight_blocked"
    assert expected_code in {blocker["code"] for blocker in payload["root_preflight"]["blockers"]}
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    if safe_evidence:
        assert Path(payload["artifact_path"]).is_file()
    else:
        assert "artifact_path" not in payload


@pytest.mark.parametrize(
    ("root_key", "expected_code", "safe_evidence"),
    [
        ("object_store_root", "SCHEDULER_ROOT_OBJECT_STORE_ROOT_NOT_DIRECTORY", True),
        ("published_root", "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_DIRECTORY", True),
        ("runtime_root", "SCHEDULER_ROOT_RUNTIME_ROOT_NOT_DIRECTORY", True),
        ("temp_root", "SCHEDULER_ROOT_TEMP_ROOT_NOT_DIRECTORY", True),
        ("lock_root", "SCHEDULER_ROOT_LOCK_ROOT_NOT_DIRECTORY", True),
        ("evidence_root", "evidence_dir must be a directory", False),
        ("workspace_root", "evidence_dir must be a safe directory", False),
    ],
)
def test_no_flag_file_roots_block_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
    root_key: str,
    expected_code: str,
    safe_evidence: bool,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    target = roots[root_key]
    shutil.rmtree(target)
    target.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("file scheduler root must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    if root_key in {"evidence_root", "workspace_root"}:
        with pytest.raises(ValueError, match=expected_code):
            cli._plan_production(
                sources=("gfs",),
                lookback_hours=24,
                cycle_lag_hours=0,
                max_cycles_per_source=1,
                model_ids=("model_a",),
                basin_ids=(),
                dry_run=True,
                continuous=False,
                interval_seconds=300.0,
                max_passes=None,
                workspace_root=None,
                lock_path=None,
                evidence_dir=None,
            )
        return

    payload = _run_no_flag_plan()
    assert payload["status"] == "preflight_blocked"
    assert expected_code in {blocker["code"] for blocker in payload["root_preflight"]["blockers"]}
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    if safe_evidence:
        assert Path(payload["artifact_path"]).is_file()


@pytest.mark.parametrize(
    ("root_key", "expected_code", "safe_evidence"),
    [
        ("object_store_root", "SCHEDULER_ROOT_OBJECT_STORE_ROOT_SYMLINK", True),
        ("published_root", "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_SYMLINK", True),
        ("runtime_root", "SCHEDULER_ROOT_RUNTIME_ROOT_SYMLINK", True),
        ("temp_root", "SCHEDULER_ROOT_TEMP_ROOT_SYMLINK", True),
        ("evidence_root", "evidence_dir must be under workspace_root", False),
        ("workspace_root", "SCHEDULER_ROOT_WORKSPACE_ROOT_SYMLINK", False),
    ],
)
def test_no_flag_symlink_final_component_blocks_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
    root_key: str,
    expected_code: str,
    safe_evidence: bool,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    target = roots[root_key]
    replacement_target = tmp_path / f"{root_key}-symlink-target"
    replacement_target.mkdir()
    shutil.rmtree(target)
    target.symlink_to(replacement_target, target_is_directory=True)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("symlink scheduler root must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    if root_key == "evidence_root":
        with pytest.raises(ValueError, match=expected_code):
            cli._plan_production(
                sources=("gfs",),
                lookback_hours=24,
                cycle_lag_hours=0,
                max_cycles_per_source=1,
                model_ids=("model_a",),
                basin_ids=(),
                dry_run=True,
                continuous=False,
                interval_seconds=300.0,
                max_passes=None,
                workspace_root=None,
                lock_path=None,
                evidence_dir=None,
            )
        return

    payload = _run_no_flag_plan()
    assert payload["status"] == "preflight_blocked"
    assert expected_code in {blocker["code"] for blocker in payload["root_preflight"]["blockers"]}
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    if safe_evidence:
        assert Path(payload["artifact_path"]).is_file()
    else:
        assert "artifact_path" not in payload


@pytest.mark.parametrize(
    ("root_key", "expected_code", "safe_evidence"),
    [
        ("object_store_root", "SCHEDULER_ROOT_OBJECT_STORE_ROOT_NOT_WRITABLE", True),
        ("published_root", "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_WRITABLE", True),
        ("runtime_root", "SCHEDULER_ROOT_RUNTIME_ROOT_NOT_WRITABLE", True),
        ("temp_root", "SCHEDULER_ROOT_TEMP_ROOT_NOT_WRITABLE", True),
        ("lock_root", "SCHEDULER_ROOT_LOCK_ROOT_NOT_WRITABLE", True),
        ("evidence_root", "SCHEDULER_ROOT_EVIDENCE_ROOT_NOT_WRITABLE", False),
        ("workspace_root", "evidence_dir must be a safe directory", False),
    ],
)
def test_no_flag_no_execute_or_not_writable_roots_block_before_registry_adapter_or_submit(
    monkeypatch: Any,
    tmp_path: Path,
    root_key: str,
    expected_code: str,
    safe_evidence: bool,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    target = roots[root_key]
    original_mode = stat.S_IMODE(target.stat().st_mode)
    target.chmod(0o600)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("unusable scheduler root must not construct active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    try:
        if root_key == "workspace_root":
            with pytest.raises(ValueError, match=expected_code):
                cli._plan_production(
                    sources=("gfs",),
                    lookback_hours=24,
                    cycle_lag_hours=0,
                    max_cycles_per_source=1,
                    model_ids=("model_a",),
                    basin_ids=(),
                    dry_run=True,
                    continuous=False,
                    interval_seconds=300.0,
                    max_passes=None,
                    workspace_root=None,
                    lock_path=None,
                    evidence_dir=None,
                )
            return
        payload = cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=("model_a",),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=None,
            lock_path=None,
            evidence_dir=None,
        )
    finally:
        target.chmod(original_mode)

    assert payload["status"] == "preflight_blocked"
    assert expected_code in {blocker["code"] for blocker in payload["root_preflight"]["blockers"]}
    assert payload["counts"]["submitted_count"] == 0
    assert payload["no_mutation_proof"] == _expected_no_mutation_proof()
    if safe_evidence:
        assert Path(payload["artifact_path"]).is_file()
    else:
        assert "artifact_path" not in payload


def test_no_flag_out_of_bound_lock_and_evidence_roots_are_rejected_at_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _scheduler_env_roots(tmp_path)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(tmp_path / "outside-locks"))
    (tmp_path / "outside-locks").mkdir()

    with pytest.raises(ValueError, match="lock_path must be under workspace_root"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=None,
            lock_path=None,
            evidence_dir=None,
        )


def test_public_from_env_wires_active_repository(monkeypatch: Any, tmp_path: Path) -> None:
    active_repository = FakeActiveRepository(active=False)
    monkeypatch.setattr("services.orchestrator.scheduler._active_repository_from_env", lambda: active_repository)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", lambda: FakeRegistry([]))
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", lambda: {})

    scheduler = ProductionScheduler.from_env(_config(tmp_path, now=_dt("2026-05-21T12:00:00Z")))

    assert scheduler.active_repository is active_repository


def test_public_from_env_wires_forcing_producer_when_enabled(monkeypatch: Any, tmp_path: Path) -> None:
    forcing_producer = FakeForcingProducer()
    monkeypatch.setenv("NHMS_PRODUCTION_FORCING_ENABLED", "1")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=False),
    )
    monkeypatch.setattr("services.orchestrator.scheduler._canonical_readiness_provider_from_env", lambda: None)
    monkeypatch.setattr("services.orchestrator.scheduler._forcing_producer_from_env", lambda: forcing_producer)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", lambda: FakeRegistry([]))
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", lambda: {})

    scheduler = ProductionScheduler.from_env(_config(tmp_path, now=_dt("2026-05-21T12:00:00Z")))

    assert scheduler.config.forcing_production_enabled is True
    assert scheduler.forcing_producer is forcing_producer


def test_plan_production_cli_public_path_skips_active_duplicate(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", "0,6,12,18")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: FakeActiveRepository(active=True),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model("model_a", "basin_a")]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._now",
        lambda config: config.now or _dt("2026-05-21T12:00:00Z"),
    )

    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=str(tmp_path),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["candidates"] == []
    assert payload["skipped_candidates"][0]["reason"] == "active_duplicate_pipeline"
    assert payload["counts"]["skipped_candidate_count"] == 1
    assert payload["counts"]["submitted_count"] == 0


def test_plan_production_click_missing_database_url_exits_cleanly_without_mutation(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.ProductionScheduler.run_once", _unexpected_run_once)

    try:
        cli._click_main(["plan-production", "--workspace-root", str(tmp_path)])
    except SystemExit as error:
        rc = int(error.code or 0)
    else:
        rc = 0
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert captured.err == "DATABASE_URL_MISSING: DATABASE_URL is required for orchestration.\n"
    assert list((tmp_path / "scheduler").glob("*")) == []


def test_plan_production_argparse_missing_database_url_exits_cleanly_without_mutation(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.ProductionScheduler.run_once", _unexpected_run_once)

    rc = cli._argparse_main(["plan-production", "--workspace-root", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert captured.err == "DATABASE_URL_MISSING: DATABASE_URL is required for orchestration.\n"
    assert list((tmp_path / "scheduler").glob("*")) == []


def test_plan_production_cli_smoke_with_injected_scheduler(monkeypatch: Any, tmp_path: Path) -> None:
    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> Any:
            return SimpleResult(
                {
                    "status": "planned",
                    "sources": list(self.config.sources),
                    "operator_filters": {"expression": "model_id in [model_a]"},
                }
            )

    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(
        [
            "plan-production",
            "--source",
            "gfs,IFS",
            "--model-id",
            "model_a",
            "--workspace-root",
            str(tmp_path),
        ]
    )

    assert rc == 0


def test_run_continuous_unbounded_keeps_only_latest_result(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), interval_seconds=1)
    scheduler = CountingScheduler(config, stop_after=3)

    with pytest.raises(StopIteration):
        scheduler.run_continuous()

    assert len(scheduler.snapshots) == 3
    assert scheduler.snapshots == [1, 1, 1]


def test_run_continuous_finite_within_cap_returns_pass_results(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), interval_seconds=1)
    scheduler = CountingScheduler(config, stop_after=10)

    results = scheduler.run_continuous(max_passes=3)

    assert [result.pass_id for result in results] == ["pass_1", "pass_2", "pass_3"]
    assert scheduler.pass_count == 3


def test_run_continuous_rejects_excessive_finite_passes(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), interval_seconds=1)
    scheduler = CountingScheduler(config, stop_after=10)

    with pytest.raises(ValueError, match="max_passes exceeds finite JSON output limit"):
        scheduler.run_continuous(max_passes=MAX_CONTINUOUS_JSON_PASSES + 1)

    assert scheduler.pass_count == 0


def test_run_continuous_rejects_zero_finite_passes(tmp_path: Path) -> None:
    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), interval_seconds=1)
    scheduler = CountingScheduler(config, stop_after=10)

    with pytest.raises(ValueError, match="max_passes must be at least 1"):
        scheduler.run_continuous(max_passes=0)

    assert scheduler.pass_count == 0


def test_plan_production_cli_uses_scheduler_env_interval_and_max_passes_for_no_flag_continuous(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_continuous(self, *, max_passes: int | None = None) -> list[SimpleResult]:
            captured["max_passes"] = max_passes
            return [SimpleResult({"status": "planned", "pass": index + 1}) for index in range(max_passes or 0)]

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_PASSES", "2")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--continuous"])

    assert rc == 0
    assert captured["config"].interval_seconds == 12.5
    assert captured["max_passes"] == 2


def test_plan_production_cli_uses_scheduler_env_max_cycles_when_omitted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", "2")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production"])

    assert rc == 0
    assert captured["config"].max_cycles_per_source == 2


def test_plan_production_cli_explicit_max_cycles_one_overrides_scheduler_env(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", "2")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--max-cycles-per-source", "1"])

    assert rc == 0
    assert captured["config"].max_cycles_per_source == 1


def test_plan_production_cli_uses_scheduler_env_cycle_lag_when_omitted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "16")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production"])

    assert rc == 0
    assert captured["config"].cycle_lag_hours == 16


def test_plan_production_cli_explicit_cycle_lag_overrides_scheduler_env(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "16")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--cycle-lag-hours", "6"])

    assert rc == 0
    assert captured["config"].cycle_lag_hours == 6


def test_plan_production_cli_cycle_time_pins_single_cycle_and_disables_backfill(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, ProductionSchedulerConfig] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_once(self) -> SimpleResult:
            return SimpleResult({"status": "planned"})

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_LOOKBACK_HOURS", "48")
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "16")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production", "--cycle-time", "2000-01-01T00:00:00Z"])

    assert rc == 0
    assert captured["config"].now is None
    assert captured["config"].lookback_hours == 0
    assert captured["config"].cycle_lag_hours > 0
    assert captured["config"].max_cycles_per_source == 1
    assert captured["config"].backfill_enabled is False


def test_plan_production_cli_rejects_non_positive_explicit_max_cycles(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("non-positive max cycles flag must not construct scheduler")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    try:
        cli._click_main(["plan-production", "--max-cycles-per-source", "0"])
    except SystemExit as error:
        rc = int(error.code or 0)
    else:
        rc = 0
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err == "plan-production max_cycles_per_source must be at least 1\n"


def test_plan_production_cli_explicit_interval_and_max_passes_override_scheduler_env(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config
            captured["config"] = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_continuous(self, *, max_passes: int | None = None) -> list[SimpleResult]:
            captured["max_passes"] = max_passes
            return [SimpleResult({"status": "planned", "pass": index + 1}) for index in range(max_passes or 0)]

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_INTERVAL_SECONDS", "12.5")
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_PASSES", "2")
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(
        [
            "plan-production",
            "--continuous",
            "--interval-seconds",
            "33",
            "--max-passes",
            "4",
        ]
    )

    assert rc == 0
    assert captured["config"].interval_seconds == 33.0
    assert captured["max_passes"] == 4


def test_cli_rejects_unbounded_json_continuous_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--continuous JSON output requires --max-passes"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=True,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=str(tmp_path),
            lock_path=None,
            evidence_dir=None,
        )


def test_cli_rejects_zero_continuous_json_passes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_passes must be at least 1"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=True,
            interval_seconds=300.0,
            max_passes=0,
            workspace_root=str(tmp_path),
            lock_path=None,
            evidence_dir=None,
        )


def test_cli_rejects_excessive_continuous_json_passes(monkeypatch: Any, tmp_path: Path) -> None:
    class FailingScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            raise AssertionError("scheduler must not be constructed for excessive finite JSON output")

    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    with pytest.raises(ValueError, match="max_passes exceeds limit"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=True,
            interval_seconds=300.0,
            max_passes=MAX_CONTINUOUS_JSON_PASSES + 1,
            workspace_root=str(tmp_path),
            lock_path=None,
            evidence_dir=None,
        )


def test_cli_rejects_invalid_scheduler_max_cycles_env_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class FailingScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            raise AssertionError("scheduler must not be constructed for invalid max cycles env")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", "not-an-int")
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    with pytest.raises(ValueError, match="NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE must be an integer"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=None,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=str(workspace_root),
            lock_path=None,
            evidence_dir=None,
        )


def test_cli_rejects_non_positive_scheduler_max_cycles_env_before_scheduler_construction(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class FailingScheduler:
        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FailingScheduler:
            del config
            raise AssertionError("non-positive max cycles env must not construct scheduler")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", "0")
    monkeypatch.setattr(cli, "ProductionScheduler", FailingScheduler)

    with pytest.raises(ValueError, match="max_cycles_per_source must be at least 1"):
        cli._plan_production(
            sources=("gfs",),
            lookback_hours=24,
            cycle_lag_hours=0,
            max_cycles_per_source=None,
            model_ids=(),
            basin_ids=(),
            dry_run=True,
            continuous=False,
            interval_seconds=300.0,
            max_passes=None,
            workspace_root=str(workspace_root),
            lock_path=None,
            evidence_dir=None,
        )


def test_production_scheduler_config_rejects_non_positive_max_cycles(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_cycles_per_source must be at least 1"):
        _config(tmp_path, max_cycles_per_source=0)


class SimpleResult:
    status = "planned"

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence

    def to_dict(self) -> dict[str, Any]:
        return dict(self.evidence)


class FakeRegistry:
    def __init__(self, models: list[dict[str, Any]]) -> None:
        self.models = models

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        del basin_version_id, active
        items = self.models[offset : offset + limit]
        return {"items": items, "total": len(self.models), "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        matches = [model for model in self.models if model["model_id"] == model_id]
        if not matches:
            raise KeyError(model_id)
        return dict(matches.pop(0))


class RedactingRegistry(FakeRegistry):
    def get_model(self, model_id: str) -> dict[str, Any]:
        model = super().get_model(model_id)
        profile = dict(model.get("resource_profile") or {})
        if "package_checksum" in profile:
            profile["package_checksum"] = None
        model["resource_profile"] = profile
        model["package_checksum"] = None
        model["source_inventory_checksum"] = None
        return model

    def get_model_internal(self, model_id: str) -> dict[str, Any]:
        return super().get_model(model_id)


class PublicOnlyRedactingRegistry(RedactingRegistry):
    get_model_internal = None


class FakeAdapter:
    def __init__(
        self,
        source_id: str,
        cycles: list[tuple[str, bool] | tuple[str, bool, dict[str, Any]]],
        *,
        policy_identity: dict[str, Any] | None = None,
        source_object_identity: dict[str, Any] | None = None,
    ) -> None:
        self.source_id = source_id
        self.cycles = cycles
        self.download_calls = 0
        self._policy_identity = policy_identity
        self._source_object_identity = source_object_identity

    def discover_cycles(self, cycle_date: Any, end_date: Any = None) -> list[CycleDiscovery]:
        del end_date
        requested_date = cycle_date.date() if isinstance(cycle_date, datetime) else cycle_date
        discoveries: list[CycleDiscovery] = []
        for cycle in self.cycles:
            cycle_time, available, *extra = cycle
            parsed_cycle_time = _dt(cycle_time)
            if parsed_cycle_time.date() != requested_date:
                continue
            metadata = dict(extra[0]) if extra else {}
            discoveries.append(
                CycleDiscovery(
                    cycle_id=cycle_id_for(self.source_id, parsed_cycle_time),
                    source_id=self.source_id,
                    cycle_time=parsed_cycle_time,
                    cycle_hour=parsed_cycle_time.hour,
                    available=available,
                    status=metadata.get("status") or ("discovered" if available else "unavailable"),
                    reason=metadata.get("reason"),
                    classifier=metadata.get("classifier"),
                    retryable=metadata.get("retryable"),
                    probe_uri=metadata.get("probe_uri"),
                    evidence=dict(metadata.get("evidence") or {}),
                )
            )
        return discoveries

    def download_plan(self, *_args: Any, **_kwargs: Any) -> None:
        self.download_calls += 1
        raise AssertionError("dry-run scheduler must not download")

    def source_policy_identity(self, *_args: Any) -> dict[str, Any]:
        return dict(self._policy_identity or {"source": self.source_id, "forecast_hours": [0, 3]})

    def source_object_identity(self, *_args: Any) -> dict[str, Any]:
        return dict(self._source_object_identity or {"source": self.source_id, "object": "fake"})


class FakeCanonicalReadinessProvider:
    def __init__(self, readiness_by_cycle: Mapping[tuple[str, datetime], Mapping[str, Any]]) -> None:
        self.readiness_by_cycle = dict(readiness_by_cycle)

    def canonical_readiness(self, **kwargs: Any) -> Mapping[str, Any]:
        key = (kwargs["source_id"], kwargs["cycle_time"])
        return dict(self.readiness_by_cycle[key])


class OverLimitAdapter:
    def __init__(self, source_id: str, cycle_time: str) -> None:
        self.source_id = source_id
        self.cycle_time = _dt(cycle_time)

    def discover_cycles(self, cycle_date: Any, end_date: Any = None) -> list[CycleDiscovery]:
        del cycle_date, end_date
        return [
            CycleDiscovery(
                cycle_id=f"{self.source_id}_cycle_{index}",
                source_id=self.source_id,
                cycle_time=self.cycle_time,
                cycle_hour=self.cycle_time.hour,
                available=True,
                status="discovered",
            )
            for index in range(MAX_DISCOVERED_CYCLES + 1)
        ]


class FakeActiveRepository:
    def __init__(self, *, active: bool, completed: bool = False) -> None:
        self.active = active
        self.completed = completed

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return False

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return self.active

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return self.completed


class FakeSlurmActiveRepository(FakeActiveRepository):
    def __init__(self, *, active_jobs: list[dict[str, Any]]) -> None:
        super().__init__(active=False, completed=False)
        self.active_jobs = active_jobs

    def active_slurm_jobs(self, *, source_id: str, cycle_time: datetime, model_id: str) -> list[dict[str, Any]]:
        del source_id, cycle_time, model_id
        return [dict(job) for job in self.active_jobs]


class FakeCandidateStateRepository(FakeActiveRepository):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__(active=False, completed=False)
        self.state = state
        self.queries: list[dict[str, Any]] = []

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        self.queries.append(
            {
                "source_id": source_id,
                "cycle_time": cycle_time,
                "model_id": model_id,
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "candidate_id": candidate_id,
            }
        )
        return {
            **dict(self.state),
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "candidate_id": candidate_id,
        }


class RawCandidateStateRepository(FakeActiveRepository):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__(active=False, completed=False)
        self.state = state

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        del source_id, cycle_time, model_id, run_id, forcing_version_id, candidate_id
        return dict(self.state)


class BoundedReadSequence(Sequence[Any]):
    def __init__(self, items: list[Any], *, allowed_reads: int) -> None:
        self.items = items
        self.allowed_reads = allowed_reads
        self.read_count = 0

    def __iter__(self) -> Any:
        for index, item in enumerate(self.items):
            if index >= self.allowed_reads:
                raise AssertionError("task_results scanned past overflow sentinel")
            self.read_count = index + 1
            yield item

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            raise AssertionError("task_results must not be sliced")
        if index >= self.allowed_reads:
            raise AssertionError("task_results scanned past overflow sentinel")
        self.read_count = max(self.read_count, index + 1)
        return self.items[index]

    def __len__(self) -> int:
        raise AssertionError("task_results length must not be required")


class CandidateAndActiveRepository(FakeCandidateStateRepository):
    def __init__(self, state: dict[str, Any], active_jobs: list[dict[str, Any]]) -> None:
        super().__init__(state)
        self.active_jobs = active_jobs

    def active_slurm_jobs(self, *, source_id: str, cycle_time: datetime, model_id: str) -> list[dict[str, Any]]:
        del source_id, cycle_time, model_id
        return [dict(job) for job in self.active_jobs]


class PerModelCandidateStateRepository(FakeActiveRepository):
    def __init__(self, states: dict[str, dict[str, Any] | None]) -> None:
        super().__init__(active=False, completed=False)
        self.states = states

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        del source_id, cycle_time
        state = self.states.get(model_id)
        if state is None:
            return None
        return {
            **dict(state),
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "candidate_id": candidate_id,
        }


class PerCycleCandidateStateRepository(FakeActiveRepository):
    def __init__(self, states: dict[str, dict[str, Any] | None]) -> None:
        super().__init__(active=False, completed=False)
        self.states = states
        self.queries: list[dict[str, Any]] = []

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        self.queries.append(
            {
                "source_id": source_id,
                "cycle_time": cycle_time,
                "model_id": model_id,
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "candidate_id": candidate_id,
            }
        )
        state = self.states.get(cycle_time.isoformat())
        if state is None:
            return None
        return {
            **dict(state),
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "candidate_id": candidate_id,
        }


class SequencedPerModelCandidateStateRepository(FakeActiveRepository):
    def __init__(
        self,
        *,
        first_states: dict[str, dict[str, Any] | None],
        second_states: dict[str, dict[str, Any] | None],
    ) -> None:
        super().__init__(active=False, completed=False)
        self.first_states = first_states
        self.second_states = second_states
        self.use_second_scan = False
        self.queries: list[dict[str, Any]] = []

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        self.queries.append(
            {
                "source_id": source_id,
                "cycle_time": cycle_time,
                "model_id": model_id,
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "candidate_id": candidate_id,
                "scan": "second" if self.use_second_scan else "first",
            }
        )
        state = (self.second_states if self.use_second_scan else self.first_states).get(model_id)
        if state is None:
            return None
        return {
            **dict(state),
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "candidate_id": candidate_id,
        }


class FakeActiveCycleOrchestrationRepository:
    def __init__(self) -> None:
        self.orchestration_checks: list[tuple[str, datetime]] = []

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        self.orchestration_checks.append((source_id, cycle_time))
        return True

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        raise AssertionError("cycle-level active orchestration must skip before per-model active checks")

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        raise AssertionError("cycle-level active orchestration must skip before per-model completed checks")


class FakeHydroStateRepository:
    active_statuses = {"created", "staged", "submitted", "running"}
    completed_statuses = {"succeeded", "parsed", "published", "published", "complete"}
    terminal_job_statuses = {
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "permanently_failed",
    }

    def __init__(self, *, hydro_status: str, pipeline_status: str | None = None) -> None:
        self.hydro_status = hydro_status
        self.pipeline_status = pipeline_status

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        if self.hydro_status in self.active_statuses:
            return True
        return self.pipeline_status is not None and self.pipeline_status not in self.terminal_job_statuses

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return self.hydro_status in self.completed_statuses


class FakeProductionOrchestrator:
    def __init__(
        self,
        *,
        candidate_outcomes: tuple[dict[str, Any], ...] = (),
        result_status: str = "complete",
        expose_object_store: bool = True,
        cancel_payload: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        if expose_object_store:
            self.object_store = LocalObjectStore(_TEST_OBJECT_STORE_ROOT, "s3://nhms")
        self.candidate_outcomes = candidate_outcomes
        self.result_status = result_status
        self.cancel_payload = cancel_payload

    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, Any]],
    ) -> PipelineResult:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        stages = tuple(
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=f"job_{stage.stage}",
                slurm_job_id=f"slurm_{stage.stage}",
                status="succeeded",
            )
            for stage in M3_STAGES
        )
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status=self.result_status,
            stages=stages,
            candidate_outcomes=self.candidate_outcomes,
        )

    def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
        self.cancel_calls.append((cycle_id, reason))
        if self.cancel_payload is not None:
            return [dict(item) for item in self.cancel_payload]
        return [
            {
                "job_id": "job_forcing",
                "cycle_id": cycle_id,
                "slurm_job_id": "7777",
                "status": "cancelled",
                "replacement_submitted": False,
            }
        ]


class FakeForcingProducer:
    def __init__(self, *, error: Exception | None = None, forcing_version_id: str | None = None) -> None:
        self.error = error
        self.forcing_version_id = forcing_version_id
        self.calls: list[dict[str, Any]] = []

    def produce(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        max_lead_hours: int | None = None,
        basin_id: str | None = None,
        basin_version_id: str | None = None,
        river_network_version_id: str | None = None,
        canonical_product_id: str | None = None,
        canonical_identity: Mapping[str, Any] | None = None,
    ) -> Any:
        parsed_cycle_time = _dt(cycle_time) if isinstance(cycle_time, str) else cycle_time
        self.calls.append(
            {
                "source_id": source_id,
                "cycle_time": parsed_cycle_time,
                "model_id": model_id,
                "max_lead_hours": max_lead_hours,
                "basin_id": basin_id,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
                "canonical_product_id": canonical_product_id,
                "canonical_identity": dict(canonical_identity or {}),
            }
        )
        if self.error is not None:
            raise self.error
        compact_cycle = format_cycle_time(parsed_cycle_time)
        return type(
            "FakeForcingProductionResult",
            (),
            {
                "status": "forcing_ready",
                "forcing_version_id": self.forcing_version_id
                or f"forc_{str(source_id).lower()}_{compact_cycle}_{model_id}",
                "forcing_package_uri": f"s3://nhms/forcing/{source_id}/{compact_cycle}/basin_a_v1/{model_id}/",
                "checksum": "forcing-manifest-sha",
                "station_count": 2,
                "timestep_count": 2,
                "variable_count": 6,
                "time_range": {
                    "start_time": "2026-05-21T06:00:00Z",
                    "end_time": "2026-05-21T09:00:00Z",
                    "timestep_count": 2,
                },
                "units": {
                    "PRCP": "mm/day",
                    "TEMP": "degC",
                    "RH": "0-1",
                    "wind": "m/s",
                    "Rn": "W/m2",
                    "Press": "Pa",
                },
                "file_uris": {
                    "tsd_forc": f"s3://nhms/forcing/{source_id}/{compact_cycle}/basin_a_v1/{model_id}/forcing.tsd.forc",
                    "package_manifest": (
                        f"s3://nhms/forcing/{source_id}/{compact_cycle}/basin_a_v1/{model_id}/forcing_package.json"
                    ),
                },
            },
        )()


class FakeProductionOrchestratorWithStageEvidence(FakeProductionOrchestrator):
    def __init__(
        self,
        *,
        candidate_outcomes: tuple[dict[str, Any], ...] = (),
        result_status: str = "complete",
        stage_status: str = "succeeded",
        stage_error_message: str | None = None,
        stage_log_uri: str = "s3://nhms/logs/forcing.out",
    ) -> None:
        super().__init__(candidate_outcomes=candidate_outcomes, result_status=result_status)
        self.stage_status = stage_status
        self.stage_error_message = stage_error_message
        self.stage_log_uri = stage_log_uri

    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, Any]],
    ) -> PipelineResult:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        stage = StageRunResult(
            stage="forcing",
            job_type="produce_forcing_array",
            pipeline_job_id="job_forcing",
            slurm_job_id="slurm_forcing",
            status=self.stage_status,
            exit_code=0 if self.stage_status == "succeeded" else 1,
            error_code=None if self.stage_status == "succeeded" else "NODE_FAILURE",
            error_message=self.stage_error_message,
            log_uri=self.stage_log_uri,
            accounting={"elapsed": "00:03:00", "max_rss": "3072K", "alloc_tres": "cpu=2,mem=4G"},
            task_results=(
                {
                    "task_id": 0,
                    "array_task_id": 0,
                    "model_id": basins[0]["model_id"] if basins else "model_a",
                    "status": "succeeded",
                    "slurm_job_id": "slurm_forcing_0",
                    "exit_code": 0,
                    "log_uri": self.stage_log_uri,
                    "accounting": {"elapsed": "00:03:00", "max_rss": "3072K"},
                },
            ),
        )
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status=self.result_status,
            stages=(stage,),
            candidate_outcomes=self.candidate_outcomes,
        )


class StrictNoSubmitOrchestrator(FakeProductionOrchestrator):
    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, Any]],
    ) -> PipelineResult:
        del source, cycle_time, basins
        raise AssertionError("orchestrator must not run when preflight blocks submission")


class CountingScheduler(ProductionScheduler):
    def __init__(self, config: ProductionSchedulerConfig, *, stop_after: int) -> None:
        super().__init__(config, registry=FakeRegistry([]), adapters={}, sleep=self._sleep)
        self.stop_after = stop_after
        self.pass_count = 0
        self.snapshots: list[int] = []

    def run_once(self) -> SchedulerPassResult:
        self.pass_count += 1
        return SchedulerPassResult(
            pass_id=f"pass_{self.pass_count}",
            status="planned",
            evidence={"pass_id": f"pass_{self.pass_count}", "status": "planned"},
        )

    def _sleep(self, _seconds: float) -> None:
        import inspect

        caller = inspect.currentframe().f_back
        if caller is not None:
            results = caller.f_locals.get("results")
            if isinstance(results, list):
                self.snapshots.append(len(results))
        if self.pass_count >= self.stop_after:
            raise StopIteration


def _config(tmp_path: Path, **kwargs: Any) -> ProductionSchedulerConfig:
    values = {
        "workspace_root": tmp_path,
        "sources": ("gfs",),
        "lookback_hours": 24,
        "cycle_lag_hours": 0,
        "max_cycles_per_source": 1,
        "allowed_cycle_hours_utc": (0, 6, 12, 18),
        "dry_run": True,
    }
    values.update(kwargs)
    return ProductionSchedulerConfig(**values)


def _scheduler_evidence_test_context(
    config: ProductionSchedulerConfig,
    *,
    max_evidence_bytes: int = scheduler_module.MAX_EVIDENCE_BYTES,
    require_under_workspace: Any | None = None,
    write_new_regular_file: Any | None = None,
) -> Any:
    from services.orchestrator import scheduler_evidence

    return scheduler_evidence.SchedulerEvidenceWriteContext(
        config=config,
        require_safe_directory_final_component=scheduler_module._require_safe_directory_final_component,
        require_under_workspace=require_under_workspace or scheduler_module._require_under_workspace,
        max_evidence_bytes=max_evidence_bytes,
        bounded_evidence_payload=scheduler_evidence.bounded_evidence_payload,
        write_new_regular_file=write_new_regular_file or scheduler_evidence.write_new_regular_file,
        require_evidence_artifact_available=scheduler_evidence.require_evidence_artifact_available,
        reservation_blocked_payload=scheduler_evidence.evidence_reservation_blocked_payload,
    )


def _large_scheduler_evidence_payload(pass_id: str) -> dict[str, Any]:
    large_text = "x" * 2_000
    return {
        "schema_version": SCHEDULER_EVIDENCE_SCHEMA_VERSION,
        "pass_id": pass_id,
        "started_at": "2026-05-21T12:00:00Z",
        "finished_at": "2026-05-21T12:00:30Z",
        "status": "submitted",
        "execution_mode": "production_orchestration",
        "readiness_interpretation": "non_final_scheduler_evidence",
        "readiness": {
            "schema_version": "nhms.production_readiness.scheduler_input.v1",
            "interpretation": "non_final_scheduler_evidence",
            "production_ready": False,
            "final_production_readiness_claimed": False,
            "can_claim_final_production_readiness": False,
            "payload": large_text,
        },
        "counts": {"candidate_count": 1, "submitted_count": 1},
        "resolved_runtime_roots": {"evidence_root": {"path": "/workspace/evidence", "payload": large_text}},
        "runtime_config": {
            "dry_run": False,
            "allowed_cycle_hours_utc": [0, 6, 12, 18],
            "payload": large_text,
        },
        "root_preflight": {"status": "ready", "checks": {"evidence_root": {"payload": large_text}}},
        "evidence_pre_execution": {"status": "reserved", "payload": large_text},
        "execution_write_proof": {"status": "submitted", "submitted_count": 1, "payload": large_text},
        "slurm_status_sync_proof": {"status": "not_required", "payload": large_text},
        "slurm_cancellation_proof": {"status": "not_required", "payload": large_text},
        "no_mutation_proof": {**_expected_no_mutation_proof(), "payload": large_text},
        "duplicate_exclusions": [{"source_id": "gfs", "payload": large_text}],
        "candidates": [{"candidate_id": "candidate-1", "payload": large_text}],
        "blocked_candidates": [{"candidate_id": "candidate-2", "payload": large_text}],
        "skipped_candidates": [{"candidate_id": "candidate-3", "payload": large_text}],
        "source_cycles": [{"source_id": "gfs", "payload": large_text}],
        "model_discovery": {"models": [{"model_id": "model_a", "payload": large_text}]},
    }


def _slurm_roots(root: Path) -> dict[str, Path]:
    roots = {
        "workspace_root": root / "workspace",
        "object_store_root": root / "object-store",
        "published_root": root / "published",
        "log_root": root / "logs",
        "runtime_root": root / "runtime",
        "temp_root": root / "tmp",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


def _scheduler_env_roots(root: Path) -> dict[str, Path]:
    workspace_root = root / "workspace"
    roots = {
        "workspace_root": workspace_root,
        "object_store_root": root / "object-store",
        "published_root": root / "published",
        "runtime_root": root / "runtime",
        "temp_root": root / "tmp",
        "lock_root": workspace_root / "locks",
        "evidence_root": workspace_root / "evidence",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


def _set_scheduler_root_env(monkeypatch: Any, roots: Mapping[str, Path]) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(roots["workspace_root"]))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(roots["published_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_RUNTIME_ROOT", str(roots["runtime_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_TEMP_ROOT", str(roots["temp_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(roots["lock_root"]))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(roots["evidence_root"]))
    monkeypatch.setenv(
        "NHMS_SCHEDULER_ALLOWED_ROOTS",
        os.pathsep.join(
            str(roots[key])
            for key in ("workspace_root", "object_store_root", "published_root", "runtime_root", "temp_root")
        ),
    )
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", "0,6,12,18")
    monkeypatch.setenv("NHMS_SCHEDULER_SOURCES", "gfs")
    monkeypatch.setenv("NHMS_SCHEDULER_MODEL_IDS", "model_a")
    monkeypatch.setenv("NHMS_SCHEDULER_BASIN_IDS", "basin_a")


_DB_FREE_SELECTOR_ENV_KEYS = (
    "NHMS_SCHEDULER_STATE_BACKEND",
    "NHMS_SCHEDULER_LOCK_BACKEND",
    "NHMS_SCHEDULER_REGISTRY_BACKEND",
    "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
    "NHMS_SCHEDULER_JOURNAL_BACKEND",
    "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
)
_DB_FREE_PATH_ENV_KEYS = (
    "NHMS_SCHEDULER_REGISTRY_MANIFEST",
    "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
    "NHMS_SCHEDULER_JOURNAL_ROOT",
    "NHMS_SCHEDULER_STATE_INDEX",
)
_DB_FREE_FILE_PATH_ENV_KEYS = tuple(
    key for key in _DB_FREE_PATH_ENV_KEYS if key != "NHMS_SCHEDULER_JOURNAL_ROOT"
)
_DB_FREE_SELECTOR_RUNTIME_CONFIG_FIELDS = {
    "NHMS_SCHEDULER_STATE_BACKEND": "scheduler_state_backend",
    "NHMS_SCHEDULER_LOCK_BACKEND": "scheduler_lock_backend",
    "NHMS_SCHEDULER_REGISTRY_BACKEND": "scheduler_registry_backend",
    "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND": "scheduler_canonical_readiness_backend",
    "NHMS_SCHEDULER_JOURNAL_BACKEND": "scheduler_journal_backend",
    "NHMS_SCHEDULER_STATE_INDEX_BACKEND": "scheduler_state_index_backend",
}


def _gfs_default_forecast_hours() -> tuple[int, ...]:
    return tuple(range(0, 169, 3))


def _set_db_free_scheduler_env(monkeypatch: Any, root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    roots = _scheduler_env_roots(root)
    _set_scheduler_root_env(monkeypatch, roots)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_ROOTS", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in _DB_FREE_SELECTOR_ENV_KEYS:
        monkeypatch.setenv(key, "file")
    db_free_dir = roots["workspace_root"] / "db-free"
    object_index_dir = roots["object_store_root"] / "db-free"
    db_free_dir.mkdir(parents=True, exist_ok=True)
    object_index_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": db_free_dir / "registry-manifest.json",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": object_index_dir / "canonical-readiness-index.json",
        "NHMS_SCHEDULER_JOURNAL_ROOT": roots["workspace_root"] / "journal",
        "NHMS_SCHEDULER_STATE_INDEX": object_index_dir / "state-index.json",
    }
    for key, path in paths.items():
        if key == "NHMS_SCHEDULER_JOURNAL_ROOT":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv(key, str(path))
    return roots, paths


def _compact_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _checksumed_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body["checksum"] = f"sha256:{sha256_bytes(_compact_json_bytes(body))}"
    return body


def _write_json_manifest_with_checksum(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_checksumed_payload(payload), sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _db_free_model_manifest_fixture(
    roots: Mapping[str, Path],
    model: Mapping[str, Any],
    *,
    package_checksum: str = "package-model-a",
) -> tuple[dict[str, Any], str]:
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    model_id = str(model["model_id"])
    manifest_key = f"models/{model_id}/manifest.json"
    store.write_bytes_atomic(
        manifest_key,
        json.dumps(
            {
                "schema_version": "nhms.model_package_manifest.v1",
                "model_id": model_id,
                "package_checksum": package_checksum,
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    row = {
        **dict(model),
        "manifest_uri": f"s3://nhms/{manifest_key}",
        "package_checksum": package_checksum,
        "output_segment_count": 3,
        "display_capabilities": {"tiles": True},
        "source_policy": {"source": "gfs", "owner": "node-27"},
    }
    return row, package_checksum


def _file_readiness_products(
    roots: Mapping[str, Path],
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    rows = _canonical_rows(
        source_id=source_id,
        cycle_time=cycle_time,
        variables=GFS_REQUIRED_STANDARD_VARIABLES,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    for row in rows:
        key = (
            f"canonical/{source_id}/{format_cycle_time(cycle_time)}/"
            f"{row['variable']}/f{int(row['lead_time_hours']):03d}.dat"
        )
        content = f"{row['variable']}:{row['lead_time_hours']}".encode("utf-8")
        store.write_bytes_atomic(key, content)
        row["object_uri"] = f"s3://nhms/{key}"
        row["checksum"] = f"sha256:{sha256_bytes(content)}"
    return rows


def _write_db_free_file_provider_fixtures(
    monkeypatch: Any,
    roots: Mapping[str, Path],
    paths: Mapping[str, Path],
    *,
    cycle_time: datetime,
    forecast_hours: Sequence[int] = (0, 3),
    products: Sequence[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
    policy_identity: Mapping[str, Any] | None = None,
    source_object_identity: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    generated_at = generated_at or _dt("2026-06-27T00:00:00Z")
    model_row, package_checksum = _db_free_model_manifest_fixture(
        roots,
        model or _model("model_a", "basin_a"),
    )
    registry_receipt = scheduler_module.publish_scheduler_registry_manifest(
        [model_row],
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    policy = dict(policy_identity or {"source": "gfs", "forecast_hours": list(forecast_hours)})
    source_object = dict(
        source_object_identity
        or {"source": "gfs", "manifest_object_key": f"raw/gfs/{format_cycle_time(cycle_time)}/manifest.json"}
    )
    if products is None:
        products = _file_readiness_products(
            roots,
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=forecast_hours,
            policy_identity=policy,
            source_object_identity=source_object,
        )
    canonical_product_id = f"canon_gfs_{format_cycle_time(cycle_time)}"
    readiness_receipt = scheduler_module.publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "model_id": "model_a",
                "basin_id": "basin_a",
                "canonical_product_id": canonical_product_id,
                "forecast_hours": list(forecast_hours),
                "policy_identity": policy,
                "source_object_identity": source_object,
                "products": [dict(product) for product in products],
            }
        ],
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    return {
        "model": model_row,
        "package_checksum": package_checksum,
        "policy_identity": policy,
        "source_object_identity": source_object,
        "canonical_product_id": canonical_product_id,
        "registry_receipt": registry_receipt,
        "readiness_receipt": readiness_receipt,
    }


def _write_db_free_state_index_fixture(
    roots: Mapping[str, Path],
    paths: Mapping[str, Path],
    *,
    cycle_time: datetime,
    package_checksum: str,
    generated_at: datetime,
    entries: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    if entries is None:
        state_content = b"db-free-strict-warm-start-state\n"
        producer_cycle_time = scheduler_module._floor_to_source_cycle_boundary(
            cycle_time - timedelta(microseconds=1),
            ("gfs",),
            allowed_cycle_hours_utc=(0, 6, 12, 18),
        )
        producer_cycle_id = cycle_id_for("gfs", producer_cycle_time)
        lead_hours = int(round((cycle_time - producer_cycle_time).total_seconds() / 3600.0))
        state_uri = store.write_bytes_atomic(
            f"states/gfs/model_a/{format_cycle_time(cycle_time)}/state.cfg.ic",
            state_content,
        )
        entries = [
            {
                "state_id": f"state_gfs_model_a_{format_cycle_time(cycle_time)}_{producer_cycle_id}_f{lead_hours:03d}",
                "model_id": "model_a",
                "run_id": f"analysis_{producer_cycle_id}_model_a",
                "source_id": "gfs",
                "valid_time": _format_iso_z(cycle_time),
                "state_uri": state_uri,
                "checksum": f"sha256:{sha256_bytes(state_content)}",
                "usable_flag": True,
                "cycle_id": producer_cycle_id,
                "lead_hours": lead_hours,
                "model_package_version": "s3://nhms/models/model_a/package/",
                "model_package_checksum": package_checksum,
            }
        ]
    receipt = publish_state_snapshot_index(
        entries,
        paths["NHMS_SCHEDULER_STATE_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    return {"entries": [dict(entry) for entry in entries], "receipt": receipt}


def _format_iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_db_free_raw_manifest_fixture(
    roots: Mapping[str, Path],
    *,
    source_id: str = "gfs",
    cycle_time: datetime,
    manifest_source_id: str | None = None,
    manifest_cycle_time: str | None = None,
    manifest_uri: str | None = None,
    entries: Sequence[Mapping[str, Any]] | None = None,
    write_raw_files: bool = True,
    raw_content: bytes = b"node27-raw",
) -> dict[str, Any]:
    compact_cycle = format_cycle_time(cycle_time)
    raw_key = f"raw/{source_id}/{compact_cycle}/{source_id}.f000.bundle.grib2"
    rows = [dict(row) for row in (entries if entries is not None else [{"local_key": raw_key, "forecast_hour": 0}])]
    if write_raw_files:
        for row in rows:
            local_key = str(row.get("local_key") or "")
            if local_key and ".." not in Path(local_key).parts:
                raw_path = roots["object_store_root"] / local_key
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(raw_content)
    manifest_key = f"raw/{source_id}/{compact_cycle}/manifest.json"
    manifest_path = roots["object_store_root"] / manifest_key
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_id": manifest_source_id or source_id,
        "cycle_time": manifest_cycle_time or _format_iso_z(cycle_time),
        "manifest_uri": manifest_uri or f"s3://nhms/{manifest_key}",
        "entries": rows,
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return {"manifest": manifest, "manifest_key": manifest_key, "manifest_path": manifest_path, "raw_key": raw_key}


def _run_no_flag_plan() -> dict[str, Any]:
    return cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=None,
        lock_path=None,
        evidence_dir=None,
    )


def _expected_no_mutation_proof() -> dict[str, bool]:
    return {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "slurm_status_sync_called": False,
        "slurm_cancellation_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
        "pipeline_status_writes": False,
        "pipeline_event_writes": False,
    }


def _model(model_id: str, basin_id: str, *, resource_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = {
        "runnable": True,
        "memory_gb": 8,
        "display_capabilities": {"tiles": True},
    }
    if resource_profile is not None:
        profile = dict(resource_profile)
    return {
        "model_id": model_id,
        "basin_id": basin_id,
        "basin_version_id": f"{basin_id}_v1",
        "river_network_version_id": f"{basin_id}_rivnet_v1",
        "segment_count": 3,
        "model_package_uri": f"s3://nhms/models/{model_id}/package/",
        "shud_code_version": "2.0",
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": profile,
    }


def _canonical_rows(
    *,
    source_id: str,
    cycle_time: datetime,
    variables: Sequence[str],
    forecast_hours: Sequence[int],
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
    omit_pairs: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    omitted = omit_pairs or set()
    for forecast_hour in forecast_hours:
        for variable in variables:
            if (variable, forecast_hour) in omitted:
                continue
            rows.append(
                {
                    "canonical_product_id": (
                        f"{source_id}_{format_cycle_time(cycle_time)}_{variable}_f{forecast_hour:03d}"
                    ),
                    "source_id": source_id,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=forecast_hour),
                    "lead_time_hours": forecast_hour,
                    "variable": variable,
                    "object_uri": f"canonical/{source_id}/{variable}/f{forecast_hour:03d}.nc",
                    "checksum": f"sha256:{variable}:{forecast_hour}",
                    "quality_flag": "ok",
                    "lineage_json": {
                        "policy_identity": dict(policy_identity),
                        "source_object_identity": dict(source_object_identity),
                    },
                }
            )
    return rows


def _production_identity_fixture() -> dict[str, str]:
    return {
        "run_id": "fcst_gfs_2026052106_model_a",
        "model_id": "model_a",
        "basin_id": "basin_a",
        "source": "gfs",
        "source_id": "gfs",
        "cycle_time": "2026-05-21T06:00:00Z",
        "basin_version_id": "basin_a_v1",
        "river_network_version_id": "basin_a_rivnet_v1",
        "canonical_product_id": "canon_gfs_2026052106",
        "forcing_version_id": "forc_gfs_2026052106_model_a",
        "hydro_run_id": "fcst_gfs_2026052106_model_a",
        "published_manifest_id": "manifest_fcst_gfs_2026052106_model_a",
    }


def _scheduler_candidate_fixture() -> scheduler_module.SchedulerCandidate:
    return scheduler_module._candidate_for(
        discovery=CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        model=scheduler_module.RegisteredSchedulerModel(
            model_id="model_a",
            basin_id="basin_a",
            basin_version_id="basin_a_v1",
            river_network_version_id="basin_a_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri="s3://nhms/models/model_a/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(evidence["candidates"], key=lambda item: item["candidate_id"])


def _unexpected_registry() -> FakeRegistry:
    raise AssertionError("missing DATABASE_URL must fail before registry construction")


def _unexpected_adapters() -> dict[str, FakeAdapter]:
    raise AssertionError("missing DATABASE_URL must fail before adapter construction")


def _unexpected_lock_acquire(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("missing DATABASE_URL must fail before scheduler lock acquisition")


def _unexpected_run_once(*_args: Any, **_kwargs: Any) -> SchedulerPassResult:
    raise AssertionError("missing DATABASE_URL must fail before candidate or evidence work")


def test_db_free_scheduler_config_parses_canonical_env_matrix(monkeypatch: Any, tmp_path: Path) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)

    config = ProductionSchedulerConfig()
    evidence = scheduler_module._scheduler_runtime_config_evidence(config)

    assert config.db_free_required is True
    assert config.database_url is None
    assert config.database_url_configured is False
    assert config.scheduler_state_backend == "file"
    assert config.scheduler_lock_backend == "file"
    assert config.scheduler_registry_backend == "file"
    assert config.scheduler_canonical_readiness_backend == "file"
    assert config.scheduler_journal_backend == "file"
    assert config.scheduler_state_index_backend == "file"
    assert config.scheduler_registry_manifest == str(paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"])
    assert config.scheduler_canonical_readiness_index == str(paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"])
    assert config.scheduler_journal_root == str(paths["NHMS_SCHEDULER_JOURNAL_ROOT"])
    assert config.scheduler_state_index == str(paths["NHMS_SCHEDULER_STATE_INDEX"])
    assert evidence["database_url_configured"] is False
    assert evidence["scheduler_db_free_required"] is True
    assert evidence["scheduler_lock_backend"] == "file"
    assert set(evidence["db_free_runtime"]["canonical_selector_fields"]) == set(_DB_FREE_SELECTOR_ENV_KEYS)
    assert set(evidence["db_free_runtime"]["canonical_path_fields"]) == set(_DB_FREE_PATH_ENV_KEYS)


def test_db_free_default_cycle_discovery_uses_nfs_raw_manifest_not_network_adapters(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    def _network_adapters_must_not_be_built() -> dict[str, FakeAdapter]:
        raise AssertionError("DB-free scheduler must not build network source adapters")

    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        _network_adapters_must_not_be_built,
    )

    scheduler = _RealProductionScheduler(
        ProductionSchedulerConfig(now=cycle_time, dry_run=True),
        registry=FakeRegistry([]),
    )

    cycles, evidence = scheduler._discover_cycles(cycle_time)

    assert any(
        cycle.discovery.source_id == "gfs"
        and cycle.discovery.cycle_time == cycle_time
        and cycle.discovery.available is True
        and cycle.discovery.probe_uri is None
        for cycle in cycles
    )
    source_cycle = next(
        item
        for item in evidence
        if item.get("source_id") == "gfs" and item.get("cycle_time_utc") == "2026-05-21T12:00:00Z"
    )
    assert source_cycle["discovery_evidence"]["source"] == "node27_nfs_raw_manifest"
    assert source_cycle["discovery_evidence"]["manifest_path"] == "[local-path]"


def test_db_free_raw_manifest_discovery_ignores_stale_06_18_allowed_hours(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig(
        now=_dt("2026-05-21T12:00:00Z"),
        sources=("gfs",),
        allowed_cycle_hours_utc=(0, 6, 12, 18),
    )

    adapter = scheduler_module._db_free_default_adapters(config)["gfs"]
    discoveries = adapter.discover_cycles("2026-05-21")

    assert {discovery.cycle_hour for discovery in discoveries} == {0, 12}


def test_legacy_non_db_free_postgres_config_remains_valid(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        scheduler_lock_backend="postgres",
        scheduler_state_backend="postgres",
        scheduler_registry_backend="postgres",
        scheduler_canonical_readiness_backend="postgres",
        scheduler_journal_backend="postgres",
        scheduler_state_index_backend="postgres",
    )

    assert config.db_free_required is False
    assert config.scheduler_lock_backend == "postgres"
    assert config.scheduler_state_backend == "postgres"
    assert config.scheduler_registry_backend == "postgres"


def test_legacy_scheduler_backend_uri_evidence_is_summarized(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        scheduler_registry_backend="postgresql://nhms:supersecret@db.prod.example:55433/nhms?token=qhh",
    )
    evidence = scheduler_module._scheduler_runtime_config_evidence(config)
    rendered = json.dumps(evidence, sort_keys=True)

    assert evidence["scheduler_registry_backend"] == "[db-like]"
    assert "supersecret" not in rendered
    assert "db.prod.example" not in rendered
    assert "55433" not in rendered
    assert "token" not in rendered
    assert "postgresql" not in rendered.lower()


def test_legacy_scheduler_db_like_backend_text_evidence_is_summarized(tmp_path: Path) -> None:
    config = _config(tmp_path, scheduler_registry_backend="postgresql+psycopg")
    evidence = scheduler_module._scheduler_runtime_config_evidence(config)
    rendered = json.dumps(evidence, sort_keys=True)

    assert evidence["scheduler_registry_backend"] == "[db-like]"
    assert "postgresql" not in rendered.lower()
    assert "psycopg" not in rendered.lower()


def test_db_free_database_url_blocks_before_lock_or_factories(monkeypatch: Any, tmp_path: Path) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:supersecret@db.prod.example:55433/nhms")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("DB-free DATABASE_URL blocker must not construct active repository"),
    )

    scheduler = ProductionScheduler.from_env(ProductionSchedulerConfig())
    result = scheduler.run_once()
    rendered = json.dumps(result.evidence)

    assert result.status == "preflight_blocked"
    assert result.evidence["lock"]["acquired"] is False
    assert result.evidence["lock"]["reason"] == "db_free_runtime_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    blockers = result.evidence["db_free_runtime"]["blockers"]
    assert ("database_url_forbidden", "DATABASE_URL") in {
        (blocker["code"], blocker["field"]) for blocker in blockers
    }
    assert "supersecret" not in rendered
    assert "55433" not in rendered


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
@pytest.mark.parametrize("selector_value", [None, "", "postgres", "psycopg", "memory"])
def test_db_free_selector_misconfig_blocks_exact_field_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
    selector_value: str | None,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    if selector_value is None:
        monkeypatch.delenv(selector_env, raising=False)
    else:
        monkeypatch.setenv(selector_env, selector_value)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["lock"]["reason"] == "db_free_runtime_preflight_blocked"
    assert selector_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
def test_db_free_selector_uri_blocks_without_endpoint_leak(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(selector_env, "postgresql://nhms:supersecret@db.prod.example:55433/nhms?token=qhh")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert selector_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    selector_check = result.evidence["db_free_runtime"]["checks"][selector_env]
    assert selector_check["selected"] == "[db-like]"
    assert selector_check["file_selected"] is False
    assert "supersecret" not in rendered
    assert "db.prod.example" not in rendered
    assert "55433" not in rendered
    assert "token" not in rendered
    assert "postgresql" not in rendered.lower()


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
def test_db_free_selector_file_uri_uses_uri_evidence_not_file_backend(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(selector_env, "file:///private/unsafe-secret-token/backend")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    selector_check = result.evidence["db_free_runtime"]["checks"][selector_env]
    assert selector_check["selected"] == "[uri]"
    assert selector_check["file_selected"] is False
    runtime_field = _DB_FREE_SELECTOR_RUNTIME_CONFIG_FIELDS[selector_env]
    assert result.evidence["runtime_config"][runtime_field] == "[uri]"
    assert "unsafe-secret-token" not in rendered
    assert "file:///private" not in rendered


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
@pytest.mark.parametrize(
    "selector_value",
    [
        "/tmp/unsafe-secret-token/backend",
        "../unsafe-secret-token/backend",
        "//[unsafe-secret-token",
        "prod-secret-token",
        "db.prod.example",
    ],
)
def test_db_free_selector_non_file_text_uses_bounded_evidence(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
    selector_value: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(selector_env, selector_value)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    selector_check = result.evidence["db_free_runtime"]["checks"][selector_env]
    expected_evidence = "[invalid-uri]" if selector_value.startswith("//[") else "[non-file]"
    assert selector_check["selected"] == expected_evidence
    runtime_field = _DB_FREE_SELECTOR_RUNTIME_CONFIG_FIELDS[selector_env]
    assert result.evidence["runtime_config"][runtime_field] == expected_evidence
    assert selector_value not in rendered
    assert "unsafe-secret-token" not in rendered
    assert "db.prod.example" not in rendered


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
def test_db_free_selector_db_like_text_blocks_without_dependency_leak(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(selector_env, "postgresql+psycopg")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert selector_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    selector_check = result.evidence["db_free_runtime"]["checks"][selector_env]
    assert selector_check["selected"] == "[db-like]"
    assert selector_check["file_selected"] is False
    assert "postgresql" not in rendered.lower()
    assert "psycopg" not in rendered.lower()


@pytest.mark.parametrize("selector_env", _DB_FREE_SELECTOR_ENV_KEYS)
def test_db_free_malformed_selector_uri_blocks_without_crash(
    monkeypatch: Any,
    tmp_path: Path,
    selector_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(selector_env, "postgresql://[bad/path")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert selector_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    assert result.evidence["db_free_runtime"]["checks"][selector_env]["selected"] == "[invalid-uri]"
    assert "bad/path" not in rendered
    assert "postgresql" not in rendered.lower()


@pytest.mark.parametrize("path_env", _DB_FREE_PATH_ENV_KEYS)
@pytest.mark.parametrize("path_case", ["missing", "blank", "outside", "unsafe"])
def test_db_free_required_path_misconfig_blocks_exact_field_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
    path_case: str,
) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    if path_case == "missing":
        monkeypatch.delenv(path_env, raising=False)
    elif path_case == "blank":
        monkeypatch.setenv(path_env, "")
    elif path_case == "outside":
        outside = tmp_path / "outside" / path_env.lower()
        if path_env == "NHMS_SCHEDULER_JOURNAL_ROOT":
            outside.mkdir(parents=True)
        else:
            outside.parent.mkdir(parents=True)
            outside.write_text("{}", encoding="utf-8")
        monkeypatch.setenv(path_env, str(outside))
    else:
        target = paths[path_env]
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        symlink_target = tmp_path / "unsafe-target"
        if path_env == "NHMS_SCHEDULER_JOURNAL_ROOT":
            symlink_target.mkdir(exist_ok=True)
            target.symlink_to(symlink_target, target_is_directory=True)
        else:
            symlink_target.write_text("{}", encoding="utf-8")
            target.symlink_to(symlink_target)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["lock"]["reason"] == "db_free_runtime_preflight_blocked"
    assert path_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


@pytest.mark.parametrize("path_env", _DB_FREE_PATH_ENV_KEYS)
def test_db_free_required_path_db_uri_blocks_without_endpoint_leak(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(path_env, "postgresql://nhms:supersecret@db.prod.example:55433/nhms?token=qhh")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert path_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    path_check = result.evidence["db_free_runtime"]["checks"][path_env]
    assert path_check["path"] == "[uri]"
    assert path_check["scheme"] == "[db-like]"
    assert "supersecret" not in rendered
    assert "db.prod.example" not in rendered
    assert "55433" not in rendered
    assert "token" not in rendered
    assert "postgresql" not in rendered.lower()


@pytest.mark.parametrize("path_env", _DB_FREE_PATH_ENV_KEYS)
def test_db_free_malformed_required_path_uri_blocks_without_crash(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv(path_env, "postgresql://[bad/path")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert path_env in {blocker["field"] for blocker in result.evidence["db_free_runtime"]["blockers"]}
    path_check = result.evidence["db_free_runtime"]["checks"][path_env]
    assert path_check["path"] == "[invalid-uri]"
    assert path_check["scheme"] == "[invalid]"
    assert "bad/path" not in rendered
    assert "postgresql" not in rendered.lower()


@pytest.mark.parametrize("path_env", _DB_FREE_PATH_ENV_KEYS)
def test_db_free_required_path_symlink_loop_blocks_without_crash(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    target = paths[path_env]
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    loop = target.parent / "secret-token-loop"
    if loop.exists() or loop.is_symlink():
        loop.unlink()
    loop.symlink_to(loop)
    monkeypatch.setenv(path_env, str(loop / "child"))
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    blockers = result.evidence["db_free_runtime"]["blockers"]
    assert path_env in {blocker["field"] for blocker in blockers}
    assert ("db_free_required_path_unsafe", path_env) in {
        (blocker["code"], blocker["field"]) for blocker in blockers
    }
    path_check = result.evidence["db_free_runtime"]["checks"][path_env]
    assert path_check["path"] == "[local-path]"
    assert "secret-token-loop" not in rendered
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


@pytest.mark.parametrize("path_env", _DB_FREE_PATH_ENV_KEYS)
@pytest.mark.parametrize("path_case", ["outside_boundary", "traversal_sensitive_component"])
def test_db_free_required_local_path_blocks_without_raw_path_leak(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
    path_case: str,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    if path_case == "outside_boundary":
        raw_path = tmp_path / "outside-boundary" / path_env.lower()
        if path_env == "NHMS_SCHEDULER_JOURNAL_ROOT":
            raw_path.mkdir(parents=True)
        else:
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("{}", encoding="utf-8")
        expected_code = "db_free_required_path_outside_boundary"
        forbidden_fragments = ("outside-boundary", str(raw_path))
    else:
        raw_path = roots["workspace_root"] / "db-free" / ".." / f"{path_env.lower()}-unsafe-secret-token"
        expected_code = "db_free_required_path_unsafe"
        forbidden_fragments = ("..", "unsafe-secret-token", str(raw_path))
    monkeypatch.setenv(path_env, str(raw_path))
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    path_check = result.evidence["db_free_runtime"]["checks"][path_env]
    assert path_check["path"] == "[local-path]"
    blocker = next(
        blocker
        for blocker in result.evidence["db_free_runtime"]["blockers"]
        if blocker["field"] == path_env and blocker["code"] == expected_code
    )
    assert blocker["path"] == "[local-path]"
    for fragment in forbidden_fragments:
        assert fragment not in rendered
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


@pytest.mark.parametrize("path_env", _DB_FREE_FILE_PATH_ENV_KEYS)
def test_db_free_required_file_path_must_be_readable_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
    path_env: str,
) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    target = paths[path_env]
    original_mode = target.stat().st_mode
    target.chmod(0)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    try:
        result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    finally:
        target.chmod(original_mode)
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    blocker = next(
        blocker
        for blocker in result.evidence["db_free_runtime"]["blockers"]
        if blocker["field"] == path_env
    )
    assert blocker["code"] == "db_free_required_path_not_readable"
    assert blocker["path"] == "[local-path]"
    assert result.evidence["db_free_runtime"]["checks"][path_env]["path"] == "[local-path]"
    assert str(target) not in rendered
    assert result.evidence["lock"]["reason"] == "db_free_runtime_preflight_blocked"


@pytest.mark.parametrize(
    "object_uri",
    [
        "s3://user:pass@nhms-prod/scheduler/registry.json?token=secret#frag",
        "s3://nhms-prod:badport/scheduler/registry.json",
        "s3:///scheduler/registry.json",
        "s3://nhms-prod/scheduler/../secret.json",
        "s3://nhms-prod/scheduler/%2e%2e/secret.json",
        "s3://nhms-prod/scheduler/reg\u0001istry.json",
        "s3://other-prod/scheduler/registry.json",
        "s3://nhms-prod/other/registry.json",
        "published://user:pass@manifests/scheduler.json",
        "published://manifests/../secret.json",
        "published://manifests/%2e%2e/secret.json",
        "published://manifests/path%2fsecret.json",
        "published://private/scheduler.json",
    ],
)
def test_db_free_object_uri_misconfig_blocks_without_raw_uri_leak(
    monkeypatch: Any,
    tmp_path: Path,
    object_uri: str,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-prod/scheduler")
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", object_uri)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    blockers = result.evidence["db_free_runtime"]["blockers"]
    assert "NHMS_SCHEDULER_REGISTRY_MANIFEST" in {blocker["field"] for blocker in blockers}
    path_check = result.evidence["db_free_runtime"]["checks"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]
    assert path_check["path"] == "[object-uri]"
    assert path_check["supported_object_uri"] is False
    assert "user:pass" not in rendered
    assert "token=secret" not in rendered
    assert "secret.json" not in rendered
    assert "other-prod" not in rendered
    assert "badport" not in rendered
    assert "nhms-prod" not in rendered


def test_db_free_object_uri_blocks_malformed_object_store_prefix_without_crash(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://[bad/prefix")
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", "s3://nhms-prod/scheduler/registry.json")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    blockers = result.evidence["db_free_runtime"]["blockers"]
    assert "NHMS_SCHEDULER_REGISTRY_MANIFEST" in {blocker["field"] for blocker in blockers}
    assert result.evidence["db_free_runtime"]["checks"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]["path"] == "[object-uri]"
    assert "bad/prefix" not in rendered


def test_db_free_safe_object_uri_paths_pass_runtime_preflight(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-prod/scheduler")
    object_paths = {
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": "s3://nhms-prod/scheduler/registry-manifest.json",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": "s3://nhms-prod/scheduler/readiness-index.json",
        "NHMS_SCHEDULER_STATE_INDEX": "published://manifests/scheduler/state-index.json",
    }
    for key, uri in object_paths.items():
        monkeypatch.setenv(key, uri)

    preflight = ProductionSchedulerConfig().db_free_runtime_preflight()

    assert preflight["status"] == "ready"
    for key in object_paths:
        check = preflight["checks"][key]
        assert check["path"] == "[object-uri]"
        assert check["supported_object_uri"] is True
        assert check.get("bucket") in (None, "[object-bucket]")
        assert check.get("namespace") == "[object-prefix]"
    rendered = json.dumps(preflight, sort_keys=True)
    assert "nhms-prod" not in rendered


def test_file_registry_publisher_and_loader_validate_manifest_last_and_checksum(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=_dt("2026-05-21T06:00:00Z"),
    )

    registry = scheduler_module.FileSchedulerModelRegistry(
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=_dt("2026-06-27T00:00:00Z"),
    )
    page = registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)
    detail = registry.get_model("model_a")
    evidence = registry.scheduler_registry_evidence()

    assert fixture["registry_receipt"]["status"] == "published"
    assert fixture["registry_receipt"]["manifest_last"] is True
    assert fixture["registry_receipt"]["atomic_write"] is True
    assert page["total"] == 1
    assert detail["model_id"] == "model_a"
    assert detail["basin_id"] == "basin_a"
    assert detail["basin_version_id"] == "basin_a_v1"
    assert detail["river_network_version_id"] == "basin_a_rivnet_v1"
    assert detail["resource_profile"]["package_checksum"] == fixture["package_checksum"]
    assert detail["resource_profile"]["manifest_uri"] == fixture["model"]["manifest_uri"]
    assert detail["display_capabilities"] == {"tiles": True}
    assert detail["output_segment_count"] == 3
    assert evidence["status"] == "ready"
    assert evidence["schema_version"] == scheduler_module.REGISTRY_MANIFEST_SCHEMA_VERSION
    assert evidence["manifest"] == "[local-path]"
    assert evidence["model_count"] == 1
    assert evidence["model_ids"] == ["model_a"]
    assert evidence["content_checksum_verified"] is True
    assert "db-free-local-root" not in json.dumps(evidence, sort_keys=True)


def test_file_registry_duplicate_model_id_fails_closed_with_bounded_evidence(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    generated_at = _dt("2026-06-27T00:00:00Z")
    model_a, _package_checksum = _db_free_model_manifest_fixture(roots, _model("model_a", "basin_a"))
    duplicate = {**model_a, "basin_id": "basin_b", "basin_version_id": "basin_b_v1"}
    _write_json_manifest_with_checksum(
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        {
            "schema_version": scheduler_module.REGISTRY_MANIFEST_SCHEMA_VERSION,
            "generated_at": _format_iso_z(generated_at),
            "models": [model_a, duplicate],
        },
    )

    registry = scheduler_module.FileSchedulerModelRegistry(
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )
    page = registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)
    evidence = registry.scheduler_registry_evidence()
    rendered = json.dumps(evidence, sort_keys=True)

    assert page["items"] == []
    assert evidence["status"] == "blocked"
    assert evidence["blockers"][0]["code"] == "registry_duplicate_model_id"
    assert evidence["blockers"][0]["field"] == "models[].model_id"
    assert "db-free-local-root" not in rendered


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("schema", "file_manifest_schema_unsupported"),
        ("stale", "file_manifest_stale"),
        ("checksum", "file_manifest_checksum_mismatch"),
        ("missing_shud_code_version", "registry_model_required_field_missing"),
        ("empty_resource_profile", "file_manifest_mapping_empty"),
        ("model_manifest_missing", "registry_model_package_manifest_missing"),
        ("model_manifest_checksum", "registry_model_package_manifest_checksum_mismatch"),
        ("model_manifest_outside_prefix", "registry_model_package_manifest_unsafe_uri"),
        ("model_package_local_path", "registry_model_package_uri_unsupported_uri"),
    ],
)
def test_file_registry_manifest_fail_closed_cases(
    monkeypatch: Any,
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    generated_at = _dt("2026-06-27T00:00:00Z")
    model_a, _package_checksum = _db_free_model_manifest_fixture(roots, _model("model_a", "basin_a"))
    payload: dict[str, Any] = {
        "schema_version": scheduler_module.REGISTRY_MANIFEST_SCHEMA_VERSION,
        "generated_at": _format_iso_z(generated_at),
        "models": [model_a],
    }
    if case_name == "schema":
        payload["schema_version"] = "nhms.scheduler.file_model_registry.v0"
    elif case_name == "stale":
        payload["generated_at"] = "2026-01-01T00:00:00Z"
    elif case_name == "missing_shud_code_version":
        payload["models"][0].pop("shud_code_version")
    elif case_name == "empty_resource_profile":
        payload["models"][0]["resource_profile"] = {}
    elif case_name == "model_manifest_missing":
        payload["models"][0]["manifest_uri"] = "s3://nhms/models/missing/manifest.json"
    elif case_name == "model_manifest_checksum":
        payload["models"][0]["package_checksum"] = "sha256:bad"
    elif case_name == "model_manifest_outside_prefix":
        payload["models"][0]["manifest_uri"] = "s3://outside/models/model_a/manifest.json"
    elif case_name == "model_package_local_path":
        payload["models"][0]["model_package_uri"] = str(tmp_path / "outside-package")
    _write_json_manifest_with_checksum(paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"], payload)
    if case_name == "checksum":
        broken = json.loads(paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"].read_text(encoding="utf-8"))
        broken["checksum"] = "sha256:bad"
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"].write_text(json.dumps(broken), encoding="utf-8")

    registry = scheduler_module.FileSchedulerModelRegistry(
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )
    page = registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)
    evidence = registry.scheduler_registry_evidence()
    rendered = json.dumps(evidence, sort_keys=True)

    assert page["items"] == []
    assert evidence["status"] == "blocked"
    assert evidence["blockers"][0]["code"] == expected_reason
    assert "db-free-local-root" not in rendered
    assert "outside-package" not in rendered
    assert "outside" not in rendered


def test_file_registry_publisher_failure_keeps_previous_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    generated_at = _dt("2026-06-27T00:00:00Z")
    model_a, _package_checksum = _db_free_model_manifest_fixture(roots, _model("model_a", "basin_a"))
    scheduler_module.publish_scheduler_registry_manifest(
        [model_a],
        paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    previous = paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"].read_bytes()
    missing_ref = {**model_a, "manifest_uri": "s3://nhms/models/missing/manifest.json"}

    with pytest.raises(scheduler_module.SchedulerFileProviderError) as error_info:
        scheduler_module.publish_scheduler_registry_manifest(
            [missing_ref],
            paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"],
            object_store_root=roots["object_store_root"],
            object_store_prefix="s3://nhms",
            generated_at=generated_at,
        )

    assert error_info.value.reason == "registry_model_package_manifest_missing"
    assert paths["NHMS_SCHEDULER_REGISTRY_MANIFEST"].read_bytes() == previous


def test_file_canonical_readiness_publisher_and_provider_use_existing_evaluator(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        generated_at=_dt("2026-06-27T00:00:00Z"),
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=_dt("2026-06-27T00:00:00Z"),
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        policy_identity=fixture["policy_identity"],
        source_object_identity=fixture["source_object_identity"],
        canonical_product_id=fixture["canonical_product_id"],
        model_id="model_a",
        basin_id="basin_a",
    )

    assert fixture["readiness_receipt"]["status"] == "published"
    assert fixture["readiness_receipt"]["index_last"] is True
    assert fixture["readiness_receipt"]["atomic_write"] is True
    assert evidence["ready"] is True
    assert evidence["status"] == "canonical_ready"
    assert evidence["row_count"] == len(GFS_REQUIRED_STANDARD_VARIABLES) * 2
    assert evidence["readiness_index"]["status"] == "ready"
    assert evidence["readiness_index"]["schema_version"] == scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION
    assert evidence["readiness_index"]["content_checksum_verified"] is True
    assert evidence["readiness_index"]["entry_product_row_count"] == len(GFS_REQUIRED_STANDARD_VARIABLES) * 2


def test_file_canonical_readiness_provider_uses_product_catalog_when_index_products_are_externalized(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forecast_hours = (0, 3)
    policy_identity = {"source": "gfs", "forecast_hours": list(forecast_hours)}
    source_object_identity = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    products = _file_readiness_products(
        roots,
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        products=products,
        generated_at=_dt("2026-06-27T00:00:00Z"),
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    catalog_rows: list[dict[str, Any]] = []
    for product in products:
        row = dict(product)
        row["cycle_time"] = _format_iso_z(row["cycle_time"])
        row["valid_time"] = _format_iso_z(row["valid_time"])
        catalog_rows.append(row)
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    store.write_bytes_atomic(
        f"canonical/gfs/{format_cycle_time(cycle_time)}/_catalog/catalog.json",
        json.dumps(
            {
                "schema_version": "nhms.canonical.product_catalog.v1",
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "products": catalog_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    scheduler_module.publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "model_id": "model_a",
                "basin_id": "basin_a",
                "canonical_product_id": fixture["canonical_product_id"],
                "forecast_hours": list(forecast_hours),
                "policy_identity": policy_identity,
                "source_object_identity": source_object_identity,
                "products": [],
            }
        ],
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-06-27T00:00:00Z"),
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=_dt("2026-06-27T00:00:00Z"),
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
        canonical_product_id=fixture["canonical_product_id"],
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is True
    assert evidence["status"] == "canonical_ready"
    assert evidence["row_count"] == len(GFS_REQUIRED_STANDARD_VARIABLES) * len(forecast_hours)
    assert evidence["readiness_index"]["product_row_count"] == 0
    assert evidence["readiness_index"]["entry_product_source"] == "catalog"
    assert evidence["readiness_index"]["entry_product_row_count"] == len(products)
    assert evidence["readiness_index"]["canonical_product_catalog"]["status"] == "ready"
    assert evidence["readiness_index"]["canonical_product_catalog"]["product_row_count"] == len(products)


def test_file_canonical_readiness_provider_uses_product_catalog_when_identity_is_missing(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forecast_hours = (0, 3)
    policy_identity = {"source": "gfs", "forecast_hours": list(forecast_hours)}
    source_object_identity = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    products = _file_readiness_products(
        roots,
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    catalog_rows: list[dict[str, Any]] = []
    for product in products:
        row = dict(product)
        row["cycle_time"] = _format_iso_z(row["cycle_time"])
        row["valid_time"] = _format_iso_z(row["valid_time"])
        catalog_rows.append(row)
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    store.write_bytes_atomic(
        f"canonical/gfs/{format_cycle_time(cycle_time)}/_catalog/catalog.json",
        json.dumps(
            {
                "schema_version": "nhms.canonical.product_catalog.v1",
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "products": catalog_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    generated_at = _dt("2026-06-27T00:00:00Z")
    scheduler_module.publish_canonical_readiness_index(
        [],
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
        canonical_product_id=f"canon_gfs_{format_cycle_time(cycle_time)}",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is True
    assert evidence["status"] == "canonical_ready"
    assert evidence["row_count"] == len(products)
    assert evidence["readiness_index"]["entry_status"] == "missing"
    assert evidence["readiness_index"]["entry_product_source"] == "catalog"
    assert evidence["readiness_index"]["entry_product_row_count"] == len(products)
    assert evidence["readiness_index"]["canonical_product_catalog"]["status"] == "ready"


def test_file_canonical_readiness_provider_missing_identity_is_fresh_zero_row(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forecast_hours = (0, 3)
    policy_identity = {"source": "gfs", "forecast_hours": list(forecast_hours)}
    source_object_identity = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052106/manifest.json"}
    generated_at = _dt("2026-06-27T00:00:00Z")
    scheduler_module.publish_canonical_readiness_index(
        [],
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
        canonical_product_id=f"canon_gfs_{format_cycle_time(cycle_time)}",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is False
    assert evidence["status"] == "canonical_incomplete"
    assert evidence["candidate_row_count"] == 0
    assert evidence["expected_leads"] == [0, 3]
    assert evidence["readiness_index"]["status"] == "ready"
    assert evidence["readiness_index"]["entry_status"] == "missing"
    assert evidence["readiness_index"]["entry_product_row_count"] == 0
    assert evidence["readiness_index"]["entry_product_source"] == "missing_identity_zero_rows"
    assert scheduler_module._canonical_evidence_is_fresh_zero_row(evidence) is True


def test_file_canonical_readiness_provider_treats_stale_empty_identity_as_fresh_zero_row(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    forecast_hours = (0, 3)
    generated_at = _dt("2026-06-27T00:00:00Z")
    scheduler_module.publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "model_id": "model_a",
                "basin_id": "basin_a",
                "canonical_product_id": f"canon_gfs_{format_cycle_time(cycle_time)}",
                "forecast_hours": list(forecast_hours),
                "policy_identity": {"source": "gfs", "manifest_digest": "old-policy"},
                "source_object_identity": {"source": "gfs", "manifest_digest": "old-object"},
                "products": [],
            }
        ],
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity={"source": "gfs", "manifest_digest": "new-policy"},
        source_object_identity={"source": "gfs", "manifest_digest": "new-object"},
        canonical_product_id=f"canon_gfs_{format_cycle_time(cycle_time)}",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is False
    assert evidence["status"] == "canonical_incomplete"
    assert evidence["candidate_row_count"] == 0
    assert evidence["readiness_index"]["entry_status"] == "identity_mismatch_empty_entry"
    assert evidence["readiness_index"]["entry_product_row_count"] == 0
    assert scheduler_module._canonical_evidence_is_fresh_zero_row(evidence) is True


def test_file_canonical_readiness_provider_infers_root_from_scheduler_index_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    index_path = roots["object_store_root"] / "scheduler" / "canonical-readiness" / "index-last.json"
    cycle_time = _dt("2026-05-21T12:00:00Z")
    generated_at = _dt("2026-06-27T00:00:00Z")
    scheduler_module.publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": _format_iso_z(cycle_time),
                "model_id": "model_a",
                "basin_id": "basin_a",
                "canonical_product_id": f"canon_gfs_{format_cycle_time(cycle_time)}",
                "forecast_hours": [0, 3],
                "policy_identity": {"source": "gfs", "manifest_digest": "old-policy"},
                "source_object_identity": {"source": "gfs", "manifest_digest": "old-object"},
                "products": [],
            }
        ],
        index_path,
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        generated_at=generated_at,
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        index_path,
        object_store_prefix="s3://nhms",
        now=generated_at,
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        policy_identity={"source": "gfs", "manifest_digest": "new-policy"},
        source_object_identity={"source": "gfs", "manifest_digest": "new-object"},
        canonical_product_id=f"canon_gfs_{format_cycle_time(cycle_time)}",
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is False
    assert evidence["candidate_row_count"] == 0
    assert evidence["readiness_index"]["entry_status"] == "identity_mismatch_empty_entry"
    assert evidence["readiness_index"]["canonical_product_catalog"]["source"] == "index_empty"
    assert scheduler_module._canonical_evidence_is_fresh_zero_row(evidence) is True


def test_file_canonical_readiness_evidence_redacts_identity_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    policy_identity = {"source": "gfs", "policy_uri": "s3://private-bucket/raw/policy.json"}
    source_object_identity = {
        "source": "gfs",
        "manifest_uri": "s3://private-bucket/raw/gfs/2026052106/manifest.json",
        "manifest_path": "/home/ghdc/nwm/object-store/raw/gfs/2026052106/manifest.json",
    }
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0,),
        generated_at=_dt("2026-06-27T00:00:00Z"),
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=_dt("2026-06-27T00:00:00Z"),
    )

    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0,),
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
        canonical_product_id=fixture["canonical_product_id"],
        model_id="model_a",
        basin_id="basin_a",
    )
    rendered = json.dumps(evidence, sort_keys=True)

    assert evidence["ready"] is True
    assert evidence["policy_identity"]["policy_uri"] == "[object-uri]"
    assert evidence["source_object_identity"]["manifest_uri"] == "[object-uri]"
    assert evidence["source_object_identity"]["manifest_path"] == "[local-path]"
    assert "private-bucket" not in rendered
    assert "/home/ghdc" not in rendered

    stale_cache = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0,),
        policy_identity={"source": "gfs", "policy_uri": "s3://private-bucket/raw/other-policy.json"},
        source_object_identity=source_object_identity,
        canonical_product_id=fixture["canonical_product_id"],
        model_id="model_a",
        basin_id="basin_a",
    )
    stale_cache_rendered = json.dumps(stale_cache, sort_keys=True)

    assert stale_cache["ready"] is False
    assert stale_cache["reason"] == "canonical_identity_mismatch_cache_miss"
    assert stale_cache["candidate_row_count"] == 0
    assert stale_cache["readiness_index"]["entry_status"] == "identity_mismatch_stale_products"
    assert stale_cache["readiness_index"]["stale_product_row_count"] > 0
    assert stale_cache["policy_identity"]["policy_uri"] == "[object-uri]"
    assert stale_cache["source_object_identity"]["manifest_uri"] == "[object-uri]"
    assert stale_cache["source_object_identity"]["manifest_path"] == "[local-path]"
    assert "private-bucket" not in stale_cache_rendered
    assert "/home/ghdc" not in stale_cache_rendered


def test_file_canonical_readiness_publisher_failure_keeps_previous_index(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-06-27T00:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0,),
        generated_at=generated_at,
    )
    previous = paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"].read_bytes()
    broken_product = {
        "source_id": "gfs",
        "cycle_time": _format_iso_z(cycle_time),
        "lead_time_hours": 0,
        "variable": "air_temperature_2m",
        "object_uri": "s3://nhms/canonical/gfs/missing/f000.dat",
        "checksum": "sha256:missing",
        "lineage_json": {
            "policy_identity": fixture["policy_identity"],
            "source_object_identity": fixture["source_object_identity"],
        },
    }

    with pytest.raises(scheduler_module.SchedulerFileProviderError) as error_info:
        scheduler_module.publish_canonical_readiness_index(
            [
                {
                    "source_id": "gfs",
                    "cycle_time": _format_iso_z(cycle_time),
                    "model_id": "model_a",
                    "basin_id": "basin_a",
                    "canonical_product_id": fixture["canonical_product_id"],
                    "forecast_hours": [0],
                    "policy_identity": fixture["policy_identity"],
                    "source_object_identity": fixture["source_object_identity"],
                    "products": [broken_product],
                }
            ],
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            object_store_root=roots["object_store_root"],
            object_store_prefix="s3://nhms",
            generated_at=generated_at,
        )

    assert error_info.value.reason == "readiness_product_object_unreadable"
    assert paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"].read_bytes() == previous


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("missing", "file_manifest_missing"),
        ("stale", "file_manifest_stale"),
        ("schema", "file_manifest_schema_unsupported"),
        ("checksum", "file_manifest_checksum_mismatch"),
        ("deep", "file_manifest_json_depth_exceeded"),
        ("object_missing", "readiness_product_object_missing"),
        ("self_reported_checksum", "readiness_product_object_checksum_mismatch"),
        ("local_object_uri", "readiness_product_object_unsupported_uri"),
        ("identity", "canonical_readiness_index_identity_mismatch"),
        ("forecast_hours_missing", "canonical_readiness_index_forecast_hours_missing"),
    ],
)
def test_file_canonical_readiness_index_fail_closed_cases(
    monkeypatch: Any,
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-06-27T00:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0,),
        generated_at=generated_at,
    )
    if case_name == "missing":
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"].unlink()
    elif case_name == "stale":
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "generated_at": "2026-01-01T00:00:00Z",
                "entries": [],
            },
        )
    elif case_name == "schema":
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": "nhms.scheduler.canonical_readiness_index.v0",
                "generated_at": _format_iso_z(generated_at),
                "entries": [],
            },
        )
    elif case_name == "checksum":
        payload = json.loads(paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"].read_text(encoding="utf-8"))
        payload["checksum"] = "sha256:bad"
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"].write_text(json.dumps(payload), encoding="utf-8")
    elif case_name == "deep":
        nested: dict[str, Any] = {}
        cursor = nested
        for index in range(70):
            cursor[f"level_{index}"] = {}
            cursor = cursor[f"level_{index}"]
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "generated_at": _format_iso_z(generated_at),
                "entries": [],
                "nested": nested,
            },
        )
    elif case_name == "object_missing":
        product = _file_readiness_products(
            roots,
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0,),
            policy_identity=fixture["policy_identity"],
            source_object_identity=fixture["source_object_identity"],
        )[0]
        store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
        (roots["object_store_root"] / store.normalize_key(product["object_uri"])).unlink()
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "generated_at": _format_iso_z(generated_at),
                "entries": [
                    {
                        "source_id": "gfs",
                        "cycle_time": _format_iso_z(cycle_time),
                        "model_id": "model_a",
                        "basin_id": "basin_a",
                        "canonical_product_id": fixture["canonical_product_id"],
                        "forecast_hours": [0],
                        "policy_identity": fixture["policy_identity"],
                        "source_object_identity": fixture["source_object_identity"],
                        "products": [product],
                    }
                ],
            },
        )
    elif case_name == "self_reported_checksum":
        product = _file_readiness_products(
            roots,
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0,),
            policy_identity=fixture["policy_identity"],
            source_object_identity=fixture["source_object_identity"],
        )[0]
        store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
        object_path = roots["object_store_root"] / store.normalize_key(product["object_uri"])
        object_path.write_text(json.dumps({"checksum": product["checksum"]}), encoding="utf-8")
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "generated_at": _format_iso_z(generated_at),
                "entries": [
                    {
                        "source_id": "gfs",
                        "cycle_time": _format_iso_z(cycle_time),
                        "model_id": "model_a",
                        "basin_id": "basin_a",
                        "canonical_product_id": fixture["canonical_product_id"],
                        "forecast_hours": [0],
                        "policy_identity": fixture["policy_identity"],
                        "source_object_identity": fixture["source_object_identity"],
                        "products": [product],
                    }
                ],
            },
        )
    elif case_name == "local_object_uri":
        product = _file_readiness_products(
            roots,
            source_id="gfs",
            cycle_time=cycle_time,
            forecast_hours=(0,),
            policy_identity=fixture["policy_identity"],
            source_object_identity=fixture["source_object_identity"],
        )[0]
        product["object_uri"] = str(tmp_path / "outside-canonical.dat")
        _write_json_manifest_with_checksum(
            paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
            {
                "schema_version": scheduler_module.CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "generated_at": _format_iso_z(generated_at),
                "entries": [
                    {
                        "source_id": "gfs",
                        "cycle_time": _format_iso_z(cycle_time),
                        "model_id": "model_a",
                        "basin_id": "basin_a",
                        "canonical_product_id": fixture["canonical_product_id"],
                        "forecast_hours": [0],
                        "policy_identity": fixture["policy_identity"],
                        "source_object_identity": fixture["source_object_identity"],
                        "products": [product],
                    }
                ],
            },
        )

    provider = scheduler_module.FileCanonicalReadinessProvider(
        paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"],
        object_store_root=roots["object_store_root"],
        object_store_prefix="s3://nhms",
        now=generated_at,
    )
    evidence = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        forecast_hours=(0, 3) if case_name == "forecast_hours_missing" else (0,),
        policy_identity=(
            {"source": "gfs", "forecast_hours": [999]}
            if case_name == "identity"
            else fixture["policy_identity"]
        ),
        source_object_identity=fixture["source_object_identity"],
        canonical_product_id=fixture["canonical_product_id"],
        model_id="model_a",
        basin_id="basin_a",
    )

    assert evidence["ready"] is False
    assert evidence["status"] == "canonical_unavailable"
    assert evidence["reason"] == expected_reason
    assert evidence["readiness_index"]["index"] == "[local-path]"
    rendered = json.dumps(evidence, sort_keys=True)
    assert "db-free-local-root" not in rendered
    assert str(paths["NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"]) not in rendered
    assert "outside-canonical" not in rendered


def test_valid_db_free_from_env_uses_file_registry_and_canonical_readiness_without_db_factories(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setenv("NHMS_PRODUCTION_FORCING_ENABLED", "true")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=fixture["policy_identity"],
                source_object_identity=fixture["source_object_identity"],
            )
        },
    )

    def fail_db_factory(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("DB-free file providers must not construct DB-backed factory")

    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._active_repository_from_env",
        lambda: pytest.fail("DB-free from_env must not construct DB-backed active repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._canonical_readiness_provider_from_env",
        lambda: pytest.fail("DB-free from_env must not construct DB-backed readiness provider"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._orchestrator_repository_from_env",
        lambda: pytest.fail("DB-free file providers must not construct orchestrator repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._retry_service_from_env",
        lambda: pytest.fail("DB-free file providers must not construct retry service"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._forcing_producer_from_env",
        lambda: pytest.fail("DB-free from_env must not construct forcing producer"),
    )
    monkeypatch.setattr("packages.common.met_store.PsycopgMetStore.from_env", staticmethod(fail_db_factory))
    monkeypatch.setattr(
        "packages.common.state_manager.PsycopgStateSnapshotRepository.from_env",
        staticmethod(fail_db_factory),
    )
    monkeypatch.setattr(
        "services.orchestrator.chain.PsycopgOrchestratorRepository.from_env",
        staticmethod(fail_db_factory),
    )
    monkeypatch.setattr(
        "services.orchestrator.chain_repository.PsycopgOrchestratorRepository.from_env",
        staticmethod(fail_db_factory),
    )
    monkeypatch.setattr("services.orchestrator.persistence.PipelineStore", fail_db_factory)
    monkeypatch.setattr("workers.forcing_producer.producer.ForcingProducer.from_env", staticmethod(fail_db_factory))
    monkeypatch.setattr(
        scheduler_module.StateManager,
        "from_env",
        staticmethod(lambda: pytest.fail("DB-free file providers must not construct state manager")),
    )

    config = ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z"))
    result = _RealProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "planned"
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["lease"]["lock_path"] == "[local-path]"
    assert result.evidence["model_discovery"]["registry"]["status"] == "ready"
    assert result.evidence["model_discovery"]["registry"]["selected_model_ids"] == ["model_a"]
    assert result.evidence["candidates"][0]["state_evidence"]["canonical_readiness"]["ready"] is True
    assert (
        result.evidence["candidates"][0]["state_evidence"]["canonical_readiness"]["readiness_index"]["status"]
        == "ready"
    )
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["runtime_config"]["database_url_configured"] is False
    assert result.evidence["runtime_config"]["scheduler_state_backend"] == "file"
    assert result.evidence["runtime_config"]["scheduler_registry_backend"] == "file"
    assert result.evidence["runtime_config"]["scheduler_canonical_readiness_backend"] == "file"
    assert result.evidence["runtime_config"]["scheduler_journal_backend"] == "file"
    assert result.evidence["runtime_config"]["scheduler_state_index_backend"] == "file"
    assert set(result.evidence["runtime_config"]["db_free_runtime"]["paths"]) == set(_DB_FREE_PATH_ENV_KEYS)
    assert "postgres" not in rendered.lower()
    assert "psycopg" not in rendered.lower()
    assert "advisory" not in rendered.lower()
    assert "db-free-local-root" not in rendered
    assert str(config.lock_path) not in rendered


def test_db_free_strict_warm_start_uses_ready_file_state_index_without_db_state_repository(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    state_fixture = _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setattr(
        "packages.common.state_manager.PsycopgStateSnapshotRepository.from_env",
        staticmethod(lambda: pytest.fail("DB-free strict warm start must not construct PostgreSQL state repository")),
    )
    monkeypatch.setattr(
        scheduler_module.StateManager,
        "from_env",
        staticmethod(lambda: pytest.fail("DB-free strict warm start must not construct DB-backed StateManager")),
    )
    monkeypatch.setattr(
        scheduler_module.FileStateSnapshotIndexRepository,
        "get_latest_usable_state",
        lambda *_args, **_kwargs: pytest.fail("Strict file state lookup must not use latest fallback"),
    )
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail("candidate construction must not build orchestrator"),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert len(candidates) == 1
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    state_evidence = candidates[0].state_evidence
    assert state_evidence["ready"] is True
    assert state_evidence["candidate_state"]["init_state_uri"] == state_fixture["entries"][0]["state_uri"]
    assert state_evidence["candidate_state"]["init_state_lineage"]["lead_hours"] == (
        state_fixture["entries"][0]["lead_hours"]
    )
    assert state_evidence["state_snapshot_index"]["status"] == "ready"
    assert state_evidence["state_snapshot_index"]["entry_status"] == "ready"
    assert state_evidence["state_snapshot_index"]["object_evidence"]["exists"] is True


def test_db_free_strict_warm_start_discovery_reopens_completed_cold_start_terminal(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }

    class CompletedColdStartRepository(FakeCandidateStateRepository):
        def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

    run_id = "fcst_gfs_2026052106_model_a"
    repository = CompletedColdStartRepository(
        {
            "hydro_status": "published",
            "hydro_run": {
                "run_id": run_id,
                "status": "published",
                "init_state_id": None,
                "output_uri": f"s3://nhms/runs/{run_id}/output/",
            },
        }
    )
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at),
        registry=FakeRegistry([model]),
        adapters={},
        active_repository=repository,
    )

    status = scheduler._cycle_completion_status(
        CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=cycle_time,
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        [scheduler_module._coerce_registered_model(model)],
        horizon={},
    )

    assert status == "gap"


def test_db_free_strict_warm_start_resubmits_completed_cold_start_terminal(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    state_fixture = _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }

    class CompletedColdStartRepository(FakeCandidateStateRepository):
        def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

    run_id = "fcst_gfs_2026052106_model_a"
    repository = CompletedColdStartRepository(
        {
            "hydro_status": "published",
            "hydro_run": {
                "run_id": run_id,
                "status": "published",
                "init_state_id": None,
                "output_uri": f"s3://nhms/runs/{run_id}/output/",
            },
        }
    )
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at),
        registry=FakeRegistry([model]),
        adapters={},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: pytest.fail("candidate construction must not build orchestrator"),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert len(candidates) == 1
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    evidence = candidates[0].state_evidence
    assert evidence["reason"] == "strict_warm_start_terminal_init_state_mismatch"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["candidate_state"]["init_state_id"] == state_fixture["entries"][0]["state_id"]
    assert evidence["strict_warm_start"]["ready"] is True


def test_db_free_strict_warm_start_reopens_completed_producer_missing_successor_checkpoint(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-20T12:00:00Z")
    required_from = _dt("2026-05-21T00:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=required_from,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }

    class CompletedProducerRepository(FakeCandidateStateRepository):
        def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
            del source_id, cycle_time, model_id
            return True

    run_id = "fcst_gfs_2026052012_model_a"
    repository = CompletedProducerRepository(
        {
            "hydro_status": "published",
            "hydro_run": {
                "run_id": run_id,
                "status": "published",
                "init_state_id": None,
                "output_uri": f"s3://nhms/runs/{run_id}/output/",
            },
        }
    )
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setenv("NHMS_FORECAST_WARM_START_REQUIRED_FROM", _format_iso_z(required_from))
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 12)),
        registry=FakeRegistry([model]),
        adapters={},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: pytest.fail("candidate construction must not build orchestrator"),
    )

    discovery = CycleDiscovery(
        cycle_id="gfs_2026052012",
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_hour=12,
        available=True,
        status="discovered",
    )
    status = scheduler._cycle_completion_status(
        discovery,
        [scheduler_module._coerce_registered_model(model)],
        horizon={},
    )
    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[scheduler_module.SchedulerSourceCycle(discovery=discovery, horizon={})],
    )

    assert status == "gap"
    assert len(candidates) == 1
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    evidence = candidates[0].state_evidence
    assert evidence["reason"] == "strict_warm_start_successor_checkpoint_missing"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["successor_state"]["successor_cycle_time"] == _format_iso_z(required_from)
    assert evidence["successor_state"]["reason"] == "state_snapshot_index_exact_checkpoint_missing"


def test_db_free_strict_warm_start_required_lead_uses_previous_allowed_cycle_f006(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    state_content = b"db-free-strict-warm-start-state-f006\n"
    state_uri = store.write_bytes_atomic(
        "states/gfs/model_a/2026052106/gfs_2026052100/f006/state.cfg.ic",
        state_content,
    )
    state_fixture = _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[
            {
                "state_id": "state_gfs_model_a_2026052106_gfs_2026052100_f006",
                "model_id": "model_a",
                "run_id": "analysis_gfs_2026052100_model_a",
                "source_id": "gfs",
                "valid_time": _format_iso_z(cycle_time),
                "state_uri": state_uri,
                "checksum": f"sha256:{sha256_bytes(state_content)}",
                "usable_flag": True,
                "cycle_id": "gfs_2026052100",
                "lead_hours": 6,
                "model_package_version": "s3://nhms/models/model_a/package/",
                "model_package_checksum": fixture["package_checksum"],
            }
        ],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 6, 12, 18)),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail("candidate construction must not build orchestrator"),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert len(candidates) == 1
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    state_evidence = candidates[0].state_evidence
    assert state_evidence["ready"] is True
    assert state_evidence["candidate_state"]["init_state_id"] == state_fixture["entries"][0]["state_id"]
    assert state_evidence["candidate_state"]["init_state_lineage"]["cycle_id"] == "gfs_2026052100"
    assert state_evidence["candidate_state"]["init_state_lineage"]["lead_hours"] == 6


def test_db_free_strict_warm_start_required_lead_blocks_stale_f012_for_six_hour_cycle(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    state_content = b"db-free-strict-warm-start-state-stale-f012\n"
    state_uri = store.write_bytes_atomic(
        "states/gfs/model_a/2026052106/gfs_2026052018/f012/state.cfg.ic",
        state_content,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[
            {
                "state_id": "state_gfs_model_a_2026052106_gfs_2026052018_f012",
                "model_id": "model_a",
                "run_id": "analysis_gfs_2026052018_model_a",
                "source_id": "gfs",
                "valid_time": _format_iso_z(cycle_time),
                "state_uri": state_uri,
                "checksum": f"sha256:{sha256_bytes(state_content)}",
                "usable_flag": True,
                "cycle_id": "gfs_2026052018",
                "lead_hours": 12,
                "model_package_version": "s3://nhms/models/model_a/package/",
                "model_package_checksum": fixture["package_checksum"],
            }
        ],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 6, 12, 18)),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail("blocked candidate must not build orchestrator"),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert candidates == []
    assert len(blocked) == 1
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    assert blocked[0].reason == "state_snapshot_index_lead_hours_mismatch"
    assert blocked[0].state_evidence["reason"] == "state_snapshot_index_lead_hours_mismatch"
    assert "candidate_state" not in blocked[0].state_evidence


def test_db_free_strict_warm_start_blocks_missing_file_state_index_without_latest_fallback(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setattr(
        "packages.common.state_manager.PsycopgStateSnapshotRepository.from_env",
        staticmethod(lambda: pytest.fail("DB-free strict warm start must not construct PostgreSQL state repository")),
    )
    monkeypatch.setattr(
        scheduler_module.FileStateSnapshotIndexRepository,
        "get_latest_usable_state",
        lambda *_args, **_kwargs: pytest.fail("Strict file state lookup must not use latest fallback"),
    )
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail("blocked candidate must not build orchestrator"),
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert candidates == []
    assert len(blocked) == 1
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    assert blocked[0].reason == "state_snapshot_index_exact_checkpoint_missing"
    state_evidence = blocked[0].state_evidence
    assert state_evidence["ready"] is False
    assert state_evidence["reason"] == "state_snapshot_index_exact_checkpoint_missing"
    assert state_evidence["state_snapshot_index"]["status"] == "ready"
    assert state_evidence["state_snapshot_index"]["entry_count"] == 0
    assert "candidate_state" not in state_evidence
    assert "latest" not in json.dumps(state_evidence, sort_keys=True).lower()


def test_db_free_strict_warm_start_bootstrap_boundary_skips_prior_state_index_gate(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setenv("NHMS_FORECAST_WARM_START_REQUIRED_FROM", "2026-05-21T12:00:00Z")
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at),
        registry=FakeRegistry([model]),
        adapters={},
    )

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052106",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=6,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )

    assert len(candidates) == 1
    assert blocked == []
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []


def test_db_free_strict_warm_start_run_once_blocks_corrupt_file_state_index_before_mutation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    paths["NHMS_SCHEDULER_STATE_INDEX"].write_text("{not-json", encoding="utf-8")
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setattr(
        scheduler_module.FileStateSnapshotIndexRepository,
        "get_latest_usable_state",
        lambda *_args, **_kwargs: pytest.fail("Strict file state lookup must not use latest fallback"),
    )
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, dry_run=False),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=scheduler_module.FileOrchestrationJournalRepository(paths["NHMS_SCHEDULER_JOURNAL_ROOT"]),
        orchestrator_factory=lambda _source_id: pytest.fail("blocked candidate must not build orchestrator"),
    )

    result = scheduler.run_once()

    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "state_snapshot_index_malformed_json"
    assert blocked["state_evidence"]["reason"] == "state_snapshot_index_malformed_json"
    assert "candidate_state" not in blocked["state_evidence"]
    assert "latest" not in json.dumps(blocked["state_evidence"], sort_keys=True).lower()


def test_db_free_strict_warm_start_refreshes_file_state_index_between_passes(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, dry_run=False),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=scheduler_module.FileOrchestrationJournalRepository(paths["NHMS_SCHEDULER_JOURNAL_ROOT"]),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    first = scheduler.run_once()
    state_fixture = _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
    )
    second = scheduler.run_once()

    assert first.evidence["counts"]["submitted_count"] == 0
    assert first.evidence["blocked_candidates"][0]["reason"] == "state_snapshot_index_exact_checkpoint_missing"
    assert second.status == "submitted"
    assert second.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls
    assert orchestrator.calls[0]["basins"][0]["init_state_id"] == state_fixture["entries"][0]["state_id"]


def test_db_free_strict_warm_start_run_once_submits_basin_manifest_init_state(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    state_fixture = _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, dry_run=False),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=scheduler_module.FileOrchestrationJournalRepository(paths["NHMS_SCHEDULER_JOURNAL_ROOT"]),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert orchestrator.calls
    submitted_basin = orchestrator.calls[0]["basins"][0]
    state_entry = state_fixture["entries"][0]
    assert submitted_basin["init_state_id"] == state_entry["state_id"]
    assert submitted_basin["init_state_uri"] == state_entry["state_uri"]
    assert submitted_basin["init_state_checksum"] == state_entry["checksum"]
    assert submitted_basin["init_state_valid_time"] == state_entry["valid_time"]
    assert submitted_basin["init_state_quality"] == "fresh"
    assert submitted_basin["init_state_lineage"]["source_id"] == "gfs"
    assert submitted_basin["init_state_lineage"]["lead_hours"] == state_entry["lead_hours"]
    assert submitted_basin["init_state_lineage"]["model_package_checksum"] == fixture["package_checksum"]
    assert submitted_basin["state_evidence"]["candidate_state"]["init_state_id"] == state_entry["state_id"]
    assert result.evidence["candidates"][0]["state_evidence"]["state_snapshot_index"]["entry_status"] == "ready"


def test_db_free_strict_warm_start_blocks_active_slurm_before_status_sync(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T06:00:00Z")
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    repository = CandidateAndActiveRepository(
        {
            "pipeline_status": "running",
            "pipeline_jobs": [
                {
                    "job_id": "job_forcing",
                    "status": "running",
                    "stage": "forcing",
                    "slurm_job_id": "7777",
                }
            ],
        },
        [{"job_id": "job_forcing", "slurm_job_id": "7777", "status": "running", "stage": "forcing"}],
    )
    factory_calls: list[str] = []
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")

    class SyncMustNotRunOrchestrator(FakeProductionOrchestrator):
        def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
            del cycle_id
            raise AssertionError("sync_cycle_statuses must not run before strict warm-start gate")

    def factory(source_id: str) -> SyncMustNotRunOrchestrator:
        factory_calls.append(source_id)
        return SyncMustNotRunOrchestrator()

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, dry_run=False),
        registry=FakeRegistry([model]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=repository,
        orchestrator_factory=factory,
    )

    result = scheduler.run_once()

    assert factory_calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["counts"]["slurm_status_sync_count"] == 0
    assert result.evidence["no_mutation_proof"]["slurm_status_sync_called"] is False
    assert result.evidence["blocked_candidates"][0]["reason"] == "state_snapshot_index_exact_checkpoint_missing"
    assert result.evidence["blocked_candidates"][0]["state_evidence"]["reason"] == (
        "state_snapshot_index_exact_checkpoint_missing"
    )


def test_db_free_orchestrator_uses_slurm_gateway_retry_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.setenv("SLURM_GATEWAY_MAX_RETRIES", "9")
    monkeypatch.setenv("SLURM_GATEWAY_RETRY_BACKOFF_SECONDS", "[7,11]")
    repository = scheduler_module.FileOrchestrationJournalRepository(paths["NHMS_SCHEDULER_JOURNAL_ROOT"])
    captured: dict[str, Any] = {}

    class CapturingFileRetryService:
        def __init__(self, repository_arg: Any, config: Any | None = None) -> None:
            captured["retry_repository"] = repository_arg
            captured["retry_config"] = config

    class CapturingForecastOrchestrator:
        def __init__(self, **kwargs: Any) -> None:
            captured["orchestrator_kwargs"] = kwargs

    monkeypatch.setattr(scheduler_module, "FileJournalRetryService", CapturingFileRetryService)
    monkeypatch.setattr(scheduler_module, "ForecastOrchestrator", CapturingForecastOrchestrator)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._retry_service_from_env",
        lambda: pytest.fail("DB-free orchestrator must not construct DB retry service"),
    )
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z")),
        registry=object(),
        adapters={},
        active_repository=repository,
    )

    orchestrator = scheduler._default_orchestrator_for("gfs", state_manager=None)

    assert isinstance(orchestrator, CapturingForecastOrchestrator)
    assert captured["retry_repository"] is repository
    assert captured["orchestrator_kwargs"]["retry_service"] is not None
    assert captured["retry_config"].max_retries == 9
    assert captured["retry_config"].backoff_schedule == [7, 11]


def test_db_free_file_providers_refresh_between_scheduler_passes(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    policy_identity = {"source": "gfs", "forecast_hours": list(_gfs_default_forecast_hours())}
    source_object_identity = {"source": "gfs", "manifest_object_key": "raw/gfs/2026052112/manifest.json"}
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
            )
        },
    )
    scheduler = _RealProductionScheduler.from_env(ProductionSchedulerConfig(now=_dt("2026-05-21T12:00:00Z")))

    first = scheduler.run_once()

    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=_dt("2026-05-21T12:00:00Z"),
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    )
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    assert fixture["policy_identity"] == policy_identity
    second = scheduler.run_once()

    assert first.status == "preflight_blocked"
    assert first.evidence["execution_boundary"] == "db_free_registry_blocked"
    assert second.status == "planned"
    assert second.evidence["model_discovery"]["registry"]["status"] == "ready"
    assert second.evidence["candidates"][0]["state_evidence"]["canonical_readiness"]["ready"] is True


def test_db_free_injected_collaborators_plan_without_unimplemented_provider_blocker(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    config = ProductionSchedulerConfig(dry_run=True, now=_dt("2026-05-21T12:00:00Z"))
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: pytest.fail("dry-run scheduler must not construct orchestrator"),
    )

    result = scheduler.run_once()

    assert result.status == "planned"
    assert result.evidence["execution_boundary"] == "planning_only"
    assert "provider_blocker" not in result.evidence.get("db_free_runtime", {})
    assert len(result.evidence["candidates"]) == 1
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_injected_factory_ready_candidate_submit_blocks_without_factory_call(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    factory_calls: list[str] = []
    forcing_producer = FakeForcingProducer()

    def _factory(source_id: str) -> FakeProductionOrchestrator:
        factory_calls.append(source_id)
        return FakeProductionOrchestrator()

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(dry_run=False, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=forcing_producer,
        orchestrator_factory=_factory,
    )

    result = scheduler.run_once()

    assert factory_calls == []
    assert forcing_producer.calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "db_free_journal_write_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["model_run_evidence"][0]["evidence_pre_execution"]["reason"] == (
        "db_free_file_journal_write_not_implemented"
    )
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_journal_write_block_forces_retention_dry_run_before_deletion(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    old_cycle = format_cycle_time(now - timedelta(days=30))
    expired_file = roots["object_store_root"] / "raw" / "gfs" / old_cycle / "gfs.f000.nc"
    expired_file.parent.mkdir(parents=True)
    expired_file.write_text("expired\n", encoding="utf-8")
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)
    orchestrator = FakeProductionOrchestrator()

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(dry_run=False, now=now),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert expired_file.exists()
    assert orchestrator.calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "db_free_journal_write_blocked"
    assert result.evidence["retention"]["status"] == "completed"
    assert result.evidence["retention"]["dry_run"] is True
    assert result.evidence["retention"]["forced_dry_run_by_scheduler"] is True
    assert result.evidence["retention"]["forced_dry_run_reason"] == "db_free_journal_write_blocked"
    assert result.evidence["retention"]["planned"]
    assert result.evidence["retention"]["deleted"] == []


def test_db_free_injected_factory_active_slurm_status_sync_blocks_without_factory_call(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    factory_calls: list[str] = []
    active_state = {
        "pipeline_status": "running",
        "pipeline_jobs": [
            {
                "job_id": "job_forcing",
                "run_id": "fcst_gfs_2026052106_model_a",
                "status": "running",
                "stage": "forcing",
                "slurm_job_id": "7777",
            }
        ],
    }
    active_jobs = [
        {
            "job_id": "job_forcing",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": "running",
            "stage": "forcing",
            "slurm_job_id": "7777",
        }
    ]

    def _factory(source_id: str) -> FakeProductionOrchestrator:
        factory_calls.append(source_id)
        return FakeProductionOrchestrator()

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(dry_run=False, now=_dt("2026-05-21T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=CandidateAndActiveRepository(active_state, active_jobs),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=_factory,
    )

    result = scheduler.run_once()

    assert factory_calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "db_free_journal_write_blocked"
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_slurm_status_sync_deferred"
    assert result.evidence["slurm_status_sync_proof"]["status"] == "preflight_blocked"
    assert result.evidence["slurm_status_sync_proof"]["sync_called"] is False
    assert result.evidence["model_run_evidence"][0]["error_code"] == "DB_FREE_FILE_JOURNAL_WRITE_NOT_IMPLEMENTED"
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_injected_factory_cancel_active_slurm_blocks_without_factory_call(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    factory_calls: list[str] = []
    active_jobs = [
        {
            "job_id": "job_forcing",
            "run_id": "fcst_gfs_2026052106_model_a",
            "status": "running",
            "stage": "forcing",
            "slurm_job_id": "7777",
        }
    ]

    def _factory(source_id: str) -> FakeProductionOrchestrator:
        factory_calls.append(source_id)
        return FakeProductionOrchestrator()

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            dry_run=False,
            now=_dt("2026-05-21T12:00:00Z"),
            cancel_active_slurm=True,
        ),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeSlurmActiveRepository(active_jobs=active_jobs),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=_factory,
    )

    result = scheduler.run_once()

    assert factory_calls == []
    assert result.status == "preflight_blocked"
    assert result.evidence["skipped_candidates"][0]["reason"] == "cancel_requested_active_slurm"
    assert result.evidence["slurm_cancellation_proof"]["status"] == "preflight_blocked"
    assert result.evidence["slurm_cancellation_proof"]["block_reason"] == (
        "db_free_file_journal_write_not_implemented"
    )
    assert result.evidence["slurm_cancellation_proof"]["cancel_called"] is False
    assert result.evidence["slurm_cancellation_evidence"][0]["error_code"] == (
        "DB_FREE_FILE_JOURNAL_WRITE_NOT_IMPLEMENTED"
    )
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_from_env_raw_ready_canonical_zero_submits_convert_without_download_source_cycle(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        products=[],
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    raw_fixture = _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=fixture["policy_identity"],
                source_object_identity=fixture["source_object_identity"],
            )
        },
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = _RealProductionScheduler(
        ProductionSchedulerConfig(
            now=_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
            sources=("gfs",),
            allowed_cycle_hours_utc=(0, 12),
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["blocked_candidates"] == []
    assert orchestrator.calls
    state_evidence = result.evidence["candidates"][0]["state_evidence"]
    assert state_evidence["restart_stage"] == "convert"
    assert state_evidence["nfs_raw_manifest"]["status"] == "ready"
    assert state_evidence["nfs_raw_manifest"]["manifest_uri"] == "[object-uri]"
    assert state_evidence["nfs_raw_manifest"]["object_store_root"] == "[local-path]"
    assert state_evidence["nfs_raw_manifest"]["manifest_path"] == "[local-path]"
    assert state_evidence["raw_manifest_reuse"]["manifest_uri"] == "[object-uri]"
    assert state_evidence["canonical_readiness"]["candidate_row_count"] == 0
    submitted_basin = orchestrator.calls[0]["basins"][0]
    assert submitted_basin["state_evidence"]["restart_stage"] == "convert"
    assert submitted_basin["orchestration_run_id"] == "cycle_gfs_2026052112_convert_model_a"
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is True
    rendered_submission = json.dumps(
        {"evidence": result.evidence},
        sort_keys=True,
        default=str,
    )
    assert "download_source_cycle" not in rendered_submission
    assert str(roots["object_store_root"]) not in rendered_submission
    assert str(raw_fixture["manifest_path"]) not in rendered_submission


def test_db_free_from_env_raw_missing_blocks_canonical_zero_without_submission(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        products=[],
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=fixture["policy_identity"],
                source_object_identity=fixture["source_object_identity"],
            )
        },
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = _RealProductionScheduler(
        ProductionSchedulerConfig(
            now=_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
            sources=("gfs",),
            allowed_cycle_hours_utc=(0, 12),
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "planned"
    assert orchestrator.calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["blocked_candidates"][0]["reason"] == "nfs_raw_manifest_manifest_not_found"
    assert "download_source_cycle" not in json.dumps(result.evidence, sort_keys=True)


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("bad_json", "nfs_raw_manifest_manifest_invalid_json"),
        ("source_mismatch", "nfs_raw_manifest_manifest_source_mismatch"),
        ("cycle_mismatch", "nfs_raw_manifest_manifest_cycle_time_mismatch"),
        ("uri_mismatch", "nfs_raw_manifest_manifest_uri_mismatch"),
        ("entries_missing", "nfs_raw_manifest_manifest_entries_missing"),
        ("local_key_invalid", "nfs_raw_manifest_manifest_entry_local_key_invalid"),
        ("raw_file_missing", "nfs_raw_manifest_raw_files_missing"),
    ],
)
def test_db_free_from_env_raw_invalid_blocks_without_submission(
    monkeypatch: Any,
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        products=[],
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    raw_entries: Sequence[Mapping[str, Any]] | None = None
    if case_name == "entries_missing":
        raw_entries = []
    elif case_name == "local_key_invalid":
        raw_entries = [{"local_key": "../escape.grib2", "forecast_hour": 0}]
    raw_fixture = _write_db_free_raw_manifest_fixture(
        roots,
        cycle_time=cycle_time,
        manifest_source_id="ifs" if case_name == "source_mismatch" else None,
        manifest_cycle_time="2026-05-21T00:00:00Z" if case_name == "cycle_mismatch" else None,
        manifest_uri="s3://nhms/raw/gfs/2026052100/manifest.json" if case_name == "uri_mismatch" else None,
        entries=raw_entries,
        write_raw_files=case_name not in {"local_key_invalid", "raw_file_missing"},
    )
    if case_name == "bad_json":
        raw_fixture["manifest_path"].write_text("{", encoding="utf-8")
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=fixture["policy_identity"],
                source_object_identity=fixture["source_object_identity"],
            )
        },
    )
    orchestrator = FakeProductionOrchestrator()
    scheduler = _RealProductionScheduler(
        ProductionSchedulerConfig(
            now=_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
            sources=("gfs",),
            allowed_cycle_hours_utc=(0, 12),
        ),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert orchestrator.calls == []
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["blocked_candidates"][0]["reason"] == expected_reason
    assert "download_source_cycle" not in rendered
    assert str(roots["object_store_root"]) not in rendered
    assert str(raw_fixture["manifest_path"]) not in rendered


def test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from tests.test_orchestration_chain import ImmediateTerminalSlurmClient

    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        products=[],
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [("2026-05-21T12:00:00Z", True)],
                policy_identity=fixture["policy_identity"],
                source_object_identity=fixture["source_object_identity"],
            )
        },
    )
    fake_slurm = ImmediateTerminalSlurmClient()

    def fail_db_factory(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("DB-free fake Slurm submission must not construct DB-backed state")

    monkeypatch.setattr("services.orchestrator.chain.HttpSlurmGatewayClient", lambda _url: fake_slurm)
    monkeypatch.setattr(
        "services.orchestrator.scheduler._slurm_preflight",
        lambda _config: {"status": "ready", "enabled": True, "blockers": [], "checks": {}},
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._orchestrator_repository_from_env",
        lambda: pytest.fail("DB-free fake Slurm submission must not construct psycopg orchestrator repository"),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._retry_service_from_env",
        lambda: pytest.fail("DB-free fake Slurm submission must not construct DB retry service"),
    )
    monkeypatch.setattr(
        "services.orchestrator.chain.PsycopgOrchestratorRepository.from_env",
        staticmethod(fail_db_factory),
    )
    monkeypatch.setattr(
        "services.orchestrator.chain_repository.PsycopgOrchestratorRepository.from_env",
        staticmethod(fail_db_factory),
    )
    monkeypatch.setattr("services.orchestrator.persistence.PipelineStore", fail_db_factory)
    monkeypatch.setattr(
        scheduler_module.StateManager,
        "from_env",
        staticmethod(lambda: pytest.fail("DB-free fake Slurm submission must not construct state manager")),
    )
    config = ProductionSchedulerConfig(
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
    )

    result = _RealProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)
    journal_repository = scheduler_module.FileOrchestrationJournalRepository(paths["NHMS_SCHEDULER_JOURNAL_ROOT"])
    jobs = journal_repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    state = journal_repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        run_id="fcst_gfs_2026052112_model_a",
        forcing_version_id="forc_gfs_2026052112_model_a",
        candidate_id="candidate_a",
    )

    assert result.status in {"submitted", "submitted_partial"}
    assert result.evidence["execution_boundary"] == "slurm_gateway_orchestration"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is True
    assert fake_slurm.submissions
    assert jobs
    assert state is not None
    assert state["pipeline_jobs"]
    assert any(event["event_type"] == "status_change" for event in state["pipeline_events"])
    assert "download_source_cycle" not in rendered
    assert str(roots["object_store_root"]) not in rendered


def test_db_free_required_implies_strict_runtime_root_preflight(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path)
    monkeypatch.delenv("NHMS_SCHEDULER_REQUIRE_ROOTS", raising=False)

    config = ProductionSchedulerConfig()
    runtime_preflight = scheduler_module._scheduler_runtime_root_preflight(config)
    lock_preflight = scheduler_module._scheduler_lock_evidence_root_preflight(config)

    assert config.require_runtime_roots is True
    assert runtime_preflight["required"] is True
    assert runtime_preflight["status"] == "ready"
    assert "service_role" in runtime_preflight["checks"]
    assert lock_preflight["required"] is True
    assert lock_preflight["status"] == "ready"


@pytest.mark.parametrize(
    ("root_env", "check_field"),
    [
        ("OBJECT_STORE_ROOT", "object_store_root"),
        ("NHMS_PUBLISHED_ARTIFACT_ROOT", "published_artifact_root"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "runtime_root"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "temp_root"),
    ],
)
def test_db_free_runtime_root_preflight_masks_local_paths_when_blocked(
    monkeypatch: Any,
    tmp_path: Path,
    root_env: str,
    check_field: str,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    outside_root = tmp_path / f"outside-{check_field}-secret-token"
    outside_root.mkdir()
    monkeypatch.setenv(root_env, str(outside_root))

    config = ProductionSchedulerConfig()
    runtime_preflight = scheduler_module._scheduler_runtime_root_preflight(config)
    resolved_roots = scheduler_module._scheduler_resolved_runtime_roots(config)
    rendered = json.dumps(
        {"runtime_preflight": runtime_preflight, "resolved_roots": resolved_roots},
        sort_keys=True,
    )

    assert runtime_preflight["status"] == "blocked"
    assert runtime_preflight["checks"][check_field]["path"] == "[local-path]"
    blocker = next(
        blocker
        for blocker in runtime_preflight["blockers"]
        if blocker["field"] == check_field
    )
    assert blocker["path"] == "[local-path]"
    assert runtime_preflight["checks"]["allowed_roots_policy"]["allowed_roots"] == [
        "[local-path]"
        for _root in runtime_preflight["allowed_roots"]
    ]
    assert set(runtime_preflight["allowed_roots"]) == {"[local-path]"}
    for item in resolved_roots.values():
        if item["configured"]:
            assert item["path"] == "[local-path]"
    for root in roots.values():
        assert str(root) not in rendered
    assert f"outside-{check_field}-secret-token" not in rendered
    assert str(outside_root) not in rendered


@pytest.mark.parametrize(
    ("root_env", "check_field"),
    [
        ("WORKSPACE_ROOT", "workspace_root"),
        ("OBJECT_STORE_ROOT", "object_store_root"),
        ("NHMS_PUBLISHED_ARTIFACT_ROOT", "published_artifact_root"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "runtime_root"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "temp_root"),
        ("NHMS_SCHEDULER_LOCK_ROOT", "lock_root"),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", "evidence_root"),
    ],
)
def test_db_free_runtime_root_preflight_blocks_raw_traversal_inside_allowed_root(
    monkeypatch: Any,
    tmp_path: Path,
    root_env: str,
    check_field: str,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    resolved_root = roots["workspace_root"] / f"safe-{check_field}"
    resolved_root.mkdir()
    raw_root = roots["workspace_root"] / "nested" / ".." / f"safe-{check_field}"
    monkeypatch.setenv(root_env, str(raw_root))
    if root_env == "WORKSPACE_ROOT":
        monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(raw_root / "locks"))
        monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(raw_root / "evidence"))

    config = ProductionSchedulerConfig()
    runtime_preflight = scheduler_module._scheduler_runtime_root_preflight(config)
    rendered = json.dumps(runtime_preflight, sort_keys=True)

    assert runtime_preflight["status"] == "blocked"
    root_check = runtime_preflight["checks"][check_field]
    assert root_check["path"] == "[local-path]"
    blocker = next(
        blocker
        for blocker in runtime_preflight["blockers"]
        if blocker["field"] == check_field
    )
    assert blocker["reason"] == "unsafe_path"
    assert blocker["path"] == "[local-path]"
    assert ".." not in rendered
    assert str(raw_root) not in rendered
    assert f"safe-{check_field}" not in rendered


@pytest.mark.parametrize(
    ("root_env", "check_field"),
    [
        ("OBJECT_STORE_ROOT", "object_store_root"),
        ("NHMS_PUBLISHED_ARTIFACT_ROOT", "published_artifact_root"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "runtime_root"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "temp_root"),
    ],
)
def test_db_free_runtime_root_run_once_blocks_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
    root_env: str,
    check_field: str,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    outside_root = tmp_path / f"outside-{check_field}-secret-token"
    outside_root.mkdir()
    monkeypatch.setenv(root_env, str(outside_root))
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", _unexpected_registry)
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", _unexpected_adapters)

    result = ProductionScheduler.from_env(ProductionSchedulerConfig()).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert result.evidence["lock"]["acquired"] is False
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["reason"] == "scheduler_root_preflight_blocked"
    assert result.evidence["root_preflight"]["status"] == "blocked"
    blocker = next(
        blocker
        for blocker in result.evidence["root_preflight"]["blockers"]
        if blocker["field"] == check_field
    )
    assert blocker["path"] == "[local-path]"
    assert result.evidence["root_preflight"]["checks"][check_field]["path"] == "[local-path]"
    assert result.evidence["model_discovery"] == scheduler_module._empty_model_discovery()
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert "outside-" not in rendered
    assert "secret-token" not in rendered
    assert str(outside_root) not in rendered
    for root in roots.values():
        assert str(root) not in rendered


def test_db_free_unsafe_workspace_root_constructs_and_blocks_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    loop = tmp_path / "workspace-secret-token-loop"
    loop.symlink_to(loop)
    unsafe_workspace = loop / "child"
    monkeypatch.setenv("WORKSPACE_ROOT", str(unsafe_workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(unsafe_workspace / "locks"))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(unsafe_workspace / "evidence"))
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    config = ProductionSchedulerConfig()
    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "preflight_blocked"
    assert result.artifact_path is None
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["reason"] == "scheduler_root_preflight_blocked"
    assert result.evidence["root_preflight"]["status"] == "blocked"
    assert "workspace-secret-token-loop" not in rendered
    assert str(unsafe_workspace) not in rendered
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_unknown_user_workspace_root_constructs_and_blocks_redacted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    unknown_workspace = "~nhms_missing_user/workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", unknown_workspace)
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", f"{unknown_workspace}/locks")
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", f"{unknown_workspace}/evidence")
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    config = ProductionSchedulerConfig()
    runtime_preflight = scheduler_module._scheduler_runtime_root_preflight(config)
    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(
        {"runtime_preflight": runtime_preflight, "evidence": result.evidence},
        sort_keys=True,
    )

    assert runtime_preflight["status"] == "blocked"
    assert runtime_preflight["checks"]["workspace_root"]["path"] == "[local-path]"
    assert result.status == "preflight_blocked"
    assert result.evidence["root_preflight"]["status"] == "blocked"
    assert result.evidence["root_preflight"]["checks"]["workspace_root"]["path"] == "[local-path]"
    assert result.evidence["lock"]["reason"] == "scheduler_root_preflight_blocked"
    assert "nhms_missing_user" not in rendered
    assert unknown_workspace not in rendered
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_unknown_user_required_path_blocks_redacted_before_lock(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    unknown_path = "~nhms_missing_user/registry-manifest.json"
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", unknown_path)
    monkeypatch.setattr("services.orchestrator.scheduler.FileSchedulerLease.acquire", _unexpected_lock_acquire)

    config = ProductionSchedulerConfig()
    db_free_preflight = config.db_free_runtime_preflight()
    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(
        {"db_free_preflight": db_free_preflight, "evidence": result.evidence},
        sort_keys=True,
    )

    assert db_free_preflight["status"] == "blocked"
    assert db_free_preflight["checks"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]["path"] == "[local-path]"
    assert result.status == "preflight_blocked"
    assert result.evidence["lock"]["reason"] == "db_free_runtime_preflight_blocked"
    assert result.evidence["db_free_runtime"]["checks"]["NHMS_SCHEDULER_REGISTRY_MANIFEST"]["path"] == "[local-path]"
    blocker = next(
        blocker
        for blocker in result.evidence["db_free_runtime"]["blockers"]
        if blocker["field"] == "NHMS_SCHEDULER_REGISTRY_MANIFEST"
    )
    assert blocker["path"] == "[local-path]"
    assert "nhms_missing_user" not in rendered
    assert unknown_path not in rendered
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()


def test_db_free_file_lock_contention_is_bounded_and_no_submit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig()
    held_lock = FileSchedulerLease(Path(config.lock_path), ttl_seconds=60, workspace_root=Path(config.workspace_root))
    acquired = held_lock.acquire(pass_id="already-running", started_at=_dt("2026-05-21T12:00:00Z"))
    assert acquired["acquired"] is True

    try:
        result = ProductionScheduler.from_env(config).run_once()
    finally:
        held_lock.release(pass_id="already-running")

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["existing_lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["contention"] is True
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    rendered = json.dumps(result.evidence, sort_keys=True)
    assert "db-free-local-root" not in rendered
    assert str(config.lock_path) not in rendered


def test_db_free_file_lock_contention_masks_raw_existing_lock_payload(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig()
    lock_path = Path(config.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = f"{config.lock_path}:unsafe-secret-token"
    lock_path.write_text(json.dumps(raw_payload), encoding="utf-8")

    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["existing_lock"]["raw"] == "[lock-payload]"
    assert raw_payload not in rendered
    assert "unsafe-secret-token" not in rendered
    assert str(config.lock_path) not in rendered


def test_db_free_file_lock_contention_collapses_unknown_mapping_payload(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig()
    lock_path = Path(config.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "path": "/tmp/node22-secret-token",
                "note": "postgresql://user:pass@db.internal.example:55433/nhms",
            }
        ),
        encoding="utf-8",
    )

    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["lock_type"] == "file"
    assert result.evidence["lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["existing_lock"] == {"raw": "[lock-payload]"}
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert "node22-secret-token" not in rendered
    assert "postgresql" not in rendered.lower()
    assert "db.internal.example" not in rendered
    assert "55433" not in rendered
    assert str(config.lock_path) not in rendered


def test_db_free_file_lock_contention_handles_deep_existing_payload_without_crash(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    config = ProductionSchedulerConfig()
    lock_path = Path(config.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = '"deep-secret-token"'
    for _index in range(1200):
        payload = '{"nested":' + payload + "}"
    assert len(payload.encode("utf-8")) < MAX_LOCK_PAYLOAD_BYTES
    lock_path.write_text(payload, encoding="utf-8")

    result = ProductionScheduler.from_env(config).run_once()
    rendered = json.dumps(result.evidence, sort_keys=True)

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["lock_path"] == "[local-path]"
    assert result.evidence["lock"]["existing_lock"] in ({"raw": None}, {"raw": "[lock-payload]"})
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["no_mutation_proof"] == _expected_no_mutation_proof()
    assert "deep-secret-token" not in rendered
    assert str(config.lock_path) not in rendered


def test_db_free_slurm_preflight_masks_storage_root_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    outside_root = tmp_path / "outside-slurm-secret-token"
    outside_root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(outside_root))
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ENABLED", "true")

    config = ProductionSchedulerConfig()
    preflight = scheduler_module._slurm_preflight(config)
    rendered = json.dumps(preflight, sort_keys=True)

    assert preflight["status"] == "blocked"
    database_check = preflight["checks"]["database"]
    assert database_check["required"] is False
    assert database_check["compute_node_reachable"] == "not_required"
    assert "SLURM_PREFLIGHT_DATABASE_URL_MISSING" not in {blocker["code"] for blocker in preflight["blockers"]}
    assert preflight["checks"]["storage_roots"]["object_store_root"]["path"] == "[local-path]"
    blocker = next(
        blocker
        for blocker in preflight["blockers"]
        if blocker["field"] == "object_store_root"
    )
    assert blocker["path"] == "[local-path]"
    assert set(preflight["checks"]["allowed_roots"]) == {"[local-path]"}
    assert "outside-slurm-secret-token" not in rendered
    assert str(outside_root) not in rendered
    for root in roots.values():
        assert str(root) not in rendered


def test_db_free_slurm_storage_root_check_masks_symlink_loop_path(tmp_path: Path) -> None:
    loop = tmp_path / "slurm-secret-token-loop"
    loop.symlink_to(loop)

    check, blocker = scheduler_module._storage_root_check(
        "object_store_root",
        loop,
        (tmp_path,),
        evidence_safe_paths=True,
    )
    rendered = json.dumps({"check": check, "blocker": blocker}, sort_keys=True)

    assert check["path"] == "[local-path]"
    assert blocker is not None
    assert blocker["code"] == "SLURM_PREFLIGHT_OBJECT_STORE_ROOT_UNSAFE_PATH"
    assert blocker["path"] == "[local-path]"
    assert "slurm-secret-token-loop" not in rendered
    assert str(loop) not in rendered


def test_db_free_slurm_preflight_masks_env_and_grib_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots, _paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "approved")
    log_root = roots["workspace_root"] / "logs"
    log_root.mkdir()
    grib_root = tmp_path / "nfs" / "unsafe-secret-token" / "grib"
    config = ProductionSchedulerConfig(
        slurm_execution_enabled=True,
        log_root=log_root,
        slurm_env={
            "NHMS_PROFILE": str(tmp_path / "profiles" / "unsafe-secret-token"),
            "NHMS_DB_HINT": "postgresql://db.internal.example:55433/nhms",
            "NHMS_GRIB_ENV_ROOT": str(grib_root),
        },
    )

    preflight = scheduler_module._slurm_preflight(config)
    rendered = json.dumps(preflight, sort_keys=True)

    assert preflight["status"] == "blocked"
    assert preflight["checks"]["environment"]["sanitized"]["NHMS_PROFILE"] == "[redacted]"
    assert preflight["checks"]["environment"]["sanitized"]["NHMS_DB_HINT"] == "[db-like]"
    assert preflight["checks"]["environment"]["sanitized"]["NHMS_GRIB_ENV_ROOT"] == "[redacted]"
    assert preflight["checks"]["grib_env"]["root"] == "[redacted]"
    assert preflight["checks"]["grib_env"]["bin_present"] is False
    assert preflight["checks"]["grib_env"]["lib_present"] is False
    grib_blocker = next(
        blocker for blocker in preflight["blockers"] if blocker["code"] == "GRIB_ENV_ROOT_INVALID"
    )
    assert grib_blocker["root"] == "[redacted]"
    assert grib_blocker["bin_present"] is False
    assert grib_blocker["lib_present"] is False
    assert "unsafe-secret-token" not in rendered
    assert "postgresql" not in rendered.lower()
    assert "db.internal.example" not in rendered
    assert "55433" not in rendered
    assert str(grib_root) not in rendered


# --- Issue #257 / M23-6: scheduler SHUD executable pre-submit preflight -------


class _AssertNoSubmitOrchestrator:
    """Orchestrator that fails the test if it is ever invoked.

    Does not allocate a LocalObjectStore, so it is safe to construct on any
    platform (unlike FakeProductionOrchestrator).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def orchestrate_cycle(self, source: str, cycle_time: datetime, basins: list[dict[str, Any]]) -> Any:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        raise AssertionError("orchestrator must not run when SHUD preflight blocks submission")


def _slurm_shud_scheduler(tmp_path: Path, *, shud_executable: str) -> tuple[Any, Any]:
    roots = _slurm_roots(tmp_path)
    orchestrator = _AssertNoSubmitOrchestrator()
    config = _config(
        roots["workspace_root"],
        now=_dt("2026-05-21T12:00:00Z"),
        dry_run=False,
        slurm_execution_enabled=True,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_env={"SHUD_EXECUTABLE": shud_executable},
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )
    return scheduler, orchestrator


@pytest.mark.parametrize(
    ("shud_executable", "expected_code"),
    [
        ("/bin/true", "SHUD_EXECUTABLE_STUB_REJECTED"),
        ("/bin/false", "SHUD_EXECUTABLE_STUB_REJECTED"),
        ("", "SHUD_EXECUTABLE_NOT_CONFIGURED"),
        ("/nonexistent/shud_omp", "SHUD_EXECUTABLE_MISSING"),
    ],
)
def test_scheduler_slurm_preflight_blocks_stub_or_missing_shud_before_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shud_executable: str,
    expected_code: str,
) -> None:
    # The empty case must also clear the ambient SHUD_EXECUTABLE env so the
    # scheduler cannot fall back to a valid one set by the autouse fixture.
    if shud_executable == "":
        monkeypatch.delenv("SHUD_EXECUTABLE", raising=False)
    scheduler, orchestrator = _slurm_shud_scheduler(tmp_path, shud_executable=shud_executable)

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "slurm_preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    # No Slurm submission, no active pipeline job, no hydro success state.
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False
    assert orchestrator.calls == []
    blocker_codes = {b["code"] for b in result.evidence["slurm_preflight"]["blockers"]}
    assert expected_code in blocker_codes
    model_run = result.evidence["model_run_evidence"][0]
    assert model_run["status"] == "preflight_blocked"
    assert model_run["submitted"] is False
    assert "secret" not in json.dumps(result.evidence)


def test_scheduler_slurm_preflight_does_not_leak_library_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "shud_omp"
    binary.write_text('#!/bin/sh\necho "SHUD"\n', encoding="utf-8")
    binary.chmod(0o755)

    import packages.common.shud_preflight as preflight

    monkeypatch.setattr(preflight, "_missing_shared_libraries", lambda _resolved: ["libqhh-token.so.2"])
    monkeypatch.setattr(preflight, "_version_identity_signal", lambda _resolved: "present")

    scheduler, orchestrator = _slurm_shud_scheduler(tmp_path, shud_executable=str(binary))

    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert orchestrator.calls == []
    blockers = result.evidence["slurm_preflight"]["blockers"]
    library_blockers = [b for b in blockers if b["code"] == "SHUD_EXECUTABLE_LIBRARY_MISSING"]
    assert library_blockers
    assert library_blockers[0]["library"] == "libqhh-token.so.2"
    assert "password" not in json.dumps(result.evidence)


# --- M23-7 (#258): node-22 Slurm gateway preflight ---------------------------


def _gateway_config(tmp_path: Path, **kwargs: Any) -> ProductionSchedulerConfig:
    defaults = {
        "slurm_execution_enabled": True,
        "database_url": "postgresql://nhms:secret@db.prod.example/nhms",
    }
    defaults.update(kwargs)
    return _config(tmp_path, **defaults)


def _healthy_gateway_probe(_config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "mode": "real",
        "backend": "slurm",
        "version": "24.05",
        "healthy": True,
        "submit_capable": True,
        "accounting_available": True,
    }


def test_slurm_gateway_check_healthy_records_mode_without_credentials(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, slurm_gateway_url="https://user:pass@gw-node22.internal:8000")

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=_healthy_gateway_probe)

    assert blockers == []
    assert checks["mode"] == "real"
    assert checks["endpoint"]["host"] == "gw-node22.internal"
    # Credentials in the URL must never reach evidence.
    serialized = json.dumps(checks)
    assert "pass" not in serialized
    assert "user" not in serialized


def test_slurm_gateway_check_unavailable_blocks(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8000")

    def unhealthy(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {"mode": "real", "healthy": False, "reason": "sinfo --version failed"}

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=unhealthy)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_UNAVAILABLE" in codes
    assert checks["healthy"] is False


def test_slurm_gateway_check_self_reference_blocks_with_real_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler_module, "_slurm_gateway_backend", lambda: "real")
    config = _gateway_config(tmp_path, slurm_gateway_url="http://localhost:8000", service_port=8000)

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=_healthy_gateway_probe)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_SELF_REFERENCE" in codes
    # Self-reference is decisive: the probe must not also be consulted.
    assert checks["self_reference"] is True


def test_slurm_gateway_check_does_not_flag_colocated_mock_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # never-break: the mock co-located dev convention (localhost:8000) is allowed.
    monkeypatch.setattr(scheduler_module, "_slurm_gateway_backend", lambda: "mock")
    config = _gateway_config(tmp_path, slurm_gateway_url="http://localhost:8000", service_port=8000)

    _checks, blockers = scheduler_module._slurm_gateway_check(config, probe=_healthy_gateway_probe)

    assert blockers == []


def test_slurm_gateway_check_probe_exception_fails_safe(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8000")

    def boom(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        raise RuntimeError("slurm cli missing")

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=boom)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_UNAVAILABLE" in codes
    assert checks["healthy"] is False


def test_slurm_preflight_ready_with_healthy_gateway_adds_no_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _slurm_roots(tmp_path)
    monkeypatch.setattr(scheduler_module, "_default_gateway_probe", _healthy_gateway_probe)
    config = _gateway_config(
        roots["workspace_root"],
        slurm_gateway_url="http://gw-node22.internal:8000",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )

    preflight = scheduler_module._slurm_preflight(config)

    assert preflight["status"] == "ready"
    gateway_blockers = [b for b in preflight["blockers"] if b["code"].startswith("SLURM_GATEWAY")]
    assert gateway_blockers == []
    assert preflight["checks"]["gateway"]["healthy"] is True


def test_slurm_preflight_blocked_when_gateway_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _slurm_roots(tmp_path)

    def unhealthy(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {"mode": "real", "healthy": False, "reason": "gateway down"}

    monkeypatch.setattr(scheduler_module, "_default_gateway_probe", unhealthy)
    config = _gateway_config(
        roots["workspace_root"],
        slurm_gateway_url="http://gw-node22.internal:8000",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )

    preflight = scheduler_module._slurm_preflight(config)

    assert preflight["status"] == "blocked"
    codes = {b["code"] for b in preflight["blockers"]}
    assert "SLURM_GATEWAY_UNAVAILABLE" in codes


@pytest.mark.parametrize(
    "gateway_url",
    ["http://gw-node22.internal:8000", "gw-node22.internal:8000"],
)
def test_slurm_gateway_check_real_backend_remote_host_is_not_self_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_url: str,
) -> None:
    # never-break reverse: a real backend pointed at a genuine remote node-22
    # host (with or without scheme) must NOT be flagged as a self-reference,
    # even when the port matches the service's own listen port.
    monkeypatch.setattr(scheduler_module, "_slurm_gateway_backend", lambda: "real")
    config = _gateway_config(tmp_path, slurm_gateway_url=gateway_url, service_port=8000)

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=_healthy_gateway_probe)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_SELF_REFERENCE" not in codes
    assert checks["self_reference"] is False


def test_slurm_gateway_check_self_reference_blocks_schemeless_localhost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression lock for the schemeless host:port endpoint parse fix: a real
    # backend pointed at a bare ``localhost:8000`` (no ``http://``) is a true
    # self-reference and must be blocked. Before the fix _gateway_endpoint
    # mis-parsed ``localhost:`` as a scheme, returned host=None, and the guard
    # silently passed.
    monkeypatch.setattr(scheduler_module, "_slurm_gateway_backend", lambda: "real")
    config = _gateway_config(tmp_path, slurm_gateway_url="localhost:8000", service_port=8000)

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=_healthy_gateway_probe)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_SELF_REFERENCE" in codes
    assert checks["self_reference"] is True


# --- M24-4 §4.5 (#292): GRIB-env preflight fail-loud -------------------------


def _clear_grib_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ambient operator env must never bleed into these checks.
    monkeypatch.delenv("NHMS_GRIB_ENV_ROOT", raising=False)
    monkeypatch.delenv("NHMS_GRIB_SYSTEM_ECCODES", raising=False)


def test_grib_env_check_valid_root_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_grib_env(monkeypatch)
    root = tmp_path / "nhms-grib"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)
    config = _config(tmp_path, slurm_env={"NHMS_GRIB_ENV_ROOT": str(root)})

    checks, blockers = scheduler_module._slurm_grib_env_check(config)

    assert blockers == []
    assert checks["bin_present"] is True
    assert checks["lib_present"] is True


def test_grib_env_check_root_missing_bin_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_grib_env(monkeypatch)
    root = tmp_path / "nhms-grib"
    (root / "lib").mkdir(parents=True)  # bin/ absent
    config = _config(tmp_path, slurm_env={"NHMS_GRIB_ENV_ROOT": str(root)})

    _checks, blockers = scheduler_module._slurm_grib_env_check(config)

    codes = {b["code"] for b in blockers}
    assert "GRIB_ENV_ROOT_INVALID" in codes


def test_grib_env_check_root_missing_lib_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_grib_env(monkeypatch)
    root = tmp_path / "nhms-grib"
    (root / "bin").mkdir(parents=True)  # lib/ absent
    config = _config(tmp_path, slurm_env={"NHMS_GRIB_ENV_ROOT": str(root)})

    _checks, blockers = scheduler_module._slurm_grib_env_check(config)

    codes = {b["code"] for b in blockers}
    assert "GRIB_ENV_ROOT_INVALID" in codes


def test_grib_env_check_empty_root_with_system_eccodes_asserted_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_grib_env(monkeypatch)
    config = _config(
        tmp_path,
        slurm_env={"NHMS_GRIB_ENV_ROOT": "", "NHMS_GRIB_SYSTEM_ECCODES": "1"},
    )

    checks, blockers = scheduler_module._slurm_grib_env_check(config)

    assert blockers == []
    assert checks["system_eccodes_available"] is True


def test_grib_env_check_empty_root_without_assertion_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Default no-fence: empty root + nothing asserted -> no GRIB blocker.
    _clear_grib_env(monkeypatch)
    config = _config(tmp_path, slurm_env={})

    checks, blockers = scheduler_module._slurm_grib_env_check(config)

    codes = {b["code"] for b in blockers}
    assert "GRIB_ENV_UNAVAILABLE" not in codes
    assert "GRIB_ENV_ROOT_INVALID" not in codes
    assert checks["system_eccodes_available"] is True


def test_grib_env_check_empty_root_nodes_lacking_eccodes_asserted_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opt-in fail-loud: operator asserts nodes lack eccodes -> empty root blocks.
    _clear_grib_env(monkeypatch)
    config = _config(
        tmp_path,
        slurm_env={"NHMS_GRIB_ENV_ROOT": "", "NHMS_GRIB_SYSTEM_ECCODES": "false"},
    )

    checks, blockers = scheduler_module._slurm_grib_env_check(config)

    codes = {b["code"] for b in blockers}
    assert "GRIB_ENV_UNAVAILABLE" in codes
    assert checks["system_eccodes_available"] is False


def test_grib_env_check_probe_exception_fails_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_grib_env(monkeypatch)
    config = _config(tmp_path, slurm_env={})

    def boom(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        raise RuntimeError("cannot reach compute node")

    checks, blockers = scheduler_module._slurm_grib_env_check(config, probe=boom)

    codes = {b["code"] for b in blockers}
    assert "GRIB_ENV_UNAVAILABLE" in codes
    assert checks["system_eccodes_available"] is False


def test_slurm_preflight_surfaces_grib_blocker_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_grib_env(monkeypatch)
    roots = _slurm_roots(tmp_path)
    # Keep gateway healthy so the only blocker is the GRIB one.
    monkeypatch.setattr(scheduler_module, "_default_gateway_probe", _healthy_gateway_probe)
    config = _gateway_config(
        roots["workspace_root"],
        slurm_gateway_url="http://gw-node22.internal:8000",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        # Empty GRIB root + operator asserts nodes lack eccodes -> fail-loud.
        slurm_env={"NHMS_GRIB_ENV_ROOT": "", "NHMS_GRIB_SYSTEM_ECCODES": "false"},
    )

    preflight = scheduler_module._slurm_preflight(config)

    assert preflight["status"] == "blocked"
    codes = {b["code"] for b in preflight["blockers"]}
    assert "GRIB_ENV_UNAVAILABLE" in codes
    assert preflight["checks"]["grib_env"]["system_eccodes_available"] is False


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeHttpClient:
    """Records the GET URL so tests can assert the configured URL is probed."""

    last_url: str | None = None
    response: Any = None
    raise_error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        del args, kwargs

    def __enter__(self) -> _FakeHttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def get(self, url: str) -> Any:
        type(self).last_url = url
        if type(self).raise_error is not None:
            raise type(self).raise_error
        return type(self).response


def _healthy_health_payload() -> dict[str, Any]:
    executable = {"resolved": True, "executable": True, "detail": None}
    return {
        "backend": "slurm",
        "version": "24.05",
        "status": "healthy",
        "healthy": True,
        "binaries": {name: dict(executable) for name in ("sbatch", "squeue", "sacct", "scancel")},
    }


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Any = None,
    raise_error: Exception | None = None,
) -> type[_FakeHttpClient]:
    import httpx

    # HTTP probing is the real-gateway path; force backend=real so the probe
    # does not take the in-process mock branch.
    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "real")
    _FakeHttpClient.last_url = None
    _FakeHttpClient.response = response
    _FakeHttpClient.raise_error = raise_error
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    return _FakeHttpClient


def test_preflight_http_probes_configured_gateway_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default probe must HTTP-GET ${SLURM_GATEWAY_URL}/api/v1/slurm/health,
    # NOT an in-process create_gateway().health(). Guard create_gateway so any
    # in-process call would fail the test.
    import services.slurm_gateway.gateway as gateway_module

    def _boom(*_args: Any, **_kwargs: Any):
        raise AssertionError("preflight must not use in-process create_gateway().health()")

    monkeypatch.setattr(gateway_module, "create_gateway", _boom)
    client = _install_fake_httpx(monkeypatch, response=_FakeHttpResponse(200, _healthy_health_payload()))

    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8081")
    result = scheduler_module._default_gateway_probe(config)

    assert client.last_url == "http://gw-node22.internal:8081/api/v1/slurm/health"
    assert result["healthy"] is True
    assert result["submit_capable"] is True
    assert result["accounting_available"] is True


def test_preflight_http_probe_missing_binary_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _healthy_health_payload()
    payload["binaries"]["sacct"] = {"resolved": True, "executable": False, "detail": "no sacct"}
    _install_fake_httpx(monkeypatch, response=_FakeHttpResponse(200, payload))

    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8081")
    result = scheduler_module._default_gateway_probe(config)

    assert result["healthy"] is False
    assert "sacct" in (result["reason"] or "")


def test_preflight_http_probe_unreachable_fails_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    _install_fake_httpx(monkeypatch, raise_error=httpx.ConnectError("connection refused"))

    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8081")
    result = scheduler_module._default_gateway_probe(config)

    assert result["healthy"] is False
    assert result["submit_capable"] is False


def test_preflight_http_probe_non_2xx_fails_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_httpx(monkeypatch, response=_FakeHttpResponse(503, {"detail": "unavailable"}))

    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8081")
    result = scheduler_module._default_gateway_probe(config)

    assert result["healthy"] is False
    assert "503" in (result["reason"] or "")


def test_preflight_http_probe_legacy_status_only_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Backward compat: a gateway still on the old shape (only ``status``) is
    # accepted via the legacy fallback.
    _install_fake_httpx(monkeypatch, response=_FakeHttpResponse(200, {"status": "ok", "backend": "mock"}))

    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8081")
    result = scheduler_module._default_gateway_probe(config)

    assert result["healthy"] is True


def test_unreachable_or_unhealthy_gateway_blocks_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unreachable gateway -> pre-mutation BLOCKED via _slurm_gateway_check,
    # and the preflight aggregate is blocked (no submission proceeds).
    import httpx

    _install_fake_httpx(monkeypatch, raise_error=httpx.ConnectError("refused"))
    roots = _slurm_roots(tmp_path)
    config = _gateway_config(
        roots["workspace_root"],
        slurm_gateway_url="http://gw-node22.internal:8081",
        object_store_root=roots["object_store_root"],
        log_root=roots["log_root"],
        runtime_root=roots["runtime_root"],
        allowed_storage_roots=(tmp_path,),
        slurm_job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
    )

    preflight = scheduler_module._slurm_preflight(config)

    assert preflight["status"] == "blocked"
    codes = {b["code"] for b in preflight["blockers"]}
    assert "SLURM_GATEWAY_UNAVAILABLE" in codes
    assert preflight["checks"]["gateway"]["healthy"] is False


def test_slurm_gateway_check_healthy_but_not_submit_capable_blocks(tmp_path: Path) -> None:
    # A gateway that is healthy and has accounting but cannot submit must still
    # be blocked: submit capability is required before any submission.
    config = _gateway_config(tmp_path, slurm_gateway_url="http://gw-node22.internal:8000")

    def healthy_no_submit(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {
            "mode": "real",
            "healthy": True,
            "submit_capable": False,
            "accounting_available": True,
        }

    checks, blockers = scheduler_module._slurm_gateway_check(config, probe=healthy_no_submit)

    codes = {b["code"] for b in blockers}
    assert "SLURM_GATEWAY_UNAVAILABLE" in codes
    assert checks["healthy"] is True
    assert checks["submit_capable"] is False


# --- M24 §3A: concurrent submit-and-return with durable reservation ----------


class _BarrierOrchestrator:
    """Orchestrator whose ``orchestrate_cycle`` blocks on a shared barrier.

    Two cohorts submitted concurrently both reach the barrier before either
    returns, deterministically proving the submits overlap (neither waits for
    the other's terminal state).
    """

    def __init__(self, barrier: Any) -> None:
        self._barrier = barrier
        self.object_store = None
        self.calls: list[str] = []

    def orchestrate_cycle(self, source: str, cycle_time: datetime, basins: list[dict[str, Any]]) -> PipelineResult:
        self.calls.append(source)
        # Block until BOTH cohorts have entered; if execution were sequential the
        # barrier would deadlock and the test would time out.
        self._barrier.wait(timeout=5.0)
        stages = tuple(
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=f"job_{stage.stage}",
                slurm_job_id=f"slurm_{stage.stage}",
                status="succeeded",
            )
            for stage in M3_STAGES
        )
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status="complete",
            stages=stages,
            candidate_outcomes=(),
        )


def _concurrency_candidate(source_id: str, model_id: str, basin_id: str) -> scheduler_module.SchedulerCandidate:
    return scheduler_module._candidate_for(
        discovery=CycleDiscovery(
            cycle_id=f"{source_id}_2026052106",
            source_id=source_id,
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        model=scheduler_module.RegisteredSchedulerModel(
            model_id=model_id,
            basin_id=basin_id,
            basin_version_id=f"{basin_id}_v1",
            river_network_version_id=f"{basin_id}_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri=f"s3://nhms/models/{model_id}/package/",
            shud_code_version="2.0",
            resource_profile={"output_uri": f"s3://nhms/out/{model_id}/"},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )


def _evidence_identity_order(evidence: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str, str]]:
    return [
        (
            str(item["source_id"]),
            str(item["candidate_id"]),
            str(item["model_id"]),
            str(item["run_id"]),
        )
        for item in evidence
    ]


def test_concurrent_candidates_submits_overlap(tmp_path: Path) -> None:
    import threading

    barrier = threading.Barrier(2)
    orchestrators: dict[str, _BarrierOrchestrator] = {}

    def _factory(source_id: str) -> _BarrierOrchestrator:
        orchestrators.setdefault(source_id, _BarrierOrchestrator(barrier))
        return orchestrators[source_id]

    config = _config(tmp_path, sources=("gfs", "IFS"), concurrent_submit_bound=4)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", []), "IFS": FakeAdapter("IFS", [])},
        orchestrator_factory=_factory,
    )

    candidates = [
        _concurrency_candidate("gfs", "model_a", "basin_a"),
        _concurrency_candidate("IFS", "model_b", "basin_b"),
    ]

    evidence = scheduler._execute_candidates(candidates)

    # Both cohorts ran (barrier did not deadlock) => submits overlapped.
    assert len(evidence) == 2
    assert _evidence_identity_order(evidence) == [
        (
            "gfs",
            "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "model_a",
            "fcst_gfs_2026052106_model_a",
        ),
        (
            "IFS",
            "IFS:2026-05-21T06:00:00Z:model_b:forecast_ifs_deterministic",
            "model_b",
            "fcst_ifs_2026052106_model_b",
        ),
    ]
    receipt = scheduler._last_submit_overlap_receipt
    assert receipt.overlapping is True
    receipt_dict = receipt.to_dict()
    assert receipt_dict["concurrent_submit_count"] == 2
    assert receipt_dict["overlapping"] is True
    # Two windows recorded, each with start/finish timestamps.
    assert len(receipt_dict["submissions"]) == 2
    for entry in receipt_dict["submissions"]:
        assert entry["submit_finished_at"] >= entry["submit_started_at"]


def test_concurrent_candidates_same_source_cycle_submits_basins_overlap(tmp_path: Path) -> None:
    import threading

    barrier = threading.Barrier(2)
    orchestrator = _BarrierOrchestrator(barrier)
    config = _config(tmp_path, sources=("gfs",), concurrent_submit_bound=2)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    candidates = [
        _concurrency_candidate("gfs", "model_a", "basin_a"),
        _concurrency_candidate("gfs", "model_b", "basin_b"),
    ]

    evidence = scheduler._execute_candidates(candidates)

    assert len(evidence) == 2
    assert _evidence_identity_order(evidence) == [
        (
            "gfs",
            "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "model_a",
            "fcst_gfs_2026052106_model_a",
        ),
        (
            "gfs",
            "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
            "model_b",
            "fcst_gfs_2026052106_model_b",
        ),
    ]
    assert orchestrator.calls == ["gfs", "gfs"]
    receipt = scheduler._last_submit_overlap_receipt
    assert receipt.overlapping is True
    assert receipt.to_dict()["concurrent_submit_count"] == 2


def test_concurrent_submit_bound_one_keeps_sequential_evidence_order(tmp_path: Path) -> None:
    import threading
    import time as _time

    active = 0
    peak = 0
    lock = threading.Lock()
    call_order: list[str] = []

    class _SequentialProofOrchestrator:
        def __init__(self) -> None:
            self.object_store = None

        def orchestrate_cycle(self, source: str, cycle_time: datetime, basins: list[dict[str, Any]]) -> PipelineResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                call_order.append(source)
            try:
                _time.sleep(0.01)
            finally:
                with lock:
                    active -= 1
            stages = tuple(
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=f"job_{source.lower()}_{stage.stage}",
                    slurm_job_id=f"slurm_{source.lower()}_{stage.stage}",
                    status="succeeded",
                )
                for stage in M3_STAGES
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=stages,
                candidate_outcomes=(),
            )

    config = _config(tmp_path, sources=("gfs", "IFS"), concurrent_submit_bound=1)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", []), "IFS": FakeAdapter("IFS", [])},
        orchestrator_factory=lambda _source_id: _SequentialProofOrchestrator(),
    )

    candidates = [
        _concurrency_candidate("gfs", "model_a", "basin_a"),
        _concurrency_candidate("IFS", "model_b", "basin_b"),
    ]

    evidence = scheduler._execute_candidates(candidates)

    assert peak == 1
    assert call_order == ["gfs", "IFS"]
    assert _evidence_identity_order(evidence) == [
        (
            "gfs",
            "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "model_a",
            "fcst_gfs_2026052106_model_a",
        ),
        (
            "IFS",
            "IFS:2026-05-21T06:00:00Z:model_b:forecast_ifs_deterministic",
            "model_b",
            "fcst_ifs_2026052106_model_b",
        ),
    ]
    for item in evidence:
        assert {"candidate_id", "source_id", "model_id", "run_id", "status", "submitted", "pipeline_run_id"} <= set(
            item
        )
        assert item["status"] == "complete"
        assert item["submitted"] is True
    receipt = scheduler._last_submit_overlap_receipt
    assert receipt.overlapping is False
    receipt_dict = receipt.to_dict()
    assert receipt_dict["concurrent_submit_count"] == 2
    assert receipt_dict["overlapping"] is False


def test_mixed_cohort_orchestrator_exception_keeps_sibling_cohort_evidence(tmp_path: Path) -> None:
    class _MixedOutcomeOrchestrator:
        def __init__(self, *, raise_on_submit: bool) -> None:
            self.raise_on_submit = raise_on_submit
            self.object_store = None
            self.calls: list[dict[str, Any]] = []

        def orchestrate_cycle(self, source: str, cycle_time: datetime, basins: list[dict[str, Any]]) -> PipelineResult:
            self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
            if self.raise_on_submit:
                raise RuntimeError("submit failed for mixed cohort")
            stages = tuple(
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=f"job_{source.lower()}_{stage.stage}",
                    slurm_job_id=f"slurm_{source.lower()}_{stage.stage}",
                    status="succeeded",
                )
                for stage in M3_STAGES
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=stages,
                candidate_outcomes=(),
            )

    orchestrators = {
        "gfs": _MixedOutcomeOrchestrator(raise_on_submit=True),
        "IFS": _MixedOutcomeOrchestrator(raise_on_submit=False),
    }
    config = _config(tmp_path, sources=("gfs", "IFS"), concurrent_submit_bound=2)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", []), "IFS": FakeAdapter("IFS", [])},
        orchestrator_factory=lambda source_id: orchestrators[source_id],
    )

    candidates = [
        _concurrency_candidate("gfs", "model_a", "basin_a"),
        _concurrency_candidate("IFS", "model_b", "basin_b"),
    ]

    evidence = scheduler._execute_candidates(candidates)

    assert [len(orchestrators[source].calls) for source in ("gfs", "IFS")] == [1, 1]
    assert _evidence_identity_order(evidence) == [
        (
            "gfs",
            "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "model_a",
            "fcst_gfs_2026052106_model_a",
        ),
        (
            "IFS",
            "IFS:2026-05-21T06:00:00Z:model_b:forecast_ifs_deterministic",
            "model_b",
            "fcst_ifs_2026052106_model_b",
        ),
    ]
    failed, submitted = evidence
    assert failed["status"] == "submission_failed"
    assert failed["submitted"] is False
    assert failed["slurm_submit_called"] == "unknown_after_attempt"
    assert failed["mutation_outcome"] == "unknown_after_attempt"
    assert failed["mutation_occurred"] == "unknown_after_attempt"
    assert submitted["status"] == "complete"
    assert submitted["submitted"] is True
    assert submitted["slurm_submit_called"] is True
    assert submitted["pipeline_run_id"] == "cycle_ifs_2026052106"


def test_scheduler_pass_startup_reconciles_reserved_unbound_jobs(tmp_path: Path) -> None:
    """An executing scheduler pass MUST, at startup (after lock + root preflight,
    before planning/submitting), run reserved-unbound reconcile so a submit-crash
    reservation is bound back to its real slurm_job_id by the idempotency comment
    — recovering, not duplicating, an already in-flight cohort (item 3 MAJOR).

    Counterfactual: remove the ``reconcile_reserved_unbound_jobs`` call from
    ``_run_restart_reconcile`` (or its invocation in the pass) and the reserved
    row is never bound: ``bound_calls`` stays empty and the evidence assertion
    on ``reserved_unbound`` goes red.
    """

    from services.orchestrator.reconcile import SacctRecord
    from services.orchestrator.reservation import slurm_comment_for

    key = "fcst_gfs_2026052106_model_a:forcing"

    class _ReservedUnboundJob:
        job_id = "job_reserved_crash"
        idempotency_key = key
        status = "reserved"
        slurm_job_id = None
        stage = "forcing"
        job_type = "produce_forcing_array"

    bound_calls: list[dict[str, Any]] = []

    class _FakeReconcileStore:
        def query_reserved_unbound_jobs(self) -> list[Any]:
            # Drain after the first reconcile so a re-query returns nothing,
            # mirroring the durable row transitioning out of reserved-unbound.
            if bound_calls:
                return []
            return [_ReservedUnboundJob()]

        def query_inflight_jobs(self) -> list[Any]:
            return []

        def bind_reservation(self, idem: str, *, slurm_job_id: str, status: str = "submitted") -> dict[str, Any]:
            record = {"idempotency_key": idem, "slurm_job_id": slurm_job_id, "status": status}
            bound_calls.append(record)
            return record

        def update_job_status(self, *args: Any, **kwargs: Any) -> None:
            pass

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="88123",
                raw_state="RUNNING",
                job_name="nhms_forcing",
                comment=slurm_comment_for(key),
            )
        return None

    scheduler = _RealProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        # No discoverable cycles: the pass still reaches the startup reconcile
        # hook (which runs before candidate discovery), so this isolates item 3.
        adapters={"gfs": FakeAdapter("gfs", [])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        reconcile_store=_FakeReconcileStore(),
        reconcile_comment_query=_comment_query,
        reconcile_sacct_query=lambda _slurm_job_id: None,
    )

    result = scheduler.run_once()

    # The crashed reservation was bound back by idempotency comment at pass start.
    assert result.status == "restart_reconciled"
    assert result.evidence["status"] == "restart_reconciled"
    assert result.evidence["execution_boundary"] == "restart_reconcile"
    assert bound_calls == [{"idempotency_key": key, "slurm_job_id": "88123", "status": "submitted"}]
    reconcile_evidence = result.evidence["restart_reconcile"]
    assert reconcile_evidence["status"] == "completed"
    reserved = reconcile_evidence["reserved_unbound"]
    assert reserved["count"] == 1
    assert reserved["outcomes"][0]["action"] == "bound"
    assert reserved["outcomes"][0]["slurm_job_id"] == "88123"
    assert reserved["outcomes"][0]["idempotency_key"] == key
    proof = result.evidence["restart_reconcile_proof"]
    assert proof["mutation_occurred"] is True
    assert proof["pipeline_status_writes"] is True
    assert proof["pipeline_event_writes"] is False
    no_mutation = result.evidence["no_mutation_proof"]
    assert no_mutation["pipeline_status_writes"] is True
    assert no_mutation["pipeline_event_writes"] is False
    assert no_mutation["restart_reconcile_writes"] is True


def test_db_free_restart_reconcile_uses_file_journal_repository(tmp_path: Path) -> None:
    from services.orchestrator.reconcile import SacctRecord
    from services.orchestrator.reservation import slurm_comment_for

    cycle_time = _dt("2026-05-21T06:00:00Z")
    key = "gfs:gfs_2026052106:basin_a:forecast"
    repository = scheduler_module.FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.reserve_pipeline_job(
        {
            "job_id": "job_db_free_reserved",
            "run_id": "fcst_gfs_2026052106_model_a",
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "job_type": "run_shud_forecast_array",
            "model_id": "model_a",
            "status": "reserved",
            "stage": "forecast",
            "idempotency_key": key,
            "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
        }
    )

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="3001",
                raw_state="RUNNING",
                job_name="nhms_forecast",
                comment=slurm_comment_for(key),
            )
        return None

    def _sacct_query(slurm_job_id: str) -> SacctRecord | None:
        if slurm_job_id == "3001":
            return SacctRecord(
                slurm_job_id="3001",
                raw_state="RUNNING",
                job_name="nhms_forecast",
                comment=slurm_comment_for(key),
            )
        return None

    scheduler = _RealProductionScheduler(
        _config(
            tmp_path,
            dry_run=False,
            scheduler_db_free_required=True,
            scheduler_journal_backend="file",
            scheduler_journal_root=tmp_path / "journal",
            database_url=None,
            database_url_configured=False,
        ),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [])},
        active_repository=repository,
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        reconcile_comment_query=_comment_query,
        reconcile_sacct_query=_sacct_query,
    )

    evidence = scheduler._run_restart_reconcile()
    job = repository.get_pipeline_job("job_db_free_reserved")

    assert evidence is not None
    assert evidence["status"] == "completed"
    assert evidence["reserved_unbound"]["outcomes"][0]["action"] == "bound"
    assert evidence["reserved_unbound"]["outcomes"][0]["slurm_job_id"] == "3001"
    assert evidence["inflight"]["outcomes"][0]["action"] == "still_running"
    assert job is not None
    assert job["slurm_job_id"] == "3001"
    assert job["status"] == "running"


def test_restart_reconcile_error_marks_final_mutation_proof_unknown(tmp_path: Path) -> None:
    from services.orchestrator.reconcile import SacctRecord
    from services.orchestrator.reservation import slurm_comment_for

    key = "fcst_gfs_2026052106_model_a:forcing"

    class _ReservedUnboundJob:
        job_id = "job_reserved_crash"
        idempotency_key = key
        status = "reserved"
        slurm_job_id = None
        stage = "forcing"
        job_type = "produce_forcing_array"

    class _FailingReconcileStore:
        def query_reserved_unbound_jobs(self) -> list[Any]:
            return [_ReservedUnboundJob()]

        def query_inflight_jobs(self) -> list[Any]:
            return []

        def bind_reservation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("durable bind failed")

        def update_job_status(self, *args: Any, **kwargs: Any) -> None:
            pass

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="88123",
                raw_state="RUNNING",
                job_name="nhms_forcing",
                comment=slurm_comment_for(key),
            )
        return None

    scheduler = _RealProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        reconcile_store=_FailingReconcileStore(),
        reconcile_comment_query=_comment_query,
        reconcile_sacct_query=lambda _slurm_job_id: None,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    assert result.status == "restart_reconcile_unknown"
    for evidence in (result.evidence, persisted):
        assert evidence["status"] == "restart_reconcile_unknown"
        assert evidence["execution_boundary"] == "restart_reconcile"
        assert evidence["restart_reconcile"]["status"] == "error"
        assert "durable bind failed" in evidence["restart_reconcile"]["reserved_unbound_error"]
        proof = evidence["restart_reconcile_proof"]
        assert proof["status"] == "unknown_after_attempt"
        assert proof["mutation_occurred"] == "unknown_after_attempt"
        assert proof["mutation_outcome"] == "unknown_after_attempt"
        assert proof["pipeline_status_writes"] == "unknown_after_attempt"
        assert proof["pipeline_event_writes"] == "unknown_after_attempt"
        assert proof["pipeline_status_writes_proven_absent"] is False
        assert proof["pipeline_event_writes_proven_absent"] is False
        no_mutation = evidence["no_mutation_proof"]
        assert no_mutation["pipeline_status_writes"] == "unknown_after_attempt"
        assert no_mutation["pipeline_event_writes"] == "unknown_after_attempt"
        assert no_mutation["restart_reconcile_writes"] == "unknown_after_attempt"


def test_restart_reconcile_mutation_is_recorded_when_later_reservation_blocks(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from services.orchestrator.reconcile import SacctRecord
    from services.orchestrator.reservation import slurm_comment_for

    now = _dt("2026-05-21T12:00:00Z")
    suffix = "dddccc111222"
    pass_id = f"scheduler_{format_cycle_time(now)}_{suffix}"
    key = "fcst_gfs_2026052106_model_a:forcing"

    class _ReservedUnboundJob:
        job_id = "job_reserved_crash"
        idempotency_key = key
        status = "reserved"
        slurm_job_id = None
        stage = "forcing"
        job_type = "produce_forcing_array"

    query_calls: list[str] = []
    bound_calls: list[dict[str, Any]] = []

    class _MutatingReconcileStore:
        def query_reserved_unbound_jobs(self) -> list[Any]:
            query_calls.append("reserved_unbound")
            return [_ReservedUnboundJob()]

        def query_inflight_jobs(self) -> list[Any]:
            query_calls.append("inflight")
            return []

        def bind_reservation(self, idem: str, *, slurm_job_id: str, status: str = "submitted") -> dict[str, Any]:
            record = {"idempotency_key": idem, "slurm_job_id": slurm_job_id, "status": status}
            bound_calls.append(record)
            return record

        def update_job_status(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("reservation-blocked reconcile must not update durable state")

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="88123",
                raw_state="RUNNING",
                job_name="nhms_forcing",
                comment=slurm_comment_for(key),
            )
        return None

    monkeypatch.setattr(scheduler_module, "uuid4", lambda: type("FixedUUID", (), {"hex": suffix})())
    scheduler = _RealProductionScheduler(
        _config(tmp_path, now=now, dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        orchestrator_factory=lambda _source_id: pytest.fail("reservation block must stop execution"),
        reconcile_store=_MutatingReconcileStore(),
        reconcile_comment_query=_comment_query,
        reconcile_sacct_query=lambda _slurm_job_id: None,
    )
    reservation_path = Path(scheduler.config.evidence_dir) / f"{pass_id}.pre_execution.json"
    reservation_path.parent.mkdir(parents=True)
    reservation_path.write_text("existing reservation\n", encoding="utf-8")

    result = scheduler.run_once()

    assert query_calls == ["reserved_unbound", "inflight"]
    assert bound_calls == [{"idempotency_key": key, "slurm_job_id": "88123", "status": "submitted"}]
    assert reservation_path.read_text(encoding="utf-8") == "existing reservation\n"
    assert result.status == "preflight_blocked"
    assert result.evidence["execution_boundary"] == "evidence_preflight_blocked"
    assert result.evidence["restart_reconcile"]["status"] == "completed"
    assert result.evidence["restart_reconcile"]["reserved_unbound"]["outcomes"][0]["action"] == "bound"
    assert result.evidence["restart_reconcile_proof"]["mutation_occurred"] is True
    assert result.evidence["restart_reconcile_proof"]["bind_reservation_count"] == 1
    assert result.evidence["evidence_pre_execution"]["status"] == "blocked"
    assert result.evidence["evidence_pre_execution"]["reason"] == "evidence_artifact_exists"
    no_mutation = result.evidence["no_mutation_proof"]
    assert no_mutation["slurm_submit_called"] is False
    assert no_mutation["pipeline_status_writes"] is True
    assert no_mutation["restart_reconcile_writes"] is True
    assert no_mutation != _expected_no_mutation_proof()


def test_concurrency_stays_within_configured_bound(tmp_path: Path) -> None:
    import threading

    active = 0
    peak = 0
    lock = threading.Lock()

    class _CountingOrchestrator:
        def __init__(self) -> None:
            self.object_store = None

        def orchestrate_cycle(self, source: str, cycle_time: datetime, basins: list[dict[str, Any]]) -> PipelineResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                import time as _time

                _time.sleep(0.02)
            finally:
                with lock:
                    active -= 1
            stages = tuple(
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=f"job_{stage.stage}",
                    slurm_job_id=f"slurm_{stage.stage}",
                    status="succeeded",
                )
                for stage in M3_STAGES
            )
            return PipelineResult(
                run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
                cycle_id=cycle_id_for(source, cycle_time),
                status="complete",
                stages=stages,
                candidate_outcomes=(),
            )

    config = _config(tmp_path, sources=("gfs", "IFS", "ERA5"), concurrent_submit_bound=2)
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={
            "gfs": FakeAdapter("gfs", []),
            "IFS": FakeAdapter("IFS", []),
            "ERA5": FakeAdapter("ERA5", []),
        },
        orchestrator_factory=lambda _s: _CountingOrchestrator(),
    )

    candidates = [
        _concurrency_candidate("gfs", "model_a", "basin_a"),
        _concurrency_candidate("IFS", "model_b", "basin_b"),
        _concurrency_candidate("ERA5", "model_c", "basin_c"),
    ]

    evidence = scheduler._execute_candidates(candidates)

    assert len(evidence) == 3
    # Never exceed the configured bound, but do run more than one at a time.
    assert peak <= 2
    assert peak >= 2


# ---------------------------------------------------------------------------
# Issue #292 §4.2: lease heartbeat / renewal + CAS-on-reclaim + liveness reconcile
# ---------------------------------------------------------------------------


def _read_lock(lock_path: Path) -> dict[str, Any]:
    return json.loads(lock_path.read_text(encoding="utf-8"))


def test_scheduler_config_accepts_postgres_lock_backend(tmp_path: Path) -> None:
    config = _config(tmp_path, scheduler_lock_backend="postgres")

    assert config.scheduler_lock_backend == "postgres"


def test_postgres_advisory_lock_key_uses_scheduler_compat_monkeypatch(
    monkeypatch: Any,
) -> None:
    lock_name = "production_scheduler"
    database_url = "postgresql://nhms:secret@db.prod.example/nhms"
    display_lock_path = "/workspace/locks/scheduler.lock"
    expected_lock_key = scheduler_module._postgres_advisory_lock_key(lock_name)

    normal_lease = scheduler_module.PostgresSchedulerLease(
        database_url,
        lock_name=lock_name,
        display_lock_path=display_lock_path,
    )
    assert normal_lease.lock_key == expected_lock_key

    monkeypatch.setattr(
        "services.orchestrator.scheduler._postgres_advisory_lock_key",
        lambda _value: 123,
    )

    patched_lease = scheduler_module.PostgresSchedulerLease(
        database_url,
        lock_name=lock_name,
        display_lock_path=display_lock_path,
    )

    assert patched_lease.lock_key == 123


def test_postgres_lock_backend_does_not_touch_file_guard(monkeypatch: Any, tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        dry_run=False,
        database_url="postgresql://nhms:secret@db.prod.example/nhms",
        scheduler_lock_backend="postgres",
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=FakeForcingProducer(),
        orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(expose_object_store=False),
    )

    def _unexpected_file_lock(self: Any, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        del self, pass_id, started_at
        raise AssertionError("postgres lock backend must not use the file guard")

    def _contended_postgres_lock(self: Any, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        del self, pass_id, started_at
        return {
            "acquired": False,
            "contention": True,
            "lock_path": str(config.lock_path),
            "lock_type": "postgres_advisory",
            "reason": "postgres_advisory_lock_contended",
            "existing_lock": {"raw": None},
        }

    monkeypatch.setattr(FileSchedulerLease, "acquire", _unexpected_file_lock)
    monkeypatch.setattr(PostgresSchedulerLease, "acquire", _contended_postgres_lock)

    result = scheduler.run_once()

    assert result.status == "lock_contended"
    assert result.evidence["lock"]["lock_type"] == "postgres_advisory"
    assert result.evidence["lock"]["reason"] == "postgres_advisory_lock_contended"


def test_renew_bumps_heartbeat_seq_and_preserves_identity(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)

    acquired = lease.acquire(pass_id="p1", started_at=_dt("2026-05-21T12:00:00Z"))
    assert acquired["acquired"] is True
    before = _read_lock(lock_path)
    assert before["heartbeat_seq"] == 0

    # Backdate mtime so we can prove renew refreshes it.
    os.utime(lock_path, (1, 1))

    assert lease.renew(pass_id="p1") is True
    after1 = _read_lock(lock_path)
    assert after1["heartbeat_seq"] == 1
    assert os.stat(lock_path).st_mtime > 1
    # Identity is unchanged.
    for key in ("pass_id", "lease_token", "pid", "host", "started_at", "owner"):
        assert after1[key] == before[key]

    assert lease.renew(pass_id="p1") is True
    assert _read_lock(lock_path)["heartbeat_seq"] == 2


def test_renew_returns_false_after_lease_taken_over(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    acquired = lease.acquire(pass_id="p1", started_at=_dt("2026-05-21T12:00:00Z"))
    assert acquired["acquired"] is True

    # Externally take over: same pass_id but a different lease_token.
    payload = _read_lock(lock_path)
    payload["lease_token"] = "someone-else-token"
    lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    assert lease.renew(pass_id="p1") is False


def test_renew_never_leaves_lock_empty_and_keeps_valid_json(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    acquired = lease.acquire(pass_id="p1", started_at=_dt("2026-05-21T12:00:00Z"))
    assert acquired["acquired"] is True
    before = _read_lock(lock_path)

    assert lease.renew(pass_id="p1") is True

    # The lock must always be non-empty, parseable JSON with the bumped seq and
    # preserved identity — never the empty/half-written window the old
    # truncate-then-write form exposed.
    raw = lock_path.read_text(encoding="utf-8")
    assert raw != ""
    after = json.loads(raw)
    assert after["heartbeat_seq"] == 1
    for key in ("pass_id", "lease_token", "pid", "host", "started_at", "owner"):
        assert after[key] == before[key]
    # No stray temp file left behind by the atomic swap.
    assert not (tmp_path / f"{lock_path.name}.renew.tmp").exists()


def test_renew_failing_mid_write_leaves_original_lock_intact(monkeypatch: Any, tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    assert lease.acquire(pass_id="p1", started_at=_dt("2026-05-21T12:00:00Z"))["acquired"] is True
    original = lock_path.read_text(encoding="utf-8")
    assert original != ""

    real_write = os.write

    def _boom(fd: int, data: bytes) -> int:
        # Fail only the renew payload write, not the guard's own bookkeeping.
        if b'"heartbeat_seq": 1' in data:
            raise OSError("simulated write failure")
        return real_write(fd, data)

    monkeypatch.setattr("services.orchestrator.scheduler.os.write", _boom)

    # renew swallows UnsafeSchedulerLockError-style failures by returning False;
    # a raw OSError propagates out of _rewrite_lock_in_place. Either way the
    # ORIGINAL lock must be untouched and no temp file should survive.
    try:
        result = lease.renew(pass_id="p1")
    except OSError:
        result = False
    assert result is False

    assert lock_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / f"{lock_path.name}.renew.tmp").exists()


def test_live_long_running_holder_is_not_reclaimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    holder = FileSchedulerLease(lock_path, ttl_seconds=1, workspace_root=tmp_path)
    acquired = holder.acquire(pass_id="holder", started_at=_dt("2026-05-21T12:00:00Z"))
    assert acquired["acquired"] is True
    # Holder is THIS process (pid + host recorded), but lock has aged past TTL.
    os.utime(lock_path, (1, 1))

    contender = FileSchedulerLease(lock_path, ttl_seconds=1, workspace_root=tmp_path)
    result = contender.acquire(pass_id="contender", started_at=_dt("2026-05-21T12:10:00Z"))

    assert result["acquired"] is False
    assert result["contention"] is True
    assert _read_lock(lock_path)["pass_id"] == "holder"


def test_dead_holder_is_reclaimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    holder = FileSchedulerLease(lock_path, ttl_seconds=1, workspace_root=tmp_path)
    holder.acquire(pass_id="dead", started_at=_dt("2026-05-21T12:00:00Z"))
    os.utime(lock_path, (1, 1))

    contender = FileSchedulerLease(
        lock_path,
        ttl_seconds=1,
        workspace_root=tmp_path,
        owner_liveness_probe=lambda _payload: False,
    )
    result = contender.acquire(pass_id="contender", started_at=_dt("2026-05-21T12:10:00Z"))

    assert result["acquired"] is True
    assert _read_lock(lock_path)["pass_id"] == "contender"


def test_cross_host_grace_requires_two_ttls(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    now = _dt("2026-05-21T12:00:00Z")

    def _write_foreign_lock(age_seconds: float) -> None:
        payload = {
            "owner": LOCK_OWNER,
            "schema_version": LOCK_SCHEMA_VERSION,
            "pass_id": "foreign",
            "lease_token": "foreign-token",
            "pid": 1,
            "host": "other-host.example",
            "heartbeat_seq": 3,
            "started_at": "2026-05-21T11:00:00Z",
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        ts = now.timestamp() - age_seconds
        os.utime(lock_path, (ts, ts))

    # ttl < age < 2*ttl -> probe None -> NOT reclaimed (grace).
    _write_foreign_lock(age_seconds=15)
    a = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    res_grace = a.acquire(pass_id="contender", started_at=now)
    assert res_grace["acquired"] is False
    assert _read_lock(lock_path)["pass_id"] == "foreign"

    # age > 2*ttl -> reclaimed.
    _write_foreign_lock(age_seconds=25)
    b = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    res_reclaim = b.acquire(pass_id="contender", started_at=now)
    assert res_reclaim["acquired"] is True
    assert _read_lock(lock_path)["pass_id"] == "contender"


def test_cas_aborts_reclaim_when_holder_renews_concurrently(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    holder = FileSchedulerLease(lock_path, ttl_seconds=1, workspace_root=tmp_path)
    holder.acquire(pass_id="holder", started_at=_dt("2026-05-21T12:00:00Z"))
    os.utime(lock_path, (1, 1))

    contender = FileSchedulerLease(
        lock_path,
        ttl_seconds=1,
        workspace_root=tmp_path,
        # Force the stale decision (probe says dead) so we reach the CAS gate.
        owner_liveness_probe=lambda _payload: False,
    )

    # Stale decision saw heartbeat_seq=0; the holder renews between that and the
    # pre-unlink CAS re-read. We must NOT call holder.renew() here because it
    # takes the same guard flock the contender already holds (would deadlock);
    # instead rewrite the lock file in place to advance heartbeat_seq, exactly
    # what a renew would persist.
    real_read = contender._read_existing_lock
    calls = {"n": 0}

    def _read_with_concurrent_renew(*, parent_fd: int) -> dict[str, Any]:
        calls["n"] += 1
        # First read = stale-state read (seq0). Before the CAS re-read (2nd)
        # returns, advance the on-disk heartbeat_seq as a concurrent renew would.
        if calls["n"] == 2:
            payload = _read_lock(lock_path)
            payload["heartbeat_seq"] = int(payload.get("heartbeat_seq", 0)) + 1
            lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return real_read(parent_fd=parent_fd)

    contender._read_existing_lock = _read_with_concurrent_renew  # type: ignore[method-assign]
    result = contender.acquire(pass_id="contender", started_at=_dt("2026-05-21T12:10:00Z"))

    assert result["acquired"] is False
    assert result["contention"] is True
    # Holder's lock is intact (not unlinked) and bumped.
    assert _read_lock(lock_path)["pass_id"] == "holder"
    assert _read_lock(lock_path)["heartbeat_seq"] == 1


def test_default_owner_liveness_probe(tmp_path: Path) -> None:
    import socket as _socket

    me = {"host": _socket.gethostname(), "pid": os.getpid()}
    assert _default_owner_liveness_probe(me) is True

    dead = {"host": _socket.gethostname(), "pid": 2_000_000_000}
    assert _default_owner_liveness_probe(dead) is False

    cross = {"host": "other-host", "pid": 1}
    assert _default_owner_liveness_probe(cross) is None

    no_pid = {"host": _socket.gethostname()}
    assert _default_owner_liveness_probe(no_pid) is None


def test_lease_heartbeat_advances_then_detects_takeover(tmp_path: Path) -> None:
    import time

    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    lease.acquire(pass_id="hb", started_at=_dt("2026-05-21T12:00:00Z"))

    heartbeat = _LeaseHeartbeat(lease, "hb", interval_seconds=0.05)
    heartbeat.start()
    try:
        deadline = time.monotonic() + 2.0
        while _read_lock(lock_path)["heartbeat_seq"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _read_lock(lock_path)["heartbeat_seq"] >= 1
        assert heartbeat.lost is False

        # Externally take over -> heartbeat renew fails -> lost flips True.
        payload = _read_lock(lock_path)
        payload["lease_token"] = "stolen"
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        deadline = time.monotonic() + 2.0
        while not heartbeat.lost and time.monotonic() < deadline:
            time.sleep(0.02)
        assert heartbeat.lost is True
    finally:
        heartbeat.stop()
    assert heartbeat._thread is None


def test_run_once_skips_submission_when_lease_lost_mid_pass(monkeypatch: Any, tmp_path: Path) -> None:
    # A holder whose lease was reclaimed mid-pass must stop before submitting:
    # heartbeat.lost short-circuits the pass to a lease_lost result with no
    # orchestration, no submission, and no mutation.
    #
    # The orchestrator factory must NEVER be invoked once the lease is lost, so
    # we wire a factory that explodes if called — proving the short-circuit ran
    # strictly before any orchestration. (This also sidesteps the env-fragile
    # LocalObjectStore root that FakeProductionOrchestrator builds at /tmp.)
    factory_calls: list[str] = []

    def _exploding_factory(source_id: str) -> Any:
        factory_calls.append(source_id)
        raise AssertionError("orchestrator must not be built after lease lost")

    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=FakeForcingProducer(),
        orchestrator_factory=_exploding_factory,
    )

    # Force the heartbeat to report the lease as lost the instant it starts,
    # without spinning a real thread (deterministic).
    def _start_lost(self: Any) -> None:
        self.lost = True

    monkeypatch.setattr("services.orchestrator.scheduler._LeaseHeartbeat.start", _start_lost)

    result = scheduler.run_once()

    assert result.status == "lease_lost"
    assert result.evidence["status"] == "lease_lost"
    assert result.evidence["pass_id"] == result.pass_id
    assert result.evidence["execution_boundary"] == "lease_lost"
    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    # No submission/orchestration happened once the lease was known lost.
    assert factory_calls == []
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False


def test_run_once_does_not_fence_healthy_pass_when_lease_held(tmp_path: Path) -> None:
    # Common path: lease held (heartbeat.lost stays False) -> the §4.2 guard
    # must NOT fence the pass; it proceeds past the guard into candidate
    # planning + orchestration. (expose_object_store=False keeps the fixture off
    # the env-fragile /tmp LocalObjectStore root so this stays deterministic.)
    orchestrator = FakeProductionOrchestrator(expose_object_store=False)
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        active_repository=FakeActiveRepository(active=False),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=FakeForcingProducer(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    # The §4.2 guard must NOT fire on a held lease: the pass is never fenced as
    # lease_lost and proceeds past the guard into candidate planning. (The final
    # status downstream depends on the slurm/db preflight, which is env-specific
    # on this box; FIX 1 only governs the lease_lost short-circuit.)
    assert result.status != "lease_lost"
    assert result.evidence["execution_boundary"] != "lease_lost"
    assert result.evidence["candidates"], "guard must not fence away planned candidates"


def test_normal_acquire_release_cycle_and_release_cas_noop(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    assert lease.acquire(pass_id="p1", started_at=_dt("2026-05-21T12:00:00Z"))["acquired"] is True
    assert lock_path.exists()

    # release CAS no-ops when the on-disk token no longer matches ours.
    payload = _read_lock(lock_path)
    payload["lease_token"] = "different"
    lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    lease.release(pass_id="p1")
    assert lock_path.exists()  # not unlinked because token mismatched

    # A fresh, matching acquire/release pair fully cleans up.
    lock_path.unlink()
    lease2 = FileSchedulerLease(lock_path, ttl_seconds=10, workspace_root=tmp_path)
    assert lease2.acquire(pass_id="p2", started_at=_dt("2026-05-21T12:00:00Z"))["acquired"] is True
    lease2.release(pass_id="p2")
    assert not lock_path.exists()
