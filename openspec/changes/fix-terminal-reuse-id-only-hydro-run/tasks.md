# Tasks

## Risk triage

```text
Issue type: bugfix
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: high
Fixture level: high (expanded + Invariant Matrix)
Upstream suggested level: absent (hand-authored issue) — set to high: the change moves an admission gate in the silent direction (toward reuse) and modifies a spec requirement whose existing scenario mandates today's behavior.
Why:
- persisted/shared state transitions + retry budget accounting (#1173) — mandatory expanded trigger
- legacy row-shape compatibility (id-only hydro_run rows are the entire population, never rewritten)
- field/schema presence semantics (absent vs disagreeing) is the whole change
- a wrong reuse silently skips required forecast replay; no alarm exists downstream
Selected risk packs:
- Concurrency / shared state / ordering
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Schema / columns / units / field names
- Warm-start / state lineage (domain)
OpenSpec change: fix-terminal-reuse-id-only-hydro-run (generated)
Evidence floor:
- uv run pytest -q on the seam suites (see §3)
- uv run ruff check .
- openspec validate fix-terminal-reuse-id-only-hydro-run --strict --no-interactive
- node-22 live receipt: skipped_candidate_count == completed models on a revisited succeeded cycle
```

## 1. Predicate

- [x] Add the id-only upgrade to the candidate ladder's `hydro_run` leg per the decision table **and the "Required wiring" section** in `design.md`: `init_state_id` equal + remaining fields absent + `run_manifest_initial_state` four-field match ⇒ terminal reuse exit. The manifest proof SHALL be evaluated inside `_terminal_decision_matches_strict_warm_start`'s `hydro_run` leg so the leg returns `False` for every other id-only shape; relaxing the leg to return `True` for all matching-id rows is explicitly rejected — it reroutes both negative-pin cases onto the unbudgeted `strict_warm_start_terminal_run_manifest_missing` retry.
- [x] Leave the call site's `successor_state` readiness gate in force for the new reuse row.
- [x] Keep `_warm_state_record_matches` unchanged and keep every not-match outcome on the budgeted `_strict_warm_start_terminal_mismatch_decision` route; the unbudgeted `strict_warm_start_terminal_run_manifest_missing` retry SHALL NOT become reachable from any shape that could not reach it before.
- [x] Keep the `candidate_state` terminal-source branch and the `COLD_START_QUARANTINED` escape ahead of everything, unchanged.
- [x] Leave `scheduler_init_state_match.terminal_init_state_match` and the verdict path untouched.

## 2. Dead dedup gates verdict (issue acceptance item 5)

- [x] Audit every `active_repository` shape reachable in production and in the test suite for a missing `candidate_state` attribute; record the audit (paths + result) here.

  **Method.** Reason-string greps are insufficient (`scheduler_candidates.py:1053`/`:1097` emit the same `active_duplicate_pipeline` string from another path), so the audit was line-level: a temporary `PYTEST_CURRENT_TEST` marker inside each of the three gate bodies, run over nine orchestrator suites (`test_production_scheduler`, `test_scheduler_backfill`, `test_scheduler_generation`, `test_scheduler_backfill_predecessor`, `test_retention`, `test_file_orchestration_journal`, `test_orchestration_chain`, `test_warm_start_chaining`, `test_source_scoped_dispatch` — 3068 passed in 1107.62s), plus an AST enumeration of every class declaring `has_active_pipeline` / `has_completed_pipeline` / `has_active_orchestration`. The instrumentation was removed; it is not in the diff.

  **Production side — all three gates are dead.** Every repository reachable in a supported deployment implements `candidate_state`: `chain_repository.py:31` `PsycopgOrchestratorRepository`, `file_orchestration_journal.py:525` `FileOrchestrationJournalRepository`, `scheduler_file_providers.py:518` `FileRawHandoffCandidateRepository`; the Protocol at `scheduler_adapters.py:50` declares it too.

  **Test side — all three gates fire.** 17 executions across 9 test functions, all in `tests/test_production_scheduler.py`: `:391-397` from `test_active_cycle_orchestration_without_hydro_state_skips_all_candidates` (`FakeActiveCycleOrchestrationRepository`); `:398-409` from `test_active_duplicate_pipeline_is_skipped_before_submission`, `test_plan_production_cli_public_path_skips_active_duplicate` (`FakeActiveRepository(active=True)`), `test_active_hydro_state_is_skipped_as_active[×4]`, `test_active_cycle_pipeline_job_is_skipped_as_active[×3]` (`FakeHydroStateRepository`); `:414-424` from `test_completed_duplicate_pipeline_is_skipped_before_submission`, `test_completed_duplicate_is_skipped_before_not_ready_canonical_gate` (`FakeActiveRepository(completed=True)`), `test_completed_hydro_state_is_skipped_as_completed_not_active[×5]` (`FakeHydroStateRepository`). The three depending fixtures — `FakeActiveRepository` (`:33429`), `FakeActiveCycleOrchestrationRepository` (`:33675`), `FakeHydroStateRepository` (`:33692`) — implement no `candidate_state`.

  Full record: `.workplans/1736/dead-gate-verdict.md`.
