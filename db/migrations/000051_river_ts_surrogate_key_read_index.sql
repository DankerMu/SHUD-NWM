-- Change `river-ts-read-path-surrogate-keys` (issue #1341). Adds the integer
-- discovery index that serves the display-boundary read shapes after those
-- readers switch their fact-table predicates from the repeated text identity
-- columns to the surrogate key / enum columns added by 000050 (issue #1339)
-- and dual-written by #1340.
--
-- This migration adds ONE index and drops NOTHING. The text indexes stay in
-- place on purpose: they remain the rollback path (reverting the reader code
-- restores text predicates with no schema change) and they still serve the
-- out-of-boundary text readers (packages/common/forecast_store.py,
-- services/tile_publisher, scripts/node27_autopipeline.py). Retiring the text
-- columns and their indexes is issue #1342, not this one.
--
-- ---------------------------------------------------------------------------
-- SHAPE: the key-form equivalent of the retained text discovery index
-- ---------------------------------------------------------------------------
--
-- (run_key, basin_version_key, river_network_version_key, variable_e,
--  valid_time DESC) mirrors
-- river_timeseries_mvt_selected_identity_valid_time_discovery_idx
-- (000021: run_id, basin_version_id, river_network_version_id, variable,
--  valid_time DESC) column for column, in the same order.
--
-- One index serves every switched in-boundary shape:
--   * hydro tile point lookup -- all five columns bound by equality
--     (services/tiles/mvt.py, hydro layer source CTE);
--   * valid_times_for_layer named-identity discovery -- strict four-column
--     equality prefix plus an ordered valid_time scan, which is exactly the
--     shape whose text-side plan flip is recorded in issue #1378;
--   * display coverage river scan -- run_key-leading prefix
--     (packages/common/display_coverage.py);
--   * MVT source-identity existence probe
--     (apps/api/routes/hydro_display.py).
--
-- No second index is created. The national-tile legs bind run_key +
-- river_network_version_key without basin_version_key and therefore use this
-- index only as a run_key prefix; that is the same coverage the text era gave
-- them (000049 measured both national legs falling back to the primary key
-- after its drop), so nothing regresses, and speculating a second
-- billion-row index ahead of a measured need is not free. The post-cutover
-- index set is decided in #1342.
--
-- ---------------------------------------------------------------------------
-- OPERATIONAL CONSTRAINT -- READ BEFORE APPLYING ON node-27
-- ---------------------------------------------------------------------------
--
-- (i) CREATE INDEX CONCURRENTLY is REJECTED on this hypertable. The #1338
--     live rollback pre-check on node-27 (2026-08-14) got
--     'ERROR: hypertables do not support concurrent index creation'
--     (TimescaleDB 2.10.2, compressed chunks present) while a plain
--     CREATE INDEX was accepted. There is no non-blocking variant, so the
--     statement below is deliberately NOT concurrent -- exactly the
--     constraint recorded in the 000049 header.
-- (ii) The plain CREATE INDEX holds a SHARE lock on the target hypertable for
--     the WHOLE build. That blocks ingest writes (workers/output_parser) for
--     the duration; it does not block readers. The build must therefore be
--     scheduled inside a 12h-cycle gap on node-27, and the start time, end
--     time, elapsed wall time and resulting index size go into the delivery
--     receipt.
-- (iii) Compressed chunks present at build time do not block the build (same
--     precondition under which 000049's plain-CREATE rollback was measured to
--     be accepted). Rows whose surrogate keys are still NULL are simply not
--     discoverable through this index -- that exclusion is a recorded,
--     retention-bounded contract of issue #1341, not an index defect.
-- (iv) On CI and hermetic test databases the table is empty or tiny and the
--     build is instantaneous; IF NOT EXISTS keeps the statement replayable.
--
-- Rollback: DROP INDEX CONCURRENTLY IF EXISTS
-- hydro.river_ts_selected_identity_key_valid_time_idx; (a concurrent DROP is
-- allowed even though a concurrent CREATE is not). Reverting the reader code
-- alone is already a complete functional rollback -- the text columns and
-- text indexes are untouched by this file.

CREATE INDEX IF NOT EXISTS river_ts_selected_identity_key_valid_time_idx
    ON hydro.river_timeseries (
        run_key,
        basin_version_key,
        river_network_version_key,
        variable_e,
        valid_time DESC
    );
