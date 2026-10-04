"""#2713: the compression/ingest advisory fence and the chunk-DDL lanes that take it.

Covers the public seams the change touches on the DDL side:

* ``packages.common.timeseries_compression_fence`` -- keys, try/acquire/release;
* the compression runner -- the ``FENCE_WAIT_MS`` knob, the fence-first
  session in ``_default_compress_chunk`` (driven through ``main``), the
  ``deferred_contended`` / ``deferred`` receipt semantics and schema 2.2;
* the retention drop -- the same exclusive fence before ``drop_chunks`` and its
  ``lock-contention(55P03)`` rendering.

Only the driver boundary (``psycopg2``) and the clock are faked. Expected
values are literals: the fence keys come from an independent CRC-32 (the gzip
trailer of each name), not from re-running the module's own derivation.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common import timeseries_compression_fence as fence
from scripts import node27_timeseries_compression as compression
from scripts import node27_timeseries_retention as retention

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _ROOT / "schemas/timeseries_compression_receipt.schema.json"
_ENV_EXAMPLE_PATH = _ROOT / "infra/env/node27-timeseries-compression.example"
_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
_FENCE_WAIT_KEY = "NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS"

# (classid, objid) per hypertable. objid = CRC-32 of the CANONICAL family name
# folded into int4 (a `_legacy` sibling shares its canonical key, because both
# lock the same FK-referenced tables); the unsigned CRCs were read off
# `printf %s <name> | gzip -c | tail -c8`.
_RIVER_FAMILY_KEY = (2713, 2330119012 - 2**32)
_FORCING_FAMILY_KEY = (2713, 431776087)
_PINNED_KEYS = {
    "hydro.river_timeseries": _RIVER_FAMILY_KEY,
    "met.forcing_station_timeseries": _FORCING_FAMILY_KEY,
    "hydro.river_timeseries_legacy": _RIVER_FAMILY_KEY,
    "met.forcing_station_timeseries_legacy": _FORCING_FAMILY_KEY,
}


class _DriverError(Exception):
    def __init__(self, message: str, pgcode: str | None) -> None:
        super().__init__(message)
        self.pgcode = pgcode


class _RecordingCursor:
    """Records every statement; answers the fence statements from a script."""

    def __init__(
        self,
        statements: list[tuple[str, Any]],
        *,
        try_result: Any = (True,),
        lock_error: Exception | None = None,
        compress_error: Exception | None = None,
        unlock_error: Exception | None = None,
    ) -> None:
        self.statements = statements
        self.try_result = try_result
        self.lock_error = lock_error
        self.compress_error = compress_error
        self.unlock_error = unlock_error
        self._row: Any = None

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))
        if sql.startswith("SELECT pg_try_advisory_xact_lock_shared"):
            self._row = self.try_result
        elif sql.startswith("SELECT pg_advisory_lock(") and self.lock_error is not None:
            raise self.lock_error
        elif sql.startswith("SELECT compress_chunk(") and self.compress_error is not None:
            raise self.compress_error
        elif sql.startswith("SELECT pg_advisory_unlock(") and self.unlock_error is not None:
            raise self.unlock_error
        else:
            self._row = (None,)

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return [self._row]


class _RecordingConnection:
    def __init__(self, cursor_factory: Any) -> None:
        self._cursor_factory = cursor_factory
        self.exits: list[type[BaseException] | None] = []
        self.closed = 0

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_exc: object) -> bool:
        # psycopg2: clean exit commits, a raising block rolls back.
        self.exits.append(exc_type)
        return False

    def cursor(self, *_args: object, **_kwargs: object) -> _RecordingCursor:
        return self._cursor_factory()

    def close(self) -> None:
        self.closed += 1


def _install_driver(monkeypatch: pytest.MonkeyPatch, **cursor_kwargs: Any) -> tuple[list, list]:
    statements: list[tuple[str, Any]] = []
    connections: list[_RecordingConnection] = []

    def connect(*_args: object, **_kwargs: object) -> _RecordingConnection:
        connection = _RecordingConnection(lambda: _RecordingCursor(statements, **cursor_kwargs))
        connections.append(connection)
        return connection

    module = types.ModuleType("psycopg2")
    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    return statements, connections


def _fixed_clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> None:
    clock = iter(readings)
    monkeypatch.setattr(fence, "_monotonic", lambda: next(clock))


# ---------------------------------------------------------------------------
# Fence module
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("hypertable", "expected"), sorted(_PINNED_KEYS.items()))
def test_fence_keys_are_pinned_per_hypertable(hypertable: str, expected: tuple[int, int]) -> None:
    assert fence.fence_key(hypertable) == expected


def test_fence_keys_are_one_int4_pair_per_canonical_family() -> None:
    keys = [fence.fence_key(name) for name in fence.COMPRESSED_HYPERTABLES]
    assert sorted(fence.COMPRESSED_HYPERTABLES) == sorted(_PINNED_KEYS)
    # Exactly two families over the four lifecycle hypertables, and they differ.
    assert set(keys) == {_RIVER_FAMILY_KEY, _FORCING_FAMILY_KEY}
    assert _RIVER_FAMILY_KEY != _FORCING_FAMILY_KEY
    for classid, objid in keys:
        assert -(2**31) <= classid < 2**31 and -(2**31) <= objid < 2**31


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("hydro.river_timeseries_legacy", "hydro.river_timeseries"),
        ("met.forcing_station_timeseries_legacy", "met.forcing_station_timeseries"),
    ],
)
def test_a_legacy_sibling_shares_its_canonical_family_key(legacy: str, canonical: str) -> None:
    # Ingest writes only the canonical table, but DDL on the legacy chunk locks
    # the same FK-referenced tables: a separate key would leave the cycle open.
    assert fence.fence_key(legacy) == fence.fence_key(canonical)


def test_legacy_ddl_excludes_a_canonical_ingest_try_on_the_same_key() -> None:
    statements: list[tuple[str, Any]] = []
    cursor = _RecordingCursor(statements)
    fence.acquire_compression_fence(cursor, "met.forcing_station_timeseries_legacy", 1000)
    fence.try_ingest_fence(cursor, "met.forcing_station_timeseries")
    lock_params = next(params for sql, params in statements if sql.startswith("SELECT pg_advisory_lock("))
    try_params = next(
        params for sql, params in statements if sql.startswith("SELECT pg_try_advisory_xact_lock_shared")
    )
    assert tuple(lock_params) == tuple(try_params) == _FORCING_FAMILY_KEY


@pytest.mark.parametrize("hypertable", ["hydro.hydro_run", "river_timeseries", "hydro.river_timeseries "])
def test_a_hypertable_outside_the_lifecycle_set_has_no_fence(hypertable: str) -> None:
    with pytest.raises(ValueError, match="not a compressed lifecycle hypertable"):
        fence.fence_key(hypertable)


@pytest.mark.parametrize(("row", "expected"), [((True,), True), ((False,), False), ({"x": True}, True)])
def test_try_ingest_fence_is_a_single_shared_try(row: Any, expected: bool) -> None:
    statements: list[tuple[str, Any]] = []
    cursor = _RecordingCursor(statements, try_result=row)

    assert fence.try_ingest_fence(cursor, "met.forcing_station_timeseries") is expected
    assert statements == [("SELECT pg_try_advisory_xact_lock_shared(%s, %s)", (2713, 431776087))]


@pytest.mark.parametrize("row", [None, (), (1,), ("t",), (True, True)])
def test_try_ingest_fence_fails_closed_on_an_unexpected_result(row: Any) -> None:
    cursor = _RecordingCursor([], try_result=row)
    with pytest.raises(RuntimeError, match="unexpected compression fence result"):
        fence.try_ingest_fence(cursor, "hydro.river_timeseries")


def test_acquire_bounds_the_wait_resets_lock_timeout_and_reports_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[tuple[str, Any]] = []
    _fixed_clock(monkeypatch, 10.0, 10.0011)

    elapsed = fence.acquire_compression_fence(_RecordingCursor(statements), "hydro.river_timeseries", 5000)

    assert elapsed == 2  # 1.1 ms rounds UP: the charge never undercounts the wait
    assert statements == [
        ("SET lock_timeout = 5000", None),
        ("SELECT pg_advisory_lock(%s, %s)", (2713, -1964848284)),
        ("RESET lock_timeout", None),
    ]


def test_acquire_maps_lock_not_available_to_fence_contended(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch, 3.0, 3.5)
    cursor = _RecordingCursor([], lock_error=_DriverError("canceling statement due to lock timeout", "55P03"))

    with pytest.raises(fence.FenceContended) as excinfo:
        fence.acquire_compression_fence(cursor, "hydro.river_timeseries", 500)

    assert excinfo.value.elapsed_ms == 500
    assert excinfo.value.hypertable == "hydro.river_timeseries"
    assert excinfo.value.pgcode == "55P03"


def test_acquire_does_not_disguise_other_driver_errors_as_contention() -> None:
    cursor = _RecordingCursor([], lock_error=_DriverError("canceling statement due to statement timeout", "57014"))
    with pytest.raises(_DriverError):
        fence.acquire_compression_fence(cursor, "hydro.river_timeseries", 500)


@pytest.mark.parametrize("wait_ms", [0, -1, True, 1.5])
def test_acquire_refuses_an_unbounded_or_malformed_wait(wait_ms: Any) -> None:
    statements: list[tuple[str, Any]] = []
    with pytest.raises(ValueError):
        fence.acquire_compression_fence(_RecordingCursor(statements), "hydro.river_timeseries", wait_ms)
    assert statements == []


def test_release_unlocks_the_same_key() -> None:
    statements: list[tuple[str, Any]] = []
    fence.release_compression_fence(_RecordingCursor(statements), "met.forcing_station_timeseries")
    assert statements == [("SELECT pg_advisory_unlock(%s, %s)", (2713, 431776087))]


# ---------------------------------------------------------------------------
# Compression runner: knob
# ---------------------------------------------------------------------------


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {"enforce": False, "receipt_path": None, "lock_path": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _env(tmp_path: Path, **override: str | None) -> dict[str, str]:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "172800",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "2",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    for key, value in override.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


@pytest.mark.parametrize("raw", [None, ""])
def test_fence_wait_defaults_to_fifteen_minutes(tmp_path: Path, raw: str | None) -> None:
    config = compression.config_from_args(_args(), _env(tmp_path, **{_FENCE_WAIT_KEY: raw}))
    assert config.fence_wait_ms == 900_000


def test_fence_wait_override_is_honoured(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(), _env(tmp_path, **{_FENCE_WAIT_KEY: "600000"}))
    assert config.fence_wait_ms == 600_000


@pytest.mark.parametrize("raw", ["0", "-1", "abc", " 600000", "600000 ", "1_000", "+5", "60e4"])
def test_a_malformed_fence_wait_fails_closed(tmp_path: Path, raw: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match=_FENCE_WAIT_KEY):
        compression.config_from_args(_args(), _env(tmp_path, **{_FENCE_WAIT_KEY: raw}))


@pytest.mark.parametrize("raw", ["3600000", "3600001"])
def test_a_fence_wait_not_below_the_compress_timeout_fails_closed(tmp_path: Path, raw: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match="strictly less than"):
        compression.config_from_args(_args(), _env(tmp_path, **{_FENCE_WAIT_KEY: raw}))


def test_lowering_the_compress_timeout_under_the_default_wait_names_the_knob(tmp_path: Path) -> None:
    env = _env(tmp_path, NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="900000")
    with pytest.raises(compression.CompressionConfigError) as excinfo:
        compression.config_from_args(_args(), env)
    assert _FENCE_WAIT_KEY in str(excinfo.value)
    assert "900000 >= 900000" in str(excinfo.value)


def test_a_bad_fence_wait_is_refused_before_any_db_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _env(tmp_path, **{_FENCE_WAIT_KEY: "3600000"}).items():
        monkeypatch.setenv(key, value)
    statements, connections = _install_driver(monkeypatch)
    watermark_calls: list[str] = []
    monkeypatch.setattr(
        compression, "fetch_display_watermark", lambda dsn, **_kwargs: watermark_calls.append(dsn) or _NOW
    )

    assert compression.main(["--enforce"]) == 1
    assert connections == [] and statements == [] and watermark_calls == []


def test_both_budget_knobs_are_keyword_only_without_defaults() -> None:
    parameters = inspect.signature(compression._default_compress_chunk).parameters
    for name in ("compress_timeout_ms", "fence_wait_ms"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        compression._default_compress_chunk("postgresql://x", _chunk("narrow"), compress_timeout_ms=3_600_000)


def test_env_example_pins_the_fence_wait_default_once() -> None:
    import re

    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert re.findall(rf"(?m)^{_FENCE_WAIT_KEY}=(\d+)$", text) == ["900000"]


# ---------------------------------------------------------------------------
# Compression runner: fence-first session (real ``_default_compress_chunk``)
# ---------------------------------------------------------------------------


def _chunk(label: str, *, schema: str = "hydro", name: str = "river_timeseries", age_days: int = 5):
    end = _NOW - timedelta(days=age_days)
    return compression.ChunkRow(
        hypertable_schema=schema,
        hypertable_name=name,
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=end - timedelta(days=1),
        range_end=end,
        is_compressed=False,
    )


def test_compress_session_charges_the_fence_wait_and_releases_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements, connections = _install_driver(monkeypatch)
    _fixed_clock(monkeypatch, 50.0, 50.0 + 123.4)  # 123.4 s spent waiting for in-flight writers

    elapsed = compression._default_compress_chunk(
        "postgresql://x",
        _chunk("_hyper_9_213_chunk", schema="met", name="forcing_station_timeseries"),
        compress_timeout_ms=3_600_000,
        fence_wait_ms=900_000,
    )

    assert elapsed == 123_400
    assert statements == [
        ("SET statement_timeout = 3600000", None),
        ("SET lock_timeout = 900000", None),
        ("SELECT pg_advisory_lock(%s, %s)", (2713, 431776087)),
        ("RESET lock_timeout", None),
        ("SET statement_timeout = 3476600", None),
        ("SELECT compress_chunk(%s::regclass)", ("_timescaledb_internal._hyper_9_213_chunk",)),
        ("SELECT pg_advisory_unlock(%s, %s)", (2713, 431776087)),
    ]
    [connection] = connections
    assert connection.exits == [None]  # committed, THEN unlocked
    assert connection.closed == 1


def test_compress_failure_still_releases_the_fence_and_keeps_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compress_error = _DriverError("deadlock detected", "40P01")
    statements, connections = _install_driver(
        monkeypatch, compress_error=compress_error, unlock_error=_DriverError("connection lost", None)
    )
    _fixed_clock(monkeypatch, 1.0, 1.0)

    with pytest.raises(_DriverError) as excinfo:
        compression._default_compress_chunk(
            "postgresql://x", _chunk("c1"), compress_timeout_ms=3_600_000, fence_wait_ms=900_000
        )

    assert excinfo.value is compress_error  # the unlock failure never masks it
    assert statements[-1] == ("SELECT pg_advisory_unlock(%s, %s)", (2713, -1964848284))
    [connection] = connections
    assert connection.exits == [_DriverError]  # rolled back before the unlock
    assert connection.closed == 1


def test_a_contended_fence_copies_nothing_and_skips_the_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    statements, connections = _install_driver(
        monkeypatch, lock_error=_DriverError("canceling statement due to lock timeout", "55P03")
    )
    _fixed_clock(monkeypatch, 0.0, 900.0)

    with pytest.raises(fence.FenceContended) as excinfo:
        compression._default_compress_chunk(
            "postgresql://x", _chunk("c1"), compress_timeout_ms=3_600_000, fence_wait_ms=900_000
        )

    assert excinfo.value.elapsed_ms == 900_000
    assert not any("compress_chunk" in sql or "pg_advisory_unlock" in sql for sql, _ in statements)
    assert connections[0].closed == 1


# ---------------------------------------------------------------------------
# Compression runner: receipt semantics
# ---------------------------------------------------------------------------


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _receipt(tmp_path: Path, chunks: list, compress: Any, *, measure_after_error: bool = False) -> dict:
    env = _env(tmp_path, NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="3")
    config = compression.config_from_args(_args(enforce=True), env)
    reconciled: list[str] = []

    def measure(_dsn: str, chunk: Any, *, after: bool = False) -> int:
        if after and measure_after_error:
            raise RuntimeError("sibling not visible")
        return 400 if after else 1000

    def reconcile(_dsn: str, chunk: Any) -> bool:
        reconciled.append(chunk.chunk_name)
        return False

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: list(chunks),
        measure_chunk_bytes=measure,
        compress_chunk=compress,
        reconcile_chunk_state=reconcile,
        head_sha="b" * 40,
    )
    receipt["_reconciled"] = reconciled
    return receipt


def _compress_script(outcomes: dict[str, Any]):
    def compress(_dsn: str, chunk: Any) -> int | None:
        outcome = outcomes[chunk.chunk_name]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return compress


def test_only_contended_chunks_make_a_deferred_tick_that_exits_zero_and_poisons_nothing(tmp_path: Path) -> None:
    chunks = [_chunk("busy", age_days=4), _chunk("done", age_days=5)]
    receipt = _receipt(
        tmp_path,
        chunks,
        _compress_script({"busy": fence.FenceContended("hydro.river_timeseries", 900_000), "done": 75}),
    )

    assert receipt.pop("_reconciled") == []  # no copy started, so nothing to reconcile
    assert receipt["outcome"] == "deferred"
    assert receipt["deferred_contended_count"] == 1
    busy, done = receipt["selected"]
    assert busy == {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "busy",
        "range_start": "2026-09-29T12:00:00Z",
        "range_end": "2026-09-30T12:00:00Z",
        "before_bytes": 1000,
        "after_bytes": None,
        "mutation_state": "deferred_contended",
        "fence_wait_elapsed_ms": 900_000,
    }
    assert done["mutation_state"] == "committed" and done["fence_wait_elapsed_ms"] == 75
    # The deferred chunk neither counts nor poisons: totals are exactly "done".
    assert receipt["per_table_totals"]["hydro.river_timeseries"] == {
        "before_bytes": 1000,
        "after_bytes": 400,
        "chunks_compressed": 1,
    }
    jsonschema.validate(receipt, _load_schema())


def test_a_real_failure_beside_a_deferral_is_still_partial(tmp_path: Path) -> None:
    chunks = [_chunk("busy", age_days=4), _chunk("broken", age_days=5)]
    receipt = _receipt(
        tmp_path,
        chunks,
        _compress_script(
            {
                "busy": fence.FenceContended("hydro.river_timeseries", 900_000),
                "broken": _DriverError("deadlock detected", "40P01"),
            }
        ),
    )

    assert receipt.pop("_reconciled") == ["broken"]  # only the real failure reconciles
    assert receipt["outcome"] == "partial"
    assert receipt["deferred_contended_count"] == 1
    assert [row["mutation_state"] for row in receipt["selected"]] == ["deferred_contended", "failed_before_mutation"]
    jsonschema.validate(receipt, _load_schema())


def test_a_clean_fenced_tick_records_each_charged_wait(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path, [_chunk("a", age_days=4)], _compress_script({"a": 0}))
    receipt.pop("_reconciled")
    assert receipt["outcome"] == "clean"
    assert receipt["deferred_contended_count"] == 0
    assert receipt["selected"][0]["fence_wait_elapsed_ms"] == 0
    assert receipt["budget"]["fence_wait_ms"] == 900_000
    jsonschema.validate(receipt, _load_schema())


def test_an_injected_compressor_without_a_fence_records_no_wait(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path, [_chunk("a", age_days=4)], _compress_script({"a": None}))
    receipt.pop("_reconciled")
    assert receipt["outcome"] == "clean"
    assert "fence_wait_elapsed_ms" not in receipt["selected"][0]
    jsonschema.validate(receipt, _load_schema())


@pytest.mark.parametrize(
    ("outcomes", "expected_rc", "expected_outcome"),
    [
        ({"busy": fence.FenceContended("hydro.river_timeseries", 1)}, 0, "deferred"),
        ({"busy": _DriverError("deadlock detected", "40P01")}, 1, "partial"),
    ],
    ids=["deferred-exits-0", "partial-exits-1"],
)
def test_main_exit_code_separates_contention_from_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, Any],
    expected_rc: int,
    expected_outcome: str,
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "c" * 40)
    monkeypatch.setattr(compression, "acquire_timeseries_lifecycle_lock", lambda: 99)
    monkeypatch.setattr(compression, "release_timeseries_lifecycle_lock", lambda _fd: None)

    rc = compression.main(
        ["--enforce"],
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [_chunk("busy", age_days=4)],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 10,
        compress_chunk=_compress_script(outcomes),
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )

    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert (rc, receipt["outcome"]) == (expected_rc, expected_outcome)
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Compression runner: tick-deadline guard
# ---------------------------------------------------------------------------


class _TickClock:
    """Scripted ``compression._monotonic``; records how often it was read."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self._readings.pop(0)


