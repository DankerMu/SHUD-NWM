"""Unit tests for the node-27 timeseries compression runner (issue #851)."""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import inspect
import json
import os
import random
import re
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from packages.common import timeseries_compression_fence as fence
from packages.common.migrate import split_sql_statements
from scripts import node27_timeseries_compression as compression

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _ROOT / "schemas/timeseries_compression_receipt.schema.json"
_MIGRATION_PATH = _ROOT / "db/migrations/000047_hypertable_compression_settings.sql"
_RUNNER_SOURCE_PATH = _ROOT / "scripts/node27_timeseries_compression.py"
_SYSTEMD_SERVICE_PATH = _ROOT / "infra/systemd/nhms-node27-timeseries-compression.service"
_SYSTEMD_REPLAY_SERVICE_PATH = _ROOT / "infra/systemd/nhms-node27-timeseries-compression-replay.service"
_SYSTEMD_TIMER_PATH = _ROOT / "infra/systemd/nhms-node27-timeseries-compression.timer"
_ENV_EXAMPLE_PATH = _ROOT / "infra/env/node27-timeseries-compression.example"

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {"enforce": False, "receipt_path": None, "lock_path": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _base_env(tmp_path: Path, *, override: dict[str, str | None] | None = None) -> dict[str, str]:
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "5",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    if override:
        for k, v in override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return env


def _chunk(
    schema_name: str,
    hyper: str,
    label: str,
    *,
    now: datetime = _NOW,
    delta_days: float,
) -> compression.ChunkRow:
    end = now - timedelta(days=delta_days)
    start = end - timedelta(days=7)
    return compression.ChunkRow(
        hypertable_schema=schema_name,
        hypertable_name=hyper,
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=start,
        range_end=end,
        is_compressed=False,
    )


# ---------------------------------------------------------------------------
# Config parse fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": ""}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "0"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "-1"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "not-a-number"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": None}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "0"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": ""}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "-3"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": None}, "PER_TICK_BOUND"),
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": None}, "receipt path"),
        ({"NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": None}, "lock path"),
        ({"NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": "relative/receipt.json"}, "absolute"),
        ({"NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": "relative.lock"}, "absolute"),
    ],
)
def test_config_parse_fails_closed(tmp_path: Path, override: dict[str, str | None], match: str) -> None:
    env = _base_env(tmp_path, override=override)
    with pytest.raises(compression.CompressionConfigError, match=match):
        compression.config_from_args(_args(), env)


def test_config_parse_happy_path(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(), env)
    assert config.lag_seconds == 604800
    assert config.per_tick_bound == 5
    assert config.enforce is False
    assert config.database_url.startswith("postgresql://")


def test_compress_timeout_preserves_outer_cleanup_budgets(tmp_path: Path) -> None:
    """B1: with no budget env set, the three fields are today's literals.

    The runner's timeout budget chain became operator-configurable in #1156;
    #1352 resized the defaults for the 17-basin dual-source régime, where
    weekly hydro.river_timeseries chunks are 268-409 GB and compress at a
    measured ~6.0 s/GB. This pins today's literals and pins that both legs of
    the invariant hold on them.
    """
    config = compression.config_from_args(_args(), _base_env(tmp_path))

    assert config.compress_timeout_ms == 3_600_000
    assert config.wrapper_wall_seconds == 3_900
    assert config.systemd_wall_seconds == 3_941
    from packages.common.node27_timeseries_compression_budget import COMPRESSION_CLEANUP_MARGIN_SECONDS

    assert COMPRESSION_CLEANUP_MARGIN_SECONDS == 300
    assert compression._CLEANUP_MARGIN_SECONDS is COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert compression._SYSTEMD_MARGIN_SECONDS == 40
    # The exact admission boundary is the shared 300-second cleanup contract.
    assert config.wrapper_wall_seconds == 3_600 + COMPRESSION_CLEANUP_MARGIN_SECONDS
    # Leg 2: service wall must sit strictly above wrapper + 40 s systemd margin.
    # Equality at wrapper+40 is refused; the committed default is 3941.
    assert config.wrapper_wall_seconds + compression._SYSTEMD_MARGIN_SECONDS < config.systemd_wall_seconds
    assert config.systemd_wall_seconds == 3_900 + 40 + 1


@pytest.mark.parametrize(
    ("variable", "raw", "match"),
    [
        ("NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS", "0", "must be a canonical positive integer"),
        ("NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS", "-1", "must be a canonical positive integer"),
        ("NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS", "abc", "must be a canonical positive integer"),
        ("NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS", "999", "must be >= 1000"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "0", "must be a canonical positive integer"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "abc", "must be a canonical positive integer"),
        ("NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS", "abc", "must be a canonical positive integer"),
    ],
)
def test_budget_chain_variables_parse_fail_closed(tmp_path: Path, variable: str, raw: str, match: str) -> None:
    """B4: anything present but not a positive integer at/above the minimum is refused."""
    env = _base_env(tmp_path, override={variable: raw})
    with pytest.raises(compression.CompressionConfigError, match=match):
        compression.config_from_args(_args(), env)


def test_empty_budget_chain_variables_take_the_defaults(tmp_path: Path) -> None:
    """B4: empty string means "unset" — same as the wrapper's ``${VAR:-3900}``."""
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "",
            "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "",
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "",
        },
    )
    config = compression.config_from_args(_args(), env)
    assert (config.compress_timeout_ms, config.wrapper_wall_seconds, config.systemd_wall_seconds) == (
        3_600_000,
        3_900,
        3_941,
    )


class _FakeCursor:
    """Minimal psycopg2 cursor recording every statement it is handed."""

    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self._statements = statements

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._statements.append((sql, params))

    def fetchone(self) -> tuple[object, ...]:
        return (None,)

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self._statements = statements
        self.closed = False

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def cursor(self, *_args: object, **_kwargs: object) -> _FakeCursor:
        return _FakeCursor(self._statements)

    def set_session(self, **_kwargs: object) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _install_fake_psycopg2(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[tuple, dict]], list[tuple[str, object]]]:
    """Route every ``import psycopg2`` at call time to a recording double.

    ``connect_calls`` is the DB-touch witness: a configuration that fails
    closed before any database call leaves it empty.
    """
    connect_calls: list[tuple[tuple, dict]] = []
    statements: list[tuple[str, object]] = []

    def connect(*args: object, **kwargs: object) -> _FakeConnection:
        connect_calls.append((args, kwargs))
        return _FakeConnection(statements)

    module = types.ModuleType("psycopg2")
    module.connect = connect  # type: ignore[attr-defined]
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object  # type: ignore[attr-defined]
    module.extras = extras  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    return connect_calls, statements


def test_main_enforce_propagates_the_configured_compress_timeout_to_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: the override reaches the real ``SET statement_timeout`` end to end.

    Deliberately runs ``--enforce`` with NO injected ``compress_chunk``: the
    dry-run path never reaches the compression hook, and injecting a fake hook
    would step over the very assembly point (``functools.partial`` binding
    ``config.compress_timeout_ms`` and ``config.fence_wait_ms``) this pins.

    #2713: the fence comes first in the session, its 250 ms (faked clock)
    wait is charged against the override, and the fence is released after
    the compress transaction.
    """
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "1800000",
            "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "5",
            "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect_calls, statements = _install_fake_psycopg2(monkeypatch)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: _NOW)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    clock = iter([100.0, 100.25])
    monkeypatch.setattr(fence, "_monotonic", lambda: next(clock))
    chunk = _chunk("hydro", "river_timeseries", "oversized", delta_days=9)

    code = compression.main(
        ["--enforce"],
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 100,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )

    assert code == 0
    assert statements == [
        ("SET statement_timeout = 1800000", None),
        ("SET lock_timeout = 900000", None),
        ("SELECT pg_advisory_lock(%s, %s)", (2713, -1964848284)),
        ("RESET lock_timeout", None),
        ("SET statement_timeout = 1799750", None),
        ("SET lock_timeout = 5000", None),
        ("SELECT compress_chunk(%s::regclass)", ("_timescaledb_internal.oversized",)),
        ("SELECT pg_advisory_unlock(%s, %s)", (2713, -1964848284)),
    ]
    assert len(connect_calls) == 1
    receipt = json.loads(Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"]).read_text())
    assert receipt["outcome"] == "clean"


@pytest.mark.parametrize(
    ("compress_ms", "wall", "systemd", "reason_fragment", "numbers"),
    [
        # A wrapper below the 300-second operational cleanup floor is refused.
        ("3600000", "3800", "3941", "compression wrapper wall", ("3800", "3900")),
        # Equality at wrapper+40 is refused; the service wall must sit strictly above.
        ("3600000", "3900", "3940", "service wall", ("3940", "3940")),
        # A raised statement ceiling cannot fit the actual wrapper plus cleanup margin.
        ("4200000", "3900", "3941", "cleanup margin", ("3900", "4500")),
    ],
)
def test_main_rejects_a_budget_chain_violation_before_any_db_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    compress_ms: str,
    wall: str,
    systemd: str,
    reason_fragment: str,
    numbers: tuple[str, ...],
) -> None:
    """B3: fail closed, name the violated compression budget, touch no DB.

    ``fetch_display_watermark`` is intentionally NOT patched: it opens its own
    connection, so a configuration that wrongly slipped through would show up
    in ``connect_calls``.
    """
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": compress_ms,
            "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": wall,
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": systemd,
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect_calls, _statements = _install_fake_psycopg2(monkeypatch)

    code = compression.main(["--enforce"])

    assert code != 0
    assert connect_calls == []
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["status"] == "failed"
    assert reason_fragment in diagnostic["reason"]
    for number in numbers:
        assert number in diagnostic["reason"]


def test_main_accepts_the_exact_budget_chain_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3: the assembly-owned defaults are not rejected."""
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
            "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect_calls, _statements = _install_fake_psycopg2(monkeypatch)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: _NOW)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    chunk = _chunk("hydro", "river_timeseries", "at-the-boundary", delta_days=9)

    code = compression.main(
        [],
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 100,
        compress_chunk=lambda _dsn, _chunk: None,
    )

    assert code == 0
    assert connect_calls == []


def test_receipt_and_lock_alias_is_rejected(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(shared),
            "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(shared),
        },
    )
    with pytest.raises(compression.CompressionConfigError, match="aliases"):
        compression.config_from_args(_args(), env)


def test_unsafe_early_receipt_symlink_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"outcome":"stale-clean"}\n', encoding="utf-8")
    link = tmp_path / "receipt.json"
    link.symlink_to(target)
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(link),
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert compression.main([]) == 1
    assert target.read_text(encoding="utf-8") == '{"outcome":"stale-clean"}\n'


def test_invalid_config_does_not_replace_receipt_that_is_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "receipt-and-lock"
    original = b'{"outcome":"input-must-remain"}\n'
    shared.write_bytes(original)
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(shared),
            "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(shared),
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert compression.main([]) == 1
    assert shared.read_bytes() == original


def test_invalid_config_does_not_replace_receipt_hardlinked_to_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "runner.lock"
    receipt_path = tmp_path / "receipt.json"
    original = b'{"outcome":"input-must-remain"}\n'
    lock_path.write_bytes(original)
    os.link(lock_path, receipt_path)
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(receipt_path),
            "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(lock_path),
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert compression.main([]) == 1
    assert receipt_path.read_bytes() == original
    assert lock_path.read_bytes() == original


def test_invalid_config_replaces_disjoint_known_safe_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"outcome":"stale-clean"}\n', encoding="utf-8")
    env = _base_env(
        tmp_path,
        override={"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad"},
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert compression.main([]) == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "2.3"
    assert receipt["outcome"] == "failed"
    assert receipt["failure"] == {
        "stage": "config",
        "mutation_state": "failed_before_mutation",
    }


@pytest.mark.parametrize("missing_parent", [False, True])
def test_invalid_config_missing_receipt_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_parent: bool
) -> None:
    receipt_path = tmp_path / ("missing/receipt.json" if missing_parent else "receipt.json")
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(receipt_path),
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad",
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert compression.main([]) == 1
    assert not receipt_path.exists()
    if missing_parent:
        assert not receipt_path.parent.exists()


def test_catalog_discovery_uses_bounded_server_cursor_and_limit() -> None:
    source = inspect.getsource(compression._default_fetch_chunks)
    assert "cursor(name=cursor_name" in source
    assert "LIMIT %s" in source
    assert "min(_MAX_CATALOG_ROWS, _MAX_CANDIDATES) + 1" in source
    assert "fetchmany(1000)" in source


