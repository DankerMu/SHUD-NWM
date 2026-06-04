from __future__ import annotations

import importlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from packages.common.test_netcdf4 import encode_test_netcdf4

converter_module = importlib.import_module("workers.canonical_converter.converter")

IFSCanonicalConverter = converter_module.IFSCanonicalConverter
IFSCanonicalConverterConfig = converter_module.IFSCanonicalConverterConfig
IFS_VARIABLE_MAPPING = converter_module.IFS_VARIABLE_MAPPING
convert_ifs_precipitation_with_metadata = converter_module.convert_ifs_precipitation_with_metadata
convert_ifs_shortwave_down_values = converter_module.convert_ifs_shortwave_down_values
parse_cycle_time = converter_module.parse_cycle_time

IFS_STANDARD_UNITS = converter_module.IFS_STANDARD_UNITS

IFS_VARIABLES: tuple[str, ...] = ("2t", "2d", "10u", "10v", "tp", "sp", "ssr", "str")


class FakeCanonicalRepository:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        product = self.products.get(canonical_product_id)
        return dict(product) if product is not None else None

    def upsert_canonical_product(self, record: dict[str, Any]) -> dict[str, Any]:
        self.products[record["canonical_product_id"]] = dict(record)
        return self.products[record["canonical_product_id"]]

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["source_id"], kwargs["cycle_time"])
        cycle = self.cycles.setdefault(key, {"source_id": kwargs["source_id"], "cycle_time": kwargs["cycle_time"]})
        for field in ("status", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                cycle[field] = kwargs[field]
        return cycle


def default_ifs_value(variable: str, forecast_hour: int) -> float:
    if variable == "2t":
        return 293.15
    if variable == "2d":
        return 283.15
    if variable == "10u":
        return 3.5
    if variable == "10v":
        return -2.0
    if variable == "tp":
        return forecast_hour * 0.001
    if variable == "sp":
        return 100500.0
    if variable == "ssr":
        return forecast_hour * 3600.0 * 120.0
    if variable == "str":
        return forecast_hour * 3600.0 * -40.0
    raise ValueError(f"Unsupported IFS variable: {variable}")


def build_ifs_manifest(
    tmp_path: Path,
    *,
    forecast_hours: tuple[int, ...] = (0,),
    overrides: dict[tuple[str, int], list[float]] | None = None,
) -> tuple[LocalObjectStore, dict[str, Any]]:
    cycle_time = parse_cycle_time("2026050100")
    compact_cycle = "2026050100"
    store = LocalObjectStore(tmp_path)
    entries: list[dict[str, Any]] = []
    overrides = overrides or {}

    for forecast_hour in forecast_hours:
        for variable in IFS_VARIABLES:
            values = overrides.get((variable, forecast_hour), [default_ifs_value(variable, forecast_hour)])
            local_key = f"raw/IFS/{compact_cycle}/ifs.t00z.f{forecast_hour:03d}.{variable}.grib2"
            store.write_bytes_atomic(
                local_key,
                encode_test_netcdf4(
                    variable,
                    forecast_hour,
                    values=values,
                    cycle_time=cycle_time,
                    source="IFS",
                ),
            )
            entries.append(
                {
                    "remote_url": f"mock://IFS/{variable}/{forecast_hour}",
                    "local_key": local_key,
                    "variable": variable,
                    "forecast_hour": forecast_hour,
                }
            )

    return (
        store,
        {
            "source_id": "IFS",
            "cycle_time": cycle_time.isoformat(),
            "metadata": {"forecast_hours": list(forecast_hours), "max_lead_hours": max(forecast_hours)},
            "entries": entries,
        },
    )


def build_converter(tmp_path: Path, repository: FakeCanonicalRepository | None = None) -> IFSCanonicalConverter:
    config = IFSCanonicalConverterConfig(workspace_root=tmp_path)
    return IFSCanonicalConverter(
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


def test_ifs_variable_mapping_uses_surface_pressure() -> None:
    assert IFS_VARIABLE_MAPPING["2t"] == "air_temperature_2m"
    assert IFS_VARIABLE_MAPPING["2d"] == "relative_humidity_2m"
    assert IFS_VARIABLE_MAPPING["tp"] == "prcp_rate_or_amount"
    assert IFS_VARIABLE_MAPPING["sp"] == "surface_pressure"


def test_missing_ssr_records_shortwave_down_fail_product(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_ifs_manifest(tmp_path, forecast_hours=(0,))
    manifest["entries"] = [entry for entry in manifest["entries"] if entry["variable"] != "ssr"]
    converter = build_converter(tmp_path, repository=repository)

    with pytest.raises(Exception, match="ssr->net_radiation"):
        converter.convert_manifest(manifest)

    net_radiation = repository.products["IFS_2026050100_net_radiation_f000"]
    shortwave = repository.products["IFS_2026050100_shortwave_down_f000"]
    assert net_radiation["quality_flag"] == "fail"
    assert shortwave["quality_flag"] == "fail"
    assert shortwave["lineage_json"]["conversion_params"]["missing_native_variable"] == "ssr"
    assert shortwave["lineage_json"]["conversion_params"]["missing_standard_variable"] == "shortwave_down"


def test_temperature_rh_wind_and_pressure_conversion(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_ifs_manifest(
        tmp_path,
        forecast_hours=(0,),
        overrides={
            ("2t", 0): [293.15],
            ("2d", 0): [283.15],
            ("10u", 0): [6.0],
            ("10v", 0): [-4.0],
            ("sp", 0): [100125.0],
        },
    )
    converter = build_converter(tmp_path, repository=repository)

    result = converter.convert_manifest(manifest)

    assert result.status == "canonical_ready"
    assert len(result.products) == 8
    temperature = repository.products["IFS_2026050100_air_temperature_2m_f000"]
    humidity = repository.products["IFS_2026050100_relative_humidity_2m_f000"]
    wind_u = repository.products["IFS_2026050100_wind_u_10m_f000"]
    wind_v = repository.products["IFS_2026050100_wind_v_10m_f000"]
    assert read_product_values(store, temperature, "air_temperature_2m") == pytest.approx([20.0])
    assert read_product_values(store, humidity, "relative_humidity_2m") == pytest.approx([0.525], abs=1e-3)
    assert read_product_values(store, wind_u, "wind_u_10m") == pytest.approx([6.0])
    assert read_product_values(store, wind_v, "wind_v_10m") == pytest.approx([-4.0])
    pressure = repository.products["IFS_2026050100_surface_pressure_f000"]
    assert pressure["variable"] == "surface_pressure"
    assert pressure["unit"] == "Pa"
    assert read_product_values(store, pressure, "surface_pressure") == pytest.approx([100125.0])


def test_precipitation_cumulative_m_to_mm_per_step(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_ifs_manifest(
        tmp_path,
        forecast_hours=(3, 6),
        overrides={
            ("tp", 3): [0.003],
            ("tp", 6): [0.006],
        },
    )
    converter = build_converter(tmp_path, repository=repository)

    converter.convert_manifest(manifest)

    precipitation = repository.products["IFS_2026050100_prcp_rate_or_amount_f006"]
    assert precipitation["unit"] == "mm/day"
    assert precipitation["quality_flag"] == "ok"
    # per-step delta 3.0 mm over a 3h step -> 3.0 * 24 / 3 = 24.0 mm/day
    assert read_product_values(store, precipitation, "prcp_rate_or_amount") == pytest.approx([24.0])
    lineage = precipitation["lineage_json"]["conversion_params"]
    assert lineage["accumulation_type"] == "since_cycle"
    assert lineage["unit_conversion"] == "m_to_mm_day"
    assert lineage["step_hours"] == 3.0


def test_ifs_canonical_prcp_unit_contract_is_mm_day(tmp_path: Path) -> None:
    # Cross-layer contract: IFS canonical PRCP is emitted in mm/day, aligned with ERA5. The
    # converter rescales each per-step accumulation by its actual step (24 / step_hours) so the
    # producer can pass it through (factor 1.0) without any further step-dependent conversion.
    # Pin the mm/day contract both at the constant and at the actually-produced canonical product.
    assert IFS_STANDARD_UNITS["prcp_rate_or_amount"] == "mm/day"

    repository = FakeCanonicalRepository()
    store, manifest = build_ifs_manifest(
        tmp_path,
        forecast_hours=(3, 6),
        overrides={("tp", 3): [0.003], ("tp", 6): [0.006]},
    )
    converter = build_converter(tmp_path, repository=repository)
    converter.convert_manifest(manifest)

    precipitation = repository.products["IFS_2026050100_prcp_rate_or_amount_f006"]
    assert precipitation["unit"] == "mm/day"
    assert precipitation["unit"] == IFS_STANDARD_UNITS["prcp_rate_or_amount"]
    # per-step delta 3.0 mm over a 3h step -> 24.0 mm/day
    assert read_product_values(store, precipitation, "prcp_rate_or_amount") == pytest.approx([24.0])


def test_negative_precipitation_handling_all_cases() -> None:
    small, small_count, _ = convert_ifs_precipitation_with_metadata([0.001999], [0.002])
    warning, warning_count, _ = convert_ifs_precipitation_with_metadata([0.001], [0.002])
    error, error_count, _ = convert_ifs_precipitation_with_metadata(
        [0.001],
        [0.002],
        consecutive_negative_count=2,
    )

    assert small.values == pytest.approx((0.0,))
    assert small.quality_flag == "ok"
    assert small_count == 0
    assert warning.values == pytest.approx((0.0,))
    assert warning.quality_flag == "warning_negative_precip"
    assert warning_count == 1
    assert error.values == pytest.approx((0.0,))
    assert error.quality_flag == "error_precip_accumulation"
    assert error_count == 3


def test_radiation_cumulative_diff_to_w_m2(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_ifs_manifest(
        tmp_path,
        forecast_hours=(3, 6),
        overrides={
            ("ssr", 3): [720_000.0],
            ("str", 3): [-180_000.0],
            ("ssr", 6): [1_440_000.0],
            ("str", 6): [-360_000.0],
        },
    )
    converter = build_converter(tmp_path, repository=repository)

    converter.convert_manifest(manifest)

    radiation = repository.products["IFS_2026050100_net_radiation_f006"]
    shortwave = repository.products["IFS_2026050100_shortwave_down_f006"]
    assert read_product_values(store, radiation, "net_radiation") == pytest.approx([50.0])
    assert read_product_values(store, shortwave, "shortwave_down") == pytest.approx([66.6666667])
    assert radiation["lineage_json"]["radiation_method"] == "direct_net"
    assert radiation["lineage_json"]["components"] == ["ssr", "str"]
    assert shortwave["lineage_json"]["conversion_params"]["operation"] == "cumulative_j_m2_to_w_m2_downward_shortwave"


def test_ifs_shortwave_rejects_nonfinite_accumulated_values() -> None:
    with pytest.raises(Exception, match="finite"):
        convert_ifs_shortwave_down_values([float("nan")], [0.0], forecast_hour=3, previous_forecast_hour=0)


def test_ifs_precipitation_rejects_nonfinite_accumulated_values() -> None:
    with pytest.raises(Exception, match="finite"):
        convert_ifs_precipitation_with_metadata([math.nan], [0.0], forecast_hour=3, previous_forecast_hour=0)


def test_ifs_shortwave_significant_negative_delta_is_warn_lineage_not_silent_ok() -> None:
    # -20000 J/m² over 3h ≈ -1.85 W/m²,超出量化噪声容差(1.0 W/m²)→ 真异常,标 warn。
    conversion, step_hours = convert_ifs_shortwave_down_values(
        [0.0],
        [20000.0],
        forecast_hour=6,
        previous_forecast_hour=3,
    )

    assert step_hours == 3.0
    assert conversion.values == pytest.approx((0.0,))
    assert conversion.quality_flag == "warn"
    assert conversion.anomalies[0]["type"] == "negative_ifs_shortwave_delta"


def test_ifs_shortwave_quantization_noise_negative_delta_stays_ok() -> None:
    # 夜间持平段的 GRIB 量化抖动:-100 J/m² over 3h ≈ -0.009 W/m²,SHUD 自身对 Rn<0
    # 即钳 0 并取整到整数 W/m²,此负值读入后与 0 逐位等价 → 不标 warn,值 clamp 到 0。
    conversion, step_hours = convert_ifs_shortwave_down_values(
        [100.0],
        [200.0],
        forecast_hour=6,
        previous_forecast_hour=3,
    )

    assert step_hours == 3.0
    assert conversion.values == pytest.approx((0.0,))
    assert conversion.quality_flag == "ok"
    assert conversion.anomalies[0]["type"] == "small_negative_ifs_shortwave_delta"


def test_ifs_shortwave_negative_delta_writes_warn_product_with_lineage(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_ifs_manifest(
        tmp_path,
        forecast_hours=(3, 6),
        overrides={
            ("ssr", 3): [20000.0],
            ("ssr", 6): [0.0],
        },
    )
    converter = build_converter(tmp_path, repository=repository)

    converter.convert_manifest(manifest)

    shortwave = repository.products["IFS_2026050100_shortwave_down_f006"]
    assert shortwave["quality_flag"] == "warn"
    anomalies = shortwave["lineage_json"]["conversion_params"]["anomalies"]
    assert anomalies[0]["type"] == "negative_ifs_shortwave_delta"


def test_ifs_convert_manifest_streams_by_group_without_read_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_ifs_manifest(tmp_path, forecast_hours=(0, 3))
    converter = build_converter(tmp_path, repository=repository)

    def forbidden_read_records(_entries: list[dict[str, Any]]) -> list[Any]:
        raise AssertionError("_read_records must not be used by convert_manifest")

    monkeypatch.setattr(converter, "_read_records", forbidden_read_records)

    result = converter.convert_manifest(manifest)

    assert result.status == "canonical_ready"
    assert len(result.products) == 16


def test_lineage_json_structure_for_each_variable_type(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_ifs_manifest(tmp_path, forecast_hours=(0, 3))
    converter = build_converter(tmp_path, repository=repository)

    converter.convert_manifest(manifest)

    temperature = repository.products["IFS_2026050100_air_temperature_2m_f000"]["lineage_json"]
    humidity = repository.products["IFS_2026050100_relative_humidity_2m_f000"]["lineage_json"]
    precipitation = repository.products["IFS_2026050100_prcp_rate_or_amount_f003"]["lineage_json"]
    radiation = repository.products["IFS_2026050100_net_radiation_f003"]["lineage_json"]
    wind = repository.products["IFS_2026050100_wind_u_10m_f000"]["lineage_json"]
    pressure = repository.products["IFS_2026050100_surface_pressure_f000"]["lineage_json"]

    assert temperature["conversion_params"]["unit_conversion"] == "K_to_C"
    assert humidity["conversion_params"]["derived_from"] == ["2t", "2d"]
    assert humidity["method"] == "magnus_formula"
    assert precipitation["conversion_params"]["operation"] == "cumulative_m_to_mm_day"
    assert precipitation["conversion_params"]["step_hours"] == 3.0
    assert radiation["conversion_params"]["components"] == ["ssr", "str"]
    assert wind["conversion_params"]["operation"] == "pass_through"
    assert pressure["conversion_params"]["native_variable"] == "sp"
    lineages = (temperature, humidity, precipitation, radiation, wind, pressure)
    assert {lineage["converter_version"] for lineage in lineages} == {"m4.1"}
