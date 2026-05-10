from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.forecast import get_forecast_store
from packages.common.best_available import BestAvailableManager, BestAvailableSelection, ForcingInputSelection
from packages.common.forecast_store import _spliced_response_from_rows, analysis_window_for_issue_time
from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import StateManager, StateSnapshot
from packages.common.test_netcdf4 import encode_test_netcdf4
from services.orchestrator.chain import ForcingContext, ForecastOrchestrator, InitialStateSelection, ModelContext
from services.orchestrator.chain import OrchestratorConfig as ChainOrchestratorConfig
from workers.canonical_converter.converter import (
    CanonicalConverter,
    CanonicalConverterConfig,
    ERA5CanonicalConverter,
    ERA5CanonicalConverterConfig,
)
from workers.data_adapters.era5_adapter import ERA5Adapter, ERA5AdapterConfig, MockCDSClient
from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
from workers.forcing_producer import CanonicalProduct, ForcingProducer, ForcingProducerConfig, MetStation
from workers.output_parser import (
    HydroRunContext,
    OutputParser,
    OutputParserConfig,
    RiverSegmentOrder,
    RiverTimeseriesRow,
)
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig


class E2ERepository:
    def __init__(self) -> None:
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}
        self.canonical_products: dict[str, dict[str, Any]] = {}
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.forcing_version: dict[str, Any] | None = None
        self.forcing_components: list[Any] = []
        self.forcing_rows: list[Any] = []
        self.interp_weights: list[Any] = []
        self.hydro_runs: dict[str, dict[str, Any]] = {}
        self.river_timeseries: list[RiverTimeseriesRow] = []
        self.state_snapshots: dict[str, StateSnapshot] = {}
        self.best_available_selections: dict[tuple[datetime, str], BestAvailableSelection] = {}
        self.qc_results: list[Any] = []

    def ensure_data_source(self, **kwargs: Any) -> dict[str, Any]:
        record = dict(kwargs)
        self.data_sources[record["source_id"]] = record
        return record

    def upsert_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["source_id"], _utc(kwargs["cycle_time"]))
        record = self.cycles.setdefault(key, {})
        record.update(kwargs)
        record["cycle_time"] = _utc(record["cycle_time"])
        return dict(record)

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["source_id"], _utc(kwargs["cycle_time"]))
        record = self.cycles.setdefault(
            key,
            {"source_id": kwargs["source_id"], "cycle_time": _utc(kwargs["cycle_time"])},
        )
        for field in ("status", "manifest_uri", "retry_count", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                record[field] = kwargs[field]
        return dict(record)

    def get_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
        record = self.cycles.get((source_id, _utc(cycle_time)))
        return dict(record) if record is not None else None

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        product = self.canonical_products.get(canonical_product_id)
        return dict(product) if product is not None else None

    def upsert_canonical_product(self, record: dict[str, Any]) -> dict[str, Any]:
        self.canonical_products[record["canonical_product_id"]] = dict(record)
        return dict(record)

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        assert model_id == "demo_model"
        return "basin_v1"

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        assert basin_version_id == "basin_v1"
        return (
            MetStation(
                station_id="station_001",
                basin_version_id="basin_v1",
                station_name="Demo Station",
                longitude=110.0,
                latitude=30.0,
                elevation_m=20.0,
                station_role="forcing_proxy",
            ),
        )

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"]),
                quality_flag=str(row.get("quality_flag") or "ok"),
            )
            for row in self.canonical_products.values()
            if row["source_id"] == source_id and _utc(row["cycle_time"]) == _utc(cycle_time)
        )

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: list[str] | tuple[str, ...],
    ) -> tuple[CanonicalProduct, ...]:
        selected: dict[tuple[datetime, str], CanonicalProduct] = {}
        for product in self.list_canonical_products_for_source(source_id=source_id):
            if product.variable not in variables or not _utc(start_time) <= product.valid_time <= _utc(end_time):
                continue
            key = (product.valid_time, product.variable)
            existing = selected.get(key)
            if existing is None or (product.lead_time_hours or 0) < (existing.lead_time_hours or 0):
                selected[key] = product
        return tuple(sorted(selected.values(), key=lambda product: (product.variable, product.valid_time)))

    def list_canonical_products_for_source(self, *, source_id: str) -> tuple[CanonicalProduct, ...]:
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"]),
                quality_flag=str(row.get("quality_flag") or "ok"),
            )
            for row in self.canonical_products.values()
            if row["source_id"] == source_id
        )

    def load_interp_weights(self, *, source_id: str, grid_id: str, model_id: str) -> tuple[Any, ...]:
        return tuple(
            weight
            for weight in self.interp_weights
            if weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id
        )

    def upsert_interp_weights(self, weights: list[Any] | tuple[Any, ...]) -> None:
        self.interp_weights = list(weights)

    def get_forcing_version(self, **_kwargs: Any) -> dict[str, Any] | None:
        for record in self.forcing_versions.values():
            if (
                record.get("source_id") == _kwargs.get("source_id")
                and _utc(record["cycle_time"]) == _utc(_kwargs["cycle_time"])
                and record.get("model_id") == _kwargs.get("model_id")
                and record.get("checksum")
            ):
                return dict(record)
        return None

    def upsert_forcing_version(self, record: dict[str, Any]) -> dict[str, Any]:
        self.forcing_version = dict(record)
        self.forcing_versions[record["forcing_version_id"]] = dict(record)
        return dict(record)

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        record = self.forcing_versions[forcing_version_id]
        record["checksum"] = checksum
        if self.forcing_version and self.forcing_version["forcing_version_id"] == forcing_version_id:
            self.forcing_version = dict(record)
        return dict(record)

    def replace_forcing_components(self, forcing_version_id: str, components: list[Any] | tuple[Any, ...]) -> None:
        assert forcing_version_id in self.forcing_versions
        self.forcing_components = [
            component for component in self.forcing_components if component.forcing_version_id != forcing_version_id
        ]
        self.forcing_components.extend(components)

    def replace_forcing_timeseries(self, forcing_version_id: str, rows: list[Any] | tuple[Any, ...]) -> None:
        assert forcing_version_id in self.forcing_versions
        self.forcing_rows = [row for row in self.forcing_rows if row.forcing_version_id != forcing_version_id]
        self.forcing_rows.extend(rows)

    def create_run(self, manifest: dict[str, Any], run_manifest_uri: str) -> dict[str, Any]:
        record = {
            "run_id": manifest["run_id"],
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest.get("scenario_id", "forecast_gfs_deterministic"),
            "model_id": manifest["model"]["model_id"],
            "basin_version_id": manifest["model"]["basin_version_id"],
            "river_network_version_id": manifest["model"]["river_network_version_id"],
            "forcing_version_id": manifest["forcing"]["forcing_version_id"],
            "init_state_id": (manifest.get("initial_state") or {}).get("state_id"),
            "source_id": manifest["source_id"],
            "cycle_time": _parse_time(manifest["cycle_time"]),
            "start_time": _parse_time(manifest["start_time"]),
            "end_time": _parse_time(manifest["end_time"]),
            "status": "created",
            "run_manifest_uri": run_manifest_uri,
            "output_uri": None,
            "log_uri": None,
            "run_manifest": manifest,
        }
        self.hydro_runs[record["run_id"]] = record
        return dict(record)

    def update_status(self, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        record = self.hydro_runs[run_id]
        record["status"] = status
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        return dict(record)

    def mark_failed(self, run_id: str, error_code: str, error_message: str, **fields: Any) -> dict[str, Any]:
        record = self.hydro_runs[run_id]
        record["status"] = "failed"
        record["error_code"] = error_code
        record["error_message"] = error_message
        record.update({key: value for key, value in fields.items() if value is not None})
        return dict(record)

    def load_run_context(self, run_id: str) -> HydroRunContext:
        run = self.hydro_runs[run_id]
        return HydroRunContext(
            run_id=run_id,
            model_id=run["model_id"],
            basin_version_id=run["basin_version_id"],
            river_network_version_id=run["river_network_version_id"],
            source_id=run["source_id"],
            cycle_id=f"{run['source_id']}_{run['cycle_time'].strftime('%Y%m%d%H')}",
            cycle_time=run["cycle_time"],
            start_time=run["start_time"],
            output_uri=run["output_uri"],
            run_type=run["run_type"],
            scenario_id=run["scenario_id"],
        )

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        assert river_network_version_id == "rivnet_v1"
        return (RiverSegmentOrder("seg_0001", "rivnet_v1", 1),)

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        assert batch_size > 0
        run_ids = {row.run_id for row in rows}
        self.river_timeseries = [row for row in self.river_timeseries if row.run_id not in run_ids]
        self.river_timeseries.extend(rows)

    def insert_qc_result(self, record: Any) -> dict[str, Any]:
        self.qc_results.append(record)
        return {"qc_id": len(self.qc_results)}

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        self.hydro_runs[run_id]["status"] = "parsed"
        return dict(self.hydro_runs[run_id])

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        return self.mark_failed(run_id, error_code, error_message)

    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["variables"] == ["q_down"]
        segment_id = kwargs["segment_id"]
        if kwargs.get("include_analysis"):
            issue_time = self._spliced_issue_time(kwargs["issue_time"])
            if issue_time is None:
                return {
                    "segments": [],
                    "issue_time": None,
                    "river_segment_id": segment_id,
                    "variable": "discharge",
                    "unit": "m3/s",
                }
            analysis_start, analysis_end = analysis_window_for_issue_time(issue_time)
            analysis_rows = self._segment_rows(
                segment_id=segment_id,
                scenario_id="analysis_true_field",
                start_time=analysis_start,
                end_time=analysis_end,
                include_end=False,
            )
            forecast_rows = self._segment_rows(
                segment_id=segment_id,
                scenario_id="forecast_gfs_deterministic",
                start_time=issue_time,
                end_time=issue_time + timedelta(days=7),
                include_end=True,
            )
            return _spliced_response_from_rows(
                river_segment_id=segment_id,
                issue_time=issue_time,
                variable="discharge",
                analysis_rows=analysis_rows,
                forecast_rows=forecast_rows,
            )

        run = max(
            (run for run in self.hydro_runs.values() if run["run_type"] == "forecast"),
            key=lambda item: item["cycle_time"],
        )
        points = [
            [int(row.valid_time.timestamp() * 1000), row.value]
            for row in sorted(self.river_timeseries, key=lambda item: item.valid_time)
            if row.run_id == run["run_id"] and row.river_segment_id == segment_id and row.variable == "q_down"
        ]
        return {
            "segment_id": segment_id,
            "issue_time": _format_time(run["cycle_time"]),
            "unit": "m3/s",
            "series": [
                {
                    "scenario_id": run["scenario_id"],
                    "segment_role": "future_7_days",
                    "points": points,
                }
            ],
            "frequency_thresholds": {},
        }

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        return self.state_snapshots.get(state_id)

    def get_state_snapshot_by_model_time(self, *, model_id: str, valid_time: datetime) -> StateSnapshot | None:
        for snapshot in self.state_snapshots.values():
            if snapshot.model_id == model_id and snapshot.valid_time == _utc(valid_time):
                return snapshot
        return None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        superseded = [
            state_id
            for state_id, existing in self.state_snapshots.items()
            if existing.model_id == snapshot.model_id and existing.valid_time == snapshot.valid_time
        ]
        for state_id in superseded:
            self.state_snapshots.pop(state_id)
        self.state_snapshots[snapshot.state_id] = snapshot
        return snapshot

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
        snapshot = self.state_snapshots.get(state_id)
        if snapshot is None:
            return None
        updated = replace(snapshot, usable_flag=usable_flag)
        self.state_snapshots[state_id] = updated
        return updated

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        candidates = [
            snapshot
            for snapshot in self.state_snapshots.values()
            if snapshot.model_id == model_id and snapshot.usable_flag and snapshot.valid_time <= _utc(before_time)
        ]
        return max(candidates, key=lambda snapshot: snapshot.valid_time) if candidates else None

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        snapshots = list(self.state_snapshots.values())
        if model_id is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.model_id == model_id]
        if usable is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.usable_flag is usable]
        return {
            "total_count": len(snapshots),
            "items": snapshots[offset : offset + limit],
            "limit": limit,
            "offset": offset,
        }

    def list_enabled_sources(self) -> tuple[str, ...]:
        return tuple(source_id.upper() for source_id in self.data_sources)

    def list_forcing_inputs(self, forcing_version_id: str) -> list[ForcingInputSelection]:
        selections: list[ForcingInputSelection] = []
        forcing = self.forcing_versions[forcing_version_id]
        for row in self.forcing_rows:
            if row.forcing_version_id != forcing_version_id:
                continue
            selections.append(
                ForcingInputSelection(
                    valid_time=row.valid_time,
                    variable=row.variable,
                    selected_source=row.source_id,
                    source_cycle_time=forcing["cycle_time"],
                )
            )
        return selections

    def upsert_selection(self, selection: BestAvailableSelection) -> dict[str, Any]:
        self.best_available_selections[(_utc(selection.valid_time), selection.variable)] = selection
        return {
            "valid_time": selection.valid_time,
            "variable": selection.variable,
            "selected_source": selection.selected_source,
            "source_cycle_time": selection.source_cycle_time,
            "fallback_order": list(selection.fallback_order),
            "quality_flag": selection.quality_flag,
        }

    def list_selections(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        variable: str | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for selection in self.best_available_selections.values():
            if not (_utc(from_time) <= selection.valid_time <= _utc(to_time)):
                continue
            if variable is not None and selection.variable != variable:
                continue
            rows.append(
                {
                    "valid_time": selection.valid_time,
                    "variable": selection.variable,
                    "selected_source": selection.selected_source,
                    "source_cycle_time": selection.source_cycle_time,
                    "fallback_order": list(selection.fallback_order),
                    "quality_flag": selection.quality_flag,
                }
            )
        return rows

    def _spliced_issue_time(self, issue_time: str) -> datetime | None:
        if issue_time != "latest":
            return _parse_time(issue_time)
        forecast_times = [run["cycle_time"] for run in self.hydro_runs.values() if run["run_type"] == "forecast"]
        if forecast_times:
            return max(forecast_times)
        analysis_end_times = [run["end_time"] for run in self.hydro_runs.values() if run["run_type"] == "analysis"]
        return max(analysis_end_times) if analysis_end_times else None

    def _segment_rows(
        self,
        *,
        segment_id: str,
        scenario_id: str,
        start_time: datetime,
        end_time: datetime,
        include_end: bool,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self.river_timeseries:
            run = self.hydro_runs[row.run_id]
            if run["scenario_id"] != scenario_id or row.river_segment_id != segment_id or row.variable != "q_down":
                continue
            in_window = (
                start_time <= row.valid_time <= end_time if include_end else start_time <= row.valid_time < end_time
            )
            if not in_window:
                continue
            forcing = self.forcing_versions.get(run["forcing_version_id"], {})
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "source_id": run["source_id"],
                    "lineage_json": forcing.get("lineage_json") or {},
                    "valid_time": row.valid_time,
                    "value": row.value,
                    "unit": row.unit,
                }
            )
        return sorted(rows, key=lambda item: item["valid_time"])


