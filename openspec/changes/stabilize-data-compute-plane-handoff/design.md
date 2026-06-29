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

The initial gap was not a single endpoint bug. It was an incomplete handoff
contract between compute artifacts and data-plane ingestion. Pre-#837, the
clearest code symptom was `scripts/node27_mirror_forcing.py`: it treated
node-22 DB rows as the authoritative source for forcing-domain metadata and
could fall back to `infra/env/display.env`, even though node-22's local DB was
historical and display env belongs to the read-only display runtime. After issue
837, that mirror path is archived/stopped rollback-only and requires explicit DSN
plus `NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR` or the matching CLI allow
flag.

## Goals / Non-Goals

**Goals:**

- Make object-store artifacts the canonical handoff between node-22 compute and
  node-27 data-plane ingest for forcing-domain data.
- Keep any compatibility-only archived node-22 rollback mirror safe until it is
  fully removed: explicit DSN plus archived-rollback allow flag only, no
  display-env fallback, structured skip/fail reasons, and no silent use of the
  historical node-22 DB.
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

2. **The node-22 DB mirror is archived rollback-only, compatibility-only,
   explicit, allow-flagged, and sunset-bound.** The normal importer path is
   object-store handoff. Any autopipeline rollback mirror drill may configure
   the source through parent `--node22-url` or env `N22_DSN`, but the mirror
   subprocess itself reads source DSN only from `N22_DSN`; the drill also
   requires `--allow-archived-node22-db-rollback-mirror` or
   `NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR`, must never read
   `infra/env/display.env`, must emit a stable unavailable reason when no
   explicit mirror DSN is configured, and must retain removal/sunset wording.

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
  forcing products. Mitigation: keep only an explicit, archived-rollback mirror
  drill path with a stable skip result and live receipts for qhh/heihe before
  declaring completion.
- Adding a new ingest env could drift from display env or host cron reality.
  Mitigation: source it from a committed template, add preflight tests, and
  include node-27 live evidence in the final issue.

## Migration Plan

1. Define and validate the object-store forcing-domain manifest contract.
2. Harden the existing mirror path so unsafe fallback is removed before deeper
   importer work begins.
3. Implement the object-store importer and prefer it in node-27 autopipeline;
   keep explicit archived-rollback mirror use only for controlled rollback
   drills whose handoff package predates the contract.
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
  and archived rollback mirror assumptions must remain understandable.
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

## Issue #644 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- Database mutation path for `met.forcing_version`, `met.met_station`,
  `met.forcing_station_timeseries`, and `met.interp_weight`.
- PostGIS/Timescale-backed table shape, FK ordering, idempotency, and
  replacement semantics.
- Failure handling must preserve parser unavailable reports and avoid partial
  writes.
- This helper feeds node-27 ingest/autopipeline in later issues but does not
  switch production policy in #644.

Change surface:
- A shared object-store forcing-domain DB apply helper that consumes #643 parser
  output.
- Focused unit tests with SQL/row-count evidence for complete, duplicate,
  missing-field, checksum-mismatch, and credential-safe failure paths.
- OpenSpec `tasks.md` evidence for task 1.6.

Must preserve:
- `parse_forcing_domain_handoff_path(...)` and
  `validate_forcing_domain_handoff_path(...)` public behavior.
- No `scripts/node27_autopipeline.py` preference/fallback switch; #645 owns
  production policy.
- No node-27 live qhh/heihe receipt; #648 owns live evidence.
- Existing DB schema and migration constraints remain the authority; #644 must
  not add or relax migrations.

Must add/change:
- A public apply helper that accepts a parser envelope or a declared handoff
  manifest path + object-store root, and writes only when parser result is
  available.
- The apply helper must upsert/verify `met.forcing_version`, upsert
  `met.met_station`, replace the target `met.forcing_station_timeseries` rows
  for one `forcing_version_id`, and replace/verify the parsed
  `met.interp_weight` source/grid/model scopes.
- The four target table mutations must run inside one apply transaction owned
  by the helper unless the caller explicitly passes an already-open transaction
  context. A failure after any intermediate step must roll back the whole apply
  and must not leave a row-count report that can be read as readiness.
