#!/usr/bin/env bash
# Run one explicit 00/12 production cycle on node-22 with the DB-free scheduler config.

set -euo pipefail

REPO="${NHMS_NODE22_REPO:-/scratch/frd_muziyao/NWM}"
ENV_FILE="${NHMS_NODE22_SCHEDULER_ENV:-$REPO/infra/env/compute.scheduler-dbfree.env}"
# Same second EnvironmentFile as
# infra/systemd/node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf.
SECRET_ENV_FILE="${NHMS_NODE22_GATEWAY_SECRET_ENV:-/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env}"
read -r -a SYSTEMCTL <<<"${NHMS_NODE22_SYSTEMCTL:-systemctl --user}"
TIMER="${NHMS_NODE22_SCHEDULER_TIMER:-nhms-compute-scheduler.timer}"
SERVICE="${NHMS_NODE22_SCHEDULER_SERVICE:-nhms-compute-scheduler.service}"
WAIT_SECONDS="${NHMS_NODE22_TIMER_WAIT_SECONDS:-14400}"
POLL_SECONDS="${NHMS_NODE22_TIMER_POLL_SECONDS:-15}"
MODE="--plan"
SOURCE_ARGS=()
BASIN_ARGS=()
CYCLE_TIME=""
REPAIR_MISSING_FORCING="false"

usage() {
  cat <<'USAGE'
Usage:
  scripts/ops/node22-run-cycle-once.sh --cycle-time YYYY-MM-DDTHH:MM:SSZ [--source gfs|IFS] [--basin-id ID ...] [--repair-missing-forcing] [--plan|--submit]

Notes:
  - Sources default to the scheduler env when omitted.
  - Basin filters are optional; omit --basin-id to run every active basin in the registry.
  - --cycle-time pins a single source cycle and disables backfill gap ordering.
  - --repair-missing-forcing authorizes only this exact cycle to rebuild a missing
    direct-grid forcing package from a verified ready raw manifest. Without the
    flag, missing forcing remains fail-closed.
  - The DB-free runtime preflight binds both configured raw-authority roots to
    the fixed node-22 shared-NFS topology before lock acquisition or repair work.
  - Loads the same environment as nhms-compute-scheduler.service: the scheduler
    env, the Slurm gateway secret env (NHMS_NODE22_GATEWAY_SECRET_ENV) and
    NHMS_PYTHON_VENV_BIN. --submit refuses to run without a readable secret.
  - If nhms-compute-scheduler.timer is active, it is stopped, the running pass
    is awaited (NHMS_NODE22_TIMER_WAIT_SECONDS, default 14400), and the timer is
    started again on every exit path. An inactive timer is left alone.
  - Exit codes: planner exit code; 2 usage/env/secret/undeterminable unit state
    or timer stop failure; 3 wait timeout; 4 timer restore failed after a
    successful planner; 129/130/131/143 on HUP/INT/QUIT/TERM.
USAGE
}

# Event log on stderr. Never fatal: stderr may already be closed (ssh hangup).
log() {
  printf 'node22-run-cycle-once: %s %s\n' "$(date -u +%FT%TZ)" "$*" >&2 || true
}

# `is-active` output, never its exit code: real systemctl exits 3 for
# activating/inactive/failed/deactivating/unknown and 0 only for active/reloading.
unit_state() {
  local state
  state="$("${SYSTEMCTL[@]}" is-active "$1" 2>/dev/null)" || true
  printf '%s' "$state"
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
    --repair-missing-forcing)
      REPAIR_MISSING_FORCING="true"
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
case "$WAIT_SECONDS" in
  ''|*[!0-9]*)
    log "invalid NHMS_NODE22_TIMER_WAIT_SECONDS (want whole seconds)"
    exit 2
    ;;
