# timeseries-db-retention Specification

## Purpose
TBD - created by archiving change fix-retention-freed-bytes-compressed. Update Purpose after archive.
## Requirements
### Requirement: freed_bytes accounting MUST be compression-aware

The retention receipt's per-chunk `freed_bytes` SHALL report the total bytes
reclaimed by dropping the chunk, including the compressed sibling relation's
bytes when the chunk is compressed, measured BEFORE the drop (H4 ordering
preserved) via a compression-aware size source
(`chunks_detailed_size(<hypertable>)` filtered to the chunk). Per-chunk
measurement failures and empty results SHALL keep the existing best-effort
semantics: record `0` for that chunk, continue measuring the rest, and
never block the drop phase.

#### Scenario: Compressed chunk reports compression-inclusive bytes

- **WHEN** an eligible compressed chunk is measured before drop
- **THEN** the recorded `freed_bytes` SHALL equal the chunk's
  `chunks_detailed_size.total_bytes` (main + compressed sibling + indexes),
  not the main relation's bytes alone

#### Scenario: Measurement failure stays best-effort

- **WHEN** the per-chunk size query raises or returns no row for a chunk
- **THEN** that chunk's `freed_bytes` SHALL be recorded as `0`, the
  remaining chunks SHALL still be measured on fresh connections, and the
  drop phase SHALL proceed unchanged

#### Scenario: Historical receipts are immutable evidence

- **WHEN** the measurement fix lands
- **THEN** previously generated retention receipts (including the
  2026-07-25 first-enforce receipt with the known under-report) SHALL
  remain byte-unchanged, with the discrepancy documented in the receipts
  README rather than rewritten

