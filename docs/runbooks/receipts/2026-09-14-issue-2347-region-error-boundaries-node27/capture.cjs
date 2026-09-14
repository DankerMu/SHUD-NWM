// #2347 E8 live receipt against https://test.nwm.ac.cn (read-only browser session).
// Run A: unmodified default page -> zero fallbacks. Run B: client-side injection of
// `cycles: [null, ...original]` into the gfs /cycles response -> control-bar fallback only.
const path = require('path')
const fs = require('fs')
const { chromium } = require(path.resolve(__dirname, '../../../../apps/frontend/node_modules/@playwright/test'))

const BASE = process.env.BASE || 'https://test.nwm.ac.cn'
const OUT = process.env.OUT || path.resolve(process.cwd(), '.workplans/issue-2347/evidence')
fs.mkdirSync(OUT, { recursive: true })
const log = []
const say = (line) => { console.log(line); log.push(line) }
const FALLBACKS = '[data-testid^="region-error-"], [data-testid="route-error-fallback"]'

async function settle(page, quietMs = 4000, maxMs = 60000) {
  const start = Date.now()
  let last = Date.now()
  const bump = () => { last = Date.now() }
  page.on('request', bump)
  page.on('requestfinished', bump)
  while (Date.now() - last < quietMs && Date.now() - start < maxMs) await page.waitForTimeout(250)
  page.off('request', bump)
  page.off('requestfinished', bump)
}

async function run(browser, name, inject) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const tiles = []
  const consoleErrors = []
  let injected = 0
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 160)) })
  page.on('response', (res) => {
    const u = new URL(res.url())
    if (u.pathname.startsWith('/api/v1/tiles/hydro-national/')) tiles.push(res.status())
  })
  if (inject) {
    await page.route('**/api/v1/layers/discharge/cycles?*', async (route) => {
      const url = new URL(route.request().url())
      if (url.searchParams.get('source') !== 'gfs') return route.continue()
      const res = await route.fetch()
      const body = await res.json()
      body.data.cycles = [null, ...body.data.cycles]
      injected += 1
      say(`[${name}] injected null into ${url.pathname}${url.search} (default_cycle=${body.data.default_cycle}, cycles=${body.data.cycles.length})`)
      return route.fulfill({ response: res, json: body })
    })
  }
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  await settle(page)
  const fallbackIds = await page.locator(FALLBACKS).evaluateAll((els) => els.map((el) => `${el.dataset.testid}:${el.textContent.trim()}`))
  const count = async (sel) => page.locator(sel).count()
  say(`## ${name}`)
  say(`url=${page.url()} injected=${injected}`)
  say(`fallbacks=${JSON.stringify(fallbackIds)}`)
  say(`header=${await count('header')} fullscreen-map=${await count('[data-testid="m11-fullscreen-map"]')} control-bar=${await count('[data-testid="m11-bottom-control-bar"]')} canvas=${await count('canvas')}`)
  say(`hydro-national tiles: total=${tiles.length} 200=${tiles.filter((s) => s === 200).length} other=${JSON.stringify(tiles.filter((s) => s !== 200))}`)
  say(`boundary console.error lines=${consoleErrors.filter((t) => t.includes('RegionErrorBoundary')).length}`)
  await page.screenshot({ path: path.join(OUT, `${name}.png`) })
  await page.close()
}

;(async () => {
  const browser = await chromium.launch()
  say(`captured_at=${new Date().toISOString()} base=${BASE}`)
  await run(browser, 'a-unmodified-default', false)
  await run(browser, 'b-injected-null-cycle', true)
  await browser.close()
  fs.writeFileSync(path.join(OUT, 'capture.log'), log.join('\n') + '\n')
})()
