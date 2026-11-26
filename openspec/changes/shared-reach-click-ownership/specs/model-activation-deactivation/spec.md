## ADDED Requirements

### Requirement: The model list SHALL carry the segment count of each model's river network

Every item of `GET /api/v1/models` SHALL carry `segment_count` equal to
`core.river_network_version.segment_count` of the item's `river_network_version_id`, the same value
`GET /api/v1/models/{model_id}` returns for that model. Which models are listed, their order, `total`,
`limit`, `offset` and every other field of an item SHALL be unchanged by this; the count query SHALL
count the same rows the page query returns. The overview derives its per-network segment counts from
this list, so a list without the value leaves the map's shared-reach rule on its fallback.

#### Scenario: A listed model carries its network's segment count

- **WHEN** a model is registered on a river network whose `segment_count` is 2 and
  `GET /api/v1/models?active=all` is requested
- **THEN** the model's item has `segment_count` 2
- **AND** `GET /api/v1/models/{model_id}` returns the same `segment_count`

#### Scenario: The list is otherwise unchanged

- **WHEN** the list is requested with and without `basin_version_id` and with each value of `active`
- **THEN** the items, their order and `total` are the ones returned before this requirement, with
  `segment_count` filled in

#### Scenario: A real-database test pins the value

- **WHEN** the CI `real-db-integration` job runs
- **THEN** a test under `pytest.mark.integration` seeds a model on a network with a known
  `segment_count`, requests `GET /api/v1/models?active=all` through the FastAPI TestClient and asserts
  the seeded model's item carries that count
