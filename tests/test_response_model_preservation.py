"""#2348 oracle: a runtime response model never changes a byte-level JSON value.

Before #2348 every JSON route's return annotation was ``dict[str, Any]`` (or
``list[dict[str, Any]]`` for best-available), so FastAPI serialized the handler
object through an implicit ``dict[str, Any]`` field. #2348 gives every route a
typed ``response_model`` in ``apps/api/response_models``. The oracle captures
the object each handler really returns (by wrapping
``fastapi.routing.serialize_response``, the one call FastAPI makes per
response), serializes it through the master field *in the same call*, and
compares the two JSON documents with a TYPE-STRICT walk: ``int`` vs ``float``,
``bool`` vs ``int``, ``null`` vs absent key all differ; key order is ignored.

The only permitted difference is #2222: ``/api/v1/runs`` and
``/api/v1/runs/{run_id}`` project onto the live public ``HydroRun`` key set
(``tests/test_hydro_run_public_projection.py::LIVE_PUBLIC_RUN_KEYS``, taken
from the live pre-state payload), so the expected value there is the master
output filtered to that set -- never the model's own field list.

Samples are handler objects built by fakes whose shapes cite the production
builder, or by the production builders themselves (``_hydro_run_response``,
``_qhh_latest_candidate_response``, ``_qhh_identity_product``,
``_data_source_response`` / ``_cycle_response`` / ``_station_response`` via
the real ``PsycopgForecastStore`` over a row-feeding cursor,
``read_station_forcing_csv`` over a real CSV, ``state_snapshot_to_dict``,
``_selection_row_response``). Every sample lists the routes it must reach, and
``test_every_json_route_is_sampled`` proves the union is the full route table.
Pipeline samples live in ``tests/test_response_model_preservation_pipeline.py``.
"""

from __future__ import annotations

import asyncio
import json
import typing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import fastapi.routing
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from fastapi.utils import create_model_field

from apps.api.main import app
from apps.api.routes import forecast as forecast_routes
from apps.api.routes.best_available import get_best_available_manager
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.models import get_model_registry_store
from apps.api.routes.state_snapshots import get_state_manager
from packages.common.best_available import _selection_row_response
from packages.common.forecast_store import (
    MVP_STATION_VARIABLES,
    _qhh_identity_product,
    _qhh_latest_candidate_response,
)
from packages.common.state_manager import StateSnapshot, state_snapshot_to_dict
from tests.api_contract_helpers import DATA_SOURCE_ROW, forecast_cycle_row, met_station_row
from tests.test_forecast_api_met_station_series import (
    CYCLE_TIME as STATION_CYCLE_TIME,
)
from tests.test_forecast_api_met_station_series import (
    MODEL_ID as STATION_MODEL_ID,
)
from tests.test_forecast_api_met_station_series import (
    SOURCE_ID as STATION_SOURCE_ID,
)
from tests.test_forecast_api_met_station_series import (
    STATION_ID,
    _write_csv,
)
from tests.test_forecast_api_met_station_series import (
    _client as station_series_client,
)
from tests.test_hydro_run_public_projection import (
    LIVE_PUBLIC_RUN_KEYS,
    RUN_ID,
    _LeakingStore,
    _post_i7_row,
    _SqlShapeStore,
)
from tests.test_model_registration import _REGISTRY_CREATED_AT, FakeModelRegistryStore
from tests.test_openapi_response_conformance import (
    _LifecycleModelRegistryStore,
    _RiverSegmentStore,
    _RiverSeriesForecastStore,
    _SplicedForecastStore,
)

# --------------------------------------------------------------------------- #
# Oracle core
# --------------------------------------------------------------------------- #

MASTER_DICT_FIELD = create_model_field(name="Response", type_=dict[str, Any], mode="serialization")
MASTER_LIST_FIELD = create_model_field(name="Response", type_=list[dict[str, Any]], mode="serialization")
# Master annotated best-available `-> list[dict[str, Any]]`; every other route `-> dict[str, Any]`.
MASTER_LIST_ROUTES = frozenset({"GET /api/v1/met/best-available"})
# #2222: the one pair whose expected value is the master output FILTERED.
HYDRO_RUN_DETAIL = "GET /api/v1/runs/{run_id}"
HYDRO_RUN_PAGE = "GET /api/v1/runs"
# Keys `list_runs` (the route) builds around the store page: master handler code.
HYDRO_RUN_PAGE_KEYS = frozenset({"items", "total", "total_count", "limit", "offset"})

