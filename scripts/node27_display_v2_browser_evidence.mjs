#!/usr/bin/env node
/**
 * Capture the display-v2 national overview browser evidence on node-27 (#2017 7.2).
 *
 * Deliberately a standalone Playwright driver rather than a new `apps/frontend/e2e`
 * lane: this is receipt tooling for one live deployment, and the e2e configs carry
 * their own env contracts and mock guards that an ops capture has no business
 * widening. It drives a real browser against the live site, mocks nothing, and
 * writes one JSON report plus the screenshots the receipt cites.
 *
 * What it measures, per the issue's acceptance criteria:
 *   - time from navigation start until the overview stops settling, observed as
 *     `[data-testid="m11-overview-loading"]` being absent while the fullscreen map
 *     is present. That notice renders exactly while
 *     `mapBootstrapLoading || (!overview.bootstrap && !bootstrapError)`
 *     (OverviewPage.tsx), so its disappearance is a strictly stronger signal than
 *     `mapBootstrapLoading` alone falling false;
 *   - the layout oracle: header 84px, bottom control bar 64px, no horizontal scroll;
 *   - that the discharge tile and precipitation PNG request URLs actually change
 *     when the source, cycle or valid time changes;
 *   - that `precip=0` hides the overlay and its legend.
 *
 * Usage:
 *   node scripts/node27_display_v2_browser_evidence.mjs --base-url https://test.nwm.ac.cn \
 *        --out-dir /path/to/evidence [--viewport 1920x1080] [--settle-budget-ms 1000]
 *
 * Mobile evidence (openspec mobile-responsive-display 7.1, #2815) is a separate
 * code path selected by `--device-preset`; without that flag nothing above
 * changes. With it the script emulates a phone, opens the test gate so the two
 * read-only locate hooks exist, and captures `/` in three states -- default,
 * river window, station window -- each on a fresh page, opening a window with
 * one real touch tap at the located point. It writes its own files
 * (`mobile-<preset>-<state>.png`, `mobile-geometry-<preset>.json`) and only
 * ever issues GET requests. See docs/runbooks/display-mobile-evidence.md.
 *
 *   node scripts/node27_display_v2_browser_evidence.mjs --base-url https://test.nwm.ac.cn \
 *        --out-dir /path/to/evidence --device-preset mobile-portrait|mobile-landscape \
 *        --river-basin-id <basin_id> --river-segment-id <river_segment_id> [--station-id <station_id>]
 *
 * Exit codes: 0 every assertion held, 1 at least one failed, 2 usage/launch error.
 */

import { realpathSync } from 'node:fs'
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const TITLE = '全国水文模拟系统（V2.0）'
const HEADER_HEIGHT = 84
const CONTROL_BAR_HEIGHT = 64
const TILE_RE = /\/api\/v1\/tiles\/hydro-national\//
const PRECIP_PNG_RE = /\/api\/v1\/precip\/.*\.png(\?|$)/
const PRECIP_INDEX_RE = /\/api\/v1\/precip\/[^?]*\/index(\?|$)/

export function parseArgs(argv) {
  const args = {
    baseUrl: 'http://127.0.0.1:8080',
    outDir: null,
    viewport: '1920x1080',
    settleBudgetMs: 1000,
    settleSamples: 3,
    timeoutMs: 60000,
    // Where to resolve the browser driver from. This script is run from a scratch
    // directory on node-27 so that the active tree stays clean, and bare
    // `import('playwright')` resolves against the script's own location, not cwd.
    playwrightRoot: null,
  }
  // Preset-only arguments are kept off `args` unless a preset is given, so the
  // default invocation parses to exactly the object it always did.
  const mobile = {}
  let viewportGiven = false
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]
    const value = argv[i + 1]
    if (key === '--base-url') { args.baseUrl = value; i += 1 }
    else if (key === '--out-dir') { args.outDir = value; i += 1 }
    else if (key === '--viewport') { args.viewport = value; viewportGiven = true; i += 1 }
    else if (Object.hasOwn(MOBILE_ARGUMENTS, key)) { mobile[MOBILE_ARGUMENTS[key]] = value; i += 1 }
    else if (key === '--settle-budget-ms') { args.settleBudgetMs = Number(value); i += 1 }
    else if (key === '--timeout-ms') { args.timeoutMs = Number(value); i += 1 }
    else if (key === '--playwright-root') { args.playwrightRoot = value; i += 1 }
    else if (key === '--settle-samples') { args.settleSamples = Number(value); i += 1 }
    else { throw new Error(`unknown argument: ${key}`) }
  }
  if (!args.outDir) throw new Error('--out-dir is required')
  if (!('devicePreset' in mobile)) {
    const stray = Object.keys(MOBILE_ARGUMENTS).find((flag) => MOBILE_ARGUMENTS[flag] in mobile)
    if (stray) throw new Error(`${stray} is only valid together with --device-preset`)
  } else {
    const preset = resolveDevicePreset(mobile.devicePreset)
    // Judged on presence, not value: --viewport has a default.
    if (viewportGiven) throw new Error('--viewport conflicts with --device-preset (the preset sets the viewport)')
    for (const [flag, field] of [['--river-basin-id', 'riverBasinId'], ['--river-segment-id', 'riverSegmentId']]) {
      if (mobile[field] === undefined) throw new Error(`${flag} is required with --device-preset`)
      if (!PIN_RE.test(mobile[field])) throw new Error(`bad ${flag}: must match ${PIN_RE.source}`)
    }
    if ('stationId' in mobile && !mobile.stationId) throw new Error('bad --station-id: must not be empty')
    Object.assign(args, { devicePreset: preset, riverBasinId: mobile.riverBasinId, riverSegmentId: mobile.riverSegmentId, stationId: mobile.stationId ?? null })
    args.viewport = `${preset.viewport.width}x${preset.viewport.height}`
  }
  const [w, h] = args.viewport.split('x').map(Number)
  if (!w || !h) throw new Error(`bad --viewport: ${args.viewport}`)
  args.width = w
  args.height = h
  return args
}

