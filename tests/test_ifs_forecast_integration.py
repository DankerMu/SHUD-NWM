from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from services.orchestrator.chain import (
    ForcingContext,
    ForecastOrchestrator,
    ModelContext,
    OrchestratorConfig,
    _auto_trigger_source_object_identity,
    _auto_trigger_source_policy_identity,
    scenario_for_source,
)
from workers.canonical_converter.converter import IFS_REQUIRED_STANDARD_VARIABLES
from workers.data_adapters.base import format_cycle_time
from workers.forcing_producer import (
    CanonicalProduct,
    ForcingProducer,
    ForcingProducerConfig,
    InterpolationWeight,
    MetStation,
    parse_cycle_time,
)
from workers.forcing_producer.producer import ForcingComponent, ForcingTimeseriesRow
from workers.forcing_producer.store import PsycopgForcingRepository


def test_scenario_for_source_maps_forecast_sources() -> None:
    assert scenario_for_source("GFS") == "forecast_gfs_deterministic"
    assert scenario_for_source("IFS") == "forecast_ifs_deterministic"
    assert scenario_for_source("custom") == "forecast_custom_deterministic"


def test_build_run_context_uses_ifs_run_id_and_144h_horizon_for_06z(tmp_path: Path) -> None:
    orchestrator = _build_context_orchestrator(tmp_path, source_id="IFS")
    cycle_time = _dt("2026-05-07T06:00:00Z")
    model = _model()
    forcing = ForcingContext("forc_ifs_2026050706_demo_model", "forcing/ifs/2026050706/basin_v1/demo_model/")

    context = orchestrator._build_run_context("IFS", cycle_time, model, forcing)
    manifest = orchestrator._build_run_manifest(context)

    assert context.run_id == "fcst_ifs_2026050706_demo_model"
    assert context.end_time == cycle_time + timedelta(hours=144)
    assert context.forecast_horizon_hours == 144
    assert manifest["scenario_id"] == "forecast_ifs_deterministic"
    assert manifest["forecast_horizon_hours"] == 144


