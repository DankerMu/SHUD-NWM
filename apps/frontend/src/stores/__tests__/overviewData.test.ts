import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  pendingActiveCycleValidTimesDisabledReason,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import { resolveM11NationalValidTimeCorrection, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'

const CYCLES_PATH = '/api/v1/layers/discharge/cycles'
const VALID_TIMES_PATH = '/api/v1/layers/{layer_id}/valid-times'
const PRECIP_INDEX_PATH = '/api/v1/precip/{source}/{cycle}/index'
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
const OTHER_CYCLE = '2026-05-17T12:00:00Z'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

const query: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
  segmentId: 'river-001',
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

const basinVersion = {
  basin_version_id: 'bv-001',
  basin_id: 'basin-demo',
  version_label: 'v1',
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
  geom: { type: 'MultiPolygon', coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31], [100, 30]]]] },
}

const model = {
  model_id: 'model-001',
  model_name: 'Demo SHUD',
  basin_id: 'basin-demo',
  basin_name: 'Demo Basin',
  basin_version_id: 'bv-001',
  river_network_version_id: 'rn-001',
  mesh_version_id: 'mesh-001',
  calibration_version_id: null,
  segment_count: 1,
  mesh_uri: null,
  mesh_checksum: null,
  shud_code_version: 'v1',
  rshud_code_version: null,
  autoshud_code_version: null,
  active_flag: true,
  container_image: null,
  model_package_uri: null,
  package_checksum: null,
  manifest_uri: null,
  source_inventory_checksum: null,
  basin_slug: 'basin-demo',
  shud_input_name: null,
  source_path: null,
  resolved_source_path: null,
  source_uri: null,
  source_is_symlink: null,
  resource_profile: {},
  created_at: '2026-05-02T00:00:00Z',
}

const run = {
  run_id: 'run-001',
  run_type: 'forecast',
  scenario_id: 'forecast_gfs_deterministic',
  model_id: 'model-001',
  basin_version_id: 'bv-001',
  river_network_version_id: 'rn-001',
  forcing_version_id: null,
  init_state_id: null,
  source_id: 'GFS',
  cycle_time: '2026-05-18T00:00:00Z',
  status: 'published',
  slurm_job_id: null,
  start_time: '2026-05-18T00:00:00Z',
  end_time: '2026-05-25T00:00:00Z',
  run_manifest_uri: null,
  output_uri: null,
  log_uri: null,
  error_code: null,
  error_message: null,
  created_at: '2026-05-18T00:05:00Z',
  updated_at: '2026-05-18T00:10:00Z',
}

// 全国 source/cycle 目录形状（spec mvt-tile-contract）：默认周期的 valid_times 直接进 metadata，
// 控制条与时间轴首屏即可从中渲染，不需要任何 valid-times 请求。
const nationalDischargeMetadata = {
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
  valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z'],
  fallback_available: false,
  release_blocking: false,
}

const layer = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: nationalDischargeMetadata,
}

const precipIndex = {
  source: 'gfs',
  cycle: DEFAULT_CYCLE,
  window_hours: 24,
  unit: 'mm',
  bounds: [73, 18, 135, 54],
  image_size: [1316, 800],
  legend: [],
  palette_version: 'v1',
  valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z'],
}

function apiError(code: string) {
  return { data: undefined, error: { request_id: 'req-1', status: 'error', error: { code, message: code } } }
}

const riverSegments = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'river-001',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-001',
        name: 'Demo River',
        stream_order: 2,
        length_m: 1000,
        value: 12,
        unit: 'm3/s',
        valid_time: '2026-05-18T06:00:00Z',
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
  ],
  total: 1,
  feature_total: 1,
  limit: 1,
  offset: 0,
}

