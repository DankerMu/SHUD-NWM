import { readFileSync } from 'node:fs'

import type { Page, Route } from '@playwright/test'

import type { components } from '../../src/api/types'
import { RIVER_TILE_URL_TEMPLATE, isRiverFixtureTilePath, riverFixture, riverTileFile } from './riverFixture'

/**
 * 曲线窗 e2e 的共享 mock（openspec mobile-responsive-display task 1.3；站点两条由 1.4 消费）。
 *
 * 一个流域、一个模型、GFS / IFS 各一个已发布 run、同一个起报时次；瓦片里的河段、latest-product
 * 与 forecast-series 的身份互相一致。响应形状按 `src/api/types.ts` 的生成类型标注。
 * 未列出的 `/api/v1/**` 请求回 404 并记进 `unmocked`——不拿 `{data: []}` 兜底，那会被形状守卫
 * 判成“数据异常”而悄悄改变页面状态。
 */
type Schemas = components['schemas']

const { identity, tile, anchor } = riverFixture

export const MOCK_CYCLE = '2026-05-18T00:00:00Z'
const HOUR_MS = 60 * 60 * 1000
const cycleMs = Date.parse(MOCK_CYCLE)
const hoursAfterCycle = (hours: number) => new Date(cycleMs + hours * HOUR_MS).toISOString().replace('.000Z', 'Z')
/** 起报后 0–24h、3 小时一步，共 9 个有效时刻。 */
export const MOCK_VALID_TIMES = Array.from({ length: 9 }, (_, index) => hoursAfterCycle(index * 3))
const MOCK_HORIZON_HOURS = 24
const MOCK_VALID_TIME_END = hoursAfterCycle(MOCK_HORIZON_HOURS)
const MOCK_MODEL_ID = 'e2e-model'
const MOCK_FORCING_VERSION_IDS = { GFS: 'e2e-forcing-gfs', IFS: 'e2e-forcing-ifs' } as const
const MOCK_RUN_IDS = { GFS: 'e2e-run-gfs', IFS: 'e2e-run-ifs' } as const
const MOCK_SCENARIOS = { GFS: 'forecast_gfs_deterministic', IFS: 'forecast_ifs_deterministic' } as const
type MockSource = keyof typeof MOCK_RUN_IDS

export const mockBasin = {
  basin_id: identity.basinId,
  basin_name: 'E2E Basin',
  basin_group: 'e2e',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
} satisfies Schemas['Basin']

export const mockBasinVersion = {
  basin_version_id: identity.basinVersionId,
  basin_id: identity.basinId,
  version_label: 'v1',
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
  geom: {
    type: 'MultiPolygon',
    coordinates: [
      [
        [
          [anchor[0] - 0.1, anchor[1] - 0.1],
          [anchor[0] + 0.1, anchor[1] - 0.1],
          [anchor[0] + 0.1, anchor[1] + 0.1],
          [anchor[0] - 0.1, anchor[1] + 0.1],
          [anchor[0] - 0.1, anchor[1] - 0.1],
        ],
      ],
    ],
  },
} satisfies Schemas['BasinVersion']

export const mockModel = {
  model_id: MOCK_MODEL_ID,
  model_name: 'E2E SHUD',
  basin_id: identity.basinId,
  basin_name: mockBasin.basin_name,
  basin_version_id: identity.basinVersionId,
  river_network_version_id: identity.riverNetworkVersionId,
  mesh_version_id: 'e2e-mesh-v1',
  calibration_version_id: 'e2e-calibration-v1',
  segment_count: 1,
  shud_code_version: 'v1',
  active_flag: true,
  lifecycle_state: 'active',
  model_package_uri: null,
  resource_profile: {},
  created_at: '2026-05-02T00:00:00Z',
} satisfies Schemas['ModelInstance']

