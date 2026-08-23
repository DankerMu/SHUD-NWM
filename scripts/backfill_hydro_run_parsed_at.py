#!/usr/bin/env python3
"""One-time backfill of ``hydro.hydro_run.parsed_at`` (#1789).

Migration 000056 adds the column empty. Until it is filled, every historical
run looks unparsed to ``scripts/node27_autopipeline.py::_already_ingested_runs``
-- harmless for ``published`` runs (the gate is authority-state first) but a
detection blind spot for recompute. This script pays, ONCE, the aggregate that
the completeness criterion used to pay every ~15 minutes.

Shape (design D4), and why it is not the cheaper-looking alternative:

* one full-table single-pass ``GROUP BY run_key`` over the fact table. The
  issue suggested reusing PR #1777's ``rt.run_id = ANY(...)`` compressed-side
  pushdown aid instead. Rejected: that aid also prunes the fact rows of runs
  whose STORED run_id differs from their current binding, so that drifted
  cohort would backfill to NULL -- while the key-only aggregate gives them a
  real timestamp today. Backfilling with the aid would import a degradation
  that does not currently exist.
* ``AND h.parsed_at IS NULL`` makes it idempotent and fill-only. Re-running it
  never overwrites a timestamp the parser wrote. That is a deliberate contract,
  not an optimisation: replacing it with ``GREATEST(h.parsed_at, b.parsed_at)``
  would rewrite every already-stamped row on every re-run and void the "never
  overwrites a parser stamp" guarantee, which is the one property that stops a
  re-run from walking a fresh parser timestamp backwards.

  Being fill-only also bounds what the SECOND, mandatory run can close. It
  closes the migrate->deploy gap only for runs whose FIRST parse happened in
  that gap, i.e. whose ``parsed_at`` is still NULL. A run that the first
  backfill already stamped and that was then genuinely RE-parsed in the gap by
  old code (which does not stamp the column at all) is NOT closed: the
  aggregate does compute the fresher ``MAX(created_at)``, but ``UPDATE_SQL``
  skips the run because ``parsed_at`` is no longer NULL, so its ``parsed_at``
  stays stale. The cost of that is bounded and traced: the run gets exactly one
  extra forcing handoff. If the handoff succeeds, the new code's unconditional
  stamp in ``mark_run_parsed`` corrects the timestamp and the next tick
  converges -- self-healing after one cycle. If the compressed-chunk guard
  blocks it, it lands one recorded #1781 terminal decline (a false-positive
  decline, not a retry loop). Pausing the autopipe timer for the deploy window
  makes this sub-case empty outright: no tick means no in-window re-parse.
* the UPDATE is applied in run_key batches under an explicit
  ``statement_timeout`` so a single statement cannot hold locks unboundedly.
  The aggregate itself is one statement and gets its own, larger budget.

Runs at NULL ``run_key`` (the legacy cohort) have no key to aggregate by and
keep ``parsed_at`` NULL. That is the recorded residual, unchanged in size from
the fact join this column replaces.

Deploy sequence (all six steps, in order):

    pause autopipe timer -> apply migration 000056 -> backfill -> pull code ->
    second (idempotent) backfill -> resume timer

Pausing the timer is not cosmetic. It keeps this script's full sequential scan
of the fact table from competing with the tick's own heavy aggregate, and it
empties the not-closed sub-case described above (a paused tick cannot re-parse
a run inside the deploy window).

Usage (node-27, detached, watched):

    { setsid nohup uv run python scripts/backfill_hydro_run_parsed_at.py \
        --log-file /home/nwm/backfill-1789.log & }
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOGGER = logging.getLogger("backfill_hydro_run_parsed_at")

DEFAULT_BATCH_SIZE = 500
DEFAULT_UPDATE_TIMEOUT_MS = 60_000
DEFAULT_AGGREGATE_TIMEOUT_MS = 3_600_000

TEMP_TABLE = "_parsed_at_backfill"

# The one expensive statement. Key-only, no pushdown aid: the aid would prune
# the drifted-run_id cohort's rows and backfill them to NULL (see module
# docstring).
AGGREGATE_SQL = f"""
CREATE TEMP TABLE {TEMP_TABLE} AS
SELECT run_key, MAX(created_at) AS parsed_at
FROM hydro.river_timeseries
WHERE run_key IS NOT NULL
GROUP BY run_key
"""

CANDIDATES_SQL = """
SELECT h.run_key
FROM hydro.hydro_run h
WHERE h.parsed_at IS NULL
  AND h.run_key IS NOT NULL
ORDER BY h.run_key
"""

# `h.parsed_at IS NULL` is the idempotence clause: fill only, never overwrite.
# `h.run_key = ANY(%s)` is batching on the AUTHORITY table, not a fact-table
# pushdown aid -- it prunes nothing the aggregate already produced.
UPDATE_SQL = f"""
UPDATE hydro.hydro_run h
SET parsed_at = b.parsed_at
FROM {TEMP_TABLE} b
WHERE h.run_key = b.run_key
  AND h.parsed_at IS NULL
  AND h.run_key = ANY(%s)
