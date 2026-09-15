## ADDED Requirements

### Requirement: Operator-action decisions SHALL be enumerable from db-free pass evidence

The scheduler CLI SHALL provide a read-only `list-operator-actions` subcommand that scans the most recent N terminal scheduler pass evidence files under the evidence root (`--evidence-root`, defaulting to `NHMS_SCHEDULER_EVIDENCE_ROOT`), excluding `.pre_execution.json` files and ordering by modification time. It SHALL identify blocked candidates by decision literal — `permanent_failure`, `cancelled_manual_retry_required`, `blocked_strict_warm_start_init_state_mismatch`, `blocked_journal_predecessor_identity_quarantine` — not by the `manual_retry_required` flag, so that bounded-summarized passes remain enumerable. It SHALL also list each model named in a not-selected `source_cycles` entry whose `selection_reason` is `journal_predecessor_identity_quarantine_breaker_engaged`, because a breaker-released cycle never reaches candidate construction. Each listed action SHALL carry `candidate_id`, `source_id`, `cycle_time`, `model_id`, `decision`, `reason`, `attempt`, `retry_limit`, `occurrences` (null when absent), and first/last seen pass. The command SHALL exit `1` when at least one action is listed, `0` when none, and `2` when the evidence root is missing or unreadable; an individual unreadable pass file SHALL be reported and SHALL NOT abort the scan. A scanned pass SHALL be decidable only when it is readable and its terminal status belongs to a closed allowlist of statuses known to be written only after candidate construction ran (a post-construction status missing from the allowlist errs toward undecidable), and it is not a size-fallback product (`resource_limit_blocked` carrying `limit.pre_limit_status`), because that product empties `source_cycles` and so cannot show breaker-released cycles — its summarized blocked candidates SHALL still be listed; every other pass (for example `lock_contended`, `preflight_blocked`, or an unknown status) SHALL be reported under `non_evaluating_passes` with its status. When no action is listed and at least one scanned pass dropped its candidate lists under the evidence byte budget, or no scanned pass is decidable, or a size-fallback pass is newer than the newest decidable pass (its emptied `source_cycles` may hide a breaker release that engaged after that decidable pass), the command SHALL report those passes and exit `3` (undecidable) instead of `0`. The bounded candidate summary SHALL retain `retry_policy` `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required`, including false and zero values. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

#### Scenario: Summarized pass still lists a budget-exhausted candidate
- **WHEN** the latest pass evidence was bounded-summarized and contains a blocked candidate whose decision is `blocked_strict_warm_start_init_state_mismatch`
- **THEN** `list-operator-actions` SHALL list it with `attempt` and `retry_limit` taken from the retained bounded keys
- **AND** the command SHALL exit `1`

#### Scenario: Breaker-released cycle is listed from source-cycle evidence
- **WHEN** a pass released a breaker-engaged cycle from the backfill slot so no candidate entry exists for it
- **THEN** `list-operator-actions` SHALL list each model of that not-selected entry with decision `blocked_journal_predecessor_identity_quarantine`

#### Scenario: Dropped candidate lists are undecidable
- **WHEN** no action is found and a scanned pass marked its candidate lists as dropped
- **THEN** the command SHALL exit `3` and name that pass

#### Scenario: A window without an evaluating pass is undecidable
- **WHEN** every scanned pass is unreadable or non-evaluating (for example lock-contended or preflight-blocked) while an older evaluated pass outside the window holds a blocked candidate
- **THEN** the command SHALL list those passes under `unreadable_passes` or `non_evaluating_passes` and exit `3`, never `0`

