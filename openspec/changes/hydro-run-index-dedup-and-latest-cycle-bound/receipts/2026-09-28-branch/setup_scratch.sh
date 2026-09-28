#!/bin/bash
# Create $SCRATCH_DB on the node-27 cluster, migrate from zero through $THROUGH with $WT's
# db/migrations, then copy hydro.hydro_run read-only from production (ro DSN) into it.
set -euo pipefail
. /home/nwm/tmp/batch-db/scratch_env.sh
WT="${WT:?}"; THROUGH="${THROUGH:-}"
psql "$ADMIN_URL" -X -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $SCRATCH_DB" -c "CREATE DATABASE $SCRATCH_DB"
cd "$WT"
PYTHONPATH="$WT" SCRATCH_URL="$SCRATCH_URL" THROUGH="$THROUGH" /home/nwm/NWM/.venv/bin/python -B -c '
import os
from tests.integration_helpers import apply_migrations_from_zero
apply_migrations_from_zero(os.environ["SCRATCH_URL"], through=os.environ["THROUGH"] or None)
'
COLS=$(psql "$SCRATCH_URL" -X -A -t -c "SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum) FROM pg_attribute WHERE attrelid='hydro.hydro_run'::regclass AND attnum>0 AND NOT attisdropped")
echo "columns: $COLS"
psql "$RO_DSN" -X -q -v ON_ERROR_STOP=1 -c "\copy (SELECT $COLS FROM hydro.hydro_run ORDER BY run_key) TO STDOUT" \
 | psql "$SCRATCH_URL" -X -q -v ON_ERROR_STOP=1 -c "SET session_replication_role = replica" -c "\copy hydro.hydro_run ($COLS) FROM STDIN"
psql "$SCRATCH_URL" -X -A -c "ANALYZE hydro.hydro_run" -c "SELECT count(*) AS rows, pg_relation_size('hydro.hydro_run')/8192 AS pages FROM hydro.hydro_run" \
  -c "SELECT version FROM public.schema_migrations ORDER BY 1 DESC LIMIT 2"
