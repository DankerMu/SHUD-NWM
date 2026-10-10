import { expect, test } from '@playwright/test'

import { mockMinimalDischargeCatalogApi } from './support/layerCatalog.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 移动 project 的基线：只断言今天已经成立的事实，用来证明三个移动 project 真的在跑、
 * 而不是空跑仍显示绿。移动布局本身的几何断言归后续 task 各自的 `.mobile.` spec。
 */
test.describe('M11 单图页移动视口基线', () => {
  test.beforeEach(async ({ page }) => {
    await mockMinimalDischargeCatalogApi(page)
  })

  test('加载 / 后 viewport meta 存在且地图区可见', async ({ page }) => {
    // 前提：本 spec 只该在移动形态的 project 下执行（844×390 只靠高度条件进入移动形态）。
    expect(isMobileForm(requireViewport(page))).toBe(true)

    await page.goto('/')

    const viewportMeta = page.locator('meta[name="viewport"]')
    await expect(viewportMeta).toHaveCount(1)
    await expect(viewportMeta).toHaveAttribute('content', /width=device-width/)
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()
  })
})
