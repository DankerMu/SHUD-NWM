import { act, render, waitFor } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { pendingActiveCycleValidTimesDisabledReason } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'
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

const VALID_TIMES_PATH = '/api/v1/layers/{layer_id}/valid-times'
const PRECIP_INDEX_PATH = '/api/v1/precip/{source}/{cycle}/index'
const CYCLES_PATH = '/api/v1/layers/discharge/cycles'
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
// URL 带的 validTime 属于**默认源** gfs 的列表，故意不在下面 ifs 那份里：
// 闸门放开后校正必须真的改写它，用来证明这个 effect 是活的（不是「从未触发」冒充的绿）。
const SHARED_VALID_TIME = '2026-05-18T06:00:00.000Z'
const IFS_VALID_TIMES = ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z']

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

// 全国 discharge 目录：`default_source` 是 gfs，所以 `?source=ifs` 的活动对天然非默认，
// per-cycle 列表必须自己取 —— 这正是 pending 窗口打开的条件（无需 `?cycle=`）。
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
    valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z', SHARED_VALID_TIME.replace('.000Z', 'Z')],
    fallback_available: false,
    release_blocking: false,
  },
}

type MockOptions = { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }

/** 闸住 per-cycle valid-times：bootstrap 正常落定，pending 窗口在测试控制下保持打开。 */
function mockApiWithGatedValidTimes() {
  let release: () => void = () => undefined
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: MockOptions) => {
    if (path === VALID_TIMES_PATH) {
      await gate
      return success({ layer_id: 'discharge', valid_times: IFS_VALID_TIMES })
    }
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([])
    if (path === '/api/v1/layers') return success([dischargeLayer])
    if (path === '/api/v1/models') return success({ items: [], total: 0, limit: 200, offset: 0 })
    if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 })
    if (path === CYCLES_PATH) {
      return success({
        source: options?.params?.query?.source ?? 'ifs',
        cycles: [{ cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T03:00:00Z' }],
        default_cycle: DEFAULT_CYCLE,
      })
    }
    if (path === PRECIP_INDEX_PATH) {
      return success({
        source: 'ifs',
        cycle: DEFAULT_CYCLE,
        window_hours: 24,
        unit: 'mm',
        bounds: [73, 18, 135, 54],
        image_size: [1316, 800],
        legend: [],
        palette_version: 'v1',
        valid_times: IFS_VALID_TIMES,
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
  return { release: () => release() }
}

function renderOverviewAt(search: string) {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: [`/?${search}`] })
  render(<RouterProvider router={router} />)
  // MemoryRouter 不碰 window.location：URL 一律从 router 自己的 location 读。
  return () => new URLSearchParams(router.state.location.search).get('validTime')
}

function dischargeDisabledReason() {
  return useOverviewDataStore
    .getState()
    .overview?.layers.find((item) => item.layerId === 'discharge')?.disabledReason
}

async function flushPendingEffects() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  clearOverviewDataCache()
  useOverviewDataStore.setState({
    overview: null,
    basinDetail: null,
    mapBootstrapLoading: false,
    enrichmentLoading: false,
    basinLoading: false,
    bootstrapError: null,
    error: null,
    basinError: null,
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

describe('OverviewPage validTime auto-correction gate', () => {
  it('keeps a shared ?source=ifs&validTime= link intact while the active cycle list is unresolved', async () => {
    // spec frontend-mvt-layer-consumption「The active cycle's list is unresolved」：
    // 未定期间不得改写 URL 的 validTime。页面调用点用的必须是**全国包装**
    // `resolveM11NationalValidTimeCorrection`；换成裸的 `resolveM11ValidTimeCorrection`，
    // 未定态的空列表会被判成「该图层没有时次」，validTime 立刻被清成 null、分享链接作废。
    const { release } = mockApiWithGatedValidTimes()
    const search = serializeM11QueryState({ ...defaultM11QueryState, source: 'ifs', validTime: SHARED_VALID_TIME })
    const currentValidTime = renderOverviewAt(search)
    expect(currentValidTime()).toBe(SHARED_VALID_TIME)

    // bootstrap 已落定（校正 effect 的早退闸门 `mapBootstrapLoading` 已抬起），
    // 且活动 (ifs, default_cycle) 的列表确实还未定 —— 断言是在真实的 pending 窗口里做的。
    await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
    await waitFor(() => expect(dischargeDisabledReason()).toBe(pendingActiveCycleValidTimesDisabledReason))
    await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
    await flushPendingEffects()

    expect(dischargeDisabledReason()).toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(currentValidTime()).toBe(SHARED_VALID_TIME)

    // 闸门放开 → 列表落地 → 校正恢复：URL 里越界的 validTime 被换成该周期列表的首项。
    // 这一段证明上面的绿不是「effect 从未触发」造成的假阳性。
    release()
    await waitFor(() => expect(dischargeDisabledReason()).toBeNull())
    await waitFor(() => expect(currentValidTime()).toBe('2026-05-18T00:00:00.000Z'))
  })
})
