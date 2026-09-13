# Mandatory selective-cold retirement — active implementation contract

Status: proposed target only. This revision updates documentation/specification
and removes one obsolete bringup-checklist wording test and its private helper.
The merged cold runtime remains present; runtime retirement is still pending.
The sole executable retirement plan is this file. Old cold deployment approval
does not authorize retirement, and disabling a lane is not code retirement.

## Publication DAG and smallest safe merge boundaries

R1/R2 work may be prepared concurrently; this is not permission to publish an
R2-only destructive interface change. R4.3's fresh fixture review is the entry
gate before implementation, regardless of its position in the numbered groups.

| Publication slice | Prerequisite | Minimal mergeable scope and proof |
|---|---|---|
| Owner-transfer PRs (R1) | R4.3 fixture approval | One retained-owner transfer with all actual callers, tests, selectors and contracts; preserve still-used callers until their cutover, without a legacy facade. |
| Compression cutover and dependent retirement (R2 + dependent R3 rows) | Only retained-consumer transfers needed by the removed paths | Single-lane preflight/budget/unit/compression wrapper together with cold runner/wrapper removal and all affected direct/transitive tests, config, schema and selector edges. No intermediate merge keeps a caller of removed paired-budget APIs. |
| Other retirement slices (remaining R3) | Each deleted owner's actual R1 transfers; C4 acceptance before G0/C3 removal | Delete complete caller/owner closures with matching contracts and tests; unrelated SQL, governance or G7 deletions need not wait for compression cutover. |
| Effective handoff and closure (R5) | Runtime-retirement proof and R4 authority updates | Separately approved actual deployment disposition and linked survivor evidence; no source-only closure. |

Suggested fixture level for implementation slices: expanded; effective repair
intensity: high. Atomicity follows actual dependency closure, not whole task
groups. Do not split a closure across PRs leaving broken imports/CLIs/tests, or
serialize unrelated owner transfers. A single integration owner serializes shared
`select_ci_tests.py` and contract mutations; tests/docs accompany each merge.
An owner-transfer slice cannot silently remove a shared export still used by
unretired callers. The final post-merge compression launch needs no cold env,
and no unit, executable or retained test depends on `--launch cold` or paired
budget fields. Real launcher and collection/survivor proofs are required.

## R1 — Transfer minimal surviving consumers to their actual owners

- [ ] R1.1 Migrate `packages/common/node27_pgdata_host.py` and
  `node27_pgdata_migrate.py` off cold imports. Transfer `run_bounded_command` and
  required process-kill/pipe/output-bound closure into PGDATA-owned command/host
  code; translate `ColdRuntimeError` at the PGDATA error boundary. Transfer only
  consumed `ContainerSnapshot` normalization/serialization from
  `node27_cold_tablespace_container.py` into PGDATA-owned container code.
  Exclude `with_cold_bind`, `build_recreate_argv` and cold rollback planning.
- [ ] R1.2 Transfer consumed `EvidencePolicy`, descriptor/mdadm/both-member SMART
  verification and dataclasses from `node27_cold_tablespace_evidence.py` into
  PGDATA-owned evidence code. Do not carry installer path/capacity/backup-inventory
  machinery without a real retained consumer. Use existing
  `node27_external_contract_snapshot.json` / `node27_container_contract.py` for
  the image pin, not a copied literal or `compressed_chunk_cold_residency.py`.
  Respect existing module-size boundaries.
- [ ] R1.3 Move generic `collect_filesystem`, `collect_postgres`,
  `collect_working_set`, `bytes_pretty`, `run_command` and required sampling
  helpers from `node27_cold_governance*` to resource-governance-owned code used by
  `scripts/node27_resource_governance.py`. Preserve actual PGDATA device and
  available-space binding, unknown/conflict refusal, bounded observations and
  `/data/GHDC` observation. Remove cold inventory/topology/history/receipt
  branches, `--cold-governance-*` inputs and `cold_tablespace_governance` output.
  Preserve the subsequently merged #2277/#1769 maintenance-output collection,
  effective thresholds, freshness/unavailable recommendations and behavioral
  tests when extracting this sampler; these are ordinary governance, not
  cold-only inventory. This does not assert production deployment or close
  any remaining maintenance/recovery duty.
