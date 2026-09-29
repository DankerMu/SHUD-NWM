## ADDED Requirements

### Requirement: Default basin catalog differences from the scheduler manifest are machine-explained
The default `GET /api/v1/basins` catalog SHALL remain the raw `core.basin` directory (excluding `evidence-only` rows), including retired basins, as already defined by `multibasin-product-discovery` (default parameters stay backward compatible). The repository SHALL provide a read-only audit, `scripts/basin_catalog_manifest_audit.py`, that computes the default and `has_display_product=true` basin sets through the production `list_basins` implementation and compares them with the node-22 scheduler manifest's `models[].basin_id` set. The audit SHALL pass only when the display set equals the manifest set and every basin in the default set but not in the manifest has zero active `core.model_instance` rows. A violation SHALL exit 1 with a JSON receipt naming each offending basin; a missing, unreadable or empty manifest SHALL exit 2 and never report a pass. During a basin onboarding window the display set may transiently differ from the manifest; the audit reports that as exit 1 with both difference sets rather than suppressing it.

#### Scenario: retired basins in the default catalog are explained
- **WHEN** the default catalog contains basins absent from the manifest and each of them has no active model instance
- **AND** the display set equals the manifest set
- **THEN** the audit exits 0 and lists each extra basin with `active_models: 0`

#### Scenario: an active basin missing from the manifest is a violation
- **WHEN** a basin in the default catalog is absent from the manifest but still has an active model instance
- **THEN** the audit exits 1 and names that basin as a violation

#### Scenario: display and manifest drift is a violation
- **WHEN** the display set and the manifest set differ in either direction
- **THEN** the audit exits 1 and reports both difference sets

#### Scenario: unusable manifest fails closed
- **WHEN** the manifest path is missing, unparseable, or yields no basin ids
- **THEN** the audit exits 2 without reporting a verdict of pass
