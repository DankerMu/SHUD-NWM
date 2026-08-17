# Tasks: ci-selector-followup-hardening

## 1. #1453 — support-file selection (scripts/select_ci_tests.py)

- [x] 1.1 Changed-test branch: `tests/**.py` not matching
      `tests/test_*.py` no longer self-selects; maps to
      `SELECTOR_META_GUARD_TEST` (redirect rules, if ever matching,
      still win via `matched_changed_test`). Comment states the
      exit-5 rationale.
- [x] 1.2 Rewrite pin
      `test_meta_guard_accumulation_is_scoped_to_test_file_names`
      (:761-767): non-spill intent preserved, defect behavior pin
      replaced by `== [SELECTOR_META_GUARD_TEST]`.
- [x] 1.3 New tree-derived invariant test: every tracked
      `tests/**.py` not matching `tests/test_*.py` (derived, not
      hardcoded; non-empty; 8 files at HEAD per design) selects
      exactly `[SELECTOR_META_GUARD_TEST]`.

## 2. #1454 — meta_guard_only collapse (selector CLI + ci.yml)

- [x] 2.1 `main`/`_write_github_output`: emit
      `meta_guard_only=true|false` computed on the final (post-filter)
      list; existing fields/stdout unchanged.
- [x] 2.2 Tests: tmp-root deleted `tests/test_*.py` scenario →
      selection `[meta-guard]`, output field true; ≥2-target and
      empty selections → false; the selector-development classes
      (`scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`)
      pinned as true with a comment recording the accepted
      shape-not-provenance semantics (design decision 2).
- [x] 2.3 ci.yml `unit-test-targeted` run step: when count != 0 AND
      `meta_guard_only == 'true'`, ALSO run the labeled collect-only
      smoke after the targeted run. Wording constraint: this branch
      must NOT claim "0 assertions were executed" (the targeted run
      did execute) — adapted warning/step-summary says the selection
      collapsed to the selector meta-guard and full-tree collect-only
      ran in addition; redirect-not-pipe; collection failure fails
      the step; `count == 0` branch byte-identical.
- [x] 2.4 String-coupling pin: selector suite asserts ci.yml's
      targeted job consumes the literal `meta_guard_only`.
- [x] 2.5 Route-C pins comment (:812-838) updated: deleted-test-file
      class no longer a lost-smoke case; wording matches final
      semantics.

## 3. #1455(1) — one-hop importer closure

- [x] 3.1 `_one_hop_importer_modules` (non-recursive; domain =
      tracked `.py` outside `tests/**`; top-level imports only) +
      guard extension: owning rule must cover direct ∪ one-hop
      non-gated importer suites for each `GUARDED_MODULE_CLOSURES`
      module; comment records the forward-looking hop-bound rationale
      (design decision 4 corrected numbers — no integer from the
      issue or design is an assertion).
- [x] 3.2 Extend the owning rule(s) so the derived `real_backend`
      one-hop set (re-derived at HEAD; anti-vacuity member
      `tests/test_reconcile_sacct_parse.py`) is fully selected;
      derivation command + output pasted into PR body.
- [x] 3.3 `display_coverage` one-hop set derived and covered (or
      derivation shows it adds nothing — record either way).
- [x] 3.4 Function-body exclusion pin :692-700 stays green
      unmodified (top-level-edges-only composition, design
      decision 4).

## 4. #1455(3) — duplicate-pattern guard

- [x] 4.1 Guard function parameterized over a rule list + test on
      real `PATH_TEST_RULES` with
      `INTENTIONAL_DUPLICATE_PATTERNS = {"services/orchestrator/scheduler.py"}`;
      allowlist anti-rot both directions (every duplicate allowlisted,
      every allowlist member actually duplicated);
      `CHANGED_TEST_FILE_RULES` exempt.
- [x] 4.2 #1443 simulation test: constructed list with a second
      `packages/common/display_coverage.py` entry → guard flags it
      by name.

## 5. #1455(4) — gating-marker anchor

- [x] 5.1 AST-derive conftest auto-skip marker set from the
      `"<marker>" in item.keywords` shape (conftest :83-88), source
      passed as a parameter (path or text), loud failure on empty;
      binding assertion is the EQUALITY
      `derived == GATING_MARKER_NAMES | {"grib"}` plus
      `{"real_disk", "timescaledb_210"} ∩ derived == ∅`;
      grib-absence rationale in comment.

## R1. Round-1 verified findings (fix pass @ c9152264)

