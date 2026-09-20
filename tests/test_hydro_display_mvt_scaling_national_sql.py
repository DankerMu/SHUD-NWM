"""#2007: the hydro-national ``{source}``/``{cycle}`` identity, SQL-text half.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074). Every landmark
slice below keeps the non-empty and ordering preconditions it was written with —
``_national_sql_sites`` asserts ``cte_start < cte_end < probe_start < probe_end``
and ``_national_digest_ranked_subquery`` asserts the slice is really the
sub-query — because a slice that degrades to comparing empty strings passes
vacuously.
"""

from __future__ import annotations

from sqlalchemy import text

from services.tiles.mvt import (
    NATIONAL_DISCHARGE_QUERY_VERSION,
    national_discharge_source_version,
    national_river_network_source_version,
    postgis_tile_sql,
)
from tests.hydro_display_mvt_helpers import (
    _CYCLE_CONJUNCT,
    _NATIONAL_CYCLE,
    _NATIONAL_VALID_TIME,
    _SOURCE_CONJUNCT,
    _CapturingSession,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _NationalRouteSession,
    _Session,
)

# --- #2007: the hydro-national {source}/{cycle} identity -------------------
#
# `postgis_tile_sql` keeps its single-argument signature, so the identity
# travels as the named binds `:source` / `:cycle`. Two independent run
# selections consume them and they must agree, because one produces the tile's
# rows and the other produces the 0/1 the route turns into 200 vs 424.


def _national_sql_sites() -> tuple[str, str]:
    """The two run-selection slices of the national tile SQL, located not counted.

    A `count(...) >= 2` would pass with both occurrences inside the data CTE,
    which is exactly the bug design D1 warns about: the identity probe would
    then still answer "present" from another source's run and the route would
    serve an empty 200 where the contract requires 424. So each site is sliced
    out by its own landmark and asserted separately.
    """
    sql = postgis_tile_sql("hydro-national")
    cte_start = sql.index("WITH latest_runs AS MATERIALIZED (")
    cte_end = sql.index("network_stream_max AS MATERIALIZED", cte_start)
    probe_start = sql.index("source_identity_stats AS (")
    probe_end = sql.index("AS source_identity_count", probe_start)
    # Non-vacuity: the slices are disjoint and in this order, so neither
    # assertion below can be satisfied by the other site's text.
    assert cte_start < cte_end < probe_start < probe_end
    return sql[cte_start:cte_end], sql[probe_start:probe_end]


def test_national_tile_sql_binds_source_and_cycle_at_both_run_selection_sites() -> None:
    cte_slice, probe_slice = _national_sql_sites()

    for site_name, site in (("latest_runs CTE", cte_slice), ("identity probe", probe_slice)):
        # The NULL guard is what keeps the legacy 5-segment route's run
        # selection unchanged, so it is part of the locked shape. It also splits
        # the two sub-predicates apart, which is why they are located
        # individually and never as one contiguous string. The leading `AND (`
        # is inside the pinned literal: without it a conjunct -> disjunct flip
        # keeps every substring satisfied while the predicate narrows nothing.
        assert _SOURCE_CONJUNCT in site, site_name
        assert _CYCLE_CONJUNCT in site, site_name
        # `:source::text` would make SQLAlchemy's bind regex backtrack and emit
        # a bogus `sourc` bind that no fake-session test can see.
        assert ":source::" not in site, site_name
        assert ":cycle::" not in site, site_name

    binds = set(text(postgis_tile_sql("hydro-national"))._bindparams)
    assert {"source", "cycle"} <= binds
    assert not binds & {"sourc", "cycl"}


# The coverage-window clamp verbatim, `AND (` included, for the same reason the
# two conjuncts above carry theirs: SQL's AND binds tighter than OR, so an
# `AND (` -> `OR  (` flip turns the whole WHERE into "every unbound row, OR the
# matching ones" and re-admits every candidate run while every inner substring
# stays satisfied.
#
# Deliberately NOT byte-identical to the tile CTE's window predicate: that one
# is an unguarded `JOIN ... ON` (the tile always has an instant), this one has
# to stay inert for the instant-less catalog call. The oracle for the pair is
# behavioural -- same run selected when both are bound --
# `tests/test_mvt_national_identity_probe_integration.py`, not string equality.
_VALID_TIME_CONJUNCT_HEAD = "AND ("
_VALID_TIME_GUARD = "CAST(:valid_time AS timestamptz) IS NULL"
_VALID_TIME_WINDOW_START = "rdc.river_valid_time_start <= :valid_time"
_VALID_TIME_WINDOW_END = "rdc.river_valid_time_end >= :valid_time"


