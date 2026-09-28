## Why

#2667 (#2666) made the DB-free candidate-state decision fail closed on a held reservation: a `reserved` row with no Slurm binding makes the candidate skip `active_duplicate_pipeline`. That check runs ahead of the terminal branches, so the cycle reads `gap` and holds the source's single backfill slot until restart reconcile binds, demotes, or releases the row (issue #2668). Some held shapes have no supported exit:

- **(a)** `ambiguous_fallback_match`. The basis is either `name_window_count` (an eligible master has no provable key) or `submitline_exact` with two masters carrying the row's key. `failed-basin-retry.md` says "no supported operator bind command; keep the row held and escalate", and demote is forbidden while the job ran.
- **(b)** A comment-less fallback window, `[attempt anchor, now]`, that has grown past the scan budget. The row reports `query_unavailable` on every pass.
- **(c)** An unversioned forecast cohort master (`legacy_unversioned_read_only`). The demote CAS refuses it.

These shapes freeze the whole source's forward lane. `list-operator-actions` cannot see them, and the #2662 stall probe treats zero-submit `active_duplicate_pipeline` passes as idle, so nothing alerts.

## What Changes

- **New row-scoped operator command `bind-reserved-job`**, file-journal only, modelled on #1564 `demote-reserved-job`. It binds one held accepted-submit forecast cohort master to an operator-verified Slurm master.
  - Operator inputs, all required (the Slurm submit time must fall inside `[attempt anchor, checked-at]`):
    - `--job-id`
    - `--slurm-job-id`, a bare numeric master id
    - `--slurm-submit-time`, the sacct `Submit` value
    - `--submit-line`, the sacct `SubmitLine` excerpt
    - `--expected-attempt`
    - `--expected-attempt-started-at`
    - `--checked-by`
    - `--checked-at`
    - `--verification-note`
    - `--confirm`
  - Key check: the SubmitLine must carry exactly one distinct `--comment=` value (`_submitline_comment_key`), and it must equal the row's own `nhms_idem:<idempotency_key>`.
  - CAS, evaluated under the cycle lock: current contract master; `reserved`; unbound; `submit_result_ambiguous`; expected attempt and anchor equal; durable reconcile tuple is the held tuple.
  - Claimant exclusivity: the Slurm id must not be bound or claimed by any other row. This reuses the #2655 journal claimant scan narrowed by key.
  - Post-state: the same durable tuple the #2655 fallback bind writes (`matched_bound`, `submit_outcome=accepted`, `status=submitted`, `reconciliation_source=slurm_name_window_unique`, `matched_slurm_job_id`, `slurm_accounting_submitted_at`). There is no new durable token, so rollback stays safe. One operator audit event carrying the evidence is written in the same durable append.
  - Every refusal writes zero bytes.
  - The next pass's `reconcile_inflight_jobs` projects the bound master to its terminal status as usual.
- **Visibility.** `list-operator-actions` also lists a held reservation that reconcile could not resolve, as decision `held_reservation_unresolved`. Entries are read from `restart_reconcile.reserved_unbound.outcomes[]` of the scanned passes, per a closed per-action table:
  - `ambiguous_fallback_match`: always listed, with `operator_command` `bind-reserved-job`.
  - `legacy_unversioned_read_only` / `multiple_matches_blocked`: always listed, with `escalate`.
  - `query_unavailable` / `fallback_no_match` / `absence_unconfirmed`: listed with `triage` once the attempt anchor is ≥ 6h old or unknown.
  - `identity_mismatch_blocked` / `stale_attempt_blocked` / `journal_quarantined`: listed with `escalate` under the same age rule.
  - `bound` / `reservation_lost` / `absence_retry_permitted`: never listed.
  - Any unknown action: listed with `escalate`.
  - An entry disappears once a newer reconciling pass no longer reports that job held.
- **Runbook.** `failed-basin-retry.md` case 2 and the #2666 section of `scheduler-dbfree-typed-reasons.md` point to `bind-reserved-job`, with its sacct verification procedure.

## Triage

```text
Issue type: bugfix (missing operator exit) + operator surface
Fixture level: expanded
Upstream suggested level: absent (issue readiness needs-triage; direction chosen here: bind command, not completion-verdict relaxation — relaxation clears only the "members already succeeded" shape and leaves a sole-attempt held row, whose members stay `created` forever, with no exit)
Blast radius: a wrong bind attaches a cohort to another job (wrong terminal projection, wrong successor state) or double-binds one Slurm master; a leaky refusal writes partial journal state; a noisy listing buries real operator actions.
Selected risk packs: Public API/CLI/script entry; Schema/field names; Concurrency/shared state; Auth/permissions/secrets (operator evidence redaction); Legacy compatibility; Error handling; Documentation; Slurm production lifecycle; Run manifest/QC provenance
Evidence floor: red-before tests, CAS/negative matrix, both CLI entrypoints, listing tests, focused suites + full `uv run pytest -q` on node-27, ruff, strict OpenSpec, node-22 scratch-journal rehearsal with a synthesized held row (zero sbatch), runbook
```

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `production-scheduler-orchestration`: add "Operators can atomically bind a manually verified held reservation" and "Unresolved held reservations SHALL be enumerable from db-free pass evidence".

## Impact

- Code:
  - new `services/orchestrator/operator_reserved_bind.py` (a CLI sibling of `operator_reserved_demotion.py`);
  - registration in `services/orchestrator/cli.py`;
  - a typed CAS method on `FileOrchestrationJournalRepository`;
  - `services/orchestrator/operator_action_listing.py`.
- Tests: a new bind suite plus listing tests, registered in `scripts/select_ci_tests.py`.
- Docs: `docs/runbooks/failed-basin-retry.md`, `docs/runbooks/scheduler-dbfree-typed-reasons.md`, `docs/runbooks/node22-control-plane-manual-recovery.md` (operator-action table).
- Additive pass-evidence key `submission_attempt_started_at` on reserved-unbound outcomes (`scheduler_runtime.py`, `scheduler_evidence_payload.py`). No scheduler decision change (the #2667 skip stays), no reconcile decision change, no DB schema, no display/gateway/sbatch change.
- Deviation from the #2668 acceptance: shape (c) gets visibility and a named refusal, but no bind exit (see design Non-goals). It gets a follow-up issue, as the forcing siblings do.
