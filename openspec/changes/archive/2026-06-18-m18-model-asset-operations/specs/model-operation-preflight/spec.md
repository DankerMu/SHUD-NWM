## ADDED Requirements

### Requirement: Model Operation Preflight
Model lifecycle operations SHALL run preflight checks before mutating active/deprecated state.

Preflight output SHALL include basin/version scope, candidate model id, current active model id, river network id, mesh id, package checksum, object URI prefix validation, copied-root-not-symlink evidence when applicable, downstream impact surfaces, blockers, warnings, and request id.

#### Scenario: Activation preflight passes
WHEN a candidate model has valid basin_version, river_network_version, mesh/checksum/package lineage, and no conflicting active state
THEN preflight returns an impact summary and allows activation to proceed.

#### Scenario: Incompatible model
WHEN a candidate model references missing or incompatible basin/river/mesh lineage
THEN preflight blocks mutation with stable error details.

#### Scenario: Unsafe package lineage
WHEN a candidate model points to a development symlink root, raw `data/Basins` runtime source, local `/volume` source path, invalid object URI prefix, or checksum that cannot be reread from stored package evidence
THEN preflight blocks mutation and no registry state changes.

#### Scenario: Operational basin without active model
WHEN deactivation would leave a required basin without an active model
THEN preflight blocks unless an explicitly authorized override policy is present.
