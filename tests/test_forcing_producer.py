from __future__ import annotations

import math
import tempfile
from collections.abc import Mapping
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
from workers.forcing_producer.producer import FORCING_VARIABLES, ForcingComponent, ForcingTimeseriesRow


class FakeForcingRepository:
    def __init__(
        self,
        *,
        stations: tuple[MetStation, ...],
        products: tuple[CanonicalProduct, ...],
        fail_next_timeseries_replace: bool = False,
    ) -> None:
        self.basin_by_model = {"demo_model": "basin_v1"}
        self.stations = stations
        self.products = products
        self.interp_weights: list[InterpolationWeight] = []
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.components: list[ForcingComponent] = []
        self.timeseries: list[ForcingTimeseriesRow] = []
        self.cycle_updates: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.fail_next_timeseries_replace = fail_next_timeseries_replace
        self.upsert_count = 0

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        return self.basin_by_model[model_id]

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        return tuple(station for station in self.stations if station.basin_version_id == basin_version_id)

    def list_canonical_products(self, *, source_id: str, cycle_time: Any) -> tuple[CanonicalProduct, ...]:
        return tuple(
            product for product in self.products if product.source_id == source_id and product.cycle_time == cycle_time
        )

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: Any,
        end_time: Any,
        variables: list[str] | tuple[str, ...],
    ) -> tuple[CanonicalProduct, ...]:
        selected: dict[tuple[Any, str], CanonicalProduct] = {}
        for product in self.products:
            if product.source_id != source_id or product.variable not in variables:
                continue
            if not start_time <= product.valid_time <= end_time:
                continue
            if product.quality_flag == "fail" or not product.checksum:
                continue
            key = (product.valid_time, product.variable)
            existing = selected.get(key)
            if existing is None or _lead_time_sort_key(product) < _lead_time_sort_key(existing):
                selected[key] = product
        return tuple(sorted(selected.values(), key=lambda product: (product.variable, product.valid_time)))

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
        self.events.append(("upsert_forcing_version", record["checksum"]))
        return self.forcing_versions[record["forcing_version_id"]]

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        self.forcing_versions[forcing_version_id]["checksum"] = checksum
        self.events.append(("finalize_forcing_version", checksum))
        return dict(self.forcing_versions[forcing_version_id])

    def replace_forcing_components(
        self,
        forcing_version_id: str,
        components: list[ForcingComponent] | tuple[ForcingComponent, ...],
    ) -> None:
        self.components = [
            component for component in self.components if component.forcing_version_id != forcing_version_id
        ]
        self.components.extend(components)
        self.events.append(("replace_forcing_components", forcing_version_id))

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: list[ForcingTimeseriesRow] | tuple[ForcingTimeseriesRow, ...],
    ) -> None:
        if self.fail_next_timeseries_replace:
            self.fail_next_timeseries_replace = False
            raise RuntimeError("timeseries write failed")
        self.timeseries = [row for row in self.timeseries if row.forcing_version_id != forcing_version_id]
        self.timeseries.extend(rows)
        self.events.append(("replace_forcing_timeseries", forcing_version_id))

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

    keys = {(row.forcing_version_id, row.station_id, row.variable, row.valid_time) for row in repository.timeseries}
    assert result.status == "forcing_ready"
    assert len(repository.timeseries) == 1 * 6 * 2
    assert len(keys) == len(repository.timeseries)
    assert {row.variable for row in repository.timeseries} == {"PRCP", "TEMP", "RH", "wind", "Rn", "Press"}
    assert len(repository.components) == 7 * 2
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


def test_era5_produce_uses_net_radiation_for_forcing(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, source_id="ERA5", radiation_variable="net_radiation")
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    content = store.read_bytes(result.file_uris["tsd_forc"]).decode("utf-8")
    lines = content.splitlines()
    rn_lines = [line for line in lines if ",Rn," in line]
    assert result.status == "forcing_ready"
    assert len(lines) == 1 + 2 * 6
    assert len(rn_lines) == 2
    assert {component.variable for component in repository.components} == {
        "prcp_rate_or_amount",
        "air_temperature_2m",
        "relative_humidity_2m",
        "wind_u_10m",
        "wind_v_10m",
        "pressure_surface",
        "net_radiation",
    }
    assert {row.source_id for row in repository.timeseries} == {"ERA5"}
    assert repository.forcing_versions[result.forcing_version_id]["source_id"] == "ERA5"


