## 1. Scheduler Facade Deepening

- [x] 1.1 Scheduler compatibility guard and parity fixture.
  - Module/Scope: `services/orchestrator/scheduler.py` facade guard plus scheduler compatibility inventory assertions.
  - Dependencies: None.
  - Out of Scope: moving implementation behavior.
  - Fixture Level: expanded; Repair Intensity: high, because this guards a shared scheduler compatibility facade,
    legacy import/monkeypatch paths, and governance evidence that later scheduler extraction tasks rely on.
  - Selected Risk Packs: Public API / CLI / script entry (stable `ProductionScheduler` facade);
    Legacy compatibility / examples (old `services.orchestrator.scheduler` imports and monkeypatches);
    Schema / columns / units / field names (entropy guard JSON/markdown signal shape);
    Concurrency / shared state / ordering (state-helper monkeypatch wrappers use shared compatibility bindings and must keep old call ordering);
    Documentation / migration notes (inventory rows remain the authority);
    Error handling / rollback / partial outputs (audit remains report-only and never writes `.entropy-baseline/latest.json`).
    Not Selected: Auth / permissions / secrets, File IO / path safety / overwrite, Config / project setup,
    Resource limits / large input / discovery, Release / packaging / dependency compatibility,
    Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
    SHUD numerical runtime / conservation / NaN, PostGIS / TimescaleDB domain behavior,
    Slurm production lifecycle / mock-vs-real parity, External hydro-met providers / snapshot reproducibility,
    Run manifest / QC provenance, Published NHMS artifacts / display identity - no runtime, provider, DB, Slurm,
    artifact, or frontend behavior changes are in scope.
  - Invariant Matrix: Governing invariant: every scheduler facade compatibility surface that grows or changes is either
    covered by `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` guard-hook metadata or reported by the entropy audit
    before merge. Source-of-truth identity/contract: `services/orchestrator/scheduler.py` facade symbols plus the scheduler
    inventory `Guard Hook Seed` rows. Surfaces: Producers: `services/orchestrator/scheduler.py`;
    Validators/preflight: `scripts/governance/audit_repo_entropy.py` and `tests/test_entropy_audit_script.py`;
    Storage/cache/query: none - report-only audit; Public routes/entrypoints: `ProductionScheduler`,
    `ProductionSchedulerConfig`, and legacy `services.orchestrator.scheduler` import/monkeypatch paths;
    Frontend/downstream consumers: scheduler tests and downstream private imports;
    Failure paths/rollback/stale state: report-only findings without baseline writes;
    Evidence/audit/readiness: scheduler compatibility inventory and entropy report metadata.
  - Regression Rows: new un-inventoried scheduler alias/wrapper/import -> compatibility-facade-growth finding;
    inventoried scheduler alias/wrapper/import with owner, retention/removal, and verification metadata -> no finding;
    audit JSON/markdown report -> preserves compatibility guard schema and does not create/update `.entropy-baseline/latest.json`;
    existing scheduler monkeypatch path -> focused scheduler tests continue to pass.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_gateway_reconcile.py`; `uv run pytest -q tests/test_entropy_audit_script.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: update `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` with guard expectations and exact commands.
