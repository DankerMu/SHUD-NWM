Fixture level: expanded
Project profile: NHMS
Upstream suggested level: absent
Issues: #2655
Minimal mergeable slice: 1-2 are the production fix; 3 (ambiguity cause evidence) and 4 (runbooks) ride along per the issue's acceptance criteria.

## 1. SubmitLine exact-key classification (reconcile.py)

- [x] 1.1 Append `SubmitLine` to the comment-less fallback `--format` only; parse it as the rejoined remainder after `Submit`; an 8-field row yields no key. Visibility-probe and exact-comment sacct commands stay byte-identical.
- [x] 1.2 Extract the submit-line key (single distinct `--comment=` value) per eligible row; aggregate per bare master (known only if all eligible rows agree).
- [x] 1.3 Basis `submitline_exact` only when every eligible master's key is known: then exclude known-different-key masters before the `MAX_FALLBACK_MASTERS` cap. Any unknown key -> exclude nothing, basis `name_window_count`, pre-change count-only over all eligible masters. Parser reads every row of the bounded result (no early stop) so the basis is decided over all eligible masters.
- [x] 1.4 Carry `fallback_match_basis` on reserved-unbound fallback outcomes (including a `journal_quarantined` raised after classification) and serialize it additively in `restart_reconcile.reserved_unbound.outcomes[]` (not durable).

## 2. Durable claimant exclusivity under a proven key (file_orchestration_journal.py)

- [x] 2.1 `commit_pipeline_job_submit_attempt` accepts `fallback_submitline_key`; refuses with zero journal bytes when it differs from the committing row's idempotency comment.
- [x] 2.2 `_reconcile_inventory_jobs_matching_unlocked` counts a reserved-unbound sibling as a claimant for a keyed candidate only when the sibling's idempotency comment equals the key; unkeyed candidates keep the window-overlap rule; active-owner and same-incarnation occupancy unchanged.
- [x] 2.3 Bind persists the existing `slurm_name_window_unique` durable tuple (no new token); binding provenance survives defer/terminal projection as before.

## 3. Forecast ambiguous-submit cause (chain_stage_execution.py)

- [x] 3.1 The forecast-cohort ambiguous-submit cause is recorded in the `submission_ambiguous` event details (`origin_error_code` = gateway code or `SBATCH_SUBMIT_RESULT_AMBIGUOUS`, redacted `origin_error_message`); the durable transition row is unchanged (forcing-only `error_code` guard kept), because released identity-blocked rows stay out of automatic retry only while they carry no `error_code`. Regression: row `error_code` None, event carries `SLURM_TIMEOUT`, released row `should_auto_retry` False.

## 4. Operations docs

- [x] 4.1 `docs/runbooks/failed-basin-retry.md`: fallback section documents SubmitLine exact-key basis, the updated outcome table (`fallback_match_basis`), and a "completed but unbound" note (automatic bind when SubmitLine proves the key; demotion stays for confirmed-dead only).
- [x] 4.2 `docs/runbooks/node22-control-plane-manual-recovery.md` decision table and `docs/runbooks/scheduler-dbfree-typed-reasons.md`: route `active_duplicate_pipeline` + reserved-held forecast master + `ambiguous_fallback_match` to the fallback section.

## 5. Verification

- [x] 5.1 Tests (red on pre-change source where the behavior is new, green after) for every Required evidence row in design.md: concurrent gfs/IFS both orders; 107 foreign + 1 equal; two equal-key masters ambiguous with held tuple byte-identical; known-foreign + unknown-key pair stays ambiguous; unknown-key variants (8-field, empty, key-less, multi-valued, inconsistent with the foreign-key row first) pre-change outcome + claimant blocking; commit refusal zero bytes; bound COMPLETED cohort with members at `hydro_run=created` -> inflight terminal projection -> `has_active_pipeline` False and the scheduler state decision no longer `active_duplicate_pipeline`, no forecast resubmission; comment-storing command unchanged; forecast ambiguous cause on the event (row code-less, release not auto-retriable); late unknown-key master forces `name_window_count`; residue quarantine keeps `fallback_match_basis`.
- [x] 5.2 Update existing pins that assume the 8-field fallback format (`tests/test_gateway_reconcile_comment_accounting.py` format assertion, `_fallback_row` helper) without weakening them.
- [x] 5.3 `uv run ruff check .`, focused `uv run pytest -q tests/test_gateway_reconcile_*.py tests/test_orchestration_chain.py`, full `uv run pytest -q` on node-27, strict OpenSpec validate. — node-27 isolated worktree: full run @6005aed08 20875 passed / 8 failed (6 CI-selector guards fixed in 6b14748b3; 2 entropy tests from a stale node-27 `origin/master` ref, green after `git fetch origin master`); fix-pass head cb4bd6997 focused 3498 passed rc=0; CI green @6b14748b3.
- [x] 5.4 node-22 scratch-journal rehearsal (design.md Rollout): four held masters bind to 56823/56839/47091/47826, inflight terminal projection, zero sbatch; receipt `docs/runbooks/receipts/2026-09-27-issue2655-submitline-rehearsal.md` (one bind per pass; found the `.bak-zombie` residue prerequisite).
- [ ] 5.5 After merge, with operator approval: move `pipeline-jobs/*.bak-zombie-20260808` out of the production journal root (backup kept), then node-22 `git pull --ff-only`; expect one bind per pass (~4 passes); first live pass receipt — held outcomes gone, 0925 not `active_duplicate_pipeline` and selected (resume after forecast); `2026092512` follows once 0925 completes state_save_qc, no forecast resubmission for 0925/0911/0912.

## Risk packs considered (core)

- Public API / CLI / script entry: selected - scheduler restart reconcile is the shared entry; covered by 5.1 restart-reconcile seam tests.
- Config / project setup: not selected - no config or env change; capability probe unchanged.
- File IO / path safety / overwrite: not selected - journal writes go through existing typed CAS; no new path handling.
- Schema / columns / units / field names: selected - sacct format list and pass-evidence key change; durable tokens deliberately unchanged (5.1, 5.2, design Must preserve).
- Auth / permissions / secrets: not selected - owner/account filter unchanged; SubmitLine read only for our own user/account rows.
- Concurrency / shared state / ordering: selected - claimant exclusivity across concurrent sibling reservations; both iteration orders + CAS refusal tested (5.1).
- Resource limits / large input / discovery: selected - SubmitLine roughly doubles row bytes; measured 751 KB at widest held window, existing budget unchanged; 107-master test (5.1).
- Legacy compatibility / examples: selected - unknown-key and comment-storing paths byte-identical; no new durable token keeps rollback safe (5.1, design).
- Error handling / rollback / partial outputs: selected - every unsuccessful path preserves the held tuple; refusal writes zero bytes (5.1).
- Release / packaging / dependency compatibility: not selected - no dependency change; node-22 deploy is a pull.
- Documentation / migration notes: selected - runbook routing (4.1, 4.2).

## Domain risk packs considered

- Slurm production lifecycle / mock-vs-real parity: selected - consumes live sacct `SubmitLine`; node-22 rehearsal + live receipt (5.4, 5.5).
- Run manifest / QC provenance: selected - the bind attaches cohort provenance to a Slurm master; exact-key basis and projection tested (5.1).
- Geospatial, hydro-met windows, SHUD numerics, PostGIS/Timescale, providers, published artifacts, alerting lanes: not selected - untouched surfaces (stall probe is #2662).
