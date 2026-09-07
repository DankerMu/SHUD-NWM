#!/usr/bin/env python3
"""Prewarm the node-27 national overview MVT + precipitation working set.

The script talks only to the readonly display API. It does not connect to the
database and is safe to run after every idempotent autopipeline tick.

Envelope (per `precipitation-raster-overlay`'s prewarm requirement): the
national river network at `--zooms` (unchanged), plus -- for each of `gfs` and
`ifs`, at that source's OWN newest cycle -- the z3-z4 China discharge tiles for
every valid time of that cycle and one precipitation PNG per valid time. A
source with no cycle contributes zero requests; a source whose discovery FAILS
is a different terminal state and is reported as an error.

The whole run is bounded by `--deadline-seconds` as well as by the per-request
`--timeout`: once the deadline passes the remaining requests are abandoned
rather than issued, counted as `deadline_skipped`, and the exit code is non-zero.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import math
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# Defensive redundancy, not a necessity: cron starts this file as
# `$REPO/.venv/bin/python $REPO/scripts/node27_mvt_prewarm.py`, and that venv's
# editable install already puts `services` on the path. The bootstrap only
# covers non-editable-install invocations. Precedent: node27_autopipeline.py.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.precip.mirror import horizon_valid_times  # noqa: E402 - after the sys.path bootstrap above

CHINA_BOUNDS = (73.5, 18.1, 134.8, 53.6)
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
SUMMARY_SCHEMA = "nhms.node27-mvt-prewarm.v2"
PREWARM_SOURCES = ("gfs", "ifs")
# Fixed by the spec, NOT `zooms & {3, 4}`: an operator setting
# AUTOPIPE_MVT_PREWARM_ZOOMS=5,6,7 must not silently warm zero discharge tiles.
DISCHARGE_ZOOMS = (3, 4)
# `apps/api/display_cache.py`: /cycles and /valid-times are catalog-cached with a
# 60 s TTL and 600 s stale-while-revalidate, and prewarm runs right after publish.
CACHE_WARM_HEADER = "x-nhms-cache-warm"
PRECIP_WINDOW_INCOMPLETE = "PRECIP_WINDOW_INCOMPLETE"
PRECIP_CYCLE_NOT_MIRRORED = "PRECIP_CYCLE_NOT_MIRRORED"
# Whitelist, not `!= "mirror_root_unconfigured"`: a future third reason must not
# be absorbed as "expected". `mirror_root_unconfigured` is a deployment fault
# (`apps/api/routes/precip.py::_mirror_root`) and stays a failure.
EXPECTED_NOT_MIRRORED_REASON = "cycle_not_mirrored"
# Same whitelist treatment, for the same reason: `PRECIP_WINDOW_INCOMPLETE` has
# TWO producers in the route. `_window_incomplete_error`
# (`apps/api/routes/precip.py`) is genuine mirror lag and its reason set is
# exactly these two (`services/precip/errors.py`'s default `missing_slice`, and
# `services/precip/mirror.py`'s `no_mirrored_cycle_before_window_end`).
# `_slice_invalid_error` reuses the SAME code for the 14 `grid_definition_*` /
# `slice_*` reasons of `services/precip/field.py`, every one of which is a data
# or deployment fault -- a missing `grid.json` would otherwise turn all 57 of a
# source's PNGs into a silent `rc=0`.
EXPECTED_WINDOW_INCOMPLETE_REASONS = frozenset({"missing_slice", "no_mirrored_cycle_before_window_end"})
# Half the 600 s ingest timer period this script is invoked from, so one
# degraded run cannot span several ticks. `--timeout` bounds ONE socket
# operation; at 8 workers the 1639-request envelope's per-request worst case is
# ~102 min, which is why the run needs a total wall-clock bound of its own.
DEFAULT_DEADLINE_SECONDS = 300.0


@dataclass(frozen=True)
class WarmResult:
    url: str
    status: int
    bytes: int
    cache: str | None
    error: str | None = None
    error_code: str | None = None
    error_reason: str | None = None


@dataclass(frozen=True)
class SourceDiscovery:
    cycle: str | None
    valid_times: tuple[str, ...]


@dataclass(frozen=True)
class WarmJob:
    url: str
    source: str | None
    kind: str  # "river" | "discharge" | "png"


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


def fetch_json(url: str, timeout: float) -> Any:
    request = Request(  # noqa: S310 - operator-controlled localhost URL
        url,
        headers={"Accept": "application/json", CACHE_WARM_HEADER: "refresh"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-controlled localhost URL
        return json.loads(response.read())


def discover_source(
    base_url: str,
    source: str,
    *,
    timeout: float,
    fetch_json: Callable[[str, float], Any] = fetch_json,
) -> SourceDiscovery:
    """Two hops: the source's newest cycle, then that cycle's valid times.

    Raises on anything that is not a well-formed answer. `default_cycle: null`
    is a legal terminal state (zero requests for this source); a MISSING
    `default_cycle` key is not, which is why this reads `data["default_cycle"]`
    and never `data.get("default_cycle")`.
    """
    root = base_url.rstrip("/")
    cycles_url = f"{root}/api/v1/layers/discharge/cycles?{urlencode({'source': source})}"
    cycle = _envelope_data(fetch_json(cycles_url, timeout))["default_cycle"]
    if cycle is None:
        return SourceDiscovery(cycle=None, valid_times=())
    if not isinstance(cycle, str) or not cycle:
        raise ValueError(f"default_cycle must be a non-empty string or null: {cycle!r}")
    _parse_instant(cycle)
    valid_times_url = f"{root}/api/v1/layers/discharge/valid-times?{urlencode({'source': source, 'cycle': cycle})}"
    values = _envelope_data(fetch_json(valid_times_url, timeout))["valid_times"]
    if not isinstance(values, list):
        raise ValueError(f"valid_times must be a list: {values!r}")
    for value in values:
        _parse_instant(value)
    return SourceDiscovery(cycle=cycle, valid_times=tuple(values))


def partition_png_valid_times(cycle: str, valid_times: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split `valid_times` into the PNG-requestable ones and the rest.

    The PNG route's request shape is a CHECKED condition, not an assumed fact:
    `/valid-times` has no upper clamp of its own, and `/cycles` does not filter
    sub-hour cycle times. `horizon_valid_times` is imported (not re-derived) so
    the route's gate and this one cannot drift.
    """
    cycle_instant = _parse_instant(cycle)
    if cycle_instant.minute or cycle_instant.second:
        return [], list(valid_times)
    horizon = set(horizon_valid_times(cycle_instant))
    requestable: list[str] = []
    out_of_contract: list[str] = []
    for valid_time in valid_times:
        target = requestable if _parse_instant(valid_time) in horizon else out_of_contract
        target.append(valid_time)
    return requestable, out_of_contract


