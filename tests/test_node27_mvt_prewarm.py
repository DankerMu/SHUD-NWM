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
from services.tiles.mvt import NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_AUTOPIPE_TIMER = REPO_ROOT / "infra" / "systemd" / "nhms-node27-autopipe.timer"
AUTOPIPE_CRON_SCRIPT = REPO_ROOT / "scripts" / "node27_autopipe_cron.sh"

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
    "workers",
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


def test_an_on_grid_valid_time_beyond_the_png_horizon_is_out_of_contract() -> None:
    """The SECOND conjunct of the PNG request-shape gate, asserted on its own.

    `partition_png_valid_times` gates on `valid_time in horizon_valid_times(cycle)`,
    a single membership test that folds two independent conditions together: ON
    the 3 h grid measured from the cycle, AND inside the +168 h forecast horizon.
    Every other case in this file exercises only the grid conjunct -- the
    `cycle+4h` outlier above is off the grid, the half-hour case fails on the
    cycle instead -- so dropping the horizon bound entirely left the whole file
    green (measured, round 5 PROSE-1). This case is the horizon conjunct alone:
    on the grid, far outside the horizon.

    The arithmetic is stated as literals rather than recomputed, because an
    expectation derived by calling `horizon_valid_times` (or by reading
    `PRECIP_FORECAST_HORIZON_HOURS` / `PRECIP_STEP_HOURS`) would be the code's
    own model answering for the code -- round-3 B-1's defect, written up in
    `docs/adr/0003-review-lens-rotation-keep.md`. From `_GFS_CYCLE`
    (`2026-09-02T12:00:00Z`) to `2027-01-01T00:00:00Z` is 2892 h; 2892 = 964 x 3,
    so the instant sits exactly on the 3 h grid, and it is 2724 h past the
    horizon's last member. A single published entry is also the only shape that
    reaches the gate at all: as the sole entry it anchors the lead window itself,
    so `select_lead_window` cannot truncate it away first.

    If the horizon bound is deleted and only the grid test survives, this instant
    becomes "requestable", the PNG goes out, and every assertion below flips.
    """
    far_future = "2027-01-01T00:00:00Z"
    offset = datetime.fromisoformat(far_future) - datetime.fromisoformat(_GFS_CYCLE)
    assert offset == timedelta(hours=2892), "the fixture must stay on the stated offset"
    assert 2892 % 3 == 0, "the fixture must stay ON the 3 h grid, or it proves the wrong conjunct"

    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=[far_future]))

    assert _png_urls("gfs", _GFS_CYCLE, [far_future]).isdisjoint(warmer.urls)
    assert not [url for url in warmer.urls if url.startswith(f"{_BASE_URL}/api/v1/precip/gfs/")]
    # The discharge route carries no such gate, so its tiles are still warmed.
    assert _discharge_urls("gfs", _GFS_CYCLE, [far_future]) <= set(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["png_out_of_contract"] == 1
    assert gfs["png_ok"] == 0
    assert gfs["valid_times_available"] == 1
    assert gfs["valid_times_warmed"] == 1
    assert gfs["discharge_requests"] == _DISCHARGE_TILE_COUNT
    # Out of contract is a reported state, not a discovery error and not a
    # request failure -- but it does have to be noisy.
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


def test_a_clamped_first_valid_time_is_warmed_from_that_entry_not_from_the_cycle() -> None:
    """The coverage-clamped case: `/valid-times` starts well after the cycle.

    `services/tiles/mvt.py:2171` sets `window_start = max(cycle, max(coverage
    starts))`, so a source whose river coverage begins later publishes a list
    whose FIRST entry is not the cycle instant -- and that first entry is what
    `map-layer-timeline-controls` calls lead 0 and what the frontend opens on.
    A cycle-anchored window would have warmed NOTHING here (the first entry is
    15 h out, past `PREWARM_LEAD_HOURS`), at rc=0, with no counter and no
    assertion able to see it.

    The cycle is still the cycle: it is a separate dimension of the URL, and the
    `{cycle}` path segment must stay the published cycle rather than follow the
    anchor.
    """
    head_offset = timedelta(hours=15)
    first = datetime.fromisoformat(_GFS_CYCLE) + head_offset
    assert head_offset > timedelta(hours=prewarm.PREWARM_LEAD_HOURS), "the head offset must clear the window"
    clamped = _steps(_instant(first), _VALID_TIME_COUNT)
    expected_warmed = clamped[:_WARMED_VALID_TIME_COUNT]

    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=clamped))

    gfs_urls = {url for url in warmer.urls if "/gfs/" in url}
    assert gfs_urls == _discharge_urls("gfs", _GFS_CYCLE, expected_warmed) | _png_urls(
        "gfs", _GFS_CYCLE, expected_warmed
    )
    assert {_cycle_segment(url) for url in gfs_urls} == {quote(_GFS_CYCLE, safe="")}
    gfs = summary["per_source"]["gfs"]
    assert gfs["cycle"] == _GFS_CYCLE
    assert gfs["valid_times_available"] == _VALID_TIME_COUNT
    assert gfs["valid_times_warmed"] == _WARMED_VALID_TIME_COUNT
    assert gfs["discharge_requests"] == _DISCHARGE_TILE_COUNT * _WARMED_VALID_TIME_COUNT
    # These clamped entries are on the 3 h grid measured from the CYCLE
    # (`mvt.py:2178` takes ceiling division on that grid) AND inside the 168 h
    # PNG horizon -- cycle+15h .. cycle+27h -- so every one of them gets a PNG.
    # Being on the grid is NOT on its own sufficient: `horizon_valid_times`
    # stops at cycle+168h (`services/precip/mirror.py:106-115`), so a grid
    # instant beyond that is out of contract. That branch is ASSERTED by
    # `test_an_on_grid_valid_time_beyond_the_png_horizon_is_out_of_contract`
    # above -- and only there. The `single-far-future-entry` parametrization
    # below feeds the same input but asserts the exit-code RULE
    # (`rc == (1 if png_out_of_contract else 0)`), never the counter's value, so
    # it stays green with the horizon bound removed. What is pinned here is this
    # window, not the general implication. Even the grid claim holds only while
    # `NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS`
    # (`services/tiles/mvt.py:122`) equals `PRECIP_STEP_HOURS`
    # (`services/precip/constants.py:28`) -- two independently declared 3s, with
    # no assertion coupling them.
    assert gfs["png_ok"] == _WARMED_VALID_TIME_COUNT
    assert gfs["png_out_of_contract"] == 0
    assert gfs["error"] is None
    assert rc == 0


