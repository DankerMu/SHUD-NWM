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
 * Exit codes: 0 every assertion held, 1 at least one failed, 2 usage/launch error.
 */

import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'

const TITLE = '全国水文模拟系统（V2.0）'
const HEADER_HEIGHT = 84
const CONTROL_BAR_HEIGHT = 64
const TILE_RE = /\/api\/v1\/tiles\/hydro-national\//
const PRECIP_PNG_RE = /\/api\/v1\/precip\/.*\.png(\?|$)/
const PRECIP_INDEX_RE = /\/api\/v1\/precip\/[^?]*\/index(\?|$)/

function parseArgs(argv) {
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
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]
    const value = argv[i + 1]
    if (key === '--base-url') { args.baseUrl = value; i += 1 }
    else if (key === '--out-dir') { args.outDir = value; i += 1 }
    else if (key === '--viewport') { args.viewport = value; i += 1 }
    else if (key === '--settle-budget-ms') { args.settleBudgetMs = Number(value); i += 1 }
    else if (key === '--timeout-ms') { args.timeoutMs = Number(value); i += 1 }
    else if (key === '--playwright-root') { args.playwrightRoot = value; i += 1 }
    else if (key === '--settle-samples') { args.settleSamples = Number(value); i += 1 }
    else { throw new Error(`unknown argument: ${key}`) }
  }
  if (!args.outDir) throw new Error('--out-dir is required')
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

async function main() {
  const args = parseArgs(process.argv.slice(2))
  await mkdir(args.outDir, { recursive: true })
  const chromium = await loadChromium(args.playwrightRoot)
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
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

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    process.stderr.write(`${error?.stack ?? error}\n`)
    process.exit(2)
  })
