import { render, screen, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { client } from '@/api/client'
import { OverviewPage } from '@/pages/OverviewPage'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'

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

const CYCLES_PATH = '/api/v1/layers/discharge/cycles'
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'

const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

function success<T>(data: T) {
  return { data: { status: 'ok', data }, error: undefined }
}

const basin = {
  basin_id: 'basin-demo',
  basin_name: 'Demo Basin',
  basin_group: 'demo',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
}

const dischargeLayer = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: {
    layer_id: 'discharge',
    tile_format: 'mvt',
    maplibre_source_layer: 'hydro',
    min_zoom: 3,
    max_zoom: 10,
    url_template: '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
    required_placeholders: ['source', 'cycle', 'valid_time', 'z', 'x', 'y'],
    source_refs: {},
    default_source: 'gfs',
    default_cycle: DEFAULT_CYCLE,
    valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z'],
    fallback_available: false,
    release_blocking: false,
  },
}

type MockOptions = { params?: { query?: Record<string, unknown> } }

/**
 * 默认 `/`（gfs = 目录默认源）下 `/cycles` 回一个含 `null` 元素的数组、`default_cycle` 仍合法：
 * store 侧消费者全部 null 安全，唯一的渲染期消费者是控制条派生（`entry.cycle_time` 在 render 里抛），
 * 触达路径见 design D3。`?source=ifs` 无合法 default_cycle 时走 fail-closed、不抛，故不用它。
 */
function mockApiWithNullCycleEntry() {
  const cycleRequests: Array<unknown> = []
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: MockOptions) => {
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([])
    if (path === '/api/v1/layers') return success([dischargeLayer])
    if (path === '/api/v1/models') return success({ items: [], total: 0, limit: 200, offset: 0 })
    if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 })
    if (path === CYCLES_PATH) {
      cycleRequests.push(options?.params?.query?.source)
      return success({
        source: options?.params?.query?.source,
        cycles: [null, { cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T03:00:00Z' }],
        default_cycle: DEFAULT_CYCLE,
      })
    }
    if (path === '/api/v1/layers/{layer_id}/valid-times') {
      return success({ layer_id: 'discharge', valid_times: dischargeLayer.metadata.valid_times })
    }
    if (path === '/api/v1/precip/{source}/{cycle}/index') {
      return success({
        source: 'gfs',
        cycle: DEFAULT_CYCLE,
        window_hours: 24,
        unit: 'mm',
        bounds: [73, 18, 135, 54],
        image_size: [1316, 800],
        legend: [],
        palette_version: 'v1',
        valid_times: dischargeLayer.metadata.valid_times,
      })
    }
    if (path === '/api/v1/pipeline/status') {
      return success({
        cycle_time: DEFAULT_CYCLE,
        updated_at: '2026-05-18T00:30:00Z',
        job_counts: { succeeded: 1, running: 0, failed: 0, pending: 0 },
      })
    }
    throw new Error(`Unexpected GET ${path}`)
  }) as never)
  return cycleRequests
}

let consoleError: MockInstance<typeof console.error>

beforeEach(() => {
  vi.clearAllMocks()
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  clearOverviewDataCache()
  useOverviewDataStore.setState({
    overview: null,
    mapBootstrapLoading: false,
    enrichmentLoading: false,
    bootstrapError: null,
    error: null,
    cyclesBySource: {},
    validTimesByCycle: {},
    precipIndexByCycle: {},
  })
  useMonitoringStore.setState({ runtimeConfig: displayRuntimeConfig, runtimeConfigError: null })
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

describe('OverviewPage region error boundaries', () => {
  it('contains a control bar derivation throw to the control bar region on the default page', async () => {
    const cycleRequests = mockApiWithNullCycleEntry()
    const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: ['/'] })
    render(<RouterProvider router={router} />)

    const fallback = await screen.findByTestId('region-error-control-bar')
    expect(fallback).toHaveAttribute('role', 'alert')
    expect(fallback).toHaveTextContent('此区域加载失败')
    // 前置条件：确实是默认源 gfs 的 `/cycles` 载荷进了 store（不是别的路径抛的）。
    expect(cycleRequests).toContain('gfs')
    expect(useOverviewDataStore.getState().cyclesBySource.gfs?.status).toBe('available')

    expect(screen.queryByTestId('m11-bottom-control-bar')).toBeNull()
    expect(screen.getByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-map-surface')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-layer-switcher')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-basemap-switcher')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
    // 只有控制条这一个区域掉进 fallback。
    await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
    expect(screen.queryAllByTestId(/^region-error-/).map((node) => node.getAttribute('data-testid'))).toEqual([
      'region-error-control-bar',
    ])
    expect(
      consoleError.mock.calls.some((call) => call[0] === '[RegionErrorBoundary]' && call[1] === '起报时次与时间轴'),
    ).toBe(true)
  })
})
