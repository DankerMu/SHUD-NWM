from __future__ import annotations

import email.message
import io
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

from scripts import node27_mvt_prewarm as prewarm

_BASE_URL = "http://127.0.0.1:8080"
_GFS_CYCLE = "2026-09-02T12:00:00Z"
_IFS_CYCLE = "2026-09-02T00:00:00Z"
# The counts the envelope arithmetic is pinned to; `xyz_tiles` is the oracle for
# the coordinates, `test_china_default_working_set_is_small_and_unique` for 43.
_RIVER_TILE_COUNT = 43
_DISCHARGE_TILE_COUNT = 13
_VALID_TIME_COUNT = 57
# 43 + 2 * (13 * 57 + 57) -- discovery calls are NOT counted.
_GOLDEN_REQUESTS_TOTAL = 1639

_ENCODED_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A\d{2}Z")


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _steps(cycle: str, count: int, *, first_step: int = 0) -> list[str]:
    start = datetime.fromisoformat(cycle)
    return [_instant(start + timedelta(hours=3 * step)) for step in range(first_step, first_step + count)]


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


class _FakeDiscovery:
    """Serves the two discovery hops and refuses anything else."""

    def __init__(self, plan: dict[str, dict[str, Any]]) -> None:
        self._plan = plan
        self.urls: list[str] = []
        self.requested_cycles: dict[str, str | None] = {}

    def __call__(self, url: str, timeout: float) -> Any:
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
    def __init__(self, overrides: dict[str, tuple[int, str | None, str | None]] | None = None) -> None:
        self.urls: list[str] = []
        self._overrides = overrides or {}
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: float) -> prewarm.WarmResult:
        with self._lock:
            self.urls.append(url)
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
) -> tuple[int, dict[str, Any], _FakeWarmer, _FakeDiscovery]:
    discovery = _FakeDiscovery(plan)
    warmer = _FakeWarmer(overrides)
    rc, summary = prewarm.prewarm(
        base_url=_BASE_URL,
        zooms=zooms if zooms is not None else [3, 4, 5],
        workers=4,
        timeout=1.0,
        fetch_json=discovery,
        warm=warmer,
    )
    return rc, summary, warmer, discovery


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


def _empty_source_entry(cycle: str | None = None) -> dict[str, Any]:
    return {
        "cycle": cycle,
        "valid_times": 0,
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


def test_envelope_is_river_43_plus_798_per_source_and_counts_1639_warm_requests() -> None:
    gfs_times = _steps(_GFS_CYCLE, _VALID_TIME_COUNT)
    ifs_times = _steps(_IFS_CYCLE, _VALID_TIME_COUNT)

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
    assert len(expected) == _RIVER_TILE_COUNT + 2 * (_DISCHARGE_TILE_COUNT * _VALID_TIME_COUNT + _VALID_TIME_COUNT)
    assert rc == 0
    assert summary["schema"] == "nhms.node27-mvt-prewarm.v2"
    assert summary["requests_total"] == _GOLDEN_REQUESTS_TOTAL
    assert summary["river_tile_count"] == _RIVER_TILE_COUNT
    assert "valid_time" not in summary
    assert isinstance(summary["elapsed_seconds"], float)
    assert summary["elapsed_seconds"] >= 0.0
    assert summary["per_source"]["gfs"]["discharge_requests"] == _DISCHARGE_TILE_COUNT * _VALID_TIME_COUNT
    assert summary["per_source"]["gfs"]["png_ok"] == _VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["png_ok"] == _VALID_TIME_COUNT


def test_discharge_zooms_are_fixed_and_do_not_follow_the_river_zoom_flag() -> None:
    """`--zooms` names the river-network set only; discharge is pinned to z3-z4."""
    rc, summary, warmer, _ = _run(_plan(), zooms=[5])

    assert prewarm.DISCHARGE_ZOOMS == (3, 4)
    assert summary["zooms"] == [5]
    assert summary["discharge_zooms"] == [3, 4]
    assert summary["river_tile_count"] == 30
    assert set(warmer.urls) & _river_urls([5]) == _river_urls([5])
    for source, cycle in (("gfs", _GFS_CYCLE), ("ifs", _IFS_CYCLE)):
        assert summary["per_source"][source]["discharge_requests"] == _DISCHARGE_TILE_COUNT * _VALID_TIME_COUNT
        assert _discharge_urls(source, cycle, _steps(cycle, _VALID_TIME_COUNT)) <= set(warmer.urls)
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
    assert summary["per_source"]["ifs"]["valid_times"] == _VALID_TIME_COUNT


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

    ifs_times = _steps(_IFS_CYCLE, _VALID_TIME_COUNT)
    assert set(warmer.urls) == (
        _river_urls() | _discharge_urls("ifs", _IFS_CYCLE, ifs_times) | _png_urls("ifs", _IFS_CYCLE, ifs_times)
    )
    assert summary["per_source"]["ifs"]["discharge_requests"] == _DISCHARGE_TILE_COUNT * _VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["png_ok"] == _VALID_TIME_COUNT
    assert summary["per_source"]["ifs"]["error"] is None
    assert summary["per_source"]["gfs"]["error"]
    assert summary["requests_total"] == (
        _RIVER_TILE_COUNT + _DISCHARGE_TILE_COUNT * _VALID_TIME_COUNT + _VALID_TIME_COUNT
    )
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
    assert summary["per_source"]["ifs"]["png_ok"] == _VALID_TIME_COUNT


_Overrides = dict[str, tuple[int, str | None, str | None]]


def _precip_override_plan() -> tuple[dict[str, dict[str, Any]], _Overrides, list[str]]:
    gfs_times = _steps(_GFS_CYCLE, _VALID_TIME_COUNT)
    png = sorted(_png_urls("gfs", _GFS_CYCLE, gfs_times))
    overrides = {
        png[0]: (404, "PRECIP_WINDOW_INCOMPLETE", "slice_missing"),
        png[1]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "cycle_not_mirrored"),
        png[2]: (500, "INTERNAL_ERROR", None),
    }
    return _plan(), overrides, png


