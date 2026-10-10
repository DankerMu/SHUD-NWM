import { expect, test, type Page } from '@playwright/test'

import {
  controlledCycleTime,
  controlledFailedJobId,
  controlledRetryJobId,
  controlledRunId,
  cycle,
  cycleTime,
  expectedFormattedDate,
  fulfill,
  mockControlledOpsApi,
  mockModelAssetsApi,
  mockMonitoringApi,
  runtimeConfig,
  type MonitoringApiMockOptions,
} from './support/monitoring.mocked'
import { setRole } from './support/setRole'

async function openMonitoringAsOperator(page: Page, mockOptions?: MonitoringApiMockOptions) {
  await mockMonitoringApi(page, mockOptions)
  // 监控页默认 cycle = 当前整点并走 strict identity 校验；测试 fixture 是固定周期，
  // 必须用 URL 钉住该 source/cycle，使页面请求的 cycle 与 mocked 响应身份一致。
  await page.goto(`/monitoring?source=gfs&cycle=${encodeURIComponent(cycleTime)}`)
  await expect(page.getByText('权限不足')).toBeVisible()
  await setRole(page, 'operator')
  await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
}

test.describe('monitoring page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/monitoring')
    await expect(
      page.getByLabel('Role'),
      'monitoring E2E requires the explicit dev/test role override; use the Playwright webServer or run the target server with VITE_ENABLE_ROLE_OVERRIDE=true',
    ).toBeVisible()
  })

  test('keeps viewer as the default role before local dev override is used', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByLabel('Role')).toHaveText('Viewer')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })

  test('loads summary bar, stage cards, jobs table, and trend charts', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await expect(page.getByRole('heading', { name: '当前周期' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '七阶段流水线' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '作业列表' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '趋势' })).toBeVisible()

    const summarySection = page.locator('section').filter({
      has: page.getByRole('heading', { name: '当前周期' }),
    }).first()
    await expect(summarySection).toContainText(cycle.source)
    await expect(summarySection).toContainText(expectedFormattedDate(cycleTime))
    await expect(summarySection).toContainText(/成功\s*3/)
    await expect(summarySection).toContainText(/失败\s*1/)
    await expect(summarySection).toContainText(/运行中\s*1/)
    await expect(summarySection).toContainText(/等待\s*2/)

    await expect(page.getByRole('button', { name: /下载.*succeeded/ })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-success' })).toBeVisible()
    await expect(page.getByRole('row', { name: /run-failed/ })).toContainText('model-b')
    await expect(page.getByRole('row', { name: /run-failed/ })).toContainText('failed')
  })

  test('expands a failed stage to show basin failures', async ({ page }) => {
    await openMonitoringAsOperator(page)

    const stageSection = page.locator('section, div').filter({
      has: page.getByRole('heading', { name: '七阶段流水线' }),
    }).first()

    await stageSection.getByRole('button', { name: /强迫场.*partially_failed/ }).click()

    const basinFailures = stageSection.locator('div').filter({
      hasText: 'FORCING_MISSING',
    }).last()
    await expect(basinFailures).toContainText('model-b')
    await expect(basinFailures).toContainText('FORCING_MISSING')
    await expect(basinFailures).toContainText('forcing input missing')
  })

  test('updates the jobs table when filters change', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await page.getByLabel('Status filter').click()
    await page.getByRole('option', { name: 'succeeded' }).click()

    await expect(page.getByRole('cell', { name: 'run-success' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toHaveCount(0)
  })

  test('opens the job log modal and shows log content', async ({ page }) => {
    await openMonitoringAsOperator(page)

    const failedRow = page.getByRole('row', { name: /run-failed/ })
    await failedRow.getByRole('button', { name: /查看日志/ }).click()

    await expect(page.getByRole('dialog')).toContainText('作业日志 job-failed')
    await expect(page.getByRole('dialog')).toContainText('forecast stderr: model failed')
  })

  test('shows retry for dev override operator and sends the role header', async ({ page }) => {
    const retryRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    await openMonitoringAsOperator(page, {
      onRetryRequest: (request) => {
        retryRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    const retryButton = page.getByRole('row', { name: /run-failed/ }).getByRole('button', { name: /重试/ })
    await expect(retryButton).toBeVisible()
    await retryButton.click()

    await expect.poll(() => retryRequests).toEqual([
      { method: 'POST', pathname: '/api/v1/runs/run-failed/retry', role: 'operator' },
    ])
    await expect(page.getByRole('listitem').filter({ hasText: '重试已提交' })).toBeVisible()

    await setRole(page, 'viewer')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
  })

  test('proves the /ops controlled failure log and retry lifecycle', async ({ page }) => {
    const retryRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    const opsApi = await mockControlledOpsApi(page, {
      onRetryRequest: (request) => {
        retryRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    await page.goto(`/ops?source=gfs&cycle=${encodeURIComponent(controlledCycleTime)}`)
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)

    await setRole(page, 'operator')
    // M26：/ops 工作台标题为「内部诊断」（旧「运维工作台」已改名）。
    await expect(page.getByRole('heading', { name: '内部诊断' })).toBeVisible()
    const currentStateField = page.getByText('Current State').locator('..')
    await expect(currentStateField).toContainText('failed_run')
    await expect(page.getByRole('cell', { name: controlledRunId })).toBeVisible()
    const failedRow = page.getByRole('row', { name: new RegExp(controlledFailedJobId) })
    await expect(failedRow).toContainText('forecast')
    await expect(failedRow).toContainText('failed')
    await expect(failedRow).toContainText('slurm_qhh_forecast_failed_2100')

    await failedRow.getByRole('button', { name: /查看日志/ }).click()
    await expect(page.getByRole('dialog')).toContainText(`作业日志 ${controlledFailedJobId}`)
    await expect(page.getByRole('dialog')).toContainText('controlled qhh forecast failure')
    await expect(page.getByRole('dialog')).toContainText(controlledRunId)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)

    await failedRow.getByRole('button', { name: /重试/ }).click()
    await expect.poll(() => retryRequests).toEqual([
      { method: 'POST', pathname: `/api/v1/runs/${controlledRunId}/retry`, role: 'operator' },
    ])
    await expect.poll(() => opsApi.retrySubmitted()).toBe(true)
    await expect(page.getByRole('listitem').filter({ hasText: '重试已提交' })).toBeVisible()

    const retryRow = page.getByRole('row', { name: new RegExp(controlledRetryJobId) })
    await expect(retryRow).toBeVisible()
    await expect(retryRow).toContainText('succeeded')
    await expect(retryRow).toContainText('slurm_retry')
    await expect(retryRow).toContainText('2')
    await expect(currentStateField).toContainText('failed_run')
    await expect(page.getByRole('button', { name: /预报.*succeeded/ })).toBeVisible()
    await expect(page.getByText('qhh_sibling_cycle_forecast_failed')).toHaveCount(0)
    expect(opsApi.observedJobsQueries.length).toBeGreaterThan(0)
    for (const query of opsApi.observedJobsQueries) {
      expect(query).toEqual({ source: 'GFS', cycle: controlledCycleTime })
    }

    await setRole(page, 'viewer')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
  })

  test('shows cancel for dev override operator and hides it when role becomes viewer', async ({ page }) => {
    const cancelRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    await openMonitoringAsOperator(page, {
      onCancelRequest: (request) => {
        cancelRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    const cancelButton = page.getByRole('row', { name: /run-running/ }).getByRole('button', { name: /取消/ })
    await expect(cancelButton).toBeVisible()
    await cancelButton.click()

    await expect.poll(() => cancelRequests).toEqual([
      { method: 'POST', pathname: '/api/v1/runs/run-running/cancel', role: 'operator' },
    ])
    await expect(page.getByRole('listitem').filter({ hasText: '取消请求已提交' })).toBeVisible()

    await setRole(page, 'viewer')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /取消/ })).toHaveCount(0)
  })

  test('uses the configured API base for monitoring reads and operator actions', async ({ page }) => {
    const origins: Array<{ origin: string; pathname: string; method: string }> = []
    await openMonitoringAsOperator(page, {
      onApiRequest: (request) => {
        const url = new URL(request.url())
        origins.push({ origin: url.origin, pathname: url.pathname, method: request.method() })
      },
    })

    await expect.poll(() => origins.map((call) => call.pathname)).toContain('/api/v1/metrics/stage-duration')
    await expect.poll(() => origins.map((call) => call.pathname)).toContain('/api/v1/metrics/success-rate')
    await page.getByRole('row', { name: /run-failed/ }).getByRole('button', { name: /重试/ }).click()
    await page.getByRole('row', { name: /run-running/ }).getByRole('button', { name: /取消/ }).click()

    const expectedPaths = new Set([
      '/api/v1/pipeline/status',
      '/api/v1/pipeline/stages',
      '/api/v1/queue/depth',
      '/api/v1/metrics/stage-duration',
      '/api/v1/metrics/success-rate',
      '/api/v1/jobs',
      '/api/v1/runs/run-failed/retry',
      '/api/v1/runs/run-running/cancel',
    ])
    for (const path of expectedPaths) {
      expect(origins.some((call) => call.origin === 'https://api.example.test' && call.pathname === path)).toBe(true)
    }
  })

  test('denies monitoring access to viewer role', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })

  test('serves monitoring deep link through the SPA fallback', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    // 去 NavBar 后（#337），SPA fallback 命中的证据是路由真被服务并渲染出 RBAC gate，
    // 而非旧导航栏品牌字样；viewer 默认角色 → 权限不足占位可见。
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })
})

async function wheelPageToBottom(page: Page, heading: string) {
  const headingLocator = page.getByRole('heading', { name: heading })
  await expect(headingLocator).toBeVisible()

  const geometry = await headingLocator.evaluate(() => {
    const root = document.querySelector('main')?.querySelector(':scope > :last-child')
    if (!(root instanceof HTMLElement)) {
      throw new Error('operational page root under main was not found')
    }
    const firstChild = root.firstElementChild
    return {
      scrollTop: root.scrollTop,
      scrollHeight: root.scrollHeight,
      clientHeight: root.clientHeight,
      contentY: firstChild instanceof HTMLElement ? firstChild.getBoundingClientRect().y : null,
      windowScrollY: window.scrollY,
    }
  })
  expect(geometry.scrollHeight, 'fixture content must overflow the short viewport').toBeGreaterThan(geometry.clientHeight)
  expect(geometry.contentY).not.toBeNull()

  const rootBox = await headingLocator.evaluate(() => {
    const root = document.querySelector('main')?.querySelector(':scope > :last-child')
    if (!(root instanceof HTMLElement)) throw new Error('missing operational scroll root')
    const box = root.getBoundingClientRect()
    return { x: box.x + box.width / 2, y: box.y + Math.min(40, box.height / 2) }
  })
  await page.mouse.move(rootBox.x, rootBox.y)
  await page.mouse.wheel(0, 2400)

  await expect
    .poll(async () =>
      headingLocator.evaluate((_node, startTop: number) => {
        const root = document.querySelector('main')?.querySelector(':scope > :last-child')
        if (!(root instanceof HTMLElement)) return null
        return {
          moved: root.scrollTop > startTop,
          windowScrollY: window.scrollY,
        }
      }, geometry.scrollTop),
    )
    .toEqual(expect.objectContaining({ moved: true, windowScrollY: 0 }))

  const afterFirstWheel = await headingLocator.evaluate(() => {
    const root = document.querySelector('main')?.querySelector(':scope > :last-child')
    if (!(root instanceof HTMLElement)) throw new Error('missing operational scroll root')
    const firstChild = root.firstElementChild
    return {
      scrollTop: root.scrollTop,
      maxScroll: root.scrollHeight - root.clientHeight,
      contentY: firstChild instanceof HTMLElement ? firstChild.getBoundingClientRect().y : null,
      windowScrollY: window.scrollY,
    }
  })
  expect(afterFirstWheel.scrollTop, 'wheel must move the internal page, not only the window').toBeGreaterThan(geometry.scrollTop)
  expect(afterFirstWheel.contentY).not.toBeNull()
  expect(afterFirstWheel.contentY!).toBeLessThan(geometry.contentY!)
  expect(afterFirstWheel.windowScrollY).toBe(0)

  const remaining = afterFirstWheel.maxScroll - afterFirstWheel.scrollTop
  if (remaining > 1) {
    await page.mouse.wheel(0, remaining + 400)
  }

  await expect
    .poll(async () =>
      headingLocator.evaluate(() => {
        const root = document.querySelector('main')?.querySelector(':scope > :last-child')
        if (!(root instanceof HTMLElement)) return null
        return {
          remaining: root.scrollHeight - root.clientHeight - root.scrollTop,
          windowScrollY: window.scrollY,
        }
      }),
    )
    .toEqual(expect.objectContaining({ windowScrollY: 0 }))
  const settled = await headingLocator.evaluate(() => {
    const root = document.querySelector('main')?.querySelector(':scope > :last-child')
    if (!(root instanceof HTMLElement)) throw new Error('missing operational scroll root')
    return root.scrollHeight - root.clientHeight - root.scrollTop
  })
  expect(settled, 'wheel must be able to reach the page bottom').toBeLessThanOrEqual(1)
}

async function mockOverviewApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/runtime/config') return fulfill(route, runtimeConfig)
    if (url.pathname === '/api/v1/layers') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          data: [
            {
              layer_id: 'discharge',
              layer_name: 'Discharge',
              layer_type: 'hydrology',
              variables: ['q_down'],
              metadata: { layer_id: 'discharge', valid_times: [] },
            },
          ],
        }),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data: [] }) })
  })
}

