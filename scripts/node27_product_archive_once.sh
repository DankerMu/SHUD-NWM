#!/bin/sh
set -eu

ENV_FILE=${NODE27_PRODUCT_ARCHIVE_ENV_FILE:-/home/nwm/NWM/infra/env/node27-product-archive.env}
PYTHON_BIN=${NODE27_PRODUCT_ARCHIVE_PYTHON:-/home/nwm/NWM/.venv/bin/python}
SCRIPT=${NODE27_PRODUCT_ARCHIVE_SCRIPT:-/home/nwm/NWM/scripts/node27_product_archive.py}

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
  echo '{"status":"failed","reason":"archive entrypoint is unavailable or a symlink"}' >&2
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
case "$ZSTD" in /*) ;; *) echo '{"status":"failed","reason":"zstd path must be absolute"}' >&2; exit 1 ;; esac
[ -x "$ZSTD" ] && [ -f "$ZSTD" ] && [ ! -L "$ZSTD" ] || {
  echo '{"status":"failed","reason":"zstd executable is unavailable or unsafe"}' >&2
  exit 1
}

exec "$PYTHON_BIN" "$SCRIPT" "$@"
