## ADDED Requirements

### Requirement: The run-list cache key folds source case like the store does

The display cache key for `GET /api/v1/runs` SHALL lowercase the `source`
dimension, because the store compares `source` case-insensitively. The value
passed to the store and the response body SHALL be unchanged. The `basin_id`
and `status` dimensions, which the store matches exactly, SHALL stay
case-sensitive. An absent `source` SHALL remain distinct from the literal value
`None`.

#### Scenario: the same run list is requested with three source spellings

- **WHEN** a display client requests `/api/v1/runs?source=GFS`, `?source=gfs`
  and `?source=Gfs` in turn
- **THEN** exactly one cache entry is created, the store loader runs once, and
  the three response bodies are identical.
