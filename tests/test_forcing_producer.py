from __future__ import annotations

import json
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
        if not weights:
            return
        scopes = {(weight.source_id, weight.grid_id, weight.model_id) for weight in weights}
        assert len(scopes) == 1
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


def test_existing_forcing_version_not_reused_when_lead_window_changes(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model", max_lead_hours=0)
    row_count = len(repository.timeseries)
    second = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

    assert first.status == "forcing_ready"
    assert second.status == "forcing_ready"
    assert repository.upsert_count == 2
    assert len(repository.timeseries) > row_count
    lineage = repository.forcing_versions[second.forcing_version_id]["lineage_json"]
    assert lineage["min_lead_hours"] == 0
    assert lineage["max_lead_hours"] == 3


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
    proxy = MetStation(
        "qhh_proxy_001",
        "basin_v1",
        101.95,
        37.25,
        3700.0,
        "forcing_proxy",
        properties_json={"shud_forcing_index": 999, "forcing_filename": "proxy.csv"},
    )
    forcing_grid = (
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
            "qhh_forc_002",
            "basin_v1",
            101.05,
            36.25,
            3660.0,
            "forcing_grid",
            properties_json={"shud_forcing_index": 2, "forcing_filename": "X101.05Y36.25.csv"},
        ),
    )
    repository = FakeForcingRepository(stations=(proxy,), products=_write_canonical_products(store))
    producer = _build_producer(tmp_path, repository, store)

    first = producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")
    repository.stations = (*forcing_grid, proxy)
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


def test_warn_precipitation_or_radiation_products_do_not_enter_ok_forcing(tmp_path: Path) -> None:
    for variable in ("prcp_rate_or_amount", "shortwave_down"):
        store, repository = _build_repository(tmp_path / variable)
        repository.products = tuple(
            _replace_product_quality(product, "warn") if product.variable == variable else product
            for product in repository.products
        )
        producer = _build_producer(tmp_path / variable, repository, store)

        with pytest.raises(ForcingProductionError, match=variable):
            producer.produce(source_id="gfs", cycle_time="2026050700", model_id="demo_model")

        assert repository.forcing_versions == {}


def test_streaming_field_read_retains_only_required_interpolation_cells(tmp_path: Path) -> None:
    store, repository = _build_repository(tmp_path)
    producer = _build_producer(tmp_path, repository, store)
    product = next(product for product in repository.products if product.variable == "prcp_rate_or_amount")

    field = producer._read_canonical_field(
        product,
        required_grid_cell_ids=frozenset({"1"}),
        retain_grid_points=False,
    )

    assert field.grid_points == ()
    assert field.values_by_grid_cell_id == {"1": pytest.approx(2.0)}


def test_product_grid_mismatch_is_rejected_before_interpolation_reuses_weights(tmp_path: Path) -> None:
    store, repository = _build_repository(
        tmp_path,
        longitudes=(-75.0, -74.5, -74.0),
        latitudes=(40.0, 40.2, 40.4),
    )
    mismatched = _write_canonical_products(
        store,
        product_id_prefix="mismatch",
        forecast_hours=(0,),
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
    if omitted_variables:
        variables = {variable: details for variable, details in variables.items() if variable not in omitted_variables}
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