def test_era5_latency_fallback_uses_min_lead_gfs_products(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        source_id="ERA5",
        radiation_variable="net_radiation",
        omitted_by_time={("net_radiation", 3)},
    )
    gfs_fallback = _write_canonical_products(
        store,
        source_id="gfs",
        cycle_time_text="2026050618",
        product_id_prefix="gfs_recent",
        radiation_variable="shortwave_down",
        forecast_hours=(9,),
        values_by_variable={"shortwave_down": (500.0, 500.0, 500.0)},
    )
    older_gfs = _write_canonical_products(
        store,
        source_id="gfs",
        cycle_time_text="2026050612",
        product_id_prefix="gfs_older",
        radiation_variable="shortwave_down",
        forecast_hours=(15,),
        values_by_variable={"shortwave_down": (900.0, 900.0, 900.0)},
    )
    repository.products = (*repository.products, *gfs_fallback, *older_gfs)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    rn_rows_at_f003 = [
        row
        for row in repository.timeseries
        if row.variable == "Rn" and row.valid_time == parse_cycle_time("2026050703")
    ]
    assert rn_rows_at_f003
    assert {row.source_id for row in rn_rows_at_f003} == {"gfs"}
    assert rn_rows_at_f003[0].value == pytest.approx(500.0)
    lineage = repository.forcing_versions[result.forcing_version_id]["lineage_json"]
    assert lineage["fallback_reason"] == "era5_latency"
    assert lineage["fallback_source_id"] == "gfs"
    assert lineage["fallback_valid_times"] == ["2026-05-07T03:00:00Z"]
    assert any(component.canonical_product_id.startswith("gfs_recent") for component in repository.components)
    assert not any(component.canonical_product_id.startswith("gfs_older") for component in repository.components)


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


def test_forcing_version_checksum_is_finalized_after_child_rows(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.checksum is not None
    assert repository.forcing_versions[result.forcing_version_id]["checksum"] == result.checksum
    assert repository.events[:4] == [
        ("upsert_forcing_version", None),
        ("replace_forcing_components", result.forcing_version_id),
        ("replace_forcing_timeseries", result.forcing_version_id),
        ("finalize_forcing_version", result.checksum),
    ]


def test_failed_child_write_leaves_forcing_version_incomplete_and_retry_finalizes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, fail_next_timeseries_replace=True)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="timeseries write failed"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    forcing_version_id = next(iter(repository.forcing_versions))
    assert repository.forcing_versions[forcing_version_id]["checksum"] is None
    assert not any(event[0] == "finalize_forcing_version" for event in repository.events)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert result.forcing_version_id == forcing_version_id
    assert repository.forcing_versions[forcing_version_id]["checksum"] == result.checksum
    assert repository.upsert_count == 2


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
    assert repository.forcing_versions["stale_forcing_version"]["checksum"] == result.checksum
    assert {row.forcing_version_id for row in repository.timeseries} == {"stale_forcing_version"}


def test_pending_existing_forcing_version_is_replaced_not_skipped(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)
    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    repository.forcing_versions[first.forcing_version_id]["checksum"] = "pending"
    repository.timeseries.clear()

    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert second.status == "forcing_ready"
    assert second.forcing_version_id == first.forcing_version_id
    assert repository.forcing_versions[first.forcing_version_id]["checksum"] == second.checksum
    assert len(repository.timeseries) == 1 * 6 * 2


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


def test_missing_geographic_grid_coordinates_raise_before_weight_generation(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, include_geographic_coords=False)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="usable geographic grid coordinates"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.interp_weights == []
    assert repository.forcing_versions == {}


def test_nonfinite_grid_coordinates_are_rejected(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, longitudes=(math.nan, -74.5, -74.0))
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="non-finite grid coordinates"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")


