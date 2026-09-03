## ADDED Requirements

### Requirement: API instants use one RFC3339 seconds-precision spelling
Every instant this capability places in a URL path segment, in a JSON `cycle`/`valid_times[]` field, or in a cache file name SHALL be serialized as `YYYY-MM-DDTHH:MM:SSZ` — seconds precision, literal trailing `Z`, no fractional seconds and no numeric offset. This is the form `services/tiles/mvt.py::canonical_mvt_time` already emits for discharge valid times, so precipitation index times, precipitation PNG path segments, discharge valid-time lists and the `cycle` path segment of the national tile route all share one spelling. Routes MAY accept an RFC3339 instant that carries fractional seconds or a `+00:00` offset, but MUST canonicalize it to that spelling before it reaches the mirror lookup, the file-cache key, or the ETag input, so two spellings of one instant can never produce two cache entries.

#### Scenario: Canonical instant spelling everywhere
- **WHEN** `GET /api/v1/precip/gfs/2026-09-02T12:00:00Z/index` responds
- **THEN** `cycle` and every entry of `valid_times[]` match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`
- **AND** the cached PNG file name for a listed valid time embeds that same spelling

#### Scenario: Alternate spellings collapse to one cache entry
- **WHEN** the PNG is requested with `valid_time` spelled `2026-09-02T12:00:00.000Z`, then `2026-09-02T12:00:00+00:00`, then `2026-09-02T12:00:00Z`
- **THEN** all three resolve to the same cache file and the second and third requests are cache hits
- **AND** no second file is written under a fractional-second or offset-bearing name

### Requirement: Route triple maps to canonical mirror paths by two pinned rules
The resolver SHALL translate a `(source, cycle)` route pair to mirror paths with exactly two rules and no ad-hoc string handling. **Source:** the route segment is validated against the enum `{gfs, ifs}` FIRST — the enum check rejects anything else (`ERA5`, `best`, `compare`, upper-case spellings) with HTTP 422 before any normalization or filesystem access — and only then translated to the storage source id with `packages/common/source_identity.py::normalize_source_id` (`"gfs" → "gfs"`, `"ifs" → "IFS"`). **Cycle:** the canonical RFC3339 instant is rendered to the compact directory token `%Y%m%d%H`, the same token `workers/canonical_converter/converter.py::format_cycle_time` produces. For storage source `S`, cycle token `K` and lead hour `L`, the slice file is `<copyback_root>/canonical/<S>/<K>/prcp_rate_or_amount/<S>_<K>_prcp_rate_or_amount_f<L:03d>.nc` and the grid definition is `<copyback_root>/canonical/<S>/grid/<grid_id>/grid.json` with `grid_id = gfs_0p25` for `gfs` and `ifs_0p25` for `IFS`.

#### Scenario: ifs route resolves to the upper-case mirror directory
- **WHEN** the resolver handles route source `ifs`, cycle `2026-09-02T12:00:00Z` and a slice end time `T = 2026-09-02T15:00:00Z` (lead 3h)
- **THEN** it reads `<copyback_root>/canonical/IFS/2026090212/prcp_rate_or_amount/IFS_2026090212_prcp_rate_or_amount_f003.nc`
- **AND** the grid comes from `<copyback_root>/canonical/IFS/grid/ifs_0p25/grid.json`

#### Scenario: gfs route keeps the lower-case mirror directory
- **WHEN** the resolver handles route source `gfs`, cycle `2026-09-02T12:00:00Z` and a slice end time `T = 2026-09-02T15:00:00Z`
- **THEN** it reads `<copyback_root>/canonical/gfs/2026090212/prcp_rate_or_amount/gfs_2026090212_prcp_rate_or_amount_f003.nc`
- **AND** the grid comes from `<copyback_root>/canonical/gfs/grid/gfs_0p25/grid.json`

#### Scenario: Enum check precedes normalization
- **WHEN** the route is called with `source=ERA5` (a value `normalize_source_id` would happily accept)
- **THEN** the response is HTTP 422 and `normalize_source_id` is never called and no path is stat-ed

### Requirement: Past-24h precipitation window resolves slices across cycles
The display API SHALL compute the precipitation field for `(source, cycle, valid_time)` as the sum of the eight 3-hour canonical `prcp_rate_or_amount` slices whose end times are `valid_time − 21h … valid_time`, each converted from mm/day to mm per 3h (`rate × 3/24`), yielding mm/24h. For every slice end time `T` the resolver MUST pick the most recent mirrored cycle `C` of the same source satisfying `C ≤ min(requested cycle, T − 3h)` and read lead `T − C`.

The requested cycle is an upper bound: slices come from the requested cycle or from EARLIER cycles of the same source, never from a cycle newer than the requested one, however many newer cycles are mirrored. Because `C ≤ T − 3h`, every resolved lead is ≥ 3h, so a missing GFS f000 product is never a gap. With this bound the resolved slice set for `(source, cycle, valid_time)` — and therefore the PNG cache key — is stable for as long as the selected cycles `≤ requested cycle` stay mirrored; mirror pruning is bounded by the keep-watermark requirement in `canonical-precip-copyback`. If any slice file is missing the window MUST be reported as incomplete and no field MUST be produced.

#### Scenario: Lead-0 window comes from prior cycles
- **WHEN** `source=gfs`, `cycle=2026-09-02T12:00:00Z`, `valid_time=2026-09-02T12:00:00Z`, and mirrored cycles `2026-09-02T00:00:00Z` and `2026-09-01T12:00:00Z` exist with leads f003–f168
- **THEN** the resolver selects f003, f006, f009, f012 from `2026-09-02T00:00:00Z` for end times 03Z, 06Z, 09Z, 12Z and f003, f006, f009, f012 from `2026-09-01T12:00:00Z` for end times 15Z, 18Z, 21Z (previous day) and 00Z
- **AND** the resolved list has exactly 8 slices, none from the requested cycle itself
- **AND** the absence of a GFS f000 file does not produce an incomplete window

#### Scenario: Window inside the forecast horizon uses the requested cycle
- **WHEN** `valid_time = cycle + 48h` and the requested cycle is mirrored
- **THEN** all 8 slices resolve to the requested cycle with leads f027…f048, because `min(requested cycle, T − 3h) = requested cycle` for every end time in the window

#### Scenario: Newer mirrored cycles are never borrowed from
- **WHEN** `source=gfs`, requested `cycle=2026-09-01T12:00:00Z`, `valid_time=2026-09-02T12:00:00Z`, and the mirror holds `2026-09-01T00:00:00Z`, `2026-09-01T12:00:00Z`, `2026-09-02T00:00:00Z` and `2026-09-02T12:00:00Z`
- **THEN** all 8 end times `2026-09-01T15:00:00Z … 2026-09-02T12:00:00Z` resolve to `C = 2026-09-01T12:00:00Z` with leads f003…f024
- **AND** no slice is read from `2026-09-02T00:00:00Z` or `2026-09-02T12:00:00Z`, even though an unbounded "most recent `C ≤ T − 3h`" rule would have selected `2026-09-02T00:00:00Z` for the end times at or after `2026-09-02T03:00:00Z`
- **AND** mirroring a further newer cycle afterwards leaves the resolved slice set and the PNG cache key unchanged

#### Scenario: Missing slice fails closed
- **WHEN** any of the 8 resolved slice files is absent from the mirror
- **THEN** the resolver raises `PrecipWindowIncomplete` naming the missing slice
- **AND** no partial field is rendered or cached

### Requirement: Precipitation PNG rendering is Web-Mercator aligned and dependency-free
The display API SHALL render the mm/24h field to an 8-bit palette PNG whose rows are resampled to Web-Mercator spacing over the grid bbox (63–145E, 8–64N), width 1316 px, height derived from the Mercator aspect ratio, using bilinear sampling of the 0.25° field. The palette MUST be the CMA 24h six-class scale in PLTE index order: index 0 = transparent (<0.1 mm/24h); 1 = `#A6F28F` 淡绿 (0.1–10); 2 = `#3DBA3D` 绿 (10–25); 3 = `#61B8FF` 蓝 (25–50); 4 = `#0000FF` 深蓝 (50–100); 5 = `#FA00FA` 紫 (100–250); 6 = `#800040` 深紫 (≥250). The same six hex values in the same order MUST be returned as `legend[].color` by the precip index and by the `precip` entry of `/api/v1/layers`, and `palette_version` MUST change whenever any hex value or threshold changes. Encoding MUST use only numpy + zlib (no Pillow) and MUST write the PNG atomically to `NHMS_MVT_FILE_CACHE_DIR/precip/<storage_source>/<cycle_token>/<valid_time>.<palette_version>.png`, where `<storage_source>` and `<cycle_token>` are the SAME `normalize_source_id` / `%Y%m%d%H` pair used for the mirror path (so the cache directory for a cycle carries the identical name as `canonical/<storage_source>/<cycle_token>`) and `<valid_time>` is the seconds-precision RFC3339 spelling.

