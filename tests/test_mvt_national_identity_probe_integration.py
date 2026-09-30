"""424 semantics oracle for the ``hydro-national`` identity-existence probe (#1596).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_mvt_national_identity_probe_integration.py \
        tests/test_mvt_national_identity_probe_cycles_integration.py \
        tests/test_mvt_national_identity_probe_digest_integration.py

#2490 split the oracle into those three partitions (pure move); the shared seeds,
the ``national_tile`` fixture and the assertions live in
``tests/mvt_national_identity_probe_integration_helpers.py``.

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Why this file exists
--------------------

#1596 reshapes ``source_identity_stats`` into a per-identity ``CROSS JOIN
LATERAL`` probe so the compressed chunks stop being decompressed whole. The
acceptance standard is that the probe's 0/1 answer is unchanged, and the whole
repo had ZERO tests on the branch it drives: ``grep
MVT_LIVE_POSTGIS_UNAVAILABLE tests/`` matched nothing before this file. So the
cheap-looking alternative — answer existence from ``hydro.run_display_coverage``
alone and never touch the fact table — had no oracle that could reject it.

It must be rejected, and the middle case below is what rejects it: the coverage
window is a MIN/MAX over *complete* instants (packages/common/display_coverage.py
:439-456), not a per-instant bitmap. An instant inside the window with no rows
at all — an interior gap — is 424 today; a coverage-only probe would answer 1
and serve an empty 200 tile instead.

Two traps this file is written around (design D4):

* **The false green.** ``_require_live_postgis_mvt`` (hydro_display.py:490-498)
  and the probe's zero branch (:543-549) raise the SAME 424 with the SAME code,
  differing only in ``details``. ``set_integration_env`` does not set
  ``NHMS_ENABLE_LIVE_POSTGIS_MVT``, so without the explicit ``setenv`` below all
  three cases would pass against any probe whatsoever. Every 424 assertion
  therefore checks that ``details`` carries the tile coordinates and NOT
  ``required_env``.
* **Coverage that never materializes.** A coverage window only exists when the
  run is ``run_type='forecast'`` with a ``met.forcing_version`` row (the window
  is a GREATEST/LEAST against the forcing window — a missing row NULLs it away)
  and when the endpoint instants are *complete*: ``segment_count =
  expected_segment_count``, taken here from ``rnv.segment_count`` because the
  model instance declares no ``resource_profile`` override. The seed writes
  every segment at both endpoints for exactly that reason, and the interior-gap
  case asserts the materialized window before it asserts the 424.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from sqlalchemy.orm import Session

from services.tiles.mvt import (
    MVT_MEDIA_TYPE,
    national_discharge_source_version,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)
from tests.integration_helpers import (
    sqlalchemy_engine,
)
from tests.mvt_national_identity_probe_integration_helpers import (
    _CYCLE_TIME,
    _GAP_TIME,
    _IFS_CYCLE_TIME,
    _IFS_RUN_ID,
    _IFS_SOURCE_ID,
    _IFS_WINDOW_END,
    _LATE_CYCLE_TIME,
    _LATE_GFS_FORCING_VERSION_ID,
    _LATE_GFS_RUN_ID,
    _NETWORK_ID,
    _PRUNED_CYCLE_TIME,
    _RUN_ID,
    _SAME_CYCLE_IFS_FORCING_VERSION_ID,
    _SAME_CYCLE_IFS_RUN_ID,
    _SEGMENT_IDS,
    _SOURCE_ID,
    _UNLANDED_CYCLE_TIME,
    _WINDOW_END,
    _WINDOW_START,
    _assert_both_runs_are_candidates_at,
    _assert_coverage_segment_counts,
    _assert_probe_said_no_data,
    _assert_tile_carries_the_seeded_features,
    _assert_tile_was_painted_by,
    _query,
    _refresh_coverage,
    _request_identity_tile,
    _request_tile,
    _seed_rival_display_ready_run,
    _seed_uppercase_ifs_run,
)
from tests.mvt_national_identity_probe_integration_helpers import (
    national_tile as national_tile,
)

pytestmark = pytest.mark.integration


def test_national_tile_is_424_when_no_display_ready_run_covers_the_instant(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The zero branch with no coverage row at all.

    The fact rows for this instant exist — the run is simply not display-ready,
    so the probe's discovery sub-select is empty and never touches the fact
    table. This is the branch that made an uncovered compressed instant cost 38
    seconds before #1596 and now costs nothing at all.
    """
    database_url, client = national_tile

    assert _query(database_url, "SELECT run_id FROM hydro.run_display_coverage", ()) == []
    rows_at_instant = _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE valid_time = %s",
        (_WINDOW_START,),
    )
    assert rows_at_instant[0]["n"] == len(_SEGMENT_IDS), "seed must have rows here, or the case proves nothing"
    post_expand_forecast_database({_RUN_ID: "narrow"})

    _assert_probe_said_no_data(_request_tile(client, _WINDOW_START))


