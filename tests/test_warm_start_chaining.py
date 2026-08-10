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

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_lineage import WARM_START_LINEAGE_MISMATCH
from packages.common.state_manager import StateSnapshot
from services.orchestrator.chain import (
    ForecastOrchestrator,
    OrchestratorConfig,
    OrchestratorError,
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


def _format_time_for_test(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _witness_provenance_for(run_id: str, requested_hours: list[int] | None = None) -> dict[str, Any]:
    """#1325: the writer's provenance block, as the publish-side gate requires it."""

    return {
        "run_id": run_id,
        "generated_at": "2026-05-02T00:00:05Z",
        "slurm_job_id": "990011",
        "array_task_id": 0,
        "requested_checkpoint_hours": list(requested_hours or []),
    }


def _write_witness_manifest(
    output_dir: Path,
    *,
    run_id: str,
    checkpoints: list[dict[str, Any]] | None = None,
    requested_hours: list[int] | None = None,
    final_ic_name: str | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "checkpoints": list(checkpoints or []),
        "provenance": _witness_provenance_for(run_id, requested_hours),
    }
    if final_ic_name is not None:
        payload["final_ic"] = {
            "relative_path": final_ic_name,
            "original_shud_filename": final_ic_name,
            "checksum": sha256_bytes((output_dir / final_ic_name).read_bytes()),
        }
    manifest_dir = output_dir / "state_checkpoints"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "state_checkpoints.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


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


def test_analysis_forcing_causality_marker_is_delayed_reanalysis_for_era5(monkeypatch: Any) -> None:
    # The analysis segment is built from delayed ERA5 reanalysis (the only
    # implementation), so the default marker MUST be delayed_reanalysis with a
    # recorded latency -- never causal (which would over-claim no future leak from a
    # real-time nowcast that does not exist yet).
    from services.orchestrator import chain as chain_module

    monkeypatch.delenv("ERA5_REANALYSIS_LATENCY_MINUTES", raising=False)
    marker = _analysis_forcing_causality()
    assert marker["mode"] == "delayed_reanalysis"
    assert marker["mode"] != "causal"
    assert marker["latency_minutes"] == chain_module.DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES
    assert marker["latency_minutes"] > 0
    assert marker["no_future_leak"] is True

    # Explicit latency is recorded verbatim, still delayed_reanalysis.
    explicit = _analysis_forcing_causality(latency_minutes=720)
    assert explicit == {"mode": "delayed_reanalysis", "latency_minutes": 720, "no_future_leak": True}


def test_analysis_forcing_causality_latency_is_env_overridable(monkeypatch: Any) -> None:
    monkeypatch.setenv("ERA5_REANALYSIS_LATENCY_MINUTES", "4320")
    marker = _analysis_forcing_causality()
    assert marker == {"mode": "delayed_reanalysis", "latency_minutes": 4320, "no_future_leak": True}


def test_future_causal_mode_carries_no_reanalysis_latency() -> None:
    # Semantic guard for the FUTURE real-time causal path: a true causal nowcast has
    # no future leak AND, by construction, no reanalysis latency. ERA5 today cannot
    # satisfy this; this asserts the reserved mode's contract so it is not conflated
    # with delayed_reanalysis.
    from services.orchestrator.chain import FORCING_CAUSALITY_CAUSAL, FORCING_CAUSALITY_DELAYED_REANALYSIS

    assert FORCING_CAUSALITY_CAUSAL == "causal"
    assert FORCING_CAUSALITY_DELAYED_REANALYSIS == "delayed_reanalysis"
    assert FORCING_CAUSALITY_CAUSAL != FORCING_CAUSALITY_DELAYED_REANALYSIS


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
    # minute-time at T_{N+1}, plus the solver-success witness the analysis lane's
    # zero-checkpoint solve writes for it (#1325).
    output_dir = workspace / "runs" / run_id / "output"
    output_dir.mkdir(parents=True)
    ic_update = output_dir / "demo.cfg.ic.update"
    ic_update.write_text(f"2 1 {_minute_time(t_next):.6f}\n1 0.1\n2 0.2\n1 0.0\n", encoding="utf-8")
    _write_witness_manifest(output_dir, run_id=run_id, final_ic_name="demo.cfg.ic.update")

    captured: dict[str, Any] = {}

    class _Repo(PsycopgStateSnapshotRepository):
        def __init__(self) -> None:  # noqa: D401 - test double
            pass

        def get_state_snapshot_by_model_time(
            self,
            *,
            model_id: str,
            valid_time: datetime,
            source_id: str | None = None,
            cycle_id: str | None = None,
            lead_hours: int | None = None,
        ) -> StateSnapshot | None:
            del cycle_id, lead_hours
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


def test_saved_state_finds_restart_from_object_store_output_directory_uri(tmp_path: Path) -> None:
    from packages.common.state_cli import StateRunContext, save_state_for_run
    from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager

    t_next = "2026-05-02T00:00:00Z"
    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    run_id = "fcst_gfs_2026050100_demo_model"
    output_dir = object_root / "runs" / run_id / "output"
    output_dir.mkdir(parents=True)
    ic_update = output_dir / "demo.cfg.ic.update"
    ic_update.write_text(f"2 1 {_minute_time(t_next):.6f}\n1 0.1\n2 0.2\n1 0.0\n", encoding="utf-8")
    _write_witness_manifest(output_dir, run_id=run_id, final_ic_name="demo.cfg.ic.update")

    captured: dict[str, Any] = {}

    class _Repo(PsycopgStateSnapshotRepository):
        def __init__(self) -> None:  # noqa: D401 - test double
            pass

        def get_state_snapshot_by_model_time(
            self,
            *,
            model_id: str,
            valid_time: datetime,
            source_id: str | None = None,
            cycle_id: str | None = None,
            lead_hours: int | None = None,
        ) -> StateSnapshot | None:
            del cycle_id, lead_hours
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
                output_uri=f"s3://nhms/runs/{run_id}/output/",
            )

    manager = StateManager(repository=_Repo(), object_store=LocalObjectStore(object_root, "s3://nhms"))

    result = save_state_for_run(
        run_id,
        manager=manager,
        repository=_RunRepo(),
        workspace_root=workspace,
    )

    assert captured["snapshot"].valid_time == _dt(t_next)
    assert captured["snapshot"].original_shud_filename == "demo.cfg.ic.update"
    assert result["valid_time"] == t_next


def test_saved_state_persists_long_run_checkpoints_at_each_valid_time(tmp_path: Path) -> None:
    from packages.common.state_cli import StateRunContext, save_state_for_run
    from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager
    from workers.shud_runtime.runtime import _read_cfg_ic_header_minute

    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    run_id = "fcst_gfs_2026050100_demo_model"
    output_dir = workspace / "runs" / run_id / "output" / "state_checkpoints"
    output_dir.mkdir(parents=True)
    t6 = _dt("2026-05-01T06:00:00Z")
    t12 = _dt("2026-05-01T12:00:00Z")
    f006 = output_dir / "demo.f006.cfg.ic.update"
    f012 = output_dir / "demo.f012.cfg.ic.update"
    f006.write_text(f"2 1 {_minute_time('2026-05-01T06:00:00Z'):.6f}\n1 0.1\n2 0.2\n1 0.0\n", encoding="utf-8")
    f012.write_text(f"2 1 {_minute_time('2026-05-01T12:00:00Z'):.6f}\n1 0.3\n2 0.4\n1 0.0\n", encoding="utf-8")
    _write_witness_manifest(
        output_dir.parent,
        run_id=run_id,
        requested_hours=[6, 12],
        checkpoints=[
            {
                "lead_hours": 6,
                "valid_time": _format_time_for_test(t6),
                "relative_path": "state_checkpoints/demo.f006.cfg.ic.update",
                "checkpoint_filename": "demo.f006.cfg.ic.update",
                "checksum": sha256_bytes(f006.read_bytes()),
            },
            {
                "lead_hours": 12,
                "valid_time": _format_time_for_test(t12),
                "relative_path": "state_checkpoints/demo.f012.cfg.ic.update",
                "checkpoint_filename": "demo.f012.cfg.ic.update",
                "checksum": sha256_bytes(f012.read_bytes()),
            },
        ],
    )

    captured: dict[str, Any] = {"snapshots": []}

    class _Repo(PsycopgStateSnapshotRepository):
        def __init__(self) -> None:
            pass

        def get_state_snapshot_by_model_time(
            self,
            *,
            model_id: str,
            valid_time: datetime,
            source_id: str | None = None,
            cycle_id: str | None = None,
            lead_hours: int | None = None,
        ) -> StateSnapshot | None:
            del model_id, valid_time, cycle_id, lead_hours
            return None

        def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
            captured["snapshots"].append(snapshot)
            return snapshot

        def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
            return next((item for item in captured["snapshots"] if item.state_id == state_id), None)

        def insert_qc_result(self, record: Any) -> dict[str, Any]:
            captured.setdefault("qc_records", []).append(record)
            return {}

        def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
            del usable_flag
            return self.get_state_snapshot(state_id)

    class _RunRepo:
        def load_run_context(self, _run_id: str) -> StateRunContext:
            return StateRunContext(
                run_id=run_id,
                model_id="demo_model",
                end_time=_dt("2026-05-08T00:00:00Z"),
                output_uri=None,
                source_id="GFS",
                cycle_time=_dt("2026-05-01T00:00:00Z"),
                model_package_version="s3://nhms/models/demo_model/package/",
                model_package_checksum="package-sha-1",
            )

    manager = StateManager(repository=_Repo(), object_store=LocalObjectStore(object_root, ""))

    result = save_state_for_run(run_id, manager=manager, repository=_RunRepo(), workspace_root=workspace)

    snapshots = captured["snapshots"]
    assert [snapshot.valid_time for snapshot in snapshots] == [t6, t12]
    assert [snapshot.original_shud_filename for snapshot in snapshots] == [
        "demo.f006.cfg.ic.update",
        "demo.f012.cfg.ic.update",
    ]
    assert [snapshot.source_id for snapshot in snapshots] == ["GFS", "GFS"]
    assert [snapshot.cycle_id for snapshot in snapshots] == ["gfs_2026050100", "gfs_2026050100"]
    assert [snapshot.lead_hours for snapshot in snapshots] == [6, 12]
    assert [snapshot.model_package_version for snapshot in snapshots] == [
        "s3://nhms/models/demo_model/package/",
        "s3://nhms/models/demo_model/package/",
    ]
    assert [snapshot.model_package_checksum for snapshot in snapshots] == ["package-sha-1", "package-sha-1"]
    assert [item["valid_time"] for item in result["checkpoints"]] == [
        "2026-05-01T06:00:00Z",
        "2026-05-01T12:00:00Z",
    ]
    assert [item["lead_hours"] for item in result["checkpoints"]] == [6, 12]
    assert result["state_uri"].endswith("state.cfg.ic")
    saved_t6 = (
        object_root
        / "states"
        / "GFS"
        / "demo_model"
        / "2026050106"
        / "gfs_2026050100"
        / "f006"
        / "state.cfg.ic"
    )
    saved_t12 = (
        object_root
        / "states"
        / "GFS"
        / "demo_model"
        / "2026050112"
        / "gfs_2026050100"
        / "f012"
        / "state.cfg.ic"
    )
    assert round(_read_cfg_ic_header_minute(saved_t6)) == round(_minute_time("2026-05-01T06:00:00Z"))
    assert round(_read_cfg_ic_header_minute(saved_t12)) == round(_minute_time("2026-05-01T12:00:00Z"))


def test_state_save_rekeys_checkpoint_when_manifest_lagged_header_minute(tmp_path: Path) -> None:
    from packages.common.state_cli import StateRunContext, save_state_for_run
    from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager

    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    run_id = "fcst_gfs_2026050100_demo_model"
    output_dir = workspace / "runs" / run_id / "output" / "state_checkpoints"
    output_dir.mkdir(parents=True)
    f006 = output_dir / "demo.f006.cfg.ic.update"
    f006.write_text("2 1 720.000000\n1 0.3\n2 0.4\n1 0.0\n", encoding="utf-8")
    _write_witness_manifest(
        output_dir.parent,
        run_id=run_id,
        requested_hours=[6],
        checkpoints=[
            {
                "lead_hours": 6,
                "valid_time": "2026-05-01T06:00:00Z",
                "relative_path": "state_checkpoints/demo.f006.cfg.ic.update",
                "checkpoint_filename": "demo.f006.cfg.ic.update",
                "checksum": sha256_bytes(f006.read_bytes()),
            }
        ],
    )
    captured: dict[str, Any] = {"snapshots": []}

    class _Repo(PsycopgStateSnapshotRepository):
        def __init__(self) -> None:
            pass

        def get_state_snapshot_by_model_time(
            self,
            *,
            model_id: str,
            valid_time: datetime,
            source_id: str | None = None,
            cycle_id: str | None = None,
            lead_hours: int | None = None,
        ) -> StateSnapshot | None:
            del model_id, valid_time, source_id, cycle_id, lead_hours
            return None

        def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
            captured["snapshots"].append(snapshot)
            return snapshot

        def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
            return next((item for item in captured["snapshots"] if item.state_id == state_id), None)

        def insert_qc_result(self, record: Any) -> dict[str, Any]:
            captured.setdefault("qc_records", []).append(record)
            return {}

        def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
            del usable_flag
            return self.get_state_snapshot(state_id)

    class _RunRepo:
        def load_run_context(self, _run_id: str) -> StateRunContext:
            return StateRunContext(
                run_id=run_id,
                model_id="demo_model",
                end_time=_dt("2026-05-08T00:00:00Z"),
                output_uri=None,
                source_id="GFS",
                cycle_time=_dt("2026-05-01T00:00:00Z"),
                model_package_version="s3://nhms/models/demo_model/package/",
                model_package_checksum="package-sha-1",
            )

    manager = StateManager(repository=_Repo(), object_store=LocalObjectStore(object_root, ""))

    result = save_state_for_run(run_id, manager=manager, repository=_RunRepo(), workspace_root=workspace)

    snapshots = captured["snapshots"]
    assert [snapshot.valid_time for snapshot in snapshots] == [_dt("2026-05-01T12:00:00Z")]
    assert [snapshot.lead_hours for snapshot in snapshots] == [12]
    assert [item["valid_time"] for item in result["checkpoints"]] == ["2026-05-01T12:00:00Z"]
    assert [item["lead_hours"] for item in result["checkpoints"]] == [12]
    saved_t12 = (
        object_root
        / "states"
        / "GFS"
        / "demo_model"
        / "2026050112"
        / "gfs_2026050100"
        / "f012"
        / "state.cfg.ic"
    )
    assert round(_read_cfg_ic_header_minute(saved_t12)) == round(_minute_time("2026-05-01T12:00:00Z"))


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


def test_consume_projects_bounded_unsat_residual_and_records_evidence(tmp_path: Path) -> None:
    t_next = "2026-05-02T00:00:00Z"
    rows = [f"{index + 1}\t0\t0\t0\t{(-0.014834 if index == 73 else 0.1):.6f}\t0" for index in range(100)]
    ic_content = (
        f"100\t1\t{_minute_time(t_next):.6f}\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        + "\n".join(rows)
        + "\nIndex\tStage\n1\t0\n"
    ).encode()
    state = _ic_state(t_next, ic_content)
    state_manager = FakeStateManager([state])
    runtime, object_root, workspace = _runtime(tmp_path, state_manager)
    (object_root / state.state_uri).parent.mkdir(parents=True, exist_ok=True)
    (object_root / state.state_uri).write_bytes(ic_content)
    manifest = _consume_manifest(state, start_time=t_next, valid_time=t_next, quality="fresh")
    manifest["runtime"]["warm_start_policy"] = "exact_required"
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime._stage_initial_state(manifest, input_dir)

    project_ic = input_dir / "demo.cfg.ic"
    assert project_ic.read_text(encoding="utf-8").splitlines()[75].split()[4] == "0"
    evidence = manifest["runtime"]["initial_state_normalization"]
    assert evidence["normalized_unsat_row_count"] == 1
    assert evidence["max_unsat_correction_m"] == pytest.approx(0.014834)


def test_exact_warm_state_unavailable_never_falls_back_to_cold_or_older_state(tmp_path: Path) -> None:
    t_next = "2026-05-02T00:00:00Z"
    ic_content = f"2 1 {_minute_time(t_next):.6f}\n1 0.1\n2 0.2\n1 0.0\n".encode()
    state = _ic_state(t_next, ic_content)
    state_manager = FakeStateManager([state])
    runtime, _object_root, workspace = _runtime(tmp_path, state_manager)
    manifest = _consume_manifest(state, start_time=t_next, valid_time=t_next, quality="fresh")
    manifest["runtime"]["warm_start_policy"] = "exact_required"
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(Exception) as excinfo:
        runtime._stage_initial_state(manifest, input_dir)

    assert getattr(excinfo.value, "error_code", None) == "WARM_START_UNAVAILABLE"
    assert manifest["initial_state"]["state_id"] == state.state_id
    assert manifest["initial_state"]["quality"] == "fresh"
    assert manifest["runtime"]["init_mode"] == 3


def test_consume_warm_continuity_blocks_on_three_way_run_start_mismatch(tmp_path: Path) -> None:
    # PRODUCTION-PATH three-way enforcement: the snapshot is consumed as the exact
    # successor (valid_time == run start_time == T_{N+1}, quality=fresh), but the
    # native .cfg.ic header minute-time disagrees with both. Warm-continuity demands
    # snapshot valid_time == header == run start; the mismatch must be a recorded
    # WARM_START_TIME_MISMATCH blocker on the production consume path, not a silent
    # restart. (Reverting the production wiring of _check_three_way_time_consistency
    # makes this test red.)
    t_next = "2026-05-02T00:00:00Z"
    header_wrong = "2026-05-02T06:00:00Z"  # header off by 6h from snapshot/run-start
    ic_content = f"2 1 {_minute_time(header_wrong):.6f}\n1 0.1\n2 0.2\n1 0.0\n".encode()
    state = _ic_state(t_next, ic_content)
    state_manager = FakeStateManager([state])
    runtime, object_root, workspace = _runtime(tmp_path, state_manager)
    (object_root / state.state_uri).parent.mkdir(parents=True, exist_ok=True)
    (object_root / state.state_uri).write_bytes(ic_content)

    # valid_time == start_time == T_{N+1} -> warm-continuity (exact successor).
    manifest = _consume_manifest(state, start_time=t_next, valid_time=t_next, quality="fresh")
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(Exception) as excinfo:
        runtime._stage_initial_state(manifest, input_dir)
    assert "WARM_START_TIME_MISMATCH" in str(getattr(excinfo.value, "error_code", "")) or "mismatch" in str(
        excinfo.value
    )


def test_consume_degraded_reuse_of_older_state_is_not_three_way_blocked(tmp_path: Path) -> None:
    # Legitimate degraded/stale reuse: an OLDER state (valid_time < run start_time) is
    # reused; its native header equals its own valid_time but NOT the run start (which
    # is intentionally re-stamped downstream). The three-way run-start leg must NOT be
    # forced here -- this must succeed, not be a false WARM_START_TIME_MISMATCH.
    snapshot_valid = "2026-04-25T00:00:00Z"  # older than run start
    run_start = "2026-05-02T00:00:00Z"
    # Native header agrees with the snapshot's own valid_time (it is the state it claims).
    ic_content = f"2 1 {_minute_time(snapshot_valid):.6f}\n1 0.1\n2 0.2\n1 0.0\n".encode()
    state = _ic_state(snapshot_valid, ic_content)
    state_manager = FakeStateManager([state])
    runtime, object_root, workspace = _runtime(tmp_path, state_manager)
    (object_root / state.state_uri).parent.mkdir(parents=True, exist_ok=True)
    (object_root / state.state_uri).write_bytes(ic_content)

    manifest = _consume_manifest(
        state, start_time=run_start, valid_time=snapshot_valid, quality="degraded_stale_init_state"
    )
    input_dir = workspace / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    # No raise: degraded reuse of an older state is allowed; header re-stamped to run start.
    runtime._stage_initial_state(manifest, input_dir)
    project_ic = input_dir / "demo.cfg.ic"
    assert project_ic.exists()
    assert round(_read_cfg_ic_header_minute(project_ic)) == round(_minute_time(run_start))


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


def _cohort_orchestrator(
    tmp_path: Path,
    state_manager: FakeStateManager,
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
        repository=FakeOrchestratorRepository(),
        state_manager=state_manager,
        slurm_client=FakeSlurmClient(),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )


class LineageAwareFakeStateManager(FakeStateManager):
    def __init__(self, snapshots: list[StateSnapshot]) -> None:
        super().__init__(snapshots)
        self.exact_queries: list[dict[str, Any]] = []

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        parsed_valid_time = valid_time if valid_time.tzinfo is not None else valid_time.replace(tzinfo=UTC)
        self.exact_queries.append(
            {
                "model_id": model_id,
                "valid_time": parsed_valid_time,
                "source_id": source_id,
                "cycle_id": cycle_id,
                "lead_hours": lead_hours,
            }
        )
        for snapshot in self.snapshots.values():
            if snapshot.model_id != model_id or snapshot.valid_time != parsed_valid_time:
                continue
            if source_id is not None and snapshot.source_id != source_id:
                continue
            if cycle_id not in (None, "") and snapshot.cycle_id != cycle_id:
                continue
            if lead_hours is not None and snapshot.lead_hours != lead_hours:
                continue
            return snapshot
        return None


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


def test_strict_new_model_first_cycle_is_cold_seed_but_not_marked_exact_warm(tmp_path: Path) -> None:
    from services.orchestrator.chain import CycleOrchestrationContext

    cycle_time = _dt("2026-05-02T00:00:00Z")
    orchestrator = _cohort_orchestrator(
        tmp_path,
        FakeStateManager([]),
        require_forecast_warm_start=True,
    )
    basin = {
        "model_id": "new_model",
        "basin_id": "new_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/new_model/package/",
        "source_id": "gfs",
        "state_evidence": {
            "decision": "retry_strict_warm_start_terminal_init_state_mismatch",
            "strict_warm_start": {
                "mode": "db_free_cold_new_model",
                "ready": True,
                "cold_start_reason": "no_prior_history",
            },
        },
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", cycle_time)

    orchestrator._apply_cohort_warm_start(basins, "gfs", cycle_time)

    record = basins[0]
    assert record["init_state_id"] is None
    assert record["init_state_quality"] == "cold_start_no_state"
    assert record["cold_start_reason"] == "no_prior_history"
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id="gfs_2026050200",
        run_id="cycle_run",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage=None,
    )
    runtime_manifest = orchestrator._build_forecast_runtime_manifest(context, record)
    assert runtime_manifest["runtime"]["init_mode"] == 1
    assert "warm_start_policy" not in runtime_manifest["runtime"]


def test_strict_cycle_prefilled_exact_successor_is_validated_and_preserved(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    prior_state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    orchestrator = _cohort_orchestrator(
        tmp_path,
        FakeStateManager([prior_state]),
        require_forecast_warm_start=True,
    )

    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "model_package_checksum": "package-sha",
        "source_id": "gfs",
        **_prefilled_state_fields(prior_state, t_next),
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt(t_next))
    orchestrator._apply_cohort_warm_start(basins, "gfs", _dt(t_next))

    record = basins[0]
    assert record["init_state_id"] == prior_state.state_id
    assert record["init_state_uri"] == prior_state.state_uri
    assert record["init_state_checksum"] == prior_state.checksum
    assert record["init_state_valid_time"] == t_next
    assert record["init_state_quality"] == "fresh"
    assert record["init_state_lineage"] == {
        "source_id": "gfs",
        "cycle_id": "gfs_2026050100",
        "lead_hours": 12,
        "model_package_version": "models/demo_model/package/",
        "model_package_checksum": "package-sha",
    }

    from services.orchestrator.chain import CycleOrchestrationContext

    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_dt(t_next),
        cycle_id="gfs_2026050112",
        run_id="cycle_run",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage=None,
    )
    runtime_manifest = orchestrator._build_forecast_runtime_manifest(context, record)
    entry = orchestrator._reindexed_manifest_entries(context.active_basins)[0]

    assert runtime_manifest["initial_state"]["state_id"] == prior_state.state_id
    assert runtime_manifest["initial_state"]["valid_time"] == t_next
    assert runtime_manifest["initial_state"]["lineage"]["lead_hours"] == 12
    assert runtime_manifest["runtime"]["init_mode"] == 3
    assert runtime_manifest["initial_state"]["quality"] != "cold_start_no_state"
    assert entry["init_state_uri"] == prior_state.state_uri
    assert entry["init_state_checksum"] == prior_state.checksum