def test_catalog_over_limit_replaces_known_stale_with_failed_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"])
    receipt_path.write_text('{"outcome":"stale-clean"}\n')
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)

    def over_limit(_dsn: str) -> list[compression.ChunkRow]:
        raise compression.CompressionConfigError("catalog discovery exceeds the row/candidate ceiling")

    assert compression.main([], now_utc=_NOW, fetch_chunks=over_limit) == 1
    marker = json.loads(receipt_path.read_text())
    assert marker["failure"] == {
        "stage": "runner",
        "mutation_state": "failed_before_mutation",
    }


def test_schema_rejects_v2_failure_labeled_as_v1(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(enforce=True), _base_env(tmp_path))
    marker = compression.build_failed_receipt(
        config,
        now_utc=_NOW,
        stage="freeze_head",
        head_sha=None,
    )
    marker["schema_version"] = "1.0"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(marker, _load_schema())


# ---------------------------------------------------------------------------
# Chunk classification
# ---------------------------------------------------------------------------


def test_classify_partitions_by_lag_window() -> None:
    inside = _chunk("hydro", "river_timeseries", "recent", delta_days=3)
    outside = _chunk("hydro", "river_timeseries", "old", delta_days=10)
    selected, deferred, skipped = compression._classify(
        [inside, outside], now_utc=_NOW, lag_seconds=7 * 86400, per_tick_bound=5
    )
    assert [c.chunk_name for c in selected] == ["old"]
    assert deferred == []
    assert [c.chunk_name for c in skipped] == ["recent"]


def test_classify_respects_per_tick_bound() -> None:
    chunks = [_chunk("hydro", "river_timeseries", f"c{i:02d}", delta_days=30 - i) for i in range(8)]
    selected, deferred, skipped = compression._classify(chunks, now_utc=_NOW, lag_seconds=7 * 86400, per_tick_bound=3)
    assert len(selected) == 3
    assert len(deferred) == 5
    assert skipped == []
    # Ordering (#2425): one hypertable, walked newest range_end first; the
    # input is oldest-first (c00 oldest), so selected + deferred is its reverse.
    assert [c.chunk_name for c in selected] == ["c07", "c06", "c05"]
    assert [c.chunk_name for c in selected + deferred] == [c.chunk_name for c in reversed(chunks)]


# #2425: compression and DB retention derive their cutoffs from ONE display
# watermark W: compression lag 2 d, retention window 21 d. Both were the
# production values when #2425 landed (node-27 runs a 30 d window since #3106,
# 2026-11-09); the fixture keeps 21 because only window > lag matters to the
# disjointness it pins. Spelled out here instead of read from either runner.
_RETENTION_WINDOW = timedelta(days=21)
_LAG_SECONDS = 2 * 86400


def _day_chunk(schema_name: str, hyper: str, label: str, *, range_end: datetime) -> compression.ChunkRow:
    return compression.ChunkRow(
        hypertable_schema=schema_name,
        hypertable_name=hyper,
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=range_end - timedelta(days=1),
        range_end=range_end,
        is_compressed=False,
    )


def _river_backlog(watermark: datetime) -> list[compression.ChunkRow]:
    """Day chunks ending W-25d .. W-3d, catalog order (range_end ASC).

    Five of them (W-25d .. W-21d) are inside the retention drop set
    `range_end <= W - 21 d`; the rest are younger, so at least `bound` eligible
    chunks survive retention -- the spec's overlap precondition.
    """
    return [
        _day_chunk("hydro", "river_timeseries", f"river-d{age:02d}", range_end=watermark - timedelta(days=age))
        for age in range(25, 2, -1)
    ]


def test_classify_selection_is_disjoint_from_the_retention_drop_set() -> None:
    """Spec scenario "compression's oldest end overlaps retention's drop set"."""
    chunks = _river_backlog(_NOW)
    retention_cutoff = _NOW - _RETENTION_WINDOW
    retention_drop_set = {c.chunk_name for c in chunks if c.range_end <= retention_cutoff}
    assert len(retention_drop_set) == 5, "fixture must straddle the retention cutoff"

    selected, deferred, skipped = compression._classify(
        chunks, now_utc=_NOW, lag_seconds=_LAG_SECONDS, per_tick_bound=2
    )

    assert [c.chunk_name for c in selected] == ["river-d03", "river-d04"]
    assert all(c.range_end > retention_cutoff for c in selected)
    assert {c.chunk_name for c in selected}.isdisjoint(retention_drop_set)
    # The drop set is what is left at the far end of the deferred sequence.
    assert [c.chunk_name for c in deferred[-5:]] == [
        "river-d21",
        "river-d22",
        "river-d23",
        "river-d24",
        "river-d25",
    ]
    assert skipped == []


def test_classify_keeps_table_order_across_hypertables() -> None:
    """Spec scenario "table order is preserved across hypertables"."""
    river = _river_backlog(_NOW)
    legacy = [
        _day_chunk("hydro", "river_timeseries_legacy", "legacy-old", range_end=_NOW - timedelta(days=40)),
        _day_chunk("hydro", "river_timeseries_legacy", "legacy-new", range_end=_NOW - timedelta(days=30)),
    ]
    forcing = [_day_chunk("met", "forcing_station_timeseries", "forcing", range_end=_NOW - timedelta(days=9))]

    selected, deferred, _skipped = compression._classify(
        river + legacy + forcing, now_utc=_NOW, lag_seconds=_LAG_SECONDS, per_tick_bound=2
    )

    assert {(c.hypertable_schema, c.hypertable_name) for c in selected} == {("hydro", "river_timeseries")}
    assert [c.chunk_name for c in selected] == ["river-d03", "river-d04"]
    # Deferred: rest of the river table newest-first, then legacy newest-first,
    # then met -- the table order of the input, never interleaved.
    assert [c.chunk_name for c in deferred][-3:] == ["legacy-new", "legacy-old", "forcing"]
    assert [c.chunk_name for c in deferred][:-3] == [f"river-d{age:02d}" for age in range(5, 26)]


def test_classify_moves_a_free_slot_to_the_next_hypertable_newest_first() -> None:
    """Spec scenario "a free slot moves to the next hypertable"."""
    chunks = [
        _day_chunk("hydro", "river_timeseries", "river-only", range_end=_NOW - timedelta(days=3)),
        _day_chunk("hydro", "river_timeseries", "river-inside-lag", range_end=_NOW - timedelta(days=1)),
        _day_chunk("hydro", "river_timeseries_legacy", "legacy-old", range_end=_NOW - timedelta(days=40)),
        _day_chunk("hydro", "river_timeseries_legacy", "legacy-new", range_end=_NOW - timedelta(days=30)),
        _day_chunk("met", "forcing_station_timeseries", "forcing", range_end=_NOW - timedelta(days=9)),
    ]

    selected, deferred, skipped = compression._classify(
        chunks, now_utc=_NOW, lag_seconds=_LAG_SECONDS, per_tick_bound=2
    )

    assert [c.chunk_name for c in selected] == ["river-only", "legacy-new"]
    assert [c.chunk_name for c in deferred] == ["legacy-old", "forcing"]
    assert [c.chunk_name for c in skipped] == ["river-inside-lag"]


def test_classify_order_is_deterministic_for_a_shuffled_catalog() -> None:
    """The within-table order is a sort on range_end, not a reversal of input.

    Table order follows first appearance, so each shuffle is regrouped by
    table first; within that, any permutation must yield the same sequence.
    """
    river = _river_backlog(_NOW)
    forcing = [
        _day_chunk("met", "forcing_station_timeseries", f"forcing-d{age:02d}", range_end=_NOW - timedelta(days=age))
        for age in (20, 13, 6)
    ]
    inside_lag = [
        _day_chunk("hydro", "river_timeseries", "lag-a", range_end=_NOW - timedelta(days=1)),
        _day_chunk("met", "forcing_station_timeseries", "lag-b", range_end=_NOW),
    ]
    expected_selected = ["river-d03", "river-d04"]
    expected_deferred = [f"river-d{age:02d}" for age in range(5, 26)] + ["forcing-d06", "forcing-d13", "forcing-d20"]

    rng = random.Random(2425)
    for _ in range(20):
        shuffled_river = list(river)
        shuffled_forcing = list(forcing)
        rng.shuffle(shuffled_river)
        rng.shuffle(shuffled_forcing)
        catalog = shuffled_river + inside_lag + shuffled_forcing
        selected, deferred, skipped = compression._classify(
            catalog, now_utc=_NOW, lag_seconds=_LAG_SECONDS, per_tick_bound=2
        )
        assert [c.chunk_name for c in selected] == expected_selected
        assert [c.chunk_name for c in deferred] == expected_deferred
        # Inside-lag chunks keep input order.
        assert [c.chunk_name for c in skipped] == ["lag-a", "lag-b"]


def test_main_uses_display_watermark_as_compression_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reference_time = datetime(2026, 7, 11, 12, tzinfo=UTC)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: reference_time)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    chunk = _chunk(
        "hydro", "river_timeseries", "watermark-old", now=reference_time, delta_days=8
    )

    code = compression.main(
        [],
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 100,
        compress_chunk=lambda _dsn, _chunk: None,
    )

    assert code == 0
    receipt = json.loads(Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"]).read_text())
    assert receipt["now_utc"] == "2026-07-11T12:00:00Z"
    assert [item["chunk_name"] for item in receipt["selected"]] == ["watermark-old"]


# ---------------------------------------------------------------------------
# Dry-run vs enforce
# ---------------------------------------------------------------------------


# Byte-size shape for the shared stubs. before returns 1 GiB per chunk, after
# returns 512 MiB — the delta proves the runner routes ``after=True`` to a
# distinct catalog lookup (cand-A). Tests that don't distinguish should assert
# on ``before_bytes`` only.
_STUB_BEFORE_BYTES = 1_073_741_824  # 1 GiB
_STUB_AFTER_BYTES = 536_870_912  # 512 MiB


def _install_stubs(monkeypatch: pytest.MonkeyPatch, *, chunks: list[compression.ChunkRow]) -> dict[str, list]:
    calls: dict[str, list] = {"compress": [], "measure": [], "measure_after": []}

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after:
            calls["measure_after"].append(chunk.chunk_name)
            return _STUB_AFTER_BYTES
        calls["measure"].append(chunk.chunk_name)
        return _STUB_BEFORE_BYTES

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        calls["compress"].append(chunk.chunk_name)

    return calls, fake_fetch, fake_measure, fake_compress


def test_dry_run_never_compresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=False), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "old-1", delta_days=10),
        _chunk("met", "forcing_station_timeseries", "old-2", delta_days=12),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    assert calls["compress"] == []
    assert receipt["mode"] == "dry-run"
    for descriptor in receipt["selected"]:
        assert descriptor["after_bytes"] is None
    assert receipt["outcome"] == "clean"


def test_enforce_calls_compress_for_each_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "old-a", delta_days=10),
        _chunk("hydro", "river_timeseries", "old-b", delta_days=11),
        _chunk("met", "forcing_station_timeseries", "old-c", delta_days=12),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    assert calls["compress"] == ["old-a", "old-b", "old-c"]
    # cand-A: the second measure call must be routed with after=True so that
    # the default DB implementation would query the compressed sibling
    # relation instead of the truncated origin chunk.
    assert calls["measure_after"] == ["old-a", "old-b", "old-c"]
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    forcing = receipt["per_table_totals"]["met.forcing_station_timeseries"]
    assert river["chunks_compressed"] == 2
    assert forcing["chunks_compressed"] == 1
    assert river["before_bytes"] == 2 * _STUB_BEFORE_BYTES
    assert forcing["before_bytes"] == _STUB_BEFORE_BYTES
    # after_bytes reflects the (fake) compressed footprint, not the origin.
    assert river["after_bytes"] == 2 * _STUB_AFTER_BYTES
    assert forcing["after_bytes"] == _STUB_AFTER_BYTES
    assert river["after_bytes"] < river["before_bytes"]
    assert forcing["after_bytes"] < forcing["before_bytes"]
    # And each per-chunk descriptor must carry the compressed after size.
    for descriptor in receipt["selected"]:
        assert descriptor["after_bytes"] == _STUB_AFTER_BYTES
        assert descriptor["before_bytes"] == _STUB_BEFORE_BYTES
    assert receipt["outcome"] == "clean"


