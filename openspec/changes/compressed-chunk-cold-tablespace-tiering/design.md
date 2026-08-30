## Context

Node-27 runs PostgreSQL 15.2 / TimescaleDB 2.10.2. Its two business
hypertables use seven-day chunks; terminal chunks are compressed, but both the
active rows and compressed bytes currently live in `pg_default` on `/home`.
The prior `ghdc` tablespace and product-archive lane were retired after the
`/dev/md0` incident. The recovered RAID may be re-admitted only through a new,
narrow DB-only contract.

A compressed TimescaleDB chunk has two identities: the origin chunk shell and
an internal compressed relation. Each can own indexes and TOAST storage.
Moving only the origin shell changes where decompression writes but does not
prove that the compressed bytes left `/home`. Tablespaces are PostgreSQL
cluster-scoped, so a throwaway database inside the live `nhms-db` cluster is
not an isolated oracle for tablespace creation, filesystem faults, or catalog
drift. The 2.10.2 experiment therefore runs in a separate disposable cluster
using the exact node-27 image identity; the live cluster is read-only during
Issue #1892.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Move only terminal, already-compressed chunk residency groups to one fixed
  cold tablespace, `nhms_cold`, without attaching it to a hypertable.
- Make the physical group complete, transactionally convergent, independently
  auditable, bounded per tick, idempotent, and safe across decompression,
  replay, recompression, retention, lock contention, timeout, and interruption.
- Fail closed when the display business watermark, cluster identity,
  RAID/SMART evidence, mount/catalog/path identity, capacity, backup coverage,
  or relation mapping is unavailable or inconsistent.
- Preserve ingest/display availability and current compression/retention
  policy while relieving `/home` only by the bytes actually moved.

**Non-Goals:**

- No product archive, salvage, rebuild drill, archive-gated retention, or
  on-demand decompression.
- No active/uncompressed-chunk move; no PGDATA, WAL, object-store, or node-22
  move; no PostgreSQL/TimescaleDB upgrade.
- No public API, frontend, row schema, compression-lag default, retention
  window, or display-watermark definition change.

## Decisions

### D1 — Eligibility is business-time and state based

A candidate MUST be one of the two allowlisted business hypertables, report
`is_compressed=true`, and have `range_end <= business_watermark -
compression_lag`. The watermark is the existing display-catalog forecast
watermark; there is no wall-clock fallback. Selection revalidates all facts
under the migration transaction. Hot/uncompressed and cold/uncompressed replay
states are ineligible.

### D2 — One complete residency group is the mutation unit

The durable group identity is `(hypertable schema/name, origin chunk
OID/schema/name, range_start/range_end)`. Each observation additionally records
the current compressed relation OID/schema/name plus every heap, owned TOAST
relation, and index reachable from the origin and compressed relations. The
compressed sibling is not part of the durable key: `decompress_chunk` removes
it and `compress_chunk` creates a new sibling with a new OID/name inside the
same transaction. Before and after sibling identities must therefore be
recorded separately and bound to the same origin identity/window. OIDs bind an
observation to the resolved objects; names make receipts operable. Every
currently reachable member must resolve to one tablespace before a terminal
`migrated` or `already_cold` result is legal. Missing, duplicated, changed,
cross-group, or mixed mappings at a terminal boundary fail closed.

### D3 — The probe-proven sequence is shell-first decompress/move/recompress

The pinned PostgreSQL 15.2 / TimescaleDB 2.10.2 isolated-cluster probe selected
exactly one production sequence:

1. resolve the compressed source group and data parity; begin one transaction,
   set finite local lock/statement timeouts, lock the origin and compressed
   heaps in stable OID order, and revalidate the source group;
2. `ALTER TABLE <origin> SET TABLESPACE nhms_cold`, then explicitly move every
   origin index in stable OID order; do not directly ALTER a compressed heap or
   any TOAST relation;
3. call `decompress_chunk(origin)`, re-resolve the uncompressed origin group,
   and prove its heap, indexes and owned TOAST are all cold;
4. call `compress_chunk(origin)`, re-resolve the new complete compressed group,
   prove every origin/compressed heap/index/TOAST member is cold, prove data
   parity, then commit; and
5. use a fresh post-commit readback for terminal success. A test rollback uses a
   fresh connection to prove the original compressed source group and parity
   were restored.