- [ ] R1.4 Migrate/repoint manual consumers as real dependency edges: the PGDATA
  procedure in `docs/runbooks/tier-node27-timeseries-storage.md` must own its
  generic SQL/API/browser/ingest workload and evidence instructions rather than
  refer to old C1-C4/G0-G8 sections or `node27_issue1895_*` exits. Retain genuinely
  needed producers/validators with their schema/tests under existing
  display/read-only/PGDATA owners before deleting old exits; do not rename the
  entire cold acceptance stack. Preserve
  `services/production_closure/readonly_db_validation.py` and
  `scripts/validate_readonly_db_boundary.py`.
  Explicit-cycle SQL performance is a retained manual consumer: transfer any
  required query capture, canonical parameter validation, native EXPLAIN binding
  and deterministic query identity from the old recorder to this workload owner
  with behavioral proof before R3.6. Preserve meaning, not synthetic positional
  compatibility or the four-lane cold protocol. A shipping query API alone is
  not the replacement performance recorder or its identity validation.
- [ ] R1.5 Transfer behavioral tests and selector ownership with each extraction.
  Prove PGDATA clean-stop/copy verification, exact container preservation,
  pre-write restoration, no stale post-write rollback, bounded commands and
  descriptor/RAID/SMART refusals; prove governance placement/unknown/conflict
  behavior and #2273 capacity plus #1985 discovery/lag contracts. Keep protective
  rejection of incompatible legacy cold binds/services, not executable aliases.
  Independent C4 (`apps/frontend/package.json` `test:e2e:live-c4-display`,
  `e2e/live-c4-display.spec.ts`, producer/binder/schema and
  `openspec/specs/c4-live-display-evidence`) survives: old publication-current
  consumes C4, not the reverse. Generic readonly survives old C2's removal.
- [ ] R1.6 Transfer the necessary outer C4 reviewed-SHA freeze and exact-byte
  SHA-256/file-identity recheck from G0/C3 to the existing display deployment
  owner's **Bringup-C4 production acceptance** seam. R1 must ship and record its
  real public entrypoint, code owner, original binding-record provenance and
  assertion-bearing acceptance/refusal tests before R3 deletes the old owner.
  Freeze reviewed SHA from approved delivery/C1 evidence before execution,
  bind the initial accepted C4 bytes/identity/inputs/bracket, then recheck against
  that record at final acceptance. Missing bindings, mismatched SHA or changed
  bytes/identity refuse; do not derive new expectations from the final read.
  Keep the independent C4 closed schema/CLI unchanged, do not restore broader
  retired C3 checks, and never substitute local C4 CLI PASS for this outer gate
  or independent C1-C3 proof. Apply the matching `c4-live-display-evidence`
  MODIFIED delta with this transfer; generic “C4 survives” wording is not proof.

## R2 — Detach normal compression while preserving safety

- [ ] R2.1 Change `infra/systemd/nhms-node27-timeseries-compression.service`,
  `scripts/node27_timeseries_compression_once.sh`,
  `scripts/node27_timeseries_budget_preflight.py`,
  `scripts/node27_timeseries_compression.py`,
  `packages/common/node27_timeseries_sequential_budget.py` and
  `infra/env/node27-timeseries-compression.example` to one compression-owned
  budget/launcher. Remove paired-env loading, cold arguments/defaults/mirrors/
  assembly fields and the cold `ExecStart`; migrate every
  `timeseries_compression_receipt` schema/example/consumer affected by paired
  fields. No compatibility aliases or mechanical restoration of old literals.
- [ ] R2.2 Derive a consistent single-compression budget: statement plus cleanup
  must fit the wrapper, and systemd must retain the required outer safety margin.
  Preserve descriptor-bound mode-0600/no-symlink inert env parsing, import-origin
  validation, argv execution, bounded timeout, secret-safe refusal and fixed
  lifecycle mutex before local/DB locks. Preserve ordinary compression,
  retention windows, discovery/lag and maintenance scheduling safety.
- [ ] R2.3 Migrate launcher/compression/retention tests, CI selectors and docs
  atomically. Real wrapper/CLI smoke must launch normal compression without any
  cold env and refuse malformed/unsafe compression configuration. Prove finite
  timeout/cleanup and lifecycle-lock contention behavior on surviving paths;
  do not infer effective production units from repository templates.

## R3 — Delete cold-only runtime and old G0-G8 delivery surfaces