- `met.met_station` writes must use safe-update semantics for the global
  `station_id` primary key: inserting new handoff stations is allowed; updating
  an existing station is allowed only when it is the same basin/version and the
  existing row is compatible with the same handoff-derived station identity.
  Conflicting station metadata, basin ownership, station role, geometry, or
  incompatible `properties_json` MUST fail closed before overwriting the row.
- Station geometry must be converted from parser coordinate evidence into
  `geometry(Point, 4490)` using either longitude/latitude or GeoJSON Point
  geometry without guessing missing coordinates.
- When both longitude/latitude and GeoJSON Point geometry are present for a
  station, #644 must verify they identify the same point within a tiny numeric
  tolerance before writing. Inconsistent coordinate evidence MUST fail closed;
  consistent evidence may use the longitude/latitude pair as the canonical
  `ST_MakePoint` input while preserving the original geometry in
  `properties_json`/lineage evidence if needed.
- `met.forcing_version.checksum` must use the canonical
  `forcing_package_manifest_checksum_sha256`; handoff/package/payload checksums
  remain in apply evidence/lineage rather than being substituted for the
  canonical checksum.
- Complete apply reports must include mode, run/source/cycle/model/basin
  identity, row counts by table, parser/apply evidence, and whether writes were
  performed.
- Unavailable parser outcomes, missing declared fields, checksum mismatch,
  station conflicts, coordinate-evidence conflicts, SQL exceptions, or shape
  conflicts must fail closed with credential-safe reports, no fabricated
  readiness, and transaction rollback of the whole apply transaction.

Selected risk packs:
- Public API / CLI / script entry: selected - #644 introduces a reusable apply
  entrypoint consumed by later node-27 automation.
- Database / transaction / migration: selected - helper mutates four `met.*`
  tables and must respect existing constraints.
- PostGIS / TimescaleDB domain behavior: selected - station geometry and
  hypertable timeseries replacement are central.
- Schema / columns / units / field names: selected - parser rows become DB
  rows; row shape drift must fail before writes.
- Evidence / JSON / Schema Ingestion: selected - reports must bind writes to
  parser evidence and checksums.
- Auth / permissions / secrets: selected - SQL failure/fallback reports must
  not print DSNs or credential-bearing payload values.
- Error handling / rollback / partial outputs: selected - unavailable apply
  must not leave partial DB writes, even when failure is injected after an
  earlier table mutation.
- Concurrency / shared state / ordering: selected - idempotent re-apply,
  global station-id safe-update rules, and FK ordering matter.
- File IO / path safety / overwrite: selected only for the manifest-path
  convenience wrapper that delegates to #643 parser; no new object-store scan.
- Hydro-met time series / forcing windows: selected - station-timeseries rows
  are replaced by forcing version and must preserve units/native resolution.
- Legacy compatibility / examples: selected - archived rollback mirror remains
  explicit and unchanged until #645.

Invariant Matrix
Governing invariant: A DB apply report may claim forcing-domain readiness only
after the exact #643 parser envelope is available and the same rows are written
or verified in the four target `met.*` tables under one forcing/run identity.
Source-of-truth identity/contract: #643 parser envelope, parser evidence,
canonical forcing package checksum, existing DB schema/migrations, and
transaction outcome.
Surfaces:
- Producers: #643 parser output and object-store handoff fixtures.
- Validators/preflight: parser availability, declared row shape, and DB apply
  preflight before writes.
- Storage/cache/query: node-27 PostgreSQL `met.*` tables.
- Public routes/entrypoints: none in #644; later autopipeline policy consumes
  the helper.
- Frontend/downstream consumers: display readiness reads from DB after later
  ingest integration.
- Failure paths/rollback/stale state: parser unavailable, missing fields,
  checksum mismatch, station conflicts, coordinate conflicts, FK/constraint
  conflicts, and SQL exceptions return stable reports without partial readiness
  or partial table mutations.
- Evidence/audit/readiness: apply report row counts, mode, identity, checksum
  lineage, and focused tests.
Regression rows:
- Complete parser fixture -> apply writes/verifies one forcing_version row,
  two station rows, eight timeseries rows, and four interpolation rows with
  canonical checksum lineage and expected row counts.
- Re-applying the same parser fixture -> idempotent report with the same final
  row counts and no duplicated timeseries/interp rows.
