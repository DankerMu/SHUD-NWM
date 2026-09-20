"""Station-series / QHH latest-product / layer component schemas.

Split out of `apps/api/openapi_patching.py` (#2074). Self-contained: these
builders are literal shape constructors with no repository imports.
"""


def _station_series_point_schema() -> dict:
    return {
        "type": "object",
        "required": ["valid_time", "value", "quality_flag"],
        "properties": {
            "valid_time": {"type": "string", "format": "date-time"},
            "value": {"type": "number"},
            "quality_flag": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
        },
    }


def _station_series_station_schema() -> dict:
    return {
        "type": "object",
        "required": ["station_id", "basin_version_id"],
        "properties": {
            "station_id": {"type": "string"},
            "basin_version_id": {"type": "string"},
            "station_name": {"type": "string", "nullable": True},
            "name": {"type": "string", "nullable": True},
            "longitude": {"type": "number", "nullable": True},
            "latitude": {"type": "number", "nullable": True},
            "elevation_m": {"type": "number", "nullable": True},
            "elevation": {"type": "number", "nullable": True},
            "station_role": {"type": "string", "nullable": True},
            "active_flag": {"type": "boolean", "nullable": True},
            "properties_json": {"type": "object", "nullable": True, "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time", "nullable": True},
        },
    }


def _station_series_metadata_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "limit",
            "returned_points",
            "requested_from",
            "requested_to",
            "returned_from",
            "returned_to",
            "truncated",
        ],
        "properties": {
            "limit": {"type": "integer", "minimum": 1},
            "returned_points": {"type": "integer", "minimum": 0},
            "requested_from": {"type": "string", "format": "date-time", "nullable": True},
            "requested_to": {"type": "string", "format": "date-time", "nullable": True},
            "returned_from": {"type": "string", "format": "date-time", "nullable": True},
            "returned_to": {"type": "string", "format": "date-time", "nullable": True},
            "truncated": {"type": "boolean"},
        },
    }


def _station_series_schema() -> dict:
    return {
        "type": "object",
        "required": ["variable", "unit", "native_resolution", "points", "truncated", "metadata"],
        "properties": {
            "variable": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn"]},
            "unit": {"type": "string", "nullable": True},
            "native_resolution": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
            "cycle_time": {"type": "string", "format": "date-time", "nullable": True},
            "points": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/StationSeriesPoint"},
            },
            "truncated": {"type": "boolean"},
            "metadata": {"$ref": "#/components/schemas/StationSeriesMetadata"},
        },
    }


def _station_series_response_schema() -> dict:
    return {
        "type": "object",
        "required": ["station_id", "station", "forcing_version_id", "source_id", "limit", "series"],
        "properties": {
            "station_id": {"type": "string"},
            "station": {"$ref": "#/components/schemas/StationSeriesStation"},
            "forcing_version_id": {"type": "string"},
            "model_id": {"type": "string", "nullable": True},
            "source_id": {"type": "string"},
            "cycle_time": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "limit": {"type": "integer", "minimum": 1},
            "requested_from": {"type": "string", "format": "date-time", "nullable": True},
            "requested_to": {"type": "string", "format": "date-time", "nullable": True},
            "series": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/StationSeries"},
            },
        },
    }


def _qhh_latest_unavailable_reason_schema() -> dict:
    return {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "run_id": {"type": "string", "nullable": True},
            "source_id": {"type": "string", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_quality_note_schema() -> dict:
    return {
        "type": "object",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "expected_horizon_hours": {"type": "integer", "nullable": True},
            "available_horizon_hours": {"type": "integer", "nullable": True},
            "available_end_time": {"type": "string", "format": "date-time", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_station_variable_coverage_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "variable",
            "station_count",
            "sample_count",
            "unit_count",
            "quality_flag_count",
            "missing_unit_samples",
            "missing_quality_flag_samples",
            "valid_time_start",
            "valid_time_end",
        ],
        "properties": {
            "variable": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]},
            "station_count": {"type": "integer", "minimum": 0},
            "sample_count": {"type": "integer", "minimum": 0},
            "unit_count": {"type": "integer", "minimum": 0},
            "quality_flag_count": {"type": "integer", "minimum": 0},
            "missing_unit_samples": {"type": "integer", "minimum": 0},
            "missing_quality_flag_samples": {"type": "integer", "minimum": 0},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
        },
    }


def _qhh_latest_query_index_schema() -> dict:
    return {
        "type": "object",
        "required": ["table", "index", "status", "columns"],
        "properties": {
            "table": {"type": "string"},
            "index": {"type": "string"},
            "status": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "predicate": {"type": "string", "nullable": True},
        },
        "additionalProperties": True,
    }


