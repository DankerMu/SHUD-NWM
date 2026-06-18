from __future__ import annotations

import hashlib
import json
import math
import tempfile
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.met_store import MetStoreError
from packages.common.object_store import LocalObjectStore, sha256_bytes
from workers.forcing_producer import (
    CanonicalProduct,
    DirectGridContractError,
    ForcingProducer,
    ForcingProducerConfig,
    ForcingProductionError,
    GridPoint,
    InterpolationWeight,
    MetStation,
    compute_idw_weights,
    load_forcing_mapping_contract_from_manifest,
    parse_cycle_time,
    parse_direct_grid_forcing_contract,
    wind_speed,
)
from workers.forcing_producer.direct_grid_contract import (
    MAX_DIRECT_GRID_STATION_BINDINGS,
    REQUIRED_MANIFEST_FIELDS,
    REQUIRED_STATION_FIELDS,
)
from workers.forcing_producer.producer import (
    EXPECTED_CANONICAL_UNITS,
    FORCING_VARIABLES,
    OUTPUT_UNITS,
    ForcingComponent,
    ForcingTimeseriesRow,
    format_shud_forcing_package,
)
from workers.forcing_producer.store import PsycopgForcingRepository


class FakeForcingRepository:
    def __init__(
        self,
        *,
        stations: tuple[MetStation, ...],
        products: tuple[CanonicalProduct, ...],
        forcing_mapping_manifest: Mapping[str, Any] | None = None,
        forcing_mapping_contract: Any = None,
        forcing_mapping_contract_error: Exception | None = None,
        direct_grid_validation_assets: Mapping[str, Any] | None = None,
        fail_next_timeseries_replace: bool = False,
    ) -> None:
        self.basin_by_model = {"demo_model": "basin_v1"}
        self.model_identity_by_model = {
            "demo_model": {
                "basin_id": "basin_a",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rivnet_v1",
            }
        }
        self.stations = stations
        self.products = products
        self.forcing_mapping_manifest = forcing_mapping_manifest
        self.forcing_mapping_contract = forcing_mapping_contract
        self.forcing_mapping_contract_error = forcing_mapping_contract_error
        self.direct_grid_validation_assets = dict(direct_grid_validation_assets or {})
        self.interp_weights: list[InterpolationWeight] = []
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.components: list[ForcingComponent] = []
        self.timeseries: list[ForcingTimeseriesRow] = []
        self.cycle_updates: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.mapping_contract_calls: list[dict[str, Any]] = []
        self.load_station_count = 0
        self.load_weight_count = 0
        self.fail_next_timeseries_replace = fail_next_timeseries_replace
        self.upsert_count = 0

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        return self.basin_by_model[model_id]

    def resolve_model_identity(self, *, model_id: str) -> Mapping[str, Any]:
        return dict(self.model_identity_by_model[model_id])

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        self.load_station_count += 1
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
        self.load_weight_count += 1
        return tuple(
            weight
            for weight in self.interp_weights
            if weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id
        )

    def upsert_interp_weights(self, weights: list[InterpolationWeight] | tuple[InterpolationWeight, ...]) -> None:
        if not weights:
            return
        scopes = {(weight.source_id, weight.grid_id, weight.model_id) for weight in weights}
        if len(scopes) != 1:
            raise MetStoreError("Interpolation weights must be replaced one source/grid/model scope at a time.")
        source_id, grid_id, model_id = next(iter(scopes))
        self.interp_weights = [
            weight
            for weight in self.interp_weights
            if not (weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id)
        ]
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
            else:
                self.interp_weights = [
                    weight
                    if (
                        existing.source_id,
                        existing.grid_id,
                        existing.model_id,
                        existing.station_id,
                        existing.variable,
                        existing.grid_cell_id,
                    )
                    == key
                    else existing
                    for existing in self.interp_weights
                ]

    def load_forcing_mapping_contract(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        source_id: str | None = None,
    ) -> Any:
        self.mapping_contract_calls.append(
            {"model_id": model_id, "basin_version_id": basin_version_id, "source_id": source_id}
        )
        if self.forcing_mapping_contract_error is not None:
            raise self.forcing_mapping_contract_error
        if self.forcing_mapping_manifest is not None:
            return load_forcing_mapping_contract_from_manifest(self.forcing_mapping_manifest, source_id=source_id)
        return self.forcing_mapping_contract

    def load_direct_grid_validation_assets(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        contract: Any,
    ) -> Mapping[str, Any]:
        return dict(self.direct_grid_validation_assets)

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

    def verify_forcing_version_children(
        self,
        *,
        forcing_version_id: str,
        expected_component_ids: list[str] | tuple[str, ...],
        expected_station_ids: list[str] | tuple[str, ...],
        expected_valid_times: list[Any] | tuple[Any, ...],
        expected_variables: list[str] | tuple[str, ...],
    ) -> Mapping[str, Any]:
        components = [
            component
            for component in self.components
            if component.forcing_version_id == forcing_version_id
            and component.canonical_product_id in set(expected_component_ids)
        ]
        rows = [
            row
            for row in self.timeseries
            if row.forcing_version_id == forcing_version_id
            and row.station_id in set(expected_station_ids)
            and row.valid_time in set(expected_valid_times)
            and row.variable in set(expected_variables)
        ]
        expected_row_count = len(expected_station_ids) * len(expected_valid_times) * len(expected_variables)
        proof = {
            "forcing_version_id": forcing_version_id,
            "expected_component_count": len(expected_component_ids),
            "component_count": len(components),
            "expected_timeseries_row_count": expected_row_count,
            "timeseries_row_count": len(rows),
            "station_count": len({row.station_id for row in rows}),
            "timestep_count": len({row.valid_time for row in rows}),
            "variable_count": len({row.variable for row in rows}),
        }
        proof["complete"] = (
            proof["component_count"] == proof["expected_component_count"]
            and proof["timeseries_row_count"] == proof["expected_timeseries_row_count"]
            and proof["station_count"] == len(expected_station_ids)
            and proof["timestep_count"] == len(expected_valid_times)
            and proof["variable_count"] == len(expected_variables)
        )
        return proof

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


class FailingWriteObjectStore(LocalObjectStore):
    def __init__(self, root: Path, *, fail_key_suffix: str) -> None:
        super().__init__(root)
        object.__setattr__(self, "fail_key_suffix", fail_key_suffix)

    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        if str(key_or_uri).endswith(self.fail_key_suffix):
            raise RuntimeError("object write failed")
        return super().write_bytes_atomic(key_or_uri, content)


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


def test_idw_neighbor_selection_preserves_latitude_scaled_distance_semantics() -> None:
    station = MetStation("station_1", "basin_v1", 0.0, 80.0, 10.0, "forcing_proxy")
    grid_points = (
        GridPoint("east", 10.0, 80.0),
        GridPoint("north", 0.0, 82.0),
    )

    weights = compute_idw_weights(
        stations=(station,),
        grid_points=grid_points,
        variables=("PRCP",),
        source_id="gfs",
        grid_id="grid_high_lat",
        model_id="demo_model",
        neighbors=1,
    )

    assert [weight.grid_cell_id for weight in weights] == ["east"]


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


