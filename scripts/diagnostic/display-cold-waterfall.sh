#!/usr/bin/env bash
#
# display-cold-waterfall.sh — cold-cache latency waterfall for NHMS display API
#
# Target: node-27 (Linux + bash 5+, pgrep, setsid, /home/nwm/NWM layout,
# infra/env/display.env present). The --host arg supports remote probing of
# the API surface but the uvicorn-restart logic is node-27-specific; pass
# --skip-restart when running off-node and accept that the result is warm.
#
# Usage:
#   scripts/diagnostic/display-cold-waterfall.sh [--host HOST:PORT] [--runs N] [--skip-restart]
#
# Defaults:
#   --host 127.0.0.1:8080
#   --runs 3 (median of N cold passes per endpoint; supports any N >= 1)
#
# Cold-cache strategy:
#   1. Restart uvicorn (flushes Python-level `cached()` LRU; SIGTERM + 5s grace
#      + SIGKILL fallback, always relaunches when not --skip-restart)
#   2. Wait for /healthz 200
#   3. For each endpoint hit ONCE per measurement (no repeats in a single pass)
#   4. Between passes, restart uvicorn again — every measurement is cold
#
# Measures:
#   - /healthz                      (sanity)
#   - /api/v1/layers                (canonical 21.8s baseline; spec target < 200 ms p95)
#   - /api/v1/basins
#   - /api/v1/runs?source=best      (default discharge — should NOT carry flood_product_ready=true post PR 5/7)
#   - /api/v1/models
#   - /api/v1/queue-depth
#   - /api/v1/pipeline-status
#
# Output:
#   - Markdown table to stdout (header dynamically sized for $RUNS; per-endpoint
#     cold timing + median + max; --skip-restart prepends a WARM-mode banner)
#   - Raw curl-format timings to /tmp/display-cold-waterfall-<timestamp>.tsv
#
# Exit codes:
#   0 success
#   1 health check never came up (uvicorn never bound the port)
#   2 uvicorn relaunch failed (env file missing OR launcher subshell aborted)
#   3 missing required tool (curl, jq, awk, sort, pgrep)

set -euo pipefail

# ---- arg parsing ----
HOST="127.0.0.1:8080"
RUNS=3
SKIP_RESTART=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)        HOST="$2"; shift 2 ;;
    --runs)        RUNS="$2"; shift 2 ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    -h|--help)
      sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

