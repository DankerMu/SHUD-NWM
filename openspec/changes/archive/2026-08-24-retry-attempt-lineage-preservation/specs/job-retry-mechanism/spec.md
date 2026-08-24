## ADDED Requirements

### Requirement: File-journal retry attempts preserve durable warm-start lineage without claiming accepted-submit authority

The file-journal retry service SHALL derive every retry attempt from the private durable predecessor row. A manual or automatic retry attempt SHALL preserve that row's bounded `init_state_identities` exactly, SHALL identify the predecessor through `previous_job_id`, and SHALL NOT carry `accepted_submit_contract_version` because a retry attempt is not the authoritative accepted-submit row. Public redaction placeholders such as `[object-uri]`, `[local-path]`, and `[redacted]` MUST NOT be persisted as retry lineage.

#### Scenario: Contract-current candidate manual retry preserves lineage

- **WHEN** a failed contract-current forecast candidate with a real durable `init_state_identities` mapping is manually retried
- **THEN** the new durable retry row SHALL be created and submitted with the same bounded mapping, `previous_job_id` SHALL identify the candidate, and `accepted_submit_contract_version` SHALL be absent
- **THEN** neither the direct payload nor its journal record SHALL contain a public redaction placeholder in the mapping

#### Scenario: Contract-current master manual retry preserves lineage

- **WHEN** a failed or partially failed contract-current forecast master with real durable lineage is manually retried
- **THEN** the new durable retry row SHALL be created and submitted with that lineage and without `accepted_submit_contract_version`
- **THEN** accepted-submit candidate/master structural validation SHALL NOT reject the retry identity

#### Scenario: Successful manual submission update remains private

- **WHEN** a pending manual retry row with real durable lineage is updated after Slurm submission
- **THEN** the service SHALL update the private durable row and preserve the lineage exactly
- **THEN** no value obtained only from the public scheduler projection SHALL be written back

#### Scenario: Concurrent manual retry has one durable winner

- **WHEN** two callers synchronously attempt manual retry for the same failed `run_id`
- **THEN** exactly one caller SHALL create one retry payload and one retry event
- **THEN** the other caller SHALL receive the existing `RetryConflictError` result without creating a second payload or event

#### Scenario: Selected public source disappears before private rebind

- **WHEN** public retry selection identifies a failed source but the private durable lookup for that `job_id` returns no row
- **THEN** the service SHALL raise `RetryNotFoundError`
- **THEN** no retry payload, journal record, or retry event SHALL be written

#### Scenario: Automatic retry inherits durable predecessor lineage

- **WHEN** `schedule_auto_retry` receives either a full row or a narrow production snapshot for a failed job whose durable row has non-empty `init_state_identities`
- **THEN** the new durable retry payload and journal record SHALL inherit the durable predecessor mapping exactly
- **THEN** a predecessor that genuinely has no mapping SHALL produce `[]` without crashing

#### Scenario: Full-row and narrow-snapshot automatic retry agree

- **WHEN** automatic retry is invoked once with a full durable-row input and once with a narrow production snapshot for equivalent failed jobs whose private durable predecessors carry the same non-empty mapping
- **THEN** both retry attempts SHALL persist the same inherited mapping in their direct payload and corresponding journal record
- **THEN** a predecessor with genuinely empty lineage SHALL still persist `[]`

#### Scenario: Legacy and sibling behavior remains compatible

- **WHEN** marker-free or non-forecast failed rows with real durable lineage are manually retried
- **THEN** their existing eligibility, retry identity, and status behavior SHALL remain unchanged
- **THEN** their direct retry payload and corresponding journal record SHALL inherit the exact mapping without `[object-uri]`, `[local-path]`, or `[redacted]`
- **WHEN** an existing failed row with a non-empty mapping is marked permanently failed
- **THEN** that sibling path SHALL retain its existing mapping

### Requirement: Invalid file-journal retry evidence has a structured API boundary

File-journal identity or evidence validation failures encountered while creating a manual retry SHALL fail before a retry row is written and SHALL be exposed as a stable `RetryError` family result. The monitoring API SHALL return HTTP 409 with code `RETRY_EVIDENCE_INVALID` and safe details rather than an unclassified HTTP 500.

#### Scenario: Invalid retry evidence is rejected before mutation

- **WHEN** private durable retry source evidence cannot satisfy file-journal normalization
- **THEN** manual retry SHALL raise `RetryError` code `RETRY_EVIDENCE_INVALID` with the affected `run_id` and stable journal field/code in safe details
- **THEN** no pending retry payload or retry event SHALL be written

#### Scenario: Monitoring API maps invalid retry evidence

- **WHEN** `POST /api/v1/runs/{run_id}/retry` encounters that retry evidence error
- **THEN** the response SHALL have status 409 and `error.code == "RETRY_EVIDENCE_INVALID"`
- **THEN** the response SHALL not expose secrets, private URIs, or a raw runtime traceback
