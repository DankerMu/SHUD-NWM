// #2537 retry receipt: national view, no zoom; step the timeline so every visible tile is cold.
import { chromium } from '@playwright/test'
import fs from 'node:fs'
const BASE = 'https://test.nwm.ac.cn'
const OUT = '/home/nwm/tmp/fe-receipt'
fs.mkdirSync(OUT, { recursive: true })
const out = []
const note = (k, v) => { const l = `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`; out.push(l); console.log(l) }
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const tiles = []
page.on('response', async (r) => {
  const u = r.url()
  if (!u.includes('/api/v1/tiles/hydro-national/')) return
  let code = null
  if (r.status() === 503) { try { code = (await r.json())?.error?.code ?? null } catch (e) { code = 'unreadable' } }
  tiles.push({ t: Date.now(), status: r.status(), url: u.replace(/^https?:\/\/[^/]+/, ''), retryAfter: r.headers()['retry-after'] ?? null, code, worker: !r.frame?.() })
})
const errors = []
page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 300)) })
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
await page.waitForFunction(() => document.querySelector('[data-testid="m11-map-surface"]')?.getAttribute('data-registered-overlays') === 'discharge', null, { timeout: 60000 })
await page.waitForTimeout(6000)
const next = page.getByRole('button', { name: '下一个有效时刻' })
for (let i = 1; i <= 4; i += 1) {
  await next.click()
  await page.waitForTimeout(12000)
  note(`step${i}_validTime`, new URL(page.url()).searchParams.get('validTime'))
}
// Was the tile fetched from the main thread (custom protocol) rather than the worker's built-in fetch?
const mainThreadTileEntries = await page.evaluate(() => performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/v1/tiles/hydro-national/')).length)
note('main_thread_hydro_tile_resource_entries', mainThreadTileEntries)
const busy = tiles.filter((t) => t.status === 503)
const perUrl = [...new Set(busy.map((b) => b.url))].map((u) => {
  const seq = tiles.filter((t) => t.url === u).map((t) => t.status)
  const first503 = Math.min(...busy.filter((b) => b.url === u).map((b) => b.t))
  const ok = tiles.find((t) => t.url === u && t.status === 200 && t.t > first503)
  return { url: u.slice(0, 120), seq, recovered_after_ms: ok ? ok.t - first503 : null }
})
note('hydro_tile_statuses', tiles.reduce((a, t) => ((a[t.status] = (a[t.status] ?? 0) + 1), a), {}))
note('busy_503_count', busy.length)
note('busy_codes', [...new Set(busy.map((b) => `${b.code}|retry-after=${b.retryAfter}`))])
note('busy_urls', perUrl.length)
note('busy_urls_recovered', perUrl.filter((p) => p.recovered_after_ms !== null).length)
note('busy_url_detail', perUrl.slice(0, 12))
note('source_error_banner', (await page.locator('[data-testid="m11-map-source-error"]').count()) ? await page.locator('[data-testid="m11-map-source-error"]').innerText() : 'none')
note('non_resource_console_errors', errors.slice(0, 10))
await page.screenshot({ path: `${OUT}/07-after-cold-steps.png` })
fs.writeFileSync(`${OUT}/retry-receipt.txt`, out.join('\n') + '\n')
fs.writeFileSync(`${OUT}/retry-tiles.json`, JSON.stringify(tiles, null, 1))
await browser.close()
