from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes

LOGGER = logging.getLogger(__name__)

REQUIRED_CANONICAL_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "pressure_surface",
    "shortwave_down",
)
FORCING_VARIABLES: tuple[str, ...] = ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
OUTPUT_UNITS: dict[str, str] = {
    "PRCP": "mm",
    "TEMP": "degC",
    "RH": "0-1",
    "wind": "m/s",
    "Rn": "W/m2",
    "Press": "Pa",
}
CANONICAL_TO_FORCING: dict[str, str] = {
    "prcp_rate_or_amount": "PRCP",
    "air_temperature_2m": "TEMP",
    "relative_humidity_2m": "RH",
    "shortwave_down": "Rn",
    "pressure_surface": "Press",
}


class ForcingProductionError(RuntimeError):
    """Raised when forcing production cannot complete."""


@dataclass(frozen=True)
class MetStation:
    station_id: str
    basin_version_id: str
    longitude: float
    latitude: float
    elevation_m: float
    station_role: str
    station_name: str | None = None


@dataclass(frozen=True)
class GridPoint:
    grid_cell_id: str
    longitude: float
    latitude: float


@dataclass(frozen=True)
class InterpolationWeight:
    source_id: str
    grid_id: str
    model_id: str
    station_id: str
    variable: str
    grid_cell_id: str
    weight: float
    method: str = "idw"


@dataclass(frozen=True)
class CanonicalProduct:
    canonical_product_id: str
    source_id: str
    cycle_time: datetime
    valid_time: datetime
    variable: str
    unit: str
    grid_id: str
    object_uri: str
    checksum: str
    grid_definition_uri: str | None = None
    native_time_resolution: str | None = None
    native_spatial_resolution: str | None = None
    quality_flag: str = "ok"


@dataclass(frozen=True)
class CanonicalField:
    product: CanonicalProduct
    grid_points: tuple[GridPoint, ...]
    values_by_grid_cell_id: Mapping[str, float]


@dataclass(frozen=True)
class ForcingTimeseriesRow:
    forcing_version_id: str
    basin_version_id: str
    station_id: str
    valid_time: datetime
    source_id: str
    variable: str
    value: float
    unit: str
    native_resolution: str | None
    quality_flag: str = "ok"


@dataclass(frozen=True)
class ForcingComponent:
    forcing_version_id: str
    canonical_product_id: str
    variable: str
    valid_time_start: datetime
    valid_time_end: datetime
    role: str = "forcing_input"


@dataclass(frozen=True)
class ForcingProductionResult:
    status: str
    forcing_version_id: str
    forcing_package_uri: str
    checksum: str | None
    station_count: int
    timestep_count: int
    file_uris: Mapping[str, str] = field(default_factory=dict)


class ForcingRepository(Protocol):
    def resolve_model_basin_version(self, *, model_id: str) -> str:
        ...

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        ...

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        ...

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        ...

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        ...

    def get_forcing_version(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None:
        ...

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        ...

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None:
        ...

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: Sequence[ForcingTimeseriesRow],
    ) -> None:
        ...

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class ForcingProducerConfig:
    source_id: str = "gfs"
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    idw_power: float = 2.0
    idw_neighbors: int = 4
    rn_shortwave_factor: float = 1.0
    producer_version: str = "m1.0"
    forcing_filename: str = "forcing.tsd.forc"
    csv_filename: str = "forcing_debug.csv"
    package_manifest_filename: str = "forcing_package.json"
    output_variables: tuple[str, ...] = FORCING_VARIABLES
    required_canonical_variables: tuple[str, ...] = REQUIRED_CANONICAL_VARIABLES


