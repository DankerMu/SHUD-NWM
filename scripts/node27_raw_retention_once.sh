#!/usr/bin/env bash
# node-27 raw source-data retention wrapper.

set -u

CALLER_PYTHONPATH=${PYTHONPATH-}
readonly CALLER_PYTHONPATH
REPO="${NODE27_RAW_RETENTION_REPO:-/home/nwm/NWM}"
ENV_FILE="${NODE27_RAW_RETENTION_ENV_FILE:-$REPO/infra/env/node27-raw-retention.env}"
BOOTSTRAP_LOG="${NODE27_RAW_RETENTION_BOOTSTRAP_LOG:-/home/nwm/node27-raw-retention.log}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-raw-retention: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo "[$(ts)] node27-raw-retention: BLOCKED rc=2 reason=$reason" >&2
  exit 2
}

case "$REPO" in
  *:*) blocked "REPOSITORY_ROOT_PATH_LIST_DELIMITER" ;;
  /*) ;;
  *) blocked "REPOSITORY_ROOT_NOT_ABSOLUTE" ;;
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

REPO="${NODE27_RAW_RETENTION_REPO:-/home/nwm/NWM}"
case "$REPO" in
  *:*) blocked "REPOSITORY_ROOT_PATH_LIST_DELIMITER" ;;
  /*) ;;
  *) blocked "REPOSITORY_ROOT_NOT_ABSOLUTE" ;;
esac
if [ -n "$CALLER_PYTHONPATH" ]; then
  PYTHONPATH="$REPO:$CALLER_PYTHONPATH"
else
  PYTHONPATH="$REPO"
fi
export PYTHONPATH

if [ -z "${NODE27_RAW_RETENTION_OBJECT_STORE_ROOT:-}" ]; then
  blocked "OBJECT_STORE_ROOT_MISSING"
fi
case "$NODE27_RAW_RETENTION_OBJECT_STORE_ROOT" in
  /*) ;;
  *) blocked "OBJECT_STORE_ROOT_NOT_ABSOLUTE" ;;
esac

LOG_ROOT="${NODE27_RAW_RETENTION_LOG_ROOT:-/home/nwm/node27-raw-retention-logs}"
case "$LOG_ROOT" in
  /*) ;;
  *) blocked "LOG_ROOT_NOT_ABSOLUTE" ;;
esac
if [ "$LOG_ROOT" = "/" ]; then
  blocked "LOG_ROOT_UNSAFE"
fi
mkdir -p "$LOG_ROOT" 2>/dev/null || blocked "LOG_ROOT_UNWRITABLE"

PYTHON_BIN="$REPO/.venv/bin/python"
SCRIPT="$REPO/scripts/node27_raw_retention.py"
[ -x "$PYTHON_BIN" ] || blocked "python executable is unavailable"
if [ ! -f "$SCRIPT" ] || [ -L "$SCRIPT" ]; then
  blocked "raw retention entrypoint is unavailable or a symlink"
fi

if ! "$PYTHON_BIN" -c '
import importlib.machinery
import os
import sys

root = os.path.realpath(sys.argv[1])
expected_namespace = os.path.join(root, "scripts")
spec = importlib.machinery.PathFinder.find_spec("scripts", sys.path[1:])
locations = (
    []
    if spec is None or spec.submodule_search_locations is None
    else [os.path.realpath(path) for path in spec.submodule_search_locations]
)
valid = (
    spec is not None
    and spec.origin is None
    and locations
    and all(path == expected_namespace for path in locations)
)
raise SystemExit(0 if valid else 1)
' "$REPO"; then
  blocked "SCRIPTS_IMPORT_ORIGIN_OUTSIDE_REPOSITORY_ROOT"
fi

LOCK_PATH="${NODE27_RAW_RETENTION_LOCK_PATH:-/tmp/node27-raw-retention.lock}"
SUMMARY_PATH="${NODE27_RAW_RETENTION_SUMMARY_PATH:-$LOG_ROOT/raw-retention-$(date -u +%Y%m%dT%H%M%SZ).json}"
LOG_FILE="${NODE27_RAW_RETENTION_LOG_FILE:-$LOG_ROOT/raw-retention.log}"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "[$(ts)] node27-raw-retention: previous run still active, skipping tick" >> "$LOG_FILE"
  exit 0
fi

echo "[$(ts)] node27-raw-retention: start summary=$SUMMARY_PATH" >> "$LOG_FILE"
START=$(date +%s)
cd "$REPO" || blocked "REPO_UNAVAILABLE"

"$PYTHON_BIN" "$SCRIPT" \
  --summary-path "$SUMMARY_PATH" >> "$LOG_FILE" 2>&1
RC=$?

END=$(date +%s)
echo "[$(ts)] node27-raw-retention: done rc=$RC elapsed_sec=$((END - START)) summary=$SUMMARY_PATH" >> "$LOG_FILE"
exit "$RC"