function mockRun(source: MockSource) {
  return {
    run_id: MOCK_RUN_IDS[source],
    run_type: 'forecast',
    scenario_id: MOCK_SCENARIOS[source],
    model_id: MOCK_MODEL_ID,
    basin_id: identity.basinId,
    basin_version_id: identity.basinVersionId,
    river_network_version_id: identity.riverNetworkVersionId,
    forcing_version_id: MOCK_FORCING_VERSION_IDS[source],
    source_id: source,
    cycle_time: MOCK_CYCLE,
    status: 'published',
    start_time: MOCK_CYCLE,
    end_time: MOCK_VALID_TIME_END,
    created_at: '2026-05-18T04:00:00Z',
    updated_at: '2026-05-18T04:30:00Z',
  } satisfies Schemas['HydroRun']
}

export const mockRuns = { GFS: mockRun('GFS'), IFS: mockRun('IFS') }

/** 全国径流图层：默认源 gfs、默认周期 = `MOCK_CYCLE`（与周期目录一致，故同一次加载零次 valid-times 请求）。 */
export const mockDischargeLayer = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: {
    layer_id: 'discharge',
    tile_format: 'mvt',
    maplibre_source_layer: tile.sourceLayer,
    min_zoom: 3,
    max_zoom: tile.z,
    url_template: RIVER_TILE_URL_TEMPLATE,
    required_placeholders: ['source', 'cycle', 'valid_time', 'z', 'x', 'y'],
    source_refs: {},
    default_source: 'gfs',
    default_cycle: MOCK_CYCLE,
    valid_times: MOCK_VALID_TIMES,
    fallback_available: false,
    release_blocking: false,
  },
} satisfies Schemas['Layer']

function mockDischargeCycles(source: 'gfs' | 'ifs') {
  return {
    source,
    cycles: [{ cycle_time: MOCK_CYCLE, valid_time_start: MOCK_CYCLE, valid_time_end: MOCK_VALID_TIME_END }],
    default_cycle: MOCK_CYCLE,
  } satisfies Schemas['DischargeCycles']
}

function mockPipelineStatus(source: string) {
  return {
    cycle_id: `e2e-cycle-${source.toLowerCase()}`,
    source,
    cycle_time: MOCK_CYCLE,
    current_state: 'published',
    started_at: '2026-05-18T03:00:00Z',
    updated_at: '2026-05-18T04:30:00Z',
    job_counts: { succeeded: 1, failed: 0, running: 0, pending: 0 },
  } satisfies Schemas['PipelineStatus']
}

const mockRuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
} satisfies Schemas['RuntimeConfig']

/** 气象代站：离河段 anchor 约 0.006° / 0.003°，同屏时不压在河段上。 */
export const mockStation = {
  station_id: 'e2e-station-0001',
  basin_version_id: identity.basinVersionId,
  station_name: 'E2E Station',
  longitude: anchor[0] + 0.006,
  latitude: anchor[1] + 0.003,
  elevation_m: 3200,
  station_role: 'forcing_proxy',
  created_at: '2026-05-01T00:00:00Z',
} satisfies Schemas['MetStation']

const mockStationPage = {
  items: [mockStation],
  total_count: 1,
  limit: 500,
  offset: 0,
  filters: { available: { search: true, variables: false, qc_status: false } },
} satisfies Schemas['MetStationPage']

function mockLatestProduct(source: MockSource) {
  return {
    basin_id: identity.basinId,
    model_id: MOCK_MODEL_ID,
    basin_version_id: identity.basinVersionId,
    river_network_version_id: identity.riverNetworkVersionId,
    available_issue_times: [MOCK_CYCLE],
    source_id: source,
    cycle_time: MOCK_CYCLE,
    run_id: MOCK_RUN_IDS[source],
    forcing_version_id: MOCK_FORCING_VERSION_IDS[source],
    station_count: 1,
    expected_station_count: 1,
    segment_count: 1,
    expected_segment_count: 1,
    status: 'ready',
    run_status: 'published',
    valid_time_start: MOCK_CYCLE,
    valid_time_end: MOCK_VALID_TIME_END,
    river_valid_time_start: MOCK_CYCLE,
    river_valid_time_end: MOCK_VALID_TIME_END,
    forcing_valid_time_start: MOCK_CYCLE,
    forcing_valid_time_end: MOCK_VALID_TIME_END,
    available_horizon_hours: MOCK_HORIZON_HOURS,
    expected_horizon_hours: MOCK_HORIZON_HOURS,
    shorter_horizon: false,
    availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
    quality: {
      station_sample_count: 0,
      river_sample_count: 0,
      required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn'],
      station_variable_coverage: [],
      candidate_limit: 0,
      search_limit: 0,
      context_limit: 0,
      query_indexes: [],
    },
  } satisfies Schemas['QhhLatestProduct']
}