def test_national_tile_is_424_on_an_interior_coverage_window_gap(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The case that rejects answering existence from the coverage window.

    ``river_valid_time_start/end`` are a MIN/MAX over complete instants, so this
    instant is inside the advertised window while holding no rows at all. A
    coverage-only probe answers 1 here and serves an empty 200 tile; the
    fact-touching probe answers 0 and keeps the 424 the pre-change shape
    produced.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    coverage = _query(
        database_url,
        """
        SELECT segment_count, river_valid_time_start, river_valid_time_end
        FROM hydro.run_display_coverage WHERE run_id = %s
        """,
        (_RUN_ID,),
    )
    assert len(coverage) == 1
    assert coverage[0]["segment_count"] == len(_SEGMENT_IDS)
    assert coverage[0]["river_valid_time_start"] == _WINDOW_START
    assert coverage[0]["river_valid_time_end"] == _WINDOW_END
    # Non-vacuity, both halves: the instant is inside the window AND the fact
    # table really is empty there.
    assert coverage[0]["river_valid_time_start"] < _GAP_TIME < coverage[0]["river_valid_time_end"]
    gap_rows = _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE valid_time = %s",
        (_GAP_TIME,),
    )
    assert gap_rows[0]["n"] == 0
    post_expand_forecast_database({_RUN_ID: "narrow"})

    _assert_probe_said_no_data(_request_tile(client, _GAP_TIME))


def test_national_tile_is_200_with_a_non_empty_mvt_when_the_instant_has_data(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The one branch of the probe: covered window, rows at that exact instant."""
    database_url, client = national_tile
    _refresh_coverage(database_url)
    _assert_coverage_segment_counts(database_url, {_RUN_ID: len(_SEGMENT_IDS)})
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE run_id = %s AND valid_time = %s",
        (_RUN_ID, _WINDOW_END),
    ) == [{"n": len(_SEGMENT_IDS)}]
    post_expand_forecast_database({_RUN_ID: "narrow"})

    response = _request_tile(client, _WINDOW_END)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(MVT_MEDIA_TYPE)
    assert response.content, "a 200 with empty bytes would mean the probe answered 1 for nothing"
    # MVT string values are stored as plain UTF-8 in the protobuf, so the seeded
    # identity is visible in the bytes without decoding the tile.
    for segment_id in _SEGMENT_IDS:
        assert segment_id.encode() in response.content, segment_id
    assert _NETWORK_ID.encode() in response.content


def test_national_identity_tile_is_424_for_the_source_without_a_run_at_that_cycle(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """#2007's fail-closed case, and the only oracle that can see a half-bound query.

    Binding `(source, cycle)` in the `latest_runs` data CTE alone leaves the
    identity probe answering from ANY source's run: `source_identity_count`
    stays 1, the data CTE selects nothing, and the route serves an empty 200
    where the contract requires 424. Everything else in the repo passes under
    that bug, because a fake session never runs the SQL.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE cycle_time = %s ORDER BY source_id",
        (_CYCLE_TIME,),
    ) == [{"source_id": _SOURCE_ID}], "only gfs may hold a run at this cycle, or the case proves nothing"
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE run_id = %s AND valid_time = %s",
        (_RUN_ID, _WINDOW_END),
    ) == [{"n": len(_SEGMENT_IDS)}]
    post_expand_forecast_database({_RUN_ID: "narrow"})

    _assert_tile_carries_the_seeded_features(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    )
    _assert_probe_said_no_data(_request_identity_tile(client, "ifs", _CYCLE_TIME, _WINDOW_END))


