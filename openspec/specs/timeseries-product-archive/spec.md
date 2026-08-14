# timeseries-product-archive Specification

## Purpose
TBD - created by archiving change audit-producer-window-containment. Update Purpose after archive.
## Requirements
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

