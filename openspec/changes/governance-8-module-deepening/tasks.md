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
- [x] 1.7 Scheduler evidence-write and proof owner-family completion.
  - Module/Scope: `services.orchestrator.scheduler_evidence` evidence schema/constants, pre-execution reservation, bounded payloads, runtime-root evidence, write safety, proof assembly wrappers.
  - Dependencies: 1.1 and 1.6.
  - Out of Scope: local cancellation orchestration glue and Slurm cancellation side effects.
  - Fixture Level: expanded; Repair Intensity: high, because this slice guards the pre-mutation evidence write fence,
    bounded evidence serialization, approved runtime-root evidence, write-error blocker payloads, and legacy scheduler
    proof helper paths without changing cancellation orchestration or Slurm side effects.
  - Selected Risk Packs: Public API / CLI / script entry (`ProductionScheduler._write_prelock_blocked_evidence`,
    `_reserve_pre_execution_evidence`, `_scheduler_evidence_write_context`, `_base_evidence`, `_write_evidence`, and
    legacy `services.orchestrator.scheduler` evidence helper imports stay stable); Legacy compatibility / examples
    (old private scheduler evidence helper imports and monkeypatches keep working); Schema / columns / units / field
    names (scheduler evidence schema/version constants, review contract, model-run evidence schema, `artifact_path`,
    `evidence_pre_execution`, `resolved_runtime_roots`, `runtime_config`, execution write proof fields, and
    `no_mutation_proof` stay equivalent); Config / project setup (`evidence_dir`, `workspace_root`,
    `object_store_root`, `published_artifact_root`, `runtime_root`, `temp_root`, `require_runtime_roots`, service role,
    and `MAX_EVIDENCE_BYTES` flow through the write context and root evidence exactly once); Auth / permissions /
    secrets selected narrowly for approved-root and file-mode safety, not API auth; Filesystem / path safety (artifact
    names reject traversal/absolute paths, final evidence no-clobber guard remains, evidence directory opens through
    safe final-component and under-workspace checks); Concurrency / shared state / ordering selected narrowly for
    prelock/pre-execution evidence ordering, no-clobber-before-mutation behavior, reservation status gating execution
    proof, and unknown-after-attempt proof preservation, not for lease heartbeat, scheduler cohort concurrency, Slurm
    cancellation ordering, or #719 cancellation/status retained-glue behavior; Resource limits / large input / discovery
    (oversized evidence payloads are bounded without dropping required proof fields); Error handling / rollback / partial outputs
    (`SchedulerEvidenceWriteError`, prelock write failure, pre-execution reservation blocks, and write-error payloads
    remain stable before production mutation); Run manifest / QC provenance (runtime-root evidence, pre-execution proof,
    execution write proof, and no-mutation proof stay bound to the current pass); Documentation / migration notes
    (inventory group remains the owner/removal authority).
    Not Selected: local cancellation orchestration glue, Slurm cancellation side effects, lease acquisition/heartbeat,
    discovery/source-window selection, candidate construction/canonical readiness selection, execution/cohort grouping,
    Slurm numerical runtime or submit behavior beyond proof field interpretation, public API route auth,
    Release / packaging / dependency compatibility, Geospatial / CRS / basin geometry, Hydro-met numerical forcing
    contents, PostGIS / TimescaleDB domain behavior, External hydro-met providers / snapshot reproducibility, frontend
    display identity, and deletion of legacy scheduler evidence helper paths.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_evidence` owns evidence constants,
    `SchedulerEvidenceWriteContext`, base evidence, prelock/pre-execution evidence writes, bounded payload fitting,
    root/runtime evidence, evidence write blocker payloads, evidence directory/file guards, evidence status helpers, and
    execution/no-mutation proof assembly helpers, while `services.orchestrator.scheduler` exposes only inventoried
    evidence forwarding methods, wrappers, and constants. Source-of-truth identity/contract: scheduler-evidence owner
    module plus inventory group `scheduler-evidence-write-compat`. Surfaces: Producers:
    `services/orchestrator/scheduler_evidence.py`; Compatibility facade: `services/orchestrator/scheduler.py`;
    Validators/preflight: production scheduler tests and compatibility-facade guard; Storage/cache/query: evidence
    artifact path, pre-execution reservation artifact, final evidence no-clobber check, and runtime-root evidence;
    Public routes/entrypoints: `ProductionScheduler._write_prelock_blocked_evidence`,
    `_reserve_pre_execution_evidence`, `_scheduler_evidence_write_context`, `_base_evidence`, `_write_evidence`,
    `_candidate_evidence_write_blocked_evidence`, `_cancel_candidate_evidence_write_blocked_evidence`,
    `_sync_candidate_evidence_write_blocked_evidence`, `_evidence_reservation_blocked_payload`,
    `_evidence_write_error_payload`, `_scheduler_resolved_runtime_roots`, `_root_evidence_item`,
    `_scheduler_runtime_config_evidence`, `_open_evidence_directory`, `_write_new_regular_file`,
    `_require_evidence_artifact_available`, `_bounded_evidence_payload`, `_evidence_status`, direct scheduler evidence
    constants/classes, and execution/no-mutation proof wrappers that do not own local cancellation orchestration;
    Failure paths/rollback/stale state: unsafe artifact names, unsafe/root path failure, final artifact already exists,
    oversized payload, evidence write failure, reservation blocked, and prelock write skipped when evidence root is not
    writable; Evidence/audit/readiness: scheduler compatibility inventory and focused production scheduler evidence.
  - Regression Rows: scheduler facade evidence constants/classes match owner attributes and inventory group; forwarding
    methods delegate to `SchedulerEvidenceWriteContext`, `base_evidence`, `write_prelock_blocked_evidence`,
    `reserve_pre_execution_evidence`, and `write_evidence` while preserving scheduler-level monkeypatch inputs for
    bounded payload, open/write/availability helpers, runtime-root evidence, and write-error payloads; oversized
    evidence retains required proof fields and records the bounded reason; unsafe artifact names and final-artifact
    collisions block writes before mutation; pre-execution reservation status controls execution/slurm proof protection;
    execution write proof and no-mutation proof wrappers preserve unknown-after-attempt and absent-write semantics; apart
    from the explicit reclassification of `_execution_write_proof`, `_execution_write_proof_from_evidence`, and
    `_no_mutation_proof`, no cancellation local glue, lease, discovery, candidate-construction, execution/cohort, or
    cancellation/status inventory groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `scheduler-evidence-write-compat`, or state that no
    evidence-write facade surface changed and prove it with compatibility tests. This slice may reclassify exactly
    `_execution_write_proof`, `_execution_write_proof_from_evidence`, and `_no_mutation_proof` from
    `cancellation-status-proof-wrappers` into `scheduler-evidence-write-compat`; preserve all other
    cancellation/status proof wrappers and local cancellation retained-glue classification for #719. Do not change local
    cancellation orchestration glue, Slurm cancellation side effects, or #719 retained-glue ownership.
- [x] 1.8 Scheduler cancellation/status proof local-glue closure.
  - Module/Scope: cancellation/status/proof wrappers, local cancellation orchestration retained in `scheduler.py`, and explicit retained-glue classification.
  - Dependencies: 1.1 and 1.7.
  - Out of Scope: extracting cancellation orchestration unless the issue proves equivalent cancellation, status-sync, mutation-proof, and lease-lost evidence behavior.
  - Fixture Level: expanded; Repair Intensity: high, because this slice closes the remaining scheduler cancellation /
    status proof compatibility surface while deliberately documenting local cancellation orchestration as retained glue.
    It must prove owner-module delegation for proof/status helpers without converting live Slurm cancellation control
    flow into a shallow forwarding layer.
  - Selected Risk Packs: Public API / CLI / script entry (legacy `services.orchestrator.scheduler` private proof helper
    imports and `ProductionScheduler.run_once` evidence fields stay stable); Legacy compatibility / examples (old
    monkeypatch/import paths for cancellation/status/proof helpers remain inventoried); Schema / columns / units / field
    names (`status`, `execution_boundary`, `counts`, `slurm_status_sync_proof`, `slurm_cancellation_proof`,
    `no_mutation_proof`, `pipeline_status_writes`, `pipeline_event_writes`, `unknown_after_attempt`, and cancellation
    evidence item fields remain equivalent); Concurrency / shared state / ordering selected narrowly for preserving
    pre-execution evidence protection before status sync or cancellation mutation and for preserving unknown-after-attempt
    conservative proof aggregation, not for lease heartbeat or execution cohort concurrency; Error handling / rollback /
    partial outputs (`SLURM_CANCEL_UNSUPPORTED`, `SLURM_CANCEL_FAILED`, `SLURM_CANCELLATION_GAP`,
    `JOB_ALREADY_TERMINAL`, status-sync failures, and lease-lost evidence keep current status/proof semantics);
    Run manifest / QC provenance (mutation proof aggregation and no-mutation proof remain tied to current pass evidence);
    Documentation / migration notes (inventory records owner wrappers and retained local glue separately).
    Not Selected: extracting `ProductionScheduler._cancel_requested_active_slurm`, `_cancel_orchestrator_for`,
    `_scheduler_cancellation_status`, `_cancelled_job_pipeline_status_write`, `_cancelled_job_pipeline_event_write`, or
    `_execution_mutation_value`; changing Slurm cancellation side effects, replacing active Slurm job queries, changing
    retry/orchestrator construction, changing candidate discovery/construction/execution/cohorting, changing evidence
    file write safety, changing lease acquisition/heartbeat, changing DB schema or API routes, changing SHUD numerical
    runtime behavior, changing frontend/display surfaces, or deleting legacy scheduler helper paths.
  - Invariant Matrix: Governing invariant: `services.orchestrator.scheduler_evidence` owns cancellation/status proof
    assembly and proof-value helpers, `services.orchestrator.scheduler_candidates` owns `_slurm_status_sync_failed_evidence`,
    and `services.orchestrator.scheduler` retains local cancellation orchestration glue until a separate extraction proves
    full equivalence. Source-of-truth identity/contract: scheduler-evidence owner module, scheduler-candidates status-sync
    failure helper, local retained-glue list, and inventory group `cancellation-status-proof-wrappers`. Surfaces:
    Producers: `services/orchestrator/scheduler_evidence.py` and `services/orchestrator/scheduler_candidates.py`;
    Compatibility facade: `services/orchestrator/scheduler.py`; Validators/preflight: production scheduler tests,
    compatibility-facade guard, and entropy inventory guard; Storage/cache/query: final scheduler evidence and
    pre-execution proof; Public routes/entrypoints: `_scheduler_pass_status_from_cancellation`,
    `_scheduler_execution_boundary_from_cancellation`, `_slurm_status_sync_proof`,
    `_slurm_status_sync_proof_from_candidates`, `_slurm_cancellation_proof`, `_slurm_cancellation_proof_from_evidence`,
    `_slurm_status_sync_count`, `_slurm_status_sync_unknown_count`, `_slurm_status_sync_mutated`,
    `_slurm_status_sync_failed`, `_slurm_cancelled_count`, `_slurm_cancellation_blocked_count`,
    `_slurm_cancellation_unknown_count`, `_scheduler_mutation_proof`, `_proof_mutation_value`, `_named_proof_value`,
    `_slurm_submit_proof_value`, `_pipeline_status_write_proof_value`, `_pipeline_event_write_proof_value`,
    `_merge_proof_values`, `_positive_count`, `_empty_counts`, and `_slurm_status_sync_failed_evidence`; retained local
    glue: `ProductionScheduler._cancel_requested_active_slurm`, `_cancel_orchestrator_for`,
    `_scheduler_cancellation_status`, `_cancelled_job_pipeline_status_write`,
    `_cancelled_job_pipeline_event_write`, and `_execution_mutation_value`. Failure paths/rollback/stale state:
    unsupported cancellation, cancellation exception after attempt, partial cancellation, already-terminal job gap,
    status-sync failure after attempt, preflight-blocked reservation, and lease-lost no-mutation evidence.
    Evidence/audit/readiness: scheduler compatibility inventory and focused production scheduler evidence.
  - Regression Rows: scheduler facade cancellation/status proof wrapper names match owner attributes and inventory group;
    `_slurm_status_sync_failed_evidence` remains mapped to `scheduler_candidates`; wrappers delegate to
    `scheduler_evidence` or `scheduler_candidates` after owner monkeypatches; local retained glue remains present in
    `scheduler.py`, is not in the pure wrapper owner map, and is explicitly inventoried with retention reason/removal
    condition; cancellation proof preserves cancelled/partial/blocked/unknown counts, pipeline status/event write
    aggregation, `JOB_ALREADY_TERMINAL` event-write semantics, and `unknown_after_attempt`; status-sync proof preserves
    terminal update counts, failed-sync conservative mutation outcome, and pre-execution protection; no-mutation proof
    preserves submit/status-sync/cancellation/pipeline write fields; no scheduler state, lease, discovery, candidate,
    execution/cohort, evidence-write/file-safety, chain, API, frontend, or topology groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`.
  - Inventory/Evidence Update: update scheduler inventory group `cancellation-status-proof-wrappers` with explicit
    wrapper-owner names and retained local-glue classification. Do not move local cancellation orchestration glue unless
    equivalent cancellation, status-sync, mutation-proof, and lease-lost behavior is proved in this issue; if not moved,
    inventory must say why it remains local and when it can be removed or extracted.
- [x] 1.9 Scheduler group verification and evidence closeout.
  - Module/Scope: integration gate for scheduler group.
  - Dependencies: 1.1-1.8.
  - Out of Scope: new scheduler behavior, Slurm resource changes, DB schema changes.
  - Fixture Level: expanded integration closeout. This slice is evidence-only
    but high impact because it records the scheduler group's task-to-issue-to-PR
    chain and verifies that the compatibility facade did not grow after the
    owner-family slices.
  - Risk Pack Selection: Selected: Public API / stable facade (the
    `ProductionScheduler` facade and legacy `services.orchestrator.scheduler`
    import/monkeypatch paths remain stable); Legacy compatibility (all eight
    governed scheduler groups must stay inventoried); Test / evidence coverage
    (focused scheduler, backfill, gateway reconcile, and entropy guard suites
    form the group gate); Documentation / migration notes (implementation
    evidence records the scheduler issue/PR mapping); CI / release governance
    (closeout evidence must line up with merged PR state). Not Selected: moving
    owner behavior, changing Slurm resources, changing DB schema, changing API
    routes, changing frontend/display surfaces, changing runtime evidence
    payload semantics, or deleting compatibility symbols.
  - Invariant Matrix: Governing invariant: scheduler group 1.1-1.8 is complete
    before this closeout is checked. Source-of-truth identity/contract:
    `docs/review-loop-log.jsonl`, GitHub issue/PR state, and
    `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` agree on the
    scheduler task-to-issue-to-PR mapping. Surfaces: Producers: completed PRs
    #771-#778 and this closeout PR; Compatibility facade:
    `services/orchestrator/scheduler.py` plus owner modules; Validators:
    production scheduler tests, scheduler backfill tests, gateway reconcile
    tests, entropy inventory guard, OpenSpec validation, and diff-check;
    Evidence/audit/readiness: scheduler compatibility inventory closeout map
    and the post-merge review-loop log.
  - Regression Rows: every governed scheduler group remains present in the
    inventory (`scheduler-state-monkeypatch-bindings`,
    `candidate-state-reexports`, `scheduler-lease-reexports`,
    `discovery-compat-aliases`, `candidate-construction-compat-aliases`,
    `execution-restart-cohort-wrappers`, `scheduler-evidence-write-compat`,
    and `cancellation-status-proof-wrappers`); the final scheduler evidence map
    records tasks 1.1-1.9 with issue/PR references; focused verification
    commands pass after the evidence update; no scheduler runtime code,
    Slurm behavior, DB schema, API route, chain, readiness, two-node, or
    frontend groups change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_gateway_reconcile.py`; `uv run pytest -q tests/test_entropy_audit_script.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final scheduler issue/PR mapping in implementation evidence.

