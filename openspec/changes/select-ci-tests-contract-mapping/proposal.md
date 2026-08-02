# Map node27_container_contract to its dependent suites in the CI selector (#1247)

## Why

The #1087→#1242→#1244→#1245 lane single-sourced the recovery-target
six fields into `packages/common/node27_container_contract.py` — so a
legitimate future recovery-target change touches ONLY that file. But a
contract-only diff falls through `PATH_TEST_RULES` (packages/common
has only per-file whitelist entries — the 5-rule block at
select_ci_tests.py:284-317 plus the safe_fs stop-on-match rule
around :94/:160-163 — none covering the contract) to
the `unknown_backend_python` CORE_SMOKE fallback: the PR CI runs 5
files with ZERO relation to the contract while every drift guard /
byte-freeze the lane built goes unselected. The issue proved the
guards work (a single-field flip reddens 6 tests across the closure)
and that the mis-selected CORE_SMOKE set is MORE expensive than the
correct closure. Regression discovery is deferred to post-merge master
runs (Unit Tests (full) never runs on pull_request).

Current-state re-measurement at master 054bbf1d (the issue anchored
86edf883 and predicted the closure would grow — it did): the closure
is now FIVE suites, not four. `tests/test_node27_timeseries_
decompression_replay.py` imports the contract directly since PR #1248
(#1245), joining benchmark/capture/supervisor (direct) and
live_evidence (one-hop transitive via `from scripts import ...` — its
own text never names the contract, so textual grep cannot find it).
The selector still returns pure CORE_SMOKE for the contract-only diff
(re-verified).

## What Changes

Issue's recommended KISS two-parter, scoped to this one module plus a
reusable anti-drift guard (batch-mapping the other ~32 packages/common
modules stays a maintainer decision, out of scope):

1. `scripts/select_ci_tests.py`: one explicit `PathTestRule` for
   `packages/common/node27_container_contract.py` mapping to the five
   dependent suites (benchmark, capture, supervisor, live_evidence,
   decompression_replay test files), same style as the existing
   packages/common per-file rules. No change to
   `_is_backend_python_path`, the CORE_SMOKE fallback, or the #1191
   `_same_name_script_test` scope — independent of the #1182 policy
   decision.
2. `tests/test_select_ci_tests.py`: a NEW meta-guard in the style of
   the existing `test_every_tracked_script_with_a_same_name_suite_
   selects_it_without_core_smoke` element — derive the contract's
   dependent-test closure by AST import analysis over the TRACKED
   tree (`git ls-files`, same enumeration style as the sibling
   helper). The AST must recognize BOTH contract-import spellings —
   `from packages.common import node27_container_contract` (today:
   the four direct test importers) and `from packages.common.node27_
   container_contract import ...` (today: scripts only —
   live_evidence/benchmark/supervisor) — and transitivity is computed
   to a FIXED POINT over the `scripts/` import graph (a test
   importing a scripts module whose transitive scripts-imports reach
   the contract), not one hop: today the fixed point equals the
   one-hop set (the same five suites, re-verified), but two-hop
   chains already exist (bundle_author → live_evidence, plan_author →
   supervisor) and a future `tests/test_..._bundle_author.py` must
   redden the guard, not slip past it. Non-`scripts/` prefixes
   (services/workers/apps) are deliberately excluded: zero contract
   importers today, verified. Then assert `select_tests(["packages/
   common/node27_container_contract.py"])` is a SUPERSET of that
   closure and is DISJOINT from the CORE_SMOKE set
   (`set(CORE_SMOKE_TESTS) & set(selected) == set()` — the same
   no-overlap binding the #1191 meta-guard uses; a mere `!=` would
   pass a closure∪CORE_SMOKE selection and silently void this
   change's cost argument). Two anti-vacuity floors, both derived
   rather than frozen: the closure must contain
   `tests/test_node27_timeseries_compression_live_evidence.py`
   (the transitive member textual grep cannot see), and the AST
   closure must be a superset of an INDEPENDENTLY regex-derived
   direct-importer set (cross-derivation — a buggy AST walk fails
   loudly without freezing the closure's cardinality). Future closure
   growth reddens the guard (pointing at the rule to extend) instead
   of silently unselecting.

## Non-goals

- #1182 (empty-selection collect-only fallback policy) — this diff's
  selection is non-empty either way; independent and mergeable alone.
- #1138 (`scripts/**/*.sh` paths-filter blindness).
- Batch mapping for the other packages/common modules (34 modules
  fall to CORE_SMOKE today; cost profiles differ wildly — e.g.
  model_registry would select 15 files); this change ships the
  mechanism (the meta-guard is written so extending it to more
  modules is a parametrization, not a rewrite) but claims only the
  contract module.
- The five suites' assertions themselves; ci.yml gating semantics;
  import-graph-based selection (the rejected alternative — unbounded
  selection cost, conflicts with the CI cost discipline).