async function loadChromium(playwrightRoot) {
  const specs = ['playwright', 'playwright-core', '@playwright/test']
  if (playwrightRoot) {
    const require = createRequire(path.join(path.resolve(playwrightRoot), 'package.json'))
    for (const spec of specs) {
      try {
        const mod = await import(pathToFileURL(require.resolve(spec)).href)
        // A CommonJS entry (which `@playwright/test` is) lands the exports on
        // `default`, so both shapes have to be accepted.
        const chromium = mod.chromium ?? mod.default?.chromium
        if (chromium) return chromium
      } catch { /* try the next one */ }
    }
  }
  for (const spec of specs) {
    try {
      const mod = await import(spec)
      const chromium = mod.chromium ?? mod.default?.chromium
      if (chromium) return chromium
    } catch { /* try the next one */ }
  }
  throw new Error('no playwright package found; pass --playwright-root <workspace with node_modules>')
}

/** One navigation: collect request URLs, wait for the surface to settle, measure. */
async function visit(context, url, { timeoutMs, screenshot }) {
  const page = await context.newPage()
  const tileUrls = new Set()
  const precipPngUrls = new Set()
  const precipIndexUrls = new Set()
  page.on('request', (request) => {
    const requested = request.url()
    if (TILE_RE.test(requested)) tileUrls.add(requested)
    // The catalog probe (`/index`) is issued whether or not the overlay is on --
    // only the PNG requests are evidence that the layer is actually painting.
    if (PRECIP_PNG_RE.test(requested)) precipPngUrls.add(requested)
    else if (PRECIP_INDEX_RE.test(requested)) precipIndexUrls.add(requested)
  })

  const started = Date.now()
  await page.goto(url, { waitUntil: 'commit', timeout: timeoutMs })
  await page.waitForFunction(
    () =>
      document.querySelector('[data-testid="m11-fullscreen-map"]') !== null &&
      document.querySelector('[data-testid="m11-overview-loading"]') === null,
    undefined,
    { timeout: timeoutMs },
  )
  const settleMs = Date.now() - started

  const layout = await page.evaluate(() => {
    const box = (selector) => {
      const element = document.querySelector(selector)
      if (!element) return null
      const rect = element.getBoundingClientRect()
      return { height: Math.round(rect.height), width: Math.round(rect.width), bottom: Math.round(rect.bottom) }
    }
    return {
      title: document.querySelector('header')?.textContent?.trim() ?? '',
      header: box('header'),
      controlBar: box('[data-testid="m11-bottom-control-bar"]'),
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      innerHeight: window.innerHeight,
      precipLegend: document.querySelector('[data-testid="m11-floating-legend-precip"]') !== null,
      precipToggle: document.querySelector('[data-testid="m11-layer-toggle-precip"]') !== null,
      mapCanvas: document.querySelector('[data-testid="m11-fullscreen-map"] canvas') !== null,
      emptyNotice: document.querySelector('[data-testid="m11-overview-empty"]')?.textContent?.trim() ?? null,
    }
  })

  // The overlay keeps painting after the surface settles; give the tile and PNG
  // requests a bounded moment so the URL sets are not measured half-empty.
  await page.waitForTimeout(4000)
  if (screenshot) await page.screenshot({ path: screenshot, fullPage: false })
  await page.close()

  return {
    url,
    settle_ms: settleMs,
    layout,
    tile_urls: [...tileUrls].sort(),
    precip_png_urls: [...precipPngUrls].sort(),
    precip_index_urls: [...precipIndexUrls].sort(),
    screenshot: screenshot ? path.basename(screenshot) : null,
  }
}

function checkScene(name, scene, args, failures) {
  const fail = (message) => failures.push(`${name}: ${message}`)
  if (!scene.layout.header || scene.layout.header.height !== HEADER_HEIGHT) {
    fail(`header height ${scene.layout.header?.height} != ${HEADER_HEIGHT}`)
  }
  if (!scene.layout.controlBar || scene.layout.controlBar.height !== CONTROL_BAR_HEIGHT) {
    fail(`control bar height ${scene.layout.controlBar?.height} != ${CONTROL_BAR_HEIGHT}`)
  }
  if (scene.layout.scrollWidth > scene.layout.clientWidth) {
    fail(`horizontal scroll: scrollWidth ${scene.layout.scrollWidth} > clientWidth ${scene.layout.clientWidth}`)
  }
  if (!scene.layout.title.includes(TITLE)) fail(`header text lacks ${TITLE}`)
  const settle = scene.settle_median_ms ?? scene.settle_ms
  if (settle >= args.settleBudgetMs) {
    fail(`surface settled in ${settle} ms (median of ${scene.settle_samples_ms?.length ?? 1}), budget ${args.settleBudgetMs} ms`)
  }
}

// --- Mobile evidence (`--device-preset`): a separate code path; nothing below
// --- is reached by the default invocation.

const MOBILE_SCHEMA = 'nhms.node27-display-mobile-evidence.v1'
const MOBILE_HEADER_HEIGHT = 48
const MOBILE_CHART_MIN_HEIGHT = 160
const SHORT_LANDSCAPE_CHART_MIN_HEIGHT = 120
/** Mobile form = width < 768 or height < 500 (specs/mobile-viewport-shell). */
const MOBILE_FORM_MAX_WIDTH = 768
const MOBILE_FORM_MAX_HEIGHT = 500
const EDGE_TOLERANCE_PX = 0.5
const MOBILE_STATES = ['default', 'river', 'station']
const MOBILE_ARGUMENTS = { '--device-preset': 'devicePreset', '--river-basin-id': 'riverBasinId', '--river-segment-id': 'riverSegmentId', '--station-id': 'stationId' }
/** Same identifier shape the live-river-click lane accepts for its two pins. */
const PIN_RE = /^[A-Za-z0-9._:-]{1,96}$/
/** Smallest extent a zero-width or zero-height segment bbox is padded to. */
const BBOX_EPSILON_DEG = 1e-6
const STATION_PAGE_LIMIT = 200
const STATION_PAGE_CAP = 50
const STATION_CANDIDATE_LIMIT = 5
/** Hook answers that say "this station is not tappable here"; the next candidate may be. */
const STATION_ROTATE_CODES = ['STATION_HOOK_NOT_RENDERED', 'STATION_HOOK_CLUSTERED', 'STATION_HOOK_POINT_OCCLUDED']
/** What the operator should do about the two river hook answers that mean "wrong pin". */
const RIVER_HOOK_HINTS = {
  HOOK_POINT_OCCLUDED: ' -- the segment sits under the control bar or a launcher; pick another river segment pin',
  HOOK_FEATURE_MISMATCH: ' -- the discharge layer does not render this id at the anchor; pin the id family it renders (`..._shud_riv_...`), not a `..._reach_...` or `..._seg_...` id',
}
/** Both hooks bound themselves at 15 s; the Node-side cut-off sits just above it. */
const LOCATE_TIMEOUT_MS = 20000
/** Both hooks are installed when the map surface mounts, and the page-ready wait has already seen the map. */
const HOOK_TIMEOUT_MS = 10000
const WINDOW_TIMEOUT_MS = 10000
const GEOMETRY_SETTLE_TIMEOUT_MS = 10000
const GEOMETRY_PROBE_MS = 250
/** The product's camera pan lasts 450 ms; two reads further apart than that must agree. */
const CAMERA_PROBE_MS = 650