type MockOptions = { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
type MockCall = {
  path: string
  query?: Record<string, unknown>
  pathParams?: Record<string, unknown>
  /** 调用发生时的 mapBootstrapLoading 读数：用来钉「enrichment 在 bootstrap 落定之后才发出」。 */
  mapBootstrapLoading: boolean
}

function mockApi(overrides: Record<string, (options: MockOptions) => unknown> = {}) {
  const calls: MockCall[] = []
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: MockOptions) => {
    calls.push({
      path,
      query: options?.params?.query,
      pathParams: options?.params?.path,
      mapBootstrapLoading: useOverviewDataStore.getState().mapBootstrapLoading,
    })
    const override = overrides[path]
    if (override) return override(options ?? {})
    if (path === CYCLES_PATH) {
      return success({
        source: options?.params?.query?.source ?? 'gfs',
        cycles: [
          { cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T06:00:00Z' },
          { cycle_time: OTHER_CYCLE, valid_time_start: OTHER_CYCLE, valid_time_end: '2026-05-17T18:00:00Z' },
        ],
        default_cycle: DEFAULT_CYCLE,
      })
    }
    if (path === PRECIP_INDEX_PATH) return success(precipIndex)
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion])
    if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 })
    if (path === '/api/v1/models/{model_id}') return success(model)
    if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: options?.params?.query?.offset ?? 0 })
    if (path === '/api/v1/layers') return success([layer])
    if (path === VALID_TIMES_PATH) {
      const cycle = options?.params?.query?.cycle as string | undefined
      return success({
        layer_id: 'discharge',
        valid_times: cycle ? [cycle, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'] : ['2026-05-18T06:00:00Z'],
      })
    }
    if (path === '/api/v1/pipeline/status') {
      return success({
        cycle_time: '2026-05-18T00:00:00Z',
        updated_at: '2026-05-18T00:30:00Z',
        job_counts: { succeeded: 1, running: 0, failed: 0, pending: 0 },
      })
    }
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(riverSegments)
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
      return success({
        river_segment_id: 'river-001',
        river_network_version_id: 'rn-001',
        segment_order: 1,
        downstream_segment_id: null,
        length_m: 1000,
        geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
      return success({
        segment_id: 'river-001',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source_id: 'GFS',
            segment_role: 'future_7_days',
            points: [['2026-05-18T06:00:00Z', 12]],
          },
        ],
      })
    }
    if (path === '/api/v1/lineage/river-point') return success({ status: 'available', records: [] })
    throw new Error(`Unexpected GET ${path}`)
  }) as never)
  return calls
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
})