## 2. Chain Facade Deepening

- [x] 2.1 Chain compatibility guard and parity fixture.
  - Module/Scope: `services/orchestrator/chain.py` facade guard plus chain compatibility inventory assertions.
  - Dependencies: None.
  - Out of Scope: moving owner-family behavior.
  - Fixture Level: expanded guard fixture. This slice makes the chain
    compatibility inventory executable as a current Governance-8 guard without
    moving any chain owner-family behavior.
  - Risk Pack Selection: Selected: Public API / stable facade
    (`ForecastOrchestrator`, `AnalysisOrchestrator`, `OrchestratorConfig`,
    result/context types, gateway clients, and legacy
    `services.orchestrator.chain` imports remain stable); Legacy compatibility
    (chain import, re-export, wrapper, and monkeypatch paths must be inventoried
    before growth); Dependency / ownership direction (new import families
    through `chain.py` require explicit no-ownership-inversion justification);
    Test / evidence coverage (entropy guard plus chain, retry/cancel, and
    gateway reconcile suites); Documentation / migration notes (inventory
    records owner, retention reason, removal condition, caller migration path,
    and verification command); Release/package compatibility (legacy import
    surfaces remain available and no package entrypoint changes). Not Selected:
    Config / project setup (no config files, CLI defaults, or environment
    defaults change); File IO / path safety (no artifact, manifest, runtime-root,
    or safe-write logic changes); Auth / secrets / redaction (no route,
    credential, or payload-redaction behavior changes); Resource limits (no
    Slurm resources, array sizing, timeout, or concurrency behavior changes);
    Error handling / rollback / partial outputs (no runtime failure semantics
    change); moving stage execution, array accounting, manifests, reservations,
    retry, tile publication, worker/source identity, persistence/repository
    behavior; changing Slurm behavior, changing DB schema, changing scheduler
    behavior, changing API/frontend/display surfaces, or removing legacy chain
    symbols.
  - Invariant Matrix: Governing invariant: any new chain facade re-export,
    wrapper, monkeypatch alias, import family, or local implementation growth is
    either blocked by the compatibility-facade guard or covered by the Chain
    Compatibility Inventory Guard Hook Seed with required metadata.
    Source-of-truth identity/contract: `services/orchestrator/chain.py`,
    extracted owner modules, `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`,
    and the chain-facade-deepening spec agree on the nine governed chain groups.
    Surfaces: Producers: `chain.py` and owner modules; Compatibility facade:
    legacy `services.orchestrator.chain` imports and monkeypatch paths,
    including `OrchestratorConfig`;
    Validators/preflight: entropy audit compatibility guard, chain inventory
    metadata regression, orchestration chain tests, retry/cancel consistency
    tests, gateway reconcile tests, OpenSpec validation, and diff-check;
    Storage/cache/query: no DB or repository schema changes; Public
    routes/entrypoints: no API route changes.
  - Regression Rows: current repository compatibility-facade guard reports zero
    signals; every chain governed group has exactly one Guard Hook Seed metadata
    row with owner, retention, removal condition, and exact verification command;
    synthetic chain facade growth still produces report-only findings until the
    inventory metadata is updated; chain import-family growth still requires a
    no-ownership-inversion justification; non-forwarding local chain growth still
    requires owner-hosting rationale, concrete follow-up issue, and removal
    condition; no owner-family behavior moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_entropy_audit_script.py`; `uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py tests/test_gateway_reconcile.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: update `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` with guard expectations and exact commands.
- [x] 2.2 Chain stage catalog/type owner-family completion.
  - Module/Scope: `services.orchestrator.chain_stages`, `services.orchestrator.chain_types`, static catalog/type re-exports, and result/context type compatibility.
  - Dependencies: 2.1.
  - Out of Scope: stage execution, array accounting, manifest assembly, reservation, retry, tile publication, worker adapters, repository behavior.
  - Fixture Level: expanded owner-family fixture. This slice completes the
    stage catalog/type re-export compatibility contract by making owner/facade
    name maps explicit and testable; it does not move runtime orchestration
    behavior.
  - Risk Pack Selection: Selected: Public API / stable facade (stage catalogs,
    shared dataclasses, result/context types, and legacy
    `services.orchestrator.chain` imports stay identity-compatible); Legacy
    compatibility (package and chain-level imports retain object identity);
    Schema / field names (dataclass fields, defaults, type hints, and frozen
    contracts remain unchanged); Dependency / ownership direction
    (`chain_stages` and `chain_types` stay lightweight owner modules that import
    without loading `chain.py` runtime dependencies); Test / evidence coverage
    (owner/facade maps, `__all__` drift, inventory tokens, and static snapshots
    are guarded). Not Selected: stage execution behavior, array accounting,
    manifest assembly, reservation, retry, tile publication, worker/source
    identity, repository behavior, Slurm behavior, DB schema, API/frontend
    surfaces, or production topology changes.
  - Invariant Matrix: Governing invariant: every static stage catalog and
    chain type re-export exposed through `chain.py` is present in the matching
    owner module `__all__`, mapped in `_CHAIN_STAGE_CATALOG_TYPE_COMPAT_*`, and
    identity-equal through the legacy facade. Source-of-truth
    identity/contract: `services.orchestrator.chain_stages`,
    `services.orchestrator.chain_types`, `services.orchestrator.chain`, and
    `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` agree on the
    `chain-stage-catalog-type-reexports` group. Surfaces: Producers:
    `chain_stages.py` and `chain_types.py`; Compatibility facade:
    `chain.py` static imports and package-level lazy exports; Validators:
    orchestration chain tests, entropy inventory guard, OpenSpec validation,
    markdownlint, ruff, and diff-check; Storage/cache/query: no DB or artifact
    changes.
  - Regression Rows: `chain_stages.__all__` and `chain_types.__all__` match the
    explicit compatibility name tuples; owner/facade maps cover every stage and
    type export exactly once; `ModelRunAssembly` remains type-owned even when
    manifest builders import it; static stage snapshots and dataclass field
    defaults/type hints remain unchanged; `chain_stages` and `chain_types`
    still import without loading `services.orchestrator.chain`, `httpx`, or
    tile-publisher modules; no 2.3+ owner-family behavior changes in this
    slice.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py`; `uv run pytest -q tests/test_entropy_audit_script.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: update chain inventory group `chain-stage-catalog-type-reexports`.
- [x] 2.3 Chain stage execution owner-family completion.
  - Module/Scope: `services.orchestrator.chain_stage_execution`, `StageExecutionDependencies`, reservation-before-submit, bind-after-submit, polling, timeout, retry bridge, and published-log semantics.
  - Dependencies: 2.1 and 2.2.
  - Out of Scope: reservation protocol internals, retry service internals, tile publisher implementation, array accounting.
  - Fixture Expansion: make the stage-execution compatibility contract explicit
    in `chain.py` with `_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES`,
    `_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES`,
    `_CHAIN_STAGE_EXECUTION_COMPAT_DEPENDENCY_FIELDS`, and
    `_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDERS`; import-time guards verify owner
    functions, owner `__all__`, dependency fields, and legacy
    `ForecastOrchestrator` facade methods stay synchronized.
  - Risk Pack Selection: Selected: Legacy compatibility (old private
    `ForecastOrchestrator` method and monkeypatch paths stay callable);
    Dependency / ownership direction (`chain_stage_execution` remains the
    owner and imports without loading `chain.py`); Schema / field names
    (`StageExecutionDependencies` field order/names are guarded); Runtime
    behavior invariants (reservation-before-submit, bind-after-submit, polling,
    timeout, retry bridge, and published-log semantics continue through the
    focused tests); Test / evidence coverage (compat maps, inventory tokens,
    entropy guard, OpenSpec validation, ruff, markdownlint, and diff-check).
    Not Selected: reservation protocol internals, retry service internals, tile
    publisher implementation, array accounting, manifest assembly,
    worker/source identity, repository behavior, DB schema, API/frontend
    surfaces, or production topology changes.
  - Invariant Matrix: Governing invariant: every legacy stage-execution
    wrapper exposed by `ForecastOrchestrator` maps to a named function in
    `services.orchestrator.chain_stage_execution`, and every dependency needed
    by those owner functions is present in `StageExecutionDependencies`.
    Source-of-truth identity/contract:
    `services.orchestrator.chain_stage_execution`,
    `services.orchestrator.chain`, and
    `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` agree on the
    `chain-stage-execution-forwarders` group. Surfaces: Producers:
    `chain_stage_execution.py` owner functions and dependency dataclass;
    Compatibility facade: `ForecastOrchestrator` private wrappers and
    `_chain_stage_execution_dependencies`; Validators: orchestration chain
    tests, pipeline-log artifact tests, M3 retry e2e tests, entropy inventory
    guard, OpenSpec validation, ruff, markdownlint, and diff-check;
    Storage/cache/query: no DB or artifact schema changes.
  - Regression Rows: `chain_stage_execution` still imports without loading
    `services.orchestrator.chain`; owner function names are present in owner
    `__all__`; facade wrapper names remain callable on `ForecastOrchestrator`;
    `_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDERS` maps each legacy wrapper to the
    expected owner function; `StageExecutionDependencies` field names/order
    remain unchanged; submit/resume/poll/timeout/array/local-publish behaviors
    retain existing reservation, retry, and published-log evidence.
  - Focused Verification:
    - `uv run pytest -q tests/test_orchestration_chain.py tests/test_pipeline_logs_artifacts.py tests/test_e2e_m3.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group `chain-stage-execution-forwarders`.
- [x] 2.4 Chain array-accounting owner-family completion.
  - Module/Scope: `services.orchestrator.chain_array_accounting`, sacct parsing, task evidence, resource metrics, partial status, candidate outcome sanitization.
  - Dependencies: 2.1.
  - Out of Scope: manifest assembly, retry, reservation, tile publication, worker/source identity.
  - Fixture Expansion: make the array-accounting compatibility contract
    explicit in `chain.py` with
    `_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES`,
    `_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES`,
    `_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS`, and
    `_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS`; import-time guards
    verify owner functions, dependency fields, legacy top-level wrappers,
    `ForecastOrchestrator` method wrappers, and current legacy dependency
    bindings stay synchronized.
  - Risk Pack Selection: Selected: Legacy compatibility (old helper imports,
    private method paths, and monkeypatch bindings stay callable); Dependency /
    ownership direction (`chain_array_accounting` remains the owner while
    `chain.py` keeps compatibility glue); Schema / field names
    (`ArrayAccountingDependencies` field order/names and binding targets are
    guarded); Runtime behavior invariants (sacct parsing, task evidence,
    resource metrics, partial status, candidate outcome sanitization, and
    incomplete-accounting gap behavior continue through focused tests); Test /
    evidence coverage (compat maps, inventory tokens, entropy guard, OpenSpec
    validation, ruff, markdownlint, and diff-check). Not Selected: manifest
    assembly, retry, reservation, tile publication, worker/source identity,
    repository behavior, DB schema, API/frontend surfaces, Slurm runtime
    behavior, or production topology changes.
  - Invariant Matrix: Governing invariant: every legacy array-accounting helper
    or method wrapper exposed through `chain.py` maps to its intended owner
    function or intentionally local legacy binding, and every dependency needed
    by array-accounting owner functions is present in
    `ArrayAccountingDependencies`. Source-of-truth identity/contract:
    `services.orchestrator.chain_array_accounting`,
    `services.orchestrator.chain`, and
    `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` agree on the
    `chain-array-accounting-forwarders` group. Surfaces: Producers:
    `chain_array_accounting.py` owner functions and dependency dataclass;
    Compatibility facade: `chain.py` top-level helpers,
    `_array_accounting_dependencies`, and `ForecastOrchestrator` method
    wrappers; Validators: orchestration chain tests, partial-success tests,
    entropy inventory guard, OpenSpec validation, ruff, markdownlint, and
    diff-check; Storage/cache/query: no DB or artifact schema changes.
  - Regression Rows: owner function names remain present; top-level helper and
    method wrapper names remain callable; `_CHAIN_ARRAY_ACCOUNTING_COMPAT_*`
    maps cover each governed wrapper exactly once; `ArrayAccountingDependencies`
    field names/order remain unchanged; dependency bindings still point at
    current legacy chain functions so monkeypatches for log URIs, parse/coerce
    helpers, resource metrics, production status, and candidate sanitization
    remain effective; partial-success and malformed accounting behavior remain
    unchanged.
  - Focused Verification:
    - `uv run pytest -q tests/test_orchestration_chain.py tests/test_partial_success.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group `chain-array-accounting-forwarders`.
- [x] 2.5 Chain manifest owner-family completion.
  - Module/Scope: `services.orchestrator.chain_manifests`, `services.orchestrator.production_contract`, model-run assembly builders and payload serialization, runtime manifest safe writes, manifest index, quality states, residual blockers.
  - Dependencies: 2.1 and 2.2.
  - Out of Scope: array accounting, stage execution, repository persistence, tile publishing.
  - Fixture: add explicit `_CHAIN_MANIFEST_COMPAT_*` maps/guards for direct legacy aliases, top-level manifest wrappers, `ForecastOrchestrator` method forwarders, `AnalysisOrchestrator` manifest method forwarders, and monkeypatch dependency bindings that intentionally call the current `chain.py` facade.
  - Regression Coverage: extend `tests/test_orchestration_chain.py` to prove owner/facade alias identity, wrapper owner maps, forecast/analysis method maps, dependency binding inventory, and inventory token coverage stay aligned with `chain_manifests` / `production_contract`.
  - Risk/Invariant: no manifest assembly, safe-write, runtime-root, quality-state, residual-blocker, production-contract, repository, Slurm, API, or frontend behavior moves in this slice; this only makes the existing manifest owner-family compatibility surface executable and reviewable.
  - Focused Verification:
    - `uv run pytest -q tests/test_orchestration_chain.py tests/test_warm_start_chaining.py tests/test_analysis_pipeline.py tests/test_production_scheduler.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group `chain-manifest-forwarders`.
- [x] 2.6 Chain reservation owner-family completion.
  - Module/Scope: `services.orchestrator.reservation`, reserve/bind/reclaim protocol, Slurm comment contract, chain reservation wrappers.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: repository extraction, retry service behavior, stage execution body.
  - Fixture: add explicit `_CHAIN_RESERVATION_COMPAT_*` maps/guards for direct reservation aliases, owner-backed reserve/bind wrappers, local `_reservation_already_inflight` gate classification, chain-local `_cycle_stage_idempotency_key`, and StageExecutionDependencies reservation bindings.
  - Regression Coverage: extend `tests/test_orchestration_chain.py` to prove owner/facade alias identity, owner wrapper map parity, legacy monkeypatch paths for `reserve_candidate` / `bind_reservation`, local gate behavior, stage-execution dependency bindings, and inventory token coverage.
  - Risk/Invariant: no repository extraction, retry behavior, stage-execution body, reservation protocol behavior, DB schema, Slurm behavior, API/frontend, or display behavior moves in this slice; this only makes the existing reservation compatibility surface executable and reviewable.
  - Focused Verification:
    - `uv run pytest -q tests/test_gateway_reconcile.py tests/test_orchestration_chain.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group `chain-reservation-facade`.
