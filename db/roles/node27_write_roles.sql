-- node-27 write-path least-privilege roles (issue #1774).
--
-- Provisions the two non-superuser login roles the runtime env templates have
-- always named -- `nhms_ingest_rw` and `nhms_download_rw` -- and makes
-- `nhms_ingest_rw` the owner of every relation in the six application schemas
-- so that both stats-guard ANALYZE legs (#1643 frontier chunks, #1468 authority
-- tables) and every TimescaleDB tiering function keep working without a
-- superuser.  `nhms` keeps the database, the extension and the migrations.
--
-- Run it through scripts/node27_provision_write_roles.sh; that runner supplies
-- the phase variables below, the retry passes and the before/after captures.
-- Direct invocation is supported too (all phases, strict audit):
--
--   docker exec -i nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 \
--     < db/roles/node27_write_roles.sql
--
-- Idempotent: safe (and required) to re-run after every migration.
--
-- Phase variables (all boolean, all default `on`):
--   do_roles      roles, flags, passwords, schema USAGE, DML grants, sequence
--                 USAGE, default privileges, cold-tablespace CREATE grant and
--                 the negative COPY ... FROM PROGRAM probes.  Purely additive:
--                 nothing here transfers ownership.
--   do_ownership  the per-relation ownership transfer loop.
--   do_audit      the trailing audit queries.
--   strict_audit  when on, owner drift raises (psql exits non-zero).
--
-- Secrets: passwords are never in this file.  They are read from the
-- environment of the psql process (NODE27_INGEST_RW_PASSWORD /
-- NODE27_DOWNLOAD_RW_PASSWORD) via \getenv and are skipped when unset.

\set VERBOSITY terse
\set SHOW_CONTEXT never

\if :{?do_roles}
\else
\set do_roles on
\endif
\if :{?do_ownership}
\else
\set do_ownership on
\endif
\if :{?do_audit}
\else
\set do_audit on
\endif
\if :{?strict_audit}
\else
\set strict_audit on
\endif


\if :do_roles

\echo '## phase: roles (additive -- no ownership transfer, no relation lock)'

-- Role shape mirrors scripts/local_pg.sh:151, plus NOBYPASSRLS.  ALTER on the
-- else branch is what makes a re-run converge a hand-edited role back to the
-- committed flags instead of skipping it.
DO $roles$
DECLARE
  v_role text;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['nhms_ingest_rw', 'nhms_download_rw'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
      EXECUTE format(
        'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
        v_role);
      RAISE NOTICE 'created role %', v_role;
    ELSE
      EXECUTE format(
        'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
        v_role);
      RAISE NOTICE 'role % already existed; flags reasserted', v_role;
    END IF;
  END LOOP;
END
$roles$;

-- Passwords: present only in the psql process environment.  An unset variable
-- leaves the existing password untouched, which is what makes a re-run after a
-- migration safe for an operator who does not hold the credentials.
--
-- `ALTER ROLE ... PASSWORD` is logged VERBATIM by the server whenever
-- log_statement is 'ddl'/'mod'/'all' or log_min_duration_statement is low
-- enough, so the cleartext would land in the container log even though it never
-- touches the repo or an argv.  Both GUCs are SUSET and the provisioning
-- session is the superuser, so suppress logging around the two ALTERs and
-- restore afterwards.  Residual: log_min_error_statement still logs the
-- statement if the ALTER itself FAILS -- treat a failed password set as a
-- credential to rotate.
SET log_statement = 'none';
SET log_min_duration_statement = -1;
\getenv ingest_rw_password NODE27_INGEST_RW_PASSWORD
\if :{?ingest_rw_password}
ALTER ROLE nhms_ingest_rw PASSWORD :'ingest_rw_password';
\echo '   nhms_ingest_rw password set from NODE27_INGEST_RW_PASSWORD'
\else
\echo '   NODE27_INGEST_RW_PASSWORD unset -- nhms_ingest_rw password left unchanged'
\endif
\getenv download_rw_password NODE27_DOWNLOAD_RW_PASSWORD
\if :{?download_rw_password}
ALTER ROLE nhms_download_rw PASSWORD :'download_rw_password';
\echo '   nhms_download_rw password set from NODE27_DOWNLOAD_RW_PASSWORD'
\else
\echo '   NODE27_DOWNLOAD_RW_PASSWORD unset -- nhms_download_rw password left unchanged'
\endif
RESET log_statement;
RESET log_min_duration_statement;

\echo '## grants: nhms_ingest_rw over the six application schemas'
-- Every schema-scoped statement is generated per EXISTING schema instead of
-- being spelled as one `IN SCHEMA core, hydro, ...` list: `flood` is
-- provisioned outside db/ and is absent in a fresh container, and a list form
-- would abort the whole run on the missing name.
SELECT format('GRANT USAGE ON SCHEMA %I TO nhms_ingest_rw', n.nspname)
FROM pg_namespace n
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
ORDER BY n.nspname
\gexec

SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO nhms_ingest_rw', n.nspname)
FROM pg_namespace n
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
ORDER BY n.nspname
\gexec

SELECT format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO nhms_ingest_rw', n.nspname)
FROM pg_namespace n
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
ORDER BY n.nspname
\gexec

-- Default privileges: a table a later migration creates as `nhms` is owned by
-- `nhms` until the next provision run, but stays readable/writable by ingest
-- during that drift window.  Only its stats-guard ANALYZE entry degrades to
-- `warning` (ANALYZE needs ownership; PG 15 has no MAINTAIN).
\echo '## default privileges: ALTER DEFAULT PRIVILEGES FOR ROLE nhms -> nhms_ingest_rw'
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nhms_ingest_rw', n.nspname)
FROM pg_namespace n
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
ORDER BY n.nspname
\gexec

SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA %I GRANT USAGE ON SEQUENCES TO nhms_ingest_rw', n.nspname)
FROM pg_namespace n
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
ORDER BY n.nspname
\gexec

-- The cold-residency lane issues `ALTER TABLE/INDEX ... SET TABLESPACE
-- nhms_cold` on compressed chunks; moving a relation into a tablespace needs
-- CREATE on it.  Conditional: the tablespace is a one-time superuser install
-- (#1894) and does not exist in a disposable container.
SELECT format('GRANT CREATE ON TABLESPACE %I TO nhms_ingest_rw', t.spcname)
FROM pg_tablespace t
WHERE t.spcname = 'nhms_cold'
\gexec

\echo '## grants: nhms_download_rw over met only'
-- The download lane opens no database connection today (measured, design D3).
-- The role exists so the template's promise holds and a future adapter write
-- lands here instead of on the superuser.
GRANT USAGE ON SCHEMA met TO nhms_download_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA met TO nhms_download_rw;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA met TO nhms_download_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA met GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nhms_download_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE nhms IN SCHEMA met GRANT USAGE ON SEQUENCES TO nhms_download_rw;

\echo '## negative probe: COPY ... FROM PROGRAM must be refused for both write roles'
-- The whole point of the change: a leaked write credential must not be command
-- execution inside the database container.  The probe runs under SET LOCAL ROLE
-- (COPY checks GetUserId(), so the surrounding superuser session does not mask
-- the refusal) and touches no application relation.
DO $copy_probe$
DECLARE
  v_role  text;
  v_state text;
  v_msg   text;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['nhms_ingest_rw', 'nhms_download_rw'] LOOP
    EXECUTE format('SET LOCAL ROLE %I', v_role);
    -- Created OUTSIDE the guarded block on purpose: a refusal to create the
    -- temp table is also SQLSTATE 42501 and would read as a passing probe.
    EXECUTE 'CREATE TEMP TABLE node27_copy_program_probe (x text)';
    BEGIN
      EXECUTE 'COPY node27_copy_program_probe FROM PROGRAM ''echo probe''';
      RAISE EXCEPTION 'SECURITY REGRESSION: role % executed COPY ... FROM PROGRAM', v_role;
    EXCEPTION
      WHEN insufficient_privilege THEN
        GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_msg = MESSAGE_TEXT;
        IF v_msg NOT LIKE '%external program%' THEN
          RAISE EXCEPTION 'unexpected % refusal for role %: %', v_state, v_role, v_msg;
        END IF;
        RAISE NOTICE 'copy-from-program refused for %: %', v_role, v_msg;
    END;
    EXECUTE 'DROP TABLE node27_copy_program_probe';
    EXECUTE 'RESET ROLE';
  END LOOP;
END
$copy_probe$;

\endif


\if :do_ownership

\echo '## phase: ownership transfer of the application schemas to nhms_ingest_rw'
-- Executed by \gexec, which runs each generated statement as its own
-- autocommitted statement.  Deliberately NOT one DO block and NOT one
-- transaction: `ALTER ... OWNER TO` takes AccessExclusiveLock down the chunk
-- tree, and the display API (nhms_display_ro, public site, cannot be stopped)
-- holds AccessShareLock on served relations.  One statement per transaction
-- means a display query stalls for at most one relation's lock_timeout, and a
-- completed transfer is never rolled back by a later failure.
--
-- ON_ERROR_STOP is off for the loop so a lock_timeout on one relation does not
-- abandon the rest of the pass; the runner re-runs this phase (up to 5 passes)
-- and each pass re-selects only what is still owned by somebody else.
-- Exhaustion leaves a partial, audit-visible transfer -- never a rollback.
\unset ON_ERROR_STOP
SET lock_timeout = '5s';

-- 1/4 ordinary and partitioned tables.  These go FIRST: a sequence owned by a
-- table column follows its table automatically, and a standalone
-- `ALTER SEQUENCE ... OWNER TO` on such a sequence is refused.
SELECT format('ALTER TABLE %I.%I OWNER TO nhms_ingest_rw', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind IN ('r', 'p')
  AND c.relowner <> 'nhms_ingest_rw'::regrole
ORDER BY n.nspname, c.relname
\gexec

-- 2/4 sequences.  Re-selected here, after the table pass committed, so the
-- OWNED BY sequences have already followed their tables and drop out.
SELECT format('ALTER SEQUENCE %I.%I OWNER TO nhms_ingest_rw', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind = 'S'
  AND c.relowner <> 'nhms_ingest_rw'::regrole
ORDER BY n.nspname, c.relname
\gexec

-- 3/4 views and 4/4 materialized views: included so a future view cannot make
-- the audit red with no loop branch to fix it (REFRESH MATERIALIZED VIEW also
-- requires ownership).
SELECT format('ALTER VIEW %I.%I OWNER TO nhms_ingest_rw', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind = 'v'
  AND c.relowner <> 'nhms_ingest_rw'::regrole
ORDER BY n.nspname, c.relname
\gexec

SELECT format('ALTER MATERIALIZED VIEW %I.%I OWNER TO nhms_ingest_rw', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind = 'm'
  AND c.relowner <> 'nhms_ingest_rw'::regrole
ORDER BY n.nspname, c.relname
\gexec

RESET lock_timeout;
\set ON_ERROR_STOP 1

\endif


\if :do_audit

\echo '## audit: write-role flags (all five privilege flags must be false)'
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin
FROM pg_roles
WHERE rolname IN ('nhms', 'nhms_display_ro', 'nhms_ingest_rw', 'nhms_download_rw')
ORDER BY rolname;

\echo '## audit: relation ownership summary for the application schemas'
SELECT n.nspname AS schema,
       c.relkind,
       c.relowner::regrole::text AS owner,
       count(*) AS relations
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo '## audit: owner drift -- relations NOT owned by nhms_ingest_rw'
SELECT n.nspname || '.' || c.relname AS relation,
       c.relkind,
       c.relowner::regrole::text AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
  AND c.relowner <> 'nhms_ingest_rw'::regrole
ORDER BY 1;

\echo '## audit: compression-capable hypertable owners'
-- Generated so the file still runs on a database without TimescaleDB.
SELECT $tsdb$SELECT h.hypertable_schema, h.hypertable_name, h.compression_enabled,
       c.relowner::regrole::text AS owner
FROM timescaledb_information.hypertables h
JOIN pg_namespace n ON n.nspname = h.hypertable_schema
JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = h.hypertable_name
ORDER BY 1, 2$tsdb$
WHERE to_regclass('timescaledb_information.hypertables') IS NOT NULL
\gexec

\echo '## audit: nhms_display_ro effective SELECT set over the application schemas'
-- ALTER ... OWNER TO rewrites the grantor references inside relacl, so the
-- read-side boundary is asserted as an effective privilege set rather than
-- assumed from the ACL text.  The join makes the query empty (not an error)
-- when the display role is absent.
SELECT n.nspname || '.' || c.relname AS relation
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.rolname = 'nhms_display_ro'
WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND has_table_privilege(r.oid, c.oid, 'SELECT')
ORDER BY 1;

-- Always asserted, in both modes: the roles exist and carry none of the five
-- privilege flags.  This is the invariant `--roles-only` is allowed to prove.
DO $flags$
DECLARE
  v_bad     text;
  v_logins  int;
BEGIN
  SELECT string_agg(rolname, ', ' ORDER BY rolname) INTO v_bad
  FROM pg_roles
  WHERE rolname IN ('nhms_ingest_rw', 'nhms_download_rw')
    AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls);
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION 'privilege regression: over-privileged write role(s): %', v_bad;
  END IF;

  SELECT count(*) INTO v_logins
  FROM pg_roles
  WHERE rolname IN ('nhms_ingest_rw', 'nhms_download_rw') AND rolcanlogin;
  IF v_logins <> 2 THEN
    RAISE EXCEPTION 'expected 2 write roles able to log in, found %', v_logins;
  END IF;
END
$flags$;

\if :strict_audit
-- Full mode only: owner drift is a hard failure, so the cutover cannot proceed
-- on a partial transfer.
DO $strict$
DECLARE
  v_drift int;
BEGIN
  SELECT count(*) INTO v_drift
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
    AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
    AND c.relowner <> 'nhms_ingest_rw'::regrole;
  IF v_drift > 0 THEN
    RAISE EXCEPTION 'owner drift: % application relation(s) not owned by nhms_ingest_rw (listed above); re-run the provision script', v_drift;
  END IF;
END
$strict$;
\echo '## audit: OK -- no owner drift'
\else
\echo '## audit: non-strict (--roles-only): owner drift above is expected, ownership is transferred post-merge'
\endif

\endif
