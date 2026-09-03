## ADDED Requirements

### Requirement: Past-24h precipitation window resolves slices across cycles
The display API SHALL compute the precipitation field for `(source, cycle, valid_time)` as the sum of the eight 3-hour canonical `prcp_rate_or_amount` slices whose end times are `valid_time − 21h … valid_time`, each converted from mm/day to mm per 3h (`rate × 3/24`), yielding mm/24h. For every slice end time `T` the resolver MUST pick the most recent mirrored cycle `C` of the same source with `C ≤ T − 3h` and read lead `T − C`. If any slice file is missing the window MUST be reported as incomplete and no field MUST be produced.

#### Scenario: Lead-0 window comes from prior cycles
- **WHEN** `source=gfs`, `cycle=2026-09-02T12:00Z`, `valid_time=2026-09-02T12:00Z`, and mirrored cycles `2026-09-02T00Z` and `2026-09-01T12Z` exist with leads f003–f168
- **THEN** the resolver selects f003, f006, f009, f012 from `2026-09-02T00Z` for end times 03Z, 06Z, 09Z, 12Z and f003, f006, f009, f012 from `2026-09-01T12Z` for end times 15Z, 18Z, 21Z (previous day) and 00Z
- **AND** the resolved list has exactly 8 slices, none from the requested cycle itself
- **AND** the absence of a GFS f000 file does not produce an incomplete window

#### Scenario: Window inside the forecast horizon uses the requested cycle
- **WHEN** `valid_time = cycle + 48h`
- **THEN** all 8 slices resolve to the requested cycle with leads f027…f048

#### Scenario: Missing slice fails closed
- **WHEN** any of the 8 resolved slice files is absent from the mirror
- **THEN** the resolver raises `PrecipWindowIncomplete` naming the missing slice
- **AND** no partial field is rendered or cached

### Requirement: Precipitation PNG rendering is Web-Mercator aligned and dependency-free
The display API SHALL render the mm/24h field to an 8-bit palette PNG whose rows are resampled to Web-Mercator spacing over the grid bbox (63–145E, 8–64N), width 1316 px, height derived from the Mercator aspect ratio, using bilinear sampling of the 0.25° field. The palette MUST be the CMA 24h six-class scale (mm/24h: <0.1 transparent; 0.1–10; 10–25; 25–50; 50–100; 100–250; ≥250). Encoding MUST use only numpy + zlib (no Pillow) and MUST write the PNG atomically under `NHMS_MVT_FILE_CACHE_DIR/precip/<source>/<cycle>/<valid_time>.<palette_version>.png`.

#### Scenario: Valid PNG structure
- **WHEN** `render_png` is called with a 225×329 field and the grid definition
- **THEN** the bytes start with the PNG signature, contain IHDR (width 1316, bit depth 8, colour type 3), PLTE with 7 entries, tRNS with index 0 fully transparent, and a zlib IDAT

#### Scenario: Classification thresholds
- **WHEN** a cell value is exactly 0.1, 10, 25, 50, 100, or 250 mm/24h
- **THEN** it maps to the class whose lower bound equals that value (lower-inclusive bins)
- **AND** a value below 0.1 maps to palette index 0 (transparent)

#### Scenario: Mercator row placement
- **WHEN** the output image is generated
- **THEN** the output row for latitude φ is located at the Mercator-linear position `(y(φ) − y(8°)) / (y(64°) − y(8°))` where `y(φ) = ln(tan(π/4 + φ/2))`
- **AND** a unit test asserts the row index of the 36°N band against that formula within one pixel

#### Scenario: Cache hit
- **WHEN** the PNG for `(source, cycle, valid_time, palette_version)` already exists in the file cache
- **THEN** the route serves the cached bytes without reading NetCDF

### Requirement: Precipitation endpoints and catalog entry
The display API SHALL expose `GET /api/v1/precip/{source}/{cycle}/index` and `GET /api/v1/precip/{source}/{cycle}/{valid_time}.png` with `source ∈ {gfs, ifs}` and RFC3339 `cycle`/`valid_time`, and SHALL add a `precip` entry to `/api/v1/layers` with `layer_type = meteorology`, `tile_format = png`, and metadata `image_url_template`, `index_url_template`, `bounds`, `legend`, `window_hours = 24`, `unit = "mm/24h"`.

#### Scenario: Index lists only complete windows
- **WHEN** the index is requested for a mirrored cycle
- **THEN** the response contains `source`, `cycle`, `window_hours: 24`, `unit`, `bounds [63, 8, 145, 64]`, `image_size`, `legend[]`, `palette_version`, and `valid_times[]` limited to 3-hour steps from `cycle` to `cycle + 168h` whose windows resolve completely

#### Scenario: PNG for an incomplete window
- **WHEN** the PNG is requested for a valid time whose window is incomplete
- **THEN** the route returns HTTP 404 with code `PRECIP_WINDOW_INCOMPLETE`

#### Scenario: Unmirrored cycle
- **WHEN** the requested `cycle` directory does not exist in the copyback root
- **THEN** the route returns HTTP 404 with code `PRECIP_CYCLE_NOT_MIRRORED`

#### Scenario: Invalid source
- **WHEN** `source` is not `gfs` or `ifs`
- **THEN** the route returns HTTP 422 without touching the filesystem

### Requirement: Frontend precipitation overlay follows the hydrology selection
The frontend SHALL render the precipitation PNG as a MapLibre `image` source + `raster` layer (opacity 0.55, linear resampling) placed beneath the national river layers, driven by the same `(source, cycle, validTime)` as the discharge layer. The overlay SHALL be a boolean query-state field `precip` defaulting to `true`, serialized as `precip=0` when disabled, and MUST NOT be a member of the `M11Layer` union.

#### Scenario: Overlay tracks timeline
- **WHEN** the operator changes `validTime`, `cycle`, or `source`
- **THEN** the image source URL updates to `/api/v1/precip/{source}/{cycle}/{validTime}.png` for the new selection

#### Scenario: Incomplete window is hidden honestly
- **WHEN** the current `validTime` is not in the precip index `valid_times[]`
- **THEN** the raster layer is hidden
- **AND** a notice near the timeline states that the 24h precipitation window is incomplete for this time

#### Scenario: URL round-trip
- **WHEN** the URL contains `precip=0`
- **THEN** the parsed state has `precip === false` and the overlay is not registered
- **AND** serializing a state with `precip === true` omits the parameter

#### Scenario: Legend shows both layers
- **WHEN** the precipitation overlay is enabled
- **THEN** the legend panel shows the six-class precipitation legend (mm/24h) beneath the discharge legend
