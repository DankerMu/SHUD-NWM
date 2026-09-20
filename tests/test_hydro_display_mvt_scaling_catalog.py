"""#2009: the layer catalog and the discovery routes that serve the cycles.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.routes import hydro_display, hydro_display_catalog
from services.tiles.mvt import (
    canonical_mvt_time,
    layer_metadata,
    national_discharge_cycles,
    national_discharge_valid_times,
)
from tests.hydro_display_mvt_helpers import (
    _CYCLE,
    _CYCLE_CONJUNCT,
    _INSTANT_RE,
    _PREVIOUS_CYCLE,
    _SOURCE_CONJUNCT,
    _coverage_row,
    _dual_patch,
    _entry,
    _ExplodingSession,
    _full_coverage_rows,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _NationalDiscoverySession,
    _Rows,
    _Session,
)


def _national_catalog_app(
    monkeypatch: Any,
    session: Any,
    *,
    display_ready: dict[str, Any] | None = None,
) -> Any:
    """A `/api/v1/layers` app whose ONLY live query path is the national discovery one."""
    monkeypatch.setattr(
        hydro_display, "display_ready_run", lambda _session: display_ready or {"run_id": "run_latest"}
    )
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a"))
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    _dual_patch(
        monkeypatch, "national_discharge_source_version", lambda _s, **_k: "national-hydro-v1"
    )
    monkeypatch.setattr(hydro_display_catalog, "_mvt_live_postgis_enabled", lambda _s: False)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    return app


def test_layer_catalog_discharge_entry_is_byte_identical_runless_and_run_scoped(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    # A DIFFERENT run than the latest one, so a `run_id`-dependent default cycle
    # would have to diverge somewhere.
    monkeypatch.setattr(
        hydro_display, "_require_display_ready", lambda _s, run_id: {"run_id": run_id, "status": "published"}
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            runless = client.get("/api/v1/layers")
            run_scoped = client.get("/api/v1/layers", params={"run_id": "run_other"})
    finally:
        app.dependency_overrides.clear()

    assert runless.status_code == 200, runless.text
    assert run_scoped.status_code == 200, run_scoped.text
    discharge = _entry(runless.json()["data"], "discharge")
    assert discharge == _entry(run_scoped.json()["data"], "discharge")

    metadata = discharge["metadata"]
    assert metadata["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert metadata["required_placeholders"] == ["source", "cycle", "valid_time", "z", "x", "y"]
    assert metadata["default_source"] == "gfs"
    assert metadata["default_cycle"] == "2026-09-02T12:00:00Z"
    assert len(metadata["valid_times"]) == 57
    assert metadata["source_refs"] == {}
    assert metadata["maplibre_source_layer"] == "hydro"
    assert "basin_id" in metadata["property_schema"]["required"]
    assert _INSTANT_RE.match(metadata["default_cycle"])
    assert all(_INSTANT_RE.match(instant) for instant in metadata["valid_times"])

    # Unchanged sibling: `river-network` keeps its two caller-shaped templates.
    assert _entry(runless.json()["data"], "river-network")["metadata"]["tile_url_template"] == (
        "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"
    )
    assert _entry(run_scoped.json()["data"], "river-network")["metadata"]["tile_url_template"] == (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"
    )


def test_layer_catalog_keeps_the_discharge_entry_when_the_intersection_is_empty(monkeypatch: Any) -> None:
    """Runs exist, no cycle covers every network: an honest fail-closed entry, not a drop."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    metadata = _entry(response.json()["data"], "discharge")["metadata"]
    assert metadata["default_cycle"] is None
    assert metadata["valid_times"] == []
    assert metadata["default_source"] == "gfs"


