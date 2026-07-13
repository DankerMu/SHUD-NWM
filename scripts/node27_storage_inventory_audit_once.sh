#!/bin/sh
set -eu

ENV_FILE=${NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE:-/home/nwm/NWM/infra/env/node27-storage-inventory-audit.env}
PYTHON_BIN=${NODE27_STORAGE_INVENTORY_AUDIT_PYTHON:-/home/nwm/NWM/.venv/bin/python}
SCRIPT=${NODE27_STORAGE_INVENTORY_AUDIT_SCRIPT:-/home/nwm/NWM/scripts/node27_storage_inventory_audit.py}

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
  echo '{"status":"failed","reason":"audit entrypoint is unavailable or a symlink"}' >&2
  exit 1
}

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

REPO_ROOT=${NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT:-/home/nwm/NWM}
case "$REPO_ROOT" in
  /*) ;;
  *) echo '{"status":"failed","reason":"repository root must be absolute"}' >&2; exit 1 ;;
esac
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH="$REPO_ROOT:$PYTHONPATH"
else
  PYTHONPATH="$REPO_ROOT"
fi
export PYTHONPATH

exec "$PYTHON_BIN" "$SCRIPT" "$@"
