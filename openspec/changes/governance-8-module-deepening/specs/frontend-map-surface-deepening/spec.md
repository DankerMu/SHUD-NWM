## ADDED Requirements

### Requirement: M11 map surface interface remains stable
The frontend map extraction SHALL keep `M11MapLibreSurface`, exported helper
builders, overlay interaction shape, popup slot behavior, and display-readonly
evidence boundaries stable while implementation moves to focused modules.

#### Scenario: map surface renders after extraction
- **WHEN** the M11 map surface receives the same props as before extraction
- **THEN** rendered sources, layer IDs, selected/hovered data attributes,
  popup slot rendering, unavailable states, and map error behavior remain
  equivalent

### Requirement: Map responsibilities move by complete owner families
The M11 map extraction SHALL move pure GeoJSON/source builders, MapLibre
primitive renderers, interaction dispatch, camera/error state, and popup/
selection coordination by complete owner families rather than by arbitrary
line ranges.

#### Scenario: pure builders move
- **WHEN** basin, basin-river, selected-segment, national-river, or registered
  overlay builders move to helper modules
- **THEN** serialization budgets, geometry sanitization, MVT metadata checks,
  source keys, layer paint, unavailable reasons, and exported helper contracts
  remain unchanged

#### Scenario: interaction dispatch moves
- **WHEN** hover/click hit-testing moves to a controller or hook
- **THEN** the click priority remains station cluster/point before basin river
  before MVT hit before basin fill, and cursor/hover state remains unchanged

### Requirement: Frontend extraction preserves live versus mocked boundaries
The M11 map extraction SHALL preserve station-MVT scope separation, live
display evidence boundaries, and frontend API contract discipline. Each PR SHALL
update the structural disposition or scoped map ownership inventory with the new
owner module, retained surface, removal condition if any, and focused
verification command when ownership changes.

#### Scenario: station overlay remains GeoJSON-backed
- **WHEN** map surface internals are extracted before station-MVT backend
  closure
- **THEN** station overlay behavior remains the existing clustered GeoJSON
  behavior and no extraction claims station-MVT endpoint completion

#### Scenario: frontend verification runs
- **WHEN** a map surface extraction PR changes map helpers, primitives, or
  interaction code
- **THEN** focused map tests, popup tests, `pnpm test`, and `pnpm build` pass
  before merge

#### Scenario: popup and selection boundaries remain stable
- **WHEN** popup slot, curve-window placement, selected station data attributes,
  selected segment data attributes, or station-MVT separation is touched
- **THEN** the PR proves equivalent selection/popup behavior with focused tests
  and does not claim station-MVT backend completion
