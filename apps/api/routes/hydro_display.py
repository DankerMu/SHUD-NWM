"""Hydro display read-only API: the router, its nine route handlers and the engine.

Implementation bodies were split out to `hydro_display_constants.py`,
`hydro_display_models.py`, `hydro_display_instants.py`, `hydro_display_catalog.py`,
`hydro_display_identity.py` and `hydro_display_postgis.py` (#2026). This module
stays the facade: every route handler, the `hydro-display` router, and the engine /
pool-sizing / cold-gate / session block stay here, so `monkeypatch.setattr` on this
module object keeps biting the bindings the routes actually resolve.

Where to patch, after the split (getting this wrong makes a test pass while
testing nothing, because a re-export does NOT preserve `monkeypatch.setattr`):

- Patch the OWNER MODULE only — the whole consumer set moved, and the name is
  deliberately absent here so a stale facade patch fails loudly:
  `_mvt_live_postgis_enabled` in `hydro_display_catalog.py`;
  `MVT_MAX_COORDINATES` and `national_discharge_cycle_coverage` in
  `hydro_display_postgis.py`.
- Patch `hydro_display_postgis` for `_fetch_postgis_tile_bytes`.
  `river_network_national_mvt_tile` below calls it through the
  `hydro_display_postgis` module object so that route and the four
  `_fetch_*_tile_bytes` wrappers share ONE patch target. The name is still
  re-exported here because scripts and suites read it, but the facade binding is
  read-only: patching it here is inert.
- Patch BOTH modules for `national_discharge_source_version`,
  `national_discharge_valid_times` and `national_discharge_cycles`: they have two
  live homes. `_default_layer_catalog` resolves them from
  `hydro_display_catalog.py`, while `list_discharge_cycles`,
  `list_layer_valid_times`, `hydro_national_mvt_tile` and
  `hydro_national_source_cycle_mvt_tile` resolve them from here. Dropping either
  half leaves that path on the real implementation (measured: dropping the facade
  half reds the legacy national tile route with a 500).
- Everything else — including `_default_layer_catalog` itself, whose only consumer
  is `list_layers` here — stays patchable on this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Generator
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.display_cache import display_catalog_cached
from apps.api.errors import ApiError
from apps.api.routes import hydro_display_postgis
from apps.api.routes.hydro_display_catalog import _default_layer_catalog, _empty_valid_times
from apps.api.routes.hydro_display_constants import (
    DISPLAY_PRODUCT_READY_STATUSES,
    HYDRO_NATIONAL_SOURCE_ID,
    HYDRO_NATIONAL_SOURCE_VERSION,
    MVT_ROUTE_RESPONSES,
    PUBLIC_LAYER_DEFINITIONS,
    RIVER_NETWORK_NATIONAL_SOURCE_ID,
    SUPPORTED_PUBLIC_LAYER_IDS,
)
from apps.api.routes.hydro_display_constants import TILE_X_DESCRIPTION as TILE_X_DESCRIPTION
from apps.api.routes.hydro_display_constants import TILE_Y_DESCRIPTION as TILE_Y_DESCRIPTION
from apps.api.routes.hydro_display_identity import (
    _require_hydro_mvt_source_identity,
    _require_run_source_identity,
    _station_source_version,
)
from apps.api.routes.hydro_display_instants import (
    Rfc3339Instant,
    _format_time,
    _national_source_cycle_tile_input,
    _require_representable_instant,
    _require_seconds_precision_instant,
    _validated_national_valid_time_selector,
)
from apps.api.routes.hydro_display_models import ApiSuccessEnvelope as ApiSuccessEnvelope
from apps.api.routes.hydro_display_models import DischargeCycle as DischargeCycle
from apps.api.routes.hydro_display_models import DischargeCycles as DischargeCycles
from apps.api.routes.hydro_display_models import (
    DischargeCyclesResponse,
    Layer,
    LayerListResponse,
    LayerValidTimesResponse,
)
from apps.api.routes.hydro_display_models import LayerValidTimes as LayerValidTimes
from apps.api.routes.hydro_display_postgis import (
    _fetch_hydro_mvt_tile_bytes,
    _fetch_hydro_national_mvt_tile_bytes,
    _fetch_river_network_mvt_tile_bytes,
    _fetch_station_mvt_tile_bytes,
    _validate_supported_hydro_variable,
)
from apps.api.routes.hydro_display_postgis import _fetch_postgis_tile_bytes as _fetch_postgis_tile_bytes
from apps.api.routes.hydro_display_postgis import _postgis_tile_params as _postgis_tile_params
from apps.api.routes.pipeline import _ok
from services.tiles.mvt import (
    MVT_MAX_ZOOM,
    MVT_MEDIA_TYPE,
    MVT_SCHEMA_VERSION,
    TileError,
    TileInput,
    TileResponse,
    display_ready_run,
    layer_metadata,
    national_discharge_cycles,
    national_discharge_source_version,
    national_discharge_valid_times,
    national_river_network_source_version,
    public_hydro_layer_id,
    tile_generation_lock,
    valid_times_for_layer,
)
from services.tiles.mvt import build_raw_tile_response as _build_raw_tile_response
from services.tiles.mvt import read_cached_tile_response as _read_cached_tile_response
from services.tiles.mvt import simplification_tolerance_m as simplification_tolerance_m
from services.tiles.mvt import validate_identifier as _validate_tile_identifier
from services.tiles.mvt import validate_xyz as _validate_tile_xyz

router = APIRouter(tags=["hydro-display"])


# #1714: default pg_stat_activity attribution for this component. libpq
# treats fallback_application_name as a default only, so an operator's
# explicit ?application_name=... in DATABASE_URL still wins.
_APPLICATION_NAME = "nhms-display-api"


_POOL_CONFIGURATION_LOCK = threading.Lock()
_DISPLAY_POOL_CONFIGURATION: tuple[int, int] | None = None
_COLD_GATE_LOCK = threading.Lock()
_COLD_GATE: threading.BoundedSemaphore | None = None
_COLD_GATE_LIMIT: int | None = None
MVT_COLD_BUSY_CODE = "MVT_COLD_GENERATION_BUSY"
MVT_COLD_BUSY_MESSAGE = "Cold MVT generation is saturated; retry after the stated delay."
MVT_COLD_BUSY_HEADERS = {"Retry-After": "1", "Cache-Control": "no-store"}


@lru_cache
def _engine(database_url: str) -> Engine:
    pool_size, max_overflow = _display_pool_configuration()
    return create_engine(
        database_url,
        future=True,
        connect_args={"fallback_application_name": _APPLICATION_NAME},
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=10,
        pool_recycle=1800,
    )


def _bounded_env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _display_pool_size() -> int:
    return _bounded_env_int("NHMS_DISPLAY_DB_POOL_SIZE", default=4, minimum=1, maximum=16)


def _display_max_overflow() -> int:
    return _bounded_env_int("NHMS_DISPLAY_DB_MAX_OVERFLOW", default=2, minimum=0, maximum=16)


def _display_pool_configuration() -> tuple[int, int]:
    """The effective bounded pool settings captured once for this process."""
    global _DISPLAY_POOL_CONFIGURATION
    with _POOL_CONFIGURATION_LOCK:
        if _DISPLAY_POOL_CONFIGURATION is None:
            _DISPLAY_POOL_CONFIGURATION = (_display_pool_size(), _display_max_overflow())
        return _DISPLAY_POOL_CONFIGURATION


def _display_pool_capacity() -> int:
    pool_size, max_overflow = _display_pool_configuration()
    return pool_size + max_overflow


def _effective_cold_limit(capacity: int | None = None) -> int:
    """Cold admission strictly below the same bounded pool the engine uses."""
    pool_capacity = _display_pool_capacity() if capacity is None else capacity
    if pool_capacity <= 1:
        return 0
    default = pool_capacity // 2
    raw = os.getenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "").strip()
    try:
        requested = int(raw) if raw else default
    except ValueError:
        requested = default
    if requested < 0:
        requested = default
    return min(requested, pool_capacity - 1)


def _cold_generation_gate() -> tuple[threading.BoundedSemaphore, int]:
    global _COLD_GATE, _COLD_GATE_LIMIT
    with _COLD_GATE_LOCK:
        if _COLD_GATE is None or _COLD_GATE_LIMIT is None:
            limit = _effective_cold_limit()
            _COLD_GATE = threading.BoundedSemaphore(limit)
            _COLD_GATE_LIMIT = limit
        return _COLD_GATE, _COLD_GATE_LIMIT


def _release_session_checkout(session: Session) -> None:
    """Return a session checkout before an admission or single-flight wait."""
    try:
        session.rollback()
    except BaseException as rollback_error:
        try:
            session.invalidate()
        except BaseException as invalidate_error:
            raise invalidate_error from rollback_error
        raise


def _mvt_cold_generation_busy() -> ApiError:
    return ApiError(
        status_code=503,
        code=MVT_COLD_BUSY_CODE,
        message=MVT_COLD_BUSY_MESSAGE,
        headers=MVT_COLD_BUSY_HEADERS,
    )


def get_hydro_display_session() -> Generator[Session, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for hydro display API operations.",
        )
    with Session(_engine(database_url)) as session:
        yield session


def _tile_api_error(exc: TileError) -> ApiError:
    return ApiError(status_code=exc.status_code, code=exc.code, message=exc.message, details=exc.details)


def validate_identifier(value: str, field_name: str) -> None:
    try:
        _validate_tile_identifier(value, field_name)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def validate_xyz(z: int, x: int, y: int, *, max_zoom: int = MVT_MAX_ZOOM) -> None:
    try:
        _validate_tile_xyz(z, x, y, max_zoom=max_zoom)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def build_raw_tile_response(session: Session, tile: TileInput, data: bytes) -> TileResponse:
    try:
        return _build_raw_tile_response(session, tile, data)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


def read_cached_tile_response(session: Session, tile: TileInput) -> TileResponse | None:
    try:
        return _read_cached_tile_response(session, tile)
    except TileError as exc:
        raise _tile_api_error(exc) from exc


@router.get("/api/v1/layers", response_model=LayerListResponse)
def list_layers(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(
        default=0,
        ge=0,
        description=(
            "Zero-based offset into the run's layer catalog; an offset at or beyond "
            "the catalog length yields an empty `data` page (HTTP 200)."
        ),
    ),
    run_id: str | None = Query(default=None),
    session: Session = Depends(get_hydro_display_session),
) -> dict[str, Any]:
    if run_id is not None:
        validate_identifier(run_id, "run_id")

    def _load() -> list[dict[str, Any]]:
        run = _require_display_ready(session, run_id) if run_id is not None else display_ready_run(session)
        if run is None:
            # Precipitation is independent of hydrological run identity. When
            # nothing is display-ready, advertise only that public definition
            # and its existing metadata — no fabricated run, digest, or
            # run-scoped siblings.
            precip = next(definition for definition in PUBLIC_LAYER_DEFINITIONS if definition[0] == "precip")
            layer_id, name, layer_type, variables = precip
            return [
                Layer(
                    layer_id=layer_id,
                    layer_name=name,
                    layer_type=layer_type,
                    variables=variables,
                    metadata=layer_metadata(layer_id),
                ).model_dump()
            ]
        resolved_run_id = str(run["run_id"])
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id="layers")
        source_version = _run_source_version(run)
        river_network_source_version = _river_network_source_version(session, basin_version_id)
        national_river_source_version = national_river_network_source_version(session)
        # No `national_discharge_source_version` call here: the discharge entry's
        # digest is scoped to the `(default_source, default_cycle)` identity the
        # entry advertises, which only `_default_layer_catalog` knows. Same single
        # digest query, two more binds -- not an extra round trip.
        layers = _default_layer_catalog(
            session,
            run_id=resolved_run_id,
            source_version=source_version,
            river_network_source_version=river_network_source_version,
            national_river_source_version=national_river_source_version,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            national=run_id is None,
        )
        return [layer.model_dump() for layer in layers]

    # 分页在缓存**之后**（#2078）：key 不含 `limit`/`offset`，所以客户端可控的无界
    # offset 维度不再制造缓存条目；越界 offset 从缓存值切出 `[]`，不落 DB。切片结果
    # 与从前在 loader 里切片逐字节相同。
    # key 用 `!r` 而不是裸插值：字面量 `?run_id=None` 通过 `SAFE_TILE_IDENTIFIER_RE`，
    # 裸插值会让它折叠进国家级 key（`layers:None`），拿到别人的目录还顺手劫持热 path。
    catalog = display_catalog_cached(request, f"layers:{run_id!r}", _load)
    return _ok(request, catalog[offset : offset + limit])


@router.get("/api/v1/layers/discharge/cycles", response_model=DischargeCyclesResponse)
def list_discharge_cycles(
    request: Request,
    # `Literal`, deliberately not a Python `Enum`, for the same reason as the
    # canonical tile route: an Enum makes FastAPI emit a `$ref` the
    # hand-maintained `openapi/nhms.v1.yaml` would have to mirror twice.
    source: Literal["gfs", "ifs"] = Query(),
    session: Session = Depends(get_hydro_display_session),
) -> dict[str, Any]:
    """Cycles of `source` that EVERY active river network can render, newest first.

    Only cycles inside a 12-day lookback window are considered, so neither this
    list nor the query behind it grows with the pipeline's lifetime; a cycle older
    than the window is not listed even when every network covers it.

    Fail-closed: one active network without a display-ready run for `source` --
    or a pipeline stalled for longer than the window -- yields `cycles: []` and
    `default_cycle: null`, and the national discharge layer renders disabled.
    `source` is rejected by FastAPI itself before this body runs, so a bad or
    missing one costs no SQL.
    """

    def _load() -> dict[str, Any]:
        return national_discharge_cycles(session, source=source)

    return _ok(request, display_catalog_cached(request, f"discharge-cycles:{source}", _load))


@router.get("/api/v1/layers/{layer_id}/valid-times", response_model=LayerValidTimesResponse)
def list_layer_valid_times(
    request: Request,
    layer_id: str,
    run_id: str | None = Query(default=None),
    source: Literal["gfs", "ifs"] | None = Query(default=None),
    cycle: Rfc3339Instant | None = Query(default=None),
    session: Session = Depends(get_hydro_display_session),
) -> dict[str, Any]:
    validate_identifier(layer_id, "layer_id")
    if layer_id not in SUPPORTED_PUBLIC_LAYER_IDS:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Unsupported layer_id for valid-time discovery.",
            details={"layer_id": layer_id, "supported": sorted(SUPPORTED_PUBLIC_LAYER_IDS)},
        )
    cycle_instant = _validated_national_valid_time_selector(
        layer_id=layer_id, run_id=run_id, source=source, cycle=cycle
    )
    requested_run_id = run_id
    cycle_key = _format_time(cycle_instant) if cycle_instant is not None else None

    def _load() -> dict[str, Any]:
        run_id = requested_run_id
        if source is not None and cycle_instant is not None:
            return national_discharge_valid_times(session, source=source, cycle=cycle_instant).model_dump()
        if run_id is None and layer_id == "discharge":
            return national_discharge_valid_times(session).model_dump()
        if layer_id != "discharge":
            return _empty_valid_times().model_dump()
        if run_id is not None:
            validate_identifier(run_id, "run_id")
            run = _require_display_ready(session, run_id)
        else:
            run = display_ready_run(session)
            if run is None:
                return _empty_valid_times().model_dump()
            run_id = str(run["run_id"])
        basin_version_id, river_network_version_id = _require_run_source_identity(run, layer_id=layer_id)
        valid_time_sample = valid_times_for_layer(
            session,
            layer_id,
            run_id=run_id,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
        return valid_time_sample.model_dump()

    # The canonicalized cycle spelling, not the raw query string: `...T12:00:00.000Z`
    # and `...T12:00:00Z` name one instant and must share one cache entry.
    return _ok(
        request,
        display_catalog_cached(
            request,
            # `!r` 隔离客户端可控的维度：字面量 `?run_id=None` 记作 `'None'`，不与
            # 「没给 run_id」的国家级条目同 key（#2078）。`layer_id` 被
            # `SUPPORTED_PUBLIC_LAYER_IDS` 限死，是有界维度。
            f"valid-times:{layer_id}:{requested_run_id!r}:{source!r}:{cycle_key!r}",
            _load,
            # 空 `valid_times`（交集外的 fail-closed cycle、无覆盖的国家级列表、非
            # discharge 图层）不进缓存也不进热 path：`cycle` 是客户端可控的无界维度。
            cacheable=lambda payload: bool(payload["valid_times"]),
        ),
    )


@router.get(
    "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def hydro_mvt_tile(
    run_id: str,
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(run_id, "run_id")
    validate_identifier(variable, "variable")
    _validate_supported_hydro_variable(variable)
    validate_xyz(z, x, y)
    # #2033, range check only -- NOT `_require_seconds_precision_instant`, which
    # would newly 422 the in-range sub-second instants this alias has always
    # accepted. The return is discarded on purpose: everything below keeps
    # receiving the ORIGINAL `valid_time`, so the diff is purely additive and
    # "zero shift for in-range instants" is checkable by inspection. Placed here
    # because `_require_display_ready` and `_require_hydro_mvt_source_identity`
    # are two SQL statements this used to pay before answering 500.
    _require_representable_instant(valid_time, "valid_time")
    run = _require_display_ready(session, run_id)
    basin_version_id, river_network_version_id = _require_run_source_identity(
        run,
        layer_id=public_hydro_layer_id(variable),
    )
    _require_hydro_mvt_source_identity(
        session,
        run_id=run_id,
        variable=variable,
        valid_time=valid_time,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
    )
    tile_input = TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=run_id,
        source_version=_run_source_version(run),
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        lambda: _fetch_hydro_mvt_tile_bytes(
            session,
            run_id=run_id,
            variable=variable,
            valid_time=valid_time,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            z=z,
            x=x,
            y=y,
        ),
    )


@router.get(
    "/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def hydro_national_source_cycle_mvt_tile(
    # `Literal`, deliberately not a Python `Enum`: an Enum makes FastAPI emit a
    # `$ref` into `components/schemas`, which the hand-maintained
    # `openapi/nhms.v1.yaml` would then have to mirror in a second place.
    source: Literal["gfs", "ifs"],
    cycle: Rfc3339Instant,
    variable: str,
    valid_time: Rfc3339Instant,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    """Canonical national discharge tile for one (source, cycle) identity; 424 when it has no display-ready run."""
    # `cycle` / `valid_time` are RFC3339 at seconds precision
    # (`YYYY-MM-DDTHH:MM:SSZ`); `...T12:00:00.000Z`, `...T12:00:00+00:00` and a
    # non-UTC `...T20:00:00+08:00` are accepted and canonicalize onto it. Every
    # check below runs before the session is touched, so a bad variable/z/x/y
    # costs no SQL; `source` and the two instants' STRING SHAPE are rejected by
    # FastAPI itself (`Rfc3339Instant`), ahead of this body, and what remains
    # here is the seconds-precision and in-range checks pydantic cannot express.
    validate_identifier(variable, "variable")
    _validate_supported_hydro_variable(variable)
    validate_xyz(z, x, y)
    cycle_instant = _require_seconds_precision_instant(cycle, "cycle")
    valid_time_instant = _require_seconds_precision_instant(valid_time, "valid_time")
    tile_input = _national_source_cycle_tile_input(
        source=source,
        cycle_text=_format_time(cycle_instant),
        variable=variable,
        valid_time=valid_time_instant,
        z=z,
        x=x,
        y=y,
        # `valid_time` too (#2031): the tile SQL's `latest_runs` clamps candidate
        # runs to the instant's coverage window, so a digest that skips the clamp
        # describes a run this tile may never paint.
        source_digest=national_discharge_source_version(
            session, source=source, cycle=cycle_instant, valid_time=valid_time_instant
        ),
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        lambda: _fetch_hydro_national_mvt_tile_bytes(
            session,
            variable=variable,
            valid_time=valid_time_instant,
            z=z,
            x=x,
            y=y,
            source=source,
            cycle=cycle_instant,
        ),
    )


@router.get(
    "/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def hydro_national_mvt_tile(
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(variable, "variable")
    _validate_supported_hydro_variable(variable)
    validate_xyz(z, x, y)
    # #2033, range check only (design D3), return discarded (design D4): the
    # original `valid_time` is what the digest, the cache key and the tile SQL
    # below all keep receiving. It must precede the `TileInput(...)` construction,
    # not follow it -- the `source_version=` kwarg calls
    # `national_discharge_source_version`, which is the one SQL round trip this
    # route used to pay before answering 500.
    _require_representable_instant(valid_time, "valid_time")
    tile_input = TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=HYDRO_NATIONAL_SOURCE_ID,
        # `source`/`cycle` stay NULL — this alias binds no identity — but the
        # instant is bound (#2031), and it is the SAME `valid_time` object the
        # tile SQL below is given, so the digest ranks the run the tile reads.
        # Bytes and the 200/424 verdict are unchanged; the cache key rotates once
        # for instants outside the overall-latest run's window.
        source_version=(
            f"{HYDRO_NATIONAL_SOURCE_VERSION}:"
            f"{national_discharge_source_version(session, valid_time=valid_time)}"
        ),
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        # Non-canonical alias: unchanged run selection, unchanged bytes,
        # unchanged 200/424 verdict. `source`/`cycle` are bound NULL rather
        # than omitted because `text()` raises on a missing named bind.
        lambda: _fetch_hydro_national_mvt_tile_bytes(
            session, variable=variable, valid_time=valid_time, z=z, x=x, y=y, source=None, cycle=None
        ),
    )


@router.get(
    "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def river_network_national_mvt_tile(
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id="river-network",
        source_id=RIVER_NETWORK_NATIONAL_SOURCE_ID,
        source_version=national_river_network_source_version(session),
        valid_time=None,
        z=z,
        x=x,
        y=y,
        variant_id="national",
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        lambda: hydro_display_postgis._fetch_postgis_tile_bytes(session, "river-network-national", {}, z=z, x=x, y=y),
    )


@router.get(
    "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
)
def river_network_mvt_tile(
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id="river-network",
        source_id=basin_version_id,
        source_version=_river_network_source_version(session, basin_version_id),
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        lambda: _fetch_river_network_mvt_tile_bytes(
            session, basin_version_id=basin_version_id, z=z, x=x, y=y
        ),
    )


@router.get(
    "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
    responses=MVT_ROUTE_RESPONSES,
    response_class=Response,
    operation_id="getMetStationTile",
)
def met_station_mvt_tile(
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_hydro_display_session),
) -> Response:
    validate_identifier(basin_version_id, "basin_version_id")
    validate_xyz(z, x, y)
    tile_input = TileInput(
        layer_id="met-stations",
        source_id=basin_version_id,
        source_version=_station_source_version(session, basin_version_id),
        valid_time=None,
        z=z,
        x=x,
        y=y,
    )
    return _cached_or_generated_mvt_response(
        session,
        tile_input,
        lambda: _fetch_station_mvt_tile_bytes(session, basin_version_id=basin_version_id, z=z, x=x, y=y),
    )


def _cached_or_generated_mvt_response(
    session: Session,
    tile_input: TileInput,
    producer: Callable[[], bytes],
) -> Response:
    try:
        cached = read_cached_tile_response(session, tile_input)
    except BaseException:
        _release_session_checkout(session)
        raise
    if cached is not None:
        return _mvt_response(cached)

    _release_session_checkout(session)
    gate, limit = _cold_generation_gate()
    if limit <= 0 or not gate.acquire(blocking=False):
        raise _mvt_cold_generation_busy()
    try:
        with tile_generation_lock(tile_input):
            cached = read_cached_tile_response(session, tile_input)
            if cached is not None:
                return _mvt_response(cached)
            return _mvt_response(build_raw_tile_response(session, tile_input, producer()))
    finally:
        try:
            _release_session_checkout(session)
        finally:
            gate.release()


def _river_network_source_version(session: Session, basin_version_id: str) -> str:
    """Digest every river network of one basin version, per network.

    The basis is `id|segment_count|checksum|geometry_generation` (#2156), the
    same inventory members `national_river_network_source_version` digests.
    `geometry_generation` is what sees an in-place geometry rewrite:
    `_backfill_output_segment_geometry` moves `core.river_segment.geom` and
    `stream_type` under an unchanged network id and bumps that counter in the
    same transaction. `segment_count`/`checksum` move with row-level inventory
    refreshes. The returned string keeps the `river-network-set:` prefix and the
    trailing id list so the catalog stays readable.
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT river_network_version_id, segment_count, checksum, geometry_generation
            FROM core.river_network_version
            WHERE basin_version_id = :basin_version_id
            ORDER BY river_network_version_id
            """
        ),
        {"basin_version_id": basin_version_id},
    ).mappings().all()
    networks = [row for row in rows if row.get("river_network_version_id") is not None]
    versions = [str(row["river_network_version_id"]) for row in networks]
    if not versions:
        raise ApiError(
            status_code=404,
            code="MVT_SOURCE_IDENTITY_NOT_FOUND",
            message="River-network MVT source identity was not found for the requested basin version.",
            details={"layer_id": "river-network", "basin_version_id": basin_version_id},
        )
    joined = ",".join(versions)
    basis = "\n".join(
        f"{row['river_network_version_id']}|{row.get('segment_count')}|{row.get('checksum')}|"
        f"{row.get('geometry_generation')}"
        for row in networks
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"river-network-set:{digest}:{joined}"


def _run_source_version(run: dict[str, Any] | Any) -> str:
    """Revision identity of one run-scoped hydro tile set.

    `geometry_generation` (#2156) is the run's network counter, projected by BOTH
    run readers (`_run_row` and `services.tiles.mvt.display_ready_run`): the run
    row alone cannot see `_backfill_output_segment_geometry` rewriting the
    segment geometry the tile paints. `None` when the run has no network.
    """
    base_version = str(run.get("river_network_version_id") or run.get("basin_version_id") or run.get("run_id"))
    revision_basis = {
        "basin_version_id": run.get("basin_version_id"),
        "cycle_time": _format_time(run.get("cycle_time")) if run.get("cycle_time") is not None else None,
        "geometry_generation": run.get("geometry_generation"),
        "river_network_version_id": run.get("river_network_version_id"),
        "run_id": run.get("run_id"),
        "source_id": run.get("source_id"),
        "status": run.get("status"),
        "updated_at": _format_time(run.get("updated_at")) if run.get("updated_at") is not None else None,
    }
    digest = hashlib.sha256(
        json.dumps(revision_basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{base_version};run-revision:{digest}"


def _require_display_ready(session: Session, run_id: str) -> dict[str, Any]:
    row = _run_row(session, run_id)
    if str(row["status"]) not in DISPLAY_PRODUCT_READY_STATUSES:
        raise ApiError(
            status_code=409,
            code="DISPLAY_PRODUCT_NOT_READY",
            message="Display hydrology products are not yet available for this run",
            details={
                "run_id": run_id,
                "status": row["status"],
                "allowed_statuses": sorted(DISPLAY_PRODUCT_READY_STATUSES),
            },
        )
    return row


def _run_row(session: Session, run_id: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                   h.updated_at, mi.river_network_version_id, rnv.geometry_generation
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            -- #2156: the same projection `display_ready_run` carries, so the catalog
            -- and the tile route feed `_run_source_version` the same revision basis.
            LEFT JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id
            WHERE h.run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if row is None:
        raise ApiError(
            status_code=404,
            code="RUN_NOT_FOUND",
            message=f"Run not found: {run_id}",
            details={"run_id": run_id},
        )
    return dict(row)


def _mvt_response(tile: Any) -> Response:
    return Response(
        content=tile.data,
        media_type=MVT_MEDIA_TYPE,
        headers={
            "Cache-Control": "public, max-age=300",
            "ETag": tile.etag,
            "X-Tile-Layer-ID": tile.layer_id,
            "X-Tile-Checksum": tile.checksum,
            "X-Tile-Cache-Key": tile.cache_key,
            "X-Tile-Cache": tile.cache_status,
            "X-MVT-Schema-Version": MVT_SCHEMA_VERSION,
        },
    )