#### Scenario: Valid PNG structure
- **WHEN** `render_png` is called with a 225×329 field and the grid definition
- **THEN** the bytes start with the PNG signature, contain IHDR (width 1316, bit depth 8, colour type 3), PLTE with exactly 7 entries whose RGB bytes for indices 1–6 are `A6F28F`, `3DBA3D`, `61B8FF`, `0000FF`, `FA00FA`, `800040` in that order, tRNS with index 0 fully transparent, and a zlib IDAT
- **AND** the precip index `legend[]` and the `/api/v1/layers` `precip` entry carry those same six colours with their thresholds

#### Scenario: Classification thresholds
- **WHEN** a cell value is exactly 0.1, 10, 25, 50, 100, or 250 mm/24h
- **THEN** it maps to the class whose lower bound equals that value (lower-inclusive bins)
- **AND** a value below 0.1 maps to palette index 0 (transparent)

#### Scenario: Mercator row placement
- **WHEN** the output image is generated
- **THEN** the output row for latitude φ is located at the Mercator-linear position `(y(φ) − y(8°)) / (y(64°) − y(8°))` where `y(φ) = ln(tan(π/4 + φ/2))`
- **AND** a unit test asserts the row index of the 36°N band against that formula within one pixel

#### Scenario: Cache path mirrors the canonical cycle directory
- **WHEN** the PNG for `source=ifs`, `cycle=2026-09-02T12:00:00Z`, `valid_time=2026-09-02T15:00:00Z` is written
- **THEN** the file is `NHMS_MVT_FILE_CACHE_DIR/precip/IFS/2026090212/2026-09-02T15:00:00Z.<palette_version>.png`
- **AND** the directory component `IFS/2026090212` equals the mirror component `canonical/IFS/2026090212`

