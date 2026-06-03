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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AbstractSet, Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.source_identity import normalize_source_id
from workers.canonical_converter.converter import canonical_product_is_forcing_usable

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
ERA5_REQUIRED_CANONICAL_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "pressure_surface",
    "net_radiation",
)
IFS_REQUIRED_CANONICAL_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "surface_pressure",
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
EXPECTED_CANONICAL_UNITS: dict[str, tuple[str, ...]] = {
    "prcp_rate_or_amount": ("mm", "mm/day"),
    "air_temperature_2m": ("degC",),
    "relative_humidity_2m": ("0-1",),
    "wind_u_10m": ("m/s",),
    "wind_v_10m": ("m/s",),
    "pressure_surface": ("Pa",),
    "surface_pressure": ("Pa",),
    "shortwave_down": ("W/m2",),
    "net_radiation": ("W/m2",),
}
CANONICAL_TO_FORCING: dict[str, str] = {
    "prcp_rate_or_amount": "PRCP",
    "air_temperature_2m": "TEMP",
    "relative_humidity_2m": "RH",
    "shortwave_down": "Rn",
    "pressure_surface": "Press",
}
ERA5_CANONICAL_TO_FORCING: dict[str, str] = {
    "prcp_rate_or_amount": "PRCP",
    "air_temperature_2m": "TEMP",
    "relative_humidity_2m": "RH",
    "net_radiation": "Rn",
    "pressure_surface": "Press",
}
IFS_CANONICAL_TO_FORCING: dict[str, str] = {
    "prcp_rate_or_amount": "PRCP",
    "air_temperature_2m": "TEMP",
    "relative_humidity_2m": "RH",
    "shortwave_down": "Rn",
    "surface_pressure": "Press",
}
ERA5_FALLBACK_SOURCE_ID = "gfs"
ERA5_LATENCY_FALLBACK_REASON = "era5_latency"


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
    properties_json: Mapping[str, Any] = field(default_factory=dict)


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
    grid_signature: str | None = None


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
    lead_time_hours: int | None = None
    lineage_json: Mapping[str, Any] = field(default_factory=dict)


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
    variable_count: int = 0
    time_range: Mapping[str, str | int] = field(default_factory=dict)
    units: Mapping[str, str] = field(default_factory=dict)
    file_uris: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FallbackLineage:
    fallback_reason: str
    fallback_source_id: str
    fallback_valid_times: tuple[datetime, ...]