const MOBILE_USER_AGENT = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'
/** Same viewports as the mocked lane's `mobile-portrait` / `mobile-landscape` projects. */
const DEVICE_PRESETS = {
  'mobile-portrait': { width: 390, height: 664 },
  'mobile-landscape': { width: 750, height: 342 },
}

const SELECTORS = {
  header: 'header',
  controlBar: '[data-testid="m11-bottom-control-bar"]',
  map: '[data-testid="m11-fullscreen-map"]',
  surface: '[data-testid="m11-map-surface"]',
  launchers: Object.fromEntries(['layers', 'basemap', 'legend'].map((name) => [name, `[data-testid="m11-launcher-${name}"]`])),
  // Tappable controls and the one exemption, ported from the 5.1 touch audit.
  controls: [
    'button', 'a[href]', 'select', 'input:not([type=hidden])', 'summary',
    '[role=button]', '[role=tab]', '[role=combobox]', '[role=switch]', '[role=checkbox]',
    '[role=radio]', '[role=menuitem]', '[role=option]', '[tabindex]:not([tabindex="-1"])',
  ].join(', '),
  exempt: '.maplibregl-ctrl-attrib',
}
/** The error fallback of the region both curve windows render in; it replaces the window, frame included. */
const CURVE_FALLBACK = 'region-error-map-panels'
const CURVE_FALLBACK_SELECTOR = `[data-testid="${CURVE_FALLBACK}"]`
const CURVE_FALLBACK_FAILURE = `the curve region crashed: its error fallback ${CURVE_FALLBACK} replaced the curve window (a product bug; a larger --timeout-ms does not help)`
/** `busy` = every window body that means "not finished". River: the blank placeholder of the first 600 ms, then the notice. Station: the notice (no delayed placeholder), "waiting for the source", a refresh. */
const WINDOWS = {
  river: {
    path: '/',
    hookGlobal: '__nhmsRiverClickEvidence',
    hookMethod: 'locateRenderedRiver',
    ready: '[data-testid="m11-map-surface"][data-registered-overlays="discharge"]',
    readyWhat: 'the map did not register the discharge overlay',
    frame: '[data-testid="m11-river-forecast-panel"]',
    chart: '[data-testid="m11-river-panel-chart"]',
    busy: '[data-testid="m11-river-panel-pending"], [data-testid="m11-river-panel-loading"]',
    empty: '[data-testid="m11-river-panel-empty"]',
  },
  station: {
    path: '/?metStations=1',
    hookGlobal: '__nhmsStationLocateEvidence',
    hookMethod: 'locateRenderedStation',
    ready: '[data-testid="m11-map-surface"]:not([data-met-station-feature-count="0"])',
    readyWhat: 'the station layer has no features (the page loads stations for up to 50 basins one after another)',
    frame: '[data-testid="m11-station-popup"]',
    chart: '[data-testid="m11-station-panel-chart"]',
    busy: '[data-testid="m11-station-popup-loading"], [data-testid="m11-station-popup-no-product"], [data-testid="m11-station-panel-refreshing"]',
    empty: '[data-testid="m11-station-popup-empty"]',
  },
}

/** Preset name -> the full emulation descriptor; an unknown name is a usage error. */
export function resolveDevicePreset(name) {
  if (!Object.hasOwn(DEVICE_PRESETS, name)) {
    throw new Error(`unknown --device-preset: ${name} (valid: ${Object.keys(DEVICE_PRESETS).join(', ')})`)
  }
  return {
    name,
    viewport: { ...DEVICE_PRESETS[name] },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
    userAgent: MOBILE_USER_AGENT,
  }
}

/**
 * Viewport -> form -> the layout expectations that hold in it. Desktop form is the three pinned checks of `checkScene`; mobile form replaces the
 * control-bar height pin with "inside the viewport" and never consults the document scroll width (the shell clips overflow, so it proves nothing).
 */
export function expectationsForViewport({ width, height }) {
  const mobile = width < MOBILE_FORM_MAX_WIDTH || height < MOBILE_FORM_MAX_HEIGHT
  if (!mobile) {
    return {
      form: 'desktop',
      shortLandscape: false,
      headerHeight: HEADER_HEIGHT,
      controlBarHeight: CONTROL_BAR_HEIGHT,
      horizontalScrollCheck: true,
      controlsInViewport: false,
      chartMinHeight: null,
      sheetSide: null,
    }
  }
  const shortLandscape = height < MOBILE_FORM_MAX_HEIGHT && width > height
  return {
    form: 'mobile',
    shortLandscape,
    headerHeight: MOBILE_HEADER_HEIGHT,
    controlBarHeight: null,
    horizontalScrollCheck: false,
    controlsInViewport: true,
    chartMinHeight: shortLandscape ? SHORT_LANDSCAPE_CHART_MIN_HEIGHT : MOBILE_CHART_MIN_HEIGHT,
    sheetSide: shortLandscape ? 'right' : 'bottom',
  }
}

function describeBox(box) {
  return `${box.width}x${box.height} @ (${box.x}, ${box.y})`
}

/** Horizontally the whole hit box counts. Vertically a member of a vertical scroller (the sheet body) is judged on its visible span only, and one scrolled fully out of view passes. */
function controlOutsideViewport(control, viewport) {
  const { box } = control
  if (box.x < -EDGE_TOLERANCE_PX || box.x + box.width > viewport.width + EDGE_TOLERANCE_PX) return true
  if (control.inVerticalScroller && control.visibleY === null) return false
  const top = control.inVerticalScroller ? control.visibleY.top : box.y
  const bottom = control.inVerticalScroller ? control.visibleY.bottom : box.y + box.height
  return top < -EDGE_TOLERANCE_PX || bottom > viewport.height + EDGE_TOLERANCE_PX
}

