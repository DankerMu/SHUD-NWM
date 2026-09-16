// Isolated node-27 target-build smoke. No route mocks or production writes.
const path = require('node:path')
const fs = require('node:fs')
const assert = require('node:assert/strict')
const { chromium } = require(path.resolve(process.cwd(), 'apps/frontend/node_modules/@playwright/test'))
const base = process.env.BASE || 'http://127.0.0.1:18023'
const role = process.env.SMOKE_ROLE || 'viewer'
const out = process.env.OUT || '.workplans/issue-2023/evidence/live'
fs.mkdirSync(out, { recursive: true })
const overlap = (a, b) => Math.min(a.x+a.width,b.x+b.width)>Math.max(a.x,b.x) && Math.min(a.y+a.height,b.y+b.height)>Math.max(a.y,b.y)
;(async () => {
  const browser = await chromium.launch()
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
    const requests = []
    page.on('request', req => {
      if (new URL(req.url()).pathname.startsWith('/api/')) {
        assert.ok(['GET','HEAD'].includes(req.method()), `unexpected mutation ${req.method()}`)
        requests.push({ method:req.method(), path:new URL(req.url()).pathname })
      }
    })
    const runtime = await page.request.get(`${base}/api/v1/runtime/config`)
    assert.equal(runtime.status(),200)
    const capabilities = (await runtime.json()).data
    assert.equal(capabilities.service_role,'display_readonly')
    assert.equal(capabilities.control_mutations_enabled,false)
    console.log(JSON.stringify({role,base,capabilities,sha:process.env.SOURCE_SHA}))
    if (role === 'viewer') {
      await page.goto(base+'/', {waitUntil:'domcontentloaded'})
      await page.getByTestId('m11-bottom-control-bar').waitFor({timeout:60000})
      await page.locator('.maplibregl-ctrl-attrib').waitFor({timeout:60000})
      for (const viewport of [{width:1920,height:1080},{width:1440,height:900},{width:1280,height:900},{width:800,height:900},{width:520,height:900},{width:1280,height:600}]) {
        await page.setViewportSize(viewport)
        await page.waitForTimeout(500)
        const boxes = {}
        for (const [name,selector] of Object.entries({bar:'[data-testid="m11-bottom-control-bar"]',attribution:'.maplibregl-ctrl-attrib',legend:'[data-testid="m11-floating-legend"]',map:'[data-testid="m11-fullscreen-map"]'})) boxes[name] = await page.locator(selector).boundingBox()
        console.log(JSON.stringify({viewport,boxes}))
        assert.ok(boxes.bar && boxes.attribution && boxes.map)
        assert.ok(!overlap(boxes.bar,boxes.attribution),'bar overlaps attribution')
        if(boxes.legend) assert.ok(!overlap(boxes.legend,boxes.attribution),'legend overlaps attribution')
        assert.ok(boxes.map.y+boxes.map.height <= viewport.height+1,'map clipped below viewport')
        const scroll = await page.evaluate(() => ({y:window.scrollY,x:window.scrollX,height:document.documentElement.scrollHeight,client:document.documentElement.clientHeight,width:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth}))
        assert.equal(scroll.y,0); assert.equal(scroll.x,0)
        assert.ok(scroll.height<=scroll.client+1 && scroll.width<=scroll.cw+1,'document overflow')
        await page.screenshot({path:path.join(out,`map-${viewport.width}-${viewport.height}.jpg`),type:'jpeg',quality:70})
      }
    } else {
      const routes = role==='operator' ? ['/ops','/monitoring'] : ['/system/model-assets']
      await page.setViewportSize({width:1280,height:600})
      for (const route of routes) {
        await page.goto(base+route,{waitUntil:'domcontentloaded'})
        await page.waitForTimeout(4000)
        assert.equal(await page.getByLabel('Role',{exact:true}).count(),0,'built smoke must not expose dev Role selector')
        assert.equal(await page.getByText('权限不足',{exact:true}).count(),0,'route blocked')
        const before = await page.locator('main').evaluate(main => {
          const candidates = [...main.querySelectorAll('*')].filter(e=>['auto','scroll'].includes(getComputedStyle(e).overflowY) && e.clientHeight>0)
          const e = candidates.find(e=>e.scrollHeight>e.clientHeight+1) || candidates[0]
          if(!e) throw new Error('missing operational scroll container')
          e.dataset.receiptScroll='true'
          const rect=e.getBoundingClientRect()
          return {scrollTop:e.scrollTop,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,x:rect.x,y:rect.y,width:rect.width,height:rect.height,overflow:getComputedStyle(e).overflowY}
        })
        await page.mouse.move(before.x+before.width/2,before.y+Math.min(before.height/2,200))
        for(let n=0;n<8;n++){await page.mouse.wheel(0,2000);await page.waitForTimeout(150)}
        const after = await page.locator('[data-receipt-scroll]').evaluate(e=>({scrollTop:e.scrollTop,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,windowY:window.scrollY,windowX:window.scrollX}))
        console.log(JSON.stringify({route,role,before,after}))
        if(before.scrollHeight>before.clientHeight+1) assert.ok(after.scrollTop>before.scrollTop,'wheel did not move content')
        assert.ok(after.scrollHeight-after.clientHeight-after.scrollTop<=2,'wheel did not reach bottom')
        assert.equal(after.windowY,0);assert.equal(after.windowX,0)
        await page.screenshot({path:path.join(out,route.replaceAll('/','_')+'.jpg'),type:'jpeg',quality:70})
      }
    }
    fs.writeFileSync(path.join(out,`requests-${role}.json`),JSON.stringify(requests,null,2))
    console.log(`PASS isolated live-API browser smoke role=${role}`)
  } finally { await browser.close() }
})().catch(error=>{console.error(error);process.exitCode=1})
