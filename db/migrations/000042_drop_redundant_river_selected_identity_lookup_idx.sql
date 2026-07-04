-- run_id is the display run identity and already binds the basin identity.
-- The generic MVT lookup index covers run_id + variable + valid_time +
-- river_network_version_id lookups without carrying another 37GB chunk-index
-- family for basin_version_id.
DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_mvt_selected_identity_lookup_idx;
