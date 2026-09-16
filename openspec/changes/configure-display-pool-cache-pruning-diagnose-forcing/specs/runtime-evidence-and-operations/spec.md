## ADDED Requirements

### Requirement: Evidence-bound display pool configuration mitigation

A display pool capacity mitigation SHALL be admitted using current database capacity and effective worker count, preserve the display read-only boundary and running code identity, and record configuration, restart, smoke and rollback evidence without claiming cold-generation isolation.

#### Scenario: Capacity-admitted pool increase

- **WHEN** measured connection budget admits 8 base plus 8 overflow connections per existing worker
- **THEN** the effective display configuration uses those values after controlled restart
- **AND** database connection counts before/after, root/catalog/river/discharge HTTP 200 responses and preserved read-only boundary are recorded
- **AND** the remaining cold-generation isolation work stays open

#### Scenario: Unsafe restart or insufficient budget

- **WHEN** code identity is ambiguous, unrelated checkout changes would deploy, or measured connection capacity is insufficient
- **THEN** the operation refuses the restart and records the prerequisite rather than claiming mitigation success

### Requirement: PNG retention configuration lane evidence

The PNG retention cache root SHALL match the display cache root. Activation SHALL preserve existing retention watermarks, deletion safety and canonical copyback lock ownership, and SHALL report PNG lane results independently of sibling lanes.

#### Scenario: Configured PNG pruning tick

- **WHEN** the missing cache-root key is supplied and an inspected plan is followed by a normal production tick
- **THEN** precip_cache_root is non-null, configured source directories are safely evaluated, all PNG unavailable/unsafe skips are absent, and PNG planned/deleted/failed counts are recorded
- **AND** root-unconfigured, root-missing, root-unsafe, source-unsafe or other unavailable/unsafe PNG skips prevent an activation success claim
- **AND** zero expired PNG candidates after actual lane evaluation is reported as zero rather than fabricated deletion evidence

#### Scenario: Canonical lock remains unavailable

- **WHEN** the canonical lane rejects its existing owner-mismatched lock
- **THEN** that failure remains fail-closed and is attributed to #2360 separately from PNG execution
- **AND** the lock mode, ownership and safety guard are unchanged

### Requirement: Upstream forcing diagnosis without recovery claims

Upstream diagnosis SHALL connect a concrete source/cycle candidate to its active forcing provenance and producer failure evidence using read-only operations, distinguishing observed cause from hypotheses and downstream symptoms.

#### Scenario: Missing forcing witness diagnosis

- **WHEN** a scheduler candidate is blocked by FORCING_VERSION_ROW_ABSENT
- **THEN** diagnosis records an executed failing diagnostic signal, active backend/provenance tier, discriminating evidence and root cause or exact unresolved prerequisite
- **AND** it does not infer a missing PostgreSQL row from that classifier in a file backend
- **AND** it neither submits work nor alters the compute environment, and does not claim pipeline recovery
