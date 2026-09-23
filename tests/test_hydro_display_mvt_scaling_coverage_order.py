"""#2153 identity-coverage refusal and #2087 coverage read ORDER.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074). The two blocks
share ``national_discharge_cycle_coverage`` and its two-statement race, so they
stay in one file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apps.api import main
from apps.api.routes import hydro_display_postgis
from services.tiles import mvt as mvt_module
from services.tiles.mvt import TileResponse, national_discharge_cycles, national_discharge_valid_times
from tests.hydro_display_mvt_helpers import (
    _CYCLE,
    _NATIONAL_CYCLE,
    _NATIONAL_TILE_X,
    _NATIONAL_TILE_Y,
    _NATIONAL_TILE_Z,
    _PREVIOUS_CYCLE,
    _full_coverage_rows,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _legacy_national_url,
    _national_identity_url,
    _NationalDiscoverySession,
    _NationalRouteSession,
    _request_national_identity_tile,
    _Rows,
)

# ---------------------------------------------------------------------------
# #2153: the canonical source/cycle national tile refuses a partially covered
# identity, on a cache miss, with the per-cycle valid-times coverage rule.
#
# The helper is reached through the module objects, never imported by name at
# the top of this file: the red proof runs these cases against a tree where it
# does not exist yet, and a top-level import would fail the whole module's
# collection instead of failing the cases that need it.
# ---------------------------------------------------------------------------

_PARTIAL_IDENTITY_URL = _national_identity_url("gfs", "2026-09-02T12:00:00Z")
_INCOMPLETE_CODE = "MVT_NATIONAL_IDENTITY_INCOMPLETE"


def _statement_kinds(session: _NationalRouteSession) -> list[str]:
    return [kind for kind, _sql in session.statements]


def test_canonical_national_tile_refuses_a_partially_covered_identity(monkeypatch: Any, tmp_path: Any) -> None:
    """E1: two of three active networks cover `(gfs, C)` -> 424, no tile SQL, nothing built.

    Pre-#2153 this was a 200 carrying the two networks' features, cached under
    the identity's key: a national map whose third network looks like "no flow".
    """
    session = _NationalRouteSession(active_networks=("rn-a", "rn-b", "rn-c"), coverage_networks=("rn-a", "rn-b"))
    built: list[bytes] = []

    response, _captured = _request_national_identity_tile(
        _PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path, built=built
    )

    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == _INCOMPLETE_CODE
    # Counts, never the network ids: the route is public.
    assert error["details"] == {
        "layer_id": "discharge",
        "source": "gfs",
        "cycle": "2026-09-02T12:00:00Z",
        "covered_network_count": 2,
        "active_network_count": 3,
    }
    assert "tile" not in _statement_kinds(session)
    assert session.tile_params == []
    assert built == []


def test_canonical_national_tile_refuses_an_equal_size_membership_mismatch(monkeypatch: Any, tmp_path: Any) -> None:
    """E4: the two-statement race at equal cardinality fails closed (matrix 40b's tile twin)."""
    session = _NationalRouteSession(
        active_networks=("rn-b", "rn-c1", "rn-c2"), coverage_networks=("rn-a", "rn-c1", "rn-c2")
    )

    response, _captured = _request_national_identity_tile(_PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path)

    # Non-vacuity: equal sizes, different members, so a cardinality compare would serve it.
    assert len(session.active_networks) == len(session.coverage_networks) == 3
    assert set(session.active_networks) != set(session.coverage_networks)
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == _INCOMPLETE_CODE
    assert (error["details"]["covered_network_count"], error["details"]["active_network_count"]) == (3, 3)
    assert "tile" not in _statement_kinds(session)


def test_canonical_national_tile_refuses_a_covered_superset_of_the_active_set(monkeypatch: Any, tmp_path: Any) -> None:
    """#2459: covered is a strict SUPERSET of active -> 424, the `complete` equality's route oracle.

    The shape a network deactivated between the two reads leaves behind while it
    still holds a display-ready run. A "covers at least the active set" relaxation
    of `NationalCycleCoverage.complete` (`>=`) would serve this tile.
    """
    session = _NationalRouteSession(active_networks=("rn-a", "rn-b"), coverage_networks=("rn-a", "rn-b", "rn-c"))
    built: list[bytes] = []

    response, _captured = _request_national_identity_tile(
        _PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path, built=built
    )

    # Non-vacuity: covered really contains every active network and one more.
    assert set(session.coverage_networks) > set(session.active_networks)
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == _INCOMPLETE_CODE
    assert (error["details"]["covered_network_count"], error["details"]["active_network_count"]) == (3, 2)
    assert "tile" not in _statement_kinds(session)
    assert built == []


def test_canonical_national_tile_keeps_the_no_run_verdict_for_an_uncovered_identity(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """E3: covered = empty falls through to the tile SQL's own 424, byte-identical to before."""
    session = _NationalRouteSession(
        active_networks=("rn-a", "rn-b"),
        coverage_networks=(),
        tile_row={**_NationalRouteSession._TILE_ROW, "source_identity_count": 0},
    )

    response, _captured = _request_national_identity_tile(
        _national_identity_url("ifs", "2026-09-02T12:00:00Z"), session, monkeypatch, tmp_path
    )

    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert error["message"] == "Live PostGIS MVT query returned no source rows for the requested identity."
    assert error["details"] == {
        "layer_id": "discharge",
        "z": _NATIONAL_TILE_Z,
        "x": _NATIONAL_TILE_X,
        "y": _NATIONAL_TILE_Y,
    }
    assert len(session.tile_params) == 1


def test_canonical_national_tile_cache_hit_issues_no_coverage_statement(monkeypatch: Any, tmp_path: Any) -> None:
    """E6, hit half: a cached tile is served before the producer, so the check costs nothing."""
    session = _NationalRouteSession(active_networks=("rn-a", "rn-b", "rn-c"), coverage_networks=("rn-a", "rn-b"))
    cached = TileResponse(
        data=b"cached-pbf",
        checksum="cached-checksum",
        etag='W/"cached"',
        cache_key="cached-key",
        cache_status="hit",
        layer_id="discharge",
    )

    response, _captured = _request_national_identity_tile(
        _PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path, cached=cached
    )

    assert response.status_code == 200, response.text
    assert response.content == b"cached-pbf"
    assert _statement_kinds(session) == ["digest"]


def test_canonical_national_tile_with_live_postgis_disabled_issues_no_coverage_statement(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """E6, disabled half: the live-PostGIS gate precedes the coverage pair, same 424 as before."""
    session = _NationalRouteSession(active_networks=("rn-a", "rn-b", "rn-c"), coverage_networks=("rn-a", "rn-b"))

    response, _captured = _request_national_identity_tile(
        _PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path, live_postgis=False
    )

    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert error["message"] == "Live PostGIS MVT is required for canonical .pbf tile routes and is not enabled."
    assert error["details"] == {"layer_id": "hydro-national", "required_env": "NHMS_ENABLE_LIVE_POSTGIS_MVT=true"}
    assert _statement_kinds(session) == ["digest"]


def test_legacy_national_tile_never_evaluates_the_identity_coverage_rule(monkeypatch: Any, tmp_path: Any) -> None:
    """E7: the source-less alias is mixed-cycle by design and binds no identity to check."""
    session = _NationalRouteSession(active_networks=("rn-a", "rn-b", "rn-c"), coverage_networks=("rn-a", "rn-b"))

    response, _captured = _request_national_identity_tile(_legacy_national_url(), session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert response.content == b"pbf-bytes"
    assert _statement_kinds(session) == ["digest", "tile"]
    assert (session.tile_params[0]["source"], session.tile_params[0]["cycle"]) == (None, None)


def test_valid_times_and_the_tile_route_read_one_coverage_helper(monkeypatch: Any, tmp_path: Any) -> None:
    """E5: both call sites go through `national_discharge_cycle_coverage`, so they cannot drift.

    The replacement reports an incomplete identity for data that is really fully
    covered; each surface must then refuse. `hydro_display` imports the helper by
    name, so it is patched in both modules.
    """
    full = _NationalDiscoverySession(_full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")))
    # Non-vacuity: unpatched, this very session yields a non-empty timeline.
    assert national_discharge_valid_times(full, source="gfs", cycle=_CYCLE).valid_times

    incomplete = mvt_module.NationalCycleCoverage(
        rows=[],
        covered_networks=frozenset({"rn-a"}),
        active_networks=frozenset({"rn-a", "rn-b"}),
    )
    calls: list[dict[str, Any]] = []

    def _incomplete_coverage(_session: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return incomplete

    monkeypatch.setattr(mvt_module, "national_discharge_cycle_coverage", _incomplete_coverage)
    monkeypatch.setattr(hydro_display_postgis, "national_discharge_cycle_coverage", _incomplete_coverage)

    assert national_discharge_valid_times(full, source="gfs", cycle=_CYCLE).valid_times == []
    assert calls == [{"source": "gfs", "cycle": _CYCLE}]

    session = _NationalRouteSession()
    response, _captured = _request_national_identity_tile(_PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path)

    assert response.status_code == 424, response.text
    assert response.json()["error"]["code"] == _INCOMPLETE_CODE
    assert calls[1:] == [{"source": "gfs", "cycle": _NATIONAL_CYCLE}]
    assert "tile" not in _statement_kinds(session)


@pytest.mark.parametrize(
    ("case", "covering", "active", "complete"),
    [
        ("partial", ("rn-a", "rn-b"), ["rn-a", "rn-b", "rn-c"], False),
        ("full", ("rn-a", "rn-b", "rn-c"), ["rn-a", "rn-b", "rn-c"], True),
        ("empty", (), ["rn-a", "rn-b"], False),
        ("equal-size-mismatch", ("rn-a", "rn-c1", "rn-c2"), ["rn-b", "rn-c1", "rn-c2"], False),
        ("covered-superset", ("rn-a", "rn-b", "rn-c"), ["rn-a", "rn-b"], False),
    ],
)
def test_national_cycle_coverage_helper_compares_sets_like_the_per_cycle_valid_times(
    case: str, covering: tuple[str, ...], active: list[str], complete: bool
) -> None:
    """E5: the helper's verdict IS the per-cycle valid-times verdict, on the same inputs.

    `_PREVIOUS_CYCLE` rows for every active network are mixed in so the `:cycle`
    bind, not the fixture, is what keeps them out of the covered set.
    """
    rows = _full_coverage_rows(_CYCLE, networks=covering) + _full_coverage_rows(
        _PREVIOUS_CYCLE, networks=tuple(active)
    )
    helper_session = _NationalDiscoverySession(rows, active_networks=active)

    coverage = mvt_module.national_discharge_cycle_coverage(helper_session, source="gfs", cycle=_CYCLE)

    assert coverage.covered_networks == frozenset(covering), case
    assert coverage.active_networks == frozenset(active), case
    assert coverage.complete is complete, case
    assert [row["river_network_version_id"] for row in coverage.rows] == list(covering), case
    # The same two statements, with the same binds, the valid-times branch always
    # issued -- and in the #2087 order: the identity-bound coverage read first, the
    # unbound active-set read second, so an activation between them cannot hide in
    # the coverage rows.
    assert [params for _sql, params in helper_session.executions] == [
        {"source": "gfs", "cycle": _CYCLE, "since": None},
        None,
    ], case

    discovery = national_discharge_valid_times(
        _NationalDiscoverySession(rows, active_networks=active), source="gfs", cycle=_CYCLE
    )
    assert bool(discovery.valid_times) is complete, case


def test_runtime_openapi_documents_both_424_codes_on_the_canonical_national_route_only() -> None:
    """E8: the new code is documented where it can be emitted, and nowhere else."""
    schema = main.create_app().openapi()
    canonical = "/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"
    siblings = (
        "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
        "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
    )

    def _code_enum(name: str) -> list[str]:
        response = schema["components"]["responses"][name]
        return response["content"]["application/json"]["schema"]["properties"]["error"]["properties"]["code"]["enum"]

    assert schema["paths"][canonical]["get"]["responses"]["424"] == {
        "$ref": "#/components/responses/MvtNationalIdentityUnavailable"
    }
    for path in siblings:
        assert schema["paths"][path]["get"]["responses"]["424"] == {
            "$ref": "#/components/responses/MvtLivePostgisUnavailable"
        }, path
    assert _code_enum("MvtNationalIdentityUnavailable") == ["MVT_LIVE_POSTGIS_UNAVAILABLE", _INCOMPLETE_CODE]
    assert _code_enum("MvtLivePostgisUnavailable") == ["MVT_LIVE_POSTGIS_UNAVAILABLE"]
    assert _code_enum("MvtColdGenerationBusy") == ["MVT_COLD_GENERATION_BUSY"]
    assert schema["components"]["responses"]["MvtColdGenerationBusy"]["headers"] == {
        "Retry-After": {"schema": {"type": "string"}},
        "Cache-Control": {"schema": {"type": "string"}},
        "X-Request-ID": {"schema": {"type": "string"}},
    }
    for path in (canonical, *siblings):
        assert schema["paths"][path]["get"]["responses"]["503"] == {
            "$ref": "#/components/responses/MvtColdGenerationBusy"
        }, path


# Never recomputed from the module under test: the #2153 refusal must leave a
# fully covered identity's cache key and tile binds alone.
#
# Deliberately re-pinned by #2165 (design D4, a contract change, not a
# loosening): `NATIONAL_DISCHARGE_QUERY_VERSION` moved v5 -> v6 and the
# hydro-national `feature_limit` bind moved 10000 -> 20000. The key below was
# captured on a scratch export of 258b06ecf with ONLY the
# `NATIONAL_DISCHARGE_QUERY_VERSION` literal edited to `fair-network-budget-v6`,
# running exactly this test's request; reverting that literal to v5 in the same
# export reproduced the previous pin `f408b0dfaa54...` (captured on the
# pre-#2153 tree, origin/master 015423c71), so the version literal is the only
# input that moved. The cache key hashes neither the SQL nor the binds, so the
# `feature_limit` re-pin below does not feed it.
_PRE_2153_FULL_COVERAGE_CACHE_KEY = "0b6ae6d0223c3a694e233bbcb01092fffff733a66b299a53acad78105efcd4a0"
_PRE_2153_FULL_COVERAGE_TILE_BINDS: dict[str, Any] = {
    "variable": "q_down",
    "valid_time": datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    "source": "gfs",
    "cycle": datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    "z": 4,
    "x": 13,
    "y": 6,
    "feature_limit": 20000,
    "feature_coordinate_limit": 50000,
    "collection_coordinate_limit": 50000,
    "max_coordinate_dimensions": 3,
    "extent": 4096,
    "buffer": 64,
    "simplification_tolerance_m": 256.0,
}


def test_canonical_national_tile_serves_a_fully_covered_identity_unchanged(monkeypatch: Any, tmp_path: Any) -> None:
    """E2: every active network covers `(gfs, C)` -> 200 with the pre-#2153 key and binds.

    The coverage pair runs once each, bound to the requested identity, between the
    digest and the tile SQL. Byte-level sameness on real rows is E11's job.

    The expected key and `feature_limit` were re-pinned by #2165 (design D4 contract
    change: v6 query version, 20,000 national discharge feature budget); see the
    derivation above `_PRE_2153_FULL_COVERAGE_CACHE_KEY`.
    """
    session = _NationalRouteSession(
        active_networks=("rn-a", "rn-b", "rn-c"), coverage_networks=("rn-a", "rn-b", "rn-c")
    )

    response, _captured = _request_national_identity_tile(_PARTIAL_IDENTITY_URL, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert response.content == b"pbf-bytes"
    assert _statement_kinds(session) == ["digest", "coverage", "active", "tile"]
    assert len(session.active_params) == 1
    assert session.coverage_params == [{"source": "gfs", "cycle": _NATIONAL_CYCLE, "since": None}]
    assert response.headers["X-Tile-Cache-Key"] == _PRE_2153_FULL_COVERAGE_CACHE_KEY
    assert session.tile_params == [_PRE_2153_FULL_COVERAGE_TILE_BINDS]


# ---------------------------------------------------------------------------
# #2087: the ORDER of `_national_discharge_coverage_rows`' two statements. Also
# appended at the END, for the reason stated above the #2009 round-4 block.
# ---------------------------------------------------------------------------


class _ActivationBetweenStatementsSession(_NationalDiscoverySession):
    """A network is activated BETWEEN the helper's two reads.

    The name says "activation" because that is the case #2087 set out to close,
    but nothing here constrains the DIRECTION: `active_after` may be any set, so
    the same fake models a deactivation (`active_after` smaller) and a version
    switch (equal size, different members). The deactivation cases below use it
    as-is. Kept under this name rather than renamed because the invariant matrix's
    row 40d cites it by name as the mutation oracle.

    Deliberately order-INDEPENDENT, so the same case is red under the old
    statement order and green under the #2087 one: the activation lands after the
    FIRST `execute()`, whichever statement that turns out to be. A fake keyed on
    "the active-set statement" instead would encode the very order under test and
    could not distinguish the two.

    The coverage branch filters its rows by the active set current AT THAT
    EXECUTION, because `mi.active_flag` sits inside the real coverage statement:
    a snapshot taken before the activation cannot see the newcomer's rows. Without
    that filter the partial-coverage case would hand back the newcomer's rows from
    a pre-activation snapshot and prove less than the SQL does.

    A subclass, never an edit to `_NationalDiscoverySession`: the base
    deliberately answers the coverage query without consulting `active_networks`,
    and the fail-closed fixtures above pass an explicit `active_networks=` WIDER
    than their rows precisely to exercise that.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        active_before: list[str],
        active_after: list[str],
    ) -> None:
        super().__init__(rows, active_networks=active_before)
        self.active_before = list(active_before)
        self.active_after = list(active_after)
        # What the coverage statement actually handed back, per execution: the
        # non-vacuity oracle, so a case cannot pass on an empty fixture.
        self.served_coverage_rows: list[list[dict[str, Any]]] = []

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        # Evaluated BEFORE `super().execute`, which is what appends to
        # `executions`: read 1 sees the pre-activation state, every later read the
        # post-activation one.
        self.active_networks = self.active_before if not self.executions else self.active_after
        result = super().execute(statement, params)
        if "hydro.run_display_coverage" not in str(statement):
            return result
        visible = frozenset(self.active_networks)
        served = [row for row in result.all() if row["river_network_version_id"] in visible]
        self.served_coverage_rows.append(served)
        return _Rows(served)


def _zero_coverage_activation_session() -> _ActivationBetweenStatementsSession:
    """`rn-a` is activated between the reads holding NO display-ready run at all."""
    return _ActivationBetweenStatementsSession(
        _full_coverage_rows(_CYCLE, networks=("rn-b", "rn-c")),
        active_before=["rn-b", "rn-c"],
        active_after=["rn-a", "rn-b", "rn-c"],
    )


def _partial_coverage_activation_session() -> _ActivationBetweenStatementsSession:
    """`rn-a` is activated between the reads with a run for `_CYCLE` (K) but not `_PREVIOUS_CYCLE` (J)."""
    return _ActivationBetweenStatementsSession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b", "rn-c"))
        + _full_coverage_rows(_PREVIOUS_CYCLE, networks=("rn-b", "rn-c")),
        active_before=["rn-b", "rn-c"],
        active_after=["rn-a", "rn-b", "rn-c"],
    )


def test_national_cycles_close_when_a_network_is_activated_with_zero_coverage_rows() -> None:
    """Branch (a) of #2087, at the `cycles` site.

    A newcomer with no display-ready row contributes to NO cycle's covered set, so
    no comparison of coverage-statement output can see it. Reading the coverage
    rows FIRST makes the denominator the younger set instead: `covered` lacks
    `rn-a`, the active read has it, every cycle fails closed. Under the old order
    the covered set equalled the stale active set and the cycle was listed.
    """
    session = _zero_coverage_activation_session()

    result = national_discharge_cycles(session, source="gfs")

    # Non-vacuity: three active networks after the activation, and the coverage
    # statement really did serve rows -- an empty fixture would pass regardless.
    assert len(session.active_after) == 3
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_per_cycle_valid_times_close_when_a_network_is_activated_with_zero_coverage_rows() -> None:
    """Branch (a) of #2087, at the per-cycle valid-times site (through `NationalCycleCoverage`)."""
    session = _zero_coverage_activation_session()

    result = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert len(session.active_after) == 3
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    assert result.valid_times == []
    assert result.observed_count == 0


def test_national_cycles_close_both_cycles_when_the_newcomer_covers_only_the_newer_one() -> None:
    """Branch (b) of #2087: rows for K but not J must close J as well as K.

    The set comparison alone (#2073) only ever caught K -- for J the newcomer is
    absent from the covered set, which then equals the stale active set. With the
    coverage read first, `rn-a`'s K rows are filtered out by the pre-activation
    snapshot's `active_flag` too, so both cycles land unequal.
    """
    session = _partial_coverage_activation_session()

    result = national_discharge_cycles(session, source="gfs")

    assert len(session.active_after) == 3
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    # Not a disguised branch (a): the newcomer really does hold a run for K.
    assert any(
        row["river_network_version_id"] == "rn-a" and row["cycle_time"] == _CYCLE for row in session.rows
    )
    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_per_cycle_valid_times_close_the_older_cycle_the_newcomer_does_not_cover() -> None:
    """Branch (b) of #2087 at the per-cycle site, asked for J -- the cycle the newcomer misses."""
    session = _partial_coverage_activation_session()

    result = national_discharge_valid_times(session, source="gfs", cycle=_PREVIOUS_CYCLE)

    assert len(session.active_after) == 3
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    assert any(
        row["river_network_version_id"] == "rn-a" and row["cycle_time"] == _CYCLE for row in session.rows
    )
    assert result.valid_times == []
    assert result.observed_count == 0


# ---------------------------------------------------------------------------
# #2087 round 2: the DEACTIVATION half of the read-order trade, and a
# CHARACTERIZATION of the fail-open class the swap newly opens. The four cases
# above cover activation (numerator growth) only; design D2 distinguishes two
# deactivation outcomes and accepts one residual, and none of the three had an
# oracle.
# ---------------------------------------------------------------------------


def _zero_coverage_deactivation_session() -> _ActivationBetweenStatementsSession:
    """`rn-d` is DEACTIVATED between the reads holding NO display-ready run at all."""
    return _ActivationBetweenStatementsSession(
        _full_coverage_rows(_CYCLE, networks=("rn-b", "rn-c")),
        active_before=["rn-b", "rn-c", "rn-d"],
        active_after=["rn-b", "rn-c"],
    )


def _covered_deactivation_session() -> _ActivationBetweenStatementsSession:
    """`rn-d` is DEACTIVATED between the reads while HOLDING a display-ready run."""
    return _ActivationBetweenStatementsSession(
        _full_coverage_rows(_CYCLE, networks=("rn-b", "rn-c", "rn-d")),
        active_before=["rn-b", "rn-c", "rn-d"],
        active_after=["rn-b", "rn-c"],
    )


def test_national_cycles_list_a_cycle_when_a_zero_coverage_network_is_deactivated() -> None:
    """The permissive half of #2087's deactivation delta (design D2), pinned.

    `rn-d` is active with no display-ready row at T1 and gone by T2. Reading the
    coverage rows FIRST makes it invisible to the comparison: `covered == {rn-b,
    rn-c}` equals the T2 active set, so the cycle is LISTED. That is the right
    answer -- a deactivated network does not need rendering -- but it is a
    behaviour DELTA the swap introduced, not a pre-existing property: with the
    active set read first, `active@T1 == {rn-b, rn-c, rn-d}` was compared against
    the same `covered`, and every cycle failed closed.

    Mutation killed: restoring the pre-#2087 statement order in
    `_national_discharge_coverage_rows` (invariant matrix row 40d). This is the
    only case asserting the swap relaxes a MEMBERSHIP-change outcome -- the four
    activation cases all assert a newly CLOSED outcome, so a partial revert that
    kept the growth closures while restoring the old deactivation strictness
    would pass every one of them and fail only here. It is NOT the file's only
    more-permissive assertion:
    `test_national_cycles_still_list_a_cycle_whose_covered_run_stopped_being_display_ready`
    is listed under the swap where "the old order caught this one" too, but its
    active set is identical at both reads -- only a row's display-readiness moves
    -- so no membership-based revert can reach it.
    """
    session = _zero_coverage_deactivation_session()

    result = national_discharge_cycles(session, source="gfs")

    # Non-vacuity: three networks were really active at T1, one really left, and
    # the departing one really brought no coverage row (otherwise this is the
    # fail-closed case below wearing the wrong name).
    assert len(session.active_before) == 3
    assert "rn-d" not in session.active_after
    assert all(row["river_network_version_id"] != "rn-d" for row in session.rows)
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    assert [entry["cycle_time"] for entry in result["cycles"]] == ["2026-09-02T12:00:00Z"]
    assert result["default_cycle"] == "2026-09-02T12:00:00Z"


def test_national_cycles_close_when_a_covered_network_is_deactivated() -> None:
    """The fail-closed half of the same delta -- and it holds in BOTH statement orders.

    `rn-d` is in the T1 covered set (it has a display-ready run) and out of the T2
    active set, so `covered` is a strict SUPERSET of `active` and the cycle is
    refused. Under the old order the deactivation instead shrank the coverage read
    and `covered` was a strict SUBSET, refused as well. D2 distinguishes this case
    from the zero-coverage one precisely because only one of the two moved; this
    test is what keeps the unmoved one unmoved.

    Mutation killed: relaxing the comparison in `national_discharge_cycles` from
    equality to "covers at least the active set"
    (`if not covered_networks >= active_networks: continue`). Every other case in
    this file that reaches `national_discharge_cycles` has `covered` a subset of
    `active` (equality included) or incomparable with it, and on all three shapes
    `>=` and `==` return the same verdict; this is the only one where `covered`
    is a strict SUPERSET, the one shape where they differ, so that mutation is
    green everywhere except here. (The #2459 superset cases added to this file
    judge `NationalCycleCoverage.complete`, never `national_discharge_cycles`.)
    """
    session = _covered_deactivation_session()

    result = national_discharge_cycles(session, source="gfs")

    # Non-vacuity: the departing network really did hold a display-ready row --
    # the one thing that separates this case from the test above.
    assert len(session.active_before) == 3
    assert "rn-d" not in session.active_after
    assert any(row["river_network_version_id"] == "rn-d" for row in session.rows)
    assert session.served_coverage_rows and all(session.served_coverage_rows)
    assert result["cycles"] == []
    assert result["default_cycle"] is None


class _CoverageRowVanishesBetweenStatementsSession(_NationalDiscoverySession):
    """A covered run stops being display-ready BETWEEN the helper's two reads.

    The numerator-SHRINK race: the active-network SET never moves, only a row's
    eligibility for the coverage statement does -- `h.status` leaving
    `('succeeded', 'parsed', 'published')`, or `rdc.segment_count` going to zero.
    `_ActivationBetweenStatementsSession` cannot express it: its only lever is the
    active set, and `_NationalDiscoverySession.rows` is fixed at construction.

    Order-INDEPENDENT on the same terms as its sibling: the write lands after the
    FIRST `execute()`, whichever statement that turns out to be, so the case is
    green under the #2087 order and red under the old one rather than encoding
    either. A subclass, never an edit to the base.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        vanishing_network: str,
        active_networks: list[str],
    ) -> None:
        super().__init__(rows, active_networks=active_networks)
        self.vanishing_network = vanishing_network
        self.served_coverage_rows: list[list[dict[str, Any]]] = []

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        # Evaluated BEFORE `super().execute`, which is what appends to
        # `executions`: read 1 still sees the run as display-ready, every later
        # read does not.
        still_display_ready = not self.executions
        result = super().execute(statement, params)
        if "hydro.run_display_coverage" not in str(statement):
            return result
        served = [
            row
            for row in result.all()
            if still_display_ready or row["river_network_version_id"] != self.vanishing_network
        ]
        self.served_coverage_rows.append(served)
        return _Rows(served)


def _coverage_shrink_session() -> _CoverageRowVanishesBetweenStatementsSession:
    return _CoverageRowVanishesBetweenStatementsSession(
        _full_coverage_rows(_CYCLE, networks=("rn-b", "rn-c")),
        vanishing_network="rn-c",
        active_networks=["rn-b", "rn-c"],
    )


def test_national_cycles_still_list_a_cycle_whose_covered_run_stopped_being_display_ready() -> None:
    """CHARACTERIZATION of the residual #2087 newly opens. NOT the desired answer.

    `rn-c` is display-ready at T1 and not at T2 while the active set stays
    `{rn-b, rn-c}`, so `covered == active` and the cycle is listed although the
    run painting `rn-c` is gone. The old order caught this one; no TWO-statement
    design can close it and the growth class at once, and #2457 RULED to keep
    the two statements and accept it (criteria and reopen triggers: design D1 of
    the OpenSpec change `national-discharge-intersection-closure`). This test
    exists so the residual is VISIBLE to the suite -- if a later change closes
    it, this case goes red and must be rewritten deliberately rather than silently.

    Reachable in production, each writer read in the tree:
      * `mark_run_failed` (`workers/output_parser/parser.py`) -- its
        `FAILABLE_RUN_STATUSES` guard includes `succeeded` and `parsed`, and
        `mark_run_parsed` admits an already-`parsed` run, so a failed re-parse
        drops a run that HAS a populated coverage row. The routine writer.
      * `scripts/node27_refresh_coverage.py --force` -- zeroes `segment_count`
        past the #1446 refusal (`WHERE %(force)s OR ...`), which the coverage
        statement's `rdc.segment_count > 0` join then drops. Operator-gated.
      * `mark_failed` (`workers/shud_runtime/runtime.py`) -- unguarded UPDATE, but
        behind `create_run`'s retriability refusal, so it needs a duplicate
        concurrent `execute`.

    Mutation killed: restoring the pre-#2087 statement order (matrix row 40d) --
    which is also the empirical proof that the old order really did catch this
    class, the claim the helper's docstring makes.
    """
    session = _coverage_shrink_session()

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == ["2026-09-02T12:00:00Z"]
    assert result["default_cycle"] == "2026-09-02T12:00:00Z"
    # Non-vacuity, and the whole point: the shrink is real and the DENOMINATOR
    # never moved. A second pass over the same session -- now past the write --
    # sees `rn-c` gone from the covered set while the active set still holds it.
    after = mvt_module.national_discharge_cycle_coverage(session, source="gfs", cycle=_CYCLE)
    assert after.covered_networks == frozenset({"rn-b"})
    assert after.active_networks == frozenset({"rn-b", "rn-c"})
    assert after.complete is False
