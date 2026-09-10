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

Fixture level: high. Repair intensity: high. Project profile: NHMS. The
original expanded scope escalates to high for #2224 because a production G1
observation exposed that D4's older target-window wording admitted a parent-
hypertable scan; this interpretation correction affects every production parity
caller and requires a real TimescaleDB result-and-plan oracle before live retry.

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
order, in-transaction revalidation, the D3 sequence, and parity read through the
exact physical relation named by the durable origin chunk identity, with that
chunk's half-open time range retained as a second identity fence. The production
parity builder/owner requires origin schema and name as non-optional, no-default
input derived from the currently resolved `CatalogChunk`; the owning call also
binds origin OID and window. Missing or empty identity, the allowlisted parent,
the current compressed sibling, or any OID/schema/name/window mismatch fails
closed rather than selecting another relation. The parity query MUST
NOT read from the parent hypertable and trust planner pruning to find the target,
fall back to the parent when origin identity is absent, or hash the compressed
internal sibling's encoded columns as business rows. On TimescaleDB 2.10.2, the
accepted origin-relation query shape must be proven against a compressed chunk to
return transparent business rows while accessing no sibling chunk; `ONLY` is not
accepted unless that same oracle proves it preserves transparent decompression.
Before the first candidate mutation in every run, it queries both allowlisted
live hypertables for every non-dropped user column
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
first movement SQL. The parity SELECT holds the selected origin relation's read lock through commit.
Any inventory, origin-identity, statement, or proof failure aborts before success
and, when it precedes mutation, before the first movement SQL. G1 census,
production runtime preflight, locked revalidation, recompression, post-commit
readback and reconciliation, and #1895 post-target named-group observation all
route through this same mandatory origin-qualified owner. The probe-private
four-column fixture helper remains independent and is not widened into production.
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
does not invent a universal reserve from disposable WAL observations.
Issue #1895 derives `E` from the fresh six-group live catalog expansion census and
uses that same byte value for both reserves: the WAL reserve is an explicitly
conservative same-order expansion proxy, not a measured/per-group-attributed WAL
claim and not the disposable 165736-byte LSN delta. Exact equality is admitted,
and either one-byte short case refuses before movement SQL.

Receipt publication is atomic, mode 0600, schema-validated before replacement,
and bound to the exact head SHA, config, business watermark, lag/cutoff, cluster
identity, target catalog/path/device identity, validated business-column
inventory and per-window parity, capacity decision, and group observations.
Target bind/host/device observations come from a required production inspector;
configuration supplies expected values only and may never echo them as observed
truth. The same rule applies to the PostgreSQL server principal. The runner
requires explicit non-root numeric UID and GID inputs before connection in both
dry-run and enforce. Each component must be a decimal integer in
`1..4294967294` (`2^32-2`, excluding root and the `(uid_t)-1` sentinel). Missing,
empty, whitespace-padded, named/non-integral, boolean at the Python seam,
negative, above-bound, or one-component-only configuration refuses before DB
connection. The public example exposes both keys
unassigned; #1895 fills them only after fresh measurement.

One bounded inert Docker inspection uses a small projection—not the full inspect
document—to observe both `Mounts` and strict numeric `Config.User` inside the
existing 5-second and 64-KiB ceilings. Missing/empty, named, UID-only, malformed,
either-component-root, or mismatched runtime identity refuses before the
writability command. Only after equality is proved may the inspector execute
`test -w` as that exact `uid:gid`; image user names such as `postgres`, root
execution, and implicit fallbacks are forbidden. No second `Config.User` read is
required after the command; the existing descriptor-bound host before/after
identity comparison remains the accepted TOCTOU fence. The observed numeric pair
is carried through target identity into every current receipt.