def test_nonfinite_field_values_are_rejected(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        values_by_variable={"prcp_rate_or_amount": (math.nan, 2.0, 3.0)},
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="non-finite field value"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")


def test_nonfinite_interp_weights_are_rejected(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    repository.interp_weights = [
        InterpolationWeight("gfs", "grid_a", "demo_model", "station_1", variable, "0", math.nan)
        for variable in FORCING_VARIABLES
    ]
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="non-finite values"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")


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
    source_id: str = "gfs",
    omitted_variables: set[str] | None = None,
    omitted_by_time: set[tuple[str, int]] | None = None,
    stations: tuple[MetStation, ...] | None = None,
    fail_next_timeseries_replace: bool = False,
    include_geographic_coords: bool = True,
    values_by_variable: Mapping[str, tuple[float, float, float]] | None = None,
    radiation_variable: str = "shortwave_down",
    longitudes: tuple[float, float, float] = (-75.0, -74.5, -74.0),
    latitudes: tuple[float, float, float] = (40.0, 40.2, 40.4),
) -> tuple[LocalObjectStore, FakeForcingRepository]:
    store = LocalObjectStore(tmp_path)
    products = _write_canonical_products(
        store,
        source_id=source_id,
        omitted_variables=omitted_variables,
        omitted_by_time=omitted_by_time or set(),
        include_geographic_coords=include_geographic_coords,
        values_by_variable=values_by_variable,
        radiation_variable=radiation_variable,
        longitudes=longitudes,
        latitudes=latitudes,
    )
    repository = FakeForcingRepository(
        stations=stations
        if stations is not None
        else (MetStation("station_1", "basin_v1", -74.7, 40.1, 50.0, "forcing_proxy"),),
        products=products,
        fail_next_timeseries_replace=fail_next_timeseries_replace,
    )
    return store, repository


def _write_canonical_products(
    store: LocalObjectStore,
    *,
    source_id: str = "gfs",
    cycle_time_text: str = "2026050700",
    product_id_prefix: str | None = None,
    forecast_hours: tuple[int, ...] = (0, 3),
    lead_time_by_hour: Mapping[int, int] | None = None,
    omitted_variables: set[str] | None = None,
    omitted_by_time: set[tuple[str, int]] | None = None,
    include_geographic_coords: bool = True,
    values_by_variable: Mapping[str, tuple[float, float, float]] | None = None,
    radiation_variable: str = "shortwave_down",
    longitudes: tuple[float, float, float] = (-75.0, -74.5, -74.0),
    latitudes: tuple[float, float, float] = (40.0, 40.2, 40.4),
) -> tuple[CanonicalProduct, ...]:
    cycle_time = parse_cycle_time(cycle_time_text)
    product_id_prefix = product_id_prefix or source_id.lower()
    lead_time_by_hour = lead_time_by_hour or {}
    omitted_variables = omitted_variables or set()
    omitted_by_time = omitted_by_time or set()
    values_by_variable = values_by_variable or {}
    products: list[CanonicalProduct] = []
    variables = {
        "prcp_rate_or_amount": ("mm", 1.0),
        "air_temperature_2m": ("degC", 10.0),
        "relative_humidity_2m": ("0-1", 0.5),
        "wind_u_10m": ("m/s", 3.0),
        "wind_v_10m": ("m/s", 4.0),
        "pressure_surface": ("Pa", 101000.0),
        radiation_variable: ("W/m2", 250.0),
    }
    compact_cycle = cycle_time.strftime("%Y%m%d%H")
    for forecast_hour in forecast_hours:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        for variable, (unit, base_value) in variables.items():
            if variable in omitted_variables or (variable, forecast_hour) in omitted_by_time:
                continue
            product_id = f"{product_id_prefix}_{compact_cycle}_{variable}_f{forecast_hour:03d}"
            key = f"canonical/{source_id}/{compact_cycle}/{variable}/{product_id}.nc"
            values = values_by_variable.get(
                variable,
                (base_value + forecast_hour, base_value + forecast_hour + 1.0, base_value + forecast_hour + 2.0),
            )
            content = _netcdf_bytes(
                variable,
                values=values,
                include_geographic_coords=include_geographic_coords,
                longitudes=longitudes,
                latitudes=latitudes,
            )
            object_uri = store.write_bytes_atomic(key, content)
            products.append(
                CanonicalProduct(
                    canonical_product_id=product_id,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    valid_time=valid_time,
                    variable=variable,
                    unit=unit,
                    grid_id="grid_a",
                    object_uri=object_uri,
                    checksum=sha256_bytes(content),
                    native_time_resolution="3h",
                    native_spatial_resolution="1deg",
                    lead_time_hours=lead_time_by_hour.get(forecast_hour, forecast_hour),
                )
            )
    return tuple(products)


def _netcdf_bytes(
    variable: str,
    *,
    values: tuple[float, float, float],
    include_geographic_coords: bool = True,
    longitudes: tuple[float, float, float] = (-75.0, -74.5, -74.0),
    latitudes: tuple[float, float, float] = (40.0, 40.2, 40.4),
) -> bytes:
    import xarray as xr

    coords: dict[str, Any] = {"point": [0, 1, 2]}
    if include_geographic_coords:
        coords.update(
            {
                "longitude": ("point", list(longitudes)),
                "latitude": ("point", list(latitudes)),
            }
        )
    dataset = xr.Dataset(
        data_vars={variable: ("point", list(values))},
        coords=coords,
    )
    try:
        with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
            dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
            temp_file.seek(0)
            return temp_file.read()
    finally:
        dataset.close()


def _lead_time_sort_key(product: CanonicalProduct) -> tuple[int, Any, str]:
    lead_time = product.lead_time_hours if product.lead_time_hours is not None else 10**9
    return lead_time, product.cycle_time, product.canonical_product_id