- [x] 1.2 Scheduler state owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_state` state helpers, candidate-state re-exports, and legacy monkeypatch wrappers.
  - Dependencies: 1.1.
  - Out of Scope: lease, discovery, candidate construction, execution, evidence, cancellation/status proof.
  - Fixture Level: expanded; Repair Intensity: medium-high, because this narrows the scheduler state compatibility surface while
    preserving old scheduler imports and monkeypatch paths that existing tests still use.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler` and legacy `services.orchestrator.scheduler`
    state imports stay stable); Legacy compatibility / examples (old private state helper monkeypatches keep working);
    Schema / columns / units / field names (candidate-state evidence and decision fields stay equivalent);
    Concurrency / shared state / ordering (compat wrappers temporarily bind scheduler monkeypatches into `scheduler_state`);
    Resource limits / large input / discovery (candidate-state bounded jobs/events/task-results and overflow evidence stay stable,
    while discovery behavior remains out of scope);
    Documentation / migration notes (inventory groups remain the owner/removal authority).
    Not Selected: Auth / permissions / secrets, File IO / path safety / overwrite, Config / project setup,
    Release / packaging / dependency compatibility,
    Geospatial / CRS / basin geometry, Hydro-met time series / forcing windows,
    SHUD numerical runtime / conservation / NaN, PostGIS / TimescaleDB domain behavior,
    Slurm production lifecycle / mock-vs-real parity, External hydro-met providers / snapshot reproducibility,
    Run manifest / QC provenance, Published NHMS artifacts / display identity - this task does not change runtime,
    provider, DB, Slurm, artifact, evidence-write, discovery, lease, or frontend behavior.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_state` owns candidate-state decision/evidence
    behavior, while `services.orchestrator.scheduler` exposes only inventoried compatibility names and wrappers.
    Source-of-truth identity/contract: scheduler-state owner module plus inventory groups
    `scheduler-state-monkeypatch-bindings` and `candidate-state-reexports`. Surfaces: Producers:
    `services/orchestrator/scheduler_state.py`; Compatibility facade: `services/orchestrator/scheduler.py`;
    Validators/preflight: focused scheduler tests and compatibility-facade guard; Storage/cache/query: candidate-state
    repository/provider rows are read-only inputs; Public routes/entrypoints: `ProductionScheduler` and legacy scheduler
    private imports; Failure paths/rollback/stale state: candidate decision/evidence stays behaviorally equivalent.
  - Regression Rows: scheduler facade state export names match the owner module and inventory groups; monkeypatching an
    inventoried scheduler state helper through `services.orchestrator.scheduler` affects nested `scheduler_state` calls;
    direct owner-module candidate-state decisions match facade decisions; bounded candidate-state jobs/events/task-results
    and overflow evidence stay equivalent through owner and facade paths; no lease/discovery/candidate-construction/execution/
    evidence inventory groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory groups `scheduler-state-monkeypatch-bindings` and
    `candidate-state-reexports`, or state that no state facade surface changed and prove it with compatibility tests.
- [x] 1.3 Scheduler lease owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_lease` lease classes/constants, compat lookup names, heartbeat/guard-file helpers.
  - Dependencies: 1.1.
  - Out of Scope: scheduler state, discovery, candidate construction, execution, evidence, cancellation/status proof.
  - Fixture Level: expanded; Repair Intensity: medium-high, because lease acquisition/renewal/release and scheduler
    heartbeat behavior guard the single-run scheduler mutation boundary while old scheduler imports and monkeypatch paths
    must stay compatible after lease extraction.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler` lock behavior and legacy
    `services.orchestrator.scheduler` lease imports stay stable); Legacy compatibility / examples (old private
    guard-file, liveness, unlink, heartbeat, and Postgres lock-key monkeypatches keep working); File IO / path safety /
    overwrite (lock parent, guard file, symlink, stale lock, and atomic renew behavior stay safe); Concurrency / shared
    state / ordering (file lock guard, stale-lock CAS, heartbeat renewal/loss, and Postgres advisory lock semantics stay
    equivalent); Config / project setup (`scheduler_lock_backend`, `database_url`, workspace-root lock paths, and bounded
    DB connect timeout stay compatible); Resource limits / large input (oversized lock payload rejection remains bounded);
    Error handling / rollback / partial outputs (unsafe lock paths return stable contention evidence without mutation);
    Documentation / migration notes (inventory group remains the owner/removal authority).
    Not Selected: Scheduler state/candidate evidence fields, discovery/backfill selection, candidate construction,
    execution/cohort semantics, evidence serialization/write safety beyond lease-loss proof, cancellation/status proof,
    Auth / permissions / secrets, Release / packaging / dependency compatibility, Geospatial / CRS / basin geometry,
    Hydro-met time series / forcing windows, SHUD numerical runtime / conservation / NaN, PostGIS / TimescaleDB domain
    behavior beyond advisory-lock acquisition, Slurm production lifecycle / mock-vs-real parity, External hydro-met
    providers / snapshot reproducibility, Run manifest / QC provenance, Published NHMS artifacts / display identity.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_lease` owns lease classes/constants,
    heartbeat, guard-file, liveness, unlink, and Postgres advisory lock-key behavior, while
    `services.orchestrator.scheduler` exposes only inventoried lease compatibility names and old monkeypatch lookup paths.
    Source-of-truth identity/contract: scheduler-lease owner module plus inventory group `scheduler-lease-reexports`.
    Surfaces: Producers: `services/orchestrator/scheduler_lease.py`; Compatibility facade:
    `services/orchestrator/scheduler.py`; Validators/preflight: focused scheduler/gateway tests and compatibility-facade
    guard; Storage/cache/query: lock file/guard file and optional Postgres advisory lock; Public routes/entrypoints:
    `ProductionScheduler`, `ProductionSchedulerConfig`, and legacy scheduler lease imports; Failure paths/rollback/stale
    state: unsafe lock evidence, stale lock CAS, heartbeat lease-loss boundary, and Postgres lock contention/unavailable
    evidence.
  - Regression Rows: scheduler facade lease export names match the owner module, owner `__all__`, and inventory group;
    `scheduler_lease._scheduler_compat_function` resolves inventoried old scheduler monkeypatch names for liveness,
    unlink, parent/guard open, and Postgres lock-key helpers; file lock guard rejects symlink/non-regular/oversized/stale
    unowned lock paths without mutating outside the workspace; atomic renew and stale-lock CAS preserve lock identity and
    avoid empty/half-written locks; heartbeat loss fences submission and healthy heartbeat does not fence a pass; Postgres
    advisory lock backend uses the scheduler compat lock-key lookup and does not touch file guard helpers; no state/
    discovery/candidate-construction/execution/evidence/cancellation inventory groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_gateway_reconcile.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `scheduler-lease-reexports`, or state that no lease facade
    surface changed and prove it with compatibility tests.
- [x] 1.4 Scheduler discovery owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_discovery` and forwarding methods for cycle discovery/backfill/source windows.
  - Dependencies: 1.1.
  - Out of Scope: candidate construction, execution, evidence writes, cancellation/status proof.
  - Fixture Level: expanded; Repair Intensity: medium-high, because discovery selects source cycles, preserves backfill
    warm-start ordering, emits source-window evidence, and still exposes old scheduler private methods and aliases that
    tests monkeypatch directly.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler` discovery pass behavior and legacy
    `services.orchestrator.scheduler` discovery imports stay stable); Legacy compatibility / examples (old
    `_discover_cycles`, `_discover_source_window`, `_cycle_completion_status`, source-cycle evidence helpers, and
    discovery aliases keep working); Schema / columns / units / field names (source-cycle evidence, backfill audit,
    deferred evidence, cycle-status candidates, and redaction fields stay equivalent); Resource limits / large input /
    discovery (`MAX_DISCOVERED_CYCLES`, max-cycles-per-source, duplicate collapse, allowed-hour filter, and multi-day
    backfill windows stay bounded); External hydro-met providers / snapshot reproducibility (legacy one-arg/two-arg
    adapter discovery fallback and source-window filtering stay deterministic); Concurrency / shared state / ordering
    (global oldest backfill selection and per-source ordering stay stable); Documentation / migration notes (inventory
    group remains the owner/removal authority).
    Not Selected: Lease acquisition/heartbeat, candidate construction/canonical readiness, execution/cohort handling,
    evidence file writes, cancellation/status proof, Auth / permissions / secrets beyond discovery evidence redaction,
    Config / project setup, File IO / path safety / overwrite, Release / packaging / dependency compatibility,
    Geospatial / CRS / basin geometry, Hydro-met numerical forcing contents, SHUD numerical runtime / conservation /
    NaN, PostGIS / TimescaleDB domain behavior, Slurm production lifecycle / mock-vs-real parity, Run manifest / QC
    provenance, Published NHMS artifacts / display identity.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_discovery` owns cycle discovery, source-window
    querying, completion/gap classification, backfill selection, source-cycle evidence, sensitive discovery evidence
    redaction, duplicate/deferred evidence, and source horizon metadata, while `services.orchestrator.scheduler` exposes
    only inventoried discovery aliases and forwarding methods. Source-of-truth identity/contract: scheduler-discovery
    owner module plus inventory group `discovery-compat-aliases`. Surfaces: Producers:
    `services/orchestrator/scheduler_discovery.py`; Compatibility facade: `services/orchestrator/scheduler.py`;
    Validators/preflight: focused backfill/production scheduler tests and compatibility-facade guard;
    Storage/cache/query: adapter cycle discovery and read-only active repository completion/candidate-state queries;
    Public routes/entrypoints: `ProductionScheduler._discover_cycles`, `_discover_source_window`,
    `_cycle_completion_status`, `SchedulerSourceCycle`, and legacy source-cycle helper imports; Failure paths/rollback/
    stale state: adapter TypeError fallback, source unavailable/probe-failed/rate-limited evidence, duplicate exclusions,
    cycle discovery cap, and global backfill deferral.
  - Regression Rows: scheduler facade discovery alias names match owner attributes and inventory group; forwarding methods
    delegate to owner module functions while preserving instance monkeypatches for `_discover_source_window` and
    `_cycle_completion_status`; legacy adapter TypeError fallback and wrong-source/out-of-window filters remain
    equivalent; allowed-cycle-hour filter happens before duplicate collapse; backfill picks the oldest available
    incomplete cycle per source and globally defers later cycles; unavailable/probe-failed/rate-limited gaps do not
    consume source budget and keep redacted retryable evidence; `MAX_DISCOVERED_CYCLES` blocks before candidate/evidence
    amplification; no state/lease/candidate-construction/execution/evidence-write/cancellation inventory groups change in
    this slice.
  - Focused Verification: `uv run pytest -q tests/test_scheduler_backfill.py tests/test_production_scheduler.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `discovery-compat-aliases`, or state that no discovery
    facade surface changed and prove it with compatibility tests.
