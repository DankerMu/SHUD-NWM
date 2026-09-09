-- #2210: change only the interval used when new hot-timeseries chunks are created.
-- Existing chunk ranges, rows and compression states remain unchanged.
-- Compression lag, retention and other hypertables are outside this migration.
-- Both assignments are repeatable, including after a partially applied migration.

SELECT set_chunk_time_interval('hydro.river_timeseries', INTERVAL '3 days');
SELECT set_chunk_time_interval('met.forcing_station_timeseries', INTERVAL '3 days');