class ForcingRepository(Protocol):
    def resolve_model_identity(self, *, model_id: str) -> Mapping[str, Any]: ...

    def resolve_model_basin_version(self, *, model_id: str) -> str: ...

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]: ...

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]: ...

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]: ...

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]: ...

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None: ...


    def get_forcing_version(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None: ...

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]: ...

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]: ...

    def verify_forcing_version_children(
        self,
        *,
        forcing_version_id: str,
        expected_component_ids: Sequence[str],
        expected_station_ids: Sequence[str],
        expected_valid_times: Sequence[datetime],
        expected_variables: Sequence[str],
    ) -> Mapping[str, Any]: ...

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None: ...

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: Sequence[ForcingTimeseriesRow],
    ) -> None: ...

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class ForcingProducerConfig:
    source_id: str = "gfs"
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT", ""))
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
    era5_latency_fallback_hours: int = 23
    ifs_precip_step_hours: float = 3.0
    max_station_count: int = field(default_factory=lambda: _env_int("FORCING_MAX_STATION_COUNT", 10_000))
    max_timestep_count: int = field(default_factory=lambda: _env_int("FORCING_MAX_TIMESTEP_COUNT", 10_000))
    max_grid_cell_count: int = field(default_factory=lambda: _env_int("FORCING_MAX_GRID_CELL_COUNT", 5_000_000))
    max_timeseries_row_count: int = field(
        default_factory=lambda: _env_int("FORCING_MAX_TIMESERIES_ROW_COUNT", 10_000_000)
    )
    max_manifest_bytes: int = field(default_factory=lambda: _env_int("FORCING_MAX_MANIFEST_BYTES", 2_000_000))

    def __post_init__(self) -> None:
        if not str(self.object_store_root):
            object.__setattr__(self, "object_store_root", self.workspace_root)


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
            self.config.object_store_root,
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
        max_lead_hours: int | None = None,
        basin_id: str | None = None,
        basin_version_id: str | None = None,
        river_network_version_id: str | None = None,
        canonical_product_id: str | None = None,
        canonical_identity: Mapping[str, Any] | None = None,
    ) -> ForcingProductionResult:
        if self.repository is None:
            raise ForcingProductionError("A forcing repository is required for production.")

        resolved_source_id = normalize_source_id(source_id or self.config.source_id)
        parsed_cycle_time = parse_cycle_time(cycle_time)
        _safe_path_component(model_id)
        _safe_path_component(_object_source_segment(resolved_source_id))

        try:
            existing = self.repository.get_forcing_version(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                model_id=model_id,
            )

            model_identity = self._resolve_model_identity(model_id=model_id)
            self._validate_scheduler_identity(
                model_identity=model_identity,
                basin_id=basin_id,
                basin_version_id=basin_version_id,
                river_network_version_id=river_network_version_id,
            )
            resolved_basin_id = str(model_identity.get("basin_id") or basin_id or "")
            resolved_basin_version_id = str(model_identity["basin_version_id"])
            resolved_river_network_version_id = str(model_identity.get("river_network_version_id") or "")
            _safe_path_component(resolved_basin_version_id)
            stations = self._load_valid_stations(basin_version_id=resolved_basin_version_id)
            self._enforce_limit("station_count", len(stations), self.config.max_station_count)
            required_variables = self._required_canonical_variables(resolved_source_id)
            canonical_to_forcing = self._canonical_to_forcing(resolved_source_id)
            products_by_variable = self._load_canonical_products(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                required_variables=required_variables,
                require_complete=(max_lead_hours is None and not _uses_era5_latency_fallback(resolved_source_id)),
            )
            products_by_variable = _limit_products_by_max_lead_hours(
                products_by_variable,
                cycle_time=parsed_cycle_time,
                max_lead_hours=max_lead_hours,
            )
            products_by_variable = _limit_products_by_min_lead_hours(
                products_by_variable,
                cycle_time=parsed_cycle_time,
                min_lead_hours=_min_lead_hours_from_env(),
            )
            fallback_lineage = self._apply_era5_latency_fallback(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                products_by_variable=products_by_variable,
                required_variables=required_variables,
            )
            self._validate_canonical_products(
                products_by_variable=products_by_variable,
                required_variables=required_variables,
            )
            self._validate_scheduler_canonical_identity(
                source_id=resolved_source_id,
                products_by_variable=products_by_variable,
                canonical_product_id=canonical_product_id,
                canonical_identity=canonical_identity,
            )
            lead_window = _lead_window_from_products(products_by_variable, parsed_cycle_time)
            valid_times = _valid_times(products_by_variable)
            self._enforce_limit("timestep_count", len(valid_times), self.config.max_timestep_count)
            station_signature = _station_signature(stations)
            scheduler_canonical_identity = _scheduler_canonical_identity_manifest(
                canonical_product_id=canonical_product_id,
                canonical_identity=canonical_identity,
            )
            canonical_input_signature = self._canonical_input_signature(products_by_variable, parsed_cycle_time)
            if self._existing_forcing_version_is_current(
                existing,
                lead_window=lead_window,
                station_signature=station_signature,
                canonical_input_signature=canonical_input_signature,
                scheduler_canonical_identity=scheduler_canonical_identity,
                expected_station_ids=station_signature["station_ids"],
                expected_valid_times=valid_times,
                expected_variables=self.config.output_variables,
                expected_component_ids=_canonical_product_ids(products_by_variable),
            ):
                return ForcingProductionResult(
                    status="already_done",
                    forcing_version_id=str(existing["forcing_version_id"]),
                    forcing_package_uri=str(existing["forcing_package_uri"]),
                    checksum=str(existing["checksum"]),
                    station_count=int(existing["station_count"]),
                    timestep_count=len(valid_times),
                    variable_count=len(self.config.output_variables),
                    time_range=_time_range_manifest(valid_times),
                    units={variable: OUTPUT_UNITS[variable] for variable in self.config.output_variables},
                    file_uris={
                        "package_manifest": _package_manifest_uri(
                            str(existing["forcing_package_uri"]),
                            self.config.package_manifest_filename,
                        ),
                    },
                )

            grid_points_by_source_grid = self._grid_points_by_source_grid_from_products(products_by_variable)
            self._enforce_limit(
                "grid_cell_count",
                sum(len(grid_points) for grid_points in grid_points_by_source_grid.values()),
                self.config.max_grid_cell_count,
            )
            grid_ids = tuple(sorted({grid_id for _, grid_id in grid_points_by_source_grid}))
            grid_id = grid_ids[0] if len(grid_ids) == 1 else "mixed"
            grid_signature_by_source_grid = {
                source_grid: _grid_signature_hash(grid_points)
                for source_grid, grid_points in grid_points_by_source_grid.items()
            }
            weights = self._load_or_create_weights(
                model_id=model_id,
                stations=stations,
                grid_points_by_source_grid=grid_points_by_source_grid,
                grid_signature_by_source_grid=grid_signature_by_source_grid,
            )
            forcing_version_id = (
                str(existing["forcing_version_id"])
                if existing is not None
                else _forcing_version_id(resolved_source_id, parsed_cycle_time, model_id)
            )
            values, components = self._generate_timeseries_streaming(
                source_id=resolved_source_id,
                forcing_version_id=forcing_version_id,
                basin_version_id=resolved_basin_version_id,
                products_by_variable=products_by_variable,
                stations=stations,
                weights=weights,
                grid_points_by_source_grid=grid_points_by_source_grid,
                canonical_to_forcing=canonical_to_forcing,
            )
            del grid_points_by_source_grid

            result = self._write_outputs_and_records(
                source_id=resolved_source_id,
                cycle_time=parsed_cycle_time,
                model_id=model_id,
                basin_id=resolved_basin_id,
                basin_version_id=resolved_basin_version_id,
                river_network_version_id=resolved_river_network_version_id,
                scheduler_canonical_identity=scheduler_canonical_identity,
                grid_id=grid_id,
                stations=stations,
                rows=values,
                components=components,
                products_by_variable=products_by_variable,
                fallback_lineage=fallback_lineage,
                lead_window=lead_window,
                station_signature=station_signature,
                grid_signature_by_source_grid=grid_signature_by_source_grid,
                canonical_input_signature=canonical_input_signature,
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

    def _resolve_model_identity(self, *, model_id: str) -> Mapping[str, Any]:
        assert self.repository is not None
        resolver = getattr(self.repository, "resolve_model_identity", None)
        if callable(resolver):
            identity = dict(resolver(model_id=model_id))
        else:
            identity = {"basin_version_id": self.repository.resolve_model_basin_version(model_id=model_id)}
        if identity.get("basin_version_id") in (None, ""):
            raise ForcingProductionError(f"Model instance {model_id!r} has no basin_version_id.")
        return identity

    def _validate_scheduler_identity(
        self,
        *,
        model_identity: Mapping[str, Any],
        basin_id: str | None,
        basin_version_id: str | None,
        river_network_version_id: str | None,
    ) -> None:
        expected = {
            "basin_id": basin_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        }
        for field_name, expected_value in expected.items():
            if expected_value in (None, ""):
                continue
            actual_value = model_identity.get(field_name)
            if actual_value in (None, ""):
                if field_name == "basin_id":
                    continue
                raise ForcingProductionError(f"Model identity is missing {field_name}.")
            if str(actual_value) != str(expected_value):
                raise ForcingProductionError(
                    f"Scheduler {field_name} {expected_value!r} does not match repository value {actual_value!r}."
                )

    def _validate_scheduler_canonical_identity(
        self,
        *,
        source_id: str,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        canonical_product_id: str | None,
        canonical_identity: Mapping[str, Any] | None,
    ) -> None:
        identity = _scheduler_canonical_identity_manifest(
            canonical_product_id=canonical_product_id,
            canonical_identity=canonical_identity,
        )
        expected_policy = _stable_identity(identity.get("policy_identity"))
        expected_source_object = _stable_identity(identity.get("source_object_identity"))
        if not expected_policy and not expected_source_object and not identity.get("canonical_product_id"):
            return
        mismatches: list[str] = []
        for product in _products(products_by_variable):
            if product.source_id != source_id:
                continue
            lineage = product.lineage_json if isinstance(product.lineage_json, Mapping) else {}
            row_policy = _stable_identity(
                lineage.get("policy_identity")
                or lineage.get("source_policy")
                or lineage.get("canonical_policy_identity")
            )
            row_source_object = _stable_identity(
                lineage.get("source_object_identity")
                or lineage.get("source_identity")
                or lineage.get("object_identity")
            )
            row_canonical_product_id = lineage.get("canonical_product_id")
            if expected_policy and row_policy != expected_policy:
                mismatches.append(f"{product.canonical_product_id}:policy_identity")
            if expected_source_object and row_source_object != expected_source_object:
                mismatches.append(f"{product.canonical_product_id}:source_object_identity")
            if (
                identity.get("canonical_product_id")
                and row_canonical_product_id not in (None, "")
                and str(row_canonical_product_id) != str(identity["canonical_product_id"])
            ):
                mismatches.append(f"{product.canonical_product_id}:canonical_product_id")
        if mismatches:
            raise ForcingProductionError(
                "Canonical products do not match scheduler-selected canonical identity: "
                + ", ".join(mismatches[:10])
            )

    def _enforce_limit(self, name: str, value: int, limit: int) -> None:
        if value > limit:
            raise ForcingProductionError(f"Forcing {name} {value} exceeds configured limit {limit}.")

    def _load_valid_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        assert self.repository is not None
        loaded = self.repository.load_met_stations(basin_version_id=basin_version_id)
        selected = tuple(station for station in loaded if station.station_role == "forcing_grid")
        stations: list[MetStation] = []
        for station in selected:
            if not _valid_station(station):
                LOGGER.warning("Excluding invalid met station %s for basin %s", station.station_id, basin_version_id)
                continue
            _validate_forcing_grid_station_contract(station)
            stations.append(station)

        if not stations:
            raise ForcingProductionError(
                f"No active forcing_grid meteorological stations are defined for basin version {basin_version_id}."
            )
        _validate_unique_station_forcing_contract(stations)
        return tuple(sorted(stations, key=_station_forcing_sort_key))

    def _required_canonical_variables(self, source_id: str) -> tuple[str, ...]:
        if _is_ifs_source(source_id):
            return IFS_REQUIRED_CANONICAL_VARIABLES
        if _is_era5_source(source_id):
            return ERA5_REQUIRED_CANONICAL_VARIABLES
        return self.config.required_canonical_variables

    def _canonical_to_forcing(self, source_id: str) -> Mapping[str, str]:
        if _is_ifs_source(source_id):
            return IFS_CANONICAL_TO_FORCING
        if _is_era5_source(source_id):
            return ERA5_CANONICAL_TO_FORCING
        return CANONICAL_TO_FORCING

    def _load_canonical_products(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        required_variables: Sequence[str],
        require_complete: bool = True,
    ) -> dict[str, dict[datetime, CanonicalProduct]]:
        assert self.repository is not None
        products = self.repository.list_canonical_products(source_id=source_id, cycle_time=cycle_time)
        products_by_variable: dict[str, dict[datetime, CanonicalProduct]] = {
            variable: {} for variable in required_variables
        }
        for product in products:
            if product.variable not in products_by_variable:
                continue
            if not canonical_product_is_forcing_usable(
                {"quality_flag": product.quality_flag, "checksum": product.checksum}
            ):
                continue
            products_by_variable[product.variable][product.valid_time] = product

        if not require_complete:
            return products_by_variable

        self._validate_canonical_products(
            products_by_variable=products_by_variable,
            required_variables=required_variables,
        )
        return products_by_variable

    def _validate_canonical_products(
        self,
        *,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        required_variables: Sequence[str],
    ) -> None:
        missing = _missing_product_details(products_by_variable, required_variables)
        if missing:
            raise ForcingProductionError(f"Missing required canonical products: {', '.join(missing)}")

        grid_ids = {
            product.grid_id
            for products_for_variable in products_by_variable.values()
            for product in products_for_variable.values()
        }
        if not grid_ids:
            raise ForcingProductionError("No canonical products are available.")
        _validate_canonical_product_units(products_by_variable)

    def _apply_era5_latency_fallback(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        products_by_variable: dict[str, dict[datetime, CanonicalProduct]],
        required_variables: Sequence[str],
    ) -> FallbackLineage | None:
        if not _uses_era5_latency_fallback(source_id):
            return None

        assert self.repository is not None
        existing_valid_times = sorted(
            {
                valid_time
                for products_for_variable in products_by_variable.values()
                for valid_time in products_for_variable
            }
        )
        if existing_valid_times:
            target_valid_times = tuple(existing_valid_times)
            start_time = existing_valid_times[0]
            end_time = existing_valid_times[-1]
        else:
            target_valid_times = ()
            start_time = cycle_time
            end_time = cycle_time + timedelta(hours=self.config.era5_latency_fallback_hours)

        fallback_variables = _fallback_variables_for_required(required_variables)
        fallback_products = self.repository.list_fallback_canonical_products(
            source_id=ERA5_FALLBACK_SOURCE_ID,
            start_time=start_time,
            end_time=end_time,
            variables=fallback_variables,
        )
        fallback_by_variable = _fallback_products_by_required_variable(
            fallback_products,
            required_variables=required_variables,
        )
        if not target_valid_times:
            target_valid_times = tuple(
                sorted(
                    {
                        valid_time
                        for products_for_variable in fallback_by_variable.values()
                        for valid_time in products_for_variable
                    }
                )
            )

        fallback_valid_times: set[datetime] = set()
        for valid_time in target_valid_times:
            missing_variables = [
                variable for variable in required_variables if valid_time not in products_by_variable[variable]
            ]
            for variable in missing_variables:
                fallback_product = fallback_by_variable.get(variable, {}).get(valid_time)
                if fallback_product is None:
                    continue
                products_by_variable[variable][valid_time] = fallback_product
                fallback_valid_times.add(valid_time)

        if not fallback_valid_times:
            return None
        return FallbackLineage(
            fallback_reason=ERA5_LATENCY_FALLBACK_REASON,
            fallback_source_id=ERA5_FALLBACK_SOURCE_ID,
            fallback_valid_times=tuple(sorted(fallback_valid_times)),
        )

    def _load_or_create_weights(
        self,
        *,
        model_id: str,
        stations: Sequence[MetStation],
        grid_points_by_source_grid: Mapping[tuple[str, str], Sequence[GridPoint]],
        grid_signature_by_source_grid: Mapping[tuple[str, str], str],
    ) -> dict[tuple[str, str], tuple[InterpolationWeight, ...]]:
        assert self.repository is not None
        weights_by_source_grid: dict[tuple[str, str], tuple[InterpolationWeight, ...]] = {}
        for (source_id, grid_id), grid_points in sorted(grid_points_by_source_grid.items()):
            grid_signature = grid_signature_by_source_grid[(source_id, grid_id)]
            existing = self.repository.load_interp_weights(source_id=source_id, grid_id=grid_id, model_id=model_id)
            if _weights_cover(existing, stations, self.config.output_variables) and _weights_match_grid_signature(
                existing,
                grid_signature,
            ):
                _validate_weight_sums(existing)
                weights_by_source_grid[(source_id, grid_id)] = tuple(existing)
                continue

            computed = compute_idw_weights(
                stations=stations,
                grid_points=grid_points,
                variables=self.config.output_variables,
                source_id=source_id,
                grid_id=grid_id,
                model_id=model_id,
                neighbors=self.config.idw_neighbors,
                power=self.config.idw_power,
                grid_signature=grid_signature,
            )
            self.repository.upsert_interp_weights(computed)
            weights_by_source_grid[(source_id, grid_id)] = computed
        return weights_by_source_grid

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

    def _grid_points_by_source_grid_from_products(
        self,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    ) -> dict[tuple[str, str], tuple[GridPoint, ...]]:
        representatives: dict[tuple[str, str], CanonicalProduct] = {}
        for products_for_variable in products_by_variable.values():
            for product in products_for_variable.values():
                representatives.setdefault((product.source_id, product.grid_id), product)
        if not representatives:
            raise ForcingProductionError("No canonical products are available for interpolation grid discovery.")
        return {
            source_grid: self._read_canonical_grid(product)
            for source_grid, product in sorted(representatives.items(), key=lambda item: item[0])
        }

    def _validate_field_grid_matches_product(
        self,
        product: CanonicalProduct,
        grid_points: Sequence[GridPoint],
    ) -> None:
        representative = self._read_canonical_grid(product)
        if _grid_signature(representative) != _grid_signature(grid_points):
            raise ForcingProductionError(
                f"Canonical product {product.canonical_product_id} grid definition/order does not match "
                f"the interpolation grid for source {product.source_id} grid {product.grid_id}."
            )

    def _read_canonical_grid(self, product: CanonicalProduct) -> tuple[GridPoint, ...]:
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
            expected_count = _data_array_size(data_array)
            grid_points = self._grid_points_for_dataset(
                product,
                dataset,
                data_array,
                _data_array_shape(data_array),
                expected_count,
            )
            if len(grid_points) != expected_count:
                raise ForcingProductionError(
                    f"Canonical product {product.canonical_product_id} has {expected_count} values but "
                    f"{len(grid_points)} grid points."
                )
            _validate_grid_points(grid_points, product.canonical_product_id)
            return grid_points
        except (OSError, ObjectStoreError, TypeError, ValueError) as error:
            raise ForcingProductionError(
                f"Failed to read canonical product grid {product.canonical_product_id}: {error}"
            ) from error
        finally:
            if dataset is not None:
                dataset.close()

    def _read_canonical_field(
        self,
        product: CanonicalProduct,
        *,
        required_grid_cell_ids: AbstractSet[str] | None = None,
        retain_grid_points: bool = True,
    ) -> CanonicalField:
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
            flat_values = raw_values.ravel()
            grid_points = self._grid_points_for_dataset(
                product,
                dataset,
                data_array,
                _data_array_shape(data_array),
                len(flat_values),
            )
            if len(grid_points) != len(flat_values):
                raise ForcingProductionError(
                    f"Canonical product {product.canonical_product_id} has {len(flat_values)} values but "
                    f"{len(grid_points)} grid points."
                )
            _validate_grid_points(grid_points, product.canonical_product_id)
            values_by_grid_cell_id: dict[str, float] = {}
            for point, raw_value in zip(grid_points, flat_values, strict=True):
                value = float(raw_value)
                if not math.isfinite(value):
                    raise ForcingProductionError(
                        f"Canonical product {product.canonical_product_id} has non-finite field value "
                        f"for grid cell {point.grid_cell_id}."
                    )
                if required_grid_cell_ids is None or point.grid_cell_id in required_grid_cell_ids:
                    values_by_grid_cell_id[point.grid_cell_id] = value
            if required_grid_cell_ids is not None:
                missing = sorted(required_grid_cell_ids.difference(values_by_grid_cell_id))
                if missing:
                    sample = ", ".join(missing[:5])
                    raise ForcingProductionError(
                        f"Canonical product {product.canonical_product_id} is missing required interpolation "
                        f"grid cells: {sample}."
                    )
            return CanonicalField(
                product=product,
                grid_points=grid_points if retain_grid_points else (),
                values_by_grid_cell_id=values_by_grid_cell_id,
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
        shape: tuple[int, ...],
        expected_count: int,
    ) -> tuple[GridPoint, ...]:
        grid_from_definition = self._grid_points_from_definition(product, expected_count)
        if grid_from_definition:
            return grid_from_definition

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
        if definition.get("layout") == "rectilinear":
            try:
                longitudes = tuple(float(value) for value in definition["longitudes"])
                latitudes = tuple(float(value) for value in definition["latitudes"])
                y_count, x_count = (int(value) for value in definition["shape"])
            except (KeyError, TypeError, ValueError):
                return None
            if len(longitudes) != x_count or len(latitudes) != y_count or x_count * y_count != expected_count:
                return None
            return tuple(
                GridPoint(
                    grid_cell_id=str(index),
                    longitude=_normalize_longitude(longitude),
                    latitude=latitude,
                )
                for index, (latitude, longitude) in enumerate(
                    (lat, lon) for lat in latitudes for lon in longitudes
                )
            )
        return None

    def _generate_timeseries_streaming(
        self,
        *,
        source_id: str,
        forcing_version_id: str,
        basin_version_id: str,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        stations: Sequence[MetStation],
        weights: Mapping[tuple[str, str], Sequence[InterpolationWeight]],
        grid_points_by_source_grid: Mapping[tuple[str, str], Sequence[GridPoint]],
        canonical_to_forcing: Mapping[str, str],
    ) -> tuple[tuple[ForcingTimeseriesRow, ...], tuple[ForcingComponent, ...]]:
        valid_times = _valid_times(products_by_variable)
        weights_by_source_grid_station_variable = {
            source_grid: _weights_by_station_variable(source_grid_weights)
            for source_grid, source_grid_weights in weights.items()
        }
        required_grid_cell_ids_by_source_grid = _required_grid_cell_ids_by_source_grid(weights)
        rows: list[ForcingTimeseriesRow] = []
        radiation_variable = _canonical_variable_for_forcing("Rn", canonical_to_forcing)
        pressure_variable = _canonical_variable_for_forcing("Press", canonical_to_forcing)

        for valid_time in valid_times:
            field_cache: dict[str, CanonicalField] = {}

            def field_for(variable: str) -> CanonicalField:
                cached = field_cache.get(variable)
                if cached is not None:
                    return cached
                product = products_by_variable[variable][valid_time]
                source_grid = (product.source_id, product.grid_id)
                field = self._read_canonical_field(
                    product,
                    required_grid_cell_ids=required_grid_cell_ids_by_source_grid.get(source_grid),
                    retain_grid_points=False,
                )
                self._validate_field_grid_matches_product(
                    product,
                    tuple(grid_points_by_source_grid[source_grid]),
                )
                field_cache[variable] = field
                return field

            precip_product = products_by_variable["prcp_rate_or_amount"][valid_time]
            precip_factor = (
                24.0 / _precip_step_hours(precip_product, self.config.ifs_precip_step_hours)
                if _is_ifs_source(source_id)
                else 1.0
            )
            station_values: dict[str, dict[str, float]] = {
                "PRCP": {
                    station_id: value * precip_factor
                    for station_id, value in self._interpolate_forcing_variable(
                        "PRCP",
                        field_for("prcp_rate_or_amount"),
                        stations,
                        weights_by_source_grid_station_variable,
                    ).items()
                },
                "TEMP": self._interpolate_forcing_variable(
                    "TEMP",
                    field_for("air_temperature_2m"),
                    stations,
                    weights_by_source_grid_station_variable,
                ),
                "RH": self._interpolate_forcing_variable(
                    "RH",
                    field_for("relative_humidity_2m"),
                    stations,
                    weights_by_source_grid_station_variable,
                ),
                "Rn": {
                    station_id: value
                    * (1.0 if radiation_variable == "net_radiation" else self.config.rn_shortwave_factor)
                    for station_id, value in self._interpolate_forcing_variable(
                        "Rn",
                        field_for(radiation_variable),
                        stations,
                        weights_by_source_grid_station_variable,
                    ).items()
                },
                "Press": self._interpolate_forcing_variable(
                    "Press",
                    field_for(pressure_variable),
                    stations,
                    weights_by_source_grid_station_variable,
                ),
            }
            u_values = self._interpolate_forcing_variable(
                "wind",
                field_for("wind_u_10m"),
                stations,
                weights_by_source_grid_station_variable,
            )
            v_values = self._interpolate_forcing_variable(
                "wind",
                field_for("wind_v_10m"),
                stations,
                weights_by_source_grid_station_variable,
            )
            station_values["wind"] = {
                station.station_id: wind_speed(u_values[station.station_id], v_values[station.station_id])
                for station in stations
            }

            for variable in self.config.output_variables:
                native_resolution = _native_resolution_for_output(
                    variable,
                    products_by_variable,
                    valid_time,
                    canonical_to_forcing,
                )
                row_source_id = _source_id_for_output_variable(
                    variable,
                    products_by_variable,
                    valid_time,
                    canonical_to_forcing,
                )
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
                            source_id=row_source_id,
                            variable=variable,
                            value=value,
                            unit=OUTPUT_UNITS[variable],
                            native_resolution=native_resolution,
                        )
                    )
            field_cache.clear()

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
        weights_by_source_grid_station_variable: Mapping[
            tuple[str, str],
            Mapping[tuple[str, str], tuple[InterpolationWeight, ...]],
        ],
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        source_grid = (field.product.source_id, field.product.grid_id)
        try:
            weights_by_station_variable = weights_by_source_grid_station_variable[source_grid]
        except KeyError as error:
            raise ForcingProductionError(
                f"No interpolation weights are available for source {field.product.source_id} "
                f"grid {field.product.grid_id}."
            ) from error
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
        basin_id: str,
        basin_version_id: str,
        river_network_version_id: str,
        scheduler_canonical_identity: Mapping[str, Any],
        grid_id: str,
        stations: Sequence[MetStation],
        rows: Sequence[ForcingTimeseriesRow],
        components: Sequence[ForcingComponent],
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        fallback_lineage: FallbackLineage | None = None,
        lead_window: Mapping[str, int | None] | None = None,
        station_signature: Mapping[str, Any] | None = None,
        grid_signature_by_source_grid: Mapping[tuple[str, str], str] | None = None,
        canonical_input_signature: Mapping[str, Any] | None = None,
    ) -> ForcingProductionResult:
        assert self.repository is not None
        compact_cycle = format_cycle_time(cycle_time)
        source_segment = _object_source_segment(source_id)
        forcing_version_id = rows[0].forcing_version_id
        valid_times = sorted({row.valid_time for row in rows})
        self._enforce_limit("timeseries_row_count", len(rows), self.config.max_timeseries_row_count)
        _validate_package_filenames(
            forcing_filename=self.config.forcing_filename,
            csv_filename=self.config.csv_filename,
            package_manifest_filename=self.config.package_manifest_filename,
            stations=stations,
        )
        prefix = f"forcing/{source_segment}/{compact_cycle}/{basin_version_id}/{model_id}"
        package_uri = _directory_uri(self.object_store, prefix)
        package_manifest_key = f"{prefix}/{self.config.package_manifest_filename}"
        package_manifest_uri = self.object_store.uri_for_key(package_manifest_key)

        tsd_content = format_tsd_forc(rows, stations=stations, variables=self.config.output_variables).encode("utf-8")
        csv_content = format_debug_csv(rows).encode("utf-8")
        tsd_key = f"{prefix}/{self.config.forcing_filename}"
        csv_key = f"{prefix}/{self.config.csv_filename}"
        tsd_uri = self.object_store.uri_for_key(tsd_key)
        csv_uri = self.object_store.uri_for_key(csv_key)
        tsd_checksum = sha256_bytes(tsd_content)
        csv_checksum = sha256_bytes(csv_content)
        shud_files = format_shud_forcing_package(rows, stations=stations)
        shud_file_payloads: list[tuple[str, bytes]] = []
        shud_file_entries: list[dict[str, str]] = []
        for relative_path, content in shud_files.items():
            content_bytes = content.encode("utf-8")
            key = f"{prefix}/{relative_path}"
            uri = self.object_store.uri_for_key(key)
            shud_file_payloads.append((key, content_bytes))
            shud_file_entries.append(
                {
                    "role": "shud_forcing" if relative_path == "shud/qhh.tsd.forc" else "shud_forcing_csv",
                    "relative_path": relative_path,
                    "uri": uri,
                    "checksum": sha256_bytes(content_bytes),
                }
            )

        variable_set = list(self.config.output_variables)
        units = {variable: OUTPUT_UNITS[variable] for variable in variable_set}
        time_range = _time_range_manifest(valid_times)
        station_order = _station_order_manifest(stations)
        quality_flags = _quality_flags_manifest(rows, products_by_variable)
        canonical_product_ids = _canonical_product_ids(products_by_variable)
        lineage_json = {
            "producer_version": self.config.producer_version,
            "source_id": source_id,
            "cycle_time": _format_time(cycle_time),
            "min_lead_hours": (lead_window or {}).get("min_lead_hours"),
            "max_lead_hours": (lead_window or {}).get("max_lead_hours"),
            "model_id": model_id,
            "basin_id": basin_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
            "scheduler_canonical_identity": dict(scheduler_canonical_identity),
            "grid_id": grid_id,
            "station_count": len(stations),
            "station_ids": [station.station_id for station in stations],
            "station_signature": station_signature or _station_signature(stations),
            "grid_signatures": _format_grid_signatures(grid_signature_by_source_grid or {}),
            "canonical_input_signature": canonical_input_signature
            or self._canonical_input_signature(products_by_variable, cycle_time),
            "forcing_variables": variable_set,
            "variable_set": variable_set,
            "units": units,
            "variable_count": len(variable_set),
            "time_range": time_range,
            "quality_flags": quality_flags,
            "station_order": station_order,
            "canonical_product_ids": canonical_product_ids,
        }
        if fallback_lineage is not None:
            lineage_json.update(
                {
                    "fallback_reason": fallback_lineage.fallback_reason,
                    "fallback_source_id": fallback_lineage.fallback_source_id,
                    "fallback_valid_times": [
                        _format_time(valid_time) for valid_time in fallback_lineage.fallback_valid_times
                    ],
                }
            )
        package_manifest = {
            "forcing_version_id": forcing_version_id,
            "model_id": model_id,
            "source_id": source_id,
            "cycle_time": _format_time(cycle_time),
            "start_time": _format_time(valid_times[0]),
            "end_time": _format_time(valid_times[-1]),
            "basin_id": basin_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
            "scheduler_canonical_identity": dict(scheduler_canonical_identity),
            "station_count": len(stations),
            "timestep_count": len(valid_times),
            "variable_count": len(variable_set),
            "time_range": time_range,
            "variable_set": variable_set,
            "units": units,
            "quality_flags": quality_flags,
            "station_order": station_order,
            "files": [
                {"role": "tsd_forc", "uri": tsd_uri, "checksum": tsd_checksum},
                {"role": "csv_debug", "uri": csv_uri, "checksum": csv_checksum},
                *shud_file_entries,
            ],
            "lineage": lineage_json,
        }
        package_content = _json_bytes(package_manifest)
        self._enforce_limit("manifest_bytes", len(package_content), self.config.max_manifest_bytes)
        package_checksum = sha256_bytes(package_content)

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
                "forcing_package_manifest_checksum": package_checksum,
                "output_files": package_manifest["files"],
            },
        }
        self.repository.upsert_forcing_version(record)
        self.object_store.write_bytes_atomic(tsd_key, tsd_content)
        self.object_store.write_bytes_atomic(csv_key, csv_content)
        for key, content_bytes in shud_file_payloads:
            self.object_store.write_bytes_atomic(key, content_bytes)
        self.object_store.write_bytes_atomic(package_manifest_key, package_content)
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
            variable_count=len(variable_set),
            time_range=time_range,
            units=units,
            file_uris={
                "tsd_forc": tsd_uri,
                "csv_debug": csv_uri,
                "package_manifest": package_manifest_uri,
            },
        )

    def _existing_forcing_version_is_current(
        self,
        existing: Mapping[str, Any] | None,
        *,
        lead_window: Mapping[str, int | None],
        station_signature: Mapping[str, Any],
        canonical_input_signature: Mapping[str, Any],
        scheduler_canonical_identity: Mapping[str, Any],
        expected_station_ids: Sequence[str],
        expected_valid_times: Sequence[datetime],
        expected_variables: Sequence[str],
        expected_component_ids: Sequence[str],
    ) -> bool:
        if not existing:
            return False
        checksum = str(existing.get("checksum") or "").strip()
        package_uri = str(existing.get("forcing_package_uri") or "")
        if not checksum or checksum.lower() == "pending" or not package_uri:
            return False
        lineage = existing.get("lineage_json")
        if isinstance(lineage, str):
            try:
                lineage = json.loads(lineage)
            except json.JSONDecodeError:
                lineage = {}
        if not isinstance(lineage, Mapping):
            lineage = {}
        if _optional_int(lineage.get("min_lead_hours")) != lead_window.get("min_lead_hours"):
            return False
        if _optional_int(lineage.get("max_lead_hours")) != lead_window.get("max_lead_hours"):
            return False
        if not _station_signature_matches(lineage.get("station_signature"), station_signature):
            return False
        if list(lineage.get("station_ids") or []) != list(station_signature["station_ids"]):
            return False
        if _optional_int(existing.get("station_count")) != int(station_signature["station_count"]):
            return False
        if not _canonical_input_signature_matches(lineage.get("canonical_input_signature"), canonical_input_signature):
            return False
        if not _scheduler_canonical_identity_matches(
            lineage.get("scheduler_canonical_identity"),
            scheduler_canonical_identity,
        ):
            return False
        try:
            manifest_uri = _package_manifest_uri(package_uri, self.config.package_manifest_filename)
            if not self.object_store.exists(manifest_uri) or self.object_store.checksum(manifest_uri) != checksum:
                return False
            manifest = json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
            if _optional_int(manifest.get("station_count")) != int(station_signature["station_count"]):
                return False
            manifest_lineage = manifest.get("lineage")
            if not isinstance(manifest_lineage, Mapping):
                return False
            if not _station_signature_matches(manifest_lineage.get("station_signature"), station_signature):
                return False
            if not _canonical_input_signature_matches(
                manifest_lineage.get("canonical_input_signature"),
                canonical_input_signature,
            ):
                return False
            if not _scheduler_canonical_identity_matches(
                manifest_lineage.get("scheduler_canonical_identity"),
                scheduler_canonical_identity,
            ):
                return False
            return self._forcing_children_are_complete(
                forcing_version_id=str(existing["forcing_version_id"]),
                expected_component_ids=expected_component_ids,
                expected_station_ids=expected_station_ids,
                expected_valid_times=expected_valid_times,
                expected_variables=expected_variables,
            )
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.warning("Existing forcing package checksum could not be verified for %s", package_uri)
            return False

    def _forcing_children_are_complete(
        self,
        *,
        forcing_version_id: str,
        expected_component_ids: Sequence[str],
        expected_station_ids: Sequence[str],
        expected_valid_times: Sequence[datetime],
        expected_variables: Sequence[str],
    ) -> bool:
        assert self.repository is not None
        verifier = getattr(self.repository, "verify_forcing_version_children", None)
        if not callable(verifier):
            return False
        proof = verifier(
            forcing_version_id=forcing_version_id,
            expected_component_ids=expected_component_ids,
            expected_station_ids=expected_station_ids,
            expected_valid_times=expected_valid_times,
            expected_variables=expected_variables,
        )
        return bool(proof.get("complete"))

    def _canonical_input_signature(
        self,
        products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
        cycle_time: datetime,
    ) -> dict[str, Any]:
        return _canonical_input_signature(
            products_by_variable,
            cycle_time,
            object_store=self.object_store,
        )

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
    grid_signature: str | None = None,
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
    indexed_points = tuple(enumerate(grid_points))
    for station in stations:
        distances = sorted(
            (
                (_distance_degrees(station.longitude, station.latitude, point.longitude, point.latitude), point)
                for _, point in indexed_points
            ),
            key=lambda item: (item[0], item[1].grid_cell_id),
        )[:neighbor_count]
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
                    grid_signature=grid_signature,
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
        writer.writerow(
            [
                _format_time(row.valid_time),
                row.station_id,
                row.variable,
                _format_number(row.value),
                row.unit,
            ]
        )
    return output.getvalue()