def test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Path segment `ifs` must find a run stored as `source_id = 'IFS'`.

    Production stores exactly `gfs` and `IFS`, so an equality match instead of
    `lower(h.source_id) = :source` would 424 every IFS tile in the fleet, and
    no other test in the repo would notice.
    """
    database_url, client = national_tile
    _seed_uppercase_ifs_run(database_url)
    _refresh_coverage(database_url, _IFS_RUN_ID)

    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE run_id = %s",
        (_IFS_RUN_ID,),
    ) == [{"source_id": "IFS"}], "the stored spelling must be upper-case, or the case proves nothing"
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE run_id = %s AND valid_time = %s",
        (_IFS_RUN_ID, _IFS_WINDOW_END),
    ) == [{"n": len(_SEGMENT_IDS)}]
    post_expand_forecast_database({_IFS_RUN_ID: "narrow", _RUN_ID: "narrow"})

    _assert_tile_carries_the_seeded_features(
        _request_identity_tile(client, "ifs", _IFS_CYCLE_TIME, _IFS_WINDOW_END)
    )
    # The gfs identity has no run at the IFS cycle, so it stays fail-closed.
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _IFS_CYCLE_TIME, _IFS_WINDOW_END))

    # The legacy source-less alias, at the SAME instant, is the only place in
    # the repo where its `source=None` bind is observable: this test refreshes
    # coverage for the IFS run ONLY, so `_IFS_WINDOW_END` is an instant no gfs
    # run can serve. Everywhere else — every other case here and every unit
    # case — the seed is `_SOURCE_ID = "gfs"`, so a legacy route that started
    # binding `source="gfs"` instead of NULL would answer identically and stay
    # green. Here it 424s.
    assert _query(
        database_url,
        "SELECT run_id FROM hydro.run_display_coverage ORDER BY run_id",
        (),
    ) == [{"run_id": _IFS_RUN_ID}], "only the IFS run may be display-ready here, or the case proves nothing"
    legacy = _request_tile(client, _IFS_WINDOW_END)
    _assert_tile_carries_the_seeded_features(legacy)
    # `_assert_tile_was_painted_by` cannot be used for this pair: `_RUN_ID`
    # (`it1596_forecast_run`) is a PREFIX of `_IFS_RUN_ID`
    # (`it1596_forecast_run_ifs`), and that helper rejects such a pair outright
    # because its negative half would be unfalsifiable. The positive assertion
    # on the longer id is strictly stronger than the pair would have been.
    assert _IFS_RUN_ID.encode() in legacy.content, _IFS_RUN_ID


def test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Same source, two seeded cycles plus a third with no run: each answer is its own.

    Run selection is `DISTINCT ON (river_network_version_id) ... ORDER BY
    h.cycle_time DESC`, so with the `:cycle` predicate deleted BOTH requests
    below would be painted by the late run -- a request for an old cycle served
    with the newest cycle's discharge, silently, at HTTP 200. Making an old
    identity addressable is the entire point of the issue, so this is its
    behavioral oracle.

    The legacy source-less route is asserted alongside precisely to show that
    newest-wins IS the unbound default: it still picks the late run, and only
    the bound cycle overrides it.

    The third request — `_PRUNED_CYCLE_TIME`, which has no run at all — is the
    only behavioral oracle on the `:cycle` half of the identity PROBE. The two
    painted-by cases above run entirely inside the 200 branch, so they cannot
    tell a probe that filters on `:cycle` from one where the predicate is
    present but ineffective; every other identity case in this file either
    leaves `:source` bound to a source with no run, or asks for a cycle that
    does have one. Delete or neuter `:cycle` in
    `source_identity_stats_sql` only and the probe answers "present" from the
    late gfs run, which turns this contract's 424 into an empty 200.
    """
    database_url, client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_LATE_GFS_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_LATE_GFS_FORCING_VERSION_ID,
        cycle_time=_LATE_CYCLE_TIME,
        window_start=_LATE_CYCLE_TIME,
        window_end=_WINDOW_END,
        value_base=300.0,
    )
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _LATE_GFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _LATE_GFS_RUN_ID), _WINDOW_END)
    same_source_runs = _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE source_id = %s ORDER BY cycle_time",
        (_SOURCE_ID,),
    )
    assert same_source_runs == [{"run_id": _RUN_ID}, {"run_id": _LATE_GFS_RUN_ID}], (
        "both rivals must be the SAME source, or :cycle is not what is under test"
    )
    post_expand_forecast_database({_RUN_ID: "narrow", _LATE_GFS_RUN_ID: "narrow"})

    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END), _RUN_ID, _LATE_GFS_RUN_ID
    )
    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _LATE_CYCLE_TIME, _WINDOW_END), _LATE_GFS_RUN_ID, _RUN_ID
    )
    # A gfs cycle that was never seeded, asked for at an instant BOTH seeded
    # runs cover (`_assert_both_runs_are_candidates_at` above established that
    # half; the mutation only bites because it holds). Fail-closed 424, not the
    # newest run's tile and not an empty 200.
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.hydro_run WHERE cycle_time = %s",
        (_PRUNED_CYCLE_TIME,),
    )[0]["n"] == 0, "the pruned cycle must have no run at all, or the case proves nothing"
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _PRUNED_CYCLE_TIME, _WINDOW_END))
    # The same fail-closed demand from the OTHER side of the seeded cycles. The
    # pruned case above is older than every run, so `h.cycle_time <= :cycle`
    # selects nothing there either and answers 424 exactly like `=` does; only a
    # cycle NEWER than the newest run separates the two. Under `<=` this request
    # is painted by the late run, i.e. a not-yet-issued cycle silently served
    # with an older cycle's discharge at HTTP 200.
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.hydro_run WHERE cycle_time >= %s",
        (_UNLANDED_CYCLE_TIME,),
    )[0]["n"] == 0, "the unlanded cycle must be newer than every seeded run, or the case proves nothing"
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _UNLANDED_CYCLE_TIME, _WINDOW_END))
    # Unbound default, unchanged: newest cycle wins.
    _assert_tile_was_painted_by(_request_tile(client, _WINDOW_END), _LATE_GFS_RUN_ID, _RUN_ID)


