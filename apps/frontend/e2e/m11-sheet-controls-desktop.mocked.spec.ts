import { expect, test, type Page } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  RETENTION_UNAVAILABLE_SUFFIX,
  installMultipleIssueTimes,
  installRetainedIssueTime,
} from './support/curveSheet.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import {
  ISSUE_TIME_BAR,
  MIN_TOUCH_TARGET_PX,
  STATION_LOADED,
  STATION_TOOLBAR,
  STATION_VARIABLES,
  STATION_VARIABLE_SELECTOR,
  measureControl,
  measureControls,
  measureIssueTimeTrigger,
  openSheetWithControls,
  sheetControlParts,
} from './support/sheetControls.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 曲线窗控件的桌面不变量（openspec mobile-responsive-display task 4.9 的 Risk pack「Legacy compatibility」）。
 *
 * 4.9 只加带移动前缀的尺寸类；桌面形态下这些控件的包围盒与计算字号必须与改动前相同。下面的字面值
 * 量自改动前的 `src/`（`origin/master` d0a570f6b，1280×900，Chromium），改动前后都绿。
 * 只钉由 CSS 决定的量（高度、定宽、字号、内边距）；由字体决定的文本宽度（气象代站触发器、要素切换项）
 * 不写字面值，改为钉“没有被撑到触控下限”（切换项计算 `min-width` 为 `auto`、RH / Rn 宽 < 44）。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
/** 「保留时次」用例的视口（openspec issue-time-trigger-truncate task 1.4）。 */
const DESKTOP_1280x800: ViewportSize = { width: 1280, height: 800 }
const TOLERANCE_PX = 0.01

const MASTER_1280x900 = {
  closeSize: 28,
  triggerHeight: 28,
  /** 河段触发器占满剩余宽度后被 `max-w-[12rem]` 截到 192px。 */
  riverTriggerWidth: 192,
  controlFontPx: 11,
  optionHeight: 24.5,
  optionFontPx: 11,
  toggleHeight: 28,
  /** 起报时次条 / 工具条：28 + 上下内边距 16 + 下边线 1。 */
  barHeight: 45,
  stationLoadedPaddingTop: '10px',
}

function expectNear(actual: number, expected: number, name: string) {
  expect(Math.abs(actual - expected), `${name} ${actual} 应为 ${expected}`).toBeLessThanOrEqual(TOLERANCE_PX)
}

async function heightOf(page: Page, testId: string) {
  return (await boxOf(page.getByTestId(testId), testId)).height
}