def build_river_urls(base_url: str, tiles: Iterable[tuple[int, int, int]]) -> list[str]:
    """The national river network carries no source/cycle dimension at all.

    Its tile route's `required_placeholders` are `[z, x, y]`
    (`services/tiles/mvt.py::_NATIONAL_RIVER_NETWORK_METADATA`), so this set is
    built once, outside the per-source loop.
    """
    root = base_url.rstrip("/")
    return [f"{root}/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf" for z, x, y in tiles]


def build_warm_urls(
    base_url: str,
    tiles: Iterable[tuple[int, int, int]],
    *,
    source: str,
    cycle: str,
    valid_times: Sequence[str],
) -> list[str]:
    """The per-source envelope. `tiles` is the z3-z4 discharge set only."""
    root = base_url.rstrip("/")
    encoded_source = quote(source, safe="")
    encoded_cycle = quote(cycle, safe="")
    tiles = list(tiles)
    requestable = set(partition_png_valid_times(cycle, valid_times)[0])
    urls: list[str] = []
    for valid_time in valid_times:
        encoded_time = quote(valid_time, safe="")
        for z, x, y in tiles:
            urls.append(
                f"{root}/api/v1/tiles/hydro-national/{encoded_source}/{encoded_cycle}"
                f"/q_down/{encoded_time}/{z}/{x}/{y}.pbf"
            )
        if valid_time in requestable:
            urls.append(f"{root}/api/v1/precip/{encoded_source}/{encoded_cycle}/{encoded_time}.png")
    return urls


