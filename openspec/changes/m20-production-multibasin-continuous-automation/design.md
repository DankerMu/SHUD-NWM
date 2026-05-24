## Context

The repository already has a service-oriented orchestrator, real/mock Slurm gateway, production-closure validation lanes, Basins model registry, and worker implementations for GFS/IFS, canonical conversion, forcing, SHUD runtime, output parsing, frequency, and publishing. PR #190 adds a qhh-specific standard reproduction path and documents live evidence for GFS/IFS 00Z and 06Z. The next production step is to convert that proof into generic backend automation for all active registered Basins/SHUD model instances.

## Decisions

### Scheduler Boundary

Continuous automation belongs in the backend orchestration layer, not in a basin-specific script. The service scheduler SHALL discover source cycles and runnable model instances, create deterministic work candidates, and submit work through orchestrator/Slurm gateway contracts. `scripts/run_qhh_continuous.py` can remain a diagnostic fallback but MUST NOT be the production scheduler dependency.

### Candidate Identity

The canonical candidate identity is:

```text
{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}
```

Run ids and forcing ids continue to use existing deterministic conventions:

```text
fcst_{source_lower}_{YYYYMMDDHH}_{model_id}
forc_{source_lower}_{YYYYMMDDHH}_{model_id}
```

Where two model instances share a `model_id` conflict is already invalid registry state; the scheduler must reject duplicate active model identities rather than generate ad hoc suffixes.

### Source Scope

Initial production scope is GFS and IFS. GFS and IFS may have different forecast horizons and availability lag. Unavailable IFS cycles are first-class `unavailable`/`blocked` evidence, not synthetic success and not silent skip.

### Execution Model

Heavy execution defaults to Slurm. The scheduler may submit shared source-level stages once per source/cycle, then array stages per model where the existing orchestrator supports it:

```text
download -> canonical -> forcing[] -> forecast[] -> parse[] -> frequency[] -> publish
```

Frequency is array-capable per model. Display/tile publication remains a cycle-level publish stage unless a later change defines a per-model publish contract. For smaller initial implementation, separate per-model jobs are acceptable only when the evidence records the non-array mode and does not regress the final array-capable contract.

### Database and Object Store Preflight

Slurm execution requires a compute-node reachable `DATABASE_URL` and object-store/workspace roots. Localhost database URLs are rejected for Slurm mode. Runtime artifacts must remain under project-configured workspace/object-store roots or production object storage, never system disk defaults.

### State and Idempotency

State is persisted in database tables and events, not only filesystem JSON. `unavailable` and `blocked` are scheduler reason codes stored in pipeline/event details unless a migration explicitly extends the relevant database enum; they are not written directly into `met.cycle_status` values that lack those enum members. Repeated scheduler scans must:

- skip terminal success candidates;
- detect active submitted/running Slurm jobs;
- resume after downstream parse/publish failures without re-running a successful SHUD execution when durable output exists;
- retry failed/unavailable candidates according to configured policy;
- preserve partial success for multi-basin cycles using existing M3 reduced-manifest and `_partial` aggregate-state semantics;
- treat `hydro.hydro_run` `succeeded`, `parsed`, `frequency_done`, and `published` as durable successful stage states according to the downstream retry point.

### Model-Run Assembly

Production model-run assembly reuses Basins registry and model package data. Basin-specific assumptions like qhh forcing station seeding, SHUD output-river identity, and display product handling must become reusable contracts driven by model metadata or package artifacts. Missing optional products must become explicit unavailable/quality states rather than fabricated data.

### Evidence and Operations

Each scheduler pass emits structured evidence covering candidates, selected/skipped reasons, source availability, submitted job ids, array task summaries, Slurm accounting, resource metrics, forcing station counts, SHUD output row counts, parse status, frequency/display status, and residual blockers. Fast validation uses deterministic fixtures and unit/integration tests; live multi-cycle reruns remain opt-in.

Resource metrics that do not fit existing `ops.pipeline_job` columns are recorded in `ops.pipeline_event.details` and scheduler evidence artifacts unless an implementation issue adds a migration. Evidence must label deterministic fixture runs separately from opt-in live executions and must not set final production readiness to true without accepted live receipts.

## Risks and Mitigations

- **Risk: qhh script logic diverges from production orchestration.** Mitigation: encode the reusable behavior in orchestrator/workers and keep qhh script as diagnostic evidence only.
- **Risk: duplicate cycle scans submit duplicate jobs.** Mitigation: enforce candidate identity and active-state locks in DB before Slurm submission.
- **Risk: Slurm jobs cannot write back to local PG.** Mitigation: reject localhost DB URLs in Slurm mode and record preflight blockers.
- **Risk: array partial failures hide basin failures.** Mitigation: persist task-level results and aggregate cycle state as partial, not success.
- **Risk: fast CI overclaims production readiness.** Mitigation: deterministic tests verify contracts; full live GFS/IFS/SHUD multi-cycle runs are opt-in evidence.

## Open Questions

- Whether production scheduler should be a long-running API-managed service, a cron/systemd command, or both. The implementation issues should support a command first and expose service/API hooks where existing patterns make it cheap.
- Whether frequency/display publication should be a separate array stage or merged into parse for the first production implementation. The contract requires evidence either way.

