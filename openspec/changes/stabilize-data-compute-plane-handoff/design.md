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

## Issue #641 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- Object-store handoff schema and payload manifest for forcing-domain readiness.
- Display readiness table field names for `met.forcing_version`,
  `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`.
- File IO/path safety for package URIs and payload refs; no traversal, sibling
  package reads, or broad root scans.
- Bounded package/run discovery scoped to one declared package/run identity.
- Hydro-met time-series forcing windows: `cycle_time`, `start_time`, `end_time`,
  valid-time coverage, units, and source identity.
- PostGIS/Timescale table identity and row-count evidence without DB writes in
  #641.
- Run manifest/QC provenance fields that existing node-27 ingest and mirror
  scripts already consume.
- Published display identity for run/model/basin/source/cycle readiness.

Change surface:
- Object-store forcing-domain handoff contract files, JSON schema/docs, and
  complete/incomplete test fixtures.
- Existing producers/consumers are inspected only for field inventory; runtime
  parser, DB apply, and autopipeline behavior remain out of scope for #641.

Must preserve:
- Existing `runs/<run_id>/input/manifest.json` field availability used by
  `scripts/node27_ingest_run.py`, `scripts/node27_mirror_forcing.py`, and
  `scripts/node27_autopipeline.py`: `run_id`, `source_id`/`source`,
  `cycle_time`, `start_time`, `end_time`, `model_id`, `basin_id`,
  `basin_version_id`, `model_package_uri`, `forcing_version_id`,
  `forcing_uri`/`forcing_package_uri`, `scenario_id`, `run_manifest_uri`, and
  `output_uri`.
- Display readiness table identities for `met.forcing_version`,
  `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`.

Must add/change:
- A named object-store forcing-domain handoff contract with exact identity keys,
  temporal bounds, payload references, checksums, station count, and per-table
  row-count evidence.
- Complete and incomplete fixtures that later parser/apply issues can consume
  without changing autopipeline control flow.

Selected risk packs:
- Schema / columns / units / field names: selected - #641 defines the contract
  keys and table coverage later issues must implement.
- Evidence / JSON / Schema Ingestion: selected - fixtures must bind payloads to
  checksums and stable missing/mismatch reasons.
- Resource limits / large input / discovery: selected - fixture discovery must be
  scoped to a package/run, not broad object-store roots.
- File IO / path safety / overwrite: selected - object-store URIs and fixture
  payload paths are contract inputs; no runtime writes are introduced in #641.
- Documentation / migration notes: selected - this issue records the handoff
  contract and compatibility boundary for later implementation.
- Public API / CLI / script entry: not selected - no entrypoint behavior changes.
- Config / project setup: not selected - no env or deployment config changes.
- Auth / permissions / secrets: not selected - no credentials or auth boundary
  changes; fixtures must still avoid secret-like values.
- Concurrency / shared state / ordering: not selected - no runtime state machine
  changes in #641.
- Legacy compatibility / examples: selected - existing manifest identity fields
  and transitional mirror assumptions must remain understandable.
- Error handling / rollback / partial outputs: selected - incomplete fixture
  reasons become the oracle for later stable failures.
- Release / packaging / dependency compatibility: not selected - no dependency or
  package release behavior changes.

Domain risk packs:
- Geospatial / CRS / basin geometry: not selected - #641 preserves basin/model
  identifiers only and does not parse geometry, CRS, shapefiles, or PostGIS
  geometry payloads.
- Hydro-met time series / forcing windows: selected - station-timeseries payloads
  must identify source/cycle/start/end/valid-time/unit coverage.
- SHUD numerical runtime / conservation / NaN: not selected - #641 does not run
  SHUD, alter numerical outputs, or inspect conservation/runtime behavior.
- PostGIS / TimescaleDB domain behavior: selected - table names and row-count
  evidence target PostGIS/Timescale-backed schemas even though #641 does not
  write DB rows.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler,
  sbatch, or production job lifecycle behavior changes.
- External hydro-met providers / snapshot reproducibility: not selected - #641
  records provider/source/cycle identity but does not fetch or compare upstream
  GFS/IFS/ERA5/provider snapshots.
- Run manifest / QC provenance: selected - the handoff contract binds manifests,
  payload checksums, and readiness evidence.
- Published NHMS artifacts / display identity: selected - node-27 display
  readiness depends on exact model/basin/run/source/cycle identities.

Invariant Matrix
Governing invariant: A contracted forcing-domain handoff package must identify
exactly one run/model/basin/source/cycle and bind every readiness payload to a
checksum and expected row-count evidence, or be rejected by a stable reason.
Source-of-truth identity/contract: `run_id`, `source_id`, `cycle_time`,
`start_time`, `end_time`, `model_id`, `basin_id`, `basin_version_id`,
`forcing_version_id`, package URI/checksum, and payload URI/checksum pairs for
station inventory, station timeseries, and interpolation weights.
Surfaces:
- Producers: object-store package contract fixtures and existing run/forcing
  manifest examples.
- Validators/preflight: schema/fixture validation tests added by #641.
- Storage/cache/query: no DB writes in #641; table identities and row-count
  expectations are contract-only.
- Public routes/entrypoints: none - no API/CLI behavior changes.
- Frontend/downstream consumers: display readiness semantics only; no frontend
  behavior changes.
- Failure paths/rollback/stale state: incomplete/malformed/checksum-mismatch
  fixtures must yield stable unavailable reasons.
- Evidence/audit/readiness: fixture contract docs/schema and tests.
Regression rows:
- Complete package fixture -> validates required identities, payload checksums,
  station count, and table row-count evidence.
- Missing payload/checksum/identity/temporal field fixture -> stable unavailable
  reason naming the missing component without secrets.
- Traversal or sibling package payload URI fixture -> rejected without broad root
  scan or object-store escape.
- Existing run manifest identity consumer -> unchanged field availability for
  `node27_ingest_run.py` / `node27_mirror_forcing.py` inventory assumptions.
- Validation helper output -> no writes and no credential-like values in returned
  unavailable reasons.

Required evidence:
- Complete fixture input path -> validated identity/checksum/row-count output.
- Missing payload/checksum/temporal field -> named stable unavailable reason.
- Traversal/sibling package URI -> rejected without broad root scan.
- Validation helper -> no writes and no credential-like values in output.
- `openspec validate stabilize-data-compute-plane-handoff --strict --no-interactive`.
- `git diff --check`.

Non-goals:
- No object-store parser public interface beyond contract/fixture helpers.
- No DB apply/upsert path.
- No `node27_autopipeline.py` behavior change.
- No node-27 live receipt; #648 owns live evidence.

Review focus:
- The contract is exact enough for #643/#644 without re-interpreting fields.
- Fixtures are bounded and credential-safe.
- No runtime behavior or autopipeline control flow sneaks into #641.