# Defaults: wrapper wall 3900 s, fence wait 900 s, cleanup margin 300 s, so a
# chunk may still start its fence at elapsed <= 2700 s (3900 - 900 - 300).
_LAST_FENCE_START_S = 3900 - 900 - 300


@pytest.mark.parametrize(
    ("second_check_s", "second_deferred"),
    [(_LAST_FENCE_START_S, False), (_LAST_FENCE_START_S + 0.001, True)],
    ids=["exactly-fits", "one-ms-over"],
)
def test_the_tick_deadline_guard_defers_the_rest_of_the_tick_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_check_s: float, second_deferred: bool
) -> None:
    config = compression.config_from_args(
        _args(enforce=True), _env(tmp_path, NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="3")
    )
    assert (config.wrapper_wall_seconds, config.fence_wait_ms) == (3900, 900_000)
    # Tick started at 100.0; chunk 1 is checked at +10 s and chunk 2 at
    # +second_check_s. Chunk 3 is checked at +3000 s if chunk 2 did not trip.
    clock = _TickClock(100.0 + 10, 100.0 + second_check_s, 100.0 + 3000)
    monkeypatch.setattr(compression, "_monotonic", clock)
    attempted: list[str] = []

    def compress(_dsn: str, chunk: Any) -> int:
        attempted.append(chunk.chunk_name)
        return 5

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [_chunk("c1", age_days=4), _chunk("c2", age_days=5), _chunk("c3", age_days=6)],
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 400 if after else 1000,
        compress_chunk=compress,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
        head_sha="b" * 40,
        tick_started_monotonic=100.0,
    )

    c1, c2, c3 = receipt["selected"]
    assert c1["mutation_state"] == "committed"
    # c3 is always past the deadline (+3000 s), and once tripped the guard
    # stops reading the clock: the rest of the tick is deferred, not re-tried.
    assert c3["mutation_state"] == "deferred_contended"
    if second_deferred:
        assert attempted == ["c1"]
        assert clock.reads == 2
        assert c2["mutation_state"] == "deferred_contended"
        assert receipt["deferred_contended_count"] == 2
    else:
        assert attempted == ["c1", "c2"]
        assert clock.reads == 3
        assert c2["mutation_state"] == "committed"
        assert receipt["deferred_contended_count"] == 1
    # A guard deferral carries no fence wait: that is what tells it apart from
    # an expired fence wait. Nothing it deferred counts toward the totals.
    assert "fence_wait_elapsed_ms" not in c3
    assert c3["before_bytes"] == 1000 and c3["after_bytes"] is None
    assert receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"] == len(attempted)
    assert receipt["outcome"] == "deferred"
    jsonschema.validate(receipt, _load_schema())


