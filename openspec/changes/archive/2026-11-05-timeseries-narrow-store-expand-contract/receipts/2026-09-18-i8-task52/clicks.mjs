// #1987 task 5.2: `/` per-segment CLICK evidence, production https://test.nwm.ac.cn.
// ONE real mouse click per network: the curve panel loads the GFS and the IFS run
// together, so a single click is the two-curve evidence — a second click on an already
// selected segment fires nothing, which is what the first attempt mis-measured.
// Read-only: navigation and clicks only.
import { chromium } from '/home/nwm/NWM/apps/frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs';
import { writeFileSync } from 'node:fs';

const OUT = '/home/nwm/tmp/1987/shots';
const ONLY = process.env.ONLY_LABEL;
const PICKS = [
  ['large-shj-nj',   'basins_shj_nj_rivnet_vbasins',   123.69417, 47.59333, 32018],
  ['medium-qhh',     'basins_qhh_rivnet_vbasins',       99.37500, 37.44667,  3266],
  ['small-tailanhe', 'basins_tailanhe_rivnet_vbasins',  80.41500, 41.95167,   126, 'basins_tailanhe_shud_shud_riv_000001'],
];
const FIND_MAP = () => {
  const seen = new Set(); let found = null;
  const isMap = o => o && typeof o === 'object' && typeof o.queryRenderedFeatures === 'function' && typeof o.project === 'function';
  const dig = (o, d) => { if (found || !o || d > 12 || typeof o !== 'object' || seen.has(o)) return; seen.add(o);
    if (isMap(o)) { found = o; return; } for (const k of Object.keys(o)) { try { dig(o[k], d + 1); } catch {} } };
  for (const el of document.querySelectorAll('*')) {
    for (const k of Object.keys(el)) if (k.startsWith('__reactFiber$') || k.startsWith('__reactProps$')) dig(el[k], 0);
    if (found) break;
  }
  window.__nwmMap = found; return !!found;
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const calls = [];
page.on('response', r => { const u = r.url(); if (u.includes('/api/v1/') && !u.includes('/tiles/')) calls.push({ status: r.status(), url: u }); });
await page.goto('https://test.nwm.ac.cn/', { waitUntil: 'networkidle', timeout: 90000 });
if (!await page.evaluate(FIND_MAP)) throw new Error('MAP_NOT_FOUND');
const box = await page.locator('canvas.maplibregl-canvas').boundingBox();
console.log('canvas box', JSON.stringify(box));

const report = [];
for (const [label, net, lon, lat, segs, wantSeg] of PICKS.filter(p => !ONLY || p[0] === ONLY)) {
  // Close any open curve panel by its own button. Escape does NOT close it, and a
  // left-over panel covers the next network's click point — that is exactly what made
  // the small network measure zero on the previous attempt.
  for (let i = 0; i < 3; i++) {
    const x = page.locator('button[aria-label*=\"关闭\"], button[aria-label*=\"close\" i], [class*=panel] button:has-text(\"×\")').first();
    if (await x.count()) { await x.click({ timeout: 3000 }).catch(() => {}); await page.waitForTimeout(600); } else break;
  }
  await page.waitForTimeout(600);
  let hit = null;
  for (const z of [11, 10, 9, 8, 7]) {
    await page.evaluate(([lon, lat, z]) => window.__nwmMap.jumpTo({ center: [lon, lat], zoom: z }), [lon, lat, z]);
    await page.waitForTimeout(7000);
    hit = await page.evaluate(([net, wantSeg]) => {
      const m = window.__nwmMap;
      const fs = m.queryRenderedFeatures({ layers: ['m11-national-river-line'] })
                  .filter(f => f.properties?.river_network_version_id === net);
      if (!fs.length) return null;
      const f = (wantSeg && fs.find(x => x.properties?.river_segment_id === wantSeg)) || fs[0];
      if (wantSeg && f.properties?.river_segment_id !== wantSeg) return null;
      c = f.geometry.coordinates;
      const mid = Array.isArray(c[0][0]) ? c[0][Math.floor(c[0].length / 2)] : c[Math.floor(c.length / 2)];
      const pt = m.project(mid);
      return { x: Math.round(pt.x), y: Math.round(pt.y), props: f.properties, rendered: fs.length };
    }, [net, wantSeg]);
    if (hit) { hit.zoom = z; break; }
  }
  if (!hit) { report.push({ label, network: net, segments: segs, error: 'NO_RENDERED_FEATURE' }); console.log(`${label}: NO FEATURE`); continue; }

  calls.length = 0;
  const t0 = Date.now();
  await page.mouse.click(box.x + hit.x, box.y + hit.y);
  try { await page.waitForResponse(r => r.url().includes('forecast-series'), { timeout: 30000 }); } catch {}
  await page.waitForTimeout(4000);
  const ms = Date.now() - t0;
  await page.screenshot({ path: `${OUT}/${label}.png` });
  const fs_ = calls.filter(c => c.url.includes('forecast-series'));
  const runs = fs_.map(c => (c.url.match(/run_id=([^&]+)/) || [])[1] || null);
  const entry = { label, network: net, segments: segs, zoom: hit.zoom,
    rendered_features_in_view: hit.rendered, clicked: hit.props,
    click_to_settled_ms: ms, forecast_series_calls: fs_.length,
    statuses: fs_.map(c => c.status), runs,
    sources_seen: [...new Set(runs.map(r => (r || '').split('_')[1]))],
    non_2xx: calls.filter(c => c.status >= 400).map(c => `${c.status} ${c.url.slice(0, 140)}`) };
  report.push(entry);
  console.log(`${label}: z=${hit.zoom} ${ms}ms series=${fs_.length} statuses=${entry.statuses} sources=${entry.sources_seen} non2xx=${entry.non_2xx.length}`);
  for (const b of entry.non_2xx) console.log('   !! ' + b);
}
await browser.close();
writeFileSync(process.env.OUT_JSON || '/home/nwm/tmp/1987/clicks-0918.json', JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
