# Route the river-segment write-surface scan into the targeted CI lane (#2185)

## 1. Why

`tests/test_river_segment_write_surface_scan.py` is the boundary guard for `fix-national-digest-cache-identity-2031`'s governing invariant: exactly one in-place `UPDATE core.river_segment` exists in production code, it lives in `workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry`, and it bumps `core.river_network_version.geometry_generation` in the same transaction. A second in-place rewrite added anywhere under `apps/`, `services/`, `workers/`, `packages/` or `scripts/` would move geometry with no cache-key rotation, and every digest test in the repo would stay green, because they all run against the one function that does bump.

`scripts/select_ci_tests.py` does not route a single production path to that scan. Measured on this branch's base (`c21bacf9`), over all 3653 tracked non-test paths, the number that select `tests/test_river_segment_write_surface_scan.py` is **zero** — including the two modules the scan names by literal (`BACKFILL_MODULE` at `tests/test_river_segment_write_surface_scan.py:50`, `UPSERT_MODULE` at `:52`) and the shared registry module the issue's grep evidence adds:

```
workers/model_registry/basins_registry_import.py   -> 0
workers/model_registry/qhh_production_bootstrap.py -> 0
packages/common/model_registry.py                  -> 0
```

The gate is therefore live, not latent: a PR that adds `UPDATE core.river_segment` to either worker module today reaches the targeted `unit-test-targeted` lane with a non-empty, plausible-looking selection that does not contain the guard. The zero-assertion `--collect-only` degradation does not fire, because the selection is not empty — it is merely missing the one suite that would have caught the regression. The scan itself is invisible to both of the selector's structural auto-derivations: it imports only `ast`, `re`, `dataclasses` and `pathlib`, so the importer-closure derivation finds no production module to attribute it to, and there is no production module named `river_segment_write_surface_scan` for the same-name derivation to match.

## 2. What changes

`scripts/select_ci_tests.py` gains a constant pair and a purely additive routing loop, in the shape #1656 established for `tests/test_timescale_write_guard_wire_site_invariant.py` (`scripts/select_ci_tests.py:251`, `:258`, loop at `:4317`):

- `RIVER_SEGMENT_WRITE_SURFACE_TEST` — the suite path.
- `RIVER_SEGMENT_WRITE_SURFACE_ROOTS` — the five root globs the scan walks, spelled `apps/**`, `services/**`, `workers/**`, `packages/**`, `scripts/**`.
- Inside `select_tests`, a loop over `changed` that adds the suite for every `.py` path matching one of those roots. Set-union only: it does not assign `matched`, does not participate in `stop_on_match`, and does not change whether a path counts as known for the unknown-backend fallback.

`tests/test_select_ci_tests.py` gains meta-guards that derive the required root set from the scan's own source, so adding a sixth directory to the scan without wiring it here reddens by name.

The routing shape is copied from #1656. **The derivation shape is not, and cannot be.** The timescale suite exposes a `_scan_roots()` FunctionDef whose final `return` is a tuple of `REPO_ROOT / "workers"` `ast.BinOp`/`ast.Div` chains, and the #1656 meta-test's `_invariant_scan_roots()` walks exactly that node shape. The river-segment scan exposes a module-level binding instead:

```python
# tests/test_river_segment_write_surface_scan.py:48
PRODUCTION_DIRS = ("apps", "services", "workers", "packages", "scripts")
```

whose value is a tuple of bare string constants, consumed at `:100` by `for directory in PRODUCTION_DIRS: root = REPO_ROOT / directory`. The #2185 derivation must locate that binding by target name and read its constants; reusing `_invariant_scan_roots()` or its `REPO_ROOT / "workers"` walker would find nothing.

