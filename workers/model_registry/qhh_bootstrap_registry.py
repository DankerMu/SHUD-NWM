"""QHH bootstrap registry-row helpers: scheduler-ready model upsert and
resource profile, exact-set / geometry / stream-type assertions, model
activation and identity lookups, and station/output-segment digests (#2490
split of ``qhh_production_bootstrap``).

``workers.model_registry.qhh_production_bootstrap`` stays the stable import
path and re-exports every name defined here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .basins_registry_import import ImportSources, _fetch_optional, _json, _json_dict, _transaction
from .qhh_bootstrap_contracts import (
    QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS,
    QHH_RESOURCE_PROFILE_PRESERVED_OPERATIONAL_FIELDS,
    QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS,
    QhhBootstrapContext,
    QhhForcingStation,
    QhhProductionBootstrapError,
)


def _upsert_scheduler_ready_model(
    cursor: Any,
    context: QhhBootstrapContext,
    *,
    resource_profile_overrides: dict[str, Any],
) -> dict[str, int]:
    sources = context.sources
    ids = sources.ids
    expected_profile = _scheduler_ready_resource_profile(context, resource_profile_overrides=resource_profile_overrides)
    existing = _fetch_optional(
        cursor,
        """
        SELECT model_id,
               shud_code_version,
               model_package_uri,
               active_flag,
               COALESCE(lifecycle_state, CASE WHEN active_flag THEN 'active' ELSE 'inactive' END) AS lifecycle_state,
               resource_profile
        FROM core.model_instance
        WHERE model_id = %s
        FOR UPDATE
        """,
        (ids["model_id"],),
    )
    if existing is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_INSTANCE_MISSING",
            "Basins registry import did not create the expected QHH model instance.",
            model_id=ids["model_id"],
        )
    existing_profile = _json_dict(existing["resource_profile"])
    merged_profile = _canonical_scheduler_ready_resource_profile(
        existing_profile,
        expected_profile,
        resource_profile_overrides=resource_profile_overrides,
    )
    updates: list[str] = []
    params: list[Any] = []
    if existing["shud_code_version"] != context.shud_code_version:
        updates.append("shud_code_version = %s")
        params.append(context.shud_code_version)
    if existing["model_package_uri"] != sources.manifest["model_package_uri"]:
        updates.append("model_package_uri = %s")
        params.append(sources.manifest["model_package_uri"])
    if existing_profile != merged_profile:
        updates.append("resource_profile = %s")
        params.append(_json(merged_profile))
    if updates:
        cursor.execute(
            f"""
            UPDATE core.model_instance
            SET {", ".join(updates)}
            WHERE model_id = %s
            """,
            (*params, ids["model_id"]),
        )
        return {"created": 0, "updated": 1, "unchanged": 0}
    return {"created": 0, "updated": 0, "unchanged": 1}


def _registry_report_from_row_counts(sources: ImportSources, row_counts: dict[str, int]) -> dict[str, Any]:
    status = "already_imported" if all(count == 0 for count in row_counts.values()) else "imported"
    return {
        "schema_version": "basins.registry_import.v1",
        "status": status,
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "active": False,
        "segment_count": sources.geometry.segment_count,
        "row_counts": row_counts,
        "model_package_uri": sources.manifest["model_package_uri"],
        "manifest_uri": sources.manifest["manifest_uri"],
        "package_checksum": sources.manifest["package_checksum"],
    }


def _scheduler_ready_resource_profile(
    context: QhhBootstrapContext,
    *,
    resource_profile_overrides: dict[str, Any],
) -> dict[str, Any]:
    sources = context.sources
    profile = {
        "runnable": True,
        "lineage": "qhh_production_bootstrap",
        "resource_profile_id": "qhh-production-default",
        "scheduler": "slurm",
        "partition": os.getenv("NHMS_QHH_SLURM_PARTITION", "standard"),
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": int(os.getenv("NHMS_QHH_CPUS_PER_TASK", "4")),
        "memory_mb": int(os.getenv("NHMS_QHH_MEMORY_MB", "8192")),
        "memory_gb": int(os.getenv("NHMS_QHH_MEMORY_GB", "8")),
        "walltime_minutes": int(os.getenv("NHMS_QHH_WALLTIME_MINUTES", "720")),
        "display_capabilities": {"q_down": True, "tiles": True},
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "project_name": sources.model["shud_input_name"],
        "shud_input_name": sources.model["shud_input_name"],
        "basin_slug": sources.model["basin_slug"],
        "manifest_uri": sources.manifest["manifest_uri"],
        "model_package_uri": sources.manifest["model_package_uri"],
        "package_checksum": sources.manifest["package_checksum"],
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": sources.manifest.get("source_inventory_schema_version"),
        "station_count": len(context.stations),
        "output_segment_count": context.output_segment_count,
        "qhh_tsd_forc_sha256": context.tsd_forc_checksum,
        "qhh_sp_riv_sha256": context.sp_riv_checksum,
    }
    profile.update(_safe_resource_profile_overrides(resource_profile_overrides))
    return profile


def _canonical_scheduler_ready_resource_profile(
    existing_profile: Mapping[str, Any],
    expected_profile: Mapping[str, Any],
    *,
    resource_profile_overrides: Mapping[str, Any],
) -> dict[str, Any]:
    preserved = {
        key: value
        for key, value in dict(existing_profile).items()
        if key in QHH_RESOURCE_PROFILE_PRESERVED_OPERATIONAL_FIELDS
        and key not in QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS
        and key not in expected_profile
    }
    return {
        **preserved,
        **dict(expected_profile),
        **_safe_resource_profile_overrides(resource_profile_overrides),
    }


def _safe_resource_profile_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(overrides).items()
        if key in QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS and key not in QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS
    }


def _assert_exact_qhh_station_set(cursor: Any, context: QhhBootstrapContext) -> None:
    expected_ids = {station.station_id for station in context.stations}
    basin_version_id = context.sources.ids["basin_version_id"]
    cursor.execute(
        """
        SELECT station_id
        FROM met.met_station
        WHERE basin_version_id = %s
          AND station_role = 'forcing_grid'
          AND active_flag = true
        ORDER BY station_id
        """,
        (basin_version_id,),
    )
    observed_ids = {str(row["station_id"]) for row in cursor.fetchall()}
    extras = sorted(observed_ids - expected_ids)
    missing = sorted(expected_ids - observed_ids)
    if extras or missing:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STALE_STATION_IDENTITY",
            "Active QHH forcing-grid stations must match the validated qhh.tsd.forc exact set before activation.",
            model_id=context.sources.ids["model_id"],
            details={
                "basin_version_id": basin_version_id,
                "extra_station_ids": extras,
                "missing_station_ids": missing,
                "no_scheduler_ready_model": True,
                "rollback_expected": True,
            },
        )


def _assert_exact_qhh_output_segment_set(cursor: Any, context: QhhBootstrapContext) -> None:
    model_id = context.sources.ids["model_id"]
    river_network_version_id = context.sources.ids["river_network_version_id"]
    expected_ids = {f"{model_id}_shud_riv_{index:06d}" for index in range(1, context.output_segment_count + 1)}
    cursor.execute(
        """
        SELECT river_segment_id
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
        ORDER BY river_segment_id
        """,
        (river_network_version_id,),
    )
    observed_ids = {str(row["river_segment_id"]) for row in cursor.fetchall()}
    extras = sorted(observed_ids - expected_ids)
    missing = sorted(expected_ids - observed_ids)
    if extras or missing:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STALE_OUTPUT_SEGMENT_IDENTITY",
            "Active QHH output river segments must match the validated qhh.sp.riv exact set before activation.",
            model_id=model_id,
            details={
                "river_network_version_id": river_network_version_id,
                "extra_river_segment_ids": extras,
                "missing_river_segment_ids": missing,
                "no_scheduler_ready_model": True,
                "rollback_expected": True,
            },
        )


def _assert_complete_qhh_output_segment_geometry(
    cursor: Any,
    context: QhhBootstrapContext,
    *,
    geometry_backfilled_count: int,
) -> dict[str, int]:
    model_id = context.sources.ids["model_id"]
    river_network_version_id = context.sources.ids["river_network_version_id"]
    geometry_counts = _qhh_output_segment_geometry_counts(
        cursor,
        river_network_version_id,
        geometry_backfilled_count=geometry_backfilled_count,
    )
    if geometry_counts["geometry_missing_count"] > 0:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_OUTPUT_SEGMENT_GEOMETRY_INCOMPLETE",
            "Active QHH output river segments must have complete geometry before scheduler activation.",
            model_id=model_id,
            details={
                "river_network_version_id": river_network_version_id,
                **geometry_counts,
                "no_scheduler_ready_model": True,
                "rollback_expected": True,
            },
        )
    return geometry_counts


def _assert_complete_qhh_output_segment_stream_type(
    cursor: Any,
    river_network_version_id: str,
    *,
    model_id: str,
) -> None:
    """Fail closed when the output-segment upsert left an erased ``Type`` behind (#2154).

    ``_seed_output_segment_rows``' ``ON CONFLICT DO UPDATE`` rewrites
    ``properties_json`` wholesale, dropping the ``Type`` a previous backfill
    copied in and so NULLing the STORED ``stream_type``; it never bumps
    ``geometry_generation`` itself. Both of its entry points rely on the
    trailing ``_backfill_output_segment_geometry`` on the same cursor to
    restore ``Type`` and bump. When that backfill updates nothing -- e.g. every
    candidate is dropped by its ``ST_Length(source.geom) > 0`` filter -- the
    erasure would commit with the tile cache identity unrotated.

    The predicate reuses the backfill's candidate/source predicates and
    deliberately omits ``ST_Length(s.geom) > 0``: a degenerate source reach
    that still carries ``Type`` is exactly the case the backfill skips and this
    check must catch. "Carries ``Type``" means a non-null value
    (``->>'Type' IS NOT NULL``), mirroring the backfill, which copies ``Type``
    only when the source value is not JSON null: a source whose ``Type`` is
    ``null`` (a blank dbf cell) legitimately leaves the output row without one.
    Run it after the trailing backfill; raising rolls the whole transaction
    back, upsert included.
    """
    cursor.execute(
        """
        SELECT COUNT(*) AS stream_type_missing_count
        FROM core.river_segment t
        WHERE t.river_network_version_id = %s
          AND COALESCE(t.properties_json->>'shud_output_river', 'false') = 'true'
          AND (t.properties_json->>'shud_riv_index') ~ '^[0-9]+$'
          AND NOT t.properties_json ? 'Type'
          AND EXISTS (
              SELECT 1
              FROM core.river_segment s
              WHERE s.river_network_version_id = t.river_network_version_id
                AND COALESCE(s.properties_json->>'shud_output_river', 'false') <> 'true'
                AND s.properties_json ? 'iRiv'
                AND (s.properties_json->>'iRiv') ~ '^[0-9]+$'
                AND s.properties_json->>'iRiv' = t.properties_json->>'shud_riv_index'
                AND s.geom IS NOT NULL
                AND s.properties_json->>'Type' IS NOT NULL
          )
        """,
        (river_network_version_id,),
    )
    missing = int(cursor.fetchone()["stream_type_missing_count"] or 0)
    if missing > 0:
        raise QhhProductionBootstrapError(
            "QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE",
            "QHH output river segments lost their source stream Type and the geometry backfill did not restore it.",
            model_id=model_id,
            details={
                "river_network_version_id": river_network_version_id,
                "stream_type_missing_count": missing,
                "rollback_expected": True,
            },
        )


def _qhh_output_segment_geometry_counts(
    cursor: Any,
    river_network_version_id: str,
    *,
    geometry_backfilled_count: int,
) -> dict[str, int]:
    cursor.execute(
        """
        SELECT COUNT(*) AS geometry_missing_count
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
          AND geom IS NULL
        """,
        (river_network_version_id,),
    )
    row = cursor.fetchone()
    return {
        "geometry_backfilled_count": int(geometry_backfilled_count),
        "geometry_missing_count": int(row["geometry_missing_count"] or 0),
    }


def _seed_station_rows(
    cursor: Any,
    *,
    model: dict[str, Any],
    stations: Sequence[QhhForcingStation],
    project_name: str,
    tsd_forc_path: Path,
    tsd_forc_checksum: str,
) -> dict[str, int]:
    from psycopg2.extras import execute_values

    rows = []
    for station in stations:
        expected_properties = {
            "seed": "qhh_production_bootstrap",
            "model_id": model["model_id"],
            "basin_id": model["basin_id"],
            "basin_version_id": model["basin_version_id"],
            "project_name": project_name,
            "source": f"{project_name}.tsd.forc",
            "source_file": str(tsd_forc_path),
            "source_sha256": tsd_forc_checksum,
            "shud_forcing_index": station.forcing_index,
            "forcing_filename": station.forcing_filename,
            "forcing_source_identity": f"{project_name}.tsd.forc:{station.forcing_index}:{station.forcing_filename}",
            "original_id": station.original_id,
            "x": station.x,
            "y": station.y,
            "z": station.z,
            "elevation_metadata": {
                "source": f"{project_name}.tsd.forc",
                "raw_z": station.z,
                "normalized_missing_to_zero": station.z <= -9990,
            },
        }
        rows.append(
            (
                station.station_id,
                model["basin_version_id"],
                station.station_name,
                station.longitude,
                station.latitude,
                station.elevation_m,
                "forcing_grid",
                True,
                _json(expected_properties),
                _station_digest(
                    basin_version_id=model["basin_version_id"],
                    station_name=station.station_name,
                    lon=station.longitude,
                    lat=station.latitude,
                    elevation_m=station.elevation_m,
                    station_role="forcing_grid",
                    active_flag=True,
                    properties_json=expected_properties,
                ),
            )
        )
    existing = _existing_station_digests(cursor, [row[0] for row in rows])
    created = sum(1 for row in rows if row[0] not in existing)
    unchanged = sum(1 for row in rows if existing.get(row[0]) == row[9])
    updated = len(rows) - created - unchanged
    if rows:
        execute_values(
            cursor,
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
            ON CONFLICT (station_id) DO UPDATE
            SET basin_version_id = EXCLUDED.basin_version_id,
                station_name = EXCLUDED.station_name,
                geom = EXCLUDED.geom,
                elevation_m = EXCLUDED.elevation_m,
                station_role = EXCLUDED.station_role,
                active_flag = EXCLUDED.active_flag,
                properties_json = EXCLUDED.properties_json
            """,
            [row[:9] for row in rows],
            template="(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s, %s, %s, %s)",
            page_size=1000,
        )
    return {"created": created, "updated": updated, "unchanged": unchanged}


