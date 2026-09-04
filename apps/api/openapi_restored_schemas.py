"""Hand-written named response schemas for routes FastAPI can only type opaquely.

Every route covered here returns ``dict[str, Any]`` (the ``_ok()`` envelope
pattern, or a bare store payload for ``forecast-series``), so FastAPI emits
``{type: object, additionalProperties: true}`` and the published contract lost
its named components. ``apps/api/openapi_patching.py`` injects these schemas and
rewrites the corresponding 200 bodies; nothing here touches a handler, so the
change is runtime-inert (a ``response_model=`` would filter response bodies).

**Shape oracle**: every schema below is derived from the store/handler function
that builds the dict, not from the historical ``openapi/nhms.v1.yaml``, which was
hand-maintained and never validated against a response. The ``:source:`` note on
each builder names that function. Where the historical document disagreed with
the runtime, the runtime won; those deviations are called out inline.

**Nullability**: OpenAPI 3.1 ``anyOf`` unions only, never the OpenAPI 3.0
null-flag keyword that ``openapi_patching``'s legacy flag helper emits (pinned
by ``tests/test_openapi_31_contract.py::BASELINE_NULLABLE_COUNT``), and never
the scalar type-array form ``type: [T, "null"]`` (pinned by the same module's
``_scalar_type_union_null_count`` assertion).
"""

from typing import Any

_JSON_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": True}


def _null_union(schema: dict) -> dict:
    """Nullability as a native OpenAPI 3.1 union instead of the 3.0 keyword.

    The legacy flag helper in ``openapi_patching`` emits the OpenAPI 3.0-only
    null flag that ``_finalize_openapi_schema`` has to rewrite, and every such
    node is pinned by
    ``tests/test_openapi_31_contract.py::BASELINE_NULLABLE_COUNT``. Hand-written
    schemas express the union directly, so they need no finalizer rewrite and
    leave that baseline untouched.
    """
    return {"anyOf": [schema, {"type": "null"}]}


# --------------------------------------------------------------------------- #
# Basin registry (`apps/api/routes/models.py`)
# --------------------------------------------------------------------------- #


def _basin_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.list_basins`` (model_registry.py:824)."""
    return {
        "type": "object",
        "required": ["basin_id", "basin_name", "created_at"],
        "properties": {
            "basin_id": {"type": "string"},
            "basin_name": {"type": "string"},
            "basin_group": _null_union({"type": "string"}),
            "description": _null_union({"type": "string"}),
            "created_at": {"type": "string", "format": "date-time"},
        },
    }


def _geojson_multi_polygon_schema() -> dict:
    """:source: ``ST_AsGeoJSON(core.basin_version.geom)``; the column is
    ``geometry(MultiPolygon, 4490) NOT NULL`` (db/migrations/000004_core.sql:13)."""
    position = {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "number"}}
    return {
        "type": "object",
        "required": ["type", "coordinates"],
        "properties": {
            "type": {"type": "string", "enum": ["MultiPolygon"]},
            "coordinates": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "array", "items": position}},
            },
        },
    }


def _basin_version_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.list_basin_versions``
    (model_registry.py:869-893) through ``_basin_version_public_projection``
    (model_registry.py:3625), which redacts ``source_uri``/``checksum`` to null.
    """
    return {
        "type": "object",
        "required": ["basin_version_id", "basin_id", "version_label", "geom", "active_flag", "created_at"],
        "properties": {
            "basin_version_id": {"type": "string"},
            "basin_id": {"type": "string"},
            "version_label": {"type": "string"},
            "geom": {"$ref": "#/components/schemas/GeoJsonMultiPolygon"},
            "active_flag": {"type": "boolean"},
            "valid_from": _null_union({"type": "string", "format": "date-time"}),
            "valid_to": _null_union({"type": "string", "format": "date-time"}),
            "source_uri": {
                **_null_union({"type": "string"}),
                "description": "Public responses redact local/source lineage URIs and return null.",
            },
            "checksum": {
                **_null_union({"type": "string"}),
                "description": "Public responses redact raw lineage checksums and return null.",
            },
            "created_at": {"type": "string", "format": "date-time"},
        },
    }