def test_ifs_forcing_uses_surface_pressure_shortwave_and_precip_conversion(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    cycle_time = parse_cycle_time("2026050706")
    products = _write_ifs_products(store, cycle_time=cycle_time, forecast_hours=(0, 3))
    repository = FakeForcingRepository(products=products)
    producer = _build_forcing_producer(tmp_path, repository, store)

    result = producer.produce(source_id="IFS", cycle_time=cycle_time, model_id="demo_model")

    assert result.status == "forcing_ready"
    assert result.forcing_version_id == "forc_ifs_2026050706_demo_model"
    assert result.timestep_count == 2
    component_variables = {component.variable for component in repository.components}
    assert "surface_pressure" in component_variables
    assert "pressure_surface" not in component_variables
    assert "shortwave_down" in component_variables
    assert "net_radiation" not in component_variables
    assert _row_value(repository.timeseries, "PRCP", cycle_time + timedelta(hours=3)) == pytest.approx(16.0)
    assert _row_value(repository.timeseries, "Rn", cycle_time + timedelta(hours=3)) == pytest.approx(500.0)
    assert _row_value(repository.timeseries, "Press", cycle_time + timedelta(hours=3)) == pytest.approx(100000.0)


def test_fallback_query_uses_stripped_nonblank_checksum_predicate() -> None:
    class CapturingRepository(PsycopgForcingRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")
            self.statement = ""

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            self.statement = statement
            assert parameters == (
                "gfs",
                parse_cycle_time("2026050700"),
                parse_cycle_time("2026050703"),
                ["shortwave_down"],
            )
            return []

    repository = CapturingRepository()

    assert (
        repository.list_fallback_canonical_products(
            source_id="gfs",
            start_time=parse_cycle_time("2026050700"),
            end_time=parse_cycle_time("2026050703"),
            variables=["shortwave_down"],
        )
        == ()
    )
    assert "NULLIF(BTRIM(cmp.checksum), '') IS NOT NULL" in repository.statement
    assert "cmp.checksum <> ''" not in repository.statement


def test_ifs_max_lead_hours_limits_forcing_range(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    cycle_time = parse_cycle_time("2026050718")
    products = _write_ifs_products(store, cycle_time=cycle_time, forecast_hours=(0, 3, 144, 147))
    repository = FakeForcingRepository(products=products)
    producer = _build_forcing_producer(tmp_path, repository, store)

    result = producer.produce(
        source_id="IFS",
        cycle_time=cycle_time,
        model_id="demo_model",
        max_lead_hours=144,
    )

    valid_times = {row.valid_time for row in repository.timeseries}
    assert result.timestep_count == 3
    assert max(valid_times) == cycle_time + timedelta(hours=144)
    assert cycle_time + timedelta(hours=147) not in valid_times
    assert repository.forcing_versions[result.forcing_version_id]["lineage_json"]["max_lead_hours"] == 144


def test_ifs_max_lead_filter_runs_before_completeness_validation(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    cycle_time = parse_cycle_time("2026050718")
    products = tuple(
        product
        for product in _write_ifs_products(store, cycle_time=cycle_time, forecast_hours=(0, 3, 144, 147))
        if not (product.lead_time_hours == 147 and product.variable == "net_radiation")
    )
    repository = FakeForcingRepository(products=products)
    producer = _build_forcing_producer(tmp_path, repository, store)

    result = producer.produce(
        source_id="IFS",
        cycle_time=cycle_time,
        model_id="demo_model",
        max_lead_hours=144,
    )

    assert result.status == "forcing_ready"
    assert result.timestep_count == 3


def test_gfs_and_ifs_contexts_produce_independent_runs(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-07T00:00:00Z")
    model = _model()
    forcing = ForcingContext(None, None)
    gfs_orchestrator = _build_context_orchestrator(tmp_path / "gfs", source_id="GFS")
    ifs_orchestrator = _build_context_orchestrator(tmp_path / "ifs", source_id="IFS")

    gfs_context = gfs_orchestrator._build_run_context("GFS", cycle_time, model, forcing)
    ifs_context = ifs_orchestrator._build_run_context("IFS", cycle_time, model, forcing)

    assert gfs_context.run_id == "fcst_gfs_2026050700_demo_model"
    assert ifs_context.run_id == "fcst_ifs_2026050700_demo_model"
    assert gfs_orchestrator._build_run_manifest(gfs_context)["scenario_id"] == "forecast_gfs_deterministic"
    assert ifs_orchestrator._build_run_manifest(ifs_context)["scenario_id"] == "forecast_ifs_deterministic"


def test_explicit_scenario_id_override_is_preserved_for_ifs_context(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        source_id="GFS",
        scenario_id="custom_forecast_scenario",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    orchestrator = ForecastOrchestrator(
        config=config,
        repository=NoopRepository(),
        object_store=LocalObjectStore(object_root),
        slurm_client=ImmediateSlurmClient(),
    )

    context = orchestrator._build_run_context(
        "IFS",
        _dt("2026-05-07T00:00:00Z"),
        _model(),
        ForcingContext(None, None),
    )

    assert orchestrator._build_run_manifest(context)["scenario_id"] == "custom_forecast_scenario"


def test_ifs_canonical_ready_auto_trigger_starts_at_forecast_stage(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-07T06:00:00Z")
    object_root = tmp_path / "object-store"
    repository = FakeReadyRepository(
        source_id="IFS",
        cycle_time=cycle_time,
        max_lead_hours=144,
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
    )
    slurm_client = ImmediateSlurmClient()
    orchestrator = _build_ready_orchestrator(tmp_path, repository, slurm_client)

    results = orchestrator.trigger_ready_forecasts(source_id="IFS")

    assert len(results) == 1
    assert results[0].run_id == "fcst_ifs_2026050706_demo_model"
    assert [payload["manifest"]["stage"] for payload in slurm_client.submissions] == [
        "run_shud_forecast",
        "parse_output",
    ]
    context, manifest = repository.created_runs[0]
    assert context.end_time == cycle_time + timedelta(hours=144)
    assert manifest["scenario_id"] == "forecast_ifs_deterministic"


def test_auto_trigger_skips_completed_ifs_cycle(tmp_path: Path) -> None:
    cycle_time = _dt("2026-05-07T06:00:00Z")
    object_root = tmp_path / "object-store"
    repository = FakeReadyRepository(
        source_id="IFS",
        cycle_time=cycle_time,
        max_lead_hours=144,
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        completed=True,
    )
    slurm_client = ImmediateSlurmClient()
    orchestrator = _build_ready_orchestrator(tmp_path, repository, slurm_client)

    assert orchestrator.trigger_ready_forecasts(source_id="IFS") == ()
    assert slurm_client.submissions == []


class FakeForcingRepository:
    def __init__(self, *, products: Sequence[CanonicalProduct]) -> None:
        self.products = tuple(products)
        self.station = MetStation(
            "station_1",
            "basin_v1",
            0.0,
            0.0,
            10.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
        )
        self.interp_weights: list[InterpolationWeight] = []
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.components: list[ForcingComponent] = []
        self.timeseries: list[ForcingTimeseriesRow] = []
        self.cycle_updates: list[dict[str, Any]] = []

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        assert model_id == "demo_model"
        return "basin_v1"

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        assert basin_version_id == "basin_v1"
        return (self.station,)

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        return tuple(
            product for product in self.products if product.source_id == source_id and product.cycle_time == cycle_time
        )

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]:
        del source_id, start_time, end_time, variables
        return ()

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        return tuple(
            weight
            for weight in self.interp_weights
            if weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id
        )

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        self.interp_weights.extend(weights)

    def get_forcing_version(self, *, source_id: str, cycle_time: datetime, model_id: str) -> dict[str, Any] | None:
        for record in self.forcing_versions.values():
            if (
                record["source_id"] == source_id
                and record["cycle_time"] == cycle_time
                and record["model_id"] == model_id
            ):
                return dict(record)
        return None

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        stored = dict(record)
        self.forcing_versions[str(stored["forcing_version_id"])] = stored
        return stored

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        self.forcing_versions[forcing_version_id]["checksum"] = checksum
        return dict(self.forcing_versions[forcing_version_id])

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None:
        self.components = [
            component for component in self.components if component.forcing_version_id != forcing_version_id
        ]
        self.components.extend(components)

    def replace_forcing_timeseries(self, forcing_version_id: str, rows: Sequence[ForcingTimeseriesRow]) -> None:
        self.timeseries = [row for row in self.timeseries if row.forcing_version_id != forcing_version_id]
        self.timeseries.extend(rows)

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.cycle_updates.append(dict(kwargs))
        return dict(kwargs)


class FakeReadyRepository:
    def __init__(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        max_lead_hours: int,
        workspace_root: Path,
        object_store_root: Path,
        object_store_prefix: str = "",
        completed: bool = False,
    ) -> None:
        self.source_id = source_id
        self.cycle_time = cycle_time
        self.max_lead_hours = max_lead_hours
        self.workspace_root = workspace_root
        self.object_store_root = object_store_root
        self.object_store_prefix = object_store_prefix
        self.completed = completed
        self.model = _model()
        self.created_runs: list[tuple[Any, dict[str, Any]]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.hydro_statuses: list[str] = []
        self.cycle_statuses: list[str] = []

    def list_canonical_ready_cycles(self, *, source_id: str | None, limit: int) -> list[dict[str, Any]]:
        del limit
        if source_id not in (None, self.source_id):
            return []
        return [
            {
                "source_id": self.source_id,
                "cycle_time": self.cycle_time,
                "max_lead_hours": self.max_lead_hours,
                "canonical_products": self._canonical_products(),
            }
        ]

    def list_forecast_model_ids(self) -> list[str]:
        return [self.model.model_id]

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        assert (source_id, cycle_time, model_id) == (self.source_id, self.cycle_time, self.model.model_id)
        return self.completed

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        assert (source_id, cycle_time, model_id) == (self.source_id, self.cycle_time, self.model.model_id)
        return False

    def load_model_context(self, model_id: str) -> ModelContext:
        assert model_id == self.model.model_id
        return self.model

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        assert (source_id, cycle_time, model_id) == (self.source_id, self.cycle_time, self.model.model_id)
        return ForcingContext(
            f"forc_{source_id.lower()}_{cycle_time:%Y%m%d%H}_{model_id}",
            f"forcing/{source_id.lower()}/{cycle_time:%Y%m%d%H}/basin_v1/{model_id}/",
            max_lead_hours=self.max_lead_hours,
        )

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        assert (source_id, cycle_time) == (self.source_id, self.cycle_time)
        return {"source_id": source_id, "cycle_time": cycle_time}

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        self.created_runs.append((context, manifest))
        return {"run_id": context.run_id, "status": "created"}

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        del slurm_job_id, error_code, error_message
        assert run_id == "fcst_ifs_2026050706_demo_model"
        self.hydro_statuses.append(status)
        return {"run_id": run_id, "status": status}

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.jobs[record["job_id"]] = dict(record)
        return dict(record)

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        del started_at, finished_at, exit_code, error_code, error_message
        previous = self.jobs[job_id]["status"]
        self.jobs[job_id]["status"] = status
        if log_uri is not None:
            self.jobs[job_id]["log_uri"] = log_uri
        return previous, dict(self.jobs[job_id])

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
        }

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        del error_code, error_message
        assert (source_id, cycle_time) == (self.source_id, self.cycle_time)
        self.cycle_statuses.append(status)
        return {"source_id": source_id, "cycle_time": cycle_time, "status": status}

    def _canonical_products(self) -> list[dict[str, Any]]:
        forecast_hours = list(range(0, self.max_lead_hours + 1, 3))
        policy_identity = _auto_trigger_source_policy_identity(
            source_id=self.source_id,
            cycle_time=self.cycle_time,
            forecast_hours=forecast_hours,
            workspace_root=self.workspace_root,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
        )
        source_object_identity = _auto_trigger_source_object_identity(
            source_id=self.source_id,
            cycle_time=self.cycle_time,
            forecast_hours=forecast_hours,
            workspace_root=self.workspace_root,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
        )
        compact_cycle = format_cycle_time(self.cycle_time)
        rows: list[dict[str, Any]] = []
        for forecast_hour in forecast_hours:
            for variable in IFS_REQUIRED_STANDARD_VARIABLES:
                rows.append(
                    {
                        "canonical_product_id": (
                            f"{self.source_id}_{compact_cycle}_{variable}_f{forecast_hour:03d}"
                        ),
                        "source_id": self.source_id,
                        "cycle_time": self.cycle_time,
                        "valid_time": self.cycle_time + timedelta(hours=forecast_hour),
                        "lead_time_hours": forecast_hour,
                        "variable": variable,
                        "checksum": f"sha256:{variable}:{forecast_hour}",
                        "quality_flag": "ok",
                        "lineage_json": {
                            "policy_identity": policy_identity,
                            "source_object_identity": source_object_identity,
                            "source_cycle_id": f"{self.source_id}_{compact_cycle}",
                        },
                    }
                )
        return rows


class ImmediateSlurmClient:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submissions.append(payload)
        stage = payload["manifest"]["stage"]
        return {
            "job_id": f"job_{len(self.submissions)}",
            "status": "succeeded",
            "stage": stage,
            "submitted_at": _fmt(_dt("2026-05-07T06:00:00Z")),
            "started_at": _fmt(_dt("2026-05-07T06:01:00Z")),
            "finished_at": _fmt(_dt("2026-05-07T06:02:00Z")),
            "exit_code": 0,
        }

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        raise AssertionError(f"terminal job should not be polled: {job_id}")

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "logs": "ok"}


class NoopRepository:
    pass


def _build_context_orchestrator(tmp_path: Path, *, source_id: str) -> ForecastOrchestrator:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        source_id=source_id,
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return ForecastOrchestrator(
        config=config,
        repository=NoopRepository(),
        object_store=LocalObjectStore(object_root),
        slurm_client=ImmediateSlurmClient(),
    )


def _build_ready_orchestrator(
    tmp_path: Path,
    repository: FakeReadyRepository,
    slurm_client: ImmediateSlurmClient,
) -> ForecastOrchestrator:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        source_id="IFS",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return ForecastOrchestrator(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(object_root),
        slurm_client=slurm_client,
    )


def _build_forcing_producer(
    tmp_path: Path,
    repository: FakeForcingRepository,
    store: LocalObjectStore,
) -> ForcingProducer:
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=1)
    return ForcingProducer(config=config, repository=repository, object_store=store)


def _write_ifs_products(
    store: LocalObjectStore,
    *,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
) -> tuple[CanonicalProduct, ...]:
    values_by_variable = {
        # Canonical IFS PRCP is now mm/day: the converter already applied 24 / step_hours
        # (per-step 2.0 mm over a 3h step -> 16.0 mm/day). Producer passes it through.
        "prcp_rate_or_amount": ("mm/day", 16.0),
        "air_temperature_2m": ("degC", 20.0),
        "relative_humidity_2m": ("0-1", 0.6),
        "wind_u_10m": ("m/s", 3.0),
        "wind_v_10m": ("m/s", 4.0),
        "surface_pressure": ("Pa", 100000.0),
        "net_radiation": ("W/m2", -50.0),
        "shortwave_down": ("W/m2", 500.0),
    }
    products: list[CanonicalProduct] = []
    compact_cycle = cycle_time.strftime("%Y%m%d%H")
    for forecast_hour in forecast_hours:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        for variable, (unit, value) in values_by_variable.items():
            product_id = f"IFS_{compact_cycle}_{variable}_f{forecast_hour:03d}"
            content = _netcdf_bytes(variable, value)
            object_uri = store.write_bytes_atomic(
                f"canonical/IFS/{compact_cycle}/{variable}/{product_id}.nc",
                content,
            )
            products.append(
                CanonicalProduct(
                    canonical_product_id=product_id,
                    source_id="IFS",
                    cycle_time=cycle_time,
                    valid_time=valid_time,
                    variable=variable,
                    unit=unit,
                    grid_id="ifs_grid",
                    object_uri=object_uri,
                    checksum=sha256_bytes(content),
                    native_time_resolution="3h",
                    native_spatial_resolution="0.25deg",
                    lead_time_hours=forecast_hour,
                )
            )
    return tuple(products)


def _netcdf_bytes(variable: str, value: float) -> bytes:
    import xarray as xr

    dataset = xr.Dataset(
        data_vars={variable: ("point", [value])},
        coords={
            "point": [0],
            "longitude": ("point", [0.0]),
            "latitude": ("point", [0.0]),
        },
    )
    try:
        with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
            dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
            temp_file.seek(0)
            return temp_file.read()
    finally:
        dataset.close()


def _row_value(rows: Sequence[ForcingTimeseriesRow], variable: str, valid_time: datetime) -> float:
    matches = [row.value for row in rows if row.variable == variable and row.valid_time == valid_time]
    assert len(matches) == 1
    return matches[0]


def _model() -> ModelContext:
    return ModelContext(
        model_id="demo_model",
        basin_id="basin",
        basin_version_id="basin_v1",
        river_network_version_id="rivnet_v1",
        segment_count=1,
        model_package_uri="models/demo_model/package/",
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