class ForcingProducer:
    def __init__(
        self,
        *,
        config: ForcingProducerConfig | None = None,
        repository: ForcingRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self.config = config or ForcingProducerConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.workspace_root,
            object_store_prefix=self.config.object_store_prefix,
        )

    @classmethod
    def from_env(cls) -> ForcingProducer:
        from .store import PsycopgForcingRepository

        config = ForcingProducerConfig()
        repository = PsycopgForcingRepository(PsycopgMetStore.from_env().database_url)
        return cls(config=config, repository=repository)

    def produce(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
    ) -> ForcingProductionResult:
        if self.repository is None:
            raise ForcingProductionError("A forcing repository is required for production.")

        resolved_source_id = source_id or self.config.source_id
        parsed_cycle_time = parse_cycle_time(cycle_time)
        _safe_path_component(model_id)
        _safe_path_component(_object_source_segment(resolved_source_id))

        try:
            existing = self.repository.get_forcing_version(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                model_id=model_id,
            )
            if self._existing_forcing_version_is_current(existing):
                return ForcingProductionResult(
                    status="already_done",
                    forcing_version_id=str(existing["forcing_version_id"]),
                    forcing_package_uri=str(existing["forcing_package_uri"]),
                    checksum=str(existing["checksum"]),
                    station_count=int(existing["station_count"]),
                    timestep_count=0,
                    file_uris={},
                )

            basin_version_id = self.repository.resolve_model_basin_version(model_id=model_id)
            stations = self._load_valid_stations(basin_version_id=basin_version_id)
            products_by_variable = self._load_canonical_products(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
            )

            sample_field = self._read_canonical_field(_first_product(products_by_variable))
            grid_id = sample_field.product.grid_id
            grid_points = sample_field.grid_points
            weights = self._load_or_create_weights(
                source_id=resolved_source_id,
                grid_id=grid_id,
                model_id=model_id,
                stations=stations,
                grid_points=grid_points,
            )
            forcing_version_id = (
                str(existing["forcing_version_id"])
                if existing is not None
                else _forcing_version_id(resolved_source_id, parsed_cycle_time, model_id)
            )
            fields = self._read_fields(products_by_variable)
            values, components = self._generate_timeseries(
                forcing_version_id=forcing_version_id,
                basin_version_id=basin_version_id,
                source_id=resolved_source_id,
                products_by_variable=products_by_variable,
                fields=fields,
                stations=stations,
                weights=weights,
            )

            result = self._write_outputs_and_records(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                model_id=model_id,
                basin_version_id=basin_version_id,
                grid_id=grid_id,
                stations=stations,
                rows=values,
                components=components,
                products_by_variable=products_by_variable,
            )
            self.repository.update_forecast_cycle(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                status="forcing_ready",
                error_code="",
                error_message="",
            )
            return result
        except Exception as error:
            self._mark_failed(resolved_source_id, parsed_cycle_time, error)
            if isinstance(error, ForcingProductionError):
                raise
            raise ForcingProductionError(str(error)) from error

    def _load_valid_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        assert self.repository is not None
        loaded = self.repository.load_met_stations(basin_version_id=basin_version_id)
        stations: list[MetStation] = []
        for station in loaded:
            if not _valid_station(station):
                LOGGER.warning("Excluding invalid met station %s for basin %s", station.station_id, basin_version_id)
                continue
            stations.append(station)

        if not stations:
            raise ForcingProductionError(
                f"No active meteorological stations are defined for basin version {basin_version_id}."
            )
        return tuple(sorted(stations, key=lambda station: station.station_id))

    def _load_canonical_products(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> dict[str, dict[datetime, CanonicalProduct]]:
        assert self.repository is not None
        products = self.repository.list_canonical_products(source_id=source_id, cycle_time=cycle_time)
        products_by_variable: dict[str, dict[datetime, CanonicalProduct]] = {
            variable: {} for variable in self.config.required_canonical_variables
        }
        for product in products:
            if product.variable not in products_by_variable:
                continue
            if product.quality_flag == "fail" or not product.checksum:
                continue
            products_by_variable[product.variable][product.valid_time] = product

        missing = _missing_product_details(products_by_variable, self.config.required_canonical_variables)
        if missing:
            raise ForcingProductionError(f"Missing required canonical products: {', '.join(missing)}")

        grid_ids = {
            product.grid_id
            for products_for_variable in products_by_variable.values()
            for product in products_for_variable.values()
        }
        if len(grid_ids) != 1:
            raise ForcingProductionError(f"Canonical products must share one grid_id; found {sorted(grid_ids)}.")
        return products_by_variable

    def _load_or_create_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
        stations: Sequence[MetStation],
        grid_points: Sequence[GridPoint],
    ) -> tuple[InterpolationWeight, ...]:
        assert self.repository is not None
        existing = self.repository.load_interp_weights(source_id=source_id, grid_id=grid_id, model_id=model_id)
        if _weights_cover(existing, stations, self.config.output_variables):
            _validate_weight_sums(existing)
            return tuple(existing)

        computed = compute_idw_weights(
            stations=stations,
            grid_points=grid_points,
            variables=self.config.output_variables,
            source_id=source_id,
            grid_id=grid_id,
            model_id=model_id,
            neighbors=self.config.idw_neighbors,
            power=self.config.idw_power,
        )
        self.repository.upsert_interp_weights(computed)
        return computed

    def _read_fields(
        self,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    ) -> dict[str, dict[datetime, CanonicalField]]:
        fields: dict[str, dict[datetime, CanonicalField]] = {}
        for variable, products_for_variable in products_by_variable.items():
            fields[variable] = {}
            for valid_time, product in products_for_variable.items():
                fields[variable][valid_time] = self._read_canonical_field(product)
        return fields

    def _read_canonical_field(self, product: CanonicalProduct) -> CanonicalField:
        try:
            import xarray as xr
        except ImportError as error:
            raise ForcingProductionError("Reading canonical NetCDF4 products requires xarray.") from error

        dataset = None
        try:
            path = self.object_store.resolve_path(product.object_uri)
            dataset = xr.open_dataset(path)
            data_variable = _select_data_variable(dataset, product.variable)
            data_array = dataset[data_variable]
            raw_values = data_array.values
            values = tuple(float(value) for value in raw_values.ravel().tolist())
            grid_points = self._grid_points_for_dataset(product, dataset, data_array, len(values))
            if len(grid_points) != len(values):
                raise ForcingProductionError(
                    f"Canonical product {product.canonical_product_id} has {len(values)} values but "
                    f"{len(grid_points)} grid points."
                )
            _validate_grid_points(grid_points, product.canonical_product_id)
            _validate_field_values(values, grid_points, product.canonical_product_id)
            return CanonicalField(
                product=product,
                grid_points=grid_points,
                values_by_grid_cell_id={
                    point.grid_cell_id: value for point, value in zip(grid_points, values, strict=True)
                },
            )
        except (OSError, ObjectStoreError, TypeError, ValueError) as error:
            raise ForcingProductionError(
                f"Failed to read canonical product {product.canonical_product_id}: {error}"
            ) from error
        finally:
            if dataset is not None:
                dataset.close()

    def _grid_points_for_dataset(
        self,
        product: CanonicalProduct,
        dataset: Any,
        data_array: Any,
        expected_count: int,
    ) -> tuple[GridPoint, ...]:
        grid_from_definition = self._grid_points_from_definition(product, expected_count)
        if grid_from_definition:
            return grid_from_definition

        raw_values = data_array.values
        shape = tuple(int(size) for size in getattr(raw_values, "shape", ()))
        grid_cell_ids = _grid_cell_ids(dataset, expected_count)
        direct_coords = _direct_lon_lat_coords(dataset, expected_count)
        if direct_coords is not None:
            longitudes, latitudes = direct_coords
            return tuple(
                GridPoint(grid_cell_id=grid_cell_id, longitude=longitude, latitude=latitude)
                for grid_cell_id, longitude, latitude in zip(grid_cell_ids, longitudes, latitudes, strict=True)
            )

        rectilinear_coords = _rectilinear_lon_lat_coords(dataset, shape)
        if rectilinear_coords is not None and len(rectilinear_coords) == expected_count:
            return tuple(
                GridPoint(grid_cell_id=grid_cell_id, longitude=longitude, latitude=latitude)
                for grid_cell_id, (longitude, latitude) in zip(grid_cell_ids, rectilinear_coords, strict=True)
            )

        raise ForcingProductionError(
            f"Canonical product {product.canonical_product_id} does not provide usable geographic grid "
            "coordinates. Provide a readable grid_definition_uri with finite lon/lat cells or NetCDF "
            "longitude/latitude coordinates."
        )

    def _grid_points_from_definition(
        self,
        product: CanonicalProduct,
        expected_count: int,
    ) -> tuple[GridPoint, ...] | None:
        if not product.grid_definition_uri:
            return None
        try:
            definition = json.loads(self.object_store.read_bytes(product.grid_definition_uri).decode("utf-8"))
        except Exception:
            return None

        cells = definition.get("cells") or definition.get("points")
        if isinstance(cells, list):
            points: list[GridPoint] = []
            for index, cell in enumerate(cells):
                if not isinstance(cell, Mapping):
                    continue
                try:
                    longitude = float(cell.get("lon", cell.get("longitude")))
                    latitude = float(cell.get("lat", cell.get("latitude")))
                except (TypeError, ValueError):
                    return None
                if not _valid_geographic_coordinate(longitude, latitude):
                    return None
                points.append(
                    GridPoint(
                        grid_cell_id=str(cell.get("grid_cell_id", cell.get("id", index))),
                        longitude=longitude,
                        latitude=latitude,
                    )
                )
            return tuple(points) if len(points) == expected_count else None
        return None

    def _generate_timeseries(
        self,
        *,
        forcing_version_id: str,
        basin_version_id: str,
        source_id: str,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        fields: Mapping[str, Mapping[datetime, CanonicalField]],
        stations: Sequence[MetStation],
        weights: Sequence[InterpolationWeight],
    ) -> tuple[tuple[ForcingTimeseriesRow, ...], tuple[ForcingComponent, ...]]:
        valid_times = _valid_times(products_by_variable)
        weights_by_station_variable = _weights_by_station_variable(weights)
        rows: list[ForcingTimeseriesRow] = []

        for valid_time in valid_times:
            station_values: dict[str, dict[str, float]] = {
                "PRCP": self._interpolate_forcing_variable(
                    "PRCP",
                    fields["prcp_rate_or_amount"][valid_time],
                    stations,
                    weights_by_station_variable,
                ),
                "TEMP": self._interpolate_forcing_variable(
                    "TEMP",
                    fields["air_temperature_2m"][valid_time],
                    stations,
                    weights_by_station_variable,
                ),
                "RH": self._interpolate_forcing_variable(
                    "RH",
                    fields["relative_humidity_2m"][valid_time],
                    stations,
                    weights_by_station_variable,
                ),
                "Rn": {
                    station_id: value * self.config.rn_shortwave_factor
                    for station_id, value in self._interpolate_forcing_variable(
                        "Rn",
                        fields["shortwave_down"][valid_time],
                        stations,
                        weights_by_station_variable,
                    ).items()
                },
                "Press": self._interpolate_forcing_variable(
                    "Press",
                    fields["pressure_surface"][valid_time],
                    stations,
                    weights_by_station_variable,
                ),
            }
            u_values = self._interpolate_forcing_variable(
                "wind",
                fields["wind_u_10m"][valid_time],
                stations,
                weights_by_station_variable,
            )
            v_values = self._interpolate_forcing_variable(
                "wind",
                fields["wind_v_10m"][valid_time],
                stations,
                weights_by_station_variable,
            )
            station_values["wind"] = {
                station.station_id: wind_speed(u_values[station.station_id], v_values[station.station_id])
                for station in stations
            }

            for variable in self.config.output_variables:
                native_resolution = _native_resolution_for_output(variable, products_by_variable, valid_time)
                for station in stations:
                    value = station_values[variable][station.station_id]
                    if not math.isfinite(value):
                        raise ForcingProductionError(
                            f"Interpolated forcing value is not finite for station {station.station_id} "
                            f"variable {variable} at {_format_time(valid_time)}."
                        )
                    rows.append(
                        ForcingTimeseriesRow(
                            forcing_version_id=forcing_version_id,
                            basin_version_id=basin_version_id,
                            station_id=station.station_id,
                            valid_time=valid_time,
                            source_id=source_id,
                            variable=variable,
                            value=value,
                            unit=OUTPUT_UNITS[variable],
                            native_resolution=native_resolution,
                        )
                    )

        components = tuple(
            ForcingComponent(
                forcing_version_id=forcing_version_id,
                canonical_product_id=product.canonical_product_id,
                variable=product.variable,
                valid_time_start=product.valid_time,
                valid_time_end=product.valid_time,
            )
            for products_for_variable in products_by_variable.values()
            for product in products_for_variable.values()
        )
        return tuple(rows), components

    def _interpolate_forcing_variable(
        self,
        variable: str,
        field: CanonicalField,
        stations: Sequence[MetStation],
        weights_by_station_variable: Mapping[tuple[str, str], tuple[InterpolationWeight, ...]],
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for station in stations:
            station_weights = weights_by_station_variable[(station.station_id, variable)]
            weighted_value = 0.0
            for weight in station_weights:
                try:
                    grid_value = field.values_by_grid_cell_id[weight.grid_cell_id]
                except KeyError as error:
                    raise ForcingProductionError(
                        f"Canonical product {field.product.canonical_product_id} does not contain "
                        f"grid cell {weight.grid_cell_id} required by interpolation weights."
                    ) from error
                weighted_value += grid_value * weight.weight
            values[station.station_id] = weighted_value
        return values

    def _write_outputs_and_records(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        basin_version_id: str,
        grid_id: str,
        stations: Sequence[MetStation],
        rows: Sequence[ForcingTimeseriesRow],
        components: Sequence[ForcingComponent],
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    ) -> ForcingProductionResult:
        assert self.repository is not None
        compact_cycle = format_cycle_time(cycle_time)
        source_segment = _object_source_segment(source_id)
        forcing_version_id = rows[0].forcing_version_id
        valid_times = sorted({row.valid_time for row in rows})
        prefix = f"forcing/{source_segment}/{compact_cycle}/{basin_version_id}/{model_id}"

        tsd_content = format_tsd_forc(rows, stations=stations, variables=self.config.output_variables).encode("utf-8")
        csv_content = format_debug_csv(rows).encode("utf-8")
        tsd_key = f"{prefix}/{self.config.forcing_filename}"
        csv_key = f"{prefix}/{self.config.csv_filename}"
        tsd_uri = self.object_store.write_bytes_atomic(tsd_key, tsd_content)
        csv_uri = self.object_store.write_bytes_atomic(csv_key, csv_content)
        tsd_checksum = sha256_bytes(tsd_content)
        csv_checksum = sha256_bytes(csv_content)

        package_uri = _directory_uri(self.object_store, prefix)
        lineage_json = {
            "producer_version": self.config.producer_version,
            "source_id": source_id,
            "cycle_time": _format_time(cycle_time),
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "grid_id": grid_id,
            "station_ids": [station.station_id for station in stations],
            "forcing_variables": list(self.config.output_variables),
            "canonical_product_ids": sorted(
                product.canonical_product_id
                for products_for_variable in products_by_variable.values()
                for product in products_for_variable.values()
            ),
        }
        package_manifest = {
            "forcing_version_id": forcing_version_id,
            "model_id": model_id,
            "source_id": source_id,
            "cycle_time": _format_time(cycle_time),
            "start_time": _format_time(valid_times[0]),
            "end_time": _format_time(valid_times[-1]),
            "basin_version_id": basin_version_id,
            "station_count": len(stations),
            "files": [
                {"role": "tsd_forc", "uri": tsd_uri, "checksum": tsd_checksum},
                {"role": "csv_debug", "uri": csv_uri, "checksum": csv_checksum},
            ],
            "lineage": lineage_json,
        }
        package_content = _json_bytes(package_manifest)
        package_checksum = sha256_bytes(package_content)
        package_manifest_uri = self.object_store.write_bytes_atomic(
            f"{prefix}/{self.config.package_manifest_filename}",
            package_content,
        )

        record = {
            "forcing_version_id": forcing_version_id,
            "model_id": model_id,
            "source_id": source_id,
            "cycle_time": cycle_time,
            "start_time": valid_times[0],
            "end_time": valid_times[-1],
            "station_count": len(stations),
            "forcing_package_uri": package_uri,
            "checksum": None,
            "lineage_json": {
                **lineage_json,
                "forcing_package_manifest_uri": package_manifest_uri,
                "output_files": package_manifest["files"],
            },
        }
        self.repository.upsert_forcing_version(record)
        self.repository.replace_forcing_components(forcing_version_id, components)
        self.repository.replace_forcing_timeseries(forcing_version_id, rows)
        self.repository.finalize_forcing_version(forcing_version_id, package_checksum)
        return ForcingProductionResult(
            status="forcing_ready",
            forcing_version_id=forcing_version_id,
            forcing_package_uri=package_uri,
            checksum=package_checksum,
            station_count=len(stations),
            timestep_count=len(valid_times),
            file_uris={
                "tsd_forc": tsd_uri,
                "csv_debug": csv_uri,
                "package_manifest": package_manifest_uri,
            },
        )

    def _existing_forcing_version_is_current(self, existing: Mapping[str, Any] | None) -> bool:
        if not existing:
            return False
        checksum = str(existing.get("checksum") or "").strip()
        package_uri = str(existing.get("forcing_package_uri") or "")
        if not checksum or checksum.lower() == "pending" or not package_uri:
            return False
        try:
            manifest_uri = _package_manifest_uri(package_uri, self.config.package_manifest_filename)
            return self.object_store.exists(manifest_uri) and self.object_store.checksum(manifest_uri) == checksum
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.warning("Existing forcing package checksum could not be verified for %s", package_uri)
            return False

    def _mark_failed(self, source_id: str, cycle_time: datetime, error: Exception) -> None:
        if self.repository is None:
            return
        try:
            self.repository.update_forecast_cycle(
                source_id=source_id,
                cycle_time=cycle_time,
                status="failed_forcing",
                error_code="FORCING_FAILED",
                error_message=str(error),
            )
        except Exception:
            LOGGER.exception(
                "Failed to update forecast cycle forcing failure status for %s",
                format_cycle_time(cycle_time),
            )


def compute_idw_weights(
    *,
    stations: Sequence[MetStation],
    grid_points: Sequence[GridPoint],
    variables: Sequence[str],
    source_id: str,
    grid_id: str,
    model_id: str,
    neighbors: int = 4,
    power: float = 2.0,
) -> tuple[InterpolationWeight, ...]:
    if not grid_points:
        raise ForcingProductionError("Cannot compute IDW weights without grid points.")
    if neighbors < 1:
        raise ForcingProductionError("IDW neighbor count must be at least 1.")
    if power <= 0.0:
        raise ForcingProductionError("IDW power must be positive.")
    for station in stations:
        if not _valid_station(station):
            raise ForcingProductionError(f"Station {station.station_id} has invalid geographic coordinates.")
    for point in grid_points:
        if not _valid_geographic_coordinate(point.longitude, point.latitude):
            raise ForcingProductionError(f"Grid point {point.grid_cell_id} has invalid geographic coordinates.")

    weights: list[InterpolationWeight] = []
    neighbor_count = min(neighbors, len(grid_points))
    for station in stations:
        distances = [
            (_distance_degrees(station.longitude, station.latitude, point.longitude, point.latitude), point)
            for point in grid_points
        ]
        distances.sort(key=lambda item: (item[0], item[1].grid_cell_id))
        exact_matches = [point for distance, point in distances if distance <= 1e-12]
        if exact_matches:
            selected = [(exact_matches[0], 1.0)]
        else:
            selected_points = distances[:neighbor_count]
            raw_weights = [(point, 1.0 / (distance**power)) for distance, point in selected_points]
            raw_sum = sum(weight for _, weight in raw_weights)
            selected = [(point, weight / raw_sum) for point, weight in raw_weights]

        for variable in variables:
            variable_weights = [
                InterpolationWeight(
                    source_id=source_id,
                    grid_id=grid_id,
                    model_id=model_id,
                    station_id=station.station_id,
                    variable=variable,
                    grid_cell_id=point.grid_cell_id,
                    weight=weight,
                    method="idw",
                )
                for point, weight in selected
            ]
            _assert_normalized(variable_weights, station.station_id, variable)
            weights.extend(variable_weights)
    return tuple(weights)


def wind_speed(u_value: float, v_value: float) -> float:
    return math.sqrt(u_value**2 + v_value**2)


def format_tsd_forc(
    rows: Sequence[ForcingTimeseriesRow],
    *,
    stations: Sequence[MetStation],
    variables: Sequence[str] = FORCING_VARIABLES,
) -> str:
    station_ids = [station.station_id for station in stations]
    values = {(row.valid_time, row.variable, row.station_id): row.value for row in rows}
    valid_times = sorted({row.valid_time for row in rows})
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["valid_time", "variable", *station_ids])
    for valid_time in valid_times:
        for variable in variables:
            writer.writerow(
                [
                    _format_time(valid_time),
                    variable,
                    *[_format_number(values[(valid_time, variable, station_id)]) for station_id in station_ids],
                ]
            )
    return output.getvalue()


