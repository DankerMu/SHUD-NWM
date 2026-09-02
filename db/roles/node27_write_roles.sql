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
-- the phase variables below, the retry passes, the before/after captures and
-- the exit-code contract.  Direct invocation is AUDIT-ONLY -- use it to read the
-- current state, never to perform a cutover:
--
--   docker exec -i nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 \
--     -v do_roles=off -v do_ownership=off < db/roles/node27_write_roles.sql
--
-- Running it directly with the default phases DOES execute the ownership
-- transfer, but WITHOUT the runner's display before/after gate: only the runner
-- captures nhms_display_ro's effective SELECT set on both sides and exits 4 when
-- it changed.  This file cannot detect that regression on its own.
--
-- Idempotent: safe (and required) to re-run after every migration.
--
-- Phase variables (all boolean, all default `on`):
--   do_roles      roles, flags, passwords, schema USAGE, DML grants, sequence
--                 USAGE, default privileges, cold-tablespace CREATE grant, the
--                 negative COPY ... FROM PROGRAM probes and the event trigger
--                 that refuses CREATE RULE / CREATE TRIGGER from the write
--                 roles.  Purely additive: nothing here transfers ownership.
--   do_ownership  the per-relation ownership transfer loop AND the one
--                 non-additive privilege statement in this file: the TEMP
--                 tightening (`REVOKE TEMPORARY ... FROM PUBLIC`, re-granted to
--                 nhms_display_ro).  Limit worth
--                 knowing before running it: a view/matview EXECUTES as its
--                 owner (PG 15 defaults to security_invoker = false), so a view
--                 whose body reads a relation OUTSIDE the six schemas can become
--                 unreadable for display after the transfer even though
--                 nhms_display_ro's SELECT privilege set is unchanged -- neither
--                 this file nor the runner's exit-4 gate can see that.  T7
--                 enumerates relkind v/m and their out-of-schema pg_depend
--                 targets before the transfer for exactly this reason.
--   do_audit      the trailing audit queries.  Scope of every relation-level
--                 leg: the six application schemas PLUS the
--                 `_timescaledb_internal` relations owned by a write role
--                 (chunks and `_compressed_hypertable_N` follow their parent
--                 through the transfer; TimescaleDB's own superuser-owned
--                 catalog tables are never scanned).  The legs, all printed in
--                 every mode:
--                   1. the five privilege flags are false and both roles log in;
--                   2. pg_auth_members holds NO row in either direction for the
--                      write roles -- neither a membership they hold (which
--                      hands back privileges the flags cannot show) nor a grant
--                      of a write role to somebody else (which hands that
--                      somebody SET ROLE into the write set);
--                   3. relation ownership summary + owner drift;
--                   4. nhms_display_ro's effective SELECT set;
--                   5. the nhms_cold CREATE grant;
--                   6. the rule/trigger inventory against the four-trigger
--                      migration allow-list (TimescaleDB's blocker excluded by
--                      FUNCTION IDENTITY, never by name);
--                   7. those four allow-listed triggers are PRESENT (count = 4)
--                      and ENABLED (tgenabled = 'O');
--                   8. the function-provenance sweep over every stored
--                      expression (column defaults incl. STORED generated
--                      columns, CHECK constraints, rule actions, trigger
--                      functions): a referenced function is untrusted when it
--                      lives in a temp schema, is owned by a non-superuser, is
--                      not executable by nhms_ingest_rw, or is deny-listed;
--                   9. the event trigger is present, enabled and
--                      superuser-owned;
--                  10. TEMP on the database for each role (verdict is
--                      strict-only -- see the block itself).
--   strict_audit  when on, every leg above raises (psql exits non-zero) instead
--                 of warning, and the TEMP verdict is checked at all.
--
-- Secrets: passwords are never in this file.  They are read from the
-- environment of the psql process (NODE27_INGEST_RW_PASSWORD /
-- NODE27_DOWNLOAD_RW_PASSWORD) via \getenv and are skipped when unset.

