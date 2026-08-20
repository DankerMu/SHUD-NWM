# Proposal: align-multibasin-oom-non-transient

## Why

Two approved specs contradict each other on `OUT_OF_MEMORY` retry policy:
`job-retry-mechanism` (Retry Guard — Non-Transient Error Exclusion,
spec.md:151) rules OOM non-transient — "MUST NOT schedule an automatic
retry", "SHALL mark the job as permanently failed immediately" — while
`multibasin-state-idempotency` spec.md:53 lists "out-of-memory" alongside
node failure / preemption / timeout in the transient array-task retry
scenario. Code is already aligned (#1161: `OUT_OF_MEMORY` in
`NON_TRANSIENT_ERROR_CODES`, services/orchestrator/retry.py); this is pure
spec-prose residue from the 2026-06-23 bulk import (`35ae1b96`) that the
#1161 sweep missed. The dual authority is a re-entry point for the waste
#1161 removed. (Issue #1323.)

## What Changes

- Rewrite "Scenario: transient array task retry" in
  `multibasin-state-idempotency` (MODIFIED delta on the `Resumable
  downstream failures` requirement): remove `out-of-memory` from the
  transient list and add a cross-reference sentence routing OOM to
  `job-retry-mechanism`'s non-transient / permanent-failure path.
- Fix the same stale prose in the unarchived delta copy
  `openspec/changes/fix-node22-scheduler-business-concurrency/specs/multibasin-state-idempotency/spec.md:78-79`
  so its future archive does not re-inject the contradiction.
- Spec-only: zero code changes (`services/orchestrator/retry.py` untouched).

Non-goals: archived change texts (history, not rewritten); the parity-anchor
regex extension over free spec prose (alternative recorded in #1323, left to
triage); issues #1312/#1313/#1314 (no overlap).

## Capabilities

### Modified Capabilities

- `multibasin-state-idempotency`: transient array-task retry scenario no
  longer lists out-of-memory; OOM explicitly routed to the
  `job-retry-mechanism` non-transient permanent-failure path.

## Impact

- `openspec/specs/multibasin-state-idempotency/spec.md` (via this delta at
  archive time)
- `openspec/changes/fix-node22-scheduler-business-concurrency/specs/multibasin-state-idempotency/spec.md`
  (direct edit of the unarchived sibling delta)
