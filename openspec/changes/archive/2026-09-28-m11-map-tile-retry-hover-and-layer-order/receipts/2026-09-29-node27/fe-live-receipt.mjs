// FE batch (#2537 #2628 #2650) live receipt on https://test.nwm.ac.cn — read-only browser session.
import { chromium } from '@playwright/test'
import fs from 'node:fs'

const BASE = process.env.BASE ?? 'https://test.nwm.ac.cn'
const OUT = process.env.OUT ?? '/home/nwm/tmp/fe-receipt'
fs.mkdirSync(OUT, { recursive: true })
const log = []
const note = (k, v) => { const line = `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`; log.push(line); console.log(line) }

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const tiles = []
const consoleErrors = []
page.on('response', async (r) => {
  const u = r.url()
  if (!u.includes('/api/v1/tiles/')) return
  let code = null
  if (r.status() === 503) { try { code = JSON.parse(await r.text())?.error?.code ?? null } catch {} }
  tiles.push({ t: Date.now(), status: r.status(), url: u, retryAfter: r.headers()['retry-after'] ?? null, code })
})
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)) })
page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e).slice(0, 300)))

const surface = page.locator('[data-testid="m11-map-surface"]')
const sourceError = page.locator('[data-testid="m11-map-source-error"]')

// Read-only style inspection: walk React fiber from the map container to react-map-gl's map instance.
async function layerOrder() {
  return page.evaluate(() => {
    const el = document.querySelector('.maplibregl-map')
    if (!el) return null
    const key = Object.keys(el).find((k) => k.startsWith('__reactFiber$'))
    let fiber = key ? el[key] : null
    for (let depth = 0; fiber && depth < 8; depth += 1, fiber = fiber.return) {
      for (let h = fiber.memoizedState; h && typeof h === 'object' && 'next' in h; h = h.next) {
        const v = h.memoizedState
        const map = v?.map?.getLayersOrder ? v.map : v?.getMap?.()?.getLayersOrder ? v.getMap() : v?.current?.getMap?.()
        if (map?.getLayersOrder) return map.getLayersOrder()
      }
    }
    return null
  })
}
function orderVerdict(order) {
  if (!order) return { ok: null, reason: 'map not reachable' }
  const station = ['clusters', 'cluster-count', 'met-stations-point', 'met-stations-selected-halo', 'met-stations-selected-point']
  const st = order.map((id, i) => (station.includes(id) ? i : -1)).filter((i) => i >= 0)
  const river = order.map((id, i) => (/discharge|m11-overlay|hydro/i.test(id) && !station.includes(id) ? i : -1)).filter((i) => i >= 0)
  const others = order.filter((id) => !station.includes(id)).slice(-4)
  return { ok: st.length === 0 ? null : Math.min(...st) > Math.max(-1, ...river), stationIdx: st, maxRiverIdx: Math.max(-1, ...river), topNonStation: others, top: order.slice(-8) }
}

// ---------- 1. load ----------
await page.goto(BASE + '/?metStations=1', { waitUntil: 'domcontentloaded' })
await surface.waitFor({ timeout: 60000 })
await page.waitForFunction(() => document.querySelector('[data-testid="m11-map-surface"]')?.getAttribute('data-registered-overlays') === 'discharge', null, { timeout: 60000 })
await page.waitForTimeout(8000)
note('bundle', await page.evaluate(() => [...document.scripts].map((s) => s.src).filter(Boolean).join(',')))
note('overlay_source_type', await surface.getAttribute('data-overlay-source-type'))
note('station_feature_count', await surface.getAttribute('data-met-station-feature-count'))
note('initial_tile_statuses', tiles.reduce((a, t) => ((a[t.status] = (a[t.status] ?? 0) + 1), a), {}))
note('initial_layer_order', orderVerdict(await layerOrder()))
await page.screenshot({ path: `${OUT}/01-initial.png` })

// ---------- 2. fast zoom (cold tiles) ----------
const box = await page.locator('.maplibregl-canvas').boundingBox()
const zoomAt = { x: box.x + box.width * 0.78, y: box.y + box.height * 0.22 } // north-east China (HLJ)
await page.mouse.move(zoomAt.x, zoomAt.y)
const zoomStart = Date.now()
const nextBtn = page.getByRole('button', { name: '下一个有效时刻' })
// a fresh valid time makes every visible tile cold, then zoom fast
await nextBtn.click()
for (let i = 0; i < 7; i += 1) { await page.mouse.wheel(0, -400); await page.waitForTimeout(250) }
await page.waitForTimeout(15000)
const zoomTiles = tiles.filter((t) => t.t >= zoomStart)
const busy = zoomTiles.filter((t) => t.status === 503)
const busyUrls = [...new Set(busy.map((t) => t.url))]
const recovered = busyUrls.filter((u) => zoomTiles.some((t) => t.url === u && t.status === 200 && t.t > Math.min(...busy.filter((b) => b.url === u).map((b) => b.t))))
note('zoom_tile_statuses', zoomTiles.reduce((a, t) => ((a[t.status] = (a[t.status] ?? 0) + 1), a), {}))
note('zoom_busy_503', busy.map((b) => ({ code: b.code, retryAfter: b.retryAfter, url: b.url.replace(/^https?:\/\/[^/]+/, '').slice(0, 140) })).slice(0, 10))
note('zoom_busy_urls', busyUrls.length)
note('zoom_busy_recovered_200', recovered.length)
note('source_error_banner_after_zoom', (await sourceError.count()) > 0 ? await sourceError.innerText() : 'none')
await page.screenshot({ path: `${OUT}/02-after-fast-zoom.png` })