def format_debug_csv(rows: Sequence[ForcingTimeseriesRow]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["valid_time", "station_id", "variable", "value", "unit"])
    for row in sorted(rows, key=lambda item: (item.valid_time, item.station_id, item.variable)):
        writer.writerow([
            _format_time(row.valid_time),
            row.station_id,
            row.variable,
            _format_number(row.value),
            row.unit,
        ])
    return output.getvalue()


def parse_cycle_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    candidate = value.strip()
    if len(candidate) == 10 and candidate.isdigit():
        return datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return _ensure_utc(datetime.fromisoformat(candidate))


def format_cycle_time(value: str | datetime) -> str:
    return parse_cycle_time(value).strftime("%Y%m%d%H")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _format_number(value: float) -> str:
    return f"{float(value):.10g}"


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_time(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable.")


def _valid_station(station: MetStation) -> bool:
    return (
        math.isfinite(station.longitude)
        and math.isfinite(station.latitude)
        and -180.0 <= station.longitude <= 180.0
        and -90.0 <= station.latitude <= 90.0
        and math.isfinite(station.elevation_m)
    )


def _valid_geographic_coordinate(longitude: float, latitude: float) -> bool:
    return (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180.0 <= longitude <= 180.0
        and -90.0 <= latitude <= 90.0
    )


def _validate_grid_points(grid_points: Sequence[GridPoint], canonical_product_id: str) -> None:
    for point in grid_points:
        if not math.isfinite(point.longitude) or not math.isfinite(point.latitude):
            raise ForcingProductionError(
                f"Canonical product {canonical_product_id} has non-finite grid coordinates "
                f"for grid cell {point.grid_cell_id}."
            )
        if not _valid_geographic_coordinate(point.longitude, point.latitude):
            raise ForcingProductionError(
                f"Canonical product {canonical_product_id} has grid coordinates outside geographic bounds "
                f"for grid cell {point.grid_cell_id}."
            )


def _validate_field_values(
    values: Sequence[float],
    grid_points: Sequence[GridPoint],
    canonical_product_id: str,
) -> None:
    for point, value in zip(grid_points, values, strict=True):
        if not math.isfinite(value):
            raise ForcingProductionError(
                f"Canonical product {canonical_product_id} has non-finite field value "
                f"for grid cell {point.grid_cell_id}."
            )


def _missing_product_details(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    required_variables: Sequence[str],
) -> list[str]:
    all_times = sorted({
        valid_time
        for variable in required_variables
        for valid_time in products_by_variable.get(variable, {})
    })
    missing: list[str] = []
    for variable in required_variables:
        product_times = set(products_by_variable.get(variable, {}))
        if not product_times:
            missing.append(f"{variable}:*")
            continue
        for valid_time in all_times:
            if valid_time not in product_times:
                missing.append(f"{variable}:{_format_time(valid_time)}")
    return missing


def _first_product(products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]]) -> CanonicalProduct:
    for variable in REQUIRED_CANONICAL_VARIABLES:
        products_for_variable = products_by_variable.get(variable)
        if products_for_variable:
            return products_for_variable[sorted(products_for_variable)[0]]
    raise ForcingProductionError("No canonical products are available.")


