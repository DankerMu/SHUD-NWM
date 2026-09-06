import { act, render, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { useBasinDetailMode } from '@/components/m11/BasinDetailPanels'
import { activeCycleValidTimesErrorDisabledReason } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryPatch, type M11QueryState } from '@/lib/m11/queryState'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

const VALID_TIMES_PATH = '/api/v1/layers/{layer_id}/valid-times'
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
const BASIN_ID = 'basin-demo'
// 目录默认对 (gfs, DEFAULT_CYCLE) 的时次列表；`?source=ifs` 的活动对拿不到自己的列表。
const DEFAULT_PAIR_VALID_TIMES = ['2026-05-18T00:00:00Z', '2026-05-18T03:00:00Z', '2026-05-18T06:00:00Z']

const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

// 与 stores/__tests__/overviewData.test.ts 里被证明会产出「未定终态」的输入同形：
// 默认源 gfs、默认周期，URL 的 validTime 属于默认对的列表。
const defaultPairQuery: M11QueryState = {
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
  basin_id: BASIN_ID,
  basin_name: 'Demo Basin',
  basin_group: 'demo',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
}

const basinVersion = {
  basin_version_id: 'bv-001',
  basin_id: BASIN_ID,
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
  basin_id: BASIN_ID,
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
  basin_slug: BASIN_ID,
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
  cycle_time: DEFAULT_CYCLE,
  status: 'published',
  slurm_job_id: null,
  start_time: DEFAULT_CYCLE,
  end_time: '2026-05-25T00:00:00Z',
  run_manifest_uri: null,
  output_uri: null,
  log_uri: null,
  error_code: null,
  error_message: null,
  created_at: '2026-05-18T00:05:00Z',
  updated_at: '2026-05-18T00:10:00Z',
}

// 后端对 run-scoped `/api/v1/layers?run_id=` 同样合并全国 discharge 元数据：流域详情的
// discharge 也是 `{source}/{cycle}` 模板，`valid_times` 只是**目录默认对**那一份。
const layer = {
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
    valid_times: DEFAULT_PAIR_VALID_TIMES,
    fallback_available: false,
    release_blocking: false,
  },
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

function mockApi() {
  vi.mocked(client.GET).mockImplementation((async (path: string, options?: MockOptions) => {
    if (path === '/api/v1/basins') return success([basin])
    if (path === '/api/v1/basins/{basin_id}/versions') return success([basinVersion])
    if (path === '/api/v1/models') return success({ items: [model], total: 1, limit: 200, offset: 0 })
    if (path === '/api/v1/models/{model_id}') return success(model)
    if (path === '/api/v1/runs') return success({ items: [run], total: 1, limit: 20, offset: options?.params?.query?.offset ?? 0 })
    if (path === '/api/v1/layers') return success([layer])
    if (path === VALID_TIMES_PATH) return success({ layer_id: 'discharge', valid_times: ['2026-05-18T06:00:00Z'] })
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
        issue_time: DEFAULT_CYCLE,
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
}

/** 只跑 hook：详情页的地图/弹窗与本用例无关，被测的是校正 effect 这一个消费点。 */
function BasinDetailProbe({ state, onQueryChange }: { state: M11QueryState; onQueryChange: (patch: M11QueryPatch) => void }) {
  useBasinDetailMode({ basinId: BASIN_ID, state, onQueryChange })
  return null
}

function dischargeLayerState() {
  return useOverviewDataStore.getState().basinDetail?.layers.find((item) => item.layerId === 'discharge')
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
})

describe('basin detail validTime auto-correction gate', () => {
  it('keeps a deep-linked validTime when the basin-detail active pair is unresolved', async () => {
    // spec frontend-mvt-layer-consumption「Basin detail renders the national discharge overlay only
    // for the catalog default pair」末条：未定态（这里是终态 error）期间不得改写 URL 的 validTime。
    // 消费点必须用**闸门版** `resolveM11NationalValidTimeCorrection`；换成裸的
    // `resolveM11ValidTimeCorrection`，未定态的空列表会被判成「该图层没有时次」→ 立刻
    // `onQueryChange({ validTime: null })`。而流域详情永不取 per-cycle 列表，未定即终态，
    // 丢掉的 validTime 不会再被任何后续落地补回来。
    mockApi()
    const onQueryChange = vi.fn()
    const unresolvedState: M11QueryState = { ...defaultPairQuery, source: 'ifs' }

    const { rerender } = render(<BasinDetailProbe state={unresolvedState} onQueryChange={onQueryChange} />)

    // 前提断言：快照已落定、校正 effect 的两道早退闸门（loading / metadata 匹配）都已放开，
    // 且活动对 (ifs, 默认周期) 确实是未定终态 —— 下面的绿不是「effect 从未跑」冒充的。
    await waitFor(() => expect(useOverviewDataStore.getState().basinDetail).not.toBeNull())
    await waitFor(() => expect(useOverviewDataStore.getState().basinLoading).toBe(false))
    expect(dischargeLayerState()?.validTimes).toEqual([])
    expect(dischargeLayerState()?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    await flushPendingEffects()

    // 校正 effect 也会推 riverNetworkVersionId 补丁，故只钉「没有任何 validTime 补丁」。
    expect(onQueryChange.mock.calls.map(([patch]) => patch).filter((patch) => 'validTime' in patch)).toEqual([])

    // 正对照：切回目录默认对且 URL 的 validTime 越界时，同一个消费点必须照常校正 ——
    // 证明闸门只挡未定态，没有把整个 effect 一并废掉。
    onQueryChange.mockClear()
    const outOfRangeState: M11QueryState = { ...defaultPairQuery, validTime: '2026-05-18T09:00:00.000Z' }
    rerender(<BasinDetailProbe state={outOfRangeState} onQueryChange={onQueryChange} />)

    await waitFor(() => expect(dischargeLayerState()?.disabledReason).toBeNull())
    expect(dischargeLayerState()?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    // 越界 validTime → `pickCurrentValidTime` 回落活动列表首项（lead 0）。
    await waitFor(() => expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T00:00:00.000Z' }))
  })
})
