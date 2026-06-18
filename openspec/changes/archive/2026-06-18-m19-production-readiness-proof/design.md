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

Allowed status/execution-mode combinations are normative:

| status | allowed execution_mode values |
|---|---|
| `passed` | `deterministic`, `policy_simulated`, `backend_route_executed`, `dry_run_sink`, `simulated_drill`, `live_proof` |
| `failed` | any executed mode except `not_executed` |
| `blocked` | `not_executed` |
| `not_executed` | `not_executed` |
| `release_blocked` | `not_executed`, `policy_simulated`, `dry_run_sink`, `simulated_drill`, `live_proof` |

Failed live proof is represented as `status=release_blocked`,
`execution_mode=live_proof`, `required_for_final=true`, and
`live_proof_accepted=false`; the item must include residual risk and removal
criteria. Non-final optional live proof that executes and fails may use
`status=failed`, `execution_mode=live_proof`, and `required_for_final=false`.

Every readiness item must carry `required_for_final`,
`live_proof_accepted`, `artifact_refs`, `residual_risk`, and
`removal_criteria` fields. The summary must carry
`final_production_readiness_claimed`; it may be true only when every
`required_for_final=true` item is `passed` with `live_proof_accepted=true` or is
an explicit scoped exclusion.

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

## Risk Triage

- Fixture level: expanded.
- Repair intensity: broad-expanded.
- Rationale: M19 crosses production-readiness evidence, live-proof gating, release-blocker semantics, redaction of live credentials/receipts, docs, and existing production closure lanes. A false positive readiness claim or unredacted live proof would be a release/security issue.

## Risk Packs

- Auth/permissions: selected - live backend auth proof and policy-simulated/backend-route/live-proof modes must remain distinguishable.
- Evidence/audit/receipt contract: selected - readiness items, release blockers, dependency receipts, artifact references, residual risks, and removal criteria are the core contract.
- Live dependency safety: selected - live lanes must be opt-in only and fast CI must not execute live IdP, alert sink, Slurm, object store, rollback, weather, or real-national-data operations accidentally.
- Secret/path redaction: selected - live credentials, alert sinks, auth provider metadata, dependency receipts, and artifact paths must be redacted/bounded before persisted or printed.
- Error handling / partial outputs: selected - deterministic failures, missing fixtures, missing live dependencies, and malformed receipts must produce stable `failed`, `blocked`, `not_executed`, or `release_blocked` evidence without claiming readiness.
- Schema/type drift: selected - status/execution_mode truth table, required fields, blockers, exclusions, and final readiness claim must be represented by stable schema/tests.
- Compatibility / existing lane drift: selected - M10/M16/M17/M18 production-like evidence and existing validation commands must remain compatible and truthfully consumed.
- Performance/resource bounds: selected - readiness report generation must not recursively ingest unbounded live proof payloads or huge artifacts; references should remain bounded.
- Money/data loss/destructive live actions: not selected - M19 must ingest or simulate evidence by default; any real live rollback/drill is opt-in and evidence-oriented, not destructive by default.

## Invariant Matrix

Governing invariant: M19 must truthfully separate deterministic evidence, opt-in live proof, release blockers, and scoped exclusions so the system never claims final production readiness without accepted live evidence and never leaks live credentials.

Source-of-truth identity/contract: readiness item `{surface, status, execution_mode, required_for_final, artifact_refs, residual_risk, removal_criteria, exclusions, live_proof_accepted}` plus final summary `{final_production_readiness_claimed, release_blockers, exclusions}`.

Surfaces:

- Producers: existing production closure validators in `services/production_closure/*`, new readiness command/report producer, deterministic fixture inputs, optional live-proof receipt inputs.
- Validators/preflight: schema validation, status/execution_mode truth-table validator, live opt-in guards, redaction/bounds checks, dependency receipt acceptance checks.
- Storage/cache/query: evidence root artifact writer, summary JSON, blocker JSON, docs/progress references.
- Public routes/entrypoints: CLI/module command for readiness validation, existing `nhms-production validate-*` producers consumed by M19.
- Frontend/downstream consumers: release reviewers, docs, `progress.md`, CI evidence readers; no frontend runtime change expected unless evidence links are surfaced.
- Failure paths/rollback/stale state: missing live IdP/alert sink/Slurm/object store/source credentials, deterministic fixture missing, malformed receipt, failed dependency proof, live mode omitted, CLDAS/national-data exclusions.
- Evidence/audit/readiness: readiness evidence schema, release blocker summary, redacted live proof metadata, residual risk/removal criteria, artifact references.

Regression rows:

- deterministic lane with current demo/Basins/production-like evidence -> `status=passed` for deterministic checks, `execution_mode` never equals `live_proof`, `final_production_readiness_claimed=false` while live blockers remain.
- missing live IdP, alert sink, Slurm/object store/source/weather credentials -> each required live surface records `release_blocked` or `not_executed` with owner/action/removal criteria and does not fail deterministic checks by default.
- explicit live auth proof receipt with provider metadata -> accepted evidence records `execution_mode=live_proof`, redacts credentials/URLs/tokens, and remains release-blocked if required allowed/denied coverage is incomplete.
- fast CI/default command without live flags -> no live network/backend/sink/rollback execution is attempted; report records not-executed/release-blocked live proof.
- CLDAS and incomplete real national data in current scope -> recorded under exclusions with `status=not_executed`, not under failed deterministic checks and not as satisfied live proof.
- malformed or oversized live proof payload -> stable blocked/release-blocked evidence with bounded redacted artifact, no exception traceback or secret leakage.
- release summary with any required live blocker -> `final_production_readiness_claimed=false` and blocker summary lists blocker id, surface, status, residual risk, removal criteria, and artifact refs.
- all deterministic checks pass but live proof missing -> deterministic summary remains useful, final readiness remains false, docs explain interpretation.

## Boundary Surface Checklist

- Shared helper roots: readiness schema/status validator, redaction helper, artifact writer, blocker summary builder.
- Public entrypoints: new readiness command/report and existing `nhms-production validate-*` evidence consumed by M19.
- Read surfaces: summary JSON, blocker JSON, docs/progress, CI logs.
- Write/delete/overwrite surfaces: evidence artifact writes under configured evidence root only; no destructive live operation by default.
- Staging/publish/rollback surfaces: opt-in live rollback drill receipt/execution evidence.
- Producer/consumer evidence boundaries: M10/M16/M17/M18 validation outputs consumed into readiness summary without changing their semantics.
- Stale-state/idempotency boundaries: repeated readiness runs overwrite or namespace artifacts deterministically and do not mix stale live proof with current run ids.
- Unchanged downstream consumers: existing production validation tests and docs remain valid; no fast CI live dependency is introduced.
