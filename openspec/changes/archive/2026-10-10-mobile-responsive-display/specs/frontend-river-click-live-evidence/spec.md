## ADDED Requirements

### Requirement: A separate gated hook SHALL locate one rendered station

`M11MapLibreSurface` SHALL expose `window.__nhmsStationLocateEvidence` with exactly one method, `locateRenderedStation(input)`, only when the exact pre-start boolean `window.__NHMS_E2E_HOOKS__ === true` is present. `input` SHALL carry the station's `stationId` and its `lngLat`. The method SHALL move the camera to that point, confirm that a rendered `met-stations` feature with that `station_id` lies at the returned viewport point and is not covered by another element, and return the viewport point and the feature's identity. It SHALL be read-only: it SHALL NOT expose a map ref or a generic query surface and SHALL NOT invoke any product callback; the only way into the product click path remains a real pointer or touch activation at the returned point. This hook SHALL NOT change `window.__nhmsRiverClickEvidence`, which keeps exactly its three methods.

#### Scenario: Station hook absent by default

- **WHEN** the map mounts without `window.__NHMS_E2E_HOOKS__ === true`
- **THEN** `window.__nhmsStationLocateEvidence` is undefined

#### Scenario: Station hook locates a rendered station

- **WHEN** the gate is present, the station overlay is on, and `locateRenderedStation` is called with a rendered station's id and coordinates
- **THEN** it returns a viewport point inside the map canvas together with that `station_id`
- **AND** a real click at that point opens the station forcing window for that station

#### Scenario: Station hook refuses an unrendered station

- **WHEN** `locateRenderedStation` is called with a station id that has no rendered feature at the given coordinates
- **THEN** it reports failure and returns no point

#### Scenario: River hook is unchanged

- **WHEN** the gate is present
- **THEN** `window.__nhmsRiverClickEvidence` exposes exactly `locateRenderedRiver`, `armPointerCapture` and `takePointerCapture`

## MODIFIED Requirements

### Requirement: The live river hook MUST be absent by default and select one actual rendered discharge feature

`M11MapLibreSurface` SHALL expose
`window.__nhmsRiverClickEvidence` with exactly the three methods
`locateRenderedRiver(input)`, `armPointerCapture()` and `takePointerCapture()`,
only when the exact
pre-start boolean `window.__NHMS_E2E_HOOKS__ === true` is present. The global
SHALL register on the gated component mount without exposing a map ref or generic
query/control method. Without the flag it SHALL be absent and ordinary pointer,
hover, selection, popup, and request behavior SHALL remain unchanged.

`input` SHALL contain only
`bbox: [[minLon,minLat],[maxLon,maxLat]]`, `anchor: [lon,lat]`, `basinId`,
`riverSegmentId`, `basinVersionId`, and `riverNetworkVersionId`. Coordinates
SHALL be finite WGS84 values with ordered non-degenerate bounds. Identity values
SHALL be nonempty and bounded. The Promise-returning method SHALL wait at most
15,000 ms for the current map and a renderable `discharge` overlay, fit the bbox
with padding 48, duration 0, and maxZoom 14, project the anchor, and query only
the 16-by-16 CSS-pixel box around it in
`m11RegisteredOverlayHitLayerId(renderableOverlay)`. It SHALL reject more than 64
total results and require exactly one actual rendered feature whose `basin_id`,
`basin_version_id`, and `river_network_version_id` match. Segment identity SHALL
be `river_segment_id` falling back to `segment_id`; if both exist they SHALL be
equal and the normalized value SHALL match the pin.

After the match, `locateRenderedRiver` SHALL convert the projected anchor to
viewport client coordinates using the map canvas bounding rectangle, rounded to
whole CSS pixels. It SHALL
require `document.elementFromPoint` at that point to be the map canvas, and it
SHALL require the product's own click-target resolution for that point to select
the matched river feature. The resolver is shared with the product click handler
and walks station cluster, station point, overlay hit-layer feature and then basin
fill. It is fed the features rendered at that rounded point (in canvas
coordinates, queried with an array point geometry, never a whole-viewport query)
in the interactive layers that currently exist on the map. Otherwise the hook SHALL reject with
`HOOK_POINT_OCCLUDED`. It SHALL resolve only the four normalized feature
identities and finite `clientX`/`clientY`. It SHALL NOT call `onOverlayClick` or
any other product selection callback; no hook method SHALL be able to put a
feature into the product click path, which only the map's own click event
reaches.

`armPointerCapture()` SHALL clear any previous capture and install one
capture-phase `pointerdown` listener on the map canvas that observes every
pointer-down targeting the canvas, trusted or not. `takePointerCapture()` SHALL
remove that listener and return, when exactly one trusted event arrived, its
`timeStamp` (a high-resolution timestamp in the same time origin as
`performance.now()`), `clientX`, `clientY` and `isTrusted: true`; otherwise a
closed error code: `HOOK_POINTER_MISSING` when none arrived or the capture was
not armed, and `HOOK_POINTER_INVALID` when more than one arrived or any arrived
untrusted.

The hook SHALL never synthesize/modify feature properties, dispatch a DOM or map
event, fetch an API, expose a body or credential, or offer arbitrary application
mutation. Rejections SHALL use only the closed hook codes `HOOK_INVALID_INPUT`,
`HOOK_MAP_UNAVAILABLE`, `HOOK_WRONG_LAYER`, `HOOK_MAP_TIMEOUT`,
`HOOK_QUERY_FAILED`, `HOOK_QUERY_LIMIT`, `HOOK_FEATURE_MISMATCH`,
`HOOK_POINT_OCCLUDED`, `HOOK_POINTER_MISSING`, or `HOOK_POINTER_INVALID`, with
bounded redacted messages. Registration and cleanup SHALL compare both object
identity and a generation token so stale cleanup cannot delete a newer hook, and
cleanup SHALL remove any armed listener.

#### Scenario: Ordinary users have no hook surface

- **WHEN** the map mounts without the exact boolean pre-start flag
- **THEN** `window.__nhmsRiverClickEvidence` is absent and existing pointer-driven river behavior is unchanged

#### Scenario: One current rendered river uses the product click path

- **WHEN** the gate is enabled, exactly one result from the bounded discharge hit-layer query matches the four live identities, the map canvas is the top element at the anchor point, and the product's click-target resolution at that point selects that river
- **THEN** the hook resolves the identities and the finite viewport point without calling any product callback, and only a real click at that point, handled by the map's own click event, enters the product click path

#### Scenario: A located point another feature would win is refused

- **WHEN** a DOM element covers the anchor point, or the product's click-target resolution at that point selects a station, a cluster, basin fill or a different river
- **THEN** `locateRenderedRiver` rejects with `HOOK_POINT_OCCLUDED` and no click is attempted

#### Scenario: Only a trusted pointer event becomes t0

- **WHEN** the capture is armed and a synthetic, untrusted, duplicated, or absent pointer-down reaches the map canvas
- **THEN** `takePointerCapture()` returns `HOOK_POINTER_INVALID` or `HOOK_POINTER_MISSING` instead of a timestamp

#### Scenario: Invalid, stale, or ambiguous selection fails closed

- **WHEN** input is invalid, map/overlay readiness exceeds 15,000 ms, the query fails or exceeds 64 results, or zero/multiple/drifted features match
- **THEN** the Promise rejects with one closed hook code without selecting a first feature, synthesizing a panel, exposing the map, or leaving stale global ownership