"""

CENSUS_SQL = """
SELECT count(*) FILTER (WHERE parsed_at IS NOT NULL) AS stamped,
       count(*) FILTER (WHERE parsed_at IS NULL) AS unstamped,
       count(*) FILTER (WHERE parsed_at IS NULL AND run_key IS NULL) AS unstamped_null_key
FROM hydro.hydro_run
"""


class BackfillError(RuntimeError):
    """Operator-facing failure: bad arguments or a missing database URL."""


def _set_statement_timeout(cursor: Any, timeout_ms: int) -> None:
    if timeout_ms <= 0:
        raise BackfillError(f"statement_timeout must be positive, got {timeout_ms}")
    # No placeholder: SET does not take bind parameters. The value is an int
    # parsed by argparse and re-validated above, so nothing user-shaped reaches
    # the statement text.
    cursor.execute(f"SET statement_timeout = {int(timeout_ms)}")


def _census(cursor: Any) -> tuple[int, int, int]:
    cursor.execute(CENSUS_SQL)
    row = cursor.fetchone()
    return (int(row[0]), int(row[1]), int(row[2]))


def run_backfill(
    connection: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    update_timeout_ms: int = DEFAULT_UPDATE_TIMEOUT_MS,
    aggregate_timeout_ms: int = DEFAULT_AGGREGATE_TIMEOUT_MS,
) -> dict[str, Any]:
    """Fill every NULL ``parsed_at`` that the fact table can answer for.

    Returns the receipt the delivery evidence needs: the before/after split of
    the stamped and unstamped cohorts, and how many rows this run updated.
    """
    if batch_size <= 0:
        raise BackfillError(f"batch size must be positive, got {batch_size}")

    with connection.cursor() as cursor:
        stamped_before, unstamped_before, null_key_before = _census(cursor)
        LOGGER.info(
            "before: parsed_at stamped=%d unstamped=%d (of which NULL run_key=%d)",
            stamped_before,
            unstamped_before,
            null_key_before,
        )

        _set_statement_timeout(cursor, aggregate_timeout_ms)
        LOGGER.info("aggregating fact rows by run_key (statement_timeout=%dms)", aggregate_timeout_ms)
        cursor.execute(AGGREGATE_SQL)
        connection.commit()

        cursor.execute(CANDIDATES_SQL)
        candidates = [row[0] for row in cursor.fetchall()]
        LOGGER.info("candidates with a run_key and no parsed_at: %d", len(candidates))

        _set_statement_timeout(cursor, update_timeout_ms)
        updated = 0
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            cursor.execute(UPDATE_SQL, (batch,))
            updated += int(cursor.rowcount or 0)
            connection.commit()
            LOGGER.info(
                "batch %d/%d: %d candidates, %d rows stamped so far",
                offset // batch_size + 1,
                (len(candidates) + batch_size - 1) // batch_size,
                len(batch),
                updated,
            )

        stamped_after, unstamped_after, null_key_after = _census(cursor)
        connection.commit()

    LOGGER.info(
        "after: parsed_at stamped=%d unstamped=%d (of which NULL run_key=%d); updated=%d",
        stamped_after,
        unstamped_after,
        null_key_after,
        updated,
    )
    return {
        "stamped_before": stamped_before,
        "unstamped_before": unstamped_before,
        "unstamped_null_key_before": null_key_before,
        "candidates": len(candidates),
        "updated": updated,
        "stamped_after": stamped_after,
        "unstamped_after": unstamped_after,
        "unstamped_null_key_after": null_key_after,
    }


def _configure_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-time backfill of hydro.hydro_run.parsed_at (#1789).")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", "").strip())
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--update-timeout-ms", type=int, default=DEFAULT_UPDATE_TIMEOUT_MS)
    parser.add_argument("--aggregate-timeout-ms", type=int, default=DEFAULT_AGGREGATE_TIMEOUT_MS)
    parser.add_argument("--log-file", default=None, help="append a copy of the progress log here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_file)
    if not args.database_url:
        LOGGER.error("--database-url (or DATABASE_URL) is required")
        return 2

    import psycopg2

    connection = psycopg2.connect(args.database_url)
    connection.autocommit = False
    try:
        receipt = run_backfill(
            connection,
            batch_size=args.batch_size,
            update_timeout_ms=args.update_timeout_ms,
            aggregate_timeout_ms=args.aggregate_timeout_ms,
        )
    except BackfillError as error:
        connection.rollback()
        LOGGER.error("%s", error)
        return 2
    except Exception:
        connection.rollback()
        LOGGER.exception("backfill failed; nothing from the current batch was committed")
        return 1
    finally:
        connection.close()

    LOGGER.info("receipt: %s", receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
