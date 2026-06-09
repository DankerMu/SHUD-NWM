import { expect, test } from '@playwright/test'

function liveApiUrl(path: string) {
  const apiBaseURL = process.env.PLAYWRIGHT_LIVE_API_BASE_URL
  if (!apiBaseURL) throw new Error('PLAYWRIGHT_LIVE_API_BASE_URL is required for live display tests.')
  return new URL(path, apiBaseURL.endsWith('/') ? apiBaseURL : `${apiBaseURL}/`).toString()
}

function unwrapApiData(value: unknown) {
  if (value && typeof value === 'object' && 'data' in value) {
    return (value as { data: unknown }).data
  }
  return value
}

test.describe('live display_readonly evidence', () => {
  test('loads live display_readonly frontend without local or mutation controls', async ({ page, request }) => {
    const runtimeConfigResponse = await request.get(liveApiUrl('/api/v1/runtime/config'))
    expect(runtimeConfigResponse.ok(), `runtime config request failed with ${runtimeConfigResponse.status()}`).toBe(true)

    const runtimeConfig = unwrapApiData(await runtimeConfigResponse.json()) as {
      display_readonly?: boolean
      service_role?: string
    }
    expect(
      runtimeConfig.service_role === 'display_readonly' || runtimeConfig.display_readonly === true,
      `expected display_readonly runtime config, received ${JSON.stringify(runtimeConfig)}`,
    ).toBe(true)

    const forbiddenControlRequests: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      const isRunMutation = /^\/api\/v1\/runs\/[^/]+\/(retry|cancel)$/.test(url.pathname)
      const isSlurmControl = url.pathname.startsWith('/api/v1/slurm/')
      if ((isRunMutation || isSlurmControl) && request.method() !== 'GET') {
        forbiddenControlRequests.push(`${request.method()} ${url.pathname}`)
      }
    })

    await page.goto('/monitoring')
    await expect(
      page.getByRole('heading', { name: '监控工作台' }).or(page.getByRole('alert').filter({ hasText: '权限不足' })),
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.getByLabel('Role')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /重试|取消/ })).toHaveCount(0)
    await page.waitForLoadState('networkidle').catch(() => undefined)

    expect(forbiddenControlRequests).toEqual([])
  })
})
