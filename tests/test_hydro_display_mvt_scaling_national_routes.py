"""#2007: the hydro-national identity routes and the layer catalog's digest.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api import main
from apps.api.routes import hydro_display, hydro_display_catalog
from services.tiles.mvt import TileInput, cache_key, national_discharge_source_version, postgis_tile_sql
from tests.hydro_display_mvt_helpers import (
    _NATIONAL_CYCLE,
    _NATIONAL_VALID_TIME,
    _dual_patch,
    _entry,
    _ExplodingSession,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _legacy_national_url,
    _national_identity_url,
    _NationalRouteSession,
    _request_national_identity_tile,
    _Session,
)


def test_national_identity_route_collapses_time_spellings_onto_one_bind_and_one_cache_key(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """`.000Z`, `+00:00`, `+08:00`, `-08:00` and `Z` are one instant, one `:cycle` bind, one cache entry.

    `+08:00` is here because the RFC3339 shape gate (#2007 F4) must not narrow
    the accepted spellings to UTC: `2026-09-02T20:00:00+08:00` is the SAME
    instant as the other three and must collapse onto the same canonical
    `2026-09-02T12:00:00Z` bind and cache key, not a fifth one.

    `2026-09-02T04:00:00-08:00` is the same instant again, and it is the ONLY
    accepted NEGATIVE offset anywhere in this file. Without it, narrowing
    `_RFC3339_INSTANT_RE`'s offset alternative from `[+-]` to `\\+` stays green:
    the only other negative-offset spelling in the suite is the year-9999
    reject-set case, which is rejected for RANGE, not shape, and would simply
    start being rejected one layer earlier -- silently taking the `OverflowError`
    guard's last discriminating oracle with it.
    """
    spellings = (
        "2026-09-02T12:00:00.000Z",
        "2026-09-02T12:00:00+00:00",
        "2026-09-02T20:00:00+08:00",
        "2026-09-02T04:00:00-08:00",
        "2026-09-02T12:00:00Z",
    )
    keys: list[str] = []
    binds: list[Any] = []
    captured: list[TileInput] = []
    for index, spelling in enumerate(spellings):
        session = _NationalRouteSession()
        response, captured = _request_national_identity_tile(
            _national_identity_url("gfs", quote(spelling, safe="")),
            session,
            monkeypatch,
            tmp_path / f"cache-{index}",
        )
        assert response.status_code == 200, response.text
        assert response.content == b"pbf-bytes"
        # The single-flight re-reads the cache inside the lock, so one request
        # offers the same TileInput twice; what matters is that it is the same.
        assert captured and len({cache_key(tile) for tile in captured}) == 1
        keys.append(cache_key(captured[0]))
        assert len(session.tile_params) == 1
        binds.append(session.tile_params[0])

    assert len(set(keys)) == 1, keys
    for bound in binds:
        assert bound["source"] == "gfs"
        assert bound["cycle"] == _NATIONAL_CYCLE
        assert bound["valid_time"] == _NATIONAL_VALID_TIME
    # Vacuity guard: the canonical cycle really is inside the cache identity.
    assert ":gfs:2026-09-02T12:00:00Z:" in captured[0].source_version


def test_national_identity_route_gives_two_identities_two_cache_keys(monkeypatch: Any, tmp_path: Any) -> None:
    """EVERY dimension `_national_source_cycle_tile_input` puts in the identity separates the cache.

    `source` and `cycle` ride in `source_version`; `valid_time` and `z`/`x`/`y`
    are fields of the `TileInput` itself, and the whole point of the route is
    that two requests that differ in ANY of them are two cache entries. The
    file cache has no TTL, so a dimension that silently drops out of the key
    serves the second requester the first requester's bytes forever.

    The x/y values are chosen so the two SLOT-SWAP mutations collide, which a
    pairwise `!=` between an arbitrary pair would not catch: against the base
    `(z=4, x=13, y=6)`, `y=y` → `y=x` makes the `y=13` case the base's twin,
    `x=x` → `x=y` makes the `x=6` case the base's twin, and `z=z` → `z=x` makes
    the `z=5` case the base's twin. Hence the assertion is on the SIZE of the
    distinct-key set: one collision in one dimension is one missing key.
    """
    urls = (
        _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
        _national_identity_url("ifs", "2026-09-02T12:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T00:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="2026-09-03T06:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=5),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", x=6),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", y=13),
    )
    keys: list[str] = []
    for index, url in enumerate(urls):
        response, captured = _request_national_identity_tile(
            url,
            _NationalRouteSession(),
            monkeypatch,
            tmp_path / f"cache-{index}",
        )
        assert response.status_code == 200, response.text
        keys.append(cache_key(captured[0]))

    assert len(set(keys)) == len(urls), keys


@pytest.mark.parametrize(
    "url",
    [
        _national_identity_url("ERA5", "2026-09-02T12:00:00Z"),
        _national_identity_url("best", "2026-09-02T12:00:00Z"),
        _national_identity_url("gfs", "not-an-instant"),
        _national_identity_url("gfs", quote("2026-09-02T12:00:00.500Z", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("2026-09-03T00:00:00.500Z", safe="")),
        # Spellings `cycle: datetime` coerces on its own, all of which reached
        # the tile SQL with a 200 before the RFC3339 shape gate: a bare Unix
        # epoch (which silently became 2025-09-02T12:00:00Z), an offset-less
        # local-looking instant, and a space-separated one.
        _national_identity_url("gfs", "1756814400"),
        _national_identity_url("gfs", "2026-09-02T12:00:00"),
        _national_identity_url("gfs", quote("2026-09-02 12:00:00", safe="")),
        # The same three in the `valid_time` position: the gate is on BOTH
        # instants, and a `cycle`-only gate would leave half the route lax.
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="1756814400"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="2026-09-03T00:00:00"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("2026-09-03 00:00:00", safe="")),
        # Well-formed RFC3339 that leaves `datetime`'s range once shifted to
        # UTC: the shape gate passes it and `astimezone` raised `OverflowError`,
        # i.e. an HTTP 500 from a public URL. It is a bad request, so it is 422.
        _national_identity_url("gfs", quote("9999-12-31T23:59:59-08:00", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("9999-12-31T23:59:59-08:00", safe="")),
        # The LOWER bound in both positions too -- task 3.5 says "both extreme
        # instants", and `datetime.min` overflows on a POSITIVE offset, which the
        # year-9999 pair cannot reach. `quote(..., safe="")` is mandatory here:
        # an unescaped `+08:00` decodes as a space and would test a different,
        # shape-invalid input.
        _national_identity_url("gfs", quote("0001-01-01T00:00:00+08:00", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("0001-01-01T00:00:00+08:00", safe="")),
        # `variable` is a path segment on this route too, and the route body's
        # comment claims a bad one costs no SQL. Only the SUPPORTED-set check has
        # a distinct oracle: `SUPPORTED_HYDRO_MVT_VARIABLES == ("q_down",)`, and
        # `q_down` satisfies `SAFE_TILE_IDENTIFIER_RE`, so every shape-invalid
        # spelling is also unsupported and `validate_identifier(variable, ...)`
        # can never be the layer that rejects. Both spellings are pinned anyway
        # because both are client-visible; the malformed one is subsumed.
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", variable="q_up"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", variable=quote("q down", safe="")),
    ],
)
def test_national_identity_route_rejects_a_bad_identity_before_running_any_sql(url: str) -> None:
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    # One rejection contract for the whole route, whichever layer rejects:
    # FastAPI's own path validation (`source`, and the RFC3339 shape gate) and
    # the route body's `ApiError` (sub-second, out-of-UTC-range) must be
    # indistinguishable to a client.
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text


@pytest.mark.parametrize(
    ("case", "url"),
    [
        # z above MVT_MAX_ZOOM (14).
        ("z-too-large", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=15, x=0, y=0)),
        # z below 0. FastAPI parses `-1` as an int path param, so this really
        # does reach `validate_xyz` rather than failing to route.
        ("z-negative", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=-1, x=0, y=0)),
        # In-range z, x/y outside that zoom's 2^z matrix (z=4 -> 0..15).
        ("x-out-of-matrix", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=4, x=16, y=6)),
        ("y-out-of-matrix", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=4, x=13, y=16)),
    ],
)
def test_national_identity_route_rejects_bad_tile_coordinates_before_running_any_sql(
    case: str, url: str
) -> None:
    """`validate_xyz(z, x, y)` on the new route, which nothing pinned before.

    The route's `z`/`x`/`y` are plain `int` path params: the `maximum: 14` /
    `16383` in the runtime OpenAPI comes from
    `apps/api/openapi_patching.py::_patch_mvt_tile_openapi`, which rewrites the
    DOCUMENT and installs no validator. So `validate_xyz` is the only thing
    between a bad coordinate and the tile SQL, and deleting that one line from
    the route left the whole suite green -- `grep -rn "validate_xyz" tests/`
    matched nothing repo-wide.

    Under the deletion the request reaches `national_discharge_source_version`,
    `_ExplodingSession` raises, and the response becomes a 500: both the status
    and the code below move.

    The code is `TILE_XYZ_INVALID`, NOT the `VALIDATION_ERROR` every other
    rejection on this route renders. That is the pre-existing contract of
    `services/tiles/mvt.py::validate_xyz`, shared with the four sibling tile
    routes, and this route joins it rather than inventing a fifth spelling.
    """
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, f"{case}: {response.text}"
    assert response.json()["error"]["code"] == "TILE_XYZ_INVALID", f"{case}: {response.text}"


@pytest.mark.parametrize(
    ("route_name", "url", "expected_identity_binds"),
    [
        (
            "identity",
            _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
            {"source": "gfs", "cycle": _NATIONAL_CYCLE},
        ),
        ("legacy", _legacy_national_url(), {"source": None, "cycle": None}),
    ],
)
def test_every_national_tile_sql_bind_is_supplied_by_the_route_that_executes_it(
    route_name: str, url: str, expected_identity_binds: dict[str, Any], monkeypatch: Any, tmp_path: Any
) -> None:
    """No bind in the national tile SQL may be missing from a call site's params.

    `text()` raises `StatementError: A value is required for bind parameter
    'source'` at execution time, so a bind added to the SQL without a matching
    param is a RUNTIME failure that no fake-session test sees: this file's fake
    session ignores its params entirely, and the real-DB suites that would catch
    it are opt-in. That is exactly how #2007's first pass left four cases in
    `test_river_ts_read_path_surrogate_keys_integration.py` broken. This case
    compares the two sets directly instead of trusting a human to remember.
    """
    declared = set(text(postgis_tile_sql("hydro-national"))._bindparams)
    # Non-vacuity: an empty or truncated `declared` would make the subset check
    # below pass for any call site at all.
    assert {"source", "cycle", "variable", "valid_time", "z", "x", "y"} <= declared

    session = _NationalRouteSession()
    response, _captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert len(session.tile_params) == 1
    supplied = set(session.tile_params[0])
    assert declared - supplied == set(), f"{route_name} route omits binds the SQL declares"
    # ...and the VALUES, not just the key set. A key-set-only check is satisfied
    # by any value at all, so the legacy route binding `source="gfs"` -- which
    # would silently make the source-less alias filter on one source -- passed
    # here, passed
    # `test_each_national_route_hands_the_digest_helper_its_own_identity` (that
    # one asserts the DIGEST binds, a different call), and passed every
    # integration case, because every one of them seeds `_SOURCE_ID = "gfs"`.
    # The behavioral half is
    # `tests/test_mvt_national_identity_probe_integration.py`
    # ::test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path,
    # where the legacy route must serve an `IFS`-only instant.
    identity_binds = {name: session.tile_params[0][name] for name in expected_identity_binds}
    assert identity_binds == expected_identity_binds, route_name


@pytest.mark.parametrize(
    ("route_name", "url", "expected_digest_params"),
    [
        (
            "identity",
            _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
            {"source": "gfs", "cycle": _NATIONAL_CYCLE, "valid_time": _NATIONAL_VALID_TIME},
        ),
        (
            "legacy",
            _legacy_national_url(),
            {"source": None, "cycle": None, "valid_time": _NATIONAL_VALID_TIME},
        ),
    ],
)
def test_each_national_route_hands_the_digest_helper_its_own_identity(
    route_name: str,
    url: str,
    expected_digest_params: dict[str, Any],
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    """The identity route must narrow the digest to `(source, cycle)`; the legacy route must not.

    Freshness, not separation, is what this protects, and that is why nothing
    else catches it: `source_version` embeds the literal source and cycle text,
    so two identities keep two cache keys even when the route drops the kwargs.
    Rebinding the helper to a wrapper that swallows them left this file plus
    `test_api_contract`, `test_openapi_drift` and `test_display_publish_status_only`
    entirely green. What silently breaks is the other half: a RE-RUN of a
    non-latest `(source, cycle)` stops rotating the digest, so the cache key
    does not move, and the tile file cache has no TTL — the stale tile is served
    until something else evicts it.

    `test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one`
    proves the helper honours the arguments; this proves the routes pass them.
    """
    session = _NationalRouteSession()
    response, _captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    # Non-vacuity: the digest really was computed once for this request. An
    # empty list would make the equality below unreachable, and a longer one
    # would mean a third statement now lands on this branch.
    assert len(session.digest_params) == 1, f"{route_name} route: {session.digest_params}"
    assert session.digest_params[0] == expected_digest_params, route_name


def test_national_identity_cache_key_moves_when_the_identity_digest_moves(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The digest must REACH the cache key, which is the freshness half of #2007.

    `test_each_national_route_hands_the_digest_helper_its_own_identity` proves
    the route passes `(source, cycle)` down, and
    `test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one`
    proves the helper uses them -- but neither looks at what the returned digest
    does next. Dropping `:{source_digest}` from `_national_source_cycle_tile_input`'s
    `source_version` leaves both of them green and every identity still gets its
    own cache key (the literal source and cycle text are in there too); what
    breaks is exactly the case the narrowing exists for: a RE-RUN of the SAME
    `(source, cycle)` no longer rotates the key, and the tile file cache has no
    TTL, so the stale tile is served until something else evicts it.

    Same identity, same URL, two different sets of ranked runs -> two keys.
    """
    rerun_rows = [{**_NationalRouteSession._DIGEST_ROWS[0], "run_id": "run_b"}]
    # Non-vacuity: the two digests really do differ, so a difference downstream
    # can be attributed to the digest rather than to anything else.
    assert national_discharge_source_version(_Session(_NationalRouteSession._DIGEST_ROWS)) != (
        national_discharge_source_version(_Session(rerun_rows))
    )

    url = _national_identity_url("gfs", "2026-09-02T12:00:00Z")
    keys: list[str] = []
    for index, rows in enumerate((None, rerun_rows)):
        _response, captured = _request_national_identity_tile(
            url, _NationalRouteSession(rows), monkeypatch, tmp_path / f"cache-{index}"
        )
        assert _response.status_code == 200, _response.text
        keys.append(cache_key(captured[0]))

    assert len(set(keys)) == 2, keys


def test_legacy_national_route_keeps_accepting_the_instant_spellings_it_always_did(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The RFC3339 shape gate is on the NEW route only, and that is load-bearing.

    `Rfc3339Instant` is deliberately not applied to the legacy 5-segment alias:
    `valid_time: datetime` there has always accepted offset-less and
    space-separated spellings, and clients are already sending them. Annotating
    the alias with `Rfc3339Instant` would turn those into 422s -- a silent
    break of the very route this change promises to leave alone -- and nothing
    in the repo noticed, because every legacy case in every suite happens to
    spell its instant `...Z`.

    This is a regression pin on the alias's pre-existing accept-set, not an
    endorsement of lax parsing; the new canonical route is where the shape gate
    lives.
    """
    session = _NationalRouteSession()
    response, captured = _request_national_identity_tile(
        _legacy_national_url(valid_time="2026-09-03T00:00:00"), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert len(session.tile_params) == 1
    # It lands on the same instant the `...Z` spelling does, so this pins the
    # accept-set without also blessing a second cache identity for it.
    assert captured[0].valid_time == "2026-09-03T00:00:00Z"


def _recording_digest_calls(monkeypatch: Any) -> list[dict[str, Any]]:
    """Swap `national_discharge_source_version` for a recorder of its kwargs."""
    recorded: list[dict[str, Any]] = []

    def _recording_digest(_session: Any, **kwargs: Any) -> str:
        recorded.append(kwargs)
        return "national-hydro-digest"

    _dual_patch(monkeypatch, "national_discharge_source_version", _recording_digest)
    return recorded


def test_layer_catalog_digests_the_identity_it_advertises(monkeypatch: Any) -> None:
    """`GET /api/v1/layers` must digest `(default_source, default_cycle)`, nothing wider.

    The OPPOSITE of what this file pinned before #2009. Task 3.1 kept #2007's
    catalog call argument-free *because the catalog change is this issue's*, and
    named the hole verbatim: "一个非最新 `(source, cycle)` 身份的 re-run 不改变 digest,
    `cache_key` 不变而 tile 已陈旧——该洞正是因为本 issue 让旧身份可寻址才被打开". This is
    that issue, and the catalog now advertises `(default_source, default_cycle)`,
    so the digest must observe exactly those runs.

    Why the argument-free form is wrong here and not merely wider: it keeps one
    `rn = 1` row per network across ALL sources and cycles, so in the normal
    propagation state (some networks already on the next cycle) it observes no run
    of the advertised identity at all, and an `ifs` newest cycle blinds it to `gfs`
    entirely. A corrective re-run of the advertised identity would then leave
    `metadata.version` / `cache_version` unchanged, so the frontend's cache token
    and MapLibre source key would not move and the browser would keep the
    superseded tiles. Over-inclusion (an `ifs` landing rotating the `gfs` entry)
    goes away as a side effect.

    `_default_layer_catalog` runs for real here -- stubbing it away is what let the
    previous version of this test pass while asserting the wrong call shape.
    `tests/test_api_contract.py` still passes `national_hydro_source_version` as a
    literal and therefore never reaches the helper.
    """

    class _ValidTimes:
        valid_times = ["2026-09-02T12:00:00Z", "2026-09-02T15:00:00Z"]
        limit = 24
        observed_count = 2
        truncated = False

    recorded = _recording_digest_calls(monkeypatch)
    _dual_patch(
        monkeypatch,
        "national_discharge_cycles",
        lambda _session, **_kwargs: {
            "source": "gfs",
            "cycles": [
                {
                    "cycle_time": "2026-09-02T12:00:00Z",
                    "valid_time_start": "2026-09-02T12:00:00Z",
                    "valid_time_end": "2026-09-02T15:00:00Z",
                }
            ],
            "default_cycle": "2026-09-02T12:00:00Z",
        },
    )
    _dual_patch(
        monkeypatch, "national_discharge_valid_times", lambda _session, **_kwargs: _ValidTimes()
    )
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_1"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(
        hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a")
    )
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display_catalog, "_mvt_live_postgis_enabled", lambda _s: False)
    # `display_catalog_cached` is a process-wide TTL cache; without this the
    # loader may never run and `recorded` would be empty for the wrong reason.
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    # Exactly one call: `list_layers` no longer digests separately, so a leftover
    # argument-free call there would show up here as a second entry.
    assert len(recorded) == 1, recorded
    assert recorded[0] == {"source": "gfs", "cycle": datetime(2026, 9, 2, 12, tzinfo=UTC)}
    # #2031: identity only. Both TILE routes now bind their instant, and the
    # catalog has none -- it advertises a cycle, not a frame. Binding one here
    # would clamp the digest to a single instant of the timeline and leave the
    # entry's `metadata.version` describing whichever instant happened to be
    # picked. Subsumed by the equality above; spelled out because it is the
    # assertion the change's contract names.
    assert "valid_time" not in recorded[0], recorded[0]
    # The digest is scoped to the identity the SAME response advertises.
    assert _entry(response.json()["data"], "discharge")["metadata"]["default_cycle"] == (
        "2026-09-02T12:00:00Z"
    )


@pytest.mark.parametrize(
    ("advertised_default_cycle", "advertised_valid_times"),
    [
        pytest.param(None, [], id="intersection-already-empty-at-the-cycles-query"),
        pytest.param(
            "2026-09-02T12:00:00Z", [], id="intersection-empties-between-the-two-snapshots"
        ),
    ],
)
def test_layer_catalog_falls_back_to_the_argument_free_digest_when_no_cycle_is_advertised(
    monkeypatch: Any, advertised_default_cycle: str | None, advertised_valid_times: list[str]
) -> None:
    """With nothing addressable advertised, the digest must take NO identity kwargs.

    The other half of `test_layer_catalog_digests_the_identity_it_advertises`,
    which only pins the happy identity and stays green if the null branch is
    dropped. What makes the null branch worth its own pin is that dropping it
    fails QUIETLY. `national_discharge_source_version`'s cycle predicate is a
    null passthrough -- `CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time =
    :cycle` (`services/tiles/mvt.py`) -- so calling it with
    `source=NATIONAL_DISCHARGE_DEFAULT_SOURCE, cycle=None` raises nothing and
    selects nothing empty; it silently degenerates to source-only narrowing and
    returns a perfectly plausible digest of the latest `gfs` run per network.
    That is the wrong question for an entry that advertises no identity at all:
    the ranking it should reflect is every network's overall latest run, the one
    the argument-free form answers. Scoped to `gfs`, the digest stops observing
    the rest of the pipeline -- an `ifs` cycle landing, or changing which
    networks are covered, no longer moves `metadata.version`, and the frontend
    derives its cache token and MapLibre source key from exactly that string.
    `default_cycle = null` advertises nothing, so the honest input is every
    ranked run, i.e. the argument-free call.

    The second parameter is the ordering pin (invariant-matrix decision 10):
    `national_discharge_cycles` DOES hand back a cycle, and the emptiness only
    shows up in `national_discharge_valid_times`, which is a real race -- the two
    queries take separate `read committed` snapshots. `_default_layer_catalog`
    forces `default_cycle`/`default_cycle_instant` back to `None` on an empty
    timeline, and the digest must be computed AFTER that forcing. Hoisting the
    digest above it -- a plausible "compute the identity once, up top" cleanup --
    leaves this case digesting a cycle the response then refuses to advertise.
    """

    recorded = _recording_digest_calls(monkeypatch)
    _dual_patch(
        monkeypatch,
        "national_discharge_cycles",
        lambda _session, **_kwargs: {
            "source": "gfs",
            "cycles": [],
            "default_cycle": advertised_default_cycle,
        },
    )
    _dual_patch(
        monkeypatch,
        "national_discharge_valid_times",
        lambda _session, **_kwargs: SimpleNamespace(
            valid_times=advertised_valid_times,
            limit=24,
            observed_count=len(advertised_valid_times),
            truncated=False,
        ),
    )
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_1"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(
        hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a")
    )
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display_catalog, "_mvt_live_postgis_enabled", lambda _s: False)
    # `display_catalog_cached` is a process-wide TTL cache; without this the
    # loader may never run and `recorded` would be empty for the wrong reason.
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(recorded) == 1, recorded
    assert recorded[0] == {}
    # Non-vacuity: the digest is argument-free BECAUSE the entry ended up
    # advertising nothing. `(C, [])` is the forbidden pair, so an empty timeline
    # next to a non-null `default_cycle` would mean the forcing never ran and the
    # argument-free digest above was reached for some other reason.
    discharge = _entry(response.json()["data"], "discharge")
    assert discharge["metadata"]["default_cycle"] is None
    assert discharge["metadata"]["valid_times"] == []



def test_legacy_national_tile_route_digest_binds_the_instant_and_no_identity(monkeypatch: Any) -> None:
    """The 5-segment alias advertises no identity, so its digest must not narrow on one.

    Companion to `test_layer_catalog_digests_the_identity_it_advertises`: the
    catalog moved, this call site did not. Narrowing on `(source, cycle)` would
    change the alias\'s run selection, for a route whose whole contract is
    "unchanged run selection, unchanged bytes".

    `valid_time` is the one exception and it is not a narrowing of the identity
    (#2031): the alias\'s own tile SQL already clamps candidate runs to the
    instant\'s coverage window, so the digest passing the same instant makes the
    cache key describe the run the route actually paints. Its bytes and its
    200/424 verdict are untouched — only the key rotates, once, for instants
    outside the overall-latest run\'s window.
    """
    recorded = _recording_digest_calls(monkeypatch)
    monkeypatch.setattr(
        hydro_display,
        "_cached_or_generated_mvt_response",
        lambda *_a, **_k: hydro_display.Response(
            content=b"pbf", media_type=hydro_display.MVT_MEDIA_TYPE
        ),
    )

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(_legacy_national_url())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(recorded) == 1, recorded
    # Exactly the instant, and nothing else: `source`/`cycle` absent is what
    # keeps the alias source-less.
    assert recorded[0] == {"valid_time": datetime(2026, 9, 3, tzinfo=UTC)}


def test_runtime_openapi_documents_the_national_identity_tile_route() -> None:
    """Without the `mvt_paths` entry the runtime schema and the hand-written yaml
    would be consistently WRONG, so the equality drift test could not catch it."""
    operation = main.create_app().openapi()["paths"][
        "/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"
    ]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert operation["responses"]["424"] == {"$ref": "#/components/responses/MvtNationalIdentityUnavailable"}
    assert operation["responses"]["503"] == {"$ref": "#/components/responses/MvtColdGenerationBusy"}
    assert parameters["variable"]["schema"]["enum"] == ["q_down"]
    assert parameters["source"]["schema"]["enum"] == ["gfs", "ifs"]
    assert parameters["z"]["schema"]["maximum"] == 14
    assert parameters["x"]["schema"]["maximum"] == 16383
    assert parameters["y"]["schema"]["maximum"] == 16383
