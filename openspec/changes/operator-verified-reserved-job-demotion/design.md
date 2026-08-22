## Context

Issue #1564 is the deliberate operational complement to #1116. When `AccountingStoreFlags` lacks `job_comment`, reconcile records a current accepted-submit cohort master as `reserved`, unbound, `submit_result_ambiguous`, `slurm_exact_comment`, `accounting_unavailable`, and `comment_accounting_unproven`. Treating the empty query as absence would risk duplicate submission, so automation must remain fail-closed. After an operator independently proves the job dead with name/time/user/account evidence, however, the file journal has no typed way to record that proof; generic transition, HTTP manual retry, and reclaim all reject the held row.

**Fixture level:** expanded because this changes a production CLI and a persisted state transition. **Repair intensity:** high because a false positive can double-submit a forecast cohort and a partial write can separate durable state from its audit evidence. The NHMS project profile already covers orchestration, Slurm mock-vs-real parity, persisted state, and node-22 runtime receipts; no profile update is needed.

## Goals / Non-Goals

**Goals:**

- Provide one explicit, row-scoped, file-journal-only operator recovery command.
- Reject stale, bound, wrong-state, wrong-decision, wrong-reason, wrong-attempt, wrong-anchor, or unconfirmed requests with a non-zero CLI exit and byte-identical journal state.
- Atomically persist the terminal master transition, matching hydro-member failure fan-out, and a durable audit event.
- Preserve manual and automatic provenance as distinct decisions while making both legitimate absence decisions reclaimable.
- Make the next scheduler pass take the existing reclaim/resubmit path instead of `PIPELINE_ALREADY_ACTIVE`.

**Non-Goals:**

