# file-state-snapshot-index — delta (pin-env-override-state-lineage-blocks)

## ADDED Requirements

### Requirement: The warm-start env override SHALL NOT admit candidates blocked by state-lineage invariants

With `NHMS_REQUIRE_FORECAST_WARM_START=false` and a valid in-window cutover declaration, the §8 gate SHALL still block a candidate whose required predecessor checkpoint is absent from generation-matched state-index history (typed reason `state_snapshot_index_prior_checkpoint_missing_after_history`, transition decision `BLOCK_PREDECESSOR_PENDING`) and a candidate whose checkpoint at the expected predecessor identity key — `valid_time` equal to the candidate cycle, producing `cycle_id` of cycle minus the source cadence, matching `lead_hours` — carries a different generation token (typed reason `state_snapshot_index_generation_mismatch`, transition decision `BLOCK_WRONG_GENERATION`); the env override never bypasses state-lineage blocks.

#### Scenario: Env override does not admit a missing predecessor

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid and in-window, and generation-matched state-index
  history exists but holds no checkpoint at the expected predecessor
  identity key
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`
- **AND** the recorded transition decision is `BLOCK_PREDECESSOR_PENDING`
- **AND** no candidate is admitted

#### Scenario: Env override does not admit a wrong-generation checkpoint

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid and in-window, and the checkpoint at the expected
  predecessor identity key carries a generation token different from the
  candidate's
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_generation_mismatch`
- **AND** the recorded transition decision is `BLOCK_WRONG_GENERATION`
- **AND** no candidate is admitted