def _output_segment_expected_properties(
    *,
    model: dict[str, Any],
    project_name: str,
    index: int,
    sp_riv_path: Path,
    sp_riv_checksum: str,
) -> dict[str, Any]:
    return {
        "seed": "qhh_production_bootstrap",
        "model_id": model["model_id"],
        "basin_id": model["basin_id"],
        "basin_version_id": model["basin_version_id"],
        "shud_output_river": True,
        "shud_riv_index": index,
        "source": f"{project_name}.sp.riv",
        "source_file": str(sp_riv_path),
        "source_sha256": sp_riv_checksum,
        "geometry_source": "gis_rivseg_iRiv",
        "output_identity": f"{project_name}.sp.riv:{index}",
    }


def _activate_qhh_model(cursor: Any, model_id: str) -> None:
    cursor.execute(
        """
        UPDATE core.model_instance
        SET active_flag = true,
            lifecycle_state = 'active'
        WHERE model_id = %s
        """,
        (model_id,),
    )


def _persist_inactive_on_scheduler_visibility_blocker(
    database_url: str,
    error: QhhProductionBootstrapError,
    *,
    model_id: str,
) -> None:
    if error.error_code not in {
        "QHH_BOOTSTRAP_STALE_STATION_IDENTITY",
        "QHH_BOOTSTRAP_STALE_OUTPUT_SEGMENT_IDENTITY",
        "QHH_BOOTSTRAP_OUTPUT_SEGMENT_GEOMETRY_INCOMPLETE",
    }:
        return
    if error.model_id is not None and error.model_id != model_id:
        return
    try:
        removed = _persistently_mark_qhh_model_inactive(
            database_url,
            model_id=model_id,
            blocker=error.to_payload(),
        )
    except Exception as inactive_error:
        error.details["persistent_scheduler_visibility_removed"] = False
        error.details["persistent_inactive_error"] = inactive_error.__class__.__name__
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STALE_MODEL_INACTIVATION_FAILED",
            "QHH stale exact-set blocker was found, but the target model could not be persistently marked inactive.",
            model_id=model_id,
            details={
                "stale_blocker": error.to_payload(),
                "persistent_scheduler_visibility_removed": False,
            },
        ) from inactive_error
    error.details["persistent_scheduler_visibility_removed"] = removed


