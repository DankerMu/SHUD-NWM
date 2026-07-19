import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'

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

const layer = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: { layer_id: 'discharge', valid_times: ['2026-05-18T06:00:00Z'] },
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

function mockApi() {
  const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
    calls.push({ path, query: options?.params?.query })
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion])
    if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 })
    if (path === '/api/v1/models/{model_id}') return success(model)
    if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: options?.params?.query?.offset ?? 0 })
    if (path === '/api/v1/layers') return success([layer])
    if (path === '/api/v1/layers/{layer_id}/valid-times') return success({ layer_id: 'discharge', valid_times: ['2026-05-18T06:00:00Z'] })
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
  })
  useMonitoringStore.setState({ runtimeConfig: displayRuntimeConfig, runtimeConfigError: null })
})

describe('overview data store discharge loading', () => {
  it('loads overview runs without product-specific readiness filters', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.summary.freshness.runId).toBe('run-001')
    expect(calls.filter((call) => call.path === '/api/v1/basins')).toEqual([
      {
        path: '/api/v1/basins',
        query: { limit: 200, offset: 0, has_display_product: true },
      },
    ])
    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    const allowedRunQueryKeys = new Set(['basin_id', 'source', 'cycle_time', 'status', 'limit', 'offset'])
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.status === 'published')).toBe(true)
    expect(runCalls.every((call) => Object.keys(call.query ?? {}).every((key) => allowedRunQueryKeys.has(key)))).toBe(true)
  })

  it('loads basin detail with river geometry and q_down forecast only', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', query)

    expect(snapshot.segments[0]).toMatchObject({ currentQ: 12, qUnit: 'm3/s' })
    expect(snapshot.selectedSegment?.currentQ).toBe(12)
    expect(calls.filter((call) => call.path === '/api/v1/runs').every((call) => call.query?.status === 'published')).toBe(true)
  })
})
