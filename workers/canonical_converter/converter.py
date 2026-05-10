from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes

LOGGER = logging.getLogger(__name__)

VARIABLE_MAPPING: dict[str, str] = {
    "tmp2m": "air_temperature_2m",
    "apcp": "prcp_rate_or_amount",
    "rh2m": "relative_humidity_2m",
    "u10m": "wind_u_10m",
    "v10m": "wind_v_10m",
    "pressfc": "pressure_surface",
    "dswrf": "shortwave_down",
}
ERA5_VARIABLE_MAPPING: dict[str, str] = {
    "2m_temperature": "air_temperature_2m",
    "2m_dewpoint_temperature": "relative_humidity_2m",
    "10m_u_component_of_wind": "wind_u_10m",
    "10m_v_component_of_wind": "wind_v_10m",
    "surface_pressure": "pressure_surface",
    "total_precipitation": "prcp_rate_or_amount",
    "surface_net_solar_radiation": "net_radiation",
    "surface_net_thermal_radiation": "net_radiation",
}
IFS_VARIABLE_MAPPING: dict[str, str] = {
    "2t": "air_temperature_2m",
    "2d": "relative_humidity_2m",
    "tp": "prcp_rate_or_amount",
    "10u": "wind_u_10m",
    "10v": "wind_v_10m",
    "sp": "surface_pressure",
    "ssr": "net_radiation",
    "str": "net_radiation",
}
STANDARD_UNITS: dict[str, str] = {
    "air_temperature_2m": "degC",
    "prcp_rate_or_amount": "mm",
    "relative_humidity_2m": "0-1",
    "wind_u_10m": "m/s",
    "wind_v_10m": "m/s",
    "wind_speed": "m/s",
    "pressure_surface": "Pa",
    "shortwave_down": "W/m2",
    "net_radiation": "W/m2",
}
ERA5_STANDARD_UNITS: dict[str, str] = {
    **STANDARD_UNITS,
    "prcp_rate_or_amount": "mm/day",
}
IFS_STANDARD_UNITS: dict[str, str] = {
    **STANDARD_UNITS,
    "surface_pressure": "Pa",
    "prcp_rate_or_amount": "mm",
}
CONVERSION_PARAMS: dict[str, str] = {
    "tmp2m": "K_to_C",
    "apcp": "cumulative_to_period",
    "rh2m": "pct_to_frac",
    "u10m": "pass_through",
    "v10m": "pass_through",
    "pressfc": "pass_through",
    "dswrf": "pass_through",
    "2m_temperature": "K_to_C",
    "2m_dewpoint_temperature": "dewpoint_magnus_rh",
    "10m_u_component_of_wind": "pass_through",
    "10m_v_component_of_wind": "pass_through",
    "surface_pressure": "pass_through",
    "total_precipitation": "cumulative_m_to_mm_day",
    "surface_net_solar_radiation": "cumulative_j_m2_to_w_m2",
    "surface_net_thermal_radiation": "cumulative_j_m2_to_w_m2",
    "2t": "K_to_C",
    "2d": "dewpoint_magnus_rh",
    "tp": "cumulative_m_to_mm_step",
    "10u": "pass_through",
    "10v": "pass_through",
    "sp": "pass_through",
    "ssr": "cumulative_j_m2_to_w_m2",
    "str": "cumulative_j_m2_to_w_m2",
}
CFGRIB_VARIABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "tmp2m": ("tmp2m", "t2m", "2t"),
    "apcp": ("apcp", "tp", "total_precipitation"),
    "rh2m": ("rh2m", "r2", "2r"),
    "u10m": ("u10m", "u10", "10u"),
    "v10m": ("v10m", "v10", "10v"),
    "pressfc": ("pressfc", "sp", "pres"),
    "dswrf": ("dswrf", "ssrd", "sdswrf"),
    "2m_temperature": ("2m_temperature", "t2m", "2t"),
    "2m_dewpoint_temperature": ("2m_dewpoint_temperature", "d2m", "2d"),
    "10m_u_component_of_wind": ("10m_u_component_of_wind", "u10", "10u"),
    "10m_v_component_of_wind": ("10m_v_component_of_wind", "v10", "10v"),
    "surface_pressure": ("surface_pressure", "sp"),
    "total_precipitation": ("total_precipitation", "tp"),
    "surface_net_solar_radiation": ("surface_net_solar_radiation", "ssr"),
    "surface_net_thermal_radiation": ("surface_net_thermal_radiation", "str"),
    "2t": ("2t", "t2m"),
    "2d": ("2d", "d2m"),
    "10u": ("10u", "u10"),
    "10v": ("10v", "v10"),
    "tp": ("tp",),
    "sp": ("sp",),
    "ssr": ("ssr",),
    "str": ("str",),
}


class CanonicalRepository(Protocol):
    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None: ...

    def upsert_canonical_product(self, record: Mapping[str, Any]) -> dict[str, Any]: ...

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None: ...


class CanonicalConversionError(RuntimeError):
    """Raised when canonical conversion cannot complete for a cycle."""


@dataclass(frozen=True)
class CanonicalConverterConfig:
    source_id: str = "gfs"
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    converter_version: str = "m1.0"
    grid_id: str = "gfs_0p25"
    grid_definition_uri: str = "grids/gfs_0p25.json"
    native_time_resolution: str = "3h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )


@dataclass(frozen=True)
class ERA5CanonicalConverterConfig(CanonicalConverterConfig):
    source_id: str = "ERA5"
    converter_version: str = "m2.0"
    grid_id: str = "era5_0p25"
    grid_definition_uri: str = "grids/era5_0p25.json"
    native_time_resolution: str = "1h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(ERA5_VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )


@dataclass(frozen=True)
class IFSCanonicalConverterConfig(CanonicalConverterConfig):
    source_id: str = "IFS"
    converter_version: str = "m4.0"
    grid_id: str = "ifs_0p25"
    grid_definition_uri: str = "grids/ifs_0p25.json"
    native_time_resolution: str = "3h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(IFS_VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )


