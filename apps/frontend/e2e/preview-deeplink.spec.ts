import { expect, test, type Page, type Route } from '@playwright/test'

function success<T>(data: T) {
  return { status: 'success', data }
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(success(data)),
  })
}

async function mockMinimalApis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/runs') {
      return fulfill(route, { items: [], total: 0, limit: 50, offset: 0 })
    }
    if (url.pathname === '/api/v1/pipeline/status') {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'error',
          error: { code: 'PIPELINE_CYCLE_NOT_FOUND', message: 'No cycle' },
        }),
      })
    }
    if (url.pathname === '/api/v1/pipeline/stages') {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'error',
          error: { code: 'PIPELINE_CYCLE_NOT_FOUND', message: 'No cycle' },
        }),
      })
    }
    if (url.pathname === '/api/v1/queue/depth') {
      return fulfill(route, { running: 0, pending: 0, idle: 0 })
    }
    if (url.pathname === '/api/v1/jobs') {
      return fulfill(route, { items: [], total: 0, limit: 12, offset: 0 })
    }
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, [])
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])

    throw new Error(`Unhandled preview API route: ${url.pathname}`)
  })
}

test.describe('production preview deep links', () => {
  test('loads /monitoring without local role selector', async ({ page }) => {
    await mockMinimalApis(page)
    await page.goto('/monitoring')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByLabel('Role')).toHaveCount(0)
  })

  test('loads /flood-alerts without local role selector', async ({ page }) => {
    await mockMinimalApis(page)
    await page.goto('/flood-alerts')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('heading', { name: '暂无洪水预警数据' })).toBeVisible()
    await expect(page.getByLabel('Role')).toHaveCount(0)
  })
})
