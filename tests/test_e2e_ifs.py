from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.forecast import get_forecast_store
from packages.common.forecast_store import _forecast_response_from_rows, _spliced_response_from_rows
from packages.common.object_store import LocalObjectStore
from packages.common.test_netcdf4 import encode_test_netcdf4
from services.orchestrator.chain import ForcingContext, ForecastOrchestrator, ModelContext, OrchestratorConfig
from tests.test_e2e import E2ERepository, _run_output_parser, _run_shud_runtime, _write_grid_definition
from workers.canonical_converter.converter import IFSCanonicalConverter, IFSCanonicalConverterConfig
from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig
from workers.forcing_producer import ForcingProducer, ForcingProducerConfig


def test_ifs_adapter_canonical_forcing_run_parse_e2e(tmp_path: Path) -> None:
    repository = E2ERepository()
    object_root = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    store = LocalObjectStore(object_root, "s3://nhms")
    cycle_time = _dt("2026-05-01T00:00:00Z")

    manifest = _run_ifs_adapter(repository, store, object_root, cycle_time, forecast_hours=[0, 3])
    grid_definition_uri = _write_grid_definition(store, "canonical/IFS/2026050100/grid/ifs_0p25.json")
    _run_ifs_canonical_converter(repository, store, object_root, manifest, grid_definition_uri)
    forcing = _run_ifs_forcing_producer(repository, store, object_root, cycle_time, max_lead_hours=168)

    assert repository.data_sources["IFS"]["adapter_name"] == "ifs_adapter"
    assert repository.cycles[("IFS", cycle_time)]["status"] == "forcing_ready"
    assert forcing.timestep_count == 2
    assert repository.forcing_versions[forcing.forcing_version_id]["source_id"] == "IFS"

    component_variables = {component.variable for component in repository.forcing_components}
    assert "shortwave_down" in component_variables
    assert "net_radiation" not in component_variables

    valid_time_f003 = cycle_time + timedelta(hours=3)
    precip_product = _canonical_product(repository, variable="prcp_rate_or_amount", valid_time=valid_time_f003)
    assert precip_product["unit"] == "mm/day"
    # canonical per-step 2.0 mm over a 3h step -> 2.0 * 24 / 3 = 16.0 mm/day
    assert _netcdf_scalar(store, precip_product["object_uri"], "prcp_rate_or_amount") == pytest.approx(16.0)
    # producer passes mm/day through unchanged
    assert _forcing_value(repository, forcing.forcing_version_id, "PRCP", valid_time_f003) == pytest.approx(16.0)
    assert _forcing_value(repository, forcing.forcing_version_id, "Rn", valid_time_f003) >= 0.0

    run_id = "fcst_ifs_2026050100_demo_model"
    _run_shud_runtime(
        repository,
        store,
        object_root,
        workspace,
        cycle_time,
        forcing.forcing_version_id,
        run_id=run_id,
        scenario_id="forecast_ifs_deterministic",
        source_id="IFS",
        end_time=cycle_time + timedelta(days=1),
        expected_timesteps=1,
    )
    _run_output_parser(repository, store, object_root, run_id)

    assert repository.hydro_runs[run_id]["status"] == "parsed"
    assert repository.hydro_runs[run_id]["source_id"] == "IFS"
    assert repository.hydro_runs[run_id]["scenario_id"] == "forecast_ifs_deterministic"
    assert [row.variable for row in repository.river_timeseries if row.run_id == run_id] == ["q_down"]


def test_ifs_06z_144h_manifest_context_and_forcing_limit(tmp_path: Path) -> None:
    repository = E2ERepository()
    object_root = tmp_path / "object-store"
    store = LocalObjectStore(object_root, "s3://nhms")
    cycle_time = _dt("2026-05-01T06:00:00Z")
    adapter = _build_ifs_adapter(repository, store, object_root)

    default_manifest = adapter.build_manifest(cycle_time)
    assert default_manifest.metadata["max_lead_hours"] == 144
    assert max(entry.forecast_hour for entry in default_manifest.entries) == 144

    manifest = _run_ifs_adapter(
        repository,
        store,
        object_root,
        cycle_time,
        forecast_hours=[0, 3, 144],
    )
    grid_definition_uri = _write_grid_definition(store, "canonical/IFS/2026050106/grid/ifs_0p25.json")
    _run_ifs_canonical_converter(repository, store, object_root, manifest, grid_definition_uri)
    forcing = _run_ifs_forcing_producer(repository, store, object_root, cycle_time, max_lead_hours=144)

    valid_times = {
        row.valid_time for row in repository.forcing_rows if row.forcing_version_id == forcing.forcing_version_id
    }
    assert max(valid_times) == cycle_time + timedelta(hours=144)
    assert repository.forcing_versions[forcing.forcing_version_id]["lineage_json"]["max_lead_hours"] == 144

    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            source_id="IFS",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
        ),
        repository=object(),
        object_store=store,
    )
    model = ModelContext("demo_model", "basin", "basin_v1", "rivnet_v1", 1, "models/demo_model/package/")
    forcing_context = ForcingContext(
        forcing.forcing_version_id,
        forcing.forcing_package_uri,
        max_lead_hours=144,
    )
    run_context = orchestrator._build_run_context("IFS", cycle_time, model, forcing_context)
    run_manifest = orchestrator._build_run_manifest(run_context)

    assert run_context.run_id == "fcst_ifs_2026050106_demo_model"
    assert run_context.end_time == cycle_time + timedelta(hours=144)
    assert run_context.forecast_horizon_hours == 144
    assert run_manifest["scenario_id"] == "forecast_ifs_deterministic"
    assert run_manifest["forecast_horizon_hours"] == 144


