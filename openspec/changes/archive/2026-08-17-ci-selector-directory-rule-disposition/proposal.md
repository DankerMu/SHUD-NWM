# Proposal: ci-selector-directory-rule-disposition

## Why

Second and final batch of issue #1455 (item (2); items (1)(3)(4)
shipped in PR #1486). The 9-directory rule-gap audit from PR #1452
lives only in that PR's body; PR #1486's fixture split it out after
re-derivation showed the workload would dominate that PR. Re-derived
at master d02b4edb with the merged guard helpers (inverted-index
form): **211 module→suite gap pairs, 74 unique non-gated top-level
importer suites** across the 9 audited directory rules — every one a
suite whose assertions do not run on a PR that changes a module it
imports. The heaviest is `services/orchestrator` (26 modules with
gaps, 82 pairs, 45 unique suites); the #1452 audit already ruled many
of those "deliberate redirect / edge consumer", but that ruling rots
in a PR body unless it becomes guard-visible data.

## What Changes

One PR closing #1455 (this is the issue's declared second batch):

- **Mechanical disposition policy** (design decision 2): every derived
  gap pair is either SELECTED by a rule or EXCLUDED by an explicit
  table entry carrying a reason token from
  {`fn-gated`, `redirect`, `edge-consumer`, `runtime-budget`}, where
  `fn-gated` means gating invisible to the file-level marker filter
  (function-level markers / env opt-ins) with the suite executing
  zero assertions in the PR lane — the PR #1486 fixture-review P2-6
  correction, pre-recorded there and applied here.
- **Rule additions**: narrow per-module rules following the
  `real_backend.py` precedent for modules with material gaps;
  directory-rule list extension only where precision loss is nil
  (small directories). Placement respects `stop_on_match` ordering
  (stop rules at indices 0-13 make later rules unreachable for their
  paths — additions for those modules go BEFORE the stop rule or
  extend it AT THE RULE SITE). Shared-tuple hazard (fixture-review
  F1): several rules share module-level test tuples
  (`FILE_JOURNAL_READ_STATE_TESTS` — also used by
  `packages/common/safe_fs.py` OUTSIDE the nine directories —
  `ORCHESTRATOR_MANIFEST_SURFACE_TESTS`); extending a rule means
  `(*SHARED_CONST, "tests/new.py")` at that rule's site, NEVER
  editing the shared constant.
- **Positive-selection floor** (fixture-review F2 — an
  all-exclusions delivery must be impossible): explicit
  `select_tests` assertions pin the audit's confirmed same-name
  direct gaps as ADDED (tile_publisher→cli_publish_qdown,
  output_parser cli/parser suites, shud_runtime warm-start pair,
  slurm_gateway app/gateway suites, the orchestrator same-name
  family: persistence/retry/reconcile/scheduler_generation/
  scheduler_timing/replay_lineage/retention/run_tree_copyback);
  `edge-consumer` entries are machine-checked (the suite must be
  selected by some other rule, else the pair is an orphan, not a
  routing).
- **Guard** (spec ADDED requirement deferred from PR #1486's change):
  a selector-suite test derives the gap set over the 9 directories
  (inverted-index form, the naive per-module walk measured >2 min vs
  1.3 s) and fails on any gap pair that is neither selected nor
  excluded, any stale exclusion (pair no longer derives as a gap),
  and any invalid reason token.
- Disposition decided by MEASUREMENT, not by the #1452 table's
  integers: addition-candidate suites run once in PR-lane conditions
  recording (collected, passed, failed, errors, skipped, wall);
  `fn-gated` requires `passed == failed == errors == 0` AND
  `skipped == collected` (a suite that ERRORS is a broken gap, not a
  gated one); runs may be time-capped and recorded as
  "> N min → runtime-budget"; `redirect`/`edge-consumer` routings
  need no wall number. Fixture-review measurement preview: the
  e2e-named family largely EXECUTES real assertions
  (`test_two_node_e2e_evidence.py` 844 passed / 137.9 s local) — the
  fn-gated bucket is expected near-empty; the real mass falls on
  additions, `redirect`, and `runtime-budget`.

Non-goals: no change to any production module or test-suite content;
no selection-semantics change for input classes outside the 9
directories; no revisiting of PR #1486's surfaces (support-file
mapping, meta_guard_only, one-hop closure, duplicate guard, marker
anchor); no `tests/` helper mapping (#1487); no `scripts/**/*.sh`
gating (#1138); no route-A/B policy change.

## Capabilities

- `ci-contract-baseline`: ADDED requirement "Directory-rule importer
  gaps MUST be dispositioned as selections or reasoned exclusions";
  MODIFIED requirement "Guarded-module selector rules MUST cover
  their non-gated importer closure" gains the directory-rule
  disposition sentence deferred from PR #1486 (byte-faithful
  otherwise).

## Impact

- `scripts/select_ci_tests.py` (rule table growth only),
  `tests/test_select_ci_tests.py` (disposition guard + exclusion
  table), `openspec/specs/ci-contract-baseline/spec.md` (delta).
- Closes #1455. Verification per its `Verification:` field:
  `uv run pytest -q tests/test_select_ci_tests.py`,
  `uv run ruff check .`,
  `printf 'services/slurm_gateway/real_backend.py\n' | uv run python scripts/select_ci_tests.py`.