def warm_url(url: str, timeout: float) -> WarmResult:
    """Issue one warm request. Deliberately dumb: it does not know the URL kind.

    Classifying which 404s are expected is the aggregator's job, because only
    the aggregator knows whether a URL was a tile or a precipitation PNG.
    """
    request = Request(url, headers={"Accept": "*/*"})  # noqa: S310 - operator-controlled localhost URL
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
        error_code, error_reason = _parse_error_envelope(exc)
        return WarmResult(
            url=url,
            status=exc.code,
            bytes=0,
            cache=None,
            error=f"HTTP {exc.code}",
            error_code=error_code,
            error_reason=error_reason,
        )
    except (TimeoutError, URLError, OSError, http.client.HTTPException) as exc:
        # `http.client.HTTPException` is listed for `IncompleteRead`, whose MRO
        # is `(IncompleteRead, HTTPException, Exception, ...)` -- it is neither
        # an `OSError` nor a `ValueError`, so the other three do not cover it,
        # and a truncated response body on the graceful-uvicorn-restart path
        # would otherwise escape this function. `prewarm` has a backstop for
        # direct escapees, but only here is the URL of the failed request known
        # to be reportable as a network fault rather than an unknown one.
        return WarmResult(url=url, status=0, bytes=0, cache=None, error=type(exc).__name__)


def classify_png_failure(result: WarmResult) -> str | None:
    """The expected-precipitation-404 bucket for a non-2xx PNG result, or None.

    Classified by `error.code` (and `error.details.reason`), never by the status
    code alone: an unconfigured `NHMS_PRECIP_MIRROR_ROOT` answers 404
    `PRECIP_CYCLE_NOT_MIRRORED` too, and that is a deployment fault.
    """
    if result.status != 404:
        return None
    if result.error_code == PRECIP_WINDOW_INCOMPLETE and result.error_reason in EXPECTED_WINDOW_INCOMPLETE_REASONS:
        return "png_window_incomplete"
    if result.error_code == PRECIP_CYCLE_NOT_MIRRORED and result.error_reason == EXPECTED_NOT_MIRRORED_REASON:
        return "png_not_mirrored"
    return None


