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
-- `ALTER TABLE ... SET (timescaledb.compress = ...)` is idempotent on both
-- fresh and already-compression-configured hypertables, so re-applying this
-- migration after a partial apply (one hypertable succeeded, the other did
-- not) will complete the second statement without erroring on the first.
-- The statements are order-independent and touch disjoint tables; no
-- transaction wrapper is used, matching the repo house style
-- (see 000045_hydro_run_type_hindcast.sql, 000046_state_snapshot_clone_provenance.sql).

ALTER TABLE hydro.river_timeseries SET (
    timescaledb.compress = true,
    timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id',
    timescaledb.compress_orderby = 'variable, valid_time'
);

ALTER TABLE met.forcing_station_timeseries SET (
    timescaledb.compress = true,
    timescaledb.compress_segmentby = 'forcing_version_id, station_id',
    timescaledb.compress_orderby = 'variable, valid_time'
);