@dataclass(frozen=True)
class RawRecord:
    source_file: str
    native_variable: str
    forecast_hour: int
    values: tuple[float, ...]


@dataclass(frozen=True)
class MissingForecastVariable:
    native_variable: str
    standard_variable: str
    forecast_hour: int


@dataclass(frozen=True)
class UnitConversionResult:
    values: tuple[float, ...]
    quality_flag: str = "ok"
    anomalies: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CanonicalProductResult:
    canonical_product_id: str
    variable: str
    valid_time: datetime
    lead_time_hours: int
    object_uri: str
    checksum: str
    status: str
    quality_flag: str = "ok"


@dataclass(frozen=True)
class ConversionResult:
    status: str
    products: tuple[CanonicalProductResult, ...]


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_cycle_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    candidate = value.strip()
    if len(candidate) == 10 and candidate.isdigit():
        return datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return ensure_utc(datetime.fromisoformat(candidate))


def format_cycle_time(value: str | datetime) -> str:
    return parse_cycle_time(value).strftime("%Y%m%d%H")


def map_variable(native_variable: str, mapping: Mapping[str, str] | None = None) -> str | None:
    return dict(mapping or VARIABLE_MAPPING).get(native_variable)


def unit_for_standard_variable(standard_variable: str) -> str:
    try:
        return STANDARD_UNITS[standard_variable]
    except KeyError as error:
        raise CanonicalConversionError(f"No standard unit configured for {standard_variable}") from error


def convert_units(
    native_variable: str,
    values: tuple[float, ...] | list[float],
    previous_values: tuple[float, ...] | list[float] | None = None,
) -> tuple[float, ...]:
    return convert_units_with_metadata(native_variable, values, previous_values).values


