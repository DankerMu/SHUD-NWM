from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator.chain import M3_STAGES, PipelineResult, StageRunResult
from services.orchestrator.scheduler import (
    LOCK_OWNER,
    LOCK_SCHEMA_VERSION,
    MAX_CONTINUOUS_JSON_PASSES,
    MAX_DISCOVERED_CYCLES,
    MAX_LOCK_PAYLOAD_BYTES,
    FileSchedulerLease,
    ProductionScheduler,
    ProductionSchedulerConfig,
    SchedulerEvidenceWriteError,
    SchedulerPassResult,
)
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time


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
    assert result.evidence["no_mutation_proof"] == {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
    }


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


def test_plan_production_cli_uses_fresh_default_workspace_path(
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

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)

    rc = cli.main(["plan-production"])

    workspace_root = tmp_path / ".nhms-workspace"
    assert rc == 0
    assert captured["config"].workspace_root == workspace_root.resolve()
    assert Path(captured["config"].lock_path) == workspace_root.resolve() / "scheduler" / "production-scheduler.lock"
    assert Path(captured["config"].evidence_dir) == workspace_root.resolve() / "scheduler" / "evidence"


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
    assert result.evidence["counts"]["submitted_count"] == 0


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

    assert result.status == "planned"
    assert result.evidence["execution_boundary"] == "production_orchestration"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is True
    assert result.evidence["model_run_evidence"][0]["standard_chain_shape"] == [stage.stage for stage in M3_STAGES]
    assert result.evidence["model_run_evidence"][0]["qhh_script_invoked"] is False
    assert result.evidence["model_run_evidence"][0]["output_key"] == (
        "runs/fcst_gfs_2026052106_basins_qhh_shud/output/"
    )
    assert "output_uri" not in result.evidence["model_run_evidence"][0]
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
    assert "output_uri" not in submitted_basin


def test_public_from_env_wires_active_repository(monkeypatch: Any, tmp_path: Path) -> None:
    active_repository = FakeActiveRepository(active=False)
    monkeypatch.setattr("services.orchestrator.scheduler._active_repository_from_env", lambda: active_repository)
    monkeypatch.setattr("services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env", lambda: FakeRegistry([]))
    monkeypatch.setattr("services.orchestrator.scheduler._default_adapters", lambda: {})

    scheduler = ProductionScheduler.from_env(_config(tmp_path, now=_dt("2026-05-21T12:00:00Z")))

    assert scheduler.active_repository is active_repository


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


class FakeAdapter:
    def __init__(self, source_id: str, cycles: list[tuple[str, bool]]) -> None:
        self.source_id = source_id
        self.cycles = cycles
        self.download_calls = 0

    def discover_cycles(self, cycle_date: Any, end_date: Any = None) -> list[CycleDiscovery]:
        del end_date
        requested_date = cycle_date.date() if isinstance(cycle_date, datetime) else cycle_date
        return [
            CycleDiscovery(
                cycle_id=cycle_id_for(self.source_id, _dt(cycle_time)),
                source_id=self.source_id,
                cycle_time=_dt(cycle_time),
                cycle_hour=_dt(cycle_time).hour,
                available=available,
                status="discovered" if available else "unavailable",
            )
            for cycle_time, available in self.cycles
            if _dt(cycle_time).date() == requested_date
        ]

    def download_plan(self, *_args: Any, **_kwargs: Any) -> None:
        self.download_calls += 1
        raise AssertionError("dry-run scheduler must not download")


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
    def __init__(self, *, active: bool) -> None:
        self.active = active

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return self.active


class FakeProductionOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
            status="complete",
            stages=stages,
        )


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