## Issue #192 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestration
Repair intensity: high

Change surface:
- Scheduler public entrypoint and command/service configuration.
- Model registry discovery queries.
- GFS/IFS cycle discovery and candidate identity.
- Scheduler pass locking/lease behavior.
- Dry-run evidence and non-mutating planning.

Must preserve:
- Existing qhh diagnostic runner remains a diagnostic path, not the production scheduler dependency.
- Existing orchestrator source/scenario/run-id conventions stay compatible with `scenario_for_source`, `cycle_id_for`, `fcst_{source_lower}_{YYYYMMDDHH}_{model_id}`, and `forc_{source_lower}_{YYYYMMDDHH}_{model_id}`.
- Existing DB enums are not given unsupported `unavailable`/`blocked` values.

Must add/change:
- A production scheduler entrypoint can plan one-shot or continuous passes.
- Default discovery covers every active runnable registered SHUD model unless an explicit operator filter is supplied and evidenced.
- Candidate identity is deterministic across repeated scans.
- Lock/lease behavior prevents duplicate concurrent pass submission.
- Dry-run mode proves no download, no Slurm submission, no SHUD execution, and no hydro/met result mutation.

Risk packs considered:
- Public API / CLI / script entry: selected - new scheduler entrypoint and operator-facing dry-run/continuous mode.
- Config / project setup: selected - source/cycle/filter/lookback/lag settings and root/DB assumptions.
- File IO / path safety / overwrite: selected - pass evidence artifacts and lock/lease files or DB leases may touch filesystem/state.
- Schema / columns / units / field names: selected - candidate identity, run id, forcing id, status/reason fields, registry fields.
- Geospatial / CRS / shapefile sidecars: not selected - #192 only discovers registered models, does not parse geometry.
- Time series / forcing / temporal boundaries: selected - source cycle windows, UTC cycle times, lookback/lag/horizon behavior.
- Numerical stability / conservation / NaN: not selected - no solver execution in #192.
- Solver runtime / performance / threading: not selected - no SHUD runtime execution in #192.
- Resource limits / large input / discovery: selected - registry discovery may cover many basins; cycle discovery and evidence must be bounded.
- Legacy compatibility / examples: selected - qhh diagnostic behavior and existing orchestrator conventions must remain compatible.
- Error handling / rollback / partial outputs: selected - lock contention, unavailable source, duplicate identity, and dry-run failures need stable behavior.
- Release / packaging / dependency compatibility: not selected - no packaging/dependency changes expected.
- Documentation / migration notes: selected - issue should document scheduler command and dry-run semantics if introduced.

Invariant Matrix

Governing invariant: a scheduler scan must map the configured GFS/IFS cycle window and active runnable registry models to one deterministic, non-duplicated candidate set, and dry-run planning must have no runtime side effects.

Source-of-truth identity/contract: `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}` candidate identity; deterministic `run_id`/`forcing_version_id`; active model registry rows; scheduler pass id and lock/lease id.

Surfaces:
- Producers: scheduler candidate builder and source cycle discovery.
- Validators/preflight: source/model filters, duplicate model detection, UTC cycle parsing, lock/lease acquisition.
- Storage/cache/query: model registry reads, optional scheduler pass evidence, optional lock/lease storage.
- Public routes/entrypoints: scheduler CLI/service entrypoint.
- Frontend/downstream consumers: existing orchestrator and monitoring consumers read unchanged pipeline conventions.
- Failure paths/rollback/stale state: lock contention, unsupported source/status, unavailable IFS cycle reason, duplicate active model identity, dry-run failure.
- Evidence/audit/readiness: dry-run/planning evidence with selected/excluded counts, filters, no-mutation proof, and deterministic execution mode.

Regression rows:
- all active runnable models + GFS/IFS cycle window -> every runnable model produces stable candidate/run/forcing ids.
- explicit model/basin filter -> only matching models selected and excluded runnable count/filter expression appear in evidence.
- concurrent pass lock held -> second pass exits or reports lock contention without submitting candidates.
- dry-run pass -> no adapter download, no Slurm submit, no SHUD runtime, and no hydro/met result-table mutation.
- unavailable IFS cycle -> reason stored in evidence/event details, not unsupported `met.cycle_status`.
- duplicate active model identity -> stable rejection/exclusion reason before submission.

Boundary-surface checklist:
- Public entrypoints: scheduler one-shot/continuous/dry-run command or service hook.
- Config boundary: source list, cycle window, filters, lookback/lag/max-cycle values.
- Storage boundary: registry read queries, pass evidence, lock/lease storage.
- Stale-state/idempotency boundary: deterministic candidate identity and repeated scan behavior.
- Evidence boundary: deterministic fixture evidence cannot claim live readiness.

## Issue #193 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestration with SHUD runtime workers
Repair intensity: high

Change surface:
- `services/orchestrator/scheduler.py` candidate metadata flowing into production execution.
- `services/orchestrator/chain.py` cycle orchestration, model-run assembly, manifest indexes, runtime manifest generation, partial status handling, and publish/frequency handoff.
- Worker contracts in `workers/forcing_producer`, `workers/shud_runtime`, `workers/output_parser`, `workers/flood_frequency`, and `services/tile_publisher` where existing tests require fixture-level compatibility.
- qhh fixture tests and runbook boundaries proving production automation does not call qhh-specific continuous scripts.

