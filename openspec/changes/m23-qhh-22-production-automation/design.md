## Context

M20 defines the generic production scheduler direction, M21 defines the QHH hydro-met/ops MVP, and M22 defines the two-node Docker/read-only display boundary. Current local evidence shows node 22 is only partially wired:

- `nhms-compute-compute-api-1` runs with `NHMS_SERVICE_ROLE=compute_control`, but `SHUD_EXECUTABLE=/bin/true`.
- The compute container does not have Slurm CLI tools, and the configured Slurm gateway points to the compute API itself instead of a working gateway path.
- The live DB has canonical meteorology but no active `core.model_instance`, no `met.forcing_version`, no station forcing rows, no hydro runs, no river time series, and no pipeline job/event history.
- The processed QHH package does exist under `NHMS_BASINS_ROOT`, including `qhh.tsd.forc`; therefore the missing piece is production bootstrap and dynamic per-cycle forcing generation, not regenerating the basin package.
- `nhms-pipeline plan-production` can plan only when explicitly given the configured workspace root; the CLI currently falls back to `.nhms-workspace`, which is unsafe inside the container.

The corrected architecture is: rSHUD/AutoSHUD informs the static SHUD project/forcing format; SHUD performs hydrologic computation; the processed basin supplies fixed forcing stations and river/output identities; each forecast cycle downloads fresh data and interpolates/extracts that data to the fixed stations before running SHUD.

## Goals / Non-Goals

**Goals:**

- Make QHH on node 22 a complete automated production slice from forecast discovery through DB/published outputs.
- Use existing generic scheduler/orchestrator and worker modules rather than depending on QHH diagnostic shell scripts.
- Make all live blockers explicit and machine-readable: unavailable forecast source, missing model bootstrap, missing SHUD library, unhealthy Slurm path, parser/publish failure.
- Keep artifacts and evidence under the repository `artifacts/` tree or `/scratch/frd_muziyao`, with display products under `/ghdc/data/nwm/published`.
- Preserve node 27 as a readonly display plane that consumes database state and published artifacts only.

**Non-Goals:**

- No nationwide rollout or new basin onboarding beyond QHH.
- No frontend feature work except preserving data contracts that 27 already consumes.
- No fake SHUD success, synthetic forcing rows, or placeholder Slurm receipts.
- No attempt to make Docker itself a Slurm cluster; a host Slurm gateway is acceptable for MVP if it is preflighted and documented.

## Decisions

### 0. Production identity contract is the shared fixture for all M23 issues

M23-1 defines a single QHH production contract before any worker/runtime issue can add mutation. Every downstream stage must carry the same required identity tuple: `run_id`, `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `source`, `cycle_time`, `canonical_product_id`, `forcing_version_id`, `hydro_run_id`, and `published_manifest_id`. `pipeline_job_id` and `pipeline_event_id` are optional stage/event evidence-correlation fields: when both expected and actual evidence provide them, they must match, but they must not be synthesized as run-level identity. A later issue may add fields only by preserving this tuple and documenting migration behavior in this change.

Alternative considered: let scheduler, forcing, Slurm, parser, and publisher each define local identity fields. Rejected because cross-stage evidence can otherwise mix a fresh forecast from one cycle with forcing, hydro output, or display artifacts from a sibling run.

### 1. Bootstrap fixed model state before scheduling

The scheduler must treat "no active model" as a blocker, not as an empty success. A bootstrap command or service task will import/publish the QHH Basins package, create/activate the model instance, seed fixed forcing stations from `qhh.tsd.forc`, and seed output river/segment identities before candidate discovery can submit work.

Alternative considered: keep invoking `scripts/run_qhh_cycle.sh` because it can perform several bootstrap steps. Rejected for production automation because M20 requires generic scheduler behavior and because diagnostic scripts make idempotency, locks, and pipeline evidence harder to prove.

### 2. Dynamic forcing targets fixed SHUD stations

Fresh GFS/IFS cycles are downloaded and canonicalized every run. The forcing producer then maps canonical grids to fixed `met.met_station` rows with `station_role="forcing_grid"` and writes `met.forcing_version`, `met.forcing_station_timeseries`, and SHUD forcing package files. This matches the processed basin contract without pretending stations were pre-extracted for future forecasts.

Alternative considered: require regenerating station definitions per forecast cycle. Rejected because the processed basin already defines SHUD forcing stations; only meteorological values are dynamic.

### 3. Real runtime readiness is a preflight gate

`/bin/true` is treated as invalid for production. The runtime preflight must resolve the configured SHUD executable, required shared libraries, project inputs, workspace/object-store/published roots, and Slurm gateway/host submission path before a candidate is submitted. Missing Slurm CLI inside the app container is acceptable only when a configured gateway or host service can submit and account for jobs.

Alternative considered: allow local foreground SHUD execution as the first production mode. Rejected for this change because the user specifically wants SHUD Slurm running on node 22; local execution can remain a deterministic test fixture, not the business path.

### 4. Published artifacts are the cross-node boundary

Node 22 writes logs, manifests, and display products under the configured published artifact root and records supported `published://` or allowlisted URIs in DB state. Node 27 does not read private workspaces, Slurm files, or compute-only paths.