#### Scenario: Cache hit
- **WHEN** the PNG for `(source, cycle, valid_time, palette_version)` already exists in the file cache
- **THEN** the route serves the cached bytes without reading NetCDF

### Requirement: Precipitation endpoints and catalog entry
The display API SHALL expose `GET /api/v1/precip/{source}/{cycle}/index` and `GET /api/v1/precip/{source}/{cycle}/{valid_time}.png` with `source ∈ {gfs, ifs}` and RFC3339 `cycle`/`valid_time` canonicalized per the seconds-precision requirement, and SHALL add a `precip` entry to `/api/v1/layers` with `layer_type = meteorology`, `tile_format = png`, and metadata `image_url_template`, `index_url_template`, `bounds`, `legend`, `window_hours = 24`, `unit = "mm/24h"`. Both routes and both `/api/v1/layers` shape changes MUST be reflected in the hand-maintained `openapi/nhms.v1.yaml`.

#### Scenario: Index lists only complete windows
- **WHEN** the index is requested for a mirrored cycle
- **THEN** the response contains `source`, `cycle`, `window_hours: 24`, `unit`, `bounds [63, 8, 145, 64]`, `image_size`, `legend[]`, `palette_version`, and `valid_times[]` limited to 3-hour steps from `cycle` to `cycle + 168h` whose windows resolve completely against the mirror as it exists at request time

#### Scenario: PNG for an incomplete window
- **WHEN** the PNG is requested for a valid time whose window is incomplete
- **THEN** the route returns HTTP 404 with code `PRECIP_WINDOW_INCOMPLETE`

#### Scenario: Unmirrored cycle
- **WHEN** the requested `cycle` directory does not exist in the copyback root (never mirrored, or pruned by retention)
- **THEN** the route returns HTTP 404 with code `PRECIP_CYCLE_NOT_MIRRORED`

#### Scenario: Invalid source
- **WHEN** `source` is not `gfs` or `ifs`
- **THEN** the route returns HTTP 422 without touching the filesystem

