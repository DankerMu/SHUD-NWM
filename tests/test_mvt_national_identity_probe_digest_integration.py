"""National digest binding, geometry-backfill rotation and full-coverage identity tiles.

Partition of ``tests/test_mvt_national_identity_probe_integration.py`` (#2490,
pure move); the opt-in and the throwaway-database contract are the base file's.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor
from sqlalchemy.orm import Session

from apps.api.routes import hydro_display
from services.tiles.mvt import (
    MVT_MEDIA_TYPE,
    display_ready_run,
    national_discharge_source_version,
    national_river_network_source_version,
)
from tests.integration_helpers import (
    insert_river_timeseries_dual_written,
    sqlalchemy_engine,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)
from tests.mvt_national_identity_probe_integration_helpers import (
    _BASIN_VERSION_ID,
    _CYCLE_TIME,
    _LATE_CYCLE_TIME,
    _LAYER_ID,
    _NETWORK_ID,
    _PREFIX,
    _RUN_ID,
    _SECOND_BASIN_VERSION_ID,
    _SECOND_NETWORK_ID,
    _SEGMENT_IDS,
    _SEGMENT_LAT,
    _SEGMENT_LON,
    _SOURCE_ID,
    _VARIABLE,
    _WINDOW_END,
    _WINDOW_START,
    _ZOOM,
    _assert_both_runs_are_candidates_at,
    _assert_coverage_segment_counts,
    _assert_tile_carries_the_seeded_features,
    _assert_tile_was_painted_by,
    _query,
    _refresh_coverage,
    _request_identity_tile,
    _seed_rival_display_ready_run,
    _seed_second_network,
    _seed_second_network_run,
    _segment_geom_sql,
    _stamp,
    _tile_xy,
)
from tests.mvt_national_identity_probe_integration_helpers import (
    national_tile as national_tile,
)
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# #2031: the national cache identity must describe the data the tile reads.
# Appended at the end of the file so every line citation above keeps its number.
#
# Two failures, one invariant:
#
# * A. The digest ranked each network's latest run for the bound `(source,
#   cycle)` WITHOUT the coverage-window clamp `latest_runs` applies, so two
#   instants served by two different runs shared one cache key. Measured on
#   node-27 (`docs/runbooks/receipts/2026-09-08-issue-2031-digest-precondition.md`):
#   reachable on 38/38 active networks for the legacy route, and 20 same-cycle
#   double-run groups on the new one.
# * B. `_backfill_output_segment_geometry` rewrites `core.river_segment.geom`
#   and the STORED `stream_type` UNDER an unchanged network version. No run row
#   moves and `rnv.segment_count`/`checksum` describe the imported package, so
#   both national digests were blind to it. `geometry_generation` (000057) is
#   the missing signal.
#
# Neither is visible to a fake session: A is a SQL predicate that only a real
# planner applies, and B needs a real PostGIS UPDATE plus the generated
# `stream_type` column. Hence both live here.
# ---------------------------------------------------------------------------

# Lexically GREATER than `_RUN_ID` (`it1596_forecast_run`), and neither a
# substring of it nor containing it, so `_assert_tile_was_painted_by` stays
# falsifiable. Greater is the direction that matters: the ranking's tie-break at
# an equal `cycle_time` is `ORDER BY h.run_id DESC`, so this run wins rank 1
# wherever it is a candidate at all -- which is exactly what the window clamp
# has to take away at an instant it does not cover.
_RIVAL_RUN_ID = "it2031_gfs_same_cycle_rival_run"
_RIVAL_FORCING_VERSION_ID = "it2031_forcing_gfs_same_cycle_v1"
# The rival's window ENDS an hour before the base run's does. Same cycle, same
# source, same network: the window is the ONLY thing separating them.
_RIVAL_WINDOW_END = _LATE_CYCLE_TIME

_OUTPUT_SEGMENT_ID = f"{_PREFIX}_shud_riv_000001"
_REACH_SEGMENT_ID = f"{_PREFIX}_reach_000001"
_REACH_INDEX = 1


def _national_digests(database_url: str) -> dict[str, str]:
    """Both national digests, read through a connection of their own.

    A fresh engine per call rather than one long-lived session: these values are
    compared ACROSS a commit made by a different connection, and a session left
    holding an open transaction would be comparing snapshots instead of states.
    The engine is disposed because the throwaway database is DROPped on
    teardown and a live pooled connection makes that DROP block.
    """
    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            return {
                "discharge": national_discharge_source_version(session),
                "river_network": national_river_network_source_version(session),
            }
    finally:
        engine.dispose()


def _bound_digest(database_url: str, *, valid_time: datetime | None) -> str:
    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            return national_discharge_source_version(
                session, source="gfs", cycle=_CYCLE_TIME, valid_time=valid_time
            )
    finally:
        engine.dispose()


def _coverage_window(database_url: str, run_id: str) -> tuple[datetime, datetime]:
    rows = _query(
        database_url,
        """
        SELECT river_valid_time_start, river_valid_time_end
        FROM hydro.run_display_coverage WHERE run_id = %s
        """,
        (run_id,),
    )
    assert len(rows) == 1, f"{run_id} has no coverage row, so it is not a digest candidate at all"
    return rows[0]["river_valid_time_start"], rows[0]["river_valid_time_end"]


def _geometry_generation(database_url: str, river_network_version_id: str) -> int:
    rows = _query(
        database_url,
        "SELECT geometry_generation FROM core.river_network_version WHERE river_network_version_id = %s",
        (river_network_version_id,),
    )
    assert len(rows) == 1, river_network_version_id
    return int(rows[0]["geometry_generation"])


def _clear_tile_cache(database_url: str) -> int:
    """Drop the DB tile tier so the next request REGENERATES instead of replaying.

    Deliberately not routed through `_query`: that helper opens a connection
    without autocommit and never commits, so a DELETE through it is rolled back
    on close and silently does nothing -- the exact failure mode that would turn
    the tile-side oracle below back into a cache replay.

    Only the DB tier needs clearing: the fixture never sets
    `NHMS_MVT_FILE_CACHE_DIR`, so the file tier is off.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM map.tile_cache")
            return int(cursor.rowcount)
    finally:
        connection.close()