- [x] 1.5 Scheduler candidate-construction owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_candidates` candidate building, canonical readiness, active Slurm sync, duplicate exclusion, and candidate-state merge.
  - Dependencies: 1.1 and 1.2.
  - Out of Scope: discovery source window logic, execution/cohort handling, evidence file writes, cancellation/status proof.
  - Fixture Level: expanded; Repair Intensity: medium-high, because candidate construction selects mutable scheduler
    work items from discovery and state inputs, gates canonical readiness before execution, can trigger active Slurm
    status sync, and still exposes old scheduler private methods/constants that tests monkeypatch directly.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler._build_candidates`,
    `_candidate_construction_context`, and legacy `services.orchestrator.scheduler` candidate imports stay stable);
    Legacy compatibility / examples (old private candidate helper imports and `MAX_CANDIDATES` monkeypatches keep
    working); Schema / columns / units / field names (candidate dictionaries, blocked/skipped reasons, canonical
    readiness evidence, duplicate exclusions, active Slurm sync evidence, and state-evidence merge fields stay
    equivalent); Concurrency / shared state / ordering (candidate-state decisions, active orchestration checks, active
    Slurm jobs, status-sync retries, and duplicate exclusion ordering stay stable); Resource limits / large input /
    discovery (`MAX_CANDIDATES`, bounded active Slurm jobs, candidate-state job/event limits, and duplicate collapse
    stay bounded); Slurm production lifecycle / mock-vs-real parity (active Slurm sync/cancel/defer paths keep existing
    proof inputs without moving execution); Run manifest / QC provenance (run_id, forcing_version_id, canonical product,
    source policy identity, and source object identity remain bound to the candidate); Error handling / rollback /
    partial outputs (canonical readiness unavailable/query-failed evidence, active Slurm status-sync failed/deferred
    no-submit outcomes, and blocked/skipped candidate lists stay stable without writing evidence files in this slice);
    Documentation / migration notes (inventory group remains the owner/removal authority).
    Not Selected: Discovery source-window selection and backfill ordering, execution/cohort submission, evidence file
    writes/reservation safety, cancellation/status proof assembly, lease acquisition/heartbeat, Auth / permissions /
    secrets beyond existing evidence redaction, Config / project setup, File IO / path safety / overwrite, Release /
    packaging / dependency compatibility, Geospatial / CRS / basin geometry, Hydro-met numerical forcing contents, SHUD
    numerical runtime / conservation / NaN, PostGIS / TimescaleDB domain behavior beyond read-only candidate-state
    queries, External hydro-met providers / snapshot reproducibility beyond input horizon identity, Published NHMS
    artifacts / display identity.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_candidates` owns candidate construction,
    canonical readiness gating, active Slurm sync/defer/cancel classification, duplicate exclusion, candidate-state
    merge, source identity helpers, and status-sync failure evidence, while `services.orchestrator.scheduler` exposes
    only inventoried candidate aliases and forwarding methods. Source-of-truth identity/contract: scheduler-candidates
    owner module plus inventory group `candidate-construction-compat-aliases`. Surfaces: Producers:
    `services/orchestrator/scheduler_candidates.py`; Compatibility facade: `services/orchestrator/scheduler.py`;
    Validators/preflight: focused production scheduler/backfill tests and compatibility-facade guard;
    Storage/cache/query: read-only active repository candidate-state, active orchestration, active Slurm jobs, and
    completed pipeline queries; Public routes/entrypoints: `ProductionScheduler._build_candidates`,
    `_candidate_construction_context`, canonical readiness/source identity helper imports, and `MAX_CANDIDATES`;
    Failure paths/rollback/stale state: canonical readiness unavailable/query-failed evidence, identity mismatch blocks,
    duplicate candidate exclusions, active duplicate pipeline skips, active Slurm sync failure/defer/cancel evidence, and
    max-candidate limit errors; Evidence/audit/readiness: scheduler compatibility inventory and candidate construction
    test evidence.
  - Regression Rows: scheduler facade candidate alias names match owner attributes and inventory group; forwarding methods
    delegate to `SchedulerCandidateConstructionContext` and `build_candidates` while preserving scheduler-level
    monkeypatches for `MAX_CANDIDATES` and candidate-state decider inputs; duplicate candidate ids produce skipped
    candidates plus `duplicate_exclusions` before amplification; canonical readiness unavailable/not-ready/fresh-zero-row
    evidence keeps existing blocked or full-chain behavior; terminal candidate state and active Slurm state are recorded
    before not-ready canonical gates; active Slurm status sync success/failure/defer paths keep sync evidence and
    candidate-state merge semantics; no discovery source-window, execution/cohort, evidence-write, lease, or
    cancellation/status inventory groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `candidate-construction-compat-aliases`, or state that no
    candidate-construction facade surface changed and prove it with compatibility tests.
- [x] 1.6 Scheduler execution/cohort owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_execution` execution, restart-compatible cohorts, forced production, run-id/cohort grouping, concurrent submissions.
  - Dependencies: 1.1 and 1.5.
  - Out of Scope: evidence serialization/write safety, cancellation/status proof, lease implementation.
  - Fixture Level: expanded; Repair Intensity: high, because this slice guards production execution handoff,
    restart-compatible cohort grouping, concurrent submit ordering, Slurm preflight blockers, and old scheduler private
    execution helper paths without changing evidence-write/proof or cancellation ownership.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler._produce_forcing_for_candidates`,
    `_execute_candidates`, `_execute_candidate_cohort`, `_scheduler_execution_context`, and legacy
    `services.orchestrator.scheduler` execution helper imports stay stable); Legacy compatibility / examples (old private
    execution/cohort helper imports and monkeypatches keep working); Schema / columns / units / field names (execution
    evidence status, submitted flags, mutation outcomes, pipeline write proof fields passed through by context, cohort
    run IDs, and basin manifest fields stay equivalent); Config / project setup (`concurrent_submit_bound`,
    `slurm_execution_enabled`, and `slurm_env` flow into execution context and preflight exactly once);
    Concurrency / shared state / ordering (source/cycle/model ordering, restart-vs-full cohort ordering, overlap receipt,
    concurrent submit bound, sibling cohort failure isolation, and active mutation ordering stay stable); Resource limits
    / large input / discovery (candidate cohorts are grouped deterministically and submit fan-out remains bounded by
    config); Slurm production lifecycle / mock-vs-real parity (Slurm env/resource/secret preflight blockers, QHH chain
    handoff, submit overlap receipts, forced/in-process forcing handoff, and unknown-after-attempt semantics remain
    equivalent); Error handling / rollback / partial outputs (orchestrator exceptions keep per-candidate submission
    failure evidence without dropping sibling cohort evidence); Run manifest / QC provenance (candidate identity,
    output URI, basin manifest, orchestration_run_id, restart_stage, canonical identity, and forcing result identity stay
    bound to the submitted candidate); Documentation / migration notes (inventory group remains the owner/removal
    authority). Auth / permissions / secrets is selected narrowly for execution preflight redaction/secret-manifest
    blockers, not for API auth or scheduler lease permissions.
    Not Selected: Evidence serialization/write safety and file overwrite behavior, cancellation/status proof assembly,
    lease acquisition/heartbeat, discovery/source-window selection, candidate construction/canonical readiness
    selection, public API route auth, Release / packaging / dependency compatibility, Geospatial / CRS / basin geometry,
    Hydro-met numerical forcing contents, SHUD numerical runtime / conservation / NaN beyond execution handoff shape,
    PostGIS / TimescaleDB domain behavior, External hydro-met providers / snapshot reproducibility, Published NHMS
    artifacts / display identity beyond candidate output URI identity.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_execution` owns forcing production handoff,
    candidate execution, execution cohorting, restart-compatible grouping, concurrent submissions, Slurm execution
    preflight, and execution/cohort run-id helpers, while `services.orchestrator.scheduler` exposes only inventoried
    execution forwarding methods and wrappers. Source-of-truth identity/contract: scheduler-execution owner module plus
    inventory group `execution-restart-cohort-wrappers`. Surfaces: Producers:
    `services/orchestrator/scheduler_execution.py`; Compatibility facade: `services/orchestrator/scheduler.py`;
    Validators/preflight: production scheduler tests and compatibility-facade guard; Storage/cache/query:
    submit-overlap receipt and read-only candidate state already attached to candidates; Public routes/entrypoints:
    `ProductionScheduler._produce_forcing_for_candidates`, `_execute_candidates`, `_execute_candidate_cohort`,
    `_scheduler_execution_context`, `_restart_compatible_candidate_cohorts`, `_candidate_restart_stage`,
    `_candidate_restart_cohort_key`, `_candidate_execution_cohort_run_id`, `_candidate_execution_cohorts`, and
    `_candidate_execution_cohort_run_id_for_candidate`; Failure paths/rollback/stale state: output-uri unavailable,
    Slurm env/resource/secret preflight block, orchestrator exception, unknown-after-attempt mutation evidence, sibling
    cohort failure isolation, and stale restart_stage ignored for fresh full-chain candidates; Evidence/audit/readiness:
    scheduler compatibility inventory and focused production scheduler evidence.
  - Regression Rows: scheduler facade execution wrapper names match owner attributes and inventory group; forwarding
    methods delegate to `SchedulerExecutionContext`, `produce_forcing_for_candidates`, `execute_candidates`, and
    `execute_candidate_cohort` while preserving scheduler-level monkeypatch inputs for cohort/run-id helpers; fresh
    full-chain candidates ignore residual restart_stage and submit one full-chain cohort; mixed restart/fresh candidates
    split into candidate-scoped restart cohorts and a full-chain cohort with stable orchestration_run_id values; concurrent
    submissions stay within `concurrent_submit_bound` and preserve deterministic evidence ordering; one cohort exception
    yields submission-failed evidence without dropping sibling successful cohort evidence; Slurm env/secret/resource
    blockers prevent submission without moving evidence-write/proof ownership; no evidence-write, cancellation/status,
    lease, discovery, or candidate-construction inventory groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `execution-restart-cohort-wrappers`, or state that no
    execution/cohort facade surface changed and prove it with compatibility tests.