def _persistently_mark_qhh_model_inactive(
    database_url: str,
    *,
    model_id: str,
    blocker: Mapping[str, Any],
) -> bool:
    with _transaction(database_url) as cursor:
        cursor.execute(
            """
            UPDATE core.model_instance
            SET active_flag = false,
                lifecycle_state = 'inactive',
                resource_profile = COALESCE(resource_profile, '{}'::jsonb) || %s::jsonb
            WHERE model_id = %s
              AND active_flag = true
              AND COALESCE(lifecycle_state, 'active') = 'active'
            """,
            (
                _json(
                    {
                        "runnable": False,
                        "qhh_scheduler_blocker": {
                            "code": blocker.get("error_code"),
                            "message": blocker.get("message"),
                            "basin_version_id": blocker.get("basin_version_id"),
                            "river_network_version_id": blocker.get("river_network_version_id"),
                            "extra_station_ids": blocker.get("extra_station_ids", []),
                            "missing_station_ids": blocker.get("missing_station_ids", []),
                            "extra_river_segment_ids": blocker.get("extra_river_segment_ids", []),
                            "missing_river_segment_ids": blocker.get("missing_river_segment_ids", []),
                        },
                    }
                ),
                model_id,
            ),
        )
        return cursor.rowcount > 0


def _lock_qhh_basin_scope(cursor: Any, basin_version_id: str) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"qhh-bootstrap:{basin_version_id}",))
    cursor.execute(
        """
        SELECT basin_version_id
        FROM core.basin_version
        WHERE basin_version_id = %s
        FOR UPDATE
        """,
        (basin_version_id,),
    )