def test_national_digest_narrows_the_ranked_runs_to_the_bound_identity(national_tile: Any) -> None:
    """`national_discharge_source_version`'s narrowing, EXECUTED, which nothing ever did.

    The third `(source, cycle)` site lives in this helper's ranked sub-query and
    it was the only one with no behavioral oracle anywhere: `_CapturingSession`
    in `tests/hydro_display_mvt_helpers.py` (#2074 moved it there with the suite
    partition) records binds and returns
    canned rows without running SQL, so a predicate that is present but
    ineffective — the `AND (` -> `OR  (` flip, which SQL precedence turns into
    "every unbound row, OR the matching ones" — kept the whole suite green.
    `grep -rn "AND (CAST(:source" tests/` matched nothing before this case.

    Two seeded gfs cycles on one network, so the ranking has something to
    choose between:

    * bound to the EARLY cycle -> ranks run_a,
    * bound to the LATE cycle -> ranks the late run,
    * bound to nothing -> ranks each network's overall latest, i.e. the late run
      again, which is the legacy/catalog question and must not move,
    * bound to a cycle with no run at all -> ranks nothing.

    Under the flip all four collapse onto the unbound value. Freshness is what
    that costs: the digest reaches `source_version` and therefore `cache_key`,
    so a re-run of a non-latest identity would stop rotating its cache entry
    while the tile it names went stale, and the tile file cache has no TTL.
    """
    database_url, _client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_LATE_GFS_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_LATE_GFS_FORCING_VERSION_ID,
        cycle_time=_LATE_CYCLE_TIME,
        window_start=_LATE_CYCLE_TIME,
        window_end=_WINDOW_END,
        value_base=300.0,
    )
    # The digest's ranked sub-query INNER JOINs `hydro.run_display_coverage`, so
    # a run without a refreshed coverage row is not a candidate at all and the
    # comparisons below would be vacuous.
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _LATE_GFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _LATE_GFS_RUN_ID), _WINDOW_END)

    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            unbound = national_discharge_source_version(session)
            early = national_discharge_source_version(session, source="gfs", cycle=_CYCLE_TIME)
            late = national_discharge_source_version(session, source="gfs", cycle=_LATE_CYCLE_TIME)
            pruned = national_discharge_source_version(session, source="gfs", cycle=_PRUNED_CYCLE_TIME)
    finally:
        # The throwaway database is DROPped on teardown; a live pooled
        # connection would make the DROP block and take the file down with it.
        engine.dispose()

    assert early != late, "the digest does not narrow: both cycles rank the same run"
    assert unbound == late, "the unbound digest must stay the overall-latest question"
    assert pruned not in (unbound, early, late), "a cycle with no run must digest an empty ranking"
    # Non-vacuity for `unbound == late`: it is an equality, so it would also
    # hold if the helper returned a constant. `early` differing from it is what
    # rules that out, and `pruned` differing from all three rules out a digest
    # that only ever sees two shapes.
    assert len({unbound, early, pruned}) == 3


