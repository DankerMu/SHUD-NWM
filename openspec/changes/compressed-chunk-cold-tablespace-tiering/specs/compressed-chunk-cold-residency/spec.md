## ADDED Requirements

### Requirement: Cold-residency eligibility MUST derive from terminal compressed business-time state

The system SHALL consider a chunk for cold residency only when it belongs to
`hydro.river_timeseries` or `met.forcing_station_timeseries`, is currently
compressed, and its half-open range satisfies `range_end <= business_watermark
- compression_lag`. The business watermark SHALL be the existing display
catalog forecast watermark; missing or unreadable truth SHALL block selection
without a wall-clock fallback. The same facts SHALL be revalidated after
relation locks are acquired and before any movement.

#### Scenario: Exact-cutoff compressed chunk is eligible

- **WHEN** an allowlisted compressed chunk has `range_end` exactly equal to the display business watermark minus the configured compression lag
- **THEN** it is eligible, subject to the remaining residency and environment preflights

#### Scenario: Hot or uncompressed state is ineligible

- **WHEN** a chunk is newer than the cutoff, is uncompressed, belongs to another hypertable, or the business watermark is unavailable
- **THEN** no member of that chunk is moved and the observation records an ineligible or fail-closed result without consulting host wall time

#### Scenario: Selection state changes before locking

- **WHEN** an initially selected chunk is decompressed, dropped, renamed, or otherwise changes compression identity before the migration transaction locks and revalidates it
- **THEN** the transaction performs no stale move and reports a deferred or recovery-classified result

### Requirement: A residency group MUST include all physical storage owned by both chunk relations

For one compressed chunk, the system SHALL resolve one residency group
containing the origin chunk heap, its compressed relation heap, every index on
both relations, and every owned TOAST heap and TOAST index reachable from those
relations. The durable group identity SHALL bind hypertable identity, origin
OID/name and half-open range. Each observation SHALL additionally bind the
current compressed relation OID/name and every current member; before and after
compressed identities SHALL be distinct fields because transactional
recompression replaces the sibling. Missing, duplicated, cross-group, or
unexpectedly changing mappings SHALL be rejected. A group SHALL be terminally
cold only when every currently reachable member resolves to the configured cold
tablespace.

#### Scenario: Origin shell alone cannot prove cold residency

- **WHEN** the origin chunk shell is in the cold tablespace but its compressed relation, an index, or owned TOAST storage remains in `pg_default`
- **THEN** the group is classified mixed/partial and cannot produce `migrated` or `already_cold`

#### Scenario: Empty and index-shape variants remain complete

- **WHEN** the group has an empty origin shell, no user-created index, multiple indexes including a quoted numeric-leading name, or owned TOAST storage
- **THEN** discovery deterministically enumerates exactly the members that physically exist and verifies every member's tablespace

#### Scenario: Same-window chunks retain separate identities

- **WHEN** both allowlisted hypertables contain chunks covering the same time window
- **THEN** each chunk resolves to a separate residency group and neither group can contribute members or evidence to the other

### Requirement: Supported TimescaleDB 2.10.2 movement semantics MUST be proven on an isolated cluster

Before production runner implementation, the change SHALL execute an automated
probe against PostgreSQL 15.2 and TimescaleDB 2.10.2 in a separately named,
disposable cluster using the exact pinned node-27 database image identity. The
probe SHALL evaluate `timescaledb_experimental.move_chunk`, direct compressed
relation/index movement, decompress-first movement, and the shell-first
transactional sequence. It SHALL freeze only this measured shell-first
sequence after proving complete movement, rollback, locking, query and
lifecycle behavior: lock and revalidate the compressed group; move the origin
shell and every origin index to `nhms_cold`; decompress; prove the expanded
origin/TOAST/index group is cold; recompress; prove the newly-created complete
compressed group is cold; prove data parity; then commit and perform a fresh
readback. It SHALL never directly ALTER the compressed heap or TOAST. Rejected
sequences and their exact errors SHALL remain in the committed probe evidence.

The probe SHALL NOT create/drop a tablespace or inject filesystem faults inside
the live `nhms-db` cluster. It SHALL refuse the live container name, live
PGDATA/tablespace paths, and live PostgreSQL port, and SHALL clean up its own
container, PGDATA and tablespace paths at the end.