@pytest.mark.asyncio
async def test_m1_forecast_cycle_data_flow_and_api_response(tmp_path: Path) -> None:
    repository = E2ERepository()
    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    store = LocalObjectStore(object_root, "s3://nhms")
    cycle_time = _parse_time("2026-05-07T00:00:00Z")
    forecast_hours = list(range(0, 168, 24))

    manifest = _run_gfs_adapter(repository, store, object_root, cycle_time, forecast_hours)
    grid_definition_uri = _write_grid_definition(store)
    _run_canonical_converter(repository, store, object_root, manifest, grid_definition_uri)
    forcing_result = _run_forcing_producer(repository, store, object_root, cycle_time)
    run_id = _run_shud_runtime(repository, store, object_root, workspace, cycle_time, forcing_result.forcing_version_id)
    _run_output_parser(repository, store, object_root, run_id)

    assert repository.data_sources["gfs"]["native_format"] == "GRIB2"
    assert repository.cycles[("gfs", cycle_time)]["status"] in {"forcing_ready", "raw_complete"}
    assert len(repository.canonical_products) == len(forecast_hours) * 7
    assert repository.forcing_version is not None
    assert repository.forcing_rows
    assert repository.hydro_runs[run_id]["status"] == "parsed"
    assert len(repository.river_timeseries) == 7
    assert {row.variable for row in repository.river_timeseries} == {"q_down"}

    component = repository.forcing_components[0]
    canonical = repository.canonical_products[component.canonical_product_id]
    cycle = repository.cycles[(canonical["source_id"], _utc(canonical["cycle_time"]))]
    assert repository.hydro_runs[run_id]["forcing_version_id"] == repository.forcing_version["forcing_version_id"]
    assert component.forcing_version_id == repository.forcing_version["forcing_version_id"]
    assert canonical["lineage_json"]["source_cycle_id"] == cycle["cycle_id"]
    assert repository.data_sources[cycle["source_id"]]["adapter_name"] == "gfs_adapter"

    app.dependency_overrides[get_forecast_store] = lambda: repository
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_0001/forecast-series"
                "?issue_time=latest&variables=q_down&scenarios=GFS"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["unit"] == "m3/s"
    assert payload["series"][0]["scenario_id"] == "forecast_gfs_deterministic"
    assert len(payload["series"][0]["points"]) == 7
    assert all(len(point) == 2 and isinstance(point[0], int) for point in payload["series"][0]["points"])


