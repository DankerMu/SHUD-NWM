#!/usr/bin/env bash
# Run one explicit 00/12 production cycle on node-22 with the DB-free scheduler config.

set -euo pipefail

REPO="${NHMS_NODE22_REPO:-/scratch/frd_muziyao/NWM}"
ENV_FILE="${NHMS_NODE22_SCHEDULER_ENV:-$REPO/infra/env/compute.scheduler-dbfree.env}"
MODE="--plan"
SOURCE_ARGS=()
BASIN_ARGS=()
CYCLE_TIME=""

usage() {
  cat <<'USAGE'
Usage:
  scripts/ops/node22-run-cycle-once.sh --cycle-time YYYY-MM-DDTHH:MM:SSZ [--source gfs|IFS] [--basin-id ID ...] [--plan|--submit]

Notes:
  - Sources default to the scheduler env when omitted.
  - Basin filters are optional; omit --basin-id to run every active basin in the registry.
  - --cycle-time pins a single source cycle and disables backfill gap ordering.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --cycle-time)
      CYCLE_TIME="${2:-}"
      shift 2
      ;;
    --source)
      SOURCE_ARGS+=(--source "${2:-}")
      shift 2
      ;;
    --basin-id)
      BASIN_ARGS+=(--basin-id "${2:-}")
      shift 2
      ;;
    --plan)
      MODE="--plan"
      shift
      ;;
    --submit)
      MODE="--submit"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$CYCLE_TIME" ]; then
  echo "--cycle-time is required" >&2
  usage >&2
  exit 2
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Scheduler env file not found: $ENV_FILE" >&2
  exit 2
fi

cd "$REPO"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

export NHMS_SERVICE_ROLE="${NHMS_SERVICE_ROLE:-compute_control}"
export SLURM_GATEWAY_URL="${SLURM_GATEWAY_URL:-http://127.0.0.1:8090}"
export NHMS_PRODUCTION_SLURM_ENABLED="${NHMS_PRODUCTION_SLURM_ENABLED:-true}"

exec "$REPO/.venv/bin/python" -m services.orchestrator.cli plan-production \
  "${SOURCE_ARGS[@]}" \
  "${BASIN_ARGS[@]}" \
  --cycle-time "$CYCLE_TIME" \
  --disable-backfill \
  "$MODE"
