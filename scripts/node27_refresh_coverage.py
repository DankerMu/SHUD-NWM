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

Overwrite guard (#1446), scoped to the retention window (#2504)
-------------------------------------------------------------

An existing populated row (``segment_count > 0``) whose fresh scan comes back
empty is **refused** unless ``--force`` is passed — as long as the row's stored
``river_valid_time_end`` lies INSIDE the retention window. #1446 built that
guard for the legacy-routed cohort: since #1341 the river scan selects rows by
surrogate key, and after #1342's contract (migration 000060) those runs' rows
went with ``hydro.river_timeseries_legacy``, so a rescan finds nothing and the
materialized counts are the only record left. Inside the window a refusal still
means *investigate* (that cohort, or facts lost for another reason).

Outside the window (#2504 D6) an ordinary refresh writes the scanned count,
zero included. The cutoff is the display watermark
(``packages.common.display_watermark.fetch_display_watermark`` — the anchor the
retention runner uses) minus ``NODE27_TIMESERIES_RETENTION_WINDOW_DAYS``, read
from THIS process's env through
``packages.common.storage.configured_retention_window_days``. After 000060 no
column distinguishes a legacy-routed run from a narrow-routed one and no display
path reads the dropped legacy table, so outside the window every cohort's
vanished facts are equally gone and a populated row only advertises an empty
curve: out-of-window zero is expected convergence. A row whose facts still exist
(chunk not dropped yet) keeps its count, because the refresh rescans real facts.
A NULL stored end never relaxes. Fail-closed: when the window is not in the env
(the autopipe cron sources ``infra/env/node27-ingest.env``; the operator adds
the variable there, equal to the retention env's value) or the watermark cannot
be read, the refresh behaves exactly as before #2504. The JSON report names the
cutoff it used (``expired_cutoff``, ``null`` = no relaxation).

* ``--run-id <in-window legacy run>`` exits **3** and prints one
  ``DISPLAY_COVERAGE_REFRESH_REFUSED run_id=… existing_segment_count=… advice=…``
  line on stderr. Nothing is written.
* ``--all`` counts refusals under ``refused`` in the JSON report and still
  exits 0; the batch is never aborted.
* ``--force`` performs the zeroing deliberately. The intended manual use is
  ``--run-id <run> --force`` — one operator-reviewed run. It also composes with
  ``--all``, which zeroes *every* refused run in the batch in one command; that
  is an explicit operator opt-in and the cron loop never passes it.

``--skip-fresh`` selects runs whose coverage is missing or older than the run's
``updated_at`` (a refused run keeps its old ``refreshed_at``, so it is rescanned
only while already stale), plus — with a cutoff — populated rows whose stored
end is older than the cutoff and that were not refreshed within
``--expired-rescan-hours`` (default 24): an empty one converges to 0 and leaves
the selection, one whose facts survive is rescanned at most once per interval.

``--audit-populated-empty`` is read-only and changes nothing: for every
populated row of an eligible run it probes whether any ``q_down`` fact remains
inside the row's stored valid-time range (one short autocommit statement per
probe, ``statement_timeout`` and ``lock_timeout`` set, so no ``AccessShareLock``
is held across chunks to block retention's ``drop_chunk``) and prints
``{in_window: {total, empty, …}, out_of_window: {total, empty, …},
null_end: {total}, watermark, cutoff}``. NULL-end rows are counted, never
probed. Exit 0, or 2 on a missing window / unreadable watermark / database
error (one ``DISPLAY_COVERAGE_AUDIT_FAILED`` line, no traceback).

Examples::

    DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms \\
    python scripts/node27_refresh_coverage.py --all

    DATABASE_URL=... python scripts/node27_refresh_coverage.py \\
        --run-id fcst_gfs_2026061312_basins_qhh_shud

    NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21 DATABASE_URL=... \\
    python scripts/node27_refresh_coverage.py --audit-populated-empty
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.errors

from packages.common.display_coverage import (
    DEFAULT_EXPIRED_RESCAN_INTERVAL,
    DisplayCoverageRefreshRefused,
    refresh_all_run_display_coverage,
    refresh_run_display_coverage,
    resolve_expired_cutoff,
    run_display_coverage_available,
)
from packages.common.display_watermark import fetch_display_watermark
from packages.common.redaction import redact_database_dsn
from packages.common.storage import RETENTION_WINDOW_ENV, configured_retention_window_days

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


# ---------------------------------------------------------------------------
# #2504 read-only audit: how many populated coverage rows advertise no facts.
# ---------------------------------------------------------------------------

#: Per-probe bounds. Each probe is one autocommit statement, so its
#: AccessShareLock on the chunks it touches ends with the statement; the lock
#: timeout makes a probe that queues behind retention's `drop_chunk` give up
#: instead of extending the queue (the #2355 comment attributes 2026-09-19's
#: retention 55P03 refusal to exactly such a manual probe).
AUDIT_STATEMENT_TIMEOUT_MS = 30_000
AUDIT_LOCK_TIMEOUT_MS = 2_000
#: Run ids printed per probed bucket, so an in-window empty row can be
#: investigated without a second query; the counts are always complete.
AUDIT_SAMPLE_RUN_IDS = 10

# The refresh's eligible set (`_eligible_run_ids`) restricted to populated rows:
# exactly the rows `--all --skip-fresh` can still converge.
_AUDIT_ROWS_SQL = """
    SELECT cov.run_id, h.run_key, cov.river_valid_time_start, cov.river_valid_time_end
    FROM hydro.run_display_coverage cov
    JOIN hydro.hydro_run h ON h.run_id = cov.run_id
    WHERE cov.segment_count > 0
      AND h.run_type = 'forecast'
      AND h.status IN ('succeeded', 'parsed', 'published')
      AND h.cycle_time IS NOT NULL
    ORDER BY cov.run_id
"""

# Bounded to the row's stored valid-time range (chunk exclusion) and to the
# run's surrogate key (000051's index leads with it); EXISTS stops at one row.
_AUDIT_PROBE_SQL = """
    SELECT EXISTS (
        SELECT 1
        FROM hydro.river_timeseries rt
        WHERE rt.run_key = %(run_key)s
          AND rt.variable_e = 'q_down'::hydro.river_variable
          AND (%(start)s::timestamptz IS NULL OR rt.valid_time >= %(start)s::timestamptz)
          AND rt.valid_time <= %(end)s::timestamptz
    ) AS has_facts
"""


class AuditConfigError(RuntimeError):
    """The audit cannot split the rows without a window."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bucket() -> dict[str, Any]:
    return {"total": 0, "empty": 0, "probe_failed": 0, "sample_empty_run_ids": []}


def audit_populated_empty(dsn: str, window_days: int) -> dict[str, Any]:
    """The #2504 detection surface: populated rows whose facts are gone.

    Read-only by construction (``readonly`` session, SELECT only). Bucketed on
    the SAME cutoff the refresh would relax below, so ``out_of_window.empty`` is
    what one ``--all --skip-fresh`` tick with the window configured will lower to
    0 and ``in_window.empty`` is what the guard keeps refusing. A probe that
    hits its statement or lock timeout is counted under ``probe_failed`` (the
    counts stay honest) and the audit continues; any other database error
    propagates (exit 2).
    """
    watermark = fetch_display_watermark(dsn, connect=_attributed_connect)
    cutoff = watermark - timedelta(days=window_days)
    report: dict[str, Any] = {
        "mode": "audit-populated-empty",
        "window_days": window_days,
        "watermark": _iso(watermark),
        "cutoff": _iso(cutoff),
        "in_window": _bucket(),
        "out_of_window": _bucket(),
        "null_end": {"total": 0},
    }
    connection = _attributed_connect(dsn, connect_timeout=10)
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = %s", (AUDIT_STATEMENT_TIMEOUT_MS,))
            cursor.execute("SET lock_timeout = %s", (AUDIT_LOCK_TIMEOUT_MS,))
            cursor.execute(_AUDIT_ROWS_SQL)
            rows = cursor.fetchall()
            for run_id, run_key, start, end in rows:
                if end is None:
                    report["null_end"]["total"] += 1
                    continue
                bucket = report["out_of_window" if end < cutoff else "in_window"]
                bucket["total"] += 1
                try:
                    cursor.execute(_AUDIT_PROBE_SQL, {"run_key": run_key, "start": start, "end": end})
                    (has_facts,) = cursor.fetchone()
                except (psycopg2.errors.QueryCanceled, psycopg2.errors.LockNotAvailable):
                    bucket["probe_failed"] += 1
                    continue
                if not has_facts:
                    bucket["empty"] += 1
                    if len(bucket["sample_empty_run_ids"]) < AUDIT_SAMPLE_RUN_IDS:
                        bucket["sample_empty_run_ids"].append(run_id)
    finally:
        connection.close()
    return report


def _run_audit(dsn: str, env: Mapping[str, str]) -> int:
    t0 = time.perf_counter()
    try:
        window_days = configured_retention_window_days(env)
        if window_days is None:
            raise AuditConfigError(
                f"{RETENTION_WINDOW_ENV} is not set to a positive integer; the audit splits rows on "
                "the retention cutoff and has no default window"
            )
        report = audit_populated_empty(dsn, window_days)
    except Exception as error:  # noqa: BLE001 - one typed line, never a traceback
        reason = " ".join(redact_database_dsn(str(error), dsn).split())
        print(
            f"DISPLAY_COVERAGE_AUDIT_FAILED reason={type(error).__name__}: {reason}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    report["elapsed_s"] = round(time.perf_counter() - t0, 3)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _positive_hours(raw: str) -> timedelta:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"must be a number of hours, got {raw!r}") from error
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive, finite number of hours, got {raw!r}")
    return timedelta(hours=value)


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    env_map = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Refresh hydro.run_display_coverage materialization.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", help="Refresh coverage for a single run.")
    group.add_argument("--all", action="store_true", help="Refresh coverage for all parsed/finished QHH runs.")
    group.add_argument(
        "--audit-populated-empty",
        action="store_true",
        help=(
            "Read-only #2504 audit: count populated coverage rows whose river facts are gone, split "
            "into in_window / out_of_window (on the display watermark minus "
            f"{RETENTION_WINDOW_ENV}) and null_end. Changes nothing."
        ),
    )
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
            "the run under 'refused'. Intended manual use is '--run-id <run> --force' (one "
            "reviewed run); it also composes with --all, which zeroes every refused run in "
            "the batch at once. The cron loop never passes this."
        ),
    )
    parser.add_argument(
        "--expired-rescan-hours",
        type=_positive_hours,
        default=DEFAULT_EXPIRED_RESCAN_INTERVAL,
        help=(
            "With --all --skip-fresh and a configured window (#2504): rescan a populated row whose stored "
            "end is older than the retention cutoff at most once per this many hours (default 24)."
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

    if args.audit_populated_empty:
        return _run_audit(args.database_url, env_map)

    # #2504 D6: resolved ONCE per invocation (a whole --all batch shares it).
    # None -- window absent from this env, or watermark unreadable -- is the
    # pre-#2504 guard exactly.
    expired_cutoff = resolve_expired_cutoff(
        args.database_url,
        configured_retention_window_days(env_map),
        connect=_attributed_connect,
    )

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
                expired_cutoff=expired_cutoff,
                expired_rescan_interval=args.expired_rescan_hours,
            )
            # ``counts`` carries the #1446 ``refused`` key alongside
            # refreshed/skipped/failed; a batch refusal is reported, never fatal.
            report = {"mode": "all", "skip_fresh": args.skip_fresh, "workers": args.workers, **counts}
        else:
            try:
                present = refresh_run_display_coverage(
                    connection, args.run_id, force=args.force, expired_cutoff=expired_cutoff
                )
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
        report["expired_cutoff"] = _iso(expired_cutoff)
        report["elapsed_s"] = round(time.perf_counter() - t0, 3)
    finally:
        connection.close()

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
