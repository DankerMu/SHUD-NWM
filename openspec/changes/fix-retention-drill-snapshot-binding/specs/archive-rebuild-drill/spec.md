# Spec Delta: archive-rebuild-drill

## ADDED Requirements

### Requirement: A derivation-mode drill MUST record its completeness snapshot's db-export universe

A derivation-mode drill (`--completeness-receipt`) SHALL record in
`salvage_derivation` the field `db_export_windows` — the normalized
`{start, end}` windows of ALL subjects with `coverage == "db-export"` AND
`verdict == "complete"` in the consumed completeness receipt, unfiltered
by any drop window, deduplicated by exact pair and sorted ascending (the
same normalization as the retention gate's
`derive_salvage_backed_windows`) — plus `completeness_generated_at` equal
to the consumed receipt's `generated_at`; explicit-manifest drills SHALL
continue to omit the `salvage_derivation` section entirely, and the
receipt SHALL validate against
`schemas/archive_rebuild_drill_receipt.schema.json`.

#### Scenario: Universe recorded unfiltered and schema-valid

- **WHEN** a derivation-mode drill runs with a narrowed drop window
  against a completeness receipt containing db-export/complete subjects,
  including one whose window does not overlap that drop window
- **THEN** `salvage_derivation.db_export_windows` SHALL contain the
  normalized windows of ALL db-export/complete subjects (including the
  non-overlapping one), `completeness_generated_at` SHALL equal the
  consumed receipt's `generated_at`, and the receipt SHALL be
  schema-valid

#### Scenario: Explicit-manifest drills are unchanged

- **WHEN** a drill runs with `--salvage-manifest` and no
  `--completeness-receipt`
- **THEN** the receipt SHALL omit `salvage_derivation` entirely, exactly
  as before this change