Locating it by walking `tree.body` for `ast.Assign`/`ast.AnnAssign` is not enough, and round 1 measured why: `PRODUCTION_DIRS += ("db",)` yields module-level node types `['Assign', 'AugAssign']` and exactly **one** binding under that collection, so the derivation returns the stale five-element tuple, which still equals the selector constant — nothing reds while the scan walks six directories. The shipped derivation therefore collects every `ast.Name` store of the identifier anywhere in the module (`ast.walk`), requires exactly one, and requires that one to be a module-level `Assign`/`AnnAssign` it can read. That also closes the module-level `if some_flag: PRODUCTION_DIRS = (...)` and `for PRODUCTION_DIRS in ...:` shapes, neither of which is a direct child of `tree.body`. A rebind producing no `ast.Name` store at all (`globals()["PRODUCTION_DIRS"] = ...`) stays out of reach, and the backstop for it is narrower than it looks: the scan's own runtime non-vacuity assertion at `tests/test_river_segment_write_surface_scan.py:123` catches such a rebind only if it executes AFTER `_LITERALS = _sql_literals()` at `tests/test_river_segment_write_surface_scan.py:110`. `:110` materializes the whole walk, so a store-less rebind above that line is walked by the scan itself and both sides of the `:123` comparison read the same rebound name — six directories on the left, six on the right, green. See `tasks.md` §6 D12.

## 3. Measured blast radius and cost

Mechanical whole-repo differential, base selector (`c21bacf9`) versus candidate, one changed path at a time, over all 3653 tracked non-test paths:

| quantity | value |
|---|---|
| paths whose selection gains exactly `tests/test_river_segment_write_surface_scan.py` | 457 |
| paths that lose any target | 0 |
| paths with any delta other than that single gain | 0 |
| tracked non-test `.py` files under the five roots | 457 |

The gain set and the root-membership set are the same 457 paths, so the routing neither over- nor under-reaches its declared roots. Per root: `apps` 21, `packages` 120, `scripts` 86, `services` 174, `workers` 56.

Lane cost, measured locally on this branch (`uv run pytest -q tests/test_river_segment_write_surface_scan.py --durations=0`): **4 tests, 2.06s cold / 1.90s warm**. The figures are wall-clock on a loaded developer machine and drift with load; what the Evidence Floor re-checks is the test count and an order-of-magnitude bound, not the decimals. The dominant term is the module-level `_LITERALS = _sql_literals()` AST parse of every `.py` file under the five roots, which is paid once per session regardless of how many of the four tests run. The issue quotes 1.85s; the figures above are this branch's own measurement.

`packages/**` is genuinely wider than the #1656 precedent's `packages/common/**`: `packages/` also holds `packages/scheduler/` and a top-level `packages/__init__.py`. That width is correct rather than incidental — the scan walks `packages/` whole, and its own non-vacuity assertion at `:123` pins `{path.split("/")[0] for path in scanned_files} == set(PRODUCTION_DIRS)`, so a literal under `packages/scheduler/` is inside the invariant's blast radius and must be able to red the guard.

## 4. Must-preserve behavior

- No existing rule's targets shrink. The differential above is gain-only over all 3653 paths and is re-run as an Evidence Floor clause.
- `services/brand_new_thing.py` continues to arm the unknown-backend fallback (`CORE_SMOKE_TESTS`) and continues **not** to select `tests/test_timescale_write_guard_wire_site_invariant.py` — the #1656 supplemental route stays scoped to its own four roots. The existing assertions for that at `tests/test_select_ci_tests.py:10365-10366` use `not in` and `<=`, so they stay green unmodified and stay meaningful.
- The scan's own assertions, scope, `PRODUCTION_DIRS` membership and the SQL in the two worker modules are untouched.
- Existing exact-equality selection assertions in `tests/test_select_ci_tests.py` whose input is a `.py` path under one of the five roots now legitimately gain one target. Each such expectation is updated by adding the scan suite — never by loosening the assertion from equality to containment, and never by narrowing a root to dodge the update.

## 4b. The one semantic flip: `apps/**` outside `apps/api/` leaves the pinned-empty class

