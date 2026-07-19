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

    def execute(self, sql: str, _params: object) -> None:
        self.statements.append(sql)
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


def test_national_hydro_mvt_prefers_source_stream_type_with_rank_fallback() -> None:
    sql = postgis_tile_sql("hydro-national")

    assert "AS stream_type" in sql
    assert "stream_type IS NOT NULL" in sql
    assert "WHEN :z = 5 THEN 4.0" in sql
    assert "stream_type IS NULL" in sql
    assert "value_percent_rank >= CASE" in sql
    assert HYDRO_NATIONAL_SOURCE_VERSION.endswith("stream-type-v2")
