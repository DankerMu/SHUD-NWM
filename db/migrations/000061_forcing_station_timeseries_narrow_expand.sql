-- I12 (#1991, tasks.md 7.3): atomic, restartable forcing narrow-store expand.
--
-- ONE STATEMENT, deliberately. `packages/common/migrate.py:327` applies
-- migrations with `connection.autocommit = True`, so a multi-statement file is
-- not atomic and a failure half way leaves a catalog no ledger row describes.
-- Restartability comes from INSIDE the block (`to_regclass` / `to_regtype` /
-- `IF NOT EXISTS` guards), exactly as 000059 does for river -- not from
-- splitting the file.
--
-- 000058 is not re-run and is not contradicted: it set a 3-day chunk interval on
-- the table this migration RENAMES to `..._legacy`, and that setting travels with
-- the rename. The narrow table below is created fresh at ONE day
-- (specs/forcing-narrow-store/spec.md:5,13). River did exactly the same thing
-- (000058:6 -> 000059:29). It reads like a regression and is not.
--
-- ---------------------------------------------------------------------------
-- Enum value sets: the three-source union P u S u L, per-value annotated
-- ---------------------------------------------------------------------------
--
-- Vocabulary sources, following the annotation style of
-- `000050_river_identity_normalization.sql:95-122`. Every value carries the
-- source that justifies it, and every literal that exists somewhere in the tree
-- and is NOT here is enumerated as an exclusion below -- adding an enum value
-- later is cheap (`ALTER TYPE ... ADD VALUE`), removing one is not.
--
--   P = workers/forcing_producer/producer.py -- the only production writer of
--       the forcing fact table (via workers/forcing_producer/store.py and
--       packages/common/forcing_domain_handoff_apply.py, which carry the
--       producer's rows over the node-22 -> node-27 handoff unchanged).
--         FORCING_VARIABLES = ("PRCP","TEMP","RH","wind","Rn","Press")
--                                                        (producer.py:87)
--         OUTPUT_UNITS      = PRCP mm/day, TEMP degC, RH 0-1, wind m/s,
--                             Rn W/m2, Press Pa        (producer.py:88-95)
--         quality_flag      = "ok"                      (producer.py:273,296
--                             dataclass default; store.py:175,263 and
--                             file_store.py:568,621 coalesce to the same
--                             literal and no code path writes another)
--   S = db/seeds/seed_demo.py -- demo/dev seeding. Its vocabulary WAS disjoint
--       from production (t2m/rh2m/wind_u/wind_v/precip/srad with units
--       %/mm/h/...); this change corrects the SEED to the production vocabulary
--       instead of widening a production type to accommodate it, so after this
--       change S is a subset of P. See the exclusions below.
--   L = node-27 live distinct values, read-only, over all six
--       met.forcing_station_timeseries chunks, 259 255 326 rows, 2026-09-18
--       (openspec/changes/timeseries-narrow-store-expand-contract/receipts/
--        2026-09-18-i10-forcing-readonly/full-distinct.out). This is a COMPLETE
--       scan, not a pg_stats sample:
--         variable      {PRCP, Press, RH, Rn, TEMP, wind}  (43 209 221 each)
--         unit          {0-1, Pa, W/m2, degC, m/s, mm/day} (43 209 221 each,
--                       in strict 1:1 with variable)
--         quality_flag  {ok}                               (all 259 255 326)
--       Measured: P == L exactly for all three columns.
--
-- DELIBERATELY EXCLUDED, each with the reason:
--
--   1. The pre-change seed vocabulary `t2m`, `rh2m`, `wind_u`, `wind_v`,
--      `precip`, `srad` and the seed-only units `%` (rh2m) and `mm/h` (precip)
--      -- db/seeds/seed_demo.py:87,254-262 as they stood before this change.
--      They match no production writer and no live row. The seed is corrected to
--      the production vocabulary IN THIS SAME CHANGE (with tests/test_seed.py
--      cascading), which is 000050:119-122's own recorded precedent: that
--      migration excluded the `'m3 s-1'` literal that existed only in a test and
--      corrected the test rather than widening the type. River could INCLUDE its
--      seed vocabulary (000050:109-112) only because it overlapped production;
--      forcing's did not overlap at all.
--   2. `qc_warning`, excluded from met.forcing_quality_flag. It is a RIVER value
--      (hydro.river_quality_flag, 000050:127-138, written by
--      workers/output_parser/parser.py:190). No forcing writer emits it and it
--      appears in zero live rows.
--   3. `fail`, excluded from met.forcing_quality_flag. `workers/forcing_producer/
--      file_store.py:182` reads `product.quality_flag == "fail"`, but `product`
--      is a canonical met product record from the object store and the
--      comparison is a PRE-INGEST FILTER that drops the product before any fact
--      row exists. It is not a fact-table value and must not widen this type.
--   4. Every IFS/GFS canonical variable name (`prcp_rate_or_amount`,
--      `air_temperature_2m`, ...; producer.py:60-86) and every canonical unit in
--      `EXPECTED_CANONICAL_UNITS` (producer.py:96-106). Those name the OBJECT
--      STORE's vocabulary upstream of interpolation; the fact table only ever
--      receives the `FORCING_VARIABLES` / `OUTPUT_UNITS` image of them.
--
-- `native_resolution` stays free TEXT and NULLABLE. It is an ATTRIBUTE, not an
-- identity column, so invariant I1 ("no text identity column on a narrow fact
-- row") does not reach it; live values are {3h, 6h} but there is no writer
-- constraining them and no reader switching on them, so an enum here would buy
-- nothing and cost a migration the next time a source changes cadence.
--
-- ---------------------------------------------------------------------------
-- Routing backfill: the oracle is ROW EXISTENCE, and that is not a shortcut
-- ---------------------------------------------------------------------------
--
-- River classifies with `parsed_at IS NOT NULL OR status IN ('parsed',
-- 'published')` (000059:78-79). `met.forcing_version` (000005_met.sql:74-86) has
-- NEITHER column -- no `parsed_at`, no `status` -- so that predicate has nothing
-- to map onto. The only fact available is whether the version has rows in the
-- renamed legacy table, so that is what is used. It is a PK-leading probe per
-- version (the legacy PK leads with `forcing_version_id`), i.e. an index
-- descent per row rather than a scan of the 259M-row hypertable. Do not ask for
-- river's shape here; it does not exist on this side.
--
-- Classification happens ONLY on installation, inside the `IF NOT EXISTS` guard,
-- for 000059:70's reason: a replay must not reclassify a version that a
-- narrow-only writer has since populated.
--
-- ---------------------------------------------------------------------------
-- Lock budget: >= ~24 s COLD, >= ~3 s warm -- the block, not one step
-- ---------------------------------------------------------------------------
--
-- There is no BEGIN/COMMIT in this file and that is not an omission: it is ONE
-- `DO $$` block, hence ONE implicit transaction (see the note at the top of
-- this file). So every AccessExclusiveLock it takes -- step 1's two table
-- rewrites, step 3's RENAME of the 259M-row fact table, step 11's ADD COLUMN on
-- met.forcing_version -- is held until the BLOCK ends. The window to plan
-- against is the SUM, and the two measured components come from the SAME
-- receipt
-- (`openspec/changes/timeseries-narrow-store-expand-contract/receipts/`
-- `2026-09-18-i10-forcing-readonly/README.md`):
--
--   component                                        cold        warm
--   -------------------------------------------      --------    ------
--   IDENTITY rewrites, §3 (:53-65)                    ~2 552 ms   --
--     met.met_station, live PAGE constitution          1 863 ms
--     met.forcing_version                                689 ms
--   routing backfill existence probe, §4 (:82-96)    21 849 ms    238 ms
--     (one PK-leading probe per version, all 8 653)
--   step 11 ADD COLUMN + UPDATE on met.forcing_version  UNMEASURED
--   -------------------------------------------      --------    ------
--   MEASURED TOTAL, AccessExclusiveLock window           ~24 s      ~3 s
--
-- The totals are a LOWER BOUND. Step 11's `ADD COLUMN timeseries_store TEXT NOT
-- NULL DEFAULT 'narrow'` and the `UPDATE` beneath it take their own
-- AccessExclusiveLock on met.forcing_version and are not in the receipt; the
-- ADD COLUMN is a catalog-only change on PG11+ (non-volatile default) and the
-- UPDATE touches at most the 4 764 versions §4 found rows for, so it is
-- expected to be small -- but no number is invented here. 8.1 measures it.
--
-- `ADD COLUMN ... INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE` carries a
-- volatile default, so PostgreSQL rewrites the whole table and then builds the
-- UNIQUE index; §3 also measures met.met_station at 1 141 ms on the live DATA
-- constitution, and the window is planned against the PAGE figure (1 863 ms)
-- because that is the worse one. `identity.out`'s 734/625 ms are a rejected
-- first attempt (same receipt, :71) and must not be quoted.
--
-- Quoting the 1 863 ms IDENTITY figure ALONE understates this by roughly 10x;
-- the backfill is the dominant term on a cold cache and it is not optional.
-- Task 8.1 plans the production window from the TOTAL above. The lock covers
-- met.forcing_station_timeseries(_legacy), met.met_station and
-- met.forcing_version -- the display API on the active primary reads all three.
DO $$
DECLARE
    drift_count integer;
BEGIN
    -- 1. Integer surrogate keys on the two authority tables. River already had
    --    these from 000050; forcing has none today
    --    (`grep -rn "forcing_version_key\|station_key" db/` was empty), so this
    --    step is new and it is the lock event above.
    ALTER TABLE met.met_station
        ADD COLUMN IF NOT EXISTS station_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
    ALTER TABLE met.forcing_version
        ADD COLUMN IF NOT EXISTS forcing_version_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;

    -- 2. Native enum types, named `<domain>_<concept>` per 000050. There is no
    --    `CREATE TYPE IF NOT EXISTS`, and 000050's `duplicate_object` handler is
    --    per-block and cannot be reused inside this single block, so the guard
    --    is a `to_regtype` probe.
    IF to_regtype('met.forcing_variable') IS NULL THEN
        CREATE TYPE met.forcing_variable AS ENUM (
            'PRCP',   -- P (producer.py:87), L
            'TEMP',   -- P (producer.py:87), L
            'RH',     -- P (producer.py:87), L
            'wind',   -- P (producer.py:87), L
            'Rn',     -- P (producer.py:87), L
            'Press'   -- P (producer.py:87), L
        );
    END IF;
    IF to_regtype('met.forcing_unit') IS NULL THEN
        CREATE TYPE met.forcing_unit AS ENUM (
            'mm/day',  -- P (producer.py:89, PRCP), L
            'degC',    -- P (producer.py:90, TEMP), L
            '0-1',     -- P (producer.py:91, RH), L
            'm/s',     -- P (producer.py:92, wind), L
            'W/m2',    -- P (producer.py:93, Rn), L
            'Pa'       -- P (producer.py:94, Press), L
        );
    END IF;
    IF to_regtype('met.forcing_quality_flag') IS NULL THEN
        CREATE TYPE met.forcing_quality_flag AS ENUM (
            'ok'       -- P (producer.py:273,296 default), L (all 259 255 326 rows)
        );
    END IF;

    -- 3. Rename. The legacy table keeps its original shape, indexes (including
    --    `forcing_station_timeseries_qhh_latest_window_idx`, which travels with
    --    it) and owner; NO key columns are added to it (spec :13).
    IF to_regclass('met.forcing_station_timeseries_legacy') IS NULL THEN
        ALTER TABLE met.forcing_station_timeseries RENAME TO forcing_station_timeseries_legacy;
    END IF;

-- 4/5/9. The narrow table. Exactly the column set of spec :5 -- note
--        `native_resolution`, which river has no counterpart for and which a
--        transcription of 000059 silently drops. Foreign keys are ENFORCED
--        (inline, as 000059:13,16), not documented-only. The primary key is
--        named explicitly because the legacy table took the auto-generated
--        `forcing_station_timeseries_pkey` index name with it through the
--        rename, and index names are unique per schema.
CREATE TABLE IF NOT EXISTS met.forcing_station_timeseries (
    forcing_version_key INTEGER NOT NULL REFERENCES met.forcing_version(forcing_version_key),
    station_key INTEGER NOT NULL REFERENCES met.met_station(station_key),
    valid_time TIMESTAMPTZ NOT NULL,
    variable_e met.forcing_variable NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit_e met.forcing_unit NOT NULL,
    quality_flag_e met.forcing_quality_flag NOT NULL,
    native_resolution TEXT,
    CONSTRAINT forcing_station_timeseries_narrow_pkey
        PRIMARY KEY (forcing_version_key, station_key, variable_e, valid_time)
);

-- 7. One-day chunks (spec :5,13), no default indexes.
PERFORM create_hypertable('met.forcing_station_timeseries', 'valid_time',
    chunk_time_interval => interval '1 day', create_default_indexes => false,
    if_not_exists => true);

-- 6. EXACTLY ONE secondary index: the key-shaped successor of
--    `forcing_station_timeseries_qhh_latest_window_idx` (000024:9), which the
--    QHH latest-product fallback CTE uses. River creates two (000059:32-36);
--    forcing's second river index has no forcing counterpart.
CREATE INDEX IF NOT EXISTS forcing_ts_version_variable_time_key_idx
    ON met.forcing_station_timeseries
    (forcing_version_key, variable_e, valid_time DESC);

    -- 8. Compression, set as the LAST schema DDL and drift-guarded like
    --    000059:38-65. These four rows must match
    --    `scripts/node27_timeseries_compression_supervisor.py:1748-1757`
    --    VERBATIM: that supervisor already pins the forcing narrow table's
    --    segmentby/orderby, so a deviation here does not fail loudly -- it turns
    --    "the supervisor accepts the mixed catalog with no code change" into a
    --    silent false pass.
    WITH live AS (
        SELECT attname::text AS attname,
               segmentby_column_index::int AS segmentby_column_index,
               orderby_column_index::int AS orderby_column_index,
               orderby_asc::boolean AS orderby_asc,
               orderby_nullsfirst::boolean AS orderby_nullsfirst
        FROM timescaledb_information.compression_settings
        WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries'
    ), expected AS (
        SELECT * FROM (VALUES
            ('forcing_version_key'::text, 1::int, NULL::int, NULL::boolean, NULL::boolean),
            ('station_key'::text, 2::int, NULL::int, NULL::boolean, NULL::boolean),
            ('variable_e'::text, NULL::int, 1::int, true::boolean, false::boolean),
            ('valid_time'::text, NULL::int, 2::int, true::boolean, false::boolean)
        ) AS t(attname, segmentby_column_index, orderby_column_index, orderby_asc, orderby_nullsfirst)
    )
    SELECT count(*) INTO drift_count FROM (
        (SELECT * FROM live EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM live)
    ) AS mismatch;
    IF drift_count <> 0 THEN
    ALTER TABLE met.forcing_station_timeseries SET (
        timescaledb.compress = true,
        timescaledb.compress_segmentby = 'forcing_version_key, station_key',
        timescaledb.compress_orderby = 'variable_e, valid_time'
    );
    END IF;

-- 10. Fail closed if deployment has not provisioned its runtime owner.
ALTER TABLE met.forcing_station_timeseries OWNER TO nhms_ingest_rw;

-- 11. Routing column. Classify only on installation; replay must preserve
--     versions a narrow-only writer has populated since.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'met' AND table_name = 'forcing_version'
          AND column_name = 'timeseries_store'
    ) THEN
        ALTER TABLE met.forcing_version ADD COLUMN timeseries_store TEXT NOT NULL DEFAULT 'narrow'
            CHECK (timeseries_store IN ('legacy', 'narrow'));
        UPDATE met.forcing_version fv SET timeseries_store = 'legacy'
        WHERE EXISTS (
            SELECT 1 FROM met.forcing_station_timeseries_legacy legacy
            WHERE legacy.forcing_version_id = fv.forcing_version_id
        );
    END IF;
END;
$$;
