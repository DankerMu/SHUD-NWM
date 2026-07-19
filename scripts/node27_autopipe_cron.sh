#!/bin/bash
# node-27 autopipeline automation wrapper.
#
# Periodically scans the object-store for new basins/runs and ingests them via
# scripts/node27_autopipeline.py (seed registry -> register -> object-store
# forcing handoff -> parse -> refresh-coverage). Idempotent:
# already-seeded basins and already-parsed runs are detected and skipped, so
# re-running every N minutes is safe and only does outstanding work.
#
# flock guards against overlapping runs (a long ingest must not be re-entered by
# the next cron tick). All output (timestamped + the JSON summary) is appended
# to AUTOPIPE_LOG_FILE.
#
# Installed on node-27 by infra/systemd/nhms-node27-autopipe.{service,timer}.

set -u

REPO="${NODE27_AUTOPIPE_REPO:-/home/nwm/NWM}"
INGEST_ENV="${NODE27_AUTOPIPE_ENV_FILE:-$REPO/infra/env/node27-ingest.env}"
ALLOW_AMBIENT_ENV="${NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV:-0}"
BOOTSTRAP_LOG="${NODE27_AUTOPIPE_BOOTSTRAP_LOG:-/home/nwm/autopipe.log}"
INGEST_ENV_STRICT_SOURCE=0

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

bootstrap_blocked() {
  local reason="$1"
  mkdir -p "$(dirname "$BOOTSTRAP_LOG")" 2>/dev/null || true
  echo "[$(ts)] autopipe: BLOCKED rc=2 reason=$reason" >> "$BOOTSTRAP_LOG"
  echo "[$(ts)] autopipe: BLOCKED rc=2 reason=$reason" >&2
  exit 2
}

