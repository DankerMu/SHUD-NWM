import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 抽屉打开时地图外壳让位（openspec mobile-responsive-display task 4.6，design.md D11）。
 * 让位 = 移动形态 && 有曲线面板在渲染 && 曲线区域不在兜底；控制条与启动器列据它加 `invisible`
 * （保持挂载）、展开值复位、时间轴暂停。桌面形态恒不让位。
 *
 * 做法沿用 `OverviewPageSingleCurveWindow.test.tsx`：地图本体换成桩，由桩把一次命中要素的点击交给
 * 页面传下来的 `onOverlayClick`——它**不**调用 `onMapClick`，所以这里的“收起面板”只能来自让位本身。
 * 两个曲线面板换成带关闭按钮的桩；曲线区域的抛错用既有的测试门控崩溃开关（`RegionCrashProbe`）。
 */
vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: MaplibreControlStub,
    ScaleControl: MaplibreControlStub,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

const RIVER_CLICK = {
  layerId: 'discharge',
  event: { lngLat: { lng: 100.5, lat: 36.5 } },
  feature: {
    properties: {
      river_segment_id: 'seg-under-test',
      segment_id: 'seg-under-test',
      basin_version_id: 'basin-version-under-test',
      river_network_version_id: 'river-network-under-test',
      basin_id: 'basin-under-test',
    },
  },
} as unknown as M11MapOverlayInteraction

const STATION_CLICK = {
  layerId: 'met-stations',
  event: { lngLat: { lng: 100.5, lat: 36.5 } },
  feature: { properties: { station_id: 'station-under-test', station_name: 'Station under test', basin_id: 'basin-under-test' } },
} as unknown as M11MapOverlayInteraction

vi.mock('@/components/map/M11MapLibreSurface', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/map/M11MapLibreSurface')>()),
  M11MapLibreSurface: ({ onOverlayClick }: { onOverlayClick?: (interaction: M11MapOverlayInteraction) => void }) => (
    <div data-testid="m11-map-surface">
      <button type="button" data-testid="stub-click-river" onClick={() => onOverlayClick?.(RIVER_CLICK)} />
      <button type="button" data-testid="stub-click-station" onClick={() => onOverlayClick?.(STATION_CLICK)} />
    </div>
  ),
}))

type PanelStubProps = { onClose?: () => void }

vi.mock('@/components/map/M11RiverForecastPanel', () => ({
  M11RiverForecastPanel: ({ onClose }: PanelStubProps) => (
    <div data-testid="stub-river-panel">
      <button type="button" data-testid="stub-river-close" onClick={() => onClose?.()} />
    </div>
  ),
}))

vi.mock('@/components/map/M11StationForcingPopup', () => ({
  M11StationForcingPopup: ({ onClose }: PanelStubProps) => (
    <div data-testid="stub-station-panel">
      <button type="button" data-testid="stub-station-close" onClick={() => onClose?.()} />
    </div>
  ),
}))

type SwitchWindow = { __NHMS_E2E_HOOKS__?: unknown; __NHMS_E2E_CRASH_REGION__?: unknown }
const switchWindow = window as unknown as SwitchWindow

/** 桌面形态曲线区域兜底的定位类：逐字不变。 */
const CURVE_FALLBACK_DESKTOP_CLASS = 'absolute left-1/2 top-24 z-[130] -translate-x-1/2'
const PANEL_TEST_IDS = ['m11-floating-layer-switcher', 'm11-floating-basemap-switcher', 'm11-floating-legend'] as const
const LAUNCHER_TEST_IDS = ['m11-launcher-layers', 'm11-launcher-basemap', 'm11-launcher-legend'] as const

let router: ReturnType<typeof createMemoryRouter>

async function renderOverview() {
  router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  const view = render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  return view
}

const clickRiver = () => fireEvent.click(screen.getByTestId('stub-click-river'))
const clickStation = () => fireEvent.click(screen.getByTestId('stub-click-station'))
const controlBar = () => screen.getByTestId('m11-bottom-control-bar')
const launcherColumn = () => screen.getByTestId('m11-launcher-column')
const hasInvisible = (element: Element) => element.className.split(/\s+/).includes('invisible')
const validTimeInUrl = () => new URLSearchParams(router.state.location.search).get('validTime')

