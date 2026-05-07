from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from workers.forcing_producer import (
    CanonicalProduct,
    ForcingProducer,
    ForcingProducerConfig,
    ForcingProductionError,
    GridPoint,
    InterpolationWeight,
    MetStation,
    compute_idw_weights,
    parse_cycle_time,
    wind_speed,
)
from workers.forcing_producer.producer import ForcingComponent, ForcingTimeseriesRow


class FakeForcingRepository:
    def __init__(self, *, stations: tuple[MetStation, ...], products: tuple[CanonicalProduct, ...]) -> None:
        self.basin_by_model = {"demo_model": "basin_v1"}
        self.stations = stations
        self.products = products
        self.interp_weights: list[InterpolationWeight] = []
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.components: list[ForcingComponent] = []
        self.timeseries: list[ForcingTimeseriesRow] = []
        self.cycle_updates: list[dict[str, Any]] = []
        self.upsert_count = 0

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        return self.basin_by_model[model_id]

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        return tuple(station for station in self.stations if station.basin_version_id == basin_version_id)

    def list_canonical_products(self, *, source_id: str, cycle_time: Any) -> tuple[CanonicalProduct, ...]:
        return tuple(
            product
            for product in self.products
            if product.source_id == source_id and product.cycle_time == cycle_time
        )

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

    def upsert_interp_weights(self, weights: list[InterpolationWeight] | tuple[InterpolationWeight, ...]) -> None:
        existing_keys = {
            (weight.source_id, weight.grid_id, weight.model_id, weight.station_id, weight.variable, weight.grid_cell_id)
            for weight in self.interp_weights
        }
        for weight in weights:
            key = (
                weight.source_id,
                weight.grid_id,
                weight.model_id,
                weight.station_id,
                weight.variable,
                weight.grid_cell_id,
            )
            if key not in existing_keys:
                self.interp_weights.append(weight)
                existing_keys.add(key)

    def get_forcing_version(self, *, source_id: str, cycle_time: Any, model_id: str) -> dict[str, Any] | None:
        for record in self.forcing_versions.values():
            if (
                record["source_id"] == source_id
                and record["cycle_time"] == cycle_time
                and record["model_id"] == model_id
            ):
                return dict(record)
        return None

    def upsert_forcing_version(self, record: dict[str, Any]) -> dict[str, Any]:
        self.upsert_count += 1
        self.forcing_versions[record["forcing_version_id"]] = dict(record)
        return self.forcing_versions[record["forcing_version_id"]]

    def replace_forcing_components(
        self,
        forcing_version_id: str,
        components: list[ForcingComponent] | tuple[ForcingComponent, ...],
    ) -> None:
        self.components = [
            component for component in self.components if component.forcing_version_id != forcing_version_id
        ]
        self.components.extend(components)

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: list[ForcingTimeseriesRow] | tuple[ForcingTimeseriesRow, ...],
    ) -> None:
        self.timeseries = [row for row in self.timeseries if row.forcing_version_id != forcing_version_id]
        self.timeseries.extend(rows)

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.cycle_updates.append(dict(kwargs))
        return dict(kwargs)


def test_idw_weights_are_normalized_for_station() -> None:
    station = MetStation("station_1", "basin_v1", 0.5, 0.0, 10.0, "forcing_proxy")
    grid_points = (
        GridPoint("g0", 0.0, 0.0),
        GridPoint("g1", 1.0, 0.0),
        GridPoint("g2", 2.0, 0.0),
    )

    weights = compute_idw_weights(
        stations=(station,),
        grid_points=grid_points,
        variables=("PRCP",),
        source_id="gfs",
        grid_id="grid_a",
        model_id="demo_model",
        neighbors=3,
    )

    assert len(weights) == 3
    assert sum(weight.weight for weight in weights) == pytest.approx(1.0, abs=1e-6)
    assert all(weight.weight >= 0.0 for weight in weights)


def test_wind_speed_uses_square_root_of_component_squares() -> None:
    assert wind_speed(3.0, 4.0) == pytest.approx(5.0)