@pytest.mark.asyncio
async def test_ifs_api_multi_source_response_and_single_analysis_segment() -> None:
    store = IfsApiStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            forecast_response = await client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_0001/forecast-series"
                "?river_network_version_id=rivnet_v1&issue_time=latest&variables=q_down&scenarios=GFS,IFS"
            )
            analysis_response = await client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_0001/forecast-series"
                "?river_network_version_id=rivnet_v1&issue_time=latest&variables=q_down&scenarios=GFS,IFS&include_analysis=true"
            )
    finally:
        app.dependency_overrides.clear()

    assert forecast_response.status_code == 200
    forecast_payload = forecast_response.json()
    series_by_scenario = {series["scenario_id"]: series for series in forecast_payload["series"]}
    assert set(series_by_scenario) == {"forecast_gfs_deterministic", "forecast_ifs_deterministic"}
    assert series_by_scenario["forecast_gfs_deterministic"]["source_id"] == "GFS"
    assert series_by_scenario["forecast_gfs_deterministic"]["available_lead_hours"] == 168
    assert series_by_scenario["forecast_ifs_deterministic"]["source_id"] == "IFS"
    assert series_by_scenario["forecast_ifs_deterministic"]["available_lead_hours"] == 144

    assert analysis_response.status_code == 200
    analysis_payload = analysis_response.json()
    analysis_segments = [
        segment for segment in analysis_payload["segments"] if segment["scenario_id"] == "analysis_true_field"
    ]
    forecast_segments = [
        segment for segment in analysis_payload["segments"] if segment["scenario_id"] != "analysis_true_field"
    ]
    assert len(analysis_segments) == 1
    assert {segment["source_id"] for segment in forecast_segments} == {"GFS", "IFS"}
    assert store.calls[-1]["scenarios"] == ["GFS", "IFS"]


def test_frontend_ifs_chart_contract_handles_multi_source_metadata() -> None:
    forecast_store = Path("apps/frontend/src/stores/forecast.ts").read_text(encoding="utf-8")
    forecast_chart = Path("apps/frontend/src/components/charts/ForecastChart.tsx").read_text(encoding="utf-8")

    assert "selectedScenarios.join(',')" in forecast_store
    assert "available_lead_hours" in forecast_store
    assert "source_id" in forecast_store
    assert "data?.series ?? []" in forecast_chart
    assert "#2ca02c" in forecast_store
    assert "#2ca02c" in forecast_chart
    assert "IFS_SIX_DAY_LEAD_HOURS = 144" in forecast_chart
    assert "IFS 6d" in forecast_chart
    assert "markLine" in forecast_chart


class IfsApiStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.issue_time = _dt("2026-05-01T06:00:00Z")
        self.analysis_rows = [
            {
                "scenario_id": "analysis_true_field",
                "source_id": "ERA5",
                "valid_time": _dt("2026-05-01T00:00:00Z"),
                "value": 930.0,
                "unit": "m3/s",
            }
        ]
        self.forecast_rows = [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "GFS",
                "cycle_time": _dt("2026-05-01T00:00:00Z"),
                "run_end_time": _dt("2026-05-08T00:00:00Z"),
                "valid_time": _dt("2026-05-01T00:00:00Z"),
                "value": 1000.0,
                "unit": "m3/s",
            },
            {
                "scenario_id": "forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time": self.issue_time,
                "run_end_time": self.issue_time + timedelta(hours=144),
                "lineage_json": {"max_lead_hours": 144},
                "valid_time": self.issue_time,
                "value": 980.0,
                "unit": "m3/s",
            },
        ]

    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        scenarios = {token.upper() for token in kwargs["scenarios"]}
        rows = [
            row
            for row in self.forecast_rows
            if str(row["source_id"]).upper() in scenarios or str(row["scenario_id"]).upper() in scenarios
        ]
        if kwargs.get("include_analysis"):
            return _spliced_response_from_rows(
                river_segment_id=kwargs["segment_id"],
                issue_time=self.issue_time,
                variable="discharge",
                analysis_rows=self.analysis_rows,
                forecast_rows=rows,
            )
        return _forecast_response_from_rows(segment_id=kwargs["segment_id"], issue_time=self.issue_time, rows=rows)


