import { expect, test, type Page } from '@playwright/test'

import {
  FORM_CONTROL_LABELS,
  FORM_CONTROL_SELECTOR,
  LONG_LOG,
  OPS_FALLBACK_TARGETS,
  ROLE_TRIGGER_SELECTOR,
  logDialogViolations,
  measureCheckedOpsFallbackPage,
  measureFormControls,
  measureLogDialog,
  measureNamedFormControls,
  measureOpsFallbackPage,
  openLogDialog,
  openOpsFallbackPage,
  outsidePagePadding,
  scrollOpsFallbackPageToEnd,
  visitOpsFallbackPage,
} from './support/opsFallback.mocked'
import { MIN_FORM_FONT_PX } from './support/sheetControls.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 运维页的移动形态兜底（openspec mobile-responsive-display task 6.1，design.md D14）。
 * `/ops` 与 `/monitoring` 是同一个页面组件的两种模式，每条用例对两个路由各跑一遍；
 * 三个移动 project 下都要通过、不按 project 跳过。读数一律轮询（趋势图是懒加载的，卡片高度会晚一拍定下来）。
 */

/** 页面滚动容器的左右内边距下限（px）。 */
const MIN_PAGE_PADDING = 12
const TOLERANCE = 0.5
/** 宽到足以触发概要区 `min-[900px]` 三列、又靠高度进入移动形态的矮视口。 */
const WIDE_SHORT_VIEWPORT = { width: 932, height: 430 }
const PHONE_VIEWPORT = { width: 390, height: 664 }
const SMALL_PHONE_VIEWPORT = { width: 320, height: 568 }

/** 内边距与单列：卡片与页面容器的直接子元素都在内边距内、没有并排卡片、两个网格都是单列。 */
async function expectPaddedSingleColumn(page: Page) {
  await expect
    .poll(async () => {
      const measure = await measureOpsFallbackPage(page)
      if (!measure) return ['页面还没有就绪']
      return [...outsidePagePadding(measure, MIN_PAGE_PADDING), ...measure.sideBySideCards.map((pair) => `并排卡片：${pair}`)]
    })
    .toEqual([])

  const measure = await measureCheckedOpsFallbackPage(page)
  expect(measure.scroller.paddingLeft, '页面滚动容器左内边距').toBeGreaterThanOrEqual(MIN_PAGE_PADDING)
  expect(measure.scroller.paddingRight, '页面滚动容器右内边距').toBeGreaterThanOrEqual(MIN_PAGE_PADDING)
  expect(outsidePagePadding(measure, MIN_PAGE_PADDING)).toEqual([])
  expect(measure.sideBySideCards, '并排卡片').toEqual([])
  // 计算样式层面的证据：移动单列类压过了 `min-[800px]` / `min-[900px]` / `min-[1200px]` 的多列，且没有隐式列。
  expect(measure.mainGridColumns, '主网格列数').toBe(1)
  expect(measure.summaryGridColumns, '概要区外层网格列数').toBe(1)
}

/** 页面滚动容器自身不横向溢出。 */
async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(async () => {
      const measure = await measureOpsFallbackPage(page)
      return measure ? measure.scroller.scrollWidth - measure.scroller.clientWidth : null
    }, { message: '页面滚动容器 scrollWidth − clientWidth' })
    .toBe(0)
  await measureCheckedOpsFallbackPage(page)
}

async function expectLogDialogFits(page: Page) {
  await expect
    .poll(async () => {
      const measure = await measureLogDialog(page)
      return measure ? logDialogViolations(measure) : ['日志弹窗还没有就绪']
    })
    .toEqual([])
  const measure = await measureLogDialog(page)
  expect(measure).not.toBeNull()
  // 滚动者是日志区自己，不是弹窗。
  expect(measure!.logScroll.overflowX, '日志区 overflow-x').toBe('auto')
  expect(measure!.logScroll.overflowY, '日志区 overflow-y').toBe('auto')
}

