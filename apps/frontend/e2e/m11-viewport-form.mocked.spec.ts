import { expect, test, type Locator, type Page } from '@playwright/test'

import { isMobileForm, isShortLandscape } from './support/viewportForm'

/**
 * 移动形态判据（openspec mobile-responsive-display task 2.1 / design.md D1）。
 * 桌面 project 下用 setViewportSize 走各边界视口，核对外壳根节点上的两侧可观测标记：
 * JS 侧（data 属性，来自 useMobileForm）与 CSS 侧（`mobile:` 变体设置的自定义属性的计算值）。
 */

type FormCase = {
  width: number
  height: number
  form: 'mobile' | 'desktop'
  shortLandscape: 'true' | 'false'
}

// 期望值逐条抄自 specs/mobile-viewport-shell/spec.md 的场景，不由助手函数推导。
const formCases: FormCase[] = [
  { width: 767, height: 900, form: 'mobile', shortLandscape: 'false' },
  { width: 768, height: 900, form: 'desktop', shortLandscape: 'false' },
  { width: 900, height: 499, form: 'mobile', shortLandscape: 'true' },
  { width: 900, height: 500, form: 'desktop', shortLandscape: 'false' },
  { width: 390, height: 664, form: 'mobile', shortLandscape: 'false' },
  { width: 320, height: 480, form: 'mobile', shortLandscape: 'false' },
  { width: 600, height: 400, form: 'mobile', shortLandscape: 'true' },
  { width: 1280, height: 900, form: 'desktop', shortLandscape: 'false' },
]

function shellRoot(page: Page): Locator {
  return page.locator('[data-viewport-form]')
}

/** CSS 侧 oracle：真实布局引擎算出的自定义属性值，与 data 属性互相独立。 */
function computedViewportForm(root: Locator): Promise<string> {
  return root.evaluate((element) => getComputedStyle(element).getPropertyValue('--nhms-viewport-form').trim())
}

test.describe('M11 移动形态判据', () => {
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

  for (const { width, height, form, shortLandscape } of formCases) {
    test(`${width}×${height} -> data-viewport-form=${form}, data-viewport-short-landscape=${shortLandscape}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await page.goto('/')

      const root = shellRoot(page)
      await expect(root).toHaveCount(1)
      await expect(root).toHaveAttribute('data-viewport-form', form)
      await expect(root).toHaveAttribute('data-viewport-short-landscape', shortLandscape)

      // 测试侧镜像（e2e/support/viewportForm.ts）须与规格取值一致，否则 `.mobile.` spec 的分支会建在错的信号上。
      expect(isMobileForm({ width, height })).toBe(form === 'mobile')
      expect(isShortLandscape({ width, height })).toBe(shortLandscape === 'true')
    })
  }

  test('390×664 -> 750×342 不刷新即更新矮视口横屏标记', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 664 })
    await page.goto('/')

    const root = shellRoot(page)
    await expect(root).toHaveAttribute('data-viewport-form', 'mobile')
    await expect(root).toHaveAttribute('data-viewport-short-landscape', 'false')

    // 在根节点上打一个只存在于当前文档与当前 DOM 节点的记号：刷新或重挂载都会让它消失。
    await root.evaluate((element) => {
      ;(element as HTMLElement & { __viewportFormProbe?: string }).__viewportFormProbe = 'kept'
    })
    let navigations = 0
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) navigations += 1
    })

    await page.setViewportSize({ width: 750, height: 342 })

    await expect(root).toHaveAttribute('data-viewport-short-landscape', 'true')
    await expect(root).toHaveAttribute('data-viewport-form', 'mobile')
    expect(
      await root.evaluate((element) => (element as HTMLElement & { __viewportFormProbe?: string }).__viewportFormProbe),
    ).toBe('kept')
    expect(navigations).toBe(0)
  })

  test('768×900 -> 767×900 -> 768×900 跨宽度边界不刷新即往返更新', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 900 })
    await page.goto('/')

    const root = shellRoot(page)
    await expect(root).toHaveAttribute('data-viewport-form', 'desktop')
    expect(await computedViewportForm(root)).toBe('desktop')

    await page.setViewportSize({ width: 767, height: 900 })
    await expect(root).toHaveAttribute('data-viewport-form', 'mobile')
    expect(await computedViewportForm(root)).toBe('mobile')

    await page.setViewportSize({ width: 768, height: 900 })
    await expect(root).toHaveAttribute('data-viewport-form', 'desktop')
    expect(await computedViewportForm(root)).toBe('desktop')
  })

  for (const { width, height, form } of [
    // 宽 ≥ 768，只靠高度臂进入移动形态：单行简写的变体会丢掉这一臂。
    { width: 844, height: 390, form: 'mobile' },
    { width: 1280, height: 900, form: 'desktop' },
  ] as const) {
    test(`${width}×${height} CSS 侧计算值与 JS 侧 data 属性一致（${form}）`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await page.goto('/')

      const root = shellRoot(page)
      await expect(root).toHaveAttribute('data-viewport-form', form)

      const computed = await computedViewportForm(root)
      expect(computed).toBe(form)
      expect(computed).toBe(await root.getAttribute('data-viewport-form'))
    })
  }
})
