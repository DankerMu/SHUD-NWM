import { test } from '@playwright/test'

import {
  expectMapControlsMounted,
  expectZoomControlAbsent,
  expectZoomControlPresent,
  mockZoomControlApi,
} from './support/zoomControl.mocked'

/**
 * 桌面形态保留地图缩放 / 指北控件（openspec mobile-responsive-display task 3.1，design.md D7），
 * 以及形态在运行中翻转时控件随之卸载 / 重新挂载、不重复不残留。
 */
test.describe('M11 地图缩放控件桌面形态', () => {
  test.beforeEach(async ({ page }) => {
    await mockZoomControlApi(page)
  })

  for (const viewport of [
    { width: 1280, height: 900 },
    // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态。
    { width: 768, height: 1024 },
  ]) {
    test(`/ 上 zoom-in / zoom-out / reset-bearing 各恰一个且可见 @ ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.goto('/')

      await expectMapControlsMounted(page)
      await expectZoomControlPresent(page)
    })
  }

  test('1280x900 -> 390x664 -> 1280x900：按钮消失后重新出现，仍各恰一个', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/')
    await expectMapControlsMounted(page)
    await expectZoomControlPresent(page)

    await page.setViewportSize({ width: 390, height: 664 })
    // 翻到移动形态后地图与兄弟控件仍在，只有缩放 / 指北控件被卸载。
    await expectMapControlsMounted(page)
    await expectZoomControlAbsent(page)

    await page.setViewportSize({ width: 1280, height: 900 })
    await expectMapControlsMounted(page)
    await expectZoomControlPresent(page)
  })
})