- [ ] 1.7 Scheduler evidence-write and proof owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_evidence` evidence schema/constants, pre-execution reservation, bounded payloads, runtime-root evidence, write safety, proof assembly wrappers.
  - Dependencies: 1.1 and 1.6.
  - Out of Scope: local cancellation orchestration glue and Slurm cancellation side effects.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py`.
  - Inventory/Evidence Update: update scheduler inventory group `scheduler-evidence-write-compat`.
- [ ] 1.8 Scheduler cancellation/status proof local-glue closure.
  - Module/Scope: cancellation/status/proof wrappers, local cancellation orchestration retained in `scheduler.py`, and explicit retained-glue classification.
  - Dependencies: 1.1 and 1.7.
  - Out of Scope: extracting cancellation orchestration unless the issue proves equivalent cancellation, status-sync, mutation-proof, and lease-lost evidence behavior.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py`.
  - Inventory/Evidence Update: update scheduler inventory group `cancellation-status-proof-wrappers`.
- [ ] 1.9 Scheduler group verification and evidence closeout.
  - Module/Scope: integration gate for scheduler group.
  - Dependencies: 1.1-1.8.
  - Out of Scope: new scheduler behavior, Slurm resource changes, DB schema changes.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_gateway_reconcile.py`; `uv run pytest -q tests/test_entropy_audit_script.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final scheduler issue/PR mapping in implementation evidence.

## 2. Chain Facade Deepening

- [ ] 2.1 Chain compatibility guard and parity fixture.
  - Module/Scope: `services/orchestrator/chain.py` facade guard plus chain compatibility inventory assertions.
  - Dependencies: None.
  - Out of Scope: moving owner-family behavior.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py tests/test_gateway_reconcile.py`.
  - Inventory/Evidence Update: update `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` with guard expectations and exact commands.