def test_national_digest_binds_the_instant_so_a_rival_outside_the_window_moves_nothing(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2031 A: the digest ranks the run `latest_runs` paints, per instant.

    Two display-ready gfs runs at the SAME cycle on one network. The rival has
    the greater `run_id`, so the unbound tie-break gives it rank 1 everywhere --
    but its coverage window stops an hour short of `_WINDOW_END`, so the tile at
    `_WINDOW_END` is still painted by the base run. Before the clamp the digest
    said otherwise: the cache key for `_WINDOW_END` rotated onto a run that
    instant is never served from, and — the failure that actually bites — the
    key for `_WINDOW_END` and the key for an instant the rival DOES serve moved
    in lockstep, so one of the two was always describing the wrong run.

    Four assertions, four different questions:

    * bound to `_WINDOW_END` -> UNCHANGED by the rival's arrival (the clamp
      excludes it). This is the one that fails without the fix.
    * bound to `_RIVAL_WINDOW_END`, which BOTH runs cover -> MOVED (the rival is
      genuinely the rank-1 run there). Non-vacuity for the first: a digest that
      simply ignored the rival everywhere would satisfy it.
    * unbound -> MOVED, because the instant-less question is still "each
      network's overall latest run" and that is now the rival.
    * the TILE at `_WINDOW_END` is still painted by the base run -- the tile
      half of the same clamp, read out of `latest_runs` rather than out of the
      digest.

    That fourth question only exists because `_clear_tile_cache` runs first.
    With the digest unchanged the cache key is unchanged too, so without the
    clear the second response is a cache hit by construction and replays the
    baseline bytes: `_assert_tile_was_painted_by` would then re-assert the
    BASELINE tile and say nothing at all about how `latest_runs` ranks the two
    runs now that the rival exists. Clearing the DB tile tier forces a
    regeneration -- proved, not assumed, by `X-Tile-Cache: miss` -- so the run
    identity in those bytes is the one the tile SQL just selected.

    The byte equality is the weaker half and now asserts something different
    from before: that regenerating the tile reproduces the baseline bytes.
    `_assert_tile_was_painted_by` remains the load-bearing tile-side assertion.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    baseline_at_window_end = _bound_digest(database_url, valid_time=_WINDOW_END)
    baseline_at_rival_window_end = _bound_digest(database_url, valid_time=_RIVAL_WINDOW_END)
    baseline_unbound = _national_digests(database_url)["discharge"]
    # Non-vacuity: the baselines really do observe one ranked run. An empty
    # basis (no coverage refresh, say) would make every comparison below a
    # comparison of two empty digests.
    assert baseline_at_window_end.endswith(":1"), baseline_at_window_end
    assert baseline_unbound.endswith(":1"), baseline_unbound
    # Baseline taken through the production statement. It used to be taken
    # through a frozen pre-store snapshot of the national tile SQL; #1342's
    # contract (task 6.3) deleted that snapshot together with the routing it
    # existed to be compared against (fixture deletion D2), and the baseline was
    # never about the snapshot — it is "what this request returned BEFORE the
    # rival run was seeded", which the live statement answers directly.
    baseline_tile = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    _assert_tile_was_painted_by(baseline_tile, _RUN_ID, _RIVAL_RUN_ID)

    _seed_rival_display_ready_run(
        database_url,
        run_id=_RIVAL_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_RIVAL_FORCING_VERSION_ID,
        cycle_time=_CYCLE_TIME,
        window_start=_WINDOW_START,
        window_end=_RIVAL_WINDOW_END,
        value_base=500.0,
    )
    _refresh_coverage(database_url, _RIVAL_RUN_ID)

    # Non-vacuity for the whole case, in three parts.
    #
    # (1) Both runs are real display-ready candidates. Asserted at
    # `_WINDOW_START`, which both windows contain and where both runs hold a
    # full segment set -- `_assert_both_runs_are_candidates_at` also demands
    # fact rows at the instant, and neither `_WINDOW_END` (the rival has none)
    # nor `_RIVAL_WINDOW_END` (the base seed leaves that hour empty on purpose,
    # it is `_GAP_TIME`) satisfies that for both runs.
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _RIVAL_RUN_ID), _WINDOW_START)
    # (2) The windows really are what the case claims: only the base run covers
    # `_WINDOW_END`, and BOTH cover `_RIVAL_WINDOW_END`. This is the entire
    # mechanism, materialized by the production coverage refresh rather than
    # asserted from the seed arguments.
    base_start, base_end = _coverage_window(database_url, _RUN_ID)
    rival_start, rival_end = _coverage_window(database_url, _RIVAL_RUN_ID)
    assert base_start <= _WINDOW_END <= base_end
    assert rival_end < _WINDOW_END, "the rival must NOT cover _WINDOW_END, or the case proves nothing"
    assert base_start <= _RIVAL_WINDOW_END <= base_end
    assert rival_start <= _RIVAL_WINDOW_END <= rival_end
    # (3) The rival really would win the ranking wherever it is a candidate:
    # same cycle, greater run_id under the database's own collation.
    assert _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE source_id = %s AND cycle_time = %s ORDER BY run_id DESC",
        (_SOURCE_ID, _CYCLE_TIME),
    ) == [{"run_id": _RIVAL_RUN_ID}, {"run_id": _RUN_ID}], (
        "the rival must sort above the base run at the same cycle, or the clamp has nothing to undo"
    )

    assert _bound_digest(database_url, valid_time=_WINDOW_END) == baseline_at_window_end, (
        "a run whose coverage window excludes the requested instant must not enter the digest: "
        "the tile at that instant is still painted by the base run"
    )
    assert _bound_digest(database_url, valid_time=_RIVAL_WINDOW_END) != baseline_at_rival_window_end, (
        "at an instant BOTH runs cover, the rival IS the run the tile paints and the key must rotate"
    )
    assert _national_digests(database_url)["discharge"] != baseline_unbound, (
        "the instant-less question is unchanged: each network's overall latest run, now the rival"
    )

    # The tile half of the same clamp. Non-vacuity: the baseline request must
    # really have populated the DB tier, or there is nothing to clear and the
    # "miss" below would be describing a cache that was never warm.
    assert _clear_tile_cache(database_url) >= 1, (
        "the baseline tile request must have written the DB tile tier, "
        "or clearing it proves nothing about the request that follows"
    )
    post_expand_forecast_database({_RUN_ID: "narrow", _RIVAL_RUN_ID: "narrow"})
    after_tile = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    assert after_tile.headers["X-Tile-Cache"] == "miss", (
        "this tile must be REGENERATED, not replayed: on a cache hit the run identity "
        "below is the baseline's and asserts nothing about how latest_runs ranks the rival"
    )
    _assert_tile_was_painted_by(after_tile, _RUN_ID, _RIVAL_RUN_ID)
    assert after_tile.content == baseline_tile.content