def test_national_identity_tile_serves_the_requested_source_not_the_other_one_at_that_cycle(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Same cycle, two sources: neither request may be painted by the other source's run.

    This is the mutation that survives everything else in the repo -- deleting
    the identity pair from the `latest_runs` DATA CTE alone, leaving the probe
    and the digest bound. The probe still answers 1, so the route still returns
    200, and the CTE paints the other source's discharge.

    Both runs share `cycle_time`, so the unbound tie-break is `ORDER BY h.run_id
    DESC`: `it2007_ifs_same_cycle_run` sorts above `it1596_forecast_run`, which
    makes the `gfs` request the direction the mutation actually bites. The `ifs`
    direction is asserted for symmetry -- it is the one that would break if the
    predicate were inverted or the case-folding dropped.
    """
    database_url, client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_SAME_CYCLE_IFS_RUN_ID,
        source_id=_IFS_SOURCE_ID,
        forcing_version_id=_SAME_CYCLE_IFS_FORCING_VERSION_ID,
        cycle_time=_CYCLE_TIME,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        value_base=400.0,
        new_data_source_name="IFS 2007 same cycle",
    )
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _SAME_CYCLE_IFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _SAME_CYCLE_IFS_RUN_ID), _WINDOW_END)
    assert _query(
        database_url,
        "SELECT run_id, source_id FROM hydro.hydro_run WHERE cycle_time = %s ORDER BY run_id",
        (_CYCLE_TIME,),
    ) == [
        {"run_id": _RUN_ID, "source_id": _SOURCE_ID},
        {"run_id": _SAME_CYCLE_IFS_RUN_ID, "source_id": _IFS_SOURCE_ID},
    ], "both rivals must share the cycle, or :source is not what is under test"
    post_expand_forecast_database({_RUN_ID: "narrow", _SAME_CYCLE_IFS_RUN_ID: "narrow"})

    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END), _RUN_ID, _SAME_CYCLE_IFS_RUN_ID
    )
    _assert_tile_was_painted_by(
        _request_identity_tile(client, "ifs", _CYCLE_TIME, _WINDOW_END), _SAME_CYCLE_IFS_RUN_ID, _RUN_ID
    )
