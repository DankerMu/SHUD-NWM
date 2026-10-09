import { expect, test, type Page, type TestInfo } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  expectSheet,
  installMultipleIssueTimes,
  intersectionOf,
  type CurveWindowKind,
} from './support/curveSheet.mocked'
import { contains, type Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import { CHART_FLOOR_PX, CHART_FLOOR_SHORT_LANDSCAPE_PX, measureSettledSheetChart } from './support/sheetChart.mocked'
import {
  FORM_SELECT_SELECTOR,
  INTERACTIVE_SELECTOR,
  MIN_FORM_FONT_PX,
  MIN_TOUCH_TARGET_PX,
  SHEET_CHART,
  STATION_VARIABLES,
  STATION_VARIABLE_SELECTOR,
  belowTouchTarget,
  installIssueTimes,
  measureControl,
  measureControls,
  measureListScroller,
  mockIssueTimes,
  openSheetWithControls,
  sheetControlParts,
  stationVariableToggle,
  withinHorizontally,
  type ControlMeasure,
} from './support/sheetControls.mocked'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 抽屉内控件的触控与表单字号下限（openspec mobile-responsive-display task 4.9，design.md D13）。
 *
 * 用例 (a)–(h) 对应 tasks.md 里 #2810 的 Triage。每条用例在用例内 `setViewportSize`，所以三个移动
 * project 下执行的是同一组断言，不按 project 跳过。全部在“曲线已加载”（图表 canvas 可见）之后量，
 * 判据取自包围盒与计算样式。桌面对照在 `m11-sheet-controls-desktop.mocked.spec.ts`。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
/**
 * 要素切换项折成两行的移动视口：右侧抽屉宽 284px，切换项那一行可用 250px，五项合计约 265px
 * （实测：前四项一行、Rn 落到第二行）。页面最小宽 320px，所以底部抽屉（宽 ≥ 320）里五项总是一行。
 */
const NARROW_SHEET: ViewportSize = { width: 568, height: 320 }
const AUDIT_VIEWPORTS = [PORTRAIT, LANDSCAPE]
/** 足以让 750×342 的下拉列表溢出的时次数（每项 44px，列表可用高约 150px）。 */
const MANY_ISSUE_TIMES = mockIssueTimes(8)
/** 口径 (4)：图表区底边在主体可视底边之上至少留这么多。 */
const CHART_BOTTOM_MARGIN_PX = 2

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function report(name: string, where: string, measure: unknown) {
  console.log(`sheet-controls ${name} @ ${where}`, JSON.stringify(measure))
}

function viewportBox(page: Page): Box {
  const viewport = requireViewport(page)
  return { x: 0, y: 0, width: viewport.width, height: viewport.height }
}

/** 展开起报时次列表，返回每个选项（portal 里的真实选项节点）的测量。 */
async function expandIssueTimes(page: Page, kind: CurveWindowKind, expectedCount: number, where: string): Promise<ControlMeasure[]> {
  const parts = sheetControlParts(page, kind)
  await expect(parts.trigger).toBeEnabled()
  await parts.trigger.tap()
  await expect(parts.options).toHaveCount(expectedCount)
  const options = await measureControls(parts.options)
  const listbox = await boxOf(parts.listbox, '起报时次列表')
  report('issue-time list', where, { listbox, options })
  expect(options, '每个选项都应可量').toHaveLength(expectedCount)
  for (const option of options) {
    expect(option.box.height, `选项「${option.name}」高 @ ${where}`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX)
    expect(option.fontSizePx, `选项「${option.name}」字号 @ ${where}`).toBe(MIN_FORM_FONT_PX)
  }
  expect(contains(viewportBox(page), listbox), `列表盒 ${JSON.stringify(listbox)} 应在视口内 @ ${where}`).toBe(true)
  return options
}

/** 五个要素切换项：各自 ≥ 44×44、在抽屉水平范围内、两两不重叠。返回按 DOM 顺序的测量。 */
async function expectVariableToggles(page: Page, where: string): Promise<ControlMeasure[]> {
  const frame = page.getByTestId(CURVE_WINDOWS.station.testId)
  const sheet = await boxOf(frame, '气象代站抽屉')
  const toggles = await measureControls(frame.getByTestId(STATION_VARIABLE_SELECTOR).getByRole('tab'))
  report('variable toggles', where, { sheet, toggles })
  expect(toggles.map((toggle) => toggle.name)).toEqual([...STATION_VARIABLES])
  expect(belowTouchTarget(toggles), `不到 44×44 的要素切换项 @ ${where}`).toEqual([])
  for (const toggle of toggles) {
    expect(withinHorizontally(sheet, toggle.box), `切换项「${toggle.name}」${JSON.stringify(toggle.box)} 应在抽屉水平范围内 @ ${where}`).toBe(true)
  }
  for (const [index, first] of toggles.entries()) {
    for (const second of toggles.slice(index + 1)) {
      expect(intersectionOf(first.box, second.box), `切换项「${first.name}」与「${second.name}」不应重叠 @ ${where}`).toBeNull()
    }
  }
  return toggles
}

test.describe('M11 抽屉内控件触控下限', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const kind of CURVE_WINDOW_KINDS) {
    const name = CURVE_WINDOWS[kind].name

    test(`(a) 390x664 ${name}：关闭按钮 ≥ 44×44 且在抽屉盒内`, async ({ page }, testInfo) => {
      await openSheetWithControls(page, PORTRAIT, kind)
      const where = label(page, testInfo)
      const sheet = await expectSheet(page, kind, where)

      const close = await measureControl(sheetControlParts(page, kind).close, '关闭按钮')
      report('(a) close', where, close)
      expect(close.box.width, '关闭按钮宽').toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX)
      expect(close.box.height, '关闭按钮高').toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX)
      expect(contains(sheet.frame, close.box), `关闭按钮 ${JSON.stringify(close.box)} 应在抽屉盒 ${JSON.stringify(sheet.frame)} 内`).toBe(true)
    })

    test(`(b) 390x664 ${name}：起报时次触发器高 ≥ 44、字号 ≥ 16px、在抽屉水平范围内；选项高 ≥ 44、列表在视口内，可选中另一个时次`, async ({ page }, testInfo) => {
      await openSheetWithControls(page, PORTRAIT, kind, installMultipleIssueTimes)
      const where = label(page, testInfo)
      const sheet = await expectSheet(page, kind, where)
      const parts = sheetControlParts(page, kind)

      const trigger = await measureControl(parts.trigger, '起报时次触发器')
      report('(b) trigger', where, trigger)
      expect(trigger.box.height, '触发器高').toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX)
      expect(trigger.fontSizePx, '触发器字号').toBeGreaterThanOrEqual(MIN_FORM_FONT_PX)
      expect(withinHorizontally(sheet.frame, trigger.box), `触发器 ${JSON.stringify(trigger.box)} 应在抽屉水平范围内`).toBe(true)

      const defaultText = (await parts.trigger.innerText()).trim()
      expect(defaultText).not.toBe('')
      const options = await expandIssueTimes(page, kind, 2, where)
      const other = options.find((option) => option.name !== defaultText)
      expect(other, `应有一个不同于默认项「${defaultText}」的时次`).toBeDefined()
      await parts.options.filter({ hasText: other!.name }).tap()
      await expect(parts.listbox).toHaveCount(0)
      await expect(parts.trigger, '选中后触发器显示该时次').toHaveText(other!.name)
      await expect(parts.trigger, '控件仍可用').toBeEnabled()
    })
  }

  test('(c) 390x664 气象代站窗：五个要素切换项各 ≥ 44×44、在抽屉水平范围内、互不重叠、独占一行；轻触另一个要素后它成为选中项', async ({ page }, testInfo) => {
    await openSheetWithControls(page, PORTRAIT, 'station')
    const where = label(page, testInfo)
    await expectSheet(page, 'station', where)

    const toggles = await expectVariableToggles(page, where)
    // 独占一行：切换项全部排在起报时次条的下方，而不是挤在它右侧。
    const issueTimeBar = await boxOf(sheetControlParts(page, 'station').issueTimeBar, '起报时次条')
    for (const toggle of toggles) {
      expect(toggle.box.y, `切换项「${toggle.name}」顶边应不高于起报时次条底边 ${issueTimeBar.y + issueTimeBar.height}`).toBeGreaterThanOrEqual(
        issueTimeBar.y + issueTimeBar.height,
      )
    }

    await expect(stationVariableToggle(page, 'PRCP')).toHaveAttribute('aria-selected', 'true')
    await stationVariableToggle(page, 'TEMP').tap()
    await expect(stationVariableToggle(page, 'TEMP')).toHaveAttribute('aria-selected', 'true')
    await expect(stationVariableToggle(page, 'PRCP')).toHaveAttribute('aria-selected', 'false')
    await expect(page.getByTestId('m11-station-variable-TEMP-chart')).toBeVisible()
  })

  test('(d) 568x320 气象代站窗：要素切换项折成两行时各 ≥ 44×44、在抽屉水平范围内、互不重叠', async ({ page }, testInfo) => {
    await openSheetWithControls(page, NARROW_SHEET, 'station')
    const where = label(page, testInfo)
    expect(isShortLandscape(requireViewport(page))).toBe(true)
    await expectSheet(page, 'station', where)

    const toggles = await expectVariableToggles(page, where)
    // 硬前提：这个视口里切换项确实折行了——否则上面的断言没有验证“可换行”。
    const rowTops = new Set(toggles.map((toggle) => Math.round(toggle.box.y)))
    expect(rowTops.size, `前提：切换项应至少折成两行，实测行顶 ${JSON.stringify([...rowTops])}`).toBeGreaterThanOrEqual(2)
  })

  for (const viewport of AUDIT_VIEWPORTS) {
    for (const kind of CURVE_WINDOW_KINDS) {
      const name = CURVE_WINDOWS[kind].name

      test(`(e) ${viewport.width}x${viewport.height} ${name}：抽屉内每个可见的可交互控件 ≥ 44×44`, async ({ page }, testInfo) => {
        await openSheetWithControls(page, viewport, kind)
        const where = label(page, testInfo)
        await expectSheet(page, kind, where)

        const controls = await measureControls(page.getByTestId(CURVE_WINDOWS[kind].testId).locator(INTERACTIVE_SELECTOR))
        report('(e) audit', where, controls)
        // 选择器没匹配到任何东西时不让审计静默通过。
        expect(controls.length, '审计到的可交互控件数').toBeGreaterThan(0)
        expect(belowTouchTarget(controls), `不到 44×44 的控件（共审计 ${controls.length} 个）@ ${where}`).toEqual([])
      })

      test(`(f) ${viewport.width}x${viewport.height} ${name}：抽屉内每个可见的选择控件字号 ≥ 16px`, async ({ page }, testInfo) => {
        await openSheetWithControls(page, viewport, kind)
        const where = label(page, testInfo)
        await expectSheet(page, kind, where)

        const selects = await measureControls(page.getByTestId(CURVE_WINDOWS[kind].testId).locator(FORM_SELECT_SELECTOR))
        report('(f) audit', where, selects)
        expect(selects.length, '审计到的选择控件数').toBeGreaterThan(0)
        expect(
          selects.filter((select) => select.fontSizePx < MIN_FORM_FONT_PX).map((select) => `${select.name} ${select.fontSizePx}px`),
          `字号不到 16px 的选择控件 @ ${where}`,
        ).toEqual([])
      })
    }
  }

  for (const kind of CURVE_WINDOW_KINDS) {
    test(`(g) 750x342 ${CURVE_WINDOWS[kind].name}：${MANY_ISSUE_TIMES.length} 个起报时次——列表在视口内并自身滚动，最后一个选项可滚入可视区并选中`, async ({ page }, testInfo) => {
      await openSheetWithControls(page, LANDSCAPE, kind, (target) => installIssueTimes(target, MANY_ISSUE_TIMES))
      const where = label(page, testInfo)
      await expectSheet(page, kind, where)
      const parts = sheetControlParts(page, kind)

      const options = await expandIssueTimes(page, kind, MANY_ISSUE_TIMES.length, where)
      const scroller = await measureListScroller(page)
      report('(g) scroller', where, scroller)
      // 硬前提：列表确实溢出——否则“可滚入”是空转。
      expect(scroller.scrollHeight, `前提：列表应溢出（scrollHeight ${scroller.scrollHeight} > clientHeight ${scroller.clientHeight}）`).toBeGreaterThan(scroller.clientHeight)

      const last = parts.options.last()
      const lastText = options[options.length - 1].name
      const listbox = await boxOf(parts.listbox, '起报时次列表')
      expect(contains(listbox, await boxOf(last, '最后一个选项')), '前提：滚动前最后一个选项不在列表可视区内').toBe(false)
      // 轮询：列表第一次离开顶部时，选择组件新挂上的“向上滚动”按钮会把焦点项（当前时次）拉回视野，
      // 这一次滚动会被它抵消；按钮挂上之后再滚就不会了。
      await expect(async () => {
        await last.evaluate((element) => element.scrollIntoView({ block: 'nearest' }))
        const lastBox = await boxOf(last, '最后一个选项')
        expect(contains(listbox, lastBox), `滚动后最后一个选项 ${JSON.stringify(lastBox)} 应在列表盒 ${JSON.stringify(listbox)} 内`).toBe(true)
      }).toPass({ timeout: 4_000 })
      expect((await measureListScroller(page)).scrollTop, '列表自身应滚动了').toBeGreaterThan(0)
      expect(await boxOf(parts.listbox, '起报时次列表'), '列表滚动不改变列表盒').toEqual(listbox)

      await last.tap()
      await expect(parts.listbox).toHaveCount(0)
      await expect(parts.trigger, '选中后触发器显示最后一个时次').toHaveText(lastText)
    })
  }

  test('(h) 390x664 气象代站窗：图表区高 ≥ 160、不滚动就在抽屉盒内，底边在主体可视底边之上 ≥ 2px', async ({ page }, testInfo) => {
    await openSheetWithControls(page, PORTRAIT, 'station')
    const where = label(page, testInfo)
    await expectSheet(page, 'station', where)

    const measure = await measureSettledSheetChart(page, SHEET_CHART.station, where)
    const bodyBottom = measure.body.visible.y + measure.body.visible.height
    const chartBottom = measure.chart.y + measure.chart.height
    report('(h)', where, { ...measure, bodyOverflow: measure.body.scrollHeight - measure.body.clientHeight, chartBottomMargin: bodyBottom - chartBottom })
    expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 ≥ ${CHART_FLOOR_PX}`).toBeGreaterThanOrEqual(CHART_FLOOR_PX)
    expect(measure.body.scrollTop, '没有滚动过').toBe(0)
    expect(contains(measure.frame, measure.chart), `图表区 ${JSON.stringify(measure.chart)} 应在抽屉盒 ${JSON.stringify(measure.frame)} 内`).toBe(true)
    expect(bodyBottom - chartBottom, `图表区底边 ${chartBottom} 应在主体可视底边 ${bodyBottom} 之上 ≥ ${CHART_BOTTOM_MARGIN_PX}px`).toBeGreaterThanOrEqual(CHART_BOTTOM_MARGIN_PX)
  })

  // 口径 (5) 的河段侧：起报时次条长高后，750×342 的河段抽屉仍与改动前一样不咬下限、不溢出
  // （改动前图表区 127.5px；条的移动形态内边距不收紧时图表区被钉在 120、主体溢出 8.5px）。
  test('(h) 750x342 河段窗：起报时次条长高后图表区仍 > 120、主体不溢出', async ({ page }, testInfo) => {
    await openSheetWithControls(page, LANDSCAPE, 'river')
    const where = label(page, testInfo)
    await expectSheet(page, 'river', where)

    const measure = await measureSettledSheetChart(page, SHEET_CHART.river, where)
    report('(h) river', where, measure)
    expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 > ${CHART_FLOOR_SHORT_LANDSCAPE_PX}（没有被钉在下限）`).toBeGreaterThan(CHART_FLOOR_SHORT_LANDSCAPE_PX)
    expect(measure.body.scrollHeight, '主体不应溢出').toBe(measure.body.clientHeight)
  })
})
