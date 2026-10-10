/**
 * Unit tests for the pure parts of scripts/node27_display_v2_browser_evidence.mjs
 * (openspec mobile-responsive-display 7.1, #2815).
 *
 *   node --test scripts/__tests__/node27_display_v2_browser_evidence.test.mjs
 *
 * The capture itself needs a live site and is proven on node-27; what is pinned
 * here is everything that decides pass / fail without a browser: preset and
 * argument parsing, form -> expectations, the mobile-state judge on synthetic
 * geometry, report assembly, and the entry guard (as subprocesses).
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  assembleMobileReport,
  expectationsForViewport,
  judgeMobileState,
  mobileOutputFiles,
  parseArgs,
  resolveDevicePreset,
  riverTargetFromGeometry,
  stationCandidates,
} from '../node27_display_v2_browser_evidence.mjs'

const SCRIPT = fileURLToPath(new URL('../node27_display_v2_browser_evidence.mjs', import.meta.url))
const REPO_ROOT = path.resolve(path.dirname(SCRIPT), '..')
// The values docs/runbooks/display-mobile-evidence.md documents for both presets.
const DOCUMENTED_DPR = 2
const DOCUMENTED_USER_AGENT = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'
const PINS = ['--river-basin-id', 'basins_qhh', '--river-segment-id', 'basins_qhh_shud_reach_000001']

// --- preset resolution ------------------------------------------------------

test('mobile-portrait preset is a 390x664 touch phone', () => {
  const preset = resolveDevicePreset('mobile-portrait')
  assert.equal(preset.name, 'mobile-portrait')
  assert.deepEqual(preset.viewport, { width: 390, height: 664 })
  assert.equal(preset.hasTouch, true)
  assert.equal(preset.isMobile, true)
  assert.equal(preset.deviceScaleFactor, DOCUMENTED_DPR)
  assert.equal(preset.userAgent, DOCUMENTED_USER_AGENT)
})

test('mobile-landscape preset is a 750x342 touch phone', () => {
  const preset = resolveDevicePreset('mobile-landscape')
  assert.equal(preset.name, 'mobile-landscape')
  assert.deepEqual(preset.viewport, { width: 750, height: 342 })
  assert.equal(preset.hasTouch, true)
  assert.equal(preset.isMobile, true)
  assert.equal(preset.deviceScaleFactor, DOCUMENTED_DPR)
  assert.equal(preset.userAgent, DOCUMENTED_USER_AGENT)
})

test('an unknown preset name is rejected and the message lists the valid names', () => {
  for (const name of ['tablet', 'toString', undefined]) {
    assert.throws(() => resolveDevicePreset(name), (error) => {
      assert.match(error.message, /mobile-portrait/)
      assert.match(error.message, /mobile-landscape/)
      return true
    })
  }
})

// --- argument parsing -------------------------------------------------------

test('without a preset the arguments parse to exactly the pre-change object', () => {
  assert.deepStrictEqual(parseArgs(['--out-dir', '/tmp/out']), {
    baseUrl: 'http://127.0.0.1:8080',
    outDir: '/tmp/out',
    viewport: '1920x1080',
    settleBudgetMs: 1000,
    settleSamples: 3,
    timeoutMs: 60000,
    playwrightRoot: null,
    width: 1920,
    height: 1080,
  })
})

test('without a preset every pre-existing argument keeps its meaning', () => {
  const args = parseArgs([
    '--base-url', 'https://example.test', '--out-dir', 'out', '--viewport', '1280x900', '--settle-budget-ms', '2500',
    '--settle-samples', '5', '--timeout-ms', '9000', '--playwright-root', 'apps/frontend',
  ])
  assert.deepStrictEqual(args, {
    baseUrl: 'https://example.test',
    outDir: 'out',
    viewport: '1280x900',
    settleBudgetMs: 2500,
    settleSamples: 5,
    timeoutMs: 9000,
    playwrightRoot: 'apps/frontend',
    width: 1280,
    height: 900,
  })
})

test('usage errors that existed before are unchanged', () => {
  assert.throws(() => parseArgs([]), /--out-dir is required/)
  assert.throws(() => parseArgs(['--out-dir', 'out', '--bogus', '1']), /unknown argument: --bogus/)
  assert.throws(() => parseArgs(['--out-dir', 'out', 'constructor', '1']), /unknown argument: constructor/)
  assert.throws(() => parseArgs(['--out-dir', 'out', '--viewport', 'wide']), /bad --viewport: wide/)
})

test('a preset run takes the viewport from the preset and carries both river pins', () => {
  const args = parseArgs(['--out-dir', 'out', '--device-preset', 'mobile-landscape', ...PINS])
  assert.equal(args.devicePreset.name, 'mobile-landscape')
  assert.equal(args.width, 750)
  assert.equal(args.height, 342)
  assert.equal(args.riverBasinId, 'basins_qhh')
  assert.equal(args.riverSegmentId, 'basins_qhh_shud_reach_000001')
  assert.equal(args.stationId, null)
  assert.equal(args.timeoutMs, 60000)
})

test('a preset run accepts an optional station id', () => {
  const args = parseArgs(['--out-dir', 'out', '--device-preset', 'mobile-portrait', ...PINS, '--station-id', 'dg-gfs-1::cell:34027'])
  assert.equal(args.stationId, 'dg-gfs-1::cell:34027')
})

test('a preset run without either river pin is a usage error', () => {
  const base = ['--out-dir', 'out', '--device-preset', 'mobile-portrait']
  assert.throws(() => parseArgs(base), /--river-basin-id is required/)
  assert.throws(() => parseArgs([...base, '--river-basin-id', 'basins_qhh']), /--river-segment-id is required/)
  assert.throws(() => parseArgs([...base, '--river-segment-id', 'seg_1']), /--river-basin-id is required/)
})

test('a river pin outside the identifier shape is a usage error', () => {
  const base = ['--out-dir', 'out', '--device-preset', 'mobile-portrait']
  assert.throws(() => parseArgs([...base, '--river-basin-id', 'a/b', '--river-segment-id', 'seg_1']), /bad --river-basin-id/)
  assert.throws(() => parseArgs([...base, '--river-basin-id', 'basin', '--river-segment-id', 'seg 1']), /bad --river-segment-id/)
  assert.throws(() => parseArgs([...base, '--river-basin-id', 'basin', '--river-segment-id', 'x'.repeat(97)]), /bad --river-segment-id/)
})

test('a preset together with an explicit --viewport is a usage error, even at the default value', () => {
  const base = ['--out-dir', 'out', '--device-preset', 'mobile-portrait', ...PINS]
  assert.throws(() => parseArgs([...base, '--viewport', '390x664']), /--viewport conflicts with --device-preset/)
  assert.throws(() => parseArgs([...base, '--viewport', '1920x1080']), /--viewport conflicts with --device-preset/)
})

test('an unknown preset name is a usage error at parse time', () => {
  assert.throws(() => parseArgs(['--out-dir', 'out', '--device-preset', 'phablet', ...PINS]), /unknown --device-preset: phablet .*mobile-portrait, mobile-landscape/)
})

test('preset-only arguments without a preset are usage errors', () => {
  assert.throws(() => parseArgs(['--out-dir', 'out', '--river-basin-id', 'basins_qhh']), /--river-basin-id is only valid together with --device-preset/)
  assert.throws(() => parseArgs(['--out-dir', 'out', '--river-segment-id', 'seg_1']), /--river-segment-id is only valid together with --device-preset/)
  assert.throws(() => parseArgs(['--out-dir', 'out', '--station-id', 'st_1']), /--station-id is only valid together with --device-preset/)
})

test('an unknown argument is still rejected on a preset run', () => {
  assert.throws(() => parseArgs(['--out-dir', 'out', '--device-preset', 'mobile-portrait', ...PINS, '--bogus', '1']), /unknown argument: --bogus/)
})

// --- form -> expectations ---------------------------------------------------

test('desktop form expects header 84, control bar 64 and the horizontal-scroll check', () => {
  for (const [width, height] of [[1920, 1080], [1280, 900], [768, 1024]]) {
    const expectations = expectationsForViewport({ width, height })
    assert.equal(expectations.form, 'desktop', `${width}x${height}`)
    assert.equal(expectations.headerHeight, 84)
    assert.equal(expectations.controlBarHeight, 64)
    assert.equal(expectations.horizontalScrollCheck, true)
    assert.equal(expectations.shortLandscape, false)
  }
})

test('portrait phone expects header 48, controls in the viewport, chart >= 160 and a bottom sheet', () => {
  assert.deepStrictEqual(expectationsForViewport({ width: 390, height: 664 }), {
    form: 'mobile',
    shortLandscape: false,
    headerHeight: 48,
    controlBarHeight: null,
    horizontalScrollCheck: false,
    controlsInViewport: true,
    chartMinHeight: 160,
    sheetSide: 'bottom',
  })
})

test('short landscape phone expects header 48, chart >= 120 and a right sheet', () => {
  assert.deepStrictEqual(expectationsForViewport({ width: 750, height: 342 }), {
    form: 'mobile',
    shortLandscape: true,
    headerHeight: 48,
    controlBarHeight: null,
    horizontalScrollCheck: false,
    controlsInViewport: true,
    chartMinHeight: 120,
    sheetSide: 'right',
  })
})

test('the form boundary is width < 768 or height < 500', () => {
  const form = (width, height) => {
    const { form: name, shortLandscape } = expectationsForViewport({ width, height })
    return `${name}${shortLandscape ? '+short-landscape' : ''}`
  }
  assert.equal(form(767, 800), 'mobile')
  assert.equal(form(768, 800), 'desktop')
  assert.equal(form(800, 499), 'mobile+short-landscape')
  assert.equal(form(800, 500), 'desktop')
  assert.equal(form(750, 342), 'mobile+short-landscape')
  assert.equal(form(390, 664), 'mobile')
  // Narrow and short but taller than wide: mobile, not a landscape.
  assert.equal(form(320, 480), 'mobile')
})

// --- mobile-state judge on synthetic geometry -------------------------------

const PORTRAIT = { width: 390, height: 664 }
const LANDSCAPE = { width: 750, height: 342 }
const portrait = expectationsForViewport(PORTRAIT)
const landscape = expectationsForViewport(LANDSCAPE)

const control = (name, box, extra = {}) => ({ name, tag: 'button', box, inSheet: false, inVerticalScroller: false, visibleY: null, ...extra })

function defaultGeometry(overrides = {}) {
  return {
    state: 'default',
    viewport: PORTRAIT,
    header: { x: 0, y: 0, width: 390, height: 48 },
    controlBar: { x: 0, y: 584, width: 390, height: 80 },
    launchers: {
      layers: { x: 334, y: 60, width: 44, height: 44 },
      basemap: { x: 334, y: 112, width: 44, height: 44 },
      legend: { x: 334, y: 164, width: 44, height: 44 },
    },
    map: { x: 0, y: 48, width: 390, height: 616 },
    sheet: null,
    chart: null,
    loading: false,
    panel: null,
    emptyText: null,
    anchor: { x: null, y: null },
    camera: { center: '98.000000,38.000000', zoom: '9.0000' },
    controls: [
      control('m11-launcher-layers', { x: 334, y: 60, width: 44, height: 44 }),
      control('play', { x: 8, y: 600, width: 44, height: 44 }),
    ],
    ...overrides,
  }
}

/** Portrait: the sheet covers the bottom of the map from y = 264; the anchor sits above it. */
function bottomSheetGeometry(overrides = {}) {
  return {
    state: 'river',
    viewport: PORTRAIT,
    header: { x: 0, y: 0, width: 390, height: 48 },
    controlBar: null,
    launchers: { layers: null, basemap: null, legend: null },
    map: { x: 0, y: 48, width: 390, height: 616 },
    sheet: { x: 0, y: 264, width: 390, height: 400 },
    chart: { x: 12, y: 360, width: 366, height: 200 },
    loading: false,
    panel: 'm11-river-panel-chart',
    emptyText: null,
    anchor: { x: '195', y: '156' },
    camera: { center: '98.000000,38.000000', zoom: '12.0000' },
    controls: [control('close', { x: 334, y: 272, width: 44, height: 44 }, { inSheet: true })],
    ...overrides,
  }
}

