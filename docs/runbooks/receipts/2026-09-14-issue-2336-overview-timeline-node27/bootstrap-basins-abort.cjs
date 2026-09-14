const path = require('path')
const { chromium } = require(process.cwd() + '/apps/frontend/node_modules/@playwright/test')
;(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1440, height: 900 } })
  let n = 0
  await p.route('**/api/v1/basins?**', (route) => { n += 1; return n === 1 ? route.abort('failed') : route.continue() })
  const reqs = []; p.on('request', (r) => { if (r.url().includes('/api/v1/basins')) reqs.push(r.url()) })
  await p.goto('https://test.nwm.ac.cn/'); await p.waitForTimeout(15000)
  console.log('basins requests:', reqs.length, 'intercept count:', n)
  for (const id of ['m11-overview-empty', 'm11-overview-loading', 'm11-precip-notice']) {
    const loc = p.locator(`[data-testid="${id}"]`)
    console.log(id, await loc.count() ? await loc.textContent() : null)
  }
  await p.screenshot({ path: path.resolve((process.env.OUT || '.workplans/issue-2336/evidence') + '/06-bootstrap-basins-abort.png') })
  await b.close()
})()