R1 and R2 may be prepared independently; publication follows actual dependency
closures in the DAG above. In particular `node27_cold_residency.py` and
`node27_cold_residency_once.sh`, their affected direct/transitive test consumers,
config/schema and selector entries disappear in the same merge that removes
their preflight/paired-budget API. Complete only the retained-consumer transfers
needed by that deletion first. Other R3 closures may publish separately once
their own consumers are migrated; no broken executable or compatibility facade.
The source census found 95 candidate Python files (38 cold package, 6 cold CLI,
31 issue1895 package, 20 issue1895 CLI); these are families to classify after
extraction, not wildcard deletion authority or proof of dynamic completeness.

- [ ] R3.1 Delete `packages/common/compressed_chunk_cold_{residency,target,tick,
  receipt,runtime,runtime_catalog,runtime_target,runtime_timing}.py` and
  `compressed_chunk_cold_probe/`; `node27_cold_tablespace_{authority,container,
  engine,evidence,host,identity,install,integration,observation,pending,receipt,
  recovery,root_capability,topology,types}.py`; `node27_cold_governance{,_cli,
  _collection,_history,_runtime}.py`; `node27_cold_residency_census_policy.py`;
  and cold-only `packages/common/node27_issue1895_*.py` after R1 extraction.
  Delete issue1895 first or together with cold because it imports cold owners.
- [ ] R3.2 Delete `scripts/node27_cold_residency.py`,
  `node27_cold_residency_census.py`, `node27_cold_identity_observe.py`,
  `node27_cold_tablespace_install.py`, `node27_cold_tablespace_root_evidence_setup.py`,
  `probe_compressed_chunk_cold_tablespace.py`, `node27_cold_residency_once.sh`,
  cold-only `scripts/node27_issue1895_*.py` and
  `infra/env/node27-cold-residency.example`. Remove all deployable old G0-G8 exits,
  including manual/runbook callers after R1.4; no dormant stubs or re-exports.
- [ ] R3.3 Remove the `nhms_cold` CREATE grant and positive cold-grant audit from
  `db/roles/node27_write_roles.sql` and update
  `scripts/node27_provision_write_roles.sh` description. Preserve all unrelated
  ownership, role flags, membership, trigger/default and security audits.
  Source retirement does not authorize live privilege revocation or DROP.
- [ ] R3.4 Delete `timeseries_cold_residency_receipt`,
  `node27_cold_tablespace_install_receipt`, `node27_cold_governance_receipt`,
  `node27_issue1895_c1/c2/c3` receipt schemas and matching synthetic examples.
  Delete cold-only tests/fakes/mutants, including issue1895/2224/2290/2291 tests
  whose sole contract is retired. Move surviving command/container/evidence/
  capacity/launcher tests to actual owners; remove incidental source-text or
  constant-count pins rather than repinning obsolete wording.
- [ ] R3.5 Update `scripts/select_ci_tests.py` PathTestRule/owner tuples/closure
  sets and `tests/test_select_ci_tests.py`; close shared conftest/fixture/import/
  generated and non-Python references. Every retained path must select existing
  assertion-bearing surviving suites with no stale collection imports.
  Preserve independent C4 and generic readonly suites. Leave migrations
  `000058`/`000059`, ordinary compression/retention and current data unchanged.
- [ ] R3.6 Retire the G7-only `explicit-cycle-query-binding` capability with
  `node27_issue1895_query`'s recorder/capture-validator/digest and retired
  performance/lanes callers. Delete
  `tests/test_issue2227_explicit_cycle_named_binding.py` and `ISSUE2227_*`
  selector edges in that same atomic cutover, applying its REMOVED delta.
  Its purpose and callers are #1895/G7-only; a test import is not justification
  to promote dead recorder code. Keep the real shipping forecast owner,
  native named psycopg binding, API and independent tests unchanged.
- [ ] R3.7 Close the following mixed/non-Python rows in the atomic cutover.
  These are explicit work items, not work deferred to R5's audit.

| Surface | Required disposition | Closure proof |
|---|---|---|
| `infra/env/node27-resource-governance.example` | Remove only the cold-governance option block with the Python CLI/config branch; preserve PGDATA and maintenance configuration | No retired `NODE27_COLD_GOVERNANCE_*` declarations; surviving audit config smoke |
| `docs/runbooks/current-production-ops.md` database role table | Remove cold env/CREATE grant as current runtime guidance; preserve ordinary roles and distinguish pending source removal from live revocation | Role/source/docs matrix agrees; no current instruction activates cold |
| `packages/common/node27_timeseries_lifecycle_lock.py` and `tests/test_node27_timeseries_lifecycle_lock.py` | Keep the fixed mutex implementation; update stale peer prose and remove the cold CLI test branch while retaining compression/retention/replay/PGDATA exclusion behavior | Surviving contention/override tests execute without importing the deleted CLI |
| `tests/test_node27_cold_tablespace_marker_contract.py` | Delete cold AST/source pins; move its generic Docker collection-gate behavior test into proposed `tests/test_node27_docker_collection_gate.py`, without cold fakes, and update selector ownership | New unmarked gate test proves Docker opt-in does not unlock ordinary DB integration; old cold test file/import gone |
| `scripts/diagnostic/display-cold-waterfall.sh` | Retain: “cold” means display cache/startup latency, not tablespace residency; no such npm script exists in current frontend package | No name-only retirement or change to this independent diagnostic |

