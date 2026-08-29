"""Disposable four-column fixture parity for the isolated-cluster probe (#1892).

The probe fixture tables are exactly ``id``, ``valid_time``, ``value``, and
``payload``. This helper is not a production-column contract: migrations
000005/000006 define different identity and business columns on the live
hypertables. #1893 must derive and validate every real business column for both
production hypertables from the live schema before mutation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_probe.types import ProbeError
from packages.common.compressed_chunk_cold_residency import qualified_ident

PROBE_FIXTURE_PARITY_COLUMNS: tuple[str, ...] = ("id", "valid_time", "value", "payload")


def fixture_window_parity_sql(schema: str, table: str) -> str:
    rel = qualified_ident(schema, table)
    token = (
        "id::text || chr(31) || "
        "to_char(timezone('UTC', valid_time), 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') || chr(31) || "
        "value::text || chr(31) || "
        "CASE WHEN payload IS NULL THEN 'N' ELSE 'P' || payload END"
    )
    return (
        "SELECT count(*)::bigint AS n, "
        "coalesce(sum(value), 0)::float8 AS value_sum, "
        f"coalesce(md5(string_agg({token}, chr(30) ORDER BY id, valid_time)), md5('')) AS checksum "
        f"FROM {rel} "
        "WHERE valid_time >= %s AND valid_time < %s"
    )


def fixture_canonical_parity_token(
    row_id: object,
    valid_time: datetime,
    value: object,
    payload: str | None,
) -> str:
    if valid_time.tzinfo is None or valid_time.utcoffset() is None:
        raise ProbeError("parity valid_time must be timezone-aware")
    stamped = valid_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload_token = "N" if payload is None else f"P{payload}"
    return f"{row_id}\x1f{stamped}\x1f{value}\x1f{payload_token}"


def _row_valid_time(row: Mapping[str, Any]) -> datetime:
    value = row["valid_time"]
    if not isinstance(value, datetime):
        raise ProbeError("parity row valid_time must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProbeError("parity valid_time must be timezone-aware")
    return value.astimezone(UTC)


def fixture_window_parity_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    range_start: datetime,
    range_end: datetime,
) -> dict[str, Any]:
    selected: list[tuple[object, datetime, object, str | None]] = []
    for row in rows:
        valid_time = _row_valid_time(row)
        if valid_time < range_start.astimezone(UTC) or valid_time >= range_end.astimezone(UTC):
            continue
        selected.append((row["id"], valid_time, row["value"], row.get("payload")))
    selected.sort(key=lambda item: (item[0], item[1]))
    tokens = [
        fixture_canonical_parity_token(row_id, valid_time, value, payload)
        for row_id, valid_time, value, payload in selected
    ]
    joined = "\x1e".join(tokens)
    checksum = hashlib.md5(joined.encode("utf-8"), usedforsecurity=False).hexdigest()
    value_sum = float(sum((item[2] or 0) for item in selected))  # type: ignore[arg-type]
    return {"count": len(selected), "value_sum": value_sum, "checksum": checksum}
