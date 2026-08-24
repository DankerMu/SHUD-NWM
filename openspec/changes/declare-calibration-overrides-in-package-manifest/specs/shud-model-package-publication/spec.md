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
declaration claims otherwise, and only a published basin can tell that lie. Keying it on
*declared but not published* would make every narrowed publish — a `--basin-slug` run — fail on
a declaration that is doing nothing wrong, which would in turn push operators toward not
loading the declaration at all.

That leniency stops at the discovered inventory. A declared basin that the run *discovers but
does not select* is doing nothing wrong; a declared basin that the discovered inventory does
not contain **at all** is a typo or a stale rename in checked-in configuration, and the
declaration will never bite again, with no signal. The two MUST NOT be reported under one
indistinguishable value.

#### Scenario: A declared basin that the discovered inventory does not contain refuses

- **WHEN** the declaration names a basin slug that appears nowhere in the discovered Basins
  inventory
- **THEN** the publish fails with a diagnosable error whose code is distinct from every other
  override refusal and which names the offending entry
- **AND** no package is published in that run, on `--dry-run` too
- **AND** the previous scheduler registry generation stays live

A bad slug in checked-in configuration is a broken deploy, so it must fail at publish time. The
refusal is fail-safe — nothing is committed — and the manual publisher CLI reaches it long
before the unattended timer does. Silently reverting one basin to a calibration that makes SHUD
produce NaN is strictly worse than a loud, diagnosable, non-committing failure.

#### Scenario: A declared basin discovered but not selected for this run is reported, not refused

- **WHEN** the declaration names a basin that the run discovers but does not publish — narrowed
  out by `--basin-slug`, or discovered and not publishable
- **THEN** the run records that entry as not applied, under a value distinct from the
  inventory-absent refusal
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

#### Scenario: The refresh lane's receipt names the offending entry

- **WHEN** the scheduler file-provider refresh cannot load or apply a declared override
- **THEN** its receipt carries a refusal reason distinct from the generic provider-invalid
  reason, together with the override error code and the offending entry
- **AND** nothing is committed and the previous registry generation stays live

The refresh lane is unattended and retries every tick, and it persists no publisher summary. A
bad declaration that surfaced only as the generic reason a dozen unrelated causes already emit
would recur silently every tick while the scheduler ran on an ever-staler registry.

#### Scenario: The refresh lane's receipt records declared entries that did not apply

- **WHEN** a refresh run publishes successfully and the declaration names a basin it did not
  publish
- **THEN** its receipt records that entry as not applied, with the same reason value the
  publisher summary uses

#### Scenario: Recording an override re-derives the model identity

- **WHEN** a package is published with a calibration override applied
- **THEN** its `package_checksum` differs from the same package published without the
  override
- **AND** the derived `model_id` differs accordingly