/**
 * Judge one captured state against the mobile expectations and return the failures (empty = the state holds).
 * Pure -- `curveWait` is how the wait for the curve ended -- so it is unit-tested on synthetic geometry.
 */
export function judgeMobileState(geometry, expectations, curveWait = null) {
  const failures = []
  const { viewport } = geometry
  const outside = (controls) => {
    for (const control of controls) {
      if (controlOutsideViewport(control, viewport)) {
        failures.push(`control ${control.tag}「${control.name}」 ${describeBox(control.box)} is outside the ${viewport.width}x${viewport.height} viewport`)
      }
    }
  }

  if (geometry.state === 'default') {
    if (!geometry.header || Math.abs(geometry.header.height - expectations.headerHeight) > EDGE_TOLERANCE_PX) {
      failures.push(`header height ${geometry.header?.height} != ${expectations.headerHeight}`)
    }
    if (!geometry.controlBar) failures.push('bottom control bar is missing')
    else if (controlOutsideViewport({ box: geometry.controlBar, inVerticalScroller: false }, viewport)) {
      failures.push(`bottom control bar ${describeBox(geometry.controlBar)} is outside the ${viewport.width}x${viewport.height} viewport`)
    }
    for (const [name, box] of Object.entries(geometry.launchers)) {
      if (!box) failures.push(`launcher ${name} is missing`)
    }
    outside(geometry.controls)
    return failures
  }

  // The fallback is the whole story of the state: no frame, no chart and no anchor left to judge.
  if (geometry.panel === CURVE_FALLBACK) return [CURVE_FALLBACK_FAILURE]
  if (!geometry.sheet) return ['curve window frame is missing']
  const panel = `panel state: ${geometry.panel ?? 'none'}${geometry.emptyText ? `「${geometry.emptyText}」` : ''}`
  // A wait that ran out fails the state even if the curve turned up before the read.
  if (curveWait?.timed_out) failures.push(`curve still loading after ${curveWait.limit_ms} ms (${panel})`)
  else if (!geometry.chart) failures.push(`no curve data: the wait for the curve ended without a chart area (${panel})`)
  else if (geometry.chart.height < expectations.chartMinHeight) failures.push(`chart area height ${geometry.chart.height} < ${expectations.chartMinHeight}`)
  if (!geometry.map) failures.push('map area is missing')
  if (geometry.anchor.x === null || geometry.anchor.y === null) {
    failures.push('selected-anchor attributes are missing on the map surface')
  } else if (geometry.map) {
    const x = Number(geometry.anchor.x)
    const y = Number(geometry.anchor.y)
    const { map, sheet } = geometry
    if (!(x > map.x && x < map.x + map.width && y > map.y && y < map.y + map.height)) {
      failures.push(`selected anchor (${x}, ${y}) is outside the map area ${describeBox(map)}`)
    } else if (expectations.sheetSide === 'right' && !(x < sheet.x)) {
      failures.push(`selected anchor (${x}, ${y}) is under the right sheet (left edge ${sheet.x})`)
    } else if (expectations.sheetSide === 'bottom' && !(y < sheet.y)) {
      failures.push(`selected anchor (${x}, ${y}) is under the bottom sheet (top edge ${sheet.y})`)
    }
  }
  outside(geometry.controls.filter((control) => control.inSheet))
  return failures
}

/** The files a preset run writes; none collides with the desktop outputs. */
export function mobileOutputFiles(presetName) {
  const screenshots = Object.fromEntries(MOBILE_STATES.map((state) => [state, `mobile-${presetName}-${state}.png`]))
  return { report: `mobile-geometry-${presetName}.json`, screenshots }
}

/** Per-state results -> the report, its file name and the exit code. Pure. */
export function assembleMobileReport({ baseUrl, preset, expectations, riverTarget, stationTarget, states, nonGetRequests, capturedAt }) {
  const failures = []
  for (const state of MOBILE_STATES) {
    for (const failure of states[state].failures) failures.push(`${state}: ${failure}`)
  }
  const report = {
    schema: MOBILE_SCHEMA,
    base_url: baseUrl,
    preset,
    form: expectations.shortLandscape ? 'mobile-short-landscape' : expectations.form,
    expectations,
    river_target: riverTarget,
    station_target: stationTarget,
    captured_at: capturedAt,
    states,
    non_get_requests: nonGetRequests,
    failures,
    pass: failures.length === 0,
  }
  return { report, reportFile: mobileOutputFiles(preset.name).report, exitCode: failures.length === 0 ? 0 : 1 }
}

/**
 * Segment geometry -> the river hook's `bbox` and `anchor`: bbox over every coordinate (a zero-width or zero-height extent padded by a tiny
 * epsilon), anchor = the middle of the flattened coordinates. Returns null when the geometry is not a usable LineString / MultiLineString.
 */
export function riverTargetFromGeometry(geom) {
  const parts = geom?.type === 'LineString' ? [geom.coordinates] : geom?.type === 'MultiLineString' ? geom.coordinates : null
  if (!Array.isArray(parts)) return null
  const flat = []
  for (const part of parts) {
    if (!Array.isArray(part)) return null
    for (const coordinate of part) {
      const [lon, lat] = Array.isArray(coordinate) ? coordinate : []
      if (!Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90) return null
      flat.push([lon, lat])
    }
  }
  if (flat.length === 0) return null
  const extent = (axis) => {
    const [min, max] = flat.reduce(([low, high], point) => [Math.min(low, point[axis]), Math.max(high, point[axis])], [Infinity, -Infinity])
    return min === max ? [min - BBOX_EPSILON_DEG, max + BBOX_EPSILON_DEG] : [min, max]
  }
  const [minLon, maxLon] = extent(0)
  const [minLat, maxLat] = extent(1)
  return { bbox: [[minLon, minLat], [maxLon, maxLat]], anchor: flat[Math.floor((flat.length - 1) / 2)] }
}

/**
 * Station list -> the candidates to try, in `station_id` order, keeping only stations with coordinates. With `stationId` the list is that one
 * station or empty. Coordinates are read the way the page reads them (`getHydroMetStationCoordinates`): the first two numbers of
 * `geom.coordinates` when both are finite, else the top-level `longitude` / `latitude`.
 */
