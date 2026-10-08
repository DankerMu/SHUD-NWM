import { expect, test, type Locator, type Page } from '@playwright/test'

/**
 * 外壳安全区（openspec mobile-responsive-display task 2.3 / design.md D3）。
 * 桌面 project 下用 setViewportSize 走各视口，核对三件事：viewport meta 含 `viewport-fit=cover`；
 * 外壳根节点四边内边距的声明各引用对应方向的 `env(safe-area-inset-*)`（读规则文本——模拟器里 `env()` 为 0，
 * 计算值区分不出来）；inset 为 0 时四边计算内边距为 0、根节点仍恰好占满视口。
 */

const sides = ['top', 'right', 'bottom', 'left'] as const

type BoxCase = { path: string; width: number; height: number }

// 视口逐条抄自 tasks.md 的 Triage（#2792）。
const boxCases: BoxCase[] = [
  { path: '/', width: 1280, height: 900 },
  { path: '/', width: 390, height: 664 },
  { path: '/ops', width: 1280, height: 900 },
]

function shellRoot(page: Page): Locator {
  return page.locator('[data-viewport-form]')
}

test.describe('M11 外壳安全区', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname === '/api/v1/layers') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'ok',
            data: [
              {
                layer_id: 'discharge',
                layer_name: 'Discharge',
                layer_type: 'hydrology',
                variables: ['q_down'],
                metadata: { layer_id: 'discharge', valid_times: [] },
              },
            ],
          }),
        })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data: [] }) })
    })
  })

  test('390×664 viewport meta 保留既有两项并含 viewport-fit=cover', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 664 })
    await page.goto('/')

    const meta = page.locator('meta[name="viewport"]')
    await expect(meta).toHaveCount(1)
    const content = (await meta.getAttribute('content')) ?? ''

    expect(content).toContain('width=device-width')
    expect(content).toContain('initial-scale=1')
    expect(content).toContain('viewport-fit=cover')
  })

  for (const side of sides) {
    test(`390×664 根节点 padding-${side} 的声明引用 env(safe-area-inset-${side})（读匹配的样式规则）`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 664 })
      await page.goto('/')

      const root = shellRoot(page)
      await expect(root).toHaveCount(1)

      // Tailwind v4 的工具类在 `@layer utilities` 里，只遍历顶层找不到，所以递归进入分组规则。
      // 每条命中的声明连同它的祖先分组规则链（规则类名，由外到内）一起记下来，用来判它是不是无条件生效。
      const declarations = await root.evaluate((element, property) => {
        const found: { value: string; ancestors: string[] }[] = []
        // `selector` 是最近的祖先样式规则展开后的选择器；顶层为 null。
        const walk = (rules: CSSRuleList, ancestors: string[], selector: string | null) => {
          for (const rule of Array.from(rules)) {
            const kind = rule.constructor.name
            let own = selector
            if (rule instanceof CSSStyleRule) {
              // 嵌套规则的 `&` 指父选择器；`matches('&')` 在嵌套之外恒真，所以先展开。
              own = selector === null ? rule.selectorText : rule.selectorText.replaceAll('&', `:is(${selector})`)
            }
            // 除样式规则外，`.mobile\:x { @media (...) { 声明 } }` 这类嵌套写法里的声明落在 CSSNestedDeclarations 上，
            // 它没有选择器，沿用父样式规则的。
            const style = rule instanceof CSSStyleRule || kind === 'CSSNestedDeclarations' ? (rule as CSSStyleRule).style : null
            if (style && own !== null) {
              const value = style.getPropertyValue(property)
              if (value !== '' && element.matches(own)) found.push({ value, ancestors })
            }
            // CSSStyleRule 也可能带嵌套规则，所以不用 else。
            const nested = (rule as CSSRule & { cssRules?: CSSRuleList }).cssRules
            if (nested) walk(nested, [...ancestors, kind], own)
          }
        }
        for (const sheet of Array.from(document.styleSheets)) walk(sheet.cssRules, [], null)
        return found
      }, `padding-${side}`)

      // preflight 对 `*` 声明了 `padding: 0`，同样命中根节点，所以只数引用了对应 env() 的声明，不数全部声明。
      const safeArea = declarations.filter(({ value }) => value.includes(`env(safe-area-inset-${side})`))
      expect(safeArea).toHaveLength(1)

      // 安全区内边距在两种形态下都要生效：声明不得挂在条件分组规则下（`mobile:` 变体会把它放进 @media）。
      // `@layer`（CSSLayerBlockRule）不是条件，放行。
      const conditional = ['CSSMediaRule', 'CSSContainerRule', 'CSSSupportsRule']
      expect(safeArea[0].ancestors.filter((kind) => conditional.includes(kind))).toEqual([])
    })
  }

  for (const { path, width, height } of boxCases) {
    test(`${path} @ ${width}×${height} 四边计算内边距为 0px 且根节点包围盒等于视口（±1px）`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await page.goto(path)

      const root = shellRoot(page)
      await expect(root).toHaveCount(1)

      const measured = await root.evaluate((element) => {
        const box = element.getBoundingClientRect()
        const style = getComputedStyle(element)
        return {
          padding: [style.paddingTop, style.paddingRight, style.paddingBottom, style.paddingLeft],
          width: box.width,
          height: box.height,
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight,
        }
      })

      expect(measured.padding).toEqual(['0px', '0px', '0px', '0px'])
      // setViewportSize 的入参是独立于页面的期望值；innerWidth / innerHeight 是 Triage 指定的比较基准。
      expect(measured.innerWidth).toBe(width)
      expect(measured.innerHeight).toBe(height)
      expect(Math.abs(measured.width - measured.innerWidth)).toBeLessThanOrEqual(1)
      expect(Math.abs(measured.height - measured.innerHeight)).toBeLessThanOrEqual(1)
    })
  }
})