def test_strict_cycle_prefilled_required_f006_succeeds_through_chain_warm_start(tmp_path: Path) -> None:
    t_next = "2026-05-01T06:00:00Z"
    stale_f012 = StateSnapshot(
        state_id="state_demo_model_2026050106_gfs_2026043018_f012",
        model_id="demo_model",
        run_id="fcst_gfs_2026043018_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050106/gfs_2026043018/f012/state.cfg.ic",
        checksum="csum-stale-f012",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026043018",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    selected_f006 = StateSnapshot(
        state_id="state_demo_model_2026050106_gfs_2026050100_f006",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050106/gfs_2026050100/f006/state.cfg.ic",
        checksum="csum-selected-f006",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=6,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    state_manager = LineageAwareFakeStateManager([stale_f012, selected_f006])
    orchestrator = _cohort_orchestrator(
        tmp_path,
        state_manager,
        require_forecast_warm_start=True,
    )
    prefilled = _prefilled_state_fields(
        selected_f006,
        t_next,
        cycle_id="gfs_2026050100",
        lead_hours=6,
    )
    prefilled.pop("init_state_id")
    basin = {
        **_strict_basin(model_package_checksum="package-sha"),
        **prefilled,
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt(t_next))

    orchestrator._apply_cohort_warm_start(basins, "gfs", _dt(t_next))

    record = basins[0]
    assert state_manager.exact_queries[0]["cycle_id"] == "gfs_2026050100"
    assert state_manager.exact_queries[0]["lead_hours"] == 6
    assert record["init_state_id"] == selected_f006.state_id
    assert record["init_state_uri"] == selected_f006.state_uri
    assert record["init_state_lineage"]["cycle_id"] == "gfs_2026050100"
    assert record["init_state_lineage"]["lead_hours"] == 6


@pytest.mark.parametrize(
    ("state_cycle_id", "state_lead_hours"),
    [
        ("gfs_2026043018", 12),
        ("gfs_2026043018", 6),
    ],
)
def test_strict_cycle_prefilled_required_f006_rejects_stale_or_wrong_cycle_same_valid_time(
    tmp_path: Path,
    state_cycle_id: str,
    state_lead_hours: int,
) -> None:
    t_next = "2026-05-01T06:00:00Z"
    state = StateSnapshot(
        state_id=f"state_demo_model_2026050106_{state_cycle_id}_f{state_lead_hours:03d}",
        model_id="demo_model",
        run_id=f"fcst_{state_cycle_id}_demo_model",
        valid_time=_dt(t_next),
        state_uri=f"states/gfs/demo_model/2026050106/{state_cycle_id}/f{state_lead_hours:03d}/state.cfg.ic",
        checksum="csum-wrong-lineage",
        usable_flag=True,
        source_id="gfs",
        cycle_id=state_cycle_id,
        lead_hours=state_lead_hours,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=LineageAwareFakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = {
        **_strict_basin(model_package_checksum="package-sha"),
        **_prefilled_state_fields(
            state,
            t_next,
            cycle_id="gfs_2026050100",
            lead_hours=6,
        ),
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client, run_id="fcst_gfs_2026050106_demo_model")


@pytest.mark.parametrize(
    ("state_checksum", "prefilled_checksum"),
    [
        ("sha256:package-sha", "package-sha"),
        ("package-sha", "sha256:package-sha"),
    ],
)
def test_strict_cycle_prefilled_package_checksum_alias_is_validated_and_preserved(
    tmp_path: Path,
    state_checksum: str,
    prefilled_checksum: str,
) -> None:
    t_next = "2026-05-01T12:00:00Z"
    prior_state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum=state_checksum,
    )
    orchestrator = _cohort_orchestrator(
        tmp_path,
        FakeStateManager([prior_state]),
        require_forecast_warm_start=True,
    )
    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "model_package_checksum": "package-sha",
        "source_id": "gfs",
        "init_state_id": prior_state.state_id,
        "init_state_uri": prior_state.state_uri,
        "init_state_checksum": prior_state.checksum,
        "init_state_valid_time": t_next,
        "init_state_lineage": {
            "source_id": "gfs",
            "cycle_id": "gfs_2026050100",
            "lead_hours": 12,
            "model_package_version": "models/demo_model/package/",
            "model_package_checksum": prefilled_checksum,
        },
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt(t_next))

    orchestrator._apply_cohort_warm_start(basins, "gfs", _dt(t_next))

    assert basins[0]["init_state_id"] == prior_state.state_id
    assert basins[0]["init_state_lineage"]["model_package_checksum"] == state_checksum


