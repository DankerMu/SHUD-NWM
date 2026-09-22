## Context

Every line number in the three issues is stale against current master. Locate code by symbol. The anchors are the `build_candidates` terminal-skip dispatch in `services/orchestrator/scheduler_candidates.py` and these helpers: `_terminal_run_manifest_retry_evidence`, `_journal_predecessor_identity_quarantine`, `_journal_predecessor_identity_retry_evidence`, `_upgrade_retry_for_strict_warm_start_manifest`, `_strict_warm_start_forcing_witness_decision`, and `_apply_explicit_missing_forcing_repair_policy`.

Change surface: `build_candidates`, the `terminal_run_manifest_missing` leg and the comment above the post-upgrade witness consultation, plus `_apply_explicit_missing_forcing_repair_policy`, both in `scheduler_candidates.py`.

Governing invariants:
- #1843: every decision that restarts at `forecast` consults the candidate's own per-model forcing witness before it can be emitted, on every lane.
- §8.7: every real re-run of a quarantined stale lineage either moves the breaker count at the forecast-cohort reservation or is refused before it submits. There is no third kind. This extends r2-01 from confirmed to unconfirmed candidates.

## Decisions

### D1 (#2396): consult the witness at the `terminal_run_manifest_missing` leg

Right after the leg builds its `CandidateStateDecision("retry", "terminal_run_manifest_missing", ...)`, call `_strict_warm_start_forcing_witness_decision(candidate, raw_candidate_state, state_decision)` with the RAW state. The blocker is returned verbatim, not re-merged. The call is unconditional inside the leg, with a comment explaining that the leg is reachable only when `strict_warm_start is None`: the first strict branch consumes every not-None × `_STRICT_WARM_START_TERMINAL_SKIP_REASONS` shape. An `if strict_warm_start is None:` wrapper would be dead-conditional here. It is not needed as a lane scope because no later consultation runs on this lane. Rewrite the post-upgrade comment so it names the None-lane emitting points that consult the witness themselves (this leg and the #1844 quarantine leg) instead of claiming that `scheduler_state_decision` covered everything.

### D2 (#2408, owner decision (b)): refuse the repair reclassification for quarantine-descended blockers

In `_apply_explicit_missing_forcing_repair_policy`, immediately after the r2-01 `operator_reentry_confirmation` refusal and before the other preconditions, `return rejected("journal_predecessor_quarantine_present", ...)` when the blocker descends from a §8.7 quarantine retry. Two predicates each mark that descent, and either one is sufficient:
- `decision.evidence["journal_predecessor_identity"]` is a Mapping, which survives the blocker via `**base_evidence`;
- `decision.evidence["artifact_guard"]["planned_retry_decision"] == "retry_journal_predecessor_identity_mismatch"`.

Use the existing constant `JOURNAL_PREDECESSOR_QUARANTINE_RETRY_DECISION` if it can be imported without a cycle; otherwise use a local constant with a comment pointing to it. Include the recorded and expected init-state ids in the rejection details for triage. Blockers that do not descend from quarantine keep today's behaviour byte-for-byte. The rejection keeps the candidate `blocked` on the stable missing-forcing contract, so the operator drains it by restoring the model's own forcing (`scripts/node22_backfill_forcing_for_model_ids.py`). The quarantine retry then restarts at `forecast`, the witness passes, and the reservation stamps the retry.

Rejected (a), a third provenance trigger in `canonical_quarantine_rerun_model_ids`: the reclassified retry restarts at `forcing`, and `accepted_submit_row_kind` returns `None` for non-forecast stages. A failing forcing stage would still submit (`retry_repair_missing_forcing` is whitelisted) and would never stamp.

### D3 (#2407): pin the strict-lane literal survival

This is test only. The fixture must make the early-return deletion observable. The next check after the early return, `_terminal_decision_run_manifest_matches_strict_warm_start`, must be **False**, so the terminal row's run manifest has to be ABSENT or MISMATCHED against the strict evidence. The issue's hint to "seed the run manifest" would keep the test green with the early return deleted. `terminal_completed_cycle` is not in `_STRICT_WARM_START_TERMINAL_SKIP_REASONS`, so the successor and manifest legs never route it, and the issue's hints (b)/(c) do not apply. Required fixture: db-free required, `NHMS_REQUIRE_FORECAST_WARM_START` not false, strict evidence `ready` (so warm admission passes), the model's own forcing witnessed, the stale journal token, and the manifest absent or mismatched.

## Must preserve

- Quarantine retries on the None lane (the #1844 consultation) and the confirmed-reentry refusal (r2-01, `operator_reentry_confirmation_present`) are unchanged, and r2-01 is still evaluated first.
- The repair policy still reclassifies non-quarantine missing-forcing blockers when the exact-cycle, direct-grid, warm-state and raw-manifest preconditions hold.
- `retry_terminal_run_manifest_missing` with its own forcing present still submits, stays in both forced-resubmit whitelists (`chain_forced_resubmit.py`, `chain_runtime_utils.py`; pinned by `tests/test_warm_start_chaining.py`), and keeps `tests/test_forced_resubmit_veto.py` green.
- `canonical_quarantine_rerun_model_ids` and `canonical_budget_reentry_model_ids` are unchanged.
- The strict budget leg `retry_strict_warm_start_terminal_init_state_mismatch` still survives through the same early return (existing budget-reentry tests).

## Sibling surfaces

- The four existing witness call sites in `build_candidates` (strict run-manifest leg, strict terminal-mismatch leg, #1844 quarantine leg, post-upgrade consultation). Reviewers confirm that no other `restart_stage: "forecast"` emitter in the dispatch is left unconsulted, and that `strict_warm_start_successor_checkpoint_missing` restarts at `state_save_qc` (out of scope).
- The sink guard `_refuse_confirmed_candidates_off_forecast` (confirmed side only, untouched).
- `scheduler_state_decision` emitting points (unchanged).
- The #1846 drain contract `_decision_is_stable_missing_forcing_blocker`, which new blockers must satisfy.

## Seams under test

`build_candidates` / `ProductionScheduler._build_candidates` is driven end to end. The None variant (#2396) is derived from `_run_wiring_a_build_candidates` (`tests/test_scheduler_generation.py`). The strict variant (#2407/#2408) needs a `terminal_completed_cycle` state and an accessor-visible stale token, as described in tasks 2.0; it does not use that skeleton's `hydro_status="complete"`. `canonical_quarantine_rerun_model_ids` is not a seam here.

## Non-goals

- Automatic forcing-stage re-entry (#1846).
- The breaker threshold and the confirmed-reentry semantics (#1555).
- Counting failed reruns (#2401, archived).
- Configuration of the manifest read root.
- The `strict_warm_start_successor_checkpoint_missing` leg.

## Review focus

1. D2 refuses only quarantine-descended blockers. Both predicates are correct, and a non-quarantine blocker carrying stale keys is not over-refused.
2. D1 does not alter None-lane retries other than `terminal_run_manifest_missing`.
3. The #2407 test really goes red with the early return removed (the implementer reports the red run).
4. Each test asserts that its lane really is the target lane: strict evidence is non-None for #2407/#2408, None for #2396.