- Parser unavailable for missing required field or checksum mismatch -> helper
  performs no DB writes and returns credential-safe unavailable evidence.
- Station geometry and direct-grid constraints -> apply uses parser-proven
  DB-shaped rows and does not reinterpret invalid coordinate evidence.
- Existing station conflict -> helper fails closed without overwriting a global
  `station_id` that belongs to a different basin/role/geometry/identity.
- Longitude/latitude plus GeoJSON geometry mismatch -> helper fails closed
  before station write; consistent dual evidence writes one `geometry(Point,
  4490)` and reports the source of coordinate evidence.
- Failure injected after `forcing_version`, `met_station`, timeseries, or
  interpolation-weight stage -> helper rolls back all four table mutations and
  reports no readiness.
- `scripts/node27_autopipeline.py` and archived rollback mirror code -> no
  behavior change in #644.

Non-goals:
- No autopipeline preference/fallback switch; #645 owns policy.
- No node-27 live qhh/heihe receipt; #648 owns live evidence.
- No schema migration or constraint relaxation.

Review focus:
- Transaction ordering and rollback prevent partial readiness.
- Idempotency is explicit, not inferred from lucky row counts.
- Station upsert safety prevents object-store handoff from clobbering unrelated
  global station ids.
- Coordinate conversion has deterministic consistency rules when both
  lon/lat and geometry exist.
- Apply evidence distinguishes object-store handoff mode from archived node-22
  rollback mirror mode.

## Issue #645 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- `scripts/node27_autopipeline.py` production control-flow change for per-run
  ingest policy.
- Fallback policy between canonical object-store forcing-domain handoff and
  explicit-DSN, allow-flagged, sunset-bound archived node-22 rollback mirror
  compatibility-only mode.
- Subprocess orchestration and JSON summary stability across register,
  forcing-handoff/mirror, parse, and coverage stages. Publish-status remains the
  existing global post-run phase in #645.
- Run-level failure isolation: one run's handoff/mirror/parse failure must not
  abort unrelated runnable runs.
- Secret-safe operator summaries for no declared handoff material, declared but
  unavailable/failed handoff material, missing explicit mirror DSN, mirror
  failures, and parse/coverage failures.

Change surface:
- `scripts/node27_autopipeline.py` handoff stage policy.
- Focused tests for autopipeline per-run orchestration and final summary
  aggregation.
- OpenSpec `tasks.md` evidence for tasks 1.4 and 1.7.

Must preserve:
- Basin seed discovery/activation/backfill behavior is out of scope except that
  per-run failures remain isolated after seed succeeds.
- #643 parser and #644 DB apply internals are consumed only through public
  interfaces; #645 must not reinterpret handoff package rows or checksums.
- Transitional mirror remains available only as explicit compatibility mode and
  still never reads display runtime configuration.
- Output parser, coverage refresh, global publish-status behavior, and
  already-ingested skip semantics remain unchanged except for the recorded
  forcing stage evidence.
- No node-27 ingest env/preflight wrapper changes; #646 owns that boundary.
- No qhh/heihe live receipt; #648 owns live production evidence.

Must add/change:
- Per-run stage order becomes `register -> object-store forcing handoff ->
  parse -> refresh coverage` for runs whose object-store package declares an
  available forcing-domain handoff. The existing `_publish_display_runs()` call
  remains a global post-run phase and is not converted into a per-run stage in
  #645.
- The handoff stage must call the #644 public apply path with the run's declared
  `runs/<run_id>/input/forcing_domain_handoff.json`, `OBJECT_STORE_ROOT`, and
  optional `OBJECT_STORE_PREFIX`.
- A complete handoff apply report with `available=true`/`ready=true` advances
  to parse without invoking the mirror script.
- A run whose declared handoff manifest file is absent is a pre-contract or
  compatibility case. It may use the archived rollback mirror only when
  `N22_DSN` is set or the autopipeline `--node22-url` option is provided and the
  archived-rollback allow flag is explicitly enabled. Autopipeline must pass any
  `--node22-url` value to `scripts/node27_mirror_forcing.py` through the child
  environment as `N22_DSN` plus `NHMS_NODE22_DSN_SOURCE=cli:--node22-url`, never
  as a raw DSN argv entry. The mirror script itself accepts source DSN only from
  `N22_DSN`, and its destination `DATABASE_URL` must reject node-22 historical
  hosts and port `:55433`. The mirror fallback must be labeled
  `archived_node22_rollback_forcing_mirror` and recorded as compatibility
  evidence.
