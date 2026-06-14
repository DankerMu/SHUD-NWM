#!/bin/bash
# node-27 autopipeline cron wrapper.
#
# Periodically scans the object-store for new basins/runs and ingests them via
# scripts/node27_autopipeline.py (seed registry -> register -> mirror -> parse
# -> refresh-coverage). Idempotent: already-seeded basins and already-parsed
# runs are detected and skipped, so re-running every N minutes is safe and only
# does outstanding work.
#
# flock guards against overlapping runs (a long ingest must not be re-entered by
# the next cron tick). All output (timestamped + the JSON summary) is appended
# to /home/nwm/autopipe.log.
#
# Install (every 10 minutes):
#   crontab -e   # then add:
#   */10 * * * * /home/nwm/NWM/scripts/node27_autopipe_cron.sh >> /home/nwm/autopipe.log 2>&1

set -u

REPO=/home/nwm/NWM
LOG=/home/nwm/autopipe.log
LOCK=/tmp/autopipe.cron.lock

export OBJECT_STORE_ROOT="${OBJECT_STORE_ROOT:-/home/ghdc/nwm/object-store}"
export OBJECT_STORE_PREFIX="${OBJECT_STORE_PREFIX:-s3://nhms}"
export DATABASE_URL="${DATABASE_URL:-postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms}"
export BASINS_ROOT="${BASINS_ROOT:-/home/ghdc/nwm/Basins}"
# Seed scratch (multi-GB basin copies) on the big /home volume, never the small /.
export AUTOPIPE_WORK_ROOT="${AUTOPIPE_WORK_ROOT:-/home/nwm/autopipe-work}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Non-blocking lock: if a previous run is still going, skip this tick.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(ts)] autopipe: previous run still active, skipping tick" >> "$LOG"
  exit 0
fi

echo "[$(ts)] autopipe: start" >> "$LOG"
START=$(date +%s)

cd "$REPO" || { echo "[$(ts)] autopipe: cannot cd $REPO" >> "$LOG"; exit 1; }
"$REPO/.venv/bin/python" "$REPO/scripts/node27_autopipeline.py" >> "$LOG" 2>&1
RC=$?

# Backstop: materialize display coverage for any run still missing/stale it so
# latest-product keeps the <1s fast path (per-run refresh is wired in the
# autopipeline, but a run that failed mid-refresh, or coverage seeded out of
# band, is healed here). Owned by Mission-4; we only invoke it. --skip-fresh
# makes this cheap + resumable. Non-fatal: never masks the ingest exit code.
if [ -f "$REPO/scripts/node27_refresh_coverage.py" ]; then
  echo "[$(ts)] autopipe: coverage backstop (--all --skip-fresh)" >> "$LOG"
  "$REPO/.venv/bin/python" "$REPO/scripts/node27_refresh_coverage.py" --all --skip-fresh >> "$LOG" 2>&1 \
    || echo "[$(ts)] autopipe: coverage backstop rc=$? (non-fatal)" >> "$LOG"
fi

END=$(date +%s)
echo "[$(ts)] autopipe: done rc=$RC elapsed_sec=$((END - START))" >> "$LOG"
exit "$RC"