def test_produce_writes_standard_shud_forcing_package_for_all_stations(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    stations = (
        MetStation(
            "qhh_forc_002",
            "basin_v1",
            101.05,
            36.25,
            0.0,
            "forcing_grid",
            properties_json={
                "shud_forcing_index": 2,
                "forcing_filename": "X101.05Y36.25.csv",
                "x": 2,
                "y": 3,
                "z": -9999,
            },
        ),
        MetStation(
            "qhh_forc_001",
            "basin_v1",
            100.95,
            36.25,
            3657.0,
            "forcing_grid",
            properties_json={
                "shud_forcing_index": 1,
                "forcing_filename": "X100.95Y36.25.csv",
                "x": 1,
                "y": 2,
                "z": 3657,
            },
        ),
    )
    repository = FakeForcingRepository(stations=stations, products=_write_canonical_products(store))
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    package_root = tmp_path / result.forcing_package_uri.strip("/")
    tsd_forc = (package_root / "shud" / "qhh.tsd.forc").read_text(encoding="utf-8").splitlines()
    assert tsd_forc[0] == "2 20260507"
    assert tsd_forc[2] == "ID\tLon\tLat\tX\tY\tZ\tFilename"
    assert tsd_forc[3].split()[-1] == "X100.95Y36.25.csv"
    assert tsd_forc[4].split()[-1] == "X101.05Y36.25.csv"
    assert (package_root / "shud" / "X100.95Y36.25.csv").exists()
    assert (package_root / "shud" / "X101.05Y36.25.csv").exists()
    assert result.station_count == 2
    manifest = json.loads((package_root / "forcing_package.json").read_text(encoding="utf-8"))
    assert manifest["variable_set"] == list(FORCING_VARIABLES)
    assert manifest["units"] == {
        "PRCP": "mm/day",
        "TEMP": "degC",
        "RH": "0-1",
        "wind": "m/s",
        "Rn": "W/m2",
        "Press": "Pa",
    }
    assert [station["station_id"] for station in manifest["station_order"]] == ["qhh_forc_001", "qhh_forc_002"]


def test_gfs_forcing_rows_use_cycle_start_with_next_interval_products(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    products = _write_canonical_products(
        store,
        forecast_hours=(0, 3, 6),
        values_by_variable={
            "air_temperature_2m": (100.0, 100.0, 100.0),
            "relative_humidity_2m": (0.5, 0.5, 0.5),
            "wind_u_10m": (3.0, 3.0, 3.0),
            "wind_v_10m": (4.0, 4.0, 4.0),
            "pressure_surface": (101000.0, 101000.0, 101000.0),
            "prcp_rate_or_amount": (30.0, 30.0, 30.0),
            "shortwave_down": (300.0, 300.0, 300.0),
        },
    )
    repository = FakeForcingRepository(
        stations=(
            MetStation(
                "station_1",
                "basin_v1",
                -74.7,
                40.1,
                50.0,
                "forcing_grid",
                properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
            ),
        ),
        products=products,
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    row_times = sorted({row.valid_time for row in repository.timeseries})
    assert [time.isoformat() for time in row_times] == [
        "2026-05-07T00:00:00+00:00",
        "2026-05-07T03:00:00+00:00",
    ]
    first_prcp = next(
        row
        for row in repository.timeseries
        if row.valid_time == parse_cycle_time("2026050700") and row.variable == "PRCP"
    )
    first_temp = next(
        row
        for row in repository.timeseries
        if row.valid_time == parse_cycle_time("2026050700") and row.variable == "TEMP"
    )
    assert first_prcp.value == pytest.approx(30.0)
    assert first_temp.value == pytest.approx(100.0)

    manifest = json.loads(
        (tmp_path / result.forcing_package_uri.strip("/") / "forcing_package.json").read_text(encoding="utf-8")
    )
    assert manifest["start_time"] == "2026-05-07T00:00:00Z"
    assert manifest["end_time"] == "2026-05-07T06:00:00Z"
    assert manifest["lineage"]["row_time_range"]["end_time"] == "2026-05-07T03:00:00Z"
    assert manifest["quality_flags"] == {"canonical_products": ["ok"], "station_timeseries": ["ok"]}
    lineage = repository.forcing_versions[result.forcing_version_id]["lineage_json"]
    assert lineage["forcing_package_manifest_uri"].endswith("/forcing_package.json")
    assert lineage["forcing_package_manifest_checksum"] == result.checksum


def test_shud_station_csv_time_day_is_relative_to_forcing_start(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    products = _write_canonical_products(store, forecast_hours=(0, 3, 6))
    repository = FakeForcingRepository(
        stations=(
            MetStation(
                "station_1",
                "basin_v1",
                -74.7,
                40.1,
                50.0,
                "forcing_grid",
                properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
            ),
        ),
        products=products,
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    station_csv = (
        tmp_path / result.forcing_package_uri.strip("/") / "shud" / "station_1.csv"
    ).read_text(encoding="utf-8").splitlines()
    assert station_csv[0] == "2\t6\t20260507\t20260507"
    assert station_csv[2].split("\t")[0] == "0"
    assert station_csv[3].split("\t")[0] == "0.125"


def test_produce_uses_only_forcing_grid_stations_for_qhh_package(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    stations = (
        MetStation(
            "qhh_forc_001",
            "basin_v1",
            100.95,
            36.25,
            3657.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 1, "forcing_filename": "X100.95Y36.25.csv"},
        ),
        MetStation(
            "qhh_proxy_001",
            "basin_v1",
            101.95,
            37.25,
            3700.0,
            "forcing_proxy",
            properties_json={"shud_forcing_index": 999, "forcing_filename": "proxy.csv"},
        ),
    )
    repository = FakeForcingRepository(stations=stations, products=_write_canonical_products(store))
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    package_root = tmp_path / result.forcing_package_uri.strip("/")
    tsd_forc = (package_root / "shud" / "qhh.tsd.forc").read_text(encoding="utf-8")
    assert result.station_count == 1
    assert repository.forcing_versions[result.forcing_version_id]["station_count"] == 1
    assert tsd_forc.splitlines()[0] == "1 20260507"
    assert "proxy.csv" not in tsd_forc
    assert {row.station_id for row in repository.timeseries} == {"qhh_forc_001"}


def test_forcing_timeseries_long_table_rows_have_composite_pk_shape(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    keys = {(row.forcing_version_id, row.station_id, row.variable, row.valid_time) for row in repository.timeseries}
    assert result.status == "forcing_ready"
    assert len(repository.timeseries) == 1 * 6 * 2
    assert len(keys) == len(repository.timeseries)
    assert {row.variable for row in repository.timeseries} == {"PRCP", "TEMP", "RH", "wind", "Rn", "Press"}
    assert len(repository.components) == len(repository.products)
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
    # already_done must not re-write met result tables (no upsert/finalize/replace after the first run).
    write_events = [name for name, _ in repository.events]
    assert write_events.count("replace_forcing_timeseries") == 1
    assert write_events.count("upsert_forcing_version") == 1
    assert write_events.count("finalize_forcing_version") == 1


def test_existing_forcing_version_not_reused_when_child_rows_are_missing(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    repository.components.clear()
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert second.forcing_version_id == first.forcing_version_id
    assert repository.upsert_count == 2
    assert len(repository.components) == len(repository.products)


def test_existing_forcing_version_not_reused_when_producer_version_is_stale(tmp_path: Path) -> None:
    # A forcing_version produced before the output-semantics bump (producer_version "m1.0")
    # carries stale per-step bytes even though every other reuse signature (station / canonical
    # input / scheduler identity / manifest checksum) still matches the current inputs. The
    # currency check must treat the producer_version mismatch as non-current and force a
    # recompute instead of short-circuiting to already_done with mislabelled bytes.
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    assert first.status == "forcing_ready"
    assert producer.config.producer_version == "m2.1"

    # Downgrade only the producer_version on the stored lineage to mimic a pre-#266 record;
    # all other signatures stay byte-identical to the current inputs.
    record = repository.forcing_versions[first.forcing_version_id]
    lineage = dict(record["lineage_json"])
    assert lineage["producer_version"] == "m2.1"
    lineage["producer_version"] = "m1.0"
    record["lineage_json"] = lineage

    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert second.status == "forcing_ready"
    assert second.status != "already_done"
    assert second.forcing_version_id == first.forcing_version_id
    assert repository.upsert_count == 2
    # The recompute restamps the lineage with the current producer_version.
    assert repository.forcing_versions[second.forcing_version_id]["lineage_json"]["producer_version"] == "m2.1"


def test_scheduler_basin_identity_mismatch_blocks_before_records(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="basin_version_id"):
        producer.produce(
            source_id="gfs",
            cycle_time="2026050700",
            model_id="demo_model",
            basin_version_id="other_basin_v1",
        )

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def test_scheduler_canonical_identity_mismatch_blocks_sibling_rows(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="scheduler-selected canonical identity"):
        producer.produce(
            source_id="gfs",
            cycle_time="2026050700",
            model_id="demo_model",
            canonical_product_id="canon_gfs_2026050700",
            canonical_identity={
                "policy_identity": {"source": "gfs", "forecast_hours": [0, 3]},
                "source_object_identity": {"source": "gfs", "object": "selected"},
            },
        )

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def test_era5_precipitation_mm_per_day_passthrough_mm_per_day(tmp_path: Path) -> None:
    # SHUD PRCP unit is mm/day (Decision A), so a mm/day canonical product passes through
    # unchanged (factor 1.0) regardless of step: 24 mm/day -> 24.0 mm/day.
    store, repository = _build_repository(
        tmp_path,
        source_id="ERA5",
        radiation_variable="net_radiation",
        values_by_variable={"prcp_rate_or_amount": (24.0, 24.0, 24.0)},
    )
    repository.products = tuple(
        _replace_product_unit(product, "mm/day") if product.variable == "prcp_rate_or_amount" else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    prcp_rows = [row for row in repository.timeseries if row.variable == "PRCP"]
    assert prcp_rows
    assert all(row.value == pytest.approx(24.0) for row in prcp_rows)
    assert all(row.unit == "mm/day" for row in prcp_rows)


def test_era5_precipitation_mm_per_day_passthrough_is_step_independent(tmp_path: Path) -> None:
    # mm/day is the SHUD unit, so an hourly (1h step) mm/day product still passes through
    # unchanged: 48 mm/day -> 48.0 mm/day (step does not affect the passthrough factor).
    store, repository = _build_repository(
        tmp_path,
        source_id="ERA5",
        radiation_variable="net_radiation",
        values_by_variable={"prcp_rate_or_amount": (48.0, 48.0, 48.0)},
    )
    repository.products = tuple(
        _replace_product_unit(_replace_product_time_resolution(product, "1h"), "mm/day")
        if product.variable == "prcp_rate_or_amount"
        else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    prcp_rows = [row for row in repository.timeseries if row.variable == "PRCP"]
    assert prcp_rows
    assert all(row.value == pytest.approx(48.0) for row in prcp_rows)


def test_gfs_per_step_mm_unit_is_rejected_by_unit_gate(tmp_path: Path) -> None:
    # Under the converter-side mm/day contract, all canonical PRCP must arrive as mm/day. A GFS
    # product still labelled per-step `mm` is outside EXPECTED_CANONICAL_UNITS and must be rejected
    # by the canonical unit gate before any station_timeseries / forcing_version is written.
    store, repository = _build_repository(
        tmp_path,
        source_id="gfs",
        values_by_variable={"prcp_rate_or_amount": (24.0, 24.0, 24.0)},
    )
    repository.products = tuple(
        _replace_product_unit(product, "mm")
        if product.variable == "prcp_rate_or_amount"
        else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="unit mismatch"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    assert not repository.timeseries
    # Rejection must not leak a half-built forcing_version.
    assert repository.forcing_versions == {}
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


def test_precipitation_mm_per_second_unit_is_rejected_before_records(tmp_path: Path) -> None:
    # "mm/s" is outside EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"] = {"mm/day"}.
    # The canonical unit gate rejects it before any forcing_version / station_timeseries is
    # written. Even if it reached _precip_to_timestep_factor there is no documented mm/s ->
    # mm/day conversion, so the factor would raise too; the unit gate is the first guard.
    store, repository = _build_repository(
        tmp_path,
        source_id="ERA5",
        radiation_variable="net_radiation",
        values_by_variable={"prcp_rate_or_amount": (24.0, 24.0, 24.0)},
    )
    repository.products = tuple(
        _replace_product_unit(product, "mm/s") if product.variable == "prcp_rate_or_amount" else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="unit mismatch"):
        producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


@pytest.mark.parametrize("step", ["1h", "3h", "6h"])
def test_gfs_mm_per_day_amplitude_is_step_independent_passthrough(tmp_path: Path, step: str) -> None:
    # GFS canonical PRCP now arrives as mm/day (the converter already applied 24 / step_hours).
    # The producer passes it through unchanged regardless of the native step. All grid cells share
    # value c, so the IDW-interpolated station value is exactly c.
    canonical_value = 5.0
    store, repository = _build_repository(
        tmp_path,
        source_id="gfs",
        values_by_variable={"prcp_rate_or_amount": (canonical_value,) * 3},
    )
    repository.products = tuple(
        _replace_product_unit(_replace_product_time_resolution(product, step), "mm/day")
        if product.variable == "prcp_rate_or_amount"
        else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    prcp_rows = [row for row in repository.timeseries if row.variable == "PRCP"]
    assert prcp_rows
    assert all(row.value == pytest.approx(canonical_value) for row in prcp_rows)
    assert all(row.unit == "mm/day" for row in prcp_rows)


@pytest.mark.parametrize("step", ["1h", "3h", "6h"])
def test_era5_mm_per_day_amplitude_is_step_independent_passthrough(tmp_path: Path, step: str) -> None:
    # ERA5 canonical PRCP is already mm/day; output equals the canonical value at any step.
    canonical_value = 7.0
    store, repository = _build_repository(
        tmp_path,
        source_id="ERA5",
        radiation_variable="net_radiation",
        values_by_variable={"prcp_rate_or_amount": (canonical_value,) * 3},
    )
    repository.products = tuple(
        _replace_product_unit(_replace_product_time_resolution(product, step), "mm/day")
        if product.variable == "prcp_rate_or_amount"
        else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="ERA5", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    prcp_rows = [row for row in repository.timeseries if row.variable == "PRCP"]
    assert prcp_rows
    assert all(row.value == pytest.approx(canonical_value) for row in prcp_rows)
    assert all(row.unit == "mm/day" for row in prcp_rows)


@pytest.mark.parametrize("step", ["1h", "3h", "6h"])
def test_ifs_mm_per_day_factor_is_one_at_any_step(step: str) -> None:
    # IFS canonical PRCP now arrives as mm/day (converter applied 24 / step_hours), so the producer
    # passes it through with factor 1.0 regardless of the native step.
    producer = ForcingProducer(
        config=ForcingProducerConfig(workspace_root=Path(".")),
        repository=None,
        object_store=None,
    )
    product = _make_precip_product(unit="mm/day", native_time_resolution=step)

    assert producer._precip_to_timestep_factor("IFS", product) == pytest.approx(1.0)


def test_precip_factor_only_passthrough_mm_per_day_and_rejects_others() -> None:
    # Contract: the only accepted canonical PRCP unit is mm/day (passthrough, factor 1.0). Any
    # other unit -- including per-step `mm` drifting from upstream and undocumented `mm/s` -- has
    # no documented ->mm/day conversion and must raise rather than silently convert.
    producer = ForcingProducer(
        config=ForcingProducerConfig(workspace_root=Path(".")),
        repository=None,
        object_store=None,
    )
    accepted_units = EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]
    assert set(accepted_units) == {"mm/day"}

    mm_day = _make_precip_product(unit="mm/day", native_time_resolution="3h")
    assert producer._precip_to_timestep_factor("gfs", mm_day) == pytest.approx(1.0)

    for bad_unit in ("mm", "mm/s"):
        product = _make_precip_product(unit=bad_unit, native_time_resolution="3h")
        with pytest.raises(ForcingProductionError, match="no documented PRCP->mm/day conversion"):
            producer._precip_to_timestep_factor("gfs", product)


def _make_precip_product(*, unit: str, native_time_resolution: str | None) -> CanonicalProduct:
    return CanonicalProduct(
        canonical_product_id="prcp_factor_probe",
        source_id="probe",
        cycle_time=parse_cycle_time("2026050700"),
        valid_time=parse_cycle_time("2026050700"),
        variable="prcp_rate_or_amount",
        unit=unit,
        grid_id="grid_a",
        object_uri="memory://prcp_factor_probe",
        checksum="sha256:probe",
        native_time_resolution=native_time_resolution,
    )


def _direct_grid_manifest() -> dict[str, Any]:
    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": "s3://nhms/models/demo/direct-grid/binding.json",
        "binding_checksum": "sha256:binding",
        "model_input_package_id": "model-input-demo-v1",
        "sp_att_path": "input/qhh.sp.att",
        "sp_att_checksum": "sha256:sp-att",
        "applicable_source_ids": ["GFS", "IFS"],
        "grid_id": "ifs_gfs_025deg",
        "grid_signature": "sha256:grid-signature",
        "station_bindings": [
            {
                "station_id": "qhh_forc_001",
                "shud_forcing_index": 1,
                "forcing_filename": "X100.95Y36.25.csv",
                "longitude": 100.95,
                "latitude": 36.25,
                "x": 1,
                "y": 2,
                "z": 3657,
                "grid_id": "ifs_gfs_025deg",
                "grid_cell_id": "cell-001",
            },
            {
                "station_id": "qhh_forc_002",
                "shud_forcing_index": 2,
                "forcing_filename": "X101.05Y36.25.csv",
                "longitude": 101.05,
                "latitude": 36.25,
                "x": 2,
                "y": 3,
                "z": -9999,
                "grid_id": "ifs_gfs_025deg",
                "grid_cell_id": "cell-002",
            },
        ],
    }


def _direct_grid_manifest_for_default_grid() -> dict[str, Any]:
    manifest = _direct_grid_manifest()
    manifest.update(
        {
            "binding_checksum": "sha256:binding-actual",
            "sp_att_checksum": "sha256:sp-att-actual",
            "grid_id": "grid_a",
            "grid_signature": "sha256:grid-signature-actual",
        }
    )
    manifest["station_bindings"][0].update(
        {"grid_id": "grid_a", "grid_cell_id": "0", "longitude": -75.0, "latitude": 40.0}
    )
    manifest["station_bindings"][1].update(
        {"grid_id": "grid_a", "grid_cell_id": "1", "longitude": -74.5, "latitude": 40.2}
    )
    return manifest


def _sp_att_content(forc_values: tuple[str | int, ...] = (1, 2)) -> str:
    rows = "\n".join(f"{index}\t0\t0\t0\t{value}" for index, value in enumerate(forc_values, start=1))
    return f"2 1\nTRI\tA\tB\tC\tFORC\n{rows}\n"


def _direct_grid_validation_assets(
    *,
    binding_checksum: str = "binding-actual",
    model_input_package_id: str = "model-input-demo-v1",
    sp_att_checksum: str = "sp-att-actual",
    sp_att_content: str | None = None,
) -> dict[str, Any]:
    return {
        "binding_checksum": binding_checksum,
        "model_input_package_id": model_input_package_id,
        "sp_att_checksum": sp_att_checksum,
        "sp_att_content": _sp_att_content() if sp_att_content is None else sp_att_content,
    }


def test_existing_forcing_version_not_reused_when_lead_window_changes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model", max_lead_hours=3)
    row_count = len(repository.timeseries)
    repository.products = (
        *repository.products,
        *_write_canonical_products(
            store,
            forecast_hours=(6,),
            omitted_by_time={
                ("air_temperature_2m", 6),
                ("relative_humidity_2m", 6),
                ("wind_u_10m", 6),
                ("wind_v_10m", 6),
                ("pressure_surface", 6),
            },
        ),
    )
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert repository.upsert_count == 2
    assert len(repository.timeseries) > row_count
    lineage = repository.forcing_versions[second.forcing_version_id]["lineage_json"]
    assert lineage["min_lead_hours"] == 0
    assert lineage["max_lead_hours"] == 6


def test_existing_forcing_version_not_reused_when_canonical_inputs_change_under_same_ids(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    first_lineage = repository.forcing_versions[first.forcing_version_id]["lineage_json"]
    original_prcp = next(product for product in repository.products if product.variable == "prcp_rate_or_amount")
    replacement_prcp = _write_replacement_canonical_product(
        store,
        original_prcp,
        values=(99.0, 100.0, 101.0),
        object_suffix="fresh",
    )
    repository.products = tuple(
        replacement_prcp if product.canonical_product_id == original_prcp.canonical_product_id else product
        for product in repository.products
    )

    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert repository.upsert_count == 2
    assert second.forcing_version_id == first.forcing_version_id
    updated_lineage = repository.forcing_versions[second.forcing_version_id]["lineage_json"]
    assert updated_lineage["canonical_product_ids"] == first_lineage["canonical_product_ids"]
    assert (
        updated_lineage["canonical_input_signature"]["checksum"]
        != first_lineage["canonical_input_signature"]["checksum"]
    )
    prcp_rows = [row for row in repository.timeseries if row.variable == "PRCP"]
    assert prcp_rows
    assert max(row.value for row in prcp_rows) > 90.0
    package_root = tmp_path / second.forcing_package_uri.strip("/")
    manifest = json.loads((package_root / "forcing_package.json").read_text(encoding="utf-8"))
    assert manifest["lineage"]["canonical_input_signature"] == updated_lineage["canonical_input_signature"]


def test_existing_forcing_version_not_reused_when_same_grid_definition_uri_content_changes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path, include_geographic_coords=False)
    grid_definition_uri = "canonical/gfs/grid/grid_a/grid.json"
    _write_grid_definition(
        store,
        grid_definition_uri,
        longitudes=(-75.0, -74.5, -74.0),
        latitudes=(40.0, 40.2, 40.4),
    )
    repository.products = tuple(
        _replace_product_grid_definition(product, grid_definition_uri) for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    first_lineage = repository.forcing_versions[first.forcing_version_id]["lineage_json"]
    _write_grid_definition(
        store,
        grid_definition_uri,
        longitudes=(-75.0, -74.5, -73.0),
        latitudes=(40.0, 40.2, 40.4),
    )
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert second.status != "already_done"
    assert repository.upsert_count == 2
    updated_lineage = repository.forcing_versions[second.forcing_version_id]["lineage_json"]
    assert (
        updated_lineage["canonical_input_signature"]["checksum"]
        != first_lineage["canonical_input_signature"]["checksum"]
    )
    grid_signature = updated_lineage["canonical_input_signature"]["products"][0][
        "grid_definition_content_signature"
    ]
    assert grid_signature["uri"] == grid_definition_uri
    assert grid_signature["grid_signature"]["cells"][2]["longitude"] == -73.0


def test_existing_forcing_version_not_reused_when_forcing_grid_station_set_changes(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    first_forcing_grid = MetStation(
        "qhh_forc_001",
        "basin_v1",
        100.95,
        36.25,
        3657.0,
        "forcing_grid",
        properties_json={"shud_forcing_index": 1, "forcing_filename": "X100.95Y36.25.csv"},
    )
    forcing_grid = (
        first_forcing_grid,
        MetStation(
            "qhh_forc_002",
            "basin_v1",
            101.05,
            36.25,
            3660.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 2, "forcing_filename": "X101.05Y36.25.csv"},
        ),
    )
    repository = FakeForcingRepository(stations=(first_forcing_grid,), products=_write_canonical_products(store))
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    repository.stations = forcing_grid
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert second.station_count == 2
    assert repository.forcing_versions[second.forcing_version_id]["station_count"] == 2
    assert {row.station_id for row in repository.timeseries} == {"qhh_forc_001", "qhh_forc_002"}
    package_root = tmp_path / second.forcing_package_uri.strip("/")
    manifest = json.loads((package_root / "forcing_package.json").read_text(encoding="utf-8"))
    tsd_forc = (package_root / "shud" / "qhh.tsd.forc").read_text(encoding="utf-8")
    assert manifest["station_count"] == 2
    assert manifest["lineage"]["station_signature"]["station_count"] == 2
    assert manifest["lineage"]["station_signature"]["station_ids"] == ["qhh_forc_001", "qhh_forc_002"]
    assert tsd_forc.splitlines()[0] == "2 20260507"
    assert "proxy.csv" not in tsd_forc
    assert "X100.95Y36.25.csv" in tsd_forc
    assert "X101.05Y36.25.csv" in tsd_forc


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


def test_failed_package_manifest_write_leaves_parent_incomplete_and_retry_finalizes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    failing_store = FailingWriteObjectStore(tmp_path, fail_key_suffix="forcing_package.json")
    producer = _build_producer(tmp_path, repository, failing_store)

    with pytest.raises(ForcingProductionError, match="object write failed"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    forcing_version_id = next(iter(repository.forcing_versions))
    assert repository.forcing_versions[forcing_version_id]["checksum"] is None
    assert not any(event[0] == "replace_forcing_components" for event in repository.events)

    retry_producer = _build_producer(tmp_path, repository, store)
    result = retry_producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert result.forcing_version_id == forcing_version_id
    assert repository.forcing_versions[forcing_version_id]["checksum"] == result.checksum


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

    with pytest.raises(ForcingProductionError, match="No active forcing_grid"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def test_proxy_only_stations_block_fixed_station_forcing(tmp_path: Path) -> None:
    proxy = MetStation(
        "qhh_proxy_001",
        "basin_v1",
        -74.7,
        40.1,
        50.0,
        "forcing_proxy",
        properties_json={"shud_forcing_index": 1, "forcing_filename": "proxy.csv"},
    )
    store, repository = _build_repository(tmp_path, stations=(proxy,))
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="No active forcing_grid"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


@pytest.mark.parametrize(
    ("properties_json", "message"),
    [
        ({"forcing_filename": "station.csv"}, "shud_forcing_index"),
        ({"shud_forcing_index": 1}, "forcing_filename"),
        ({"shud_forcing_index": 1, "forcing_filename": "../station.csv"}, "forcing_filename"),
    ],
)
def test_forcing_grid_station_requires_shud_index_and_filename(
    tmp_path: Path,
    properties_json: Mapping[str, Any],
    message: str,
) -> None:
    station = MetStation(
        "qhh_forc_001",
        "basin_v1",
        -74.7,
        40.1,
        50.0,
        "forcing_grid",
        properties_json=properties_json,
    )
    store, repository = _build_repository(tmp_path, stations=(station,))
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match=message):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


@pytest.mark.parametrize(
    ("station_properties", "message"),
    [
        (
            (
                {"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
                {"shud_forcing_index": 1, "forcing_filename": "station_2.csv"},
            ),
            "Duplicate SHUD forcing index 1",
        ),
        (
            (
                {"shud_forcing_index": 1, "forcing_filename": "station.csv"},
                {"shud_forcing_index": 2, "forcing_filename": "station.csv"},
            ),
            "Duplicate SHUD forcing filename 'station.csv'",
        ),
    ],
)
def test_duplicate_forcing_grid_station_contract_blocks_record_creation(
    tmp_path: Path,
    station_properties: tuple[Mapping[str, Any], Mapping[str, Any]],
    message: str,
) -> None:
    stations = (
        MetStation(
            "qhh_forc_001",
            "basin_v1",
            -74.7,
            40.1,
            50.0,
            "forcing_grid",
            properties_json=station_properties[0],
        ),
        MetStation(
            "qhh_forc_002",
            "basin_v1",
            -74.6,
            40.2,
            55.0,
            "forcing_grid",
            properties_json=station_properties[1],
        ),
    )
    store, repository = _build_repository(tmp_path, stations=stations)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match=message):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.upsert_count == 0
    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def test_non_contiguous_forcing_grid_indexes_block_record_creation(tmp_path: Path) -> None:
    stations = (
        MetStation(
            "qhh_forc_001",
            "basin_v1",
            -74.7,
            40.1,
            50.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
        ),
        MetStation(
            "qhh_forc_003",
            "basin_v1",
            -74.6,
            40.2,
            55.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 3, "forcing_filename": "station_3.csv"},
        ),
    )
    store, repository = _build_repository(tmp_path, stations=stations)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="contiguous SHUD forcing indexes"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.upsert_count == 0
    assert repository.forcing_versions == {}


def test_reserved_qhh_tsd_forc_station_filename_blocks_record_creation(tmp_path: Path) -> None:
    station = MetStation(
        "qhh_forc_001",
        "basin_v1",
        -74.7,
        40.1,
        50.0,
        "forcing_grid",
        properties_json={"shud_forcing_index": 1, "forcing_filename": "qhh.tsd.forc"},
    )
    store, repository = _build_repository(tmp_path, stations=(station,))
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="Reserved SHUD forcing filename"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.upsert_count == 0
    assert repository.forcing_versions == {}


def test_direct_grid_contract_valid_parse_preserves_manifest_and_station_identity() -> None:
    contract = parse_direct_grid_forcing_contract(_direct_grid_manifest(), source_id="GFS")

    assert contract.forcing_mapping_mode == "direct_grid"
    assert contract.binding_uri == "s3://nhms/models/demo/direct-grid/binding.json"
    assert contract.binding_checksum == "sha256:binding"
    assert contract.model_input_package_id == "model-input-demo-v1"
    assert contract.sp_att_path == "input/qhh.sp.att"
    assert contract.sp_att_checksum == "sha256:sp-att"
    assert contract.applicable_source_ids == ("gfs", "IFS")
    assert contract.grid_id == "ifs_gfs_025deg"
    assert contract.grid_signature == "sha256:grid-signature"
    assert [station.station_id for station in contract.stations] == ["qhh_forc_001", "qhh_forc_002"]
    assert [station.grid_cell_id for station in contract.stations] == ["cell-001", "cell-002"]
    assert [station.shud_forcing_index for station in contract.stations] == [1, 2]
    assert [station.properties for station in contract.stations] == [{}, {}]


def test_producer_legacy_absent_mapping_contract_uses_existing_idw_path(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 1
    assert repository.load_weight_count == 1
    assert repository.interp_weights
    assert repository.forcing_versions[result.forcing_version_id]["checksum"] == result.checksum
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


def test_producer_legacy_repository_without_mapping_mode_contract_method_uses_idw_path(tmp_path: Path) -> None:
    class LegacyRepository:
        def __init__(self, wrapped: FakeForcingRepository) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> Any:
            if name == "load_forcing_mapping_contract":
                raise AttributeError(name)
            return getattr(self._wrapped, name)

    store, repository = _build_repository(tmp_path)
    legacy_repository = LegacyRepository(repository)
    producer = ForcingProducer(
        config=ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3),
        repository=legacy_repository,
        object_store=store,
    )

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert repository.mapping_contract_calls == []
    assert repository.load_station_count == 1
    assert repository.load_weight_count == 1
    assert repository.interp_weights
    assert repository.timeseries
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


def test_producer_explicit_idw_mapping_mode_uses_existing_idw_path(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_manifest={"forcing_mapping_mode": "idw"},
    )
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 1
    assert repository.load_weight_count == 1
    assert repository.interp_weights
    assert repository.timeseries
    assert repository.cycle_updates[-1]["status"] == "forcing_ready"


def test_producer_direct_grid_mapping_mode_validates_then_fails_closed_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    contract = parse_direct_grid_forcing_contract(_direct_grid_manifest_for_default_grid(), source_id="GFS")
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_contract=contract,
        direct_grid_validation_assets=_direct_grid_validation_assets(),
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="not implemented after the issue #542 validation gate"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.interp_weights == []
    assert repository.forcing_versions == {}
    assert repository.components == []
    assert repository.timeseries == []
    assert repository.upsert_count == 0
    assert not any(event[0] == "finalize_forcing_version" for event in repository.events)
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    assert repository.cycle_updates[-1]["error_code"] == "FORCING_FAILED"


def test_producer_direct_grid_real_store_style_loader_reaches_validation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    binding_content = b'{"schema":"direct-grid-binding-v1"}'
    sp_att_content = _sp_att_content().encode("utf-8")
    manifest = _direct_grid_manifest_for_default_grid()
    manifest.update(
        {
            "binding_checksum": f"sha256:{sha256_bytes(binding_content)}",
            "sp_att_path": "models/demo_model/input/qhh.sp.att",
            "sp_att_checksum": f"sha256:{sha256_bytes(sp_att_content)}",
        }
    )
    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    class ResourceProfileBackedRepository(FakeForcingRepository):
        def __init__(self, *, stations: tuple[MetStation, ...], products: tuple[CanonicalProduct, ...]) -> None:
            super().__init__(stations=stations, products=products)
            self.statement = ""
            self.parameters: tuple[Any, ...] | None = None

        def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
            self.statement = statement
            self.parameters = parameters
            return {"resource_profile": {"direct_grid_forcing": manifest}}

        def load_forcing_mapping_contract(
            self,
            *,
            model_id: str,
            basin_version_id: str,
            source_id: str | None = None,
        ) -> Any:
            self.mapping_contract_calls.append(
                {"model_id": model_id, "basin_version_id": basin_version_id, "source_id": source_id}
            )
            return PsycopgForcingRepository.load_forcing_mapping_contract(
                self,
                model_id=model_id,
                basin_version_id=basin_version_id,
                source_id=source_id,
            )

        def load_direct_grid_validation_assets(
            self,
            *,
            model_id: str,
            basin_version_id: str,
            contract: Any,
        ) -> Mapping[str, Any]:
            return PsycopgForcingRepository.load_direct_grid_validation_assets(
                self,
                model_id=model_id,
                basin_version_id=basin_version_id,
                contract=contract,
            )

    store = LocalObjectStore(tmp_path)
    store.write_bytes_atomic(contract.binding_uri, binding_content)
    store.write_bytes_atomic(contract.sp_att_path, sp_att_content)
    products = _write_canonical_products(store)
    repository = ResourceProfileBackedRepository(
        stations=(),
        products=products,
    )
    producer = _build_producer(tmp_path, repository, store)

    assets = repository.load_direct_grid_validation_assets(
        model_id="demo_model",
        basin_version_id="basin_v1",
        contract=contract,
    )
    assert assets["model_input_package_id"] == "model-input-demo-v1"

    with pytest.raises(ForcingProductionError, match="not implemented after the issue #542 validation gate"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert "FROM core.model_instance" in repository.statement
    assert "resource_profile" in repository.statement
    assert "met.interp_weight" not in repository.statement
    assert repository.parameters == ("demo_model", "basin_v1")
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.forcing_versions == {}
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


def test_producer_rejects_root_direct_grid_manifest_before_station_loading(tmp_path: Path) -> None:
    class RootDirectGridManifestRepository(FakeForcingRepository):
        def load_forcing_mapping_contract(
            self,
            *,
            model_id: str,
            basin_version_id: str,
            source_id: str | None = None,
        ) -> Any:
            self.mapping_contract_calls.append(
                {"model_id": model_id, "basin_version_id": basin_version_id, "source_id": source_id}
            )
            return load_forcing_mapping_contract_from_manifest(
                _direct_grid_manifest(),
                source_id=source_id,
                allow_root_direct_grid=False,
            )

    store, repository = _build_repository(tmp_path)
    repository = RootDirectGridManifestRepository(stations=repository.stations, products=repository.products)
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="Invalid forcing mapping contract"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.interp_weights == []
    assert repository.forcing_versions == {}
    assert repository.components == []
    assert repository.timeseries == []
    assert repository.upsert_count == 0
    assert not any(event[0] == "finalize_forcing_version" for event in repository.events)
    assert not (tmp_path / "forcing").exists()
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    assert repository.cycle_updates[-1]["error_code"] == "FORCING_FAILED"
    assert "Invalid forcing mapping contract" in repository.cycle_updates[-1]["error_message"]


def test_producer_malformed_mapping_mode_error_fails_closed_without_ready_version(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_contract_error=DirectGridContractError(
            "Unsupported forcing_mapping_mode 'nearest'.",
            field="forcing_mapping_mode",
            source_id="gfs",
        ),
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="Invalid forcing mapping contract"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.mapping_contract_calls == [
        {"model_id": "demo_model", "basin_version_id": "basin_v1", "source_id": "gfs"}
    ]
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.interp_weights == []
    assert repository.forcing_versions == {}
    assert repository.timeseries == []
    assert repository.upsert_count == 0
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    assert "Invalid forcing mapping contract" in repository.cycle_updates[-1]["error_message"]


@pytest.mark.parametrize(
    ("asset_overrides", "manifest_overrides", "expected_field", "expected_text"),
    [
        ({"binding_checksum": "different-binding"}, {}, "binding_checksum", "binding-actual"),
        ({"model_input_package_id": "other-model-input"}, {}, "model_input_package_id", "model-input-demo-v1"),
        ({"sp_att_checksum": "different-sp-att"}, {}, "sp_att_checksum", "sp-att-actual"),
        ({}, {"grid_id": "other_grid"}, "grid_id", "other_grid"),
        ({}, {"grid_signature": "sha256:stale-grid"}, "grid_signature", "sha256:stale-grid"),
    ],
)
def test_producer_direct_grid_validation_identity_mismatches_fail_before_idw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_overrides: Mapping[str, Any],
    manifest_overrides: Mapping[str, Any],
    expected_field: str,
    expected_text: str,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    manifest = _direct_grid_manifest_for_default_grid()
    manifest.update(manifest_overrides)
    if "grid_id" in manifest_overrides:
        for station in manifest["station_bindings"]:
            station["grid_id"] = manifest_overrides["grid_id"]
    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")
    assets = _direct_grid_validation_assets()
    assets.update(asset_overrides)
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_contract=contract,
        direct_grid_validation_assets=assets,
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError) as exc_info:
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    message = str(exc_info.value)
    assert "DIRECT_GRID_VALIDATION_FAILED" in message
    assert f'"field":"{expected_field}"' in message
    assert expected_text in message
    _assert_direct_grid_failure_without_idw_or_ready_outputs(repository, tmp_path)


@pytest.mark.parametrize(
    ("sp_att_content", "expected_actual"),
    [
        (_sp_att_content((0, 1)), "0"),
        (_sp_att_content((-1, 1)), "-1"),
        ("2 1\nTRI\tA\tB\tC\tFORC\n1\t0\t0\t0\n", "missing"),
        (_sp_att_content(("x", 1)), "x"),
        (_sp_att_content((3, 1)), "3"),
    ],
)
def test_producer_direct_grid_validation_sp_att_forc_invalid_cases_fail_before_idw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sp_att_content: str,
    expected_actual: str,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    contract = parse_direct_grid_forcing_contract(_direct_grid_manifest_for_default_grid(), source_id="GFS")
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_contract=contract,
        direct_grid_validation_assets=_direct_grid_validation_assets(sp_att_content=sp_att_content),
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError) as exc_info:
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    message = str(exc_info.value)
    assert "DIRECT_GRID_VALIDATION_FAILED" in message
    assert '"field":"sp_att.FORC"' in message
    assert expected_actual in message
    _assert_direct_grid_failure_without_idw_or_ready_outputs(repository, tmp_path)


def test_producer_direct_grid_validation_sp_att_forc_missing_bound_index_fails_before_idw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    contract = parse_direct_grid_forcing_contract(_direct_grid_manifest_for_default_grid(), source_id="GFS")
    store, repository = _build_repository(
        tmp_path,
        forcing_mapping_contract=contract,
        direct_grid_validation_assets=_direct_grid_validation_assets(sp_att_content=_sp_att_content((1, 1))),
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError) as exc_info:
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    message = str(exc_info.value)
    assert "DIRECT_GRID_VALIDATION_FAILED" in message
    assert '"field":"sp_att.FORC"' in message
    assert '"expected":[1,2]' in message
    assert '"actual":[1]' in message
    assert '"missing_indexes":[2]' in message
    _assert_direct_grid_failure_without_idw_or_ready_outputs(repository, tmp_path)


def test_producer_direct_grid_fallback_oversized_binding_fails_before_idw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    sp_att_content = _sp_att_content().encode("utf-8")
    manifest = _direct_grid_manifest_for_default_grid()
    manifest.update(
        {
            "binding_checksum": f"sha256:{sha256_bytes(b'expected')}",
            "sp_att_path": "models/demo_model/input/qhh.sp.att",
            "sp_att_checksum": f"sha256:{sha256_bytes(sp_att_content)}",
        }
    )
    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")
    store, repository = _build_repository(tmp_path, forcing_mapping_contract=contract)
    store.write_bytes_atomic(contract.binding_uri, b"x" * 17)
    store.write_bytes_atomic(contract.sp_att_path, sp_att_content)

    class RepositoryWithoutValidationLoader:
        def __init__(self, wrapped: FakeForcingRepository) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> Any:
            if name == "load_direct_grid_validation_assets":
                raise AttributeError(name)
            return getattr(self._wrapped, name)

    producer = ForcingProducer(
        config=ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3, max_manifest_bytes=16),
        repository=RepositoryWithoutValidationLoader(repository),
        object_store=store,
    )

    with pytest.raises(ForcingProductionError) as exc_info:
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    message = str(exc_info.value)
    assert "DIRECT_GRID_VALIDATION_FAILED" in message
    assert '"field":"validation_assets"' in message
    assert "exceeds read limit" in message
    _assert_direct_grid_failure_without_idw_or_ready_outputs(repository, tmp_path)


def test_producer_direct_grid_validation_runs_before_stale_existing_ready_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "sha256:grid-signature-actual",
    )
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)
    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    assert first.status == "forcing_ready"
    row_count = len(repository.timeseries)
    repository.forcing_mapping_contract = parse_direct_grid_forcing_contract(
        _direct_grid_manifest_for_default_grid(),
        source_id="GFS",
    )
    repository.direct_grid_validation_assets = _direct_grid_validation_assets(binding_checksum="stale-binding")
    repository.load_station_count = 0
    repository.load_weight_count = 0

    with pytest.raises(ForcingProductionError) as exc_info:
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert "DIRECT_GRID_VALIDATION_FAILED" in str(exc_info.value)
    assert '"field":"binding_checksum"' in str(exc_info.value)
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert len(repository.timeseries) == row_count
    assert repository.upsert_count == 1
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"


@pytest.mark.parametrize("field_name", ["longitude", "latitude", "x", "y", "z"])
@pytest.mark.parametrize(
    "bad_value",
    [
        True,
        False,
        "1.0",
        float("nan"),
        float("inf"),
        float("-inf"),
        [1.0],
        {"value": 1.0},
    ],
)
def test_direct_grid_contract_station_coordinates_must_be_finite_json_numbers(
    field_name: str,
    bad_value: Any,
) -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0][field_name] = bad_value

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    error = exc_info.value.to_dict()
    assert error["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error["field"] == field_name
    assert error["source_id"] == "GFS"
    assert error["station_id"] == "qhh_forc_001"
    assert error["actual_type"] == type(bad_value).__name__
    if type(bad_value) is float and not math.isfinite(bad_value):
        assert error["message"] == f"Direct-grid station field {field_name!r} must be finite."
        assert error["value"] == repr(bad_value)
    else:
        assert error["message"] == f"Direct-grid station field {field_name!r} must be a finite JSON number."


def test_direct_grid_contract_station_coordinates_accept_finite_ints_and_floats() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0].update(
        {
            "longitude": 100,
            "latitude": 36.5,
            "x": 1,
            "y": 2.25,
            "z": -9999,
        }
    )

    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    station = contract.stations[0]
    assert station.longitude == 100.0
    assert station.latitude == 36.5
    assert station.x == 1.0
    assert station.y == 2.25
    assert station.z == -9999.0


def test_direct_grid_contract_station_longitude_is_normalized_for_shud_output() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0]["longitude"] = 181.25

    contract = parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert contract.stations[0].longitude == pytest.approx(-178.75)
    assert contract.stations[0].grid_cell_id == "cell-001"


@pytest.mark.parametrize(
    ("field_name", "bad_value", "expected_range"),
    [
        ("longitude", -181.0, "[-180, 360] before normalization"),
        ("longitude", 360.1, "[-180, 360] before normalization"),
        ("latitude", -90.1, "[-90, 90]"),
        ("latitude", 90.1, "[-90, 90]"),
    ],
)
def test_direct_grid_contract_station_coordinates_must_be_in_wgs84_bounds(
    field_name: str,
    bad_value: float,
    expected_range: str,
) -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0][field_name] = bad_value

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    error = exc_info.value.to_dict()
    assert error["field"] == field_name
    assert error["station_id"] == "qhh_forc_001"
    assert error["value"] == bad_value
    assert error["expected_range"] == expected_range


@pytest.mark.parametrize("missing_field", REQUIRED_MANIFEST_FIELDS)
def test_direct_grid_contract_missing_manifest_field_raises_structured_error(missing_field: str) -> None:
    manifest = _direct_grid_manifest()
    del manifest[missing_field]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == missing_field
    assert exc_info.value.source_id == "GFS"
    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": str(exc_info.value),
        "field": missing_field,
        "source_id": "GFS",
    }


def test_direct_grid_contract_metadata_without_mode_fails_closed() -> None:
    manifest = _direct_grid_manifest()
    del manifest["forcing_mapping_mode"]

    with pytest.raises(DirectGridContractError) as exc_info:
        load_forcing_mapping_contract_from_manifest({"direct_grid_forcing": manifest}, source_id="GFS")

    assert exc_info.value.field == "forcing_mapping_mode"


@pytest.mark.parametrize("missing_field", REQUIRED_STATION_FIELDS)
def test_direct_grid_contract_missing_station_field_raises_structured_error(missing_field: str) -> None:
    manifest = _direct_grid_manifest()
    del manifest["station_bindings"][0][missing_field]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == missing_field
    if missing_field == "station_id":
        assert exc_info.value.station_id is None
    else:
        assert exc_info.value.station_id == "qhh_forc_001"
    error = exc_info.value.to_dict()
    assert error["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error["field"] == missing_field
    assert error["source_id"] == "GFS"
    if missing_field == "station_id":
        assert "station_id" not in error
    else:
        assert error["station_id"] == "qhh_forc_001"


@pytest.mark.parametrize(
    ("source_scope", "source_id", "expected_source"),
    [
        ([], "GFS", "GFS"),
        (["IFS"], "GFS", "gfs"),
    ],
)
def test_direct_grid_contract_source_scope_must_be_nonempty_and_apply_to_current_source(
    source_scope: list[str],
    source_id: str,
    expected_source: str,
) -> None:
    manifest = _direct_grid_manifest()
    manifest["applicable_source_ids"] = source_scope

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id=source_id)

    assert exc_info.value.field == "applicable_source_ids"
    assert exc_info.value.source_id == expected_source
    assert exc_info.value.to_dict()["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"


def test_direct_grid_contract_non_string_source_scope_entry_uses_bounded_error_details() -> None:
    invalid_source = {"source_id": "GFS", "debug_payload": ["raw", "manifest", "fragment"]}
    manifest = _direct_grid_manifest()
    manifest["applicable_source_ids"] = ["GFS", invalid_source]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    error = exc_info.value.to_dict()
    assert error["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error["field"] == "applicable_source_ids"
    assert error["source_id"] == "GFS"
    assert error["invalid_source_index"] == 1
    assert error["actual_type"] == "dict"
    assert "invalid_source_id" not in error
    assert invalid_source not in error.values()


def test_direct_grid_contract_unsupported_source_scope_entry_uses_bounded_error_details() -> None:
    invalid_source = "UNSUPPORTED_" + ("x" * 4096)
    manifest = _direct_grid_manifest()
    manifest["applicable_source_ids"] = ["GFS", invalid_source]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    error = exc_info.value.to_dict()
    assert error["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error["message"] == "Direct-grid contract includes an unsupported source identifier."
    assert error["field"] == "applicable_source_ids"
    assert error["source_id"] == "GFS"
    assert error["invalid_source_index"] == 1
    assert error["actual_type"] == "str"
    assert error["source_id_length"] == len(invalid_source)
    assert "invalid_source_id" not in error
    assert invalid_source not in str(exc_info.value)
    assert invalid_source not in error.values()
    assert exc_info.value.__cause__ is None
    assert invalid_source not in repr(exc_info.value.__cause__)
    assert invalid_source not in "".join(
        traceback.format_exception(type(exc_info.value), exc_info.value, exc_info.value.__traceback__)
    )


def test_direct_grid_contract_unsupported_mode_fails_closed() -> None:
    manifest = _direct_grid_manifest()
    manifest["forcing_mapping_mode"] = "nearest"

    with pytest.raises(DirectGridContractError) as exc_info:
        load_forcing_mapping_contract_from_manifest(manifest, source_id="GFS")

    assert exc_info.value.field == "forcing_mapping_mode"
    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Unsupported forcing_mapping_mode 'nearest'.",
        "field": "forcing_mapping_mode",
        "source_id": "GFS",
        "supported_modes": ["idw", "direct_grid"],
    }


def test_direct_grid_contract_explicit_idw_top_level_overrides_nested_direct_grid() -> None:
    manifest = {
        "forcing_mapping_mode": "idw",
        "direct_grid_forcing": _direct_grid_manifest(),
    }

    assert load_forcing_mapping_contract_from_manifest(manifest, source_id="GFS") is None


def test_direct_grid_contract_unsupported_top_level_mode_fails_before_nested_direct_grid() -> None:
    manifest = {
        "forcing_mapping_mode": "nearest",
        "direct_grid_forcing": _direct_grid_manifest(),
    }

    with pytest.raises(DirectGridContractError) as exc_info:
        load_forcing_mapping_contract_from_manifest(manifest, source_id="GFS")

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Unsupported forcing_mapping_mode 'nearest'.",
        "field": "forcing_mapping_mode",
        "source_id": "GFS",
        "supported_modes": ["idw", "direct_grid"],
    }


def test_direct_grid_contract_valid_nested_manifest_still_parses() -> None:
    contract = load_forcing_mapping_contract_from_manifest(
        {"direct_grid_forcing": _direct_grid_manifest()},
        source_id="GFS",
    )

    assert contract is not None
    assert contract.binding_checksum == "sha256:binding"
    assert [station.grid_cell_id for station in contract.stations] == ["cell-001", "cell-002"]


def test_direct_grid_contract_valid_root_direct_manifest_still_parses_for_helper_callers() -> None:
    contract = load_forcing_mapping_contract_from_manifest(_direct_grid_manifest(), source_id="GFS")

    assert contract is not None
    assert contract.binding_checksum == "sha256:binding"


def test_direct_grid_contract_rejects_explicit_root_direct_grid_when_root_authority_disabled() -> None:
    with pytest.raises(DirectGridContractError) as exc_info:
        load_forcing_mapping_contract_from_manifest(
            _direct_grid_manifest(),
            source_id="GFS",
            allow_root_direct_grid=False,
        )

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": (
            "Root-level forcing_mapping_mode='direct_grid' requires an authoritative nested direct-grid "
            "contract section."
        ),
        "field": "forcing_mapping_mode",
        "source_id": "GFS",
        "supported_sections": (
            "direct_grid_forcing",
            "direct_grid_contract",
            "forcing_mapping_contract",
        ),
    }


@pytest.mark.parametrize("bad_index", [True, 1.5, "1", "1.5"])
def test_direct_grid_contract_shud_forcing_index_must_be_json_integer(bad_index: Any) -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0]["shud_forcing_index"] = bad_index

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Direct-grid station field 'shud_forcing_index' must be a JSON integer.",
        "field": "shud_forcing_index",
        "source_id": "GFS",
        "station_id": "qhh_forc_001",
        "actual_type": type(bad_index).__name__,
    }


@pytest.mark.parametrize(
    "field_name",
    [
        "binding_uri",
        "binding_checksum",
        "model_input_package_id",
        "sp_att_path",
        "sp_att_checksum",
        "grid_id",
        "grid_signature",
    ],
)
@pytest.mark.parametrize("bad_value", [{"nested": "value"}, ["value"], 123, 1.5, True])
def test_direct_grid_contract_manifest_identity_fields_must_be_json_strings(
    field_name: str,
    bad_value: Any,
) -> None:
    manifest = _direct_grid_manifest()
    manifest[field_name] = bad_value

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": f"Direct-grid contract field {field_name!r} must be a JSON string.",
        "field": field_name,
        "source_id": "GFS",
        "actual_type": type(bad_value).__name__,
    }