export function stationCandidates(items, stationId) {
  const pair = ([longitude, latitude]) => (Number.isFinite(longitude) && Number.isFinite(latitude) ? { longitude, latitude } : null)
  const coordinates = (item) => (Array.isArray(item?.geom?.coordinates) ? pair(item.geom.coordinates) : null) ?? pair([item?.longitude, item?.latitude])
  const located = items
    .map((item) => ({ station_id: item?.station_id, ...coordinates(item) }))
    .filter((item) => typeof item.station_id === 'string' && 'longitude' in item)
    .sort((a, b) => (a.station_id < b.station_id ? -1 : a.station_id > b.station_id ? 1 : 0))
  if (stationId) return located.filter((item) => item.station_id === stationId).slice(0, 1)
  return located.slice(0, STATION_CANDIDATE_LIMIT)
}

function errorText(error) {
  return String(error?.message ?? error).split('\n')[0]
}

/** One read-only GET of a display API route; returns the envelope's `data`. */
async function getApiData(context, args, route) {
  const response = await context.request.get(`${args.baseUrl}${route}`, { timeout: args.timeoutMs })
  if (!response.ok()) throw new Error(`GET ${route} answered HTTP ${response.status()}`)
  const body = await response.json()
  if (body?.status !== 'ok' || body.data === null || typeof body.data !== 'object') {
    throw new Error(`GET ${route} did not answer an ok envelope`)
  }
  return body.data
}

/**
 * Resolve what to tap, the way the live-river-click preflight does: the GFS identity-only latest product for the basin pin, then the segment
 * geometry and the station list of that product's model (the set the page renders). Each target is `{ ...fields }` or `{ error }`; a failed
 * product request fails both.
 */
async function resolveMobileTargets(context, args) {
  const enc = encodeURIComponent
  let product
  try {
    product = await getApiData(context, args, `/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=${enc(args.riverBasinId)}`)
    for (const key of ['basin_version_id', 'river_network_version_id', 'model_id']) {
      if (typeof product[key] !== 'string' || product[key] === '') throw new Error(`latest-product has no ${key}`)
    }
  } catch (error) {
    const failure = { error: `GFS latest-product for basin ${args.riverBasinId} is unavailable: ${errorText(error)}` }
    return { river: failure, station: failure }
  }
  const identity = {
    basin_id: args.riverBasinId,
    basin_version_id: product.basin_version_id,
    river_network_version_id: product.river_network_version_id,
    model_id: product.model_id,
  }

  let river
  try {
    const segment = await getApiData(
      context,
      args,
      `/api/v1/basin-versions/${enc(identity.basin_version_id)}/river-segments/${enc(args.riverSegmentId)}?river_network_version_id=${enc(identity.river_network_version_id)}`,
    )
    const target = riverTargetFromGeometry(segment.geom)
    if (!target) throw new Error('segment geom is not a usable LineString / MultiLineString')
    river = { ...identity, river_segment_id: args.riverSegmentId, ...target }
  } catch (error) {
    river = { error: `river segment ${args.riverSegmentId} is unavailable: ${errorText(error)}` }
  }

  let station
  try {
    const items = []
    let total = 0
    for (let page = 0; page < STATION_PAGE_CAP; page += 1) {
      const data = await getApiData(
        context,
        args,
        `/api/v1/met/stations?basin_version_id=${enc(identity.basin_version_id)}&model_id=${enc(identity.model_id)}&limit=${STATION_PAGE_LIMIT}&offset=${items.length}`,
      )
      if (!Array.isArray(data.items)) throw new Error('station list has no items array')
      items.push(...data.items)
      total = Number.isFinite(data.total_count) ? data.total_count : items.length
      if (data.items.length === 0 || items.length >= total) break
    }
    const candidates = stationCandidates(items, args.stationId)
    if (candidates.length === 0) {
      throw new Error(args.stationId ? `--station-id ${args.stationId} is not in the list (${items.length} stations)` : `no station with coordinates among ${items.length}`)
    }
    station = { ...identity, requested_station_id: args.stationId, total_count: total, candidates }
  } catch (error) {
    station = { error: `station list for basin ${args.riverBasinId} (GFS model) is unavailable: ${errorText(error)}` }
  }
  return { river, station }
}

/**
 * Call a locate hook in the page. The hooks reject with a plain `{ code, message }` object, which is folded into a serializable outcome in
 * the page; the Node-side timer covers a hook that never answers.
 */
async function callLocateHook(page, spec, input, timeoutMs) {
  const locating = page.evaluate(
    async ({ hookGlobal, hookMethod, value }) => {
      const hook = window[hookGlobal]
      if (typeof hook?.[hookMethod] !== 'function') {
        return { ok: false, code: 'HOOK_MISSING', message: `window.${hookGlobal}.${hookMethod} is missing` }
      }
      try {
        return { ok: true, located: await hook[hookMethod](value) }
      } catch (error) {
        const failure = error ?? {}
        return { ok: false, code: String(failure.code ?? 'UNKNOWN'), message: String(failure.message ?? error) }
      }
    },
    { hookGlobal: spec.hookGlobal, hookMethod: spec.hookMethod, value: input },
  )
  // After the cut-off the in-page call is still running; nobody awaits it.
  locating.catch(() => undefined)
  let timer
  const timedOut = new Promise((resolve) => {
    timer = setTimeout(
      () => resolve({ ok: false, code: 'LOCATE_TIMEOUT', message: `no answer from ${spec.hookMethod} within ${timeoutMs} ms` }),
      timeoutMs,
    )
  })
  return Promise.race([locating, timedOut]).finally(() => clearTimeout(timer))
}

function readCamera(page) {
  return page.locator(SELECTORS.surface).first().evaluate((surface) => [
    surface.getAttribute('data-selected-anchor-x'),
    surface.getAttribute('data-selected-anchor-y'),
    surface.getAttribute('data-camera-center'),
    surface.getAttribute('data-camera-zoom'),
  ])
}

/**
 * The anchor attributes only update once the camera rests, so a read taken during the sheet's auto-pan still shows the covered pre-pan
 * position. Poll until two reads at least one pan duration apart agree on all four.
 */