@pytest.mark.asyncio
async def test_m2_analysis_warm_start_spliced_curve_and_selection_e2e(tmp_path: Path) -> None:
    repository = E2ERepository()
    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    store = LocalObjectStore(object_root, "s3://nhms")
    analysis_start = _parse_time("2026-04-30T00:00:00Z")
    issue_time = _parse_time("2026-05-01T00:00:00Z")

    era5_manifest = _run_era5_adapter(repository, store, object_root, analysis_start, [0])
    era5_grid_definition_uri = _write_grid_definition(store, "canonical/ERA5/2026-04-30/grid/era5_0p25.json")
    _run_era5_canonical_converter(repository, store, object_root, era5_manifest, era5_grid_definition_uri)
    analysis_forcing = _run_forcing_producer(repository, store, object_root, analysis_start, source_id="ERA5")
    analysis_run_id = _run_shud_runtime(
        repository,
        store,
        object_root,
        workspace,
        analysis_start,
        analysis_forcing.forcing_version_id,
        run_id="analysis_era5_2026043000_2026050100_demo",
        run_type="analysis",
        scenario_id="analysis_true_field",
        source_id="ERA5",
        start_time=analysis_start,
        end_time=issue_time,
        expected_timesteps=1,
    )
    _run_output_parser(repository, store, object_root, analysis_run_id)

    state_manager = StateManager(repository=repository, object_store=store)
    state_result = state_manager.save_state_snapshot(
        model_id="demo_model",
        run_id=analysis_run_id,
        valid_time=issue_time,
        ic_file_path=workspace / "runs" / analysis_run_id / "output" / "demo_model.cfg.ic",
    )
    assert state_manager.run_qc(state_result) is True
    state = repository.state_snapshots[state_result.state_id]

    selections = BestAvailableManager(repository).write_forcing_version(
        analysis_forcing.forcing_version_id,
        now=issue_time,
    )

    forecast_hours = list(range(0, 168, 24))
    gfs_manifest = _run_gfs_adapter(repository, store, object_root, issue_time, forecast_hours)
    gfs_grid_definition_uri = _write_grid_definition(store, "canonical/gfs/2026050100/grid/gfs_0p25.json")
    _run_canonical_converter(repository, store, object_root, gfs_manifest, gfs_grid_definition_uri)
    forecast_forcing = _run_forcing_producer(repository, store, object_root, issue_time, source_id="gfs")
    forecast_run_id = _run_shud_runtime(
        repository,
        store,
        object_root,
        workspace,
        issue_time,
        forecast_forcing.forcing_version_id,
        run_id="run_gfs_2026050100_demo",
        initial_state=state,
    )
    _run_output_parser(repository, store, object_root, forecast_run_id)

    assert store.exists("raw/ERA5/2026-04-30/2m_temperature_00.grib")
    analysis_canonical_sources = {
        row["source_id"] for row in repository.canonical_products.values() if row["cycle_time"] == analysis_start
    }
    analysis_forcing_sources = {
        row.source_id
        for row in repository.forcing_rows
        if row.forcing_version_id == analysis_forcing.forcing_version_id
    }
    assert analysis_canonical_sources == {"ERA5"}
    assert analysis_forcing_sources == {"ERA5"}
    assert repository.hydro_runs[analysis_run_id]["scenario_id"] == "analysis_true_field"
    assert any(
        row.run_id == analysis_run_id and repository.hydro_runs[row.run_id]["scenario_id"] == "analysis_true_field"
        for row in repository.river_timeseries
    )
    assert state.usable_flag is True
    assert repository.qc_results
    assert repository.hydro_runs[forecast_run_id]["init_state_id"] == state.state_id
    assert repository.hydro_runs[forecast_run_id]["run_manifest"]["runtime"]["init_mode"] == 3
    assert repository.hydro_runs[forecast_run_id]["run_manifest"]["initial_state"]["ic_file_uri"] == state.state_uri
    assert selections
    assert {selection.selected_source for selection in selections} == {"ERA5"}
    assert {selection.fallback_order for selection in selections} == {("ERA5",)}

    app.dependency_overrides[get_forecast_store] = lambda: repository
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_0001/forecast-series"
                "?issue_time=2026-05-01T00:00:00Z&variables=q_down&include_analysis=true"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [segment["scenario"] for segment in payload["segments"]] == [
        "analysis_true_field",
        "forecast_gfs_deterministic",
    ]
    analysis_times = {point["valid_time"] for point in payload["segments"][0]["data"]}
    forecast_times = {point["valid_time"] for point in payload["segments"][1]["data"]}
    assert analysis_times == {"2026-04-30T00:00:00Z"}
    assert "2026-05-01T00:00:00Z" in forecast_times
    assert analysis_times.isdisjoint(forecast_times)


