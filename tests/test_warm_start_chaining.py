"""M24 §2 Lane 2: analysis-segment time semantics, IC materialization, and the
three-way (scheduler basin / cycle-stage / forecast runtime) warm-start manifest wiring.

Requirement-driven tests for ``cross-cycle-warm-start-chaining`` spec:
- restart cadence (Update_IC_STEP) lands exactly on T_{N+1} for 6h/12h/24h segments
- native ``*.cfg.ic.update`` -> canonical ``state.cfg.ic`` -> ``<project>.cfg.ic``
  materialization records the original SHUD filename and restamps the header
- snapshot valid_time / IC header minute-time / run start three-way consistency
- the saved snapshot is keyed at the next cycle's init time (segment end), not the
  forecast-window end
- cohort manifests (scheduler basin / cycle-stage index / forecast runtime) carry the
  same selected ``init_state_uri`` + checksum + lineage with ``init_mode=3``
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import StateSnapshot
from services.orchestrator.chain import (
    ForecastOrchestrator,
    OrchestratorConfig,
    _analysis_forcing_causality,
    _analysis_update_ic_step_minutes,
    _check_three_way_time_consistency,
)
from tests.test_orchestrator import FakeOrchestratorRepository, FakeSlurmClient
from tests.test_warm_start import FakeRuntimeRepository, FakeStateManager
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig, _read_cfg_ic_header_minute


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _minute_time(value: str) -> float:
    return _dt(value).timestamp() / 60.0


# ---------------------------------------------------------------------------
# Update_IC_STEP restart cadence lands exactly on T_{N+1}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected_minutes"),
    [
        ("2026-05-01T00:00:00Z", "2026-05-01T06:00:00Z", 360),  # 6h cycle
        ("2026-05-01T00:00:00Z", "2026-05-01T12:00:00Z", 720),  # 12h cycle
        ("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", 1440),  # 24h cycle
    ],
)
def test_update_ic_step_lands_on_next_cycle_init(start: str, end: str, expected_minutes: int) -> None:
    # Cadence equals the full segment length so the restart write lands exactly on
    # T_{N+1}, never the default 1440-minute day, never an earlier modulo boundary.
    cadence = _analysis_update_ic_step_minutes(_dt(start), _dt(end))
    assert cadence == expected_minutes
    segment_minutes = int((_dt(end) - _dt(start)).total_seconds() // 60)
    assert segment_minutes % cadence == 0
    # A restart write at multiples of the cadence lands on the segment end.
    assert segment_minutes // cadence == 1


def test_update_ic_step_rejects_non_positive_window() -> None:
    with pytest.raises(Exception):
        _analysis_update_ic_step_minutes(_dt("2026-05-01T06:00:00Z"), _dt("2026-05-01T00:00:00Z"))


def test_analysis_forcing_causality_marker_defaults_to_causal() -> None:
    causal = _analysis_forcing_causality()
    assert causal == {"mode": "causal", "latency_minutes": None, "no_future_leak": True}
    delayed = _analysis_forcing_causality(latency_minutes=720)
    assert delayed == {"mode": "delayed_reanalysis", "latency_minutes": 720, "no_future_leak": True}


# ---------------------------------------------------------------------------
# Three-way time consistency helper
# ---------------------------------------------------------------------------


def test_three_way_time_consistency_passes_when_all_equal_next_cycle_init() -> None:
    t_next = "2026-05-02T00:00:00Z"
    reason = _check_three_way_time_consistency(
        snapshot_valid_time=_dt(t_next),
        ic_header_minute_time=_minute_time(t_next),
        run_start_time=_dt(t_next),
    )
    assert reason is None


def test_three_way_time_consistency_blocks_on_mismatch() -> None:
    reason = _check_three_way_time_consistency(
        snapshot_valid_time=_dt("2026-05-02T00:00:00Z"),
        ic_header_minute_time=_minute_time("2026-05-02T00:00:00Z"),
        run_start_time=_dt("2026-05-01T00:00:00Z"),  # forecast-window end, wrong key
    )
    assert reason is not None
    assert "mismatch" in reason


# ---------------------------------------------------------------------------
# Saved snapshot is keyed at the next cycle init time (analysis segment end)
# ---------------------------------------------------------------------------


def test_saved_state_valid_time_equals_next_cycle_init(tmp_path: Path) -> None:
    # Analysis segment [T_N, T_{N+1}] with end_time == T_{N+1}; the saved snapshot
    # MUST be keyed at T_{N+1} (the next cycle init), not at any forecast-window end.
    from packages.common.state_cli import StateRunContext, save_state_for_run
    from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager

    t_n = "2026-05-01T00:00:00Z"
    t_next = "2026-05-02T00:00:00Z"
    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    run_id = "analysis_era5_2026050100_2026050200_demo_model"

    # Native SHUD end-of-segment restart artifact: *.cfg.ic.update with a header
    # minute-time at T_{N+1}.
    output_dir = workspace / "runs" / run_id / "output"
    output_dir.mkdir(parents=True)
    ic_update = output_dir / "demo.cfg.ic.update"
    ic_update.write_text(f"2 1 {_minute_time(t_next):.6f}\n1 0.1\n2 0.2\n1 0.0\n", encoding="utf-8")

    captured: dict[str, Any] = {}

    class _Repo(PsycopgStateSnapshotRepository):
        def __init__(self) -> None:  # noqa: D401 - test double
            pass

        def get_state_snapshot_by_model_time(self, *, model_id: str, valid_time: datetime) -> StateSnapshot | None:
            return None

        def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
            captured["snapshot"] = snapshot
            return snapshot

        def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
            return captured.get("snapshot")

        def insert_qc_result(self, record: Any) -> dict[str, Any]:
            captured.setdefault("qc_records", []).append(record)
            return {}

        def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
            captured["usable_flag"] = usable_flag
            return captured.get("snapshot")

    class _RunRepo:
        def load_run_context(self, _run_id: str) -> StateRunContext:
            return StateRunContext(
                run_id=run_id,
                model_id="demo_model",
                end_time=_dt(t_next),
                output_uri=None,
            )

    manager = StateManager(repository=_Repo(), object_store=LocalObjectStore(object_root, ""))

    result = save_state_for_run(
        run_id,
        manager=manager,
        repository=_RunRepo(),
        workspace_root=workspace,
    )

    snapshot = captured["snapshot"]
    # Keyed at T_{N+1}, NOT at T_N or any forecast-window end.
    assert snapshot.valid_time == _dt(t_next)
    assert snapshot.valid_time != _dt(t_n)
    # Canonical object key normalizes to state.cfg.ic and records the original name.
    assert snapshot.state_uri.endswith("state.cfg.ic")
    assert snapshot.original_shud_filename == "demo.cfg.ic.update"
    assert result["state_uri"].endswith("state.cfg.ic")


# ---------------------------------------------------------------------------
# IC materialization on the consume side: state.cfg.ic -> <project>.cfg.ic
# ---------------------------------------------------------------------------


def _runtime(tmp_path: Path, state_manager: FakeStateManager) -> tuple[SHUDRuntime, Path, Path]:
    object_root = tmp_path / "object-store"
    package = object_root / "models" / "demo_model" / "package"
    package.mkdir(parents=True)
    (package / "demo.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "demo.para").write_text("START\tEND\nINIT_MODE\n", encoding="utf-8")
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    runtime = SHUDRuntime(
        config=config,
        repository=FakeRuntimeRepository(),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
        state_manager=state_manager,
    )
    return runtime, object_root, config.workspace_root


def _ic_state(valid_time: str, content: bytes) -> StateSnapshot:
    return StateSnapshot(
        state_id=f"state_demo_model_{_dt(valid_time):%Y%m%d%H}",
        model_id="demo_model",
        run_id="analysis_prev",
        valid_time=_dt(valid_time),
        state_uri=f"states/demo_model/{_dt(valid_time):%Y%m%d%H}/state.cfg.ic",
        checksum=sha256_bytes(content),
        usable_flag=True,
    )


def _consume_manifest(state: StateSnapshot, *, start_time: str, valid_time: str, quality: str) -> dict[str, Any]:
    return {
        "run_id": "fcst_gfs_2026050200_demo_model",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": "GFS",
        "cycle_time": start_time,
        "start_time": start_time,
        "end_time": "2026-05-03T00:00:00Z",
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
            "valid_time": valid_time,
            "checksum": state.checksum,
            "quality": quality,
        },
        "forcing": {"forcing_uri": "s3://nhms/forcing/gfs/2026050200/basin_v01/demo_model/"},
        "runtime": {"output_interval_minutes": 1440, "init_mode": 3},
        "outputs": {"run_manifest_uri": "s3://nhms/runs/fcst_gfs_2026050200_demo_model/input/manifest.json"},
    }


def test_consume_materializes_canonical_ic_to_project_name(tmp_path: Path) -> None:
    # The warm-start object is canonical state.cfg.ic; SHUD reads <project>.cfg.ic.
    t_next = "2026-05-02T00:00:00Z"
    ic_content = f"2 1 {_minute_time(t_next):.6f}\n1 0.1\n2 0.2\n1 0.0\n".encode()
    state = _ic_state(t_next, ic_content)
    state_manager = FakeStateManager([state])
    runtime, object_root, workspace = _runtime(tmp_path, state_manager)
    (object_root / state.state_uri).parent.mkdir(parents=True, exist_ok=True)
    (object_root / state.state_uri).write_bytes(ic_content)

    manifest = _consume_manifest(state, start_time=t_next, valid_time=t_next, quality="fresh")
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime._stage_initial_state(manifest, input_dir)

    # SHUD reads <project>.cfg.ic; the canonical name has been materialized.
    project_ic = input_dir / "demo.cfg.ic"
    assert project_ic.exists()
    assert manifest["runtime"]["init_mode"] == 3
    # Header is restamped to the run start (== T_{N+1} here).
    assert round(_read_cfg_ic_header_minute(project_ic)) == round(_minute_time(t_next))


def test_consume_blocks_on_native_header_snapshot_mismatch(tmp_path: Path) -> None:
    # Native IC header time disagrees with the recorded snapshot valid_time: blocker.
    snapshot_valid = "2026-05-02T00:00:00Z"
    header_wrong = "2026-04-15T00:00:00Z"
    ic_content = f"2 1 {_minute_time(header_wrong):.6f}\n1 0.1\n2 0.2\n1 0.0\n".encode()
    state = _ic_state(snapshot_valid, ic_content)
    state_manager = FakeStateManager([state])
    runtime, object_root, workspace = _runtime(tmp_path, state_manager)
    (object_root / state.state_uri).parent.mkdir(parents=True, exist_ok=True)
    (object_root / state.state_uri).write_bytes(ic_content)

    manifest = _consume_manifest(state, start_time=snapshot_valid, valid_time=snapshot_valid, quality="fresh")
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(Exception) as excinfo:
        runtime._stage_initial_state(manifest, input_dir)
    assert "WARM_START_TIME_MISMATCH" in str(getattr(excinfo.value, "error_code", "")) or "mismatch" in str(
        excinfo.value
    )


# ---------------------------------------------------------------------------
# Cohort forecast manifest uses the prior cycle's saved state across three faces
# ---------------------------------------------------------------------------


def _cohort_orchestrator(tmp_path: Path, state_manager: FakeStateManager) -> ForecastOrchestrator:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return ForecastOrchestrator(
        config=config,
        repository=FakeOrchestratorRepository(),
        state_manager=state_manager,
        slurm_client=FakeSlurmClient(),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )


def test_cycle_cohort_forecast_manifest_uses_prior_cycle_saved_state(tmp_path: Path) -> None:
    # Cycle N saved a snapshot valid at T_{N+1}; cycle N+1 (init == T_{N+1}) selects it.
    t_next = "2026-05-02T00:00:00Z"
    prior_state = StateSnapshot(
        state_id="state_demo_model_2026050200",
        model_id="demo_model",
        run_id="analysis_cycle_n",
        valid_time=_dt(t_next),
        state_uri="states/demo_model/2026050200/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="GFS",
        cycle_id="GFS_2026050100",
        lead_hours=0,
        model_package_version="models/demo_model/package/",
    )
    orchestrator = _cohort_orchestrator(tmp_path, FakeStateManager([prior_state]))

    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "source_id": "gfs",
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt(t_next))
    orchestrator._apply_cohort_warm_start(basins, "gfs", _dt(t_next))

    # Face 1: scheduler basin record (the basin dict the cohort was handed).
    record = basins[0]
    assert record["init_state_uri"] == prior_state.state_uri
    assert record["init_state_checksum"] == prior_state.checksum
    assert record["init_state_lineage"]["source_id"] == "GFS"
    assert record["init_state_lineage"]["lead_hours"] == 0
    # Not the packaged calibrated state.
    assert record["init_state_quality"] == "fresh"

    # Face 2: forecast runtime manifest reads the same selection.
    from services.orchestrator.chain import CycleOrchestrationContext

    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt(t_next),
        cycle_id="gfs_2026050200",
        run_id="cycle_run",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage=None,
    )
    runtime_manifest = orchestrator._build_forecast_runtime_manifest(context, record)
    assert runtime_manifest["initial_state"]["ic_file_uri"] == prior_state.state_uri
    assert runtime_manifest["initial_state"]["checksum"] == prior_state.checksum
    assert runtime_manifest["initial_state"]["lineage"]["source_id"] == "GFS"
    assert runtime_manifest["runtime"]["init_mode"] == 3

    # Face 3: cycle-stage manifest index entries carry the same selection.
    index_entries = orchestrator._reindexed_manifest_entries(context.active_basins)
    entry = index_entries[0]
    assert entry["init_state_uri"] == prior_state.state_uri
    assert entry["init_state_checksum"] == prior_state.checksum

    # All three faces agree on the single selected state's uri + checksum.
    assert (
        record["init_state_uri"]
        == runtime_manifest["initial_state"]["ic_file_uri"]
        == entry["init_state_uri"]
    )
    assert (
        record["init_state_checksum"]
        == runtime_manifest["initial_state"]["checksum"]
        == entry["init_state_checksum"]
    )