`BACKEND_PYTHON_SOURCE_PREFIXES` (`scripts/select_ci_tests.py:4371-4377`) is `apps/api/`, `packages/`, `services/`, `workers/`, `scripts/`. Every `.py` under `apps/` that is not under `apps/api/` therefore sits outside the backend prefixes while sitting inside the scan's `PRODUCTION_DIRS`, and that class stops selecting nothing.

**This is a change today, not only for a future path.** Of the 3030 tracked non-test paths whose base selection is empty, exactly one becomes non-empty:

```
apps/__init__.py    base: []    candidate: ['tests/test_river_segment_write_surface_scan.py']
```

`apps/__init__.py` is a tracked file. The remaining member of the flipped class, `apps/frontend/**/*.py`, has no tracked files today; `apps/`'s 21 tracked non-test `.py` are 1 at the top level, 11 in `apps/api/` and 9 in `apps/api/routes/`.

The flip is deliberate and is the direction the pin's own record anticipates. `tests/test_select_ci_tests.py:5417-5428` states the seven params are "pins, NOT endorsements", with "the remaining classes belong to a future route-A/B (selector-widening or empty-selection-fails) decision". This change is that selector-widening for one of them, so it also modifies the archived requirement that pinned the class — see the `MODIFIED Requirements` block in the spec delta. Adding only new requirements while leaving `openspec/specs/ci-contract-baseline/spec.md:188-197` asserting an empty selection for this class would leave the permanent spec self-contradictory, and `openspec validate --strict` would not catch it: it checks structure, not cross-requirement consistency.

The trade is stated rather than buried, with its actual mechanism. Before: such a path selected nothing, `count=0`, so `.github/workflows/ci.yml:389-390` ran the whole-tree collect-only zero-assertion smoke with a loud warning annotation. After: `count=1`, so `ci.yml:354` takes the targeted branch and runs one suite carrying four real tests, and the collect-only smoke does not run. Neither carve-out re-arms it: `meta_guard_only` is false (the one element is not the selector meta-guard suite), and `collection_smoke_required` is false both before and after, because neither the selector source nor its suite is in such a diff. Measured on both selectors:

```
base  apps/__init__.py  count=0  meta_guard_only=false  collection_smoke_required=false
cand  apps/__init__.py  count=1  meta_guard_only=false  collection_smoke_required=false
```

The alternative — excluding the non-prefix part of `apps/` from the routed roots — is rejected. It would break the root set's derivation from the scan's own `PRODUCTION_DIRS`, which is the issue's stated acceptance criterion and the only thing that makes a sixth scanned directory red by name; and it would be untrue to the scan, which really does parse every `.py` under `apps/`.

## 4c. Measured existing-test blast radius

Running the full selector meta-guard suite against the candidate selector (tracked source patched locally, then restored) gives **31 failures out of 679** (`648 passed in 295.28s`), across 30 test functions.

The absorption is **not** uniform, and the fixture says so because an implementer told "add one target to each" would be unable to execute the instruction on three of them:

| shape | count | absorption |
|---|---|---|
| `select_tests(...) == an exact list or set` | 28 | add `tests/test_river_segment_write_surface_scan.py` to the expected set |
| `assert fields["count"] == "2"` (`tests/test_select_ci_tests.py:5634`) | 1 | change the count string to `"3"`; the redrun diff is literally `- 2 / + 3` |
| the `py-under-apps-frontend` empty-selection pin (`tests/test_select_ci_tests.py:5457`) | 1 | move the param out and replace it with a positive assertion |

The table comes from an AST pass over all 30 failing functions rather than a sample, but it classifies only equality comparisons, so it is not exhaustive. Implementation found a fourth shape it missed: `tests/test_select_ci_tests.py:701` also asserts `all("::" in test_path for test_path in selected)`, and the supplemental rider is a whole-file path. That one is absorbed by excluding the scan by name (`... for test_path in selected if test_path != WRITE_SURFACE_SCAN_PATH`), which keeps the redirect's "focused node ids only" semantics pinned instead of loosening the check.

Two of the 28 carry a second thing the added target moves:

- `tests/test_select_ci_tests.py:682` — `assert len(selected) == 1 + len(DIRECT_GRID_CONTRACT_TESTS) + len(DIRECT_GRID_CONTRACT_IMPORTER_TESTS) + 1`. The trailing `+ 1` is #1656's rider; it becomes `+ 2`.
- `tests/test_select_ci_tests.py:857-858` — a ledger comment reading "the literal below — not the arithmetic above — is the authority: it holds 47 entries, the rule's 46 targets plus the meta-guard rider". After absorption it holds 48, and the new entry is a supplemental rider rather than a rule target. The assertion does not red when this comment goes stale, so the comment is a task, not a side effect.

Three other failing functions carry `len(...) == N` assertions that must be left alone — `tests/test_select_ci_tests.py:2631`, `:5145` and `:12350` count matching *rules* or explicit targets, not selected suites, so the added supplemental rider does not move them. Only `:682` is a selection-cardinality assert.

No expectation is loosened from equality to containment: exact-equality is the property that lets these tests catch an *unintended* widening, and trading it away to absorb an intended one would disarm them permanently.

## 4d. Adjacent comment prose that this change falsifies

The #2098 review loop's entire finding set was prose drifting from code. Two comments in `tests/test_select_ci_tests.py` describe guarantees this change deliberately breaks, and neither reds on its own:

- `:5005-5009` — "an unknown backend Python path selects exactly the five core-smoke suites and NO meta-guard rider… so a refactor that gains a sixth target on every unknown route reds here before it silently costs ~15 s across the whole tree". This change **is** that refactor; the comment must record the new count and that the extra target is #2185's supplemental rider, along with its measured cost from §3.
- `:5626-5631` — "the meta-guard PLUS the write-site invariant… the two targeted suites". It becomes three.

Provenance comments that name only `#1656` beside a rider that now has two sources (`tests/test_select_ci_tests.py:678-680`, `:5041-5042`, `:5162-5165`, and every other comment beside an expectation this change edits) must name `#2185` as well, or a reader will attribute the new element to the timescale route.

## 5. Seams under test

- **Selector routing seam** — `select_tests(changed, repo_root)` for a path under each of the five roots, existing and future-shaped, asserted as set membership plus the gain-only differential.
- **Root-authority seam** — the scan's `PRODUCTION_DIRS` binding as the single source of the required root set, versus the selector's `RIVER_SEGMENT_WRITE_SURFACE_ROOTS`, versus what the routing loop actually consults. All three must be pinned to each other: a mutant that inlines the roots in the loop and leaves the constant as decoration passes any test that never calls `select_tests`.
- **Additivity seam** — `matched`, `stop_on_match` and unknown-path fallback behavior are unchanged by the new loop.
- **CI-output seam** — the flipped class's `count` / `meta_guard_only` / `collection_smoke_required` fields, which is where §4b's accepted trade actually lives.

## 6. Risk triage

Fixture level: **compact**. One constant pair, one additive loop, and meta-guards, all inside the CI selector's structural layer. There is no runtime surface, no DB, no HTTP route, no app composition, and therefore no runtime mutation-proof fixture of the kind that made #2098 expanded. Round-1 reviewer seats: 2.

Selected risk packs: `correctness` (does the routing select what it claims, for existing and future paths), `test-evidence` (does the meta-guard actually red when routing or a root is removed, through a positive oracle rather than a mirrored constant, and does it survive the mutants the fixture review already constructed), `spec-compliance` (the `MODIFIED Requirements` block must leave the archived capability internally consistent — `openspec validate --strict` cannot check that).

Not selected: `security-perf` — the change adds 4 tests / ~2s to backend Python diffs and touches no auth, secret, or data path; the cost is measured in §3 and the trade is the whole point of the issue. `invariant-state` — no state machine or persisted invariant is touched; the invariant this guards is enforced by the scan, which is unmodified. `integration` is covered by the Evidence Floor's whole-tree differential rather than by a dedicated seat at compact level.

## 7. Non-goals

