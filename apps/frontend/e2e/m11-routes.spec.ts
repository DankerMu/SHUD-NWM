import { expect, test } from '@playwright/test'

test.describe('M11 navigation and route shells', () => {
  test('renders the national overview shell at / and /overview', async ({ page }) => {
    await page.route('**/api/v1/**', (route) => route.abort())

    await page.goto('/')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByRole('link', { name: /全国总览/ })).toBeVisible()

    await page.goto('/overview?source=gfs&layer=flood-return-period&basemap=terrain')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('flood-return-period')).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('terrain')).toBeVisible()
  })

  test('renders basin drill-down shell with restored query state', async ({ page }) => {
    await page.route('**/api/v1/**', (route) => route.abort())

    await page.goto(
      '/basins/basin-demo?basinVersionId=bv-001&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange&q=main',
    )

    await expect(page.getByRole('heading', { name: '流域分析' })).toBeVisible()
    await expect(page.getByLabel('流域钻取地图')).toBeVisible()
    await expect(page.getByText('basin-demo', { exact: true })).toBeVisible()
    await expect(page.getByText('seg-009').first()).toBeVisible()
    await expect(page.getByText('orange').first()).toBeVisible()
    await expect(page).toHaveURL(/cycle=2026-05-18T00%3A00%3A00.000Z/)
  })

  test('keeps implemented workflow routes reachable', async ({ page }) => {
    await page.route('**/api/v1/**', (route) => route.abort())

    for (const [path, label] of [
      ['/forecast', /水文预报/],
      ['/flood-alerts?warningLevel=major', /洪水预警/],
      ['/monitoring', /产品监控/],
    ] as const) {
      await page.goto(path)
      await expect(page.getByText('NHMS')).toBeVisible()
      await expect(page.getByRole('link', { name: label })).toBeVisible()
    }
  })
})
