import { expect, test } from '@playwright/test'

import { isMobileForm, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted, expectZoomControlAbsent, mockZoomControlApi } from './support/zoomControl.mocked'

/**
 * 移动形态不渲染地图缩放 / 指北控件（openspec mobile-responsive-display task 3.1，design.md D7）。
 * 三个移动 project 下都要通过；期望不随“是否矮视口横屏”变化，所以没有形态分支。
 */
test.describe('M11 地图缩放控件移动形态', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    await mockZoomControlApi(page)
  })

  test('/ 上地图与比例尺控件已挂载，但没有 zoom-in / zoom-out / reset-bearing 按钮', async ({ page }) => {
    await page.goto('/')

    // 先证明地图与兄弟控件都挂上了，再断言缩放按钮计数为 0（否则是空过）。
    await expectMapControlsMounted(page)
    await expectZoomControlAbsent(page)
  })
})
