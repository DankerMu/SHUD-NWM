import { expect, test } from '@playwright/test'

import { setRole } from './support/setRole'

/**
 * 开发用角色切换器的桌面形态不变量（openspec mobile-responsive-display task 2.5）：
 * 位置、宽度与改动前相同，`setRole` 助手在桌面形态下同样可用。
 */
test.describe('M11 角色切换器桌面形态', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      const data =
        url.pathname === '/api/v1/runtime/config'
          ? {
              service_role: 'compute_control',
              control_mutations_enabled: true,
              slurm_routes_enabled: true,
              queue_depth_mode: 'slurm_gateway',
              display_readonly: false,
            }
          : []
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
    })
  })

  for (const viewport of [
    { width: 1280, height: 900 },
    // 平板：宽恰为 768、高 ≥ 500，仍是桌面形态。
    { width: 768, height: 1024 },
  ]) {
    test(`/ops 上位于 main 右上角 16px、宽 144px，并能切到 operator @ ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.goto('/ops')

      const trigger = page.getByLabel('Role')
      await expect(trigger).toBeVisible()
      await expect(trigger).toHaveText('Viewer')

      const triggerBox = await trigger.boundingBox()
      const mainBox = await page.locator('main').boundingBox()
      expect(triggerBox).not.toBeNull()
      expect(mainBox).not.toBeNull()
      expect(triggerBox!.width).toBe(144)
      expect(mainBox!.x + mainBox!.width - (triggerBox!.x + triggerBox!.width)).toBe(16)
      expect(triggerBox!.y - mainBox!.y).toBe(16)

      await expect(page.getByText('权限不足')).toBeVisible()
      await setRole(page, 'operator')
      await expect(page.getByText('权限不足')).toHaveCount(0)
      await expect(page.getByRole('heading', { name: '内部诊断' })).toBeVisible()
    })
  }
})
