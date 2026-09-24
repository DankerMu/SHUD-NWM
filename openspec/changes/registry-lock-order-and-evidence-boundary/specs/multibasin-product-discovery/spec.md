## ADDED Requirements

### Requirement: Evidence-only basins stay out of public basin discovery

`GET /api/v1/basins` SHALL exclude every `core.basin` row whose `basin_group` is
`evidence-only`, with and without `has_display_product`, applying the exclusion
before pagination, and SHALL keep rows whose `basin_group` is NULL or any other
value. `GET /api/v1/basins/{basin_id}/versions` SHALL answer 404 for an
evidence-only basin.

#### Scenario: an evidence fixture row is present in core.basin

- **WHEN** `core.basin` contains a row with `basin_group = 'evidence-only'`
- **THEN** neither basin list mode returns it, page sizes count only non-evidence
  rows, and its versions endpoint returns 404.
