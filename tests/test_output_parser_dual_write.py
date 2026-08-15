"""Unit coverage for the river_timeseries surrogate-key dual write (#1340).

The production writer (``PsycopgOutputParserRepository``) must populate the
seven normalized columns added by migration 000050 in the SAME INSERT that
writes the legacy text columns, resolve the four keys from the two load
queries it already runs (zero extra round-trips), and fail the whole batch
closed when a key cannot be resolved.

Everything here is asserted against the statements the repository actually
hands to psycopg2 — a fake connection records them in order — so the shape
claims are mechanical, not narrative.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from workers.output_parser.parser import (
    OutputParsingError,
    PsycopgOutputParserRepository,
    RiverTimeseriesRow,
    RunIdentityKeys,
)

# Spelled out rather than imported: asserting a constant against itself would
# pass no matter what the writer raises.
IDENTITY_KEY_MISSING_ERROR_CODE = "OUTPUT_PARSE_IDENTITY_KEY_MISSING"

RUN_KEY = 7001
RIVER_NETWORK_VERSION_KEY = 8002
BASIN_VERSION_KEY = 9003
SEGMENT_KEYS: dict[str, int | None] = {"seg_0": 101, "seg_1": 102, "seg_2": 103}

EXPECTED_INSERT_COLUMNS = [
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

# Text columns the pre-#1340 statement refreshed. Byte-for-byte unchanged: the
# rollback-safety guarantee is that the text write is untouched.
EXPECTED_TEXT_SET_COLUMNS = {
    "basin_version_id",
    "lead_time_hours",
    "value",
    "unit",
    "quality_flag",
}
# Mirror rule (design D3): every refreshed text column that HAS a surrogate
# counterpart gets it re-set too. Conflict-target identity columns are re-set
# on neither side.
EXPECTED_SURROGATE_SET_COLUMNS = {"basin_version_key", "unit_e", "quality_flag_e"}


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


# The parser flips a whole batch to ``qc_warning`` when QC fails, so both enum
# vocabulary members are reachable in production. Rotating them WITHIN one batch
# is what makes a hardcoded ``'ok'`` in the enum position observable.
QUALITY_FLAGS = ("ok", "qc_warning")


def _rows(count: int = 3, quality_flags: tuple[str, ...] = QUALITY_FLAGS) -> tuple[RiverTimeseriesRow, ...]:
    return tuple(
        RiverTimeseriesRow(
            run_id="run_a",
            basin_version_id="basin_v1",
            river_network_version_id="rivnet_v1",
            river_segment_id=f"seg_{n}",
            valid_time=_t(n),
            lead_time_hours=n,
            variable="q_down",
            value=float(n),
            unit="m3/s",
            quality_flag=quality_flags[n % len(quality_flags)],
        )
        for n in range(count)
    )


def _run_identity() -> RunIdentityKeys:
    return RunIdentityKeys(
        run_key=RUN_KEY,
        river_network_version_key=RIVER_NETWORK_VERSION_KEY,
        basin_version_key=BASIN_VERSION_KEY,
    )


# ---------------------------------------------------------------------------
# Fake psycopg2 plumbing
# ---------------------------------------------------------------------------


class _Description:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self._columns: list[str] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.connection.executions.append((statement, tuple(parameters)))
        self._columns, self._rows = self.connection.respond(statement)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    @property
    def description(self) -> Any:
        if self._columns is None:
            return None
        return [_Description(name) for name in self._columns]


class _FakeConnection:
    """Records statements in order; answers SELECTs from a needle table."""

    encoding = "UTF8"

    def __init__(self, responses: list[tuple[str, list[str], list[tuple[Any, ...]]]] | None = None) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.responses = responses or []
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = False

    def respond(self, statement: str) -> tuple[list[str] | None, list[tuple[Any, ...]]]:
        normalized = " ".join(statement.lower().split())
        for needle, columns, rows in self.responses:
            if needle.lower() in normalized:
                return columns, rows
        return None, []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def _repository(connection: _FakeConnection) -> PsycopgOutputParserRepository:
    from dataclasses import replace

    return replace(PsycopgOutputParserRepository(database_url="postgres://unused"), _connection=connection)


def _patch_execute_values(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[tuple[Any, ...]]]]:
    from psycopg2 import extras as psycopg2_extras  # type: ignore[import-untyped]

    calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def _fake_execute_values(cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        cursor.connection.executions.append((f"execute_values:{statement}", ()))
        calls.append((statement, list(rows)))

    monkeypatch.setattr(psycopg2_extras, "execute_values", _fake_execute_values)
    return calls


def _insert_columns(statement: str) -> list[str]:
    match = re.search(r"INSERT INTO hydro\.river_timeseries\s*\((.*?)\)\s*VALUES", statement, re.S)
    assert match is not None, statement
    return [column.strip() for column in match.group(1).split(",") if column.strip()]


def _do_update_assignments(statement: str) -> dict[str, str]:
    match = re.search(r"DO UPDATE SET\s*(.*)", statement, re.S)
    assert match is not None, statement
    assignments: dict[str, str] = {}
    for fragment in match.group(1).split(","):
        fragment = fragment.strip()
        if not fragment:
            continue
        column, _, value = fragment.partition("=")
        assignments[column.strip()] = value.strip()
    return assignments


def _conflict_target(statement: str) -> list[str]:
    match = re.search(r"ON CONFLICT\s*\((.*?)\)\s*DO UPDATE", statement, re.S)
    assert match is not None, statement
    return [column.strip() for column in match.group(1).split(",") if column.strip()]


def _upsert(
    connection: _FakeConnection,
    rows: tuple[RiverTimeseriesRow, ...],
    **overrides: Any,
) -> None:
    kwargs: dict[str, Any] = {
        "batch_size": 8,
        "run_identity": _run_identity(),
        "segment_keys": dict(SEGMENT_KEYS),
    }
    kwargs.update(overrides)
    _repository(connection).upsert_river_timeseries(rows, **kwargs)


# ---------------------------------------------------------------------------
# INSERT shape: seven new columns, arity 17, enum values same-sourced
# ---------------------------------------------------------------------------


def test_insert_column_list_appends_the_seven_normalized_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_execute_values(monkeypatch)
    _upsert(_FakeConnection(), _rows())

    statement, _value_rows = calls[0]
    assert _insert_columns(statement) == EXPECTED_INSERT_COLUMNS


def test_every_value_row_has_arity_17_with_keys_and_same_sourced_enum_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each enum column carries the row's own text value (design D2). The batch
    mixes both ``quality_flag`` vocabulary members, so an enum position wired to
    a literal instead of the row cannot pass."""
    calls = _patch_execute_values(monkeypatch)
    rows = _rows()
    _upsert(_FakeConnection(), rows)

    statement, value_rows = calls[0]
    columns = _insert_columns(statement)
    assert len(value_rows) == len(rows)
    observed_quality_flags: set[str] = set()

    for row, value_row in zip(rows, value_rows, strict=True):
        assert len(value_row) == 17
        field = dict(zip(columns, value_row, strict=True))

        assert field["run_key"] == RUN_KEY
        assert field["river_network_version_key"] == RIVER_NETWORK_VERSION_KEY
        assert field["basin_version_key"] == BASIN_VERSION_KEY
        assert field["river_segment_key"] == SEGMENT_KEYS[row.river_segment_id]

        # Values first: the enum column must equal the row's own text value.
        assert field["variable_e"] == row.variable
        assert field["unit_e"] == row.unit
        assert field["quality_flag_e"] == row.quality_flag
        assert (field["variable_e"], field["unit_e"]) == ("q_down", "m3/s")
        # Same in-process object, not merely equal. Weak evidence on its own
        # (interned literals compare identical anyway) — it only documents that
        # the writer forwards the value rather than re-deriving it.
        assert field["variable_e"] is field["variable"]
        assert field["unit_e"] is field["unit"]
        assert field["quality_flag_e"] is field["quality_flag"]
        observed_quality_flags.add(field["quality_flag_e"])

    assert observed_quality_flags == set(QUALITY_FLAGS), "both enum members must be exercised in one batch"