def _seed_output_and_reach_rows(database_url: str) -> None:
    """The minimal pair `_backfill_output_segment_geometry` needs, and nothing else.

    It reads two sets out of ``core.river_segment`` for one network:

    * output rows -- ``shud_output_river='true'`` with a numeric
      ``shud_riv_index`` -- seeded here with NULL geom and no ``Type``, which is
      the state ``_ensure_output_river_segments`` leaves them in;
    * reach rows -- geom NOT NULL, NOT ``shud_output_river``, numeric ``iRiv`` --
      the gis/river.shp source it copies geom/``length_m``/``Type`` from.

    The base seed's `_SEGMENT_IDS` rows are neither (no ``iRiv``, no
    ``shud_output_river``), so they cannot accidentally act as a source or a
    target, and the network's `segment_count` is deliberately left at 2 so the
    coverage windows already materialized do not move.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO core.river_segment
                    (river_segment_id, river_network_version_id, segment_order,
                     length_m, geom, properties_json)
                VALUES (%s, %s, %s, %s, {_segment_geom_sql(5)}, %s::jsonb)
                """,
                (
                    _REACH_SEGMENT_ID,
                    _NETWORK_ID,
                    10,
                    1234.5,
                    f'{{"iRiv": {_REACH_INDEX}, "Type": 4}}',
                ),
            )
            cursor.execute(
                """
                INSERT INTO core.river_segment
                    (river_segment_id, river_network_version_id, segment_order,
                     geom, properties_json)
                VALUES (%s, %s, %s, NULL, %s::jsonb)
                """,
                (
                    _OUTPUT_SEGMENT_ID,
                    _NETWORK_ID,
                    11,
                    f'{{"shud_output_river": "true", "shud_riv_index": "{_REACH_INDEX}"}}',
                ),
            )
    finally:
        connection.close()


