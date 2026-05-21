#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_ROOT="${QHH_RUN_ROOT:-$ROOT_DIR/.nhms-runs/qhh-continuous}"
OBJECT_ROOT="${OBJECT_STORE_ROOT:-$RUN_ROOT}"
OBJECT_PREFIX="${OBJECT_STORE_PREFIX:-s3://nhms}"
BASINS_ROOT="${NHMS_BASINS_ROOT:-data/Basins}"
MODEL_ID="${QHH_MODEL_ID:-basins_qhh_shud}"
PACKAGE_VERSION="${QHH_PACKAGE_VERSION:-v0.0.1-qhh-smoke-lake2}"
AUTH_ACTOR_ID="${QHH_AUTH_ACTOR_ID:-qhh-continuous}"
AUTH_ROLE="${QHH_AUTH_ROLE:-model_admin}"
SOURCE_INPUT="${QHH_SOURCE_ID:-${1:-gfs}}"
CYCLE_TIME="${QHH_CYCLE_TIME:-${2:-}}"

log() {
  printf '[qhh-cycle] %s\n' "$*"
}

json_status() {
  local path="$1"
  local status="$2"
  local reason="$3"
  shift 3
  mkdir -p "$(dirname "$path")"
  uv run python - "$path" "$status" "$reason" "$@" <<'PY'
import json
import sys
from datetime import UTC, datetime

path, status, reason, *pairs = sys.argv[1:]
payload = {
    "status": status,
    "reason": reason,
    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}
for pair in pairs:
    key, value = pair.split("=", 1)
    payload[key] = value
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

require_cycle_time() {
  if [[ -z "$CYCLE_TIME" ]]; then
    log "blocked: QHH_CYCLE_TIME is not set and no cycle argument was provided"
    exit 2
  fi
}

normalize_source() {
  uv run python - "$SOURCE_INPUT" <<'PY'
import sys
from packages.common.source_identity import normalize_source_id
print(normalize_source_id(sys.argv[1]))
PY
}

run_id_for() {
  local source_segment="$1"
  printf 'fcst_%s_%s_%s\n' "$source_segment" "$CYCLE_TIME" "$MODEL_ID"
}

db_run_status() {
  local run_id="$1"
  uv run python - "$run_id" <<'PY'
import os
import sys
import psycopg2

run_id = sys.argv[1]
with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3) as conn, conn.cursor() as cur:
    cur.execute("SELECT status FROM hydro.hydro_run WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    print(row[0] if row else "")
PY
}

db_run_field() {
  local run_id="$1"
  local field="$2"
  uv run python - "$run_id" "$field" <<'PY'
import os
import sys
import psycopg2

run_id, field = sys.argv[1:3]
allowed = {"output_uri", "log_uri"}
if field not in allowed:
    raise SystemExit(f"unsupported hydro_run field: {field}")
with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3) as conn, conn.cursor() as cur:
    cur.execute(f"SELECT {field} FROM hydro.hydro_run WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    print(row[0] if row and row[0] else "")
PY
}

registry_ready() {
  uv run python - "$MODEL_ID" <<'PY'
import os
import sys
import psycopg2

model_id = sys.argv[1]
with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3) as conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT 1
        FROM core.model_instance
        WHERE model_id = %s
        LIMIT 1
        """,
        (model_id,),
    )
    print("1" if cur.fetchone() else "0")
PY
}

last_json_status() {
  local path="$1"
  uv run python - "$path" <<'PY'
import json
import sys

status = ""
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        status = json.loads(line).get("status", status)
    except json.JSONDecodeError:
        pass
print(status)
PY
}

prepare_database_url() {
  if [[ -n "${DATABASE_URL:-}" ]]; then
    export DATABASE_URL
    return
  fi
  if [[ "${QHH_AUTO_START_PG:-1}" == "1" ]]; then
    log "starting project-local PostgreSQL"
    ./scripts/local_pg.sh start >/dev/null
  fi
  export DATABASE_URL
  DATABASE_URL="$(./scripts/local_pg.sh url)"
}

require_database() {
  uv run python - <<'PY'
import os
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3)
conn.close()
PY
}

require_shud() {
  SHUD_EXECUTABLE="${SHUD_EXECUTABLE:-$ROOT_DIR/SHUD/shud}"
  export SHUD_EXECUTABLE
  if [[ ! -x "$SHUD_EXECUTABLE" ]]; then
    log "blocked: SHUD_EXECUTABLE is not executable: $SHUD_EXECUTABLE"
    exit 1
  fi
}

require_cycle_time
SOURCE_ID="$(normalize_source)"
SOURCE_SEGMENT="$(uv run python - "$SOURCE_ID" <<'PY'
import sys
print(sys.argv[1].lower())
PY
)"
RUN_ID="${QHH_RUN_ID:-$(run_id_for "$SOURCE_SEGMENT")}"
CYCLE_ROOT="$RUN_ROOT/cycles/$SOURCE_SEGMENT/$CYCLE_TIME"
STATE_FILE="$RUN_ROOT/state/cycles/$SOURCE_SEGMENT/$CYCLE_TIME.json"
INVENTORY="$RUN_ROOT/basins-inventory.json"
PACKAGE_MANIFEST="$RUN_ROOT/qhh-package-manifest.json"
IMPORT_REPORT="$RUN_ROOT/qhh-registry-import-report.json"

mkdir -p "$RUN_ROOT" "$OBJECT_ROOT" "$CYCLE_ROOT"

export QHH_RUN_ROOT="$RUN_ROOT"
export QHH_SOURCE_ID="$SOURCE_ID"
export QHH_CYCLE_TIME="$CYCLE_TIME"
export QHH_RUN_ID="$RUN_ID"
export QHH_MODEL_ID="$MODEL_ID"
export QHH_PACKAGE_VERSION="$PACKAGE_VERSION"
export WORKSPACE_ROOT="$RUN_ROOT"
export OBJECT_STORE_ROOT="$OBJECT_ROOT"
export OBJECT_STORE_PREFIX="$OBJECT_PREFIX"
export NHMS_BASINS_ROOT="$BASINS_ROOT"
export MODEL_OUTPUT_INTERVAL="${QHH_MODEL_OUTPUT_INTERVAL:-180}"
export SHUD_COMMAND_STYLE="${QHH_SHUD_COMMAND_STYLE:-shud_project}"

if [[ "$SOURCE_ID" == "gfs" ]]; then
  export GFS_FORECAST_START_HOUR="${QHH_GFS_FORECAST_START_HOUR:-3}"
  export GFS_FORECAST_END_HOUR="${QHH_GFS_FORECAST_END_HOUR:-168}"
elif [[ "$SOURCE_ID" == "IFS" ]]; then
  export IFS_FORECAST_START_HOUR="${QHH_IFS_FORECAST_START_HOUR:-${IFS_FORECAST_START_HOUR:-3}}"
  export IFS_FORECAST_END_HOUR="${QHH_IFS_FORECAST_END_HOUR:-${IFS_FORECAST_END_HOUR:-168}}"
  export FORCING_MIN_LEAD_HOURS="${QHH_FORCING_MIN_LEAD_HOURS:-${FORCING_MIN_LEAD_HOURS:-$IFS_FORECAST_START_HOUR}}"
fi

if [[ -f "$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6" ]]; then
  export LD_PRELOAD="$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6${LD_PRELOAD:+:$LD_PRELOAD}"
fi

json_status "$STATE_FILE" "running" "cycle execution started" \
  "source_id=$SOURCE_ID" "cycle_time=$CYCLE_TIME" "run_id=$RUN_ID"

prepare_database_url
require_database

if [[ "${QHH_SKIP_COMPLETED:-1}" == "1" ]]; then
  EXISTING_STATUS="$(db_run_status "$RUN_ID")"
  if [[ "$EXISTING_STATUS" == "frequency_done" || "$EXISTING_STATUS" == "published" ]]; then
    log "skip completed run $RUN_ID with status $EXISTING_STATUS"
    json_status "$STATE_FILE" "already_done" "run already completed" \
      "source_id=$SOURCE_ID" "cycle_time=$CYCLE_TIME" "run_id=$RUN_ID" "run_status=$EXISTING_STATUS"
    exit 0
  fi
fi

log "discovering Basins inventory from $BASINS_ROOT"
uv run nhms-model discover-basins \
  --basins-root "$BASINS_ROOT" \
  --output "$INVENTORY" | tee "$CYCLE_ROOT/discover-basins.stdout.json"

log "publishing qhh package for $MODEL_ID@$PACKAGE_VERSION"
uv run nhms-model publish-basins \
  --inventory "$INVENTORY" \
  --model-id "$MODEL_ID" \
  --version "$PACKAGE_VERSION" \
  --output "$PACKAGE_MANIFEST" | tee "$CYCLE_ROOT/publish-basins.stdout.json"

if [[ "${QHH_USE_SMOKE_MIGRATIONS:-1}" == "1" ]]; then
  log "applying local PostgreSQL-compatible migrations"
  uv run python scripts/apply_smoke_migrations.py | tee "$CYCLE_ROOT/migrate.log"
else
  log "applying production migrations"
  uv run python -m packages.common.migrate | tee "$CYCLE_ROOT/migrate.log"
fi

if [[ "$(registry_ready)" == "1" ]]; then
  log "qhh registry records already exist for $MODEL_ID; skipping registry import"
  json_status "$CYCLE_ROOT/import-basins-registry.stdout.json" "already_done" "registry already exists" \
    "model_id=$MODEL_ID"
else
  log "importing qhh registry records"
  uv run nhms-model import-basins-registry \
    --inventory "$INVENTORY" \
    --package-manifest "$PACKAGE_MANIFEST" \
    --output "$IMPORT_REPORT" \
    --auth-actor-id "$AUTH_ACTOR_ID" \
    --auth-role "$AUTH_ROLE" | tee "$CYCLE_ROOT/import-basins-registry.stdout.json"
fi

log "seeding qhh standard forcing stations and SHUD output river identities"
uv run python scripts/seed_qhh_forcing_stations.py | tee "$CYCLE_ROOT/seed-qhh-forcing-stations.stdout.json"
uv run python scripts/seed_qhh_shud_output_segments.py | tee "$CYCLE_ROOT/seed-qhh-shud-output-segments.stdout.json"

if [[ "$SOURCE_ID" == "IFS" ]]; then
  log "downloading IFS cycle $CYCLE_TIME"
  uv run nhms-ifs download --cycle-time "$CYCLE_TIME" | tee "$CYCLE_ROOT/download.stdout.json"
else
  log "downloading GFS cycle $CYCLE_TIME for forecast hours $GFS_FORECAST_START_HOUR-$GFS_FORECAST_END_HOUR"
  uv run nhms-gfs download --source-id "$SOURCE_ID" --cycle-time "$CYCLE_TIME" | tee "$CYCLE_ROOT/download.stdout.json"
fi

DOWNLOAD_STATUS="$(last_json_status "$CYCLE_ROOT/download.stdout.json")"
if [[ "$DOWNLOAD_STATUS" == "unavailable" ]]; then
  log "$SOURCE_ID cycle $CYCLE_TIME is unavailable; downstream stages skipped"
  json_status "$STATE_FILE" "unavailable" "source cycle is not available" \
    "source_id=$SOURCE_ID" "cycle_time=$CYCLE_TIME" "run_id=$RUN_ID"
  exit 0
fi

log "converting $SOURCE_ID cycle $CYCLE_TIME to canonical products"
uv run nhms-canonical convert --source-id "$SOURCE_ID" --cycle-time "$CYCLE_TIME" | tee "$CYCLE_ROOT/canonical-convert.stdout.json"

log "producing qhh forcing for $MODEL_ID from $SOURCE_ID cycle $CYCLE_TIME"
FORCING_ARGS=(nhms-forcing produce --source-id "$SOURCE_ID" --cycle-time "$CYCLE_TIME" --model-id "$MODEL_ID")
if [[ -n "${QHH_MAX_LEAD_HOURS:-}" ]]; then
  FORCING_ARGS+=(--max-lead-hours "$QHH_MAX_LEAD_HOURS")
fi
uv run "${FORCING_ARGS[@]}" | tee "$CYCLE_ROOT/forcing-produce.stdout.json"

require_shud

log "creating qhh SHUD runtime manifest for $RUN_ID"
uv run python scripts/create_qhh_shud_manifest.py | tee "$CYCLE_ROOT/create-qhh-shud-manifest.stdout.json"
MANIFEST_PATH="$RUN_ROOT/runs/$RUN_ID/input/manifest.json"

HYDRO_STATUS="$(db_run_status "$RUN_ID")"
if [[ "$HYDRO_STATUS" == "succeeded" || "$HYDRO_STATUS" == "parsed" || "$HYDRO_STATUS" == "frequency_done" || "$HYDRO_STATUS" == "published" ]]; then
  OUTPUT_URI="$(db_run_field "$RUN_ID" output_uri)"
  if [[ -z "$OUTPUT_URI" ]]; then
    log "blocked: existing $RUN_ID status $HYDRO_STATUS has no output_uri for parse resume"
    json_status "$STATE_FILE" "failed" "existing hydro run cannot resume without output_uri" \
      "source_id=$SOURCE_ID" "cycle_time=$CYCLE_TIME" "run_id=$RUN_ID" "run_status=$HYDRO_STATUS"
    exit 1
  fi
  log "skip SHUD runtime for $RUN_ID; hydro_run already $HYDRO_STATUS"
  json_status "$CYCLE_ROOT/shud-runtime.stdout.json" "already_done" "hydro run already has SHUD output" \
    "run_id=$RUN_ID" "run_status=$HYDRO_STATUS" "output_uri=$OUTPUT_URI"
else
  log "running SHUD for $RUN_ID using $SHUD_EXECUTABLE"
  uv run nhms-shud-runtime execute --manifest "$MANIFEST_PATH" | tee "$CYCLE_ROOT/shud-runtime.stdout.json"
fi

HYDRO_STATUS="$(db_run_status "$RUN_ID")"
if [[ "$HYDRO_STATUS" == "parsed" || "$HYDRO_STATUS" == "frequency_done" || "$HYDRO_STATUS" == "published" ]]; then
  log "skip parse for $RUN_ID; hydro_run already $HYDRO_STATUS"
  json_status "$CYCLE_ROOT/parse-shud-output.stdout.json" "already_done" "hydro run already parsed" \
    "run_id=$RUN_ID" "run_status=$HYDRO_STATUS"
else
  log "parsing SHUD output for $RUN_ID"
  uv run nhms-parse shud-output --run-id "$RUN_ID" | tee "$CYCLE_ROOT/parse-shud-output.stdout.json"
fi

log "summarizing and publishing qhh display products for $RUN_ID"
uv run python scripts/summarize_qhh_smoke_results.py | tee "$CYCLE_ROOT/qhh-result-summary.stdout.json"
uv run python scripts/publish_qhh_display_products.py | tee "$CYCLE_ROOT/qhh-display-products.stdout.json"

json_status "$STATE_FILE" "frequency_done" "cycle execution completed through display products" \
  "source_id=$SOURCE_ID" "cycle_time=$CYCLE_TIME" "run_id=$RUN_ID"
