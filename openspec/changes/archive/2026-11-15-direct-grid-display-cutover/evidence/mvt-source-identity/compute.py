#!/usr/bin/env python3
"""Standalone MVT station source-identity computer.

Mirrors `_station_source_version` from `apps/api/routes/hydro_display.py:582-620`
verbatim so `rehearse.py` can capture the before/after station-MVT source
identity without going through the FastAPI display API.

The identity string returned here MUST be byte-identical to what
`_station_source_version` returns for the same DB state — the flip is
proved by (a) the string differing between the pre-cutover baseline and
the committed target set, and (b) the string returning to the sentinel
`MVT_SOURCE_IDENTITY_NOT_FOUND` after restore (no active-flag=true synth
rows remaining).

Usage (node-27):
    DATABASE_URL="postgresql://nhms:...@127.0.0.1:55432/nhms" \\
    uv run python mvt-source-identity/compute.py <basin_version_id>

Emits the identity string on stdout, or `MVT_SOURCE_IDENTITY_NOT_FOUND` on
the empty-rowset path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

# Verbatim mirror of services/tiles/mvt.py MVT_MAX_FEATURES (line 25).
# Kept as a local constant so this script has no import dependency on the
# API app. If MVT_MAX_FEATURES ever changes in the API, update this literal
# to match — the rehearsal is only meaningful when the two are in sync.
MVT_MAX_FEATURES = 10_000

MVT_SOURCE_IDENTITY_NOT_FOUND = "MVT_SOURCE_IDENTITY_NOT_FOUND"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms")


def _station_active_flag(value) -> bool:
    """Mirror of `apps/api/routes/hydro_display.py::_station_active_flag`."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "t", "true", "yes"}
    return bool(value)


def _format_time(value) -> str | None:
    """Match the API's ISO-8601 formatting for identity-string stability."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compute_station_source_identity(
    cursor: psycopg.Cursor[dict], basin_version_id: str
) -> str:
    """Return the station-MVT source-identity string for a basin_version.

    Verbatim mirror of `apps/api/routes/hydro_display.py::_station_source_version`
    (postgres branch): same SELECT columns, same WHERE, same ORDER BY, same
    LIMIT (MVT_MAX_FEATURES + 1), same digest composition, same output
    template `met-stations:{digest16}:{basin_version_id}:{count}`.
    """
    row_limit = MVT_MAX_FEATURES + 1
    cursor.execute(
        """
        SELECT station_id, basin_version_id, COALESCE(station_name, '') AS station_name,
               station_role, active_flag,
               encode(ST_AsEWKB(geom), 'hex') AS geom,
               created_at
        FROM met.met_station
        WHERE basin_version_id = %s
          AND active_flag = true
        ORDER BY station_id
        LIMIT %s
        """,
        (basin_version_id, row_limit),
    )
    rows = cursor.fetchall()
    if not rows:
        return MVT_SOURCE_IDENTITY_NOT_FOUND
    if len(rows) > MVT_MAX_FEATURES:
        return (
            f"MVT_TILE_BUDGET_EXCEEDED:{basin_version_id}:{len(rows)}"
        )
    basis = {
        "rows": [
            [
                row.get("station_id"),
                row.get("basin_version_id"),
                row.get("station_name"),
                row.get("station_role"),
                _station_active_flag(row.get("active_flag")),
                row.get("geom"),
                _format_time(row.get("created_at")) if row.get("created_at") is not None else None,
            ]
            for row in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"met-stations:{digest}:{basin_version_id}:{len(rows)}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: compute.py <basin_version_id>", file=sys.stderr)
        return 2
    basin_version_id = sys.argv[1]
    with psycopg.connect(_database_url(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            identity = compute_station_source_identity(cursor, basin_version_id)
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
