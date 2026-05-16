from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ModelRegistryError(RuntimeError):
    """Base class for model registry failures."""


class DuplicateResourceError(ModelRegistryError):
    """Raised when a registry resource already exists."""


class MissingResourceError(ModelRegistryError):
    """Raised when a requested registry resource does not exist."""


class InvalidReferenceError(ModelRegistryError):
    """Raised when a payload references a missing or mismatched resource."""


class InvalidPayloadError(ModelRegistryError):
    """Raised when a payload is structurally invalid."""


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ModelRegistryError("DATABASE_URL is required for model registry operations.")
    return database_url


def build_versioned_id(prefix: str, version_label: str | None, explicit_id: str | None = None) -> str:
    """Build a conservative ID from a prefix and version label when an explicit ID is absent."""
    if explicit_id:
        return explicit_id
    if not version_label:
        raise InvalidPayloadError("version_label is required when an explicit id is not provided.")

    label = re.sub(r"[^a-zA-Z0-9]+", "_", version_label.strip()).strip("_").lower()
    if not label:
        raise InvalidPayloadError("version_label must contain at least one alphanumeric character.")
    if not label.startswith("v"):
        label = f"v{label}"
    return f"{prefix}_{label}"


def geometry_to_wkt(geom: Mapping[str, Any] | str, expected_type: str) -> str:
    """Convert simple GeoJSON geometry or WKT into WKT for PostGIS insertion."""
    if isinstance(geom, str):
        candidate = geom.strip()
        if not candidate.upper().startswith(expected_type.upper()):
            raise InvalidPayloadError(f"geom must be a {expected_type} geometry.")
        return candidate

    geom_type = str(geom.get("type", ""))
    if geom_type != expected_type:
        raise InvalidPayloadError(f"geom.type must be {expected_type}.")

    coordinates = geom.get("coordinates")
    if geom_type == "MultiPolygon":
        return _multipolygon_to_wkt(coordinates)
    if geom_type == "LineString":
        return _linestring_to_wkt(coordinates)
    raise InvalidPayloadError(f"Unsupported geometry type: {geom_type}.")


def _linestring_to_wkt(coordinates: Any) -> str:
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise InvalidPayloadError("LineString coordinates must contain at least two points.")
    return "LINESTRING(" + ", ".join(_format_point(point) for point in coordinates) + ")"


def _multipolygon_to_wkt(coordinates: Any) -> str:
    if not isinstance(coordinates, list) or not coordinates:
        raise InvalidPayloadError("MultiPolygon coordinates must contain at least one polygon.")

    polygons: list[str] = []
    for polygon in coordinates:
        if not isinstance(polygon, list) or not polygon:
            raise InvalidPayloadError("Each MultiPolygon polygon must contain at least one ring.")
        rings = []
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise InvalidPayloadError("Each MultiPolygon ring must contain at least four points.")
            rings.append("(" + ", ".join(_format_point(point) for point in ring) + ")")
        polygons.append("(" + ", ".join(rings) + ")")
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"


def _format_point(point: Any) -> str:
    if not isinstance(point, list | tuple) or len(point) < 2:
        raise InvalidPayloadError("Geometry point must contain longitude and latitude.")
    return f"{float(point[0]):.12g} {float(point[1]):.12g}"