def test_frontend_forecast_react_requests_segments_and_configures_echarts() -> None:
    forecast_page = Path("apps/frontend/src/pages/ForecastPage.tsx").read_text(encoding="utf-8")
    forecast_store = Path("apps/frontend/src/stores/forecast.ts").read_text(encoding="utf-8")
    forecast_chart = Path("apps/frontend/src/components/charts/ForecastChart.tsx").read_text(encoding="utf-8")
    forecast_panel = Path("apps/frontend/src/components/forecast/ForecastPanel.tsx").read_text(encoding="utf-8")

    assert "includeAnalysis: true" in forecast_page
    assert "include_analysis" in forecast_store
    assert "payload.segments" in forecast_store or "payload.segments" in forecast_chart
    assert "analysis_true_field" in forecast_store
    assert "#2266cc" in forecast_store
    assert "#ef7d22" in forecast_store
    assert "markLine" in forecast_chart
    assert "起报时间" in forecast_chart
    assert "资料来源" in forecast_panel


def test_forecast_manifest_contains_degraded_initial_state_markers(tmp_path: Path) -> None:
    object_store = LocalObjectStore(tmp_path / "object-store", "s3://nhms")
    orchestrator = ForecastOrchestrator(
        config=ChainOrchestratorConfig(workspace_root=tmp_path / "workspace", object_store_root=tmp_path),
        repository=E2ERepository(),
        object_store=object_store,
    )
    model = ModelContext("demo_model", "yangtze", "basin_v1", "rivnet_v1", 1, "models/demo_model/package/")
    forcing = ForcingContext("forc_gfs_2026050100_demo_model", "forcing/gfs/2026050100/basin_v1/demo_model/")
    cycle_time = _parse_time("2026-05-01T00:00:00Z")

    for quality, init_mode in (
        ("cold_start_no_state", 1),
        ("degraded_stale_init_state", 3),
        ("cold_start_stale_state", 1),
    ):
        selection = InitialStateSelection(
            "state_demo_model_2026042000" if init_mode == 3 else None,
            "states/demo_model/2026042000/state.cfg.ic" if init_mode == 3 else None,
            _parse_time("2026-04-20T00:00:00Z") if init_mode == 3 else None,
            "abc123" if init_mode == 3 else None,
            quality,
        )
        context = orchestrator._build_run_context("gfs", cycle_time, model, forcing, selection)
        manifest = orchestrator._build_run_manifest(context)

        assert manifest["initial_state"]["quality"] == quality
        assert manifest["runtime"]["init_mode"] == init_mode


