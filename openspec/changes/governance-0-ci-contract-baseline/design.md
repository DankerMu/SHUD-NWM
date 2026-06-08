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

## Risks / Mitigations

- **Risk: regenerating types changes broad frontend snapshots.** Mitigation: only accept changes generated from the committed OpenAPI and verify with focused contract tests plus frontend type checks.
- **Risk: Makefile edits change developer ergonomics.** Mitigation: keep target names and semantics unchanged; only prefix Python/lint invocations with `uv run`.
- **Risk: CI red has more than one cause.** Mitigation: run the focused failing test first, then the full fast backend gate.

## Verification

- `uv run pytest -q tests/test_api_contract.py::test_generated_frontend_types_match_openapi`
- `uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`
- `uv run pytest -q -m "not e2e and not grib and not integration"`
- `cd apps/frontend && corepack pnpm run check:api-types`
