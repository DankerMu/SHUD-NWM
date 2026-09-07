from __future__ import annotations

import email.message
import http.client
import io
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

from scripts import node27_mvt_prewarm as prewarm
from services.precip.mirror import horizon_valid_times

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_AUTOPIPE_TIMER = REPO_ROOT / "infra" / "systemd" / "nhms-node27-autopipe.timer"

_BASE_URL = "http://127.0.0.1:8080"
_GFS_CYCLE = "2026-09-02T12:00:00Z"
_IFS_CYCLE = "2026-09-02T00:00:00Z"
# The counts the envelope arithmetic is pinned to; `xyz_tiles` is the oracle for
# the coordinates, `test_china_default_working_set_is_small_and_unique` for 43.
_RIVER_TILE_COUNT = 43
_DISCHARGE_TILE_COUNT = 13
# What `/valid-times` publishes, as measured on node-27:
# `docs/runbooks/receipts/2026-09-05-issue-2009-discharge-cycles-node27.md`
# records 56 entries (`[C, min(river_valid_time_end)]` = 165 h / 3 + 1).
_VALID_TIME_COUNT = 56
# What survives the `PREWARM_LEAD_HOURS = 12` cut on that 3 h grid: k = 0...4.
_WARMED_VALID_TIME_COUNT = 5
# 13 * 5 discharge tiles + 5 PNGs.
_PER_SOURCE_REQUESTS = _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT + _WARMED_VALID_TIME_COUNT
# 43 + 2 * 70 -- discovery calls are NOT counted.
_GOLDEN_REQUESTS_TOTAL = _RIVER_TILE_COUNT + 2 * _PER_SOURCE_REQUESTS

_ENCODED_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A\d{2}Z")


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _steps(cycle: str, count: int, *, first_step: int = 0) -> list[str]:
    start = datetime.fromisoformat(cycle)
    return [_instant(start + timedelta(hours=3 * step)) for step in range(first_step, first_step + count)]


def _warmed(cycle: str) -> list[str]:
    """The valid times of `cycle` that survive the `PREWARM_LEAD_HOURS` cut."""
    return _steps(cycle, _WARMED_VALID_TIME_COUNT)


def _cycles_payload(cycle: str | None, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "cycles": [] if cycle is None else [{"cycle": cycle}],
        "default_cycle": cycle,
    }
    if metadata is not None:
        data["metadata"] = metadata
    return {"request_id": "req-1", "status": "ok", "data": data}


def _valid_times_payload(values: Any) -> dict[str, Any]:
    return {"request_id": "req-1", "status": "ok", "data": {"valid_times": values}}


def _plan(
    *,
    gfs: str | None = _GFS_CYCLE,
    ifs: str | None = _IFS_CYCLE,
    gfs_valid_times: Any = None,
    ifs_valid_times: Any = None,
    gfs_metadata: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "gfs": {
            "cycles": _cycles_payload(gfs, metadata=gfs_metadata),
            "valid_times": _valid_times_payload(
                gfs_valid_times if gfs_valid_times is not None else _steps(gfs or _GFS_CYCLE, _VALID_TIME_COUNT)
            ),
        },
        "ifs": {
            "cycles": _cycles_payload(ifs),
            "valid_times": _valid_times_payload(
                ifs_valid_times if ifs_valid_times is not None else _steps(ifs or _IFS_CYCLE, _VALID_TIME_COUNT)
            ),
        },
    }


class _FakeClock:
    """A monotonically increasing counter driven by the fakes, not by call count.

    READING it never advances it. `prewarm` reads the clock once per job for the
    deadline check, so a self-advancing fake would pin `elapsed_seconds` to an
    implementation detail; only `_FakeDiscovery` and `_FakeWarmer` advance it, so
    the pinned span is exactly "discovery work + warm work". For the same reason
    it must not be a two-value iterator: `ThreadPoolExecutor` reads the real
    `time.monotonic` internally, and this clock is injected, never patched in.
    """

    def __init__(self, *, discovery_step: float = 0.0, warm_step: float = 0.0) -> None:
        self.discovery_step = discovery_step
        self.warm_step = warm_step
        self._now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _FakeDiscovery:
    """Serves the two discovery hops and refuses anything else."""

    def __init__(self, plan: dict[str, dict[str, Any]], *, clock: _FakeClock | None = None) -> None:
        self._plan = plan
        self._clock = clock
        self.urls: list[str] = []
        self.requested_cycles: dict[str, str | None] = {}

    def __call__(self, url: str, timeout: float) -> Any:
        if self._clock is not None:
            self._clock.advance(self._clock.discovery_step)
        self.urls.append(url)
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        source = query.get("source")
        if source not in self._plan:
            raise AssertionError(f"prewarm asked for an unknown source: {url}")
        if parts.path == "/api/v1/layers/discharge/cycles":
            entry = self._plan[source]["cycles"]
        elif parts.path == "/api/v1/layers/discharge/valid-times":
            self.requested_cycles[source] = query.get("cycle")
            entry = self._plan[source]["valid_times"]
        else:
            raise AssertionError(f"prewarm consulted an unexpected discovery URL: {url}")
        if isinstance(entry, Exception):
            raise entry
        return entry

    def urls_for(self, source: str) -> list[str]:
        return [url for url in self.urls if f"source={source}" in url]


class _FakeWarmer:
    def __init__(
        self,
        overrides: dict[str, tuple[int, str | None, str | None]] | None = None,
        *,
        raises: dict[str, Exception] | None = None,
        clock: _FakeClock | None = None,
    ) -> None:
        self.urls: list[str] = []
        self._overrides = overrides or {}
        # The double must be able to express "this request RAISED": a double
        # that can only return a `WarmResult` structurally exempts the whole
        # exception-classification failure class from the suite.
        self._raises = raises or {}
        self._clock = clock
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: float) -> prewarm.WarmResult:
        with self._lock:
            self.urls.append(url)
        if self._clock is not None:
            self._clock.advance(self._clock.warm_step)
        raising = self._raises.get(url)
        if raising is not None:
            raise raising
        status, code, reason = self._overrides.get(url, (200, None, None))
        ok = 200 <= status < 300
        return prewarm.WarmResult(
            url=url,
            status=status,
            bytes=128 if ok else 0,
            cache="hit" if ok else None,
            error=None if ok else f"HTTP {status}",
            error_code=code,
            error_reason=reason,
        )


