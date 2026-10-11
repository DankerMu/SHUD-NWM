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
 * 高度必须与改动前相同，这些字面值量自 6.1 改动前的 `src/`（`origin/master` e031e4bd0，Chromium）。
 * 表单控件的字号不在此列：#2861 删掉 `src/index.css` 层外的 `font: inherit` 之后，字号由控件自己的类决定，
 * 下面钉的是类写出来的值。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }

const MASTER: Array<{ viewport: ViewportSize; mainGridColumns: number; summaryGridColumns: number }> = [
  { viewport: DESKTOP, mainGridColumns: 3, summaryGridColumns: 3 },
  // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态；宽不到 800 / 900，两个网格本来就是单列。
  { viewport: { width: 768, height: 1024 }, mainGridColumns: 1, summaryGridColumns: 1 },
]
/** 五个表单控件与角色切换器触发器：字号来自各自的 `text-sm`（14px），高度来自 `h-[var(--control-height-lg)]` / `h-10`。 */
const MASTER_CONTROL = { fontSizePx: 14, height: 40 }
/** 注入的祖先字号：与 `text-sm` 不同，控件若仍在继承祖先（层外的 `font: inherit` 回来了）就会量到它。 */
const INJECTED_FONT_PX = 12
/** `/` 曲线窗起报时次触发器的桌面字号（触发器自己的 `text-[11px]`，与 4.9 的桌面 spec 同值）。 */
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

    // 字号工具类在表单控件上生效（openspec layer-font-inherit-reset design D4，#2861）：不带 `!` 的 `text-sm`
    // 胜过祖先的字号。在 `src/index.css` 里恢复层外的 `button, input, select, textarea { font: inherit }` 会让它转红。
    test(`${target.route} @ 1280x900：祖先带 12px 字号时五个表单控件与角色切换器触发器仍是自己类名的 14px`, async ({ page }) => {
      await page.setViewportSize(DESKTOP)
      await openOpsFallbackPage(page, target)
      expect(isMobileForm(requireViewport(page))).toBe(false)
      await page.addStyleTag({ content: `main { font-size: ${INJECTED_FONT_PX}px; }` })
      await expect.poll(() => page.locator('main').evaluate((main) => getComputedStyle(main).fontSize)).toBe(`${INJECTED_FONT_PX}px`)

      await expect
        .poll(async () => (await measureNamedFormControls(page)).map(({ name, fontSizePx }) => `${name} ${fontSizePx}px`))
        .toEqual(FORM_CONTROL_LABELS.map((label) => `${label} ${MASTER_CONTROL.fontSizePx}px`))
      const role = await measureFormControls(page.locator(ROLE_TRIGGER_SELECTOR))
      expect(role.map(({ fontSizePx }) => fontSizePx), '角色切换器触发器字号').toEqual([MASTER_CONTROL.fontSizePx])
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
