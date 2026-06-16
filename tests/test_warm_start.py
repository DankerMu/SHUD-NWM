from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_lineage import (
    LINEAGE_PACKAGE_VERSION_MISMATCH,
    LINEAGE_SOURCE_MISMATCH,
    STATE_QC_FAILED,
    STATE_TOO_STALE,
    WARM_START_LINEAGE_MISMATCH,
    WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
    WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE,
)
from packages.common.state_manager import StateSnapshot
from services.orchestrator.chain import (
    ForecastOrchestrator,
    ModelContext,
    OrchestratorConfig,
    OrchestratorError,
)
from tests.test_orchestrator import FakeOrchestratorRepository, FakeSlurmClient
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig


class FakeStateManager:
    def __init__(
        self,
        snapshots: list[StateSnapshot] | None = None,
        *,
        qc_failures: set[str] | None = None,
    ) -> None:
        self.snapshots = {snapshot.state_id: snapshot for snapshot in snapshots or []}
        self.corrupted: list[str] = []
        self.qc_failures = qc_failures or set()
        self.latest_usable_calls = 0

    def state_variable_qc_passed(self, state: StateSnapshot) -> bool:
        return state.state_id not in self.qc_failures

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        self.latest_usable_calls += 1
        candidates = [
            snapshot
            for snapshot in self.snapshots.values()
            if snapshot.model_id == model_id and snapshot.usable_flag and snapshot.valid_time <= _dt(before_time)
        ]
        return max(candidates, key=lambda snapshot: snapshot.valid_time) if candidates else None

    @property
    def repository(self) -> "FakeStateManager":
        return self

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
    ) -> StateSnapshot | None:
        for snapshot in self.snapshots.values():
            if (
                snapshot.model_id == model_id
                and snapshot.valid_time == _dt(valid_time)
                and (source_id is None or snapshot.source_id == source_id)
            ):
                return snapshot
        return None

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        return self.snapshots.get(state_id)

    def mark_init_state_corrupted(
        self,
        state_id: str,
        *,
        message: str,
        actual_checksum: str | None,
        expected_checksum: str | None,
    ) -> None:
        snapshot = self.snapshots[state_id]
        self.snapshots[state_id] = replace(snapshot, usable_flag=False)
        self.corrupted.append(state_id)


class FakeRuntimeRepository:
    def __init__(self) -> None:
        self.init_state_updates: list[str | None] = []

    def create_run(self, _manifest: dict[str, Any], _run_manifest_uri: str) -> dict[str, Any]:
        return {}

    def update_status(self, _run_id: str, _status: str, **_fields: Any) -> dict[str, Any]:
        return {}

    def mark_failed(self, _run_id: str, _error_code: str, _error_message: str, **_fields: Any) -> dict[str, Any]:
        return {}

    def update_init_state(self, _run_id: str, init_state_id: str | None) -> dict[str, Any]:
        self.init_state_updates.append(init_state_id)
        return {}


def test_forecast_selects_latest_fresh_state_and_writes_nested_manifest(tmp_path: Path) -> None:
    state = _state("state_demo_model_2026043000", "2026-04-30T00:00:00Z")
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]))

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    context, manifest = repository.created_runs[0]
    assert context.init_state_id == state.state_id
    assert manifest["initial_state"] == {
        "state_id": state.state_id,
        "ic_file_uri": state.state_uri,
        "valid_time": "2026-04-30T00:00:00Z",
        "checksum": state.checksum,
        "quality": "fresh",
    }
    assert manifest["runtime"]["init_mode"] == 3
    manifest_path = tmp_path / "workspace" / "runs" / context.run_id / "input" / "manifest.json"
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["initial_state"]["state_id"] == state.state_id
    assert written["runtime"]["init_mode"] == 3


def test_forecast_cold_starts_when_no_state_is_available(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager())

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    _context, manifest = repository.created_runs[0]
    assert manifest["initial_state"]["state_id"] is None
    assert manifest["initial_state"]["quality"] == "cold_start_no_state"
    assert manifest["runtime"]["init_mode"] == 1


def test_forecast_manifest_keeps_product_horizon_and_enables_state_checkpoints(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager())

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    context, manifest = repository.created_runs[0]
    assert context.forecast_horizon_hours == 168
    assert manifest["forecast_horizon_hours"] == 168
    assert manifest["end_time"] == "2026-05-08T00:00:00Z"
    assert manifest["runtime"]["state_checkpoint_hours"] == [6, 12]
    assert manifest["runtime"]["update_ic_step_minutes"] == 360
    assert manifest["runtime"]["update_ic_step_minutes"] * 60 < (
        _dt(manifest["end_time"]) - _dt(manifest["start_time"])
    ).total_seconds()
    manifest_path = tmp_path / "workspace" / "runs" / context.run_id / "input" / "manifest.json"
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["end_time"] == "2026-05-08T00:00:00Z"
    assert written["forecast_horizon_hours"] == 168
    assert written["runtime"]["state_checkpoint_hours"] == [6, 12]