# Routes #2348 deliberately leaves unmodelled, with the reason.
OUT_OF_SCOPE_ROUTES: dict[str, str] = {
    "GET /api/v1/runtime/config": "startup wiring, not a business route; frozen runtime contract",
    "GET /health": "liveness probe, not part of the versioned API surface",
    "GET /{full_path:path}": "SPA static fallback (HTML), not JSON",
    "services.slurm_gateway.routes": "internal slurm gateway, separate auth plane and contract",
}
# Routes already modelled before #2348 (layers / precip): outside this oracle.
PRE_MODELLED_ROUTES = frozenset(
    {
        "GET /api/v1/layers",
        "GET /api/v1/layers/discharge/cycles",
        "GET /api/v1/layers/{layer_id}/valid-times",
        "GET /api/v1/precip/{source}/{cycle}/index",
    }
)


@dataclass(frozen=True)
class Capture:
    route: str
    master: Any
    model: Any


@contextmanager
def capturing() -> Iterator[list[Capture]]:
    """Record every JSON response: master serialization vs the route's model."""
    captures: list[Capture] = []
    original = fastapi.routing.serialize_response

    async def recorder(**kwargs: Any) -> Any:
        body = await original(**kwargs)
        route = (kwargs.get("endpoint_ctx") or {}).get("path")
        if kwargs.get("field") is not None and route is not None:
            master_field = MASTER_LIST_FIELD if route in MASTER_LIST_ROUTES else MASTER_DICT_FIELD
            master = await original(
                field=master_field,
                response_content=kwargs["response_content"],
                is_coroutine=kwargs.get("is_coroutine", True),
                dump_json=True,
            )
            captures.append(Capture(route=route, master=json.loads(master), model=json.loads(body)))
        return body

    with mock.patch.object(fastapi.routing, "serialize_response", recorder):
        yield captures


def strict_diff(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if type(expected) is not type(actual):
        return [f"{path}: {type(expected).__name__} {expected!r:.80} != {type(actual).__name__} {actual!r:.80}"]
    if isinstance(expected, dict):
        diffs = []
        if set(expected) != set(actual):
            diffs.append(
                f"{path}: missing {sorted(set(expected) - set(actual))} extra {sorted(set(actual) - set(expected))}"
            )
        for key in sorted(set(expected) & set(actual)):
            diffs.extend(strict_diff(expected[key], actual[key], f"{path}.{key}"))
        return diffs
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        return [d for index, pair in enumerate(zip(expected, actual)) for d in strict_diff(*pair, f"{path}[{index}]")]
    return [] if expected == actual else [f"{path}: {expected!r:.80} != {actual!r:.80}"]


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if key in LIVE_PUBLIC_RUN_KEYS}


def expected_output(capture: Capture) -> Any:
    if capture.route == HYDRO_RUN_DETAIL:
        return {**capture.master, "data": _public_run(capture.master["data"])}
    if capture.route == HYDRO_RUN_PAGE:
        page = {key: value for key, value in capture.master["data"].items() if key in HYDRO_RUN_PAGE_KEYS}
        page["items"] = [_public_run(item) for item in page["items"]]
        return {**capture.master, "data": page}
    return capture.master


def assert_preserved(captures: list[Capture], expected_routes: frozenset[str]) -> None:
    assert {capture.route for capture in captures} == expected_routes
    failures = {
        f"{capture.route} #{index}": diff
        for index, capture in enumerate(captures)
        if (diff := strict_diff(expected_output(capture), capture.model))
    }
    assert not failures, json.dumps(failures, indent=1)


@dataclass
class SampleEnv:
    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    overrides: dict[Any, Callable[[], Any]] = field(default_factory=dict)

    def client(self, dependency: Any, store: Any) -> TestClient:
        app.dependency_overrides[dependency] = lambda: store
        self.overrides[dependency] = app.dependency_overrides[dependency]
        return TestClient(app)


