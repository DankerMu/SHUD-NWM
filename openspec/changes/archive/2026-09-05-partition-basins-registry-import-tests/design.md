## Context

Fixture level: **expanded** (agrees with issue #1913). Repair intensity: **high** because a
physical split can silently drop collection, helper consumers, integration markers, or the
real-database lane. The NHMS project profile routes local static/full tests to the Mac and
real-DB evidence only to node-27's local PostgreSQL `:55432`.
Node-22 remains DB-free.

The immutable source baseline is commit
`3c29698f9eda5efdd2d48f3c2922da8df0d3aa2a`. Its monolith is 3,931 lines,
156,249 bytes, and SHA-256
`c61c61f6e905ee951b9e5fb1c7566722367c8cc3e1fd07baad21bda91461f708`.
It defines 94 tests, 19 support functions, one helper class, and four private constants, and
collects 96 unique nodes. The immutable ignored contract is
`.workplans/issue-1913/baseline/contract.json`, SHA-256
`42803dd59276621d559bf6719b4c31cccc64ad751ed0f46105c373ba7b17c60c`.
It is captured twice byte-identically from a disposable Git-archive snapshot with database
and real-Basins opt-ins scrubbed. Stable suffix and integration-suffix digests remain
`ba89c8cb7520a24aab19195dc0a724ecbf4034675d2bd38fba8b9e04aff6c5c8` and
`2531bdef5d481f1024fafe0d5fe36ae7aabb47c478746ceb3286952bccefd73c`.

## Goals / Non-Goals

**Goals:**

- Produce exactly seven collectible suites and one non-collectible helper, each below 1,000
  lines, while preserving all 96 suffixes and 94 test definitions one-to-one.
- Preserve all 17 registry integration cases under auth 5 / DB 5 / QHH 7 owners.
- Make registry direct consumers, the QHH support bridge, selector routes, and the combined
  registry/QHH database authority explicit and independently testable.
- Keep BUG-008's two registry nodes and #1948's 66-node QHH-bootstrap contract biting.

**Non-Goals:**

- No #1903 mapping case or fixture-byte change.
- No registry SQL/schema, geometry, authentication policy, error semantic, or production
  behavior change.
- No production DB mutation, frontend, Slurm, SHUD runtime, node-22 access, or real-Basins
  ingest during node-27 acceptance.
- No `.large-file-guard.json` edit, broad database glob, collectible compatibility shim, or
  archived evidence rewrite.

## Decisions

### D1: Freeze seven owners by function identity

Complete top-level definitions move according to the immutable owner map, not arbitrary line
chunks:

- retained core: 18 functions / 18 nodes;
- parser: 13 / 13;
- CLI: 5 / 5;
- security: 20 / 20;
- auth: 11 functions / 13 nodes;
- DB: 5 / 5;
- QHH: 22 / 22.

Every definition source fragment, normalized AST, signature, decorator, marker, skip,
parameter ID, assertion, and monkeypatch target remains frozen. The #1693 assertions already
present in the baseline remain in the QHH owner. Alternatives rejected: line-chunk splitting
is brittle because families are interleaved; wrapper/re-export suites can duplicate or hide
collection.

### D2: Separate direct consumers from the QHH support bridge

`tests/basins_registry_import_helpers.py` owns all 19 support functions,
`_FakeRiverSegmentCursor`, and the four private constants. Its eight direct collectible
importers are the seven registry suites plus
`tests/test_publish_scheduler_file_registry.py`. The ninth direct importer is the
non-collectible `tests/qhh_production_bootstrap_helpers.py` (QHH owner D). D's three
collectible downstream suites are owners A/B/C.

`SUPPORT_MODULE_TEST_RULES` does not recursively expand one helper rule through another.
Therefore a registry-helper-only diff explicitly routes the eight direct collectible
importers plus QHH A/B/C: eleven suites total, with the selector meta rider added by normal
selector behavior. D itself is recorded and statically checked as the support-to-support
importer, but is not passed to pytest as a suite. Alternatives rejected: claiming nine
collectible importers is false after #1948; relying on transitive selector expansion drops
A/B/C; adding unused imports to A/B/C falsifies dependency ownership.

`tests/test_publish_scheduler_file_registry.py` lifts all registry-helper imports to module
scope. D retains equivalent module/function scope where needed; only the imported module
changes. No registry suite is imported as support after the split.

### D3: Treat the QHH helper edit as a controlled fingerprint transition

D currently imports `_write_registry_fixture` and `_package_manifest_for_model` from the
registry monolith. Both imports retarget to `tests.basins_registry_import_helpers`; A/B/C
must not import that helper directly. The function-local module change inside
`_refresh_inventory_and_manifest` changes its source and AST fingerprints, so the existing
QHH oracle cannot simply remain byte-identical.

The implementation records the pinned `3c29698f…` D blob and the old QHH oracle as before
authority, permits exactly those two import-module substitutions, and regenerates the one
changed helper row, helper aggregate, frozen literal, and self-digest. Tests must prove all
other QHH rows, nodes, owners, markers, execution summaries, selector authority, and database
C+D authority are unchanged. Alternatives rejected: deleting or weakening QHH fingerprint
checks hides semantic drift; forcing fake imports into A/B/C misrepresents consumers.

### D4: Preserve suffix, execution, marker, and BUG-008 contracts

Module prefixes necessarily change, so stable collection identity is each suffix after the
first `::`. The post corpus must contain all 96 suffixes exactly once and all 94 baseline
definitions under their frozen owners. Local execution remains 78 passed / 18 skipped;
non-integration remains 78 passed / 1 skipped / 17 deselected; integration-only remains 17
skipped / 79 deselected; the retained BUG-008 command remains 2 passed / 16 deselected after
the split. The shared helper collects zero nodes.

### D5: Make owner/helper selector edges additive and biting

A sorted `BASINS_REGISTRY_IMPORT_TESTS` tuple is the seven-owner authority. The existing
`workers/model_registry/**` rule replaces only the monolith literal with this tuple and
preserves all unrelated baseline targets, including the package-publication and QHH tuples.
Tests compare required baseline edges by subset, so unrelated future additions remain legal.
A non-same-name production probe independently deletes each registry edge to construct RED.

The registry helper rule targets exactly eleven collectible suites. Tests separately derive
the eight direct collectible importers and support importer D from the tracked AST, require
A/B/C to import D rather than the registry helper, and delete each routed edge independently.

### D6: Freeze the exact eight-path database union

The Basins/QHH authority relevant to this change is the exact union:

1. `tests/basins_registry_import_helpers.py`;
2. `tests/test_basins_registry_import.py`;
3. `tests/test_basins_registry_import_auth.py`;
4. `tests/test_basins_registry_import_db.py`;
5. `tests/test_basins_registry_import_qhh.py`;
6. `tests/test_basins_reingest.py`;
7. `tests/qhh_production_bootstrap_helpers.py`;
8. `tests/test_qhh_production_bootstrap_scheduler.py`.

The first six are registry-specific; the final two are #1948's C+D authority. Parser, CLI,
security, and QHH A/B remain absent. Tests parse only the `database:` block, reject broad
registry globs, and prove each exact path independently unrescued after deletion. Unrelated
future database patterns remain legal.

### D7: Update current commands without rewriting history

Live full-registry commands list all seven owners, and the opt-in real-Basins smoke command
moves to the DB owner. BUG-008's historical core command and archived result evidence remain
unchanged. Current QHH commands retain A/B/C ownership.

### D8: Node-27 executes the complete 28-node integration selection

The frozen-final-SHA node-27 receipt runs registry auth, DB, and QHH owners (17 nodes) plus
QHH-bootstrap C (11 nodes) against one isolated temporary database using local PostgreSQL
`:55432`. With `NHMS_RUN_REAL_BASINS_IMPORT` disabled, 27 nodes must PASS and the deliberate
real-Basins import smoke must SKIP; all seven registry-QHH and all eleven bootstrap-QHH nodes
must PASS rather than skip. The receipt proves cleanup and unchanged production DB/display
identity. Node-22 is not accessed.

## Risk Packs Considered

- Public API / CLI / script entry: **selected narrowly** — selector and CI are operational
  entries; product CLI behavior is unchanged.
- Config / project setup: **selected** — selector, workflow, and structural guard are gates.
- File IO / path safety / overwrite: **not selected for behavior** — existing test fixture IO
  moves byte-identically; no production IO changes.
- Schema / columns / units / field names: **selected narrowly** — test node/marker/owner and
  oracle schema identities are contracts; product schema is unchanged.
- Auth / permissions / secrets: **selected narrowly** — auth tests remain intact and node-27
  receipts must contain no DSN or credential.
- Concurrency / shared state / ordering: **selected** — helper imports and temporary DB cleanup
  cannot depend on collection order.
- Resource limits / large input / discovery: **selected** — all outputs stay below 1,000 lines.
- Legacy compatibility / examples: **selected** — core path, BUG-008, QHH oracle, and external
  consumers remain compatible.
- Error handling / rollback / partial outputs: **selected** — no case may disappear, duplicate,
  unexpectedly skip, or leak a temporary database.
- Release / packaging / dependency compatibility: **selected** — targeted and database lanes
  must execute every moved owner; no dependency changes.
- Documentation / migration notes: **selected** — current commands move; history does not.
- Geospatial / CRS / basin geometry: **selected narrowly** — existing registry-QHH oracles
  move intact; semantics do not change.
- Hydro-met time series / forcing windows: **not selected** — no forcing behavior changes.
- SHUD numerical runtime / conservation / NaN: **not selected** — no solver behavior changes.
- PostGIS / TimescaleDB domain behavior: **selected narrowly** — node-27 executes existing
  integration cases in isolation.
- Slurm production lifecycle / mock-vs-real parity: **not selected** — no scheduling change.
- External hydro-met providers / snapshot reproducibility: **not selected**.
- Run manifest / QC provenance: **not selected**.
- Published NHMS artifacts / display identity: **selected narrowly** — final receipt proves no
  production display mutation.

## Invariant Matrix

- Governing invariant: physical ownership may change, but pytest, targeted CI, and node-27
  SHALL execute the same registry contracts once while preserving #1948 QHH coverage.
- Source-of-truth identity: pinned source Git blob, 94 definition rows, 96 suffixes, 17
  integration suffixes, helper inventory, controlled D transition, selector/database sets,
  and dynamic guard shape.
- Producers: seven registry suites, one registry helper, QHH helper D, selector tuple, CI
  database block, current validation commands, and tracked oracle.
- Validators/preflight: collection/execution, source/AST/marker comparators, AST importer
  closure, per-edge selector/database mutations, BUG-008, QHH oracle transition, and hook.
- Storage/cache/query: isolated node-27 integration database only; no product storage change.
- Public routes/entrypoints: explicit seven-file pytest commands, retained core, selector,
  and CI database trigger.
- Frontend/downstream consumers: scheduler-file-registry; QHH A/B/C via D; targeted/full
  pytest; current docs.
- Failure paths/rollback/stale state: missing/duplicate/renamed node, helper import omission,
  transitive route gap, broad database rescue, over-limit file, skipped QHH integration, or
  leaked temporary DB. Rollback is reverting the PR.
- Evidence/audit/readiness: baseline/oracle manifests, focused/full pytest, final-SHA node-27
  receipt, Ruff, OpenSpec, Markdown, entropy, hook, CI, and scope proofs.
- Regression rows:
  - 96 baseline suffixes / 94 definitions -> seven suites yield exact one-to-one identity;
  - registry-helper-only diff -> eight direct collectible importers plus QHH A/B/C and meta;
  - D retarget -> only two import sources and required QHH digests change, 66 QHH nodes do not;
  - eight database paths -> deleting any edge leaves exactly that path unbound;
  - 28 node-27 integration nodes -> 27 PASS / one intentional real-Basins SKIP, cleanup zero;
  - BUG-008 command -> retained core still passes exactly two cases.

## Boundary-Surface Checklist

- Shared helper roots: one registry helper; D is the only support-to-support importer.
- Public entrypoints: seven-file pytest list, retained core, selector, CI filter.
- Read/write/delete: existing fixture IO plus owned temporary node-27 DB lifecycle only.
- Producer/consumer evidence: registry helper -> direct suites and D -> QHH A/B/C.
- Stale/idempotency: immutable source contract, controlled oracle transition, unique suffixes,
  and per-edge mutations prevent stale routes.
- Unchanged downstream consumers: production registry code, SQL/schema/auth/geometry,
  frontend, Slurm, SHUD, QHH A/B/C behavior, and #1903 fixture bytes.

## Risks / Trade-offs

- A case is dropped or duplicated -> exact suffix multiset and per-definition fingerprints.
- QHH owner exceeds the cap -> QHH sample support remains in the shared registry helper.
- D hides behind non-transitive support routing -> explicit A/B/C edges plus AST bridge proof.
- Oracle is weakened to accept D -> before/after controlled transition and self-digest checks.
- A database edge is rescued by a glob -> block-scoped independent deletion tests.
- Integration cases collect but skip -> node-27 receipt distinguishes 27 PASS from the one
  intentionally gated real-Basins SKIP.
- Current guard evolves upstream -> freeze issue-start provenance but enforce current shape,
  threshold, no registry exclusions, and zero issue-scoped guard diff.

## Migration Plan

Capture the immutable baseline, mechanically generate the helper and owners, validate exact
identity before routing changes, update selector/database/docs/QHH transition guards, run all
local gates, then run a frozen-final-SHA node-27 isolated integration receipt. Rollback is a
single PR revert; no production migration is required. After merge and governance closure,
issue #1903 is unblocked.

## Open Questions

None.
