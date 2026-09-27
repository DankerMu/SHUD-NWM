## Context

Issue #2655. node-22 is explicitly comment-less (`AccountingStoreFlags=(null)`, Slurm 23.11.4). Measured read-only on node-22 2026-09-27:

- `sacct --parsable2 --format=...,SubmitLine` returns, on every forecast array-task allocation row (e.g. `56823_0`), `/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:cycle_ifs_2026092500_convert_cohort_34e13d82a8a5:forecast /tmp/nhms_6y2bx17b.sbatch`; `.batch`/`.extern` step rows have an empty `SubmitLine` (they are already ineligible by job name).
- The four held masters map one-to-one by key: ifs 0925 -> 56823, gfs 0925 -> 56839 (47/47 COMPLETED each), ifs 0911 -> 47091 (36 COMPLETED + 2 FAILED of 38), gfs 0912 -> 47826 (38 COMPLETED).
- Output size with `SubmitLine`: 0925 window 188 rows / 26 KB; widest held window (anchor 2026-09-12T08:15Z) 5380 rows / 751 KB — inside `MAX_COMMENT_SACCT_BYTES` (2 MiB) and `MAX_COMMENT_SACCT_ROWS` (20 000).
- The gateway builds sbatch as an argv list (`services/slurm_gateway/real_backend.py` `_submit_rendered_script`), so `--comment=nhms_idem:<key>` is one argv element; keys match `^[A-Za-z0-9:._-]+$` (`services/orchestrator/reservation.py`), so they never contain whitespace or `|`.

## Design

Change surface: `services/orchestrator/reconcile.py` (`_query_name_window_fallback` format list, `_parse_fallback_sacct_rows`, fallback classification, the `fallback_unique` commit call and outcome construction), `services/orchestrator/file_orchestration_journal.py` (`commit_pipeline_job_submit_attempt` new keyword, `_reconcile_inventory_jobs_matching_unlocked` claimant filter), `services/orchestrator/scheduler_runtime.py` (`_serialize_reserved_unbound_outcome` additive key), `services/orchestrator/chain_stage_execution.py` (forecast ambiguous-submit event `origin_error_code`/`origin_error_message`), runbooks.

Must preserve:
- Comment-storing and unknown-capability clusters: no `SubmitLine` consulted, byte-identical query and behavior (the exact-comment lane uses its own 7-field format).
- Every existing fallback outcome/evidence shape when any remaining master's key is unknown (count-only semantics, window-overlap claimant rule).
- The durable #1564 held tuple on every unsuccessful fallback, so `demote-reserved-job` CAS stays valid.
- Durable tokens: a successful bind persists the existing `slurm_binding_source=slurm_name_window_unique` / `reconciliation_source=slurm_name_window_unique` / `matched_bound` / canonical `slurm_accounting_submitted_at`. **No new durable token**: the node-22 checkout must stay rollback-safe — older code validating `SLURM_BINDING_SOURCES` (`accepted_submit_identity.py`) would reject an unknown token in the journal. Provenance of the basis lives in pass evidence (`fallback_match_basis`).
- Active-owner and same-accounting-incarnation occupancy gates; empty-comment-as-not-stored at both reserved comment gates; present-but-different comment fatal.
- `MAX_FALLBACK_MASTERS=2` bound on retained non-excluded masters and the shared byte/row/time budget.

