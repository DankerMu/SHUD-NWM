# Local disposable-container transcript — issue #1774 (T2)

**Captured at: post-round-2 fix (§0–§13), extended after the round-3 fix (§14).** The whole file was re-run end-to-end after the
round-2 review fixes (membership audit in both directions, the rule/trigger event
trigger and its allow-list audit, the full-mode exit-code mapping); every block
below is output from that single run, in the order shown, except §13 and §14
(two later containers: one re-checks a literal changed after this capture, the
other carries the round-3 fix that closes §12), §15 (round-4: four more
containers), Appendix A (an earlier container,
unchanged code path) and Appendix B (a static scan). One consequence worth
stating rather than hiding: §0–§12 were captured before the round-3
function-privilege sweep landed, so their audit blocks do not contain the
`## audit: function-privilege sweep` section that every mode now prints — §14
shows it, in a clean full-mode run and in the audit-only invocation.

Oracle: a **fresh** `timescale/timescaledb:2.10.2-pg15` container (PG 15.2 / TSDB 2.10.2,
the node-27 versions), created and destroyed for this run:

```
docker run -d --name nwm-probe-1774-fix2b --env-file <scratch env> \
  timescale/timescaledb:2.10.2-pg15
$ docker exec nwm-probe-1774-fix2b psql -U postgres -tAc "select version()"
PostgreSQL 15.2 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 12.2.1_git20220924-r4) 12.2.1 20220924, 64-bit
```

No real secret appears below. The container is disposable, its `POSTGRES_PASSWORD`
and the two role passwords are throwaway values that exist only inside it and are
forwarded to `psql` **by name** (`docker exec -e VAR`); they are written here as
`<redacted>`. The container was removed with `docker rm -f nwm-probe-1774-fix2b`
at the end.

## 0. Fixture: the node-27 shape

Created by the **superuser `nhms`**, exactly as the migrations do on the live box —
`nhms` superuser + `nhms_display_ro` readonly + database `nhms`; schemas `core`,
`hydro`, `met`, `ops`, `map`; the two compression-capable hypertables
(`hydro.river_timeseries`, `met.forcing_station_timeseries`); three authority tables
with identity sequences; one standalone sequence; one view; one materialized view;
`GRANT SELECT` to `nhms_display_ro` on all of them.

