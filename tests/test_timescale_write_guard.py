"""Unit tests for ``packages.common.timescale_write_guard``.

Covers the shared helper's requirement scenarios per the fixture in
``openspec/changes/tier-node27-timeseries-storage/design.md``:

* Empty batch short-circuit (AND semantics on ``valid_time_min``/``max``).
* Partial-None batch range refuses (caller bug fails closed).
* Uncompressed batch allowed.
* Compressed chunk overlap raises the named error.
* Catalog lookup exceptions (``OperationalError``, ``QueryCanceled``, generic)
  fail-closed with ``CompressedChunkGuardError``.
* ``range_end`` exclusive AND ``range_start`` inclusive boundaries (a batch
  whose ``valid_time_max == compressed_chunk.range_start`` blocks).
* ``SET LOCAL statement_timeout`` used, ``SET SESSION`` never used; the
  ``DEFAULT`` reset fires in ``finally:`` even when the catalog SELECT
  raised.
* DSN masking in error messages via ``redact_text`` on the actual
  DSN-carrying content (``str(error)``).
* ``HYPERTABLES_GUARDED`` matches the two production hypertables only, and
  the guard refuses any unregistered pair before running SQL.
* Query text asserts ``is_compressed = true`` predicate (drift guard).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common.timescale_write_guard import (
    _COMPRESSED_CHUNK_QUERY,
    HYPERTABLES_GUARDED,
    RUNBOOK_ANCHOR,
    CompressedChunkGuardError,
    CompressedChunkWriteError,
    check_batch_targets_uncompressed,
)


class _FakeCursor:
    """Records ``execute()`` calls and returns a caller-configured row.

    ``row`` is what the guard's ``fetchone()`` returns (a tuple, dict, or
    ``None``). ``raise_on_query`` optionally makes the second ``execute``
    raise, exercising the catalog-error path.
    """

    def __init__(
        self,
        *,
        row: Any = None,
        raise_on_query: BaseException | None = None,
        raise_on_timeout: BaseException | None = None,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._row = row
        self._raise_on_query = raise_on_query
        self._raise_on_timeout = raise_on_timeout

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((statement, parameters))
        if statement.startswith("SET LOCAL"):
            if self._raise_on_timeout is not None:
                raise self._raise_on_timeout
            return
        if self._raise_on_query is not None:
            raise self._raise_on_query

    def fetchone(self) -> Any:
        return self._row


class _FakeOperationalError(Exception):
    """Stand-in for ``psycopg2.OperationalError`` — the guard catches by base ``Exception``."""


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def _tday(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


class _PredicateAwareFakeCursor:
    """Cursor that evaluates the guard's SQL predicate against a modeled chunk row.

    Unlike ``_FakeCursor`` (which returns a caller-configured row unconditionally
    regardless of the query's WHERE clause), this cursor parses the guard's
    ``(hypertable_schema, hypertable_name, valid_time_max, valid_time_min)``
    parameter tuple and evaluates ``range_start <= batch_max AND
    range_end > batch_min`` against each modeled chunk row — mirroring the
    real TimescaleDB catalog behavior at the boundary. This is the
    fake-oracle repair (D2 / D3): boundary tests must exercise the
    predicate, not just the param binding.
    """

    def __init__(
        self,
        *,
        chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        # Each modeled chunk row is a dict with keys:
        #   hypertable_schema, hypertable_name, chunk_schema, chunk_name,
        #   is_compressed (bool), range_start (datetime), range_end (datetime).
        self._chunks = list(chunks or [])
        self._last_row: dict[str, Any] | None = None

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((statement, parameters))
        if statement.startswith("SET LOCAL"):
            return
        if "timescaledb_information.chunks" not in statement:
            return
        # I1: SQL sanity check — assert the guard's catalog query literal is
        # BYTE-IDENTICAL to :data:`_COMPRESSED_CHUNK_QUERY`. Any drift in the
        # guard SQL (predicate, ORDER BY, LIMIT, whitespace) makes this fake
        # fail-fast rather than silently modeling the OLD SQL against the
        # NEW production behavior. This is the class-pattern fake-oracle
        # repair: predicate-aware evaluation is only trustworthy if the
        # SQL under evaluation is the SQL production actually runs.
        assert statement == _COMPRESSED_CHUNK_QUERY, (
            "Guard SQL drift: _PredicateAwareFakeCursor evaluates the Python "
            "predicate against a modeled chunk row, but the guard's SQL "
            "literal has changed. Update this fake's predicate to match the "
            "new SQL, then re-run boundary tests. Diff:\n"
            f"expected={_COMPRESSED_CHUNK_QUERY!r}\nactual={statement!r}"
        )
        # Guard binds params as (schema, name, batch_max, batch_min).
        hypertable_schema, hypertable_name, batch_max, batch_min = parameters
        matching = [
            chunk
            for chunk in self._chunks
            if chunk["hypertable_schema"] == hypertable_schema
            and chunk["hypertable_name"] == hypertable_name
            and chunk["is_compressed"] is True
            and chunk["range_start"] <= batch_max
            and chunk["range_end"] > batch_min
        ]
        # ORDER BY range_start LIMIT 1
        matching.sort(key=lambda chunk: chunk["range_start"])
        self._last_row = matching[0] if matching else None

    def fetchone(self) -> Any:
        if self._last_row is None:
            return None
        return (self._last_row["chunk_schema"], self._last_row["chunk_name"])


def test_empty_batch_short_circuits() -> None:
    cursor = _FakeCursor()
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=None,
        valid_time_max=None,
    )
    assert cursor.executed == []


def test_uncompressed_chunk_allows_write() -> None:
    cursor = _FakeCursor(row=None)
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_t(0),
        valid_time_max=_t(23),
    )
    assert any(stmt.startswith("SET LOCAL statement_timeout") for stmt, _ in cursor.executed)
    assert any("timescaledb_information.chunks" in stmt for stmt, _ in cursor.executed)


def test_compressed_chunk_raises_named_error() -> None:
    cursor = _FakeCursor(row=("_timescaledb_internal", "_hyper_1_1_chunk"))
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    error = exc_info.value
    assert error.chunk_schema == "_timescaledb_internal"
    assert error.chunk_name == "_hyper_1_1_chunk"
    assert error.hypertable_schema == "hydro"
    assert error.hypertable_name == "river_timeseries"
    message = str(error)
    assert "_hyper_1_1_chunk" in message
    assert RUNBOOK_ANCHOR in message
    assert "#43-decompress-procedure" in message


def test_compressed_chunk_raises_from_dict_cursor_row() -> None:
    cursor = _FakeCursor(
        row={"chunk_schema": "_timescaledb_internal", "chunk_name": "_hyper_2_5_chunk"}
    )
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    assert exc_info.value.chunk_name == "_hyper_2_5_chunk"


def test_catalog_lookup_error_raises_guard_error() -> None:
    cursor = _FakeCursor(raise_on_query=_FakeOperationalError("connection reset"))
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    error = exc_info.value
    assert not isinstance(error, CompressedChunkWriteError)
    assert "hydro.river_timeseries" in str(error)
    # No DSN leakage
    assert "postgres://" not in str(error)
    assert "postgresql://" not in str(error)


def test_query_cancelled_raises_guard_error() -> None:
    """Use the real ``psycopg2.errors.QueryCanceled`` if psycopg2 is present.

    Closes MINOR C-te-5: the previous ``_FakeQueryCanceled(Exception)`` was a
    stand-in whose semantics diverged from the real class. Use the actual
    exception type so the guard's ``except Exception`` catch is exercised
    against the type it will see in production. Skip the test if psycopg2 is
    not importable in the test env.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import errors as psycopg2_errors

    cursor = _FakeCursor(raise_on_query=psycopg2_errors.QueryCanceled("statement timeout"))
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    assert not isinstance(exc_info.value, CompressedChunkWriteError)
    assert "met.forcing_station_timeseries" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, psycopg2_errors.QueryCanceled)
    # Silence the linter: psycopg2 alias is only used for the importorskip.
    del psycopg2


