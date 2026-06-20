import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import {
  _floodRankingInFlightSize,
  basinSnapshotMatchesQuery,
  clearOverviewDataCache,
  loadFloodRankingOnDemand,
  releaseFloodRankingOnDemand,
  useOverviewDataStore,
} from '@/stores/overviewData'
import type { M11QueryState } from '@/lib/m11/queryState'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { filterBasinSegmentRows, m11BasinRiverCollectionBudget, normalizeLayerStates } from '@/lib/m11/overviewDataContracts'

const RIVER_SEGMENT_RETAINED_ITEM_CAP = 10_000

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

const query: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00Z',
  validTime: '2026-05-18T06:00:00Z',
  layer: 'flood-return-period',
  basinVersionId: 'yangtze_v2026_01',
  segmentId: 'seg-123',
  warningLevel: null,
  q: null,
}

function success<T>(data: T) {
  return { data: { status: 'ok', data }, error: undefined }
}

const basin = {
  basin_id: 'yangtze',
  basin_name: 'Yangtze Basin',
  basin_group: 'major',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
}

const basinVersion = {
  basin_version_id: 'yangtze_v2026_01',
  basin_id: 'yangtze',
  version_label: 'v2026_01',
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
  geom: { type: 'MultiPolygon', coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31], [100, 30]]]] },
}

const model = {
  model_id: 'yangtze_shud_v12',
  model_name: 'Yangtze SHUD',
  basin_id: 'yangtze',
  basin_name: 'Yangtze Basin',
  basin_version_id: 'yangtze_v2026_01',
  river_network_version_id: 'yangtze_rivnet_v12',
  mesh_version_id: 'mesh-1',
  calibration_version_id: 'cal-1',
  segment_count: 1,
  mesh_uri: null,
  mesh_checksum: null,
  shud_code_version: 'v1',
  rshud_code_version: null,
  autoshud_code_version: null,
  active_flag: true,
  container_image: null,
  model_package_uri: 's3://models/yangtze',
  package_checksum: null,
  manifest_uri: null,
  source_inventory_checksum: null,
  basin_slug: 'yangtze',
  shud_input_name: null,
  source_path: null,
  resolved_source_path: null,
  source_uri: null,
  source_is_symlink: null,
  resource_profile: {},
  created_at: '2026-05-02T00:00:00Z',
}

const run = {
  run_id: 'run-gfs-1',
  run_type: 'forecast',
  scenario_id: 'forecast_gfs_deterministic',
  model_id: 'yangtze_shud_v12',
  basin_version_id: 'yangtze_v2026_01',
  river_network_version_id: 'yangtze_rivnet_v12',
  forcing_version_id: null,
  init_state_id: null,
  source_id: 'GFS',
  cycle_time: '2026-05-18T00:00:00Z',
  status: 'frequency_done',
  slurm_job_id: null,
  start_time: '2026-05-18T00:00:00Z',
  end_time: '2026-05-25T00:00:00Z',
  run_manifest_uri: null,
  output_uri: null,
  log_uri: null,
  error_code: null,
  error_message: null,
  product_quality: {
    flood_return_period: {
      quality_state: 'ready',
      max_over_window: true,
      result_rows: 2,
      return_period_rows: 2,
      warning_rows: 2,
      unavailable_products: [],
      residual_blockers: [],
    },
  },
  created_at: '2026-05-18T00:01:00Z',
  updated_at: '2026-05-18T01:00:00Z',
}

const ifsRun = {
  ...run,
  run_id: 'run-ifs-1',
  scenario_id: 'forecast_ifs_deterministic',
  source_id: 'IFS',
  updated_at: '2026-05-18T01:05:00Z',
}

const ranking = {
  items: [
    {
      rank: 1,
      river_segment_id: 'seg-123',
      segment_id: 'seg-123',
      segment_name: 'Segment 123',
      basin_version_id: 'yangtze_v2026_01',
      river_network_version_id: 'yangtze_rivnet_v12',
      q_value: 123,
      q_unit: null,
      return_period: 20,
      warning_level: 'warning',
      duration: '1h',
      valid_time: '2026-05-18T06:00:00Z',
    },
  ],
  total: 1,
  limit: 200,
  offset: 0,
}

const pipelineStatus = {
  cycle_id: 'cycle-1',
  source: 'GFS',
  cycle_time: '2026-05-18T00:00:00Z',
  current_state: 'running',
  started_at: '2026-05-18T00:01:00Z',
  updated_at: '2026-05-18T06:00:00Z',
  job_counts: { succeeded: 3, failed: 0, running: 2, pending: 1 },
}

const featureCollection = {
  type: 'FeatureCollection',
  total: 1,
  feature_total: 1,
  limit: 1000,
  offset: 0,
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-123',
        river_segment_id: 'seg-123',
        basin_version_id: 'yangtze_v2026_01',
        river_network_version_id: 'yangtze_rivnet_v12',
        name: 'Segment 123',
        stream_order: 4,
        length_m: 1000,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  clearOverviewDataCache()
  useOverviewDataStore.setState(useOverviewDataStore.getInitialState(), true)
})