// ---------- 3. hover ----------
let hover = null
const t0 = Date.now()
await page.evaluate(() => { window.__lt = 0; try { new PerformanceObserver((l) => { window.__lt += l.getEntries().length }).observe({ type: 'longtask', buffered: false }) } catch {} })
outer: for (let gy = 0.15; gy <= 0.9; gy += 0.03) {
  for (let gx = 0.1; gx <= 0.9; gx += 0.02) {
    const x = box.x + box.width * gx, y = box.y + box.height * gy
    await page.mouse.move(x, y)
    const id = await surface.getAttribute('data-hovered-segment-id')
    if (id && !hover) { hover = { x, y, id }; break outer }
  }
}
note('hover_sweep_ms', Date.now() - t0)
note('hover_found', hover)
if (hover) {
  // jiggle within the segment: id must stay the same
  await page.mouse.move(hover.x + 1, hover.y); await page.mouse.move(hover.x, hover.y + 1); await page.mouse.move(hover.x, hover.y)
  note('hover_id_stable', (await surface.getAttribute('data-hovered-segment-id')) === hover.id)
  await page.screenshot({ path: `${OUT}/03-hover.png`, clip: { x: Math.max(0, hover.x - 200), y: Math.max(0, hover.y - 150), width: 400, height: 300 } })
  await page.mouse.move(box.x + 5, box.y + box.height - 5)
  await page.waitForTimeout(300)
  note('hover_after_leave_segment', (await surface.getAttribute('data-hovered-segment-id')) || '<empty>')
  await page.mouse.move(hover.x, hover.y)
  await page.waitForTimeout(300)
  await page.mouse.click(hover.x, hover.y)
  await page.waitForTimeout(4000)
  note('selected_segment_after_click', await surface.getAttribute('data-selected-segment-id'))
  note('hover_while_selected', await surface.getAttribute('data-hovered-segment-id'))
  await page.screenshot({ path: `${OUT}/04-selected-over-hover.png`, clip: { x: Math.max(0, hover.x - 200), y: Math.max(0, hover.y - 150), width: 400, height: 300 } })
  await page.screenshot({ path: `${OUT}/05-selected-full.png` })
}
note('longtasks_during_hover', await page.evaluate(() => window.__lt))

// ---------- 4. station order across timeline steps ----------
await page.keyboard.press('Escape')
await page.mouse.wheel(0, 1600); await page.waitForTimeout(3000) // back out to regional view
await page.waitForFunction(() => Number(document.querySelector('[data-testid="m11-map-surface"]')?.getAttribute('data-met-station-feature-count') ?? 0) > 0, null, { timeout: 90000 }).catch(() => undefined)
note('station_feature_count_before_steps', await surface.getAttribute('data-met-station-feature-count'))
note('pre_step_layer_order', orderVerdict(await layerOrder()))
for (let step = 1; step <= 3; step += 1) {
  await page.getByRole('button', { name: '下一个有效时刻' }).click()
  await page.waitForTimeout(5000)
  note(`step${step}_validTime`, new URL(page.url()).searchParams.get('validTime'))
  note(`step${step}_layer_order`, orderVerdict(await layerOrder()))
  await page.screenshot({ path: `${OUT}/06-step${step}.png` })
}
note('source_error_banner_final', (await sourceError.count()) > 0 ? await sourceError.innerText() : 'none')
note('console_errors', consoleErrors.slice(0, 20))
note('ajaxerror_console_count', consoleErrors.filter((e) => /AJAXError/.test(e)).length)
note('total_tile_statuses', tiles.reduce((a, t) => ((a[t.status] = (a[t.status] ?? 0) + 1), a), {}))
fs.writeFileSync(`${OUT}/receipt.txt`, log.join('\n') + '\n')
fs.writeFileSync(`${OUT}/tiles.json`, JSON.stringify(tiles.map((t) => ({ ...t, url: t.url.replace(/^https?:\/\/[^/]+/, '') })), null, 1))
await browser.close()
