## Context

PR-B for `object-store-station-series-read` moved the public
`GET /api/v1/met/stations/{station_id}/series` route to
`read_station_forcing_csv`. That route intentionally ignores deprecated
`forcing_version_id` selection when the required tuple filters are present, and
does not use `met.forcing_station_timeseries` as a fallback when retained disk
CSV files are absent.

`PsycopgForecastStore.station_series()` remains in `packages/common` with
thorough unit coverage of the historical DB-backed response contract. Deleting
it now would erase useful design/test material for the ADR 0001 archive/history
boundary, but leaving it unlabeled creates entropy: future changes could
reintroduce DB fallback to the hot display route by accident.

## Decision

Keep `PsycopgForecastStore.station_series()` as a legacy/internal DB helper.
It is not an active public display-route implementation and must not be called
by production route/service code. Tests may continue to exercise it to preserve
the historical DB contract. Any long-term archive endpoint or mode must follow
ADR 0001 and land through a future explicit API change.

## Non-Goals

- No behavior change to the current direct-disk station-series route.
- No new historical DB/archive endpoint.
- No deletion of DB-backed helper tests in this issue.
- No frontend cycle-picker changes; #629 owns retention-window UX.

## Risk Fixture

Issue type: contract cleanup / legacy-surface clarification
Project profile: NHMS
Blast radius: low-to-medium
Fixture level: focused

Selected risk packs:

- Public API / CLI / script entry: selected - prevent the public station-series
  route from drifting back to the DB helper.
- Legacy compatibility / examples: selected - preserve historical unit tests
  while labeling the helper as legacy/internal.
- Documentation / migration notes: selected - runbooks must tell operators not
  to diagnose disk-retention misses through old DB error codes.
- Error handling / rollback / partial outputs: selected - disk-retention misses
  must remain `STATION_FORCING_FILE_NOT_FOUND`, not DB fallback successes.
- Schema / columns / units / field names: not selected - no response schema
  changes.
- Auth / permissions / secrets: not selected - no permission or secret surface.
- File IO / path safety / overwrite: not selected - object-store reader safety
  is unchanged.

## Verification Plan

- Focused route/static tests for station-series call boundaries.
- Existing DB-backed helper tests remain green.
- OpenSpec strict validation.
- Ruff.
