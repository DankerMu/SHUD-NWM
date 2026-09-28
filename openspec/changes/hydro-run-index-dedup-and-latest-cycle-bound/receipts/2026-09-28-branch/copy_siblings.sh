#!/bin/bash
# Copy the authority tables the hydro_run sibling selectors join (tasks 3.4) read-only
# from production (ro DSN) into $SCRATCH_DB, so their plans see production-sized joins.
set -euo pipefail
. /home/nwm/tmp/batch-db/scratch_env.sh
for table in core.basin core.basin_version core.river_network_version core.model_instance met.forcing_version hydro.run_display_coverage; do
  COLS=$(psql "$SCRATCH_URL" -X -A -t -c "SELECT string_agg(quote_ident(attname), ',' ORDER BY attnum) FROM pg_attribute WHERE attrelid='$table'::regclass AND attnum>0 AND NOT attisdropped AND attgenerated = ''")
  psql "$SCRATCH_URL" -X -q -v ON_ERROR_STOP=1 -c "SET session_replication_role = replica" -c "DELETE FROM $table"
  psql "$RO_DSN" -X -q -v ON_ERROR_STOP=1 -c "\copy (SELECT $COLS FROM $table) TO STDOUT" \
   | psql "$SCRATCH_URL" -X -q -v ON_ERROR_STOP=1 -c "SET session_replication_role = replica" -c "\copy $table ($COLS) FROM STDIN"
  psql "$SCRATCH_URL" -X -A -t -c "ANALYZE $table" -c "SELECT '$table', count(*) FROM $table"
done
psql "$SCRATCH_URL" -X -A -t -c "ANALYZE hydro.hydro_run" -c "SELECT max(version) FROM public.schema_migrations"
