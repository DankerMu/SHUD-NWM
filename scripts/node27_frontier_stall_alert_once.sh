#!/usr/bin/env bash
# node-27 frontier stall alert wrapper (issue #1368).
#
# Deliberately a THIN shell (design D4): the single-instance mutex lives in
# the python runner (fcntl.flock on a lockfile beside the state file) so it is
# unit-testable, unlike the wrapper-level flock the sibling *_once.sh scripts
# use. This wrapper only does what a shell must do: refuse an unsafe env file
# (symlinked or not mode-0600 — checked on EVERY path, systemd or manual),
# make the log directory, and exec the runner.

set -u

CALLER_PYTHONPATH=${PYTHONPATH-}
readonly CALLER_PYTHONPATH
REPO="${NODE27_FRONTIER_ALERT_REPO:-/home/nwm/NWM}"
ENV_FILE="${NODE27_FRONTIER_ALERT_ENV_FILE:-$REPO/infra/env/node27-frontier-alert.env}"
BOOTSTRAP_LOG="${NODE27_FRONTIER_ALERT_BOOTSTRAP_LOG:-/home/nwm/node27-frontier-alert.log}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] node27-frontier-alert: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo '{"status":"failed","reason":"'"$reason"'"}' >&2
  exit 2
}

case "$REPO" in
  *:*) blocked "REPOSITORY_ROOT_PATH_LIST_DELIMITER" ;;
  /*) ;;
  *) blocked "REPOSITORY_ROOT_NOT_ABSOLUTE" ;;
esac

case "$ENV_FILE" in
  /*) ;;
  *) blocked "ENV_FILE_NOT_ABSOLUTE" ;;
esac

# --- env file safety: UNCONDITIONAL (design D4, round-2 P2) ----------------
# The permission contract on this file (0600, never a symlink) is ORTHOGONAL
# to who parses it: the file holds the read-only role's password whether the
# wrapper sources it or systemd already did. Round-1 put these checks behind
# the "should I source?" branch, which silently accepted a mode-0644 password
# file on the systemd path while three documents still promised refusal.
# -L is tested BEFORE -e because -e follows the link: a dangling symlink is a
# symlink, and must be refused as one rather than reported as missing.
if [ -L "$ENV_FILE" ]; then
  blocked "ENV_FILE_SYMLINK_FORBIDDEN"
fi
if [ -e "$ENV_FILE" ]; then
  ENV_MODE=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)
  if [ "$ENV_MODE" != "600" ]; then
    blocked "ENV_FILE_MODE_UNSAFE"
  fi
fi

# --- single reader: only the SOURCING is gated -----------------------------
# Under systemd the env file is loaded exactly once by EnvironmentFile=, and
# systemd's parser is NOT the shell's. Re-sourcing here would make a value's
# meaning depend on which parser ran last (quoting, $-expansion, backslashes).
# The sentinel is LANE-SCOPED (Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1
# in the service unit) rather than DATABASE_URL: DATABASE_URL is the most
# shared variable name in this repo, so a debugging shell that happens to have
# another lane's DSN exported would skip sourcing and run this lane against the
# wrong database.
if [ -n "${NODE27_FRONTIER_ALERT_ENV_INJECTED:-}" ]; then
  : "env injected by systemd EnvironmentFile=; not re-sourcing $ENV_FILE"
elif [ -e "$ENV_FILE" ]; then
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

if [ -n "$CALLER_PYTHONPATH" ]; then
  PYTHONPATH="$REPO:$CALLER_PYTHONPATH"
else
  PYTHONPATH="$REPO"
fi
export PYTHONPATH

LOG_ROOT="${NODE27_FRONTIER_ALERT_LOG_ROOT:-/home/nwm/node27-frontier-alert-logs}"
case "$LOG_ROOT" in
  /*) ;;
  *) blocked "LOG_ROOT_NOT_ABSOLUTE" ;;
esac
if [ "$LOG_ROOT" = "/" ]; then
  blocked "LOG_ROOT_UNSAFE"
fi
mkdir -p "$LOG_ROOT" 2>/dev/null || blocked "LOG_ROOT_UNWRITABLE"

PYTHON_BIN="${NODE27_FRONTIER_ALERT_PYTHON:-$REPO/.venv/bin/python}"
SCRIPT="${NODE27_FRONTIER_ALERT_SCRIPT:-$REPO/scripts/node27_frontier_stall_alert.py}"
LOG_FILE="${NODE27_FRONTIER_ALERT_LOG_FILE:-$LOG_ROOT/frontier-alert.log}"

case "$PYTHON_BIN:$SCRIPT" in
  /*:/*) ;;
  *) blocked "WRAPPER_PATHS_NOT_ABSOLUTE" ;;
esac
[ -x "$PYTHON_BIN" ] || blocked "PYTHON_EXECUTABLE_UNAVAILABLE"
if [ ! -f "$SCRIPT" ] || [ -L "$SCRIPT" ]; then
  blocked "ALERT_ENTRYPOINT_UNAVAILABLE"
fi

cd "$REPO" || blocked "REPO_UNAVAILABLE"

echo "[$(ts)] node27-frontier-alert: start" >> "$LOG_FILE"
# Single-instance mutex is inside the runner; a contended tick exits 0 with a
# structured FRONTIER_ALERT_CONCURRENT_INVOCATION log line.
"$PYTHON_BIN" "$SCRIPT" --once "$@" >> "$LOG_FILE" 2>&1
RC=$?
echo "[$(ts)] node27-frontier-alert: done rc=$RC" >> "$LOG_FILE"
exit "$RC"
