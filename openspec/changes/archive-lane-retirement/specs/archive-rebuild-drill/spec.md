# archive-rebuild-drill delta（archive-lane-retirement，#1370）

## ADDED Requirements

### Requirement: The archive rebuild drill capability is permanently retired

The archive rebuild drill SHALL NOT exist as a runnable capability: the
drill script, its wrapper, its receipt schema, its env template, and its
tests are removed from the repository (ADR 0002 Revision 2026-08-11,
#1370). Historical drill receipts under
`docs/runbooks/receipts/tier-node27-timeseries-storage/archive-rebuild-drill/`
remain as immutable evidence.

#### Scenario: No drill components remain in the repository

- **WHEN** the repository is searched for
  `scripts/node27_archive_rebuild_drill.py`, its `_once.sh` wrapper, and
  `schemas/archive_rebuild_drill_receipt.schema.json`
- **THEN** none SHALL exist, and no production code SHALL reference them

## REMOVED Requirements

### Requirement: db-export drill coverage is salvage-window-scoped

**Reason**: the archive rebuild drill is permanently retired with the
archive lane (ADR 0002 Revision 2026-08-11, #1370); the drill script,
its receipt schema, and all consumers are removed.

### Requirement: Salvage input derives from the completeness receipt

**Reason**: retired with the archive lane (#1370); neither the salvage
runner nor the completeness receipt exists after this change.

### Requirement: A derivation-mode drill MUST record its completeness snapshot's db-export universe

**Reason**: retired with the archive lane (#1370); derivation-mode
drills and the completeness snapshot no longer exist.
