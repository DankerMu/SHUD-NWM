#!/usr/bin/env bash
# node-27 timeseries retention wrapper (issue #855).
#
# Systemd oneshot glue that:
#  - refuses on missing / mode-non-0600 / symlinked env file (or when the
#    env-file path itself is not absolute),
#  - creates the log directory if it does not exist and refuses if it is
#    unwritable,
#  - flocks a bootstrap lock so overlapping wrapper invocations skip cleanly
#    without invoking the python runner (the python side also holds a
#    separate DB-scoped lock at NODE27_TIMESERIES_RETENTION_LOCK_PATH),
#  - execs the retention runner with the receipt path threaded through so
#    the timer-generated per-run filename is preserved.

set -u

REPO="${NODE27_TIMESERIES_RETENTION_REPO:-/home/nwm/NWM}"
ENV_FILE="${NODE27_TIMESERIES_RETENTION_ENV_FILE:-$REPO/infra/env/node27-timeseries-retention.env}"
BOOTSTRAP_LOG="${NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG:-/home/nwm/node27-timeseries-retention.log}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-timeseries-retention: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo '{"status":"failed","reason":"'"$reason"'"}' >&2
  exit 2
}

case "$ENV_FILE" in
  /*) ;;
  *) blocked "ENV_FILE_NOT_ABSOLUTE" ;;
esac

if [ -f "$ENV_FILE" ]; then
  if [ -L "$ENV_FILE" ]; then
    blocked "ENV_FILE_SYMLINK_FORBIDDEN"
  fi
  ENV_MODE=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)
  if [ "$ENV_MODE" != "600" ]; then
    blocked "ENV_FILE_MODE_UNSAFE"
  fi
  set -a
  # shellcheck disable=SC1090
  if ! . "$ENV_FILE"; then
    set +a
    blocked "ENV_FILE_SOURCE_FAILED"
  fi
  set +a
else
  blocked "ENV_FILE_MISSING"
fi

LOG_ROOT="${NODE27_TIMESERIES_RETENTION_LOG_ROOT:-/home/nwm/node27-timeseries-retention-logs}"
case "$LOG_ROOT" in
  /*) ;;
  *) blocked "LOG_ROOT_NOT_ABSOLUTE" ;;
esac
if [ "$LOG_ROOT" = "/" ]; then
  blocked "LOG_ROOT_UNSAFE"
fi
mkdir -p "$LOG_ROOT" 2>/dev/null || blocked "LOG_ROOT_UNWRITABLE"

# Python entry + interpreter — allow overrides for tests but default to
# the checked-out repo + its .venv.
PYTHON_BIN=${NODE27_TIMESERIES_RETENTION_PYTHON:-$REPO/.venv/bin/python}
SCRIPT=${NODE27_TIMESERIES_RETENTION_SCRIPT:-$REPO/scripts/node27_timeseries_retention.py}

case "$PYTHON_BIN:$SCRIPT" in
  /*:/*) ;;
  *) blocked "wrapper paths must be absolute" ;;
esac

[ -x "$PYTHON_BIN" ] || blocked "python executable is unavailable"
[ -f "$SCRIPT" ] && [ ! -L "$SCRIPT" ] || blocked "retention entrypoint is unavailable or a symlink"

# Bootstrap lock — protects wrapper reentry. The python runner also holds a
# separate DB-scoped flock at NODE27_TIMESERIES_RETENTION_LOCK_PATH.
BOOTSTRAP_LOCK_PATH="${NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOCK:-/tmp/nhms-node27-timeseries-retention-wrapper.lock}"
SUMMARY_PATH="${NODE27_TIMESERIES_RETENTION_RECEIPT_PATH:-$LOG_ROOT/retention-$(date -u +%Y%m%dT%H%M%SZ).json}"
LOG_FILE="${NODE27_TIMESERIES_RETENTION_LOG_FILE:-$LOG_ROOT/retention.log}"

export NODE27_TIMESERIES_RETENTION_RECEIPT_PATH="$SUMMARY_PATH"

exec 9>"$BOOTSTRAP_LOCK_PATH"
if ! flock -n 9; then
  echo "[$(ts)] node27-timeseries-retention: previous wrapper still active, skipping tick" >> "$LOG_FILE"
  exit 0
fi

echo "[$(ts)] node27-timeseries-retention: start summary=$SUMMARY_PATH" >> "$LOG_FILE"
START=$(date +%s)
cd "$REPO" || blocked "REPO_UNAVAILABLE"

"$PYTHON_BIN" "$SCRIPT" "$@" >> "$LOG_FILE" 2>&1
RC=$?

END=$(date +%s)
echo "[$(ts)] node27-timeseries-retention: done rc=$RC elapsed_sec=$((END - START)) summary=$SUMMARY_PATH" >> "$LOG_FILE"
exit "$RC"
