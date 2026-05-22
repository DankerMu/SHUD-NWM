from __future__ import annotations

import builtins
import importlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from packages.common.test_netcdf4 import encode_test_netcdf4

converter_module = importlib.import_module("workers.canonical_converter.converter")

CanonicalConversionError = converter_module.CanonicalConversionError
CanonicalConverter = converter_module.CanonicalConverter
CanonicalConverterConfig = converter_module.CanonicalConverterConfig
VARIABLE_MAPPING = converter_module.VARIABLE_MAPPING
compute_time_axis = converter_module.compute_time_axis
convert_units = converter_module.convert_units
convert_units_with_metadata = converter_module.convert_units_with_metadata
convert_era5_precipitation_with_metadata = converter_module.convert_era5_precipitation_with_metadata
map_variable = converter_module.map_variable
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


def build_raw_manifest(
    tmp_path: Path,
    *,
    forecast_hours: tuple[int, ...] = (0, 3),
    include_unmapped: bool = False,
    omitted_variables: set[str] | None = None,
    omitted_pairs: set[tuple[str, int]] | None = None,
) -> tuple[LocalObjectStore, dict[str, Any]]:
    cycle_time = parse_cycle_time("2026050700")
    compact_cycle = "2026050700"
    store = LocalObjectStore(tmp_path)
    entries: list[dict[str, Any]] = []
    omitted_variables = omitted_variables or set()
    omitted_pairs = omitted_pairs or set()

    for forecast_hour in forecast_hours:
        for variable in VARIABLE_MAPPING:
            if variable in omitted_variables or (variable, forecast_hour) in omitted_pairs:
                continue
            local_key = f"raw/gfs/{compact_cycle}/gfs.t00z.pgrb2.0p25.f{forecast_hour:03d}.{variable}.grib2"
            store.write_bytes_atomic(local_key, encode_test_netcdf4(variable, forecast_hour, cycle_time=cycle_time))
            entries.append(
                {
                    "remote_url": f"mock://{variable}/{forecast_hour}",
                    "local_key": local_key,
                    "variable": variable,
                    "forecast_hour": forecast_hour,
                }
            )

    if include_unmapped:
        entries.append(
            {
                "remote_url": "mock://badvar/0",
                "local_key": f"raw/gfs/{compact_cycle}/badvar.grib2",
                "variable": "badvar",
                "forecast_hour": 0,
            }
        )

    return store, {"source_id": "gfs", "cycle_time": cycle_time.isoformat(), "entries": entries}


def build_converter(tmp_path: Path, repository: FakeCanonicalRepository | None = None) -> CanonicalConverter:
    config = CanonicalConverterConfig(workspace_root=tmp_path)
    return CanonicalConverter(
        config=config,
        repository=repository or FakeCanonicalRepository(),
        object_store=LocalObjectStore(tmp_path),
    )


def _netcdf_dataset_bytes(dataset: Any) -> bytes:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
        dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
        temp_file.seek(0)
        return temp_file.read()


def test_variable_mapping_covers_required_gfs_variables() -> None:
    assert map_variable("tmp2m") == "air_temperature_2m"
    assert map_variable("apcp") == "prcp_rate_or_amount"
    assert map_variable("rh2m") == "relative_humidity_2m"
    assert map_variable("u10m") == "wind_u_10m"
    assert map_variable("v10m") == "wind_v_10m"
    assert map_variable("pressfc") == "pressure_surface"
    assert map_variable("dswrf") == "shortwave_down"
    assert map_variable("unexpected") is None


def test_unit_conversion_boundaries() -> None:
    assert convert_units("tmp2m", [233.15]) == pytest.approx((-40.0,))
    assert convert_units("apcp", [0.0], [0.0]) == pytest.approx((0.0,))
    assert convert_units("apcp", [6.0], [2.0]) == pytest.approx((4.0,))
    assert convert_units("rh2m", [0.0, 100.0]) == pytest.approx((0.0, 1.0))
    assert convert_units("u10m", [3.5]) == pytest.approx((3.5,))


def test_time_axis_is_monotonic() -> None:
    axis = compute_time_axis("2026050700", [0, 3, 6, 9])

    valid_times = [item["valid_time"] for item in axis]
    assert valid_times == sorted(valid_times)
    assert [item["lead_time_hours"] for item in axis] == [0, 3, 6, 9]
    assert valid_times[2].isoformat() == "2026-05-07T06:00:00+00:00"