test.describe('运维页移动形态兜底', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const target of OPS_FALLBACK_TARGETS) {
    test.describe(target.route, () => {
      test('(a) 卡片与页面内容离视口左右边至少 12px，卡片不并排', async ({ page }) => {
        await openOpsFallbackPage(page, target)
        await expectPaddedSingleColumn(page)
      })

      test('(a) 932x430 的宽矮视口下同样单列并带内边距', async ({ page }) => {
        await page.setViewportSize(WIDE_SHORT_VIEWPORT)
        expect(isMobileForm(requireViewport(page))).toBe(true)
        await openOpsFallbackPage(page, target)
        await expectPaddedSingleColumn(page)
      })

      test('(b) 页面滚动容器能纵向滚到底，窗口不滚', async ({ page }) => {
        await openOpsFallbackPage(page, target)
        const before = await measureCheckedOpsFallbackPage(page)
        // 硬前提：内容确实高于容器，且最后一张卡片此刻在视口之下。
        expect(before.scroller.scrollHeight, 'mock 内容必须高于页面滚动容器').toBeGreaterThan(before.scroller.clientHeight)
        const lastBefore = before.cards[before.cards.length - 1].box
        expect(lastBefore.y + lastBefore.height, '滚动前最后一张卡片的底边应在视口之下').toBeGreaterThan(before.viewport.height)

        await scrollOpsFallbackPageToEnd(page)

        await expect
          .poll(async () => {
            const measure = await measureOpsFallbackPage(page)
            if (!measure) return null
            const last = measure.cards[measure.cards.length - 1].box
            return {
              lastCardInside: last.y + last.height <= measure.viewport.height + TOLERANCE,
              atEnd: measure.scroller.scrollHeight - measure.scroller.clientHeight - measure.scroller.scrollTop <= 1,
              windowScrollY: measure.windowScrollY,
              documentScrollTop: measure.documentScrollTop,
            }
          })
          .toEqual({ lastCardInside: true, atEnd: true, windowScrollY: 0, documentScrollTop: 0 })
        const after = await measureCheckedOpsFallbackPage(page)
        expect(after.scroller.scrollTop, '滚的是页面滚动容器自己').toBeGreaterThan(0)
      })

      test('(c) 页面滚动容器不横向溢出；390x664 下任务表在自己的容器里横向滚', async ({ page }) => {
        await openOpsFallbackPage(page, target)
        await expectNoPageOverflow(page)

        // 换视口后重新加载，不在已加载的页面上直接改视口（见 visitOpsFallbackPage 的说明）。
        await page.setViewportSize(PHONE_VIEWPORT)
        await visitOpsFallbackPage(page, target)
        await expectNoPageOverflow(page)
        await expect
          .poll(async () => {
            const measure = await measureOpsFallbackPage(page)
            return measure ? measure.tableContainer.scrollWidth - measure.tableContainer.clientWidth : null
          }, { message: '任务表容器 scrollWidth − clientWidth' })
          .toBeGreaterThan(0)
        const measure = await measureCheckedOpsFallbackPage(page)
        expect(measure.tableContainer.overflowX, '任务表容器 overflow-x').toBe('auto')
        expect(measure.viewport).toEqual(PHONE_VIEWPORT)
      })

      test('(d) 日志弹窗与关闭按钮在视口内，日志区在弹窗内且双向可滚', async ({ page }) => {
        await openOpsFallbackPage(page, target, { logContent: LONG_LOG })
        await openLogDialog(page, target)
        await expectLogDialogFits(page)
      })

      test('(e) 可见的 select、选择器触发器与 input 字号不小于 16px', async ({ page }) => {
        await openOpsFallbackPage(page, target)
        const named = await measureNamedFormControls(page)
        expect(named.map(({ name }) => name)).toEqual([...FORM_CONTROL_LABELS])

        // 遍历式：页面上全部可见的表单控件，只豁免开发用角色切换器。
        const controls = await measureFormControls(page.locator(`:is(${FORM_CONTROL_SELECTOR}):not(${ROLE_TRIGGER_SELECTOR})`))
        expect(controls.map(({ name }) => name).sort(), '可见的表单控件').toEqual([...FORM_CONTROL_LABELS].sort())
        const tooSmall = controls.filter(({ fontSizePx }) => fontSizePx < MIN_FORM_FONT_PX).map(({ name, fontSizePx }) => `${name} ${fontSizePx}px`)
        expect(tooSmall, `字号小于 ${MIN_FORM_FONT_PX}px 的表单控件`).toEqual([])
      })

      test('(e) 祖先带 12px 字号时五个表单控件仍不小于 16px', async ({ page }) => {
        await openOpsFallbackPage(page, target)
        // 表单控件靠 `font: inherit` 取祖先字号；注入小字号祖先才能证明移动字号规则本身在起作用。
        await page.addStyleTag({ content: 'main { font-size: 12px; }' })
        await expect.poll(() => page.locator('main').evaluate((main) => getComputedStyle(main).fontSize)).toBe('12px')

        await expect
          .poll(async () => (await measureNamedFormControls(page)).map(({ name, fontSizePx }) => `${name} ${fontSizePx}px`))
          .toEqual(FORM_CONTROL_LABELS.map((label) => `${label} ${MIN_FORM_FONT_PX}px`))
      })

      test('(f) 320x568 下页面不横向溢出、卡片在内边距内、日志弹窗在视口内', async ({ page }) => {
        await page.setViewportSize(SMALL_PHONE_VIEWPORT)
        await openOpsFallbackPage(page, target, { logContent: LONG_LOG })
        await expectNoPageOverflow(page)
        await expectPaddedSingleColumn(page)

        await openLogDialog(page, target)
        await expectLogDialogFits(page)
      })
    })
  }
})
