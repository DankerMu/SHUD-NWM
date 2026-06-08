## Context

The audit found tracked placeholder and legacy paths:

- `apps/web` is explicitly a deprecated frontend placeholder.
- Hyphenated worker directories such as `workers/forcing-producer` and `workers/shud-runtime` are placeholders; canonical packages use underscores.
- `workers/sbatch_templates` contains legacy single-run templates and should be treated more carefully than empty placeholders because `services/slurm_gateway/config.py` references its legacy status.
- `services/tile-publisher` exists while active tile publisher code is elsewhere.
- QHH scripts are diagnostic-only and still useful for bring-up/debugging.
- Playwright specs use `page.route('**/api/v1/**')` and should not be called live e2e.
- `.github/workflows/ci.yml` contains a paused job with `&& false`.

## Decisions

### D1. Use four path statuses

Each governed path should be classified as:

- `production`: active runtime/build/deploy path.
- `diagnostic`: supported debugging or bring-up path, not production.
- `test-only`: regression fixtures or mocked tests.
- `archived`: historical reference outside active build/test/deploy.

The inventory must be a persistent artifact, not just PR body prose. Each row should include exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, and final action.

### D2. Do not delete diagnostic QHH scripts first

The QHH scripts already have `DIAGNOSTIC-ONLY` headers and static tests that keep them out of production orchestrator code. First create a diagnostic README/manifest and optionally move them behind short compatibility wrappers later.

### D3. Delete or archive placeholders only with import/CI/runbook evidence

Empty or README-only placeholders can be deleted or archived after grep/import/CI evidence proves no active path depends on them. `workers/sbatch_templates` needs a stronger migration note because it contains actual sbatch templates.

### D4. Separate mocked regression from live proof

Mocked Playwright tests remain useful, but filenames/config/docs must say mocked regression. Live e2e profiles must prohibit `page.route('**/api/v1/**')`.

The live display-readonly profile is a deliverable, not only a plan. If live credentials or node access are unavailable, the issue may mark runtime execution blocked, but the config/script/static no-mock guard still needs to land.

## Risks / Mitigations

- **Risk: removing historical placeholders breaks old docs.** Mitigation: update docs or archive historical docs in the same PR.
- **Risk: moving diagnostic scripts breaks runbooks.** Mitigation: first add README/manifest; move only with wrappers and deprecation messaging.
- **Risk: live e2e cannot run locally.** Mitigation: separate config/profile and make live receipt explicit, not part of fast local tests.

## Verification

- `rg` inventory for candidate paths before deletion/archive.
- `uv run pytest -q tests/test_qhh_scripts_static.py`
- Frontend: `cd apps/frontend && corepack pnpm test && corepack pnpm build`
- CI workflow lint or focused grep proving no `&& false` paused job remains.
