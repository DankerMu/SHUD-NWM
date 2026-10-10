import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  RETENTION_UNAVAILABLE_SUFFIX,
  expectSheet,
  installRetainedIssueTime,
  intersectionOf,
  type CurveWindowKind,
} from './support/curveSheet.mocked'
import { contains, type Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import {
  MIN_FORM_FONT_PX,
  MIN_TOUCH_TARGET_PX,
  measureIssueTimeTrigger,
  openSheetWithControls,
  sheetControlParts,
  withinHorizontally,
  type IssueTimeTriggerMeasure,
} from './support/sheetControls.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 起报时次触发器里的文字单行、不出框（openspec issue-time-trigger-truncate，#2870）。
 *
 * (a) 「保留时次」长标签被省略号截断而不是折行出框；(b) 窄抽屉里河段窗起报条仍是单行，让位的是
 * 说明文字而不是触发器；(c) 放得下的宽度上说明文字仍在。每条用例在用例内 `setViewportSize`，所以三个
 * 移动 project 下执行的是同一组断言。判据取自包围盒、`scrollWidth` / `clientWidth` 与计算样式；
 * 文本宽度用同字体的离屏量具量，不写像素字面值。桌面形态的一条在 `m11-sheet-controls-desktop.mocked.spec.ts`。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
/** 河段窗起报条放不下「起报 + 触发器 + 说明文字」的两个视口：右侧抽屉宽 284px、底部抽屉宽 320px。 */
const NARROW_SHEETS: ViewportSize[] = [
  { width: 568, height: 320 },
  { width: 320, height: 568 },
]
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const CAPTION_TEXT = 'GFS + IFS 同步切换'
const ISSUE_TIME_LABEL = /^\d{2}-\d{2} \d{2}:\d{2} UTC$/
const TOLERANCE_PX = 0.5

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function report(name: string, where: string, measure: unknown) {
  console.log(`issue-time-trigger ${name} @ ${where}`, JSON.stringify(measure))
}

/** 触发器里的文字没有出框，值是单行。 */
function expectValueInsideTrigger(trigger: IssueTimeTriggerMeasure, where: string) {
  expect(trigger.scrollHeight, `触发器 scrollHeight ${trigger.scrollHeight} 应 ≤ clientHeight ${trigger.clientHeight} @ ${where}`).toBeLessThanOrEqual(trigger.clientHeight)
  expect(trigger.scrollWidth, `触发器 scrollWidth ${trigger.scrollWidth} 应 ≤ clientWidth ${trigger.clientWidth} @ ${where}`).toBeLessThanOrEqual(trigger.clientWidth)
  expect(trigger.value.box.height, `值元素高 ${trigger.value.box.height} 应为单行（≤ 触发器 clientHeight ${trigger.clientHeight}）@ ${where}`).toBeLessThanOrEqual(trigger.clientHeight)
}

interface BarMeasure {
  bar: Box
  /** 触发器高 + 条的计算上下内边距 + 计算下边线宽。 */
  expectedHeight: number
  /** 条的直接子项里可见的那些：有布局盒、没有 `visibility: hidden`、与条的包围盒相交。 */
  visibleChildren: Array<{ name: string; box: Box }>
  /** 说明文字；不渲染（没有节点、`display: none` 或零面积）为 null。`lines` 是文本实际排出的行数。 */
  caption: { box: Box; lines: number } | null
}

async function measureCycleBar(bar: Locator, trigger: Locator): Promise<BarMeasure> {
  const triggerHeight = (await boxOf(trigger, '起报时次触发器')).height
  return bar.evaluate(
    (element, { captionText, triggerHeight }) => {
      const box = (target: Element | Range) => {
        const rect = target.getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      }
      const overlaps = (a: DOMRect, b: DOMRect) => Math.min(a.right, b.right) > Math.max(a.left, b.left) && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top)
      const barRect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      const visibleChildren = [...element.children].flatMap((child) => {
        const rect = child.getBoundingClientRect()
        if (rect.width === 0 || rect.height === 0 || getComputedStyle(child).visibility === 'hidden' || !overlaps(rect, barRect)) return []
        return [{ name: (child.getAttribute('aria-label') ?? child.textContent ?? '').trim() || child.tagName.toLowerCase(), box: box(child) }]
      })
      const captionElement = [...element.querySelectorAll('span')].find((span) => span.children.length === 0 && span.textContent?.trim() === captionText)
      let caption: { box: ReturnType<typeof box>; lines: number } | null = null
      if (captionElement) {
        const range = document.createRange()
        range.selectNodeContents(captionElement)
        const rect = range.getBoundingClientRect()
        if (rect.width > 0 && rect.height > 0) {
          const lineTops = new Set([...range.getClientRects()].map((line) => Math.round(line.top)))
          caption = { box: box(range), lines: lineTops.size }
        }
      }
      return {
        bar: box(element),
        expectedHeight: triggerHeight + Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom) + Number.parseFloat(style.borderBottomWidth),
        visibleChildren,
        caption,
      }
    },
    { captionText: CAPTION_TEXT, triggerHeight },
  )
}

