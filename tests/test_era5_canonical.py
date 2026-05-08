from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.mock_grib import ERA5_VARIABLES, build_mock_era5_payload, encode_mock_grib2
from packages.common.object_store import LocalObjectStore

converter_module = importlib.import_module("workers.canonical_converter.converter")

ERA5CanonicalConverter = converter_module.ERA5CanonicalConverter
ERA5CanonicalConverterConfig = converter_module.ERA5CanonicalConverterConfig
ERA5_VARIABLE_MAPPING = converter_module.ERA5_VARIABLE_MAPPING
compute_relative_humidity = converter_module.compute_relative_humidity
convert_era5_precipitation_with_metadata = converter_module.convert_era5_precipitation_with_metadata
convert_era5_radiation_values = converter_module.convert_era5_radiation_values
parse_cycle_time = converter_module.parse_cycle_time


class FakeCanonicalRepository:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}
        self.upsert_count = 0

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        product = self.products.get(canonical_product_id)
        return dict(product) if product is not None else None

    def upsert_canonical_product(self, record: dict[str, Any]) -> dict[str, Any]:
        self.upsert_count += 1
        self.products[record["canonical_product_id"]] = dict(record)
        return self.products[record["canonical_product_id"]]

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["source_id"], kwargs["cycle_time"])
        cycle = self.cycles.setdefault(key, {"source_id": kwargs["source_id"], "cycle_time": kwargs["cycle_time"]})
        for field in ("status", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                cycle[field] = kwargs[field]
        return cycle


def build_era5_manifest(
    tmp_path: Path,
    *,
    forecast_hours: tuple[int, ...] = (0, 1),
    overrides: dict[tuple[str, int], list[float]] | None = None,
) -> tuple[LocalObjectStore, dict[str, Any]]:
    cycle_time = parse_cycle_time("2026042000")
    date_key = "2026-04-20"
    store = LocalObjectStore(tmp_path)
    entries: list[dict[str, Any]] = []
    overrides = overrides or {}

    for forecast_hour in forecast_hours:
        for variable in ERA5_VARIABLES:
            local_key = f"raw/ERA5/{date_key}/{variable}_{forecast_hour:02d}.grib"
            payload = build_mock_era5_payload(
                cycle_time,
                variable,
                forecast_hour,
                values=overrides.get((variable, forecast_hour)),
            )
            store.write_bytes_atomic(local_key, encode_mock_grib2(payload))
            entries.append(
                {
                    "remote_url": f"mock://ERA5/{variable}/{forecast_hour}",
                    "local_key": local_key,
                    "variable": variable,
                    "forecast_hour": forecast_hour,
                }
            )

    return (
        store,
        {
            "source_id": "ERA5",
            "cycle_time": cycle_time.isoformat(),
            "metadata": {"forecast_hours": list(forecast_hours)},
            "entries": entries,
        },
    )


def build_converter(tmp_path: Path, repository: FakeCanonicalRepository | None = None) -> ERA5CanonicalConverter:
    config = ERA5CanonicalConverterConfig(workspace_root=tmp_path)
    return ERA5CanonicalConverter(
        config=config,
        repository=repository or FakeCanonicalRepository(),
        object_store=LocalObjectStore(tmp_path),
    )


def read_product_values(store: LocalObjectStore, product: dict[str, Any], variable: str) -> list[float]:
    import xarray as xr

    dataset = xr.open_dataset(store.resolve_path(product["object_uri"]), engine="netcdf4")
    try:
        return [float(value) for value in dataset[variable].values.tolist()]
    finally:
        dataset.close()


def test_dewpoint_to_rh_boundaries() -> None:
    assert compute_relative_humidity(20.0, 20.0) == pytest.approx(1.0)
    assert compute_relative_humidity(40.0, -40.0) < 0.01


def test_radiation_j_m2_to_w_m2_accuracy() -> None:
    result = convert_era5_radiation_values(
        ssr_values=[720_000.0],
        str_values=[-360_000.0],
        previous_ssr_values=[360_000.0],
        previous_str_values=[-180_000.0],
        forecast_hour=2,
        previous_forecast_hour=1,
    )

    assert result == pytest.approx((50.0,))


def test_precipitation_negative_delta_clamps_to_zero() -> None:
    result = convert_era5_precipitation_with_metadata([0.001], [0.002], forecast_hour=2, previous_forecast_hour=1)

    assert result.values == pytest.approx((0.0,))
    assert result.quality_flag == "warn"
    assert result.anomalies[0]["type"] == "negative_era5_precipitation_delta"


def test_precipitation_zero_and_small_values() -> None:
    zero = convert_era5_precipitation_with_metadata([0.0], [0.0], forecast_hour=1, previous_forecast_hour=0)
    tiny = convert_era5_precipitation_with_metadata([0.0000001], [0.0], forecast_hour=1, previous_forecast_hour=0)

    assert zero.values == pytest.approx((0.0,))
    assert tiny.values == pytest.approx((0.0024,))


def test_era5_variable_mapping_correctness() -> None:
    assert ERA5_VARIABLE_MAPPING["2m_temperature"] == "air_temperature_2m"
    assert ERA5_VARIABLE_MAPPING["2m_dewpoint_temperature"] == "relative_humidity_2m"
    assert ERA5_VARIABLE_MAPPING["10m_u_component_of_wind"] == "wind_u_10m"
    assert ERA5_VARIABLE_MAPPING["10m_v_component_of_wind"] == "wind_v_10m"
    assert ERA5_VARIABLE_MAPPING["surface_pressure"] == "pressure_surface"
    assert ERA5_VARIABLE_MAPPING["total_precipitation"] == "prcp_rate_or_amount"
    assert ERA5_VARIABLE_MAPPING["surface_net_solar_radiation"] == "net_radiation"
    assert ERA5_VARIABLE_MAPPING["surface_net_thermal_radiation"] == "net_radiation"


def test_end_to_end_era5_canonical_conversion_with_mock_data(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_era5_manifest(tmp_path)
    converter = build_converter(tmp_path, repository=repository)

    result = converter.convert_manifest(manifest)

    assert result.status == "canonical_ready"
    assert len(result.products) == 16
    assert len(repository.products) == 16
    cycle = repository.cycles[("ERA5", parse_cycle_time("2026042000"))]
    assert cycle["status"] == "canonical_ready"

    product_variables = {product["variable"] for product in repository.products.values()}
    assert product_variables == {
        "air_temperature_2m",
        "relative_humidity_2m",
        "wind_u_10m",
        "wind_v_10m",
        "wind_speed",
        "pressure_surface",
        "prcp_rate_or_amount",
        "net_radiation",
    }

    wind_speed = repository.products["ERA5_2026042000_wind_speed_f001"]
    assert read_product_values(store, wind_speed, "wind_speed") == pytest.approx([5.0])

    precipitation = repository.products["ERA5_2026042000_prcp_rate_or_amount_f001"]
    assert precipitation["unit"] == "mm/day"
    assert read_product_values(store, precipitation, "prcp_rate_or_amount") == pytest.approx([6.0])

    radiation = repository.products["ERA5_2026042000_net_radiation_f001"]
    assert radiation["lineage_json"]["radiation_method"] == "direct_net"
    assert radiation["lineage_json"]["conversion_params"]["operation"] == "cumulative_j_m2_to_w_m2_direct_net"
    assert read_product_values(store, radiation, "net_radiation") == pytest.approx([110.0])