# --------------------------------------------------------------------------- #
# River segments (`apps/api/routes/models.py`)
# --------------------------------------------------------------------------- #


def _geojson_line_string_schema() -> dict:
    """:source: ``ST_LineSubstring`` output on the Path C segment-slice branch
    (model_registry.py:1313)."""
    position = {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "number"}}
    return {
        "type": "object",
        "required": ["type", "coordinates"],
        "properties": {
            "type": {"type": "string", "enum": ["LineString"]},
            "coordinates": {"type": "array", "minItems": 2, "items": position},
        },
    }


def _geojson_multi_line_string_schema() -> dict:
    """:source: ``ST_AsGeoJSON(core.river_segment.geom)``; the column is
    ``geometry(MultiLineString, 4490)`` since db/migrations/000037."""
    position = {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "number"}}
    return {
        "description": (
            "GeoJSON MultiLineString. The underlying core.river_segment.geom column type is "
            "MultiLineString(4490); since the PR-2 contract, every reach row currently stored "
            "holds exactly one part (the single-part flow-ordered polyline derived from "
            "gis/river.shp). The wrapper type is retained to allow a basin's input to express "
            "a genuine multi-part reach in the future without a schema change."
        ),
        "type": "object",
        "required": ["type", "coordinates"],
        "properties": {
            "type": {"type": "string", "enum": ["MultiLineString"]},
            "coordinates": {
                "type": "array",
                "items": {"type": "array", "minItems": 2, "items": position},
            },
        },
    }


def _river_segment_geometry_schema() -> dict:
    return {
        "oneOf": [
            {"$ref": "#/components/schemas/GeoJsonLineString"},
            {"$ref": "#/components/schemas/GeoJsonMultiLineString"},
        ]
    }


def _river_segment_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.get_river_segment``
    (model_registry.py:1749-1813) through ``_river_segment_detail``
    (model_registry.py:3580)."""
    return {
        "type": "object",
        "required": ["river_segment_id", "river_network_version_id", "geom", "properties_json", "created_at"],
        "properties": {
            "river_segment_id": {"type": "string"},
            "river_network_version_id": {"type": "string"},
            "segment_order": _null_union({"type": "integer"}),
            "downstream_segment_id": _null_union({"type": "string"}),
            "length_m": _null_union({"type": "number"}),
            "geom": _river_segment_geometry_schema(),
            "properties_json": dict(_JSON_OBJECT),
            "created_at": {"type": "string", "format": "date-time"},
        },
    }


def _river_segment_feature_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.list_river_segments``
    (reach path model_registry.py:1179-1209, Path C slice path
    model_registry.py:1603-1626).

    ``id`` is emitted only by the Path C slice branch; ``downstream_segment_id``
    only by the reach branch, and the slice branch adds ``iRiv``/``iEle``/
    ``reach_segment_id`` inside ``properties`` (admitted by
    ``additionalProperties: true``). The six ``required`` property keys are the
    ones both branches write unconditionally.
    """
    return {
        "type": "object",
        "required": ["type", "properties", "geometry"],
        "properties": {
            "type": {"type": "string", "enum": ["Feature"]},
            "id": {
                "type": "string",
                "description": "Segment-level feature id, emitted by the segment-slice query path only.",
            },
            "properties": {
                "type": "object",
                "required": [
                    "segment_id",
                    "river_segment_id",
                    "basin_version_id",
                    "river_network_version_id",
                    "name",
                    "stream_order",
                ],
                "properties": {
                    "segment_id": {"type": "string"},
                    "river_segment_id": {"type": "string"},
                    "basin_version_id": {"type": "string"},
                    "river_network_version_id": {"type": "string"},
                    "name": {"type": "string"},
                    "stream_order": {"type": "integer"},
                    "segment_order": _null_union({"type": "integer"}),
                    "downstream_segment_id": _null_union({"type": "string"}),
                    "length_m": _null_union({"type": "number"}),
                },
                "additionalProperties": True,
            },
            "geometry": _river_segment_geometry_schema(),
        },
    }