def _run(
    plan: dict[str, dict[str, Any]],
    *,
    overrides: dict[str, tuple[int, str | None, str | None]] | None = None,
    zooms: list[int] | None = None,
    base_url: str = _BASE_URL,
    workers: int = 4,
    raises: dict[str, Exception] | None = None,
    clock: _FakeClock | None = None,
    deadline_seconds: float | None = None,
) -> tuple[int, dict[str, Any], _FakeWarmer, _FakeDiscovery]:
    discovery = _FakeDiscovery(plan, clock=clock)
    warmer = _FakeWarmer(overrides, raises=raises, clock=clock)
    optional: dict[str, Any] = {}
    if clock is not None:
        optional["clock"] = clock
    if deadline_seconds is not None:
        optional["deadline_seconds"] = deadline_seconds
    rc, summary = prewarm.prewarm(
        base_url=base_url,
        zooms=zooms if zooms is not None else [3, 4, 5],
        workers=workers,
        timeout=1.0,
        fetch_json=discovery,
        warm=warmer,
        **optional,
    )
    return rc, summary, warmer, discovery


def _raising_urlopen(exc: Exception) -> Any:
    def _fake_urlopen(request: Any, timeout: float) -> Any:
        raise exc

    return _fake_urlopen


_SUMMARY_V2_KEYS = {
    "schema",
    "base_url",
    "zooms",
    "discharge_zooms",
    "river_tile_count",
    "requests_total",
    "failed_count",
    "cache_hits",
    "bytes",
    "failures",
    "elapsed_seconds",
    "deadline_seconds",
    "deadline_skipped",
    "lead_hours",
    "per_source",
}


def _river_urls(zooms: list[int] | None = None) -> set[str]:
    tiles = prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, zooms if zooms is not None else [3, 4, 5])
    return {f"{_BASE_URL}/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf" for z, x, y in tiles}


def _discharge_urls(source: str, cycle: str, valid_times: list[str]) -> set[str]:
    tiles = prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [3, 4])
    encoded_cycle = quote(cycle, safe="")
    return {
        f"{_BASE_URL}/api/v1/tiles/hydro-national/{source}/{encoded_cycle}"
        f"/q_down/{quote(valid_time, safe='')}/{z}/{x}/{y}.pbf"
        for valid_time in valid_times
        for z, x, y in tiles
    }


def _png_urls(source: str, cycle: str, valid_times: list[str]) -> set[str]:
    encoded_cycle = quote(cycle, safe="")
    return {
        f"{_BASE_URL}/api/v1/precip/{source}/{encoded_cycle}/{quote(valid_time, safe='')}.png"
        for valid_time in valid_times
    }


def _cycle_segment(url: str) -> str:
    """The `{cycle}` path segment of a discharge-tile or precipitation-PNG URL."""
    parts = urlsplit(url).path.split("/")
    if parts[4] == "hydro-national":
        return parts[6]
    assert parts[3] == "precip", url
    return parts[5]


def _group_key(url: str) -> tuple[str, str] | None:
    """`(source, valid_time)` path segments, or `None` for a river-network URL."""
    parts = urlsplit(url).path.split("/")
    if parts[3] == "tiles" and parts[4] == "hydro-national":
        return parts[5], parts[8]
    if parts[3] == "precip":
        return parts[4], parts[6].removesuffix(".png")
    return None


def _empty_source_entry(cycle: str | None = None) -> dict[str, Any]:
    return {
        "cycle": cycle,
        "valid_times_available": 0,
        "valid_times_warmed": 0,
        "discharge_requests": 0,
        "png_ok": 0,
        "png_not_mirrored": 0,
        "png_window_incomplete": 0,
        "png_out_of_contract": 0,
        "png_failed": 0,
        "error": None,
    }


def test_china_default_working_set_is_small_and_unique() -> None:
    tiles = prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [3, 4, 5])

    assert tiles
    assert len(tiles) == len(set(tiles))
    assert len(tiles) == _RIVER_TILE_COUNT
    assert all(z in {3, 4, 5} and 0 <= x < 2**z and 0 <= y < 2**z for z, x, y in tiles)
    assert len(prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, prewarm.DISCHARGE_ZOOMS)) == _DISCHARGE_TILE_COUNT


def test_build_warm_urls_encodes_cycle_and_valid_time_in_the_path() -> None:
    urls = prewarm.build_warm_urls(
        _BASE_URL,
        [(4, 12, 6)],
        source="gfs",
        cycle="2026-07-20T00:00:00Z",
        valid_times=["2026-07-20T03:00:00Z"],
    )

    assert urls == [
        f"{_BASE_URL}/api/v1/tiles/hydro-national/gfs/2026-07-20T00%3A00%3A00Z"
        "/q_down/2026-07-20T03%3A00%3A00Z/4/12/6.pbf",
        f"{_BASE_URL}/api/v1/precip/gfs/2026-07-20T00%3A00%3A00Z/2026-07-20T03%3A00%3A00Z.png",
    ]


def test_envelope_is_river_43_plus_70_per_source_and_counts_183_warm_requests() -> None:
    gfs_times = _warmed(_GFS_CYCLE)
    ifs_times = _warmed(_IFS_CYCLE)

    rc, summary, warmer, _ = _run(_plan())

    expected = (
        _river_urls()
        | _discharge_urls("gfs", _GFS_CYCLE, gfs_times)
        | _png_urls("gfs", _GFS_CYCLE, gfs_times)
        | _discharge_urls("ifs", _IFS_CYCLE, ifs_times)
        | _png_urls("ifs", _IFS_CYCLE, ifs_times)
    )
    assert set(warmer.urls) == expected
    assert len(warmer.urls) == _GOLDEN_REQUESTS_TOTAL
    assert len(expected) == _RIVER_TILE_COUNT + 2 * _PER_SOURCE_REQUESTS
    assert _GOLDEN_REQUESTS_TOTAL == 183
    assert rc == 0
    assert summary["schema"] == "nhms.node27-mvt-prewarm.v2"
    assert summary["requests_total"] == _GOLDEN_REQUESTS_TOTAL
    assert summary["river_tile_count"] == _RIVER_TILE_COUNT
    assert summary["lead_hours"] == prewarm.PREWARM_LEAD_HOURS == 12
    assert "valid_time" not in summary
    assert isinstance(summary["elapsed_seconds"], float)
    assert summary["elapsed_seconds"] >= 0.0
    # `_FakeWarmer` answers every OK request with `cache="hit"` and 128 bytes,
    # so both counters have a value oracle, not just a key.
    assert summary["cache_hits"] == _GOLDEN_REQUESTS_TOTAL
    assert summary["bytes"] == 128 * _GOLDEN_REQUESTS_TOTAL
    assert summary["deadline_skipped"] == 0
    assert summary["deadline_seconds"] == prewarm.DEFAULT_DEADLINE_SECONDS
    for source in ("gfs", "ifs"):
        entry = summary["per_source"][source]
        # Both counts are in the summary so the receipt SEES the descope instead
        # of having to infer it from the request total.
        assert entry["valid_times_available"] == _VALID_TIME_COUNT == 56
        assert entry["valid_times_warmed"] == _WARMED_VALID_TIME_COUNT == 5
        assert entry["discharge_requests"] == _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT
        assert entry["png_ok"] == _WARMED_VALID_TIME_COUNT