-- Self-sufficient under a direct invocation too: the runner always passes
-- -v ON_ERROR_STOP=1, so this changes nothing there, but a hand-run audit that
-- hits an error must stop instead of reporting the remaining legs as if the
-- failed one had passed.  The ownership loop below turns it OFF deliberately
-- (one relation's lock_timeout must not abandon the pass) and back on after.
\set ON_ERROR_STOP on
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
-- Receipt attribution: the runner passes -v phase=... on every invocation and
-- -v pass=N on the ownership passes.  Defaulted here so a direct invocation
-- still prints an attributable audit header instead of a literal `:phase`.
\if :{?phase}
\else
\set phase direct
\endif
\if :{?pass}
\else
\set pass n/a
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
-- leaves an EXISTING role's password untouched, which is what makes a re-run
-- after a migration safe for an operator who does not hold the credentials.  A
-- role this run just CREATED has no password at all in that case: it exists,
-- owns relations and can be SET ROLE'd into, but cannot log in over TCP until a
-- later run sets one.
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
--
-- EMPTY is not the same as unset, and psql cannot tell them apart on its own:
-- `\getenv` sets the variable whenever the environment variable EXISTS, so
-- `NODE27_INGEST_RW_PASSWORD=` would make `\if :{?ingest_rw_password}` true and
-- set an EMPTY password -- silently disabling password login for a role that
-- had a working one.  The runner already refuses to forward an empty value
-- (`${!var:+x}`, scripts/node27_provision_write_roles.sh); this second gate is
-- what makes a DIRECT invocation safe as well.  The emptiness test is a
-- server-side \gset because psql's own \if evaluates a literal, not an
-- expression; that statement carries the cleartext exactly like the ALTER it
-- guards, and both run inside the log-suppression window above.
\getenv ingest_rw_password NODE27_INGEST_RW_PASSWORD
\if :{?ingest_rw_password}
SELECT :'ingest_rw_password' <> '' AS ingest_rw_password_present \gset
\else
\set ingest_rw_password_present f
\endif
\if :ingest_rw_password_present
ALTER ROLE nhms_ingest_rw PASSWORD :'ingest_rw_password';
\echo '   nhms_ingest_rw password set from NODE27_INGEST_RW_PASSWORD'
\else
\echo '   NODE27_INGEST_RW_PASSWORD unset or empty -- nhms_ingest_rw keeps its existing password; if this run CREATED the role it has none and cannot log in over TCP until one is set'
\endif
\getenv download_rw_password NODE27_DOWNLOAD_RW_PASSWORD
\if :{?download_rw_password}
SELECT :'download_rw_password' <> '' AS download_rw_password_present \gset
\else
\set download_rw_password_present f
\endif
\if :download_rw_password_present
ALTER ROLE nhms_download_rw PASSWORD :'download_rw_password';
\echo '   nhms_download_rw password set from NODE27_DOWNLOAD_RW_PASSWORD'
\else
\echo '   NODE27_DOWNLOAD_RW_PASSWORD unset or empty -- nhms_download_rw keeps its existing password; if this run CREATED the role it has none and cannot log in over TCP until one is set'
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
-- during that drift window.  Its stats-guard ANALYZE entry degrades to
-- `warning` (ANALYZE needs ownership; PG 15 has no MAINTAIN).
--
-- A migration that creates a HYPERTABLE is worse than a warning: compress_chunk
-- and drop_chunks refuse outright with `must be owner of hypertable`, so the
-- compression and retention lanes FAIL on it, they do not degrade.  Nil today --
-- both lanes hard-filter to the two existing hypertables -- but that is a
-- property of those filters, not of this grant.  Re-run this file after every
-- migration (runbook 9.6).
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
-- A leaked write credential must not be DIRECT command execution inside the
-- database container.  That is what this probe pins, and it is all it pins:
-- ownership still carries CREATE RULE / CREATE TRIGGER, and a rule action or
-- trigger body executes as whichever role next WRITES the relation -- including
-- the migration/seed/replay lanes, which stay on the superuser `nhms` (design
-- D2).  That indirect path reaches pg_read_file, lo_export and
-- `COPY ... TO PROGRAM`, so it is closed separately: the event trigger below
-- refuses that DDL family for the write roles everywhere TimescaleDB lets it
-- see the command (prevention; it does not see `CREATE TRIGGER` on a
-- hypertable -- see the guard block), and the audit
-- enumerates every rule and non-internal trigger against an explicit
-- allow-list, checks that each allow-listed trigger is still enabled, and
-- sweeps every stored expression for functions the write role cannot itself
-- execute -- the `ALTER TABLE ... SET DEFAULT` form, which no event trigger can
-- refuse without breaking cold residency (detection).  The
-- probe runs under SET LOCAL ROLE (COPY checks GetUserId(), so the surrounding
-- superuser session does not mask the refusal) and touches no application
-- relation.
DO $copy_probe$
DECLARE
  v_role  text;
  v_state text;
  v_msg   text;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['nhms_ingest_rw', 'nhms_download_rw'] LOOP
    -- Created as the superuser, BEFORE `SET LOCAL ROLE`, and outside the
    -- guarded block: a refusal to create the temp table is also SQLSTATE 42501
    -- and would read as a passing probe.  After the full-mode TEMP tightening
    -- the write roles cannot create a temp table at all, so creating it under
    -- the role would turn every later additive re-run into a hard failure
    -- (measured).  INSERT is granted so the only privilege the COPY can trip
    -- over is the program one.
    EXECUTE 'CREATE TEMP TABLE node27_copy_program_probe (x text)';
    EXECUTE format('GRANT INSERT ON node27_copy_program_probe TO %I', v_role);
    EXECUTE format('SET LOCAL ROLE %I', v_role);
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
    -- RESET first: after the tightening the write role holds no rights on the
    -- session temp schema, so the DROP has to run as the superuser that owns it.
    EXECUTE 'RESET ROLE';
    EXECUTE 'DROP TABLE node27_copy_program_probe';
  END LOOP;
END
$copy_probe$;

\echo '## guard: event trigger refusing CREATE RULE / CREATE TRIGGER from the write roles'
-- Relation ownership carries CREATE RULE and CREATE TRIGGER.  A rule action and
-- a trigger body execute as the role that WRITES the relation, not as the role
-- that created them, so an owner-planted object turns the next migration, seed
-- or replay write -- all of which stay on the superuser `nhms` -- into arbitrary
-- superuser SQL (pg_read_file, lo_export into PGDATA, COPY ... TO PROGRAM).
--
-- Prevention lives here, in the ADDITIVE phase, so `--roles-only` installs it
-- BEFORE the post-merge ownership transfer hands the write roles that ability.
-- Detection (the allow-list inventory) lives in the audit below; neither
-- replaces the other.
--
-- Why this holds: the event trigger and its function are owned by the
-- provisioning superuser and live in a schema the write roles have no CREATE on,
-- the write roles hold no role membership (audited below), so `SET ROLE` cannot
-- reach a session_user the guard ignores, and a non-superuser cannot drop,
-- disable or replace an event trigger.
--
-- Where it does NOT hold, measured on 2.10.2 (transcript §15): TimescaleDB's
-- process-utility hook handles `CREATE TRIGGER` on a HYPERTABLE itself and
-- never fires `ddl_command_start`, so the write roles ARE refused on ordinary
-- tables and on chunks but NOT on a hypertable -- including the internal
-- `_compressed_hypertable_N` that the ownership transfer hands them.  That gap
-- is covered by detection only: the audit's rule/trigger inventory follows
-- ownership into `_timescaledb_internal`.
--
-- Tag list: `CREATE OR REPLACE RULE` carries the `CREATE RULE` tag, and the
-- ALTER/DROP tags stop the write roles renaming or removing the four `met`
-- immutability triggers from migration 000043.  `ALTER TABLE` is deliberately
-- NOT in the list: the cold-residency lane needs `ALTER TABLE ... SET
-- TABLESPACE`.  The two `ALTER TABLE` forms of the same gadget -- a planted
-- column DEFAULT / CHECK expression, and `... DISABLE TRIGGER` on an
-- allow-listed guard -- are therefore closed by detection instead: the audit's
-- function-privilege sweep and its `tgenabled` check.
CREATE SCHEMA IF NOT EXISTS nhms_guard;

CREATE OR REPLACE FUNCTION nhms_guard.refuse_write_role_rules_and_triggers()
RETURNS event_trigger
LANGUAGE plpgsql
AS $guard$
BEGIN
  IF session_user IN ('nhms_ingest_rw', 'nhms_download_rw') THEN
    RAISE EXCEPTION 'refused: role % may not run % -- a rule action or trigger body executes as the role that next writes the relation, including the migration superuser',
      session_user, tg_tag
      USING ERRCODE = 'insufficient_privilege',
            HINT = 'run migration-class DDL as the migration role; see OpenSpec change node27-write-path-roles design D2';
  END IF;
END
$guard$;

DROP EVENT TRIGGER IF EXISTS nhms_guard_no_write_role_rules_triggers;
CREATE EVENT TRIGGER nhms_guard_no_write_role_rules_triggers
  ON ddl_command_start
  WHEN TAG IN ('CREATE RULE', 'ALTER RULE', 'DROP RULE',
               'CREATE TRIGGER', 'ALTER TRIGGER', 'DROP TRIGGER')
  EXECUTE FUNCTION nhms_guard.refuse_write_role_rules_and_triggers();

\endif


\if :do_ownership

\echo '## phase: ownership transfer of the application schemas to nhms_ingest_rw (pass' :pass 'of the runner retry loop)'
-- Executed by \gexec, which runs each generated statement as its own
-- autocommitted statement.  Deliberately NOT one DO block and NOT one
-- transaction: `ALTER ... OWNER TO` takes AccessExclusiveLock down the chunk
-- tree, and the display API (nhms_display_ro, public site, cannot be stopped)
-- holds AccessShareLock on served relations.  One statement per transaction
-- means the lock is held for one STATEMENT -- for a hypertable, the whole chunk
-- tree in that one statement -- bounded by that ALTER's own lock_timeout below,
-- and released at its end instead of at the end of the loop.  A display query
-- arriving mid-ALTER waits for that statement (up to its lock_timeout), not for
-- the rest of the pass, and a completed transfer is never rolled back by a
-- later failure.
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

\echo '## privilege tightening: TEMP on the database (the one NON-additive statement in this file)'
-- Why this is here and not in do_roles.  `pg_temp` is the ONLY schema in which
-- the write roles can create a FUNCTION: they hold USAGE, never CREATE, on the
-- six application schemas and on nhms_guard.  A function they author is a
-- function whose body they choose, and the audit's provenance sweep exists
-- because such a function is evaluated with the authority of whoever writes the
-- row.  Removing TEMP removes the authoring surface itself -- prevention, not
-- detection -- but it TAKES a privilege away, so it belongs in the post-merge
-- full-mode phase that already takes a lock and never in `--roles-only`, which
-- must stay purely additive and reversible.
--
-- It has to go through PUBLIC: TEMP reaches the write roles as PUBLIC's default
-- database privilege (`datacl` NULL, i.e. `=Tc/nhms`), so a per-role REVOKE is a
-- no-op (measured).  Revoking from PUBLIC and re-granting to nhms_display_ro
-- leaves display with exactly the privilege it has today; the superuser is
-- unaffected (superusers bypass ACLs); CONNECT for PUBLIC is untouched.
--
-- Measured precondition: no write lane creates temp objects.  The only temp
-- user in the repo is scripts/backfill_hydro_run_parsed_at.py, an operator
-- script that runs as the migration role.
SELECT current_database() AS provision_dbname \gset
REVOKE TEMPORARY ON DATABASE :"provision_dbname" FROM PUBLIC;
-- Generated per EXISTING role: nhms_display_ro is created outside this file and
-- a missing role must not abort the cutover.
SELECT format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), r.rolname)
FROM pg_roles r
WHERE r.rolname = 'nhms_display_ro'
\gexec