def _river_segment_feature_collection_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.list_river_segments``
    (model_registry.py:1211-1218 / :1654-1661)."""
    return {
        "type": "object",
        "required": ["type", "features", "total", "feature_total", "limit", "offset"],
        "properties": {
            "type": {"type": "string", "enum": ["FeatureCollection"]},
            "features": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/RiverSegmentFeature"},
            },
            "total": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Total matching stored river segment rows, including rows omitted from "
                    "features because geom is null."
                ),
            },
            "feature_total": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Total matching river segment rows with non-null LineString geometry that "
                    "can be emitted as GeoJSON features."
                ),
            },
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
        },
    }


# --------------------------------------------------------------------------- #
# Model instances and lifecycle (`apps/api/routes/models.py`)
# --------------------------------------------------------------------------- #


def _model_instance_schema() -> dict:
    """:source: three producers, whose union this schema documents.

    1. ``_model_public_projection`` (model_registry.py:3601) over ``list_models``
       rows (model_registry.py:2426 ``SELECT mi.*, b.basin_id, b.basin_name``).
    2. ``_model_asset_detail`` (model_registry.py:3542) over
       ``get_model_internal`` rows (model_registry.py:2450-2468).
    3. ``_model_public_projection`` again, over the lifecycle rows behind
       ``POST /api/v1/models/{model_id}/lifecycle``
       (``_fetch_model_lifecycle_row`` model_registry.py:2634-2662,
       ``_fetch_active_model_for_scope`` :2666-2700,
       ``_update_model_lifecycle_state`` :3143-3190), which reach the client
       unsanitized as ``data.model``/``data.previous_model``
       (apps/api/routes/models.py:629-640).

    The detail rows and the lifecycle rows each strictly extend the list rows,
    but neither contains the other, so only the ``core.model_instance`` columns
    every producer writes are ``required``:

    * the list projection omits ``segment_count``/``mesh_uri``/``mesh_checksum``/
      ``model_name`` and the nine ``MODEL_ASSET_LINEAGE_KEYS``;
    * only the detail projection derives those nine lineage keys (none is a
      ``core.model_instance`` column) and normalizes ``model_name``;
    * only the lifecycle rows carry ``basin_checksum``/``river_network_checksum``
      (``bv.checksum``/``rnv.checksum`` aliases, nulled by the projection but
      still present as keys) and ``mesh_properties_json`` -- the SQL alias for
      ``mv.properties_json``, which ``_model_asset_detail`` pops at :3545 and
      ``_model_public_projection`` never does.
    """
    return {
        "type": "object",
        "required": [
            "model_id",
            "basin_version_id",
            "river_network_version_id",
            "mesh_version_id",
            "calibration_version_id",
            "shud_code_version",
            "active_flag",
            "lifecycle_state",
            "model_package_uri",
            "resource_profile",
            "created_at",
        ],
        "properties": {
            "model_id": {"type": "string"},
            "model_name": _null_union({"type": "string"}),
            "basin_id": _null_union({"type": "string"}),
            "basin_name": _null_union({"type": "string"}),
            "basin_version_id": {"type": "string"},
            "basin_checksum": {
                **_null_union({"type": "string"}),
                "description": (
                    "bv.checksum, projected by the lifecycle queries only. Public responses "
                    "redact raw lineage checksums, so the key is present and always null."
                ),
            },
            "river_network_version_id": {"type": "string"},
            "river_network_checksum": {
                **_null_union({"type": "string"}),
                "description": (
                    "rnv.checksum, projected by the lifecycle queries only. Public responses "
                    "redact raw lineage checksums, so the key is present and always null."
                ),
            },
            "mesh_version_id": {"type": "string"},
            "calibration_version_id": {"type": "string"},
            "segment_count": _null_union({"type": "integer", "minimum": 0}),
            "mesh_uri": _null_union({"type": "string"}),
            "mesh_checksum": _null_union({"type": "string"}),
            "mesh_properties_json": {
                **_JSON_OBJECT,
                "description": (
                    "mv.properties_json (core.mesh_version.properties_json, JSONB NOT NULL "
                    "DEFAULT '{}'). Emitted only by the lifecycle route, whose projection -- "
                    "unlike _model_asset_detail -- does not pop this key."
                ),
            },
            "shud_code_version": {"type": "string"},
            "rshud_code_version": _null_union({"type": "string"}),
            "autoshud_code_version": _null_union({"type": "string"}),
            "active_flag": {"type": "boolean"},
            "lifecycle_state": {
                "type": "string",
                "enum": ["inactive", "active", "deprecated", "superseded"],
                "description": (
                    "core.model_instance.lifecycle_state, NOT NULL DEFAULT 'inactive' with a "
                    "four-member CHECK (db/migrations/000022_model_asset_lifecycle.sql:2,23-24). "
                    "Both projections write it unconditionally, falling back to "
                    "active if active_flag else inactive, so it is always present."
                ),
            },
            "container_image": _null_union({"type": "string"}),
            "model_package_uri": _null_union({"type": "string"}),
            "package_checksum": _null_union({"type": "string"}),
            "manifest_uri": _null_union({"type": "string"}),
            "source_inventory_checksum": _null_union({"type": "string"}),
            "basin_slug": _null_union({"type": "string"}),
            "shud_input_name": _null_union({"type": "string"}),
            "source_path": _null_union({"type": "string"}),
            "resolved_source_path": _null_union({"type": "string"}),
            "source_uri": _null_union({"type": "string"}),
            "source_is_symlink": _null_union({"type": "boolean"}),
            "resource_profile": dict(_JSON_OBJECT),
            "created_at": {"type": "string", "format": "date-time"},
        },
    }


def _model_instance_page_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.list_models`` (model_registry.py:2437)
    through ``sanitize_model_list_payload`` (model_registry.py:3427), which
    replaces ``items`` and passes ``total``/``limit``/``offset`` through."""
    return {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/ModelInstance"}},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "offset": {"type": "integer", "minimum": 0},
        },
    }