#### Scenario: A window of size-fallback passes is undecidable
- **WHEN** every scanned pass is a size-fallback product whose original payload had a breaker-released not-selected source cycle and no other listed decision
- **THEN** the command SHALL report those passes under `non_evaluating_passes` and exit `3`, never `0`
- **AND WHEN** such a pass still carries a summarized blocked candidate of a listed decision
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`

#### Scenario: A size-fallback pass newer than every decidable pass is undecidable
- **WHEN** no action is listed, the newest scanned pass is a size-fallback product, and an older scanned pass is decidable
- **THEN** the command SHALL exit `3`, never `0`
- **AND WHEN** instead a decidable pass is newer than every size-fallback pass in the window
- **THEN** the command SHALL exit `0`

#### Scenario: No operator actions
- **WHEN** the scanned passes contain only blocked candidates of other decisions, at least one scanned pass is decidable, and no pass dropped its candidate lists
- **THEN** the command SHALL print an empty `operator_actions` list and exit `0`

### Requirement: A breaker- or budget-blocked candidate SHALL re-enter only through a pinned one-shot operator confirmation

An operator SHALL be able to record a re-entry confirmation for one `(source_id, cycle_time, model_id)` and one of the decisions `blocked_journal_predecessor_identity_quarantine` or `blocked_strict_warm_start_init_state_mismatch` via the `confirm-operator-reentry` subcommand, which SHALL default to dry-run and write only with `--attest`. The confirmation SHALL be stored as a file-journal `forecast_cycle` pipeline event with a dedicated `event_type` (`operator_reentry_confirmation`) that the manual-retry marker reader does not adopt, and SHALL carry the operator, reason, request id, and an integer pin; a breaker confirmation SHALL also carry the recorded stale `init_state_id` it authorizes. The writer SHALL refuse when the journal records no completed identity for the target; for the breaker decision it SHALL refuse when the breaker is not engaged, when the recorded token differs from the live recorded `init_state_id`, or when the pin differs from the model's live quarantine rerun count; for the budget decision it SHALL refuse when the pin differs from the model's live budget re-entry count. The quarantine rerun count SHALL be the number of cohort MASTER rows for the cycle whose quarantine-rerun provenance names the model, regardless of their terminal status and of the identity they recorded, so every quarantine rerun accepted for submission increments it by exactly one at acceptance time. The budget re-entry count SHALL be the number of cohort MASTER rows for the cycle whose budget re-entry provenance names the model — provenance stamped by the reservation writer when the reserved basin carries a strict warm-start retry with an `operator_reentry_confirmation` evidence block — regardless of terminal status, job id, or retry suffix, so every budget re-entry accepted for submission increments it by exactly one at acceptance time. Neither count SHALL depend on the stage-scoped retry attempt or on job-id prefixes.

When the §8.7 quarantine breaker is engaged, the scheduler SHALL emit the existing `journal_predecessor_identity_mismatch` retry instead of the breaker `blocked` decision if and only if a matching confirmation exists whose pin equals the model's current quarantine rerun count; the discovery-side backfill selection SHALL treat such a confirmed model as having real work so its cycle keeps the execution slot. When the strict warm-start retry budget is exhausted, the scheduler SHALL emit the existing `strict_warm_start_terminal_init_state_mismatch` retry instead of the budget `blocked` decision if and only if a matching confirmation exists whose pin equals the model's current budget re-entry count. Pin comparison SHALL be exact equality. A re-entry retry SHALL carry an `operator_reentry_confirmation` evidence block and SHALL still pass the per-model forcing witness on its lane. Confirmations SHALL be read journal-direct through a repository accessor that reads the cycle's full event rows regardless of model filtering and forecast-cycle terminal status; a repository without the accessor SHALL behave as if no confirmation exists. The blocked decisions SHALL remain absent from both forced-resubmit whitelists, the global retry limit SHALL NOT change, and the scoring and filtering surfaces SHALL NOT write to the journal. The blocked evidence `retry_policy` SHALL name the `confirm-operator-reentry` command and the `node22-control-plane-manual-recovery` runbook.

#### Scenario: Confirmed breaker re-entry runs once and the breaker re-engages
- **GIVEN** a candidate blocked by the quarantine breaker whose quarantine rerun count is N
- **WHEN** an operator attests a confirmation with pin N
- **THEN** the next pass SHALL submit a real replacement forecast carrying quarantine provenance
- **AND WHEN** that rerun completes re-recording the same stale token
- **THEN** the quarantine rerun count SHALL become N+1, the confirmation SHALL no longer match, and the candidate SHALL return to `blocked` with no submission

#### Scenario: A rerun that records a different stale token does not reuse the confirmation
- **WHEN** the confirmed rerun completes recording a different stale token whose own occurrence count equals the old pin
- **THEN** the quarantine rerun count SHALL still become N+1, the confirmation SHALL NOT match, and the candidate SHALL be `blocked` with no submission

#### Scenario: A failed confirmed rerun does not restore the confirmation
- **WHEN** the confirmed quarantine rerun was accepted for submission and then failed at the compute layer while the recorded identity is still stale
- **THEN** the quarantine rerun count SHALL already equal N+1 from the accepted rerun, the confirmation SHALL NOT match on any later pass, and no further quarantine rerun SHALL be submitted on that confirmation

#### Scenario: No second submission while the rerun is in flight
- **WHEN** a pass runs after the confirmed re-entry was submitted but before the rerun completes
- **THEN** no further forecast submission SHALL occur for that candidate

#### Scenario: Confirmed budget re-entry without raising the global limit
- **GIVEN** a strict warm-start candidate blocked with `attempt >= retry_limit` and budget re-entry count M
- **WHEN** an operator attests a confirmation with pin M
- **THEN** the next pass SHALL submit one retry without any change to `NHMS_SCHEDULER_RETRY_LIMIT`
- **AND** once that retry is accepted for submission the budget re-entry count SHALL be M+1, and every later pass SHALL keep the candidate `blocked` with no further submission on that confirmation, whether the rerun succeeds, fails, or was reserved under a different job-id prefix than the retries that exhausted the budget

#### Scenario: Stale or absent confirmation keeps the fail-stop
- **WHEN** no confirmation exists, or its pin differs from the live value
- **THEN** the breaker and budget decisions SHALL remain `blocked` with no submission

#### Scenario: Re-entry without the model's own forcing stays blocked
- **WHEN** a matching breaker confirmation exists on the non-strict lane but the candidate's own forcing witness is absent
- **THEN** the candidate SHALL land in the named missing-forcing `blocked` decision and SHALL NOT submit

### Requirement: The released-identity recovery listing SHALL isolate malformed journal rows without swallowing budget refusals

`query_released_identity_blocked_jobs` and the `recover-released-identity-blocked-reservation` command SHALL skip an individual flat pipeline-job row whose read fails, in both the first flat scan and the cycle-scoped confirming read, with a single-row content-validation reason (a closed, enumerated set covering malformed JSON, non-object payloads, record type, schema, identity, cycle-time, identity-field mismatch, accepted-submit evidence validation, and JSON node/depth limits), and SHALL continue the scan. Every skip SHALL appear in the command receipt as `skipped` entries carrying the path and reason, with a `skipped_count`. Every other journal error — including file, depth, record, and byte budget refusals, unreadable files, and containment faults — SHALL still propagate. The targeted `--job-id` mode and the unscoped whole-tree fallback leg are unchanged. Candidate-state and pipeline-job reads used by the scheduler SHALL keep failing closed on the same rows.

#### Scenario: One malformed row does not blind the recovery listing
- **WHEN** the flat pipeline-jobs directory holds one invariant-invalid row next to wedged released-identity rows
- **THEN** the list receipt SHALL include the wedged rows
- **AND** SHALL report the malformed row under `skipped` with reason `file_journal_evidence_invariant_invalid`
- **AND** this SHALL hold when the malformed row belongs to the same cycle as the wedged rows or has a filename that does not resolve to a cycle

#### Scenario: Budget refusal still raises
- **WHEN** the scan exceeds the record budget while a malformed row is also present
- **THEN** the command SHALL fail with `file_journal_record_limit_exceeded`
