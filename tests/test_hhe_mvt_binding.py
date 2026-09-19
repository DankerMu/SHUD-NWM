from __future__ import annotations

import json
import re
from typing import Any

import psycopg2.extras
import pytest

from apps.api.routes.hydro_display import HYDRO_NATIONAL_SOURCE_VERSION
from services.tiles.mvt import postgis_tile_sql
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry

# #2157 / B-3b: the backfill's first statement is now the parent-row lock
# `SELECT 1 FROM core.river_network_version ... FOR NO KEY UPDATE`. That text
# contains the substring "UPDATE", so the "writes nothing" assertions below
# filter on WRITE statements (`UPDATE core.<table>` / a `SET` clause) instead of
# on the bare substring -- same "no write" intent, and each no-write case also
# pins that the lock is the ONLY non-SELECT-candidate statement it issued.
_PARENT_LOCK = re.compile(
    r"\bSELECT\s+1\s+FROM\s+core\.river_network_version\s+WHERE\s+river_network_version_id\s*=\s*%s\s+"
    r"FOR\s+NO\s+KEY\s+UPDATE\b"
)
_WRITE = re.compile(r"\bUPDATE\s+core\.|\bSET\b")


def _writes(statements: list[str]) -> list[str]:
    return [sql for sql in statements if _WRITE.search(sql)]


def _assert_parent_lock_first(cursor: "_BackfillCursor", river_network_version_id: str) -> None:
    locks = [index for index, (sql, _params) in enumerate(cursor.calls) if _PARENT_LOCK.search(sql)]
    assert locks == [0], f"exactly one parent-row lock, as statement 0, expected; got {cursor.statements}"
    assert cursor.calls[0][1] == (river_network_version_id,), cursor.calls[0]


class _BackfillCursor:
    def __init__(self, *, source_type: float | None, geom_missing: bool) -> None:
        self.source_type = source_type
        self.geom_missing = geom_missing
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []
        # `statements` stays SQL-only (two pre-existing cases read it by index);
        # `calls` is the params-carrying view #2031 needs, and it is where the
        # monkeypatched `execute_values` records the segment UPDATE too, so the
        # ORDER of the two writes is observable at all.
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, _params: object = None) -> None:
        self.statements.append(sql)
        self.calls.append((sql, _params))
        if "FOR NO KEY UPDATE" in sql:
            # #2157 parent-row lock: returns no rows the backfill reads.
            return
        if "SET geometry_generation" in sql:
            # Not a SELECT: leave `rows` alone so a later fetchall() (there is
            # none today) cannot be answered by this statement's absence of rows.
            return
        if "AS geom_missing" in sql:
            self.rows = [
                {
                    "river_segment_id": "basins_hhe_shud_shud_riv_000001",
                    "shud_riv_index": "1",
                    "geom_missing": self.geom_missing,
                }
            ]
        else:
            self.rows = [
                {
                    "shud_riv_index": "1",
                    "geom_wkt": "MULTILINESTRING((100 35,101 36))",
                    "length_m": 1000.0,
                    "stream_type": self.source_type,
                }
            ]

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def test_output_geometry_backfill_copies_source_stream_type(monkeypatch: Any) -> None:
    cursor = _BackfillCursor(source_type=5.0, geom_missing=False)
    captured: dict[str, Any] = {}

    def fake_execute_values(
        _cursor: object,
        _sql: str,
        rows: list[tuple[Any, ...]],
        **_kwargs: object,
    ) -> list[dict[str, str]]:
        captured["rows"] = rows
        return [{"river_segment_id": str(rows[0][0])}]

    monkeypatch.setattr(psycopg2.extras, "execute_values", fake_execute_values)

    assert _backfill_output_segment_geometry(cursor, "basins_hhe_rivnet_vbasins", only_missing=True) == 1
    # #2157 re-pin (D2 contract change, not a loosening): statement 0 is now the
    # parent-row lock, so the candidate SELECT moved to statement 1.
    _assert_parent_lock_first(cursor, "basins_hhe_rivnet_vbasins")
    assert "NOT properties_json ? 'Type'" in cursor.statements[1]
    provenance = json.loads(captured["rows"][0][3])
    assert provenance["Type"] == 5.0
    assert provenance["geometry_source"] == "gis_rivseg_iRiv"