def test_discharge_zooms_are_fixed_and_do_not_follow_the_river_zoom_flag() -> None:
    """`--zooms` names the river-network set only; discharge is pinned to z3-z4."""
    rc, summary, warmer, _ = _run(_plan(), zooms=[5])

    assert prewarm.DISCHARGE_ZOOMS == (3, 4)
    assert summary["zooms"] == [5]
    assert summary["discharge_zooms"] == [3, 4]
    assert summary["river_tile_count"] == 30
    assert set(warmer.urls) & _river_urls([5]) == _river_urls([5])
    for source, cycle in (("gfs", _GFS_CYCLE), ("ifs", _IFS_CYCLE)):
        assert (
            summary["per_source"][source]["discharge_requests"] == _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT
        )
        assert _discharge_urls(source, cycle, _warmed(cycle)) <= set(warmer.urls)
    assert rc == 0


def test_each_source_is_warmed_at_its_own_cycle_with_no_cross_contamination() -> None:
    _, summary, warmer, discovery = _run(_plan())

    gfs_encoded = quote(_GFS_CYCLE, safe="")
    ifs_encoded = quote(_IFS_CYCLE, safe="")
    gfs_urls = [url for url in warmer.urls if "/gfs/" in url]
    ifs_urls = [url for url in warmer.urls if "/ifs/" in url]

    assert gfs_urls and ifs_urls
    # The cycle SEGMENT, not any substring: `2026-09-02T12:00:00Z` is also a
    # legitimate valid time of the 00Z ifs cycle.
    assert {_cycle_segment(url) for url in gfs_urls} == {gfs_encoded}
    assert {_cycle_segment(url) for url in ifs_urls} == {ifs_encoded}
    assert ifs_encoded not in {_cycle_segment(url) for url in gfs_urls}
    assert gfs_encoded not in {_cycle_segment(url) for url in ifs_urls}
    assert summary["per_source"]["gfs"]["cycle"] == _GFS_CYCLE
    assert summary["per_source"]["ifs"]["cycle"] == _IFS_CYCLE
    assert discovery.requested_cycles == {"gfs": _GFS_CYCLE, "ifs": _IFS_CYCLE}


def test_null_default_cycle_warms_nothing_for_that_source_and_fabricates_no_cycle() -> None:
    plan = _plan(gfs=None, gfs_metadata={"valid_times": ["2099-01-01T00:00:00Z"]})

    rc, summary, warmer, discovery = _run(plan)

    assert rc == 0
    assert summary["per_source"]["gfs"] == _empty_source_entry()
    assert not [url for url in warmer.urls if "/gfs/" in url]
    # Only the first hop happened: the second hop, and `metadata`, were never consulted.
    assert discovery.urls_for("gfs") == [f"{_BASE_URL}/api/v1/layers/discharge/cycles?source=gfs"]
    # (a) not borrowed from the other source
    assert not [url for url in warmer.urls if quote(_IFS_CYCLE, safe="") in url and "/ifs/" not in url]
    # (b) not taken from `metadata.valid_times`
    assert not [url for url in warmer.urls if "2099" in url]
    # (c) not derived from the wall clock: every instant in the URL set was supplied by the fixture
    supplied = {quote(value, safe="") for value in [_IFS_CYCLE, *_steps(_IFS_CYCLE, _VALID_TIME_COUNT)]}
    seen = {match for url in warmer.urls for match in _ENCODED_INSTANT.findall(url)}
    assert seen <= supplied
    assert summary["per_source"]["ifs"]["valid_times_available"] == _VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["valid_times_warmed"] == _WARMED_VALID_TIME_COUNT


def test_both_sources_empty_still_warm_the_river_network_and_succeed() -> None:
    """Replaces the v1 'river is warmed even when no valid time exists' case."""
    rc, summary, warmer, _ = _run(_plan(gfs=None, ifs=None))

    assert set(warmer.urls) == _river_urls()
    assert len(warmer.urls) == _RIVER_TILE_COUNT
    assert summary["requests_total"] == _RIVER_TILE_COUNT
    assert summary["per_source"]["gfs"] == _empty_source_entry()
    assert summary["per_source"]["ifs"] == _empty_source_entry()
    assert rc == 0


def test_both_sources_failing_discovery_warm_the_river_network_but_fail_the_run() -> None:
    plan = _plan()
    plan["gfs"]["cycles"] = TimeoutError("cycles timed out")
    plan["ifs"]["cycles"] = OSError("connection reset")

    rc, summary, warmer, _ = _run(plan)

    assert set(warmer.urls) == _river_urls()
    assert summary["requests_total"] == _RIVER_TILE_COUNT
    assert summary["per_source"]["gfs"]["error"]
    assert summary["per_source"]["ifs"]["error"]
    assert summary["per_source"]["gfs"]["cycle"] is None
    assert rc != 0
    # The full summary survives a total discovery failure.
    assert summary["elapsed_seconds"] >= 0.0
    assert summary["river_tile_count"] == _RIVER_TILE_COUNT


@pytest.mark.parametrize(
    ("hop", "payload"),
    [
        ("cycles", {"request_id": "req-1", "status": "ok", "data": {}}),
        ("valid_times", {"request_id": "req-1", "status": "ok", "data": {"valid_times": "2026-09-02T12:00:00Z"}}),
        ("cycles", {"request_id": "req-1", "status": "ok", "data": None}),
    ],
)
def test_unexpected_200_envelope_is_a_source_error_not_a_zero_request_success(hop: str, payload: Any) -> None:
    plan = _plan()
    plan["gfs"][hop] = payload

    rc, summary, warmer, _ = _run(plan)

    assert summary["per_source"]["gfs"]["error"]
    assert rc != 0
    assert not [url for url in warmer.urls if "/gfs/" in url]
    # NOT the legitimate "this source contributes zero requests" terminal state.
    assert summary["per_source"]["gfs"] != _empty_source_entry()


