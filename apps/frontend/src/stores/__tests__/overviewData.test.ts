import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import type { M11QueryState } from '@/lib/m11/queryState'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { normalizeLayerStates } from '@/lib/m11/overviewDataContracts'

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

  it('does not fetch basin-version fields when the measured overview plan exceeds the request threshold', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown>; pathParams?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } }
      calls.push({ path, query: options?.params?.query, pathParams: options?.params?.path })

      if (path === '/api/v1/basins') return success([basin]) as never
      if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion]) as never
      if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: 0 }) as never
      if (path === '/api/v1/layers') {
        return success([{ layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: [] }]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 2, pending: 1, idle: 3 }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({
          run_id: 'run-gfs-1',
          total_segments: 1,
          usable_curves: 1,
          unavailable_count: 0,
          quality_note: null,
          levels: [{ level: 'warning', count: 1, color: '#FF8C00' }],
        }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success(ranking) as never
      if (path === '/api/v1/pipeline/status') return success(pipelineStatus) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') {
        return success(['2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z']) as never
      }
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
    expect(snapshot.aggregationDecision).toMatchObject({ needsAggregationEndpoint: true, reason: 'too-many-initial-requests' })
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/overview/summary')
    expect(calls.map((call) => call.path)).not.toContain('/api/v1/basins/{basin_id}/versions')
    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: 'GFS',
      cycle_time: '2026-05-18T00:00:00Z',
      status: 'frequency_done',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/summary')?.query).toMatchObject({
      run_id: 'run-gfs-1',
      valid_time: '2026-05-18T06:00:00Z',
    })
    expect(calls.find((call) => call.path === '/api/v1/layers/{layer_id}/valid-times')?.pathParams).toMatchObject({
      layer_id: 'flood-return-period',
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
        return success([{ layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: [] }]) as never
      }
      if (path === '/api/v1/queue/depth') return success({ running: 0, pending: 0, idle: 0 }) as never
      if (path === '/api/v1/pipeline/status') return success({ ...pipelineStatus, source: 'IFS' }) as never
      if (path === '/api/v1/flood-alerts/summary') {
        return success({ run_id: 'run-ifs-1', total_segments: 1, usable_curves: 1, unavailable_count: 0, levels: [] }) as never
      }
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') {
        return success(['2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z']) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview(bestQuery)

    expect(calls.find((call) => call.path === '/api/v1/runs')?.query).toMatchObject({
      source: undefined,
      cycle_time: undefined,
      status: 'frequency_done',
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

  it('marks registered but unrenderable overview layers unavailable before store hydration retains them', () => {
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
    for (const layerId of ['discharge', 'warning-level', 'river-network']) {
      expect(layers.find((layer) => layer.layerId === layerId)).toMatchObject({
        available: false,
        disabledReason: 'Layer is registered but no renderable map source is implemented in this repository.',
      })
    }
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
            { level: 'normal', count: 2, color: '#808080' },
            { level: 'watch', count: 1, color: '#FFD700' },
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

  it('accounts for distinct non-flood layer valid-time requests in the aggregation decision', async () => {
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
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadOverview({ ...query, layer: 'discharge' })

    expect(
      calls
        .filter((call) => call.path === '/api/v1/layers/{layer_id}/valid-times')
        .map((call) => call.pathParams?.layer_id),
    ).toEqual(['discharge', 'flood-return-period'])
    expect(snapshot.aggregationDecision.evidence).toContain('9 initial requests')
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
      if (path === '/api/v1/flood-alerts/ranking') return success({ items: [], total: 0, limit: 200, offset: 0 }) as never
      if (path === '/api/v1/layers/{layer_id}/valid-times') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await Promise.all([
      useOverviewDataStore.getState().loadOverview(query),
      useOverviewDataStore.getState().loadOverview(query),
    ])

    expect(counts.get('/api/v1/basins')).toBe(1)
    expect(counts.get('/api/v1/models')).toBe(1)
    expect(counts.get('/api/v1/layers/{layer_id}/valid-times')).toBe(1)
  })

  it('keeps stale overview responses from overwriting the latest request state', async () => {
    const delayedQuery = { ...query, cycle: '2026-05-18T03:00:00Z', validTime: '2026-05-18T03:00:00Z' }
    let releaseDelayedRuns: ((value: ReturnType<typeof success<{ items: typeof run[]; total: number; limit: number; offset: number }>>) => void) | null = null

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }

      if (path === '/api/v1/runs' && options?.params?.query?.cycle_time === delayedQuery.cycle) {
        await new Promise((resolve) => {
          releaseDelayedRuns = resolve
        })
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
    await vi.waitFor(() => expect(releaseDelayedRuns).toBeTypeOf('function'))

    const latestSnapshot = await useOverviewDataStore.getState().loadOverview(query)
    expect(useOverviewDataStore.getState().overview).toBe(latestSnapshot)

    releaseDelayedRuns?.(success({ items: [run], total: 1, limit: 20, offset: 0 }))
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
    })
    expect(calls.find((call) => call.path === '/api/v1/models')?.query).toMatchObject({
      basin_version_id: 'yangtze_v2026_01',
      active: 'true',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.query).toMatchObject({
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
    expect(snapshot.detail.warningDistribution.severe).toBe(0)
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

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.offset)).toEqual([0, 20])
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
      20,
      220,
      420,
      620,
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

    expect(calls.filter((call) => call.path === '/api/v1/runs').map((call) => call.query?.offset)).toEqual([0, 20])
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
      handoffUrl: '/forecast?segmentId=river-seg-123&basinVersionId=yangtze_v2026_01',
    })
    expect(
      calls.find((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}')?.pathParams,
    ).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(calls.find((call) => call.path.endsWith('/forecast-series'))?.pathParams).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(calls.find((call) => call.path === '/api/v1/flood-alerts/timeline')?.query).toMatchObject({
      segment_id: 'river-seg-123',
    })
    expect(calls.find((call) => call.path === '/api/v1/lineage/river-point')?.query).toMatchObject({
      segment_id: 'river-seg-123',
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

    expect(snapshot.segments).toHaveLength(1)
    expect(snapshot.segments[0].riverSegmentId).toBe('seg-123')
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
      throw new Error(`Unexpected GET ${path}`)
    })

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('yangtze', missingLateQuery)

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000])
    expect(snapshot.selectedSegment).toBeNull()
    expect(snapshot.detail.segmentCount).toBe(20_000)
    expect(snapshot.detail.partialErrors).toContain(
      'river segments: Stopped segment lookup after 10 pages or 10000 features before the requested segment was found.',
    )
    expect(calls).not.toEqual(expect.arrayContaining([expect.objectContaining({ path: expect.stringContaining('/{segment_id}') })]))
  })

  it('loads only the first river-segment page for default basin detail without a requested segment', async () => {
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

    expect(
      calls
        .filter((call) => call.path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
        .map((call) => call.query?.offset),
    ).toEqual([0])
    expect(snapshot.detail.segmentCount).toBe(20_000)
    expect(snapshot.segments[0].riverSegmentId).toBe('seg-123')
    expect(snapshot.selectedSegment).toMatchObject({
      riverSegmentId: 'seg-123',
      segmentId: 'seg-123',
      currentQ: 123,
      lineageStatus: 'available',
    })
    expect(snapshot.detail.partialErrors).not.toEqual(expect.arrayContaining([expect.stringContaining('river segments: Stopped')]))
    expect(useOverviewDataStore.getState().basinError).toBeNull()
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
    expect(snapshot.detail.warningDistribution.warning).toBe(0)
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
})
