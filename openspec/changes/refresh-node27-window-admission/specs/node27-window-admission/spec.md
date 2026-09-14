## ADDED Requirements

### Requirement: Exact pending migration admission preserves historical ledger

Window preparation and its migration worker SHALL admit the migration step only when sorted current SQL filenames absent
from the applied ledger equal exactly `000059_river_timeseries_narrow_expand.sql`. Applied entries without current SQL
files SHALL remain intact and SHALL NOT alone cause refusal.

#### Scenario: Legitimately retired SQL remains applied history

- **GIVEN** the ledger includes the seven observed intentionally retired migrations and all current migrations except
  000059
- **WHEN** preparation and the migration worker evaluate admission
- **THEN** both accept the exact pending set, without recreating retired files or modifying historical entries
- **AND** expand adds only 000059 while preserving legacy facts, and D12 retains the entire post-expand ledger including
  000059 and all historical entries, without erasing narrow provenance

#### Scenario: Recovery follows a failure before migration commits

- **GIVEN** the original ledger is L and migration has not committed
- **WHEN** admitted recovery runs
- **THEN** the ledger remains L; recovery neither fabricates nor deletes migration history

#### Scenario: Pending set is not the single expand

- **GIVEN** no migration is pending, or another current migration is also absent from the ledger
- **WHEN** either admission boundary evaluates the operation
- **THEN** it refuses before migration side effects and leaves the original ledger and catalog unchanged

### Requirement: Runtime admission binds an immutable qualified release

The deployment SHALL bind target `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2` across window execution, governance unstage
checks, import-origin probes, restore refs and admission evidence. It SHALL retain OLD and the observed container/device
identities. A driver or input change SHALL require fresh preparation.

#### Scenario: Final target is qualified before T0

- **GIVEN** the historical-ledger child is complete and target runtime is independently staged
- **WHEN** the final-target worker and governance interfaces are qualified
- **THEN** the isolated window matrix passes, the exact selector in `small-selector-oracle.json` returns 168 points with
  SHA-256 `84a95266f402a20d9f26834bba5f16972266f1075e6859798eea37b82738b483`, and the actual read-only governance audit
  has no criticals
- **AND** fresh prepare uses new state and current input/driver hashes without switching the active checkout or invoking
  a production migration

#### Scenario: Target or audit evidence is unsuitable

- **GIVEN** a stale target/ref/hash, or a critical governance recommendation including `AUTOVACUUM_OUTPUT_STALLED`
- **WHEN** admission is evaluated
- **THEN** readiness is refused without filtering the finding, weakening a safety threshold or silently adopting a later
  master

### Requirement: Owned governance handoff preserves existing staged identity

The healthy 1a32 governance pin SHALL remain through the window. Unstage SHALL consume unchanged old staged
arguments/state and verify pin ownership only after active source equals the new target. After owned pin removal and
reload, successful completion SHALL require a fresh ordinary-runtime audit before restoring timer activity.

#### Scenario: Existing pin hands off after validated cutover

- **GIVEN** original hash-verified f24 code produced staged state at retained runtime 1a32 and the independently
  identified active root subsequently reached target 415cbd1e9
- **WHEN** the new tool performs the owned unstage using identical arguments apart from operation
- **THEN** ownership checks pass without rewriting identity/template/digests, only the owned pin is removed, and
  original timer activity is restored after a successful new-runtime audit

#### Scenario: Handoff is interrupted or ownership no longer agrees

- **GIVEN** an interrupted owned unstage, an active runner, or foreign pin/environment/unit drift
- **WHEN** recovery or unstage is requested
- **THEN** interrupted owned work follows the existing recovery protocol, while active-runner and foreign-drift cases
  refuse rather than overwriting foreign state

#### Scenario: Ordinary audit fails after owned pin removal

- **GIVEN** owned unstage removed its pin and reloaded, but the new ordinary-runtime audit fails
- **WHEN** completion or recovery is evaluated
- **THEN** completion is not claimed and timer activity is not restored before a successful audit; durable owned
  recovery remains available

#### Scenario: Cutover has not happened

- **GIVEN** active source is still OLD
- **WHEN** post-cutover pin removal is requested
- **THEN** it refuses and the healthy staged pin is preserved

### Requirement: Child qualification does not claim parent rollout completion

The two child deliveries SHALL preserve the original f24 five-round and final-review lineage and SHALL NOT reset or
disguise that review counter. Their qualification SHALL NOT execute node-22 operations, retention, manual ledger/state
repair or production pin removal, or claim T0 and parent completion.

#### Scenario: Child evidence is ready for parent execution

- **GIVEN** isolated migration/rollback and old-pin/new-active handoff proofs plus read-only final-target audit and
  actual fresh prepare have passed
- **WHEN** the child merge gate is completed
- **THEN** parent #1987 still owns production window execution, immediate post-cutover owned unstage and full task 5.2
  evidence, and #2280 still requires actual uncached four-route validation

### Requirement: Window immutable checks compare stable unit semantics

Window preparation and subsequent immutable checks SHALL retain all stable unit and protected-file checks while
excluding only typed execution metadata from ExecStart and next-elapse from TimersCalendar.

#### Scenario: Normal service execution and timer rescheduling

- **WHEN** path, complete argv, ignore_errors and calendar expressions are unchanged but execution metadata or
  next-elapse advances
- **THEN** immutable configuration comparison succeeds without bypassing source, ledger, ownership or file checks

#### Scenario: Real or unreadable configuration changes

- **WHEN** any stable command/calendar/environment/path/timeout field or protected file changes, or typed property data
  is malformed or unsupported
- **THEN** admission refuses before window mutation; every ordered command and calendar entry remains covered

#### Scenario: Prior snapshot format is presented

- **WHEN** a prior raw-display snapshot is consumed by the changed executor
- **THEN** it refuses with an explicit fresh-state requirement before mutating that state; no phase-only bypass occurs

### Requirement: Display startup waits for bounded actual readiness

The shared forward and recovery startup path SHALL wait for actual local health HTTP200 after submitting service
start, bounded by30 seconds and by remaining forward window/outage budgets where those budgets apply.

#### Scenario: Listener becomes ready after service start returns

- **WHEN** the service is starting and the local listener is temporarily unavailable, then becomes healthy within budget
- **THEN** startup proceeds only after health200 and the existing source/proxy checks, without restarting the service

#### Scenario: Readiness fails or its budget expires

- **WHEN** the service fails, a permanent response error occurs, or actual readiness is not achieved within budget
- **THEN** startup refuses without falsely marking basic_ready or releasing the fence/timers

#### Scenario: Authorized recovery begins after a historical forward deadline

- **WHEN** recovery is otherwise eligible after a prior forward deadline expired
- **THEN** the existing recovery policy remains available with its own bounded readiness attempt and all guards intact
