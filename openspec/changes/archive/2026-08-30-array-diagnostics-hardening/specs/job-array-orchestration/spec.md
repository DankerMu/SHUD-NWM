## ADDED Requirements

### Requirement: Array accounting handles absent Slurm state fail-closed
The orchestrator array-accounting normalizer SHALL return only `succeeded`, `cancelled`, or `failed`. Empty or whitespace-only state SHALL map to `failed`, because `UNKNOWN` belongs to a different raw-state normalization contract and is not a valid task accounting status. Neither sacct rows nor gateway task payloads with absent state SHALL leak a bare `IndexError`.

#### Scenario: Empty sacct task state becomes a failed task
- **WHEN** a field-count-valid array sacct row such as `<master>_0||1:0` has an empty State field
- **THEN** parsing MUST produce a failed task record through the existing error-classification path
- **AND** no bare `IndexError` or other unclassified exception escapes

#### Scenario: Missing gateway task state becomes a failed task
- **WHEN** an array task payload has neither a usable `status` nor a `state`
- **THEN** coercion MUST produce a failed task record in the existing return domain
- **AND** no bare `IndexError` escapes

#### Scenario: Existing state vocabulary is preserved
- **WHEN** the normalizer receives `COMPLETED`, `CANCELLED`, or any other non-empty Slurm state, including suffix or annotation forms already supported
- **THEN** it MUST preserve the existing succeeded, cancelled, or failed mapping respectively