def test_boundary_range_end_exclusive_allows_write() -> None:
    """A batch whose ``valid_time_min`` equals a compressed chunk's ``range_end`` is allowed.

    TimescaleDB chunk intervals are ``[range_start, range_end)`` — ``range_end``
    is EXCLUSIVE. The guard's SQL filter ``range_start <= %s AND range_end > %s``
    binds ``(valid_time_max, valid_time_min)``. If a compressed chunk ends at
    exactly ``valid_time_min`` (i.e. ``range_end == valid_time_min``), then
    ``range_end > valid_time_min`` is FALSE — the chunk is excluded, and the
    batch is allowed to write.

    I2: migrated to :class:`_PredicateAwareFakeCursor` so this test exercises
    the exclusive-end predicate against a modeled compressed chunk, not
    against a caller-configured pre-set row. A regression that flipped
    ``range_end > %s`` to ``range_end >= %s`` would produce a matching
    chunk here and the test would fail with ``CompressedChunkWriteError`` —
    the exclusive-end contract is now driven by the predicate.
    """
    # Compressed chunk runs [day 2 00:00, day 3 00:00) — range_end is EXCLUSIVE.
    compressed_chunk = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_1_1_chunk",
        "is_compressed": True,
        "range_start": _tday(2, 0),
        "range_end": _tday(3, 0),
    }
    cursor = _PredicateAwareFakeCursor(chunks=[compressed_chunk])
    # Batch min equals chunk range_end (day 3 00:00), so range_end > min is FALSE.
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_tday(3, 0),
        valid_time_max=_tday(3, 12),
    )
    # verify the query was bound with (max, min) — max first
    query_calls = [
        (stmt, params) for stmt, params in cursor.executed if "chunks" in stmt
    ]
    assert len(query_calls) == 1
    _, params = query_calls[0]
    assert params == ("hydro", "river_timeseries", _tday(3, 12), _tday(3, 0))


