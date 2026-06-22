# ADR 0001: Station Forcing History API Boundary

Date: 2026-06-22

## Status

Accepted

## Context

The current public station forcing route,
`GET /api/v1/met/stations/{station_id}/series`, reads retained SHUD forcing CSV
files from the node-27 object-store mirror. It is intentionally disk-only: if a
cycle has rotated out of the retained disk window, the route returns
`STATION_FORCING_FILE_NOT_FOUND` even when historical DB rows might still exist.

`PsycopgForecastStore.station_series()` remains as a legacy/internal DB-backed
helper, but #630 clarified that production route/service code must not silently
call it as a fallback.

## Decision

Long-term station forcing history is a valid product direction, but it must be a
separate explicit archive/history API surface or mode. It must not be a silent
fallback from the current display route.

The current display route keeps these semantics:

- retained disk CSV is the only source for series values;
- disk retention misses return `STATION_FORCING_FILE_NOT_FOUND`;
- `forcing_version_id` remains deprecated/non-selector compatibility input when
  tuple filters are present;
- no DB/archive rows are read to mask missing disk artifacts.

A future history API must make its source and retention class explicit. It
should require an explicit selector such as station id plus model/source/cycle,
or station id plus forcing version, and it should return provenance that lets
the caller distinguish archive/DB data from retained display CSV data.

## Future API Semantics

If implemented, the history surface should define:

- freshness: archive responses are historical and not a signal that the current
  display disk route is healthy;
- retention: retained-disk and long-term archive windows are separate;
- provenance: include source id, cycle time, model id, forcing version id, and a
  storage/source marker such as `archive_db` or `archive_object_store`;
- error codes: DB/archive selector errors should use DB/archive-specific codes
  such as `FORCING_VERSION_NOT_FOUND`, `FORCING_VERSION_NOT_FINALIZED`,
  `FORCING_VERSION_FILTER_CONFLICT`, and `STATION_NOT_IN_FORCING_VERSION`, not
  disk-path codes such as `STATION_FORCING_FILE_NOT_FOUND`;
- empty results: a valid historical selector with no samples in the requested
  time filter may return an empty bounded series, but a station/version that was
  never archived should be a stable not-found error.

## Consequences

- #629 can focus on hiding or explaining cycles outside the retained disk window
  without designing historical playback.
- Current map popups and display station charts remain fast-path display
  features, not archive explorers.
- Historical replay work can reuse the legacy DB helper as design material, but
  it needs its own route/OpenAPI/frontend contract before becoming product
  behavior.
