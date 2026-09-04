"""Validate real API response bodies against the static OpenAPI 200 schemas.

`tests/test_openapi_drift.py` only proves `openapi/nhms.v1.yaml == app.openapi()`
(both sides can carry the same lie) and `tests/test_api_contract.py` hand-writes
per-field assertions without ever consulting the schema. This module closes the
remaining gap for the 14 routes whose 200 responses were given named component
schemas: it drives each route through `TestClient` with the established stub /
dependency-override machinery and validates the resulting body against the
schema the static document actually declares.

Three properties are asserted per route, because any one of them alone is
vacuous:

1. the operation was really rewritten (its `data` position `$ref`s a non-empty
   named component) -- a route missed by `_set_operation_response_schema` keeps
   FastAPI's default `{type: object, additionalProperties: true}` body, against
   which every instance validates;
2. the real response validates against that schema;
3. deleting or retyping one field makes it fail, and the constraint that fires
   is proven to live *inside* the referenced component -- so an unresolved
   `$ref` (another vacuous pass) cannot masquerade as coverage.

Limit: no `format_checker` is installed, so `format: date-time` / `format: date`
are annotations here, not assertions (jsonschema's default behaviour).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, ValidationError
from referencing.exceptions import Unresolvable

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.forecast import get_forecast_store
from apps.api.routes.models import get_model_registry_store
from tests.test_api_contract import _ModelRegistryStore, _RunStore
from tests.test_monitoring_api import (
    _client,
    _create_job,
    _cycle_time,
    _MockGateway,
    _seed_monitoring_jobs,
    _store,
)
from workers.data_adapters.base import cycle_id_for

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
SUCCESS_ENVELOPE_REF = {"$ref": "#/components/schemas/SuccessEnvelope"}
# What FastAPI emits for a handler annotated `-> dict[str, Any]` with no
# `response_model=`. Every instance validates against it, so a route still
# carrying it documents nothing.
FASTAPI_DEFAULT_RESPONSE_SCHEMA = {"type": "object", "additionalProperties": True}
COMPONENT_REF_PREFIX = "#/components/schemas/"


# --------------------------------------------------------------------------- #
# Stores
#
# `_ModelRegistryStore` / `_RunStore` (tests/test_api_contract.py) and the
# sqlite-backed `_store` / `_client` pair (tests/test_monitoring_api.py) are
# reused verbatim. The stores below are local because no existing stub emits
# these shapes, or emits them thinner than the real store: the shape oracle for
# each is the production projection cited in its docstring.
# --------------------------------------------------------------------------- #


class _RiverSegmentStore(_ModelRegistryStore):
    """Shapes mirror `PsycopgModelRegistryStore.list_river_segments` (feature
    assembly at `packages/common/model_registry.py:1180-1218`) and
    `_river_segment_detail` (`:3580`)."""

    def list_river_segments(self, *, limit: int, offset: int, **_kwargs: Any) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "segment_id": "seg_1",
                        "river_segment_id": "seg_1",
                        "basin_version_id": "basin_v1",
                        "river_network_version_id": "network_v1",
                        "name": "seg_1",
                        "stream_order": 3,
                        "segment_order": 3,
                        "downstream_segment_id": "seg_2",
                        "length_m": 1234.5,
                    },
                    "geometry": {"type": "LineString", "coordinates": [[101.0, 36.0], [101.1, 36.1]]},
                }
            ],
            "total": 1,
            "feature_total": 1,
            "limit": limit,
            "offset": offset,
        }

    def get_river_segment(self, *, segment_id: str, river_network_version_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "river_segment_id": segment_id,
            "river_network_version_id": river_network_version_id,
            "segment_order": 3,
            "downstream_segment_id": "seg_2",
            "length_m": 1234.5,
            "geom": {"type": "LineString", "coordinates": [[101.0, 36.0], [101.1, 36.1]]},
            "properties_json": {"name": "seg_1"},
            "created_at": "2026-05-14T00:00:00Z",
        }


class _MetStationStore:
    """Shape mirrors `PsycopgForecastStore.list_met_stations`
    (`packages/common/forecast_store.py:1026`): the SELECT at `:1107-1116`
    always projects `station_role` and `created_at`, and `_station_response`
    (`:4525`) adds the `name` / `elevation` aliases."""

    def list_met_stations(self, *, basin_version_id: str | None, limit: int, offset: int, **_kwargs: Any) -> dict:
        return {
            "total_count": 1,
            "items": [
                {
                    "station_id": "station_1",
                    "basin_version_id": basin_version_id or "basin_v1",
                    "station_name": "Station 1",
                    "name": "Station 1",
                    "longitude": 101.0,
                    "latitude": 36.0,
                    "elevation_m": 3200.0,
                    "elevation": 3200.0,
                    "station_role": "forcing_proxy",
                    "properties_json": {"source": "fixture"},
                    "created_at": "2026-05-14T00:00:00Z",
                }
            ],
            "limit": limit,
            "offset": offset,
            "filters": {"applied": {}, "available": {"search": True}},
        }


class _SplicedForecastStore:
    """Shape mirrors the spliced branch of `forecast_series`
    (`packages/common/forecast_store.py:4330-4380`), which always writes
    `scenario`, `scenario_id`, `source`, `segment_role` and `data`."""

    def forecast_series(self, *, segment_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "segments": [
                {
                    "scenario": "analysis_true_field",
                    "scenario_id": "analysis_true_field",
                    "source": "ERA5",
                    "segment_role": "past_7_days",
                    "data": [{"valid_time": "2026-05-14T00:00:00Z", "value": 10.0}],
                },
                {
                    "scenario": "forecast_gfs_deterministic",
                    "scenario_id": "forecast_gfs_deterministic",
                    "source": "GFS",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-14T00:00:00Z",
                    "available_lead_hours": 168,
                    "segment_role": "future_7_days",
                    "data": [{"valid_time": "2026-05-15T00:00:00Z", "value": 11.0}],
                },
            ],
            "issue_time": "2026-05-14T00:00:00Z",
            "river_segment_id": segment_id,
            "variable": "discharge",
            "unit": "m3/s",
        }


class _RiverSeriesForecastStore:
    """Shape mirrors the non-spliced branch of `forecast_series`
    (`_forecast_response_from_rows`, `packages/common/forecast_store.py:4012`),
    which groups rows into `series` entries carrying `scenario_id`,
    `segment_role`, `variable` and `[epoch_ms, value]` points."""

    def forecast_series(self, *, segment_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "segment_id": segment_id,
            "issue_time": "2026-05-14T00:00:00Z",
            "unit": "m3/s",
            "series": [
                {
                    "scenario_id": "forecast_gfs_deterministic",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-14T00:00:00Z",
                    "available_lead_hours": 168,
                    "segment_role": "future_7_days",
                    "variable": "q_down",
                    "points": [[1778371200000, 10.0], [1778374800000, 11.0]],
                }
            ],
        }


# --------------------------------------------------------------------------- #
# Response drivers
# --------------------------------------------------------------------------- #


@contextmanager
def _overridden_client(dependency: Any, store: Any, *, allow_dev_role_header: bool = False) -> Iterator[TestClient]:
    app.dependency_overrides[dependency] = lambda: store
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    if allow_dev_role_header:
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(dependency, None)
        if allow_dev_role_header:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header


def _json_200(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def _fetch_basins() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore()) as client:
        return _json_200(client.get("/api/v1/basins"))


def _fetch_basin_versions() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore()) as client:
        return _json_200(client.get("/api/v1/basins/basins_basin_a/versions"))


def _fetch_river_segments() -> Any:
    with _overridden_client(get_model_registry_store, _RiverSegmentStore()) as client:
        return _json_200(
            client.get(
                "/api/v1/basin-versions/basin_v1/river-segments",
                params={"river_network_version_id": "network_v1"},
            )
        )


def _fetch_river_segment_detail() -> Any:
    with _overridden_client(get_model_registry_store, _RiverSegmentStore()) as client:
        return _json_200(
            client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_1",
                params={"river_network_version_id": "network_v1"},
            )
        )


def _fetch_spliced_forecast_series() -> Any:
    with _overridden_client(get_forecast_store, _SplicedForecastStore()) as client:
        return _json_200(
            client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_1/forecast-series",
                params={"river_network_version_id": "network_v1", "include_analysis": "true"},
            )
        )


def _fetch_river_series_forecast_series() -> Any:
    with _overridden_client(get_forecast_store, _RiverSeriesForecastStore()) as client:
        return _json_200(
            client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_1/forecast-series",
                params={"river_network_version_id": "network_v1"},
            )
        )


def _fetch_models() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore()) as client:
        return _json_200(client.get("/api/v1/models", params={"active": "all", "limit": 10, "offset": 0}))


def _fetch_model_detail() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore()) as client:
        return _json_200(client.get("/api/v1/models/inactive_model"))


def _fetch_model_preflight() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore(), allow_dev_role_header=True) as client:
        return _json_200(
            client.post(
                "/api/v1/models/inactive_model/preflight",
                json={"operation": "activate"},
                headers={"X-User-Role": "model_admin"},
            )
        )


def _fetch_model_lifecycle() -> Any:
    with _overridden_client(get_model_registry_store, _ModelRegistryStore(), allow_dev_role_header=True) as client:
        return _json_200(
            client.post(
                "/api/v1/models/inactive_model/lifecycle",
                json={"operation": "activate"},
                headers={"X-User-Role": "model_admin"},
            )
        )


def _fetch_runs() -> Any:
    with _overridden_client(get_forecast_store, _RunStore()) as client:
        return _json_200(client.get("/api/v1/runs", params={"status": "parsed", "limit": 10, "offset": 0}))


def _fetch_met_stations() -> Any:
    with _overridden_client(get_data_source_store, _MetStationStore()) as client:
        return _json_200(client.get("/api/v1/met/stations", params={"basin_version_id": "basin_v1"}))


def _fetch_stage_duration() -> Any:
    with _store() as store:
        _seed_monitoring_jobs(store, cycle_id=cycle_id_for("GFS", _cycle_time()))
        with _client(store) as client:
            return _json_200(client.get("/api/v1/metrics/stage-duration", params={"days": 30}))


def _fetch_success_rate() -> Any:
    with _store() as store:
        cycle_time = _cycle_time()
        _seed_monitoring_jobs(store, cycle_id=cycle_id_for("GFS", cycle_time))
        success_cycle = cycle_id_for("IFS", cycle_time)
        _create_job(store, job_id="job_success_1", cycle_id=success_cycle, status="succeeded")
        with _client(store) as client:
            return _json_200(client.get("/api/v1/metrics/success-rate", params={"days": 30}))


def _fetch_queue_depth() -> Any:
    with _store() as store:
        with _client(store, _MockGateway(depth={"running": 2, "pending": 3, "idle": 1})) as client:
            return _json_200(client.get("/api/v1/queue/depth"))


# --------------------------------------------------------------------------- #
# Route cases
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Mutation:
    """One field-level corruption that the referenced component must reject.

    `component` names the component schema whose own constraint has to fire;
    `field` is a property declared inside it. `kind="required"` deletes the
    field (the failing subschema is then the component itself), `kind="value"`
    replaces it (the failing subschema is that property's schema inside the
    component). Either way the expected subschema is looked up *through*
    `components.schemas`, which is what makes an unresolved `$ref` detectable.
    """

    component: str
    field: str
    kind: str
    validator: str
    apply: Callable[[Any], None]

    def expected_schema(self) -> Any:
        component_schema = SPEC["components"]["schemas"][self.component]
        if self.kind == "required":
            return component_schema
        return component_schema["properties"][self.field]


@dataclass(frozen=True)
class RouteCase:
    route_id: str
    method: str
    path: str
    fetch: Callable[[], Any]
    mutation: Mutation
    enveloped: bool = True
    # Component names expected at the `data` position (list = one per oneOf branch).
    data_components: tuple[str, ...] = ()
    # Body pointers whose collections must be non-empty, so item-level `$ref`s
    # are actually exercised rather than vacuously skipped.
    non_empty: tuple[tuple[str, ...], ...] = ()


def _pop(body: Any, path: tuple[Any, ...]) -> None:
    node = _walk(body, path[:-1])
    del node[path[-1]]


def _put(body: Any, path: tuple[Any, ...], value: Any) -> None:
    node = _walk(body, path[:-1])
    node[path[-1]] = value


def _walk(body: Any, path: tuple[Any, ...]) -> Any:
    node = body
    for key in path:
        node = node[key]
    return node


ROUTE_CASES: tuple[RouteCase, ...] = (
    RouteCase(
        route_id="GET /api/v1/basins",
        method="get",
        path="/api/v1/basins",
        fetch=_fetch_basins,
        data_components=("Basin",),
        non_empty=(("data",),),
        mutation=Mutation(
            component="Basin",
            field="basin_name",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", 0, "basin_name")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/basins/{basin_id}/versions",
        method="get",
        path="/api/v1/basins/{basin_id}/versions",
        fetch=_fetch_basin_versions,
        data_components=("BasinVersion",),
        non_empty=(("data",),),
        mutation=Mutation(
            component="BasinVersion",
            field="geom",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", 0, "geom")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/basin-versions/{basin_version_id}/river-segments",
        method="get",
        path="/api/v1/basin-versions/{basin_version_id}/river-segments",
        fetch=_fetch_river_segments,
        data_components=("RiverSegmentFeatureCollection",),
        non_empty=(("data", "features"),),
        mutation=Mutation(
            component="RiverSegmentFeature",
            field="geometry",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", "features", 0, "geometry")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}",
        method="get",
        path="/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}",
        fetch=_fetch_river_segment_detail,
        data_components=("RiverSegment",),
        mutation=Mutation(
            component="RiverSegment",
            field="segment_order",
            kind="value",
            validator="anyOf",
            apply=lambda body: _put(body, ("data", "segment_order"), "3"),
        ),
    ),
    RouteCase(
        route_id=(
            "GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series [spliced]"
        ),
        method="get",
        path="/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series",
        fetch=_fetch_spliced_forecast_series,
        enveloped=False,
        data_components=("RiverSeriesResponse", "SplicedForecastResponse"),
        non_empty=(("segments",),),
        mutation=Mutation(
            component="SplicedForecastResponse",
            field="variable",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("variable",)),
        ),
    ),
    RouteCase(
        route_id=(
            "GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series [river-series]"
        ),
        method="get",
        path="/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series",
        fetch=_fetch_river_series_forecast_series,
        enveloped=False,
        data_components=("RiverSeriesResponse", "SplicedForecastResponse"),
        non_empty=(("series",),),
        mutation=Mutation(
            component="RiverSeriesResponse",
            field="unit",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("unit",)),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/models",
        method="get",
        path="/api/v1/models",
        fetch=_fetch_models,
        data_components=("ModelInstancePage",),
        non_empty=(("data", "items"),),
        mutation=Mutation(
            component="ModelInstance",
            field="calibration_version_id",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", "items", 0, "calibration_version_id")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/models/{model_id}",
        method="get",
        path="/api/v1/models/{model_id}",
        fetch=_fetch_model_detail,
        data_components=("ModelInstance",),
        mutation=Mutation(
            component="ModelInstance",
            field="active_flag",
            kind="value",
            validator="type",
            apply=lambda body: _put(body, ("data", "active_flag"), "false"),
        ),
    ),
    RouteCase(
        route_id="POST /api/v1/models/{model_id}/preflight",
        method="post",
        path="/api/v1/models/{model_id}/preflight",
        fetch=_fetch_model_preflight,
        data_components=("ModelOperationPreflight",),
        mutation=Mutation(
            component="ModelOperationPreflight",
            field="status",
            kind="value",
            validator="enum",
            apply=lambda body: _put(body, ("data", "status"), "probably_ready"),
        ),
    ),
    RouteCase(
        route_id="POST /api/v1/models/{model_id}/lifecycle",
        method="post",
        path="/api/v1/models/{model_id}/lifecycle",
        fetch=_fetch_model_lifecycle,
        data_components=("ModelLifecycleResult",),
        mutation=Mutation(
            component="ModelLifecycleResult",
            field="preflight",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", "preflight")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/runs",
        method="get",
        path="/api/v1/runs",
        fetch=_fetch_runs,
        data_components=("HydroRunPage",),
        non_empty=(("data", "items"),),
        mutation=Mutation(
            component="HydroRun",
            field="scenario_id",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", "items", 0, "scenario_id")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/metrics/stage-duration",
        method="get",
        path="/api/v1/metrics/stage-duration",
        fetch=_fetch_stage_duration,
        data_components=("StageDurationMetric",),
        non_empty=(("data",),),
        mutation=Mutation(
            component="StageDurationMetric",
            field="stage",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", 0, "stage")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/metrics/success-rate",
        method="get",
        path="/api/v1/metrics/success-rate",
        fetch=_fetch_success_rate,
        data_components=("SuccessRateMetric",),
        non_empty=(("data",),),
        mutation=Mutation(
            component="SuccessRateMetric",
            field="success_rate",
            kind="value",
            validator="maximum",
            apply=lambda body: _put(body, ("data", 0, "success_rate"), 1.5),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/met/stations",
        method="get",
        path="/api/v1/met/stations",
        fetch=_fetch_met_stations,
        data_components=("MetStationPage",),
        non_empty=(("data", "items"),),
        mutation=Mutation(
            component="MetStation",
            field="station_role",
            kind="required",
            validator="required",
            apply=lambda body: _pop(body, ("data", "items", 0, "station_role")),
        ),
    ),
    RouteCase(
        route_id="GET /api/v1/queue/depth",
        method="get",
        path="/api/v1/queue/depth",
        fetch=_fetch_queue_depth,
        data_components=("QueueDepth",),
        mutation=Mutation(
            component="QueueDepth",
            field="running",
            kind="value",
            validator="type",
            apply=lambda body: _put(body, ("data", "running"), "2"),
        ),
    ),
)

CASE_IDS = tuple(case.route_id for case in ROUTE_CASES)


# --------------------------------------------------------------------------- #
# Schema plumbing
# --------------------------------------------------------------------------- #


def _operation_response_schema(case: RouteCase) -> dict[str, Any]:
    operation = SPEC["paths"][case.path][case.method]
    return operation["responses"]["200"]["content"]["application/json"]["schema"]


def _validation_root(case: RouteCase) -> dict[str, Any]:
    """Wrap the operation schema so fragment-only `$ref`s resolve.

    `jsonschema` resolves `#/components/schemas/X` against the root document it
    was handed, so `components` has to travel with the sub-schema. The negative
    control `test_component_reference_resolution_is_load_bearing` proves this
    key is doing real work.
    """
    return {"allOf": [_operation_response_schema(case)], "components": SPEC["components"]}


def _data_schema(case: RouteCase) -> dict[str, Any]:
    """The sub-schema describing the payload the client actually consumes."""
    schema = _operation_response_schema(case)
    if not case.enveloped:
        return schema
    return schema["allOf"][1]["properties"]["data"]


def _referenced_component_names(schema: dict[str, Any]) -> tuple[str, ...]:
    """Component names `$ref`d directly, through `items`, or through `oneOf`."""
    candidates: list[dict[str, Any]] = []
    if "oneOf" in schema:
        candidates.extend(schema["oneOf"])
    elif "items" in schema:
        candidates.append(schema["items"])
    else:
        candidates.append(schema)
    names: list[str] = []
    for candidate in candidates:
        ref = candidate.get("$ref")
        if ref is None:
            return ()
        assert ref.startswith(COMPONENT_REF_PREFIX), ref
        names.append(ref.removeprefix(COMPONENT_REF_PREFIX))
    return tuple(names)


def _flatten(errors: Any) -> list[ValidationError]:
    """`oneOf`/`anyOf` bury branch failures in `error.context`; flatten them."""
    flattened: list[ValidationError] = []
    for error in errors:
        flattened.append(error)
        flattened.extend(_flatten(error.context))
    return flattened


@pytest.fixture(scope="module")
def route_responses() -> dict[str, Any]:
    return {case.route_id: case.fetch() for case in ROUTE_CASES}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", ROUTE_CASES, ids=CASE_IDS)
def test_route_200_declares_named_component_schema(case: RouteCase) -> None:
    """The operation was really rewritten, not left on the FastAPI default body.

    `_set_operation_response_schema` (`apps/api/openapi_patching.py`) walks the
    document through chained `.get(..., {})` calls and no-ops silently when a
    path or method does not match, so a missed route keeps
    `{type: object, additionalProperties: true}` -- and every later assertion in
    this module would pass vacuously against it.
    """
    schema = _operation_response_schema(case)
    assert schema != FASTAPI_DEFAULT_RESPONSE_SCHEMA
    assert schema.get("additionalProperties") is not True

    if case.enveloped:
        assert list(schema) == ["allOf"]
        assert schema["allOf"][0] == SUCCESS_ENVELOPE_REF
        assert schema["allOf"][1]["required"] == ["data"]

    data_schema = _data_schema(case)
    assert _referenced_component_names(data_schema) == case.data_components, data_schema

    for name in case.data_components:
        component = SPEC["components"]["schemas"][name]
        assert component["type"] == "object"
        assert component["required"], name
        assert component["properties"], name


@pytest.mark.parametrize("case", ROUTE_CASES, ids=CASE_IDS)
def test_route_200_schema_is_a_valid_2020_12_schema(case: RouteCase) -> None:
    Draft202012Validator.check_schema(_validation_root(case))


@pytest.mark.parametrize("case", ROUTE_CASES, ids=CASE_IDS)
def test_real_response_body_validates_against_declared_schema(
    case: RouteCase, route_responses: dict[str, Any]
) -> None:
    body = route_responses[case.route_id]
    for pointer in case.non_empty:
        assert _walk(body, pointer), f"{case.route_id}: {'/'.join(map(str, pointer))} must be non-empty"

    errors = _flatten(Draft202012Validator(_validation_root(case)).iter_errors(body))
    assert not errors, [(list(error.absolute_path), error.message) for error in errors]


@pytest.mark.parametrize("case", ROUTE_CASES, ids=CASE_IDS)
def test_mutated_response_body_is_rejected_by_referenced_component(
    case: RouteCase, route_responses: dict[str, Any]
) -> None:
    """Per-route proof that the declared schema bites, and bites from inside the
    referenced component -- an unresolved `$ref` cannot produce these errors."""
    mutation = case.mutation
    body = copy.deepcopy(route_responses[case.route_id])
    mutation.apply(body)

    errors = _flatten(Draft202012Validator(_validation_root(case)).iter_errors(body))
    assert errors, f"{case.route_id}: mutation of {mutation.component}.{mutation.field} was accepted"

    expected_schema = mutation.expected_schema()
    matching = [
        error
        for error in errors
        if error.validator == mutation.validator and error.schema == expected_schema
    ]
    assert matching, [(error.validator, error.message) for error in errors]
    if mutation.kind == "required":
        assert any(mutation.field in error.message for error in matching)


@pytest.mark.parametrize("case", ROUTE_CASES, ids=CASE_IDS)
def test_component_reference_resolution_is_load_bearing(case: RouteCase, route_responses: dict[str, Any]) -> None:
    """Negative control: drop `components` from the validation root and the same
    body stops being validatable at all. Proves the passing runs above resolved
    the `$ref`s instead of skipping them."""
    root_without_components = {"allOf": [_operation_response_schema(case)]}
    with pytest.raises(Unresolvable):
        Draft202012Validator(root_without_components).validate(route_responses[case.route_id])
