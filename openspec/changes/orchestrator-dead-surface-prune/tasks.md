# Tasks

Fixture level: expanded · repair intensity: standard · issues: #1581, #1659, #1661, #1762, #1763.
Line cites against `origin/master` `4f3fd89a`; symbol names are authoritative.

## 0. Evidence Floor

Oracles: local pytest (macOS) for red/green and greps; node-27 for the real-DB
run of the same suites at the final head; CI status read at every head.

- [x] Red proof (#1762): `_cycle_scope_from_file_run_id("analysis_era5_2026010100_2026010200_model_x")` returns `("ERA5", 2026-01-01T00Z)` at `4f3fd89a` and `None` after (scratch `red_proof.txt`)
- [x] Red proof (#1661): the cohort-identity stub's `assert include_direct_jobs is False` fails against the kwarg-free production call before the stub edit
- [x] Parity counter-proof (#1581): a fake member added to one alias site (`chain.COMPLETED_HYDRO_STATUSES = {...}` literal) turns `tests/test_hydro_status_set_parity.py` red; a fake member appended to the shared set turns the enum assertion red; execution record in the PR body
- [x] `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile_file_cohort_identity.py tests/test_gateway_reconcile_comment_accounting.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_retry.py tests/test_run_identity.py tests/test_hydro_status_set_parity.py tests/test_scheduler_generation.py tests/test_scheduler_backfill.py` green locally (implementer: 3503 passed / 3 skipped; orchestrator re-run: seven heavy suites 3420 passed / 3 skipped + parity/run_identity/comment_accounting 83 passed)
- [x] `uv run pytest -q tests/test_select_ci_tests.py` green (new test file added; the first head fc74f888 failed its directory-rule importer audit on CI and node-27 — seven `services/orchestrator/*.py -> tests/test_hydro_status_set_parity.py` pairs undispositioned at fc74f888, eight once fix pass 1 imported `scheduler_state_decision` — fixed in fix pass 1 in three places per the selector's own #1455 convention: the `services/orchestrator/**` directory rule closes six pairs, and the stop-rule-owned `chain.py` / `file_orchestration_journal.py` pairs are closed at their sites via `CHAIN_IMPORTER_TESTS` and `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`; the frozen broad-rule list pin in `tests/test_select_ci_tests.py` grew 44→45 with the wall-clock recorded; fix pass 2 (round-2 cand-11) additionally routes `db/migrations/*.sql` to the parity suite; the implementer's "460 passed" claim at fc74f888 was wrong)
- [x] `uv run ruff check .` clean
- [x] Acceptance greps recorded in the PR body:
  - `git grep -n include_direct_jobs -- services tests` → only `_materialize_latest_unlocked`'s parameter and routing (`:9946-9950` region) and its three callers' `include_direct_jobs=False` (`:3775 :4115 :4327`, which survive unchanged)
  - `git grep -n "_next_sequence(" -- services tests` → zero hits (unlocked variant excluded); the archived `tasks.md:35` mention is an accepted residual (history, not edited)
  - `git grep -n _ANALYSIS_RUN_ID_RE -- services/orchestrator/file_orchestration_journal.py` → zero hits; `run_identity.py:36` untouched
  - `git grep -n candidate_idempotency_key -- services tests apps workers packages` → zero hits
  - `git grep -n 'succeeded", "parsed", "published"' -- services` → hits only at `scheduler_state_types.py` (the single definition), `retry.py` (3-member #1155 set) and `file_orchestration_journal.py` `_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES` (cycle domain, accepted residual)
- [ ] node-27 receipt at the final head (same suites, real DB; command lines + counts in the PR body)
- [x] `openspec validate orchestrator-dead-surface-prune --strict --no-interactive`

## 1. #1661 — `_cycle_rows_by_model_unlocked` flag removal (D1)

- [x] 1.1 Remove `include_direct_jobs` from the signature (`:5854`), the `direct_jobs` ternary (`:5903-5910`) and its insert loop (`:5912-5913`, inside the retained outer loop at `:5911`), and the in-guard `_cache_cycle_rows` store (`:5916-5921`); docstring states that the batch reducer never includes direct jobs and never stores
- [x] 1.2 Drop the kwarg at the five direct callers (`:2397 :3690 :4052 :4215 :4903`) and at the `_materialize_latest_unlocked` routing arm (`:9951`); `_materialize_latest_unlocked`'s own parameter and routing unchanged; of its nine callers, the three at `:3771 :4111 :4323` keep `include_direct_jobs=False` verbatim (those kwargs are the materialiser's, not the reducer's) and the six default-`True` callers (`:5202 :9041 :9181 :9227 :9427 :9998`) are untouched
- [x] 1.3 `tests/test_gateway_reconcile_comment_accounting.py:147` drops the kwarg; `tests/test_gateway_reconcile_file_cohort_identity.py:766-771` stub drops the parameter and the `is False` assertion (red proof recorded first)

## 2. #1659 — `_next_sequence` wrapper removal (D2)

- [x] 2.1 Delete `:9603-9605`
- [x] 2.2 `tests/test_file_orchestration_journal.py:12701 :12726 :12738 :12766 :12771` call `_next_sequence_unlocked`; assertions verbatim

## 3. #1762 — analysis derivation branch removal (D3)

- [x] 3.1 Delete `:12668-12678` (the `safe_run_id` local, the analysis match and its `except` pair; the forecast/cohort `try/except: pass` is followed by a bare `return None`) and the import `:129-131`; rewrite the docstring (`:12655-12663`) to say only forecast/cohort derive and why analysis cannot (validator rejects on both branches)
- [x] 3.2 Test: `_validate_pipeline_job_identity` raises `file_journal_run_mismatch` for `analysis_era5_2026010100_2026010200_model_qhh.v1` with `model_id="model_qhh.v1"` and with `model_id=None` (the durable written ruling)
- [x] 3.3 Test: `_cycle_scope_from_file_run_id("analysis_era5_2026010100_2026010200_model_x") is None` and `_cycle_scope_from_idempotency_key("analysis_era5_..._model_x:forecast") is None` (fall-open); `tests/test_run_identity.py` analysis case untouched and green

## 4. #1763 — `candidate_idempotency_key` removal (D4)

- [x] 4.1 Delete `reservation.py:67-76`; rewrite `:45-46` to `run_id:stage[:suffix]` minted by `chain_runtime_utils._cycle_stage_idempotency_key`
- [x] 4.2 (round-2 cand-07) rewrite the module docstring `reservation.py:21-24`, which still spelled `f"{source_id}:{cycle_id}:{basin_id}:{stage}"`, to the same production shape; drop the phantom-shape mention at `chain_runtime_utils.py:472`; `grep -rn 'basin_id}:{stage\|source:cycle:basin:stage' services/ workers/ apps/` → zero hits

## 5. #1581 — single-source hydro status sets + parity lock (D5)

- [x] 5.1 `scheduler_state_types.py`: `DURABLE_HYDRO_SUCCESS_STATUSES` keeps its four members; the `:30-34` comment restates the D5 ruling (DB lane closed enum → `"complete"` never matches; journal lane: no production writer, status unvalidated by `_validate_hydro_run_identity`, test face uses it) and drops the "collapsing would change scheduler behavior" absolute; `ACTIVE_HYDRO_STATUSES` gets a one-line "pending divergence unadjudicated, see parity test" comment; new `HYDRO_RUN_CODE_CLEARING_STATUSES` as a **`frozenset`** (six members, moved from `scheduler_state_failure.py:321`; its comment moves too with the stale cite `file_orchestration_journal.py:1507-1513` corrected to the actual write site `:2485`)
- [x] 5.2 `chain.py:208` and `chain_repository.py:19`: `COMPLETED_HYDRO_STATUSES = DURABLE_HYDRO_SUCCESS_STATUSES` (import; same object); `ACTIVE_HYDRO_STATUSES` literals keep membership with the divergence comment
- [x] 5.3 `scheduler_state_failure.py:149` uses `DURABLE_HYDRO_SUCCESS_STATUSES` (add to the `:56` import); `:321` becomes `_HYDRO_RUN_CODE_CLEARING_STATUSES = HYDRO_RUN_CODE_CLEARING_STATUSES` (name kept for the `:27706`/`:27769` inventory tables)
- [x] 5.4 `file_orchestration_journal.py:2485` uses `HYDRO_RUN_CODE_CLEARING_STATUSES` imported from `scheduler_state_types`
- [x] 5.5 `tests/test_hydro_status_set_parity.py`: identity (three sites + trigger seam + the two spec-named from-import consumers `file_orchestration_journal.COMPLETED_HYDRO_STATUSES` and `scheduler_state_decision.DURABLE_HYDRO_SUCCESS_STATUSES` — round-1 cand-02, mutants rebinding either survived the first cut), the durable-set inline-site probe (sentinel member via `try/finally` on the mutable `DURABLE_HYDRO_SUCCESS_STATUSES` only), enum membership parsed from `db/migrations/000003_enums.sql` + `000013` with `"complete"` as the one named exception, `ACTIVE_HYDRO_STATUSES` divergence lock, code-clearing set: identity across `scheduler_state_types` / `scheduler_state_failure` / journal + `isinstance(frozenset)` + six-member value (no mutation probe: it is immutable by the `:27962` type pin)
- [x] 5.8 (round-2 cand-11) the parity enum oracle sweeps every `db/migrations/**/*.sql` for the one `CREATE TYPE hydro.run_status` block and every `ALTER TYPE hydro.run_status ADD VALUE`, instead of two hardcoded files, so a future `ADD VALUE 'complete'` migration turns the lock red; `scripts/select_ci_tests.py`'s `db/migrations/*.sql` rule routes migration PRs to the parity suite (#1774 precedent), exact-list pins in `tests/test_select_ci_tests.py` updated; red proof: a fake `ADD VALUE 'complete'` migration in a temp migrations dir is invisible to the old parser and reds the enum assertion under the sweep
- [x] 5.6 `tests/test_retry.py:164-176` docstring corrected per the issue's second comment; assertions `:178-181` unchanged
- [x] 5.7 Behavior-parity check: `tests/test_scheduler_generation.py`, `tests/test_scheduler_backfill.py`, `tests/test_production_scheduler.py`, `tests/test_orchestration_chain.py` pass with zero assertion edits

## Risk packs

- Concurrency / shared state / ordering — selected (unfingerprinted store and lock wrapper removed; no new lock; existing cache-sweep and containment tests are the evidence).
- Legacy compatibility / examples — selected (name imports, `getattr` seam, compat list, inventory tables; parity identity assertions + import smoke).
- Documentation / migration notes — selected (rulings land in comments/docstrings; reviewer checks them against D3/D5).
- Public API, Config, File IO, Schema, Auth, Resource limits, Error handling, Release, domain packs — not selected (see design.md reasons).

## Accepted residuals (pre-recorded so reviewers do not re-litigate)

- `_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES` (`file_orchestration_journal.py:606`) matches the #1581 grep but is a cycle-domain set; out of scope by the issue.
- `openspec/changes/archive/2026-08-18-journal-containment-aware-existence-probe/tasks.md:35` mentions `_next_sequence`; archives are history and are not edited.
- `ACTIVE_HYDRO_STATUSES` `"pending"` divergence is locked and labelled, not adjudicated (issue Out of scope; no new issue filed this batch by user instruction — recorded in the work summary).
- `"complete"` stays in the hydro durable-success set (D5 ruling); the parity test names it as the single out-of-enum member. Test-face reachability is wider than the issue's 13-failure figure: `tests/test_file_orchestration_journal.py` holds 19 more `hydro_status="complete"` constructions.
- `_materialize_latest_unlocked`'s three `include_direct_jobs=False` callers (`:3775 :4115 :4327`) and its six default-`True` callers are all untouched; none is #1661's dead surface.
- #1661 acceptance bullet 3 names `tests/test_gateway_reconcile.py`, which was split into 25 `tests/test_gateway_reconcile_*.py` files before this change; the two successors that reference `_cycle_rows_by_model_unlocked` (`comment_accounting`, `file_cohort_identity`) are the Evidence Floor suites, and the whole `tests/test_gateway_reconcile_*.py` family is covered by the CI selector's importer closure (round-1 cand-04, recorded in the PR 偏离记录).

## Non-goals

See proposal.
