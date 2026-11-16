# met-station-series-api Specification

## Purpose
Define the public `/api/v1/met/stations/{station_id}/series` response contract
for the current station forcing time-series route. The active route is aligned
with `object-store-station-series-read`: it serves retained SHUD forcing CSV
values from `OBJECT_STORE_ROOT`, while using `met.met_station` only for station
metadata. DB archival/history semantics through `met.forcing_station_timeseries`
are governed by `docs/adr/0001-station-forcing-history-api-boundary.md` and
require a future explicit archive/history API change.

## Requirements

### Requirement: Station forcing series route

The system SHALL expose a bounded station forcing time-series route for
`GET /api/v1/met/stations/{station_id}/series`. The current route SHALL use the
disk-only object-store reader defined by `object-store-station-series-read` for
series values and SHALL NOT treat `met.forcing_station_timeseries` or
`met.forcing_version` as the active data selector for this route.

#### Scenario: Model source cycle disk query
- **WHEN** a client requests `GET /api/v1/met/stations/{station_id}/series` with `model_id`, `source_id`, and `cycle_time`
- **THEN** the route resolves station metadata from `met.met_station`
- **AND** the route reads series values from the corresponding SHUD CSV under `OBJECT_STORE_ROOT`
- **AND** the response includes the requested `station_id`, `model_id`, `source_id`, `cycle_time`, the compatibility `forcing_version_id` response field, station metadata, and grouped series for the returned variables
- **AND** each variable series includes `variable`, `unit`, `points`, and `truncated`
- **AND** each point includes `valid_time`, `value`, and `quality_flag`.

#### Scenario: Deprecated forcing_version_id query compatibility
- **WHEN** a client supplies `forcing_version_id` together with `model_id`, `source_id`, and `cycle_time`
- **THEN** the route accepts but ignores `forcing_version_id`
- **AND** the response is selected by `model_id`, `source_id`, `cycle_time`, and `station_id`, not by the supplied `forcing_version_id`.

#### Scenario: forcing_version_id alone is not an active selector
- **WHEN** a client supplies `forcing_version_id` without the required `model_id`, `source_id`, and `cycle_time` tuple
- **THEN** the route returns the existing `MISSING_REQUIRED_FILTER` error shape
- **AND** it does not attempt to resolve a DB-backed forcing version for this route.

#### Scenario: Required MVP variables
- **WHEN** station series is requested without an explicit variable filter
- **THEN** the API returns available disk-backed series for `PRCP`, `TEMP`, `RH`, `wind`, and `Rn`
- **AND** it does not emit `Press`, because current SHUD station CSV files do not contain a `Press` column
- **AND** it does not invent variables or samples that are absent from the CSV.

#### Scenario: Variable and time filtering
- **WHEN** a client supplies `variables`, `from`, `to`, or `limit`
- **THEN** the API applies those filters before returning chart points
- **AND** omitted, empty, or blank-only `variables` behaves like the default request and returns `PRCP`, `TEMP`, `RH`, `wind`, and `Rn`
- **AND** unknown or unsupported variables, including `Press`, are silently dropped from this route's result set
- **AND** `from > to` returns HTTP 200 with `data.series=[]`
- **AND** syntactically invalid times or invalid limits still produce validation errors rather than broad unbounded queries.

#### Scenario: Truncation metadata
- **WHEN** more points exist than the requested or default limit allows
- **THEN** the response marks the affected variable series as `truncated=true`
- **AND** exposes `limit`, `returned_points`, `requested_from`, `requested_to`, `returned_from`, and `returned_to` metadata for the UI to show that only a bounded sample was returned.

#### Scenario: Missing station or disk artifact
- **WHEN** `station_id` does not exist
- **THEN** the API returns a stable `STATION_NOT_FOUND` response
- **WHEN** the station exists but the resolved disk CSV is missing
- **THEN** the API returns `STATION_FORCING_FILE_NOT_FOUND`
- **AND** it does not fall back to `met.forcing_station_timeseries` or return an empty success response that could be confused with a valid but empty time range.

