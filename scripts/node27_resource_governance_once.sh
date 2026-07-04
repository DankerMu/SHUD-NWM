#!/usr/bin/env bash
# node-27 resource governance audit wrapper.

set -u

REPO="${NODE27_RESOURCE_GOVERNANCE_REPO:-/home/nwm/NWM}"
ENV_FILE="${NODE27_RESOURCE_GOVERNANCE_ENV_FILE:-$REPO/infra/env/node27-resource-governance.env}"
BOOTSTRAP_LOG="${NODE27_RESOURCE_GOVERNANCE_BOOTSTRAP_LOG:-/home/nwm/node27-resource-governance.log}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-resource-governance: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo "[$(ts)] node27-resource-governance: BLOCKED rc=2 reason=$reason" >&2
  exit 2
}

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

if [ -z "${DATABASE_URL:-}" ]; then
  blocked "DATABASE_URL_MISSING"
fi

LOG_ROOT="${NODE27_RESOURCE_GOVERNANCE_LOG_ROOT:-/home/nwm/node27-resource-governance-logs}"
case "$LOG_ROOT" in
  /*) ;;
  *) blocked "LOG_ROOT_NOT_ABSOLUTE" ;;
esac
if [ "$LOG_ROOT" = "/" ]; then
  blocked "LOG_ROOT_UNSAFE"
fi
mkdir -p "$LOG_ROOT" 2>/dev/null || blocked "LOG_ROOT_UNWRITABLE"

LOCK_PATH="${NODE27_RESOURCE_GOVERNANCE_LOCK_PATH:-/tmp/node27-resource-governance.lock}"
SUMMARY_PATH="${NODE27_RESOURCE_GOVERNANCE_SUMMARY_PATH:-$LOG_ROOT/resource-governance-$(date -u +%Y%m%dT%H%M%SZ).json}"
LOG_FILE="${NODE27_RESOURCE_GOVERNANCE_LOG_FILE:-$LOG_ROOT/resource-governance.log}"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "[$(ts)] node27-resource-governance: previous run still active, skipping tick" >> "$LOG_FILE"
  exit 0
fi

echo "[$(ts)] node27-resource-governance: start summary=$SUMMARY_PATH" >> "$LOG_FILE"
START=$(date +%s)
cd "$REPO" || blocked "REPO_UNAVAILABLE"

"$REPO/.venv/bin/python" "$REPO/scripts/node27_resource_governance.py" \
  --summary-path "$SUMMARY_PATH" --quiet >> "$LOG_FILE" 2>&1
RC=$?

END=$(date +%s)
echo "[$(ts)] node27-resource-governance: done rc=$RC elapsed_sec=$((END - START)) summary=$SUMMARY_PATH" >> "$LOG_FILE"
exit "$RC"