/** 河段 q_down 曲线：两个源各一条、逐 3 小时、数值不同（GFS 高于 IFS），点为 `[毫秒, 值]`。 */
function mockRiverForecastSeries(source: MockSource) {
  const base = source === 'GFS' ? riverFixture.value : riverFixture.value * 0.8
  return {
    segment_id: identity.riverSegmentId,
    issue_time: MOCK_CYCLE,
    unit: 'm3/s',
    series: [
      {
        scenario_id: MOCK_SCENARIOS[source],
        source_id: source,
        cycle_time: MOCK_CYCLE,
        available_lead_hours: MOCK_HORIZON_HOURS,
        segment_role: 'forecast',
        variable: 'q_down',
        points: MOCK_VALID_TIMES.map((validTime, index) => [Date.parse(validTime), base + index * 1.5]),
      },
    ],
  } satisfies Schemas['RiverSeriesResponse']
}

const STATION_VARIABLES = [
  { variable: 'PRCP', unit: 'mm', base: 0.4 },
  { variable: 'TEMP', unit: 'degC', base: 6 },
  { variable: 'RH', unit: '%', base: 55 },
  { variable: 'wind', unit: 'm/s', base: 3 },
  { variable: 'Rn', unit: 'W/m2', base: 180 },
] as const

function mockStationSeries(source: MockSource) {
  return {
    station_id: mockStation.station_id,
    station: mockStation,
    forcing_version_id: MOCK_FORCING_VERSION_IDS[source],
    model_id: MOCK_MODEL_ID,
    source_id: source,
    cycle_time: MOCK_CYCLE,
    valid_time_start: MOCK_CYCLE,
    valid_time_end: MOCK_VALID_TIME_END,
    limit: 480,
    requested_from: null,
    requested_to: null,
    series: STATION_VARIABLES.map(({ variable, unit, base }) => ({
      variable,
      unit,
      native_resolution: 'PT3H',
      source_id: source,
      cycle_time: MOCK_CYCLE,
      points: MOCK_VALID_TIMES.map((validTime, index) => ({
        valid_time: validTime,
        value: base + index * 0.5,
        quality_flag: 'ok',
        source_id: source,
      })),
      truncated: false,
      metadata: {
        limit: 480,
        returned_points: MOCK_VALID_TIMES.length,
        requested_from: null,
        requested_to: null,
        returned_from: MOCK_CYCLE,
        returned_to: MOCK_VALID_TIME_END,
        truncated: false,
      },
    })),
  } satisfies Schemas['StationSeriesResponse']
}

function mockError(code: string, message: string) {
  return { request_id: 'req_e2e', status: 'error', error: { code, message } } satisfies Schemas['ErrorResponse']
}

function sourceParam(value: string | null): MockSource | null {
  const upper = value?.toUpperCase()
  return upper === 'GFS' || upper === 'IFS' ? upper : null
}

// 1×1 全透明 RGBA PNG：底图瓦片的“空”响应（栅格源拿到非图片会报 map error 并挂出底图不可用横幅）。
const TRANSPARENT_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=',
  'base64',
)

export interface RiverWindowMockLog {
  /** 夹具瓦片被请求过的 pathname（按请求顺序）。 */
  riverTileRequests: string[]
  /** 河段 forecast-series 请求的 `scenarios` 参数（按请求顺序）。 */
  forecastSeriesScenarios: string[]
  /** 没有对应 mock 的 `/api/v1/**` 请求（`METHOD pathname?search`）；正常应为空。 */
  unmocked: string[]
}

/**
 * 安装曲线窗 mock。须在 `page.goto` 之前调用；返回的记录对象随请求实时更新。
 * 夹具瓦片之外的矢量瓦片回 204（空瓦片），底图瓦片回 1×1 透明 PNG——都立即返回，地图才能到 `idle`。
 */
