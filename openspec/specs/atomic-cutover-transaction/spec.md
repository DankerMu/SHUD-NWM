# atomic-cutover-transaction Specification

## Purpose
TBD - created by archiving change mapping-variant-state-compatibility. Update Purpose after archive.
## Requirements
### Requirement: State clone executes in the same transaction as model activation

The state clone SHALL run as a hook mounted on the ordered pre-activation extension point of the `variant-activation-cutover` capability defined by change `source-specific-model-variant-routing` (Change 4, committed contract: hooks run inside the lifecycle transaction, each receiving the transaction cursor and the activation context — `basin_version_id`, previous active model, target model, source scope), and SHALL execute inside the SAME database transaction that marks `M1` active and the previous active model `M0` `superseded`. When the clone step is engaged (per the applicability requirement below), any failure in it — fingerprint-gate refusal, invalid gate inputs, a `(model_id, COALESCE(source_id, ''), valid_time)` unique-key conflict, or a missing/stale qualified source snapshot for a source in scope not covered by an approved cold-start input — SHALL cause the whole transaction to roll back. This capability is a CONSUMER of the Change 4 extension-point contract and SHALL NOT define the extension point, the activation transaction, or the hook ordering itself.

#### Scenario: Activation and clone commit atomically

- **WHEN** a cutover activates `M1`, supersedes `M0`, and clones `(M0, source, t*)` into `(M1, source, t*)` for every source in scope and every step succeeds
- **THEN** the activation and the clones commit together in one transaction
- **THEN** after commit `M1` is `active`, `M0` is `superseded`, and the `(M1, source, t*)` clone rows are present.

#### Scenario: Clone failure rolls back the whole transaction

- **WHEN** the activation transaction runs but the engaged clone step fails (fingerprint refusal, invalid gate inputs, unique-key conflict, or a missing/stale qualified source snapshot with no approved cold-start input covering that source)
- **THEN** the whole transaction rolls back
- **THEN** `M1` is not left `active`, `M0` is not left `superseded`, and no `(M1, source, t*)` clone row is written
- **THEN** no "activated-but-not-transferred" intermediate state is committed or observable.

#### Scenario: No activated-but-not-transferred state is ever observable

- **WHEN** any external reader observes the database at any point during or after a cutover
- **THEN** it never sees `M1` `active` with a missing `(M1, source, t*)` successor state for a source in scope, unless an approved cold-start audit record explicitly covers that source
- **THEN** for every non-approved source, the activation and the clone are only ever both-present (committed) or both-absent (rolled back).

#### Scenario: Hook ordering relative to display flag flip is owned by the extension point

- **WHEN** the same cutover transaction also flips display station `active_flag` (change `direct-grid-display-cutover`, Change 8)
- **THEN** the relative order of the state-clone hook and the station-flag-flip hook is defined by the Change 4 extension-point contract, not by this capability
- **THEN** this capability only guarantees the clone participates in the same transaction and fails the transaction as a whole on error.

### Requirement: The clone step engages only for direct-grid activations with a previous active model

The clone hook SHALL be a no-op — writing no row, raising no error, and recording an audited skip reason — unless BOTH hold in the Change 4 hook context: (a) the activation context carries a previous active model for the scope, and (b) the target model classifies as direct-grid under Change 4's single classifier (its `resource_profile.direct_grid_forcing` parses successfully through `workers/forcing_producer/direct_grid_contract.py:load_forcing_mapping_contract_from_manifest` with `forcing_mapping_mode='direct_grid'`; a contract that is absent, malformed, or non-direct classifies as legacy-mapping, fail-closed). The engaged forms are therefore exactly the legacy→direct-grid cutover and the direct→direct′ fix-forward. On the idempotent already-current short-circuit the hook is not invoked at all (owned by Change 4's contract).

#### Scenario: A fresh basin's first activation commits without a clone

- **WHEN** a direct-grid variant is activated for a basin scope that has no previous active model (a new basin routed direct-grid from day one, docs §12 — no warm state to migrate exists by design)
- **THEN** the clone hook no-ops with the audited skip reason `no_previous_active_model` and the activation commits
- **THEN** no missing-source rollback fires, because the clone step never engages.

#### Scenario: A legacy-target activation does not engage the clone

- **WHEN** an activation's target model classifies as legacy-mapping (contract absent, malformed, or non-direct) on a basin without direct-grid activation history
- **THEN** the clone hook no-ops with the audited skip reason `target_not_direct_grid` and the operation proceeds under the pre-existing lifecycle rules
- **THEN** the 13 live legacy basins' normal activation operations are unaffected by the hook.

