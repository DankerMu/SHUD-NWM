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
`forcing_version_id`, package directory URI, package manifest URI/checksum, and
payload URI/checksum pairs for station inventory, station timeseries, and
interpolation weights.
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

## Issue #643 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- Parser/reader public helper for forcing-domain handoff packages.
- File IO/path safety and bounded JSON payload reads from object-store material.
- Schema/field/unit/time-series payload interpretation for DB reconstruction.
- Hydro-met station timeseries windows and native-resolution lattice already
  validated by the #641 contract.
- Legacy compatibility with #641 validation evidence and future #644 DB apply
  input shape.

Change surface:
- `packages/common/forcing_domain_handoff.py` parser/read API and focused tests.
- Existing #641 validation behavior remains the gate; #643 may reuse it but
  must not weaken unavailable reasons or readiness evidence.

Must preserve:
- `validate_forcing_domain_handoff_path(...)` output shape and reason codes for
  complete, incomplete, unsafe, malformed, checksum-mismatch, and oversized
  cases.
- No `scripts/node27_autopipeline.py` behavior changes and no DB writes.

Must add/change:
- A parser/fixture reader that returns structured reconstruction data for
  `met.forcing_version`, `met.met_station`,
  `met.forcing_station_timeseries`, and `met.interp_weight`.
- The public parser helper must accept the same declared handoff manifest path
  and object-store root inputs as the validator, and return an envelope with
  `available`, `status`, `unavailable_reasons`, `evidence`, and `parsed` fields.
- The parser result must include source/cycle/run/model/basin/forcing identity,
  handoff manifest URI/checksum, canonical forcing package manifest checksum,
  payload checksums, station count, and expected per-table row counts.
- `parsed.met.forcing_version[0]` must include `forcing_version_id`,
  `source_id`, `cycle_time`, `start_time`, `end_time`, `basin_id`,
  `basin_version_id`, `model_id`, `station_count`, `forcing_package_uri`,
  `forcing_package_manifest_uri`, and `checksum`, where `checksum` is populated
  from `forcing_package_manifest_checksum_sha256`.
- `parsed.met.met_station[*]` must preserve station payload fields including
  `station_id`, `basin_version_id`, `station_name`, `elevation_m`,
  `station_role`, `active_flag`, `properties_json`, and coordinate evidence as
  either `longitude` plus `latitude` or `geometry`. If optional `geometry` is
  present alongside longitude/latitude, the parser must preserve all declared
  coordinate evidence.
- `parsed.met.forcing_station_timeseries[*]` must preserve
  `forcing_version_id`, `basin_version_id`, `station_id`, `valid_time`,
  `source_id`, `variable`, `value`, `unit`, `native_resolution`, and
  `quality_flag`.
- `parsed.met.interp_weight[*]` must preserve `source_id`, `grid_id`,
  `model_id`, `station_id`, `variable`, `grid_cell_id`, `weight`, `method`, and
  optional `grid_signature`.
- Unavailable parse outcomes must be credential-safe and stable; incomplete,
  malformed, missing-payload, and checksum-mismatch inputs must not expose
  readiness-style parsed rows.

Selected risk packs:
- Public API / CLI / script entry: selected - #643 introduces a shared parser
  function consumed by #644, though no CLI/autopipeline entrypoint changes.
- File IO / path safety / overwrite: selected - parser reads manifest and
  payload paths from object-store metadata; no writes are allowed.
- Evidence / JSON / Schema Ingestion: selected - parser trusts only validated
  manifest/payload bytes and must preserve stable unavailable reasons.
- Resource limits / large input / discovery: selected - parser must keep #641
  bounded reads and must not scan broad object-store roots.
- Schema / columns / units / field names: selected - parsed rows become the
  #644 DB apply contract for `met.*` tables.
- Hydro-met time series / forcing windows: selected - station-timeseries rows
  must preserve valid_time, variable, unit, value, and native_resolution.
