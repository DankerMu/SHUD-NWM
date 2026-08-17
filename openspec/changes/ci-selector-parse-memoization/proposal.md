# Proposal: ci-selector-parse-memoization

## Why

The selector meta-guard suite's wall-clock doubled (32.7 s pre-#1487 →
~66-68 s post-#1502). The amplifier is `_parse_tracked`
(tests/test_select_ci_tests.py:464-465): zero memoization, so one suite
run re-`ast.parse`s the same ~188 tracked files ~10^4 times (measured
9,731+ calls / ~49 s ≈ 75% of the run — issue #1504, verifier-measured
in PR #1502 round 1 and independently re-measured on master). Three
consecutive hardening PRs (#1492/#1496/#1502) each added a full-tree
AST derivation and none added caching; a fourth would push the suite
toward two minutes while the marginal cost could be ~0. Not a CI
blocker (35-min lane budget) — developer-loop friction plus growth-trend
debt.

## What Changes

- Memoize `_parse_tracked` keyed by resolved file identity:
  `(str(Path(path).resolve()), st_mtime_ns, st_size)` — NEVER the
  caller-supplied relative path (two existing tests chdir into
  tmp_path and parse repo-shaped relative spellings; a relative key
  would silently alias them onto repository parses). Signature and
  all call sites unchanged; guard predicates byte-identical.
- Two new guard tests pin the cache's only new failure modes:
  cwd aliasing (chdir + same-named relative spelling must yield the
  tmp_path content, not the cached repo parse) and staleness
  (rewrite with distinct stat identity must be re-observed).
- Before/after evidence: wall-clock and actual `ast.parse` counts
  (~10^4 → order of tracked-file count ~10^2).
- Spec delta: ADDED requirement in `ci-contract-baseline` pinning
  content-faithfulness of the memoized parse layer (the cache
  introduces these two failure modes; the contract pins their
  impossibility).

Non-goals (issue #1504 boundary): derivation semantics (import /
literal-consumption edges), `scripts/select_ci_tests.py` (does no AST
parsing), ci.yml lane structure, sinking the module-level derived
parametrize (recorded as possible separate follow-up).

## Capabilities

- `ci-contract-baseline`: ADDED "Meta-guard tree derivations MUST stay
  content-faithful under parse caching".

## Impact

- `tests/test_select_ci_tests.py` (cache + 2 guard tests),
  `openspec/specs/ci-contract-baseline/spec.md` (delta).
- Closes #1504. Verification per its `Verification:` field:
  `uv run pytest -q tests/test_select_ci_tests.py` (before/after
  wall-clock recorded); `git ls-files '*.py' | xargs uv run ruff
  check`.
