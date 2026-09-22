## Risk Packs

- [x] Concurrency / shared state / ordering — **selected**: every fix orders two persisted journal truths by timestamp. Covered by 1.x/2.x/3.x recency and tie tests.
- [x] Legacy compatibility — **selected**: legacy `hydro_run`-only identity, the old-failure/new-success terminal shape, and the #2254/#1201/#1555 regressions must hold. Covered by 1.4, 2.4, 3.4, 3.5.
- [x] Error handling / partial outputs — **selected**: a failed rerun must reach the budgeted failure path, not a forced resubmit. Covered by 1.2, 1.3.
- [x] Resource limits — **selected**: unbounded Slurm resubmission. Covered by the submission-count bound in 1.2 and 3.2.
- [x] Public API / CLI / script entry — not selected: no CLI, route, or payload schema change.
- [x] Config / project setup — not selected: no new env/config. The large-file guard is honoured without new excludes (0.2).
- [x] File IO / path safety — not selected: journal row reads only, no new paths.
- [x] Schema / columns / field names — not selected: the D3 mint floor rides only on the in-memory retry decision evidence (`state_evidence`), never as a new run-manifest top-level key. `schemas/run_manifest.schema.json` has root `additionalProperties: false`, and 3.2 asserts the manifest gains no top-level key (the pre-existing schema drift is #2539).
- [x] Auth / permissions — not selected.
- [x] Release / packaging — not selected.
- [x] Documentation / migration notes — not selected: no runbook change; the behaviour is recorded in spec deltas.

## 0. Setup

- [x] 0.1 Branch `feat/issue-2401-2404-2397-bound-scheduler-rerun-truth` from `origin/master`. Three serial implementer passes in the order #2401 → #2397 → #2404, one commit per issue.
- [x] 0.2 Honour the large-file guard: every committed non-exempt file stays ≤1000 lines. New tests go in new test files (each ≤1000 lines, routed by `scripts/select_ci_tests.py` — add a `PathTestRule` if name derivation does not reach them) or in already-exempt suites. Touch `scheduler_state_identity_filter.py` / `chain_forecast_orchestrator_cycle.py` / `scheduler_discovery.py` only after extracting a cohesive helper module that brings the file to ≤1000 lines. No new excludes.

## 1. #2401 — newest truth wins for completed-type terminal skips

- [x] 1.1 Red first: a real-journal reproduction (`_seed_budget_journal`-style fixture or `orchestrate_cycle` + `FakeCycleSlurmClient(fail_stage="forecast", array_results_by_stage={"forecast": ["failed"]})`). Seed a stale-token completed cycle with no operator confirmation and no completed stamped master. Pass 1 emits `retry_journal_predecessor_identity_mismatch`, the rerun fails, and more than `retry_limit` further passes keep submitting. Record the red output (submission count) in the implementer report. If the loop turns out bounded by something the static read missed, stop and report.
- [x] 1.2 Fix per design D1. Green: the same test asserts total submissions ≤ the retry budget and a final blocked/budget-exhausted decision (not still `submitted`).
- [x] 1.3 After the failed rerun, the next decision is `retry_failed_candidate` (not a `terminal_pipeline_success` / `terminal_completed_cycle` skip rewritten into a quarantine retry). Sibling tests show that `terminal_run_manifest_missing` and the strict warm-start mismatch leg (`scheduler_candidates.py:2771-2842`) both stop re-firing when a failure is newer than the success.
- [x] 1.4 Must-preserve tests: old failure + newer success stays terminal on the pipeline and completed-cycle legs; equal timestamps stay terminal; a success without a timestamp keeps today's decision; `terminal_hydro_success` is unchanged; `test_breaker_reentry_confirmation_is_consumed_when_the_rerun_is_accepted_even_if_it_fails` (`tests/test_production_scheduler.py`) stays green (its direct `_build_candidates` assertion intentionally updated to `retry_failed`; the real-pass invariants are kept and strengthened).

## 2. #2397 — newest truth wins for §8.7 identity authority

- [x] 2.1 Red first: through the real `create_hydro_run_from_basin` and pipeline terminal writes, complete run 1 recording X, then a same-run_id rerun with quarantine provenance recording Y. `completed_pipeline_init_state_identity` returns X (red).
- [x] 2.2 Fix per design D2. Green: it returns Y, and the report names the `hydro_run` comparison key and shows it survives `update_hydro_run_status`.
- [x] 2.3 After a rerun recording the correct lineage, `_journal_predecessor_identity_quarantine` returns `None` and the breaker does not engage. When the rerun records the stale X again, the breaker engages exactly as before.
- [x] 2.4 Legacy shape (no accepted-submit master, identity only on `hydro_run`) returns the same identity as before. The discovery-side §8.7 scoring reads the new authority, with one assertion through `scheduler_discovery`.

## 3. #2404 — retry mint shares the budget read

- [x] 3.1 Red first: single model. After attempt M is spent under the `..._full_<model>` prefix, a strict warm-start retry minted via the public `orchestrate_cycle` under `..._forecast_<model>` gets a suffix ≤ M (red). Record the output.
- [x] 3.2 Fix per design D3. Green: the minted suffix is > M, and the budget blocks (`blocked_strict_warm_start_init_state_mismatch`) on the pass where the cumulative attempt reaches `retry_limit`, with no extra submission. The floor travels in retry decision evidence only; the emitted run manifest gains no top-level key and carries no floor. It does not validate today because of pre-existing drift, tracked in #2539.
- [x] 3.3 Cohort geometry (D4): run evidence for `..._forecast_cohort_<digest>`, including a digest change. Either a fix plus regression test, or a test that proves the budget advances. Audit the auto-retry minter at `file_orchestration_journal.py:11289` and record the verdict.
- [x] 3.4 Model isolation: #1845 is still OPEN, so no existing regression covers it. Add an assertion that a sibling model's `_retry_<n>` rows (same source/cycle, including cohort geometry) do not raise this model's floor.
- [x] 3.5 Must-preserve: the #2254 block (`tests/test_orchestration_chain.py` "#2254" section), the #1201 occupied-row tests (`tests/test_production_scheduler.py`), and the existing strict warm-start/budget tests stay green. The floor never flows through `context.retry_attempt` (asserted, or evident in the diff).

## 4. Verification

- [x] 4.1 `uv run ruff check .`
- [ ] 4.2 Local: `uv run pytest -q tests/test_production_scheduler.py -k "strict_warm_start or budget or quarantine or breaker"`, plus the new test files, `tests/test_orchestration_chain.py -k "retry"`, and `tests/test_file_orchestration_journal.py -k "identity or hydro_run"`.
- [ ] 4.3 node-27 (oracle): `tests/test_production_scheduler.py tests/test_file_orchestration_journal.py tests/test_scheduler_generation.py tests/test_orchestration_chain.py tests/test_retry.py tests/test_scheduler_state*.py` + new test files all green at the PR head.
- [x] 4.4 `openspec validate bound-scheduler-rerun-truth-and-budget --strict --no-interactive`

## 5. Review round 1 fixes

- [x] 5.1 The ordinary failure retry (`retry_failed_candidate` and its strict escalation) carries the forecast `retry_attempt_floor`. Prefix-switch and cohort-digest tests are red before the fix and green after.
- [x] 5.2 The #2397 master is chosen filter-first (terminal-success, self-bound, names the model), then newest. A later failed rerun does not undo a converged Y.
- [x] 5.3 `tests/test_retry_mint_floor.py` is routed from the `file_orchestration_journal.py` stop rule.
- [x] 5.4 Coverage: the mixed-outcome cohort splits into terminal skip and the failure path; a failing rerun without inline retry stays within the forecast budget.
- [x] 5.5 Mixed-floor cohort over-charge is kept as shared charging (fail-closed). Per-member charging is deferred to a follow-up issue.
