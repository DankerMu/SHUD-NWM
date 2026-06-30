#!/bin/bash
# node-27 bounded source download wrapper.
#
# Source infra/env/node27-download.env, then run one GFS/IFS download pass
# through scripts/node27_download_cycles.py. NODE27_DOWNLOAD_CYCLE_TIME may be
# set for an explicit backfill; when it is empty, the Python runner selects the
# latest allowed UTC business cycle after NODE27_DOWNLOAD_CYCLE_DELAY_HOURS.

set -u

REPO="${NODE27_DOWNLOAD_REPO:-/home/nwm/NWM}"
DOWNLOAD_ENV="${NODE27_DOWNLOAD_ENV_FILE:-$REPO/infra/env/node27-download.env}"
ALLOW_AMBIENT_ENV="${NODE27_DOWNLOAD_ALLOW_AMBIENT_ENV:-0}"
BOOTSTRAP_LOG="${NODE27_DOWNLOAD_BOOTSTRAP_LOG:-/home/nwm/node27-download.log}"
DOWNLOAD_ENV_STRICT_SOURCE=0

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

bootstrap_blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-download: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo "[$(ts)] node27-download: BLOCKED rc=2 reason=$reason" >&2
  exit 2
}

if [ -f "$DOWNLOAD_ENV" ]; then
  case "$DOWNLOAD_ENV" in
    *display.env|*display.example|*display-readonly-secrets.env)
      bootstrap_blocked "DOWNLOAD_ENV_DISPLAY_RUNTIME_FORBIDDEN"
      ;;
  esac
  if [ -L "$DOWNLOAD_ENV" ]; then
    bootstrap_blocked "DOWNLOAD_ENV_SYMLINK_FORBIDDEN"
  fi
  ENV_MODE=$(stat -c '%a' "$DOWNLOAD_ENV" 2>/dev/null || stat -f '%Lp' "$DOWNLOAD_ENV" 2>/dev/null || true)
  if [ "$ENV_MODE" != "600" ]; then
    bootstrap_blocked "DOWNLOAD_ENV_MODE_UNSAFE"
  fi
  if [ "$ALLOW_AMBIENT_ENV" != "1" ]; then
    DOWNLOAD_ENV_STRICT_SOURCE=1
    unset DATABASE_URL
    unset NHMS_NODE27_DOWNLOAD_ROLE
    unset NHMS_SERVICE_ROLE
    unset OBJECT_STORE_ROOT
    unset WORKSPACE_ROOT
    unset NODE27_DOWNLOAD_LOG_ROOT
    unset NODE27_DOWNLOAD_LOCK_PATH
    unset NODE27_DOWNLOAD_CYCLE_TIME
    unset NODE27_DOWNLOAD_SOURCES
    unset NODE27_DOWNLOAD_SUMMARY_PATH
    unset PGUSER
    unset PGPASSWORD
    unset PGPASSFILE
    unset PGSERVICE
    unset PGSERVICEFILE
  fi
  set -a
  # shellcheck disable=SC1090
  if ! . "$DOWNLOAD_ENV"; then
    set +a
    bootstrap_blocked "DOWNLOAD_ENV_SOURCE_FAILED"
  fi
  set +a
  export NHMS_NODE27_DOWNLOAD_CONFIG_SOURCE="env_file:$DOWNLOAD_ENV"
elif [ "$ALLOW_AMBIENT_ENV" = "1" ]; then
  export NHMS_NODE27_DOWNLOAD_CONFIG_SOURCE="${NHMS_NODE27_DOWNLOAD_CONFIG_SOURCE:-ambient:NODE27_DOWNLOAD_ALLOW_AMBIENT_ENV}"
else
  bootstrap_blocked "DOWNLOAD_ENV_MISSING"
fi

if [ "${NHMS_NODE27_DOWNLOAD_ROLE:-}" != "node27_data_plane_download" ]; then
  bootstrap_blocked "DOWNLOAD_ROLE_REQUIRED"
fi

if [ "$DOWNLOAD_ENV_STRICT_SOURCE" = "1" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    bootstrap_blocked "DATABASE_URL_MISSING"
  fi
  if [ -z "${OBJECT_STORE_ROOT:-}" ]; then
    bootstrap_blocked "OBJECT_STORE_ROOT_MISSING"
  fi
  if [ -z "${WORKSPACE_ROOT:-}" ]; then
    bootstrap_blocked "WORKSPACE_ROOT_MISSING"
  fi
  if [ -z "${NODE27_DOWNLOAD_LOG_ROOT:-}" ]; then
    bootstrap_blocked "NODE27_DOWNLOAD_LOG_ROOT_MISSING"
  fi
fi

mkdir -p "$NODE27_DOWNLOAD_LOG_ROOT" 2>/dev/null || bootstrap_blocked "NODE27_DOWNLOAD_LOG_ROOT_UNWRITABLE"
LOG="${NODE27_DOWNLOAD_LOG_FILE:-$NODE27_DOWNLOAD_LOG_ROOT/download.log}"
SUMMARY="${NODE27_DOWNLOAD_SUMMARY_PATH:-$NODE27_DOWNLOAD_LOG_ROOT/last-summary.json}"

cd "$REPO" || bootstrap_blocked "REPO_UNAVAILABLE"
echo "[$(ts)] node27-download: start cycle=${NODE27_DOWNLOAD_CYCLE_TIME:-auto}" >> "$LOG"
"$REPO/.venv/bin/python" "$REPO/scripts/node27_download_cycles.py" \
  --cycle-time "${NODE27_DOWNLOAD_CYCLE_TIME:-}" \
  --summary-path "$SUMMARY" >> "$LOG" 2>&1
RC=$?
echo "[$(ts)] node27-download: done rc=$RC summary=$SUMMARY" >> "$LOG"
exit "$RC"
