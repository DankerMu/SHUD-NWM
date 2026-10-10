import { expect, test, type Locator, type Page } from '@playwright/test'

import { mockMinimalDischargeCatalogApi } from './support/layerCatalog.mocked'

/**
 * 外壳动态视口（openspec mobile-responsive-display task 2.2 / design.md D3）。
 * 桌面 project 下用 setViewportSize 走各视口，核对外壳根节点的两个事实：
 * 高度声明写的是 `100dvh`（读规则文本——模拟器里 dvh == vh，计算值区分不出来），以及根节点恰好占满视口。
 */

type BoxCase = { path: string; width: number; height: number }

// 视口逐条抄自 tasks.md 的 Triage（#2791）与 specs/mobile-viewport-shell/spec.md。
const boxCases: BoxCase[] = [
  { path: '/', width: 1280, height: 900 },
  { path: '/', width: 768, height: 1024 },
  { path: '/', width: 390, height: 664 },
  { path: '/', width: 750, height: 342 },
  { path: '/ops', width: 1280, height: 900 },
]

function shellRoot(page: Page): Locator {
  return page.locator('[data-viewport-form]')
}

test.describe('M11 外壳动态视口', () => {
  test.beforeEach(async ({ page }) => {
    await mockMinimalDischargeCatalogApi(page)
  })

  test('390×664 根节点的高度声明是 100dvh（读匹配的样式规则）', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 664 })
    await page.goto('/')

    const root = shellRoot(page)
    await expect(root).toHaveCount(1)

    // Tailwind v4 的工具类在 `@layer utilities` 里，只遍历顶层找不到，所以递归进入分组规则。
    const heights = await root.evaluate((element) => {
      const found: string[] = []
      const walk = (rules: CSSRuleList) => {
        for (const rule of Array.from(rules)) {
          if (rule instanceof CSSStyleRule) {
            if (element.matches(rule.selectorText) && rule.style.height !== '') found.push(rule.style.height)
          }
          // CSSStyleRule 也可能带嵌套规则，所以不用 else。
          const nested = (rule as CSSRule & { cssRules?: CSSRuleList }).cssRules
          if (nested) walk(nested)
        }
      }
      for (const sheet of Array.from(document.styleSheets)) walk(sheet.cssRules)
      return found
    })

    expect(heights).toEqual(['100dvh'])
  })

  for (const { path, width, height } of boxCases) {
    test(`${path} @ ${width}×${height} 根节点包围盒等于视口（±1px）`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await page.goto(path)

      const root = shellRoot(page)
      await expect(root).toHaveCount(1)

      const measured = await root.evaluate((element) => {
        const box = element.getBoundingClientRect()
        return { width: box.width, height: box.height, innerWidth: window.innerWidth, innerHeight: window.innerHeight }
      })

      // setViewportSize 的入参是独立于页面的期望值；innerWidth / innerHeight 是 Triage 指定的比较基准。
      expect(measured.innerWidth).toBe(width)
      expect(measured.innerHeight).toBe(height)
      expect(Math.abs(measured.width - measured.innerWidth)).toBeLessThanOrEqual(1)
      expect(Math.abs(measured.height - measured.innerHeight)).toBeLessThanOrEqual(1)
    })
  }
})