- PostGIS / TimescaleDB domain behavior: selected - table identities and row
  counts are shaped for Timescale/PostGIS-backed `met.*` tables, but no DB write
  occurs in #643.
- Geospatial / CRS / basin geometry: not selected - parser preserves station
  longitude/latitude and optional geometry/properties fields as payload data but
  does not interpret CRS, shapefiles, basin geometry, or PostGIS geometries.
- SHUD numerical runtime / conservation / NaN: not selected - parser does not
  run SHUD, alter numerical model output, or evaluate conservation/runtime
  behavior; parser-only DB reconstruction shape checks may reject rows after
  #641 validation succeeds without changing validator readiness behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected - parser does
  not touch sbatch, Slurm job state, scheduler lifecycle, or node-22 compute.
- External hydro-met providers / snapshot reproducibility: not selected - parser
  preserves `source_id`/cycle/provider identity from the handoff but does not
  fetch, compare, or reproduce external GFS/IFS/ERA5/provider snapshots.
- Run manifest / QC provenance: selected - parser binds reconstruction data to
  exact run/source/cycle/model/basin package identity and checksums.
- Published NHMS artifacts / display identity: selected - parser output feeds
  later display readiness apply logic.
- Config / project setup: not selected - no env or deployment config changes.
- Auth / permissions / secrets: selected - failure reasons and parse reports
  must not include credential-bearing URIs or DSNs.
- Concurrency / shared state / ordering: not selected - no shared mutable state
  or runtime state machine changes.
- Legacy compatibility / examples: selected - #641 fixtures/examples remain
  valid and validator output remains stable.
- Error handling / rollback / partial outputs: selected - failures must return
  stable unavailable reports and omit partial parsed rows.
- Release / packaging / dependency compatibility: not selected - no new runtime
  dependency or packaging behavior.
- Documentation / migration notes: selected - `tasks.md` records parser-only
  evidence and #644 boundary.

Invariant Matrix
Governing invariant: A parser result may expose DB-reconstruction rows only
after the exact handoff manifest, package manifests, payload checksums,
row-count evidence, and time-series lattice validate for one run identity.
Source-of-truth identity/contract: #641 handoff manifest fields and checksums,
`validate_forcing_domain_handoff_path(...)`, payload JSON bytes, and the four
target table names.
Surfaces:
- Producers: #641 complete/incomplete/unsafe fixtures and payload JSON files.
- Validators/preflight: `validate_forcing_domain_handoff_path(...)` and the new
  parser helper.
- Storage/cache/query: no DB/storage writes in #643.
- Public routes/entrypoints: none - no API/CLI/autopipeline changes.
- Frontend/downstream consumers: #644 DB apply helper only; no frontend changes.
- Failure paths/rollback/stale state: missing/malformed/checksum-mismatch inputs
  return unavailable reports with no parsed rows.
- Evidence/audit/readiness: parser evidence, row-count summary, checksums, and
  focused tests.
Regression rows:
- Complete fixture -> parser returns one forcing_version row, two met_station
  rows, eight station_timeseries rows, four interp_weight rows, exact checksums,
  and expected table row counts; met_station rows carry explicit coordinate
  evidence via longitude/latitude or geometry without losing optional geometry.
- Missing required field / malformed payload / missing payload / checksum
  mismatch -> unavailable report with stable reason code and no parsed rows.
- Unsafe traversal or sibling-package payload URI -> validator/parser rejects
  without broad root scan or object-store escape.
- Oversized handoff/package/payload material -> bounded unavailable report
  rather than unbounded memory read or partial parsed rows.
- Existing #641 validator tests -> unchanged output shape and reason codes.

Non-goals:
- No DB apply/upsert/idempotency logic; #644 owns writes.
- No `node27_autopipeline.py` preference/fallback changes; #645 owns policy.
- No node-27 live receipt; #648 owns live evidence.

Review focus:
- Parser output is complete enough for #644 without DB-specific guessing.
- Failure/unavailable outputs do not leak secrets and do not expose partial rows.
- #641 validator behavior is preserved rather than forked or weakened.
