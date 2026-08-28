## Context

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

The monolithic file collects 120 unique pytest cases from 87 test functions.
Its natural sections are core retention, pipeline frontier, canonical run/extra
roots, and primary/root-overlap/no-follow admission. `tests/test_retention.py`
must remain as the same-name suite for `services/orchestrator/retention.py`, while
all moved partitions must join the existing targeted-CI owner selection. A helper
named like a pytest suite or imported only inside test functions would make
collection/selector coverage misleading.

## Goals / Non-Goals

**Goals:**

- Produce four collectible modules and one non-collectible helper, each below
  1,000 lines, with the pre/post 120 node suffixes byte-identical and unique.
- Preserve every test function name, body, decorator, fixture, parameter value/ID,
  assertion, import-time behavior and production monkeypatch target.
- Preserve and strengthen production-owner CI routing so every partition runs for
  a `services/orchestrator/retention.py` change.
- Remove the final #1872 exact exclusion and add none.

**Non-Goals:**

- No production behavior, bug fix, test assertion rewrite, test rename, skip/xfail,
  dead-code cleanup, fixture semantic change or existing frontier-suite merger.
- No edits to historical/archived OpenSpec commands and no use of the unrelated
  `tests/test_retention_frontier.py` as a destination.
- No `tests/conftest.py` expansion and no collectible re-export shim.

## Decisions

1. **Use four collectible owners.** Keep core tests in `test_retention.py`; move
   pipeline-frontier tests to `test_retention_pipeline_frontier.py`, canonical
   run plus extra-root tests to `test_retention_extra_roots.py`, and primary/root
   overlap/no-follow tests to `test_retention_root_admission.py`. This follows
   existing issue sections and leaves usable line-count headroom. Three files are
   too close to the threshold; five add ownership without another requirement.
2. **Use one non-collectible helper.** `retention_test_helpers.py` owns only shared
   constants and helpers needed by multiple partitions. Every collectible module
   imports it at module scope so selector importer derivation sees the dependency.
   Section-local fixtures/helpers stay with their tests. `conftest.py` would expose
   private retention fixtures repository-wide.
3. **Compare collection by suffix, not old file path.** The full path prefix must
   change for moved tests; the stable identity is everything after the first `::`.
   Pre/post sorted suffixes must contain exactly 120 unique entries and be byte-
   identical. Test AST/decorator fingerprints add a second oracle against body or
   parameter drift.
4. **Extend the existing retention owner route.** Every production retention change
   must select all four partitions, while `test_retention_frontier.py` retains its
   existing independent role. Selector tests pin the exact set and prove removing
   any new partition turns the routing test red.
5. **Remove one exact exclusion.** All five new/current test files stay below 1,000
   lines, no replacement wildcard or exact exclusion is allowed, and unrelated
   exclusions/threshold remain unchanged.

## Risk Packs

- Public API / CLI / script entry: **not selected** — no product entrypoint changes;
  the CI selector script is covered under compatibility/evidence.
- Config / project setup: **selected** — large-file configuration and CI routing
  metadata change.
- File IO / path safety / overwrite: **not selected** — production filesystem
  behavior is only tested, not changed.
- Schema / columns / units / field names: **not selected** — no data schema changes;
  pytest node suffix identity is governed under compatibility.
- Auth / permissions / secrets: **not selected** — no security boundary changes.
- Concurrency / shared state / ordering: **not selected** — test execution remains
  ordinary function-scoped pytest collection.
- Resource limits / large input / discovery: **selected** — every file must remain
  below the structural limit and selector discovery must remain complete.
- Legacy compatibility / examples: **selected** — test names, param IDs, bodies,
  fixtures, owner route and same-name suite are compatibility contracts.
- Error handling / rollback / partial outputs: **selected** — no test may disappear,
  duplicate or silently skip; rollback is source revert.
- Release / packaging / dependency compatibility: **not selected** — no package or
  dependency change.
- Documentation / migration notes: **selected** — active compatibility inventory
  must list every partition.
- All NHMS domain packs: **not selected** — retention runtime behavior, hydro-met
  windows, storage, DB, Slurm, SHUD and display identities are unchanged.

## Invariant Matrix

**Governing invariant:** Physical test ownership may change, but pytest and
production-owner CI SHALL execute the exact same 120 retention cases with the same
oracles exactly once.

**Source of truth:** sorted `::test_name[param-id]` suffix set, per-test AST and
decorator fingerprints, fixture/helper bindings, selector partition set, line
counts and exact exclusion list.

- Producers: four collectible retention test modules and non-collectible helper.
- Validators/preflight: pytest collection, selector `PathTestRule` and frozen/floor
  selector tests, large-file guard.
- Storage/cache/query: none; test layout only.
- Public entrypoints: `pytest` explicit file list and CI selector for production
  retention paths.
- Downstream consumers: production retention PR lane, full repository test run,
  scheduler compatibility inventory.
- Failure paths: dropped/duplicated test, changed param ID/body/decorator, helper
  collected as a suite, missing selector partition, line over limit.
- Evidence/audit: pre/post collection suffixes, AST fingerprints, focused/full
  pytest, selector mutations, line guard/entropy, ruff, strict OpenSpec.

Regression rows:

- Original 120 cases -> four modules collect exactly 120 unique identical suffixes
  and pass with no body/decorator/fixture drift.
- A production `retention.py` change -> selector includes all four partitions and
  its prior retention-frontier route; deleting one route makes its pin fail.
- All five files + exclusion removal -> every file below 1,000, no new exclusion,
  unrelated guard entries and production source unchanged.

## Boundary-Surface Checklist

- Shared helper: non-collectible local module, module-scope imports only.
- Public/test entrypoints: explicit four-file pytest list and production selector.
- Producer/consumer boundary: test partitions to CI selector and full collection.
- Stale/idempotency boundary: unique suffix set prevents duplicates or omissions.
- Unchanged downstream consumers: production code and independent frontier suite.

## Risks / Trade-offs

- **A moved test disappears or duplicates** → exact 120 suffix multiset plus AST
  fingerprints and focused/full execution.
- **CI runs only the retained same-name file** → extend and mutation-test the owner
  rule/frozen floor for all partitions.
- **Shared helper becomes collectible** → non-`test_*.py` name and collect-only
  assertion that it contributes no node.
- **Pure moves hide assertion edits** → compare normalized per-test AST/decorators
  against the original baseline; any mismatch blocks.

## Migration Plan

Move helpers/tests mechanically, update selector/guard/inventory, run identity and
verification gates, then merge as a test/governance-only source change. Rollback is
reverting the PR; no data/runtime migration is needed. After merge and governance
closure, close #1872.

## Open Questions

None.