def test_text_columns_of_each_value_row_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback safety: the first ten values are the pre-#1340 tuple, in order."""
    calls = _patch_execute_values(monkeypatch)
    rows = _rows()
    _upsert(_FakeConnection(), rows)

    _statement, value_rows = calls[0]
    for row, value_row in zip(rows, value_rows, strict=True):
        assert value_row[:10] == (
            row.run_id,
            row.basin_version_id,
            row.river_network_version_id,
            row.river_segment_id,
            row.valid_time,
            row.lead_time_hours,
            row.variable,
            row.value,
            row.unit,
            row.quality_flag,
        )


def test_on_conflict_set_mirrors_exactly_the_three_refreshable_surrogate_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_execute_values(monkeypatch)
    _upsert(_FakeConnection(), _rows())

    statement, _value_rows = calls[0]
    assignments = _do_update_assignments(statement)

    assert set(assignments) == EXPECTED_TEXT_SET_COLUMNS | EXPECTED_SURROGATE_SET_COLUMNS
    for column in assignments:
        assert assignments[column] == f"EXCLUDED.{column}"

    # Conflict-target identity is re-set on NEITHER side.
    assert "ON CONFLICT (run_id, river_network_version_id, river_segment_id, variable, valid_time)" in statement
    for identity_column in (
        "run_id",
        "river_network_version_id",
        "river_segment_id",
        "variable",
        "valid_time",
        "run_key",
        "river_network_version_key",
        "river_segment_key",
        "variable_e",
    ):
        assert identity_column not in assignments