@pytest.mark.parametrize("field_name", ["station_id", "forcing_filename", "grid_id", "grid_cell_id"])
@pytest.mark.parametrize("bad_value", [{"nested": "value"}, ["value"], 123, 1.5, True])
def test_direct_grid_contract_station_identity_fields_must_be_json_strings(
    field_name: str,
    bad_value: Any,
) -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0][field_name] = bad_value

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    error = exc_info.value.to_dict()
    assert error["error_code"] == "DIRECT_GRID_CONTRACT_INVALID"
    assert error["message"] == f"Direct-grid contract field {field_name!r} must be a JSON string."
    assert error["field"] == field_name
    assert error["source_id"] == "GFS"
    assert error["actual_type"] == type(bad_value).__name__
    if field_name != "station_id":
        assert error["station_id"] == "qhh_forc_001"


def test_direct_grid_contract_oversized_station_bindings_fail_before_contract_creation() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"] = [{} for _ in range(MAX_DIRECT_GRID_STATION_BINDINGS + 1)]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Direct-grid contract exceeds the station binding count limit.",
        "field": "station_bindings",
        "source_id": "GFS",
        "observed_count": MAX_DIRECT_GRID_STATION_BINDINGS + 1,
        "max_count": MAX_DIRECT_GRID_STATION_BINDINGS,
    }