def _qhh_latest_availability_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "ready",
            "unavailable_reasons",
            "quality_flags",
            "quality_notes",
        ],
        "properties": {
            "ready": {"type": "boolean"},
            "unavailable_reasons": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestUnavailableReason"},
            },
            "quality_flags": {"type": "array", "items": {"type": "string"}},
            "quality_notes": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestQualityNote"},
            },
        },
    }


def _qhh_latest_quality_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "station_sample_count",
            "river_sample_count",
            "required_station_variables",
            "station_variable_coverage",
            "candidate_limit",
            "search_limit",
            "context_limit",
            "query_indexes",
        ],
        "properties": {
            "station_sample_count": {"type": "integer", "minimum": 0},
            "river_sample_count": {"type": "integer", "minimum": 0},
            "required_station_variables": {
                "type": "array",
                "items": {"type": "string", "enum": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]},
            },
            "station_variable_coverage": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestStationVariableCoverage"},
            },
            "candidate_limit": {"type": "integer", "minimum": 1},
            "search_limit": {"type": "integer", "minimum": 1},
            "context_limit": {"type": "integer", "minimum": 1},
            "query_indexes": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/QhhLatestQueryIndex"},
            },
        },
    }


def _qhh_latest_product_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "basin_id",
            "model_id",
            "basin_version_id",
            "river_network_version_id",
            "source_id",
            "cycle_time",
            "run_id",
            "forcing_version_id",
            "station_count",
            "expected_station_count",
            "segment_count",
            "expected_segment_count",
            "status",
            "run_status",
            "valid_time_start",
            "valid_time_end",
            "river_valid_time_start",
            "river_valid_time_end",
            "forcing_valid_time_start",
            "forcing_valid_time_end",
            "available_horizon_hours",
            "expected_horizon_hours",
            "shorter_horizon",
            "availability",
            "quality",
        ],
        "properties": {
            "basin_id": {"type": "string"},
            "model_id": {"type": "string"},
            "basin_version_id": {"type": "string"},
            "river_network_version_id": {"type": "string"},
            "available_issue_times": {
                "type": "array",
                "items": {"type": "string", "format": "date-time"},
                "description": (
                    "Recent forecast cycles (newest first) for the issue-time selector. "
                    "Only populated by the identity_only resolver."
                ),
            },
            "source_id": {"type": "string", "enum": ["GFS", "IFS"]},
            "cycle_time": {"type": "string", "format": "date-time"},
            "run_id": {"type": "string"},
            "forcing_version_id": {"type": "string"},
            "station_count": {"type": "integer", "minimum": 0},
            "expected_station_count": {"type": "integer", "minimum": 0, "nullable": True},
            "segment_count": {"type": "integer", "minimum": 0},
            "expected_segment_count": {"type": "integer", "minimum": 0, "nullable": True},
            "status": {"type": "string", "enum": ["ready", "unavailable"]},
            "run_status": {"type": "string"},
            "valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "river_valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "river_valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "forcing_valid_time_start": {"type": "string", "format": "date-time", "nullable": True},
            "forcing_valid_time_end": {"type": "string", "format": "date-time", "nullable": True},
            "available_horizon_hours": {"type": "integer", "nullable": True},
            "expected_horizon_hours": {"type": "integer"},
            "shorter_horizon": {"type": "boolean"},
            "availability": {"$ref": "#/components/schemas/QhhLatestAvailability"},
            "quality": {"$ref": "#/components/schemas/QhhLatestQuality"},
        },
    }


def _layer_schema() -> dict:
    return {
        "type": "object",
        "required": ["layer_id", "layer_name", "layer_type", "variables"],
        "properties": {
            "layer_id": {"type": "string"},
            "layer_name": {"type": "string"},
            "layer_type": {"type": "string"},
            "variables": {"type": "array", "items": {"type": "string"}},
            "metadata": {
                "type": "object",
                "nullable": True,
                "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
            },
        },
    }