def _model_operation_preflight_schema() -> dict:
    """:source: ``_build_model_operation_preflight`` (model_registry.py:3034-3091),
    optionally rewritten by ``_apply_idempotent_rollback_preflight``
    (model_registry.py:3784), which only replaces existing keys."""
    def evidence_list() -> dict:
        # A fresh dict per call: PyYAML emits anchors/aliases for shared object
        # identity, which would make the regenerated spec unreadable.
        return {"type": "array", "items": dict(_JSON_OBJECT)}

    return {
        "type": "object",
        "required": ["schema", "operation", "status", "model_id", "blockers", "warnings", "impact"],
        "properties": {
            "schema": {"type": "string"},
            "request_id": _null_union({"type": "string"}),
            "operation": {"type": "string"},
            "action_id": _null_union({"type": "string"}),
            "actor_id": _null_union({"type": "string"}),
            "roles": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": ["ready", "blocked"]},
            "model_id": {"type": "string"},
            "basin_id": _null_union({"type": "string"}),
            "basin_version_id": _null_union({"type": "string"}),
            "current_active_model_id": _null_union({"type": "string"}),
            "previous_model_id": _null_union({"type": "string"}),
            "restored_model_id": _null_union({"type": "string"}),
            "prior_audit_log_id": _null_union({"type": "integer"}),
            "rollback_history": _null_union(dict(_JSON_OBJECT)),
            "river_network_version_id": _null_union({"type": "string"}),
            "mesh_version_id": _null_union({"type": "string"}),
            "lineage": dict(_JSON_OBJECT),
            "object_uri_prefix": dict(_JSON_OBJECT),
            "impact": dict(_JSON_OBJECT),
            "blockers": evidence_list(),
            "warnings": evidence_list(),
            "override_missing_active": {"type": "boolean"},
            "reason": _null_union({"type": "string"}),
        },
    }


def _model_lifecycle_result_schema() -> dict:
    """:source: ``PsycopgModelRegistryStore.model_lifecycle_operation``
    (model_registry.py:2189-2195 idempotent-rollback, :2222-2228 blocked,
    :2318-2327 committed); ``previous_model`` is absent on the blocked branch.
    ``status`` values are the three ``_apply_model_lifecycle_transition``
    outcomes (model_registry.py:3106/3112/3140) plus ``blocked``.
    """
    return {
        "type": "object",
        "required": ["status", "operation", "model", "preflight"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["allowed", "blocked", "already_current", "rollback"],
            },
            "operation": {"type": "string"},
            "model": {"$ref": "#/components/schemas/ModelInstance"},
            "previous_model": _null_union({"$ref": "#/components/schemas/ModelInstance"}),
            "preflight": {"$ref": "#/components/schemas/ModelOperationPreflight"},
            "audit_reference": _null_union(dict(_JSON_OBJECT)),
        },
    }