- [x] 2.7 Chain retry owner-family completion.
  - Module/Scope: `services.orchestrator.retry`, retry service/config/backoff, manual retry identity, partial-array retry bridge, `_retry_service_from_env` classification.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: reservation protocol, stage execution polling, repository schema, cancel endpoint behavior, and Slurm cancellation side effects; cancelled-run manual retry identity remains covered by the retry owner tests.
  - Fixture Level: expanded; Repair Intensity: high, because retry/cancellation consistency, retry store transaction release, scheduler default-orchestrator construction, and partial-array shared `CycleOrchestrationContext` state are guarded in this slice.
  - Fixture: add explicit `_CHAIN_RETRY_COMPAT_*` maps/guards for direct retry aliases, constructor retry injection/config seams, local retry bridge methods, store transaction release glue, and chain-local `_retry_service_from_env` factory classification.
  - Regression Coverage:
    - Extend `tests/test_orchestration_chain.py` to prove owner/facade alias identity, constructor seam retention, local retry bridge method presence, `_retry_service_from_env` classification, retry service/store transaction behavior through legacy chain paths, and inventory token coverage.
  - Risk/Invariant:
    - No retry policy, backoff schedule, conflict detection, runtime-root recovery, partial-array retry semantics, reservation protocol, stage-execution polling, repository schema, Slurm behavior, API/frontend, or display behavior moves in this slice.
    - This only makes the existing retry compatibility surface executable and reviewable.
  - Invariant Matrix:
    - Governing invariant: retry policy and retry identity remain owned by `services.orchestrator.retry`, while `chain.py` only preserves legacy import, injection, factory, and stage-bridge compatibility seams.
    - Source-of-truth identity/contract: `RetryService` / `RetryConfig` owner aliases, `compute_backoff_seconds`, manual retry `PipelineJob` identity, `ForecastOrchestrator(retry_service=...)`, and `_retry_service_from_env`.
    - Producers: `services/orchestrator/retry.py` retry classification/submission/runtime-root logic; chain local bridge methods only adapt stage results to retry service inputs.
    - Validators/preflight: `_CHAIN_RETRY_COMPAT_*` guards and `tests/test_orchestration_chain.py` compat assertions.
    - Storage/cache/query: `RetryService.store` / `PipelineStore`; repository lookup fallback in `_retry_job_for_stage_result` remains chain-local compatibility glue.
    - Public routes/entrypoints: no API route change; stable chain facade aliases, `ForecastOrchestrator(retry_service=...)`, `ForecastOrchestrator.from_env()`, and `_retry_service_from_env` legacy import/factory seams stay compatible.
    - Frontend/downstream consumers: no frontend or display change; `services/orchestrator/scheduler.py` continues importing `_retry_service_from_env` and constructing the default `ForecastOrchestrator` path through `chain.py`.
    - Failure paths/rollback/stale state: retry service absence returns no retry, transaction release commits retry store changes, and partial-array retry restores context basins.
    - Evidence/audit/readiness: `chain-retry-facade` inventory row and focused retry/orchestration tests.
    - Regression rows:
      - retry owner aliases imported through `chain.py` -> object identity matches `services.orchestrator.retry`.
      - injected retry service with failed stage result -> legacy chain bridge resolves a retry `PipelineJob`, computes owner backoff, releases store transaction, and returns pending retry job id.
      - no retry service or missing job -> chain bridge returns `None` without mutating reservation, repository schema, or stage execution polling behavior.
      - `_retry_service_from_env` without `DATABASE_URL` -> returns `None`; with database settings -> remains classified as chain-local factory for later dependency-injection work.
      - partial-array retry with original `active_basins` task ids `[0,1,2]`, failed task id `[1]`, and retry success -> second submit contains only basin/task 1.
      - partial-array retry completion -> final context restores original `active_basins` and preserves cleanup semantics for `had_partial`, `last_partial_status`, and `task_outcomes`.
      - scheduler default orchestrator without a custom factory -> `scheduler.py` still uses chain's `_retry_service_from_env` path and passes the returned retry service into `ForecastOrchestrator`.
  - Risk Packs:
    - Public API / stable facade selected for chain retry aliases, `ForecastOrchestrator(retry_service=...)`, `ForecastOrchestrator.from_env()`, and `_retry_service_from_env`.
    - Concurrency / shared state / ordering selected.
    - Legacy compatibility / examples selected.
    - Error handling / rollback / partial outputs selected.
    - Config / project setup selected for `_retry_service_from_env`.
    - Resource limits / discovery selected for bounded retry/backoff semantics through owner tests.
    - File IO/path safety, Schema/columns, Auth/secrets, Release/packaging, Documentation/migration notes, and NHMS domain packs not selected because this slice does not change file IO, schema, credentials, packaging, geospatial/time-series/runtime formats, or display artifacts.
  - Focused Verification:
    - `uv run pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_e2e_m3.py tests/test_orchestration_chain.py`
    - `uv run pytest -q tests/test_production_scheduler.py -k test_slurm_preflight_ready_without_factory_uses_default_orchestrator_path`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group `chain-retry-facade`.
- [x] 2.8 Chain tile-publisher owner-family completion.
  - Fixture Level: expanded; Repair Intensity: high.
  - Module/Scope: `services.tile_publisher`, `services.tile_publisher.publisher`,
    chain tile-publisher imports, failure payload mapping, local publish
    dependency wiring, and local publish log URI evidence.
  - Dependencies: 2.1 and 2.3.
  - Out of Scope: Slurm stage execution semantics, array accounting,
    repository behavior, tile-publisher copyback implementation, delivery DB
    schema, API/frontend/display behavior, and production topology.
  - Stable Facade / Compatibility Surface:
    - `services.orchestrator.chain.TilePublisher`,
      `services.orchestrator.chain.PublishError`, and
      `services.orchestrator.chain.failure_payload` stay as legacy import and
      monkeypatch paths backed by `services.tile_publisher` /
      `services.tile_publisher.publisher`.
    - `ForecastOrchestrator._chain_stage_execution_dependencies()` continues
      wiring `tile_publisher_cls`, `publish_error_cls`, and `failure_payload`
      into `chain_stage_execution.StageExecutionDependencies`.
    - `ForecastOrchestrator._run_local_publish_stage(...)` remains a legacy
      chain method that forwards to
      `services.orchestrator.chain_stage_execution.run_local_publish_stage`.
  - Invariants:
    - Owner/facade alias identity for `TilePublisher` and `PublishError`.
    - Owner/facade function identity for `failure_payload`.
    - Stage-execution dependency fields and chain bindings for
      `tile_publisher_cls`, `publish_error_cls`, and `failure_payload`.
    - Existing monkeypatch path
      `services.orchestrator.chain.TilePublisher` still controls local publish
      behavior through dependency construction.
    - `PublishError` maps through owner `failure_payload` to
      `failed_publish`; generic local publish failures remain redacted
      `PUBLISH_TILES_FAILED` payloads.
    - Local publish still writes advertised published log URIs when
      `NHMS_PUBLISHED_ARTIFACT_ROOT` is configured.
  - Risk Packs:
    - Public API / stable facade selected.
    - Legacy compatibility / examples selected.
    - Error handling / rollback / partial outputs selected for
      `PublishError` and generic local publish failure payloads.
    - File IO/path safety selected for local publish log URI writes.
    - Config / project setup selected for publish-root and URI-prefix
      environment wiring.
    - Concurrency / shared state / ordering, Resource limits / discovery,
      Schema/columns, Auth/secrets, Release/packaging, and NHMS domain packs
      not selected because this slice does not change Slurm ordering,
      tile-publisher internals, DB schema, credentials, packaging,
      geospatial/time-series formats, or display endpoints.
  - Focused Verification:
    - `uv run pytest -q tests/test_orchestration_chain.py tests/test_pipeline_logs_artifacts.py`
    - `uv run pytest -q tests/test_tile_publisher.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group
    `chain-tile-publisher-facade`.
- [x] 2.9 Chain worker/source-identity and time-consistency owner-family completion.
  - Fixture Level: expanded; Repair Intensity: high.
  - Module/Scope: worker/adapter imports, source identity helpers, canonical
    readiness, cycle id/time helpers, source scenario glue, and
    `services.orchestrator.time_consistency` aliasing.
  - Dependencies: 2.1 and 2.5.
  - Out of Scope: manifest schema changes, source product policy changes,
    station-MVT work, forcing producer internals, worker adapter download
    behavior, DB schema, API/frontend/display behavior, Slurm behavior, and
    production topology.
  - Stable Facade / Compatibility Surface:
    - `services.orchestrator.chain.evaluate_canonical_readiness` and
      `expected_converter_version` stay as legacy aliases backed by
      `workers.canonical_converter.converter`.
    - `services.orchestrator.chain.parse_cycle_time`, `format_cycle_time`, and
      `cycle_id_for` stay as legacy aliases backed by
      `workers.data_adapters.base`.
    - `services.orchestrator.chain._check_three_way_time_consistency` stays as
      a legacy private alias backed by
      `services.orchestrator.time_consistency.check_three_way_time_consistency`.
    - `scenario_for_source`, `_auto_trigger_source_policy_identity`,
      `_auto_trigger_source_object_identity`, and
      `_auto_trigger_source_identity_adapter` remain chain-local business glue
      until a later owner decision.
    - `_auto_trigger_source_identity_adapter` continues dynamic GFS/IFS adapter
      imports without forcing those modules to load at `chain.py` import time.
  - Invariants:
    - Owner/facade identity for canonical readiness and cycle time aliases.
    - Owner/facade identity for the time-consistency alias.
    - Chain-local helper classification for scenario/source identity glue.
    - Dynamic adapter metadata for GFS and IFS matches the adapter modules and
      returned adapter/config types.
    - Legacy monkeypatch paths for auto-trigger source policy/object identity
      still fail closed before submission when the provider errors.
    - `scenario_for_source` preserves existing GFS/IFS/custom business
      semantics.
  - Risk Packs:
    - Public API / stable facade selected.
    - Legacy compatibility / examples selected.
    - Error handling / rollback / partial outputs selected for canonical
      readiness provider failures.
    - Config / project setup selected for adapter workspace/object-store
      construction.
    - NHMS domain selected for source identity and cycle-time semantics.
    - Concurrency / shared state / ordering, File IO/path safety, Resource
      limits / discovery, Schema/columns, Auth/secrets, and Release/packaging
      not selected because this slice does not change Slurm ordering, adapter
      download/file IO behavior, discovery limits, schema, credentials,
      packaging, or display endpoints.
  - Focused Verification:
    - `uv run pytest -q tests/test_ifs_forecast_integration.py tests/test_source_identity.py tests/test_warm_start_chaining.py tests/test_orchestration_chain.py`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group
    `chain-worker-adapter-facade`.
- [x] 2.10 Chain persistence/repository ownership decision and extraction/retention.
  - Fixture Level: expanded; Repair Intensity: high.
  - Module/Scope: `PipelineJob`, `PipelineEvent`, `PipelineStore`,
    `OrchestratorRepository`, `PsycopgOrchestratorRepository`, active pipeline
    detection, candidate state, reservations, events, forecast cycles, hydro
    run status, and downstream scheduler/test import compatibility.
  - Dependencies: 2.1, 2.6, and 2.7.
  - Out of Scope: DB migration, scheduler behavior changes, retry policy
    changes, repository SQL behavior changes, persistence schema changes,
    Slurm behavior, API/frontend/display behavior, and production topology.
  - Stable Facade / Compatibility Surface:
    - `services.orchestrator.chain.PipelineJob`, `PipelineEvent`, and
      `PipelineStore` stay as legacy aliases backed by
      `services.orchestrator.persistence`.
    - `OrchestratorRepository` remains a chain-local protocol for the current
      orchestration facade.
    - `PsycopgOrchestratorRepository` remains a chain-owned local
      implementation in this slice; it is not a pure owner-module forwarder.
    - Scheduler/default-orchestrator imports of
      `services.orchestrator.chain.PsycopgOrchestratorRepository` remain
      compatible until a later repository-owner extraction migrates callers.
  - Invariants:
    - Owner/facade identity for persistence aliases matches
      `services.orchestrator.persistence`.
    - Repository protocol and concrete repository surface cover active
      orchestration/pipeline detection, candidate state, reservation, event,
      pipeline-job, forecast-cycle, and hydro-run methods.
    - Inventory metadata records the explicit retained local implementation,
      removal condition, caller migration path, and focused verification
      command.
    - Downstream scheduler/test import paths still resolve through the legacy
      chain facade without changing scheduler behavior.
    - The compatibility guard must not classify the local repository
      implementation as a pure forwarding wrapper.
    - Focused tests with `persistence_repository_compat` in their names prove
      persistence owner alias identity and the chain-local repository retention
      classification; existing package-level legacy export tests are not
      sufficient evidence by themselves.
  - Risk Packs:
    - Public API / stable facade selected.
    - Legacy compatibility / examples selected.
    - Concurrency / shared state / ordering selected for durable reservation
      and active-pipeline semantics.
    - Schema/columns selected as an explicit no-change invariant for
      persistence primitives and repository SQL behavior.
    - Error handling / rollback / partial outputs selected for reservation,
      event, cycle-status, and hydro-run status mutation paths.
    - Config / project setup selected for repository `from_env()` and
      scheduler default-orchestrator construction.
    - File IO/path safety, Resource limits / discovery, Auth/secrets,
      Release/packaging, and NHMS domain packs not selected because this slice
      does not change artifact writes, discovery limits, credentials,
      packaging, geospatial/time-series formats, or display endpoints.
  - Focused Verification:
    - `uv run pytest -q tests/test_gateway_reconcile.py tests/test_production_scheduler.py tests/test_retry_cancel_consistency.py tests/test_real_database_integration.py`
    - `uv run pytest -q tests/test_orchestration_chain.py -k "persistence_repository_compat"`
    - `uv run pytest -q tests/test_entropy_audit_script.py`
    - `uv run ruff check services/orchestrator/chain.py tests/test_orchestration_chain.py`
    - `openspec validate governance-8-module-deepening --strict --no-interactive`
    - `corepack pnpm dlx markdownlint-cli2 --config .markdownlint.yaml docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md openspec/changes/governance-8-module-deepening/tasks.md`
    - `git diff --check`
  - Inventory/Evidence Update: update chain inventory group
    `chain-persistence-repository-facade` with explicit retained local
    implementation evidence and executable guard metadata.
- [x] 2.11 Chain group verification and evidence closeout.
  - Module/Scope: integration gate for chain group.
  - Dependencies: 2.1-2.10.
  - Out of Scope: new orchestration behavior, Slurm behavior changes, DB schema changes.
  - Fixture Level: expanded integration closeout. This slice is evidence-only
    but high impact because it records the chain group's task-to-issue-to-PR
    chain and verifies that the compatibility facade did not grow after the
    owner-family slices.
  - Risk Pack Selection: Selected: Public API / stable facade
    (`ForecastOrchestrator`, `AnalysisOrchestrator`, `OrchestratorConfig`,
    `SlurmGatewayClient`, `HttpSlurmGatewayClient`, and legacy
    `services.orchestrator.chain` import/monkeypatch paths remain stable);
    Legacy compatibility (all nine governed chain groups must stay
    inventoried); Test / evidence coverage (focused chain, retry/cancel,
    gateway reconcile, real-DB integration, and entropy guard suites form the
    group gate); Documentation / migration notes (implementation evidence
    records the chain issue/PR mapping); CI / release governance (closeout
    evidence must line up with merged PR state). Not Selected: moving owner
    behavior, changing orchestration runtime behavior, changing Slurm behavior,
    changing DB schema or SQL semantics, changing API routes, changing
    frontend/display surfaces, changing scheduler behavior, or deleting
    compatibility symbols.
  - Invariant Matrix: Governing invariant: chain group 2.1-2.10 is complete
    before this closeout is checked. Source-of-truth identity/contract:
    `docs/review-loop-log.jsonl`, GitHub issue/PR state, and
    `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` agree on the chain
    task-to-issue-to-PR mapping. Surfaces: Producers: completed PRs #780-#789
    and this closeout PR; Compatibility facade:
    `services/orchestrator/chain.py` plus owner modules; Validators:
    orchestration chain tests, retry/cancel consistency tests, gateway
    reconcile tests, real-DB integration tests, entropy inventory guard,
    OpenSpec validation, and diff-check; Evidence/audit/readiness: chain
    compatibility inventory closeout map and the post-merge review-loop log.
  - Regression Rows: every governed chain group remains present in the
    inventory (`chain-stage-catalog-type-reexports`,
    `chain-stage-execution-forwarders`, `chain-array-accounting-forwarders`,
    `chain-manifest-forwarders`, `chain-reservation-facade`,
    `chain-retry-facade`, `chain-tile-publisher-facade`,
    `chain-worker-adapter-facade`, and `chain-persistence-repository-facade`);
    the final chain evidence map records tasks 2.1-2.11 with issue/PR
    references; focused verification commands pass after the evidence update;
    no chain runtime code, Slurm behavior, DB schema, API route, scheduler,
    readiness, two-node, API-bootstrap, or frontend groups change in this
    slice.
  - Focused Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py tests/test_gateway_reconcile.py tests/test_real_database_integration.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py`;
    `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
  - Inventory/Evidence Update: record final chain issue/PR mapping in implementation evidence.

## 3. Two-Node E2E Lane Deepening

- [x] 3.1 Shared two-node evidence contracts.
  - Module/Scope: shared lane result adapter, current-run binding, producer/source artifact validation, strict identity, approved-root path safety, redaction, log URI safety.
  - Dependencies: None.
  - Out of Scope: moving individual lane evaluators or final aggregation.
  - Fixture Level: expanded; Repair Intensity: high, because these shared
    contracts are consumed by every later two-node lane extraction and guard
    final PASS/BLOCKED/FAIL semantics without moving lane evaluators yet.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (existing evidence bundle aliases and lane summary fields stay
    accepted); Schema / field names (strict identities, current-run binding
    aliases, producer proof containers, blocker/finding namespaces, and final
    summary fields remain unchanged); File IO / path safety / overwrite
    (approved roots, safe bounded JSON reads, no symlink/traversal, and
    `EvidenceWriter` safety stay guarded); Error handling / rollback / partial
    outputs (stale, unsafe, incomplete, non-authoritative, private-log, and
    unredacted evidence remain blockers/findings, not silent PASS);
    Documentation / migration notes (two-node lane inventory records shared
    contract ownership and focused verification). Not Selected: moving
    metadata, Docker, readonly DB, API, browser, logs, manual ops, simple-live,
    cross-plane, or final aggregation evaluators; changing live evidence
    product semantics; changing DB roles/schema; changing API/frontend/display
    behavior; changing Slurm/compute execution behavior.
  - Invariant Matrix: Governing invariant: shared two-node contracts are
    single-source behind the stable validator before lane extraction starts.
    Source-of-truth identity/contract:
    `services/production_closure/two_node_e2e_evidence.py` shared contract
    metadata, `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, and
    the two-node E2E spec agree on the lane result adapter, strict identity,
    current-run binding, producer/source-artifact validation, redaction,
    approved-root path safety, and log URI safety contracts. Surfaces:
    Producers: current evidence bundle JSON and future lane owner modules;
    Compatibility/stable entrypoint: `validate_two_node_e2e_evidence(config)`;
    Validators/preflight: `tests/test_two_node_e2e_evidence.py` focused
    producer/source-artifact/strict-identity/metadata/source-scope/safety
    selectors and OpenSpec validation; Storage/cache/query: bounded JSON evidence artifacts
    under approved roots; Public outputs: final summary JSON, lane summaries,
    source-scope results, blockers/findings, and redacted evidence; Failure
    paths/rollback/stale state: stale current-run IDs, unsafe artifact paths,
    wrapper-only proof, private or credential-bearing log URIs, and redaction
    depth failures.
  - Regression Rows: shared contract metadata contains the seven governed
    contracts (`lane-result-adapter`, `current-run-binding`,
    `producer-source-artifacts`, `strict-identity`, `approved-root-path-safety`,
    `redaction`, and `log-uri-safety`) with owner, consumers, guard symbols,
    blocker/finding namespaces, and focused verification commands; the two-node
    inventory contains each shared contract row and Guard Hook Seed token;
    focused tests prove producer proof cannot be wrapper-only, source artifacts
    must be current-run and approved-root bound, strict identity feeds
    source-scope and downstream lane matching, final/lane summaries stay
    redacted, and private/unsafe log URIs block final PASS; no lane evaluator or
    final aggregation behavior moves in this slice.
  - Focused Verification:
    - `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"`.
    - `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"`.
    - `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs or log_uri or redaction or evidence_root or path_safety or stale"`.
  - Inventory/Evidence Update: update `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` shared-contract rows.
