## ADDED Requirements

### Requirement: Every pre-commit reason has stable owners

The replay tool SHALL map every `_PRE_COMMIT_INDEX_REASONS` member to one or
more owning function names and SHALL NOT use mutable source line numbers as the
ownership identity.

#### Scenario: Ownership key set is complete

- **WHEN** focused tests compare the runtime pre-commit reason set with ownership metadata
- **THEN** the key sets are exactly equal
- **AND** every owner tuple is non-empty

#### Scenario: Multi-owner reasons preserve every raise-point owner

- **WHEN** focused tests inspect the known reasons emitted by multiple merge-path functions
- **THEN** all seven multi-owner reason sets equal their expected function sets
- **AND** no row is compressed to a single representative owner

#### Scenario: Named owners contain the reason literal

- **WHEN** focused tests inspect each mapped function in `packages/common/state_manager.py`
- **THEN** that function body contains the corresponding reason literal

#### Scenario: Unrelated source insertion does not stale ownership

- **WHEN** unrelated lines are inserted before a mapped function
- **THEN** the function-name ownership remains valid without updating numeric citations

### Requirement: Replay runtime behavior remains unchanged

The metadata repair SHALL preserve the runtime reason set, allowlist,
classification, exit status, and receipt behavior.

#### Scenario: Existing replay regressions remain compatible

- **WHEN** the focused replay suite exercises pre-commit refusal and commit-uncertain outcomes
- **THEN** existing classification, exit-code, and receipt assertions continue to pass
