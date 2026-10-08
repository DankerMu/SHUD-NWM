import { expect, test, type Page } from '@playwright/test'

import {
  STATION_LAYER_URL,
  gotoWithStationLayer,
  locateStationOnPage,
  openStationWindow,
  stationLocateInput,
} from './support/openStationWindow'
import { installRiverWindowMocks, mockStation, type RiverWindowMockLog } from './support/riverWindow.mocked'

/**
 * 气象代站窗测试夹具的自证（openspec mobile-responsive-display task 1.4）。
 *
 * 代站来自 GeoJSON 源。夹具用独立的只读钩子 `window.__nhmsStationLocateEvidence` 定位它，再做一次
 * 真实指针输入。这里验的是：窗口确实经产品点击路径打开，且曲线到达“已加载”（GFS 与 IFS 两源都成功）；
 * 不开门控时两个钩子全局都不存在；对没渲染的站点 id 钩子拒绝且不给点。移动 project 下的开窗归 task 4.2。
 */
const STATION_WINDOW = 'm11-station-popup'
const MAP_SURFACE = 'm11-map-surface'

type HookGlobals = { station: string[] | null; river: string[] | null }

/** 两个钩子全局各自的可枚举属性名；不存在时为 null。 */
function readHookGlobals(page: Page): Promise<HookGlobals> {
  return page.evaluate(() => {
    const globals = window as unknown as Record<string, unknown>
    const keys = (name: string) => (globals[name] === undefined ? null : Object.keys(globals[name] as object).sort())
    return { station: keys('__nhmsStationLocateEvidence'), river: keys('__nhmsRiverClickEvidence') }
  })
}

test.describe('M11 气象代站窗夹具：经真实点击路径打开代站窗', () => {
  let mocks: RiverWindowMockLog

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    mocks = await installRiverWindowMocks(page)
  })

  async function expectCurveLoaded(page: Page) {
    await expect(page.getByTestId('m11-station-popup-loaded')).toBeVisible()
    // 默认选中的变量有图，且 ECharts 已在容器里画出画布。
    const chart = page.getByTestId('m11-station-variable-PRCP-chart')
    await expect(chart).toBeVisible()
    await expect(chart.locator('canvas').first()).toBeVisible()
    // 两源都成功：没有 partial 说明，也不在刷新中；更不是空态 / 加载态 / 无产品态。
    for (const absent of [
      'm11-station-popup-partial',
      'm11-station-panel-refreshing',
      'm11-station-popup-empty',
      'm11-station-popup-loading',
      'm11-station-popup-no-product',
    ]) {
      await expect(page.getByTestId(absent)).toHaveCount(0)
    }
    // 图例里两个源都在画（没画出来的源会被划掉）。
    const loaded = page.getByTestId('m11-station-popup-loaded')
    for (const source of ['GFS', 'IFS']) {
      await expect(loaded.getByText(source, { exact: true })).not.toHaveClass(/line-through/)
    }
    expect(mocks.unmocked, '不应有未被 mock 的 /api/v1 请求').toEqual([])
  }

  async function expectWindowForMockStation(page: Page) {
    const stationWindow = page.getByTestId(STATION_WINDOW)
    await expect(stationWindow.getByText(mockStation.station_name, { exact: true })).toBeVisible()
    await expect(stationWindow.getByText(`站点 ID ${mockStation.station_id}`, { exact: true })).toBeVisible()
    await expect(page.getByTestId(MAP_SURFACE)).toHaveAttribute('data-selected-station-id', mockStation.station_id)
  }

  test('桌面 1280×900：鼠标点击打开代站窗，曲线为已加载', async ({ page }) => {
    const opened = await openStationWindow(page)

    expect(opened.input).toBe('click')
    // 钩子给的点在地图画布内。
    const canvasBox = await page.getByTestId(MAP_SURFACE).locator('canvas.maplibregl-canvas').boundingBox()
    expect(canvasBox).not.toBeNull()
    expect(opened.point.x).toBeGreaterThanOrEqual(canvasBox!.x)
    expect(opened.point.x).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width)
    expect(opened.point.y).toBeGreaterThanOrEqual(canvasBox!.y)
    expect(opened.point.y).toBeLessThanOrEqual(canvasBox!.y + canvasBox!.height)

    await expect(page.getByTestId(STATION_WINDOW)).toBeVisible()
    await expectWindowForMockStation(page)
    await expectCurveLoaded(page)
    // 开门控时：站点钩子恰一个方法，river 钩子仍恰三个。
    expect(await readHookGlobals(page)).toEqual({
      station: ['locateRenderedStation'],
      river: ['armPointerCapture', 'locateRenderedRiver', 'takePointerCapture'],
    })
  })

  // 触摸分支也在桌面 project 里自证（同一视口，只开触屏）；三个移动 project 下的开窗归 task 4.2。
  test.describe('触屏上下文', () => {
    test.use({ hasTouch: true })

    test('桌面 1280×900：轻触打开代站窗，曲线为已加载', async ({ page }) => {
      const opened = await openStationWindow(page)

      expect(opened.input).toBe('tap')
      await expect(page.getByTestId(STATION_WINDOW)).toBeVisible()
      await expectWindowForMockStation(page)
      await expectCurveLoaded(page)
    })
  })

  test('不开门控：站点钩子与 river 钩子两个全局都不存在', async ({ page }) => {
    // 不经助手（助手会开门控）。等到地图挂载、径流层注册、站点层有要素——钩子若会装，此时早已装上。
    await page.goto(STATION_LAYER_URL)
    await expect(page.getByTestId(MAP_SURFACE)).toHaveAttribute('data-registered-overlays', 'discharge')
    await expect(page.getByTestId(MAP_SURFACE)).toHaveAttribute('data-met-station-feature-count', '1')

    expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).__NHMS_E2E_HOOKS__)).toBeUndefined()
    expect(await readHookGlobals(page)).toEqual({ station: null, river: null })
    expect(
      await page.evaluate(() => {
        const globals = window as unknown as Record<string, unknown>
        return [typeof globals.__nhmsStationLocateEvidence, typeof globals.__nhmsRiverClickEvidence]
      }),
    ).toEqual(['undefined', 'undefined'])
  })

  test('未渲染的站点 id：钩子返回失败、不给点，也不开窗', async ({ page }) => {
    await gotoWithStationLayer(page)

    // 坐标是真实站点的坐标，id 不是：该点渲染着的是另一个站点。
    const outcome = await locateStationOnPage(page, { ...stationLocateInput, stationId: 'e2e-station-not-rendered' })

    expect(outcome).toEqual({ ok: false, code: 'STATION_HOOK_NOT_RENDERED', message: expect.any(String) })
    expect(outcome).not.toHaveProperty('located')
    expect(JSON.stringify(outcome)).not.toMatch(/clientX|clientY/)
    await expect(page.getByTestId(STATION_WINDOW)).toHaveCount(0)
    await expect(page.getByTestId(MAP_SURFACE)).toHaveAttribute('data-selected-station-id', '')

    // 同一个点、真实的 id：钩子给点（相机已在该处，第二次调用同样能等到地图静止），仍然不开窗。
    const rendered = await locateStationOnPage(page, stationLocateInput)
    expect(rendered).toEqual({
      ok: true,
      located: { stationId: mockStation.station_id, clientX: expect.any(Number), clientY: expect.any(Number) },
    })
    await expect(page.getByTestId(STATION_WINDOW)).toHaveCount(0)
  })
})
