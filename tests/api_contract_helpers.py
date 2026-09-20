"""Shared doubles and assertion helpers for the `tests/test_api_contract*` partitions.

The single home of every mock store, gateway double and private assertion helper
the API-contract partitions share (#2074). Moved here verbatim from the former
2,132-line `tests/test_api_contract.py`; nothing is re-implemented and nothing is
duplicated back into a partition, so a store's response shape cannot drift
between the partition that reads it and the partition that reads its sibling.

Deliberately not named `test_*`: it holds no test case, and pytest must not
collect it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.forecast_store import (
    QHH_LATEST_CONTEXT_LIMIT,
    QHH_LATEST_REFLECTED_VALUE_LIMIT,
    QHH_LATEST_SEARCH_LIMIT,
    ForecastStoreError,
)
from tests.test_monitoring_api import _MockGateway

PIPELINE_JOB_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "pipeline_job.schema.json"


STATION_SERIES_MISSING_REQUIRED_FILTER_MESSAGE = (
    "forcing_version_id or model_id, source_id, and cycle_time are required for station series queries."
)


QHH_LATEST_REFLECTED_PREFIX_LIMIT = QHH_LATEST_REFLECTED_VALUE_LIMIT - 3


def _parameter_names(operation: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for parameter in operation.get("parameters", []):
        resolved = _resolve_parameter(parameter, spec)
        names.append(str(resolved["name"]))
    return names


def _resolve_parameter(parameter: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    reference = parameter.get("$ref")
    if not reference:
        return parameter
    _, _, name = reference.rpartition("/")
    return spec["components"]["parameters"][name]


def _response_error_codes(spec: dict[str, Any], path: str, method: str, status_code: str) -> list[str]:
    response = spec["paths"][path][method]["responses"][status_code]
    if "$ref" in response:
        component = response["$ref"].split("/")[-1]
        response = spec["components"]["responses"][component]
    schema = response["content"]["application/json"]["schema"]
    return list(schema["properties"]["error"]["properties"]["code"]["enum"])


def _assert_success_envelope(body: dict[str, Any]) -> Any:
    assert {"request_id", "status", "data"} <= set(body)
    assert body["request_id"]
    assert body["status"] == "ok"
    return body["data"]


def _bounded_qhh_latest_reflected_value(value: Any) -> str:
    text = str(value or "")
    if len(text) <= QHH_LATEST_REFLECTED_VALUE_LIMIT:
        return text
    return f"{text[:QHH_LATEST_REFLECTED_PREFIX_LIMIT]}..."


def _persisted_pipeline_job_statuses() -> list[str]:
    schema = json.loads(PIPELINE_JOB_SCHEMA_PATH.read_text(encoding="utf-8"))
    return list(schema["properties"]["status"]["enum"])


def _assert_schema_example_shape(schema: dict[str, Any], example: dict[str, Any], *, path: str) -> None:
    assert schema.get("type") == "object", path
    for field in schema.get("required", []):
        assert field in example, f"{path}.{field}"
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        assert set(example).issubset(properties), path
    for key, value in example.items():
        if key not in properties:
            continue
        _assert_schema_value_shape(properties[key], value, path=f"{path}.{key}")


def _assert_schema_value_shape(schema: dict[str, Any], value: Any, *, path: str) -> None:
    if "enum" in schema:
        assert value in schema["enum"], path
    schema_type = schema.get("type")
    if schema_type == "object":
        assert isinstance(value, dict), path
        for field in schema.get("required", []):
            assert field in value, f"{path}.{field}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(value).issubset(properties), path
        for key, nested in value.items():
            if key in properties:
                _assert_schema_value_shape(properties[key], nested, path=f"{path}.{key}")
    elif schema_type == "array":
        assert isinstance(value, list), path
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _assert_schema_value_shape(item_schema, item, path=f"{path}[{index}]")
    elif schema_type == "string":
        assert isinstance(value, str), path
        if schema.get("minLength") is not None:
            assert len(value) >= int(schema["minLength"]), path
        if schema.get("format") == "uri":
            parsed = urlparse(value)
            assert parsed.scheme, path
        if schema.get("format") == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif schema_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), path
        if schema.get("minimum") is not None:
            assert value >= schema["minimum"], path
    elif schema_type == "number":
        assert isinstance(value, int | float) and not isinstance(value, bool), path
        if schema.get("minimum") is not None:
            assert value >= schema["minimum"], path
        if schema.get("maximum") is not None:
            assert value <= schema["maximum"], path


class _RetryGateway(_MockGateway):
    def submit_job(self, request: Any) -> dict[str, Any]:
        return {
            "job_id": "slurm_retry_contract",
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }


class _RunStore:
    def __init__(self) -> None:
        self.latest_qhh_calls: list[dict[str, Any]] = []

    def latest_qhh_display_product(
        self,
        source: str,
        *,
        basin_id: str = "basins_qhh",
        run_id: str | None = None,
        cycle_time: datetime | str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        self.latest_qhh_calls.append(
            {"source": source, "run_id": run_id, "cycle_time": cycle_time, "model_id": model_id}
        )
        if run_id or cycle_time or model_id:
            requested_cycle_time = (
                cycle_time.isoformat().replace("+00:00", "Z") if isinstance(cycle_time, datetime) else cycle_time
            )
            requested_run_id = _bounded_qhh_latest_reflected_value(run_id) if run_id is not None else None
            requested_model_id = _bounded_qhh_latest_reflected_value(model_id) if model_id is not None else None
            if (
                source.upper(),
                run_id,
                requested_cycle_time,
                model_id,
            ) != ("GFS", "qhh_gfs_2026050700", "2026-05-07T00:00:00Z", "basins_qhh_shud"):
                raise ForecastStoreError(
                    status_code=404,
                    code="QHH_LATEST_PRODUCT_UNAVAILABLE",
                    message="No usable latest QHH display product is available for source GFS.",
                    details={
                        "source_id": source.upper(),
                        "basin_id": "basins_qhh",
                        "status": "unavailable",
                        "strict_identity": True,
                        "requested_identity": {
                            "source": source.upper(),
                            "source_id": source.upper(),
                            "run_id": requested_run_id,
                            "cycle_time": requested_cycle_time,
                            "model_id": requested_model_id,
                        },
                        "unavailable_reasons": [
                            {
                                "code": "STRICT_IDENTITY_NOT_FOUND",
                                "message": "No candidates.",
                                "requested_identity": {
                                    "source": source.upper(),
                                    "source_id": source.upper(),
                                    "run_id": requested_run_id,
                                    "cycle_time": requested_cycle_time,
                                    "model_id": requested_model_id,
                                },
                            }
                        ],
                    },
                )
        if source.upper() != "GFS":
            raise ForecastStoreError(
                status_code=404,
                code="QHH_LATEST_PRODUCT_UNAVAILABLE",
                message="No usable latest QHH display product is available for source IFS.",
                details={
                    "source_id": source.upper(),
                    "basin_id": "basins_qhh",
                    "status": "unavailable",
                    "unavailable_reasons": [{"code": "NO_CANDIDATES", "message": "No candidates."}],
                },
            )
        return {
            "basin_id": "basins_qhh",
            "model_id": "basins_qhh_shud",
            "basin_version_id": "basins_qhh_vbasins",
            "river_network_version_id": "basins_qhh_rivnet_vbasins",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "run_id": "qhh_gfs_2026050700",
            "forcing_version_id": "forc_qhh_gfs_2026050700_basins_qhh_shud",
            "station_count": 386,
            "expected_station_count": 386,
            "segment_count": 1633,
            "expected_segment_count": 1633,
            "status": "ready",
            "run_status": "parsed",
            "valid_time_start": "2026-05-07T00:00:00Z",
            "valid_time_end": "2026-05-14T00:00:00Z",
            "river_valid_time_start": "2026-05-07T00:00:00Z",
            "river_valid_time_end": "2026-05-14T00:00:00Z",
            "forcing_valid_time_start": "2026-05-07T00:00:00Z",
            "forcing_valid_time_end": "2026-05-14T00:00:00Z",
            "available_horizon_hours": 168,
            "expected_horizon_hours": 168,
            "shorter_horizon": False,
            "availability": {
                "ready": True,
                "unavailable_reasons": [],
                "quality_flags": [],
                "quality_notes": [],
            },
            "quality": {
                "station_sample_count": 12000,
                "river_sample_count": 10000,
                "required_station_variables": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"],
                "station_variable_coverage": [],
                "candidate_limit": QHH_LATEST_SEARCH_LIMIT,
                "search_limit": QHH_LATEST_SEARCH_LIMIT,
                "context_limit": QHH_LATEST_CONTEXT_LIMIT,
                "query_indexes": [
                    {
                        "table": "hydro.hydro_run",
                        "index": "hydro_run_qhh_latest_candidate_idx",
                        "status": "covered_by_latest_product_candidate_index",
                        "columns": [
                            "LOWER(source_id)",
                            "run_type",
                            "basin_version_id",
                            "cycle_time DESC",
                            "run_id DESC",
                        ],
                    }
                ],
            },
        }

    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        now = datetime(2026, 5, 3, tzinfo=UTC)
        return {
            "items": [
                {
                    "run_id": "run_parsed",
                    "run_type": "forecast",
                    "scenario_id": "forecast_gfs_deterministic",
                    "model_id": "model_1",
                    "basin_version_id": "basin_v1",
                    "river_network_version_id": "network_v1",
                    "forcing_version_id": None,
                    "init_state_id": None,
                    "source_id": "GFS",
                    "cycle_time": now.isoformat(),
                    "status": kwargs.get("status") or "parsed",
                    "slurm_job_id": None,
                    "start_time": now.isoformat(),
                    "end_time": (now + timedelta(days=7)).isoformat(),
                    "run_manifest_uri": "object://manifest",
                    "output_uri": None,
                    "log_uri": None,
                    "error_code": None,
                    "error_message": None,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }


class _DataSourceStore:
    def __init__(self) -> None:
        self.station_series_calls: list[dict[str, Any]] = []

    def list_data_sources(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {
            "items": [{"source_id": "GFS", "provider": "NOAA/NCEP", "format": "GRIB2"}],
            "total_count": 1,
            "limit": limit,
            "offset": offset,
        }

    def list_cycles(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "cycle_id": f"{kwargs['source_id']}_2026051400",
                    "source_id": kwargs["source_id"],
                    "status": "raw_complete",
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def list_met_stations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "station_id": "station_1",
                    "basin_version_id": kwargs["basin_version_id"],
                    "active_flag": True,
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def station_series(self, **kwargs: Any) -> dict[str, Any]:
        self.station_series_calls.append(kwargs)
        if kwargs.get("forcing_version_id") == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="FORCING_VERSION_NOT_FOUND",
                message="Forcing version not found: missing",
                details={"forcing_version_id": "missing"},
            )
        return {
            "station_id": kwargs["station_id"],
            "station": {
                "station_id": kwargs["station_id"],
                "basin_version_id": "basin_v1",
                "station_name": "Station 1",
                "name": "Station 1",
                "longitude": 101.0,
                "latitude": 36.0,
                "elevation_m": 3200.0,
                "elevation": 3200.0,
                "station_role": "forcing_proxy",
                "active_flag": True,
                "properties_json": {"source": "fixture"},
            },
            "forcing_version_id": kwargs.get("forcing_version_id") or "forc_qhh_gfs_2026050700",
            "model_id": "qhh_shud_v1",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "valid_time_start": "2026-05-07T00:00:00Z",
            "valid_time_end": "2026-05-14T00:00:00Z",
            "limit": kwargs["limit"],
            "requested_from": "2026-05-07T00:00:00Z",
            "requested_to": "2026-05-07T03:00:00Z",
            "series": [
                {
                    "variable": "PRCP",
                    "unit": "mm/h",
                    "native_resolution": "1h",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "points": [
                        {
                            "valid_time": "2026-05-07T00:00:00Z",
                            "value": 1.0,
                            "quality_flag": "ok",
                            "source_id": "GFS",
                        }
                    ],
                    "truncated": True,
                    "metadata": {
                        "limit": kwargs["limit"],
                        "returned_points": 1,
                        "requested_from": "2026-05-07T00:00:00Z",
                        "requested_to": "2026-05-07T03:00:00Z",
                        "returned_from": "2026-05-07T00:00:00Z",
                        "returned_to": "2026-05-07T00:00:00Z",
                        "truncated": True,
                    },
                }
            ],
        }


class _ModelRegistryStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.models = [
            {
                "model_id": "active_model",
                "model_name": "active_model",
                "basin_id": "basin",
                "basin_name": "Basin",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "network_v1",
                "mesh_version_id": "mesh_v1",
                "calibration_version_id": "calibration_v1",
                "shud_code_version": "2.0",
                "segment_count": 1,
                "mesh_uri": "s3://nhms/models/active_model/package/active.sp.mesh",
                "mesh_checksum": "mesh-sha-active",
                "model_package_uri": "s3://nhms/models/active_model/package/",
                "package_checksum": None,
                "manifest_uri": None,
                "source_inventory_checksum": None,
                "basin_slug": None,
                "shud_input_name": None,
                "source_path": None,
                "resolved_source_path": None,
                "source_uri": None,
                "source_is_symlink": None,
                "active_flag": True,
                "lifecycle_state": "active",
                "resource_profile": {},
                "created_at": "2026-05-14T00:00:00Z",
            },
            {
                "model_id": "inactive_model",
                "model_name": "alias-a",
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "network_v1",
                "mesh_version_id": "mesh_v1",
                "calibration_version_id": "calibration_v1",
                "shud_code_version": "2.0",
                "segment_count": 2,
                "mesh_uri": "s3://nhms/models/inactive_model/vbasins/package/alias-a.sp.mesh",
                "mesh_checksum": "mesh-sha-1",
                "model_package_uri": "s3://nhms/models/inactive_model/package/",
                "package_checksum": "package-sha-1",
                "manifest_uri": "s3://nhms/models/inactive_model/vbasins/manifest.json",
                "source_inventory_checksum": "inventory-sha-1",
                "basin_slug": "basin-a",
                "shud_input_name": "alias-a",
                "source_path": "/volume/data/nwm/Basins/basin-a",
                "resolved_source_path": "/volume/data/nwm/Basins/basin-a",
                "source_uri": "s3://nhms/sources/basin-a",
                "source_is_symlink": False,
                "active_flag": False,
                "lifecycle_state": "inactive",
                "resource_profile": {
                    "manifest_uri": "s3://nhms/models/inactive_model/vbasins/manifest.json",
                    "source_uri": "s3://nhms/sources/basin-a",
                    "lineage": {
                        "source_uris": [
                            "s3://nhms/sources/nested",
                            "/volume/data/nwm/Basins/local-source",
                        ],
                        "note": "s3 label only",
                    },
                },
                "created_at": "2026-05-14T00:00:00Z",
            },
        ]
        self.basin_versions = [
            {
                "basin_version_id": "basins_basin_a_vbasins",
                "basin_id": "basins_basin_a",
                "version_label": "vbasins",
                "geom": {"type": "MultiPolygon", "coordinates": []},
                "active_flag": True,
                "valid_from": None,
                "valid_to": None,
                "source_uri": None,
                "checksum": None,
                "created_at": "2026-05-14T00:00:00Z",
            }
        ]

    def set_model_active(self, model_id: str, active: bool, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((model_id, active))
        return {
            "model_id": model_id,
            "basin_version_id": "basin_v1",
            "river_network_version_id": "network_v1",
            "mesh_version_id": "mesh_v1",
            "calibration_version_id": "calibration_v1",
            "shud_code_version": "2.0",
            "model_package_uri": "s3://nhms/models/model_1/package/",
            "active_flag": active,
            "lifecycle_state": "active" if active else "inactive",
            "resource_profile": {},
            "created_at": "2026-05-14T00:00:00Z",
        }

    def preflight_model_operation(self, model_id: str, *, operation: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "request_id": "contract",
            "operation": operation,
            "status": "ready",
            "model_id": model_id,
            "basin_version_id": "basin_v1",
            "blockers": [],
            "warnings": [],
            "impact": {"downstream_surfaces": ["forecast-routing"]},
        }

    def model_lifecycle_operation(self, model_id: str, *, operation: str, **_kwargs: Any) -> dict[str, Any]:
        model = self.set_model_active(model_id, operation in {"activate", "switch_version", "rollback_version"})
        return {
            "status": "allowed",
            "operation": operation,
            "model": model,
            "preflight": self.preflight_model_operation(model_id, operation=operation),
            "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": 7},
        }

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        del basin_version_id
        items = self.models
        if active is not None:
            items = [item for item in items if item["active_flag"] == active]
        return {"items": items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        for item in self.models:
            if item["model_id"] == model_id:
                return dict(item)
        from packages.common.model_registry import MissingResourceError

        raise MissingResourceError(f"model_id not found: {model_id}")

    def list_basins(
        self, *, limit: int, offset: int, has_display_product: bool = False
    ) -> list[dict[str, Any]]:
        del has_display_product
        return [
            {
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "basin_group": None,
                "description": None,
                "created_at": "2026-05-14T00:00:00Z",
            }
        ][offset : offset + limit]

    def list_basin_versions(self, *, basin_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        del basin_id
        return [dict(item) for item in self.basin_versions[offset : offset + limit]]


class _OversizedRiverSegmentStore(_ModelRegistryStore):
    def list_river_segments(self, **_kwargs: Any) -> dict[str, Any]:
        from packages.common.model_registry import RiverSegmentGeoJsonBudgetError

        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=100,
            serialized_bytes=101,
            scope="collection",
        )

    def get_river_segment(self, **_kwargs: Any) -> dict[str, Any]:
        from packages.common.model_registry import RiverSegmentGeoJsonBudgetError

        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=100,
            serialized_bytes=101,
            scope="detail",
        )


class _ForecastSeriesStore:
    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["include_analysis"] is True
        assert kwargs["run_types"] == ["forecast"]
        assert kwargs["river_network_version_id"] == "network_v1"
        return {
            "segments": [
                {
                    "scenario": "analysis_true_field",
                    "source": "ERA5",
                    "data": [{"valid_time": "2026-05-14T00:00:00Z", "value": 10.0}],
                }
            ],
            "issue_time": "2026-05-14T00:00:00Z",
            "river_segment_id": kwargs["segment_id"],
            "variable": "discharge",
            "unit": "m3/s",
        }