def test_the_guard_charges_a_smaller_fence_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With a 60 s fence wait the same +3000 s check still fits (3000 + 60 + 300 <= 3900).
    env = _env(tmp_path, **{_FENCE_WAIT_KEY: "60000"})
    config = compression.config_from_args(_args(enforce=True), env)
    monkeypatch.setattr(compression, "_monotonic", _TickClock(3000.0))
    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [_chunk("c1", age_days=4)],
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 400 if after else 1000,
        compress_chunk=lambda _dsn, _chunk: 5,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
        head_sha="b" * 40,
        tick_started_monotonic=0.0,
    )
    assert receipt["selected"][0]["mutation_state"] == "committed"
    assert receipt["outcome"] == "clean"


def test_dry_run_never_reads_the_tick_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = compression.config_from_args(_args(), _env(tmp_path))
    clock = _TickClock(0.0)
    monkeypatch.setattr(compression, "_monotonic", clock)
    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [_chunk("c1", age_days=4)],
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 1000,
        compress_chunk=lambda _dsn, _chunk: pytest.fail("dry-run must not compress"),
        head_sha="b" * 40,
    )
    assert clock.reads == 1  # only the default tick-start anchor
    assert receipt["outcome"] == "clean"


def test_main_anchors_the_tick_deadline_at_process_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # main reads the clock once on entry (0.0); the only chunk is checked at
    # +3000 s. Had build_receipt re-anchored at its own start, elapsed would
    # read ~0 and the chunk would be attempted.
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "c" * 40)
    monkeypatch.setattr(compression, "acquire_timeseries_lifecycle_lock", lambda: 99)
    monkeypatch.setattr(compression, "release_timeseries_lifecycle_lock", lambda _fd: None)
    clock = _TickClock(0.0, 3000.0)
    monkeypatch.setattr(compression, "_monotonic", clock)

    rc = compression.main(
        ["--enforce"],
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [_chunk("late", age_days=4)],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 10,
        compress_chunk=lambda _dsn, _chunk: pytest.fail("past the tick deadline: must not compress"),
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )

    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert (rc, receipt["outcome"], receipt["deferred_contended_count"]) == (0, "deferred", 1)
    assert receipt["selected"][0]["mutation_state"] == "deferred_contended"
    assert clock.reads == 2
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Receipt schema 2.2
# ---------------------------------------------------------------------------