def test_direct_grid_contract_unsafe_forcing_filename_is_rejected() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][0]["forcing_filename"] = "../station.csv"

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == "forcing_filename"
    assert exc_info.value.station_id == "qhh_forc_001"
    assert exc_info.value.to_dict()["forcing_filename"] == "../station.csv"


def test_direct_grid_contract_non_contiguous_shud_forcing_index_is_rejected() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][1]["shud_forcing_index"] = 3

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == "shud_forcing_index"
    assert exc_info.value.details["actual_indexes"] == (1, 3)
    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Direct-grid shud_forcing_index values must be unique and contiguous from 1.",
        "field": "shud_forcing_index",
        "source_id": "GFS",
        "actual_indexes": (1, 3),
        "expected_indexes": (1, 2),
    }


def test_direct_grid_contract_duplicate_shud_forcing_index_is_rejected() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][1]["shud_forcing_index"] = 1

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == "shud_forcing_index"
    assert exc_info.value.details["actual_indexes"] == (1, 1)


def test_direct_grid_contract_duplicate_forcing_filename_is_rejected() -> None:
    manifest = _direct_grid_manifest()
    manifest["station_bindings"][1]["forcing_filename"] = manifest["station_bindings"][0]["forcing_filename"]

    with pytest.raises(DirectGridContractError) as exc_info:
        parse_direct_grid_forcing_contract(manifest, source_id="GFS")

    assert exc_info.value.field == "forcing_filename"
    assert exc_info.value.station_id == "qhh_forc_002"
    assert exc_info.value.to_dict()["forcing_filename"] == "X100.95Y36.25.csv"