def test_per_chunk_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Universal invariant (R3-01): compress-fail on chunk B in one hypertable.

    B did not reach compressed state, so it must contribute NOTHING to
    ``chunks_compressed`` or ``before_bytes`` (round-3 closure — including
    B's before while excluding it from chunks_compressed would inflate the
    (before-after)/before savings ratio a consumer computes). And it must
    poison ``after_bytes`` to null even though sibling A succeeded end-to-end.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "bad", delta_days=11),
    ]

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        return 100

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        if chunk.chunk_name == "bad":
            raise RuntimeError("simulated compress_chunk failure")

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    assert "error" not in by_name["good"]
    assert by_name["good"]["after_bytes"] == 100
    assert "error" in by_name["bad"]
    assert by_name["bad"]["error"] == "compress_chunk before mutation failed (RuntimeError)"
    assert by_name["bad"]["after_bytes"] is None
    assert receipt["outcome"] == "partial"


def test_lost_commit_ack_reconciles_to_committed_truth(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(enforce=True), _base_env(tmp_path))
    chunk = _chunk("hydro", "river_timeseries", "ack-lost", delta_days=10)

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 40 if after else 100,
        compress_chunk=lambda _dsn, _chunk: (_ for _ in ()).throw(RuntimeError("lost ack")),
        reconcile_chunk_state=lambda _dsn, _chunk: True,
    )

    selected = receipt["selected"][0]
    assert selected["mutation_state"] == "committed"
    assert selected["after_bytes"] == 40
    assert receipt["outcome"] == "partial"
    assert receipt["per_table_totals"]["hydro.river_timeseries"] == {
        "before_bytes": 100,
        "after_bytes": 40,
        "chunks_compressed": 1,
    }


def test_lost_commit_ack_with_unavailable_reconciliation_is_indeterminate(
    tmp_path: Path,
) -> None:
    config = compression.config_from_args(_args(enforce=True), _base_env(tmp_path))
    chunk = _chunk("hydro", "river_timeseries", "unknown", delta_days=10)

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 100,
        compress_chunk=lambda _dsn, _chunk: (_ for _ in ()).throw(RuntimeError("lost ack")),
        reconcile_chunk_state=lambda _dsn, _chunk: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )

    assert receipt["selected"][0]["mutation_state"] == "indeterminate"
    assert receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"] == 0


def test_known_failure_destination_atomically_replaces_stale_success(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(enforce=True), _base_env(tmp_path))
    config.receipt_path.write_text('{"outcome":"clean"}\n', encoding="utf-8")

    compression._replace_stale_with_failure(
        config,
        now_utc=_NOW,
        stage="runner",
        head_sha="a" * 40,
        mutation_state="indeterminate",
    )

    receipt = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert receipt["failure"] == {
        "stage": "runner",
        "mutation_state": "indeterminate",
    }
    jsonschema.validate(receipt, _load_schema())


def test_measure_after_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cand-F: after-measurement failure MUST not cascade to other chunks."""
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "measure-failure", delta_days=11),
    ]

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after and chunk.chunk_name == "measure-failure":
            raise RuntimeError("simulated pg_total_relation_size timeout")
        return 200 if not after else 50

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        return None

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    assert "error" not in by_name["good"]
    assert by_name["good"]["after_bytes"] == 50
    assert by_name["measure-failure"]["after_bytes"] is None
    assert "measure_chunk_bytes(after) failed" in by_name["measure-failure"]["error"]
    assert receipt["outcome"] == "partial"
    # The compressed chunk still counts in the roll-up so operators can see
    # that the mutation happened, even though after_bytes is unknown.
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    assert river["chunks_compressed"] == 2
    assert river["before_bytes"] == 200 + 200
    # cand-H: mixed success + after-fail in the same table MUST poison
    # per-table after_bytes to null — a partial sum over successful chunks
    # would mislead the (before-after)/before savings computation.
    assert river["after_bytes"] is None
    # F-INVSTATE-02: partial-outcome receipts stay schema-conformant.
    jsonschema.validate(receipt, _load_schema())


def test_after_fail_poisons_per_table_after_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cand-H: mixed success + after-fail in one hypertable nulls after_bytes.

    Constructs two chunks in ``hydro.river_timeseries``: A succeeds with a
    real after measurement, B fails on after-measure. The per-table roll-up
    MUST show ``chunks_compressed=2`` and ``before_bytes = A_before + B_before``
    but ``after_bytes = None`` (poisoned by the invariant), so a downstream
    consumer cannot compute an inflated savings ratio from the partial sum.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunk_a = _chunk("hydro", "river_timeseries", "mixed-A", delta_days=10)
    chunk_b = _chunk("hydro", "river_timeseries", "mixed-B", delta_days=11)

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return [chunk_a, chunk_b]

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after and chunk.chunk_name == "mixed-B":
            raise RuntimeError("simulated after-measure failure on mixed-B")
        if not after:
            return 400 if chunk.chunk_name == "mixed-A" else 800
        # after=True, success (chunk_a only)
        return 100

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        return None

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    assert river["chunks_compressed"] == 2
    assert river["before_bytes"] == 400 + 800
    assert river["after_bytes"] is None
    assert receipt["outcome"] == "partial"
    # F-INVSTATE-02: partial-outcome receipts stay schema-conformant.
    jsonschema.validate(receipt, _load_schema())


def test_compress_fail_poisons_per_table_after_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-3 R3-01: mixed success + compress-fail in one hypertable.

    Direct analog of ``test_after_fail_poisons_per_table_after_bytes`` but for
    the compress_chunk failure path. Two chunks in ``hydro.river_timeseries``:
    A (700 before, 200 after) succeeds; B fails inside compress_chunk. The
    per-table roll-up MUST show:
      * ``chunks_compressed = 1`` — B never reached compressed state
      * ``before_bytes = 700`` — B's before is NOT summed (round-3 closure)
      * ``after_bytes = None`` — poisoned by B's failure
    so a downstream consumer computing ``(before-after)/before`` sees either
    the true savings from A alone or nothing, never an inflated hybrid.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunk_a = _chunk("hydro", "river_timeseries", "mixed-A", delta_days=10)
    chunk_b = _chunk("hydro", "river_timeseries", "mixed-B", delta_days=11)

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return [chunk_a, chunk_b]

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if not after:
            return 700 if chunk.chunk_name == "mixed-A" else 900
        # after=True path — only reached for A.
        return 200

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        if chunk.chunk_name == "mixed-B":
            raise RuntimeError("simulated compress_chunk failure on mixed-B")

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    assert river["chunks_compressed"] == 1
    assert river["before_bytes"] == 700
    assert river["after_bytes"] is None
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    assert by_name["mixed-A"]["after_bytes"] == 200
    assert by_name["mixed-B"]["after_bytes"] is None
    assert by_name["mixed-B"]["error"] == "compress_chunk before mutation failed (RuntimeError)"
    assert receipt["outcome"] == "partial"
    # F-INVSTATE-02: partial-outcome receipts stay schema-conformant.
    jsonschema.validate(receipt, _load_schema())


def test_measure_before_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Universal invariant: before-fail MUST not cascade AND MUST poison after_bytes.

    The failed chunk never reached compressed state, so it contributes nothing
    to per-table ``chunks_compressed`` or ``before_bytes``. But the failure
    still poisons per-table ``after_bytes`` to null so a successful sibling
    cannot silently masquerade as the whole table's savings.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "before-fail", delta_days=11),
    ]
    compressed_names: list[str] = []

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if not after and chunk.chunk_name == "before-fail":
            raise RuntimeError("simulated catalog probe error")
        return 300 if not after else 60

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        compressed_names.append(chunk.chunk_name)

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    # Failing chunk carries the error and never entered compress_chunk.
    assert "error" in by_name["before-fail"]
    assert "measure_chunk_bytes(before) failed" in by_name["before-fail"]["error"]
    assert by_name["before-fail"]["before_bytes"] == 0
    assert by_name["before-fail"]["after_bytes"] is None
    assert compressed_names == ["good"]
    assert receipt["outcome"] == "partial"
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    # Only "good" reached compressed state; "before-fail" contributes nothing.
    assert river["chunks_compressed"] == 1
    assert river["before_bytes"] == 300
    # "before-fail" poisons the whole table's after_bytes.
    assert river["after_bytes"] is None
    # F-INVSTATE-02: partial-outcome receipts stay schema-conformant.
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Universal invariant regression (round-3 closure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_path", ["before-fail", "compress-fail", "after-fail"])
def test_partial_receipt_totals_stay_locked_across_all_failure_paths(tmp_path: Path, failure_path: str) -> None:
    """One high-level invariant test across all three per-chunk failure paths.

    For every failure path (before-measurement, compress_chunk, after-measurement)
    in a hypertable with one success + one failure, the per-table roll-up must
    satisfy the universal invariant:

    * ``before_bytes`` = sum(descriptor.before for d in selected where
      d has NO error, OR d.error starts with "measure_chunk_bytes(after)")
      — chunks that never reached the compressed state contribute nothing.
    * ``after_bytes`` is None — any failure on any chunk in the table poisons
      the roll-up so a partial sum can never masquerade as the whole table's
      compressed footprint.
    * ``chunks_compressed`` = count of selected with (no error OR after-measure
      error) — matches the "reached compressed state" set.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    success = _chunk("hydro", "river_timeseries", "success", delta_days=10)
    victim = _chunk("hydro", "river_timeseries", "victim", delta_days=11)

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return [success, victim]

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if not after:
            if failure_path == "before-fail" and chunk.chunk_name == "victim":
                raise RuntimeError("simulated before-measure failure")
            return 500
        # after=True
        if failure_path == "after-fail" and chunk.chunk_name == "victim":
            raise RuntimeError("simulated after-measure failure")
        return 120

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        if failure_path == "compress-fail" and chunk.chunk_name == "victim":
            raise RuntimeError("simulated compress_chunk failure")

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    river = receipt["per_table_totals"]["hydro.river_timeseries"]

    # Compute the invariant expectations from the descriptor stream itself, so
    # the assertion is spec-shape rather than a per-path magic number.
    def _reached_compressed(descriptor: dict) -> bool:
        error = descriptor.get("error")
        return error is None or error.startswith("measure_chunk_bytes(after)")

    reached = [d for d in receipt["selected"] if _reached_compressed(d)]
    expected_before = sum(int(d["before_bytes"]) for d in reached)
    expected_chunks_compressed = len(reached)

    assert river["before_bytes"] == expected_before
    assert river["chunks_compressed"] == expected_chunks_compressed
    # All three failure paths poison the whole-table after_bytes.
    assert river["after_bytes"] is None
    assert receipt["outcome"] == "partial"
    # Sanity: exactly one victim chunk carries an error, the other does not.
    errors = [d.get("error") for d in receipt["selected"] if d.get("error")]
    assert len(errors) == 1
    # Schema still holds for partial-outcome receipts.
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Lock contention
# ---------------------------------------------------------------------------


def test_main_publishes_refused_lock_receipt_without_db_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    # Pre-hold the lock in the same process.
    lock_path = Path(env["NODE27_TIMESERIES_COMPRESSION_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt_path = Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"])
    receipt_path.write_text('{"outcome":"stale-clean"}\n', encoding="utf-8")
    receipt_path.chmod(0o600)
    db_calls: list[str] = []

    def fail_fetch(dsn: str) -> list[compression.ChunkRow]:
        db_calls.append(dsn)
        raise AssertionError("lock contender must not call the database")

    try:
        code = compression.main(argv=[], now_utc=_NOW, fetch_chunks=fail_fetch)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert code == 0
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["status"] == "refused_lock"
    assert diagnostic["reason"] == "lock-contended"
    assert "secretpw" not in json.dumps(diagnostic)
    assert db_calls == []
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refused_lock"
    assert receipt["selected"] == receipt["deferred"] == receipt["skipped"] == []
    assert receipt["per_table_totals"] == {
        "hydro.river_timeseries": {
            "before_bytes": 0,
            "after_bytes": None,
            "chunks_compressed": 0,
        },
        "met.forcing_station_timeseries": {
            "before_bytes": 0,
            "after_bytes": None,
            "chunks_compressed": 0,
        },
    }
    jsonschema.validate(receipt, _load_schema())
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_lifecycle_lock_env_override_is_refused_before_any_lock(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        override={"NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": str(tmp_path / "split.lock")},
    )
    with pytest.raises(compression.CompressionConfigError, match="cannot override"):
        compression.config_from_args(_args(), env)