/** Short landscape: the sheet covers the right of the map from x = 390; the anchor sits left of it. */
function rightSheetGeometry(overrides = {}) {
  return {
    state: 'station',
    viewport: LANDSCAPE,
    header: { x: 0, y: 0, width: 750, height: 48 },
    controlBar: { x: 0, y: 286, width: 390, height: 56 },
    launchers: { layers: null, basemap: null, legend: null },
    map: { x: 0, y: 48, width: 750, height: 294 },
    sheet: { x: 390, y: 48, width: 360, height: 294 },
    chart: { x: 402, y: 150, width: 336, height: 140 },
    loading: false,
    panel: 'm11-station-panel-chart',
    emptyText: null,
    anchor: { x: '195', y: '195' },
    camera: { center: '98.000000,38.000000', zoom: '12.0000' },
    controls: [control('close', { x: 698, y: 52, width: 44, height: 44 }, { inSheet: true })],
    ...overrides,
  }
}

test('a default state that meets every expectation has no failure', () => {
  assert.deepEqual(judgeMobileState(defaultGeometry(), portrait), [])
})

test('open-window states that meet every expectation have no failure', () => {
  assert.deepEqual(judgeMobileState(bottomSheetGeometry(), portrait), [])
  assert.deepEqual(judgeMobileState(rightSheetGeometry(), landscape), [])
})

