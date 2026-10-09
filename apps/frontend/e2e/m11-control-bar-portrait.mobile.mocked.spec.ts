import { expect, test, type Page, type TestInfo } from '@playwright/test'

import {
  EMPTY_CYCLE_OPTION_TEXT,
  controlBarParts,
  expectControlsInViewport,
  installFailClosedDischargeLayer,
  measureControlBar,
  overlapsVertically,
  type ControlBarMeasure,
} from './support/controlBar.mocked'
import { contains, intersects, type Box } from './support/legendLauncher.mocked'
import {
  BASEMAP_UNAVAILABLE_TEXT,
  NOTICE_TEST_IDS,
  STATION_EMPTY_TEXT,
  STATUS_TEST_IDS,
  hitsBelow,
  installNoticeMocks,
} from './support/notices.mocked'
import { STATION_LAYER_URL } from './support/openStationWindow'
import { CONTROL_BAR, LAUNCHER_COLUMN, MAP_REGION, boxOf, expectOnlyExpanded, overlayPart } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 控制条竖屏两行（openspec mobile-responsive-display task 3.7，design.md D6）。
 * 三个移动 project 下都要通过、不按 project 跳过：
 * - 默认视口用例：竖屏 project 断言整组两行几何；矮视口横屏 project 只断言“全部控件在视口内”
 *   （横屏单行的收尾与专属 spec 归 3.8）。
 * - 指定视口用例（390×664、320×568、跨形态对照）在用例内 `setViewportSize`，三个 project 都跑同一组断言。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const NARROW: ViewportSize = { width: 320, height: 568 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const TOLERANCE = 0.5
const MIN_TOUCH_TARGET = 44
const MIN_SLIDER_WIDTH = 120
const MIN_SELECT_FONT_PX = 16
/** 控制条底边距地图区底边（attribution 带）。 */
const BAR_BOTTOM_INSET = 40
/** 移动形态下控制条左右各让出的边距。 */
const BAR_SIDE_INSET = 8
const STATUS_CONTAINER = 'm11-map-status-overlays'
const FAIL_CLOSED_REASON = 'No cycle covers every basin, so the national discharge layer is disabled.'

/** 两种目录：带 9 个有效时刻与一个起报时次；fail-closed（无周期、禁用原因出现——竖屏下最高的条）。 */
const CATALOGS = [
  { name: '有周期', failClosed: false },
  { name: '无周期 + 禁用原因', failClosed: true },
] as const

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

async function installCatalog(page: Page, failClosed: boolean) {
  await installRiverWindowMocks(page)
  if (failClosed) await installFailClosedDischargeLayer(page)
}

/** 等控制条进入该目录的落定态：有周期时「下一个有效时刻」可用；fail-closed 时原因文案与空周期选项出现。 */
async function openMap(page: Page, failClosed: boolean, url = '/') {
  await page.goto(url)
  await expectMapControlsMounted(page)
  const parts = controlBarParts(page)
  await expect(parts.bar).toBeVisible()
  if (failClosed) {
    await expect(parts.reason).toHaveText(FAIL_CLOSED_REASON)
    await expect(parts.cycle.locator('option')).toHaveText([EMPTY_CYCLE_OPTION_TEXT])
    await expect(parts.cycle).toBeDisabled()
  } else {
    await expect(parts.stepButtons[2]).toBeEnabled()
    await expect(parts.cycle).toBeEnabled()
    await expect(parts.reason).toHaveCount(0)
  }
}

async function measure(page: Page, where: string, tag: string): Promise<ControlBarMeasure> {
  const measured = await measureControlBar(page, requireViewport(page))
  console.log(`control-bar ${tag} @ ${where}`, JSON.stringify(measured))
  return measured
}

/** 竖屏两行的整组断言（Risk pack「Public API / entry」与「Resource limits / 溢出」）。 */
async function expectPortraitTwoRows(page: Page, where: string, failClosed: boolean) {
  const m = await measure(page, where, failClosed ? 'portrait fail-closed' : 'portrait')
  expectControlsInViewport(m, where)

  // 两行：第一行 = 预报源分段 + 起报时次 + 播放速度；第二行 = 步进 / 播放按钮 + 滑块。
  expect.soft(m.slider.width, `滑块宽 ${m.slider.width} @ ${where}`).toBeGreaterThanOrEqual(MIN_SLIDER_WIDTH)
  expect
    .soft(m.slider.y, `滑块顶边 ${m.slider.y} 应不高于预报源分段底边 ${m.sourceGroup.y + m.sourceGroup.height} @ ${where}`)
    .toBeGreaterThanOrEqual(m.sourceGroup.y + m.sourceGroup.height)
  expect.soft(overlapsVertically(m.speed, m.sourceGroup), `播放速度与预报源分段应同在第一行 @ ${where}`).toBe(true)
  expect.soft(overlapsVertically(m.cycle, m.sourceGroup), `起报时次与预报源分段应同在第一行 @ ${where}`).toBe(true)
  for (const [index, button] of m.stepButtons.entries()) {
    expect.soft(overlapsVertically(button, m.slider), `步进 / 播放按钮 ${index + 1} 与滑块应同在第二行 @ ${where}`).toBe(true)
    expect
      .soft(button.y, `步进 / 播放按钮 ${index + 1} 应在第一行之下 @ ${where}`)
      .toBeGreaterThanOrEqual(m.sourceGroup.y + m.sourceGroup.height)
  }
  expect.soft(m.timelineFlexBasis, `时间轴的 flex-basis 应为 100%（换到第二行的机制）@ ${where}`).toBe('100%')

  // 恰一个速度选择器，且已上提出时间轴。
  expect.soft(m.speedCount).toBe(1)
  expect.soft(m.speedInsideTimeline, `竖屏下播放速度不应在 m11-timeline 里 @ ${where}`).toBe(false)

  // 条的位置：全宽减两侧 8px，底边距地图区底 40px，不压 attribution。
  const bottomInset = m.map.y + m.map.height - (m.bar.y + m.bar.height)
  expect.soft(Math.abs(bottomInset - BAR_BOTTOM_INSET), `控制条底边距地图区底 ${bottomInset}px @ ${where}`).toBeLessThanOrEqual(TOLERANCE)
  expect.soft(Math.abs(m.bar.x - m.map.x - BAR_SIDE_INSET), `控制条左边距 ${m.bar.x - m.map.x}px @ ${where}`).toBeLessThanOrEqual(TOLERANCE)
  expect
    .soft(Math.abs(m.bar.width - (m.map.width - 2 * BAR_SIDE_INSET)), `控制条宽 ${m.bar.width}，地图区宽 ${m.map.width} @ ${where}`)
    .toBeLessThanOrEqual(TOLERANCE)
  expect.soft(contains(m.map, m.bar), `控制条 ${JSON.stringify(m.bar)} 不在地图区 ${JSON.stringify(m.map)} 内 @ ${where}`).toBe(true)
  expect
    .soft(intersects(m.bar, m.attribution), `控制条 ${JSON.stringify(m.bar)} 与版权归属 ${JSON.stringify(m.attribution)} 相交 @ ${where}`)
    .toBe(false)
  // 条高由内容决定：两行必然高过 64px 的桌面 token。
  expect.soft(m.bar.height, `竖屏条高 ${m.bar.height} @ ${where}`).toBeGreaterThan(64 + TOLERANCE)

  // 尺寸与字号下限。
  for (const [name, box, fontSize] of [
    ['起报时次', m.cycle, m.cycleFontSizePx],
    ['播放速度', m.speed, m.speedFontSizePx],
  ] as const) {
    expect.soft(box.height, `${name}高 ${box.height} @ ${where}`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(fontSize, `${name}计算字号 ${fontSize}px @ ${where}`).toBeGreaterThanOrEqual(MIN_SELECT_FONT_PX)
  }
  for (const [index, button] of [...m.sourceButtons, ...m.stepButtons].entries()) {
    expect.soft(button.width, `按钮 ${index + 1} 宽 ${button.width} @ ${where}`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(button.height, `按钮 ${index + 1} 高 ${button.height} @ ${where}`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
  }

  // 启动器列不与控制条相交（竖屏条变高后的兄弟浮层）。
  const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
  expect.soft(intersects(column, m.bar), `启动器列 ${JSON.stringify(column)} 与控制条 ${JSON.stringify(m.bar)} 相交 @ ${where}`).toBe(false)

  // 禁用原因：自成第三行、在视口与控制条内，不把任何控件挤出视口（上面已逐个断言）。
  if (failClosed) {
    expect(m.reason, `fail-closed 目录下禁用原因应出现 @ ${where}`).not.toBeNull()
    const reason = m.reason!
    const viewportBox: Box = { x: 0, y: 0, width: m.viewport.width, height: m.viewport.height }
    expect.soft(contains(viewportBox, reason), `禁用原因 ${JSON.stringify(reason)} 不在视口内 @ ${where}`).toBe(true)
    expect.soft(contains(m.bar, reason), `禁用原因 ${JSON.stringify(reason)} 不在控制条 ${JSON.stringify(m.bar)} 内 @ ${where}`).toBe(true)
    expect.soft(reason.y, `禁用原因应在第二行之下 @ ${where}`).toBeGreaterThanOrEqual(m.timeline.y + m.timeline.height)
    // 单行截断：条高有上界，状态条容器的静态界限才可证。
    const lines = await controlBarParts(page).reason.evaluate(
      (element) => element.getBoundingClientRect().height / Number.parseFloat(getComputedStyle(element).lineHeight),
    )
    expect.soft(lines, `禁用原因应为单行，实际 ${lines} 行 @ ${where}`).toBeLessThanOrEqual(1.05)
  } else {
    expect(m.reason, `带有效时刻的目录下不应有禁用原因 @ ${where}`).toBeNull()
  }
  return m
}

test.describe('M11 控制条竖屏两行', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const catalog of CATALOGS) {
    test(`默认视口（${catalog.name}）：全部控件在视口内；竖屏另断言两行几何`, async ({ page }, testInfo) => {
      await installCatalog(page, catalog.failClosed)
      await openMap(page, catalog.failClosed)
      const where = label(page, testInfo)

      if (isShortLandscape(requireViewport(page))) {
        // 矮视口横屏：本文件只断言“全部控件在视口内”（约定；单行的其余断言归 3.8 的 spec）。
        const m = await measure(page, where, catalog.failClosed ? 'short-landscape fail-closed' : 'short-landscape')
        expectControlsInViewport(m, where)
        expect(m.speedCount).toBe(1)
      } else {
        await expectPortraitTwoRows(page, where, catalog.failClosed)
      }
    })

    for (const viewport of [PORTRAIT, NARROW]) {
      test(`${viewport.width}x${viewport.height}（${catalog.name}）：两行、控件在视口内、尺寸与字号下限、不压 attribution`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport)
        await installCatalog(page, catalog.failClosed)
        await openMap(page, catalog.failClosed)

        await expectPortraitTwoRows(page, label(page, testInfo), catalog.failClosed)
      })
    }
  }

  test('前进一步后的 valid-time query 与桌面同一操作的结果相同（390x664 对 1280x900）', async ({ page }, testInfo) => {
    await page.setViewportSize(PORTRAIT)
    await installCatalog(page, false)

    const stepOnce = async () => {
      await openMap(page, false)
      const before = await page.evaluate(() => window.location.search)
      const speedInsideTimeline = (await controlBarParts(page).timeline.getByLabel('播放速度').count()) === 1
      await controlBarParts(page).stepButtons[2].click()
      await expect.poll(() => page.evaluate(() => window.location.search)).not.toBe(before)
      return { before, after: await page.evaluate(() => window.location.search), speedInsideTimeline }
    }

    const portrait = await stepOnce()
    await page.setViewportSize(DESKTOP)
    const desktop = await stepOnce()
    console.log(`control-bar valid-time @ ${testInfo.project.name}`, JSON.stringify({ portrait, desktop }))

    // 两次确实分属两种布局：竖屏的速度选择器在时间轴外，桌面在时间轴内。
    expect(portrait.speedInsideTimeline).toBe(false)
    expect(desktop.speedInsideTimeline).toBe(true)
    expect(new URLSearchParams(portrait.after).get('validTime'), '前进一步应写入 validTime').not.toBeNull()
    expect(portrait.after).toBe(desktop.after)
    expect(portrait.after).not.toBe(portrait.before)
    expect(desktop.after).not.toBe(desktop.before)
  })

  for (const viewport of [PORTRAIT, NARROW]) {
    for (const catalog of CATALOGS) {
      test(`${viewport.width}x${viewport.height}（${catalog.name}）：展开图例面板后面板底边仍在控制条顶边之上`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport)
        await installCatalog(page, catalog.failClosed)
        await openMap(page, catalog.failClosed)

        const { launcher, panel } = overlayPart(page, 'legend')
        await launcher.tap()
        await expectOnlyExpanded(page, 'legend')

        const map = await boxOf(page.locator(MAP_REGION), '地图区')
        const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
        const panelBox = await boxOf(panel, '图例面板')
        console.log(`control-bar legend panel @ ${label(page, testInfo)}`, JSON.stringify({ map, bar, panel: panelBox }))
        expect(contains(map, panelBox), `图例面板 ${JSON.stringify(panelBox)} 不在地图区内`).toBe(true)
        expect(panelBox.y + panelBox.height, `图例面板底边应在控制条顶边 ${bar.y} 之上`).toBeLessThanOrEqual(bar.y + TOLERANCE)
        expect(intersects(panelBox, bar), '图例面板与控制条相交').toBe(false)
      })
    }
  }

  // 状态条容器的竖屏底部界限：320×568 + fail-closed 是最高的条（「Analysis / Forecast」折成三行 + 禁用原因行）。
  for (const viewport of [PORTRAIT, NARROW]) {
    for (const catalog of CATALOGS) {
      test(`${viewport.width}x${viewport.height}（${catalog.name}）：地图源错误状态条与浮动提示不与控制条相交，状态条下方的点命中地图画布`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport)
        const mocks = await installNoticeMocks(page, { failBasemapTiles: true, emptyStations: true })
        if (catalog.failClosed) await installFailClosedDischargeLayer(page)
        await openMap(page, catalog.failClosed, STATION_LAYER_URL)

        const notice = page.getByTestId(NOTICE_TEST_IDS.stationStatus)
        const status = page.getByTestId(STATUS_TEST_IDS.mapSourceError)
        await expect(notice).toHaveText(STATION_EMPTY_TEXT)
        await expect(status).toHaveText(BASEMAP_UNAVAILABLE_TEXT)
        expect(mocks.failedBasemapTiles(), '状态条应由真实的底图瓦片 503 点亮').toBeGreaterThan(0)

        const container = page.getByTestId(STATUS_CONTAINER)
        await expect(container).toHaveCSS('pointer-events', 'none')
        const boxes = {
          map: await boxOf(page.locator(MAP_REGION), '地图区'),
          bar: await boxOf(page.locator(CONTROL_BAR), '控制条'),
          notice: await boxOf(notice, '代站状态提示'),
          status: await boxOf(status, '地图源错误状态条'),
          container: await boxOf(container, '状态条容器'),
          role: await boxOf(page.getByLabel('Role'), '角色切换器'),
        }
        const hits = await hitsBelow(page, boxes.container, boxes.status, [boxes.role])
        console.log(
          `control-bar status container @ ${label(page, testInfo)}`,
          JSON.stringify({
            ...boxes,
            containerBottomToBarTop: boxes.bar.y - (boxes.container.y + boxes.container.height),
            checkedPoints: hits.checked,
          }),
        )

        expect(intersects(boxes.notice, boxes.bar), `代站状态提示 ${JSON.stringify(boxes.notice)} 与控制条相交`).toBe(false)
        expect(intersects(boxes.status, boxes.bar), `地图源错误状态条 ${JSON.stringify(boxes.status)} 与控制条相交`).toBe(false)
        expect(contains(boxes.map, boxes.container), `状态条容器 ${JSON.stringify(boxes.container)} 不在地图区内`).toBe(true)
        expect(
          intersects(boxes.container, boxes.bar),
          `状态条容器 ${JSON.stringify(boxes.container)} 与控制条 ${JSON.stringify(boxes.bar)} 相交`,
        ).toBe(false)
        expect(hits.checked, '状态条下方应有可检查的点，否则命中断言是空的').toBeGreaterThan(0)
        expect(hits.misses, '状态条下方、容器范围内有点没命中地图画布').toEqual([])
      })
    }
  }
})
