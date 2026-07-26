# timeseries-product-archive Specification

## Purpose
TBD - created by archiving change audit-producer-window-containment. Update Purpose after archive.
## Requirements
### Requirement: Producer provenance window verification SHALL use containment semantics

When the inventory audit binds an archived product's producer provenance to its DB inventory subject, identity fields (kind, stable subject identifier, producer-manifest path, model and basin-version identity) SHALL be compared by equality, while the producer window SHALL be verified by containment — the audit SHALL accept the subject when the producer window contains the DB subject window (producer start at or before the subject start AND producer end at or after the subject end) and SHALL block with a stable sanitized reason when the producer window does not contain the DB window, when a window value is missing or unparseable, or when any identity field differs.

#### Scenario: Superset producer window passes verification

- **WHEN** an archived product's producer window strictly contains the DB
  subject window (for example, the archive covers three additional hours
  beyond a DB row truncated by an ingest interruption)
- **THEN** producer provenance verification passes and the subject remains
  eligible for `product-archive` coverage classification

#### Scenario: Subset producer window blocks fail-closed

- **WHEN** an archived product's producer window ends before the DB
  subject window ends (the archive is missing coverage the DB holds)
- **THEN** the audit blocks the subject with a stable sanitized message
  identifying the subject, and the receipt outcome remains fail-closed

#### Scenario: Identity field mismatch blocks fail-closed

- **WHEN** any producer identity field (kind, subject identifier,
  manifest path, model, or basin-version identity) differs from the DB
  subject
- **THEN** the audit blocks the subject exactly as before this change

