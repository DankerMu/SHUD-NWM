# Tasks: ci-selector-subprocess-consumption-edge

## 1. Consumption edge (#1498)

- [x] 1.1 `_literal_path_consumer_index()` in the selector test
      suite: single AST pass, exact repo-relative-path string
      constants, targets = tracked non-suite `tests/` module paths,
      same file-level non-gated filter; **scan source EXCLUDES the
      selector meta-guard suite** (P1-1 — it enumerates paths as
      data; exclusion pinned by a test with rationale); implemented
      ONCE (no authority/executor twin); literal edges NEVER enter
      `_non_gated_top_level_importer_index()`'s return value (its
      raw consumers: #1499 pin + #1455 disposition guard) — union
      only in `_derived_support_module_importers`.
- [x] 1.2 `_derived_support_module_importers` = import index ∪
      literal index per module; guard failure message distinguishes
      edge kind via an optional `consumer_edges` label-source
      parameter defaulting lazily to the real literal index (built
      only when an offender message needs it — round-1 fix; label only;
      derived sets stay parameter-driven — the exact-message test
      at :2432-2442 stays green); branch (c) semantics unchanged.
- [x] 1.3 Fifth `SUPPORT_MODULE_TEST_RULES` entry:
      tests/mock_shud_omp.py → test_shud_runtime +
      test_direct_grid_e2e + test_e2e (comment: subprocess
      consumption rationale, ~27 s lane measurement, and the P2-3
      honesty note — test_e2e's consumption sits behind
      function-level e2e gating, 0 PR-lane mock assertions from that
      file; routed for closure integrity).
- [x] 1.4 0-consumer parametrize sample → DERIVED from the tree
      (union-empty modules; today exactly keliya/build.py,
      determination recorded); empty-set visibility per design
      decision 5 (skip-marked fallback param, never bare assert,
      never silent zero-collection); anti-vacuity figure 6→7 of 8;
      known-member anchors gain mock_shud_omp→test_shud_runtime.
- [x] 1.5 Rewrite
      `test_support_module_closure_guard_reds_on_a_gratuitous_zero_importer_selection`
      (:2447-2477) off mock_shud_omp (it stops being 0-consumer) —
      onto the derived zero-consumer module or a constructed select
      (P2-2; sanctioned existing-test change #2).

## 2. Equivalence pin (#1499)

- [x] 2.1 One pytest: derived sample (all 8 support modules + first
      3 sorted modules from `_directory_rule_audit_modules()` —
      NOT the gap-map universe, which is biased toward
      index-visible modules (Note-6)), one import-index build,
      per-module authority call, assert equality; sample derived,
      never hardcoded.
- [x] 2.2 Red evidence: constructed divergence (injectable seam or
      scratch probe; zero tracked mutation).

## 3. Guard evidence

- [x] 3.1 Routing pins for mock_shud_omp (⊇ 3 suites + meta-guard,
      targets exist + file-level non-gated).
- [x] 3.2 Red: rule entry dropped (constructed) → closure guard
      names the pair with consumer-suite wording.
- [x] 3.3 Whole-tree selection diff vs master: delta = exactly
      tests/mock_shud_omp.py (additive).
- [x] 3.4 #1455 disposition guard + #1487 carve-outs untouched
      (suite green unmodified there; real_backend byte-identity).

## 4. Spec delta

- [x] 4.1 MODIFIED "Support-module changes MUST select their
      non-gated importer suites" per design decision 7 ("importer
      suites" term KEPT with either-edge definition (P2-4); union
      authority + meta-guard scan-source exclusion; recorded-gap
      parenthetical retired; derived 0-consumer example; new
      subprocess-consumption scenario without PR-lane execution
      overclaim (P2-3); "closure completeness" WHEN broadened to
      both edge kinds (P2-5) with the import half keeping its
      "at top level" qualifier (round-1 P2 — dropping it overclaims
      function-body imports)); byte-faithful otherwise.

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q tests/test_select_ci_tests.py` green
      (115 → after; guard + pin wall-clocks reported, incl. the
      collection-time cost the derived parametrize adds to the
      collect-only smoke lane).
- [x] 5.2 Red evidence per 2.2/3.2 (honest labels for naturally
      green pins).
- [x] 5.3 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [x] 5.4 `openspec validate ci-selector-subprocess-consumption-edge
      --strict --no-interactive`; MODIFIED block difflib clean apart
      from the surgical spots.
- [x] 5.5 Whole-tree diff = exactly mock_shud_omp (list in PR body).
- [x] 5.6 Both issues' acceptance checkboxes mapped; Verification
      commands green.
