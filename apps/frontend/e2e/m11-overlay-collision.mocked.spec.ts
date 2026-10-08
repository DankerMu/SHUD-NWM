import { expect, test, type Page } from '@playwright/test'

import { isMobileForm } from './support/viewportForm'

/**
 * 地图自带的版权归属（MapLibre attribution）是瓦片供应商的硬要求，必须可见。
 * 它固定在地图右下角，而径流量图例也浮在右下角——两者曾经互相压盖，图例把
 * attribution 遮成只露一角。这条守的是几何：两个矩形不许相交。
 *
 * 只能在真实浏览器里验：jsdom 不做布局，量不出任何一个矩形。
 * 失败时也把实测 bounding box 打进日志，作为 baseline 红证据。
 */

type Box = { x: number; y: number; width: number; height: number }

function intersects(a: Box, b: Box) {
  const overlapX = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const overlapY = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  return overlapX > 0 && overlapY > 0
}

async function openOverview(page: Page, viewport: { width: number; height: number }) {
  await page.setViewportSize(viewport)
  await page.goto('/overview?source=gfs&layer=discharge&basemap=vector')
}

/**
 * 520px 属移动形态（openspec mobile-responsive-display D1 / D5）：图例默认收成启动器，
 * 图例面板不在；避让关系由启动器承接。启动器必须在地图区内、不压 attribution 与控制条。
 * 这里不写控制条的任何数值——它的移动几何由控制条自己的 spec 负责。
 */
async function expectLegendCollapsedToLauncher(page: Page, width: number) {
  const launcher = page.locator('[data-testid="m11-launcher-legend"]')
  const attribution = page.locator('.maplibregl-ctrl-attrib')
  const bar = page.locator('[data-testid="m11-bottom-control-bar"]')
  const map = page.locator('[data-testid="m11-fullscreen-map"]')
  await expect(launcher).toBeVisible()
  await expect(attribution).toBeVisible()
  await expect(bar).toBeVisible()
  await expect(map).toBeVisible()
  await expect(page.locator('[data-testid="m11-floating-legend"]')).toBeHidden()
  const boxes = {
    launcher: await launcher.boundingBox(),
    attribution: await attribution.boundingBox(),
    bar: await bar.boundingBox(),
    map: await map.boundingBox(),
  }
  console.log(`legend launcher @ ${width}`, JSON.stringify(boxes))
  expect(boxes.launcher, '图例启动器应有可测量的布局矩形').not.toBeNull()
  expect(boxes.attribution, '版权归属应有可测量的布局矩形').not.toBeNull()
  expect(boxes.bar, '底部控制条应有可测量的布局矩形').not.toBeNull()
  expect(boxes.map, '地图应有可测量的布局矩形').not.toBeNull()
  expect(boxes.launcher!.x).toBeGreaterThanOrEqual(boxes.map!.x)
  expect(boxes.launcher!.y).toBeGreaterThanOrEqual(boxes.map!.y)
  expect(boxes.launcher!.x + boxes.launcher!.width).toBeLessThanOrEqual(boxes.map!.x + boxes.map!.width)
  expect(boxes.launcher!.y + boxes.launcher!.height).toBeLessThanOrEqual(boxes.map!.y + boxes.map!.height)
  expect(
    intersects(boxes.launcher!, boxes.attribution!),
    `图例启动器 ${JSON.stringify(boxes.launcher)} 与版权归属 ${JSON.stringify(boxes.attribution)} 相交`,
  ).toBe(false)
  expect(
    intersects(boxes.launcher!, boxes.bar!),
    `图例启动器 ${JSON.stringify(boxes.launcher)} 与控制条 ${JSON.stringify(boxes.bar)} 相交`,
  ).toBe(false)
}

