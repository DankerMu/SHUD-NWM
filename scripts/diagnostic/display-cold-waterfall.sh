#!/usr/bin/env bash
#
# display-cold-waterfall.sh — cold-cache latency waterfall for NHMS display API
#
# Usage:
#   scripts/diagnostic/display-cold-waterfall.sh [--host HOST:PORT] [--runs N]
#
# Defaults:
#   --host 127.0.0.1:8080
#   --runs 3 (median of 3 cold passes per endpoint)
#
# Cold-cache strategy:
#   1. Restart uvicorn (flushes Python-level `cached()` LRU)
#   2. Wait for /api/v1/health 200
#   3. For each endpoint hit ONCE per measurement (no repeats in a single pass)
#   4. Between passes, restart uvicorn again — every measurement is cold
#
# Measures:
#   - /api/v1/health           (sanity)
#   - /api/v1/layers           (canonical 21.8s baseline; spec target < 200 ms p95)
#   - /api/v1/basins
#   - /api/v1/runs?source=best (default discharge — should NOT carry flood_product_ready=true post PR 5/7)
#   - /api/v1/models
#   - /api/v1/queue-depth
#   - /api/v1/pipeline-status
#
# Output:
#   - Markdown table to stdout (waterfall + per-endpoint cold timing + median + p95)
#   - Raw curl-format timings to /tmp/display-cold-waterfall-<timestamp>.tsv
#
# Exit codes:
#   0 success
#   1 health check never came up
#   2 uvicorn restart failed
#   3 missing required tool (curl, jq, awk)

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
      sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

# ---- tool check ----
for tool in curl jq awk sort; do
  command -v "$tool" >/dev/null 2>&1 || { echo "MISSING: $tool" >&2; exit 3; }
done

BASE="http://${HOST}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
RAW_OUT="/tmp/display-cold-waterfall-${TS}.tsv"
echo -e "endpoint\trun\ttime_namelookup\ttime_connect\ttime_starttransfer\ttime_total\thttp_code\tsize_download" > "$RAW_OUT"

# ---- helpers ----
restart_uvicorn() {
  # Find current uvicorn PID for our app, kill it, and let the supervisor (parent bash) relaunch.
  # On node-27 the uvicorn was launched via:
  #   setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 >/tmp/uvicorn-display.log 2>&1
  # so we kill the python process and rely on the operator to have relaunched it OR rely on a supervisor.
  # If --skip-restart is set, this is a no-op (measurements are NOT cold).
  if [[ "$SKIP_RESTART" -eq 1 ]]; then
    return 0
  fi
  local pid
  pid=$(pgrep -f 'uvicorn apps.api.main:app' | head -1 || true)
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 2
    # Relaunch via the canonical command (no supervisor on node-27 currently)
    if [[ -d /home/nwm/NWM ]]; then
      (
        cd /home/nwm/NWM
        # shellcheck disable=SC1091
        set -a; source infra/env/display.env; set +a
        setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 \
          >/tmp/uvicorn-display.log 2>&1 </dev/null &
      )
    fi
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

for run in $(seq 1 "$RUNS"); do
  echo "=== Cold pass ${run}/${RUNS} ===" >&2
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
echo "## Cold-waterfall results (${RUNS} cold passes)"
echo
echo "**Target host**: \`${HOST}\`"
echo "**UTC timestamp**: \`${TS}\`"
echo "**Raw timings**: \`${RAW_OUT}\`"
echo
echo "| Endpoint | TTFB run1 (ms) | TTFB run2 (ms) | TTFB run3 (ms) | Median (ms) | Max (ms) | Spec target |"
echo "|---|---|---|---|---|---|---|"

for ep in "${ENDPOINTS[@]}"; do
  vals=()
  for run in $(seq 1 "$RUNS"); do
    vals+=("${ROUND_TIMES["${ep}|${run}"]:-0}")
  done
  sorted=$(printf '%s\n' "${vals[@]}" | sort -n)
  median=$(echo "$sorted" | awk -v r="$RUNS" 'NR==int((r+1)/2){print}')
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
