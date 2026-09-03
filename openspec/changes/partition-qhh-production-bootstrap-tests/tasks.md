## 1. Baseline Contract

- [x] 1.1 Persist baseline SHA/source/guard/selector/database-filter digests, exactly 66
  unique nodes, full-node digest
  `baa0c8e8027cff175e61abd9f0f273a41e226cc1a8d85fdfd20e35d0130333bc`, stable
  suffix digest `896acb7934114ed26a4b749398131526e26651b52310c88b9477e34f49cd0c86`,
  50 test definitions, 12 support functions and four private constants. The persisted
  source/partition-input baseline is `57ddc54501322728f518b776f48d14317a479d14`
  (monolith 2,285 lines / 89,160 bytes, source SHA
  `015334d7a7fd2cc70deec2ec191452edbca45ecce34ef7feb45db787b90d1603`); the frozen
  guard digest is guard-PROVENANCE only, from issue start
  `9785e52d541aba71845316da3a9c5b9011749644`, and may legally diverge from the source
  baseline guard blob.
- [x] 1.2 Record baseline default execution (55 passed, 11 skipped), non-integration
  execution (55 passed, 11 deselected), local integration collection (11 skipped) and
  BUG-008 three-file execution (eight passed); freeze decorator/parameter/marker/
  monkeypatch inventories and source/AST fingerprints.
- [x] 1.3 Freeze all 11 integration suffixes to the scheduler owner; integration suffix
  digest `746147ebe8ab8023183d1986074d305ceb61ac8c1204e4c811db8d172cc82ef1`.

## 2. Three-Suite Physical Partition

- [x] 2.1 Retain the first 31 functions / 44 nodes, including BUG-008's five QHH
  `output_segment_count` cases, in `tests/test_qhh_production_bootstrap.py`; move the next
  10 functions / 11 non-integration nodes to
  `tests/test_qhh_production_bootstrap_state.py` and the final 9 functions / 11 integration
  nodes to `tests/test_qhh_production_bootstrap_scheduler.py`.
- [x] 2.2 Move all 12 non-test functions and four scheduler-readiness constants into
  non-collectible `tests/qhh_production_bootstrap_helpers.py`; A/B import only used support,
  while C imports the fixture symbol `qhh_scheduler_canonical_readiness` at module scope.
  Require helper zero collection; successful fixture resolution for bootstrap success,
  existing-GFS preservation and stale-identity replacement; and fixture-not-found RED for
  those three nodes when the symbol import is removed.
- [x] 2.3 Compare pre/post suffix sets, exact definition/source segments, normalized AST,
  decorators, parameters, markers, fixture arguments, assertions, skips and monkeypatch
  targets; require 66 unique identical suffixes, 50 one-to-one tests and 16 one-to-one
  helper members.
- [x] 2.4 Keep exactly D's two master registry-monolith imports for this prerequisite;
  A/B/C must not import that monolith or #1913's future helper. Record that #1913 retargets
  D and re-freezes the support-to-support consumer authority/count/digest rather than
  fabricating unused A/B/C imports.

## 3. Selector, Database Filter, Guard and Current Docs

- [x] 3.1 Add one explicit sorted three-partition selector authority; replace the single
  QHH bootstrap target under `workers/model_registry/**` without dropping any prior target.
- [x] 3.2 Route helper-only changes through `SUPPORT_MODULE_TEST_RULES` to exactly the three
  partitions plus the existing selector meta rider; derive module-scope importers from the
  tracked tree and require exact equality.
- [x] 3.3 Run per-edge constructed-RED mutations for all three production-owner routes
  using a non-same-name production probe and all three helper consumers; restore every edge,
  then prove the real tables GREEN.
- [x] 3.4 Replace the historical QHH bootstrap CI `database:` literal with the scheduler
  owner and add D as a second exact trigger; retained/state stay absent. Prove all 11
  integration suffixes bind to C, a D-only diff opens the real-DB job, and deleting either
  C/D edge is not rescued by any surviving pattern, especially `tests/*integration*.py`,
  `tests/**/*integration*.py` or broad test globs.
- [x] 3.5 Update current scheduler compatibility commands to name all three owners or the
  scheduler owner as required, and prove every focused `-k` command collects at least one
  intended node; keep BUG-008, historical result evidence and archives byte-identical.
- [x] 3.6 Prove all four Python outputs `<1000` lines, the baseline
  `.large-file-guard.json` blob digest remains
  `5c06fad8ba8f488d8bfc836e747cd7af642232a880bec25ae132e1bd17ab87ad`, the
  guard stays outside the #1948 PR-visible change set with no QHH replacement exclusion,
  and the exact pending changeset passes the ordinary wired hook.
- [x] 3.7 Add tracked self-contained
  `tests/fixtures/qhh_bootstrap_partition_oracle.json` with schema
  `qhh-bootstrap-partition-oracle/v1`; pin the frozen source / partition-input commit
  `57ddc54501322728f518b776f48d14317a479d14` (the last relevant input snapshot this
  partition used, already carrying #1765's scoped monkeypatch context), source SHA
  `015334d7a7fd2cc70deec2ec191452edbca45ecce34ef7feb45db787b90d1603`,
  capture SHA `084b1677f7a45eaf9a813f2f6f5bce1e1a8e6cc21c88f40c53675783317f2257`
  and 2,285 lines. Record the two-baseline split explicitly: source/partition-input
  baseline = `57ddc545…`; guard-provenance baseline = issue start
  `9785e52d541aba71845316da3a9c5b9011749644` whose `.large-file-guard.json` digest
  remains `5c06fad8ba8f488d8bfc836e747cd7af642232a880bec25ae132e1bd17ab87ad`
  (provenance only; the guard may evolve upstream). Plus explicit empty integration sets
  for A/B and exact C/D database authority. Prove the
  oracle itself is tracked and does not depend on ignored `.workplans/` state in another
  checkout.

## 4. Evidence Floor

- [x] 4.1 Run explicit three-file collect-only/default/non-integration pytest; expect 66
  unique identical suffixes, 55 passed/11 skipped default semantics, 55 passed/11
  deselected non-integration semantics and helper zero collection.
- [x] 4.2 Run the BUG-008 three-file command and require exactly eight passed with QHH /
  registry / production-scheduler ownership 5 / 2 / 1; run the ledger validator.
- [x] 4.3 Run `uv run pytest -q tests/test_select_ci_tests.py`, selector 3/3 exact sets,
  fixture-import RED and all selector/database C+D per-edge mutation proofs; expect every
  assertion GREEN after each mutation is restored.
- [x] 4.4 Run affected QHH bootstrap, model-registry, scheduler and compatibility consumers
  plus full `uv run pytest -q`; expect no production, SQL/schema/geometry/auth/oracle drift.
- [x] 4.5 Run Ruff for every changed/new Python file, entropy report/hard gate, strict
  single/all OpenSpec, Markdown lint and `git diff --check`; expect zero new violations.
- [ ] 4.6 On node-27, run frozen-final-SHA scheduler-owner integration suite against an
  isolated temporary database on node-27's local PostgreSQL `:55432`; require all 11 nodes
  PASSED, including the three nodes requesting imported
  `qhh_scheduler_canonical_readiness`, verify cleanup and no production DB/display mutation.
  Node-22 is not applicable and remains DB-free.
- [x] 4.7 Confirm final diff contains only QHH test layout/helper, selector metadata/
  meta-tests, exact C/D CI database paths, current compatibility commands, tracked oracle and
  this OpenSpec change; explicitly exclude production code, guard changes, registry split,
  BUG-008 history and #1903 behavior.
