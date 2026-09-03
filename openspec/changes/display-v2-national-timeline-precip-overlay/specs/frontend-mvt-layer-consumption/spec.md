## MODIFIED Requirements

### Requirement: Layer valid_times are consumed from `metadata.valid_times` first
The frontend SHALL consume `apiLayer.metadata.valid_times` returned by `GET /api/v1/layers` as the valid-time list for the **default** `(default_source, default_cycle)` of the `discharge` layer, and SHALL fetch `GET /api/v1/layers/discharge/valid-times?source=&cycle=` whenever the active `(source, cycle)` differs from the metadata defaults. `buildM11RegisteredOverlay` SHALL validate the requested `validTime` against the list held in the store for the active `(source, cycle)`, never solely against `metadata.valid_times`, and SHALL substitute `{source}` and `{cycle}` placeholders in the national template.

#### Scenario: Metadata carries valid_times for the default cycle
- **WHEN** `/api/v1/layers` returns `discharge` with non-empty `metadata.valid_times` and the active `(source, cycle)` equals `(default_source, default_cycle)`
- **THEN** `normalizeLayerStates` MUST use that array directly
- **AND** the frontend MUST NOT issue a separate `/api/v1/layers/discharge/valid-times` request during the same overview load

#### Scenario: Non-default cycle fetches its own list
- **WHEN** the operator selects a cycle other than `default_cycle` (or a source other than `default_source`)
- **THEN** the frontend MUST fetch `/api/v1/layers/discharge/valid-times?source=<source>&cycle=<cycle>` and store it keyed by `(source, cycle)`
- **AND** `buildM11RegisteredOverlay` MUST resolve the overlay using that stored list, producing a non-null overlay when `validTime` is in it

#### Scenario: Metadata.valid_times is intentionally empty (time-less layer)
- **WHEN** `apiLayer.metadata.valid_times === []` (e.g. `river-network` is a topology layer with no time dimension)
- **THEN** the frontend MUST treat the layer as having no time dimension
- **AND** the frontend MUST NOT issue a fallback `/api/v1/layers/<layer_id>/valid-times` request
- **AND** unit tests MUST cover the empty-array primary path explicitly

#### Scenario: Metadata.valid_times is missing or null (schema gap)
- **WHEN** `apiLayer.metadata.valid_times` is `undefined` or `null`
- **THEN** the frontend MAY fetch `/api/v1/layers/<layer_id>/valid-times` as a fallback
- **AND** unit tests MUST cover both the primary and fallback paths (a dedicated `normalizeLayerStates` unit test pair in `apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`)

#### Scenario: National template substitution
- **WHEN** the overlay is built for `source=ifs`, `cycle=2026-09-02T12:00:00Z`, `validTime=2026-09-02T15:00:00Z`
- **THEN** the tile URL is `/api/v1/tiles/hydro-national/ifs/2026-09-02T12:00:00Z/q_down/2026-09-02T15:00:00Z/{z}/{x}/{y}.pbf`
- **AND** the MapLibre source key changes when any of source, cycle, or validTime changes

## ADDED Requirements

### Requirement: Query state carries source, cycle, and precipitation toggle
`M11QueryState` SHALL carry `source` (`gfs|ifs|best|compare`), `cycle` (RFC3339 or null), `validTime`, and `precip: boolean` (default `true`). `queryParamsFromState` SHALL emit `precip=0` when `precip === false` and omit it when true; `parseM11Query` SHALL read `precip=0` as `false` and any other/absent value as `true`. The `M11Layer` union SHALL remain `'discharge'` only.

#### Scenario: Round trip with precipitation disabled
- **WHEN** a state with `precip: false` is serialized and parsed again
- **THEN** the parsed state has `precip === false`

#### Scenario: Default precipitation is on
- **WHEN** the URL has no `precip` parameter
- **THEN** the parsed state has `precip === true`

#### Scenario: Layer enum unchanged
- **WHEN** any code path attempts `layer=precip`
- **THEN** the layer parser falls back to `discharge` and type checking rejects `'precip'` as an `M11Layer`
