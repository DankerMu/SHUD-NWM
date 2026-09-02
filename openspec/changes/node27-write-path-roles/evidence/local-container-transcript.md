# Local disposable-container transcript — issue #1774 (T2)

Oracle: a **fresh** `timescale/timescaledb:2.10.2-pg15` container (PG 15.2 / TSDB 2.10.2,
the node-27 versions), created and destroyed for this run:

```
docker run -d --name nwm-probe-1774-impl -e POSTGRES_PASSWORD=x \
  -p 127.0.0.1:55499:5432 timescale/timescaledb:2.10.2-pg15
$ docker exec nwm-probe-1774-impl psql -U postgres -tAc "select version()"
PostgreSQL 15.2 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 12.2.1_git20220924-r4) 12.2.1 20220924, 64-bit
```

No real secret appears below: the container is disposable and the two probe passwords
(`ingest-probe-pw` / `download-probe-pw`) exist only inside it. It was removed with
`docker rm -f nwm-probe-1774-impl` at the end.

## 0. Fixture: the node-27 shape

Created by the **superuser `nhms`**, exactly as the migrations do on the live box —
`nhms` superuser + `nhms_display_ro` readonly + database `nhms`; schemas `core`,
`hydro`, `met`, `ops`, `map`; the two compression-capable hypertables
(`hydro.river_timeseries`, `met.forcing_station_timeseries`); three authority tables
with identity sequences; one standalone sequence; one view; one materialized view;
`GRANT SELECT` to `nhms_display_ro` on all of them.

