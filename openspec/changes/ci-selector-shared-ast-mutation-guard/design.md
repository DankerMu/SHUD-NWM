# Design: ci-selector-shared-ast-mutation-guard

Fixture level: compact (S; one test + one helper in one file; risk is
guard-precision — false positives blocking legitimate code, false
negatives missing real mutation idioms — not blast radius).

## Risk triage

- False positive: the scan is deliberately zero-tolerance for
  Assign-to-Attribute ANYWHERE in the module (it cannot know whether
  the target is an AST node). Today's count is zero (verified thrice:
  PR #1507 verifier, scribe re-scan, and this fixture's authoring
  check must re-confirm). A future legitimate non-AST attribute
  assignment would red the guard and force an explicit allowlist
  entry — intended friction in a meta-guard module, recorded in the
  guard's docstring.
- False negative (accepted boundaries — the guard pins the COMMON
  idioms; it is a tripwire, not a proof): attribute-callee `setattr`
  (e.g. a rebound alias `m.setattr(node, ...)` — the Name-only rule
  (v) deliberately skips attribute callees so `monkeypatch.setattr`
  stays legal), mutation inside an imported out-of-module helper,
  INDIRECT `NodeTransformer` subclasses (rule (iii) sees direct
  bases only), and `exec`-built code.

## Decisions

1. **Scan shape** (rewritten per fixture-review P2-2/P2-3/P2-4 —
   every rule re-verified zero-hit on this branch's HEAD): helper
   `_tree_mutation_offenders(tree) -> list[str]` returning
   "line N: <construct>" strings for six rule classes:
   (i) any `ast.Attribute` node whose `ctx` is `Store` or `Del` —
   ONE rule covering every assignment form (plain/augmented/
   annotated, tuple/starred targets, `for`/`with`/comprehension
   targets) plus attribute `del`, so the spec scenario needs no
   form-enumeration caveats;
   (ii) `ast.Subscript` with `Store`/`Del` ctx whose base (`.value`)
   is an `Attribute` — catches `tree.body[0] = x` /
   `del tree.body[0]` (the module's three subscript assigns all have
   `Name` bases: :499, :2129, :2132);
   (iii) ClassDef with a DIRECT base whose name ends
   `NodeTransformer` (indirect subclasses evade — recorded boundary);
   (iv) Call whose callee name (bare or attribute) is
   `fix_missing_locations` / `copy_location` / `increment_lineno`;
   (v) Call with a BARE `ast.Name` callee `setattr` / `delattr` —
   catches `setattr(node, "parent", p)`, the canonical generic
   parent-pointer idiom, with zero false positives:
   `monkeypatch.setattr` at :2945 is an ATTRIBUTE callee and is not
   matched. The issue CAVEAT forbids `setattr` only in the
   attribute-inclusive call-name list (iv) — this Name-only rule
   honors it;
   (vi) Call of a mutating list-method name (`append` / `extend` /
   `insert` / `remove` / `pop` / `clear` / `sort` / `reverse` —
   round-1 C1 completed the set: `dir(list) - dir(tuple)` non-dunder
   minus non-mutating `copy` is exactly these eight, and the dunder
   mutators route to rules (i)/(ii), so the enumeration IS the
   general class the spec names) whose receiver is an `Attribute`
   (e.g. `tree.body.append(x)`) — the module's append-family calls
   all have `Name` receivers today; a future legitimate
   attribute-receiver call reds the guard and forces an explicit
   allowlist entry, same intended-friction stance as (i).
2. **Guard test**: parse the suite's own source through
   `_parse_tracked(SELECTOR_META_GUARD_TEST)` (fixture-review Note-5:
   the module's existing single source for its own path, already
   imported at :30 — survives a rename; same entry point as all
   derivations, rides the cache, ~0 marginal cost) and assert the
   offender list is empty. Docstring records: pins the
   `_PARSE_CACHE` shared-instance premise ("every consumer only
   reads"), points at archived change `ci-selector-parse-memoization`
   design decision 4 and issue #1511.
3. **Red/clean arms are STANDING TESTS, not one-shot evidence**
   (fixture-review P1-1: a tripwire helper that rots into
   always-empty must red something in-tree — otherwise the
   audit-in-PR-body failure mode this change exists to kill just
   moves up one level). One parametrized red test with PER-MEMBER
   pinning (round-1 C3: the verifier's full kill matrix showed 7
   helper sub-paths — bare-name fixup branch, `_base_name`'s Name
   branch, both Del ctx arms, and the multi-member set entries —
   survive coarse one-per-rule params; per-member params make every
   deletable path kill ≥1 test): 20 params — the 6 rule-class
   originals plus bare-name fixup, `copy_location`,
   `increment_lineno`, attribute del, subscript del, bare-Name
   `NodeTransformer` base, bare `delattr`, and the 7 remaining list
   mutators (in-memory `ast.parse` of literal sources: the offending
   code lives inside string constants, which are `ast.Constant` in
   the real module's own tree, so the arms cannot self-trip) — each
   asserting the offender line + construct; one clean-source test
   (containing `monkeypatch.setattr(...)`, a Name-base subscript
   assign, and a Name-receiver `append`) asserting zero offenders —
   the no-false-positive pins for :2945, :499-style and
   append-family code. Final count: 123 existing + 3 new test
   functions = 145 node ids (guard + 20 red params + clean arm).
4. **No new module-level work at import time**: the guard parses
   inside the test body only; collect-only lanes pay nothing new.

## Must preserve

- All 123 existing tests pass unmodified (no edits/renames/skips).
- `_parse_tracked`, `_PARSE_CACHE`, `tests/conftest.py` untouched.
- `scripts/select_ci_tests.py` absent from the diff; selection
  behavior unchanged (this PR's own diff still routes to the
  meta-guard lane).

## Seams under test

The helper's tree parameter is the seam; real guard binds it to the
suite's own parsed source. No injectable config, no filesystem
fixtures.

## Test plan (maps to acceptance)

1. Guard green on the real module (123 → 124 passed).
2. Constructed reds: one per idiom class (4), each naming line +
   construct; constructed clean source stays green (setattr +
   subscript-assign no-false-positive pin).
3. `uv run pytest -q tests/test_select_ci_tests.py` green; ruff
   tracked clean; openspec validate strict.

## Risks to watch

- Do not let the scan wander into other files (`_tracked_python_
  files` etc.) — domain is exactly this module's own source.
- Keep the helper's category logic simple enough to read as a
  specification; no regex-on-source shortcuts (AST-node classes
  only).