describe('useOverviewDataStore', () => {
  it('does not repopulate the shared cache when an in-flight request resolves after cache clear', async () => {
    vi.useFakeTimers()
    const calls: string[] = []
    let basinCalls = 0
    let releaseInitialBasins: (() => void) | null = null
    const initialBasins = new Promise<unknown>((resolve) => {
      releaseInitialBasins = () => resolve(success([]))
    })

    try {
      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        calls.push(path)

        if (path === '/api/v1/basins') {
          basinCalls += 1
          if (basinCalls === 1) return (await initialBasins) as never
          return success([]) as never
        }
        if (path === '/api/v1/models') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
        if (path === '/api/v1/layers') return success([]) as never
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      const staleLoad = useOverviewDataStore.getState().loadOverview(query)
      expect(releaseInitialBasins).toBeTypeOf('function')

      clearOverviewDataCache()
      expect(vi.getTimerCount()).toBe(0)
      releaseInitialBasins?.()
      await staleLoad

      calls.length = 0
      await useOverviewDataStore.getState().loadOverview(query)

      expect(calls.filter((path) => path === '/api/v1/basins')).toHaveLength(1)
      expect(vi.getTimerCount()).toBeGreaterThan(0)
    } finally {
      clearOverviewDataCache()
      vi.useRealTimers()
    }
  })

  // PR 4/7：跨 basin 时 missing-required-field 决策保留；默认 path 已不再 fan-out ranking 与
  // per-layer valid-times，断言相应删除。Layer state 通过 metadata.valid_times 直接消费验证。
  it('does not fetch basin-version fields when the basin set is larger than one (cross-basin requires aggregation)', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') {
        return success([basin, { ...basin, basin_id: 'yellow', basin_name: 'Yellow Basin', basin_group: 'major' }]) as never
      }
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        return success([
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: [],
            metadata: { valid_times: ['2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z'] },
          },
        ]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 2, pending: 1, idle: 3 }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({
          run_id: 'run-gfs-1',
          total_segments: 1,
          usable_curves: 1,
          unavailable_count: 0,
          quality_note: null,
          levels: [{ level: 'warning', count: 1, color: '#FFB74D' }],
        }) as never
      }
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.basins[0]).toMatchObject({
      basinId: 'yangtze',
      selectedBasinVersionId: null,
      bbox: null,
      unavailableReason: 'Basin version and bbox require the M11 aggregation endpoint.',
    })
    expect(snapshot.summary).toMatchObject({ completedCyclesToday: 3, runningJobs: 2, warningSegmentCount: 1 })
    expect(snapshot.aggregationDecision).toMatchObject({ needsAggregationEndpoint: true, reason: 'missing-required-field' })
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/overview/summary')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/basins/{basin_id}/versions')
    // PR 4/7 spec scenarios "Default overview bootstrap omits ranking" /
    // "Metadata carries valid_times"：默认 path 既不 fetchFloodRanking 也不 fan-out
    // per-layer valid-times。
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/layers/{layer_id}/valid-times')
    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: 'GFS',
      cycle_time: '2026-05-18T00:00:00Z',
      status: 'frequency_done',
      flood_product_ready: true,
    })
    expect(calls.find((call) => call.path === '/api/v1/runs' && call.query?.status === 'published')?.query).toMatchObject({
      source: 'GFS',
      cycle_time: '2026-05-18T00:00:00Z',
      status: 'published',
      flood_product_ready: true,
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/summary')?.query).toMatchObject({
      run_id: 'run-gfs-1',
      valid_time: '2026-05-18T06:00:00Z',
    })
    // PR 4/7：layer state 直接从 apiLayer.metadata.valid_times 解析，无需独立 fetchLayerValidTimes RTT。
    expect(snapshot.layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({
      validTimeSource: 'api',
      currentValidTime: '2026-05-18T06:00:00.000Z',
    })
  })

  it('keeps basin-version and bbox fields when the measured overview plan stays inside the request threshold', async () => {
    const lowRequestQuery = { ...query, cycle: null, validTime: null }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        return success([{ layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: [] }]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') {
        return success(['2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z']) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(lowRequestQuery)

    expect(snapshot.basins[0]).toMatchObject({
      selectedBasinVersionId: 'yangtze_v2026_01',
      boundary: basinVersion.geom,
      bbox: { minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 },
    })
    expect(snapshot.aggregationDecision).toMatchObject({ needsAggregationEndpoint: false, reason: 'reuse-existing' })
    expect(calls).toContain('/api/v1/basins/{basin_id}/versions')
  })

  // PR 4/7：layer 的 metadata.valid_times 已是默认消费源；空 query.validTime 时仍按数组末尾解析。
  it('defaults no-query layer state to the newest item carried by metadata.valid_times', async () => {
    const noValidTimeQuery = { ...query, validTime: null }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        return success([
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: [],
            metadata: { valid_times: ['2026-05-21T00:00:00Z', '2026-05-21T06:00:00Z', '2026-05-21T12:00:00Z'] },
          },
        ]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: run.run_id, total_segments: 1, usable_curves: 1, unavailable_count: 0, levels: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(noValidTimeQuery)

    expect(snapshot.layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({
      available: true,
      validTimeSource: 'api',
      validTimes: ['2026-05-21T00:00:00.000Z', '2026-05-21T06:00:00.000Z', '2026-05-21T12:00:00.000Z'],
      currentValidTime: '2026-05-21T12:00:00.000Z',
      freshness: {
        validTime: '2026-05-21T12:00:00.000Z',
      },
    })
    // metadata-first：默认 path 不发 fallback /layers/<id>/valid-times（spec scenario "Metadata carries valid_times"）。
    expect(calls).not.toContain('/api/v1/layers/{layer_id}/valid-times')
  })

  // PR 4/7：valid-times 解析改走 metadata-first；run-scope 仍由 fetchLayers(runId) 体现
  // （后端按 run_id 返回不同 metadata.valid_times），不再依赖独立 fetchLayerValidTimes RTT。
  it('scopes overview layer valid-times via run-scoped fetchLayers metadata, no separate per-layer fan-out', async () => {
    const noValidTimeQuery = { ...query, validTime: null }
    const olderRun = { ...run, run_id: 'run-gfs-old', end_time: '2026-05-19T00:00:00Z', updated_at: '2026-05-18T01:00:00Z' }
    const resolvedRun = { ...run, run_id: 'run-gfs-ready', end_time: '2026-05-25T00:00:00Z', updated_at: '2026-05-18T02:00:00Z' }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [olderRun, resolvedRun], total: 2, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        // runless 调用（phase 1 bootstrap）返回空 metadata，run-scoped 调用（enrichment phase）返回 resolvedRun 对应 valid_times。
        const isRunScoped = options?.params?.query?.run_id !== undefined && options?.params?.query?.run_id !== null
        if (isRunScoped && options?.params?.query?.run_id !== resolvedRun.run_id) {
          throw new Error('layers not scoped to resolved run')
        }
        return success([
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: [],
            metadata: isRunScoped
              ? { valid_times: ['2026-05-18T09:17:00Z', '2026-05-18T11:41:00Z'] }
              : null,
          },
        ]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: resolvedRun.run_id, total_segments: 1, usable_curves: 1, unavailable_count: 0, levels: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(noValidTimeQuery)

    // run-scoped fetchLayers 携带 run_id 反映 resolvedRun；这一通道替代旧的独立 per-layer valid-times RTT。
    expect(calls.find((call) => call.path === '/api/v1/layers' && call.query?.run_id === resolvedRun.run_id)).toBeTruthy()
    // 默认 path 不再发 per-layer fan-out（spec scenario "Metadata carries valid_times"）。
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/layers/{layer_id}/valid-times')
    expect(snapshot.layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({
      currentValidTime: '2026-05-18T11:41:00.000Z',
      freshness: {
        runId: resolvedRun.run_id,
        validTime: '2026-05-18T11:41:00.000Z',
      },
    })
  })

  it('selects published-only ready runs for overview flood surfaces (no ranking on default path)', async () => {
    const publishedRun = { ...run, run_id: 'run-gfs-published', status: 'published', updated_at: '2026-05-18T01:30:00Z' }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') {
        return options?.params?.query?.status === 'published'
          ? (success({ items: [publishedRun], total: 1, limit: 20, offset: 0 }) as never)
          : (success({ items: [], total: 0, limit: 20, offset: 0 }) as never)
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({
          run_id: publishedRun.run_id,
          total_segments: 1,
          usable_curves: 1,
          unavailable_count: 0,
          quality_note: null,
          levels: [{ level: 'warning', count: 1, color: '#FFB74D' }],
        }) as never
      }
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.status).sort()).toEqual([
      'frequency_done',
      'published',
    ])
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/summary')?.query).toMatchObject({
      run_id: publishedRun.run_id,
    })
    // PR 4/7：默认 path 不发 ranking（spec scenario "Default overview bootstrap omits ranking"）。
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
    expect(snapshot.summary.freshness.runId).toBe(publishedRun.run_id)
    expect(snapshot.summary.warningSegmentCount).toBe(1)
  })

  it('does not select status-ready overview runs whose flood product quality is unavailable', async () => {
    const unavailableRun = {
      ...run,
      run_id: 'run-warning-thresholds-unavailable',
      product_quality: {
        flood_return_period: {
          quality_state: 'unavailable',
          max_over_window: true,
          result_rows: 2,
          return_period_rows: 2,
          warning_rows: 0,
          unavailable_products: ['warning_thresholds'],
          residual_blockers: [],
        },
      },
    }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') {
        return options?.params?.query?.status === 'frequency_done'
          ? (success({ items: [unavailableRun], total: 1, limit: 20, offset: 0 }) as never)
          : (success({ items: [], total: 0, limit: 20, offset: 0 }) as never)
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(calls.filter((call) => call.path === '/api/v1/runs').every((call) => call.query?.flood_product_ready === true)).toBe(true)
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/summary')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
    expect(snapshot.summary.freshness.runId).toBeNull()
  })

  it('resolves default best overview surfaces to the latest concrete GFS or IFS run without unsupported source identifiers', async () => {
    const bestQuery = { ...query, source: 'best' as const, cycle: null }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run, ifsRun], total: 2, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        return success([
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: [],
            metadata: { valid_times: ['2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z'] },
          },
        ]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success({ ...pipelineStatus, source: 'IFS' }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: 'run-ifs-1', total_segments: 1, usable_curves: 1, unavailable_count: 0, levels: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(bestQuery)

    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: undefined,
      cycle_time: undefined,
      status: 'frequency_done',
      flood_product_ready: true,
    })
    expect(calls.find((call) => call.path === '/api/v1/pipeline/status')?.query).toMatchObject({
      source: 'IFS',
      cycle_time: '2026-05-18T00:00:00Z',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/summary')?.query).toMatchObject({
      run_id: 'run-ifs-1',
    })
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
    expect(snapshot.summary.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
      cycleTime: '2026-05-18T00:00:00Z',
    })
    expect(snapshot.summary.freshness.runId).toBe('run-ifs-1')
    expect(snapshot.summary.warningSegmentCount).toBe(0)
    expect(snapshot.layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({
      available: true,
      currentValidTime: '2026-05-18T06:00:00.000Z',
      freshness: {
        runId: 'run-ifs-1',
        source: 'IFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        validTime: '2026-05-18T06:00:00.000Z',
      },
    })
  })

  it('marks registered hydrology data layers renderable before store hydration retains them', () => {
    const layers = normalizeLayerStates({
      query,
      layers: [
        { layer_id: 'discharge', layer_name: 'River discharge', layer_type: 'hydrology', variables: ['q_down'], metadata: null },
        { layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: ['return_period'], metadata: null },
        { layer_id: 'warning-level', layer_name: 'Warning level', layer_type: 'hydrology', variables: ['warning_level'], metadata: null },
        { layer_id: 'river-network', layer_name: 'River network', layer_type: 'base', variables: ['geometry'], metadata: null },
      ],
      validTimesByLayerId: {
        discharge: ['2026-05-18T06:00:00Z'],
        'flood-return-period': ['2026-05-18T06:00:00Z'],
        'warning-level': ['2026-05-18T06:00:00Z'],
        'river-network': ['2026-05-18T06:00:00Z'],
      },
      resolvedRun: run,
    })

    expect(layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({
      available: true,
      disabledReason: null,
    })
    for (const layerId of ['discharge', 'warning-level']) {
      expect(layers.find((layer) => layer.layerId === layerId)).toMatchObject({
        available: true,
        disabledReason: null,
      })
    }
    expect(layers.find((layer) => layer.layerId === 'river-network')).toMatchObject({
      available: false,
      disabledReason: 'Layer is registered but no renderable map source is implemented in this repository.',
    })
  })

  it('preserves zero warning count when flood summary succeeds without super-warning levels', async () => {
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({
          run_id: 'run-gfs-1',
          total_segments: 3,
          usable_curves: 3,
          unavailable_count: 0,
          quality_note: null,
          levels: [
            { level: 'normal', count: 2, color: '#4FC3F7' },
            { level: 'watch', count: 1, color: '#FFD54F' },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.summary.warningSegmentCount).toBe(0)
    expect(snapshot.summary.totalSegments).toBe(3)
  })

  it('marks default best overview unavailable and skips pipeline when no concrete GFS or IFS run resolves', async () => {
    const bestQuery = { ...query, source: 'best' as const, cycle: null }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(bestQuery)

    expect(calls.map((call) => call.path)).not.toContain('/api/v1/pipeline/status')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/summary')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
    expect(snapshot.summary.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'Unknown',
      scenarioIds: [],
      unavailableReason: 'Requested source is not available in current payload.',
    })
  })

  it('marks compare overview flood summary and ranking aggregation-needed when GFS and IFS runs are present', async () => {
    const compareQuery = { ...query, source: 'compare' as const }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run, ifsRun], total: 2, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(compareQuery)

    expect(snapshot.summary.sourceSelection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'GFS+IFS',
      comparisonAvailable: true,
    })
    expect(snapshot.summary.warningSegmentCount).toBeNull()
    expect(snapshot.summary.freshness.runId).toBeNull()
    expect(snapshot.summary.partialErrors).toEqual(
      expect.arrayContaining([
        'flood summary: 对比模式洪水摘要需要 GFS+IFS 聚合端点',
        'flood ranking: 对比模式洪水排名需要 GFS+IFS 聚合端点',
      ]),
    )
    expect(calls).not.toContain('/api/v1/flood-alerts/summary')
    expect(calls).not.toContain('/api/v1/flood-alerts/ranking')
  })

  it('marks compare overview unavailable when one source is missing without one-run warning counts', async () => {
    const compareQuery = { ...query, source: 'compare' as const }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(compareQuery)

    expect(snapshot.summary.sourceSelection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'Unknown',
      comparisonAvailable: false,
      unavailableReason: 'Comparison requires both GFS and IFS series.',
    })
    expect(snapshot.summary.warningSegmentCount).toBeNull()
    expect(snapshot.summary.freshness.runId).toBeNull()
    expect(snapshot.summary.partialErrors).toContain('flood summary: 对比模式洪水摘要需要 GFS+IFS 聚合端点')
    expect(calls).not.toContain('/api/v1/flood-alerts/summary')
    expect(calls).not.toContain('/api/v1/flood-alerts/ranking')
  })

  it('marks overview basin versions aggregation-needed instead of issuing per-basin N+1 calls', async () => {
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)
      if (path === '/api/v1/basins') {
        return success([
          basin,
          { ...basin, basin_id: 'yellow', basin_name: 'Yellow Basin', basin_group: 'major' },
        ]) as never
      }
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.aggregationDecision).toMatchObject({
      needsAggregationEndpoint: true,
      reason: 'missing-required-field',
    })
    expect(snapshot.aggregationDecision.evidence).toContain('basin_versions')
    expect(snapshot.basins.map((item) => item.selectedBasinVersionId)).toEqual([null, null])
    expect(calls).not.toContain('/api/v1/basins/{basin_id}/versions')
  })

  // PR 4/7 regression：默认 path 已删除 layerIdsForOverview.map(fetchLayerValidTimes) fan-out
  // （spec scenario "Metadata carries valid_times"）。本测试改为反向断言：discharge 路径不再
  // 触发 per-layer valid-times 请求；aggregationDecision 反映新的最小请求计数（无 fan-out）。
  it('does not fan out per-layer valid-times requests on the default discharge path (PR 4/7 regression)', async () => {
    const calls: Array<{ path: string; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { path?: Record<string, unknown> } }
      calls.push({ path, pathParams: options?.params?.path })
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview({ ...query, layer: 'discharge' })

    // 关键 regression：默认 discharge path 不发任何 per-layer /valid-times 请求。
    expect(calls.filter((call) => call.path === '/api/v1/layers/{layer_id}/valid-times')).toEqual([])
    // 同时确认 ranking 也未被发起。
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
    // initialRequestCount = 5 base（无 latestRun）+ 1 pipeline + 1 version = 7 → reuse-existing。
    expect(snapshot.aggregationDecision).toMatchObject({
      needsAggregationEndpoint: false,
      reason: 'reuse-existing',
    })
  })

  it('uses safe scoped partial errors for pipeline failures without exposing backend paths', async () => {
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 4, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/pipeline/status') {
        return { data: undefined, error: { error: { message: 'failed opening s3://secret/path and /internal/path' } } } as never
      }
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.summary.runningJobs).toBe(4)
    expect(snapshot.summary.partialErrors).toContain('pipeline: 暂不可用')
    expect(useOverviewDataStore.getState().error).toBe('pipeline: 暂不可用')
    expect(JSON.stringify(snapshot.summary.partialErrors)).not.toContain('s3://secret/path')
    expect(JSON.stringify(snapshot.summary.partialErrors)).not.toContain('/internal/path')
  })

  it('deduplicates repeated identical overview loads through the shared request cache', async () => {
    const counts = new Map<string, number>()

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      counts.set(path, (counts.get(path) ?? 0) + 1)
      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    await Promise.all([
      useOverviewDataStore.getState().loadOverview(query),
      useOverviewDataStore.getState().loadOverview(query),
    ])

    expect(counts.get('/api/v1/basins')).toBe(1)
    expect(counts.get('/api/v1/models')).toBe(1)
    // PR 4/7：默认 path 不再 fan-out per-layer valid-times（spec scenario "Metadata carries valid_times"）。
    expect(counts.get('/api/v1/layers/{layer_id}/valid-times')).toBeUndefined()
    // PR 4/7：默认 path 不再 fetch ranking（spec scenario "Default overview bootstrap omits ranking"）。
    expect(counts.get('/api/v1/flood-alerts/ranking')).toBeUndefined()
  })

  it('keeps stale overview responses from overwriting the latest request state', async () => {
    const delayedQuery = { ...query, cycle: '2026-05-18T03:00:00Z', validTime: '2026-05-18T03:00:00Z' }
    let delayedRunRequests = 0
    let releaseDelayedRuns: (() => void) | null = null
    const delayedRuns = new Promise<void>((resolve) => {
      releaseDelayedRuns = resolve
    })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }

      if (path === '/api/v1/runs' && options?.params?.query?.cycle_time === delayedQuery.cycle) {
        delayedRunRequests += 1
        await delayedRuns
      }

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const staleLoad = useOverviewDataStore.getState().loadOverview(delayedQuery)
    await vi.waitFor(() => expect(delayedRunRequests).toBe(2))

    const latestSnapshot = await useOverviewDataStore.getState().loadOverview(query)
    expect(useOverviewDataStore.getState().overview).toBe(latestSnapshot)

    releaseDelayedRuns?.()
    const staleSnapshot = await staleLoad

    expect(staleSnapshot).not.toBe(latestSnapshot)
    expect(useOverviewDataStore.getState().overview).toBe(latestSnapshot)
    expect(useOverviewDataStore.getState().overview?.summary.freshness.validTime).toBe('2026-05-18T06:00:00.000Z')
  })

  it('loads basin detail from existing APIs and preserves partial lineage failure as scoped state', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 123 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return { data: undefined, error: { error: { message: 'lineage unavailable' } } } as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', query)

    expect(snapshot.detail).toMatchObject({
      basinId: 'yangtze',
      selectedBasinVersionId: 'yangtze_v2026_01',
      boundary: basinVersion.geom,
      segmentCount: 1,
    })
    expect(snapshot.segments[0]).toMatchObject({ currentQ: 123, qUnit: 'm3/s', warningLevel: 'warning' })
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'seg-123',
      lineageStatus: 'failed',
      lineageUnavailableReason: '河段追溯暂不可用',
      handoffUrl:
        '/?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=yangtze_v2026_01&riverNetworkVersionId=yangtze_rivnet_v12&segmentId=seg-123',
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    })
    expect(calls.find((call) => call.path === '/api/v1/models')?.query).toMatchObject({
      basin_version_id: 'yangtze_v2026_01',
      active: 'true',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.query).toMatchObject({
      river_network_version_id: 'yangtze_rivnet_v12',
      issue_time: '2026-05-18T00:00:00Z',
      variables: 'q_down',
      scenarios: 'forecast_gfs_deterministic',
      include_analysis: true,
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')?.query).toMatchObject({
      run_id: 'run-gfs-1',
      segment_id: 'seg-123',
      valid_time: '2026-05-18T06:00:00Z',
      variable: 'q_down',
    })
  })

  // M26-2 store 护栏：basinId 经 query 入参（D2），且不污染既有取数键（R1 回归）。
  function mockHeiheBasinDetail() {
    const heiheBasin = { ...basin, basin_id: 'basins_heihe', basin_name: 'Heihe Basin' }
    const heiheVersion = { ...basinVersion, basin_version_id: 'heihe_v1', basin_id: 'basins_heihe' }
    const heiheModel = { ...model, model_id: 'heihe_shud', basin_id: 'basins_heihe', basin_version_id: 'heihe_v1' }
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/basins') return success([heiheBasin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([heiheVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [heiheModel], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success({ ...featureCollection, features: [], total: 0, feature_total: 0, limit: 0 }) as never
      if (path === '/api/v1/flood-alerts/ranking') return success({ ...ranking, items: [], total: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })
  }

  it('loads basin detail using basinId from query and matches only that basin', async () => {
    mockHeiheBasinDetail()
    const heiheQuery: M11QueryState = { ...defaultM11QueryState, source: 'gfs', basinId: 'basins_heihe', basinVersionId: null, segmentId: null }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basins_heihe', heiheQuery)

    expect(snapshot.requestScope.kind).toBe('basin-detail')
    expect(snapshot.requestScope.basinId).toBe('basins_heihe')
    expect(snapshot.detail.basinId).toBe('basins_heihe')
    // 匹配函数按 query 内 basinId 判定，命中当前查询
    expect(basinSnapshotMatchesQuery(snapshot, 'basins_heihe', heiheQuery)).toBe(true)
    // 不串到其他流域
    expect(basinSnapshotMatchesQuery(snapshot, 'basins_qhh', { ...heiheQuery, basinId: 'basins_qhh' })).toBe(false)
  })

  it('keeps basinId out of the request scope keys so it does not churn existing caches (R1)', async () => {
    mockHeiheBasinDetail()
    const baseQuery: M11QueryState = { ...defaultM11QueryState, source: 'gfs', basinId: null, basinVersionId: null, segmentId: null }
    const withBasinId: M11QueryState = { ...baseQuery, basinId: 'basins_heihe' }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basins_heihe', baseQuery)

    // 加 basinId 字段前后，经匹配函数的可观察行为一致：basinId 不进序列化键，零缓存 churn。
    expect(basinSnapshotMatchesQuery(snapshot, 'basins_heihe', baseQuery)).toBe(
      basinSnapshotMatchesQuery(snapshot, 'basins_heihe', withBasinId),
    )
    expect(basinSnapshotMatchesQuery(snapshot, 'basins_heihe', withBasinId)).toBe(true)
    // dataKey 本身不含 basinId 痕迹
    expect(snapshot.requestScope.dataKey).not.toContain('basins_heihe')
    expect(snapshot.requestScope.queryKey).not.toContain('basins_heihe')
  })

  it('does not retain basin snapshot row geometry beyond the aggregate river budget', async () => {
    const features = Array.from({ length: m11BasinRiverCollectionBudget.maxFeatures + 3 }, (_, index) => ({
      ...featureCollection.features[0],
      properties: {
        ...featureCollection.features[0].properties,
        segment_id: `seg-budget-${index}`,
        river_segment_id: `seg-budget-${index}`,
        name: `Budget Segment ${index}`,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [100.01, 30.01]] },
    }))
    const largeFeatureCollection = {
      ...featureCollection,
      total: features.length,
      feature_total: features.length,
      limit: features.length,
      features,
    }
    const largeRanking = {
      ...ranking,
      total: features.length,
      limit: features.length,
      items: features.map((feature, index) => ({
        ...ranking.items[0],
        rank: index + 1,
        river_segment_id: feature.properties.river_segment_id,
        segment_id: feature.properties.segment_id,
        segment_name: feature.properties.name,
        q_value: 200 + index,
      })),
    }

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(largeFeatureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(largeRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-budget-0',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-budget-0',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'seg-budget-0',
          river_segment_id: 'seg-budget-0',
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-budget-0', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', { ...query, segmentId: null })
    const retainedGeometryCount = snapshot.segments.filter((row) => row.geometry).length
    const skippedRow = snapshot.segments[m11BasinRiverCollectionBudget.maxFeatures]

    expect(snapshot.segments).toHaveLength(m11BasinRiverCollectionBudget.maxFeatures + 3)
    expect(retainedGeometryCount).toBeLessThanOrEqual(m11BasinRiverCollectionBudget.maxFeatures)
    expect(skippedRow).toMatchObject({
      riverSegmentId: `seg-budget-${m11BasinRiverCollectionBudget.maxFeatures}`,
      displayName: `Budget Segment ${m11BasinRiverCollectionBudget.maxFeatures}`,
      hasGeometry: false,
      geometry: null,
      currentQ: 200 + m11BasinRiverCollectionBudget.maxFeatures,
    })
    expect(skippedRow.unavailableReason).toContain('aggregate client rendering budget')
    expect(filterBasinSegmentRows(snapshot.segments, { warningLevel: null, q: 'Budget Segment 2002' })).toHaveLength(1)
  })

  it('treats basin search and warning filters as local list state outside the basin load identity', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []
    const multiFeatureCollection = {
      ...featureCollection,
      total: 2,
      feature_total: 2,
      features: [
        featureCollection.features[0],
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            segment_id: 'seg-456',
            river_segment_id: 'seg-456',
            name: 'North Branch 456',
          },
        },
      ],
    }

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(multiFeatureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const filteredQuery = { ...query, warningLevel: 'orange' as const, q: 'north' }
    const firstSnapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', query)
    const secondSnapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', filteredQuery)

    expect(secondSnapshot.requestScope.dataKey).toBe(firstSnapshot.requestScope.dataKey)
    expect(secondSnapshot.requestScope).toMatchObject({ warningLevel: null, q: null })
    expect(calls.filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')).toHaveLength(1)
    expect(calls.filter((call) => call.path === '/api/v1/runs')).toHaveLength(2)
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/ranking')?.query).not.toHaveProperty('warningLevel')
  })

  it('does not mark basin detail not-found when the basin list lookup fails', async () => {
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)

      if (path === '/api/v1/basins') return { data: undefined, error: { error: { message: 'basins unavailable' } } } as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 123 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ trace: [], nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', query)

    expect(calls).toContain('/api/v1/basins')
    expect(snapshot.detail.basinId).toBe('')
    expect(snapshot.detail.displayName).toBe('')
    expect(snapshot.detail.selectedBasinVersionId).toBe('yangtze_v2026_01')
    expect(snapshot.detail.unavailableReason).toBeNull()
    expect(snapshot.detail.partialErrors).toEqual(expect.arrayContaining(['basins: 暂不可用']))
    expect(snapshot.selectedSegment?.riverSegmentId).toBe('seg-123')
    expect(useOverviewDataStore.getState().basinError).toBe('basins: 暂不可用')
  })

  it('marks basin detail not-found when the basin list succeeds without the requested id', async () => {
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])

      if (path === '/api/v1/basins') return success([]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([]) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('not-a-real-basin', {
      ...defaultM11QueryState,
      segmentId: null,
      basinVersionId: null,
    })

    expect(snapshot.detail).toMatchObject({
      basinId: '',
      displayName: '',
      selectedBasinVersionId: null,
      unavailableReason: 'Basin was not found.',
    })
    expect(snapshot.selectedSegment).toBeNull()
  })

  it('resolves default best basin detail forecast requests to a concrete GFS or IFS scenario', async () => {
    const bestQuery = { ...query, source: 'best' as const, cycle: null }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run, ifsRun], total: 2, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_ifs_deterministic',
              source: 'IFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 120 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-ifs-1',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', bestQuery)

    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: undefined,
      cycle_time: undefined,
      status: 'frequency_done',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.query).toMatchObject({
      issue_time: 'latest',
      variables: 'q_down',
      scenarios: 'forecast_ifs_deterministic',
      include_analysis: true,
    })
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
    expect(snapshot.segments[0].source).toBe('IFS')
    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
    })
    expect(snapshot.selectedSegment?.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
    })
  })

  it('does not bind newer same-segment run surfaces to an explicitly selected older basin version', async () => {
    const oldVersion = { ...basinVersion, basin_version_id: 'yangtze_v2025_12', version_label: 'v2025_12', active_flag: false }
    const newVersion = { ...basinVersion, basin_version_id: 'yangtze_v2026_01', version_label: 'v2026_01', active_flag: true }
    const oldQuery = { ...query, basinVersionId: oldVersion.basin_version_id, segmentId: 'seg-123', source: 'best' as const, cycle: null }
    const newRun = {
      ...ifsRun,
      run_id: 'run-ifs-new-version',
      basin_version_id: newVersion.basin_version_id,
      cycle_time: '2026-05-19T00:00:00Z',
      updated_at: '2026-05-19T01:00:00Z',
    }
    const newRanking = {
      ...ranking,
      items: [
        {
          ...ranking.items[0],
          basin_version_id: newVersion.basin_version_id,
          q_value: 9999,
          return_period: 100,
          warning_level: 'severe',
          valid_time: '2026-05-19T06:00:00Z',
        },
      ],
    }
    const oldFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: oldVersion.basin_version_id,
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([oldVersion, newVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [newRun], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [{ ...model, basin_version_id: oldVersion.basin_version_id }], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(oldFeatures) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12_old',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(newRanking) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-19T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_ifs_deterministic',
              source: 'IFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-19T06:00:00Z', value: 9999 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-ifs-new-version',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-19T06:00:00Z', return_period: 100, warning_level: 'severe', q_value: 9999 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', oldQuery)

    expect(snapshot.detail.selectedBasinVersionId).toBe(oldVersion.basin_version_id)
    expect(snapshot.detail.latestRun.runId).toBeNull()
    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'Unknown',
      scenarioIds: [],
    })
    // ranking 在该 stale-version path 内被显式 settle（同 path 走 best 解析失败但仍 normalize），
    // warningDistribution 当为定义后再断言；narrow 是 PR 4/7 把 warningDistribution 改成 `| undefined` 的兼容写法。
    expect(snapshot.detail.warningDistribution).toEqual(expect.objectContaining({ severe: 0 }))
    expect(snapshot.segments[0]).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      currentQ: null,
      warningLevel: 'unavailable',
      source: 'Unknown',
    })
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      riverSegmentId: 'seg-123',
      currentQ: null,
      returnPeriod: null,
      warningLevel: 'unavailable',
      trendPoints: [],
      lineageStatus: 'unavailable',
    })
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([
        'flood ranking: No same-version concrete run is available for this basin/source.',
        'flood timeline: No same-version concrete run is available for this basin/source.',
        'lineage: No same-version concrete run is available for this basin/source.',
      ]),
    )
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/flood-alerts/ranking' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/flood-alerts/timeline' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/lineage/river-point' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: expect.stringContaining('forecast-series') })]))
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
  })

  it('paginates basin runs until an older selected version resolves its own concrete run', async () => {
    const oldVersion = { ...basinVersion, basin_version_id: 'yangtze_v2025_12', version_label: 'v2025_12', active_flag: false }
    const newVersion = { ...basinVersion, basin_version_id: 'yangtze_v2026_01', version_label: 'v2026_01', active_flag: true }
    const oldQuery = { ...query, basinVersionId: oldVersion.basin_version_id, segmentId: 'seg-123', source: 'best' as const, cycle: null }
    const newerRuns = Array.from({ length: 20 }, (_, index) => ({
      ...ifsRun,
      run_id: `run-ifs-new-version-${index}`,
      basin_version_id: newVersion.basin_version_id,
      cycle_time: `2026-05-${String(19 + index).padStart(2, '0')}T00:00:00Z`,
      updated_at: `2026-05-${String(19 + index).padStart(2, '0')}T01:00:00Z`,
    }))
    const oldRun = {
      ...run,
      run_id: 'run-gfs-old-version-late-page',
      basin_version_id: oldVersion.basin_version_id,
      cycle_time: '2026-05-18T00:00:00Z',
      updated_at: '2026-05-18T01:00:00Z',
    }
    const oldRanking = {
      ...ranking,
      items: [
        {
          ...ranking.items[0],
          basin_version_id: oldVersion.basin_version_id,
          q_value: 321,
          return_period: 10,
          warning_level: 'watch',
        },
      ],
    }
    const oldFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: oldVersion.basin_version_id,
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([oldVersion, newVersion]) as never
      if (path === '/api/v1/runs') {
        return options?.params?.query?.offset === 20
          ? (success({ items: [oldRun], total: 21, limit: 200, offset: 20 }) as never)
          : (success({ items: newerRuns, total: 21, limit: 20, offset: 0 }) as never)
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [{ ...model, basin_version_id: oldVersion.basin_version_id }], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(oldFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(oldRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12_old',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: oldRun.run_id,
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 10, warning_level: 'watch', q_value: 321 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', oldQuery)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.offset)).toEqual([0, 0, 20, 20])
    expect(calls.find((call) => call.path === '/api/v1/runs' && call.query?.offset === 20)?.query).toMatchObject({
      limit: 200,
      source: undefined,
      status: 'frequency_done',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/ranking')?.query).toMatchObject({
      run_id: oldRun.run_id,
      basin_id: 'yangtze',
    })
    expect(snapshot.detail.latestRun).toMatchObject({
      runId: oldRun.run_id,
      cycleTime: '2026-05-18T00:00:00.000Z',
      source: 'GFS',
    })
    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'GFS',
      scenarioIds: ['forecast_gfs_deterministic'],
    })
    expect(snapshot.segments[0]).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      currentQ: 321,
      returnPeriod: 10,
      warningLevel: 'watch',
      source: 'GFS',
    })
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      currentQ: 321,
      returnPeriod: 10,
      warningLevel: 'watch',
      lineageStatus: 'available',
    })
    expect(snapshot.detail.partialErrors).not.toEqual(
      expect.arrayContaining([
        'flood ranking: No same-version concrete run is available for this basin/source.',
        'flood timeline: No same-version concrete run is available for this basin/source.',
        'lineage: No same-version concrete run is available for this basin/source.',
      ]),
    )
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
  })

  it('keeps independent ready-run cursors when both statuses have additional pages', async () => {
    const selectedVersion = { ...basinVersion, basin_version_id: 'yangtze_v2025_12', version_label: 'v2025_12', active_flag: false }
    const newVersion = { ...basinVersion, basin_version_id: 'yangtze_v2026_01', version_label: 'v2026_01', active_flag: true }
    const selectedQuery = {
      ...query,
      basinVersionId: selectedVersion.basin_version_id,
      segmentId: 'seg-123',
      source: 'best' as const,
      cycle: null,
    }
    const firstPageRuns = (status: string) =>
      Array.from({ length: 20 }, (_, index) => ({
        ...ifsRun,
        run_id: `run-${status}-new-version-${index}`,
        status,
        basin_version_id: newVersion.basin_version_id,
        cycle_time: `2026-05-${String(19 + index).padStart(2, '0')}T00:00:00Z`,
        updated_at: `2026-05-${String(19 + index).padStart(2, '0')}T01:00:00Z`,
      }))
    const secondPageRuns = (status: string) =>
      Array.from({ length: 20 }, (_, index) => ({
        ...ifsRun,
        run_id: `run-${status}-middle-version-${index}`,
        status,
        basin_version_id: newVersion.basin_version_id,
        cycle_time: `2026-04-${String(10 + index).padStart(2, '0')}T00:00:00Z`,
        updated_at: `2026-04-${String(10 + index).padStart(2, '0')}T01:00:00Z`,
      }))
    const selectedRun = {
      ...run,
      run_id: 'run-published-selected-version-second-page',
      status: 'published',
      basin_version_id: selectedVersion.basin_version_id,
      river_network_version_id: 'yangtze_rivnet_v12',
      cycle_time: '2026-05-18T00:00:00Z',
      updated_at: '2026-05-18T01:30:00Z',
    }
    const selectedFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: selectedVersion.basin_version_id,
          },
        },
      ],
    }
    const selectedRanking = {
      ...ranking,
      items: [{ ...ranking.items[0], basin_version_id: selectedVersion.basin_version_id, q_value: 456, return_period: 20 }],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([selectedVersion, newVersion]) as never
      if (path === '/api/v1/runs') {
        const status = String(options?.params?.query?.status)
        const offset = Number(options?.params?.query?.offset ?? 0)
        if (offset === 20 && status === 'published') {
          return success({ items: [selectedRun, ...secondPageRuns(status).slice(1)], total: 60, limit: 20, offset }) as never
        }
        if (offset === 20) return success({ items: secondPageRuns(status), total: 60, limit: 20, offset }) as never
        return success({ items: firstPageRuns(status), total: 60, limit: 20, offset: 0 }) as never
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') {
        return success({ items: [{ ...model, basin_version_id: selectedVersion.basin_version_id }], total: 1, limit: 200, offset: 0 }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(selectedFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(selectedRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({ river_segment_id: 'seg-123', issue_time: selectedRun.cycle_time, variable: 'q_down', unit: 'm3/s', segments: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: selectedRun.run_id,
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', selectedQuery)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => `${call.query?.status}:${call.query?.offset}`)).toEqual([
      'frequency_done:0',
      'published:0',
      'frequency_done:20',
      'published:20',
    ])
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/ranking')?.query).toMatchObject({
      run_id: selectedRun.run_id,
      basin_id: 'yangtze',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      run_id: selectedRun.run_id,
      river_network_version_id: 'yangtze_rivnet_v12',
    })
    expect(snapshot.detail.latestRun.runId).toBe(selectedRun.run_id)
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: selectedVersion.basin_version_id,
      currentQ: 456,
      returnPeriod: 20,
      lineageStatus: 'available',
    })
  })

  it('uses published-only basin detail runs for flood ranking, timeline, and map rows', async () => {
    const publishedRun = { ...run, run_id: 'run-gfs-published', status: 'published', updated_at: '2026-05-18T01:30:00Z' }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') {
        return options?.params?.query?.status === 'published'
          ? (success({ items: [publishedRun], total: 1, limit: 20, offset: 0 }) as never)
          : (success({ items: [], total: 0, limit: 20, offset: 0 }) as never)
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({ river_segment_id: 'seg-123', issue_time: '2026-05-18T00:00:00Z', variable: 'q_down', unit: 'm3/s', segments: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: publishedRun.run_id,
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 123 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', query)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.status).sort()).toEqual([
      'frequency_done',
      'published',
    ])
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/ranking')?.query).toMatchObject({
      run_id: publishedRun.run_id,
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      run_id: publishedRun.run_id,
      river_network_version_id: 'yangtze_rivnet_v12',
    })
    expect(snapshot.detail.latestRun.runId).toBe(publishedRun.run_id)
    expect(snapshot.segments[0]).toMatchObject({ currentQ: 123, returnPeriod: 20, warningLevel: 'warning' })
    expect(snapshot.selectedSegment).toMatchObject({ currentQ: 123, returnPeriod: 20, warningLevel: 'warning' })
  })

  it('binds duplicate segment flood alerts to the matching river network version', async () => {
    const selectedQuery = { ...query, riverNetworkVersionId: 'yangtze_rivnet_selected' }
    const selectedModel = { ...model, river_network_version_id: 'yangtze_rivnet_selected' }
    const duplicateFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            river_network_version_id: 'yangtze_rivnet_selected',
          },
        },
      ],
    }
    const duplicateRanking = {
      ...ranking,
      total: 2,
      items: [
        {
          ...ranking.items[0],
          river_network_version_id: 'yangtze_rivnet_sibling',
          q_value: 999,
          return_period: 100,
          warning_level: 'severe',
        },
        {
          ...ranking.items[0],
          river_network_version_id: 'yangtze_rivnet_selected',
          q_value: 222,
          return_period: 5,
          warning_level: 'watch',
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [selectedModel], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(duplicateFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(duplicateRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: options?.params?.query?.river_network_version_id,
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({ river_segment_id: 'seg-123', issue_time: '2026-05-18T00:00:00Z', variable: 'q_down', unit: 'm3/s', segments: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 5, warning_level: 'watch', q_value: 222 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', selectedQuery)

    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')?.query).toMatchObject({
      river_network_version_id: 'yangtze_rivnet_selected',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      segment_id: 'seg-123',
      river_network_version_id: 'yangtze_rivnet_selected',
    })
    expect(snapshot.segments[0]).toMatchObject({
      riverNetworkVersionId: 'yangtze_rivnet_selected',
      currentQ: 222,
      returnPeriod: 5,
      warningLevel: 'watch',
    })
    expect(snapshot.selectedSegment).toMatchObject({
      riverNetworkVersionId: 'yangtze_rivnet_selected',
      currentQ: 222,
      returnPeriod: 5,
      warningLevel: 'watch',
    })
    expect(snapshot.selectedSegment).not.toMatchObject({ currentQ: 999, returnPeriod: 100, warningLevel: 'severe' })
  })

  it('ignores stale query river network when the run model resolves a newer network for the same segment id', async () => {
    const staleNetworkQuery = { ...query, riverNetworkVersionId: 'rnv_old', segmentId: 'seg-123' }
    const selectedModel = { ...model, river_network_version_id: 'rnv_new' }
    const selectedRun = { ...run, river_network_version_id: 'rnv_new' }
    const newNetworkFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            river_network_version_id: 'rnv_new',
          },
        },
      ],
    }
    const newNetworkRanking = {
      ...ranking,
      items: [
        {
          ...ranking.items[0],
          river_network_version_id: 'rnv_old',
          q_value: 999,
          return_period: 100,
          warning_level: 'severe',
        },
        {
          ...ranking.items[0],
          river_network_version_id: 'rnv_new',
          q_value: 234,
          return_period: 10,
          warning_level: 'watch',
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [selectedRun], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [selectedModel], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(newNetworkFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(newNetworkRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: selectedRun.cycle_time,
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 234 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: selectedRun.run_id,
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 10, warning_level: 'watch', q_value: 234 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') {
        return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', staleNetworkQuery)

    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')).toMatchObject({
      query: { river_network_version_id: 'rnv_new' },
    })
    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')).toMatchObject({
      query: { river_network_version_id: 'rnv_new' },
      pathParams: { segment_id: 'seg-123' },
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))).toMatchObject({
      query: { river_network_version_id: 'rnv_new' },
      pathParams: { segment_id: 'seg-123' },
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')).toMatchObject({
      query: { run_id: selectedRun.run_id, segment_id: 'seg-123', river_network_version_id: 'rnv_new' },
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')).toMatchObject({
      query: { run_id: selectedRun.run_id, segment_id: 'seg-123', river_network_version_id: 'rnv_new' },
    })
    expect(snapshot.selectedSegment).toMatchObject({
      riverNetworkVersionId: 'rnv_new',
      currentQ: 234,
      returnPeriod: 10,
      warningLevel: 'watch',
      lineageStatus: 'available',
    })
    expect(snapshot.selectedSegment?.handoffUrl).toContain('riverNetworkVersionId=rnv_new')
    expect(JSON.stringify(calls)).not.toContain('rnv_old')
  })

  it('stops same-version run lookup at the explicit cap and reports unavailable state when no match is found', async () => {
    const oldVersion = { ...basinVersion, basin_version_id: 'yangtze_v2025_12', version_label: 'v2025_12', active_flag: false }
    const newVersion = { ...basinVersion, basin_version_id: 'yangtze_v2026_01', version_label: 'v2026_01', active_flag: true }
    const oldQuery = { ...query, basinVersionId: oldVersion.basin_version_id, segmentId: 'seg-123', source: 'best' as const, cycle: null }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []
    const newVersionRuns = Array.from({ length: 200 }, (_, index) => ({
      ...ifsRun,
      run_id: `run-ifs-new-version-${index}`,
      basin_version_id: newVersion.basin_version_id,
    }))
    const oldFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: oldVersion.basin_version_id,
          },
        },
      ],
    }

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([oldVersion, newVersion]) as never
      if (path === '/api/v1/runs') {
        const offset = Number(options?.params?.query?.offset ?? 0)
        return success({
          items: offset === 0 ? newVersionRuns.slice(0, 20) : newVersionRuns,
          total: 100_000,
          limit: offset === 0 ? 20 : 200,
          offset,
        }) as never
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [{ ...model, basin_version_id: oldVersion.basin_version_id }], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(oldFeatures) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12_old',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', oldQuery)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.offset)).toEqual([
      0,
      0,
      20,
      20,
      220,
      220,
      420,
      420,
      620,
      620,
      820,
      820,
    ])
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/flood-alerts/ranking' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: expect.stringContaining('forecast-series') })]))
    expect(snapshot.detail.latestRun.runId).toBeNull()
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([
        'runs: Stopped same-version run lookup after 5 extra pages or 1000 retained runs.',
        'flood ranking: No same-version concrete run is available for this basin/source.',
        'flood timeline: No same-version concrete run is available for this basin/source.',
        'lineage: No same-version concrete run is available for this basin/source.',
      ]),
    )
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      riverSegmentId: 'seg-123',
      trendPoints: [],
      lineageStatus: 'unavailable',
    })
  })

  it('keeps basin detail populated when an extra same-version run page fails', async () => {
    const oldVersion = { ...basinVersion, basin_version_id: 'yangtze_v2025_12', version_label: 'v2025_12', active_flag: false }
    const newVersion = { ...basinVersion, basin_version_id: 'yangtze_v2026_01', version_label: 'v2026_01', active_flag: true }
    const oldQuery = { ...query, basinVersionId: oldVersion.basin_version_id, segmentId: 'seg-123', source: 'best' as const, cycle: null }
    const unknownOldRun = {
      ...run,
      run_id: 'run-custom-old-version',
      basin_version_id: oldVersion.basin_version_id,
      source_id: 'custom',
      scenario_id: 'forecast_custom_deterministic',
    }
    const newVersionRuns = Array.from({ length: 19 }, (_, index) => ({
      ...ifsRun,
      run_id: `run-ifs-new-version-${index}`,
      basin_version_id: newVersion.basin_version_id,
      cycle_time: `2026-05-${String(19 + index).padStart(2, '0')}T00:00:00Z`,
    }))
    const oldFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: oldVersion.basin_version_id,
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([oldVersion, newVersion]) as never
      if (path === '/api/v1/runs') {
        if (options?.params?.query?.offset === 20) {
          return { data: undefined, error: { error: { message: 'extra run page unavailable' } } } as never
        }
        return success({ items: [unknownOldRun, ...newVersionRuns], total: 21, limit: 20, offset: 0 }) as never
      }
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [{ ...model, basin_version_id: oldVersion.basin_version_id }], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(oldFeatures) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12_old',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', oldQuery)

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.offset)).toEqual([0, 0, 20, 20])
    expect(snapshot.detail).toMatchObject({
      basinId: 'yangtze',
      displayName: 'Yangtze Basin',
      selectedBasinVersionId: oldVersion.basin_version_id,
      segmentCount: 1,
      activeModelCount: 1,
    })
    expect(snapshot.detail.latestRun.runId).toBeNull()
    expect(snapshot.segments[0]).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      riverSegmentId: 'seg-123',
      currentQ: null,
      warningLevel: 'unavailable',
    })
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: oldVersion.basin_version_id,
      riverSegmentId: 'seg-123',
      lineageStatus: 'unavailable',
      trendPoints: [],
    })
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([
        'runs: Same-version run lookup failed before resolving the selected basin version run.',
        'flood ranking: No same-version concrete run is available for this basin/source.',
        'flood timeline: No same-version concrete run is available for this basin/source.',
        'lineage: No same-version concrete run is available for this basin/source.',
      ]),
    )
    expect(useOverviewDataStore.getState().basinError).toBe('runs: Same-version run lookup failed before resolving the selected basin version run.')
    expect(useOverviewDataStore.getState().basinError).not.toBe('加载流域数据失败')
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/flood-alerts/ranking' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/flood-alerts/timeline' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: '/api/v1/lineage/river-point' })]))
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: expect.stringContaining('forecast-series') })]))
  })

  it('keeps concrete best selected-segment provenance when forecast series is empty', async () => {
    const bestQuery = { ...query, source: 'best' as const, cycle: null }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [ifsRun], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: ifsRun.run_id,
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 123 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', bestQuery)

    expect(snapshot.selectedSegment?.trendPoints).toEqual([])
    expect(snapshot.selectedSegment?.currentQ).toBe(123)
    expect(snapshot.selectedSegment?.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
      cycleTime: '2026-05-18T00:00:00Z',
      unavailableReason: null,
    })
    expect(snapshot.selectedSegment?.freshness).toMatchObject({
      runId: ifsRun.run_id,
      source: 'IFS',
    })
    expect(snapshot.selectedSegment?.handoffUrl).toContain('/?source=ifs&')
    expect(snapshot.selectedSegment?.handoffUrl).not.toContain('source=best')
    expect(snapshot.layers.find((layer) => layer.layerId === 'flood-return-period')?.freshness).toMatchObject({
      runId: ifsRun.run_id,
      source: 'IFS',
    })
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
  })

  it('preserves IFS source provenance on basin segment rows', async () => {
    const ifsQuery = { ...query, source: 'ifs' as const }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [ifsRun], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'seg-123',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'seg-123',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_ifs_deterministic',
              source: 'IFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 120 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-ifs-1',
          segment_id: 'seg-123',
          river_segment_id: 'seg-123',
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', ifsQuery)

    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: 'IFS',
      cycle_time: '2026-05-18T00:00:00Z',
      status: 'frequency_done',
      flood_product_ready: true,
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.query).toMatchObject({
      issue_time: '2026-05-18T00:00:00Z',
      variables: 'q_down',
      scenarios: 'forecast_ifs_deterministic',
      include_analysis: true,
    })
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
    expect(snapshot.segments[0].source).toBe('IFS')
    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'ifs',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
    })
  })

  it('skips default best basin detail forecast requests when no concrete run resolves', async () => {
    const bestQuery = { ...query, source: 'best' as const, cycle: null }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { path?: Record<string, unknown> } }
      calls.push(path)

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', bestQuery)

    expect(calls).not.toContain('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series')
    expect(JSON.stringify(calls)).not.toContain('best_available')
    expect(JSON.stringify(calls)).not.toContain('forecast_best_available')
    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'Unknown',
      scenarioIds: [],
      unavailableReason: 'Requested source is not available in current payload.',
    })
    expect(snapshot.selectedSegment?.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'Unknown',
      scenarioIds: [],
    })
  })

  it('resolves divergent segment_id and river_segment_id to the backend river segment key', async () => {
    const divergentQuery = { ...query, segmentId: 'display-seg-123' }
    const divergentRanking = {
      ...ranking,
      items: [
        {
          ...ranking.items[0],
          river_segment_id: 'river-seg-123',
          segment_id: 'display-seg-123',
        },
      ],
    }
    const divergentFeatures = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            river_segment_id: 'river-seg-123',
            river_network_version_id: 'yangtze_rivnet_v12_selected',
            segment_id: 'display-seg-123',
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(divergentFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(divergentRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'display-seg-123',
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'river-seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', divergentQuery)

    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'river-seg-123',
      segmentId: 'display-seg-123',
      handoffUrl:
        '/?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=yangtze_v2026_01&riverNetworkVersionId=yangtze_rivnet_v12&segmentId=river-seg-123',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.pathParams,
    ).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.query,
    ).toMatchObject({
      river_network_version_id: 'yangtze_rivnet_v12',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')?.query,
    ).toMatchObject({
      river_network_version_id: 'yangtze_rivnet_v12',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.pathParams).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.query).toMatchObject({
      river_network_version_id: 'yangtze_rivnet_v12',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')?.query).toMatchObject({
      segment_id: 'river-seg-123',
      river_network_version_id: 'yangtze_rivnet_v12',
    })
  })

  it('resolves a filtered query segment from the full feature collection without falling back to row zero', async () => {
    const filteredQuery = { ...query, segmentId: 'filtered-display', warningLevel: 'warning' as const }
    const mixedFeatures = {
      ...featureCollection,
      total: 2,
      feature_total: 2,
      features: [
        featureCollection.features[0],
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            river_segment_id: 'filtered-river',
            segment_id: 'filtered-display',
            name: 'Filtered Segment',
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(mixedFeatures) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: 'filtered-display',
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'filtered-river', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', filteredQuery)

    expect(snapshot.segments).toHaveLength(2)
    expect(filterBasinSegmentRows(snapshot.segments, filteredQuery)).toMatchObject([{ riverSegmentId: 'seg-123' }])
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'filtered-river',
      segmentId: 'filtered-display',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.pathParams,
    ).toMatchObject({
      segment_id: 'filtered-river',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.pathParams).toMatchObject({
      segment_id: 'filtered-river',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      segment_id: 'filtered-river',
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')?.query).toMatchObject({
      segment_id: 'filtered-river',
    })
  })

  it('paginates river segments until a requested later-page segment binds same-version detail surfaces', async () => {
    const laterQuery = { ...query, segmentId: 'late-display' }
    const firstPageFeature = {
      ...featureCollection.features[0],
      properties: {
        ...featureCollection.features[0].properties,
        river_segment_id: 'first-river',
        segment_id: 'first-display',
        name: 'First Segment',
      },
    }
    const laterFeature = {
      ...featureCollection.features[0],
      properties: {
        ...featureCollection.features[0].properties,
        river_segment_id: 'late-river',
        segment_id: 'late-display',
        name: 'Late Segment',
      },
    }
    const laterRanking = {
      ...ranking,
      items: [
        {
          ...ranking.items[0],
          river_segment_id: 'late-river',
          segment_id: 'late-display',
          segment_name: 'Late Segment',
          q_value: 456,
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        return options?.params?.query?.offset === 1000
          ? (success({
              ...featureCollection,
              total: 1001,
              feature_total: 1001,
              limit: 1000,
              offset: 1000,
              features: [laterFeature],
            }) as never)
          : (success({
              ...featureCollection,
              total: 1001,
              feature_total: 1001,
              limit: 1000,
              offset: 0,
              features: [firstPageFeature],
            }) as never)
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(laterRanking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 2,
          downstream_segment_id: null,
          length_m: 2000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 456 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: run.run_id,
          segment_id: 'late-display',
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'late-river', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', laterQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0, 1000])
    expect(snapshot.detail.segmentCount).toBe(1001)
    expect(snapshot.segments.map((row) => row.riverSegmentId)).toEqual(['first-river', 'late-river'])
    expect(snapshot.segments.find((row) => row.riverSegmentId === 'late-river')).toMatchObject({
      basinVersionId: 'yangtze_v2026_01',
      currentQ: 456,
      source: 'GFS',
    })
    expect(snapshot.selectedSegment).toMatchObject({
      basinVersionId: 'yangtze_v2026_01',
      riverSegmentId: 'late-river',
      segmentId: 'late-display',
      currentQ: 456,
      lineageStatus: 'available',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.pathParams,
    ).toMatchObject({ basin_version_id: 'yangtze_v2026_01', segment_id: 'late-river' })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.pathParams).toMatchObject({
      basin_version_id: 'yangtze_v2026_01',
      segment_id: 'late-river',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      run_id: run.run_id,
      segment_id: 'late-river',
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')?.query).toMatchObject({
      run_id: run.run_id,
      segment_id: 'late-river',
    })
    expect(snapshot.detail.partialErrors).not.toEqual(expect.arrayContaining([expect.stringContaining('river segments: Stopped')]))
  })

  it('reports partial river-segment state when the requested segment is beyond the pagination cap', async () => {
    const missingLateQuery = { ...query, segmentId: 'beyond-cap' }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        const offset = Number(options?.params?.query?.offset ?? 0)
        return success({
          ...featureCollection,
          total: 20_000,
          feature_total: 20_000,
          limit: 1000,
          offset,
          features: [
            {
              ...featureCollection.features[0],
              properties: {
                ...featureCollection.features[0].properties,
                river_segment_id: `page-river-${offset}`,
                segment_id: `page-display-${offset}`,
              },
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: 'beyond-cap',
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: 'beyond-cap',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 654 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: run.run_id,
          segment_id: 'beyond-cap',
          river_segment_id: 'beyond-cap',
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') {
        return success({ target_type: 'river_point', target_id: 'beyond-cap', nodes: [], edges: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', missingLateQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000])
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'beyond-cap',
      currentQ: 654,
      lineageStatus: 'available',
    })
    expect(snapshot.detail.segmentCount).toBe(20_000)
    expect(snapshot.detail.partialErrors).toContain(
      'river segments: Stopped segment lookup after 10 pages or 10000 features before the requested segment was found.',
    )
    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')).toBeDefined()
  })

  it('bounds an oversized first river-segment page and still fetches a selected segment detail directly', async () => {
    const cappedQuery = { ...query, segmentId: 'first-page-truncated-selected' }
    const firstPageFeatures = Array.from({ length: RIVER_SEGMENT_RETAINED_ITEM_CAP + 1 }, (_, index) => ({
      ...featureCollection.features[0],
      properties: {
        ...featureCollection.features[0].properties,
        river_segment_id: `first-page-river-${index}`,
        segment_id: `first-page-display-${index}`,
        name: `First Page Segment ${index}`,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [100.01, 30.01]] },
    }))
    const oversizedFirstPage = {
      ...featureCollection,
      total: firstPageFeatures.length,
      feature_total: firstPageFeatures.length,
      limit: firstPageFeatures.length,
      offset: 0,
      features: firstPageFeatures,
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(oversizedFirstPage) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 321 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: run.run_id,
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') {
        return success({ target_type: 'river_point', target_id: cappedQuery.segmentId, nodes: [], edges: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', cappedQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0])
    expect(snapshot.detail.segmentCount).toBe(firstPageFeatures.length)
    expect(snapshot.segments).toHaveLength(RIVER_SEGMENT_RETAINED_ITEM_CAP)
    expect(snapshot.segments.at(-1)?.riverSegmentId).toBe(`first-page-river-${RIVER_SEGMENT_RETAINED_ITEM_CAP - 1}`)
    expect(snapshot.segments.some((row) => row.riverSegmentId === cappedQuery.segmentId)).toBe(false)
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([
        expect.stringContaining(`Retained only the first ${RIVER_SEGMENT_RETAINED_ITEM_CAP} features from an oversized river-segment page`),
        expect.stringContaining('Stopped segment lookup after 10 pages or 10000 features before the requested segment was found.'),
      ]),
    )
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: cappedQuery.segmentId,
      currentQ: 321,
      lineageStatus: 'available',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.pathParams,
    ).toMatchObject({ basin_version_id: 'yangtze_v2026_01', segment_id: cappedQuery.segmentId })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.pathParams).toMatchObject({
      basin_version_id: 'yangtze_v2026_01',
      segment_id: cappedQuery.segmentId,
    })
  })

  it('pages through every river-segment page (full network) even when no segment is requested', async () => {
    const noSegmentQuery = { ...query, segmentId: null }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        const offset = Number(options?.params?.query?.offset ?? 0)
        return success({
          ...featureCollection,
          total: 3,
          feature_total: 3,
          limit: 1,
          offset,
          features: [
            {
              ...featureCollection.features[0],
              properties: {
                ...featureCollection.features[0].properties,
                river_segment_id: `full-river-${offset}`,
                segment_id: `full-display-${offset}`,
              },
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', noSegmentQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0, 1, 2])
    expect(snapshot.segments).toHaveLength(3)
    expect(snapshot.detail.partialErrors).not.toEqual(expect.arrayContaining([expect.stringContaining('river segments')]))
  })

  it('halves the page limit and retries when the server rejects a page with the GeoJSON budget (413)', async () => {
    const noSegmentQuery = { ...query, segmentId: null }
    const budgetError = {
      data: undefined,
      error: {
        status: 'error',
        error: {
          code: 'RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED',
          message: 'River segment GeoJSON payload budget exceeded; request fewer segments or a more specific river network.',
        },
      },
    }
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        const limit = Number(options?.params?.query?.limit ?? 0)
        if (limit > 250) return budgetError as never
        return success({ ...featureCollection, total: 1, feature_total: 1, limit: 1, offset: 0 }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', noSegmentQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.limit),
    ).toEqual([500, 250])
    expect(snapshot.segments).toHaveLength(1)
    expect(snapshot.detail.partialErrors).not.toEqual(expect.arrayContaining([expect.stringContaining('river segments')]))
  })

  it('scopes partial river collections and direct selected detail to the active run model network', async () => {
    const cappedQuery = { ...query, segmentId: 'selected-only-in-detail' }
    const siblingModel = {
      ...model,
      model_id: 'yangtze_shud_sibling',
      river_network_version_id: 'yangtze_rivnet_sibling',
      created_at: '2026-05-03T00:00:00Z',
    }
    const selectedModel = {
      ...model,
      model_id: run.model_id,
      river_network_version_id: 'yangtze_rivnet_selected_run',
      created_at: '2026-05-02T00:00:00Z',
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [siblingModel, selectedModel], total: 2, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        return success({
          ...featureCollection,
          total: 20_000,
          feature_total: 20_000,
          limit: 1000,
          offset: Number(options?.params?.query?.offset ?? 0),
          features: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: run.run_id,
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') {
        return success({ target_type: 'river_point', target_id: cappedQuery.segmentId, nodes: [], edges: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', cappedQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.river_network_version_id),
    ).toEqual(Array(10).fill('yangtze_rivnet_selected_run'))
    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_selected_run' },
      pathParams: { segment_id: cappedQuery.segmentId },
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_selected_run' },
      pathParams: { segment_id: cappedQuery.segmentId },
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_selected_run' },
    })
    expect(snapshot.selectedSegment?.riverNetworkVersionId).toBe('yangtze_rivnet_selected_run')
    expect(JSON.stringify(calls)).not.toContain('yangtze_rivnet_sibling')
  })

  it('resolves an inactive selected-run model exactly before scoping basin river geometry', async () => {
    const historicalRun = {
      ...run,
      run_id: 'run-gfs-historical',
      model_id: 'yangtze_shud_historical',
      cycle_time: '2026-05-17T00:00:00Z',
      updated_at: '2026-05-17T01:00:00Z',
    }
    const activeModel = {
      ...model,
      model_id: 'yangtze_shud_active',
      river_network_version_id: 'yangtze_rivnet_active',
      active_flag: true,
      created_at: '2026-05-18T00:00:00Z',
    }
    const historicalModel = {
      ...model,
      model_id: historicalRun.model_id,
      river_network_version_id: 'yangtze_rivnet_historical',
      active_flag: false,
      created_at: '2026-05-17T00:00:00Z',
    }
    const historicalCollection = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            river_network_version_id: historicalModel.river_network_version_id,
          },
        },
      ],
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [historicalRun], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [activeModel], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/models/{model_id}') return success(historicalModel) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(historicalCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: historicalRun.cycle_time,
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: historicalRun.run_id,
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') {
        return success({ target_type: 'river_point', target_id: query.segmentId, nodes: [], edges: [] }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', {
      ...query,
      cycle: historicalRun.cycle_time,
    })

    expect(calls.find((call) => call.path === '/api/v1/models')).toMatchObject({
      query: { basin_version_id: 'yangtze_v2026_01', active: 'true' },
    })
    expect(calls.find((call) => call.path === '/api/v1/models/{model_id}')).toMatchObject({
      pathParams: { model_id: historicalRun.model_id },
    })
    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_historical' },
    })
    expect(calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_historical' },
      pathParams: { segment_id: query.segmentId },
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_historical' },
      pathParams: { segment_id: query.segmentId },
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')).toMatchObject({
      query: { river_network_version_id: 'yangtze_rivnet_historical' },
    })
    expect(snapshot.selectedSegment?.modelId).toBe(historicalRun.model_id)
    expect(snapshot.selectedSegment?.riverNetworkVersionId).toBe('yangtze_rivnet_historical')
    expect(JSON.stringify(calls)).not.toContain('yangtze_rivnet_active')
  })

  it('pages default basin detail up to the client cap and reports a partial network honestly', async () => {
    const defaultQuery = { ...query, segmentId: null }
    const largeFirstPage = {
      ...featureCollection,
      total: 20_000,
      feature_total: 20_000,
      limit: 1000,
      offset: 0,
    }
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(largeFirstPage) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 123 }],
            },
          ],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: run.run_id,
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          timesteps: [],
          timeline: [],
          peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 123 },
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ target_type: 'river_point', target_id: 'seg-123', nodes: [], edges: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', defaultQuery)

    // 全河段语义：无 segmentId 也持续翻页取齐河网，直到 MAX_PAGES 上限。
    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000])
    expect(snapshot.detail.segmentCount).toBe(20_000)
    expect(snapshot.segments[0].riverSegmentId).toBe('seg-123')
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'seg-123',
      segmentId: 'seg-123',
      currentQ: 123,
      lineageStatus: 'available',
    })
    expect(snapshot.detail.partialErrors).not.toEqual(expect.arrayContaining([expect.stringContaining('river segments: Stopped')]))
    // cap 截断的河网要诚实标注 partial（partialErrors[0] 同步上浮为 basinError 通知）。
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([expect.stringContaining('the map river network is partial')]),
    )
    expect(useOverviewDataStore.getState().basinError).toContain('the map river network is partial')
  })

  it('does not issue selected-segment detail requests for an invalid query segment when row zero exists', async () => {
    const invalidQuery = { ...query, segmentId: 'missing-segment' }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      calls.push(path)

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', invalidQuery)

    expect(snapshot.segments[0].riverSegmentId).toBe('seg-123')
    expect(snapshot.selectedSegment).toBeNull()
    expect(calls).not.toContain('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')
    expect(calls).not.toContain('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series')
    expect(calls).not.toContain('/api/v1/flood-alerts/timeline')
    expect(calls).not.toContain('/api/v1/lineage/river-point')
  })

  it('marks compare basin ranking, timeline, and lineage unavailable instead of binding them to one run', async () => {
    const compareQuery = { ...query, source: 'compare' as const }
    const calls: string[] = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { path?: Record<string, unknown> } }
      calls.push(path)

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run, ifsRun], total: 2, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(featureCollection) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 123 }],
            },
            {
              scenario: 'forecast_ifs_deterministic',
              source: 'IFS',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 120 }],
            },
          ],
        }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', compareQuery)

    expect(snapshot.detail.sourceSelection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'GFS+IFS',
      comparisonAvailable: true,
    })
    expect(snapshot.detail.latestRun.runId).toBeNull()
    // PR 4/7 之后 warningDistribution 是 `| undefined`，用 objectContaining narrow 兼容空态。
    expect(snapshot.detail.warningDistribution).toEqual(expect.objectContaining({ warning: 0 }))
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'seg-123',
      lineageStatus: 'unavailable',
      lineageUnavailableReason: '对比模式河段追溯需要 GFS+IFS 聚合端点',
      comparisonAvailable: true,
    })
    expect(snapshot.detail.partialErrors).toEqual(
      expect.arrayContaining([
        'flood ranking: 对比模式洪水排名需要 GFS+IFS 聚合端点',
        'flood timeline: 对比模式洪水时间线需要 GFS+IFS 聚合端点',
        'lineage: 对比模式河段追溯需要 GFS+IFS 聚合端点',
      ]),
    )
    expect(calls).not.toContain('/api/v1/flood-alerts/ranking')
    expect(calls).not.toContain('/api/v1/flood-alerts/timeline')
    expect(calls).not.toContain('/api/v1/lineage/river-point')
  })

  it('keeps stale basin detail responses from overwriting the latest basin request state', async () => {
    const staleQuery = { ...query, segmentId: 'seg-stale' }
    const staleFeatureCollection = {
      ...featureCollection,
      features: [
        ...featureCollection.features,
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            segment_id: 'seg-stale',
            river_segment_id: 'seg-stale',
            name: 'Stale Segment',
          },
        },
      ],
      total: 2,
      feature_total: 2,
    }
    let releaseStaleSegment: ((value: ReturnType<typeof success<typeof featureCollection>>) => void) | null = null

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { path?: Record<string, unknown>; query?: Record<string, unknown> } }

      if (
        path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}' &&
        options?.params?.path?.segment_id === staleQuery.segmentId
      ) {
        await new Promise((resolve) => {
          releaseStaleSegment = resolve
        })
      }

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') return success([]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return success(staleFeatureCollection) as never
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          river_network_version_id: 'yangtze_rivnet_v12',
          segment_order: 1,
          downstream_segment_id: null,
          length_m: 1000,
          geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
          properties_json: {},
          created_at: '2026-05-01T00:00:00Z',
        }) as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
        return success({
          river_segment_id: options?.params?.path?.segment_id,
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          frequency_thresholds: null,
          segments: [],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/timeline') {
        return success({
          run_id: 'run-gfs-1',
          segment_id: options?.params?.query?.segment_id,
          river_segment_id: options?.params?.query?.segment_id,
          river_network_version_id: options?.params?.query?.river_network_version_id,
          timesteps: [],
          timeline: [],
          peak: null,
          frequency_thresholds: null,
          quality_note: null,
        }) as never
      }
      if (path === '/api/v1/lineage/river-point') return success({ status: 'available', upstream: [], artifacts: [] }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const staleLoad = useOverviewDataStore.getState().loadBasinDetail('yangtze', staleQuery)
    await vi.waitFor(() => expect(releaseStaleSegment).toBeTypeOf('function'))

    const latestSnapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', query)
    expect(useOverviewDataStore.getState().basinDetail).toBe(latestSnapshot)

    releaseStaleSegment?.(success(featureCollection))
    const staleSnapshot = await staleLoad

    expect(staleSnapshot).not.toBe(latestSnapshot)
    expect(useOverviewDataStore.getState().basinDetail).toBe(latestSnapshot)
    expect(useOverviewDataStore.getState().basinDetail?.selectedSegment?.riverSegmentId).toBe(query.segmentId)
  })

  // PR 3/7 #582 — loadOverview 拆分为 mapBootstrap / enrichment 两阶段（spec
  // "Map interactivity is decoupled from enrichment loading"）。下面的测试覆盖：
  // (a) 4 状态矩阵（00 初始 / 10 phase1 in-flight / 01 phase2 in-flight / 11 同帧调用）；
  // (b) phase 1 settle 不依赖 fetchRuns；
  // (c) phase 1 reject → mapBootstrapLoading=false + scoped bootstrap error 暴露；
  // (d) enrichment 单点 reject → scoped panel error；mapBootstrapLoading 保持 false；map 可交互。
  describe('mapBootstrap vs enrichment loading split (PR 3/7)', () => {
    // (a)-00 初始（未 loadOverview）：spec scenario "Initial state before loadOverview"
    it('initial state before loadOverview: both flags false and overview null', () => {
      const state = useOverviewDataStore.getState()
      expect(state.mapBootstrapLoading).toBe(false)
      expect(state.enrichmentLoading).toBe(false)
      expect(state.overview).toBeNull()
      expect(state.bootstrapError).toBeNull()
    })

    // (a)-10/01/11 + (b) phase 1 不依赖 fetchRuns / phase 1 settle 写 bootstrap：
    // 用受控的 runs/pipeline 仅阻塞 enrichment；basins/layers 立刻成功 → phase 1 settle，
    // 此时观察 mapBootstrapLoading=false 且 enrichmentLoading=true（state 01 = phase 2 in-flight），
    // 同时 overview.bootstrap 已写入；释放 runs 后 enrichmentLoading=false（state 00 settle）。
    it('phase 1 settles independently of fetchRuns and writes bootstrap snapshot', async () => {
      let releaseRuns: ((value: unknown) => void) | null = null
      const runsPromise = new Promise<unknown>((resolve) => {
        releaseRuns = resolve
      })
      const fetchRunsObserved: string[] = []

      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        if (path === '/api/v1/basins') return success([basin]) as never
        if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
        if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') {
          fetchRunsObserved.push('runs')
          return (await runsPromise) as never
        }
        if (path === '/api/v1/layers') {
          // metadata.valid_times 携带，phase 1 直接消费、无需 /layers/<id>/valid-times fan-out。
          return success([
            {
              layer_id: 'flood-return-period',
              layer_name: 'Flood return period',
              layer_type: 'hydrology',
              variables: [],
              metadata: { valid_times: ['2026-05-18T06:00:00Z'] },
            },
          ]) as never
        }
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
        if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
        if (path === '/api/v1/flood-alerts/summary') {
          return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
        }
        if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      const load = useOverviewDataStore.getState().loadOverview(query)

      // (a)-11 同帧：两 flag 都为 true（call site 刚同步发出 loadOverview）。
      const initialState = useOverviewDataStore.getState()
      expect(initialState.mapBootstrapLoading).toBe(true)
      expect(initialState.enrichmentLoading).toBe(true)
      expect(initialState.overview).toBeNull()

      // 等 phase 1 settle（basins + runless layers 都立刻成功 → 一个 microtask 队列轮转就能解锁）。
      await vi.waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))

      // (a)-01：phase 1 已 settle、phase 2 仍 in-flight；
      // (b)：phase 1 settle 不需要 fetchRuns 完成（runs 仍 pending）。
      const phase1State = useOverviewDataStore.getState()
      expect(phase1State.mapBootstrapLoading).toBe(false)
      expect(phase1State.enrichmentLoading).toBe(true)
      expect(phase1State.overview?.bootstrap).not.toBeNull()
      expect(phase1State.overview?.bootstrap?.basins).toEqual([basin])
      expect(phase1State.overview?.bootstrap?.layers.map((layer) => layer.layer_id)).toEqual(['flood-return-period'])
      expect(phase1State.overview?.bootstrap?.layerStates.some((s) => s.layerId === 'flood-return-period')).toBe(true)
      expect(phase1State.bootstrapError).toBeNull()
      // fetchRuns 已发但未 resolve → phase 1 仍 settle 通过（不阻塞）。
      expect(fetchRunsObserved.length).toBeGreaterThan(0)

      // 释放 enrichment → 两个 flag 都解锁（state 00 settle）。
      releaseRuns?.(success({ items: [run], total: 1, limit: 20, offset: 0 }))
      await load

      const finalState = useOverviewDataStore.getState()
      expect(finalState.mapBootstrapLoading).toBe(false)
      expect(finalState.enrichmentLoading).toBe(false)
      expect(finalState.overview?.bootstrap).not.toBeNull()
      expect(finalState.bootstrapError).toBeNull()
    })

    // (c) phase 1 reject（fetchBasins 失败）→ mapBootstrapLoading=false + bootstrapError；
    // enrichment 仍可独立推进（不阻塞 phase 2 promise）。
    it('phase 1 rejection exposes scoped bootstrap error without blocking enrichment', async () => {
      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        if (path === '/api/v1/basins') {
          return { data: undefined, error: { error: { message: 'basins exploded' } } } as never
        }
        if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
        if (path === '/api/v1/layers') {
          return success([{ layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: [], metadata: null }]) as never
        }
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
        if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
        if (path === '/api/v1/flood-alerts/summary') {
          return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
        }
        if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      await useOverviewDataStore.getState().loadOverview(query)

      const finalState = useOverviewDataStore.getState()
      expect(finalState.mapBootstrapLoading).toBe(false)
      expect(finalState.enrichmentLoading).toBe(false)
      // scoped bootstrap error 写入 bootstrapError（不与 enrichment 通用 error 共流）。
      expect(finalState.bootstrapError).toBeTruthy()
      expect(finalState.bootstrapError).toMatch(/basins/)
      // overview.bootstrap 仍为 null 表示 mapBootstrap 失败；OverviewPage 据此走"bootstrap failed"分支。
      expect(finalState.overview?.bootstrap ?? null).toBeNull()
    })

    // (d) enrichment 单点 reject（tasks.md 3.4(d) 枚举 4 端点：pipeline / queue / flood summary /
    // basin versions）→ scoped partial error 仅暴露在 panel；mapBootstrap 仍正常 settle；map 可交互
    // （overview.bootstrap 非空 + bootstrapError 为 null）。每条 case 只 reject 一个端点，其余正常。
    // 注：basin versions 需要 shouldFetchVersions=true（initialRequestCount<=8），该阈值在 hasLatestRun
    // 时被超过 → basin versions case 显式让 /api/v1/runs 返回空 page（latestRun=null → baseRequestCount
    // 从 7 降到 5 → versions 被请求 → reject 才能透出 partial error），不污染其它端点。
    describe.each([
      {
        label: 'pipeline status',
        rejectPath: '/api/v1/pipeline/status',
        rejectMessage: 'pipeline downstream timeout',
        partialErrorEntry: 'pipeline: 暂不可用',
        errorPattern: /pipeline/,
        runsReturnsEmpty: false,
      },
      {
        label: 'queue depth',
        rejectPath: '/api/v1/queue/depth',
        rejectMessage: 'queue downstream timeout',
        partialErrorEntry: 'queue: 暂不可用',
        errorPattern: /queue/,
        runsReturnsEmpty: false,
      },
      {
        label: 'flood summary',
        rejectPath: '/api/v1/flood-alerts/summary',
        rejectMessage: 'flood summary downstream timeout',
        partialErrorEntry: 'flood summary: 暂不可用',
        errorPattern: /flood summary/,
        runsReturnsEmpty: false,
      },
      {
        label: 'basin versions',
        rejectPath: '/api/v1/basins/{basin_id}/versions',
        rejectMessage: 'basin versions downstream timeout',
        partialErrorEntry: 'basin versions: 暂不可用',
        errorPattern: /basin versions/,
        // versions 仅在 initialRequestCount<=8 时才发，需要 hasLatestRun=false → /api/v1/runs 空 page。
        runsReturnsEmpty: true,
      },
    ])(
      'enrichment single-point rejection ($label) does not block map interactivity',
      ({ rejectPath, rejectMessage, partialErrorEntry, errorPattern, runsReturnsEmpty }) => {
        it('keeps mapBootstrap settled + scoped partial error surfaced (panel only)', async () => {
          vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
            const path = String(args[0])
            // 阶段 2 单点失败：被点名端点拒绝；其它端点正常返回。
            if (path === rejectPath) {
              return { data: undefined, error: { error: { message: rejectMessage } } } as never
            }
            if (path === '/api/v1/basins') return success([basin]) as never
            if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
            if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
            if (path === '/api/v1/runs') {
              return success({ items: runsReturnsEmpty ? [] : [run], total: runsReturnsEmpty ? 0 : 1, limit: 20, offset: 0 }) as never
            }
            if (path === '/api/v1/layers') {
              return success([
                {
                  layer_id: 'flood-return-period',
                  layer_name: 'Flood return period',
                  layer_type: 'hydrology',
                  variables: [],
                  metadata: { valid_times: ['2026-05-18T06:00:00Z'] },
                },
              ]) as never
            }
            if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
            if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
            if (path === '/api/v1/flood-alerts/summary') {
              return success({ run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
            }
            if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
            if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
            throw new Error(`Unexpected GET ${path}`)
          })

          await useOverviewDataStore.getState().loadOverview(query)

          const finalState = useOverviewDataStore.getState()
          // 关键合同：map 仍可交互（mapBootstrap 已 settle + bootstrap 已写入 + bootstrapError 为 null）。
          expect(finalState.mapBootstrapLoading).toBe(false)
          expect(finalState.bootstrapError).toBeNull()
          expect(finalState.overview?.bootstrap).not.toBeNull()
          expect(finalState.overview?.bootstrap?.basins).toEqual([basin])
          // enrichment 完成（即便单点 reject），但 scoped partial error 透出到 panel level。
          expect(finalState.enrichmentLoading).toBe(false)
          expect(finalState.overview?.summary.partialErrors).toEqual(expect.arrayContaining([partialErrorEntry]))
          // 通用 error 字段拿到 enrichment partial error 摘要（既有契约：partialErrors[0] → error），
          // 仅 panel/notice 消费，不阻塞 map（OverviewPage.surfaceSettling 只看 mapBootstrap+bootstrap）。
          expect(finalState.error).toMatch(errorPattern)
        })
      },
    )
  })

  // PR 4/7：dead-call 删除 + 按需 ranking + metadata-first valid_times。
  // 覆盖 spec capability "overview-data-contracts" 的两个 Requirement scenarios + spec capability
  // "frontend-mvt-layer-consumption" 的 metadata-first regression。
  describe('dead-call removal + on-demand ranking (PR 4/7)', () => {
    // 4.5 (a)：默认 path 不发 ranking；调用 loadFloodRankingOnDemand 才触发；同 key 并发去重。
    it('ranking is not fetched on default loadOverview and is issued on-demand when requested', async () => {
      const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        const options = args[1] as { params?: { query?: Record<string, unknown> } }
        calls.push({ path, query: options?.params?.query })
        if (path === '/api/v1/basins') return success([basin]) as never
        if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
        if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
        if (path === '/api/v1/layers') {
          return success([
            {
              layer_id: 'flood-return-period',
              layer_name: 'Flood return period',
              layer_type: 'hydrology',
              variables: [],
              metadata: { valid_times: ['2026-05-18T06:00:00Z'] },
            },
          ]) as never
        }
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
        if (path === '/api/v1/flood-alerts/summary') {
          return success({ run_id: run.run_id, total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
        }
        if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      await useOverviewDataStore.getState().loadOverview(query)

      // 默认 path：ranking 缺席。
      expect(calls.map((call) => call.path)).not.toContain('/api/v1/flood-alerts/ranking')
      // ranking 数据未反映在 overview snapshot 上：basin warningCounts === undefined（pending），
      // 等按需 fetch settle 才覆盖。
      expect(useOverviewDataStore.getState().overview?.basins[0].warningCounts).toBeUndefined()

      // 第一次按需调用：触发 fetch。
      const firstResult = await loadFloodRankingOnDemand(run.run_id, query)
      expect(firstResult.items).toHaveLength(1)
      const firstCount = calls.filter((c) => c.path === '/api/v1/flood-alerts/ranking').length
      expect(firstCount).toBe(1)

      // 同 key 第二次调用：模块级 cached() 命中，不再发新 RTT。
      const secondResult = await loadFloodRankingOnDemand(run.run_id, query)
      expect(secondResult).toBe(firstResult)
      expect(calls.filter((c) => c.path === '/api/v1/flood-alerts/ranking').length).toBe(firstCount)
    })

    // 4.5 (a)：concurrent 面板挂载 coalesce 到同一 in-flight promise（只发一次网络 RTT）。
    it('coalesces concurrent on-demand ranking requests through the in-flight cache', async () => {
      // Promise constructor 同步运行 callback → release 在下一句即被赋值；用 definite-assignment
      // assertion 跳开 `null` 初值，避免 TS control-flow 把闭包内赋值的回写 narrow 成 `never`
      // 后再触发 TS2349 "expression is not callable"（PR 内同 pattern 的旧测试也踩过该坑）。
      let release!: (value: unknown) => void
      const blockedRanking = new Promise<unknown>((resolve) => {
        release = resolve
      })
      const calls: string[] = []

      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        calls.push(path)
        if (path === '/api/v1/flood-alerts/ranking') return (await blockedRanking) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      const p1 = loadFloodRankingOnDemand(run.run_id, query)
      const p2 = loadFloodRankingOnDemand(run.run_id, query)
      expect(_floodRankingInFlightSize()).toBe(1)
      release(success(ranking))
      const [r1, r2] = await Promise.all([p1, p2])
      expect(r1).toBe(r2)
      expect(calls.filter((p) => p === '/api/v1/flood-alerts/ranking').length).toBe(1)
      // resolve 后 in-flight 条目被清掉（cached() 持久兜底）。
      expect(_floodRankingInFlightSize()).toBe(0)
    })

    // 4.5 (c)：unmount / layer 切回 discharge → release 清掉 in-flight，下一次调用发新 fetch。
    it('clears the in-flight ranking entry on release (unmount / layer switch back to discharge)', async () => {
      // 同上：用 definite-assignment 跳开 TS narrowing 与 optional-call narrowing 问题。
      let release!: (value: unknown) => void
      const blockedRanking = new Promise<unknown>((resolve) => {
        release = resolve
      })

      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        if (path === '/api/v1/flood-alerts/ranking') return (await blockedRanking) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      const inFlight = loadFloodRankingOnDemand(run.run_id, query)
      expect(_floodRankingInFlightSize()).toBe(1)
      // 模拟面板 unmount：调用方 release。
      releaseFloodRankingOnDemand(run.run_id, query)
      expect(_floodRankingInFlightSize()).toBe(0)
      // 释放原 promise 不影响 in-flight 状态（已被清空，新调用要发新 fetch）。
      release(success(ranking))
      await inFlight
      expect(_floodRankingInFlightSize()).toBe(0)
    })

    it('releases ranking in-flight entries by query when runId is unknown (latestRun pending)', async () => {
      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        if (path === '/api/v1/flood-alerts/ranking') return new Promise(() => undefined) as never
        throw new Error(`Unexpected GET ${path}`)
      })

      loadFloodRankingOnDemand('run-a', query).catch(() => undefined)
      loadFloodRankingOnDemand('run-b', query).catch(() => undefined)
      expect(_floodRankingInFlightSize()).toBe(2)
      // 模拟 layer 切走但当时 latestRun 已变 / 未知：用 null runId 清掉同 query/basinId 全部 in-flight。
      releaseFloodRankingOnDemand(null, query)
      expect(_floodRankingInFlightSize()).toBe(0)
    })

    // 4.5 (e)：layerIdsForOverview.map(fetchLayerValidTimes) 不在默认 loadOverview 路径上的 regression
    // 断言（与 "deduplicates" / "accounts for distinct..." 测试形成多层防护）。
    it('does not call layerIdsForOverview.map(fetchLayerValidTimes) on the default overview path', async () => {
      const calls: string[] = []
      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        calls.push(path)
        if (path === '/api/v1/basins') return success([basin]) as never
        if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
        if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
        if (path === '/api/v1/layers') {
          return success([
            {
              layer_id: 'flood-return-period',
              layer_name: 'Flood return period',
              layer_type: 'hydrology',
              variables: [],
              metadata: { valid_times: ['2026-05-18T06:00:00Z'] },
            },
            // 同时验证：discharge layer 也不触发 fan-out（覆盖 layerIdsForOverview 多 layer set）。
            {
              layer_id: 'discharge',
              layer_name: 'Discharge',
              layer_type: 'hydrology',
              variables: ['q_down'],
              metadata: { valid_times: ['2026-05-18T06:00:00Z'] },
            },
          ]) as never
        }
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
        if (path === '/api/v1/flood-alerts/summary') {
          return success({ run_id: run.run_id, total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
        }
        throw new Error(`Unexpected GET ${path}`)
      })

      await useOverviewDataStore.getState().loadOverview({ ...query, layer: 'discharge' })

      // 关键 regression：默认 path 上一个 per-layer valid-times 请求都不能有。
      expect(calls.filter((p) => p === '/api/v1/layers/{layer_id}/valid-times')).toEqual([])
      // 同时确认 ranking 也未被请求（防 PR 5/7 rebase 时回归）。
      expect(calls.filter((p) => p === '/api/v1/flood-alerts/ranking')).toEqual([])
    })
  })

  // PR 5/7（spec scenario "Default discharge run selection is independent of flood readiness" /
  // "Layer toggle re-evaluates flood_product_ready filter"）：
  // discharge layer 不应强制 `flood_product_ready=true`；切到 flood-return-period 时下一次
  // `/runs` 必须带 `flood_product_ready=true` 并重选 latest run。
  describe('flood_product_ready layer-gating (PR 5/7)', () => {
    it('omits flood_product_ready on discharge, injects on flood layer, and re-resolves latestRun across the toggle', async () => {
      const dischargeQuery = { ...query, layer: 'discharge' as const }
      const floodQuery = { ...query, layer: 'flood-return-period' as const }

      // 两个候选 run：run-A 是 frequency-ready 但 flood-incomplete；run-B 是 fully flood-ready。
      // 关键：run-A 的 `updated_at` 显式比 run-B 新（00:00 cycle 平手 → updated_at tiebreaker）。
      // - discharge layer 不强制 flood_product_ready → 后端返回 [run-A, run-B]，前端 latestPublishedRun
      //   按 updated_at DESC 选到 run-A。
      // - flood-return-period layer 强制 flood_product_ready=true → 后端只返回 [run-B]（run-A 因洪频
      //   不完整被过滤），前端必须重选 run-B（而非沿用 cache 中的 run-A）。这是 spec scenario
      //   "Layer toggle re-evaluates flood_product_ready filter" 的 latest-run-re-resolution 子句。
      const runA = {
        ...run,
        run_id: 'run-A-flood-incomplete',
        cycle_time: '2026-05-19T00:00:00Z',
        updated_at: '2026-05-19T01:00:00Z',
      }
      const runB = {
        ...run,
        run_id: 'run-B-flood-ready',
        cycle_time: '2026-05-19T00:00:00Z',
        updated_at: '2026-05-19T00:30:00Z',
      }

      const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

      vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
        const path = String(args[0])
        const options = args[1] as { params?: { query?: Record<string, unknown> } }
        const requestQuery = options?.params?.query
        calls.push({ path, query: requestQuery })

        if (path === '/api/v1/basins') return success([basin]) as never
        if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
        if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
        if (path === '/api/v1/runs') {
          // 仅 frequency_done status 才返回非空，其余空 page 保持 readyStatusPages 结构。
          if (requestQuery?.status !== 'frequency_done') {
            return success({ items: [], total: 0, limit: 20, offset: 0 }) as never
          }
          // 模拟后端：flood_product_ready=true 时只返回完整 flood-ready 的 run-B；
          // 未传 flood_product_ready 时返回全部候选 [run-A, run-B]。
          if (requestQuery?.flood_product_ready === true) {
            return success({ items: [runB], total: 1, limit: 20, offset: 0 }) as never
          }
          return success({ items: [runA, runB], total: 2, limit: 20, offset: 0 }) as never
        }
        if (path === '/api/v1/layers') {
          return success([
            {
              layer_id: 'discharge',
              layer_name: 'River discharge',
              layer_type: 'hydrology',
              variables: ['q_down'],
              metadata: { valid_times: ['2026-05-19T06:00:00Z'] },
            },
            {
              layer_id: 'flood-return-period',
              layer_name: 'Flood return period',
              layer_type: 'hydrology',
              variables: ['return_period'],
              metadata: { valid_times: ['2026-05-19T06:00:00Z'] },
            },
          ]) as never
        }
        if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
        if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
        if (path === '/api/v1/flood-alerts/summary') {
          // 关键：echo 请求的 run_id 让 freshness.runId 真实反映上游 latestRun 选择。
          const echoed = String(requestQuery?.run_id ?? '')
          return success({ run_id: echoed, total_segments: 0, usable_curves: 0, unavailable_count: 0, levels: [] }) as never
        }
        throw new Error(`Unexpected GET ${path}`)
      })

      // (1) discharge：请求不应携带 flood_product_ready；latestRun 来自 [run-A, run-B]
      // （cycle_time 平手 → updated_at 较新者胜 = run-A）。
      const dischargeSnapshot = await useOverviewDataStore.getState().loadOverview(dischargeQuery)
      const dischargeRunCalls = calls.filter((call) => call.path === '/api/v1/runs')
      expect(dischargeRunCalls.length).toBeGreaterThan(0)
      // 关键 assertion 1：discharge 路径下任意 /runs 调用都不应包含 flood_product_ready。
      expect(dischargeRunCalls.every((call) => call.query?.flood_product_ready === undefined)).toBe(true)
      // discharge layer 选到的 latestRun = run-A（cycle_time 平手 → updated_at 较新者胜）。
      expect(dischargeSnapshot.summary.freshness.runId).toBe(runA.run_id)

      // (2) 切到 flood-return-period：下一次 /runs 必须带 flood_product_ready=true；
      // latestRun 必须从新 run set [run-B] 重新选，不能复用上一轮 cached 的 run-A。
      const callsBeforeToggle = calls.length
      const floodSnapshot = await useOverviewDataStore.getState().loadOverview(floodQuery)
      const floodRunCallsAfterToggle = calls
        .slice(callsBeforeToggle)
        .filter((call) => call.path === '/api/v1/runs')
      // 关键 assertion 2：layer toggle 后下一轮 /runs 必须发，且每条都携带 flood_product_ready=true。
      expect(floodRunCallsAfterToggle.length).toBeGreaterThan(0)
      expect(floodRunCallsAfterToggle.every((call) => call.query?.flood_product_ready === true)).toBe(true)
      // 关键 assertion 3：latestRun 重选 → flood-ready 路径下选到 run-B（discharge 阶段的 run-A 不能串味）。
      expect(floodSnapshot.summary.freshness.runId).toBe(runB.run_id)
      expect(floodSnapshot.summary.freshness.runId).not.toBe(runA.run_id)
    })
  })
})