The last row is an explicit exclusion, not a new implementation task. Generic
Docker collection gates in `tests/conftest.py` remain unchanged; only their
retired test container changes owner.

## R4 — Correct active authority and preserve history

- [ ] R4.1 Implement the proposed surviving-capability deltas beside this file
  with their matching source changes: PGDATA placement, compression launch/roles,
  runtime role provisioning, CI selection and C4 production-acceptance ownership.
  Canonical `openspec/specs/**`
  remains the current implemented contract until that cutover; do not claim
  this document-only revision has already removed code.
  Apply the `explicit-cycle-query-binding` REMOVED delta with R3.6 and remove
  its now-empty canonical capability, not leave a stale G7 Purpose behind.
  This does not remove shipping forecast behavior or its independent contracts.
- [ ] R4.2 Reconcile #1891/#1895, ADR 0002, storage runbook, bringup checklist
  and `docs/runbooks/current-production-ops.md` with mandatory retirement and
  actual surviving owners. The former pending
  rollout is withdrawn, not executed. Keep completed ledger below and immutable
  `evidence/**`, `fixtures/**` and `probe-1892-throwaway.md` as historical records,
  never active deployment authority. Runtime synthetic examples are not
  historical evidence merely because they predate retirement.
- [ ] R4.3 Before implementation, obtain fresh retirement-specific high-risk
  fixture/invariant review covering R1 ownership, R2 config/process/locking,
  R3 deletion/security/CI closure and manual consumers, R4 spec disposition and
  R5 deployment boundaries. Historical cold fixture approvals do not authorize
  these edits. Require fresh independent review, finding verification, Gap
  Sweep, regression evidence and exact-head CI before merge.
- [ ] R4.4 At final closeout obtain reviewed archive/disposition that preserves
  surviving-spec updates and the G7-only REMOVED delta but never promotes withdrawn
  cold-enabling ADDED requirements. There is no canonical
  `compressed-chunk-cold-residency` capability to remove. If using
  `openspec archive --skip-specs`, first apply and validate those updates/removals,
  including removal of the empty G7 capability; skipping promotion must not
  discard them. Never use `--no-validate`. Do not archive during this
  specification revision or while implementation remains pending.

## R5 — Verify survivors, authorize deployment handoff, then close

- [ ] R5.1 Audit zero active imports/entrypoints/options/CI targets for removed
  families using Python and non-Python/manual consumers. Document only justified
  protective negative guards and immutable history exceptions. Prove R1-R3
  retained behavior via real isolated runtime oracles on node27 with owned
  TMPDIR/resources, not a database inside production. Run affected local
  Ruff/OpenSpec/frontend checks plus node27 targeted, required full/backend and
  isolated PostgreSQL 15.2 / TimescaleDB 2.10.2 regression. No test/PASS or fresh
  review is claimed by this planning revision.

  Implementation-stage commands below belong on node-27 (owned checkout/venv,
  `TMPDIR=/home/nwm/tmp`, no production database substitution):

  ```bash
  uv run pytest -q tests/test_node27_pgdata_migrate.py tests/test_node27_resource_governance.py tests/test_node27_timeseries_compression.py tests/test_node27_timeseries_retention.py tests/test_node27_timeseries_lifecycle_lock.py tests/test_node27_write_roles.py tests/test_node27_external_contract_snapshot.py tests/test_select_ci_tests.py
  NHMS_RUN_NODE27_DOCKER=1 uv run pytest -q -m 'integration and timescaledb_210 and node27_docker' tests/test_node27_pgdata_migrate_oracle.py
  uv run pytest -q tests/test_node27_docker_collection_gate.py
  uv run pytest -q
  ```

  The collection-gate path is the explicit R3.7 migration target, not a file
  claimed to exist in this review revision. The PGDATA unit and oracle paths
  both exist now and are retained. The isolated command must execute real
  oracle assertions; empty selection or all-skipped output cannot satisfy it.
  Retained/transferred test paths and their selector rows move together.
  Record the actual compression-only wrapper launch/refusal smoke invocation
  using a private compression env and no cold env; `--help` alone is not
  production or database proof. For C4/manual authority, use the unchanged
  local binder's input/bracket/private-file behavioral suites and R1.6's
  real outer acceptance tests (missing/mismatched SHA or changed bytes/identity),
  plus execute each retained documented entrypoint in an appropriate safe
  mode. Reconcile actual command paths with both runbooks and the R3.7 matrix;
  do not replace the removed prose-pin test with a new wording assertion.
  On the local frontend, `pnpm test` covers its surviving C4 binder/producer
  suites; any required real C4 browser proof remains node-27/owner-authorized.
  Local contract checks are
  `openspec validate compressed-chunk-cold-tablespace-tiering --strict --no-interactive`
  and Ruff/Markdown checks on the changed surface. No command here is claimed
  to have run for runtime retirement in this contract review.
