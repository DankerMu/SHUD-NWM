## Why

M11 deliberately marks precipitation, temperature, and station layers unavailable. Design §8 and §8B / effect images 5 and 6 require actual meteorology product surfaces with explicit contracts for raster/grid products and station forcing/QC.

## What Changes

- Add a `/meteorology` route with sub-tabs for spatial grid display and station query once minimum contracts exist.
- Define metadata/API contracts for variables `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, `Press`; sources `GFS`, `IFS`, `ERA5`, `CLDAS`, and `Best Available`; valid times; units; bbox; native time/spatial resolution; raster tile/query URLs; station inventory; forcing series; adjacent-station relationships; and QC summaries.
- Implement grid controls for variable, source, color scale, opacity, contours, station overlay, timeline playback, grid-cell query, area statistics, and multi-source comparison.
- Implement station workflow with basin filter, search/sort, station completeness, map markers, popup, forcing charts, QC markers, and adjacent-station behavior.
- Keep CLDAS/restricted/missing tiles explicit; never render fabricated meteorology values or generated time/value sequences when source contracts are absent.
- Update progress evidence to move effect images 5 and 6 from gap-only to contract-backed UI scope while preserving the remaining live-data limitations.

## Capabilities

### New Capabilities

- `meteorology-navigation-contract`
- `meteorology-grid-layer-contract`
- `meteorology-grid-page`
- `meteorology-station-contract`
- `meteorology-station-page`

## Impact

- Frontend nav/route, query state, and M11 layer placeholders.
- API/OpenAPI/types if missing metadata/query/station/forcing endpoints are added.
- Test fixtures for grid metadata, station inventory, station series, restricted products, tile failures, stale valid-time handling, and empty states.
- Tile publisher/storage integration is limited to choosing and documenting a renderer-neutral tile URL contract unless this issue must add a minimal API endpoint.

## Non-Goals

- CLDAS credential enablement or live download proof.
- Production MVT vector hydrology work; that is M16.
- Fake grid/station data.
- Full TiTiler deployment, national raster publication pipeline, or performance tuning beyond bounded frontend/API request contracts.
- Bias correction, assimilation, downscaling, or multi-source fusion beyond displaying selected-source provenance and comparison statistics when supplied by contracts.
