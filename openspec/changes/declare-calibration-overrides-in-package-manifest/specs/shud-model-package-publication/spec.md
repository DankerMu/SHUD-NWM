# shud-model-package-publication Specification Delta

## ADDED Requirements

### Requirement: Calibration is overridden only where a declaration names it, and the override travels in the package

A published package's calibration files MUST be a byte copy of their source unless a
checked-in declaration names that basin and that calibration parameter. Where a
declaration does name them, the publisher MUST apply the declared value on an isolated
staging copy, MUST leave the Basins source tree unwritten, and MUST record the applied
override in the package manifest so the record travels with the package rather than
living only in a publisher-workspace receipt.

#### Scenario: An undeclared basin publishes its calibration unchanged

- **WHEN** a basin is absent from the override declaration
- **THEN** every calibration file in its published package is byte-identical to the source
- **AND** the manifest records no calibration override for it

#### Scenario: A declared override is applied and recorded

- **WHEN** the declaration names a basin and one of its calibration parameters
- **THEN** the published package carries the declared value for that parameter
- **AND** every other calibration value in that package is byte-identical to the source
- **AND** the manifest records the parameter, the applied value, and the declared reason
- **AND** the basin's source tree is unchanged after publication

#### Scenario: A published basin whose declared override did not apply refuses

- **WHEN** a basin is being published, the declaration names it, and the declared override was
  not applied — the calibration parameter is absent from its calibration file, or the declared
  value cannot be parsed
- **THEN** the publish fails with a diagnosable error naming the offending entry
- **AND** no package is published for that basin

The refusal is keyed on *published but not applied*, not on *declared but not published*. The
lie this requirement prevents is a package that carries the original value while the
declaration claims otherwise, and only a published basin can tell that lie. Keying it the other
way would make every narrowed publish — a `--basin-slug` run, a test fixture, any tree that
legitimately does not contain the declared basin — fail on a declaration that is doing nothing
wrong, which would in turn push operators toward not loading the declaration at all.

#### Scenario: A declared basin outside the publish set is reported, not refused

- **WHEN** the declaration names a basin that the current run does not publish
- **THEN** the run records that entry as not applied in its summary
- **AND** the publish proceeds for every other basin

### Requirement: Every publishing lane loads the declaration by default

Both the manual publisher CLI and the scheduler file-provider refresh lane MUST load the
checked-in declaration without an operator having to name it. An override that only applies on
one lane is worse than no override: the other lane republishes the source value, re-derives the
original `model_id`, and silently reverts the registry to a model whose per-model artifacts
have since been rebuilt under the overridden identity.

#### Scenario: The refresh lane applies the same declaration as the manual publisher

- **WHEN** the scheduler file-provider refresh republishes a basin the declaration names
- **THEN** the published package carries the declared value
- **AND** its `package_checksum` equals the one the manual publisher produces for the same
  inputs

#### Scenario: Recording an override re-derives the model identity

- **WHEN** a package is published with a calibration override applied
- **THEN** its `package_checksum` differs from the same package published without the
  override
- **AND** the derived `model_id` differs accordingly