test('a header that is not 48 high fails the default state once', () => {
  const failures = judgeMobileState(defaultGeometry({ header: { x: 0, y: 0, width: 390, height: 84 } }), portrait)
  assert.equal(failures.length, 1)
  assert.match(failures[0], /header height 84 != 48/)
})

test('a missing control bar or launcher fails the default state once each', () => {
  const noBar = judgeMobileState(defaultGeometry({ controlBar: null }), portrait)
  assert.equal(noBar.length, 1)
  assert.match(noBar[0], /control bar is missing/)
  const launchers = { ...defaultGeometry().launchers, legend: null }
  const noLegend = judgeMobileState(defaultGeometry({ launchers }), portrait)
  assert.equal(noLegend.length, 1)
  assert.match(noLegend[0], /launcher legend is missing/)
})

test('a control bar that leaves the viewport fails the default state once', () => {
  const failures = judgeMobileState(defaultGeometry({ controlBar: { x: 0, y: 600, width: 390, height: 80 } }), portrait)
  assert.equal(failures.length, 1)
  assert.match(failures[0], /control bar .* outside/)
})

test('a control outside the viewport fails once and names the control', () => {
  for (const box of [
    { x: 360, y: 60, width: 44, height: 44 }, // past the right edge
    { x: -4, y: 60, width: 44, height: 44 }, // past the left edge
    { x: 8, y: 630, width: 44, height: 44 }, // past the bottom edge
    { x: 8, y: -3, width: 44, height: 44 }, // past the top edge
  ]) {
    const geometry = defaultGeometry()
    geometry.controls.push(control('m11-legend-toggle', box))
    const failures = judgeMobileState(geometry, portrait)
    assert.equal(failures.length, 1, JSON.stringify(box))
    assert.match(failures[0], /m11-legend-toggle.*outside the 390x664 viewport/)
  }
})