def test_lifecycle_contention_leaves_the_mutex_file_and_skips_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from packages.common.node27_timeseries_lifecycle_lock import (
        LIFECYCLE_LOCK_PATH,
        acquire_timeseries_lifecycle_lock,
        release_timeseries_lifecycle_lock,
    )

    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: _NOW)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    held = acquire_timeseries_lifecycle_lock()
    assert held is not None
    db_calls: list[str] = []

    def fail_fetch(dsn: str) -> list[compression.ChunkRow]:
        db_calls.append(dsn)
        raise AssertionError("lifecycle contender must not call the database")

    try:
        code = compression.main(argv=[], now_utc=_NOW, fetch_chunks=fail_fetch)
        assert code == 0
        diagnostic = json.loads(capsys.readouterr().err.strip())
        assert diagnostic["status"] == "refused_lock"
        assert diagnostic["reason"] == "lock-contended"
        assert db_calls == []
        assert LIFECYCLE_LOCK_PATH.exists()
        receipt = json.loads(Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"]).read_text())
        assert receipt["outcome"] == "refused_lock"
    finally:
        release_timeseries_lifecycle_lock(held)
        assert LIFECYCLE_LOCK_PATH.exists()


# ---------------------------------------------------------------------------
# Receipt schema + semantic contract
# ---------------------------------------------------------------------------


def test_receipt_validates_against_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "sel-a", delta_days=10),
        _chunk("hydro", "river_timeseries", "sel-b", delta_days=11),
        _chunk("hydro", "river_timeseries", "def-a", delta_days=12),
        _chunk("met", "forcing_station_timeseries", "def-b", delta_days=13),
        _chunk("met", "forcing_station_timeseries", "def-c", delta_days=14),
        _chunk("met", "forcing_station_timeseries", "def-d", delta_days=15),
        _chunk("met", "forcing_station_timeseries", "skip-1", delta_days=1),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    # per_tick_bound=5, we should see 5 selected, 1 deferred, 1 skipped
    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    jsonschema.validate(receipt, _load_schema())
    assert receipt["schema_version"] == "2.3"
    assert receipt["head_sha"] == compression._current_head_sha()
    assert len(receipt["selected"]) == 5
    assert len(receipt["deferred"]) == 1
    assert len(receipt["skipped"]) == 1

    # Disjointness by (hypertable_schema, hypertable_name, chunk_name)
    def _key(d):
        return (d["hypertable_schema"], d["hypertable_name"], d["chunk_name"])

    selected_keys = {_key(d) for d in receipt["selected"]}
    deferred_keys = {_key(d) for d in receipt["deferred"]}
    skipped_keys = {_key(d) for d in receipt["skipped"]}
    assert selected_keys.isdisjoint(deferred_keys)
    assert selected_keys.isdisjoint(skipped_keys)
    assert deferred_keys.isdisjoint(skipped_keys)
    # per_table_totals should aggregate the selected before_bytes
    for schema_name, hyper in compression.HYPERTABLES:
        key = f"{schema_name}.{hyper}"
        expected_before = sum(
            d["before_bytes"]
            for d in receipt["selected"]
            if d["hypertable_schema"] == schema_name and d["hypertable_name"] == hyper
        )
        assert receipt["per_table_totals"][key]["before_bytes"] == expected_before


def test_example_validates_against_schema() -> None:
    example = json.loads((_ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text())
    jsonschema.validate(example, _load_schema())


# #1389: the four committed historical runner receipts are a Must-preserve of
# openspec change compression-receipt-budget-audit — tightening the schema's
# 1.0/2.0 branches must go red here, not silently invalidate the archive.
# terminal-replay-*.json shares the directory and even schema_version "2.0"
# but belongs to the live-evidence schema family, so selection is by filename
# prefix, deliberately NOT by schema_version dispatch.
_COMMITTED_RECEIPT_DIR = _ROOT / "docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-compression"
_COMMITTED_RUNNER_RECEIPTS = sorted(
    path
    for path in _COMMITTED_RECEIPT_DIR.glob("*.json")
    if path.name.startswith(("dry-run-", "enforce-"))
)


def test_committed_runner_receipt_glob_is_not_silently_empty() -> None:
    # Guard the guard: a mistyped glob/prefix must not fake-green the family.
    assert len(_COMMITTED_RUNNER_RECEIPTS) >= 4, _COMMITTED_RUNNER_RECEIPTS


@pytest.mark.parametrize(
    "receipt_path",
    _COMMITTED_RUNNER_RECEIPTS,
    ids=lambda path: path.name,
)
def test_committed_historical_receipts_stay_valid_against_schema(receipt_path: Path) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, _load_schema())


