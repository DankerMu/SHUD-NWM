// #2129 裁决 B（design D2/D6）：变形载荷在默认 `/`（gfs）上降级为显式「数据异常」，
// 不再抛进渲染、不再与请求失败共用文案、也不再让地图 bootstrap 无限加载。
import { render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import { CYCLES_PATH, DEFAULT_CYCLE, basin, mockApi, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

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

const validCycleEntry = { cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T06:00:00Z' }

let consoleError: MockInstance<typeof console.error>

beforeEach(() => {
  resetOverviewDataTestState()
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
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
  consoleError.mockRestore()
})

function renderDefaultOverview() {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
  render(<RouterProvider router={router} />)
}

async function enrichmentSettled() {
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
}

function boundaryFallbacks() {
  return [...screen.queryAllByTestId(/^region-error-/), ...screen.queryAllByTestId('route-error-fallback')].map((node) =>
    node.getAttribute('data-testid'),
  )
}

describe('OverviewPage data anomaly notice (E7)', () => {
  const malformedCycles = [
    { label: 'a null entry', cycles: [null, validCycleEntry] },
    { label: 'a string array', cycles: [DEFAULT_CYCLE] },
  ]

  it.each(malformedCycles)('shows 数据异常 naming 起报时次 for gfs cycles with $label, keeping every region rendered', async ({ cycles }) => {
    mockApi({ [CYCLES_PATH]: () => success({ source: 'gfs', cycles, default_cycle: DEFAULT_CYCLE }) })
    renderDefaultOverview()
    await enrichmentSettled()
    await waitFor(() => expect(useOverviewDataStore.getState().cyclesBySource.gfs).toBeDefined())

    expect(boundaryFallbacks()).toEqual([])
    const notice = await screen.findByTestId('m11-data-anomaly', undefined, { timeout: 2000 })
    expect(notice).toHaveTextContent('数据异常：起报时次返回格式不符，已按不可用处理')
    expect(screen.getByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
    expect(screen.getByTestId('m11-bottom-control-bar')).toBeInTheDocument()
  })
})

describe('OverviewPage bootstrap data anomaly (E8)', () => {
  it('names 数据异常 in the empty/error notice when basins are not an array', async () => {
    mockApi({ '/api/v1/basins': () => success({ items: [basin] }) })
    renderDefaultOverview()
    await enrichmentSettled()

    const notice = await screen.findByTestId('m11-overview-empty', undefined, { timeout: 2000 })
    expect(notice).toHaveTextContent('数据异常')
    expect(screen.queryByTestId('m11-overview-loading')).toBeNull()
  })

  it('settles the loading notice and names 数据异常 when layers contain a null element', async () => {
    mockApi({ '/api/v1/layers': () => success([null]) })
    renderDefaultOverview()
    await enrichmentSettled()

    // 有界等待：改动前 bootstrap 快照与 bootstrapError 都不写，加载提示会一直挂着。
    await waitFor(() => expect(screen.queryByTestId('m11-overview-loading')).toBeNull(), { timeout: 2000 })
    expect(screen.getByTestId('m11-overview-empty')).toHaveTextContent('数据异常')
  })
})

describe('OverviewPage runs data anomaly (E5 page)', () => {
  it('surfaces a runs shape error as the data anomaly notice while basins are non-empty', async () => {
    mockApi({ '/api/v1/runs': () => success({ items: 'x', total: 0, limit: 20, offset: 0 }) })
    renderDefaultOverview()
    await enrichmentSettled()

    const notice = await screen.findByTestId('m11-data-anomaly', undefined, { timeout: 2000 })
    expect(notice).toHaveTextContent('运行记录')
    expect(screen.queryByTestId('m11-overview-empty')).toBeNull()
    expect(useOverviewDataStore.getState().overview?.basins.length ?? 0).toBeGreaterThan(0)
  })
})
