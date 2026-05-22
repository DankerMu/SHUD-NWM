from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator.scheduler import (
    LOCK_OWNER,
    LOCK_SCHEMA_VERSION,
    MAX_DISCOVERED_CYCLES,
    FileSchedulerLease,
    ProductionScheduler,
    ProductionSchedulerConfig,
    SchedulerEvidenceWriteError,
)
from workers.data_adapters.base import CycleDiscovery, cycle_id_for


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


def test_plan_production_cli_smoke_with_injected_scheduler(monkeypatch: Any, tmp_path: Path) -> None:
    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config

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


def _model(model_id: str, basin_id: str) -> dict[str, Any]:
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
        "resource_profile": {
            "runnable": True,
            "memory_gb": 8,
            "display_capabilities": {"tiles": True},
            "frequency_capabilities": {"return_periods": True},
        },
    }


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(evidence["candidates"], key=lambda item: item["candidate_id"])
