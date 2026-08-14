# timeseries-product-archive delta（archive-lane-retirement，#1370）

## ADDED Requirements

### Requirement: The product-archive mover capability is permanently retired

The product-archive mover SHALL NOT exist as a runnable capability: the
mover script, its wrapper, its manifest/receipt schemas, its systemd
units, its env template, and its tests are removed from the repository
(ADR 0002 Revision 2026-08-11, #1370). Historical mover receipts under
`docs/runbooks/receipts/tier-node27-timeseries-storage/product-archive/`
remain as immutable evidence.

#### Scenario: No mover components remain in the repository

- **WHEN** the repository is searched for
  `scripts/node27_product_archive.py`, its `_once.sh` wrapper, the
  `nhms-node27-product-archive.{service,timer}` units, and the
  `product_archive_manifest`/`product_archive_receipt` schemas
- **THEN** none SHALL exist, and no production code SHALL reference them

## REMOVED Requirements

### Requirement: Producer provenance window verification SHALL use containment semantics

**Reason**: the product-archive mover is permanently retired with the
archive lane (ADR 0002 Revision 2026-08-11, #1370); the mover script,
its manifest/receipt schemas, systemd units, and tests are removed.

### Requirement: Archive min-age validation MUST compare against the live DB retention window

**Reason**: retired with the archive lane (#1370); the mover's min-age
validation no longer exists. The DB retention window itself remains
governed by the timeseries-db-retention capability.