def format_shud_forcing_package(
    rows: Sequence[ForcingTimeseriesRow],
    *,
    stations: Sequence[MetStation],
) -> dict[str, str]:
    if not rows or not stations:
        return {}
    station_order = sorted(stations, key=_station_forcing_sort_key)
    rows_by_station_time: dict[tuple[str, datetime], dict[str, float]] = {}
    for row in rows:
        rows_by_station_time.setdefault((row.station_id, row.valid_time), {})[row.variable] = row.value
    valid_times = sorted({row.valid_time for row in rows})
    start_time = _ensure_utc(valid_times[0])
    end_time = _ensure_utc(valid_times[-1])
    start_date = start_time.strftime("%Y%m%d")
    end_date = end_time.strftime("%Y%m%d")
    start_day = start_time.timestamp() / 86_400.0

    files: dict[str, str] = {}
    tsd = io.StringIO()
    tsd.write(f"{len(station_order)} {start_date}\n")
    tsd.write("shud\n")
    tsd.write("ID\tLon\tLat\tX\tY\tZ\tFilename\n")
    for station in station_order:
        forcing_index = _station_forcing_index(station)
        filename = _station_forcing_filename(station, forcing_index)
        props = _station_properties(station)
        tsd.write(
            "\t".join(
                [
                    str(forcing_index),
                    _format_number(station.longitude),
                    _format_number(station.latitude),
                    _format_number(float(props.get("x", 0.0) or 0.0)),
                    _format_number(float(props.get("y", 0.0) or 0.0)),
                    _format_number(float(props.get("z", station.elevation_m) or 0.0)),
                    filename,
                ]
            )
            + "\n"
        )
        csv_buffer = io.StringIO()
        csv_buffer.write(f"{len(valid_times)}\t6\t{start_date}\t{end_date}\n")
        csv_buffer.write("Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n")
        for valid_time in valid_times:
            values = rows_by_station_time[(station.station_id, valid_time)]
            time_day = start_day + (_ensure_utc(valid_time) - start_time).total_seconds() / 86_400.0
            csv_buffer.write(
                "\t".join(
                    [
                        _format_number(time_day),
                        _format_number(values.get("PRCP", 0.0)),
                        _format_number(values.get("TEMP", 0.0)),
                        _format_number(values.get("RH", 0.0)),
                        _format_number(values.get("wind", 0.0)),
                        _format_number(values.get("Rn", 0.0)),
                    ]
                )
                + "\n"
            )
        files[f"shud/{filename}"] = csv_buffer.getvalue()
    files["shud/qhh.tsd.forc"] = tsd.getvalue()
    return files


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