async function waitForCameraSettled(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  let previous = await readCamera(page)
  for (;;) {
    await page.waitForTimeout(CAMERA_PROBE_MS)
    const current = await readCamera(page)
    if (current[2] !== null && current[3] !== null && current.every((value, index) => value === previous[index])) return true
    if (Date.now() >= deadline) return false
    previous = current
  }
}

/** One in-page read of everything `judgeMobileState` needs for a state. */
function measureGeometry(page, state) {
  const windowSpec = WINDOWS[state] ?? null
  return page.evaluate(
    ({ stateName, selectors, frame, chart, busy, empty, fallback }) => {
      const rectBox = (rect) => ({ x: rect.left, y: rect.top, width: rect.right - rect.left, height: rect.bottom - rect.top })
      const box = (selector) => {
        const element = selector ? document.querySelector(selector) : null
        return element ? rectBox(element.getBoundingClientRect()) : null
      }
      const nameOf = (element) => {
        const candidates = [element.getAttribute('data-testid'), element.getAttribute('aria-label'), element.tagName === 'SELECT' ? null : element.textContent]
        const found = candidates.map((value) => (value ?? '').trim().replace(/\s+/g, ' ')).find((value) => value !== '')
        return (found ?? element.tagName.toLowerCase()).slice(0, 40)
      }

      const controls = []
      for (const element of document.querySelectorAll(selectors.controls)) {
        if (element.tagName === 'CANVAS') continue
        const style = getComputedStyle(element)
        const own = element.getBoundingClientRect()
        if (own.width === 0 || own.height === 0) continue
        if (style.display === 'none' || style.visibility !== 'visible') continue
        if (element.closest('[inert], [aria-hidden="true"]')) continue
        if (element.closest(selectors.exempt)) continue

        const label = element.tagName === 'INPUT' ? (element.labels?.[0] ?? null) : null
        const hit = label ? label.getBoundingClientRect() : own
        // Every clipping ancestor narrows the visible vertical span. A control
        // with no overlap with a non-scrolling clipper is not on screen at all;
        // members of a vertical scroller are kept and judged on that span.
        let clipped = false
        let inVerticalScroller = false
        let visibleTop = hit.top
        let visibleBottom = hit.bottom
        for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
          const { overflowX, overflowY } = getComputedStyle(ancestor)
          if (overflowX === 'visible' && overflowY === 'visible') continue
          const rect = ancestor.getBoundingClientRect()
          if (overflowY === 'auto' || overflowY === 'scroll') inVerticalScroller = true
          if (own.right <= rect.left || own.left >= rect.right || own.bottom <= rect.top || own.top >= rect.bottom) clipped = true
          visibleTop = Math.max(visibleTop, rect.top)
          visibleBottom = Math.min(visibleBottom, rect.bottom)
        }
        if (clipped && !inVerticalScroller) continue

        controls.push({
          name: nameOf(element),
          tag: element.tagName.toLowerCase(),
          box: rectBox(hit),
          inSheet: frame !== null && element.closest(frame) !== null,
          inVerticalScroller,
          visibleY: inVerticalScroller && visibleBottom > visibleTop ? { top: visibleTop, bottom: visibleBottom } : null,
        })
      }

      const attribute = (name) => document.querySelector(selectors.surface)?.getAttribute(name) ?? null
      const panel = frame === null ? null : document.querySelector(`${fallback}, ${busy}, ${chart}, ${empty}`)
      return {
        state: stateName,
        viewport: { width: window.innerWidth, height: window.innerHeight },
        header: box(selectors.header),
        controlBar: box(selectors.controlBar),
        launchers: Object.fromEntries(Object.entries(selectors.launchers).map(([name, selector]) => [name, box(selector)])),
        map: box(selectors.map),
        sheet: box(frame),
        chart: box(chart),
        loading: panel !== null && panel.matches(busy),
        panel: panel?.getAttribute('data-testid') ?? null,
        emptyText: panel?.matches(empty) ? panel.textContent.trim().replace(/\s+/g, ' ').slice(0, 120) : null,
        anchor: { x: attribute('data-selected-anchor-x'), y: attribute('data-selected-anchor-y') },
        camera: { center: attribute('data-camera-center'), zoom: attribute('data-camera-zoom') },
        controls,
      }
    },
    { stateName: state, selectors: SELECTORS, frame: windowSpec?.frame ?? null, chart: windowSpec?.chart ?? null, busy: windowSpec?.busy ?? null, empty: windowSpec?.empty ?? null, fallback: CURVE_FALLBACK_SELECTOR },
  )
}

/** Measure until two consecutive reads are identical, so no box is taken mid-transition. */
async function measureSettledGeometry(page, state, timeoutMs, failures) {
  const limit = Math.min(GEOMETRY_SETTLE_TIMEOUT_MS, timeoutMs)
  const deadline = Date.now() + limit
  let geometry = await measureGeometry(page, state)
  for (;;) {
    await page.waitForTimeout(GEOMETRY_PROBE_MS)
    const next = await measureGeometry(page, state)
    const settled = JSON.stringify(next) === JSON.stringify(geometry)
    geometry = next
    if (settled) return geometry
    if (Date.now() >= deadline) {
      failures.push(`geometry did not settle within ${limit} ms; the last read is reported`)
      return geometry
    }
  }
}

/**
 * Open one curve window on a page that already shows `/`: wait for the hook and the layer, locate, tap once, wait for the frame, the curve
 * and the camera. Returns whether the window opened; every reason it did not is in `result.failures`.
 */