@pytest.mark.parametrize("hop", ["cycles", "valid_times"])
def test_one_source_discovery_failure_does_not_swallow_the_other(hop: str) -> None:
    plan = _plan()
    plan["gfs"][hop] = HTTPError(
        f"{_BASE_URL}/api/v1/layers/discharge/{hop}", 503, "Service Unavailable", email.message.Message(), None
    )

    rc, summary, warmer, _ = _run(plan)

    ifs_times = _warmed(_IFS_CYCLE)
    assert set(warmer.urls) == (
        _river_urls() | _discharge_urls("ifs", _IFS_CYCLE, ifs_times) | _png_urls("ifs", _IFS_CYCLE, ifs_times)
    )
    assert summary["per_source"]["ifs"]["discharge_requests"] == _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["png_ok"] == _WARMED_VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["error"] is None
    assert summary["per_source"]["gfs"]["error"]
    assert summary["requests_total"] == _RIVER_TILE_COUNT + _PER_SOURCE_REQUESTS
    assert rc != 0


def test_unparseable_valid_time_element_is_a_source_error_not_a_process_failure() -> None:
    plan = _plan(gfs_valid_times=[*_steps(_GFS_CYCLE, 3), "not-a-time"])

    rc, summary, warmer, _ = _run(plan)

    assert summary["per_source"]["gfs"]["error"]
    assert rc != 0
    assert not [url for url in warmer.urls if "/gfs/" in url]
    # The full summary, not `main()`'s one-line failure envelope.
    assert summary["schema"] == "nhms.node27-mvt-prewarm.v2"
    assert "status" not in summary
    assert summary["per_source"]["ifs"]["png_ok"] == _WARMED_VALID_TIME_COUNT


_Overrides = dict[str, tuple[int, str | None, str | None]]


def _precip_override_plan() -> tuple[dict[str, dict[str, Any]], _Overrides, list[str]]:
    png = sorted(_png_urls("gfs", _GFS_CYCLE, _warmed(_GFS_CYCLE)))
    # ASYMMETRIC (2 window-incomplete vs 1 not-mirrored) and using the reason
    # strings the backend actually produces: with a symmetric 1/1 injection both
    # counters read 1, so swapping the two bucket names in `classify_png_failure`
    # leaves every assertion green.
    overrides = {
        png[0]: (404, "PRECIP_WINDOW_INCOMPLETE", "missing_slice"),
        png[1]: (404, "PRECIP_WINDOW_INCOMPLETE", "no_mirrored_cycle_before_window_end"),
        png[2]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "cycle_not_mirrored"),
        png[3]: (500, "INTERNAL_ERROR", None),
    }
    return _plan(), overrides, png


def test_expected_precip_404s_are_counted_and_a_500_is_still_a_failure() -> None:
    plan, overrides, _ = _precip_override_plan()

    rc, summary, _, _ = _run(plan, overrides=overrides)

    gfs = summary["per_source"]["gfs"]
    assert (gfs["png_window_incomplete"], gfs["png_not_mirrored"]) == (2, 1)
    assert gfs["png_failed"] == 1
    assert gfs["png_ok"] == _WARMED_VALID_TIME_COUNT - 4
    assert summary["failed_count"] == 1
    assert [entry["url"] for entry in summary["failures"]] == [sorted(overrides)[3]]
    assert rc != 0


def test_expected_precip_404s_alone_do_not_fail_the_run() -> None:
    png = sorted(_png_urls("gfs", _GFS_CYCLE, _warmed(_GFS_CYCLE)))
    overrides = {
        png[0]: (404, "PRECIP_WINDOW_INCOMPLETE", "missing_slice"),
        png[1]: (404, "PRECIP_WINDOW_INCOMPLETE", "no_mirrored_cycle_before_window_end"),
        png[2]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "cycle_not_mirrored"),
    }

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    gfs = summary["per_source"]["gfs"]
    assert rc == 0
    assert summary["failed_count"] == 0
    assert summary["failures"] == []
    assert (gfs["png_window_incomplete"], gfs["png_not_mirrored"]) == (2, 1)
    assert gfs["png_failed"] == 0


def test_corrupt_product_reasons_under_the_window_incomplete_code_are_failures() -> None:
    """`PRECIP_WINDOW_INCOMPLETE` has two producers; only mirror lag is expected.

    `apps/api/routes/precip.py::_slice_invalid_error` answers with the SAME code
    for `services/precip/field.py`'s `grid_definition_*` / `slice_*` reasons,
    which are data or deployment faults. Without the reason whitelist a missing
    `grid.json` turns every one of a source's PNGs into `rc=0`.
    """
    png = sorted(_png_urls("gfs", _GFS_CYCLE, _warmed(_GFS_CYCLE)))
    overrides = {
        png[0]: (404, "PRECIP_WINDOW_INCOMPLETE", "slice_unreadable"),
        png[1]: (404, "PRECIP_WINDOW_INCOMPLETE", "grid_definition_missing"),
        # Whitelist, not exclusion: no reason at all is not "expected" either.
        png[2]: (404, "PRECIP_WINDOW_INCOMPLETE", None),
    }

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    gfs = summary["per_source"]["gfs"]
    assert gfs["png_window_incomplete"] == 0
    assert gfs["png_failed"] == 3
    assert gfs["png_ok"] == _WARMED_VALID_TIME_COUNT - 3
    assert summary["failed_count"] == 3
    assert {entry["error_reason"] for entry in summary["failures"]} == {
        "slice_unreadable",
        "grid_definition_missing",
        None,
    }
    assert rc != 0


def test_unconfigured_mirror_root_and_unparseable_body_are_failures() -> None:
    png = sorted(_png_urls("gfs", _GFS_CYCLE, _warmed(_GFS_CYCLE)))
    overrides = {
        png[0]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "mirror_root_unconfigured"),
        png[1]: (404, None, None),
        # Whitelist, not exclusion: a reason the backend has not shipped yet must
        # not be absorbed as "expected" by a `!= mirror_root_unconfigured` test.
        png[2]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "some_future_reason"),
    }

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    gfs = summary["per_source"]["gfs"]
    assert gfs["png_not_mirrored"] == 0
    assert gfs["png_failed"] == 3
    assert summary["failed_count"] == 3
    assert rc != 0