class _NetworkVanishesBetweenCallsSession(_NationalDiscoverySession):
    """`read committed`: the catalog's two coverage queries take two snapshots.

    The unbound query (`national_discharge_cycles`) still sees every network
    covered; the `cycle`-bound query that follows finds `rn-c` gone -- what a
    network being newly ACTIVATED without a display-ready run for that cycle, or
    a covered run's status being rewritten, looks like from here.
    """

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        rows = super().execute(statement, params)
        if (params or {}).get("cycle") is None:
            return rows
        return _Rows([row for row in rows.all() if row["river_network_version_id"] != "rn-c"])


def test_layer_catalog_never_advertises_a_cycle_whose_timeline_came_back_empty(monkeypatch: Any) -> None:
    """`(default_cycle=C, valid_times=[])` is a state the contract forbids.

    The empty intersection has exactly one spelling -- `default_cycle` null AND
    `valid_times` empty -- so the frontend cannot read "there is a default cycle,
    it just has no timeline" and pin the map to a cycle nothing can render.
    """
    session = _NetworkVanishesBetweenCallsSession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    metadata = _entry(response.json()["data"], "discharge")["metadata"]
    assert metadata["valid_times"] == []
    assert metadata["default_cycle"] is None
    # Still the fail-closed ENTRY, not a dropped layer (the other empty state).
    assert metadata["default_source"] == "gfs"


def test_layer_catalog_is_precip_only_when_no_run_is_display_ready(monkeypatch: Any) -> None:
    """No ghost discharge/river/met-station entries when nothing hydrological is renderable."""
    session = _NationalDiscoverySession([])
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: None)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["layer_id"] for item in data] == ["precip"]
    assert data[0] == {
        "layer_id": "precip",
        "layer_name": "Precipitation (past 24h)",
        "layer_type": "meteorology",
        "variables": ["precip_24h"],
        "metadata": layer_metadata("precip"),
    }
    for sibling in ("discharge", "river-network", "met-stations"):
        assert sibling not in {item["layer_id"] for item in data}
    # No-run precip must not consult run identity, coverage, or digest queries.
    assert session.executions == []


def test_layer_catalog_rejects_an_unknown_run_without_a_discharge_side_channel(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers", params={"run_id": "run_missing"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert "data" not in response.json()


def test_layer_catalog_rejects_an_explicit_not_ready_run(monkeypatch: Any) -> None:
    session = _Session(
        [
            {
                "run_id": "run_not_ready",
                "status": "running",
                "model_id": "model-a",
                "basin_version_id": "bv_a",
                "source_id": "GFS",
                "cycle_time": "2026-09-02T12:00:00Z",
                "updated_at": "2026-09-02T13:00:00Z",
                "river_network_version_id": "rnv_a",
                "timeseries_store": "narrow",
            }
        ]
    )
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers", params={"run_id": "run_not_ready"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DISPLAY_PRODUCT_NOT_READY"
    assert "data" not in response.json()
    assert len(session.executions) == 1
    assert "FROM hydro.hydro_run h" in session.executions[0][0]


def test_layer_catalog_advertises_the_list_the_valid_times_endpoint_serves(monkeypatch: Any) -> None:
    """One stride implementation: the catalog's list and the endpoint's must be identical.

    The fixture clamps the lower bound (one network starts six hours late), so a
    second, private stride computation in the catalog would have to reproduce the
    clamp as well to stay equal.
    """
    rows = _full_coverage_rows(_CYCLE)
    rows[0] = _coverage_row(
        network="rn-a", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=96)
    )
    session = _NationalDiscoverySession(rows)
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            catalog = client.get("/api/v1/layers")
            metadata = _entry(catalog.json()["data"], "discharge")["metadata"]
            endpoint = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": metadata["default_source"], "cycle": metadata["default_cycle"]},
            )
    finally:
        app.dependency_overrides.clear()

    assert endpoint.status_code == 200, endpoint.text
    assert metadata["valid_times"] == endpoint.json()["data"]["valid_times"]
    assert metadata["valid_times"][0] == "2026-09-02T18:00:00Z"
    assert metadata["valid_times"][-1] == "2026-09-06T12:00:00Z"
    # The advertised list is the per-cycle 3-hour stride, not the no-argument
    # branch's hourly union: falling back to the old list would keep the two
    # sides equal while advertising an identity nothing asked for.
    instants = [datetime.fromisoformat(value) for value in metadata["valid_times"]]
    assert {later - earlier for earlier, later in zip(instants, instants[1:])} == {timedelta(hours=3)}


