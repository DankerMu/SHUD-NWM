# national-overview-page

## REMOVED Requirements

### Requirement: National overview is the default operational entry

**Reason**: the page has no global navigation, side panels or three-column layout; `single-map-shell-routing` forbids the top navigation this requirement prescribed.

**Migration**: `single-map-shell-routing` (the single map entry and its redirects), `map-first-layout-conformance` and `mobile-viewport-shell` (header, control bar, forms).

### Requirement: Basin tree controls national map visibility

**Reason**: no basin tree exists; every basin is always visible.

**Migration**: none; the behaviour was dropped.

### Requirement: Right-side summaries provide operational links

**Reason**: no summary card is rendered; the only operations entry is the ops link.

**Migration**: `single-map-shell-routing` states the ops entry.

### Requirement: Overview handles degraded data states

**Reason**: it placed the states in a left panel, basin rows and summary cards that do not exist, and prescribed a retry control that does not exist.

**Migration**: "Overview reports missing data in floating notices" below; malformed payloads are stated by `frontend-overview-data-shape-guards`.

## ADDED Requirements

### Requirement: Overview reports missing data in floating notices

The overview (`apps/frontend/src/pages/OverviewPage.tsx`, `apps/frontend/src/components/map/m11MapRuntime.tsx`) SHALL report
missing or failed data in notices floating over the map, and the map and its controls SHALL stay usable while a notice is
shown. No retry control is offered.

#### Scenario: No basin inventory

- **WHEN** the overview data has settled and the basin inventory is empty or the overview bootstrap failed
- **THEN** a floating notice (`m11-overview-empty`) states the reason
- **AND** the map is still rendered

#### Scenario: A business-layer map source fails

- **WHEN** the map reports an error for a source that is not a basemap source
- **THEN** a map status notice (`m11-map-source-error`) shows the error message
- **AND** the other layers and controls stay usable
