# Tasks: ci-selector-shared-ast-mutation-guard

## 1. Guard (#1511)

- [x] 1.1 `_tree_mutation_offenders(tree)` helper in
      tests/test_select_ci_tests.py per design decision 1 — SIX rule
      classes (Attribute-in-Store/Del-ctx unified rule;
      Store/Del-subscript-over-Attribute-base; direct
      `NodeTransformer` base; location-fixup call names bare or
      attribute; bare-Name `setattr`/`delattr`; mutating list-method
      on Attribute receiver). NO `setattr` in the attribute-inclusive
      call-name list — issue CAVEAT; the Name-only rule honors it.
      Offender strings carry line + construct.
- [x] 1.2 Guard test parses the suite's own source via
      `_parse_tracked(SELECTOR_META_GUARD_TEST)` (Note-5: existing
      single source of the self path, imported at :30) and asserts
      zero offenders; docstring pins the `_PARSE_CACHE`
      shared-instance premise and cites archived change
      `ci-selector-parse-memoization` decision 4 + issue #1511.
- [x] 1.3 Red/clean arms as STANDING TESTS (fixture-review P1-1):
      one parametrized red over the six rule classes (in-memory
      constructed sources; offender line + construct asserted); one
      clean-source test (`monkeypatch.setattr(...)`, Name-base
      subscript assign, Name-receiver `append`) asserting zero
      offenders. 3 new test functions total.

## 2. Spec delta

- [x] 2.1 MODIFIED "Meta-guard tree derivations MUST stay
      content-faithful under parse caching": requirement body gains
      the shared-instance premise + standing-guard clause with the
      setattr/out-of-module evasion boundary recorded; one new
      scenario (mutation idioms are mechanically barred);
      byte-faithful otherwise (difflib check).

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py` green:
      123 existing unmodified + 3 new test functions (guard +
      parametrized red arm + clean arm; node-id set proof: before ⊂
      after, difference exactly the new tests' node ids).
- [x] 3.2 Red/clean arms green per 1.3 (constructed, in-memory, zero
      tracked mutation — standing tests, not one-shot evidence).
- [x] 3.3 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [x] 3.4 `openspec validate ci-selector-shared-ast-mutation-guard
      --strict --no-interactive` valid; MODIFIED block difflib clean
      apart from the surgical additions.
- [x] 3.5 Diff = exactly tests/test_select_ci_tests.py + spec delta;
      `scripts/select_ci_tests.py`, `tests/conftest.py` untouched.
- [x] 3.6 Issue #1511 acceptance checkboxes mapped in the PR body.