def test_discharge_cycles_route_returns_the_intersection_in_the_pinned_spelling(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE) + _full_coverage_rows(_PREVIOUS_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers/discharge/cycles", params={"source": "gfs"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "gfs"
    assert data["default_cycle"] == "2026-09-02T12:00:00Z"
    assert [entry["cycle_time"] for entry in data["cycles"]] == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T06:00:00Z",
    ]
    instants = [data["default_cycle"]]
    for entry in data["cycles"]:
        instants.extend([entry["cycle_time"], entry["valid_time_start"], entry["valid_time_end"]])
    assert all(_INSTANT_RE.match(instant) for instant in instants), instants


def test_valid_times_route_serves_the_requested_identity_in_the_pinned_spelling(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    valid_times = response.json()["data"]["valid_times"]
    assert len(valid_times) == 57
    assert valid_times[0] == "2026-09-02T12:00:00Z"
    assert all(_INSTANT_RE.match(instant) for instant in valid_times), valid_times


def test_valid_times_route_without_arguments_keeps_serving_the_national_list(monkeypatch: Any) -> None:
    """The no-argument branch is a live route, not just an internal default.

    The frontend's only valid-times fetch, `fetchLayerValidTimesForCycle`
    (`apps/frontend/src/stores/overviewData.ts`), always passes `source` and
    `cycle`, and so does `scripts/node27_mvt_prewarm.py` since #2013. The branch
    stays part of the public route contract, so making `source`/`cycle`
    mandatory would still be an API break.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers/discharge/valid-times")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    valid_times = response.json()["data"]["valid_times"]
    assert valid_times
    assert all(_INSTANT_RE.match(instant) for instant in valid_times), valid_times


def test_valid_times_cache_key_collapses_spellings_and_separates_identities(monkeypatch: Any) -> None:
    keys: list[str] = []
    options: list[dict[str, Any]] = []

    def _record(_request: Any, key: str, load: Any, **kwargs: Any) -> Any:
        keys.append(key)
        options.append(kwargs)
        return load()

    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", _record)
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for params in (
                {"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
                {"source": "gfs", "cycle": "2026-09-02T12:00:00.000Z"},
                # Same instant again, spelled with a non-UTC offset: only the
                # canonicalized value collapses this one onto the first two.
                {"source": "gfs", "cycle": "2026-09-02T20:00:00+08:00"},
                {"source": "gfs", "cycle": "2026-09-02T15:00:00Z"},
                {"source": "ifs", "cycle": "2026-09-02T12:00:00Z"},
            ):
                assert client.get("/api/v1/layers/discharge/valid-times", params=params).status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert keys[0] == keys[1] == keys[2], keys
    assert len(set(keys)) == 3, keys
    # 客户端可控的 `cycle` 维度必须带准入谓词（空 valid_times 不入缓存）。
    assert all("cacheable" in option for option in options), options


def test_cycles_cache_key_separates_the_two_sources(monkeypatch: Any) -> None:
    """The only red-capable oracle for this route's key.

    `display_catalog_cached` returns `loader()` directly unless `display_readonly`
    is on, and this route's other test discards the key entirely -- so replacing
    it with a constant is green everywhere locally while node-27 serves the gfs
    intersection under `?source=ifs` for a whole cache window.
    """
    keys: list[str] = []
    options: list[dict[str, Any]] = []

    def _record(_request: Any, key: str, load: Any, **kwargs: Any) -> Any:
        keys.append(key)
        options.append(kwargs)
        return load()

    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", _record)
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for source in ("gfs", "ifs"):
                response = client.get("/api/v1/layers/discharge/cycles", params={"source": source})
                assert response.status_code == 200, response.text
    finally:
        app.dependency_overrides.clear()

    assert keys == ["discharge-cycles:gfs", "discharge-cycles:ifs"]
    # `source` 是两值 `Literal`（有界维度），且 fail-closed 的 `cycles: []` 是正当答案：
    # 这条路由**不**传准入谓词，否则空交集期间每次请求都要重跑那趟发现查询。
    assert all("cacheable" not in option for option in options), options


@pytest.mark.parametrize(
    ("case", "path", "params"),
    [
        ("cycle-without-source", "/api/v1/layers/discharge/valid-times", {"cycle": "2026-09-02T12:00:00Z"}),
        ("source-without-cycle", "/api/v1/layers/discharge/valid-times", {"source": "gfs"}),
        (
            "run-id-with-identity",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00Z", "run_id": "run_1"},
        ),
        (
            "identity-on-another-layer",
            "/api/v1/layers/river-network/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
        ),
        (
            "sub-second-cycle",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00.500Z"},
        ),
        (
            "unshaped-cycle",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02 12:00:00"},
        ),
        ("unknown-source", "/api/v1/layers/discharge/valid-times", {"source": "ERA5", "cycle": "2026-09-02T12:00:00Z"}),
        ("cased-source", "/api/v1/layers/discharge/valid-times", {"source": "GFS", "cycle": "2026-09-02T12:00:00Z"}),
        ("cycles-unknown-source", "/api/v1/layers/discharge/cycles", {"source": "ERA5"}),
        ("cycles-best-source", "/api/v1/layers/discharge/cycles", {"source": "best"}),
        ("cycles-cased-source", "/api/v1/layers/discharge/cycles", {"source": "GFS"}),
        ("cycles-missing-source", "/api/v1/layers/discharge/cycles", {}),
    ],
)
def test_national_discovery_routes_reject_half_formed_selectors_before_any_sql(
    case: str, path: str, params: dict[str, str]
) -> None:
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(path, params=params)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text


# ---------------------------------------------------------------------------
# Post-gate site-rule oracles (#2009 review round 4). Appended at the END of the
# file on purpose: every line-number citation in the invariant matrix and in the
# mutation driver anchors on the cases above, so inserting between them would
# invalidate the fixture rather than extend it.
# ---------------------------------------------------------------------------


def test_national_per_cycle_valid_times_fail_closed_when_a_network_activates_between_the_two_statements() -> None:
    """The per-cycle twin of `test_national_cycles_fail_closed_when_a_network_activates...`.

    Row 40's oracle only ever touched the `national_discharge_cycles` site; this
    one drives the SECOND set comparison, the one inside
    `national_discharge_valid_times`' per-cycle branch. Same race, same shape:
    equal cardinality, different membership. The coverage read returns
    `{rn-a, rn-c1, rn-c2}` for the requested cycle; a version switch then swaps
    `rn-a` out for `rn-b`, which never had that cycle at all, so the active read
    returns `{rn-b, rn-c1, rn-c2}`. `3 == 3`, so a cardinality comparison would
    serve `rn-b`'s basins a timeline they cannot render. Order-independent, like
    its `national_discharge_cycles` twin.
    """
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-c1", "rn-c2")),
        active_networks=["rn-b", "rn-c1", "rn-c2"],
    )

    result = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    # Non-vacuity: the two statements really do disagree AT EQUAL SIZE, so a
    # cardinality comparison would pass where the set comparison fails.
    assert len(session.active_networks) == 3
    assert len({row["river_network_version_id"] for row in session.rows}) == 3
    assert result.valid_times == []
    assert result.observed_count == 0
    assert result.truncated is False


def test_no_argument_national_valid_times_are_empty_with_no_coverage_rows() -> None:
    """Zero coverage rows on the no-argument branch: `[]`, not a crash.

    Every other no-argument case in this file feeds the branch at least one row,
    so the `if not latest_by_network` guard had no oracle: deleting it lets the
    empty `coverage` list reach `max(start for start, _ in coverage)`, which
    raises `ValueError` and turns a fail-closed empty timeline into an HTTP 500.
    """
    session = _NationalDiscoverySession([], active_networks=["rn-a", "rn-b"])

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == []
    assert discovery.observed_count == 0


def test_discharge_routes_pass_ifs_through_to_the_coverage_bind(monkeypatch: Any) -> None:
    """`?source=ifs` must reach the SQL bind on BOTH routes, not a `gfs` literal.

    The fixture separates the two sources by CYCLE (`gfs` at `_CYCLE`, `ifs` at
    `_PREVIOUS_CYCLE`), so any call site that hardcodes `gfs` -- the route's own
    `source=` argument, the response echo, or the helper's forward into
    `_national_discharge_coverage_rows` -- answers for the wrong cycle.

    The helper-level NEGATIVE half at the end is the only oracle for the
    forward at `services/tiles/mvt.py`'s per-cycle `_national_discharge_coverage_rows`
    call: dropping `source=source` binds `None`, which both the SQL and this
    file's fake read as "no filter", so the `ifs` half stays green while `gfs`
    at `_PREVIOUS_CYCLE` starts answering with the `ifs` rows.
    """
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=168),
            source="ifs",
            run_id=f"run-ifs-{network}",
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            cycles_response = client.get("/api/v1/layers/discharge/cycles", params={"source": "ifs"})
            ifs_times = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "ifs", "cycle": canonical_mvt_time(_PREVIOUS_CYCLE)},
            )
            gfs_times = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": canonical_mvt_time(_PREVIOUS_CYCLE)},
            )
    finally:
        app.dependency_overrides.clear()

    assert cycles_response.status_code == 200, cycles_response.text
    body = cycles_response.json()["data"]
    assert body["source"] == "ifs"
    assert [entry["cycle_time"] for entry in body["cycles"]] == [canonical_mvt_time(_PREVIOUS_CYCLE)]
    assert body["default_cycle"] == canonical_mvt_time(_PREVIOUS_CYCLE)

    assert ifs_times.status_code == 200, ifs_times.text
    assert len(ifs_times.json()["data"]["valid_times"]) == 57
    assert gfs_times.status_code == 200, gfs_times.text
    assert gfs_times.json()["data"]["valid_times"] == []

    helper_ifs = national_discharge_valid_times(session, source="ifs", cycle=_PREVIOUS_CYCLE)
    helper_gfs = national_discharge_valid_times(session, source="gfs", cycle=_PREVIOUS_CYCLE)
    assert helper_ifs.valid_times
    assert helper_gfs.valid_times == []
    assert helper_gfs.observed_count == 0


def test_national_per_cycle_valid_times_are_not_truncated_when_observed_equals_the_limit() -> None:
    """`truncated` is `observed > limit`, strictly: a full-but-not-over list is complete.

    Today's cases sit at 57 < 100 or 57 > 5, so both spellings agree on them and
    `>=` would ship a list that IS the whole window while telling the frontend
    there is more behind it.
    """
    session = _NationalDiscoverySession(
        [
            _coverage_row(network=network, cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=12))
            for network in ("rn-a", "rn-b", "rn-c")
        ]
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE, limit=5)

    assert len(discovery.valid_times) == 5
    assert discovery.observed_count == 5
    assert discovery.truncated is False


def test_national_per_cycle_valid_times_clamp_an_off_grid_window_inward() -> None:
    """Both clamp ends round INWARD: an advertised instant must be inside the coverage.

    `run_display_coverage` is an hourly grid, so a window can start and end off
    the 3-hour stride. `C+4h … C+97h` has its first stride instant at `C+6h` and
    its last at `C+96h`; rounding the start down would advertise `C+3h` (before
    any basin has data) and rounding the end up would advertise `C+99h` (after
    the earliest coverage end).
    """
    session = _NationalDiscoverySession(
        [
            _coverage_row(
                network=network,
                cycle=_CYCLE,
                start=_CYCLE + timedelta(hours=4),
                end=_CYCLE + timedelta(hours=97),
            )
            for network in ("rn-a", "rn-b", "rn-c")
        ]
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == canonical_mvt_time(_CYCLE + timedelta(hours=6))
    assert discovery.valid_times[-1] == canonical_mvt_time(_CYCLE + timedelta(hours=96))
    assert discovery.observed_count == 31


def test_national_cycles_pass_their_limit_to_the_per_cycle_clamp() -> None:
    """`national_discharge_cycles(limit=)` must reach the per-cycle stride computation.

    No route or catalog call site passes `limit` to this function, so the
    argument's only forwarding site was unobserved: hardcoding the module
    constant there leaves every existing case green (a 168 h rectangle yields 57
    entries, under the constant 100). With `limit=5` the retained list stops at
    the fifth stride instant and the listed window's END is what shows it.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    result = national_discharge_cycles(session, source="gfs", limit=5)

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(_CYCLE)]
    assert result["cycles"][0]["valid_time_start"] == canonical_mvt_time(_CYCLE)
    assert result["cycles"][0]["valid_time_end"] == canonical_mvt_time(_CYCLE + timedelta(hours=12))


