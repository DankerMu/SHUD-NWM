# Spec Delta: timeseries-db-retention

## ADDED Requirements

### Requirement: The drop_chunks identity guard MUST have negative-direction regression coverage

The H3 identity guard in the per-chunk drop driver SHALL be pinned by unit
tests that exercise its failure directions against the real function (fake
cursor, no DB): a zero-row `drop_chunks` result (chunk vanished mid-tick),
a mismatched returned identity (server dropped a different chunk), and a
multi-row result containing the selected chunk alongside extras (server
dropped MORE than the selected chunk), each raising the guard's RuntimeError
with the selected chunk's qualified name in the message. Deleting or
weakening the guard SHALL fail the retention test suite — including
cardinality-relaxing weakenings (membership or first-row checks) that would
accept an extra-chunk drop as success.

#### Scenario: Zero-row drop result raises

- **WHEN** `_default_drop_chunk` runs against a cursor whose `fetchall()`
  returns no rows
- **THEN** it SHALL raise RuntimeError matching `expected exact selected
  chunk` and naming the selected chunk's qualified name

#### Scenario: Mismatched dropped identity raises

- **WHEN** the cursor reports a dropped chunk name different from the
  selected chunk's qualified name
- **THEN** it SHALL raise the same RuntimeError shape, preserving the
  diagnostic that names both the returned list and the expected chunk

#### Scenario: Extra chunk dropped alongside the selected chunk raises

- **WHEN** the cursor reports the selected chunk's qualified name plus at
  least one additional dropped chunk name
- **THEN** it SHALL raise the same RuntimeError shape — cardinality binds:
  a superset containing the selected chunk is NOT success
