# Design: ci-gate-selector-closure-guards

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS (openspec/project-profile.md)

## Change surface

- `scripts/select_ci_tests.py` — new `display_coverage` rule, extended
  `slurm_gateway` rule, changed-test branch meta-guard accumulation.
- `tests/test_select_ci_tests.py` — traversal importer-closure guards,
  meta-guard-selection tests, updated exact-equality expectations.
- `.github/workflows/ci.yml` — one deleted line in the `database` filter.
- `workers/shud_runtime/runtime.py` — comment prose only (line ~973).

## Must preserve

- All existing selector semantics pinned by `tests/test_select_ci_tests.py`:
  orchestrator manifest-surface redirects (stop_on_match focused nodes),
  file-journal redirects, same-name script derivation scoped to `scripts/`,
  CORE_SMOKE fallback for unknown backend paths, docs-only → empty
  selection, missing-target drop-with-warning.
- `database`/`backend`/`frontend` filter behavior in `ci.yml`: the deleted
  pattern never matched anything, so filter outputs are bit-identical.
- `workers/shud_runtime/runtime.py` executable behavior: diff must be
  comment-only (`git diff` shows no non-comment lines).
- Existing entropy-audit semantics: no allowlist additions, no checker
  changes — the fix is on the flagged text, not the gate.

## Key decisions

1. **Meta-guard accumulation, not conditional self-select suppression**
   (#1254): any changed `tests/test_*.py` additionally selects
   `tests/test_select_ci_tests.py`, uniformly — including when a redirect
   rule (orchestrator/file-journal) already matched. Exact-equality
   assertions in the existing suite are updated to include the meta-guard
   file; the redirect intent ("don't run whole slow suites") is untouched
   because the meta-guard suite costs ~2.5s. Rationale: conditional
   accumulation ("only when nothing matched") would leave the guards
   unselected exactly when a redirect-listed test file is edited.
2. **Traversal guards derive, never freeze** (#1447/#1283): a shared helper
   in the test file computes, per guarded module, the set of
   `tests/test_*.py` with a top-level import of that module and no
   `integration`/`e2e` marker, and asserts rule-selection superset. Modeled
   on the #1191 (`git ls-files` pair walk) and #1247 (AST closure) guards.
   Marker detection must be file-level (`pytestmark` or a module-level
   `pytest.mark` usage), not substring guesswork.
3. **Integration-marked display_coverage suite deliberately excluded**
   (#1447 ruling): on master exactly one importer suite is
   `integration`-gated — `tests/test_display_coverage_residual_debt_integration.py`
   (skips on CI); including it adds constant skips and no assertions. The
   exclusion is recorded in a comment next to the rule, which must name
   only files that exist in the tree. The issue also names
   `tests/test_river_ts_read_path_surrogate_keys_integration.py`, but that
   file exists only on unmerged PR #1443's branch (same deviation family as
   decision 5); when #1443 merges, the traversal guard's marker filter
   already excludes it mechanically.
4. **#1372 fix is prose rewording, not allowlist**: the comment keeps its
   engineering content (run-manifest `forcing.files` entries are the
   diagnostic-lane fallback) but names no diagnostic token; the alternative
   (comment-aware allowlist in the checker) widens the gate's blind spot and
   needs boundary tests — rejected per the issue's recommendation.
5. **display_coverage rule is created fresh on master** (#1447 deviation
   from issue wording): the issue was filed against unmerged PR #1443's
   branch where an incomplete rule + false comment exist. On master neither
   exists; this change creates the rule with an accurate comment. When
   #1443 merges, the textual conflict in `select_ci_tests.py` forces manual
   reconciliation, and the traversal guard mechanically requires its new
   `tests/test_river_ts_read_path_surrogate_keys.py` to be added.

## Seams under test

- `select_tests(changed_paths, repo_root)` — the selector's single public
  seam; every selector behavior asserts through it.
- Tracked-tree traversal helpers in `tests/test_select_ci_tests.py`
  (`git ls-files` + AST import walk) — reused/extended, not forked.
- `build_report(REPO_ROOT, mode="hard-gate")` via
  `tests/test_entropy_audit_script.py` — the #1372 oracle.

## Risk of regression to watch

- The #1254 accumulation fires ONLY for changed files matching
  `tests/test_*.py` (decided, not left open): the early-continue branch
  condition (`startswith("tests/") and endswith(".py")`) is wider — it also
  admits `tests/conftest.py` and `tests/integration_helpers.py` — and those
  non-`test_*` inputs must NOT gain the meta-guard (pinned by an evidence
  row: `tests/conftest.py` alone selects only itself). tmp-root behavior is
  mechanically decided by the existing global missing-target drop
  (`select_ci_tests.py:459`): under a tmp_path root without
  `tests/test_select_ci_tests.py` the meta-guard target is dropped with a
  warning — pin, don't re-litigate.
- Guard helpers must not import the guarded modules (pure text/AST reads),
  or collection cost and import side effects leak into the selector suite.
