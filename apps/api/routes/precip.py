"""Public precipitation raster routes (display-v2 I8, #2010).

Both routes are DB-free — no `Depends(get_hydro_display_session)` — and read
exactly two environment variables: `NHMS_PRECIP_MIRROR_ROOT` (the node-27 view of
the NFS tree node-22 mirrors into) and `NHMS_MVT_FILE_CACHE_DIR`. The display
profile forbids `NHMS_OBJECT_STORE_COPYBACK_ROOT` outright
(see runtime_mode's display forbidden-env list), so it is never read here.

The three mutable path segments all come from closed sets: `source` from the
`Literal["gfs","ifs"]` enum (FastAPI answers 422 before this body runs, ahead of
`normalize_source_id` and of any filesystem call), the cycle token from
`%Y%m%d%H` (digits only), and the lead from `f%03d`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from apps.api.display_cache import display_catalog_cached
from apps.api.errors import ApiError
from apps.api.routes.hydro_display import (
    ApiSuccessEnvelope,
    Rfc3339Instant,
    _require_seconds_precision_instant,
)
from apps.api.routes.pipeline import _ok
from packages.common.source_identity import normalize_source_id
from services.precip import (
    PALETTE_VERSION,
    PRECIP_FORECAST_HORIZON_HOURS,
    PRECIP_LEGEND,
    PRECIP_PALETTE,
    PRECIP_STEP_HOURS,
    PRECIP_UNIT,
    PRECIP_WINDOW_HOURS,
    GridDefinition,
    PrecipSliceInvalid,
    PrecipWindowIncomplete,
    accumulate_24h,
    cycle_token,
    discover_mirrored_cycles,
    grid_id_for,
    image_size,
    load_grid,
    precip_directory_key,
    render_png,
    resolve_window,
    slice_digest,
)
from services.precip import cache as precip_cache
from services.precip.constants import MIRROR_ROOT_ENV
from services.tiles.mvt import canonical_mvt_time

router = APIRouter(tags=["precip"])

PRECIP_CACHE_CONTROL = "public, max-age=300"

_NOT_FOUND_RESPONSE = {
    "description": (
        "The requested cycle is not mirrored (or no mirror root is configured), "
        "or the past-24h window cannot be assembled from cycles at or before it."
    ),
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["request_id", "status", "error"],
                "properties": {
                    "request_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["error"]},
                    "error": {
                        "type": "object",
                        "required": ["code", "message"],
                        "properties": {
                            "code": {
                                "type": "string",
                                "enum": ["PRECIP_CYCLE_NOT_MIRRORED", "PRECIP_WINDOW_INCOMPLETE"],
                            },
                            "message": {"type": "string"},
                            "details": {"type": "object", "nullable": True, "additionalProperties": True},
                        },
                    },
                },
            }
        }
    },
}

PRECIP_INDEX_RESPONSES: dict[int | str, dict[str, Any]] = {404: _NOT_FOUND_RESPONSE}
PRECIP_PNG_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Past-24h precipitation raster as an 8-bit palette PNG.",
        "headers": {
            "Cache-Control": {"schema": {"type": "string"}},
            "ETag": {"schema": {"type": "string"}},
            "X-Tile-Cache": {"schema": {"type": "string", "enum": ["hit", "miss"]}},
        },
        "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
    },
    304: {
        "description": "The client's If-None-Match matches the current ETag; the body is not re-read.",
        "headers": {
            "Cache-Control": {"schema": {"type": "string"}},
            "ETag": {"schema": {"type": "string"}},
        },
    },
    404: _NOT_FOUND_RESPONSE,
}


class PrecipLegendEntry(BaseModel):
    min: float
    # Open-ended top class: `null`, never a sentinel number.
    max: float | None = None
    color: str
    label: str


class PrecipIndex(BaseModel):
    source: Literal["gfs", "ifs"]
    cycle: str
    window_hours: int
    unit: str
    bounds: list[float]
    image_size: list[int]
    legend: list[PrecipLegendEntry]
    palette_version: str
    valid_times: list[str]


class PrecipIndexResponse(ApiSuccessEnvelope):
    data: PrecipIndex


@router.get(
    "/api/v1/precip/{source}/{cycle}/index",
    response_model=PrecipIndexResponse,
    responses=PRECIP_INDEX_RESPONSES,
)
def precip_index(
    request: Request,
    # `Literal`, deliberately not a Python `Enum`, for the same reason as the
    # canonical tile route: an Enum makes FastAPI emit a `$ref` the
    # hand-maintained `openapi/nhms.v1.yaml` would have to mirror twice.
    source: Literal["gfs", "ifs"],
    cycle: Rfc3339Instant,
) -> dict[str, Any]:
    """Window-complete valid times of one (source, cycle), plus the render contract; existence checks only."""
    # No NetCDF file is opened here, however many valid times are listed: the
    # index resolves each candidate window by file existence alone.
    cycle_instant = _require_seconds_precision_instant(cycle, "cycle")
    mirror_root = _mirror_root()
    storage_source = normalize_source_id(source)
    token = cycle_token(cycle_instant)
    _require_mirrored_cycle(mirror_root, storage_source, token, source=source, cycle=cycle_instant)

    def _load() -> dict[str, Any]:
        grid = _load_grid(mirror_root, storage_source)
        width, height = image_size(grid)
        mirrored = discover_mirrored_cycles(mirror_root, storage_source)
        valid_times = [
            _instant(candidate)
            for candidate in _horizon_valid_times(cycle_instant)
            if _window_resolves(source, cycle_instant, candidate, mirror_root, mirrored)
        ]
        return {
            "source": source,
            "cycle": _instant(cycle_instant),
            "window_hours": PRECIP_WINDOW_HOURS,
            "unit": PRECIP_UNIT,
            "bounds": list(grid.bounds),
            "image_size": [width, height],
            "legend": [dict(entry) for entry in PRECIP_LEGEND],
            "palette_version": PALETTE_VERSION,
            "valid_times": valid_times,
        }

    return _ok(request, display_catalog_cached(request, f"precip-index:{storage_source}:{token}", _load))


@router.get(
    "/api/v1/precip/{source}/{cycle}/{valid_time}.png",
    responses=PRECIP_PNG_RESPONSES,
    response_class=Response,
)
def precip_png(
    request: Request,
    source: Literal["gfs", "ifs"],
    cycle: Rfc3339Instant,
    valid_time: Rfc3339Instant,
) -> Response:
    """The past-24h precipitation field at valid_time as an 8-bit palette PNG, rendered once and file-cached."""
    cycle_instant = _require_seconds_precision_instant(cycle, "cycle")
    valid_time_instant = _require_seconds_precision_instant(valid_time, "valid_time")
    mirror_root = _mirror_root()
    storage_source = normalize_source_id(source)
    token = cycle_token(cycle_instant)
    _require_mirrored_cycle(mirror_root, storage_source, token, source=source, cycle=cycle_instant)

    try:
        slices = resolve_window(source, cycle_instant, valid_time_instant, mirror_root)
    except PrecipWindowIncomplete as exc:
        raise _window_incomplete_error(exc) from exc

    valid_time_text = _instant(valid_time_instant)
    digest = slice_digest(slices)
    etag = precip_cache.precip_etag(
        source=source,
        cycle=_instant(cycle_instant),
        valid_time=valid_time_text,
        palette_version=PALETTE_VERSION,
        object_keys=[item.object_key for item in slices],
    )
    if request.headers.get("if-none-match") == etag:
        # 304 before any cache read: the identity tuple already decided this.
        return Response(
            status_code=304,
            headers={"Cache-Control": PRECIP_CACHE_CONTROL, "ETag": etag},
        )

    root = precip_cache.cache_root()
    cache_path = (
        None
        if root is None
        else precip_cache.cache_file_path(
            root,
            storage_source=storage_source,
            cycle_token=token,
            valid_time=valid_time_text,
            palette_version=PALETTE_VERSION,
            digest=digest,
        )
    )
    if cache_path is not None:
        cached = precip_cache.read_cached_png(cache_path)
        if cached is not None:
            return _png_response(cached, etag=etag, cache_status="hit")

    try:
        grid = _load_grid(mirror_root, storage_source)
        field = accumulate_24h(slices, grid)
    except PrecipSliceInvalid as exc:
        raise _slice_invalid_error(exc) from exc
    data = render_png(field, grid, PRECIP_PALETTE)
    if cache_path is not None:
        precip_cache.write_cached_png(cache_path, data)
    return _png_response(data, etag=etag, cache_status="miss")


def _mirror_root() -> Path:
    root = os.getenv(MIRROR_ROOT_ENV, "").strip()
    if not root:
        # Fail closed WITHOUT stat-ing anything: an unconfigured deployment is
        # indistinguishable from a pruned cycle to the client, but never a 500.
        raise ApiError(
            status_code=404,
            code="PRECIP_CYCLE_NOT_MIRRORED",
            message="No precipitation mirror root is configured on this deployment.",
            details={"reason": "mirror_root_unconfigured"},
        )
    return Path(root).expanduser()


def _require_mirrored_cycle(
    mirror_root: Path, storage_source: str, token: str, *, source: str, cycle: datetime
) -> None:
    """Route-level gate (pinned decision 9), ahead of any slice lookup."""
    if (mirror_root / precip_directory_key(storage_source, token)).is_dir():
        return
    raise ApiError(
        status_code=404,
        code="PRECIP_CYCLE_NOT_MIRRORED",
        message="The requested cycle has no mirrored precipitation products.",
        details={"reason": "cycle_not_mirrored", "source": source, "cycle": _instant(cycle)},
    )


def _load_grid(mirror_root: Path, storage_source: str) -> GridDefinition:
    try:
        return load_grid(mirror_root, storage_source, grid_id_for(storage_source))
    except PrecipSliceInvalid as exc:
        raise _slice_invalid_error(exc) from exc


def _window_incomplete_error(exc: PrecipWindowIncomplete) -> ApiError:
    details: dict[str, Any] = {"reason": exc.reason}
    if exc.missing is not None:
        details["missing"] = exc.missing
    if exc.window_end is not None:
        details["window_end"] = _instant(exc.window_end)
    return ApiError(
        status_code=404,
        code="PRECIP_WINDOW_INCOMPLETE",
        message="The past-24h precipitation window cannot be assembled from the mirror.",
        details=details,
    )


def _slice_invalid_error(exc: PrecipSliceInvalid) -> ApiError:
    return ApiError(
        status_code=404,
        code="PRECIP_WINDOW_INCOMPLETE",
        message="A mirrored precipitation product does not match the canonical contract.",
        details={"reason": exc.reason, "object_key": exc.object_key},
    )


def _horizon_valid_times(cycle: datetime) -> list[datetime]:
    steps = PRECIP_FORECAST_HORIZON_HOURS // PRECIP_STEP_HOURS
    return [cycle + timedelta(hours=PRECIP_STEP_HOURS * step) for step in range(steps + 1)]


def _window_resolves(
    source: str,
    cycle: datetime,
    valid_time: datetime,
    mirror_root: Path,
    mirrored: tuple[datetime, ...],
) -> bool:
    try:
        resolve_window(source, cycle, valid_time, mirror_root, mirrored_cycles=mirrored)
    except PrecipWindowIncomplete:
        return False
    return True


def _png_response(data: bytes, *, etag: str, cache_status: str) -> Response:
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": PRECIP_CACHE_CONTROL,
            "ETag": etag,
            "X-Tile-Cache": cache_status,
        },
    )


def _instant(value: datetime) -> str:
    """One spelling everywhere: `YYYY-MM-DDTHH:MM:SSZ` (D10)."""
    instant = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return canonical_mvt_time(instant) or instant.isoformat()
