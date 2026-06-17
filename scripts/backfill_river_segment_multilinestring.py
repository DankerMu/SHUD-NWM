"""Re-split core.river_segment geometries into gap-aware MultiLineStrings.

After migration 000036 widens ``core.river_segment.geom`` to
``geometry(MultiLineString, 4490)``, existing rows are single-part
MultiLineStrings that STILL carry the fabricated cross-gap straight bridge inside
that one part (the migration only wraps via ST_Multi, it does not split). This
script re-splits every row at those bridges using the SAME gap detector as the
parser / backfill / frontend (``gap_split_positions``), so a genuine source gap
becomes separate parts and is never drawn as a straight jump.

Idempotent: parts are concatenated in stored order and re-split; an
already-correctly-split row re-splits to the identical part set (the gap edge is
re-introduced between the concatenated parts and detected again), so a second run
updates zero rows. The split never adds or moves a vertex, so ``length_m`` and the
total vertex count are untouched.

Usage:
    DATABASE_URL=postgres://user:pass@host:5432/db \
        uv run python scripts/backfill_river_segment_multilinestring.py [--dry-run] \
        [--river-network-version-id RNV] [--batch-size N]

The connection string comes from the DATABASE_URL environment variable (or
config); no credentials are hard-coded. ``--dry-run`` reports how many rows WOULD
change and writes nothing. Restrict to one river network with
``--river-network-version-id`` (default: all rows).

This performs DB WRITES and is meant to be run by operations on the primary
(node-22), not from local development against the real database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from workers.model_registry.basins_geometry import _point_wkt, gap_split_positions

_FETCH_BATCH_SIZE = 1000


def _coordinates_from_geojson(geometry: dict | None) -> list[list[tuple[float, float]]]:
    """Return the row's parts as ordered (lon, lat) tuples from GeoJSON.

    A LineString (legacy / unmigrated) yields one part; a MultiLineString yields
    its parts in stored order. Coordinates beyond lon/lat are ignored for the gap
    metric but the full position is preserved on write by re-emitting from the
    original WKT path -- here we only need lon/lat to decide split boundaries and
    to re-render, matching the 2D WKT the parser/backfill already store.
    """
    if not geometry:
        return []
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if geom_type == "LineString":
        return [[(float(x), float(y)) for x, y, *_rest in coords if x is not None and y is not None]]
    if geom_type == "MultiLineString":
        return [
            [(float(x), float(y)) for x, y, *_rest in part if x is not None and y is not None]
            for part in coords
        ]
    return []


def _multilinestring_wkt(parts: list[list[tuple[float, float]]]) -> str:
    rendered = [
        "(" + ", ".join(_point_wkt(point) for point in part) + ")"
        for part in parts
        if len(part) >= 2
    ]
    return "MULTILINESTRING(" + ", ".join(rendered) + ")"


def _resplit_parts(parts: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]] | None:
    """Concatenate parts in order, re-split at gaps, drop <2-point parts.

    Returns None when the row has no renderable geometry (fewer than two points
    total) so the caller can skip it untouched.
    """
    merged = [point for part in parts for point in part]
    if len(merged) < 2:
        return None
    split = [part for part in gap_split_positions(merged) if len(part) >= 2]
    return split or [merged]


def _parts_equal(a: list[list[tuple[float, float]]], b: list[list[tuple[float, float]]]) -> bool:
    return a == b


def backfill(
    database_url: str,
    *,
    dry_run: bool,
    river_network_version_id: str | None,
    batch_size: int,
) -> dict[str, int]:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    scanned = 0
    changed = 0
    skipped_null = 0

    connection = psycopg2.connect(database_url)
    try:
        connection.autocommit = False
        where = "WHERE geom IS NOT NULL"
        params: list[object] = []
        if river_network_version_id is not None:
            where += " AND river_network_version_id = %s"
            params.append(river_network_version_id)

        with connection.cursor(name="river_segment_resplit", cursor_factory=RealDictCursor) as read_cursor:
            read_cursor.itersize = batch_size
            read_cursor.execute(
                f"""
                SELECT river_segment_id,
                       river_network_version_id,
                       ST_AsGeoJSON(geom)::text AS geojson
                FROM core.river_segment
                {where}
                """,
                tuple(params),
            )
            with connection.cursor() as write_cursor:
                for row in read_cursor:
                    scanned += 1
                    geometry = json.loads(row["geojson"]) if row["geojson"] else None
                    current_parts = _coordinates_from_geojson(geometry)
                    new_parts = _resplit_parts(current_parts)
                    if new_parts is None:
                        skipped_null += 1
                        continue
                    if _parts_equal(current_parts, new_parts):
                        continue
                    changed += 1
                    if dry_run:
                        continue
                    write_cursor.execute(
                        """
                        UPDATE core.river_segment
                        SET geom = ST_GeomFromText(%s, 4490)
                        WHERE river_segment_id = %s
                          AND river_network_version_id = %s
                        """,
                        (
                            _multilinestring_wkt(new_parts),
                            row["river_segment_id"],
                            row["river_network_version_id"],
                        ),
                    )
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {"scanned": scanned, "changed": changed, "skipped_null": skipped_null}


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-split river_segment geom into gap-aware MultiLineStrings.")
    parser.add_argument("--dry-run", action="store_true", help="Report changed-row count without writing.")
    parser.add_argument(
        "--river-network-version-id",
        default=None,
        help="Restrict the backfill to a single river network version (default: all).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_FETCH_BATCH_SIZE,
        help=f"Server-side fetch batch size (default: {_FETCH_BATCH_SIZE}).",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print(json.dumps({"error": "DATABASE_URL is required."}), file=sys.stderr)
        return 2

    result = backfill(
        database_url,
        dry_run=args.dry_run,
        river_network_version_id=args.river_network_version_id,
        batch_size=max(1, args.batch_size),
    )
    result["dry_run"] = args.dry_run
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