def test_schema_keeps_v1_read_compatibility_but_v2_requires_head_sha() -> None:
    receipt = _example_receipt()
    del receipt["head_sha"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
    receipt["schema_version"] = "1.0"
    # ``budget`` arrived with "2.1" (issue #1351) and ``deferred_contended_count``
    # with "2.2" (#2713); both are forbidden on a 1.0 receipt, so the downgrade
    # drops them. The compatibility claim under test — 1.0 receipts stay
    # readable without ``head_sha`` — is unchanged.
    receipt.pop("budget")
    receipt.pop("deferred_contended_count")
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# cand-C: negative schema coverage — receipts violating the fresh contract
# rows in design.md (fixture Receipt schema) MUST be rejected.
# ---------------------------------------------------------------------------


def _example_receipt() -> dict:
    return json.loads(
        (_ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text(encoding="utf-8")
    )


def test_schema_rejects_selected_entry_without_chunk_name() -> None:
    receipt = _example_receipt()
    receipt["outcome"] = "clean"
    receipt["selected"] = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_1_42_chunk",
            "range_start": "2026-05-01T00:00:00Z",
            "range_end": "2026-05-08T00:00:00Z",
            "before_bytes": 1,
            "after_bytes": None,
        }
    ]
    del receipt["selected"][0]["chunk_name"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_per_table_total_missing_chunks_compressed() -> None:
    receipt = _example_receipt()
    del receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_invalid_mode_enum() -> None:
    receipt = _example_receipt()
    receipt["mode"] = "compress"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_unknown_top_level_key() -> None:
    receipt = _example_receipt()
    receipt["unknown_field"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_per_table_totals_missing_forcing_key() -> None:
    receipt = _example_receipt()
    del receipt["per_table_totals"]["met.forcing_station_timeseries"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_refused_lock_with_mutation_evidence() -> None:
    receipt = _example_receipt()
    # Pin the refusal outcome and its zeroed totals explicitly rather than
    # inheriting them from the example: an example that ever becomes a mutating
    # tick would otherwise make the totals a second, unrelated violation, and
    # this test would pass without ever exercising ``selected``.
    receipt["outcome"] = "refused_lock"
    receipt["per_table_totals"]["hydro.river_timeseries"] = {
        "before_bytes": 0,
        "after_bytes": None,
        "chunks_compressed": 0,
    }
    receipt["selected"] = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_1_42_chunk",
            "range_start": "2026-05-01T00:00:00Z",
            "range_end": "2026-05-08T00:00:00Z",
            "before_bytes": 1,
            "after_bytes": None,
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
    # The sole surviving violation names this test's target.
    errors = list(jsonschema.Draft202012Validator(_load_schema()).iter_errors(receipt))
    assert errors
    assert all(list(error.absolute_path)[:1] == ["selected"] for error in errors), [
        list(error.absolute_path) for error in errors
    ]


# ---------------------------------------------------------------------------
# Issue #1351: the effective budget chain is receipted, and raising the
# per-chunk ceiling fails closed unless the per-tick bound is 1.
# ---------------------------------------------------------------------------


_DEFAULT_BUDGET = {
    "compress_timeout_ms": 3_600_000,
    "wrapper_wall_seconds": 3_900,
    "systemd_wall_seconds": 3_941,
    "cleanup_margin_seconds": 300,
}
_NON_DEFAULT_BUDGET = {
    "compress_timeout_ms": 1_800_000,
    "wrapper_wall_seconds": 3_900,
    "systemd_wall_seconds": 3_941,
    "cleanup_margin_seconds": 300,
}
# #2713: schema 2.2 receipts additionally record the effective fence wait. The
# two 2.1-shaped literals above stay for the 2.1 schema-branch tests below.
_DEFAULT_BUDGET_V22 = {**_DEFAULT_BUDGET, "fence_wait_ms": 900_000}
_NON_DEFAULT_BUDGET_V22 = {**_NON_DEFAULT_BUDGET, "fence_wait_ms": 600_000}
# #3380: schema 2.3 receipts additionally record the lock timeout of the compress statement.
_DEFAULT_BUDGET_V23 = {**_DEFAULT_BUDGET_V22, "lock_timeout_ms": 5_000}
_NON_DEFAULT_BUDGET_V23 = {**_NON_DEFAULT_BUDGET_V22, "lock_timeout_ms": 2_000}


def _budget_env_override(budget: dict[str, int], *, per_tick_bound: str) -> dict[str, str]:
    override = {
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": str(budget["compress_timeout_ms"]),
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": str(budget["wrapper_wall_seconds"]),
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": str(budget["systemd_wall_seconds"]),
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": per_tick_bound,
    }
    if "fence_wait_ms" in budget:
        override["NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS"] = str(budget["fence_wait_ms"])
    if "lock_timeout_ms" in budget:
        override["NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS"] = str(budget["lock_timeout_ms"])
    return override


def _three_config_receipts(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> list[dict]:
    """The three config-bearing construction points, built from one config."""

    config = compression.config_from_args(_args(enforce=True), env)
    _calls, fake_fetch, fake_measure, fake_compress = _install_stubs(
        monkeypatch, chunks=[_chunk("hydro", "river_timeseries", "old-1", delta_days=10)]
    )
    return [
        compression.build_receipt(
            config,
            now_utc=_NOW,
            fetch_chunks=fake_fetch,
            measure_chunk_bytes=fake_measure,
            compress_chunk=fake_compress,
        ),
        compression.build_refused_lock_receipt(config, now_utc=_NOW, head_sha="a" * 40),
        compression.build_failed_receipt(config, now_utc=_NOW, stage="runner", head_sha="a" * 40),
    ]


def test_receipts_record_the_non_default_budget_actually_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A tick run on operator-set budgets is byte-distinguishable from a
    default-budget tick, at every construction point that has a config."""

    env = _base_env(tmp_path, override=_budget_env_override(_NON_DEFAULT_BUDGET_V23, per_tick_bound="1"))
    receipts = _three_config_receipts(monkeypatch, env)
    assert len(receipts) == 3
    for receipt in receipts:
        assert receipt["schema_version"] == "2.3"
        assert receipt["budget"] == _NON_DEFAULT_BUDGET_V23
        assert receipt["budget"] != _DEFAULT_BUDGET_V23
        jsonschema.validate(receipt, _load_schema())


def test_receipts_record_the_default_budget_when_nothing_is_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) Defaults are recorded as values, not as an absence."""

    env = _base_env(tmp_path)
    receipts = _three_config_receipts(monkeypatch, env)
    for receipt in receipts:
        assert receipt["schema_version"] == "2.3"
        assert receipt["budget"] == _DEFAULT_BUDGET_V23
        jsonschema.validate(receipt, _load_schema())


def test_config_tombstone_carries_no_budget_and_still_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The one legal omission: no valid config ever existed on this path."""

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"outcome":"stale-clean"}\n', encoding="utf-8")
    env = _base_env(tmp_path, override={"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "bad"})
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert compression.main([]) == 1

    tombstone = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert tombstone["schema_version"] == "2.3"
    assert tombstone["outcome"] == "failed"
    assert tombstone["failure"]["stage"] == "config"
    assert "budget" not in tombstone
    assert "per_tick_bound" not in tombstone
    jsonschema.validate(tombstone, _load_schema())


def test_config_stage_label_belongs_to_the_tombstone_call_site_only() -> None:
    """The schema's budget exemption keys off ``failure.stage == "config"``.

    ``build_failed_receipt`` always carries ``per_tick_bound`` so a hypothetical
    ``stage="config"`` failure receipt would not be exempted (it is required to
    carry ``budget``, and does) — but the exemption is only *meaningful* while
    the label stays tombstone-exclusive. Pin that.
    """

    source = _RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
    occurrences = [match.start() for match in re.finditer(r'stage="config"', source)]
    assert len(occurrences) == 1
    prefix = source[: occurrences[0]]
    assert prefix.rfind("_replace_early_stale_with_failure(") > prefix.rfind("build_failed_receipt(")


def _budget_receipt(**overrides: object) -> dict:
    receipt = _example_receipt()
    if overrides.get("schema_version") in ("1.0", "2.0", "2.1"):
        # Downgrading the 2.3 example: strip the #2713 and #3380 fields first,
        # so the assertions below see only the prohibition they are about.
        receipt.pop("deferred_contended_count")
        receipt["budget"].pop("fence_wait_ms")
        receipt["budget"].pop("lock_timeout_ms")
    receipt.update(overrides)
    return receipt


def test_schema_rejects_a_partial_budget_object() -> None:
    """(d) all-or-nothing: every field of the chain, or the object is a lie."""

    for missing in (
        "compress_timeout_ms",
        "wrapper_wall_seconds",
        "systemd_wall_seconds",
    ):
        receipt = _example_receipt()
        del receipt["budget"][missing]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_an_unknown_budget_field() -> None:
    receipt = _example_receipt()
    receipt["budget"]["query_timeout_ms"] = 60_000
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


@pytest.mark.parametrize("legacy_version", ["1.0", "2.0"])
def test_schema_rejects_budget_on_a_legacy_receipt(legacy_version: str) -> None:
    """(d) A pre-#1351 version number can never carry post-#1351 evidence."""

    receipt = _budget_receipt(schema_version=legacy_version)
    if legacy_version == "1.0":
        del receipt["head_sha"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
    # The prohibition this test is about lives on the receipt root, not on an
    # entry -- so ``['selected', 0]`` errors would mean it passed for the
    # wrong reason.
    errors = list(jsonschema.Draft202012Validator(_load_schema()).iter_errors(receipt))
    assert errors
    assert all(list(error.absolute_path) == [] for error in errors), [
        list(error.absolute_path) for error in errors
    ]
    assert all("budget" in error.message for error in errors), [error.message for error in errors]


@pytest.mark.parametrize("legacy_version", ["1.0", "2.0"])
def test_schema_accepts_a_legacy_receipt_without_budget(legacy_version: str) -> None:
    """(d) Historical receipts stay verifiable under the updated schema."""

    receipt = _budget_receipt(schema_version=legacy_version)
    del receipt["budget"]
    if legacy_version == "1.0":
        del receipt["head_sha"]
    jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_a_2_1_receipt_without_budget() -> None:
    """(d) Explicitly through Draft7Validator — the live-evidence consumer's
    validator class — so the conditional block cannot rely on 2020-12-only
    keywords that would silently no-op there."""

    receipt = _example_receipt()
    del receipt["budget"]
    validator = jsonschema.Draft7Validator(_load_schema())
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(receipt)
    assert any("budget" in error.message for error in validator.iter_errors(receipt))


def test_schema_accepts_the_config_tombstone_shape_and_rejects_it_with_budget() -> None:
    tombstone = {
        "schema_version": "2.1",
        "generated_at": "2026-07-15T12:00:00Z",
        "now_utc": "2026-07-15T12:00:00Z",
        "mode": "dry-run",
        "outcome": "failed",
        "provenance_state": "unavailable",
        "failure": {"stage": "config", "mutation_state": "failed_before_mutation"},
    }
    jsonschema.validate(tombstone, _load_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**tombstone, "budget": dict(_DEFAULT_BUDGET)}, _load_schema())


def test_schema_rejects_a_non_config_stage_failure_that_hides_budget_by_omitting_the_bound() -> None:
    """The exemption needs BOTH discriminators — the stage label is load-bearing.

    A failure past the config stage ran under a resolved budget, so omitting
    ``per_tick_bound`` must not buy it a ``budget``-free receipt. Without the
    ``failure.stage == "config"`` conjunct this shape validates and a real tick's
    budget chain silently disappears from the audit trail.
    """

    hidden_budget = {
        "schema_version": "2.1",
        "provenance_state": "unavailable",
        "generated_at": "2026-07-15T12:00:00Z",
        "now_utc": "2026-07-15T12:00:00Z",
        "mode": "dry-run",
        "outcome": "failed",
        "failure": {"stage": "freeze_head", "mutation_state": "failed_before_mutation"},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(hidden_budget, _load_schema())


def test_schema_requires_budget_on_a_config_stage_failure_that_has_a_config() -> None:
    """The exemption is a double discriminator, not a bare stage label.

    A ``stage == "config"`` receipt that still carries ``per_tick_bound`` had a
    config, so it must carry ``budget`` — and does not fall into a
    schema-invalid trap for carrying it.
    """

    failed = {
        "schema_version": "2.1",
        "provenance_state": "unavailable",
        "generated_at": "2026-07-15T12:00:00Z",
        "now_utc": "2026-07-15T12:00:00Z",
        "lag_seconds": 604800,
        "per_tick_bound": 1,
        "budget": dict(_DEFAULT_BUDGET),
        "mode": "dry-run",
        "outcome": "failed",
        "selected": [],
        "deferred": [],
        "skipped": [],
        "per_table_totals": {
            "hydro.river_timeseries": {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0},
            "met.forcing_station_timeseries": {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0},
        },
        "failure": {"stage": "config", "mutation_state": "failed_before_mutation"},
    }
    jsonschema.validate(failed, _load_schema())
    without_budget = {key: value for key, value in failed.items() if key != "budget"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(without_budget, _load_schema())


def test_schema_still_requires_head_sha_on_a_2_1_non_failed_receipt() -> None:
    """The version bump must not weaken the provenance pin it inherited."""

    receipt = _example_receipt()
    del receipt["head_sha"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_raising_the_compress_timeout_without_dropping_the_bound_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(e) The §4.5 catch-up recipe is enforced, not merely documented."""

    env = _base_env(
        tmp_path,
        override=_budget_env_override(
            {
                "compress_timeout_ms": compression._DEFAULT_COMPRESS_TIMEOUT_MS + 1,
                "wrapper_wall_seconds": 3_901,
                "systemd_wall_seconds": 3_942,
            },
            per_tick_bound="4",
        ),
    )
    with pytest.raises(compression.CompressionConfigError) as excinfo:
        compression.config_from_args(_args(), env)
    message = str(excinfo.value)
    assert "§4.5" in message
    assert "PER_TICK_BOUND=1" in message

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect_calls, _statements = _install_fake_psycopg2(monkeypatch)
    assert compression.main(["--enforce"]) != 0
    assert connect_calls == []
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["status"] == "failed"
    assert "§4.5" in diagnostic["reason"]


@pytest.mark.parametrize(
    ("compress_timeout_ms", "wrapper", "systemd", "bound"),
    [
        # Raised ceiling with the catch-up bound: the sanctioned combination.
        (_DEFAULT_BUDGET["compress_timeout_ms"] + 1, 3_901, 3_942, "1"),
        # Default ceiling with the #2425 capacity bound: the normal régime.
        (_DEFAULT_BUDGET["compress_timeout_ms"], 3_900, 3_941, "2"),
        # Below the default with a high bound: leg 3 only guards raising.
        (1_800_000, 3_900, 3_941, "4"),
    ],
)
def test_legal_timeout_bound_combinations_still_construct(
    tmp_path: Path, compress_timeout_ms: int, wrapper: int, systemd: int, bound: str
) -> None:
    env = _base_env(
        tmp_path,
        override=_budget_env_override(
            {
                "compress_timeout_ms": compress_timeout_ms,
                "wrapper_wall_seconds": wrapper,
                "systemd_wall_seconds": systemd,
            },
            per_tick_bound=bound,
        ),
    )
    config = compression.config_from_args(_args(), env)
    assert config.compress_timeout_ms == compress_timeout_ms
    assert config.per_tick_bound == int(bound)


def test_env_template_literals_survive_the_catch_up_invariant(tmp_path: Path) -> None:
    """(e) The deployed combination, read out of the template rather than out
    of the runner's own constants.

    ``_DEFAULT_COMPRESS_TIMEOUT_MS`` is the threshold AND has been retuned once
    (#1352). A future lowering would keep every constant-based assertion green
    while the template's literal timeout became a "raised" one and node-27's
    ``PER_TICK_BOUND=2`` tick started failing closed. The template is the
    deployment source, so pin the template's own numbers.
    """

    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    def _literal(name: str) -> str:
        matches = re.findall(rf"(?m)^{name}=(\d+)$", text)
        assert len(matches) == 1, (name, matches)
        return matches[0]

    override = {
        f"NODE27_TIMESERIES_COMPRESSION_{name}": _literal(f"NODE27_TIMESERIES_COMPRESSION_{name}")
        for name in (
            "COMPRESS_TIMEOUT_MS",
            "WRAPPER_WALL_SECONDS",
            "SYSTEMD_WALL_SECONDS",
            "PER_TICK_BOUND",
            "LAG_SECONDS",
        )
    }
    config = compression.config_from_args(_args(), _base_env(tmp_path, override=override))
    assert config.compress_timeout_ms == int(override["NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS"])
    assert config.per_tick_bound == int(override["NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND"])
    assert config.lag_seconds == 172800
    assert config.per_tick_bound == 2
    assert "one chunk width" not in text


# ---------------------------------------------------------------------------
# Migration text guardrails
# ---------------------------------------------------------------------------


def test_migration_contains_verbatim_segmentby_orderby() -> None:
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "hydro.river_timeseries" in text
    assert "met.forcing_station_timeseries" in text
    assert "timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id'" in text
    assert "timescaledb.compress_orderby = 'variable, valid_time'" in text
    assert "timescaledb.compress_segmentby = 'forcing_version_id, station_id'" in text


def test_migration_does_not_add_compression_policy() -> None:
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    # D3 forbids background policy jobs. Grep the executable statements only,
    # not the prose header (which explains WHY policy jobs are rejected).
    executable = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    assert "add_compression_policy" not in executable


def _migration_statements() -> list[str]:
    """Top-level executable statements, via the real apply-lane splitter oracle."""
    return split_sql_statements(_MIGRATION_PATH.read_text(encoding="utf-8"))


def _strip_comment_lines(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_migration_alter_statements_are_disjoint_and_order_independent() -> None:
    # `split_sql_statements` is the same splitter `packages.common.migrate`
    # uses to apply migrations, so it is the authoritative statement oracle.
    statements = [_strip_comment_lines(s) for s in _migration_statements()]
    assert len(statements) == 2
    river, forcing = statements
    # Statement 1 touches only hydro.river_timeseries, statement 2 only
    # met.forcing_station_timeseries: disjoint tables, order-independent.
    assert "hydro.river_timeseries" in river
    assert "met.forcing_station_timeseries" not in river
    assert "met.forcing_station_timeseries" in forcing
    assert "hydro.river_timeseries" not in forcing
    assert len(re.findall(r"ALTER\s+TABLE\s+hydro\.river_timeseries\s+SET\s*\(", river)) == 1
    assert len(re.findall(r"ALTER\s+TABLE\s+met\.forcing_station_timeseries\s+SET\s*\(", forcing)) == 1


def test_migration_statements_are_guarded_do_blocks_without_transaction_verbs() -> None:
    """G8 (#1069): unconditional bare ALTERs are NOT re-runnable.

    Measured on node-27 (PG 15.2 + TimescaleDB 2.10.2, 2026-07-16):
    ``ALTER TABLE ... SET (timescaledb.compress ...)`` fails with
    ``cannot change configuration on already compressed chunks`` whenever the
    hypertable has ANY compressed chunk — even when the requested settings
    exactly match the catalog. The per-table guarded ``DO $$`` block (skip
    when the catalog already matches D3, otherwise ALTER) is what makes the
    header's partial-apply/re-run promise actually true.

    Session-level transaction verbs stay forbidden: each of the two DO
    blocks must run as its own implicit transaction under the autocommit
    apply lanes, so a partial apply is completed by a plain re-run.
    """
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    forbidden = re.search(
        r"\bSTART\s+TRANSACTION\b|\bCOMMIT\b|\bROLLBACK\b|\bSAVEPOINT\b",
        executable,
        flags=re.IGNORECASE,
    )
    assert forbidden is None, f"migration must not use session-level transaction verbs; matched: {forbidden.group(0)!r}"
    statements = [_strip_comment_lines(s).strip() for s in _migration_statements()]
    assert len(statements) == 2
    for statement in statements:
        assert re.match(r"DO\s*\$\$", statement), f"expected a DO $$ block, got: {statement[:40]!r}"


def test_migration_g8_guard_reads_catalog_and_pins_expected_rows() -> None:
    """G8 regression lock: each DO block guards on the live catalog vs the
    D3-expected `timescaledb_information.compression_settings` rows."""
    statements = _migration_statements()
    assert len(statements) == 2
    river, forcing = statements
    for statement in statements:
        assert "timescaledb_information.compression_settings" in statement
    # Sentinel expected-row literals from the measured node-27 catalog oracle.
    assert "('river_segment_id'::text, 3::int" in river
    assert "('valid_time'::text, NULL::int, 2::int" in river
    assert "('station_id'::text, 2::int" in forcing
    assert "('valid_time'::text, NULL::int, 2::int" in forcing


# ---------------------------------------------------------------------------
# Runner source: catalog-only guard
# ---------------------------------------------------------------------------


def test_chunk_query_filters_out_already_compressed_chunks() -> None:
    # cand-E: assert on the runtime constant so refactors that move the
    # literal into a comment or unrelated docstring can't accidentally
    # silence the guard.
    query = compression._CHUNK_QUERY
    assert "is_compressed = false" in query


def test_chunk_query_does_not_scan_detail_hypertables() -> None:
    query = compression._CHUNK_QUERY
    assert "timescaledb_information.chunks" in query
    # The runner MUST NOT read hydro.river_timeseries or
    # met.forcing_station_timeseries rows directly; those literals in the
    # source are only allowed as tuple filters against the catalog view.
    lines = [line for line in query.splitlines() if line.strip()]
    for line in lines:
        if "hydro.river_timeseries" in line or "met.forcing_station_timeseries" in line:
            # Must be inside the tuple filter (as string literals with quotes).
            assert "'" in line, f"detail hypertable referenced outside string literal: {line!r}"
    from packages.common.node27_timeseries_discovery import RUNTIME_HYPERTABLES_SQL

    assert RUNTIME_HYPERTABLES_SQL in query
    assert "UNION ALL" in RUNTIME_HYPERTABLES_SQL
    assert "UNION ALL" in query


# ---------------------------------------------------------------------------
# DSN masking
# ---------------------------------------------------------------------------


def test_mask_dsn_strips_credentials() -> None:
    masked = compression._mask_dsn("postgresql://user:secretpw@127.0.0.1:55432/nhms")
    assert "secretpw" not in masked
    assert "user" not in masked
    assert "127.0.0.1" in masked
    assert "55432" in masked


def test_dsn_never_appears_in_lock_contention_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    lock_path = Path(env["NODE27_TIMESERIES_COMPRESSION_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        compression.main(argv=[], now_utc=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    err = capsys.readouterr().err
    assert "secretpw" not in err
    assert "user:" not in err


def test_dsn_never_appears_in_config_failure_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://alice:supersekret@127.0.0.1:55432/nhms")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND", "5")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH", str(tmp_path / "receipt.json"))
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LOCK_PATH", str(tmp_path / "runner.lock"))
    code = compression.main(argv=[], now_utc=_NOW)
    assert code == 1
    err = capsys.readouterr().err
    assert "supersekret" not in err
    assert "alice" not in err
    assert not (tmp_path / "receipt.json").exists()


def test_timeseries_compression_service_bootstraps_log_dir() -> None:
    service_text = _SYSTEMD_SERVICE_PATH.read_text(encoding="utf-8")
    assert "ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-timeseries-compression-logs" in service_text
    assert "StandardOutput=append:/home/nwm/node27-timeseries-compression-logs/systemd.log" in service_text
    lines = service_text.splitlines()
    pre_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStartPre="))
    start_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
    assert pre_index < start_index


def test_compressed_sibling_lookup_matches_timescaledb_210_catalog() -> None:
    query = compression._COMPRESSED_SIBLING_QUERY
    assert "_timescaledb_catalog.chunk AS origin" in query
    assert "_timescaledb_catalog.chunk AS sibling" in query
    assert "sibling.id = origin.compressed_chunk_id" in query
    assert "origin.schema_name = %s" in query
    assert "origin.table_name = %s" in query
    assert "NOT origin.dropped" in query
    assert "NOT sibling.dropped" in query
    assert "compressed_chunk_schema" not in query
    assert "compressed_chunk_name" not in query


def test_compression_cannot_shrink_service_to_wrapper_plus_margin(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3940",
        },
    )
    with pytest.raises(compression.CompressionConfigError, match="service wall"):
        compression.config_from_args(_args(), env)


def test_compression_env_example_pins_the_per_tick_capacity_target() -> None:
    """#1237 / #2425: the per-tick bound is a decided capacity target, not a default.

    The substring pins above cannot see this line — the only other
    `PER_TICK_BOUND=` occurrence in the template is the commented catch-up
    hint (`=1`). Pin the assignment line by UNIQUENESS, not by presence: a
    bare search still passes when a second, contradicting assignment is
    appended later in the file (the last one is what `set -a; . env` actually
    exports). Derivation: runbook §4 "Per-tick capacity".
    """
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert re.findall(r"(?m)^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=(\d+)$", text) == ["2"]


def test_replay_service_is_explicit_no_timer_supervisor_lane() -> None:
    replay = _SYSTEMD_REPLAY_SERVICE_PATH.read_text(encoding="utf-8")
    assert "node27_timeseries_compression_supervisor.py --enforce" in replay
    for option in (
        "--run-plan-path /home/nwm/node27-timeseries-compression-replay/run-plan.json",
        "--ledger-path /home/nwm/node27-timeseries-compression-replay/supervisor-ledger.jsonl",
        "--receipt-path /home/nwm/node27-timeseries-compression-replay/terminal-evidence.json",
        "--finalizer-state-path /home/nwm/node27-timeseries-compression-replay/finalizer-state.json",
        "--wall-seconds 900",
    ):
        assert option in replay
    assert "ExecStopPost=" in replay and "--finalize-only" in replay
    assert "TimeoutStartSec=920" in replay
    assert not (_ROOT / "infra/systemd/nhms-node27-timeseries-compression-replay.timer").exists()
    env_example = (_ROOT / "infra/env/node27-timeseries-compression-replay.example").read_text()
    assert "NODE27_COMPRESSION_EXPECTED_STALE_SHA256=REPLACE_WITH_64_HEX_DIGEST" in env_example


def test_timeseries_compression_timer_oncalendar_and_unit_wiring() -> None:
    timer_text = _SYSTEMD_TIMER_PATH.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04:25:00 UTC" in timer_text
    assert "Unit=nhms-node27-timeseries-compression.service" in timer_text


def test_early_failure_publisher_lock_is_deadline_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt.json"
    raw = b'{"outcome":"newer"}\n'
    receipt.write_bytes(raw)
    info = receipt.stat()
    target = compression._SafeReceiptTarget(
        receipt,
        info.st_dev,
        info.st_ino,
        hashlib.sha256(raw).hexdigest(),
    )
    lock_fd = os.open(receipt.with_name(f".{receipt.name}.publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(compression, "PUBLISH_LOCK_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        compression._replace_early_stale_with_failure(
            target,
            enforce=True,
            now_utc=_NOW,
            stage="held-lock",
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert time.monotonic() - started < 0.5
    assert receipt.read_bytes() == raw


# ---------------------------------------------------------------------------
# #1647 — the qualified chunk name is fail-closed
# ---------------------------------------------------------------------------


def _chunk_named(chunk_schema: str, chunk_name: str) -> compression.ChunkRow:
    return compression.ChunkRow(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        chunk_schema=chunk_schema,
        chunk_name=chunk_name,
        range_start=_NOW - timedelta(days=8),
        range_end=_NOW - timedelta(days=1),
        is_compressed=False,
    )


_CHUNK_IDENT_ATTRS = frozenset({"qualified_chunk", "chunk_name", "chunk_schema"})


def _fstring_chunk_refs(node: ast.JoinedStr) -> set[str]:
    """Chunk-identifier names interpolated by one f-string, attributes or bare."""
    names: set[str] = set()
    for value in node.values:
        if not isinstance(value, ast.FormattedValue):
            continue
        for sub in ast.walk(value.value):
            if isinstance(sub, ast.Attribute):
                names.add(sub.attr)
            elif isinstance(sub, ast.Name):
                names.add(sub.id)
    return names & _CHUNK_IDENT_ATTRS


def _fstring_sql_naming_a_chunk(source: str) -> list[str]:
    """Every f-string in ``source`` that would put a chunk name into SQL text.

    Exactly two rules fire, and their reach is worth stating because neither is
    a taint analysis:

    * **Rule 1 — any f-string that interpolates ``qualified_chunk``**, wherever
      it sits. Position-independent: an argument, a kwarg, a nested
      ``sql.SQL(f"")``, a bare log line all match, because the property has no
      production consumer at all, so every occurrence is news.
    * **Rule 2 — an f-string handed DIRECTLY as the first positional argument
      (the statement) to ``execute``/``executemany``**, whose interpolations
      name ``chunk_schema`` / ``chunk_name`` / ``qualified_chunk``. Syntactic
      position only.

    What that leaves unflagged, verified rather than assumed: a local rebinding
    (``q = chunk.qualified_chunk`` then ``execute(f"... {q}")``) escapes BOTH
    rules — nothing follows the name — and only ``qualified_chunk``'s own
    ``ValueError``, which fires on the attribute access before any text exists,
    still stands there; a ``query=`` kwarg and ``sql.SQL(f"")`` escape rule 2
    (not the first positional argument) but are still caught by rule 1 when the
    f-string names ``qualified_chunk``; and ``.format`` / ``+`` / ``%``
    concatenation escape both rules always, since they produce no ``JoinedStr``
    node.

    Parsed, not grepped: the module legitimately builds f-strings out of
    ``chunk_schema`` / ``chunk_name`` for bind-parameter values
    (``compress_chunk(%s::regclass)``) and for error text, and a substring scan
    would match those, the module's comments, and this docstring.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr) and "qualified_chunk" in _fstring_chunk_refs(node):
            offenders.append(f"line {node.lineno}: f-string interpolates qualified_chunk")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany"}
            and node.args
            and isinstance(node.args[0], ast.JoinedStr)
            and _fstring_chunk_refs(node.args[0])
        ):
            offenders.append(
                f"line {node.lineno}: {node.func.attr}() statement interpolates "
                f"{sorted(_fstring_chunk_refs(node.args[0]))}"
            )
    return sorted(offenders)


def test_the_module_has_no_fstring_sql_naming_a_chunk() -> None:
    """`qualified_chunk` has no production consumer, and that is the point.

    Every chunk reference in the runner is a bound ``%s::regclass`` parameter;
    the ride-along ANALYZE the helper was written for was removed in f5069257.
    The helper is defence in depth for a future interpolation site, so the
    thing worth pinning is that no such site has quietly appeared.
    """
    source = Path(compression.__file__).read_text(encoding="utf-8")

    assert _fstring_sql_naming_a_chunk(source) == []


def test_the_fstring_chunk_scan_flags_an_injected_analyze() -> None:
    """A scan that cannot red is decoration. It must fire on the forbidden
    shape and stay quiet on the three shapes the module actually uses.
    """
    assert _fstring_sql_naming_a_chunk(
        'cursor.execute(f"ANALYZE {chunk.qualified_chunk}")\n'
    ) == [
        "line 1: execute() statement interpolates ['qualified_chunk']",
        "line 1: f-string interpolates qualified_chunk",
    ]
    assert _fstring_sql_naming_a_chunk(
        'cursor.execute(f"ANALYZE {chunk_schema}.{chunk_name}")\n'
    ) == ["line 1: execute() statement interpolates ['chunk_name', 'chunk_schema']"]

    assert (
        _fstring_sql_naming_a_chunk(
            'cursor.execute("SELECT compress_chunk(%s::regclass)",'
            ' (f"{chunk.chunk_schema}.{chunk.chunk_name}",))\n'
        )
        == []
    )
    assert (
        _fstring_sql_naming_a_chunk(
            'raise ValueError(f"non-identifier {self.chunk_schema}.{self.chunk_name}")\n'
        )
        == []
    )
    assert _fstring_sql_naming_a_chunk('cursor.execute(f"SET statement_timeout = {ms}")\n') == []


@pytest.mark.parametrize(
    ("chunk_schema", "chunk_name"),
    [
        ("_timescaledb_internal", '_hyper_1_2_chunk"; DROP TABLE hydro.river_timeseries; --'),
        ("_timescaledb_internal", "_hyper_1_2_chunk; SELECT 1"),
        ('_timescaledb_internal"', "_hyper_1_2_chunk"),
        ("_timescaledb_internal", "_hyper_1_2_chunk chunk"),
        ("_timescaledb_internal", ""),
        ("", "_hyper_1_2_chunk"),
    ],
    ids=["quote-in-name", "semicolon-in-name", "quote-in-schema", "space", "empty-name", "empty-schema"],
)
def test_qualified_chunk_raises_before_returning_a_non_identifier_name(
    chunk_schema: str, chunk_name: str
) -> None:
    """The refusal is the property's own, not a caller's.

    Any future consumer would interpolate this into a statement that takes no
    bind parameters, so the only safe input is one that cannot need escaping —
    and the check has to fire on the attribute access itself, before any text
    exists to hand on. Both parts are checked: a catalog row is two
    identifiers, not one.
    """
    chunk = _chunk_named(chunk_schema, chunk_name)

    with pytest.raises(ValueError, match="non-identifier chunk name"):
        chunk.qualified_chunk


def test_qualified_chunk_accepts_a_real_catalog_name_unchanged() -> None:
    """The shape TimescaleDB actually emits is produced byte-for-byte as before."""
    chunk = _chunk_named("_timescaledb_internal", "_hyper_3_8_chunk")

    assert chunk.qualified_chunk == '"_timescaledb_internal"."_hyper_3_8_chunk"'


# ---------------------------------------------------------------------------
# #3380: the compress statement runs under its own short ``lock_timeout``, and
# a lock-wait timeout (SQLSTATE 55P03) after a "not compressed" reconcile is
# contention, not failure. Only the driver boundary (``psycopg2``) and the
# clocks are faked; expected values are literals.
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_KEY = "NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS"
_FENCE_WAIT_KEY = "NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS"
# (classid, objid) of the river family fence, as pinned literally by the fence tests.
_RIVER_FENCE = (2713, -1964848284)
_COMPRESS_SQL = "SELECT compress_chunk(%s::regclass)"


class LockNotAvailable(Exception):
    """Driver double named after the psycopg2.errors class; the SQLSTATE is in ``pgcode``."""

    pgcode = "55P03"


class QueryCanceled(Exception):
    """Driver double for a statement timeout."""

    pgcode = "57014"


class _ScriptedCursor:
    """Records every statement; raises the scripted error for an exact ``(sql, params)``."""

    def __init__(self, statements: list[tuple[str, object]], failures: dict[tuple[str, object], Exception]) -> None:
        self._statements = statements
        self._failures = failures

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._statements.append((sql, params))
        error = self._failures.get((sql, params))
        if error is not None:
            raise error

    def fetchone(self) -> tuple[object, ...]:
        return (None,)


class _ScriptedConnection:
    def __init__(self, statements: list[tuple[str, object]], failures: dict[tuple[str, object], Exception]) -> None:
        self._statements = statements
        self._failures = failures
        self.exits: list[type[BaseException] | None] = []
        self.closed = 0

    def __enter__(self) -> "_ScriptedConnection":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_exc: object) -> bool:
        # psycopg2: a clean exit commits, a raising block rolls back.
        self.exits.append(exc_type)
        return False

    def cursor(self, *_args: object, **_kwargs: object) -> _ScriptedCursor:
        return _ScriptedCursor(self._statements, self._failures)

    def close(self) -> None:
        self.closed += 1


def _install_scripted_driver(
    monkeypatch: pytest.MonkeyPatch, failures: dict[tuple[str, object], Exception] | None = None
) -> tuple[list[tuple[str, object]], list[_ScriptedConnection]]:
    statements: list[tuple[str, object]] = []
    connections: list[_ScriptedConnection] = []

    def connect(*_args: object, **_kwargs: object) -> _ScriptedConnection:
        connection = _ScriptedConnection(statements, failures or {})
        connections.append(connection)
        return connection

    module = types.ModuleType("psycopg2")
    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    return statements, connections


def _fence_clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> None:
    clock = iter(readings)
    monkeypatch.setattr(fence, "_monotonic", lambda: next(clock))


def _compress_once(chunk_name: str = "c1", *, lock_timeout_ms: int = 2000) -> int:
    """The real ``_default_compress_chunk``: 3600000 ms budget, 900000 ms fence wait."""
    return compression._default_compress_chunk(
        "postgresql://x",
        _chunk("hydro", "river_timeseries", chunk_name, delta_days=10),
        compress_timeout_ms=3_600_000,
        fence_wait_ms=900_000,
        lock_timeout_ms=lock_timeout_ms,
    )


# The whole session when the fence wait took 123.4 s: the remainder 3476600 ms
# is the compress statement's statement_timeout, and the lock timeout comes
# after the fence's own RESET and right before the compress statement.
_LOCK_BOUNDED_SESSION = [
    ("SET statement_timeout = 3600000", None),
    ("SET lock_timeout = 900000", None),
    ("SELECT pg_advisory_lock(%s, %s)", _RIVER_FENCE),
    ("RESET lock_timeout", None),
    ("SET statement_timeout = 3476600", None),
    ("SET lock_timeout = 2000", None),
    (_COMPRESS_SQL, ("_timescaledb_internal.c1",)),
    ("SELECT pg_advisory_unlock(%s, %s)", _RIVER_FENCE),
]


# 4.1 configuration ----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, ""])
def test_lock_timeout_defaults_to_five_seconds(tmp_path: Path, raw: str | None) -> None:
    config = compression.config_from_args(_args(), _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: raw}))
    assert config.lock_timeout_ms == 5000


def test_a_declared_lock_timeout_is_used(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(), _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: "2000"}))
    assert config.lock_timeout_ms == 2000


def test_the_lock_timeout_bound_is_the_budget_left_after_the_fence_wait(tmp_path: Path) -> None:
    # 3600000 - 3000000 = 600000: one below is accepted, the remainder itself is not.
    env = {_FENCE_WAIT_KEY: "3000000"}
    accepted = compression.config_from_args(
        _args(), _base_env(tmp_path, override={**env, _LOCK_TIMEOUT_KEY: "599999"})
    )
    assert accepted.lock_timeout_ms == 599_999
    with pytest.raises(compression.CompressionConfigError, match=_LOCK_TIMEOUT_KEY):
        compression.config_from_args(_args(), _base_env(tmp_path, override={**env, _LOCK_TIMEOUT_KEY: "600000"}))


# Defaults: compress 3600000 ms, fence wait 900000 ms, so 2700000 is the first refused value.
_BAD_LOCK_TIMEOUTS = ["abc", "0", "-1", "2700000", "2700001"]


@pytest.mark.parametrize("raw", _BAD_LOCK_TIMEOUTS)
def test_a_bad_lock_timeout_fails_closed(tmp_path: Path, raw: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match=_LOCK_TIMEOUT_KEY):
        compression.config_from_args(_args(), _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: raw}))


@pytest.mark.parametrize("raw", _BAD_LOCK_TIMEOUTS)
def test_a_bad_lock_timeout_is_refused_before_any_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    # ``fetch_display_watermark`` is deliberately not patched: it opens its own
    # connection, so a value that slipped through would show in ``connect_calls``.
    for key, value in _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: raw}).items():
        monkeypatch.setenv(key, value)
    connect_calls, statements = _install_fake_psycopg2(monkeypatch)

    assert compression.main(["--enforce"]) == 1
    assert connect_calls == [] and statements == []
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["status"] == "failed"
    assert _LOCK_TIMEOUT_KEY in diagnostic["reason"]