def test_an_expected_precip_error_code_on_a_tile_url_is_still_a_failure() -> None:
    """`warm_url` is dumb; only the aggregator knows a URL was a PNG."""
    tile = sorted(_discharge_urls("gfs", _GFS_CYCLE, _steps(_GFS_CYCLE, 1)))[0]
    overrides = {tile: (404, "PRECIP_WINDOW_INCOMPLETE", "missing_slice")}

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    assert summary["failed_count"] == 1
    assert summary["per_source"]["gfs"]["png_window_incomplete"] == 0
    assert summary["failures"][0]["url"] == tile
    assert summary["failures"][0]["error_code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert rc != 0


def test_out_of_horizon_valid_time_is_reported_and_its_discharge_tiles_still_warmed() -> None:
    """The off-grid valid time must be INSIDE the lead window to be an oracle.

    The previous `cycle+171h` outlier is now cut by `PREWARM_LEAD_HOURS` before
    the PNG request-shape gate ever sees it, which would have made every
    assertion below vacuously true. `cycle+4h` is inside the window and off the
    3 h grid, so it exercises the gate itself.
    """
    outlier = "2026-09-02T16:00:00Z"  # _GFS_CYCLE + 4h: in-window, off the 3 h grid
    gfs_times = [*_steps(_GFS_CYCLE, _VALID_TIME_COUNT), outlier]
    warmed = [*_warmed(_GFS_CYCLE), outlier]

    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=gfs_times))

    assert _png_urls("gfs", _GFS_CYCLE, [outlier]).isdisjoint(warmer.urls)
    assert _discharge_urls("gfs", _GFS_CYCLE, [outlier]) <= set(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["png_out_of_contract"] == 1
    assert gfs["png_ok"] == _WARMED_VALID_TIME_COUNT
    assert gfs["valid_times_available"] == _VALID_TIME_COUNT + 1
    assert gfs["valid_times_warmed"] == len(warmed) == _WARMED_VALID_TIME_COUNT + 1
    assert gfs["discharge_requests"] == _DISCHARGE_TILE_COUNT * len(warmed)
    assert gfs["error"] is None
    assert summary["failed_count"] == 0
    assert rc != 0


def test_half_hour_cycle_puts_every_png_out_of_contract_but_still_warms_tiles() -> None:
    half_hour = "2026-09-02T12:30:00Z"
    gfs_times = _steps(half_hour, _VALID_TIME_COUNT)

    rc, summary, warmer, _ = _run(_plan(gfs=half_hour, gfs_valid_times=gfs_times))

    assert not [url for url in warmer.urls if url.startswith(f"{_BASE_URL}/api/v1/precip/gfs/")]
    assert _discharge_urls("gfs", half_hour, _warmed(half_hour)) <= set(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["cycle"] == half_hour
    # Only the WARMED valid times can be out of contract: the ones the lead
    # window dropped were never candidates for a PNG request.
    assert gfs["png_out_of_contract"] == _WARMED_VALID_TIME_COUNT
    assert gfs["png_ok"] == 0
    assert gfs["error"] is None
    assert rc != 0


def test_the_lead_window_is_cut_by_timestamp_not_by_list_position() -> None:
    """Out-of-order in, window-correct out: `valid_times[:5]` must go red here.

    `/valid-times` ordering and step are not properties this script may assume,
    so the cut compares instants. The input below puts `cycle+9h` first and an
    out-of-window `cycle+15h` fourth: a fixed-length prefix would warm `+15h`
    and drop `+12h`.
    """
    in_window = _warmed(_GFS_CYCLE)
    outside = _instant(datetime.fromisoformat(_GFS_CYCLE) + timedelta(hours=15))
    scrambled = [in_window[3], in_window[0], in_window[1], outside, in_window[2], in_window[4]]

    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=scrambled))

    expected = _discharge_urls("gfs", _GFS_CYCLE, in_window) | _png_urls("gfs", _GFS_CYCLE, in_window)
    assert {url for url in warmer.urls if "/gfs/" in url} == expected
    assert _discharge_urls("gfs", _GFS_CYCLE, [outside]).isdisjoint(warmer.urls)
    assert _png_urls("gfs", _GFS_CYCLE, [outside]).isdisjoint(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["valid_times_available"] == len(scrambled) == 6
    assert gfs["valid_times_warmed"] == _WARMED_VALID_TIME_COUNT
    assert gfs["discharge_requests"] == _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT
    assert gfs["png_ok"] == _WARMED_VALID_TIME_COUNT
    # A descope, not an error: the dropped valid time is neither a failure nor
    # out-of-contract.
    assert gfs["png_out_of_contract"] == 0
    assert gfs["error"] is None
    assert summary["failed_count"] == 0
    assert rc == 0


def test_job_submission_interleaves_the_two_sources_lead_by_lead() -> None:
    """A deadline truncation must degrade both sources symmetrically.

    Source-major submission + FIFO means the truncated source is always `ifs`,
    including its lead-0 default view, which is exactly what the frontend shows
    first. `workers=1` makes the pool's execution order the submission order.
    """
    rc, _, warmer, _ = _run(_plan(), workers=1)

    keys = [_group_key(url) for url in warmer.urls]
    assert keys[:_RIVER_TILE_COUNT] == [None] * _RIVER_TILE_COUNT, "river tiles stay first"
    groups: list[tuple[str, str]] = []
    for key in keys[_RIVER_TILE_COUNT:]:
        assert key is not None
        if not groups or groups[-1] != key:
            groups.append(key)
    expected = [
        (source, quote(valid_time, safe=""))
        for valid_time_index in range(_WARMED_VALID_TIME_COUNT)
        for source, cycle in (("gfs", _GFS_CYCLE), ("ifs", _IFS_CYCLE))
        for valid_time in [_warmed(cycle)[valid_time_index]]
    ]
    assert groups == expected
    # The load-bearing property, stated on its own: no source's k=1 is submitted
    # before either source's k=0.
    last_k0 = max(groups.index(key) for key in expected[:2])
    first_k1 = min(groups.index(key) for key in expected[2:4])
    assert last_k0 < first_k1
    assert rc == 0


def test_summary_v2_key_set_and_accounting_identity() -> None:
    plan, overrides, _ = _precip_override_plan()

    _, summary, _, _ = _run(plan, overrides=overrides)

    assert set(summary) == _SUMMARY_V2_KEYS
    accounted = summary["river_tile_count"]
    for entry in summary["per_source"].values():
        accounted += entry["discharge_requests"]
        accounted += entry["png_ok"] + entry["png_not_mirrored"] + entry["png_window_incomplete"] + entry["png_failed"]
    assert summary["requests_total"] == accounted
    assert summary["failures"]
    for entry in summary["failures"]:
        assert "error_code" in entry
        assert "error_reason" in entry
    # The whole summary stays JSON-serialisable, exactly as `main()` prints it.
    json.dumps(summary, ensure_ascii=False, sort_keys=True)


def test_discovery_requests_force_a_display_catalog_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []

    class _Response:
        def read(self) -> bytes:
            return json.dumps({"data": {"default_cycle": None}}).encode()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def _fake_urlopen(request: Any, timeout: float) -> Any:
        captured.append(request)
        return _Response()

    monkeypatch.setattr(prewarm, "urlopen", _fake_urlopen)
    prewarm.fetch_json(f"{_BASE_URL}/api/v1/layers/discharge/cycles?source=gfs", 1.0)

    assert len(captured) == 1
    # urllib capitalizes header names on `add_header`, so look it up the same way.
    assert captured[0].get_header("X-nhms-cache-warm") == "refresh"


def test_warm_url_parses_the_error_envelope_and_survives_a_bodyless_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "request_id": "req-1",
            "status": "error",
            "error": {
                "code": "PRECIP_CYCLE_NOT_MIRRORED",
                "message": "no mirror",
                "details": {"reason": "cycle_not_mirrored"},
            },
        }
    ).encode()
    url = f"{_BASE_URL}/api/v1/precip/gfs/c/v.png"

    monkeypatch.setattr(
        prewarm,
        "urlopen",
        _raising_urlopen(HTTPError(url, 404, "Not Found", email.message.Message(), io.BytesIO(body))),
    )
    result = prewarm.warm_url(url, 1.0)
    assert result.status == 404
    assert result.error_code == "PRECIP_CYCLE_NOT_MIRRORED"
    assert result.error_reason == "cycle_not_mirrored"

    monkeypatch.setattr(
        prewarm, "urlopen", _raising_urlopen(HTTPError(url, 502, "Bad Gateway", email.message.Message(), None))
    )
    bodyless = prewarm.warm_url(url, 1.0)
    assert bodyless.status == 502
    assert bodyless.error_code is None
    assert bodyless.error_reason is None