This evidence addition uses a versioned schema union: readers and the shipping
schema accept both historical `1.0` and current `1.1`, while the writer and all
shipping examples emit only `1.1`. Historical `1.0` target objects omit
`container_exec_uid/gid`; observed `1.1` targets require both non-root integers,
and unobserved `1.1` targets require both fields present as null without echoing
expected config. A schema with `const: "1.1"` alone is invalid because it would
strand existing authoritative sidecars. Before enforce mutation, the runner
atomically writes a same-directory mode-0600
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

Production authorization requires descriptor-bound root-owned evidence files:
`mdadm --detail` for the named array and SMART results for exactly the two parsed
member devices. The installer validates their regular-file identity, bounded
content, capture time against an explicit positive maximum age, command/host/
device identity, and parsed health; a self-reported `root=true`, `/proc/mdstat
[UU]`, or a successful mount is insufficient. Both members must report SMART
PASS and the array must have no degraded, rebuild, recovery, reshape, missing,
spare-substitution, or unknown state. Backup readiness must cover PGDATA plus
every external `pg_tblspc` target, and measured free space must cover the
configured install plus rollback headroom. Missing, stale, malformed, or
unhealthy evidence is a NO-GO, not a warning.

The raw `docker inspect` document is bounded inert input, never shell source.
Before mutation the installer atomically persists a mode-0600 private recovery
bundle containing the exact secret-bearing environment and reconstructible
container fields; public receipts contain only field names, normalized
non-secret config, identities, and digests. Recreate argv is built directly
without a shell and must preserve every supported Config/HostConfig field; any
non-default field that cannot be reproduced exactly is a blocker. The old
container is stopped and renamed, not destroyed, until catalog/bind/readiness
proof closes installation. Rollback drops only installer-created catalog state
with no dependents, removes only the identity-recorded new container, restores
the renamed prior container, and removes an installer-created host directory
only after catalog, every live bind, `pg_tblspc`, identity and emptiness checks
prove it unreferenced. It never binds an empty directory over referenced data.

Installer outcomes use one strict public receipt schema with normal, already-
ready, NO-GO, progress, rollback and error examples. Every receipt is atomically
published mode 0600 and binds the exact reviewed head, fixed path/catalog
contract, observed container/image/config digests, host path/device identity,
root evidence file identities and freshness, parsed RAID/SMART state, backup
inventory, capacity/rollback decision, catalog/bind/writability readback,
mutation ownership, rollback state and stable redacted errors. An existing
complete topology is an idempotent no-write `already_ready`; any partial or
drifted topology is NO-GO rather than an implicit repair. During an interrupted
or failed install, the private authority drives only the installer's own
reconciliation or rollback. A terminal `installed` receipt is published only
after that authority is durably removed and reported closed; a later
`already_ready` invocation is observation-only and is not an operator rollback
entrypoint.

### D8 — Governance reports both devices without collapsing categories

One audit interval, identified by one started/finished timestamp pair, samples
`/home` and `/data/GHDC` and reports each observation time plus totals/free
bytes, PGDATA bytes, `nhms_cold` relation bytes, object-store bytes, and residual
third-party/shared use separately. Residual use is filesystem-used bytes minus
non-overlapping known categories, not a recursive walk of the shared 14-TB
root. Catalog/mount/filesystem divergence, dangling catalog, dangling bind,
stopped-container stale mount, negative/unreconcilable residuals, capacity/
rollback shortfall, stale health evidence, and backup coverage gaps are explicit
blockers. No fixed capacity number in this fixture is current truth; live
thresholds derive from measured rollback headroom.

Governance emits a separate strict mode-0600 atomic receipt bound to the exact
head, audit interval, filesystem observation identities, category accounting,
relation-by-tablespace catalog rows, current and stopped-container mount
inventories, root evidence identities/parsed health, backup coverage and every
blocker. The normal example proves two-device reconciliation; a drift example
proves catalog/filesystem/bind disagreement remains NO-GO. Credentials, raw
secret-bearing environment values and signed URLs never enter either receipt.

### D9 — Rollout is bounded and fails closed at shipping recovery boundaries

