# Design: operator-verified forcing bind (#2675)

## Context

Held forcing master (verified on master `bf1145908`):

- **Durable state.** `reserved`, `slurm_job_id` / `matched_slurm_job_id` empty, `submit_outcome=submit_result_ambiguous`. It is written by `_transition_forcing_submit_ambiguity` (`AcceptedSubmitTransition.timeout`). No `reconciliation_*` tuple is ever written for these rows.
- **Identity.** `forcing_submit_identity_is_complete(row)` holds:
  - the stage is a forcing alias;
  - `slurm_comment == forcing_attempt_comment_for(idempotency_key, submission_attempt)`;
  - the anchor is present;
  - `cohort_members` cover tasks `0..n-1`;
  - the owner is present when ownership is required.
- **Reconcile.** Each pass reports `multiple_matches_blocked`, `identity_mismatch_blocked` (multi-record or single-record) or `query_unavailable`, as pass evidence only.
- **Existing typed writers, neither reusable as is:**
  - `bind_forcing_submit_attempt` has no audit fields, no submit-time window, no mandatory external evidence and no claimant exclusivity. It is safe today only because its caller pre-derives the id from a singular owned proof.
  - `permit_forcing_submit_retry` is the absence exit, which is split out to #2682.
- **Submit shape.** The gateway submits forcing arrays as `sbatch --array=0-{n-1}%{k} --comment=<attempt comment> <script>` (`services/slurm_gateway/real_backend.py` `_submit_rendered_script`), so sacct `SubmitLine` carries both the attempt comment and the array size.
- **Production.** Node-22 held 0 `reserved` forcing rows. This comes from a read-only raw scan on 2026-09-28T08:20Z over every `pipeline_job` record in `journal/*/*.jsonl`.

## Governing invariant

An operator bind writes only the durable tuple that the lane's automatic bind already writes, plus one audit event. It happens only after every piece of operator evidence has been checked against the row's own identity: key or attempt comment, array size, owner, submit window, attempt and anchor, and id exclusivity. Every refusal leaves the journal byte-identical.

## Must preserve

