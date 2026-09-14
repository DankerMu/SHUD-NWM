// #2336 live receipt capture against https://test.nwm.ac.cn (read-only browser session).
const path = require('path')
const fs = require('fs')
const { chromium } = require(path.resolve(__dirname, '../../../../apps/frontend/node_modules/@playwright/test'))

const BASE = process.env.BASE || 'https://test.nwm.ac.cn'
const OUT = process.env.OUT || path.resolve(process.cwd(), '.workplans/issue-2336/evidence')
fs.mkdirSync(OUT, { recursive: true })
const log = []
const say = (line) => { console.log(line); log.push(line) }

const classify = (url) => {
  const u = new URL(url)
  if (u.origin !== new URL(BASE).origin) return 'external'
  if (u.pathname.startsWith('/api/v1/tiles/')) return 'tile'
  if (u.pathname.startsWith('/api/')) return 'api'
  return 'static'
}

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

async function phase(page, reqs, name, action) {
  const mark = reqs.length
  const loadingSeen = await page.evaluate(() => {
    window.__loadingHits = 0
    window.__obs?.disconnect()
    window.__obs = new MutationObserver(() => {
      if (document.querySelector('[data-testid="m11-overview-loading"]')) window.__loadingHits += 1
    })
    window.__obs.observe(document.body, { childList: true, subtree: true })
    return 0
  })
  void loadingSeen
  const before = await page.locator('input[aria-label="有效时间滑块"]').inputValue()
  await action()
  const after = await page.locator('input[aria-label="有效时间滑块"]').inputValue()
  const hits = await page.evaluate(() => window.__loadingHits)
  const slice = reqs.slice(mark)
  const api = slice.filter((r) => r.kind === 'api')
  const tiles = slice.filter((r) => r.kind === 'tile')
  say(`## ${name}`)
  say(`slider index: ${before} -> ${after}`)
  say(`loading notice appearances (MutationObserver): ${hits}`)
  say(`new /api (non-tile) requests: ${api.length}`)
  for (const r of api) say(`  API ${r.method} ${r.url}`)
  const tileValidTimes = new Set(tiles.map((r) => decodeURIComponent(r.url).match(/\/(\d{4}-\d{2}-\d{2}T[^/]+)\/\d+\/\d+\/\d+\.pbf/)?.[1]).filter(Boolean))
  say(`new tile requests: ${tiles.length} across ${tileValidTimes.size} valid_time(s)`)
  say(`tile sample: ${tiles.slice(0, 3).map((r) => r.url).join(' | ')}`)
  await page.screenshot({ path: path.join(OUT, `${name}.png`) })
}

;(async () => {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  const reqs = []
  page.on('request', (r) => reqs.push({ t: Date.now(), method: r.method(), url: r.url(), kind: classify(r.url()) }))
  const statuses = []
  page.on('response', (r) => { if (classify(r.url()) !== 'static') statuses.push(`${r.status()} ${r.url()}`) })

  // 1. default load + smoke
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  await page.locator('[data-testid="m11-bottom-control-bar"]').waitFor({ timeout: 60000 })
  await settle(page)
  const bundle = await page.evaluate(() => [...document.scripts].map((s) => s.src).find((s) => /index-.*\.js/.test(s)))
  say(`# #2336 receipt capture ${new Date().toISOString()}`)
  say(`url: ${page.url()}`)
  say(`bundle: ${bundle}`)
  say(`initial requests: api=${reqs.filter((r) => r.kind === 'api').length} tile=${reqs.filter((r) => r.kind === 'tile').length}`)
  for (const r of reqs.filter((x) => x.kind === 'api')) say(`  API ${r.method} ${r.url}`)
  const tileOk = statuses.filter((s) => s.includes('/api/v1/tiles/hydro-national/'))
  say(`hydro-national tile responses: ${tileOk.length} (status counts: ${JSON.stringify(tileOk.reduce((a, s) => { const k = s.split(' ')[0]; a[k] = (a[k] || 0) + 1; return a }, {}))})`)
  const nonOkApi = statuses.filter((s) => !s.startsWith('200') && !s.startsWith('304'))
  say(`non-2xx api/tile responses: ${nonOkApi.length}`)
  for (const s of nonOkApi.slice(0, 10)) say(`  ${s}`)
  say(`disabled reason: ${await page.locator('[data-testid="m11-control-bar-disabled-reason"]').textContent().catch(() => null)}`)
  const slider = page.locator('input[aria-label="有效时间滑块"]')
  say(`slider range: min=${await slider.getAttribute('min')} max=${await slider.getAttribute('max')} value=${await slider.inputValue()}`)
  await page.screenshot({ path: path.join(OUT, '01-default-load.png') })

  // 2. 1x playback
  await phase(page, reqs, '02-play-1x', async () => {
    await page.locator('select[aria-label="播放速度"]').selectOption('1')
    await page.locator('button[aria-label="播放时间轴"]').click()
    await page.waitForTimeout(6500)
    await page.locator('button[aria-label="暂停时间轴"]').click()
    await page.waitForTimeout(1500)
  })
  // 3. 4x playback
  await phase(page, reqs, '03-play-4x', async () => {
    await page.locator('select[aria-label="播放速度"]').selectOption('4')
    await page.locator('button[aria-label="播放时间轴"]').click()
    await page.waitForTimeout(4000)
    const pause = page.locator('button[aria-label="暂停时间轴"]')
    if (await pause.count()) await pause.click()
    await page.waitForTimeout(1500)
  })
  // 4. slider drag (keyboard steps + set to start)
  await phase(page, reqs, '04-slider', async () => {
    await slider.focus()
    for (let i = 0; i < 6; i += 1) { await page.keyboard.press('ArrowLeft'); await page.waitForTimeout(200) }
    await page.keyboard.press('Home')
    await page.waitForTimeout(500)
    for (let i = 0; i < 4; i += 1) { await page.keyboard.press('ArrowRight'); await page.waitForTimeout(200) }
    await page.waitForTimeout(1500)
  })

  // 5. IFS unlisted cycle wording
  const page2 = await context.newPage()
  await page2.goto(`${BASE}/?source=ifs&cycle=1999-01-01T00:00:00Z`, { waitUntil: 'domcontentloaded' })
  await page2.locator('[data-testid="m11-bottom-control-bar"]').waitFor({ timeout: 60000 })
  await settle(page2)
  say('## 05-ifs-unlisted-cycle')
  say(`url: ${page2.url()}`)
  say(`disabled reason: ${await page2.locator('[data-testid="m11-control-bar-disabled-reason"]').textContent().catch(() => null)}`)
  await page2.screenshot({ path: path.join(OUT, '05-ifs-unlisted-cycle.png') })

  fs.writeFileSync(path.join(OUT, 'capture.log'), `${log.join('\n')}\n`)
  fs.writeFileSync(path.join(OUT, 'requests.jsonl'), reqs.map((r) => JSON.stringify(r)).join('\n'))
  await browser.close()
})().catch((err) => { console.error(err); process.exit(1) })