@dataclass(frozen=True)
class Sample:
    drive: Callable[[SampleEnv], None]
    routes: frozenset[str]


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def run_sample(sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = SampleEnv(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    try:
        with capturing() as captures:
            sample.drive(env)
    finally:
        for dependency in env.overrides:
            app.dependency_overrides.pop(dependency, None)
    assert_preserved(captures, sample.routes)


# --------------------------------------------------------------------------- #
# Model registry samples
# --------------------------------------------------------------------------- #

ADMIN = {"X-User-Role": "model_admin"}
GEOM = {"type": "MultiPolygon", "coordinates": [[[[90, 25], [91, 25], [91, 26], [90, 25]]]]}


def _registry_writes(env: SampleEnv) -> None:
    client = env.client(get_model_registry_store, FakeModelRegistryStore())
    version = {"basin_version_id": "basin_v01", "version_label": "v01", "geom": GEOM}
    ok(
        client.post(
            "/api/v1/basins", json={"basin_id": "b", "basin_name": "B", "basin_version": version}, headers=ADMIN
        ),
        201,
    )
    ok(client.post("/api/v1/basins/b/versions", json=version, headers=ADMIN), 201)
    segment = {"river_segment_id": "seg_001", "geom": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]}}
    network = {"basin_version_id": "basin_v01", "version_label": "v01", "segments": [segment]}
    ok(client.post("/api/v1/river-networks", json=network, headers=ADMIN), 201)
    mesh = {
        "basin_version_id": "basin_v01",
        "version_label": "v01",
        "mesh_uri": "s3://nhms/models/m/package/m.sp.mesh",
        "properties_json": {"cells": 1204, "area_km2": 12.5},
    }
    ok(client.post("/api/v1/mesh-versions", json=mesh, headers=ADMIN), 201)
    model = {
        "model_id": "new_model",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
        "mesh_version_id": "basin_mesh_v01",
        "calibration_version_id": "basin_cal_v01",
        "shud_code_version": "2.0",
        "model_package_uri": "s3://nhms/models/new_model/package/",
        "resource_profile": {"cpus": 4, "memory_gb": 7.5},
    }
    ok(client.post("/api/v1/models", json=model, headers=ADMIN), 201)
    crosswalk = {
        "river_network_version_id": "basin_rivnet_v01",
        "entries": [
            {"river_segment_id": "seg_001", "source": "nwm", "external_id": "101", "properties_json": {"w": 1}}
        ],
    }
    ok(client.post("/api/v1/river-segment-crosswalks", json=crosswalk, headers=ADMIN), 201)


