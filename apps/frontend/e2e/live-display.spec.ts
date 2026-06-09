import { expect, test } from '@playwright/test'

import {
  assertLiveDisplayPageEvidence,
  classifyLiveDisplayControlRequest,
  createLiveDisplayReadApiEvidence,
  isLiveDisplayReadApiUrl,
  isLiveDisplayRuntimeConfigUrl,
  liveDisplayApiBinding,
  parseLiveDisplayRuntimeConfigEvidence,
  type LiveDisplayBrowserResponse,
} from '../playwright.config.helpers'

test.describe('live display_readonly evidence', () => {
  test('loads live display_readonly frontend without local or control-plane requests', async ({ page, baseURL }) => {
    const apiBaseURL = process.env.PLAYWRIGHT_LIVE_API_BASE_URL
    if (!baseURL) throw new Error('PLAYWRIGHT_LIVE_BASE_URL is required for live display tests.')
    if (!apiBaseURL) throw new Error('PLAYWRIGHT_LIVE_API_BASE_URL is required for live display tests.')

    const binding = liveDisplayApiBinding(baseURL, apiBaseURL)
    const runtimeConfigResponses: LiveDisplayBrowserResponse[] = []
    const readApiResponses: LiveDisplayBrowserResponse[] = []
    const forbiddenControlRequests: string[] = []
    const responseParses: Promise<void>[] = []

    page.on('request', (request) => {
      const classification = classifyLiveDisplayControlRequest(request.method(), request.url())
      if (classification) {
        const url = new URL(request.url())
        forbiddenControlRequests.push(`${request.method()} ${url.pathname} (${classification})`)
      }
    })
    page.on('response', (response) => {
      const url = response.url()
      if (!isLiveDisplayRuntimeConfigUrl(url, binding) && !isLiveDisplayReadApiUrl(url, binding)) return

      if (isLiveDisplayRuntimeConfigUrl(url, binding)) {
        responseParses.push(parseLiveDisplayRuntimeConfigEvidence(response).then((evidence) => {
          runtimeConfigResponses.push(evidence)
        }))
        return
      }

      readApiResponses.push(createLiveDisplayReadApiEvidence(response))
    })

    await page.goto('/monitoring')
    const monitoringHeading = page.getByRole('heading', { name: '监控工作台' })
    const permissionDeniedAlert = page.getByRole('alert').filter({ hasText: '权限不足' })
    const runtimeConfigUnavailableStatus = page.getByText(/runtime config 不可用/)
    await expect(
      monitoringHeading.or(permissionDeniedAlert).or(runtimeConfigUnavailableStatus),
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.getByLabel('Role')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /重试|取消/ })).toHaveCount(0)
    await page.waitForLoadState('networkidle').catch(() => undefined)
    await Promise.all(responseParses)

    const evidence = assertLiveDisplayPageEvidence({
      runtimeConfigResponses,
      readApiResponses,
      forbiddenControlRequests,
      permissionDeniedVisible: await permissionDeniedAlert.isVisible().catch(() => false),
      runtimeConfigUnavailableVisible: await runtimeConfigUnavailableStatus.isVisible().catch(() => false),
    })

    await expect(monitoringHeading).toBeVisible()
    expect(
      new URL(evidence.runtimeConfigResponse.url).origin,
      `expected browser runtime config to bind to ${binding.mode} origin ${binding.expectedOrigin}`,
    ).toBe(binding.expectedOrigin)
    expect(
      new URL(evidence.readApiResponse.url).origin,
      `expected browser read API to bind to ${binding.mode} origin ${binding.expectedOrigin}`,
    ).toBe(binding.expectedOrigin)
  })
})