- A run whose handoff manifest is declared/present but whose parser/apply report
  is `available=false` or `status=failed` is not a compatibility fallback case.
  It must stop at `stage=forcing_handoff` without invoking mirror, even when
  `N22_DSN` or `--node22-url` plus the archived-rollback allow flag is
  configured.
- If no declared handoff exists and no explicit mirror DSN is configured, the
  run summary must be `outcome=skipped`, `stage=forcing_handoff`, with a stable
  reason distinguishing no object-store handoff declaration from missing mirror
  configuration. It must not call the mirror subprocess just to observe its
  missing-DSN `rc=2`, and it must not abort unrelated runs.
- If declared object-store handoff parsing/apply is unavailable or failed, the
  run summary must be `outcome=failed`, `stage=forcing_handoff`, with redacted
  stable reasons and no mirror fallback. It must not abort unrelated runs.
- Mirror rc=2 remains a skipped compatibility outcome only when mirror was
  explicitly configured and invoked but reported a compatibility skip such as
  `FORCING_NOT_ON_NODE22`; missing explicit mirror DSN is handled by
  autopipeline precheck before invoking mirror. Mirror nonzero failures remain
  run-level failures.
- Successful run summaries must expose the forcing stage mode, row-count
  evidence for handoff or mirror, river rows, parse status, and coverage refresh
  status without leaking DSNs or credential-bearing paths.
- Final `main()` JSON must add `runs.details[]` while preserving existing
  aggregate counts. Each processed run detail must contain `run_id`, `outcome`,
  `stage`, `forcing_stage.mode`, `forcing_stage.status`,
  `forcing_stage.ready`, `forcing_stage.row_counts`, `forcing_stage.reason_codes`,
  `river_rows`, `parse_status`, and `coverage_refresh` when applicable. Existing
  `runs.skipped_runs[]` and `runs.failed_runs[]` may remain as compact
  compatibility views, but tests must assert the richer `runs.details[]`
  contract.
- `forcing_stage.row_counts` must use stable table keys for both handoff and
  mirror modes: `met.forcing_version`, `met.met_station`,
  `met.forcing_station_timeseries`, and `met.interp_weight`. Handoff mode may
  use #644 `row_counts` directly. Mirror mode must normalize
  `forcing_version`, `met_stations`, `station_timeseries`, and `interp_weight`
  report shapes into those table keys. `forcing_stage.reason_codes` must be
  derived from handoff `unavailable_reasons[].code` or mirror `reason` values.

Selected risk packs:
- Public API / CLI / script entry: selected - autopipeline control flow changes.
- Config / project setup: selected - fallback depends on explicit environment
  variables but does not introduce new env files.
- Evidence / JSON / Schema Ingestion: selected - operator summaries are the
  acceptance surface.
- Auth / permissions / secrets: selected - DSNs and credential-bearing errors
  must be redacted in summary evidence.
- Error handling / rollback / partial outputs: selected - per-run skip/fail must
  stop before parse when forcing readiness is unavailable.
- Concurrency / shared state / ordering: selected - stage order and run
  isolation matter.
- Legacy compatibility / examples: selected - explicit archived rollback mirror
  is retained for compatibility-only rollback drills.
- Database / transaction / migration: selected only through #644 helper
  invocation; no schema changes in #645.
- Hydro-met time series / forcing windows: selected through handoff readiness
  reports; #645 does not inspect payload internals.
- Slurm production lifecycle / mock-vs-real parity: not selected - no compute
  scheduling change.
- Frontend/display behavior: not selected - display consumes downstream DB
  readiness later.