Must preserve:
- #192 deterministic candidate identity, run id, forcing version id, source/scenario conventions, dry-run no-mutation evidence, and duplicate active model rejection.
- Existing M3 array stage order: download, canonical, forcing[], forecast[], parse[], frequency[], then cycle-level publish.
- Existing worker manifests remain compatible with current SHUD runtime, output parser, frequency, and publish tests.
- qhh diagnostic shell scripts remain usable as diagnostic evidence only, never as a production scheduler dependency.

Must add/change:
- Production execution can assemble a reusable per-model run contract from registry/package metadata, source/cycle identity, forcing station metadata, SHUD project mode inputs, output URI, and parser/frequency/display handoff.
- Candidate model/package/forcing/runtime identity is bound end to end across manifest index, runtime manifest, hydro run creation, worker handoff, parser input, and publish/frequency evidence.
- Missing frequency curves, warning thresholds, station forcing, and optional weather/display inputs become explicit unavailable or quality states with residual blockers, not fabricated values.
- A focused qhh fixture proves the production path plans and executes the same standard chain shape through generic contracts without invoking qhh-specific continuous scripts or requiring a live full-chain rerun.

Risk packs considered:
- Public API / CLI / script entry: selected - scheduler candidates transition from planning to production execution and qhh scripts must stay out of the production path.
- Config / project setup: selected - workspace/object-store roots, source/model filters, model package URI, runtime paths, and worker command settings must remain explicit.
- File IO / path safety / overwrite: selected - manifests, runtime inputs, outputs, and publish artifacts are assembled from package/forcing/output URIs.
- Schema / columns / units / field names: selected - run manifest, forcing package, SHUD output river identity, parse rows, display/frequency state, and quality fields are contracts.
- Geospatial / CRS / shapefile sidecars: not selected - #193 reuses registered package metadata; geometry parsing is not changed.
- Time series / forcing / temporal boundaries: selected - source cycle time, forecast horizon, forcing station counts, and SHUD start/end windows must stay consistent.
- Numerical stability / conservation / NaN: not selected - no solver algorithm change; live solver correctness is outside the deterministic fixture scope.
- Solver runtime / performance / threading: selected - native SHUD project mode handoff, runtime manifest, and resource profile mapping affect worker execution.
- Resource limits / large input / discovery: selected - manifest indexes and qhh fixtures must stay bounded and deterministic.
- Legacy compatibility / examples: selected - qhh standard chain shape and existing worker/orchestrator tests must remain compatible.
- Error handling / rollback / partial outputs: selected - missing optional inputs and partial basin success require stable unavailable/quality states and no fabricated downstream products.
- Release / packaging / dependency compatibility: not selected - no dependency/package release change expected.
- Documentation / migration notes: selected - production scheduler automation must be distinguished from qhh diagnostic scripts where touched.

Invariant Matrix

Governing invariant: one scheduler candidate's source/cycle/model identity must propagate unchanged through model package resolution, forcing production, SHUD runtime manifest, hydro run record, parser/frequency/display handoff, and evidence; unavailable optional products must be explicit state, never synthetic data.

Source-of-truth identity/contract: `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}` candidate identity plus deterministic `run_id`, `forcing_version_id`, `model_package_uri`, `basin_version_id`, `river_network_version_id`, and output URI.

Surfaces:
- Producers: scheduler candidate builder; cycle basin/task manifest builders; runtime manifest builders.
- Validators/preflight: manifest validation, package/forcing URI validation, source/scenario normalization, qhh fixture production-path assertions.
- Storage/cache/query: object-store runtime manifests, workspace manifest indexes, `hydro.hydro_run`, forcing metadata, parsed/frequency/publish artifacts.
- Public routes/entrypoints: production scheduler command/service path and existing orchestrator public methods used by tests.
- Frontend/downstream consumers: output parser, flood frequency, tile publisher, monitoring/API consumers of hydro/pipeline status and display quality fields.
- Failure paths/rollback/stale state: missing package/forcing/station metadata, missing frequency curves/warning thresholds, optional weather/display absence, partial basin failures, stale qhh diagnostic script assumptions.
- Evidence/audit/readiness: scheduler/model-run evidence, qhh fixture evidence, quality/unavailable states, deterministic execution mode.

Regression rows:
- qhh active model candidate -> generic production chain shape uses registry/package metadata and does not invoke qhh-specific continuous scripts.
- candidate model/package/forcing identity -> manifest index, runtime manifest, hydro run, parser input, frequency handoff, publish evidence all carry the same run/model/source/cycle identifiers.
- missing frequency curves or warning thresholds -> explicit quality/unavailable state and residual blocker; no fabricated return periods or warning values.
- missing station forcing or optional weather/display product -> stable unavailable/quality evidence while successful durable outputs remain reusable where valid.
- partial model success in a cycle -> reduced downstream manifests and cycle-level publish over successful basins only.
- unchanged non-qhh model fixture -> existing orchestrator and worker tests still pass with the same manifest schema and status contracts.

