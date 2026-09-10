"""G3 live engine identity: PG 15.2 family prefix + exact Timescale 2.10.2."""

from __future__ import annotations

from packages.common.compressed_chunk_cold_residency import (
    PINNED_PG_VERSION_PREFIX,
    PINNED_TIMESCALEDB_VERSION,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError


def parse_engine_sql_row(text: str) -> tuple[str, str]:
    """Split ``psql -tA`` ``server_version|extversion`` into two fields."""

    line = str(text).strip()
    if not line or "|" not in line:
        raise Issue1895ReadinessError(
            "engine identity row is malformed",
            code="ENGINE_ROW_INVALID",
            stage="engine",
        )
    server, separator, timescale = line.partition("|")
    if not separator or not server or not timescale or "|" in timescale:
        raise Issue1895ReadinessError(
            "engine identity row is malformed",
            code="ENGINE_ROW_INVALID",
            stage="engine",
        )
    return server, timescale


def assert_pinned_engine(server_version: str, timescaledb_version: str) -> None:
    """PostgreSQL must be the 15.2 family/prefix; Timescale extversion is exact."""

    if not str(server_version).startswith(PINNED_PG_VERSION_PREFIX):
        raise Issue1895ReadinessError(
            "PostgreSQL version is not the pinned 15.2 family",
            code="ENGINE_PG_MISMATCH",
            stage="engine",
        )
    if str(timescaledb_version) != PINNED_TIMESCALEDB_VERSION:
        raise Issue1895ReadinessError(
            "TimescaleDB version is not exactly 2.10.2",
            code="ENGINE_TIMESCALEDB_MISMATCH",
            stage="engine",
        )


def assert_engine_sql_row(text: str) -> tuple[str, str]:
    server, timescale = parse_engine_sql_row(text)
    assert_pinned_engine(server, timescale)
    return server, timescale