def _valid_times(products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]]) -> tuple[datetime, ...]:
    first_variable = REQUIRED_CANONICAL_VARIABLES[0]
    return tuple(sorted(products_by_variable[first_variable]))


def _weights_cover(
    weights: Sequence[InterpolationWeight],
    stations: Sequence[MetStation],
    variables: Sequence[str],
) -> bool:
    if not weights:
        return False
    covered = {(weight.station_id, weight.variable) for weight in weights}
    required = {(station.station_id, variable) for station in stations for variable in variables}
    return required.issubset(covered)


def _validate_weight_sums(weights: Sequence[InterpolationWeight]) -> None:
    grouped: dict[tuple[str, str], list[InterpolationWeight]] = {}
    for weight in weights:
        grouped.setdefault((weight.station_id, weight.variable), []).append(weight)
    for (station_id, variable), group in grouped.items():
        _assert_normalized(group, station_id, variable)


def _assert_normalized(weights: Sequence[InterpolationWeight], station_id: str, variable: str) -> None:
    if any(not math.isfinite(weight.weight) for weight in weights):
        raise ForcingProductionError(
            f"IDW weights for station {station_id} variable {variable} include non-finite values."
        )
    total = sum(weight.weight for weight in weights)
    if abs(total - 1.0) > 1e-6:
        raise ForcingProductionError(
            f"IDW weights for station {station_id} variable {variable} sum to {total}, expected 1.0."
        )
    if any(weight.weight < 0.0 for weight in weights):
        raise ForcingProductionError(f"IDW weights for station {station_id} variable {variable} include negatives.")


