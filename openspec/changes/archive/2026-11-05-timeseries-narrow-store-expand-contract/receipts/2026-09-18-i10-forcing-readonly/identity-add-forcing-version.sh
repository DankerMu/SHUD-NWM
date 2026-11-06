#!/bin/bash
set -uo pipefail
set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms:*@*) : ;; *) echo DSN_ROLE_UNEXPECTED; exit 2;; esac
DB="throwaway_1989c_$(date -u +%Y%m%d%H%M%S)_$$"
ADMIN="${DATABASE_URL%/*}/nhms"; TARGET="${DATABASE_URL%/*}/$DB"
cleanup(){ psql "$ADMIN" -q -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null 2>&1; echo "dropped $DB"; }
trap cleanup EXIT
psql "$ADMIN" -q -c "CREATE DATABASE \"$DB\"" || exit 2
psql "$TARGET" -P pager=off <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE forcing_version (
    forcing_version_id text PRIMARY KEY, model_id text NOT NULL, source_id text NOT NULL,
    cycle_time timestamptz, start_time timestamptz NOT NULL, end_time timestamptz NOT NULL,
    station_count integer NOT NULL, forcing_package_uri text NOT NULL, checksum text,
    lineage_json jsonb NOT NULL, created_at timestamptz NOT NULL);
ALTER TABLE forcing_version SET (autovacuum_enabled = false);
-- live: 8 653 rows / 15 MB heap ~= 1.8 kB per row. gen_random_bytes caps at 1024,
-- so two chunks; base64 of random bytes is what defeats TOAST compression.
INSERT INTO forcing_version SELECT 'fv_'||i, 'm_'||(i%40),
  CASE WHEN i%2=0 THEN 'gfs' ELSE 'IFS' END, now(), now(), now()+interval '7 days',
  42029, 's3://bucket/fv_'||i, md5(i::text),
  jsonb_build_object('a', encode(gen_random_bytes(650),'base64'),
                     'b', encode(gen_random_bytes(650),'base64'), 'i', i), now()
  FROM generate_series(1,8653) i;
SQL
psql "$TARGET" -At -c "SELECT 'seeded forcing_version heap='||pg_size_pretty(pg_relation_size('forcing_version'))||' (live: 15 MB)';"
( sleep 0.25; psql "$TARGET" -At -c "SELECT '  locks: '||coalesce(string_agg(DISTINCT l.mode,','),'(none seen)')
    FROM pg_locks l JOIN pg_class c ON c.oid=l.relation WHERE c.relname='forcing_version';" ) &
w=$!
psql "$TARGET" -P pager=off -c "\timing on" -c "ALTER TABLE forcing_version ADD COLUMN forcing_version_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;"
wait $w
psql "$TARGET" -At -c "SELECT 'after heap='||pg_size_pretty(pg_relation_size('forcing_version'))||' idx='||pg_size_pretty(pg_indexes_size('forcing_version'));"
echo "=== FV DONE ==="
