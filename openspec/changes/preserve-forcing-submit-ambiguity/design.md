## Context

The incident's client already marks POST502 ambiguous, but chain_stage_execution's durable branch is forecast-only. A later pass used a new stage-derived cohort key and repeated forcing. Physical cause of empty stdout is not proven. #2439 is merged in #2445; both sources were published at2026091512 on2026-09-16T23:06Z. User now explicitly requires minimal change.

## Goals / Non-Goals

Governing invariant: one unresolved new forcing attempt prevents another intersecting source/cycle/model attempt from being submitted; when the original is authoritatively resolved, normal orchestration can progress without duplicate forcing.

Must preserve: forecast aliases and historical digest bytes, existing retry/absence proof rules, strict warm-start and forcing witness guards, normal no-ambiguity submit and completion, unrelated source/cycle/model eligibility, successful legacy products and existing ownership/attempt/CAS checks.

Non-goals: generic historical adoption or evidence reconstruction, new operator CLI, journal migration, arbitrary receipt-as-authority, broad fencing of sparse permanent failures, current49174 adoption, stdout capture changes, transient-code-list shortcuts, circuit reset, DB on22, new dependencies/config or unrelated source rollout.

## Decisions

1. Extend the existing submission lifecycle, not a parallel state system. Persist the new forcing attempt and sufficient source/cycle/stage/member/task/owner/attempt identity before Gateway entry, using existing fields and journal mechanisms wherever possible. Do not change forecast aliases, digest bytes or historical row interpretation to pretend forcing is forecast.
2. Crossed-boundary response ambiguity (HTTP502, transport failure, invalid success identity) remains unknown acceptance with unbound Slurm identity and origin error. It is not permanent failure, known absence or automatically retryable. Proven pre-acceptance rejection retains existing semantics.
3. Use source/cycle/model member overlap as the duplicate boundary. Check-and-reserve is atomic under existing locking; renamed convert_cohort/forcing_cohort keys and member reordering/subsets do not bypass unresolved new authority. Persisted identity makes restart safe. Unrelated work continues.
4. Finish the normal lifecycle: trustworthy existing task identity/accounting/status queries resolve the original new attempt. Confirmed bound completion goes through normal forcing output/witness validation and stage continuation, not an operator adoption command. Confirmed nonacceptance may use the existing guarded retry/absence path; lack of evidence is never absence. If identity cannot be proven, expose reconciling/blocker accurately and do not manufacture progress. A fix that adds only an uncleared fence fails acceptance.
5. Do not infer model intersections from a hash or turn every historical permanent/null-id row into an ambiguity. Historical49174 has no durable members and was superseded by49309; leave it and published authority unchanged. New records must be self-sufficient for the new lifecycle. Any narrowly supported legacy handling must be justified by already-complete identity; no field reconstruction or default owner.

## Sibling surfaces and references

chain_slurm_client and chain_stage_execution classify/record submit; reservation and file_orchestration_journal own concurrency/current authority; reconcile and scheduler candidate/execution evidence consume it; forcing completion consumers own valid witness projection. accepted_submit_cohort owns forecast digest invariants (#1183). Reuse existing normal task-completion/package-validation seams, not new operator paths.

Incident references: openspec/changes/archive/2026-09-17-preserve-pre-forecast-retry-stages/evidence/{node22-ifs-gateway-failure,node22-ifs-acceptance-probe,node22-recovered-stage-identities,node27-dual-source-latest,node27-dual-source-decoded-tiles}.txt and docs/runbooks/receipts/2026-09-16-forcing-phase-recovery.md. They are historical diagnosis, not runtime authority.

## Risks / Trade-offs

Incomplete resolution → require confirmed-completion→forecast continuation regression. Cross-key concurrency race → atomic reservation overlap test. Overbroad legacy guard → successful replacement/sparse old-row compatibility. Forecast contagion → unchanged digest/alias/reconcile tests. Untrustworthy accounting → preserve existing owner/attempt/uniqueness requirements, fail closed without auto-retry.

## Verification and Deployment

Parent runs deterministic red/green onnode27 against exact source plus real file journal/HTTP client; no real production submit reproduction. Local Ruff/OpenSpec and existing affected suites. Source is based on masterb922b245; productione7bd816 is older. Build and test the actual minimal deployment tree separately, retainingPython3.12.7 and env. Quiesce readers/jobs for rollout; no full-master deployment. Production normal progression and node27 data read are smoke evidence, not substitutes for an injected ambiguity regression. No historical apply/dry-run tool is introduced.