- [x] R1.1 (P2, three lenses) Nested `tests/<dir>/test_*.py`
      misclassified as support module and the new invariant test
      cements the loss: classification at :477 AND the support-module
      derivation (:964) switch to a BASENAME predicate
      (`fnmatch(name, "test_*.py")`); keep ONE shared predicate so
      the meta-guard accumulation (:501) widens consistently
      (verifier-ruled consistent with #1254 intent); do NOT touch
      `_tracked_top_level_test_files` (:700 — it feeds the
      importer-closure domain, a different surface). Verified: zero
      classification delta on all 196 tracked `tests/**.py` today.
- [x] R1.2 (Note) `_dotted_module_name` strips a trailing
      `.__init__` so re-exporting packages contribute their real
      dotted name to one-hop derivation (zero delta today, closes a
      silent-∅ channel).
- [x] R1.3 (P2) `meta_guard_only` discrimination boundary pinned:
      add `("db/schema.sql", "1")` to the suppression parametrize
      (kills the `len(tests)==1` mutant; 15 single-target rules in
      today's table).
- [x] R1.4 (P2) ci.yml coupling pin de-hollowed: slice the collapse
      block (`if [ "…meta_guard_only }}"` → matching top-level
      `fi`), assert comparator is `= "true"` and
      `pytest tests/ -q --collect-only` sits INSIDE the block, with
      a readable "collapse branch not found" message on the marker
      locate (kills condition-flip, dead-reference, and neutered-
      smoke mutants; wording refactors stay green).
- [x] R1.5 (Note) `assert "0 assertions" not in collapse_block` —
      the spec's MUST-NOT wording constraint gets its pin.
- [x] R1.6 (P2) `all(rule.only_when_any_changed …)` at :1088
      narrowed to DUPLICATED patterns only (open PR #1443 adds a
      legitimate unconditional non-duplicate rule and would false-red
      with a duplicates-pointing message); add the real-hazard red
      case (injected duplicate WITHOUT `only_when_any_changed` →
      False) to keep the narrowed assert killable.
- [x] R1.7 (P2) Operator-contract docs: the Unit Tests third mode
      (meta-guard collapse → targeted + smoke, no 0-assertion claim)
      added to `instructions/agents/shared.md` CI section and applied
      VERBATIM-identically to CLAUDE.md and AGENTS.md (three files,
      1-2 sentences each, precedent: archive
      2026-08-10-ci-empty-selection-signal-legibility task 1.3);
      MUST ride in the same commit as the code fixes (CI cost
      discipline — no trailing docs commit).
- [x] R1.8 (ride-along) "five uncompensated support files" corrected
      to SIX in proposal.md + design.md (8 files − 2 database-filter
      compensated).
- [x] R1.9 (closure P2, orchestrator in-place) spec delta + design
      decision-1 wording moved to the basename-shaped predicate
      (four spec places + design :89/:102) with a new nested-suite
      scenario; tasks 1.1/1.3 above retain their original path-shaped
      phrasing as HISTORY of the pre-R1.1 shape — R1.1 supersedes
      them, this line is the pointer. Closure Note (b) recorded: the
      class-boundary assert at tests/test_select_ci_tests.py:1013 is
      tautological by construction (derivation already filters
      basename matches); harmless, kept — the falsifiable boundary
      pin is the nested-suite test.
- DEFER (recorded): helper→importer gate-strength challenge
  (CONFIRMED; history has zero helper-only PRs, fixture rung-1
  records the conditional trade and its revisit trigger has not
  fired) — routed to a NEW tracked issue (not #1455(2), which owns a
  different surface).

## 6. Spec delta

- [x] 6.1 `specs/ci-contract-baseline/spec.md`: MODIFIED ×3
      (empty-selection legibility with honest collapse-flag
      semantics, guarded-module closure one-hop, changed-test
      meta-guards support-file mapping — byte-faithful outside the
      refinements) + ADDED ×2 (duplicate-pattern allowlist,
      gating-marker anchor). Directory-rule disposition delta
      deferred to the follow-up change.

## 7. Evidence Floor

- [x] 7.1 `uv run pytest -q tests/test_select_ci_tests.py` green
      (report counts before/after).
- [x] 7.2 Red evidence: each new guard/pin red against the unmodified
      selector via parameter seams or in-memory/out-of-tree copies
      only (honest labels for naturally-green pins).
- [x] 7.3 `uv run ruff check .` per issue Verification fields
      (tracked-tree form).
- [x] 7.4 `openspec validate ci-selector-followup-hardening --strict
      --no-interactive` passes.
- [x] 7.5 CLI spot checks:
      `printf 'services/slurm_gateway/real_backend.py\n' | uv run python scripts/select_ci_tests.py`
      includes the derived one-hop set;
      `echo tests/conftest.py | …` returns the meta-guard suite;
      deleted-file scenario shows `meta_guard_only=true` via
      `--github-output`.
- [x] 7.6 Runtime accounting in PR body: selected-set size
      before/after for the touched rule(s) + wall-clock of the
      newly-required suites (design: ~20 s / 754 passed measured at
      fixture time; re-measure at HEAD).
- [x] 7.7 Diff inspection: `count == 0` ci.yml branch byte-identical;
      no gated suite added to any rule; existing suite pins
      unmodified except sanctioned 1.2.