test('a control within the half-pixel edge tolerance passes', () => {
  const geometry = defaultGeometry()
  geometry.controls.push(control('edge', { x: 346.4, y: 620.4, width: 44, height: 44 }))
  assert.deepEqual(judgeMobileState(geometry, portrait), [])
})

test('a sheet control scrolled out of the sheet body is not a failure', () => {
  // Its own box lies below the viewport, but it is a member of a vertical
  // scroller and nothing of it is visible.
  const scrolledOut = control('row-40', { x: 12, y: 900, width: 366, height: 44 }, { inSheet: true, inVerticalScroller: true, visibleY: null })
  // Partly scrolled: the box overruns the viewport, the visible span does not.
  const partly = control('row-7', { x: 12, y: 640, width: 366, height: 44 }, { inSheet: true, inVerticalScroller: true, visibleY: { top: 640, bottom: 664 } })
  const geometry = bottomSheetGeometry()
  geometry.controls.push(scrolledOut, partly)
  assert.deepEqual(judgeMobileState(geometry, portrait), [])
})

test('a scroller member whose visible span leaves the viewport, or that overflows sideways, fails', () => {
  const below = control('row-9', { x: 12, y: 640, width: 366, height: 44 }, { inSheet: true, inVerticalScroller: true, visibleY: { top: 640, bottom: 684 } })
  const sideways = control('row-3', { x: 12, y: 400, width: 420, height: 44 }, { inSheet: true, inVerticalScroller: true, visibleY: null })
  for (const [name, extra] of [['row-9', below], ['row-3', sideways]]) {
    const geometry = bottomSheetGeometry()
    geometry.controls.push(extra)
    const failures = judgeMobileState(geometry, portrait)
    assert.equal(failures.length, 1, name)
    assert.match(failures[0], new RegExp(name))
  }
})

test('with a window open only the sheet controls are held to the viewport', () => {
  const geometry = bottomSheetGeometry()
  geometry.controls.push(control('behind-the-sheet', { x: 380, y: 60, width: 44, height: 44 }))
  assert.deepEqual(judgeMobileState(geometry, portrait), [])
  geometry.controls.push(control('sheet-tab', { x: 380, y: 300, width: 44, height: 44 }, { inSheet: true }))
  const failures = judgeMobileState(geometry, portrait)
  assert.equal(failures.length, 1)
  assert.match(failures[0], /sheet-tab/)
})

test('the portrait chart floor is 160: 159 fails, exactly 160 passes', () => {
  const chart = (height) => ({ x: 12, y: 360, width: 366, height })
  const failures = judgeMobileState(bottomSheetGeometry({ chart: chart(159) }), portrait)
  assert.equal(failures.length, 1)
  assert.match(failures[0], /chart area height 159 < 160/)
  assert.deepEqual(judgeMobileState(bottomSheetGeometry({ chart: chart(160) }), portrait), [])
})

test('the short-landscape chart floor is 120: 119 fails, exactly 120 passes', () => {
  const chart = (height) => ({ x: 402, y: 150, width: 336, height })
  const failures = judgeMobileState(rightSheetGeometry({ chart: chart(119) }), landscape)
  assert.equal(failures.length, 1)
  assert.match(failures[0], /chart area height 119 < 120/)
  assert.deepEqual(judgeMobileState(rightSheetGeometry({ chart: chart(120) }), landscape), [])
  // 140 clears the landscape floor but not the portrait one.
  assert.equal(judgeMobileState(bottomSheetGeometry({ chart: { x: 12, y: 360, width: 366, height: 140 } }), portrait).length, 1)
})

const WAIT_DONE = { waited_ms: 3200, limit_ms: 60000, timed_out: false }
const WAIT_TIMED_OUT = { waited_ms: 4003, limit_ms: 4000, timed_out: true }
const PENDING = { chart: null, loading: true, panel: 'm11-river-panel-pending' }
const EMPTY = { chart: null, loading: false, panel: 'm11-river-panel-empty', emptyText: '暂无 q_down 预报数据' }

