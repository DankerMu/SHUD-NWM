#!/usr/bin/env bash
# start-display-api.sh — reproducible hand-launch of the node-27 display-api uvicorn.
# Issue #597 (carried follow-up of PR #596 root-cause): the prior ad-hoc launcher
# at /tmp/start_display.sh did not source infra/env/display.env, so restarts
# silently dropped DATABASE_URL and surfaced as "请选择流域" popups in the
# frontend. This script is the single-command restart wrapper that always
# sources display.env, gracefully replaces the prior uvicorn, relaunches
# detached, and runs a basin_id smoke check before exiting.

set -euo pipefail

# -- repo root resolution (works from any cwd on node-27) ----------------------
if command -v git >/dev/null 2>&1 && git_root=$(git rev-parse --show-toplevel 2>/dev/null); then
    REPO_ROOT="$git_root"
else
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
    REPO_ROOT=$(cd "$script_dir/../.." && pwd)
fi
readonly REPO_ROOT

ENV_FILE="${REPO_ROOT}/infra/env/display.env"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
UVICORN_PATTERN='\.venv/bin/python -m uvicorn apps\.api\.main:app'
REQUIRED_KEYS=(DATABASE_URL NHMS_ENABLE_LIVE_POSTGIS_MVT OBJECT_STORE_ROOT)

# -- preflight ------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file missing: $ENV_FILE" >&2
    echo "       expected on node-27 host; do NOT relaunch without it (would silently drop DATABASE_URL)." >&2
    exit 2
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "ERROR: venv python missing or not executable: $VENV_PYTHON" >&2
    exit 2
fi

UVICORN_HOST="${NHMS_DISPLAY_HOST:-127.0.0.1}"
LOG_PATH="${NHMS_DISPLAY_LOG_PATH:-/tmp/display-api.log}"

# -- source env file (export every key) -----------------------------------------
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# -- assert required env keys are present (do NOT leak values) ------------------
missing=()
for key in "${REQUIRED_KEYS[@]}"; do
    if [[ -z "${!key:-}" ]]; then
        missing+=("$key")
    fi
