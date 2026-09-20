"""#2033: a user-supplied tile instant never yields a 5xx, plus the cache-key pins.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api import main
from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles import mvt as mvt_module
from services.tiles.mvt import TileInput, cache_key, canonical_mvt_time
from tests.hydro_display_mvt_helpers import (
    _NATIONAL_TILE_X,
    _NATIONAL_TILE_Y,
    _NATIONAL_TILE_Z,
    _budget_row,
    _ExplodingSession,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _legacy_national_url,
    _NationalRouteSession,
    _request_national_identity_tile,
    _Session,
    _TileResult,
)
from tests.river_ts_template_registry import entry_by_key

# ---------------------------------------------------------------------------
# #2033: a user-supplied tile instant never yields a 5xx. Appended at the END
# for the same reason the block above says: the fixture's anchors are ordinal.
# ---------------------------------------------------------------------------

# Well-formed RFC3339 whose shift to UTC leaves `datetime.max` / `datetime.min`.
# CPython answers `OverflowError` -- NOT `ValueError` -- so nothing on the tile
# path caught it and both legacy routes answered 500 (measured on
# `https://test.nwm.ac.cn`, 2026-09-08).
_OUT_OF_UTC_RANGE_INSTANTS = ("9999-12-31T23:59:59-08:00", "0001-01-01T00:00:00+08:00")

# The SAME extremes spelled NAIVE. `canonical_mvt_time` reads a naive instant as
# UTC, so no shift happens and neither one can overflow: they must stay
# ACCEPTED. This pair is what fails if the guard is written as
# `value.astimezone(UTC)` unconditionally.
_NAIVE_EXTREME_INSTANTS = ("9999-12-31T23:59:59", "0001-01-01T00:00:00")

_UNREPRESENTABLE_INSTANT_MESSAGE = "Tile time instants must be representable in UTC."

_LEGACY_RUN_ID = "run_a"


def _legacy_run_url(valid_time: str = "2026-09-03T00:00:00Z") -> str:
    """The legacy single-run alias `/api/v1/tiles/hydro/{run_id}/...`."""
    return (
        f"/api/v1/tiles/hydro/{_LEGACY_RUN_ID}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


class _CountingNationalRouteSession(_NationalRouteSession):
    """`_NationalRouteSession` plus the counter that makes `sql=0` a MEASURED claim.

    `_ExplodingSession` proves "no statement ran" only by turning one into a
    500; acceptance criterion 3 asks for the count itself, because the measured
    pre-fix baseline was `sql=1` on this route (the `source_version=` kwarg
    calls `national_discharge_source_version` before `valid_time=` is ever
    formatted).
    """

    def __init__(self, digest_rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(digest_rows)
        self.execute_count = 0

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        self.execute_count += 1
        return super().execute(statement, params)


class _LegacyRunRouteSession:
    """Answers -- and counts -- the three statements `hydro_mvt_tile` issues.

    `_run_row` (display-ready lookup), `_require_hydro_mvt_source_identity`
    (existence probe) and the tile SQL, in that order. The pre-fix baseline on
    this route was two statements before the 500.
    """

    _RUN_ROW = {
        "run_id": _LEGACY_RUN_ID,
        "status": "published",
        "model_id": "model_a",
        "basin_version_id": "bv_a",
        "source_id": "gfs",
        "cycle_time": "2026-09-02T12:00:00Z",
        "updated_at": "2026-09-02T13:00:00Z",
        "river_network_version_id": "rnv_a",
        "timeseries_store": "legacy",
    }

    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.execute_count = 0
        self.tile_params: list[dict[str, Any]] = []
        self.executions: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        self.execute_count += 1
        sql = str(statement)
        self.executions.append((sql, params))
        if "ST_AsMVT" in sql:
            self.tile_params.append(dict(params or {}))
            return _TileResult([dict(_NationalRouteSession._TILE_ROW)])
        if "FROM hydro.hydro_run h" in sql:
            return _TileResult([dict(self._RUN_ROW)])
        return _TileResult([{"exists": 1}])

    def get_bind(self) -> Any:
        return self.bind

    def rollback(self) -> None:
        return None


@pytest.mark.parametrize("found", (True, False), ids=("found", "not-found"))
def test_hydro_mvt_probe_reads_one_narrow_statement_and_preserves_not_found(found: bool) -> None:
    """The probe's whole contract, with the store argument gone.

    It used to be parametrised over ``timeseries_store`` and asserted the
    rendered statement named one of two physical tables and carried three
    transitional aids on the legacy one. #1342's contract (task 6.3) left one
    table, so what is pinned is that the probe issues exactly ONE statement,
    that the statement is the registered template rendered for ``narrow``, that
    it names the canonical table and no other, and that the 404 payload is
    byte-identical to the routed era's.
    """
    session = _Session([{"exists": 1}] if found else [])
    arguments = {
        "run_id": _LEGACY_RUN_ID,
        "variable": "q_down",
        "valid_time": datetime(2026, 9, 3, tzinfo=UTC),
        "basin_version_id": "bv_a",
        "river_network_version_id": "rnv_a",
    }
    if found:
        hydro_display._require_hydro_mvt_source_identity(session, **arguments)
    else:
        with pytest.raises(ApiError) as raised:
            hydro_display._require_hydro_mvt_source_identity(session, **arguments)
        assert raised.value.status_code == 404
        assert raised.value.code == "MVT_SOURCE_IDENTITY_NOT_FOUND"
        assert raised.value.details == {
            **arguments,
            "layer_id": "discharge",
            "valid_time": "2026-09-03T00:00:00Z",
        }

    assert len(session.executions) == 1
    sql, params = session.executions[0]
    raw = entry_by_key("hydro_display:mvt_source_identity_probe").source("narrow")
    assert sql == render_river_ts_sql(raw, "narrow", entry="hydro_display:mvt_source_identity_probe").sql
    assert re.findall(r"\bFROM\s+hydro\.(river_timeseries\w*)\b", sql) == ["river_timeseries"]
    assert "UNION" not in sql.upper()
    assert "timeseries_store" not in sql
    assert len(re.findall(r"\bLIMIT\s+1\b", sql, re.IGNORECASE)) == 1
    assert params == arguments
    assert set(re.findall(r"(?<!:):([a-z_]+)", sql)) == set(arguments)
    assert session.results[0].first_count == 1
    for column in ("run_key", "basin_version_key", "river_network_version_key", "variable_e"):
        assert re.search(rf"\b{column}\s*=\s*\(", sql)
    assert "unnest(enum_range(NULL::hydro.river_variable))" in sql
    assert "AND valid_time = :valid_time" in sql
    # The three transitional aids are gone with the column they pushed into.
    for column in ("run_id", "river_network_version_id", "variable"):
        assert re.search(rf"\bAND {column} = :{column}\b", sql) is None
    assert sql.count("transitional compressed-chunk pushdown aid") == 0


@pytest.mark.parametrize(
    "metadata, status, code",
    (
        pytest.param({"status": "running"}, 409, "DISPLAY_PRODUCT_NOT_READY", id="not-ready"),
        pytest.param(
            {"basin_version_id": None}, 404, "MVT_SOURCE_IDENTITY_NOT_FOUND", id="no-basin",
        ),
        pytest.param(
            {"river_network_version_id": None}, 404, "MVT_SOURCE_IDENTITY_NOT_FOUND", id="no-network",
        ),
        pytest.param({}, 200, None, id="ready"),
    ),
)
def test_hydro_mvt_route_preserves_error_precedence_and_sql_order(
    metadata: dict[str, Any], status: int, code: str | None, monkeypatch: Any, tmp_path: Path,
) -> None:
    """Precedence and statement COUNT, with the store-invalid arm retired.

    The route used to answer 500 ``TIMESERIES_STORE_INVALID`` when the run's
    routing column was absent, null or unknown, and that 500 had to be reached
    in ONE statement rather than the three the happy path pays. #1342's contract
    (task 6.3) dropped the column, so the arm and its error code are gone; the
    readiness and identity arms that outranked it are unchanged and are pinned
    here with the same statement-count discipline.
    """
    session = _LegacyRunRouteSession()
    session._RUN_ROW = dict(session._RUN_ROW)
    session._RUN_ROW.update(metadata)
    response, captured = _request_national_identity_tile(_legacy_run_url(), session, monkeypatch, tmp_path)

    assert response.status_code == status, response.text
    assert session.execute_count == (3 if status == 200 else 1)
    assert len(session.executions) == session.execute_count
    metadata_sql, metadata_params = session.executions[0]
    assert "FROM hydro.hydro_run h" in metadata_sql
    assert "timeseries_store" not in metadata_sql
    assert metadata_params == {"run_id": _LEGACY_RUN_ID}
    if status != 200:
        assert response.json()["error"]["code"] == code
        assert captured == []
        assert session.tile_params == []
        return

    probe_sql, probe_params = session.executions[1]
    raw = entry_by_key("hydro_display:mvt_source_identity_probe").source("narrow")
    assert probe_sql == render_river_ts_sql(raw, "narrow", entry="hydro_display:mvt_source_identity_probe").sql
    assert re.findall(r"\bFROM\s+hydro\.(river_timeseries\w*)\b", probe_sql) == ["river_timeseries"]
    assert probe_params == {
        "run_id": _LEGACY_RUN_ID,
        "variable": "q_down",
        "valid_time": datetime(2026, 9, 3, tzinfo=UTC),
        "basin_version_id": "bv_a",
        "river_network_version_id": "rnv_a",
    }
    assert "ST_AsMVT" in session.executions[2][0]
    assert response.headers["content-type"] == "application/x-protobuf"
    assert response.headers["x-tile-layer-id"] == "discharge"


def _legacy_route_case(route: str) -> tuple[Any, Any]:
    """`(session, url_builder)` for one of the two legacy tile routes."""
    if route == "national":
        return _CountingNationalRouteSession(), _legacy_national_url
    return _LegacyRunRouteSession(), _legacy_run_url


@pytest.mark.parametrize("form", ["datetime", "str"])
@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
def test_canonical_mvt_time_raises_a_typed_error_outside_the_utc_range(form: str, instant: str) -> None:
    """Task 1.2 / spec "Helper raises a typed error rather than OverflowError".

    BOTH branches: the `isinstance(value, datetime)` one and the one that runs
    after `_parse_iso_datetime`. A `str` here is not a formality -- the cache-row
    comparison in `mvt.py::_read_cache` feeds this helper raw column values.
    """
    value: Any = datetime.fromisoformat(instant) if form == "datetime" else instant

    with pytest.raises(mvt_module.MvtTimeOutOfRangeError) as excinfo:
        canonical_mvt_time(value)

    # The TYPE, not just "some ValueError": `ValueError` is the base class so
    # existing callers keep working, and the subclass is what the route layer
    # translates precisely.
    assert type(excinfo.value) is mvt_module.MvtTimeOutOfRangeError
    assert isinstance(excinfo.value, ValueError)
    # Chained, never swallowed: the CPython original stays diagnosable.
    assert isinstance(excinfo.value.__cause__, OverflowError)


def test_canonical_mvt_time_is_unchanged_for_every_in_range_and_unparseable_value() -> None:
    """The no-shift half of the contract: nothing about canonicalization moves."""
    for spelling in (
        "2026-09-03T00:00:00Z",
        "2026-09-03T00:00:00+00:00",
        "2026-09-03T00:00:00.000Z",
        "2026-09-03T08:00:00+08:00",
    ):
        assert canonical_mvt_time(spelling) == "2026-09-03T00:00:00Z", spelling
        assert canonical_mvt_time(datetime.fromisoformat(spelling.replace("Z", "+00:00"))) == "2026-09-03T00:00:00Z"

    # Sub-second still round-trips rather than truncating (that is what makes
    # the legacy routes' acceptance of it observable at all).
    assert canonical_mvt_time("2026-09-03T00:00:00.500Z") == "2026-09-03T00:00:00.500000Z"
    # Space-separated DB spelling, `None`, and text that is not an instant.
    assert canonical_mvt_time("2026-09-03 00:00:00+00:00") == "2026-09-03T00:00:00Z"
    assert canonical_mvt_time(None) is None
    assert canonical_mvt_time("not-an-instant") == "not-an-instant"
    # The naive extremes: read as UTC, no shift, so the new branch must NOT fire.
    for naive in _NAIVE_EXTREME_INSTANTS:
        assert canonical_mvt_time(naive) == f"{naive}Z", naive
        assert canonical_mvt_time(datetime.fromisoformat(naive)) == f"{naive}Z", naive


@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_reject_an_unrepresentable_instant_before_any_sql(
    route: str, instant: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Acceptance criteria 2 and 3, on the two routes that answered 500.

    `execute_count == 0` is the discriminating assertion: the pre-fix national
    route paid a `national_discharge_source_version` round trip and the
    single-run route paid `_require_display_ready` +
    `_require_hydro_mvt_source_identity` BEFORE raising.
    """
    session, url_for = _legacy_route_case(route)
    response, _ = _request_national_identity_tile(
        url_for(quote(instant, safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", response.text
    # The MESSAGE too: the three tile routes share one validator precisely so
    # they cannot drift on the error body.
    assert error["message"] == _UNREPRESENTABLE_INSTANT_MESSAGE, response.text
    # The whole `details` body, not just code + message: the field name is what
    # points the client at the offending path segment, and the two legacy call
    # sites pass it as an independent literal each. `_require_representable_instant`
    # echoes `value.isoformat()` of the pydantic-parsed aware datetime, which
    # round-trips the requested offset spelling verbatim.
    assert error["details"] == {
        "valid_time": datetime.fromisoformat(instant).isoformat(),
        "expected_format": "YYYY-MM-DDTHH:MM:SSZ",
    }, response.text
    assert session.execute_count == 0


@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_keep_their_unparseable_and_in_range_verdicts(
    route: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Regression either side of the new branch: FastAPI's 422 and the SQL path."""
    session, url_for = _legacy_route_case(route)
    response, _ = _request_national_identity_tile(url_for("not-an-instant"), session, monkeypatch, tmp_path)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text
    # Unchanged: pydantic rejects it before the route body, so still no SQL.
    assert session.execute_count == 0

    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for("2026-09-03T00:00:00Z"), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert session.execute_count > 0
    assert captured[0].valid_time == "2026-09-03T00:00:00Z"


@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_keep_accepting_an_in_range_sub_second_instant(
    route: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Design D3: the legacy routes get the RANGE guard, never the sub-second one.

    Reusing `_require_seconds_precision_instant` here would turn this 200 into a
    422 -- a behavior break on the very routes this change promises to leave
    alone. This is the case that fails if someone "shares one validator" too far.
    """
    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for(quote("2026-09-03T00:00:00.500Z", safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert captured[0].valid_time == "2026-09-03T00:00:00.500000Z"


@pytest.mark.parametrize("instant", _NAIVE_EXTREME_INSTANTS)
@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_still_accept_a_naive_extreme_instant(
    route: str, instant: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Task 3.11a. The naive branch of the guard is load-bearing.

    `valid_time: datetime` is lax on both legacy aliases, so a naive instant is a
    real input class. `value.replace(tzinfo=UTC)` cannot overflow; a guard
    written as `value.astimezone(UTC)` would reinterpret it in SERVER-LOCAL time
    and newly 422 these two -- platform-dependent, and invisible to every
    tz-aware case above.

    `== 200`, not `!= 422`: the spec requirement is "a user-supplied tile instant
    never produces a 5xx", and `!= 422` is satisfied by the 500 it forbids. The
    cache read that fills `captured` happens BEFORE the producer runs, so
    `execute_count > 0` and the `captured[0]` assertion are both already
    satisfied by then -- a producer-side exception left all three green.
    """
    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for(quote(instant, safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert session.execute_count > 0
    assert captured[0].valid_time == f"{instant}Z"


# `cache_key` digests computed by `origin/master`'s `services/tiles/mvt.py`
# (which this branch leaves byte-identical apart from the new failure branch).
# Recorded as literals rather than recomputed from the module under test, so the
# assertion has an oracle outside the code it guards.
_MASTER_NATIONAL_TILE_BASIS: dict[str, Any] = {
    "layer_id": "discharge",
    "source_id": "hydro-national",
    "source_version": "hydro-national-latest-per-basin-stream-type-v3:national-hydro-digest",
    "z": 4,
    "x": 13,
    "y": 6,
    "variant_id": "variable:q_down",
}
_MASTER_NATIONAL_CACHE_KEY = "30470851130440a8aa8144e4a5ecb1dbe402a5020ed00467f41a9f2c4cd119ab"

_MASTER_SIBLING_TILE_BASES: dict[str, dict[str, Any]] = {
    "river-network-national": {
        "layer_id": "river-network",
        "source_id": "river-network-national",
        "source_version": "river-network-national-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
        "variant_id": "national",
    },
    "river-network": {
        "layer_id": "river-network",
        "source_id": "bv_a",
        "source_version": "river-network-basin-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
    },
    "met-stations": {
        "layer_id": "met-stations",
        "source_id": "bv_a",
        "source_version": "met-stations-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
    },
}
_MASTER_SIBLING_CACHE_KEYS = {
    "river-network-national": "6db92a86bbbfd96f2cbedefd292dc63fb073a8a65e6717f534dfff34033d962c",
    "river-network": "d78922eaed1befeb60f8976ce374d5c62dda1bddd7fb8f93a09ad7ab672a2685",
    "met-stations": "2f0b7cc7e02c6e99114b9700254c92fc7068ecbd3cc8ac93d33edf0055561d99",
}


@pytest.mark.parametrize(
    "spelling",
    ["2026-09-03T00:00:00Z", "2026-09-03T00:00:00+00:00", "2026-09-03T00:00:00.000Z", "2026-09-03T08:00:00+08:00"],
)
def test_in_range_instant_spellings_keep_the_cache_key_master_computes(spelling: str) -> None:
    """Published-artifact identity: the four in-range spellings still collapse onto
    ONE digest, and that digest is the one `origin/master` produced."""
    assert cache_key(TileInput(valid_time=spelling, **_MASTER_NATIONAL_TILE_BASIS)) == _MASTER_NATIONAL_CACHE_KEY


@pytest.mark.parametrize("layer", sorted(_MASTER_SIBLING_TILE_BASES))
def test_valid_time_less_sibling_layers_keep_the_cache_key_master_computes(layer: str) -> None:
    """Task 3.8, digest half: the three `valid_time=None` layers are untouched.

    `canonical_mvt_time(None)` returns before either guarded branch, so the new
    failure path cannot be reached from them at all -- pinned rather than argued.
    """
    assert cache_key(TileInput(**_MASTER_SIBLING_TILE_BASES[layer])) == _MASTER_SIBLING_CACHE_KEYS[layer]


@pytest.mark.parametrize(
    ("layer", "url"),
    [
        ("river-network-national", f"/api/v1/tiles/river-network-national/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
        ("river-network", f"/api/v1/tiles/river-network/bv_a/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
        ("met-stations", f"/api/v1/tiles/met-stations/bv_a/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
    ],
)
def test_valid_time_less_sibling_tile_routes_still_answer_the_same_bytes(
    layer: str, url: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Task 3.8, response half: the three sibling routes take no instant at all."""
    session = _NationalRouteSession()
    response, captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert response.content == b"pbf-bytes"
    assert captured[0].valid_time is None
    assert captured[0].layer_id == ("met-stations" if layer == "met-stations" else "river-network")


@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
def test_valid_times_route_keeps_its_unrepresentable_cycle_verdict(instant: str) -> None:
    """Task 3.9: the refactored validator is reached from a SIXTH route.

    `source` AND `cycle` are both given on purpose: a `cycle`-only request is
    rejected by `_validated_national_valid_time_selector`'s "source and cycle
    must be given together." branch and never reaches the validator, so it has
    zero oracle power here. The MESSAGE is asserted for the same reason -- the
    code alone cannot tell those two 422 branches apart.
    """
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/layers/discharge/valid-times", params={"source": "gfs", "cycle": instant}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", response.text
    assert error["message"] == _UNREPRESENTABLE_INSTANT_MESSAGE, response.text


def test_seconds_precision_validator_still_returns_a_utc_normalized_instant() -> None:
    """Task 3.11 / design D4c: the RETURN VALUE is the contract, not a detail.

    `precip.py::_require_whole_hour_instant` reads `.minute`/`.second` off this
    return, and `_RFC3339_INSTANT_RE` accepts half-hour offsets. An extraction
    that returned the caller's ORIGINAL object would let
    `2026-09-02T20:00:00+05:30` (= 14:30 UTC) pass the whole-hour gate and be
    floored to hour 14 by `cycle_token` -- the exact cache poisoning that gate
    exists to prevent, and no error-path test would notice.
    """
    shifted = hydro_display._require_seconds_precision_instant(
        datetime.fromisoformat("2026-09-02T20:00:00+05:30"), "cycle"
    )

    assert shifted.utcoffset() == timedelta(0)
    assert (shifted.hour, shifted.minute) == (14, 30)
    assert shifted == datetime(2026, 9, 2, 14, 30, tzinfo=UTC)

    # The naive branch: read as UTC, never as server-local time.
    naive = hydro_display._require_seconds_precision_instant(datetime(2026, 9, 2, 20, 0), "cycle")

    assert naive == datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
    assert naive.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, b"pbf-bytes"),
        ({"tile": None, "feature_count": 0}, b""),
        ({"source_identity_count": 0}, (424, "MVT_LIVE_POSTGIS_UNAVAILABLE")),
        ({"invalid_property_count": 1, "invalid_properties": "value"}, (500, "MVT_TILE_CONTRACT_INVALID")),
        ({"feature_count": 10001}, (413, "MVT_TILE_BUDGET_EXCEEDED")),
    ],
)
def test_per_basin_consumer_keeps_one_statement_and_first_row_outcomes(
    monkeypatch: Any, overrides: dict[str, Any], expected: Any,
) -> None:
    """One statement, five first-row outcomes, one physical fact table.

    The routing wiring made the per-basin source CTE a two-branch ``UNION ALL``
    told apart by ``timeseries_store``; #1342's contract (task 6.3) left one
    branch. The outcomes below never depended on the routing and are unchanged,
    so the single-statement discipline is asserted with the single-table one.
    """
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([{**_budget_row(6), **overrides}])
    params = {
        "run_id": "run_a", "basin_version_id": "bv_a", "river_network_version_id": "rn_a",
        "variable": "q_down", "valid_time": datetime(2026, 6, 1, tzinfo=UTC),
    }
    if isinstance(expected, bytes):
        assert hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=398, y=197) == expected
    else:
        with pytest.raises(ApiError) as raised:
            hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=398, y=197)
        assert (raised.value.status_code, raised.value.code) == expected
    assert len(session.executions) == 1
    sql, bound = session.executions[0]
    assert sql.count("FROM hydro.river_timeseries ts") == 1
    assert "hydro.river_timeseries_legacy" not in sql
    assert "timeseries_store" not in sql
    assert "UNION ALL" not in sql
    assert set(text(sql)._bindparams) <= bound.keys()
    assert all(bound[key] == value for key, value in params.items())
