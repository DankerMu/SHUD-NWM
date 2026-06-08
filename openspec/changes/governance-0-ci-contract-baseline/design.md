## Context

The entropy audit found a current master CI failure after previous issue closures. The failed job is `Unit Tests`; the failing test is `test_generated_frontend_types_match_openapi`. That means the repo's shared contract is not in a clean state even before governance cleanup begins.

This is not a feature change. It is the guardrail that prevents later cleanup PRs from masking a pre-existing contract failure.

## Decisions

### D1. Treat CI green as Governance-0

Governance changes must start with a passing trunk. This change is intentionally separated from role-boundary, dead-code, docs, and entropy automation so that those PRs are not blamed for existing contract drift.

The governance issue DAG is:

| Order | Change | Depends on | Blocks |
|---|---|---|---|
| 1 | `governance-0-ci-contract-baseline` | none | all downstream governance work |
| 2 | `governance-1-role-boundary-inventory` | Governance-0 merged or explicit waiver | entropy hard-gates that enforce role boundaries |
| 2 | `governance-2-legacy-dead-code-retirement` | Governance-0 merged or explicit waiver | entropy hard-gates that fail legacy/diagnostic drift |
| 2 | `governance-3-doc-status-alignment` | Governance-0 merged or explicit waiver | entropy doc freshness checks and role-boundary doc links |
| 3 | `governance-4-entropy-automation` | Governance-0 plus report inputs from Governance-1/2/3, or explicit waiver | future hard-gate rollout |

Each GitHub Epic must repeat this dependency in its `Dependencies` field so implementation work cannot start from an accidentally red baseline.

### D2. Make OpenAPI-to-frontend generation explicit

The repository already enforces generated frontend types. The missing piece is an unambiguous maintainer command and acceptance rule. The command should run in the existing frontend toolchain and must not depend on ambient system Python.

### D3. Align Makefile with `uv run`

`AGENTS.md` and README already say Python commands should use the repo-managed environment. `Makefile` still uses `python -m ...` and `ruff check .` directly. This is low-risk and removes a common local/CI mismatch.

## Four-Role Coverage

| Role | Relevance |
|---|---|
| `compute_control` | Must not start governance while scheduler/orchestrator tests are red. |
| `display_readonly` | Frontend generated types and display API contracts must be current before docs/live-evidence cleanup. |
| `slurm_gateway` | No direct code change expected, but CI baseline must prove gateway tests remain unaffected. |
| `shared_contract` | Primary target: OpenAPI, frontend generated types, Makefile command discipline, and CI contract drift. |

## Issue Slices

Governance-0 is implemented by two child PRs:

- #358 closes only the shared OpenAPI/generated frontend type drift.
- #359 closes only Makefile `uv run` command discipline.

The #358 PR must not change Makefile targets, role-boundary docs, dead-code paths, or entropy automation. The #359 PR must not regenerate OpenAPI or frontend type artifacts unless a new drift is introduced by its own change.

## #358 OpenSpec Fixture

Fixture level: expanded

Project profile: NHMS

Change surface:

- `openapi/nhms.v1.yaml`
- `apps/frontend/src/api/types.ts`
- `tests/test_api_contract.py::test_generated_frontend_types_match_openapi`
- `tests/test_openapi_drift.py`
- `apps/frontend` `check:api-types`

Must preserve:

- The committed OpenAPI document remains the source of truth for generated frontend API types.
- Runtime/static OpenAPI drift allowlists remain issue-scoped and unchanged unless a #358 contract mismatch requires a narrow correction.
- Existing frontend consumers of `apps/frontend/src/api/types.ts` remain compatible with the current frontend CI gate.
- #359 Makefile command discipline remains out of scope for #358.

Must add/change:

- Reconcile `apps/frontend/src/api/types.ts` so regeneration from `openapi/nhms.v1.yaml` produces byte-for-byte identical output.
- Record focused evidence that the OpenAPI/generated-type drift is closed.

Risk packs considered:

- Public API / CLI / script entry: selected - OpenAPI is the public API contract and the frontend generation command is the contract entrypoint.
- Config / project setup: selected - #358 must use the existing frontend toolchain and not ambient system Python.
- File IO / path safety / overwrite: not selected - generated output is a committed artifact under a fixed repo path; no user-controlled path behavior changes.
- Schema / columns / units / field names: selected - the generated TypeScript schema must match OpenAPI exactly.
- Auth / permissions / secrets: not selected - no auth or credential behavior changes.
- Concurrency / shared state / ordering: not selected - no shared runtime state or ordering behavior changes.
- Resource limits / large input / discovery: not selected - no data discovery or large-input runtime code changes.
- Legacy compatibility / examples: selected - existing frontend generated-type consumers must remain compatible.
- Error handling / rollback / partial outputs: not selected - no runtime failure or publish path behavior changes.
- Release / packaging / dependency compatibility: selected - use the repository frontend generation/check command.
- Documentation / migration notes: selected - PR evidence must say Governance-1/2/3/4 depend on the restored baseline.
- Geospatial / CRS / basin geometry: not selected - no geospatial contract semantics change.
- Hydro-met time series / forcing windows: not selected - no hydro-met time semantics change.
- SHUD numerical runtime / conservation / NaN: not selected - no runtime solver change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior change.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm behavior change.
- External hydro-met providers / snapshot reproducibility: not selected - no provider snapshot change.
- Run manifest / QC provenance: not selected - no run evidence contract change.
- Published NHMS artifacts / display identity: not selected - only generated frontend API type identity changes.