Must add/change:
- Format `JobID,JobName,State,ExitCode,Comment,User,Account,Submit,SubmitLine`; parser reads `SubmitLine` as `"|".join(fields[8:])`; an 8-field row (older sacct / test fixtures) yields no key.
- Key extraction: whitespace-split tokens starting `--comment=`; exactly one distinct non-empty value -> key, else none. Master key = common key of all its eligible rows, else unknown.
- `submitline_exact` applies only when every eligible master has a known key; only then are known-different-key masters excluded, before the two-master cap. Any unknown key -> nothing excluded, count-only over all eligible masters (a known-foreign + unknown pair stays ambiguous). The parser reads every row of the bounded (`MAX_COMMENT_SACCT_ROWS`) result with no early stop, so the basis is decided over all eligible masters (an unknown-key master late in the output still forces `name_window_count`); `MAX_FALLBACK_MASTERS` caps only the returned records. Side effect on key-less windows: a malformed-Submit eligible row after the first two masters is now seen, so such a window reports transient `query_unavailable`/`fallback_submit_unparsable` instead of `ambiguous_fallback_match` (both held, fail-closed).
- Classification per spec delta (basis `submitline_exact` vs `name_window_count`).
- `commit_pipeline_job_submit_attempt(..., fallback_submitline_key=None)`: when set, (a) refuse with zero bytes unless it equals the committing row's `slurm_comment_for(idempotency_key)`, (b) the claimant scan counts a sibling only when its own idempotency comment equals the key.
- Additive pass-evidence key `fallback_match_basis` on reserved-unbound fallback outcomes (not durable).
- Forecast ambiguous-submit cause is recorded on the `submission_ambiguous` event details as `origin_error_code` (gateway code, else `SBATCH_SUBMIT_RESULT_AMBIGUOUS`) / `origin_error_message` (redacted); the reserved forecast master row is unchanged (no `error_code`). Reason: a released identity-blocked row copies the row, and released rows stay out of automatic retry only because they carry no `error_code` (`release_identity_blocked_reservation` invariant; `SLURM_TIMEOUT` etc. are transient). The key is `origin_*`, not `error_code`, because `scheduler_state_failure._state_error_code` reads `error_code` from event details (precedent: #2584 `origin_error_code`).

Governing invariant: a comment-less reservation binds a Slurm master only when that master is provably its own submission (exact key) and no other accepted submission carries the same key, or — when exact keys are unavailable — under the unchanged count-only uniqueness rule; every uncertain case stays held.

Sibling surfaces:
- Producers of the key: gateway sbatch argv (`real_backend.py`) and `slurm_comment_for` — read-only here, unchanged.
- Other sacct consumers: visibility probe (`reconcile.py` ~:246) and exact-comment lane (~:942) — unchanged, must not gain `SubmitLine`.
- Durable claimant scan `_reconcile_inventory_jobs_matching_unlocked` — changed only under a proven key.
- Binding-source enumeration (`accepted_submit_identity.py` `SLURM_BINDING_SOURCES`, lifecycle validators) — unchanged by design (no new token).
- Inflight terminal projection after bind (`reconcile_inflight_jobs` -> `_terminal_file_cohort_identity_matches` -> `project_forecast_cohort_tasks`): unchanged; its `hydro_is_retryable` gate leaves already-terminal `hydro_run` rows untouched (0911/0912 stay `succeeded`), `created` rows (0925) advance.
- Consumers of the evidence action: `scheduler_no_progress.py` tracker, `scripts/node22_scheduler_stall_health.py` suppression — unchanged; a bound row simply stops appearing.
- Operator path `demote-reserved-job` — unchanged; still valid on every held row.

Seams under test: `reconcile_reservations`/restart-reconcile through the fake sacct seam (`_bounded_sacct_stdout` monkeypatch, `tests/test_gateway_reconcile_claimant_exclusivity.py` helpers) with a real `FileOrchestrationJournalRepository`; `commit_pipeline_job_submit_attempt` directly for the key-refusal CAS; scheduler serialization of outcomes; chain ambiguous-submit durable row.

Required evidence:
- gfs+IFS reservations, windows both containing arrays A (key=IFS) and B (key=gfs) -> each binds its own array, basis `submitline_exact`, both iteration orders.
- 107 foreign-key masters + 1 equal -> binds equal.
- two equal-key masters -> `ambiguous_fallback_match`/2, held tuple byte-identical.
- known-foreign-key master B + unknown-key master X -> `ambiguous_fallback_match`/2, basis `name_window_count`, no bind, held tuple byte-identical.
- inconsistent master: foreign-key row first, then equal-key or key-less row of the same master -> master key unknown -> count-only.
- any eligible master with 8-field row / empty / key-less / multi-valued / inconsistent SubmitLine -> pre-change outcome and claimant blocking.
- commit with foreign key -> refusal, zero journal bytes.
- bound COMPLETED 0925-shaped cohort with members at `hydro_run=created` -> next inflight pass projects terminal; `has_active_pipeline` False for members, member `hydro_run` leaves `created`, and the scheduler state decision for those models (`scheduler_state_decision` / candidate build) is no longer `skip_active`/`active_duplicate_pipeline`; no sbatch issued.
- comment-storing cluster command/format unchanged.
- forecast `submit_result_ambiguous` durable row keeps `error_code=None`; the event carries the real gateway code (`SLURM_TIMEOUT`) as `origin_error_code`; the released identity-blocked reservation is not auto-retriable.
- a classified window whose commit-time scan quarantines (`pipeline-jobs/` residue) still reports `fallback_match_basis`.
- two equal-key masters followed by an unknown-key master -> ambiguous, basis `name_window_count`.

## Rollout

node-22 deploy is `git pull --ff-only` in `/scratch/frd_muziyao/NWM` (no dependency change; no `uv sync`; the oneshot timer picks up the new code next tick). Before the pull, rehearse on a copy: `cp -a` the production journal to a scratch directory and run restart reconcile (reservations then inflight) against it with the real `default_comment_sacct_querier` and the gateway's read-only status client, via an ad hoc script using the exact interpreter `/scratch/frd_muziyao/NWM/.venv/bin/python` from a detached checkout of the PR head (not the active checkout's `.venv` rebuild). Expected: the four held masters bind to 56823/56839/47091/47826, inflight projects terminal, zero sbatch; the receipt also reports how many eligible masters in each window (widest: 0911 anchor) have an unknown key. Then pull and capture the first live pass: the four `reserved_unbound` outcomes gone, 0925 no longer `active_duplicate_pipeline` and selected (resume after the completed forecast; `2026092512` follows once 0925 completes, oldest-first backfill), `squeue`/`sacct` show no forecast resubmission for 0925/0911/0912.