- [x] 3.2 Metadata and strict-identity lane extraction.
  - Module/Scope: metadata aliases, source-scope resolution, reduced-scope flags, five-field identities, downstream source-lane seeding.
  - Dependencies: 3.1.
  - Out of Scope: source proof lanes, cross-plane aggregation, final aggregation.
  - Fixture Level: expanded; Repair Intensity: high, because metadata and
    strict identities seed every downstream source-scoped lane and final source
    summaries, while the stable public validator entrypoint must remain
    unchanged.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (metadata filename aliases, status aliases, configured source
    overrides, and reduced-scope flags remain accepted); Schema / field names
    (`RUN_METADATA_SCHEMAS`, five-field strict identities, source aliases, and
    blocker/finding namespaces stay stable); Error handling / partial outputs
    (missing metadata, stale bundle ID, undeclared source, incomplete identity,
    duplicate identity, and reduced source scope keep the same blocker/finding
    behavior); Documentation / migration notes (two-node inventory records the
    new owner module and guard symbols). Not Selected: moving API/browser/logs
    source proof lanes; moving readonly DB, manual ops, cross-plane, or final
    aggregation; changing DB roles/schema; changing API/frontend/display or
    Slurm behavior.
  - Invariant Matrix: Governing invariant: metadata source scope and strict
    identity resolution are owned by
    `services.production_closure.two_node_e2e_metadata_lane` while
    `validate_two_node_e2e_evidence(config)` remains the stable composition
    boundary. Source-of-truth identity/contract:
    `METADATA_DOCUMENT_CANDIDATES`, `RUN_METADATA_SCHEMAS`,
    `STRICT_LOG_IDENTITY_FIELDS`, `MetadataScope`, `MetadataLaneEvaluation`, and
    `evaluate_metadata_lane(...)` agree with the two-node inventory and spec.
    Surfaces: Producers: run/identity metadata JSON; Compatibility/stable
    entrypoint: `validate_two_node_e2e_evidence(config)`; Validators/preflight:
    focused metadata/strict-identity/source-scope tests plus OpenSpec; Public
    outputs: top-level `metadata`, `strict_identity`, `lane_summaries.metadata`,
    and downstream `source_scope_results`; Failure paths/rollback/stale state:
    stale evidence run IDs, unsupported schemas, missing source scope,
    incomplete strict identities, source key mismatches, duplicate sources, and
    reduced-scope PARTIAL semantics.
  - Regression Rows: new owner module exposes metadata aliases, schema
    constants, strict identity constants, owner result dataclass, scope
    resolver, metadata evaluator, and strict identity resolver; aggregator calls
    owner result and still seeds readonly/API/browser/logs/manual/cross-plane
    lanes with identical `declared_sources` and `strict_identities`; direct
    owner tests prove reduced-scope flags, five-field identities, and
    downstream source-scope seeding; inventory row records current owner and
    guard hook tokens; no source proof lane, cross-plane aggregation, or final
    aggregation moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"`.
  - Inventory/Evidence Update: update two-node inventory row `metadata`.
- [x] 3.3 Docker preflight lane extraction.
  - Module/Scope: Docker preflight current-run, disk/command/resource checks, approved-root rules, Docker root resource evidence, blocker namespace.
  - Dependencies: 3.1.
  - Out of Scope: Docker security child artifacts, display readonly proof, final aggregation.
  - Fixture Level: expanded; Repair Intensity: medium-high, because Docker
    preflight is already a focused owner module but this slice completes its
    discovery aliases, resource/command/path guard metadata, and inventory
    ownership without touching Docker security or final aggregation.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (all Docker preflight filename aliases stay accepted); Schema /
    field names (`DOCKER_PREFLIGHT_SCHEMA`, required command names, required
    disk labels, DockerRootDir resource evidence, and blocker namespaces stay
    stable); File IO / path safety / overwrite (preflight `evidence_root` and
    `tmpdir` approved-root checks stay wired through shared helpers while
    host `docker_root_dir` remains resource evidence); Error handling / partial
    outputs (missing lane, stale run, missing resource, missing/failed command,
    low/non-numeric disk, unsafe recorded path, and producer blockers keep the
    same BLOCKED semantics); Documentation / migration notes (two-node
    inventory records the current owner module and guard symbols). Not
    Selected: Docker security child artifacts, display readonly proof, final
    aggregation, DB roles/schema, API/frontend/display behavior, or Slurm
    behavior.
  - Invariant Matrix: Governing invariant: Docker preflight contract checks are
    owned by `services.production_closure.two_node_e2e_docker_preflight` while
    the aggregator keeps stable composition and shared helper injection.
    Source-of-truth identity/contract: `DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES`,
    `DOCKER_PREFLIGHT_SCHEMA`, `DOCKER_PREFLIGHT_REQUIRED_COMMANDS`,
    `DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS`,
    `DOCKER_PREFLIGHT_BLOCKER_NAMESPACES`,
    `DockerPreflightEvaluationHelpers`, and `evaluate_docker_preflight(...)`
    agree with the two-node inventory and spec. Surfaces: Producers:
    `docker-preflight/*` JSON summaries; Compatibility/stable entrypoint:
    `validate_two_node_e2e_evidence(config)`; Validators/preflight: focused
    Docker preflight tests plus OpenSpec; Public outputs:
    `lane_summaries.docker_preflight`; Failure paths/rollback/stale state:
    stale or missing current-run IDs, unsupported schema, missing resource
    evidence, unsafe recorded paths, missing/failed commands, invalid disk
    evidence, DockerRootDir absence, and producer blockers.
  - Regression Rows: owner module exposes discovery aliases, schema,
    command/disk/resource constants, blocker namespaces, helper dataclass, and
    evaluator; aggregator uses owner discovery aliases and helper injection
    while keeping final summary shape unchanged; direct tests prove owner guard
    metadata and every preflight alias fallback; existing focused tests prove
    current-run, disk/command/resource, approved-root, DockerRootDir, and
    blocker namespace behavior; inventory row records current owner and guard
    hook tokens; no Docker security child artifact, display readonly proof, or
    final aggregation moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"`.
  - Inventory/Evidence Update: update two-node inventory row `Docker preflight`.
- [x] 3.4 Docker security lane extraction.
  - Module/Scope: Docker security child/source artifacts, display-readonly runtime proof, forbidden capability findings, readonly published/root filesystem proof.
  - Dependencies: 3.1 and 3.3.
  - Out of Scope: readonly DB lane, API/browser/logs lanes, manual ops.
  - Fixture Level: expanded; Repair Intensity: high, because Docker security
    moves a large owner family with child/source artifact reads, raw Docker
    proof aliasing, source-trust checked-path contracts, display runtime proof,
    and forbidden capability findings while preserving the stable final
    validator entrypoint.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (all Docker security filename aliases stay accepted in owner
    discovery order); Schema / field names (`DOCKER_SECURITY_SUMMARY_SCHEMA`,
    `DOCKER_SECURITY_CHILD_SCHEMAS`, false/true Docker proof aliases,
    source-trust required labels, and blocker/finding namespaces stay stable);
    File IO / path safety / overwrite (child artifact path, sha256, current-run
    containment, approved-root, bounded JSON, and safe-read checks stay wired
    through shared helpers); Error handling / partial outputs (missing lane,
    stale run, missing child, stale/unscoped child, unsafe path, hash mismatch,
    schema mismatch, producer blockers/findings, missing proof, forbidden
    capability, and writable published/root evidence preserve BLOCKED/FAIL
    semantics); Documentation / migration notes (two-node inventory records the
    current owner module and guard symbols). Not Selected: readonly DB lane,
    API/browser/logs source lanes, manual ops, DB roles/schema, frontend/display
    behavior beyond Docker display proof evidence, Slurm runtime behavior, or
    final aggregation movement.
  - Invariant Matrix: Governing invariant: Docker security contract checks are
    owned by `services.production_closure.two_node_e2e_docker_security` while
    the aggregator keeps stable composition and shared helper injection.
    Source-of-truth identity/contract: `DOCKER_SECURITY_DOCUMENT_CANDIDATES`,
    `DOCKER_SECURITY_SUMMARY_SCHEMA`, `DOCKER_SECURITY_CHILD_SCHEMAS`,
    `DOCKER_REQUIRED_FALSE_PROOFS`, `DOCKER_REQUIRED_TRUE_PROOFS`,
    `DOCKER_FORBIDDEN_BOOL_KEYS`, `DOCKER_FORBIDDEN_FINDING_TOKENS`,
    `DOCKER_SOURCE_TRUST_COMMON_REQUIRED_LABELS`,
    `DOCKER_SOURCE_TRUST_ROLE_LABELS`, `DockerSecurityEvaluationHelpers`, and
    `evaluate_docker_security(...)` agree with the two-node inventory and spec.
    Surfaces: Producers: `docker-security/*` and `docker-smoke*` JSON summaries
    plus source child artifacts; Compatibility/stable entrypoint:
    `validate_two_node_e2e_evidence(config)`; Validators/security: focused
    Docker security/display tests plus OpenSpec; Public outputs:
    `lane_summaries.docker_security`; Failure paths/rollback/stale state:
    stale/missing current-run IDs, unsupported summary/child schemas, missing or
    stale children, unsafe/unapproved paths, invalid JSON, hash mismatch,
    missing live Docker evidence, missing display readonly proof, forbidden
    Docker capability findings, writable published/root filesystem proof, and
    producer blockers/findings.
  - Regression Rows: owner module exposes discovery aliases, summary/child
    schemas, Docker false/true proof aliases, forbidden bool/finding tokens,
    source-trust label constants, helper dataclass, evaluator, and blocker
    namespaces; aggregator uses owner discovery aliases and helper injection
    while keeping final summary shape unchanged; direct tests prove owner guard
    metadata and every security alias fallback, including legacy smoke aliases
    that require child artifact path/sha rewrites in fixtures; existing focused
    tests prove child/source artifacts, source-trust role env proof, checked
    paths, raw inspect hazards, published mount readonly proof, root filesystem
    readonly proof, display runtime role proof, forbidden capability findings,
    missing-proof blockers, and fail/block status semantics; inventory row
    records current owner and guard hook tokens; no readonly DB lane,
    API/browser/logs lanes, or manual ops move in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"`.
  - Inventory/Evidence Update: update two-node inventory row `Docker security`.