Invariant Matrix
Governing invariant: The autopipeline may proceed to hydro output parse only
after forcing readiness is provided by a declared canonical object-store handoff
or, for runs with no declared handoff, by an explicitly configured archived
rollback mirror with the archived-rollback allow flag; no implicit historical
node-22 DB or display runtime fallback may satisfy the forcing stage.
Source-of-truth identity/contract: run id, object-store root/prefix, declared
handoff manifest path, #643 parser/#644 apply report, #642 mirror report, and
per-run JSON summary.
Surfaces:
- Producers: object-store run directories and forcing-domain handoff manifests.
- Validators/preflight: #643 parser availability and #644 apply report.
- Storage/cache/query: node-27 PostgreSQL writes only through #644 helper or
  explicit #642 mirror.
- Public routes/entrypoints: `scripts/node27_autopipeline.py`.
- Frontend/downstream consumers: parse/coverage/publish stages run only after
  forcing stage readiness.
- Failure paths/rollback/stale state: no declared handoff, declared handoff
  unavailable/failed, missing explicit mirror, mirror skipped/failed, parse
  failed, coverage failed, and one-run isolation.
- Evidence/audit/readiness: run summary fields, handoff/mirror mode labels,
  row counts, stable reasons, and focused tests.
Regression rows:
- Complete handoff package -> autopipeline calls object-store apply, does not
  call mirror, then runs parse/coverage and reports `outcome=ingested`.
- No declared handoff + explicit mirror configured through `N22_DSN` or
  autopipeline `--node22-url` plus archived-rollback allow flag -> autopipeline
  calls archived rollback mirror, labels compatibility mode, records the mirror
  DSN source without the DSN value, then proceeds to parse when mirror succeeds.
- No declared handoff + no explicit mirror configured -> that run is skipped at
  `forcing_handoff` with stable reason; unrelated runs continue.
- Declared handoff unavailable (missing required field, malformed payload,
  checksum mismatch, or parser unavailable) + explicit mirror configured -> that
  run fails at `forcing_handoff` without invoking mirror; unrelated runs
  continue.
- Handoff apply `status=failed` -> that run fails at `forcing_handoff` without
  mirror fallback; unrelated runs continue.
- Explicitly configured mirror rc=2 and mirror nonzero rc -> skipped/failed
  summary preserves stable mirror reason and run isolation; no-DSN compatibility
  cases are skipped by autopipeline before mirror invocation.
- Parse failure after forcing readiness -> run fails at `parse` and does not
  hide the forcing stage evidence.
- Coverage refresh failure after forcing readiness and parse success -> run
  detail records a distinct coverage refresh failure/status without being
  confused with forcing handoff or mirror unavailability; unrelated runs
  continue.
- Final `main()` output -> `runs.details[]` preserves per-run forcing mode,
  status, readiness, row counts, stable reason codes, parse status, river rows,
  and coverage refresh status for success/skip/fail outcomes.
- Publish phase -> `_publish_display_runs()` remains one global post-run call
  after the run loop; it is not represented as a per-run `runs.details[].stage`
  and is still counted in the existing aggregate `runs.published` field.

Non-goals:
- No parser/apply implementation changes beyond invoking their public API.
- No mirror internals changes beyond autopipeline invocation/summary labeling.
- No node-27 env/preflight wrapper, topology guardrail, or live receipt changes.

Review focus:
- Fallback policy cannot accidentally resurrect implicit node-22/display-env
  dependencies.
- Summary fields let operators distinguish object-store unavailable, explicit
  mirror unavailable, mirror compatibility, and downstream parse/coverage
  failures.
- Tests prove one-run failure isolation and that successful object-store handoff
  bypasses mirror.

## Issue #646 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- Production cron/on-demand entrypoint changes on node-27.
- Writer-capable `DATABASE_URL` and secret-bearing environment handling.
- Preflight must run before DB writes, object-store mutation, run registration,
  forcing handoff/mirror, output parse, coverage refresh, and publish-status
  advancement.
- Display API `display_readonly` boundary must remain separate from data-plane
  ingest readiness.
- Node-27 live receipt is required because this issue changes production
  wrapper/env behavior.

Change surface:
- Add a committed ingest env template/contract under `infra/env/` for node-27
  data-plane ingest, separate from `infra/env/display.example`.
- Update `scripts/node27_autopipe_cron.sh` to source or validate the ingest
  env contract, stop using hard-coded writer defaults, never source display env,
  and pass explicit role/config-source evidence into the Python autopipeline.