Boundary-surface checklist:
- Shared helper roots: scheduler candidate ids, cycle manifest builders, runtime manifest builders, output URI helpers.
- Public entrypoints: scheduler production execution path and orchestrator cycle-run methods.
- Read surfaces: registry package metadata, forcing package metadata, runtime manifest reads, parser/frequency input reads.
- Write/delete/overwrite surfaces: workspace manifest indexes, object-store runtime manifests, hydro run records, parse/frequency/publish artifacts.
- Staging/publish/rollback surfaces: SHUD runtime staging, parse/frequency stages, cycle-level tile publish, partial aggregate state.
- Producer/consumer evidence boundaries: scheduler evidence, model-run evidence, worker manifests, quality/unavailable state fields.
- Stale-state/idempotency boundaries: deterministic candidate/run/forcing ids and output URI reuse across repeated qhh fixture scans.
- Unchanged downstream consumers: output parser, flood frequency, tile publisher, monitoring/API status readers.

## Issue #194 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestration with Slurm gateway integration
Repair intensity: high

Change surface:
- `services/orchestrator/scheduler.py` Slurm-enabled candidate submission and preflight blockers.
- `services/orchestrator/chain.py` stage submission, array-capable task manifests, partial downstream manifest reduction, and publish-stage aggregation.
- `services/slurm_gateway/*` real/mock backend contracts, sbatch template allowlist, export/env handling, status/accounting parsing, and cancellation/status fields where touched.
- `services/orchestrator/persistence.py` and `ops.pipeline_job` / `ops.pipeline_event` state and accounting evidence where existing fields support persistence.
- Tests for Slurm preflight rejection, safe export/env handling, array partial success, and accounting/resource evidence.

Must preserve:
- #192 deterministic candidate identity, lock/dry-run no-mutation behavior, and source/model discovery semantics.
- #193 model-run assembly identity/output URI/product-quality contracts and reduced-manifest partial behavior.
- Existing M3 stage order: `download -> canonical -> forcing[] -> forecast[] -> parse[] -> frequency[] -> publish`.
- Existing sbatch template allowlist and Slurm gateway security expectations; scheduler must not construct unsafe shell exports or bypass template validation.
- Display/tile publish remains cycle-level unless this issue adds and tests a new per-model publish contract; default scope is cycle-level publish.

Must add/change:
- Slurm mode performs compute-node preflight before submission: `DATABASE_URL` must be present and not localhost-only; workspace/object-store/log/runtime dependency roots must be configured, contained under allowed project/production roots, and suitable for compute-node visibility.
- Scheduler/orchestrator submit through the real/mock Slurm gateway when Slurm execution is enabled, with preflight blockers recorded as evidence and no Slurm job created on blocker.
- Forcing, forecast, parse, and frequency stages support array/task-level status and manifest indexes; downstream stages receive only successful eligible model entries after partial failures.
- Slurm job id, array task id, state, exit code, log URI, elapsed time, MaxRSS, and resource metrics are persisted in existing pipeline fields or `ops.pipeline_event.details` / scheduler evidence when no dedicated column exists.
- Safe env/export handling rejects secret leakage, shell injection, unsafe template names, and unbounded user/config values.

Risk packs considered:
- Public API / CLI / script entry: selected - scheduler Slurm mode and operator-facing preflight/cancellation/status paths are public operational entrypoints.
- Config / project setup: selected - database URL, workspace/object-store/log/runtime roots, Slurm templates, resource profiles, and compute-node visibility are configuration boundaries.
- File IO / path safety / overwrite: selected - sbatch scripts, log roots, workspace/object-store roots, manifest indexes, and evidence artifacts cross storage trust boundaries.
- Schema / columns / units / field names: selected - pipeline job/event details, Slurm ids, array task ids, state, exit code, elapsed, MaxRSS, and resource metric field names are persistent contracts.
- Geospatial / CRS / shapefile sidecars: not selected - this issue routes existing model artifacts and does not parse or transform geometry.
- Time series / forcing / temporal boundaries: selected - source/cycle identity and forcing/forecast task manifests must preserve cycle time and stage order.
- Numerical stability / conservation / NaN: not selected - no solver algorithm or numerical output computation changes are required.
- Solver runtime / performance / threading: selected - SHUD runtime is submitted under Slurm and resource/accounting evidence affects runtime operations.
- Resource limits / large input / discovery: selected - array fan-out, manifest indexes, Slurm polling/accounting, and log/evidence reads must be bounded.
- Legacy compatibility / examples: selected - qhh diagnostic scripts and existing non-Slurm/mock orchestrator tests must keep working.
- Error handling / rollback / partial outputs: selected - preflight blockers, partial array failure, submission failure, accounting gaps, and cancellation need stable evidence and no duplicate submission.
- Release / packaging / dependency compatibility: selected - compute-node runtime dependencies and sbatch template allowlist are deployment-sensitive.
- Documentation / migration notes: selected - any new Slurm mode/config expectations must be discoverable through OpenSpec/runbook or inline operator evidence; full operator docs are completed in #196.

Invariant Matrix