def test_set_local_not_set_session() -> None:
    """The guard MUST use SET LOCAL (transaction-scoped) and never SET SESSION.

    Also verifies the guard restores the session default statement_timeout
    after its catalog lookup so subsequent DELETE + INSERT are not clipped
    by the guard's short timeout — golden-path byte-identity preservation.
    """
    cursor = _FakeCursor(row=None)
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_t(0),
        valid_time_max=_t(1),
    )
    session_statements = [stmt for stmt, _ in cursor.executed if stmt.startswith("SET SESSION")]
    assert session_statements == []
    local_statements = [
        stmt for stmt, _ in cursor.executed if stmt.startswith("SET LOCAL statement_timeout")
    ]
    # Two: one setting '5s' before the SELECT, one resetting to DEFAULT after.
    assert len(local_statements) == 2
    assert "'5s'" in local_statements[0]
    assert "DEFAULT" in local_statements[1]


def test_dsn_not_leaked_in_error() -> None:
    """If a catalog error carries a DSN, the guard's reason message masks it."""

    class _DsnBearingError(Exception):
        def __init__(self) -> None:
            super().__init__("psql://user:password@host:5432/db failed")

    cursor = _FakeCursor(raise_on_query=_DsnBearingError())
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    guard_message = str(exc_info.value)
    assert "password" not in guard_message
    # The type name IS exposed but not the DSN string; verify the guard's
    # own message never embeds URL credentials.
    assert "psql://user:password" not in guard_message


def test_hypertables_guarded_constant_only_covers_production_pair() -> None:
    """``HYPERTABLES_GUARDED`` is the drill-exemption assertion source."""
    assert HYPERTABLES_GUARDED == frozenset(
        {
            ("hydro", "river_timeseries"),
            ("met", "forcing_station_timeseries"),
        }
    )


def test_runbook_anchor_matches_expected_github_slug() -> None:
    """The anchor is caller-observable contract; guard drift breaks the runbook link."""
    assert RUNBOOK_ANCHOR.endswith("#43-decompress-procedure")
    assert RUNBOOK_ANCHOR == "docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure"


def test_timeout_error_before_query_also_wraps() -> None:
    """A ``SET LOCAL`` failure (e.g. transaction aborted) still fails closed."""
    cursor = _FakeCursor(raise_on_timeout=_FakeOperationalError("transaction aborted"))
    with pytest.raises(CompressedChunkGuardError):
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )


# ---------------------------------------------------------------------------
# A2 — Boundary at ``range_start`` (inclusive) blocks
# ---------------------------------------------------------------------------


