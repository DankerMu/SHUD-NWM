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
import { contains, intersects } from './support/legendLauncher.mocked'
import { LAUNCHER_COLUMN, OPS_LINK, boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { setRole } from './support/setRole'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 控制条矮视口横屏单行（openspec mobile-responsive-display task 3.8，design.md D6）。
 * 每条用例都在用例内 `setViewportSize`：两个验收视口（750×342、844×390）因此在三个移动 project 下
 * 都被同一组断言覆盖（竖屏 project 也跑），不按 project 跳过。
 */

const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const LANDSCAPE_WIDE: ViewportSize = { width: 844, height: 390 }
/** 高 < 500 但不是横屏：移动形态的竖屏两行，矮视口横屏专属类不得生效。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }
const TOLERANCE = 0.5
const MIN_TOUCH_TARGET = 44
const MIN_SLIDER_WIDTH = 120
const MIN_SELECT_FONT_PX = 16
const BAR_HEIGHT = 64
/** 控制条底边距地图区底边（attribution 带）。 */
const BAR_BOTTOM_INSET = 40
/**
 * 时间轴在条内（#2864）：流高 = 20 + (8+16) + (4+16) = 64px，实测与条同高、上下溢出 0；
 * 滑块行的行盒回到 21px（+5px）或底行折行（+16px / 行）都会超过这两个上限。
 */
const MAX_TIMELINE_HEIGHT = BAR_HEIGHT + TOLERANCE
const MAX_TIMELINE_OVERHANG = TOLERANCE
/** 桌面 / 横屏单行里禁用原因的既有宽度上限（`max-w-48`）；矮视口横屏的专属上限必须比它小。 */
const DESKTOP_REASON_MAX_WIDTH = 192
const TIMELINE_ROWS = 'm11-timeline-rows'
const FAIL_CLOSED_REASON = 'No cycle covers every basin, so the national discharge layer is disabled.'

/** 两种目录：带 9 个有效时刻与一个起报时次；fail-closed（无周期 + 禁用原因——单行最挤的状态）。 */
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
async function openMap(page: Page, failClosed: boolean) {
  await page.goto('/')
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

function rowsMinWidth(page: Page) {
  return page.getByTestId(TIMELINE_ROWS).evaluate((element) => getComputedStyle(element).minWidth)
}

async function measure(page: Page, where: string, tag: string): Promise<ControlBarMeasure & { rows: Awaited<ReturnType<typeof boxOf>> }> {
  const measured = {
    ...(await measureControlBar(page, requireViewport(page))),
    rows: await boxOf(page.getByTestId(TIMELINE_ROWS), '时间轴行列'),
  }
  console.log(`control-bar ${tag} @ ${where}`, JSON.stringify(measured))
  return measured
}

/** 矮视口横屏单行的整组断言（Risk pack「Public API / entry」与「Resource limits / 溢出」）。 */
async function expectLandscapeSingleRow(page: Page, where: string, failClosed: boolean) {
  expect(isShortLandscape(requireViewport(page)), `${where} 应是矮视口横屏`).toBe(true)
  const m = await measure(page, where, failClosed ? 'landscape fail-closed' : 'landscape')
  expectControlsInViewport(m, where)

  // 滑块 ≥ 120px：硬断言（改动前 750×342 fail-closed 为 27.5px）。
  expect(m.slider.width, `滑块宽 ${m.slider.width} @ ${where}`).toBeGreaterThanOrEqual(MIN_SLIDER_WIDTH)

  // 单行：滑块、步进 / 播放按钮、两个选择器都与预报源分段纵向重叠。
  expect.soft(overlapsVertically(m.slider, m.sourceGroup), `滑块与预报源分段应同在一行 @ ${where}`).toBe(true)
  expect.soft(overlapsVertically(m.cycle, m.sourceGroup), `起报时次与预报源分段应同在一行 @ ${where}`).toBe(true)
  expect.soft(overlapsVertically(m.speed, m.sourceGroup), `播放速度与预报源分段应同在一行 @ ${where}`).toBe(true)
  for (const [index, button] of m.stepButtons.entries()) {
    expect.soft(overlapsVertically(button, m.sourceGroup), `步进 / 播放按钮 ${index + 1} 与预报源分段应同在一行 @ ${where}`).toBe(true)
  }

  // 恰一个速度选择器，仍在时间轴里（竖屏才上提）。
  expect.soft(m.speedCount).toBe(1)
  expect.soft(m.speedInsideTimeline, `矮视口横屏下播放速度应在 m11-timeline 里 @ ${where}`).toBe(true)

  // 条的位置：高 64、底边距地图区底 40px、在地图区内、不压 attribution。
  const bottomInset = m.map.y + m.map.height - (m.bar.y + m.bar.height)
  expect.soft(Math.abs(m.bar.height - BAR_HEIGHT), `控制条高 ${m.bar.height} @ ${where}`).toBeLessThanOrEqual(TOLERANCE)
  expect.soft(Math.abs(bottomInset - BAR_BOTTOM_INSET), `控制条底边距地图区底 ${bottomInset}px @ ${where}`).toBeLessThanOrEqual(TOLERANCE)
  expect.soft(contains(m.map, m.bar), `控制条 ${JSON.stringify(m.bar)} 不在地图区 ${JSON.stringify(m.map)} 内 @ ${where}`).toBe(true)
  expect
    .soft(intersects(m.bar, m.attribution), `控制条 ${JSON.stringify(m.bar)} 与版权归属 ${JSON.stringify(m.attribution)} 相交 @ ${where}`)
    .toBe(false)

  // 尺寸与字号下限（同 3.7）。
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

  // 时间轴在条内：盒高 ≤ 64、上下都不超出控制条（各留 0.5px 容差）——折行或滑块行行盒变高都会红。
  const overhangTop = m.bar.y - m.timeline.y
  const overhangBottom = m.timeline.y + m.timeline.height - (m.bar.y + m.bar.height)
  expect.soft(m.timeline.height, `时间轴盒高 ${m.timeline.height} @ ${where}`).toBeLessThanOrEqual(MAX_TIMELINE_HEIGHT)
  expect.soft(overhangTop, `时间轴顶边超出控制条 ${overhangTop}px @ ${where}`).toBeLessThanOrEqual(MAX_TIMELINE_OVERHANG)
  expect.soft(overhangBottom, `时间轴底边超出控制条 ${overhangBottom}px @ ${where}`).toBeLessThanOrEqual(MAX_TIMELINE_OVERHANG)

  // 防“溢出式通过”：滑块（及其所在列）必须真的在时间轴盒里，时间轴在控制条的水平范围内。
  expect.soft(contains(m.timeline, m.slider), `滑块 ${JSON.stringify(m.slider)} 不在时间轴盒 ${JSON.stringify(m.timeline)} 内 @ ${where}`).toBe(true)
  expect
    .soft(m.rows.x + m.rows.width, `时间轴行列右边 ${m.rows.x + m.rows.width} 超出时间轴盒右边 ${m.timeline.x + m.timeline.width} @ ${where}`)
    .toBeLessThanOrEqual(m.timeline.x + m.timeline.width + TOLERANCE)
  expect.soft(m.timeline.x, `时间轴左边应在控制条内 @ ${where}`).toBeGreaterThanOrEqual(m.bar.x)
  expect.soft(m.timeline.x + m.timeline.width, `时间轴右边应在控制条内 @ ${where}`).toBeLessThanOrEqual(m.bar.x + m.bar.width + TOLERANCE)

  if (failClosed) {
    expect(m.reason, `fail-closed 目录下禁用原因应出现 @ ${where}`).not.toBeNull()
    const reason = m.reason!
    expect.soft(intersects(m.slider, reason), `滑块 ${JSON.stringify(m.slider)} 压到禁用原因 ${JSON.stringify(reason)} 上 @ ${where}`).toBe(false)
    expect.soft(intersects(m.rows, reason), `时间轴行列 ${JSON.stringify(m.rows)} 压到禁用原因 ${JSON.stringify(reason)} 上 @ ${where}`).toBe(false)
    const reasonLocator = controlBarParts(page).reason
    await expect.soft(reasonLocator, '禁用原因的 title 应为全文').toHaveAttribute('title', FAIL_CLOSED_REASON)
    if (reason.width > 0) {
      expect.soft(contains(m.bar, reason), `禁用原因 ${JSON.stringify(reason)} 不在控制条 ${JSON.stringify(m.bar)} 内 @ ${where}`).toBe(true)
      const lines = await reasonLocator.evaluate(
        (element) => element.getBoundingClientRect().height / Number.parseFloat(getComputedStyle(element).lineHeight),
      )
      expect.soft(lines, `禁用原因应为单行，实际 ${lines} 行 @ ${where}`).toBeLessThanOrEqual(1.5)
    }
  } else {
    expect(m.reason, `带有效时刻的目录下不应有禁用原因 @ ${where}`).toBeNull()
  }

  // 启动器列（含 operator 的运维入口）不与控制条相交——依赖条高仍是 64px。
  await setRole(page, 'operator')
  await expect(page.locator(LAUNCHER_COLUMN).locator(OPS_LINK), '运维入口应在启动器列里').toBeVisible()
  const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
  const ops = await boxOf(page.locator(OPS_LINK), '运维入口')
  const bar = await boxOf(controlBarParts(page).bar, '控制条')
  console.log(`control-bar launcher column @ ${where}`, JSON.stringify({ column, ops, bar, opsBottomToBarTop: bar.y - (ops.y + ops.height) }))
  expect.soft(intersects(column, bar), `启动器列 ${JSON.stringify(column)} 与控制条 ${JSON.stringify(bar)} 相交 @ ${where}`).toBe(false)
  expect.soft(intersects(ops, bar), `运维入口 ${JSON.stringify(ops)} 与控制条 ${JSON.stringify(bar)} 相交 @ ${where}`).toBe(false)
  return m
}

test.describe('M11 控制条矮视口横屏单行', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const viewport of [LANDSCAPE, LANDSCAPE_WIDE]) {
    for (const catalog of CATALOGS) {
      test(`${viewport.width}x${viewport.height}（${catalog.name}）：单行、滑块 ≥ 120、条高 64、控件在视口内、尺寸与字号下限、不压 attribution`, async ({ page }, testInfo) => {
        await page.setViewportSize(viewport)
        await installCatalog(page, catalog.failClosed)
        await openMap(page, catalog.failClosed)

        await expectLandscapeSingleRow(page, label(page, testInfo), catalog.failClosed)
      })
    }

    test(`${viewport.width}x${viewport.height}：点「下一个有效时刻」后 validTime query 变化`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await installCatalog(page, false)
      await openMap(page, false)

      const before = await page.evaluate(() => window.location.search)
      await controlBarParts(page).stepButtons[2].click()
      await expect.poll(() => page.evaluate(() => window.location.search)).not.toBe(before)
      const after = new URLSearchParams(await page.evaluate(() => window.location.search))
      expect(after.get('validTime'), '前进一步应写入 validTime').not.toBeNull()
      expect(after.get('validTime')).not.toBe(new URLSearchParams(before).get('validTime'))
    })
  }

  // 形态归属 oracle（#2790 携带项）：`mobile-landscape` 变体在横屏命中、在竖向的矮视口不命中。
  test('320x480 是竖屏两行且矮视口横屏专属类不生效；转到 750x342 后生效（m11-timeline-rows 的计算 min-width）', async ({ page }, testInfo) => {
    await page.setViewportSize(SHORT_PORTRAIT)
    await installCatalog(page, true)
    await openMap(page, true)
    expect(isShortLandscape(requireViewport(page))).toBe(false)

    const where = label(page, testInfo)
    const portrait = await measure(page, where, 'short-portrait fail-closed')
    const portraitMinWidth = await rowsMinWidth(page)
    const reasonMaxWidth = () => controlBarParts(page).reason.evaluate((element) => getComputedStyle(element).maxWidth)
    console.log(`control-bar form oracle @ ${where}`, JSON.stringify({ rowsMinWidth: portraitMinWidth, reasonMaxWidth: await reasonMaxWidth() }))

    expectControlsInViewport(portrait, where)
    // 竖屏两行：滑块在预报源分段之下，速度已上提出时间轴。
    expect(portrait.slider.y, '320x480 应是竖屏两行').toBeGreaterThanOrEqual(portrait.sourceGroup.y + portrait.sourceGroup.height)
    expect(portrait.speedInsideTimeline).toBe(false)
    expect(portraitMinWidth, '矮视口横屏专属的 120px 最小宽度不应在 320x480 生效').not.toBe('120px')
    // 禁用原因自成第三行、占满条内容宽，不受横屏宽度上限影响。
    expect(portrait.reason).not.toBeNull()
    const portraitReason = portrait.reason!
    expect(portraitReason.y, '禁用原因应在时间轴之下自成一行').toBeGreaterThanOrEqual(portrait.timeline.y + portrait.timeline.height)
    expect(await reasonMaxWidth(), '竖屏的禁用原因不应有宽度上限').toBe('none')
    expect(portraitReason.width, `竖屏禁用原因宽 ${portraitReason.width} 应超过横屏上限`).toBeGreaterThan(DESKTOP_REASON_MAX_WIDTH)
    expect(Math.abs(portraitReason.width - portrait.timeline.width), '禁用原因应与第二行（时间轴）同宽').toBeLessThanOrEqual(TOLERANCE)

    // 旋转到矮视口横屏：同一页面、不重新导航，专属类随媒体查询生效。
    await page.setViewportSize(LANDSCAPE)
    expect(isShortLandscape(requireViewport(page))).toBe(true)
    await expect.poll(() => rowsMinWidth(page), '矮视口横屏专属的 120px 最小宽度应在 750x342 生效').toBe('120px')
    const landscapeWhere = label(page, testInfo)
    const landscape = await measure(page, landscapeWhere, 'rotated fail-closed')
    expect(landscape.slider.width).toBeGreaterThanOrEqual(MIN_SLIDER_WIDTH)
    expect(overlapsVertically(landscape.slider, landscape.sourceGroup), '750x342 应是单行').toBe(true)
    expect(landscape.speedInsideTimeline).toBe(true)
    // 禁用原因在横屏被专属上限压窄（比桌面的 192px 上限小）。
    const landscapeMaxWidth = Number.parseFloat(await reasonMaxWidth())
    expect(landscapeMaxWidth, `横屏禁用原因的计算 max-width ${landscapeMaxWidth}`).toBeLessThan(DESKTOP_REASON_MAX_WIDTH)
    expect(landscape.reason!.width).toBeLessThanOrEqual(landscapeMaxWidth + TOLERANCE)
  })
})