- [ ] R5.2 Separately authorize effective-deployment handoff at the final release:
  identify actual unit/dropins/env/ExecStartPre and referenced paths, coordinate
  owners and foreign holds, remove retired cold activation/configuration without
  disabling normal maintenance, and record observed disposition. Repository
  templates are not deployment proof. Do not delete real tablespaces/data, old
  PGDATA or private evidence. Unexpected deployed cold relations/state require
  STOP and a dedicated safe disposition, never silent DROP or ignore.
- [ ] R5.3 Only after affected source paths and deployed references are gone,
  dispose #2293/#2298/#1938 as capability retired, not fixed; a defect carried by
  extraction follows the surviving owner. Link deletion, regression, reviewed
  authority and deployment evidence before closing #1895/#1891. No optional
  dormant-retention backlog substitutes for code removal.

Cold samples, I9, I8, #2162 and #2017 are not blanket retirement dependencies.
No new RPO/RTO, capacity-construction or general storage-acceptance epic is
created. Existing unrelated upgrade, recovery, capacity and autovacuum duties
remain with their owners; deployment cannot bypass their actual foreign holds.

## Historical completed delivery ledger — non-normative

The entries below preserve completed delivery and original acceptance wording,
not current install/move instructions. Original fixture level and repair
intensity were high (NHMS profile; upstream level absent). Those approvals and
seams are historical only; R4.3 requires fresh retirement approval.

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
- [x] 4.0B Merge child #2290 physical-parent admission before fresh production G0.
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
- [x] 4.0C After #2290, merge child #2291 reviewed-count/baseline cutover before
  fresh production G0. Preserve explicit bounded `--require-count` input with no
  default or observation-derived expectation; validate/freeze the original
  count/keys/groups/capacity set and pass its count through all four readiness
  owners and CLIs. Replace historical-six active assumptions and align G3's
  independent current count to the admitted physical-parent population, with
  extra/missing/duplicate/stale/set/digest drift refusal and no truncation.
  Preserve per-tick1, E/S/2E arithmetic, actual ranges, timeouts and wire schemas.
  Detailed contract, selected risk packs, invariant matrix and scenario evidence:
  `fixtures/issue-2291.md`. Freeze N and held-reader whole-original-file SHA in
  the existing private G1 policy; all later original loaders require that frozen
  hash and reviewed SHA before consuming original N. Existing semantic digest,
  original provenance, current brackets and natural newly-terminal sets retain
  their separate meanings. Prove full non-six G5/G6/G8 traversal, 1/63 complete
  artifacts within unchanged bounds, malformed/duplicate/extra/replaced originals,
  real mixed-range population counts and every changed selector removal edge.
  Require node27 boundary/consumer/isolated-population evidence plus the normal
  fixture, selector-removal, review/verifier/final/CI gates; no production access.

### Withdrawn outstanding rollout — not executed

Original unchecked tasks 4.1–4.8 (fresh G0/G1 census and approval, isolated cold
move/move-back probe, live writer quiescence, fresh cold tablespace/bind install,
bounded group enforcement, live hot/cold C1-C4/performance acceptance, automatic
cold convergence and rollout close/archive) are WITHDRAWN. They are not completed
tasks or active prerequisites. The first historical G1 NO-GO remains a NO-GO;
there is no claim that later G2-G8 ran. Their old risk-pack/rollout mapping no
longer supplies active authority; R1-R5 above replaces it. Completed preparation
entries 4.0–4.0C and their evidence remain intact above.