`flood` is deliberately **not** created — it is provisioned outside `db/` on node-27,
and its absence has to be tolerated rather than fatal. Neither is `nhms_cold` (until
§6): the tablespace is a one-time superuser install (#1894).

## 1. `--roles-only` — the additive, pre-merge phase

```
$ NODE27_WRITE_ROLES_CONTAINER=nwm-probe-1774-impl \
  NODE27_INGEST_RW_PASSWORD='ingest-probe-pw' NODE27_DOWNLOAD_RW_PASSWORD='download-probe-pw' \
  bash scripts/node27_provision_write_roles.sh --roles-only

node27-write-roles: container=nwm-probe-1774-impl database=nhms superuser=nhms
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
## audit: write-role flags (all five privilege flags must be false)
     rolname      | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin
------------------+----------+---------------+-------------+----------------+--------------+-------------
 nhms             | t        | f             | f           | f              | f            | t
 nhms_display_ro  | f        | f             | f           | f              | f            | t
 nhms_download_rw | f        | f             | f           | f              | f            | t
 nhms_ingest_rw   | f        | f             | f           | f              | f            | t
(4 rows)
...
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
## audit: non-strict (--roles-only): owner drift above is expected, ownership is transferred post-merge
node27-write-roles: roles-only phase complete; ownership transfer deferred to the post-merge run
EXIT=0
```

**Key results.** Both roles created with all five privilege flags false; the absent
`flood` schema and the absent `nhms_cold` tablespace were tolerated (the run is clean);
**every one of the 11 relations is still owned by `nhms`** — the mode really is
additive, it executes no `ALTER … OWNER TO`; both `COPY … FROM PROGRAM` probes were
refused; neither password appears anywhere in the output.

## 2. Full mode — the ownership transfer

```
$ NODE27_WRITE_ROLES_CONTAINER=nwm-probe-1774-impl bash scripts/node27_provision_write_roles.sh
node27-write-roles: NODE27_INGEST_RW_PASSWORD unset -- that role's password will be left unchanged
node27-write-roles: NODE27_DOWNLOAD_RW_PASSWORD unset -- that role's password will be left unchanged
node27-write-roles: mode=full (additive phase + ownership transfer)
node27-write-roles: captured 7 display-visible relation(s) before the transfer
node27-write-roles: ownership pass 1/5
...
## phase: ownership transfer of the application schemas to nhms_ingest_rw
node27-write-roles: after pass 1: 0 relation(s) still not owned by nhms_ingest_rw
node27-write-roles: relacl diff across the transfer (informational -- ALTER ... OWNER TO rewrites grantor references):
-core.basin | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms
+core.basin | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw
-met.forcing_station_timeseries | r | nhms | nhms=arwdDxt/nhms nhms_display_ro=r/nhms nhms_ingest_rw=arwd/nhms nhms_download_rw=arwd/nhms
+met.forcing_station_timeseries | r | nhms_ingest_rw | nhms_ingest_rw=arwdDxt/nhms_ingest_rw nhms_display_ro=r/nhms_ingest_rw nhms_download_rw=arwd/nhms_ingest_rw
   (… all 11 relations, same shape …)
node27-write-roles: nhms_display_ro effective SELECT set diff (must be empty):
  (identical -- read-side boundary preserved)
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
## audit: OK -- no owner drift
node27-write-roles: full provision complete; audit clean
EXIT=0
```

**Key results.** The `relacl` diff is exactly what D4 predicted — the *grantor*
references were rewritten `…/nhms` → `…/nhms_ingest_rw`, which is why the display
boundary is asserted as an **effective SELECT set** (unchanged, 7 relations) and not as
ACL text. `nhms_display_ro=r/…` survives on every relation. Both hypertables changed
owner; chunk ownership followed (§4).

## 3. Idempotence (spec: "Provision is idempotent")

```
$ bash scripts/node27_provision_write_roles.sh   # run 2 -> EXIT=0
$ bash scripts/node27_provision_write_roles.sh   # run 3 -> EXIT=0
$ diff -u <(audit section of run 2) <(audit section of run 3)
IDEMPOTENT: audit output byte-identical across runs 2 and 3
$ sed -n '/relacl diff/,/display_ro effective/p' run2
node27-write-roles: relacl diff across the transfer (informational …):
  (no relacl change)
```

## 4. `nhms_ingest_rw` as a real login (not `SET ROLE` from a superuser session)

```
$ docker exec -i nwm-probe-1774-impl psql -U nhms_ingest_rw -d nhms -X < probe_ingest.sql
  session_user  |  current_user  | usesuper
----------------+----------------+----------
 nhms_ingest_rw | nhms_ingest_rw | f

--- compress_chunk on the oldest hydro.river_timeseries chunk ---
 _timescaledb_internal._hyper_1_1_chunk
--- decompress_chunk, then compress again ---
 _timescaledb_internal._hyper_1_1_chunk
 _timescaledb_internal._hyper_1_1_chunk
--- chunks_detailed_size ---
    chunk_name    | total_bytes
------------------+-------------
 _hyper_1_1_chunk |       49152
 _hyper_1_2_chunk |       32768
--- drop_chunks older than 2026-01-02 ---
 _timescaledb_internal._hyper_1_1_chunk
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
(`_hyper_1_11_chunk`) and the compressed hypertables:

```
         relname          |     owner
--------------------------+----------------
 _compressed_hypertable_2 | nhms_ingest_rw
 _compressed_hypertable_4 | nhms_ingest_rw
 _hyper_1_11_chunk        | nhms_ingest_rw
 _hyper_1_2_chunk         | nhms_ingest_rw
 _hyper_1_3_chunk         | nhms_ingest_rw
 _hyper_1_4_chunk         | nhms_ingest_rw
```

### Both stats-guard ANALYZE legs, no `WARNING: skipping`

```
$ docker exec -i nwm-probe-1774-impl psql -U nhms_ingest_rw -d nhms -X < probe_analyze.sql 2>&1
--- stats-guard leg 1 (#1643): ANALYZE an UNCOMPRESSED frontier chunk ---
ANALYZE
--- stats-guard leg 2 (#1468): ANALYZE an ordinary authority table ---
ANALYZE
ANALYZE
$ # read back from a LATER transaction (as nhms):
      schemaname       |     relname      | analyzed
-----------------------+------------------+----------
 _timescaledb_internal | _hyper_1_2_chunk | t
 core                  | basin            | t
 met                   | met_station      | t
```

Standard output **and** standard error were captured together; no
`WARNING: skipping … only table or database owner can analyze it` was emitted, and
`last_analyze` is non-NULL for both legs. (An *uncompressed* chunk is analyzed on
purpose: ANALYZE on a bare compressed-chunk name zeroes the relstats TimescaleDB
preserved at compression time, which is why the autopipe guard excludes them, #1378 D3.)

## 5. `nhms_download_rw` as a real login

```
$ docker exec -i nwm-probe-1774-impl psql -U nhms_download_rw -d nhms -X < probe_download.sql
   session_user   |   current_user   | usesuper
------------------+------------------+----------
 nhms_download_rw | nhms_download_rw | f
--- privilege-shape INSERT into a met.* table (DML grant) ---
INSERT 0 1
INSERT 0 1
--- negative: compress_chunk (not the owner) ---
ERROR:  must be owner of hypertable "forcing_station_timeseries"
--- negative: DML outside met (no grant) ---
ERROR:  permission denied for schema ops
--- negative: COPY ... FROM PROGRAM ---
CREATE TABLE
ERROR:  must be superuser or have privileges of the pg_execute_server_program role to COPY to or from an external program
```

## 6. Cold residency: `SET TABLESPACE` needs, and gets, the conditional grant

Before the tablespace existed, every run above was clean — the conditional
`GRANT CREATE ON TABLESPACE` simply produced no statement. After
`CREATE TABLESPACE nhms_cold` (superuser, one-time, #1894):

```
$ docker exec -i … psql -U nhms_ingest_rw -c "ALTER TABLE _timescaledb_internal._hyper_1_2_chunk SET TABLESPACE nhms_cold;"
ERROR:  permission denied for tablespace nhms_cold          <- before re-provision

$ bash scripts/node27_provision_write_roles.sh                # re-run -> EXIT=0
$ psql -tAc "SELECT spcname, array_to_string(spcacl,' ') FROM pg_tablespace WHERE spcname='nhms_cold';"
nhms_cold|nhms=C/nhms nhms_ingest_rw=C/nhms

$ docker exec -i … psql -U nhms_ingest_rw \
    -c "SELECT compress_chunk('_timescaledb_internal._hyper_1_3_chunk')::text;" \
    -c "ALTER TABLE _timescaledb_internal._hyper_1_2_chunk SET TABLESPACE nhms_cold;"
 _timescaledb_internal._hyper_1_3_chunk
ALTER TABLE
$ psql -c "SELECT c.relname, t.spcname FROM pg_class c LEFT JOIN pg_tablespace t ON t.oid=c.reltablespace WHERE c.relname='_hyper_1_2_chunk';"
     relname      |  spcname
------------------+-----------
 _hyper_1_2_chunk | nhms_cold
```

## 7. Drift regression row (spec: "Migration-added tables stay usable before re-provision")

```
$ psql -U nhms -c "CREATE TABLE hydro.newtab(x int); INSERT INTO hydro.newtab SELECT generate_series(1,500);"
$ psql -U nhms_ingest_rw -c "INSERT INTO hydro.newtab VALUES (1);" -c 'ANALYZE "hydro"."newtab";'
INSERT 0 1                                   <- default privileges keep the lane writable
ANALYZE
WARNING:  skipping "newtab" --- only table or database owner can analyze it   <- only ANALYZE degrades

# audit reports the drift and --roles-only stays exit 0 (additive phase):
$ bash scripts/node27_provision_write_roles.sh --roles-only     # EXIT=0
## audit: owner drift -- relations NOT owned by nhms_ingest_rw
   relation    | relkind | owner
---------------+---------+-------
 hydro.newtab2 | r       | nhms
(1 row)
## audit: non-strict (--roles-only): owner drift above is expected, ownership is transferred post-merge

# the STRICT audit (what the full runner runs, and what turns into exit 3) refuses:
$ psql -v ON_ERROR_STOP=1 -v do_roles=off -v do_ownership=off -v do_audit=on -v strict_audit=on \
    < db/roles/node27_write_roles.sql
ERROR:  owner drift: 1 application relation(s) not owned by nhms_ingest_rw (listed above); re-run the provision script
psql EXIT=3

# re-running the full provision converges it:
$ bash scripts/node27_provision_write_roles.sh                  # EXIT=0
node27-write-roles: full provision complete; audit clean
$ psql -U nhms_ingest_rw -c 'ANALYZE "hydro"."newtab2";'
ANALYZE                                       <- no warning; the leg is alive again
```

## 8. Final catalog state

```
     rolname      | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin
------------------+----------+---------------+-------------+----------------+--------------+-------------
 nhms             | t        | f             | f           | f              | f            | t
 nhms_display_ro  | f        | f             | f           | f              | f            | t
 nhms_download_rw | f        | f             | f           | f              | f            | t
 nhms_ingest_rw   | f        | f             | f           | f              | f            | t
 postgres         | t        | t             | t           | t              | t            | t

 schema | relkind |     owner      | count
--------+---------+----------------+-------
 core   | S       | nhms_ingest_rw |     1
 core   | r       | nhms_ingest_rw |     1
 hydro  | r       | nhms_ingest_rw |     3
 map    | S       | nhms_ingest_rw |     1
 map    | m       | nhms_ingest_rw |     1
 map    | v       | nhms_ingest_rw |     1
 met    | S       | nhms_ingest_rw |     1
 met    | r       | nhms_ingest_rw |     2
 ops    | S       | nhms_ingest_rw |     1
 ops    | r       | nhms_ingest_rw |     1
```

Container removed: `docker rm -f nwm-probe-1774-impl`.

## Limits of this oracle (do not read more into it than it proves)

- These are **privilege-shape primitives**, not real component runs. The
  `timescale/timescaledb:2.10.2-pg15` image ships no PostGIS, so `packages/common/migrate.py`
  cannot apply the real schema and no lane (`autopipe`, compression, retention,
  cold-residency) can execute against this container. T1's "local container runs of each
  component under the candidate role" is therefore **not** closed here; the per-component
  runs are T7 post-merge on node-27, per design D5.
- The retry/exhaustion path of the ownership loop is not exercised here (nothing contends
  for the locks in a single-client container). It is covered by the fake-`docker` shell
  test in `tests/test_node27_write_roles.py`.
- `flood` was absent, so its branch is proven only as "tolerated", not as "transferred".

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

Writes fail loudly for a non-owner; superuser-gated **reads** do not —
`pg_stat_activity` / `pg_locks` return a row set filtered to the caller's own
sessions, so a quiescence or lock-conflict guard running as `nhms_ingest_rw`
would go permanently green instead of erroring. Scanned the converted lanes for
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
