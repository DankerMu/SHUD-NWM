## Context

The verified current deployment has node-27 hosting active PostgreSQL `:55432`,
cron-driven ingest, display API, frontend, and public `https://test.nwm.ac.cn`.
Node-22 hosts Slurm Gateway, SHUD/forcing compute capability, and shared NFS
artifact production. It should not be an active NHMS DB writer, but the current
runtime still includes a historical node-22 PostgreSQL `:55433` dependency in
the scheduler path.

Moving downloads to node-27 changes the right control-plane boundary. If
download writes node-27 DB while downstream scheduler state remains in node-22
DB, the system keeps two competing state sources. Therefore this change treats
download migration and node-22 DB retirement as one governed migration.

## Goals

- Run GFS/IFS discovery and download on node-27 using a data-plane writer role.
- Keep display runtime read-only and separate from download/ingest writer env.
- Make raw source-cycle status and manifest identity live in node-27 DB.
- Remove business `DATABASE_URL` from node-22 Slurm/SHUD compute jobs.
- Retire node-22 historical PostgreSQL only after node-27 proves end-to-end
  download -> compute handoff -> ingest -> display for live cycles.

## Non-Goals

- No frontend feature work.
- No expansion of display API mutation authority.
- No immediate deletion of historical node-22 DB data before archival.
- No return-period quality fix; degraded return-period products remain separate.

## Decisions

1. **Node-27 owns downloads.** GFS/IFS download is a data-plane writer task
   because the adapters persist `met.forecast_cycle` and raw manifest identity.
   It belongs beside node-27 ingest, not beside display_readonly and not inside
   node-22 historical DB state.

2. **Node-22 becomes DB-free compute.** Slurm jobs may receive artifact identity,
   object-store paths, and receipt paths. They must not receive active business
   DB credentials. Any step that needs DB mutation either runs on node-27 or
   writes an object-store receipt for node-27 to apply.

3. **Retirement is gated by live cycles.** The historical node-22 PostgreSQL
   process is stopped only after the node-27 path has advanced at least two
   production cycles covering GFS and IFS evidence, and after an archive/dump is
   recorded.

4. **Rollback keeps topology truth intact.** A temporary rollback may re-enable
   the previous 22 path while the new path is fixed, but docs and guardrails
   must still mark node-22 DB as historical and sunset-bound rather than current
   target architecture.

## Migration Plan

1. Capture current live evidence and freeze active dependency facts.
2. Add node-27 download preflight and bounded runner.
3. Promote node-27 download to production source-cycle ownership.
4. Convert node-22 compute jobs to DB-free artifact producers.
5. Move orchestration state to node-27 and submit compute through node-22
   Slurm Gateway.
6. Archive and stop node-22 historical PostgreSQL; add guardrails and receipts.

## Risks

- Node-27 may lack GRIB dependencies or network behavior matching node-22.
- Download load could interfere with display/API without bounded scheduling.
- Existing 22 scheduler jobs may still be running during cutover.
- Pre-contract run packages may still need explicit transitional mirror handling.

## Rollback

Rollback is phase-bounded. Before Phase 5, node-22 PG remains available as an
emergency fallback. After Phase 5, rollback requires restoring the archived DB
only as a time-limited emergency path and recording the blocker that prevented
node-27 ownership from holding.