- [ ] 2.2 Chain stage catalog/type owner-family completion.
  - Module/Scope: `services.orchestrator.chain_stages`, `services.orchestrator.chain_types`, static catalog/type re-exports, and result/context type compatibility.
  - Dependencies: 2.1.
  - Out of Scope: stage execution, array accounting, manifest assembly, reservation, retry, tile publication, worker adapters, repository behavior.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-stage-catalog-type-reexports`.
- [ ] 2.3 Chain stage execution owner-family completion.
  - Module/Scope: `services.orchestrator.chain_stage_execution`, `StageExecutionDependencies`, reservation-before-submit, bind-after-submit, polling, timeout, retry bridge, and published-log semantics.
  - Dependencies: 2.1 and 2.2.
  - Out of Scope: reservation protocol internals, retry service internals, tile publisher implementation, array accounting.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_pipeline_logs_artifacts.py tests/test_e2e_m3.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-stage-execution-forwarders`.
- [ ] 2.4 Chain array-accounting owner-family completion.
  - Module/Scope: `services.orchestrator.chain_array_accounting`, sacct parsing, task evidence, resource metrics, partial status, candidate outcome sanitization.
  - Dependencies: 2.1.
  - Out of Scope: manifest assembly, retry, reservation, tile publication, worker/source identity.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_partial_success.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-array-accounting-forwarders`.
- [ ] 2.5 Chain manifest owner-family completion.
  - Module/Scope: `services.orchestrator.chain_manifests`, `services.orchestrator.production_contract`, model-run assembly, runtime manifest safe writes, manifest index, quality states, residual blockers.
  - Dependencies: 2.1 and 2.2.
  - Out of Scope: array accounting, stage execution, repository persistence, tile publishing.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_warm_start_chaining.py tests/test_analysis_pipeline.py tests/test_production_scheduler.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-manifest-forwarders`.
- [ ] 2.6 Chain reservation owner-family completion.
  - Module/Scope: `services.orchestrator.reservation`, reserve/bind/reclaim protocol, Slurm comment contract, chain reservation wrappers.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: repository extraction, retry service behavior, stage execution body.
  - Focused Verification: `uv run pytest -q tests/test_gateway_reconcile.py tests/test_orchestration_chain.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-reservation-facade`.
- [ ] 2.7 Chain retry owner-family completion.
  - Module/Scope: `services.orchestrator.retry`, retry service/config/backoff, manual retry identity, partial-array retry bridge, `_retry_service_from_env` classification.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: reservation protocol, stage execution polling, repository schema.
  - Focused Verification: `uv run pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_e2e_m3.py tests/test_orchestration_chain.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-retry-facade`.
- [ ] 2.8 Chain tile-publisher owner-family completion.
  - Module/Scope: `services.tile_publisher`, `services.tile_publisher.publisher`, chain tile-publisher imports, failure payload mapping, local publish dependency wiring.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: Slurm stage execution semantics, array accounting, repository behavior.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_pipeline_logs_artifacts.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-tile-publisher-facade`.
- [ ] 2.9 Chain worker/source-identity and time-consistency owner-family completion.
  - Module/Scope: worker/adapter imports, source identity helpers, canonical readiness, cycle id/time helpers, source scenario glue, and `services.orchestrator.time_consistency` aliasing.
  - Dependencies: 2.1 and 2.5.
  - Out of Scope: manifest schema changes, source product policy changes, station-MVT work.
  - Focused Verification: `uv run pytest -q tests/test_ifs_forecast_integration.py tests/test_source_identity.py tests/test_warm_start_chaining.py tests/test_orchestration_chain.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-worker-adapter-facade`.
- [ ] 2.10 Chain persistence/repository ownership decision and extraction/retention.
  - Module/Scope: `PipelineJob`, `PipelineStore`, `OrchestratorRepository`, `PsycopgOrchestratorRepository`, active pipeline detection, candidate state, reservations, events, forecast cycles, hydro run status.
  - Dependencies: 2.1, 2.6, and 2.7.
  - Out of Scope: DB migration, scheduler behavior changes, retry policy changes.
  - Focused Verification: `uv run pytest -q tests/test_gateway_reconcile.py tests/test_production_scheduler.py tests/test_retry_cancel_consistency.py tests/test_real_database_integration.py`.
  - Inventory/Evidence Update: update chain inventory group `chain-persistence-repository-facade` with either the new repository owner module or explicit retained local implementation.
- [ ] 2.11 Chain group verification and evidence closeout.
  - Module/Scope: integration gate for chain group.
  - Dependencies: 2.1-2.10.
  - Out of Scope: new orchestration behavior, Slurm behavior changes, DB schema changes.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py tests/test_gateway_reconcile.py tests/test_real_database_integration.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`;
    `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final chain issue/PR mapping in implementation evidence.

## 3. Two-Node E2E Lane Deepening

- [ ] 3.1 Shared two-node evidence contracts.
  - Module/Scope: shared lane result adapter, current-run binding, producer/source artifact validation, strict identity, approved-root path safety, redaction, log URI safety.
  - Dependencies: None.
  - Out of Scope: moving individual lane evaluators or final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"`.
  - Inventory/Evidence Update: update `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` shared-contract rows.
