import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, precipLayer, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 地图外壳的浮层展开值接线（openspec mobile-responsive-display task 3.2，design.md D5）：
 * 四个写入点（启动器、地图点击、Escape、形态切换）在页面组件层各走一遍；桌面形态不渲染启动器。
 * 形态用用例内可控的 `matchMedia` 桩造出，用后还原（全局桩恒为桌面）。
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

const DESKTOP_LEGEND_CLASSES = ['absolute', 'bottom-[7.5rem]', 'right-4', 'z-[120]']
const SEGMENT_ID = 'seg-under-test'

type ClickWindow = { __nhmsOrdinaryMapClickEvent?: unknown }

/** 经 MapLibre 桩把一次地图点击交给生产 `onClick`；`features` 为空即点在空白处。 */
function clickMap(features: unknown[]) {
  ;(window as unknown as ClickWindow).__nhmsOrdinaryMapClickEvent = {
    features,
    point: { x: 10, y: 20 },
    lngLat: { lng: 101, lat: 31 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  }
  fireEvent.click(screen.getByTestId('mock-maplibre-ordinary-click'))
}

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  await waitFor(() => expect(screen.queryByTestId('m11-overview-loading')).toBeNull())
  return router
}

function expandLegend() {
  fireEvent.click(screen.getByTestId('m11-launcher-legend'))
  expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
}

describe('OverviewPage legend launcher', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia>

  beforeEach(() => {
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer, precipLayer]) })
    installMaplibreStubMap({
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      project: vi.fn(() => ({ x: 0, y: 0 })),
      queryRenderedFeatures: vi.fn(() => []),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: (_event: string, callback: () => void) => {
        queueMicrotask(callback)
      },
    })
  })

  afterEach(() => {
    form?.restore()
    delete (window as unknown as ClickWindow).__nhmsOrdinaryMapClickEvent
  })

  it('desktop form renders the always-on legend at its desktop offset and no launcher', async () => {
    form = installMobileFormMatchMedia(false)
    await renderOverview()

    expect(screen.queryByTestId('m11-launcher-legend')).toBeNull()
    expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    const legend = screen.getByTestId('m11-floating-legend')
    expect(legend.className.split(/\s+/)).toEqual(expect.arrayContaining(DESKTOP_LEGEND_CLASSES))
    // 桌面图例直接挂在地图区下，没有多出来的包裹层。
    expect(legend.parentElement).toBe(screen.getByTestId('m11-fullscreen-map'))
  })

  it('mobile form starts collapsed with the launcher inside the launcher column of the map region', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()

    const column = within(screen.getByTestId('m11-fullscreen-map')).getByTestId('m11-launcher-column')
    expect(within(column).getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
  })

  it('mobile form toggles the legend panel from its launcher without touching the URL', async () => {
    form = installMobileFormMatchMedia(true)
    const router = await renderOverview()
    const search = router.state.location.search

    expandLegend()
    expect(screen.getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'true')
    // 面板内容与桌面图例同源：流量段带单位，降水段六级。
    const panel = screen.getByTestId('m11-floating-legend')
    expect(within(panel).getByTestId('m11-floating-legend-entries')).toHaveTextContent('m³/s')
    expect(within(panel).getAllByTestId('m11-floating-legend-precip-row')).toHaveLength(6)

    fireEvent.click(screen.getByTestId('m11-launcher-legend'))
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
    expect(screen.getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'false')
    expect(router.state.location.search).toBe(search)
  })

  it('mobile form collapses the panel on a map click that hits nothing', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    expandLegend()

    clickMap([])
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', '')
  })

  it('mobile form collapses the panel on a map click that hits a river segment, and the segment is still selected', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    expandLegend()

    clickMap([
      {
        layer: { id: 'm11-discharge-line-hit' },
        properties: {
          river_segment_id: SEGMENT_ID,
          segment_id: SEGMENT_ID,
          basin_version_id: 'bv-unmapped',
          river_network_version_id: 'rnv-under-test',
        },
        geometry: { type: 'LineString', coordinates: [[101, 31], [101.1, 31.1]] },
      },
    ])
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
    // 要素自身的既有行为照常发生：河段被选中。
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', SEGMENT_ID)
  })

  it('mobile form collapses the panel on Escape', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    expandLegend()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
    expect(screen.getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'false')
  })

  it('leaving mobile form restores the desktop legend, and re-entering starts collapsed', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    expandLegend()

    act(() => form.setMobile(false))
    expect(screen.queryByTestId('m11-launcher-legend')).toBeNull()
    expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    const legend = screen.getByTestId('m11-floating-legend')
    expect(legend.className.split(/\s+/)).toEqual(expect.arrayContaining(DESKTOP_LEGEND_CLASSES))

    act(() => form.setMobile(true))
    expect(screen.getByTestId('m11-launcher-legend')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('m11-floating-legend')).toBeNull()
  })

  it('desktop form ignores map clicks and Escape: the always-on legend stays', async () => {
    form = installMobileFormMatchMedia(false)
    await renderOverview()

    clickMap([])
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
  })
})
