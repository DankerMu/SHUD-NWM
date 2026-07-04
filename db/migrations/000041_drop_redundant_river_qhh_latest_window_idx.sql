-- The generic selected-identity lookup index covers the river latest-product
-- window access pattern. Keeping both indexes multiplies every Timescale chunk's
-- write and storage cost without materially improving the steady-state display
-- path, which is served from hydro.run_display_coverage.
DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_qhh_latest_window_idx;
