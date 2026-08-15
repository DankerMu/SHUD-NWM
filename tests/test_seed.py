import random
import re
from pathlib import Path
from typing import Any

import pytest

from db.seeds import seed_demo


def test_seed_module_importable() -> None:
    from db.seeds.seed_demo import main

    assert callable(main)


def test_seed_sql_strings_contain_expected_tables() -> None:
    source = Path(seed_demo.__file__).read_text(encoding="utf-8")
    expected_tables = {
        "core.basin",
        "core.basin_version",
        "core.river_network_version",
        "core.river_segment",
        "core.mesh_version",
        "core.model_instance",
        "met.data_source",
        "met.forecast_cycle",
        "met.met_station",
        "met.forcing_version",
        "met.forcing_station_timeseries",
        "hydro.hydro_run",
        "hydro.river_timeseries",
        "map.tile_layer",
        "ops.pipeline_job",
        "ops.qc_result",
    }

    missing_tables = sorted(table for table in expected_tables if table not in source)

    assert missing_tables == []


def test_seed_active_model_sets_lifecycle_state_active() -> None:
    source = Path(seed_demo.__file__).read_text(encoding="utf-8")

    assert "lifecycle_state" in source
    assert "'active'" in source


def test_seed_ids_follow_convention() -> None:
    assert seed_demo.BASIN_ID == "yangtze"
    assert re.fullmatch(r"yangtze_v\d{4}_\d{2}", seed_demo.BASIN_VERSION_ID)
    assert re.fullmatch(r"yangtze_rivnet_v\d{2}", seed_demo.RIVER_NETWORK_VERSION_ID)
    assert re.fullmatch(r"yangtze_shud_v\d{2}", seed_demo.MODEL_ID)
    assert re.fullmatch(r"yangtze_mesh_v\d{2}", seed_demo.MESH_VERSION_ID)
    assert seed_demo.SOURCE_ID == "gfs"
    assert seed_demo.SOURCE_ID_IN_IDS == "gfs"
    assert re.fullmatch(r"forc_gfs_\d{10}_yangtze_shud_v\d{2}", seed_demo.FORCING_VERSION_ID)
    assert re.fullmatch(r"fcst_gfs_\d{10}_yangtze_shud_v\d{2}", seed_demo.RUN_ID)
    assert re.fullmatch(r"gfs_\d{10}", seed_demo.CYCLE_ID)

    segment_ids = [segment.river_segment_id for segment in seed_demo.build_river_segments()]
    assert segment_ids == [f"{seed_demo.RIVER_NETWORK_VERSION_ID}_riv_{index:04d}" for index in range(1, 16)]
    assert all(5000 <= segment.length_m <= 50000 for segment in seed_demo.build_river_segments())

    station_ids = [station.station_id for station in seed_demo.build_met_stations()]
    assert station_ids == [f"{seed_demo.BASIN_VERSION_ID}_stn_{index:04d}" for index in range(1, 6)]


# ---------------------------------------------------------------------------
# river_timeseries surrogate-key dual write (#1340)
# ---------------------------------------------------------------------------

_RIVER_INSERT_COLUMNS = [
    "run_id",
    "basin_version_id",
    "river_network_version_id",
    "river_segment_id",
    "valid_time",
    "lead_time_hours",
    "variable",
    "value",
    "unit",
    "quality_flag",
    "run_key",
    "river_network_version_key",
    "basin_version_key",
    "river_segment_key",
    "variable_e",
    "unit_e",
    "quality_flag_e",
]


