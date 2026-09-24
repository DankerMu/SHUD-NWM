"""Registry catalog methods of ``PsycopgModelRegistryStore``: basin / basin
version / river network / mesh / model / crosswalk registration and reads, the
basin-version scope lock, and the shared cursor helpers (#2617 split of
``packages.common.model_registry``).

A plain mixin: no fields and no dunders. ``PsycopgModelRegistryStore`` in
``packages.common.model_registry`` is the only class that composes it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision
from packages.common.forecast_store import QHH_LATEST_READY_RUN_STATUSES
from packages.common.model_registry_contracts import (
    EVIDENCE_ONLY_BASIN_GROUP,
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    MissingResourceError,
    ModelRegistryError,
    build_versioned_id,
    geometry_to_wkt,
)
from packages.common.model_registry_public import (
    _basin_version_public_projection,
    _model_asset_detail,
    _model_public_projection,
)


class _RegistryCatalogMixin:
    """Basin / network / mesh / model catalog reads and writes."""

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

    def list_basins(
        self, *, limit: int, offset: int, has_display_product: bool = False
    ) -> list[dict[str, Any]]:
        # When has_display_product is true, restrict to basins that have at least
        # one run that the latest-product candidate query could surface. We align
        # discovery with availability on the three run-level dimensions the
        # candidate query also filters on (forecast_store latest-product):
        #   - status ∈ QHH_LATEST_READY_RUN_STATUSES (single source of truth)
        #   - run_type = 'forecast'
        #   - cycle_time IS NOT NULL
        # The source (GFS/IFS) and run_id dimensions are intentionally NOT pushed
        # down here: discovery is source-agnostic (a basin with any forecast run
        # should be discoverable); the concrete source/run_id is resolved later by
        # the latest-product query. So this stays a superset on source but is exact
        # on status/run_type/cycle_time.
        # #1729: both paths drop evidence-only fixtures in SQL, before LIMIT /
        # OFFSET, so page sizes count only public basins; NULL groups stay.
        display_filter = ""
        parameters: tuple[Any, ...] = (EVIDENCE_ONLY_BASIN_GROUP, limit, offset)
        if has_display_product:
            display_filter = """
                AND EXISTS (
                    SELECT 1
                    FROM core.basin_version bv
                    JOIN hydro.hydro_run hr
                        ON hr.basin_version_id = bv.basin_version_id
                    WHERE bv.basin_id = core.basin.basin_id
                      AND (bv.valid_to IS NULL OR bv.valid_to > now())
                      AND hr.status = ANY(%s::hydro.run_status[])
                      AND hr.run_type = 'forecast'
                      AND hr.cycle_time IS NOT NULL
                )
                """
            parameters = (EVIDENCE_ONLY_BASIN_GROUP, list(QHH_LATEST_READY_RUN_STATUSES), limit, offset)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT basin_id, basin_name, basin_group, description, created_at
                FROM core.basin
                WHERE basin_group IS DISTINCT FROM %s
                {display_filter}
                ORDER BY basin_name, basin_id
                LIMIT %s OFFSET %s
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_basin_versions(self, *, basin_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            # #1729: an evidence-only basin answers exactly like a missing one.
            cursor.execute(
                "SELECT 1 FROM core.basin WHERE basin_id = %s AND basin_group IS DISTINCT FROM %s",
                (basin_id, EVIDENCE_ONLY_BASIN_GROUP),
            )
            if cursor.fetchone() is None:
                raise MissingResourceError(f"basin_id not found: {basin_id}")
            cursor.execute(
                """
                SELECT
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
                FROM core.basin_version
                WHERE basin_id = %s
                ORDER BY active_flag DESC, created_at DESC, basin_version_id
                LIMIT %s OFFSET %s
                """,
                (basin_id, limit, offset),
            )
            return [_basin_version_public_projection(row) for row in cursor.fetchall()]

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
        # PR 2 (feat-reach-geom-from-river-shp): the reach-source contract
        # writes one single-part LineString per reach; SQL-side ST_Multi (see
        # the INSERT template below) wraps it into the column's required
        # MultiLineString shape.
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
                    # geom is geometry(MultiLineString, 4490) (000036). ST_Multi wraps a
                    # LineString payload into a single-part MultiLineString so the legacy
                    # LineString write contract still inserts; a MultiLineString WKT passes
                    # through unchanged.
                    template="(%s, %s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
                )
        return {"river_network_version": network, "segment_count": segment_count}

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
        # #1729: models under an evidence-only basin stay out of every active
        # mode (count and page alike); `b.` is untouched by the requalification.
        clauses.append("b.basin_group IS DISTINCT FROM %s")
        parameters.append(EVIDENCE_ONLY_BASIN_GROUP)
        where = f"WHERE {' AND '.join(clauses)}"

        # JOIN basin_version + basin so each row carries basin_id / basin_name —
        # parity with get_model_internal. OpenAPI ModelInstance schema declares
        # basin_id/basin_name (nullable), and the frontend builds basinVersionToBasinId
        # from model rows; without basin_id the map stays empty and single-run hydro
        # MVT popups (whose feature properties don't self-describe basin_id) fall back
        # to null → "请选择流域" placeholder.
        # Filter clauses must be requalified — `basin_version_id` and `active_flag`
        # exist on BOTH `core.basin_version` and `core.model_instance`, so the
        # unqualified WHERE form raises 'column reference is ambiguous' once the
        # JOIN is in place. The mechanical rewrite below is load-bearing, not
        # defensive: do NOT remove it.
        join_where = where.replace("basin_version_id", "mi.basin_version_id").replace(
            "active_flag", "mi.active_flag"
        )
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM core.model_instance mi
                JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
                JOIN core.basin b ON b.basin_id = bv.basin_id
                {join_where}
                """,
                tuple(parameters),
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"""
                SELECT mi.*, b.basin_id, b.basin_name
                FROM core.model_instance mi
                JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
                JOIN core.basin b ON b.basin_id = bv.basin_id
                {join_where}
                ORDER BY mi.created_at DESC, mi.model_id
                LIMIT %s OFFSET %s
                """,
                tuple([*parameters, limit, offset]),
            )
            items = [_model_public_projection(row) for row in cursor.fetchall()]
        return {"total": total, "items": items, "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        row = self.get_model_internal(model_id)
        if row is None:
            raise MissingResourceError(f"model_id not found: {model_id}")
        # #1729: the public detail hides an evidence-only basin's model like a
        # missing one; ``get_model_internal`` (scheduler / lifecycle) does not,
        # and its row stays free of ``basin_group`` so no response shape moves.
        with self._transaction() as cursor:
            cursor.execute("SELECT basin_group FROM core.basin WHERE basin_id = %s", (row["basin_id"],))
            basin = cursor.fetchone()
        if basin is not None and basin.get("basin_group") == EVIDENCE_ONLY_BASIN_GROUP:
            raise MissingResourceError(f"model_id not found: {model_id}")
        return _model_asset_detail(row)

    def get_model_internal(self, model_id: str) -> dict[str, Any]:
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
        return dict(row)

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
                ON CONFLICT (river_network_version_id, source, external_id)
                DO UPDATE SET river_segment_id = EXCLUDED.river_segment_id,
                              properties_json = EXCLUDED.properties_json
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