def _national_digest_ranked_subquery(sql: str) -> str:
    """The ranked sub-query alone, sliced out by its own landmarks.

    A whole-statement `in` check would be satisfied by the predicate sitting in
    the OUTER `WHERE rn = 1`, and that placement is a real, silent bug rather
    than a style difference: filtering after `ROW_NUMBER()` drops the rank-1 row
    of a network whose latest run misses the instant and leaves that network
    absent from the digest entirely, instead of letting the next candidate
    become rank 1 -- which is exactly the run the tile paints.
    """
    start = sql.index("FROM (")
    end = sql.index(") ranked", start)
    assert start < end
    # Non-vacuity: the slice really is the sub-query, not the whole statement.
    assert "WHERE rn = 1" not in sql[start:end]
    return sql[start:end]


def test_national_digest_clamps_its_ranking_to_the_requested_instants_coverage_window() -> None:
    """#2031: the digest must rank the run `latest_runs` would paint, not the newest one.

    Measured on node-27 (receipt `2026-09-08-issue-2031-digest-precondition.md`):
    the legacy route's digest and its tile disagreed on the selected run for
    every one of the 38 active networks, and the new route has 20 same-cycle
    double-run groups one differing window away from the same split. When they
    disagree, two instants painted from two different runs share one cache key
    and the file tile cache has no TTL.

    The NULL guard is half the contract: `/api/v1/layers` has no instant, and a
    clamp that is not inert when unbound would empty the catalog's digest.
    """
    session = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))

    national_discharge_source_version(session, valid_time=_NATIONAL_VALID_TIME)

    ranked = _national_digest_ranked_subquery(session.sql)
    # Inside the ranked sub-query, before ROW_NUMBER() decides rank 1.
    assert _VALID_TIME_GUARD in ranked
    assert _VALID_TIME_WINDOW_START in ranked
    assert _VALID_TIME_WINDOW_END in ranked
    # Conjunct, not disjunct: the guard opens an `AND (`-introduced group.
    guard_at = ranked.index(_VALID_TIME_GUARD)
    assert _VALID_TIME_CONJUNCT_HEAD in ranked[:guard_at][-40:], (
        f"the coverage clamp must be a conjunct; preceding text: {ranked[:guard_at][-40:]!r}"
    )
    # `:valid_time::timestamptz` would make SQLAlchemy's bind regex backtrack
    # and emit a bogus `valid_tim` bind that no fake-session test can see.
    assert ":valid_time::" not in session.sql
    binds = set(text(session.sql)._bindparams)
    assert binds == {"source", "cycle", "valid_time"}, binds
    assert not binds & {"sourc", "cycl", "valid_tim"}


def test_national_digest_always_binds_valid_time_even_when_no_instant_is_given() -> None:
    """`text()` raises on a missing named bind, and no fake session can see that.

    The catalog and every other instant-less caller reach the same statement, so
    omitting the parameter instead of binding NULL is a runtime failure on the
    real driver only -- the exact class of break #2007 left in four integration
    cases.
    """
    session = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))

    national_discharge_source_version(session)

    declared = set(text(session.sql)._bindparams)
    supplied = set(session.params[0])
    assert declared - supplied == set(), "the digest omits a bind its own SQL declares"
    assert session.params[0]["valid_time"] is None


def test_national_digests_join_the_network_version_and_project_its_geometry_generation() -> None:
    """#2031's other half: an in-place geometry rewrite must move both keys.

    `_backfill_output_segment_geometry` rewrites `core.river_segment.geom` and
    the STORED `stream_type` UNDER an unchanged network version. No run row
    moves, and `rnv.segment_count` / `rnv.checksum` describe the imported
    package rather than the stored geometry, so before this column both national
    digests were blind to it: the tile repainted and the cache key did not.

    The INNER JOIN drops no candidate -- `core.model_instance.river_network_version_id`
    is `NOT NULL REFERENCES core.river_network_version` (000004_core.sql) -- and it
    aligns the digest's join shape with `latest_runs`.
    """
    discharge = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))
    national_discharge_source_version(discharge, source="gfs", cycle=_NATIONAL_CYCLE)

    ranked = _national_digest_ranked_subquery(discharge.sql)
    assert "JOIN core.river_network_version rnv" in ranked
    assert "ON rnv.river_network_version_id = mi.river_network_version_id" in ranked
    # Both projections: the inner one selects it, the OUTER list is explicit and
    # would silently drop the column from the digest basis if it were forgotten.
    assert "rnv.geometry_generation" in ranked
    outer = discharge.sql[: discharge.sql.index("FROM (")]
    assert "geometry_generation" in outer, f"outer projection drops the column: {outer!r}"

    river = _Session(
        [
            {
                "river_network_version_id": "rnv_a",
                "basin_version_id": "bv_a",
                "segment_count": 10,
                "checksum": "abc",
                "geometry_generation": 0,
                "created_at": "2026-07-20T00:00:00Z",
            }
        ]
    )
    national_river_network_source_version(river)
    assert "rnv.geometry_generation" in river.sql
    # The sibling layer's version literal is NOT bumped by this projection
    # change: the basis moves on its own, which is the whole point of D7.
    assert "stream-type-aggregate-v3" in national_river_network_source_version(river)


