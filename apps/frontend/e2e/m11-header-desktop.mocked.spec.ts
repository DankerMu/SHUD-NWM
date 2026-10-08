import { expect, test } from '@playwright/test'

import type { RoleValue } from './support/setRole'
import {
  BOX_TOLERANCE,
  HEADER_DESKTOP_HEIGHT,
  HEADER_MOBILE_HEIGHT,
  expectHeaderHeight,
  expectInside,
  mockSiteHeaderApi,
  openAuthorizedRoute,
  requireBox,
  siteHeaderParts,
  titleStyle,
} from './support/siteHeader.mocked'

/**
 * 桌面形态头部不变（84px、带副标题），以及桌面 project 里经 `setViewportSize` 进入移动形态的两个窗口
 * （openspec mobile-responsive-display task 2.4，design.md D4）。
 */
const DESKTOP = { width: 1280, height: 900 }
const LOGO_DESKTOP_SIZE = 48
const TITLE_DESKTOP_FONT_SIZE = 28
const SPONSORS_DESKTOP_HEIGHT = 56

const AUTHORIZED_ROUTES: Array<{ path: string; role: RoleValue; heading: string }> = [
  { path: '/ops', role: 'operator', heading: '内部诊断' },
  { path: '/monitoring', role: 'operator', heading: '监控工作台' },
  { path: '/system/model-assets', role: 'model_admin', heading: '模型资产管理' },
]

test.describe('M11 头部桌面形态', () => {
  test.beforeEach(async ({ page }) => {
    await mockSiteHeaderApi(page)
  })

  for (const viewport of [
    DESKTOP,
    // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态。
    { width: 768, height: 1024 },
  ]) {
    test(`/ 上头部 84px、副标题可见、标题 28px、徽标 48x48 @ ${viewport.width}x${viewport.height}`, async ({ page }) => {
      const label = `${viewport.width}x${viewport.height}`
      await page.setViewportSize(viewport)
      await page.goto('/')
      await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()

      const headerBox = await expectHeaderHeight(page, HEADER_DESKTOP_HEIGHT, label)
      const { logo, title, subtitle } = siteHeaderParts(page)
      await expect(subtitle).toBeVisible()
      await expect(title).toBeVisible()
      expect((await titleStyle(title)).fontSize, `${label}: 标题字号`).toBe(TITLE_DESKTOP_FONT_SIZE)

      await expect(logo).toBeVisible()
      const logoBox = await requireBox(logo, '徽标')
      expect(Math.abs(logoBox.width - LOGO_DESKTOP_SIZE), `${label}: 徽标宽 ${logoBox.width}`).toBeLessThanOrEqual(BOX_TOLERANCE)
      expect(Math.abs(logoBox.height - LOGO_DESKTOP_SIZE), `${label}: 徽标高 ${logoBox.height}`).toBeLessThanOrEqual(BOX_TOLERANCE)
      expectInside(logoBox, headerBox, `${label}: 徽标`)
    })
  }

  test('/ 在 1280x900 下合作单位条可见且高 56', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await page.goto('/')

    const headerBox = await expectHeaderHeight(page, HEADER_DESKTOP_HEIGHT, '1280x900')
    const { sponsors } = siteHeaderParts(page)
    await expect(sponsors).toBeVisible()
    const sponsorsBox = await requireBox(sponsors, '合作单位条')
    expect(Math.abs(sponsorsBox.height - SPONSORS_DESKTOP_HEIGHT), `合作单位条高 ${sponsorsBox.height}`).toBeLessThanOrEqual(BOX_TOLERANCE)
    expectInside(sponsorsBox, headerBox, '1280x900: 合作单位条')
  })

  for (const { path, role, heading } of AUTHORIZED_ROUTES) {
    test(`${path}（${role}）在 1280x900 下头部 84px`, async ({ page }) => {
      await page.setViewportSize(DESKTOP)
      await openAuthorizedRoute(page, path, role, heading)

      await expectHeaderHeight(page, HEADER_DESKTOP_HEIGHT, `${path} 1280x900`)
      await expect(siteHeaderParts(page).subtitle).toBeVisible()
    })
  }

  test('/ 在 520x900 下头部 48px（窄桌面窗口走移动形态）', async ({ page }) => {
    await page.setViewportSize({ width: 520, height: 900 })
    await page.goto('/')
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()

    await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, '520x900')
    await expect(siteHeaderParts(page).subtitle).toBeHidden()
  })

  test('/ 在 1280x400（高度臂 + lg）下头部 48px，合作单位条仍显示且留在头部内', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 400 })
    await page.goto('/')
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()

    const { sponsors } = siteHeaderParts(page)
    // 显示规则不改：1280 满足 lg，合作单位条仍可见。
    await expect(sponsors).toBeVisible()
    const headerBox = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, '1280x400')
    const sponsorsBox = await requireBox(sponsors, '合作单位条')
    console.log('site-header sponsors @ 1280x400', JSON.stringify({ header: headerBox, sponsors: sponsorsBox }))
    expect(sponsorsBox.width, '合作单位条应有宽度').toBeGreaterThan(0)
    expectInside(sponsorsBox, headerBox, '1280x400: 合作单位条')
  })
})