def _weights_by_station_variable(
    weights: Sequence[InterpolationWeight],
) -> dict[tuple[str, str], tuple[InterpolationWeight, ...]]:
    grouped: dict[tuple[str, str], list[InterpolationWeight]] = {}
    for weight in weights:
        grouped.setdefault((weight.station_id, weight.variable), []).append(weight)
    return {
        key: tuple(sorted(group, key=lambda item: item.grid_cell_id))
        for key, group in grouped.items()
    }


def _native_resolution_for_output(
    variable: str,
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    valid_time: datetime,
) -> str | None:
    if variable == "wind":
        return products_by_variable["wind_u_10m"][valid_time].native_time_resolution
    canonical_variable = {
        "PRCP": "prcp_rate_or_amount",
        "TEMP": "air_temperature_2m",
        "RH": "relative_humidity_2m",
        "Rn": "shortwave_down",
        "Press": "pressure_surface",
    }[variable]
    return products_by_variable[canonical_variable][valid_time].native_time_resolution


def _distance_degrees(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    longitude_scale = math.cos(math.radians((lat_a + lat_b) / 2.0))
    return math.hypot((lon_a - lon_b) * longitude_scale, lat_a - lat_b)


def _select_data_variable(dataset: Any, expected_variable: str) -> str:
    if expected_variable in dataset.data_vars:
        return expected_variable
    data_variables = list(dataset.data_vars)
    if len(data_variables) == 1:
        return str(data_variables[0])
    raise ForcingProductionError(
        f"NetCDF product for {expected_variable} has no matching variable; found {data_variables}."
    )


def _grid_cell_ids(dataset: Any, expected_count: int) -> tuple[str, ...]:
    for name in ("grid_cell_id", "cell", "point"):
        if name in dataset.coords:
            values = dataset[name].values.ravel().tolist()
            if len(values) == expected_count:
                return tuple(str(value) for value in values)
    return tuple(str(index) for index in range(expected_count))


def _direct_lon_lat_coords(dataset: Any, expected_count: int) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    longitudes = _flat_coord(dataset, ("lon", "longitude"), expected_count)
    latitudes = _flat_coord(dataset, ("lat", "latitude"), expected_count)
    if longitudes is None or latitudes is None:
        return None
    return longitudes, latitudes


def _flat_coord(dataset: Any, names: Sequence[str], expected_count: int) -> tuple[float, ...] | None:
    for name in names:
        if name in dataset.coords:
            values = dataset[name].values.ravel().tolist()
            if len(values) == expected_count:
                return tuple(float(value) for value in values)
    return None


def _rectilinear_lon_lat_coords(dataset: Any, shape: tuple[int, ...]) -> tuple[tuple[float, float], ...] | None:
    if len(shape) != 2:
        return None
    y_count, x_count = shape
    longitudes = _coord_by_length(dataset, ("lon", "longitude"), x_count)
    latitudes = _coord_by_length(dataset, ("lat", "latitude"), y_count)
    if longitudes is None or latitudes is None:
        return None
    return tuple((longitude, latitude) for latitude in latitudes for longitude in longitudes)


def _coord_by_length(dataset: Any, names: Sequence[str], expected_count: int) -> tuple[float, ...] | None:
    for name in names:
        if name in dataset.coords:
            values = dataset[name].values.ravel().tolist()
            if len(values) == expected_count:
                return tuple(float(value) for value in values)
    return None


def _forcing_version_id(source_id: str, cycle_time: datetime, model_id: str) -> str:
    return f"forc_{_object_source_segment(source_id)}_{format_cycle_time(cycle_time)}_{model_id}"


def _object_source_segment(source_id: str) -> str:
    return source_id.lower()


def _directory_uri(object_store: LocalObjectStore, key_prefix: str) -> str:
    prefix = object_store.object_store_prefix.rstrip("/")
    if not prefix:
        return key_prefix.rstrip("/") + "/"
    return f"{prefix}/{key_prefix.strip('/')}/"


def _package_manifest_uri(package_uri: str, manifest_filename: str) -> str:
    return f"{package_uri.rstrip('/')}/{manifest_filename}"


_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_path_component(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid path component.")
    if value.startswith("-") or "/" in value or "\\" in value or ".." in value or "\x00" in value:
        raise ValueError("Invalid path component.")
    if _SAFE_PATH_COMPONENT.fullmatch(value) is None:
        raise ValueError("Invalid path component.")
    return value