def _station_properties(station: MetStation) -> Mapping[str, Any]:
    properties = getattr(station, "properties_json", None)
    return properties if isinstance(properties, Mapping) else {}


def _validate_forcing_grid_station_contract(station: MetStation) -> None:
    props = _station_properties(station)
    raw_index = props.get("shud_forcing_index")
    if raw_index in (None, ""):
        raise ForcingProductionError(
            f"Fixed forcing_grid station {station.station_id} is missing shud_forcing_index metadata."
        )
    try:
        forcing_index = int(raw_index)
    except (TypeError, ValueError) as error:
        raise ForcingProductionError(
            f"Fixed forcing_grid station {station.station_id} has invalid shud_forcing_index metadata."
        ) from error
    if forcing_index < 1:
        raise ForcingProductionError(
            f"Fixed forcing_grid station {station.station_id} has non-positive shud_forcing_index metadata."
        )
    raw_filename = props.get("forcing_filename")
    filename = str(raw_filename or "").strip()
    if not _safe_station_forcing_filename(filename):
        raise ForcingProductionError(
            f"Fixed forcing_grid station {station.station_id} is missing a safe forcing_filename metadata value."
        )


def _validate_unique_station_forcing_contract(stations: Sequence[MetStation]) -> None:
    indexes: dict[int, str] = {}
    filenames: dict[str, str] = {}
    reserved = _reserved_shud_station_filenames()
    for station in stations:
        forcing_index = _station_forcing_index(station)
        filename = _station_forcing_filename(station, forcing_index)
        if filename in reserved:
            raise ForcingProductionError(
                f"Reserved SHUD forcing filename {filename!r} cannot be used for station {station.station_id}."
            )
        existing_station_id = indexes.setdefault(forcing_index, station.station_id)
        if existing_station_id != station.station_id:
            raise ForcingProductionError(
                f"Duplicate SHUD forcing index {forcing_index} for stations "
                f"{existing_station_id} and {station.station_id}."
            )
        existing_filename_station_id = filenames.setdefault(filename, station.station_id)
        if existing_filename_station_id != station.station_id:
            raise ForcingProductionError(
                f"Duplicate SHUD forcing filename {filename!r} for stations "
                f"{existing_filename_station_id} and {station.station_id}."
            )
    expected_indexes = list(range(1, len(stations) + 1))
    actual_indexes = sorted(indexes)
    if actual_indexes != expected_indexes:
        raise ForcingProductionError(
            "Fixed forcing_grid stations must use contiguous SHUD forcing indexes "
            f"{expected_indexes}; got {actual_indexes}."
        )