if [ -f "$INGEST_ENV" ]; then
  case "$INGEST_ENV" in
    *display.env|*display.example|*display-readonly-secrets.env)
      bootstrap_blocked "INGEST_ENV_DISPLAY_RUNTIME_FORBIDDEN"
      ;;
  esac
  if [ -L "$INGEST_ENV" ]; then
    bootstrap_blocked "INGEST_ENV_SYMLINK_FORBIDDEN"
  fi
  if [ ! -f "$INGEST_ENV" ]; then
    bootstrap_blocked "INGEST_ENV_NOT_REGULAR_FILE"
  fi
  ENV_MODE=$(stat -c '%a' "$INGEST_ENV" 2>/dev/null || stat -f '%Lp' "$INGEST_ENV" 2>/dev/null || true)
  if [ "$ENV_MODE" != "600" ]; then
    bootstrap_blocked "INGEST_ENV_MODE_UNSAFE"
  fi
  if [ "$ALLOW_AMBIENT_ENV" != "1" ]; then
    INGEST_ENV_STRICT_SOURCE=1
    # Env-file mode must not borrow required ingest runtime config from the
    # invoking shell. A partial file fails closed below or in Python preflight.
    unset DATABASE_URL
    unset NHMS_NODE27_INGEST_ROLE
    unset NHMS_SERVICE_ROLE
    unset OBJECT_STORE_ROOT
    unset BASINS_ROOT
    unset AUTOPIPE_WORK_ROOT
    unset AUTOPIPE_LOG_ROOT
    unset PGAPPNAME
    unset PGCHANNELBINDING
    unset PGCLIENTENCODING
    unset PGCONNECT_TIMEOUT
    unset PGDATABASE
    unset PGDATESTYLE
    unset PGGEQO
    unset PGGSSDELEGATION
    unset PGGSSENCMODE
    unset PGGSSLIB
    unset PGHOST
    unset PGHOSTADDR
    unset PGKRBSRVNAME
    unset PGLOCALEDIR
    unset PGLOADBALANCEHOSTS
    unset PGMAXPROTOCOLVERSION
    unset PGMINPROTOCOLVERSION
    unset PGOPTIONS
    unset PGPASSFILE
    unset PGPASSWORD
    unset PGPORT
    unset PGREQUIREAUTH
    unset PGREQUIREPEER
    unset PGREQUIRESSL
    unset PGSERVICE
    unset PGSERVICEFILE
    unset PGSSL_CERT_FILE
    unset PGSSL_KEY_FILE
    unset PGSSL_ROOT_CERT_FILE
    unset PGSSLCERT
    unset PGSSLCERTMODE
    unset PGSSLCOMPRESSION
    unset PGSSLCRL
    unset PGSSLCRLDIR
    unset PGSSLKEY
    unset PGSSLMAXPROTOCOLVERSION
    unset PGSSLMINPROTOCOLVERSION
    unset PGSSLMODE
    unset PGSSLNEGOTIATION
    unset PGSSLROOTCERT
    unset PGSSLSNI
    unset PGSYSCONFDIR
    unset PGTARGETSESSIONATTRS
    unset PGTZ
    unset PGUSER
  fi
  set -a
  # shellcheck disable=SC1090
  if ! . "$INGEST_ENV"; then
    set +a
    bootstrap_blocked "INGEST_ENV_SOURCE_FAILED"
  fi
  set +a
  if [ "$INGEST_ENV_STRICT_SOURCE" = "1" ]; then
    for key in \
      PGAPPNAME PGCHANNELBINDING PGCLIENTENCODING PGCONNECT_TIMEOUT PGDATABASE \
      PGDATESTYLE PGGEQO PGGSSDELEGATION PGGSSENCMODE PGGSSLIB PGHOST \
      PGHOSTADDR PGKRBSRVNAME PGLOCALEDIR PGLOADBALANCEHOSTS \
      PGMAXPROTOCOLVERSION PGMINPROTOCOLVERSION PGOPTIONS PGPASSFILE \
      PGPASSWORD PGPORT PGREQUIREAUTH PGREQUIREPEER PGREQUIRESSL PGSERVICE \
      PGSERVICEFILE PGSSL_CERT_FILE PGSSL_KEY_FILE PGSSL_ROOT_CERT_FILE \
      PGSSLCERT PGSSLCERTMODE PGSSLCOMPRESSION PGSSLCRL PGSSLCRLDIR \
      PGSSLKEY PGSSLMAXPROTOCOLVERSION PGSSLMINPROTOCOLVERSION PGSSLMODE \
      PGSSLNEGOTIATION PGSSLROOTCERT PGSSLSNI PGSYSCONFDIR \
      PGTARGETSESSIONATTRS PGTZ PGUSER
    do
      value="${!key:-}"
      if [ -n "$value" ]; then
        bootstrap_blocked "LIBPQ_AMBIENT_ENV_FORBIDDEN_$key"
      fi
    done
  fi
  export NHMS_NODE27_INGEST_CONFIG_SOURCE="env_file:$INGEST_ENV"
elif [ "$ALLOW_AMBIENT_ENV" = "1" ]; then
  export NHMS_NODE27_INGEST_CONFIG_SOURCE="${NHMS_NODE27_INGEST_CONFIG_SOURCE:-ambient:NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV}"
else
  bootstrap_blocked "INGEST_ENV_MISSING"
fi

if [ -n "${N22_DSN:-}" ] ||
  [ -n "${NHMS_NODE22_DSN_SOURCE:-}" ] ||
  [ -n "${NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR:-}" ]; then
  bootstrap_blocked "NODE22_DB_RUNTIME_ENV_FORBIDDEN"
fi

if [ "${NHMS_NODE27_INGEST_ROLE:-}" != "node27_data_plane_ingest" ]; then
  bootstrap_blocked "INGEST_ROLE_REQUIRED"
fi

if [ -z "${AUTOPIPE_LOG_ROOT:-}" ]; then
  bootstrap_blocked "AUTOPIPE_LOG_ROOT_MISSING"
