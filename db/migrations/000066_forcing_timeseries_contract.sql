-- I14 / task 8.2 (#1993): forcing contract. Drops the forcing expand
-- migration's (000061) transitional surface once no legacy-routed forcing
-- version can still be visible. The forcing mirror of
-- 000060_river_timeseries_contract.sql.
--
-- One DO block on purpose. packages/common/migrate.py runs migrations with
-- `connection.autocommit = True` and splits the file into statements, so a
-- multi-statement migration is NOT atomic: a failure midway leaves the earlier
-- statements committed while the ledger row is never recorded. A single DO
-- block is one statement, hence one transaction, hence all-or-nothing.
--
-- LOCKS -- read this before running it against live traffic.
--
--   * The DROP TABLE below is the FIRST DDL in the block. It takes ACCESS
--     EXCLUSIVE on the legacy table and ALSO on `met.forcing_version` and
--     `met.met_station`: the legacy table carries an outgoing FK to each
--     (000005_met.sql:100,102), and dropping those constraints removes the RI
--     triggers on the REFERENCED side, which locks both referenced tables.
--     TimescaleDB replicates the constraints per chunk, so there are more
--     triggers but the same two locks (measured 2026-10-11 on a TimescaleDB
--     2.10.2 catalog built to 000065: after the DROP TABLE alone, pg_locks
--     shows AccessExclusiveLock on both). `met.forcing_version` is written by
--     the autopipe on every tick and read, with `met.met_station`, by the QHH
--     latest-product path and the station endpoints.
--   * That first DDL is therefore also a lock UPGRADE on `met.forcing_version`:
--     the refusal count above it already holds ACCESS SHARE there. A
--     conflicting concurrent session resolves the upgrade through lock_timeout
--     or deadlock detection. That aborts safely (the whole block rolls back),
--     but it is a real abort path.
--   * The final ALTER needs ACCESS EXCLUSIVE on `met.forcing_version` for the
--     column drop and finds it already held; it adds no new lock and no new
--     wait.
--   * lock_timeout bounds how long we WAIT for a lock, not how long we HOLD
--     it. This file does not set it: the migrate lane does (default 5s,
--     packages/common/migrate.py). Every lock above is held until COMMIT, and
--     COMMIT is where the legacy relation segments (32 GB, 3 chunks, measured
--     read-only on node-27 2026-10-11) are actually unlinked.
--
-- Therefore: run this in a maintenance window with the display API stopped and
-- the autopipe, download, compression and retention timers paused -- the
-- procedure is "Forcing contract" in
-- docs/runbooks/tier-node27-timeseries-storage.md (fixture
-- I14-1993-task82.md D6). That is a requirement, not a recommendation. The
-- block is atomic, so an abort is always safe -- but "safe to abort" is not
-- "safe to run hot".
--
-- This file's own gate has been satisfiable since 2026-10-09, so it does NOT
-- protect itself against being applied in passing: any
-- `packages.common.migrate` run on a database that still has the legacy table
-- executes the drop. The window is an operator discipline, not a property of
-- the SQL.
--
-- There is NO down migration. The legacy facts are gone at COMMIT; recovery
-- from a wrong apply is restore-from-backup.
DO $$
DECLARE
    -- The time column the refusal window is measured on. ONE token on purpose
    -- (specs/forcing-narrow-store/spec.md): revisiting the choice is an edit to
    -- this line and nothing else. The column is NULLABLE on met.forcing_version
    -- (000005_met.sql:78), unlike river's end_time, so NULL is handled below.
    c_time_column constant text := 'cycle_time';
    window_days   integer;
    in_window     bigint;
BEGIN
    -- Retention window: there is NO in-database source of truth for it (see
    -- 000060 for the full account). The floor is pinned at the live value and a
    -- GUC may only WIDEN it; narrowing is the direction that loses data.
    window_days := GREATEST(
        21,
        COALESCE(NULLIF(current_setting('nhms.retention_window_days', true), '')::integer, 0)
    );

    -- Guarded so a re-run after the column is gone is a no-op, not an error.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'met'
          AND table_name = 'forcing_version'
          AND column_name = 'timeseries_store'
    ) THEN
        -- Dynamic only so the time column is spelled once, above. NULL is
        -- fail-closed: a legacy-routed version with no value in that column
        -- cannot be shown to be outside the window, so it counts as inside.
        EXECUTE format(
            'SELECT count(*) FROM met.forcing_version '
            'WHERE timeseries_store = %L '
            'AND (%I IS NULL OR %I > now() - make_interval(days => $1))',
            'legacy', c_time_column, c_time_column
        ) INTO in_window USING window_days;

        IF in_window <> 0 THEN
            RAISE EXCEPTION
                'contract refused: % legacy-routed forcing version(s) with % inside the %-day '
                'retention window (a NULL % counts as inside); wait for retention, then '
                're-run -- nothing was changed',
                in_window, c_time_column, window_days, c_time_column;
        END IF;
    ELSIF to_regclass('met.forcing_station_timeseries_legacy') IS NOT NULL THEN
        -- Column already gone but the table still here: the routing column is
        -- this migration's ONLY evidence of what is still routed legacy, so a
        -- half-applied or hand-edited catalog must not be papered over with an
        -- unconditional drop. A successful run leaves BOTH gone, so replay
        -- still no-ops.
        RAISE EXCEPTION
            'contract refused: met.forcing_version.timeseries_store is already absent while '
            'met.forcing_station_timeseries_legacy still exists; the routing column is the only '
            'evidence of what is still routed legacy, so the drop cannot be justified '
            '-- nothing was changed';
    END IF;

    -- Legacy-routed versions outside the window keep their route until the
    -- column goes; their remaining chunks go with the table, the same
    -- visibility loss retention imposes. The primary key, the indexes
    -- (000024's `..._qhh_latest_window_idx` among them), every chunk, both
    -- outgoing foreign keys and the grants go with it. No CASCADE: an unknown
    -- dependant aborts the contract instead of being eaten.
    DROP TABLE IF EXISTS met.forcing_station_timeseries_legacy;

    -- `met` has no cutover / verify functions (river's 000050 pair has no
    -- forcing counterpart), so there is nothing between the two drops.
    --
    -- Takes ACCESS EXCLUSIVE on met.forcing_version; the routing-free code must
    -- already be the RUNNING process (not merely the checked-out tree) before
    -- this lands. The column's DEFAULT and its CHECK constraint drop with it.
    -- Kept: forcing_version_key, station_key, the three met.forcing_* enums,
    -- the narrow table and its indexes.
    ALTER TABLE met.forcing_version DROP COLUMN IF EXISTS timeseries_store;
END;
$$;