def test_produce_writes_tsd_forc_with_header_columns_and_rows(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    content = store.read_bytes(result.file_uris["tsd_forc"]).decode("utf-8")
    lines = content.splitlines()
    assert lines[0].split(",") == ["valid_time", "variable", "station_1"]
    assert len(lines) == 1 + 2 * 6
    assert all(len(line.split(",")) == 3 for line in lines[1:])
    assert lines[1].startswith("2026-05-07T00:00:00Z,PRCP,")
    assert store.read_bytes(result.file_uris["csv_debug"]).decode("utf-8").splitlines()[0] == (
        "valid_time,station_id,variable,value,unit"
    )


def test_forcing_timeseries_long_table_rows_have_composite_pk_shape(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    keys = {
        (row.forcing_version_id, row.station_id, row.variable, row.valid_time)
        for row in repository.timeseries
    }
    assert result.status == "forcing_ready"
    assert len(repository.timeseries) == 1 * 6 * 2
    assert len(keys) == len(repository.timeseries)
    assert {row.variable for row in repository.timeseries} == {"PRCP", "TEMP", "RH", "wind", "Rn", "Press"}
    assert len(repository.components) == 7 * 2
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


def test_produce_is_idempotent_when_existing_checksum_is_valid(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    row_count = len(repository.timeseries)
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "already_done"
    assert repository.upsert_count == 1
    assert len(repository.forcing_versions) == 1
    assert len(repository.timeseries) == row_count


def test_invalid_existing_forcing_version_is_replaced_not_duplicated(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)
    cycle_time = parse_cycle_time("2026050700")
    repository.forcing_versions["stale_forcing_version"] = {
        "forcing_version_id": "stale_forcing_version",
        "model_id": "demo_model",
        "source_id": "gfs",
        "cycle_time": cycle_time,
        "start_time": cycle_time,
        "end_time": cycle_time,
        "station_count": 1,
        "forcing_package_uri": "forcing/gfs/2026050700/basin_v1/demo_model/",
        "checksum": None,
        "lineage_json": {},
    }

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert result.forcing_version_id == "stale_forcing_version"
    assert list(repository.forcing_versions) == ["stale_forcing_version"]
    assert {row.forcing_version_id for row in repository.timeseries} == {"stale_forcing_version"}


def test_missing_canonical_product_blocks_generation(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, omitted_variables={"shortwave_down"})
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="shortwave_down"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


def test_no_stations_raises_error_before_forcing_records(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, stations=())
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="No active meteorological stations"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def _build_producer(
    tmp_path: Path,
    repository: FakeForcingRepository,
    store: LocalObjectStore,
) -> ForcingProducer:
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3)
    return ForcingProducer(config=config, repository=repository, object_store=store)


def _build_repository(
    tmp_path: Path,
    *,
    omitted_variables: set[str] | None = None,
    stations: tuple[MetStation, ...] | None = None,
) -> tuple[LocalObjectStore, FakeForcingRepository]:
    store = LocalObjectStore(tmp_path)
    products = _write_canonical_products(store, omitted_variables=omitted_variables or set())
    repository = FakeForcingRepository(
        stations=stations
        if stations is not None
        else (MetStation("station_1", "basin_v1", 1.0, 0.0, 50.0, "forcing_proxy"),),
        products=products,
    )
    return store, repository


def _write_canonical_products(
    store: LocalObjectStore,
    *,
    omitted_variables: set[str],
) -> tuple[CanonicalProduct, ...]:
    cycle_time = parse_cycle_time("2026050700")
    products: list[CanonicalProduct] = []
    variables = {
        "prcp_rate_or_amount": ("mm", 1.0),
        "air_temperature_2m": ("degC", 10.0),
        "relative_humidity_2m": ("0-1", 0.5),
        "wind_u_10m": ("m/s", 3.0),
        "wind_v_10m": ("m/s", 4.0),
        "pressure_surface": ("Pa", 101000.0),
        "shortwave_down": ("W/m2", 250.0),
    }
    for forecast_hour in (0, 3):
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        for variable, (unit, base_value) in variables.items():
            if variable in omitted_variables:
                continue
            product_id = f"gfs_2026050700_{variable}_f{forecast_hour:03d}"
            key = f"canonical/gfs/2026050700/{variable}/{product_id}.nc"
            content = _netcdf_bytes(
                variable,
                values=(base_value + forecast_hour, base_value + forecast_hour + 1.0, base_value + forecast_hour + 2.0),
            )
            object_uri = store.write_bytes_atomic(key, content)
            products.append(
                CanonicalProduct(
                    canonical_product_id=product_id,
                    source_id="gfs",
                    cycle_time=cycle_time,
                    valid_time=valid_time,
                    variable=variable,
                    unit=unit,
                    grid_id="grid_a",
                    object_uri=object_uri,
                    checksum=sha256_bytes(content),
                    native_time_resolution="3h",
                    native_spatial_resolution="1deg",
                )
            )
    return tuple(products)


def _netcdf_bytes(variable: str, *, values: tuple[float, float, float]) -> bytes:
    import xarray as xr

    dataset = xr.Dataset(
        data_vars={variable: ("point", list(values))},
        coords={
            "point": [0, 1, 2],
            "longitude": ("point", [0.0, 1.0, 2.0]),
            "latitude": ("point", [0.0, 0.0, 0.0]),
        },
    )
    try:
        with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
            dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
            temp_file.seek(0)
            return temp_file.read()
    finally:
        dataset.close()