def test_conversion_writes_lineage_json_with_required_keys(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path)
    converter = build_converter(tmp_path, repository=repository)

    result = converter.convert_manifest(manifest)

    assert result.status == "canonical_ready"
    assert len(repository.products) == 14
    prcp_f003 = repository.products["gfs_2026050700_prcp_rate_or_amount_f003"]
    lineage = prcp_f003["lineage_json"]
    assert set(lineage) >= {"source_files", "source_cycle_id", "conversion_params", "converter_version"}
    assert len(lineage["source_files"]) == 2
    assert lineage["conversion_params"]["operation"] == "cumulative_to_period"


def test_conversion_writes_rectilinear_grid_definition(tmp_path: Path) -> None:
    _, manifest = build_raw_manifest(tmp_path)
    converter = build_converter(tmp_path)

    converter.convert_manifest(manifest)

    definition = converter.object_store.read_bytes("canonical/gfs/grid/gfs_0p25/grid.json").decode("utf-8")
    assert '"cells":[{"id":0,"lat":0.0,"lon":0.0}]' in definition


def test_conversion_normalizes_point_grid_definition_longitudes(tmp_path: Path) -> None:
    store, manifest = build_raw_manifest(tmp_path)
    import xarray as xr

    for entry in manifest["entries"]:
        dataset = xr.open_dataset(store.resolve_path(entry["local_key"]), engine="netcdf4")
        try:
            variable = next(iter(dataset.data_vars))
            rewritten = xr.Dataset(
                data_vars={variable: ("point", dataset[variable].values.tolist())},
                coords={
                    "point": [0],
                    "longitude": ("point", [350.0]),
                    "latitude": ("point", [35.0]),
                },
            )
            try:
                store.write_bytes_atomic(entry["local_key"], _netcdf_dataset_bytes(rewritten))
            finally:
                rewritten.close()
        finally:
            dataset.close()
    converter = build_converter(tmp_path)

    converter.convert_manifest(manifest)

    definition = converter.object_store.read_bytes("canonical/gfs/grid/gfs_0p25/grid.json").decode("utf-8")
    assert '"lon":-10.0' in definition
    assert '"lon":350.0' not in definition


def test_unmapped_variable_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path, include_unmapped=True)
    converter = build_converter(tmp_path, repository=repository)

    result = converter.convert_manifest(manifest)

    assert len(result.products) == 14
    assert all(product.variable != "badvar" for product in result.products)
    assert "Skipping unmapped variable badvar" in caplog.text


def test_conversion_is_idempotent_on_rerun(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path)
    converter = build_converter(tmp_path, repository=repository)

    first = converter.convert_manifest(manifest)
    upserts_after_first_run = repository.upsert_count
    second = converter.convert_manifest(manifest)

    assert len(first.products) == 14
    assert len(second.products) == 14
    assert {product.status for product in second.products} == {"already_done"}
    assert repository.upsert_count == upserts_after_first_run
    assert len(repository.products) == 14


def test_convert_manifest_streams_without_reading_all_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path)
    converter = build_converter(tmp_path, repository=repository)

    def forbidden_read_records(_entries: list[dict[str, Any]]) -> list[Any]:
        raise AssertionError("_read_records must not be used by convert_manifest")

    monkeypatch.setattr(converter, "_read_records", forbidden_read_records)

    result = converter.convert_manifest(manifest)

    assert result.status == "canonical_ready"
    assert len(result.products) == 14


def test_quality_flag_fail_triggers_reconversion(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path)
    converter = build_converter(tmp_path, repository=repository)
    converter.convert_manifest(manifest)
    repository.products["gfs_2026050700_air_temperature_2m_f000"]["quality_flag"] = "fail"
    upserts_before_rerun = repository.upsert_count

    result = converter.convert_manifest(manifest)

    updated = [
        product for product in result.products if product.canonical_product_id.endswith("air_temperature_2m_f000")
    ]
    assert updated[0].status == "updated"
    assert repository.products["gfs_2026050700_air_temperature_2m_f000"]["quality_flag"] == "ok"
    assert repository.upsert_count == upserts_before_rerun + 1