test.describe('operational pages own vertical scrolling', () => {
  test('wheels /monitoring to the bottom without moving the window', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 })
    await mockMonitoringApi(page)
    await page.goto(`/monitoring?source=gfs&cycle=${encodeURIComponent(cycleTime)}`)
    await expect(page.getByText('权限不足')).toBeVisible()
    await setRole(page, 'operator')
    await wheelPageToBottom(page, '监控工作台')
  })

  test('wheels /ops to the bottom without moving the window', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 })
    await mockControlledOpsApi(page)
    await page.goto(`/ops?source=gfs&cycle=${encodeURIComponent(controlledCycleTime)}`)
    await expect(page.getByText('权限不足')).toBeVisible()
    await setRole(page, 'operator')
    await wheelPageToBottom(page, '内部诊断')
  })

  test('wheels /system/model-assets to the bottom without moving the window', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 })
    await mockModelAssetsApi(page)
    await page.goto('/system/model-assets?modelId=basins_basin_a_shud')
    await expect(page.getByText('权限不足')).toBeVisible()
    await setRole(page, 'model_admin')
    await expect(page.getByRole('heading', { name: '模型资产管理' })).toBeVisible()
    await expect(page.getByText('生命周期操作')).toBeVisible()
    await wheelPageToBottom(page, '模型资产管理')
  })
})