This shell-first order is load-bearing. On 2.10.2, moving the compressed origin
shell also moves the compressed heap/TOAST but can leave its compressed index
hot; that transient mixed state is legal only inside the transaction because
`decompress_chunk` removes the old sibling. The expanded uncompressed bytes
then land directly in `nhms_cold`, not `pg_default`; recompression creates a new
compressed heap/index/TOAST group inheriting `nhms_cold`.

Rejected alternatives remain evidence, not fallback lanes:
`timescaledb_experimental.move_chunk` is a non-transactional access-node-only
procedure on this image; direct ALTER of the compressed heap is rejected;
direct TOAST ALTER/LOCK is rejected; attaching `nhms_cold` to the internal
compressed hypertable does not route new compressed chunks; decompress-first
is atomic but expands the origin on the hot device; and committing a move then
recompressing in a second transaction leaves a non-atomic recovery window.
Later code may not carry a second sequence.

The probe runs in a separately named disposable container/cluster with its own
PGDATA and tablespace mounts. It MUST refuse the live container name, live
data/mount paths, or live PostgreSQL port, record image digest and
extension/server versions, and remove the disposable cluster and directories
in terminal cleanup. A per-database fixture in `nhms-db` cannot satisfy this
requirement.

### D4 — Migration is one rewrite transaction with pre-commit and post-commit proof

The production owner is `packages/common/compressed_chunk_cold_runtime.py`, not
the CLI wrapper or any `compressed_chunk_cold_probe` module. It consumes the
pure contract frozen by Issue #1892 and owns relation discovery, stable OID lock
order,
in-transaction revalidation, the D3 sequence, and parity over the durable origin
chunk's half-open time range. Before the first candidate mutation in every run,
it queries both allowlisted live hypertables for every non-dropped user column
in physical `attnum` order, validates from the Timescale catalog that `valid_time` is the sole open time
dimension and that its PostgreSQL type is `timestamptz`, and binds the complete name/type/nullability/
generation inventory plus its digest to the run. Its generated parity query
covers that exact inventory and records window row count, per-column non-null
counts, and a deterministic multiset checksum. The database returns one bounded
aggregate row per window; production code never fetches or materializes all
business rows in the client, and any configured row ceiling is enforced by SQL
before client materialization. A whole-hypertable aggregate cannot substitute
because unrelated sibling rows can hide target-chunk loss.

The #1892 probe hashes every column of its own four-column disposable fixture
(`id` / canonical UTC `valid_time` / `value` / NULL-distinct `payload`) via a
probe-support helper; that token is not a production-column contract.
Migrations 000005/000006 define different identity and business columns. The
production runtime MUST NOT import the fixture helper or accept an unvalidated
caller-supplied column list. After stable heap locks, the runtime re-derives both
inventories and the target-window parity on the moving transaction, compares
them with the preflight descriptors/digests/parity, and only then issues the
first movement SQL. The parity SELECT holds the hypertable read lock through
commit. Any inventory, statement, or proof failure aborts before success and,
when it precedes mutation, before the first movement SQL.
Connection loss/process kill is reconciled by a fresh catalog read keyed by the durable origin
identity/window: complete source, complete target, mixed, or unknown;
mixed/unknown is a recovery blocker, never success. A rolled-back source result
must restore the original compressed sibling OID/name and parity. Only a
complete committed target with matching origin/window/parity may carry the new
compressed sibling created by recompression; a new sibling at source is
unknown, not proof of rollback.

The operation is not metadata-only: it temporarily rewrites the full
uncompressed chunk on the cold device and emits WAL through PGDATA. Preflight
therefore requires cold free bytes for the full pre-compression expansion plus
cold reserve, and hot free bytes for the configured WAL reserve. The original
compressed source bytes remain allocated until commit and are recorded, but are
not reclaimed capacity and are not a second hot-free demand. A post-commit
readback proves every current member at `nhms_cold`. The
receipt records before/intermediate/after identities, tablespaces and relation
bytes, transaction outcome, parity, waits/durations, and recovery
classification. Filesystem deltas and WAL growth are secondary aggregate
checks; catalog relation bytes are the primary group-accounting unit.

### D5 — Lifecycle converges without a second mutation lane

State flow is:

`hot-uncompressed -> hot-compressed -> cold-compressed ->
cold-uncompressed-replay -> cold-compressed`.

