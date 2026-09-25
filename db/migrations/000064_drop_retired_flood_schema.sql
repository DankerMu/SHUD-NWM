-- Convergence (#2048). Commit b97c16e2 retired the frequency pipeline by deleting
-- the flood migrations (000007, 000015, 000017, 000020, 000034, 000036) and the
-- CREATE SCHEMA in 000002 instead of dropping the objects forward. Production
-- therefore still holds the schema: flood_frequency_curve, return_period_result
-- (a hypertable) and run_product_quality, with their indexes, constraints and
-- TimescaleDB's insert-blocker trigger, all empty, and no code under apps/,
-- packages/, services/ or workers/ reads them. This drops them.
--
-- Fail-closed, in ONE DO block so the guard and the drops are one transaction:
--
--   1. each flood table that exists is locked ACCESS EXCLUSIVE and then
--      counted, and a single row aborts the migration. The lock comes first
--      so no insert can land between the count and the drop;
--   2. each table is dropped WITHOUT CASCADE, so an object outside the schema
--      that depends on one (a view, a foreign key into it) aborts the migration
--      and nothing is dropped. The hypertable's chunks go with it;
--   3. the schema is dropped WITHOUT CASCADE, so a leftover object nobody
--      recorded aborts it too.
--
-- On a database built from db/migrations there is no flood schema and every
-- step is a no-op. The foreign keys from the flood tables to hydro.hydro_run
-- and core.model_instance belong to the flood tables and go with them, which is
-- what the migration ledger already describes.
--
-- Dynamic EXECUTE throughout, so PL/pgSQL never prepares a statement against a
-- table that does not exist. Production rollback is a pg_restore of the pre-apply
-- `pg_dump -n flood` plus deleting this file's public.schema_migrations row.
DO $flood$
DECLARE
  retired_tables CONSTANT text[] := ARRAY[
    'flood.flood_frequency_curve',
    'flood.return_period_result',
    'flood.run_product_quality'
  ];
  retired_name text;
  retired regclass;
  has_rows boolean;
BEGIN
  FOREACH retired_name IN ARRAY retired_tables LOOP
    retired := to_regclass(retired_name);
    CONTINUE WHEN retired IS NULL;
    EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', retired);
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s)', retired) INTO has_rows;
    IF has_rows THEN
      RAISE EXCEPTION 'refusing to drop retired table %: it holds rows', retired_name
        USING HINT = 'Back the rows up and decide their fate before rerunning 000064 (#2048).';
    END IF;
  END LOOP;

  FOREACH retired_name IN ARRAY retired_tables LOOP
    retired := to_regclass(retired_name);
    CONTINUE WHEN retired IS NULL;
    EXECUTE format('DROP TABLE %s', retired);
  END LOOP;

  DROP SCHEMA IF EXISTS flood;
END
$flood$;
