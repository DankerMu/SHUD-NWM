## REMOVED Requirements

### Requirement: State clone executes in the same transaction as model activation

**Reason**: Its last scenario orders the clone against the station-flag flip hook, which #3164 retires. Every other scenario is unchanged.

**Migration**: Replaced by the requirement "State clone commits atomically with model activation", which keeps the remaining obligations unchanged.

## ADDED Requirements

### Requirement: State clone commits atomically with model activation

The state clone SHALL run as a hook mounted on the ordered pre-activation extension point of the `variant-activation-cutover` capability defined by change `source-specific-model-variant-routing` (Change 4, committed contract: hooks run inside the lifecycle transaction, each receiving the transaction cursor and the activation context — `basin_version_id`, previous active model, target model, source scope), and SHALL execute inside the SAME database transaction that marks `M1` active and the previous active model `M0` `superseded`. When the clone step is engaged (per this capability's applicability requirement), any failure in it — fingerprint-gate refusal, invalid gate inputs, a `(model_id, COALESCE(source_id, ''), valid_time)` unique-key conflict, or a missing/stale qualified source snapshot for a source in scope not covered by an approved cold-start input — SHALL cause the whole transaction to roll back. This capability is a CONSUMER of the Change 4 extension-point contract and SHALL NOT define the extension point, the activation transaction, or the hook ordering itself.

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
