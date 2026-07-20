#!/usr/bin/env python3
"""Prewarm the node-27 national overview MVT working set.

The script talks only to the readonly display API. It does not connect to the
database and is safe to run after every idempotent autopipeline tick.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

CHINA_BOUNDS = (73.5, 18.1, 134.8, 53.6)
DEFAULT_BASE_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class WarmResult:
    url: str
    status: int
    bytes: int
    cache: str | None
    error: str | None = None


def xyz_tiles(bounds: tuple[float, float, float, float], zooms: Iterable[int]) -> list[tuple[int, int, int]]:
    west, south, east, north = bounds
    tiles: list[tuple[int, int, int]] = []
    for z in zooms:
        if z < 0 or z > 14:
            raise ValueError(f"zoom must be between 0 and 14: {z}")
        min_x = _lon_to_x(west, z)
        max_x = _lon_to_x(east, z)
        min_y = _lat_to_y(north, z)
        max_y = _lat_to_y(south, z)
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                tiles.append((z, x, y))
    return tiles


def discover_latest_valid_time(base_url: str, timeout: float) -> str | None:
    url = f"{base_url.rstrip('/')}/api/v1/layers/discharge/valid-times"
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-controlled localhost URL
        payload = json.loads(response.read())
    data = payload.get("data") if isinstance(payload, dict) else None
    values = data.get("valid_times") if isinstance(data, dict) else None
    valid_times = [str(value) for value in values or [] if value]
    return max(valid_times) if valid_times else None


def build_warm_urls(base_url: str, tiles: Iterable[tuple[int, int, int]], valid_time: str | None) -> list[str]:
    root = base_url.rstrip("/")
    encoded_time = quote(valid_time, safe="") if valid_time else None
    urls: list[str] = []
    for z, x, y in tiles:
        urls.append(f"{root}/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf")
        if encoded_time:
            urls.append(f"{root}/api/v1/tiles/hydro-national/q_down/{encoded_time}/{z}/{x}/{y}.pbf")
    return urls


def warm_url(url: str, timeout: float) -> WarmResult:
    request = Request(url, headers={"Accept": "application/x-protobuf"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-controlled localhost URL
            body = response.read()
            return WarmResult(
                url=url,
                status=int(response.status),
                bytes=len(body),
                cache=response.headers.get("X-Tile-Cache"),
            )
    except HTTPError as exc:
        return WarmResult(url=url, status=exc.code, bytes=0, cache=None, error=f"HTTP {exc.code}")
    except (TimeoutError, URLError, OSError) as exc:
        return WarmResult(url=url, status=0, bytes=0, cache=None, error=type(exc).__name__)


def prewarm(
    *,
    base_url: str,
    zooms: list[int],
    workers: int,
    timeout: float,
    valid_time: str | None,
) -> tuple[int, dict[str, Any]]:
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    resolved_valid_time = valid_time or discover_latest_valid_time(base_url, timeout)
    tiles = xyz_tiles(CHINA_BOUNDS, zooms)
    urls = build_warm_urls(base_url, tiles, resolved_valid_time)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mvt-prewarm") as executor:
        results = list(executor.map(lambda url: warm_url(url, timeout), urls))
    failed = [result for result in results if result.status < 200 or result.status >= 300]
    summary = {
        "schema": "nhms.node27-mvt-prewarm.v1",
        "base_url": base_url,
        "valid_time": resolved_valid_time,
        "zooms": zooms,
        "tile_count": len(tiles),
        "request_count": len(results),
        "failed_count": len(failed),
        "cache_hits": sum(result.cache == "hit" for result in results),
        "bytes": sum(result.bytes for result in results),
        "failures": [asdict(result) for result in failed[:20]],
    }
    return (1 if failed else 0), summary


def _lon_to_x(lon: float, zoom: int) -> int:
    size = 1 << zoom
    return min(size - 1, max(0, int((lon + 180.0) / 360.0 * size)))


def _lat_to_y(lat: float, zoom: int) -> int:
    size = 1 << zoom
    clipped = min(85.05112878, max(-85.05112878, lat))
    radians = math.radians(clipped)
    value = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * size
    return min(size - 1, max(0, int(value)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--zooms", default="3,4")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--valid-time")
    args = parser.parse_args(argv)
    try:
        zooms = sorted({int(value) for value in args.zooms.split(",") if value.strip()})
        if not zooms:
            raise ValueError("at least one zoom is required")
        rc, summary = prewarm(
            base_url=args.base_url,
            zooms=zooms,
            workers=args.workers,
            timeout=args.timeout,
            valid_time=args.valid_time,
        )
    except (ValueError, HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "nhms.node27-mvt-prewarm.v1", "status": "failed", "error": str(exc)}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