# Validate RUNS
if ! [[ "$RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --runs must be a positive integer, got: $RUNS" >&2
  exit 1
fi

# ---- tool check (pgrep required for restart_uvicorn even though skipped on --skip-restart, for clarity) ----
REQUIRED_TOOLS=(curl jq awk sort)
if [[ "$SKIP_RESTART" -eq 0 ]]; then
  REQUIRED_TOOLS+=(pgrep setsid)
fi
for tool in "${REQUIRED_TOOLS[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "MISSING: $tool" >&2; exit 3; }
done

BASE="http://${HOST}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
RAW_OUT="${RAW_OUT_DIR:-/tmp}/display-cold-waterfall-${TS}.tsv"
echo -e "endpoint\trun\ttime_namelookup\ttime_connect\ttime_starttransfer\ttime_total\thttp_code\tsize_download" > "$RAW_OUT"
NWM_ROOT="${NHMS_NWM_ROOT:-/home/nwm/NWM}"
UVICORN_LOG="${UVICORN_LOG:-/tmp/uvicorn-display.log}"

# ---- helpers ----
launch_uvicorn() {
  # node-27-specific launcher: cd, source env, setsid uvicorn, detach.
  # Returns 0 if the env file existed and the launcher subshell exited 0;
  # 1 if env file missing (caller decides whether to fail or proceed).
  if [[ ! -f "${NWM_ROOT}/infra/env/display.env" ]]; then
    echo "[launch_uvicorn] ERROR: ${NWM_ROOT}/infra/env/display.env not found." >&2
    echo "[launch_uvicorn] On node-27 this file should exist; on other hosts use --skip-restart." >&2
    return 1
  fi
  (
    cd "$NWM_ROOT"
    # shellcheck disable=SC1091
    set -a; source infra/env/display.env; set +a
    setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 \
      >"$UVICORN_LOG" 2>&1 </dev/null &
    disown
  )
  return 0
}

restart_uvicorn() {
  # Kill any existing uvicorn matching our app, then always relaunch.
  # SIGTERM -> 5s grace -> SIGKILL fallback. If no existing pid, just launch.
  # If --skip-restart, no-op (caller knows measurements are NOT cold).
  if [[ "$SKIP_RESTART" -eq 1 ]]; then
    return 0
  fi
  local pid
  pid=$(pgrep -f 'uvicorn apps.api.main:app' | head -1 || true)
  if [[ -n "$pid" ]]; then
    if ! kill "$pid" 2>/dev/null; then
      echo "[restart_uvicorn] WARN: kill $pid returned non-zero (pid recycled?); proceeding to relaunch" >&2
    fi
    local g=0
    while [[ $g -lt 5 ]]; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1; g=$((g+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "[restart_uvicorn] SIGTERM after 5s grace did not stop pid $pid — escalating to SIGKILL" >&2
      kill -9 "$pid" 2>/dev/null || true
      sleep 1
    fi
  fi
  if ! launch_uvicorn; then
    echo "[restart_uvicorn] launch_uvicorn returned non-zero; exit 2" >&2
    exit 2
  fi
}

wait_for_health() {
  local i=0
  while [[ $i -lt 60 ]]; do
    if curl -sf -m 2 "${BASE}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i+1))
  done
  return 1
}

measure_one() {
  local endpoint="$1"
  local run="$2"
  local result
  result=$(curl -sS -o /dev/null \
    -w '%{time_namelookup}\t%{time_connect}\t%{time_starttransfer}\t%{time_total}\t%{http_code}\t%{size_download}' \
    --max-time 60 \
    "${BASE}${endpoint}" 2>/dev/null || echo -e "0\t0\t0\t0\tERR\t0")
  echo -e "${endpoint}\t${run}\t${result}" >> "$RAW_OUT"
  echo "$result" | awk -F'\t' '{printf "%.0f\n", $3*1000}'  # ttfb in ms
}

ENDPOINTS=(
  "/healthz"
  "/api/v1/layers"
  "/api/v1/basins"
  "/api/v1/runs?source=best"
  "/api/v1/models"
  "/api/v1/queue-depth"
  "/api/v1/pipeline-status"
)

# ---- main ----
declare -A ROUND_TIMES   # key="endpoint|run" → ttfb_ms

PASS_LABEL="Cold"
if [[ "$SKIP_RESTART" -eq 1 ]]; then
  PASS_LABEL="Warm (--skip-restart)"
fi

for run in $(seq 1 "$RUNS"); do
  echo "=== ${PASS_LABEL} pass ${run}/${RUNS} ===" >&2
  restart_uvicorn
  if ! wait_for_health; then
    echo "health check timeout on pass ${run}" >&2
    exit 1
  fi

  for ep in "${ENDPOINTS[@]}"; do
    ttfb=$(measure_one "$ep" "$run")
    ROUND_TIMES["${ep}|${run}"]="$ttfb"
    echo "  ${ep}: ${ttfb}ms" >&2
  done
done

# ---- aggregate + markdown output ----
echo
if [[ "$SKIP_RESTART" -eq 1 ]]; then
  echo "## Waterfall results — WARM (${RUNS} passes, --skip-restart)"
  echo
  echo "> **NOT cold-cache.** --skip-restart was passed, so Python LRU + Postgres buffers retain state between passes. Use without --skip-restart for cold measurements."
else
  echo "## Cold-waterfall results (${RUNS} cold passes)"
fi
echo
echo "**Target host**: \`${HOST}\`"
echo "**UTC timestamp**: \`${TS}\`"
echo "**Raw timings**: \`${RAW_OUT}\`"
echo
# Dynamic table header sized to $RUNS
header="| Endpoint "
sep="|---"
for run in $(seq 1 "$RUNS"); do
  header+="| TTFB run${run} (ms) "
  sep+="|---"
done
header+="| Median (ms) | Max (ms) | Spec target |"
sep+="|---|---|---|"
echo "$header"
echo "$sep"

for ep in "${ENDPOINTS[@]}"; do
  vals=()
  for run in $(seq 1 "$RUNS"); do
    vals+=("${ROUND_TIMES["${ep}|${run}"]:-0}")
  done
  sorted=$(printf '%s\n' "${vals[@]}" | sort -n)
  # Median: for odd N, the middle value; for even N, average of two middle values
  if (( RUNS % 2 == 1 )); then
    median=$(echo "$sorted" | awk -v r="$RUNS" 'NR==int((r+1)/2){print}')
  else
    median=$(echo "$sorted" | awk -v r="$RUNS" 'NR==r/2{a=$1; getline b; printf "%.0f", (a+b)/2}')
  fi
  max=$(echo "$sorted" | tail -1)
  cells=$(printf '| %s ' "${vals[@]}")
  case "$ep" in
    "/api/v1/layers")  target="< 200 ms (spec)" ;;
    *)                 target="< 500 ms (spec)" ;;
  esac
  echo "| \`${ep}\` ${cells}| ${median} | ${max} | ${target} |"
done

echo
echo "## Cold first-paint waterfall (single cold pass, run 1)"
echo
echo "Sequence consumed by \`loadOverview\` map-bootstrap stage (PR 3/7 split):"
echo
echo "1. \`GET /healthz\` → ${ROUND_TIMES["/healthz|1"]:-N/A} ms"
echo "2. \`GET /api/v1/layers\` → ${ROUND_TIMES["/api/v1/layers|1"]:-N/A} ms (**canonical 21.8s baseline endpoint; was THE bottleneck**)"
echo "3. \`GET /api/v1/basins\` → ${ROUND_TIMES["/api/v1/basins|1"]:-N/A} ms"
echo
echo "Enrichment stage (decoupled, runs in background after map is interactive):"
echo
echo "4. \`GET /api/v1/runs?source=best\` → ${ROUND_TIMES["/api/v1/runs?source=best|1"]:-N/A} ms (default discharge, post PR 5/7 no \`flood_product_ready=true\`)"
echo "5. \`GET /api/v1/models\` → ${ROUND_TIMES["/api/v1/models|1"]:-N/A} ms"
echo "6. \`GET /api/v1/queue-depth\` → ${ROUND_TIMES["/api/v1/queue-depth|1"]:-N/A} ms"
echo "7. \`GET /api/v1/pipeline-status\` → ${ROUND_TIMES["/api/v1/pipeline-status|1"]:-N/A} ms"
echo
echo "## Verification"
echo
echo "Discharge \`/runs\` request MUST NOT include \`flood_product_ready=true\` (PR 5/7 spec):"
echo
echo "\`\`\`bash"
echo "curl -sv '${BASE}/api/v1/runs?source=best' 2>&1 | grep -i 'GET /api/v1/runs'"
echo "\`\`\`"
echo
echo "(Frontend would set \`flood_product_ready=true\` only on \`layer ∈ {flood-return-period, warning-level}\`; this script hits the raw endpoint without that param.)"