\endif


\if :do_audit

\echo '## audit: invocation phase =' :phase
\echo '## audit: ownership pass  =' :pass
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

-- Always asserted, in both modes: the roles exist, carry none of the five
-- privilege flags, and hold NO role membership.  This is the invariant
-- `--roles-only` is allowed to prove.
--
-- The membership leg is not redundant with the flag leg: `pg_auth_members` is
-- invisible to every rolsuper/rolcreaterole/... column, so a single
-- `GRANT pg_write_server_files TO nhms_ingest_rw` leaves all five flags false
-- while restoring server-side file reads via COPY, and a membership in the
-- migration role restores everything it can do.  The two write roles need no
-- membership of any kind -- their whole privilege set is relation ownership plus
-- the explicit grants above -- so ANY row here is a regression, not a policy
-- judgement.  If a membership ever becomes genuinely necessary, add it to an
-- explicit allow-list here with the reason, do not widen the predicate.
--
-- BOTH directions are checked.  The member direction catches
-- `GRANT <anything> TO nhms_ingest_rw`; the grantee direction catches
-- `GRANT nhms_ingest_rw TO nhms_display_ro`, which leaves every flag false and
-- every membership-of-the-writer row empty while letting the read-only display
-- credential `SET ROLE nhms_ingest_rw` into the full write and ownership set.
DO $flags$
DECLARE
  v_bad      text;
  v_logins   int;
  v_member   text;
  v_grantee  text;
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

  FOR v_member, v_grantee IN
    SELECT m.rolname, g.rolname
    FROM pg_auth_members am
    JOIN pg_roles m ON m.oid = am.member
    JOIN pg_roles g ON g.oid = am.roleid
    WHERE m.rolname IN ('nhms_ingest_rw', 'nhms_download_rw')
       OR g.rolname IN ('nhms_ingest_rw', 'nhms_download_rw')
    ORDER BY 1, 2
  LOOP
    IF v_member IN ('nhms_ingest_rw', 'nhms_download_rw') THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: role % is a member of % -- the write roles must hold no role membership; revoke it before proceeding', v_member, v_grantee;
    ELSE
      RAISE EXCEPTION 'SECURITY REGRESSION: role % has been granted to % -- the write roles must not be reachable by SET ROLE from any other role; revoke it before proceeding', v_grantee, v_member;
    END IF;
  END LOOP;
