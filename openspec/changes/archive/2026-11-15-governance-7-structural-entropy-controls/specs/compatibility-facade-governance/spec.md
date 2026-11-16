## ADDED Requirements

### Requirement: Compatibility facade inventory SHALL exist before facade reduction

Compatibility facades SHALL have an inventory before implementation PRs remove
or grow compatibility symbols.

#### Scenario: scheduler facade is inventoried

- **WHEN** `services/orchestrator/scheduler.py` retains compatibility exports,
  wrappers, or monkeypatch binding paths
- **THEN** the inventory SHALL record each governed symbol or export group,
  real owner module, known callers/tests, retention reason, removal condition,
  and verification command.

#### Scenario: chain facade is inventoried

- **WHEN** `services/orchestrator/chain.py` retains stage, manifest,
  reservation, retry, tile publisher, worker, or persistence compatibility
  surfaces
- **THEN** the inventory SHALL record each governed symbol or export group,
  real owner module, known callers/tests, retention reason, removal condition,
  caller migration path, and verification command.

### Requirement: Compatibility facades SHALL be growth-guarded

Compatibility facades MUST NOT accumulate new implementation logic or new
cross-domain imports without an explicit budget update.

#### Scenario: new re-export is proposed

- **WHEN** a PR adds a new compatibility re-export, monkeypatch alias, or private
  helper forwarding path to a governed facade
- **THEN** a guard test SHALL require the inventory to include the symbol, real
  owner, retention reason, and removal condition.

#### Scenario: new implementation is proposed inside facade

- **WHEN** a PR adds non-forwarding scheduler or chain implementation logic to a
  governed facade
- **THEN** the guard SHALL fail unless the PR documents why the owning extracted
  module cannot host the implementation and creates a follow-up issue.

#### Scenario: import-family budget grows

- **WHEN** a governed facade imports from a new top-level owner family or
  production domain
- **THEN** the guard SHALL require an inventory update and a justification that
  the import does not invert ownership.

### Requirement: Facade reduction SHALL preserve downstream compatibility

Removing compatibility surface SHALL be behavior-preserving for unchanged
callers until migration is complete.

#### Scenario: caller migration precedes removal

- **WHEN** a compatibility symbol is removed from a facade
- **THEN** all known callers/tests from the inventory SHALL be migrated to the
  real owner or explicitly descoped
- **AND** focused compatibility tests SHALL prove unchanged callers do not lose
  behavior.