# --------------------------------------------------------------------------- #
# Hydro runs (`apps/api/routes/forecast.py`)
# --------------------------------------------------------------------------- #


def _run_status_schema() -> dict:
    """:source: the ``hydro.run_status`` enum: ten members from
    db/migrations/000003_enums.sql:10-21 plus ``pending`` from
    db/migrations/000013_enum_remediation.sql:3."""
    return {
        "type": "string",
        "enum": [
            "created",
            "staged",
            "pending",
            "submitted",
            "running",
            "succeeded",
            "parsed",
            "published",
            "failed",
            "cancelled",
            "superseded",
        ],
    }


def _run_type_schema() -> dict:
    """:source: the ``hydro.run_type`` enum: db/migrations/000003_enums.sql:3 plus
    ``hindcast`` from db/migrations/000045_hydro_run_type_hindcast.sql:18."""
    return {"type": "string", "enum": ["analysis", "forecast", "hindcast"]}


def _hydro_run_schema() -> dict:
    """:source: ``PsycopgForecastStore.list_runs`` (forecast_store.py:926-940
    ``SELECT h.*, mi.river_network_version_id, bv.basin_id,
    COALESCE(ds.adapter_name, h.source_id) AS source``) through
    ``_hydro_run_response`` (forecast_store.py:3964), which is a pure
    ``_json_ready(dict(row))`` pass-through.

    ``h.*`` is every ``hydro.hydro_run`` column, so ``run_key``
    (db/migrations/000050:178) and ``parsed_at`` (db/migrations/000056:22) are
    part of the response, as are the three joined columns.
    """
    return {
        "type": "object",
        "required": [
            "run_id",
            "run_type",
            "scenario_id",
            "model_id",
            "basin_version_id",
            "river_network_version_id",
            "status",
            "start_time",
            "end_time",
            "created_at",
            "updated_at",
        ],
        "properties": {
            "run_id": {"type": "string"},
            "run_key": {"type": "integer"},
            "run_type": {"$ref": "#/components/schemas/RunType"},
            "scenario_id": {"type": "string"},
            "model_id": {"type": "string"},
            "basin_id": _null_union({"type": "string"}),
            "basin_version_id": {"type": "string"},
            "river_network_version_id": _null_union({"type": "string"}),
            "forcing_version_id": _null_union({"type": "string"}),
            "init_state_id": _null_union({"type": "string"}),
            "source_id": _null_union({"type": "string"}),
            "source": {
                **_null_union({"type": "string"}),
                "description": "Adapter name for source_id, falling back to source_id itself.",
            },
            "cycle_time": _null_union({"type": "string", "format": "date-time"}),
            "status": {"$ref": "#/components/schemas/RunStatus"},
            "slurm_job_id": _null_union({"type": "string"}),
            "start_time": {"type": "string", "format": "date-time"},
            "end_time": {"type": "string", "format": "date-time"},
            "run_manifest_uri": _null_union({"type": "string"}),
            "output_uri": _null_union({"type": "string"}),
            "log_uri": _null_union({"type": "string"}),
            "error_code": _null_union({"type": "string"}),
            "error_message": _null_union({"type": "string"}),
            "parsed_at": _null_union({"type": "string", "format": "date-time"}),
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    }


def _hydro_run_page_schema() -> dict:
    """:source: ``PsycopgForecastStore.list_runs`` (forecast_store.py:942-947)
    wrapped by ``_paginated_payload`` (apps/api/routes/forecast.py:230-236),
    which adds ``total`` alongside the store's ``total_count``."""
    return {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/HydroRun"}},
            "total": {"type": "integer", "minimum": 0},
            "total_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Backward-compatible alias of total.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
        },
    }


