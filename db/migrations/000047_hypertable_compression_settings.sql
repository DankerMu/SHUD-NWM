-- Change `tier-node27-timeseries-storage` decision D3 (issue #851, follow-up to
-- the tiering foundation captured in #845 / ADR 0002). TimescaleDB native
-- compression is enabled on the two detail hypertables so that a receipted
-- runner (`scripts/node27_timeseries_compression.py`, task 4.2 of the same
-- change) can subsequently invoke `compress_chunk` on terminal chunks. This
-- migration only pins the per-hypertable compression *settings*; it never
-- calls `add_compression_policy` — D3 rejects the background policy job
-- outright because it publishes no receipt, respects no per-tick bound, and
-- is invisible to the node-27 governance audit trail this project runs on.
--
-- Segmentby covers the primary-key column set of each hypertable, which is
-- the TimescaleDB 2.10 unique-constraint requirement. It also happens to
-- match the equality filters of the curve and station-MVT read shapes, so
-- compressed-chunk reads stay index-driven:
--
--   - hydro.river_timeseries         PK/segmentby: run_id,
--                                    river_network_version_id,
--                                    river_segment_id
--                                    orderby: variable, valid_time
--   - met.forcing_station_timeseries PK/segmentby: forcing_version_id,
--                                    station_id
--                                    orderby: variable, valid_time
--
-- G8 re-runnability contract (issue #1069, measured on node-27 PG 15.2 +
-- TimescaleDB 2.10.2, 2026-07-16): `ALTER TABLE ... SET
-- (timescaledb.compress ...)` is NOT re-runnable once the hypertable has any
-- compressed chunk — TimescaleDB rejects it with
-- `ERROR: cannot change configuration on already compressed chunks`
-- even when the requested settings exactly match the catalog. Each hypertable
-- is therefore wrapped in a guarded DO block: the guard reads
-- `timescaledb_information.compression_settings` and, when the live rows
-- already exactly match the D3-expected rows below, skips the ALTER entirely.
-- Otherwise it attempts the original ALTER: on a fresh database the view has
-- zero rows for the table, so the ALTER runs; on drifted settings with
-- compressed chunks TimescaleDB itself fails closed with the error above (no
-- extra RAISE logic is layered on top).
--
-- Each table's DO block is a separate top-level statement, so under the
-- autocommit apply lanes (packages/common/migrate.py and the psql replay
-- lane) each block runs in its own implicit transaction. A partial apply
-- (one table succeeded, the other did not) is completed by a re-run: the
-- succeeded table's guard no-ops because its catalog already matches D3, and
-- the other table's guard applies. The blocks touch disjoint tables and are
-- order-independent. No explicit transaction wrapper is used, matching the
-- repo house style
-- (see 000045_hydro_run_type_hindcast.sql, 000046_state_snapshot_clone_provenance.sql).

DO $$
DECLARE
    drift_count integer;
BEGIN
    WITH live AS (
        SELECT attname::text AS attname,
               segmentby_column_index::int AS segmentby_column_index,
               orderby_column_index::int AS orderby_column_index,
               orderby_asc::boolean AS orderby_asc,
               orderby_nullsfirst::boolean AS orderby_nullsfirst
        FROM timescaledb_information.compression_settings
        WHERE hypertable_schema = 'hydro'
          AND hypertable_name = 'river_timeseries'
    ), expected AS (
        SELECT *
        FROM (VALUES
            ('run_id'::text, 1::int, NULL::int, NULL::boolean, NULL::boolean),
            ('river_network_version_id'::text, 2::int, NULL::int, NULL::boolean, NULL::boolean),
            ('river_segment_id'::text, 3::int, NULL::int, NULL::boolean, NULL::boolean),
            ('variable'::text, NULL::int, 1::int, true::boolean, false::boolean),
            ('valid_time'::text, NULL::int, 2::int, true::boolean, false::boolean)
        ) AS t(attname, segmentby_column_index, orderby_column_index, orderby_asc, orderby_nullsfirst)
    )
    SELECT count(*) INTO drift_count
    FROM (
        (SELECT * FROM live EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM live)
    ) AS mismatch;

    IF drift_count = 0 THEN
        RETURN;
    END IF;

    ALTER TABLE hydro.river_timeseries SET (
        timescaledb.compress = true,
        timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id',
        timescaledb.compress_orderby = 'variable, valid_time'
    );
END;
$$;

DO $$
DECLARE
    drift_count integer;
BEGIN
    WITH live AS (
        SELECT attname::text AS attname,
               segmentby_column_index::int AS segmentby_column_index,
               orderby_column_index::int AS orderby_column_index,
               orderby_asc::boolean AS orderby_asc,
               orderby_nullsfirst::boolean AS orderby_nullsfirst
        FROM timescaledb_information.compression_settings
        WHERE hypertable_schema = 'met'
          AND hypertable_name = 'forcing_station_timeseries'
    ), expected AS (
        SELECT *
        FROM (VALUES
            ('forcing_version_id'::text, 1::int, NULL::int, NULL::boolean, NULL::boolean),
            ('station_id'::text, 2::int, NULL::int, NULL::boolean, NULL::boolean),
            ('variable'::text, NULL::int, 1::int, true::boolean, false::boolean),
            ('valid_time'::text, NULL::int, 2::int, true::boolean, false::boolean)
        ) AS t(attname, segmentby_column_index, orderby_column_index, orderby_asc, orderby_nullsfirst)
    )
    SELECT count(*) INTO drift_count
    FROM (
        (SELECT * FROM live EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM live)
    ) AS mismatch;

    IF drift_count = 0 THEN
        RETURN;
    END IF;

    ALTER TABLE met.forcing_station_timeseries SET (
        timescaledb.compress = true,
        timescaledb.compress_segmentby = 'forcing_version_id, station_id',
        timescaledb.compress_orderby = 'variable, valid_time'
    );
END;
$$;