def test_the_lock_timeout_is_keyword_only_without_a_default() -> None:
    parameter = inspect.signature(compression._default_compress_chunk).parameters["lock_timeout_ms"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# 4.2 session order ----------------------------------------------------------


def test_the_compress_statement_runs_under_the_lock_timeout_set_after_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements, connections = _install_scripted_driver(monkeypatch)
    _fence_clock(monkeypatch, 50.0, 50.0 + 123.4)

    assert _compress_once() == 123_400
    assert statements == _LOCK_BOUNDED_SESSION
    [connection] = connections
    assert connection.exits == [None]
    assert connection.closed == 1


def test_main_binds_the_configured_lock_timeout_into_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No injected ``compress_chunk``: the ``functools.partial`` in ``main`` is what is pinned."""
    env = _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: "2000"})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    statements, _connections = _install_scripted_driver(monkeypatch)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: _NOW)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    _fence_clock(monkeypatch, 100.0, 100.25)
    chunk = _chunk("hydro", "river_timeseries", "c1", delta_days=9)

    code = compression.main(
        ["--enforce"],
        fetch_chunks=lambda _dsn: [chunk],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 100,
        reconcile_chunk_state=lambda _dsn, _chunk: False,
    )

    assert code == 0
    assert statements == [
        ("SET statement_timeout = 3600000", None),
        ("SET lock_timeout = 900000", None),
        ("SELECT pg_advisory_lock(%s, %s)", _RIVER_FENCE),
        ("RESET lock_timeout", None),
        ("SET statement_timeout = 3599750", None),
        ("SET lock_timeout = 2000", None),
        (_COMPRESS_SQL, ("_timescaledb_internal.c1",)),
        ("SELECT pg_advisory_unlock(%s, %s)", _RIVER_FENCE),
    ]
    receipt = json.loads(Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"]).read_text())
    assert receipt["budget"]["lock_timeout_ms"] == 2000


# 4.3 translation ------------------------------------------------------------


def test_a_lock_timeout_of_the_compress_statement_becomes_relation_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements, connections = _install_scripted_driver(
        monkeypatch, {(_COMPRESS_SQL, ("_timescaledb_internal.c1",)): LockNotAvailable("lock timeout")}
    )
    _fence_clock(monkeypatch, 50.0, 50.0 + 123.4)

    with pytest.raises(compression.RelationLockContended) as excinfo:
        _compress_once()

    assert excinfo.value.fence_wait_elapsed_ms == 123_400
    assert excinfo.value.hypertable == "hydro.river_timeseries"
    assert not isinstance(excinfo.value, fence.FenceContended)
    assert statements == _LOCK_BOUNDED_SESSION  # the fence is released after the rollback
    [connection] = connections
    assert connection.exits == [compression.RelationLockContended]
    assert connection.closed == 1


@pytest.mark.parametrize(
    "statement",
    [("SET statement_timeout = 3476600", None), ("SET lock_timeout = 2000", None)],
    ids=["second-statement-timeout", "lock-timeout-set"],
)
def test_a_55p03_from_an_earlier_statement_is_not_translated(
    monkeypatch: pytest.MonkeyPatch, statement: tuple[str, object]
) -> None:
    error = LockNotAvailable("earlier statement")
    statements, connections = _install_scripted_driver(monkeypatch, {statement: error})
    _fence_clock(monkeypatch, 50.0, 50.0 + 123.4)

    with pytest.raises(LockNotAvailable) as excinfo:
        _compress_once()

    assert excinfo.value is error
    assert not any(sql == _COMPRESS_SQL for sql, _ in statements)
    assert statements[-1] == ("SELECT pg_advisory_unlock(%s, %s)", _RIVER_FENCE)
    assert connections[0].closed == 1


def test_the_fence_wait_stays_fence_contention(monkeypatch: pytest.MonkeyPatch) -> None:
    statements, connections = _install_scripted_driver(
        monkeypatch, {("SELECT pg_advisory_lock(%s, %s)", _RIVER_FENCE): LockNotAvailable("fence wait")}
    )
    _fence_clock(monkeypatch, 0.0, 900.0)

    with pytest.raises(fence.FenceContended) as excinfo:
        _compress_once()

    assert not isinstance(excinfo.value, compression.RelationLockContended)
    assert excinfo.value.elapsed_ms == 900_000
    assert not any(sql == _COMPRESS_SQL or sql.startswith("SELECT pg_advisory_unlock") for sql, _ in statements)
    assert connections[0].closed == 1


def test_a_statement_timeout_of_the_compress_statement_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = QueryCanceled("statement timeout")
    statements, connections = _install_scripted_driver(
        monkeypatch, {(_COMPRESS_SQL, ("_timescaledb_internal.c1",)): error}
    )
    _fence_clock(monkeypatch, 50.0, 50.0 + 123.4)

    with pytest.raises(QueryCanceled) as excinfo:
        _compress_once()

    assert excinfo.value is error
    assert statements == _LOCK_BOUNDED_SESSION
    assert connections[0].exits == [QueryCanceled]
    assert connections[0].closed == 1


# 4.4 - 4.6 the tick loop ----------------------------------------------------


def _run_main_enforce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[compression.ChunkRow],
    *,
    compress_chunk: compression.CompressChunk | None = None,
    reconcile: object = False,
) -> tuple[int, dict, list[str]]:
    """``main --enforce`` on the default budget; returns (rc, receipt, reconciled chunk names)."""
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compression, "fetch_display_watermark", lambda _dsn, **_kwargs: _NOW)
    monkeypatch.setattr(compression, "_current_head_sha", lambda **_kwargs: "a" * 40)
    reconciled: list[str] = []

    def reconcile_chunk_state(_dsn: str, chunk: compression.ChunkRow) -> bool:
        reconciled.append(chunk.chunk_name)
        if isinstance(reconcile, Exception):
            raise reconcile
        return bool(reconcile)

    rc = compression.main(
        ["--enforce"],
        fetch_chunks=lambda _dsn: list(chunks),
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 40 if after else 100,
        compress_chunk=compress_chunk,
        reconcile_chunk_state=reconcile_chunk_state,
    )
    receipt = json.loads(Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"]).read_text())
    return rc, receipt, reconciled


def _busy_and_done() -> list[compression.ChunkRow]:
    # Same hypertable; newest first, so "busy" is attempted before "done".
    return [
        _chunk("hydro", "river_timeseries", "busy", delta_days=9),
        _chunk("hydro", "river_timeseries", "done", delta_days=10),
    ]


_BUSY_RANGE = {"range_start": "2026-06-25T12:00:00Z", "range_end": "2026-07-02T12:00:00Z"}


def _assert_valid_2_3(receipt: dict, *, lock_timeout_ms: int = 5000) -> None:
    assert receipt["schema_version"] == "2.3"
    assert receipt["budget"]["lock_timeout_ms"] == lock_timeout_ms
    jsonschema.validate(receipt, _load_schema())
    jsonschema.Draft7Validator(_load_schema()).validate(receipt)


def test_a_relation_lock_timeout_through_the_real_session_defers_the_chunk_and_the_tick_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_scripted_driver(monkeypatch, {(_COMPRESS_SQL, ("_timescaledb_internal.busy",)): LockNotAvailable()})
    _fence_clock(monkeypatch, 10.0, 10.25, 20.0, 20.0)

    rc, receipt, reconciled = _run_main_enforce(tmp_path, monkeypatch, _busy_and_done())

    busy, done = receipt["selected"]
    assert busy == {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "busy",
        **_BUSY_RANGE,
        "before_bytes": 100,
        "after_bytes": None,
        "mutation_state": "deferred_contended",
        "fence_wait_elapsed_ms": 250,
        "contended_on": "relation_lock",
    }
    assert reconciled == ["busy"]  # reconciled before it is labelled
    assert done["mutation_state"] == "committed" and done["after_bytes"] == 40
    assert receipt["per_table_totals"]["hydro.river_timeseries"] == {
        "before_bytes": 100,
        "after_bytes": 40,
        "chunks_compressed": 1,
    }
    assert (receipt["outcome"], receipt["deferred_contended_count"], rc) == ("deferred", 1, 0)
    _assert_valid_2_3(receipt)


def _busy_times_out(_dsn: str, chunk: compression.ChunkRow) -> int:
    if chunk.chunk_name == "busy":
        raise compression.RelationLockContended("hydro.river_timeseries", 250)
    return 0


def test_relation_lock_arm_not_compressed_is_a_counted_deferral_without_error_or_poison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, receipt, reconciled = _run_main_enforce(
        tmp_path, monkeypatch, _busy_and_done(), compress_chunk=_busy_times_out, reconcile=False
    )

    assert reconciled == ["busy"]
    busy, done = receipt["selected"]
    assert busy["mutation_state"] == "deferred_contended"
    assert busy["contended_on"] == "relation_lock"
    assert busy["fence_wait_elapsed_ms"] == 250
    assert "error" not in busy
    assert done["mutation_state"] == "committed"
    assert receipt["per_table_totals"]["hydro.river_timeseries"]["after_bytes"] == 40
    assert (receipt["outcome"], receipt["deferred_contended_count"], rc) == ("deferred", 1, 0)
    _assert_valid_2_3(receipt)


def test_relation_lock_arm_compressed_after_all_is_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, receipt, reconciled = _run_main_enforce(
        tmp_path, monkeypatch, _busy_and_done(), compress_chunk=_busy_times_out, reconcile=True
    )

    assert reconciled == ["busy"]
    busy = receipt["selected"][0]
    assert busy["mutation_state"] == "committed"
    assert busy["error"] == "compression committed after lost acknowledgement"
    assert "contended_on" not in busy
    assert (receipt["outcome"], receipt["deferred_contended_count"], rc) == ("partial", 0, 1)
    _assert_valid_2_3(receipt)


def test_relation_lock_arm_with_reconcile_unavailable_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, receipt, reconciled = _run_main_enforce(
        tmp_path,
        monkeypatch,
        _busy_and_done(),
        compress_chunk=_busy_times_out,
        reconcile=RuntimeError("catalog unavailable"),
    )

    assert reconciled == ["busy"]
    busy = receipt["selected"][0]
    assert busy["mutation_state"] == "indeterminate"
    assert busy["error"] == "compress_chunk result indeterminate; exact-target reconciliation unavailable"
    assert "contended_on" not in busy
    assert (receipt["outcome"], rc) == ("partial", 1)
    _assert_valid_2_3(receipt)


def test_fence_and_tick_deadline_deferrals_name_what_they_contended_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default walls: a fence may still start at elapsed <= 3900 - 900 - 300 = 2700 s.
    config = compression.config_from_args(_args(enforce=True), _base_env(tmp_path))
    clock = iter([10.0, 3000.0])
    monkeypatch.setattr(compression, "_monotonic", lambda: next(clock))

    def compress(_dsn: str, chunk: compression.ChunkRow) -> int:
        assert chunk.chunk_name == "busy", "past the tick deadline: must not compress"
        raise fence.FenceContended("hydro.river_timeseries", 900_000)

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=lambda _dsn: _busy_and_done(),
        measure_chunk_bytes=lambda _dsn, _chunk, *, after=False: 100,
        compress_chunk=compress,
        reconcile_chunk_state=lambda _dsn, _chunk: pytest.fail("no reconcile for a deferral"),
        head_sha="a" * 40,
        tick_started_monotonic=0.0,
    )

    fenced, late = receipt["selected"]
    assert (fenced["mutation_state"], fenced["contended_on"], fenced["fence_wait_elapsed_ms"]) == (
        "deferred_contended",
        "fence",
        900_000,
    )
    assert (late["mutation_state"], late["contended_on"]) == ("deferred_contended", "tick_deadline")
    assert "fence_wait_elapsed_ms" not in late
    assert (receipt["outcome"], receipt["deferred_contended_count"]) == ("deferred", 2)
    _assert_valid_2_3(receipt)


def test_a_statement_timeout_of_the_compress_statement_stays_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through the real session, so neither the translation nor the loop can
    # mistake 57014 for contention.
    _install_scripted_driver(monkeypatch, {(_COMPRESS_SQL, ("_timescaledb_internal.busy",)): QueryCanceled()})
    _fence_clock(monkeypatch, 10.0, 10.25)

    rc, receipt, reconciled = _run_main_enforce(tmp_path, monkeypatch, _busy_and_done()[:1])

    assert reconciled == ["busy"]
    [busy] = receipt["selected"]
    assert busy["mutation_state"] == "failed_before_mutation"
    assert busy["error"] == "compress_chunk before mutation failed (QueryCanceled)"
    assert "contended_on" not in busy
    assert (receipt["outcome"], receipt["deferred_contended_count"], rc) == ("partial", 0, 1)
    _assert_valid_2_3(receipt)


# 4.7 receipt schema 2.3 -----------------------------------------------------


def test_refused_lock_and_failed_receipts_carry_the_lock_timeout(tmp_path: Path) -> None:
    config = compression.config_from_args(
        _args(enforce=True), _base_env(tmp_path, override={_LOCK_TIMEOUT_KEY: "2000"})
    )
    refused = compression.build_refused_lock_receipt(config, now_utc=_NOW, head_sha="a" * 40)
    failed = compression.build_failed_receipt(config, now_utc=_NOW, stage="runner", head_sha="a" * 40)
    for receipt in (refused, failed):
        _assert_valid_2_3(receipt, lock_timeout_ms=2000)


def _relation_lock_deferral_2_3() -> dict:
    receipt = _example_receipt()
    receipt.update(outcome="deferred", deferred_contended_count=1)
    receipt["selected"] = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_9_213_chunk",
            **_BUSY_RANGE,
            "before_bytes": 1,
            "after_bytes": None,
            "mutation_state": "deferred_contended",
            "fence_wait_elapsed_ms": 250,
            "contended_on": "relation_lock",
        }
    ]
    return receipt


def test_schema_2_3_example_and_relation_lock_deferral_validate() -> None:
    assert _example_receipt()["schema_version"] == "2.3"
    _assert_valid_2_3(_example_receipt())
    _assert_valid_2_3(_relation_lock_deferral_2_3())


def _commit_the_deferral(receipt: dict) -> None:
    receipt.update(outcome="clean", deferred_contended_count=0)
    receipt["selected"][0].update(mutation_state="committed", after_bytes=1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["budget"].pop("lock_timeout_ms"),
        lambda r: r["selected"][0].pop("contended_on"),
        lambda r: r["selected"][0].update(contended_on="vacuum"),
        _commit_the_deferral,
        lambda r: r["budget"].update(lock_timeout_ms=0),
        # The 2.2 rules hold for 2.3 too.
        lambda r: r["budget"].pop("fence_wait_ms"),
        lambda r: r.pop("deferred_contended_count"),
    ],
    ids=[
        "2.3-budget-without-lock-timeout",
        "2.3-deferral-without-contended-on",
        "unknown-contended-on",
        "contended-on-on-a-committed-chunk",
        "zero-lock-timeout",
        "2.3-budget-without-fence-wait",
        "2.3-without-count",
    ],
)
def test_schema_2_3_rejects_an_incoherent_receipt(mutate: object) -> None:
    receipt = _relation_lock_deferral_2_3()
    mutate(receipt)  # type: ignore[operator]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(_load_schema()).validate(receipt)


def _fence_deferral_2_2() -> dict:
    """A receipt as the 2.2 runner wrote it: no lock timeout, no ``contended_on``."""
    receipt = _relation_lock_deferral_2_3()
    receipt["schema_version"] = "2.2"
    receipt["budget"].pop("lock_timeout_ms")
    receipt["selected"][0].pop("contended_on")
    receipt["selected"][0]["fence_wait_elapsed_ms"] = 900_000
    return receipt


def test_a_2_2_receipt_keeps_validating() -> None:
    jsonschema.validate(_fence_deferral_2_2(), _load_schema())
    jsonschema.Draft7Validator(_load_schema()).validate(_fence_deferral_2_2())
    committed = _fence_deferral_2_2()
    _commit_the_deferral(committed)
    jsonschema.validate(committed, _load_schema())


@pytest.mark.parametrize("dropped", ["budget", "head_sha"])
def test_a_non_failed_2_2_receipt_still_requires_budget_and_head_sha(dropped: str) -> None:
    """The 2.1/2.2 pins above read the example, which is 2.3 since #3380; keep 2.2 pinned on a real 2.2 receipt."""
    receipt = _fence_deferral_2_2()
    jsonschema.validate(receipt, _load_schema())  # the baseline is valid
    receipt.pop(dropped)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(_load_schema()).validate(receipt)


@pytest.mark.parametrize(
    ("version", "leak"),
    [
        ("2.2", "lock_timeout_ms"),
        ("2.2", "contended_on-on-a-deferral"),
        ("2.2", "contended_on-on-a-committed-chunk"),
        ("2.1", "lock_timeout_ms"),
        ("2.1", "contended_on-on-a-committed-chunk"),
    ],
)
def test_a_pre_2_3_receipt_cannot_carry_lock_timeout_evidence(version: str, leak: str) -> None:
    receipt = _fence_deferral_2_2()
    if version == "2.1" or leak == "contended_on-on-a-committed-chunk":
        _commit_the_deferral(receipt)
    if version == "2.1":
        receipt["schema_version"] = "2.1"
        receipt.pop("deferred_contended_count")
        receipt["budget"].pop("fence_wait_ms")
        receipt["selected"][0].pop("fence_wait_elapsed_ms")
    jsonschema.validate(receipt, _load_schema())  # the baseline is valid
    if leak == "lock_timeout_ms":
        receipt["budget"]["lock_timeout_ms"] = 5000
    else:
        receipt["selected"][0]["contended_on"] = "relation_lock"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


@pytest.mark.parametrize("receipt_path", _COMMITTED_RUNNER_RECEIPTS, ids=lambda path: path.name)
def test_a_historical_receipt_with_contended_on_injected_is_rejected(receipt_path: Path) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, _load_schema())
    assert receipt["selected"], "a historical runner receipt without a selected chunk proves nothing here"
    receipt["selected"][0]["contended_on"] = "fence"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())