Governing invariant: Slurm-enabled production scheduling must either reject unsafe/unreachable execution before submission or submit bounded, identity-preserving stage jobs whose task-level outcomes and accounting evidence drive downstream partial manifests without duplicate or fabricated success.

Source-of-truth identity/contract: `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}` candidate identity plus deterministic `run_id`, `forcing_version_id`, stage name, Slurm job id, optional array task id, pipeline job id, and task manifest entry identity.

Surfaces:
- Producers: scheduler candidate execution; orchestrator stage manifest builders; Slurm job submission builders; mock/real gateway job records.
- Validators/preflight: database reachability checks, root/path containment and visibility checks, sbatch template allowlist, safe env/export serialization, runtime dependency checks.
- Storage/cache/query: `ops.pipeline_job`, `ops.pipeline_event.details`, scheduler evidence artifacts, workspace manifest indexes, Slurm log URI paths, object-store roots.
- Public routes/entrypoints: scheduler CLI/service run-once/continuous Slurm mode; `ForecastOrchestrator.orchestrate_cycle`; Slurm gateway submit/status/accounting/cancel methods.
- Frontend/downstream consumers: existing pipeline status readers, output parser/frequency/publisher consumers of reduced manifests, monitoring/API consumers of job status and accounting evidence.
- Failure paths/rollback/stale state: localhost/missing DB, missing/out-of-root roots, unsafe template/env, submission failure, partial array failure, accounting unavailable, cancellation, repeated scans with active Slurm jobs.
- Evidence/audit/readiness: preflight blocker evidence, submitted job/task evidence, partial aggregate evidence, Slurm accounting/resource metrics, deterministic fixture marker, no final live-readiness overclaim.

Regression rows:
- Slurm enabled + missing or localhost `DATABASE_URL` -> preflight blocker before Slurm submit, no pipeline job submitted as active.
- Slurm enabled + workspace/object-store/log/runtime root missing or outside allowed roots -> storage preflight blocker before Slurm submit.
- Slurm enabled + allowed template/resource profile/env -> gateway submit receives allowlisted template and shell-safe bounded env without leaking secrets.
- forcing/forecast/parse/frequency array with one failed task and successful siblings -> task states persist, downstream manifest contains only successful eligible siblings, aggregate state uses `_partial`.
- accounting available for job/array task -> pipeline job/event or scheduler evidence records job id, array task id, state, exit code, log URI, elapsed, MaxRSS/resource metrics.
- accounting unavailable or malformed -> stable evidence gap/blocker without crashing or fabricating metrics.
- repeated scan with active Slurm job for same candidate/stage -> skip/resume evidence, no duplicate submission.
- cancellation for active Slurm job -> gateway cancel called, cancelled state/event recorded, no replacement work in the same pass.
- unchanged non-Slurm/mock path -> existing dry-run, deterministic fixture, and mock-orchestrator tests keep passing.

Boundary-surface checklist:
- Shared helper roots: scheduler preflight/submission helpers, orchestrator stage submission helpers, Slurm gateway config/env/accounting parsers.
- Public entrypoints: scheduler Slurm mode CLI/service path, orchestrator cycle-run methods, Slurm gateway submit/status/accounting/cancel.
- Read surfaces: database URL/config, workspace/object-store/log/runtime roots, sbatch templates, resource profiles, Slurm status/accounting output, manifest indexes.
- Write/delete/overwrite surfaces: pipeline jobs/events, scheduler evidence files, Slurm log paths, workspace task manifests.
- Staging/publish/rollback surfaces: array stage task manifests, reduced downstream manifests, cycle-level publish after partial success, cancellation/failed submission evidence.
- Producer/consumer evidence boundaries: job/task status, accounting metrics, resource evidence, preflight blockers, downstream manifest eligibility.
- Stale-state/idempotency boundaries: active Slurm job detection, repeated scans, partial retry eligibility, cancellation no-replacement-in-pass.
- Unchanged downstream consumers: qhh diagnostic lane, non-Slurm orchestrator tests, output parser, flood frequency, tile publisher, monitoring/API status readers.

## Issue #195 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestration state and retry control
Repair intensity: high

Change surface:
- `services/orchestrator/scheduler.py` repeated-scan skip/resume/retry decisions, active Slurm detection, cancellation request handling, and pass evidence.
- `services/orchestrator/chain.py` durable stage status interpretation, partial array retry inputs, hydro run reuse, and downstream parse/frequency/publish restart points where touched.
- `services/orchestrator/persistence.py`, `services/orchestrator/retry.py`, and `ops.pipeline_job` / `ops.pipeline_event` state where retry/cancellation state is persisted or queried.
- API/operator retry and cancellation routes where scheduler-visible state, manual retry, or cancellation proof is exposed.
- Tests for terminal skip, active skip, unavailable retry, parse-after-SHUD resume, transient/permanent failure classification, manual retry, and cancellation no-replacement behavior.

Must preserve:
- #192 deterministic candidate identity, pass locking, dry-run no-mutation behavior, source/model discovery, and explicit filter evidence.
- #193 model-run identity, durable output URI reuse, quality/unavailable state handling, qhh diagnostic script boundary, and reduced-manifest partial behavior.
- #194 Slurm preflight, array task evidence, accounting gap semantics, cancel proof/no-replacement behavior, safe env/template/resource/log boundaries, and non-Slurm/mock compatibility.
- Existing M3 stage order and cycle-level publish default unless a later issue changes publish semantics.
- Retry/cancellation changes must not fabricate success, silently delete evidence, or resubmit terminal/active work by default.

