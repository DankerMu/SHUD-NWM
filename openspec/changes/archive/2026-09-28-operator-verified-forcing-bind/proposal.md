## Why

After #2667, any held forcing master freezes its source's forward lane. A held forcing master is one that is `reserved`, unbound, `submit_result_ambiguous`, and has a complete forcing submit identity. Restart reconcile then reports it in one of these ways, and each writes pass evidence only:

- `multiple_matches_blocked`
- `identity_mismatch_blocked`
- a persistent `query_unavailable`

No supported command resolves it:

- the automatic forcing bind (`bind_forcing_submit_attempt`) runs only on a single owned proof;
- `permit_forcing_submit_retry` runs only on credible absence;
- `demote-reserved-job` and the #2668 `bind-reserved-job` both refuse forcing rows.

The #2668 listing already routes these rows to `escalate` with `follow_up_issue` `#2675`, so the "triage trap" described in the issue body no longer exists on master. What is missing is an exit (issue #2675).

Triage decision (maintainer, 2026-09-28): implement only the **operator-verified forcing bind** (the issue's recommendation). The exit for a dead job (operator-verified absence) is split out to #2682.

## What Changes

- **`bind-reserved-job` gains a forcing branch.** The same command and both entrypoints; the forecast inputs are unchanged, and forcing rows additionally take the optional owner flags below. The typed journal CAS dispatches on the durable row:
  - a held forcing master is bound on operator-verified sacct evidence;
  - forecast rows keep the #2668 path unchanged.
- **Forcing evidence.** The operator's `--submit-line` (sacct `SubmitLine`) must satisfy three checks:
  - its single `--comment=` value equals the row's own attempt comment, `forcing_attempt_comment_for(idempotency_key, submission_attempt)`, which is also the durable `slurm_comment`;
  - its `--array=` value is `0-<n-1>` (optionally `%<k>`), with `n == len(cohort_members)`;
  - `--slurm-submit-time` falls within `[attempt anchor, checked-at]`.

  The Slurm id is canonical, and the same claimant exclusivity as #2668 applies.
- **Owner evidence (forcing only).** When the row requires Slurm ownership, the operator also passes `--slurm-user` and `--slurm-account` from the same sacct output. They must equal the row's `expected_slurm_user` / `expected_slurm_account`; otherwise the new refusal is `slurm_owner_mismatch`. This keeps a wrong-owner job (reported `query_unavailable` on the comment-less cluster) from being bound only to read `reconcile_unverified` on the next inflight pass.
- **Post-state.** Exactly the automatic forcing bind tuple (`bind_forcing_submit_attempt`): `slurm_job_id`, `matched_slurm_job_id`, `submit_outcome=accepted`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=matched_bound`, `reconciliation_reason_class=null`, `status=submitted`. There is no new durable token. One `operator_verified_bind` audit event is written in the same append.
- **Listing.**
  - A forcing held outcome whose action is `multiple_matches_blocked`, or `query_unavailable` with an anchor at least 6h old or unknown, gets `operator_command` `bind-reserved-job`.
  - Other forcing actions stay `escalate`.
  - Forcing entries no longer carry `follow_up_issue` `#2675`. The absence case is documented in the runbook and points to #2682.
- **Runbook.** A forcing disposition subsection in `failed-basin-retry.md`, plus updates to `scheduler-dbfree-typed-reasons.md` and `node22-control-plane-manual-recovery.md`.

## Triage

```text
Issue type: feature (operator exit)
Fixture level: expanded
Upstream suggested level: absent (issue: M, needs-triage); direction settled by the maintainer (bind only).
Blast radius: a wrong forcing bind attaches another attempt's (or a foreign) array to this row, and the forecast stage consumes the wrong forcing products; a too-strict CAS leaves the lane frozen.
Selected risk packs: CLI entry, file IO / persisted state (typed CAS, zero-byte refusals), schema (listing mapping, refusal tokens), concurrency (claimant exclusivity under the cycle and inventory locks), legacy compatibility (forecast path unchanged), error handling, docs, Slurm lifecycle (SubmitLine / array parity; node-22 rehearsal).
Evidence floor: CAS refusal matrix red to green; happy path through the real journal plus inflight reconcile plus the candidate-state decision; listing mapping tests; node-27 focused and full; node-22 copy rehearsal.
```

## Impact

- `services/orchestrator/file_orchestration_journal.py`: the forcing branch of the operator bind.
- `services/orchestrator/operator_reserved_bind.py`: help text only, if needed.
- `services/orchestrator/operator_action_listing_held.py`: the forcing mapping.
- Tests: bind CAS, CLI, lane, listing, and the #2675 reproduction test.
- Runbooks.
