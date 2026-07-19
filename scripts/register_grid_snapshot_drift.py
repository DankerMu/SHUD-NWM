#!/usr/bin/env python
"""Atomically register a canonical-grid drift and supersede its prior snapshot."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from packages.common.source_identity import normalize_source_id
from workers.grid_registry.input_record import read_input_record
from workers.grid_registry.registry import prepare_snapshot


class GridSnapshotDriftError(RuntimeError):
    pass


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise GridSnapshotDriftError("--effective-at must include a timezone offset.")
    return parsed.astimezone(UTC)


def _sidecar_for_grid(grid_path: Path, effective_at: datetime) -> dict[str, Any]:
    try:
        payload = json.loads(grid_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise GridSnapshotDriftError(f"Cannot read grid definition {grid_path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise GridSnapshotDriftError("grid.json must decode to an object.")
    longitudes = payload.get("longitudes")
    latitudes = payload.get("latitudes")
    if not isinstance(longitudes, list) or not longitudes:
        raise GridSnapshotDriftError("grid.json must contain non-empty longitudes.")
    if not isinstance(latitudes, list) or not latitudes:
        raise GridSnapshotDriftError("grid.json must contain non-empty latitudes.")
    lon_values = [float(value) for value in longitudes]
    lat_values = [float(value) for value in latitudes]
    return {
        "valid_from": effective_at.isoformat(),
        "valid_to": None,
        "download_bbox": {
            "south": min(lat_values),
            "north": max(lat_values),
            "west": min(lon_values),
            "east": max(lon_values),
        },
    }


def register_grid_snapshot_drift(
    *,
    database_url: str,
    source_id: str,
    grid_json: str | Path,
    grid_definition_uri: str,
    effective_at: datetime,
) -> dict[str, Any]:
    """Insert the new immutable snapshot and stale the prior snapshot/caches.

    All database changes share one transaction: the new snapshot and cells,
    prior supersession, and derived station/weight invalidation either commit
    together or roll back together.
    """

    grid_path = Path(grid_json)
    sidecar_payload = _sidecar_for_grid(grid_path, effective_at)
    with tempfile.TemporaryDirectory(prefix="nhms-grid-drift-") as temp_dir:
        sidecar_path = Path(temp_dir) / "grid_snapshot_metadata.json"
        sidecar_path.write_text(
            json.dumps(sidecar_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        record = read_input_record(
            source_id,
            grid_path,
            sidecar_path,
            grid_definition_uri=grid_definition_uri,
        )
    snapshot, cells = prepare_snapshot(record, source_id=source_id)
    new_snapshot_id = uuid4()

    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError as error:
        raise GridSnapshotDriftError("psycopg2 is required for grid drift registration.") from error

    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT grid_snapshot_id, grid_signature
                    FROM met.canonical_grid_snapshot
                    WHERE source_id = %s AND grid_id = %s AND superseded_at IS NULL
                    ORDER BY created_at DESC
                    FOR UPDATE
                    """,
                    (normalize_source_id(source_id), snapshot.grid_id),
                )
                active = cursor.fetchall()
                identical = [row for row in active if str(row[1]) == snapshot.grid_signature]
                if identical:
                    if len(active) != 1:
                        raise GridSnapshotDriftError(
                            "An identical active snapshot coexists with another active drift row; "
                            "manual reconciliation is required."
                        )
                    return {
                        "status": "unchanged",
                        "source_id": snapshot.source_id,
                        "grid_id": snapshot.grid_id,
                        "grid_snapshot_id": str(identical[0][0]),
                        "grid_signature": snapshot.grid_signature,
                        "latitude_order": snapshot.latitude_order,
                    }
                if len(active) != 1:
                    raise GridSnapshotDriftError(
                        f"Expected exactly one active prior snapshot, found {len(active)}."
                    )
                prior_snapshot_id = UUID(str(active[0][0]))
                cursor.execute(
                    """
                    INSERT INTO met.canonical_grid_snapshot (
                        grid_snapshot_id, canonical_grid_key, source_id, grid_id,
                        grid_signature, grid_definition_uri, grid_definition_checksum,
                        longitude_convention, latitude_order, flatten_order,
                        native_resolution, bbox_south, bbox_north, bbox_west, bbox_east,
                        converter_version, valid_from, valid_to, applicable_source_ids,
                        superseded_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL
                    )
                    """,
                    (
                        str(new_snapshot_id),
                        snapshot.canonical_grid_key,
                        snapshot.source_id,
                        snapshot.grid_id,
                        snapshot.grid_signature,
                        snapshot.grid_definition_uri,
                        snapshot.grid_definition_checksum,
                        snapshot.longitude_convention,
                        snapshot.latitude_order,
                        snapshot.flatten_order,
                        snapshot.native_resolution,
                        snapshot.bbox_south,
                        snapshot.bbox_north,
                        snapshot.bbox_west,
                        snapshot.bbox_east,
                        snapshot.converter_version,
                        snapshot.valid_from,
                        snapshot.valid_to,
                        list(snapshot.applicable_source_ids),
                    ),
                )
                execute_values(
                    cursor,
                    """
                    INSERT INTO met.canonical_grid_cell (
                        grid_snapshot_id, grid_cell_id, longitude, latitude, canonical_ordinal
                    ) VALUES %s
                    """,
                    [
                        (
                            str(new_snapshot_id),
                            cell.grid_cell_id,
                            cell.longitude,
                            cell.latitude,
                            cell.canonical_ordinal,
                        )
                        for cell in cells
                    ],
                    page_size=5_000,
                )
                cursor.execute(
                    """
                    UPDATE met.canonical_grid_snapshot
                    SET superseded_at = %s
                    WHERE grid_snapshot_id = %s AND superseded_at IS NULL
                    """,
                    (effective_at, str(prior_snapshot_id)),
                )
                if cursor.rowcount != 1:
                    raise GridSnapshotDriftError("Prior snapshot lost its active state during supersession.")
                cursor.execute(
                    """
                    UPDATE met.met_station
                    SET active_flag = false, superseded_at = %s
                    WHERE grid_snapshot_id = %s AND active_flag = true
                    """,
                    (effective_at, str(prior_snapshot_id)),
                )
                stale_station_count = cursor.rowcount
                cursor.execute(
                    """
                    UPDATE met.interp_weight
                    SET active_flag = false, superseded_at = %s
                    WHERE grid_snapshot_id = %s AND active_flag = true
                    """,
                    (effective_at, str(prior_snapshot_id)),
                )
                stale_weight_count = cursor.rowcount
    finally:
        connection.close()

    return {
        "status": "superseded",
        "source_id": snapshot.source_id,
        "grid_id": snapshot.grid_id,
        "prior_snapshot_id": str(prior_snapshot_id),
        "grid_snapshot_id": str(new_snapshot_id),
        "grid_signature": snapshot.grid_signature,
        "latitude_order": snapshot.latitude_order,
        "cell_count": len(cells),
        "stale_station_count": stale_station_count,
        "stale_weight_count": stale_weight_count,
        "effective_at": effective_at.isoformat(),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--grid-json", required=True)
    parser.add_argument("--grid-definition-uri", required=True)
    parser.add_argument("--effective-at", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.database_url:
        raise GridSnapshotDriftError("--database-url or DATABASE_URL is required.")
    receipt = register_grid_snapshot_drift(
        database_url=args.database_url,
        source_id=args.source_id,
        grid_json=args.grid_json,
        grid_definition_uri=args.grid_definition_uri,
        effective_at=_parse_timestamp(args.effective_at),
    )
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
