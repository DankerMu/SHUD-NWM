## 0. Dependency gate

- [ ] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks.

## 1. Inventory and classification

- [ ] 1.1 Create a persistent legacy path inventory covering `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, `services/tile-publisher`, QHH diagnostic scripts, mocked e2e specs, and paused CI jobs.
- [ ] 1.2 For each inventory row, record exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, final action, and verification command.

## 2. Placeholder cleanup

- [ ] 2.1 Remove or archive `apps/web` after updating docs that mention it.
- [ ] 2.2 Remove or archive hyphenated worker placeholder directories after proving canonical underscore packages are the only active entrypoints.
- [ ] 2.3 Decide `services/tile-publisher` status and either archive it or document its active role.
- [ ] 2.4 Treat `workers/sbatch_templates` separately: add an archive/legacy manifest before any deletion.

## 3. Diagnostic isolation

- [ ] 3.1 Add `scripts/diagnostic/qhh/README.md` or an equivalent diagnostic manifest listing QHH diagnostic scripts and production replacement commands.
- [ ] 3.2 Keep or strengthen `tests/test_qhh_scripts_static.py`.
- [ ] 3.3 If scripts are moved, add temporary wrappers and update runbooks in the same PR.

## 4. E2E evidence split

- [ ] 4.1 Rename mocked Playwright specs or add config grouping so API-mocked specs are visibly `mocked-regression`.
- [ ] 4.2 Add a live display-readonly e2e profile and npm/pnpm script that uses explicit `BASE_URL`/`API_BASE_URL` and forbids broad `page.route('**/api/v1/**')` mocks.
- [ ] 4.3 Add a static guard that fails if files classified as live e2e contain broad API route mocks.
- [ ] 4.4 If live runtime is unavailable, record runtime execution as `BLOCKED` while still landing config/script/static guard.
- [ ] 4.5 Update `docs/VALIDATION.md` and `docs/bugs.md` so mocked regression cannot be cited as live receipt.

## 5. Paused CI cleanup

- [ ] 5.1 Replace the `frontend-m15-visual` `&& false` job with archived documentation or a manual workflow.
- [ ] 5.2 Verify workflow files no longer contain indefinite `&& false` disabled jobs.