### Requirement: Prewarm envelope is per-source, cycle-aware, and bounded
This requirement is deliberately hosted in `precipitation-raster-overlay` (rather than `national-river-density`) because the precipitation PNG set is the new surface prewarm gains; it nevertheless governs the whole prewarm envelope, including the discharge-tile and river-network parts. `scripts/node27_mvt_prewarm.py` SHALL discover the newest cycle per source from `GET /api/v1/layers/discharge/cycles?source=<source>` for each of `gfs` and `ifs`, and warm exactly this envelope: for each source with a non-empty cycle list, the z3–z4 China tiles of `/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf` for every valid time of that source's newest cycle, plus one `/api/v1/precip/{source}/{cycle}/{valid_time}.png` per valid time of that cycle; the national river-network prewarm stays z3–z5 and unchanged. The emitted request count and elapsed time MUST be part of the run summary and the deployment receipt. The script MUST NOT fabricate a cycle: an empty `cycles[]` for a source means that source contributes zero requests.

#### Scenario: Envelope and request count
- **WHEN** prewarm runs and both sources report a newest cycle with 57 valid times
- **THEN** the request set is `river-network-national` z3–z5 China tiles (unchanged) plus, per source, `|z3–z4 China tiles| × 57` discharge tile requests and `57` precipitation PNG requests
- **AND** the summary reports the total request count and elapsed seconds, and both land in the deployment receipt

#### Scenario: Per-source newest cycle
- **WHEN** `gfs` newest cycle is `2026-09-02T12:00:00Z` and `ifs` newest cycle is `2026-09-02T00:00:00Z`
- **THEN** each source is warmed at its own newest cycle; the cycle of one source is never reused for the other

#### Scenario: Empty cycles list warms nothing for that source
- **WHEN** `cycles[]` is empty for a source (fail-closed intersection)
- **THEN** prewarm emits zero discharge-tile and zero precipitation requests for that source, reports it in the summary, and MUST NOT substitute a cycle from the other source, from `metadata.valid_times`, or from the current wall clock

### Requirement: Frontend precipitation overlay follows the hydrology selection
The frontend SHALL render the precipitation PNG as a MapLibre `image` source + `raster` layer (opacity 0.55, linear resampling) placed beneath the national river layers, driven by the same `(source, cycle, validTime)` as the discharge layer. The overlay SHALL be a boolean query-state field `precip` defaulting to `true`, serialized as `precip=0` when disabled, and MUST NOT be a member of the `M11Layer` union. The overlay URL MUST only ever name a concrete `gfs` or `ifs` source: when the active source is `best` or `compare` (still offered in basin detail), the frontend MUST use the concrete resolved source if one exists — the same resolution `map-layer-timeline-controls` already requires for run, pipeline and forecast APIs — and otherwise hide the overlay with a stated reason. A request to `/api/v1/precip/best/...` or `/api/v1/precip/compare/...` MUST never be issued.

#### Scenario: Overlay tracks timeline
- **WHEN** the operator changes `validTime`, `cycle`, or `source`
- **THEN** the image source URL updates to `/api/v1/precip/{source}/{cycle}/{validTime}.png` for the new selection, with `cycle` and `validTime` in the seconds-precision RFC3339 spelling

#### Scenario: Non-concrete source resolves or hides
- **WHEN** the active source is `best` or `compare` in basin detail
- **THEN** the overlay URL uses the concrete GFS or IFS source that Best Available resolved to
- **AND** when no concrete source can be resolved (including `compare`, which has no single source), the raster layer is hidden with a stated reason and no precipitation request is issued
- **AND** the string `/api/v1/precip/best/` or `/api/v1/precip/compare/` MUST NOT appear in any issued request

#### Scenario: Incomplete window is hidden honestly
- **WHEN** the current `validTime` is not in the precip index `valid_times[]`
- **THEN** the raster layer is hidden
- **AND** a notice near the timeline states that the 24h precipitation window is incomplete for this time

#### Scenario: Unmirrored cycle is distinguishable from an incomplete window
- **WHEN** the index request for the selected `(source, cycle)` returns HTTP 404 with code `PRECIP_CYCLE_NOT_MIRRORED` (a cycle the discharge cycles endpoint still lists but whose precipitation mirror is absent or pruned)
- **THEN** the raster layer is hidden and the discharge layer and timeline keep working
- **AND** the notice states that this cycle has no precipitation mirror, with wording distinct from the incomplete-window notice, so the two states are told apart in the UI and in the vitest assertion
- **AND** no PNG request is issued for that cycle

#### Scenario: URL round-trip
- **WHEN** the URL contains `precip=0`
- **THEN** the parsed state has `precip === false` and the overlay is not registered
- **AND** serializing a state with `precip === true` omits the parameter

#### Scenario: Legend shows both layers
- **WHEN** the precipitation overlay is enabled
- **THEN** the legend panel shows the six-class precipitation legend (mm/24h) beneath the discharge legend