def _example() -> dict:
    return json.loads((_ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text())


def _deferred_receipt() -> dict:
    receipt = _example()
    receipt.update(outcome="deferred", deferred_contended_count=1)
    receipt["selected"] = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_9_213_chunk",
            "range_start": "2026-09-28T00:00:00Z",
            "range_end": "2026-09-29T00:00:00Z",
            "before_bytes": 1,
            "after_bytes": None,
            "mutation_state": "deferred_contended",
            "fence_wait_elapsed_ms": 900000,
        }
    ]
    return receipt


def test_schema_2_2_example_and_deferred_receipt_validate() -> None:
    assert _example()["schema_version"] == "2.2"
    jsonschema.validate(_example(), _load_schema())
    jsonschema.validate(_deferred_receipt(), _load_schema())
    jsonschema.Draft7Validator(_load_schema()).validate(_deferred_receipt())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["budget"].pop("fence_wait_ms"),
        lambda r: r.pop("deferred_contended_count"),
        lambda r: r.update(deferred_contended_count=0),
        lambda r: r.update(mode="dry-run"),
        lambda r: r["budget"].update(fence_wait_ms=0),
        lambda r: r["selected"][0].update(fence_wait_elapsed_ms=-1),
    ],
    ids=[
        "2.2-budget-without-fence-wait",
        "2.2-without-count",
        "deferred-with-zero-count",
        "deferred-dry-run",
        "zero-fence-wait",
        "negative-elapsed",
    ],
)
def test_schema_2_2_rejects_an_incoherent_deferral(mutate: Any) -> None:
    receipt = _deferred_receipt()
    mutate(receipt)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