class _KeyCursor:
    """Fake cursor answering the seed's authority-key SELECTs."""

    def __init__(self, *, segment_keys: dict[str, int] | None = None, run_keys: dict[str, int] | None = None) -> None:
        segments = [segment.river_segment_id for segment in seed_demo.build_river_segments()]
        self.segment_keys = (
            segment_keys
            if segment_keys is not None
            else {segment_id: 500 + index for index, segment_id in enumerate(segments)}
        )
        self.run_keys = (
            run_keys
            if run_keys is not None
            else {
                seed_demo.RUN_ID: 11,
                seed_demo.IFS_RUN_ID: 12,
                seed_demo.IFS_06Z_RUN_ID: 13,
            }
        )
        self.statements: list[str] = []
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, statement: str, parameters: Any = ()) -> None:
        del parameters
        self.statements.append(" ".join(statement.split()))
        normalized = self.statements[-1].lower()
        if "from hydro.hydro_run" in normalized:
            self._rows = sorted(self.run_keys.items())
        elif "from core.basin_version" in normalized:
            self._rows = [(21,)]
        elif "from core.river_network_version" in normalized:
            self._rows = [(31,)]
        elif "from core.river_segment" in normalized:
            self._rows = sorted(self.segment_keys.items())
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


def _seed_hydro(cursor: _KeyCursor) -> list[tuple[str, list[tuple[Any, ...]]]]:
    calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def _execute_values(_cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        calls.append((statement, list(rows)))

    seed_demo.seed_hydro(cursor, _execute_values, random.Random(42))
    return calls


def test_seed_river_timeseries_insert_dual_writes_all_seven_normalized_columns() -> None:
    cursor = _KeyCursor()
    calls = _seed_hydro(cursor)

    statement, rows = next(call for call in calls if "hydro.river_timeseries" in call[0])
    match = re.search(r"INSERT INTO hydro\.river_timeseries \((.*?)\)\s*VALUES", statement, re.S)
    assert match is not None
    assert [column.strip() for column in match.group(1).split(",") if column.strip()] == _RIVER_INSERT_COLUMNS
    assert "ON CONFLICT DO NOTHING" in statement

    assert rows
    for row in rows:
        assert len(row) == 17
        field = dict(zip(_RIVER_INSERT_COLUMNS, row, strict=True))
        assert field["run_key"] == cursor.run_keys[field["run_id"]]
        assert field["basin_version_key"] == 21
        assert field["river_network_version_key"] == 31
        assert field["river_segment_key"] == cursor.segment_keys[field["river_segment_id"]]
        # Enum columns carry the row's own text value (not a parallel literal).
        assert field["variable_e"] == field["variable"]
        assert field["unit_e"] == field["unit"]
        assert field["quality_flag_e"] == field["quality_flag"]

    # Seed is the only writer producing the y_stage / 'm' branch: it must be
    # dual-written too.
    variants = {(row[6], row[8], row[9]) for row in rows}
    assert variants == {("q_down", "m3/s", "ok"), ("y_stage", "m", "ok")}
    # Same triples read off the enum positions, by value: a swapped or stale
    # literal in the enum tail shows up here even when it equals some other
    # row's text value.
    enum_variants = {(row[14], row[15], row[16]) for row in rows}
    assert enum_variants == variants


def test_seed_reads_authority_keys_with_select_not_returning() -> None:
    """The authority inserts are all ``ON CONFLICT DO NOTHING``; on a re-seed
    ``RETURNING`` yields nothing while the rows exist, so keys must be read
    back with an explicit SELECT."""
    cursor = _KeyCursor()
    _seed_hydro(cursor)

    key_selects = [statement for statement in cursor.statements if "_key" in statement]
    assert len(key_selects) == 4
    for statement in key_selects:
        assert statement.lower().startswith("select")
    assert not any("returning" in statement.lower() for statement in cursor.statements)


def test_seed_fails_loudly_when_an_authority_key_is_missing() -> None:
    segments = [segment.river_segment_id for segment in seed_demo.build_river_segments()]
    cursor = _KeyCursor(segment_keys={segment_id: 500 for segment_id in segments[:-1]})

    with pytest.raises(RuntimeError) as exc_info:
        _seed_hydro(cursor)

    assert segments[-1] in str(exc_info.value)
    assert "000050" in str(exc_info.value)
