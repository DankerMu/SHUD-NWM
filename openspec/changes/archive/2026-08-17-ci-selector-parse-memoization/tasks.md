# Tasks: ci-selector-parse-memoization

## 1. Cache (#1504)

- [x] 1.1 Memoize `_parse_tracked` (tests/test_select_ci_tests.py:464)
      with module-level dict keyed
      `(str(Path(path).resolve()), st_mtime_ns, st_size)`; signature
      and every call site unchanged; comment records the key
      rationale (cwd safety via resolve, staleness via stat identity)
      and the two recorded boundaries (identical-stat rewrite;
      first-parse filename spelling).
- [x] 1.2 Mechanical shared-AST audit: no consumer mutates trees
      returned by `_parse_tracked` (attribute assignment/transform
      scan over the suite); result recorded in the PR body. If a
      mutation exists: STOP and report (design decision 4).

## 2. Guard tests

- [x] 2.1 cwd-aliasing guard: prime cache with a real tracked file's
      repo-relative spelling, chdir to tmp_path, same spelling with
      different content → parse reflects tmp_path content (design
      decision 2).
- [x] 2.2 staleness guard: parse, rewrite with different content +
      explicit `os.utime` bump, re-parse → new content observed
      (design decision 3).
- [x] 2.3 Red evidence (out-of-tree only; plan corrected by
      fixture-review P2-2): variant A = bare raw-path-spelling
      memoization (e.g. `functools.lru_cache` over the unmodified
      argument) reds BOTH guards (cwd aliasing hits the repo entry;
      rewrite hits the stale entry); variant B = resolved-path-only
      key without stat identity reds the staleness guard while the
      cwd guard stays green. Recorded boundary: a hypothetical
      "relative spelling + stat identity" hybrid is deliberately
      OUTSIDE the guards' discrimination (stat identity already
      separates the tmp copy) — do not claim a red for it.

## 3. Measurement evidence

- [x] 3.1 Wall-clock before/after for
      `uv run pytest -q tests/test_select_ci_tests.py` (both numbers
      recorded; baseline ~66-68 s).
- [x] 3.2 Instrumented actual-`ast.parse` count before/after
      (~10^4 → ~5×10^2: fixture review measured 10,114 baseline calls
      and 510 distinct resolved identities — 188 top-level suites +
      313 non-test modules via `_one_hop_importer_modules` + support
      modules + tmp fixtures), out-of-tree pytest plugin.
- [x] 3.3 collect-only before/after (suite-only and full-tree),
      recorded — no parametrize restructuring.

## 4. Spec delta

- [x] 4.1 ADDED requirement in `ci-contract-baseline`: "Meta-guard
      tree derivations MUST stay content-faithful under parse
      caching" with the cwd-aliasing and rewrite-observation
      scenarios; boundaries in the requirement parenthetical.

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q tests/test_select_ci_tests.py` → 123
      passed (121 existing unmodified + 2 new guards), no skips
      added; wall-clock recorded before/after. Name-set proof
      (fixture-review Note-2): `pytest --collect-only -q` node-id
      sets before/after — before is a strict subset of after and the
      difference is exactly the 2 new guard tests.
- [x] 5.2 Red evidence per 2.3 (honest labels).
- [x] 5.3 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [x] 5.4 `openspec validate ci-selector-parse-memoization --strict
      --no-interactive` valid.
- [x] 5.5 Diff confined to tests/test_select_ci_tests.py +
      tests/conftest.py (the session-end clear hook — design
      decision 1 second amendment) + spec delta;
      `scripts/select_ci_tests.py` absent from the diff; guard
      predicates zero-diff (issue acceptance bullet 6).
- [x] 5.6 Issue #1504 acceptance checkboxes mapped one-by-one in the
      PR body (incl. the recorded interpretation of bullet 1 — design
      decision 6).
