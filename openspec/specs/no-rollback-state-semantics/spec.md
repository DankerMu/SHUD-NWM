# no-rollback-state-semantics Specification

## Purpose
TBD - created by archiving change mapping-variant-state-compatibility. Update Purpose after archive.
## Requirements
### Requirement: No reverse state clone to a legacy-mapping model

Direct-grid is a one-way channel (user explicit decision, grill 2026-07-09). The clone mechanism SHALL NOT perform any reverse clone onto a legacy-mapping model: there SHALL be no code path that clones a direct-grid state row back onto a legacy-mapping model. Direction SHALL be classified with Change 4's single classifier (pinned by `source-specific-model-variant-routing`'s `legacy-reactivation-guard`): a model classifies as direct-grid if and only if its `resource_profile.direct_grid_forcing` parses successfully through `workers/forcing_producer/direct_grid_contract.py:load_forcing_mapping_contract_from_manifest` with `forcing_mapping_mode='direct_grid'`; a model whose contract is absent, malformed, or non-direct classifies as legacy-mapping (fail-closed). The guard SHALL be enforced at the clone function's own signature (source model row, target model row) as defense-in-depth inside the Change 4 pre-activation hook, so a reverse request is refused regardless of how the clone is reached. Only forward state transfer is permitted.

#### Scenario: Reverse clone targeting a legacy model is refused

- **WHEN** a request attempts to clone a direct-grid `(M1, source, t*)` state back onto a model that classifies as legacy-mapping (`M0`)
- **THEN** the request is refused fail-closed with no state row written
- **THEN** the refusal is recorded and there is no override path.

#### Scenario: A malformed-contract target classifies as legacy-mapping and is refused

- **WHEN** a clone request's target model declares `forcing_mapping_mode='direct_grid'` but its `resource_profile.direct_grid_forcing` fails the contract parser (e.g. missing `binding_checksum` or malformed `station_bindings`)
- **THEN** the target classifies as legacy-mapping (fail-closed) and the clone is refused with no row written
- **THEN** the classification mirrors Change 4's single-classifier behavior, so the clone guard and the legacy-reactivation guard can never disagree about a model's class.

#### Scenario: Forward-only transfer remains available

- **WHEN** state transfer is requested from a legacy or prior model onto a newer direct-grid variant with an equal `hydrologic_core_fingerprint`
- **THEN** the forward clone proceeds through the fingerprint gate
- **THEN** the mechanism exposes no symmetric reverse operation.

### Requirement: Fix-forward state continuity routes by fingerprint

A fix-forward `M1→M1′` SHALL reuse the same `hydrologic_core_fingerprint` gate. When the fix only changes `FORC`/binding the fingerprint is unchanged and the clone SHALL proceed; when the fingerprint differs the candidate is NOT a fix-forward but a new model, and it SHALL be routed to the explicit cold-start approval route defined by `atomic-cutover-transaction` instead of a clone (§11.3).

#### Scenario: Fingerprint-equal fix-forward clones through the same gate

- **WHEN** a fix-forward rebuilds `M1` into `M1′` changing only `FORC`/binding, leaving the `hydrologic_core_fingerprint` equal
- **THEN** the state clone `(M1, source, t*) → (M1′, source, t*)` proceeds through the same fingerprint gate
- **THEN** the `cloned_from` provenance records `M1` as the source and the gating fingerprint value.

#### Scenario: Fingerprint-unequal candidate is routed to cold-start plus approval

- **WHEN** a candidate labeled fix-forward has a `hydrologic_core_fingerprint` that differs from the active model's
- **THEN** the mechanism refuses to clone, classifies the candidate as a new model, rolls back the activation, and surfaces the stable error code `state_clone_cold_start_approval_required` plus an `ops.audit_log` record naming the blocked scope
- **THEN** only an activation carrying an explicit cold-start approval input (per `atomic-cutover-transaction`) commits without a clone row, with the spin-up-distortion-announcement obligation recorded (§11.3).

