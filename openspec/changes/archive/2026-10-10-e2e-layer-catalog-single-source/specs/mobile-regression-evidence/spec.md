## ADDED Requirements

### Requirement: Broad-route layer-catalogue fixtures SHALL come from one source

The layer-catalogue entries that the mocked regression lane serves through its broad catalogue routes — the minimal discharge entry, the discharge entry with extended metadata, the precipitation entry and their combined list — SHALL each be defined once in a shared e2e support module, and specs and support files SHALL import them instead of restating the literals. The river-window fixtures, which serve their own discharge layer with tile templates and valid times, are outside this requirement.

#### Scenario: A broad-route catalogue entry changes

- **WHEN** a field of the minimal discharge entry, the extended discharge entry or the precipitation entry has to change
- **THEN** it is changed in one support module and every mocked spec that serves that entry through a broad catalogue route receives the new value
