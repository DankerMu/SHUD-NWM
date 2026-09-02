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

Overwrite guard (#1446)
-----------------------

Since issue #1341 the river coverage scan selects rows by surrogate key, so a
legacy (pre-#1340) run whose ``hydro.river_timeseries`` rows still carry NULL
keys computes as ``segment_count = 0``, ``river_sample_count = 0`` and NULL
river valid-time bounds. Overwriting the correct text-era values with that
result drops the run out of latest-product readiness and off the national tile,
and there is no undo — so the upsert now **refuses** it: an existing populated
row is never replaced by an empty scan unless ``--force`` is passed.

* ``--run-id <legacy run>`` exits **3** and prints one
  ``DISPLAY_COVERAGE_REFRESH_REFUSED run_id=… existing_segment_count=… advice=…``
  line on stderr. Nothing is written.
* ``--all`` counts refusals under ``refused`` in the JSON report and still
  exits 0; the batch is never aborted.
* ``--force`` performs the zeroing deliberately (an operator who really wants
  the empty scan materialized).

A refused run keeps its old ``refreshed_at``, so it stays stale and the cron's
``--all --skip-fresh`` loop rescans it every tick until its keys are backfilled
(#1408) or an operator forces it. That standing cost is why ``--skip-fresh``
remains the right default for the cron loop, even though omitting it is no
longer destructive.

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
from typing import Any

import psycopg2

from packages.common.display_coverage import (
    DisplayCoverageRefreshRefused,
    refresh_all_run_display_coverage,
    refresh_run_display_coverage,
    run_display_coverage_available,
)

LOCAL_DEFAULT = "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms"

# #1714: default pg_stat_activity attribution for this component. libpq
# treats fallback_application_name as a default only, so an operator's
# explicit ?application_name=... in DATABASE_URL still wins.
_APPLICATION_NAME = "nhms-refresh-coverage"


def _attributed_connect(*args: Any, **kwargs: Any) -> Any:
    """``psycopg2.connect`` with this component's #1714 identity attached.

    Injected into ``refresh_all_run_display_coverage`` so the per-run worker
    connections it opens itself (up to 8 concurrently, on every autopipe tick
    via ``--all``) are attributed too, not just this script's own connection.
    """
    return psycopg2.connect(*args, fallback_application_name=_APPLICATION_NAME, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh hydro.run_display_coverage materialization.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", help="Refresh coverage for a single run.")
    group.add_argument("--all", action="store_true", help="Refresh coverage for all parsed/finished QHH runs.")
    parser.add_argument(
        "--skip-fresh",
        action="store_true",
        help=(
            "With --all, only refresh runs whose coverage is missing or stale (resumable). "
            "Recommended since #1341: the #1446 guard keeps legacy NULL-key runs from being "
            "zeroed either way, but without --skip-fresh every already-fresh run is rescanned."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite a populated coverage row even when the fresh scan finds no segments "
            "(#1446). Without it such a refresh is refused: --run-id exits 3 and --all counts "
            "the run under 'refused'. The cron loop never passes this."
        ),
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

    connection = _attributed_connect(args.database_url)
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
                connect=_attributed_connect,
                force=args.force,
            )
            # ``counts`` carries the #1446 ``refused`` key alongside
            # refreshed/skipped/failed; a batch refusal is reported, never fatal.
            report = {"mode": "all", "skip_fresh": args.skip_fresh, "workers": args.workers, **counts}
        else:
            try:
                present = refresh_run_display_coverage(connection, args.run_id, force=args.force)
            except DisplayCoverageRefreshRefused as refusal:
                # #1446: a refusal is an expected operator-facing outcome, not a
                # crash. One structured line an operator (or a log scraper) can
                # read, and a distinct exit code the caller can branch on --
                # never a traceback. The finally below still closes the
                # connection.
                print(
                    "DISPLAY_COVERAGE_REFRESH_REFUSED "
                    f"run_id={refusal.run_id} "
                    f"existing_segment_count={refusal.existing_segment_count} "
                    f"advice={refusal.advice}",
                    file=sys.stderr,
                    flush=True,
                )
                return 3
            report = {"mode": "run", "run_id": args.run_id, "refreshed": present}
        report["elapsed_s"] = round(time.perf_counter() - t0, 3)
    finally:
        connection.close()

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