def _active_qhh_identity_rows(cursor: Any, sources: ImportSources, *, model_id: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT mi.model_id,
               bv.basin_id,
               mi.basin_version_id,
               mi.river_network_version_id,
               mi.model_package_uri,
               mi.resource_profile,
               CASE
                 WHEN mi.model_id = %s THEN 'model_id'
                 WHEN bv.basin_id = %s THEN 'basin_id'
                 WHEN mi.basin_version_id = %s THEN 'basin_version_id'
                 WHEN mi.river_network_version_id = %s THEN 'river_network_version_id'
                 WHEN mi.model_package_uri = %s THEN 'model_package_uri'
                 WHEN mi.resource_profile->>'package_checksum' = %s THEN 'package_checksum'
                 WHEN mi.resource_profile->>'basin_slug' = %s
                      AND COALESCE(mi.resource_profile->>'project_name', mi.resource_profile->>'shud_input_name') = %s
                   THEN 'basin_project_identity'
                 ELSE 'unknown'
               END AS duplicate_reason
        FROM core.model_instance mi
        JOIN core.basin_version bv
          ON bv.basin_version_id = mi.basin_version_id
        WHERE mi.active_flag = true
          AND COALESCE(mi.lifecycle_state, 'active') = 'active'
          AND (
            mi.model_id = %s
            OR bv.basin_id = %s
            OR mi.basin_version_id = %s
            OR mi.river_network_version_id = %s
            OR mi.model_package_uri = %s
            OR mi.resource_profile->>'package_checksum' = %s
            OR (
              mi.resource_profile->>'basin_slug' = %s
              AND COALESCE(mi.resource_profile->>'project_name', mi.resource_profile->>'shud_input_name') = %s
            )
          )
        ORDER BY mi.model_id
        """,
        (
            model_id,
            sources.ids["basin_id"],
            sources.ids["basin_version_id"],
            sources.ids["river_network_version_id"],
            sources.manifest["model_package_uri"],
            sources.manifest.get("package_checksum"),
            sources.model["basin_slug"],
            sources.model["shud_input_name"],
            model_id,
            sources.ids["basin_id"],
            sources.ids["basin_version_id"],
            sources.ids["river_network_version_id"],
            sources.manifest["model_package_uri"],
            sources.manifest.get("package_checksum"),
            sources.model["basin_slug"],
            sources.model["shud_input_name"],
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    if len(rows) <= 1 and (not rows or rows[0]["model_id"] == model_id):
        return []
    return [
        {
            "model_id": str(row["model_id"]),
            "basin_id": str(row["basin_id"]),
            "basin_version_id": str(row["basin_version_id"]),
            "river_network_version_id": str(row["river_network_version_id"]),
            "duplicate_reason": str(row.get("duplicate_reason") or "unknown"),
        }
        for row in rows
    ]


def _fetch_model_identity(cursor: Any, model_id: str) -> dict[str, Any]:
    row = _fetch_optional(
        cursor,
        """
        SELECT mi.model_id,
               bv.basin_id,
               mi.basin_version_id,
               mi.river_network_version_id,
               mi.mesh_version_id,
               mi.shud_code_version,
               mi.model_package_uri,
               mi.active_flag,
               COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
               mi.resource_profile
        FROM core.model_instance mi
        JOIN core.basin_version bv
          ON bv.basin_version_id = mi.basin_version_id
        WHERE mi.model_id = %s
        """,
        (model_id,),
    )
    if row is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_INSTANCE_MISSING",
            "QHH model instance is missing.",
            model_id=model_id,
        )
    return row


def _assert_dynamic_forcing_unchanged(model_id: str, *, before: dict[str, int], after: dict[str, int]) -> None:
    if before != after:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DYNAMIC_FORCING_PRESENT",
            "QHH bootstrap must not create future-cycle forcing rows in this task.",
            model_id=model_id,
            details={"before": before, "after": after},
        )


def _existing_station_digests(cursor: Any, station_ids: Sequence[str]) -> dict[str, str]:
    if not station_ids:
        return {}
    cursor.execute(
        """
        SELECT station_id,
               basin_version_id,
               station_name,
               ST_X(geom) AS lon,
               ST_Y(geom) AS lat,
               elevation_m,
               station_role,
               active_flag,
               properties_json
        FROM met.met_station
        WHERE station_id = ANY(%s)
        """,
        (list(station_ids),),
    )
    return {
        str(row["station_id"]): _station_digest(
            basin_version_id=row["basin_version_id"],
            station_name=row["station_name"],
            lon=row["lon"],
            lat=row["lat"],
            elevation_m=row["elevation_m"],
            station_role=row["station_role"],
            active_flag=bool(row["active_flag"]),
            properties_json=_json_dict(row["properties_json"]),
        )
        for row in cursor.fetchall()
    }


def _existing_output_segment_digests(
    cursor: Any,
    river_network_version_id: str,
    river_segment_ids: Sequence[str],
    *,
    expected_properties_by_id: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    if not river_segment_ids:
        return {}
    cursor.execute(
        """
        SELECT river_segment_id,
               river_network_version_id,
               segment_order,
               properties_json
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND river_segment_id = ANY(%s)
        """,
        (river_network_version_id, list(river_segment_ids)),
    )
    digests: dict[str, str] = {}
    expected_properties_by_id = expected_properties_by_id or {}
    for row in cursor.fetchall():
        river_segment_id = str(row["river_segment_id"])
        properties_json = _json_dict(row["properties_json"])
        expected_properties = expected_properties_by_id.get(river_segment_id)
        if expected_properties is not None:
            properties_json = _output_segment_idempotency_properties(properties_json, expected_properties)
        digests[river_segment_id] = _output_segment_digest(
            river_network_version_id=row["river_network_version_id"],
            segment_order=row["segment_order"],
            properties_json=properties_json,
        )
    return digests


def _output_segment_idempotency_properties(
    stored_properties: dict[str, Any],
    expected_properties: dict[str, Any],
) -> dict[str, Any]:
    return {key: stored_properties[key] for key in expected_properties if key in stored_properties}


def _station_digest(
    *,
    basin_version_id: Any,
    station_name: Any,
    lon: Any,
    lat: Any,
    elevation_m: Any,
    station_role: Any,
    active_flag: bool,
    properties_json: dict[str, Any],
) -> str:
    return _canonical_json(
        {
            "basin_version_id": basin_version_id,
            "station_name": station_name,
            "lon": None if lon is None else float(lon),
            "lat": None if lat is None else float(lat),
            "elevation_m": None if elevation_m is None else float(elevation_m),
            "station_role": station_role,
            "active_flag": bool(active_flag),
            "properties_json": properties_json,
        }
    )


def _output_segment_digest(
    *,
    river_network_version_id: Any,
    segment_order: Any,
    properties_json: dict[str, Any],
) -> str:
    return _canonical_json(
        {
            "river_network_version_id": river_network_version_id,
            "segment_order": None if segment_order is None else int(segment_order),
            "properties_json": properties_json,
        }
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
