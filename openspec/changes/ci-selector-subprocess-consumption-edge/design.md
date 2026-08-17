# Design: ci-selector-subprocess-consumption-edge

## Change surface

- `tests/test_select_ci_tests.py`: derivation helpers
  (`_non_gated_top_level_importer_tests` :829 authority,
  `_non_gated_top_level_importer_index` :1876 executed form,
  `_derived_support_module_importers` :2336 feeding
  `_support_module_closure_offenders` :2344ff with its three-way
  branch — carve-out / derived ⊆ selection / 0-importer equality),
  the 0-importer parametrize sample :2561, the routing pins, the
  stale-target union.
- `scripts/select_ci_tests.py`: `SUPPORT_MODULE_TEST_RULES` (4
  entries after #1487).

## Verified facts (orchestrator-derived at 4fb0172e)

- `tests/mock_shud_omp.py`: zero imports repo-wide; consumed via
  `workers/shud_runtime/runtime.py:2881`
  `[sys.executable, executable, *args]`. Exact literal
  `"tests/mock_shud_omp.py"` appears in three tracked top-level
  suites OUTSIDE the excluded meta-guard suite (four including it —
  the exclusion in decision 1 is what makes it three), none
  file-level gated:
  `tests/test_shud_runtime.py` (:234 shared `_runtime()` helper +
  :933/:970/:1093 …), `tests/test_direct_grid_e2e.py` (:206,:238),
  `tests/test_e2e.py` (:901). `test_e2e.py`'s e2e gating is
  function-level `@pytest.mark.e2e` (:531,:584) — NO file-level
  pytestmark, so the file-level filter authority (the same one the
  import derivation uses) includes it; its e2e functions auto-skip
  in the PR lane (measured whole file: 4 collected / 2 passed /
  2 skipped / 0.29 s).
- Suite costs: test_shud_runtime + test_direct_grid_e2e = 251
  passed / ~27 s; test_e2e whole file 2 passed / 2 skipped / 0.29 s.
  Lane total well under budget. HONEST LIMIT (fixture-review P2-3):
  test_e2e.py's mock consumption sits inside `_run_shud_runtime`
  whose call sites are all in function-level `@pytest.mark.e2e`
  tests, auto-skipped in the PR lane — routing it preserves closure
  integrity (the edge exists in the tree) but buys ZERO mock
  assertions in the PR lane; the rule comment and spec scenario must
  say so, never claim it "executes" the mock there.
- `tests/fixtures/mapping_builder/keliya/build.py`: true
  zero-consumer — zero imports AND zero literal-path references in
  any test source OUTSIDE the meta-guard suite; its own docstring:
  "The test suite reads the checked-in files directly and never
  invokes this script."
- No OTHER support module gains a literal-path edge (scan of all
  tracked top-level suites' string constants against all 8 support
  module paths, WITH the meta-guard suite excluded from the scan
  source per decision 1, yields hits only for mock_shud_omp; the
  fixture review verified that WITHOUT the exclusion the meta-guard
  suite's own path-as-data literals — e.g. :673/:1058/:2306/:2561 —
  would falsely mark it a consumer of all 8 modules).
- Helper/index equivalence currently holds for all 8 support modules
  (sampled 0/3/5/2 on 4 of them in #1499's evidence; full check is
  the new pin's job). Index build ≈ 1.05 s; per-module helper
  ≈ 1 s/module.

## Key decisions

1. **Edge model**: consumption edges are a SECOND edge type derived
   guard-side, not a selector-runtime behavior change. One new
   single-pass index function `_literal_path_consumer_index()`
   scans tracked top-level test suites' AST `ast.Constant` string
   values for EXACT equality with tracked non-suite `tests/` module
   repo-relative paths. **Scan-source exclusion (fixture-review
   P1-1): the selector meta-guard suite
   (`tests/test_select_ci_tests.py`) is excluded from the scan
   source set** — it enumerates support-module paths AS DATA (rules,
   pins, parametrize samples), which would otherwise make it a
   phantom "consumer" of all 8 support modules, clearing the
   zero-consumer domain and neutering anti-vacuity; the meta-guard
   rider already joins every routed selection, so the edge carries
   zero information. The exclusion is pinned by a test and its
   rationale recorded at the exclusion site. Exact full-path
   equality only (all live sites use the full literal); basename or
   suffix matching is rejected — false positives would manufacture
   phantom consumption edges that force spurious rules. IMPLEMENTED
   ONCE: a single index-form function with no per-module authority
   twin (decision feeds #1499's lesson — a second implementation
   pair would need a second equivalence pin). Literal edges NEVER
   enter `_non_gated_top_level_importer_index()`'s return value —
   the #1499 pin and the #1455 disposition guard both consume that
   index raw; the union happens only downstream (decision 2).
2. **Union feeds the existing three-way guard unchanged**:
   `_derived_support_module_importers` becomes the union of the
   import index and the literal-path index per module. Branch (c)'s
   equality needs NO new vocabulary: mock_shud_omp's union is
   non-empty so it moves to branch (b); keliya/build.py stays in
   (c). The guard failure message distinguishes edge kind ("importer
   suite" vs "literal-path consumer suite") for the reader —
   RULED (iteration-2 P2-1, option b-parameterized):
   `_support_module_closure_offenders` gains an optional
   `consumer_edges` parameter DEFAULTING to the real literal-path
   index, used ONLY for choosing the label wording; the derived
   sets still come exclusively from the `derived` parameter, so the
   existing exact-message test at :2432-2442 stays green (a
   constructed newcomer absent from `consumer_edges` falls back to
   the "importer suite" wording) and tasks 3.2's red arm gets the
   consumer-suite wording for real literal edges. The docstring's
   "every input is a parameter" property is preserved — the label
   source is itself a parameter.
3. **Scope of the literal scan**: targets are tracked non-suite
   `tests/` module paths ONLY (the 8 support modules' repo-relative
   paths). Production modules are out of domain — the #1455
   disposition guard's derivation and domain are byte-untouched
   (whole-tree diff proves it: delta = exactly
   `tests/mock_shud_omp.py`).
4. **Routing**: fifth `SUPPORT_MODULE_TEST_RULES` entry —
   `tests/mock_shud_omp.py` → (`tests/test_shud_runtime.py`,
   `tests/test_direct_grid_e2e.py`, `tests/test_e2e.py`) + the
   meta-guard rider added by the branch hook (#1487 convention,
   unchanged). Entry comment records the subprocess-consumption
   rationale, the ~27 s lane measurement, AND the P2-3 honesty note
   (test_e2e's consumption is behind function-level e2e gating —
   0 PR-lane mock assertions from that file). The known-member
   anchor set (anti-vacuity) gains mock_shud_omp →
   test_shud_runtime.
5. **0-consumer sample rewrite** (Note-8 hardened): the
   parametrize sample at :2561 is DERIVED from the tree — the
   modules whose union edge set is empty — rather than hardcoded
   (single-element parametrize is mechanically fine; deriving keeps
   the "derivation is authority" line consistent and self-heals).
   Today that derives exactly
   `tests/fixtures/mapping_builder/keliya/build.py`; the keliya
   determination (docstring + zero references) recorded in a
   comment. EMPTY-SET VISIBILITY (iteration-2 P2-2): if the derived
   zero-consumer set ever becomes empty (every support module has
   consumers — a legal terminal state), the parametrize must not
   silently collect zero cases: emit one
   `pytest.param(..., marks=pytest.mark.skip(reason="zero-consumer
   domain is empty — collapse-route guard needs re-decision"))`
   fallback, never a bare `assert sample` (which would false-red the
   legal state) and never a silent vanish. The anti-vacuity floor
   "≥3 modules with non-zero derived" now counts 7 of 8 — update
   the recorded figure. Collection-time cost of the derived sample
   (~1 s index + literal scan) also lands on the full-tree
   collect-only smoke lane — record it in the 5.1 wall-clock note.
6. **Equivalence pin (#1499)**: one test, sample = all tracked
   non-suite `tests/` support modules (derived) + the first N (e.g.
   3) sorted modules from `_directory_rule_audit_modules()` (159
   modules, 0.07 s, already sorted — Note-6: the gap-map universe is
   BIASED because it only contains modules the index already reports
   ≥1 importer for, which is exactly the regression shape the pin
   exists to catch; the audit-modules list is unbiased and cheaper). Build the import
   index once; per-module call the authority helper; assert
   equality. Expected cost ≈ index 1 s + N+8 helper calls ≈ 12 s —
   acceptable; if too slow, shrink the production sample, never the
   support-module half (they are the guards' live domain). Red
   evidence: constructed divergence via an injectable index/helper
   seam OR a scratch-tree probe — zero tracked mutation.
7. **Spec delta**: MODIFIED "Support-module changes MUST select
   their non-gated importer suites" only. Surgical rewrites:
   (a) first line — the term "importer suites" is KEPT throughout
   (P2-4 terminology bridge: the body defines it as suites reaching
   the module by EITHER edge kind — top-level import, or exact
   repo-relative literal-path consumption — so the requirement name
   and the two cross-referencing requirements stay coherent without
   their own MODIFIED blocks; fixture review verified their
   routed/unrouted language is edge-agnostic); the derivation
   authority sentence gains the union + the meta-guard scan-source
   exclusion; (b) the "(… remain a recorded gap)" parenthetical is
   RETIRED, replaced by the exact-literal boundary note; (c) the
   0-consumer scenario's examples become the derived zero-consumer
   module; (d) ONE new scenario for subprocess-consumed routing
   whose wording does NOT claim PR-lane execution for
   function-gated consumers (P2-3); (e) the "closure completeness
   is mechanized" scenario's WHEN broadens to "importing, or
   carrying the repo-relative literal path of, a routed support
   module" (P2-5 — the original rot direction, a NEW consumer
   appearing without a rule extension, must be covered for both
   edge kinds). Byte-faithful otherwise (difflib zero substantive
   deletions beyond the surgical spots).

## Must preserve

- All 115 existing selector tests green EXCEPT the sanctioned
  rewrites (fixture-review P2-2 completed the list):
  (a) the 0-consumer parametrize sample (:2561 — becomes derived,
  loses mock_shud_omp);
  (b) `test_support_module_closure_guard_reds_on_a_gratuitous_zero_importer_selection`
  (:2447-2477) — uses mock_shud_omp with the REAL selection and
  asserts the offender lists exactly [meta-guard, test_gateway];
  the 5th rule breaks it — rewrite onto the derived zero-consumer
  module or a constructed select;
  (c) anti-vacuity figure 6→7 and the anchors-equal-routing-table
  assert gains the 5th anchor.
  `test_unrouted_tests_support_modules_select_only_the_meta_guard_suite`
  auto-adjusts via its rule-table scoping (verified by fixture
  review — no edit). Any other existing-test change is an
  undisclosed deviation.
- Whole-tree selection diff vs master: delta = EXACTLY
  `tests/mock_shud_omp.py` (additive: 3 suites + meta-guard).
- #1455 disposition guard domain/derivation untouched; #1487
  carve-outs untouched; real_backend.py byte-identical;
  `--github-output` and stdout format unchanged.

## Seams under test

Existing injectable seams of `_support_module_closure_offenders`
(modules/derived/select/carveouts); the new literal-path index takes
an injectable suite-source mapping (or file list) for red evidence;
the equivalence pin's divergence red via constructed inputs. Zero
tracked mutation.

## Test plan (maps to acceptance)

1. #1498 acceptance: `select_tests(['tests/mock_shud_omp.py'])` ⊇
   {test_shud_runtime, test_direct_grid_e2e} (+ test_e2e + meta-guard
   pinned too); closure guard reds on a missing consumption edge
   (constructed: drop the rule entry → offender names the pair with
   the consumer-suite wording); 0-consumer sample = keliya/build.py
   with determination recorded.
2. #1499 acceptance: equivalence pin over the derived sample; red
   evidence for divergence; sample derived not hardcoded.
3. Whole-tree diff = exactly mock_shud_omp (recorded).
4. Both issues' Verification commands green.

## Risks to watch

- Literal scan false negatives: a future consumer spelling the path
  as `Path(__file__).parent / "mock_shud_omp.py"` produces no exact
  full-path constant. Accepted boundary — recorded in the guard
  comment (exact-literal edges only; the alternative substring
  matching buys false positives). The 10 live sites all use the
  full literal.
- Wall-clock: the literal scan must ride the SAME single AST pass as
  the import index (collect string constants alongside imports), not
  a second parse of 190 files.
- Do not let the union leak into `_directory_rule_importer_map`
  (#1455 domain) — support-module paths never appear there, but keep
  the union scoped to `_derived_support_module_importers`.