- [x] 3.5 Readonly DB lane extraction.
  - Module/Scope: readonly DB source/sibling artifacts, live readonly proof, route identity, no-write probes, source coverage, recomputed status.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: Docker security, API/browser/logs source lanes, DB schema or role changes.
  - Fixture Level: expanded; Repair Intensity: high, because readonly DB
    moves a large product-safety owner family with authoritative sibling files,
    merged per-source artifacts, live validation provenance, route strict
    identity, manual-action no-write probes, permission/catalog mutation
    findings, and recomputed PASS/BLOCKED/FAIL semantics while preserving the
    stable final validator entrypoint.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (both readonly DB filename aliases stay accepted in owner
    discovery order); Schema / field names (`nhms.readonly_db_boundary.evidence.v1`,
    `validation_provenance`, `role`, `route_smoke`,
    `permission_probes`, `manual_action_probes`, and blocker/finding namespaces
    stay stable); File IO / path safety / overwrite (sibling/source artifact
    paths, sha256, approved roots, current-run binding, bounded JSON, and
    safe-read checks stay wired through shared helpers); Auth / permissions /
    secrets (database URL redaction, readonly role proof, mutating catalog
    privileges, successful mutation probes, and manual-action no-write proof
    preserve fail-closed behavior); Error handling / partial outputs (missing
    lane, stale run, missing live provenance, missing source coverage,
    sibling mismatch, stale/unscoped child evidence, route identity mismatch,
    manual-action write proof, permission coverage gap, and producer
    blockers/findings preserve BLOCKED/FAIL semantics); Documentation /
    migration notes (two-node inventory records the current owner module and
    guard symbols). Not Selected: Docker security, API/browser/logs lanes,
    manual ops lane extraction, DB schema/role creation, frontend/display route
    implementation, Slurm runtime behavior, or final aggregation movement.
  - Invariant Matrix: Governing invariant: readonly DB contract checks are
    owned by `services.production_closure.two_node_e2e_readonly_db_lane` while
    the aggregator keeps stable composition and shared helper injection.
    Source-of-truth identity/contract: readonly DB document candidates, live
    schema, required route names, strict route identity fields, required
    permission targets/operations, required authoritative sibling/source
    artifact filenames, manual no-write proof aliases, helper dataclass, and
    `evaluate_readonly_db(...)` agree with the two-node inventory and spec.
    Surfaces: Producers: `db/readonly-db-boundary/*` and merged per-source
    readonly DB artifacts; Compatibility/stable entrypoint:
    `validate_two_node_e2e_evidence(config)`; Validators/security: focused
    readonly DB tests plus OpenSpec; Public outputs:
    `lane_summaries.readonly_db`; Failure paths/rollback/stale state:
    stale/missing current-run IDs, unsupported schema, missing live proof,
    unsafe/unapproved paths, invalid JSON, hash mismatch, source coverage gaps,
    sibling mismatch, route strict identity gaps, manual-action write proof,
    mutating privileges, successful mutation probes, and recomputed status
    disagreement.
  - Regression Rows: owner module exposes discovery aliases, live schema,
    required route/manual/permission/source-artifact constants, helper
    dataclass, evaluator, guard symbols, and blocker namespaces; aggregator
    uses owner discovery aliases and helper injection while keeping final
    summary shape unchanged; direct tests prove owner guard metadata and both
    readonly DB alias fallbacks; existing focused tests prove live provenance,
    source artifact path/sha/current-run/root binding, authoritative sibling
    recomputation, route strict identity, source coverage, manual action
    no-write proof, permission operation coverage, mutating finding, and
    fail/block/pass status semantics; inventory row records current owner and
    guard hook tokens; no Docker security, API/browser/logs, manual ops, DB
    schema, DB role, or final aggregation behavior moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"`.
  - Inventory/Evidence Update: update two-node inventory row `readonly DB`.
- [x] 3.6 Simple live lane helper and Slurm/compute/display lanes.
  - Module/Scope: shared simple-live helper plus Slurm, compute summary, and display summary lanes.
  - Dependencies: 3.1.
  - Out of Scope: Docker, readonly DB, API/browser/logs, manual ops, cross-plane.
  - Fixture Level: expanded; Repair Intensity: medium-high, because this
    slice moves a shared helper used by three runtime summary lanes with
    different document aliases, live-proof flags, and PASS aliases while
    preserving stable final-validator composition.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, CLI behavior, final summary
    schema, and `LaneEvaluation.to_summary` stay stable); Legacy compatibility
    / examples (Slurm, 22-compute, compute-summary, 27-display, and
    display-summary filename aliases stay accepted in owner discovery order);
    Schema / field names (live flags `live_slurm_evidence`,
    `live_compute_evidence`, `live_display_evidence`, and pass aliases
    `ready`/`submitted` stay stable); File IO / path safety / overwrite
    (source artifact path, sha256, approved-root, current-run, recursive stale,
    and bounded JSON checks stay wired through shared helper contracts);
    Error handling / partial outputs (missing lane, stale run, nested stale
    evidence, missing live evidence, missing producer proof, mock evidence,
    and pass-alias mismatch preserve BLOCKED/FAIL semantics); Documentation /
    migration notes (two-node inventory records current owners and guard
    symbols for the shared helper plus the three lanes). Not Selected: Docker,
    readonly DB, API/browser/logs, manual ops, cross-plane/source-scope
    aggregation, DB schema/role changes, frontend/display route behavior,
    Slurm scheduling behavior, or final aggregation movement.
  - Invariant Matrix: Governing invariant: simple-live status/current-run/
    producer/live-proof/mock semantics are owned by a focused production
    closure module while the aggregator keeps stable composition and helper
    injection. Source-of-truth identity/contract: Slurm, compute summary, and
    display summary document candidates, lane names, live flags, pass aliases,
    helper dataclass, evaluator, blocker namespaces, and focused verification
    commands agree with the inventory and spec. Surfaces: Producers:
    `slurm/*`, `22-compute/*`, `compute*`, `27-display/*`, and `display*`
    summaries; Compatibility/stable entrypoint:
    `validate_two_node_e2e_evidence(config)`; Validators/preflight: focused
    simple-lane selectors plus OpenSpec; Public outputs:
    `lane_summaries.slurm`, `lane_summaries.compute_summary`, and
    `lane_summaries.display_summary`; Failure paths/rollback/stale state:
    missing lane files, stale current-run fields, stale nested producer proof,
    missing live evidence, missing producer evidence, mock/fixture findings,
    and pass alias normalization.
  - Regression Rows: owner module exposes simple-live helper metadata,
    Slurm/compute/display document aliases, live flags, pass aliases, helper
    dataclass, evaluator, guard symbols, and blocker namespaces; aggregator
    uses owner discovery aliases and helper injection while keeping final
    summary shape unchanged; direct tests prove owner guard metadata, each lane
    alias fallback, every pass alias, non-PASS status preservation, missing
    lane summary shape, mock/fixture failure, and flat-alias producer artifact
    scope; existing focused tests continue covering live flag constants,
    producer proof requirements, recursive stale current-run blockers, and
    final redacted summary compatibility; inventory rows record current owner
    and guard hook tokens; no Docker, readonly DB, API/browser/logs, manual ops,
    cross-plane, DB schema/role, frontend/display route, Slurm scheduling, or
    final aggregation behavior moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"`.
  - Inventory/Evidence Update: update two-node inventory rows `Slurm proof`, `compute summary`, and `display summary`.
- [x] 3.7 API proof lane extraction.
  - Module/Scope: API source lane required checks, live proof flags, producer-backed command/request/response/artifact proof, per-source scope contribution.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: browser/logs source lanes, API route implementation, final aggregation.
  - Fixture Rows: Positive parity: API PASS bundle keeps identical `lane_summaries.api`
    status, summary status, blockers/findings shape, redacted evidence path/hash,
    and `source_scope_results` GFS/IFS contribution through the stable
    `validate_two_node_e2e_evidence(config)` entrypoint; Negative/parity:
    missing API summary, stale run IDs, missing live API flag, missing producer
    proof, mock/fixture evidence, historical latest fallback, missing declared
    source, missing/non-PASS required checks, and strict identity mismatches
    preserve existing blocker/finding namespaces; Legacy compatibility: input
    aliases remain `api/summary.json` then `api/evidence.json`, shared producer
    and strict-identity contracts remain single-source, and browser/logs
    continue through their current source-lane path.
  - Risk Axes: Entrypoints: `validate_two_node_e2e_evidence(config)`,
    `_load_lane_documents`, and `lane_summaries.api`; Source scope:
    downstream `source_scope_results` aggregation for full GFS/IFS scope and
    reduced scope; Producer proof: command/request/response/artifact and
    source-scoped per-check evidence; Identity: four-field API source/check
    matching (`run_id`, `source`, `cycle_time`, `model_id`) plus producer proof
    binding to the check name and evidence run; Failure paths/rollback/stale
    state: missing lane file, stale current-run fields, stale nested producer
    proof, mock/fixture evidence, historical latest fallback, missing source,
    missing required check, non-PASS check, source/check FAIL/BLOCKED/PARTIAL.
  - Regression Rows: owner module exposes API document aliases, required check
    tuple, live flag, helper dataclass, evaluator, guard symbols, and blocker
    namespaces; aggregator uses owner discovery aliases and helper injection
    while keeping final summary and source-scope composition unchanged; direct
    tests prove owner guard metadata, owner-vs-aggregator PASS parity, alias
    fallback for both API candidate files, missing API summary shape, and
    source-scope lane status contribution; existing focused API tests continue
    covering producer proof, strict identity, stale current-run, mock/historical
    evidence, required checks, and source-scoped proof edge cases; inventory row
    records the current owner and retained aggregator/shared-contract surfaces;
    no browser, logs, manual ops, cross-plane, final aggregation, API route,
    frontend/display route, DB schema/role, or Slurm scheduling behavior moves
    in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"`.
  - Inventory/Evidence Update: update two-node inventory row `API proof`.
- [x] 3.8 Browser proof lane extraction.
  - Module/Scope: browser source lane, source-switch proof, job-like check identity, live browser evidence, per-source scope contribution.
  - Dependencies: 3.1, 3.2, and 3.7.
  - Out of Scope: API/logs lane behavior, frontend UI changes, final aggregation.
  - Fixture Rows: Positive parity: browser PASS bundle keeps identical
    `lane_summaries.browser` status, summary status, blockers/findings shape,
    redacted evidence path/hash, and `source_scope_results` GFS/IFS contribution
    through the stable `validate_two_node_e2e_evidence(config)` entrypoint;
    Negative/parity: missing browser summary, stale current-run fields, missing
    live browser flag, missing producer proof, mock/fixture evidence, historical
    latest fallback, missing declared source, missing/non-PASS required checks,
    strict identity mismatch/incomplete, job-like check missing `job_id`, and
    source-switch gaps for multi-source scope preserve existing
    blocker/finding namespaces; Legacy compatibility: input aliases remain
    `browser/summary.json` then `browser/evidence.json`, shared producer and
    strict-identity contracts remain single-source, and API/logs continue
    through their current owner/source-lane paths.
  - Risk Axes: Entrypoints: `validate_two_node_e2e_evidence(config)`,
    `_load_lane_documents`, and `lane_summaries.browser`; Source scope:
    downstream `source_scope_results` aggregation for full GFS/IFS scope and
    reduced/single-source scope; Producer proof: browser/network/artifact,
    command/request/response, and source-scoped per-check evidence; Identity:
    four-field browser source/check matching plus `job_id` for `ops_jobs` and
    `ops_job_logs`; Failure paths/rollback/stale state: missing lane file,
    stale current-run fields, stale nested producer proof, mock/fixture
    evidence, historical latest fallback, missing source, missing source-switch
    check, missing required check, non-PASS check, source/check
    FAIL/BLOCKED/PARTIAL, and job-like identity incompleteness.
  - Regression Rows: owner module exposes browser document aliases, required
    check resolver, live flag, helper dataclass, evaluator, guard symbols, and
    blocker namespaces; aggregator uses owner discovery aliases and helper
    injection while keeping final summary and source-scope composition
    unchanged; direct tests prove owner guard metadata, owner-vs-aggregator
    PASS parity, alias fallback for both browser candidate files, missing
    browser summary shape, source-scope lane status contribution, single-source
    source-switch omission allowance, multi-source source-switch requirement,
    job-like `job_id` binding, required-check missing/failed/blocked/partial
    paths, strict identity/historical-latest negative paths, source
    FAIL/BLOCKED/PARTIAL folding, and lane/check mock evidence; inventory row
    records the current owner and retained aggregator/shared-contract surfaces;
    no API, logs, manual ops, cross-plane, final aggregation, frontend UI, API
    route, DB schema/role, Slurm scheduling, or production topology behavior
    moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"`.
  - Inventory/Evidence Update: update two-node inventory row `browser proof`.
