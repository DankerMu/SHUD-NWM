Fixture level: high
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent

Seams under test:

- Isolated-cluster probe CLI/test boundary: pinned image + isolated paths ->
  measured PG 15.2 / TimescaleDB 2.10.2 movement and lifecycle verdicts.
- Shared residency-group module: catalog snapshot -> complete group or stable
  blocker; group + target -> transactional move/reconciliation result.
- Runner CLI/wrapper: dry-run/enforce config, including required numeric runtime
  UID/GID -> schema-valid receipt and bounded DB effects.
- Target inspector: one bounded Mounts + strict numeric `Config.User` observation
  -> exact `uid:gid` writable probe -> observed schema-1.1 target evidence.
- Installer/governance CLI: host/container/catalog evidence -> NO-GO or exact
  topology receipt.
- Local G0 readiness chain: production census owners + C1/C2/C3/G8 CLIs,
  schemas and binders + shared private-file primitives + canonical readonly
  facade + CI selector -> fail-closed current-run evidence without remote access.
- Origin-scoped production parity (#2224): parent-derived validated column
  inventory + mandatory current `CatalogChunk` durable origin schema/name/OID/
  window -> one bounded business-row aggregate over only that physical origin,
  with no default, parent or compressed-sibling fallback. Census, runtime
  preflight/locked/post-commit/reconciliation and post-target observations all
  use this same owner.
- Physical parent admission (#2290): canonical narrow river/current forcing
  catalog OID + Timescale hypertable ID + actual columns -> mandatory bound
  inventories and constrained origin membership, with no legacy/name-only
  fallback. Detailed matrix/evidence: `fixtures/issue-2290.md`.
- Node-27 rollout (tasks 4.1-4.8 only): merged reviewed SHA + approved
  maintenance inputs -> live parity/performance/timer receipt.

## 1. #1892 — Freeze the TimescaleDB 2.10.2 contract

- [x] 1.1 Create the expanded OpenSpec fixture, complete risk-pack selection, invariant matrix, boundary checklist, and pass one read-only fixture review plus strict validation.
- [x] 1.2 Add an automated isolated-cluster integration probe that refuses the live container/port/paths, pins the exact node-27 image digest and PG/TimescaleDB versions, creates its own PGDATA/cold/hot storage, and always cleans up its container and directories.
- [x] 1.3 Probe and record `timescaledb_experimental.move_chunk`, direct
  compressed-member ALTER, decompress-first, internal attach, two-transaction,
  and shell-first alternatives; freeze the single accepted shell-first
  transaction (lock/revalidate -> move origin shell/indexes -> decompress ->
  prove expanded cold -> recompress -> prove new complete cold group/parity ->
  commit/fresh readback), with every rejection and transient state evidenced.
- [x] 1.4 Prove normal lifecycle behavior in the isolated cluster: hot compression, complete cold move, cold read, cold decompression, replay write, recompression, repeated convergence, move-back, and `drop_chunks`, with row/value/checksum and member residency parity.
- [x] 1.5 Prove boundary behavior: exact cutoff contract, empty chunk, no user index, multiple/quoted indexes, owned TOAST, already-target no-op, and same-window chunks in both business hypertables.
- [x] 1.6 Prove failures and concurrency at shell-move, post-decompress and
  post-recompress stages: pinned image/server/extension drift, missing/wrong
  target, a safely injected catalog/path mismatch before mutation, bounded
  full-filesystem fault, cold expansion plus hot PGDATA/WAL headroom refusal,
  permission error, lock conflict, statement timeout, process/connection
  interruption at pre-commit and post-commit acknowledgement boundaries,
  relation disappearance, and injected mid-group failure; fresh readback plus
  target-window parity must prove original-sibling rollback or new-sibling
  committed target, otherwise yield an explicit mixed/unknown blocker without
  false success.
- [x] 1.7 Commit `probe-1892-throwaway.md` with exact image/server/extension
  identity and digest, commands, target-window count/aggregate/all-business-column
  checksum, before/intermediate/after relation/index/TOAST residency and bytes,
  query/lifecycle results, lock/timeout/WAL observations, cleanup proof, accepted
  sequence and rejected alternatives; the probe's PASS predicate must
  machine-check every required row rather than merely record it.
- [x] 1.8 Amend ADR 0002 and the tiering runbook: retire stale live-`ghdc`
  wording, distinguish the new DB-only tier from #1309/#1370 archive retirement,
  freeze the probe-supported sequence, require root `mdadm --detail` plus
  both-member SMART evidence, forbid hypertable attach, and state that
  PGDATA-only backup is incomplete.
- [x] 1.9 Run `openspec validate compressed-chunk-cold-tablespace-tiering
  --strict --no-interactive`, focused local collection/contract tests, the pinned
  2.10.2 integration suite on the isolated node-27 cluster, and `uv run ruff
  check .`; record exact results and no stranded container/directory/red-proof
  stash.

## 2. #1893 — Implement bounded cold-residency convergence

- [x] 2.1 Implement the production catalog/parity/transaction owner in
  `packages/common/compressed_chunk_cold_runtime.py`, consuming the #1892 pure
  contract and its sole shell-first sequence. It must resolve complete OID/member
  mappings, perform read-only validation of the fixed target catalog/container/
  host-path/device identity, lock in stable order, revalidate under finite local
  timeouts, reconcile source/target/mixed/unknown outcomes, and derive every
  non-dropped user column in physical order from both live hypertables before
  any mutation.
  It must validate that `valid_time` is the sole open Timescale dimension and has PostgreSQL type `timestamptz`, bind the inventory
  descriptor/digest to window count/non-null counts/checksum, and never import
  the probe-private four-column helper. Production parity must be a database-side
  bounded single-row aggregate; client code must not fetch/materialize all rows.
  After heap locks and before movement SQL, re-derive both inventories and the
  target-window parity in the moving transaction and require exact preflight
  equality.
- [x] 2.2 Implement `scripts/node27_cold_residency.py` plus
  `scripts/node27_cold_residency_once.sh`, dry-run by default, using the existing
  display business watermark and compression lag. Scan bounded per-hypertable
  catalog input, assign oldest-first rank within each hypertable, and merge all
  catch-up candidates by stable `(per_hypertable_rank, range_end, hypertable,
  origin_oid)` order. Record no-write `already_cold` observations without
  consuming the mutation bound, enforce a positive per-tick mutation
  bound and maximum member count, and apply finite statement/wrapper budgets.
  Require positive `NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES` and
  `NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES` with no implicit Python, shell, or
  example-template defaults; Issue #1895 supplies measured live values before
  deployment. Freshly sample cold/hot free bytes immediately before every group;
  never reuse one sample across multiple rewrites.
- [x] 2.3 Define `schemas/timeseries_cold_residency_receipt.schema.json` and
  normal/no-op/intent/partial/error examples. Bind exact head/config/cluster/
  target identity from a required real inspector (expected config cannot be its
  own observation), validated business-column inventory and per-window parity,
  complete before/intermediate/after member residency and bytes, capacity
  inputs/decision, durations, result/deferred/error/recovery fields, and stable
  redaction. Before enforce mutation, atomically write a same-directory mode-0600
  intent sidecar and replace the public receipt with the same schema-valid
  `in_progress` payload. For every planned mutation, intent must include the
  original compressed sibling/member snapshot, source residency, preflight
  inventory/parity and actual per-group capacity decision so startup can prove
  complete-source rollback rather than comparing an after-state to itself. The sidecar is authoritative until a freshly reconciled
  terminal receipt is durably published and the sidecar is durably removed with
  parent-directory fsync and identity verification. On
  startup, an existing sidecar must be fresh-reconciled and terminally published
  before new selection; mixed/unknown blocks the tick. Publication failure is
  non-success, never triggers mutation replay, and cannot leave an older success
  looking current.
- [x] 2.4 Add `packages/common/node27_timeseries_lifecycle_lock.py` with fixed
  mutex `/tmp/nhms-node27-timeseries-lifecycle.lock`. Compression, cold
  residency, retention, and manual decompression/replay must acquire it before
  any existing lane-local or database relation lock, assert the fixed file is a
  no-follow regular file owned by the effective user with mode 0600, and release
  it on every terminal path; autopipe remains outside the flock because it cannot write an eligible
  compressed group and is fenced by transactional revalidation. Add cold
  residency as the second sequential `ExecStart` of the existing compression
  oneshot, after compression, using the existing 04:25 timer and no new timer.
  Assert each wrapper wall exceeds its statement wall plus cleanup margin and the
  one service wall exceeds both sequential wrapper walls plus the systemd margin.
  Move the existing retention timer after that worst-case service window and
  retain lifecycle-lock refusal as the runtime backstop. Do not change the
  retention window and do not attach `nhms_cold` to a hypertable.
- [x] 2.5 Test normal migration, already-cold no-op, catch-up, exact cutoff,
  empty selection, bound/fairness, maximum member count, all legal states,
  selection races, partial recovery, capacity/lock/timeout/disappearance errors,
  multi-group free-space shrinkage, bounded single-row parity, locked inventory/
  parity drift, real target-inspector failure, pre-movement SQL event identity,
  unresolved-intent source/target startup, durable unlink failure, every early
  error replacing stale success, and receipt-publication failure.
- [x] 2.6 Run focused unit/schema tests, the isolated PG 15.2 / TimescaleDB 2.10.2
  integration suite, strict OpenSpec validation, and `uv run ruff check .`;
  attach normal/no-op/intent/partial/error receipt examples.

## 2A. #1929 — Bind target writability to numeric runtime identity

- [x] 2A.1 Require explicit non-root
  `NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID/GID` integers for dry-run and enforce
  before any database connection; propagate them through `RunnerConfig` and
  `RuntimeConfig`. Require each decimal component in `1..4294967294`; reject
  missing/empty/whitespace/named/non-integral/Python-bool/negative/above-bound/
  either-zero/one-component-only input with no `postgres`, root, image-default,
  UID-only, or implicit fallback. Expose both keys unassigned
  in the public env example for #1895 to fill after fresh measurement.
- [x] 2A.2 Replace the mount-only production observation with one bounded inert,
  small Docker inspect projection that stays inside the existing 5-second/64-KiB
  ceilings and parses exactly one cold bind plus strict numeric `Config.User`;
  reject missing/empty/named/UID-only/malformed/either-root/mismatched identity
  before running `test -w`, then execute that check as the same `<uid>:<gid>`.
- [x] 2A.3 Carry observed `container_exec_uid/gid` through `TargetIdentity` and
  target receipt evidence. New writers/examples use schema `1.1`; the shipping
  schema/readers accept historical `1.0` and current `1.1`; `1.0` target objects
  omit the fields, observed `1.1` requires both non-root integers, and unobserved
  `1.1` requires both present as null without expected-config echo.
- [x] 2A.4 Test dry-run and enforce preflight for the discriminating
  image-`postgres=1000:1000` / expected+observed runtime `1005:1005` /
  owner-matched mode-0700 path case; assert exact numeric argv, the complete
  env/Python and inspect refusal matrices before writable/SQL, config tombstone/
  redaction, 1.0-omit/1.1-observed-required/1.1-unobserved-null schema
  compatibility, shipping examples, fixed inspect ceilings, and selector
  producer-consumer closure.
- [x] 2A.5 Run focused target/runtime/CLI/schema/selector tests, full pytest, Ruff,
  strict OpenSpec, and a node-27 live read-only/disposable receipt proving current
  `Config.User`, numeric writable success, named `postgres` failure, unchanged
  live container identity, and zero DDL/chunk movement.

## 3. #1894 — Install and govern the fresh cold tablespace

- [x] 3.1 Implement dry-run-default installation/preflight for `nhms_cold` with fixed host/container paths, empty non-symlink directory, exact owner/mode/device, root RAID/SMART evidence freshness, capacity/rollback budget, and backup coverage gates.
- [x] 3.2 Implement exact raw-container config snapshot/diff/recreate/ready/rollback handling that preserves image, env, ports, mounts, limits and restart policy while adding only the cold bind and refusing empty-directory shadowing.
- [x] 3.3 Implement `CREATE TABLESPACE` and readback validation for catalog location, current container bind source, host device and writability; prove neither business hypertable is attached and new chunks remain in `pg_default`.
- [x] 3.4 Extend governance to sample `/home` and `/data/GHDC` together and separately report filesystem capacity, PGDATA, cold relation bytes, object-store and shared residual use, plus trend/threshold evidence.
- [x] 3.5 Detect dangling catalog/bind/filesystem identities, stopped-container stale mounts, degraded/rebuilding/unknown RAID fixtures, SMART failures, permission/capacity faults, and PGDATA-only backup gaps; all live-precondition failures are NO-GO.
- [x] 3.6 Document and test rollback that stops writers/timers, restores the prior container, verifies catalog/read paths, never deletes a referenced path, and never binds an empty directory over valid data.
- [x] 3.7 Run disposable install/rollback tests with this minimum checked-in fixture matrix and verification set:
  - pinned-image synthetic-mount container;
  - `mdadm --detail` healthy `[UU]`, degraded, rebuilding, recovering/reshaping, missing/substituted-member, and unknown cases;
  - two-member SMART PASS, one-member FAIL, and one-member unknown;
  - correct/wrong/missing mount, symlink, nonempty, and wrong owner/mode/device paths;
  - catalog absent, expected, drifted, and dangling `pg_tblspc` targets;
  - PGDATA-only and PGDATA-plus-all-target backup inventories;
  - stopped-container stale mount, rollback empty-shadow, and referenced-path deletion attempts;
  - exact container config diff, installer normal/already-ready/NO-GO/progress/rollback/error receipts, governance healthy/drift receipts, selector ownership, strict OpenSpec, focused pytest, and `uv run ruff check .`.

## 4. #1895 — Controlled node-27 rollout and closure

- [x] 4.0 Merge child #2137 before any node-27 access. That atomic child owns the
  executable G0 runbook contract; production pre-target census and C1-C3/G8
  Python owners and CLIs; C1-C3 schemas/examples/binders; bounded private receipt
  and file-publication primitives; the canonical readonly-validator
  behavior-preserving split; complete CI selector routing; tests, review and
  local evidence. Its census derives durable keys and capacity inputs from the
  production catalog/inventory/parity owners without target preflight. Its
  receipts reject missing or mismatched inputs, SHA, invocation bracket,
  identity, digest, private path and partial/stale state. C3 only binds raw C4
  PASS bytes from the capability merged by #2123 under the #2130 precedence; it
  neither embeds that frontend producer nor lets a C4 CLI PASS replace G0/C3
  SHA/digest gates. The readonly split preserves canonical exports and the
  `run_display_route_smoke`, `importlib` and `psycopg2` patch seams. The selector
  must execute every acceptance partition rather than fall back to collect-only.
  This child performs no SSH, census, probe, install, movement, live C1-C4 or
  timer work; it does not close #1895/#1891, archive this change, or check any of
  4.1-4.8.
- [x] 4.0A Merge child #2224 before retrying G1. The first node-27 G1 invocation
  at reviewed SHA `a8db554d6402bec642e9a05627eae64b2b79aec3` ended NO-GO after
  the parent-hypertable parity aggregate reached the finite 3600-second statement
  timeout; it published no census, policy or valid-times baseline, and G2-G8 did
  not run. Repair production parity so the exact quoted durable origin relation,
  not the parent hypertable or compressed internal sibling, is the `FROM` target;
  retain parent-derived physical-order user-column inventory, one aggregate row,
  deterministic count/non-null/checksum fields and the half-open range as a
  second identity fence. Origin schema/name is a mandatory, no-default input to
  the production parity builder/owner and comes from the currently resolved
  `CatalogChunk` durable identity. Missing/empty identity, the allowlisted parent,
  the current compressed sibling, or any OID/schema/name/window mismatch fails
  closed. Census; runtime preflight, locked revalidation, recompression,
  post-commit readback and reconciliation; and post-target named-group observation
  must all pass that identity to the same owner. Keep the census `3600000` ms and
  runtime `3600s` finite statement ceilings unchanged.

  Add a TimescaleDB 2.10.2 isolated discriminator with large sibling data that
  proves both executed result and execution plan: target parity is sibling-
  independent, target-sensitive and planned without sibling chunks. Compare
  direct and `ONLY` origin forms and accept only the form that preserves
  transparent business-row decompression. SQL-text unit assertions cannot satisfy
  that database oracle; unit tests instead prove mandatory identity, quoted
  identifiers, no parent/sibling `FROM`, OID/name/window drift and relation disappearance refusal. Map the catalog SQL owner, runtime owner, census owner,
  post-target owner and runbook directly to their assertion-bearing census,
  runtime and integration partitions, with non-vacuous removal mutants; a local
  or GitHub skip/collect-only result cannot substitute for the node-27 isolated
  PG 15.2 / TimescaleDB 2.10.2 result.

  Update the executable runbook G0/G1 STOP fence: #2224 must merge first, the
  `a8db554d6402bec642e9a05627eae64b2b79aec3` failed census/bracket and absent
  policy/baseline cannot be reused, and a fresh maintenance window starts from G0
  at the new exact merged SHA. Pass fixture review, strict OpenSpec, focused/full
  tests, Ruff, independent review/verifier/Gap Sweep and exact-head CI, merge the
  child, then restart #1895 from G0. Local or isolated PASS does not satisfy the
  fresh production G1 retry.
- [ ] 4.0B Merge child #2290 physical-parent admission before fresh production G0.
  Complete the `fixtures/issue-2290.md` invariant matrix: narrow signature and
  mandatory parent OID/Timescale ID; digest-bound inventory with unchanged wire
  keys; all selection/reload/locked/fresh/census/post-target/intersecting-group
  callers migrated; pre-expand/wide-with-keys/legacy/missing/ambiguous/substituted
  identity refusal; actual ranges and current forcing preserved; stale artifacts
  unable to authorize mutation/PASS. Require fixture review/strict validation,
  tests-first node27 red/green, existing #2224 role/result/plan/lifecycle proof on
  a narrow fixture, actual isolated rename/replacement/legacy-exclusion proof,
  every-owner selector/removal closure, independent review/verifier/Gap Sweep,
  exact-head CI and merged cold-only STOP/runbook. No production execution.
- [ ] 4.0C After #2290, merge child #2291 reviewed-count/baseline cutover before
  fresh production G0. Preserve explicit bounded `--require-count` input with no
  default or observation-derived expectation; validate/freeze the original
  count/keys/groups/capacity set and pass its count through all four readiness
  owners and CLIs. Replace historical-six active assumptions and align G3's
  independent current count to the admitted physical-parent population, with
  extra/missing/duplicate/stale/set/digest drift refusal and no truncation.
  Preserve per-tick1, E/S/2E arithmetic, actual ranges, timeouts and wire schemas.
  Require node27 boundary/consumer/isolated-population evidence plus the normal
  fixture, selector-removal, review/verifier/final/CI gates; no production access.
- [ ] 4.1 At the reviewed SHA, after 4.0A-4.0C merge and external readiness is supplied, execute the merged live runbook for the first
  node-27 observation. Before access, its exact deployed SHA must have passed
  the #2137 issue-specific fixture review, strict OpenSpec validation, contract
  tests and normal CI. The runbook must provide: a pre-target census using the
  production catalog/inventory/parity owners (`ranked_candidates_from_execute`,
  `derive_bound_inventories`, `collect_residency_group`,
  `compute_window_parity`, `compression_before_bytes`,
  `retained_source_bytes`) without calling target preflight; the complete #1894
  installer argv; canonical-decimal env assembly; one-group-at-a-time
  invoke/readback/halt commands; the exhaustive D9 trigger table; current-run
  receipt checks; and evidence-PR/close/archive order. It must bind rollback
  wording to the shipping installer state machine: only a failed or interrupted
  install with a still-live private authority may reconcile/roll back, while
  terminal `installed` closes that authority and every later trigger stops and
  preserves the installed topology. It must name the historical §4.3.3/manual
  `docker run` recipe as forbidden for this cold bind.

  Production root evidence is one fixed contract, never the synthetic #1894
  helper: JSON envelope schema `1.0`; hostname equal to the exact `/bin/hostname`
  output passed to the installer; `captured_at` UTC RFC3339; root:root owner;
  mode `0600`; maximum age `900` seconds; exact leaf argv
  `/usr/sbin/mdadm --detail /dev/md0`, `/usr/sbin/smartctl -H <each of the two
  parsed active-sync members>`, and `/usr/local/sbin/nhms-backup-inventory
  --json`; exact subject identities; nonempty output; and backup `covered_paths`
  matching PGDATA plus the sorted catalog-derived external targets including
  `/home/postgres/pgdata/tablespaces/nhms_cold`. A missing producer or mismatched
  owner/mode/argv/subject/hostname/freshness/coverage is NO-GO.

  The two device identities are deliberately different fields and must never be
  copied between configs. `INSTALLER_DEVICE_IDENTITY` is the exact
  `device_identity` returned before install by
  `node27_cold_tablespace_host.inspect_host_path()` for the absent production
  child (mount identity `major:minor:mount-id:source`) and feeds installer
  `--expected-device-identity`. After install,
  `RUNNER_DEVICE_IDENTITY` is freshly returned by
  `compressed_chunk_cold_target.inspect_host_path()` for the created directory
  (descriptor identity `st_dev:st_ino`) and alone feeds
  `NODE27_COLD_RESIDENCY_DEVICE_IDENTITY`. Installer `--expected-mode` is `0700`;
  `--expected-uid/gid` and runner UID/GID are the freshly observed canonical
  numeric `.Config.User` pair, never the historical `1005:1005` text.
  At the reviewed SHA, capture the read-only live preflight: clean worktree,
  container config/image/runtime UID:GID, cluster/catalog, every
  candidate/hot group identity/member residency/rows/checksum, both filesystems,
  timer/writer/lock state, backup readiness, fresh root RAID/SMART evidence, API
  valid-times/publication and #1342 baselines. The six compressed groups observed
  on 2026-08-29 are a historical count, not reusable identities: the fresh
  census must resolve exactly six complete eligible source groups and freeze
  each durable key, current sibling/member digest, production inventory/parity,
  `before_compression_total_bytes` and `retained_source_bytes`; missing, extra or
  unexplained drift ends this window as NO-GO, never an arbitrary oldest-six
  subset. Let `E` be the checked positive maximum expansion and `S` the checked
  positive sum of all six retained-source byte values; freeze canonical decimal values
  `COLD_RESERVE=E`, `WAL_RESERVE=E`, `INSTALL_REQUIRED=S`, and
  `ROLLBACK_HEADROOM=2*E`, rejecting overflow or any stale/probe-derived value.
  `WAL_RESERVE=E` is an intentionally conservative same-order proxy derived from
  fresh live expansion, not a measured or per-group-attributed WAL value; no LSN
  probe or the disposable 165736-byte observation participates.
- [ ] 4.2 Before any live service, container, catalog or relation mutation, rerun
  `scripts/probe_compressed_chunk_cold_tablespace.py --mode isolated-cluster` at
  the exact reviewed SHA with pinned image and isolated names/port/paths. Require
  a current-run PASS proving complete `nhms_cold` movement, inverse `pg_default`
  move-back, parity, and owned container/path cleanup; historical #1892 evidence
  cannot satisfy this gate. Then live-read-only recheck the engine and group
  contract inputs used by that primitive.
- [ ] 4.3 Stop and drain autopipe/compression/residency/retention writers under
  the documented mutex/lock order. Any active writer, conflicting lock, unknown
  health, insufficient worst-case rollback space or identity mismatch is NO-GO;
  units remain at this quiesced state until a written GO, a proven pre-install
  no-mutation abort, or a completed installer-owned rollback receipt authorizes
  restoration.
- [ ] 4.4 Establish the fresh `nhms_cold` bind/tablespace only through the #1894
  installer using the 4.1 values, and deploy #1893/#1929 at the exact reviewed
  SHA. Re-observe numeric runtime identity and prove catalog/bind/device identity,
  no hypertable attach and new-chunk `pg_default` placement. An installer failure
  or interruption with a live private authority is reconciled only by the same
  installer contract. A terminal `installed` receipt closes and removes that
  authority; every later trigger, including a post-install pre-movement failure
  with zero groups moved, stops and preserves the installed topology rather than
  invoking a fictional operator rollback. The historical manual container recipe
  is never an alternate install or rollback path.
- [ ] 4.5 Run a dry-run preview and, before the first movement SQL, re-census the
  same six 4.1 keys as complete source with the same inventory/parity inputs and
  no unexplained extra eligible group. Set `PER_TICK_BOUND=1`; issue exactly one
  enforce invocation per group and do not issue the next until the unique
  current-run receipt passes head/time/config/outcome, complete residency,
  parity, duration/wait/bytes and hot/cold filesystem reconciliation. Any D9
  trigger stops all later groups, forbids ad hoc SQL and path/catalog deletion,
  leaves every writer/timer quiesced and routes a reviewed owning-implementation
  response. The shipping runner has no live move-back entrypoint.
- [ ] 4.6 With recurring timers still stopped, prove all active/uncompressed
  groups remain wholly `pg_default`, perform one bounded controlled ingest smoke
  and re-quiesce its writer, then pass node-27 real-DB pytest, C1-C4, hot/cold
  curve/MVT/click flows and #1342 plans/latencies. Both hot and cold plans must
  have no Seq Scan or all-chunk decompression regression; require buffers <=
  5000, SQL P95 <= 300 ms, local API P95 <= 500 ms, frontend click P95 < 2 s,
  non-regressing valid-times and complete current GFS/IFS publication counts.
- [ ] 4.7 Only after 4.6 has a written preliminary GO, restore the original
  autopipe/compression/retention timer enablement and observe at least one natural
  serialized tick plus one newly terminal group's automatic convergence or a
  catalog-proven truthful no-op. Require current-run schema-valid receipt, active
  timers and no issue-owned failed unit; any trigger returns to the 4.3 quiesced
  state rather than continuing or patching production.
- [ ] 4.8 Post schema-valid current-run live receipts and final GO/NO-GO with exact
  deviations/triggers. Historical #1894/#1929 receipts may anchor contracts but
  cannot satisfy a 4.x observation, and #1938 is a non-blocking parser follow-up:
  every manual value is short canonical decimal and every GO receipt must match
  this invocation's reviewed SHA, bracketed `generated_at`, mode/outcome and exit
  status rather than a pre-existing clean file. Merge the evidence PR while the
  shared change is still strict-valid; then close #1895, update/close #1891, and
  archive this shared OpenSpec change in the immediate post-merge follow-up only
  after its final strict validation gate.

## Risk-pack evidence mapping

- Public API / CLI / config: tasks 2.2, 2.4, 2A.1-2A.2, 3.1-3.3 and
  4.0. The 4.0 evidence covers `node27_cold_residency_census_policy.py`,
  `packages/common/node27_issue1895_*.py` and every corresponding census/C1-C3/G8
  CLI; missing inputs, invalid canonical values or a wrong SHA fail before remote
  access, publication or mutation.
- File IO / path / permissions / secrets: tasks 1.2, 2.3, 2A.1-2A.4, 3.1-3.2
  and 4.0. The 4.0 evidence covers `packages/common/evidence_io.py`,
  `packages/common/safe_fs_publication.py`, private receipt owners and readonly
  DSN binding; symlink, alias, mode, nlink, replacement, stale/private-path and
  secret-bearing cases fail without unsafe overwrite or disclosure.
- Schema / evidence identity: tasks 1.7, 2.3, 2A.2-2A.5, 3.1-3.7, 4.0 and
  4.8. The 4.0 evidence covers all C1-C3 schemas/examples/binders and requires
  exact identity, digest and invocation bracket. Historical 1.0/current 1.1
  residency evidence, installer private authority and live receipts retain
  schema validation, redaction, durable publication and semantic readback.
- Concurrency / resources / rollback: tasks 1.6, 2.1-2.5, 3.5-3.6, 4.0 and
  4.2-4.6. The 4.0 current-run owners prove ordered publication and allow C3
  binding only after raw C4 PASS bytes exist; lock, timeout, full, interruption,
  race, stale or partial output yields explicit non-PASS state.
- Legacy/display compatibility: tasks 2.4, 2A.3, 3.3, 4.0 and 4.5-4.7. The
  4.0 evidence covers `services/production_closure/readonly_db_{types,probe_adapter,
  permission_probes,merge,route_smoke,validation}.py`, preserving public exports
  and `run_display_route_smoke`/`importlib`/`psycopg2` patch seams. It consumes
  the promoted #2123/#2130 C4 contract and never reimplements that producer.
- TimescaleDB/time-series domain: tasks 1.3-1.6, 2.1-2.5, 4.0, 4.0A and 4.4-4.7.
  The 4.0 census uses production catalog/inventory/parity owners without target
  preflight; 4.0A closes the live-discovered parent-hypertable scan by proving
  transparent business-row parity from the exact durable origin relation while
  keeping the finite timeout; six remains a fresh count gate rather than reusable
  identity.
- Published NHMS identity: tasks 4.0 and 4.6-4.8. C1-C3/G8 identity and digest
  binders, source-scoped current publication and raw C4 bytes prove the local
  acceptance chain first and exact-SHA live display identity only after merge.
- Documentation/migration/backup: tasks 1.8, 3.6, 4.0, 4.0A and 4.1-4.8. The 4.0
  evidence covers the executable G0 text and `scripts/select_ci_tests.py` exact
  acceptance partitions/removal mutants; C4 uses `test:e2e:live-c4-display`, not
  legacy `e2e/monitoring.spec.ts`, and no production access precedes all gates.

Non-goals:

- No TimescaleDB/PostgreSQL upgrade, row-schema change, archive-lane restoration,
  active chunk/PGDATA/WAL/object-store move, node-22/Slurm change, automatic
  decompression, or compression-lag/retention/display-contract change.