def test_forecast_marks_soft_stale_state_as_degraded(tmp_path: Path) -> None:
    state = _state("state_demo_model_2026042000", "2026-04-20T00:00:00Z")
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]))

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    _context, manifest = repository.created_runs[0]
    assert manifest["initial_state"]["state_id"] == state.state_id
    assert manifest["initial_state"]["quality"] == "degraded_stale_init_state"
    assert manifest["runtime"]["init_mode"] == 3


def test_forecast_hard_stale_state_falls_back_to_cold_start(tmp_path: Path) -> None:
    state = _state("state_demo_model_2026033000", "2026-03-30T00:00:00Z")
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]))

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    _context, manifest = repository.created_runs[0]
    assert manifest["initial_state"]["state_id"] is None
    assert manifest["initial_state"]["quality"] == "cold_start_stale_state"
    assert manifest["runtime"]["init_mode"] == 1


def test_runtime_corrupted_init_state_is_rejected_and_next_state_is_staged(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_runtime_inputs(object_root)
    good_content = b"good-state"
    bad_content = b"bad-state"
    _write_object(object_root, "states/demo_model/2026043000/state.cfg.ic", bad_content)
    _write_object(object_root, "states/demo_model/2026042900/state.cfg.ic", good_content)
    bad_state = _state("state_demo_model_2026043000", "2026-04-30T00:00:00Z", checksum=sha256_bytes(b"expected"))
    good_state = _state("state_demo_model_2026042900", "2026-04-29T00:00:00Z", checksum=sha256_bytes(good_content))
    state_manager = FakeStateManager([bad_state, good_state])
    repository = FakeRuntimeRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    runtime = SHUDRuntime(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
        state_manager=state_manager,
    )
    manifest = _runtime_manifest(bad_state)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert state_manager.corrupted == [bad_state.state_id]
    assert manifest["initial_state"]["state_id"] == good_state.state_id
    assert manifest["initial_state"]["checksum"] == good_state.checksum
    assert manifest["runtime"]["init_mode"] == 3
    assert repository.init_state_updates[-1] == good_state.state_id
    staged_ic = next(input_dir.rglob("*.cfg.ic"))
    assert staged_ic.read_bytes() == good_content


def test_lineage_reject_incompatible_state(tmp_path: Path) -> None:
    # Newer candidate has a mismatched source; older candidate is compatible.
    bad = _state(
        "state_demo_model_2026043012",
        "2026-04-30T12:00:00Z",
        source_id="IFS",
        model_package_version="models/demo_model/package/",
    )
    good = _state(
        "state_demo_model_2026043000",
        "2026-04-30T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([bad, good]))

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        source_id="gfs",
        model_package_version="models/demo_model/package/",
    )

    # Falls back to the next usable (compatible) state, not failing the cycle.
    assert selection.state_id == good.state_id
    assert selection.quality == "fresh"


def test_lineage_reject_falls_back_to_cold_start_with_code(tmp_path: Path) -> None:
    # Only candidate is incompatible (wrong model package version) -> cold start,
    # carrying the stable rejection code.
    bad = _state(
        "state_demo_model_2026043000",
        "2026-04-30T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/OLD/",
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([bad]))

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        source_id="gfs",
        model_package_version="models/demo_model/package/",
    )

    assert selection.state_id is None
    assert selection.quality == "cold_start_no_state"
    assert selection.rejection_code == LINEAGE_PACKAGE_VERSION_MISMATCH


def test_lineage_source_mismatch_rejection_code(tmp_path: Path) -> None:
    bad = _state("state_demo_model_2026043000", "2026-04-30T00:00:00Z", source_id="IFS")
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([bad]))

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        source_id="gfs",
    )

    assert selection.state_id is None
    assert selection.rejection_code == LINEAGE_SOURCE_MISMATCH


def test_qc_failure_fallback(tmp_path: Path) -> None:
    # Newer candidate fails state-variable QC; older one passes and is selected.
    failing = _state("state_demo_model_2026043012", "2026-04-30T12:00:00Z")
    healthy = _state("state_demo_model_2026043000", "2026-04-30T00:00:00Z")
    repository = FakeOrchestratorRepository()
    state_manager = FakeStateManager([failing, healthy], qc_failures={failing.state_id})
    orchestrator = _orchestrator(tmp_path, repository, state_manager)

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
    )

    assert selection.state_id == healthy.state_id
    assert selection.quality == "fresh"