- Update `scripts/node27_autopipeline.py` so preflight happens at the beginning
  of `main()` and returns structured, redacted JSON with rc=2 before any seed,
  register, mirror, parse, coverage, or publish work when config is missing or
  unsafe.
- Add focused tests for preflight-before-work, wrapper/env separation,
  credential-safe summaries, basin failure isolation, one-run isolation, and
  already-ingested skip behavior.

Must preserve:
- #645 handoff preference and explicit mirror fallback semantics.
- Existing run discovery, seed/import/activate/backfill, register, parse,
  coverage, and global publish behavior when preflight passes.
- Display API startup semantics in `apps/api/runtime_mode.py`: display
  `display_readonly` health evidence must not become ingest writer evidence.
- No parser/apply/mirror internals or schema migrations in #646.

Must add/change:
- Ingest role evidence must identify `node27_data_plane_ingest`, stage shape,
  config source, object-store root, Basins root, work root, log root, DB
  host/port/database without password/token material, discovered/processed
  counts, and final return code.
- Preflight blockers must use stable codes for missing/unsafe `DATABASE_URL`,
  display-readonly role leakage, readonly/display-looking DB identity, missing
  `OBJECT_STORE_ROOT`, missing `BASINS_ROOT`, missing/unsafe work root, and
  missing/unsafe log root.
- Preflight failure output must be the only work result; tests must prove that
  `_basin_seeded`, `_already_ingested_runs`, `_seed_basin`, `_process_run`, and
  `_publish_display_runs` are not reached. The shell wrapper must also stop
  immediately on preflight-blocked rc=2 and must not run coverage backstop or
  any other post-hook after a blocked preflight.
- The cron wrapper must require an ingest-specific env file or an explicit
  ambient-env override, export the ingest role/config source, and log blocked
  config without printing secrets.
- The node-27 receipt must prove the wrapper/preflight sees writer ingest
  readiness separately from display API `display_readonly` health.

Selected risk packs:
- Config / project setup: selected - production env/template and wrapper
  semantics change.
- Auth / permissions / secrets: selected - writer DB URL and logs must redact
  credentials and reject display-readonly leakage.
- Public API / CLI / script entry: selected - cron wrapper and autopipeline CLI
  are operational entrypoints.
- Error handling / rollback / partial outputs: selected - preflight must fail
  before partial writes and retain existing run/basin isolation after passing.
- Evidence / JSON / Schema Ingestion: selected - operator receipts and summaries
  are the acceptance surface.
- Concurrency / shared state / ordering: selected - flock wrapper behavior and
  global publish ordering must remain unchanged.
- Legacy compatibility / examples: selected - existing display env examples must
  remain readonly-only; archived rollback mirror config stays explicit and
  allow-flagged.
- Slurm production lifecycle / mock-vs-real parity: not selected - no compute
  scheduling change.
- Frontend/display behavior: not selected except display health separation.

Invariant Matrix
Governing invariant: Node-27 ingest may perform data-plane writer work only
after an explicit ingest-role preflight validates writer DB, object-store,
Basins, work, and log roots; display-readonly runtime evidence never satisfies
ingest readiness.
Source-of-truth identity/contract: `infra/env/node27-ingest.example`,
untracked node-27 ingest env file, `scripts/node27_autopipe_cron.sh`,
`scripts/node27_autopipeline.py` preflight summary, and node-27 live receipt.
Surfaces:
- Producers: node-27 cron/on-demand environment and object-store run dirs.
- Validators/preflight: ingest env loader and autopipeline preflight.
- Storage/cache/query: node-27 PostgreSQL writer URL only after preflight; no
  display env writer derivation.
- Public routes/entrypoints: shell cron wrapper and autopipeline CLI.
- Frontend/downstream consumers: display API remains readonly consumer only.
- Failure paths/rollback/stale state: missing env, unsafe display role, readonly
  DB identity, missing roots, seed failure, per-run failure, already-ingested
  skip, publish-status failure visibility.
- Evidence/audit/readiness: structured preflight JSON, final summary role block,
  redacted logs, node-27 receipt paths.
Regression rows:
- Missing `DATABASE_URL`/root config -> rc=2 preflight JSON with stable blocker
  codes, no seed/run/publish calls, no wrapper coverage backstop, and no
  secret-bearing output.
