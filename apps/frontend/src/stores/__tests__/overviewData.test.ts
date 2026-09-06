import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
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
    expect(Object.values(stored.validTimesByCycle)[0]).toEqual([
      OTHER_CYCLE,
      '2026-05-17T15:00:00Z',
      '2026-05-17T18:00:00Z',
    ])
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

  it('discards enrichment results that arrive after overviewRequestNonce advanced', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [CYCLES_PATH]: async () => {
        await gate
        return success({ source: 'gfs', cycles: [], default_cycle: null })
      },
    })

    const load = useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    await vi.waitFor(() => expect(calls.some((call) => call.path === CYCLES_PATH)).toBe(true))
    // 新一轮请求已开始（nonce 递增）：迟到的结果一律不写入 store。
    clearOverviewDataCache()
    release()
    await load

    expect(useOverviewDataStore.getState().cyclesBySource).toEqual({})
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
})
