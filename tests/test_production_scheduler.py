from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
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
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
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

    class DefaultPathCancelOrchestrator:
        stages = M3_STAGES

        def __init__(self, *, config: Any, repository: Any, state_manager: Any) -> None:
            constructed.append({"config": config, "repository": repository, "state_manager": state_manager})

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
    assert cancellation["cancelled_jobs"][0]["slurm_job_id"] == "7777"
    assert cancellation["cancelled_jobs"][0]["replacement_submitted"] is False
    assert cancel_calls == [("gfs_2026052106", "scheduler_cancel_requested")]
    assert len(constructed) == 1
    assert constructed[0]["repository"] == "repository-from-env"
    assert constructed[0]["state_manager"] is None
    assert constructed[0]["config"].source_id == "gfs"
    assert constructed[0]["config"].object_store_root == roots["object_store_root"].resolve()
    assert constructed[0]["config"].slurm_gateway_url == "http://slurm-gateway.internal:8000"


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
                "pipeline_status": "running",
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
    evidence_text = json.dumps(result.evidence, sort_keys=True)
    assert result.evidence["model_run_evidence"][0]["error_code"] == "PRODUCTION_ORCHESTRATION_FAILED"
    for raw_secret in ("user:pass", "sig123", "tok123", "pass123", "signature=sig123", "token=tok123"):
        assert raw_secret not in evidence_text
        assert raw_secret not in artifact_text
    assert "[redacted]" in evidence_text
    assert "[redacted]" in artifact_text


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
    assert result.evidence["no_mutation_proof"] == {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
    }
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


def test_plan_production_public_slurm_path_rejects_pipeline_database_url_only(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    roots = _slurm_roots(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://nhms:secret@db.prod.example/nhms")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ENABLED", "1")
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("SLURM_SHARED_LOG_ROOT", str(roots["log_root"]))
    monkeypatch.setenv("NHMS_RUNTIME_ROOT", str(roots["runtime_root"]))
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
        scheduler_module,
        "_now",
        lambda _config: _dt("2026-05-21T12:00:00Z"),
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
    assert payload["counts"]["submitted_count"] == 0
    assert payload["model_run_evidence"][0]["error_code"] == "SLURM_PREFLIGHT_DATABASE_URL_MISSING"


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
        "log_root": root / "logs",
        "runtime_root": root / "runtime",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


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