Rollout prerequisite (found by the rehearsal): the fallback bind's flat-candidate scan rejects any non-`.json` entry under `pipeline-jobs/`; production holds `job_cycle_gfs_2026072300_convert_cohort_29a594caa8bc_forecast.json.bak-zombie-20260808`, which fails every bind closed as `journal_quarantined` (held tuple intact). The operator moves it out of the journal root (kept as a backup) before the pull; operator rule: no backup/residue files under the scheduler journal root (precedent #1925). The querier's shared whole-query time budget lets one bind complete per pass, so the four rows converge over ~4 passes.

Expected side effect (accepted): ifs 0911 master projects `partially_failed` (2 FAILED tasks) with two failed per-task candidate rows; its `hydro_run` rows stay `succeeded` (not retryable), the cycle is outside the discovery window, and reconcile never submits downstream.

## Non-goals

- Changing Slurm config (`AccountingStoreFlags=job_comment`) or the job name / wckey (alternative A in the issue).
- An operator `bind-reserved-job` command (alternative B).
- The out-of-scope siblings named in #2655 (downstream-before-upstream state_save submission, state_save ambiguous duplicates) and the stall-probe blind spot (#2662).
- A packaged reconcile-only CLI; the rehearsal script is ad hoc and its output is the receipt.

## Review focus

1. Exclusion happens before the two-master cap and cannot turn a genuine double submission into a bind.
2. Unknown-key paths keep classification, claimant rule, and the durable held tuple byte-identical to pre-change; pass evidence only gains `fallback_match_basis=name_window_count`.
3. The claimant-filter change is gated by a key the journal re-verifies against the committing row.
4. No durable token/enum change; rollback to the previous checkout still reads bound rows.
5. Comment-storing lane untouched.