def test_a_geometry_generation_bump_moves_both_national_digests() -> None:
    """A returned `geometry_generation` reaches the digest basis.

    Fake rows, so this proves the row-consuming half only: `_national_source_digest`
    hashes whatever the query returns, with no column whitelist and no row
    mapping that drops unknown keys -- either of which would keep every SQL-text
    assertion green while the key stood still through a geometry rewrite. Same
    fake rows, one incremented counter, two different digests -- on BOTH layers,
    because the backfill repaints both.

    That the real SQL actually PROJECTS the column is a different claim, pinned
    elsewhere: the SQL-text assertions in this file, and 5.2 on node-27 against
    a live database.
    """
    discharge_rows = [{**_NationalRouteSession._DIGEST_ROWS[0], "geometry_generation": 0}]
    bumped_discharge_rows = [{**discharge_rows[0], "geometry_generation": 1}]
    assert national_discharge_source_version(_Session(discharge_rows)) != (
        national_discharge_source_version(_Session(bumped_discharge_rows))
    )

    river_rows = [
        {
            "river_network_version_id": "rnv_a",
            "basin_version_id": "bv_a",
            "segment_count": 10,
            "checksum": "abc",
            "geometry_generation": 0,
            "created_at": "2026-07-20T00:00:00Z",
        }
    ]
    bumped_river_rows = [{**river_rows[0], "geometry_generation": 1}]
    assert national_river_network_source_version(_Session(river_rows)) != (
        national_river_network_source_version(_Session(bumped_river_rows))
    )
    # Non-vacuity: the same rows twice really do digest identically, so the
    # inequalities above are attributable to the counter and not to nondeterminism.
    assert national_discharge_source_version(_Session(discharge_rows)) == (
        national_discharge_source_version(_Session(list(discharge_rows)))
    )
    assert national_river_network_source_version(_Session(river_rows)) == (
        national_river_network_source_version(_Session(list(river_rows)))
    )


def test_national_discharge_query_version_is_pinned_to_the_literal_the_spec_names() -> None:
    """A literal, not the imported constant.

    Every other assertion on this constant imports it from the module under
    test and interpolates it, so reverting the value would keep them all green
    while silently un-rotating the cache key that a tile-shape change requires.
    The spec names `fair-network-budget-v6`; this is the only place the repo
    says so.

    Deliberately re-pinned from v5 to v6 by #2165 (design D4, a contract
    change, not a loosening): the layer's own `NATIONAL_DISCHARGE_FEATURE_LIMIT`
    binds `:feature_limit` at 20,000 instead of 10,000, which changes the bytes
    of every national discharge tile that intersects more than 10,000 features.
    """
    assert NATIONAL_DISCHARGE_QUERY_VERSION == "fair-network-budget-v6"


def test_sibling_tile_layers_carry_no_source_or_cycle_bind() -> None:
    for layer in ("hydro", "river-network-national", "river-network", "met-stations"):
        layer_sql = postgis_tile_sql(layer)
        assert ":source" not in layer_sql, layer
        assert ":cycle" not in layer_sql, layer


def test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one() -> None:
    """The digest feeds `source_version`, so it must answer the identity's question.

    Ranking each network's OVERALL latest run would leave the digest — and the
    cache key — unchanged when a non-latest `(source, cycle)` is re-run, and
    this issue is what makes such an identity addressable.
    """
    rows = [
        {
            "run_id": "run_a",
            "river_network_version_id": "rnv_a",
            "cycle_time": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T13:00:00Z",
        }
    ]
    unbound = _CapturingSession(rows)
    bound = _CapturingSession(rows)

    national_discharge_source_version(unbound)
    national_discharge_source_version(bound, source="gfs", cycle=_NATIONAL_CYCLE)

    assert unbound.params == [{"source": None, "cycle": None, "valid_time": None}]
    assert bound.params == [{"source": "gfs", "cycle": _NATIONAL_CYCLE, "valid_time": None}]
    # Same locked literal as the two `postgis_tile_sql` sites, `AND (` included:
    # this helper's narrowing has no fake-session oracle at all (`_Session` never
    # executes SQL), so the shape assertion is the only local guard and a
    # conjunct -> disjunct flip must not pass it. The BEHAVIORAL oracle is
    # `tests/test_mvt_national_identity_probe_integration.py`
    # ::test_national_digest_narrows_the_ranked_runs_to_the_bound_identity.
    assert _SOURCE_CONJUNCT in bound.sql
    assert _CYCLE_CONJUNCT in bound.sql
    # One shared status set, one occurrence: `test_display_publish_status_only`
    # pins the counts this helper contributes.
    assert bound.sql.count("h.status IN ('succeeded', 'parsed', 'published')") == 1
