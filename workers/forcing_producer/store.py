from __future__ import annotations

import math
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.common.met_store import MetStoreError, default_database_url
from packages.common.source_identity import normalize_source_id

from .direct_grid_contract import (
    DirectGridContractError,
    DirectGridForcingContract,
    load_forcing_mapping_contract_from_manifest,
)
from .producer import (
    CanonicalProduct,
    ForcingComponent,
    ForcingTimeseriesRow,
    InterpolationWeight,
    MetStation,
)

DIRECT_GRID_CACHE_STATION_ROLE = "direct_grid_cache"


@dataclass(frozen=True)
class PsycopgForcingRepository:
    """Postgres repository for the SHUD forcing producer."""

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgForcingRepository:
        return cls(default_database_url())

    def resolve_model_identity(self, *, model_id: str) -> dict[str, Any]:
        row = self._fetch_optional(
            """
            SELECT
                mi.basin_version_id,
                bv.basin_id,
                mi.river_network_version_id
            FROM core.model_instance mi
            JOIN core.basin_version bv
              ON bv.basin_version_id = mi.basin_version_id
            WHERE mi.model_id = %s
            """,
            (model_id,),
        )
        if row is None:
            raise MetStoreError(f"Model instance {model_id!r} was not found.")
        return {
            "basin_id": str(row["basin_id"]),
            "basin_version_id": str(row["basin_version_id"]),
            "river_network_version_id": str(row["river_network_version_id"]),
        }

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        row = self._fetch_optional(
            """
            SELECT basin_version_id
            FROM core.model_instance
            WHERE model_id = %s
            """,
            (model_id,),
        )
        if row is None:
            raise MetStoreError(f"Model instance {model_id!r} was not found.")
        return str(row["basin_version_id"])

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        rows = self._fetch_all(
            """
            SELECT
                station_id,
                basin_version_id,
                station_name,
                ST_X(geom) AS longitude,
                ST_Y(geom) AS latitude,
                elevation_m,
                station_role,
                properties_json
            FROM met.met_station
            WHERE basin_version_id = %s
              AND active_flag = true
              AND station_role <> %s
              AND NOT (properties_json @> '{"derived_cache": true}'::jsonb)
              AND COALESCE(properties_json->>'forcing_mapping_mode', '') <> 'direct_grid'
            ORDER BY station_id
            """,
            (basin_version_id, DIRECT_GRID_CACHE_STATION_ROLE),
        )
        return tuple(
            MetStation(
                station_id=str(row["station_id"]),
                basin_version_id=str(row["basin_version_id"]),
                station_name=row.get("station_name"),
                longitude=float(row["longitude"]),
                latitude=float(row["latitude"]),
                elevation_m=float(row["elevation_m"]),
                station_role=str(row["station_role"]),
                properties_json=row.get("properties_json") or {},
            )
            for row in rows
            if row["elevation_m"] is not None
        )

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        rows = self._fetch_all(
            """
            SELECT
                canonical_product_id,
                source_id,
                cycle_time,
                valid_time,
                lead_time_hours,
                variable,
                unit,
                grid_id,
                grid_definition_uri,
                native_time_resolution,
                native_spatial_resolution,
                object_uri,
                checksum,
                quality_flag,
                lineage_json
            FROM met.canonical_met_product
            WHERE source_id = %s
              AND cycle_time = %s
            ORDER BY variable, valid_time, canonical_product_id
            """,
            (source_id, cycle_time),
        )
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                lead_time_hours=row.get("lead_time_hours"),
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"] or ""),
                quality_flag=str(row.get("quality_flag") or "ok"),
                lineage_json=row.get("lineage_json") or {},
            )
            for row in rows
        )

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]:
        if not variables:
            return ()
        rows = self._fetch_all(
            """
            WITH ranked AS (
                SELECT
                    cmp.canonical_product_id,
                    cmp.source_id,
                    cmp.cycle_time,
                    cmp.valid_time,
                    cmp.lead_time_hours,
                    cmp.variable,
                    cmp.unit,
                    cmp.grid_id,
                    cmp.grid_definition_uri,
                    cmp.native_time_resolution,
                    cmp.native_spatial_resolution,
                    cmp.object_uri,
                    cmp.checksum,
                    cmp.quality_flag,
                    cmp.lineage_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY cmp.valid_time, cmp.variable
                        ORDER BY cmp.lead_time_hours ASC NULLS LAST, cmp.cycle_time DESC, cmp.canonical_product_id
                    ) AS rank
                FROM met.canonical_met_product cmp
                JOIN met.forecast_cycle fc
                  ON fc.source_id = cmp.source_id
                 AND fc.cycle_time = cmp.cycle_time
                WHERE cmp.source_id = %s
                  AND fc.status = 'canonical_ready'
                  AND cmp.valid_time >= %s
                  AND cmp.valid_time <= %s
                  AND cmp.variable = ANY(%s)
                  AND cmp.quality_flag = 'ok'
                  AND NULLIF(BTRIM(cmp.checksum), '') IS NOT NULL
            )
            SELECT
                canonical_product_id,
                source_id,
                cycle_time,
                valid_time,
                lead_time_hours,
                variable,
                unit,
                grid_id,
                grid_definition_uri,
                native_time_resolution,
                native_spatial_resolution,
                object_uri,
                checksum,
                quality_flag,
                lineage_json
            FROM ranked
            WHERE rank = 1
            ORDER BY variable, valid_time, canonical_product_id
            """,
            (source_id, start_time, end_time, list(variables)),
        )
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                lead_time_hours=row.get("lead_time_hours"),
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"] or ""),
                quality_flag=str(row.get("quality_flag") or "ok"),
                lineage_json=row.get("lineage_json") or {},
            )
            for row in rows
        )

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        rows = self._fetch_all(
            """
            SELECT source_id,
                   grid_id,
                   model_id,
                   station_id,
                   variable,
                   grid_cell_id,
                   weight,
                   method,
                   grid_signature
            FROM met.interp_weight
            WHERE source_id = %s
              AND grid_id = %s
              AND model_id = %s
            ORDER BY station_id, variable, grid_cell_id
            """,
            (source_id, grid_id, model_id),
        )
        return tuple(
            InterpolationWeight(
                source_id=str(row["source_id"]),
                grid_id=str(row["grid_id"]),
                model_id=str(row["model_id"]),
                station_id=str(row["station_id"]),
                variable=str(row["variable"]),
                grid_cell_id=str(row["grid_cell_id"]),
                weight=float(row["weight"]),
                method=str(row["method"]),
                grid_signature=row.get("grid_signature"),
            )
            for row in rows
        )

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        if not weights:
            return
        scopes = {(weight.source_id, weight.grid_id, weight.model_id) for weight in weights}
        if len(scopes) != 1:
            raise MetStoreError("Interpolation weights must be replaced one source/grid/model scope at a time.")
        _validate_interp_weight_snapshot(weights)
        source_id, grid_id, model_id = next(iter(scopes))
        rows = [
            (
                weight.source_id,
                weight.grid_id,
                weight.model_id,
                weight.station_id,
                weight.variable,
                weight.grid_cell_id,
                weight.weight,
                weight.method,
                weight.grid_signature,
            )
            for weight in weights
        ]
        self._replace_values(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))
            """,
            (f"met.interp_weight:{source_id}\x1f{grid_id}\x1f{model_id}",),
            """
            DELETE FROM met.interp_weight
            WHERE source_id = %s
              AND grid_id = %s
              AND model_id = %s
            """,
            (source_id, grid_id, model_id),
            """
            INSERT INTO met.interp_weight (
                source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method, grid_signature
            )
            VALUES %s
            ON CONFLICT (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
            DO UPDATE SET
                weight = EXCLUDED.weight,
                method = EXCLUDED.method,
                grid_signature = EXCLUDED.grid_signature
            """,
            rows,
        )

    def ensure_direct_grid_met_stations(
        self,
        *,
        basin_version_id: str,
        contract: DirectGridForcingContract,
    ) -> None:
        if not contract.stations:
            return
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        rows = [
            (
                station.station_id,
                basin_version_id,
                f"Direct-grid station {station.shud_forcing_index}",
                station.longitude,
                station.latitude,
                station.z,
                DIRECT_GRID_CACHE_STATION_ROLE,
                Json(
                    {
                        **dict(station.properties),
                        "derived_cache": True,
                        "forcing_mapping_mode": "direct_grid",
                        "direct_grid": True,
                        "manifest_authority": True,
                        "binding_checksum": contract.binding_checksum,
                        "binding_uri": contract.binding_uri,
                        "model_input_package_id": contract.model_input_package_id,
                        "sp_att_path": contract.sp_att_path,
                        "sp_att_checksum": contract.sp_att_checksum,
                        "grid_id": station.grid_id,
                        "contract_grid_id": contract.grid_id,
                        "grid_cell_id": station.grid_cell_id,
                        "grid_signature": contract.grid_signature,
                        "shud_forcing_index": station.shud_forcing_index,
                        "forcing_filename": station.forcing_filename,
                        "x": station.x,
                        "y": station.y,
                        "z": station.z,
                        "mirror_identity": _direct_grid_mirror_identity(contract, station.grid_id),
                    }
                ),
            )
            for station in sorted(contract.stations, key=lambda item: item.shud_forcing_index)
        ]
        self._replace_values(
            None,
            (),
            None,
            (),
            """
            INSERT INTO met.met_station (
                station_id,
                basin_version_id,
                station_name,
                geom,
                elevation_m,
                station_role,
                active_flag,
                properties_json
            )
            VALUES %s
            ON CONFLICT (station_id) DO UPDATE SET
                basin_version_id = EXCLUDED.basin_version_id,
                station_name = EXCLUDED.station_name,
                geom = EXCLUDED.geom,
                elevation_m = EXCLUDED.elevation_m,
                station_role = EXCLUDED.station_role,
                properties_json = EXCLUDED.properties_json
            WHERE met.met_station.basin_version_id = EXCLUDED.basin_version_id
              AND met.met_station.station_role = 'direct_grid_cache'
              AND met.met_station.properties_json @> '{"derived_cache": true}'::jsonb
              AND met.met_station.properties_json->>'forcing_mapping_mode' = 'direct_grid'
              AND met.met_station.properties_json->>'binding_checksum' = EXCLUDED.properties_json->>'binding_checksum'
              AND met.met_station.properties_json->>'model_input_package_id' =
                  EXCLUDED.properties_json->>'model_input_package_id'
              AND met.met_station.properties_json->>'grid_signature' = EXCLUDED.properties_json->>'grid_signature'
              AND met.met_station.properties_json->>'contract_grid_id' = EXCLUDED.properties_json->>'contract_grid_id'
              AND met.met_station.properties_json->>'grid_id' = EXCLUDED.properties_json->>'grid_id'
            """,
            rows,
            template=(
                "(%s, %s, %s, "
                "ST_SetSRID(ST_MakePoint(%s, %s), 4490), "
                "%s, %s, false, %s)"
            ),
            expected_insert_count=len(rows),
            conflict_error=(
                "Direct-grid met_station mirror conflicts with an existing station_id that is not the same "
                "derived direct-grid cache binding."
            ),
        )

    def load_forcing_mapping_contract(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        source_id: str | None = None,
    ) -> DirectGridForcingContract | None:
        row = self._fetch_optional(
            """
            SELECT resource_profile
            FROM core.model_instance
            WHERE model_id = %s
              AND basin_version_id = %s
            """,
            (model_id, basin_version_id),
        )
        if row is None:
            raise MetStoreError(
                f"Model instance {model_id!r} for basin_version_id {basin_version_id!r} was not found."
            )
        resource_profile = row.get("resource_profile")
        if resource_profile is None:
            resource_profile = {}
        if not isinstance(resource_profile, Mapping):
            raise DirectGridContractError(
                "Model resource_profile must be a JSON object.",
                details={
                    "model_id": model_id,
                    "basin_version_id": basin_version_id,
                    "actual_type": type(resource_profile).__name__,
                },
            )
        return load_forcing_mapping_contract_from_manifest(
            resource_profile,
            source_id=source_id,
            allow_root_direct_grid=False,
        )

    def load_direct_grid_validation_assets(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        contract: DirectGridForcingContract,
    ) -> Mapping[str, Any]:
        row = self._fetch_optional(
            """
            SELECT resource_profile
            FROM core.model_instance
            WHERE model_id = %s
              AND basin_version_id = %s
            """,
            (model_id, basin_version_id),
        )
        if row is None:
            raise MetStoreError(
                f"Model instance {model_id!r} for basin_version_id {basin_version_id!r} was not found."
            )
        resource_profile = row.get("resource_profile")
        if resource_profile is None:
            resource_profile = {}
        if not isinstance(resource_profile, Mapping):
            raise DirectGridContractError(
                "Model resource_profile must be a JSON object.",
                details={
                    "model_id": model_id,
                    "basin_version_id": basin_version_id,
                    "actual_type": type(resource_profile).__name__,
                },
            )
        source_id = contract.applicable_source_ids[0] if len(contract.applicable_source_ids) == 1 else None
        authoritative_contract = load_forcing_mapping_contract_from_manifest(
            resource_profile,
            source_id=source_id,
            allow_root_direct_grid=False,
        )
        if authoritative_contract is None:
            return {}
        if authoritative_contract != contract:
            raise DirectGridContractError(
                "Direct-grid validation contract does not match model resource_profile.",
                field="direct_grid_forcing",
                details={
                    "model_id": model_id,
                    "basin_version_id": basin_version_id,
                },
            )
        return {
            "model_input_package_id": authoritative_contract.model_input_package_id,
        }

    def get_forcing_version(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None:
        return self._fetch_optional(
            """
            SELECT *
            FROM met.forcing_version
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_id, cycle_time, model_id),
        )

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        return self._fetch_one(
            """
            INSERT INTO met.forcing_version (
                forcing_version_id,
                model_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                station_count,
                forcing_package_uri,
                checksum,
                lineage_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (forcing_version_id) DO UPDATE SET
                model_id = EXCLUDED.model_id,
                source_id = EXCLUDED.source_id,
                cycle_time = EXCLUDED.cycle_time,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                station_count = EXCLUDED.station_count,
                forcing_package_uri = EXCLUDED.forcing_package_uri,
                checksum = EXCLUDED.checksum,
                lineage_json = EXCLUDED.lineage_json
            RETURNING *
            """,
            (
                record["forcing_version_id"],
                record["model_id"],
                record["source_id"],
                record["cycle_time"],
                record["start_time"],
                record["end_time"],
                record["station_count"],
                record["forcing_package_uri"],
                record["checksum"],
                Json(dict(record.get("lineage_json") or {})),
            ),
        )

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE met.forcing_version
            SET checksum = %s
            WHERE forcing_version_id = %s
            RETURNING *
            """,
            (checksum, forcing_version_id),
        )

    def clear_forcing_version_checksum(self, forcing_version_id: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE met.forcing_version
            SET checksum = NULL
            WHERE forcing_version_id = %s
            RETURNING *
            """,
            (forcing_version_id,),
        )

    def verify_forcing_version_children(
        self,
        *,
        forcing_version_id: str,
        expected_components: Sequence[ForcingComponent],
        expected_station_ids: Sequence[str],
        expected_valid_times: Sequence[datetime],
        expected_variables: Sequence[str],
    ) -> Mapping[str, Any]:
        expected_component_tuples = Counter(
            (
                component.canonical_product_id,
                component.variable,
                component.valid_time_start,
                component.valid_time_end,
                component.role,
            )
            for component in expected_components
        )
        expected_component_count = sum(expected_component_tuples.values())
        expected_timeseries_count = (
            len(tuple(expected_station_ids)) * len(tuple(expected_valid_times)) * len(tuple(expected_variables))
        )
        component_rows = self._fetch_all(
            """
            SELECT canonical_product_id,
                   variable,
                   valid_time_start,
                   valid_time_end,
                   role
            FROM met.forcing_version_component
            WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        component_tuples = Counter(
            (
                str(row["canonical_product_id"]),
                str(row["variable"]),
                row["valid_time_start"],
                row["valid_time_end"],
                str(row["role"]),
            )
            for row in component_rows
        )
        timeseries_rows = self._fetch_all(
            """
            SELECT station_id,
                   valid_time,
                   variable
            FROM met.forcing_station_timeseries
            WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        timeseries_tuples = Counter(
            (str(row["station_id"]), row["valid_time"], str(row["variable"]))
            for row in timeseries_rows
        )
        expected_timeseries_tuples = Counter(
            (station_id, valid_time, variable)
            for station_id in expected_station_ids
            for valid_time in expected_valid_times
            for variable in expected_variables
        )
        proof = {
            "forcing_version_id": forcing_version_id,
            "expected_component_count": expected_component_count,
            "component_count": sum(component_tuples.values()),
            "component_tuple_count": len(component_tuples),
            "expected_component_tuple_count": len(expected_component_tuples),
            "expected_timeseries_row_count": expected_timeseries_count,
            "timeseries_row_count": sum(timeseries_tuples.values()),
            "timeseries_tuple_count": len(timeseries_tuples),
            "expected_timeseries_tuple_count": len(expected_timeseries_tuples),
            "station_count": len({station_id for station_id, _, _ in timeseries_tuples}),
            "timestep_count": len({valid_time for _, valid_time, _ in timeseries_tuples}),
            "variable_count": len({variable for _, _, variable in timeseries_tuples}),
        }
        proof["complete"] = (
            proof["component_count"] == expected_component_count
            and component_tuples == expected_component_tuples
            and proof["timeseries_row_count"] == expected_timeseries_count
            and timeseries_tuples == expected_timeseries_tuples
            and proof["station_count"] == len(tuple(expected_station_ids))
            and proof["timestep_count"] == len(tuple(expected_valid_times))
            and proof["variable_count"] == len(tuple(expected_variables))
        )
        return proof

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None:
        rows = [
            (
                component.forcing_version_id,
                component.canonical_product_id,
                component.variable,
                component.valid_time_start,
                component.valid_time_end,
                component.role,
            )
            for component in components
        ]
        self._replace_values(
            None,
            (),
            "DELETE FROM met.forcing_version_component WHERE forcing_version_id = %s",
            (forcing_version_id,),
            """
            INSERT INTO met.forcing_version_component (
                forcing_version_id, canonical_product_id, variable, valid_time_start, valid_time_end, role
            )
            VALUES %s
            """,
            rows,
        )

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: Sequence[ForcingTimeseriesRow],
    ) -> None:
        value_rows = [
            (
                row.forcing_version_id,
                row.basin_version_id,
                row.station_id,
                row.valid_time,
                row.source_id,
                row.variable,
                row.value,
                row.unit,
                row.native_resolution,
                row.quality_flag,
            )
            for row in rows
        ]
        self._replace_values(
            None,
            (),
            "DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (forcing_version_id,),
            """
            INSERT INTO met.forcing_station_timeseries (
                forcing_version_id,
                basin_version_id,
                station_id,
                valid_time,
                source_id,
                variable,
                value,
                unit,
                native_resolution,
                quality_flag
            )
            VALUES %s
            """,
            value_rows,
        )

    def find_registered_snapshot_bbox_by_identity(
        self,
        *,
        source_id: str,
        grid_id: str,
        grid_signature: str,
    ) -> tuple[float, float, float, float, uuid.UUID, datetime | None] | None:
        """Return ``(bbox_south, bbox_north, bbox_west, bbox_east,
        grid_snapshot_id, superseded_at)`` for the identity, or ``None``.

        The caller (SUB-6 producer bbox preflight) distinguishes "missing row"
        from "superseded row" via the returned ``superseded_at`` field, so the
        query DOES NOT filter ``superseded_at IS NULL`` here — that's the
        preflight's fail-closed responsibility. ``source_id`` is normalized to
        mirror :meth:`packages.common.grid_registry_store.PsycopgGridRegistryStore.find_snapshot_by_identity`.
        Most-recently-created row wins when duplicates ever exist.
        """
        normalized_source = normalize_source_id(source_id)
        row = self._fetch_optional(
            """
            SELECT bbox_south,
                   bbox_north,
                   bbox_west,
                   bbox_east,
                   grid_snapshot_id,
                   superseded_at
            FROM met.canonical_grid_snapshot
            WHERE source_id = %s
              AND grid_id = %s
              AND grid_signature = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized_source, grid_id, grid_signature),
        )
        if row is None:
            return None
        return (
            float(row["bbox_south"]),
            float(row["bbox_north"]),
            float(row["bbox_west"]),
            float(row["bbox_east"]),
            uuid.UUID(str(row["grid_snapshot_id"])),
            row["superseded_at"],
        )

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("status", status),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        if not assignments:
            return None
        parameters.extend([source_id, cycle_time])
        return self._fetch_optional(
            f"""
            UPDATE met.forecast_cycle
            SET {", ".join(assignments)}
            WHERE source_id = %s
              AND cycle_time = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def _fetch_one(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise MetStoreError("Forcing database operation did not return a row.")
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    connection.commit()
                    return []
                rows = cursor.fetchall()
                columns = [description.name for description in cursor.description]
                connection.commit()
                return [dict(zip(columns, row, strict=True)) for row in rows]
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise MetStoreError(f"Forcing database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _replace_values(
        self,
        pre_delete_statement: str | None,
        pre_delete_parameters: tuple[Any, ...],
        delete_statement: str | None,
        delete_parameters: tuple[Any, ...],
        insert_statement: str,
        rows: Sequence[tuple[Any, ...]],
        *,
        template: str | None = None,
        expected_insert_count: int | None = None,
        conflict_error: str | None = None,
    ) -> None:
        try:
            import psycopg2
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                if pre_delete_statement is not None:
                    cursor.execute(pre_delete_statement, pre_delete_parameters)
                if delete_statement is not None:
                    cursor.execute(delete_statement, delete_parameters)
                if rows:
                    execute_values(
                        cursor,
                        insert_statement,
                        rows,
                        page_size=len(rows) if expected_insert_count is not None else 5000,
                        template=template,
                    )
                    if expected_insert_count is not None and cursor.rowcount != expected_insert_count:
                        message = conflict_error or "Forcing database write affected an unexpected row count."
                        raise MetStoreError(message)
            connection.commit()
        except MetStoreError:
            if connection is not None:
                connection.rollback()
            raise
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise MetStoreError(f"Forcing database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()


def _validate_interp_weight_snapshot(weights: Sequence[InterpolationWeight]) -> None:
    methods = {weight.method for weight in weights}
    if len(methods) != 1:
        raise MetStoreError("Interpolation weight snapshots must use a single mapping method.")
    if methods != {"direct_grid"}:
        return

    seen: set[tuple[str, str]] = set()
    grid_signatures: set[str] = set()
    for weight in weights:
        station_variable = (weight.station_id, weight.variable)
        if station_variable in seen:
            raise MetStoreError(
                "Direct-grid interpolation weights must contain exactly one grid cell per station/variable."
            )
        seen.add(station_variable)
        if not math.isfinite(weight.weight) or weight.weight != 1.0:
            raise MetStoreError("Direct-grid interpolation weights must use weight 1.0.")
        if not str(weight.grid_cell_id).strip():
            raise MetStoreError("Direct-grid interpolation weights must include a grid_cell_id.")
        grid_signature = str(weight.grid_signature or "").strip()
        if not grid_signature:
            raise MetStoreError("Direct-grid interpolation weights must include a grid_signature.")
        grid_signatures.add(grid_signature)
    if len(grid_signatures) != 1:
        raise MetStoreError("Direct-grid interpolation weight snapshots must use exactly one grid_signature.")


def _direct_grid_mirror_identity(contract: DirectGridForcingContract, station_grid_id: str) -> dict[str, str]:
    return {
        "binding_checksum": contract.binding_checksum,
        "model_input_package_id": contract.model_input_package_id,
        "grid_signature": contract.grid_signature,
        "contract_grid_id": contract.grid_id,
        "grid_id": station_grid_id,
    }
