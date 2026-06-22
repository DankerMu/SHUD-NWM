## Context

The verified 2026-06-22 production topology is:

- node-22 runs Slurm/SHUD compute and writes shared NFS object-store artifacts.
- node-27 hosts active PostgreSQL `:55432`, cron-driven ingest, display API, and
  public frontend.
- node-27 ingest scans `/home/ghdc/nwm/object-store/runs`, seeds missing basin
  registries, registers/mirrors/parses runs, writes node-27 PostgreSQL, and
  refreshes display coverage.
- node-27 display API runs as `display_readonly` and must not expose compute
  mutations or writer semantics.

The remaining gap is not a single endpoint bug. It is an incomplete handoff
contract between compute artifacts and data-plane ingestion. The clearest code
symptom is `scripts/node27_mirror_forcing.py`: it still treats node-22 DB rows
as the authoritative source for forcing-domain metadata and can fall back to
`infra/env/display.env`, even though node-22's local DB is historical and
display env belongs to the read-only display runtime.

## Goals / Non-Goals

**Goals:**

- Make object-store artifacts the canonical handoff between node-22 compute and
  node-27 data-plane ingest for forcing-domain data.
- Keep the transitional node-22 mirror safe until the object-store importer is
  complete: explicit DSN only, no display-env fallback, structured skip/fail
  reasons, and no silent use of the historical node-22 DB.
- Give node-27 ingest its own operational contract: env, wrapper preflight before
  seed/import/activate/backfill/register/mirror/parse/coverage/publish writes,
  role label, logging/evidence, and tests.
- Add guardrails that prevent active docs and scripts from reintroducing
  node-22-writer or display-env-writer assumptions.

**Non-Goals:**

- No change to Slurm scheduling semantics or SHUD compute behavior.
- No expansion of display API write permissions.
- No frontend feature work.
- No attempt to remove historical documents that are explicitly marked as
  historical/governance evidence.

## Decisions

1. **Object-store forcing-domain handoff becomes canonical.** Node-22 may compute
   and publish artifacts, but node-27 ingest must be able to reconstruct
   `met.forcing_version`, `met.met_station`, `met.forcing_station_timeseries`,
   and `met.interp_weight` from object-store package material and manifests.
   This matches the existing shared NFS boundary and removes active DB coupling
   from node-22. The node-22 local PostgreSQL `:55433` process is historical,
   do-not-connect production state and must have a removal/sunset path rather
   than becoming a permanent compatibility dependency.

2. **The node-22 DB mirror remains transitional and explicit only.** Until the
   importer covers every live forcing package, mirror mode may remain as a
   controlled fallback. It must require `--node22-url` or `N22_DSN`, must never
   read `infra/env/display.env`, and must emit a stable unavailable reason when
   no explicit mirror DSN is configured.

3. **Node-27 ingest is a data-plane writer role, not display_readonly.** The
   cron wrapper and scripts should advertise a distinct ingest role and preflight
   writer/object-store/Basins env before doing any seed/import/activate/backfill
   or per-run work. The display API keeps read-only DB credentials and no Slurm
   routes.

4. **Topology truth is enforced at the edges.** Runbooks and role-boundary docs
   are necessary but insufficient. Static checks should flag active references
   that describe node-22 as an active NHMS DB writer or reuse display env for
   writer/mirror jobs, while allowing clearly historical evidence.

## Risks / Trade-offs

- Object-store packages may not yet contain every field needed by the importer.
  Mitigation: first inventory the exact missing fields and make the handoff
  manifest explicit before changing ingestion behavior.
- Cutting off the implicit mirror fallback too early could reduce visible
  forcing products. Mitigation: keep an explicit transitional mirror mode with a
  stable skip result and live receipts for qhh/heihe before declaring completion.
- Adding a new ingest env could drift from display env or host cron reality.
  Mitigation: source it from a committed template, add preflight tests, and
  include node-27 live evidence in the final issue.

## Migration Plan

1. Define and validate the object-store forcing-domain manifest contract.
2. Harden the existing mirror path so unsafe fallback is removed before deeper
   importer work begins.
3. Implement the object-store importer and prefer it in node-27 autopipeline;
   keep explicit mirror fallback only for runs whose handoff package predates
   the contract.
4. Split node-27 ingest env/wrapper semantics from display runtime env.
5. Update topology docs and static drift checks, then capture node-27 live
   receipts proving display remains read-only while ingest writes active DB
   state.

Rollback is by reverting the ingest preference to explicit mirror mode only; the
rollback must still preserve the no-display-env-fallback invariant.
