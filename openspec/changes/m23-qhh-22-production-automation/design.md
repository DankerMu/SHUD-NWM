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

M23-1 defines a single QHH production contract before any worker/runtime issue can add mutation. Every downstream stage must carry the same identity tuple: `run_id`, `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `source`, `cycle_time`, `canonical_product_id`, `forcing_version_id`, `hydro_run_id`, `published_manifest_id`, and `pipeline_job_id`/`pipeline_event` correlation. A later issue may add fields only by preserving this tuple and documenting migration behavior in this change.

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
Source-of-truth identity/contract: `run_id` plus `model_id`, `source`, `cycle_time`, `basin_version_id`, `river_network_version_id`, `canonical_product_id`, `forcing_version_id`, `hydro_run_id`, `published_manifest_id`, and pipeline job/event correlation.
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
- Same `run_id` with mismatched `model_id`, `source`, `cycle_time`, basin/river version, canonical product, forcing version, hydro run, manifest, or pipeline job -> rejected as identity mismatch before evidence reuse.
- `published://` or allowlisted published-root URI bound to the same identity -> accepted as display-readable boundary evidence.
- Workspace-only, scratch-only, Slurm-private, traversal, or non-allowlisted local path -> rejected as display-readable artifact/log evidence.
- Existing M22 readonly DB/published artifact consumer -> remains compatible and is not required to mount node-22 private workspace paths.

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