@pytest.mark.parametrize(
    ("version", "leak"),
    [
        (version, leak)
        for version in ("2.0", "2.1")
        for leak in ("outcome", "count", "fence_wait_ms", "mutation_state", "elapsed")
        # 2.0 carries no budget at all, so it has no fence_wait_ms to leak.
        if (version, leak) != ("2.0", "fence_wait_ms")
    ],
)
def test_pre_2_2_receipts_cannot_carry_fence_evidence(version: str, leak: str) -> None:
    receipt = _example()
    receipt.pop("deferred_contended_count")
    receipt["budget"].pop("fence_wait_ms")
    receipt["schema_version"] = version
    if version == "2.0":
        receipt.pop("budget")
    receipt["selected"] = [dict(_deferred_receipt()["selected"][0], mutation_state="committed", after_bytes=1)]
    receipt["selected"][0].pop("fence_wait_elapsed_ms")
    receipt["outcome"] = "clean"
    jsonschema.validate(receipt, _load_schema())  # the clean baseline is valid
    if leak == "outcome":
        receipt["outcome"] = "deferred"
    elif leak == "count":
        receipt["deferred_contended_count"] = 0
    elif leak == "fence_wait_ms":
        receipt["budget"]["fence_wait_ms"] = 900000
    elif leak == "mutation_state":
        receipt["selected"][0]["mutation_state"] = "deferred_contended"
    else:
        receipt["selected"][0]["fence_wait_elapsed_ms"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_2_2_failed_receipt_and_tombstone_carry_no_count() -> None:
    tombstone = {
        "schema_version": "2.2",
        "generated_at": "2026-10-04T12:00:00Z",
        "now_utc": "2026-10-04T12:00:00Z",
        "mode": "enforce",
        "outcome": "failed",
        "provenance_state": "unavailable",
        "failure": {"stage": "config", "mutation_state": "failed_before_mutation"},
    }
    jsonschema.validate(tombstone, _load_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**tombstone, "deferred_contended_count": 0}, _load_schema())