def test_geometry_backfill_rotates_both_national_digests_exactly_once(national_tile: Any) -> None:
    """#2031 B: an in-place geometry rewrite must move both national cache keys.

    `_backfill_output_segment_geometry` is the only `UPDATE core.river_segment`
    in production code (pinned by
    `tests/test_river_segment_write_surface_scan.py`). It moves `geom`, `length_m`
    and the source `Type` -- and therefore the STORED `stream_type` both national
    tile queries filter on -- onto rows that already exist, under a network
    version whose `segment_count` and `checksum` do not move because they
    describe the imported package. Before `geometry_generation` both digests were
    byte-identical across that rewrite, so every cached national tile kept
    serving pre-backfill geometry with no TTL to save it.

    The second half is the guard that makes the first affordable: every
    bootstrap tick runs the same backfill with `only_missing=True` over
    already-complete networks. That pass must update nothing, bump nothing, and
    leave both digests byte-identical -- otherwise the fix would rotate all 38
    networks' keys on every tick.

    Ordering note: coverage is refreshed BEFORE the baselines are taken. The
    discharge digest INNER JOINs `hydro.run_display_coverage`, so without it the
    basis is empty and pre/post would match for the wrong reason -- hence the
    `:1` suffix assertion (the digest's trailing field is the basis row count).
    """
    database_url, _client = national_tile
    _refresh_coverage(database_url)
    _seed_output_and_reach_rows(database_url)

    assert _geometry_generation(database_url, _NETWORK_ID) == 0, (
        "migration 000057 must default every pre-existing network to 0"
    )
    before = _national_digests(database_url)
    assert before["discharge"].endswith(":1"), before["discharge"]
    assert before["river_network"].endswith(":1"), before["river_network"]

    # A real cursor, and NOT autocommit: the bump has to land in the same
    # transaction as the geometry it describes, and a rollback must drop both.
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            updated = _backfill_output_segment_geometry(cursor, _NETWORK_ID)
            # Visible inside the transaction, before anyone else can see it.
            cursor.execute(
                "SELECT geometry_generation FROM core.river_network_version "
                "WHERE river_network_version_id = %s",
                (_NETWORK_ID,),
            )
            in_transaction_generation = int(cursor.fetchone()["geometry_generation"])
        connection.commit()
    finally:
        connection.close()

    assert updated == 1, "the backfill must have rewritten the seeded output row"
    assert in_transaction_generation == 1
    # The rewrite really happened, and it carried the source `Type` through to
    # the generated column the national queries filter on.
    rewritten = _query(
        database_url,
        """
        SELECT geom IS NOT NULL AS has_geom, stream_type, length_m
        FROM core.river_segment
        WHERE river_segment_id = %s AND river_network_version_id = %s
        """,
        (_OUTPUT_SEGMENT_ID, _NETWORK_ID),
    )
    assert rewritten[0]["has_geom"] is True
    assert rewritten[0]["stream_type"] == 4.0
    assert rewritten[0]["length_m"] == 1234.5

    assert _geometry_generation(database_url, _NETWORK_ID) == 1
    after = _national_digests(database_url)
    assert after["discharge"] != before["discharge"], (
        "the discharge digest must rotate when the geometry its tile paints is rewritten"
    )
    assert after["river_network"] != before["river_network"], (
        "the river-network digest paints the same geometry and must rotate with it"
    )

    # Second pass on the now-complete network: the shape every bootstrap tick
    # runs. Nothing to update, so nothing may rotate.
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            second_pass = _backfill_output_segment_geometry(cursor, _NETWORK_ID, only_missing=True)
        connection.commit()
    finally:
        connection.close()

    assert second_pass == 0
    assert _geometry_generation(database_url, _NETWORK_ID) == 1, (
        "a zero-row backfill must not bump: every tick would otherwise rotate every national key"
    )
    unchanged = _national_digests(database_url)
    assert unchanged == after