Issue #2137 first commits, reviews and merges the executable G0/live runbook
contract without remote access. Only the resulting merged runbook at #1895's
exact reviewed deployment SHA may supply the pre-target census, root-evidence
capture, installer, one-group runner, current-receipt, stop/resume and
close/archive commands; the historical manual container-recreation section is
explicitly not a cold-bind entrypoint. The census calls the production
catalog/inventory/parity owners directly and never calls the absent target
preflight. No node-27 access starts before #2137 merge, its issue-specific fixture
review, strict validation, contract tests and normal CI pass.

Root evidence uses schema `1.0`, the exact `/bin/hostname` output, UTC RFC3339
capture time, root:root mode `0600`, 900-second freshness, exact leaf argv
`/usr/sbin/mdadm --detail /dev/md0`, `/usr/sbin/smartctl -H <parsed-member>` for
each of exactly two active-sync members, and `/usr/local/sbin/nhms-backup-inventory
--json`, exact subjects/nonempty outputs, and PGDATA-plus-sorted-external-target
coverage including the future `nhms_cold` container path. The #1894 synthetic
helper is forbidden; a missing production producer is NO-GO.

Installer and runner device identities are intentionally distinct. Before path
creation, `node27_cold_tablespace_host.inspect_host_path()` supplies the installer
mount identity `major:minor:mount-id:source`; after creation,
`compressed_chunk_cold_target.inspect_host_path()` supplies the runner descriptor
identity `st_dev:st_ino`. They are recorded under separate names and never copied
between installer argv and runner env. Mode is `0700`; UID/GID come only from the
fresh canonical numeric `.Config.User`, never the historical deployment value.

The read-only preflight freezes exact HEAD plus catalog/data/filesystem/API
baselines. The six compressed groups observed on 2026-08-29 are a historical
count only. A fresh census that does not require the absent cold target must
resolve exactly six complete eligible source groups and bind each durable
origin/window key, current compressed sibling, complete member digest,
production inventory/parity, pre-compression expansion and retained-source
bytes. A missing group, unexplained extra group or identity/count drift ends the window as NO-GO; the
rollout never substitutes an arbitrary oldest six. Let `E` be the positive
maximum `before_compression_total_bytes` and `S` the positive sum of the six
retained-source group bytes. Checked, non-overflowing canonical decimal policy
is `cold_reserve=E`, `wal_reserve=E`, `install_required=S`, and
`rollback_headroom=2*E`. Here `wal_reserve=E` is a conservative same-order proxy
from fresh live expansion, not a WAL measurement or attribution. The existing
gates therefore require installer cold free >= `S + 2E`, and each group freshly
requires cold free >= its expansion + `E` and hot free >= `E`; no disposable WAL
number, LSN delta or historical free-space value supplies a default.

Before any live mutation, the exact reviewed SHA reruns the isolated #1892 oracle
and requires current-run complete cold movement, inverse complete-group move-back,
parity and owned-resource cleanup, followed by live read-only compatibility. The
accepted primitive supports `pg_default`, but the shipping production runner
exposes only convergence to `nhms_cold`; this is the issue acceptance criterion's
equivalent-disposable branch, not authority for ad hoc live SQL.

Writers and lifecycle timers are then quiesced. The #1894 installer is the sole
container/catalog mutation entry. If installation fails or is interrupted while
its private authority remains live, only the same installer state machine may
reconcile or roll back that in-progress install. A terminal `installed` receipt
closes and removes the authority; it does not leave an operator rollback handle.
After install, a second census must match the same six keys and preimages with no
extra eligible group before the first movement SQL. A failure at this post-install
boundary stops and preserves the terminally installed topology even when zero
groups have moved. Enforce fixes `PER_TICK_BOUND=1` and does not start group N+1
until group N's uniquely named, current-SHA, invocation-bracketed receipt proves
complete target, parity and filesystem reconciliation. Recurring timers remain
stopped through a controlled ingest smoke and all catalog/data/display/performance
gates; they resume only after a written preliminary GO.