test.describe('M11 曲线窗控件桌面尺寸不变', () => {
  for (const kind of CURVE_WINDOW_KINDS) {
    test(`(i) 1280x900 ${CURVE_WINDOWS[kind].name}：关闭按钮、起报时次触发器与选项的包围盒和字号等于改动前的字面值`, async ({ page }) => {
      await openSheetWithControls(page, DESKTOP, kind, installMultipleIssueTimes)
      expect(isMobileForm(requireViewport(page))).toBe(false)
      const parts = sheetControlParts(page, kind)

      const close = await measureControl(parts.close, '关闭按钮')
      expectNear(close.box.width, MASTER_1280x900.closeSize, '关闭按钮宽')
      expectNear(close.box.height, MASTER_1280x900.closeSize, '关闭按钮高')

      const trigger = await measureControl(parts.trigger, '起报时次触发器')
      expectNear(trigger.box.height, MASTER_1280x900.triggerHeight, '触发器高')
      expect(trigger.fontSizePx, '触发器字号').toBe(MASTER_1280x900.controlFontPx)
      if (kind === 'river') expectNear(trigger.box.width, MASTER_1280x900.riverTriggerWidth, '河段触发器宽')

      // 河段的起报时次条自带内边距与边线；气象代站的那一条包在工具条里，量工具条。
      expectNear(await heightOf(page, kind === 'river' ? ISSUE_TIME_BAR.river : STATION_TOOLBAR), MASTER_1280x900.barHeight, '起报时次条 / 工具条高')

      await parts.trigger.click()
      await expect(parts.options).toHaveCount(2)
      const options = await measureControls(parts.options)
      expect(options).toHaveLength(2)
      for (const option of options) {
        expectNear(option.box.height, MASTER_1280x900.optionHeight, `选项「${option.name}」高`)
        expect(option.fontSizePx, `选项「${option.name}」字号`).toBe(MASTER_1280x900.optionFontPx)
      }
    })
  }

  test('(i) 1280x900 气象代站窗：要素切换项高 28、字号 11px、没有最小宽度，与起报时次条同一行；已加载容器上内边距 10px', async ({ page }) => {
    await openSheetWithControls(page, DESKTOP, 'station')
    expect(isMobileForm(requireViewport(page))).toBe(false)
    const frame = page.getByTestId(CURVE_WINDOWS.station.testId)

    const toggles = await measureControls(frame.getByTestId(STATION_VARIABLE_SELECTOR).getByRole('tab'))
    expect(toggles.map((toggle) => toggle.name)).toEqual([...STATION_VARIABLES])
    for (const toggle of toggles) {
      expectNear(toggle.box.height, MASTER_1280x900.toggleHeight, `切换项「${toggle.name}」高`)
      expect(toggle.fontSizePx, `切换项「${toggle.name}」字号`).toBe(MASTER_1280x900.controlFontPx)
      expect(toggle.minWidth, `切换项「${toggle.name}」计算 min-width`).toBe('auto')
    }
    // 短标签没有被撑到触控下限（改动前实测 RH 37.88、Rn 36.09）。
    for (const name of ['RH', 'Rn']) {
      expect(toggles.find((toggle) => toggle.name === name)!.box.width, `切换项「${name}」宽`).toBeLessThan(MIN_TOUCH_TARGET_PX)
    }
    // 同一行：切换项与起报时次条的顶边相同（改动前实测都在 y=260）。
    const issueTimeBar = await boxOf(sheetControlParts(page, 'station').issueTimeBar, '起报时次条')
    for (const toggle of toggles) expectNear(toggle.box.y, issueTimeBar.y, `切换项「${toggle.name}」顶边`)

    expect(await frame.getByTestId(STATION_LOADED).evaluate((element) => getComputedStyle(element).paddingTop)).toBe(MASTER_1280x900.stationLoadedPaddingTop)
  })

  // openspec issue-time-trigger-truncate（#2870）的桌面形态：长标签被截断而不是折成两行越出触发器。
  test('(j) 1280x800 河段窗：保留时次的长标签不出框，触发器高仍为 28，title 为完整标签', async ({ page }) => {
    await openSheetWithControls(page, DESKTOP_1280x800, 'river', installRetainedIssueTime)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    const parts = sheetControlParts(page, 'river')

    const latest = (await parts.trigger.innerText()).trim()
    await parts.trigger.click()
    await expect(parts.options).toHaveCount(2)
    const earlier = (await parts.options.allInnerTexts()).map((text) => text.trim()).find((text) => text !== latest)
    expect(earlier, `应有一个不同于默认项「${latest}」的时次`).toMatch(/^\d{2}-\d{2} \d{2}:\d{2} UTC$/)
    await parts.options.filter({ hasText: earlier! }).click()
    await expect(parts.listbox).toHaveCount(0)
    // 选中保留时次后窗进入空态、没有图表 canvas：同步点是触发器文本里的后缀。
    await expect(parts.trigger).toContainText(RETENTION_UNAVAILABLE_SUFFIX.trim())
    const fullLabel = `${earlier}${RETENTION_UNAVAILABLE_SUFFIX}`

    const trigger = await measureIssueTimeTrigger(parts.trigger)
    console.log('sheet-controls-desktop (j)', JSON.stringify(trigger))
    expect(trigger.value.text, '触发器显示的是保留时次的长标签').toBe(fullLabel)
    expectNear(trigger.box.height, MASTER_1280x900.triggerHeight, '触发器高')
    expect(trigger.scrollHeight, `触发器 scrollHeight ${trigger.scrollHeight} 应 ≤ clientHeight ${trigger.clientHeight}`).toBeLessThanOrEqual(trigger.clientHeight)
    expect(trigger.scrollWidth, `触发器 scrollWidth ${trigger.scrollWidth} 应 ≤ clientWidth ${trigger.clientWidth}`).toBeLessThanOrEqual(trigger.clientWidth)
    expect(trigger.title, '触发器 title').toBe(fullLabel)
  })
})