@pytest.mark.parametrize(
    "published",
    [
        pytest.param(_steps(_GFS_CYCLE, _VALID_TIME_COUNT), id="fully-covered"),
        pytest.param(_steps("2026-09-03T03:00:00Z", 4), id="clamped-past-the-window"),
        pytest.param(["2027-01-01T00:00:00Z"], id="single-far-future-entry"),
        pytest.param(list(reversed(_steps(_GFS_CYCLE, 9))), id="descending"),
    ],
)
def test_a_non_empty_published_list_always_warms_at_least_its_first_entry(published: list[str]) -> None:
    """`valid_times_available > 0` implies `valid_times_warmed > 0`, unconditionally.

    This is the property that makes the silent-zero state unreachable, which is
    why there is no `warmed == 0` error branch to test: anchoring the window on
    the earliest published instant means the window always contains it. An error
    branch for a state the code cannot reach would be the same premise-as-guard
    defect wearing a different hat.
    """
    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=published))

    gfs = summary["per_source"]["gfs"]
    assert gfs["valid_times_available"] == len(published)
    assert gfs["valid_times_warmed"] >= 1
    lead_zero = min(published, key=datetime.fromisoformat)
    assert _discharge_urls("gfs", _GFS_CYCLE, [lead_zero]) <= set(warmer.urls)
    # Whatever else the run reports, the source itself discovered cleanly: the
    # window is a descope, never an error.
    assert gfs["error"] is None
    assert summary["failed_count"] == 0
    assert rc == (1 if gfs["png_out_of_contract"] else 0)


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


def test_the_summary_reports_the_effective_worker_count_not_the_module_default() -> None:
    """`workers` must round-trip the PARAMETER, not `DEFAULT_WORKERS`.

    The deployed invocation always overrides `--workers`
    (`scripts/node27_autopipe_cron.sh`), so this key is the only thing that can
    tell the #2017 receipt what concurrency actually ran. A silent fallback to
    the module constant would make the receipt read 8 against a deployed 2 and
    record round-3 B-2 closed on a value production never uses. Membership in
    `_SUMMARY_V2_KEYS` proves the key exists; only this proves its value.

    The discriminating mechanism is that the two counts differ from EACH OTHER,
    not that either differs from `DEFAULT_WORKERS`: any implementation that
    reports a constant `c` yields `(c, c)`, and `(c, c) != (4, 1)` because
    `4 != 1`. So the pair assertion below kills `"workers": DEFAULT_WORKERS`
    whatever the constant happens to be -- including if it were 4 or 1.
    """
    _, four, _, _ = _run(_plan(), workers=4)
    _, one, _, _ = _run(_plan(), workers=1)

    assert (four["workers"], one["workers"]) == (4, 1)
    # Fixture hygiene, NOT a second oracle: the assertion above already carries
    # the whole discriminating power, by the arithmetic in the docstring, and it
    # keeps it even at DEFAULT_WORKERS == 4. This line exists only to tell the
    # maintainer that the fixture counts have collided with the production
    # default, which makes the case harder to read than it needs to be. Nothing
    # about the code under test is wrong when it fires.
    assert prewarm.DEFAULT_WORKERS not in {4, 1}, (
        f"fixture hygiene only: DEFAULT_WORKERS drifted to {prewarm.DEFAULT_WORKERS}, colliding with a "
        "fixture count. The assertion above still discriminates the parameter from the constant; pick "
        "two other distinct counts so the case reads unambiguously"
    )


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


