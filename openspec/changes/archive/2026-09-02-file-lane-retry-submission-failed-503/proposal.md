# File-lane manual retry submission failure surfaces as the route's 503, not an unclassified 500

## Why

`POST /api/v1/runs/{run_id}/retry` (`apps/api/routes/pipeline.py:506`
`retry_run`) has one `submission_failed` arm: it builds the structured 503
(`error.code`, redacted `error_message`, `details.status/run_id/job_id`,
optional `details.runtime_root_resolution`) and, at `:545`, reads the evidence
through `context.service.submission_runtime_root_resolution(job.job_id)`. That
method exists only on the DB lane (`services/orchestrator/retry.py:713`
`RetryService.submission_runtime_root_resolution`). The file lane
`FileJournalRetryService` (`services/orchestrator/file_orchestration_journal.py:10554`)
has no such method, no base class and no `__getattr__`, so a real submission
failure on the file lane — `attempt_manual_retry`'s `except Exception`
(`:11238`) records the failure through `_record_manual_retry_submission_failure`
(`:11667`) and returns a `submission_failed` row — reaches `:545` and dies with
`AttributeError`, i.e. the unclassified HTTP 500 the file-lane retry spec
forbids (`openspec/specs/job-retry-mechanism/spec.md`, "rather than an
unclassified HTTP 500", pinned today only for the 409 evidence-invalid arm).
Reproduced end to end by issue #1945 with no monkeypatch; the journal side is
correct (the failure event is durable with `error_code` and, when resolved, the
`_public_evidence`-scrubbed `runtime_root_resolution`), only the HTTP surface
collapses. Line cites are against `origin/master` `af394fd1`; symbol names are
authoritative.

Exposure: latent. The default DI (`pipeline.py:132 get_retry_service`) builds
only the DB lane and nothing under `apps/` imports the file lane, so the public
display API cannot reach this path today. But the route is treated as lane-
agnostic by spec and by the repo's own tests (the 409 test injects
`FileJournalRetryService` into `_RetryExecutionContext` under a
`# type: ignore[arg-type]`), and any db-free display wiring turns every real
submission failure into a 500 with zero structured attribution. No route-level
test covers the file-lane 503 arm: the three existing 503 assertions
(`tests/test_retry.py:1783/1850/2308`) all build `RetryService`, and the one
file-lane route test's gateway asserts it is never reached.

## What changes

1. **File-lane reader (D1).** `FileJournalRetryService.submission_runtime_root_resolution(job_id)`
   with the DB lane's latest-first semantics over the job's own `submission`
   events, returning the persisted (already `_public_evidence`-scrubbed)
   `runtime_root_resolution` mapping or `None`; a typed journal read fault on
   this second read yields `None` (evidence absent, 503 shape intact) instead
   of a second 500 collapse.
2. **One seam for both lanes (D2).** A `@runtime_checkable` `Protocol` beside
   `RetrySubmitter` in `retry.py` naming the two methods the route calls;
   `_RetryExecutionContext.service` and `get_retry_execution_context` are
   annotated with it; the `# type: ignore[arg-type]` on the 409 test's
   `service=` goes away; an `isinstance` pin holds both concrete lanes to it.
3. **Route-level regression tests** for the file lane: 503 with evidence
   present (response equals the persisted event details), 503 with evidence
   absent (key absent, not `null`), 503 when the second read faults, plus the
   existing redaction assertions. DB-lane 503 tests unchanged.

## Non-goals

- `attempt_manual_retry`'s `except Exception` swallow semantics and the
  `submission_failed` persistence shape (journal side is correct today).
- The 409 / `RETRY_EVIDENCE_INVALID` mapping.
- Wiring the file lane into the production API (`get_retry_service` keeps
  returning the DB lane); a deployment decision, not a contract one.
- The alternative of carrying `runtime_root_resolution` on the
  `attempt_manual_retry` return namespace (changes the DB lane's return
  contract; evidence would ride memory instead of the durable surface).
- Widening either lane's redaction: both lanes' evidence is `redact_payload`-ed
  at construction; the file lane additionally applies `_public_evidence` at
  write, the DB lane re-applies `_redacted_mapping` at read. This change adds
  no third layer. The DB lane's read side returning absolute local roots
  verbatim is a pre-existing surface, routed as #1961, not patched here.

## Issues

Closes #1945. Origin: PR #1939 Phase 7 side finding, pre-existing at master.
The issue carried no upstream `Suggested fixture level` (not a
stage-change-pipeline issue); triaged here as `compact`.