Manual decompression is permitted only after the same group and capacity
preflight. The decompressed origin stays in `nhms_cold`; replay writes there.
Recompression must produce a fully cold group as proven by the #1892 sequence;
if engine behavior creates any hot member, the same serialized tick immediately
converges that group or reports a non-success mixed/recovery state.

One fixed process mutex, `/tmp/nhms-node27-timeseries-lifecycle.lock`, is owned by
`packages/common/node27_timeseries_lifecycle_lock.py`. Recurring compression,
cold residency, retention, and manual decompression/replay acquire it before
any existing lane-local flock and before database relation locks; contention is
a no-mutation refused/deferred result, and every terminal path releases it.
Autopipe does not acquire this mutex because its writer contract excludes
eligible compressed groups; selection-to-lock drift remains fenced by stable
heap locks plus in-transaction catalog and eligibility revalidation.

The existing compression oneshot is the only recurring trigger: its current
04:25 timer starts compression first and cold residency as a second sequential
`ExecStart`. No cold-residency timer or asynchronous service lane is added. The
service wall budget exceeds both sequential wrapper wall budgets plus a systemd
margin; each wrapper wall exceeds its own maximum statement budget plus cleanup
margin. The existing retention timer moves after that worst-case service window;
the lifecycle mutex remains the runtime backstop if manual activation or delay
still overlaps. The retention window does not change. The checked-in unit/config
is installed and enabled only by #1895 after #1894
creates the target. Retention, decompression, or relation disappearance after
selection yields a deferred/reconciled result, not stale success.

### D6 — Runner is dry-run by default, bounded, fair, and receipted

Issue #1893 adds `scripts/node27_cold_residency.py` and its one wrapper to the
existing compression oneshot. Enforce has an independent positive per-tick
mutation bound, a maximum members-per-group bound, finite catalog row/byte
limits, and a whole-run wall outside per-statement bounds. Candidates receive
an oldest-first rank within each allowlisted hypertable and are merged by
`(per_hypertable_rank, range_end, hypertable identity, origin OID)`. This deterministic interleave gives each nonempty hypertable a candidate
before either receives its next rank. Newly eligible chunks have later range
ends, and `already_cold` observations are recorded without consuming the
mutation bound, so both backlogs progress without a persisted cursor. The runner
scans all eligible compressed groups, including chunks compressed before the
current tick. Partially cold groups are
never ordinary candidates: they enter explicit recovery classification and
enforce fails closed unless the #1892 recovery protocol proves a safe
all-or-nothing convergence.

Capacity preflight obtains `before_compression_total_bytes` from the target
chunk's TimescaleDB compression statistics and uses the #1892 arithmetic without
crediting retained source bytes. Immediately before every group preflight, it
freshly samples both devices; a prior group's free-space sample is never reused
for a later rewrite. Both `NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES`
and `NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES` are mandatory positive integer
byte inputs with no Python, shell, or example-template default. This fixture
does not invent a universal reserve from disposable WAL observations; #1895
freezes measured live values in the mode-0600 environment before installing the
unit. Exact equality is admitted, and either one-byte short case refuses before
movement SQL.

Receipt publication is atomic, mode 0600, schema-validated before replacement,
and bound to the exact head SHA, config, business watermark, lag/cutoff, cluster
identity, target catalog/path/device identity, validated business-column
inventory and per-window parity, capacity decision, and group observations.
Target bind/host/device observations come from a required production inspector;
configuration supplies expected values only and may never echo them as observed
truth. Before enforce mutation, the runner atomically writes a same-directory mode-0600
intent sidecar and replaces the public receipt with the same schema-valid
`in_progress` payload naming selected groups and the complete preflight source
snapshot: original compressed sibling/member identities, source residency,
inventory digest, target-window parity, and per-group capacity decision. The
sidecar is the authority whenever it exists. Terminal publication first atomically replaces the
public receipt after fresh reconciliation, then durably removes the sidecar; a
failure before or after replacement therefore leaves either the public intent or
a truthful terminal plus an authoritative sidecar, never an older success as
current. Durable sidecar removal includes parent-directory fsync and identity
verification; a successful pathname unlink alone is not terminal durability. A later invocation that finds the sidecar fresh-reconciles every named
group and durably publishes a recovery terminal before selecting new mutation;
mixed/unknown blocks the tick. Publication failure makes the run non-success and
never replays database mutation or overwrites unresolved intent with a new run.

### D7 — Fresh installation is a separate fail-closed boundary