@dataclass(frozen=True)
class PsycopgModelRegistryStore:
    database_url: str
    audit_actor: str = "nhms-api"
    audit_actor_role: str = "model-registry"

    @classmethod
    def from_env(cls) -> PsycopgModelRegistryStore:
        return cls(default_database_url())

    def create_basin_with_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        basin_version = dict(payload["basin_version"])
        basin_version_id = build_versioned_id(
            str(payload["basin_id"]),
            basin_version.get("version_label"),
            basin_version.get("basin_version_id"),
        )
        geom_wkt = geometry_to_wkt(basin_version["geom"], "MultiPolygon")
        with self._transaction() as cursor:
            if self._exists(cursor, "core.basin", "basin_id", payload["basin_id"]):
                raise DuplicateResourceError(f"basin_id already exists: {payload['basin_id']}")
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (
                    payload["basin_id"],
                    payload["basin_name"],
                    payload.get("basin_group"),
                    payload.get("description"),
                ),
            )
            basin = dict(cursor.fetchone())
            basin_version_row = self._insert_basin_version(
                cursor,
                basin_id=payload["basin_id"],
                basin_version_id=basin_version_id,
                payload=basin_version,
                geom_wkt=geom_wkt,
            )
        return {"basin": basin, "basin_version": basin_version_row}

    def create_basin_version(self, basin_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        basin_version_id = build_versioned_id(basin_id, payload.get("version_label"), payload.get("basin_version_id"))
        geom_wkt = geometry_to_wkt(payload["geom"], "MultiPolygon")
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin", "basin_id", basin_id):
                raise MissingResourceError(f"basin_id not found: {basin_id}")
            return self._insert_basin_version(
                cursor,
                basin_id=basin_id,
                basin_version_id=basin_version_id,
                payload=payload,
                geom_wkt=geom_wkt,
            )

    def create_river_network(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        segments = list(payload.get("segments") or [])
        segment_count = int(payload.get("segment_count") if payload.get("segment_count") is not None else len(segments))
        if segment_count != len(segments):
            raise InvalidPayloadError("segment_count must equal the number of supplied river segments.")

        river_network_version_id = build_versioned_id(
            f"{payload['basin_version_id']}_rivnet",
            payload.get("version_label"),
            payload.get("river_network_version_id"),
        )
        segment_rows = [
            (
                segment["river_segment_id"],
                river_network_version_id,
                segment.get("segment_order"),
                segment.get("downstream_segment_id"),
                segment.get("length_m"),
                geometry_to_wkt(segment["geom"], "LineString"),
                self._json(segment.get("properties_json") or {}),
            )
            for segment in segments
        ]

        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id,
                    basin_version_id,
                    version_label,
                    segment_count,
                    source_uri,
                    checksum
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    river_network_version_id,
                    payload["basin_version_id"],
                    payload["version_label"],
                    segment_count,
                    payload.get("source_uri"),
                    payload.get("checksum"),
                ),
            )
            network = dict(cursor.fetchone())
            if segment_rows:
                self._execute_values(
                    cursor,
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id,
                        river_network_version_id,
                        segment_order,
                        downstream_segment_id,
                        length_m,
                        geom,
                        properties_json
                    )
                    VALUES %s
                    """,
                    segment_rows,
                    template="(%s, %s, %s, %s, %s, ST_GeomFromText(%s, 4490), %s)",
                )
        return {"river_network_version": network, "segment_count": segment_count}

    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None = None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        filters = ["rnv.basin_version_id = %s"]
        params: list[Any] = [basin_version_id]
        if river_network_version_id is not None:
            filters.append("rnv.river_network_version_id = %s")
            params.append(river_network_version_id)

        where_clause = " AND ".join(filters)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(rs.geom) AS feature_total
                FROM core.river_segment rs
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = rs.river_network_version_id
                WHERE {where_clause}
                """,
                tuple(params),
            )
            counts = cursor.fetchone()
            total = int(counts["total"])
            feature_total = int(counts["feature_total"])
            cursor.execute(
                f"""
                SELECT
                    rs.river_segment_id,
                    rs.river_network_version_id,
                    rnv.basin_version_id,
                    rs.segment_order,
                    rs.downstream_segment_id,
                    rs.length_m,
                    rs.properties_json,
                    ST_AsGeoJSON(rs.geom)::json AS geometry
                FROM core.river_segment rs
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = rs.river_network_version_id
                WHERE {where_clause}
                  AND rs.geom IS NOT NULL
                ORDER BY COALESCE(rs.segment_order, 2147483647), rs.river_segment_id
                LIMIT %s OFFSET %s
                """,
                tuple([*params, limit, offset]),
            )
            rows = [dict(row) for row in cursor.fetchall()]

        features = []
        for row in rows:
            properties_json = row.get("properties_json") or {}
            if isinstance(properties_json, str):
                try:
                    properties_json = json.loads(properties_json)
                except json.JSONDecodeError:
                    properties_json = {}
            properties = dict(properties_json) if isinstance(properties_json, Mapping) else {}
            stream_order = row.get("segment_order")
            name = properties.get("name") or properties.get("segment_name") or row["river_segment_id"]
            properties.update(
                {
                    "segment_id": str(row["river_segment_id"]),
                    "river_segment_id": str(row["river_segment_id"]),
                    "basin_version_id": str(row["basin_version_id"]),
                    "river_network_version_id": str(row["river_network_version_id"]),
                    "name": str(name),
                    "stream_order": int(stream_order) if stream_order is not None else 1,
                    "segment_order": int(stream_order) if stream_order is not None else None,
                    "downstream_segment_id": row.get("downstream_segment_id"),
                    "length_m": float(row["length_m"]) if row.get("length_m") is not None else None,
                }
            )
            features.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": row["geometry"],
                }
            )

        return {
            "type": "FeatureCollection",
            "features": features,
            "total": total,
            "feature_total": feature_total,
            "limit": limit,
            "offset": offset,
        }

    def create_mesh_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mesh_version_id = build_versioned_id(
            f"{payload['basin_version_id']}_mesh",
            payload.get("version_label"),
            payload.get("mesh_version_id"),
        )
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id,
                    basin_version_id,
                    version_label,
                    mesh_uri,
                    checksum,
                    properties_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    mesh_version_id,
                    payload["basin_version_id"],
                    payload["version_label"],
                    payload["mesh_uri"],
                    payload.get("checksum"),
                    self._json(payload.get("properties_json") or {}),
                ),
            )
            return dict(cursor.fetchone())

    def create_model(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            network = self._fetch_optional(
                cursor,
                """
                SELECT basin_version_id
                FROM core.river_network_version
                WHERE river_network_version_id = %s
                """,
                (payload["river_network_version_id"],),
            )
            if network is None:
                raise InvalidReferenceError(
                    f"river_network_version_id does not exist: {payload['river_network_version_id']}"
                )
            if network["basin_version_id"] != payload["basin_version_id"]:
                raise InvalidReferenceError("river_network_version_id does not belong to basin_version_id.")
            mesh = self._fetch_optional(
                cursor,
                """
                SELECT basin_version_id
                FROM core.mesh_version
                WHERE mesh_version_id = %s
                """,
                (payload["mesh_version_id"],),
            )
            if mesh is None:
                raise InvalidReferenceError(f"mesh_version_id does not exist: {payload['mesh_version_id']}")
            if mesh["basin_version_id"] != payload["basin_version_id"]:
                raise InvalidReferenceError("mesh_version_id does not belong to basin_version_id.")

            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    rshud_code_version,
                    autoshud_code_version,
                    container_image,
                    model_package_uri,
                    active_flag,
                    resource_profile
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    payload["model_id"],
                    payload["basin_version_id"],
                    payload["river_network_version_id"],
                    payload["mesh_version_id"],
                    payload["calibration_version_id"],
                    payload["shud_code_version"],
                    payload.get("rshud_code_version"),
                    payload.get("autoshud_code_version"),
                    payload.get("container_image"),
                    payload["model_package_uri"],
                    bool(payload.get("active_flag", False)),
                    self._json(payload.get("resource_profile") or {}),
                ),
            )
            return dict(cursor.fetchone())

    def set_model_active(self, model_id: str, active: bool) -> dict[str, Any]:
        with self._transaction() as cursor:
            current = self._fetch_optional(
                cursor,
                """
                SELECT *
                FROM core.model_instance
                WHERE model_id = %s
                FOR UPDATE
                """,
                (model_id,),
            )
            if current is None:
                raise MissingResourceError(f"model_id not found: {model_id}")
            if bool(current["active_flag"]) == active:
                state = "active" if active else "inactive"
                raise DuplicateResourceError(f"model_id {model_id} is already {state}.")
            cursor.execute(
                """
                UPDATE core.model_instance
                SET active_flag = %s
                WHERE model_id = %s
                RETURNING *
                """,
                (active, model_id),
            )
            updated = dict(cursor.fetchone())
            self._insert_model_activation_audit(
                cursor,
                current=current,
                updated=updated,
                active=active,
            )
            return updated

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if basin_version_id is not None:
            clauses.append("basin_version_id = %s")
            parameters.append(basin_version_id)
        if active is not None:
            clauses.append("active_flag = %s")
            parameters.append(active)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._transaction() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS total FROM core.model_instance {where}", tuple(parameters))
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"""
                SELECT *
                FROM core.model_instance
                {where}
                ORDER BY created_at DESC, model_id
                LIMIT %s OFFSET %s
                """,
                tuple([*parameters, limit, offset]),
            )
            items = [dict(row) for row in cursor.fetchall()]
        return {"total": total, "items": items, "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                """
                SELECT
                    mi.*,
                    b.basin_id,
                    b.basin_name,
                    rnv.segment_count,
                    mv.mesh_uri,
                    mv.checksum AS mesh_checksum,
                    mv.properties_json AS mesh_properties_json
                FROM core.model_instance mi
                JOIN core.basin_version bv
                  ON bv.basin_version_id = mi.basin_version_id
                JOIN core.basin b
                  ON b.basin_id = bv.basin_id
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = mi.river_network_version_id
                LEFT JOIN core.mesh_version mv
                  ON mv.mesh_version_id = mi.mesh_version_id
                WHERE mi.model_id = %s
                """,
                (model_id,),
            )
        if row is None:
            raise MissingResourceError(f"model_id not found: {model_id}")
        return _model_asset_detail(row)

    def create_crosswalk_entries(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        entries = list(payload.get("entries") or [])
        if not entries:
            raise InvalidPayloadError("entries must not be empty.")
        rows = [
            (
                payload["river_network_version_id"],
                entry["river_segment_id"],
                entry["source"],
                entry["external_id"],
                self._json(entry.get("properties_json") or {}),
            )
            for entry in entries
        ]
        with self._transaction() as cursor:
            inserted = self._execute_values(
                cursor,
                """
                INSERT INTO core.river_segment_crosswalk (
                    river_network_version_id,
                    river_segment_id,
                    source,
                    external_id,
                    properties_json
                )
                VALUES %s
                ON CONFLICT (river_network_version_id, river_segment_id, source)
                DO UPDATE SET external_id = EXCLUDED.external_id, properties_json = EXCLUDED.properties_json
                RETURNING river_network_version_id, river_segment_id, source, external_id, properties_json
                """,
                rows,
                fetch=True,
            )
        items = [dict(row) for row in inserted]
        return {"count": len(items), "items": items}

    def _insert_basin_version(
        self,
        cursor: Any,
        *,
        basin_id: str,
        basin_version_id: str,
        payload: Mapping[str, Any],
        geom_wkt: str,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id,
                basin_id,
                version_label,
                geom,
                active_flag,
                valid_from,
                valid_to,
                source_uri,
                checksum
            )
            VALUES (%s, %s, %s, ST_GeomFromText(%s, 4490), %s, %s, %s, %s, %s)
            RETURNING
                basin_version_id,
                basin_id,
                version_label,
                ST_AsGeoJSON(geom)::json AS geom,
                active_flag,
                valid_from,
                valid_to,
                source_uri,
                checksum,
                created_at
            """,
            (
                basin_version_id,
                basin_id,
                payload["version_label"],
                geom_wkt,
                bool(payload.get("active_flag", False)),
                payload.get("valid_from"),
                payload.get("valid_to"),
                payload.get("source_uri"),
                payload.get("checksum"),
            ),
        )
        return dict(cursor.fetchone())

    def _exists(self, cursor: Any, table: str, column: str, value: str) -> bool:
        cursor.execute(f"SELECT 1 FROM {table} WHERE {column} = %s", (value,))
        return cursor.fetchone() is not None

    def _fetch_optional(self, cursor: Any, statement: str, parameters: Sequence[Any]) -> dict[str, Any] | None:
        cursor.execute(statement, tuple(parameters))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def _json(self, value: Mapping[str, Any]) -> Any:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error
        return Json(dict(value))

    def _execute_values(
        self,
        cursor: Any,
        statement: str,
        rows: Sequence[Sequence[Any]],
        *,
        template: str | None = None,
        fetch: bool = False,
    ) -> list[Any]:
        try:
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error
        result = execute_values(cursor, statement, rows, template=template, page_size=1000, fetch=fetch)
        return list(result or [])

    def _insert_model_activation_audit(
        self,
        cursor: Any,
        *,
        current: Mapping[str, Any],
        updated: Mapping[str, Any],
        active: bool,
    ) -> None:
        details = {
            "previous_active": bool(current["active_flag"]),
            "active": bool(active),
            "basin_version_id": updated["basin_version_id"],
            "river_network_version_id": updated["river_network_version_id"],
            "mesh_version_id": updated["mesh_version_id"],
            "model_package_uri": _sanitize_audit_uri(updated["model_package_uri"]),
        }
        basins_lineage = _basins_lineage_details(updated.get("resource_profile"))
        if basins_lineage:
            details["basins_lineage"] = basins_lineage
        cursor.execute(
            """
            INSERT INTO ops.audit_log (
                actor,
                actor_role,
                action,
                entity_type,
                entity_id,
                details
            )
            VALUES (%s, %s, 'model_instance.active.set', 'model_instance', %s, %s)
            """,
            (
                self.audit_actor,
                self.audit_actor_role,
                updated["model_id"],
                self._json(details),
            ),
        )

    def _transaction(self) -> Any:
        return _PsycopgTransaction(self.database_url)