def _build_ifs_adapter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
) -> IFSAdapter:
    return IFSAdapter(
        config=IFSAdapterConfig(
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            max_retries=1,
            max_wait_seconds=0,
            poll_interval_seconds=0,
        ),
        repository=repository,
        object_store=store,
        downloader=lambda url: b"GRIB mock IFS payload " + url.encode("utf-8"),
        availability_checker=lambda _url: True,
        sleeper=lambda _seconds: None,
    )


def _run_ifs_adapter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
    *,
    forecast_hours: list[int],
) -> Any:
    adapter = _build_ifs_adapter(repository, store, object_root)
    discoveries = adapter.discover_cycles(cycle_time.date().isoformat())
    assert any(discovery.cycle_time == cycle_time and discovery.available for discovery in discoveries)

    manifest = adapter.build_manifest(cycle_time, forecast_hours=forecast_hours)
    result = adapter.download_plan(manifest)
    assert result.status == "raw_complete"
    assert manifest.metadata["max_lead_hours"] == (144 if cycle_time.hour in {6, 18} else 168)

    for entry in manifest.entries:
        store.write_bytes_atomic(
            entry.local_key,
            encode_test_netcdf4(
                entry.variable,
                entry.forecast_hour,
                values=[_ifs_raw_value(entry.variable, entry.forecast_hour)],
                cycle_time=cycle_time,
                source="IFS",
                longitudes=[110.0],
                latitudes=[30.0],
            ),
        )
    return manifest


def _run_ifs_canonical_converter(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    manifest: Any,
    grid_definition_uri: str,
) -> None:
    converter = IFSCanonicalConverter(
        config=IFSCanonicalConverterConfig(
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            grid_definition_uri=grid_definition_uri,
        ),
        repository=repository,
        object_store=store,
    )
    result = converter.convert_manifest(manifest.as_dict())
    assert result.status == "canonical_ready"


def _run_ifs_forcing_producer(
    repository: E2ERepository,
    store: LocalObjectStore,
    object_root: Path,
    cycle_time: datetime,
    *,
    max_lead_hours: int,
) -> Any:
    producer = ForcingProducer(
        config=ForcingProducerConfig(
            source_id="IFS",
            workspace_root=object_root,
            object_store_prefix="s3://nhms",
            idw_neighbors=1,
        ),
        repository=repository,
        object_store=store,
    )
    result = producer.produce(
        source_id="IFS",
        cycle_time=cycle_time,
        model_id="demo_model",
        max_lead_hours=max_lead_hours,
    )
    assert result.status == "forcing_ready"
    return result


def _ifs_raw_value(variable: str, forecast_hour: int) -> float:
    if variable == "2t":
        return 293.15 + forecast_hour * 0.01
    if variable == "2d":
        return 288.15 + forecast_hour * 0.01
    if variable == "10u":
        return 3.0
    if variable == "10v":
        return 4.0
    if variable == "tp":
        return 0.002 * (forecast_hour / 3.0)
    if variable == "sp":
        return 100000.0
    if variable == "ssr":
        return forecast_hour * 3600.0 * 300.0
    if variable == "str":
        return forecast_hour * 3600.0 * -50.0
    raise ValueError(f"Unsupported IFS variable: {variable}")


def _canonical_product(repository: E2ERepository, *, variable: str, valid_time: datetime) -> dict[str, Any]:
    matches = [
        row
        for row in repository.canonical_products.values()
        if row["variable"] == variable and _ensure_utc(row["valid_time"]) == _ensure_utc(valid_time)
    ]
    assert len(matches) == 1
    return matches[0]


def _forcing_value(repository: E2ERepository, forcing_version_id: str, variable: str, valid_time: datetime) -> float:
    matches = [
        row.value
        for row in repository.forcing_rows
        if row.forcing_version_id == forcing_version_id
        and row.variable == variable
        and _ensure_utc(row.valid_time) == _ensure_utc(valid_time)
    ]
    assert len(matches) == 1
    return matches[0]


def _netcdf_scalar(store: LocalObjectStore, object_uri: str, variable: str) -> float:
    import xarray as xr

    dataset = xr.open_dataset(store.resolve_path(object_uri))
    try:
        return float(dataset[variable].values.ravel()[0])
    finally:
        dataset.close()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
