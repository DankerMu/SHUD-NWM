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
- **WHEN** `apiLayer.metadata.valid_times === []` for any layer other than `discharge` (e.g. `river-network` is a topology layer with no time dimension, and `precip` carries its times in its own index rather than in the catalog)
- **THEN** the frontend MUST treat the layer as having no time dimension
- **AND** the frontend MUST NOT issue a fallback `/api/v1/layers/<layer_id>/valid-times` request
- **AND** unit tests MUST cover the empty-array primary path explicitly

#### Scenario: Discharge with an empty list is fail-closed, not time-less
- **WHEN** `apiLayer.metadata.valid_times === []` for `layer_id === 'discharge'` together with `metadata.default_cycle === null` (the fail-closed intersection signal defined in `overview-data-contracts`)
- **THEN** the frontend MUST NOT classify `discharge` as a time-less layer
- **AND** it MUST render the disabled cycle selector, timeline and playback required by `map-layer-timeline-controls`, with the notice that no cycle covers every basin
- **AND** it MUST NOT issue a fallback `/api/v1/layers/discharge/valid-times` request and MUST NOT request tiles

#### Scenario: Metadata.valid_times is missing or null (schema gap)
- **WHEN** `apiLayer.metadata.valid_times` is `undefined` or `null`
- **THEN** the frontend MAY fetch `/api/v1/layers/<layer_id>/valid-times` as a fallback
- **AND** unit tests MUST cover both the primary and fallback paths (a dedicated `normalizeLayerStates` unit test pair in `apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`)

#### Scenario: National template substitution
- **WHEN** the overlay is built for `source=ifs`, `cycle=2026-09-02T12:00:00Z`, `validTime=2026-09-02T15:00:00Z`
- **THEN** the tile URL is `/api/v1/tiles/hydro-national/ifs/2026-09-02T12:00:00Z/q_down/2026-09-02T15:00:00Z/{z}/{x}/{y}.pbf`
- **AND** the MapLibre source key changes when any of source, cycle, or validTime changes

#### Scenario: Substituted instants are canonicalized to seconds precision
- **WHEN** the query state holds the millisecond spelling `parseM11QueryState` produces (`normalizeIsoInstant` returns `2026-09-02T12:00:00.000Z`) for `cycle` and `validTime`
- **THEN** the frontend MUST substitute the seconds-precision spelling `2026-09-02T12:00:00Z` into `{cycle}` and `{valid_time}`, matching the single spelling the backend serializes
- **AND** membership checks against the stored `valid_times[]` for `(source, cycle)` MUST compare on that same canonical spelling, so a `.000Z` state value never fails to match an API `...:00Z` entry
- **AND** the same canonicalization applies to the precipitation index and PNG URLs

## ADDED Requirements

### Requirement: Query state carries source, cycle, and precipitation toggle
`M11QueryState` SHALL carry `source` (`gfs|ifs|best|compare`; `defaultM11QueryState.source` becomes `'gfs'` and a parsed `best` is resolved to `gfs` at national scale), `cycle` (RFC3339 or null), `validTime`, and `precip: boolean` (default `true`). The constraint is on the exported surface of `apps/frontend/src/lib/m11/queryState.ts`: `parseM11QueryState` SHALL read `precip=0` as `false` and any other or absent value as `true`; `serializeM11QueryState` SHALL emit `precip=0` when the normalized state has `precip === false` and omit the parameter when it is `true`. Because `serializeM11QueryState` normalizes through `parseM11QueryState(queryParamsFromState(state))` and then rebuilds the query string from an explicit whitelist, the private `queryParamsFromState` helper — which today drops every `false` boolean — MUST be taught to carry `precip: false` through that internal round trip, and `precip` MUST be added to the whitelist; no other boolean's behaviour changes. The `M11Layer` union SHALL remain `'discharge'` only.

#### Scenario: Round trip with precipitation disabled
- **WHEN** `serializeM11QueryState(parseM11QueryState('precip=0'))` is evaluated
- **THEN** the returned query string still contains `precip=0`
- **AND** parsing that returned string yields `precip === false`
- **AND** `needsM11QueryReplacement('precip=0')` is `false`, so the URL is not rewritten away on load

#### Scenario: Serializing an explicit false state
- **WHEN** `serializeM11QueryState({ ...defaultM11QueryState, precip: false })` is evaluated
- **THEN** the result contains `precip=0`

#### Scenario: Default precipitation is on
- **WHEN** the URL has no `precip` parameter
- **THEN** `parseM11QueryState` returns `precip === true`
- **AND** `serializeM11QueryState` of that state omits the `precip` parameter

#### Scenario: Other booleans keep their existing serialization
- **WHEN** a state with `metStations: false` is serialized
- **THEN** the `metStations` parameter is omitted, unchanged from today's behaviour

#### Scenario: Layer enum unchanged
- **WHEN** any code path attempts `layer=precip`
- **THEN** the layer parser falls back to `discharge` and type checking rejects `'precip'` as an `M11Layer`
