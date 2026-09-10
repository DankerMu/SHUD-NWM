## ADDED Requirements

### Requirement: Explicit whole-cluster maintenance authority

The relocation CLI SHALL default to read-only planning. Mutating actions MUST require explicit enforce authority, durable operation identity and serialized lifecycle ownership. Preparation MUST preserve the actual application runtime and original unit/container configuration, fence writers persistently, drain work and cleanly stop the source database before physical copying.

#### Scenario: Default planning against an active primary
- **WHEN** an operator runs the default action without enforce
- **THEN** the command reports observations and missing prerequisites without stopping services, changing mounts or creating a replacement database

#### Scenario: Interruption during maintenance preparation
- **WHEN** preparation stops after recording intent or fencing units
- **THEN** a new process observes the actual owned state and either safely restores the original or reports recovery required without silently resuming writers

### Requirement: Complete verified offline physical copy

The tool MUST copy the complete clean-stopped cluster using the same resolved image, preserving data and required filesystem metadata. It MUST require fresh root-bound RAID and both-member SMART evidence, capacity/reserve, safe owned target identities and complete source coverage. Uncovered external tablespaces/WAL, unsafe paths, unclean shutdown, unknown ownership or source drift MUST refuse activation. Copy verification SHALL be streamed and include the whole tree; original and partial-copy data SHALL remain preserved.

#### Scenario: Copy includes compressed and warm data
- **WHEN** an admitted stopped fixture contains normal tables, warm and compressed chunks, roles and migration metadata
- **THEN** its verified copy preserves their contents and identities without logical reload or index rebuild

#### Scenario: Partial or unsafe copy
- **WHEN** a copy is interrupted, checksum differs, health is unavailable, or a source/target path is unsafe or changed
- **THEN** activation is refused and neither the original cluster nor an unrelated directory is deleted

### Requirement: Exact deployment rebind and pre-write rollback

Activation MUST change only the PGDATA host bind while preserving resolved image, effective environment/command, ports, ownership, unrelated mounts and resource limits. The exact original container and data directory MUST remain stopped and retained. Business writers MUST remain fenced until explicit write release; startup, cluster identity and read-only access MUST be checked before activation readiness.

#### Scenario: Copied cluster activates without a cold bind
- **WHEN** a verified copy is activated
- **THEN** the expected database is served through the original port and image with the new PGDATA bind, no added cold tablespace bind and no application/schema upgrade

#### Scenario: Pre-write restoration
- **WHEN** rollback is requested before write release and original/candidate identities still match
- **THEN** the exact original container/configuration and original service/timer state are restored without deleting either data directory

### Requirement: Durable first-write boundary

The tool MUST durably mark write release before controlled ingest or any other business writer can resume. After release, it MUST refuse switchback to the stale original even if a crash makes actual write completion uncertain. Post-release recovery MUST use current consistent data rather than the original snapshot.

#### Scenario: Crash during write release
- **WHEN** the release marker is persisted and the process exits before writer restoration completes
- **THEN** stale rollback remains forbidden and remaining recovery follows the post-release boundary

### Requirement: Placement-aware observation and truthful evidence

Current PGDATA bytes MUST be attributed to their configured observed storage root without also charging `/home`; existing `/home` placement and cold-installer behavior MUST remain compatible. Disposable validation MUST use isolated identities and MUST NOT be represented as production HDD performance or rollout acceptance. Secrets MUST NOT appear in public receipts or errors.

#### Scenario: PGDATA is relocated to the large volume
- **WHEN** current PGDATA is observed under `/data/GHDC` and the old directory remains retained
- **THEN** its current-PGDATA sample is assigned only to the large-volume observation and the old copy is not treated as the live cluster

#### Scenario: Disposable evidence is presented as production approval
- **WHEN** an isolated rehearsal succeeds without the later live SQL/API/browser/ingest gates
- **THEN** its result proves migration mechanics only and cannot authorize production cutover or deletion of the old copy