Must add/change:
- Persist candidate/stage lifecycle state through DB-backed records/events and scheduler evidence so repeated scans can distinguish terminal success, active work, retryable transient/unavailable state, permanent failure, manual retry, and cancellation.
- Treat hydro run `succeeded`, `parsed`, `frequency_done`, and `published` as durable successful states for scheduler skip/restart decisions.
- Resume parse/frequency/publish after durable SHUD success without rerunning native SHUD by default; force rerun must be explicit.
- Classify retry policy outcomes for source unavailable, adapter failure, forcing failure, SHUD/runtime/Slurm transient failure, parse/display failure, non-transient/permanent failure, retry limit exhaustion, manual retry, and cancellation.
- Cancellation must call the Slurm cancellation contract where applicable, record proof or gap evidence, preserve local state when cancellation is unproven, and never submit replacement work in the same pass.

Risk packs considered:
- Public API / CLI / script entry: selected - scheduler run-once/continuous, retry, cancellation, and operator controls affect production state.
- Config / project setup: selected - retry limits, transient/permanent classifiers, force-rerun/manual retry flags, and Slurm active-state providers are configuration boundaries.
- File IO / path safety / overwrite: selected - retry/resume may reuse durable artifacts and evidence files; it must not overwrite or delete successful outputs unintentionally.
- Schema / columns / units / field names: selected - pipeline job/event status, hydro run status, forcing version status, retry counters, cancellation proof, and permanent failure reason fields are persistent contracts.
- Geospatial / CRS / shapefile sidecars: not selected - #195 does not parse or transform geometry.
- Time series / forcing / temporal boundaries: selected - source/cycle identity, unavailable retry windows, and restart stage ordering must preserve UTC cycle semantics.
- Numerical stability / conservation / NaN: not selected - no solver algorithm or numerical output computation changes are required.
- Solver runtime / performance / threading: selected - retry/resume controls whether native SHUD reruns and how transient runtime failures are retried.
- Resource limits / large input / discovery: selected - repeated scans, active job queries, retry candidate sets, and evidence must remain bounded.
- Legacy compatibility / examples: selected - qhh diagnostic scripts, existing retry routes, non-Slurm/mock tests, and worker/orchestrator contracts must remain compatible.
- Error handling / rollback / partial outputs: selected - unavailable state, transient failure, permanent failure, manual retry, cancellation, and partial outputs are the primary issue surface.
- Release / packaging / dependency compatibility: not selected - no package/dependency release change expected.
- Documentation / migration notes: selected - operator-facing retry/cancel semantics must be discoverable through evidence or docs touched by this issue; broader ops docs belong to #196.

Invariant Matrix

Governing invariant: repeated scheduler scans must make exactly one state transition per candidate/stage from persisted truth, never resubmitting terminal or active work, never rerunning durable successful upstream work unless explicitly requested, and never fabricating success or replacing unproven cancellation.

Source-of-truth identity/contract: `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}` candidate identity plus deterministic `run_id`, `forcing_version_id`, stage name, pipeline job id/status, hydro run id/status, forcing version id/status, Slurm job/task id/status, retry attempt, failure classification, cancellation proof, and manual retry marker.

Surfaces:
- Producers: scheduler candidate scanner, retry planner, cancellation planner, orchestrator stage outcome writers, Slurm/task outcome collectors.
- Validators/preflight: duplicate/active/terminal state checks, retry-limit checks, transient/permanent classifier, force-rerun/manual retry authorization, cancellation proof checks.
- Storage/cache/query: `ops.pipeline_job`, `ops.pipeline_event`, `hydro.hydro_run`, `met.forecast_cycle`, `met.forcing_version`, scheduler pass evidence, durable output URIs.
- Public routes/entrypoints: scheduler CLI/service run-once/continuous, retry route/service, cancel route/service, orchestrator cycle-run methods.
- Frontend/downstream consumers: monitoring/API status readers, retry/cancel API clients, output parser, flood frequency, tile publisher, qhh diagnostic consumers.
- Failure paths/rollback/stale state: terminal success, active submitted/running work, unavailable source, parse/display failure after SHUD success, transient Slurm/runtime failure, non-transient failure, retry exhaustion, manual retry, cancellation proof gap.
- Evidence/audit/readiness: skip/retry/cancel reasons, retry attempt and classifier evidence, durable output reuse evidence, permanent failure evidence, cancellation proof/gap, deterministic fixture marker.

