#!/bin/sh
set -eu

ENV_FILE=${NODE27_TIMESERIES_COMPRESSION_ENV_FILE:-/home/nwm/NWM/infra/env/node27-timeseries-compression.env}
PYTHON_BIN=${NODE27_TIMESERIES_COMPRESSION_PYTHON:-/home/nwm/NWM/.venv/bin/python}
SCRIPT=${NODE27_TIMESERIES_COMPRESSION_SCRIPT:-/home/nwm/NWM/scripts/node27_timeseries_compression.py}

case "$ENV_FILE:$PYTHON_BIN:$SCRIPT" in
  /*:/*:/*) ;;
  *) echo '{"status":"failed","reason":"wrapper paths must be absolute"}' >&2; exit 1 ;;
esac

[ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] || {
  echo '{"status":"failed","reason":"env file must be a regular non-symlink file"}' >&2
  exit 1
}
[ "$(stat -c '%a' "$ENV_FILE")" = 600 ] || {
  echo '{"status":"failed","reason":"env file must have mode 0600"}' >&2
  exit 1
}
[ -x "$PYTHON_BIN" ] || {
  echo '{"status":"failed","reason":"python executable is unavailable"}' >&2
  exit 1
}
[ -f "$SCRIPT" ] && [ ! -L "$SCRIPT" ] || {
  echo '{"status":"failed","reason":"compression entrypoint is unavailable or a symlink"}' >&2
  exit 1
}

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

exec "$PYTHON_BIN" "$SCRIPT" "$@"
