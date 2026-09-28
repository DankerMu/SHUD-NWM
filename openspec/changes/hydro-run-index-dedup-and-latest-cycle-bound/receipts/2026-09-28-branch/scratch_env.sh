# Source me. Derives ADMIN_URL (db postgres) and SCRATCH_URL (db $SCRATCH_DB) from the replay env.
# Never exports a URL whose database is nhms.
set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms:*@*) : ;; *) echo "DSN_ROLE_UNEXPECTED"; return 2 ;; esac
BASE_URL="$DATABASE_URL"; unset DATABASE_URL
SCRATCH_DB="${SCRATCH_DB:-nhms_scratch_batchdb_d2}"
case "$SCRATCH_DB" in nhms_scratch_batchdb_*) : ;; *) echo "BAD_SCRATCH_NAME"; return 2 ;; esac
ADMIN_URL="${BASE_URL%/*}/postgres"
SCRATCH_URL="${BASE_URL%/*}/$SCRATCH_DB"
RO_DSN="$(cat /home/nwm/tmp/batch-db/ro.dsn)"
export TMPDIR=/home/nwm/tmp