Rollback/NO-GO triggers are exhaustive: head/worktree or engine drift; a missing
or extra census key; inventory/parity/member/source-state drift; target catalog,
bind, path, device or runtime UID/GID drift; active writer/lock; stale, malformed,
non-root or non-PASS RAID/SMART/backup evidence; capacity failure; installer or
private-authority failure; nonzero command exit; missing/stale/wrong-SHA/wrong-mode
receipt; mixed/unknown/non-complete residency; filesystem reconciliation failure;
Seq Scan or all-chunk decompression regression; #1342/API/MVT/click/publication
failure; or natural-tick/unit failure. Any trigger stops later mutation, forbids
ad hoc SQL and deletion of referenced topology, and keeps the 4.3 quiesced state
until a written GO or reviewed owning-implementation response. Timer state may be
restored after a proven pre-install no-mutation abort or after the installer itself
publishes a completed rollback for a still-live in-progress authority. Once the
installer publishes terminal `installed`, every later trigger preserves that
installed topology even when no residency group has moved. After a live group has
moved, the same stop-and-preserve rule additionally requires any future live
move-back entrypoint to land through its owner before reversal.

Every manual value is short canonical decimal and every accepted receipt is
bound to this invocation's reviewed SHA, generated-at bracket, mode/outcome and
exit status. This makes #1938 non-blocking without permitting a stale clean file
to satisfy readiness. For C1/C2/C3, #1895 uses a direct current
publication/display receipt. The C4 lane is the promoted capability delivered by
Issue #2123 (with #2130 classification precedence), and #1895 only executes it live. C3
binds those C4 PASS bytes and strict GFS/IFS job-log
facts to local display identity-only reads, readonly DB exact identities,
source-scoped registry complete-cycle counts, and the G1 valid-times frontier.
It does not claim node-22 scheduling and is not a substitute for the generic
producer-complete twelve-lane/full-scope aggregator. That aggregator remains
fail-closed and may run only against its complete producer bundle; creating an
empty directory never closes it. The evidence PR merges while the shared change
is still strict-valid; #1895 and then #1891 close only after that merge, and
archival is an immediate post-merge follow-up after final strict validation.

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — runner, installer, census and
  C1-C3/G8 operator entrypoints are fail-closed; #2137 must reject missing inputs
  or wrong reviewed SHA without opening the maintenance window.
- Config / project setup: selected — fixed tablespace/path identity, timeout
  budgets, bounds, systemd ordering and complete CI selector partitions are part
  of the contract.
- File IO / path safety / overwrite: selected — bind paths, private receipts,
  mount identity and rollback use bounded no-follow/no-clobber publication with
  exact ownership/mode/link/readback/fsync checks.
- Schema / columns / units / field names: selected — residency and C1-C3 receipt
  schemas are shared producer/consumer contracts; row columns stay unchanged.
- Auth / permissions / secrets: selected — tablespace DDL/root health evidence
  requires privileged boundaries; readonly DSNs are short-lived and neither DSN
  nor other credential material may enter receipts.
- Concurrency / shared state / ordering: selected — relation locks, one mutex,
  compression/decompression/retention races, current-run brackets, commit
  reconciliation and C3-after-C4-PASS publication order are enforced.
- Resource limits / large input / discovery: selected — multi-gigabyte relation
  moves and receipt/HTTP/process inputs need group, byte, tick and time bounds;
  #2224 corrects the live-discovered parent-hypertable parity scan while retaining
  the census `3600000` ms and runtime `3600s` finite ceilings rather than masking
  the defect with an unbounded or enlarged timeout.
- Legacy compatibility / examples: selected — current compression, retention,
  write guards, readonly facade exports/patch seams, hot-chunk placement and
  display reads remain compatible; #2137 consumes rather than duplicates C4.
