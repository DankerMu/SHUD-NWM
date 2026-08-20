# Proposal: ci-selector-shared-ast-mutation-guard

## Why

#1504/PR #1507 made `_parse_tracked` hand every consumer the SAME
cached `ast.Module` (tests/test_select_ci_tests.py:473-501). Safety
rests on "every consumer only reads" — verified by a one-time audit
recorded in the PR body, i.e. convention, not a test. The suite is on
a documented growth trajectory (#1487, #1498/#1499, #1407, #1504 all
touched this file recently, each adding full-tree derivations), so
the audit expires with every new PR. A future in-place tree edit
(`node.parent = ...` annotation, `NodeTransformer` rewrite,
`fix_missing_locations`) would silently corrupt every other
derivation's view of that file with zero test signal. Issue #1511
(deferred from PR #1507 round 1; verifier ruling "route as
follow-up") turns design decision 4's audit into a standing
mechanical guard.

## What Changes

- One self-referential meta-guard test in
  `tests/test_select_ci_tests.py`: parse the suite's own source (via
  `_parse_tracked`, so it rides the cache) and assert emptiness of
  six mutation-idiom rule classes (issue #1511's four, widened per
  fixture review — every rule re-verified zero-hit on HEAD):
  1. any `ast.Attribute` in `Store`/`Del` context (all assignment
     forms — plain/augmented/annotated/loop/with/comprehension
     targets — plus attribute `del`, in one rule);
  2. `Store`/`Del` subscripts over an `Attribute` base
     (`tree.body[0] = x`, `del tree.body[0]`);
  3. `ClassDef` with a direct base named/ending `NodeTransformer`;
  4. `Call`s named `fix_missing_locations` / `copy_location` /
     `increment_lineno` (bare or attribute callee);
  5. bare-`Name` calls to `setattr` / `delattr` (catches the
     canonical `setattr(node, "parent", p)`; `monkeypatch.setattr`
     is an attribute callee and is not matched — the issue CAVEAT
     forbids `setattr` only in the attribute-inclusive list 4);
  6. mutating list-method calls (`append`/`extend`/`insert`/
     `remove`/`pop`/`clear`) on an `Attribute` receiver
     (`tree.body.append(x)`).
  Failure messages name the construct and line. The scan is one
  tree-in/offenders-out helper; the red arm (parametrized over the
  six classes) and the clean-source no-false-positive arm land as
  STANDING TESTS (fixture-review P1-1), constructed in-memory from
  string literals — which are `ast.Constant` in the real module's
  tree, so the arms cannot self-trip.
- Recorded boundaries: attribute-callee `setattr` aliases,
  out-of-module helpers, INDIRECT `NodeTransformer` subclasses, and
  `exec`-built code evade the scan (tripwire, not a proof).
- Spec delta: MODIFIED "Meta-guard tree derivations MUST stay
  content-faithful under parse caching" — the requirement gains the
  shared-instance premise + the standing-guard clause and one new
  scenario; byte-faithful otherwise.

Non-goals (issue #1511 boundary): changing `_parse_tracked` itself
(no deepcopy/read-only wrappers — would eat #1504's win); the
session-end integrity re-parse probe (rejected alternative: re-parsing
all cached files + `ast.dump` diff at session end hands back a chunk
of the time #1504 saved); scanning other `tests/` files (no shared
AST cache elsewhere); `tests/conftest.py` (clears the cache, never
reads trees).

## Capabilities

- `ci-contract-baseline`: MODIFIED "Meta-guard tree derivations MUST
  stay content-faithful under parse caching" (shared-instance premise
  + mutation-idiom guard scenario).

## Impact

- `tests/test_select_ci_tests.py` (one helper + one guard test),
  `openspec/specs/ci-contract-baseline/spec.md` (delta).
- Closes #1511. Verification per its `Verification:` field:
  `uv run pytest -q tests/test_select_ci_tests.py`;
  `git ls-files '*.py' | xargs uv run ruff check`.