- [ ] 3.2 Metadata and strict-identity lane extraction.
  - Module/Scope: metadata aliases, source-scope resolution, reduced-scope flags, five-field identities, downstream source-lane seeding.
  - Dependencies: 3.1.
  - Out of Scope: source proof lanes, cross-plane aggregation, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"`.
  - Inventory/Evidence Update: update two-node inventory row `metadata`.
- [ ] 3.3 Docker preflight lane extraction.
  - Module/Scope: Docker preflight current-run, disk/command/resource checks, approved-root rules, Docker root resource evidence, blocker namespace.
  - Dependencies: 3.1.
  - Out of Scope: Docker security child artifacts, display readonly proof, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"`.
  - Inventory/Evidence Update: update two-node inventory row `Docker preflight`.
- [ ] 3.4 Docker security lane extraction.
  - Module/Scope: Docker security child/source artifacts, display-readonly runtime proof, forbidden capability findings, readonly published/root filesystem proof.
  - Dependencies: 3.1 and 3.3.
  - Out of Scope: readonly DB lane, API/browser/logs lanes, manual ops.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"`.
  - Inventory/Evidence Update: update two-node inventory row `Docker security`.
- [ ] 3.5 Readonly DB lane extraction.
  - Module/Scope: readonly DB source/sibling artifacts, live readonly proof, route identity, no-write probes, source coverage, recomputed status.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: Docker security, API/browser/logs source lanes, DB schema or role changes.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"`.
  - Inventory/Evidence Update: update two-node inventory row `readonly DB`.
- [ ] 3.6 Simple live lane helper and Slurm/compute/display lanes.
  - Module/Scope: shared simple-live helper plus Slurm, compute summary, and display summary lanes.
  - Dependencies: 3.1.
  - Out of Scope: Docker, readonly DB, API/browser/logs, manual ops, cross-plane.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"`.
  - Inventory/Evidence Update: update two-node inventory rows `Slurm proof`, `compute summary`, and `display summary`.
- [ ] 3.7 API proof lane extraction.
  - Module/Scope: API source lane required checks, live proof flags, producer-backed command/request/response/artifact proof, per-source scope contribution.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: browser/logs source lanes, API route implementation, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"`.
  - Inventory/Evidence Update: update two-node inventory row `API proof`.
- [ ] 3.8 Browser proof lane extraction.
  - Module/Scope: browser source lane, source-switch proof, job-like check identity, live browser evidence, per-source scope contribution.
  - Dependencies: 3.1, 3.2, and 3.7.
  - Out of Scope: API/logs lane behavior, frontend UI changes, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"`.
  - Inventory/Evidence Update: update two-node inventory row `browser proof`.
- [ ] 3.9 Logs lane extraction.
  - Module/Scope: logs source lane, strict log identity, published log URI safety, typed unavailable proof, redaction.
  - Dependencies: 3.1, 3.2, and 3.7.
  - Out of Scope: private compute log publication changes, API/browser source lanes, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"`.
  - Inventory/Evidence Update: update two-node inventory row `logs`.
- [ ] 3.10 Manual ops lane extraction.
  - Module/Scope: manual ops receipts, node-27 fail-closed proof, no-side-effect proof, node-22 control receipt provenance, optional receipt artifact validation.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: production control behavior changes, API route changes, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"`.
  - Inventory/Evidence Update: update two-node inventory row `manual ops receipts`.
- [ ] 3.11 Cross-plane and source-scope aggregation extraction.
  - Module/Scope: cross-plane lane, source-scope result construction, GFS+IFS full PASS, reduced-scope PARTIAL, strict identity aggregation.
  - Dependencies: 3.2, 3.7, 3.8, and 3.9.
  - Out of Scope: final summary writing, output safety, lane-specific source proof logic.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"`.
  - Inventory/Evidence Update: update two-node inventory row `source-scope / cross-plane aggregation`.
- [ ] 3.12 Two-node final aggregation extraction.
  - Module/Scope: final status ordering, final summary schema, blocker/finding collection, output path safety, redaction, force/existing-output behavior.
  - Dependencies: 3.1-3.11.
  - Out of Scope: moving any lane not already interface-stable, changing final status semantics.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py`.
  - Inventory/Evidence Update: update two-node inventory row `final aggregation`.
- [ ] 3.13 Two-node group verification and evidence closeout.
  - Module/Scope: integration gate for two-node E2E group.
  - Dependencies: 3.1-3.12.
  - Out of Scope: production topology changes, station-MVT closure, live service deployment.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py`; `uv run ruff check services/production_closure tests/test_two_node_e2e_evidence.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final two-node issue/PR mapping in implementation evidence.

## 4. Readiness Validation Lane Deepening

- [ ] 4.1 Readiness item contract extraction.
  - Module/Scope: shared readiness item schema, status/execution-mode truth table, required fields, release-blocker context rules, invalid item namespaces.
  - Dependencies: None.
  - Out of Scope: proof loading, dependency summaries, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "status_execution_mode_truth_table or readiness_schema_validation_item"`.
  - Inventory/Evidence Update: update `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md` row `Readiness item validation`.
- [ ] 4.2 Shared artifact writers extraction.
  - Module/Scope: preflight artifact, environment artifact, evidence writer, safe writes, path rendering, redaction, bounded payloads.
  - Dependencies: 4.1.
  - Out of Scope: live proof receipt parsing, proof-specific validators, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "preflight or side_effect"`; `uv run pytest -q tests/test_production_readiness_validation.py -k "environment_artifact_uses_allowlist"`.
  - Inventory/Evidence Update: update readiness inventory rows `Preflight artifact surface` and `Environment artifact surface`.
- [ ] 4.3 Shared live-proof loader and receipts artifact extraction.
  - Module/Scope: inline/file ambiguity, proof file size/JSON limits, raw-payload omission, live proof receipts artifact, redaction flags.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: auth/alert/rollback/target-env semantics, dependency proof binding, scheduler proof binding.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "live_proof_receipts_artifact or live_proof_json_traversal"`.
  - Inventory/Evidence Update: update readiness inventory row `Live proof receipts artifact surface`.