def test_national_coverage_statements_pin_their_shape() -> None:
    """TRIPWIRES, not oracles, for the coverage query's SQL shape.

    `_NationalDiscoverySession` never parses SQL: it matches on a table name and
    filters its canned rows by the BOUND values, so every predicate and window
    clause below is invisible to it for any row data whatsoever. The behavioural
    oracle for seven of these eight literals is the node-27 integration file
    (`tests/test_mvt_national_identity_probe_integration.py`, matrix rows 50-56);
    what this case buys is a loud local signal the moment one of them is edited
    or deleted, so the change cannot reach review looking untouched.

    The eighth, `AND rdc.segment_count > 0`, is matrix row 3, and this assertion
    is its ONLY local signal -- its behavioural oracle is likewise on node-27
    (`test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first`).

    `SELECT DISTINCT` is asserted as SHAPE only: as enumeration site 50 it is
    excluded as result-equivalent, so its presence here is a tripwire and never
    a behavioural claim.

    The `:since` predicate already has its own pin in
    `test_national_cycles_list_only_cycles_inside_the_lookback_window` and is
    deliberately not repeated here.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    national_discharge_cycles(session, source="gfs")

    active_sql = next(
        sql
        for sql, _ in session.executions
        if "core.model_instance mi" in sql and "hydro.hydro_run" not in sql
    )
    coverage_sql = next(sql for sql, _ in session.executions if "hydro.run_display_coverage" in sql)

    # The denominator: only ACTIVE instances, and only those naming a network.
    # `AND mi.river_network_version_id IS NOT NULL` also occurs in the coverage
    # statement, so it is asserted against the active statement alone.
    assert "SELECT DISTINCT mi.river_network_version_id" in active_sql
    assert "WHERE mi.active_flag" in active_sql
    assert "AND mi.river_network_version_id IS NOT NULL" in active_sql

    # The ranked read: one winner per (network, cycle), newest run first, both
    # identity conjuncts present, and `rn = 1` selecting that winner.
    assert _SOURCE_CONJUNCT in coverage_sql
    assert _CYCLE_CONJUNCT in coverage_sql
    assert "PARTITION BY mi.river_network_version_id, h.cycle_time" in coverage_sql
    assert "ORDER BY h.run_id DESC" in coverage_sql
    assert "WHERE rn = 1" in coverage_sql

    # The join-side emptiness filter: a coverage row with no segments is not a
    # candidate at all, so it cannot win its (network, cycle) partition.
    assert "AND rdc.segment_count > 0" in coverage_sql