def test_qc_failure_cold_start_carries_code(tmp_path: Path) -> None:
    failing = _state("state_demo_model_2026043000", "2026-04-30T00:00:00Z")
    repository = FakeOrchestratorRepository()
    state_manager = FakeStateManager([failing], qc_failures={failing.state_id})
    orchestrator = _orchestrator(tmp_path, repository, state_manager)

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
    )

    assert selection.state_id is None
    assert selection.rejection_code == STATE_QC_FAILED


def test_stale_cold_start_reports_staleness_not_carried_lineage_code(tmp_path: Path) -> None:
    # A younger candidate is lineage-rejected (wrong source), and the only older
    # candidate is past the hard staleness threshold -> terminal cold_start_stale_state.
    # The rejection_code must reflect the PRIMARY cause (staleness) via STATE_TOO_STALE,
    # NOT the carried-forward LINEAGE_SOURCE_MISMATCH of the younger candidate (which
    # would falsely conflate quality=stale with a lineage rejection).
    younger_bad_lineage = _state(
        "state_demo_model_2026043000",
        "2026-04-30T00:00:00Z",  # ~1 day before cycle, but wrong source
        source_id="IFS",
    )
    older_too_stale = _state(
        "state_demo_model_2026030100",
        "2026-03-01T00:00:00Z",  # > 30 days before cycle -> cold_start_stale_state
        source_id="GFS",
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([younger_bad_lineage, older_too_stale]))

    selection = orchestrator._select_forecast_initial_state(
        model_id="demo_model",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        source_id="gfs",
    )

    assert selection.state_id is None
    assert selection.quality == "cold_start_stale_state"
    assert selection.rejection_code == STATE_TOO_STALE
    assert selection.rejection_code != LINEAGE_SOURCE_MISMATCH