# --------------------------------------------------------------------------- #
# River forecast series (`apps/api/routes/forecast.py`)
# --------------------------------------------------------------------------- #


def _series_segment_schema() -> dict:
    """:source: ``_forecast_response_from_rows`` (forecast_store.py:4025-4036)
    spread with ``_forecast_series_metadata`` (forecast_store.py:4406-4417),
    which omits ``source_id``/``cycle_time``/``available_lead_hours`` entirely
    when they are unavailable.

    ``points`` entries are ``[epoch milliseconds, value]``: the first element is
    ``_timestamp_ms`` (forecast_store.py:3950), an ``int``, never a formatted
    timestamp string.
    """
    return {
        "type": "object",
        "required": ["scenario_id", "segment_role", "points"],
        "properties": {
            "scenario_id": {"type": "string"},
            "source_id": {"type": "string", "example": "GFS"},
            "cycle_time": {"type": "string", "format": "date-time"},
            "available_lead_hours": {"type": "integer", "example": 168},
            "segment_role": {"type": "string"},
            "variable": {"type": "string", "example": "q_down"},
            "points": {
                "type": "array",
                "description": "[epoch milliseconds, value] pairs, ascending by time.",
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "number"},
                },
            },
        },
    }


def _river_series_response_schema() -> dict:
    """:source: ``PsycopgForecastStore.forecast_series`` forecast-only branches:
    ``_empty_forecast_response`` (forecast_store.py:3987-3994) and
    ``_forecast_response_from_rows`` (forecast_store.py:4040-4045)."""
    return {
        "type": "object",
        "required": ["segment_id", "issue_time", "unit", "series"],
        "properties": {
            "segment_id": {"type": "string"},
            "issue_time": _null_union({"type": "string", "format": "date-time"}),
            "unit": {"type": "string"},
            "series": {"type": "array", "items": {"$ref": "#/components/schemas/SeriesSegment"}},
        },
    }


def _spliced_forecast_response_schema() -> dict:
    """:source: ``PsycopgForecastStore.forecast_series`` ``include_analysis``
    branches: ``_empty_spliced_response`` (forecast_store.py:3997-4010) and
    ``_spliced_response_from_rows`` (forecast_store.py:4371-4377).

    Segment members come from forecast_store.py:4340-4348 (analysis, no
    forecast metadata) and :4360-4369 (per-scenario forecast, spread with
    ``_forecast_series_metadata``); ``data`` points are ``_segment_point``
    (forecast_store.py:4380-4381).
    """
    return {
        "type": "object",
        "description": (
            "Returned when include_analysis=true splices analysis-period data before the "
            "forecast window into a unified segments array.\n"
        ),
        "required": ["river_segment_id", "segments", "issue_time", "variable", "unit"],
        "properties": {
            "river_segment_id": {"type": "string"},
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["scenario", "source", "segment_role", "data"],
                    "properties": {
                        "scenario": {"type": "string"},
                        "scenario_id": {"type": "string", "description": "Canonical scenario identifier."},
                        "source": {"type": "string"},
                        "source_id": {
                            "type": "string",
                            "description": "Forecast source identifier; omitted on analysis segments.",
                        },
                        "cycle_time": {
                            "type": "string",
                            "format": "date-time",
                            "description": "Forecast source cycle time; omitted on analysis segments.",
                        },
                        "available_lead_hours": {
                            "type": "integer",
                            "description": "Available forecast lead time in hours; omitted on analysis segments.",
                        },
                        "segment_role": {"type": "string", "enum": ["past_7_days", "future_7_days"]},
                        "data": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["valid_time", "value"],
                                "properties": {
                                    "valid_time": {"type": "string", "format": "date-time"},
                                    "value": {"type": "number"},
                                },
                            },
                        },
                    },
                },
            },
            "issue_time": _null_union({"type": "string", "format": "date-time"}),
            "variable": {"type": "string"},
            "unit": {"type": "string"},
        },
    }