### Requirement: Station series contract stays aligned across API, reader, and OpenAPI

The station series contract SHALL be implemented consistently in backend reader
helpers, FastAPI routes, OpenAPI, frontend generated types, and tests.

#### Scenario: OpenAPI drift prevention
- **WHEN** the station series route is implemented
- **THEN** `openapi/nhms.v1.yaml` contains the route, query parameters, response schemas, and error schemas
- **AND** the route is removed from any deferred OpenAPI drift allowlist.

#### Scenario: Reader preserves response provenance
- **WHEN** the backend query helper reads station series
- **THEN** it preserves `unit`, `native_resolution`, `quality_flag`, `source_id`, `valid_time`, station metadata, and compatibility forcing-version provenance in the public response
- **AND** station metadata comes from `met.met_station`
- **AND** series values come from the retained disk CSV, not from `met.forcing_station_timeseries`.

#### Scenario: Current disk route defers DB archival/history behavior
- **WHEN** long-term historical station-series access or `met.forcing_station_timeseries` cleanup is implemented
- **THEN** that work SHALL follow `docs/adr/0001-station-forcing-history-api-boundary.md`
- **AND** this spec SHALL remain aligned with `object-store-station-series-read` until DB-backed history is explicitly reintroduced by a future change.

### Requirement: DB-backed station series helper is legacy internal surface

`PsycopgForecastStore.station_series()` SHALL be treated as a legacy/internal
DB-backed helper retained for historical contract tests and ADR 0001
archive/history design material. The current public display route SHALL NOT
call it.

#### Scenario: production code does not call legacy helper

- **WHEN** production Python code under `apps/`, `packages/`, `services/`, or
  `workers/` is inspected
- **THEN** no production call site outside `packages/common/forecast_store.py`
  SHALL invoke `.station_series(`
- **AND** the public FastAPI station-series route SHALL continue to call the
  object-store reader instead of the DB-backed helper

#### Scenario: legacy helper remains explicit until historical API decision

- **WHEN** tests exercise `PsycopgForecastStore.station_series()`
- **THEN** that coverage SHALL be understood as preserving a legacy/internal DB
  contract, not as evidence that the current public route falls back to DB
  history
- **AND** any reintroduction of DB-backed historical station-series access SHALL
  follow `docs/adr/0001-station-forcing-history-api-boundary.md` and land as a
  future explicit API change rather than a silent fallback from the direct-disk
  route

### Requirement: Historical station forcing access is explicit archive surface

Long-term station forcing history, if implemented, SHALL be exposed through a
separate archive/history API surface or an explicit opt-in mode. It SHALL NOT be
a silent fallback from the current retained-disk station-series route.

#### Scenario: retained disk route keeps disk miss semantics

- **WHEN** the current public station-series route resolves a disk path outside
  the retained object-store window
- **THEN** it SHALL return `STATION_FORCING_FILE_NOT_FOUND`
- **AND** it SHALL NOT read DB/archive rows to mask the missing disk artifact

#### Scenario: future archive surface exposes provenance

- **WHEN** a future archive/history station-series API returns samples
- **THEN** it SHALL expose provenance that distinguishes archive/DB data from
  retained display CSV data
- **AND** that provenance SHALL include source id, cycle time, model id,
  forcing version id, storage source, and freshness or retention class

#### Scenario: future archive errors remain distinct from disk errors

- **WHEN** a future archive/history selector cannot resolve DB/archive data
- **THEN** it SHALL use DB/archive-specific errors such as
  `FORCING_VERSION_NOT_FOUND`, `FORCING_VERSION_NOT_FINALIZED`,
  `FORCING_VERSION_FILTER_CONFLICT`, or `STATION_NOT_IN_FORCING_VERSION`
- **AND** it SHALL NOT reuse `STATION_FORCING_FILE_NOT_FOUND` for DB/archive
  selector failures