function expectYielded(yielded: boolean) {
  expect(hasInvisible(controlBar())).toBe(yielded)
  expect(hasInvisible(launcherColumn())).toBe(yielded)
}

function expectNoPanelExpanded() {
  for (const testId of PANEL_TEST_IDS) expect(screen.queryByTestId(testId)).toBeNull()
  for (const testId of LAUNCHER_TEST_IDS) expect(screen.getByTestId(testId)).toHaveAttribute('aria-expanded', 'false')
}

describe('OverviewPage: an open sheet yields the map chrome (D11)', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

  async function renderIn(mobile: boolean) {
    form = installMobileFormMatchMedia(mobile)
    await renderOverview()
  }

  beforeEach(() => {
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
    // 边界会把捕获的错误打进日志；这里是预期内的抛错。
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    form?.restore()
    form = undefined
    delete switchWindow.__NHMS_E2E_HOOKS__
    delete switchWindow.__NHMS_E2E_CRASH_REGION__
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  describe('mobile form', () => {
    it.each([
      ['layers', 'm11-launcher-layers', 'm11-floating-layer-switcher'],
      ['basemap', 'm11-launcher-basemap', 'm11-floating-basemap-switcher'],
      ['legend', 'm11-launcher-legend', 'm11-floating-legend'],
    ])('(v2) %s panel expanded, then a feature is selected without a map click: no panel stays expanded', async (_name, launcher, panel) => {
      await renderIn(true)
      fireEvent.click(screen.getByTestId(launcher))
      expect(screen.getByTestId(panel)).toBeInTheDocument()
      expect(screen.getByTestId(launcher)).toHaveAttribute('aria-expanded', 'true')

      clickRiver()

      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expectNoPanelExpanded()

      fireEvent.click(screen.getByTestId('stub-river-close'))

      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      expectNoPanelExpanded()
      expectYielded(false)
    })

    it('(v3) open -> yielded; replace -> still yielded; curve region falls back -> not yielded; retry succeeds -> yielded again', async () => {
      await renderIn(true)
      expectYielded(false)

      clickRiver()
      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expectYielded(true)

      // 河段窗开着时选中气象代站：单窗规则把河段窗换成气象代站窗，外壳一直让位。
      clickStation()
      expect(screen.getByTestId('stub-station-panel')).toBeInTheDocument()
      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      expectYielded(true)

      // 曲线面板渲染期抛错（开关在下一次渲染被探针读到）：外壳不得留在隐藏状态。
      switchWindow.__NHMS_E2E_HOOKS__ = true
      switchWindow.__NHMS_E2E_CRASH_REGION__ = 'curve'
      clickRiver()
      expect(await screen.findByTestId('region-error-map-panels')).toBeInTheDocument()
      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      expect(screen.queryByTestId('stub-station-panel')).toBeNull()
      expectYielded(false)

      // 兜底态下外壳可操作：展开一个面板。
      fireEvent.click(screen.getByTestId('m11-launcher-legend'))
      expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()

      // 开关仍在时「重试」：再次兜底，仍不让位。
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
      expect(screen.getByTestId('region-error-map-panels')).toBeInTheDocument()
      expectYielded(false)
      expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()

      // 清掉开关后「重试」：曲线窗回来，重新让位，展开的面板被收起（这条路没有地图点击）。
      delete switchWindow.__NHMS_E2E_CRASH_REGION__
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
      expect(screen.queryByTestId('region-error-map-panels')).toBeNull()
      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expectYielded(true)
      expectNoPanelExpanded()

      fireEvent.click(screen.getByTestId('stub-river-close'))
      expectYielded(false)
      expectNoPanelExpanded()
    })

    it('(v3) selecting another feature while in fallback clears the fallback through resetKeys and yields again', async () => {
      await renderIn(true)
      switchWindow.__NHMS_E2E_HOOKS__ = true
      switchWindow.__NHMS_E2E_CRASH_REGION__ = 'curve'
      clickRiver()
      expect(await screen.findByTestId('region-error-map-panels')).toBeInTheDocument()
      expectYielded(false)

      delete switchWindow.__NHMS_E2E_CRASH_REGION__
      clickStation()

      expect(screen.queryByTestId('region-error-map-panels')).toBeNull()
      expect(screen.getByTestId('stub-station-panel')).toBeInTheDocument()
      expectYielded(true)
    })

    it('(v3) the yielded chrome stays mounted: same nodes before, during and after the sheet', async () => {
      await renderIn(true)
      const bar = controlBar()
      const column = launcherColumn()
      const launchers = LAUNCHER_TEST_IDS.map((testId) => screen.getByTestId(testId))

      clickRiver()
      expectYielded(true)
      expect(controlBar()).toBe(bar)
      expect(launcherColumn()).toBe(column)
      expect(LAUNCHER_TEST_IDS.map((testId) => screen.getByTestId(testId))).toEqual(launchers)

      fireEvent.click(screen.getByTestId('stub-river-close'))
      expectYielded(false)
      expect(controlBar()).toBe(bar)
      expect(launcherColumn()).toBe(column)
    })

    it('opening a sheet while playing pauses the timeline, and closing it does not resume', async () => {
      await renderIn(true)
      vi.useFakeTimers()
      fireEvent.click(screen.getByLabelText('播放时间轴'))
      expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()

      clickRiver()

      expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
      expect(screen.queryByLabelText('暂停时间轴')).toBeNull()
      const pausedAt = validTimeInUrl()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      expect(validTimeInUrl()).toBe(pausedAt)

      fireEvent.click(screen.getByTestId('stub-river-close'))
      expectYielded(false)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      expect(validTimeInUrl()).toBe(pausedAt)
      expect(screen.getByLabelText('播放时间轴')).toBeInTheDocument()
    })

    it('leaving mobile form with a sheet open restores the chrome; re-entering yields again', async () => {
      await renderIn(false)
      clickRiver()
      expect(hasInvisible(controlBar())).toBe(false)

      act(() => form?.setMobile(true))
      expectYielded(true)

      act(() => form?.setMobile(false))
      expect(hasInvisible(controlBar())).toBe(false)
      expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    })
  })

  describe('desktop form', () => {
    it('(v3) the curve region fallback keeps its desktop positioning classes verbatim, and the chrome is not hidden', async () => {
      await renderIn(false)
      switchWindow.__NHMS_E2E_HOOKS__ = true
      switchWindow.__NHMS_E2E_CRASH_REGION__ = 'curve'
      clickRiver()

      const fallback = await screen.findByTestId('region-error-map-panels')
      expect(fallback.className).toContain(CURVE_FALLBACK_DESKTOP_CLASS)
      expect(fallback.className.endsWith(CURVE_FALLBACK_DESKTOP_CLASS)).toBe(true)
      expect(hasInvisible(controlBar())).toBe(false)
    })

    it('(v5) opening a window while playing: the control bar is not hidden and playback keeps advancing the valid time', async () => {
      await renderIn(false)
      vi.useFakeTimers()
      const before = validTimeInUrl()
      fireEvent.click(screen.getByLabelText('播放时间轴'))
      expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()

      clickRiver()

      expect(screen.getByTestId('stub-river-panel')).toBeInTheDocument()
      expect(hasInvisible(controlBar())).toBe(false)
      expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
      for (const testId of PANEL_TEST_IDS) expect(screen.getByTestId(testId)).toBeInTheDocument()
      expect(screen.getByLabelText('暂停时间轴')).toBeInTheDocument()
      // 开窗之后才推进计时器：有效时刻的前进只能来自“窗开着时仍在播放”。
      expect(validTimeInUrl()).toBe(before)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
      const advanced = validTimeInUrl()
      expect(advanced).not.toBeNull()
      expect(advanced).not.toBe(before)
    })
  })
})
