## Context

Issues #1805 and #1804 are one authority-propagation invariant over the db-free file journal. `operator_verified_absence` is durable authority granted only by the dedicated audited demotion, and `operator_recovery_attested_at` is a row-scoped operator credential granted only by the dedicated released-reservation recovery API. Current-contract writers already reject forged authority, and the consuming recovery predicate already requires `status=reservation_lost`; two compatibility producers remain open: the legacy transition/reconciliation APIs can write the decision token, and `_create_pending_manual_retry_job` clones the attestation through `**failed_job`.

Fixture level: `expanded`. Repair intensity: `high`. The change touches persisted state transitions, operator authority, legacy compatibility, and a retry constructor whose output can eventually reach Slurm submission. The active NHMS profile already covers these surfaces; no profile update is required.

## Goals / Non-Goals

**Goals:**

- Keep the dedicated typed operator APIs as the only authority producers.
- Make forged legacy writes fail before lock acquisition or durable mutation with the existing stable error.
- Make every manual-retry successor a fresh, unattested attempt even if a test or future caller selects an attested released predecessor.
- Preserve legitimate legacy decisions, source-row bytes, manual-retry lineage, and typed recovery behavior.

**Non-Goals:**

- Change `MANUAL_RETRY_SOURCE_STATUSES`, make `reservation_lost` manually retryable, or repair other manual-retry issues.
- Change reclaim, automatic retry, the operator CLI, the PostgreSQL plane, Slurm configuration, or scheduler submission policy.
- Introduce a new global write-sink privilege/bypass API.

## Decisions

### D1: Guard legacy authority at the semantic writer entries

`transition_pipeline_job_submit_evidence` in legacy mode and `record_pipeline_job_reconciliation` SHALL reject the operator decision with `file_journal_authority_transition_requires_typed_api`, field `reconciliation_decision`, before acquiring the cycle lock or calling `apply_accepted_submit_transition`. The existing versioned transition gate remains unchanged.

A guard in `apply_accepted_submit_transition` or `_write_pipeline_job_unlocked` is rejected: both are shared by legitimate typed authority paths and would require a privileged bypass flag. That trades two explicit compatibility gates for a global capability switch and makes the closed world harder to audit.

### D2: Strip the attestation at the manual-retry clone boundary

`_create_pending_manual_retry_job` SHALL explicitly set `OPERATOR_RECOVERY_ATTESTATION_FIELD` to `None` alongside the other attempt-scoped fields it clears. This is the narrow ownership boundary where an existing row becomes a distinct manual-retry row. The source row remains unchanged.

The shared journal sink is rejected as the owner because it cannot distinguish a legitimate typed update of the released source row from an illegitimate inherited credential without a bypass. Sibling constructors are audited rather than modified: auto retry copies through the closed `_file_retry_job_record` field set, which does not include the attestation, and the ordinary forecast recovery creates a clean reservation already covered by existing tests.

### D3: Keep both reachability gates and consumer defense

`reservation_lost` remains outside `MANUAL_RETRY_SOURCE_STATUSES`; a regression test makes this a deliberate contract rather than incidental reachability. A forced-source test bypasses that gate and proves the successor still clears the credential. The existing `_operator_recovery_attested` status check remains defense in depth and is exercised against the successor.

### D4: Preserve compatibility by testing both negative and positive pairs

The legacy authority tests compare journal bytes and events before/after rejection, then drive an existing legal legacy decision through each compatibility API. Manual-retry tests preserve durable lineage, predecessor identity, typed recovery behavior, and the source attestation while checking only the new successor field contract.

## Selected Risk Packs

