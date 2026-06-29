## Context

The verified current deployment has node-27 hosting active PostgreSQL `:55432`,
cron-driven ingest, display API, frontend, and public `https://test.nwm.ac.cn`.
Node-22 hosts Slurm Gateway, SHUD/forcing compute capability, and shared NFS
artifact production. It also remains the production scheduler/control point for
Slurm/SHUD cycles.

Moving downloads to node-27 changes the source acquisition boundary, not the
scheduler boundary. Node-27 now writes raw GFS/IFS bundles and manifests to the
shared NFS object-store. Node-22 scheduler must treat those NFS manifests as the
download handoff and start downstream stages only after the raw files are
present and valid.

## Goals

- Run GFS/IFS discovery and download on node-27 using a data-plane writer role.
- Keep display runtime read-only and separate from download/ingest writer env.
- Make raw source-cycle status and manifest identity live in node-27 DB.
- Write raw bundles and manifests to the shared NFS object-store so node-22 can
  consume them without copying or polling node-27 internals.
- Keep node-22 scheduler active, but gate production cycles on NFS raw manifest
  readiness and restart from `convert` when canonical outputs are absent.
- Prevent node-22 from falling back to `download_source_cycle` when the required
  node-27 NFS manifest is missing or invalid.

## Non-Goals

- No frontend feature work.
- No expansion of display API mutation authority.
- No immediate migration of the scheduler/control plane to node-27.
- No immediate deletion of historical node-22 DB data before archival or a
  separately designed scheduler-state replacement.
- No return-period quality fix; degraded return-period products remain separate.

## Decisions

1. **Node-27 owns downloads.** GFS/IFS download is a data-plane writer task
   because the adapters persist `met.forecast_cycle` and raw manifest identity.
   It belongs beside node-27 ingest, not beside display_readonly and not inside
   node-22 historical DB state.

2. **NFS manifest is the handoff contract.** Node-27 writes raw files and
   `raw/<source>/<cycle>/manifest.json` to the shared NFS object-store. Node-22
   scheduler/control node validates that manifest, its source/cycle identity,
   URI suffix, entry list, and referenced local files before treating the source
   cycle as raw-ready.

3. **Compute-visible staging is required.** Slurm compute nodes cannot be
   assumed to read `/ghdc/data/nwm` even when the scheduler/control node can.
   Before submitting `convert`, node-22 stages the NFS raw files into the
   compute-visible `OBJECT_STORE_ROOT` and copies the manifest last.

4. **Scheduler remains on node-22.** When NFS raw is ready but canonical product
   rows are absent, the scheduler builds a downstream restart candidate at
   `convert` and disables fresh download for that cycle. Missing required NFS
   raw evidence blocks the candidate instead of submitting node-22 download.

5. **DB retirement is closed by #837.** Node-22 local PostgreSQL `:55433` is
   historical, archived, stopped, and outside current topology. Scheduler
   locks/job state no longer require scheduler DB access, and the retirement
   evidence lives in `docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`.

6. **Rollback keeps topology truth intact.** A temporary rollback may disable
   the node-22 NFS gate while the new path is fixed, but docs and guardrails
   must still mark node-27 as the download owner and node-22 download as a
   deprecated emergency fallback.

## Migration Plan

1. Capture current live evidence and freeze active dependency facts.
2. Add node-27 download preflight and bounded runner.
3. Add the node-22 scheduler NFS raw manifest bridge and required gate.
4. Add node-22 pre-submit staging from NFS raw to compute-visible object-store.
5. Promote node-27 download to production source-cycle ownership.
6. Observe live GFS/IFS cycles that use node-27 raw and node-22 downstream
   compute from the NFS handoff plus local staging.
7. Treat node-22 DB retirement and scheduler-state reduction as completed by
   `node22-db-free-scheduler-state` / #837; keep this change's remaining scope
   limited to node-27 download ownership and NFS raw handoff behavior.

## Risks

- Node-27 may lack GRIB dependencies or network behavior matching node-22.
- Download load could interfere with display/API without bounded scheduling.
- Existing 22 scheduler jobs may still be running during cutover.
- If the NFS raw manifest gate is enabled before node-27 cron is stable, 22 will
  correctly block rather than silently perform node-22 downloads.
- If the staging root is not compute-visible, downstream Slurm stages will fail
  even though scheduler NFS validation passed.
- Pre-contract run packages may still need explicit transitional mirror handling.

## Rollback

Rollback is phase-bounded. Before production gate enablement, disable the
node-27 download cron/wrapper and keep the previous node-22 download path. After
the required NFS gate is enabled, rollback must explicitly turn off
`NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST` and record why node-27 download did
not hold. Node-22 PostgreSQL is no longer an active rollback dependency; any
archive restore is a separate operator recovery path governed by the #837
retirement receipt and must not reconnect scheduler runtime to `:55433`.