Regression rows:
- repeated scan with terminal pipeline/hydro success (`succeeded`, `parsed`, `frequency_done`, `published`) -> skip with terminal reason, no Slurm/orchestrator submission.
- repeated scan with active submitted/running Slurm job -> active skip/resume evidence, no duplicate submission.
- parse/display failure after durable SHUD output -> retry starts at parse/frequency/publish point and does not rerun native SHUD by default.
- source unavailable candidate -> retryable unavailable evidence distinct from model/runtime failure and no unsupported DB enum state.
- transient Slurm/runtime/task failure within retry limit -> retry scoped to failed candidate/task/stage, successful siblings/durable outputs reused.
- non-transient, malformed input, policy blocked, or retry-limit-exhausted failure -> permanent failure evidence and automatic retry stops.
- manual retry marker for permanent/blocked candidate -> explicit retry allowed, attempt evidence increments, and prior failure reason remains auditable.
- cancellation request for active job -> Slurm cancel contract called, proof or gap recorded, local state preserved on unproven cancellation, and no replacement work submitted in same pass.
- unchanged non-Slurm/mock/API consumers -> existing dry-run, mock gateway, retry route, cancel route, and monitoring tests keep passing.

Boundary-surface checklist:
- Shared helper roots: scheduler state classifier, retry planner, retry service, cancellation helpers, orchestrator stage status helpers, Slurm active-job query helpers.
- Public entrypoints: scheduler run-once/continuous, retry API/service, cancel API/service, orchestrator cycle-run methods.
- Read surfaces: pipeline job/event state, hydro run status/output URI, forcing version status, forecast cycle status, Slurm job/accounting status, retry counters, manual retry flags.
- Write/delete/overwrite surfaces: pipeline jobs/events, hydro/met state updates, retry/cancel evidence, scheduler pass evidence; no deletion/overwrite of durable successful artifacts by default.
- Staging/publish/rollback surfaces: parse/frequency/publish restart points, partial downstream manifests, permanent failure and cancellation rollback/gap evidence.
- Producer/consumer evidence boundaries: skip/retry/cancel reason codes, attempt counts, classifier details, durable output reuse, cancellation proof/gap.
- Stale-state/idempotency boundaries: repeated scans, concurrent/active jobs, retry exhaustion, manual retry, cancellation no-replacement-in-pass.
- Unchanged downstream consumers: qhh diagnostic lane, non-Slurm orchestrator tests, output parser, flood frequency, tile publisher, monitoring/API status readers.

## Issue #196 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestration evidence and readiness validation
Repair intensity: high

Change surface:
- `services/orchestrator/scheduler.py` scheduler pass/model-run evidence shaping, artifact writing, execution-mode labeling, redaction, and dry-run no-mutation proof where evidence is emitted.
- `services/production_closure/readiness_validation.py` readiness evidence ingestion, deterministic/live truth table, release-blocker summary, and final readiness claim calculation where scheduler evidence is consumed.
- Scheduler CLI/operator documentation in `docs/VALIDATION.md`, `docs/runbooks/qhh-continuous.md`, and `progress.md`.
- Tests for structured scheduler evidence, readiness ingestion, deterministic-vs-live no-overclaim behavior, dry-run documentation/output expectations, and unchanged qhh diagnostic boundaries.

Must preserve:
- #192 deterministic candidate identity, pass locking, dry-run no-download/no-Slurm/no-SHUD/no-hydro-met-mutation evidence, source/model discovery, and explicit operator filter evidence.
- #193 model-run identity, durable output URI reuse, quality/unavailable state handling, qhh diagnostic script boundary, reduced-manifest partial behavior, and no fabricated optional products.
- #194 Slurm preflight, array task accounting/resource evidence, safe env/template/resource/log boundaries, cancellation proof/no-replacement behavior, and non-Slurm/mock compatibility.
- #195 terminal/active skip, retry/manual retry/cancellation semantics, retry evidence, and durable upstream reuse.
- M19 readiness truth table: deterministic, dry-run, simulated, or production-like scheduler evidence never sets `final_production_readiness_claimed=true`; final readiness requires accepted live proof receipts.

Must add/change:
- Scheduler pass and per-model evidence include execution mode, deterministic/live receipt references only when applicable, pass/candidate/model counts, filters, skip/block/retry/cancel reasons, artifact paths, forcing station counts, canonical counts, parsed row counts, segment counts, display/frequency quality states, Slurm accounting/resource metrics, and residual blockers.
- Evidence artifacts are bounded, redacted, stable-schema JSON and remain under configured evidence/workspace roots.
- Readiness validation can ingest scheduler evidence as deterministic review evidence without requiring live multi-cycle reruns and without treating it as accepted live proof.
- Fast validation and docs clearly separate deterministic scheduler evidence, qhh diagnostic script evidence, opt-in live scheduler receipts, and final production readiness.
- Operator docs describe dry-run command/output and its explicit no-download/no-Slurm/no-SHUD/no-hydro-met-mutation contract.