_MIXED_RUN_ID = "it2206_second_network_run"


def _seed_mixed_national_networks(database_url: str) -> None:
    _seed_second_network(database_url)
    _seed_second_network_run(
        database_url, run_id=_MIXED_RUN_ID, cycle_time=_CYCLE_TIME,
        window_start=_WINDOW_START, window_end=_WINDOW_END,
    )
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _MIXED_RUN_ID)
    _assert_coverage_segment_counts(database_url, {_RUN_ID: 2, _MIXED_RUN_ID: 2})
    # Type is the authority for the generated stream_type column. Both
    # networks must have two untyped segments, making PERCENT_RANK non-vacuous.
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core.river_segment SET properties_json = properties_json - 'Type' "
                "WHERE river_network_version_id IN (%s, %s)",
                (_NETWORK_ID, _SECOND_NETWORK_ID),
            )
            assert cursor.rowcount == 4
        connection.commit()
    finally:
        connection.close()
    assert _query(
        database_url,
        "SELECT river_network_version_id, COUNT(*) AS n FROM core.river_segment "
        "WHERE river_network_version_id IN (%s, %s) AND stream_type IS NULL "
        "GROUP BY river_network_version_id ORDER BY river_network_version_id",
        (_NETWORK_ID, _SECOND_NETWORK_ID),
    ) == [
        {"river_network_version_id": network, "n": 2}
        for network in sorted((_NETWORK_ID, _SECOND_NETWORK_ID))
    ]


# ---------------------------------------------------------------------------
# #2153: the canonical route refuses a `(source, cycle)` identity only SOME
# active networks cover, using the per-cycle valid-times coverage helper, and a
# fully covered identity keeps its bytes. These prove the helper's SQL and the
# tile SQL agree on real rows, which no fake session can.
# ---------------------------------------------------------------------------

_E11_EVIDENCE_FILE = "e11c-full-coverage-tile.txt"


