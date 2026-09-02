## ADDED Requirements

### Requirement: The compression runner MUST refuse non-identifier chunk names before ANALYZE

`scripts/node27_timeseries_compression.py` SHALL validate both parts of a chunk's qualified name against `^[A-Za-z0-9_]+$` (byte-identical to the autopipeline `_STATS_GUARD_IDENT_RE`) before interpolating it into `ANALYZE`, raising `ValueError` on mismatch so no statement is issued for a malformed name.

#### Scenario: Malformed chunk name

- **WHEN** a catalog row yields a chunk name containing `"` or `;`
- **THEN** `qualified_chunk` raises `ValueError` and no cursor call is made

#### Scenario: Well-formed chunk name

- **WHEN** the chunk name is `_hyper_3_8_chunk` in schema `_timescaledb_internal`
- **THEN** the qualified name is produced unchanged and ANALYZE proceeds as before