def test_negative_apcp_delta_marks_product_warn(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_raw_manifest(tmp_path)
    cycle_time = parse_cycle_time("2026050700")
    compact_cycle = "2026050700"
    for forecast_hour, value in ((0, 5.0), (3, 3.0)):
        local_key = f"raw/gfs/{compact_cycle}/gfs.t00z.pgrb2.0p25.f{forecast_hour:03d}.apcp.grib2"
        store.write_bytes_atomic(
            local_key,
            encode_test_netcdf4("apcp", forecast_hour, values=[value], cycle_time=cycle_time),
        )
    converter = build_converter(tmp_path, repository=repository)

    result = converter.convert_manifest(manifest)

    prcp_f003 = repository.products["gfs_2026050700_prcp_rate_or_amount_f003"]
    result_prcp_f003 = [
        product
        for product in result.products
        if product.canonical_product_id == "gfs_2026050700_prcp_rate_or_amount_f003"
    ][0]
    assert prcp_f003["quality_flag"] == "warn"
    assert result_prcp_f003.quality_flag == "warn"
    conversion_params = prcp_f003["lineage_json"]["conversion_params"]
    assert conversion_params["negative_delta_forecast_hours"] == [3]
    assert conversion_params["anomalies"][0]["min_delta"] == -2.0


def test_gfs_apcp_rejects_nonfinite_accumulated_values() -> None:
    with pytest.raises(CanonicalConversionError, match="finite"):
        convert_units_with_metadata("apcp", [math.nan], [0.0], forecast_hour=3, previous_forecast_hour=0)


def test_era5_precipitation_rejects_nonfinite_accumulated_values() -> None:
    with pytest.raises(CanonicalConversionError, match="finite"):
        convert_era5_precipitation_with_metadata([math.nan], [0.0], forecast_hour=3, previous_forecast_hour=0)


def test_grid_definition_mismatch_for_same_configured_uri_fails_conversion(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    store, manifest = build_raw_manifest(tmp_path, forecast_hours=(0,))
    import xarray as xr

    for entry in manifest["entries"]:
        values = [float(entry["forecast_hour"])]
        variable = entry["variable"]
        if variable == "dswrf":
            longitudes = [1.0, 0.0]
            latitudes = [1.0, 0.0]
        else:
            longitudes = [0.0, 1.0]
            latitudes = [0.0, 1.0]
        dataset = xr.Dataset(
            data_vars={variable: ("point", values * 2)},
            coords={
                "point": [0, 1],
                "longitude": ("point", longitudes),
                "latitude": ("point", latitudes),
            },
        )
        try:
            store.write_bytes_atomic(entry["local_key"], _netcdf_dataset_bytes(dataset))
        finally:
            dataset.close()
    converter = build_converter(tmp_path, repository=repository)

    with pytest.raises(CanonicalConversionError, match="different longitude/latitude definition"):
        converter.convert_manifest(manifest)


def test_missing_required_variable_marks_cycle_failed_and_records_fail_product(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path, omitted_variables={"dswrf"})
    converter = build_converter(tmp_path, repository=repository)

    with pytest.raises(CanonicalConversionError, match="Missing required canonical variables"):
        converter.convert_manifest(manifest)

    cycle = repository.cycles[("gfs", parse_cycle_time("2026050700"))]
    assert cycle["status"] == "failed_convert"
    assert cycle["error_code"] == "CONVERT_FAILED"
    fail_product = repository.products["gfs_2026050700_shortwave_down_f000"]
    assert fail_product["quality_flag"] == "fail"
    assert fail_product["lineage_json"]["conversion_params"]["missing_native_variable"] == "dswrf"


def test_missing_variable_for_one_forecast_hour_records_specific_fail_product(tmp_path: Path) -> None:
    repository = FakeCanonicalRepository()
    _, manifest = build_raw_manifest(tmp_path, omitted_pairs={("dswrf", 3)})
    converter = build_converter(tmp_path, repository=repository)

    with pytest.raises(CanonicalConversionError, match="dswrf->shortwave_down f003"):
        converter.convert_manifest(manifest)

    assert "gfs_2026050700_shortwave_down_f003" in repository.products
    assert "gfs_2026050700_shortwave_down_f000" not in repository.products
    assert repository.products["gfs_2026050700_shortwave_down_f003"]["quality_flag"] == "fail"


def test_cfgrib_variable_mismatch_does_not_fallback_to_first_data_var(tmp_path: Path) -> None:
    class FakeDataArray:
        attrs = {"GRIB_shortName": "v10"}

    class FakeDataset:
        data_vars = {"v10": FakeDataArray()}

        def __getitem__(self, key: str) -> FakeDataArray:
            return self.data_vars[key]

    converter = build_converter(tmp_path)

    with pytest.raises(CanonicalConversionError, match="cfgrib variable mismatch"):
        converter._select_cfgrib_data_variable(FakeDataset(), "u10m", "raw/gfs/file.grib2")


def test_netcdf4_missing_raises_without_json_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "netCDF4":
            raise ImportError("missing netCDF4")
        return real_import(name, *args, **kwargs)

    converter = build_converter(tmp_path)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(CanonicalConversionError, match="NetCDF4 serialization requires"):
        converter._serialize_product(
            variable="air_temperature_2m",
            values=(1.0,),
            cycle_time=parse_cycle_time("2026050700"),
            valid_time=parse_cycle_time("2026050700"),
            lead_time_hours=0,
            unit="degC",
            lineage_json={},
        )