def _run_gfs_adapter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
    forecast_hours: list[int],
) -> Any:
    payloads_by_url: dict[str, bytes] = {}
    adapter = GFSAdapter(
        config=GFSAdapterConfig(
            source_id="gfs",
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            forecast_start_hour=0,
            forecast_end_hour=144,
            forecast_step_hours=24,
            native_format="GRIB2",
            max_retries=1,
            poll_interval_seconds=0,
        ),
        repository=repository,
        object_store=store,
        downloader=lambda url: payloads_by_url[url],
        sleeper=lambda _seconds: None,
    )
    manifest = adapter.build_manifest(cycle_time, forecast_hours=forecast_hours)
    for entry in manifest.entries:
        payloads_by_url[entry.remote_url] = encode_test_netcdf4(
            entry.variable, entry.forecast_hour, cycle_time=cycle_time
        )
    result = adapter.download_plan(manifest)
    assert result.status == "raw_complete"
    return manifest


def _run_era5_adapter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
    forecast_hours: list[int],
) -> Any:
    adapter = ERA5Adapter(
        config=ERA5AdapterConfig(
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            max_retries=1,
            retry_backoff_seconds=(0,),
            cds_timeout_seconds=5,
        ),
        repository=repository,
        object_store=store,
        cds_client=MockCDSClient(available_dates={cycle_time.date()}),
        sleeper=lambda _seconds: None,
        now=lambda: _parse_time("2026-05-08T00:00:00Z"),
    )
    discoveries = adapter.discover_cycles(cycle_time)
    assert discoveries[0].available is True
    manifest = adapter.build_manifest(cycle_time, forecast_hours=forecast_hours)
    result = adapter.download_plan(manifest)
    assert result.status == "raw_complete"
    return manifest


