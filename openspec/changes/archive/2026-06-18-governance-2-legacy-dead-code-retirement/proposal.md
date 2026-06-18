## Why

The repository contains active production paths, diagnostic paths, historical placeholders, paused CI lanes, and old mocked e2e tests. Without a governed retirement process, future contributors cannot tell whether a path is production, diagnostic, archived, or test-only.

## What Changes

- Establish a deletion/archive policy for legacy placeholders and diagnostic paths.
- Retire or archive `apps/web` and hyphenated worker placeholders only after inventory confirms they are not active entrypoints.
- Isolate QHH diagnostic scripts without deleting their diagnostic value.
- Split mocked Playwright regression tests from live display-readonly evidence tests.
- Convert permanently paused CI jobs such as `frontend-m15-visual` from `&& false` entropy into either archived evidence or a manual workflow.

## Capabilities

### New Capabilities

- `legacy-dead-code-retirement`: Governs how production, diagnostic, archived, and test-only paths are classified and retired.

### Modified Capabilities

<!-- No existing product capability is modified. -->

## Impact

- Candidate cleanup paths: `apps/web`, `workers/*-*`, `workers/sbatch_templates`, `services/tile-publisher`.
- Diagnostic paths: `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`, `scripts/create_qhh_shud_manifest.py`.
- Frontend e2e: `apps/frontend/e2e/*.spec.ts`, Playwright config, validation docs.
- CI: `.github/workflows/ci.yml`.
- Documentation: `docs/modules/00_module_index.md`, `docs/archived/**`, `docs/VALIDATION.md`, `docs/bugs.md`.