- [ ] 4.4 Dependency summary reader extraction.
  - Module/Scope: Slurm, object-store, source, E2E, and MVT deterministic dependency summaries, aliases, issue/schema/status checks, artifact refs, sha256 details, review-only final semantics.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: dependency live proof receipts, final live readiness, two-node E2E lane extraction.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or object_store or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or source or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or e2e or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or mvt or existing_m19"`.
  - Inventory/Evidence Update: update readiness inventory dependency-summary rows.
- [ ] 4.5 Scheduler evidence reader extraction.
  - Module/Scope: scheduler evidence root/file mutual exclusion, file limits, schema/pass-id checks, review modes, count/cardinality logic, no-mutation proof, redaction.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: optional live scheduler proof binding, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "scheduler and evidence"`.
  - Inventory/Evidence Update: update readiness inventory row `Scheduler evidence`.
- [ ] 4.6 Proof-specific live validators extraction.
  - Module/Scope: auth, alert, rollback, and target-environment proof validators under the shared live-proof loader.
  - Dependencies: 4.1 and 4.3.
  - Out of Scope: dependency proof binding, scheduler proof binding, executing live side effects.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "auth or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "alert or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "rollback or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "target_env or final_readiness"`.
  - Inventory/Evidence Update: update readiness inventory rows `Live backend auth proof`, `Live alert sink proof`, `Live rollback proof`, and `Target-environment config proof`.
- [ ] 4.7 Dependency live-proof binder extraction.
  - Module/Scope: Slurm, object-store, source, E2E, and MVT dependency proof binders, alias precedence, summary-binding comparisons before redaction, live producer provenance.
  - Dependencies: 4.3 and 4.4.
  - Out of Scope: deterministic summary acceptance, scheduler live proof binding, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_receipt or slurm_proof"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_receipt or object_store"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_receipt or source"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_receipt or e2e"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_receipt or mvt"`.
  - Inventory/Evidence Update: update readiness dependency live-proof rows.
- [ ] 4.8 Scheduler live-proof binder extraction.
  - Module/Scope: optional live scheduler proof, exact producer binding, ambiguity detection, live-eligible producer mode/status, final count behavior.
  - Dependencies: 4.3 and 4.5.
  - Out of Scope: treating deterministic scheduler evidence as final live proof.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "scheduler and live"`.
  - Inventory/Evidence Update: update readiness inventory row `Optional live scheduler proof`.
- [ ] 4.9 Scoped exclusion extraction.
  - Module/Scope: CLDAS restricted source and incomplete real national data exclusions, non-failure semantics, summary inclusion, removal criteria.
  - Dependencies: 4.1.
  - Out of Scope: adding/removing product exclusions unless the inventory and product authority are updated.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "exclusions"`.
  - Inventory/Evidence Update: update readiness inventory row `Scoped exclusions`.
- [ ] 4.10 Readiness final aggregation extraction.
  - Module/Scope: `_final_ready`, release blockers, summary schema, item counts, artifact refs, safe output, deterministic-vs-live separation.
  - Dependencies: 4.1-4.9.
  - Out of Scope: moving final aggregation before lane item/result interfaces are stable.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "final or release_blocker or existing_lane"`; `uv run pytest -q tests/test_production_readiness_validation.py`.
  - Inventory/Evidence Update: update readiness inventory row `Final aggregation and release blockers`.
- [ ] 4.11 Readiness group verification and evidence closeout.
  - Module/Scope: integration gate for readiness validation group.
  - Dependencies: 4.1-4.10.
  - Out of Scope: live service mutation, production deploy changes, relaxing live proof requirements.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py`; `uv run ruff check services/production_closure tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final readiness issue/PR mapping in implementation evidence.

## 5. API Bootstrap Deepening

- [ ] 5.1 API OpenAPI patch owner-module extraction.
  - Module/Scope: OpenAPI patching for runtime, pipeline, station-series, QHH latest-product, MVT, flood, and layer metadata schema output.
  - Dependencies: None.
  - Out of Scope: route registration, auth policy, request-body validation, frontend UI behavior.
  - Focused Verification: `uv run pytest -q tests/test_api.py tests/test_openapi_drift.py`; if OpenAPI output or generated API types change, also run `cd apps/frontend && pnpm check:api-types`.
  - Inventory/Evidence Update: update `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` API row with owner module and verification command.
- [ ] 5.2 API role-aware route registry extraction.
  - Module/Scope: route inclusion by runtime role, display-readonly Slurm route exclusion, compute/dev route compatibility, slurm-gateway reserved-role behavior.
  - Dependencies: None.
  - Out of Scope: OpenAPI patching internals, auth guard behavior, route handler implementation.
  - Focused Verification: `uv run pytest -q tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_api.py`.
  - Inventory/Evidence Update: update structural inventory API row with route registry owner module and retained `create_app` facade surface.
- [ ] 5.3 API static/health/cache/startup wiring extraction.
  - Module/Scope: static frontend serving, health routes, cache-control, display cache warmup, startup state wiring, `runtime_config(request)` behavior.
  - Dependencies: None.
  - Out of Scope: route registry role decisions, OpenAPI patching, protected mutation auth guard.
  - Focused Verification: `uv run pytest -q tests/test_runtime_mode.py tests/test_api.py tests/test_monitoring_api.py`.
  - Inventory/Evidence Update: update structural inventory API row with static/health/cache owner module and retained app-factory surface.
- [ ] 5.4 API protected mutation seam retention and tests.
  - Module/Scope: protected mutation auth guard and request-body validation stay on a stable seam; add or strengthen tests for request id, error shape, auth policy, and fail-closed display behavior.
  - Dependencies: 5.2.
  - Out of Scope: extracting the auth guard into an owner module, changing authorization semantics, changing route registration.
  - Focused Verification: `uv run pytest -q tests/test_runtime_mode.py tests/test_api.py tests/test_role_boundary_static.py tests/test_retry_cancel_consistency.py`.
  - Inventory/Evidence Update: update structural inventory API row with explicit retained seam classification and future extraction conditions.
- [ ] 5.5 API group verification and evidence closeout.
  - Module/Scope: integration gate for API bootstrap group.
  - Dependencies: 5.1-5.4.
  - Out of Scope: public route removal, DB migration, display-readonly capability expansion.
  - Focused Verification: `uv run pytest -q tests/test_runtime_mode.py tests/test_api.py tests/test_role_boundary_static.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py tests/test_openapi_drift.py`;
    when schema output changes, `cd apps/frontend && pnpm check:api-types`;
    `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final API issue/PR mapping in implementation evidence.