export async function installRiverWindowMocks(page: Page): Promise<RiverWindowMockLog> {
  const log: RiverWindowMockLog = { riverTileRequests: [], forecastSeriesScenarios: [], unmocked: [] }
  const riverTile = readFileSync(riverTileFile)
  const json = (route: Route, data: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
  const ok = (route: Route, data: unknown) => json(route, { status: 'ok', data })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = decodeURIComponent(url.pathname)
    const query = url.searchParams
    if (request.method() !== 'GET') {
      log.unmocked.push(`${request.method()} ${url.pathname}${url.search}`)
      return json(route, mockError('METHOD_NOT_MOCKED', 'Only GET is mocked.'), 405)
    }

    if (path.startsWith('/api/v1/basemap/tianditu/')) {
      return route.fulfill({ status: 200, contentType: 'image/png', body: TRANSPARENT_PNG })
    }
    if (path.startsWith('/api/v1/tiles/')) {
      if (isRiverFixtureTilePath(path)) {
        log.riverTileRequests.push(path)
        return route.fulfill({ status: 200, contentType: 'application/x-protobuf', body: riverTile })
      }
      return route.fulfill({ status: 204 })
    }

    if (path === '/api/v1/runtime/config') return ok(route, mockRuntimeConfig)
    if (path === '/api/v1/basins') return ok(route, [mockBasin] satisfies Schemas['Basin'][])
    if (path === `/api/v1/basins/${identity.basinId}/versions`) {
      return ok(route, [mockBasinVersion] satisfies Schemas['BasinVersion'][])
    }
    if (path === '/api/v1/models') {
      return ok(route, { items: [mockModel], total: 1, limit: 200, offset: 0 } satisfies Schemas['ModelInstancePage'])
    }
    if (path === '/api/v1/runs') {
      const source = sourceParam(query.get('source'))
      const status = query.get('status')
      const items = (source ? [mockRuns[source]] : [mockRuns.GFS, mockRuns.IFS]).filter(
        (run) => !status || run.status === status,
      )
      return ok(route, { items, total: items.length, limit: 20, offset: 0 } satisfies Schemas['HydroRunPage'])
    }
    if (path === '/api/v1/pipeline/status') return ok(route, mockPipelineStatus(query.get('source') ?? 'GFS'))
    if (path === '/api/v1/layers') return ok(route, [mockDischargeLayer] satisfies Schemas['Layer'][])
    if (path === '/api/v1/layers/discharge/cycles') {
      const source = query.get('source')
      if (source === 'gfs' || source === 'ifs') return ok(route, mockDischargeCycles(source))
    }
    // 图层目录没有 precip 条目；store 仍按活动周期取一次降水索引，如实回“未镜像”。
    if (/^\/api\/v1\/precip\/(gfs|ifs)\/[^/]+\/index$/.test(path)) {
      return json(route, mockError('PRECIP_CYCLE_NOT_MIRRORED', 'Precipitation is not mirrored for this cycle.'), 404)
    }

    if (path === '/api/v1/mvp/qhh/latest-product') {
      const source = sourceParam(query.get('source'))
      const basinId = query.get('basin_id')
      if (source && (!basinId || basinId === identity.basinId)) return ok(route, mockLatestProduct(source))
    }
    if (
      path ===
      `/api/v1/basin-versions/${identity.basinVersionId}/river-segments/${identity.riverSegmentId}/forecast-series`
    ) {
      const scenarios = query.get('scenarios') ?? ''
      const source = (Object.keys(MOCK_SCENARIOS) as MockSource[]).find((key) => MOCK_SCENARIOS[key] === scenarios)
      if (source && query.get('run_id') === MOCK_RUN_IDS[source]) {
        log.forecastSeriesScenarios.push(scenarios)
        return ok(route, mockRiverForecastSeries(source))
      }
    }

    if (path === '/api/v1/met/stations') return ok(route, mockStationPage)
    if (path === `/api/v1/met/stations/${mockStation.station_id}/series`) {
      const source = sourceParam(query.get('source_id'))
      if (source) return ok(route, mockStationSeries(source))
    }

    log.unmocked.push(`GET ${url.pathname}${url.search}`)
    return json(route, mockError('NOT_MOCKED', 'No e2e mock for this request.'), 404)
  })

  return log
}