/** 说明文字可见、单行、整个在条内，与触发器不相交。 */
function expectCaptionShown(measure: BarMeasure, trigger: Box, where: string) {
  expect(measure.caption, `说明文字应渲染 @ ${where}`).not.toBeNull()
  const caption = measure.caption!
  expect(caption.lines, `说明文字应为单行 @ ${where}`).toBe(1)
  expect(contains(measure.bar, caption.box), `说明文字 ${JSON.stringify(caption.box)} 应在起报条 ${JSON.stringify(measure.bar)} 内 @ ${where}`).toBe(true)
  expect(intersectionOf(caption.box, trigger), `说明文字不应与触发器相交 @ ${where}`).toBeNull()
}

/** 经真实交互进入「保留时次」状态：在下拉里选中更早时次，等触发器出现后缀。返回完整长标签。 */
async function selectRetainedIssueTime(page: Page, kind: CurveWindowKind): Promise<string> {
  const parts = sheetControlParts(page, kind)
  const latest = (await parts.trigger.innerText()).trim()
  await expect(parts.trigger).toBeEnabled()
  await parts.trigger.tap()
  await expect(parts.options).toHaveCount(2)
  const earlier = (await parts.options.allInnerTexts()).map((text) => text.trim()).find((text) => text !== latest)
  expect(earlier, `应有一个不同于默认项「${latest}」的时次`).toMatch(ISSUE_TIME_LABEL)
  await parts.options.filter({ hasText: earlier! }).tap()
  await expect(parts.listbox).toHaveCount(0)
  // 选中保留时次后窗进入空态、没有图表 canvas：同步点是触发器文本里的后缀。
  await expect(parts.trigger).toContainText(RETENTION_UNAVAILABLE_SUFFIX.trim())
  return `${earlier}${RETENTION_UNAVAILABLE_SUFFIX}`
}

