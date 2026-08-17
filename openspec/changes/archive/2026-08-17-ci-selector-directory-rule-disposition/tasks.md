# Tasks: ci-selector-directory-rule-disposition

## 1. Derivation + measurement

- [x] 1.1 Re-derive the 9-directory gap map at HEAD (inverted-index
      form); record the map in the PR body (design table is context
      only).
- [x] 1.2 Measurement table: each addition-candidate gap suite run
      once, PR-lane conditions → (collected, passed, failed, errors,
      skipped, wall) rows in the PR body; `fn-gated` requires
      `passed == failed == errors == 0` AND `skipped == collected`
      (an ERRORS suite is a broken gap, not a gated one); runs may be
      time-capped and recorded as "> N min → runtime-budget";
      `redirect`/`edge-consumer` routings need no wall number.

## 2. Disposition (rules + exclusions)

- [x] 2.1 Rule additions per design decisions 3-4: narrow per-module
      rules (or directory-list extension where uniform), every added
      target existing + file-level non-gated; `scheduler.py` gaps
      extend the existing allowlisted entry; shared module-level
      tuples (`FILE_JOURNAL_READ_STATE_TESTS`,
      `ORCHESTRATOR_MANIFEST_SURFACE_TESTS`) are NEVER edited —
      extension happens at the rule site as
      `(*SHARED_CONST, "tests/new.py")`; no new unallowlisted
      duplicate pattern; order-audit vs `stop_on_match` rules
      recorded.
- [x] 2.2 `INTENTIONAL_RULE_GAP_EXCLUSIONS` table with tokens
      {fn-gated, redirect, edge-consumer, runtime-budget}; block
      comments carry rationale (+ measured number for
      runtime-budget).
- [x] 2.3 Every one of the derived pairs dispositioned (guard-green
      is the proof); #1452 audit routings honored (redirect targets,
      edge consumers, production_closure lane family single-suite
      closure per measurement).
- [x] 2.4 Positive-selection floor (design decision 4b): explicit
      `select_tests` pins for the audit's confirmed same-name direct
      gaps (tile_publisher, output_parser cli/parser, shud_runtime
      warm-start pair, slurm_gateway app/gateway, orchestrator
      same-name family) — an all-exclusions delivery fails these
      pins.

## 3. Guard

- [x] 3.1 Disposition guard (inverted index, < 5 s): domain = every
      tracked module under the 9 directory paths (stop-rule-owned
      modules included); "selected" judged at node level (node-id-only
      selection counts as a gap); every gap pair selected XOR
      excluded; stale exclusion (selected or vanished) reds; invalid
      token reds; `edge-consumer` entries machine-checked as selected
      by a rule whose pattern does not match the module; `redirect`
      entries machine-checked as reached via `::` node ids in the
      module's own selection; anti-vacuity anchored on the
      pre-subtraction gap universe (nonzero pairs before exclusion
      subtraction + all 9 directories contribute modules).
- [x] 3.2 Guard red evidence via injectable selection/exclusion
      seams — constructed rule-list/table inputs (parameter seams;
      zero tracked mutation).
- [x] 3.3 Order-audit pins: narrow-rule targets provably present in
      `select_tests([module])` for modules coexisting with stop
      rules.
- [x] 3.4 Domain note vs the PR #1486 closure guard recorded in the
      guard comment (direct importers here; one-hop only for
      GUARDED_MODULE_CLOSURES).

## 4. Spec delta

- [x] 4.1 ADDED requirement (disposition guard, first-line SHALL,
      3 scenarios) + MODIFIED closure requirement (directory-rule
      sentence appended, byte-faithful otherwise).

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q tests/test_select_ci_tests.py` green
      (counts before/after; guard wall-clock reported).
- [x] 5.2 Red evidence per task 3.2 (honest labels for
      naturally-green pins).
- [x] 5.3 `git ls-files '*.py' | xargs uv run ruff check` clean.
- [x] 5.4 `openspec validate ci-selector-directory-rule-disposition
      --strict --no-interactive` passes.
- [x] 5.5 Whole-tree selection diff vs master: delta = exactly the
      modules with grown rules (list in PR body).
- [x] 5.6 Runtime accounting per design decision 6 (heaviest module
      classes before/after counts + wall, plus one multi-file union
      measurement; nothing beyond ~5 min local per single module
      without a runtime-budget routing).
- [x] 5.7 Issue #1455 Verification commands green.
