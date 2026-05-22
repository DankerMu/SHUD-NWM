#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_PREFIX="${PG_PREFIX:-$ROOT_DIR/.conda-postgres-runtime}"
PGDATA="${PGDATA:-$ROOT_DIR/.pgdata/qhh-smoke}"
PGSOCKET_DIR="${PGSOCKET_DIR:-$ROOT_DIR/.pgsocket}"
PGLOG_DIR="${PGLOG_DIR:-$ROOT_DIR/.pglogs}"
PGPORT="${PGPORT:-55432}"
PGLISTEN="${PGLISTEN:-127.0.0.1}"
PGHOSTCIDR="${PGHOSTCIDR:-127.0.0.1/32}"
APP_DB="${APP_DB:-nhms}"
APP_USER="${APP_USER:-nhms}"
APP_PASSWORD="${APP_PASSWORD:-nhms_dev}"
QHH_LOCAL_PG_ALLOW_REMOTE="${QHH_LOCAL_PG_ALLOW_REMOTE:-0}"

export PATH="$PG_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PG_PREFIX/lib:${LD_LIBRARY_PATH:-}"

log() {
  printf '[local-pg] %s\n' "$*"
}

database_url() {
  printf 'postgresql://%s:%s@%s:%s/%s\n' "$APP_USER" "$APP_PASSWORD" "$PGLISTEN" "$PGPORT" "$APP_DB"
}

redacted_database_url() {
  printf 'postgresql://%s:****@%s:%s/%s\n' "$APP_USER" "$PGLISTEN" "$PGPORT" "$APP_DB"
}

require_bins() {
  for bin in initdb pg_ctl createdb psql; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      printf '[local-pg] missing %s under %s/bin\n' "$bin" "$PG_PREFIX" >&2
      exit 1
    fi
  done
}

is_loopback_listen() {
  python - "$PGLISTEN" <<'PY'
from __future__ import annotations

import ipaddress
import sys

listen = sys.argv[1].strip()
if listen == "localhost":
    raise SystemExit(0)
try:
    address = ipaddress.ip_address(listen)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if address.is_loopback else 1)
PY
}

guard_remote_listen() {
  if is_loopback_listen; then
    return
  fi
  if [[ "$QHH_LOCAL_PG_ALLOW_REMOTE" != "1" ]]; then
    printf '[local-pg] refusing non-loopback PGLISTEN=%s without QHH_LOCAL_PG_ALLOW_REMOTE=1\n' "$PGLISTEN" >&2
    exit 2
  fi
  if [[ "$APP_PASSWORD" == "nhms_dev" || -z "$APP_PASSWORD" ]]; then
    printf '[local-pg] refusing remote helper mode with default or empty APP_PASSWORD\n' >&2
    exit 2
  fi
}

configure_access() {
  python - "$PGDATA/postgresql.conf" "$PGDATA/pg_hba.conf" "$PGLISTEN" "$PGPORT" "$PGSOCKET_DIR" "$PGHOSTCIDR" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

postgresql_conf = Path(sys.argv[1])
pg_hba = Path(sys.argv[2])
listen, port, socket_dir, host_cidr = sys.argv[3:]


def upsert_setting(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f"{key} ="
    replacement = f"{key} = {value}\n"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(prefix):
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(replacement)
    return updated


conf_lines = postgresql_conf.read_text(encoding="utf-8").splitlines(keepends=True)
conf_lines = upsert_setting(conf_lines, "listen_addresses", repr(listen))
conf_lines = upsert_setting(conf_lines, "port", port)
conf_lines = upsert_setting(conf_lines, "unix_socket_directories", repr(socket_dir))
conf_lines = upsert_setting(conf_lines, "shared_preload_libraries", "''")
postgresql_conf.write_text("".join(conf_lines), encoding="utf-8")

hba_lines = pg_hba.read_text(encoding="utf-8").splitlines()
required = [
    "host all all 127.0.0.1/32 scram-sha-256",
    "host all all ::1/128 scram-sha-256",
]
if host_cidr != "127.0.0.1/32":
    required.append(f"host all all {host_cidr} scram-sha-256")
for line in required:
    if line not in hba_lines:
        hba_lines.append(line)
pg_hba.write_text("\n".join(hba_lines) + "\n", encoding="utf-8")
PY
}

init() {
  require_bins
  guard_remote_listen
  mkdir -p "$ROOT_DIR/.pgdata" "$PGDATA" "$PGSOCKET_DIR" "$PGLOG_DIR"
  chmod 700 "$ROOT_DIR/.pgdata" "$PGDATA" "$PGSOCKET_DIR" "$PGLOG_DIR"
  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    log "initializing PGDATA at $PGDATA"
    initdb -D "$PGDATA" --encoding=UTF8 --locale=C.UTF-8 --auth-local=trust --auth-host=scram-sha-256
  else
    log "PGDATA already initialized at $PGDATA"
  fi
  configure_access
}

start() {
  init
  if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    log "already running"
  else
    log "starting PostgreSQL on $PGLISTEN:$PGPORT"
    pg_ctl -D "$PGDATA" -l "$PGLOG_DIR/postgres.log" -o "-p $PGPORT -k $PGSOCKET_DIR" start
  fi

  log "ensuring role/database $APP_USER/$APP_DB"
  psql -h "$PGSOCKET_DIR" -p "$PGPORT" -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER') THEN
    EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', '$APP_USER', '$APP_PASSWORD');
  ELSE
    EXECUTE format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', '$APP_USER', '$APP_PASSWORD');
  END IF;
END
\$\$;
SELECT format('CREATE DATABASE %I OWNER %I', '$APP_DB', '$APP_USER')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$APP_DB')\gexec
ALTER DATABASE "$APP_DB" OWNER TO "$APP_USER";
SQL

  umask 077
  url_file="$ROOT_DIR/.pgdata/qhh-smoke.database-url"
  tmp_url_file="$(mktemp "$url_file.XXXXXX")"
  database_url > "$tmp_url_file"
  chmod 600 "$tmp_url_file"
  mv -f "$tmp_url_file" "$url_file"
  log "DATABASE_URL=$(redacted_database_url)"
  log "full DATABASE_URL is available via ./scripts/local_pg.sh url"
}

stop() {
  require_bins
  if [[ -f "$PGDATA/PG_VERSION" ]]; then
    pg_ctl -D "$PGDATA" stop -m fast || true
  else
    log "PGDATA not initialized"
  fi
}

status() {
  require_bins
  pg_ctl -D "$PGDATA" status || true
}

url() {
  database_url
}

case "${1:-start}" in
  init) init ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  url) url ;;
  *)
    echo "Usage: $0 {init|start|stop|restart|status|url}" >&2
    exit 2
    ;;
esac