def _write_grid_definition(store: LocalObjectStore, key: str = "canonical/gfs/2026050700/grid/gfs_0p25.json") -> str:
    return store.write_bytes_atomic(
        key,
        b'{"cells":[{"grid_cell_id":"0","longitude":110.0,"latitude":30.0}]}',
    )


def _run_canonical_converter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    manifest: Any,
    grid_definition_uri: str,
) -> None:
    converter = CanonicalConverter(
        config=CanonicalConverterConfig(
            source_id="gfs",
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            native_time_resolution="24h",
            grid_definition_uri=grid_definition_uri,
        ),
        repository=repository,
        object_store=store,
    )
    result = converter.convert_manifest(manifest.as_dict())
    assert result.status == "canonical_ready"


def _run_era5_canonical_converter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    manifest: Any,
    grid_definition_uri: str,
) -> None:
    converter = ERA5CanonicalConverter(
        config=ERA5CanonicalConverterConfig(
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            native_time_resolution="1h",
            grid_definition_uri=grid_definition_uri,
        ),
        repository=repository,
        object_store=store,
    )
    result = converter.convert_manifest(manifest.as_dict())
    assert result.status == "canonical_ready"


def _run_forcing_producer(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
    *,
    source_id: str = "gfs",
) -> Any:
    producer = ForcingProducer(
        config=ForcingProducerConfig(
            source_id=source_id,
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            idw_neighbors=1,
        ),
        repository=repository,
        object_store=store,
    )
    result = producer.produce(source_id=source_id, cycle_time=cycle_time, model_id="demo_model")
    assert result.status == "forcing_ready"
    return result