- [x] 3.9 Logs lane extraction.
  - Module/Scope: logs source lane, strict log identity, published log URI safety, typed unavailable proof, redaction.
  - Dependencies: 3.1, 3.2, and 3.7.
  - Out of Scope: private compute log publication changes, API/browser source lanes, final aggregation.
  - Fixture Rows: Positive parity: logs PASS bundle keeps identical
    `lane_summaries.logs` status, summary status, blockers/findings shape,
    redacted evidence path/hash, and `source_scope_results` GFS/IFS
    contribution through the stable `validate_two_node_e2e_evidence(config)`
    entrypoint; Negative/parity: missing logs summary, stale current-run
    fields, missing live log flag, missing producer proof, missing declared
    source, missing/non-PASS `job_logs` check, strict log identity
    mismatch/incomplete including missing `job_id`, private or unsupported log
    URI, unsafe published/file/s3 URI, credential-bearing URI, missing published
    log read evidence, and typed unavailable identity/status/proof gaps preserve
    existing blocker/finding namespaces; Legacy compatibility: input aliases
    remain `logs/summary.json` then `logs/evidence.json`, shared producer,
    strict-identity, redaction, and log URI safety contracts remain single-source
    helper contracts, and API/browser behavior continues through their owner
    paths.
  - Risk Axes: Entrypoints: `validate_two_node_e2e_evidence(config)`,
    `_load_lane_documents`, and `lane_summaries.logs`; Source scope:
    downstream `source_scope_results` aggregation for full GFS/IFS scope and
    reduced/single-source scope; Producer proof: per-source and per-check
    command/request/response/artifact evidence plus published log read evidence;
    Identity: strict log identity fields including `job_id`, published log URI
    identity parsing, and typed unavailable response identity; Failure
    paths/rollback/stale state: missing lane file, stale current-run fields,
    stale nested producer proof, mock/fixture evidence, historical latest
    fallback, missing source, missing `job_logs`, non-PASS check, source/check
    FAIL/BLOCKED/PARTIAL, private compute/local log paths, unsupported schemes,
    unsafe path components, query/fragment/userinfo/credential-like URI parts,
    missing allowed published roots, and typed unavailable proof gaps.
  - Regression Rows: owner module exposes logs document aliases, required
    checks, job-id-required checks, live flag, helper dataclass, evaluator, guard
    symbols, blocker namespaces, and focused verification constant; aggregator
    uses owner discovery aliases and helper injection while keeping final summary
    and source-scope composition unchanged; direct tests prove owner metadata,
    owner-vs-aggregator PASS parity, validator delegation to owner evaluator,
    alias fallback for both logs candidate files, missing logs summary shape,
    missing declared source propagation, missing live flag, `job_logs` `job_id`
    binding, required-check missing/failed/blocked/partial paths, direct log URI
    redaction/safety parity, and typed unavailable acceptance; existing focused
    logs/log_uri tests continue to cover canonical `published://`, `file://`,
    and `s3://` allowlist behavior, private-path rejection, unavailable-log
    semantics, identity parsing, and redaction; inventory row records the current
    owner and retained aggregator/shared-contract surfaces; no API, browser,
    manual ops, cross-plane, final aggregation, frontend UI, API route, DB
    schema/role, Slurm scheduling, private compute log publication, or
    production topology behavior moves in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"`.
  - Inventory/Evidence Update: update two-node inventory row `logs`.
- [x] 3.10 Manual ops lane extraction.
  - Module/Scope: manual ops receipts, node-27 fail-closed proof, no-side-effect proof, node-22 control receipt provenance, optional receipt artifact validation.
  - Dependencies: 3.1 and 3.2.
  - Out of Scope: production control behavior changes, API route changes, final aggregation.
  - Fixture Rows: `services.production_closure.two_node_e2e_manual_ops_lane`
    owns `MANUAL_OPS_DOCUMENT_CANDIDATES`, `MANUAL_OPS_SCHEMA`,
    required 27 retry/cancel action constants, manual-action response redaction
    aliases, side-effect categories, `ManualOpsLaneEvaluationHelpers`,
    `evaluate_manual_ops_lane`, and public `manual_action_name` /
    `manual_action_outcome_status`; `validate_two_node_e2e_evidence(config)`
    delegates manual ops evaluation to the owner through `_manual_ops_lane_helpers`
    while retaining stable final composition and discovery through the facade.
  - Risk Axes: preserves production operator auth redaction, 27 display
    fail-closed semantics, no-side-effect proof, node-22 `compute_control`
    provenance, receipt source coverage, optional receipt artifact validation,
    strict identity parity, and readonly DB reuse of manual action helpers; no
    production control behavior, API route, cross-plane/source-scope aggregation,
    final aggregation, DB schema/role, Slurm scheduling, frontend/display UI, or
    production topology behavior moves in this slice.
  - Regression Rows: added owner guard, direct evaluator parity, validator
    delegation, document alias discovery, and missing manual ops lane shape
    tests; existing focused manual ops tests continue to cover old boolean-only
    shapes, production auth, 27 receipt rejection, 22 provenance, artifact
    safety/hash/payload/identity checks, response evidence, source declaration,
    fail-closed/no-side-effect matrix, full-source receipt coverage, and IFS
    identity mismatch behavior.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"`.
  - Inventory/Evidence Update: update two-node inventory row `manual ops receipts`.
- [x] 3.11 Cross-plane and source-scope aggregation extraction.
  - Module/Scope: cross-plane lane, source-scope result construction, GFS+IFS full PASS, reduced-scope PARTIAL, strict identity aggregation.
  - Dependencies: 3.2, 3.7, 3.8, and 3.9.
  - Out of Scope: final summary writing, output safety, lane-specific source proof logic.
  - Fixture Rows: `services.production_closure.two_node_e2e_cross_plane_lane`
    owns `CROSS_PLANE_DOCUMENT_CANDIDATES`, `CROSS_PLANE_LIVE_FLAG`,
    cross-plane blocker namespaces, `CrossPlaneEvaluationHelpers`,
    `build_source_scope_results`, `evaluate_cross_plane_lane`,
    `is_full_scope_sources`, and `is_full_scope_pass`;
    `validate_two_node_e2e_evidence(config)` delegates source-scope
    construction and cross-plane lane evaluation to that owner through
    `_cross_plane_helpers` while retaining stable final summary writing and
    blocker/finding collection in the facade.
  - Risk Axes: preserves GFS+IFS full PASS gating, reduced-scope PARTIAL
    semantics, strict log identity completeness aggregation, source-lane status
    folding for API/browser/logs, current-run and producer-backed evidence
    blockers, live cross-plane proof requirements, mock/historical findings,
    and source identity mismatch context; no final summary writing, output path
    safety/redaction, lane-specific source proof logic, API/browser/logs/manual
    ops lane behavior, DB schema/role, Slurm scheduling, frontend/display UI, or
    production topology behavior moves in this slice.
  - Regression Rows: added owner guard, direct evaluator/source-scope parity,
    validator delegation for both `build_source_scope_results` and
    `evaluate_cross_plane_lane`, document alias discovery, missing cross-plane
    lane shape, direct missing strict log identity aggregation, and direct
    reduced-scope PARTIAL tests; existing focused cross-plane/source-scope tests
    continue to cover source-scope status folding, boolean-only live evidence,
    stale/current-run evidence, producer/source-artifact binding, and reduced
    single-source behavior.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"`.
  - Inventory/Evidence Update: update two-node inventory row `source-scope / cross-plane aggregation`.
- [x] 3.12 Two-node final aggregation extraction.
  - Module/Scope: final status ordering, final summary schema, blocker/finding collection, output path safety, redaction, force/existing-output behavior.
  - Dependencies: 3.1-3.11.
  - Out of Scope: moving any lane not already interface-stable, changing final status semantics.
  - Fixture Rows: `services.production_closure.two_node_e2e_final_aggregation`
    owns `FINAL_EVIDENCE_SCHEMA`, final status constants,
    `FINAL_AGGREGATION_*` guard metadata, `TwoNodeE2EEvidenceError`,
    `APPROVED_EVIDENCE_ROOTS`, final output path-safety helpers,
    `EvidenceWriter`, `FinalAggregationHelpers`, `final_status`,
    `collect_blockers_and_findings`, `metadata_summary`,
    `build_final_summary`, and `write_final_summary`;
    `validate_two_node_e2e_evidence(config)` keeps lane orchestration and
    delegates final summary/status/write behavior to the owner after
    `writer.prepare()` has already enforced early output path safety.
  - Risk Axes: preserves FAIL > BLOCKED > PARTIAL > PASS ordering, GFS+IFS
    full PASS requirements, reduced/incomplete source-scope PARTIAL semantics,
    blocker versus finding split, public final summary schema, strict identity
    redaction, approved-root path checks, unsafe run ID rejection, symlink and
    traversal rejection, existing-output `force` behavior, oversized/deep JSON
    write blockers, and facade compatibility re-exports; no lane evaluator,
    source proof contract, cross-plane/source-scope semantics, DB schema/role,
    Slurm scheduling, frontend/display UI, or production topology behavior
    moves in this slice.
  - Regression Rows: added owner guard and facade re-export parity tests for
    final schema/status/error/writer/path helpers; validator delegation test
    proves final summary assembly and write pass through the owner while the
    stable facade entrypoint remains active; direct owner status-ordering test
    covers fail-over-blocked precedence plus reduced/incomplete source scope;
    final output regression covers no-clobber plus force overwrite behavior at
    validator and writer levels; inventory row records the current owner module,
    retained facade entrypoint, compatibility removal condition, guard symbols,
    and focused verification.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"`; `uv run pytest -q tests/test_two_node_e2e_evidence.py`.
  - Inventory/Evidence Update: update two-node inventory row `final aggregation`.
- [x] 3.13 Two-node group verification and evidence closeout.
  - Module/Scope: integration gate for two-node E2E group.
  - Dependencies: 3.1-3.12.
  - Out of Scope: production topology changes, station-MVT closure, live service deployment.
  - Fixture Level: expanded integration closeout; Repair Intensity: high,
    because this evidence-only gate closes the full two-node owner-family
    extraction set and verifies the stable validator entrypoint, lane summary
    schema, final summary schema, blocker/finding namespaces, source-scope
    semantics, and output safety did not drift after tasks 3.1-3.12.
  - Selected Risk Packs: Public API / stable validator entry
    (`validate_two_node_e2e_evidence(config)`, module CLI behavior, lane
    summaries, and final summary schema stay stable); Legacy compatibility /
    facade re-exports (retained aggregator aliases and owner-module guard
    symbols remain inventoried); Schema / field names (lane names, source-scope
    result shape, strict identities, blocker/finding namespaces, and final
    schema remain frozen); File IO / path safety / overwrite (approved evidence
    roots, bounded reads, redaction, no-clobber, `force`, and final output
    safety remain owner-tested); Auth / permissions / secrets (readonly DB,
    manual ops, Docker display proof, redacted database/operator evidence, and
    private log URI boundaries remain fail-closed); Error handling / rollback /
    partial outputs (missing, stale, unsafe, incomplete, non-authoritative,
    reduced-scope, and proven-unsafe evidence still map to BLOCKED, PARTIAL, or
    FAIL rather than silent PASS); Documentation / migration notes (the final
    implementation evidence map records every two-node task, issue, PR, owner,
    and verification command). Not Selected: production topology changes,
    station-MVT closure, live service deployment, DB schema/role changes, Slurm
    scheduling behavior, API route behavior, frontend/display UI behavior, or
    new lane/product semantics.
  - Invariant Matrix: Governing invariant: the completed two-node E2E lane
    decomposition is fully accounted for by owner modules and inventory
    evidence, while `validate_two_node_e2e_evidence(config)` remains the stable
    compatibility boundary. Source-of-truth identity/contract: completed tasks
    3.1-3.12, issue/PR records #732-#743, the two-node E2E spec, the
    inventory guard seed, and focused tests agree on shared contracts, lane
    closure/discovery, metadata, Docker preflight/security, readonly DB, API,
    browser, logs, simple-live lanes, manual ops, cross-plane/source-scope
    aggregation, producer/source artifacts, and final aggregation. Surfaces:
    Producers: owner modules under `services.production_closure.two_node_e2e_*`;
    Compatibility/stable entrypoint: `services.production_closure.two_node_e2e_evidence`;
    Validators/preflight: full `tests/test_two_node_e2e_evidence.py`, scoped
    ruff, OpenSpec validation, and diff-check; Public outputs:
    `lane_summaries`, `source_scope_results`, final summary JSON, blockers,
    findings, and redacted evidence; Failure paths/rollback/stale state:
    stale run IDs, unsafe artifact/output paths, private logs, reduced source
    scope, missing producer proof, failed Docker/readonly/manual/source lanes,
    and existing-output no-clobber behavior; Evidence/audit/readiness: final
    implementation evidence map and two-node inventory.
  - Regression Rows: final evidence map contains tasks 3.1-3.13 with issue
    #732-#744, PR #791-#803 closeout record, owner/surface, verification
    command, and inventory/evidence update; inventory guard seed rows cover
    shared contracts, lane closure/discovery, every extracted lane owner,
    producer/source artifacts, source-scope/cross-plane aggregation, and final
    aggregation; full two-node evidence tests pass after the evidence map and
    prove stable validator compatibility; scoped ruff passes for
    `services/production_closure` and the two-node test file; OpenSpec strict
    validation passes; `git diff --check` passes; no out-of-scope production
    topology, station-MVT, live deployment, DB schema/role, Slurm scheduling,
    API route, frontend/display UI, or new lane semantics change in this slice.
  - Focused Verification: `uv run pytest -q tests/test_two_node_e2e_evidence.py`
    -> all two-node owner, facade, path-safety, source-scope, and final
    aggregation regressions pass; `uv run ruff check services/production_closure
    tests/test_two_node_e2e_evidence.py` -> scoped lint passes; `openspec
    validate governance-8-module-deepening --strict --no-interactive` -> change
    remains valid; `git diff --check` -> no whitespace errors.
  - Inventory/Evidence Update: record final two-node issue/PR mapping in
    `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, covering tasks
    3.1-3.13, issues #732-#744, PR numbers, owner/surface, verification
    commands, inventory updates or explicit non-goals, closeout date, and the
    closeout commit/PR once available.

## 4. Readiness Validation Lane Deepening

- [x] 4.1 Readiness item contract extraction.
  - Module/Scope: shared readiness item schema, status/execution-mode truth table, required fields, release-blocker context rules, invalid item namespaces.
  - Dependencies: None.
  - Out of Scope: proof loading, dependency summaries, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "status_execution_mode_truth_table or readiness_schema_validation_item"`.
  - Inventory/Evidence Update: update `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md` row `Readiness item validation`.
  - Selected Risk Packs: public validator entry, legacy compatibility/facade re-exports, schema/field names, release-blocker context safety, error namespace stability, documentation/inventory notes.
  - Invariant Matrix: owner module `services.production_closure.readiness_item_contracts`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `validate_readiness_item`, `STATUS_VALUES`, `EXECUTION_MODE_VALUES`, `EXECUTED_MODES`, `ALLOWED_STATUS_EXECUTION_MODES`, and `ProductionReadinessValidationError`; `validate_readiness(config)` keeps proof loading, dependency summaries, artifact writes, `_final_ready`, release blocker aggregation, and final summary composition in the existing validator boundary; every readiness item contract requires `item_id`, `surface`, `status`, `execution_mode`, `required_for_final`, `live_proof_accepted`, `artifact_refs`, `residual_risk`, `removal_criteria`, `exclusions`, `owner`, and `action`; `release_blocked` items required for final readiness must carry non-empty residual risk and removal criteria; validation errors preserve `PRODUCTION_READINESS_STATUS_INVALID`, `PRODUCTION_READINESS_EXECUTION_MODE_INVALID`, `PRODUCTION_READINESS_STATUS_MODE_INVALID`, `PRODUCTION_READINESS_ITEM_FIELD_MISSING`, and `PRODUCTION_READINESS_BLOCKER_CONTEXT_MISSING`.
  - Regression Rows: owner direct truth-table tests and facade parity tests prove the same allowed status/execution-mode pairs; invalid status, invalid execution mode, invalid pair, missing required field, and missing release-blocker context assert exact `error_code`; invalid produced items still create `schema-validation-{index}` on surface `readiness_schema_validation` with `failed`/`deterministic`, artifact ref `readiness_items.json`, and release blocker presence; a blocker-eligible item missing `item_id`, `owner`, or `action` is rejected by the item contract before release-blocker aggregation can raise `KeyError`; inventory row records the owner module, retained facade surface, removal condition, and focused verification command.
  - Verification Floor: focused pytest command above; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_item_contracts.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