BASINS_AUDIT_LINEAGE_KEYS = (
    "basin_slug",
    "shud_input_name",
    "manifest_uri",
    "package_checksum",
    "source_inventory_checksum",
)
BASINS_AUDIT_LINEAGE_URI_KEYS = frozenset({"manifest_uri"})


def _sanitize_audit_uri(value: Any) -> str:
    parsed = urlsplit(str(value))
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _basins_lineage_details(resource_profile: Any) -> dict[str, Any]:
    if isinstance(resource_profile, str):
        try:
            resource_profile = json.loads(resource_profile)
        except json.JSONDecodeError:
            return {}
    if not isinstance(resource_profile, Mapping):
        return {}
    details: dict[str, Any] = {}
    for key in BASINS_AUDIT_LINEAGE_KEYS:
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        details[key] = _sanitize_audit_uri(value) if key in BASINS_AUDIT_LINEAGE_URI_KEYS else value
    return details


MODEL_ASSET_LINEAGE_KEYS = (
    "manifest_uri",
    "source_inventory_checksum",
    "basin_slug",
    "shud_input_name",
    "package_checksum",
    "source_path",
    "resolved_source_path",
    "source_uri",
    "source_is_symlink",
)
MODEL_ASSET_URI_KEYS = frozenset(
    {
        "manifest_uri",
        "mesh_uri",
        "model_package_uri",
        "source_uri",
    }
)
MODEL_ASSET_URI_OR_PATH_KEYS = frozenset({"source_path", "resolved_source_path"})


