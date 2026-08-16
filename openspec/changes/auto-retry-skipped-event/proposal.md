# Proposal: auto-retry-skipped-event

## Why

`job-retry-mechanism` (Retry Guard — Non-Transient Error Exclusion,
spec.md:139-176) has required since its introduction that when a
`pipeline_job` is blocked from automatic retry by a non-transient error code
or an unknown code defaulted non-transient, a `pipeline_event` SHALL carry
`{"auto_retry_skipped": true, "reason": ..., "error_code": ...}` and the
unknown-code case SHALL log a warning. Repo-wide grep shows ZERO
implementations on any plane — all 12 hits are spec/accounting text. Auditors
keying on `auto_retry_skipped` get an empty set forever; distinguishing
"guard-blocked" from "retries exhausted" requires reverse-engineering
non-contract fields. #1161/PR #1311 made `OUT_OF_MEMORY` non-transient, so
the blocked path's traffic (and the audit gap's cost) grew. (Issue #1314.)

## What Changes

- `services/orchestrator/retry.py`: single-source helper
  (`auto_retry_skipped_details(error_code)`) returning
  `reason="non_transient_error"` for codes in `NON_TRANSIENT_ERROR_CODES`,
  else `reason="unknown_error_code_defaulted_non_transient"` for codes on
  neither list; DB-plane `mark_permanently_failed` merges the payload into
  the existing `permanently_failed` event's `details` ONLY when the block is
  classification-caused (a limit-exhausted retryable code must NOT carry the
  key). Unknown-code path logs the spec-required warning.
- `services/orchestrator/file_orchestration_journal.py`
  (`FileJournalRetryService.mark_permanently_failed`): same payload via the
  same helper — no second copy of the reason literals.
- db-free scheduler plane disposition (documented, not coded): the part
  flowing through `FileJournalRetryService` is covered by the file-journal
  sink; the pure-adjudication path (`scheduler_state_failure.py`
  `_failure_policy_payload`) writes no pipeline events by contract
  (`pipeline_event_writes_proven_absent`) and stays sink-free.
- Tests: parameterized across both planes × (full non-transient set + an
  unknown code), limit-exhausted discrimination assertion, spec-parsing
  alignment (reuse the tests/test_retry.py spec-parse pattern), warning-log
  assertion.

Decision (issue left it to implementer; ruled here): REUSE the existing
`permanently_failed` event — the spec requires only "a `pipeline_event` …
with `details_json` containing …", not a new event type; a separate event
would double journal I/O and duplicate semantics at the same instant.

Non-goals: spec.md:234's `manual_retry_already_active` interception (same
family, different trigger point — separate issue per #1314 boundary); any
change to the classification sets; #1161 tasks 5.0 (b)-(e); archived spec
copies.

## Capabilities

### Modified Capabilities

- `job-retry-mechanism`: the Retry Guard requirement gains explicit
  plane-disposition and limit-exhausted-discrimination scenarios matching
  the implementation (event reuse, single-source payload, db-free
  pure-adjudication path declared sink-free).

## Impact

- `services/orchestrator/retry.py` (helper + DB-plane mark path)
- `services/orchestrator/file_orchestration_journal.py` (file-journal plane)
- `tests/test_retry.py`, `tests/test_file_orchestration_journal.py`
- No change: `scheduler_state_failure.py` (disposition only),
  classification sets, event types.