def test_existing_geometry_without_source_stream_type_is_not_rewritten(monkeypatch: Any) -> None:
    cursor = _BackfillCursor(source_type=None, geom_missing=False)

    def unexpected_execute_values(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("existing geometry without source Type must remain untouched")

    monkeypatch.setattr(psycopg2.extras, "execute_values", unexpected_execute_values)

    assert _backfill_output_segment_geometry(cursor, "rnv", only_missing=True) == 0
    # #2031 (c): a complete network under `only_missing=True` is the shape every
    # bootstrap tick runs. It updates nothing, so it must write nothing at all --
    # an unguarded generation bump here would rotate every national tile cache
    # key on every tick for no data change.
    # #2157 re-pin (D2): the tick now takes the parent-row lock first (the
    # `FOR NO KEY UPDATE` text is why the filter is on write shapes, not on the
    # substring "UPDATE"), and that lock is its only non-read statement.
    assert _writes(cursor.statements) == [], cursor.statements
    _assert_parent_lock_first(cursor, "rnv")
    assert [sql for sql in cursor.statements if not sql.lstrip().startswith("SELECT")] == [], cursor.statements


def _recording_execute_values(
    monkeypatch: Any, updated_rows: list[dict[str, str]]
) -> dict[str, Any]:
    """Stand in for the segment UPDATE and record it ON THE CURSOR.

    `execute_values` takes the cursor as an argument rather than going through
    `cursor.execute`, so without this the segment UPDATE is invisible to
    `_BackfillCursor.statements` and "the generation bump came AFTER the
    geometry write" has nothing to be ordered against.
    """
    captured: dict[str, Any] = {}

    def fake_execute_values(
        cursor: Any,
        sql: str,
        rows: list[tuple[Any, ...]],
        **_kwargs: object,
    ) -> list[dict[str, str]]:
        captured["rows"] = rows
        cursor.statements.append(sql)
        cursor.calls.append((sql, rows))
        return updated_rows

    monkeypatch.setattr(psycopg2.extras, "execute_values", fake_execute_values)
    return captured


_GENERATION_BUMP = "SET geometry_generation = geometry_generation + 1"


def test_backfill_that_updates_a_row_bumps_the_network_geometry_generation(monkeypatch: Any) -> None:
    """#2031 (a): the write side of the national cache identity.

    Both national digests are computed from run rows plus the network's
    inventory metadata, and NONE of that moves when the backfill rewrites
    `core.river_segment.geom` / the STORED `stream_type` under an unchanged
    network version -- `segment_count` and `checksum` describe the imported
    package. So without this counter the tile's picture changes while its cache
    key stands still, and the file tile cache has no TTL.

    Order matters as much as presence: the bump must be issued on the SAME
    cursor AFTER the segment UPDATE, so it lives in that transaction and rolls
    back with the geometry it describes.
    """
    cursor = _BackfillCursor(source_type=5.0, geom_missing=True)
    _recording_execute_values(monkeypatch, [{"river_segment_id": "basins_hhe_shud_shud_riv_000001"}])

    assert _backfill_output_segment_geometry(cursor, "basins_hhe_rivnet_vbasins") == 1

    bumps = [(index, sql) for index, (sql, _params) in enumerate(cursor.calls) if _GENERATION_BUMP in sql]
    assert len(bumps) == 1, f"exactly one generation bump expected, got {cursor.statements}"
    bump_index, bump_sql = bumps[0]
    assert "UPDATE core.river_network_version" in bump_sql
    assert "WHERE river_network_version_id = %s" in bump_sql
    # The network it actually rewrote, not a stray literal or the wrong id.
    assert cursor.calls[bump_index][1] == ("basins_hhe_rivnet_vbasins",)
    # After the geometry write, in the same cursor's statement stream.
    segment_updates = [
        index for index, (sql, _params) in enumerate(cursor.calls) if "UPDATE core.river_segment" in sql
    ]
    assert len(segment_updates) == 1, cursor.statements
    assert segment_updates[0] < bump_index, (
        "the generation bump must follow the segment UPDATE it describes"
    )


def test_backfill_whose_candidate_rows_are_all_dropped_does_not_bump(monkeypatch: Any) -> None:
    """#2031 (b): non-empty `updates`, empty `updated_rows`.

    `ST_Length(source.geom) > 0` drops degenerate (coincident-vertex) reaches
    inside the UPDATE, so the batch can be non-empty and still write nothing --
    a state neither early exit covers. Binding the bump to `updates` instead of
    to `updated_rows` would rotate both national cache keys here for a pass that
    changed no geometry at all.
    """
    cursor = _BackfillCursor(source_type=5.0, geom_missing=True)
    captured = _recording_execute_values(monkeypatch, [])

    assert _backfill_output_segment_geometry(cursor, "basins_hhe_rivnet_vbasins") == 0

    # Non-vacuity: the batch really was submitted, so the guard -- not an early
    # exit -- is what suppressed the bump.
    assert captured["rows"], "the case proves nothing unless the UPDATE was attempted"
    assert [sql for sql in cursor.statements if _GENERATION_BUMP in sql] == [], cursor.statements


def test_backfill_early_exit_with_no_output_reaches_issues_no_write(monkeypatch: Any) -> None:
    """#2031: the `not reaches_by_index` early exit writes nothing either.

    The other early exit (`not updates`) is
    `test_existing_geometry_without_source_stream_type_is_not_rewritten`; this is
    the one where the network has no output-river rows at all, which is what a
    non-SHUD or freshly-registered network looks like.
    """

    class _EmptyCursor(_BackfillCursor):
        def execute(self, sql: str, _params: object = None) -> None:
            super().execute(sql, _params)
            self.rows = []

    cursor = _EmptyCursor(source_type=5.0, geom_missing=True)

    def unexpected_execute_values(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a network with no output reaches must not submit an UPDATE batch")

    monkeypatch.setattr(psycopg2.extras, "execute_values", unexpected_execute_values)

    assert _backfill_output_segment_geometry(cursor, "rnv") == 0
    # #2157 re-pin (D2): write-shape filter instead of the bare "UPDATE"
    # substring, which the parent-row lock's `FOR NO KEY UPDATE` now carries.
    assert _writes(cursor.statements) == [], cursor.statements
    _assert_parent_lock_first(cursor, "rnv")
    assert [sql for sql in cursor.statements if not sql.lstrip().startswith("SELECT")] == [], cursor.statements


def test_geometry_complete_tick_only_locks_the_parent_row_and_bumps_nothing(monkeypatch: Any) -> None:
    """#2157 AC3 / B-3: the no-op tick takes the lock and nothing else.

    `only_missing=True` over a geometry-complete network is what every
    autopipeline tick runs. The candidate SELECT's `geom IS NULL OR NOT Type`
    filter returns no row, so the pass must: return 0, issue exactly one
    parent-row `FOR NO KEY UPDATE` as its FIRST statement, submit no UPDATE
    batch, and issue no `geometry_generation` write.
    """

    class _CompleteNetworkCursor(_BackfillCursor):
        def execute(self, sql: str, _params: object = None) -> None:
            super().execute(sql, _params)
            if "AS geom_missing" in sql:
                self.rows = []

    cursor = _CompleteNetworkCursor(source_type=5.0, geom_missing=False)

    def unexpected_execute_values(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a geometry-complete tick must not submit an UPDATE batch")

    monkeypatch.setattr(psycopg2.extras, "execute_values", unexpected_execute_values)

    assert _backfill_output_segment_geometry(cursor, "rnv_complete", only_missing=True) == 0

    _assert_parent_lock_first(cursor, "rnv_complete")
    assert len(cursor.statements) == 2, cursor.statements
    assert "AS geom_missing" in cursor.statements[1]
    assert [sql for sql in cursor.statements if "geometry_generation" in sql] == [], cursor.statements
    assert _writes(cursor.statements) == [], cursor.statements


@pytest.mark.parametrize("only_missing", [True, False])
def test_parent_row_lock_precedes_the_segment_write_and_the_bump(monkeypatch: Any, only_missing: bool) -> None:
    """#2157 B-3: on every path the parent lock comes before the first segment write.

    `only_missing=False` is the seed path (`seed_qhh_output_segments` and the
    bootstrap), which must keep backfilling and bumping unconditionally (AC4);
    `only_missing=True` is the tick path with a fillable row. Both: lock at
    statement 0, then the segment UPDATE, then exactly one bump.
    """
    cursor = _BackfillCursor(source_type=5.0, geom_missing=True)
    _recording_execute_values(monkeypatch, [{"river_segment_id": "basins_hhe_shud_shud_riv_000001"}])

    assert _backfill_output_segment_geometry(cursor, "rnv_fill", only_missing=only_missing) == 1

    _assert_parent_lock_first(cursor, "rnv_fill")
    segment_updates = [
        index for index, (sql, _params) in enumerate(cursor.calls) if "UPDATE core.river_segment" in sql
    ]
    bumps = [index for index, (sql, _params) in enumerate(cursor.calls) if _GENERATION_BUMP in sql]
    assert len(segment_updates) == 1 and len(bumps) == 1, cursor.statements
    assert 0 < segment_updates[0] < bumps[0], cursor.statements


def test_national_hydro_mvt_prefers_source_stream_type_with_rank_fallback() -> None:
    sql = postgis_tile_sql("hydro-national")

    assert "rs.stream_type" in sql
    assert "seg.stream_type IS NOT NULL" in sql
    assert "WHEN :z = 5 THEN 4.0" in sql
    assert "seg.stream_type IS NULL" in sql
    assert "value_percent_rank >= CASE" in sql
    assert "JOIN core.river_segment rs" in sql
    assert sql.index("selected_values AS") < sql.rindex("JOIN core.river_segment rs")
    assert HYDRO_NATIONAL_SOURCE_VERSION.endswith("stream-type-v3")