test('a curve wait that ended on a chart leaves the state passing', () => {
  assert.deepEqual(judgeMobileState(bottomSheetGeometry(), portrait, WAIT_DONE), [])
  assert.deepEqual(judgeMobileState(rightSheetGeometry(), landscape, WAIT_DONE), [])
})

test('a curve wait that ran to the timeout is "still loading after <timeout> ms", with the panel state', () => {
  assert.deepEqual(judgeMobileState(bottomSheetGeometry(PENDING), portrait, WAIT_TIMED_OUT), [
    'curve still loading after 4000 ms (panel state: m11-river-panel-pending)',
  ])
  const station = judgeMobileState(rightSheetGeometry({ chart: null, loading: true, panel: 'm11-station-popup-no-product' }), landscape, { ...WAIT_TIMED_OUT, limit_ms: 60000 })
  assert.deepEqual(station, ['curve still loading after 60000 ms (panel state: m11-station-popup-no-product)'])
})

test('a timed-out curve wait fails the state even when the chart turned up before the read', () => {
  const failures = judgeMobileState(bottomSheetGeometry(), portrait, WAIT_TIMED_OUT)
  assert.deepEqual(failures, ['curve still loading after 4000 ms (panel state: m11-river-panel-chart)'])
})

test('a curve wait that finished without a chart is "no curve data", naming the terminal state seen', () => {
  assert.deepEqual(judgeMobileState(bottomSheetGeometry(EMPTY), portrait, WAIT_DONE), [
    'no curve data: the wait for the curve ended without a chart area (panel state: m11-river-panel-empty「暂无 q_down 预报数据」)',
  ])
})

test('"still loading" is never reported for a wait that did not time out', () => {
  // A busy marker at the read alone does not make it a timeout.
  for (const curveWait of [WAIT_DONE, null, undefined]) {
    const failures = judgeMobileState(bottomSheetGeometry(PENDING), portrait, curveWait)
    assert.equal(failures.length, 1)
    assert.match(failures[0], /^no curve data: .*\(panel state: m11-river-panel-pending\)$/)
    assert.doesNotMatch(failures[0], /still loading/)
  }
  const none = judgeMobileState(bottomSheetGeometry({ chart: null, panel: null }), portrait, WAIT_DONE)
  assert.deepEqual(none, ['no curve data: the wait for the curve ended without a chart area (panel state: none)'])
})

test('the chart floor is not judged on top of a curve failure', () => {
  const short = { x: 12, y: 360, width: 366, height: 100 }
  assert.deepEqual(judgeMobileState(bottomSheetGeometry({ chart: short }), portrait, WAIT_TIMED_OUT), [
    'curve still loading after 4000 ms (panel state: m11-river-panel-chart)',
  ])
  assert.deepEqual(judgeMobileState(bottomSheetGeometry({ chart: short }), portrait, WAIT_DONE), ['chart area height 100 < 160'])
})

test('missing anchor attributes fail an open-window state once', () => {
  for (const anchor of [{ x: null, y: null }, { x: '195', y: null }, { x: null, y: '156' }]) {
    const failures = judgeMobileState(bottomSheetGeometry({ anchor }), portrait)
    assert.equal(failures.length, 1)
    assert.match(failures[0], /anchor attributes are missing/)
  }
})

test('an anchor under the bottom sheet fails; on the sheet edge counts as covered', () => {
  for (const y of ['300', '264']) {
    const failures = judgeMobileState(bottomSheetGeometry({ anchor: { x: '195', y } }), portrait)
    assert.equal(failures.length, 1, y)
    assert.match(failures[0], /under the bottom sheet/)
  }
  assert.deepEqual(judgeMobileState(bottomSheetGeometry({ anchor: { x: '195', y: '263' } }), portrait), [])
})

test('an anchor under the right sheet fails; on the sheet edge counts as covered', () => {
  for (const x of ['500', '390']) {
    const failures = judgeMobileState(rightSheetGeometry({ anchor: { x, y: '195' } }), landscape)
    assert.equal(failures.length, 1, x)
    assert.match(failures[0], /under the right sheet/)
  }
  assert.deepEqual(judgeMobileState(rightSheetGeometry({ anchor: { x: '389', y: '195' } }), landscape), [])
})

test('the covered side follows the sheet side, not the other axis', () => {
  // Low on the map but left of a right sheet: clear. Right on the map but above a bottom sheet: clear.
  assert.deepEqual(judgeMobileState(rightSheetGeometry({ anchor: { x: '100', y: '330' } }), landscape), [])
  assert.deepEqual(judgeMobileState(bottomSheetGeometry({ anchor: { x: '380', y: '100' } }), portrait), [])
})

