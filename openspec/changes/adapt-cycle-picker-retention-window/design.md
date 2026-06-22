# Design

## Current Behavior

`useHydroMetPopupProduct` resolves a lightweight latest-product identity and
uses `available_issue_times` for the popup issue-time selector. Those issue
times come from DB candidate rows and are not filtered by retained disk
availability. `M11StationForcingPopup` then requests
`/api/v1/met/stations/{station_id}/series` for the selected product identity.

When the disk file is gone, the API returns 404 with
`STATION_FORCING_FILE_NOT_FOUND`. That is correct backend behavior, but the
frontend currently formats it as a generic station-series failure.

## Approach

Keep backend contracts as-is and handle the miss at the frontend boundary:

1. Preserve API error code information in the station-series frontend adapter
   with a typed `HydroMetStationSeriesError`.
2. Add a type guard for retained-disk misses so callers can distinguish
   `STATION_FORCING_FILE_NOT_FOUND` from malformed data, identity mismatch, or
   transient API failures.
3. In `M11StationForcingPopup`, record retained-miss issue times by source,
   cycle, and station within the current popup session.
4. Pass those known-unavailable issue times to `M11PopupSourceControls`, which
   disables them and labels them as retained-disk unavailable.
5. Render a retention-specific empty state for the selected missing cycle.

## Rationale

This keeps the direct-disk truth boundary intact: the UI does not infer that a
DB row means disk data exists, and it does not hide the miss with fallback data.
It also avoids speculative prefetching every issue time for every station, which
would multiply popup latency and API load. The first miss becomes explicit and
prevents repeated selection of the same known-unavailable cycle.

## Test Plan

- Unit test the station-series error-code type guard.
- Component test the station forcing popup for a
  `STATION_FORCING_FILE_NOT_FOUND` response:
  - no chart is drawn;
  - the empty state names the retained disk cycle as unavailable;
  - the failed issue time option is disabled/labeled;
  - selecting another issue time still refetches normally.
