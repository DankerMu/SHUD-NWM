# Fixed-station forcing production

Capability: `fixed-station-forcing-production`
Status: draft
Parent: prcp-unit-terminus-hardening

## ADDED Requirements

### Requirement: Producer output semantics are pinned to producer_version

The forcing producer SHALL pin its output-semantics surface — `OUTPUT_UNITS`, the
precipitation conversion branch (`mm/day` accepted as factor `1.0`, any other unit
rejected), and the `rn_shortwave_factor` default — to a deterministic regression
fingerprint bound to `producer_version`. Any change to those output semantics SHALL
flip the fingerprint and fail the guard test until the developer both bumps
`producer_version` and updates the pinned fingerprint in the same change.

#### Scenario: Changing output semantics forces a producer_version bump

- **WHEN** any of `OUTPUT_UNITS`, the precipitation conversion branch behavior, or the `rn_shortwave_factor` default is changed
- **THEN** the recomputed output-semantics fingerprint MUST differ from the pinned `EXPECTED_FINGERPRINT`
- **AND** the guard test MUST fail until both `producer_version` is bumped and `EXPECTED_FINGERPRINT` is updated.

#### Scenario: Unchanged semantics keep the gate green at the pinned version

- **WHEN** the producer output semantics are unchanged
- **THEN** the recomputed fingerprint MUST equal the pinned `EXPECTED_FINGERPRINT`
- **AND** `producer_version` MUST equal the pinned value (`m2.0`).

### Requirement: OUTPUT_UNITS and manifest-unit keysets stay in lockstep

The producer's `OUTPUT_UNITS` keyset SHALL equal the manifest's
`REQUIRED_FORCING_VARIABLES` keyset, and every required forcing variable SHALL map
to a non-empty manifest unit, so that adding an `OUTPUT_UNITS` key without wiring
its manifest unit is caught by a guard test.

#### Scenario: Keyset equality and non-empty manifest units

- **WHEN** the guard test compares `set(OUTPUT_UNITS)` with `set(REQUIRED_FORCING_VARIABLES)`
- **THEN** the two keysets MUST be equal
- **AND** `package_manifest_unit(v)` MUST return a non-empty string for every required forcing variable.