- [x] 4.2 Shared artifact writers extraction.
  - Module/Scope: preflight artifact, environment artifact, evidence writer, safe writes, path rendering, redaction, bounded payloads.
  - Dependencies: 4.1.
  - Out of Scope: live proof receipt parsing, proof-specific validators, final aggregation.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "preflight or side_effect"`; `uv run pytest -q tests/test_production_readiness_validation.py -k "environment_artifact_uses_allowlist"`.
  - Inventory/Evidence Update: update readiness inventory rows `Preflight artifact surface`, `Environment artifact surface`, and the `Shared artifacts and safe writes` guard note.
  - Selected Risk Packs: public validator entry, legacy compatibility/facade re-exports, schema/field names, file IO/path safety/overwrite, redaction/secrets, bounded payloads, error namespace stability, artifact write order, documentation/inventory notes.
  - Invariant Matrix: owner module `services.production_closure.readiness_shared_artifacts`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `EvidenceWriter`, `_preflight_payload`, `_environment_payload`, `_path_for_evidence`, `_redact_paths`, `_bounded_payload`, `_bounded_redacted_payload`, and shared size/depth constants until inventory-backed caller migration; `validate_readiness(config)` keeps item collection, live proof receipt loading, `_receipt_artifact`, dependency/scheduler proof semantics, `_validate_items`, `_release_blockers`, `_final_ready`, summary composition, and CLI behavior in the existing validator boundary; `preflight.json` is written before item collection and preserves schema `nhms.production_readiness.preflight.v1`, issue `181`, run ID, rendered evidence root/dir, dependency roots, scheduler root/file, `live_proof_configured = receipt.status != "missing"`, and all fast-CI live side-effect flags false; `environment.json` is written after release blockers and preserves schema `nhms.production_readiness.environment.v1`, run ID, timestamp, Python/platform/cwd fields, the governed env allowlist, and path/secret redaction; writer/path helpers preserve `MAX_EVIDENCE_PAYLOAD_BYTES`, `MAX_JSON_DEPTH`, `MAX_JSON_NODES`, `MAX_STRING_LENGTH`, no-follow safe writes, containment under evidence/lane roots, no-clobber unless `force`, and public error namespaces `PRODUCTION_READINESS_EVIDENCE_EXISTS`, `PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE`, `PRODUCTION_READINESS_EVIDENCE_SYMLINK`, `PRODUCTION_READINESS_EVIDENCE_WRITE_FAILED`, and `PRODUCTION_READINESS_EVIDENCE_PAYLOAD_TOO_LARGE`.
  - Regression Rows: owner direct tests and facade parity tests prove shared artifact writer/helper object identity and equivalent outputs; preflight tests pin schema, issue, run ID, path rendering, dependency/scheduler references, live-proof configured booleans, and all side-effect flags false; environment tests pin the env allowlist and redaction of secrets/private paths; safe writer tests pin no-clobber, force overwrite, symlink/path containment rejection, bounded payload failure, and redacted JSON writes; path helper tests pin evidence-root/workspace/readiness prefixes and `[redacted-path]` fallback; regression selector names include `shared_artifact` so 4.2 owner/facade/safe-writer coverage is not hidden behind unrelated proof parsing tests.
  - Verification Floor: focused pytest commands above; `uv run pytest -q tests/test_production_readiness_validation.py -k "shared_artifact or preflight or side_effect or environment_artifact_uses_allowlist"`; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_shared_artifacts.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
- [x] 4.3 Shared live-proof loader and receipts artifact extraction.
  - Module/Scope: inline/file ambiguity, proof file size/JSON limits, raw-payload omission, live proof receipts artifact, redaction flags.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: auth/alert/rollback/target-env semantics, dependency proof binding, scheduler proof binding.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "live_proof_receipts_artifact or live_proof_json_traversal"`.
  - Inventory/Evidence Update: update readiness inventory row `Live proof receipts artifact surface`.
  - Fixture Level: expanded; Repair Intensity: high because this slice owns live proof file IO, JSON traversal bounds, public evidence redaction, raw-vs-public payload handling, and a shared helper/facade boundary used by later proof validators.
  - Selected Risk Packs: public validator entry, legacy compatibility/facade re-exports, file IO/path safety/overwrite, evidence JSON/schema/field names, redaction/secrets/private paths, resource limits/large input, error namespace stability, artifact write order, documentation/inventory notes.
  - Risk Packs Considered: auth/permissions/secrets selected only for redaction and no raw secret emission; concurrency/shared state not selected because the loader is single-run and has no mutable global state beyond constants; release/packaging not selected because no dependency or entrypoint packaging changes are expected; proof-specific auth/alert/rollback/target-env/dependency/scheduler semantic validation not selected because tasks 4.6-4.8 own those validators/binders.
  - Invariant Matrix: owner module `services.production_closure.readiness_shared_artifacts`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `PROOF_ENV`, `PROOF_FILE_ENV`, `MAX_RECEIPT_BYTES`, `MAX_RECEIPT_PREVIEW_BYTES`, `LIVE_PROOF_SCHEMA`, `_load_proof`, `_receipt_artifact`, `_receipt_details`, and `_receipt_validation_payload` until inventory-backed caller migration; `validate_readiness(config)` still loads receipts before preflight, writes `preflight.json`, then writes `live_proof_receipts.json`, then builds deterministic/dependency/scheduler/live-proof items; proof-specific validators `_auth_live_item`, `_surface_live_item`, `_surface_live_receipt_errors`, `_common_live_receipt_errors`, dependency binding helpers, scheduler binding helpers, `_final_ready`, release blockers, summary, and CLI behavior remain in the validator boundary; the loader preserves inline/file mutual exclusion, missing receipt status, file reads via bounded no-follow safe filesystem access, 64 KiB receipt limit, UTF-8/JSON/object/depth/node error mapping, raw payload retained only for internal validation, public receipt details omit `raw_payload`, public payload/details are bounded and redacted, and `live_proof_receipts.json` preserves schema `nhms.production_readiness.live_proof_receipts.v1`, run ID, receipt details by surface, and all redaction flags true.
  - Boundary Surfaces: shared helper roots `readiness_shared_artifacts` and retained aliases in `readiness_validation`; public entrypoints `validate_readiness(config)` and validate-readiness CLI unchanged; read surfaces inline proof strings and proof files from `PROOF_ENV`/`PROOF_FILE_ENV`; write surfaces `live_proof_receipts.json` through `EvidenceWriter`; downstream consumers `_preflight_payload`, `_receipt_validation_payload`, proof-specific validators, dependency/scheduler binders, release blockers, final summary, and environment allowlist; failure paths ambiguous inline+file, missing, unsafe/unreadable file, oversized payload, malformed JSON, non-object JSON, excessive depth, excessive node count, and redacted preview.
  - Regression Rows: owner direct tests and facade parity tests prove loader constants/helper object identity and equivalent outputs; inline+file ambiguity returns `PRODUCTION_READINESS_PROOF_AMBIGUOUS`; missing proof returns status `missing` and keeps `preflight.live_proof_configured` false; unsafe or unreadable file returns `PRODUCTION_READINESS_PROOF_FILE_INVALID` with redacted path; oversized inline/file payload returns `PRODUCTION_READINESS_PROOF_TOO_LARGE` with bounded redacted preview; malformed JSON, invalid UTF-8, and non-object JSON return `PRODUCTION_READINESS_PROOF_JSON_INVALID`; excessive depth or node count returns `PRODUCTION_READINESS_PROOF_JSON_LIMIT_EXCEEDED` and bounded public payload; valid parsed receipts keep raw payload available to `_receipt_validation_payload` for later validators but omit `raw_payload` from `live_proof_receipts.json`; public artifact tests assert schema, run ID, all receipt surfaces, redaction flags, secret/path redaction, no private paths/tokens, and bounded artifact size; unchanged proof-specific validators still consume the facade loader output and preserve existing live proof item/release blocker semantics.
  - Verification Floor: focused pytest command above; `uv run pytest -q tests/test_production_readiness_validation.py -k "live_proof or receipt or proof_file"`; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_shared_artifacts.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
