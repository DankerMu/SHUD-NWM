from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from apps.api.auth import (
    PolicyDecision,
    audit_record,
    redact_audit_payload,
    require_policy_evidence,
    trusted_internal_policy_decision,
)


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


ModelLifecycleState = Literal["inactive", "active", "deprecated", "superseded"]
ModelLifecycleOperation = Literal[
    "activate",
    "deactivate",
    "switch_version",
    "rollback_version",
    "supersede",
    "deprecate",
]

MODEL_LIFECYCLE_STATES: tuple[ModelLifecycleState, ...] = ("inactive", "active", "deprecated", "superseded")
MODEL_LIFECYCLE_ACTIONS: dict[str, str] = {
    "activate": "models.activate",
    "deactivate": "models.deactivate",
    "switch_version": "models.switch_version",
    "rollback_version": "models.rollback_version",
    "supersede": "models.supersede",
    "deprecate": "models.deactivate",
}


SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES = 10_000
SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS = 3
RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES = SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES
RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS = SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS
RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES = 50_000
RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES = 1_000_000
RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES = 250_000


class RiverSegmentGeoJsonBudgetError(ModelRegistryError):
    """Raised when a river segment GeoJSON response exceeds the server serialization budget."""

    def __init__(
        self,
        *,
        limit_type: str,
        max_bytes: int,
        serialized_bytes: int,
        scope: str,
    ) -> None:
        super().__init__("River segment GeoJSON payload budget exceeded.")
        self.limit_type = limit_type
        self.max_bytes = max_bytes
        self.serialized_bytes = serialized_bytes
        self.scope = scope


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

    def create_basin_with_version(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="basins",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
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

    def create_basin_version(
        self,
        basin_id: str,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id=basin_id,
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
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

    def create_river_network(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="river-networks",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
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
                WITH matching AS (
                    SELECT rs.river_segment_id, rs.segment_order, rs.geom
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE {where_clause}
                ),
                ordered_renderable AS (
                    SELECT
                        river_segment_id,
                        SUM(ST_NPoints(geom)) OVER (
                            ORDER BY COALESCE(segment_order, 2147483647), river_segment_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS running_coordinate_count
                    FROM matching
                    WHERE geom IS NOT NULL
                      AND ST_NPoints(geom) BETWEEN 2 AND %s
                      AND ST_NDims(geom) <= %s
                )
                SELECT
                    (SELECT COUNT(*) FROM matching) AS total,
                    COUNT(*) FILTER (WHERE running_coordinate_count <= %s) AS feature_total
                FROM ordered_renderable
                """,
                tuple([
                    *params,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
                    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                ]),
            )
            counts = cursor.fetchone()
            total = int(counts["total"])
            feature_total = int(counts["feature_total"])
            cursor.execute(
                f"""
                WITH ordered_renderable AS (
                    SELECT
                        rs.river_segment_id,
                        rs.river_network_version_id,
                        rnv.basin_version_id,
                        rs.segment_order,
                        rs.downstream_segment_id,
                        rs.length_m,
                        rs.properties_json,
                        rs.geom,
                        ST_NPoints(rs.geom) AS coordinate_count,
                        SUM(ST_NPoints(rs.geom)) OVER (
                            ORDER BY COALESCE(rs.segment_order, 2147483647), rs.river_segment_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS running_coordinate_count
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE {where_clause}
                      AND rs.geom IS NOT NULL
                      AND ST_NPoints(rs.geom) BETWEEN 2 AND %s
                      AND ST_NDims(rs.geom) <= %s
                ),
                renderable AS (
                    SELECT *
                    FROM ordered_renderable
                    WHERE running_coordinate_count <= %s
                )
                SELECT
                    river_segment_id,
                    river_network_version_id,
                    basin_version_id,
                    segment_order,
                    downstream_segment_id,
                    length_m,
                    properties_json,
                    ST_AsGeoJSON(geom)::json AS geometry
                FROM renderable
                ORDER BY COALESCE(segment_order, 2147483647), river_segment_id
                LIMIT %s OFFSET %s
                """,
                tuple([
                    *params,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
                    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                    limit,
                    offset,
                ]),
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

        collection = {
            "type": "FeatureCollection",
            "features": features,
            "total": total,
            "feature_total": feature_total,
            "limit": limit,
            "offset": offset,
        }
        _enforce_river_segment_serialized_budget(
            collection,
            max_bytes=RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
            scope="collection",
        )
        return collection

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                """
                WITH selected AS (
                    SELECT
                        rs.river_segment_id,
                        rs.river_network_version_id,
                        rs.segment_order,
                        rs.downstream_segment_id,
                        rs.length_m,
                        rs.geom,
                        rs.properties_json,
                        rs.created_at,
                        ST_NPoints(rs.geom) AS coordinate_count,
                        ST_NDims(rs.geom) AS coordinate_dimensions
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE rnv.basin_version_id = %s
                      AND rs.river_segment_id = %s
                      AND rs.river_network_version_id = %s
                )
                SELECT
                    river_segment_id,
                    river_network_version_id,
                    segment_order,
                    downstream_segment_id,
                    length_m,
                    ST_AsGeoJSON(geom)::json AS geom,
                    properties_json,
                    created_at
                FROM selected
                WHERE geom IS NOT NULL
                  AND coordinate_count BETWEEN 2 AND %s
                  AND coordinate_dimensions <= %s
                """,
                (
                    basin_version_id,
                    segment_id,
                    river_network_version_id,
                    SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES,
                    SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
                ),
            )
        if row is None:
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        detail = _river_segment_detail(row)
        _enforce_river_segment_serialized_budget(
            detail,
            max_bytes=RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
            scope="detail",
        )
        return detail

    def create_mesh_version(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="mesh-versions",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
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

    def create_model(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="models",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        if bool(payload.get("active_flag", False)):
            raise InvalidPayloadError(
                "active_flag=true is not accepted when creating models; use a lifecycle activate operation."
            )
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
                    lifecycle_state,
                    resource_profile
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    False,
                    "inactive",
                    self._json(payload.get("resource_profile") or {}),
                ),
            )
            return dict(cursor.fetchone())

    def set_model_active(
        self,
        model_id: str,
        active: bool,
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.model_lifecycle_operation(
            model_id,
            operation="activate" if active else "deactivate",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
            request_id=request_id,
        )
        return result["model"]

    def preflight_model_operation(
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: PolicyDecision | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if operation not in MODEL_LIFECYCLE_ACTIONS:
            raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")
        action_id = MODEL_LIFECYCLE_ACTIONS[operation]
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_instance",
            target_id=model_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)
        request_id = request_id or str(uuid4())
        with self._transaction() as cursor:
            model = self._fetch_model_lifecycle_row(cursor, model_id, for_update=False)
            if model is None:
                raise MissingResourceError(f"model_id not found: {model_id}")
            active = self._fetch_active_model_for_scope(cursor, str(model["basin_version_id"]), for_update=False)
            previous = (
                self._fetch_model_lifecycle_row(cursor, previous_model_id, for_update=False)
                if previous_model_id is not None
                else None
            )
            if previous_model_id is not None and previous is None:
                raise MissingResourceError(f"model_id not found: {previous_model_id}")
            history = self._fetch_trustworthy_rollback_history(
                cursor,
                current_model=model,
                previous_model_id=previous_model_id,
            )
        return self._build_model_operation_preflight(
            model=model,
            current_active=active,
            operation=operation,
            action_id=action_id,
            actor_id=decision.actor_id,
            request_id=request_id,
            previous_model=previous,
            rollback_history=history,
            override_missing_active=override_missing_active,
            reason=reason,
            actor_roles=decision.roles,
        )

    def model_lifecycle_operation(
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
        request_id: str | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if operation not in MODEL_LIFECYCLE_ACTIONS:
            raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")
        action_id = MODEL_LIFECYCLE_ACTIONS[operation]
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                action_id,
                target_type="model_instance",
                target_id=model_id,
                actor_id="trusted-internal:model-registry",
                roles=("sys_admin",),
            )
            if operation == "deactivate":
                override_missing_active = True
                reason = reason or "trusted internal legacy deactivation"
        request_id = request_id or str(uuid4())
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_instance",
            target_id=model_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)

        with self._transaction() as cursor:
            unlocked_model = self._fetch_model_lifecycle_row(cursor, model_id, for_update=False)
            if unlocked_model is None:
                raise MissingResourceError(f"model_id not found: {model_id}")
            self._lock_basin_version_scope(cursor, str(unlocked_model["basin_version_id"]))
            unlocked_current_active = self._fetch_active_model_for_scope(
                cursor,
                str(unlocked_model["basin_version_id"]),
                for_update=False,
            )
            unlocked_previous = (
                self._fetch_model_lifecycle_row(cursor, previous_model_id, for_update=False)
                if previous_model_id is not None
                else None
            )
            if previous_model_id is not None and unlocked_previous is None:
                raise MissingResourceError(f"model_id not found: {previous_model_id}")
            lock_ids = {
                str(unlocked_model["model_id"]),
                *(
                    [str(unlocked_current_active["model_id"])]
                    if unlocked_current_active is not None
                    else []
                ),
                *([str(unlocked_previous["model_id"])] if unlocked_previous is not None else []),
            }
            locked_rows: dict[str, dict[str, Any]] = {}
            for locked_model_id in sorted(lock_ids):
                locked = self._fetch_model_lifecycle_row(cursor, locked_model_id, for_update=True)
                if locked is None:
                    raise MissingResourceError(f"model_id not found: {locked_model_id}")
                locked_rows[locked_model_id] = locked
            model = locked_rows[str(unlocked_model["model_id"])]
            current_active = (
                locked_rows.get(str(unlocked_current_active["model_id"]))
                if unlocked_current_active is not None
                else None
            )
            previous = locked_rows.get(str(unlocked_previous["model_id"])) if unlocked_previous is not None else None
            rollback_history = self._fetch_trustworthy_rollback_history(
                cursor,
                current_model=model,
                previous_model_id=previous_model_id,
            )
            preflight = self._build_model_operation_preflight(
                model=model,
                current_active=current_active,
                operation=operation,
                action_id=action_id,
                actor_id=decision.actor_id,
                request_id=request_id,
                previous_model=previous,
                rollback_history=rollback_history,
                override_missing_active=override_missing_active,
                reason=reason,
                actor_roles=decision.roles,
            )
            if preflight["status"] == "blocked":
                audit_id = self._insert_model_lifecycle_audit(
                    cursor,
                    model=model,
                    updated=model,
                    operation=operation,
                    outcome="blocked",
                    policy_decision=decision,
                    request_id=request_id,
                    preflight=preflight,
                    previous_model=current_active,
                    reason=reason,
                )
                return {
                    "status": "blocked",
                    "operation": operation,
                    "model": _model_public_projection(model),
                    "preflight": preflight,
                    "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": audit_id},
                }

            transition = self._apply_model_lifecycle_transition(
                cursor,
                model=model,
                current_active=current_active,
                operation=operation,
                previous_model=previous,
            )
            audit_id = self._insert_model_lifecycle_audit(
                cursor,
                model=model,
                updated=transition["model"],
                operation=operation,
                outcome=transition["outcome"],
                policy_decision=decision,
                request_id=request_id,
                preflight=preflight,
                previous_model=transition.get("previous_model"),
                reason=reason,
            )
            return {
                "status": transition["outcome"],
                "operation": operation,
                "model": _model_public_projection(transition["model"]),
                "previous_model": (
                    _model_public_projection(transition["previous_model"]) if transition.get("previous_model") else None
                ),
                "preflight": preflight,
                "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": audit_id},
            }

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
            items = [_model_public_projection(row) for row in cursor.fetchall()]
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

    def create_crosswalk_entries(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="river-segment-crosswalks",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
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

    def _require_m17_registry_admin_write_policy(
        self,
        *,
        target_id: str,
        policy_decision: PolicyDecision | None,
        trusted_internal: bool,
    ) -> PolicyDecision:
        # M17 has no finer-grained create action ids for registry-admin writes.
        # Until M18 lifecycle actions land, route and direct writes must present
        # the canonical models.switch_version decision for their route target.
        action_id = "models.switch_version"
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                action_id,
                target_type="model_registry",
                target_id=target_id,
                actor_id="trusted-internal:model-registry",
                roles=("sys_admin",),
            )
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_registry",
            target_id=target_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)
        return decision

    def _fetch_model_lifecycle_row(self, cursor: Any, model_id: str, *, for_update: bool) -> dict[str, Any] | None:
        lock_clause = "FOR UPDATE" if for_update else ""
        return self._fetch_optional(
            cursor,
            f"""
            SELECT
                mi.*,
                COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
                b.basin_id,
                b.basin_name,
                bv.checksum AS basin_checksum,
                rnv.segment_count,
                rnv.checksum AS river_network_checksum,
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
            JOIN core.mesh_version mv
              ON mv.mesh_version_id = mi.mesh_version_id
            WHERE mi.model_id = %s
            {lock_clause}
            """,
            (model_id,),
        )

    def _fetch_active_model_for_scope(
        self,
        cursor: Any,
        basin_version_id: str,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        lock_clause = "FOR UPDATE" if for_update else ""
        return self._fetch_optional(
            cursor,
            f"""
            SELECT
                mi.*,
                COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state
            FROM core.model_instance mi
            WHERE mi.basin_version_id = %s
              AND mi.active_flag = true
              AND COALESCE(mi.lifecycle_state, 'active') = 'active'
            ORDER BY mi.created_at DESC, mi.model_id
            LIMIT 1
            {lock_clause}
            """,
            (basin_version_id,),
        )

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:
        cursor.execute(
            """
            SELECT basin_version_id
            FROM core.basin_version
            WHERE basin_version_id = %s
            FOR UPDATE
            """,
            (basin_version_id,),
        )
        if cursor.fetchone() is None:
            raise InvalidReferenceError(f"basin_version_id does not exist: {basin_version_id}")

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,
        *,
        current_model: Mapping[str, Any],
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        if previous_model_id is None:
            return None
        row = self._fetch_optional(
            cursor,
            """
            SELECT log_id, action, entity_id, details, created_at
            FROM ops.audit_log
            WHERE entity_type = 'model_instance'
              AND action IN ('models.activate', 'models.switch_version', 'models.rollback_version')
              AND details->>'operation' IN ('activate', 'switch_version', 'rollback_version')
              AND details->>'outcome' IN ('allowed', 'rollback')
              AND details->>'basin_version_id' = %s
              AND (
                entity_id = %s
                OR details->'updated_model'->>'model_id' = %s
              )
            ORDER BY created_at DESC, log_id DESC
            LIMIT 1
            """,
            (
                str(current_model["basin_version_id"]),
                str(current_model["model_id"]),
                str(current_model["model_id"]),
            ),
        )
        if row is None:
            return None
        details = _json_mapping(row.get("details"))
        previous_ref = _json_mapping(details.get("previous_model"))
        new_state = _json_mapping(details.get("new_state"))
        updated_ref = _json_mapping(details.get("updated_model"))
        made_current_active = (
            str(row.get("entity_id")) == str(current_model["model_id"])
            or str(updated_ref.get("model_id")) == str(current_model["model_id"])
        )
        trusted = (
            made_current_active
            and str(previous_ref.get("model_id")) == str(previous_model_id)
            and str(details.get("basin_version_id")) == str(current_model.get("basin_version_id"))
            and bool(new_state.get("active")) is True
            and str(new_state.get("lifecycle_state")) == "active"
            and bool(current_model.get("active_flag")) is True
            and _canonical_lifecycle_state(current_model) == "active"
        )
        row["trusted"] = trusted
        row["prior_audit_log_id"] = row.get("log_id")
        row["matched_previous_model_id"] = previous_ref.get("model_id")
        if not trusted:
            row["stale_reason"] = "latest_current_epoch_previous_mismatch"
        return row

    def _build_model_operation_preflight(
        self,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        operation: ModelLifecycleOperation,
        action_id: str,
        actor_id: str,
        request_id: str,
        previous_model: Mapping[str, Any] | None,
        rollback_history: Mapping[str, Any] | None,
        override_missing_active: bool,
        reason: str | None,
        actor_roles: Sequence[str],
    ) -> dict[str, Any]:
        resource_profile = _json_mapping(model.get("resource_profile"))
        mesh_properties = _json_mapping(model.get("mesh_properties_json"))
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        lifecycle_state = _canonical_lifecycle_state(model)
        activation_class_operation = operation in {"activate", "switch_version", "rollback_version"}

        if model.get("river_network_version_id") and model.get("basin_version_id") is None:
            blockers.append(
                _preflight_blocker("LINEAGE_MISSING_BASIN_VERSION", "Model lineage is missing basin version.")
            )
        if model.get("mesh_version_id") in (None, ""):
            blockers.append(
                _preflight_blocker("LINEAGE_MISSING_MESH_VERSION", "Model lineage is missing mesh version.")
            )
        if model.get("model_package_uri") in (None, ""):
            blockers.append(_preflight_blocker("PACKAGE_URI_MISSING", "Model package URI is missing."))
        package_checksum = _first_non_empty(resource_profile.get("package_checksum"), model.get("package_checksum"))
        if package_checksum in (None, ""):
            evidence = _preflight_blocker if activation_class_operation else _preflight_warning
            (blockers if activation_class_operation else warnings).append(
                evidence("PACKAGE_CHECKSUM_MISSING", "Package checksum evidence is not available.")
            )
        elif (
            activation_class_operation
            and _package_checksum_verification_status(resource_profile, package_checksum) == "blocked"
        ):
            blockers.append(
                _preflight_blocker(
                    "PACKAGE_CHECKSUM_UNVERIFIED",
                    "Package checksum evidence could not be reread from stored package evidence.",
                )
            )
        if _object_uri_prefix_status(model.get("model_package_uri")) == "invalid":
            blockers.append(
                _preflight_blocker("OBJECT_URI_PREFIX_INVALID", "Model package URI prefix is not supported.")
            )
        copied_root = _copied_root_status(resource_profile)
        if copied_root == "unsafe":
            blockers.append(_preflight_blocker("COPIED_ROOT_UNSAFE", "Copied-root source evidence is unsafe."))
        elif copied_root == "missing":
            warnings.append(
                {"code": "COPIED_ROOT_EVIDENCE_MISSING", "message": "Copied-root evidence is not available."}
            )
        if activation_class_operation and _has_unsafe_source_root(resource_profile):
            blockers.append(
                _preflight_blocker("SOURCE_ROOT_UNSAFE", "Model source root evidence points to an unsafe local source.")
            )

        current_active_id = str(current_active["model_id"]) if current_active else None
        if invalid_transition := _transition_blocker(
            operation=operation,
            lifecycle_state=lifecycle_state,
            model_id=str(model["model_id"]),
            current_active_id=current_active_id,
        ):
            blockers.append(invalid_transition)
        if operation in {"activate", "switch_version"} and current_active_id == model["model_id"]:
            warnings.append({"code": "ALREADY_CURRENT", "message": "Model is already the active model for this scope."})
        if operation == "switch_version" and current_active_id is None:
            blockers.append(
                _preflight_blocker("SWITCH_REQUIRES_CURRENT_ACTIVE", "Version switch requires current active model.")
            )
        removes_current_active = (
            bool(model.get("active_flag"))
            and current_active_id == model["model_id"]
            and operation in {"deactivate", "supersede", "deprecate"}
        )
        operation_supports_missing_active_override = operation == "deactivate"
        if removes_current_active and (not override_missing_active or not operation_supports_missing_active_override):
            blockers.append(
                _preflight_blocker(
                    "MISSING_ACTIVE_RISK",
                    "Operation would leave this basin version without an active model.",
                )
            )
        if (
            operation == "deactivate"
            and removes_current_active
            and override_missing_active
            and not str(reason or "").strip()
        ):
            blockers.append(_preflight_blocker("OVERRIDE_REASON_REQUIRED", "Override requires a non-empty reason."))
        if (
            operation == "deactivate"
            and removes_current_active
            and override_missing_active
            and actor_roles
            and "sys_admin" not in actor_roles
        ):
            blockers.append(
                _preflight_blocker("OVERRIDE_REQUIRES_SYS_ADMIN", "Missing-active override requires sys_admin.")
            )
        if operation == "rollback_version":
            if current_active_id != model["model_id"]:
                blockers.append(
                    _preflight_blocker("ROLLBACK_CURRENT_STALE", "Rollback target is not the current active model.")
                )
            if previous_model is None:
                blockers.append(_preflight_blocker("ROLLBACK_HISTORY_MISSING", "Rollback requires a prior model id."))
            elif previous_model.get("basin_version_id") != model.get("basin_version_id"):
                blockers.append(_preflight_blocker("ROLLBACK_SCOPE_MISMATCH", "Rollback model scope does not match."))
            if rollback_history is None:
                blockers.append(
                    _preflight_blocker("ROLLBACK_HISTORY_MISSING", "No trustworthy prior active audit history exists.")
                )
            elif not bool(rollback_history.get("trusted")):
                blockers.append(
                    _preflight_blocker(
                        "ROLLBACK_CURRENT_STALE",
                        "Rollback history is stale for the current active epoch.",
                    )
                )

        status = "blocked" if blockers else "ready"
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "request_id": request_id,
            "operation": operation,
            "action_id": action_id,
            "actor_id": actor_id,
            "roles": list(actor_roles),
            "status": status,
            "basin_id": model.get("basin_id"),
            "basin_version_id": model.get("basin_version_id"),
            "model_id": model.get("model_id"),
            "current_active_model_id": current_active_id,
            "previous_model_id": previous_model.get("model_id") if previous_model else None,
            "prior_audit_log_id": rollback_history.get("prior_audit_log_id") if rollback_history else None,
            "rollback_history": _rollback_history_preflight_reference(rollback_history),
            "river_network_version_id": model.get("river_network_version_id"),
            "mesh_version_id": model.get("mesh_version_id"),
            "lineage": redact_audit_payload(
                {
                    "package_checksum": _first_non_empty(
                        resource_profile.get("package_checksum"),
                        model.get("package_checksum"),
                    ),
                    "source_inventory_checksum": resource_profile.get("source_inventory_checksum"),
                    "mesh_checksum": model.get("mesh_checksum"),
                    "river_network_checksum": model.get("river_network_checksum"),
                    "basin_checksum": model.get("basin_checksum"),
                    "object_uri": _sanitize_audit_uri(model.get("model_package_uri")),
                    "manifest_uri": _sanitize_audit_uri(resource_profile.get("manifest_uri"))
                    if resource_profile.get("manifest_uri")
                    else None,
                    "copied_root_status": copied_root,
                    "mesh_properties": mesh_properties,
                }
            ),
            "object_uri_prefix": {
                "status": _object_uri_prefix_status(model.get("model_package_uri")),
                "uri": _sanitize_audit_uri(model.get("model_package_uri")),
            },
            "impact": {
                "downstream_surfaces": ["forecast-routing", "model-assets-api", "operator-audit"],
                "segment_count": int(model["segment_count"]) if model.get("segment_count") is not None else None,
                "active_scope": {
                    "basin_id": model.get("basin_id"),
                    "basin_version_id": model.get("basin_version_id"),
                },
            },
            "blockers": blockers,
            "warnings": warnings,
            "override_missing_active": bool(override_missing_active),
            "reason": REDACTED_REASON,
        }

    def _apply_model_lifecycle_transition(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        operation: ModelLifecycleOperation,
        previous_model: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        lifecycle_state = str(model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive"))
        if operation in {"activate", "switch_version"}:
            if lifecycle_state == "active" and bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
                raise InvalidPayloadError(f"Invalid {operation} transition from {lifecycle_state}.")
            if current_active and current_active["model_id"] != model["model_id"]:
                self._update_model_lifecycle_state(cursor, str(current_active["model_id"]), "superseded")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "active")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "deactivate":
            if lifecycle_state == "inactive" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "inactive")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "supersede":
            if lifecycle_state == "superseded" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"active", "inactive", "deprecated"}:
                raise InvalidPayloadError(f"Invalid supersede transition from {lifecycle_state}.")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "superseded")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "deprecate":
            if lifecycle_state == "deprecated" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"inactive", "superseded"}:
                raise InvalidPayloadError(f"Invalid deprecate transition from {lifecycle_state}.")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "deprecated")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "rollback_version":
            if previous_model is None:
                raise InvalidPayloadError("previous_model_id is required for rollback_version.")
            previous_state = _canonical_lifecycle_state(previous_model)
            if previous_state not in {"inactive", "superseded"}:
                raise InvalidPayloadError(f"Invalid rollback_version transition from previous {previous_state}.")
            self._update_model_lifecycle_state(cursor, str(model["model_id"]), "superseded")
            updated = self._update_model_lifecycle_state(cursor, str(previous_model["model_id"]), "active")
            return {"outcome": "rollback", "model": updated, "previous_model": model}
        raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")

    def _update_model_lifecycle_state(
        self,
        cursor: Any,
        model_id: str,
        lifecycle_state: ModelLifecycleState,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            UPDATE core.model_instance
            SET lifecycle_state = %s,
                active_flag = %s
            WHERE model_id = %s
            RETURNING *
            """,
            (lifecycle_state, lifecycle_state == "active", model_id),
        )
        return dict(cursor.fetchone())

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: PolicyDecision,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        details = audit_record(
            policy_decision,
            request_id=request_id,
            previous_state={
                "active": bool(model.get("active_flag")),
                "lifecycle_state": model.get("lifecycle_state"),
            },
            new_state={
                "active": bool(updated.get("active_flag")),
                "lifecycle_state": updated.get("lifecycle_state"),
            },
            payload={
                "operation": operation,
                "outcome": outcome,
                "basin_id": model.get("basin_id"),
                "basin_version_id": model.get("basin_version_id"),
                "river_network_version_id": model.get("river_network_version_id"),
                "mesh_version_id": model.get("mesh_version_id"),
                "model_package_uri": _sanitize_audit_uri(model.get("model_package_uri")),
                "reason": REDACTED_REASON if reason else None,
                "preflight": preflight,
                "previous_model": _model_audit_reference(previous_model),
                "updated_model": _model_audit_reference(updated),
                "prior_audit_log_id": preflight.get("prior_audit_log_id"),
            },
        )
        details.update(
            {
                "operation": operation,
                "outcome": outcome,
                "basin_id": model.get("basin_id"),
                "basin_version_id": model.get("basin_version_id"),
                "river_network_version_id": model.get("river_network_version_id"),
                "mesh_version_id": model.get("mesh_version_id"),
                "model_package_uri": _sanitize_audit_uri(model.get("model_package_uri")),
                "previous_model": _model_audit_reference(previous_model),
                "updated_model": _model_audit_reference(updated),
                "prior_audit_log_id": preflight.get("prior_audit_log_id"),
                "preflight": preflight,
                "reason": REDACTED_REASON if reason else None,
            }
        )
        details = redact_audit_payload(details)
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
            VALUES (%s, %s, %s, 'model_instance', %s, %s)
            RETURNING log_id
            """,
            (
                policy_decision.actor_id,
                ",".join(policy_decision.roles),
                policy_decision.action_id,
                model["model_id"],
                self._json(details),
            ),
        )
        return int(cursor.fetchone()["log_id"])

    def _insert_model_activation_audit(
        self,
        cursor: Any,
        *,
        current: Mapping[str, Any],
        updated: Mapping[str, Any],
        active: bool,
        policy_decision: PolicyDecision,
        request_id: str | None,
    ) -> None:
        details = audit_record(
            policy_decision,
            request_id=request_id,
            previous_state={"active": bool(current["active_flag"])},
            new_state={"active": bool(active)},
            payload={
                "basin_version_id": updated["basin_version_id"],
                "river_network_version_id": updated["river_network_version_id"],
                "mesh_version_id": updated["mesh_version_id"],
                "model_package_uri": _sanitize_audit_uri(updated["model_package_uri"]),
            },
        )
        details.update(
            {
            "previous_active": bool(current["active_flag"]),
            "active": bool(active),
            "basin_version_id": updated["basin_version_id"],
            "river_network_version_id": updated["river_network_version_id"],
            "mesh_version_id": updated["mesh_version_id"],
            "model_package_uri": _sanitize_audit_uri(updated["model_package_uri"]),
            }
        )
        basins_lineage = _basins_lineage_details(updated.get("resource_profile"))
        if basins_lineage:
            details["basins_lineage"] = basins_lineage
        details = redact_audit_payload(details)
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
            VALUES (%s, %s, %s, 'model_instance', %s, %s)
            """,
            (
                policy_decision.actor_id,
                ",".join(policy_decision.roles),
                policy_decision.action_id,
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


def _sanitize_audit_uri(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = urlsplit(str(value))
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_uri_like(value: Any) -> bool:
    parsed = urlsplit(str(value))
    return bool(parsed.scheme or parsed.netloc)


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
REDACTED_REASON = "[redacted]"
SUPPORTED_OBJECT_URI_SCHEMES = frozenset({"s3", "az", "gs", "https", "http", "integration", "memory"})


def _model_asset_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    resource_profile = _json_mapping(detail.get("resource_profile"))
    mesh_properties = _json_mapping(detail.pop("mesh_properties_json", None))
    detail["resource_profile"] = _sanitize_public_json_value(resource_profile)
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )

    for key in MODEL_ASSET_LINEAGE_KEYS:
        detail[key] = _first_non_empty(resource_profile.get(key), mesh_properties.get(key))
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_audit_uri(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, "") and _is_uri_like(detail[key]):
            detail[key] = _sanitize_audit_uri(detail[key])

    model_name = _first_non_empty(
        resource_profile.get("model_name"),
        resource_profile.get("shud_input_name"),
        detail.get("model_id"),
    )
    detail["model_name"] = str(model_name) if model_name is not None else None
    detail["segment_count"] = int(detail["segment_count"]) if detail.get("segment_count") is not None else None
    return detail


def _river_segment_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["river_segment_id"] = str(detail["river_segment_id"])
    detail["river_network_version_id"] = str(detail["river_network_version_id"])
    detail["segment_order"] = int(detail["segment_order"]) if detail.get("segment_order") is not None else None
    detail["length_m"] = float(detail["length_m"]) if detail.get("length_m") is not None else None
    detail["properties_json"] = _json_mapping(detail.get("properties_json"))
    return detail


def _enforce_river_segment_serialized_budget(payload: Mapping[str, Any], *, max_bytes: int, scope: str) -> None:
    serialized_bytes = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    if serialized_bytes > max_bytes:
        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=max_bytes,
            serialized_bytes=serialized_bytes,
            scope=scope,
        )


def _model_public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["resource_profile"] = _sanitize_public_json_value(_json_mapping(detail.get("resource_profile")))
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_audit_uri(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, "") and _is_uri_like(detail[key]):
            detail[key] = _sanitize_audit_uri(detail[key])
    return detail


def _sanitize_public_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _sanitize_public_json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_public_json_value(child) for child in value]
    if isinstance(value, tuple):
        return [_sanitize_public_json_value(child) for child in value]
    if isinstance(value, str) and _is_uri_like(value):
        return _sanitize_audit_uri(value)
    return value


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


def _preflight_blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _preflight_warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _object_uri_prefix_status(value: Any) -> str:
    if value in (None, ""):
        return "missing"
    parsed = urlsplit(str(value))
    if parsed.scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return "valid"
    return "invalid"


def _copied_root_status(resource_profile: Mapping[str, Any]) -> str:
    copied_root = _first_non_empty(
        resource_profile.get("copied_root"),
        resource_profile.get("copied_root_uri"),
        resource_profile.get("copied_root_status"),
    )
    if copied_root in (None, ""):
        return "missing"
    if str(resource_profile.get("source_is_symlink", "")).lower() == "true":
        return "unsafe"
    text = str(copied_root)
    if text.lower() in {"unsafe", "symlink", "raw", "local"}:
        return "unsafe"
    if text.lower() in {"present", "safe", "copied", "verified"}:
        return "present"
    if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
        return "unsafe"
    return "present"


def _canonical_lifecycle_state(model: Mapping[str, Any]) -> ModelLifecycleState:
    state = str(model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive"))
    if state in MODEL_LIFECYCLE_STATES:
        return state  # type: ignore[return-value]
    return "active" if model.get("active_flag") else "inactive"


def _transition_blocker(
    *,
    operation: ModelLifecycleOperation,
    lifecycle_state: ModelLifecycleState,
    model_id: str,
    current_active_id: str | None,
) -> dict[str, str] | None:
    if operation == "activate":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"activate is not allowed from {lifecycle_state}.")
    elif operation == "switch_version":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"switch_version is not allowed from {lifecycle_state}.")
    elif operation == "deactivate":
        if lifecycle_state not in {"active", "inactive"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deactivate is not allowed from {lifecycle_state}.")
    elif operation == "supersede":
        if lifecycle_state not in {"active", "inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"supersede is not allowed from {lifecycle_state}.")
    elif operation == "deprecate":
        if lifecycle_state not in {"inactive", "superseded", "deprecated"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deprecate is not allowed from {lifecycle_state}.")
    return None


def _package_checksum_verification_status(resource_profile: Mapping[str, Any], package_checksum: Any) -> str:
    verification_fields = (
        resource_profile.get("package_checksum_confirmed_from_stored_manifest"),
        resource_profile.get("package_checksum_verified"),
        resource_profile.get("checksum_reread_verified"),
    )
    if any(value is True for value in verification_fields):
        return "verified"
    if any(value is False for value in verification_fields):
        return "blocked"
    for key in (
        "package_checksum_reread_status",
        "package_checksum_reconstruction_status",
        "checksum_reread_status",
    ):
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        if str(value).lower() in {"verified", "ready", "ok", "confirmed"}:
            return "verified"
        if str(value).lower() in {"blocked", "failed", "unreadable", "missing", "limited", "mismatch"}:
            return "blocked"
    if resource_profile.get("stored_manifest_package_checksum") not in (None, ""):
        return "verified" if resource_profile.get("stored_manifest_package_checksum") == package_checksum else "blocked"
    if resource_profile.get("manifest_uri") not in (None, ""):
        return "verified"
    return "blocked"


def _has_unsafe_source_root(resource_profile: Mapping[str, Any]) -> bool:
    for value in _iter_source_evidence_values(resource_profile):
        if _is_unsafe_source_value(value):
            return True
    return False


def _iter_source_evidence_values(value: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"source_path", "resolved_source_path", "source_uri", "root", "source_root"}:
                values.append(child)
            if isinstance(child, (Mapping, list, tuple)):
                values.extend(_iter_source_evidence_values(child))
    elif isinstance(value, list | tuple):
        for child in value:
            values.extend(_iter_source_evidence_values(child))
    return values


def _is_unsafe_source_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    text = str(value)
    parsed = urlsplit(text)
    path_text = parsed.path if parsed.scheme else text
    normalized = path_text.replace("\\", "/")
    if normalized.startswith("/volume/"):
        return True
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"/", ""}]
    for index, part in enumerate(parts):
        if part == "data" and index + 1 < len(parts) and parts[index + 1] == "Basins":
            return True
        if part == "Basins" and index > 0 and parts[index - 1] == "data":
            return True
    return False


def _model_audit_reference(model: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "model_id": model.get("model_id"),
        "basin_version_id": model.get("basin_version_id"),
        "lifecycle_state": model.get("lifecycle_state"),
        "active_flag": bool(model.get("active_flag")),
    }


def _rollback_history_preflight_reference(history: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if history is None:
        return None
    return {
        "prior_audit_log_id": history.get("prior_audit_log_id") or history.get("log_id"),
        "action": history.get("action"),
        "entity_id": history.get("entity_id"),
        "trusted": bool(history.get("trusted")),
        "matched_previous_model_id": history.get("matched_previous_model_id"),
        "stale_reason": history.get("stale_reason"),
    }


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