def test_direct_grid_contract_legacy_absence_returns_none_for_idw_compatibility() -> None:
    assert load_forcing_mapping_contract_from_manifest({}) is None
    assert load_forcing_mapping_contract_from_manifest({"forcing_mapping_mode": "idw"}) is None
    assert load_forcing_mapping_contract_from_manifest({"grid_id": "legacy_grid"}) is None
    assert load_forcing_mapping_contract_from_manifest({"stations": []}) is None
    assert load_forcing_mapping_contract_from_manifest({"binding_checksum": "sha256:old"}) is None


def test_direct_grid_repository_loads_manifest_backed_contract_from_single_entrypoint() -> None:
    class CapturingRepository(PsycopgForcingRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")
            self.statement = ""
            self.parameters: tuple[Any, ...] | None = None

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            self.statement = statement
            self.parameters = parameters
            return [
                {
                    "resource_profile": {
                        "binding_checksum": "sha256:stale-mirror",
                        "grid_signature": "sha256:stale-mirror",
                        "direct_grid_forcing": _direct_grid_manifest(),
                    }
                }
            ]

    repository = CapturingRepository()

    contract = repository.load_forcing_mapping_contract(
        model_id="demo_model",
        basin_version_id="basin_v1",
        source_id="GFS",
    )

    assert contract is not None
    assert contract.binding_checksum == "sha256:binding"
    assert contract.grid_signature == "sha256:grid-signature"
    assert [station.grid_cell_id for station in contract.stations] == ["cell-001", "cell-002"]
    assert "FROM core.model_instance" in repository.statement
    assert "resource_profile" in repository.statement
    assert "met.interp_weight" not in repository.statement
    assert repository.parameters == ("demo_model", "basin_v1")


def test_direct_grid_repository_rejects_explicit_root_level_resource_profile_direct_grid() -> None:
    class MirrorOnlyRepository(PsycopgForcingRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            assert parameters == ("demo_model", "basin_v1")
            return [{"resource_profile": _direct_grid_manifest()}]

    repository = MirrorOnlyRepository()

    with pytest.raises(DirectGridContractError, match="authoritative nested direct-grid contract section"):
        repository.load_forcing_mapping_contract(
            model_id="demo_model",
            basin_version_id="basin_v1",
            source_id="GFS",
        )


def test_direct_grid_repository_returns_none_for_legacy_resource_profile() -> None:
    class LegacyRepository(PsycopgForcingRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            assert parameters == ("demo_model", "basin_v1")
            return [{"resource_profile": {"memory_gb": 8}}]

    repository = LegacyRepository()

    assert (
        repository.load_forcing_mapping_contract(
            model_id="demo_model",
            basin_version_id="basin_v1",
            source_id="GFS",
        )
        is None
    )


@pytest.mark.parametrize("resource_profile", [[], "", 0, False])
def test_direct_grid_repository_rejects_malformed_resource_profile(resource_profile: Any) -> None:
    class MalformedResourceProfileRepository(PsycopgForcingRepository):
        def __init__(self) -> None:
            super().__init__("postgresql://example")

        def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
            assert parameters == ("demo_model", "basin_v1")
            return [{"resource_profile": resource_profile}]

    repository = MalformedResourceProfileRepository()

    with pytest.raises(DirectGridContractError) as exc_info:
        repository.load_forcing_mapping_contract(
            model_id="demo_model",
            basin_version_id="basin_v1",
            source_id="GFS",
        )

    assert exc_info.value.to_dict() == {
        "error_code": "DIRECT_GRID_CONTRACT_INVALID",
        "message": "Model resource_profile must be a JSON object.",
        "model_id": "demo_model",
        "basin_version_id": "basin_v1",
        "actual_type": type(resource_profile).__name__,
    }


def test_station_count_resource_limit_blocks_before_records(tmp_path: Path) -> None:
    stations = (
        MetStation(
            "qhh_forc_001",
            "basin_v1",
            -74.7,
            40.1,
            50.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
        ),
        MetStation(
            "qhh_forc_002",
            "basin_v1",
            -74.6,
            40.2,
            55.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 2, "forcing_filename": "station_2.csv"},
        ),
    )
    store, repository = _build_repository(tmp_path, stations=stations)
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3, max_station_count=1)
    producer = ForcingProducer(config=config, repository=repository, object_store=store)

    with pytest.raises(ForcingProductionError, match="station_count"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.upsert_count == 0
    assert repository.forcing_versions == {}


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


def test_canonical_unit_mismatch_blocks_generation_before_forcing_records(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    repository.products = tuple(
        _replace_product_unit(product, "K") if product.variable == "air_temperature_2m" else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="unit mismatch"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert repository.forcing_versions == {}
    assert repository.timeseries == []


def test_warn_precipitation_or_radiation_products_enter_forcing_with_quality_evidence(tmp_path: Path) -> None:
    for variable in ("prcp_rate_or_amount", "shortwave_down"):
        store, repository = _build_repository(tmp_path / variable)
        repository.products = tuple(
            _replace_product_quality(product, "warn") if product.variable == variable else product
            for product in repository.products
        )
        producer = _build_producer(tmp_path / variable, repository, store)

        result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

        assert result.status == "forcing_ready"
        assert result.forcing_version_id in repository.forcing_versions
        package_root = tmp_path / variable / result.forcing_package_uri.strip("/")
        manifest = json.loads((package_root / "forcing_package.json").read_text(encoding="utf-8"))
        assert manifest["quality_flags"]["canonical_products"] == ["ok", "warn"]
        lineage = repository.forcing_versions[result.forcing_version_id]["lineage_json"]
        assert lineage["quality_flags"]["canonical_products"] == ["ok", "warn"]


def test_streaming_field_read_retains_only_required_interpolation_cells(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)
    product = next(
        product
        for product in repository.products
        if product.variable == "prcp_rate_or_amount" and product.lead_time_hours == 3
    )

    field = producer._read_canonical_field(
        product,
        required_grid_cell_ids=frozenset({"1"}),
        retain_grid_points=False,
    )

    assert field.grid_points == ()
    assert field.values_by_grid_cell_id == {"1": pytest.approx(5.0)}


def test_product_grid_mismatch_is_rejected_before_interpolation_reuses_weights(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        longitudes=(-75.0, -74.5, -74.0),
        latitudes=(40.0, 40.2, 40.4),
    )
    mismatched = _write_canonical_products(
        store,
        product_id_prefix="mismatch",
        forecast_hours=(3,),
        omitted_variables={
            "prcp_rate_or_amount",
            "air_temperature_2m",
            "relative_humidity_2m",
            "wind_u_10m",
            "wind_v_10m",
            "pressure_surface",
        },
        longitudes=(-74.0, -74.5, -75.0),
        latitudes=(40.4, 40.2, 40.0),
    )
    replacement = next(product for product in mismatched if product.variable == "shortwave_down")
    repository.products = tuple(
        replacement
        if product.variable == "shortwave_down" and product.valid_time == replacement.valid_time
        else product
        for product in repository.products
    )
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="grid definition/order does not match"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")


def test_nonfinite_interp_weights_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, repository = _build_repository(tmp_path)
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: "current-grid",
    )
    repository.interp_weights = [
        InterpolationWeight(
            "gfs",
            "grid_a",
            "demo_model",
            "station_1",
            variable,
            "0",
            math.nan,
            grid_signature="current-grid",
        )
        for variable in FORCING_VARIABLES
    ]
    producer = _build_producer(tmp_path, repository, store)

    with pytest.raises(ForcingProductionError, match="non-finite values"):
        producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")


def test_stale_interp_weights_are_recomputed_when_grid_signature_changes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    repository.interp_weights = [
        InterpolationWeight("gfs", "grid_a", "demo_model", "station_1", variable, "0", 1.0, grid_signature="old-grid")
        for variable in FORCING_VARIABLES
    ]
    producer = _build_producer(tmp_path, repository, store)

    result = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert result.status == "forcing_ready"
    signatures = {weight.grid_signature for weight in repository.interp_weights}
    assert "old-grid" not in signatures
    assert len(signatures) == 1
    assert all(weight.grid_signature for weight in repository.interp_weights)


def test_stale_interp_weight_rows_are_removed_when_grid_signature_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository = _build_repository(tmp_path, longitudes=(-75.0, -74.5, -74.0), latitudes=(40.0, 40.2, 40.4))
    current_signature = "current-grid"
    repository.interp_weights = [
        InterpolationWeight(
            "gfs",
            "grid_a",
            "demo_model",
            "station_1",
            variable,
            "legacy-cell",
            1.0,
            grid_signature="old",
        )
        for variable in FORCING_VARIABLES
    ]
    repository.interp_weights.extend(
        InterpolationWeight(
            "gfs",
            "grid_a",
            "demo_model",
            "station_1",
            variable,
            "0",
            1.0,
            grid_signature=current_signature,
        )
        for variable in FORCING_VARIABLES
    )
    producer = _build_producer(tmp_path, repository, store)
    monkeypatch.setattr(
        "workers.forcing_producer.producer._grid_signature_hash",
        lambda _grid_points: current_signature,
    )

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    repository.forcing_versions[first.forcing_version_id]["checksum"] = None
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert {weight.grid_cell_id for weight in repository.interp_weights}.isdisjoint({"legacy-cell"})
    assert {weight.grid_signature for weight in repository.interp_weights} == {current_signature}
    assert len(repository.interp_weights) == len(FORCING_VARIABLES) * 3


def _build_producer(
    tmp_path: Path,
    repository: FakeForcingRepository,
    store: LocalObjectStore,
) -> ForcingProducer:
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3)
    return ForcingProducer(config=config, repository=repository, object_store=store)


def _assert_direct_grid_failure_without_idw_or_ready_outputs(
    repository: FakeForcingRepository,
    tmp_path: Path,
) -> None:
    assert repository.load_station_count == 0
    assert repository.load_weight_count == 0
    assert repository.interp_weights == []
    assert repository.forcing_versions == {}
    assert repository.components == []
    assert repository.timeseries == []
    assert repository.upsert_count == 0
    assert not any(event[0] == "finalize_forcing_version" for event in repository.events)
    assert not (tmp_path / "forcing").exists()
    assert repository.cycle_updates[-1]["status"] == "failed_forcing"
    assert repository.cycle_updates[-1]["error_code"] == "FORCING_FAILED"
    assert "DIRECT_GRID_VALIDATION_FAILED" in repository.cycle_updates[-1]["error_message"]


def _build_repository(
    tmp_path: Path,
    *,
    source_id: str = "gfs",
    omitted_variables: set[str] | None = None,
    omitted_by_time: set[tuple[str, int]] | None = None,
    stations: tuple[MetStation, ...] | None = None,
    forcing_mapping_manifest: Mapping[str, Any] | None = None,
    forcing_mapping_contract: Any = None,
    forcing_mapping_contract_error: Exception | None = None,
    direct_grid_validation_assets: Mapping[str, Any] | None = None,
    fail_next_timeseries_replace: bool = False,
    include_geographic_coords: bool = True,
    values_by_variable: Mapping[str, tuple[float, float, float]] | None = None,
    radiation_variable: str = "shortwave_down",
    longitudes: tuple[float, float, float] = (-75.0, -74.5, -74.0),
    latitudes: tuple[float, float, float] = (40.0, 40.2, 40.4),
) -> tuple[LocalObjectStore, FakeForcingRepository]:
    store = LocalObjectStore(tmp_path)
    forecast_hours = (0, 3, 6) if source_id == "gfs" else (0, 3)
    products = _write_canonical_products(
        store,
        source_id=source_id,
        forecast_hours=forecast_hours,
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
        else (
            MetStation(
                "station_1",
                "basin_v1",
                -74.7,
                40.1,
                50.0,
                "forcing_grid",
                properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
            ),
        ),
        products=products,
        forcing_mapping_manifest=forcing_mapping_manifest,
        forcing_mapping_contract=forcing_mapping_contract,
        forcing_mapping_contract_error=forcing_mapping_contract_error,
        direct_grid_validation_assets=direct_grid_validation_assets,
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
        "prcp_rate_or_amount": ("mm/day", 1.0),
        "air_temperature_2m": ("degC", 10.0),
        "relative_humidity_2m": ("0-1", 0.5),
        "wind_u_10m": ("m/s", 3.0),
        "wind_v_10m": ("m/s", 4.0),
        "pressure_surface": ("Pa", 101000.0),
        radiation_variable: ("W/m2", 250.0),
    }
    if omitted_variables:
        variables = {variable: details for variable, details in variables.items() if variable not in omitted_variables}
    compact_cycle = cycle_time.strftime("%Y%m%d%H")
    for forecast_hour in forecast_hours:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        for variable, (unit, base_value) in variables.items():
            if variable in omitted_variables or (variable, forecast_hour) in omitted_by_time:
                continue
            if source_id == "gfs" and forecast_hour == 0 and variable in {"prcp_rate_or_amount", radiation_variable}:
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


def _replace_product_quality(product: CanonicalProduct, quality_flag: str) -> CanonicalProduct:
    return CanonicalProduct(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=product.unit,
        grid_id=product.grid_id,
        object_uri=product.object_uri,
        checksum=product.checksum,
        grid_definition_uri=product.grid_definition_uri,
        native_time_resolution=product.native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=quality_flag,
        lead_time_hours=product.lead_time_hours,
    )


def _replace_product_unit(product: CanonicalProduct, unit: str) -> CanonicalProduct:
    return CanonicalProduct(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=unit,
        grid_id=product.grid_id,
        object_uri=product.object_uri,
        checksum=product.checksum,
        grid_definition_uri=product.grid_definition_uri,
        native_time_resolution=product.native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=product.quality_flag,
        lead_time_hours=product.lead_time_hours,
    )


def _replace_product_time_resolution(
    product: CanonicalProduct, native_time_resolution: str | None
) -> CanonicalProduct:
    return CanonicalProduct(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=product.unit,
        grid_id=product.grid_id,
        object_uri=product.object_uri,
        checksum=product.checksum,
        grid_definition_uri=product.grid_definition_uri,
        native_time_resolution=native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=product.quality_flag,
        lead_time_hours=product.lead_time_hours,
    )


def _replace_product_grid_definition(product: CanonicalProduct, grid_definition_uri: str) -> CanonicalProduct:
    return CanonicalProduct(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=product.unit,
        grid_id=product.grid_id,
        object_uri=product.object_uri,
        checksum=product.checksum,
        grid_definition_uri=grid_definition_uri,
        native_time_resolution=product.native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=product.quality_flag,
        lead_time_hours=product.lead_time_hours,
    )


def _write_grid_definition(
    store: LocalObjectStore,
    uri: str,
    *,
    longitudes: tuple[float, float, float],
    latitudes: tuple[float, float, float],
) -> str:
    content = json.dumps(
        {
            "schema_version": "nhms.grid_definition.v1",
            "grid_id": "grid_a",
            "cells": [
                {"id": index, "lon": longitude, "lat": latitude}
                for index, (longitude, latitude) in enumerate(zip(longitudes, latitudes, strict=True))
            ],
        },
        sort_keys=True,
    ).encode("utf-8")
    return store.write_bytes_atomic(uri, content)


def _write_replacement_canonical_product(
    store: LocalObjectStore,
    product: CanonicalProduct,
    *,
    values: tuple[float, float, float],
    object_suffix: str,
    longitudes: tuple[float, float, float] = (-75.0, -74.5, -74.0),
    latitudes: tuple[float, float, float] = (40.0, 40.2, 40.4),
) -> CanonicalProduct:
    content = _netcdf_bytes(product.variable, values=values, longitudes=longitudes, latitudes=latitudes)
    key = (
        f"canonical/{product.source_id}/{product.cycle_time.strftime('%Y%m%d%H')}/"
        f"{product.variable}/{product.canonical_product_id}-{object_suffix}.nc"
    )
    object_uri = store.write_bytes_atomic(key, content)
    return CanonicalProduct(
        canonical_product_id=product.canonical_product_id,
        source_id=product.source_id,
        cycle_time=product.cycle_time,
        valid_time=product.valid_time,
        variable=product.variable,
        unit=product.unit,
        grid_id=product.grid_id,
        object_uri=object_uri,
        checksum=sha256_bytes(content),
        grid_definition_uri=product.grid_definition_uri,
        native_time_resolution=product.native_time_resolution,
        native_spatial_resolution=product.native_spatial_resolution,
        quality_flag=product.quality_flag,
        lead_time_hours=product.lead_time_hours,
    )


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


# --- #272 output-semantics regression guards ---------------------------------

# Pinned fingerprint of the producer's output semantics (OUTPUT_UNITS, precip
# conversion branch behavior, and rn_shortwave_factor default). See
# test_output_semantics_fingerprint_pins_producer_version for the contract.
EXPECTED_OUTPUT_SEMANTICS_FINGERPRINT = (
    "445888eeefbf5b2ae453866e5d4dfa81ddf4a9fe04e75c7b0b49a5de3e28e6bd"
)
EXPECTED_PRODUCER_VERSION = "m2.1"


def _precip_product(unit: str) -> CanonicalProduct:
    now = datetime(2026, 5, 7, tzinfo=UTC)
    return CanonicalProduct(
        canonical_product_id="cp_prcp",
        source_id="gfs",
        cycle_time=now,
        valid_time=now,
        variable="prcp_rate_or_amount",
        unit=unit,
        grid_id="grid_a",
        object_uri="s3://nhms/canonical/cp_prcp",
        checksum="deadbeef",
    )


def _compute_output_semantics_fingerprint(producer: ForcingProducer) -> str:
    """Reproduce the pinned fingerprint from observable output semantics.

    The fingerprint covers exactly the output-semantics surface that #272 guards:
    OUTPUT_UNITS, the precipitation conversion branch (mm/day -> 1.0, anything
    else rejected), and the rn_shortwave_factor default.
    """
    mmday_factor = producer._precip_to_timestep_factor("gfs", _precip_product("mm/day"))
    other_rejected = True
    try:
        producer._precip_to_timestep_factor("gfs", _precip_product("mm"))
        other_rejected = False
    except ForcingProductionError:
        other_rejected = True
    shud_rows = (
        ForcingTimeseriesRow(
            forcing_version_id="forc",
            basin_version_id="basin",
            station_id="station_1",
            valid_time=datetime(2026, 5, 7, hour, tzinfo=UTC),
            source_id="gfs",
            variable=variable,
            value=1.0,
            unit=OUTPUT_UNITS[variable],
            native_resolution="3h",
        )
        for hour in (0, 3)
        for variable in FORCING_VARIABLES
    )
    shud_station = MetStation(
        "station_1",
        "basin",
        100.0,
        35.0,
        1.0,
        "forcing_grid",
        properties_json={"shud_forcing_index": 1, "forcing_filename": "station_1.csv"},
    )
    shud_csv = format_shud_forcing_package(tuple(shud_rows), stations=(shud_station,))["shud/station_1.csv"]
    shud_time_day_values = tuple(line.split("\t", maxsplit=1)[0] for line in shud_csv.splitlines()[2:])
    parts = [
        "OUTPUT_UNITS=" + repr(sorted(OUTPUT_UNITS.items())),
        f"precip_mmday_factor={mmday_factor!r}",
        f"precip_other_rejected={other_rejected!r}",
        f"rn_shortwave_factor={producer.config.rn_shortwave_factor!r}",
        f"shud_time_day_values={shud_time_day_values!r}",
    ]
    blob = "|".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def test_output_semantics_fingerprint_pins_producer_version(tmp_path: Path) -> None:
    """Regression gate: any change to producer output semantics must bump the version.

    This test fingerprints the producer's output-semantics surface (OUTPUT_UNITS,
    precip conversion branch, rn_shortwave_factor default) and pins it together
    with ``producer_version``. If a developer changes any output semantic, the
    fingerprint changes and this test goes RED, forcing them to BOTH bump
    ``producer_version`` AND update ``EXPECTED_OUTPUT_SEMANTICS_FINGERPRINT`` here
    in the same change. This is the enforcement gate that keeps forcing lineage
    currency checks honest (see #266/#272).
    """
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3)
    store = LocalObjectStore(tmp_path)
    repository = _build_repository(tmp_path)[1]
    producer = ForcingProducer(config=config, repository=repository, object_store=store)

    fingerprint = _compute_output_semantics_fingerprint(producer)

    assert (fingerprint, producer.config.producer_version) == (
        EXPECTED_OUTPUT_SEMANTICS_FINGERPRINT,
        EXPECTED_PRODUCER_VERSION,
    )


def test_precip_mmday_accepted_and_other_units_rejected(tmp_path: Path) -> None:
    """#272: precip conversion branch accepts mm/day (factor 1.0) and rejects others."""
    config = ForcingProducerConfig(workspace_root=tmp_path, idw_neighbors=3)
    store = LocalObjectStore(tmp_path)
    repository = _build_repository(tmp_path)[1]
    producer = ForcingProducer(config=config, repository=repository, object_store=store)

    assert producer._precip_to_timestep_factor("gfs", _precip_product("mm/day")) == 1.0
    with pytest.raises(ForcingProductionError):
        producer._precip_to_timestep_factor("gfs", _precip_product("mm"))


class _MemoryInterpWeightRepository(PsycopgForcingRepository):
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(database_url="memory://interp-weight")
        object.__setattr__(self, "rows", list(rows or []))
        object.__setattr__(self, "replace_calls", [])

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        assert "FROM met.interp_weight" in statement
        source_id, grid_id, model_id = parameters
        return [
            dict(row)
            for row in self.rows
            if row["source_id"] == source_id and row["grid_id"] == grid_id and row["model_id"] == model_id
        ]

    def _replace_values(
        self,
        delete_statement: str | None,
        delete_parameters: tuple[Any, ...],
        insert_statement: str,
        rows: list[tuple[Any, ...]] | tuple[tuple[Any, ...], ...],
    ) -> None:
        assert delete_statement is not None
        assert "DELETE FROM met.interp_weight" in delete_statement
        assert "INSERT INTO met.interp_weight" in insert_statement
        self.replace_calls.append((delete_parameters, tuple(rows)))
        source_id, grid_id, model_id = delete_parameters
        self.rows = [
            row
            for row in self.rows
            if not (row["source_id"] == source_id and row["grid_id"] == grid_id and row["model_id"] == model_id)
        ]
        self.rows.extend(
            {
                "source_id": row[0],
                "grid_id": row[1],
                "model_id": row[2],
                "station_id": row[3],
                "variable": row[4],
                "grid_cell_id": row[5],
                "weight": row[6],
                "method": row[7],
                "grid_signature": row[8],
            }
            for row in rows
        )


def test_store_direct_grid_interp_weights_round_trip_with_grid_signature() -> None:
    repository = _MemoryInterpWeightRepository()

    repository.upsert_interp_weights(
        (
            InterpolationWeight(
                "GFS",
                "ifs_gfs_025deg",
                "demo_model",
                "qhh_forc_001",
                "PRCP",
                "cell-001",
                1.0,
                method="direct_grid",
                grid_signature="sha256:grid-signature",
            ),
            InterpolationWeight(
                "GFS",
                "ifs_gfs_025deg",
                "demo_model",
                "qhh_forc_001",
                "TEMP",
                "cell-001",
                1.0,
                method="direct_grid",
                grid_signature="sha256:grid-signature",
            ),
        )
    )

    loaded = repository.load_interp_weights(source_id="GFS", grid_id="ifs_gfs_025deg", model_id="demo_model")

    assert [(row.station_id, row.variable, row.grid_cell_id) for row in loaded] == [
        ("qhh_forc_001", "PRCP", "cell-001"),
        ("qhh_forc_001", "TEMP", "cell-001"),
    ]
    assert {row.method for row in loaded} == {"direct_grid"}
    assert {row.weight for row in loaded} == {1.0}
    assert {row.grid_signature for row in loaded} == {"sha256:grid-signature"}


def test_store_direct_grid_snapshot_replaces_same_scope_idw_rows_without_mixing() -> None:
    repository = _MemoryInterpWeightRepository(
        [
            _interp_weight_row(method="idw", grid_cell_id="stale-cell-a", weight=0.25),
            _interp_weight_row(method="idw", grid_cell_id="stale-cell-b", weight=0.75),
            _interp_weight_row(source_id="IFS", method="idw", grid_cell_id="unrelated-cell", weight=1.0),
        ]
    )

    repository.upsert_interp_weights(
        (
            InterpolationWeight(
                "GFS",
                "ifs_gfs_025deg",
                "demo_model",
                "qhh_forc_001",
                "PRCP",
                "cell-001",
                1.0,
                method="direct_grid",
                grid_signature="sha256:grid-signature",
            ),
        )
    )

    assert repository.replace_calls[0][0] == ("GFS", "ifs_gfs_025deg", "demo_model")
    same_scope = repository.load_interp_weights(source_id="GFS", grid_id="ifs_gfs_025deg", model_id="demo_model")
    unrelated = repository.load_interp_weights(source_id="IFS", grid_id="ifs_gfs_025deg", model_id="demo_model")
    assert [(row.method, row.grid_cell_id, row.weight) for row in same_scope] == [
        ("direct_grid", "cell-001", 1.0)
    ]
    assert [(row.method, row.grid_cell_id, row.weight) for row in unrelated] == [
        ("idw", "unrelated-cell", 1.0)
    ]


def test_store_interp_weight_mixed_scope_rejection_happens_before_replacement() -> None:
    repository = _MemoryInterpWeightRepository(
        [_interp_weight_row(method="idw", grid_cell_id="existing-cell", weight=1.0)]
    )
    before = list(repository.rows)

    with pytest.raises(MetStoreError, match="one source/grid/model scope at a time"):
        repository.upsert_interp_weights(
            (
                InterpolationWeight(
                    "GFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_001",
                    "PRCP",
                    "cell-001",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature",
                ),
                InterpolationWeight(
                    "IFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_002",
                    "PRCP",
                    "cell-002",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature",
                ),
            )
        )

    assert repository.replace_calls == []
    assert repository.rows == before


def test_store_rejects_invalid_direct_grid_interp_weight_shape_before_replacement() -> None:
    repository = _MemoryInterpWeightRepository()

    with pytest.raises(MetStoreError, match="exactly one grid cell"):
        repository.upsert_interp_weights(
            (
                InterpolationWeight(
                    "GFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_001",
                    "PRCP",
                    "cell-001",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature",
                ),
                InterpolationWeight(
                    "GFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_001",
                    "PRCP",
                    "cell-002",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature",
                ),
            )
        )

    assert repository.replace_calls == []


def test_store_rejects_mixed_direct_grid_grid_signatures_before_replacement() -> None:
    repository = _MemoryInterpWeightRepository(
        [_interp_weight_row(method="idw", grid_cell_id="existing-cell", weight=1.0)]
    )
    before = list(repository.rows)

    with pytest.raises(MetStoreError, match="exactly one grid_signature"):
        repository.upsert_interp_weights(
            (
                InterpolationWeight(
                    "GFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_001",
                    "PRCP",
                    "cell-001",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature-a",
                ),
                InterpolationWeight(
                    "GFS",
                    "ifs_gfs_025deg",
                    "demo_model",
                    "qhh_forc_002",
                    "PRCP",
                    "cell-002",
                    1.0,
                    method="direct_grid",
                    grid_signature="sha256:grid-signature-b",
                ),
            )
        )

    assert repository.replace_calls == []
    assert repository.rows == before


def _interp_weight_row(
    *,
    source_id: str = "GFS",
    grid_id: str = "ifs_gfs_025deg",
    model_id: str = "demo_model",
    station_id: str = "qhh_forc_001",
    variable: str = "PRCP",
    grid_cell_id: str,
    weight: float,
    method: str,
    grid_signature: str = "sha256:grid-signature",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "grid_id": grid_id,
        "model_id": model_id,
        "station_id": station_id,
        "variable": variable,
        "grid_cell_id": grid_cell_id,
        "weight": weight,
        "method": method,
        "grid_signature": grid_signature,
    }
