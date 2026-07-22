"""Resolve the node-27 display business-time watermark.

Lifecycle age on node-27 is measured from the latest forecast cycle accepted
by the display catalog, never from the host wall clock.  The query deliberately
matches the lightweight latest-product candidate status contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable


class DisplayWatermarkError(RuntimeError):
    """Raised when the display business-time watermark cannot be proven."""


DISPLAY_WATERMARK_SQL = """
SELECT MAX(cycle_time) AS reference_time
FROM hydro.hydro_run
WHERE run_type = 'forecast'
  AND status IN ('succeeded', 'parsed', 'published')
  AND cycle_time IS NOT NULL
"""


def fetch_display_watermark(
    database_url: str,
    *,
    connect: Callable[..., Any] | None = None,
) -> datetime:
    """Return the latest displayable forecast cycle in UTC, fail closed.

    The transaction is explicitly read-only and bounded.  Callers must stop
    lifecycle mutation when this function raises; falling back to wall time
    would silently age data while the business pipeline is stalled.
    """

    dsn = str(database_url or "").strip()
    if not dsn:
        raise DisplayWatermarkError("display watermark database URL is required")
    if connect is None:
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - production dependency
            raise DisplayWatermarkError("psycopg2 is unavailable") from error
        connect = psycopg2.connect

    connection = None
    try:
        connection = connect(dsn, connect_timeout=5)
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute(DISPLAY_WATERMARK_SQL)
            row = cursor.fetchone()
    except Exception as error:
        raise DisplayWatermarkError(
            f"display watermark query failed ({type(error).__name__})"
        ) from error
    finally:
        if connection is not None:
            connection.close()

    value = row[0] if row else None
    if not isinstance(value, datetime):
        raise DisplayWatermarkError("display watermark is unavailable")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DisplayWatermarkError("display watermark must be timezone-aware")
    return value.astimezone(UTC)
