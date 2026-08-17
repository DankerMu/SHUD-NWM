# Proposal: ci-selector-subprocess-consumption-edge

## Why

Two sibling follow-ups from the #1487 delivery (PR #1496), one PR
closing both:

- **#1498**: `tests/mock_shud_omp.py` is a mock SHUD CLI consumed only
  by SUBPROCESS (`workers/shud_runtime/runtime.py` builds
  `[sys.executable, "tests/mock_shud_omp.py", ...]`; zero imports
  repo-wide). The #1487 routing authority is import-derived, so the
  module classifies 0-importer and collapses to meta-guard +
  collect-only smoke — a mock-only PR runs zero of the 251+ non-gated
  assertions that depend on its output contract. PR #1496 recorded
  this honestly as a gap; the verifier's route-later ruling lands
  here: hand-adding a rule is NOT the fix (the closure guard's
  0-importer equality branch reds it, and a hand rule is a frozen
  list violating the derivation authority). The fix is a SECOND edge
  type in the derivation itself.
- **#1499**: `_non_gated_top_level_importer_tests` (semantic
  authority) and `_non_gated_top_level_importer_index` (executed
  inverted index, load-bearing for the #1455 disposition guard and
  the #1487 closure guard) have no mechanical equivalence pin — the
  claim lives only in comments, flagged independently three times
  across PR #1492/#1496 reviews. Editing one side's marker filter or
  import predicate silently shifts two guards' domains.

## What Changes

One PR closing #1498 and #1499:

- **Literal-path consumption edge** (#1498, the issue's recommended
  option): the guard-side derivation scans each tracked top-level
  test suite's AST string constants for exact repo-relative paths of
  tracked non-suite `tests/` modules; a hit is a consumption edge.
  The support-module closure guard consumes the UNION of import
  edges and consumption edges — same non-gated (file-level marker)
  filter, same staleness/closure semantics, never a frozen list.
  The scan source EXCLUDES the selector meta-guard suite, which
  enumerates support-module paths as data and would otherwise
  register as a phantom consumer of all 8 modules (fixture-review
  P1-1; exclusion pinned). Derived facts at that scope:
  `tests/mock_shud_omp.py` gains exactly three consumer suites, all
  carrying the literal `"tests/mock_shud_omp.py"` —
  `tests/test_shud_runtime.py` (8 sites incl. the shared
  `_runtime()` helper), `tests/test_direct_grid_e2e.py` (2 sites),
  and `tests/test_e2e.py` (1 site — no file-level pytestmark so the
  file-level filter authority includes it; honestly recorded: its
  consumption sits behind FUNCTION-level e2e gating, so that file
  contributes 0 PR-lane mock assertions and is routed for closure
  integrity). No other support module gains or loses an edge
  (verified; keliya/build.py is a true zero-consumer — its
  docstring records that suites read the checked-in fixture files
  and never invoke the script).
- **Routing**: `SUPPORT_MODULE_TEST_RULES` gains a fifth entry
  routing `tests/mock_shud_omp.py` → the three consumer suites (+
  meta-guard rider, #1487 convention). The 0-consumer collapse
  branch needs no new vocabulary: with the union derivation,
  mock_shud_omp simply moves from branch (c) to branch (b), and the
  0-consumer parametrize sample rewrites to the true zero-consumer
  set (`tests/fixtures/mapping_builder/keliya/build.py`).
- **Equivalence pin** (#1499, the issue's recommended option): one
  pytest asserting `_non_gated_top_level_importer_tests(dotted) ==
  _non_gated_top_level_importer_index().get(dotted, set())` over a
  DERIVED sample (all tracked support modules + the first sorted
  modules of `_directory_rule_audit_modules()` — unbiased, unlike
  the gap-map universe which only holds index-visible modules;
  never a hardcoded list), one index build for the whole run. The new literal-edge derivation is implemented ONCE
  (single function, no authority/executor split) so it cannot
  recreate the same drift class.
- **Spec delta** (`ci-contract-baseline`): MODIFIED "Support-module
  changes MUST select their non-gated importer suites" — the
  derivation authority becomes import edges ∪ literal-path
  consumption edges; the mock_shud_omp recorded-gap parenthetical
  and scenario note are RETIRED (the gap is closed); the 0-importer
  scenario's example set becomes the true zero-consumer module; one
  new scenario pins subprocess-consumed module routing. Byte-faithful
  otherwise. (The requirement KEEPS its name; the body defines
  "importer suites" as covering both edge kinds.)

Non-goals: `scripts/**/*.sh` shell-wrapper consumption (#1138,
different surface); the carve-outs (`tests/integration_helpers.py`,
`tests/conftest.py` — #1487 ruling stands); merging the
helper/index implementations (#1499's explicit out-of-scope — the
pin, not the refactor); production-directory rules and the #1455
disposition guard (its domain and derivation are untouched);
`# consumed-by:` declaration comments (#1498's rejected alternative —
moves authority from code facts to stale-prone human declarations).

## Capabilities

- `ci-contract-baseline`: MODIFIED "Support-module changes MUST
  select their non-gated importer suites" (union-edge derivation
  authority; recorded gap retired; scenarios updated).

## Impact

- `scripts/select_ci_tests.py` (one rule entry),
  `tests/test_select_ci_tests.py` (literal-edge derivation + guard
  wiring + equivalence pin + sample rewrites),
  `openspec/specs/ci-contract-baseline/spec.md` (delta).
- Closes #1498, closes #1499. Verification per both issues'
  `Verification:` fields: `uv run pytest -q
  tests/test_select_ci_tests.py`; `uv run ruff check .` (tracked
  form: `git ls-files '*.py' | xargs uv run ruff check`).
