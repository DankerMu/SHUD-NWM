from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.chain import M3_STAGES, PipelineResult, StageRunResult, build_model_run_assembly
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
    ProductionSchedulerConfig,
    SchedulerEvidenceWriteError,
    SchedulerPassResult,
)
from services.orchestrator.scheduler import (
    ProductionScheduler as _RealProductionScheduler,
)
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from workers.canonical_converter.converter import GFS_REQUIRED_STANDARD_VARIABLES, evaluate_canonical_readiness
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time
from workers.shud_runtime import runtime as shud_runtime_module

_TEST_CANONICAL_READINESS_PROVIDER_UNSET = object()


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
    ) -> None:
        self._test_canonical_readiness_omitted = (
            canonical_readiness_provider is _TEST_CANONICAL_READINESS_PROVIDER_UNSET
        )
        if canonical_readiness_provider is _TEST_CANONICAL_READINESS_PROVIDER_UNSET:
            canonical_readiness_provider = _AlwaysReadyCanonicalReadinessProvider()
        super().__init__(
            config,
            registry=registry,
            adapters=adapters,
            active_repository=active_repository,
            canonical_readiness_provider=canonical_readiness_provider,
            forcing_producer=forcing_producer,
            orchestrator_factory=orchestrator_factory,
            sleep=sleep,
        )

    def _canonical_readiness_for_candidate(self, candidate: Any, cycle: Any) -> dict[str, Any] | None:
        if self._test_canonical_readiness_omitted:
            return None
        return super()._canonical_readiness_for_candidate(candidate, cycle)


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
    assert gfs_model_a["frequency_capabilities"] == {"return_periods": True}
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
    assert canonical["missing_leads"][0]["missing_variables"] == ["shortwave_down"]
    assert adapter.download_calls == 0


def test_non_ok_canonical_readiness_blocks_forcing_candidate_submission(tmp_path: Path) -> None:
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

    assert result.evidence["candidates"] == []
    assert result.evidence["counts"]["submitted_count"] == 0
    blocked = result.evidence["blocked_candidates"][0]
    assert blocked["reason"] == "missing_canonical_leads"
    canonical = blocked["state_evidence"]["canonical_readiness"]
    assert canonical["status"] == "canonical_incomplete"
    assert canonical["rejected_quality_flags"] == {"warn": 1}
    assert canonical["missing_leads"][0]["missing_variables"] == ["shortwave_down"]
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