test.describe('M11 起报时次触发器文字不出框', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const kind of CURVE_WINDOW_KINDS) {
    test(`(a) 390x664 ${CURVE_WINDOWS[kind].name}：保留时次的长标签单行截断、不出框，日期时间部分完整，title 为完整标签`, async ({ page }, testInfo) => {
      await openSheetWithControls(page, PORTRAIT, kind, installRetainedIssueTime)
      const where = label(page, testInfo)
      const sheet = await expectSheet(page, kind, where)
      const parts = sheetControlParts(page, kind)
      const fullLabel = await selectRetainedIssueTime(page, kind)

      const trigger = await measureIssueTimeTrigger(parts.trigger)
      report(`(a) ${kind} trigger`, where, { sheet: sheet.frame, trigger })
      expect(trigger.value.text, '触发器显示的是保留时次的长标签').toBe(fullLabel)
      expect(Math.abs(trigger.box.height - MIN_TOUCH_TARGET_PX), `触发器高 ${trigger.box.height} 应为 ${MIN_TOUCH_TARGET_PX}`).toBeLessThanOrEqual(TOLERANCE_PX)
      expect(trigger.fontSizePx, '触发器字号').toBeGreaterThanOrEqual(MIN_FORM_FONT_PX)
      expect(withinHorizontally(sheet.frame, trigger.box), `触发器 ${JSON.stringify(trigger.box)} 应在抽屉水平范围内`).toBe(true)
      expectValueInsideTrigger(trigger, where)
      expect(trigger.value.textOverflow, '值元素的 text-overflow').toBe('ellipsis')
      expect(
        trigger.value.clientWidth,
        `值元素宽 ${trigger.value.clientWidth} 应放得下日期时间部分加省略号（${trigger.dateTimeWithEllipsisWidth}）`,
      ).toBeGreaterThanOrEqual(trigger.dateTimeWithEllipsisWidth)
      expect(trigger.title, '触发器 title').toBe(fullLabel)

      if (kind === 'river') {
        const bar = await measureCycleBar(parts.issueTimeBar, parts.trigger)
        report('(a) river bar', where, bar)
        expectCaptionShown(bar, trigger.box, where)
      }
    })
  }

  for (const viewport of NARROW_SHEETS) {
    test(`(b) ${viewport.width}x${viewport.height} 河段窗：起报条单行，普通时次完整显示，说明文字不折行（放不下则不显示）`, async ({ page }, testInfo) => {
      await openSheetWithControls(page, viewport, 'river')
      const where = label(page, testInfo)
      const sheet = await expectSheet(page, 'river', where)
      const parts = sheetControlParts(page, 'river')

      const trigger = await measureIssueTimeTrigger(parts.trigger)
      const bar = await measureCycleBar(parts.issueTimeBar, parts.trigger)
      report('(b)', where, { sheet: sheet.frame, trigger, bar })
      expect(trigger.value.text, '触发器显示普通时次').toMatch(ISSUE_TIME_LABEL)

      // 选择器没匹配到任何东西时不让循环空转：「起报」标签与触发器总在。
      expect(bar.visibleChildren.length, '起报条的可见子项数').toBeGreaterThanOrEqual(2)
      for (const child of bar.visibleChildren) {
        expect(child.box.height, `起报条子项「${child.name}」高 ${child.box.height} 应 ≤ 触发器高 ${trigger.box.height} @ ${where}`).toBeLessThanOrEqual(trigger.box.height + TOLERANCE_PX)
      }
      // 说明文字：不渲染，或被裁到条外（与条的包围盒不相交），或单行、在抽屉水平范围内、与触发器不相交。
      if (bar.caption && intersectionOf(bar.caption.box, bar.bar)) {
        expectCaptionShown(bar, trigger.box, where)
        expect(withinHorizontally(sheet.frame, bar.caption.box), `说明文字 ${JSON.stringify(bar.caption.box)} 应在抽屉水平范围内 @ ${where}`).toBe(true)
      }

      expect(withinHorizontally(sheet.frame, trigger.box), `触发器 ${JSON.stringify(trigger.box)} 应在抽屉水平范围内 @ ${where}`).toBe(true)
      expect(contains(bar.bar, trigger.box), `触发器 ${JSON.stringify(trigger.box)} 应在起报条 ${JSON.stringify(bar.bar)} 内 @ ${where}`).toBe(true)
      expectValueInsideTrigger(trigger, where)
      expect(trigger.value.scrollWidth, `普通时次不应被省略（值元素 scrollWidth ${trigger.value.scrollWidth} ≤ clientWidth ${trigger.value.clientWidth}）@ ${where}`).toBeLessThanOrEqual(trigger.value.clientWidth)
      expect(trigger.title, '触发器 title 为普通标签').toBe(trigger.value.text)
      // 守卫：条高不随内容变化。
      expect(Math.abs(bar.bar.height - bar.expectedHeight), `起报条高 ${bar.bar.height} 应为 ${bar.expectedHeight} @ ${where}`).toBeLessThanOrEqual(TOLERANCE_PX)
    })
  }

  test('(c) 750x342 河段窗：放得下时说明文字可见且单行', async ({ page }, testInfo) => {
    await openSheetWithControls(page, LANDSCAPE, 'river')
    const where = label(page, testInfo)
    await expectSheet(page, 'river', where)
    const parts = sheetControlParts(page, 'river')

    const trigger = await measureIssueTimeTrigger(parts.trigger)
    const bar = await measureCycleBar(parts.issueTimeBar, parts.trigger)
    report('(c)', where, { trigger, bar })
    expect(trigger.value.text, '触发器显示普通时次').toMatch(ISSUE_TIME_LABEL)
    expectCaptionShown(bar, trigger.box, where)
  })
})
