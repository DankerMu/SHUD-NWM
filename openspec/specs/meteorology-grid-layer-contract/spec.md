# meteorology-grid-layer-contract Specification

## Purpose
TBD - created by archiving change m13-meteorology-products-ui. Update Purpose after archive.
## Requirements
### Requirement: Meteorology grid metadata contract
The system SHALL provide renderer-neutral metadata for variables PRCP/TEMP/RH/wind/Rn/Press, sources GFS/IFS/ERA5/CLDAS/Best Available, valid times, units, native time resolution, spatial resolution, bbox, tile URL templates, grid-cell query URLs, area-stat query URLs, comparison support, and restricted/unavailable reasons.

#### Scenario: Valid metadata
WHEN a variable/source has valid times
THEN the grid page can request tiles, query endpoints, area statistics, and timeline ticks without generated hour or value sequences

#### Scenario: Restricted source
WHEN CLDAS is configured as restricted
THEN metadata includes restricted reason and UI disables playback/query for CLDAS

#### Scenario: Empty valid times
WHEN a variable/source has no contract-provided valid times
THEN metadata marks the product unavailable and the UI disables timeline playback, tile rendering, and query controls for that product

#### Scenario: Bounded area statistics
WHEN an area-stat query is inside the advertised bbox and max area contract but the live area-stat service is not connected
THEN the UI renders a scoped unavailable state and does not fabricate a statistic

#### Scenario: Area statistics validation
WHEN an area-stat query exceeds the advertised bbox, resolution, or max area contract
THEN the UI renders a validation state instead of an unbounded raster read or fabricated statistic

