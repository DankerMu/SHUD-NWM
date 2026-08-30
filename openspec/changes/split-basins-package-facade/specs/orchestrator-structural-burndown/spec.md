## ADDED Requirements

### Requirement: Basins package facade split preserves runtime and artifact contracts

The repository SHALL split `workers.model_registry.basins_package` into the six
responsibility owners defined by this change. The historical facade and every new
owner MUST remain below 1,000 lines, while `.large-file-guard.json` remains
byte-identical. Every pre-split facade attribute and callable signature MUST remain
available: eight callable seams use runtime facade forwarding, other private helpers
use plain re-export, and object/class attribute patches use the same imported module
or class objects. Public entrypoints, source/package identity, manifest and package
bytes/checksums, object-store effects, forcing/calibration policy, typed failures,
idempotency, and cleanup MUST remain equivalent.

#### Scenario: existing callers import and publish through the facade

- **WHEN** an existing CLI, reingest, registry, QHH, production-validation, scheduler,
  or test caller imports the historical module and publishes or plans a valid model
- **THEN** its imports/signatures/types resolve without a cycle
- **AND** source identity, package manifest/bytes/checksums, object keys/effects,
  forcing/calibration material, and success payload are identical to baseline.

#### Scenario: a historical dynamic dependency is patched

- **WHEN** a test patches one of the eight governed facade callables and invokes the
  existing high-level path that consumes it
- **THEN** its direct/transitive wrapper forwards the facade's current runtime
  binding rather than a stale leaf-local callable
- **AND** when a test patches a governed `os` or class attribute, the leaf observes
  the same shared module/class object, while coordinated limit values observe the
  facade binding
- **AND** the existing failure/race/limit oracle produces its baseline result.

#### Scenario: invalid and stale publication paths retain failure behavior

- **WHEN** existing malformed, symlink, ancestor-race, stale source, conflict, lock,
  verification, or output-write failure fixtures are run through the facade
- **THEN** the same typed error/details occur before the same partial-output boundary
- **AND** lock/temp/object/local cleanup and prior immutable objects remain equivalent.

#### Scenario: structural guard evaluates the finite owner set

- **WHEN** the six production files and guard configuration are evaluated
- **THEN** each file is strictly below 1,000 lines, no seventh owner or replacement
  exclusion exists, the guard digest is unchanged, and an ordinary verified commit
  passes the hook.
