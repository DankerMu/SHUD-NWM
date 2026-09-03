## ADDED Requirements

### Requirement: Every connection surface reachable from a deployed unit's entrypoint SHALL be attributed

For each node-27 deployed unit, every database connection site transitively reachable from the unit's entrypoint SHALL carry a component-level `fallback_application_name` that identifies its connection surface, and a static guard rooted at the unit's entrypoint registry SHALL fail when any import-reachable site is neither attributed nor pinned call-unreachable with a recorded reason. Inside `nhms-display-api.service` the surfaces are `nhms-display-api`, `nhms-api-pipeline`, `nhms-api-forecast`, `nhms-api-data-sources`, `nhms-api-best-available`, `nhms-api-models` and `nhms-api-state-snapshots`.

#### Scenario: Display unit backends are all named

- **WHEN** the display API has served at least one request on each registered router
- **THEN** no backend for `nhms_display_ro` in `pg_stat_activity` has an empty `application_name`
- **AND** connections from `pipeline.py` and from `hydro_display.py` carry different names

#### Scenario: An unregistered connect site turns the guard red

- **WHEN** a router reachable from `apps/api/route_registry.py` opens a connection without attribution
- **THEN** the unit-level guard fails and names the offending site

#### Scenario: Shared stores stay name-free by default

- **WHEN** a `packages/common` store is constructed without `application_name`
- **THEN** it connects exactly as before this change and hard-codes no name

### Requirement: Delegated modules SHALL expose a connect seam on every connection-opening function

Every function in an attributed delegated module that opens a database connection SHALL accept a keyword-only `connect` parameter through which the calling component injects its attributed connect; the static guard SHALL fail naming any such function without the seam.

#### Scenario: Second connect function escapes no longer

- **WHEN** a second connection-opening function without a `connect` seam is added to `packages/common/display_watermark.py` and called from a registered component
- **THEN** the guard fails naming that function

#### Scenario: Baseline stays green

- **WHEN** the repository is at its baseline
- **THEN** the guard passes and the existing unclassified-module check is unchanged