Alternative considered: share the entire workspace through NFS. Rejected because M22 already established a narrower readonly display boundary and because private workspaces may contain intermediate files, secrets, or unstable paths.

### 5. Stage/status taxonomy is shared, not display-specific

Pipeline stages are `download`, `convert`, `forcing`, `forecast`, `parse`, `q_down_publish`, `frequency_publish`, and aggregate `production_run`. Stable statuses are `pending`, `ready`, `running`, `succeeded`, `blocked`, `unavailable`, `partial`, `failed`, `cancelled`, and `superseded`. Stage-specific error codes may be added, but they must map to one of those statuses and must not allow deterministic/mock evidence to mark live business readiness true.

Alternative considered: keep existing ad hoc status strings in each module. Rejected because operations, E2E evidence, and node-27 display need to distinguish blocked live dependencies from successful production outputs.

### 6. Scheduler operationalization includes env defaults and service loop

The production scheduler CLI must honor `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, and evidence root env values when flags are absent. Docker/systemd `scheduler-once` and continuous/timer modes must set locks, evidence directories, and source/model filters explicitly enough to avoid duplicate submissions and accidental system-disk output.

Alternative considered: document manual `--workspace-root` invocation only. Rejected because the requested target is business automation, not an operator-run diagnostic command.

## Issue #252 Fixture

Issue type: feature/contract
Project profile: other, with AutoSHUD/SHUD contract surfaces
Blast radius: high
Fixture level: expanded
Repair intensity: high

Change surface:

- `services/orchestrator` production planning/evidence contract helpers.
- `schemas/*run*` and `schemas/pipeline_job.schema.json` example/validation fixtures where the contract is expressed.
- `tests/test_production_scheduler.py` and `tests/test_orchestration_chain.py` reusable contract tests.
- This OpenSpec design/tasks/spec delta.

Must preserve:

- Existing M22 readonly-display contract: node 27 consumes DB state and published artifacts only.
- Existing scheduler dry-run behavior and no-mutation evidence guarantees.
- Existing strict ops identity API behavior for `source`, `cycle_time`, `run_id`, and `model_id`.

Must add/change:

- A documented production identity matrix reusable by scheduler, forcing, Slurm, parser, publisher, and E2E evidence.
- A documented stage/status/error taxonomy for the full M23 lane.
- A URI/artifact boundary that rejects private workspace/scratch paths as display-readable artifacts.
- Contract tests proving identity mismatch and private path evidence cannot be accepted as same-run display evidence.

Selected risk packs:

- Public API / CLI / script entry: selected - contract helpers and fixtures become the input to later CLI/scheduler issues.
- Config / project setup: selected - URI roots and evidence roots are part of the contract.
- File IO / path safety / overwrite: selected - private workspace paths must not cross into display-readable artifact state.
- Schema / columns / units / field names: selected - identity/status fields become DB/API/schema evidence fields.
- Legacy compatibility / examples: selected - M22 node-27 display boundary and existing ops identity behavior must remain valid.
- Error handling / rollback / partial outputs: selected - blocked/unavailable/partial statuses drive later stage behavior.
- Documentation / migration notes: selected - downstream issues use this contract as their fixture.

Risk packs considered:

- Geospatial / CRS / shapefile sidecars: not selected - M23-1 defines identity only; station/segment geometry appears in later bootstrap/forcing issues.
- Time series / forcing / temporal boundaries: not selected - cycle identity is selected here, but dynamic forcing values are out of scope.
- Numerical stability / conservation / NaN: not selected - no solver output or numerical processing in this issue.
- Solver runtime / performance / threading: not selected - SHUD runtime and Slurm behavior are later issues.
- Resource limits / large input / discovery: not selected - no forecast discovery or large artifact ingestion in this issue.
- Release / packaging / dependency compatibility: not selected - no package/runtime dependency changes.

Boundary-surface checklist:

- Shared helper roots: production contract helpers and schema examples created for M23-1.
- Public entrypoints: scheduler/evidence consumers that later read the helper output; no new live runtime command in M23-1.
- Read surfaces: tests and docs that validate persisted evidence/manifest identity.
- Write/delete/overwrite surfaces: none in live runtime; contract tests may write temp fixtures only.
- Staging/publish/rollback surfaces: published artifact URI classification and private-path rejection.
- Producer/consumer evidence boundaries: scheduler, forcing producer, SHUD runtime, output parser, tile publisher, `/ops`, and node-27 artifact reader.
- Stale-state/idempotency boundaries: duplicate or mismatched run/model/source/cycle identities must be rejected before reuse.
- Unchanged downstream consumers: M22 readonly display and strict ops identity endpoints.

Invariant Matrix

Governing invariant: Every production artifact, DB row, pipeline event, and display-readable URI accepted for a QHH run must bind to the same production identity tuple and must never use private compute workspace paths as node-27-readable evidence.
Source-of-truth identity/contract: `run_id` plus `model_id`, `basin_id`, `source`, `cycle_time`, `basin_version_id`, `river_network_version_id`, `canonical_product_id`, `forcing_version_id`, `hydro_run_id`, `published_manifest_id`, and optional pipeline job/event correlation.
Surfaces:

- Producers: scheduler candidate/evidence helpers, later forcing/Slurm/parser/publisher producers.
- Validators/preflight: contract validators and reusable tests introduced by M23-1.
- Storage/cache/query: `ops.pipeline_job`, `ops.pipeline_event`, `hydro.hydro_run`, `met.forcing_version`, manifests.
- Public routes/entrypoints: production scheduler CLI and `/ops`/jobs display readers that consume identity evidence.
- Frontend/downstream consumers: node-27 readonly display and published artifact reader.
- Failure paths/rollback/stale state: blocked/unavailable/partial/error evidence, duplicate same-run detection, sibling-cycle rejection.
- Evidence/audit/readiness: OpenSpec fixture, schema examples, contract tests, and later E2E artifacts.

Regression rows:

- Full identity tuple for one QHH source/cycle/run -> accepted as same-run evidence and reusable by downstream issue tests.
- Same `run_id` with mismatched `model_id`, `basin_id`, `source`, `cycle_time`, basin/river version, canonical product, forcing version, hydro run, manifest, or present pipeline job/event correlation -> rejected as identity mismatch before evidence reuse.
- `published://` or allowlisted published-root URI bound to the same identity -> accepted as display-readable boundary evidence.
- Workspace-only, scratch-only, Slurm-private, traversal, or non-allowlisted local path -> rejected as display-readable artifact/log evidence.
- Existing M22 readonly DB/published artifact consumer -> remains compatible and is not required to mount node-22 private workspace paths.

## Issue #253 Fixture

Issue type: feature/config
Project profile: other, with production scheduler CLI and deployment config surfaces
Blast radius: high
Fixture level: expanded
Repair intensity: high

Change surface:

- `services/orchestrator/cli.py` `nhms-pipeline plan-production` option/default handling.
- `services/orchestrator/scheduler.py` runtime root resolution, root preflight, lock/evidence root safety, and pass evidence.
- `infra/compose.compute.yml`, `infra/env/compute.example`, and deployment/systemd/runbook docs for one-shot and continuous scheduler modes.
- `tests/test_production_scheduler.py` and `tests/test_production_slurm_validation.py` focused config/root tests.

Must preserve:

- #252 production identity/status/URI contract and scheduler evidence compatibility.
- Dry-run/no-mutation semantics: deterministic `--plan` evidence must not claim live readiness or call download/Slurm/SHUD/hydro/met mutation.
- Existing path containment protections for scheduler locks and evidence artifacts.
- Explicit `--workspace-root`, `--lock-path`, and `--evidence-dir` diagnostic compatibility when those flags are provided and safely contained.
- Existing M22 node-27 readonly published artifact boundary.

Must add/change:

- `nhms-pipeline plan-production` without `--workspace-root` must use documented environment/config roots, especially `WORKSPACE_ROOT`, instead of app-local `.nhms-workspace`.
- Object-store, published root, lock root, evidence root, and temporary/runtime roots must resolve from approved env/config defaults and be included in redacted scheduler evidence.
- Invalid, missing, unwritable, unsafe, or uncontained runtime roots must produce pre-mutation blockers before download, Slurm, SHUD, hydro, met, parse, or publish mutation.
- Compute compose/env/docs must make no-flag `scheduler-once` the business validation lane and describe explicit `--workspace-root` as diagnostic compatibility.

Selected risk packs:

- Public API / CLI / script entry: selected - `nhms-pipeline plan-production` and Docker scheduler-once are public operational entrypoints.
- Config / project setup: selected - runtime roots, service role, source/model filters, lock/evidence locations, and scheduler loop settings come from env/config.
- File IO / path safety / overwrite: selected - workspace, lock, evidence, object-store, temp, and published roots are user/config controlled and must not escape approved roots or overwrite unsafe lanes.
- Schema / columns / units / field names: selected - scheduler pass evidence gains/uses root/preflight fields that downstream evidence and docs consume.
- Resource limits / large input / discovery: selected - continuous/timer mode, max passes, intervals, source/model filters, and evidence writes must stay bounded.
- Legacy compatibility / examples: selected - existing explicit-root test and M22/M23 runbooks must remain valid while changing the business default.
- Error handling / rollback / partial outputs: selected - invalid roots must block before mutation and still write safe evidence when possible.
- Documentation / migration notes: selected - compute compose/env/systemd/runbooks define the accepted operational path.

Risk packs considered:

- Geospatial / CRS / shapefile sidecars: not selected - no station/segment geometry changes in #253.
- Time series / forcing / temporal boundaries: not selected - scheduler window parameters are retained, but no forcing values or temporal science processing changes.
- Numerical stability / conservation / NaN: not selected - no solver or numerical output changes.
- Solver runtime / performance / threading: not selected - SHUD execution and Slurm runtime behavior are later issues; #253 only blocks before mutation.
- Release / packaging / dependency compatibility: not selected - no package/runtime dependency changes expected.

Boundary-surface checklist:

- Shared helper roots: `ProductionSchedulerConfig`, scheduler root/preflight helpers, CLI option handling.
- Public entrypoints: `nhms-pipeline plan-production`, compute `scheduler-once`, documented continuous/timer mode.
- Read surfaces: env vars, compose env files, systemd/runbook instructions, scheduler evidence.
- Write/delete/overwrite surfaces: scheduler lock file, evidence JSON, temp/runtime root preflight; no live product mutation in #253.
- Staging/publish/rollback surfaces: published root availability/writability preflight only; no parser/publisher implementation.
- Producer/consumer evidence boundaries: scheduler pass evidence consumed by PR evidence, E2E docs, and later M23 issues.
- Stale-state/idempotency boundaries: scheduler lock/lease and no duplicate pass behavior remain bounded.
- Unchanged downstream consumers: #252 production contract helpers, M22 readonly display, `/ops` strict identity endpoints.

Invariant Matrix

Governing invariant: A no-flag production scheduler run on node 22 must resolve all runtime roots from approved deployment configuration, prove them in redacted evidence, and block before any mutation if a required root is missing, unsafe, uncontained, or unwritable.
Source-of-truth identity/contract: documented env/config fields `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, scheduler lock/evidence root env or defaults, runtime/temp root env, service role, source/model filters, and dry-run/continuous mode flags.
Surfaces:

- Producers: CLI config construction, `ProductionSchedulerConfig`, compute compose/env/systemd/runbook examples.
- Validators/preflight: scheduler root/path safety helpers, pre-mutation root checks, lock/evidence directory open/write guards.
- Storage/cache/query: scheduler evidence JSON, lock file, object-store root and published root preflight metadata.
- Public routes/entrypoints: `nhms-pipeline plan-production` and compute `scheduler-once` command; no API route change.
- Frontend/downstream consumers: E2E evidence and docs; node-27 display remains read-only and unchanged.
- Failure paths/rollback/stale state: invalid/unwritable roots, lock contention, evidence-write failure, continuous max-pass bounds.
- Evidence/audit/readiness: OpenSpec fixture, scheduler pass evidence, docs/runbook command snippets, CI/mock tests, optional Docker smoke BLOCKED/PASS evidence.
Regression rows:

- Env-only no-flag CLI with `WORKSPACE_ROOT`, object-store, published, lock/evidence/temp roots -> scheduler config uses env roots and evidence records redacted resolved roots.
- Missing `WORKSPACE_ROOT` in production/no-flag mode -> stable config/preflight blocker or CLI error; it must not create `.nhms-workspace` under `/app`.
- Invalid/unwritable/out-of-bound workspace/object-store/published/lock/evidence/temp root -> blocker before mutation with no download/Slurm/SHUD/hydro/met writes.
- Explicit safe `--workspace-root` diagnostic command -> still works and keeps lock/evidence under that workspace.
- Compute `scheduler-once` compose command -> runs `nhms-pipeline plan-production --plan` without manual root flags using env roots.
- Existing M22 readonly display and #252 contract tests -> remain compatible.

## Issue #254 Fixture

Issue type: feature/bootstrap
Project profile: NHMS, with AutoSHUD/SHUD contract surfaces
Blast radius: high
Fixture level: expanded
Repair intensity: high

Change surface:

- QHH bootstrap command/script and registry import/publish helpers for the existing processed Basins/SHUD package.
- `core.model_instance`, basin/package/version metadata, `met.met_station` forcing-grid station rows, and output river/segment identity rows needed by parser/publisher.
- `services/orchestrator/scheduler.py` discovery behavior only where needed to prove the active QHH model is schedulable.
- Focused bootstrap/registry tests plus `tests/test_production_scheduler.py`.

Must preserve:

- #252 production identity/status/URI contract and #253 scheduler env-root/pre-mutation safety.
- No dynamic forecast values, `met.forcing_version`, `met.forcing_station_timeseries`, SHUD runtime execution, Slurm submission, parse, publish, or frontend behavior in #254.
- Existing processed QHH basin package contract; bootstrap must not rebuild the watershed package or require rSHUD/AutoSHUD at runtime.
- Existing scheduler dry-run/no-mutation semantics and node-27 readonly display boundary.

Must add/change:

- An idempotent QHH bootstrap command that validates the processed package, creates or activates exactly one scheduler-ready `core.model_instance`, and reports created/updated/unchanged/blocked counts.
- Validation and seeding for fixed SHUD forcing stations from `qhh.tsd.forc`, including station role, SHUD forcing index, filename/source identity, coordinates, and elevation metadata.
- Seeding or verification for output river/segment identities required by later SHUD output parsing and display publication.
- Typed blockers for missing package/project files, station-count mismatch, incomplete model metadata, and duplicate active QHH model identities.

Selected risk packs:

- Public API / CLI / script entry: selected - bootstrap command/script becomes an operator entrypoint and scheduler precondition.
- Config / project setup: selected - `NHMS_BASINS_ROOT`, QHH project path, model ids, package URI/root, and resource profile shape drive production discovery.
- File IO / path safety / overwrite: selected - package roots, `qhh.tsd.forc`, manifest/checksum reads, and any generated evidence must stay bounded and under approved roots.
- Schema / columns / units / field names: selected - `core.model_instance`, basin/version identity, `met.met_station`, station metadata, and output identity fields are DB contracts.
- Auth / permissions / secrets: not selected - no new credential or permission model; DB access uses existing repository-managed configuration and must not log secrets.
- Concurrency / shared state / ordering: selected - repeated bootstrap and duplicate active-model handling must be idempotent and deterministic.
- Resource limits / large input / discovery: selected - package discovery and forcing/output file parsing must be bounded and scoped to QHH paths.
- Legacy compatibility / examples: selected - existing QHH Basins package and existing scheduler model discovery behavior must remain compatible.
- Error handling / rollback / partial outputs: selected - blocked bootstrap must not leave QHH marked scheduler-ready or create partial future-cycle data.
- Release / packaging / dependency compatibility: not selected - no package/runtime dependency changes expected.
- Documentation / migration notes: selected - bootstrap command and operator evidence must be clear enough for M23 follow-up issues.

Domain risk packs:

- Geospatial / CRS / basin geometry: selected - station coordinates/elevation and output segment identities are spatial/domain metadata consumed later.
- Hydro-met time series / forcing windows: not selected - #254 seeds station identities only; per-cycle forcing values and time windows are #256.
- SHUD numerical runtime / conservation / NaN: not selected - no solver execution or numerical output in #254.
- PostGIS / TimescaleDB domain behavior: selected - bootstrap writes production DB domain rows and must preserve uniqueness/active-state semantics.
- Slurm production lifecycle / mock-vs-real parity: not selected - Slurm preflight/submission is #257/#258.
- External hydro-met providers / snapshot reproducibility: not selected - fresh source discovery/canonical completeness is #255.
- Run manifest / QC provenance: selected - package manifest/checksum/source-file identity must bind model bootstrap evidence.
- Published NHMS artifacts / display identity: selected - output identities and package URIs become downstream display-readable publish inputs, without publishing artifacts in #254.

Boundary-surface checklist:

- Shared helper roots: bootstrap import/publish helpers, QHH package parser, station/output identity seeders.
- Public entrypoints: bootstrap command/script; scheduler `plan-production --plan --model-id <qhh_model_id>` discovery only.
- Read surfaces: `NHMS_BASINS_ROOT`, processed package files, `qhh.tsd.forc`, optional package manifest/checksum files, existing DB registry rows.
- Write/delete/overwrite surfaces: model instance activation, station rows, output identity rows, bootstrap evidence; no deletes except scoped idempotent updates to compatible QHH rows.
- Staging/publish/rollback surfaces: package URI/root identity only; no published display product creation.
- Producer/consumer evidence boundaries: bootstrap report, scheduler candidate evidence, future forcing/parser/publisher consumers.
- Stale-state/idempotency boundaries: repeated bootstrap, changed package identity, duplicate active model, partial station/output seed failure.
- Unchanged downstream consumers: #253 scheduler root/evidence behavior, #252 identity/URI helpers, M22 node-27 readonly display, future forcing/runtime/parser tasks.

Invariant Matrix

Governing invariant: QHH can become scheduler-ready only when one active model instance, its processed package identity, fixed forcing stations, and output segment identities all bind to the same basin/model/package version, and bootstrap failures must block before any downstream forecast, forcing, SHUD, parse, or publish work.
Source-of-truth identity/contract: `model_id`, `basin_id=qhh`, `basin_version_id`, `river_network_version_id`, `model_package_uri`, package root/digest, `shud_code_version`, project name, `resource_profile`, fixed forcing station index/file identity, and output river/segment identity mapping.
Surfaces:

- Producers: QHH bootstrap command/script, package import/publish helper, station seeder, output identity seeder.
- Validators/preflight: package/file existence checks, `qhh.tsd.forc` parser/count validator, active model uniqueness validator, scheduler model eligibility checks.
- Storage/cache/query: `core.model_instance`, basin/package/version records, `met.met_station`, output river/segment identity tables or registry fixtures.
- Public routes/entrypoints: bootstrap command/script and `nhms-pipeline plan-production --plan --model-id <qhh_model_id>`.
- Frontend/downstream consumers: future forcing producer, SHUD runtime, output parser/publisher, node-27 readonly display; no direct frontend change.
- Failure paths/rollback/stale state: missing package, malformed/station-count mismatch, incomplete metadata, duplicate active model, repeated bootstrap, changed package identity.
- Evidence/audit/readiness: bootstrap counts/report, scheduler candidate evidence, focused bootstrap tests, production scheduler dry-run evidence.
Regression rows:

- Existing QHH package with valid `qhh.tsd.forc` and no active model -> creates/activates one scheduler-ready model, seeds stations/output identities, and reports created counts.
- Repeated bootstrap against the same package identity -> no duplicate active model/station/output rows; reports unchanged or updated counts.
- Missing package/project file or malformed/station-count-mismatched `qhh.tsd.forc` -> typed blocker; no active scheduler-ready model and no future-cycle forcing rows.
- Duplicate active QHH model identity -> typed duplicate-active-model blocker; scheduler submits no forecast/forcing/SHUD work.
- Bootstrapped QHH model -> `plan-production --plan --model-id <qhh_model_id>` includes the model without `not_shud_model`, `not_runnable`, or `incomplete_model_metadata`.
- Unchanged #253 no-flag scheduler root behavior and #252 identity/URI helpers -> remain compatible.

## Issue #255 Fixture

Issue type: feature/ingestion
Project profile: NHMS, with external hydro-met provider and forcing-window surfaces
Blast radius: high
Fixture level: expanded
Repair intensity: high

Change surface:

- Forecast source discovery and adapter policy for GFS/IFS cycle lookback, lag, horizon, forbidden/unavailable responses, and operator source/model/basin filters.
- Canonical conversion/store readiness checks for QHH-required source-specific variable ids and per-valid-time lead coverage.
- Production scheduler candidate evidence that records source availability, reduced-scope filters, canonical completeness, retryable/permanent/policy-blocked states, and no downstream forcing/SHUD readiness when incomplete.
- Focused adapter/canonical tests plus `tests/test_production_scheduler.py` and `tests/test_orchestration_chain.py`.

Must preserve:

- #252 production identity/status/URI contract and #253 scheduler root/no-mutation dry-run behavior.
- #254 active QHH model/package/station/output identity semantics; #255 may consume those identities but must not mutate model bootstrap state.
- No station forcing interpolation, no `met.forcing_version` or `met.forcing_station_timeseries` production, no SHUD/Slurm submission, no parse/publish, and no frontend display behavior in #255.
- Existing deterministic/mock tests must not claim live business readiness when source availability or canonical coverage is synthetic, incomplete, reduced-scope, or blocked.

Must add/change:

- Source-specific GFS/IFS discovery policy with auditable defaults or config values for lookback, lag, accepted horizon, max cycles, and operator filters.
- Typed evidence for unavailable/forbidden/stale/source-blocked cycles, including the probed source/cycle identity and whether the condition is retryable, unavailable, permanent, or policy-blocked.
- Exact canonical completeness gates:
  - GFS requires `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `pressure_surface`, and `shortwave_down`.
  - IFS requires `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `surface_pressure`, and `shortwave_down`.
- Per-valid-time lead coverage checks before a canonical product can feed future forcing generation, with safe missing variable/lead details.
- Idempotent reuse of completed canonical products for the same source/cycle/object/policy identity, and no duplicate rows unless source identity or policy changes.

Selected risk packs:

- Public API / CLI / script entry: selected - scheduler discovery and plan evidence are public operational entrypoints for source readiness.
- Config / project setup: selected - source filters, lookback, lag, horizon, retry policy, and provider enablement must be configurable and auditable.
- File IO / path safety / overwrite: selected - forecast download/cache/object references and evidence files must remain bounded and safe even when providers return partial data.
- Schema / columns / units / field names: selected - canonical variable ids, valid-time/lead coverage, forecast cycle status, and evidence payload fields become downstream contracts.
- Auth / permissions / secrets: selected - provider credentials or restricted endpoints must not leak when 403/forbidden/source-unavailable evidence is recorded.
- Concurrency / shared state / ordering: selected - repeated scheduler scans must reuse complete canonical products and avoid duplicate/inconsistent readiness state.
- Resource limits / large input / discovery: selected - cycle discovery, lead enumeration, retry loops, and provider probes must be bounded by policy.
- Legacy compatibility / examples: selected - existing scheduler/orchestration tests and accepted canonical fixtures must remain compatible.
- Error handling / rollback / partial outputs: selected - incomplete canonical coverage must block downstream forcing/SHUD without partial readiness.
- Release / packaging / dependency compatibility: selected - adapter or canonical-store changes must not require unavailable provider libraries in default CI.
- Documentation / migration notes: selected - run/evidence semantics must distinguish live unavailable/BLOCKED from deterministic reduced-scope checks.

Domain risk packs:

- Geospatial / CRS / basin geometry: not selected - #255 handles forecast source/canonical readiness only; spatial station interpolation is #256.
- Hydro-met time series / forcing windows: selected - valid-time/lead coverage and source-specific horizons are the core gate.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD execution or hydro output in #255.
- PostGIS / TimescaleDB domain behavior: selected - forecast cycle and canonical product readiness rows may be persisted or queried for downstream stages.
- Slurm production lifecycle / mock-vs-real parity: not selected - Slurm preflight/submission is #257/#258.
- External hydro-met providers / snapshot reproducibility: selected - GFS/IFS discovery, forbidden/unavailable handling, object identity, and retry semantics are provider-bound.
- Run manifest / QC provenance: selected - canonical readiness evidence must bind to source/cycle/object/policy identity.
- Published NHMS artifacts / display identity: not selected - no display artifact publication in #255.

Boundary-surface checklist:

- Shared helper roots: forecast discovery policy helpers, canonical readiness helpers, scheduler candidate evidence builders.
- Public entrypoints: `nhms-pipeline plan-production --plan` and scheduler source/model/basin filter inputs.
- Read surfaces: provider probes, cached/downloaded forecast object references, canonical product metadata, configured policy values, QHH active model identity.
- Write/delete/overwrite surfaces: forecast cycle/canonical readiness status and bounded evidence/cache metadata only; no station forcing, SHUD, hydro, parse, or publish mutation.
- Staging/publish/rollback surfaces: no display publish; partial download/canonical artifacts must not be promoted to canonical-ready.
- Producer/consumer evidence boundaries: source adapters, canonical converter/store, scheduler candidate evidence, future forcing producer.
- Stale-state/idempotency boundaries: repeated scans, completed canonical reuse, transient retry state, policy changes, source/object identity changes.
- Unchanged downstream consumers: #254 bootstrap model discovery, #256 forcing producer contract, SHUD/Slurm/parser/publisher stages, node-27 readonly display.

Invariant Matrix

Governing invariant: A QHH forecast cycle may become canonical-ready for future forcing only when the selected source/cycle/policy identity has exact source-specific variable ids and complete per-valid-time lead coverage; otherwise the scheduler must record typed blocked/unavailable/retryable evidence and submit no downstream forcing, SHUD, parse, or publish work.
Source-of-truth identity/contract: `source`, `cycle_time`, source object/checksum or cache identity, configured lookback/lag/horizon policy, operator filters, canonical variable id set, valid-time/lead coverage matrix, `canonical_product_id`, and QHH `model_id`/`basin_id`.
Surfaces:

- Producers: GFS/IFS discovery adapters, download/canonical conversion orchestration, canonical product writer or fixture.
- Validators/preflight: source policy validators, canonical variable-id exact-set checker, valid-time/lead coverage checker, retry/permanent/policy-block classifier.
- Storage/cache/query: `met.forecast_cycle`, `met.canonical_met_product`, object/cache references, scheduler pass evidence.
- Public routes/entrypoints: scheduler CLI/source filters and plan-production evidence; no frontend/API route change expected.
- Frontend/downstream consumers: future forcing producer reads only canonical-ready complete products; node-27 remains unchanged.
- Failure paths/rollback/stale state: 403/forbidden, unavailable/stale cycle, transient download failure, incomplete variables/leads, repeated scan reuse, policy change invalidation.
- Evidence/audit/readiness: scheduler evidence, canonical readiness fixtures, adapter/canonical tests, orchestration-chain tests.
Regression rows:

- Available GFS cycle with all required GFS variables and complete lead/valid-time coverage -> canonical-ready evidence with source/cycle/policy identity and downstream forcing may proceed later.
- Available IFS cycle with all required IFS variables and configured shorter horizon coverage -> canonical-ready or reduced-scope evidence according to IFS policy, with accepted horizon recorded.
- Missing required variable or lead time -> canonical blocked/incomplete evidence with safe missing-variable/lead details; no forcing/SHUD candidate is submitted.
- Provider 403, unavailable, stale, unsupported, or policy-filtered cycle -> typed unavailable/policy-blocked/permanent evidence with no fabricated canonical-ready state.
- Transient download failure -> retryable evidence with attempt/next-retry behavior and no downstream mutation.
- Repeated scan for identical completed canonical product identity -> reuse existing canonical product without duplicate rows or redownload; changed source object or policy identity does not falsely reuse stale readiness.
- Existing #254 bootstrapped QHH model discovery and #252/#253 no-mutation scheduler evidence -> remain compatible.

## Risks / Trade-offs

- Forecast source 403/lag or partial variables can block a cycle. Mitigation: source availability and canonical completeness are recorded as blocked/unavailable without marking readiness true.
- SHUD binaries on node 22 may have missing shared libraries. Mitigation: binary/library preflight fails before pipeline mutation or Slurm submission and records exact missing libraries without leaking secrets.
- Slurm may be reachable only from the host, not the container. Mitigation: support a bounded gateway/host-service pattern with health/accounting receipts instead of requiring Slurm CLI in the app image.
- Existing DB bootstrap scripts may be QHH-specific. Mitigation: M23 allows QHH-specific bootstrap for this closure, but scheduler runtime must consume generic model/registry records afterwards.
- Live E2E can be slow or blocked by external systems. Mitigation: tests separate deterministic fixtures from opt-in live receipts and cannot claim business readiness from deterministic-only runs.

## Migration Plan

1. Define the end-to-end production identity/status/URI contract so scheduler, forcing, Slurm, parser, publisher, and evidence use the same run/model/source/cycle keys.
2. Add/fix bootstrap commands and validation so QHH model/station/output identities are present and idempotent in the node-22 DB.
3. Fix scheduler env defaults and Docker scheduler commands so dry-run works without manual workspace flags.
4. Prove fresh forecast download/canonical readiness and station forcing generation for at least one accepted QHH cycle.
5. Configure real SHUD executable/library path and Slurm gateway/host submission path; add pre-submit preflight and accounting/log receipt capture.
6. Parse SHUD output, publish q_down display products/logs/manifests, separately mark frequency/flood products unavailable or ready, and validate strict run identity for downstream display.
7. Add node-22 E2E command/tests and update runbooks with pass/blocked evidence locations.

Rollback is operational: disable scheduler timer/container, leave DB terminal evidence intact, and revert to diagnostic scripts only for investigation. Published artifacts are append-only by run identity and should not be deleted as rollback unless explicitly marked invalid.

## Open Questions

- Which SHUD binary is the accepted production executable on node 22 after library resolution: `SHUD/shud`, `/scratch/frd_muziyao/SHUD-GPU/shud_omp`, or another managed path?
- Is the production Slurm gateway expected to run as a host systemd service, an API sidecar with mounted Slurm/Munge, or direct host CLI invoked outside Docker?
- What GFS/IFS horizon and cycle lag should be the default business policy for QHH once live source availability is stable?