esac
if ! [[ "$POLL_SECONDS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  log "invalid NHMS_NODE22_TIMER_POLL_SECONDS (want seconds)"
  exit 2
fi

SECRET_PROBLEM=""
if [ ! -e "$SECRET_ENV_FILE" ]; then
  SECRET_PROBLEM="missing"
elif [ ! -f "$SECRET_ENV_FILE" ] || [ ! -r "$SECRET_ENV_FILE" ]; then
  SECRET_PROBLEM="unreadable"
fi

cd "$REPO"
# Never trace env/secret assignments, even under `bash -x`.
{ set +x; } 2>/dev/null
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
if [ -z "$SECRET_PROBLEM" ]; then
  # stderr suppressed: a malformed line would otherwise echo part of a value.
  # shellcheck disable=SC1090
  if ! . "$SECRET_ENV_FILE" 2>/dev/null; then
    SECRET_PROBLEM="unloadable"
  fi
fi
set +a
if [ -n "$SECRET_PROBLEM" ]; then
  if [ "$MODE" = "--submit" ]; then
    log "gateway_secret_env_unavailable path=$SECRET_ENV_FILE reason=$SECRET_PROBLEM (--submit refused)"
    exit 2
  fi
  log "gateway_secret_env_unavailable path=$SECRET_ENV_FILE reason=$SECRET_PROBLEM (--plan continues)"
fi

export NHMS_SERVICE_ROLE="${NHMS_SERVICE_ROLE:-compute_control}"
export SLURM_GATEWAY_URL="${SLURM_GATEWAY_URL:-http://127.0.0.1:8090}"
export NHMS_PRODUCTION_SLURM_ENABLED="${NHMS_PRODUCTION_SLURM_ENABLED:-true}"
export NHMS_SCHEDULER_REPAIR_MISSING_FORCING="$REPAIR_MISSING_FORCING"
if [ "$REPAIR_MISSING_FORCING" = "true" ]; then
  export NHMS_SCHEDULER_REPAIR_MISSING_FORCING_CYCLE_TIME="$CYCLE_TIME"
else
  unset NHMS_SCHEDULER_REPAIR_MISSING_FORCING_CYCLE_TIME
fi
# Same value as infra/systemd/node22/nhms-compute-scheduler.service.d/20-runtime-path.conf.
export NHMS_PYTHON_VENV_BIN="${NHMS_PYTHON_VENV_BIN:-$REPO/.venv/bin}"

# --- Serialize with nhms-compute-scheduler.timer --------------------------
# Traps are installed before any stop: a timer this run stopped is started
# again on every trappable exit path, exactly once. SIGKILL/OOM cannot be
# trapped; the stall probe reports timer_stopped and the runbook gives the
# manual `systemctl --user start` command.
PY_PID=""
PY_LAUNCHING=0
BG_BEFORE_PY=""
SLEEP_PID=""
SIGNAL_RECEIVED=""
TIMER_STOPPED=0
RESTORED=0

# shellcheck disable=SC2317,SC2329  # invoked via trap
on_exit() {
  rc=$?
  set +e
  trap '' INT TERM HUP QUIT
  if [ "$TIMER_STOPPED" = 1 ] && [ "$RESTORED" = 0 ]; then
    RESTORED=1
    # start before any stderr write: after a hangup stderr may be gone.
    start_rc=0
    "${SYSTEMCTL[@]}" start "$TIMER" >/dev/null || start_rc=$?
    if [ "$start_rc" = 0 ]; then
      log "timer_restored"
    else
      log "timer_restore_failed rc=$start_rc; restore manually: systemctl --user start $TIMER"
      if [ "$rc" = 0 ]; then
        rc=4
      fi
    fi
  fi
  if [ -n "$SIGNAL_RECEIVED" ]; then
    log "signal_received sig=$SIGNAL_RECEIVED rc=$rc"
  fi
  exit "$rc"
}

# shellcheck disable=SC2317,SC2329  # invoked via trap
on_signal() {
  local signame="$1" code="$2"
  set +e
  trap '' INT TERM HUP QUIT
  if [ -n "$SLEEP_PID" ]; then
    kill "$SLEEP_PID" 2>/dev/null
  fi
  if [ -z "$PY_PID" ] && [ "$PY_LAUNCHING" = 1 ] && [ "${!:-}" != "$BG_BEFORE_PY" ]; then
    # Signal landed between `&` and the PY_PID assignment.
    PY_PID=$!
  fi
  if [ -n "$PY_PID" ]; then
    # Background children of a non-interactive bash ignore SIGINT, and QUIT
    # would dump core: both are forwarded as TERM.
    case "$signame" in
      INT|QUIT) signame=TERM ;;
    esac
    kill -s "$signame" "$PY_PID" 2>/dev/null
    wait "$PY_PID" 2>/dev/null
  fi
  SIGNAL_RECEIVED="$1"
  exit "$code"
}

trap on_exit EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP
trap 'on_signal QUIT 131' QUIT
# No-op handler, not SIG_IGN: a write to a stderr pipe whose reader is gone
# (ssh drop without a pty, `2>&1 | head`) must return EPIPE to the log helper
# instead of killing the wrapper before the EXIT trap restores the timer.
# A caught signal is reset to default on exec, so children keep SIGPIPE.
trap ':' PIPE

TIMER_STATE="$(unit_state "$TIMER")"
case "$TIMER_STATE" in
  active)
    TIMER_STOPPED=1
    stop_rc=0
    "${SYSTEMCTL[@]}" stop "$TIMER" >/dev/null || stop_rc=$?
    if [ "$stop_rc" != 0 ]; then
      log "timer_stop_failed rc=$stop_rc"
      exit 2
    fi
    log "timer_stopped"
    WAIT_START=$SECONDS
    while :; do
      SERVICE_STATE="$(unit_state "$SERVICE")"
      case "$SERVICE_STATE" in
        inactive|failed)
          break
          ;;
        active|activating|deactivating|reloading)
          ;;
        *)
          log "service_state_undeterminable state=${SERVICE_STATE:-<empty>}"
          exit 2
          ;;
      esac
      if [ $((SECONDS - WAIT_START)) -ge "$WAIT_SECONDS" ]; then
        log "timer_wait_timeout waited=$((SECONDS - WAIT_START)) limit=$WAIT_SECONDS state=$SERVICE_STATE"
        exit 3
      fi
      # Background sleep + wait so a trapped signal is handled immediately.
      sleep "$POLL_SECONDS" >/dev/null 2>&1 &
      SLEEP_PID=$!
      wait "$SLEEP_PID" || true
      SLEEP_PID=""
    done
    log "service_inactive waited=$((SECONDS - WAIT_START))"
    ;;
  inactive|failed|unknown)
    log "timer_left_inactive state=$TIMER_STATE"
    ;;
  *)
    log "timer_state_undeterminable state=${TIMER_STATE:-<empty>}"
    exit 2
    ;;
esac

# Background + wait (not exec, not foreground): the EXIT trap must run after
# python, and a foreground child would defer signal traps until it exits.
BG_BEFORE_PY="${!:-}"
PY_LAUNCHING=1
"$REPO/.venv/bin/python" -m services.orchestrator.cli plan-production \
  "${SOURCE_ARGS[@]}" \
  "${BASIN_ARGS[@]}" \
  --cycle-time "$CYCLE_TIME" \
  --disable-backfill \
  "$MODE" &
PY_PID=$!
PY_RC=0
wait "$PY_PID" || PY_RC=$?
exit "$PY_RC"