- The forecast `bind-reserved-job` path, byte for byte, including its refusal set and the #2668 suites.
- The automatic forcing bind (`bind_forcing_submit_attempt`, called only from reconcile's owned or controller proof) and `permit_forcing_submit_retry`: unchanged.
- The forecast listing mapping, and the legacy unversioned `#2674` follow-up.
- The chain-level `bind_only` forcing submit recovery (`chain_stage_execution.py`, around :240-290) and `overlapping_unresolved_forcing_job`.

## Sibling surfaces

- `bind_forcing_submit_attempt`, `permit_forcing_submit_retry`, `demote_operator_verified_reserved_job` (still refuses forcing).
- The chain `bind_only` recovery.
- Reconcile's `_bound_forcing_submit_reconcile_job`, `_forcing_accounting_identity_matches` and `_transition_forcing_submit_ambiguity`.
- `is_resolved_forcing_attempt` and `overlapping_unresolved_forcing_job`.
- `_state_held_reservations` / the candidate-state decision.
- `HELD_RESERVATION_HELP` / `LIST_OPERATOR_ACTIONS_HELP`.
- The three runbooks.

## Decisions

1. **One command, dispatch in the journal.**
   - `bind_operator_verified_reserved_job` reads the row under the cycle lock and branches **before** the current-contract / `accepted_submit_row_kind` checks. Forcing reservations carry no accepted-submit contract version (`chain_forecast_orchestrator_cycle.py` forcing branch), so with today's order they read `not_held`. The branches:
     - forecast cohort master: the #2668 path, byte-for-byte unchanged;
     - forcing master (`is_forcing_stage_name(stage, job_type)`): the forcing branch below;
     - anything else: `not_held`, as today.
   - The CLI (`operator_reserved_bind.py`) keeps both entrypoints and the forecast inputs, and adds optional `--slurm-user` / `--slurm-account`, which the forcing CAS reads. `--submit-line` is the evidence for both lanes. The forcing receipt adds `lane`, `array_spec` and `slurm_submit_time` (the verified `--slurm-submit-time`, the audit event's key); its `slurm_accounting_submitted_at` reports the durable row's value, null. The forecast receipt keys are unchanged.
2. **Forcing CAS**, re-read under the cycle lock. Every failure is a named refusal with zero bytes written.
   - `forcing_submit_identity_is_complete(row)`. Otherwise `not_held`.
   - `status == reserved`, no bound or matched Slurm id, `submit_outcome == submit_result_ambiguous`, `reconciliation_decision` empty. Otherwise `not_held`.
   - Expected attempt and anchor equal the durable values. Otherwise `stale_attempt`.
   - `floor(anchor) <= --slurm-submit-time <= --checked-at`. Otherwise `submit_time_outside_attempt_window`. The anchor is floored to whole seconds because sacct `Submit` has whole-second precision while the durable anchor keeps microseconds; flooring cannot admit an earlier attempt, because the attempt-comment check below excludes it. Latent today: node-22 measured a forcing anchor-to-Submit gap of 19.99-141.19 s over 40 bound masters (2026-09-28).
   - The SubmitLine's single `--comment=` value (`reconcile._submitline_comment_key`) equals the durable `slurm_comment` (the attempt comment). Otherwise `submitline_key_mismatch`.
   - The SubmitLine's single `--array=` value matches `^0-(\d+)(%\d+)?$` with `\1 + 1 == len(cohort_members)`. Otherwise the new refusal `array_spec_mismatch`. A missing, duplicated or other-shaped value also refuses.
   - Owner, the same rule as inflight `_forcing_accounting_identity_matches`. The operator passes `--slurm-user` / `--slurm-account`, the sacct `User` / `Account` of that master.
     - When `slurm_ownership_required` is set, both expected values and both supplied values must be non-empty.
     - Any non-empty expected user or account must equal the supplied value, even when the flag is off.
     - Otherwise the new refusal is `slurm_owner_mismatch`.
     - Both values are checked at entry, before any read: at most 256 characters (else `file_journal_evidence_limit_exceeded`); a value the operator-evidence sanitizer would alter (secret, path, URI) raises `file_journal_unsafe_identity` rather than being redacted, because the journal may hold a recorded owner in redacted form and a redacted input could collide with it; blank is allowed as empty. The checked value is both the audit text and the comparison input.
     - A row with no expected owner and no ownership requirement ignores the arguments. The next inflight pass's `_forcing_accounting_identity_matches` requires the same equality, so this check refuses up front what inflight would otherwise leave `reconcile_unverified`.
   - Canonical Slurm id `[1-9][0-9]*`. Otherwise `slurm_id_invalid`.
   - Claimant exclusivity, through the forcing branch's **own locked writer**. It cannot use `_commit_pipeline_job_submit_attempt_locked`: that path returns `stale` for non-master rows, refuses a submit instant off the fallback path, and writes a `slurm_binding_source` that the automatic forcing bind never writes. The writer takes the cycle lock, then the inventory lock, and calls:
     - `_reconcile_inventory_jobs_matching_unlocked(..., fallback_unique=True, submitline_key=<the forcing attempt comment>, strict_slurm_id_exclusivity=True)`: the cross-cycle anchor scan plus the flat settled-master scan over current forecast masters. Passing the forcing comment as the key means no held forecast master counts as a window claimant, because their keys are `nhms_idem:<key>` and never equal a forcing attempt comment.
     - `_operator_bind_slurm_id_claimed_in_cycle_unlocked`: the same-cycle scan of every row, by master part.

     Any claimant refuses as `slurm_id_claimed`. The row, plus the audit event, is then written in one append (for example through `_write_operator_bind_unlocked`, if its shape fits; otherwise an equivalent single-append helper).
   - Residual, stated plainly: forcing and other non-master rows of other cycles are not scanned. The attempt-comment check proves the SubmitLine belongs to this attempt, but not that `--slurm-job-id` was typed correctly. Unlike the forecast case, inflight reconcile does not catch a mistyped id afterwards. It queries forcing accounting without an exact comment (`require_exact_comment=False`), so another cycle's `nhms_forcing` job with the same owner would match. The runbook therefore makes the re-read concrete: `sacct --jobs=<id> --parsable2 -o JobID,JobName,User,Account,Submit,SubmitLine` must show this attempt's comment and array spec, and all four bind values are taken from that output.
3. **Post-state = the automatic forcing bind tuple.**
   - `slurm_job_id = matched_slurm_job_id = <id>`, `submit_outcome=accepted`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=matched_bound`, `reconciliation_reason_class=null`, `status=submitted`, attempt and anchor unchanged.
   - This is the tuple `is_resolved_forcing_attempt` and the inflight reconcile already read. No new durable token, so rollback is safe.
   - One `operator_verified_bind` audit event is written in the same append. It carries the redacted evidence, the lane (`forcing`), the SubmitLine comment, the array spec, the Slurm id and submit time, and the prior tuple.
   - The next inflight reconcile projects the master from sacct as usual.
4. **Listing mapping for forcing held outcomes** (`operator_action_listing_held.py`). A forcing master is identified by the job-id stage suffix, matching the forecast rule.
   - `multiple_matches_blocked`: always listed, `bind-reserved-job`. It is a real double submission, and the runbook requires the other master to be terminal or cancelled first, as in #2668.
   - `query_unavailable`: listed once the anchor is at least 6h old or unknown, `bind-reserved-job`. If sacct shows no master with this attempt's comment, escalate (#2682).
   - `identity_mismatch_blocked`, `absence_unconfirmed` (the absence case, #2682) and every other action: `escalate`, listed per the existing age rules. A foreign owner or comment collision needs owner judgement: the bind refuses a foreign owner (`slurm_owner_mismatch`), and the listing does not route these rows to it.
   - Forcing entries drop `follow_up_issue` `#2675`, because this issue closes the gap. Non-forecast, non-forcing entries keep `escalate`.
5. **Tests reuse the #2668 harnesses.** `tests/orchestrator_bind_reserved_job_helpers.py` gets a forcing held-row builder, which goes through the real forcing reservation writers and `_transition_forcing_submit_ambiguity` / the timeout transition, not hand-edited JSON.

## Non-goals

- The absence exit for a dead forcing job (#2682).
- Automatic changes to forcing reconcile.
- Owner overrides: a row whose recorded owner is wrong cannot be bound; `slurm_owner_mismatch` refuses it and it stays `escalate`.
- Forecast-path behavior changes.

## Required evidence

| id | scenario | expected |
|---|---|---|
| F1 | Reproduction (AC1): a reserved forcing master driven through `multiple_matches_blocked`, single-record `identity_mismatch_blocked` and controller-missing `query_unavailable` | row stays `reserved` and unbound; its member candidates skip `active_duplicate_pipeline` (via `_state_held_reservations`); red-first where new |
| F2 | Happy path: held forcing master + a SubmitLine with the exact attempt comment and `--array=0-<n-1>%k` + matching owner + a window-valid submit time + a canonical unclaimed id | bound; the durable row equals an identical held row bound through `bind_forcing_submit_attempt` (same fields, no `slurm_accounting_submitted_at`, no `slurm_binding_source`) apart from the audit event; then inflight reconcile over a COMPLETED array projects it terminal, and the members' concrete candidate decision is no longer a skip and names forecast as the next stage; zero gateway submit calls (real journal + `reconcile_inflight_jobs` + candidate-state decision) |
| F2b | Owner: (a) ownership required and a supplied value missing or different; (b) ownership not required but a non-empty expected user or account differs from the supplied value | `slurm_owner_mismatch`, zero bytes; a row with no expected owner and no requirement ignores the arguments |
| F3 | Stale attempt or anchor | `stale_attempt`, zero bytes |
| F4 | Submit time before the (whole-second floored) anchor or after checked-at; a same-second submit against a microsecond anchor binds | `submit_time_outside_attempt_window`, zero bytes |
| F5 | SubmitLine comment of another attempt (`:a<n-1>`), of another key, a forecast `nhms_idem:` comment, none, or two distinct values | `submitline_key_mismatch`, zero bytes |
| F6 | `--array=` missing, of the wrong size, a list form, or duplicated | `array_spec_mismatch`, zero bytes |
| F7 | Id claimed by a same-cycle row of any kind or by a current forecast master; non-canonical id | `slurm_id_claimed` / `slurm_id_invalid`, zero bytes |
| F7b | A held forecast master with the same owner and a window containing the submit time, on the same source | does **not** refuse the forcing bind (the claimant scan is keyed by the forcing attempt comment) |
| F8 | Row not held (already bound, not reserved, identity incomplete, outcome not ambiguous, reconciliation decision present) | `not_held`, zero bytes; a repeated bind lands here |
| F9 | Forecast rows | #2668 suites unchanged and green |
| F10 | CLI: both entrypoints bind a forcing row; refusals exit 2 with the token | click + argparse |
| F11 | Listing: forcing `multiple_matches_blocked` → `bind-reserved-job`, always; forcing `query_unavailable` <6h not listed, ≥6h `bind-reserved-job`; forcing `identity_mismatch_blocked` / `absence_unconfirmed` ≥6h `escalate`; no `follow_up_issue` on forcing entries | listing tests; the existing forcing escalate scenario is updated |
| F12 | Concurrent automatic forcing bind before the lock | locked re-read refuses (`not_held`), zero bytes |

New-behavior tests are red before the change and green after.

## Seams under test

- The typed CAS, driven directly: F2-F8, F7b, F12.
- The CLI, through both entrypoints: F10.
- Real journal → `reconcile_inflight_jobs` → candidate-state decision: F1, F2.
- The listing over synthetic pass evidence: F11.

## Review focus

- The dispatch order.
- Parity between the forcing tuple and `bind_forcing_submit_attempt`.
- The claimant-scan parameters (F7b).
- Owner equality against the inflight identity rule.
- Zero bytes on every refusal.
- The forecast path being byte-identical.

## Rollout

- Merge, then node-22 `git pull --ff-only`. No timer stop.
- Rehearse on a fresh copy of the production journal:
  - rewind a completed forcing cohort to its held state through the typed writers, as in the #2668 rehearsal, and label it synthetic;
  - bind it with the real CLI using live sacct values;
  - run the fenced restart reconcile and the decision harness.

  Expected: bound, projected terminal, members released, the forecast stage next, zero sbatch.
- Receipt under `docs/runbooks/receipts/`.
