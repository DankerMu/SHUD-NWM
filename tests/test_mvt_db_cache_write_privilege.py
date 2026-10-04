"""Issue #2716 -- the MVT DB tile cache asks whether it may write before writing.

Under the production "read-only role + file cache" mode every cold miss used to
send `INSERT INTO map.tile_layer`, which PostgreSQL rejected with one ERROR and
the full statement in its log. The write path now asks PostgreSQL once per
engine (`has_table_privilege`) and, when the role may not write, sends nothing.

Seam: `build_raw_tile_response` / `build_tile_response` with a PostgreSQL-dialect
session double that RECORDS every statement it is sent and scripts only the
probe's answer. The assertions are about what reaches the database, so they hold
whatever the gate is called internally.
"""

from __future__ import annotations

import gc
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import ProgrammingError

import services.tiles.mvt as mvt

_MVT_LOGGER = "services.tiles.mvt"
_CACHE_TABLE_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE)\s+map\.tile_(layer|cache)\b", re.IGNORECASE)
_TABLE_COLUMNS = {
    "tile_cache": ("layer_id", "z", "x", "y", "tile_data", "cache_key", "etag", "checksum"),
    "tile_layer": ("layer_id", "layer_type", "tile_format", "tile_uri_template", "source_version"),
}
_READ_ONLY: list[dict[str, Any]] = [{"writable": False}]
_WRITABLE: list[dict[str, Any]] = [{"writable": True}]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Engine:
    """Weak-referenceable stand-in for the SQLAlchemy engine a session is bound to."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect)


class _RecordingSession:
    """Both cache tables exist with a usable column set; only the probe is scripted.

    `probe` is the row list the privilege probe returns, or an exception it
    raises. An unrecognised statement fails the test rather than being answered.
    """

    def __init__(self, bind: Any, probe: list[dict[str, Any]] | Exception) -> None:
        self._bind = bind
        self._probe = probe
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def get_bind(self) -> Any:
        return self._bind

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "has_table_privilege" in sql:
            if isinstance(self._probe, Exception):
                raise self._probe
            return _Result(self._probe)
        if "information_schema.tables" in sql or "sqlite_master" in sql:
            return _Result([{"exists": 1}])
        if "information_schema.columns" in sql:
            return _Result([{"column_name": name} for name in _TABLE_COLUMNS[params["table_name"]]])
        if "PRAGMA" in sql:
            table = "tile_cache" if "tile_cache" in sql else "tile_layer"
            return _Result([{"name": name} for name in _TABLE_COLUMNS[table]])
        if "FROM map.tile_cache" in sql or _CACHE_TABLE_WRITE.search(sql):
            return _Result([])
        raise AssertionError(f"unexpected statement: {sql}")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def probes(self) -> list[str]:
        return [sql for sql in self.statements if "has_table_privilege" in sql]

    def cache_table_writes(self) -> list[str]:
        return [sql for sql in self.statements if _CACHE_TABLE_WRITE.search(sql)]


def _tile(x: int) -> mvt.TileInput:
    return mvt.TileInput(
        layer_id="river-network",
        source_id="basins_demo_v1",
        source_version="generation-a",
        valid_time=None,
        z=6,
        x=x,
        y=20,
    )


def _build_raw(session: Any, tile: mvt.TileInput) -> mvt.TileResponse:
    return mvt.build_raw_tile_response(session, tile, f"pbf-{tile.x}".encode())


def _build_encoded(session: Any, tile: mvt.TileInput) -> mvt.TileResponse:
    return mvt.build_tile_response(session, tile, "river_network", [])


def _file_cache_path(root: Path, tile: mvt.TileInput) -> Path:
    key = mvt.cache_key(tile)
    return root / key[:2] / f"{key}.pbf"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _MVT_LOGGER and record.levelno >= logging.WARNING]


@pytest.fixture(autouse=True)
def _fresh_probe_verdicts() -> Any:
    mvt.reset_db_tile_cache_write_probe()
    yield
    mvt.reset_db_tile_cache_write_probe()


@pytest.fixture()
def file_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("build", (_build_raw, _build_encoded), ids=("raw-tile", "encoded-tile"))
def test_read_only_role_sends_no_cache_table_write_and_probes_once(
    file_cache: Path, caplog: pytest.LogCaptureFixture, build: Any
) -> None:
    caplog.set_level(logging.INFO, logger=_MVT_LOGGER)
    session = _RecordingSession(_Engine(), _READ_ONLY)
    tiles = [_tile(x) for x in (50, 51, 52)]

    responses = [build(session, tile) for tile in tiles]

    assert session.cache_table_writes() == []
    assert session.commits == 0
    # Asked once for the engine, not once per miss.
    assert len(session.probes()) == 1
    # The file cache is the effective cache: written, and reported as a miss.
    assert [response.cache_status for response in responses] == ["miss", "miss", "miss"]
    for tile, response in zip(tiles, responses, strict=True):
        assert _file_cache_path(file_cache, tile).read_bytes() == response.data
    # The production mode is not an anomaly: no WARNING, and nothing failed.
    assert _warnings(caplog) == []
    assert session.rollbacks == 0


def test_read_only_role_without_a_file_cache_is_a_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mvt.MVT_FILE_CACHE_DIR_ENV, raising=False)
    session = _RecordingSession(_Engine(), _READ_ONLY)

    response = _build_raw(session, _tile(50))

    assert response.cache_status == "bypass"
    assert session.cache_table_writes() == []


def test_the_probe_names_both_tables_and_both_privileges_and_guards_missing_objects() -> None:
    session = _RecordingSession(_Engine(), _READ_ONLY)

    _build_raw(session, _tile(50))

    (probe,) = session.probes()
    # The upsert is INSERT ... ON CONFLICT DO UPDATE on each table, and a missing
    # table must yield NULL (to_regclass), never an ERROR.
    for table in ("map.tile_cache", "map.tile_layer"):
        for privilege in ("INSERT", "UPDATE"):
            assert f"has_table_privilege(to_regclass('{table}'), '{privilege}')" in probe
    assert _CACHE_TABLE_WRITE.search(probe) is None
    # Read-only: the probe is the last statement of the miss -- none of the
    # write path's catalog round trips follow it.
    assert session.statements[-1] == probe


def test_writable_role_upserts_the_layer_and_the_tile_as_before(file_cache: Path) -> None:
    session = _RecordingSession(_Engine(), _WRITABLE)
    tiles = [_tile(50), _tile(51)]

    responses = [_build_raw(session, tile) for tile in tiles]

    writes = session.cache_table_writes()
    assert len(writes) == 4
    for layer_upsert, cache_upsert in (writes[0:2], writes[2:4]):
        assert "INSERT INTO map.tile_layer" in layer_upsert
        assert "ON CONFLICT (layer_id) DO UPDATE SET" in layer_upsert
        assert "INSERT INTO map.tile_cache" in cache_upsert
        assert "ON CONFLICT(cache_key) DO UPDATE SET" in cache_upsert
    assert session.commits == 2
    assert [response.cache_status for response in responses] == ["miss", "miss"]
    # The DB write succeeded, so the file cache was never the fallback.
    assert list(file_cache.rglob("*.pbf")) == []
    assert len(session.probes()) == 1


@pytest.mark.parametrize(
    "probe",
    (
        [],
        [{"other_column": True}],
        [{"writable": None}],
        [{"writable": 1}],
        [{"writable": "t"}],
        ProgrammingError("SELECT ...", {}, Exception("function has_table_privilege does not exist")),
    ),
    ids=("no-row", "missing-key", "null", "integer-1", "string-t", "raises"),
)
def test_a_probe_without_a_boolean_answer_means_not_writable_and_warns_once(
    file_cache: Path, caplog: pytest.LogCaptureFixture, probe: Any
) -> None:
    caplog.set_level(logging.INFO, logger=_MVT_LOGGER)
    session = _RecordingSession(_Engine(), probe)
    tiles = [_tile(50), _tile(51)]

    responses = [_build_raw(session, tile) for tile in tiles]

    assert session.cache_table_writes() == []
    assert len(session.probes()) == 1
    assert [response.cache_status for response in responses] == ["miss", "miss"]
    assert len(list(file_cache.rglob("*.pbf"))) == 2
    (warning,) = _warnings(caplog)
    assert "write-privilege probe" in warning.getMessage()
    # A statement that raised leaves the transaction aborted: rolled back once.
    assert session.rollbacks == (1 if isinstance(probe, Exception) else 0)


def test_each_engine_is_probed_separately(file_cache: Path) -> None:
    read_only_engine, writable_engine = _Engine(), _Engine()
    first = _RecordingSession(read_only_engine, _READ_ONLY)
    second = _RecordingSession(writable_engine, _WRITABLE)
    # A second session on the first engine (the per-request shape in production).
    first_again = _RecordingSession(read_only_engine, _WRITABLE)

    _build_raw(first, _tile(50))
    _build_raw(second, _tile(51))
    _build_raw(first_again, _tile(52))

    assert first.cache_table_writes() == []
    assert len(second.cache_table_writes()) == 2
    # The engine's verdict is reused: no probe, and its scripted `True` is never consulted.
    assert first_again.probes() == []
    assert first_again.cache_table_writes() == []


def test_a_collected_engine_takes_its_verdict_with_it(file_cache: Path) -> None:
    """The cache is keyed by `id(engine)`; a recycled id must not inherit a verdict."""
    engine = _Engine()
    _build_raw(_RecordingSession(engine, _READ_ONLY), _tile(50))
    assert list(mvt._DB_TILE_CACHE_WRITABLE.values()) == [False]

    del engine
    gc.collect()

    assert mvt._DB_TILE_CACHE_WRITABLE == {}


def test_reset_helper_forces_a_new_probe(file_cache: Path) -> None:
    engine = _Engine()
    _build_raw(_RecordingSession(engine, _READ_ONLY), _tile(50))

    mvt.reset_db_tile_cache_write_probe()
    after_reset = _RecordingSession(engine, _WRITABLE)
    _build_raw(after_reset, _tile(51))

    assert len(after_reset.probes()) == 1
    assert len(after_reset.cache_table_writes()) == 2


def test_an_engine_that_cannot_be_weakly_referenced_is_probed_each_time_and_never_cached(file_cache: Path) -> None:
    # The shape of `tests/hydro_display_mvt_helpers.py::_Session.bind`.
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session = _RecordingSession(bind, _READ_ONLY)

    for x in (50, 51, 52):
        _build_raw(session, _tile(x))

    assert len(session.probes()) == 3
    assert session.cache_table_writes() == []
    assert mvt._DB_TILE_CACHE_WRITABLE == {}


def test_sqlite_is_never_probed_and_still_writes(file_cache: Path) -> None:
    session = _RecordingSession(_Engine(dialect="sqlite"), _READ_ONLY)

    response = _build_raw(session, _tile(50))

    assert session.probes() == []
    assert len(session.cache_table_writes()) == 2
    assert session.commits == 1
    assert response.cache_status == "miss"
    assert mvt._DB_TILE_CACHE_WRITABLE == {}