def test_expected_precip_404s_are_counted_and_a_500_is_still_a_failure() -> None:
    plan, overrides, _ = _precip_override_plan()

    rc, summary, _, _ = _run(plan, overrides=overrides)

    gfs = summary["per_source"]["gfs"]
    assert gfs["png_window_incomplete"] == 1
    assert gfs["png_not_mirrored"] == 1
    assert gfs["png_failed"] == 1
    assert gfs["png_ok"] == _VALID_TIME_COUNT - 3
    assert summary["failed_count"] == 1
    assert [entry["url"] for entry in summary["failures"]] == [sorted(overrides)[2]]
    assert rc != 0


def test_expected_precip_404s_alone_do_not_fail_the_run() -> None:
    gfs_times = _steps(_GFS_CYCLE, _VALID_TIME_COUNT)
    png = sorted(_png_urls("gfs", _GFS_CYCLE, gfs_times))
    overrides = {
        png[0]: (404, "PRECIP_WINDOW_INCOMPLETE", "slice_missing"),
        png[1]: (404, "PRECIP_CYCLE_NOT_MIRRORED", "cycle_not_mirrored"),
    }

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    assert rc == 0
    assert summary["failed_count"] == 0
    assert summary["failures"] == []
    assert summary["per_source"]["gfs"]["png_window_incomplete"] == 1
    assert summary["per_source"]["gfs"]["png_not_mirrored"] == 1


def test_unconfigured_mirror_root_and_unparseable_body_are_failures() -> None:
    gfs_times = _steps(_GFS_CYCLE, _VALID_TIME_COUNT)
    png = sorted(_png_urls("gfs", _GFS_CYCLE, gfs_times))
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
    overrides = {tile: (404, "PRECIP_WINDOW_INCOMPLETE", "slice_missing")}

    rc, summary, _, _ = _run(_plan(), overrides=overrides)

    assert summary["failed_count"] == 1
    assert summary["per_source"]["gfs"]["png_window_incomplete"] == 0
    assert summary["failures"][0]["url"] == tile
    assert summary["failures"][0]["error_code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert rc != 0


def test_out_of_horizon_valid_time_is_reported_and_its_discharge_tiles_still_warmed() -> None:
    # 57 in-horizon steps plus one at cycle+171h.
    gfs_times = [*_steps(_GFS_CYCLE, _VALID_TIME_COUNT), *_steps(_GFS_CYCLE, 1, first_step=57)]
    assert len(gfs_times) == 58
    outlier = gfs_times[-1]
    assert outlier == "2026-09-09T15:00:00Z"

    rc, summary, warmer, _ = _run(_plan(gfs_valid_times=gfs_times))

    assert _png_urls("gfs", _GFS_CYCLE, [outlier]).isdisjoint(warmer.urls)
    assert _discharge_urls("gfs", _GFS_CYCLE, [outlier]) <= set(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["png_out_of_contract"] == 1
    assert gfs["png_ok"] == _VALID_TIME_COUNT
    assert gfs["valid_times"] == 58
    assert gfs["discharge_requests"] == _DISCHARGE_TILE_COUNT * 58
    assert gfs["error"] is None
    assert summary["failed_count"] == 0
    assert rc != 0


def test_half_hour_cycle_puts_every_png_out_of_contract_but_still_warms_tiles() -> None:
    half_hour = "2026-09-02T12:30:00Z"
    gfs_times = _steps(half_hour, _VALID_TIME_COUNT)

    rc, summary, warmer, _ = _run(_plan(gfs=half_hour, gfs_valid_times=gfs_times))

    assert not [url for url in warmer.urls if url.startswith(f"{_BASE_URL}/api/v1/precip/gfs/")]
    assert _discharge_urls("gfs", half_hour, gfs_times) <= set(warmer.urls)
    gfs = summary["per_source"]["gfs"]
    assert gfs["cycle"] == half_hour
    assert gfs["png_out_of_contract"] == _VALID_TIME_COUNT
    assert gfs["png_ok"] == 0
    assert gfs["error"] is None
    assert rc != 0


def test_summary_v2_key_set_and_accounting_identity() -> None:
    plan, overrides, _ = _precip_override_plan()

    _, summary, _, _ = _run(plan, overrides=overrides)

    assert set(summary) == {
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
        "per_source",
    }
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

    def _raise(exc: HTTPError):
        def _fake_urlopen(request: Any, timeout: float) -> Any:
            raise exc

        return _fake_urlopen

    monkeypatch.setattr(
        prewarm, "urlopen", _raise(HTTPError(url, 404, "Not Found", email.message.Message(), io.BytesIO(body)))
    )
    result = prewarm.warm_url(url, 1.0)
    assert result.status == 404
    assert result.error_code == "PRECIP_CYCLE_NOT_MIRRORED"
    assert result.error_reason == "cycle_not_mirrored"

    monkeypatch.setattr(prewarm, "urlopen", _raise(HTTPError(url, 502, "Bad Gateway", email.message.Message(), None)))
    bodyless = prewarm.warm_url(url, 1.0)
    assert bodyless.status == 502
    assert bodyless.error_code is None
    assert bodyless.error_reason is None


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
