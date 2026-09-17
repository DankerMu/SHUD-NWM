## Context

The production incident is captured in #2445 evidence. A POST502 is already marked ambiguous by chain_slurm_client; chain_stage_execution only preserves that for forecast. File-journal reconciliation cannot consume a permanently_failed/null-id forcing row. Subsequent scheduler recovery used a different cohort key and repeated work. Captured source boundary is proven; physical reason for empty stdout is not.

## Goals / Non-Goals

Governing invariant: after a forcing submit can have been accepted, no intersecting source/cycle/model forcing work is submitted again until its durable authority is resolved through verified evidence; completion is projected only from fully validated outputs of the uniquely bound attempt.

Must preserve: forecast aliases, historical digest bytes, accepted-submit CAS/owner/attempt protections, stage retry budgets, permanent policy rejection semantics, journal root authority and append-only audit, strict warm-start and forcing witness guards, unchanged Slurm resource limits.

Non-goals: fix sbatch capture implementation, reset circuit, edit raw journal, use node22 DB, change scientific/model identity, adopt superseded49174, deploy unrelated master changes, or weaken forecast reconciliation.

## Decisions

1. Keep an explicit forcing stage domain; reuse existing reservation, locks and journal transactions rather than cloning a second persistence system. Do not add forcing to FORECAST_COHORT_STAGE_ALIASES or alter historical forecast digest material. Pin forcing member/task/source/cycle/attempt/owner/account identity before Gateway entry; incomplete identity refuses before submission.
2. Introduce end-to-end forcing dispatch in producers, validators, inventory readers, reconciliation and consumers. POST502/transport/invalid-success ambiguity keeps null binding and original error provenance, yields unknown_after_attempt and never proves absence. Explicit pre-acceptance rejection remains rejected. Persist the reservation before crossing the request boundary so crashes also remain fenced.
3. Fence intersecting members by canonical source/cycle/model identity, not only stage-derived run or idempotency string. The convert_cohort→forcing_cohort rename and reordered/subset/superset membership cannot bypass an unresolved attempt. Unrelated cycles/sources/models remain eligible. Legacy gateway-crossed unbound failed records must also prevent unverified repeated execution when their member identities can be resolved; unprovable scope fails closed within its known cycle, not globally.
4. Operator command follows existing dry-run/explicit-attestation patterns in operator_released_reservation_recovery.py and journal_root_authority.py. Exact job/master, expected observed revision/attempt and evidence identity are required. Both CLI frontends share one implementation. Local operator authority is explicit; no automatic attestation, user-controlled paths treated as trusted proof, or writable display API path.
5. Adoption requires unique source/cycle/stage/job_type/idempotency/owner/account/attempt lineage, complete indexed member manifest and exact task-set accounting, successful terminal tasks and real forcing package identity/integrity checks through existing validators. Refuse extra/duplicate/missing tasks, mixed attempts, traversal, symlink escapes, unbounded reads and changed evidence. Stdout self-reported checksum and matching timestamp alone are insufficient.
6. Legacy reconstruction uses independently bound manifest, archived operator-collected Slurm identity evidence and current accounting/object integrity only under explicit operator attestation; retain immutable receipt hashes/provenance. A hash does not authenticate arbitrary evidence. Missing trustworthy provenance or unique identity is a refusal, not a default. If no supported verified evidence can prove a field, no adoption is available for that row.
7. Before any authority write, re-read current row and competing member attempts under existing cycle lock; CAS the pinned revision/attempt and deny newer accepted/running/completed/published replacements. Preserve original failure history, append an audited recovery transition and publish the complete cohort atomically through existing transactional/journal mechanisms. No partially successful adoption and no orphan retry budget changes. Repeat same successful action returns existing result without new execution or duplicated business events.
8. Prove the consumer result, not just bound fields: the next normal scheduler pass must see valid per-model forcing witnesses and continue at forecast without submitting forcing. Live49174 is superseded by49309 and MUST return a write-free refusal; full unsuperseded recovery is tested in isolated incident fixtures, never by manufacturing a new production failure.

## Sibling surfaces and seams

Submission and response classification: chain_slurm_client, chain_stage_execution, chain_forecast_submission. Identity/reservation: accepted_submit_identity and accepted_submit_cohort (forecast compatibility), reservation. Persistence: file_orchestration_journal, journal root seam, current/latest/direct/by-cycle/inventory views and recovery replay. Consumers: reconcile, scheduler candidate selection/execution evidence, retry service and stage continuation. Artifact seam: existing forcing producer/package validators; model basins package checksum is not forcing-content integrity (ADR0006). Entry/reference: operator_released_reservation_recovery and operator_reserved_demotion.

## Risks / Trade-offs

External verification races → revalidate identity/current authority before locked CAS, preserve hashes and fail on changes. Comment retention loss → only explicit auditable legacy reconstruction, otherwise refusal. Broad field addition → preserve forecast schema/digest and test old rows. Cross-key fences → test overlap and unrelated-source/cycle isolation. Multiple authority views → crash/replay and no-partial-write tests. Duplicate historical execution → superseded refusal, never overwrite later products.

## Migration Plan

Land independent PR after #2445. Test main feature and the exact minimal deployment tree based on productione7bd816; changed modules may have newer-mainline dependencies, so single-file patch equivalence is not sufficient. Quiesce relevant scheduler/Gateway readers and Slurm jobs before rollout; preserve3.12.7/env and rollback only when quiescent. Do not rewrite historical records en masse. First run dry-run against stale49174 and observe zero authority mutation; monitor one normal later source cycle and node27 exact published data. Already healthy publication is not proof of new ambiguity handling.

## Review readiness

Fixture authored, not reviewed. Required reviewer service failed repeatedly during #2445 final review (two seats, transport retries); no implementation may begin without a valid fixture pass. Production scope is authorized, not authorization to bypass review or identity guards.