test.describe('map fits the available height', () => {
  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 1280, height: 600 },
  ]) {
    test(`map fills below the header at ${viewport.width}x${viewport.height} without window scroll`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await mockOverviewApi(page)
      await page.goto('/?source=gfs&layer=discharge&basemap=vector')

      const map = page.locator('[data-testid="m11-fullscreen-map"]')
      await expect(map).toBeVisible()

      const boxes = await page.evaluate(() => {
        const mapNode = document.querySelector('[data-testid="m11-fullscreen-map"]')
        const main = document.querySelector('main')
        if (!(mapNode instanceof HTMLElement) || !(main instanceof HTMLElement)) {
          throw new Error('map or main missing')
        }
        const mapBox = mapNode.getBoundingClientRect()
        const mainBox = main.getBoundingClientRect()
        return {
          map: { x: mapBox.x, y: mapBox.y, width: mapBox.width, height: mapBox.height, bottom: mapBox.bottom },
          main: { x: mainBox.x, y: mainBox.y, width: mainBox.width, height: mainBox.height, bottom: mainBox.bottom },
          windowScrollY: window.scrollY,
          documentScrollHeight: document.documentElement.scrollHeight,
          innerHeight: window.innerHeight,
        }
      })
      console.log(`map-fit ${viewport.width}x${viewport.height}`, JSON.stringify(boxes))

      expect(boxes.windowScrollY).toBe(0)
      expect(boxes.documentScrollHeight).toBeLessThanOrEqual(boxes.innerHeight + 1)
      expect(boxes.map.bottom).toBeLessThanOrEqual(boxes.main.bottom + 1)
      expect(boxes.map.bottom).toBeGreaterThan(boxes.main.y + boxes.main.height * 0.5)
      expect(boxes.map.height).toBeGreaterThan(0)
    })
  }
})