async function openCurveWindow(page, state, target, args, result) {
  const spec = WINDOWS[state]
  const fail = (message) => { result.failures.push(message); return false }
  if (target.error) return fail(target.error)

  const hookTimeoutMs = Math.min(HOOK_TIMEOUT_MS, args.timeoutMs)
  const hookPresent = await page
    .waitForFunction(({ hookGlobal, hookMethod }) => typeof window[hookGlobal]?.[hookMethod] === 'function', spec, { timeout: hookTimeoutMs })
    .then(() => true, () => false)
  if (!hookPresent) {
    return fail(`window.${spec.hookGlobal}.${spec.hookMethod} is missing after ${hookTimeoutMs} ms (does the deployed build carry the hook?)`)
  }
  const ready = await page.waitForSelector(spec.ready, { state: 'attached', timeout: args.timeoutMs }).then(() => true, () => false)
  if (!ready) return fail(`${spec.readyWhat} within ${args.timeoutMs} ms`)

  const riverInput = () => ({
    bbox: target.bbox,
    anchor: target.anchor,
    basinId: target.basin_id,
    riverSegmentId: target.river_segment_id,
    basinVersionId: target.basin_version_id,
    riverNetworkVersionId: target.river_network_version_id,
  })
  const candidates =
    state === 'river'
      ? [{ label: `segment ${target.river_segment_id}`, id: target.river_segment_id, input: riverInput() }]
      : target.candidates.map(({ station_id: id, longitude, latitude }) => ({ label: `station ${id}`, id, input: { stationId: id, lngLat: [longitude, latitude] } }))
  const locateTimeoutMs = Math.min(LOCATE_TIMEOUT_MS, args.timeoutMs)
  result.attempts = []
  let located = null
  for (const candidate of candidates) {
    const outcome = await callLocateHook(page, spec, candidate.input, locateTimeoutMs)
    result.attempts.push({ id: candidate.id, ok: outcome.ok, code: outcome.code ?? null, message: outcome.message ?? null })
    if (outcome.ok) {
      const { clientX, clientY } = outcome.located ?? {}
      if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return fail(`${spec.hookMethod} returned a non-finite point for ${candidate.label}`)
      const answered = state === 'river' ? outcome.located.riverSegmentId : outcome.located.stationId
      if (answered !== candidate.id) return fail(`${spec.hookMethod} located ${answered}, not ${candidate.label}`)
      located = { id: candidate.id, x: clientX, y: clientY }
      break
    }
    if (state === 'station' && STATION_ROTATE_CODES.includes(outcome.code)) continue
    const hint = RIVER_HOOK_HINTS[outcome.code] ?? ''
    return fail(`${spec.hookMethod} failed for ${candidate.label} with ${outcome.code}: ${outcome.message}${hint}`)
  }
  if (!located) {
    const codes = result.attempts.map((attempt) => `${attempt.id} ${attempt.code}`).join('; ')
    const hint = result.attempts.every((attempt) => attempt.code === 'STATION_HOOK_NOT_RENDERED')
      ? ' -- the page renders stations for its first 50 basins only (and at most 5000 stations); pick a basin pin inside that set'
      : ''
    return fail(`no station could be located after ${result.attempts.length} candidate(s): ${codes}${hint}`)
  }

  await page.touchscreen.tap(located.x, located.y)
  result.tap = located
  const windowTimeoutMs = Math.min(WINDOW_TIMEOUT_MS, args.timeoutMs)
  // A region that crashes on its first render never shows the frame: its fallback ends this wait too (the two never coexist).
  const opened = await page.waitForSelector(`${spec.frame}, ${CURVE_FALLBACK_SELECTOR}`, { state: 'visible', timeout: windowTimeoutMs }).then(() => true, () => false)
  if (!opened) return fail(`the ${state} window did not appear within ${windowTimeoutMs} ms after a tap at (${located.x}, ${located.y})`)
  if ((await page.locator(CURVE_FALLBACK_SELECTOR).count()) > 0) return fail(CURVE_FALLBACK_FAILURE)

  // Wait for a terminal body -- the region's fallback, or nothing still loading AND the chart or the empty
  // notice present. "No loading notice" alone is true before the notice mounts.
  const waitStarted = Date.now()
  const terminal = ({ busy, chart, empty, fallback }) => document.querySelector(fallback) !== null || (!document.querySelector(busy) && document.querySelector(`${chart}, ${empty}`) !== null)
  const timedOut = await page
    .waitForFunction(terminal, { ...spec, fallback: CURVE_FALLBACK_SELECTOR }, { timeout: args.timeoutMs })
    .then(() => false, (error) => { if (error?.name !== 'TimeoutError') throw error; return true })
  result.curve_wait = { waited_ms: Date.now() - waitStarted, limit_ms: args.timeoutMs, timed_out: timedOut }
  if (!(await waitForCameraSettled(page, args.timeoutMs))) {
    result.failures.push(`the map camera did not come to rest within ${args.timeoutMs} ms (or the build does not expose data-camera-center / -zoom); the anchor may be the pre-pan one`)
  }
  return true
}

/** Capture one state on a fresh page. Never throws: a failure is recorded and the screenshot still taken. */
async function captureMobileState(context, state, target, args, expectations, nonGetRequests) {
  const url = `${args.baseUrl}${WINDOWS[state]?.path ?? '/'}`
  const result = { url, settle_ms: null, tap: null, attempts: null, curve_wait: null, geometry: null, screenshot: null, failures: [] }
  const page = await context.newPage()
  page.on('request', (request) => {
    if (request.method() !== 'GET') nonGetRequests.push(`${state}: ${request.method()} ${request.url()}`)
  })
  try {
    const started = Date.now()
    await page.goto(url, { waitUntil: 'commit', timeout: args.timeoutMs })
    await page.waitForFunction(
      () =>
        document.querySelector('[data-testid="m11-fullscreen-map"]') !== null &&
        document.querySelector('[data-testid="m11-overview-loading"]') === null,
      undefined,
      { timeout: args.timeoutMs },
    )
    // Recorded only: the settle budget is a desktop-path check.
    result.settle_ms = Date.now() - started
    const judged = state === 'default' || (await openCurveWindow(page, state, target, args, result))
    result.geometry = await measureSettledGeometry(page, state, args.timeoutMs, result.failures)
    // A window that never opened already has its reason; judging its absent geometry adds nothing.
    if (judged) result.failures.push(...judgeMobileState(result.geometry, expectations, result.curve_wait))
  } catch (error) {
    result.failures.push(`capture failed: ${errorText(error)}`)
  }
  try {
    const file = mobileOutputFiles(args.devicePreset.name).screenshots[state]
    await page.screenshot({ path: path.join(args.outDir, file), fullPage: false })
    result.screenshot = file
  } catch (error) {
    result.failures.push(`screenshot failed: ${errorText(error)}`)
  }
  await page.close().catch(() => undefined)
  return result
}

