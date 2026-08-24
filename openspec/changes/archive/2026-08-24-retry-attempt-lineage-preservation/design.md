## Context

The file-journal retry state machine has three coupled defects. Manual retry selects rows through the public scheduler projection and writes them back; URI fields are therefore display placeholders. Contract-current rows additionally carry `accepted_submit_contract_version`, but retry construction clears `candidate_id`/`array_task_id` and changes the job/idempotency identity, so accepted-submit validation rejects both candidate and master rows before any write. Auto retry builds from a narrow caller snapshot and silently normalizes missing `init_state_identities` to `[]`.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Make contract-current candidate and master manual retry deterministic and usable.
- Preserve the exact bounded durable `init_state_identities` lineage across manual and automatic retry attempts.
- Keep redacted display values out of durable retry rows through both manual write phases.
- Return a stable structured 4xx error if retry evidence still violates the journal contract.
- Preserve marker-free and non-forecast retry behavior.

**Non-Goals:**

- Changing accepted-submit master/candidate identity or digest semantics.
- A general journal placeholder-strip guard (#1592), historical repair, auto-retry routing, or candidate freeze semantics.
- Database, Slurm scheduling, frontend, or live deployment changes.

## Decisions

### D1: Retry attempts are not accepted-submit authority rows

Manual retry SHALL remove `accepted_submit_contract_version` from the new retry row. The retry points back through `previous_job_id`, has a new job/idempotency identity, and is not the row that proves the scheduler's accepted-submit decision. This restores pre-contract retry behavior without broadening the accepted-submit validator or inventing retry-specific cohort digests.

Alternative rejected: keep the marker and teach the accepted-submit identity contract about retry IDs. That expands #1112's core invariant, requires a new digest/discriminator policy, and creates a wider compatibility surface for no user benefit.

### D2: Durable writes are derived from private durable rows

`_create_pending_manual_retry_job` SHALL use the selected public row only for retryability/conflict decisions, then load the same `job_id` through the private durable accessor while holding the existing cycle write lock. `_record_manual_retry_submission_success` SHALL likewise update a private durable row. A missing private row fails as retry-not-found rather than writing projection bytes.

### D3: Retry attempts inherit predecessor warm-start lineage

Both manual and automatic retry attempts SHALL preserve the predecessor's bounded durable `init_state_identities` exactly. This field records the warm-start inputs actually used by the attempt; `[]` means no identities, not unknown. `schedule_auto_retry` SHALL source it from the durable current row already loaded by job ID, not from its possibly narrow caller snapshot. `mark_permanently_failed` remains unchanged.

### D4: Journal identity failures become stable retry errors

A `FileOrchestrationJournalError` encountered while constructing the pending retry SHALL be translated at the retry service boundary to `RetryError` code `RETRY_EVIDENCE_INVALID`, HTTP 409, with safe details including `run_id` and the journal field/code. The API's existing `RetryError` mapping remains the only HTTP adapter. No generic global handler is added.

## Risk Packs Considered

- Public API / CLI / script entry: selected — `POST /runs/{run_id}/retry` success and structured failure are observable contracts.
- Config / project setup: not selected — no config changes.
- File IO / path safety / overwrite: selected — private durable journal rows must not be overwritten from redacted projections.
- Schema / columns / units / field names: selected — accepted-submit marker and `init_state_identities` determine row meaning.
- Auth / permissions / secrets: not selected — role and policy checks remain unchanged.
- Concurrency / shared state / ordering: selected — preserve the existing cycle lock and conflict ordering across private rebind.
- Resource limits / large input / discovery: not selected — existing bounded lineage normalization and scoped lookup are unchanged.
- Legacy compatibility / examples: selected — marker-free and non-forecast retry behavior must remain intact.
- Error handling / rollback / partial outputs: selected — invalid evidence must fail before writes with a stable error; submission update must not launder lineage.
- Release / packaging / dependency compatibility: not selected — no dependency or package change.
- Documentation / migration notes: selected — this fixture records retry row identity and lineage semantics; no migration.
- Geospatial / CRS / basin geometry: not selected — no geometry behavior.
- Hydro-met time series / forcing windows: not selected — lineage metadata only, no forcing values or windows.
- SHUD numerical runtime / conservation / NaN: not selected — no solver execution change.
- PostGIS / TimescaleDB domain behavior: not selected — db-free file journal only.
- Slurm production lifecycle / mock-vs-real parity: not selected — submission scheduling contract is unchanged.
- External hydro-met providers / snapshot reproducibility: not selected — no provider access.
- Run manifest / QC provenance: selected — retry warm-start lineage is provenance evidence.
- Published NHMS artifacts / display identity: not selected — no publish/display output change.

## Invariant Matrix

- Governing invariant: every durable retry row records the predecessor's real bounded warm-start lineage, never a public placeholder or false empty value, and never claims accepted-submit authority under an incompatible retry identity.
- Source-of-truth identity/contract: private durable predecessor row keyed by `job_id`; `previous_job_id`; `init_state_identities`; `accepted_submit_contract_version` absence on retry attempts.
- Producers: `_create_pending_manual_retry_job`, `schedule_auto_retry`.
- Validators/preflight: `_pipeline_job_row`, accepted-submit normalization, manual retry policy/conflict checks.
- Storage/cache/query: `_pipeline_job_for_id_unlocked`, direct `pipeline-jobs/*.json`, cycle jsonl.
- Public routes/entrypoints: `FileRetryService.attempt_manual_retry`, `POST /api/v1/runs/{run_id}/retry`.
- Frontend/downstream consumers: unchanged retry API response and retry submission manifest consumers.
- Failure paths/rollback/stale state: missing durable row, active retry conflict, invalid journal evidence, submission failure update.
- Evidence/audit/readiness: durable payload/jsonl assertions, API error response, focused pytest, ruff, strict OpenSpec validation.
- Regression rows:
  - contract-current candidate/master with real lineage -> manual retry succeeds, retry marker absent, durable lineage preserved.
  - invalid retry evidence -> no retry write and structured 409 `RETRY_EVIDENCE_INVALID`.
  - marker-free/non-forecast manual retry and snapshot-shaped auto retry -> prior behavior preserved, with durable lineage inherited when present.

## Boundary Surface Checklist

- Shared helper roots: private/public file-journal row accessors and accepted-submit normalization.
- Public entrypoints: manual retry service and HTTP route.
- Read surfaces: retry source selection, durable job lookup, auto-retry current-row lookup.
- Write surfaces: pending manual retry, successful submission update, auto-retry creation.
- Staging/publish/rollback: pending row before submission and submission-failure terminalization.
- Producer/consumer evidence boundaries: predecessor lineage to retry row and retry submission manifest.
- Stale-state/idempotency: existing cycle lock, active retry conflict, retry job reuse.
- Unchanged consumers: DB retry service, `mark_permanently_failed`, marker-free/non-forecast callers.

## Risks / Trade-offs

- [Retry rows leave accepted-submit protection] → They are attempt rows rather than accepted-submit authority; preserve provenance through `previous_job_id` and dedicated lineage tests.
- [Private rebind could race with selection] → Rebind pending creation under the existing cycle lock and retain current conflict checks.
- [Snapshot caller lacks lineage] → Source auto-retry lineage from the durable current row by job ID; when the durable predecessor truly has none, persist `[]`.
- [Broad exception translation could hide corruption] → Catch only `FileOrchestrationJournalError` at the manual-retry construction boundary and expose its stable code/field safely.

## Migration Plan

No data migration. Deploy code and tests together; rollback is a normal code revert. Historical retry rows are not rewritten.

## Open Questions

None.