def test_warm_url_reports_a_truncated_success_body_as_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`IncompleteRead` is an `http.client.HTTPException`, not an `OSError`."""
    assert not isinstance(http.client.IncompleteRead(b""), OSError | ValueError)

    monkeypatch.setattr(prewarm, "urlopen", _raising_urlopen(http.client.IncompleteRead(b"partial")))
    result = prewarm.warm_url(f"{_BASE_URL}/api/v1/tiles/river-network-national/3/6/2.pbf", 1.0)

    assert result.status == 0
    assert result.error == "IncompleteRead"
    assert result.error_code is None


def test_warm_url_survives_an_html_body_and_a_truncated_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real reverse-proxy body and a mid-read cut, not a pre-parsed triple."""
    url = f"{_BASE_URL}/api/v1/precip/gfs/c/v.png"
    html = b"<html><head><title>502 Bad Gateway</title></head><body><h1>502</h1></body></html>"

    monkeypatch.setattr(
        prewarm,
        "urlopen",
        _raising_urlopen(HTTPError(url, 502, "Bad Gateway", email.message.Message(), io.BytesIO(html))),
    )
    result = prewarm.warm_url(url, 1.0)
    assert result.status == 502
    assert result.error_code is None
    assert result.error_reason is None

    class _TruncatedBody(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            raise http.client.IncompleteRead(b'{"error":')

    monkeypatch.setattr(
        prewarm,
        "urlopen",
        _raising_urlopen(HTTPError(url, 404, "Not Found", email.message.Message(), _TruncatedBody(b""))),
    )
    truncated = prewarm.warm_url(url, 1.0)
    # The status code was already known before the body read; a body-read fault
    # must not relabel a classified 404 as a bare transport error.
    assert truncated.status == 404
    assert truncated.error == "HTTP 404"
    assert truncated.error_code is None


def test_incomplete_read_during_discovery_is_a_source_error_not_a_lost_summary() -> None:
    plan = _plan()
    plan["gfs"]["cycles"] = http.client.IncompleteRead(b'{"data":')

    rc, summary, warmer, _ = _run(plan)

    assert "IncompleteRead" in summary["per_source"]["gfs"]["error"]
    assert rc != 0
    assert not [url for url in warmer.urls if "/gfs/" in url]
    # The full summary, not `main()`'s one-line failure envelope.
    assert set(summary) == _SUMMARY_V2_KEYS
    assert summary["per_source"]["ifs"]["png_ok"] == _WARMED_VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["error"] is None


def test_a_cycle_whose_horizon_arithmetic_overflows_is_a_source_error() -> None:
    """`horizon_valid_times` raises `OverflowError` at a year-9999 cycle.

    It is raised by the horizon partition, which runs AFTER discovery returns --
    so this is the oracle for that step living inside the per-source guard.
    """
    plan = _plan(gfs="9999-12-31T00:00:00Z", gfs_valid_times=["9999-12-31T03:00:00Z"])

    rc, summary, warmer, _ = _run(plan)

    assert "OverflowError" in summary["per_source"]["gfs"]["error"]
    assert rc != 0
    assert not [url for url in warmer.urls if "/gfs/" in url]
    assert set(summary) == _SUMMARY_V2_KEYS
    assert summary["per_source"]["ifs"]["png_ok"] == _WARMED_VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["error"] is None


def test_a_raising_warm_call_is_a_failed_request_not_a_lost_summary() -> None:
    """`executor.map` re-raises in the caller, which would destroy the summary."""
    tile = sorted(_river_urls())[0]

    rc, summary, warmer, _ = _run(_plan(), raises={tile: http.client.IncompleteRead(b"")})

    assert summary["requests_total"] == _GOLDEN_REQUESTS_TOTAL
    assert len(warmer.urls) == _GOLDEN_REQUESTS_TOTAL
    assert summary["failed_count"] == 1
    assert summary["failures"][0]["url"] == tile
    assert summary["failures"][0]["error"] == "IncompleteRead"
    assert summary["failures"][0]["status"] == 0
    assert set(summary) == _SUMMARY_V2_KEYS
    # rc 1 == "some requests failed"; rc 2 is reserved for process-level failure.
    assert rc == 1


def test_main_reserves_exit_2_and_the_one_line_envelope_for_process_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(prewarm, "prewarm", _boom)
    rc = prewarm.main(["--base-url", _BASE_URL])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "nhms.node27-mvt-prewarm.v2"
    assert payload["status"] == "failed"
    assert "per_source" not in payload



def test_trailing_slash_base_url_never_produces_a_double_slash_path() -> None:
    """One flow covers all three `rstrip("/")` sites at once.

    `_FakeDiscovery` matches on `urlsplit(url).path`, so a `//api/v1/...`
    regression on the discovery hops surfaces as a source error rather than
    silently passing.
    """
    gfs_times = _warmed(_GFS_CYCLE)
    ifs_times = _warmed(_IFS_CYCLE)

    rc, summary, warmer, _ = _run(_plan(), base_url=_BASE_URL + "/")

    assert set(warmer.urls) == (
        _river_urls()
        | _discharge_urls("gfs", _GFS_CYCLE, gfs_times)
        | _png_urls("gfs", _GFS_CYCLE, gfs_times)
        | _discharge_urls("ifs", _IFS_CYCLE, ifs_times)
        | _png_urls("ifs", _IFS_CYCLE, ifs_times)
    )
    assert not [url for url in warmer.urls if "//api/v1/" in url]
    assert summary["per_source"]["gfs"]["error"] is None
    assert summary["per_source"]["ifs"]["error"] is None
    assert summary["requests_total"] == _GOLDEN_REQUESTS_TOTAL
    assert rc == 0


def test_non_null_cycle_with_an_empty_valid_times_list_is_a_benign_two_hop_race() -> None:
    """`default_cycle` non-null but `/valid-times` empty: the two hops disagreed.

    NOT "this cycle covers nothing": `/cycles` runs the same valid-times
    discovery per candidate cycle and drops the ones that come back empty
    (`services/tiles/mvt.py:1985-1987` `if not discovery.valid_times:
    continue`), so `default_cycle` can never be such a cycle. What IS reachable
    is a benign publish race BETWEEN the two discovery hops -- a same-cycle rerun landing in between with an incomplete
    `run_display_coverage` rectangle makes `_national_coverage_window` return
    `None` (`mvt.py:2129-2135`). It self-heals on the next tick, so the run is
    logged, not alerted: `scripts/node27_autopipe_cron.sh:244` writes the whole
    summary to `$LOG` every tick and `:245` only adds a line on failure.
    """
    rc, summary, warmer, discovery = _run(_plan(gfs_valid_times=[]))

    assert rc == 0
    assert summary["per_source"]["gfs"] == _empty_source_entry(_GFS_CYCLE)
    assert not [url for url in warmer.urls if "/gfs/" in url]
    # The second hop DID happen, at this source's own cycle -- this is not the
    # `default_cycle: null` state, which never reaches `/valid-times`.
    assert discovery.requested_cycles["gfs"] == _GFS_CYCLE
    assert summary["requests_total"] == _RIVER_TILE_COUNT + _PER_SOURCE_REQUESTS
    assert summary["per_source"]["ifs"]["png_ok"] == _WARMED_VALID_TIME_COUNT


def test_elapsed_seconds_spans_discovery_and_warming_not_just_one_phase() -> None:
    clock = _FakeClock(discovery_step=5.0, warm_step=0.125)

    rc, summary, warmer, discovery = _run(_plan(), clock=clock)

    assert len(discovery.urls) == 4  # two hops per source
    assert len(warmer.urls) == _GOLDEN_REQUESTS_TOTAL
    # 4 discovery hops x 5 s + 183 warm requests x 0.125 s. Both terms are
    # required: dropping either phase out of the timed span changes the number.
    assert summary["elapsed_seconds"] == 4 * 5.0 + _GOLDEN_REQUESTS_TOTAL * 0.125
    assert summary["deadline_skipped"] == 0
    assert summary["requests_total"] == _GOLDEN_REQUESTS_TOTAL
    assert rc == 0


def test_the_run_stops_issuing_requests_once_the_wall_clock_deadline_passes() -> None:
    """Not-issued, not cancelled: the deadline is checked inside the pool task."""
    clock = _FakeClock(warm_step=1.0)

    rc, summary, warmer, _ = _run(_plan(), clock=clock, deadline_seconds=10.0, workers=1)

    # Single worker + 1 s per warm request: jobs 0..9 are issued at t=0..9, and
    # the check for job 10 sees t=10.0 >= the deadline.
    assert len(warmer.urls) == 10
    assert set(warmer.urls) < _river_urls()
    assert summary["requests_total"] == 10
    assert summary["deadline_seconds"] == 10.0
    assert summary["deadline_skipped"] == _GOLDEN_REQUESTS_TOTAL - 10
    assert summary["requests_total"] + summary["deadline_skipped"] == _GOLDEN_REQUESTS_TOTAL
    # Not issued means not counted per source either, and it is not an error.
    for source, cycle in (("gfs", _GFS_CYCLE), ("ifs", _IFS_CYCLE)):
        assert summary["per_source"][source] == _empty_source_entry(cycle) | {
            "valid_times_available": _VALID_TIME_COUNT,
            "valid_times_warmed": _WARMED_VALID_TIME_COUNT,
        }
    assert summary["failed_count"] == 0
    # The summary is still emitted in full, and the run still fails.
    assert set(summary) == _SUMMARY_V2_KEYS
    assert rc != 0
    json.dumps(summary, ensure_ascii=False, sort_keys=True)


def test_a_deadline_truncation_still_warms_both_sources_lead_zero_views() -> None:
    """Interleaved submission is what makes a truncated run degrade symmetrically.

    43 river jobs + one lead-0 group per source = 71 requests at 1 s each, so a
    71 s deadline stops right after both sources' lead-0 groups. Under the
    source-major order this diff replaces, `gfs`'s 70 jobs would come first and
    `ifs` would issue nothing at all -- including its default view.
    """
    clock = _FakeClock(warm_step=1.0)
    lead_zero_requests = _DISCHARGE_TILE_COUNT + 1

    rc, summary, warmer, _ = _run(_plan(), clock=clock, deadline_seconds=71.0, workers=1)

    assert len(warmer.urls) == _RIVER_TILE_COUNT + 2 * lead_zero_requests == 71
    for source, cycle in (("gfs", _GFS_CYCLE), ("ifs", _IFS_CYCLE)):
        lead_zero = [_warmed(cycle)[0]]
        assert _discharge_urls(source, cycle, lead_zero) <= set(warmer.urls)
        assert _png_urls(source, cycle, lead_zero) <= set(warmer.urls)
        assert summary["per_source"][source]["png_ok"] == 1
        assert summary["per_source"][source]["discharge_requests"] == _DISCHARGE_TILE_COUNT
    assert summary["deadline_skipped"] == _GOLDEN_REQUESTS_TOTAL - 71
    assert rc != 0


def test_non_positive_deadline_fails_closed() -> None:
    for deadline in (0.0, -1.0):
        try:
            prewarm.prewarm(base_url=_BASE_URL, zooms=[3], workers=1, timeout=1.0, deadline_seconds=deadline)
        except ValueError:
            pass
        else:
            raise AssertionError("a non-positive deadline must fail")


def test_invalid_zoom_and_worker_bounds_fail_closed() -> None:
    for zoom in (-1, 15):
        try:
            prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [zoom])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid zoom must fail")

    try:
        prewarm.prewarm(base_url="http://127.0.0.1", zooms=[3], workers=0, timeout=1)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid worker count must fail")