# ---------------------------------------------------------------------------
# Retention drop: same exclusive fence, 55P03 classification (design D2b)
# ---------------------------------------------------------------------------


def _retention_config(tmp_path: Path, *, lock_timeout_ms: int = 240_000) -> retention.RetentionConfig:
    return retention.RetentionConfig(
        database_url="postgresql://user:pw@127.0.0.1:55432/nhms",
        window_days=21,
        per_tick_bound=5,
        receipt_path=tmp_path / "receipt.json",
        lock_path=tmp_path / "runner.lock",
        enforce=True,
        lock_timeout_ms=lock_timeout_ms,
    )


def _retention_chunk(label: str) -> retention.ChunkRow:
    end = _NOW - timedelta(days=60)
    return retention.ChunkRow(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=end - timedelta(days=1),
        range_end=end,
        is_compressed=False,
    )


def test_the_drop_takes_the_fence_first_and_releases_it_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = _retention_chunk("_hyper_9_163_chunk")

    class _DropCursor(_RecordingCursor):
        def fetchall(self) -> list[Any]:
            return [(chunk.qualified_name,)]

    statements: list[tuple[str, Any]] = []
    connections: list[_RecordingConnection] = []

    def connect(*_args: object, **_kwargs: object) -> _RecordingConnection:
        connections.append(_RecordingConnection(lambda: _DropCursor(statements)))
        return connections[-1]

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    _fixed_clock(monkeypatch, 0.0, 0.0)

    retention._default_drop_chunk(_retention_config(tmp_path, lock_timeout_ms=1234), chunk)

    sql = [statement for statement, _params in statements]
    assert sql[:5] == [
        f"SET statement_timeout = {retention._DROP_TIMEOUT_MS}",
        "SET lock_timeout = 1234",
        "SELECT pg_advisory_lock(%s, %s)",
        "RESET lock_timeout",
        "SET lock_timeout = 1234",
    ]
    assert "SELECT drop_chunks(" in sql[5]
    assert statements[2][1] == (2713, -1964848284)
    assert statements[-1] == ("SELECT pg_advisory_unlock(%s, %s)", (2713, -1964848284))
    assert connections[0].exits == [None] and connections[0].closed == 1