- [x] Apply the decision rule in `design.md`: delete the three `not callable(state_provider)` gates in `build_candidates` identified by their guards in `design.md` (the two pre-strict-warm-start `active_duplicate_pipeline` gates and the `completed_duplicate_pipeline` gate) — NOT the fourth `cycle_active_blocks_candidate` site, which is out of scope if no supported deployment or exercised fixture depends on them; otherwise keep them, pin them with a test that names the depending deployment, and record the refusal to delete.

  **Verdict: KEPT — the issue's "delete" option is refused in writing.** The gates are production-dead but nine exercised test functions depend on them, and the decision rule counts exercised fixtures. Pinned by `tests/test_production_scheduler.py::test_duplicate_pipeline_dedup_gates_are_kept_for_repositories_without_candidate_state`, which names the three production repositories that must implement `candidate_state`, the three fixtures that must not, and drives all three gates through `run_once`. Bite proof: replacing `not callable(state_provider)` with `False` in all three gates fails that test.
- [x] Record the verdict verbatim in the PR body. — verdict text lives in `.workplans/1736/dead-gate-verdict.md` for the PR body.

## 3. Tests (seams: `build_candidates`, `cycle_completion_status`)

- [x] Positive: id-only `hydro_run` + matching `state_id` + four-field-matching run manifest ⇒ candidate `skipped` with its terminal reason; no `retry_strict_warm_start_terminal_init_state_mismatch`; no submission.
- [x] **Negative pin 1** (budget bypass): id-only + matching `state_id` + **no** run manifest ⇒ budgeted `strict_warm_start_terminal_init_state_mismatch`, with the retry block unchanged; assert the decision is NOT `strict_warm_start_terminal_run_manifest_missing`.
- [x] **Negative pin 2** (repaired checkpoint, #3b587c55): id-only + matching `state_id` + run manifest whose `checksum` disagrees ⇒ mismatch, recompute.
- [x] Successor gate: id-only + matching `state_id` + four-field-matching manifest + `successor_state` **not ready** ⇒ `strict_warm_start_successor_checkpoint_missing`, not a skip.
- [x] Conflict shapes unchanged: present-field disagreement; identity fields without `init_state_id`; no identity fields at all.
- [x] Wide fully-matching `hydro_run` row ⇒ byte-identical to today including the run-manifest-missing route and evidence keys.
- [x] Special branches unchanged: `candidate_state` terminal-source branch, `COLD_START_QUARANTINED` escape.
- [x] Parity guard: for the id-only + proving-manifest shape, `cycle_completion_status`'s verdict and the candidate ladder both treat the row as current; the intentional divergences are asserted as divergences, not forbidden.
- [x] Redaction corner: a redacted (placeholder) field on either side is skipped, not treated as absent-and-upgradable in a way that admits an unproven reuse.
- [x] Partial-but-agreeing pair (added: the shape the leg admits beyond the literal id-only row, now documented in `design.md`'s table): `init_state_id` + an agreeing `init_state_checksum` with `uri`/`valid_time` absent + four-field-matching manifest ⇒ reuse exit; the same shape with the present `init_state_checksum` **disagreeing** ⇒ budgeted `strict_warm_start_terminal_init_state_mismatch`, no upgrade. `tests/test_production_scheduler.py::test_db_free_partially_recorded_agreeing_terminal_row_is_reused_when_manifest_proves_it` (RED under stashed pre-change source) and `::test_db_free_partially_recorded_disagreeing_terminal_row_stays_on_the_budgeted_mismatch`.

## 4. CI routing

- [x] `scripts/select_ci_tests.py` routes `services/orchestrator/scheduler_candidates.py` to every suite added or touched here; verify with the selector's own test suite.

  **Zero rule-table edits required.** All new tests were added to the existing seam suite `tests/test_production_scheduler.py`, which `scheduler_candidates.py` already reaches through the `services/orchestrator/**` rule (`scripts/select_ci_tests.py:623`); no earlier `stop_on_match` rule claims the module. Verified:
  `printf 'services/orchestrator/scheduler_candidates.py\n' | select_ci_tests --changed-file -` selects 32 suites including `tests/test_production_scheduler.py`; adding the changed test file to the input also pulls in the meta-guard `tests/test_select_ci_tests.py`. `uv run pytest tests/test_select_ci_tests.py -q` → 211 passed.

## 5. Evidence

- [x] `uv run pytest -q` on the touched suites (local) — paste counts.

  - `uv run pytest tests/test_production_scheduler.py tests/test_select_ci_tests.py -q` → **2093 passed** (1882 + 211); 12 tests added by this change (11 in the #1736 block + 1 dead-gate pin).
  - Wider regression under the audit run (nine orchestrator suites): **3068 passed in 1107.62s**.
  - RED-before proof (a) — stash the source change only, run the new block: `test_db_free_id_only_terminal_row_is_reused_when_run_manifest_proves_the_init_state`, `test_db_free_id_only_terminal_row_admitted_by_manifest_still_obeys_the_successor_gate` and `test_candidate_and_verdict_paths_agree_a_manifest_proven_id_only_row_is_current` fail (3 failed, 10 passed); stash popped immediately.
  - RED-before proof (b) — the two negative pins are green on unmodified source **by construction** (today every id-only row already routes to the budgeted mismatch), so they were proved against the *forbidden* implementation instead: relaxing the leg to `terminal_init_state_match(...) == "match"` alone reddens `..._without_run_manifest_keeps_the_budgeted_mismatch` (flips to `strict_warm_start_terminal_run_manifest_missing`, exactly the budget bypass), `..._with_disagreeing_manifest_checksum_is_recomputed`, the `redacted_field_without_manifest` case, and the two pre-existing guards `test_candidate_wrapper_keeps_selected_driven_compare_for_id_only_terminal_rows` / `test_db_free_id_only_terminal_row_keeps_the_budgeted_mismatch_decision` (5 failed, 8 passed). Mutant reverted.
  - Dead-gate pin bite proof: replacing `not callable(state_provider)` with `False` in all three gates fails `test_duplicate_pipeline_dedup_gates_are_kept_for_repositories_without_candidate_state`. Mutant reverted.
- [x] `uv run ruff check .`
- [x] `openspec validate fix-terminal-reuse-id-only-hydro-run --strict --no-interactive`
- [ ] node-27: full targeted backend suites per the verification matrix.
- [ ] node-22 live receipt on a revisited already-`succeeded` cycle: `skipped_candidate_count` equals the completed-model count, `submitted_count` covers only genuine gaps, and no `basins_lh_gl`-scale whole-cohort `nhms_forecast` array appears. **Status: not attempted locally — this receipt is taken on node-22 after merge and deploy.**

  **Correction (supersedes the earlier "blocked until #1748 clears" note): this receipt is not blocked by the stall — it is the stall's verification.** The read-only diagnosis (`.workplans/1748/diagnosis.md`, and https://github.com/DankerMu/SHUD-NWM/issues/1748#issuecomment-5381249082) established that the wedged idempotency key is derived from the cohort's member set: `candidate_execution_cohort_run_id` hashes `sorted(model_id\0candidate_id)` (`services/orchestrator/scheduler_execution.py:914-927`) and the key is `run_id:stage` (`services/orchestrator/chain_runtime_utils.py:462-480`). Once this change makes the already-`succeeded` models skip, the remaining members mint a **different** cohort key, so the spent `identity_mismatch_released` reservation is routed around rather than waited on. The first post-deploy pass therefore yields this receipt directly, and its outcome is also the decision point for #1748: unstalled ⇒ #1748 is ordinary queued work with a frozen repro; still stalled ⇒ #1748 becomes the next emergency. Nothing from #1748 entered this branch.
