#!/usr/bin/env bash
# Six-basin production replay driver wrapper for node-22 (#1164 change 2).
#
# Thin shell over `scripts/replay_driver.py`: it loads the dedicated replay env
# file (NOT the production scheduler env), asserts the two invariants an
# operator can get wrong at the shell level, and forwards everything else.
#
# Dry run is the default; `--execute` performs the real replay.
#
# Prerequisite (fail-closed by construction): the state scope of this source
# must already have been reset with `scripts/replay_state_scope_reset.py
# --enforce`, and its receipt path passed as --reset-receipt. Without it the
# driver refuses (the prior-state fields have no admissible source).

set -euo pipefail

REPO="${NHMS_NODE22_REPO:-/scratch/frd_muziyao/NWM}"
ENV_FILE="${NHMS_REPLAY_ENV:-$REPO/infra/env/compute.replay.env}"
SOURCE=""
MODEL_ARGS=()
RESET_RECEIPT=""
RECEIPT_PATH=""
RESUME_ARGS=()
START_CYCLE="2026070500"
END_CYCLE="2026072100"
EXECUTE_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  scripts/ops/node22_six_basin_replay.sh --source gfs|IFS --model-id ID [--model-id ID ...] \
      --reset-receipt PATH --receipt-path PATH \
      [--start-cycle YYYYMMDDHH] [--end-cycle YYYYMMDDHH] [--resume-from PATH] [--execute]

Notes:
  - Without --execute the driver censuses, pre-captures and plans, and submits nothing.
  - The replay env file MUST set NHMS_RETENTION_ENABLED=false; the driver refuses otherwise.
  - Cycles run strictly serially; any failure or convergence timeout halts the sequence
    with the interruption recorded in the replacement receipt (no automatic skipping).
  - GFS 2026070712 automatically switches to the repair parameter set.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      SOURCE="${2:-}"
      shift 2
      ;;
    --model-id)
      MODEL_ARGS+=(--model-id "${2:-}")
      shift 2
      ;;
    --reset-receipt)
      RESET_RECEIPT="${2:-}"
      shift 2
      ;;
    --receipt-path)
      RECEIPT_PATH="${2:-}"
      shift 2
      ;;
    --resume-from)
      RESUME_ARGS+=(--resume-from "${2:-}")
      shift 2
      ;;
    --start-cycle)
      START_CYCLE="${2:-}"
      shift 2
      ;;
    --end-cycle)
      END_CYCLE="${2:-}"
      shift 2
      ;;
    --execute)
      EXECUTE_ARGS+=(--execute)
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

if [ -z "$SOURCE" ] || [ "${#MODEL_ARGS[@]}" -eq 0 ] || [ -z "$RESET_RECEIPT" ] || [ -z "$RECEIPT_PATH" ]; then
  echo "--source, at least one --model-id, --reset-receipt and --receipt-path are required" >&2
  usage >&2
  exit 2
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Replay env file not found: $ENV_FILE" >&2
  exit 2
fi
if [ ! -f "$RESET_RECEIPT" ]; then
  echo "State-scope reset receipt not found: $RESET_RECEIPT" >&2
  exit 2
fi

cd "$REPO"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ "${NHMS_RETENTION_ENABLED:-}" != "false" ]; then
  echo "NHMS_RETENTION_ENABLED must be false during a replay (got: ${NHMS_RETENTION_ENABLED:-unset})" >&2
  exit 2
fi
if [ "${NHMS_SCHEDULER_REPLAY_MODE:-}" != "true" ] && [ "${NHMS_SCHEDULER_REPLAY_MODE:-}" != "1" ]; then
  echo "NHMS_SCHEDULER_REPLAY_MODE must be enabled in $ENV_FILE" >&2
  exit 2
fi

exec "$REPO/.venv/bin/python" -m scripts.replay_driver \
  --source "$SOURCE" \
  "${MODEL_ARGS[@]}" \
  --start-cycle "$START_CYCLE" \
  --end-cycle "$END_CYCLE" \
  --nfs-root "${NHMS_REPLAY_NFS_ROOT:?NHMS_REPLAY_NFS_ROOT is required}" \
  --scratch-root "${OBJECT_STORE_ROOT:?OBJECT_STORE_ROOT is required}" \
  --state-index "${NHMS_SCHEDULER_STATE_INDEX:?NHMS_SCHEDULER_STATE_INDEX is required}" \
  --journal-root "${NHMS_SCHEDULER_JOURNAL_ROOT:?NHMS_SCHEDULER_JOURNAL_ROOT is required}" \
  --registry-manifest "${NHMS_SCHEDULER_REGISTRY_MANIFEST:-}" \
  --object-store-prefix "${OBJECT_STORE_PREFIX:-}" \
  --reset-receipt "$RESET_RECEIPT" \
  --receipt-path "$RECEIPT_PATH" \
  "${RESUME_ARGS[@]}" \
  "${EXECUTE_ARGS[@]}"
