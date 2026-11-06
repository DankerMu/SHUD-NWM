#!/bin/bash
set -uo pipefail
set -a; . /home/nwm/NWM/infra/env/display.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms_display_ro:*@*) : ;; *) echo DSN_ROLE_UNEXPECTED; exit 2;; esac
psql "$DATABASE_URL" -P pager=off <<'SQL'
\timing on
BEGIN READ ONLY;
SET LOCAL statement_timeout = '1800s';
SELECT 'variable' AS col, variable AS value, count(*) AS rows FROM met.forcing_station_timeseries GROUP BY 2
UNION ALL SELECT 'unit', unit, count(*) FROM met.forcing_station_timeseries GROUP BY 2
UNION ALL SELECT 'quality_flag', quality_flag, count(*) FROM met.forcing_station_timeseries GROUP BY 2
UNION ALL SELECT 'native_resolution', coalesce(native_resolution,'<NULL>'), count(*) FROM met.forcing_station_timeseries GROUP BY 2
UNION ALL SELECT 'source_id', source_id, count(*) FROM met.forcing_station_timeseries GROUP BY 2
ORDER BY 1, 2;
COMMIT;
SQL
echo "=== FULL DISTINCT DONE ==="
