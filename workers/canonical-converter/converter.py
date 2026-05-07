from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.mock_grib import MockGribError, decode_mock_grib2
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
STANDARD_UNITS: dict[str, str] = {
    "air_temperature_2m": "degC",
    "prcp_rate_or_amount": "mm",
    "relative_humidity_2m": "0-1",
    "wind_u_10m": "m/s",
    "wind_v_10m": "m/s",
    "pressure_surface": "Pa",
    "shortwave_down": "W/m2",
}
CONVERSION_PARAMS: dict[str, str] = {
    "tmp2m": "K_to_C",
    "apcp": "cumulative_to_period",
    "rh2m": "pct_to_frac",
    "u10m": "pass_through",
    "v10m": "pass_through",
    "pressfc": "pass_through",
    "dswrf": "pass_through",
}


class CanonicalRepository(Protocol):
    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        ...

    def upsert_canonical_product(self, record: Mapping[str, Any]) -> dict[str, Any]:
        ...

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
    ) -> dict[str, Any] | None:
        ...


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


@dataclass(frozen=True)
class RawRecord:
    source_file: str
    native_variable: str
    forecast_hour: int
    values: tuple[float, ...]


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
    current = tuple(float(value) for value in values)
    if native_variable == "tmp2m":
        return tuple(value - 273.15 for value in current)
    if native_variable == "apcp":
        previous = (
            tuple(float(value) for value in previous_values)
            if previous_values is not None
            else (0.0,) * len(current)
        )
        if len(previous) != len(current):
            raise CanonicalConversionError("APCP previous/current value arrays must have the same length.")
        return tuple(
            max(0.0, current_value - previous_value)
            for current_value, previous_value in zip(current, previous)
        )
    if native_variable == "rh2m":
        return tuple(value / 100.0 for value in current)
    return current


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
            raw_records = self._read_records(manifest)
            grouped = self._group_records(raw_records)
            missing_variables = sorted(set(self.config.variable_mapping.values()) - set(grouped))
            if missing_variables:
                raise CanonicalConversionError(f"Missing required canonical variables: {', '.join(missing_variables)}")

            products: list[CanonicalProductResult] = []
            for standard_variable in sorted(grouped):
                native_records = sorted(grouped[standard_variable], key=lambda record: record.forecast_hour)
                previous_values: tuple[float, ...] | None = None
                previous_source_file: str | None = None
                for record in native_records:
                    product = self._convert_record(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable=standard_variable,
                        record=record,
                        previous_values=previous_values,
                        previous_source_file=previous_source_file,
                    )
                    products.append(product)
                    previous_values = record.values
                    previous_source_file = record.source_file

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

    def _read_records(self, manifest: Any) -> list[RawRecord]:
        records: list[RawRecord] = []
        for entry in _manifest_entries(manifest):
            native_variable = entry["variable"]
            standard_variable = map_variable(native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", native_variable, entry["local_key"])
                continue
            records.append(self._read_record(entry))
        return records

    def _read_record(self, entry: Mapping[str, Any]) -> RawRecord:
        local_key = str(entry["local_key"])
        try:
            content = self.object_store.read_bytes(local_key)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to read raw file {local_key}: {error}") from error

        try:
            payload = decode_mock_grib2(content)
            return RawRecord(
                source_file=self.object_store.uri_for_key(local_key),
                native_variable=str(payload["variable"]),
                forecast_hour=int(payload["forecast_hour"]),
                values=tuple(float(value) for value in payload["values"]),
            )
        except (KeyError, TypeError, ValueError, MockGribError):
            LOGGER.debug("Raw file %s is not a synthetic mock GRIB2 payload; trying xarray/cfgrib.", local_key)

        return self._read_record_with_xarray(entry)

    def _read_record_with_xarray(self, entry: Mapping[str, Any]) -> RawRecord:
        local_key = str(entry["local_key"])
        try:
            import xarray as xr
        except ImportError as error:
            raise CanonicalConversionError(
                f"Cannot parse non-mock GRIB2 file {local_key}; install xarray and cfgrib."
            ) from error

        dataset = None
        try:
            dataset = xr.open_dataset(self.object_store.resolve_path(local_key), engine="cfgrib")
            data_variable = str(entry["variable"])
            if data_variable not in dataset.data_vars:
                data_variable = next(iter(dataset.data_vars))
            values = tuple(float(value) for value in dataset[data_variable].values.ravel().tolist())
            return RawRecord(
                source_file=self.object_store.uri_for_key(local_key),
                native_variable=str(entry["variable"]),
                forecast_hour=int(entry["forecast_hour"]),
                values=values,
            )
        except Exception as error:
            raise CanonicalConversionError(f"Failed to parse GRIB2 file {local_key}: {error}") from error
        finally:
            if dataset is not None:
                dataset.close()

    def _group_records(self, records: list[RawRecord]) -> dict[str, list[RawRecord]]:
        grouped: dict[str, list[RawRecord]] = {}
        for record in records:
            standard_variable = map_variable(record.native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", record.native_variable, record.source_file)
                continue
            grouped.setdefault(standard_variable, []).append(record)
        return grouped

    def _convert_record(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        record: RawRecord,
        previous_values: tuple[float, ...] | None,
        previous_source_file: str | None,
    ) -> CanonicalProductResult:
        converted_values = convert_units(record.native_variable, record.values, previous_values)
        valid_time = cycle_time + timedelta(hours=record.forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{record.forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{compact_cycle}/{standard_variable}/{canonical_product_id}.nc"
        source_files = [record.source_file]
        if record.native_variable == "apcp" and previous_source_file is not None:
            source_files = [previous_source_file, record.source_file]
        lineage_json = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": {
                "native_variable": record.native_variable,
                "operation": CONVERSION_PARAMS.get(record.native_variable, "pass_through"),
            },
            "converter_version": self.config.converter_version,
        }
        content = self._serialize_product(
            variable=standard_variable,
            values=converted_values,
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
            "quality_flag": "ok",
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
            import xarray as xr

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
            with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
                dataset.to_netcdf(temp_file.name, format="NETCDF4")
                temp_file.seek(0)
                return temp_file.read()
        except ImportError:
            pass
        except (OSError, ValueError, RuntimeError) as error:
            LOGGER.warning("Falling back to JSON-backed .nc payload for %s: %s", variable, error)

        fallback_payload = {
            "format": "netcdf4-json-fallback",
            "variable": variable,
            "values": list(values),
            "unit": unit,
            "cycle_time": cycle_time.isoformat(),
            "valid_time": valid_time.isoformat(),
            "lead_time_hours": lead_time_hours,
            "grid_id": self.config.grid_id,
            "lineage_json": dict(lineage_json),
        }
        return json.dumps(fallback_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

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


def _manifest_value(manifest: Any, key: str) -> Any:
    if isinstance(manifest, Mapping):
        return manifest[key]
    return getattr(manifest, key)


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
