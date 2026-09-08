from __future__ import annotations

import json
from typing import Any

import psycopg2.extras

from apps.api.routes.hydro_display import HYDRO_NATIONAL_SOURCE_VERSION
from services.tiles.mvt import postgis_tile_sql
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry


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
    assert "NOT properties_json ? 'Type'" in cursor.statements[0]
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
    assert [sql for sql in cursor.statements if "UPDATE" in sql] == [], cursor.statements


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
    assert [sql for sql in cursor.statements if "UPDATE" in sql] == [], cursor.statements


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