done
if (( ${#missing[@]} > 0 )); then
    printf 'ERROR: required env keys missing after sourcing %s:\n' "$ENV_FILE" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    echo "  (values redacted; fix display.env and rerun)" >&2
    exit 3
fi

if [[ -v NHMS_DISPLAY_API_PORT ]]; then
    UVICORN_PORT="$NHMS_DISPLAY_API_PORT"
else
    UVICORN_PORT="8080"
fi
if [[ ! "$UVICORN_PORT" =~ ^[0-9]+$ ]] || (( 10#$UVICORN_PORT < 1 || 10#$UVICORN_PORT > 65535 )); then
    echo "ERROR: NHMS_DISPLAY_API_PORT must be a decimal integer from 1 through 65535; got: ${UVICORN_PORT:-<empty>}" >&2
    echo "       fix display.env before restarting; existing uvicorn was not stopped." >&2
    exit 3
fi

if [[ ! -d "$OBJECT_STORE_ROOT" || ! -r "$OBJECT_STORE_ROOT" || ! -x "$OBJECT_STORE_ROOT" ]]; then
    echo "ERROR: OBJECT_STORE_ROOT must be an existing readable and traversable directory: $OBJECT_STORE_ROOT" >&2
    echo "       fix display.env or filesystem permissions before restarting; existing uvicorn was not stopped." >&2
    exit 3
fi

# -- redacted launch preamble ---------------------------------------------------
db_redact=$(printf '%s' "$DATABASE_URL" | sed -E 's#(://)[^@/]+#\1<redacted>#; s#@([^/]+/[^?]*).*$#@\1#')
echo "[start-display-api] repo_root=$REPO_ROOT"
echo "[start-display-api] env_file=$ENV_FILE"
echo "[start-display-api] DATABASE_URL=$db_redact"
echo "[start-display-api] NHMS_ENABLE_LIVE_POSTGIS_MVT=${NHMS_ENABLE_LIVE_POSTGIS_MVT}"
echo "[start-display-api] OBJECT_STORE_ROOT=$OBJECT_STORE_ROOT"
echo "[start-display-api] target=$UVICORN_HOST:$UVICORN_PORT  log=$LOG_PATH"

# -- gracefully replace prior uvicorn -------------------------------------------
prior_pids=$(pgrep -f "$UVICORN_PATTERN" || true)
if [[ -n "$prior_pids" ]]; then
    echo "[start-display-api] stopping prior uvicorn pid(s): $prior_pids"
    # shellcheck disable=SC2086
    kill -TERM $prior_pids 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! pgrep -f "$UVICORN_PATTERN" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    leftover=$(pgrep -f "$UVICORN_PATTERN" || true)
    if [[ -n "$leftover" ]]; then
        echo "[start-display-api] SIGTERM timed out; SIGKILL leftover: $leftover" >&2
        # shellcheck disable=SC2086
        kill -KILL $leftover 2>/dev/null || true
        sleep 1
    fi
else
    echo "[start-display-api] no prior uvicorn process found"
fi

# -- relaunch detached ----------------------------------------------------------
cd "$REPO_ROOT"
setsid nohup "$VENV_PYTHON" -m uvicorn apps.api.main:app \
    --host "$UVICORN_HOST" --port "$UVICORN_PORT" \
    >>"$LOG_PATH" 2>&1 < /dev/null &
new_pid=$!
disown "$new_pid" 2>/dev/null || true
echo "[start-display-api] relaunched pid=$new_pid (log: $LOG_PATH)"

# -- wait for port bind ---------------------------------------------------------
# Probe the root /health endpoint (apps/api/main.py:1947 _register_static_and_health_routes),
# NOT /api/v1/health which doesn't exist (would 404 even on healthy uvicorn).
bound=0
for _ in $(seq 1 30); do
    if curl --silent --show-error --fail --max-time 2 \
        "http://${UVICORN_HOST}:${UVICORN_PORT}/health" >/dev/null 2>&1; then
        bound=1
        break
    fi
    sleep 1
done
if (( bound == 0 )); then
    echo "ERROR: uvicorn did not respond on $UVICORN_HOST:$UVICORN_PORT /health within 30s; check $LOG_PATH" >&2
    exit 4
fi

# -- contract smoke check: basin_id MUST be non-null on at least one model ------
# This is the exact failure mode PR #596 fixed; the smoke check makes env-drift
# regressions surface in the restart command output, not in user-facing popups.
sample=$(curl --silent --show-error --fail --max-time 5 \
    "http://${UVICORN_HOST}:${UVICORN_PORT}/api/v1/models?limit=1")

if ! item_count=$(printf '%s' "$sample" | jq -e '.data.items | length' 2>/dev/null); then
    echo "ERROR: smoke check could not parse /api/v1/models response" >&2
    printf '%s\n' "$sample" >&2
    exit 5
fi
if [[ "$item_count" == "0" ]]; then
    echo "WARN: /api/v1/models returned 0 items; basin_id smoke check skipped (DB may be empty)." >&2
    echo "     verify model registration separately before declaring restart healthy."
    echo "[start-display-api] OK (port bound, but no models to assert basin_id contract)"
    exit 0
fi
if ! basin_id=$(printf '%s' "$sample" | jq -re '.data.items[0].basin_id' 2>/dev/null); then
    echo "ERROR: smoke check: items[0].basin_id is missing or jq parse failed" >&2
    printf '%s\n' "$sample" >&2
    exit 6
fi
if [[ "$basin_id" == "null" || -z "$basin_id" ]]; then
    echo "ERROR: smoke check: items[0].basin_id is null — DATABASE_URL or JOIN drift hazard (PR #596 regression class)" >&2
    printf '%s\n' "$sample" >&2
    exit 7
fi
echo "[start-display-api] OK pid=$new_pid basin_id=$basin_id (smoke check passed)"
