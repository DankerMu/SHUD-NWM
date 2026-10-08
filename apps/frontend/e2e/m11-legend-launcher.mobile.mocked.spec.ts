import { expect, test, type Locator, type Page } from '@playwright/test'

import {
  MOCK_PRECIP_LEGEND,
  contains,
  findBlankMapPoint,
  intersects,
  mockLegendLauncherApi,
  waitForApiQuiet,
  type Box,
  type LegendLauncherMockLog,
} from './support/legendLauncher.mocked'
import { isMobileForm, isShortLandscape, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 图例启动器与浮层展开值（openspec mobile-responsive-display task 3.2，design.md D5）。
 * 三个移动 project 下都要通过；只有“面板是否必然溢出”按是否矮视口横屏分支。
 *
 * 只断言图例：图层面板与底图切换器在 3.3 之前仍是常驻面板，所以这里不写“三个面板都不可见”。
 */

const PORTRAIT = { width: 390, height: 664 }
const SHORT_LANDSCAPE = { width: 750, height: 342 }
const DESKTOP = { width: 1280, height: 900 }
const MIN_TOUCH_TARGET = 44
/** “位于右上角”：启动器右边到地图区右边不超过这个距离。 */
const MAX_RIGHT_INSET = 16
/** 桌面图例相对地图区的偏移（`bottom-[7.5rem] right-4`）。 */
const DESKTOP_LEGEND_RIGHT = 16
const DESKTOP_LEGEND_BOTTOM = 120

function parts(page: Page) {
  return {
    map: page.locator('[data-testid="m11-fullscreen-map"]'),
    bar: page.locator('[data-testid="m11-bottom-control-bar"]'),
    column: page.locator('[data-testid="m11-launcher-column"]'),
    launcher: page.locator('[data-testid="m11-launcher-legend"]'),
    panel: page.locator('[data-testid="m11-floating-legend"]'),
    scroller: page.locator('[data-testid="m11-floating-legend-scroll"]'),
  }
}

async function boxOf(locator: Locator, name: string): Promise<Box> {
  const box = await locator.boundingBox()
  expect(box, `${name} 应有可测量的布局矩形`).not.toBeNull()
  return box!
}

async function openMap(page: Page) {
  await page.goto('/')
  await expectMapControlsMounted(page)
  await expect(parts(page).bar).toBeVisible()
}

async function expandLegend(page: Page) {
  const { launcher, panel } = parts(page)
  await launcher.tap()
  await expect(panel).toBeVisible()
  await expect(launcher).toHaveAttribute('aria-expanded', 'true')
}

/** 展开面板的几何：在地图区内、整体在控制条顶边之上、不与启动器列相交。 */
async function expectPanelFitsMapRegion(page: Page, label: string) {
  const { map, bar, column, launcher, panel } = parts(page)
  const boxes = {
    map: await boxOf(map, '地图区'),
    bar: await boxOf(bar, '控制条'),
    launcher: await boxOf(launcher, '图例启动器'),
    panel: await boxOf(panel, '图例面板'),
  }
  const columnBox = await boxOf(column, '启动器列')
  console.log(`legend-launcher boxes @ ${label}`, JSON.stringify(boxes))
  expect.soft(contains(boxes.map, boxes.panel), `面板 ${JSON.stringify(boxes.panel)} 不在地图区 ${JSON.stringify(boxes.map)} 内`).toBe(true)
  expect
    .soft(boxes.panel.y + boxes.panel.height, `面板底边越过控制条顶边 ${boxes.bar.y}`)
    .toBeLessThanOrEqual(boxes.bar.y)
  expect.soft(intersects(boxes.panel, boxes.bar), '面板与控制条相交').toBe(false)
  expect.soft(intersects(boxes.panel, columnBox), `面板 ${JSON.stringify(boxes.panel)} 与启动器列 ${JSON.stringify(columnBox)} 相交`).toBe(false)
  expect.soft(boxes.panel.width).toBeGreaterThan(0)
  expect.soft(boxes.panel.height).toBeGreaterThan(0)
  return boxes
}

/** 末项相对面板可视框的位置，连同滚动容器的滚动量。 */
async function lastEntryState(page: Page) {
  const { panel, scroller } = parts(page)
  const last = page.locator('[data-testid="m11-floating-legend-precip-row"]').last()
  const metrics = await scroller.evaluate((element) => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    scrollTop: element.scrollTop,
  }))
  const panelBox = await boxOf(panel, '图例面板')
  const lastBox = await boxOf(last, '图例末项')
  return { metrics, panelBox, lastBox, lastInsidePanel: contains(panelBox, lastBox) }
}

