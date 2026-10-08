import { expect, test } from '@playwright/test'

import { mockLegendLauncherApi } from './support/legendLauncher.mocked'
import {
  OVERLAY_PANELS,
  expectDesktopPanelsAtOffsets,
  expectDesktopPanelsVisible,
  expectNoLaunchers,
  expectOnlyExpanded,
  overlayPart,
} from './support/overlayLaunchers.mocked'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 桌面形态下图层 / 底图 / 图例仍是常驻面板、没有启动器（openspec mobile-responsive-display task 3.3，
 * design.md D5），以及离开再回到移动形态的往返。桌面 project 没有触屏，移动形态那一段用 `click()`。
 */
const PORTRAIT = { width: 390, height: 664 }
const DESKTOP = { width: 1280, height: 900 }

test.describe('M11 浮层启动器桌面形态', () => {
  test.beforeEach(async ({ page }) => {
    await mockLegendLauncherApi(page)
  })

  for (const viewport of [
    DESKTOP,
    // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态。
    { width: 768, height: 1024 },
  ]) {
    test(`三个面板常驻可见、没有任何启动器 @ ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.goto('/')
      await expectMapControlsMounted(page)

      await expectDesktopPanelsVisible(page)
      await expectNoLaunchers(page)
      await expectDesktopPanelsAtOffsets(page, `${viewport.width}x${viewport.height}`)
    })
  }

  test('390x664（展开图层面板）-> 1280x900 -> 390x664：桌面面板在原偏移且无启动器，回来后无面板展开', async ({ page }) => {
    await page.setViewportSize(PORTRAIT)
    await page.goto('/')
    await expectMapControlsMounted(page)

    await overlayPart(page, 'layers').launcher.click()
    await expectOnlyExpanded(page, 'layers')

    await page.setViewportSize(DESKTOP)
    await expectNoLaunchers(page)
    await expectDesktopPanelsVisible(page)
    await expectDesktopPanelsAtOffsets(page, 'round-trip 1280x900')

    await page.setViewportSize(PORTRAIT)
    for (const name of OVERLAY_PANELS) {
      await expect(overlayPart(page, name).launcher).toBeVisible()
    }
    await expectOnlyExpanded(page, null)
  })
})
