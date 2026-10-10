import { expect, test, type Locator, type Page } from '@playwright/test'

import {
  contains,
  findBlankMapPoint,
  intersects,
  mockLegendLauncherApi,
  waitForApiQuiet,
  type LegendLauncherMockLog,
} from './support/legendLauncher.mocked'
import {
  CONTROL_BAR,
  LAUNCHER_COLUMN,
  MAP_REGION,
  OPS_LINK,
  OVERLAY_PANELS,
  OVERLAY_PANEL_PARTS,
  TAPPABLE_SELECTOR,
  boxOf,
  expectOnlyExpanded,
  overlayPart,
  type OverlayPanelName,
} from './support/overlayLaunchers.mocked'
import { setRole } from './support/setRole'
import { isMobileForm, isShortLandscape, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 图层与底图启动器（openspec mobile-responsive-display task 3.3，design.md D5）。
 * 三个移动 project 下都要通过、不按 project 跳过；只有“图层面板是否必然溢出”按是否矮视口横屏分支。
 */

const DESKTOP = { width: 1280, height: 900 }
const MIN_TOUCH_TARGET = 44
/** “位于右上角”：启动器右边到地图区右边不超过这个距离。 */
const MAX_RIGHT_INSET = 16
/** 每个面板里的可点项个数：图层 = 流量 / 降水 / 气象代站，底图 = 矢量 / 卫星 / 地形，图例没有可点项。 */
const TAPPABLE_COUNT: Record<OverlayPanelName, number> = { layers: 3, basemap: 3, legend: 0 }

async function openMap(page: Page) {
  await page.goto('/')
  await expectMapControlsMounted(page)
  await expect(page.locator(CONTROL_BAR)).toBeVisible()
}

async function expand(page: Page, name: OverlayPanelName) {
  await overlayPart(page, name).launcher.tap()
  await expectOnlyExpanded(page, name)
}

function stationToggle(panel: Locator) {
  return panel.getByRole('button', { name: /气象代站/ })
}

function search(page: Page) {
  return new URL(page.url()).search
}

/** 元素中心的命中测试是否落在它自己身上（没被别的浮层盖住）。 */
async function hitsItself(locator: Locator) {
  return locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return {
      onSelf: top !== null && element.contains(top),
      top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
    }
  })
}

/** 展开面板的几何：在地图区内、整体在控制条顶边之上、不与启动器列相交。 */
async function expectPanelFitsMapRegion(page: Page, name: OverlayPanelName, label: string) {
  const title = OVERLAY_PANEL_PARTS[name].title
  const boxes = {
    map: await boxOf(page.locator(MAP_REGION), '地图区'),
    bar: await boxOf(page.locator(CONTROL_BAR), '控制条'),
    column: await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列'),
    panel: await boxOf(overlayPart(page, name).panel, `${title}面板`),
  }
  console.log(`launchers ${name} panel @ ${label}`, JSON.stringify(boxes))
  expect.soft(contains(boxes.map, boxes.panel), `${title}面板 ${JSON.stringify(boxes.panel)} 不在地图区 ${JSON.stringify(boxes.map)} 内`).toBe(true)
  expect.soft(boxes.panel.y + boxes.panel.height, `${title}面板底边越过控制条顶边 ${boxes.bar.y}`).toBeLessThanOrEqual(boxes.bar.y)
  expect.soft(intersects(boxes.panel, boxes.bar), `${title}面板与控制条相交`).toBe(false)
  expect.soft(intersects(boxes.panel, boxes.column), `${title}面板与启动器列 ${JSON.stringify(boxes.column)} 相交`).toBe(false)
  expect.soft(boxes.panel.width).toBeGreaterThan(0)
  expect.soft(boxes.panel.height).toBeGreaterThan(0)
  return boxes
}

/** 图层面板末行（「气象代站」）相对面板可视框的位置，连同滚动容器的滚动量。 */
async function lastLayerRowState(page: Page) {
  const { panel, scroller } = overlayPart(page, 'layers')
  const metrics = await scroller.evaluate((element) => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    scrollTop: element.scrollTop,
  }))
  const panelBox = await boxOf(panel, '图层面板')
  const lastBox = await boxOf(stationToggle(panel), '图层面板末行')
  return { metrics, panelBox, lastBox, lastInsidePanel: contains(panelBox, lastBox) }
}