def test_boundary_range_start_inclusive_blocks_write() -> None:
    """A batch whose ``valid_time_max`` equals a compressed chunk's ``range_start`` blocks.

    Design.md documents chunks as ``[range_start, range_end)`` — ``range_start``
    is INCLUSIVE. If a batch's max valid_time equals a compressed chunk's
    ``range_start``, the INSERT would land inside that chunk. Predicate must
    be ``range_start <= batch_max AND range_end > batch_min``.

    This test uses ``_PredicateAwareFakeCursor`` (not the caller-configured
    stub) so the predicate is actually exercised: the fake evaluates the
    boundary comparison against a modeled chunk row rather than blindly
    returning a preset row. A regression of the predicate back to
    ``range_start <  batch_max`` will produce no matching chunk here and
    the test will fail with "no error raised".
    """
    # Compressed chunk starts exactly at day 2 00:00, runs to day 3 00:00.
    compressed_chunk = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_1_1_chunk",
        "is_compressed": True,
        "range_start": _tday(2, 0),
        "range_end": _tday(3, 0),
    }
    cursor = _PredicateAwareFakeCursor(chunks=[compressed_chunk])
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_tday(1, 12),
            valid_time_max=_tday(2, 0),  # equals range_start
        )
    assert exc_info.value.chunk_name == "_hyper_1_1_chunk"
    assert "_hyper_1_1_chunk" in str(exc_info.value)
    # Also verify the predicate branch was actually reached (chunks query fired).
    assert any("timescaledb_information.chunks" in stmt for stmt, _ in cursor.executed)


def test_mixed_chunk_batch_uses_first_compressed_chunk() -> None:
    """When multiple compressed chunks overlap, ORDER BY range_start LIMIT 1 returns the earliest.

    Exercises the ``ORDER BY range_start LIMIT 1`` clause end-to-end via a
    predicate-aware fake with two compressed chunks in the batch window.

    I3: also seeds an UNCOMPRESSED chunk (``is_compressed = False``) whose
    time window would otherwise be selected by an earlier ``range_start``.
    The fake's ``is_compressed is True`` predicate must filter that chunk
    OUT — mirroring the guard SQL's ``is_compressed = true`` clause. If a
    future refactor drops that predicate, this test fails because the
    uncompressed chunk would surface first and the reported chunk_name
    would drift.
    """
    uncompressed_early = {
        # Uncompressed, so filtered out even though its range_start is earliest.
        "hypertable_schema": "met",
        "hypertable_name": "forcing_station_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_2_uncompressed_early",
        "is_compressed": False,
        "range_start": _tday(1, 0),
        "range_end": _tday(2, 0),
    }
    earlier = {
        "hypertable_schema": "met",
        "hypertable_name": "forcing_station_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_2_earlier",
        "is_compressed": True,
        "range_start": _tday(1, 0),
        "range_end": _tday(2, 0),
    }
    later = {
        "hypertable_schema": "met",
        "hypertable_name": "forcing_station_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_2_later",
        "is_compressed": True,
        "range_start": _tday(2, 0),
        "range_end": _tday(3, 0),
    }
    cursor = _PredicateAwareFakeCursor(chunks=[uncompressed_early, later, earlier])
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            valid_time_min=_tday(1, 12),
            valid_time_max=_tday(2, 12),
        )
    assert exc_info.value.chunk_name == "_hyper_2_earlier"
    # Uncompressed chunk MUST NOT surface even though it shares range_start.
    assert exc_info.value.chunk_name != "_hyper_2_uncompressed_early"


# ---------------------------------------------------------------------------
# B2 — Unknown hypertable pair refuses at guard entry
# ---------------------------------------------------------------------------


def test_unknown_pair_refuses_at_guard_entry() -> None:
    """Guard rejects any ``(schema, table)`` outside ``HYPERTABLES_GUARDED``.

    A wire-site typo like ``("hydro", "rivertimeseries")`` MUST NOT silently
    permit a write. The check fires BEFORE any SQL, so the cursor is never
    touched.
    """
    cursor = _FakeCursor(row=None)
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="not_a_real_table",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    assert not isinstance(exc_info.value, CompressedChunkWriteError)
    assert "not_a_real_table" in str(exc_info.value)
    assert "unregistered" in str(exc_info.value)
    # Cursor MUST NOT have been touched — no catalog query, no SET LOCAL.
    assert cursor.executed == []