def prewarm(
    *,
    base_url: str,
    zooms: list[int],
    workers: int,
    timeout: float,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    fetch_json: Callable[[str, float], Any] = fetch_json,
    warm: Callable[[str, float], WarmResult] = warm_url,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    started = clock()
    deadline_at = started + deadline_seconds
    river_tiles = xyz_tiles(CHINA_BOUNDS, zooms)
    discharge_tiles = xyz_tiles(CHINA_BOUNDS, DISCHARGE_ZOOMS)

    jobs = [WarmJob(url=url, source=None, kind="river") for url in build_river_urls(base_url, river_tiles)]
    per_source: dict[str, dict[str, Any]] = {}
    for source in PREWARM_SOURCES:
        entry = _new_source_entry()
        per_source[source] = entry
        source_urls: list[str] = []
        try:
            discovery = discover_source(base_url, source, timeout=timeout, fetch_json=fetch_json)
            entry["cycle"] = discovery.cycle
            entry["valid_times"] = len(discovery.valid_times)
            if discovery.cycle is not None:
                out_of_contract = partition_png_valid_times(discovery.cycle, discovery.valid_times)[1]
                entry["png_out_of_contract"] = len(out_of_contract)
                source_urls = build_warm_urls(
                    base_url,
                    discharge_tiles,
                    source=source,
                    cycle=discovery.cycle,
                    valid_times=discovery.valid_times,
                )
        except Exception as exc:
            # Per-source isolation by exception CLASS, not by an allow-list of
            # types: one source's discovery hiccup must not stop the other from
            # being warmed, nor suppress the summary. The previous allow-list
            # deliberately excluded `TypeError` so a programming bug could not
            # be absorbed -- but `http.client.IncompleteRead` is neither an
            # `OSError` nor a `ValueError` either, so the list silently let a
            # REACHABLE transport fault through as well. A bug is still not
            # absorbed silently: it is named in `per_source[<s>].error`, forces
            # a non-zero exit code, and merely stops destroying the other
            # source's work and the whole summary on its way out. Everything
            # that can raise for one source is inside this block, including the
            # horizon partition and URL construction.
            entry["error"] = f"{type(exc).__name__}: {exc}"
            continue
        for url in source_urls:
            jobs.append(WarmJob(url=url, source=source, kind="png" if url.endswith(".png") else "discharge"))

    def run_job(job: WarmJob) -> WarmResult | None:
        """One pool task. `None` means "not issued": the deadline had passed.

        The deadline is checked here rather than by cancelling in-flight work,
        so the pool drains the remainder at once. Any exception out of `warm`
        becomes a failed result instead of escaping `executor.map`, which
        re-raises in the caller and would take the entire summary with it.
        """
        if clock() >= deadline_at:
            return None
        try:
            return warm(job.url, timeout)
        except Exception as exc:
            return WarmResult(url=job.url, status=0, bytes=0, cache=None, error=type(exc).__name__)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mvt-prewarm") as executor:
        outcomes = list(executor.map(run_job, jobs))

    results: list[WarmResult] = []
    failed: list[WarmResult] = []
    deadline_skipped = 0
    for job, outcome in zip(jobs, outcomes, strict=True):
        if outcome is None:
            # Not issued, so not counted as a request and not counted per
            # source either; `deadline_skipped` is its only accounting home.
            deadline_skipped += 1
            continue
        result = outcome
        results.append(result)
        ok = 200 <= result.status < 300
        if job.source is not None:
            entry = per_source[job.source]
            if job.kind == "discharge":
                entry["discharge_requests"] += 1
            elif job.kind == "png":
                if ok:
                    entry["png_ok"] += 1
                else:
                    # Only here, where the URL kind is known, does the expected
                    # precipitation 404 whitelist apply.
                    bucket = classify_png_failure(result)
                    if bucket is not None:
                        entry[bucket] += 1
                        continue
                    entry["png_failed"] += 1
        if not ok:
            failed.append(result)

    out_of_contract = sum(entry["png_out_of_contract"] for entry in per_source.values())
    discovery_failed = any(entry["error"] for entry in per_source.values())
    summary = {
        "schema": SUMMARY_SCHEMA,
        "base_url": base_url,
        "zooms": zooms,
        "discharge_zooms": list(DISCHARGE_ZOOMS),
        "river_tile_count": len(river_tiles),
        "requests_total": len(results),
        "failed_count": len(failed),
        "cache_hits": sum(result.cache == "hit" for result in results),
        "bytes": sum(result.bytes for result in results),
        "failures": [asdict(result) for result in failed[:20]],
        "elapsed_seconds": round(clock() - started, 3),
        "deadline_seconds": deadline_seconds,
        "deadline_skipped": deadline_skipped,
        "per_source": per_source,
    }
    rc = 1 if (failed or discovery_failed or out_of_contract or deadline_skipped) else 0
    return rc, summary


def _new_source_entry() -> dict[str, Any]:
    return {
        "cycle": None,
        "valid_times": 0,
        "discharge_requests": 0,
        "png_ok": 0,
        "png_not_mirrored": 0,
        "png_window_incomplete": 0,
        "png_out_of_contract": 0,
        "png_failed": 0,
        "error": None,
    }


def _envelope_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"response envelope must be an object: {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"response envelope has no data object: {type(data).__name__}")
    return data


def _parse_instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"instant must be a string: {value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"instant must carry a UTC offset: {value!r}")
    return parsed


def _parse_error_envelope(exc: HTTPError) -> tuple[str | None, str | None]:
    """`{"error": {"code": ..., "details": {"reason": ...}}}` or `(None, None)`.

    A non-2xx whose body is not that envelope stays unclassified, and the
    aggregator therefore counts it as a failure.
    """
    try:
        payload = json.loads(exc.read())
        error = payload["error"]
        code = error.get("code")
        details = error.get("details")
        reason = details.get("reason") if isinstance(details, dict) else None
    except (AttributeError, LookupError, OSError, TypeError, ValueError, http.client.HTTPException):
        # `http.client.HTTPException` covers a truncated body: `exc.read()` is a
        # real socket read here, and losing the already-known status code to it
        # would relabel a classified 404 as a bare transport error.
        return None, None
    code = code if isinstance(code, str) else None
    reason = reason if isinstance(reason, str) else None
    return code, reason


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
    parser.add_argument("--zooms", default="3,4,5", help="river-network zoom set only; discharge is pinned to z3-z4")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request socket timeout")
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help="total wall-clock budget; once passed, remaining requests are abandoned, not issued",
    )
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
            deadline_seconds=args.deadline_seconds,
        )
    except Exception as exc:
        # Process-level failures only (bad arguments and the like), and this is
        # the ONLY path that returns 2. `prewarm` already isolates every
        # per-source and per-request failure into the full summary at rc=1, so
        # catching broadly here does not hide them -- it stops an unforeseen
        # escapee from exiting 1 through Python's default handler, which would
        # be indistinguishable from "some requests failed" while printing no
        # summary at all.
        print(json.dumps({"schema": SUMMARY_SCHEMA, "status": "failed", "error": str(exc)}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