fi
case "$AUTOPIPE_LOG_ROOT" in
  /*) ;;
  *) bootstrap_blocked "AUTOPIPE_LOG_ROOT_UNSAFE" ;;
esac
if [ "$AUTOPIPE_LOG_ROOT" = "/" ]; then
  bootstrap_blocked "AUTOPIPE_LOG_ROOT_UNSAFE"
fi
mkdir -p "$AUTOPIPE_LOG_ROOT" 2>/dev/null || bootstrap_blocked "AUTOPIPE_LOG_ROOT_UNWRITABLE"
CANONICAL_LOG_ROOT=$(cd "$AUTOPIPE_LOG_ROOT" 2>/dev/null && pwd -P) || bootstrap_blocked "AUTOPIPE_LOG_ROOT_UNWRITABLE"
if [ "$CANONICAL_LOG_ROOT" = "/" ]; then
  bootstrap_blocked "AUTOPIPE_LOG_ROOT_UNSAFE"
fi

if [ "$INGEST_ENV_STRICT_SOURCE" = "1" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    bootstrap_blocked "DATABASE_URL_MISSING"
  fi
  if [ -z "${OBJECT_STORE_ROOT:-}" ]; then
    bootstrap_blocked "OBJECT_STORE_ROOT_MISSING"
  fi
  if [ -z "${BASINS_ROOT:-}" ]; then
    bootstrap_blocked "BASINS_ROOT_MISSING"
  fi
  if [ -z "${AUTOPIPE_WORK_ROOT:-}" ]; then
    bootstrap_blocked "AUTOPIPE_WORK_ROOT_MISSING"
  fi
fi

LOG="${AUTOPIPE_LOG_FILE:-$AUTOPIPE_LOG_ROOT/autopipe.log}"
LOCK="${AUTOPIPE_LOCK_PATH:-${NODE27_AUTOPIPE_LOCK_PATH:-/tmp/autopipe.cron.lock}}"

# Non-blocking lock: if a previous run is still going, skip this tick.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(ts)] autopipe: previous run still active, skipping tick" >> "$LOG"
  exit 0
fi

echo "[$(ts)] autopipe: start" >> "$LOG"
START=$(date +%s)

cd "$REPO" || { echo "[$(ts)] autopipe: cannot cd $REPO" >> "$LOG"; exit 1; }
"$REPO/.venv/bin/python" "$REPO/scripts/node27_autopipeline.py" \
  --object-store-root "${OBJECT_STORE_ROOT:-}" \
  --basins-root "${BASINS_ROOT:-}" \
  --direct-grid-only \
  --workers "${AUTOPIPE_RUN_WORKERS:-1}" \
  --exclude-basins "${AUTOPIPE_EXCLUDE_BASINS:-}" >> "$LOG" 2>&1
RC=$?

if [ "$RC" -eq 2 ]; then
  END=$(date +%s)
  echo "[$(ts)] autopipe: preflight blocked rc=$RC elapsed_sec=$((END - START))" >> "$LOG"
  exit "$RC"
fi

# Backstop: materialize display coverage for any run still missing/stale it so
# latest-product keeps the <1s fast path (per-run refresh is wired in the
# autopipeline, but a run that failed mid-refresh, or coverage seeded out of
# band, is healed here). Owned by Mission-4; we only invoke it. --skip-fresh
# makes this cheap + resumable. Non-fatal: never masks the ingest exit code.
if [ -f "$REPO/scripts/node27_refresh_coverage.py" ]; then
  echo "[$(ts)] autopipe: coverage backstop (--all --skip-fresh)" >> "$LOG"
  "$REPO/.venv/bin/python" "$REPO/scripts/node27_refresh_coverage.py" --all --skip-fresh \
    --workers "${AUTOPIPE_COVERAGE_WORKERS:-1}" >> "$LOG" 2>&1 \
    || echo "[$(ts)] autopipe: coverage backstop rc=$? (non-fatal)" >> "$LOG"
fi

END=$(date +%s)
echo "[$(ts)] autopipe: done rc=$RC elapsed_sec=$((END - START))" >> "$LOG"
exit "$RC"
