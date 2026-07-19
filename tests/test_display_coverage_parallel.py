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

    def refresh(_connection: Any, run_id: str) -> list[str]:
        barrier.wait()
        return [run_id]

    monkeypatch.setattr(display_coverage.psycopg2, "connect", connect)
    monkeypatch.setattr(display_coverage, "_refresh", refresh)

    result = display_coverage.refresh_all_run_display_coverage(
        object(),
        dsn="postgresql://example",
        workers=2,
    )

    assert result == {"refreshed": 2, "skipped": 0, "failed": 0}
    assert len(connections) == 2
    assert all(connection.commits == 1 and connection.closed for connection in connections)


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
