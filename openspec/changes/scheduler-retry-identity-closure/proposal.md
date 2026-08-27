## Why

Issues #1586, #1584, and #1577 expose three pre-existing scheduler gaps at the same candidate-state boundary: a non-authoritative in-window cohort row can spend a candidate's strict-warm-start retry budget, a source-cycle download blocker is tested against the candidate's top-level `run_id` and is therefore never restored, and a truncated failed row leaves manual-retry minting unable to recover the stage whose durable attempt floor remains available. Together they violate one rule: retry and blocker decisions must be bound to the authoritative row or marker lineage they claim to represent.

## What Changes

- Build a read-only, candidate-authoritative attempt view for the strict-warm-start budget so both carried floors and in-window row-scan attempts use the same authority predicate; the bare source-cycle wedge remains authoritative while suffixed execution-cohort rows do not spend candidate budget.
- Bind top-level source-cycle download blocker recognition to a concrete matching blocker row rather than the candidate state's top-level `run_id`, making the existing restore and shared-cycle blocker branches reachable on real projected state while retaining that branch's candidate-authority narrowing of carried attempt floors.
- Recover an otherwise-unnameable manual-retry stage only inside manual-retry evidence composition, from exact adopted-marker-to-attempt-floor-source lineage, then mint `N+1`; geometry-B projection visibility and both failed-stage resolvers remain unchanged, while ambiguous, foreign, stale, or unproven marker lineage keeps the existing fail-closed fallback.
- Add regression coverage for all three issue geometries and preserve the existing #1179 selection, floor, identity, nameable-mint, stage-less, and retry-limit behavior.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `job-retry-mechanism`: Candidate-scoped retry attempts, source-cycle blocker restoration, and geometry-B manual retry minting gain one authoritative identity/lineage contract.

## Impact

- Runtime: `services/orchestrator/scheduler_state_identity_filter.py`, `scheduler_candidates.py`, `scheduler_state_manual_retry.py`, and `scheduler_state_failure.py`; existing projection helpers in `scheduler_state_rows.py` are reused rather than forked.
- Tests: focused scheduler projection/state-machine regressions in `tests/test_production_scheduler.py`; no database, API, schema migration, Slurm submission template, or dependency change.
- Compatibility: public decision/evidence shapes and retry-limit semantics remain unchanged; only the three incorrect decisions named above change.

Fixture level: expanded (mandatory triggers: scheduler entrypoint, persisted/shared retry state transitions, legacy marker compatibility).
Repair intensity: high (shared identity helper and retry/blocker state-machine decisions).
Upstream suggested level: absent (all three are legacy review-routed issues).
Minimal mergeable slice: absent; the user explicitly requested one PR because all three close the same state-identity boundary.