- `NHMS_SERVICE_ROLE=display_readonly` or readonly/display DB identity in ingest
  env -> rc=2 with stable blocker; display API runtime tests remain separate.
- Valid ingest env + no pending runs -> summary reports data-plane role,
  preflight ready, discovered counts, already-ingested counts, and return_code.
- Seed failure -> basin failure is recorded and unrelated basins/runs may
  continue; preflight evidence is still present.
- One run failure after preflight -> unrelated run continues and global publish
  remains post-loop.
- Already-ingested runs -> skipped before per-run work unless `--force`.
- Cron wrapper missing env file without explicit ambient override -> blocked
  before invoking Python autopipeline; wrapper never sources display env.
- Node-27 live receipt -> ingest writer preflight and display readonly health
  are both visible and explicitly not interchangeable.

Non-goals:
- No changes to object-store handoff parser/apply/mirror internals.
- No topology static guardrail/docs cleanup beyond the ingest env/wrapper
  boundary; #647 owns topology contract work.
- No qhh/heihe display readiness declaration; #648 owns final production
  readiness/live evidence beyond the #646 preflight receipt.

Review focus:
- No fallback path may read `infra/env/display.env` or use display readonly DB
  credentials for ingest writes.
- Preflight must happen before every mutating or expensive stage and must be
  easy to prove in tests.
- Evidence must be useful to operators without leaking DSNs, tokens, or secret
  env values.

## Issue #647 Fixture

Fixture level: expanded
Repair intensity: high
Project profile: NHMS

Mandatory expanded triggers:
- Production topology, verification oracle routing, and current runbook
  contracts.
- Static governance checks over active docs/scripts and env-source wording.
- Legacy compatibility boundary: historical/archive material must remain
  readable without being mistaken for current operation.
- Config / project setup boundary for `infra/env/display.env` versus node-27
  data-plane writer or archived rollback mirror jobs.

Change surface:
- Current operational docs and role-boundary docs.
- `openspec/project-profile.md` evidence/oracle routing guidance.
- Governance/static drift checks and their focused tests/fixtures.
- OpenSpec task evidence for the production topology contract.

Must preserve:
- Explicitly historical, archived, superseded, or compatibility-only documents
  may still mention node-22 DB/writer history when labeled as non-current.
- Display API readonly env use remains valid for display runtime operations.
- Runtime importer, autopipeline, Slurm scheduling, and display API behavior do
  not change in #647.

Must add/change:
- Active docs route DB, ingest, display, and frontend live validation to
  node-27; node-22 is compute/Slurm/artifact producer only.
- Active docs/scripts mark node-22 local PostgreSQL `:55433` as historical,
  do-not-connect for current NHMS production state, archived/stopped
  rollback-only, and sunset-bound.
- Static guardrails flag active node-22 writer assumptions and active
  `infra/env/display.env` reuse for data-plane writer or mirror authority.
- Guardrail tests include positive drift fixtures and negative historical,
  archive, compatibility-only, and display-readonly env fixtures.

Risk packs considered:
- Public API / CLI / script entry: selected - static guard script/entrypoint
  behavior and exit/finding contract may change.
- Config / project setup: selected - current topology and env-source authority
  are production setup contracts.
- File IO / path safety / overwrite: not selected - no new file traversal,
  publish, delete, or overwrite behavior; tests may use temporary fixtures only.
- Schema / columns / units / field names: not selected - no data schema change.
- Auth / permissions / secrets: selected - checks must not encourage display
  readonly credentials or leak real env values.
- Concurrency / shared state / ordering: not selected - no runtime state machine
  or concurrent job behavior changes.
- Resource limits / large input / discovery: selected - static scan scope must
  stay bounded to repo text surfaces and avoid generated/archive false positives.
- Legacy compatibility / examples: selected - historical/archive/compatibility
  wording must remain allowed when clearly non-current.
- Error handling / rollback / partial outputs: selected - drift findings should
  be deterministic with stable categories and nonzero status when applicable.
- Release / packaging / dependency compatibility: not selected - no dependency
  or package surface changes.
- Documentation / migration notes: selected - current runbooks and role docs are
  the main user-facing surface.
- Geospatial / CRS / basin geometry: not selected - no geometry or map changes.
- Hydro-met time series / forcing windows: not selected - no forcing-window
  behavior changes.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD runtime
  changes.
