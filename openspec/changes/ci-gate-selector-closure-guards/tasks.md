# Tasks: ci-gate-selector-closure-guards

Fixture level: expanded · Repair intensity: medium
Issues: #1447 #1283 #1254 #1362 #1372 (one PR)

## Risk packs (considered)

- Public API / CLI / script entry: **selected** — `select_ci_tests.py` is the
  CI selection entry consumed by `ci.yml`; behavior changes gate every PR.
- Config / project setup: **selected** — `.github/workflows/ci.yml` filter
  edit (#1362); must be provably behavior-neutral.
- Error handling / rollback / partial outputs: **selected** — missing-target
  drop/warning path interacts with the new meta-guard target in tmp-root
  tests.
- Legacy compatibility / examples: **selected** — existing selector
  assertions (redirects, fallbacks) are the compatibility surface; updates
  to exact-equality expectations must be intentional and enumerated.
- File IO / path safety / overwrite: not selected — test-only tree reads via
  `git ls-files`/AST on tracked files; no writes.
- Schema / columns / units / field names: not selected — no data schema.
- Auth / permissions / secrets: not selected — none touched.
- Concurrency / shared state / ordering: not selected — selector is pure.
- Resource limits / large input / discovery: not selected — bounded tracked
  tree walk, same shape as existing #1247 guard.
- Release / packaging / dependency compatibility: not selected — no deps.
- Documentation / migration notes: not selected — code comments carry the
  ruling records; no docs pages owed.
- Domain packs (geospatial, forcing, SHUD numerical, PostGIS, Slurm
  lifecycle, providers, manifest/QC, display identity): not selected — no
  production module logic changes; #1372 is comment prose in `runtime.py`.

## Implementation tasks

- [x] 1. `select_ci_tests.py`: add
  `PathTestRule("packages/common/display_coverage.py", (refresh, parallel,
  forecast_api))` with a comment recording the deliberate exclusion of the
  single `integration`-marked importer suite existing on master
  (`tests/test_display_coverage_residual_debt_integration.py`) and the
  accurate fallback history (pre-rule: CORE_SMOKE fallback; no
  `packages/common/**` broad rule exists). The comment must name ONLY files
  present in the tracked tree — do not mention #1443-branch-only files.
  (#1447)
- [x] 2. `select_ci_tests.py`: extend the `services/slurm_gateway/**` rule
  tests tuple with `tests/test_real_slurm_gateway.py`,
  `tests/test_slurm_array_contract.py`, `tests/test_job_array.py`. (#1283)
- [x] 3. `select_ci_tests.py`: in the changed-test branch, additionally
  select `tests/test_select_ci_tests.py` for every changed file matching
  `tests/test_*.py` — and ONLY that shape: `tests/conftest.py`,
  `tests/integration_helpers.py`, and other non-`test_*` files under
  `tests/` keep today's behavior exactly — while preserving self-selection
  and redirect semantics. (#1254)
- [x] 4. `ci.yml`: delete the `- 'tests/test_worker_chain_smoke.py'` line
  from the `database` filter; change nothing else in the file. (#1362)
- [x] 5. `workers/shud_runtime/runtime.py` ~line 973: reword the comment to
  drop the literal `create_qhh_shud_manifest` / script-path token while
  keeping the diagnostic-lane-fallback explanation. Comment-only diff.
  (#1372)
- [x] 6. `tests/test_select_ci_tests.py`: traversal importer-closure guard
  for `packages.common.display_coverage` and
  `services.slurm_gateway.real_backend` (shared helper; non-gated top-level
  importers must be selected by the owning rule; guard reddens if a rule
  entry is removed or a new importer suite appears). (#1447/#1283)
- [x] 7. `tests/test_select_ci_tests.py`: meta-guard selection tests — a
  standalone changed `tests/test_*.py` selects itself plus
  `tests/test_select_ci_tests.py`; redirect scenarios keep redirect targets
  plus meta-guard; update existing exact-equality assertions accordingly,
  each update enumerated in the implementer report. (#1254)

## Required evidence (verification commands)

- [x] `uv run pytest -q tests/test_select_ci_tests.py` — all green
  (includes new guards). (#1447 #1283 #1254)
- [x] `uv run python -c "...select_tests(['packages/common/display_coverage.py'])"`
  → includes `tests/test_display_coverage_parallel.py` and
  `tests/test_forecast_api.py`. (#1447)
- [x] `printf 'services/slurm_gateway/real_backend.py\n' | uv run python
  scripts/select_ci_tests.py` → includes all three added suites. (#1283)
- [x] `printf 'tests/test_node27_timeseries_compression_capture.py\n' | uv
  run python scripts/select_ci_tests.py` → outputs the file itself AND
  `tests/test_select_ci_tests.py`. (#1254)
- [x] `uv run pytest -q tests/test_real_slurm_gateway.py
  tests/test_slurm_array_contract.py tests/test_job_array.py` — green
  (~310-case baseline). (#1283)
- [x] `uv run pytest -q tests/test_display_coverage_parallel.py
  tests/test_forecast_api.py` — green (141-case baseline). (#1447)
- [x] `grep -rn "test_worker_chain_smoke" .github/` — empty; remaining
  `database` patterns each expand to ≥1 tracked file (audit list in PR
  body). (#1362)
- [x] `git diff -- .github/workflows/ci.yml` — exactly one deleted line
  (`- 'tests/test_worker_chain_smoke.py'`), zero added lines: remaining
  patterns verbatim untouched. (#1362)
- [x] Negative control for the traversal guards (run once, then revert):
  (a) temporarily remove `tests/test_real_slurm_gateway.py` from the
  `services/slurm_gateway/**` rule tuple; (b) drop a scratch non-gated
  `tests/test_*.py` with a top-level
  `from packages.common import display_coverage` import AND make it visible
  to the tracked-tree traversal with `git add -N tests/test_<scratch>.py`
  (the guard helpers derive from `git ls-files`; an untracked scratch file
  is invisible and the suite stays green — a green run on this step is
  itself a guard defect, not an accepted outcome). Run
  `uv run pytest -q tests/test_select_ci_tests.py` → the guard FAILS in
  each case, naming (a) the removed suite and (b) the scratch file as an
  uncovered `packages.common.display_coverage` importer. Restore
  (`git rm --cached` + delete the scratch file; revert the rule tuple),
  rerun → all green. Paste both red outputs in the implementer report.
  (#1447/#1283 acceptance 3)
- [x] `printf 'tests/conftest.py\n' | uv run python scripts/select_ci_tests.py`
  → outputs exactly `tests/conftest.py` (no meta-guard: accumulation is
  scoped to `tests/test_*.py`). (#1254 boundary)
- [x] `uv run pytest -q tests/test_entropy_audit_script.py` — all green;
  hard gate `pass`, failing count 0. (#1372)
- [x] `git diff -- workers/shud_runtime/runtime.py` — comment-only. (#1372)
- [x] `git diff --stat` — contains NO changes to
  `packages/common/display_coverage.py` or
  `services/slurm_gateway/real_backend.py`. (#1447/#1283 boundary)
- [x] `uv run ruff check .` — clean.
- [x] Sibling audit: for each other `services/**`/`workers/**` directory
  `PathTestRule`, list non-gated top-level importer suites not selected;
  each entry either fixed here (only if trivially same-class) or recorded
  with a deliberate-exclusion reason in the PR body. (#1283 acceptance)

## Non-goals (recorded)

- No production-logic change anywhere; no ci.yml trigger changes; no
  `scripts/**/*.sh` filter work (#1138); no empty-selection policy change
  (#1182); no wholesale mechanized guard for every directory rule.