## 6. Frontend Map Surface Deepening

- [ ] 6.1 M11 pure map builders extraction.
  - Module/Scope: registered overlays, vector source keys, basin feature collections, basin-river feature collections, selected-segment collections, filters, labels, unavailable reasons, geometry budgets.
  - Dependencies: None.
  - Out of Scope: MapLibre rendering primitives, interaction dispatch, popup/selection state, station-MVT backend work.
  - Focused Verification: `cd apps/frontend && pnpm test -- src/pages/__tests__/M11Shell.test.tsx -t "registers|source|geometry|unavailable|selected segment"`; `cd apps/frontend && pnpm test -- src/components/map/__tests__/M11FloatingControls.test.tsx`.
  - Inventory/Evidence Update: update structural inventory frontend row or scoped map ownership notes with builder owner module and command.
- [ ] 6.2 M11 MapLibre primitive extraction.
  - Module/Scope: national river, basin boundaries, basin labels, basin river, registered overlays, selected segment, station cluster layers, source IDs, layer IDs, paint/layout, promote IDs, selected/hovered filters.
  - Dependencies: 6.1.
  - Out of Scope: click/hover dispatch, popup state, camera behavior.
  - Focused Verification: `cd apps/frontend && pnpm test -- src/pages/__tests__/M11Shell.test.tsx -t "registers|layers|cluster|highlight|hover"`.
  - Inventory/Evidence Update: update structural inventory frontend row or scoped map ownership notes with primitive owner module and command.
- [ ] 6.3 M11 interaction dispatch extraction.
  - Module/Scope: hover, click, rendered-feature fallback, station cluster expansion, cursor state, event payloads, priority order station cluster/point -> basin river -> MVT hit -> basin fill.
  - Dependencies: 6.1 and 6.2.
  - Out of Scope: popup rendering internals, curve-window placement, station-MVT backend completion.
  - Focused Verification: `cd apps/frontend && pnpm test -- src/pages/__tests__/M11Shell.test.tsx -t "prioritizes|click|hover|cluster|rendered cluster|river-segment"`.
  - Inventory/Evidence Update: update structural inventory frontend row or scoped map ownership notes with interaction owner module and command.
- [ ] 6.4 M11 camera and map-error helper extraction.
  - Module/Scope: initial fit, fit/fly de-dupe, source-error reset, Tianditu glyph warning downgrade, loading/unavailable state rendering.
  - Dependencies: 6.1 and 6.2.
  - Out of Scope: interaction dispatch, popup/selection state.
  - Focused Verification: `cd apps/frontend && pnpm test -- src/pages/__tests__/M11Shell.test.tsx -t "camera|fit|source error|glyph|unavailable|loading"`.
  - Inventory/Evidence Update: update structural inventory frontend row or scoped map ownership notes with camera/error owner module and command.
- [ ] 6.5 M11 popup and selection boundary stabilization.
  - Module/Scope: popup slot, curve-window placement, selected station data attributes, selected segment data attributes, popup identity updates after drag, station-MVT separation.
  - Dependencies: 6.3.
  - Out of Scope: backend station-MVT endpoint completion, hydrology/station series API behavior changes.
  - Focused Verification: `cd apps/frontend && pnpm test -- src/components/map/__tests__/M11RiverForecastPanel.test.tsx`;
    `cd apps/frontend && pnpm test -- src/components/map/__tests__/M11StationForcingPopup.test.tsx`;
    `cd apps/frontend && pnpm test -- src/pages/__tests__/M11Shell.test.tsx -t "selected|popup|station overlay|selected station"`.
  - Inventory/Evidence Update: update structural inventory frontend row or scoped map ownership notes with popup/selection retained surface and command.
- [ ] 6.6 Frontend group verification and evidence closeout.
  - Module/Scope: integration gate for frontend map group.
  - Dependencies: 6.1-6.5.
  - Out of Scope: station-MVT backend closure, new map product behavior, visual redesign.
  - Focused Verification: `cd apps/frontend && pnpm test`; `cd apps/frontend && pnpm build`; when generated API types are affected, `cd apps/frontend && pnpm check:api-types`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final frontend issue/PR mapping in implementation evidence.

## 7. Completion And Evidence

- [ ] 7.1 Update structural, compatibility, and lane inventories after each owner family completes.
  - Module/Scope: documentation/evidence mapping only.
  - Dependencies: completion of each owner-family issue.
  - Out of Scope: delayed inventory batch updates that leave code and inventory out of sync.
  - Focused Verification: `git diff --check`; reviewer confirms every moved owner module has an inventory row.
  - Inventory/Evidence Update: inventory updates are same-PR requirements, not final-only cleanup.
- [ ] 7.2 Re-run report-only entropy audit after all six groups complete.
  - Module/Scope: entropy/audit report and deltas.
  - Dependencies: 1.9, 2.11, 3.13, 4.11, 5.5, and 6.6.
  - Out of Scope: enabling entropy hard gates or writing `.entropy-baseline/latest.json`.
  - Focused Verification: `uv run pytest -q tests/test_entropy_audit_script.py`; report-only entropy command used by the implementation PR.
  - Inventory/Evidence Update: record line-count, mandatory-governance, compatibility-facade, and scoped-context deltas.
- [ ] 7.3 Run full final verification appropriate to touched surfaces.
  - Module/Scope: final local verification gate.
  - Dependencies: 7.1 and 7.2.
  - Out of Scope: replacing node-27 live receipt requirements for future runtime behavior changes.
  - Focused Verification: `uv run ruff check .`; selected backend pytest suites from completed groups; `cd apps/frontend && pnpm test`; `cd apps/frontend && pnpm build`; `openspec validate --all --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: final implementation evidence maps each task to PR/issue and command output.
- [ ] 7.4 Produce final implementation evidence map.
  - Module/Scope: final evidence document/comment for Governance-8.
  - Dependencies: 7.3.
  - Out of Scope: claiming station-MVT closure, production topology change, Slurm behavior change, or display-readonly expansion.
  - Focused Verification: reviewer checks every issue from this change has a PR, focused verification, inventory update, and explicit remaining non-goal if any.
  - Inventory/Evidence Update: record the final Governance-8 task-to-issue-to-PR mapping.