test.describe('M11 图层与底图启动器移动形态', () => {
  let log: LegendLauncherMockLog

  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    log = await mockLegendLauncherApi(page)
  })

  test('默认三个启动器都在右上角的启动器列里（图层 / 底图 / 图例），三个面板都不在', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    await openMap(page)
    const column = page.locator(LAUNCHER_COLUMN)

    await expectOnlyExpanded(page, null)
    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const columnBox = await boxOf(column, '启动器列')
    const launcherBoxes = []
    for (const name of OVERLAY_PANELS) {
      const part = OVERLAY_PANEL_PARTS[name]
      const { launcher } = overlayPart(page, name)
      await expect(launcher, `${part.title}启动器应可见`).toBeVisible()
      await expect(page.getByRole('button', { name: part.launcherName, exact: true })).toHaveCount(1)
      await expect(page.getByRole('button', { name: part.launcherName, exact: true })).toHaveAttribute(
        'data-testid',
        `m11-launcher-${name}`,
      )
      await expect(column.locator(part.launcher), `${part.title}启动器应在启动器列里`).toHaveCount(1)

      const box = await boxOf(launcher, `${part.title}启动器`)
      launcherBoxes.push(box)
      expect.soft(box.width, `${part.title}启动器宽`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
      expect.soft(box.height, `${part.title}启动器高`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
      expect.soft(contains(map, box), `${part.title}启动器不在地图区内`).toBe(true)
      expect.soft(contains(columnBox, box), `${part.title}启动器不在启动器列的包围盒内`).toBe(true)
      const rightInset = map.x + map.width - (box.x + box.width)
      expect.soft(rightInset, `${part.title}启动器右边距 ${rightInset}`).toBeGreaterThanOrEqual(0)
      expect.soft(rightInset, `${part.title}启动器右边距 ${rightInset}`).toBeLessThanOrEqual(MAX_RIGHT_INSET)
      expect.soft(box.y + box.height, `${part.title}启动器底边越过控制条顶边`).toBeLessThanOrEqual(bar.y)
      const hit = await hitsItself(launcher)
      expect.soft(hit.onSelf, `${part.title}启动器中心被 ${hit.top} 盖住`).toBe(true)
    }
    console.log(
      `launchers collapsed @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`,
      JSON.stringify({ map, bar, column: columnBox, layers: launcherBoxes[0], basemap: launcherBoxes[1], legend: launcherBoxes[2] }),
    )
    // 自上而下：图层、底图、图例，互不相交。
    expect(launcherBoxes[0].y + launcherBoxes[0].height).toBeLessThanOrEqual(launcherBoxes[1].y)
    expect(launcherBoxes[1].y + launcherBoxes[1].height).toBeLessThanOrEqual(launcherBoxes[2].y)
    // DOM 顺序即视觉顺序。
    const domOrder = await column
      .locator('[data-testid^="m11-launcher-"]')
      .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-testid')))
    expect(domOrder).toEqual(['m11-launcher-layers', 'm11-launcher-basemap', 'm11-launcher-legend'])

    // 开发用角色切换器不与任何启动器相交。
    const role = page.getByLabel('Role')
    await expect(role).toBeVisible()
    const roleBox = await boxOf(role, '角色切换器')
    for (const [index, name] of OVERLAY_PANELS.entries()) {
      expect(intersects(roleBox, launcherBoxes[index]), `角色切换器 ${JSON.stringify(roleBox)} 与${OVERLAY_PANEL_PARTS[name].title}启动器相交`).toBe(false)
    }
  })

  test('点图层启动器展开图层面板，再点收起', async ({ page }) => {
    await openMap(page)

    await expand(page, 'layers')
    await overlayPart(page, 'layers').launcher.tap()
    await expectOnlyExpanded(page, null)
  })

  test('三个面板两两切换：每一步只有被点的那个面板展开', async ({ page }) => {
    await openMap(page)

    for (const name of ['legend', 'layers', 'basemap', 'legend'] as const) {
      await expand(page, name)
    }
  })

  test('在图层面板里切换气象代站之后面板保持展开', async ({ page }) => {
    await openMap(page)
    await expand(page, 'layers')
    const toggle = stationToggle(overlayPart(page, 'layers').panel)
    const pressedBefore = await toggle.getAttribute('aria-pressed')
    const searchBefore = search(page)

    await toggle.tap()
    await expect(toggle).not.toHaveAttribute('aria-pressed', pressedBefore ?? '')
    await expect.poll(() => search(page)).not.toBe(searchBefore)
    // 面板内的操作不是地图点击。
    await expectOnlyExpanded(page, 'layers')
  })

  test('点面板与启动器之外的地图点收起图层面板', async ({ page }) => {
    await openMap(page)
    await expand(page, 'layers')

    // 展开之后再找点：面板自己也盖着一块地图。
    const point = await findBlankMapPoint(page)
    await page.touchscreen.tap(point.x, point.y)
    await expect(overlayPart(page, 'layers').panel, `tap @ ${JSON.stringify(point)} 之后图层面板仍在`).toHaveCount(0)
    await expectOnlyExpanded(page, null)
  })

  test('按 Escape 收起图层面板', async ({ page }) => {
    await openMap(page)
    await expand(page, 'layers')

    await page.keyboard.press('Escape')
    await expectOnlyExpanded(page, null)
  })

  test('图层与底图面板展开再收起不改 URL、不发 API 请求', async ({ page }) => {
    await openMap(page)

    // 窗口：首屏请求落定（计数连续 1s 不增长）之后开；只含启动器点击。
    const requestsBefore = await waitForApiQuiet(page, log)
    expect(requestsBefore, '首屏应已发过 API 请求，否则计数是空的').toBeGreaterThan(0)
    const urlBefore = page.url()

    for (const name of ['layers', 'basemap'] as const) {
      await expand(page, name)
      await overlayPart(page, name).launcher.tap()
      await expectOnlyExpanded(page, null)
    }

    // 给迟到的请求留出与开窗相同的静默时长。
    await page.waitForTimeout(1_000)
    expect(page.url()).toBe(urlBefore)
    expect(log.apiRequests, '窗口内出现了 API 请求').toBe(requestsBefore)
  })

  test('移动面板里切换气象代站产生的 URL query 与桌面形态同一操作相同', async ({ page }) => {
    await openMap(page)
    const initial = search(page)
    await expand(page, 'layers')
    await stationToggle(overlayPart(page, 'layers').panel).tap()
    await expect.poll(() => search(page)).not.toBe(initial)
    const mobileSearch = search(page)

    // 跨形态对照是场景本身的要求，所以这一处桌面操作留在移动 spec 里。
    await page.setViewportSize(DESKTOP)
    await openMap(page)
    expect(search(page), '桌面形态重新进入 / 后的起点应与移动形态相同').toBe(initial)
    const desktopPanel = overlayPart(page, 'layers').panel
    await expect(overlayPart(page, 'layers').launcher).toHaveCount(0)
    await stationToggle(desktopPanel).click()
    await expect.poll(() => search(page)).not.toBe(initial)

    expect(search(page)).toBe(mobileSearch)
    expect(mobileSearch).not.toBe(initial)
  })

  test('移动面板里切到卫星底图产生的 URL query 与桌面形态同一操作相同', async ({ page }) => {
    await openMap(page)
    const initial = search(page)
    await expand(page, 'basemap')
    const mobileButton = overlayPart(page, 'basemap').panel.getByRole('button', { name: '卫星底图', exact: true })
    await expect(mobileButton).toHaveAttribute('aria-pressed', 'false')
    await mobileButton.tap()
    await expect(mobileButton).toHaveAttribute('aria-pressed', 'true')
    await expect.poll(() => search(page)).not.toBe(initial)
    const mobileSearch = search(page)
    await expectOnlyExpanded(page, 'basemap')

    await page.setViewportSize(DESKTOP)
    await openMap(page)
    expect(search(page), '桌面形态重新进入 / 后的起点应与移动形态相同').toBe(initial)
    await expect(overlayPart(page, 'basemap').launcher).toHaveCount(0)
    await overlayPart(page, 'basemap').panel.getByRole('button', { name: '卫星底图', exact: true }).click()
    await expect.poll(() => search(page)).not.toBe(initial)

    expect(search(page)).toBe(mobileSearch)
    expect(mobileSearch).not.toBe(initial)
  })

  test('图层与底图面板展开时在地图区内、在控制条之上、不与启动器列相交；图层面板超高时内部滚动', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await openMap(page)

    await expand(page, 'basemap')
    await expectPanelFitsMapRegion(page, 'basemap', label)

    await expand(page, 'layers')
    const boxes = await expectPanelFitsMapRegion(page, 'layers', label)
    const { scroller } = overlayPart(page, 'layers')
    const before = await lastLayerRowState(page)
    console.log(
      `launchers layers scroll @ ${label}`,
      JSON.stringify({ ...before.metrics, scrolls: before.metrics.scrollHeight > before.metrics.clientHeight }),
    )
    if (isShortLandscape(viewport)) {
      // 矮视口横屏下图层面板放不下：先证明真的溢出、末行此刻在可视框外，后面的断言才不是恒真。
      expect(before.metrics.scrollHeight, '矮视口横屏下图层面板内容应超出面板高度').toBeGreaterThan(before.metrics.clientHeight)
      expect(before.lastInsidePanel, '滚动前末行不应已在面板可视框内').toBe(false)
    }

    await scroller.evaluate((element) => {
      element.scrollTop = element.scrollHeight
    })
    const after = await lastLayerRowState(page)
    expect(after.metrics.scrollTop + after.metrics.clientHeight).toBeGreaterThanOrEqual(after.metrics.scrollHeight - 1)
    expect(after.lastInsidePanel, `滚到底后末行 ${JSON.stringify(after.lastBox)} 不在面板 ${JSON.stringify(after.panelBox)} 内`).toBe(true)
    // 滚动的是面板内部，面板自身不动。
    expect(after.panelBox).toEqual(boxes.panel)
  })

  test('三个面板各自展开时，面板内每个可点项不小于 44×44；图例面板没有可点项', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    await openMap(page)

    for (const name of OVERLAY_PANELS) {
      const title = OVERLAY_PANEL_PARTS[name].title
      await expand(page, name)
      const tappables = overlayPart(page, name).panel.locator(TAPPABLE_SELECTOR)
      // 计数钉死：选择器没匹配到任何东西时不让这一段静默通过。
      await expect(tappables, `${title}面板的可点项个数`).toHaveCount(TAPPABLE_COUNT[name])
      const sizes = await tappables.evaluateAll((elements) =>
        elements.map((element) => {
          const rect = element.getBoundingClientRect()
          return { text: (element.getAttribute('aria-label') ?? element.textContent ?? '').trim(), width: rect.width, height: rect.height }
        }),
      )
      console.log(`launchers ${name} tappables @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`, JSON.stringify(sizes))
      for (const size of sizes) {
        expect.soft(size.width, `${title}面板「${size.text}」宽`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
        expect.soft(size.height, `${title}面板「${size.text}」高`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
      }
    }
  })

  test('operator 角色：运维入口排在图例启动器之下，不盖启动器、不与展开的面板相交，面板仍在地图区内', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await openMap(page)
    await expect(page.locator(OPS_LINK)).toHaveCount(0)
    await setRole(page, 'operator')

    const ops = page.locator(OPS_LINK)
    await expect(ops).toBeVisible()
    await expect(page.locator(LAUNCHER_COLUMN).locator(OPS_LINK), '运维入口应在启动器列里').toHaveCount(1)
    await expect(ops).toHaveAttribute('href', '/ops')
    const opsBox = await boxOf(ops, '运维入口')
    const launcherBoxes = {
      layers: await boxOf(overlayPart(page, 'layers').launcher, '图层启动器'),
      basemap: await boxOf(overlayPart(page, 'basemap').launcher, '底图启动器'),
      legend: await boxOf(overlayPart(page, 'legend').launcher, '图例启动器'),
    }
    console.log(`launchers operator @ ${label}`, JSON.stringify({ ops: opsBox, ...launcherBoxes }))
    for (const name of OVERLAY_PANELS) {
      const title = OVERLAY_PANEL_PARTS[name].title
      const hit = await hitsItself(overlayPart(page, name).launcher)
      expect.soft(hit.onSelf, `${title}启动器中心被 ${hit.top} 盖住`).toBe(true)
      expect.soft(intersects(opsBox, launcherBoxes[name]), `运维入口 ${JSON.stringify(opsBox)} 与${title}启动器相交`).toBe(false)
    }
    expect(opsBox.y, '运维入口应在图例启动器之下').toBeGreaterThanOrEqual(launcherBoxes.legend.y + launcherBoxes.legend.height)
    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    expect.soft(map.x + map.width - (opsBox.x + opsBox.width), '运维入口右边距').toBeGreaterThanOrEqual(0)
    expect.soft(opsBox.x, '运维入口左边不得伸出地图区').toBeGreaterThanOrEqual(map.x)

    // 运维入口与启动器同宽（44×44），列多出第四项但不变宽：每个面板展开后仍须整个留在地图区内、不与运维入口相交。
    for (const name of OVERLAY_PANELS) {
      const title = OVERLAY_PANEL_PARTS[name].title
      await expand(page, name)
      const boxes = await expectPanelFitsMapRegion(page, name, `operator ${label}`)
      expect.soft(intersects(await boxOf(ops, '运维入口'), boxes.panel), `运维入口与${title}面板相交`).toBe(false)
    }
  })
})