Issue #1894 owns a dry-run-default installer/preflight and governance extension for
fixed catalog name `nhms_cold`, fresh host path
`/data/GHDC/nhms-cold-tablespace`, and fixed container path
`/home/postgres/pgdata/tablespaces/nhms_cold`. Installation requires an empty,
non-symlink host directory on the expected mounted device with pinned owner and
mode, an exact raw-container config snapshot whose only intended change is the
new bind, and post-create catalog/mount/device/write readback. It MUST NOT call
`attach_tablespace` for either business hypertable.

Production authorization requires root-generated `mdadm --detail` evidence,
SMART PASS evidence for both member devices, no degraded/rebuild/unknown state,
and backup readiness covering PGDATA plus every `pg_tblspc` target. `[UU]` or
successful mount alone is insufficient. Missing or stale root evidence is a
NO-GO, not a warning.

### D8 — Governance reports both devices without collapsing categories

A same-time governance sample reports `/home` and `/data/GHDC` totals/free
bytes, PGDATA bytes, `nhms_cold` relation bytes, object-store bytes, and residual
third-party/shared use separately. Catalog/mount/filesystem divergence,
dangling catalog, dangling bind, stopped-container stale mount, and backup
coverage gaps are explicit blockers. No fixed capacity number in this fixture
is treated as current truth; live thresholds derive from measured rollback
headroom and are recorded in the rollout receipt.

### D9 — Rollout is bounded and independently reversible

Issue #1895 freezes exact reviewed HEAD and pre-mutation catalog/data/filesystem/API
baselines, quiesces writers and lifecycle timers, passes D7 gates, creates the
fresh tablespace, previews candidates, then moves one group at a time with
post-group parity. It restores timers only after all six baseline groups and
all active/hot exclusions are proven. It validates one natural tick and either
a newly terminal group migration or a provable no-op, plus hot/cold SQL and
public display behavior against #1342 thresholds.

Rollback moves one group back to `pg_default` through the same transactional
primitive. Container/path deletion is forbidden while catalog references
remain. A live failure stops further groups and follows the verified rollback;
it never adds a production-only patch.

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — runner and installer operator
  entrypoints are fail-closed and dry-run by default.
- Config / project setup: selected — fixed tablespace/path identity, timeout
  budgets, bounds, and systemd ordering are part of the contract.
- File IO / path safety / overwrite: selected — bind paths, receipts, mount
  identity, and rollback must reject symlinks/aliases/unsafe replacement.
- Schema / columns / units / field names: selected — receipt and evidence
  fields are shared producer/consumer contracts; row columns stay unchanged.
- Auth / permissions / secrets: selected — tablespace DDL/root health evidence
  requires privileged boundaries; credentials must never enter receipts.
- Concurrency / shared state / ordering: selected — relation locks, one mutex,
  compression/decompression/retention races, and commit reconciliation.
- Resource limits / large input / discovery: selected — multi-gigabyte relation
  moves need group/tick/time bounds and capacity preflight.
- Legacy compatibility / examples: selected — current compression, retention,
  write guards, hot-chunk placement, and display reads remain compatible.
- Error handling / rollback / partial outputs: selected — timeout, lock,
  capacity, interruption, partial residency, and receipt-publish failures.
- Release / packaging / dependency compatibility: selected — behavior is pinned
  specifically to PG 15.2 / TimescaleDB 2.10.2; no upgrade is allowed.
- Documentation / migration notes: selected — ADR/runbook and production
  rollback are merge-gating artifacts.

Domain packs considered:

- Geospatial / CRS / basin geometry: not selected — no geometry value changes.
- Hydro-met time series / forcing windows: selected — two same-window
  hypertables, business watermark/cutoff, replay, and hot/cold query parity.
- SHUD numerical runtime / conservation / NaN: not selected — no solver or
  numerical transformation.
- PostGIS / TimescaleDB domain behavior: selected — compressed relation/catalog,
  chunk lifecycle, tablespaces, `drop_chunks`, and 2.10.2 locks are central.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 and
  Slurm are untouched.
- External hydro-met providers / snapshot reproducibility: not selected — no
  provider fetch or snapshot changes.
- Run manifest / QC provenance: not selected — run/QC payloads are unchanged.
- Published NHMS artifacts / display identity: selected — display/API identity
  and valid-time frontier must remain unchanged through rollout.

## Invariant Matrix

- Governing invariant: a group is reported cold only when an eligible compressed
  chunk's complete physical residency group is atomically and readably resident
  in `nhms_cold`; every uncertain state blocks further mutation.
