## ADDED Requirements

### Requirement: The compression runner's chunk identifier helper MUST fail closed

`scripts/node27_timeseries_compression.py` SHALL expose the qualified chunk name only through a helper that validates both parts against `^[A-Za-z0-9_]+$` (byte-identical to the autopipeline `_STATS_GUARD_IDENT_RE`, pinned by a test) and raises `ValueError` on mismatch. The runner itself issues no statement that interpolates a chunk name — every chunk reference is a bound `%s::regclass` parameter — so the helper has no production consumer today; it exists so that any future interpolation site (an `ANALYZE`, for example) inherits a fail-closed identifier by construction rather than by review.

#### Scenario: Malformed chunk name

- **WHEN** a catalog row yields a chunk name or schema containing `"`, `;`, whitespace, or an empty string
- **THEN** `qualified_chunk` raises `ValueError` before returning any text

#### Scenario: Well-formed chunk name

- **WHEN** the chunk name is `_hyper_3_8_chunk` in schema `_timescaledb_internal`
- **THEN** the qualified name is produced unchanged, and no statement in the module interpolates it (a repo test asserts the module carries no f-string SQL naming a chunk)