test.describe('M11 单图浮层与地图自带控件不互相压盖', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname === '/api/v1/layers') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'ok',
            data: [
              {
                layer_id: 'discharge',
                layer_name: 'Discharge',
                layer_type: 'hydrology',
                variables: ['q_down'],
                metadata: { layer_id: 'discharge', valid_times: [] },
              },
            ],
          }),
        })
      }
      if (url.pathname === '/api/v1/basins') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'ok', data: [] }),
        })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data: [] }) })
    })
  })

  // 五个宽度都要量：attribution 与控制条的避让不是只对某一个宽度成立。
  for (const viewport of [
    { width: 1920, height: 1080 },
    { width: 1440, height: 900 },
    { width: 1280, height: 900 },
    { width: 800, height: 900 },
    { width: 520, height: 900 },
  ]) {
    test(`径流量图例不压盖 MapLibre 版权归属 @ ${viewport.width}px`, async ({ page }) => {
      await openOverview(page, viewport)
      if (isMobileForm(viewport)) return expectLegendCollapsedToLauncher(page, viewport.width)
      const legend = page.locator('[data-testid="m11-floating-legend"]')
      const attribution = page.locator('.maplibregl-ctrl-attrib')
      let boxes: { legend: Box | null; attribution: Box | null } | undefined
      try {
        await expect(legend).toBeVisible()
        await expect(attribution).toBeVisible()
        boxes = { legend: await legend.boundingBox(), attribution: await attribution.boundingBox() }
        expect(boxes.legend, `图例应有可测量的布局矩形 ${JSON.stringify(boxes.legend)}`).not.toBeNull()
        expect(boxes.attribution, `版权归属应有可测量的布局矩形 ${JSON.stringify(boxes.attribution)}`).not.toBeNull()
        expect(
          intersects(boxes.legend!, boxes.attribution!),
          `图例 ${JSON.stringify(boxes.legend)} 与版权归属 ${JSON.stringify(boxes.attribution)} 相交`,
        ).toBe(false)
      } finally {
        boxes ??= { legend: await legend.boundingBox(), attribution: await attribution.boundingBox() }
        console.log(`legend/attribution @ ${viewport.width}`, JSON.stringify(boxes))
      }
    })

    test(`底部控制条不压盖 MapLibre 版权归属 @ ${viewport.width}px`, async ({ page }) => {
      await openOverview(page, viewport)
      const bar = page.locator('[data-testid="m11-bottom-control-bar"]')
      const attribution = page.locator('.maplibregl-ctrl-attrib')
      const map = page.locator('[data-testid="m11-fullscreen-map"]')
      let boxes: { bar: Box | null; attribution: Box | null; map: Box | null } | undefined
      try {
        await expect(bar).toBeVisible()
        await expect(attribution).toBeVisible()
        await expect(map).toBeVisible()
        boxes = { bar: await bar.boundingBox(), attribution: await attribution.boundingBox(), map: await map.boundingBox() }
        expect(boxes.bar, `底部控制条应有可测量的布局矩形 ${JSON.stringify(boxes.bar)}`).not.toBeNull()
        expect(boxes.attribution, `版权归属应有可测量的布局矩形 ${JSON.stringify(boxes.attribution)}`).not.toBeNull()
        expect(boxes.map, `地图应有可测量的布局矩形 ${JSON.stringify(boxes.map)}`).not.toBeNull()
        // 条高 64 与底边距 40 是桌面形态事实；移动形态只留下面两条与形态无关的断言。
        if (!isMobileForm(viewport)) {
          expect(boxes.bar!.height).toBe(64)
          expect(boxes.map!.y + boxes.map!.height - (boxes.bar!.y + boxes.bar!.height)).toBe(40)
        }
        expect(boxes.bar!.x).toBeGreaterThanOrEqual(0)
        expect(boxes.bar!.x + boxes.bar!.width).toBeLessThanOrEqual(viewport.width)
        expect(
          intersects(boxes.bar!, boxes.attribution!),
          `控制条 ${JSON.stringify(boxes.bar)} 与版权归属 ${JSON.stringify(boxes.attribution)} 相交`,
        ).toBe(false)
      } finally {
        boxes ??= { bar: await bar.boundingBox(), attribution: await attribution.boundingBox(), map: await map.boundingBox() }
        console.log(`bar/attribution @ ${viewport.width}`, JSON.stringify(boxes))
      }
    })

    test(`图例与空清单提示不压盖底部控制条 @ ${viewport.width}px`, async ({ page }) => {
      await openOverview(page, viewport)
      const bar = page.locator('[data-testid="m11-bottom-control-bar"]')
      const legend = page.locator('[data-testid="m11-floating-legend"]')
      const notice = page.locator('[data-testid="m11-overview-empty"]')
      if (isMobileForm(viewport)) {
        // 图例那一半由启动器承接；空清单提示那一半原样保留。
        await expectLegendCollapsedToLauncher(page, viewport.width)
        await expect(notice).toBeVisible()
        const mobileBoxes = { bar: await bar.boundingBox(), notice: await notice.boundingBox() }
        console.log(`overlays/bar @ ${viewport.width}`, JSON.stringify(mobileBoxes))
        expect(mobileBoxes.bar, `底部控制条应有可测量的布局矩形 ${JSON.stringify(mobileBoxes.bar)}`).not.toBeNull()
        expect(mobileBoxes.notice, `空清单提示应有可测量的布局矩形 ${JSON.stringify(mobileBoxes.notice)}`).not.toBeNull()
        expect(
          intersects(mobileBoxes.notice!, mobileBoxes.bar!),
          `空清单提示 ${JSON.stringify(mobileBoxes.notice)} 与控制条 ${JSON.stringify(mobileBoxes.bar)} 相交`,
        ).toBe(false)
        return
      }
      let boxes: { bar: Box | null; legend: Box | null; notice: Box | null } | undefined
      try {
        await expect(bar).toBeVisible()
        await expect(legend).toBeVisible()
        await expect(notice).toBeVisible()
        boxes = { bar: await bar.boundingBox(), legend: await legend.boundingBox(), notice: await notice.boundingBox() }
        expect(boxes.bar, `底部控制条应有可测量的布局矩形 ${JSON.stringify(boxes.bar)}`).not.toBeNull()
        expect(boxes.legend, `图例应有可测量的布局矩形 ${JSON.stringify(boxes.legend)}`).not.toBeNull()
        expect(boxes.notice, `空清单提示应有可测量的布局矩形 ${JSON.stringify(boxes.notice)}`).not.toBeNull()
        expect(
          intersects(boxes.legend!, boxes.bar!),
          `图例 ${JSON.stringify(boxes.legend)} 与控制条 ${JSON.stringify(boxes.bar)} 相交`,
        ).toBe(false)
        expect(
          intersects(boxes.notice!, boxes.bar!),
          `空清单提示 ${JSON.stringify(boxes.notice)} 与控制条 ${JSON.stringify(boxes.bar)} 相交`,
        ).toBe(false)
      } finally {
        boxes ??= { bar: await bar.boundingBox(), legend: await legend.boundingBox(), notice: await notice.boundingBox() }
        console.log(`overlays/bar @ ${viewport.width}`, JSON.stringify(boxes))
      }
    })
  }
})
