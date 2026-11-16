## Context

#631 follows the object-store station-series migration and #630's legacy helper
cleanup. The current display route is intentionally retained-disk-only. Old
cycles outside `/home/ghdc/nwm/object-store/forcing/{source}/` may still exist
in DB/archive storage, but that does not make them valid display-route fallback
data.

## Evaluation

NHMS likely benefits from long-term station forcing history for diagnostics,
hindcast comparison, and archival replay. However, the requirements differ from
the display route:

- users need to know whether data came from retained display CSVs or an archive;
- historical data may be older, incomplete, or produced by a superseded package;
- archive selectors may be `forcing_version_id` centric, while the display
  route is tuple-centric (`model_id`, `source_id`, `cycle_time`);
- returning archive data from the display route would hide disk-retention
  problems from operators and confuse #629 frontend UX.

## Decision

Do not add DB fallback to the current station-series route. Treat historical
station forcing as a future explicit archive/history API surface or explicit
mode with its own OpenAPI/frontend contract.

## Future Contract Shape

The future API should:

- require explicit archive intent, for example a separate route or an
  `include_archive=true` mode that is not the default;
- accept a station id plus either `(model_id, source_id, cycle_time)` or an
  explicit `forcing_version_id`, with conflict detection if both are supplied;
- return bounded series with the same chartable point shape where possible;
- include provenance fields that identify `storage_source`, `forcing_version_id`,
  source, cycle, model, and archive freshness/retention class;
- use DB/archive-specific error codes for DB/archive selection failures;
- never convert disk-path misses from the current route into success responses.

## Non-Goals

- No new endpoint or OpenAPI operation in this issue.
- No frontend archive browser or cycle picker change; #629 owns retained-window
  display UX.
- No change to `read_station_forcing_csv`.
- No revival of `PsycopgForecastStore.station_series()` as a production route
  fallback.

## Risk Fixture

Issue type: product/API boundary evaluation
Project profile: NHMS station forcing time series
Blast radius: low, docs/spec only
Fixture level: focused

Selected risk packs:

- Public API / CLI / script entry: selected - future history must be a distinct
  API surface or explicit opt-in mode.
- Error handling / rollback / partial outputs: selected - disk-path and
  DB/archive errors must remain distinguishable.
- Schema / columns / units / field names: selected conceptually - future API
  should preserve chartable point fields while adding provenance.
- Legacy compatibility / examples: selected - legacy DB helper remains design
  material, not current route behavior.
- Documentation / migration notes: selected - ADR and runbook must explain the
  boundary.

## Verification Plan

- OpenSpec strict validation.
- Markdown lint for docs.
- No runtime tests required because this issue makes no runtime behavior change.