def _model_asset_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    resource_profile = _json_mapping(detail.get("resource_profile"))
    mesh_properties = _json_mapping(detail.pop("mesh_properties_json", None))

    for key in MODEL_ASSET_LINEAGE_KEYS:
        detail[key] = _first_non_empty(resource_profile.get(key), mesh_properties.get(key))
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_audit_uri(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, "") and urlsplit(str(detail[key])).scheme:
            detail[key] = _sanitize_audit_uri(detail[key])

    model_name = _first_non_empty(
        resource_profile.get("model_name"),
        resource_profile.get("shud_input_name"),
        detail.get("model_id"),
    )
    detail["model_name"] = str(model_name) if model_name is not None else None
    detail["segment_count"] = int(detail["segment_count"]) if detail.get("segment_count") is not None else None
    return detail


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


class _PsycopgTransaction:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: Any | None = None

    def __enter__(self) -> Any:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error

        self.psycopg2 = psycopg2
        self.connection = psycopg2.connect(self.database_url)
        self.connection.autocommit = False
        register_default_json(loads=json.loads, conn_or_curs=self.connection)
        register_default_jsonb(loads=json.loads, conn_or_curs=self.connection)
        return self.connection.cursor(cursor_factory=RealDictCursor)

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: Any) -> bool:
        if self.connection is None:
            return False
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
                if isinstance(exc, ModelRegistryError):
                    return False
                self._raise_mapped_database_error(exc)
        finally:
            self.connection.close()
        return False

    def _raise_mapped_database_error(self, exc: BaseException | None) -> None:
        if exc is None:
            return
        error_code = getattr(exc, "pgcode", None)
        if error_code == "23505":
            raise DuplicateResourceError(str(exc)) from exc
        if error_code in {"23503", "22P02"}:
            raise InvalidReferenceError(str(exc)) from exc
        if error_code in {"XX000", "22023"}:
            raise InvalidPayloadError(str(exc)) from exc
        raise ModelRegistryError(f"Model registry database operation failed: {exc}") from exc