def test_partial_range_refuses_before_registry_check() -> None:
    """A partial ``(min, None)`` on an unregistered pair fails the partial-range check.

    Ordering-lock (K1): the guard checks the invariants in this order:
    1. Empty-batch (both endpoints ``None``) short-circuits — no SQL, no
       registry check (empty batch is a no-op regardless of table).
    2. Partial batch range (one endpoint ``None``) raises
       ``CompressedChunkGuardError`` with ``"partial batch range"`` in the
       message — BEFORE the registry check.
    3. Unregistered ``(schema, table)`` pair — raises with ``"unregistered"``.
    4. Registered pair with a full batch window — runs the catalog query.

    This test asserts step 2 fires when both an unregistered pair AND a
    partial range are present — the partial-range error MUST beat the
    registry error, and the error message MUST name the partial-range
    branch. Renamed from
    ``test_unknown_pair_refuses_even_for_empty_batch_after_partial_range``
    to reflect the ordering claim.
    """
    cursor = _FakeCursor(row=None)
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="ops",
            hypertable_name="scratch",
            valid_time_min=_t(0),
            valid_time_max=None,
        )
    # Partial-range check fires FIRST, so the error names the partial branch,
    # NOT the registry branch. If a future refactor reorders these two checks,
    # this assertion fires immediately.
    assert "partial batch range" in str(exc_info.value)
    # Cursor MUST NOT have been touched — the partial check fires before
    # any SQL runs.
    assert cursor.executed == []


# ---------------------------------------------------------------------------
# D3 — SQL predicate assertion (belt-and-suspenders drift guard)
# ---------------------------------------------------------------------------


def test_query_asserts_is_compressed_true_predicate() -> None:
    """The catalog query MUST filter on ``is_compressed = true``.

    A future refactor that drops the predicate would silently make the guard
    block on uncompressed chunks too — the whole point of the guard is to
    let uncompressed writes pass. Substring assertion on the constant is a
    cheap drift guard.
    """
    assert "is_compressed = true" in _COMPRESSED_CHUNK_QUERY
    # And the boundary-critical predicate for range_start (inclusive):
    assert "range_start <= %s" in _COMPRESSED_CHUNK_QUERY
    assert "range_end > %s" in _COMPRESSED_CHUNK_QUERY


# ---------------------------------------------------------------------------
# F1 — SET LOCAL DEFAULT reset fires in ``finally:`` even on SELECT failure
# ---------------------------------------------------------------------------


def test_set_local_default_resets_even_when_select_raises() -> None:
    """When the catalog SELECT raises, the ``DEFAULT`` reset MUST still fire.

    Verifies F1: the finally block restores the session default so the
    caller's subsequent DELETE + INSERT are not clipped by the guard's 5s
    cap. The DEFAULT reset is best-effort (may itself raise if the txn is
    aborted); we assert it was attempted at all.
    """
    cursor = _FakeCursor(raise_on_query=_FakeOperationalError("boom"))
    with pytest.raises(CompressedChunkGuardError):
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    default_reset = [
        stmt for stmt, _ in cursor.executed if "DEFAULT" in stmt and stmt.startswith("SET LOCAL")
    ]
    assert len(default_reset) == 1, "SET LOCAL statement_timeout = DEFAULT MUST fire in finally"


# ---------------------------------------------------------------------------
# F2 — Empty-batch AND semantics (partial-None refuses)
# ---------------------------------------------------------------------------


def test_partial_none_range_refuses() -> None:
    """A partial ``(min=None, max=<t>)`` or ``(min=<t>, max=None)`` MUST refuse.

    F2 tightens the empty-batch short-circuit from ``or`` to ``and`` — only
    fully unset windows (both endpoints ``None``, produced naturally by
    ``min(...., default=None)`` on an empty batch) short-circuit; a partial
    ``None`` is a caller bug and fails closed.
    """
    for pair in ((None, _t(1)), (_t(0), None)):
        cursor = _FakeCursor(row=None)
        with pytest.raises(CompressedChunkGuardError) as exc_info:
            check_batch_targets_uncompressed(
                cursor,
                hypertable_schema="hydro",
                hypertable_name="river_timeseries",
                valid_time_min=pair[0],
                valid_time_max=pair[1],
            )
        assert "partial batch range" in str(exc_info.value)
        # Cursor never touched — partial-none check fires before any SQL.
        assert cursor.executed == []