def _emit_full_coverage_tile_evidence(line: str) -> None:
    """Print (visible under `-s`) and, when `NHMS_EVIDENCE_DIR` is set, append to a file.

    The PR head and the pre-#2153 tree are compared on these two fields.
    """
    print(line)
    evidence_dir = os.getenv("NHMS_EVIDENCE_DIR")
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        with Path(evidence_dir, _E11_EVIDENCE_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def test_national_identity_tile_is_refused_when_one_of_two_active_networks_has_no_run(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """E11(a): network 1 covers `(gfs, A)`, active network 2 has no run -> 424 incomplete, 1/2.

    Before #2153 this was a 200 painting network 1 alone.
    """
    database_url, client = national_tile
    _seed_second_network(database_url)
    _refresh_coverage(database_url)
    _assert_coverage_segment_counts(database_url, {_RUN_ID: len(_SEGMENT_IDS)})
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 2}]
    assert _query(
        database_url,
        "SELECT count(*) AS n FROM hydro.hydro_run WHERE basin_version_id = %s",
        (_SECOND_BASIN_VERSION_ID,),
    ) == [{"n": 0}]
    post_expand_forecast_database({_RUN_ID: "narrow"})

    response = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)

    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "MVT_NATIONAL_IDENTITY_INCOMPLETE"
    assert error["details"] == {
        "layer_id": _LAYER_ID,
        "source": "gfs",
        "cycle": _stamp(_CYCLE_TIME),
        "covered_network_count": 1,
        "active_network_count": 2,
    }


def test_national_identity_tile_ignores_an_inactive_network_without_a_run(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """E11(b): the denominator is ACTIVE networks only, so an inactive one refuses nothing."""
    database_url, client = national_tile
    _seed_second_network(database_url, active=False)
    _refresh_coverage(database_url)
    _assert_coverage_segment_counts(database_url, {_RUN_ID: len(_SEGMENT_IDS)})
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 1}]
    post_expand_forecast_database({_RUN_ID: "narrow"})

    _assert_tile_carries_the_seeded_features(_request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END))


