"""Unit coverage for the one-time ``hydro_run.parsed_at`` backfill (#1789).

The three states the operator actually meets: an empty database (the fresh
node-27 smoke DB, and the guard against a script that divides by zero or
commits nothing), a populated one, and a SECOND run over an already-backfilled
database -- which is not a hypothetical, it is a mandatory step of the deploy
(runs parsed between the backfill and the code pull were parsed by old code
that does not stamp the column, so the script must be re-runnable and must not
overwrite what the new parser wrote).

The fake connection below models the SQL's semantics rather than recording
statements, because "is it idempotent" is a semantic claim: a recorder would
happily prove that a statement missing its ``h.parsed_at IS NULL`` clause was
issued twice. Shape assertions on the shipped SQL are separate, and pin the two
things the fake cannot see: that the aggregate stays key-only and that the
UPDATE keeps its fill-only clause.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.backfill_hydro_run_parsed_at import (
    AGGREGATE_SQL,
    UPDATE_SQL,
    BackfillError,
    run_backfill,
)

_T0 = datetime(2026, 8, 1, tzinfo=UTC)


class _FakeCursor:
    def __init__(self, db: "_FakeDatabase") -> None:
        self.db = db
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...] | None = None) -> None:
        self.db.statements.append(statement)
        normalized = " ".join(statement.split())
        self._rows = []
        self.rowcount = 0
        if normalized.startswith("SET statement_timeout"):
            self.db.timeouts.append(int(normalized.split("=")[1]))
            return
        if normalized.startswith("CREATE TEMP TABLE"):
            aggregate: dict[int, datetime] = {}
            for run_key, created_at in self.db.fact_rows:
                if run_key is None:
                    continue
                previous = aggregate.get(run_key)
                if previous is None or created_at > previous:
                    aggregate[run_key] = created_at
            self.db.temp_table = aggregate
            return
        if normalized.startswith("SELECT h.run_key"):
            self._rows = [
                (run["run_key"],)
                for run in self.db.runs
                if run["parsed_at"] is None and run["run_key"] is not None
            ]
            self._rows.sort()
            return
        if normalized.startswith("UPDATE hydro.hydro_run h"):
            assert self.db.temp_table is not None, "UPDATE ran before the aggregate"
            assert parameters is not None
            batch = set(parameters[0])
            updated = 0
            for run in self.db.runs:
                if run["run_key"] not in batch or run["parsed_at"] is not None:
                    continue
                stamped = self.db.temp_table.get(run["run_key"])
                if stamped is None:
                    continue
                run["parsed_at"] = stamped
                updated += 1
            self.rowcount = updated
            return
        if "FILTER" in normalized:
            stamped = sum(1 for run in self.db.runs if run["parsed_at"] is not None)
            unstamped = sum(1 for run in self.db.runs if run["parsed_at"] is None)
            null_key = sum(
                1 for run in self.db.runs if run["parsed_at"] is None and run["run_key"] is None
            )
            self._rows = [(stamped, unstamped, null_key)]
            return
        raise AssertionError(f"unexpected statement: {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeDatabase:
    def __init__(
        self,
        *,
        runs: list[dict[str, Any]] | None = None,
        fact_rows: list[tuple[int | None, datetime]] | None = None,
    ) -> None:
        self.runs = runs or []
        self.fact_rows = fact_rows or []
        self.temp_table: dict[int, datetime] | None = None
        self.statements: list[str] = []
        self.timeouts: list[int] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _run(run_key: int | None, parsed_at: datetime | None = None) -> dict[str, Any]:
    return {"run_key": run_key, "parsed_at": parsed_at}


def test_empty_database_backfills_nothing_and_still_reports_a_receipt() -> None:
    db = _FakeDatabase()

    receipt = run_backfill(db)

    assert receipt["candidates"] == 0
    assert receipt["updated"] == 0
    assert receipt["stamped_after"] == 0
    assert receipt["unstamped_after"] == 0
    # The aggregate still runs: an empty temp table is what makes the later
    # UPDATE a no-op instead of an error.
    assert db.temp_table == {}
    assert db.commits >= 1


def test_populated_database_stamps_each_run_with_its_latest_fact_row() -> None:
    db = _FakeDatabase(
        runs=[_run(1), _run(2), _run(None)],
        fact_rows=[
            (1, _T0),
            (1, _T0 + timedelta(hours=3)),
            (2, _T0 + timedelta(hours=1)),
            (None, _T0 + timedelta(hours=9)),
        ],
    )

    receipt = run_backfill(db, batch_size=1)

    assert receipt["candidates"] == 2
    assert receipt["updated"] == 2
    assert db.runs[0]["parsed_at"] == _T0 + timedelta(hours=3), "MAX, not an arbitrary row"
    assert db.runs[1]["parsed_at"] == _T0 + timedelta(hours=1)
    # The NULL-key legacy cohort keeps NULL: there is no key to aggregate by.
    # Recorded residual, identical in size to the fact join this replaces.
    assert db.runs[2]["parsed_at"] is None
    assert receipt["unstamped_after"] == 1
    assert receipt["unstamped_null_key_after"] == 1
    # batch_size=1 over two candidates must really be two statements, or the
    # batching is decorative.
    assert len([s for s in db.statements if "UPDATE hydro.hydro_run h" in s]) == 2


def test_rerun_is_idempotent_and_never_overwrites_a_newer_parser_stamp() -> None:
    """The re-run must never walk a parser stamp backwards.

    Two fixture cases, both already stamped by the first pass: (a) run 1, still
    correct, no new fact activity; (b) run 2, whose parsed_at the NEW parser has
    since advanced past the fact rows' MAX. Both must be left alone -- rewriting
    (b) would push parsed_at back below the product mtime and restart the
    per-tick re-ingest loop this whole change removes. Hence updated == 0.

    NOT covered here, deliberately: a run already stamped by the first pass and
    then RE-parsed inside the migrate->deploy gap by OLD code (which does not
    stamp the column). Its parsed_at stays stale, because UPDATE_SQL is
    fill-only. That is acceptable and is not a gap in the fixture: the cost is
    one extra forcing handoff, which either succeeds -- and the new parser's
    unconditional stamp makes the next tick converge -- or lands one recorded
    #1781 terminal decline. Pausing the autopipe timer across the deploy window
    (see the script's deploy sequence) empties the sub-case entirely, so there
    is nothing to assert.
    """
    db = _FakeDatabase(
        runs=[_run(1), _run(2)],
        fact_rows=[(1, _T0), (2, _T0)],
    )

    first = run_backfill(db)
    assert first["updated"] == 2

    fresh_parse = _T0 + timedelta(days=5)
    db.runs[1]["parsed_at"] = fresh_parse
    db.fact_rows.append((2, _T0 + timedelta(minutes=1)))

    second = run_backfill(db)

    assert second["candidates"] == 0
    assert second["updated"] == 0
    assert db.runs[0]["parsed_at"] == _T0
    assert db.runs[1]["parsed_at"] == fresh_parse


def test_a_run_whose_fact_rows_were_all_retention_dropped_stays_null() -> None:
    """No fact rows, no timestamp -- and no crash, no bogus now()."""
    db = _FakeDatabase(runs=[_run(7)], fact_rows=[])

    receipt = run_backfill(db)

    assert receipt["candidates"] == 1
    assert receipt["updated"] == 0
    assert db.runs[0]["parsed_at"] is None


def test_each_phase_arms_its_own_statement_timeout() -> None:
    db = _FakeDatabase(runs=[_run(1)], fact_rows=[(1, _T0)])

    run_backfill(db, aggregate_timeout_ms=1_234, update_timeout_ms=567)

    assert db.timeouts == [1_234, 567]


@pytest.mark.parametrize("batch_size", [0, -1])
def test_a_non_positive_batch_size_is_refused(batch_size: int) -> None:
    """Silent infinite loop / silent no-op, depending on the sign."""
    with pytest.raises(BackfillError):
        run_backfill(_FakeDatabase(), batch_size=batch_size)


def test_a_non_positive_statement_timeout_is_refused() -> None:
    """``SET statement_timeout = 0`` means NO timeout, which is exactly the
    unattended-long-statement shape ADR 0003 records as a mistake."""
    with pytest.raises(BackfillError):
        run_backfill(_FakeDatabase(), aggregate_timeout_ms=0)


def test_the_aggregate_stays_key_only_with_no_compressed_side_pushdown_aid() -> None:
    """Design D4: the ``rt.run_id = ANY(...)`` aid is forbidden HERE too.

    It is faster, and it is wrong for this job: it prunes the fact rows of runs
    whose stored run_id differs from their current binding, so that cohort
    would backfill to NULL -- a degradation the key-only aggregate does not
    have today.
    """
    normalized = " ".join(AGGREGATE_SQL.split())

    assert "GROUP BY run_key" in normalized
    assert "WHERE run_key IS NOT NULL" in normalized
    assert "MAX(created_at) AS parsed_at" in normalized
    assert "run_id" not in normalized


def test_the_update_keeps_its_fill_only_clause() -> None:
    normalized = " ".join(UPDATE_SQL.split())

    assert "AND h.parsed_at IS NULL" in normalized, "without this the script overwrites parser stamps"
    assert "h.run_key = b.run_key" in normalized
    assert "run_id" not in normalized
