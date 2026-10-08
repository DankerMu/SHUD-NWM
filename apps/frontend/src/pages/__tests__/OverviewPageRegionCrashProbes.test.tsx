import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 四个区域边界与崩溃探针的接线（openspec mobile-responsive-display task 3.6，design.md D8）：
 * 门控 + 对应标识 -> 出现对应的 `region-error-*` 且只有那一个；曲线探针只在有曲线面板渲染时挂载；
 * 无门控时开关零读取、零影响。
 *
 * 地图本体换成桩：今天没有别的办法经 `OverviewPage` 打开河段 / 代站窗（真实通路是 MapLibre 的
 * 点击事件）。桩只做一件事——把一次命中要素的点击交给页面传下来的 `onOverlayClick`。
 * 两个曲线面板也换成桩：它们自己的取数与图表不是这里要测的，这里只测“面板是否在渲染”。
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

vi.mock('@/components/map/M11RiverForecastPanel', () => ({
  M11RiverForecastPanel: () => <div data-testid="stub-river-panel" />,
}))

vi.mock('@/components/map/M11StationForcingPopup', () => ({
  M11StationForcingPopup: () => <div data-testid="stub-station-panel" />,
}))

type SwitchWindow = { __NHMS_E2E_HOOKS__?: unknown; __NHMS_E2E_CRASH_REGION__?: unknown }
const switchWindow = window as unknown as SwitchWindow

function setCrashSwitch(region: string, gate = true) {
  if (gate) switchWindow.__NHMS_E2E_HOOKS__ = true
  switchWindow.__NHMS_E2E_CRASH_REGION__ = region
}

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  const view = render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  return view
}

function regionFallbackIds() {
  return screen.queryAllByTestId(/^region-error-/).map((node) => node.getAttribute('data-testid'))
}

const FORMS = [
  ['desktop', false],
  ['mobile', true],
] as const