def test_national_identity_tile_serves_a_fully_covered_identity(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """E11(c): both active networks cover `(gfs, A)` -> 200 painting both, bytes recorded for comparison."""
    database_url, client = national_tile
    _seed_mixed_national_networks(database_url)
    post_expand_forecast_database({_RUN_ID: "narrow", _MIXED_RUN_ID: "narrow"})

    response = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(MVT_MEDIA_TYPE)
    assert _NETWORK_ID.encode() in response.content
    assert _SECOND_NETWORK_ID.encode() in response.content
    _emit_full_coverage_tile_evidence(
        f"E11(c) gfs {_stamp(_CYCLE_TIME)} {_stamp(_WINDOW_END)} z={_ZOOM} "
        f"sha256={hashlib.sha256(response.content).hexdigest()} etag={response.headers['etag']}"
    )


# ---------------------------------------------------------------------------
# #2156: the per-basin `river-network` tile and the run-scoped `hydro` tile read
# the same `core.river_segment` geometry the national layers do, but their
# digests (`_river_network_source_version`, `_run_source_version`) could not see
# `_backfill_output_segment_geometry` rewriting it in place: the cache key stayed
# put and every cached tile kept serving the pre-backfill geometry forever (the
# per-basin layer has no run identity to rotate it at all). Appended at the end
# so every line citation above keeps its number.
#
# Real routes, real PostGIS, real backfill: the fake-session and sqlite tests
# cannot show that a key rotation actually reaches the tile bytes.
# ---------------------------------------------------------------------------

_OUTPUT_SEGMENT_VALUE = 300.0


def _seed_output_segment_fact(database_url: str) -> None:
    """One `q_down` fact for the output row at `_WINDOW_END`, on the base run.

    The run-scoped `hydro` tile paints `river_timeseries x river_segment`, so
    without a fact on the backfilled row its bytes could not change whatever the
    geometry did. Seeded BEFORE `post_expand_forecast_database`, like `_seed`'s
    facts, so the run's authoritative store receives a copy instead of a
    poisoned decoy.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        _RUN_ID,
                        _BASIN_VERSION_ID,
                        _NETWORK_ID,
                        _OUTPUT_SEGMENT_ID,
                        _WINDOW_END,
                        1,
                        _VARIABLE,
                        _OUTPUT_SEGMENT_VALUE,
                        "m3/s",
                        "ok",
                    )
                ],
            )
    finally:
        connection.close()


def _run_backfill(database_url: str, *, only_missing: bool) -> int:
    """One committed backfill transaction, the shape a bootstrap tick runs."""
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            updated = _backfill_output_segment_geometry(cursor, _NETWORK_ID, only_missing=only_missing)
        connection.commit()
    finally:
        connection.close()
    return updated


def _request_per_basin_and_run_tiles(client: TestClient) -> dict[str, Any]:
    x, y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, _ZOOM)
    return {
        "river-network": client.get(f"/api/v1/tiles/river-network/{_BASIN_VERSION_ID}/{_ZOOM}/{x}/{y}.pbf"),
        "hydro": client.get(
            f"/api/v1/tiles/hydro/{_RUN_ID}/{_VARIABLE}/{_stamp(_WINDOW_END)}/{_ZOOM}/{x}/{y}.pbf"
        ),
    }


def _catalog_and_tile_run_versions(database_url: str) -> tuple[str, str, Any]:
    """`_run_source_version` of the same run read by both run readers."""
    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            catalog_row = display_ready_run(session)
            tile_row = hydro_display._run_row(session, _RUN_ID)
    finally:
        engine.dispose()
    assert catalog_row is not None and catalog_row["run_id"] == _RUN_ID
    return (
        hydro_display._run_source_version(catalog_row),
        hydro_display._run_source_version(tile_row),
        tile_row.get("geometry_generation"),
    )


def test_geometry_backfill_rotates_the_per_basin_and_run_scoped_tiles(
    national_tile: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """#2156: a backfill that rewrites rows moves both tiles' keys AND bytes; a no-op moves neither.

    Before the backfill the output row has NULL geometry, so neither tile can
    carry it. The backfill copies the reach geometry and `Type` onto it and bumps
    `geometry_generation`; both routes must then compute a new
    `X-Tile-Cache-Key`, miss the cache, and regenerate bytes that DO carry the
    row. Without the counter in their digests the key is unchanged, the second
    request is a cache hit, and it replays the pre-backfill bytes -- the stale
    tile this issue is about.

    The `only_missing=True` pass that follows is what every bootstrap tick runs
    on a complete network: it updates nothing, so both keys and both byte
    strings must stay exactly as they were (a cache hit).
    """
    database_url, client = national_tile
    _seed_output_and_reach_rows(database_url)
    _seed_output_segment_fact(database_url)
    post_expand_forecast_database({_RUN_ID: "narrow"})
    assert _geometry_generation(database_url, _NETWORK_ID) == 0

    before = _request_per_basin_and_run_tiles(client)
    for layer, response in before.items():
        assert response.status_code == 200, (layer, response.text)
        assert response.headers["content-type"].startswith(MVT_MEDIA_TYPE), layer
        assert response.headers["X-Tile-Cache"] == "miss", layer
        # Non-vacuity: the tiles really are painted, just not with the NULL-geom row.
        assert _SEGMENT_IDS[0].encode() in response.content, layer
        assert _OUTPUT_SEGMENT_ID.encode() not in response.content, layer
    assert _REACH_SEGMENT_ID.encode() in before["river-network"].content

    assert _run_backfill(database_url, only_missing=True) == 1, "the backfill must rewrite the seeded output row"
    assert _geometry_generation(database_url, _NETWORK_ID) == 1

    after = _request_per_basin_and_run_tiles(client)
    for layer, response in after.items():
        assert response.status_code == 200, (layer, response.text)
    rotated = {
        layer: after[layer].headers["X-Tile-Cache-Key"] != before[layer].headers["X-Tile-Cache-Key"]
        for layer in before
    }
    assert rotated == {"river-network": True, "hydro": True}, (
        "an in-place geometry rewrite must rotate the per-basin and run-scoped tile cache keys"
    )
    for layer, response in after.items():
        assert response.headers["X-Tile-Cache"] == "miss", layer
        assert response.content != before[layer].content, layer
        assert _OUTPUT_SEGMENT_ID.encode() in response.content, (
            f"{layer}: the regenerated tile must carry the backfilled row's new geometry"
        )

    # The catalog (`display_ready_run`) and the tile route (`_run_row`) read the
    # same generation and so advertise the same run identity.
    catalog_version, tile_version, generation = _catalog_and_tile_run_versions(database_url)
    assert generation == 1
    assert catalog_version == tile_version

    assert _run_backfill(database_url, only_missing=True) == 0
    assert _geometry_generation(database_url, _NETWORK_ID) == 1, "a zero-row backfill must not bump"

    unchanged = _request_per_basin_and_run_tiles(client)
    for layer, response in unchanged.items():
        assert response.status_code == 200, (layer, response.text)
        assert response.headers["X-Tile-Cache-Key"] == after[layer].headers["X-Tile-Cache-Key"], layer
        assert response.headers["X-Tile-Cache"] == "hit", layer
        assert response.content == after[layer].content, layer
