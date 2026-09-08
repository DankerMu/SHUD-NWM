#!/usr/bin/env bash
# node-27 MVT tile file-cache retention wrapper (issue #2032).
#
# Trimmed from scripts/node27_raw_retention_once.sh. Two deliberate omissions:
#   * no `scripts` namespace import-origin probe -- this runner imports nothing
#     from the repository (stdlib only), so there is no import to hijack;
#   * no NHMS_MVT_FILE_CACHE_DIR check -- the runner's own preflight blocks on
#     it and writes a `preflight_blocked` JSON receipt, which a wrapper-level
#     refusal would swallow.

set -u

REPO="${NODE27_MVT_CACHE_RETENTION_REPO:-/home/nwm/NWM}"
ENV_FILE="${NODE27_MVT_CACHE_RETENTION_ENV_FILE:-$REPO/infra/env/node27-mvt-cache-retention.env}"
BOOTSTRAP_LOG="${NODE27_MVT_CACHE_RETENTION_BOOTSTRAP_LOG:-/home/nwm/node27-mvt-cache-retention.log}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-mvt-cache-retention: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo "[$(ts)] node27-mvt-cache-retention: BLOCKED rc=2 reason=$reason" >&2
  exit 2
}

case "$REPO" in
  *:*) blocked "REPOSITORY_ROOT_PATH_LIST_DELIMITER" ;;
  /*) ;;
  *) blocked "REPOSITORY_ROOT_NOT_ABSOLUTE" ;;
esac

# A dangling symlink is a symlink, not a missing file: test -L before test -f.
if [ -L "$ENV_FILE" ]; then
  blocked "ENV_FILE_SYMLINK_FORBIDDEN"
fi
if [ -f "$ENV_FILE" ]; then
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

REPO="${NODE27_MVT_CACHE_RETENTION_REPO:-/home/nwm/NWM}"
case "$REPO" in
  *:*) blocked "REPOSITORY_ROOT_PATH_LIST_DELIMITER" ;;
  /*) ;;
  *) blocked "REPOSITORY_ROOT_NOT_ABSOLUTE" ;;
esac

LOG_ROOT="${NODE27_MVT_CACHE_RETENTION_LOG_ROOT:-/home/nwm/node27-mvt-cache-retention-logs}"
case "$LOG_ROOT" in
  /*) ;;
  *) blocked "LOG_ROOT_NOT_ABSOLUTE" ;;
esac
if [ "$LOG_ROOT" = "/" ]; then
  blocked "LOG_ROOT_UNSAFE"
fi
mkdir -p "$LOG_ROOT" 2>/dev/null || blocked "LOG_ROOT_UNWRITABLE"

PYTHON_BIN="$REPO/.venv/bin/python"
SCRIPT="$REPO/scripts/node27_mvt_cache_retention.py"
[ -x "$PYTHON_BIN" ] || blocked "PYTHON_EXECUTABLE_UNAVAILABLE"
if [ ! -f "$SCRIPT" ] || [ -L "$SCRIPT" ]; then
  blocked "MVT_CACHE_RETENTION_ENTRYPOINT_UNAVAILABLE"
fi

LOCK_PATH="${NODE27_MVT_CACHE_RETENTION_LOCK_PATH:-/tmp/node27-mvt-cache-retention.lock}"
SUMMARY_PATH="${NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH:-$LOG_ROOT/mvt-cache-retention-$(date -u +%Y%m%dT%H%M%SZ).json}"
LOG_FILE="${NODE27_MVT_CACHE_RETENTION_LOG_FILE:-$LOG_ROOT/mvt-cache-retention.log}"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "[$(ts)] node27-mvt-cache-retention: previous run still active, skipping tick" >> "$LOG_FILE"
  exit 0
fi

echo "[$(ts)] node27-mvt-cache-retention: start summary=$SUMMARY_PATH" >> "$LOG_FILE"
START=$(date +%s)
cd "$REPO" || blocked "REPO_UNAVAILABLE"

"$PYTHON_BIN" "$SCRIPT" \
  --summary-path "$SUMMARY_PATH" >> "$LOG_FILE" 2>&1
RC=$?

END=$(date +%s)
echo "[$(ts)] node27-mvt-cache-retention: done rc=$RC elapsed_sec=$((END - START)) summary=$SUMMARY_PATH" >> "$LOG_FILE"
exit "$RC"