def convert_units_with_metadata(
    native_variable: str,
    values: tuple[float, ...] | list[float],
    previous_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> UnitConversionResult:
    current = tuple(float(value) for value in values)
    if native_variable in {"tmp2m", "2m_temperature", "2m_dewpoint_temperature", "2t", "2d"}:
        return UnitConversionResult(tuple(value - 273.15 for value in current))
    if native_variable == "apcp":
        previous = (
            tuple(float(value) for value in previous_values) if previous_values is not None else (0.0,) * len(current)
        )
        if len(previous) != len(current):
            raise CanonicalConversionError("APCP previous/current value arrays must have the same length.")
        deltas = tuple(current_value - previous_value for current_value, previous_value in zip(current, previous))
        negative_deltas = tuple(delta for delta in deltas if delta < 0.0)
        anomalies: tuple[dict[str, Any], ...] = ()
        quality_flag = "ok"
        if negative_deltas:
            quality_flag = "warn"
            anomalies = (
                {
                    "type": "negative_apcp_delta",
                    "forecast_hour": forecast_hour,
                    "previous_forecast_hour": previous_forecast_hour,
                    "negative_count": len(negative_deltas),
                    "min_delta": min(negative_deltas),
                },
            )
        return UnitConversionResult(tuple(max(0.0, delta) for delta in deltas), quality_flag, anomalies)
    if native_variable == "total_precipitation":
        return convert_era5_precipitation_with_metadata(
            current,
            previous_values,
            forecast_hour=forecast_hour,
            previous_forecast_hour=previous_forecast_hour,
        )
    if native_variable == "rh2m":
        return UnitConversionResult(tuple(value / 100.0 for value in current))
    return UnitConversionResult(current)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def compute_relative_humidity(temperature_c: float, dewpoint_c: float) -> float:
    e_s = 6.112 * math.exp(17.67 * temperature_c / (temperature_c + 243.5))
    e_d = 6.112 * math.exp(17.67 * dewpoint_c / (dewpoint_c + 243.5))
    return clamp(e_d / e_s, 0.0, 1.0)


def compute_relative_humidity_values(
    temperature_c: tuple[float, ...] | list[float],
    dewpoint_c: tuple[float, ...] | list[float],
) -> tuple[float, ...]:
    if len(temperature_c) != len(dewpoint_c):
        raise CanonicalConversionError("Temperature and dewpoint arrays must have the same length.")
    return tuple(compute_relative_humidity(float(t), float(td)) for t, td in zip(temperature_c, dewpoint_c))


def convert_era5_precipitation_with_metadata(
    values_m: tuple[float, ...] | list[float],
    previous_values_m: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> UnitConversionResult:
    current = tuple(float(value) for value in values_m)
    previous = (
        tuple(float(value) for value in previous_values_m) if previous_values_m is not None else (0.0,) * len(current)
    )
    if len(previous) != len(current):
        raise CanonicalConversionError("ERA5 precipitation previous/current arrays must have the same length.")

    step_hours = _step_hours(forecast_hour, previous_forecast_hour)
    deltas = tuple(current_value - previous_value for current_value, previous_value in zip(current, previous))
    negative_deltas = tuple(delta for delta in deltas if delta < 0.0)
    anomalies: tuple[dict[str, Any], ...] = ()
    quality_flag = "ok"
    if negative_deltas:
        quality_flag = "warn"
        anomalies = (
            {
                "type": "negative_era5_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(negative_deltas),
                "min_delta_m": min(negative_deltas),
            },
        )

    mm_per_day = tuple(max(0.0, delta) * 1000.0 * 24.0 / step_hours for delta in deltas)
    return UnitConversionResult(mm_per_day, quality_flag, anomalies)


def convert_era5_radiation_values(
    ssr_values: tuple[float, ...] | list[float],
    str_values: tuple[float, ...] | list[float],
    previous_ssr_values: tuple[float, ...] | list[float] | None = None,
    previous_str_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> tuple[float, ...]:
    ssr = tuple(float(value) for value in ssr_values)
    str_ = tuple(float(value) for value in str_values)
    previous_ssr = (
        tuple(float(value) for value in previous_ssr_values) if previous_ssr_values is not None else (0.0,) * len(ssr)
    )
    previous_str = (
        tuple(float(value) for value in previous_str_values) if previous_str_values is not None else (0.0,) * len(str_)
    )
    lengths = {len(ssr), len(str_), len(previous_ssr), len(previous_str)}
    if len(lengths) != 1:
        raise CanonicalConversionError("ERA5 radiation arrays must have the same length.")

    step_seconds = _step_hours(forecast_hour, previous_forecast_hour) * 3600.0
    return tuple(
        ((current_ssr - prior_ssr) + (current_str - prior_str)) / step_seconds
        for current_ssr, prior_ssr, current_str, prior_str in zip(ssr, previous_ssr, str_, previous_str)
    )


def compute_ifs_relative_humidity(temperature_c: float, dewpoint_c: float) -> float:
    e_s = math.exp(17.625 * temperature_c / (243.04 + temperature_c))
    e_d = math.exp(17.625 * dewpoint_c / (243.04 + dewpoint_c))
    return clamp(e_d / e_s, 0.0, 1.0)


def compute_ifs_relative_humidity_values(
    temperature_c: tuple[float, ...] | list[float],
    dewpoint_c: tuple[float, ...] | list[float],
) -> tuple[float, ...]:
    if len(temperature_c) != len(dewpoint_c):
        raise CanonicalConversionError("IFS temperature and dewpoint arrays must have the same length.")
    return tuple(compute_ifs_relative_humidity(float(t), float(td)) for t, td in zip(temperature_c, dewpoint_c))


def convert_ifs_precipitation_with_metadata(
    values_m: tuple[float, ...] | list[float],
    previous_values_m: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
    consecutive_negative_count: int = 0,
) -> tuple[UnitConversionResult, int, float]:
    current = tuple(float(value) for value in values_m)
    previous = (
        tuple(float(value) for value in previous_values_m) if previous_values_m is not None else (0.0,) * len(current)
    )
    if len(previous) != len(current):
        raise CanonicalConversionError("IFS precipitation previous/current arrays must have the same length.")

    step_hours = _ifs_step_hours(forecast_hour, previous_forecast_hour)
    deltas_mm = tuple(
        (current_value - previous_value) * 1000.0 for current_value, previous_value in zip(current, previous)
    )
    small_negatives = tuple(delta for delta in deltas_mm if -0.01 < delta < 0.0)
    significant_negatives = tuple(delta for delta in deltas_mm if delta <= -0.01)
    anomalies: list[dict[str, Any]] = []
    next_consecutive_negative_count = 0
    quality_flag = "ok"

    if small_negatives:
        anomalies.append(
            {
                "type": "small_negative_ifs_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(small_negatives),
                "min_delta_mm": min(small_negatives),
            }
        )
    if significant_negatives:
        next_consecutive_negative_count = consecutive_negative_count + 1
        quality_flag = "warning_negative_precip"
        if next_consecutive_negative_count >= 3:
            quality_flag = "error_precip_accumulation"
        anomalies.append(
            {
                "type": "negative_ifs_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(significant_negatives),
                "min_delta_mm": min(significant_negatives),
                "consecutive_negative_count": next_consecutive_negative_count,
            }
        )

    values = tuple(max(0.0, delta) for delta in deltas_mm)
    return UnitConversionResult(values, quality_flag, tuple(anomalies)), next_consecutive_negative_count, step_hours


def convert_ifs_radiation_values(
    ssr_values: tuple[float, ...] | list[float],
    str_values: tuple[float, ...] | list[float],
    previous_ssr_values: tuple[float, ...] | list[float] | None = None,
    previous_str_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> tuple[tuple[float, ...], float]:
    ssr = tuple(float(value) for value in ssr_values)
    str_ = tuple(float(value) for value in str_values)
    previous_ssr = (
        tuple(float(value) for value in previous_ssr_values) if previous_ssr_values is not None else (0.0,) * len(ssr)
    )
    previous_str = (
        tuple(float(value) for value in previous_str_values) if previous_str_values is not None else (0.0,) * len(str_)
    )
    lengths = {len(ssr), len(str_), len(previous_ssr), len(previous_str)}
    if len(lengths) != 1:
        raise CanonicalConversionError("IFS radiation arrays must have the same length.")

    step_hours = _ifs_step_hours(forecast_hour, previous_forecast_hour)
    step_seconds = step_hours * 3600.0
    values = tuple(
        ((current_ssr - prior_ssr) + (current_str - prior_str)) / step_seconds
        for current_ssr, prior_ssr, current_str, prior_str in zip(ssr, previous_ssr, str_, previous_str)
    )
    return values, step_hours


def _step_hours(forecast_hour: int | None, previous_forecast_hour: int | None) -> float:
    if forecast_hour is None or previous_forecast_hour is None:
        return 1.0
    return float(max(1, forecast_hour - previous_forecast_hour))


def _ifs_step_hours(forecast_hour: int | None, previous_forecast_hour: int | None) -> float:
    if forecast_hour is None:
        return 3.0
    if previous_forecast_hour is None:
        return float(forecast_hour) if forecast_hour > 0 else 3.0
    return float(max(1, forecast_hour - previous_forecast_hour))


def compute_time_axis(cycle_time: str | datetime, forecast_hours: list[int]) -> list[dict[str, Any]]:
    parsed_cycle_time = parse_cycle_time(cycle_time)
    return [
        {
            "valid_time": parsed_cycle_time + timedelta(hours=forecast_hour),
            "lead_time_hours": forecast_hour,
        }
        for forecast_hour in forecast_hours
    ]


class CanonicalConverter:
    def __init__(
        self,
        *,
        config: CanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self.config = config or CanonicalConverterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.workspace_root,
            object_store_prefix=self.config.object_store_prefix,
        )

    @classmethod
    def from_env(cls) -> CanonicalConverter:
        config = CanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def load_manifest(self, manifest_uri: str) -> dict[str, Any]:
        try:
            return json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
        except (json.JSONDecodeError, OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to load manifest {manifest_uri}: {error}") from error

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            raw_records = self._read_records(entries)
            missing_pairs = self._missing_required_pairs(manifest, entries, raw_records)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            grouped = self._group_records(raw_records)
            missing_variables = sorted(set(self.config.variable_mapping.values()) - set(grouped))
            if missing_variables:
                raise CanonicalConversionError(f"Missing required canonical variables: {', '.join(missing_variables)}")

            products: list[CanonicalProductResult] = []
            for standard_variable in sorted(grouped):
                native_records = sorted(grouped[standard_variable], key=lambda record: record.forecast_hour)
                previous_values: tuple[float, ...] | None = None
                previous_source_file: str | None = None
                previous_forecast_hour: int | None = None
                for record in native_records:
                    product = self._convert_record(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable=standard_variable,
                        record=record,
                        previous_values=previous_values,
                        previous_source_file=previous_source_file,
                        previous_forecast_hour=previous_forecast_hour,
                    )
                    products.append(product)
                    previous_values = record.values
                    previous_source_file = record.source_file
                    previous_forecast_hour = record.forecast_hour

            self._update_cycle_status(cycle_time, status="canonical_ready", error_code="", error_message="")
            return ConversionResult(status="canonical_ready", products=tuple(products))
        except Exception as error:
            self._update_cycle_status(
                cycle_time,
                status="failed_convert",
                error_code="CONVERT_FAILED",
                error_message=str(error),
            )
            raise

    def convert_manifest_uri(self, manifest_uri: str) -> ConversionResult:
        return self.convert_manifest(self.load_manifest(manifest_uri))

    def _read_records(self, entries: list[dict[str, Any]]) -> list[RawRecord]:
        records: list[RawRecord] = []
        for entry in entries:
            native_variable = entry["variable"]
            standard_variable = map_variable(native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", native_variable, entry["local_key"])
                continue
            records.append(self._read_record(entry))
        return records

    def _read_record(self, entry: Mapping[str, Any]) -> RawRecord:
        return self._read_record_with_xarray(entry)

    def _read_record_with_xarray(self, entry: Mapping[str, Any]) -> RawRecord:
        local_key = str(entry["local_key"])
        try:
            import xarray as xr
        except ImportError as error:
            raise CanonicalConversionError(
                f"Cannot parse raw file {local_key}; install xarray, cfgrib, and netCDF4."
            ) from error

        dataset = None
        file_path = self.object_store.resolve_path(local_key)
        cfgrib_error: Exception | None = None
        try:
            try:
                dataset = xr.open_dataset(file_path, engine="cfgrib")
            except Exception as _cfgrib_err:
                cfgrib_error = _cfgrib_err
                dataset = xr.open_dataset(file_path, engine="netcdf4")
            expected_native_variable = str(entry["variable"])
            data_variable = self._select_data_variable(dataset, expected_native_variable, local_key)
            values = tuple(float(value) for value in dataset[data_variable].values.ravel().tolist())
            return RawRecord(
                source_file=self.object_store.uri_for_key(local_key),
                native_variable=expected_native_variable,
                forecast_hour=int(entry["forecast_hour"]),
                values=values,
            )
        except Exception as error:
            detail = f"Failed to parse raw file {local_key}: {error}"
            if cfgrib_error is not None:
                detail += f" (cfgrib also failed: {cfgrib_error})"
            raise CanonicalConversionError(detail) from error
        finally:
            if dataset is not None:
                dataset.close()

    def _select_data_variable(self, dataset: Any, expected_native_variable: str, local_key: str) -> str:
        return self._select_cfgrib_data_variable(dataset, expected_native_variable, local_key)

    def _select_cfgrib_data_variable(self, dataset: Any, expected_native_variable: str, local_key: str) -> str:
        expected_names = set(self.config.cfgrib_variable_aliases.get(expected_native_variable, ()))
        expected_names.add(expected_native_variable)
        matches: list[str] = []
        available: list[str] = []
        for data_variable in dataset.data_vars:
            variable_attrs = dataset[data_variable].attrs
            candidates = {
                str(data_variable),
                str(variable_attrs.get("GRIB_shortName", "")),
                str(variable_attrs.get("shortName", "")),
            }
            available.append("/".join(sorted(candidate for candidate in candidates if candidate)))
            if candidates & expected_names:
                matches.append(str(data_variable))

        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CanonicalConversionError(
                (
                    f"cfgrib variable mismatch for {local_key}: manifest expected {expected_native_variable} "
                    f"(aliases: {sorted(expected_names)}); dataset variables: {available}."
                )
            )
        raise CanonicalConversionError(
            (
                f"cfgrib variable mapping for {local_key} is ambiguous: manifest expected "
                f"{expected_native_variable}, matched {matches}."
            )
        )

    def _group_records(self, records: list[RawRecord]) -> dict[str, list[RawRecord]]:
        grouped: dict[str, list[RawRecord]] = {}
        for record in records:
            standard_variable = map_variable(record.native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", record.native_variable, record.source_file)
                continue
            grouped.setdefault(standard_variable, []).append(record)
        return grouped

    def _missing_required_pairs(
        self,
        manifest: Any,
        entries: list[dict[str, Any]],
        records: list[RawRecord],
    ) -> tuple[MissingForecastVariable, ...]:
        forecast_hours = self._configured_forecast_hours(manifest, entries)
        covered = {(record.native_variable, record.forecast_hour) for record in records}
        missing: list[MissingForecastVariable] = []
        for forecast_hour in forecast_hours:
            for native_variable, standard_variable in sorted(self.config.variable_mapping.items()):
                if (native_variable, forecast_hour) not in covered:
                    missing.append(
                        MissingForecastVariable(
                            native_variable=native_variable,
                            standard_variable=standard_variable,
                            forecast_hour=forecast_hour,
                        )
                    )
        return tuple(missing)

    def _configured_forecast_hours(self, manifest: Any, entries: list[dict[str, Any]]) -> list[int]:
        metadata = _manifest_metadata(manifest)
        if isinstance(metadata.get("forecast_hours"), list):
            return sorted({int(forecast_hour) for forecast_hour in metadata["forecast_hours"]})

        first_hour = metadata.get("first_forecast_hour")
        last_hour = metadata.get("last_forecast_hour")
        step_hours = self._native_time_resolution_hours()
        if first_hour is not None and last_hour is not None and step_hours is not None:
            return list(range(int(first_hour), int(last_hour) + 1, step_hours))

        return sorted({int(entry["forecast_hour"]) for entry in entries})

    def _native_time_resolution_hours(self) -> int | None:
        resolution = self.config.native_time_resolution.strip().lower()
        if not resolution.endswith("h"):
            return None
        try:
            step_hours = int(resolution[:-1])
        except ValueError:
            return None
        return step_hours if step_hours > 0 else None

    def _missing_pairs_message(self, missing_pairs: tuple[MissingForecastVariable, ...]) -> str:
        details = ", ".join(
            f"{pair.native_variable}->{pair.standard_variable} f{pair.forecast_hour:03d}" for pair in missing_pairs[:20]
        )
        suffix = ""
        if len(missing_pairs) > 20:
            suffix = f", ... ({len(missing_pairs)} total missing pairs)"
        return f"Missing required canonical variables forecast-hour coverage: {details}{suffix}"

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = (
                f"canonical/{source_id}/{compact_cycle}/{pair.standard_variable}/{canonical_product_id}.missing"
            )
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _convert_record(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        record: RawRecord,
        previous_values: tuple[float, ...] | None,
        previous_source_file: str | None,
        previous_forecast_hour: int | None,
    ) -> CanonicalProductResult:
        conversion = convert_units_with_metadata(
            record.native_variable,
            record.values,
            previous_values,
            forecast_hour=record.forecast_hour,
            previous_forecast_hour=previous_forecast_hour,
        )
        valid_time = cycle_time + timedelta(hours=record.forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{record.forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{compact_cycle}/{standard_variable}/{canonical_product_id}.nc"
        source_files = [record.source_file]
        if record.native_variable == "apcp" and previous_source_file is not None:
            source_files = [previous_source_file, record.source_file]
        conversion_params: dict[str, Any] = {
            "native_variable": record.native_variable,
            "operation": CONVERSION_PARAMS.get(record.native_variable, "pass_through"),
        }
        if conversion.anomalies:
            conversion_params["anomalies"] = list(conversion.anomalies)
            conversion_params["negative_delta_forecast_hours"] = [
                anomaly["forecast_hour"]
                for anomaly in conversion.anomalies
                if anomaly.get("type") == "negative_apcp_delta"
            ]
        lineage_json = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": conversion_params,
            "converter_version": self.config.converter_version,
        }
        content = self._serialize_product(
            variable=standard_variable,
            values=conversion.values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=record.forecast_hour,
            unit=unit_for_standard_variable(standard_variable),
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=record.forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        record_payload = {
            "canonical_product_id": canonical_product_id,
            "source_id": source_id,
            "source_version": compact_cycle,
            "cycle_time": cycle_time,
            "valid_time": valid_time,
            "lead_time_hours": record.forecast_hour,
            "variable": standard_variable,
            "unit": unit_for_standard_variable(standard_variable),
            "grid_id": self.config.grid_id,
            "grid_definition_uri": self.config.grid_definition_uri,
            "native_time_resolution": self.config.native_time_resolution,
            "native_spatial_resolution": self.config.native_spatial_resolution,
            "object_uri": object_uri,
            "checksum": checksum,
            "quality_flag": conversion.quality_flag,
            "lineage_json": lineage_json,
        }
        self._upsert_product(record_payload)
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=record.forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=conversion.quality_flag,
        )

    def _serialize_product(
        self,
        *,
        variable: str,
        values: tuple[float, ...],
        cycle_time: datetime,
        valid_time: datetime,
        lead_time_hours: int,
        unit: str,
        lineage_json: Mapping[str, Any],
    ) -> bytes:
        try:
            import netCDF4  # noqa: F401
            import xarray as xr
        except ImportError as error:
            raise CanonicalConversionError(
                "NetCDF4 serialization requires xarray and netCDF4; install both dependencies."
            ) from error

        dataset = xr.Dataset(
            data_vars={variable: ("point", list(values))},
            coords={"point": list(range(len(values)))},
            attrs={
                "cycle_time": cycle_time.isoformat(),
                "valid_time": valid_time.isoformat(),
                "lead_time_hours": lead_time_hours,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "lineage_json": json.dumps(dict(lineage_json), sort_keys=True),
            },
        )
        try:
            with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
                dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
                temp_file.seek(0)
                return temp_file.read()
        except (OSError, ValueError, RuntimeError) as error:
            raise CanonicalConversionError(f"Failed to serialize NetCDF4 product {variable}: {error}") from error
        finally:
            dataset.close()

    def _get_existing_product(self, canonical_product_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        try:
            return self.repository.get_canonical_product(canonical_product_id=canonical_product_id)
        except Exception:
            LOGGER.exception("Failed to read canonical product %s", canonical_product_id)
            raise

    def _existing_product_is_current(
        self,
        existing: Mapping[str, Any] | None,
        object_key: str,
        checksum: str,
    ) -> bool:
        if existing is None or existing.get("quality_flag") == "fail":
            return False
        existing_checksum = str(existing.get("checksum", ""))
        if existing_checksum != checksum:
            return False
        try:
            return self.object_store.exists(object_key) and self.object_store.checksum(object_key) == checksum
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to verify existing canonical object %s", object_key)
            return False

    def _upsert_product(self, record: Mapping[str, Any]) -> None:
        if self.repository is None:
            return
        try:
            self.repository.upsert_canonical_product(record)
        except Exception:
            LOGGER.exception("Failed to upsert canonical product %s", record["canonical_product_id"])
            raise

    def _update_cycle_status(
        self,
        cycle_time: datetime,
        *,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.repository is None:
            return
        try:
            self.repository.update_forecast_cycle(
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            LOGGER.exception("Failed to update forecast cycle conversion status for %s", format_cycle_time(cycle_time))
            raise


class ERA5CanonicalConverter(CanonicalConverter):
    def __init__(
        self,
        *,
        config: ERA5CanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        super().__init__(
            config=config or ERA5CanonicalConverterConfig(),
            repository=repository,
            object_store=object_store,
        )

    @classmethod
    def from_env(cls) -> ERA5CanonicalConverter:
        config = ERA5CanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            raw_records = self._read_records(entries)
            missing_pairs = self._missing_required_pairs(manifest, entries, raw_records)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            records_by_hour = self._records_by_hour_and_variable(raw_records)
            forecast_hours = self._configured_forecast_hours(manifest, entries)
            products: list[CanonicalProductResult] = []
            previous_precipitation: RawRecord | None = None
            previous_ssr: RawRecord | None = None
            previous_str: RawRecord | None = None

            for forecast_hour in forecast_hours:
                records = records_by_hour[forecast_hour]
                temperature = records["2m_temperature"]
                dewpoint = records["2m_dewpoint_temperature"]
                wind_u = records["10m_u_component_of_wind"]
                wind_v = records["10m_v_component_of_wind"]
                pressure = records["surface_pressure"]
                precipitation = records["total_precipitation"]
                ssr = records["surface_net_solar_radiation"]
                str_ = records["surface_net_thermal_radiation"]

                temperature_c = convert_units("2m_temperature", temperature.values)
                dewpoint_c = convert_units("2m_dewpoint_temperature", dewpoint.values)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="air_temperature_2m",
                        forecast_hour=forecast_hour,
                        values=temperature_c,
                        unit=self._unit_for_standard_variable("air_temperature_2m"),
                        source_files=[temperature.source_file],
                        conversion_params={"native_variable": temperature.native_variable, "operation": "K_to_C"},
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="relative_humidity_2m",
                        forecast_hour=forecast_hour,
                        values=compute_relative_humidity_values(temperature_c, dewpoint_c),
                        unit=self._unit_for_standard_variable("relative_humidity_2m"),
                        source_files=[temperature.source_file, dewpoint.source_file],
                        conversion_params={
                            "native_variables": [temperature.native_variable, dewpoint.native_variable],
                            "operation": "dewpoint_magnus_rh",
                        },
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_u_10m",
                        forecast_hour=forecast_hour,
                        values=wind_u.values,
                        unit=self._unit_for_standard_variable("wind_u_10m"),
                        source_files=[wind_u.source_file],
                        conversion_params={"native_variable": wind_u.native_variable, "operation": "pass_through"},
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_v_10m",
                        forecast_hour=forecast_hour,
                        values=wind_v.values,
                        unit=self._unit_for_standard_variable("wind_v_10m"),
                        source_files=[wind_v.source_file],
                        conversion_params={"native_variable": wind_v.native_variable, "operation": "pass_through"},
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_speed",
                        forecast_hour=forecast_hour,
                        values=self._wind_speed_values(wind_u.values, wind_v.values),
                        unit=self._unit_for_standard_variable("wind_speed"),
                        source_files=[wind_u.source_file, wind_v.source_file],
                        conversion_params={
                            "native_variables": [wind_u.native_variable, wind_v.native_variable],
                            "operation": "vector_magnitude",
                        },
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="pressure_surface",
                        forecast_hour=forecast_hour,
                        values=pressure.values,
                        unit=self._unit_for_standard_variable("pressure_surface"),
                        source_files=[pressure.source_file],
                        conversion_params={"native_variable": pressure.native_variable, "operation": "pass_through"},
                    )
                )

                precipitation_conversion = convert_era5_precipitation_with_metadata(
                    precipitation.values,
                    previous_precipitation.values if previous_precipitation is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_precipitation.forecast_hour
                    if previous_precipitation is not None
                    else None,
                )
                precipitation_sources = [precipitation.source_file]
                if previous_precipitation is not None:
                    precipitation_sources.insert(0, previous_precipitation.source_file)
                precipitation_params: dict[str, Any] = {
                    "native_variable": precipitation.native_variable,
                    "operation": "cumulative_m_to_mm_day",
                    "accumulation_type": "since_midnight",
                }
                if precipitation_conversion.anomalies:
                    precipitation_params["anomalies"] = list(precipitation_conversion.anomalies)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="prcp_rate_or_amount",
                        forecast_hour=forecast_hour,
                        values=precipitation_conversion.values,
                        unit=self._unit_for_standard_variable("prcp_rate_or_amount"),
                        source_files=precipitation_sources,
                        conversion_params=precipitation_params,
                        quality_flag=precipitation_conversion.quality_flag,
                    )
                )

                radiation_values = convert_era5_radiation_values(
                    ssr.values,
                    str_.values,
                    previous_ssr.values if previous_ssr is not None else None,
                    previous_str.values if previous_str is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_ssr.forecast_hour if previous_ssr is not None else None,
                )
                radiation_sources = [ssr.source_file, str_.source_file]
                if previous_ssr is not None and previous_str is not None:
                    radiation_sources = [
                        previous_ssr.source_file,
                        previous_str.source_file,
                        ssr.source_file,
                        str_.source_file,
                    ]
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="net_radiation",
                        forecast_hour=forecast_hour,
                        values=radiation_values,
                        unit=self._unit_for_standard_variable("net_radiation"),
                        source_files=radiation_sources,
                        conversion_params={
                            "native_variables": [ssr.native_variable, str_.native_variable],
                            "operation": "cumulative_j_m2_to_w_m2_direct_net",
                            "accumulation_type": "since_midnight",
                        },
                        lineage_updates={"radiation_method": "direct_net"},
                    )
                )

                previous_precipitation = precipitation
                previous_ssr = ssr
                previous_str = str_

            self._update_cycle_status(cycle_time, status="canonical_ready", error_code="", error_message="")
            return ConversionResult(status="canonical_ready", products=tuple(products))
        except Exception as error:
            self._update_cycle_status(
                cycle_time,
                status="failed_convert",
                error_code="CONVERT_FAILED",
                error_message=str(error),
            )
            raise

    def _records_by_hour_and_variable(self, records: list[RawRecord]) -> dict[int, dict[str, RawRecord]]:
        grouped: dict[int, dict[str, RawRecord]] = {}
        for record in records:
            grouped.setdefault(record.forecast_hour, {})[record.native_variable] = record
        return grouped

    def _write_product(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        forecast_hour: int,
        values: tuple[float, ...],
        unit: str,
        source_files: list[str],
        conversion_params: Mapping[str, Any],
        quality_flag: str = "ok",
        lineage_updates: Mapping[str, Any] | None = None,
    ) -> CanonicalProductResult:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        date_key = cycle_time.strftime("%Y-%m-%d")
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{date_key}/{standard_variable}/{canonical_product_id}.nc"
        lineage_json: dict[str, Any] = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": dict(conversion_params),
            "converter_version": self.config.converter_version,
        }
        if lineage_updates:
            lineage_json.update(lineage_updates)
        content = self._serialize_product(
            variable=standard_variable,
            values=values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            unit=unit,
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        self._upsert_product(
            {
                "canonical_product_id": canonical_product_id,
                "source_id": source_id,
                "source_version": compact_cycle,
                "cycle_time": cycle_time,
                "valid_time": valid_time,
                "lead_time_hours": forecast_hour,
                "variable": standard_variable,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "grid_definition_uri": self.config.grid_definition_uri,
                "native_time_resolution": self.config.native_time_resolution,
                "native_spatial_resolution": self.config.native_spatial_resolution,
                "object_uri": object_uri,
                "checksum": checksum,
                "quality_flag": quality_flag,
                "lineage_json": lineage_json,
            }
        )
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=quality_flag,
        )

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        date_key = cycle_time.strftime("%Y-%m-%d")
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = f"canonical/{source_id}/{date_key}/{pair.standard_variable}/{canonical_product_id}.missing"
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": self._unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _unit_for_standard_variable(self, standard_variable: str) -> str:
        try:
            return ERA5_STANDARD_UNITS[standard_variable]
        except KeyError as error:
            raise CanonicalConversionError(f"No ERA5 standard unit configured for {standard_variable}") from error

    def _wind_speed_values(
        self,
        wind_u_values: tuple[float, ...],
        wind_v_values: tuple[float, ...],
    ) -> tuple[float, ...]:
        if len(wind_u_values) != len(wind_v_values):
            raise CanonicalConversionError("ERA5 wind u/v arrays must have the same length.")
        return tuple(
            math.sqrt((u_value * u_value) + (v_value * v_value))
            for u_value, v_value in zip(wind_u_values, wind_v_values)
        )


class IFSCanonicalConverter(CanonicalConverter):
    def __init__(
        self,
        *,
        config: IFSCanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        super().__init__(
            config=config or IFSCanonicalConverterConfig(),
            repository=repository,
            object_store=object_store,
        )

    @classmethod
    def from_env(cls) -> IFSCanonicalConverter:
        config = IFSCanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            raw_records = self._read_records(entries)
            missing_pairs = self._missing_required_pairs(manifest, entries, raw_records)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            records_by_hour = self._records_by_hour_and_variable(raw_records)
            forecast_hours = self._configured_forecast_hours(manifest, entries)
            products: list[CanonicalProductResult] = []
            previous_precipitation: RawRecord | None = None
            previous_ssr: RawRecord | None = None
            previous_str: RawRecord | None = None
            consecutive_negative_precipitation = 0

            for forecast_hour in forecast_hours:
                records = records_by_hour[forecast_hour]
                temperature = records["2t"]
                dewpoint = records["2d"]
                wind_u = records["10u"]
                wind_v = records["10v"]
                pressure = records["sp"]
                precipitation = records["tp"]
                ssr = records["ssr"]
                str_ = records["str"]

                temperature_c = convert_units("2t", temperature.values)
                dewpoint_c = convert_units("2t", dewpoint.values)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="air_temperature_2m",
                        forecast_hour=forecast_hour,
                        values=temperature_c,
                        unit=self._unit_for_standard_variable("air_temperature_2m"),
                        source_files=[temperature.source_file],
                        conversion_params={
                            "native_variable": temperature.native_variable,
                            "operation": "K_to_C",
                            "unit_conversion": "K_to_C",
                        },
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="relative_humidity_2m",
                        forecast_hour=forecast_hour,
                        values=compute_ifs_relative_humidity_values(temperature_c, dewpoint_c),
                        unit=self._unit_for_standard_variable("relative_humidity_2m"),
                        source_files=[temperature.source_file, dewpoint.source_file],
                        conversion_params={
                            "native_variables": [temperature.native_variable, dewpoint.native_variable],
                            "operation": "magnus_formula",
                            "derived_from": [temperature.native_variable, dewpoint.native_variable],
                            "method": "magnus_formula",
                        },
                        lineage_updates={
                            "derived_from": [temperature.native_variable, dewpoint.native_variable],
                            "method": "magnus_formula",
                        },
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_u_10m",
                        forecast_hour=forecast_hour,
                        values=wind_u.values,
                        unit=self._unit_for_standard_variable("wind_u_10m"),
                        source_files=[wind_u.source_file],
                        conversion_params={"native_variable": wind_u.native_variable, "operation": "pass_through"},
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_v_10m",
                        forecast_hour=forecast_hour,
                        values=wind_v.values,
                        unit=self._unit_for_standard_variable("wind_v_10m"),
                        source_files=[wind_v.source_file],
                        conversion_params={"native_variable": wind_v.native_variable, "operation": "pass_through"},
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="surface_pressure",
                        forecast_hour=forecast_hour,
                        values=pressure.values,
                        unit=self._unit_for_standard_variable("surface_pressure"),
                        source_files=[pressure.source_file],
                        conversion_params={"native_variable": pressure.native_variable, "operation": "pass_through"},
                    )
                )

                precipitation_conversion, consecutive_negative_precipitation, precip_step_hours = (
                    convert_ifs_precipitation_with_metadata(
                        precipitation.values,
                        previous_precipitation.values if previous_precipitation is not None else None,
                        forecast_hour=forecast_hour,
                        previous_forecast_hour=previous_precipitation.forecast_hour
                        if previous_precipitation is not None
                        else None,
                        consecutive_negative_count=consecutive_negative_precipitation,
                    )
                )
                if not precipitation_conversion.anomalies or precipitation_conversion.quality_flag == "ok":
                    consecutive_negative_precipitation = 0
                precipitation_sources = [precipitation.source_file]
                if previous_precipitation is not None:
                    precipitation_sources.insert(0, previous_precipitation.source_file)
                precipitation_params: dict[str, Any] = {
                    "native_variable": precipitation.native_variable,
                    "operation": "cumulative_m_to_mm_step",
                    "accumulation_type": "since_cycle",
                    "unit_conversion": "m_to_mm",
                    "step_hours": precip_step_hours,
                }
                if precipitation_conversion.anomalies:
                    precipitation_params["anomalies"] = list(precipitation_conversion.anomalies)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="prcp_rate_or_amount",
                        forecast_hour=forecast_hour,
                        values=precipitation_conversion.values,
                        unit=self._unit_for_standard_variable("prcp_rate_or_amount"),
                        source_files=precipitation_sources,
                        conversion_params=precipitation_params,
                        quality_flag=precipitation_conversion.quality_flag,
                    )
                )

                radiation_values, radiation_step_hours = convert_ifs_radiation_values(
                    ssr.values,
                    str_.values,
                    previous_ssr.values if previous_ssr is not None else None,
                    previous_str.values if previous_str is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_ssr.forecast_hour if previous_ssr is not None else None,
                )
                radiation_sources = [ssr.source_file, str_.source_file]
                if previous_ssr is not None and previous_str is not None:
                    radiation_sources = [
                        previous_ssr.source_file,
                        previous_str.source_file,
                        ssr.source_file,
                        str_.source_file,
                    ]
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="net_radiation",
                        forecast_hour=forecast_hour,
                        values=radiation_values,
                        unit=self._unit_for_standard_variable("net_radiation"),
                        source_files=radiation_sources,
                        conversion_params={
                            "native_variables": [ssr.native_variable, str_.native_variable],
                            "operation": "cumulative_j_m2_to_w_m2_direct_net",
                            "accumulation_type": "since_cycle",
                            "radiation_method": "direct_net",
                            "components": [ssr.native_variable, str_.native_variable],
                            "step_hours": radiation_step_hours,
                        },
                        lineage_updates={
                            "radiation_method": "direct_net",
                            "components": [ssr.native_variable, str_.native_variable],
                        },
                    )
                )

                previous_precipitation = precipitation
                previous_ssr = ssr
                previous_str = str_

            self._update_cycle_status(cycle_time, status="canonical_ready", error_code="", error_message="")
            return ConversionResult(status="canonical_ready", products=tuple(products))
        except Exception as error:
            self._update_cycle_status(
                cycle_time,
                status="failed_convert",
                error_code="CONVERT_FAILED",
                error_message=str(error),
            )
            raise

    def _records_by_hour_and_variable(self, records: list[RawRecord]) -> dict[int, dict[str, RawRecord]]:
        grouped: dict[int, dict[str, RawRecord]] = {}
        for record in records:
            grouped.setdefault(record.forecast_hour, {})[record.native_variable] = record
        return grouped

    def _write_product(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        forecast_hour: int,
        values: tuple[float, ...],
        unit: str,
        source_files: list[str],
        conversion_params: Mapping[str, Any],
        quality_flag: str = "ok",
        lineage_updates: Mapping[str, Any] | None = None,
    ) -> CanonicalProductResult:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{compact_cycle}/{standard_variable}/{canonical_product_id}.nc"
        lineage_json: dict[str, Any] = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": dict(conversion_params),
            "converter_version": self.config.converter_version,
        }
        if lineage_updates:
            lineage_json.update(lineage_updates)
        content = self._serialize_product(
            variable=standard_variable,
            values=values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            unit=unit,
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        self._upsert_product(
            {
                "canonical_product_id": canonical_product_id,
                "source_id": source_id,
                "source_version": compact_cycle,
                "cycle_time": cycle_time,
                "valid_time": valid_time,
                "lead_time_hours": forecast_hour,
                "variable": standard_variable,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "grid_definition_uri": self.config.grid_definition_uri,
                "native_time_resolution": self.config.native_time_resolution,
                "native_spatial_resolution": self.config.native_spatial_resolution,
                "object_uri": object_uri,
                "checksum": checksum,
                "quality_flag": quality_flag,
                "lineage_json": lineage_json,
            }
        )
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=quality_flag,
        )

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = (
                f"canonical/{source_id}/{compact_cycle}/{pair.standard_variable}/{canonical_product_id}.missing"
            )
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": self._unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _unit_for_standard_variable(self, standard_variable: str) -> str:
        try:
            return IFS_STANDARD_UNITS[standard_variable]
        except KeyError as error:
            raise CanonicalConversionError(f"No IFS standard unit configured for {standard_variable}") from error


def _manifest_value(manifest: Any, key: str) -> Any:
    if isinstance(manifest, Mapping):
        return manifest[key]
    return getattr(manifest, key)


def _manifest_metadata(manifest: Any) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest.get("metadata") or {})
    return dict(getattr(manifest, "metadata", {}) or {})


def _manifest_entries(manifest: Any) -> list[dict[str, Any]]:
    entries = _manifest_value(manifest, "entries")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif hasattr(entry, "as_dict"):
            normalized.append(entry.as_dict())
        else:
            normalized.append(
                {
                    "local_key": entry.local_key,
                    "variable": entry.variable,
                    "forecast_hour": entry.forecast_hour,
                }
            )
    return normalized
