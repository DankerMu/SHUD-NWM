## Context

Held reservations reach an exit only through restart reconcile: an automatic bind (exact comment, or the #1565/#2655 name-window fallback), `demote-reserved-job` (#1564, confirmed-dead only), or the identity-blocked release (#1116). #2667 turned every unresolved held row into a source-wide forward-lane freeze, because the cycle reads `gap` (`scheduler_discovery._cycle_completion_verdict`) and occupies the backfill slot. Shapes (a)–(c) in proposal.md have no exit. Production had zero `reserved` rows on 2026-09-28 and no `reserved_unbound` outcomes in the last four passes, so this is preventive.

## Goals / Non-Goals

- Goal: a supported, audited, CAS-guarded exit for a held forecast master whose job the operator has matched in sacct, for shapes (a) and (b) as they are left by the comment-less fallback.
- Goal: make every unresolved held row visible in `list-operator-actions`.
- Non-goal: changing the #2667 planner skip, the reconcile decision, the one-bind-per-pass budget, or the demote CAS.
- Non-goal: exact-comment held rows (comment-storing clusters) whose durable reason is `coverage_incomplete` / `accounting_authority_unproven` (`reconcile.py:2449-2450`, `:2486-2491`). They keep their existing exits. The bind CAS refuses them (`not_held`).
- **Deviation from the #2668 acceptance: shape (c) (legacy unversioned masters) gets visibility only, not a bind exit.**
  - The typed accepted-submit commit handles current-contract rows only. Binding a legacy row would need the legacy store path, and this change does not prove that path's inflight projection.
  - Production had zero such rows on 2026-09-28.
  - The implementer traces the writers. `chain_forecast_orchestrator_cycle.py:716` stamps the contract version. The retry-clone paths at `file_orchestration_journal.py:11443` / `:11780` drop it. The trace must show whether a clone can become `reserved` with `cohort_members` and no version. If it can, stop and report. Otherwise record the trace in the PR.
  - The PR carries a deviation record and a follow-up issue.
  - The command refuses (c) by name (`legacy_unversioned_unsupported`). The listing shows it with `operator_command: escalate`.
- Non-goal: the forcing lane's permanent `multiple_matches_blocked` / `identity_mismatch_blocked` rows. They use a different claimant model and get a follow-up issue.
- Non-goal: relaxing the completion verdict (see proposal Triage). Also not in scope: the "superseded attempt's failed task outranks a sibling success" ordering, which is the #2666 basin-18 shape. Its exit stays the manual-retry marker (below).

## Decisions

1. **Reuse the #2655 bind tuple; add no new durable token.**
   - Commit through `commit_pipeline_job_submit_attempt(idempotency_key, expected_submission_attempt=…, slurm_job_id=…, pipeline_job_id=<row id>, slurm_accounting_submitted_at=<--slurm-submit-time>, fallback_submitline_key=<key>, transition=AcceptedSubmitTransition.accounting("matched_bound", submit_outcome="accepted", matched_slurm_job_id=…, status="submitted", reconciliation_source="slurm_name_window_unique"))`. Either call it directly, or refactor its internals into a shared locked helper so the audit event lands in the same durable append.
   - Audit event: `event_type` `operator_verified_bind`. It carries the redacted `checked_by`, `checked_at`, `verification_note`, the SubmitLine key, the Slurm id, the submit time, and the prior held tuple.
   - Reason: node-22 validates `slurm_binding_source` against a closed set, so a new token would break rollback (see #2655's deviation record).
2. **CAS gate.** The row is re-read under the cycle lock. Any failed condition is a named refusal with zero bytes written. The row must satisfy all of:
   - it is a current-contract accepted-submit **master** of a forecast cohort (`accepted_submit_contract_is_current`, `accepted_submit_row_kind == "master"`);
   - `status == reserved`, `slurm_job_id` and `matched_slurm_job_id` are empty, and `submit_outcome == submit_result_ambiguous`;
   - `submission_attempt` and the attempt anchor equal the expectations;
   - it has the exact durable held tuple `reconciliation_source == slurm_exact_comment`, `reconciliation_decision == accounting_unavailable`, `reconciliation_reason_class == comment_accounting_unproven`. This matches the demote CAS (`file_orchestration_journal.py:4727`) and is exactly what reconcile writes for (a)/(b):
     - `reconcile.py:1096-1106` and `:2449-2458`: fallback saturated or unavailable;
     - `:2642-2647`: `fallback_ambiguous`;
     - `:3078-3083`: commit-scan ambiguity.
   - **Attempt window:** `anchor <= --slurm-submit-time <= --checked-at`. Otherwise the refusal is `submit_time_outside_attempt_window`. `slurm_comment_for` depends only on the key, and `absence_retry_permitted` re-reserves under the same key, so without the window a prior attempt's master would pass the key check. This mirrors the automatic fallback's window rule (`reconcile.py:594`).
3. **Key, claimant, and typed-commit refusal map.**
   - The key is `reconcile._submitline_comment_key(--submit-line)`, reused rather than reimplemented. It must be non-null and equal to `nhms_idem:<existing.idempotency_key>`; otherwise `submitline_key_mismatch`.
   - `--slurm-job-id` must be a canonical bare decimal master (`[1-9][0-9]*`: no leading zero, so `0123` can never alias `123`); otherwise `slurm_id_invalid`.
   - Refusal map for typed-commit outcomes (`file_orchestration_journal.py:3575-3722`); every one of them writes zero bytes:

     | typed-commit outcome | named refusal |
     |---|---|
     | `missing` | `not_found` |
     | `stale` / `collision` / `idempotent` | `not_held` |
     | `ambiguous_fallback_match` (claimant scan) / `active_slurm_id_occupied` / `identity_mismatch_blocked` | `slurm_id_claimed` |
     | raised `file_journal_submit_instant_required` | `slurm_submit_time_invalid` |

   - **Deliberately stricter than #1850:** a Slurm id already present as `slurm_job_id` / `matched_slurm_job_id` on any other current row is refused, even when that id is a recycled one with a different Submit incarnation, which `_settled_incarnation_matches_candidate` would accept. The operator path accepts no recycle ambiguity.
   - **Scope: the bound row's cycle, plus current masters everywhere.** The id must not be bound or claimed by any other row of the same cycle, nor by any current accepted-submit master in the reconcile inventory. The same-cycle half is the bounded cycle-scoped replay that the held cycle lock already covers: every row of that source/cycle, of any stage, kind, contract version, or status (forcing and downstream rows, legacy unversioned rows, member task rows). The cross-cycle half is the existing reconcile-inventory anchor/flat scan with the strict branches (current forecast masters, `slurm_job_id` or `matched_slurm_job_id`, settled or active). A row's id is compared by its master part (leading decimal digits, so `57553_18` claims `57553`). A replay failure in the scanned scope raises and writes nothing; it is never read as unclaimed. The automatic commit paths are unchanged.
   - **No whole-journal replay.** On node-22 the whole-tree replay exceeds its record budget by design (#1953 census; `tests/test_file_journal_full_tree_budget_contract.py`), so a whole-tree claimant scan would refuse every production bind. Damaged authority in another cycle therefore does not block a bind.
   - **Residual (deviation):** non-master rows of other cycles (a forcing, downstream, legacy, or member task row elsewhere) are not scanned. This is mitigated by the SubmitLine key check: a SubmitLine pasted from another lane or row carries a different `--comment` key and is refused as `submitline_key_mismatch`.
   - **Genuine double submission** (`submitline_exact`, `match_count=2`): the command binds only the master it is given. The runbook requires the operator to confirm in sacct that the other master is terminal or cancelled before binding; otherwise binding lets the next stage start while that master still writes the same run outputs. The verification note must record the other master's id and state. The command does not query sacct.
4. **CLI.**
   - Click and argparse share one callable in `operator_reserved_bind.py`, mirroring `operator_reserved_demotion.py`, because `cli.py` is under the large-file guard.
   - `--confirm` is enforced before the repository is built.
   - The journal root goes through `verify_journal_root_authority`.
   - The receipt is stable JSON, carrying the redacted evidence and `written_record_count`.
   - A derived-projection failure after the commit is reported as committed with warnings (same as demote). A repeated request is a zero-write `not_held`.
5. **Evidence: anchor in pass evidence (additive schema change).**
   - `_serialize_reserved_unbound_outcome` / `_restart_reconcile_attempt_evidence` (`scheduler_runtime.py:1688-1784`) add `submission_attempt_started_at` from the durable row.
   - `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS` (`scheduler_evidence_payload.py:114-134`) keeps it under compaction.
   - Rollout consequence: passes written before this key show no anchor, so 6h-rule rows in them are listed as "anchor unknown". This lasts until those files age out of the `--passes` window, and is acceptable.
6. **Listing (closed per-action table).**
   - `operator_action_listing.py` reads `restart_reconcile.reserved_unbound.outcomes[]` from the scanned terminal passes and emits decision `held_reservation_unresolved`, with `reason = <action>`, `job_id`, `source_id` / `cycle_time` (derived from `job_id` via `_accepted_submit_source_cycle_from_job_id`), `submission_attempt_started_at`, first/last seen pass, `operator_command`, and `recovery_runbook`.
   - Dedup key: `(job_id, decision)`. Held entries carry no model id.
   - Closure pin test: the table must cover exactly the reconcile `reserved_unbound` action vocabulary, like the existing `OPERATOR_ACTION_DECISIONS` pin.

     | action | listed | operator_command |
     |---|---|---|
     | `bound`, `reservation_lost`, `absence_retry_permitted`, `identity_mismatch_released` | never | — |
     | `ambiguous_fallback_match` | always | `bind-reserved-job` |
     | `legacy_unversioned_read_only` | always | `escalate` (`follow_up_issue: "#2674"`) |
     | `multiple_matches_blocked` | always (the durable decision is refused by bind) | `escalate` |
     | `query_unavailable`, `fallback_no_match`, `absence_unconfirmed` | anchor ≥ 6h before pass start, or anchor unknown | `triage` (runbook: bind if sacct shows the job, demote if confirmed dead) |
     | `identity_mismatch_blocked`, `stale_attempt_blocked`, `journal_quarantined` | anchor ≥ 6h or unknown | `escalate` |

     Any action outside the table is always listed with `escalate`, regardless of its anchor age, so it fails visible.
   - **Non-forecast held rows.** `reserved_unbound.outcomes[]` also carries forcing rows, which have no supported bind or demote exit. Any held entry whose `job_id` stage suffix does not name a forecast cohort master stage (for example `_forcing`; an id the suffix rule cannot read counts as non-forecast) SHALL use `operator_command` `escalate` (never `bind-reserved-job` or `triage`) and carry `follow_up_issue: "#2675"`. The listing and age rules are otherwise unchanged.
   - **Resolution drops the entry.** An entry is dropped when a newer scanned pass whose restart-reconcile lane ran either lacks that `job_id` in its `reserved_unbound.outcomes[]` or reports it with a never-list action. A bound, demoted, or released row stops being reserved-unbound, so later passes emit nothing for it.
   - **Exit-code boundary (a documented fifth exit-0 boundary; no new exit-3 trigger):**
     - Held entries come only from passes whose restart-reconcile lane ran.
     - Passes that skipped the lane (`scheduler_runtime.py:1571-1582`), recorded `reserved_unbound_error` (`:1612-1614`), or carry no restart-reconcile outcomes at all are reported under a new `restart_reconcile_unscanned_passes` field as `{pass, reason}` entries, with `reason` in {`restart_reconcile_absent`, `restart_reconcile_skipped`, `reserved_unbound_error`, `reserved_unbound_outcomes_absent`}.
     - Exit 0 therefore does not vouch for held rows that exist only in unscanned passes. Held rows persist across passes, so the next reconciling pass shows them.
     - Update `LIST_OPERATOR_ACTIONS_HELP` and the spec text accordingly.
   - Compacted and size-fallback passes keep `job_id` and `action` (and, after Decision 5, the anchor), so they are read the same way.

## Required evidence

| Scenario | Expected |
|---|---|
| Held (a)-shape master with the exact fallback held tuple, members' hydro `created`, planner skip, then `bind-reserved-job` with a matching SubmitLine, id, submit time inside the window, attempt, and anchor | exit 0. Row is `submitted` / `matched_bound` / `slurm_name_window_unique` with `matched_slurm_job_id` set, plus one `operator_verified_bind` event. The next `reconcile_inflight_jobs` with COMPLETED sacct projects `succeeded`, members become terminal, and `_cycle_completion_verdict` is `complete`. Red before: no command, cycle stays `gap` |
| (b)-shape (`query_unavailable` fallback held tuple) | same as above |
| Issue scenario: members already `succeeded` through a sibling attempt, and the bound master's sacct array has one FAILED task | succeeded members stay terminal. Only the failed task's member becomes `permanent_failure_guard`; `manual_retry_failed_runs` on the cohort master previews `would_mark` and re-runs only that member |
| Submit time before the anchor, or after `--checked-at` | `submit_time_outside_attempt_window`, zero bytes |
| SubmitLine key differs, is absent, or is ambiguous | `submitline_key_mismatch`, zero bytes |
| Attempt or anchor mismatch; row not `reserved`; already bound; outcome not ambiguous; exact-comment held reason (`coverage_incomplete`) | `stale_attempt` / `not_held`, zero bytes |
| Slurm id bound or claimed by a current master of any cycle (including a recycled id), or by any row of the same cycle (a forcing row, a legacy unversioned row, a member task `<id>_<n>` of another cohort, or a legacy `matched_slurm_job_id`); id malformed or non-canonical (`123_4`, `abc`, `0123`, even while another row holds `123`) | `slurm_id_claimed` / `slurm_id_invalid`, zero bytes |
| Whole-tree replay over its record budget, or damaged authority in another cycle | the bind still commits (the claimant scan never replays the whole journal) |
| A forcing row of another cycle holding the id (the documented residual) | not scanned; the bind commits |
| Legacy unversioned master | `legacy_unversioned_unsupported`, zero bytes |
| Missing `--confirm`, blank evidence, or naive timestamps | both entrypoints exit non-zero before the repository is built |
| Concurrent reconcile bind before the lock is taken | the locked re-read refuses, zero bytes |
| Secret-looking token in the evidence | redacted in both the receipt and the event |
| Post-commit projection failure | committed with a warning; a retried request is `not_held` |
| Listing: every row of the action table, including an action outside the table | as tabled. Closure pin over the reconcile action vocabulary |
| Listing: an action outside the table whose anchor is 5 minutes old | listed with `escalate` |
| Listing: a forcing `query_unavailable` held ≥ 6h, and a forcing `multiple_matches_blocked` | both listed with `escalate` and `follow_up_issue: "#2675"` |
| Listing: `query_unavailable` with anchor < 6h / ≥ 6h / unknown | not listed / listed / listed |
| Listing: a job held in pass N (listed), then bound before pass N+1 (absent there, or `bound`) | not listed; exit follows the remaining entries |
| Listing: a pass that skipped restart reconcile, has `reserved_unbound_error`, has no `restart_reconcile`, or has no `reserved_unbound.outcomes` | reported in `restart_reconcile_unscanned_passes` (`restart_reconcile_skipped` / `reserved_unbound_error` / `restart_reconcile_absent` / `reserved_unbound_outcomes_absent`); exit code otherwise unchanged |
| Evidence: `submission_attempt_started_at` in serialized outcomes and survives compaction | present |
| #2667 pins | `tests/test_scheduler_held_reservation_block.py` unchanged and green |

## Rollout

- Merge, then node-22 `git pull --ff-only`. No timer stop is needed: the command is operator-invoked and the listing is read-only.
- Rehearse on a fresh copy of the production journal:
  - synthesize an (a)-shape held master on a completed cohort, using the legacy upsert path the #2667 tests used, and label it synthetic in the receipt;
  - run `bind-reserved-job` against the copy, then run the fenced reconcile and decision harness;
  - expected: bound, projected terminal, cycle complete, zero sbatch.
- Record `list-operator-actions` output before the bind (entry present, `operator_command: bind-reserved-job`) and after the next reconciling pass (entry gone).