@pytest.mark.parametrize(
    ("status", "reason", "classifier", "retryable"),
    [
        ("unavailable", "source_cycle_unavailable", "unavailable", True),
        ("forbidden", "source_cycle_forbidden", "forbidden", False),
        ("stale", "source_cycle_stale", "stale", True),
        ("policy_blocked", "source_cycle_policy_blocked", "policy_blocked", False),
    ],
)
def test_source_blocker_preserves_adapter_classifier_and_redacts_probe_credentials(
    tmp_path: Path,
    status: str,
    reason: str,
    classifier: str,
    retryable: bool,
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


def test_pass_source_cycle_evidence_redacts_forbidden_probe_credentials(tmp_path: Path) -> None:
    signed_probe = (
        "https://user:password@provider.example.test/file"
        "?token=super-secret&X-Amz-Signature=secret-signature"
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
                    "task_results": [
                        {**identity, "basin_id": "basin_other", "task_id": 0, "status": "failed"}
                    ],
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
            frequency_capabilities={},
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
    assert any(
        source.startswith("pipeline_events[0].details")
        for source in validation["legacy_non_authoritative"]
    )
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
    assert scheduler_module._candidate_state_is_candidate_scoped_retry(decision) is False


def test_non_authoritative_task_results_do_not_bypass_active_cycle_duplicate_block(
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

    skipped = result.evidence["skipped_candidates"][0]
    assert result.evidence["counts"]["submitted_count"] == 0
    assert skipped["reason"] == "active_duplicate_pipeline"
    assert skipped["state_evidence"]["decision"] == "retry_failed"
    assert skipped["state_evidence"]["task_identity"] == {}
    assert orchestrator.calls == []


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
        "frequency_publish",
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
    assert production_stage_for("frequency") == "frequency_publish"
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
            active_jobs=[
                {"job_id": "job_forcing", "slurm_job_id": "7777", "stage": "forcing", "status": "running"}
            ]
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


def test_scheduler_caps_reject_oversized_config_and_bound_candidate_work(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lookback_hours exceeds limit"):
        _config(tmp_path, lookback_hours=169)
    with pytest.raises(ValueError, match="source count exceeds limit"):
        _config(tmp_path, sources=("gfs", "IFS", "a", "b", "c"))

    config = _config(tmp_path, now=_dt("2026-05-21T18:00:00Z"), sources=("gfs",), max_cycles_per_source=16)
    models = [_model(f"model_{index:05d}", "basin_a") for index in range(626)]
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry(models),
        adapters={
            "gfs": FakeAdapter(
                "gfs",
                [(f"2026-05-21T{hour:02d}:00:00Z", True) for hour in range(16)],
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
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 400)
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
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 900)
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
    monkeypatch.setattr(
        "services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env",
        lambda: FakeRegistry([_model(f"model_{index:05d}", "basin_a") for index in range(626)]),
    )
    monkeypatch.setattr(
        "services.orchestrator.scheduler._default_adapters",
        lambda: {
            "gfs": FakeAdapter(
                "gfs",
                [(f"2026-05-21T{hour:02d}:00:00Z", True) for hour in range(16)],
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
        max_cycles_per_source=16,
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
    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 1200)

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
            "frequency_capabilities": {"return_periods": False},
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
            "frequency_capabilities": {"return_periods": False},
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
            "frequency_capabilities": {"return_periods": False},
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
        "frequency_capabilities": {"return_periods": False},
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


def test_slurm_preflight_ready_without_factory_uses_default_orchestrator_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    constructed: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    class DefaultPathOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any) -> None:
            constructed.append({"config": config, "repository": repository, "state_manager": state_manager})
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
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", lambda: "repository-from-env")
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
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["slurm_preflight"]["status"] == "ready"
    assert len(constructed) == 1
    assert constructed[0]["repository"] == "repository-from-env"
    assert constructed[0]["state_manager"] is None
    assert constructed[0]["config"].source_id == "gfs"
    assert constructed[0]["config"].workspace_root == roots["workspace_root"].resolve()
    assert constructed[0]["config"].object_store_root == roots["object_store_root"].resolve()
    assert constructed[0]["config"].slurm_job_type_templates == dict(DEFAULT_JOB_TYPE_TEMPLATES)
    assert constructed[0]["config"].slurm_gateway_url == "http://slurm-gateway.internal:8000"
    assert calls[0]["source"] == "gfs"
    assert calls[0]["basins"][0]["output_uri"].startswith("s3://nhms/default/runs/")


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

        def __init__(self, *, config: Any, repository: Any, state_manager: Any) -> None:
            constructed.append({"config": config, "repository": repository, "state_manager": state_manager})

        def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str) -> list[dict[str, Any]]:
            reservation_seen_before_cancel.append(bool(list(roots["workspace_root"].glob("scheduler/evidence/*.pre_execution.json"))))
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

    class DefaultPathCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any) -> None:
            del config, repository, state_manager

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


def test_cancel_active_slurm_gap_blocks_top_level_cancelled_status(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)

    class GapCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any) -> None:
            del config, repository, state_manager

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


@pytest.mark.parametrize("hydro_status", ["succeeded", "parsed", "frequency_done", "published", "complete"])
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


@pytest.mark.parametrize("hydro_status", ["succeeded", "parsed", "frequency_done", "published"])
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
    assert state["frequency_resubmitted"] is False
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
        ("frequency", "FREQUENCY_FAILED", "publication_failure"),
        ("publish", "PUBLISH_FAILED", "publication_failure"),
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
    calls_by_model = {
        call["basins"][0]["model_id"]: call
        for call in orchestrator.calls
    }
    assert calls_by_model["model_a"]["basins"][0]["restart_stage"] == "parse"
    assert calls_by_model["model_a"]["basins"][0]["orchestration_run_id"].endswith("_parse_model_a")
    assert "restart_stage" not in calls_by_model["model_b"]["basins"][0]
    assert "orchestration_run_id" not in calls_by_model["model_b"]["basins"][0]


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
    assert skipped["reason"] == expected_reason
    assert skipped["state_evidence"]["decision"] == "skip_active"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []


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
                "failed https://user:pass@example.test/log?signature=sig123 "
                "token=tok123 password=pass123"
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
                        "frequency_capabilities": {
                            "return_periods": True,
                            "curves_available": False,
                            "warning_thresholds_available": False,
                        },
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
    assert submitted_basin["candidate_id"] == (
        "gfs:2026-05-21T06:00:00Z:basins_qhh_shud:forecast_gfs_deterministic"
    )
    assert submitted_basin["run_id"] == "fcst_gfs_2026052106_basins_qhh_shud"
    assert submitted_basin["forcing_version_id"] == "forc_gfs_2026052106_basins_qhh_shud"
    assert submitted_basin["model_package_uri"] == "s3://nhms/models/basins_qhh_shud/package/"
    assert submitted_basin["station_count"] == 386
    assert submitted_basin["frequency_curves_available"] is False
    assert submitted_basin["warning_thresholds_available"] is False
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
        "frequency_capabilities": {"return_periods": True},
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
                        "frequency_capabilities": {
                            "return_periods": True,
                            "curves_available": False,
                            "warning_thresholds_available": False,
                        },
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
    assert model_evidence["quality_states"]["frequency"]["unavailable_products"] == [
        "return_period_curves",
        "warning_thresholds",
    ]
    assert any(blocker["field"] == "frequency" for blocker in model_evidence["residual_blockers"])
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
        "included_count": 0,
        "omitted_count": 1,
        "matched_count": 0,
        "matching": "candidate_identity",
        "limit": MAX_MODEL_RUN_STAGE_TASK_ROWS,
        "status_counts": {"succeeded": 1},
    }
    assert failed["resource_summary"]["stage_accounting"][0]["accounting"]["max_rss"] == "3072K"
    assert failed["resource_summary"]["task_accounting"] == []
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
        assert item["resource_summary"]["task_accounting"][0]["slurm_job_id"] == (
            stage["task_results"][0]["slurm_job_id"]
        )
        assert summary["total_count"] == 3
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
        (repo_root / relative_path).read_text(encoding="utf-8")
        for relative_path in production_sections
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
        ("published_root", "SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_FOUND"),
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
    completed_statuses = {"succeeded", "parsed", "frequency_done", "published", "complete"}
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
            self.object_store = LocalObjectStore("/tmp/nhms-test-object-store", "s3://nhms")
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
                    )
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
        "dry_run": True,
    }
    values.update(kwargs)
    return ProductionSchedulerConfig(**values)


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
    monkeypatch.setenv("NHMS_SCHEDULER_SOURCES", "gfs")
    monkeypatch.setenv("NHMS_SCHEDULER_MODEL_IDS", "model_a")
    monkeypatch.setenv("NHMS_SCHEDULER_BASIN_IDS", "basin_a")


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
        "frequency_capabilities": {"return_periods": True},
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
            frequency_capabilities={},
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