test('an anchor outside the map area fails once', () => {
  for (const anchor of [{ x: '-5', y: '156' }, { x: '195', y: '20' }, { x: '400', y: '156' }]) {
    const failures = judgeMobileState(bottomSheetGeometry({ anchor }), portrait)
    assert.equal(failures.length, 1, JSON.stringify(anchor))
    assert.match(failures[0], /outside the map area/)
  }
})

test('a missing curve window is the single failure of an open-window state', () => {
  const failures = judgeMobileState(bottomSheetGeometry({ sheet: null, chart: null, anchor: { x: null, y: null } }), portrait)
  assert.deepEqual(failures, ['curve window frame is missing'])
})

// --- the curve region's error fallback ---------------------------------------

const CURVE_REGION_CRASHED = 'the curve region crashed: its error fallback region-error-map-panels replaced the curve window (a product bug; a larger --timeout-ms does not help)'
/** What the read sees once the fallback replaced the window: no frame, no chart, the fallback as the panel. */
const CRASHED = { sheet: null, chart: null, loading: false, panel: 'region-error-map-panels' }

test('a curve region in its error fallback is the single failure of the state, and it names the fallback', () => {
  // The wait ends at once on the fallback, so it did not time out; the anchor may or may not still be there.
  for (const curveWait of [WAIT_DONE, { waited_ms: 12, limit_ms: 60000, timed_out: false }, null]) {
    assert.deepEqual(judgeMobileState(bottomSheetGeometry(CRASHED), portrait, curveWait), [CURVE_REGION_CRASHED])
    assert.deepEqual(judgeMobileState(rightSheetGeometry({ ...CRASHED, anchor: { x: null, y: null } }), landscape, curveWait), [CURVE_REGION_CRASHED])
  }
})

test('the fallback is never reported as "still loading" or as a missing frame, even after a timed-out wait', () => {
  const failures = judgeMobileState(bottomSheetGeometry(CRASHED), portrait, WAIT_TIMED_OUT)
  assert.deepEqual(failures, [CURVE_REGION_CRASHED])
  assert.doesNotMatch(failures.join('\n'), /still loading|frame is missing|no curve data/)
})

test('the fallback wins over everything else the read could fail on', () => {
  const geometry = bottomSheetGeometry({ ...CRASHED, map: null, controls: [control('retry', { x: 400, y: 100, width: 44, height: 44 }, { inSheet: true })] })
  assert.deepEqual(judgeMobileState(geometry, portrait, WAIT_DONE), [CURVE_REGION_CRASHED])
})

test('without the fallback a missing frame keeps its own message, whatever the panel marker', () => {
  for (const panel of [null, 'm11-river-panel-pending', 'region-error-map', 'region-error-map-panels-other']) {
    const failures = judgeMobileState(bottomSheetGeometry({ sheet: null, chart: null, panel }), portrait, WAIT_TIMED_OUT)
    assert.deepEqual(failures, ['curve window frame is missing'])
  }
})

test('the judge ignores the document scroll width', () => {
  const geometry = { ...defaultGeometry(), scrollWidth: 5000, clientWidth: 390 }
  assert.deepEqual(judgeMobileState(geometry, portrait), [])
})

// --- report assembly --------------------------------------------------------

function stateResult(failures = []) {
  return { url: 'https://example.test/', settle_ms: 800, tap: null, attempts: null, geometry: defaultGeometry(), screenshot: 'x.png', failures }
}

function assemble(states) {
  const preset = resolveDevicePreset('mobile-portrait')
  return assembleMobileReport({
    baseUrl: 'https://example.test',
    preset,
    expectations: expectationsForViewport(preset.viewport),
    riverTarget: { river_segment_id: 'seg_1' },
    stationTarget: { candidates: [] },
    states,
    nonGetRequests: [],
    capturedAt: '2026-10-09T00:00:00.000Z',
  })
}

test('all states passing gives pass true and exit code 0', () => {
  const { report, exitCode, reportFile } = assemble({ default: stateResult(), river: stateResult(), station: stateResult() })
  assert.equal(report.pass, true)
  assert.deepEqual(report.failures, [])
  assert.equal(exitCode, 0)
  assert.equal(reportFile, 'mobile-geometry-mobile-portrait.json')
  assert.equal(report.schema, 'nhms.node27-display-mobile-evidence.v1')
  assert.equal(report.base_url, 'https://example.test')
  assert.equal(report.captured_at, '2026-10-09T00:00:00.000Z')
  assert.deepEqual(report.preset.viewport, { width: 390, height: 664 })
  assert.equal(report.form, 'mobile')
  assert.equal(report.expectations.chartMinHeight, 160)
  assert.deepEqual(report.river_target, { river_segment_id: 'seg_1' })
  assert.deepEqual(Object.keys(report.states), ['default', 'river', 'station'])
})

