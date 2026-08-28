## ADDED Requirements

### Requirement: Scheduler evidence root discovery admits only governed pass artifacts

Readiness root discovery and scheduler pass-evidence retention SHALL use one shared pass filename contract. A discovered pass artifact SHALL have the governed `scheduler_` prefix and an accepted pass JSON suffix; non-pass state such as `no-progress-tracker.json`, temporary files, and unrelated top-level JSON SHALL NOT become readiness items, consume the bounded discovery cap, or enter pass-retention deletion selection. Runtime pass writers SHALL continue to emit names accepted by this predicate. Explicit single-file readiness input remains operator-selected and is outside root discovery.

#### Scenario: Shared root excludes the permanent no-progress tracker

- **WHEN** the configured scheduler evidence root contains `no-progress-tracker.json` beside valid passed and stable-blocked `scheduler_*.json` pass artifacts
- **THEN** readiness root discovery returns and validates only the pass artifacts
- **AND** the tracker creates no false blocked item and consumes none of the discovery cap

#### Scenario: Temporary and unrelated JSON files do not consume the cap

- **WHEN** a root contains temporary files, unprefixed JSON documents, and more governed pass artifacts than the configured discovery limit
- **THEN** filtering occurs before ordering and capping, so the cap is filled only by governed pass artifacts in the existing deterministic order

#### Scenario: Readiness and retention classify the same names

- **WHEN** the same set of governed pass, pre-execution pass, tracker, temporary, and unrelated JSON names is presented to readiness discovery and pass-evidence retention
- **THEN** both consumers agree on which names are scheduler pass artifacts, while each retains its existing operation-specific behavior after classification

#### Scenario: Explicit single-file input remains available

- **WHEN** an operator supplies one scheduler evidence file explicitly instead of a root
- **THEN** readiness validates that selected file through the existing content contract without applying root-discovery filename filtering