- Error handling / rollback / partial outputs: selected — timeout, lock,
  capacity, interruption, partial residency, stale/wrong-run/private-path state
  and receipt-publish failures cannot yield PASS.
- Release / packaging / dependency compatibility: selected — behavior is pinned
  specifically to PG 15.2 / TimescaleDB 2.10.2; no upgrade is allowed.
- Documentation / migration notes: selected — ADR, executable G0/live runbook,
  correct C4 command ownership and production rollback are merge-gating artifacts;
  #2224 must add the G0/G1 STOP that invalidates the failed `a8db554d` window and
  requires a fresh exact-SHA window after the origin-parity repair merges.

Domain packs considered:

- Geospatial / CRS / basin geometry: not selected — no geometry value changes.
- Hydro-met time series / forcing windows: selected — two same-window
  hypertables, business watermark/cutoff, replay, and hot/cold query parity.
- SHUD numerical runtime / conservation / NaN: not selected — no solver or
  numerical transformation.
- PostGIS / TimescaleDB domain behavior: selected — compressed relation/catalog,
  chunk lifecycle, tablespaces, `drop_chunks`, and 2.10.2 locks are central;
  #2224 resolves the D4 interpretation divergence by requiring transparent
  business-row parity from the exact physical origin and a real result-plus-plan
  proof that no same-hypertable sibling is accessed.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 and
  Slurm are untouched.
- External hydro-met providers / snapshot reproducibility: not selected — no
  provider fetch or snapshot changes.
- Run manifest / QC provenance: not selected — run/QC payloads are unchanged.
- Published NHMS artifacts / display identity: selected — display/API identity
  and valid-time frontier must remain unchanged through rollout.

## Invariant Matrix

- Governing invariants: a group is reported cold only when an eligible compressed
  chunk's complete physical residency group is atomically and readably resident
  in `nhms_cold`; before live mutation, the entire #2137 G0 readiness chain must
  be merged, and after the failed first G1, #2224 must also merge before any retry;
  every uncertain evidence state must block remote access or PASS.
- Source of truth: display business watermark + configured compression lag;
  TimescaleDB chunk/compression catalogs joined to PostgreSQL OIDs, with the
  mandatory current durable origin OID/schema/name/window selecting the physical
  parity relation and parent inventory selecting only business columns; fixed
  tablespace catalog/path/device identity; required expected and independently
  observed numeric container runtime UID/GID; receipt schema version; exact
  reviewed SHA and current invocation bracket; promoted raw C4 receipt bytes.
- Producers: compression runner creates compressed chunks; #1893 residency runner
  creates group receipts; #1929 binds those receipts and writable checks to the
  observed numeric runtime principal; #1894 installer/governance creates
  environment receipts; #2137 provides production census and C1-C3/G8 receipt
  owners without live execution; #2123 provides the promoted C4 producer; #1895
  executes the merged owners and creates live rollout evidence.
- Validators/preflight: shared group resolver/transaction primitive, runner
  selection/revalidation, one bounded Mounts+`Config.User` inspector followed by
  exact numeric-principal writability, installer RAID/SMART/mount/catalog/backup
  gates, #2137 census/C1-C3 binders, canonical readonly facade and complete
  `select_ci_tests.py` acceptance partitions.
- Storage/cache/query: origin/compressed heaps, TOAST and indexes across
  `pg_default` and `nhms_cold`; parity selects transparent business rows from the
  exact durable origin relation, never the parent hypertable or encoded compressed
  sibling, while retaining a finite timeout and half-open identity fence; private
  evidence files use bounded no-follow, no-clobber, exact-mode durable publication;
  no cache or row-schema change.
- Public routes/entrypoints: #1893 CLI/wrapper/systemd stage, #1894 installer and
  #2137 census/C1-C3/G8 CLIs; display API/frontend are unchanged consumers and
  C4 remains owned by the promoted `c4-live-display-evidence` capability.
