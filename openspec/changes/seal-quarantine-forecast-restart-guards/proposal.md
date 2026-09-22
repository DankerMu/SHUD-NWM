## Why

Two §8.7 / #1843 invariants each have a hole in `services/orchestrator/scheduler_candidates.py`. A third hole is not a defect but a missing test for a coupling. All three sit in the same terminal-skip dispatch of `build_candidates` and share one test skeleton, so they ship in one PR at the user's request.

- #2396 — the None-lane `terminal_run_manifest_missing` leg (the `elif` just before the §8.7 quarantine `else`) emits a `restart_stage: "forecast"` forced resubmit (`_terminal_run_manifest_retry_evidence`) and never consults the per-model forcing witness. `_upgrade_retry_for_strict_warm_start_manifest` is a no-op on that lane, and the later consultation is scoped to `strict_warm_start is not None`. This leg is reachable only when `strict_warm_start is None`: the first strict branch consumes every not-None × `_STRICT_WARM_START_TERMINAL_SKIP_REASONS` shape. That breaks the #1843 invariant "every forecast restart consults the model's own forcing witness". The comment above the post-upgrade consultation ("every other retry reaching this line was already consulted by `scheduler_state_decision`") is false for this leg.
- #2408 — on the strict lane, an UNCONFIRMED §8.7 quarantine retry for `terminal_completed_cycle` survives the upgrade helper. Its forcing witness then fails, so the retry becomes a stable missing-forcing blocker. `_apply_explicit_missing_forcing_repair_policy` then rewrites that blocker into `retry_repair_missing_forcing` with `restart_stage: "forcing"`. Provenance is stamped only at the forecast-cohort reservation, and `canonical_quarantine_rerun_model_ids` keys on the quarantine literal or a confirmation block. That rerun is therefore a real re-run of the stale lineage that either moves no breaker count (forcing fails, yet it still submits because the literal is whitelisted) or loses its quarantine literal. r2-01 already refuses this reclassification for CONFIRMED candidates. Nothing covers the unconfirmed side.
- #2407 — whether the quarantine literal survives the strict lane depends on the `native_shud_resubmitted is True` + `restart_stage == "forecast"` early return in `_upgrade_retry_for_strict_warm_start_manifest`. No test drives strict lane × quarantine: all 39 traced calls had `strict_evidence is None`.

## What Changes

- #2396: the `terminal_run_manifest_missing` leg consults `_strict_warm_start_forcing_witness_decision(candidate, raw_candidate_state, state_decision)` right after building the retry. When the witness is absent, the candidate lands in the stable missing-forcing `blocked` state. When it is present, the retry is kept and annotated with `forcing_provenance`. The misleading comment is rewritten to list the None-lane emitting points that consult the witness themselves.
- #2408, **owner decision (b), refuse the reclassification**, recorded 2026-09-22 in this session: `_apply_explicit_missing_forcing_repair_policy` rejects a missing-forcing blocker that descends from a §8.7 quarantine retry. The descent is keyed on the surviving `journal_predecessor_identity` block or on `artifact_guard.planned_retry_decision == "retry_journal_predecessor_identity_mismatch"`. The rejection sits right after the r2-01 confirmation refusal and has the same shape: nothing submits, and the stable blocker is drained by restoring the model's own forcing (rename backfill when the rename set is non-empty, otherwise out of band, as for r2-01). Option (a), a third provenance trigger, was rejected: the reclassified retry restarts at `forcing`, so a failing forcing stage would still submit and never reach the stamp site.
- #2407: a test-only pin that drives strict lane × `terminal_completed_cycle` quarantine through the real `build_candidates` and asserts that the literal survives. No runtime change.

## Capabilities

### Modified Capabilities

- `production-scheduler-orchestration`: the quarantine forcing-witness requirement gains the strict-lane literal-survival and repair-refusal rules. A new requirement covers the None-lane `terminal_run_manifest_missing` witness consultation.

## Impact

- Code: `services/orchestrator/scheduler_candidates.py` only (guard-exempt). There are no changes to `accepted_submit_identity.py`, to the forced-resubmit whitelists (`chain_forced_resubmit.py`, `chain_runtime_utils.py`), or to the breaker threshold.
- Tests: one new test file, `tests/test_quarantine_forecast_restart_guards.py` (≤1000 lines), routed by `scripts/select_ci_tests.py`.
- Runtime: both lanes. #2396 affects the DB plane and the db-free D8.9 compat lane (None lane). #2408 affects the db-free strict lane, and only while an operator has opened the single-cycle `repair_missing_forcing` window.

## Triage

```text
Issue type: bugfix x2 + test x1 (one PR at user request)
Fixture level: expanded
Upstream suggested level: absent (issue-scribe issues)
Blast radius: production scheduler forecast-restart decisions — a wrong guard submits a doomed forecast (ARTIFACT_NOT_FOUND) or re-runs a stale lineage without arming the §8.7 breaker; an over-broad guard blocks a healthy retry or repair.
Selected risk packs: concurrency/shared state/ordering; legacy compatibility; error handling/partial outputs
Evidence floor: red-then-green build_candidates regressions per issue (incl. #2407 red with the early return removed); uv run ruff check .; node-27 targeted pytest on the four scheduler suites + new file
```