- Changing #1116's comment-capability probe or automatic fail-closed outcome.
- Adding `reserved` to HTTP/manual retry source statuses.
- Adding a comment-less automatic matching heuristic (#1565).
- Changing Slurm configuration or claiming comment storage is retroactive.
- Adding PostgreSQL columns or mirroring this operator token into the coarse PostgreSQL reclaim implementation.
- Making `identity_mismatch_released` or any generic transition reclaimable.

## Decisions

### D1: One dedicated typed file-journal CAS API

Add a dedicated repository method rather than using `transition_pipeline_job_submit_evidence`, `permit_pipeline_job_retry`, or a generic status write. It accepts the versioned master `job_id`, exact `submission_attempt`, exact `submission_attempt_started_at`, `checked_by`, timezone-aware `checked_at`, and a bounded non-empty verification note. Under the cycle lock it re-reads durable authority and succeeds only when all of these hold:

- current accepted-submit master and matching `job_id`;
- `status == reserved`, `slurm_job_id` empty, and `matched_slurm_job_id` empty;
- `submit_outcome == submit_result_ambiguous`;
- `reconciliation_source == slurm_exact_comment`;
- `reconciliation_decision == accounting_unavailable`;
- `reconciliation_reason_class == comment_accounting_unproven`;
- persisted attempt and normalized UTC attempt anchor exactly equal the expected CAS values.

A mismatch returns zero and writes no bytes. Invalid input types/enums raise `FileOrchestrationJournalError`. The generic versioned-decision whitelist remains closed to both absence retry tokens.

### D2: Distinct post-state with no reason class

Successful demotion writes `status=reservation_lost`, `reconciliation_decision=operator_verified_absence`, `reconciliation_source=slurm_exact_comment`, `submit_outcome=submit_result_ambiguous`, `matched_slurm_job_id=None`, and `reconciliation_reason_class=None`. Clearing the reason class is required by the accepted-submit invariant: reason classes belong only to `accounting_unavailable`. The prior `comment_accounting_unproven` value is retained only inside audit-event details.

`operator_verified_absence` joins `ACCEPTED_RECONCILIATION_DECISIONS`, but not the generic transition whitelist or identity-streak decisions.

### D3: State and audit event are one append transaction

The typed method writes, under one lock and one `_append_journal_records_unlocked` call:

- active member `hydro_run` rows for the same attempt to `failed` / `SLURM_RESERVATION_LOST`, matching the existing automatic absence transition;
- the cohort master transition;
- a `pipeline_event` such as `operator_verified_absence`, with `status_from=reserved`, `status_to=reservation_lost`, and bounded details containing `checked_by`, normalized `checked_at`, verification note, expected attempt/anchor, and prior reason/decision.

State without audit, audit without state, or only part of the hydro cohort must never become durable. The event identity and materialization follow the existing journal record validators and sequence allocation; no new entity type is introduced.

### D4: Two reclaim doors, all other terminal shapes closed

Both `reclaim_pipeline_job_reservation` and `_verified_accepted_submit_forecast_retry` accept exactly `{absence_retry_permitted, operator_verified_absence}` while retaining their other identity, source, outcome, unbound, reason-null, attempt, anchor, and cohort checks. `identity_mismatch_released` remains non-reclaimable. Reclaim continues to mint attempt+1 and a fresh lock-owned anchor; the operator-supplied old anchor is only a CAS expectation, never a new authority value.

### D5: One command, both CLI entrypoints, explicit non-interactive confirmation

Register `demote-reserved-job` in click and argparse with identical required inputs:

- `--journal-root`
- `--job-id`
- `--expected-attempt`
- `--expected-attempt-started-at` (ISO-8601 with timezone)
- `--checked-by`
- `--checked-at` (ISO-8601 with timezone)
- `--verification-note`
- `--confirm`

No stdin prompt is used because node-22 automation and receipts must be reproducible. Missing `--confirm` must fail before repository construction/write. Success prints a stable sorted JSON receipt including the job id, prior/new status, decision, attempt/anchor, operator fields, and written-record count. CAS refusal or validation error prints to stderr and exits 2.

### D6: Tests use the highest existing seams

Prefer a focused new CLI test module if it makes click/argparse parity readable; if added, include it in `ORCHESTRATOR_CLI_IMPORTER_TESTS` so a `cli.py`-only PR selects it. Journal tests compare bytes before/after every refusal, inspect the exact master/hydro/event durable rows on success, and drive the real demote → cycle verified-retry → reclaim path. Tests do not hand-build a post-state that bypasses the typed transition.

## Selected Risk Packs

- Public API / CLI / script entry: selected — new `nhms-pipeline` command; both entrypoints, output, stderr, and exits are contract surfaces.
- File IO / path safety / overwrite: selected — append-only file journal, lock/sequence/materialization, atomic multi-record durability, explicit journal root.
- Auth / permissions / secrets: selected — operator attribution and confirmation gate an action that can cause resubmission; no secrets may enter event or CLI output.
- Concurrency / shared state / ordering: selected — stale CAS and cycle lock determine whether a concurrent bind/reclaim wins.
- Schema / fields: selected — a new decision token and event details must satisfy accepted-submit normalization and durable record validation.
- Legacy compatibility / examples: selected — automatic absence, identity release, manual retry, PostgreSQL backend, and both CLI dispatchers stay unchanged.
- Error handling / rollback / partial outputs: selected — refusal is zero-write and state/event/hydro fan-out is one transaction.
- Documentation / migration notes: selected — node-22 operator procedure and provenance distinction are user-facing; no data migration.
- Config / project setup: not selected — no new configuration key or dependency.
- Resource limits / large input / discovery: not selected — one bounded row and bounded note; no directory discovery expansion.
- Release / packaging / dependency compatibility: not selected — existing Python entrypoint and dependencies only.
- NHMS domain packs: Slurm production lifecycle / mock-vs-real parity selected for the node-22 live receipt; geospatial, forcing windows, numerical runtime, database domain behavior, provider snapshots, and published display identity are not selected.

## Invariant Matrix

**Governing invariant:** only an explicitly confirmed operator request that still matches the exact durable comment-unobservable reservation attempt may atomically convert it into an audited reclaimable absence; no other row or concurrent successor may become retryable.

**Source-of-truth identity/contract:** current accepted-submit master `job_id`, contract version, submission attempt and UTC attempt anchor, plus the persisted held accounting tuple.

**Surfaces:**

- Producers: #1116 reconcile writes the held `accounting_unavailable/comment_accounting_unproven` row; unchanged.
- Validators/preflight: CLI timestamp/confirmation/note validation; accepted-submit token/tuple normalization; typed CAS predicate.
- Storage/cache/query: file-journal locked append, pipeline-job direct materialization, hydro latest materialization, pipeline event stream.
- Public routes/entrypoints: click and argparse `nhms-pipeline demote-reserved-job`.
- Frontend/downstream consumers: scheduler cycle verified-retry shortcut, reservation reclaim, existing sbatch path; no frontend.
- Failure/rollback/stale state: wrong row shape, stale attempt/anchor, concurrent bind/permit/reclaim, append failure, event validation failure.
- Evidence/audit/readiness: success JSON, durable operator event, runbook, focused tests, node-22 live receipt.

**Regression rows:**

- Exact held master + valid confirmation/operator evidence → one terminal master + member fan-out + audit event; cycle shortcut true; reclaim mints a new attempt and fresh anchor.
- Any mismatch in job id/master kind/status/binding/outcome/source/decision/reason/attempt/anchor or missing confirmation/operator evidence → exit 2 / zero journal byte change.
- Automatic `absence_retry_permitted` → existing reclaim behavior unchanged.
- `identity_mismatch_released`, manual retry, generic transition, and PostgreSQL repository → remain outside the new operator reclaim contract.
- Append/event fault before commit → neither state nor audit becomes durable.

## Boundary-Surface Checklist

- Shared helper roots: accepted-submit decision normalization and journal record validation.
- Public entrypoints: click and argparse command parity.
- Read surfaces: current master/attempt lookup and event queries.
- Write surfaces: one locked multi-record append plus direct/latest materialization.
- Producer/consumer evidence: reconcile held tuple → operator event/decision → cycle shortcut → reclaim.
- Stale/idempotency: repeat request and concurrent successor reject without bytes; post-demotion repeat also rejects.
- Unchanged consumers: HTTP manual retry, generic transitions, identity release, PG reclaim, automatic reconcile.

## Risks / Trade-offs

- **[Operator falsely attests absence]** → explicit confirmation plus named/time-stamped bounded evidence; runbook requires `sacct`/`squeue` checks. The command cannot itself prove external absence.
- **[CAS succeeds after concurrent bind]** → re-read under the cycle lock and compare all durable fields.
- **[Master changes without event or hydro fan-out]** → one validated append transaction; fault-injection tests.
- **[New token accidentally broadens generic/manual paths]** → negative whitelist tests and unchanged manual retry set.
- **[CLI drift]** → parameterized click/argparse tests and selector importer ownership.

## Migration Plan

1. Deploy code and run focused/local tests.
2. On node-22, identify one held row and independently prove absence with the documented Slurm queries.
3. Invoke the command with exact persisted attempt/anchor and operator evidence.
4. Run one scheduler pass and capture durable event, reclaim/new attempt, disappearance of `PIPELINE_ALREADY_ACTIVE`, and one cohort resubmission.
5. Rollback is a code rollback only before use. After a successful operator demotion/reclaim, do not rewrite journal history; recover through normal scheduler semantics and the audit trail.

## Open Questions

None. The issue defines the human trust decision; this design constrains how that assertion becomes durable and reclaimable.
