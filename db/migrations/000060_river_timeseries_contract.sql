-- I9 / task 6.2: river contract. Drops the expand migration's transitional
-- surface once no legacy-routed run can still be visible.
--
-- One DO block on purpose. packages/common/migrate.py:327 runs migrations with
-- `connection.autocommit = True` and splits the file into statements, so a
-- multi-statement migration is NOT atomic: a failure midway leaves the earlier
-- statements committed while the ledger row is never recorded. A single DO
-- block is one statement, hence one transaction, hence all-or-nothing.
--
-- LOCKS -- read this before running it against live traffic. An earlier version
-- of this comment claimed the only ACCESS EXCLUSIVE lock was taken by the final
-- ALTER and that lock_timeout bounded the exposure to 5s. Both were wrong.
--
--   * The DROP TABLE below is the FIRST DDL in the block, and it takes ACCESS
--     EXCLUSIVE on `core.river_segment` as well as on the legacy table: the
--     legacy table carries an outgoing FK to core.river_segment (000006_hydro.sql:57-59),
--     and dropping that constraint removes the RI trigger on the REFERENCED
--     side, which locks core.river_segment. TimescaleDB replicates the
--     constraint per chunk, so there are more triggers but the same one lock.
--     core.river_segment is read by MVT, /river-segments and /forecast-series.
--   * The final ALTER then takes ACCESS EXCLUSIVE on hydro_run, upgrading the
--     ACCESS SHARE the refusal count already holds -- a lock upgrade, which a
--     conflicting concurrent request resolves via lock_timeout or deadlock
--     detection. That aborts safely (whole block rolls back), but it is a real
--     abort path, not "the window is minimised".
--   * lock_timeout bounds how long we WAIT for a lock, not how long we HOLD it.
--     Every lock above is held until COMMIT, and COMMIT is where the 717 GB of
--     relation segments are actually unlinked. statement_timeout is unset by
--     default (packages/common/migrate.py:258) and does not cover commit either.
--
-- Therefore: run this in a maintenance window with the API and parser units
-- stopped. That is a requirement, not a recommendation. The block is atomic, so
-- an abort is always safe -- but "safe to abort" is not "safe to run hot".
DO $$
DECLARE
    window_days integer;
    in_window   bigint;
BEGIN
    -- Retention window: there is NO in-database source of truth for it. The
    -- retention runner reads NODE27_TIMESERIES_RETENTION_WINDOW_DAYS from a
    -- node-27 env file that is not in the repo; the live value measured on
    -- 2026-09-19 is 21, while packages/common/storage.py:35 defaults to 14 and
    -- openspec/changes/timeseries-narrow-store-expand-contract/fixtures/I6-1985.md:103
    -- records that 14-vs-21 drift as deliberate, unchanged history.
    --
    -- A migration cannot read that file, so the floor is pinned at the live
    -- value and a GUC may only WIDEN it. Narrowing is the unsafe direction: a
    -- window shorter than the deployment's would let a run whose facts
    -- retention has not yet dropped slip past the refusal and lose data.
    window_days := GREATEST(
        21,
        COALESCE(NULLIF(current_setting('nhms.retention_window_days', true), '')::integer, 0)
    );

    -- Guarded so a re-run after the column is gone is a no-op, not an error.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hydro'
          AND table_name = 'hydro_run'
          AND column_name = 'timeseries_store'
    ) THEN
        SELECT count(*) INTO in_window
        FROM hydro.hydro_run
        WHERE timeseries_store = 'legacy'
          AND end_time > now() - make_interval(days => window_days);

        IF in_window <> 0 THEN
            -- end_time and timeseries_store are both NOT NULL (000006_hydro.sql:55,
            -- 000059:76), so no row escapes this predicate by being NULL.
            RAISE EXCEPTION
                'contract refused: % legacy-routed run(s) with end_time inside the %-day '
                'retention window; bring them to the narrow store with the #2382 reparse '
                'backfill or wait for retention, then re-run -- nothing was changed',
                in_window, window_days;
        END IF;
    ELSIF to_regclass('hydro.river_timeseries_legacy') IS NOT NULL THEN
        -- Column already gone but the table still here: an operator who hit the
        -- refusal and hand-dropped the column would otherwise get an
        -- unconditional 717 GB drop, because the routing column is this
        -- migration's ONLY evidence of what is still routed legacy. Refuse
        -- instead. A successful run leaves BOTH gone, so replay still no-ops.
        RAISE EXCEPTION
            'contract refused: hydro.hydro_run.timeseries_store is already absent while '
            'hydro.river_timeseries_legacy still exists; the routing column is the only '
            'evidence of what is still routed legacy, so the drop cannot be justified '
            '-- nothing was changed';
    END IF;

    -- Legacy-routed runs outside the window keep route `legacy`; their remaining
    -- chunks go with the table, the same visibility loss retention imposes.
    -- Their hydro.run_display_coverage rows are deliberately left untouched:
    -- the #1446 overwrite guard (packages/common/display_coverage.py:687-689,
    -- the `WHERE %(force)s OR EXCLUDED.segment_count > 0 OR ... = 0` clause on
    -- the upsert -- NOT :699-701, which is only the cheap-path existence gate)
    -- keeps them frozen at their last populated values, and the contract must
    -- never zero or delete them.
    DROP TABLE IF EXISTS hydro.river_timeseries_legacy;

    -- Both defined in 000050_river_identity_normalization.sql, both zero-arg.
    DROP FUNCTION IF EXISTS hydro.cutover_river_identity_normalization();
    DROP FUNCTION IF EXISTS hydro.verify_river_identity_normalization();

    -- Takes ACCESS EXCLUSIVE on hydro_run; the routing-free code must already be
    -- the RUNNING process (not merely the checked-out tree) before this lands.
    -- The hydro_run_timeseries_store_check CHECK constraint drops with the column.
    ALTER TABLE hydro.hydro_run DROP COLUMN IF EXISTS timeseries_store;
END;
$$;
