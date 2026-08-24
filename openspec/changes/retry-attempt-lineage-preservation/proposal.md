## Why

Contract-current forecast rows cannot be retried manually because the retry constructor copies the accepted-submit marker while erasing its row discriminator. The adjacent manual- and auto-retry constructors also derive durable rows from redacted or incomplete projections, so restoring retry without fixing them would either persist `[object-uri]` or falsely record empty warm-start lineage.

## What Changes

- Restore manual retry for contract-current candidate and master forecast rows by defining retry attempts as non-authoritative accepted-submit rows.
- Rebind manual retry creation and submission updates to private durable rows, never public redacted projections.
- Preserve the predecessor's bounded `init_state_identities` on manual and automatic retry attempts.
- Convert file-journal retry identity failures into a stable structured 409 response instead of an unclassified HTTP 500.
- Add durable-payload and API regression coverage for contract-current, marker-free, and snapshot-shaped inputs.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `job-retry-mechanism`: Define retry-attempt identity, durable warm-start lineage inheritance, and structured invalid-evidence rejection.

## Impact

- Runtime: `services/orchestrator/file_orchestration_journal.py`, retry error types, and the existing retry API mapping.
- Tests: file-journal retry state, retry API, and accepted-submit compatibility coverage.
- No schema, database migration, Slurm scheduling, or frontend change; local db-free pytest is the runtime oracle.

Closes #1604, #1605, and #1606.