def test_a_raising_drop_still_releases_the_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drop_error = _DriverError("deadlock detected", "40P01")

    class _RaisingDropCursor(_RecordingCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            super().execute(sql, params)
            if sql.startswith("SELECT drop_chunks("):
                raise drop_error

    statements: list[tuple[str, Any]] = []
    connections: list[_RecordingConnection] = []

    def connect(*_args: object, **_kwargs: object) -> _RecordingConnection:
        connections.append(_RecordingConnection(lambda: _RaisingDropCursor(statements)))
        return connections[-1]

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    _fixed_clock(monkeypatch, 0.0, 0.0)

    with pytest.raises(_DriverError) as excinfo:
        retention._default_drop_chunk(_retention_config(tmp_path), _retention_chunk("chk-a"))

    assert excinfo.value is drop_error
    assert statements[-1] == ("SELECT pg_advisory_unlock(%s, %s)", (2713, -1964848284))
    assert connections[0].exits == [_DriverError] and connections[0].closed == 1


def test_fence_contention_refuses_the_tick_as_lock_contention_55p03(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements, _connections = _install_driver(
        monkeypatch, lock_error=_DriverError("canceling statement due to lock timeout", "55P03")
    )
    _fixed_clock(monkeypatch, 0.0, 240.0)
    chunk = _retention_chunk("_hyper_9_163_chunk")

    receipt = retention.run_retention(
        _retention_config(tmp_path),
        _NOW,
        fetch_chunks=lambda _config, _cutoff: [chunk],
        measure_chunk_bytes=lambda _config, chunks: {row.qualified_name: 10 for row in chunks},
    )

    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == (
        "RETENTION_DROP_FAILED:hydro._hyper_9_163_chunk: lock-contention(55P03): "
        "compression fence on hydro.river_timeseries not acquired within the bounded wait "
        "(240000 ms): ingest writers still hold it"
    )
    assert not any("drop_chunks" in sql for sql, _ in statements)
