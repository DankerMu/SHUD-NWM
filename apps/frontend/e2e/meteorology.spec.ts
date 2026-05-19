import { expect, test } from '@playwright/test'

test.describe('M13 meteorology products', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', (route) => {
      throw new Error(`Meteorology E2E must not depend on backend APIs: ${route.request().url()}`)
    })
  })

  test('restores the CLDAS grid route with restricted state and disabled timeline', async ({ page }) => {
    await page.goto('/meteorology?tab=grid&source=CLDAS&variable=PRCP')

    await expect(page.getByRole('heading', { name: '气象数据产品' })).toBeVisible()
    await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: /气象数据/ })).toHaveAttribute(
      'aria-current',
      'page',
    )
    await expect(page.getByRole('tab', { selected: true, name: /空间栅格/ })).toBeVisible()
    await expect(page.getByRole('button', { name: /PRCP/ })).toHaveClass(/border-primary-600/)
    await expect(page.getByRole('button', { name: /CLDAS restricted/ })).toHaveClass(/border-primary-600/)

    await expect(page.getByTestId('cldas-restricted')).toContainText('CLDAS 数据权限尚未开通')
    await expect(page.getByTestId('grid-unavailable')).toContainText('CLDAS 数据权限尚未开通')
    await expect(page.getByTestId('grid-cell-popup')).toBeHidden()
    await page.getByTestId('meteorology-grid-map').dispatchEvent('click', { clientX: 420, clientY: 320 })
    await expect(page.getByTestId('grid-cell-popup')).toContainText('CLDAS 数据权限尚未开通')

    const timeline = page.getByTestId('grid-timeline')
    await expect(timeline).toContainText('CLDAS 数据权限尚未开通')
    await expect(timeline.getByLabel('气象有效时间')).toBeDisabled()
  })

  test('restores the Yangtze station route with selected inventory, popup, adjacent stations, and forcing charts', async ({ page }) => {
    await page.goto('/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237')

    await expect(page.getByRole('heading', { name: '气象数据产品' })).toBeVisible()
    await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: /气象数据/ })).toHaveAttribute(
      'aria-current',
      'page',
    )
    await expect(page.getByRole('tab', { selected: true, name: /气象代站/ })).toBeVisible()
    await expect(page.getByLabel('流域')).toHaveValue('yangtze')

    const inventory = page.getByTestId('station-inventory')
    await expect(inventory).toContainText('HMT-Y2-0237')
    await expect(inventory).toContainText('HMT-Y2-0236')

    await expect(page.getByTestId('station-popup')).toContainText('HMT-Y2-0237')
    await expect(page.getByTestId('station-popup')).toContainText('黄冈代站')
    await expect(page.getByRole('button', { name: '选择站点 HMT-Y2-0237' })).toBeVisible()

    await expect(page.getByTestId('adjacent-stations')).toContainText('HMT-Y2-0236')
    const forcingCharts = page.getByTestId('forcing-charts')
    await expect(forcingCharts).toContainText('PRCP / mm/day')
    await expect(forcingCharts).toContainText('TEMP / degC')
    await expect(forcingCharts).toContainText('Rn')
    await expect(forcingCharts).toContainText('UI 不渲染合同外的模拟数值')
    await expect(page.getByTestId('forcing-series-truncated')).toContainText('样本上限')
    await expect(forcingCharts.locator('canvas')).toHaveCount(0)

    const marker = page.getByTestId('station-marker-HMT-Y2-0237')
    const initialLeft = await marker.evaluate((element) => (element as HTMLElement).style.left)
    const initialTop = await marker.evaluate((element) => (element as HTMLElement).style.top)
    await page.getByLabel('排序').selectOption('station_id')
    await expect(page.getByTestId('station-popup')).toContainText('HMT-Y2-0237')
    expect(await marker.evaluate((element) => (element as HTMLElement).style.left)).toBe(initialLeft)
    expect(await marker.evaluate((element) => (element as HTMLElement).style.top)).toBe(initialTop)
  })

  test('shows reachable station and grid resource-limit states', async ({ page }) => {
    await page.goto(`/meteorology?tab=stations&search=${'HMT'.repeat(30)}`)

    await expect(page.getByText(/超过 80 字符/)).toBeVisible()
    await expect(page.getByTestId('station-empty')).toContainText('搜索无结果')

    await page.goto('/meteorology?tab=stations')
    await expect(page.getByTestId('station-inventory-truncated')).toContainText('每页 2 条')

    await page.goto('/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=140&gridQueryLat=60')
    await expect(page.getByTestId('grid-cell-popup')).toContainText('超出合同 bbox')
  })
})
