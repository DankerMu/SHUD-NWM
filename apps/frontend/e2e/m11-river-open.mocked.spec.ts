import { expect, test, type Page } from '@playwright/test'

import { openRiverWindow } from './support/openRiverWindow'
import { dischargeTileCoordinates, isRiverFixtureTilePath, riverFixture, riverFixtureTilePath } from './support/riverFixture'
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
    // 河段来自夹具瓦片：mock 记下全部径流瓦片请求（不只是命中的），其中必须有夹具所属产品
    // （默认源 / 周期 / 时次）下的那一条完整路径；且夹具层级上请求过的径流瓦片只有这一张——
    // 钩子 fit 之后视口没有落到邻瓦片上。
    expect(mocks.dischargeTileRequests, '夹具瓦片应按其所属产品的完整路径被请求过').toContain(riverFixtureTilePath)
    const atFixtureZoom = mocks.dischargeTileRequests.filter(
      (path) => dischargeTileCoordinates(path)?.z === riverFixture.tile.z,
    )
    expect(atFixtureZoom.length).toBeGreaterThan(0)
    expect(
      atFixtureZoom.filter((path) => !isRiverFixtureTilePath(path)),
      `z=${riverFixture.tile.z} 上不应请求夹具瓦片以外的径流瓦片`,
    ).toEqual([])
    expect(mocks.unmocked, '不应有未被 mock 的 /api/v1 请求').toEqual([])
  }

  // 生产的全国径流瓦片不带 segment_name（services/tiles/mvt.py 的 hydro-national 属性集），窗口标题
  // 走 formatRiverSegmentDisplayName 的回退链：夹具河段 ID 不是 `<流域>_riv_<序号>` 形，解析不出
  // “流域 河段 N”，于是标题就是河段 ID 本身，副行是“河段 ID <id>”。夹具瓦片若多带了名字，这里会红。
  async function expectProductionFallbackTitle(page: Page) {
    const header = page.getByTestId('m11-river-forecast-panel').locator('header')
    const { riverSegmentId } = riverFixture.identity
    await expect(header.getByText(riverSegmentId, { exact: true })).toBeVisible()
    await expect(header.getByText(`河段 ID ${riverSegmentId}`, { exact: true })).toBeVisible()
    await expect(header.locator(`[title="河段 ID ${riverSegmentId}"]`)).toHaveText(riverSegmentId)
  }

  test('桌面 1280×900：鼠标点击打开河段窗，曲线为已加载', async ({ page }) => {
    const opened = await openRiverWindow(page)

    expect(opened.input).toBe('click')
    await expect(page.getByTestId('m11-river-forecast-panel')).toBeVisible()
    await expectProductionFallbackTitle(page)
    await expectCurveLoaded(page)
  })

  // 触摸分支也在桌面 project 里自证（同一视口，只开触屏）；三个移动 project 下的开窗归 task 4.2。
  test.describe('触屏上下文', () => {
    test.use({ hasTouch: true })

    test('桌面 1280×900：轻触打开河段窗，曲线为已加载', async ({ page }) => {
      const opened = await openRiverWindow(page)

      expect(opened.input).toBe('tap')
      await expect(page.getByTestId('m11-river-forecast-panel')).toBeVisible()
      await expectProductionFallbackTitle(page)
      await expectCurveLoaded(page)
    })
  })
})
