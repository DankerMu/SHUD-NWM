from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.common.display_watermark import (
    DISPLAY_WATERMARK_SQL,
    DisplayWatermarkError,
    fetch_display_watermark,
)


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None, *, failure: Exception | None = None) -> None:
        self.row = row
        self.failure = failure
        self.statements: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self.failure is not None:
            raise self.failure

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.session: tuple[bool, bool] | None = None
        self.closed = False

    def set_session(self, *, readonly: bool, autocommit: bool) -> None:
        self.session = (readonly, autocommit)

    def cursor(self) -> _Cursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _connector(connection: _Connection):
    def connect(dsn: str, *, connect_timeout: int) -> _Connection:
        assert dsn == "postgresql://display-watermark"
        assert connect_timeout == 5
        return connection

    return connect


def test_fetch_display_watermark_uses_readonly_bounded_display_contract() -> None:
    cursor = _Cursor((datetime(2026, 7, 11, 12, tzinfo=UTC),))
    connection = _Connection(cursor)

    result = fetch_display_watermark(
        "postgresql://display-watermark", connect=_connector(connection)
    )

    assert result == datetime(2026, 7, 11, 12, tzinfo=UTC)
    assert connection.session == (True, False)
    assert connection.closed is True
    assert cursor.statements == ["SET LOCAL statement_timeout = '5s'", DISPLAY_WATERMARK_SQL]


@pytest.mark.parametrize("row", [None, (None,), ("2026-07-11T12:00:00Z",)])
def test_fetch_display_watermark_refuses_missing_or_untyped_truth(
    row: tuple[object, ...] | None,
) -> None:
    connection = _Connection(_Cursor(row))
    with pytest.raises(DisplayWatermarkError, match="unavailable"):
        fetch_display_watermark(
            "postgresql://display-watermark", connect=_connector(connection)
        )
    assert connection.closed is True


def test_fetch_display_watermark_refuses_naive_datetime() -> None:
    connection = _Connection(_Cursor((datetime(2026, 7, 11, 12),)))
    with pytest.raises(DisplayWatermarkError, match="timezone-aware"):
        fetch_display_watermark(
            "postgresql://display-watermark", connect=_connector(connection)
        )


def test_fetch_display_watermark_redacts_database_failure_detail() -> None:
    connection = _Connection(_Cursor(None, failure=RuntimeError("secret dsn")))
    with pytest.raises(DisplayWatermarkError, match=r"query failed \(RuntimeError\)") as raised:
        fetch_display_watermark(
            "postgresql://display-watermark", connect=_connector(connection)
        )
    assert "secret dsn" not in str(raised.value)
    assert connection.closed is True


def test_fetch_display_watermark_requires_database_url() -> None:
    with pytest.raises(DisplayWatermarkError, match="database URL is required"):
        fetch_display_watermark("")
