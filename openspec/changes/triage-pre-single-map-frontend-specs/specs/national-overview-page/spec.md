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

**Migration**: none; the summary cards were dropped. The ops entry is stated by `mobile-map-overlay-layout` "The ops entry joins the launcher column" and `ops-display-downgrade`.

### Requirement: Overview handles degraded data states

**Reason**: it placed the states in a left panel, basin rows and summary cards that do not exist, and prescribed a retry control that does not exist.

**Migration**: "Overview reports missing data in floating notices" below; malformed payloads are stated by `frontend-overview-data-shape-guards`.

## ADDED Requirements

### Requirement: Overview reports missing data in floating notices

The overview (`apps/frontend/src/pages/OverviewPage.tsx`, `apps/frontend/src/components/map/m11MapRuntime.tsx`) SHALL report
missing or failed data in notices floating over the map, and SHALL keep rendering the map while a notice is shown. No
retry control is offered. One floating notice is shown at a time; a status notice of the met-station layer takes
precedence over the empty-inventory notice.

#### Scenario: No basin inventory

- **WHEN** the overview data has settled, the basin inventory is empty or the overview bootstrap failed, and no met-station layer status notice is shown
- **THEN** a floating notice (`m11-overview-empty`) states the reason
- **AND** the map is still rendered

#### Scenario: A business-layer map source fails

- **WHEN** the map reports an error that does not come from a basemap source and whose message does not mention `glyphs`
- **THEN** a map status notice (`m11-map-source-error`) shows the message, or `地图源加载失败，受影响图层暂不可用。` when it is empty
- **AND** the map is still rendered
