# Tasks: select-ci-tests-contract-mapping

Fixture level: compact · Repair intensity: light · Issue #1247

Triage note: S — one PathTestRule + one meta-guard test, no runtime
code, fully local (no node-27/22 oracle). Issue is
implementation-ready with pre-measured evidence; re-measured at
master 054bbf1d: the dependent closure is now FIVE suites (the issue
anchored 86edf883 with four and explicitly predicted the growth —
`tests/test_node27_timeseries_decompression_replay.py` imports the
contract directly since PR #1248). Fixture review round 0 (ACCEPT
with tightenings, folded in): fixed-point transitivity, disjointness
binding, cross-derivation floor. Risk axes: (1) ANTI-TAUTOLOGY —
the meta-guard must DERIVE the closure via AST import analysis over
the tracked tree (`git ls-files`, sibling-helper style; local
untracked test files must not redden it), not hardcode the five
filenames; anti-vacuity floors are DERIVED, not frozen:
live_evidence ∈ closure, and AST closure ⊇ an independently
regex-derived direct-importer set (cross-derivation — no |closure|
constant to hand-maintain when the closure legitimately shrinks).
(2) BOTH IMPORT SPELLINGS + FIXED-POINT TRANSITIVITY — the contract
is imported as `from packages.common import node27_container_contract`
(the four direct test importers) AND `from
packages.common.node27_container_contract import ...` (scripts ONLY
today: live_evidence/benchmark/supervisor — no test uses the second
spelling); the AST walk must catch both, and transitivity runs the
`scripts/` import graph (`from scripts import <m>` /
`import scripts.<m>`) to a FIXED POINT: today fixed-point == one-hop
== the same 5 suites (re-verified), but bundle_author →
live_evidence and plan_author → supervisor two-hop chains already
exist and a future same-name test for them must redden the guard.
Non-`scripts/` prefixes (services/workers/apps) deliberately
excluded — zero contract importers today, verified.
(3) SELECTOR REGRESSION — the new rule must not perturb any other
selection: the full existing tests/test_select_ci_tests.py suite is
the oracle; CORE_SMOKE fallback and #1191 same-name mapping
untouched. (4) COST HONESTY — record the selected closure's measured
local cost in the PR body against the CI cost discipline (the
mis-selected CORE_SMOKE set includes the two heavyweight suites and
is more expensive than the correct closure). Single review round.

Must preserve:
- `scripts/select_ci_tests.py`: `_is_backend_python_path`, CORE_SMOKE
  fallback semantics, `_same_name_script_test` (#1191) scope, all
  existing PATH_TEST_RULES entries — additive rule only.
- `tests/test_select_ci_tests.py`: all existing tests green.
- Zero diff outside `scripts/select_ci_tests.py`,
  `tests/test_select_ci_tests.py` (+ this fixture).
- Suites untouched: capture 14, live_evidence 277, supervisor 127,
  benchmark, replay, prearm 38 — none modified by this change.

## Implementation tasks

- [x] 1. `scripts/select_ci_tests.py`: add one `PathTestRule` for
  `packages/common/node27_container_contract.py` → the five suites
  (`tests/test_node27_timeseries_compression_benchmark.py`,
  `..._capture.py`, `..._supervisor.py`, `..._live_evidence.py`,
  `tests/test_node27_timeseries_decompression_replay.py`), placed
  with the existing packages/common per-file rules (:284-317 style).
- [x] 2. `tests/test_select_ci_tests.py`: NEW meta-guard (style
  anchor: the existing tracked-script same-name meta-test) —
  (a) derive the contract's dependent closure: enumerate tracked
  `tests/test_*.py` via `git ls-files`, parse with `ast`, collect
  direct contract imports (both spellings) and transitives via the
  `scripts/` import graph run to a FIXED POINT (test imports
  `scripts.<m>` / `from scripts import <m>` where `scripts/<m>.py`
  reaches the contract through any chain of scripts imports, both
  spellings at every hop);
  (b) anti-vacuity floors (derived, not frozen): closure contains
  `tests/test_node27_timeseries_compression_live_evidence.py` AND
  closure ⊇ an independently regex-derived direct-importer set;
  (c) assert `select_tests(["packages/common/node27_container_contract.py"])`
  ⊇ closure AND `set(CORE_SMOKE_TESTS) & set(selected) == set()`
  (disjoint, matching the #1191 meta-guard's no-overlap binding —
  not a weak `!=`).
- [x] 3. Red proof (scratch mutation, restored, output recorded):
  remove the new PathTestRule → the meta-guard goes red (selection
  falls back to CORE_SMOKE, superset assertion fails); restore →
  green.
- [x] 4. Optional flip evidence (issue's bonus AC, cheap): scratch
  pytest plugin flips `RECOVERY_TARGET_CHUNK_NAME` at import time and
  runs the five newly-selected suites → they go red (recorded count),
  proving the newly selected gate carries information. Do NOT modify
  any repo file for this; plugin lives in the scratchpad.
- [x] 5. Cross-review round 1 repair (verifier CONFIRMED P3,
  FIX_NOW — coverage gap in the new guard itself):
  `_imported_module_names` dropped relative imports and its docstring
  asserted a false premise ("the tracked tree uses absolute imports"
  — services/workers hold 49 relative ImportFrom nodes; packages/**
  holds 0 today, so the hole was latent). Fixed by resolving
  `level >= 1` against the importer's package path
  (`_import_from_base`) so a future `packages/common` sibling using
  `from .node27_container_contract import ...` lands in the closure
  and reddens the scope guard; new negative-case test covers three
  relative spellings + overshoot-depth returns empty instead of
  raising. Red proof: pre-change helper logic → new test fails with
  empty set; restored → 31 passed. Second verified finding
  (meta-guard not self-selected on a tests/**-only PR) adjudicated
  pre-existing selector architecture (#1191 guard identical) →
  deferred with routing, not fixed here.
- [x] 6. Oracle: acceptance command
  `printf 'packages/common/node27_container_contract.py\n' > <scratch>/diff.txt
  && uv run python scripts/select_ci_tests.py --changed-file
  <scratch>/diff.txt --repo-root .` output includes the five suites
  and is not pure CORE_SMOKE; `uv run pytest -q
  tests/test_select_ci_tests.py` green (existing + new);
  `uv run ruff check .`; `git diff --stat` → exactly the two files
  (+ fixture); measured local cost of the five-suite closure
  recorded; `openspec validate select-ci-tests-contract-mapping
  --strict --no-interactive`.

## Required evidence

- Selector output before/after for the contract-only diff; meta-guard
  red-then-green proof (rule removed → red, restored → green); the
  closure the guard derived (must list live_evidence); pytest counts
  for tests/test_select_ci_tests.py; measured five-suite cost vs the
  CORE_SMOKE cost note; ruff; zero-diff proof.

## Non-goals

- #1182 empty-selection policy; #1138 shell-path filters; batch
  mapping of other packages/common modules (mechanism ships, claims
  stay scoped to the contract module); import-graph-based selection;
  ci.yml changes; any change to the five suites themselves.