END
$flags$;

\echo '## audit: rules and non-internal triggers in the application schemas'
-- Detection half of the owner-planted escalation path (the event trigger in
-- do_roles is the prevention half).  Printed in EVERY mode, before the severity
-- block below, because the pre-merge `--roles-only` run is the first time the
-- real production inventory is visible: an unexpected trigger has to surface
-- there, not in the post-merge strict run.
--
-- Excluded, by construction and not by allow-list: `_RETURN` (every view has
-- one) and TimescaleDB's `ts_insert_blocker` (recreated on every hypertable and
-- chunk).  Foreign-key and constraint triggers are `tgisinternal` and never
-- appear.
--
-- The blocker is excluded by FUNCTION IDENTITY, never by name.  A name-only
-- exclusion is a hole with the shape of the guard: on a hypertable -- the one
-- relation class where TimescaleDB routes `CREATE TRIGGER` around the event
-- trigger -- the write role can run `CREATE OR REPLACE TRIGGER ts_insert_blocker
-- ... EXECUTE FUNCTION <anything>` and inherit the exclusion.  `tgfoid` must be
-- TimescaleDB's own `_timescaledb_internal.insert_blocker()`; anything else
-- wearing that name is a planted trigger and stays in the inventory.
-- `to_regprocedure` returns NULL (it does not raise) on a database without the
-- extension, and `IS DISTINCT FROM NULL` is true, so such a trigger is listed
-- there too.
--
-- Scope, and why it is not just the six application schemas (all measured on a
-- 2.10.2 container, transcript §15).  TimescaleDB's process-utility hook takes
-- `CREATE TRIGGER` on a HYPERTABLE down its own path, which never fires a
-- `ddl_command_start` event trigger -- so the guard installed in do_roles does
-- refuse the write roles on ordinary tables and on chunks, but NOT on a
-- hypertable, including the internal `_compressed_hypertable_N` relation that
-- the transfer hands to nhms_ingest_rw with its parent.  A trigger planted
-- there propagates to every compressed chunk, and a superuser `COPY ... FROM`
-- into such a chunk -- which is exactly what the replay `pg_restore` lane does
-- -- executes its body as the superuser.  Chunks themselves refuse
-- `ALTER TABLE ... SET DEFAULT` and `ADD CONSTRAINT` ("operation not supported
-- on chunk tables"), but `_compressed_hypertable_N` accepts `SET DEFAULT`.
-- So the audit follows OWNERSHIP into `_timescaledb_internal`: relations owned
-- by a write role are in scope, TimescaleDB's own catalog tables (owned by the
-- superuser) are not -- which keeps the extension's internals out of the scan
-- while covering everything the transfer made plantable.
SELECT n.nspname || '.' || c.relname AS relation, 'rule' AS kind, r.rulename AS name
FROM pg_rewrite r
JOIN pg_class c ON c.oid = r.ev_class
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
       OR (n.nspname = '_timescaledb_internal'
           AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
  AND r.rulename <> '_RETURN'
UNION ALL
SELECT n.nspname || '.' || c.relname, 'trigger', t.tgname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
       OR (n.nspname = '_timescaledb_internal'
           AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
  AND NOT t.tgisinternal
  AND (t.tgname <> 'ts_insert_blocker'
       OR t.tgfoid IS DISTINCT FROM to_regprocedure('_timescaledb_internal.insert_blocker()'))
ORDER BY 1, 2, 3;

\echo '## audit: function-provenance sweep over stored expressions'
-- The ALTER TABLE form of the same escalation, which the event trigger cannot
-- cover (the cold-residency lane needs ALTER TABLE for SET TABLESPACE): a
-- column DEFAULT or CHECK expression planted by the relation owner is evaluated
-- by whichever role next writes the row -- including the migration superuser --
-- so `DEFAULT length(pg_read_file('/etc/hostname'))` reads server files the
-- owner may not read itself.
--
-- The discriminator is PROVENANCE, not executability.  "Can the write role
-- EXECUTE it" was the first cut and it is a proxy that fails in both obvious
-- directions (both measured, transcript §16): a function the write role
-- AUTHORED in `pg_temp` passes it (the role owns the function, so of course it
-- may execute it) and so does a PUBLIC-executable pg_catalog function whose
-- effect is superuser-only in context (`set_config('session_replication_role',
-- 'replica', false)` disables every trigger for the writing session).  A
-- referenced function is therefore untrusted when ANY of these holds:
--
--   (i)   it lives in a temp schema (`pg_temp_%` / `pg_toast_temp_%`) -- the
--         only place the write roles can author a function at all, which the
--         full-mode TEMP tightening also removes;
--   (ii)  its owner is not a superuser -- provenance proper: a superuser writer
--         must not evaluate a body some other role controls;
--   (iii) nhms_ingest_rw cannot EXECUTE it (kept: pg_read_file, lo_export,
--         pg_ls_dir -- a function the owner may not call directly but can
--         smuggle into an expression evaluated as superuser);
--   (iv)  it is on the deny-list below: PUBLIC-executable pg_catalog functions
--         whose effect for a superuser writer is not what the migration asked
--         for.  RESIDUAL, stated plainly: this leg is a LIST and is therefore
--         incomplete by construction -- it is the only non-structural leg here.
--         Legs (i) and (ii) are the structural ones and they are what the
--         design leans on.
--
-- Everything the migrations actually use -- now(), nextval(),
-- gen_random_uuid(), length(), the four met trigger functions -- is owned by
-- the migration superuser and PUBLIC-executable, so a clean catalog sweeps
-- clean.
--
-- Mechanism note (measured, not assumed): walking `pg_depend` does NOT work
-- here.  Dependencies on PINNED objects -- every function created by initdb,
-- which is exactly where pg_read_file lives -- are never recorded, so
-- pg_attrdef has pg_depend rows to pg_class only and none to pg_proc.  The
-- stored parse trees are therefore scanned directly for their `:funcid` /
-- `:opfuncid` tokens (`:opfuncid` catches an operator-wrapped call).
-- Free coverage worth stating: STORED generated columns also live in
-- pg_attrdef and are swept.  Out of this class, deliberately: expression
-- indexes (pg_read_file is VOLATILE, CREATE INDEX requires IMMUTABLE), RLS
-- policies (owner and superuser bypass RLS), and view `_RETURN` rules (a view
-- body is evaluated for the reader, not for the role writing the row) -- the
-- latter are excluded by the same predicate as the inventory above.
-- A temp table, not a CTE repeated three times: the inventory below, the
-- severity block and the T7 receipt must all read the same scan.  It is dropped
-- at the end of the block; an ON_ERROR_STOP abort ends the session anyway.
CREATE TEMP TABLE pg_temp.nhms_audit_function_refs AS
WITH sources AS (
  SELECT n.nspname || '.' || c.relname AS relation,
         'column default ' || a.attname AS kind,
         d.adbin::text AS tree,
         NULL::oid AS fnoid
  FROM pg_attrdef d
  JOIN pg_class c ON c.oid = d.adrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
  WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
         OR (n.nspname = '_timescaledb_internal'
             AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
  UNION ALL
  SELECT n.nspname || '.' || c.relname,
         'CHECK constraint ' || k.conname,
         k.conbin::text,
         NULL::oid
  FROM pg_constraint k
  JOIN pg_class c ON c.oid = k.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
         OR (n.nspname = '_timescaledb_internal'
             AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
    AND k.contype = 'c'
    AND k.conbin IS NOT NULL
  UNION ALL
  SELECT n.nspname || '.' || c.relname,
         'rule ' || r.rulename,
         r.ev_action::text,
         NULL::oid
  FROM pg_rewrite r
  JOIN pg_class c ON c.oid = r.ev_class
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
         OR (n.nspname = '_timescaledb_internal'
             AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
    AND r.rulename <> '_RETURN'
  UNION ALL
  -- A trigger body is opaque (it is compiled at call time), so the function
  -- itself is the unit of authority: tgfoid, taken straight from pg_trigger.
  SELECT n.nspname || '.' || c.relname,
         'trigger ' || t.tgname,
         NULL::text,
         t.tgfoid
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
         OR (n.nspname = '_timescaledb_internal'
             AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
    AND NOT t.tgisinternal
    AND (t.tgname <> 'ts_insert_blocker'
         OR t.tgfoid IS DISTINCT FROM to_regprocedure('_timescaledb_internal.insert_blocker()'))
), refs AS (
  SELECT s.relation, s.kind, (m[1])::oid AS fnoid
  FROM sources s, regexp_matches(s.tree, ':(?:op)?funcid (\d+)', 'g') m
  WHERE s.tree IS NOT NULL
  UNION ALL
  SELECT s.relation, s.kind, s.fnoid
  FROM sources s
  WHERE s.fnoid IS NOT NULL
)
SELECT r.relation,
       r.kind,
       pn.nspname || '.' || p.proname AS fn,
       -- All tripped legs are named, not just the first: a pg_temp function
       -- owned by the write role trips (i) and (ii), and the receipt should say
       -- so.  NULL here means "trusted for a superuser writer".
       nullif(concat_ws(', ',
         CASE WHEN pn.nspname LIKE 'pg\_temp\_%' OR pn.nspname LIKE 'pg\_toast\_temp\_%'
              THEN 'temp-schema function' END,
         CASE WHEN NOT po.rolsuper
              THEN 'owner ' || po.rolname || ' is not a superuser' END,
         CASE WHEN NOT has_function_privilege('nhms_ingest_rw', p.oid, 'EXECUTE')
              THEN 'NOT executable by nhms_ingest_rw' END,
         CASE WHEN pn.nspname = 'pg_catalog' AND p.proname = ANY (ARRAY[
                'set_config',
                'pg_terminate_backend', 'pg_cancel_backend', 'pg_reload_conf',
                'pg_notify',
                'pg_sleep', 'pg_sleep_for', 'pg_sleep_until',
                'pg_advisory_lock', 'pg_advisory_lock_shared',
                'pg_advisory_xact_lock', 'pg_advisory_xact_lock_shared',
                'pg_try_advisory_lock', 'pg_try_advisory_lock_shared',
                'pg_try_advisory_xact_lock', 'pg_try_advisory_xact_lock_shared',
                'pg_advisory_unlock', 'pg_advisory_unlock_shared',
                'pg_advisory_unlock_all'])
              THEN 'deny-listed' END
       ), '') AS untrusted_reason,
       (SELECT count(*) FROM sources) AS sources_scanned
FROM refs r
JOIN pg_proc p ON p.oid = r.fnoid
JOIN pg_namespace pn ON pn.oid = p.pronamespace
JOIN pg_roles po ON po.oid = p.proowner;

-- Printed in every mode, and the summary line is printed even when the sweep is
-- clean, so the T7 receipt shows that it ran rather than showing nothing.
SELECT x.detail
FROM (
  SELECT 0 AS ord,
         format('%s expression(s)/trigger(s) scanned, %s distinct function(s) referenced, %s untrusted for a superuser writer',
                coalesce(max(sources_scanned), 0), count(DISTINCT fn),
                count(DISTINCT fn) FILTER (WHERE untrusted_reason IS NOT NULL)) AS detail
  FROM pg_temp.nhms_audit_function_refs
  UNION ALL
  SELECT 1,
         format('%s %s references %s -- %s', relation, kind, fn, untrusted_reason)
  FROM pg_temp.nhms_audit_function_refs
  WHERE untrusted_reason IS NOT NULL
  GROUP BY relation, kind, fn, untrusted_reason
) x
ORDER BY x.ord, x.detail;

\echo '## audit: TEMP on the database (pg_temp is the only schema a write role can author a function in)'
-- Printed in EVERY mode; the verdict is strict-only (see the block below).
-- Expected after the full-mode tightening: false for both write roles, true for
-- nhms (superuser, bypasses ACLs) and for nhms_display_ro (re-granted).
SELECT r.rolname,
       has_database_privilege(r.oid, current_database(), 'TEMP') AS has_temp
FROM pg_roles r
WHERE r.rolname IN ('nhms', 'nhms_display_ro', 'nhms_ingest_rw', 'nhms_download_rw')
ORDER BY 1;

-- Severity is the phase's, not the statement's: the strict audit (full mode)
-- refuses, `--roles-only` and the audit-only invocation warn.  psql does not
-- interpolate `:variables` inside a dollar-quoted body, so the phase flag is
-- handed to the block through a session GUC instead.
SET nhms_provision.strict_audit TO :'strict_audit';
DO $planted$
DECLARE
  -- psql's own \if accepts on/true/1/yes, so this must too: a run with
  -- -v strict_audit=true would otherwise RAISE on owner drift and only WARN
  -- here, splitting the severity of one audit.
  v_strict  boolean := lower(coalesce(current_setting('nhms_provision.strict_audit', true), 'off')) IN ('on', 'true', '1', 'yes');
  v_planted  text;
  v_event    text;
  v_smuggled text;
  v_disabled text;
  v_present  int;
  v_temp     text;
BEGIN
  -- The allow-list is spelled as (schema, table, trigger) triples, not by name
  -- alone: a trigger called `canonical_grid_cell_immutable_trg` planted on a
  -- DIFFERENT table is not the migration's trigger.
  SELECT string_agg(descr, '; ' ORDER BY descr) INTO v_planted
  FROM (
    SELECT n.nspname || '.' || c.relname || ' rule ' || r.rulename AS descr
    FROM pg_rewrite r
    JOIN pg_class c ON c.oid = r.ev_class
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
           OR (n.nspname = '_timescaledb_internal'
               AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
      AND r.rulename <> '_RETURN'
    UNION ALL
    SELECT n.nspname || '.' || c.relname || ' trigger ' || t.tgname
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname = ANY (ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood'])
           OR (n.nspname = '_timescaledb_internal'
               AND c.relowner IN ('nhms_ingest_rw'::regrole, 'nhms_download_rw'::regrole)))
      AND NOT t.tgisinternal
      AND (t.tgname <> 'ts_insert_blocker'
           OR t.tgfoid IS DISTINCT FROM to_regprocedure('_timescaledb_internal.insert_blocker()'))
      AND (n.nspname, c.relname, t.tgname) NOT IN (
        ('met', 'canonical_met_product', 'canonical_met_product_grid_definition_uri_match_trg'),
        ('met', 'canonical_grid_snapshot', 'canonical_grid_snapshot_identity_immutable_trg'),
        ('met', 'canonical_grid_cell', 'canonical_grid_cell_immutable_trg'),
        ('met', 'canonical_grid_cell', 'canonical_grid_cell_direct_delete_blocked_trg')
      )
  ) planted;
  IF v_planted IS NOT NULL THEN
    IF v_strict THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): %', v_planted;
    ELSE
      RAISE WARNING 'SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): %', v_planted;
    END IF;
  END IF;

  -- The ALTER TABLE form: a stored expression is evaluated with the authority
  -- of whoever writes the row, so every function it reaches must be one a
  -- superuser writer can be handed safely -- superuser-owned, outside a temp
  -- schema, executable by the write role itself, and off the deny-list.
  SELECT string_agg(descr, '; ' ORDER BY descr) INTO v_smuggled
  FROM (
    SELECT DISTINCT relation || ' ' || kind || ' references ' || fn
                    || ' -- ' || untrusted_reason AS descr
    FROM pg_temp.nhms_audit_function_refs
    WHERE untrusted_reason IS NOT NULL
  ) smuggled;
  IF v_smuggled IS NOT NULL THEN
    IF v_strict THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: untrusted function in a stored expression (it is evaluated with the authority of whoever writes the row): %', v_smuggled;
    ELSE
      RAISE WARNING 'SECURITY REGRESSION: untrusted function in a stored expression (it is evaluated with the authority of whoever writes the row): %', v_smuggled;
    END IF;
  END IF;

  -- Enabled is not the same as present.  `DROP TRIGGER` on one of the four is
  -- refused for the write roles by the event trigger (they are on ordinary
  -- tables, where TimescaleDB does not intercept the DDL), but the superuser
  -- lanes can drop one, and a missing guard leaves NO trace in an audit that
  -- only looks at what exists.  Count them.
  SELECT count(*) INTO v_present
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE (n.nspname, c.relname, t.tgname) IN (
      ('met', 'canonical_met_product', 'canonical_met_product_grid_definition_uri_match_trg'),
      ('met', 'canonical_grid_snapshot', 'canonical_grid_snapshot_identity_immutable_trg'),
      ('met', 'canonical_grid_cell', 'canonical_grid_cell_immutable_trg'),
      ('met', 'canonical_grid_cell', 'canonical_grid_cell_direct_delete_blocked_trg')
    );
  IF v_present <> 4 THEN
    IF v_strict THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: only % of the 4 allow-listed migration triggers are present -- a dropped guard is invisible to an inventory of what exists', v_present;
    ELSE
      RAISE WARNING 'SECURITY REGRESSION: only % of the 4 allow-listed migration triggers are present -- a dropped guard is invisible to an inventory of what exists', v_present;
    END IF;
  END IF;

  -- `ALTER TABLE ... DISABLE TRIGGER` needs no rule/trigger DDL tag, so the
  -- event trigger never sees it: an allow-listed guard can be switched off in
  -- place.  000043 creates all four with the default origin firing mode, so
  -- anything other than 'O' is drift.
  SELECT string_agg(descr, '; ' ORDER BY descr) INTO v_disabled
  FROM (
    SELECT n.nspname || '.' || c.relname || ' trigger ' || t.tgname || ' (tgenabled=' || t.tgenabled::text || ')' AS descr
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname, c.relname, t.tgname) IN (
        ('met', 'canonical_met_product', 'canonical_met_product_grid_definition_uri_match_trg'),
        ('met', 'canonical_grid_snapshot', 'canonical_grid_snapshot_identity_immutable_trg'),
        ('met', 'canonical_grid_cell', 'canonical_grid_cell_immutable_trg'),
        ('met', 'canonical_grid_cell', 'canonical_grid_cell_direct_delete_blocked_trg')
      )
      AND t.tgenabled <> 'O'
  ) disabled;
  IF v_disabled IS NOT NULL THEN
    IF v_strict THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: allow-listed trigger not enabled: % -- the migration guard it implements is switched off', v_disabled;
    ELSE
      RAISE WARNING 'SECURITY REGRESSION: allow-listed trigger not enabled: % -- the migration guard it implements is switched off', v_disabled;
    END IF;
  END IF;

  SELECT CASE
           WHEN e.evtname IS NULL THEN 'missing'
           WHEN e.evtenabled = 'D' THEN 'disabled'
           WHEN NOT o.rolsuper THEN 'owned by the non-superuser role ' || o.rolname
         END
  INTO v_event
  FROM (SELECT 1) present
  LEFT JOIN pg_event_trigger e ON e.evtname = 'nhms_guard_no_write_role_rules_triggers'
  LEFT JOIN pg_roles o ON o.oid = e.evtowner;
  IF v_event IS NOT NULL THEN
    IF v_strict THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: event trigger nhms_guard_no_write_role_rules_triggers is % -- the write roles can plant rules and triggers again; re-run the provision script', v_event;
    ELSE
      RAISE WARNING 'SECURITY REGRESSION: event trigger nhms_guard_no_write_role_rules_triggers is % -- the write roles can plant rules and triggers again; re-run the provision script', v_event;
    END IF;
  END IF;

  -- TEMP is the write roles' only function-authoring surface, so a re-granted
  -- TEMP puts the pg_temp gadget back.  Verdict gated on v_strict and NOT on
  -- the presence of a finding: the tightening runs in the full-mode ownership
  -- phase only, so on a database where that phase has deliberately not run yet
  -- (`--roles-only`, pre-merge) a warning would be pure noise -- and
  -- `--roles-only` is exactly the invocation that runs with strict_audit off.
  -- Gating on `do_ownership` instead would have left the mandatory audit-only
  -- invocation (runbook 9.6, `do_ownership=off -v strict_audit=on`, run before
  -- every superuser write) unable to see a re-grant, which is the one place
  -- this has to be caught.  The state itself is printed in every mode above.
  IF v_strict THEN
    SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO v_temp
    FROM pg_roles r
    WHERE r.rolname IN ('nhms_ingest_rw', 'nhms_download_rw')
      AND has_database_privilege(r.oid, current_database(), 'TEMP');
    IF v_temp IS NOT NULL THEN
      RAISE EXCEPTION 'SECURITY REGRESSION: % still hold(s) TEMP on this database -- pg_temp is the only schema in which a write role can author a function, and such a function is evaluated with the authority of whoever writes the row; re-run the full provision', v_temp;
    END IF;
  END IF;
END
$planted$;
DROP TABLE pg_temp.nhms_audit_function_refs;

\echo '## audit: CREATE on tablespace nhms_cold for nhms_ingest_rw'
-- The grant itself is a \gexec over pg_tablespace and emits NOTHING when the
-- tablespace is absent, so neither a skipped nor a later-revoked grant shows up
-- anywhere else.  The cold-residency lane would otherwise discover it at its
-- first `ALTER ... SET TABLESPACE nhms_cold`.
SELECT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'nhms_cold') AS nhms_cold_present \gset
\if :nhms_cold_present
DO $cold_tablespace$
BEGIN
  IF NOT has_tablespace_privilege('nhms_ingest_rw', 'nhms_cold', 'CREATE') THEN
    RAISE WARNING 'cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold -- ALTER ... SET TABLESPACE will be refused';
  ELSE
    RAISE NOTICE 'nhms_ingest_rw holds CREATE on tablespace nhms_cold';
  END IF;
END
$cold_tablespace$;
\else
\echo '   tablespace nhms_cold absent -- CREATE grant skipped (expected off node-27; on node-27 this means the #1894 install did not run)'
\endif

\if :strict_audit
-- Full mode only: owner drift is a hard failure, so the cutover cannot proceed
-- on a partial transfer.
DO $strict$
DECLARE
  v_drift int;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'nhms_cold') THEN
    IF NOT has_tablespace_privilege('nhms_ingest_rw', 'nhms_cold', 'CREATE') THEN
      RAISE EXCEPTION 'cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold; re-run the provision script';
    END IF;
  END IF;

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