@pytest.mark.parametrize("zooms", ["", ","])
def test_an_empty_zoom_set_is_rejected_before_prewarm_is_ever_called(
    zooms: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard's oracle, OUTSIDE any stub that could shadow it.

    `AUTOPIPE_MVT_PREWARM_ZOOMS=","` reaches this (`node27_autopipe_cron.sh:243`)
    and `sorted({int(v) for v in ",".split(",") if v.strip()})` is `[]`. Deleting
    `main()`'s `if not zooms` guard used to stay green because the only assertion
    lived inside a `prewarm.prewarm` stub that raised anyway; here the recorder
    fails the test the moment `prewarm()` is reached at all.
    """
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append(kwargs)
        raise AssertionError("argument validation must reject this input before any request is issued")

    monkeypatch.setattr(prewarm, "prewarm", _record)

    rc = prewarm.main(["--zooms", zooms])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "nhms.node27-mvt-prewarm.v2"
    assert payload["status"] == "failed"
    assert "zoom" in payload["error"]
    assert calls == []


def test_the_cli_defaults_are_the_module_constants_the_budget_asserts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget assertions read constants; argparse must not hardcode copies."""
    captured: dict[str, Any] = {}

    def _record(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        captured.update(kwargs)
        return 0, {}

    monkeypatch.setattr(prewarm, "prewarm", _record)

    assert prewarm.main([]) == 0

    assert captured["workers"] == prewarm.DEFAULT_WORKERS
    assert captured["timeout"] == prewarm.DEFAULT_TIMEOUT_SECONDS
    assert captured["deadline_seconds"] == prewarm.DEFAULT_DEADLINE_SECONDS


def _on_unit_active_seconds(timer_text: str) -> float:
    """`OnUnitActiveSec=10min` -> 600.0. Unknown units fail closed."""
    match = re.search(r"^OnUnitActiveSec=(\d+)(s|sec|m|min|h|hr)?\s*$", timer_text, re.MULTILINE)
    if match is None:
        raise AssertionError("the autopipe timer declares no OnUnitActiveSec")
    scale = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}[match.group(2)]
    return float(int(match.group(1)) * scale)


def test_the_default_deadline_plus_one_timeout_fits_inside_the_ingest_tick() -> None:
    """Budget inequality A, read from the unit that actually invokes prewarm.

    This proves that raising `DEFAULT_DEADLINE_SECONDS` or
    `DEFAULT_TIMEOUT_SECONDS` past the tick interval turns red, so one degraded
    run cannot span several ticks. It does NOT prove the run finishes in time --
    only the node-27 receipt of task 7.2 (#2017) measures that.
    """
    tick_seconds = _on_unit_active_seconds(SYSTEMD_AUTOPIPE_TIMER.read_text(encoding="utf-8"))

    assert tick_seconds == 600.0
    assert prewarm.DEFAULT_DEADLINE_SECONDS + prewarm.DEFAULT_TIMEOUT_SECONDS <= tick_seconds


def test_the_worst_case_envelope_fits_inside_the_deadline_at_half_concurrency() -> None:
    """Budget inequality B, recomputed from the code, with nothing hardcoded.

    Cost model, all of it conservative or measured:
      - river tiles at the measured cold river SQL (0.92 s);
      - discharge tiles at the SLOWER of the two measured cold national tiles
        (13.26 s), itself an upper bound because that tile (z4/12/6) is the
        densest one in China;
      - PNGs charged at the SAME discharge-tile upper bound, because cold PNG
        cost is unmeasured (8 slice reads + a render is almost certainly
        cheaper);
      - effective concurrency assumed to be HALF of `DEFAULT_WORKERS`.

    What this proves: changing `PREWARM_LEAD_HOURS`, `DISCHARGE_ZOOMS`,
    `DEFAULT_WORKERS` or `DEFAULT_DEADLINE_SECONDS` forces the budget to be
    redone. What it does NOT prove: that the cost model is right. That oracle is
    the node-27 receipt of task 7.2 (#2017), not this assertion.
    """
    river_tiles = len(prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [3, 4, 5]))
    discharge_tiles_per_valid_time = len(prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, prewarm.DISCHARGE_ZOOMS))
    cycle = datetime(2026, 9, 2, 12, tzinfo=UTC)
    published = [_instant(value) for value in horizon_valid_times(cycle)]
    # The real predicate, not a re-spelling of it, so an inclusivity change here
    # cannot drift from the implementation.
    warmed = len(prewarm.select_lead_window(_instant(cycle), published))
    per_source_requests = warmed * discharge_tiles_per_valid_time + warmed
    cost_seconds = (
        river_tiles * prewarm.MEASURED_COLD_RIVER_TILE_SECONDS
        + len(prewarm.PREWARM_SOURCES) * per_source_requests * prewarm.MEASURED_COLD_DISCHARGE_TILE_SECONDS
    )

    # Today: (43 * 0.92 + 2 * 70 * 13.26) / 4 = 474.0 s. Every count above is
    # recomputed from the code, so none of 43 / 13 / 5 is written down here.
    assert cost_seconds / (prewarm.DEFAULT_WORKERS / 2) <= prewarm.DEFAULT_DEADLINE_SECONDS
