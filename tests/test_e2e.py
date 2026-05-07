from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.forecast import get_forecast_store
from packages.common.mock_grib import build_mock_payload, encode_mock_grib2
from packages.common.object_store import LocalObjectStore
from workers.canonical_converter.converter import CanonicalConverter, CanonicalConverterConfig
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
        self.forcing_version: dict[str, Any] | None = None
        self.forcing_components: list[Any] = []
        self.forcing_rows: list[Any] = []
        self.interp_weights: list[Any] = []
        self.hydro_runs: dict[str, dict[str, Any]] = {}
        self.river_timeseries: list[RiverTimeseriesRow] = []
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

    def load_interp_weights(self, *, source_id: str, grid_id: str, model_id: str) -> tuple[Any, ...]:
        return tuple(
            weight
            for weight in self.interp_weights
            if weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id
        )

    def upsert_interp_weights(self, weights: list[Any] | tuple[Any, ...]) -> None:
        self.interp_weights = list(weights)

    def get_forcing_version(self, **_kwargs: Any) -> dict[str, Any] | None:
        return dict(self.forcing_version) if self.forcing_version and self.forcing_version.get("checksum") else None

    def upsert_forcing_version(self, record: dict[str, Any]) -> dict[str, Any]:
        self.forcing_version = dict(record)
        return dict(record)

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        assert self.forcing_version is not None
        assert self.forcing_version["forcing_version_id"] == forcing_version_id
        self.forcing_version["checksum"] = checksum
        return dict(self.forcing_version)

    def replace_forcing_components(self, forcing_version_id: str, components: list[Any] | tuple[Any, ...]) -> None:
        assert self.forcing_version is not None
        assert self.forcing_version["forcing_version_id"] == forcing_version_id
        self.forcing_components = list(components)

    def replace_forcing_timeseries(self, forcing_version_id: str, rows: list[Any] | tuple[Any, ...]) -> None:
        assert self.forcing_version is not None
        assert self.forcing_version["forcing_version_id"] == forcing_version_id
        self.forcing_rows = list(rows)

    def create_run(self, manifest: dict[str, Any], run_manifest_uri: str) -> dict[str, Any]:
        record = {
            "run_id": manifest["run_id"],
            "run_type": manifest.get("run_type", "forecast"),
            "scenario_id": manifest.get("scenario_id", "forecast_gfs_deterministic"),
            "model_id": manifest["model"]["model_id"],
            "basin_version_id": manifest["model"]["basin_version_id"],
            "river_network_version_id": manifest["model"]["river_network_version_id"],
            "forcing_version_id": manifest["forcing"]["forcing_version_id"],
            "source_id": manifest["source_id"],
            "cycle_time": _parse_time(manifest["cycle_time"]),
            "start_time": _parse_time(manifest["start_time"]),
            "end_time": _parse_time(manifest["end_time"]),
            "status": "created",
            "run_manifest_uri": run_manifest_uri,
            "output_uri": None,
            "log_uri": None,
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
        )

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        assert river_network_version_id == "rivnet_v1"
        return (RiverSegmentOrder("seg_0001", "rivnet_v1", 1),)

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        assert batch_size > 0
        self.river_timeseries = list(rows)

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
        run = next(iter(self.hydro_runs.values()))
        points = [
            [int(row.valid_time.timestamp() * 1000), row.value]
            for row in sorted(self.river_timeseries, key=lambda item: item.valid_time)
            if row.river_segment_id == segment_id and row.variable == "q_down"
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
        payloads_by_url[entry.remote_url] = encode_mock_grib2(
            build_mock_payload(cycle_time, entry.variable, entry.forecast_hour)
        )
    result = adapter.download_plan(manifest)
    assert result.status == "raw_complete"
    return manifest


def _write_grid_definition(store: LocalObjectStore) -> str:
    return store.write_bytes_atomic(
        "canonical/gfs/2026050700/grid/gfs_0p25.json",
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


def _run_forcing_producer(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
) -> Any:
    producer = ForcingProducer(
        config=ForcingProducerConfig(
            source_id="gfs",
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            idw_neighbors=1,
        ),
        repository=repository,
        object_store=store,
    )
    result = producer.produce(source_id="gfs", cycle_time=cycle_time, model_id="demo_model")
    assert result.status == "forcing_ready"
    return result


def _run_shud_runtime(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    workspace: Path,
    cycle_time: datetime,
    forcing_version_id: str,
) -> str:
    _write_model_package(store)
    run_id = "run_gfs_2026050700_demo"
    mock_executable = Path("workers/shud_runtime/mock_shud_omp.py").resolve()
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
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": "gfs",
        "cycle_time": _format_time(cycle_time),
        "start_time": _format_time(cycle_time),
        "end_time": _format_time(cycle_time + timedelta(days=7)),
        "expected_timesteps": 7,
        "output_interval_minutes": 1440,
        "model": {
            "model_id": "demo_model",
            "project_name": "demo_model",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "rivnet_v1",
            "segment_count": 1,
            "model_package_uri": "s3://nhms/models/demo_model/package/",
        },
        "forcing": {
            "forcing_version_id": forcing_version_id,
            "forcing_uri": repository.forcing_version["forcing_package_uri"],
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