`flood` is deliberately **not** created — it is provisioned outside `db/` on node-27,
and its absence has to be tolerated rather than fatal. Neither is `nhms_cold` (until
§6): the tablespace is a one-time superuser install (#1894). The four `met` triggers
of migration 000043 arrive in §10, where the allow-list needs them.

## 1. `--roles-only` — the additive, pre-merge phase

```
$ NODE27_WRITE_ROLES_CONTAINER=nwm-probe-1774-fix2b \
  NODE27_INGEST_RW_PASSWORD=<redacted> NODE27_DOWNLOAD_RW_PASSWORD=<redacted> \
  bash scripts/node27_provision_write_roles.sh --roles-only

node27-write-roles: container=nwm-probe-1774-fix2b database=nhms superuser=nhms
node27-write-roles: mode=roles-only (additive; no ownership transfer, no relation lock)
## phase: roles (additive -- no ownership transfer, no relation lock)
NOTICE:  created role nhms_ingest_rw
NOTICE:  created role nhms_download_rw
   nhms_ingest_rw password set from NODE27_INGEST_RW_PASSWORD
   nhms_download_rw password set from NODE27_DOWNLOAD_RW_PASSWORD
## grants: nhms_ingest_rw over the six application schemas
## default privileges: ALTER DEFAULT PRIVILEGES FOR ROLE nhms -> nhms_ingest_rw
## grants: nhms_download_rw over met only
## negative probe: COPY ... FROM PROGRAM must be refused for both write roles
NOTICE:  copy-from-program refused for nhms_ingest_rw: must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
NOTICE:  copy-from-program refused for nhms_download_rw: must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
## guard: event trigger refusing CREATE RULE / CREATE TRIGGER from the write roles
NOTICE:  event trigger "nhms_guard_no_write_role_rules_triggers" does not exist, skipping
## audit: invocation phase = roles
## audit: ownership pass  = n/a
## audit: write-role flags (all five privilege flags must be false)
     rolname      | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin 
------------------+----------+---------------+-------------+----------------+--------------+-------------
 nhms             | t        | f             | f           | f              | f            | t
 nhms_display_ro  | f        | f             | f           | f              | f            | t
 nhms_download_rw | f        | f             | f           | f              | f            | t
 nhms_ingest_rw   | f        | f             | f           | f              | f            | t
(4 rows)

## audit: relation ownership summary for the application schemas
 schema | relkind | owner | relations 
--------+---------+-------+-----------
 core   | S       | nhms  |         1
 core   | r       | nhms  |         1
 hydro  | r       | nhms  |         1
 map    | S       | nhms  |         1
 map    | m       | nhms  |         1
 map    | v       | nhms  |         1
 met    | S       | nhms  |         1
 met    | r       | nhms  |         2
 ops    | S       | nhms  |         1
 ops    | r       | nhms  |         1
(10 rows)

## audit: owner drift -- relations NOT owned by nhms_ingest_rw
            relation            | relkind | owner 
--------------------------------+---------+-------
 core.basin                     | r       | nhms
 core.basin_id_seq              | S       | nhms
 hydro.river_timeseries         | r       | nhms
 map.basin_count                | m       | nhms
 map.basin_slug                 | v       | nhms
 map.tile_epoch_seq             | S       | nhms
 met.forcing_station_timeseries | r       | nhms
 met.met_station                | r       | nhms
 met.met_station_id_seq         | S       | nhms
 ops.run_journal                | r       | nhms
 ops.run_journal_id_seq         | S       | nhms
(11 rows)

## audit: compression-capable hypertable owners
 hypertable_schema |      hypertable_name       | compression_enabled | owner 
-------------------+----------------------------+---------------------+-------
 hydro             | river_timeseries           | t                   | nhms
 met               | forcing_station_timeseries | t                   | nhms
(2 rows)

## audit: nhms_display_ro effective SELECT set over the application schemas
            relation            
--------------------------------
 core.basin
 hydro.river_timeseries
 map.basin_count
 map.basin_slug
 met.forcing_station_timeseries
 met.met_station
 ops.run_journal
(7 rows)

## audit: rules and non-internal triggers in the application schemas
 relation | kind | name 
----------+------+------
(0 rows)

## audit: CREATE on tablespace nhms_cold for nhms_ingest_rw
   tablespace nhms_cold absent -- CREATE grant skipped (expected off node-27; on node-27 this means the #1894 install did not run)
## audit: non-strict (--roles-only): owner drift above is expected, ownership is transferred post-merge
node27-write-roles: roles-only phase complete; ownership transfer deferred to the post-merge run
EXIT=0
```

**Key results.** Both roles created with all five privilege flags false; the absent
`flood` schema and the absent `nhms_cold` tablespace were tolerated (the run is clean);
**every one of the 11 relations is still owned by `nhms`** — the mode really is
additive, it executes no `ALTER … OWNER TO`; both `COPY … FROM PROGRAM` probes were
refused; the `nhms_guard_no_write_role_rules_triggers` event trigger was installed
**in this phase**, i.e. before the merge and before the transfer that gives the write
roles the ownership it constrains (the `does not exist, skipping` NOTICE is the
idempotent `DROP … IF EXISTS` on a first run); the rule/trigger inventory is empty;
neither password appears anywhere in the output.

## 2. Full mode — the ownership transfer

```
$ NODE27_WRITE_ROLES_CONTAINER=nwm-probe-1774-fix2b bash scripts/node27_provision_write_roles.sh

node27-write-roles: NODE27_INGEST_RW_PASSWORD unset -- that role keeps its existing password; a role CREATED by this run has none and cannot log in over TCP until one is set
node27-write-roles: NODE27_DOWNLOAD_RW_PASSWORD unset -- that role keeps its existing password; a role CREATED by this run has none and cannot log in over TCP until one is set
node27-write-roles: container=nwm-probe-1774-fix2b database=nhms superuser=nhms
node27-write-roles: mode=full (additive phase + ownership transfer)
node27-write-roles: captured 7 display-visible relation(s) before the transfer
node27-write-roles: ownership pass 1/5
## phase: roles (additive -- no ownership transfer, no relation lock)
NOTICE:  role nhms_ingest_rw already existed; flags reasserted
NOTICE:  role nhms_download_rw already existed; flags reasserted
   NODE27_INGEST_RW_PASSWORD unset -- nhms_ingest_rw keeps its existing password; if this run CREATED the role it has none and cannot log in over TCP until one is set
   NODE27_DOWNLOAD_RW_PASSWORD unset -- nhms_download_rw keeps its existing password; if this run CREATED the role it has none and cannot log in over TCP until one is set
## grants: nhms_ingest_rw over the six application schemas
## default privileges: ALTER DEFAULT PRIVILEGES FOR ROLE nhms -> nhms_ingest_rw
## grants: nhms_download_rw over met only
## negative probe: COPY ... FROM PROGRAM must be refused for both write roles
NOTICE:  copy-from-program refused for nhms_ingest_rw: must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
NOTICE:  copy-from-program refused for nhms_download_rw: must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
## guard: event trigger refusing CREATE RULE / CREATE TRIGGER from the write roles
NOTICE:  schema "nhms_guard" already exists, skipping
## phase: ownership transfer of the application schemas to nhms_ingest_rw (pass 1 of the runner retry loop)
node27-write-roles: after pass 1: 0 relation(s) still not owned by nhms_ingest_rw
node27-write-roles: relacl diff across the transfer (informational -- ALTER ... OWNER TO rewrites grantor references):
-core.basin | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
-core.basin_id_seq | S | nhms | nhms=rwU/nhms nhms_ingest_rw=U/nhms
-hydro.river_timeseries | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
-map.basin_count | m | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
-map.basin_slug | v | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
-map.tile_epoch_seq | S | nhms | nhms=rwU/nhms nhms_ingest_rw=U/nhms
-met.forcing_station_timeseries | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms nhms_download_rw=arwd/nhms
-met.met_station | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms nhms_download_rw=arwd/nhms
-met.met_station_id_seq | S | nhms | nhms=rwU/nhms nhms_ingest_rw=U/nhms nhms_download_rw=U/nhms
-ops.run_journal | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
-ops.run_journal_id_seq | S | nhms | nhms=rwU/nhms nhms_ingest_rw=U/nhms
+core.basin | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
+core.basin_id_seq | S | nhms_ingest_rw | nhms_ingest_rw=rwU/nhms_ingest_rw
+hydro.river_timeseries | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
+map.basin_count | m | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
+map.basin_slug | v | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
+map.tile_epoch_seq | S | nhms_ingest_rw | nhms_ingest_rw=rwU/nhms_ingest_rw
+met.forcing_station_timeseries | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw nhms_download_rw=arwd/nhms_ingest_rw
+met.met_station | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw nhms_download_rw=arwd/nhms_ingest_rw
+met.met_station_id_seq | S | nhms_ingest_rw | nhms_ingest_rw=rwU/nhms_ingest_rw nhms_download_rw=U/nhms_ingest_rw
+ops.run_journal | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
+ops.run_journal_id_seq | S | nhms_ingest_rw | nhms_ingest_rw=rwU/nhms_ingest_rw
node27-write-roles: nhms_display_ro effective SELECT set diff (must be empty):
  (SELECT privilege set unchanged -- has_table_privilege per relation, identical before and after)
## audit: invocation phase = audit
## audit: ownership pass  = n/a
## audit: write-role flags (all five privilege flags must be false)
     rolname      | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin 
------------------+----------+---------------+-------------+----------------+--------------+-------------
 nhms             | t        | f             | f           | f              | f            | t
 nhms_display_ro  | f        | f             | f           | f              | f            | t
 nhms_download_rw | f        | f             | f           | f              | f            | t
 nhms_ingest_rw   | f        | f             | f           | f              | f            | t
(4 rows)

## audit: relation ownership summary for the application schemas
 schema | relkind |     owner      | relations 
--------+---------+----------------+-----------
 core   | S       | nhms_ingest_rw |         1
 core   | r       | nhms_ingest_rw |         1
 hydro  | r       | nhms_ingest_rw |         1
 map    | S       | nhms_ingest_rw |         1
 map    | m       | nhms_ingest_rw |         1
 map    | v       | nhms_ingest_rw |         1
 met    | S       | nhms_ingest_rw |         1
 met    | r       | nhms_ingest_rw |         2
 ops    | S       | nhms_ingest_rw |         1
 ops    | r       | nhms_ingest_rw |         1
(10 rows)

## audit: owner drift -- relations NOT owned by nhms_ingest_rw
 relation | relkind | owner 
----------+---------+-------
(0 rows)

## audit: compression-capable hypertable owners
 hypertable_schema |      hypertable_name       | compression_enabled |     owner      
-------------------+----------------------------+---------------------+----------------
 hydro             | river_timeseries           | t                   | nhms_ingest_rw
 met               | forcing_station_timeseries | t                   | nhms_ingest_rw
(2 rows)

## audit: nhms_display_ro effective SELECT set over the application schemas
            relation            
--------------------------------
 core.basin
 hydro.river_timeseries
 map.basin_count
 map.basin_slug
 met.forcing_station_timeseries
 met.met_station
 ops.run_journal
(7 rows)

## audit: rules and non-internal triggers in the application schemas
 relation | kind | name 
----------+------+------
(0 rows)

## audit: CREATE on tablespace nhms_cold for nhms_ingest_rw
   tablespace nhms_cold absent -- CREATE grant skipped (expected off node-27; on node-27 this means the #1894 install did not run)
## audit: OK -- no owner drift
node27-write-roles: full provision complete; audit clean
EXIT=0
```

**Key results.** The `relacl` diff is exactly what D4 predicted — the *grantor*
references were rewritten `…/nhms` → `…/nhms_ingest_rw`, which is why the display
boundary is asserted as an **effective SELECT set** (unchanged, 7 relations) and not as
ACL text. `nhms_display_ro=r/…` survives on every relation. Both hypertables changed
owner; chunk ownership followed (§4). (The two `diff -u` file-header lines and the
`@@` hunk header are elided; everything else is verbatim.)

## 3. Idempotence (spec: "Provision is idempotent")

```
$ bash scripts/node27_provision_write_roles.sh   # run 2 -> EXIT=0
$ bash scripts/node27_provision_write_roles.sh   # run 3 -> EXIT=0
$ diff -u <(audit section of run 2) <(audit section of run 3)
IDEMPOTENT: audit output byte-identical across runs 2 and 3
node27-write-roles: relacl diff across the transfer (informational -- ALTER ... OWNER TO rewrites grantor references):
  (no relacl change)
node27-write-roles: nhms_display_ro effective SELECT set diff (must be empty):
```

## 4. `nhms_ingest_rw` as a real login (not `SET ROLE` from a superuser session)

```
$ docker exec -i nwm-probe-1774-fix2b psql -U nhms_ingest_rw -d nhms -X < probe_ingest.sql
  session_user  |  current_user  | usesuper 
----------------+----------------+----------
 nhms_ingest_rw | nhms_ingest_rw | f
(1 row)

--- compress_chunk on the oldest hydro.river_timeseries chunk ---
             compress_chunk             
----------------------------------------
 _timescaledb_internal._hyper_1_1_chunk
(1 row)

--- decompress_chunk, then compress again ---
            decompress_chunk            
----------------------------------------
 _timescaledb_internal._hyper_1_1_chunk
(1 row)

             compress_chunk             
----------------------------------------
 _timescaledb_internal._hyper_1_1_chunk
(1 row)

--- chunks_detailed_size ---
    chunk_name    | total_bytes 
------------------+-------------
 _hyper_1_1_chunk |       40960
 _hyper_1_2_chunk |       40960
 _hyper_1_3_chunk |       40960
(3 rows)

--- drop_chunks older than 2026-01-02 ---
              drop_chunks               
----------------------------------------
 _timescaledb_internal._hyper_1_1_chunk
(1 row)

--- INSERT into the hypertable (creates a new chunk; owner must be inherited) ---
INSERT 0 1
--- negative: COPY ... FROM PROGRAM ---
CREATE TABLE
ERROR:  must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
--- negative: CREATE ROLE / CREATE DATABASE ---
ERROR:  permission denied to create role
ERROR:  permission denied to create database
```

Chunk-ownership cascade, including the chunk created *by the role's own INSERT*
and the compressed hypertables (listing filtered to chunk relations; the
`_timescaledb_catalog` tables stay with the extension owner `postgres`):

```
                     relname                      |     owner      
--------------------------------------------------+----------------
 _compressed_hypertable_2                         | nhms_ingest_rw
 _compressed_hypertable_4                         | nhms_ingest_rw
 _hyper_1_12_chunk                                | nhms_ingest_rw
 _hyper_1_2_chunk                                 | nhms_ingest_rw
 _hyper_1_3_chunk                                 | nhms_ingest_rw
 _hyper_3_4_chunk                                 | nhms_ingest_rw
 _hyper_3_5_chunk                                 | nhms_ingest_rw
 compress_hyper_2_10_chunk                        | nhms_ingest_rw
 compress_hyper_2_11_chunk                        | nhms_ingest_rw
```

### Both stats-guard ANALYZE legs, no `WARNING: skipping`

```
$ docker exec -i nwm-probe-1774-fix2b psql -U nhms_ingest_rw -d nhms -X < probe_analyze.sql 2>&1
--- stats-guard leg 1 (#1643): ANALYZE an UNCOMPRESSED frontier chunk ---
ANALYZE
--- stats-guard leg 2 (#1468): ANALYZE an ordinary authority table ---
ANALYZE
ANALYZE

$ # read back from a LATER transaction (as nhms):
      schemaname       |     relname      | analyzed 
-----------------------+------------------+----------
 _timescaledb_internal | _hyper_1_2_chunk | t
 _timescaledb_internal | _hyper_1_3_chunk | t
 core                  | basin            | t
 met                   | met_station      | t
(4 rows)
```

Standard output **and** standard error were captured together; no
`WARNING: skipping … only table or database owner can analyze it` was emitted, and
`last_analyze` is non-NULL for both legs. (An *uncompressed* chunk is analyzed on
purpose: ANALYZE on a bare compressed-chunk name zeroes the relstats TimescaleDB
preserved at compression time, which is why the autopipe guard excludes them, #1378 D3.)

## 5. `nhms_download_rw` as a real login

```
$ docker exec -i nwm-probe-1774-fix2b psql -U nhms_download_rw -d nhms -X ...
   session_user   |   current_user   | usesuper 
------------------+------------------+----------
 nhms_download_rw | nhms_download_rw | f
(1 row)

--- privilege-shape INSERT into a met.* table (DML grant) ---
INSERT 0 1
--- negative: compress_chunk (not the owner) ---
ERROR:  must be owner of hypertable "forcing_station_timeseries"
--- negative: DML outside met (no grant) ---
ERROR:  permission denied for schema ops
LINE 1: INSERT INTO ops.run_journal (note) VALUES ('nope')
                    ^
--- negative: CREATE RULE on a met relation it can write ---
ERROR:  refused: role nhms_download_rw may not run CREATE RULE -- a rule action or trigger body executes as the role that next writes the relation, including the migration superuser
HINT:  run migration-class DDL as the migration role; see OpenSpec change node27-write-path-roles design D2
CONTEXT:  PL/pgSQL function nhms_guard.refuse_write_role_rules_and_triggers() line 4 at RAISE
--- negative: COPY ... FROM PROGRAM ---
ERROR:  must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
HINT:  Anyone can COPY to stdout or from stdin. psql's \copy command also works for anyone.
```

The `CREATE RULE` refusal is the event trigger of §10: it covers **both** write
roles, not only the owner of the relations.

## 6. Cold residency: `SET TABLESPACE` needs, and gets, the conditional grant

Before the tablespace existed, every run above was clean — the conditional
`GRANT CREATE ON TABLESPACE` simply produced no statement. After
`CREATE TABLESPACE nhms_cold` (superuser, one-time, #1894):

```
$ # tablespace absent so far: every run above was clean and the conditional GRANT emitted nothing
$ psql -U nhms_ingest_rw -c "ALTER TABLE ops.run_journal SET TABLESPACE nhms_cold"   # before re-provision
ERROR:  permission denied for tablespace nhms_cold
$ # audit-only, STRICT, grant missing:
psql EXIT=3
WARNING:  cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold -- ALTER ... SET TABLESPACE will be refused
ERROR:  cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold; re-run the provision script
$ # audit-only, NON-strict, grant missing:
psql EXIT=0
WARNING:  cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold -- ALTER ... SET TABLESPACE will be refused
$ bash scripts/node27_provision_write_roles.sh   # re-run issues the GRANT
EXIT=0
## audit: CREATE on tablespace nhms_cold for nhms_ingest_rw
NOTICE:  nhms_ingest_rw holds CREATE on tablespace nhms_cold
nhms_cold|nhms=C/nhms nhms_ingest_rw=C/nhms
$ psql -U nhms_ingest_rw < probe_cold.sql   # compress a chunk, then move it into nhms_cold
             compress_chunk              
-----------------------------------------
 _timescaledb_internal._hyper_1_12_chunk
(1 row)

          relname          |  spcname  
---------------------------+-----------
 _hyper_1_12_chunk         | nhms_cold
 compress_hyper_2_13_chunk | nhms_cold
 pg_toast_18006            | nhms_cold
 pg_toast_18006_index      | nhms_cold
(4 rows)

$ # now REVOKE it again and show which invocation reddens:
$ bash scripts/node27_provision_write_roles.sh --roles-only   # do_roles=on re-grants BEFORE the audit
EXIT=0
NOTICE:  nhms_ingest_rw holds CREATE on tablespace nhms_cold
$ # audit-only, NON-strict, grant revoked:
psql EXIT=0
WARNING:  cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold -- ALTER ... SET TABLESPACE will be refused
$ # audit-only, STRICT, grant revoked:
psql EXIT=3
ERROR:  cold-residency regression: nhms_ingest_rw lacks CREATE on tablespace nhms_cold; re-run the provision script
```

**Read the last three blocks carefully — they are the reason §9.6 of the runbook
exists.** A revoked `nhms_cold` CREATE grant does **not** redden `--roles-only`
(or full mode): both run with `do_roles=on`, so the runner re-issues the `GRANT`
before the audit looks at it. Only the **audit-only** invocation
(`-v do_roles=off -v do_ownership=off -v do_audit=on -v strict_audit=on`) sees the
revoked state — exit 3 strict, `WARNING` non-strict. The same is true of the event
trigger (§11).

## 7. Drift regression row (spec: "Migration-added tables stay usable before re-provision")

```
$ psql -U nhms -c "CREATE TABLE hydro.newtab(x int); INSERT ..."
$ psql -U nhms_ingest_rw -c "INSERT INTO hydro.newtab VALUES (1);" -c 'ANALYZE "hydro"."newtab";'
INSERT 0 1
ANALYZE
WARNING:  skipping "newtab" --- only table or database owner can analyze it
$ bash scripts/node27_provision_write_roles.sh --roles-only
EXIT=0
## audit: owner drift -- relations NOT owned by nhms_ingest_rw
   relation   | relkind | owner 
--------------+---------+-------
 hydro.newtab | r       | nhms
(1 row)

## audit: non-strict (--roles-only): owner drift above is expected, ownership is transferred post-merge
$ # the STRICT audit-only invocation refuses:
psql EXIT=3
ERROR:  owner drift: 1 application relation(s) not owned by nhms_ingest_rw (listed above); re-run the provision script
$ bash scripts/node27_provision_write_roles.sh   # converges
EXIT=0
node27-write-roles: full provision complete; audit clean
$ psql -U nhms_ingest_rw -c 'ANALYZE "hydro"."newtab";'
ANALYZE
```

`INSERT` succeeds (default privileges keep the lane writable) and only `ANALYZE`
degrades to a warning; the audit reports the drift; `--roles-only` stays 0 because
drift is expected in the additive phase; the strict audit-only invocation refuses
with exit 3; the full re-run converges and the ANALYZE leg is alive again.

## 8. Catalog state after §7

```
     rolname      | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin 
------------------+----------+---------------+-------------+----------------+--------------+-------------
 nhms             | t        | f             | f           | f              | f            | t
 nhms_display_ro  | f        | f             | f           | f              | f            | t
 nhms_download_rw | f        | f             | f           | f              | f            | t
 nhms_ingest_rw   | f        | f             | f           | f              | f            | t
 postgres         | t        | t             | t           | t              | t            | t
(5 rows)

 schema | relkind |     owner      | count 
--------+---------+----------------+-------
 core   | S       | nhms_ingest_rw |     1
 core   | r       | nhms_ingest_rw |     1
 hydro  | r       | nhms_ingest_rw |     2
 map    | S       | nhms_ingest_rw |     1
 map    | m       | nhms_ingest_rw |     1
 map    | v       | nhms_ingest_rw |     1
 met    | S       | nhms_ingest_rw |     1
 met    | r       | nhms_ingest_rw |     2
 ops    | S       | nhms_ingest_rw |     1
 ops    | r       | nhms_ingest_rw |     1
(10 rows)
```

## 9. Role membership is refused in BOTH directions

```
$ psql -U nhms -c "GRANT pg_read_server_files TO nhms_ingest_rw"
$ bash scripts/node27_provision_write_roles.sh --roles-only
EXIT=3
ERROR:  SECURITY REGRESSION: role nhms_ingest_rw is a member of pg_read_server_files -- the write roles must hold no role membership; revoke it before proceeding
node27-write-roles: FAILED -- roles-only provision refused (docker exec/psql exit 3); do not cut the env files over

$ psql -U nhms -c "GRANT nhms_ingest_rw TO nhms_display_ro"
$ psql -U nhms_display_ro -c "SET ROLE nhms_ingest_rw; SELECT current_user, session_user"   # what the grant buys
nhms_ingest_rw|nhms_display_ro
$ # the audit at the pre-fix commit a953f9aa (git show HEAD:db/roles/...), strict, audit-only:
psql EXIT=0
SECURITY REGRESSION lines at HEAD: 0
$ bash scripts/node27_provision_write_roles.sh --roles-only   # with the fix
EXIT=3
ERROR:  SECURITY REGRESSION: role nhms_ingest_rw has been granted to nhms_display_ro -- the write roles must not be reachable by SET ROLE from any other role; revoke it before proceeding
node27-write-roles: FAILED -- roles-only provision refused (docker exec/psql exit 3); do not cut the env files over
$ psql -U nhms -c "REVOKE nhms_ingest_rw FROM nhms_display_ro"
EXIT=0
```

**Key result.** The member direction (`GRANT pg_read_server_files TO
nhms_ingest_rw`) was already caught. The grantee direction was not: at the
pre-fix commit the
strict audit-only invocation exited **0** with zero `SECURITY REGRESSION` lines
while `nhms_display_ro` — the read-only display credential — could
`SET ROLE nhms_ingest_rw` into the full write and ownership set. With the fix the
same state exits 3 naming both roles, and a `REVOKE` returns the run to 0.

## 10. The event trigger refuses owner-planted rules and triggers

Prevention half of the owner-planted escalation path. The four `met` triggers of
migration 000043 are created here as `nhms` (stand-ins with the same
`(schema, table, trigger)` triples — the 2.10.2 image ships no PostGIS, so the real
migration cannot be applied), which also proves a **migration-style `CREATE TRIGGER`
as the superuser still works**:

```
$ psql -U nhms < migration_triggers.sql   # migration-style CREATE TRIGGER as the superuser
psql EXIT=0
4 triggers created as nhms
$ bash scripts/node27_provision_write_roles.sh   # converge ownership of the new tables
EXIT=0

--- as nhms_ingest_rw: CREATE RULE on an owned relation ---
ERROR:  refused: role nhms_ingest_rw may not run CREATE RULE -- a rule action or trigger body executes as the role that next writes the relation, including the migration superuser
HINT:  run migration-class DDL as the migration role; see OpenSpec change node27-write-path-roles design D2
CONTEXT:  PL/pgSQL function nhms_guard.refuse_write_role_rules_and_triggers() line 4 at RAISE
--- as nhms_ingest_rw: CREATE TRIGGER on an owned relation ---
ERROR:  refused: role nhms_ingest_rw may not run CREATE TRIGGER -- a rule action or trigger body executes as the role that next writes the relation, including the migration superuser
HINT:  run migration-class DDL as the migration role; see OpenSpec change node27-write-path-roles design D2
CONTEXT:  PL/pgSQL function nhms_guard.refuse_write_role_rules_and_triggers() line 4 at RAISE
--- as nhms_ingest_rw: DROP / DISABLE the guard itself ---
ERROR:  must be owner of event trigger nhms_guard_no_write_role_rules_triggers
ERROR:  must be owner of event trigger nhms_guard_no_write_role_rules_triggers
--- as nhms_ingest_rw: replace the guard function ---
ERROR:  permission denied for schema nhms_guard
--- still allowed: the tiering DDL the lanes actually need ---
 compress_chunk 
----------------
(0 rows)

INSERT 0 1
              drop_chunks               
----------------------------------------
 _timescaledb_internal._hyper_1_2_chunk
(1 row)

$ psql -U nhms -tAc "SELECT evtname, evtenabled, evtowner::regrole FROM pg_event_trigger"
nhms_guard_no_write_role_rules_triggers|O|nhms
timescaledb_ddl_command_end|O|postgres
timescaledb_ddl_sql_drop|O|postgres
```

**Key results.** As `nhms_ingest_rw`: `CREATE RULE` and `CREATE TRIGGER` on a
relation it owns are both refused by the event trigger; it can neither drop nor
disable that event trigger (`must be owner of event trigger`) nor replace its
function (`permission denied for schema nhms_guard`). As `nhms` the same
`CREATE TRIGGER` succeeds. The tiering DDL the lanes actually need is untouched —
the `INSERT` (new chunk) and `drop_chunks` still work, and TimescaleDB's own chunk
DDL never trips the guard (compression was already exercised in §4 and §6; by this
point no uncompressed chunk was left, hence the empty `compress_chunk` result).

## 11. The audit refuses rules and triggers outside the migration allow-list

Detection half: an object planted **as the superuser** (a compromised migration, or
one that predates the guard) fires on the next superuser write just the same, and no
event trigger can stop it.

```
$ # allow-listed triggers only -> the inventory lists them and the strict audit is clean
psql EXIT=0
## audit: rules and non-internal triggers in the application schemas
          relation           |  kind   |                        name                         
-----------------------------+---------+-----------------------------------------------------
 met.canonical_grid_cell     | trigger | canonical_grid_cell_direct_delete_blocked_trg
 met.canonical_grid_cell     | trigger | canonical_grid_cell_immutable_trg
 met.canonical_grid_snapshot | trigger | canonical_grid_snapshot_identity_immutable_trg
 met.canonical_met_product   | trigger | canonical_met_product_grid_definition_uri_match_trg
(4 rows)

## audit: OK -- no owner drift

$ psql -U nhms -c "CREATE TRIGGER planted_trg ..." -c "CREATE RULE planted_rule ..."   # planted as the superuser, bypassing the event trigger
$ # audit-only, STRICT:
psql EXIT=3
## audit: rules and non-internal triggers in the application schemas
          relation           |  kind   |                        name                         
-----------------------------+---------+-----------------------------------------------------
 core.basin                  | rule    | planted_rule
 met.canonical_grid_cell     | trigger | canonical_grid_cell_direct_delete_blocked_trg
 met.canonical_grid_cell     | trigger | canonical_grid_cell_immutable_trg
 met.canonical_grid_snapshot | trigger | canonical_grid_snapshot_identity_immutable_trg
 met.canonical_met_product   | trigger | canonical_met_product_grid_definition_uri_match_trg
 ops.run_journal             | trigger | planted_trg
(6 rows)

ERROR:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): core.basin rule planted_rule; ops.run_journal trigger planted_trg
$ # audit-only, NON-strict (--roles-only severity):
psql EXIT=0
WARNING:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): core.basin rule planted_rule; ops.run_journal trigger planted_trg

$ psql -U nhms -c "ALTER EVENT TRIGGER ... DISABLE"   # the guard itself, strict audit-only
psql EXIT=3
ERROR:  SECURITY REGRESSION: event trigger nhms_guard_no_write_role_rules_triggers is disabled -- the write roles can plant rules and triggers again; re-run the provision script
$ psql -U nhms -c "DROP EVENT TRIGGER ..."   # non-strict audit-only
psql EXIT=0
WARNING:  SECURITY REGRESSION: event trigger nhms_guard_no_write_role_rules_triggers is missing -- the write roles can plant rules and triggers again; re-run the provision script
$ bash scripts/node27_provision_write_roles.sh --roles-only   # the additive phase re-installs it
EXIT=0
nhms_guard_no_write_role_rules_triggers|O|nhms
```

**Key results.** With only the four allow-listed `met` triggers present the strict
audit is clean and still *prints* the inventory. A rule on `core.basin` and a
trigger on `ops.run_journal` planted as `nhms` are named individually and turn the
strict audit into exit 3 (`WARNING` non-strict). A disabled event trigger is exit 3;
a dropped one warns under `--roles-only` severity; the additive phase re-installs it,
enabled and owned by `nhms`.

## 12. The same gadget without a rule or a trigger (reproduced here, closed by detection in §14)

```
$ psql -U nhms_ingest_rw -c "ALTER TABLE core.basin ALTER COLUMN name SET DEFAULT pg_read_file(...)"
$ psql -U nhms -tAc "INSERT INTO core.basin DEFAULT VALUES RETURNING name"   # evaluated as the superuser
cd0b40514c31
```

The owner can also plant a column `DEFAULT` (or a `CHECK` expression), which is
evaluated by **whoever inserts** — here the superuser `nhms`, which returned the
container's `/etc/hostname`. `ALTER TABLE` cannot go into the event trigger's tag
list (the cold-residency lane needs `ALTER TABLE … SET TABLESPACE`), and a
volatility-based audit would flag every `nextval` default. Prevention is therefore
impossible without breaking cold residency; §14 closes both this form and its
sibling `ALTER TABLE … DISABLE TRIGGER` by **detection** — the audit's
function-privilege sweep and its `tgenabled` check. Removing the superuser-write
half (migrations, seeds and replay off `nhms`) remains the real fix and stays out
of scope for this change per design D2.

## 13. Severity parity: `strict_audit=true` is as strict as `strict_audit=on`

psql's own `\if :strict_audit` accepts `on`/`true`/`1`/`yes`, so the audit's
severity switch must accept the same spellings; otherwise `-v strict_audit=true`
would RAISE on owner drift (psql-level `\if`) but only WARN on a planted rule
(SQL-level `current_setting`) in one and the same run. Captured on a second
disposable container (`nwm-probe-1774-smoke`, same image) after that literal was
widened; a rule was planted as `nhms` (the superuser bypasses the event trigger),
then the audit-only invocation was run at each spelling:

```
$ # planted: CREATE RULE planted_rule AS ON DELETE TO met.met_station DO INSTEAD NOTHING
$ # audit-only, -v strict_audit=on
EXIT=3
 met.met_station | rule | planted_rule
ERROR:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): met.met_station rule planted_rule

$ # audit-only, -v strict_audit=true
EXIT=3
 met.met_station | rule | planted_rule
ERROR:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): met.met_station rule planted_rule

$ # audit-only, -v strict_audit=off
EXIT=0
 met.met_station | rule | planted_rule
WARNING:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): met.met_station rule planted_rule
```

Key result: both strict spellings exit 3 and name the object; `off` degrades to a
WARNING and exit 0, which is what `--roles-only` uses pre-merge.

## 14. The `ALTER TABLE` forms are caught by the strict audit

Captured on a third disposable container (`nwm-probe-1774-sweep`, same image)
carrying the round-3 fix. Its fixture adds what the migrations actually produce
— `DEFAULT nextval()`, `DEFAULT gen_random_uuid()`, `DEFAULT now()`, a `STORED`
generated column and a `CHECK` — because `GENERATED … AS IDENTITY` columns
create **no** `pg_attrdef` row at all and would have made a clean sweep vacuous.

Mechanism, measured before writing the sweep: `pg_depend` cannot be walked for
this. Dependencies on **pinned** objects — every function created by initdb,
which is exactly where `pg_read_file` lives — are never recorded:

```
$ psql -U nhms -c "SELECT ... FROM pg_depend WHERE classid IN ('pg_attrdef'::regclass,'pg_constraint'::regclass)"
  -- 227 rows, every one of them refclassid = pg_class or pg_type; ZERO rows to pg_proc,
  -- with core.t carrying DEFAULT length(pg_read_file('/etc/hostname'))
```

so the sweep scans the stored parse trees (`pg_attrdef.adbin`,
`pg_constraint.conbin`, `pg_rewrite.ev_action`) for `:funcid` / `:opfuncid`, plus
`pg_trigger.tgfoid`, and resolves each oid against
`has_function_privilege('nhms_ingest_rw', …, 'EXECUTE')`.

**Clean database, full mode (ownership transferred, strict trailing audit):**

```
## audit: function-privilege sweep over stored expressions
                                                   detail
------------------------------------------------------------------------------------------------------------
 10 expression(s)/trigger(s) scanned, 9 distinct function(s) referenced, 0 not executable by nhms_ingest_rw
(1 row)

## audit: OK -- no owner drift
node27-write-roles: full provision complete; audit clean
EXIT=0
```

**Planted column default (`ALTER TABLE … SET DEFAULT`, as `nhms`), audit-only:**

```
$ psql -U nhms -c "ALTER TABLE core.gauge_reading ALTER COLUMN label
                   SET DEFAULT length(pg_read_file('/etc/hostname'))::text"
$ # audit-only, -v strict_audit=on
## audit: function-privilege sweep over stored expressions
 10 expression(s)/trigger(s) scanned, 10 distinct function(s) referenced, 1 not executable by nhms_ingest_rw
 core.gauge_reading column default label references pg_catalog.pg_read_file -- NOT executable by nhms_ingest_rw
(2 rows)

ERROR:  SECURITY REGRESSION: core.gauge_reading column default label references pg_catalog.pg_read_file the write role cannot execute (the expression is evaluated by the role that writes the row)
EXIT=3

$ # same catalog, -v strict_audit=off (the pre-merge --roles-only severity)
WARNING:  SECURITY REGRESSION: core.gauge_reading column default label references pg_catalog.pg_read_file the write role cannot execute (the expression is evaluated by the role that writes the row)
EXIT=0
```

That the finding is real, not theoretical — the superuser writer evaluates it,
while the role that planted it cannot call the function at all:

```
$ psql -U nhms -c "INSERT INTO core.gauge_reading DEFAULT VALUES" -c "SELECT label FROM core.gauge_reading"
 label
-------
 13                      # length of the container's /etc/hostname, read by nhms
$ psql -U nhms_ingest_rw -c "SELECT pg_read_file('/etc/hostname')"
ERROR:  permission denied for function pg_read_file
```

**After `DROP DEFAULT`, audit-only strict:**

```
 9 expression(s)/trigger(s) scanned, 9 distinct function(s) referenced, 0 not executable by nhms_ingest_rw
## audit: OK -- no owner drift
EXIT=0
```

**Allow-listed trigger switched off (`ALTER TABLE … DISABLE TRIGGER`, as `nhms`):**

```
$ psql -U nhms -c "ALTER TABLE met.canonical_grid_cell DISABLE TRIGGER canonical_grid_cell_immutable_trg"
$ # audit-only, -v strict_audit=on
ERROR:  SECURITY REGRESSION: allow-listed trigger not enabled: met.canonical_grid_cell trigger canonical_grid_cell_immutable_trg (tgenabled=D) -- the migration guard it implements is switched off
EXIT=3
$ psql -U nhms -c "ALTER TABLE met.canonical_grid_cell ENABLE TRIGGER canonical_grid_cell_immutable_trg"
$ # audit-only, -v strict_audit=on
EXIT=0
```

Key result: both `ALTER TABLE` forms of the §12 gadget now red the strict audit
and name the object; the rule/trigger inventory alone did not see either (the
disabled trigger is still *listed* by the inventory, and a column default is not
a rule or a trigger at all). Note the sweep is only as good as the phase it runs
in: like every other audit leg it re-checks state the additive phase does not
restore, so it reddens the audit-only invocation of §9.6, which is why that
invocation is mandatory before every superuser-write session.

## 15. TimescaleDB internals: what the write role can actually plant there

Six more disposable containers (`nwm-probe-1774-tsdb`, `-htrig`, `-comp2`,
`-copy`, `-notvalid`, and `-scope` for the fix), all
`timescale/timescaledb:2.10.2-pg15`,
each seeded like §0 plus the migration-shaped defaults of §14, provisioned in
full mode, with at least one compressed and one uncompressed chunk.

### (a) Every `ALTER`/`CREATE` form, attempted as `nhms_ingest_rw`

After the transfer the write role owns the chunks and the internal
`_compressed_hypertable_N` (they follow their parent hypertable):

```
$ psql -U nhms -tAc "SELECT relname || ' | ' || relowner::regrole FROM pg_class ... WHERE nspname='_timescaledb_internal'"
_compressed_hypertable_2 | nhms_ingest_rw
compress_hyper_2_6_chunk | nhms_ingest_rw
_hyper_3_4_chunk         | nhms_ingest_rw
```

| target (as `nhms_ingest_rw`) | `SET DEFAULT` | `ADD CONSTRAINT … CHECK` | `CREATE TRIGGER` | `CREATE RULE` |
|---|---|---|---|---|
| ordinary table `core.basin` | **succeeds** | `permission denied for function pg_read_file` (PG) — but **`NOT VALID` succeeds** | refused by our event trigger | refused by our event trigger |
| hypertable `hydro.river_timeseries` | n/a | n/a | **succeeds — event trigger never fires** | — |
| chunk `_hyper_3_4_chunk` | `operation not supported on chunk tables` (TSDB) | same (TSDB) | refused by our event trigger | refused by our event trigger |
| compressed chunk `compress_hyper_2_6_chunk` | same (TSDB) | same (TSDB) | refused by our event trigger | refused by our event trigger |
| `_compressed_hypertable_2` | **succeeds** | `permission denied for function pg_read_file` (PG) — but **`NOT VALID` succeeds** | **succeeds — event trigger never fires** | `hypertables do not support rules` (TSDB) |

Three measured facts behind that table:

1. **TimescaleDB bypasses `ddl_command_start` for `CREATE TRIGGER` on a
   hypertable.** Same session, same role, back to back:

   ```
   $ psql -U nhms_ingest_rw -c "CREATE TRIGGER planted_plain BEFORE INSERT ON core.basin ..."
   ERROR:  refused: role nhms_ingest_rw may not run CREATE TRIGGER -- ...
   $ psql -U nhms_ingest_rw -c "CREATE TRIGGER planted_hyper BEFORE INSERT ON hydro.river_timeseries ..."
   CREATE TRIGGER
   $ psql -U nhms -c "SELECT tgrelid::regclass, tgname FROM pg_trigger WHERE tgname LIKE 'planted%'"
    hydro.river_timeseries                 | planted_hyper
    _timescaledb_internal._hyper_1_1_chunk | planted_hyper     -- propagated
    _timescaledb_internal._hyper_1_2_chunk | planted_hyper
    _timescaledb_internal._hyper_1_3_chunk | planted_hyper
   ```

   In the six application schemas that is a prevention gap only: the strict
   audit already refuses it (`ERROR: … hydro.river_timeseries trigger
   planted_hyper`, exit 3). On `_compressed_hypertable_N` it was, until this
   fix, invisible.

2. **`ADD CONSTRAINT … CHECK` is refused only because it validates immediately
   — `NOT VALID` gets around that**, so the `CHECK` form is plantable by the
   write role after all:

   ```
   $ psql -U nhms_ingest_rw -c "ALTER TABLE core.basin ADD CONSTRAINT planted_nv
                                CHECK (length(pg_read_file('/etc/hostname')) > 0)"
   ERROR:  permission denied for function pg_read_file      -- validation evaluates it AS THE WRITE ROLE
   $ psql -U nhms_ingest_rw -c "ALTER TABLE core.basin ADD CONSTRAINT planted_nv
                                CHECK (length(pg_read_file('/etc/hostname')) > 0) NOT VALID"
   ALTER TABLE                                              -- existing rows are not checked, so nothing is evaluated
   $ psql -U nhms -c "INSERT INTO core.basin (name) VALUES ('nv-probe')"
   INSERT 0 1                                               -- NEW rows ARE checked: evaluated as the superuser
   $ # strict audit-only
   ERROR:  SECURITY REGRESSION: ... core.basin CHECK constraint planted_nv references pg_catalog.pg_read_file the write role cannot execute ...
   EXIT=3
   ```

   `SET DEFAULT` needs no such trick — nothing evaluates it at DDL time. Both
   forms are caught by the same `pg_attrdef` / `pg_constraint` sweep legs.

3. **A trigger planted on `_compressed_hypertable_N` is executed by the
   superuser**, and `compress_chunk` is *not* the lane that does it:

   ```
   $ # planted by nhms_ingest_rw; body does INSERT INTO core.evidence
   $ #   VALUES (length(pg_read_file('/etc/hostname'))::text, current_user)
   $ psql -U nhms   -c "SELECT compress_chunk(...)"     -> no evidence row (compression writes below the row-trigger layer)
   $ psql -U nhms_ingest_rw -c "SELECT compress_chunk(...)"  -> no evidence row
   $ # what the replay lane does -- a superuser COPY into the compressed chunk:
   $ psql -U nhms -c "COPY _timescaledb_internal.compress_hyper_2_6_chunk TO '/tmp/cc.dat'"
   $ psql -U nhms -c "COPY _timescaledb_internal.compress_hyper_2_6_chunk FROM '/tmp/cc.dat'"
   COPY 1
   $ psql -U nhms -c "SELECT * FROM core.evidence"
    txt | who
   -----+------
    13  | nhms          -- length of /etc/hostname, read as the superuser
   ```

   And the six-schema audit was blind to all of it:

   ```
   $ # strict audit-only, BEFORE this fix, with both objects planted
   EXIT=0
    10 expression(s)/trigger(s) scanned, 9 distinct function(s) referenced, 0 not executable by nhms_ingest_rw
   ```

### (b) Owner-scoped extension into `_timescaledb_internal`

The inventory, the allow-list check and all four sweep legs now cover
`_timescaledb_internal` relations **owned by a write role**. Owner-scoped on
purpose: TimescaleDB's own catalog tables stay out of the scan.

**Clean seeded database (2 compressed + 4 uncompressed chunks), strict
audit-only:**

```
## audit: rules and non-internal triggers in the application schemas
 met.canonical_grid_cell     | trigger | canonical_grid_cell_direct_delete_blocked_trg
 met.canonical_grid_cell     | trigger | canonical_grid_cell_immutable_trg
 met.canonical_grid_snapshot | trigger | canonical_grid_snapshot_identity_immutable_trg
 met.canonical_met_product   | trigger | canonical_met_product_grid_definition_uri_match_trg
(4 rows)

## audit: function-privilege sweep over stored expressions
 15 expression(s)/trigger(s) scanned, 11 distinct function(s) referenced, 0 not executable by nhms_ingest_rw
(1 row)
EXIT=0
```

No TimescaleDB-internal function shows up as non-executable, and no internal
relation shows up in the rule/trigger inventory — the schema stays in.

**Same database after `nhms_ingest_rw` plants both objects, strict audit-only:**

```
 _timescaledb_internal._compressed_hypertable_2 | trigger | planted_comp
 _timescaledb_internal.compress_hyper_2_6_chunk | trigger | planted_comp
 _timescaledb_internal.compress_hyper_2_7_chunk | trigger | planted_comp
 met.canonical_grid_cell                        | trigger | canonical_grid_cell_direct_delete_blocked_trg
 ... (7 rows)

 21 expression(s)/trigger(s) scanned, 14 distinct function(s) referenced, 1 not executable by nhms_ingest_rw
 _timescaledb_internal._compressed_hypertable_2 column default river_id references pg_catalog.pg_read_file -- NOT executable by nhms_ingest_rw
 _timescaledb_internal.compress_hyper_2_6_chunk column default river_id references pg_catalog.pg_read_file -- NOT executable by nhms_ingest_rw
 _timescaledb_internal.compress_hyper_2_7_chunk column default river_id references pg_catalog.pg_read_file -- NOT executable by nhms_ingest_rw
(4 rows)

ERROR:  SECURITY REGRESSION: rule/trigger outside the migration allow-list (its body runs as the role that next writes the relation): _timescaledb_internal._compressed_hypertable_2 trigger planted_comp; _timescaledb_internal.compress_hyper_2_6_chunk trigger planted_comp; _timescaledb_internal.compress_hyper_2_7_chunk trigger planted_comp
EXIT=3
```

`strict_audit=off` degrades both legs to `WARNING` and exit 0, as everywhere
else.

Key result: the two legs are complementary and both are needed. The planted
trigger points at a **PUBLIC-executable** function, so the function-privilege
sweep alone would never have flagged it — the rule/trigger inventory is what
catches it; and the planted `DEFAULT` is not a rule or a trigger, so only the
sweep catches that one.

Residual after this section, measured not argued: a trigger planted on an
application-schema **hypertable** is still not *prevented* (TimescaleDB does not
let the event trigger see it) — it is only detected. Closing that would mean
either TimescaleDB firing event triggers for hypertable DDL, or removing the
superuser-write half, both out of scope here.

## Limits of this oracle (do not read more into it than it proves)

- These are **privilege-shape primitives**, not real component runs. The
  `timescale/timescaledb:2.10.2-pg15` image ships no PostGIS, so `packages/common/migrate.py`
  cannot apply the real schema and no lane (`autopipe`, compression, retention,
  cold-residency) can execute against this container. T1's "local container runs of each
  component under the candidate role" is therefore **not** closed here; the per-component
  runs are T7 post-merge on node-27, per design D5.
- For the same reason the four `met` triggers in §10–§11 are **stand-ins**: same
  schema/table/trigger names as `db/migrations/000043_canonical_grid_snapshot.sql`,
  trivial bodies. That the allow-list matches the real migration is pinned statically
  by `test_the_trigger_allow_list_is_exactly_what_the_migrations_create`.
- The retry/exhaustion path of the ownership loop is not exercised here (nothing contends
  for the locks in a single-client container). It is covered by the fake-`docker` shell
  test in `tests/test_node27_write_roles.py`.
- `flood` was absent, so its branch is proven only as "tolerated", not as "transferred".
- The live escalation-surface sweep (`public` schema ACL, `SECURITY DEFINER`
  functions, the real rule/trigger inventory of a PostGIS-bearing catalog) is a T7
  node-27 step; this container cannot stand in for it.

---

## Appendix A — password never reaches the server log (container `nwm-probe-1774-log`)

Follow-up probe after review flagged that `ALTER ROLE ... PASSWORD` is logged
**verbatim** by the server under `log_statement=ddl|mod|all`. The repo/argv
paths were already clean (env var forwarded by name via `docker exec -e VAR`);
the server log was not.

Container deliberately started with the logging that leaks:

```
docker run -d --name nwm-probe-1774-log ... timescale/timescaledb:2.10.2-pg15 \
  -c log_statement=ddl -c logging_collector=off
$ SHOW log_statement;
ddl
```

Ran the runner with both password env vars set to canary values:

```
NODE27_WRITE_ROLES_CONTAINER=nwm-probe-1774-log \
NODE27_INGEST_RW_PASSWORD='s3cr3t-ingest-DO-NOT-LOG' \
NODE27_DOWNLOAD_RW_PASSWORD='s3cr3t-download-DO-NOT-LOG' \
  bash scripts/node27_provision_write_roles.sh --roles-only
```

Control (the same statement WITHOUT the suppression, issued by hand):

```
$ psql -c "ALTER ROLE nhms_download_rw PASSWORD 'CONTROL-LEAK-CANARY';"
$ docker logs nwm-probe-1774-log | grep -c 'CONTROL-LEAK-CANARY'
1
2026-09-02 12:46:06.969 UTC [126] LOG:  statement: ALTER ROLE nhms_download_rw PASSWORD 'CONTROL-LEAK-CANARY';
```

Runner-set passwords:

```
$ docker logs nwm-probe-1774-log | grep -c 'DO-NOT-LOG'
0
```

So the leak is real and the `SET log_statement = 'none'` / `SET
log_min_duration_statement = -1` window in `db/roles/node27_write_roles.sql`
closes it. The only `ALTER ROLE` the log captured across the whole run is the
hand-issued control.

Session GUCs are session-scoped and restored — a fresh session still reports the
cluster settings:

```
$ SHOW log_statement;                -> ddl
$ SHOW log_min_duration_statement;   -> -1
```

Re-run is still idempotent and the password actually took effect:

```
$ ... --roles-only ; echo exit=$?
exit=0
$ PGPASSWORD='s3cr3t-ingest-DO-NOT-LOG' psql -U nhms_ingest_rw -h 127.0.0.1 -d nhms \
    -c "SELECT current_user, usesuper FROM pg_user WHERE usename=current_user;"
nhms_ingest_rw|f
```

Residual, recorded not fixed: `log_min_error_statement` (default `error`) still
logs the statement if the `ALTER ROLE` itself FAILS. A failed password set must
be treated as a credential to rotate. Container removed (`docker rm -f
nwm-probe-1774-log`).

## Appendix B — superuser-gated READS in the converted lanes (static audit)

Writes fail loudly for a non-owner; superuser-gated **reads** do not.
`pg_locks` is **not** row-filtered — every role sees every lock row. What
degrades is `pg_stat_activity`: it keeps one row per backend for every role but
**masks the `query` / `state` / `wait_event*` columns of other users' sessions**
unless the role holds `pg_read_all_stats`, so a quiescence or lock-conflict
guard running as `nhms_ingest_rw` reads NULL where it expected the other lane's
statement text and goes permanently green instead of erroring. `pg_locks` stays
in the scan pattern because it is only ever useful joined back to that masked
view. Scanned the converted lanes for
`pg_stat_activity|pg_locks|pg_toast.|pg_stat_file|pg_ls_dir|pg_read_file|
pg_read_binary_file|pg_terminate_backend|pg_cancel_backend|pg_reload_conf`.

Result: **no executed hit.** Every match in the converted lanes is a `#` comment
(the #1714 `fallback_application_name` attribution notes, e.g.
`scripts/node27_timeseries_compression.py:138`). The live callers of these
surfaces all sit outside the conversion:

| caller | surface | why unaffected |
|---|---|---|
| `scripts/node27_timeseries_compression_supervisor.py:1281,1311` | `pg_stat_activity`, `pg_locks` | replay lane -> `node27-timeseries-compression-replay.example`, allow-listed superuser |
| `scripts/node27_timeseries_compression_capture.py:338-340` | `pg_stat_activity`, `pg_locks` | same replay lane |
| `packages/common/node27_cold_governance_collection.py:246` | `pg_stat_activity` | `scripts/node27_resource_governance.py`, own template, not converted |
| `scripts/node27_external_contract_snapshot.py:115,117` | `pg_stat_activity` | one-off contract snapshot, no converted template |
| `scripts/node27_river_identity_backfill.py:289` | `pg_locks` | one-off backfill, no converted template |

`pg_toast` specifically: `packages/common/compressed_chunk_cold_residency.py`
models TOAST members (`toast_heap` / `toast_index` kinds) but never emits SQL
naming them. `_lock_sql` is fed by `lockable_heaps()`, which filters
`relkind == "r"` (TOAST heaps are `'t'`); `_move_sql` is fed by
`origin_shell_members()`, which keeps only `origin_heap` and indexes whose
`heap_oid` is the origin. TOAST relocation happens implicitly in the
`decompress_chunk` / `compress_chunk` rewrite. The one direct
`ALTER TABLE pg_toast.… SET TABLESPACE`
(`packages/common/compressed_chunk_cold_probe/scenarios.py:80-83`) belongs to
the disposable-cluster probe, which builds its own container and connects as
that cluster's superuser. So **no `GRANT USAGE ON SCHEMA pg_toast` and no
`pg_read_all_stats` is needed**, and neither was added.

This audit is pinned by
`tests/test_node27_write_roles.py::test_converted_lanes_do_not_read_superuser_gated_catalogs`,
so a future edit that adds such a read to a converted lane fails the suite.