test('a failure in any one state gives pass false, exit code 1 and a state-prefixed failure', () => {
  for (const failing of ['default', 'river', 'station']) {
    const states = { default: stateResult(), river: stateResult(), station: stateResult() }
    states[failing] = stateResult(['chart area height 100 < 160'])
    const { report, exitCode } = assemble(states)
    assert.equal(report.pass, false, failing)
    assert.equal(exitCode, 1)
    assert.deepEqual(report.failures, [`${failing}: chart area height 100 < 160`])
  }
})

test('failures from several states are all kept, in state order', () => {
  const { report, exitCode } = assemble({ default: stateResult(['a']), river: stateResult(), station: stateResult(['b', 'c']) })
  assert.deepEqual(report.failures, ['default: a', 'station: b', 'station: c'])
  assert.equal(exitCode, 1)
})

test('output file names carry the preset and never collide with the desktop outputs', () => {
  assert.deepStrictEqual(mobileOutputFiles('mobile-landscape'), {
    report: 'mobile-geometry-mobile-landscape.json',
    screenshots: {
      default: 'mobile-mobile-landscape-default.png',
      river: 'mobile-mobile-landscape-river.png',
      station: 'mobile-mobile-landscape-station.png',
    },
  })
  const desktop = ['browser-evidence.json', 'overview-default.png', 'overview-ifs.png', 'overview-valid-time-plus-24h.png', 'overview-precip-off.png']
  for (const preset of ['mobile-portrait', 'mobile-landscape']) {
    const files = mobileOutputFiles(preset)
    for (const file of [files.report, ...Object.values(files.screenshots)]) assert.ok(!desktop.includes(file), file)
  }
})

// --- tap targets ------------------------------------------------------------

test('river target: bbox over all coordinates, anchor at the middle of the flattened line', () => {
  const line = { type: 'LineString', coordinates: [[98.1, 38.4], [98.2, 38.3], [98.3, 38.5], [98.4, 38.1], [98.5, 38.2]] }
  assert.deepEqual(riverTargetFromGeometry(line), { bbox: [[98.1, 38.1], [98.5, 38.5]], anchor: [98.3, 38.5] })
  const multi = { type: 'MultiLineString', coordinates: [[[98.1, 38.4], [98.2, 38.3]], [[98.3, 38.5], [98.4, 38.1, 4000]]] }
  assert.deepEqual(riverTargetFromGeometry(multi), { bbox: [[98.1, 38.1], [98.4, 38.5]], anchor: [98.2, 38.3] })
})

test('river target: a zero-width or zero-height extent is padded so the bbox is not degenerate', () => {
  const { bbox } = riverTargetFromGeometry({ type: 'LineString', coordinates: [[98, 38.1], [98, 38.2]] })
  assert.ok(bbox[0][0] < 98 && bbox[1][0] > 98)
  assert.ok(bbox[1][0] - bbox[0][0] < 1e-4)
  assert.deepEqual([bbox[0][1], bbox[1][1]], [38.1, 38.2])
})

test('river target: unusable geometry gives null', () => {
  assert.equal(riverTargetFromGeometry(null), null)
  assert.equal(riverTargetFromGeometry({ type: 'Point', coordinates: [98, 38] }), null)
  assert.equal(riverTargetFromGeometry({ type: 'LineString', coordinates: [] }), null)
  assert.equal(riverTargetFromGeometry({ type: 'LineString', coordinates: [[98, 38], [Number.NaN, 38]] }), null)
  assert.equal(riverTargetFromGeometry({ type: 'LineString', coordinates: [[98, 38], [198, 38]] }), null)
})

test('station candidates: sorted by station_id, without coordinate-less stations, at most five', () => {
  const items = ['s7', 's3', 's9', 's1', 's5', 's8', 's2'].map((station_id) => ({ station_id, longitude: 98, latitude: 38, station_name: 'x' }))
  items.push({ station_id: 's0', longitude: null, latitude: 38 })
  assert.deepEqual(stationCandidates(items, null).map((item) => item.station_id), ['s1', 's2', 's3', 's5', 's7'])
  assert.deepEqual(stationCandidates(items, null)[0], { station_id: 's1', longitude: 98, latitude: 38 })
})

test('station candidates: an explicit station id selects only that station, or nothing', () => {
  const items = ['s7', 's3', 's9', 's1', 's5', 's8', 's2'].map((station_id) => ({ station_id, longitude: 98, latitude: 38 }))
  assert.deepEqual(stationCandidates(items, 's9'), [{ station_id: 's9', longitude: 98, latitude: 38 }])
  assert.deepEqual(stationCandidates(items, 'missing'), [])
})

test('station candidates: a station with only geom.coordinates is kept, located by geom', () => {
  const items = [{ station_id: 'g1', geom: { type: 'Point', coordinates: [98.5, 38.25] } }]
  assert.deepEqual(stationCandidates(items, null), [{ station_id: 'g1', longitude: 98.5, latitude: 38.25 }])
  // A third ordinate (elevation) is ignored, as on the page.
  const withElevation = [{ station_id: 'g2', geom: { type: 'Point', coordinates: [98.5, 38.25, 4100] } }]
  assert.deepEqual(stationCandidates(withElevation, 'g2'), [{ station_id: 'g2', longitude: 98.5, latitude: 38.25 }])
})