describe('OverviewPage region crash probes', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

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
    vi.restoreAllMocks()
  })

  describe.each(FORMS)('%s form', (_name, mobile) => {
    beforeEach(() => {
      form = installMobileFormMatchMedia(mobile)
    })

    it('gate + map-controls: only the map-controls region falls back', async () => {
      setCrashSwitch('map-controls')
      await renderOverview()

      expect(regionFallbackIds()).toEqual(['region-error-map-controls'])
      // 兜底覆盖整个区域：图层 / 底图 / 运维入口都不在；其余区域照常。
      expect(screen.queryByTestId(mobile ? 'm11-launcher-layers' : 'm11-floating-layer-switcher')).toBeNull()
      expect(screen.queryByTestId(mobile ? 'm11-launcher-basemap' : 'm11-floating-basemap-switcher')).toBeNull()
      expect(screen.getByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeInTheDocument()
      expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
    })

    it('gate + legend: only the legend region falls back', async () => {
      setCrashSwitch('legend')
      await renderOverview()

      expect(regionFallbackIds()).toEqual(['region-error-legend'])
      expect(screen.queryByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeNull()
      expect(screen.getByTestId(mobile ? 'm11-launcher-layers' : 'm11-floating-layer-switcher')).toBeInTheDocument()
      expect(screen.getByTestId(mobile ? 'm11-launcher-basemap' : 'm11-floating-basemap-switcher')).toBeInTheDocument()
      expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
      if (mobile) {
        // 两处在列内的兜底保持在流：父节点就是启动器列，不带任何定位类。
        const fallback = screen.getByTestId('region-error-legend')
        expect(fallback.parentElement).toBe(screen.getByTestId('m11-launcher-column'))
        const tokens = fallback.className.split(/\s+/)
        for (const token of ['absolute', 'fixed', 'bottom-[7.5rem]', 'right-4', 'z-[120]']) expect(tokens).not.toContain(token)
      }
    })

    it('gate + control-bar: only the control-bar region falls back', async () => {
      setCrashSwitch('control-bar')
      await renderOverview()

      expect(regionFallbackIds()).toEqual(['region-error-control-bar'])
      expect(screen.queryByTestId('m11-bottom-control-bar')).toBeNull()
      expect(screen.getByTestId(mobile ? 'm11-launcher-layers' : 'm11-floating-layer-switcher')).toBeInTheDocument()
      expect(screen.getByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeInTheDocument()
    })

    it('gate + curve: no fallback until a curve panel renders, then only the curve region falls back (river window)', async () => {
      setCrashSwitch('curve')
      await renderOverview()

      // 没有面板打开：曲线探针没挂载，开关为 curve 也不产生兜底。
      expect(regionFallbackIds()).toEqual([])
      expect(screen.queryByTestId('stub-river-panel')).toBeNull()

      fireEvent.click(screen.getByTestId('stub-click-river'))

      expect(await screen.findByTestId('region-error-map-panels')).toBeInTheDocument()
      expect(regionFallbackIds()).toEqual(['region-error-map-panels'])
      expect(screen.queryByTestId('stub-river-panel')).toBeNull()
      // 曲线区域崩溃不带走其余区域。
      expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
      expect(screen.getByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeInTheDocument()
    })

    it('gate + curve: the station window mounts the probe too', async () => {
      setCrashSwitch('curve')
      await renderOverview()
      expect(regionFallbackIds()).toEqual([])

      fireEvent.click(screen.getByTestId('stub-click-station'))

      expect(await screen.findByTestId('region-error-map-panels')).toBeInTheDocument()
      expect(regionFallbackIds()).toEqual(['region-error-map-panels'])
      expect(screen.queryByTestId('stub-station-panel')).toBeNull()
    })

    it('gate without a matching switch: every region renders, and an opened curve panel renders too', async () => {
      setCrashSwitch('map')
      await renderOverview()
      fireEvent.click(screen.getByTestId('stub-click-river'))

      expect(await screen.findByTestId('stub-river-panel')).toBeInTheDocument()
      expect(regionFallbackIds()).toEqual([])
      expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
      expect(screen.getByTestId(mobile ? 'm11-launcher-layers' : 'm11-floating-layer-switcher')).toBeInTheDocument()
      expect(screen.getByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeInTheDocument()
    })

    it.each(['map-controls', 'legend', 'control-bar', 'curve'])(
      'no gate + %s: the switch is never read and nothing falls back, even with a curve panel open',
      async (region) => {
        const read = vi.fn(() => region)
        Object.defineProperty(window, '__NHMS_E2E_CRASH_REGION__', { configurable: true, get: read })
        await renderOverview()
        fireEvent.click(screen.getByTestId('stub-click-river'))

        expect(await screen.findByTestId('stub-river-panel')).toBeInTheDocument()
        expect(regionFallbackIds()).toEqual([])
        expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
        expect(screen.getByTestId(mobile ? 'm11-launcher-layers' : 'm11-floating-layer-switcher')).toBeInTheDocument()
        expect(screen.getByTestId(mobile ? 'm11-launcher-legend' : 'm11-floating-legend')).toBeInTheDocument()
        expect(read).not.toHaveBeenCalled()
      },
    )
  })

  it('retry keeps the existing semantics: still set -> fallback again; cleared -> the region recovers', async () => {
    form = installMobileFormMatchMedia(true)
    setCrashSwitch('legend')
    await renderOverview()
    expect(regionFallbackIds()).toEqual(['region-error-legend'])

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(regionFallbackIds()).toEqual(['region-error-legend'])
    expect(screen.queryByTestId('m11-launcher-legend')).toBeNull()

    delete switchWindow.__NHMS_E2E_CRASH_REGION__
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(regionFallbackIds()).toEqual([])
    expect(screen.getByTestId('m11-launcher-legend')).toBeInTheDocument()
  })

  it('the probes add no DOM: gate on and switch unset renders the same markup as no gate', async () => {
    form = installMobileFormMatchMedia(true)
    const plain = await renderOverview()
    fireEvent.click(screen.getByTestId('stub-click-river'))
    await screen.findByTestId('stub-river-panel')
    const plainMarkup = plain.container.innerHTML
    plain.unmount()

    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
    switchWindow.__NHMS_E2E_HOOKS__ = true
    const gated = await renderOverview()
    fireEvent.click(screen.getByTestId('stub-click-river'))
    await screen.findByTestId('stub-river-panel')

    expect(gated.container.innerHTML).toBe(plainMarkup)
  })
})