### Requirement: Explicit cold-start approval is the only way to commit an engaged cutover without a clone row

When an engaged clone step refuses — fingerprint inequality, invalid gate inputs, or a missing/stale qualified snapshot for a source in scope — the activation SHALL roll back and surface the stable error code `state_clone_cold_start_approval_required` plus an `ops.audit_log` record naming the blocked `(basin_version_id, source_id)` scope and the refusal cause. An **approved cold start** SHALL be an activation request carrying an explicit cold-start approval input — approver identity, reason, and the covered source ids — recorded in `ops.audit_log` in the same transaction; for the covered sources the clone hook skips without writing a clone row and the activation commits. The approval audit record and the activation result SHALL carry a spin-up-distortion-announcement obligation marker (§11.3 clause 3: 冷启动仅作为明确降级策略，spin-up 期流量失真必须公告); executing the announcement and running the first cold cycle under the approval are owned by the rollout change (Change 7), not by this capability.

#### Scenario: Refusal without approval surfaces a stable code and audit record

- **WHEN** an engaged cutover's fingerprint gate refuses and the activation request carries no cold-start approval
- **THEN** the whole transaction rolls back and the operation result carries the stable error code `state_clone_cold_start_approval_required`
- **THEN** an `ops.audit_log` record marks the blocked scope, its sources, and the refusal cause, giving the cold-start approval route a concrete, queryable trigger.

#### Scenario: An approved cold start commits without a clone row

- **WHEN** the same cutover is re-requested with an explicit cold-start approval input covering the blocked sources
- **THEN** the clone hook skips the covered sources writing no clone row, the activation commits (`M1` `active`, `M0` `superseded`), and the approval — approver identity, reason, covered sources — is recorded in `ops.audit_log` in the same transaction
- **THEN** the audit record and the activation result carry the spin-up-distortion-announcement obligation marker (§11.3 clause 3).

#### Scenario: Approval is scoped to named sources only

- **WHEN** a dual-source cutover carries a cold-start approval covering only `ifs` and a qualified `(M0, gfs, t*)` snapshot exists with an equal fingerprint
- **THEN** the `gfs` clone still executes through the fingerprint gate and writes its `(M1, gfs, t*)` row, while `ifs` commits without a clone row under the approval
- **THEN** the approval never widens beyond its named sources.

### Requirement: The cloned checkpoint is published to the scheduler file state index before the registry manifest re-publish

Because the node-22 DB-free scheduler resolves strict warm-start evidence from the file state snapshot index (`NHMS_SCHEDULER_STATE_INDEX`, read via `packages/common/state_manager.py::FileStateSnapshotIndexRepository.strict_warm_start_evidence` from `services/orchestrator/scheduler_core.py::_strict_warm_start_for_candidate`) and not from `hydro.state_snapshot`, a committed cutover SHALL publish one checkpoint entry per cloned `(M1, source, t*)` row into that index — carrying the same identity and lineage as the DB clone row (`model_id`, `source_id`, `valid_time`, `cycle_id`, `lead_hours`, `state_uri`, `checksum`, the `M1` `model_package_version` and `model_package_checksum`) — on the lifecycle post-commit tail, ordered BEFORE the Change 4 `publish_scheduler_registry_manifest` re-publish. If the index publish fails, the registry manifest SHALL NOT be re-published and the operation SHALL surface a recorded retry blocker, so node-22 never observes an `M1`-active registry manifest without `M1`'s successor checkpoint in the index.

#### Scenario: Index publish precedes the manifest re-publish

- **WHEN** a cutover transaction commits with clone rows for its source scope
- **THEN** the cloned checkpoint entries are published into the scheduler file state index before `publish_scheduler_registry_manifest` re-emits the registry manifest
- **THEN** by the time node-22 first consumes an `M1`-active manifest, `strict_warm_start_evidence` for `(M1, source, t*)` resolves ready from the index.

#### Scenario: Index publish failure holds back the manifest and demands retry

- **WHEN** the post-commit index publish fails after the cutover transaction committed
- **THEN** `publish_scheduler_registry_manifest` is NOT invoked, the previously published manifest remains the compute-plane authority, and a recorded blocker demands an operator/automation retry of the publish tail
- **THEN** node-22 is never routed to `M1` while the index lacks `M1`'s successor checkpoint.

#### Scenario: Approved cold-start sources publish no checkpoint entry

- **WHEN** a cutover commits with a cold-start approval covering a source (so that source has no clone row)
- **THEN** no checkpoint entry is fabricated for that source; the index publish covers exactly the cloned rows
- **THEN** the first cold cycle for the approved source is governed by the recorded approval, not by a synthetic index entry.