test('station candidates: geom.coordinates wins over different top-level fields', () => {
  const items = [{ station_id: 'b1', longitude: 100, latitude: 40, geom: { type: 'Point', coordinates: [98.5, 38.25] } }]
  assert.deepEqual(stationCandidates(items, null), [{ station_id: 'b1', longitude: 98.5, latitude: 38.25 }])
})

test('station candidates: top-level fields alone still locate a station', () => {
  for (const geom of [undefined, null, {}, { type: 'Point' }]) {
    assert.deepEqual(stationCandidates([{ station_id: 't1', longitude: 100, latitude: 40, geom }], null), [{ station_id: 't1', longitude: 100, latitude: 40 }])
  }
})

test('station candidates: non-finite geom.coordinates fall back to the top-level fields', () => {
  const unusable = [[Number.NaN, 38.25], [98.5, null], ['98.5', '38.25'], [98.5], [], 'POINT(98.5 38.25)']
  for (const coordinates of unusable) {
    const items = [{ station_id: 'f1', longitude: 100, latitude: 40, geom: { type: 'Point', coordinates } }]
    assert.deepEqual(stationCandidates(items, null), [{ station_id: 'f1', longitude: 100, latitude: 40 }], JSON.stringify(coordinates))
    // With nothing to fall back to the station has no coordinates and is dropped.
    assert.deepEqual(stationCandidates([{ station_id: 'f2', geom: { type: 'Point', coordinates } }], null), [], JSON.stringify(coordinates))
  }
})

// --- entry guard (subprocesses) ---------------------------------------------

const run = (argv, options = {}) => spawnSync(process.execPath, argv, { encoding: 'utf8', timeout: 30000, ...options })

test('importing the script has no side effect', () => {
  const result = run(['--input-type=module', '-e', `await import(${JSON.stringify(pathToFileURL(SCRIPT).href)})`])
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, '')
  assert.equal(result.stderr, '')
})

test('importing the script from another entry file has no side effect', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'nhms-evidence-entry-'))
  try {
    // A real second entry file: `argv[1]` exists and is not the script. It
    // imports the script directly and through a symlink, then proves it ran.
    const entry = path.join(dir, 'entry.mjs')
    symlinkSync(SCRIPT, path.join(dir, 'linked.mjs'))
    writeFileSync(entry, [
      `const direct = await import(${JSON.stringify(pathToFileURL(SCRIPT).href)})`,
      "const linked = await import('./linked.mjs')",
      "process.stdout.write(`imported ${typeof direct.parseArgs} ${typeof linked.parseArgs}\\n`)",
      '',
    ].join('\n'))
    const result = run([entry])
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.stdout, 'imported function function\n')
    assert.equal(result.stderr, '')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('feeding the script through stdin with no arguments exits 2 with the usage error', () => {
  // No file to compare `argv[1]` with: the module text itself is the entry.
  for (const argv of [['--input-type=module', '-'], ['--input-type=module']]) {
    const result = run(argv, { input: readFileSync(SCRIPT) })
    assert.equal(result.status, 2, JSON.stringify(argv))
    assert.match(result.stderr, /--out-dir is required/)
    assert.equal(result.stdout, '')
  }
})

test('running the script by absolute path with no arguments exits 2 with the usage error', () => {
  const result = run([SCRIPT])
  assert.equal(result.status, 2)
  assert.match(result.stderr, /--out-dir is required/)
  assert.equal(result.stdout, '')
})

test('running the script by relative path with no arguments exits 2 with the usage error', () => {
  const result = run([path.relative(REPO_ROOT, SCRIPT)], { cwd: REPO_ROOT })
  assert.equal(result.status, 2)
  assert.match(result.stderr, /--out-dir is required/)
})

test('running the script through a symlink with no arguments exits 2 with the usage error', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'nhms-evidence-entry-'))
  try {
    const link = path.join(dir, 'evidence-link.mjs')
    symlinkSync(SCRIPT, link)
    const result = run([link])
    assert.equal(result.status, 2)
    assert.match(result.stderr, /--out-dir is required/)
    // Relative to a symlinked directory as well.
    symlinkSync(path.dirname(SCRIPT), path.join(dir, 'scripts-link'))
    const viaDir = run([path.join('scripts-link', path.basename(SCRIPT))], { cwd: dir })
    assert.equal(viaDir.status, 2)
    assert.match(viaDir.stderr, /--out-dir is required/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a usage error on a preset run exits 2 before any browser is launched', () => {
  const result = run([SCRIPT, '--out-dir', path.join(os.tmpdir(), 'nhms-evidence-never-created'), '--device-preset', 'phablet'])
  assert.equal(result.status, 2)
  assert.match(result.stderr, /unknown --device-preset: phablet/)
})
