## 0. Dependency gate

- [ ] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks.

## 1. Inventory and classification

- [ ] 1.1 Create a persistent legacy path inventory covering `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, `services/tile-publisher`, QHH diagnostic scripts, mocked e2e specs, and paused CI jobs.
- [ ] 1.2 For each inventory row, record exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, final action, and verification command.
- [ ] 1.3 For issue #362, keep the PR inventory-only: do not delete, move, rename, archive, or wrap any governed path.
- [ ] 1.4 Classify owner area with the four-role vocabulary from `docs/governance/ROLE_BOUNDARY.md`: `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`.
- [ ] 1.5 Record discovery commands and results precisely enough that #363-#366 can proceed without redoing broad discovery.
- [ ] 1.6 Required #362 evidence:
  - Input command:
    `rg -n --glob '!apps/frontend/node_modules/**' --glob '!apps/frontend/dist/**' --glob '!**/__pycache__/**' "apps/web|workers/(forcing-producer|shud-runtime|output-parser|flood-frequency|sbatch_templates)|services/tile-publisher|services/tile_publisher|infra/sbatch|SLURM_GATEWAY_TEMPLATE_DIR|template_dir|run_qhh_continuous|run_qhh_cycle|create_qhh_shud_manifest|frontend-m15-visual|&& false|page\\.route\\('.*api/v1" .`
    Expected output: references needed to classify the governed paths plus
    active counterparts such as `apps/frontend`, underscore worker packages,
    `infra/sbatch`, Slurm gateway template settings, and tile publisher/display
    implementation. Each relevant hit is reflected or summarized in the
    inventory evidence column.
  - Input command:
    `find apps workers services scripts .github/workflows -maxdepth 3 -type d | sort`
    Expected output: governed candidate directories and active counterpart
    directories are visible and reflected in the inventory.
  - Input command: `uv run ruff check .`
    Expected output: exit 0.
  - Input command:
    `npx --yes markdownlint-cli2 --config .markdownlint.yaml 'docs/**/*.md'`
    Expected output: exit 0.
- [ ] 1.7 #362 non-goals:
  - No placeholder deletion/archive.
  - No QHH script move/wrapper.
  - No Playwright spec rename or live e2e config.
  - No CI workflow behavior change.
  - No cleanup approval for #363-#366 beyond recorded evidence and proposed final action.

## 2. Placeholder cleanup (#363 follow-up, not #362)

- [ ] 2.1 Remove or archive `apps/web` after updating docs that mention it.
- [ ] 2.2 Remove or archive hyphenated worker placeholder directories after proving canonical underscore packages are the only active entrypoints.
- [ ] 2.3 Decide `services/tile-publisher` status and either archive it or document its active role.
- [ ] 2.4 Treat `workers/sbatch_templates` separately: add an archive/legacy manifest before any deletion.

## 3. Diagnostic isolation (#364 follow-up, not #362)

- [ ] 3.1 Add `scripts/diagnostic/qhh/README.md` or an equivalent diagnostic manifest listing QHH diagnostic scripts and production replacement commands.
- [ ] 3.2 Keep or strengthen `tests/test_qhh_scripts_static.py`.
- [ ] 3.3 If scripts are moved, add temporary wrappers and update runbooks in the same PR.

## 4. E2E evidence split (#365 follow-up, not #362)

- [ ] 4.1 Rename mocked Playwright specs or add config grouping so API-mocked specs are visibly `mocked-regression`.
- [ ] 4.2 Add a live display-readonly e2e profile and npm/pnpm script that uses explicit `BASE_URL`/`API_BASE_URL` and forbids broad `page.route('**/api/v1/**')` mocks.
- [ ] 4.3 Add a static guard that fails if files classified as live e2e contain broad API route mocks.
- [ ] 4.4 If live runtime is unavailable, record runtime execution as `BLOCKED` while still landing config/script/static guard.
- [ ] 4.5 Update `docs/VALIDATION.md` and `docs/bugs.md` so mocked regression cannot be cited as live receipt.

## 5. Paused CI cleanup (#366 follow-up, not #362)

- [ ] 5.1 Replace the `frontend-m15-visual` `&& false` job with archived documentation or a manual workflow.
- [ ] 5.2 Verify workflow files no longer contain indefinite `&& false` disabled jobs.