test.describe('M11 图例启动器移动形态', () => {
  let log: LegendLauncherMockLog

  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    log = await mockLegendLauncherApi(page)
  })

  test('默认只有图例启动器：44×44 以上、在地图区右上角的启动器列里、点得到，图例面板不在', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    await openMap(page)
    const { map, bar, column, launcher, panel } = parts(page)

    await expect(launcher).toBeVisible()
    await expect(panel).toHaveCount(0)
    await expect(launcher).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('button', { name: '图例', exact: true })).toHaveCount(1)
    // 启动器在启动器列容器里（DOM 归属）。
    await expect(column.locator('[data-testid="m11-launcher-legend"]')).toHaveCount(1)

    const boxes = {
      map: await boxOf(map, '地图区'),
      bar: await boxOf(bar, '控制条'),
      launcher: await boxOf(launcher, '图例启动器'),
    }
    const columnBox = await boxOf(column, '启动器列')
    console.log(
      `legend-launcher collapsed @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`,
      JSON.stringify(boxes),
    )
    expect.soft(boxes.launcher.width).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(boxes.launcher.height).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(contains(boxes.map, boxes.launcher), '启动器不在地图区内').toBe(true)
    expect.soft(contains(columnBox, boxes.launcher), '启动器不在启动器列的包围盒内').toBe(true)
    // 右上角：贴右缘、整体在控制条顶边之上。不钉 top——3.3 会在它上方插入两个启动器。
    const rightInset = boxes.map.x + boxes.map.width - (boxes.launcher.x + boxes.launcher.width)
    expect.soft(rightInset, `启动器右边距 ${rightInset}`).toBeGreaterThanOrEqual(0)
    expect.soft(rightInset, `启动器右边距 ${rightInset}`).toBeLessThanOrEqual(MAX_RIGHT_INSET)
    expect.soft(boxes.launcher.y + boxes.launcher.height, '启动器底边越过控制条顶边').toBeLessThanOrEqual(boxes.bar.y)

    // 过渡期里底图切换器贴着启动器列：启动器自身必须点得到。
    const hit = await launcher.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      return {
        onLauncher: top !== null && element.contains(top),
        top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
      }
    })
    expect.soft(hit.onLauncher, `启动器中心被 ${hit.top} 盖住`).toBe(true)
  })

  test('点启动器展开图例面板，再点收起', async ({ page }) => {
    await openMap(page)
    const { launcher, panel } = parts(page)

    await expandLegend(page)
    await launcher.tap()
    await expect(panel).toHaveCount(0)
    await expect(launcher).toHaveAttribute('aria-expanded', 'false')
  })

  test('点面板与启动器之外的地图点收起图例面板', async ({ page }) => {
    await openMap(page)
    const { launcher, panel } = parts(page)

    await expandLegend(page)
    // 展开之后再找点：面板自己也盖着一块地图。
    const point = await findBlankMapPoint(page)
    await page.touchscreen.tap(point.x, point.y)
    await expect(panel, `tap @ ${JSON.stringify(point)} 之后图例面板仍在`).toHaveCount(0)
    await expect(launcher).toHaveAttribute('aria-expanded', 'false')
    await expect(launcher).toBeVisible()
  })

  test('按 Escape 收起图例面板', async ({ page }) => {
    await openMap(page)
    const { launcher, panel } = parts(page)

    await expandLegend(page)
    // 不依赖面板内焦点：监听在文档级。
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)
    await expect(launcher).toHaveAttribute('aria-expanded', 'false')
  })

  test('展开 / 收起、Escape、点地图都不改 URL、不发 API 请求', async ({ page }) => {
    await openMap(page)
    const { launcher, panel } = parts(page)

    // 窗口：首屏请求落定（计数连续 1s 不增长）之后开；只含启动器开 / 关、Escape、地图 tap。
    const requestsBefore = await waitForApiQuiet(page, log)
    expect(requestsBefore, '首屏应已发过 API 请求，否则计数是空的').toBeGreaterThan(0)
    const urlBefore = page.url()

    await expandLegend(page)
    await launcher.tap()
    await expect(panel).toHaveCount(0)

    await expandLegend(page)
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)

    await expandLegend(page)
    const point = await findBlankMapPoint(page)
    await page.touchscreen.tap(point.x, point.y)
    await expect(panel).toHaveCount(0)

    // 给迟到的请求留出与开窗相同的静默时长。
    await page.waitForTimeout(1_000)
    expect(page.url()).toBe(urlBefore)
    expect(log.apiRequests, '窗口内出现了 API 请求').toBe(requestsBefore)
  })

  test('展开的面板列出径流分级（含单位）与六级降水', async ({ page }) => {
    await openMap(page)
    const { panel } = parts(page)
    await expandLegend(page)

    await expect(panel).toContainText('径流量图例')
    const dischargeRows = panel.locator('[data-testid="m11-floating-legend-entries"] > div')
    // 径流分级：六个带单位的区间（另有一行“无径流数据”）。
    await expect(dischargeRows.filter({ hasText: 'm³/s' })).toHaveCount(6)
    await expect(dischargeRows.filter({ hasText: '<1 m³/s' })).toHaveCount(1)
    await expect(dischargeRows.filter({ hasText: '>10000 m³/s' })).toHaveCount(1)

    await expect(panel.locator('[data-testid="m11-floating-legend-precip"]')).toContainText('mm/24h')
    const precipRows = panel.locator('[data-testid="m11-floating-legend-precip-row"]')
    await expect(precipRows).toHaveCount(6)
    await expect(precipRows).toHaveText(MOCK_PRECIP_LEGEND.map((entry) => entry.label))
  })

  test('展开的面板在地图区内、在控制条之上、不与启动器列相交，滚到底后末项在面板可视框内', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    await openMap(page)
    const { scroller } = parts(page)
    await expandLegend(page)
    await expect(page.locator('[data-testid="m11-floating-legend-precip-row"]')).toHaveCount(6)

    const boxes = await expectPanelFitsMapRegion(page, `${testInfo.project.name} ${viewport.width}x${viewport.height}`)

    const before = await lastEntryState(page)
    console.log(
      `legend-launcher scroll @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`,
      JSON.stringify({ ...before.metrics, scrolls: before.metrics.scrollHeight > before.metrics.clientHeight }),
    )
    if (isShortLandscape(viewport)) {
      // 矮视口横屏 + 双图例必然放不下：先证明真的溢出、末项此刻在可视框外，后面的断言才不是恒真。
      expect(before.metrics.scrollHeight, '矮视口横屏下双图例应超出面板高度').toBeGreaterThan(before.metrics.clientHeight)
      expect(before.lastInsidePanel, '滚动前末项不应已在面板可视框内').toBe(false)
    }

    await scroller.evaluate((element) => {
      element.scrollTop = element.scrollHeight
    })
    const after = await lastEntryState(page)
    expect(after.metrics.scrollTop + after.metrics.clientHeight).toBeGreaterThanOrEqual(after.metrics.scrollHeight - 1)
    expect(after.lastInsidePanel, `滚到底后末项 ${JSON.stringify(after.lastBox)} 不在面板 ${JSON.stringify(after.panelBox)} 内`).toBe(true)
    // 滚动的是面板内部，面板自身不动。
    expect(after.panelBox).toEqual(boxes.panel)
  })

  test('展开时由 390×664 转为 750×342：面板仍展开且在地图区内', async ({ page }) => {
    await page.setViewportSize(PORTRAIT)
    await openMap(page)
    await expandLegend(page)
    const portrait = await expectPanelFitsMapRegion(page, 'rotation 390x664')

    await page.setViewportSize(SHORT_LANDSCAPE)
    const { panel, launcher } = parts(page)
    await expect(panel).toBeVisible()
    await expect(launcher).toHaveAttribute('aria-expanded', 'true')
    // 面板按新的地图区重新限高：等限高落到位（地图区变矮后面板必然比竖屏时矮）。
    await expect.poll(async () => (await panel.boundingBox())?.height ?? Infinity).toBeLessThan(portrait.panel.height)
    await expectPanelFitsMapRegion(page, 'rotation 750x342')
  })

  test('390×664 → 1280×900 → 390×664：桌面图例在原偏移且无启动器，回来后面板收起', async ({ page }) => {
    await page.setViewportSize(PORTRAIT)
    await openMap(page)
    await expandLegend(page)
    const { map, launcher, panel, column } = parts(page)

    // 跨形态往返是场景本身的要求，所以这一处桌面断言留在移动 spec 里。
    await page.setViewportSize(DESKTOP)
    await expect(launcher).toHaveCount(0)
    await expect(column).toHaveCount(0)
    await expect(panel).toBeVisible()
    await expect(panel).toHaveCount(1)
    const mapBox = await boxOf(map, '地图区')
    const legendBox = await boxOf(panel, '桌面图例')
    console.log('legend-launcher desktop round-trip @ 1280x900', JSON.stringify({ map: mapBox, legend: legendBox }))
    expect(mapBox.x + mapBox.width - (legendBox.x + legendBox.width)).toBeCloseTo(DESKTOP_LEGEND_RIGHT, 3)
    expect(mapBox.y + mapBox.height - (legendBox.y + legendBox.height)).toBeCloseTo(DESKTOP_LEGEND_BOTTOM, 3)

    await page.setViewportSize(PORTRAIT)
    await expect(launcher).toBeVisible()
    await expect(launcher).toHaveAttribute('aria-expanded', 'false')
    await expect(panel).toHaveCount(0)
  })
})
