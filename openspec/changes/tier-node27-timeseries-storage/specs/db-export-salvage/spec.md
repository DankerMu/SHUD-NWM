# db-export-salvage Specification (delta)

## ADDED Requirements

### Requirement: Salvage scope is audit-derived

The salvage exporter SHALL take its selector list (forcing versions, runs,
time windows) from the salvage selector list of the **archive-completeness
receipt** emitted by the inventory audit (see `timeseries-product-archive`),
which compares DB coverage against product presence in object-store and
archive; hardcoded date lists SHALL NOT be the scope source.

#### Scenario: DB-only forcing window enters scope

- **WHEN** the inventory audit finds forcing station series rows whose
  upstream cycle products exist in neither object-store nor archive
- **THEN** those selectors MUST appear in the archive-completeness receipt's
  salvage list and be consumed verbatim by the exporter

#### Scenario: Product-backed windows are excluded

- **WHEN** the audit finds a window whose products are present and
  checksum-verified in the archive
- **THEN** that window MUST NOT be exported by the salvage lane

### Requirement: Export format and provenance

For each selector, the exporter SHALL write `COPY`-produced `csv.zst` objects
plus a `manifest.json` under `NHMS_ARCHIVE_ROOT`, recording
`provenance: db-export`, the exact selector, exported row count, column list,
per-object sha256, and source database identity.

#### Scenario: Export parity with database

- **WHEN** a selector is exported
- **THEN** the manifest row count MUST equal the database row count for that
  selector at export time
- **AND** the manifest MUST mark `provenance: db-export` so the object is
  permanently distinguishable from product-derived archives

### Requirement: One-time receipted execution

Salvage runs SHALL default to dry-run, require an explicit enforce flag,
never delete database rows or products, be idempotent across re-runs
(verified existing objects are skipped), and produce a receipt committed to
the repository's runbook receipts.

#### Scenario: Re-run after partial completion

- **WHEN** the exporter re-runs after a partial prior run
- **THEN** selectors with verified existing objects MUST be skipped and only
  missing selectors exported

### Requirement: Restore path is a documented manual procedure

`db-export` salvage objects SHALL have no automated or steady-state restore
lane (per ADR 0002 decision 3, no parallel COPY-FROM restore lane is built);
the only restore path SHALL be the manual `COPY FROM` procedure documented
in the archive runbook. Accordingly, the archive rebuild drill verifies
these objects by checksum and manifest row-count parity, not by reingest,
and retention MAY drop salvage-covered windows only with this documented
manual recovery path in place.

#### Scenario: Salvaged window needs restoring after retention dropped it

- **WHEN** an operator must restore rows for a window whose only durable
  copy is verified `db-export` salvage objects
- **THEN** the restore MUST follow the runbook's manual `COPY FROM`
  procedure
- **AND** no pipeline code path MUST perform automated `csv.zst` import

#### Scenario: Drill verification of a salvage object

- **WHEN** the rebuild drill covers a `db-export` salvage object
- **THEN** it MUST verify the object's sha256 and decompressed per-selector
  row count against the salvage manifest instead of reingesting it