class _DatetimeRegistryStore(FakeModelRegistryStore):
    """Registry reads as psycopg returns them: ``created_at`` is a ``datetime``."""

    def __init__(self) -> None:
        super().__init__()
        for model in self.models.values():
            model["created_at"] = _REGISTRY_CREATED_AT

    def list_basins(self, *, limit: int, offset: int, has_display_product: bool = False) -> list[dict[str, Any]]:
        del has_display_product
        basin = {"basin_id": "b", "basin_name": "B", "basin_group": "g", "description": None}
        return [{**basin, "created_at": _REGISTRY_CREATED_AT}][offset : offset + limit]

    def list_basin_versions(self, *, basin_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        valid = {"valid_from": _REGISTRY_CREATED_AT, "valid_to": None, "active_flag": True}
        rows = [
            {**_basin_version(basin_id, "b_v01"), **valid},
            {**_basin_version(basin_id, "b_v00"), "valid_from": None},
        ]
        return rows[offset : offset + limit]


def _basin_version(basin_id: str, basin_version_id: str) -> dict[str, Any]:
    from tests.test_model_registration import _basin_version_record

    return _basin_version_record(basin_id, basin_version_id)


def _registry_reads(env: SampleEnv) -> None:
    client = env.client(get_model_registry_store, _DatetimeRegistryStore())
    ok(client.get("/api/v1/basins"))
    ok(client.get("/api/v1/basins/b/versions"))
    ok(client.get("/api/v1/models", params={"active": "all"}))
    ok(client.get("/api/v1/models/inactive_model"))


def _registry_reads_string_timestamps(env: SampleEnv) -> None:
    from tests.api_contract_helpers import _ModelRegistryStore

    client = env.client(get_model_registry_store, _ModelRegistryStore())
    ok(client.get("/api/v1/basins"))
    ok(client.get("/api/v1/basins/basins_basin_a/versions"))
    ok(client.get("/api/v1/models", params={"active": "all"}))
    ok(client.get("/api/v1/models/inactive_model"))


class _SliceRiverSegmentStore(_RiverSegmentStore):
    """Path C slice features (`model_registry_river_segments.py:655-677`) plus a
    legacy reach feature; the detail row carries a psycopg ``datetime``."""

    def list_river_segments(self, *, limit: int, offset: int, **kwargs: Any) -> dict[str, Any]:
        collection = super().list_river_segments(limit=limit, offset=offset, **kwargs)
        slice_feature = {
            "type": "Feature",
            "id": "seg_1_s2",
            "geometry": {"type": "LineString", "coordinates": [[101.05, 36.05], [101.1, 36.1]]},
            "properties": {
                "segment_id": "seg_1_s2",
                "river_segment_id": "seg_1_s2",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "network_v1",
                "name": "seg_1_s2",
                "stream_order": 1,
                "segment_order": None,
                "length_m": 617,
                "iRiv": 7,
                "iEle": 4412,
                "reach_segment_id": "seg_1",
            },
        }
        collection["features"].append(slice_feature)
        collection["feature_total"] = 2
        return collection

    def get_river_segment(self, **kwargs: Any) -> dict[str, Any]:
        row = super().get_river_segment(**kwargs)
        return {**row, "created_at": _REGISTRY_CREATED_AT, "downstream_segment_id": None, "segment_order": None}


def _river_segments(env: SampleEnv) -> None:
    for store in (_RiverSegmentStore(), _SliceRiverSegmentStore()):
        client = env.client(get_model_registry_store, store)
        params = {"river_network_version_id": "network_v1"}
        ok(client.get("/api/v1/basin-versions/basin_v1/river-segments", params=params))
        ok(client.get("/api/v1/basin-versions/basin_v1/river-segments/seg_1", params=params))


class _AlreadyCurrentStore(FakeModelRegistryStore):
    """The ``already_current`` lifecycle result (`model_registry_lifecycle.py:287-294`)."""

    def model_lifecycle_operation(self, model_id: str, *, operation: str, **kwargs: Any) -> dict[str, Any]:
        preflight = self.preflight_model_operation(model_id, operation=operation)
        preflight["warnings"] = [{"code": "ROLLBACK_ALREADY_CURRENT", "message": "Target is already active."}]
        return {
            "status": "already_current",
            "operation": operation,
            "model": dict(self.models["active_model"]),
            "previous_model": dict(self.models[model_id]),
            "preflight": preflight,
            "audit_reference": None,
        }


def _model_lifecycle(env: SampleEnv) -> None:
    client = env.client(get_model_registry_store, FakeModelRegistryStore())
    ok(client.put("/api/v1/models/inactive_model/active", json={"active": True}, headers=ADMIN))
    client = env.client(get_model_registry_store, FakeModelRegistryStore())
    blocked = ok(client.post("/api/v1/models/active_model/lifecycle", json={"operation": "deactivate"}, headers=ADMIN))
    assert blocked["data"]["status"] == "blocked" and blocked["data"]["preflight"]["blockers"]
    ok(client.post("/api/v1/models/active_model/preflight", json={"operation": "deactivate"}, headers=ADMIN))
    ok(client.post("/api/v1/models/inactive_model/preflight", json={"operation": "activate"}, headers=ADMIN))
    ok(client.post("/api/v1/models/inactive_model/lifecycle", json={"operation": "deprecate"}, headers=ADMIN))
    client = env.client(get_model_registry_store, _AlreadyCurrentStore())
    body = ok(
        client.post("/api/v1/models/inactive_model/lifecycle", json={"operation": "rollback_version"}, headers=ADMIN)
    )
    assert body["data"]["status"] == "already_current"
    client = env.client(get_model_registry_store, _LifecycleModelRegistryStore())
    ok(client.post("/api/v1/models/inactive_model/lifecycle", json={"operation": "activate"}, headers=ADMIN))


# --------------------------------------------------------------------------- #
# Forecast samples
# --------------------------------------------------------------------------- #


class _BareRiverSeriesStore(_RiverSeriesForecastStore):
    """Series entries with the `_forecast_series_metadata` keys omitted, int values."""

    def forecast_series(self, *, segment_id: str, **kwargs: Any) -> dict[str, Any]:
        payload = super().forecast_series(segment_id=segment_id, **kwargs)
        bare = {"scenario_id": "analysis_true_field", "segment_role": "past_3_days", "points": [[1778360400000, 9]]}
        payload["series"].append(bare)
        return payload


class _EmptySeriesStore:
    def __init__(self, *, spliced: bool) -> None:
        self.spliced = spliced

    def forecast_series(self, *, segment_id: str, **_kwargs: Any) -> dict[str, Any]:
        if self.spliced:
            return {
                "segments": [],
                "issue_time": None,
                "river_segment_id": segment_id,
                "variable": "discharge",
                "unit": "m3/s",
            }
        return {"segment_id": segment_id, "issue_time": None, "unit": "m3/s", "series": []}


def _forecast_series(env: SampleEnv) -> None:
    path = "/api/v1/basin-versions/basin_v1/river-segments/seg_1/forecast-series"
    river = {"river_network_version_id": "network_v1"}
    spliced = {**river, "include_analysis": "true"}
    for store, params in (
        (_BareRiverSeriesStore(), river),
        (_SplicedForecastStore(), spliced),
        (_EmptySeriesStore(spliced=False), river),
        (_EmptySeriesStore(spliced=True), spliced),
    ):
        ok(env.client(forecast_routes.get_forecast_store, store).get(path, params=params))


def _runs_through_the_real_store(env: SampleEnv) -> None:
    second = {**_post_i7_row("fcst_ifs_2026092312_dg_model"), "run_key": None, "parsed_at": None, "source": None}
    client = env.client(forecast_routes.get_forecast_store, _SqlShapeStore([_post_i7_row(), second]))
    ok(client.get("/api/v1/runs", params={"limit": 5}))
    client = env.client(forecast_routes.get_forecast_store, _SqlShapeStore([_post_i7_row()]))
    ok(client.get(f"/api/v1/runs/{RUN_ID}"))


def _runs_leaking_store(env: SampleEnv) -> None:
    client = env.client(forecast_routes.get_forecast_store, _LeakingStore())
    ok(client.get(f"/api/v1/runs/{RUN_ID}"))
    ok(client.get("/api/v1/runs", params={"limit": 5}))


QHH_CYCLE = datetime(2026, 5, 7, tzinfo=UTC)
QHH_END = QHH_CYCLE + timedelta(hours=120)


def _qhh_ready_row() -> dict[str, Any]:
    """A candidate row `_qhh_latest_unavailable_reasons` finds nothing wrong with."""
    window = {"valid_time_start": QHH_CYCLE, "valid_time_end": QHH_END}
    coverage = [
        {"variable": variable, "station_count": 386, "sample_count": 46706, "unit_count": 46706}
        | {"quality_flag_count": 46706, "missing_unit_samples": 0, "missing_quality_flag_samples": 0}
        | window
        for variable in MVP_STATION_VARIABLES
    ]
    return {
        "run_id": "qhh_gfs_2026050700",
        "model_id": "basins_qhh_shud",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "forcing_version_id": "forc_qhh",
        "fv_forcing_version_id": "forc_qhh",
        "source_id": "gfs",
        "cycle_time": QHH_CYCLE,
        "forcing_cycle_time": QHH_CYCLE,
        "status": "parsed",
        "forcing_checksum": "sha256:abc",
        "display_start_time": QHH_CYCLE,
        "display_end_time": QHH_END,
        "forcing_start_time": QHH_CYCLE,
        "forcing_end_time": QHH_END,
        "station_count": 386,
        "expected_station_count": 386,
        "station_sample_count": 280236,
        "station_valid_time_start": QHH_CYCLE,
        "station_valid_time_end": QHH_END,
        "station_display_start_time": QHH_CYCLE,
        "station_display_end_time": QHH_END,
        "station_variable_coverage": coverage,
        "segment_count": 1633,
        "expected_segment_count": 1633,
        "river_sample_count": 197593,
        "river_valid_time_start": QHH_CYCLE,
        "river_valid_time_end": QHH_END,
        "max_lead_time_hours": 120,
        "forcing_timeseries_store": "narrow",
    }


class _QhhStore:
    def latest_qhh_display_product(self, source: str, **_kwargs: Any) -> dict[str, Any]:
        evaluation = _qhh_latest_candidate_response(_qhh_ready_row())
        assert evaluation["ready"], evaluation["unavailable_reasons"]
        return evaluation["product"]

    def latest_qhh_product_identity(self, source: str, **_kwargs: Any) -> dict[str, Any]:
        issue_times = ["2026-05-07T00:00:00Z", "2026-05-06T12:00:00Z"]
        return _qhh_identity_product(_qhh_ready_row(), available_issue_times=issue_times)


def _qhh_latest_product(env: SampleEnv) -> None:
    client = env.client(forecast_routes.get_forecast_store, _QhhStore())
    full = ok(client.get("/api/v1/mvp/qhh/latest-product", params={"source": "GFS"}))
    assert full["data"]["quality"]["station_variable_coverage"] and full["data"]["availability"]["quality_notes"]
    ok(client.get("/api/v1/mvp/qhh/latest-product", params={"source": "GFS", "identity_only": "true"}))


# --------------------------------------------------------------------------- #
# Data source, best-available and state snapshot samples
# --------------------------------------------------------------------------- #


def _data_sources_through_the_real_store(env: SampleEnv) -> None:
    rows = [dict(DATA_SOURCE_ROW), {**DATA_SOURCE_ROW, "source_id": "ERA5", "config_json": {}}]
    ok(env.client(get_data_source_store, _SqlShapeStore(rows)).get("/api/v1/data-sources"))
    cycles = [forecast_cycle_row("GFS"), {**forecast_cycle_row("GFS"), "issue_time": None, "retry_count": 2}]
    ok(env.client(get_data_source_store, _SqlShapeStore(cycles)).get("/api/v1/data-sources/GFS/cycles"))
    stations = [met_station_row("basin_v1"), {**met_station_row("basin_v1"), "elevation_m": None, "station_name": None}]
    client = env.client(get_data_source_store, _SqlShapeStore(stations))
    ok(client.get("/api/v1/met/stations", params={"basin_version_id": "basin_v1", "search": "station"}))
    filtered = {"basin_version_id": "basin_v1", "model_id": "m", "variables": "PRCP,TEMP", "qc_status": "ok"}
    ok(client.get("/api/v1/met/stations", params=filtered))


def _station_series_from_a_real_csv(env: SampleEnv) -> None:
    _write_csv(env.tmp_path)
    params = {
        "model_id": STATION_MODEL_ID,
        "source_id": STATION_SOURCE_ID,
        "cycle_time": STATION_CYCLE_TIME,
        "variables": "PRCP,TEMP",
        "limit": 2,
    }
    with station_series_client(env.tmp_path) as client:
        ok(client.get(f"/api/v1/met/stations/{STATION_ID}/series", params=params))
        ok(client.get(f"/api/v1/met/stations/{STATION_ID}/series", params={**params, "limit": 10}))


class _BestAvailableManager:
    def list_selections(self, **_kwargs: Any) -> list[dict[str, Any]]:
        valid = datetime(2026, 4, 20, tzinfo=UTC)
        row = {
            "forcing_version_id": "forcing_era5",
            "valid_time": valid,
            "variable": "prcp_rate_or_amount",
            "selected_source": "ERA5",
            "source_cycle_time": valid,
            "fallback_order": ("ERA5", "GFS"),
            "quality_flag": "best_available_realtime",
        }
        return [
            _selection_row_response(row),
            _selection_row_response({**row, "variable": "temp", "fallback_order": []}),
        ]


def _best_available(env: SampleEnv) -> None:
    client = env.client(get_best_available_manager, _BestAvailableManager())
    ok(client.get("/api/v1/met/best-available", params={"from": "2026-04-20", "to": "2026-04-20"}))


LEGACY_SNAPSHOT = StateSnapshot(
    state_id="state_legacy",
    model_id="demo_model",
    run_id="run_001",
    valid_time=datetime(2026, 4, 28, tzinfo=UTC),
    state_uri="s3://nhms/states/demo_model/2026042800/state.cfg.ic",
    checksum="sha256:aa",
)
CLONED_SNAPSHOT = replace(
    LEGACY_SNAPSHOT,
    state_id="state_clone",
    usable_flag=True,
    created_at=datetime(2026, 4, 29, 1, tzinfo=UTC),
    source_id="GFS",
    cycle_id="gfs_2026042800",
    lead_hours=24,
    model_package_version="v2",
    model_package_checksum="sha256:pp",
    original_shud_filename="demo.cfg.ic",
    cloned_from_state_id="state_legacy",
    cloned_from_model_id="demo_model_v1",
    clone_gate_fingerprint="fp",
)


class _StateManager:
    def list_state_snapshots(self, *, limit: int, offset: int, **_kwargs: Any) -> dict[str, Any]:
        items = [state_snapshot_to_dict(CLONED_SNAPSHOT), state_snapshot_to_dict(LEGACY_SNAPSHOT)]
        return {"total_count": 2, "items": items, "limit": limit, "offset": offset}

    def get_state_snapshot(self, state_id: str) -> StateSnapshot:
        return {"state_clone": CLONED_SNAPSHOT, "state_legacy": LEGACY_SNAPSHOT}[state_id]


def _state_snapshots(env: SampleEnv) -> None:
    client = env.client(get_state_manager, _StateManager())
    ok(client.get("/api/v1/state-snapshots"))
    ok(client.get("/api/v1/state-snapshots/state_clone"))
    ok(client.get("/api/v1/state-snapshots/state_legacy"))


def _routes(*keys: str) -> frozenset[str]:
    return frozenset(keys)


SAMPLES: dict[str, Sample] = {
    "registry_writes": Sample(
        _registry_writes,
        _routes(
            "POST /api/v1/basins",
            "POST /api/v1/basins/{basin_id}/versions",
            "POST /api/v1/river-networks",
            "POST /api/v1/mesh-versions",
            "POST /api/v1/models",
            "POST /api/v1/river-segment-crosswalks",
        ),
    ),
    "registry_reads": Sample(
        _registry_reads,
        _routes(
            "GET /api/v1/basins",
            "GET /api/v1/basins/{basin_id}/versions",
            "GET /api/v1/models",
            "GET /api/v1/models/{model_id}",
        ),
    ),
    "registry_reads_string_timestamps": Sample(
        _registry_reads_string_timestamps,
        _routes(
            "GET /api/v1/basins",
            "GET /api/v1/basins/{basin_id}/versions",
            "GET /api/v1/models",
            "GET /api/v1/models/{model_id}",
        ),
    ),
    "river_segments_reach_and_slice": Sample(
        _river_segments,
        _routes(
            "GET /api/v1/basin-versions/{basin_version_id}/river-segments",
            "GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}",
        ),
    ),
    "model_lifecycle_allowed_blocked_already_current": Sample(
        _model_lifecycle,
        _routes(
            "PUT /api/v1/models/{model_id}/active",
            "POST /api/v1/models/{model_id}/lifecycle",
            "POST /api/v1/models/{model_id}/preflight",
        ),
    ),
    "forecast_series_river_spliced_and_empty": Sample(
        _forecast_series,
        _routes("GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"),
    ),
    "runs_through_the_real_store": Sample(_runs_through_the_real_store, _routes(HYDRO_RUN_PAGE, HYDRO_RUN_DETAIL)),
    "runs_leaking_store_filtered": Sample(_runs_leaking_store, _routes(HYDRO_RUN_PAGE, HYDRO_RUN_DETAIL)),
    "qhh_latest_product_full_and_identity_only": Sample(
        _qhh_latest_product, _routes("GET /api/v1/mvp/qhh/latest-product")
    ),
    "data_sources_cycles_stations_through_the_real_store": Sample(
        _data_sources_through_the_real_store,
        _routes("GET /api/v1/data-sources", "GET /api/v1/data-sources/{source_id}/cycles", "GET /api/v1/met/stations"),
    ),
    "station_series_from_a_real_csv": Sample(
        _station_series_from_a_real_csv, _routes("GET /api/v1/met/stations/{station_id}/series")
    ),
    "best_available": Sample(_best_available, _routes("GET /api/v1/met/best-available")),
    "state_snapshots_legacy_and_clone": Sample(
        _state_snapshots, _routes("GET /api/v1/state-snapshots", "GET /api/v1/state-snapshots/{state_id}")
    ),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_response_model_preserves_the_master_json(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_sample(SAMPLES[name], tmp_path, monkeypatch)


def test_leaking_store_sample_really_differs_from_master() -> None:
    # Vacuity guard for the one filtered pair: master carried the internal keys.
    with capturing() as captures:
        app.dependency_overrides[forecast_routes.get_forecast_store] = _LeakingStore
        try:
            ok(TestClient(app).get(f"/api/v1/runs/{RUN_ID}"))
        finally:
            app.dependency_overrides.pop(forecast_routes.get_forecast_store, None)
    (capture,) = captures
    assert {"timeseries_store", "authority_internal"} <= set(capture.master["data"])
    assert strict_diff(capture.master, capture.model)
    assert not strict_diff(expected_output(capture), capture.model)


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ({"a": 1}, {"a": 1.0}),
        ({"a": True}, {"a": 1}),
        ({"a": None}, {}),
        ({"a": "2026-05-07T00:00:00Z"}, {"a": "2026-05-07T00:00:00+00:00"}),
    ],
)
def test_strict_diff_distinguishes_int_float_bool_null_absent_and_reformatted_time(expected: Any, actual: Any) -> None:
    assert strict_diff(expected, actual)
    assert not strict_diff(expected, expected)


def _in_scope_route_objects() -> dict[str, APIRoute]:
    routes = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.endpoint.__module__.startswith("apps.api.routes."):
            continue
        key = f"{sorted(route.methods)[0]} {route.path}"
        if route.response_model is None or key in PRE_MODELLED_ROUTES:
            continue
        routes[key] = route
    return routes


def _in_scope_json_routes() -> set[str]:
    return set(_in_scope_route_objects())


def _is_response_model(annotation: Any) -> bool:
    args = typing.get_args(annotation)
    if args:  # `list[Model]` / `A | B`
        return all(_is_response_model(arg) for arg in args)
    return isinstance(annotation, type) and annotation.__module__.startswith("apps.api.response_models.")


def test_every_json_route_is_sampled() -> None:
    from tests.test_response_model_preservation_pipeline import PIPELINE_SAMPLES

    sampled = set().union(*(sample.routes for sample in (*SAMPLES.values(), *PIPELINE_SAMPLES.values())))
    in_scope = _in_scope_json_routes()
    assert len(in_scope) == 35, sorted(in_scope)
    assert sampled == in_scope, {"unsampled": sorted(in_scope - sampled), "unknown": sorted(sampled - in_scope)}
    # ...and each is typed by an `apps.api.response_models` model, not by the
    # implicit `dict[str, Any]` FastAPI infers from the return annotation.
    untyped = {key for key, route in _in_scope_route_objects().items() if not _is_response_model(route.response_model)}
    assert not untyped, sorted(untyped)
    # Every business route module's JSON route carries a response model, and the
    # routes left out are exactly the documented out-of-scope set.
    others = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        key = f"{sorted(route.methods)[0]} {route.path}"
        module = route.endpoint.__module__
        if key in in_scope or key in PRE_MODELLED_ROUTES:
            continue
        if module.startswith("apps.api.routes.") and route.response_model is None:
            continue  # binary tiles / PNG / basemap proxy: return a Response object
        others.add(module if module == "services.slurm_gateway.routes" else key)
    assert others == set(OUT_OF_SCOPE_ROUTES)


def test_master_fields_are_the_implicit_annotations() -> None:
    # The master field is what FastAPI built for `-> dict[str, Any]` with no response_model.
    probe = fastapi.FastAPI()

    @probe.get("/probe")
    def probe_route() -> dict[str, Any]:
        return {}

    (route,) = [route for route in probe.routes if isinstance(route, APIRoute)]
    assert route.response_field.field_info.annotation == MASTER_DICT_FIELD.field_info.annotation
    value = {"t": datetime(2026, 5, 7, tzinfo=UTC), "i": 1, "f": 1.0}
    rendered = asyncio.run(
        fastapi.routing.serialize_response(field=route.response_field, response_content=value, dump_json=True)
    )
    master = asyncio.run(
        fastapi.routing.serialize_response(field=MASTER_DICT_FIELD, response_content=value, dump_json=True)
    )
    assert rendered == master == b'{"t":"2026-05-07T00:00:00Z","i":1,"f":1.0}'