def test_strict_cycle_prefilled_invalid_state_blocks_before_side_effects(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    invalid_state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050106_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050106",
        lead_hours=6,
        model_package_version="models/demo_model/package/",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([invalid_state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )

    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "source_id": "gfs",
        **_prefilled_state_fields(
            invalid_state,
            t_next,
            cycle_id="gfs_2026050100",
            lead_hours=12,
        ),
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_non_strict_cycle_preserves_prefilled_initial_state(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    exact_state = StateSnapshot(
        state_id="state_selected_by_latest_usable",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/selected/state.cfg.ic",
        checksum="selected-csum",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
    )
    orchestrator = _cohort_orchestrator(tmp_path, FakeStateManager([exact_state]))
    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "source_id": "gfs",
        "init_state_id": "caller_state",
        "init_state_uri": "states/gfs/demo_model/caller/state.cfg.ic",
        "init_state_checksum": "caller-csum",
        "init_state_valid_time": t_next,
        "init_state_quality": "fresh",
        "init_state_lineage": {"source_id": "gfs", "lead_hours": 12},
    }
    basins = orchestrator._normalize_cycle_basins([basin], "gfs", _dt(t_next))

    orchestrator._apply_cohort_warm_start(basins, "gfs", _dt(t_next))

    record = basins[0]
    assert record["init_state_id"] == "caller_state"
    assert record["init_state_uri"] == "states/gfs/demo_model/caller/state.cfg.ic"
    assert record["init_state_checksum"] == "caller-csum"
    assert record["init_state_lineage"] == {"source_id": "gfs", "lead_hours": 12}


@pytest.mark.parametrize(
    ("mutation", "qc_failure", "expected_code"),
    [
        (lambda state: replace(state, usable_flag=False), False, "warm_start_successor_checkpoint_unusable"),
        (lambda state: state, True, "warm_start_successor_checkpoint_unusable"),
        (lambda state: replace(state, source_id="IFS"), False, "warm_start_lineage_mismatch"),
        (
            lambda state: replace(state, model_package_version="models/demo_model/old-package/"),
            False,
            "warm_start_lineage_mismatch",
        ),
        (lambda state: replace(state, model_package_checksum="old-package-sha"), False, "warm_start_lineage_mismatch"),
        (lambda state: replace(state, lead_hours=6), False, "warm_start_lineage_mismatch"),
    ],
)
def test_strict_cycle_invalid_successor_blocks_before_side_effects(
    tmp_path: Path,
    mutation: Any,
    qc_failure: bool,
    expected_code: str,
) -> None:
    t_next = "2026-05-01T12:00:00Z"
    base_state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    state = mutation(base_state)
    qc_failures = {state.state_id} if qc_failure else set()
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state], qc_failures=qc_failures),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = _strict_basin(package_checksum="package-sha")
    basin.update(
        _prefilled_state_fields(
            state,
            t_next,
            cycle_id="gfs_2026050100",
            lead_hours=12,
        )
    )

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == expected_code
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_strict_cycle_malformed_persisted_source_blocks_before_side_effects(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="UNKNOWN",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = _strict_basin(package_checksum="package-sha")
    basin.update(_prefilled_state_fields(state, t_next))

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_strict_cycle_prefilled_uri_only_mismatch_blocks_before_side_effects(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = {
        **_strict_basin(model_package_checksum="package-sha"),
        **_prefilled_state_fields(state, t_next),
        "init_state_uri": "states/gfs/demo_model/wrong/state.cfg.ic",
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_strict_cycle_raw_package_checksum_alias_mismatch_blocks_before_side_effects(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="old-package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = _strict_basin(package_checksum="package-sha")

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_strict_cycle_missing_target_checksum_blocks_when_state_has_checksum(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = _strict_basin()

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def test_strict_cycle_missing_target_and_state_checksum_blocks_before_side_effects(tmp_path: Path) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = _strict_basin()

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


@pytest.mark.parametrize(
    "prefilled_update",
    [
        {"init_state_valid_time": "not-a-time"},
        {"init_state_lineage": ["not", "a", "mapping"]},
        {"init_state_lineage": {}},
        {
            "init_state_lineage": {
                "source_id": "gfs",
                "cycle_id": "gfs_2026050100",
                "model_package_checksum": "package-sha",
            }
        },
        {"init_state_lineage": {"lead_hours": "twelve"}},
    ],
)
def test_strict_cycle_malformed_prefilled_metadata_blocks_before_side_effects(
    tmp_path: Path,
    prefilled_update: dict[str, Any],
) -> None:
    t_next = "2026-05-01T12:00:00Z"
    state = StateSnapshot(
        state_id="state_demo_model_2026050112",
        model_id="demo_model",
        run_id="fcst_gfs_2026050100_demo_model",
        valid_time=_dt(t_next),
        state_uri="states/gfs/demo_model/2026050112/state.cfg.ic",
        checksum="csum-next",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026050100",
        lead_hours=12,
        model_package_version="models/demo_model/package/",
        model_package_checksum="package-sha",
    )
    repository = FakeOrchestratorRepository()
    client = FakeSlurmClient()
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
            require_forecast_warm_start=True,
        ),
        repository=repository,
        state_manager=FakeStateManager([state]),
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    basin = {
        **_strict_basin(model_package_checksum="package-sha"),
        "init_state_id": state.state_id,
        "init_state_uri": state.state_uri,
        "init_state_checksum": state.checksum,
        "init_state_valid_time": t_next,
        "init_state_lineage": {
            "source_id": "gfs",
            "cycle_id": "gfs_2026050100",
            "lead_hours": 12,
            "model_package_version": "models/demo_model/package/",
            "model_package_checksum": "package-sha",
        },
        **prefilled_update,
    }

    with pytest.raises(OrchestratorError) as exc_info:
        orchestrator.orchestrate_cycle("gfs", _dt(t_next), [basin])

    assert exc_info.value.error_code == WARM_START_LINEAGE_MISMATCH
    _assert_no_cycle_mutation(tmp_path, repository, client)


def _strict_basin(
    *,
    package_checksum: str | None = None,
    model_package_checksum: str | None = None,
) -> dict[str, Any]:
    basin = {
        "model_id": "demo_model",
        "basin_id": "demo_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/demo_model/package/",
        "source_id": "gfs",
    }
    if package_checksum is not None:
        basin["package_checksum"] = package_checksum
    if model_package_checksum is not None:
        basin["model_package_checksum"] = model_package_checksum
    return basin


def _prefilled_state_fields(
    state: StateSnapshot,
    valid_time: str,
    *,
    source_id: str = "gfs",
    cycle_id: str | None = None,
    lead_hours: int | None = None,
    model_package_version: str | None = "models/demo_model/package/",
    model_package_checksum: str = "package-sha",
) -> dict[str, Any]:
    lineage = {
        "source_id": source_id,
        "cycle_id": cycle_id if cycle_id is not None else state.cycle_id,
        "lead_hours": lead_hours if lead_hours is not None else state.lead_hours,
        "model_package_version": model_package_version,
        "model_package_checksum": model_package_checksum,
    }
    return {
        "init_state_id": state.state_id,
        "init_state_uri": state.state_uri,
        "init_state_checksum": state.checksum,
        "init_state_valid_time": valid_time,
        "init_state_lineage": lineage,
    }


def _assert_no_cycle_mutation(
    tmp_path: Path,
    repository: FakeOrchestratorRepository,
    client: FakeSlurmClient,
    *,
    run_id: str = "fcst_gfs_2026050112_demo_model",
) -> None:
    assert repository.created_runs == []
    assert repository.hydro_statuses == []
    assert repository.cycle_statuses == []
    assert client.submissions == []
    assert not (tmp_path / "workspace" / "runs").exists()
    assert not (tmp_path / "object-store" / "runs" / run_id / "input" / "manifest.json").exists()


def test_budget_blocked_strict_warm_start_decision_is_outside_both_force_whitelists() -> None:
    """tasks 2.8 -- the demoted decision must not inherit forced resubmission.

    Both whitelists match the ``state_evidence["decision"]`` string literally, so
    the new blocked decision drops out of them by construction. Pin the member
    sets so a later "just add it to the list" edit is a red test, not a silent
    duplicate submission.
    """

    from types import SimpleNamespace

    from services.orchestrator import chain_runtime_utils
    from services.orchestrator.chain_forecast_orchestrator_cycle import (
        _FORCE_TERMINAL_RESUBMIT_DECISIONS,
        _terminal_stage_needs_forced_resubmit,
    )

    assert _FORCE_TERMINAL_RESUBMIT_DECISIONS == {
        "retry_repair_missing_forcing",
        "retry_missing_forecast_output",
        "retry_strict_warm_start_terminal_init_state_mismatch",
        "retry_strict_warm_start_terminal_run_manifest_missing",
        "retry_strict_warm_start_retry_run_manifest_mismatch",
        "retry_terminal_run_manifest_missing",
    }

    def basin(decision: str) -> dict[str, Any]:
        return {
            "model_id": "demo_model",
            "basin_id": "demo_basin",
            "candidate_id": "GFS:2026-05-01T12:00:00Z:demo_model:forecast_gfs_deterministic",
            "orchestration_run_id": "cycle_gfs_2026050112",
            "state_evidence": {
                "decision": decision,
                "restart_stage": "forecast",
                "restart_from_stage": "forecast",
            },
        }

    retry_decision = "retry_strict_warm_start_terminal_init_state_mismatch"
    blocked_decision = "blocked_strict_warm_start_init_state_mismatch"

    assert chain_runtime_utils._replacement_retry_scoped_cycle_execution([basin(retry_decision)]) is True
    assert chain_runtime_utils._replacement_retry_scoped_cycle_execution([basin(blocked_decision)]) is False

    terminal_job = {
        "job_id": "job_cycle_gfs_2026050112_forecast",
        "status": "succeeded",
        "stage": "forecast",
        "job_type": "run_shud_forecast_array",
    }
    assert (
        _terminal_stage_needs_forced_resubmit(
            SimpleNamespace(active_basins=[basin(retry_decision)], restart_stage="forecast"),
            terminal_job,
        )
        is True
    )
    assert (
        _terminal_stage_needs_forced_resubmit(
            SimpleNamespace(active_basins=[basin(blocked_decision)], restart_stage="forecast"),
            terminal_job,
        )
        is False
    )


def _released_identity_blocked_master(
    *,
    job_id: str,
    run_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """The row `identity_mismatch_released` leaves behind: terminal and unbound."""

    return {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": "gfs_2026050100",
        "job_type": "run_shud_forecast_array",
        "model_id": "model_0",
        "stage": "forecast",
        "status": "reservation_lost",
        "slurm_job_id": None,
        "idempotency_key": idempotency_key,
        "submit_outcome": "submit_result_ambiguous",
        "reconciliation_source": "slurm_exact_comment",
        "reconciliation_decision": "identity_mismatch_released",
        "matched_slurm_job_id": None,
        "identity_blocked_streak": 3,
        "retry_count": 0,
        "submitted_at": "2026-05-01T00:00:00Z",
        "error_code": "SLURM_RESERVATION_LOST",
    }


@pytest.mark.parametrize("with_higher_attempt_retry_row", [True, False])
def test_released_reservation_reenters_only_through_a_higher_attempt_retry_row(
    tmp_path: Path,
    with_higher_attempt_retry_row: bool,
) -> None:
    """Runbook precondition oracle for manual re-entry after `identity_mismatch_released`.

    ``_terminal_stage_needs_manual_retry`` short-circuits on a released
    (``reservation_lost`` + unbound) master and answers with the reclaim shortcut,
    which is ``False`` for that decision. So raising the retry budget only re-opens
    the ladder when ``_stage_job_sort_key`` picks a DIFFERENT row for the stage:
    a higher-attempt ``*_forecast_retry_<N>`` row (the real ``2026072000`` geometry).
    In a flat geometry that holds only the released row, the chain resumes the
    terminal and submits nothing.
    """

    from tests.test_orchestration_chain import (
        FakeCycleRepository,
        FakeCycleSlurmClient,
        _basins,
        _orchestrator,
    )

    run_id = "cycle_gfs_2026050100_forecast_model_0"
    base_job_id = f"job_{run_id}_forecast"
    repository = FakeCycleRepository()
    repository.jobs[base_job_id] = _released_identity_blocked_master(
        job_id=base_job_id,
        run_id=run_id,
        idempotency_key=f"{run_id}:forecast",
    )
    if with_higher_attempt_retry_row:
        repository.jobs[f"{base_job_id}_retry_2"] = {
            "job_id": f"{base_job_id}_retry_2",
            "run_id": run_id,
            "cycle_id": "gfs_2026050100",
            "job_type": "run_shud_forecast_array",
            "model_id": "model_0",
            "stage": "forecast",
            "status": "failed",
            "slurm_job_id": "7002",
            "idempotency_key": f"{run_id}:forecast:retry_2",
            "retry_count": 2,
            "submitted_at": "2026-05-01T01:00:00Z",
            "finished_at": "2026-05-01T01:30:00Z",
            "error_code": "SLURM_JOB_TIMEOUT",
        }

    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    basins = _basins(1)
    basins[0]["orchestration_run_id"] = run_id
    basins[0]["restart_stage"] = "forecast"
    basins[0]["state_evidence"] = {
        # The budget was raised above the recorded attempt, so the candidate ladder
        # emits the retry decision again instead of the blocked one.
        "decision": "retry_strict_warm_start_terminal_init_state_mismatch",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
    }

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    forecast_submissions = [
        submission for submission in client.submissions if submission.get("stage") == "forecast"
    ]
    if with_higher_attempt_retry_row:
        retry_job_id = f"{base_job_id}_retry_3"
        assert result.status == "complete"
        assert retry_job_id in repository.jobs
        assert repository.jobs[retry_job_id]["idempotency_key"] == f"{run_id}:forecast:retry_3"
        assert repository.jobs[retry_job_id]["status"] == "succeeded"
        assert [stage.pipeline_job_id for stage in result.stages if stage.stage == "forecast"] == [
            retry_job_id
        ]
        assert len(forecast_submissions) == 1
    else:
        # Flat geometry: the released row itself represents the stage, the manual
        # retry evaluation short-circuits, and nothing is re-submitted.
        assert f"{base_job_id}_retry_1" not in repository.jobs
        assert forecast_submissions == []
        # The chain DID reach the forecast stage; it just resumed the released row.
        assert [(stage.stage, stage.pipeline_job_id, stage.status) for stage in result.stages] == [
            ("forecast", base_job_id, "reservation_lost")
        ]
        assert result.status == "failed"
    # Either way the released row itself is never revived under its spent key.
    assert repository.jobs[base_job_id]["status"] == "reservation_lost"
    assert repository.jobs[base_job_id]["reconciliation_decision"] == "identity_mismatch_released"


def test_cohort_reservation_records_each_models_warm_start_identity(tmp_path: Path) -> None:
    """#1183 task 1.3: the accepted-submit master row books the warm start it planned.

    Reservation is the only point on the cohort path that still sees the
    per-basin warm-start selection (``chain_forecast_orchestrator_cycle.py:
    509-527`` reads ``context.active_basins``); the reconcile-side terminal
    write has Slurm accounting only.  The 18 models each initialise from their
    OWN checkpoint, so the booked evidence is a per-``array_task_id`` map — a
    single scalar would misattribute 17 of 18 lineages.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _basins, _orchestrator

    cycle = "2026050100"
    basins = _basins(3)
    for index, basin in enumerate(basins):
        basin.update(
            {
                "run_id": f"fcst_gfs_{cycle}_model_{index}",
                "candidate_id": f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic",
                "orchestration_run_id": f"cycle_gfs_{cycle}_forecast_cohort_fixture",
                "restart_stage": "forecast",
                "state_evidence": {"restart_stage": "forecast"},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
                "init_state_id": f"state_gfs_model_{index}_2026050100_gfs_2026043012_f012",
                "init_state_uri": f"s3://nhms/states/gfs/model_{index}/2026050100/state.cfg.ic",
                "init_state_checksum": f"sha256:state-{index}",
                "init_state_valid_time": "2026-05-01T00:00:00Z",
            }
        )
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    orchestrator = _orchestrator(tmp_path, repository, FakeCycleSlurmClient())

    result = orchestrator.orchestrate_cycle("gfs", cycle, basins)

    assert result.status == "complete"
    reopened = FileOrchestrationJournalRepository(repository.root)
    master = next(
        row
        for row in reopened.query_pipeline_jobs_by_cycle("gfs_2026050100")
        if row.get("stage") == "forecast" and row.get("model_id") is None
    )
    assert master["init_state_identities"] == [
        {
            "array_task_id": index,
            "model_id": f"model_{index}",
            "init_state_id": f"state_gfs_model_{index}_2026050100_gfs_2026043012_f012",
            "init_state_checksum": f"sha256:state-{index}",
            # Object URIs are placeholder-sanitized on every public read.
            "init_state_uri": "[object-uri]",
            "init_state_valid_time": "2026-05-01T00:00:00Z",
        }
        for index in range(3)
    ]
    # Digest inputs are untouched, so the cohort identity still validates.
    assert len(master["cohort_digest"]) == 64
    assert [member["model_id"] for member in master["cohort_members"]] == [
        f"model_{index}" for index in range(3)
    ]


def test_cold_seeded_cohort_basins_book_no_init_state_identity(tmp_path: Path) -> None:
    """Cold-seeded basins resolve no warm start, so nothing is booked for them.

    Absence stays absence: a cold-start basin must not be recorded with an
    empty-valued identity that a later reader could mistake for a claim.
    """

    from services.orchestrator.accepted_submit_identity import (
        canonical_forecast_cohort_init_state_identities,
    )

    identities = canonical_forecast_cohort_init_state_identities(
        basins=[
            {"task_id": 0, "model_id": "model_0", "init_state_id": None, "init_state_uri": None},
            {
                "task_id": 1,
                "model_id": "model_1",
                "init_state_id": "state_gfs_model_1_2026050100_gfs_2026043012_f012",
                "init_state_checksum": "sha256:state-1",
            },
        ]
    )

    assert identities == (
        {
            "array_task_id": 1,
            "model_id": "model_1",
            "init_state_id": "state_gfs_model_1_2026050100_gfs_2026043012_f012",
            "init_state_checksum": "sha256:state-1",
        },
    )


# ---------------------------------------------------------------------------
# #1164: packaged-IC bootstrap carrier — the scheduler decision must reach the
# runtime manifest under BOTH warm-start modes.  Strict must not hard-fail the
# cohort, non-strict must not degrade the basin to ``cold_start_no_state``.
# ---------------------------------------------------------------------------


PACKAGED_IC_SHA256 = "b" * 64


def _packaged_ic_bootstrap_basin(*, checksum: str | None = PACKAGED_IC_SHA256) -> dict[str, Any]:
    strict_layer: dict[str, Any] = {
        "mode": "db_free_packaged_ic_bootstrap",
        "ready": True,
        "status": "ready",
        "cold_start_reason": None,
    }
    if checksum is not None:
        strict_layer["packaged_ic_checksum"] = checksum
    return {
        "model_id": "new_model",
        "basin_id": "new_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "river_v01",
        "segment_count": 2,
        "model_package_uri": "models/new_model/package/",
        "source_id": "gfs",
        "state_evidence": {
            "decision": "submit",
            "strict_warm_start": strict_layer,
        },
    }


@pytest.mark.parametrize("require_forecast_warm_start", [True, False], ids=["strict", "non_strict"])
def test_packaged_ic_bootstrap_reaches_runtime_manifest_in_both_modes(
    tmp_path: Path, require_forecast_warm_start: bool
) -> None:
    from services.orchestrator.chain import CycleOrchestrationContext

    cycle_time = _dt("2026-07-05T00:00:00Z")
    orchestrator = _cohort_orchestrator(
        tmp_path,
        FakeStateManager([]),
        require_forecast_warm_start=require_forecast_warm_start,
    )
    basins = orchestrator._normalize_cycle_basins(
        [_packaged_ic_bootstrap_basin()], "gfs", cycle_time
    )

    orchestrator._apply_cohort_warm_start(basins, "gfs", cycle_time)

    record = basins[0]
    assert record["packaged_ic_selected"] is True
    assert record["packaged_ic_checksum"] == PACKAGED_IC_SHA256
    # ``init_state_id`` stays absent: the cohort identity map must not book a
    # packaged bootstrap as a warm-start claim (#1183/#1184 semantics).
    assert record["init_state_id"] is None
    assert record["init_state_uri"] is None

    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id="gfs_2026070500",
        run_id="cycle_run",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage=None,
    )
    runtime_manifest = orchestrator._build_forecast_runtime_manifest(context, record)

    assert runtime_manifest["runtime"]["init_mode"] == 3
    assert runtime_manifest["initial_state"]["quality"] == "packaged_calibrated_state"
    assert runtime_manifest["initial_state"]["state_id"] is None
    assert runtime_manifest["initial_state"]["ic_file_uri"] is None
    assert runtime_manifest["initial_state"]["packaged_ic_checksum"] == PACKAGED_IC_SHA256
    # No exact-warm-start policy: there is no selected state to be exact about.
    assert "warm_start_policy" not in runtime_manifest["runtime"]


def test_packaged_ic_bootstrap_cohort_books_no_init_state_identity(tmp_path: Path) -> None:
    from services.orchestrator.accepted_submit_identity import (
        canonical_forecast_cohort_init_state_identities,
    )

    cycle_time = _dt("2026-07-05T00:00:00Z")
    orchestrator = _cohort_orchestrator(
        tmp_path, FakeStateManager([]), require_forecast_warm_start=True
    )
    basins = orchestrator._normalize_cycle_basins(
        [_packaged_ic_bootstrap_basin()], "gfs", cycle_time
    )
    orchestrator._apply_cohort_warm_start(basins, "gfs", cycle_time)

    assert canonical_forecast_cohort_init_state_identities(basins=basins) == ()


def test_packaged_ic_bootstrap_without_recorded_checksum_still_bootstraps(tmp_path: Path) -> None:
    """Legacy/degenerate form: the mode is present but no digest was recorded.

    The run must still take the packaged path (fail-closed verification then
    happens in the runtime against a non-empty parseable IC) rather than
    silently falling back to a zeroed cold start.
    """
    from services.orchestrator.chain import CycleOrchestrationContext

    cycle_time = _dt("2026-07-05T00:00:00Z")
    orchestrator = _cohort_orchestrator(
        tmp_path, FakeStateManager([]), require_forecast_warm_start=True
    )
    basins = orchestrator._normalize_cycle_basins(
        [_packaged_ic_bootstrap_basin(checksum=None)], "gfs", cycle_time
    )
    orchestrator._apply_cohort_warm_start(basins, "gfs", cycle_time)
    record = basins[0]
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id="gfs_2026070500",
        run_id="cycle_run",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage=None,
    )

    runtime_manifest = orchestrator._build_forecast_runtime_manifest(context, record)

    assert runtime_manifest["runtime"]["init_mode"] == 3
    assert runtime_manifest["initial_state"]["quality"] == "packaged_calibrated_state"
    assert runtime_manifest["initial_state"]["packaged_ic_checksum"] is None


# ---------------------------------------------------------------------------
# #1325: publish-side admission gate (design D1/D3, anchors A1-A6).
#
# The gate admits a state publish only when the source output tree carries a
# solver-success witness (``state_checkpoints.json`` with a ``provenance``
# block naming THIS run) and every artifact the manifest names is present and
# checksum-matched.  Rejections lead with a typed ``STATE_SAVE_SOURCE_*``
# token and must not touch the snapshot / QC / index seam at all.
# ---------------------------------------------------------------------------


GATE_RUN_ID = "fcst_gfs_2026050100_demo_model"
GATE_CYCLE_TIME = "2026-05-01T00:00:00Z"
GATE_END_TIME = "2026-05-01T12:00:00Z"


class _RecordingStateManager:
    """StateManager double that records every publish-side call it receives."""

    def __init__(self, object_store: LocalObjectStore) -> None:
        self.object_store = object_store
        self.saved: list[dict[str, Any]] = []
        self.qc_runs: list[str] = []

    def save_state_snapshot(self, **kwargs: Any) -> Any:
        self.saved.append(kwargs)
        state_id = f"state_{len(self.saved):03d}"
        snapshot = SimpleNamespace(
            state_uri=f"states/{state_id}/state.cfg.ic",
            checksum=f"sha256:{state_id}",
            valid_time=kwargs["valid_time"],
            source_id=kwargs.get("source_id"),
            cycle_id=kwargs.get("cycle_id"),
            lead_hours=kwargs.get("lead_hours"),
            model_package_version=kwargs.get("model_package_version"),
            model_package_checksum=kwargs.get("model_package_checksum"),
            original_shud_filename=kwargs.get("original_shud_filename"),
        )
        return SimpleNamespace(state_id=state_id, status="created", snapshot=snapshot)

    def run_qc(self, state_id: str) -> bool:
        self.qc_runs.append(state_id)
        return True


def _gate_ic_text(valid_time: datetime, *, surface: str = "0.1") -> str:
    return f"2 1 {valid_time.timestamp() / 60.0:.6f}\n1 {surface}\n2 0.2\n1 0.0\n"


def _gate_provenance(run_id: str = GATE_RUN_ID, *, requested_hours: Any = ()) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "generated_at": "2026-05-01T12:00:03Z",
        "slurm_job_id": "4242",
        "array_task_id": 3,
        "requested_checkpoint_hours": list(requested_hours),
    }


def _write_gate_manifest(output_root: Path, payload: dict[str, Any]) -> Path:
    manifest_dir = output_root / "state_checkpoints"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "state_checkpoints.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _write_gate_checkpoint(output_root: Path, lead_hours: int) -> dict[str, Any]:
    valid_time = _dt(GATE_CYCLE_TIME) + timedelta(hours=lead_hours)
    manifest_dir = output_root / "state_checkpoints"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    name = f"demo.f{lead_hours:03d}.cfg.ic.update"
    content = _gate_ic_text(valid_time, surface=f"0.{lead_hours}")
    (manifest_dir / name).write_text(content, encoding="utf-8")
    return {
        "lead_hours": lead_hours,
        "valid_time": _format_time_for_test(valid_time),
        "relative_path": f"state_checkpoints/{name}",
        "checkpoint_filename": name,
        "checksum": sha256_bytes(content.encode("utf-8")),
    }


def _write_gate_final_ic(output_root: Path, *, name: str = "demo.cfg.ic.update") -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    content = _gate_ic_text(_dt(GATE_END_TIME))
    (output_root / name).write_text(content, encoding="utf-8")
    return {
        "relative_path": name,
        "original_shud_filename": name,
        "checksum": sha256_bytes(content.encode("utf-8")),
    }


def _gate_run_context(*, output_uri: str | None = None, run_id: str = GATE_RUN_ID) -> Any:
    from packages.common.state_cli import StateRunContext

    return StateRunContext(
        run_id=run_id,
        model_id="demo_model",
        end_time=_dt(GATE_END_TIME),
        output_uri=output_uri,
        source_id="GFS",
        cycle_time=_dt(GATE_CYCLE_TIME),
        model_package_version="s3://nhms/models/demo_model/package/",
        model_package_checksum="package-sha-1",
    )


def _gate_manager(tmp_path: Path) -> _RecordingStateManager:
    return _RecordingStateManager(LocalObjectStore(tmp_path / "object-store", "s3://nhms"))


def _gate_workspace_output(tmp_path: Path, run_id: str = GATE_RUN_ID) -> Path:
    output_root = tmp_path / "workspace" / "runs" / run_id / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


def _gate_object_output(tmp_path: Path, run_id: str = GATE_RUN_ID) -> Path:
    output_root = tmp_path / "object-store" / "runs" / run_id / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


def _gate_save(tmp_path: Path, manager: _RecordingStateManager, *, output_uri: str | None = None) -> dict[str, Any]:
    from packages.common.state_cli import save_state_for_run

    return save_state_for_run(
        GATE_RUN_ID,
        manager=manager,
        run_context=_gate_run_context(output_uri=output_uri),
        workspace_root=tmp_path / "workspace",
    )


def test_state_save_rejects_missing_output_root(tmp_path: Path) -> None:
    """A1 (AC-1): no output root anywhere is a typed reject, not a tree search."""

    from packages.common.state_manager import StateManagerError

    (tmp_path / "workspace").mkdir()
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_OUTPUT_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_manifest_with_missing_declared_checkpoint(tmp_path: Path) -> None:
    """A2 (AC-2): declared N, present N-1 rejects instead of publishing the subset."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    f012 = _write_gate_checkpoint(output_root, 12)
    (output_root / f012["relative_path"]).unlink()
    _write_gate_manifest(
        output_root,
        {"checkpoints": [f006, f012], "provenance": _gate_provenance(requested_hours=(6, 12))},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE")
    assert f012["relative_path"] in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_checkpoint_content_drift(tmp_path: Path) -> None:
    """A2b: a declared checkpoint whose bytes changed after capture rejects."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    (output_root / f006["relative_path"]).write_text(
        _gate_ic_text(_dt(GATE_CYCLE_TIME) + timedelta(hours=6), surface="0.9"),
        encoding="utf-8",
    )
    _write_gate_manifest(
        output_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH")
    assert f006["relative_path"] in message
    assert manager.saved == []
    assert manager.qc_runs == []


@pytest.mark.parametrize(
    "shape",
    [
        "not_a_dict",
        "missing_fields",
        "missing_checksum",
        "non_regular_target",
        "non_sequence",
        "unparseable_valid_time",
    ],
)
def test_state_save_rejects_malformed_manifest_checkpoint_entry(tmp_path: Path, shape: str) -> None:
    """A2c: every shape the loader silently drops is a gate violation.

    The declared set is the RAW ``checkpoints`` array, so an entry the parser
    would filter out (non-dict, missing fields, non-regular target) or a
    ``checkpoints`` value that is not a list can never shrink the published set
    behind the caller's back. ``unparseable_valid_time`` (PR review round-1) is
    the same class read from the other end: presence alone is not a usable
    declaration, and letting it through leaves the loader to crash with a bare
    ``ValueError`` instead of a typed reject.
    """

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    f012 = _write_gate_checkpoint(output_root, 12)
    raw_checkpoints: Any
    if shape == "not_a_dict":
        raw_checkpoints = [f006, "state_checkpoints/demo.f012.cfg.ic.update"]
    elif shape == "missing_fields":
        raw_checkpoints = [f006, {"checkpoint_filename": "demo.f012.cfg.ic.update", "checksum": "a" * 64}]
    elif shape == "missing_checksum":
        raw_checkpoints = [f006, {key: value for key, value in f012.items() if key != "checksum"}]
    elif shape == "non_regular_target":
        (output_root / f012["relative_path"]).unlink()
        (output_root / f012["relative_path"]).mkdir()
        raw_checkpoints = [f006, f012]
    elif shape == "unparseable_valid_time":
        raw_checkpoints = [f006, {**f012, "valid_time": "2026-05-01 lunchtime"}]
    else:
        raw_checkpoints = {}
    _write_gate_manifest(
        output_root,
        {"checkpoints": raw_checkpoints, "provenance": _gate_provenance(requested_hours=(6, 12))},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE")
    assert ("checkpoints" if shape == "non_sequence" else "entry 1") in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_failed_attempt_residue_without_manifest(tmp_path: Path) -> None:
    """A3 (AC-3): a killed attempt leaves ICs but no witness -> no publish."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    (output_root / "demo.cfg.ic").write_text(_gate_ic_text(_dt("2026-05-01T04:00:00Z")), encoding="utf-8")
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_MANIFEST_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_manifest_without_provenance(tmp_path: Path) -> None:
    """A4 (AC-6): a legacy (pre-upgrade) manifest cannot prove its origin."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    _write_gate_manifest(output_root, {"checkpoints": [f006]})
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_PROVENANCE_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_foreign_provenance_run_id(tmp_path: Path) -> None:
    """A4b: another run's witnessed tree is never published under this identity."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    _write_gate_manifest(
        output_root,
        {
            "checkpoints": [f006],
            "provenance": _gate_provenance("fcst_gfs_2026043012_demo_model", requested_hours=(6,)),
        },
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_PROVENANCE_MISMATCH")
    assert "fcst_gfs_2026043012_demo_model" in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_publishes_manifest_named_final_ic_when_no_checkpoints_requested(tmp_path: Path) -> None:
    """A5(a) liveness pin: the analysis / short-horizon lane stays publishable."""

    output_root = _gate_workspace_output(tmp_path)
    final_ic = _write_gate_final_ic(output_root)
    _write_gate_manifest(
        output_root,
        {"checkpoints": [], "final_ic": final_ic, "provenance": _gate_provenance()},
    )
    manager = _gate_manager(tmp_path)

    result = _gate_save(tmp_path, manager)

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == _dt(GATE_END_TIME)
    assert manager.saved[0]["original_shud_filename"] == "demo.cfg.ic.update"
    assert result["valid_time"] == GATE_END_TIME
    assert manager.qc_runs == [result["state_id"]]


def test_state_save_final_ic_lane_never_selects_undeclared_residue(tmp_path: Path) -> None:
    """A5(b): only the manifest-NAMED final IC is publishable.

    The residue sorts before the named artifact and sits in the same candidate
    class, which is exactly the selection master's ``sorted()`` rglob made.
    """

    output_root = _gate_workspace_output(tmp_path)
    final_ic = _write_gate_final_ic(output_root)
    (output_root / "aaa-residue.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T04:00:00Z"), surface="0.7"),
        encoding="utf-8",
    )
    _write_gate_manifest(
        output_root,
        {"checkpoints": [], "final_ic": final_ic, "provenance": _gate_provenance()},
    )
    manager = _gate_manager(tmp_path)

    _gate_save(tmp_path, manager)

    assert len(manager.saved) == 1
    assert manager.saved[0]["original_shud_filename"] == "demo.cfg.ic.update"
    assert manager.saved[0]["ic_file_path"].name.startswith("demo.cfg.ic.update")


def test_state_save_rejects_final_ic_content_drift(tmp_path: Path) -> None:
    """A5(c): a later killed attempt rewriting the final IC in place is caught."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    final_ic = _write_gate_final_ic(output_root)
    _write_gate_manifest(
        output_root,
        {"checkpoints": [], "final_ic": final_ic, "provenance": _gate_provenance()},
    )
    (output_root / "demo.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T09:00:00Z"), surface="0.8"),
        encoding="utf-8",
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH")
    assert "demo.cfg.ic.update" in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_total_checkpoint_miss_instead_of_final_ic_downgrade(tmp_path: Path) -> None:
    """A5(d): requested hours with nothing captured must not fall back."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    final_ic = _write_gate_final_ic(output_root)
    _write_gate_manifest(
        output_root,
        {
            "checkpoints": [],
            "final_ic": final_ic,
            "provenance": _gate_provenance(requested_hours=(6, 12)),
            "recovery_outcomes": {"6": "gate_rejected(header=1440)", "12": "exit_1"},
        },
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_rejects_fallback_manifest_without_final_ic_entry(tmp_path: Path) -> None:
    """A5(e): a witnessed solve that produced no final state rejects, not searches."""

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    (output_root / "demo.cfg.ic").write_text(_gate_ic_text(_dt(GATE_END_TIME)), encoding="utf-8")
    _write_gate_manifest(output_root, {"checkpoints": [], "provenance": _gate_provenance()})
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_FINAL_IC_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_publishes_object_store_root_when_workspace_tree_has_no_manifest(tmp_path: Path) -> None:
    """A6(a) liveness pin: the failure-lane workspace tree falls through."""

    workspace_root = _gate_workspace_output(tmp_path)
    (workspace_root / "demo.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T03:00:00Z"), surface="0.6"),
        encoding="utf-8",
    )
    object_root = _gate_object_output(tmp_path)
    f006 = _write_gate_checkpoint(object_root, 6)
    _write_gate_manifest(
        object_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)

    _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == _dt(GATE_CYCLE_TIME) + timedelta(hours=6)
    assert manager.saved[0]["original_shud_filename"] == "demo.f006.cfg.ic.update"


def test_state_save_falls_through_foreign_workspace_manifest_to_witnessed_root(tmp_path: Path) -> None:
    """A6(b): probe order never beats provenance — the witnessed root wins."""

    workspace_root = _gate_workspace_output(tmp_path)
    foreign = _write_gate_checkpoint(workspace_root, 6)
    _write_gate_manifest(
        workspace_root,
        {
            "checkpoints": [foreign],
            "provenance": _gate_provenance("fcst_gfs_2026043012_demo_model", requested_hours=(6,)),
        },
    )
    object_root = _gate_object_output(tmp_path)
    f012 = _write_gate_checkpoint(object_root, 12)
    _write_gate_manifest(
        object_root,
        {"checkpoints": [f012], "provenance": _gate_provenance(requested_hours=(12,))},
    )
    manager = _gate_manager(tmp_path)

    _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == _dt(GATE_CYCLE_TIME) + timedelta(hours=12)
    assert manager.saved[0]["ic_file_path"].is_relative_to(object_root)


def test_state_save_durable_output_reuse_retry_publishes_then_rejects_when_tree_removed(tmp_path: Path) -> None:
    """A6(c): identity, not recency — a reused durable tree keeps publishing."""

    import shutil

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    _write_gate_manifest(
        output_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)

    _gate_save(tmp_path, manager)
    _gate_save(tmp_path, manager)

    assert len(manager.saved) == 2

    shutil.rmtree(tmp_path / "workspace" / "runs" / GATE_RUN_ID)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_OUTPUT_MISSING")
    assert len(manager.saved) == 2


def test_state_save_rejects_verified_checkpoints_that_vanish_before_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2d (implementation guts): the checkpoints lane never indexes an empty list.

    The gate verifies the RAW manifest array and the publish then re-reads it
    through the loader, so a file deleted in between shrinks N verified entries
    to zero. The only reachable trigger is that TOCTOU window, hence the loader
    is monkeypatched; what is pinned is that the answer is the typed reason and
    not a bare ``IndexError`` from ``saved[0]``.
    """

    from packages.common import state_cli
    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    _write_gate_manifest(
        output_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)
    monkeypatch.setattr(state_cli, "_load_state_checkpoint_manifest", lambda _manifest_path: [])

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE")
    assert manager.saved == []
    assert manager.qc_runs == []


@pytest.mark.parametrize(
    "defect",
    ["missing_requested_hours", "blank_run_id", "blank_generated_at", "requested_hours_string"],
)
def test_state_save_rejects_partially_populated_provenance(tmp_path: Path, defect: str) -> None:
    """A4c: a provenance block the gate cannot act on is a G3 violation.

    ``requested_checkpoint_hours`` is what discriminates the zero-hour fallback
    lane from a total capture miss, so defaulting it away (``.get(..., [])``)
    would silently restore the #1164 downgrade; ``run_id``/``generated_at`` are
    the identity evidence G4 and the operator's diagnosis rest on. Each shape
    reaches ``_usable_provenance`` through a different predicate — with
    ``missing_requested_hours`` the key-presence check firing first and the
    sequence check standing behind it.
    """

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    f006 = _write_gate_checkpoint(output_root, 6)
    provenance = _gate_provenance(requested_hours=(6,))
    if defect == "missing_requested_hours":
        provenance.pop("requested_checkpoint_hours")
    elif defect == "blank_run_id":
        provenance["run_id"] = "   "
    elif defect == "blank_generated_at":
        provenance["generated_at"] = ""
    else:
        provenance["requested_checkpoint_hours"] = "6,12"
    _write_gate_manifest(output_root, {"checkpoints": [f006], "provenance": provenance})
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_PROVENANCE_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_publishes_the_exact_final_ic_path_the_gate_hashed(tmp_path: Path) -> None:
    """A5(f): verification and publish must resolve to the SAME file.

    A ``final_ic.relative_path`` carrying trailing whitespace next to a
    whitespace-twin file on disk splits them: the gate checksums the stripped
    name while a raw-string publish opens the twin, shipping bytes nothing ever
    verified. This tree is accepted, so the pin is on the live SUCCESS branch —
    the published bytes must be the checksum-verified ones, never the twin's.
    """

    output_root = _gate_workspace_output(tmp_path)
    final_ic = _write_gate_final_ic(output_root)
    verified_bytes = (output_root / "demo.cfg.ic.update").read_bytes()
    twin_bytes = _gate_ic_text(_dt(GATE_END_TIME), surface="0.9").encode("utf-8")
    (output_root / "demo.cfg.ic.update ").write_bytes(twin_bytes)
    final_ic["relative_path"] = "demo.cfg.ic.update "
    _write_gate_manifest(
        output_root,
        {"checkpoints": [], "final_ic": final_ic, "provenance": _gate_provenance()},
    )
    manager = _gate_manager(tmp_path)

    _gate_save(tmp_path, manager)

    assert len(manager.saved) == 1
    published = manager.saved[0]["ic_file_path"].read_bytes()
    assert published != twin_bytes
    assert published == verified_bytes


def test_state_save_reports_the_first_existing_roots_reason_when_every_root_fails(tmp_path: Path) -> None:
    """A6(d): D3 rule 5 — the reported reason follows probe order, deterministically.

    Two roots fail for different reasons; the operator must always be handed the
    first existing root's reason so the same tree never diagnoses two ways.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    (workspace_root / "demo.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T03:00:00Z"), surface="0.6"),
        encoding="utf-8",
    )
    object_root = _gate_object_output(tmp_path)
    foreign = _write_gate_checkpoint(object_root, 6)
    _write_gate_manifest(
        object_root,
        {
            "checkpoints": [foreign],
            "provenance": _gate_provenance("fcst_gfs_2026043012_demo_model", requested_hours=(6,)),
        },
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_MANIFEST_MISSING")
    assert "STATE_SAVE_SOURCE_PROVENANCE_MISMATCH" not in message
    assert manager.saved == []
    assert manager.qc_runs == []


# --- #1329 multi-root shadow fall-through (verified-root re-scope) -----------
#
# The failure lane uploads nothing, so "newer-but-unpublishable workspace tree /
# older-but-healthy object-store tree" is the ROUTINE retry shape. Before the
# re-scope the two post-verification verdicts hard-rejected at the first root
# and the healthy sibling was never probed.


def _gate_total_miss_manifest(output_root: Path, *, requested_hours: tuple[int, ...] = (6, 12)) -> Path:
    """Write attempt N+1's witnessed TOTAL-MISS tree: hours requested, none captured."""

    final_ic = _write_gate_final_ic(output_root)
    return _write_gate_manifest(
        output_root,
        {
            "checkpoints": [],
            "final_ic": final_ic,
            "provenance": _gate_provenance(requested_hours=requested_hours),
        },
    )


def _gate_uncaptured_message(manifest_path: Path, requested_hours: list[int]) -> str:
    """Rebuild the pre-change CHECKPOINTS_UNCAPTURED text from the contract, not the code.

    Independent oracle for the byte-identity pins: the token leads, then the
    manifest path and the requested hours the tree failed to capture.
    """

    return (
        f"STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED: manifest {manifest_path} "
        f"requested checkpoint hours {requested_hours} but captured none."
    )


def _gate_final_ic_missing_message(manifest_path: Path) -> str:
    """Rebuild the FINAL_IC_MISSING text from the contract, not the code (A7).

    Independent oracle mirroring ``_gate_uncaptured_message``: the token leads,
    then the manifest path the operator must open to diagnose it. Written as a
    literal on purpose — importing the code's constant/f-string would make the
    pin agree with any mutation of the detail (e.g. dropping the path).
    """

    return f"STATE_SAVE_SOURCE_FINAL_IC_MISSING: manifest {manifest_path} names no final IC to publish."


def test_state_save_total_miss_workspace_does_not_shadow_healthy_checkpoint_sibling(tmp_path: Path) -> None:
    """A1 (AC-2): attempt N+1's total-miss tree yields to attempt N's checkpoints.

    Pre-change the workspace root hard-rejected with
    ``STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`` and the healthy object-store
    root was never probed. The published artifact must be the OBJECT-STORE
    root's checkpoint — asserted by content, not merely by "it succeeded".
    """

    workspace_root = _gate_workspace_output(tmp_path)
    _gate_total_miss_manifest(workspace_root)
    object_root = _gate_object_output(tmp_path)
    f012 = _write_gate_checkpoint(object_root, 12)
    _write_gate_manifest(
        object_root,
        {"checkpoints": [f012], "provenance": _gate_provenance(requested_hours=(12,))},
    )
    manager = _gate_manager(tmp_path)

    result = _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert len(manager.saved) == 1
    published = manager.saved[0]["ic_file_path"]
    assert published.is_relative_to(object_root)
    assert not published.is_relative_to(workspace_root)
    assert published.read_bytes() == (object_root / f012["relative_path"]).read_bytes()
    assert manager.saved[0]["valid_time"] == _dt(GATE_CYCLE_TIME) + timedelta(hours=12)
    assert manager.saved[0]["original_shud_filename"] == "demo.f012.cfg.ic.update"
    assert manager.qc_runs == [result["state_id"]]


def test_state_save_final_ic_missing_workspace_does_not_shadow_healthy_fallback_sibling(tmp_path: Path) -> None:
    """A2 (AC-3): a zero-hours workspace tree naming no final IC yields to the sibling.

    Also the cross-root downgrade guard's companion LIVENESS pin: the guard is
    armed by a ``CHECKPOINTS_UNCAPTURED`` fall-through only, so this
    all-fallback-lane geometry must still publish.
    """

    workspace_root = _gate_workspace_output(tmp_path)
    workspace_manifest = _write_gate_manifest(
        workspace_root,
        {"checkpoints": [], "provenance": _gate_provenance()},
    )
    object_root = _gate_object_output(tmp_path)
    sibling_ic = _write_gate_final_ic(object_root, name="sibling.cfg.ic.update")
    _write_gate_manifest(
        object_root,
        {"checkpoints": [], "final_ic": sibling_ic, "provenance": _gate_provenance()},
    )
    manager = _gate_manager(tmp_path)

    result = _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert workspace_manifest.exists()
    assert len(manager.saved) == 1
    published = manager.saved[0]["ic_file_path"]
    assert published.is_relative_to(object_root)
    assert published.read_bytes() == (object_root / sibling_ic["relative_path"]).read_bytes()
    assert manager.saved[0]["original_shud_filename"] == "sibling.cfg.ic.update"
    assert manager.saved[0]["valid_time"] == _dt(GATE_END_TIME)
    assert result["valid_time"] == GATE_END_TIME
    assert manager.qc_runs == [result["state_id"]]


def test_state_save_reports_the_first_roots_total_miss_when_the_sibling_has_no_manifest(tmp_path: Path) -> None:
    """A3(a) (AC-4, rule 5): both roots unpublishable — forward geometry.

    Message pin, GREEN on both sides of the re-scope: the reported text stays
    the FIRST existing root's ``CHECKPOINTS_UNCAPTURED``, byte-identical to the
    pre-change hard raise. Teeth against an implementation that reports the LAST
    root's reason or a generic "no root verified" exhaustion message.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    workspace_manifest = _gate_total_miss_manifest(workspace_root)
    object_root = _gate_object_output(tmp_path)
    (object_root / "demo.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T03:00:00Z"), surface="0.6"),
        encoding="utf-8",
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert str(exc_info.value) == _gate_uncaptured_message(workspace_manifest, [6, 12])
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_reports_the_first_roots_missing_manifest_over_a_later_total_miss(tmp_path: Path) -> None:
    """A3(b) (AC-4, rule 5): both roots unpublishable — REVERSED geometry.

    The one both-fail shape whose reported token changes: pre-change the later
    root's hard ``CHECKPOINTS_UNCAPTURED`` escaped the loop and won; rule 5 now
    applies uniformly, so the FIRST existing root's reason is reported.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    (workspace_root / "demo.cfg.ic.update").write_text(
        _gate_ic_text(_dt("2026-05-01T03:00:00Z"), surface="0.6"),
        encoding="utf-8",
    )
    object_root = _gate_object_output(tmp_path)
    _gate_total_miss_manifest(object_root, requested_hours=(6,))
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    message = str(exc_info.value)
    assert message.startswith("STATE_SAVE_SOURCE_MANIFEST_MISSING")
    assert "STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED" not in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_final_ic_missing_message_names_the_manifest_verbatim(tmp_path: Path) -> None:
    """A7: FULL-STRING pin on the FINAL_IC_MISSING text (single root, no ``output_uri``).

    The existing anchors only assert the leading token, so a mutation dropping
    the manifest path from the detail leaves the operator with an
    undiagnosable message and still passes the whole floor. Byte-identity
    against a contract-rebuilt oracle is what bites.
    """

    from packages.common.state_manager import StateManagerError

    output_root = _gate_workspace_output(tmp_path)
    manifest_path = _write_gate_manifest(output_root, {"checkpoints": [], "provenance": _gate_provenance()})
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager)

    assert str(exc_info.value) == _gate_final_ic_missing_message(manifest_path)
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_manifest_entry_overflow_never_yields_to_a_healthy_sibling(tmp_path: Path) -> None:
    """A5(b) (AC-5): the entry-count overflow stays a HARD error next to a healthy root.

    Previously untested geometry. The re-scope widens fall-through to exactly
    two verdicts; an over-eager implementation that made every rejection
    fall through would publish the sibling here.
    """

    from packages.common.state_cli import MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES
    from packages.common.state_manager import StateManagerError

    overflow = MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES + 1
    workspace_root = _gate_workspace_output(tmp_path)
    _write_gate_manifest(
        workspace_root,
        {"checkpoints": [{} for _ in range(overflow)], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    object_root = _gate_object_output(tmp_path)
    f006 = _write_gate_checkpoint(object_root, 6)
    _write_gate_manifest(
        object_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert str(exc_info.value) == (
        f"State checkpoint manifest exceeds maximum entry count: {overflow} > "
        f"{MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES}"
    )
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_unparseable_manifest_never_yields_to_a_healthy_sibling(tmp_path: Path) -> None:
    """A5(c) (AC-5): a present-but-unparseable manifest stays a hard error.

    A suspect manifest is not an absent one: it must terminate the publish
    rather than let the next root paper over it.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    manifest_dir = workspace_root / "state_checkpoints"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "state_checkpoints.json").write_text('{"checkpoints": [', encoding="utf-8")
    object_root = _gate_object_output(tmp_path)
    f006 = _write_gate_checkpoint(object_root, 6)
    _write_gate_manifest(
        object_root,
        {"checkpoints": [f006], "provenance": _gate_provenance(requested_hours=(6,))},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert str(exc_info.value).startswith("Invalid state checkpoint manifest")
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_later_root_hard_error_supersedes_earlier_fall_through_reason(tmp_path: Path) -> None:
    """A5(d) (AC-5): a LATER root's hard error is not subject to rule 5.

    Geometry: workspace total-miss (falls through with
    ``CHECKPOINTS_UNCAPTURED``) then an object-store manifest that is present
    but unparseable. The hard error escapes the loop, so the operator is handed
    the SECOND root's suspect-manifest text — naming the file they must open —
    rather than the first root's soft reason. Teeth against an implementation
    that swallows later-root hard errors into the ``first_rejection`` report.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    workspace_manifest = _gate_total_miss_manifest(workspace_root)
    object_root = _gate_object_output(tmp_path)
    object_manifest_dir = object_root / "state_checkpoints"
    object_manifest_dir.mkdir(parents=True, exist_ok=True)
    object_manifest = object_manifest_dir / "state_checkpoints.json"
    object_manifest.write_text('{"checkpoints": [', encoding="utf-8")
    assert workspace_manifest.exists()
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    message = str(exc_info.value)
    assert message.startswith("Invalid state checkpoint manifest ")
    assert str(object_manifest) in message
    assert str(workspace_manifest) not in message
    assert not message.startswith("STATE_SAVE_SOURCE_")
    assert "STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED" not in message
    assert manager.saved == []
    assert manager.qc_runs == []


def test_state_save_total_miss_root_blocks_a_later_roots_final_ic_fallback(tmp_path: Path) -> None:
    """A6: cross-root downgrade guard — the no-downgrade invariant holds ACROSS roots.

    The workspace rejection PROVES this run's configuration requested checkpoint
    states, so a sibling attempt's single end-time IC (config drift between
    attempts) must not silently satisfy it. A naive fall-through implementation
    — one that simply lets the two re-scoped verdicts yield — publishes the
    sibling's IC here and fails this anchor.
    """

    from packages.common.state_manager import StateManagerError

    workspace_root = _gate_workspace_output(tmp_path)
    workspace_manifest = _gate_total_miss_manifest(workspace_root)
    object_root = _gate_object_output(tmp_path)
    sibling_ic = _write_gate_final_ic(object_root, name="sibling.cfg.ic.update")
    _write_gate_manifest(
        object_root,
        {"checkpoints": [], "final_ic": sibling_ic, "provenance": _gate_provenance()},
    )
    manager = _gate_manager(tmp_path)

    with pytest.raises(StateManagerError) as exc_info:
        _gate_save(tmp_path, manager, output_uri=f"s3://nhms/runs/{GATE_RUN_ID}/output/")

    assert str(exc_info.value) == _gate_uncaptured_message(workspace_manifest, [6, 12])
    assert manager.saved == []
    assert manager.qc_runs == []