Risk packs considered:
- Public API / CLI / script entry: selected - scheduler CLI output and readiness validation command are operator/release-review entrypoints.
- Config / project setup: selected - evidence roots, execution mode, live receipt references, validation inputs, and dry-run/continuous options are config boundaries.
- File IO / path safety / overwrite: selected - scheduler/readiness evidence artifacts are written/read from configured roots and must be bounded, no-clobber safe, redacted, and contained.
- Schema / columns / units / field names: selected - evidence schemas, counts, execution modes, readiness item fields, release blocker fields, resource metric names, and truth-table booleans are persistent review contracts.
- Geospatial / CRS / shapefile sidecars: not selected - #196 does not parse or transform geometry sidecars.
- Time series / forcing / temporal boundaries: selected - source/cycle windows, UTC cycle times, forcing station counts, parsed row counts, and deterministic/live cycle evidence must not collapse across cycles.
- Numerical stability / conservation / NaN: not selected - no solver math or hydrologic computation changes are required.
- Solver runtime / performance / threading: selected - evidence must report SHUD/runtime/resource state without rerunning live solver in fast validation.
- Resource limits / large input / discovery: selected - evidence ingestion, artifact reads, JSON payloads, candidate lists, and docs examples must stay bounded.
- Legacy compatibility / examples: selected - qhh continuous scripts remain diagnostic; M10/M19 validation docs and existing tests remain interpretable.
- Error handling / rollback / partial outputs: selected - residual blockers, partial model/cycle states, evidence gaps, malformed evidence, and missing live receipts need stable statuses without fabricated success.
- Release / packaging / dependency compatibility: selected - readiness validation and docs are release-decision surfaces.
- Documentation / migration notes: selected - this issue primarily updates operator docs, validation docs, progress, and runbook boundaries.

Invariant Matrix

Governing invariant: scheduler evidence may prove deterministic production-automation contracts and support release review, but only accepted live receipts bound to the readiness run may satisfy final production readiness; every evidence item must preserve source/cycle/model identity, execution mode, blockers, and artifact provenance without leaking secrets or fabricating live success.

Source-of-truth identity/contract: `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}` candidate identity plus scheduler `pass_id`, deterministic `run_id`, `forcing_version_id`, evidence schema/version, execution mode, artifact path/ref, live receipt id/checksum where applicable, readiness item `required_for_final`, and `live_proof_accepted`.

Surfaces:
- Producers: scheduler pass evidence builder/writer; model-run evidence builder; orchestrator/Slurm/accounting evidence producers where surfaced.
- Validators/preflight: dry-run no-mutation proof, evidence payload/root containment checks, readiness item schema validation, live receipt binding checks, deterministic-vs-live mode validation.
- Storage/cache/query: scheduler evidence files, `ops.pipeline_job`/`ops.pipeline_event.details` evidence references, readiness evidence bundle, consumed scheduler summary/root paths.
- Public routes/entrypoints: `nhms-pipeline plan-production` dry-run/plan/continuous output; `nhms-production validate-readiness`; operator docs and runbook commands.
- Frontend/downstream consumers: monitoring/API status readers remain compatible; release reviewers consume `docs/VALIDATION.md`, `progress.md`, runbooks, and readiness summary.
- Failure paths/rollback/stale state: malformed/oversized scheduler evidence, missing artifact, stale or mismatched run/cycle/model identity, missing live receipt, partial cycle, accounting gap, residual blocker, dry-run or deterministic evidence attempting to claim live readiness.
- Evidence/audit/readiness: scheduler pass artifacts, model-run artifacts, readiness items, release blockers, live proof receipts, deterministic summary ingestion, docs truth table.

Regression rows:
- scheduler dry-run pass -> evidence reports selected candidates, filters, skip/block reasons, artifact path, and no download/Slurm/SHUD/hydro-met mutation proof.
- scheduler submitted/partial/blocked pass -> pass and model-run evidence include execution mode, counts, source/cycle/model ids, artifact refs, stage/resource/accounting metrics where available, and residual blockers without leaking secrets.
- deterministic scheduler evidence consumed by readiness validation -> readiness item is deterministic/non-final and `final_production_readiness_claimed=false` when live receipts are absent.
- scheduler evidence with accepted live receipt binding -> readiness records receipt refs/checksum as live proof input only when schema, run id, target environment, producer artifact/ref, and execution mode match the M19 live proof contract.
- malformed, oversized, stale, or identity-mismatched scheduler evidence -> stable blocked/release_blocked evidence with redacted reason and no final readiness claim.
- qhh continuous script evidence/runbook -> remains diagnostic and must not be described as the production scheduler dependency or as final readiness proof.
- unchanged M19 readiness producer summaries -> existing Slurm/object-store/source/E2E/MVT deterministic summaries still ingest with the same final readiness truth table.
- unchanged monitoring/API/orchestrator consumers -> existing scheduler/orchestrator/retry/cancel tests keep passing with the same evidence/status contracts.

Boundary-surface checklist:
- Shared helper roots: scheduler evidence builder/writer, readiness dependency/evidence ingestion, redaction and bounded JSON helpers.
- Public entrypoints: scheduler CLI dry-run/plan/continuous output, readiness validation command, docs/runbook commands.
- Read surfaces: scheduler evidence roots/files, readiness dependency roots, live receipt files, pipeline event details, Slurm accounting/resource evidence.
- Write/delete/overwrite surfaces: scheduler evidence artifacts and readiness bundle files; no overwrite without existing force/no-clobber semantics.
- Producer/consumer evidence boundaries: scheduler pass/model evidence -> readiness ingestion -> release blockers/summary/docs.
- Stale-state/idempotency boundaries: pass id/run id/candidate id/cycle/model identity and stale receipt/evidence mismatch handling.
- Unchanged downstream consumers: M19 readiness tests/docs, qhh diagnostic lane, monitoring/API status readers, orchestrator retry/cancel behavior.