- Frontend/downstream consumers: public river/forcing curves, MVT and ingest,
  compression, decompression replay, retention; C3 consumes raw C4 PASS bytes
  only after the shipping #2123 lane completes.
- Failure paths/rollback/stale state: missing/mismatched inputs, SHA, digest,
  invocation bracket, POSIX identity or durable origin relation; parent/sibling
  substitution and OID/schema/name/window drift have no fallback; stale/partial/
  private-path evidence;
  lock/statement/wall timeout, target full, process kill, relation deletion or
  decompression, catalog/path drift, mixed residency and publication failure;
  pre-mutation disposable inverse, installer-owned reconciliation/rollback only
  while an in-progress private authority remains live, then stop-and-preserve
  after terminal installation.
- Evidence/audit/readiness: #2137 local fixture, schema, binder, readonly-seam and
  selector-mutant evidence does not claim live PASS; #2224 unit SQL shape cannot
  substitute for its isolated PG 15.2 / TimescaleDB 2.10.2 result-and-plan proof,
  and neither can substitute for a fresh production G1 after merge; current-run exact-SHA
  isolated rollback probe, invocation-bracketed schema-valid receipts, same-time
  dual-device governance, exact-SHA node-27 C1-C4/cold gates and merged rollout
  evidence remain #1895 live obligations.

Regression rows:

- Eligible hot-compressed complete group -> one transaction -> all members cold,
  values/count/checksum unchanged, schema-valid receipt.
- Exact-cutoff compressed group -> eligible; later/newer or uncompressed group ->
  ineligible with zero mutation; missing watermark -> stable refusal.
- Empty/no-index/multi-index/already-cold and same-window cross-hypertable groups
  -> complete deterministic accounting, fair bounded progress, no rewrite for
  already-cold.
- Any member missing/drifted/mixed, target missing/wrong device/unwritable/full,
  lock/timeout/kill, or receipt publish failure -> no false success, no later
  group or timer resume, and a current-run recovery-classified receipt or
  preserved authoritative topology; a prior clean receipt cannot satisfy GO.
- #2137 merged G0 chain -> production census derives durable identities and fresh
  count without target preflight; C1-C3/G8 schemas and binders reject wrong
  SHA/digest/bracket/identity, stale/partial/private-path evidence; C3 consumes
  rather than reimplements the promoted #2123 C4 lane.
- #2137 readonly extraction and CI routing -> canonical exports plus
  `run_display_route_smoke`, `importlib` and `psycopg2` patch seams remain intact,
  and every changed acceptance owner selects asserted tests rather than a
  collect-only fallback.
- Pre-merge task 4.0 -> no node-27/node-22 access and no census/probe/live receipt
  claim; only merged task 4.0 permits the post-merge 4.1 entrypoint.
- #2224 origin-parity repair -> every production caller requires the current
  durable origin identity, executes one business-row aggregate from that physical
  origin with no parent/compressed-sibling fallback, and under isolated 2.10.2
  large-sibling data produces target-sensitive, sibling-independent results plus
  a plan naming no sibling; finite production timeouts remain unchanged.
- Fresh pre-install and post-install census -> exactly the same six durable keys,
  complete-source preimages and inventory/parity inputs with no extra eligible
  key, or zero movement and terminal NO-GO.
- Cold decompression + replay + recompression -> every resulting member cold;
  retention/drop -> no orphan relation/catalog/file remains.
- New active chunks after installation -> `pg_default`; neither hypertable has
  `nhms_cold` attached.
- Unchanged ingest/display/retention consumers -> same identities, windows,
  valid-time frontier, and #1342 performance gates.

Boundary-surface checklist:

- Shared helper roots: one group resolver/migration transaction module, owned by
  #1893 after #1892 freezes the tested sequence; bounded private evidence/file
  publication primitives reused by #2137 without weakening existing consumers.