#### Scenario: Live-cluster database fixture is rejected

- **WHEN** a test configuration points tablespace DDL or fault injection at the live container/port/path even if it requests a newly created database
- **THEN** the probe refuses before connection because PostgreSQL tablespaces and their files are cluster-scoped

#### Scenario: Probe freezes one complete sequence

- **WHEN** all positive, lifecycle, lock, timeout, interruption and rollback rows pass on the pinned 2.10.2 cluster
- **THEN** the probe log records server/extension/image identity, before/after member residency and bytes, query parity, the accepted SQL/lock order, and rejected alternatives

#### Scenario: Engine behavior cannot satisfy complete residency

- **WHEN** every tested sequence leaves a member hot, cannot roll back partial work, or cannot preserve cold residency through decompression and recompression
- **THEN** the probe fails and blocks runner implementation rather than weakening the group invariant

### Requirement: One residency-group move MUST be transactional and reconcilable

The system SHALL acquire the supported heap locks in stable OID order,
revalidate the candidate and source group, and run the shell-first sequence
inside one transaction with finite local lock and statement timeouts. Moving
the shell MAY create a transient mixed group inside that transaction; no
terminal boundary may expose or accept it. After decompress, the expanded
origin group SHALL be fully target-resident; after recompress, the newly-created
complete group SHALL be fully target-resident. Any SQL or proof failure SHALL
roll back the transaction. A successful commit SHALL be followed by a fresh
catalog readback proving all current members at the target. If the client loses
the commit result, a fresh reconciliation keyed by durable origin identity and
range SHALL classify the group as complete source, complete target, mixed, or
unknown while permitting a committed recompression to have a new compressed
OID/name; mixed/unknown SHALL block further mutation and SHALL never be
reported as success.

#### Scenario: Mid-group statement fails

- **WHEN** a missing target, capacity fault, permission error, statement timeout, or injected failure occurs after at least one member movement statement
- **THEN** transaction rollback leaves every member at its before tablespace and no partial-success receipt is emitted

#### Scenario: Rewrite capacity is budgeted on both devices

- **WHEN** preflight sizes a shell-first migration
- **THEN** `required_cold = before_compression_total_bytes + cold_reserve_bytes`
  and `required_hot = wal_reserve_bytes`; exact equality is admitted, either
  device being one byte short blocks before movement SQL, and
  `retained_source_bytes` is recorded because the original compressed group
  remains hot until commit but neither credits cold free space nor adds a second
  hot-free requirement

#### Scenario: Each group uses a fresh free-space observation

- **WHEN** one enforce tick plans more than one residency-group rewrite
- **THEN** it samples cold and hot free bytes immediately before each group's
  capacity decision, so a prior rewrite's consumption can block a later group;
  no run-start or previous-group free-space snapshot is reused

#### Scenario: Production reserve configuration is explicit

- **WHEN** either production cold-reserve or WAL-reserve byte input is absent, empty, non-integral, zero, or negative
- **THEN** the runner refuses before connecting for mutation, and no Python, wrapper, environment example, or disposable-probe observation supplies an implicit production default

#### Scenario: Expanded bytes never land on the hot tablespace

- **WHEN** the shell-first transaction has moved the origin shell and then executes `decompress_chunk`
- **THEN** the expanded origin heap, indexes and owned TOAST resolve entirely to `nhms_cold`, while any transient hot compressed index disappears before terminal recompression proof

#### Scenario: Concurrent reader holds a conflicting lock

- **WHEN** a reader or lifecycle operation prevents the stable group lock from being acquired within `lock_timeout`
- **THEN** the group is deferred/refused with zero member movement while unrelated groups remain eligible for later bounded ticks

#### Scenario: Process loses the commit acknowledgement

- **WHEN** the moving process is interrupted or loses its connection around commit
- **THEN** a new connection reconciles every group member and target-window parity without blindly replaying the move; complete source requires the original compressed sibling identity, while only a complete committed target may carry the probe-proven replacement sibling

#### Scenario: Target-chunk parity cannot be hidden by sibling rows

