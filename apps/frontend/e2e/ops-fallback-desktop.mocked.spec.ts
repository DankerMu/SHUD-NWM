import { expect, test } from '@playwright/test'

import { installMultipleIssueTimes } from './support/curveSheet.mocked'
import {
  FORM_CONTROL_LABELS,
  LONG_LOG,
  OPS_FALLBACK_TARGETS,
  ROLE_TRIGGER_SELECTOR,
  measureCheckedOpsFallbackPage,
  measureFormControls,
  measureLogDialog,
  measureNamedFormControls,
  measureOpsFallbackPage,
  openLogDialog,
  openOpsFallbackPage,
} from './support/opsFallback.mocked'
import { measureControl, openSheetWithControls, sheetControlParts } from './support/sheetControls.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 运维页与共享选择器的桌面不变量（openspec mobile-responsive-display task 6.1 的 Risk pack「Legacy compatibility」）。
 *
 * 6.1 的产品改动全部是带移动前缀的类；桌面形态（含 768×1024 的平板）下两个路由的布局与表单控件的
 * 计算样式必须与改动前相同。下面的字面值量自改动前的 `src/`（`origin/master` e031e4bd0，Chromium），
 * 改动前后都绿。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }

const MASTER: Array<{ viewport: ViewportSize; mainGridColumns: number; summaryGridColumns: number }> = [
  { viewport: DESKTOP, mainGridColumns: 3, summaryGridColumns: 3 },
  // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态；宽不到 800 / 900，两个网格本来就是单列。
  { viewport: { width: 768, height: 1024 }, mainGridColumns: 1, summaryGridColumns: 1 },
]
/** 五个表单控件与角色切换器触发器：字号靠继承取浏览器默认值，高度来自 `h-[var(--control-height-lg)]` / `h-10`。 */
const MASTER_CONTROL = { fontSizePx: 16, height: 40 }
/** 注入的祖先字号：桌面形态下移动字号规则不生效，控件照旧继承它。 */
const INJECTED_FONT_PX = 12
/** `/` 曲线窗起报时次触发器的桌面字号（继承自深色面板，与 4.9 的桌面 spec 同值）。 */
const MASTER_ISSUE_TIME_TRIGGER_FONT_PX = 11
/** 1280×900 下日志弹窗与日志区的高度上限：88vh 与 65vh。 */
const MASTER_LOG_DIALOG = { dialogMaxHeight: '792px', logMaxHeight: '585px' }

test.describe('运维页与共享选择器的桌面形态不变', () => {
  for (const target of OPS_FALLBACK_TARGETS) {
    for (const { viewport, mainGridColumns, summaryGridColumns } of MASTER) {
      test(`${target.route} @ ${viewport.width}x${viewport.height}：没有页面内边距，网格列数与表单控件字号等于改动前的字面值`, async ({ page }) => {
        await page.setViewportSize(viewport)
        await openOpsFallbackPage(page, target)
        expect(isMobileForm(requireViewport(page))).toBe(false)

        await expect.poll(async () => (await measureOpsFallbackPage(page))?.mainGridColumns ?? null, { message: '主网格列数' }).toBe(mainGridColumns)
        const measure = await measureCheckedOpsFallbackPage(page)
        expect(measure.scroller.paddingLeft, '页面滚动容器左内边距').toBe(0)
        expect(measure.scroller.paddingRight, '页面滚动容器右内边距').toBe(0)
        expect(measure.summaryGridColumns, '概要区外层网格列数').toBe(summaryGridColumns)

        const controls = await measureNamedFormControls(page)
        expect(controls).toEqual(FORM_CONTROL_LABELS.map((name) => ({ name, ...MASTER_CONTROL })))
        const role = await measureFormControls(page.locator(ROLE_TRIGGER_SELECTOR))
        expect(role.map(({ fontSizePx, height }) => ({ fontSizePx, height })), '角色切换器触发器').toEqual([MASTER_CONTROL])
      })
    }

    test(`${target.route} @ 1280x900：祖先带 12px 字号时五个表单控件照旧继承 12px`, async ({ page }) => {
      await page.setViewportSize(DESKTOP)
      await openOpsFallbackPage(page, target)
      expect(isMobileForm(requireViewport(page))).toBe(false)
      await page.addStyleTag({ content: `main { font-size: ${INJECTED_FONT_PX}px; }` })
      await expect.poll(() => page.locator('main').evaluate((main) => getComputedStyle(main).fontSize)).toBe(`${INJECTED_FONT_PX}px`)

      await expect
        .poll(async () => (await measureNamedFormControls(page)).map(({ name, fontSizePx }) => `${name} ${fontSizePx}px`))
        .toEqual(FORM_CONTROL_LABELS.map((label) => `${label} ${INJECTED_FONT_PX}px`))
    })

    test(`${target.route} @ 1280x900：日志弹窗与日志区的高度上限等于改动前的 88vh / 65vh`, async ({ page }) => {
      await page.setViewportSize(DESKTOP)
      await openOpsFallbackPage(page, target, { logContent: LONG_LOG })
      await openLogDialog(page, target)

      await expect
        .poll(async () => {
          const measure = await measureLogDialog(page)
          return measure ? { dialogMaxHeight: measure.dialogMaxHeight, logMaxHeight: measure.logMaxHeight } : null
        })
        .toEqual(MASTER_LOG_DIALOG)
    })
  }

  test('/ @ 1280x900：河段窗起报时次触发器字号等于改动前的字面值', async ({ page }) => {
    await openSheetWithControls(page, DESKTOP, 'river', installMultipleIssueTimes)
    expect(isMobileForm(requireViewport(page))).toBe(false)

    const trigger = await measureControl(sheetControlParts(page, 'river').trigger, '起报时次触发器')
    expect(trigger.fontSizePx, '起报时次触发器字号').toBe(MASTER_ISSUE_TIME_TRIGGER_FONT_PX)
  })
})
