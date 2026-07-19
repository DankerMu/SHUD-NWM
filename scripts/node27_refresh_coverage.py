#!/usr/bin/env python3
"""Refresh ``hydro.run_display_coverage`` for the QHH latest-product fast path.

Materializes the per-run station/river display coverage (counts, valid-time
windows, per-variable jsonb) so ``forecast_store`` can serve latest-product
readiness from a cheap ``run_id`` JOIN instead of the deep coverage CTEs. The
computation is identical to the CTE path (see
``packages/common/display_coverage.py``), so the materialized values are a
byte-for-byte stand-in.

Standalone and independent of the ingest scripts — call it after ingest, either
per-run (``--run-id``) or for every parsed/finished QHH run (``--all``).

Examples::

    DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms \\
    python scripts/node27_refresh_coverage.py --all

    DATABASE_URL=... python scripts/node27_refresh_coverage.py \\
        --run-id fcst_gfs_2026061312_basins_qhh_shud
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psycopg2

from packages.common.display_coverage import (
    refresh_all_run_display_coverage,
    refresh_run_display_coverage,
    run_display_coverage_available,
)

LOCAL_DEFAULT = "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh hydro.run_display_coverage materialization.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", help="Refresh coverage for a single run.")
    group.add_argument("--all", action="store_true", help="Refresh coverage for all parsed/finished QHH runs.")
    parser.add_argument(
        "--skip-fresh",
        action="store_true",
        help="With --all, only refresh runs whose coverage is missing or stale (resumable).",
    )
    parser.add_argument("--progress", action="store_true", help="With --all, emit per-run progress to stderr.")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("AUTOPIPE_COVERAGE_WORKERS", "1")),
        choices=range(1, 9),
        help="Independent per-run coverage workers (1-8).",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL") or LOCAL_DEFAULT)
    args = parser.parse_args(argv)

    connection = psycopg2.connect(args.database_url)
    try:
        with connection.cursor() as cursor:
            if not run_display_coverage_available(cursor):
                parser.error(
                    "hydro.run_display_coverage does not exist; apply migration "
                    "000035_qhh_display_coverage_materialization.sql first."
                )
        t0 = time.perf_counter()
        if args.all:
            progress = None
            if args.progress:
                def progress(run_id: str, status: str) -> None:
                    print(f"  {run_id}: {status}", file=sys.stderr, flush=True)

            counts = refresh_all_run_display_coverage(
                connection,
                dsn=args.database_url,
                skip_fresh=args.skip_fresh,
                on_progress=progress,
                workers=args.workers,
            )
            report = {"mode": "all", "skip_fresh": args.skip_fresh, "workers": args.workers, **counts}
        else:
            present = refresh_run_display_coverage(connection, args.run_id)
            report = {"mode": "run", "run_id": args.run_id, "refreshed": present}
        report["elapsed_s"] = round(time.perf_counter() - t0, 3)
    finally:
        connection.close()

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