def _layer_valid_times_schema() -> dict:
    return {
        "type": "object",
        "required": ["valid_times", "items", "limit", "observed_count", "truncated"],
        "properties": {
            "valid_times": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "items": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "limit": {"type": "integer"},
            "observed_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }


def _discharge_cycles_schema() -> dict:
    instant = {"type": "string", "format": "date-time"}
    return {
        "type": "object",
        "required": ["source", "cycles", "default_cycle"],
        "properties": {
            "source": {"type": "string", "enum": ["gfs", "ifs"]},
            "cycles": {
                "type": "array",
                "description": (
                    "Cycles every active river network has a display-ready run for, newest first, "
                    "restricted to a 12-day lookback window: a cycle older than the window is not "
                    "listed even when fully covered, so a pipeline stalled for longer than that "
                    "yields an empty list. valid_time_start/valid_time_end are the first and last "
                    "entries of that cycle's 3-hour valid-time list, the same values "
                    "GET /api/v1/layers/discharge/valid-times returns for it."
                ),
                "items": {
                    "type": "object",
                    "required": ["cycle_time", "valid_time_start", "valid_time_end"],
                    "properties": {
                        "cycle_time": instant,
                        "valid_time_start": instant,
                        "valid_time_end": instant,
                    },
                },
            },
            "default_cycle": _nullable(
                {
                    **instant,
                    "description": (
                        "Newest intersected cycle; null when no cycle inside the 12-day lookback "
                        "window covers every active network, including when the whole pipeline is "
                        "stale."
                    ),
                }
            ),
        },
    }


def _nullable(schema: dict) -> dict:
    return {**schema, "nullable": True}


def _layer_metadata_schema() -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    number_array = {"type": "array", "items": {"type": "number"}}
    return {
        "type": "object",
        "required": ["layer_id", "tile_format", "fallback_available", "release_blocking"],
        "properties": {
            "layer_id": {"type": "string"},
            "tile_format": {"type": "string", "enum": ["mvt", "geojson_compatibility", "png"]},
            "url_template": _nullable({"type": "string"}),
            "tile_url_template": _nullable({"type": "string"}),
            "required_placeholders": string_array,
            "maplibre_source_layer": _nullable({"type": "string"}),
            "source_layer": _nullable({"type": "string"}),
            "property_schema_version": _nullable({"type": "string"}),
            "schema_version": _nullable({"type": "string"}),
            "encoder_version": _nullable({"type": "string"}),
            "property_schema": _nullable({"type": "object", "additionalProperties": True}),
            "min_zoom": _nullable({"type": "integer"}),
            "max_zoom": _nullable({"type": "integer"}),
            "bounds_crs": _nullable({"type": "string"}),
            "bounds": _nullable(number_array),
            "wgs84_bounds": _nullable(number_array),
            "valid_times": {"type": "array", "items": {"type": "string", "format": "date-time"}},
            "valid_time_limit": {"type": "integer"},
            "valid_time_observed_count": {"type": "integer"},
            "valid_times_truncated": {"type": "boolean"},
            "source_refs": _nullable(
                {
                    "type": "object",
                    "description": (
                        "Concrete source identity used to resolve non-XYZ route placeholders, including "
                        "run_id, basin_version_id, river_network_version_id, and bounded source_version/run "
                        "revision when advertised by required_placeholders."
                    ),
                    "additionalProperties": True,
                }
            ),
            # National discharge only: the one (source, cycle) identity the
            # catalog advertises, and where the other choices are discoverable.
            # Absent from every other layer's metadata.
            "default_source": {"type": "string", "enum": ["gfs", "ifs"]},
            "default_cycle": _nullable({"type": "string", "format": "date-time"}),
            "cycles_url_template": {"type": "string"},
            "valid_times_url_template": {"type": "string"},
            "source_generation": _nullable({"type": "string"}),
            "cache_layer_id": _nullable({"type": "string"}),
            "route_variable": _nullable({"type": "string"}),
            "legacy_layer_ids": string_array,
            "alias_of": _nullable({"type": "string"}),
            "alias_semantic": _nullable({"type": "string"}),
            "canonical_route_layer_id": _nullable({"type": "string"}),
            "cache_etag": _nullable({"type": "string"}),
            "cache_version": _nullable({"type": "string"}),
            "fallback_available": {"type": "boolean"},
            "fallback_endpoint": _nullable({"type": "string"}),
            "release_blocking": {"type": "boolean"},
            "production_mvt_readiness_claimed": _nullable({"type": "boolean"}),
            # #2010, `precip` only: a PNG overlay addressed by two templates
            # rather than an XYZ tile URL. Absent from every MVT layer's
            # metadata, hence optional. `legend` shares the one
            # `PrecipLegendEntry` component the precip index answers with, so
            # the six colours and thresholds cannot drift between the two
            # surfaces.
            "image_url_template": {"type": "string"},
            "index_url_template": {"type": "string"},
            "legend": {"type": "array", "items": {"$ref": "#/components/schemas/PrecipLegendEntry"}},
            "window_hours": {"type": "integer"},
            "unit": {"type": "string"},
            "palette_version": {"type": "string"},
        },
    }