def test_integration_replay_statement_stays_equivalent_to_the_production_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tests/test_river_ts_dual_write_integration.py::_REPLAY_INSERT`` is a
    hand-copy of this writer's INSERT: the real-DB conflict test only proves
    anything about production while the copy still matches. Lock it here, in the
    unit lane, because that is the lane an edit to the writer's SQL runs through
    (the integration lane only runs on node-27).

    The VALUES segments legitimately differ (named ``%(...)s`` placeholders vs
    the ``execute_values`` template), so the comparison is on the INSERT column
    list, the conflict target and the ``DO UPDATE SET`` assignments.
    """
    from tests.test_river_ts_dual_write_integration import _REPLAY_INSERT

    calls = _patch_execute_values(monkeypatch)
    _upsert(_FakeConnection(), _rows())
    production_statement, _value_rows = calls[0]

    assert _insert_columns(_REPLAY_INSERT) == _insert_columns(production_statement)
    assert _conflict_target(_REPLAY_INSERT) == _conflict_target(production_statement)
    assert _do_update_assignments(_REPLAY_INSERT) == _do_update_assignments(production_statement)


# ---------------------------------------------------------------------------
# Key resolution rides the two existing load queries
# ---------------------------------------------------------------------------


def _run_context_response() -> tuple[str, list[str], list[tuple[Any, ...]]]:
    columns = [
        "run_id",
        "model_id",
        "basin_version_id",
        "source_id",
        "cycle_time",
        "start_time",
        "output_uri",
        "run_type",
        "scenario_id",
        "river_network_version_id",
        "cycle_id",
        "run_key",
        "basin_version_key",
        "river_network_version_key",
    ]
    row = (
        "run_a",
        "model_a",
        "basin_v1",
        "gfs",
        _t(0),
        _t(0),
        None,
        "forecast",
        None,
        "rivnet_v1",
        "gfs_2026060100",
        RUN_KEY,
        BASIN_VERSION_KEY,
        RIVER_NETWORK_VERSION_KEY,
    )
    return ("from hydro.hydro_run h", columns, [row])


def test_load_run_context_resolves_the_three_run_level_keys_in_one_query() -> None:
    connection = _FakeConnection([_run_context_response()])
    context = _repository(connection).load_run_context("run_a")

    assert (context.run_key, context.basin_version_key, context.river_network_version_key) == (
        RUN_KEY,
        BASIN_VERSION_KEY,
        RIVER_NETWORK_VERSION_KEY,
    )
    assert len(connection.executions) == 1, "keys must ride the existing query, not add a round-trip"

    statement = " ".join(connection.executions[0][0].split())
    for projection in ("h.run_key", "bv.basin_version_key", "rnv.river_network_version_key"):
        assert projection in statement
    # basin_version is joined on the hydro_run side, NOT the model_instance
    # side: the fact row's basin_version_id text column comes from hydro_run.
    assert "JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id" in statement
    assert "bv.basin_version_id = mi.basin_version_id" not in statement
    assert "JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id" in (
        statement
    )


def test_load_run_context_still_raises_hydro_run_not_found_when_the_join_yields_nothing() -> None:
    connection = _FakeConnection([("from hydro.hydro_run h", [], [])])
    with pytest.raises(OutputParsingError) as exc_info:
        _repository(connection).load_run_context("run_a")
    assert exc_info.value.error_code == "HYDRO_RUN_NOT_FOUND"


_SEGMENT_COLUMNS = ["river_segment_id", "river_network_version_id", "segment_order", "river_segment_key"]


def test_load_river_segments_primary_query_resolves_the_segment_key() -> None:
    connection = _FakeConnection(
        [("shud_output_river", _SEGMENT_COLUMNS, [("seg_0", "rivnet_v1", 1, 101)])]
    )
    segments = _repository(connection).load_river_segments("rivnet_v1")

    assert [segment.river_segment_key for segment in segments] == [101]
    assert len(connection.executions) == 1
    assert "river_segment_key" in connection.executions[0][0]


def test_load_river_segments_fallback_query_also_resolves_the_segment_key() -> None:
    """Networks with no ``shud_output_river`` marker take the fallback branch —
    which is the production main path for such a network, so it must project
    the key too (otherwise every parse fails closed)."""
    connection = _FakeConnection(
        [
            ("shud_output_river", _SEGMENT_COLUMNS, []),
            ("from core.river_segment", _SEGMENT_COLUMNS, [("seg_1", "rivnet_v1", 2, 102)]),
        ]
    )
    segments = _repository(connection).load_river_segments("rivnet_v1")

    assert [segment.river_segment_key for segment in segments] == [102]
    assert len(connection.executions) == 2
    fallback_statement = connection.executions[1][0]
    assert "shud_output_river" not in fallback_statement
    assert "river_segment_key" in fallback_statement


# ---------------------------------------------------------------------------
# Fail-closed: an unresolvable key costs the whole batch, zero statements run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"run_identity": None}, "run identity keys"),
        ({"run_identity": RunIdentityKeys()}, "run_key"),
        (
            {"run_identity": RunIdentityKeys(run_key=RUN_KEY, river_network_version_key=RIVER_NETWORK_VERSION_KEY)},
            "basin_version_key",
        ),
        ({"segment_keys": None}, "river_segment_key mappings"),
        ({"segment_keys": {"seg_0": 101}}, "seg_1"),
        ({"segment_keys": {"seg_0": 101, "seg_1": None, "seg_2": 103}}, "seg_1"),
    ],
)
def test_missing_surrogate_key_fails_the_batch_closed_before_any_statement(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    needle: str,
) -> None:
    calls = _patch_execute_values(monkeypatch)
    connection = _FakeConnection()

    with pytest.raises(OutputParsingError) as exc_info:
        _upsert(connection, _rows(), **overrides)

    assert exc_info.value.error_code == IDENTITY_KEY_MISSING_ERROR_CODE
    assert needle in exc_info.value.message
    assert connection.executions == [], "no statement may run once a key is unresolvable"
    assert calls == [], "the whole batch must be dropped, not partially written"


def test_empty_batch_still_short_circuits_before_the_key_check() -> None:
    """An empty batch has nothing to key and must stay a no-op."""
    connection = _FakeConnection()
    _repository(connection).upsert_river_timeseries((), batch_size=8)
    assert connection.executions == []


# ---------------------------------------------------------------------------
# The dual write adds no statement: guard -> DELETE -> INSERT, unchanged
# ---------------------------------------------------------------------------


def test_statement_sequence_is_unchanged_and_adds_no_key_resolution_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_execute_values(monkeypatch)
    connection = _FakeConnection()
    _upsert(connection, _rows())

    shapes = [" ".join(statement.split()) for statement, _params in connection.executions]
    assert [_classify(shape) for shape in shapes] == [
        "existence_probe",
        "guard_set_local",
        "guard_chunks",
        "guard_reset",
        "delete",
        "insert",
    ]
    # Keys come from the load queries; the write path must not resolve them.
    for shape in shapes:
        assert "core.river_segment" not in shape
        assert "core.basin_version" not in shape
        assert "core.river_network_version" not in shape
        assert "hydro.hydro_run" not in shape


def test_unbound_repository_self_delegates_with_the_keys_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_connection is None`` re-enters through ``transaction()``. That hop
    must carry ``run_identity`` / ``segment_keys`` through unchanged — dropping
    them there would fail the batch closed on the inner call instead of writing
    it (parser.py self-delegation branch)."""
    from contextlib import contextmanager

    calls = _patch_execute_values(monkeypatch)
    connection = _FakeConnection()
    bound = _repository(connection)
    forwarded: list[dict[str, Any]] = []

    class _Recorder:
        def upsert_river_timeseries(self, rows: Any, **kwargs: Any) -> None:
            forwarded.append({"rows": rows, **kwargs})
            bound.upsert_river_timeseries(rows, **kwargs)

    @contextmanager
    def _fake_transaction(_self: Any) -> Any:
        yield _Recorder()

    monkeypatch.setattr(PsycopgOutputParserRepository, "transaction", _fake_transaction)

    unbound = PsycopgOutputParserRepository(database_url="postgres://unused")
    assert unbound._connection is None
    rows = _rows()
    run_identity = _run_identity()
    segment_keys = dict(SEGMENT_KEYS)
    unbound.upsert_river_timeseries(
        rows, batch_size=8, run_identity=run_identity, segment_keys=segment_keys
    )

    assert len(forwarded) == 1
    assert forwarded[0]["rows"] is rows
    assert forwarded[0]["batch_size"] == 8
    assert forwarded[0]["run_identity"] is run_identity
    assert forwarded[0]["segment_keys"] is segment_keys

    # ...and the inner, connection-bound repository wrote those very keys.
    statement, value_rows = calls[0]
    columns = _insert_columns(statement)
    for row, value_row in zip(rows, value_rows, strict=True):
        field = dict(zip(columns, value_row, strict=True))
        assert (
            field["run_key"],
            field["river_network_version_key"],
            field["basin_version_key"],
            field["river_segment_key"],
        ) == (RUN_KEY, RIVER_NETWORK_VERSION_KEY, BASIN_VERSION_KEY, SEGMENT_KEYS[row.river_segment_id])


def _classify(statement: str) -> str:
    lowered = statement.lower()
    if lowered.startswith("execute_values:"):
        return "insert"
    if "select 1 from hydro.river_timeseries" in lowered:
        return "existence_probe"
    if "select min(valid_time)" in lowered:
        return "window_probe"
    if "timescaledb_information.chunks" in lowered:
        return "guard_chunks"
    if "set local statement_timeout = default" in lowered:
        return "guard_reset"
    if "set local statement_timeout" in lowered:
        return "guard_set_local"
    if lowered.startswith("delete from hydro.river_timeseries"):
        return "delete"
    return f"other:{statement[:40]}"
