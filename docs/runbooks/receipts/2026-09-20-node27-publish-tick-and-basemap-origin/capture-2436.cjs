// #2436 live browser receipt — Tianditu basemap on the provider-permitted origin.
// Read-only: navigates the public display origin, asserts every request it issues is GET/HEAD.
// Key hygiene: provider URLs are never logged; only {host, T, status} is recorded.
const path = require('node:path')
const fs = require('node:fs')
const assert = require('node:assert/strict')
const { chromium } = require(path.resolve(process.cwd(), 'apps/frontend/node_modules/@playwright/test'))

const base = process.env.BASE || 'https://test.nwm.ac.cn'
const out = process.env.OUT || '/home/nwm/nwm-2436-evidence/live'
fs.mkdirSync(out, { recursive: true })

const basemaps = [
  { value: 'vector', baseT: 'vec_w', annoT: 'cva_w' },
  { value: 'terrain', baseT: 'ter_w', annoT: 'cta_w' },
  { value: 'satellite', baseT: 'img_w', annoT: 'cia_w' },
]

;(async () => {
  const browser = await chromium.launch()
  const summary = []
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
    let provider = []
    page.on('response', (res) => {
      let u
      try { u = new URL(res.url()) } catch { return }
      if (!u.hostname.endsWith('tianditu.gov.cn')) return
      provider.push({ host: u.hostname, T: u.searchParams.get('T'), status: res.status() })
    })
    page.on('request', (req) => {
      let u
      try { u = new URL(req.url()) } catch { return }
      if (u.hostname.endsWith('tianditu.gov.cn')) {
        assert.equal(req.method(), 'GET', 'provider request must be GET')
      }
      if (u.pathname.startsWith('/api/')) {
        assert.ok(['GET', 'HEAD'].includes(req.method()), `unexpected mutation ${req.method()} ${u.pathname}`)
      }
    })

    for (const bm of basemaps) {
      provider = []
      await page.goto(`${base}/?basemap=${bm.value}`, { waitUntil: 'domcontentloaded' })
      await page.getByTestId('m11-bottom-control-bar').waitFor({ timeout: 60000 })
      await page.locator('.maplibregl-ctrl-attrib').waitFor({ timeout: 60000 })
      await page.waitForTimeout(10000)

      const byT = {}
      for (const r of provider) {
        const k = `${r.T}:${r.status}`
        byT[k] = (byT[k] || 0) + 1
      }
      const providerErrorVisible = await page.getByTestId('m11-map-source-error').isVisible()
      const attribution = (await page.locator('.maplibregl-ctrl-attrib').innerText()).trim()
      const hydrologyVisible = await page.getByTestId('m11-layer-group-hydrology').isVisible().catch(() => false)
      const legendVisible = await page.getByTestId('m11-floating-legend').isVisible().catch(() => false)
      const precipToggleVisible = await page.getByTestId('m11-layer-toggle-precip').isVisible().catch(() => false)
      const statuses = [...new Set(provider.map((r) => r.status))].sort()
      const baseHits = provider.filter((r) => r.T === bm.baseT)
      const annoHits = provider.filter((r) => r.T === bm.annoT)

      const row = {
        basemap: bm.value,
        providerRequests: provider.length,
        statuses,
        baseLayer: { T: bm.baseT, requests: baseHits.length, non200: baseHits.filter((r) => r.status !== 200).length },
        annotationLayer: { T: bm.annoT, requests: annoHits.length, non200: annoHits.filter((r) => r.status !== 200).length },
        byT,
        providerErrorVisible,
        attributionText: attribution,
        hydrologyVisible,
        legendVisible,
        precipToggleVisible,
      }
      summary.push(row)
      console.log(JSON.stringify(row))

      assert.ok(provider.length > 0, `${bm.value}: no provider request observed`)
      assert.ok(baseHits.length > 0, `${bm.value}: no ${bm.baseT} request observed`)
      assert.ok(annoHits.length > 0, `${bm.value}: no ${bm.annoT} request observed`)
      assert.deepEqual(statuses, [200], `${bm.value}: provider returned non-200 ${JSON.stringify(statuses)}`)
      assert.equal(providerErrorVisible, false, `${bm.value}: map-source-error banner visible`)
      assert.ok(attribution.includes('天地图'), `${bm.value}: Tianditu attribution missing`)

      await page.screenshot({ path: path.join(out, `basemap-${bm.value}.jpg`), type: 'jpeg', quality: 70 })
    }
    fs.writeFileSync(path.join(out, 'provider-summary.json'), JSON.stringify({ base, capturedAt: new Date().toISOString(), summary }, null, 2))
    console.log('RESULT: PASS')
  } finally {
    await browser.close()
  }
})().catch((e) => {
  console.error('RESULT: FAIL', e && e.message)
  process.exit(1)
})
