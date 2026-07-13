#!/bin/sh
set -eu

CALLER_PYTHONPATH=${PYTHONPATH-}
CALLER_PYTHON_OVERRIDE=${NODE27_PRODUCT_ARCHIVE_PYTHON-}
CALLER_SCRIPT_OVERRIDE=${NODE27_PRODUCT_ARCHIVE_SCRIPT-}
readonly CALLER_PYTHONPATH CALLER_PYTHON_OVERRIDE CALLER_SCRIPT_OVERRIDE
ENV_FILE=${NODE27_PRODUCT_ARCHIVE_ENV_FILE:-/home/nwm/NWM/infra/env/node27-product-archive.env}

case "$ENV_FILE" in
  /*) ;;
  *) echo '{"status":"failed","reason":"wrapper paths must be absolute"}' >&2; exit 1 ;;
esac

if [ ! -f "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
  echo '{"status":"failed","reason":"env file must be a regular non-symlink file"}' >&2
  exit 1
fi
[ "$(stat -c '%a' "$ENV_FILE")" = 600 ] || {
  echo '{"status":"failed","reason":"env file must have mode 0600"}' >&2
  exit 1
}
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

ZSTD=${NODE27_PRODUCT_ARCHIVE_ZSTD:-/usr/bin/zstd}
ARCHIVE_ROOT=${NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT:-${NHMS_ARCHIVE_ROOT:-}}
for configured_path in \
  "${NODE27_PRODUCT_ARCHIVE_OBJECT_STORE_ROOT:-}" \
  "$ARCHIVE_ROOT" \
  "${NODE27_PRODUCT_ARCHIVE_RECEIPT:-}" \
  "${NODE27_PRODUCT_ARCHIVE_LOCK_FILE:-}"
do
  case "$configured_path" in
    /*) ;;
    *) echo '{"status":"failed","reason":"required runtime paths must be configured and absolute"}' >&2; exit 1 ;;
  esac
done
case "${OBJECT_STORE_PREFIX:-}" in
  s3://?*/*|s3://?*) ;;
  *) echo '{"status":"failed","reason":"canonical object-store prefix must be configured"}' >&2; exit 1 ;;
esac
case "$ZSTD" in /*) ;; *) echo '{"status":"failed","reason":"zstd path must be absolute"}' >&2; exit 1 ;; esac
if [ ! -x "$ZSTD" ] || [ ! -f "$ZSTD" ] || [ -L "$ZSTD" ]; then
  echo '{"status":"failed","reason":"zstd executable is unavailable or unsafe"}' >&2
  exit 1
fi

REPO_ROOT=${NODE27_PRODUCT_ARCHIVE_REPO_ROOT:-/home/nwm/NWM}
case "$REPO_ROOT" in
  *:*) echo '{"status":"failed","reason":"repository root must not contain a path-list delimiter"}' >&2; exit 1 ;;
  /*) ;;
  *) echo '{"status":"failed","reason":"repository root must be absolute"}' >&2; exit 1 ;;
esac
PYTHON_BIN=${CALLER_PYTHON_OVERRIDE:-$REPO_ROOT/.venv/bin/python}
SCRIPT=${CALLER_SCRIPT_OVERRIDE:-$REPO_ROOT/scripts/node27_product_archive.py}
case "$PYTHON_BIN:$SCRIPT" in
  /*:/*) ;;
  *) echo '{"status":"failed","reason":"wrapper paths must be absolute"}' >&2; exit 1 ;;
esac
[ -x "$PYTHON_BIN" ] || {
  echo '{"status":"failed","reason":"python executable is unavailable"}' >&2
  exit 1
}
if [ ! -f "$SCRIPT" ] || [ -L "$SCRIPT" ]; then
  echo '{"status":"failed","reason":"archive entrypoint is unavailable or a symlink"}' >&2
  exit 1
fi

if [ -n "$CALLER_PYTHONPATH" ]; then
  PYTHONPATH="$REPO_ROOT:$CALLER_PYTHONPATH"
else
  PYTHONPATH="$REPO_ROOT"
fi
export PYTHONPATH

if ! "$PYTHON_BIN" -c '
import importlib.machinery
import os
import sys

root = os.path.realpath(sys.argv[1])
expected_namespace = os.path.join(root, "scripts")
spec = importlib.machinery.PathFinder.find_spec("scripts", sys.path[1:])
locations = (
    []
    if spec is None or spec.submodule_search_locations is None
    else [os.path.realpath(path) for path in spec.submodule_search_locations]
)
valid = (
    spec is not None
    and spec.origin is None
    and locations
    and all(path == expected_namespace for path in locations)
)
raise SystemExit(0 if valid else 1)
' "$REPO_ROOT"; then
  echo '{"status":"failed","reason":"scripts import origin is outside repository root"}' >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT" "$@"
