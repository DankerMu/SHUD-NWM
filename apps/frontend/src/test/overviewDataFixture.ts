// 共享 fixture：`src/stores/__tests__/overviewData*.test.ts` 三个分片共用的 mock api client、
// 目录/周期常量与 store 重置。`vi.mock('@/api/client')` 必须留在各测试文件里（vitest 只在被
// 转换的测试文件内提升 mock 注册），本模块只消费被 mock 后的 `client`。
import { vi } from 'vitest'

import { client } from '@/api/client'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'

export const CYCLES_PATH = '/api/v1/layers/discharge/cycles'
export const VALID_TIMES_PATH = '/api/v1/layers/{layer_id}/valid-times'
export const PRECIP_INDEX_PATH = '/api/v1/precip/{source}/{cycle}/index'
export const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
export const OTHER_CYCLE = '2026-05-17T12:00:00Z'
/** IFS 自己声明的默认周期（`/layers/discharge/cycles?source=ifs` 的 `default_cycle`）。 */
export const IFS_CYCLE = '2026-05-17T06:00:00Z'

export const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

export const query: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
  segmentId: 'river-001',
}

export function success<T>(data: T) {
  return { data: { status: 'ok', data }, error: undefined }
}

export const basin = {
  basin_id: 'basin-demo',
  basin_name: 'Demo Basin',
  basin_group: 'demo',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
}

export const basinVersion = {
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

export const model = {
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

export const run = {
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
export const nationalDischargeMetadata = {
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

export const layer = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: nationalDischargeMetadata,
}

export const precipIndex = {
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

export function apiError(code: string) {
  return { data: undefined, error: { request_id: 'req-1', status: 'error', error: { code, message: code } } }
}

export const riverSegments = {
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

export type MockOptions = { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
export type MockCall = {
  path: string
  query?: Record<string, unknown>
  pathParams?: Record<string, unknown>
  /** 调用发生时的 mapBootstrapLoading 读数：用来钉「enrichment 在 bootstrap 落定之后才发出」。 */
  mapBootstrapLoading: boolean
}

export function mockApi(overrides: Record<string, (options: MockOptions) => unknown> = {}) {
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

// AC7（决策 13 / finding C1）：目录 metadata 的 `default_cycle` 是 **GFS 专有事实**
// （后端 `list_layers` 无 `source` 参数），非默认源必须读该源自己的 `cycles.default_cycle`。
// 本单新增的源分段是全仓唯一写 `onQueryChange({ source })` 的调用点，故这条路径首次可达。
export const ifsQuery = { ...query, source: 'ifs' as const, cycle: null, validTime: null }
export const cyclesPayload = (source: unknown) =>
  source === 'ifs'
    ? success({
        source: 'ifs',
        cycles: [{ cycle_time: IFS_CYCLE, valid_time_start: IFS_CYCLE, valid_time_end: '2026-05-17T18:00:00Z' }],
        default_cycle: IFS_CYCLE,
      })
    : success({
        source: 'gfs',
        cycles: [{ cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T06:00:00Z' }],
        default_cycle: DEFAULT_CYCLE,
      })

export const decodedTilePath = (overlay: { source: { tiles: string[] } } | null) =>
  overlay ? decodeURIComponent(new URL(overlay.source.tiles[0], 'http://localhost').pathname) : null

/** 每个分片的 `beforeEach`：与拆分前逐字同源，重置同一批 mock / cache / store 字段。 */
export function resetOverviewDataTestState() {
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
}
