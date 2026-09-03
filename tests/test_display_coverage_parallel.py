"""Batch coverage refresh: connection independence and per-run isolation.

``refresh_all_run_display_coverage`` refreshes each run on its own short-lived
connection, over two structurally different code paths -- a list comprehension
for ``workers == 1`` and ``ThreadPoolExecutor.map`` for ``workers > 1``. Covered
here:

* the parallel path really opens one connection per run and commits/closes each;
* #1725 -- one run failing (its ``connect`` raises) leaves the others refreshed
  and is counted once under ``failed``, identically on BOTH worker paths;
* #1725 -- the transaction hygiene of each arm: a run whose ``_refresh`` raises
  rolls its own connection back and commits nothing, and a refused run commits
  (closing its read-only transaction) without a rollback;
* #1446 -- a run the overwrite guard refused is counted under ``refused``,
  never under ``failed``, and never aborts the batch.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from packages.common import display_coverage


class _Connection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_refresh_all_uses_independent_parallel_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = ["run-a", "run-b"]
    connections: list[_Connection] = []
    barrier = threading.Barrier(2, timeout=2)

    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)

    def connect(_dsn: str) -> _Connection:
        connection = _Connection()
        connections.append(connection)
        return connection

    def refresh(_connection: Any, run_id: str, *, force: bool = False) -> display_coverage.RefreshOutcome:
        barrier.wait()
        return display_coverage.RefreshOutcome([run_id], [])

    monkeypatch.setattr(display_coverage.psycopg2, "connect", connect)
    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        workers=2,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 0, "refused": 0}
    assert len(connections) == 2
    assert all(connection.commits == 1 and connection.closed for connection in connections)


@pytest.mark.parametrize("workers", (1, 2))
def test_one_failing_run_does_not_stop_the_other_runs(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
) -> None:
    """#1725: per-run failure isolation, asserted on BOTH worker paths.

    ``workers == 1`` and ``workers > 1`` are two independent dispatch
    constructs; a change that isolates one and not the other used to reach CI
    green. The failure is injected at ``connect`` -- the earliest point inside
    the try, and the one that only isolates if ``open_connection`` was hoisted
    into the try block rather than left in front of it.

    "The failing run saw no commit" is asserted as: three connect attempts, two
    connection objects, both committed once and closed. The failing attempt
    never produced a connection, so there was nothing to commit -- and the
    ``rollback`` arm cannot have touched a survivor either.

    ``connect`` receives only the DSN, so the failure is pinned to the *second
    attempt* rather than to a named run id: with ``workers == 2`` the two paths
    interleave and no run-to-attempt mapping exists. Exactly one of three
    connects raising is the property under test.
    """
    run_ids = ["run-a", "run-b", "run-c"]
    connections: list[_Connection] = []
    lock = threading.Lock()
    attempts = 0

    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)

    def connect(_dsn: str) -> _Connection:
        nonlocal attempts
        with lock:
            attempts += 1
            failing = attempts == 2
        if failing:
            raise RuntimeError("connect refused for the second run")
        connection = _Connection()
        with lock:
            connections.append(connection)
        return connection

    def refresh(_connection: Any, run_id: str, *, force: bool = False) -> display_coverage.RefreshOutcome:
        return display_coverage.RefreshOutcome([run_id], [])

    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        workers=workers,
        connect=connect,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 1, "refused": 0}
    assert attempts == len(run_ids)
    assert len(connections) == 2
    assert all(connection.commits == 1 and connection.closed for connection in connections)
    assert all(connection.rollbacks == 0 for connection in connections)


@pytest.mark.parametrize("workers", (1, 2))
def test_a_run_failing_after_connect_rolls_its_own_connection_back(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
) -> None:
    """#1725: the failure arm's transaction hygiene, on BOTH worker paths.

    The sibling test injects at ``connect``, so the failing run never owns a
    connection and the ``rollback`` arm is never reached -- deleting
    ``conn.rollback()`` leaves it green. Here the failure lands at ``_refresh``,
    *after* ``connect`` succeeded: the run holds an open transaction that only
    the ``except`` arm can close. An uncommitted, un-rolled-back connection
    handed back to the driver is how a batch leaks locks.

    ``_refresh`` receives the run id, so the failure is pinned to a named run
    (``run-b``) rather than to an attempt counter -- deterministic on both
    dispatch paths -- and the connection that run was given is captured from the
    same call, which is what makes "its OWN connection" assertable.
    """
    run_ids = ["run-a", "run-b", "run-c"]
    connections: list[_Connection] = []
    by_run: dict[str, _Connection] = {}
    lock = threading.Lock()

    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)

    def connect(_dsn: str) -> _Connection:
        connection = _Connection()
        with lock:
            connections.append(connection)
        return connection

    def refresh(connection: Any, run_id: str, *, force: bool = False) -> display_coverage.RefreshOutcome:
        with lock:
            by_run[run_id] = connection
        if run_id == "run-b":
            raise RuntimeError("refresh blew up after the connection was open")
        return display_coverage.RefreshOutcome([run_id], [])

    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        workers=workers,
        connect=connect,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 1, "refused": 0}
    assert len(connections) == len(run_ids)
    failed_connection = by_run["run-b"]
    assert failed_connection.rollbacks == 1
    assert failed_connection.commits == 0
    assert failed_connection.closed
    survivors = [by_run["run-a"], by_run["run-c"]]
    assert all(connection.commits == 1 for connection in survivors)
    assert all(connection.rollbacks == 0 for connection in survivors)
    assert all(connection.closed for connection in survivors)


@pytest.mark.parametrize("workers", (1, 2))
def test_a_refused_run_is_counted_apart_from_failures_and_does_not_stop_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
) -> None:
    """#1446: the guard's batch face.

    A legacy populated run comes back from ``_refresh`` in ``refused``, not in
    ``refreshed``. It must land under its own counter -- charging it to
    ``failed`` would make the cron log indistinguishable from a real breakage,
    and charging it to ``skipped`` would hide it behind the "run is not a
    candidate" outcome.

    The refused run's own transaction is asserted too: the guard wrote nothing,
    so its commit is a no-op that closes a read-only transaction -- but dropping
    that commit leaves the transaction open until the driver reaps it, and
    taking the rollback arm instead would misreport a refusal as a failed write.
    """
    run_ids = ["run-a", "run-legacy", "run-c"]
    connections: list[_Connection] = []
    by_run: dict[str, _Connection] = {}
    lock = threading.Lock()

    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)

    def connect(_dsn: str) -> _Connection:
        connection = _Connection()
        with lock:
            connections.append(connection)
        return connection

    def refresh(connection: Any, run_id: str, *, force: bool = False) -> display_coverage.RefreshOutcome:
        with lock:
            by_run[run_id] = connection
        if run_id == "run-legacy":
            return display_coverage.RefreshOutcome([], [run_id])
        return display_coverage.RefreshOutcome([run_id], [])

    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        workers=workers,
        connect=connect,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 0, "refused": 1}
    assert len(connections) == 3
    assert all(connection.closed and connection.rollbacks == 0 for connection in connections)
    refused_connection = by_run["run-legacy"]
    assert refused_connection.commits == 1
    assert refused_connection.rollbacks == 0
    assert refused_connection.closed


def test_force_is_passed_through_to_every_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--force`` has to reach the worker, or the escape hatch is decorative."""
    seen: list[tuple[str, bool]] = []

    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: ["run-a", "run-b"])

    def refresh(_connection: Any, run_id: str, *, force: bool = False) -> display_coverage.RefreshOutcome:
        seen.append((run_id, force))
        return display_coverage.RefreshOutcome([run_id], [])

    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        connect=lambda _dsn: _Connection(),
        force=True,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 0, "refused": 0}
    assert seen == [("run-a", True), ("run-b", True)]


@pytest.mark.parametrize("workers", (0, 9))
def test_refresh_all_rejects_unbounded_worker_count(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
) -> None:
    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: [])

    with pytest.raises(ValueError, match="coverage workers must be between 1 and 8"):
        display_coverage.refresh_all_run_display_coverage(
            object(),
            dsn="postgresql://example",
            workers=workers,
        )
