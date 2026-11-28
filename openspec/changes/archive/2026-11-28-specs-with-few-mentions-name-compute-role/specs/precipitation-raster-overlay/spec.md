## MODIFIED Requirements

### Requirement: Precipitation endpoints and catalog entry
The display API SHALL expose `GET /api/v1/precip/{source}/{cycle}/index` and `GET /api/v1/precip/{source}/{cycle}/{valid_time}.png` with `source ∈ {gfs, ifs}` and RFC3339 `cycle`/`valid_time` canonicalized per the seconds-precision requirement, and SHALL add a `precip` entry to `/api/v1/layers` with `layer_type = meteorology`, `tile_format = png`, and metadata `image_url_template`, `index_url_template`, `bounds`, `legend`, `window_hours = 24`, `unit = "mm/24h"`. Both routes and both `/api/v1/layers` shape changes MUST be reflected in the hand-maintained `openapi/nhms.v1.yaml`. The mirror root is read from the display-side, read-only env `NHMS_PRECIP_MIRROR_ROOT` (the node-27 view of the same NFS tree the compute role writes under `NHMS_OBJECT_STORE_COPYBACK_ROOT`); the display API MUST NOT read `NHMS_OBJECT_STORE_COPYBACK_ROOT`, which the `display_readonly` profile forbids outright (`apps/api/runtime_mode.py`).

#### Scenario: Index lists only complete windows
- **WHEN** the index is requested for a mirrored cycle
- **THEN** the response contains `source`, `cycle`, `window_hours: 24`, `unit`, `bounds [63, 8, 145, 64]`, `image_size`, `legend[]`, `palette_version`, and `valid_times[]` limited to 3-hour steps from `cycle` to `cycle + 168h` whose windows resolve completely against the mirror as it exists at request time

#### Scenario: PNG for an incomplete window
- **WHEN** the PNG is requested for a valid time whose window is incomplete
- **THEN** the route returns HTTP 404 with code `PRECIP_WINDOW_INCOMPLETE`

#### Scenario: Unmirrored cycle
- **WHEN** the requested cycle's `canonical/<S>/<K>/prcp_rate_or_amount/` directory does not exist under the mirror root (never mirrored, or pruned by retention), even when older mirrored cycles could cover the whole window
- **THEN** the route returns HTTP 404 with code `PRECIP_CYCLE_NOT_MIRRORED` before any slice lookup

#### Scenario: Mirror root unconfigured
- **WHEN** `NHMS_PRECIP_MIRROR_ROOT` is unset or empty on the serving deployment
- **THEN** both routes return HTTP 404 with code `PRECIP_CYCLE_NOT_MIRRORED` and `details.reason == "mirror_root_unconfigured"`, never a 500
- **AND** no filesystem path is stat-ed

#### Scenario: PNG response carries MVT cache semantics
- **WHEN** a PNG is served (cold or from the file cache)
- **THEN** the response carries `Cache-Control: public, max-age=300` and an `ETag` derived from `(source, cycle, valid_time, palette_version, resolved slice set)`, the same header pair and helper convention the MVT tile routes use (`_mvt_response` in the display routes)
- **AND** a request with a matching `If-None-Match` returns 304 without reading the cache file body

#### Scenario: Invalid source
- **WHEN** `source` is not `gfs` or `ifs`
- **THEN** the route returns HTTP 422 without touching the filesystem

#### Scenario: Off-hour or out-of-horizon instant
- **WHEN** `cycle` or `valid_time` carries a non-zero minute or second, or the PNG route's `valid_time` is not one of the 3-hour steps from `cycle` to `cycle + 168h` (including any instant before `cycle` or so far out of range that window arithmetic would overflow)
- **THEN** the route returns HTTP 422 with code `VALIDATION_ERROR` before any filesystem access, writes no cache file, and never answers 500

#### Scenario: IFS index ends at the last complete window
- **WHEN** the mirrored IFS products follow the producer's cadence (3-hourly to lead 144 h, 6-hourly after)
- **THEN** `valid_times[]` ends at `cycle + 144h`, because every later window needs a 3-hourly lead the producer never emits; this is the documented consequence of listing only complete windows, not an error

#### Scenario: Corrupt cache or slice never reaches the client
- **WHEN** a cache file does not start with the PNG signature, or a mirrored slice opens but fails during the data read
- **THEN** the cache file is treated as a miss and rewritten by a complete render, and the failed slice maps to HTTP 404 `PRECIP_WINDOW_INCOMPLETE` with `details.reason == "slice_unreadable"`
