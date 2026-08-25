## 1. Selector Routing

- [x] 1.1 Derive a one-hop reverse index of module-scope suite-to-suite imports from the supplied `repo_root`, using only standard-library parsing and excluding file-level `integration`/`e2e` suites; build it lazily and reuse unchanged per-file derivations through strong filesystem identity.
- [x] 1.2 Extend only the ordinary changed-suite self-selection branch with its direct importer set; preserve redirects, support-module routing, owner self-selection, meta-guard accumulation, and `meta_guard_only` output semantics.

## 2. Requirement-Driven Tests

- [x] 2.1 Prove `tests/test_real_slurm_gateway.py` selects every current non-gated module-scope importer and `tests/test_production_scheduler.py` selects its five current importers without freezing the complete live-tree edge list.
- [x] 2.2 Add a mechanically derived live-tree guard covering every ordinary owner/importer edge and a synthetic repository-tree test in which adding a new module-scope edge changes the required selection.
- [x] 2.3 Pin all three supported module-scope forms (`import tests.test_owner`, `from tests.test_owner import helper`, and `from tests import test_owner`), one-hop-only behavior, function-local import exclusion, file-level gating exclusion, and nested suite paths.
- [x] 2.4 Pin malformed Python in a discovered suite as a loud selector failure when closure selection is required, while unrelated path classes avoid the scan.
- [x] 2.5 Pin redirect compatibility and confirm a changed suite still emits a non-collapsed GitHub selection (`meta_guard_only=false`) containing its owner and selector meta-guard.
- [x] 2.6 Update pre-existing exact-set/count expectations only where the new derived importer closure intentionally expands them; no other compatibility expectation changed.

## 3. Evidence Floor

- [x] 3.1 Red proof: the new-behavior batch against pre-change selector source produced 13 intended failures and 5 compatibility passes; the final implementation is green and no `red-proof` stash remains.
- [x] 3.2 `uv run pytest -q tests/test_select_ci_tests.py`: 342 passed in 200.50 seconds on the rebased final Phase 2 head.
- [x] 3.3 `uv run ruff check .`: all checks passed in a clean tracked-file projection; `git ls-files '*.py' -z | xargs -0 uv run ruff check` also passed in the worktree. The literal worktree command additionally sees protected startup-time untracked `skills/subagent-workflow/scripts/review_gate.py` and reports its pre-existing E501; that local-only file is outside this PR and was not modified.
- [x] 3.4 `openspec validate ci-selector-suite-importer-closure --strict --no-interactive`: valid on the final Phase 2 head.
- [x] 3.5 Compared 3,245 tracked/synthetic paths against rebased `origin/master`: exactly 17 ordinary changed suites gained their mechanically derived importers; zero targets were removed and no unrelated, support-module, or active redirect selection changed.
