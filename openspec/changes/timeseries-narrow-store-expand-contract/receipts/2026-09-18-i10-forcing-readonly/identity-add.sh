#!/bin/bash
# #1989 task 7.1 (v2): IDENTITY column add cost, reproducing BOTH heap constitutions
# measured live: (a) live-DATA equivalent ~45 MB, (b) live-PAGES equivalent ~325 MB
# (met.met_station is ~8x bloated: 325 MB heap, 8 kB toast, ~40 MB of column data).
# v1 is kept in the receipt as a rejected measurement: its jsonb padding compressed,
# giving an 11 MB heap and a timing that understated the real work ~30x.
set -uo pipefail
export TMPDIR=/home/nwm/tmp
set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms:*@*) : ;; *) echo DSN_ROLE_UNEXPECTED; exit 2;; esac
DB="throwaway_1989b_$(date -u +%Y%m%d%H%M%S)_$$"
ADMIN="${DATABASE_URL%/*}/nhms"; TARGET="${DATABASE_URL%/*}/$DB"
cleanup(){ psql "$ADMIN" -q -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null 2>&1; echo "dropped $DB"; }
trap cleanup EXIT
psql "$ADMIN" -q -c "CREATE DATABASE \"$DB\"" || exit 2
echo "throwaway database: $DB"

psql "$TARGET" -P pager=off <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE met_station (
    station_id text PRIMARY KEY, basin_version_id text NOT NULL, station_name text,
    geom geometry(Point,4326) NOT NULL, elevation_m double precision,
    station_role text NOT NULL, active_flag boolean NOT NULL,
    properties_json jsonb NOT NULL, created_at timestamptz NOT NULL,
    superseded_at timestamptz, grid_snapshot_id uuid);
-- autovacuum off so the bloat we build on purpose survives to the measurement
ALTER TABLE met_station SET (autovacuum_enabled = false);
CREATE TABLE forcing_version (
    forcing_version_id text PRIMARY KEY, model_id text NOT NULL, source_id text NOT NULL,
    cycle_time timestamptz, start_time timestamptz NOT NULL, end_time timestamptz NOT NULL,
    station_count integer NOT NULL, forcing_package_uri text NOT NULL, checksum text,
    lineage_json jsonb NOT NULL, created_at timestamptz NOT NULL);
ALTER TABLE forcing_version SET (autovacuum_enabled = false);

-- 928-byte properties_json, from random bytes so TOAST compression cannot shrink it
INSERT INTO met_station SELECT 'st_'||i, 'bv_'||(i%40), 'station '||i,
  ST_SetSRID(ST_MakePoint(63+(i%8200)/100.0, 8+(i%5600)/100.0),4326),
  (i%5000)::double precision, 'forcing', true,
  jsonb_build_object('pad', encode(gen_random_bytes(660),'base64'), 'i', i),
  now(), NULL, NULL FROM generate_series(1,42029) i;
INSERT INTO forcing_version SELECT 'fv_'||i, 'm_'||(i%40),
  CASE WHEN i%2=0 THEN 'gfs' ELSE 'IFS' END, now(), now(), now()+interval '7 days',
  42029, 's3://bucket/fv_'||i, md5(i::text),
  jsonb_build_object('pad', encode(gen_random_bytes(1200),'base64'), 'i', i), now()
  FROM generate_series(1,8653) i;
SQL

sizes(){ psql "$TARGET" -At -P pager=off -c "
SELECT relname||' heap='||pg_size_pretty(pg_relation_size(c.oid))
   ||' toast='||pg_size_pretty(pg_total_relation_size(coalesce(reltoastrelid,0)))
   ||' idx='||pg_size_pretty(pg_indexes_size(c.oid))
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN ('met_station','forcing_version') ORDER BY 1;"; }

echo "=== (a) live-DATA constitution seeded ==="; sizes
echo "  live target: met_station ~40 MB of column data; forcing_version heap 15 MB"

echo
echo "=== (a) ALTER ADD COLUMN IDENTITY UNIQUE, no bloat ==="
for t in forcing_version met_station; do
  ( sleep 0.3; psql "$TARGET" -At -c "SELECT '  locks on $t: '||coalesce(string_agg(DISTINCT l.mode,','),'(none seen)')
      FROM pg_locks l JOIN pg_class c ON c.oid=l.relation JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='public' AND c.relname='$t';" ) &
  w=$!
  psql "$TARGET" -P pager=off -c "\timing on" -c "ALTER TABLE $t ADD COLUMN ${t}_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;"
  wait $w
done
sizes

echo
echo "=== building ~8x bloat on met_station to match the live 325 MB heap ==="
psql "$TARGET" -P pager=off -c "\timing on" -c "
DO \$\$ BEGIN FOR k IN 1..7 LOOP UPDATE met_station SET elevation_m = elevation_m + 1; END LOOP; END \$\$;"
sizes

echo
echo "=== (b) same ALTER on the bloated table (fresh column) ==="
( sleep 0.3; psql "$TARGET" -At -c "SELECT '  locks on met_station: '||coalesce(string_agg(DISTINCT l.mode,','),'(none seen)')
    FROM pg_locks l JOIN pg_class c ON c.oid=l.relation JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relname='met_station';" ) &
w=$!
psql "$TARGET" -P pager=off -c "\timing on" -c "ALTER TABLE met_station ADD COLUMN met_station_key2 INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;"
wait $w
sizes
echo "=== IDENTITY v2 DONE ==="
