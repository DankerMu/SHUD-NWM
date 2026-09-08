import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
/** IFS 自己声明的默认周期，刻意 ≠ 目录（GFS 专有）的 `default_cycle`。 */
const IFS_OWN_CYCLE = '2026-05-17T06:00:00Z'

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

function renderOverviewRouterAt(search: string) {
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], { initialEntries: [`/?${search}`] })
  render(<RouterProvider router={router} />)
  // MemoryRouter 不碰 window.location：URL 一律从 router 自己的 location 读。
  return (key: string) => new URLSearchParams(router.state.location.search).get(key)
}

function renderOverviewAt(search: string) {
  const param = renderOverviewRouterAt(search)
  return () => param('validTime')
}

/** 按 `query.source` 分叉的 cycles / valid-times：切源后活动周期必须真的换一份。 */
function mockApiWithSourceScopedCycles() {
  const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: MockOptions) => {
    calls.push({ path, query: options?.params?.query })
    if (path === VALID_TIMES_PATH) {
      // 后端对**未覆盖**的 (source, cycle) 返回 200 + 空列表（不是 4xx）——这正是
      // `'Layer has no valid times.'` 这句假文案的来源，也是本用例要证明「请求根本没发」的原因。
      const cycle = options?.params?.query?.cycle as string | undefined
      return success({ layer_id: 'discharge', valid_times: cycle === IFS_OWN_CYCLE ? IFS_VALID_TIMES : [] })
    }
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([])
    if (path === '/api/v1/layers') return success([dischargeLayer])
    if (path === '/api/v1/models') return success({ items: [], total: 0, limit: 200, offset: 0 })
    if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 })
    if (path === CYCLES_PATH) {
      const source = options?.params?.query?.source
      const cycleTime = source === 'ifs' ? IFS_OWN_CYCLE : DEFAULT_CYCLE
      return success({
        source,
        cycles: [{ cycle_time: cycleTime, valid_time_start: cycleTime, valid_time_end: cycleTime }],
        default_cycle: cycleTime,
      })
    }
    if (path === PRECIP_INDEX_PATH) {
      return success({
        source: 'ifs',
        cycle: IFS_OWN_CYCLE,
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
  return calls
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
    // #2014 的挂载接缝（round-1 finding G）：`OverviewMode → M11FullscreenMap → M11BottomControlBar`
    // 这条接线此前零断言 —— 忘传 `controlBar` prop 或忘渲染都会全绿。`deriveM11ControlBarModel`
    // 永不返回 null，故控制条从首帧起就在 DOM 里，这里同步断言、不用 waitFor。
    expect(screen.getByTestId('m11-bottom-control-bar')).toBeTruthy()

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

describe('OverviewPage source segment', () => {
  it('does not carry the previous source cycle into the newly selected source', async () => {
    // AC9 / 决策 15 的接缝半边：`handleQueryChange` 是 `{...state, ...patch}` 无跨字段重置，
    // 源分段若只发 `{ source }`，GFS 上选定的周期会原样存活 → `nationalDischargeActivePair`
    // 取 `query.cycle` 拼出 `(ifs, C_gfs)` → 后端 200 + 空 → C1 要消灭的那句假文案在**最常见
    // 路径**上复活。本用例走真页面 + 真路由 + 真 store，断言的是那条请求根本没被发出。
    const calls = mockApiWithSourceScopedCycles()
    const search = serializeM11QueryState({ ...defaultM11QueryState, cycle: DEFAULT_CYCLE })
    const param = renderOverviewRouterAt(search)

    await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
    await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
    // 前置条件：URL 上确实带着 GFS 的周期，且默认对走目录 metadata（此刻零 valid-times 请求）。
    expect(param('cycle')).toBe('2026-05-18T00:00:00.000Z')
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'IFS' }))
    })
    await waitFor(() => expect(param('source')).toBe('ifs'))
    await waitFor(() => expect(useOverviewDataStore.getState().cyclesBySource.ifs).toBeTruthy())
    await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
    await flushPendingEffects()

    const ifsValidTimeCycles = calls
      .filter((call) => call.path === VALID_TIMES_PATH && call.query?.source === 'ifs')
      .map((call) => call.query?.cycle)
    // 主 oracle：`(ifs, C_gfs)` 这条请求根本不该存在。
    expect(ifsValidTimeCycles).not.toContain(DEFAULT_CYCLE)
    expect([...new Set(ifsValidTimeCycles)]).toEqual([IFS_OWN_CYCLE])
    // 机理那半：URL 上的 GFS 周期被切源的 patch 清掉了（`{...state, ...patch}` 无跨字段重置）。
    expect(param('cycle')).toBeNull()
  })
})
