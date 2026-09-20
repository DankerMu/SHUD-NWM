"""Published OpenAPI document, generated frontend types and display control-plane contracts.

The retained path of the #2074 partition of the former 2,132-line
`tests/test_api_contract.py`. This partition owns the cases that read the
CONTRACT ARTEFACTS — the committed `openapi/nhms.v1.yaml`, the runtime
`app.openapi()` document and the generated `apps/frontend/src/api/types.ts` —
rather than route behaviour through a client:

- generated-frontend-type equality against the committed schema,
- the station-series and QHH-latest store contracts as published shapes,
- the display layer catalog / tile control plane and its static-vs-runtime
  no-drift comparisons.

The path is deliberately retained rather than renamed: `openapi/**` selection,
`PRECIP_SURFACE_TESTS`, the `services/tiles/mvt.py` and
`apps/api/routes/hydro_display*.py` `PathTestRule` entries, the
`apps/api/routes/hydro_display_catalog.py` row of `GUARDED_MODULE_CLOSURES` and
`openspec/specs/api-contract-convergence/spec.md` all pin this literal string,
and this partition is the one that keeps the assertions each of them names.

Route-behaviour cases live in `tests/test_api_contract_pipeline_ops.py` and
`tests/test_api_contract_resources.py`; the shared doubles live in
`tests/api_contract_helpers.py`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from apps.api.main import app
from apps.api.routes import hydro_display as hydro_display_routes
from apps.api.routes import hydro_display_catalog as hydro_display_catalog_module
from tests.api_contract_helpers import (
    STATION_SERIES_MISSING_REQUIRED_FILTER_MESSAGE,
    _response_error_codes,
)

# The exact pinned openapi-typescript package for the generated-type byte
# comparison. Runs via `npx --yes <exact-pin>` so the hosted targeted Unit Tests
# job (no pnpm install) can resolve it without a local node_modules.
OPENAPI_TYPESCRIPT_PACKAGE = "openapi-typescript@7.13.0"


def test_generated_frontend_types_match_openapi(tmp_path: Path) -> None:
    generated = tmp_path / "generated-types.ts"
    subprocess.run(
        [
            "npx",
            "--yes",
            OPENAPI_TYPESCRIPT_PACKAGE,
            "../../openapi/nhms.v1.yaml",
            "--output",
            str(generated),
        ],
        cwd=Path(__file__).resolve().parents[1] / "apps" / "frontend",
        check=True,
    )
    committed = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8")


def test_generated_frontend_types_include_model_page_shapes() -> None:
    types_path = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    generated_types = types_path.read_text(encoding="utf-8")

    assert "active?: \"true\" | \"false\" | \"all\";" in generated_types
    assert "list_models_api_v1_models_get:" in generated_types
    assert "get_model_api_v1_models__model_id__get:" in generated_types


def test_station_series_openapi_and_generated_types_include_store_contract() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    operation = spec["paths"]["/api/v1/met/stations/{station_id}/series"]["get"]
    parameters: dict[str, dict[str, Any]] = {}
    for parameter in operation["parameters"]:
        if "$ref" in parameter:
            parameter = spec["components"]["parameters"][parameter["$ref"].removeprefix("#/components/parameters/")]
        parameters[parameter["name"]] = parameter

    assert operation["operationId"] == "getMetStationSeries"
    assert set(parameters) == {
        "station_id",
        "forcing_version_id",
        "model_id",
        "source_id",
        "cycle_time",
        "variables",
        "from",
        "to",
        "limit",
    }
    assert parameters["variables"]["schema"] == {
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    assert "PRCP, TEMP, RH, wind, and Rn" in parameters["variables"]["description"]
    assert "Press" not in parameters["variables"]["description"]
    for name in ("forcing_version_id", "model_id", "source_id"):
        assert parameters[name]["schema"] == {"type": "string", "minLength": 1}
    assert parameters["forcing_version_id"]["deprecated"] is True
    assert "disk-only route ignores this value" in parameters["forcing_version_id"]["description"]
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 10000,
    }
    response_data = operation["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"][
        "data"
    ]
    assert response_data["$ref"] == "#/components/schemas/StationSeriesResponse"
    schemas = spec["components"]["schemas"]
    assert schemas["StationSeriesResponse"]["required"] == [
        "station_id",
        "station",
        "forcing_version_id",
        "source_id",
        "limit",
        "series",
    ]
    assert "quality_flag" in schemas["StationSeriesPoint"]["properties"]
    assert "native_resolution" in schemas["StationSeries"]["properties"]
    assert schemas["StationSeries"]["properties"]["variable"]["enum"] == ["PRCP", "TEMP", "RH", "wind", "Rn"]
    assert "returned_points" in schemas["StationSeriesMetadata"]["properties"]
    operation_text = json.dumps(operation, sort_keys=True)
    assert "FORCING_VERSION_NOT_FOUND" not in operation_text
    assert "FORCING_VERSION_NOT_FINALIZED" not in operation_text
    assert {
        example["value"]["error"]["code"]
        for example in operation["responses"]["4XX"]["content"]["application/json"]["examples"].values()
    } == {
        "STATION_NOT_FOUND",
        "MISSING_REQUIRED_FILTER",
        "STATION_FORCING_FILE_NOT_FOUND",
    }
    missing_required_filter = operation["responses"]["4XX"]["content"]["application/json"]["examples"][
        "missingRequiredFilter"
    ]["value"]["error"]
    assert missing_required_filter["message"] == STATION_SERIES_MISSING_REQUIRED_FILTER_MESSAGE
    assert missing_required_filter["details"] == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }
    assert {
        example["value"]["error"]["code"]
        for example in operation["responses"]["5XX"]["content"]["application/json"]["examples"].values()
    } == {
        "STATION_FORCING_FILENAME_MISSING",
        "STATION_FORCING_FILE_MALFORMED",
    }
    assert schemas["ErrorResponse"]["properties"]["error"]["properties"]["details"] == {
        "oneOf": [
            {"type": ["object", "null"], "additionalProperties": True},
            {
                "type": "array",
                "items": {"$ref": "#/components/schemas/ValidationErrorDetail"},
            },
        ]
    }
    assert schemas["ValidationErrorDetail"] == {
        "type": "object",
        "required": ["field", "reason"],
        "properties": {
            "field": {"type": "string"},
            "rejected_value": {
                "oneOf": [
                    {"type": ["string", "null"]},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "object", "additionalProperties": True},
                    {"type": "array", "items": {}},
                ]
            },
            "reason": {"type": "string"},
        },
        "additionalProperties": True,
    }

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    operation_start = generated_types.index("getMetStationSeries:")
    list_runs_start = generated_types.index("list_state_snapshots_api_v1_state_snapshots_get:")
    operation_types = generated_types[operation_start:list_runs_start]
    assert "forcing_version_id?: string;" in operation_types
    assert "model_id?: string;" in operation_types
    assert "source_id?: string;" in operation_types
    assert "cycle_time?: string;" in operation_types
    assert "variables?: string | string[];" in operation_types
    assert "from?: string;" in operation_types
    assert "to?: string;" in operation_types
    assert "limit?: number;" in operation_types
    assert 'data: components["schemas"]["StationSeriesResponse"];' in operation_types
    assert "StationSeriesResponse:" in generated_types
    assert "StationSeriesPoint:" in generated_types
    station_series_start = generated_types.index("StationSeries:")
    station_series_response_start = generated_types.index("StationSeriesResponse:")
    station_series_types = generated_types[station_series_start:station_series_response_start]
    assert 'variable: "PRCP" | "TEMP" | "RH" | "wind" | "Rn";' in station_series_types
    assert "Press" not in station_series_types
    assert "quality_flag: string | null;" in generated_types
    assert "native_resolution: string | null;" in generated_types
    error_start = generated_types.index("ErrorResponse:")
    validation_detail_start = generated_types.index("ValidationErrorDetail:")
    error_types = generated_types[error_start:validation_detail_start]
    assert "details?: ({" in error_types
    assert "} | null) | components[\"schemas\"][\"ValidationErrorDetail\"][];" in error_types
    assert "rejected_value?: (string | null) | number | boolean | {" in generated_types
    assert "ValidationErrorDetail:" in generated_types


def test_qhh_latest_product_openapi_and_generated_types_include_bootstrap_contract() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    operation = spec["paths"]["/api/v1/mvp/qhh/latest-product"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert operation["operationId"] == "getQhhLatestProduct"
    assert set(parameters) == {"source", "basin_id", "run_id", "cycle_time", "model_id", "identity_only"}
    assert parameters["source"]["required"] is True
    assert parameters["source"]["schema"] == {"type": "string", "enum": ["GFS", "IFS"]}
    for parameter_name in ("basin_id", "run_id", "cycle_time", "model_id"):
        assert parameters[parameter_name]["required"] is False
        assert parameters[parameter_name]["schema"]["type"] == "string"
    assert parameters["cycle_time"]["schema"]["format"] == "date-time"
    assert parameters["identity_only"]["required"] is False
    assert parameters["identity_only"]["schema"] == {"type": "boolean", "default": False}
    response_data = operation["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"][
        "data"
    ]
    assert response_data["$ref"] == "#/components/schemas/QhhLatestProduct"
    schemas = spec["components"]["schemas"]
    latest = schemas["QhhLatestProduct"]
    assert {
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
        "valid_time_start",
        "valid_time_end",
        "available_horizon_hours",
        "shorter_horizon",
        "availability",
        "quality",
    } <= set(latest["required"])
    assert latest["properties"]["source_id"]["enum"] == ["GFS", "IFS"]
    assert latest["properties"]["status"]["enum"] == ["ready", "unavailable"]
    assert schemas["QhhLatestAvailability"]["required"] == [
        "ready",
        "unavailable_reasons",
        "quality_flags",
        "quality_notes",
    ]
    assert schemas["QhhLatestQuality"]["properties"]["station_variable_coverage"]["items"]["$ref"] == (
        "#/components/schemas/QhhLatestStationVariableCoverage"
    )
    assert schemas["QhhLatestQuality"]["required"] == [
        "station_sample_count",
        "river_sample_count",
        "required_station_variables",
        "station_variable_coverage",
        "candidate_limit",
        "search_limit",
        "context_limit",
        "query_indexes",
    ]
    assert "display_end_station_count" not in schemas["QhhLatestStationVariableCoverage"]["required"]
    assert "display_end_station_count" not in schemas["QhhLatestStationVariableCoverage"]["properties"]
    assert schemas["QhhLatestUnavailableReason"]["additionalProperties"] is True

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    operation_start = generated_types.index("getQhhLatestProduct:")
    operation_end = generated_types.index("list_best_available_api_v1_met_best_available_get:")
    operation_types = generated_types[operation_start:operation_end]
    assert 'source: "GFS" | "IFS";' in operation_types
    assert "run_id?: string;" in operation_types
    assert "cycle_time?: string;" in operation_types
    assert "model_id?: string;" in operation_types
    assert 'data: components["schemas"]["QhhLatestProduct"];' in operation_types
    assert "QhhLatestProduct:" in generated_types
    assert 'status: "ready" | "unavailable";' in generated_types
    assert "available_horizon_hours: number | null;" in generated_types
    assert "search_limit: number;" in generated_types
    assert "context_limit: number;" in generated_types
    assert "display_end_station_count: number;" not in generated_types
    assert "QhhLatestUnavailableReason:" in generated_types


def test_layer_metadata_contract_preserves_nullable_generated_type() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    # OpenAPI 3.1 expresses the nullable Layer.metadata as composition-or-null
    # so the generated type stays a union rather than an erroneous intersection.
    assert spec["components"]["schemas"]["Layer"]["properties"]["metadata"] == {
        "anyOf": [
            {
                "type": "object",
                "allOf": [{"$ref": "#/components/schemas/LayerMetadata"}],
            },
            {"type": "null"},
        ]
    }

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    layer_start = generated_types.index("Layer:")
    layer_metadata_start = generated_types.index("LayerMetadata:")
    assert 'metadata?: components["schemas"]["LayerMetadata"] | null;' in generated_types[
        layer_start:layer_metadata_start
    ]


def test_run_scoped_layer_catalog_keeps_discharge_national_source_refs() -> None:
    class _ValidTimes:
        valid_times = ["2026-06-27T12:00:00Z"]
        limit = 24
        observed_count = 1
        truncated = False

    def _fake_valid_times(_session: Any, **_kwargs: Any) -> Any:
        return _ValidTimes()

    def _fake_cycles(_session: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "source": "gfs",
            "cycles": [{
                "cycle_time": "2026-06-27T00:00:00Z",
                "valid_time_start": "2026-06-27T12:00:00Z",
                "valid_time_end": "2026-06-27T12:00:00Z",
            }],
            "default_cycle": "2026-06-27T00:00:00Z",
        }

    def _fake_mvt_enabled(_session: Any) -> bool:
        return False

    # #2026: `_default_layer_catalog` moved to apps/api/routes/hydro_display_catalog.py
    # and resolves these three names from THAT module's globals. The two national
    # discovery reads keep a second home on the facade (three route handlers), so both
    # homes are rebound; `_mvt_live_postgis_enabled` moved with its whole consumer set,
    # so the catalog module is its only home and the facade no longer exposes it.
    catalog = hydro_display_catalog_module
    original_valid_times = catalog.national_discharge_valid_times
    original_cycles = catalog.national_discharge_cycles
    original_mvt_enabled = catalog._mvt_live_postgis_enabled
    original_route_valid_times = hydro_display_routes.national_discharge_valid_times
    original_route_cycles = hydro_display_routes.national_discharge_cycles
    try:
        # Both national discovery symbols must be patched, and both must accept
        # the `(source, cycle)` identity: the discharge branch of
        # `_default_layer_catalog` resolves `default_cycle` from the cycles
        # intersection and then asks for that identity's list. The session here
        # is a bare `object()`, so anything reaching real SQL raises.
        catalog.national_discharge_valid_times = _fake_valid_times  # type: ignore[assignment]
        catalog.national_discharge_cycles = _fake_cycles  # type: ignore[assignment]
        catalog._mvt_live_postgis_enabled = _fake_mvt_enabled  # type: ignore[assignment]
        hydro_display_routes.national_discharge_valid_times = _fake_valid_times  # type: ignore[assignment]
        hydro_display_routes.national_discharge_cycles = _fake_cycles  # type: ignore[assignment]
        layers = hydro_display_routes._default_layer_catalog(
            object(),
            run_id="run_1",
            source_version="run-source-v1",
            basin_version_id="basin_v1",
            river_network_version_id="river_v1",
            river_network_source_version="river-source-v1",
            national_hydro_source_version="national-hydro-v1",
            national_river_source_version="national-river-v1",
            national=False,
        )
    finally:
        catalog.national_discharge_valid_times = original_valid_times  # type: ignore[assignment]
        catalog.national_discharge_cycles = original_cycles  # type: ignore[assignment]
        catalog._mvt_live_postgis_enabled = original_mvt_enabled  # type: ignore[assignment]
        hydro_display_routes.national_discharge_valid_times = original_route_valid_times  # type: ignore[assignment]
        hydro_display_routes.national_discharge_cycles = original_route_cycles  # type: ignore[assignment]

    by_id = {layer.layer_id: layer for layer in layers}
    discharge_metadata = by_id["discharge"].metadata or {}
    river_metadata = by_id["river-network"].metadata or {}
    assert discharge_metadata["source_refs"] == {}
    assert discharge_metadata["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert discharge_metadata["required_placeholders"] == ["source", "cycle", "valid_time", "z", "x", "y"]
    assert discharge_metadata["default_source"] == "gfs"
    assert discharge_metadata["default_cycle"] == "2026-06-27T00:00:00Z"
    assert river_metadata["source_refs"]["basin_version_id"] == "basin_v1"
    assert river_metadata["source_refs"]["river_network_version_id"] == "river_v1"


def test_postgis_tile_params_include_simplification_tolerance_bind() -> None:
    params = hydro_display_routes._postgis_tile_params(
        {"variable": "q_down", "valid_time": datetime(2026, 6, 30, 23, tzinfo=UTC)},
        z=6,
        x=48,
        y=25,
    )

    assert params["simplification_tolerance_m"] == hydro_display_routes.simplification_tolerance_m(6)


def test_display_control_plane_responses_have_no_static_runtime_drift() -> None:
    static_spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8")
    )
    runtime_spec = app.openapi()

    # Every entry declares the full expected code list, so multi-code responses
    # (the #1678 502) sit in the same table as the single-code ones.
    cases = [
        ("/api/v1/runs/{run_id}/retry", "post", "409", ["CONTROL_PLANE_MANUAL_ACTION_REQUIRED"]),
        ("/api/v1/runs/{run_id}/cancel", "post", "409", ["CONTROL_PLANE_MANUAL_ACTION_REQUIRED"]),
        ("/api/v1/queue/depth", "get", "503", ["CONTROL_PLANE_QUEUE_UNAVAILABLE"]),
        # #1678: reachable Slurm gateway failures re-raised by `queue_depth`.
        # 502 -> services/slurm_gateway/real_backend.py:1191/1208/1218 (command)
        # and :1449/:1718/:1734 (sacct parse); 504 -> :1179/:1241 (timeout).
        ("/api/v1/queue/depth", "get", "502", ["SLURM_COMMAND_ERROR", "SLURM_PARSE_ERROR"]),
        ("/api/v1/queue/depth", "get", "504", ["SLURM_TIMEOUT"]),
    ]
    for path, method, status_code, error_codes in cases:
        static_codes = _response_error_codes(static_spec, path, method, status_code)
        runtime_codes = _response_error_codes(runtime_spec, path, method, status_code)
        assert static_codes == error_codes, (path, status_code, static_codes)
        assert runtime_codes == error_codes, (path, status_code, runtime_codes)
        assert static_codes == runtime_codes, (path, status_code)


def test_operations_without_reachable_4xx_declare_none() -> None:
    """#1678: operations whose raise-site audit found no reachable 4XX declare none.

    This pins *declaration*, not reachability: the drift test above only proves
    static == runtime, so a fabricated 4XX added to both `openapi_patching.py`
    and `nhms.v1.yaml` would pass it. Design D5's raise-site audit, re-verified
    2026-09-02:

    - `GET /api/v1/queue/depth` (`apps/api/routes/pipeline.py:822-847`) takes no
      parameters and no auth dependency; the only raise site is re-raising
      `SlurmGatewayError` from `queue_depth`/`list_jobs`, whose reachable
      subclasses are 502 (`SlurmCommandError`, `SlurmParseError`) and 504
      (`SlurmTimeoutError`). 503 comes from the display_readonly guard.
    - `GET /api/v1/slurm/health` (`services/slurm_gateway/routes.py:228-233`)
      only converts a raised `SlurmGatewayError`; neither backend's `health()`
      raises — `real_backend.py:552-580` and `mock_backend.py:222-239` both
      return an `unhealthy` 200 body instead.
    - `GET /health` (`apps/api/startup_wiring.py:52-58`) returns a static dict.

    `"4XX".startswith("4")` is True, so a wildcard 4XX declaration also trips
    this test.
    """
    static_spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8")
    )
    app.openapi_schema = None
    runtime_spec = app.openapi()

    operations = [
        ("/api/v1/queue/depth", "get"),
        ("/api/v1/slurm/health", "get"),
        ("/health", "get"),
    ]
    for spec_name, spec in (("static", static_spec), ("runtime", runtime_spec)):
        for path, method in operations:
            responses = spec["paths"][path][method]["responses"]
            client_error_codes = sorted(code for code in responses if str(code).startswith("4"))
            assert client_error_codes == [], (spec_name, path, method, client_error_codes)


def test_station_mvt_tile_static_runtime_contract() -> None:
    static_spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8")
    )
    app.openapi_schema = None
    runtime_spec = app.openapi()
    path = "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf"

    static_operation = static_spec["paths"][path]["get"]
    runtime_operation = runtime_spec["paths"][path]["get"]
    assert static_operation["operationId"] == runtime_operation["operationId"] == "getMetStationTile"
    assert static_operation["responses"]["200"]["content"] == runtime_operation["responses"]["200"]["content"]
    assert set(static_operation["responses"]["200"]["headers"]) == set(runtime_operation["responses"]["200"]["headers"])
    assert static_operation["responses"]["424"] == runtime_operation["responses"]["424"]


def test_job_log_error_codes_are_declared_in_static_and_runtime_contract() -> None:
    static_spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8")
    )
    runtime_spec = app.openapi()
    expected = [
        "JOB_LOG_NOT_PUBLISHED",
        "JOB_LOG_URI_UNSUPPORTED",
        "JOB_LOG_ACCESS_DENIED",
        "JOB_LOG_NOT_FOUND",
    ]
    path, method = "/api/v1/jobs/{job_id}/logs", "get"
    for status_code in ("400", "403", "404"):
        static_codes = _response_error_codes(static_spec, path, method, status_code)
        runtime_codes = _response_error_codes(runtime_spec, path, method, status_code)
        assert static_codes == expected, (status_code, static_codes)
        assert runtime_codes == expected, (status_code, runtime_codes)
        assert static_codes == runtime_codes, status_code
