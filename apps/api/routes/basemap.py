"""Same-origin Tianditu basemap tile proxy with a shared file cache.

Why this exists: the browser used to fetch `t0..t7.tianditu.gov.cn` directly
with a key baked into the frontend bundle, so every visitor spent the one key's
quota and a throttled key (`429`, code 302010 "该tk已限流") broke the basemap
for everyone. Worse, Tianditu sends that 429 with `Cache-Control:
max-age=432000`, so browsers kept serving the error for up to five days after
the quota recovered.

This route turns visitor traffic into per-TILE upstream traffic:

- a tile fetched once is written to `<NHMS_MVT_FILE_CACHE_DIR>/basemap/tianditu/
  <layer>/<z>/<x>/<y>` (tmp + `os.replace`, so the two uvicorn workers never
  serve a half-written body) and served from disk afterwards;
- an upstream failure is NEVER cached, and is answered with `no-store`, so
  neither our disk nor the browser holds on to it;
- after an upstream 429 this worker stops asking upstream for
  `THROTTLE_COOLDOWN_SECONDS` and answers misses with 503 at once, instead of
  forwarding every miss into a key that is already throttled.

The `basemap/` subtree is outside the MVT retention runner's reach by
construction (`scripts/node27_mvt_cache_retention.py` only enumerates
two-hex-character directories). It is not pruned: the tile set is bounded by
what people actually browse, and basemap tiles do not change with model runs.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import Response

from apps.api.errors import ApiError
from services.tiles.mvt import MVT_FILE_CACHE_DIR_ENV

router = APIRouter(tags=["basemap"])

TIANDITU_KEY_ENV = "NHMS_TIANDITU_KEY"
# The key the frontend bundle carried before this route existed; kept as the
# fallback so a deployment without the env keeps its basemap.
DEFAULT_TIANDITU_KEY = "25475cca5080dc60cb126b94fd6358d3"
TIANDITU_SUBDOMAINS = ("t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7")
# Tianditu web-mercator layers: base maps and their Chinese annotation overlays.
TiandituLayer = Literal["vec", "cva", "img", "cia", "ter", "cta"]
TIANDITU_MAX_ZOOM = 18

BASEMAP_CACHE_CONTROL = "public, max-age=604800"
UPSTREAM_TIMEOUT_SECONDS = 10.0
THROTTLE_COOLDOWN_SECONDS = 60.0
# The key's permission type is "browser": a non-browser User-Agent gets 403
# `301012 权限类型错误` (or a CloudWAF 418), and a Referer outside the key's
# domain whitelist gets 403 `301007 域名不匹配`. A browser UA with NO Referer is
# accepted (docs/runbooks/receipts/2026-09-20-node27-publish-tick-and-basemap-origin
# §2.1 B1), so the proxy sends exactly that and never forwards the visitor's
# origin -- which also keeps every serving origin (test/nwm.ac.cn, loopback)
# working with one key.
UPSTREAM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"

_throttled_until = 0.0
_client: httpx.AsyncClient | None = None


def _error_response(description: str, codes: list[str]) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {"Cache-Control": {"schema": {"type": "string"}}},
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
                                "code": {"type": "string", "enum": codes},
                                "message": {"type": "string"},
                                "details": {"type": "object", "nullable": True, "additionalProperties": True},
                            },
                        },
                    },
                }
            }
        },
    }


BASEMAP_TILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "One Tianditu web-mercator raster tile (PNG or JPEG, as upstream serves the layer).",
        "headers": {
            "Cache-Control": {"schema": {"type": "string"}},
            "X-Tile-Cache": {"schema": {"type": "string", "enum": ["hit", "miss"]}},
        },
        "content": {
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
        },
    },
    404: _error_response(
        "The tile coordinate is outside the zoom level's tile grid.",
        ["BASEMAP_TILE_OUT_OF_RANGE"],
    ),
    502: _error_response(
        "Tianditu did not return a tile image; nothing was cached.",
        ["BASEMAP_UPSTREAM_UNAVAILABLE"],
    ),
    503: _error_response(
        "Tianditu throttled the configured key; retry after the Retry-After interval. Nothing was cached.",
        ["BASEMAP_UPSTREAM_THROTTLED"],
    ),
}


@router.get(
    "/api/v1/basemap/tianditu/{layer}/{z}/{x}/{y}",
    responses=BASEMAP_TILE_RESPONSES,
    response_class=Response,
)
async def tianditu_tile(
    layer: TiandituLayer,
    z: int = PathParam(ge=0, le=TIANDITU_MAX_ZOOM),
    x: int = PathParam(ge=0),
    y: int = PathParam(ge=0),
) -> Response:
    """One Tianditu basemap tile, fetched once through the server-side key and file-cached."""
    if x >= 1 << z or y >= 1 << z:
        raise ApiError(
            status_code=404,
            code="BASEMAP_TILE_OUT_OF_RANGE",
            message="The tile coordinate is outside the zoom level's tile grid.",
            details={"z": z, "x": x, "y": y},
        )
    cache_path = _cache_path(layer, z, x, y)
    if cache_path is not None:
        cached = _read_cached_tile(cache_path)
        if cached is not None:
            return _tile_response(cached, cache_status="hit")

    data = await _fetch_upstream(layer, z, x, y)
    if cache_path is not None:
        _write_cached_tile(cache_path, data)
    return _tile_response(data, cache_status="miss")


async def _fetch_upstream(layer: str, z: int, x: int, y: int) -> bytes:
    global _throttled_until
    if time.monotonic() < _throttled_until:
        raise _throttled_error()
    subdomain = TIANDITU_SUBDOMAINS[(x + y) % len(TIANDITU_SUBDOMAINS)]
    url = f"https://{subdomain}.tianditu.gov.cn/DataServer"
    params = {"T": f"{layer}_w", "x": str(x), "y": str(y), "l": str(z), "tk": _tianditu_key()}
    try:
        upstream = await _http_client().get(url, params=params)
    except httpx.HTTPError as exc:
        raise _unavailable_error(reason="network", detail=type(exc).__name__) from exc
    if upstream.status_code == 429:
        _throttled_until = time.monotonic() + THROTTLE_COOLDOWN_SECONDS
        raise _throttled_error()
    if upstream.status_code != 200 or _image_media_type(upstream.content) is None:
        # Deliberately not echoing the upstream body: it can carry the key.
        raise _unavailable_error(reason="upstream_status", detail=str(upstream.status_code))
    return upstream.content


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT_SECONDS,
            headers={"User-Agent": UPSTREAM_USER_AGENT},
            follow_redirects=False,
        )
    return _client


def _tianditu_key() -> str:
    return os.getenv(TIANDITU_KEY_ENV, "").strip() or DEFAULT_TIANDITU_KEY


def _throttled_error() -> ApiError:
    return ApiError(
        status_code=503,
        code="BASEMAP_UPSTREAM_THROTTLED",
        message="The basemap provider is throttling this deployment's key; tiles will recover automatically.",
        details={"retry_after_seconds": int(THROTTLE_COOLDOWN_SECONDS)},
        headers={"Cache-Control": "no-store", "Retry-After": str(int(THROTTLE_COOLDOWN_SECONDS))},
    )


def _unavailable_error(*, reason: str, detail: str) -> ApiError:
    return ApiError(
        status_code=502,
        code="BASEMAP_UPSTREAM_UNAVAILABLE",
        message="The basemap provider did not return a tile image.",
        details={"reason": reason, "upstream": detail},
        headers={"Cache-Control": "no-store"},
    )


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    return None


def _cache_path(layer: str, z: int, x: int, y: int) -> Path | None:
    root = os.getenv(MVT_FILE_CACHE_DIR_ENV, "").strip()
    if not root:
        return None
    return Path(root).expanduser() / "basemap" / "tianditu" / layer / str(z) / str(x) / str(y)


def _read_cached_tile(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if _image_media_type(data) is not None else None


def _write_cached_tile(path: Path, data: bytes) -> None:
    """Publish atomically; a cache write failure never fails the response."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except OSError:
        pass
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _tile_response(data: bytes, *, cache_status: str) -> Response:
    return Response(
        content=data,
        media_type=_image_media_type(data),
        headers={"Cache-Control": BASEMAP_CACHE_CONTROL, "X-Tile-Cache": cache_status},
    )
