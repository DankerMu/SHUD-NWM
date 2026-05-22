#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT="${QHH_RUN_ROOT:-$ROOT_DIR/.nhms-runs/qhh-smoke}"
OBJECT_ROOT="${OBJECT_STORE_ROOT:-$RUN_ROOT}"
OBJECT_PREFIX="${OBJECT_STORE_PREFIX:-s3://nhms}"
BASINS_ROOT="${NHMS_BASINS_ROOT:-data/Basins}"
MODEL_ID="${QHH_MODEL_ID:-basins_qhh_shud}"
PACKAGE_VERSION="${QHH_PACKAGE_VERSION:-v0.0.1-qhh-smoke-lake2}"
AUTH_ACTOR_ID="${QHH_AUTH_ACTOR_ID:-qhh-smoke}"
AUTH_ROLE="${QHH_AUTH_ROLE:-model_admin}"

INVENTORY="$RUN_ROOT/basins-inventory.json"
PACKAGE_MANIFEST="$RUN_ROOT/qhh-package-manifest.json"
IMPORT_REPORT="$RUN_ROOT/qhh-registry-import-report.json"

log() {
  printf '[qhh-smoke] %s\n' "$*"
}

write_json_status() {
  local path="$1"
  local status="$2"
  local reason="$3"
  mkdir -p "$(dirname "$path")"
  python - "$path" "$status" "$reason" <<'PY'
import json
import sys
from datetime import UTC, datetime

path, status, reason = sys.argv[1:4]
payload = {
    "status": status,
    "reason": reason,
    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

require_database() {
  if [[ -z "${DATABASE_URL:-}" ]]; then
    write_json_status "$RUN_ROOT/db-gate.json" "blocked" "DATABASE_URL is not set."
    log "blocked: DATABASE_URL is not set; registry import and downstream DB-backed stages skipped"
    return 1
  fi

  if ! uv run python - <<'PY'
import os
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3)
conn.close()
PY
  then
    write_json_status "$RUN_ROOT/db-gate.json" "blocked" "DATABASE_URL is set but PostgreSQL is not reachable."
    log "blocked: PostgreSQL is not reachable via DATABASE_URL"
    return 1
  fi

  write_json_status "$RUN_ROOT/db-gate.json" "ready" "DATABASE_URL is reachable."
}

require_cycle_time() {
  if [[ -z "${QHH_CYCLE_TIME:-}" ]]; then
    write_json_status "$RUN_ROOT/met-gate.json" "blocked" "QHH_CYCLE_TIME is not set."
    log "blocked: QHH_CYCLE_TIME is not set; met download/canonical/forcing/runtime stages skipped"
    return 1
  fi
}

require_shud() {
  SHUD_EXECUTABLE="${SHUD_EXECUTABLE:-$ROOT_DIR/SHUD/shud}"
  export SHUD_EXECUTABLE
  if [[ ! -x "$SHUD_EXECUTABLE" ]]; then
    write_json_status "$RUN_ROOT/shud-gate.json" "blocked" "SHUD_EXECUTABLE is not executable."
    log "blocked: SHUD_EXECUTABLE is not executable: $SHUD_EXECUTABLE"
    return 1
  fi
  write_json_status "$RUN_ROOT/shud-gate.json" "ready" "SHUD_EXECUTABLE is executable."
}

mkdir -p "$RUN_ROOT" "$OBJECT_ROOT"

export WORKSPACE_ROOT="$RUN_ROOT"
export OBJECT_STORE_ROOT="$OBJECT_ROOT"
export OBJECT_STORE_PREFIX="$OBJECT_PREFIX"
export NHMS_BASINS_ROOT="$BASINS_ROOT"
export QHH_PACKAGE_VERSION="$PACKAGE_VERSION"
export GFS_FORECAST_START_HOUR="${QHH_GFS_FORECAST_START_HOUR:-3}"
export GFS_FORECAST_END_HOUR="${QHH_GFS_FORECAST_END_HOUR:-24}"
export MODEL_OUTPUT_INTERVAL="${QHH_MODEL_OUTPUT_INTERVAL:-180}"
export SHUD_COMMAND_STYLE="${QHH_SHUD_COMMAND_STYLE:-shud_project}"
if [[ -f "$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6" ]]; then
  export LD_PRELOAD="$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6${LD_PRELOAD:+:$LD_PRELOAD}"
fi

log "discovering Basins inventory from $BASINS_ROOT"
uv run nhms-model discover-basins \
  --basins-root "$BASINS_ROOT" \
  --output "$INVENTORY" | tee "$RUN_ROOT/discover-basins.stdout.json"

log "publishing qhh package for $MODEL_ID@$PACKAGE_VERSION"
uv run nhms-model publish-basins \
  --inventory "$INVENTORY" \
  --model-id "$MODEL_ID" \
  --version "$PACKAGE_VERSION" \
  --output "$PACKAGE_MANIFEST" | tee "$RUN_ROOT/publish-basins.stdout.json"

if ! require_database; then
  exit 0
fi

if [[ "${QHH_USE_SMOKE_MIGRATIONS:-1}" == "1" ]]; then
  log "applying smoke database migrations without TimescaleDB"
  uv run python scripts/apply_smoke_migrations.py | tee "$RUN_ROOT/migrate.log"
else
  log "applying production database migrations"
  uv run python -m packages.common.migrate | tee "$RUN_ROOT/migrate.log"
fi

if [[ "${QHH_RESET_SMOKE_DB:-0}" == "1" ]]; then
  log "resetting qhh smoke database rows for repeatable full-chain runs"
  uv run python scripts/reset_qhh_smoke_db.py | tee "$RUN_ROOT/reset-qhh-smoke-db.stdout.json"
fi

log "importing qhh registry records"
uv run nhms-model import-basins-registry \
  --inventory "$INVENTORY" \
  --package-manifest "$PACKAGE_MANIFEST" \
  --output "$IMPORT_REPORT" \
  --auth-actor-id "$AUTH_ACTOR_ID" \
  --auth-role "$AUTH_ROLE" | tee "$RUN_ROOT/import-basins-registry.stdout.json"

log "seeding qhh standard forcing stations from qhh.tsd.forc"
uv run python scripts/seed_qhh_forcing_stations.py | tee "$RUN_ROOT/seed-qhh-forcing-stations.stdout.json"

log "seeding qhh SHUD output river identities"
uv run python scripts/seed_qhh_shud_output_segments.py | tee "$RUN_ROOT/seed-qhh-shud-output-segments.stdout.json"

if ! require_cycle_time; then
  exit 0
fi

log "downloading GFS cycle $QHH_CYCLE_TIME for forecast hours $GFS_FORECAST_START_HOUR-$GFS_FORECAST_END_HOUR"
uv run nhms-gfs download --source-id gfs --cycle-time "$QHH_CYCLE_TIME" | tee "$RUN_ROOT/gfs-download.stdout.json"

log "converting GFS cycle $QHH_CYCLE_TIME to canonical products"
uv run nhms-canonical convert --source-id gfs --cycle-time "$QHH_CYCLE_TIME" | tee "$RUN_ROOT/canonical-convert.stdout.json"

log "producing forcing for $MODEL_ID from cycle $QHH_CYCLE_TIME"
FORCING_ARGS=(nhms-forcing produce --source-id gfs --cycle-time "$QHH_CYCLE_TIME" --model-id "$MODEL_ID")
if [[ -n "${QHH_MAX_LEAD_HOURS:-}" ]]; then
  FORCING_ARGS+=(--max-lead-hours "$QHH_MAX_LEAD_HOURS")
fi
uv run "${FORCING_ARGS[@]}" | tee "$RUN_ROOT/forcing-produce.stdout.json"

if ! require_shud; then
  exit 0
fi

log "creating qhh SHUD runtime manifest"
export QHH_RUN_ID="${QHH_RUN_ID:-qhh_gfs_${QHH_CYCLE_TIME}_smoke}"
uv run python scripts/create_qhh_shud_manifest.py | tee "$RUN_ROOT/create-qhh-shud-manifest.stdout.json"
RUN_ID="$(python - "$RUN_ROOT/create-qhh-shud-manifest.stdout.json" <<'PY'
import json
import sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())["run_id"])
PY
)"
export QHH_RUN_ID="$RUN_ID"
MANIFEST_PATH="$RUN_ROOT/runs/$RUN_ID/input/manifest.json"

log "running SHUD for $RUN_ID using $SHUD_EXECUTABLE"
uv run nhms-shud-runtime execute --manifest "$MANIFEST_PATH" | tee "$RUN_ROOT/shud-runtime.stdout.json"

log "parsing SHUD output for $RUN_ID"
uv run nhms-parse shud-output --run-id "$RUN_ID" | tee "$RUN_ROOT/parse-shud-output.stdout.json"

log "summarizing qhh smoke results"
uv run python scripts/summarize_qhh_smoke_results.py | tee "$RUN_ROOT/qhh-result-summary.stdout.json"

log "publishing qhh display products for API/frontend consumption"
uv run python scripts/publish_qhh_display_products.py | tee "$RUN_ROOT/qhh-display-products.stdout.json"

write_json_status "$RUN_ROOT/runtime-gate.json" "ready" "SHUD runtime and output parse completed."