def _cli_kwargs(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, Any]:
    """What `main(argv)` would hand `prewarm()`, with `prewarm()` stubbed out.

    The production argument parser is the only thing that turns a `--zooms`
    string into a zoom list, so every comparison below goes through it instead
    of re-implementing the split.
    """
    captured: dict[str, Any] = {}

    def _record(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        captured.update(kwargs)
        return 0, {}

    monkeypatch.setattr(prewarm, "prewarm", _record)
    assert prewarm.main(argv) == 0
    return captured


def test_the_cli_defaults_are_the_module_constants_the_assertions_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inequality A and the cron-drift check read constants; argparse must not hardcode copies."""
    captured = _cli_kwargs(monkeypatch, [])

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


def test_the_planned_envelope_is_183_requests_derived_end_to_end_from_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size of the envelope prewarm PLANS, computed the way production computes it.

    This replaces the deleted "worst-case cost at half concurrency" inequality,
    which modelled the envelope with a second source for every input it needed
    (the precipitation 3 h grid instead of the discharge stride, a hardcoded
    river zoom list instead of the CLI default) and therefore stayed green when
    the real envelope changed. Here every factor comes from the production path:

      - the published grid steps by `NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS`
        (`services/tiles/mvt.py`), which is what `/valid-times` actually emits;
      - the window is cut by the real `select_lead_window`;
      - the river zoom set is the one `main()`'s parser yields by default, i.e.
        the one the cron passes;
      - the tile counts come from `xyz_tiles`, the URL set from
        `build_warm_url_groups` / `build_warm_urls` including its PNG gate.

    So `183` is the ONLY literal here, and a change to the stride, to
    `PREWARM_LEAD_HOURS`, to `DISCHARGE_ZOOMS`, to the river zoom default or to
    `PREWARM_SOURCES` turns it red. What it does NOT claim is that 183 requests
    fit inside `DEFAULT_DEADLINE_SECONDS`; that has no in-repo oracle and is
    settled only by the node-27 receipt of task 7.2 (#2017).
    """
    cycle_instant = datetime(2026, 9, 2, 12, tzinfo=UTC)
    cycle = _instant(cycle_instant)
    # A grid twice the lead window wide: long enough that the WINDOW is what
    # bounds the envelope whatever the stride is, and deliberately not a model
    # of how far the catalogue actually publishes (that length is irrelevant
    # here -- anything strictly beyond the window gives the same answer).
    span = timedelta(hours=2 * prewarm.PREWARM_LEAD_HOURS)
    stride = timedelta(hours=NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS)
    published = [_instant(cycle_instant + stride * step) for step in range(int(span / stride) + 1)]
    warmed = prewarm.select_lead_window(published)
    assert len(warmed) < len(published), "the synthetic grid must outrun the window, or the golden proves nothing"

    river_zooms = _cli_kwargs(monkeypatch, [])["zooms"]
    planned = len(prewarm.build_river_urls(_BASE_URL, prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, river_zooms)))
    for source in prewarm.PREWARM_SOURCES:
        groups = prewarm.build_warm_url_groups(
            _BASE_URL,
            prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, prewarm.DISCHARGE_ZOOMS),
            source=source,
            cycle=cycle,
            valid_times=warmed,
        )
        planned += sum(len(group) for group in groups)

    # 43 river + 2 x (5 x 13 discharge + 5 PNG). None of 43 / 13 / 5 appears
    # above: they are consequences of the constants, and this is their total.
    assert planned == 183


def _shell_fallback(script_text: str, variable: str) -> str:
    """The single `${VAR:-fallback}` default declared for `variable`.

    Fails closed on two different fallbacks for the same variable, which is the
    drift this would otherwise hide.
    """
    found = set(re.findall(rf"\$\{{{re.escape(variable)}:-([^}}]*)\}}", script_text))
    assert len(found) == 1, f"{variable} must declare exactly one fallback, found {sorted(found)}"
    return found.pop()


def test_the_cron_fallbacks_match_the_module_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed invocation always overrides `--workers` and `--zooms`.

    `scripts/node27_autopipe_cron.sh` passes both flags on every tick, so
    argparse's defaults are never reached in production and a drift between the
    two files is invisible to every other test in this file. Reading the cron
    script here is the same move `_on_unit_active_seconds` already makes for the
    timer unit; nothing modifies it, which `tasks.md`'s Non-goals forbid.
    This closes only the in-repo half: an operator exporting
    `AUTOPIPE_MVT_PREWARM_WORKERS` in the deployed environment is observable
    solely through the summary's `workers` key on the node-27 receipt (#2017).
    """
    cron_text = AUTOPIPE_CRON_SCRIPT.read_text(encoding="utf-8")

    workers = _shell_fallback(cron_text, "AUTOPIPE_MVT_PREWARM_WORKERS")
    zooms = _shell_fallback(cron_text, "AUTOPIPE_MVT_PREWARM_ZOOMS")

    assert int(workers) == prewarm.DEFAULT_WORKERS
    # Both strings go through the production parser rather than being compared
    # as text, so `3,4,5` and `5,4,3` are correctly the same zoom set.
    assert _cli_kwargs(monkeypatch, ["--zooms", zooms])["zooms"] == _cli_kwargs(monkeypatch, [])["zooms"]
