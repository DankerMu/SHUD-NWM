# frontend-mvt-layer-consumption Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: Frontend MVT layer consumption
MapLibre hydrology layers SHALL consume vector tile sources for national rendering when layer metadata advertises MVT.

#### Scenario: Metadata-driven selection
WHEN layer metadata exposes `tile_format=mvt`, URL template, source-layer id, zoom/bounds, schema/version, and valid-time/source references
THEN frontend derives MapLibre vector source/layer configuration from that metadata instead of hard-coding hidden tile URLs

#### Scenario: MVT available
WHEN layer metadata has MVT template
THEN frontend registers vector source/layers and does not request full national GeoJSON

#### Scenario: MVT unavailable
WHEN only bounded GeoJSON compatibility is available
THEN frontend labels fallback mode and limits bbox/feature requests

#### Scenario: National MVT required but unavailable
WHEN user opens a national hydrology view and MVT metadata is unavailable
THEN frontend shows a truthful unavailable/release-blocking state instead of silently requesting full-national GeoJSON

#### Scenario: State compatibility
WHEN MVT source selection changes valid_time, run, layer, basin, or restored URL state
THEN MapLibre source identity and visible status update without breaking existing timeline/selection behavior

