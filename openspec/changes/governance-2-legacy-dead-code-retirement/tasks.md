## 0. Dependency gate

- [x] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks. Evidence recorded 2026-06-09: issue #358 closed 2026-06-08 17:33:44Z via merged PR #375 (`fix(contract): reconcile generated frontend API types`, merged 2026-06-08 17:33:43Z); issue #359 closed 2026-06-08 18:21:30Z via merged PR #376 (`chore(tooling): run Makefile Python targets via uv`, merged 2026-06-08 18:21:28Z); parent baseline issue #353 closed 2026-06-08 18:22:19Z before #362 inventory completion.

## 1. Inventory and classification

- [x] 1.1 Create a persistent legacy path inventory covering `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, `services/tile-publisher`, QHH diagnostic scripts and direct helper dependencies, mocked e2e specs, and paused CI jobs. Evidence: `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`.
- [x] 1.2 For each inventory row, record exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, final action, and verification command. Evidence: governed inventory and active counterpart tables.
- [x] 1.3 For issue #362, keep the PR inventory-only: do not delete, move, rename, archive, or wrap any governed path. Evidence: inventory scope and follow-up ownership sections keep #363-#366 actions separate.
- [x] 1.4 Classify owner area with the four-role vocabulary from `docs/governance/ROLE_BOUNDARY.md`: `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`. Evidence: every inventory row uses the role-boundary vocabulary.
- [x] 1.5 Record discovery commands and results precisely enough that #363-#366 can proceed without redoing broad discovery. Evidence: discovery command register `D1`-`D10` and required discovery results.
- [x] 1.6 Required #362 evidence:
  - Input command:
    `rg -n --glob '!apps/frontend/node_modules/**' --glob '!apps/frontend/dist/**' --glob '!**/__pycache__/**' "apps/web|workers/(forcing-producer|shud-runtime|output-parser|flood-frequency|sbatch_templates)|services/tile-publisher|services/tile_publisher|infra/sbatch|SLURM_GATEWAY_TEMPLATE_DIR|template_dir|run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest|frontend-m15-visual|&& false|page\\.route\\('.*api/v1" .`
    Expected output: references needed to classify the governed paths plus
    active counterparts such as `apps/frontend`, underscore worker packages,
    `infra/sbatch`, Slurm gateway template settings, and tile publisher/display
    implementation. Each relevant hit is reflected or summarized in the
    inventory evidence column.
  - Input command:
    `find apps workers services scripts .github/workflows -maxdepth 3 \( -path apps/frontend/node_modules -o -path apps/frontend/dist -o -path '*/__pycache__' \) -prune -o -type d -print | sort`
    Expected output: governed candidate directories and active counterpart
    directories are visible and reflected in the inventory; generated directories
    such as `apps/frontend/node_modules`, `apps/frontend/dist`, and `__pycache__`
    subtrees are intentionally excluded.
  - Input command: `uv run ruff check .`
    Expected output: exit 0.
  - Input command:
    `npx --yes markdownlint-cli2 --config .markdownlint.yaml 'docs/**/*.md'`
    Expected output: exit 0.
  - Input command:
    `rg -n "seed_qhh|reset_qhh|summarize_qhh|publish_qhh|apply_smoke_migrations|create_qhh_shud_manifest|run_qhh_backend_smoke|run_qhh_cycle" scripts docs/runbooks tests docs/governance --glob '!**/__pycache__/**'`
    Expected output: QHH entrypoints, direct helper dependencies, runbook
    evidence surfaces, static test surfaces, and the related out-of-chain
    `scripts/seed_qhh_smoke_met_station.py` classification are reflected in
    `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`.
- [x] 1.7 #362 non-goals:
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

- [ ] 3.1 Add `scripts/diagnostic/qhh/README.md` or an equivalent diagnostic manifest listing QHH diagnostic scripts, direct helper dependencies, out-of-chain QHH helper notes, and production replacement commands.
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
