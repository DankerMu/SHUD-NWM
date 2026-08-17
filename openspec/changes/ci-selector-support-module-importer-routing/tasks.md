# Tasks: ci-selector-support-module-importer-routing

## 1. Routing

- [ ] 1.1 `SUPPORT_MODULE_TEST_RULES` tuple (4 exact-path entries:
      in_memory_grid_snapshot.py → 5 mapping_builder suites;
      slurm_template_helpers.py → production_slurm_validation +
      slurm_array_contract; river_identity_backfill_fakes.py → the
      two node27_river_identity_backfill suites; tests/__init__.py →
      test_integration_gate + the two node27_timeseries_compression
      suites per fixture-review P1-1); consulted in the changed-test
      branch for non-suite paths after the CHANGED_TEST_FILE_RULES
      loop (interaction comment per design decision 1); a matched
      module selects its importer suites PLUS the meta-guard suite
      (design decision 2); unmatched support paths keep the existing
      fallback byte-for-byte.
- [ ] 1.2 Every routed target verified existing + file-level
      non-gated at authoring time (the guard re-checks from the tree
      thereafter). Runtime evidence: the issue's nine suites 549
      tests / collect ~0.4-0.7 s; tests/__init__.py's three suites
      measured 454 passed in 40.18 s local (fixture re-review) —
      inside the lane budget. The tests/__init__.py rule entry
      carries a comment explaining why a 0-byte package file has a
      rule (`from tests import X` importers join its derived set by
      the package-aliasing authority).

## 2. Guard

- [ ] 2.1 Closure guard: every tracked non-suite `tests/**/*.py`
      (via `is_test_suite_path`) either (a) carve-out-allowlisted,
      (b) derived-importers ⊆ `select_tests([module])`, or
      (c) 0-importer and selects exactly `[SELECTOR_META_GUARD_TEST]`;
      semantic authority `_non_gated_top_level_importer_tests`
      executed via the inverted index (design risk: 8.35s vs 0.99s);
      never frozen; failure names module + missing suite.
- [ ] 2.2 Anti-vacuity: pre-allowlist derived universe non-empty AND
      ≥3 modules derive non-zero importers (today 6 of 8), AND one
      known-member anchor per routed module (Note-8).
- [ ] 2.3 Carve-out allowlist
      (`tests/integration_helpers.py`, `tests/conftest.py`) recorded
      as an issue-scope boundary with the measured partial-coverage
      facts in its comment (P2-4: `-m integration` covers 75/245 and
      0/19), pinned verbatim inside ci.yml's `database:` filter block
      (block-scoped text pin, not whole-file grep).
- [ ] 2.4 Red evidence via injectable mapping/selection seams
      (constructed inputs; zero tracked mutation): missing importer
      suite; gratuitous 0-importer selection; allowlist entry absent
      from the ci.yml block.
- [ ] 2.5 Domain comment: this guard covers `tests/` support modules;
      #1455's disposition guard covers the nine production
      directories; #1486's closure guard covers
      GUARDED_MODULE_CLOSURES — no overlap claimed.
- [ ] 2.6 Stale-target guard union extension (Note-7):
      `test_every_pinned_node_id_resolves_to_an_existing_test_function`
      (and siblings unioning rule targets) gains
      `SUPPORT_MODULE_TEST_RULES`.
- [ ] 2.7 Rescope
      `test_every_tracked_tests_support_module_selects_only_the_meta_guard_suite`
      (:1090-1109) to carve-out + 0-importer modules (class-boundary
      assert kept) and amend the stale prose at :1402-1407 (P1-3 —
      the ONLY permitted existing-test change).

## 3. Docs

- [ ] 3.1 Third-mode line amended in `instructions/agents/shared.md`
      + `CLAUDE.md` + `AGENTS.md`, byte-identical across the three
      carriers (importer-bearing support modules now select real
      suites; conftest.py/0-importer files still collapse).

## 4. Spec delta

- [ ] 4.1 ADDED requirement (SHALL first line, 4 scenarios: routed
      selection incl. meta-guard rider, mechanized closure red,
      carve-out exclusion honestly worded, 0-importer meta-guard
      route) + TWO MODIFIED requirements (P1-2): "Empty targeted-test
      selection MUST be loudly self-identifying" (surgical clause
      rescope + descriptive cross-reference) and "Changed-test PRs
      MUST run the selector meta-guards" (routed support modules map
      to importer suites + meta-guard; support-file scenario
      rescoped; "collectible test files" tail intact); byte-faithful
      otherwise.

## 5. Evidence Floor

- [ ] 5.1 `uv run pytest -q tests/test_select_ci_tests.py` green
      (count before 102 → after; guard wall-clock reported).
- [ ] 5.2 Red evidence per task 2.4 (honest labels for
      naturally-green pins).
- [ ] 5.3 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [ ] 5.4 `openspec validate ci-selector-support-module-importer-routing
      --strict --no-interactive` passes; BOTH MODIFIED blocks difflib
      clean apart from the surgical rescopes.
- [ ] 5.5 Whole-tree selection diff vs master: delta = exactly the
      four routed support modules (list in PR body).
- [ ] 5.6 Issue #1487 acceptance checkboxes each mapped to a test or
      recorded evidence line (checkbox 6's example set corrected per
      P1-1 — recorded deviation); Verification commands green.
