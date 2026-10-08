import { expect, test, type Page } from '@playwright/test'

import { openRiverWindow } from './support/openRiverWindow'
import { riverFixture } from './support/riverFixture'
import { installRiverWindowMocks, type RiverWindowMockLog } from './support/riverWindowMocks'

/**
 * 河段窗测试夹具的自证（openspec mobile-responsive-display task 1.3）。
 *
 * 夹具把一条河段放进一张入库的矢量瓦片，用既有只读钩子定位它，再做一次真实指针输入。
 * 这里验的是：窗口确实经产品点击路径打开，且曲线到达“已加载”——GFS 与 IFS 两条都画出来，
 * 不是空态、加载态或只有一个源。移动 project 下的开窗归 task 4.2。
 */
test.describe('M11 河段窗夹具：经真实点击路径打开河段窗', () => {
  let mocks: RiverWindowMockLog

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    mocks = await installRiverWindowMocks(page)
  })

  async function expectCurveLoaded(page: Page) {
    const chart = page.getByTestId('m11-river-panel-chart')
    await expect(chart).toBeVisible()
    // 有数据点的图表：ECharts 已在容器里画出画布。
    await expect(chart.locator('canvas').first()).toBeVisible()
    for (const state of ['empty', 'loading', 'pending', 'partial']) {
      await expect(page.getByTestId(`m11-river-panel-${state}`)).toHaveCount(0)
    }
    // 两个源的 forecast-series 都取到了（dev 构建的 StrictMode 会让同一请求发两遍，故按集合比）。
    expect([...new Set(mocks.forecastSeriesScenarios)].sort()).toEqual([
      'forecast_gfs_deterministic',
      'forecast_ifs_deterministic',
    ])
    // 河段来自夹具瓦片：钩子 fit 之后的请求落在夹具声明的那张瓦片上。
    const { z, x, y } = riverFixture.tile
    expect(mocks.riverTileRequests.length, '夹具瓦片应被请求过').toBeGreaterThan(0)
    expect(mocks.riverTileRequests[0].endsWith(`/${z}/${x}/${y}.pbf`), mocks.riverTileRequests[0]).toBe(true)
    expect(mocks.unmocked, '不应有未被 mock 的 /api/v1 请求').toEqual([])
  }

  test('桌面 1280×900：鼠标点击打开河段窗，曲线为已加载', async ({ page }) => {
    const opened = await openRiverWindow(page)

    expect(opened.input).toBe('click')
    await expect(page.getByTestId('m11-river-forecast-panel')).toBeVisible()
    await expectCurveLoaded(page)
  })

  // 触摸分支也在桌面 project 里自证（同一视口，只开触屏）；三个移动 project 下的开窗归 task 4.2。
  test.describe('触屏上下文', () => {
    test.use({ hasTouch: true })

    test('桌面 1280×900：轻触打开河段窗，曲线为已加载', async ({ page }) => {
      const opened = await openRiverWindow(page)

      expect(opened.input).toBe('tap')
      await expect(page.getByTestId('m11-river-forecast-panel')).toBeVisible()
      await expectCurveLoaded(page)
    })
  })
})
