## ADDED Requirements

### Requirement: Explicit whole-cluster maintenance authority

The relocation CLI SHALL default to read-only planning. Mutating actions MUST require explicit enforce authority, durable operation identity and serialized lifecycle ownership. Preparation MUST preserve the actual application runtime and original unit/container configuration, fence writers persistently, drain work and cleanly stop the source database before physical copying.

#### Scenario: Default planning against an active primary
- **WHEN** an operator runs the default action without enforce
- **THEN** the command reports observations and missing prerequisites without stopping services, changing mounts or creating a replacement database

#### Scenario: Interruption during maintenance preparation
- **WHEN** preparation stops after recording intent or fencing units
- **THEN** a new process observes the actual owned state and either safely restores the original or reports recovery required without silently resuming writers

### Requirement: Closed transition and durable recovery authority

The tool MUST enforce the following closed action table. Every unlisted state/action pair MUST refuse without a mutation. In-progress intent is not a completed predecessor; `plan` only reports it and `rollback` may restore it only after proving the actual recorded ownership.

| Action | Required predecessor and result |
|---|---|
| plan | Any state; observations only, no state advance or mutation. |
| prepare | New operation only; validated admission and successful fencing/drain/clean stop produce `prepared`. Interrupted prepare requires rollback/recovery, not replay. |
| copy | `prepared`, frozen source stopped and clean, persistent fences held; flushed whole-tree equality produces `copy_verified`. |
| activate | `copy_verified`, unchanged source/copy identities and fences; matching candidate plus successful read and rejected business-write proof produce `activated_readonly`. |
| rollback | Any owned pre-release prepare/copy/activation state, including incomplete intent; restore only after re-observing recorded IDs/config/path/fences, then `rolled_back`. Unknown ownership reports recovery required. |
| release | `activated_readonly` plus fresh candidate/read-only proof; persist the stale-snapshot marker first, then lift database write rejection and restore writer scheduling, producing `released`. A marked but interrupted release may continue only this release sequence after revalidation. |

Preparation MUST freeze the original Docker container ID, configuration digest, resolved image, original restart policy, PGDATA path identity, and original unit states/enablement. Recovery MUST use those identities, not assume the container currently named `nhms-db` is the original. An unrecorded or changed candidate MUST NOT be removed or treated as owned merely because its name matches. The original restart policy MUST be disabled before a replacement can exist.

The private `state.json` and write-release marker MUST live in an operator-supplied mode-0700 durable workspace, with mode-0600 atomic, file-and-parent-directory-synced records. `/tmp`, `/run`, tmpfs and the lifecycle flock are not durable journal locations. The canonical `/tmp/nhms-node27-timeseries-lifecycle.lock` remains only in-process exclusion.
The release marker SHALL be the monotonic `writes_released` field in authoritative `state.json`, not a separately trusted flag; error and recovery records MUST preserve it once true.

Persistent operation-owned fences MUST cover the service/timer pairs for `nhms-node27-{autopipe,download,frontier-alert,raw-retention,resource-governance,timeseries-compression,timeseries-retention}`, plus `nhms-node27-timeseries-compression-replay.service` and `nhms-display-api.service`. Missing units are recorded as absent, not installed. Only the display fence may be removed during read-only activation; business fences survive until rollback or release. Unknown writers and an enabled incompatible cold-residency lane refuse preparation. Unrelated units and the existing original-runtime pin dropins MUST remain untouched.

#### Scenario: Activation or release is requested out of order
- **WHEN** activation lacks `copy_verified`, or a first release with `writes_released=false` lacks the current `activated_readonly` proof
- **THEN** the action refuses before changing the container, database writability or unit fences

#### Scenario: Marked interrupted release continues forward only
- **WHEN** `writes_released=true` and a crash followed HBA restoration but preceded complete writer-fence restoration
- **THEN** release may finish only the remaining owned HBA/fence restoration after fresh identity checks, without requiring obsolete read-only proof and without permitting rollback to the original

#### Scenario: Reboot follows a stop or rename interruption
- **WHEN** the process and machine restart after stopping or renaming the original
- **THEN** the new process reads the durable journal, identifies the original by frozen Docker ID, and either restores that exact original or reports recovery required while writers remain fenced and no second primary starts

### Requirement: Complete verified offline physical copy

The tool MUST copy the complete clean-stopped cluster using the same resolved image, preserving data and required filesystem metadata. It MUST require fresh root-bound RAID and both-member SMART evidence, capacity/reserve, safe owned target identities and complete source coverage. Uncovered external tablespaces/WAL, unsafe paths, unclean shutdown, unknown ownership or source drift MUST refuse activation. Copy verification SHALL be streamed and include the whole tree; original and partial-copy data SHALL remain preserved.

#### Scenario: Copy includes compressed and warm data
- **WHEN** an admitted stopped fixture contains normal tables, warm and compressed chunks, roles and migration metadata
- **THEN** its verified copy preserves their contents and identities without logical reload or index rebuild

#### Scenario: Partial or unsafe copy
- **WHEN** a copy is interrupted, checksum differs, health is unavailable, or a source/target path is unsafe or changed
- **THEN** activation is refused and neither the original cluster nor an unrelated directory is deleted

### Requirement: Exact deployment rebind and pre-write rollback

Activation MUST change only the permanent PGDATA host bind while preserving resolved image, effective environment/command, ports, ownership, unrelated mounts and resource limits. The exact original container and data directory MUST remain stopped and retained. Before serving reads, the candidate MUST enforce a temporary operation-owned database admission fence that rejects business-writer connections/writes, including an explicitly requested read-write transaction, until release. A successful SELECT or a changeable default read-only GUC alone is not sufficient. Activation readiness MUST prove a real read succeeds and a representative business write is rejected. The trusted container-local maintenance administrator is outside the business channel and MUST be used only for bounded read-only validation before release.

The temporary admission fence may change the copied HBA file after complete-copy verification; its exact original bytes and operation-owned replacement digest MUST be recorded privately. This is a declared temporary maintenance delta, not a permanent auth/config change. The original cluster's HBA remains untouched; release restores the candidate's exact original HBA only after the durable stale-snapshot marker. Uncertain fence ownership or unexpected connected writers MUST refuse readiness.

#### Scenario: Copied cluster activates without a cold bind
- **WHEN** a verified copy is activated
- **THEN** the expected database is served through the original port and image with the new PGDATA bind, no added cold tablespace bind and no application/schema upgrade

#### Scenario: Pre-write restoration
- **WHEN** rollback is requested before write release and original/candidate identities still match
- **THEN** the exact original container/configuration and original service/timer state are restored without deleting either data directory

#### Scenario: A business client attempts a write before release
- **WHEN** the read-only candidate is serving display reads and an ordinary ingest credential attempts to connect or explicitly start a read-write transaction and insert data
- **THEN** database admission or authorization rejects it, no business data changes, and pre-release restoration does not discard an accepted write

### Requirement: Durable first-write boundary

The tool MUST durably mark write release before removing the database admission fence, starting controlled ingest, or allowing any other business writer to resume. After release, it MUST refuse switchback to the stale original even if a crash makes actual write completion uncertain. Post-release recovery MUST use current consistent data rather than the original snapshot.

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