- Source of truth: display business watermark + configured compression lag;
  TimescaleDB chunk/compression catalogs joined to PostgreSQL OIDs; fixed
  tablespace catalog/path/device identity; receipt schema version.
- Producers: compression runner creates compressed chunks; #1893 residency
  runner creates group receipts; #1894 installer/governance creates environment
  receipts; #1895 creates rollout evidence.
- Validators/preflight: shared group resolver/transaction primitive, runner
  selection/revalidation, installer RAID/SMART/mount/catalog/backup gates.
- Storage/cache/query: origin/compressed heaps, TOAST and indexes across
  `pg_default` and `nhms_cold`; no cache or row-schema change.
- Public routes/entrypoints: #1893 CLI/wrapper/systemd stage and #1894 installer;
  display API/frontend are unchanged consumers.
- Frontend/downstream consumers: public river/forcing curves, MVT and ingest,
  compression, decompression replay, retention.
- Failure paths/rollback/stale state: lock/statement/wall timeout, target full,
  process kill, relation deletion/decompression, catalog/path drift, mixed
  residency, receipt publication failure, group move-back.
- Evidence/audit/readiness: pinned isolated-cluster probe, schema-valid receipts,
  same-time dual-device governance, exact-SHA node-27 C1-C4/cold gates.

Regression rows:

- Eligible hot-compressed complete group -> one transaction -> all members cold,
  values/count/checksum unchanged, schema-valid receipt.
- Exact-cutoff compressed group -> eligible; later/newer or uncompressed group ->
  ineligible with zero mutation; missing watermark -> stable refusal.
- Empty/no-index/multi-index/already-cold and same-window cross-hypertable groups
  -> complete deterministic accounting, fair bounded progress, no rewrite for
  already-cold.
- Any member missing/drifted/mixed, target missing/wrong device/unwritable/full,
  lock/timeout/kill, or receipt publish failure -> no false success and a
  recovery-classified receipt or safely preserved prior receipt per publication
  stage.
- Cold decompression + replay + recompression -> every resulting member cold;
  retention/drop -> no orphan relation/catalog/file remains.
- New active chunks after installation -> `pg_default`; neither hypertable has
  `nhms_cold` attached.
- Unchanged ingest/display/retention consumers -> same identities, windows,
  valid-time frontier, and #1342 performance gates.

Boundary-surface checklist:

- Shared helper root: one group resolver/migration transaction module, owned by
  #1893 after #1892 freezes the tested sequence.
- Public entrypoints: one residency lifecycle lane and one installer/preflight;
  no duplicate mutation timer.
- Read/write surfaces: catalog/OID resolution, relation files, tablespace paths,
  receipt publication, no business-row rewrite.
- Staging/publish/rollback: database transaction + post-commit reconciliation;
  atomic receipt; same primitive for cold/hot rollback.
- Evidence boundaries: image/cluster identity -> probe; watermark/config/group
  identity -> runner receipt; RAID/mount/backup identity -> install receipt;
  exact SHA + parity/performance -> rollout receipt.
- Stale/idempotency boundaries: selection-to-lock drift, already-cold no-op,
  partial state recovery, receipt failure after commit, natural next tick.
- Unchanged consumers: active ingest, compression eligibility, retention window,
  public API/frontend/MVT, node-22/Slurm.

## Migration Plan

1. #1892 creates and reviews this fixture, runs the isolated 2.10.2 probe,
   freezes the supported sequence, updates ADR/runbook, and lands executable
   contract tests. No live cluster mutation.
2. #1893 implements the shared group primitive, runner, receipt/config/systemd
   serialization, and unit + isolated-cluster integration tests.
3. #1894 implements and tests fresh installation/rollback, container identity,
   RAID/SMART/backup gates, and dual-device governance without moving chunks.
4. #1895 performs the controlled live install/migration/rollback proof and
   validates automatic convergence plus display/performance.
5. Archive this shared OpenSpec change only after #1895; earlier PRs leave later
   task groups unchecked so unimplemented behavior is never published as done.

## Open Questions

- None for #1893. The #1892 probe fixed the only movement sequence; D4-D6 fix the
  production owner, live-column parity, mutex/order, systemd trigger, capacity
  inputs, and publication boundary. #1894/#1895 still own installation and the
  measured live reserve values, not alternate runtime semantics.
