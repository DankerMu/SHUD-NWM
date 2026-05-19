## Context

The project already has production-like evidence lanes, but release decision review needs a single truth table and summary artifact. M19 creates a readiness framework that can be useful immediately with deterministic data and can later ingest live proof without changing semantics.

## Design Decisions

- Readiness statuses are `passed`, `failed`, `blocked`, `not_executed`, and `release_blocked`.
- Each evidence item records `execution_mode`, input dependencies, artifact path, residual risk, and removal criteria for blockers.
- CLDAS is excluded by current product decision; evidence must state it is not considered for this readiness scope.
- Incomplete real national data is not a blocker for deterministic readiness; real-data proof is opt-in and separately marked.
- Final summary cannot set `final_production_readiness_claimed=true` unless every required live dependency is proven.

## Status and Execution Mode Truth Table

`status` and `execution_mode` are separate fields.

| Status | Meaning | Typical execution_mode |
|---|---|---|
| `passed` | Required check executed and met its acceptance criteria. | `deterministic`, `backend_route_executed`, `live_proof` |
| `failed` | Required check executed and violated acceptance criteria. | Any executed mode |
| `blocked` | Deterministic/preflight dependency needed for this lane is missing or invalid, so the check cannot run. | `not_executed` |
| `not_executed` | Optional or explicitly out-of-scope check was intentionally skipped. | `not_executed` |
| `release_blocked` | Required live proof for release is missing, incomplete, or failed, even if deterministic evidence passed. | `not_executed`, `policy_simulated`, `dry_run_sink`, `simulated_drill` |

Canonical execution modes: `deterministic`, `policy_simulated`, `backend_route_executed`, `dry_run_sink`, `simulated_drill`, `live_proof`, and `not_executed`.

Required live-proof surfaces for final production readiness: live backend auth, live alert sink delivery, live rollback execution, accepted dependency proofs for Slurm/object-store/source/E2E/MVT where claimed, and real target-environment configuration receipts. Missing CLDAS and incomplete real national data are recorded as scoped exclusions for this stage, not blockers.

## Dependency Order

- Evidence schema and truth table before validators.
- Deterministic lane before opt-in live lane.
- Blocker summary before final reporting docs.

## Risks and Mitigations

- Risk: deterministic evidence is mistaken for live proof. Mitigation: mandatory `execution_mode` and live flags.
- Risk: missing dependencies are silently skipped. Mitigation: blockers require removal criteria and owner/action text.
- Risk: scope creep into CLDAS or national data completion. Mitigation: explicit non-goals and evidence exclusions.

## Verification

- `openspec validate m19-production-readiness-proof --strict`
- Targeted production readiness schema/report tests.
- Existing production closure lane tests remain green.
- Generated summary fixture shows deterministic pass plus live release blockers without claiming final readiness.