Required evidence:

- `uv run pytest -q tests/test_api_contract.py::test_generated_frontend_types_match_openapi`: committed `types.ts` equals regenerated output from committed OpenAPI.
- `uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`: focused API contract and drift checks pass.
- `cd apps/frontend && corepack pnpm run check:api-types`: frontend toolchain sees no generated-type drift.
- `cd apps/frontend && corepack pnpm test`: existing frontend regression consumers still pass.
- `cd apps/frontend && corepack pnpm build`: current frontend build remains green.
- If the regenerated diff changes TypeScript declarations rather than comments/JSDoc only, run an additional focused consumer/typecheck proof or split the broader frontend type debt into a separate issue. A pre-existing full-project `tsc -p tsconfig.app.json --noEmit` failure is not a #358 blocker when the #358 diff is declaration-shape neutral and the current CI frontend gate passes.

Non-goals:

- #359 Makefile target updates.
- Full backend fast-gate remediation beyond reporting current evidence if an unrelated pre-existing failure remains.
- Full frontend `tsc -p tsconfig.app.json --noEmit` debt remediation when failures are unrelated to generated API type declaration shape.
- Role-boundary, docs-status, legacy cleanup, or entropy automation changes.

## #359 OpenSpec Fixture

Fixture level: compact

Project profile: NHMS

Change surface:

- `Makefile` targets `dev`, `migrate`, `seed-demo`, `seed-m1-model`, `test`, and `lint`
- `Makefile` target `reset-db` as a passthrough caller of `migrate` and `seed-demo`

Must preserve:

- Target names and user-facing behavior stay unchanged.
- `reset-db` still drops and recreates the development database before invoking `migrate` and `seed-demo`.
- Existing documentation that tells contributors to use the repository-managed Python environment remains accurate.
- #358 OpenAPI/generated frontend types remain out of scope and must not be regenerated by #359.

Must add/change:

- Backend Python and lint commands in Makefile run through `uv run`.
- `reset-db` inherits the updated `uv run` behavior through `$(MAKE) migrate` and `$(MAKE) seed-demo`.

Risk packs considered:

- Public API / CLI / script entry: selected - Makefile targets are developer command entrypoints.
- Config / project setup: selected - the change standardizes repository-managed Python resolution through `uv run`.
- File IO / path safety / overwrite: not selected - no file path behavior changes beyond existing Makefile commands.
- Schema / columns / units / field names: not selected - no schema or generated type changes.
- Auth / permissions / secrets: not selected - no credential behavior changes.
- Concurrency / shared state / ordering: not selected - target ordering stays unchanged.
- Resource limits / large input / discovery: not selected - no discovery or large-input behavior changes.
- Legacy compatibility / examples: selected - target names and existing `make reset-db` workflow remain compatible.
- Error handling / rollback / partial outputs: not selected - no new rollback behavior; existing `reset-db` DB drop/create semantics are preserved.
- Release / packaging / dependency compatibility: selected - commands must use locked repository tooling via `uv run`.
- Documentation / migration notes: selected - PR evidence must explain targets inspected and any long-running target not executed.

Required evidence:

- Inspect `make -n dev`, `make -n migrate`, `make -n seed-demo`, `make -n seed-m1-model`, `make -n test`, `make -n lint`, and `make -n reset-db` or equivalent Makefile output to prove Python/lint commands use `uv run` and `reset-db` reaches updated child targets.
- Run `make lint` or `uv run ruff check .`.
- Run `make test` or explain if the full verbose test target is substituted with an equivalent focused/fast gate due runtime.
- Do not edit `openapi/nhms.v1.yaml` or `apps/frontend/src/api/types.ts`.

Non-goals:

- #358 OpenAPI/generated frontend type reconciliation.
- CI workflow restructuring.
- Changing database reset semantics or executing destructive `reset-db` locally without explicit evidence need.

## Risks / Mitigations

- **Risk: regenerating types changes broad frontend snapshots.** Mitigation: only accept changes generated from the committed OpenAPI and verify with focused contract tests plus frontend type checks.
- **Risk: Makefile edits change developer ergonomics.** Mitigation: keep target names and semantics unchanged; only prefix Python/lint invocations with `uv run`.
- **Risk: CI red has more than one cause.** Mitigation: run the focused failing test first, then the full fast backend gate.

## Verification

- `uv run pytest -q tests/test_api_contract.py::test_generated_frontend_types_match_openapi`
- `uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`
- `uv run pytest -q -m "not e2e and not grib and not integration"`
- `cd apps/frontend && corepack pnpm run check:api-types`
- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