- Public API / CLI / script entry: selected — two repository compatibility methods are callable write boundaries; signatures and legal callers remain compatible.
- Config / project setup: not selected — no configuration or setup changes.
- File IO / path safety / overwrite: selected — refusals must leave append-only journal bytes and events unchanged; no path semantics change.
- Schema / columns / units / field names: selected — two persisted authority fields and their row-scoped meaning are the subject.
- Auth / permissions / secrets: selected — operator tokens are authorization credentials even though this is not HTTP auth.
- Concurrency / shared state / ordering: selected — rejection must happen before the cycle lock/write, and successor construction occurs under the existing lock.
- Resource limits / large input / discovery: not selected — bounded single-row operations only.
- Legacy compatibility / examples: selected — legitimate marker-free decisions and retry lineage must remain unchanged.
- Error handling / rollback / partial outputs: selected — stable typed error, byte-identical refusal, zero events, and no source-row mutation.
- Release / packaging / dependency compatibility: not selected — no dependency or package surface.
- Documentation / migration notes: selected — OpenSpec is updated; no operator migration is required.
- Geospatial / CRS / basin geometry: not selected — no spatial data.
- Hydro-met time series / forcing windows: not selected — no forcing data or time-window arithmetic.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — db-free path.
- Slurm production lifecycle / mock-vs-real parity: selected — forged authority could reopen submission, but deterministic state-machine tests are the oracle because no scheduling code changes.
- External hydro-met providers / snapshot reproducibility: not selected — no provider boundary.
- Run manifest / QC provenance: selected — operator provenance must remain bound to its original durable row and not a retry successor.
- Published NHMS artifacts / display identity: not selected — no publication/display surface.

## Invariant Matrix

- Governing invariant: operator authority may be minted only by its dedicated typed API and may never be forged by compatibility writers or inherited by a distinct retry attempt.
- Source-of-truth identity/contract: `operator_verified_absence`, `operator_recovery_attested_at`, the durable source `job_id`, and the distinct retry `job_id`/`previous_job_id` pair.
- Producers: typed operator demotion and released-reservation recovery; legacy transition/reconciliation APIs and manual-retry clone are the forbidden producers under test.
- Validators/preflight: legacy API guards, `MANUAL_RETRY_SOURCE_STATUSES`, `_pipeline_job_row`, and accepted-submit normalization.
- Storage/cache/query: append-only cycle journal, direct pipeline-job projection, and public retry-source query.
- Public routes/entrypoints: `transition_pipeline_job_submit_evidence`, `record_pipeline_job_reconciliation`, and `FileJournalRetryService._create_pending_manual_retry_job`; no new CLI/API.
- Frontend/downstream consumers: reclaim and `_operator_recovery_attested`; no frontend.
- Failure paths/rollback/stale state: forged legacy token fails pre-lock and byte-identically; forced attested predecessor yields an unattested successor without changing the source.
- Evidence/audit/readiness: focused regressions, full file-journal test module, Ruff, strict OpenSpec validation, local and node-27 db-free pytest.
- Regression rows:
  - marker-free legacy row + ordinary legal decision -> existing transition/reconciliation behavior remains applied.
  - marker-free legacy row + `operator_verified_absence` -> typed-authority error, byte-identical journal, zero events.
  - attested `reservation_lost/identity_mismatch_released` source under ordinary selection -> not selected for manual retry.
  - same source forced through the clone seam -> successor has no attestation and fails `_operator_recovery_attested`; source retains attestation.
  - dedicated typed recovery API and unchanged ordinary failed-row manual retry -> existing behavior remains green.

## Boundary-Surface Checklist

- Shared helper roots: accepted-submit transition application and journal sink are inspected, intentionally unchanged.
- Public entrypoints: both legacy compatibility writers guarded.
- Read surfaces: manual-retry source selection pinned.
- Write/delete/overwrite surfaces: two legacy writes reject before mutation; clone clears only the successor field.
- Producer/consumer evidence boundaries: typed authority producer -> durable source row -> retry clone -> recovery predicate.
- Stale-state/idempotency boundaries: source row and existing legal decisions remain unchanged.
- Unchanged downstream consumers: reclaim, typed demotion/recovery, auto retry, PostgreSQL, and Slurm submission.

## Risks / Trade-offs

- [A future clone path repeats the inheritance bug] -> audit sibling constructors now; retain the credential in the closed row constructor because legitimate typed source-row replay requires it.
- [A broad sink guard blocks the typed API] -> keep checks at semantic writer/clone boundaries; do not add a global bypass.
- [Tests prove only current unreachability] -> include a forced-source clone test independent of `MANUAL_RETRY_SOURCE_STATUSES`.
- [A narrow rejection breaks legacy accounting] -> add legal-decision positive controls for both compatibility APIs.

## Migration Plan

No data migration is needed. Deploy as a code-only hardening change. Rollback restores the prior latent behavior and does not require journal rewriting because the new paths either reject before writing or clear a field only on newly created retry rows.

## Open Questions

None. Both issues are implementation-ready, and the narrow semantic owners are present in current master.
