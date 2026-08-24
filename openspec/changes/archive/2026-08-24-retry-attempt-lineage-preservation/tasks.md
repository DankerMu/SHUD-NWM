Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Issues: #1604, #1605, #1606

## 1. Retry Identity and Durable Source

- [x] 1.1 Rebind manual-retry source creation and successful submission updates to private durable rows while preserving the existing cycle lock, retryability selection, and conflict ordering.
- [x] 1.2 Define manual retry attempts as non-authoritative accepted-submit rows by removing `accepted_submit_contract_version` from the new attempt while retaining `previous_job_id`.
- [x] 1.3 Preserve the predecessor's bounded durable `init_state_identities` across both manual retry write phases without persisting `[object-uri]`, `[local-path]`, or `[redacted]` placeholders.
- [x] 1.4 Make `schedule_auto_retry` inherit `init_state_identities` from the durable current row for full-row and narrow snapshot callers; keep `mark_permanently_failed` unchanged.

## 2. Stable Failure Boundary

- [x] 2.1 Translate `FileOrchestrationJournalError` during pending manual-retry construction into a `RetryError` family error with HTTP 409, code `RETRY_EVIDENCE_INVALID`, and safe run/code/field details before mutation.
- [x] 2.2 Lock the HTTP contract: `POST /api/v1/runs/{run_id}/retry` returns structured 409 plus `error.code`, never an unclassified 500 or private evidence.

## 3. Evidence Floor

- [x] 3.1 Candidate and master contract-current fixtures each prove manual retry succeeds, writes no accepted-submit marker, preserves real durable lineage in direct payload and jsonl, and does not raise bare `FileOrchestrationJournalError`.
- [x] 3.2 Independently drive `_record_manual_retry_submission_success` after restoring a pending retry row's real lineage; prove the second write cannot launder it.
- [x] 3.3 Marker-free and non-forecast manual retry fixtures each start with real durable `init_state_identities`; assert existing eligibility/status/retry identity plus exact mapping inheritance in the direct payload and corresponding jsonl, with no `[object-uri]`, `[local-path]`, or `[redacted]`.
- [x] 3.4 Concurrent manual retry fixture synchronizes two calls for one failed `run_id`; assert exactly one retry payload and retry event, with the other call returning the existing `RetryConflictError` result.
- [x] 3.5 Public selection/private rebind disappearance fixture makes the public failed source selectable while the private accessor returns `None`; assert `RetryNotFoundError` and no new direct payload, jsonl record, or retry event.
- [x] 3.6 Invalid private evidence proves no retry payload/jsonl/event is written and maps through the service/API to `RETRY_EVIDENCE_INVALID`.
- [x] 3.7 Auto-retry full-durable-row and narrow production snapshot fixtures independently assert the same mapping inherited from their durable predecessors in direct payload and jsonl; also cover contract-current candidate shape, marker-free shape, genuinely empty lineage as `[]`, and unchanged mapping under `mark_permanently_failed`.
- [x] 3.8 Batched mutation proof: revert the retry marker/source/lineage changes together against the new tests, run once red, restore immediately, rerun green, and leave no `red-proof` stash.

## 4. Verification and Delivery

- [x] 4.1 Run `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_retry.py tests/test_gateway_reconcile.py` with all tests passing.
- [x] 4.2 Run `uv run ruff check .` with zero findings.
- [x] 4.3 Run `openspec validate retry-attempt-lineage-preservation --strict --no-interactive` successfully.

## Must Preserve

- Accepted-submit candidate/master identity and cohort-digest validation for authoritative rows.
- Existing retry role/policy checks, conflict detection, retry counters, gateway submission, and marker-free/non-forecast behavior.
- Existing bounded lineage normalization and public URI redaction.
- DB retry service, Slurm scheduler behavior, and `mark_permanently_failed` semantics.

## Selected Risk Packs and Evidence Mapping

- Public API / CLI / script entry: selected — tasks 2.2, 3.3-3.6, 4.1.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: selected — tasks 1.1, 1.3, 3.1-3.7.
- Schema / columns / units / field names: selected — tasks 1.2-1.4, 3.1, 3.3, 3.7.
- Auth / permissions / secrets: not selected — authorization unchanged; safe error payload checked in 2.2.
- Concurrency / shared state / ordering: selected — tasks 1.1, 3.4, and focused regression suite.
- Resource limits / large input / discovery: not selected — existing bounded/scoped behavior unchanged.
- Legacy compatibility / examples: selected — tasks 3.3, 3.7.
- Error handling / rollback / partial outputs: selected — tasks 2.1-2.2, 3.4-3.6.
- Release / packaging / dependency compatibility: not selected — no dependency change.
- Documentation / migration notes: selected — proposal/design/spec; no migration.
- Geospatial / CRS / basin geometry: not selected — untouched.
- Hydro-met time series / forcing windows: not selected — no forcing data behavior.
- SHUD numerical runtime / conservation / NaN: not selected — untouched.
- PostGIS / TimescaleDB domain behavior: not selected — db-free journal.
- Slurm production lifecycle / mock-vs-real parity: not selected — scheduling unchanged.
- External hydro-met providers / snapshot reproducibility: not selected — untouched.
- Run manifest / QC provenance: selected — tasks 1.3-1.4 and durable evidence in 3.1-3.3, 3.7.
- Published NHMS artifacts / display identity: not selected — untouched.

## Seams Under Test

- `FileRetryService.attempt_manual_retry` and its pending/success durable writes.
- `FileRetryService.schedule_auto_retry` with durable predecessor lookup.
- Direct `pipeline-jobs/<job_id>.json` payload and corresponding jsonl replay records.
- `POST /api/v1/runs/{run_id}/retry` error response.

## Non-Goals

- General write-side placeholder stripping (#1592), historical backfill, accepted-submit contract expansion, retry routing changes, or live Slurm/DB/display deployment.