describe('overview data store discharge loading', () => {
  it('loads overview runs without product-specific readiness filters', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.summary.freshness.runId).toBe('run-001')
    expect(calls.filter((call) => call.path === '/api/v1/basins').map((call) => call.query)).toEqual([
      { limit: 200, offset: 0, has_display_product: true },
    ])
    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    const allowedRunQueryKeys = new Set(['basin_id', 'source', 'cycle_time', 'status', 'limit', 'offset'])
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.status === 'published')).toBe(true)
    expect(runCalls.every((call) => Object.keys(call.query ?? {}).every((key) => allowedRunQueryKeys.has(key)))).toBe(true)
  })

  it('consumes the default cycle from catalog metadata without any valid-times request', async () => {
    // spec frontend-mvt-layer-consumption「Metadata carries valid_times for the default cycle」
    const calls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    const discharge = useOverviewDataStore.getState().overview?.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    // 默认位置是 lead 0 = 活动列表首项。
    expect(discharge?.currentValidTime).toBe('2026-05-18T00:00:00.000Z')
  })

  it('treats a millisecond-spelled default cycle in the URL as the default cycle', async () => {
    // fixture 决策 3 的第四处用途：`metadata.default_cycle` 是秒精度而 URL 是毫秒形，
    // 朴素 `===` 会把默认周期误判成非默认并多发一次 valid-times。
    const calls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-18T00:00:00.000Z' })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
  })

  it('fetches a non-default cycle list once and reuses it across loadOverview calls', async () => {
    // spec「Non-default cycle fetches its own list」+ 缓存生命周期：A→B→A 共 2 次而不是 3 次。
    const calls = mockApi()
    const cycleA = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: null }
    const cycleB = { ...query, cycle: '2026-05-17T18:00:00.000Z', validTime: null }
    const validTimesCalls = () => calls.filter((call) => call.path === VALID_TIMES_PATH)

    await useOverviewDataStore.getState().loadOverview(cycleA)

    expect(validTimesCalls()).toHaveLength(1)
    expect(validTimesCalls()[0].query).toEqual({ source: 'gfs', cycle: OTHER_CYCLE })
    expect(validTimesCalls()[0].mapBootstrapLoading).toBe(false)

    const stored = useOverviewDataStore.getState()
    // 缓存键是 `(source, cycle)`，周期按秒精度归一（URL 里是毫秒形 `…T12:00:00.000Z`）。
    expect(Object.keys(stored.validTimesByCycle)).toEqual([`gfs|${OTHER_CYCLE}`])
    expect(Object.values(stored.validTimesByCycle)[0]).toEqual({
      status: 'available',
      validTimes: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
    })
    // LayerState 本身必须是活动 (source, cycle) 的列表，而不是目录里默认周期的列表。
    const discharge = stored.overview?.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    // overlay 用这份 per-cycle 列表解析（决策 4 的端到端证明）。
    const overlay = buildM11RegisteredOverlay(cycleA, stored.overview?.layers ?? [])
    expect(decodeURIComponent(new URL(overlay?.source.tiles[0] as string, 'http://localhost').pathname)).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-17T12:00:00Z/q_down/2026-05-17T12:00:00Z/{z}/{x}/{y}.pbf',
    )

    await useOverviewDataStore.getState().loadOverview(cycleB)
    expect(validTimesCalls()).toHaveLength(2)

    await useOverviewDataStore.getState().loadOverview(cycleA)
    expect(validTimesCalls()).toHaveLength(2)
  })

  it('is fail-closed when the catalog advertises no default cycle', async () => {
    // spec map-layer-timeline-controls「Cycle selector is fail-closed」+ frontend-mvt-layer-consumption
    // 「Discharge with an empty list is fail-closed, not time-less」。
    const failClosedLayer = {
      ...layer,
      metadata: { ...nationalDischargeMetadata, default_cycle: null, valid_times: [] },
    }
    const calls = mockApi({ '/api/v1/layers': () => success([failClosedLayer]) })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-17T12:00:00.000Z' })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(calls.filter((call) => call.path === PRECIP_INDEX_PATH)).toHaveLength(0)
    expect(JSON.stringify(calls)).not.toContain('{cycle}')

    const layers = useOverviewDataStore.getState().overview?.layers ?? []
    const discharge = layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).toContain('No cycle covers every basin')
    expect(buildM11RegisteredOverlay({ ...query, cycle: '2026-05-17T12:00:00.000Z' }, layers)).toBeNull()
  })

  it('issues cycles and precip index only after mapBootstrapLoading settles and never fails bootstrap', async () => {
    // spec overview-data-contracts ADDED「Cycles and precipitation index requests stay off the
    // bootstrap critical path」+「Enrichment failure of cycles or precip index does not block the map」
    const calls = mockApi({
      [CYCLES_PATH]: () => {
        throw new Error('cycles down')
      },
      [PRECIP_INDEX_PATH]: () => {
        throw new Error('precip down')
      },
    })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })

    // 对照组：关键路径的两个请求确实是在 mapBootstrapLoading === true 时发出的，
    // 所以下面 enrichment 的 false 读数是真实的先后顺序，不是「这个标志从没被置 true」。
    expect(calls[0].mapBootstrapLoading).toBe(true)
    expect(calls.filter((call) => call.path === '/api/v1/basins')[0].mapBootstrapLoading).toBe(true)
    expect(calls.filter((call) => call.path === '/api/v1/layers')[0].mapBootstrapLoading).toBe(true)

    const enrichment = calls.filter((call) => call.path === CYCLES_PATH || call.path === PRECIP_INDEX_PATH)
    expect(enrichment).toHaveLength(2)
    expect(enrichment.every((call) => call.mapBootstrapLoading === false)).toBe(true)

    const state = useOverviewDataStore.getState()
    expect(state.bootstrapError).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.overview?.bootstrap).not.toBeNull()
    expect(state.cyclesBySource).toEqual({ gfs: { status: 'error' } })
    expect(state.precipIndexByCycle).toEqual({ [`gfs|${DEFAULT_CYCLE}`]: { status: 'error' } })
    // 地图仍可注册叠加层（默认周期的 metadata 列表照旧可用）。
    expect(buildM11RegisteredOverlay({ ...query, cycle: null }, state.overview?.layers ?? [])).not.toBeNull()
  })

  // 三条 layer-time enrichment 通路各自的 `isCurrentRequest()` 守卫都必须真的挡住迟到写入。
  // 一律用**非默认周期**加载：`cycle: null` 时活动对是默认对，per-cycle valid-times 分支
  // 结构上根本进不去，只能证明其中一条通路。
  const lateEnrichmentPaths = [
    { name: 'cycles', path: CYCLES_PATH, payload: () => success({ source: 'gfs', cycles: [], default_cycle: null }) },
    {
      name: 'per-cycle valid times',
      path: VALID_TIMES_PATH,
      payload: () => success({ layer_id: 'discharge', valid_times: [OTHER_CYCLE, '2026-05-17T18:00:00Z'] }),
    },
    { name: 'precip index', path: PRECIP_INDEX_PATH, payload: () => success(precipIndex) },
  ] as const

  it.each(lateEnrichmentPaths)(
    'discards $name results that arrive after overviewRequestNonce advanced',
    async ({ path, payload }) => {
      let release: () => void = () => undefined
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      const calls = mockApi({
        [path]: async () => {
          await gate
          return payload()
        },
      })

      const load = useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-17T12:00:00.000Z' })
      await vi.waitFor(() => expect(calls.some((call) => call.path === path)).toBe(true))
      // 新一轮请求已开始（nonce 递增）：迟到的结果一律不写入 store。
      clearOverviewDataCache()
      // bump **之后**取参照：bump 之前 enrichment 的最终 set 可能尚未落地，比对会变成竞态。
      const layersAfterBump = useOverviewDataStore.getState().overview?.layers
      release()
      await load

      const state = useOverviewDataStore.getState()
      expect(state.cyclesBySource).toEqual({})
      expect(state.validTimesByCycle).toEqual({})
      expect(state.precipIndexByCycle).toEqual({})
      // valid-times 的写入口还会就地重算 layers：迟到写入若漏守卫，这里的引用会被换掉。
      expect(state.overview?.layers).toBe(layersAfterBump)
    },
  )

  it('keeps the discharge layer disabled while the non-default cycle list is still in flight', async () => {
    // cand-01：列表未到时**不得**回落到目录里默认周期的 metadata.valid_times，
    // 否则图层报 available 并拼出跨周期瓦片 URL。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [VALID_TIMES_PATH]: async () => {
        await gate
        return success({
          layer_id: 'discharge',
          valid_times: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
        })
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const load = useOverviewDataStore.getState().loadOverview(cycleQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === VALID_TIMES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })

    const pendingState = useOverviewDataStore.getState()
    const pendingLayers = pendingState.overview?.layers ?? []
    const pendingDischarge = pendingLayers.find((item) => item.layerId === 'discharge')
    // 「尚未取回」= 记录缺席。
    expect(pendingState.validTimesByCycle).toEqual({})
    expect(pendingDischarge?.available).toBe(false)
    expect(pendingDischarge?.disabledReason).toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(pendingDischarge?.validTimes).toEqual([])
    // 尤其**不是**默认周期那份列表。
    expect(pendingDischarge?.validTimes).not.toContain('2026-05-18T00:00:00.000Z')
    // 零瓦片请求：overlay 注册不出来。
    expect(buildM11RegisteredOverlay(cycleQuery, pendingLayers)).toBeNull()
    // 校正闸门：裸函数会把 URL 里的 validTime 清成 null，全国包装函数在未定期间不校正。
    expect(resolveM11ValidTimeCorrection(cycleQuery, pendingLayers)).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, pendingLayers)).toBeUndefined()

    release()
    await load

    // pending → available 的转移必须真的渲染出来（错误/成功两条终态都就地重算 layers）。
    const settledLayers = useOverviewDataStore.getState().overview?.layers ?? []
    const settledDischarge = settledLayers.find((item) => item.layerId === 'discharge')
    expect(settledDischarge?.available).toBe(true)
    expect(settledDischarge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    expect(buildM11RegisteredOverlay(cycleQuery, settledLayers)).not.toBeNull()
    // 闸门只在未定期间挡；列表落地后包装函数照常委派给裸函数：越界的 validTime 被校正回
    // 该图层的 currentValidTime（此处即活动列表里由 cycleQuery.validTime 命中的那项）。
    expect(
      resolveM11NationalValidTimeCorrection({ ...cycleQuery, validTime: '2026-05-19T00:00:00.000Z' }, settledLayers),
    ).toBe(settledDischarge?.currentValidTime)
    expect(settledDischarge?.currentValidTime).toBe('2026-05-17T15:00:00.000Z')
  })

  it('degrades to a distinct error state when the non-default cycle list rejects', async () => {
    // cand-01 的第二条终态：reject 必须写终态并重算 layers，不能永久停在 pending 文案上；
    // 且这是 scoped 降级，不是 bootstrap 失败。
    // 闸门与上面的成功用例同构：不闸住 reject，阶段 2 自己那次 buildLayerStates 就能满足全部断言，
    // 删掉 `writeValidTimes` 里的就地重算也照样绿（R2-01）。闸门把 reject 推到阶段 2 落定**之后**，
    // 于是「文案是 error」+「layers 引用被换掉」两条都只能由就地重算满足。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [VALID_TIMES_PATH]: async () => {
        await gate
        throw new Error('valid-times down')
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const load = useOverviewDataStore.getState().loadOverview(cycleQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === VALID_TIMES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })
    // 阶段 2 已落定：此刻的 layers 是「reject 之前」的引用，终态必须在它之上原地重算。
    const layersBeforeReject = useOverviewDataStore.getState().overview?.layers
    expect(
      (layersBeforeReject ?? []).find((item) => item.layerId === 'discharge')?.disabledReason,
    ).toBe(pendingActiveCycleValidTimesDisabledReason)

    release()
    await load

    const state = useOverviewDataStore.getState()
    expect(state.validTimesByCycle).toEqual({ [`gfs|${OTHER_CYCLE}`]: { status: 'error' } })
    // pending → error 的转移必须真的渲染出来：只写 record 不重算，这里的引用不会变。
    expect(state.overview?.layers).not.toBe(layersBeforeReject)
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(discharge?.validTimes).toEqual([])
    expect(buildM11RegisteredOverlay(cycleQuery, state.overview?.layers ?? [])).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, state.overview?.layers ?? [])).toBeUndefined()
    // scoped 降级：既不是 bootstrap 失败，也不进 enrichment 的 partial error。
    expect(state.bootstrapError).toBeNull()
    expect(state.error).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
  })

  it('resolves the non-default cycle to the error state when bootstrap failure skips the layer-time chain', async () => {
    // spec frontend-mvt-layer-consumption「The active cycle's list is unresolved」第三种未定情形：
    // bootstrap 失败 → 阶段 3 直接 return，一条 per-cycle valid-times 请求都不会发；而阶段 2 仍用
    // run-scoped 目录（default_cycle 非空）构造 layer 状态，记录缺席会派生出 pending。
    // 「还在加载」是谎报：既无请求在途，也不会再有终态覆盖它 → 必须与 reject 同文案落到终态。
    const calls = mockApi({
      '/api/v1/basins': () => {
        throw new Error('basins down')
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    await useOverviewDataStore.getState().loadOverview(cycleQuery)

    const state = useOverviewDataStore.getState()
    // (i) 这一轮确实一条 per-cycle 请求都没发，且没有任何 per-cycle 记录被写入。
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(state.validTimesByCycle).toEqual({})
    // bootstrap 确实失败了（否则本用例根本没进那条 skip 分支）。
    expect(state.bootstrapError).not.toBeNull()
    expect(state.overview?.bootstrap).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.enrichmentLoading).toBe(false)
    // (ii) 终态而非 pending：文案与 reject 臂一致（"could not be loaded"），且仍与其余两条禁用文案可分。
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(discharge?.available).toBe(false)
    // 未定态照旧不回落到默认周期的 metadata 列表：零瓦片、URL 的 validTime 不被改写。
    expect(discharge?.validTimes).toEqual([])
    expect(buildM11RegisteredOverlay(cycleQuery, state.overview?.layers ?? [])).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, state.overview?.layers ?? [])).toBeUndefined()
  })

  it('clears the three layer-time records together with the HTTP cache', async () => {
    // tasks.md：三个缓存与既有 `cache` 同寿，由 `clearOverviewDataCache()` / `clearCache()` 清除。
    useOverviewDataStore.setState({
      cyclesBySource: { gfs: { status: 'error' } },
      validTimesByCycle: { [`gfs|${OTHER_CYCLE}`]: { status: 'available', validTimes: [OTHER_CYCLE] } },
      precipIndexByCycle: { [`gfs|${DEFAULT_CYCLE}`]: { status: 'error' } },
    })

    useOverviewDataStore.getState().clearCache()

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource).toEqual({})
    expect(state.validTimesByCycle).toEqual({})
    expect(state.precipIndexByCycle).toEqual({})
  })

  it('keeps a not-mirrored cycle distinguishable from any other precip index failure', async () => {
    // fixture 决策 7：`PRECIP_CYCLE_NOT_MIRRORED` 与其余失败是两个可区分状态（I11 据此出两条文案）。
    const key = `gfs|${DEFAULT_CYCLE}`
    mockApi({ [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED') })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    expect(useOverviewDataStore.getState().precipIndexByCycle).toEqual({ [key]: { status: 'not_mirrored' } })

    clearOverviewDataCache()
    mockApi({ [PRECIP_INDEX_PATH]: () => apiError('PRECIP_WINDOW_INCOMPLETE') })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    expect(useOverviewDataStore.getState().precipIndexByCycle).toEqual({ [key]: { status: 'error' } })
  })

  it('never spells best or compare into a cycles, valid-times or precip request', async () => {
    // fixture 决策 8：只用解析出的具体 gfs/ifs 拼 URL，解析不出就一条都不发。
    const bestCalls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, source: 'best', cycle: null })

    expect(bestCalls.find((call) => call.path === CYCLES_PATH)?.query).toEqual({ source: 'gfs' })
    expect(bestCalls.find((call) => call.path === PRECIP_INDEX_PATH)?.pathParams).toEqual({
      source: 'gfs',
      cycle: DEFAULT_CYCLE,
    })

    clearOverviewDataCache()
    useOverviewDataStore.setState({ cyclesBySource: {}, validTimesByCycle: {}, precipIndexByCycle: {} })
    const compareCalls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, source: 'compare', cycle: null })

    expect(
      compareCalls.filter((call) => [CYCLES_PATH, VALID_TIMES_PATH, PRECIP_INDEX_PATH].includes(call.path)),
    ).toHaveLength(0)
    expect(useOverviewDataStore.getState().cyclesBySource).toEqual({})
  })

  it('keeps the untouched default query on GFS and still asks for pipeline status', async () => {
    // AC4 附加 (a)：默认 source 由 best 翻成 gfs 后，`pipelineRequestParams` 的 cycle 回退必须
    // 推广到所有非 compare 源，否则默认全国总览不再发 /api/v1/pipeline/status（摘要卡片空掉）。
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadOverview(defaultM11QueryState)

    // 同一条回退：默认态下 sourceSelection 仍带具体周期（时间轴的分析/预报分界线读它）。
    expect(snapshot.summary.sourceSelection.cycleTime).toBe('2026-05-18T00:00:00Z')
    expect(snapshot.summary.sourceSelection.provenanceLabel).toContain('cycle 2026-05-18T00:00:00Z')

    const pipelineCalls = calls.filter((call) => call.path === '/api/v1/pipeline/status')
    expect(pipelineCalls).toHaveLength(1)
    expect(pipelineCalls[0].query).toEqual({ source: 'GFS', cycle_time: '2026-05-18T00:00:00Z' })
    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.source === 'GFS')).toBe(true)
  })

  it('keeps the untouched default query on GFS in basin detail too', async () => {
    // AC4 附加 (b)：全局默认 gfs 同样适用于流域详情（`best` 只在 URL/用户显式选择时生效）；
    // 流域详情的周期仍来自 /api/v1/runs，不走 cycles 端点（fixture 决策 9）。
    const calls = mockApi()

    await useOverviewDataStore.getState().loadBasinDetail('basin-demo', defaultM11QueryState)

    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.source === 'GFS')).toBe(true)
    const forecastCall = calls.find((call) => call.path.endsWith('/forecast-series'))
    expect(forecastCall?.query?.scenarios).toBe('forecast_gfs_deterministic')
    expect(calls.filter((call) => call.path === CYCLES_PATH)).toHaveLength(0)
  })

  it('does not re-request anything when only the precipitation toggle changes', async () => {
    // precip 是纯渲染开关：进了取数身份就会整轮重载并作废在途 enrichment。
    const calls = mockApi()

    const first = await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    const settledCallCount = calls.length

    const second = await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, precip: false })

    expect(second.requestScope.dataKey).toBe(first.requestScope.dataKey)
    expect(calls).toHaveLength(settledCallCount)
  })

  it('loads basin detail with river geometry and q_down forecast only', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', query)

    expect(snapshot.segments[0]).toMatchObject({ currentQ: 12, qUnit: 'm3/s' })
    expect(snapshot.selectedSegment?.currentQ).toBe(12)
    expect(calls.filter((call) => call.path === '/api/v1/runs').every((call) => call.query?.status === 'published')).toBe(true)
  })

  // ── 流域详情的全国 discharge 叠加层：只有目录默认对才渲染 ───────────────────────────────
  // spec frontend-mvt-layer-consumption「Basin detail renders the national discharge overlay only
  // for the catalog default pair」。后端对 run-scoped `/api/v1/layers?run_id=` 同样合并全国
  // discharge 元数据，所以流域详情的 discharge 也是 `{source}/{cycle}` 模板；非默认对若回落到
  // 目录默认对的 `metadata.valid_times`，就会拼出良构但错身份的瓦片 URL。
  // 流域详情**不发** per-cycle valid-times（决策 9：时间轴来自选中的 run）；下面的
  // `perCycleValidTimesCalls` 只统计带 `source`/`cycle` 的那种请求——run-scoped 的
  // `/api/v1/layers/discharge/valid-times?run_id=` 是既有且合法的另一条通路。
  const perCycleValidTimesCalls = (calls: MockCall[]) =>
    calls.filter(
      (call) => call.path === VALID_TIMES_PATH && (call.query?.source !== undefined || call.query?.cycle !== undefined),
    )
  const decodedTilePath = (overlay: { source: { tiles: string[] } } | null) =>
    overlay ? decodeURIComponent(new URL(overlay.source.tiles[0], 'http://localhost').pathname) : null

  it('fails closed in basin detail when the URL source is not the catalog default source', async () => {
    const calls = mockApi()
    const ifsQuery = { ...query, source: 'ifs' as const }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', ifsQuery)

    // 缓存里没有 (ifs, DEFAULT_CYCLE) 的列表，而流域详情永远不会去取它 → 终态 error，不是 pending。
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
    expect(perCycleValidTimesCalls(calls)).toHaveLength(0)
    // 非空对照：run-scoped 的那条 valid-times 确实发了（决策 9 的合法通路），
    // 所以上面的 0 不是「过滤器把所有请求都排除掉了」的空断言。
    const runScopedValidTimesCalls = calls.filter((call) => call.path === VALID_TIMES_PATH)
    expect(runScopedValidTimesCalls).not.toHaveLength(0)
    expect(runScopedValidTimesCalls.every((call) => call.query?.run_id === 'run-001')).toBe(true)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.validTimes).toEqual([])
    // 尤其**不是**默认对那份列表。
    expect(discharge?.validTimes).not.toContain('2026-05-18T06:00:00.000Z')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    // 零瓦片请求：跨身份的 `/hydro-national/ifs/<gfs 的默认周期>/…` 拼不出来。
    expect(buildM11RegisteredOverlay(ifsQuery, snapshot.layers)).toBeNull()
  })

  it('fails closed in basin detail when the URL cycle is not the catalog default cycle', async () => {
    const calls = mockApi()
    const otherCycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', otherCycleQuery)

    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
    expect(perCycleValidTimesCalls(calls)).toHaveLength(0)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.validTimes).toEqual([])
    expect(discharge?.validTimes).not.toContain('2026-05-18T06:00:00.000Z')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(buildM11RegisteredOverlay(otherCycleQuery, snapshot.layers)).toBeNull()
  })

  it('still renders the basin-detail national overlay for the catalog default pair', async () => {
    // 不回归工作用例：`query.cycle` 是毫秒拼写的默认周期，同时钉住秒精度的 isDefault 比较。
    mockApi()

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', query)

    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
    expect(discharge?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    expect(decodedTilePath(buildM11RegisteredOverlay(query, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf',
    )
  })

  it('validates the same identity the tile builder substitutes when the URL says best', async () => {
    // 陷阱：`concreteSurfaceQuery` 会把 `best` 按选中的 run 解析成具体源（这里 run 是 IFS），
    // 而 `buildM11RegisteredOverlay` 代入的是 `resolveNationalScaleSource('best') === 'gfs'`。
    // 活动对若按 `concreteSurfaceQuery` 解析就成了 (ifs, …) → 非默认 → 图层被判不可用，
    // 而 URL 上的 `best` 在全国口径下本就是目录默认对。
    mockApi({
      '/api/v1/runs': (options) =>
        success({
          items: [{ ...run, source_id: 'IFS', scenario_id: 'forecast_ifs_deterministic' }],
          total: 1,
          limit: 20,
          offset: options.params?.query?.offset ?? 0,
        }),
    })
    const bestQuery = { ...query, source: 'best' as const }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', bestQuery)

    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    // 校验身份 = 代入身份：URL 的 `best` 在全国口径归一为 gfs，正是目录默认源。
    expect(decodedTilePath(buildM11RegisteredOverlay(bestQuery, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf',
    )
  })

  it('reuses the shared per-cycle cache the national overview already filled', async () => {
    // 共享缓存是特性不是特例：总览取过 (gfs, OTHER_CYCLE) 后，流域详情直接复用，
    // 自己一条 per-cycle 请求都不发。
    const calls = mockApi()
    const otherCycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    await useOverviewDataStore.getState().loadOverview(otherCycleQuery)
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({
      [`gfs|${OTHER_CYCLE}`]: {
        status: 'available',
        validTimes: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
      },
    })
    // 先清掉模块级 HTTP 缓存再进流域详情：否则「没发 per-cycle 请求」是缓存造成的真空断言 ——
    // 上面的 loadOverview 已把 (gfs, OTHER_CYCLE) 的响应落进 `cached()`，泄漏的请求根本到不了
    // `client.GET`。`clearOverviewDataCache()` 连 store 的 `validTimesByCycle` 一并清（它俩同寿），
    // 而本用例的前提正是那份共享列表还在，故快照后回填。HTTP 缓存有 TTL、store 状态没有，
    // 「缓存已过期而共享列表仍在」是真实可达状态，不是为断言捏造的。
    const sharedValidTimes = useOverviewDataStore.getState().validTimesByCycle
    clearOverviewDataCache()
    useOverviewDataStore.setState({ validTimesByCycle: sharedValidTimes })
    const callsBeforeBasinLoad = calls.length

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', otherCycleQuery)

    // 流域详情自身这一段没有再发 per-cycle valid-times。
    expect(perCycleValidTimesCalls(calls.slice(callsBeforeBasinLoad))).toHaveLength(0)
    // 非空对照：清缓存后这一段的请求确实到得了 client.GET（run-scoped valid-times 就在里面），
    // 所以上面的 0 是「真没发」而不是「发了但被缓存吃掉」。
    const runScopedValidTimesCalls = calls
      .slice(callsBeforeBasinLoad)
      .filter((call) => call.path === VALID_TIMES_PATH && call.query?.run_id === 'run-001')
    expect(runScopedValidTimesCalls).not.toHaveLength(0)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    expect(decodedTilePath(buildM11RegisteredOverlay(otherCycleQuery, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-17T12:00:00Z/q_down/2026-05-17T15:00:00Z/{z}/{x}/{y}.pbf',
    )
  })
})
