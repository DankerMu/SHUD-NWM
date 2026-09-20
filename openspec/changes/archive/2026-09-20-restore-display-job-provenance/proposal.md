## Why

#2420 blocks genuine readonly/C4 acceptance: node-22's DB-free journal owns real job state, while node-27's four Ops routes still read an empty `ops.pipeline_job` and omit a coherent strict response identity. The user requires this defect resolved before resuming held PR #2450 (#2121-A + #2346 step two), without manufacturing jobs or waiving C4.

## What Changes

- Preserve the existing job/stage/log capability. Publish bounded, validated, per-run job provenance from the real node-22 journal at the existing run-tree copyback boundary; support an explicit backfill using the same publisher.
- Publish only real, identity-bound logs. For reconciled forecast task rows without a log URI, use the existing gateway's parent-array log response and require an exact task/run/model match; do not relabel arbitrary run stdout or another array member's log.
- Ingest that sidecar on node-27 into the existing job read model, including already-parsed/published runs, without restarting scientific parsing or changing hydro readiness. Replays are idempotent; stale/conflicting/mismatched evidence cannot overwrite newer truth.
- Return coherent strict identity on all four Ops responses while preserving their current `data` container shapes, canonical request-id, filtering and control-plane denial. Update OpenAPI/generated consumers, not the validator's acceptance bar.
- Reconcile persistence specifications with the actual DB-free authority and node-27 projection ownership; PostgreSQL-backed scheduler behavior remains supported and unchanged.

## Capabilities

### New Capabilities
- `published-pipeline-job-provenance`: source-owned publication, verified log binding, bounded transport, idempotent node-27 projection and deployed backfill.

### Modified Capabilities
- `pipeline-job-persistence`: distinguish PostgreSQL scheduler writes from file-journal authority and derived display rows; preserve legacy lifecycle/event semantics in their owner lane.
- `display-readonly-ops-ui`: strict response identity is a coherent resolved object, not merely accepted query parameters or fragmented row fields.

## Impact

Source owners: `services/orchestrator/file_orchestration_journal.py` publication-safe read seam if necessary; `chain_forecast_execution.py` existing copyback boundary; new narrowly owned provenance publisher/importer; `scripts/node27_autopipeline.py` lightweight projection phase independent of parse skip; `apps/api/routes/pipeline.py`, OpenAPI/static/generated types, related behavioral tests and runbooks. No new framework/dependency, scheduler-state relocation, database on node-22, or scheduler on node-27. Existing `nhms_ingest_rw` owns `ops.pipeline_job`; live role ownership is verified before apply, and display receives no additional privileges.

## Risk Classification

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (agree; expanded triggers: public Ops API, untrusted file IO, persisted `ops.pipeline_job`, copyback-adjacent publication, cross-plane identity)
Blast radius: four production Ops routes, node-22 copyback terminal path, node-27 ingest DB rows, public C4 / readonly validator, held PR #2450
Selected risk packs: Public API / CLI / script entry; Config / project setup; File IO / path safety / overwrite; Schema / columns / units / field names; Auth / permissions / secrets; Concurrency / shared state / ordering; Resource limits / large input / discovery; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes
Not selected: Release / packaging / dependency compatibility (no new dependency); scientific computation and Slurm submit/retry/reconcile policy (explicit non-goals; copyback hook must not regress them)
Evidence floor: real-journal publisher roundtrip with actual base-red consumer regressions; node-27 real-DB idempotency/conflict/role tests; readonly deny-write matrix; two-source strict jobs/status/stages/logs plus formal public C4 PASS; node-22 publication rehearsal read-only vs journal/Slurm using the active 3.12 interpreter without environment rebuild. Merge #2420 before rebasing/resuming the held joint batch.