async function captureMobileEvidence(browser, args) {
  const preset = args.devicePreset
  const expectations = expectationsForViewport(preset.viewport)
  const context = await browser.newContext({
    viewport: preset.viewport,
    deviceScaleFactor: preset.deviceScaleFactor,
    hasTouch: preset.hasTouch,
    isMobile: preset.isMobile,
    userAgent: preset.userAgent,
  })
  try {
    // The test gate: the two locate hooks exist only when this is set before the app boots.
    await context.addInitScript(() => { window.__NHMS_E2E_HOOKS__ = true })
    const targets = await resolveMobileTargets(context, args)
    const nonGetRequests = []
    const states = {}
    for (const state of MOBILE_STATES) {
      states[state] = await captureMobileState(context, state, targets[state] ?? null, args, expectations, nonGetRequests)
    }
    const { report, reportFile, exitCode } = assembleMobileReport({
      baseUrl: args.baseUrl,
      preset,
      expectations,
      riverTarget: targets.river,
      stationTarget: targets.station,
      states,
      nonGetRequests,
      capturedAt: new Date().toISOString(),
    })
    await writeFile(path.join(args.outDir, reportFile), `${JSON.stringify(report, null, 2)}\n`, 'utf8')
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
    return exitCode
  } finally {
    await context.close()
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  await mkdir(args.outDir, { recursive: true })
  const chromium = await loadChromium(args.playwrightRoot)
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
  if (args.devicePreset) return captureMobileEvidence(browser, args).finally(() => browser.close())
  const context = await browser.newContext({ viewport: { width: args.width, height: args.height } })

  const failures = []
  const scenes = {}
  try {
    scenes.default = await visit(context, `${args.baseUrl}/`, {
      timeoutMs: args.timeoutMs,
      screenshot: path.join(args.outDir, 'overview-default.png'),
    })
    // One navigation is one sample of a network-bound number; take a few and
    // judge the budget on the median, keeping every sample in the report.
    const settleSamples = [scenes.default.settle_ms]
    for (let i = 1; i < args.settleSamples; i += 1) {
      const extra = await visit(context, `${args.baseUrl}/`, { timeoutMs: args.timeoutMs, screenshot: null })
      settleSamples.push(extra.settle_ms)
    }
    settleSamples.sort((a, b) => a - b)
    scenes.default.settle_samples_ms = settleSamples
    scenes.default.settle_median_ms = settleSamples[Math.floor(settleSamples.length / 2)]
    checkScene('default', scenes.default, args, failures)

    scenes.ifs = await visit(context, `${args.baseUrl}/?source=ifs`, {
      timeoutMs: args.timeoutMs,
      screenshot: path.join(args.outDir, 'overview-ifs.png'),
    })
    checkScene('ifs', scenes.ifs, args, failures)

    // A later valid time inside the same cycle: the tile path and the PNG name
    // both carry it, so both URL sets must move.
    const firstTile = scenes.default.tile_urls[0] ?? ''
    const cycleMatch = firstTile.match(/hydro-national\/(\w+)\/([^/]+)\/q_down\/([^/]+)\//)
    let shifted = null
    if (cycleMatch) {
      const validTime = decodeURIComponent(cycleMatch[3])
      const later = new Date(new Date(validTime).getTime() + 24 * 3600 * 1000).toISOString().replace('.000Z', 'Z')
      shifted = later
      scenes.shifted_valid_time = await visit(
        context,
        `${args.baseUrl}/?cycle=${encodeURIComponent(decodeURIComponent(cycleMatch[2]))}&validTime=${encodeURIComponent(later)}`,
        { timeoutMs: args.timeoutMs, screenshot: path.join(args.outDir, 'overview-valid-time-plus-24h.png') },
      )
      checkScene('shifted_valid_time', scenes.shifted_valid_time, args, failures)
    } else {
      failures.push('default: no hydro-national tile request observed, cannot shift the valid time')
    }

    scenes.precip_off = await visit(context, `${args.baseUrl}/?precip=0`, {
      timeoutMs: args.timeoutMs,
      screenshot: path.join(args.outDir, 'overview-precip-off.png'),
    })
    checkScene('precip_off', scenes.precip_off, args, failures)

    if (scenes.default.precip_png_urls.length === 0) failures.push('default: no precipitation PNG request observed')
    if (scenes.precip_off.precip_png_urls.length !== 0) {
      failures.push(`precip_off: ${scenes.precip_off.precip_png_urls.length} precipitation PNG requests with precip=0`)
    }
    if (scenes.precip_off.layout.precipLegend) failures.push('precip_off: precipitation legend still rendered')
    if (!scenes.default.layout.precipLegend) failures.push('default: precipitation legend missing')

    const sameSource = scenes.default.tile_urls.some((u) => scenes.ifs.tile_urls.includes(u))
    if (sameSource) failures.push('ifs: at least one discharge tile URL is identical to the gfs scene')
    if (scenes.ifs.precip_png_urls.length && scenes.default.precip_png_urls.length) {
      const sharedPng = scenes.ifs.precip_png_urls.some((u) => scenes.default.precip_png_urls.includes(u))
      if (sharedPng) failures.push('ifs: a precipitation PNG URL is identical to the gfs scene')
    }
    if (scenes.shifted_valid_time) {
      const sharedTile = scenes.shifted_valid_time.tile_urls.some((u) => scenes.default.tile_urls.includes(u))
      if (sharedTile) failures.push('shifted_valid_time: a discharge tile URL did not move with the valid time')
    }

    const report = {
      schema: 'nhms.node27-display-v2-browser-evidence.v1',
      base_url: args.baseUrl,
      viewport: { width: args.width, height: args.height },
      settle_budget_ms: args.settleBudgetMs,
      settle_samples: args.settleSamples,
      shifted_valid_time: shifted,
      captured_at: new Date().toISOString(),
      scenes,
      failures,
      pass: failures.length === 0,
    }
    const reportPath = path.join(args.outDir, 'browser-evidence.json')
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
    return failures.length === 0 ? 0 : 1
  } finally {
    await context.close()
    await browser.close()
  }
}

/**
 * True only when this module is the process entry. Fed through stdin it has no
 * file of its own (URL `<cwd>/[eval1]`), so nothing can have imported it: entry.
 * Otherwise `argv[1]` must be this file, both sides through realpath (Node
 * resolves symlinks for the module URL only); absent or another file = import.
 */
function isEntry() {
  let self
  try { self = realpathSync(fileURLToPath(import.meta.url)) } catch { return true }
  try { return realpathSync(process.argv[1]) === self } catch { return false }
}

if (isEntry()) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      process.stderr.write(`${error?.stack ?? error}\n`)
      process.exit(2)
    })
}