def _station_forcing_index(station: MetStation) -> int:
    props = _station_properties(station)
    value = props.get("shud_forcing_index")
    if value is not None:
        return int(value)
    match = re.search(r"(\d+)$", station.station_id)
    return int(match.group(1)) if match else 1


def _station_forcing_filename(station: MetStation, forcing_index: int) -> str:
    props = _station_properties(station)
    filename = str(props.get("forcing_filename") or "").strip()
    if _safe_station_forcing_filename(filename):
        return filename
    return f"forcing_{forcing_index:03d}.csv"


def _safe_station_forcing_filename(filename: str) -> bool:
    return bool(
        filename
        and "/" not in filename
        and "\\" not in filename
        and "\x00" not in filename
        and ".." not in Path(filename).parts
        and filename not in {".", ".."}
        and _SAFE_PATH_COMPONENT.fullmatch(filename) is not None
    )


def _reserved_shud_station_filenames() -> set[str]:
    return {"qhh.tsd.forc", "forcing_package.json", "forcing_debug.csv", "forcing.tsd.forc"}


def _validate_package_filenames(
    *,
    forcing_filename: str,
    csv_filename: str,
    package_manifest_filename: str,
    stations: Sequence[MetStation],
) -> None:
    package_names = {
        "forcing_filename": forcing_filename,
        "csv_filename": csv_filename,
        "package_manifest_filename": package_manifest_filename,
    }
    seen: dict[str, str] = {}
    for field_name, filename in package_names.items():
        if not _safe_station_forcing_filename(str(filename)):
            raise ForcingProductionError(f"Configured package filename {field_name}={filename!r} is unsafe.")
        previous = seen.setdefault(str(filename), field_name)
        if previous != field_name:
            raise ForcingProductionError(f"Configured package filename {filename!r} is reused by {previous}.")
    station_names = {
        _station_forcing_filename(station, _station_forcing_index(station))
        for station in stations
    }
    reserved = _reserved_shud_station_filenames()
    collisions = sorted(station_names.intersection(reserved))
    if collisions:
        raise ForcingProductionError(
            "Station forcing filenames collide with reserved SHUD/package names: " + ", ".join(collisions)
        )


