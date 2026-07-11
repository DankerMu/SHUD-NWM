#!/usr/bin/env bash
# Phase B end-to-end runner for the direct-grid-display-cutover rehearsal.
#
# Runs on node-27. Chains: provisioning/00 -> 01 -> 02 -> 03 -> rehearse.py
# (with playwright-capture.sh in parallel during the screenshot window) ->
# rehearse.py's own restore section on any failure.
#
# Usage:
#     ssh -p 32099 nwm@210.77.77.27 \\
#       'cd /home/nwm/NWM && \\
#        git fetch origin feat/issue-999-node27-rehearsal-receipt && \\
#        git checkout feat/issue-999-node27-rehearsal-receipt && \\
#        bash openspec/changes/direct-grid-display-cutover/evidence/rehearse/run-on-node27.sh'
set -euo pipefail

EVIDENCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PROVISIONING_DIR="$EVIDENCE_DIR/../provisioning"

: "${DATABASE_URL:=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms}"
: "${DOCKER_DB_CONTAINER:=nhms-db}"
export DATABASE_URL

echo "=== Phase B rehearsal on node-27 ==="
echo "EVIDENCE_DIR=$EVIDENCE_DIR"
echo "PROVISIONING_DIR=$PROVISIONING_DIR"
echo "DATABASE_URL=${DATABASE_URL/nhms_dev/******}"
echo "DOCKER_DB_CONTAINER=$DOCKER_DB_CONTAINER"

exec_sql_file() {
  local script_name="$1"
  local script_path="$PROVISIONING_DIR/$script_name"
  echo "--- exec SQL: $script_name ---"
  docker exec -i "$DOCKER_DB_CONTAINER" psql -U nhms -d nhms -v ON_ERROR_STOP=1 < "$script_path"
}

echo "STEP 0: recorded-bypass baseline provisioning"
exec_sql_file "00-baseline-and-stations.sql"

echo "STEP 1: canonical grid snapshot"
exec_sql_file "01-canonical-grid-snapshot.sql"

echo "STEP 2: register M1 direct-grid variant"
uv run python "$PROVISIONING_DIR/02-register-direct-grid-variant.py"

echo "STEP 3: seed pre-cutover forecast run"
exec_sql_file "03-seeded-forecast-run.sql"

echo "STEP 4: rehearse.py (activate + capture + restore)"

# Kick off the Playwright capture in the background: it copies the
# rehearsal spec in, runs Playwright, restores the original spec. We race
# it against rehearse.py's SCREENSHOT_WINDOW block (30 seconds by
# default). The `wait` at the end reaps the background job.
(
  # Give rehearse.py a head start so the flip has committed before
  # Playwright starts navigating; the SCREENSHOT_WINDOW block is where the
  # committed state is observable.
  sleep 5
  bash "$EVIDENCE_DIR/playwright-capture.sh" || echo "[playwright-capture] non-zero exit; rehearsal continues"
) &
PLAYWRIGHT_PID=$!

set +e
uv run python "$EVIDENCE_DIR/rehearse.py"
REHEARSE_RC=$?
set -e

echo "STEP 5: waiting on Playwright capture (pid=$PLAYWRIGHT_PID)"
wait "$PLAYWRIGHT_PID" || echo "[playwright-capture] background job exit non-zero"

echo "=== Phase B rehearsal done (rehearse.py rc=$REHEARSE_RC) ==="
exit "$REHEARSE_RC"
