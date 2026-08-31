#!/bin/sh
set -eu

fail() {
  printf '%s\n' "{\"status\":\"failed\",\"reason\":\"$1\"}" >&2
  exit 1
}

COMPRESSION_ENV_FILE=${NODE27_TIMESERIES_COMPRESSION_ENV_FILE:-/home/nwm/NWM/infra/env/node27-timeseries-compression.env}
COLD_ENV_FILE=${NODE27_COLD_RESIDENCY_ENV_FILE:-/home/nwm/NWM/infra/env/node27-cold-residency.env}
PRECHECK_REPO_ROOT=${NODE27_COLD_RESIDENCY_REPO_ROOT:-/home/nwm/NWM}
readonly COMPRESSION_ENV_FILE COLD_ENV_FILE PRECHECK_REPO_ROOT

case "$COMPRESSION_ENV_FILE" in
  /*) ;;
  *) fail "wrapper paths must be absolute" ;;
esac
case "$COLD_ENV_FILE" in
  /*) ;;
  *) fail "wrapper paths must be absolute" ;;
esac
case "$PRECHECK_REPO_ROOT" in
  *:*) fail "repository root must not contain a path-list delimiter" ;;
  /*) ;;
  *) fail "wrapper paths must be absolute" ;;
esac

PRECHECK_PYTHON=$PRECHECK_REPO_ROOT/.venv/bin/python
PRECHECK_SCRIPT=$PRECHECK_REPO_ROOT/scripts/node27_timeseries_budget_preflight.py
case "$PRECHECK_PYTHON:$PRECHECK_SCRIPT" in
  /*:/*) ;;
  *) fail "wrapper paths must be absolute" ;;
esac
[ -f "$PRECHECK_PYTHON" ] && [ -x "$PRECHECK_PYTHON" ] || fail "python executable is unavailable"
if [ ! -f "$PRECHECK_SCRIPT" ] || [ -L "$PRECHECK_SCRIPT" ]; then
  fail "sequential budget preflight is unavailable or a symlink"
fi

# The preflight reads the lane files as inert descriptor-bound data and builds
# the runner environment itself.  -E ignores caller PYTHONPATH for preflight
# imports while retaining it for the final runner environment it constructs.
exec "$PRECHECK_PYTHON" -E "$PRECHECK_SCRIPT" \
  --compression-env "$COMPRESSION_ENV_FILE" \
  --cold-env "$COLD_ENV_FILE" \
  --launch cold -- "$@"
