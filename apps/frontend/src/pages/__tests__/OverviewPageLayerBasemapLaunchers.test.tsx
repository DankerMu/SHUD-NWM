import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OverviewPage } from '@/pages/OverviewPage'
import { useAuthStore } from '@/stores/auth'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'
import { layer, mockApi, precipLayer, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

/**
 * 图层 / 底图启动器接入地图外壳的展开值（openspec mobile-responsive-display task 3.3，design.md D5）：
 * 三个启动器同处一列、互斥展开；面板里的开关与桌面改同一个 URL query；桌面形态渲染路径不变。
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

const PANELS = {
  layers: { launcher: 'm11-launcher-layers', panel: 'm11-floating-layer-switcher' },
  basemap: { launcher: 'm11-launcher-basemap', panel: 'm11-floating-basemap-switcher' },
  legend: { launcher: 'm11-launcher-legend', panel: 'm11-floating-legend' },
} as const
type PanelName = keyof typeof PANELS
const PANEL_NAMES = Object.keys(PANELS) as PanelName[]

type ClickWindow = { __nhmsOrdinaryMapClickEvent?: unknown }

function clickBlankMap() {
  ;(window as unknown as ClickWindow).__nhmsOrdinaryMapClickEvent = {
    features: [],
    point: { x: 10, y: 20 },
    lngLat: { lng: 101, lat: 31 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  }
  fireEvent.click(screen.getByTestId('mock-maplibre-ordinary-click'))
}

async function renderOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  const { unmount } = render(<RouterProvider router={router} />)
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  await waitFor(() => expect(screen.queryByTestId('m11-overview-loading')).toBeNull())
  return Object.assign(router, { unmount })
}

/** `name` 的面板在、其启动器报告展开；另外两个面板不在、启动器报告收起。`null` = 全部收起。 */
function expectOnlyExpanded(name: PanelName | null) {
  for (const candidate of PANEL_NAMES) {
    const { launcher, panel } = PANELS[candidate]
    expect(screen.getByTestId(launcher)).toHaveAttribute('aria-expanded', candidate === name ? 'true' : 'false')
    expect(screen.queryAllByTestId(panel)).toHaveLength(candidate === name ? 1 : 0)
  }
}

function tapLauncher(name: PanelName) {
  fireEvent.click(screen.getByTestId(PANELS[name].launcher))
}

describe('OverviewPage layer and basemap launchers', () => {
  let form: ReturnType<typeof installMobileFormMatchMedia>
  const initialRole = useAuthStore.getState().role

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
    useAuthStore.setState({ role: initialRole })
    delete (window as unknown as ClickWindow).__nhmsOrdinaryMapClickEvent
  })

  it('desktop form renders the three always-on panels directly in the map region and no launcher', async () => {
    form = installMobileFormMatchMedia(false)
    await renderOverview()

    const region = screen.getByTestId('m11-fullscreen-map')
    expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    for (const name of PANEL_NAMES) {
      expect(screen.queryByTestId(PANELS[name].launcher)).toBeNull()
      // 没有多出来的包裹层。
      expect(screen.getByTestId(PANELS[name].panel).parentElement).toBe(region)
    }
    expect(screen.getByTestId('m11-floating-layer-switcher').className.split(/\s+/)).toEqual(
      expect.arrayContaining(['absolute', 'left-4', 'top-4', 'z-[120]']),
    )
    expect(screen.getByTestId('m11-floating-basemap-switcher').className.split(/\s+/)).toEqual(
      expect.arrayContaining(['absolute', 'right-16', 'top-4', 'z-[120]']),
    )
  })

  it('mobile form starts collapsed with the three launchers in column order layers / basemap / legend', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()

    const column = within(screen.getByTestId('m11-fullscreen-map')).getByTestId('m11-launcher-column')
    const order = Array.from(column.querySelectorAll('[data-testid^="m11-launcher-"]')).map((element) =>
      element.getAttribute('data-testid'),
    )
    expect(order).toEqual(['m11-launcher-layers', 'm11-launcher-basemap', 'm11-launcher-legend'])
    // 启动器是列的直接子项（边界非错误态不加包裹）。
    for (const name of PANEL_NAMES) expect(screen.getByTestId(PANELS[name].launcher).parentElement).toBe(column)
    expectOnlyExpanded(null)
  })

  it('mobile form expands one panel at a time, each anchored inside the launcher column', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    const column = screen.getByTestId('m11-launcher-column')

    for (const name of ['legend', 'layers', 'basemap', 'legend', 'basemap', 'layers'] as const) {
      tapLauncher(name)
      expectOnlyExpanded(name)
      expect(screen.getByTestId(PANELS[name].panel).parentElement).toBe(column)
    }
    tapLauncher('layers')
    expectOnlyExpanded(null)
  })

  it('mobile form collapses the layer panel on a map click and on Escape', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()

    tapLauncher('layers')
    expectOnlyExpanded('layers')
    clickBlankMap()
    expectOnlyExpanded(null)

    tapLauncher('basemap')
    expectOnlyExpanded('basemap')
    fireEvent.keyDown(document, { key: 'Escape' })
    expectOnlyExpanded(null)
  })

  it('mobile panel toggles change the URL query exactly as the desktop panels do, and keep the panel expanded', async () => {
    form = installMobileFormMatchMedia(false)
    const desktopRouter = await renderOverview()
    const initial = desktopRouter.state.location.search
    fireEvent.click(within(screen.getByTestId('m11-floating-layer-switcher')).getByRole('button', { name: /气象代站/ }))
    const desktopAfterStations = desktopRouter.state.location.search
    fireEvent.click(within(screen.getByTestId('m11-floating-basemap-switcher')).getByRole('button', { name: '卫星底图' }))
    const desktopAfterBasemap = desktopRouter.state.location.search
    expect(desktopAfterStations).not.toBe(initial)
    expect(desktopAfterBasemap).not.toBe(desktopAfterStations)
    form.restore()
    desktopRouter.unmount()
    resetOverviewDataTestState()
    mockApi({ '/api/v1/layers': () => success([layer, precipLayer]) })

    form = installMobileFormMatchMedia(true)
    const mobileRouter = await renderOverview()
    expect(mobileRouter.state.location.search).toBe(initial)
    tapLauncher('layers')
    fireEvent.click(within(screen.getByTestId('m11-floating-layer-switcher')).getByRole('button', { name: /气象代站/ }))
    expect(mobileRouter.state.location.search).toBe(desktopAfterStations)
    // 面板内操作不是地图点击：面板保持展开。
    expectOnlyExpanded('layers')

    tapLauncher('basemap')
    fireEvent.click(within(screen.getByTestId('m11-floating-basemap-switcher')).getByRole('button', { name: '卫星底图' }))
    expect(mobileRouter.state.location.search).toBe(desktopAfterBasemap)
    expectOnlyExpanded('basemap')
  })

  it('leaving mobile form restores the three desktop panels, and re-entering starts collapsed', async () => {
    form = installMobileFormMatchMedia(true)
    await renderOverview()
    tapLauncher('layers')
    expectOnlyExpanded('layers')

    act(() => form.setMobile(false))
    expect(screen.queryByTestId('m11-launcher-column')).toBeNull()
    for (const name of PANEL_NAMES) {
      expect(screen.queryByTestId(PANELS[name].launcher)).toBeNull()
      expect(screen.getByTestId(PANELS[name].panel)).toBeInTheDocument()
    }

    act(() => form.setMobile(true))
    expectOnlyExpanded(null)
  })

  it('operator role in mobile form: the ops link joins the launcher column as an in-flow item ordered last', async () => {
    useAuthStore.setState({ role: 'operator' })
    form = installMobileFormMatchMedia(true)
    await renderOverview()

    const column = screen.getByTestId('m11-launcher-column')
    const link = screen.getByTestId('m11-ops-link')
    expect(link.parentElement).toBe(column)
    const tokens = link.className.split(/\s+/)
    expect(tokens).toContain('order-last')
    expect(tokens).not.toContain('absolute')

    act(() => form.setMobile(false))
    const desktopTokens = screen.getByTestId('m11-ops-link').className.split(/\s+/)
    expect(desktopTokens).toEqual(expect.arrayContaining(['absolute', 'right-4', 'top-28', 'z-[120]']))
    expect(desktopTokens).not.toContain('order-last')
  })
})