- Public entrypoints: one residency lifecycle lane, one installer/preflight and
  #2137 census/C1-C3/G8 acceptance CLIs; no duplicate mutation timer or C4 lane.
- Read/write surfaces: catalog/OID resolution, mandatory origin-qualified parity
  reads in census/runtime/post-target, relation files, tablespace paths, private
  receipt publication and readonly validation; no business-row rewrite.
- Staging/publish/rollback: database transaction + post-commit reconciliation;
  no-follow/no-clobber current-run receipts; C3 binds C4 bytes after C4 PASS;
  disposable inverse before live mutation; installer-owned recovery only for a
  still-live in-progress authority; quiesced stop-and-preserve after terminal
  installation, including a zero-group post-install abort.
- Evidence boundaries: catalog/inventory/parity -> census; display/DB/publication
  identity -> C1-C3; promoted #2123 producer -> C4; image/cluster identity ->
  probe; watermark/config/group identity -> runner receipt; RAID/mount/backup
  identity -> install receipt; exact SHA + parity/performance -> rollout receipt.
- Stale/idempotency boundaries: wrong SHA/bracket/digest/private path,
  selection-to-lock drift, already-cold no-op, partial state recovery, receipt
  failure after commit and natural next tick.
- Unchanged consumers: canonical readonly imports and patch seams, existing
  private-file consumers, active ingest, compression eligibility, retention
  window, public API/frontend/MVT and node-22/Slurm.

## Migration Plan

1. #1892 creates and reviews this fixture, runs the isolated 2.10.2 probe,
   freezes the supported sequence, updates ADR/runbook, and lands executable
   contract tests. No live cluster mutation.
2. #1893 implements the shared group primitive, runner, receipt/config/systemd
   serialization, and unit + isolated-cluster integration tests.
3. #1894 implements and tests fresh installation/rollback, container identity,
   RAID/SMART/backup gates, and dual-device governance without moving chunks.
4. #1929 corrects the #1893 target inspector before rollout: production config
   requires the measured numeric runtime UID/GID; one bounded inspect proves that
   pair and the bind, the same pair executes writability, and schema `1.1` records
   it while retaining read compatibility for historical `1.0` recovery evidence.
5. The dedicated #1895 Python/readiness child #2137 merges the executable G0 runbook,
   census, C1-C3/G8 owners, schemas, binders, shared evidence/file primitives and
   selector/tests without node-27 access. It consumes the C4 capability already
   merged through #2123 and the classification clarification from #2130; it does
   not reimplement the frontend producer or claim a live receipt.
6. The first #1895 G1 observation ended NO-GO before census publication because
   parent-hypertable parity reached its finite statement timeout. Child #2224
   closes that interpretation gap, updates the executable G0/G1 STOP, and merges
   mandatory origin-qualified production parity plus its isolated 2.10.2 oracle.
7. Only after #2224 merges, #1895 starts a fresh window from G0 at the new exact
   reviewed SHA, performs the controlled live install/migration, consumes the
   pre-mutation disposable inverse proof, and validates installer recovery
   boundaries, automatic convergence, C1-C4 and display/performance.
8. Archive this shared OpenSpec change only after #1895; the readiness child leaves
   tasks 4.1-4.8 unchecked so unexecuted live behavior is never published as done.

## Open Questions

- #2224 has no product-choice blocker. Whether direct origin or `ONLY` origin is
  the accepted SQL form is deliberately decided by the isolated TimescaleDB 2.10.2
  result-and-plan oracle: the form must preserve transparent compressed business
  rows and exclude sibling chunks. The fixture does not preselect an unproven form.
- None for #1929. Node-27's current numeric runtime identity is measured as
  `1005:1005`, but the fixture does not hard-code that deployment value: #1895
  must re-observe it and place the same explicit pair in the mode-0600 environment.
  The #1892 movement sequence and #1893 runner semantics remain unchanged; #1895
  still owns installation and measured live reserve values.
