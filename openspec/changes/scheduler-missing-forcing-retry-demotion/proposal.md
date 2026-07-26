# Missing forcing artifact must demote forecast retry, not spin (#1160)

## Why

Live incident (node-22, 2026-07-26): `gfs_2026072000` / `ifs_2026072000`
stuck — forcing objects genuinely absent from both object stores (stores
end at `2026071912`), yet every 5-min scheduler pass resubmits the
forecast cohort; journal shows `..._forecast_retry_65` (gfs) /
`_retry_99` (IFS), sacct shows `nhms_forecast` arrays failing in ~1s,
recorded `error_code: "NODE_FAILURE"`. The system violates its own spec
(`openspec/changes/fix-node22-scheduler-business-concurrency/specs/`
`job-retry-mechanism/spec.md:28-35`): missing `forcing_package_uri` MUST
block or restart upstream, with a stable artifact classifier — never
generic `NODE_FAILURE`. #874 fixed the success-history recovery paths
only; this is the same defect in failure-state geometry.

Four stacked causes (issue #1160 §2, all re-verified at HEAD 3d6d3b92):

- **L1** classification lost: SHUD runtime raises `ARTIFACT_NOT_FOUND`
  (`workers/shud_runtime/runtime.py:1144,:1211-1214`) and even writes
  `logs/runtime_error.log` (`:1554-1560`) + mirrors logs to the object
  store (`:820-823`), but DB-free `mark_failed` is a no-op
  (`:155-168`) and nobody reads the file — array accounting stamps
  `NODE_FAILURE` (`services/orchestrator/chain_array_accounting.py:598`
  no-honor path; `:655-658` honor-if-present path), which is in
  `TRANSIENT_ERROR_CODES` (`services/orchestrator/retry.py:26`).
- **L2** guard misplaced: `_missing_upstream_forecast_artifact_evidence`
  hangs only off three planned-retry branches
  (`services/orchestrator/scheduler_state_decision.py:210-221,
  :249-260,:269-280`); the branch failure states actually hit is the
  `retry_failed_candidate` fallback (`:320-327`), guard-free, with
  `restart_stage="forecast"` pinned by `_retry_failure_evidence`
  (`scheduler_state_failure.py:840-852`). Once any failure signal
  exists, `_completed_upstream_stage_retry_evidence` self-disables
  (`:645-646`) so the fail-closed default is unreachable.
- **L3** in-stage loop: `chain_forecast_execution.py:148` (`while
  True`) + `:195-200` reschedules unconditionally.
- **L4** cap structurally dead: gate `should_retry(job)`
  (`chain_forecast_orchestrator_cycle.py:192-194`) reads
  `job.retry_count`, which the journal clean-reservation invariant
  (`file_orchestration_journal.py:1471-1485`) forces to 0 on master
  rows; the true attempt lives only in the `_retry_<n>` job-id suffix,
  parsed AFTER the gate (`:198-211`) and only for naming.

## What Changes

All three layers of issue #1160's recommended option, plus the two
cross-layer couplings the design scout proved (they are why the layers
must land together, and why a naive L1-only or L2-only fix fails):

- **L1 classifier channel (DB-free, file-based)**: the runtime's
  existing failure hook (`runtime.py:402`, `_write_failure_log`) also
  emits a machine-parsable `logs/task_outcome.json`
  (`schema, run_id, error_code, error_message, failed_at`) under the
  same containment discipline; array accounting resolves per-task codes
  through ONE new resolver (object-store read keyed on the re-indexed
  cohort member's `run_id`, NOT `context.run_id`), falling back to
  `NODE_FAILURE` only when the receipt is absent/unreadable (fail-safe
  degradation, same shape as
  `services/production_closure/slurm_validation.py:1235-1257`). Both
  stamp sites (`:598` and `:655-658`) route through it;
  gateway-supplied `error_code` keeps priority on the `:655` path.
  `ARTIFACT_NOT_FOUND` is already non-transient (absent from
  `TRANSIENT_ERROR_CODES`) — no retry-table change needed.
- **L2 guard consulted at the three emitting return points** (never
  an unconditional pre-pass): one lazily-computed guard result
  (`_retry_failure_evidence(...)` fed as `planned_retry` into
  `_missing_upstream_forecast_artifact_evidence` — already
  None-safe/`.get`-safe, `scheduler_state_failure.py:344-345,
  :601-609`) consulted where a forecast-bound decision is about to be
  emitted: the model-package refresh retry
  (`scheduler_state_decision.py:300` — post-L1 the spin's other
  door), the permanent-failure block (`:304` — without this, L1
  reroutes to `permanent_failure_guard`, which the repair policy
  rejects, `scheduler_candidates.py:1508-1511`, and
  `--repair-missing-forcing` stays a NO-OP), and the
  `retry_failed_candidate` fallback (`:320`). Healthy/running
  candidates reach none of these points; `_cancelled_state_evidence`
  (`:312-318`) keeps its exact position and priority. URI-absent
  ruling (recorded): the guard's existing no-recorded-URI blocker
  semantics (`scheduler_state_failure.py:362-372`,
  `artifact_uri=None`) apply unchanged at the new points — the live
  incident journal records `forcing_version: null`, so URI-absent IS
  the incident geometry; any forecast-stage failure without recorded
  forcing provenance now blocks repair-eligible instead of retrying
  (fail-closed by design, test-pinned). Wiring the full guard also
  activates its copyback half (`:387-423`) for failed candidates;
  same fail-closed direction, pinned by a test, not scoped away.
- **L3+L4 effective attempt cap, both sides**: one shared
  suffix-aware helper (consolidating the two existing copies:
  `file_orchestration_journal.py:7070-7083` and
  `chain_forecast_orchestrator_cycle.py:198-209`; `rsplit`
  last-suffix semantics — stacked `_retry_1_retry_2` ids exist in
  production journals) feeds (a) `job.retry_count` at the DB-free job
  construction site (`chain_forecast_execution.py:389`, read side
  only — never persisted onto a reservation) and (b) the scheduler
  read side `_state_retry_attempt`
  (`scheduler_state_rows.py:415-423`), which is equally suffix-blind
  today (capping only the in-stage loop would convert tight spin into
  one-spin-per-pass). Backoff now sees the true attempt
  (`chain_forecast_orchestrator_cycle.py:184`) — intended behavior
  change (clamps to last bucket).

## Impact

- Affected specs: `job-retry-mechanism` — ADDED requirement (new
  title; the governing scenario lives in the still-active change
  `fix-node22-scheduler-business-concurrency` and is refined, not
  contradicted).
- Affected code: `workers/shud_runtime/runtime.py` (outcome receipt
  writer), `services/orchestrator/chain_array_accounting.py` (resolver
  + two stamp sites), `services/orchestrator/scheduler_state_decision.py`
  (guard wiring), `services/orchestrator/chain_forecast_execution.py`
  (`:389` attempt population), `services/orchestrator/
  chain_forecast_orchestrator_cycle.py` (use shared helper),
  `services/orchestrator/file_orchestration_journal.py` (delegate to
  shared helper), `services/orchestrator/scheduler_state_rows.py`
  (`_state_retry_attempt` suffix-aware).
- Must preserve: journal clean-reservation invariant untouched (read
  side only); `blocked_missing_upstream_artifact` evidence shape and
  `_decision_is_stable_missing_forcing_blocker` structural contract
  (`scheduler_candidates.py:1428-1445`) — the repair channel's
  acceptance test; three existing guard call sites byte-identical;
  gateway-supplied error codes keep priority; `retry_failed_candidate`
  remains the decision for failed candidates whose recorded upstream
  artifacts all exist; healthy/running candidates (no failure signal,
  no permanent classification) get decision `None` exactly as today;
  cancelled candidates keep `manual_retry_required_after_cancelled`;
  manual-retry `default_attempt` and evidence-owner attempt semantics
  unchanged for states without stage-matching retry suffixes.
- Known breakage (intended, anchors — true per-anchor causes):
  `tests/test_orchestration_chain.py:1355,:1358,:1365,:4631` pin the
  NODE_FAILURE hardcodes (break via the L1 resolver);
  `tests/test_production_scheduler.py:5056` /
  `tests/test_gateway_reconcile.py:1135` pin `retry_failed_candidate`
  for URI-ABSENT failure states (break via the URI-absent ruling —
  their fixtures record no forcing URI at all). The full breakage
  inventory is enumerated in the fix commit; each adjustment is
  recorded, not silent.
- Non-goals: online recovery of `gfs/ifs_2026072000` (ops runbook,
  issue §7 — separate from this code change); why upstream forcing
  was never produced; #1118's generic no-progress circuit breaker;
  exit-code-based classification (rejected: collapses ~60 codes,
  three-way table maintenance, maskable by wrapper layers).

## Known risks / disclosed residuals

- The copyback half of the guard newly fires for failed candidates
  with missing copyback sources — same fail-closed direction,
  test-pinned.
- `upload_logs` is best-effort (`runtime.py:404-407` swallows
  failures): if the mirror to the object store failed, the resolver
  degrades to `NODE_FAILURE` (fail-safe, never fail-open); the NFS
  workspace copy is not read in v1 (accounting keeps a single trust
  root — the object store).
- Blocked-reason string changes for transient codes at the limit
  (`retry_limit_exhausted` instead of endless retry) and
  `_downstream_failure_restartable` returning False at the limit are
  the intended payoff, covered by tests.
