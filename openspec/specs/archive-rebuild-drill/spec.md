# archive-rebuild-drill Specification

## Purpose
TBD - created by archiving change retention-drill-salvage-window-scope. Update Purpose after archive.
## Requirements
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