- [x] 4.4 Dependency summary reader extraction.
  - Module/Scope: Slurm, object-store, source, E2E, and MVT deterministic dependency summaries, aliases, issue/schema/status checks, artifact refs, sha256 details, review-only final semantics.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: dependency live proof receipts, final live readiness, two-node E2E lane extraction.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or object_store or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or source or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or e2e or existing_m19"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or mvt or existing_m19"`.
  - Inventory/Evidence Update: update readiness inventory dependency-summary rows.
  - Fixture Level: expanded; Repair Intensity: high because this slice owns external evidence readers, summary schema/producer contracts, path-safety checks, digest binding details later consumed by live proof binders, and a compatibility facade boundary behind the stable readiness validator entrypoint.
  - Selected Risk Packs: public validator entry, config/env/CLI root mapping, legacy compatibility/facade re-exports, file IO/path safety/overwrite, evidence JSON/schema/field names, resource limits/large input/discovery, redaction/secrets/private paths, stable blocked/error semantics, deterministic-vs-live readiness separation, run manifest / QC provenance identity, published NHMS artifact / display identity, documentation/inventory notes.
  - Risk Packs Considered (core):
    Public API / CLI / script entry: selected - preserve `validate_readiness(config)`, validate-readiness CLI flags, and `ProductionReadinessConfig.from_env` behavior;
    Config / project setup: selected - preserve `NHMS_PRODUCTION_READINESS_*_EVIDENCE_ROOT` env mapping and explicit CLI/root precedence without adding setup requirements;
    File IO / path safety / overwrite: selected - summary discovery is user/config-rooted file IO with no symlink components, regular-file checks, and bounded reads;
    Schema / columns / units / field names: selected - producer `issue`, `schema`, `status`, `run_id`, `execution_mode`, final-claim flag, artifact ref, and checksum fields are readiness contracts;
    Auth / permissions / secrets: selected narrowly - no auth behavior changes, but public artifacts and CLI stdout/stderr must redact private paths/secrets and never emit raw summary payloads;
    Concurrency / shared state / ordering: not selected - reader is single-run, stateless, and does not introduce mutable shared state or async ordering;
    Resource limits / large input / discovery: selected - aliases are bounded to the summary candidates and reads stay within the 64 KiB readiness ingestion limit;
    Legacy compatibility / examples: selected - retained facade aliases and existing summary path aliases must remain compatible;
    Error handling / rollback / partial outputs: selected - malformed, unsafe, unreadable, or out-of-contract summaries must become stable blocked/not-executed readiness items without changing artifact write/rollback semantics;
    Release / packaging / dependency compatibility: not selected - no dependency, packaging, or distribution entrypoints change beyond stable in-repo imports;
    Documentation / migration notes: selected - readiness inventory dependency-summary rows must record owner, retained facade, focused verification, and review-only semantics.
  - Risk Packs Considered (NHMS domain):
    Geospatial / CRS / basin geometry: not selected - summaries are read as prior producer artifacts and no geometry/CRS transformation runs here;
    Hydro-met time series / forcing windows: not selected - source summary identity is checked, but no provider data window or forcing payload is parsed;
    SHUD numerical runtime / conservation / NaN: not selected - no SHUD execution or numerical output semantics change;
    PostGIS / TimescaleDB domain behavior: not selected - no DB reads/writes or schema behavior change;
    Slurm production lifecycle / mock-vs-real parity: not selected - Slurm summary identity/status is checked, but sbatch/gateway/runtime behavior remains out of scope;
    External hydro-met providers / snapshot reproducibility: not selected - provider snapshots are not loaded or validated in this reader-only slice;
    Run manifest / QC provenance: selected - dependency summary `run_id`, producer issue/schema/status, artifact ref, and sha256 digest are provenance anchors for reviewer lineage and later live-proof binding;
    Published NHMS artifacts / display identity: selected narrowly - E2E/MVT/object-store artifact refs and deterministic-vs-live final-readiness separation must remain stable, without claiming display or live performance proof.
    Dependency live receipt proof binding: not selected - task 4.7 owns live producer provenance, raw alias validation, and summary-binding comparisons before redaction.
  - Invariant Matrix: owner module `services.production_closure.readiness_dependency_summaries`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `DEPENDENCY_SUMMARY_CONTRACTS`, `_dependency_summary_items`, `_read_dependency_summary_item`, `_dependency_summary_blocked`, `_dependency_summary_artifact_ref`, `_dependency_bindings`, and `_find_summary_path` until inventory-backed caller migration; `validate_readiness(config)` still composes deterministic items, dependency summaries, scheduler evidence, live proof items, exclusions, `_validate_items`, `_release_blockers`, `_final_ready`, summary, and CLI behavior in the existing validator boundary; `ProductionReadinessConfig.from_env` still resolves `DEPENDENCY_ROOT_ENV` roots via the shared artifact env map; dependency live proof validators `_dependency_receipt_errors` and alias consistency helpers remain in the validator boundary until 4.7 but may consume the shared summary contract table and summary bindings.
  - Boundary Surfaces: read surfaces `NHMS_PRODUCTION_READINESS_{SLURM,OBJECT_STORE,SOURCE,E2E,MVT}_EVIDENCE_ROOT` and CLI `--*-evidence-root` values; discovery aliases `summary.json`, `<dependency>/summary.json`, and object-store `object_store/summary.json` plus `object-store/summary.json`; filesystem boundary regular files only, no symlink components, bounded 64 KiB reads, and redacted unsafe/missing/unreadable paths; evidence contract fields issue, schema, run id, status, execution mode, final production readiness claim, artifact ref, and sha256 checksum; downstream consumers dependency live proof binding inputs, release blockers, final summary counts, and public readiness artifacts; unchanged sibling surfaces scheduler evidence, live proof loader, proof-specific validators, exclusions, and final aggregation.
  - Regression Rows: owner direct tests and facade parity tests prove contract/helper object identity and equivalent outputs for all five dependency summaries; missing root creates `not_executed` review items without artifact refs and keeps final readiness false; valid Slurm/object-store/source/E2E/MVT summaries create `passed`/`deterministic` items with `required_for_final=false`, `live_proof_accepted=false`, producer issue/schema/status/run id, object-store underscore/hyphen alias compatibility, deterministic artifact refs, and `sha256:` details; malformed JSON, invalid UTF-8, non-object JSON, oversized payload, symlink leaf/ancestor, directory/non-regular file, missing summary, wrong issue/schema/status, and unsafe path errors produce stable `blocked`/`not_executed` items with redacted public details and no traceback/private path leakage; `_dependency_bindings` includes only passed deterministic summaries and preserves summary run id, artifact ref, checksum, and dependency identity for later live proof binders; deterministic dependency summaries alone never satisfy `_final_ready` or final `summary.status=ready`.
  - Verification Floor: focused pytest commands above; `uv run pytest -q tests/test_production_readiness_validation.py -k "dependency_summary or dependency_bindings or existing_m19"`; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_dependency_summaries.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
- [x] 4.5 Scheduler evidence reader extraction.
  - Module/Scope: scheduler evidence root/file mutual exclusion, root scan file limits, schema/pass-id checks, review execution modes, review status allowlists, count/cardinality logic, dry-run no-mutation proof, scheduler identity/count helpers, deterministic review-only item construction, artifact refs, sha256 details, and public redaction.
  - Dependencies: 4.1 and 4.2.
  - Out of Scope: optional live scheduler proof binding, live receipt alias validation, final aggregation extraction, dependency proof binding, changing scheduler/Slurm runtime behavior, changing scheduler evidence producer schema.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "scheduler and evidence"`.
  - Inventory/Evidence Update: update readiness inventory row `Scheduler evidence`.
  - Fixture Size: high (reader extraction has file IO, public artifact, later live-proof binding, and final-readiness adjacency).
  - Risk Packs Considered (standard):
    API/contract compatibility: selected - `validate_readiness(config)` remains the public entrypoint and `services.production_closure.readiness_validation` keeps compatibility aliases for existing scheduler evidence constants/helpers plus live scheduler binding status/mode constants until caller migration is proven;
    CLI / env / config roots: selected - `ProductionReadinessConfig.from_env`, `NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_ROOT`, `NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE`, `--scheduler-evidence-root`, and `--scheduler-evidence-file` must preserve root/file mutual exclusion and optional item creation semantics;
    Serialization / schema / field names: selected - public readiness item fields, details keys, dependency strings, scheduler evidence schema/version aliases, pass id, status, execution mode, count fields, artifact ref, and checksum are contract surfaces;
    Filesystem / path safety: selected - root scan is top-level JSON only, regular files only, no symlink components, bounded 256 KiB reads, deterministic artifact refs, and redacted unsafe/missing/unreadable path errors;
    Security / privacy / redaction: selected - raw scheduler evidence can contain private paths/secrets and must only appear through bounded redacted public details/stdout/artifacts;
    Resource bounds / traversal limits: selected - file count limit, bounded reads, bounded public payload previews, and identity/status traversal must not become unbounded;
    Error handling / rollback / partial outputs: selected - malformed, unsafe, unreadable, oversized, stale, or out-of-contract evidence becomes stable blocked/not-executed review items without changing readiness artifact write/rollback behavior;
    Release / packaging / dependency compatibility: not selected - no distribution/package entrypoints change beyond stable in-repo imports;
    Documentation / migration notes: selected - readiness inventory scheduler evidence row must record owner, retained facade, focused verification, and deterministic review-only semantics.
  - Risk Packs Considered (NHMS domain):
    Geospatial / CRS / basin geometry: not selected - candidate identity may include basin/model/source ids but no geometry/CRS transformation runs here;
    Hydro-met time series / forcing windows: selected narrowly - candidate/source/cycle/run/forcing identity derivation is validated, but no provider time-series payload is loaded;
    SHUD numerical runtime / conservation / NaN: not selected - scheduler evidence references model-run outcomes but does not execute SHUD or inspect numerical outputs;
    PostGIS / TimescaleDB domain behavior: not selected - no DB reads/writes or schema behavior change;
    Slurm production lifecycle / mock-vs-real parity: selected - scheduler evidence distinguishes review modes from live-eligible production orchestration but must not treat deterministic scheduler evidence as final live proof;
    External hydro-met providers / snapshot reproducibility: selected narrowly - source/cycle/run/forcing identity consistency is checked for lineage, without validating provider snapshots;
    Run manifest / QC provenance: selected - scheduler pass id, artifact ref, checksum, candidate/model-run identities, counts, and no-mutation proof are provenance anchors for reviewer lineage and later live scheduler proof binding;
    Published NHMS artifacts / display identity: not selected - no display or published MVT/API artifact identity is read or changed.
  - Invariant Matrix: owner module `services.production_closure.readiness_scheduler_evidence`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `SCHEDULER_EVIDENCE_SCHEMA`, `MAX_SCHEDULER_EVIDENCE_BYTES`, `MAX_SCHEDULER_EVIDENCE_FILES`, `SCHEDULER_REVIEW_EXECUTION_MODES`, `SCHEDULER_REVIEW_PASSED_STATUSES`, `SCHEDULER_REVIEW_BLOCKED_STATUSES`, `SCHEDULER_REQUIRED_COUNT_FIELDS`, `SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS`, `SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES`, `SCHEDULER_LIVE_WORK_STATUSES`, `_scheduler_evidence_items`, `_read_scheduler_evidence_item`, `_scheduler_evidence_blocked`, `_scheduler_bindings`, `_find_scheduler_evidence_files`, `_safe_scheduler_evidence_file`, `_scheduler_evidence_errors`, `_scheduler_readiness_status`, `_scheduler_evidence_mode`, `_scheduler_evidence_artifact_ref`, and `_scheduler_item_suffix` until inventory-backed caller migration; `validate_readiness(config)` still composes deterministic items, dependency summaries, scheduler evidence, live proof items, exclusions, `_validate_items`, `_release_blockers`, `_final_ready`, summary, and CLI behavior in the existing validator boundary; optional live scheduler proof binding remains in the validator boundary until 4.8 but may consume scheduler evidence bindings.
  - Boundary Surfaces: read surfaces `NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_ROOT`, `NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE`, `--scheduler-evidence-root`, and `--scheduler-evidence-file`; root/file mutual exclusion; root discovery only top-level `*.json` files with `MAX_SCHEDULER_EVIDENCE_FILES`; filesystem boundary regular files only, no symlink components, bounded 256 KiB reads, and redacted unsafe/missing/unreadable paths; evidence contract fields schema/schema_version, pass id, status, execution/proof mode, counts, stale/final-readiness flags, dry-run no-mutation proof, candidate/model-run identity fields, artifact ref, and sha256 checksum; downstream consumers optional live scheduler proof binding inputs, release blockers, final summary counts, and public readiness artifacts; unchanged sibling surfaces dependency summaries, live proof loader, proof-specific validators, dependency binders, exclusions, and final aggregation.
  - Regression Rows: owner direct tests and facade parity tests prove contract/helper object identity and equivalent outputs; root/file mutual exclusion raises the existing validation error; missing scheduler root/file produces no scheduler evidence item unless scheduler proof config requires the live scheduler item; valid dry-run review evidence creates `passed`/`deterministic` scheduler items with `required_for_final=false`, `live_proof_accepted=false`, pass id, execution mode, artifact ref, checksum, and bounded redacted payload; malformed JSON, invalid UTF-8, non-object JSON, oversized file, symlink leaf/ancestor, directory/non-regular file, missing file/root, too many root files, wrong schema/pass id/status/mode/counts, stale/final-readiness claim, missing dry-run no-mutation proof, unsafe identity values, identity derivation mismatches, and count/cardinality drift produce stable blocked/not-executed items with error codes or acceptance errors and no raw secret/private path leakage; `_scheduler_bindings` includes only passed deterministic scheduler evidence and preserves pass id, artifact ref, checksum, status, and execution mode for later live scheduler proof binders; deterministic scheduler evidence alone never satisfies `_final_ready` or final `summary.status=ready`.
  - Verification Floor: focused pytest above; `uv run pytest -q tests/test_production_readiness_validation.py -k "scheduler and evidence"`; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_scheduler_evidence.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
- [ ] 4.6 Proof-specific live validators extraction.
  - Module/Scope: auth, alert, rollback, and target-environment proof validators under the shared live-proof loader.
  - Dependencies: 4.1 and 4.3.
  - Out of Scope: dependency proof binding, scheduler proof binding, executing live side effects.
  - Focused Verification: `uv run pytest -q tests/test_production_readiness_validation.py -k "auth or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "alert or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "rollback or live_receipt"`;
    `uv run pytest -q tests/test_production_readiness_validation.py -k "target_env or final_readiness"`.
  - Inventory/Evidence Update: update readiness inventory rows `Live backend auth proof`, `Live alert sink proof`, `Live rollback proof`, and `Target-environment config proof`.
  - Fixture Size: high (required final live-proof items, redaction-sensitive receipts, shared item contract, and final-readiness adjacency).
  - Risk Packs Considered (standard):
    API/contract compatibility: selected - `validate_readiness(config)` remains the public entrypoint and `services.production_closure.readiness_validation` keeps compatibility aliases/wrappers for proof-specific contracts and validators until caller migration is proven;
    CLI / env / config roots: selected - `ProductionReadinessConfig.from_env`, `NHMS_PRODUCTION_READINESS_{AUTH,ALERT,ROLLBACK,TARGET_ENV}_PROOF`, `NHMS_PRODUCTION_READINESS_{AUTH,ALERT,ROLLBACK,TARGET_ENV}_PROOF_FILE`, and `--{auth,alert,rollback,target-env}-proof{,-file}` must keep inline/file loading semantics from the shared loader;
    Serialization / schema / field names: selected - public readiness item ids, surfaces, details, acceptance error strings, receipt detail shape, `live_proof_receipts.json`, and final summary counts are contract surfaces;
    Filesystem / path safety: selected narrowly - proof file path safety remains in `readiness_shared_artifacts`; proof-specific validators must not bypass loader-bounded receipts or introduce direct file reads;
    Security / privacy / redaction: selected - raw live proof payloads can contain secrets, provider metadata, local paths, webhook URLs, and command details; public item details and stdout must remain bounded/redacted;
    Resource bounds / traversal limits: selected narrowly - receipt depth/node/byte bounds stay in the shared loader; proof-specific validators must avoid unbounded recursive scans or large public payload copies;
    Error handling / rollback / partial outputs: selected - malformed, missing, stale, incomplete, or out-of-contract receipts become stable `release_blocked` items without executing auth checks, alert delivery, rollback commands, or target-environment mutation;
    Release / packaging / dependency compatibility: not selected - no package/distribution entrypoints change beyond in-repo owner imports and retained facade;
    Documentation / migration notes: selected - readiness inventory rows must record owner, retained facade, focused verification, and removal condition for the four proof-specific validators.
  - Risk Packs Considered (NHMS domain):
    Geospatial / CRS / basin geometry: not selected - proof-specific validators do not transform geospatial data;
    Hydro-met time series / forcing windows: not selected - these validators do not read forcing/source payloads;
    SHUD numerical runtime / conservation / NaN: not selected - no model runtime output is executed or inspected;
    PostGIS / TimescaleDB domain behavior: not selected - no DB reads/writes or schema behavior change;
    Slurm production lifecycle / mock-vs-real parity: not selected - scheduler/Slurm live proof binders remain out of scope;
    External hydro-met providers / snapshot reproducibility: not selected - source dependency proof binding remains out of scope;
    Run manifest / QC provenance: selected narrowly - proof-specific live receipts must remain bound to current readiness `run_id`, target environment, artifact/evidence refs, live mode, and accepted status;
    Published NHMS artifacts / display identity: selected narrowly - target-environment proof metadata contributes to final live readiness but does not claim display/MVT performance proof.
  - Invariant Matrix: owner module `services.production_closure.readiness_live_proofs`; stable facade `services.production_closure.readiness_validation`; retained facade imports/re-exports include `PROOF_CONTRACTS`, `REQUIRED_AUTH_ACTIONS`, `EXPECTED_TARGET_ENVIRONMENT`, `_auth_live_item`, `_surface_live_item`, `_surface_live_receipt_errors`, `_common_live_receipt_errors`, `_provider_metadata_is_meaningful`, `_role_mapping_is_meaningful`, `_alert_sink_metadata_is_meaningful`, `_alert_delivery_metadata_is_meaningful`, `_rollback_command_metadata_is_meaningful`, `_rollback_result_is_meaningful`, and `_target_env_config_metadata_is_meaningful` until inventory-backed caller migration; `_live_proof_items` remains a `readiness_validation` aggregator wrapper that only delegates auth, alert, rollback, and target-env proof-specific builders to `readiness_live_proofs` in 4.6; `validate_readiness(config)` still composes deterministic items, dependency summaries, scheduler evidence, proof-specific live proof items, dependency/scheduler live proof items, exclusions, `_validate_items`, `_release_blockers`, `_final_ready`, summary, and CLI behavior in the existing validator boundary; dependency proof binding `_dependency_receipt_errors` and scheduler proof binding `_scheduler_receipt_errors` remain in the validator boundary until 4.7/4.8 but may share the retained common live receipt contract.
  - Boundary Surfaces: receipt load surfaces `auth`, `alert`, `rollback`, and `target_env`; proof file/inline ambiguity and bounded safe proof reads stay owned by `readiness_shared_artifacts`; proof-specific contracts include schema `nhms.production_readiness.live_proof.v1`, proof type, surface, current `run_id`, target environment `production`, live execution mode, meaningful artifact/evidence refs, accepted flag, allowed statuses, and proof-specific metadata; downstream consumers final required-live-proof item counts, release blockers, summary status, public readiness artifacts, and redacted stdout; unchanged sibling surfaces dependency proof binding, scheduler proof binding, deterministic summaries/evidence, exclusions, and final aggregation.
  - Regression Rows: owner direct tests and facade parity tests prove contract/helper object identity and equivalent outputs for auth, alert, rollback, and target-env receipts; missing receipts create `release_blocked` required-final items with `not_executed`; parse-failed/too-large receipts create `release_blocked` `live_proof` items from shared loader details; valid auth proof requires provider metadata, role mapping, and complete allowed/denied coverage for every `REQUIRED_AUTH_ACTIONS` member; valid alert proof requires meaningful sink metadata, delivery metadata, and delivery confirmation; valid rollback proof requires meaningful preconditions, command/drill metadata, and executed/success result; valid target-env proof requires meaningful config/environment metadata plus identifier; schema/proof type/surface/run id/target environment/status/live mode/artifact ref mismatches produce stable acceptance errors; public receipt/item/summary/stdout artifacts do not leak raw secrets, private paths, raw payload, or unbounded nested data; accepting only deterministic or proof-specific receipts without dependency/scheduler binders preserves final readiness semantics and existing release blockers.
  - Verification Floor: focused pytest commands above; `uv run pytest -q tests/test_production_readiness_validation.py -k "live_proof_receipts_artifact or live_proof_json_traversal"`; `uv run pytest -q tests/test_production_readiness_validation.py`; `uv run ruff check services/production_closure/readiness_validation.py services/production_closure/readiness_live_proofs.py tests/test_production_readiness_validation.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `git diff --check`.
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
