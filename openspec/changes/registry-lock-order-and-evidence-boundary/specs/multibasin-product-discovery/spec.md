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

### Requirement: Models under evidence-only basins stay out of public model discovery

`GET /api/v1/models` SHALL exclude every model whose basin has `basin_group =
'evidence-only'` in every `active` mode (`true`, `false`, `all`), with the total
count and the page items computed under the same predicate. `GET
/api/v1/models/{model_id}` SHALL answer 404 for such a model. The internal
lookup used by scheduler and lifecycle code (`get_model_internal`) SHALL NOT
apply this exclusion; scheduler discovery through `list_models(active=True)`
applies it by design.

#### Scenario: an evidence model exists under an evidence-only basin

- **WHEN** `core.model_instance` has a model whose basin is evidence-only
- **THEN** no `/models` mode lists or counts it, `/models/{model_id}` returns
  404, and `get_model_internal` still returns the row.