- PostGIS / TimescaleDB domain behavior: selected - docs must route active DB
  validation to node-27 and mark node-22 DB non-current.
- Slurm production lifecycle / mock-vs-real parity: selected - node-22 remains
  Slurm scheduling oracle only.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot behavior changes.
- Run manifest / QC provenance: not selected - no manifest/QC contract changes.
- Published NHMS artifacts / display identity: selected - display/readiness
  validation route remains node-27 and distinct from compute artifacts.

Required evidence:
- Static guard positive fixture: active text that makes node-22 current DB writer
  or sources `infra/env/display.env` for writer/mirror jobs -> guard finding with
  stable category.
- Static guard positive fixture: active text that mentions node-22 local
  PostgreSQL, `:55433`, or archived rollback mirror without historical,
  do-not-connect, compatibility-only, explicit-DSN, archived-rollback allow flag,
  archived/stopped rollback-only, and sunset/removal wording -> guard finding
  with stable category.
- Static guard negative fixtures: historical/archive/compatibility-only node-22
  DB text and display API readonly env text -> no finding.
- Current-doc grep or focused test evidence that node-27 owns active DB, ingest,
  display API, and frontend, while node-22 owns Slurm/SHUD/artifact production.
- Project profile evidence guidance distinguishes local checks, node-27 live
  DB/display checks, and node-22 Slurm checks.
- `uv run pytest -q <focused guardrail tests>`.
- `uv run ruff check .`.
- `openspec validate stabilize-data-compute-plane-handoff --strict --no-interactive`.

Invariant Matrix:
- Governing invariant: Current operational surfaces must describe node-22 as
  compute/artifact producer only and node-27 as active DB/ingest/display host;
  display runtime env must never become data-plane writer or mirror authority.
- Source-of-truth identity/contract: `production-topology-contract` spec plus
  current docs/static guard categories for current, historical, compatibility,
  display-readonly, and drift contexts.
- Producers: active docs and scripts that mention topology/env sourcing.
- Validators/preflight: governance/static drift checks and focused tests.
- Storage/cache/query: none - #647 does not touch DB or object-store writes.
- Public routes/entrypoints: static guard command or audit entrypoint; no API
  route changes.
- Frontend/downstream consumers: operator runbooks and agent instructions that
  choose local/node-27/node-22 validation oracles.
- Failure paths/rollback/stale state: guard findings for active drift; allowed
  historical/archive/compatibility contexts stay non-blocking.
- Evidence/audit/readiness: focused pytest, ruff, OpenSpec validation, and task
  evidence.
- Regression rows:
  - Active doc says node-22 is current NHMS DB writer -> guard reports topology
    drift.
  - Active writer/mirror instructions source `infra/env/display.env` -> guard
    reports display-env writer drift.
  - Historical/archive/compatibility-only text mentioning node-22 `:55433` ->
    guard allows it when clearly non-current.
  - Display API readonly restart/env instructions -> guard allows
    `infra/env/display.env` for display runtime.
  - Current verification routing docs -> local checks, node-27 live DB/display,
    and node-22 Slurm oracles are distinct.

Boundary-surface checklist:
- Current docs/runbooks: must describe the verified node-27-centric active
  topology and node-22 compute-only role.
- Historical/archive docs: may retain old node-22 writer context only with
  explicit historical/superseded/compatibility wording.
- Static guard scope: scan active governance surfaces and fixtures without
  turning archived evidence into required current behavior.
- Env-source boundary: display env is valid for display runtime and invalid for
  data-plane writer or archived rollback mirror authority.
- Verification boundary: node-27 is live DB/display oracle; node-22 is Slurm
  scheduling oracle; local checks do not substitute for required live receipts.

Non-goals:
- No runtime importer/autopipeline behavior change.
- No Slurm compute behavior change.
- No removal of explicitly historical/archive documents.
- No node-27 qhh/heihe live receipt; #648 owns final production evidence.

Review focus:
- Guardrail false positives/negatives around historical/archive/compatibility
  and display-readonly contexts.
- Current docs must not leave active node-22 writer instructions ambiguous.
- Verification/oracle routing must match the two-node topology in `AGENTS.md`.
