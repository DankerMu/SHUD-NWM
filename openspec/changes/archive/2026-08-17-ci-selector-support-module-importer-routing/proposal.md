# Proposal: ci-selector-support-module-importer-routing

## Why

Issue #1487 (PR #1486 round-1 CONFIRMED-but-DEFERRED, routed per its
fixture's recorded deferral). Since PR #1486, a PR touching only a
`tests/` support module (fixtures / helpers / fakes) maps to the
selector meta-guard suite plus the full-tree collect-only smoke —
informative but **non-blocking at assertion level**: the smoke checks
import/syntax only. FOUR support modules have real non-gated top-level
importer suites (re-derived at 538c02ce under the repo's derivation
authority — the issue's three plus `tests/__init__.py`, whose 3
importers the issue's no-aliasing derivation missed):
`tests/fixtures/mapping_builder/in_memory_grid_snapshot.py` → 5
mapping_builder suites; `tests/slurm_template_helpers.py` →
`test_production_slurm_validation` + `test_slurm_array_contract`;
`tests/river_identity_backfill_fakes.py` → the two
`test_node27_river_identity_backfill*` suites; `tests/__init__.py` →
`test_integration_gate` + the two node27_timeseries_compression
suites. The issue's nine suites collect 549 tests in 0.74 s —
cost of selecting them is negligible,
and their absence is exactly the #1191→#1247→#1283→#1447 rot shape
(assertion-level regressions ride a green PR lane into master).
History has zero helper-only PRs, so this is preventive hardening
(the issue records the revisit trigger), delivered now because the
mapping and guard are cheap and the pattern (mechanically derived
closure) is already in the tree.

## What Changes

One PR closing #1487:

- **Support-module routing**: a new `SUPPORT_MODULE_TEST_RULES` tuple
  in `scripts/select_ci_tests.py`, consulted in the changed-test
  branch for non-suite `tests/` paths BEFORE the meta-guard fallback:
  exact-path entries for the FOUR importer-bearing support modules
  (the issue's three plus `tests/__init__.py`, which the fixture
  review's re-derivation under the repo's package-aliasing authority
  showed carries 3 importer suites — the issue's 0 was derived under
  a no-aliasing scheme the repo already rejected) mapping to their
  importer suites plus the selector meta-guard suite. A matched
  support module selects real suites (assertion-level blocking
  restored); unmatched support modules keep the meta-guard +
  collect-only route unchanged.
- **Mechanically derived closure guard** in
  `tests/test_select_ci_tests.py`: for every tracked non-suite
  `tests/` module, the derived non-gated top-level importer set
  (semantic authority: the existing
  `_non_gated_top_level_importer_tests` helper, executed via the
  inverted index — never a frozen list) must be ⊆
  `select_tests([module])`, EXCEPT modules on an explicit carve-out
  allowlist (`tests/integration_helpers.py`, `tests/conftest.py` —
  an issue-#1487 scope boundary with PARTIAL external coverage via
  ci.yml's `database` filter → `real-db-integration -m integration`,
  measured 75/245 and 0/19 — recorded honestly, not claimed as full
  compensation); allowlist entries are pinned inside ci.yml's
  `database:` filter block so the cited predicate cannot rot
  silently. Modules with zero derived importers must keep selecting
  exactly the meta-guard suite. A new importer suite for a routed
  support module reds the guard naming the missing suite.
- **Spec delta** (`ci-contract-baseline`): ADDED requirement for the
  support-module routing + guard; TWO MODIFIED requirements —
  "Empty targeted-test selection MUST be loudly self-identifying"
  (support-module clause scoped to modules without derived importers,
  descriptive cross-reference) and "Changed-test PRs MUST run the
  selector meta-guards" (routed support modules map to importer
  suites + meta-guard instead of exactly the meta-guard; scenario
  rescoped) — byte-faithful otherwise.
- **Instruction docs**: the third-mode (collapse) line in
  `instructions/agents/shared.md` + generated `CLAUDE.md`/`AGENTS.md`
  updated so "只改 tests/ 支持模块" no longer universally implies the
  collapse route (three carriers stay byte-identical).

Non-goals: no change to `meta_guard_only` semantics or the
collect-only smoke (#1454); no change for
`tests/integration_helpers.py` / `tests/conftest.py` (issue-scope
carve-out with recorded partial external coverage — full routing is
a candidate follow-up, not this PR); no production-directory rules
(#1455 delivered those); no runtime derivation inside CI (the issue's
备选 is rejected — selector output stays statically predictable from
`select_tests()`).

## Capabilities

- `ci-contract-baseline`: ADDED "Support-module changes MUST select
  their non-gated importer suites"; MODIFIED "Empty targeted-test
  selection MUST be loudly self-identifying" (clause scoping only);
  MODIFIED "Changed-test PRs MUST run the selector meta-guards"
  (routed-support-module rescope only).

## Impact

- `scripts/select_ci_tests.py` (new rules tuple + one branch hook),
  `tests/test_select_ci_tests.py` (closure guard + allowlist + pins),
  `openspec/specs/ci-contract-baseline/spec.md` (delta),
  `instructions/agents/shared.md` + `CLAUDE.md` + `AGENTS.md` (one
  line, byte-identical across carriers).
- Closes #1487. Verification per its `Verification:` field:
  `uv run pytest -q tests/test_select_ci_tests.py`,
  `uv run ruff check .` (tracked form:
  `git ls-files '*.py' | xargs uv run ruff check`).
