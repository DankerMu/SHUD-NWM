import { render, screen, waitFor, within } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 地图控件区的错误边界在两种形态下的落点（openspec mobile-responsive-display task 3.3）：
 * 仍是一个边界；移动形态下它在启动器列里、兜底块就地入流（不带桌面的绝对定位类），
 * 图例边界独立——图层组件抛错不带走图例启动器。
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

vi.mock('@/components/map/M11FloatingControls', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/map/M11FloatingControls')>()),
  M11FloatingLayerSwitcher: () => {
    throw new Error('layer switcher render failure under test')
  },
}))

const DESKTOP_BOUNDARY_CLASSES = ['absolute', 'left-4', 'top-4', 'z-[120]']

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
}

describe('OverviewPage map-controls error boundary by viewport form', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia>

  beforeEach(() => {
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer]) })
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
    // 边界会把捕获的错误打进日志；这里是预期内的抛错。
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    form?.restore()
    vi.restoreAllMocks()
  })

  it('mobile form: one in-flow fallback inside the launcher column, and the legend launcher survives', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()

    const column = screen.getByTestId('m11-launcher-column')
    const fallback = screen.getByTestId('region-error-map-controls')
    expect(screen.getAllByTestId('region-error-map-controls')).toHaveLength(1)
    expect(fallback.parentElement).toBe(column)
    const tokens = fallback.className.split(/\s+/)
    for (const token of DESKTOP_BOUNDARY_CLASSES) expect(tokens).not.toContain(token)
    // 一个边界覆盖图层与底图：底图启动器随它一起被兜底替换。
    expect(screen.queryByTestId('m11-launcher-layers')).toBeNull()
    expect(screen.queryByTestId('m11-launcher-basemap')).toBeNull()
    // 图例边界独立：图例启动器仍在列里、排在兜底块之后，图例自己没有进兜底。
    const legendLauncher = within(column).getByTestId('m11-launcher-legend')
    expect(screen.queryByTestId('region-error-legend')).toBeNull()
    expect(fallback.compareDocumentPosition(legendLauncher) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('desktop form: the fallback keeps its absolute desktop offset in the map region', async () => {
    form = installMobileFormMatchMedia(false)
    await renderOverview()

    const fallback = screen.getByTestId('region-error-map-controls')
    expect(fallback.className.split(/\s+/)).toEqual(expect.arrayContaining(DESKTOP_BOUNDARY_CLASSES))
    expect(fallback.parentElement).toBe(screen.getByTestId('m11-fullscreen-map'))
    expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
  })
})
