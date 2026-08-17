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