def _station_forcing_sort_key(station: MetStation) -> tuple[int, str]:
    return (_station_forcing_index(station), station.station_id)


def _station_order_manifest(stations: Sequence[MetStation]) -> list[dict[str, Any]]:
    return [
        {
            "station_id": station.station_id,
            "shud_forcing_index": _station_forcing_index(station),
            "forcing_filename": _station_forcing_filename(station, _station_forcing_index(station)),
            "longitude": float(station.longitude),
            "latitude": float(station.latitude),
            "elevation_m": float(station.elevation_m),
        }
        for station in sorted(stations, key=_station_forcing_sort_key)
    ]


def _quality_flags_manifest(
    rows: Sequence[ForcingTimeseriesRow],
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
) -> dict[str, Any]:
    return {
        "station_timeseries": sorted({row.quality_flag for row in rows}),
        "canonical_products": sorted(
            {
                product.quality_flag
                for products_for_variable in products_by_variable.values()
                for product in products_for_variable.values()
            }
        ),
    }


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


def _normalize_longitude(longitude: float) -> float:
    if longitude > 180.0:
        return longitude - 360.0
    return longitude


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


def _grid_signature(grid_points: Sequence[GridPoint]) -> tuple[tuple[str, float, float], ...]:
    return tuple(
        (point.grid_cell_id, round(float(point.longitude), 12), round(float(point.latitude), 12))
        for point in grid_points
    )


