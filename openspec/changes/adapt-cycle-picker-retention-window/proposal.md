# Proposal

## Why

The station-series API now reads directly from retained object-store disk
artifacts. A cycle that still exists in DB/latest-product identity history can
legitimately return `STATION_FORCING_FILE_NOT_FOUND` after disk retention
rotates its per-station CSV out. The frontend popup issue-time picker currently
treats every `available_issue_times` entry as equally usable, so users can select
a retained-out cycle and receive a generic station-series error.

## What Changes

- Teach the M11 station forcing popup to recognize
  `STATION_FORCING_FILE_NOT_FOUND` as a retained-disk unavailable cycle.
- Keep the station-series API direct-disk behavior unchanged; do not add DB
  fallback or archive reads.
- Mark issue-time options that have failed with the retained-disk miss as
  unavailable for the current popup session.
- Render a clear retention-specific empty state instead of a generic error or a
  fake chart.

## Out of Scope

- Backend reader behavior, archive API design, or DB-backed history reads.
- Changing latest-product `available_issue_times` semantics.
- Broad M11 timeline or route-query redesign outside the station forcing popup.
