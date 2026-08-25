# scheduler-registry-refresh Specification Delta

## ADDED Requirements

### Requirement: A calibration-override refusal SHALL reach the refresh receipt with its own reason and its offending entry

A refresh run that cannot load or apply a declared calibration override SHALL persist a receipt
whose `reason`/`operation_reason` is the distinct token `calibration_override_invalid`, not the
generic `provider_invalid` that a dozen unrelated causes already emit, and SHALL carry the
override's own error code, message and offending entries in a top-level optional
`calibration_overrides` key. That key SHALL also record the declared entries that the run did
not apply, because this lane persists no publisher summary and would otherwise leave that fact
no trace at all.

The receipt JSON Schema and the runtime receipt validator SHALL admit exactly the block's shape
— `declaration_path` (string or null), `not_applied` (entries of `basin_slug`, `parameter` and
a `reason_not_applied` drawn from the publisher's vocabulary) and an optional `error`
(`error_code`, `message`, `entries` of `basin_slug`/`parameter`) — and SHALL reject additional
or malformed fields over the same corpus. Runs that fail before the declaration is reached SHALL
omit the key entirely rather than persist a null placeholder; on a receipt whose `reason` is
`calibration_override_invalid`, both the schema and the runtime validator SHALL require the key
AND its `error`, since the block is the entire content of that refusal. Every string entering
the block SHALL be bounded at the source, so an over-long declared field cannot make the receipt
itself unpublishable and destroy the diagnosability the block exists to provide.

The refusal is raised before any canonical provider replacement: nothing is committed, the
previous registry generation stays live, and the timer retries.

#### Scenario: A declaration the refresh cannot apply lands on the receipt by name

- **WHEN** a refresh run's publish fails because a declared calibration override cannot be
  loaded or applied
- **THEN** the persisted receipt's `reason` and `operation_reason` are `calibration_override_invalid`
- **AND** its `calibration_overrides.error` carries the override error code, the message, and the
  offending `basin_slug`/`parameter`
- **AND** the receipt validates against the receipt JSON Schema and the runtime validator
- **AND** the canonical registry keeps its previous generation

#### Scenario: A receipt claiming the calibration refusal without the block is rejected

- **WHEN** a receipt carries `reason="calibration_override_invalid"` but omits the
  `calibration_overrides` key, or carries the key without its `error`
- **THEN** both the receipt JSON Schema and the runtime receipt validator reject it

#### Scenario: A successful refresh records the declared entries it did not apply

- **WHEN** a refresh run publishes successfully and the declaration names a basin the run did
  not publish
- **THEN** the persisted receipt's `calibration_overrides.not_applied` names that entry with the
  same reason value the publisher summary uses
- **AND** the receipt carries no `calibration_overrides.error`