def _grid_signature_hash(grid_points: Sequence[GridPoint]) -> str:
    return sha256_bytes(_json_bytes({"grid_points": _grid_signature(grid_points)}))


def _format_grid_signatures(signatures: Mapping[tuple[str, str], str]) -> dict[str, str]:
    return {f"{source_id}:{grid_id}": signature for (source_id, grid_id), signature in sorted(signatures.items())}


def _station_signature(stations: Sequence[MetStation]) -> dict[str, Any]:
    station_rows = [
        {
            "station_id": station.station_id,
            "station_role": station.station_role,
            "longitude": round(float(station.longitude), 12),
            "latitude": round(float(station.latitude), 12),
            "elevation_m": round(float(station.elevation_m), 6),
            "shud_forcing_index": _station_properties(station).get("shud_forcing_index"),
            "forcing_filename": _station_forcing_filename(station, _station_forcing_index(station)),
        }
        for station in sorted(stations, key=lambda item: item.station_id)
    ]
    checksum = sha256_bytes(_json_bytes({"stations": station_rows}))
    return {
        "schema_version": "nhms.forcing_station_signature.v1",
        "station_count": len(station_rows),
        "station_ids": [row["station_id"] for row in station_rows],
        "checksum": checksum,
        "stations": station_rows,
    }


def _station_signature_matches(existing: Any, current: Mapping[str, Any]) -> bool:
    if not isinstance(existing, Mapping):
        return False
    return (
        _optional_int(existing.get("station_count")) == int(current["station_count"])
        and list(existing.get("station_ids") or []) == list(current["station_ids"])
        and str(existing.get("checksum") or "") == str(current["checksum"])
    )


def _products(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
) -> tuple[CanonicalProduct, ...]:
    return tuple(
        product for products_for_variable in products_by_variable.values() for product in products_for_variable.values()
    )


def _canonical_product_ids(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
) -> tuple[str, ...]:
    return tuple(sorted(product.canonical_product_id for product in _products(products_by_variable)))


def _time_range_manifest(valid_times: Sequence[datetime]) -> dict[str, str | int]:
    if not valid_times:
        return {"start_time": "", "end_time": "", "timestep_count": 0}
    ordered = sorted(valid_times)
    return {
        "start_time": _format_time(ordered[0]),
        "end_time": _format_time(ordered[-1]),
        "timestep_count": len(ordered),
    }