- Changing the scan's assertions, its `PRODUCTION_DIRS` membership, or any SQL in `basins_registry_import.py` / `qhh_production_bootstrap.py`.
- A separate rule for `services/tiles/mvt.py`: it is read-only and carries no write literal, so it is covered by the `services/**` root like any other module and needs no at-site entry.
- `services/precip/**` routing (#2122), node-27 systemd rows (#2180), and the full-run degradation tracked by #2044.
- Reviving `only_when_any_changed` for `PATH_TEST_RULES` (#2198). The new loop deliberately does not use it — the field is dead for path rules and the routing is unconditional by design.
- Repairing the two weaknesses the fixture review found in the #1656 precedent itself (`tests/test_select_ci_tests.py:10257-10260` says "final return" where `:10273` takes the first; `:10372-10385`'s `monkeypatch` is decorative because the oracle receives the reduced tuple directly). #2185's own block is built without both weaknesses; fixing the precedent in place is tracked as #2230.

## 8. Deviations

Full text, with the measurements behind each, is in `tasks.md` §6 (D1–D15). Summary:

- **D1–D5 (implementation, at `776cbec5`)**: 2.9's placement anchors moved; 3.1's "28 exact-set expectations" was low by two because a fourth assertion shape at `tests/test_select_ci_tests.py:701` was not classified by the AST pass; 2.5's negative probe had to be a literal because the obvious derivation yields a probe that legitimately selects the scan; the verifier's 4.7 clause was re-scoped by AST after a leftmost-match false pass; the verifier's 4.11 clause gained comment/whitespace normalization.
- **D6–D7 (round-1 fix pass, spec shape)**: the delta's first ADDED requirement became a MODIFIED extension of the archived `Tree-scanning invariant suites MUST follow every scanned source root`, and three further archived requirements are now MODIFIED. A mechanical sweep of every scenario in every archived spec found 11 selection-shaped scenarios naming a gaining path; 4 use `emits exactly` and were falsified, all under `openspec/specs/ci-contract-baseline/spec.md:909` and `:958`. `:637` needed no change — its scenario already carries the "unless another requirement explicitly adds a supplemental oracle" clause.
- **D8 (round-1 fix pass, dropped scenario)**: the `MODIFIED` block for `Empty targeted-test selection MUST be loudly self-identifying` had silently dropped `#### Scenario: selector-development PRs fire the flag honestly`, because the authoring brief said the archived requirement spans `:139-215` when it spans `:139-225`. Restored verbatim, and Evidence Floor clause 4.16 now checks scenario carry-over mechanically for every MODIFIED requirement.
- **D9–D11 (round-1 fix pass, content)**: the derivation's binding collection was widened as described in §2; the narrowed empty-selection class sentence was wrong in both directions (`db/**` `.py` is not empty; `.agents/**` was missing from the enumeration) and both are corrected with a new pinning scenario; and the hardcoded "four executed assertions" was removed from the permanent spec.
- **D12–D14 (round-1 fix pass, self-inflicted)**: the implementer corrected MY brief — the residual boundary holds only for a rebind executing after `tests/test_river_segment_write_surface_scan.py:110`, so the over-generous backstop claim was rewritten to what is actually true (D12); the hardened derivation's SECOND guard shipped with no red leg at all, because all four existing fixture cases place an ordinary binding first and trip the first guard, so two single-store/zero-module-level-binding cases were added and Evidence Floor 4.7 became a two-leg mutation requiring the guards to red different tests (D13); the new test's `# #2185` comment sits on the first body line rather than above the `def`, following its four neighbours (D14).
- **D15 (Phase 7 and its follow-up)**: the fixture used two `path:line` reference frames without saying so. Four citations were flagged; measured, they were two defects (one genuine merge drift, one plain off-by-one that was never right in any tree) and two refutations (both correct at base, flagged because the reviewer measured them at the branch tip). One of the two refuted citations was "corrected" to the tip value in the same commit that declared the base frame — the follow-up review called that a P1 and it was reverted. The frame is now declared at the top of `tasks.md`, and Evidence Floor 4.12b closes the gap mechanically.