def _forecast_series_response_schema() -> dict:
    """The forecast-series 200 body has no ``SuccessEnvelope``: the handler
    returns ``store.forecast_series(...)`` directly (routes/forecast.py:68-79)
    instead of going through ``_ok``."""
    return {
        "oneOf": [
            {"$ref": "#/components/schemas/RiverSeriesResponse"},
            {"$ref": "#/components/schemas/SplicedForecastResponse"},
        ]
    }


# --------------------------------------------------------------------------- #
# Meteorological stations (`apps/api/routes/data_sources.py`)
# --------------------------------------------------------------------------- #


def _met_station_schema() -> dict:
    """:source: ``PsycopgForecastStore.list_met_stations``
    (forecast_store.py:1109-1119) through ``_station_response``
    (forecast_store.py:4525-4532).

    The inventory query projects ``ST_X``/``ST_Y`` as ``longitude``/``latitude``
    and never selects ``ms.geom`` or ``ms.active_flag``, so this resource carries
    neither a GeoJSON geometry nor an active flag; the map layer synthesises the
    Point client-side (``apps/frontend/src/lib/hydroMet/runtime.ts:90-105``).
    ``name``/``elevation`` are unconditional aliases added by
    ``_station_response``.

    ``required`` keeps the historical list minus the two deleted keys rather than
    re-deriving it from the projection; every remaining property is in fact always
    written, but widening ``required`` is a consumer-visible tightening that is
    out of scope here.
    """
    return {
        "type": "object",
        "required": ["station_id", "basin_version_id", "station_role", "created_at"],
        "properties": {
            "station_id": {"type": "string"},
            "basin_version_id": {"type": "string"},
            "station_name": _null_union({"type": "string"}),
            "name": {
                **_null_union({"type": "string"}),
                "description": "Alias of station_name.",
            },
            "longitude": _null_union({"type": "number"}),
            "latitude": _null_union({"type": "number"}),
            "elevation_m": _null_union({"type": "number"}),
            "elevation": {
                **_null_union({"type": "number"}),
                "description": "Alias of elevation_m.",
            },
            "station_role": {"type": "string"},
            "properties_json": _null_union(dict(_JSON_OBJECT)),
            "created_at": {"type": "string", "format": "date-time"},
        },
    }


def _met_station_page_schema() -> dict:
    """:source: ``PsycopgForecastStore.list_met_stations``
    (forecast_store.py:1127-1149)."""
    return {
        "type": "object",
        "required": ["items", "total_count", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/MetStation"}},
            "total_count": {"type": "integer"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
            "filters": {
                "type": "object",
                "description": (
                    "Applied filters plus per-filter availability so the UI can degrade advanced "
                    "filters honestly (e.g. qc_status unavailable) without errors.\n"
                ),
                "additionalProperties": True,
            },
        },
    }


# --------------------------------------------------------------------------- #
# Pipeline queue and metrics (`apps/api/routes/pipeline.py`)
# --------------------------------------------------------------------------- #


def _queue_depth_schema() -> dict:
    """:source: ``queue_depth`` (apps/api/routes/pipeline.py:873-880)."""
    return {
        "type": "object",
        "required": ["running", "pending", "idle"],
        "properties": {
            "running": {"type": "integer"},
            "pending": {"type": "integer"},
            "idle": {"type": "integer"},
        },
    }


def _stage_duration_metric_schema() -> dict:
    """:source: ``stage_duration_metrics`` (apps/api/routes/pipeline.py:782-790)."""
    return {
        "type": "object",
        "required": ["date", "stage", "average_duration_seconds", "job_count"],
        "properties": {
            "date": {"type": "string", "format": "date"},
            "stage": {"type": "string"},
            "average_duration_seconds": {"type": "number"},
            "job_count": {"type": "integer"},
        },
    }


def _success_rate_metric_schema() -> dict:
    """:source: ``success_rate_metrics`` (apps/api/routes/pipeline.py:823-831)."""
    return {
        "type": "object",
        "required": ["date", "success_rate", "succeeded_cycles", "total_cycles"],
        "properties": {
            "date": {"type": "string", "format": "date"},
            "success_rate": {"type": "number", "minimum": 0, "maximum": 1},
            "succeeded_cycles": {"type": "integer"},
            "total_cycles": {"type": "integer"},
        },
    }