def _scheduler_canonical_identity_manifest(
    *,
    canonical_product_id: str | None,
    canonical_identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    identity = dict(canonical_identity or {})
    if canonical_product_id not in (None, ""):
        identity["canonical_product_id"] = str(canonical_product_id)
    if "policy_identity" in identity and isinstance(identity["policy_identity"], Mapping):
        identity["policy_identity"] = dict(identity["policy_identity"])
    if "source_object_identity" in identity and isinstance(identity["source_object_identity"], Mapping):
        identity["source_object_identity"] = dict(identity["source_object_identity"])
    return _json_round_trip(identity)


def _scheduler_canonical_identity_matches(existing: Any, current: Mapping[str, Any]) -> bool:
    if not current:
        return True
    if not isinstance(existing, Mapping):
        return False
    return _stable_identity(existing) == _stable_identity(current)


def _canonical_input_signature(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    cycle_time: datetime,
    *,
    object_store: LocalObjectStore | None = None,
) -> dict[str, Any]:
    product_rows = [
        {
            "canonical_product_id": product.canonical_product_id,
            "source_id": product.source_id,
            "cycle_time": _format_time(product.cycle_time),
            "valid_time": _format_time(product.valid_time),
            "lead_time_hours": _product_lead_hours(product, cycle_time),
            "variable": product.variable,
            "unit": product.unit,
            "grid_id": product.grid_id,
            "grid_definition_uri": product.grid_definition_uri,
            "native_time_resolution": product.native_time_resolution,
            "native_spatial_resolution": product.native_spatial_resolution,
            "object_uri": product.object_uri,
            "checksum": product.checksum,
            "quality_flag": product.quality_flag,
            "grid_definition_content_signature": _grid_definition_content_signature(product, object_store),
        }
        for products_for_variable in products_by_variable.values()
        for product in products_for_variable.values()
    ]
    product_rows.sort(
        key=lambda row: (
            str(row["valid_time"]),
            str(row["variable"]),
            str(row["canonical_product_id"]),
            str(row["source_id"]),
        )
    )
    checksum = sha256_bytes(_json_bytes({"products": product_rows}))
    return {
        "schema_version": "nhms.forcing_canonical_input_signature.v2",
        "product_count": len(product_rows),
        "canonical_product_ids": [str(row["canonical_product_id"]) for row in product_rows],
        "checksum": checksum,
        "products": product_rows,
    }


def _grid_definition_content_signature(
    product: CanonicalProduct,
    object_store: LocalObjectStore | None,
) -> dict[str, Any] | None:
    if object_store is None or not product.grid_definition_uri:
        return None
    try:
        content = object_store.read_bytes(product.grid_definition_uri)
        definition = json.loads(content.decode("utf-8"))
    except (ObjectStoreError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    grid_signature = _grid_definition_signature(definition)
    if grid_signature is None:
        return None
    return {
        "schema_version": "nhms.grid_definition_content_signature.v1",
        "uri": product.grid_definition_uri,
        "checksum": sha256_bytes(content),
        "grid_signature": grid_signature,
    }


def _grid_definition_signature(definition: Any) -> dict[str, Any] | None:
    if not isinstance(definition, Mapping):
        return None
    if definition.get("layout") == "rectilinear":
        try:
            y_count, x_count = (int(value) for value in definition["shape"])
            longitudes = tuple(round(_normalize_longitude(float(value)), 12) for value in definition["longitudes"])
            latitudes = tuple(round(float(value), 12) for value in definition["latitudes"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(longitudes) != x_count or len(latitudes) != y_count:
            return None
        return {
            "layout": "rectilinear",
            "shape": [y_count, x_count],
            "longitudes": list(longitudes),
            "latitudes": list(latitudes),
        }

    cells = definition.get("cells") or definition.get("points")
    if not isinstance(cells, list):
        return None
    signed_cells: list[dict[str, Any]] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            return None
        try:
            longitude = _normalize_longitude(float(cell.get("lon", cell.get("longitude"))))
            latitude = float(cell.get("lat", cell.get("latitude")))
        except (TypeError, ValueError):
            return None
        if not _valid_geographic_coordinate(longitude, latitude):
            return None
        signed_cells.append(
            {
                "grid_cell_id": str(cell.get("grid_cell_id", cell.get("id", index))),
                "longitude": round(longitude, 12),
                "latitude": round(latitude, 12),
            }
        )
    return {"layout": "cells", "cells": signed_cells}


def _canonical_input_signature_matches(existing: Any, current: Mapping[str, Any]) -> bool:
    if not isinstance(existing, Mapping):
        return False
    return (
        str(existing.get("schema_version") or "") == str(current.get("schema_version") or "")
        and _optional_int(existing.get("product_count")) == int(current["product_count"])
        and list(existing.get("canonical_product_ids") or []) == list(current["canonical_product_ids"])
        and str(existing.get("checksum") or "") == str(current["checksum"])
    )


def _stable_identity(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(_json_round_trip(value), sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_round_trip(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=_json_default))


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
    all_times = sorted(
        {valid_time for variable in required_variables for valid_time in products_by_variable.get(variable, {})}
    )
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


def _validate_canonical_product_units(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
) -> None:
    mismatches: list[str] = []
    for variable, products_for_variable in products_by_variable.items():
        expected_units = EXPECTED_CANONICAL_UNITS.get(variable)
        if expected_units is None:
            continue
        for valid_time, product in sorted(products_for_variable.items(), key=lambda item: item[0]):
            if product.unit not in expected_units:
                mismatches.append(
                    f"{variable}:{_format_time(valid_time)} unit={product.unit!r} expected={list(expected_units)}"
                )
    if mismatches:
        raise ForcingProductionError(f"Canonical product unit mismatch: {', '.join(mismatches[:10])}")


def _valid_times(products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]]) -> tuple[datetime, ...]:
    return tuple(
        sorted(
            {
                valid_time
                for products_for_variable in products_by_variable.values()
                for valid_time in products_for_variable
            }
        )
    )


def _limit_products_by_max_lead_hours(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    *,
    cycle_time: datetime,
    max_lead_hours: int | None,
) -> dict[str, dict[datetime, CanonicalProduct]]:
    if max_lead_hours is None:
        return {
            variable: dict(products_for_variable) for variable, products_for_variable in products_by_variable.items()
        }
    return {
        variable: {
            valid_time: product
            for valid_time, product in products_for_variable.items()
            if _product_lead_hours(product, cycle_time) <= max_lead_hours
        }
        for variable, products_for_variable in products_by_variable.items()
    }


def _limit_products_by_min_lead_hours(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    *,
    cycle_time: datetime,
    min_lead_hours: int | None,
) -> dict[str, dict[datetime, CanonicalProduct]]:
    if min_lead_hours is None:
        return {
            variable: dict(products_for_variable) for variable, products_for_variable in products_by_variable.items()
        }
    return {
        variable: {
            valid_time: product
            for valid_time, product in products_for_variable.items()
            if _product_lead_hours(product, cycle_time) >= min_lead_hours
        }
        for variable, products_for_variable in products_by_variable.items()
    }


def _min_lead_hours_from_env() -> int | None:
    value = os.getenv("FORCING_MIN_LEAD_HOURS")
    if value in (None, ""):
        return None
    return int(value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def _lead_window_from_products(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    cycle_time: datetime,
) -> dict[str, int | None]:
    lead_hours = sorted(
        {
            _product_lead_hours(product, cycle_time)
            for products_for_variable in products_by_variable.values()
            for product in products_for_variable.values()
        }
    )
    return {
        "min_lead_hours": lead_hours[0] if lead_hours else None,
        "max_lead_hours": lead_hours[-1] if lead_hours else None,
    }


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _max_product_lead_hours(
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    cycle_time: datetime,
) -> int | None:
    lead_hours = [
        _product_lead_hours(product, cycle_time)
        for products_for_variable in products_by_variable.values()
        for product in products_for_variable.values()
    ]
    return max(lead_hours) if lead_hours else None


def _product_lead_hours(product: CanonicalProduct, cycle_time: datetime) -> int:
    if product.lead_time_hours is not None:
        return int(product.lead_time_hours)
    elapsed_seconds = (_ensure_utc(product.valid_time) - _ensure_utc(cycle_time)).total_seconds()
    return int(round(elapsed_seconds / 3600.0))


def _is_era5_source(source_id: str) -> bool:
    return normalize_source_id(source_id) == "ERA5"


def _is_ifs_source(source_id: str) -> bool:
    return normalize_source_id(source_id) == "IFS"


def _uses_era5_latency_fallback(source_id: str) -> bool:
    return _is_era5_source(source_id)


def _fallback_variables_for_required(required_variables: Sequence[str]) -> tuple[str, ...]:
    variables: list[str] = []
    for variable in required_variables:
        fallback_variable = "shortwave_down" if variable == "net_radiation" else variable
        if fallback_variable not in variables:
            variables.append(fallback_variable)
    return tuple(variables)


def _fallback_products_by_required_variable(
    products: Sequence[CanonicalProduct],
    *,
    required_variables: Sequence[str],
) -> dict[str, dict[datetime, CanonicalProduct]]:
    reverse_variables = {
        ("shortwave_down" if variable == "net_radiation" else variable): variable for variable in required_variables
    }
    grouped: dict[str, dict[datetime, CanonicalProduct]] = {variable: {} for variable in required_variables}
    for product in products:
        required_variable = reverse_variables.get(product.variable)
        if required_variable is None or not canonical_product_is_forcing_usable(
            {"quality_flag": product.quality_flag, "checksum": product.checksum}
        ):
            continue
        grouped[required_variable][product.valid_time] = product
    return grouped


def _grid_points_by_source_grid(
    fields: Mapping[str, Mapping[datetime, CanonicalField]],
) -> dict[tuple[str, str], tuple[GridPoint, ...]]:
    grid_points_by_source_grid: dict[tuple[str, str], tuple[GridPoint, ...]] = {}
    for fields_by_time in fields.values():
        for canonical_field in fields_by_time.values():
            source_grid = (canonical_field.product.source_id, canonical_field.product.grid_id)
            existing = grid_points_by_source_grid.get(source_grid)
            if existing is None:
                grid_points_by_source_grid[source_grid] = tuple(canonical_field.grid_points)
            elif existing != tuple(canonical_field.grid_points):
                raise ForcingProductionError(
                    f"Canonical products for source {canonical_field.product.source_id} "
                    f"grid {canonical_field.product.grid_id} "
                    "do not share one grid definition."
                )
    return grid_points_by_source_grid


def _required_grid_cell_ids_by_source_grid(
    weights: Mapping[tuple[str, str], Sequence[InterpolationWeight]],
) -> dict[tuple[str, str], frozenset[str]]:
    return {
        source_grid: frozenset(weight.grid_cell_id for weight in source_grid_weights)
        for source_grid, source_grid_weights in weights.items()
    }


def _canonical_variable_for_forcing(variable: str, canonical_to_forcing: Mapping[str, str]) -> str:
    matches = sorted(
        canonical_variable
        for canonical_variable, forcing_variable in canonical_to_forcing.items()
        if forcing_variable == variable
    )
    if not matches:
        raise ForcingProductionError(f"No canonical variable is mapped to forcing variable {variable}.")
    return matches[0]


def _source_id_for_output_variable(
    variable: str,
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    valid_time: datetime,
    canonical_to_forcing: Mapping[str, str],
) -> str:
    if variable == "wind":
        return products_by_variable["wind_u_10m"][valid_time].source_id
    canonical_variable = _canonical_variable_for_forcing(variable, canonical_to_forcing)
    return products_by_variable[canonical_variable][valid_time].source_id


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


def _weights_match_grid_signature(weights: Sequence[InterpolationWeight], grid_signature: str) -> bool:
    if not weights:
        return False
    return all(str(weight.grid_signature or "") == grid_signature for weight in weights)


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
    return {key: tuple(sorted(group, key=lambda item: item.grid_cell_id)) for key, group in grouped.items()}


def _native_resolution_for_output(
    variable: str,
    products_by_variable: Mapping[str, Mapping[datetime, CanonicalProduct]],
    valid_time: datetime,
    canonical_to_forcing: Mapping[str, str],
) -> str | None:
    if variable == "wind":
        return products_by_variable["wind_u_10m"][valid_time].native_time_resolution
    canonical_variable = _canonical_variable_for_forcing(variable, canonical_to_forcing)
    return products_by_variable[canonical_variable][valid_time].native_time_resolution


def _precip_step_hours(product: CanonicalProduct, default_hours: float) -> float:
    parsed = _parse_hour_resolution(product.native_time_resolution)
    step_hours = parsed if parsed is not None else default_hours
    if step_hours <= 0.0 or not math.isfinite(step_hours):
        raise ForcingProductionError(
            f"Invalid precipitation step hours for canonical product {product.canonical_product_id}."
        )
    return step_hours


def _parse_hour_resolution(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized.startswith("pt") and normalized.endswith("h"):
        normalized = normalized[2:]
    if normalized.endswith("hours"):
        normalized = normalized.removesuffix("hours").strip()
    elif normalized.endswith("hour"):
        normalized = normalized.removesuffix("hour").strip()
    elif normalized.endswith("hrs"):
        normalized = normalized.removesuffix("hrs").strip()
    elif normalized.endswith("hr"):
        normalized = normalized.removesuffix("hr").strip()
    elif normalized.endswith("h"):
        normalized = normalized[:-1].strip()
    try:
        return float(normalized)
    except ValueError:
        return None


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


def _data_array_size(data_array: Any) -> int:
    size = getattr(data_array, "size", None)
    if size is not None:
        return int(size)
    shape = _data_array_shape(data_array)
    total = 1
    for dimension in shape:
        total *= dimension
    return total


def _data_array_shape(data_array: Any) -> tuple[int, ...]:
    shape = getattr(data_array, "shape", ())
    return tuple(int(size) for size in shape)


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
    return normalize_source_id(source_id).lower()


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