- **WHEN** migration parity is checked before, inside and after the transaction
- **THEN** count, per-column non-null counts and deterministic multiset checksum
  are computed over exactly the origin chunk's half-open `[range_start,
  range_end)` window and every non-dropped user column in validated physical
  order, so an unrelated same-table chunk cannot offset or hide target data loss;
  PostgreSQL returns one bounded aggregate row and the production client never
  fetches or materializes all business rows

#### Scenario: Locked parity and inventory are freshly revalidated

- **WHEN** stable group heap locks have been acquired and the preflight inventory
  or target-window data changed after selection
- **THEN** the moving transaction re-derives both allowlisted inventories and the
  exact target-window parity, compares them with preflight descriptors/digests/
  parity, and aborts before the first movement SQL on any drift; its parity read
  lock remains held through commit

#### Scenario: Production parity inventory is schema-derived and bound

- **WHEN** the runner starts a dry-run or enforce observation
- **THEN** it derives both allowlisted hypertables' complete non-dropped user-column
  inventories from the live PostgreSQL catalogs, validates from the Timescale catalog that `valid_time` is the sole open time
  dimension and has PostgreSQL type `timestamptz`, records name/type/nullability/generation descriptors
  and their digest, and generates parity from that exact inventory before any
  mutation; a missing, extra, reordered, unsupported, or drifted column fails
  closed, and production code never imports the #1892 four-column fixture helper
  or accepts an unvalidated caller list

#### Scenario: Catalog and filesystem identity disagree

- **WHEN** catalog location, current container bind source, host mount/device,
  directory writability, or configured expected target identity disagree
- **THEN** movement fails closed before the first relation statement; bind/host/
  device observations must come from a required real inspector and cannot be
  synthesized by echoing expected config values, while the isolated oracle
  injects at least one safe mismatch and proves zero movement SQL

#### Scenario: Pinned engine identity drifts

- **WHEN** the live read-only image identity, disposable image ID, PostgreSQL version, or TimescaleDB version differs from the pinned contract
- **THEN** the probe fails before selecting or executing a movement sequence and cannot emit PASS

### Requirement: Cold residency MUST converge across decompression, replay, recompression and retention

The legal state progression SHALL be `hot-uncompressed -> hot-compressed ->
cold-compressed -> cold-uncompressed-replay -> cold-compressed`. A manual
cold-chunk decompression SHALL recreate writable storage in the cold
tablespace. Replay writes SHALL remain cold. Recompression SHALL end with a
complete cold group through the probe-proven engine behavior or immediate
serialized convergence. Dropping the chunk SHALL remove both chunk relations
and all group-owned storage without orphaned catalog or filesystem members.

#### Scenario: Cold decompression and replay stay cold

- **WHEN** an eligible cold-compressed group is decompressed and historical rows are deleted/inserted through the existing guarded replay path
- **THEN** the uncompressed origin relation and every newly created index/TOAST member remain in the cold tablespace and values/count/checksum match the requested replay

#### Scenario: Recompression returns to complete cold state

- **WHEN** the replayed chunk becomes terminal and the existing compression lane recompresses it
- **THEN** the resulting origin/compressed relation group is fully cold before the serialized lifecycle tick can report success

#### Scenario: Drop removes the entire group

- **WHEN** existing retention drops a cold chunk through `drop_chunks`
- **THEN** no origin, compressed, index, TOAST, catalog, or tablespace-file member for that group remains

### Requirement: Cold-residency convergence MUST be dry-run by default, bounded, fair and serialized

The production runner SHALL default to dry-run and SHALL require explicit
enforce authorization. One enforce tick SHALL have a positive group bound,
finite per-statement and whole-run budgets, deterministic fair progress across
both hypertables, and the same mutex/ordering domain as compression,
decompression and retention operations that can touch the selected group. It
SHALL scan all eligible compressed groups, not only groups compressed in the
current tick. An already-cold group SHALL be a no-write no-op; a partial group
SHALL enter recovery handling rather than ordinary movement.

#### Scenario: Catch-up includes previously compressed chunks

- **WHEN** eligible hot-compressed groups predate the current compression tick
- **THEN** they appear in dry-run selection and converge under later bounded enforce ticks

#### Scenario: Bound and fairness hold

- **WHEN** more eligible hot-compressed groups exist than the configured per-tick bound across both hypertables
- **THEN** bounded catalog reads assign oldest-first rank within each hypertable
  and merge by `(per_hypertable_rank, range_end, hypertable_schema,
  hypertable_name, origin_oid)`, so every nonempty hypertable contributes one
  candidate before either contributes its next rank; no more than the bound are
  mutated, every remainder is recorded as deferred, and no table is starved

#### Scenario: No-op observations do not consume the mutation bound

- **WHEN** already-cold groups appear before hot-compressed groups in stable order
- **THEN** the runner records every observed no-op without issuing rewrite SQL or decrementing the per-tick mutation budget, so the same tick may still migrate up to its configured bound

#### Scenario: One lifecycle mutex and one recurring trigger serialize mutation

- **WHEN** compression, cold residency, retention, or manual decompression/replay starts
- **THEN** it acquires `/tmp/nhms-node27-timeseries-lifecycle.lock` before any
  lane-local or database relation lock, after verifying a no-follow regular file
  owned by the effective user with mode 0600, and contention produces a
  no-mutation refused/deferred result; the existing 04:25 compression timer invokes cold
  residency only as its second sequential `ExecStart`, with no cold-residency
  timer or asynchronous mutation lane

#### Scenario: Sequential wall budgets are mechanically ordered

- **WHEN** runner config, wrappers and the shared systemd oneshot are validated
- **THEN** each wrapper wall exceeds its maximum statement wall plus cleanup
  margin, the service wall exceeds the compression-wrapper wall plus
  cold-residency-wrapper wall plus systemd margin, and the existing retention
  timer begins after that worst-case service window; invalid or drifted literals
  refuse before mutation and fail the static contract tests without changing the
  retention window

#### Scenario: Autopipe selection race is revalidated transactionally

- **WHEN** autopipe or another writer changes a candidate after catalog selection without holding the lifecycle flock
- **THEN** the runner's stable heap locks and in-transaction compression/window/group revalidation prevent stale movement and record only the freshly proven state

#### Scenario: Already-cold rerun is idempotent

- **WHEN** a complete group is already at the cold target
- **THEN** the runner records `already_cold` without issuing a large-file rewrite

#### Scenario: Lifecycle race cannot produce stale success

- **WHEN** decompression, retention, an ingest writer, or compression races selection
- **THEN** shared serialization or locked revalidation prevents concurrent unsafe mutation and the runner reports only the state proven after the race

### Requirement: Every runner outcome MUST carry a schema-valid identity-bound receipt

The runner receipt SHALL bind its schema version, mode, exact reviewed head
SHA, cluster/server/extension identity, business watermark, compression lag and
cutoff, target tablespace/catalog/path/device identity, bounds and timeout
budgets, and every selected/deferred/skipped group. Each group observation
SHALL record origin/compressed identities, every physical member's relation
kind, OID/name, before/after tablespace and bytes, duration/wait, outcome, and
any error/recovery status. Credentials and secret environment values SHALL
never appear.

Receipt publication SHALL be bounded, mode 0600 and atomic. Before an enforce
run issues its first movement SQL, it SHALL atomically write a same-directory
intent sidecar and replace any prior public receipt with the same schema-valid
`in_progress` payload bound to each selected group's complete preflight source
snapshot: original compressed sibling/member identities, source residency,
inventory digest, target-window parity, and actual capacity decision. The
sidecar SHALL be authoritative while it exists. After fresh reconciliation,
terminal publication SHALL atomically replace the public receipt before durably
removing the sidecar by unlink plus parent-directory fsync and identity
verification. A publication failure SHALL make the run non-success even
when reconciliation proves a DB commit; the runner SHALL not repeat mutation to
recreate evidence. Failure or indeterminate durability at either terminal step
leaves the public intent or a truthful terminal plus the authoritative sidecar,
never an older success presented as current.

#### Scenario: Normal and no-op receipts prove parity

- **WHEN** one group migrates and one group is already cold
- **THEN** both receipts validate, the migrated group proves every member at the target, and the no-op proves zero movement

#### Scenario: Partial/error receipt is honest

- **WHEN** a group is mixed, disappears, times out, loses commit acknowledgement, or cannot publish its terminal receipt
- **THEN** the available evidence records a non-success/recovery state and no stale prior success is presented as current

#### Scenario: Terminal publication fails after database commit

- **WHEN** pre-mutation intent artifacts exist, the database commit is freshly
  reconciled as complete target, and terminal receipt replacement or sidecar
  removal fails or has indeterminate durability
- **THEN** the process exits non-success and does not replay movement; either the
  public receipt is still `in_progress`, or it is the truthful terminal while
  the authoritative sidecar still requires startup reconciliation, so no older
  success is current

#### Scenario: Source-side intent recovery requires original evidence

- **WHEN** startup observes a group resident at the source tablespace
- **THEN** it may classify `complete_source` only when every current member,
  original compressed sibling OID/name, inventory digest and target-window parity
  match the pre-mutation snapshot stored in intent; absent before evidence or a
  replacement sibling at source is `unknown`, never successful rollback

#### Scenario: A later tick encounters unresolved intent

- **WHEN** startup finds an authoritative intent sidecar from an earlier enforce
  run, regardless of the public receipt contents
- **THEN** it fresh-reconciles every named durable group and durably publishes a
  recovery terminal before selecting new mutation; complete source or target may
  close the intent, while any mixed/unknown result blocks the tick and the
  unresolved sidecar is never overwritten by a new run

#### Scenario: Early terminal errors invalidate stale success

- **WHEN** config is valid enough to identify a safe receipt path and a later
  head-freeze, lock-open, watermark, catalog, target-inspector, capacity,
  transaction, or terminal-publication step fails
- **THEN** the runner atomically publishes a schema-valid truthful non-success
  tombstone or preserves authoritative intent before exit; it never leaves an
  older success looking current and never fabricates unobserved engine,
  inventory, target or capacity identity to satisfy the schema

#### Scenario: Movement event identity is honest

- **WHEN** failure occurs before the first origin shell/index `SET TABLESPACE`
  statement, including timeout while acquiring heap locks or revalidation drift
- **THEN** `shell_sql_executed` is false; it becomes true only after a movement
  statement actually reaches the database, not merely because a plan contains
  movement SQL

#### Scenario: Receipt cannot leak credentials

- **WHEN** a database or filesystem error includes a DSN, password-like value, signed URL, or secret environment value
- **THEN** the published receipt and stderr carry only redacted stable diagnostics

### Requirement: Cold tablespace installation MUST bind catalog, container path and host device fail-closed

Installation SHALL use fixed tablespace name `nhms_cold`, host path
`/data/GHDC/nhms-cold-tablespace`, and container path
`/home/postgres/pgdata/tablespaces/nhms_cold`. Its default mode SHALL be
read-only dry-run. Before enforce, the host directory SHALL be empty,
non-symlink, on the expected mounted RAID, and have the exact PostgreSQL
container UID/GID and mode; the raw container configuration SHALL be snapshotted
and recreated with every environment, port, mount, resource limit and restart
policy preserved except for the one new bind. Catalog readback, bind source,
host device and in-container writability SHALL agree.

The installer SHALL NOT attach `nhms_cold` to either business hypertable. New
chunks SHALL continue to default to `pg_default`.

#### Scenario: Unsafe fresh directory or drifted catalog blocks installation

- **WHEN** the directory is nonempty, symlinked, missing its mount, wrong owner/mode/device, or `nhms_cold` already exists at another location
- **THEN** dry-run/enforce refuses before container replacement or DDL

#### Scenario: Container identity diff is narrow

- **WHEN** the fresh bind is installed
- **THEN** a normalized before/after container-config diff shows only the expected bind while image identity, environment, ports, limits, restart policy and existing mounts remain equal

#### Scenario: New chunk remains hot by default

- **WHEN** a new time range creates a chunk after installation
- **THEN** the chunk and its indexes use `pg_default`, and catalog inspection proves neither business hypertable has `nhms_cold` attached

### Requirement: Production authorization MUST require root RAID, member health and complete backup coverage

A production install or migration SHALL require fresh root-generated
`mdadm --detail` evidence and SMART PASS evidence for both RAID member devices,
with no degraded, rebuilding, recovering, missing or unknown state. `/proc/mdstat
[UU]`, a successful mount, or unprivileged evidence alone SHALL be insufficient.
It SHALL also require backup/readiness evidence that covers PGDATA and every
external `pg_tblspc` target; a PGDATA-only backup SHALL block rollout.

#### Scenario: Health evidence is incomplete or unhealthy

- **WHEN** root evidence is absent/stale, either member SMART result is unknown/non-passing, or the array is degraded/rebuilding/recovering
- **THEN** installation and migration return NO-GO without changing the live container or catalog

#### Scenario: Backup omits the tablespace target

- **WHEN** the recovery inventory protects PGDATA but cannot identify and restore the `nhms_cold` target referenced through `pg_tblspc`
- **THEN** live rollout is blocked even if RAID and capacity checks pass

### Requirement: Governance MUST report hot and cold storage from one observation without category collapse

Governance SHALL sample `/home` and `/data/GHDC` in one run and separately
report filesystem total/free bytes, PGDATA bytes, cold-tablespace relation
bytes, object-store bytes, and residual shared/third-party usage. It SHALL
surface catalog/filesystem/bind divergence, dangling catalog or bind paths,
stopped-container stale mounts, capacity/rollback-budget shortfall, and backup
coverage gaps as explicit blockers. Thresholds SHALL derive from current
measurements rather than historical constants.

#### Scenario: Same-time healthy sample is internally reconcilable

- **WHEN** catalog, current container, mounts and filesystems agree
- **THEN** governance reports both devices and the separated storage categories with timestamps and accounting residuals that can be independently checked using catalog and filesystem commands

#### Scenario: Dangling or stale topology is visible

- **WHEN** a catalog target lacks its live bind, a bind points at an unreferenced directory, a stopped container carries a stale mount, or measured category bytes cannot reconcile
- **THEN** the receipt names the divergent identities and rollout remains blocked

### Requirement: Live rollout MUST preserve data, hot placement, display behavior and performance

The rollout SHALL freeze the exact reviewed head and pre-mutation container,
cluster/catalog, group identity, row-count/checksum, dual-filesystem and public
API baselines. It SHALL quiesce writers and conflicting lifecycle operations,
install the fresh target, preview candidates, then migrate at most the bounded
groups one at a time with post-group parity. Existing baseline compressed
groups SHALL become completely cold; active/uncompressed groups SHALL remain
in `pg_default` and ingest SHALL resume. Timers SHALL be restored only after
catalog, data and display checks pass.

The rollout SHALL observe at least one natural serialized tick and either a new
terminal group's automatic cold convergence or a provable clean no-op. Hot and
cold SQL plans and public API/MVT/curve paths SHALL meet the #1342 gates:
buffers no more than 5000, SQL P95 no more than 300 ms, local API P95 no more
than 500 ms, and frontend river-click P95 under 2 seconds.

#### Scenario: Existing eligible groups migrate with data parity

- **WHEN** the controlled rollout processes the six baseline compressed groups
- **THEN** every complete group is cold with no mixed member, row count/identity/aggregate checksum and representative query results match before/after, and hot/cold filesystem deltas are reconciled against relation bytes

#### Scenario: Hot window remains writable and hot

- **WHEN** migration completes and ingest resumes
- **THEN** every active/uncompressed group remains in `pg_default`, current writers succeed, valid-times remain nonempty and non-regressing, and current GFS/IFS publication counts remain complete

#### Scenario: Rollback returns one group hot without data drift

- **WHEN** a rollback trigger fires or the planned rollback drill selects one cold group
- **THEN** the same transactional primitive returns its complete group to `pg_default`, data parity holds, and no referenced host path or catalog object is deleted

#### Scenario: Natural lifecycle tick is healthy

- **WHEN** timers are restored and the next natural compression/residency lifecycle tick completes
- **THEN** its schema-valid receipt is clean or a truthful no-op, all required timers are active, and `systemctl --user --failed` contains no issue-owned failure
