#!/usr/bin/env python3
"""Measure the national river-network tile coordinate budget straight from SQL.

Issue #2005 tasks 2.4/2.5 produced their go/no-go numbers with an ad-hoc script
that never landed (recorded as a deviation on that PR and routed to #2017 task
7.2). This is that measurement, checked in, so the next operator reproduces the
receipt instead of rewriting the harness.

Why SQL and not the tile route: `feature_coordinate_count`,
`feature_coordinate_overflow_count` and `coordinate_count` are output columns of
`postgis_tile_sql(...)`; the HTTP route consumes them and exposes none. The route
also answers from `map.tile_cache` and the file cache on the second request, so a
route-based re-measurement proves nothing about the SQL.

Two binds per tile:

* the production bind, `_postgis_tile_params(..., layer=<layer>)` verbatim, whose
  `collection_coordinate_limit` comes from `collection_coordinate_limit(layer)`;
* an effectively unbounded bind (`--unbounded-limit`, default 1e9), identical in
  every other value.

A tile whose two passes disagree on `coordinate_count` was truncated by the
fair-budget window, which is the failure this measurement exists to catch.
Passing `layer=` to `_postgis_tile_params` is not optional: omitting it binds
`:collection_coordinate_limit` to the 50000 default and every tile then "passes"
with the window closing in the wrong place.

The session is opened read-only (`SET TRANSACTION READ ONLY` per tile pass) and
the script never writes to the database.

Exit codes: 0 = every go condition held, 1 = at least one tile failed a go
condition, 2 = usage/connection error.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.routes.hydro_display import _postgis_tile_params  # noqa: E402
from scripts.node27_mvt_prewarm import CHINA_BOUNDS, xyz_tiles  # noqa: E402
from services.tiles.mvt import (  # noqa: E402
    MVT_MAX_COORDINATES,
    collection_coordinate_limit,
    postgis_tile_sql,
)

SUMMARY_SCHEMA = "nhms.node27-river-tile-coordinate-evidence.v1"
DEFAULT_LAYER = "river-network-national"
DEFAULT_ZOOMS = (3, 4, 5, 6, 7)
DEFAULT_UNBOUNDED_LIMIT = 1_000_000_000
# The go condition is the spec's per-feature ceiling, not the bind: a tile that
# merely reaches `feature_coordinate_limit` is already a rollback signal.
FEATURE_COORDINATE_GO_LIMIT = MVT_MAX_COORDINATES

_MEASURED_COLUMNS = (
    "feature_count",
    "feature_coordinate_count",
    "feature_coordinate_overflow_count",
    "coordinate_count",
    "intersecting_feature_count",
    "intersecting_coordinate_count",
    "coordinate_dimension_overflow_count",
    "invalid_property_count",
    "source_identity_count",
)

CSV_COLUMNS = (
    "z",
    "x",
    "y",
    "feature_count",
    "feature_coordinate_count",
    "feature_coordinate_overflow_count",
    "coordinate_count",
    "coordinate_count_unbounded",
    "truncated",
    "tile_bytes",
    "seconds",
)


def parse_zooms(raw: str) -> tuple[int, ...]:
    zooms: list[int] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        value = int(token)
        if value < 0 or value > 14:
            raise ValueError(f"zoom must be between 0 and 14: {value}")
        zooms.append(value)
    if not zooms:
        raise ValueError("at least one zoom is required")
    return tuple(zooms)


def _row_metrics(row: Any) -> dict[str, int]:
    mapping = dict(row) if row is not None else {}
    metrics = {name: int(mapping.get(name) or 0) for name in _MEASURED_COLUMNS}
    tile = mapping.get("tile")
    metrics["tile_bytes"] = len(tile) if tile is not None else 0
    return metrics


def measure_tile(
    session: Any,
    sql: Any,
    *,
    layer: str,
    z: int,
    x: int,
    y: int,
    unbounded_limit: int,
) -> dict[str, Any]:
    """Run both binds for one tile and return the merged per-tile record."""

    production_bind = _postgis_tile_params({}, z=z, x=x, y=y, layer=layer)
    unbounded_bind = {**production_bind, "collection_coordinate_limit": unbounded_limit}

    started = time.perf_counter()
    production = _row_metrics(session.execute(sql, production_bind).mappings().first())
    elapsed = time.perf_counter() - started
    unbounded = _row_metrics(session.execute(sql, unbounded_bind).mappings().first())

    record: dict[str, Any] = {"z": z, "x": x, "y": y, "seconds": round(elapsed, 4)}
    record.update(production)
    record["coordinate_count_unbounded"] = unbounded["coordinate_count"]
    record["truncated"] = production["coordinate_count"] != unbounded["coordinate_count"]
    record["collection_coordinate_limit"] = int(production_bind["collection_coordinate_limit"])
    record["failures"] = _tile_failures(record)
    return record


def _tile_failures(record: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if record["feature_coordinate_count"] >= FEATURE_COORDINATE_GO_LIMIT:
        failures.append("feature_coordinate_count_at_or_above_limit")
    if record["feature_coordinate_overflow_count"] != 0:
        failures.append("feature_coordinate_overflow")
    if record["coordinate_count"] > record["collection_coordinate_limit"]:
        failures.append("coordinate_count_over_collection_limit")
    if record["truncated"]:
        failures.append("truncated_by_collection_budget")
    if record["invalid_property_count"] != 0:
        failures.append("invalid_properties")
    return failures


def summarize(records: Sequence[dict[str, Any]], *, layer: str, unbounded_limit: int) -> dict[str, Any]:
    per_zoom: dict[str, Any] = {}
    for record in records:
        bucket = per_zoom.setdefault(
            str(record["z"]),
            {
                "tiles": 0,
                "max_feature_coordinate_count": 0,
                "max_feature_coordinate_tile": None,
                "max_coordinate_count": 0,
                "max_coordinate_count_tile": None,
                "max_tile_bytes": 0,
                "max_seconds": 0.0,
                "overflow_tiles": 0,
                "truncated_tiles": 0,
                "failed_tiles": 0,
            },
        )
        bucket["tiles"] += 1
        xyz = [record["z"], record["x"], record["y"]]
        if record["feature_coordinate_count"] > bucket["max_feature_coordinate_count"]:
            bucket["max_feature_coordinate_count"] = record["feature_coordinate_count"]
            bucket["max_feature_coordinate_tile"] = xyz
        if record["coordinate_count"] > bucket["max_coordinate_count"]:
            bucket["max_coordinate_count"] = record["coordinate_count"]
            bucket["max_coordinate_count_tile"] = xyz
        bucket["max_tile_bytes"] = max(bucket["max_tile_bytes"], record["tile_bytes"])
        bucket["max_seconds"] = max(bucket["max_seconds"], float(record["seconds"]))
        if record["feature_coordinate_overflow_count"]:
            bucket["overflow_tiles"] += 1
        if record["truncated"]:
            bucket["truncated_tiles"] += 1
        if record["failures"]:
            bucket["failed_tiles"] += 1

    failed = [r for r in records if r["failures"]]
    return {
        "schema": SUMMARY_SCHEMA,
        "layer": layer,
        "collection_coordinate_limit": collection_coordinate_limit(layer),
        "feature_coordinate_go_limit": FEATURE_COORDINATE_GO_LIMIT,
        "unbounded_limit": unbounded_limit,
        "tiles_total": len(records),
        "tiles_failed": len(failed),
        "go": not failed,
        "per_zoom": per_zoom,
        "failed_tiles": [
            {"z": r["z"], "x": r["x"], "y": r["y"], "failures": r["failures"]} for r in failed
        ],
    }


def write_csv(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def _build_session(database_url: str) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(database_url, future=True)
    return Session(engine, future=True)


def run(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    database_url = args.database_url or os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("node27_river_tile_coordinate_evidence: no --database-url and no DATABASE_URL", file=sys.stderr)
        return 2

    zooms = parse_zooms(args.zooms)
    tiles = xyz_tiles(CHINA_BOUNDS, zooms)
    sql = text(postgis_tile_sql(args.layer))

    records: list[dict[str, Any]] = []
    session = _build_session(database_url)
    try:
        for z, x, y in tiles:
            session.execute(text("SET TRANSACTION READ ONLY"))
            records.append(
                measure_tile(
                    session,
                    sql,
                    layer=args.layer,
                    z=z,
                    x=x,
                    y=y,
                    unbounded_limit=args.unbounded_limit,
                )
            )
            session.rollback()
            if args.progress_every and len(records) % args.progress_every == 0:
                print(f"measured {len(records)}/{len(tiles)} tiles", file=sys.stderr, flush=True)
    finally:
        session.close()

    summary = summarize(records, layer=args.layer, unbounded_limit=args.unbounded_limit)
    if args.csv:
        write_csv(Path(args.csv), records)
        summary["csv_path"] = str(args.csv)
    payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if summary["go"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--layer", default=DEFAULT_LAYER)
    parser.add_argument("--zooms", default=",".join(str(z) for z in DEFAULT_ZOOMS))
    parser.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL; use the read-only role")
    parser.add_argument("--unbounded-limit", type=int, default=DEFAULT_UNBOUNDED_LIMIT)
    parser.add_argument("--csv", default=None, help="write the per-tile table here")
    parser.add_argument("--json", default=None, help="write the summary here as well as to stdout")
    parser.add_argument("--progress-every", type=int, default=50, help="0 disables progress lines")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ValueError as exc:
        print(f"node27_river_tile_coordinate_evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
