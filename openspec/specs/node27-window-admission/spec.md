# node27-window-admission Specification

## Purpose
TBD - created by archiving change refresh-node27-window-admission. Update Purpose after archive.
## Requirements
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

#### Scenario: A bounded readiness call blocks instead of answering

- **WHEN** a unit-status or health call inside the readiness wait blocks until its clipped timeout expires
- **THEN** the budget is consumed, not extended, and exhaustion refuses with the readiness-timeout check code rather
  than an untyped process-timeout error

#### Scenario: Readiness is exercised by the disposable window oracle

- **WHEN** the disposable eight-case window oracle runs any case that starts the display service
- **THEN** the readiness transport resolves against the oracle's own simulated boundary and any real urllib HTTP
  request refuses, so no case can reach a live host HTTP service

### Requirement: Post-D12 re-admission binds a fresh state to retained provenance

After a D12 recovery that retained the narrow table, the executor SHALL admit a new re-forward only through a fresh
`reprepare` state. Admission SHALL repeat every runtime admission of initial preparation and SHALL verify, read-only,
hash-pinned prior D12 provenance against the live catalog, ledger, route column, narrow table shape and every retained
run before the state reaches `REPREPARED`. It MUST NOT resume, overwrite or write the prior state.

#### Scenario: Retained-D12 state with unchanged provenance

- **WHEN** the live catalog is exactly canonical = prior OLD OID and `river_timeseries_narrow_rollback` = prior narrow
  OID, the ledger equals prior history plus `000059` with no pending file, and every retained `run_key` matches the
  prior snapshot's `run_id` and `parsed_at` with status `parsed` or `published`
- **THEN** a fresh state records both OIDs, the ledger, the retained runs, copies of the pinned provenance and the
  route inventory, and ends in phase `REPREPARED` with mode `reforward`

#### Scenario: Catalog, ledger, ownership or provenance diverges

- **WHEN** a table is extra, missing or foreign; an owner changed; the ledger differs or a migration is pending; a
  provenance file hash differs; a retained run changed since D12; or the rollback table holds a `run_key` absent from
  provenance
- **THEN** admission refuses with a typed check and changes no catalog, ledger, route, service or prior state

### Requirement: Re-forward reattaches the retained narrow table under a verified fence

`reforward` SHALL accept only a `REPREPARED` reforward state with the signed GO and unchanged config, driver, boot,
source and ledger. It SHALL repeat re-admission before T0 and inside the drained fence, then in one transaction verify
both OIDs, rename OLD to `river_timeseries_legacy` and the retained table to `river_timeseries`, and derive routes:
retained runs `narrow`, other parsed/published runs `legacy`, unparsed runs `narrow`. It MUST NOT delete ledger rows,
drop the rollback table or execute `000059`. Validation SHALL require the admitted OIDs and derived routes, and proofs
SHALL include a real NEW parse, a narrow read, a retained-run read and the unchanged legacy read before startup.
The initial `window` path SHALL accept only `PREPARED` initial states and remain unchanged.

#### Scenario: Re-forward after OLD-window writes succeeds

- **WHEN** runs were created with the default route and parsed by OLD after D12, and re-forward is executed
- **THEN** OLD-window runs route `legacy` and stay readable, retained runs route `narrow` and read their original facts,
  a new unparsed run parses into the reattached table, both OIDs and the ledger are unchanged, and the window reaches
  its validated milestone

#### Scenario: Phase or mode is not the expected transition

- **WHEN** `reforward` is given an initial `PREPARED` state or `window` is given a `REPREPARED` state
- **THEN** it refuses before any service, catalog or ledger action

#### Scenario: Live state drifts after re-admission

- **WHEN** retained provenance or source identity changes between `reprepare` and `reforward`
- **THEN** re-forward refuses before T0 without stopping services

### Requirement: Re-forward failure recovers OLD with all data retained

Any failure after re-forward admission SHALL run the existing D12 recovery. When the reattached catalog is present,
recovery SHALL additionally require the canonical OID to equal the admitted narrow OID. Recovery SHALL preserve both
OIDs, all facts in both tables, the ledger, and write a fresh route snapshot usable as provenance for a later attempt.

#### Scenario: Interruption after reattach commits or during readiness

- **WHEN** the executor is killed after the reattach transaction commits, killed at display start, or readiness fails
- **THEN** recovery (repeated twice) restores OLD canonical and the retained narrow table with unchanged OIDs, retained
  and newly parsed narrow facts, OLD facts and ledger, restores only authorized timers, and a later fresh re-admission
  from the new state succeeds read-only

