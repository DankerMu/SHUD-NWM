## 1. Selector implementation

- [x] 1.1 Introduce one five-prefix backend Python source authority and make `_is_backend_python_path` plus the renamed/generalized same-name helper consume it.
- [x] 1.2 Preserve the existing caller's existence gate, explicit-rule set union, `matched` semantics, missing-target filtering, and core-smoke fallback for sources without a same-name suite.

## 2. Mechanized regression guards

- [x] 2.1 Explicitly rewrite the scripts-only scope test to prove a non-`scripts/` source such as `packages/common/state_qc.py` now selects its same-name suite while a source without a suite still follows its existing route.
- [x] 2.2 Generalize the tracked pair helper and completeness guard across `apps/api/`, `packages/`, `services/`, `workers/`, and `scripts/`, deriving pairs from `git ls-files` rather than a frozen list.
- [x] 2.3 Make the completeness guard distinguish unknown-fallback leakage from targets independently selected by explicit rules, especially `apps/api/**` selecting `tests/test_api.py`; directly pin `apps/api/runtime_mode.py` to the exact explicit-rule plus same-name union.
- [x] 2.4 Add a tree-derived cross-prefix stem-collision guard that requires the shared suite to import every colliding source module; current `best_available` passes without a name-specific exception.
- [x] 2.5 Preserve existing guards for scripts mappings, explicit differently named rules, missing same-name suites, changed-test/meta-guard behavior, guarded-module closure, and node-id targets.

## 3. Evidence floor

- [x] 3.1 Batched red proof: with new tests retained and production routing reverted, selector suite reported 2 failed / 173 passed (positive-scope pin plus 15-pair completeness guard). A separate caller mutant (`selected.add` -> replacement set) made the explicit-plus-derived union oracle fail. Restored source reported 176 passed; no `red-proof` stash remained.
- [x] 3.2 `uv run pytest -q tests/test_select_ci_tests.py` -> 176 passed.
- [x] 3.3 Selector CLI: `packages/common/storage.py` -> only `tests/test_storage.py`; `packages/common/auth_policy.py` -> the unchanged five-test core-smoke fallback.
- [x] 3.4 Final tracked census: 304 backend sources, 66 source/suite pairs, 65 unique suites, 0 misses, and one tree-derived two-source collision; old-vs-new comparison over all 2,938 tracked paths changed exactly the expected 15 selections.
- [x] 3.5 Full 14-suite gap set: 383 collected; timed run 365 passed / 18 skipped in 1.31 s pytest time (3.01 s wall). Thirteen core-smoke-only suites: 309 collected; 291 passed / 18 skipped in 0.85 s pytest time (2.13 s wall). Both are far below 35 minutes.
- [x] 3.6 `uv run ruff check scripts/select_ci_tests.py tests/test_select_ci_tests.py` -> clean; `openspec validate ci-selector-backend-same-name-routing --strict --no-interactive` -> valid.

## 4. Review boundary

- [x] 4.1 Diff audit confirms no changes to `PATH_TEST_RULES`, `CORE_SMOKE_TESTS`, `.github/workflows/ci.yml`, production/test assertion behavior, suite-to-suite routing (#1561), or non-same-name importer closure (#1455).
- [x] 4.2 Fixture review, Phase 2 repair, no-deviation statement, selected risk-pack evidence, and current-tree runtime numbers are recorded in `.workplans/1587/` and this checklist.