def _run_shud_runtime(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    workspace: Path,
    cycle_time: datetime,
    forcing_version_id: str,
    *,
    run_id: str = "run_gfs_2026050700_demo",
    run_type: str = "forecast",
    scenario_id: str = "forecast_gfs_deterministic",
    source_id: str = "gfs",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    expected_timesteps: int = 7,
    initial_state: StateSnapshot | None = None,
) -> str:
    _write_model_package(store)
    mock_executable = Path("tests/mock_shud_omp.py").resolve()
    start = start_time or cycle_time
    end = end_time or cycle_time + timedelta(days=7)
    forcing_version = repository.forcing_versions[forcing_version_id]
    initial_state_payload = {
        "state_id": initial_state.state_id if initial_state else None,
        "ic_file_uri": initial_state.state_uri if initial_state else None,
        "valid_time": _format_time(initial_state.valid_time) if initial_state else None,
        "checksum": initial_state.checksum if initial_state else None,
        "quality": "fresh" if initial_state else "cold_start_no_state",
    }
    runtime = SHUDRuntime(
        config=SHUDRuntimeConfig(
            workspace_root=workspace,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            shud_executable=str(mock_executable),
            output_interval_minutes=1440,
            timeout_seconds=30,
        ),
        repository=repository,
        object_store=store,
    )
    manifest = {
        "run_id": run_id,
        "run_type": run_type,
        "scenario_id": scenario_id,
        "source_id": source_id,
        "cycle_time": _format_time(cycle_time),
        "start_time": _format_time(start),
        "end_time": _format_time(end),
        "expected_timesteps": expected_timesteps,
        "output_interval_minutes": 1440,
        "model": {
            "model_id": "demo_model",
            "project_name": "demo_model",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "rivnet_v1",
            "segment_count": 1,
            "model_package_uri": "s3://nhms/models/demo_model/package/",
        },
        "initial_state": initial_state_payload,
        "forcing": {
            "forcing_version_id": forcing_version_id,
            "forcing_uri": forcing_version["forcing_package_uri"],
        },
        "runtime": {
            "output_interval_minutes": 1440,
            "init_mode": 3 if initial_state else 1,
        },
    }
    result = runtime.execute(manifest)
    assert result.status == "succeeded"
    return run_id


def _write_model_package(store: LocalObjectStore) -> None:
    prefix = "models/demo_model/package"
    store.write_bytes_atomic(f"{prefix}/demo.mesh", b"mesh\n")
    store.write_bytes_atomic(f"{prefix}/demo.calib", b"calib\n")
    store.write_bytes_atomic(
        f"{prefix}/demo.para",
        (
            "START_TIME = {{START_TIME}}\n"
            "END_TIME = {{END_TIME}}\n"
            "OUTPUT_DIR = {{OUTPUT_DIR}}\n"
            "MODEL_OUTPUT_INTERVAL = {{MODEL_OUTPUT_INTERVAL}}\n"
            "SEGMENT_COUNT = {{SEGMENT_COUNT}}\n"
        ).encode("utf-8"),
    )


def _run_output_parser(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    run_id: str,
) -> None:
    parser = OutputParser(
        config=OutputParserConfig(
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            batch_size=100,
        ),
        repository=repository,
        object_store=store,
    )
    result = parser.parse_run(run_id)
    assert result.status == "parsed"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")
