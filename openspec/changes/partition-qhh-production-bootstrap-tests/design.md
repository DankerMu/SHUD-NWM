## Context

Fixture level: **expanded** (agrees with issue #1948). Repair intensity: **high** because
collection identity, imported pytest fixture ownership, support-module routing and the CI
real-database trigger can silently become incomplete during a large physical move. Project
profile: NHMS. Real-database evidence is routed only to node-27's local PostgreSQL
`:55432`; node-22 is DB-free and not applicable.

At baseline `9785e52d541aba71845316da3a9c5b9011749644`,
`tests/test_qhh_production_bootstrap.py` is 2,278 lines / 88,774 bytes with source digest
`e94c0ebc36055c8b80131c6d92b9511ee007712303d632bd59985b1659d08f2f`.
It defines 50 tests and 12 support functions, carries four scheduler-readiness constants,
and collects 66 unique nodes. The sorted full-node digest is
`baa0c8e8027cff175e61abd9f0f273a41e226cc1a8d85fdfd20e35d0130333bc`;
the stable suffix digest is
`896acb7934114ed26a4b749398131526e26651b52310c88b9477e34f49cd0c86`.
Nine integration definitions collect eleven suffixes with digest
`746147ebe8ab8023183d1986074d305ceb61ac8c1204e4c811db8d172cc82ef1`.

The ignored deterministic baseline authority is
`.workplans/issue-1948/contract.json`, SHA-256
`5055f21cdc2fdf4c8cd7c52769e6dbc5f4382e4b2d02744f3c6f6d8e0e503d83`.
It binds every test function to one frozen owner and records exact source/AST digests,
arguments, decorators, markers, monkeypatch targets, helper inventory and execution
summaries.

## Goals / Non-Goals

**Goals:**

- Produce exactly three collectible suites and one non-collectible helper, every output
  strictly below 1,000 lines.
- Preserve all 66 baseline node suffixes exactly once and all 50 test definitions plus 12
  support functions and four constants byte-for-byte apart from module imports/header
  ownership.
- Keep the historical path as a real non-integration preflight/parser suite that still owns
  BUG-008's five QHH output-segment-count cases.
- Keep all eleven integration nodes together and bind their owner to the exact database
  filter path.
- Make production-owner, helper-only and database-filter edges explicit and mutation-proven.
- Make node-27 evidence prove integration execution rather than skip collection.

**Non-Goals:**

- No #1913 registry split or import retarget; this prerequisite keeps the master registry
  helper source until #1913 lands.
- No production bootstrap, registry SQL/schema, geometry, authentication, scheduler or
  error-semantic change.
- No assertion, test name, parameter ID, marker, skip, fixture meaning or monkeypatch
  target change.
- No `.large-file-guard.json` change, new exclusion, threshold change or hook bypass.
- No BUG-008 command, historical result, archived OpenSpec, production DB/display,
  frontend, Slurm, SHUD runtime or node-22 database access.

## Decisions

### D1: Freeze three responsibility owners by function identity

The finite layout follows complete top-level function definitions rather than arbitrary
line chunks:

- `tests/test_qhh_production_bootstrap.py`: the first 31 test functions / 44 nodes; parsing,
  preflight, path/evidence/manifest refusal and station-row preparation. It owns no
  integration node and retains all five QHH names selected by BUG-008's
  `-k output_segment_count` command.
- `tests/test_qhh_production_bootstrap_state.py`: the next 10 functions / 11 nodes; station
  provenance, output-segment digest/backfill, scheduler profile, generated inventory,
  duplicate preflight and missing-database CLI behavior. It owns no integration node.
- `tests/test_qhh_production_bootstrap_scheduler.py`: the final 9 functions / 11 nodes;
  bootstrap success, scheduler canonical readiness, rollback, stale sibling/grid cleanup,
  current identity, duplicate active model and rerun behavior. Every function retains
  `@pytest.mark.integration`; this is the only integration owner.

The counts sum to 50 functions and 66 nodes. The owner map digest is
`baacfa8fc15194a81c8061863c279df0bbbf90686c5997d4f2f3e5eb29ebd9b6`.
The integration owner filename deliberately omits `integration`: the existing
`tests/*integration*.py` database glob would otherwise rescue deletion of its exact path
and make the database edge proof vacuous.

### D2: One non-collectible helper owns all support

`tests/qhh_production_bootstrap_helpers.py` owns all 12 non-test functions and four private
scheduler-readiness constants. The helper keeps the imported fixture
`qhh_scheduler_canonical_readiness`; importing it into the scheduler suite at module scope
must let pytest resolve the fixture. The helper name does not start with `test_`, defines
no `test_*` callable and collects zero nodes.

All three collectible suites import only the support they use from this helper. The helper
is the only post-split module that continues importing `_write_registry_fixture` and
`_package_manifest_for_model` from the baseline registry owner; A/B/C MUST NOT import the
registry monolith. #1948 does not create or depend on #1913's future helper. This keeps the
prerequisite mergeable from current master and prevents a circular dependency. After this
PR merges, #1913 retargets these two residual imports in D, re-freezes its helper-consumer
count/digest for the support-to-support edge, and does not fabricate unused A/B/C imports.

Alternative rejected: leaving support local makes the integration owner exceed the limit;
putting QHH scheduler/met-store support into a registry helper pollutes unrelated registry
suite imports; `conftest.py` turns private support repository-wide.

### D3: Compare stable suffixes and exact definitions

Moved module prefixes necessarily change. Stable collection identity is every node suffix
after the first `::`; all 66 sorted unique suffixes remain identical. Every one of the 50
baseline test definitions and 16 helper members is compared by exact source fragment and
normalized AST. Arguments, decorators, markers, parameter values/IDs, fixtures, assertions,
skips and monkeypatch targets therefore remain unchanged. Imports/header ownership are the
only permitted differences. A tracked self-contained oracle is generated from the ignored
capture at baseline SHA—not from the partitioned tree—and records source/capture digests,
explicit empty A/B integration sets and the exact C/D database authority. Checkout tests
read that tracked oracle and prove it is version-controlled; ignored capture files are an
optional provenance cross-check, never a runtime requirement.

Default execution remains 55 passed / 11 skipped; `-m "not integration"` remains 55 passed
/ 11 deselected. Local `-m integration` still collects eleven expected skips until the
node-27 opt-in receipt. The BUG-008 three-file command remains eight passed: five retained
QHH cases, two retained registry cases and one production-scheduler case.

### D4: Make both selector boundaries explicit and biting

A sorted `QHH_PRODUCTION_BOOTSTRAP_TESTS` tuple is the single three-partition authority.
The existing `workers/model_registry/**` rule replaces its one bootstrap literal with this
tuple and preserves every other target. Per-edge RED uses a model-registry production
probe whose same-name derivation is not any QHH bootstrap partition, so the retained core
edge is genuinely rule-only.

`SUPPORT_MODULE_TEST_RULES` maps `tests/qhh_production_bootstrap_helpers.py` to exactly the
three partitions. The selector's branch-level meta rider remains additive and absent from
the rule tuple. Tree-derived module-scope importer equality, exact-set selection and three
independent edge deletions prevent a helper consumer from silently falling out.

The current importer-gap disposition for `services/orchestrator/scheduler.py` moves from
the historical suite to the scheduler owner, because only that owner imports scheduler
readiness classes after partitioning.

### D5: Give integration tests and DB support separate exact database triggers

The CI `database:` block replaces
`tests/test_qhh_production_bootstrap.py` with
`tests/test_qhh_production_bootstrap_scheduler.py` and adds
`tests/qhh_production_bootstrap_helpers.py`. The retained and state owners are absent because
they own no integration node. D is present because it owns the imported integration fixture,
DB seeding and cleanup: a D-only diff must set `needs.changes.outputs.database=true` and
start `real-db-integration`. `SUPPORT_MODULE_TEST_RULES` affects only targeted unit-test
selection; it cannot open the separate `dorny/paths-filter` database lane, so selecting C
there is not a substitute for D's exact database trigger.

A block-scoped meta-test removes each of the two literals independently from a constructed
workflow and requires that no surviving database pattern matches the removed path while the
other exact edge remains. It also asserts explicitly that `tests/*integration*.py`,
`tests/**/*integration*.py` and broad test globs do not rescue either chosen filename. The
exact QHH bootstrap database subset is the two-element set {scheduler owner, helper}; tests
MUST compare exact paths rather than a prefix that would incorrectly include retained/state.

### D6: Update current commands without rewriting history

Current scheduler compatibility commands that intend to exercise
`_MetStoreCanonicalReadinessProvider` or scheduler readiness name the scheduler owner and
use a non-empty `-k` expression. Commands for the whole bootstrap corpus list all three
partitions. The BUG-008 command in `docs/bugs.md`, its ledger validator, historical result
bullets and archived OpenSpec remain byte-identical; the historical path still makes that
command execute its five QHH cases.

### D7: Node-27 is the only real-database oracle

The frozen-final-SHA receipt runs the scheduler owner on node-27 with
`NHMS_RUN_INTEGRATION=1` and an integration DSN that creates an isolated temporary database
from node-27's local PostgreSQL `:55432`. It must list all eleven integration nodes as
PASSED rather than skipped, then drop temporary databases and show no production DB/display
identity change. Node-22 is not accessed and remains DB-free.

## Risk Packs Considered

- Public API / CLI / script entry: **selected narrowly** — the selector and CI workflow are
  operational entries; product APIs/CLIs are unchanged.
- Config / project setup: **selected** — selector routing, database filter and structural
  guard are release gates.
- File IO / path safety / overwrite: **not selected for behavior** — existing safety tests
  move byte-identically; runtime file IO is unchanged.
- Schema / columns / units / field names: **selected narrowly** — node/parameter/marker
  identity and real-DB route are test contracts; product schema is unchanged.
- Auth / permissions / secrets: **selected narrowly** — integration DSNs must not enter
  committed evidence; auth behavior is unchanged.
- Concurrency / shared state / ordering: **selected** — imported fixture ownership and
  temporary-database cleanup cannot depend on collection order.
- Resource limits / large input / discovery: **selected** — every output stays below 1,000
  lines and selector/database discovery remains complete.
- Legacy compatibility / examples: **selected** — historical path, BUG-008 command, node
  suffixes and scheduler compatibility commands remain usable.
- Error handling / rollback / partial outputs: **selected** — no case may disappear,
  duplicate, skip unexpectedly or lose rollback/cleanup assertions.
- Release / packaging / dependency compatibility: **selected** — targeted PR and real-DB
  jobs must execute every moved oracle; no dependency change.
- Documentation / migration notes: **selected** — current commands name real owners;
  history and archives remain unchanged.
- Geospatial / CRS / basin geometry: **selected narrowly** — existing QHH geometry/import
  oracles move intact; no geospatial semantics change.
- Hydro-met time series / forcing windows: **not selected** — no forcing behavior changes.
- SHUD numerical runtime / conservation / NaN: **not selected** — no solver behavior.
- PostGIS / TimescaleDB domain behavior: **selected narrowly** — node-27 executes existing
  integration cases in isolated temporary databases; no DB semantics change.
- Slurm production lifecycle / mock-vs-real parity: **not selected** — no scheduler runtime
  or Slurm behavior changes.
- External hydro-met providers / snapshot reproducibility: **not selected**.
- Run manifest / QC provenance: **not selected**.
- Published NHMS artifacts / display identity: **selected narrowly** — final receipt proves
  no production display mutation.

## Invariant Matrix

- Governing invariant: physical ownership may change, but pytest, targeted CI and node-27
  SHALL execute the same 66 cases with the same oracles exactly once, and all eleven
  integration nodes SHALL remain bound to the sole integration owner, while both that owner
  and its DB-support helper remain exact real-database trigger paths.
- Source-of-truth identity: baseline source and node digests, 50 test fingerprints, 16
  helper fingerprints, integration suffixes, selector/database sets, line counts and guard
  digest.
- Producers: three collectible QHH bootstrap modules, one non-collectible helper, selector
  tuple, exact C/D CI database literals and current scheduler compatibility commands.
- Validators/preflight: pytest collection/execution, source/AST/marker comparator, selector
  and database per-edge mutations, BUG-008 ledger and large-file hook.
- Storage/cache/query: isolated node-27 integration databases only; no product storage
  change.
- Public routes/entrypoints: explicit three-file pytest commands, retained historical path,
  selector owner/helper routes and CI database trigger.
- Downstream consumers: targeted/full pytest, BUG-008 command, current compatibility docs
  and node-27 real-DB lane.
- Failure paths: dropped/duplicated/renamed node, parameter/marker drift, fixture-not-found,
  missing selector/database edge, glob rescue, stale command, over-limit file, skipped
  integration case or leaked temporary database.
- Evidence/audit: baseline/post manifests, focused/full pytest, exact node-27 receipt, Ruff,
  OpenSpec, Markdown, entropy, hook, CI and scope proofs.
- Regression rows:
  - 66 baseline nodes / 50 tests → three suites collect exactly the same suffixes and
    definitions once;
  - helper-only change → exactly three consumers + meta rider, database output true and all
    eleven integration nodes execute rather than skip; deleting any selector or D database
    edge makes RED;
  - model-registry owner change → all three partitions + prior targets;
  - eleven integration suffixes → scheduler owner only, with exact database triggers for C
    and D and no glob rescue;
  - BUG-008 three-file command → exactly eight passed with retained 5/2/1 ownership;
  - node-27 integration run → all eleven nodes PASSED and temporary DBs cleaned.

## Boundary-Surface Checklist

- Shared helper root: one non-collectible helper with three module-scope consumers, one
  directly imported pytest fixture and its own exact real-database trigger.
- Public/test entrypoints: three-file pytest list, retained historical path, selector and
  exact two-path QHH database authority.
- Read/write/delete: existing test fixture IO and isolated integration DB lifecycle only;
  production behavior unchanged.
- Producer/consumer evidence: partitions/helper → selector/database filter → local/CI/
  node-27 execution.
- Stale/idempotency: unique suffixes/fingerprints prevent omission/duplication; per-edge
  mutations prevent stale routes and glob rescue.
- Unchanged downstream consumers: production bootstrap/registry owners, SQL/schema/auth,
  BUG-008 history, frontend, Slurm, SHUD and #1903/#1913 behavior.

## Risks / Trade-offs

- Mechanical move drops or duplicates a case → exact suffix multiset plus source/AST
  fingerprints and full execution.
- Imported fixture is not discovered → scheduler owner directly imports its symbol; the
  bootstrap-success, existing-GFS-preservation and stale-identity-replacement nodes all
  resolve/execute it, and removing the import produces fixture-not-found RED.
- Integration filename is rescued by a broad glob → choose `_scheduler.py` and assert all
  surviving database patterns fail to match after exact-edge deletion.
- Core remains green while moved tests are blind → explicit three-route set plus per-edge
  mutation proof with a non-same-name probe.
- Historical BUG-008 command becomes a zero-collection false green → retain five QHH names,
  execute the whole command and assert exactly eight passed with 5/2/1 ownership.
- QHH helper is mistaken for a suite → non-`test_` filename, no `test_*` definitions and
  explicit zero-collection proof.
- DB-support helper only selects C but does not open the database lane → give D an exact
  `database:` edge and independently delete/mutate both C/D triggers.
- Registry-helper transitive propagation changes after this prerequisite → #1948 leaves the
  only two residual monolith imports in D; #1913 retargets D, re-freezes its support-to-
  support consumer authority/count/digest, and does not fabricate unused A/B/C imports.

## Migration Plan

Capture baseline contracts, mechanically generate the helper and frozen owner files,
compare before semantic edits, update selector/database/current-doc routing, verify locally,
then run the frozen-final-SHA node-27 isolated integration receipt. Rollback is reverting
the PR. After merge and post-merge closure, #1913 rebases, retargets the two residual
imports in D, replaces its historical bootstrap database literal with the exact C/D pair,
and re-freezes the support-to-support consumer authority/count/digest; no production
migration is required.

## Open Questions

None.