def test_strict_forecast_uses_exact_successor_and_writes_manifest(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    repository.model = replace(repository.model, model_package_checksum="package-sha")
    state_manager = FakeStateManager([state])
    orchestrator = _orchestrator(tmp_path, repository, state_manager, require_forecast_warm_start=True)

    orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    context, manifest = repository.created_runs[0]
    assert context.init_state_id == state.state_id
    assert context.init_state_valid_time == _dt("2026-05-01T00:00:00Z")
    assert manifest["initial_state"]["state_id"] == state.state_id
    assert manifest["initial_state"]["valid_time"] == "2026-05-01T00:00:00Z"
    assert manifest["initial_state"]["quality"] == "fresh"
    assert manifest["initial_state"]["lineage"]["lead_hours"] == 12
    assert manifest["initial_state"]["lineage"]["model_package_checksum"] == "package-sha"
    assert manifest["runtime"]["init_mode"] == 3
    assert state_manager.latest_usable_calls == 0


def test_strict_forecast_missing_exact_blocks_before_side_effects(tmp_path: Path) -> None:
    latest = _state(
        "state_demo_model_2026043012",
        "2026-04-30T12:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    state_manager = FakeStateManager([latest])
    orchestrator = _orchestrator(tmp_path, repository, state_manager, require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_SUCCESSOR_CHECKPOINT_MISSING
    assert state_manager.latest_usable_calls == 0
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_without_state_manager_blocks_before_side_effects(tmp_path: Path) -> None:
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, None, require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_SUCCESSOR_CHECKPOINT_MISSING
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_unusable_exact_state_blocks(tmp_path: Path) -> None:
    state = replace(
        _state(
            "state_demo_model_2026050100",
            "2026-05-01T00:00:00Z",
            source_id="GFS",
            model_package_version="models/demo_model/package/",
            lead_hours=12,
        ),
        usable_flag=False,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_qc_failure_blocks(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    state_manager = FakeStateManager([state], qc_failures={state.state_id})
    orchestrator = _orchestrator(tmp_path, repository, state_manager, require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_lineage_mismatch_blocks(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="IFS",
        model_package_version="models/demo_model/package/",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_package_version_mismatch_blocks_before_side_effects(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/old-package/",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_package_checksum_mismatch_blocks(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        model_package_checksum="old-package-sha",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    repository.model = replace(repository.model, model_package_checksum="package-sha")
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_missing_target_checksum_blocks_when_state_has_checksum(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_missing_target_and_state_checksum_blocks(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_strict_forecast_wrong_lead_blocks(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050100",
        "2026-05-01T00:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        lead_hours=6,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.trigger_forecast(source_id="gfs", cycle_time="2026050100", model_id="demo_model")

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_forecast_mutation(tmp_path, repository, orchestrator)


def test_model_context_checksum_mismatch_blocks_under_strict_mode(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050112",
        "2026-05-01T12:00:00Z",
        source_id="gfs",
        model_package_version="models/demo_model/package/",
        model_package_checksum="old-package-sha",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)
    model = ModelContext(
        model_id="demo_model",
        basin_id="yangtze",
        basin_version_id="basin_v1",
        river_network_version_id="rivnet_v1",
        segment_count=2,
        model_package_uri="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    basins = orchestrator._normalize_cycle_basins([model], "gfs", _dt("2026-05-01T12:00:00Z"))

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._apply_cohort_warm_start(basins, "gfs", _dt("2026-05-01T12:00:00Z"))

    assert basins[0]["model_package_checksum"] == "package-sha"
    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH


def test_normalize_raw_package_checksum_alias_feeds_strict_validation(tmp_path: Path) -> None:
    state = _state(
        "state_demo_model_2026050112",
        "2026-05-01T12:00:00Z",
        source_id="GFS",
        model_package_version="models/demo_model/package/",
        model_package_checksum="old-package-sha",
        lead_hours=12,
    )
    repository = FakeOrchestratorRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeStateManager([state]), require_forecast_warm_start=True)
    basin = {
        "model_id": "demo_model",
        "basin_id": "yangtze",
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rivnet_v1",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "package_checksum": "package-sha",
        "source_id": "gfs",
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt("2026-05-01T12:00:00Z"))

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator._apply_cohort_warm_start(basins, "gfs", _dt("2026-05-01T12:00:00Z"))

    assert basins[0]["model_package_checksum"] == "package-sha"
    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH


def _assert_no_forecast_mutation(
    tmp_path: Path,
    repository: FakeOrchestratorRepository,
    orchestrator: ForecastOrchestrator,
) -> None:
    assert repository.created_runs == []
    assert repository.hydro_statuses == []
    assert not (tmp_path / "workspace" / "runs").exists()
    assert not orchestrator.object_store.exists("runs/fcst_gfs_2026050100_demo_model/input/manifest.json")
    assert orchestrator.slurm_client.submissions == []


def _orchestrator(
    tmp_path: Path,
    repository: FakeOrchestratorRepository,
    state_manager: FakeStateManager | None,
    *,
    require_forecast_warm_start: bool = False,
) -> ForecastOrchestrator:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
        require_forecast_warm_start=require_forecast_warm_start,
    )
    return ForecastOrchestrator(
        config=config,
        repository=repository,
        state_manager=state_manager,
        slurm_client=FakeSlurmClient(),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )


def _state(
    state_id: str,
    valid_time: str,
    *,
    checksum: str = "abc123",
    source_id: str | None = None,
    model_package_version: str | None = None,
    model_package_checksum: str | None = None,
    lead_hours: int | None = None,
) -> StateSnapshot:
    return StateSnapshot(
        state_id=state_id,
        model_id="demo_model",
        run_id="analysis_previous",
        valid_time=_dt(valid_time),
        state_uri=f"states/demo_model/{_dt(valid_time):%Y%m%d%H}/state.cfg.ic",
        checksum=checksum,
        usable_flag=True,
        source_id=source_id,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
        lead_hours=lead_hours,
    )


def _runtime_manifest(state: StateSnapshot) -> dict[str, Any]:
    return {
        "run_id": "fcst_gfs_2026050100_demo_model",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": "GFS",
        "cycle_time": "2026-05-01T00:00:00Z",
        "start_time": "2026-05-01T00:00:00Z",
        "end_time": "2026-05-02T00:00:00Z",
        "model": {
            "model_id": "demo_model",
            "basin_version_id": "basin_v01",
            "model_package_uri": "s3://nhms/models/demo_model/package/",
            "project_name": "demo",
            "segment_count": 2,
        },
        "initial_state": {
            "state_id": state.state_id,
            "ic_file_uri": state.state_uri,
            "valid_time": "2026-04-30T00:00:00Z",
            "checksum": state.checksum,
            "quality": "fresh",
        },
        "forcing": {
            "forcing_version_id": "forc_gfs_2026050100_demo_model",
            "forcing_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/",
        },
        "runtime": {"output_interval_minutes": 1440, "init_mode": 3},
        "outputs": {"run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050100_demo_model/input/manifest.json"},
    }


def _write_runtime_inputs(object_root: Path) -> None:
    package = object_root / "models" / "demo_model" / "package"
    package.mkdir(parents=True)
    (package / "demo.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "demo.para").write_text("START_TIME = {{START_TIME}}\n", encoding="utf-8")
    (package / "demo.calib").write_text("calib\n", encoding="utf-8")
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    forcing.mkdir(parents=True)
    (forcing / "forcing.tsd.forc").write_text("forcing\n", encoding="utf-8")


def _write_object(object_root: Path, key: str, content: bytes) -> None:
    path = object_root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _dt(value: str | datetime) -> datetime:
    candidate = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)
