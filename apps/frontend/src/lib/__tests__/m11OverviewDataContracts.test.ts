import { describe, expect, it, vi } from 'vitest'

import type { components } from '@/api/types'
import {
  createSourceScenarioSelection,
  decideAggregationEndpoint,
  filterBasinSegmentRows,
  getM11LayerLegend,
  getM11BasinGeometryBudgetStatus,
  getM11SelectedSegmentGeometryBudgetStatus,
  m11BasinGeometryBudget,
  m11BasinRiverCollectionBudget,
  m11SelectedSegmentGeometryBudget,
  normalizeBasinDetail,
  normalizeBasinSegmentRows,
  normalizeLayerStates,
  normalizeOverviewBasins,
  normalizeOverviewSummary,
  normalizeSelectedSegmentDetail,
} from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'
import { defaultM11QueryState } from '@/lib/m11/queryState'

type ApiBasin = components['schemas']['Basin']
type ApiBasinVersion = components['schemas']['BasinVersion']
type ApiModelInstance = components['schemas']['ModelInstance']
type ApiHydroRun = components['schemas']['HydroRun']
type ApiFloodAlertRankingItem = components['schemas']['FloodAlertRankingItem']
type ApiRiverFeatureCollection = components['schemas']['RiverSegmentFeatureCollection']
type ApiRiverSegment = components['schemas']['RiverSegment']
type ApiForecastPayload = components['schemas']['SplicedForecastResponse']
type ApiFloodAlertTimeline = components['schemas']['FloodAlertTimeline']
type ApiLineageResponse = components['schemas']['LineageResponse']

const query: M11QueryState = {
  ...defaultM11QueryState,
  source: 'best',
  cycle: '2026-05-18T00:00:00Z',
  validTime: '2026-05-18T06:00:00Z',
  layer: 'flood-return-period',
  basinVersionId: 'yangtze_v2026_01',
  riverNetworkVersionId: 'yangtze_rivnet_v12',
}

const basin: ApiBasin = {
  basin_id: 'yangtze',
  basin_name: 'Yangtze Basin',
  basin_group: 'major',
  description: null,
  created_at: '2026-05-01T00:00:00Z',
}

const basinVersion: ApiBasinVersion = {
  basin_version_id: 'yangtze_v2026_01',
  basin_id: 'yangtze',
  version_label: 'v2026_01',
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: 's3://basins/yangtze.geojson',
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
  geom: {
    type: 'MultiPolygon',
    coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31], [100, 30]]]],
  },
}

const model: ApiModelInstance = {
  model_id: 'yangtze_shud_v12',
  model_name: 'Yangtze SHUD',
  basin_id: 'yangtze',
  basin_name: 'Yangtze Basin',
  basin_version_id: 'yangtze_v2026_01',
  river_network_version_id: 'yangtze_rivnet_v12',
  mesh_version_id: 'mesh-1',
  calibration_version_id: 'cal-1',
  segment_count: 2,
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

const run: ApiHydroRun = {
  run_id: 'fcst_gfs_2026051800_yangtze_shud_v12',
  run_type: 'forecast',
  scenario_id: 'forecast_gfs_deterministic',
  model_id: 'yangtze_shud_v12',
  basin_version_id: 'yangtze_v2026_01',
  river_network_version_id: 'yangtze_rivnet_v12',
  forcing_version_id: 'forc_gfs_2026051800_yangtze_shud_v12',
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
  created_at: '2026-05-18T00:05:00Z',
  updated_at: '2026-05-18T01:00:00Z',
}

const rankingItem: ApiFloodAlertRankingItem = {
  rank: 1,
  river_segment_id: 'yangtze_rivnet_v12_riv_000123',
  segment_id: 'seg-123',
  segment_name: 'Yichang mainstem',
  basin_version_id: 'yangtze_v2026_01',
  q_value: 5242,
  q_unit: '',
  return_period: 20,
  warning_level: 'orange',
  duration: '1h',
  valid_time: '2026-05-18T06:00:00Z',
}

const featureCollection: ApiRiverFeatureCollection = {
  type: 'FeatureCollection',
  total: 2,
  feature_total: 2,
  limit: 1000,
  offset: 0,
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-123',
        river_segment_id: 'yangtze_rivnet_v12_riv_000123',
        basin_version_id: 'yangtze_v2026_01',
        river_network_version_id: 'yangtze_rivnet_v12',
        name: 'Yichang mainstem',
        stream_order: 7,
        segment_order: 12,
        downstream_segment_id: null,
        length_m: 452700,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-404',
        river_segment_id: 'yangtze_rivnet_v12_riv_000404',
        basin_version_id: 'yangtze_v2026_01',
        river_network_version_id: 'yangtze_rivnet_v12',
        name: 'No forecast segment',
        stream_order: 3,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [100.5, 30.5]] },
    },
  ],
}

const segment: ApiRiverSegment = {
  river_segment_id: 'yangtze_rivnet_v12_riv_000123',
  river_network_version_id: 'yangtze_rivnet_v12',
  segment_order: 12,
  downstream_segment_id: null,
  length_m: 452700,
  geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
  properties_json: {},
  created_at: '2026-05-01T00:00:00Z',
}

describe('M11 overview data contracts', () => {
  it('normalizes overview basins with explicit IDs, bbox, versions, warnings, and unavailable fields', () => {
    const basins = normalizeOverviewBasins({
      basins: [basin, { ...basin, basin_id: 'empty', basin_name: 'Empty Basin', basin_group: null }],
      versionsByBasinId: { yangtze: [basinVersion], empty: [] },
      models: [model],
      runs: [run],
      rankingItems: [rankingItem],
    })

    expect(basins[0]).toMatchObject({
      basinId: 'yangtze',
      selectedBasinVersionId: 'yangtze_v2026_01',
      activeModelCount: 1,
      riverCount: 2,
      latestForecastTime: '2026-05-18T00:00:00.000Z',
      warningCounts: { warning: 1 },
      boundary: basinVersion.geom,
      bbox: { minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 },
    })
    expect(basins[1]).toMatchObject({
      basinId: 'empty',
      selectedBasinVersionId: null,
      unavailableReason: 'No published basin version is available.',
    })
  })

  it('normalizes overview summary freshness, partial failures, warning counts, and source provenance', () => {
    vi.setSystemTime(new Date('2026-05-18T07:00:00Z'))

    const summary = normalizeOverviewSummary({
      query,
      basins: [],
      floodSummary: {
        run_id: run.run_id,
        total_segments: 10,
        usable_curves: 8,
        unavailable_count: 2,
        quality_note: '2 curves unavailable',
        levels: [
          { level: 'normal', count: 5, color: '#4FC3F7' },
          { level: 'severe', count: 3, color: '#E57373' },
        ],
      },
      pipeline: {
        cycle_id: 'cycle-1',
        source: 'GFS',
        cycle_time: '2026-05-18T00:00:00Z',
        current_state: 'running',
        started_at: '2026-05-18T00:01:00Z',
        updated_at: '2026-05-18T06:30:00Z',
        job_counts: { succeeded: 4, failed: 1, running: 2, pending: 0 },
      },
      queue: { running: 7, pending: 1, idle: 3 },
      latestRun: run,
      partialErrors: ['layers: unavailable'],
    })

    expect(summary).toMatchObject({
      completedCyclesToday: 4,
      runningJobs: 7,
      warningSegmentCount: 3,
      latestUpdate: '2026-05-18T06:30:00.000Z',
      totalSegments: 10,
      partialErrors: ['layers: unavailable'],
    })
    expect(summary.freshness).toMatchObject({
      cycleTime: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
      isStale: false,
      runId: run.run_id,
      source: 'GFS',
    })
    expect(summary.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'GFS',
      cycleTime: '2026-05-18T00:00:00Z',
      validTime: '2026-05-18T06:00:00Z',
    })
    expect(summary.qualityNotes).toContain('2 curves unavailable')

    vi.useRealTimers()
  })

  it('preserves zero warning count for a successful flood summary with no super-warning segments', () => {
    const summary = normalizeOverviewSummary({
      query,
      floodSummary: {
        run_id: run.run_id,
        total_segments: 7,
        usable_curves: 7,
        unavailable_count: 0,
        quality_note: null,
        levels: [
          { level: 'normal', count: 4, color: '#4FC3F7' },
          { level: 'watch', count: 3, color: '#FFD54F' },
        ],
      },
      latestRun: run,
    })

    expect(summary.warningSegmentCount).toBe(0)
    expect(summary.totalSegments).toBe(7)
  })

  it('normalizes layer valid-times from the API and marks unavailable layers without synthetic times', () => {
    const layers = normalizeLayerStates({
      query: { ...query, validTime: '2026-05-18T03:00:00Z' },
      layers: [
        {
          layer_id: 'flood-return-period',
          layer_name: 'Flood return period',
          layer_type: 'hydrology',
          variables: ['return_period'],
          metadata: null,
        },
      ],
      validTimesByLayerId: {
        'flood-return-period': ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z'],
        discharge: [],
      },
    })

    const floodLayer = layers.find((layer) => layer.layerId === 'flood-return-period')
    const discharge = layers.find((layer) => layer.layerId === 'discharge')

    expect(floodLayer).toMatchObject({
      available: true,
      currentValidTime: '2026-05-18T06:00:00.000Z',
      validTimeSource: 'api',
    })
    expect(discharge).toMatchObject({
      available: false,
      validTimes: [],
      validTimeSource: 'none',
      disabledReason: 'Layer is not registered by the API.',
    })
  })

  it('marks hydrology data layers renderable when basin segment rows can provide geometry', () => {
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

    expect(layers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({ available: true, disabledReason: null })
    expect(layers.find((layer) => layer.layerId === 'discharge')).toMatchObject({ available: true, disabledReason: null })
    expect(layers.find((layer) => layer.layerId === 'warning-level')).toMatchObject({ available: true, disabledReason: null })
    expect(layers.find((layer) => layer.layerId === 'river-network')).toMatchObject({
      available: false,
      disabledReason: 'Layer is registered but no renderable map source is implemented in this repository.',
    })
  })

  it('derives best layer freshness from a resolved concrete run', () => {
    const layers = normalizeLayerStates({
      query: { ...query, source: 'ifs', cycle: null },
      layers: [
        {
          layer_id: 'flood-return-period',
          layer_name: 'Flood return period',
          layer_type: 'hydrology',
          variables: ['return_period'],
          metadata: null,
        },
      ],
      validTimesByLayerId: {
        'flood-return-period': ['2026-05-18T06:00:00Z'],
      },
      resolvedRun: {
        ...run,
        run_id: 'fcst_ifs_2026051800_yangtze_shud_v12',
        scenario_id: 'forecast_ifs_deterministic',
        source_id: 'IFS',
      },
    })

    expect(layers.find((layer) => layer.layerId === 'flood-return-period')?.freshness).toMatchObject({
      runId: 'fcst_ifs_2026051800_yangtze_shud_v12',
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
    })
  })

  // PR 4/7 task 4.5 (d)：normalizeLayerStates 三态 metadata-first 消费（spec capability
  // "frontend-mvt-layer-consumption" Requirement "Layer valid_times are consumed from
  // metadata.valid_times first" 的 3 scenarios）。
  describe('metadata-first valid_times consumption (PR 4/7)', () => {
    // 第一态：metadata.valid_times 非空数组 → 直接消费，调用方不应发 fallback；fallback override
    // 即使被传入也应该被 normalizeLayerStates 忽略，避免反向重写真实 metadata 语义。
    it('consumes non-empty metadata.valid_times directly and ignores fallback override', () => {
      const layers = normalizeLayerStates({
        query: { ...query, validTime: null },
        layers: [
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: ['return_period'],
            metadata: { valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z'] },
          },
        ],
        // fallback 入参出现也应被忽略（metadata 已是数组 → metadata 优先；spec MUST NOT 子句）。
        validTimesByLayerId: { 'flood-return-period': ['2026-05-19T00:00:00Z'] },
      })

      const target = layers.find((l) => l.layerId === 'flood-return-period')!
      expect(target.validTimeSource).toBe('api')
      expect(target.validTimes).toEqual(['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z'])
      expect(target.currentValidTime).toBe('2026-05-18T06:00:00.000Z')
      // fallback 数组完全被忽略 —— 没有 '2026-05-19T00:00:00.000Z'。
      expect(target.validTimes).not.toContain('2026-05-19T00:00:00.000Z')
    })

    // 第二态：metadata.valid_times === [] → time-less layer（如 river-network）→ validTimes
    // 输出空，调用方不发 fallback；fallback override 同样被忽略。
    it('treats metadata.valid_times === [] as a time-less layer and does not honor fallback override', () => {
      const layers = normalizeLayerStates({
        query: { ...query, validTime: null, layer: 'discharge' },
        layers: [
          {
            layer_id: 'discharge',
            layer_name: 'Discharge',
            layer_type: 'hydrology',
            variables: ['q_down'],
            // 显式空数组 = time-less（不是 schema gap）。
            metadata: { valid_times: [] },
          },
        ],
        validTimesByLayerId: { discharge: ['2026-06-01T00:00:00Z'] },
      })

      const target = layers.find((l) => l.layerId === 'discharge')!
      // metadata 显式空 → 不从 fallback override 取（time-less 优先于 fallback）。
      expect(target.validTimes).toEqual([])
      expect(target.currentValidTime).toBeNull()
      expect(target.validTimeSource).toBe('none')
    })

    // 第三态：metadata.valid_times === undefined / null → schema gap → 调用方 MAY fallback，
    // normalizeLayerStates 接受 fallback override 并消费。
    it('falls back to validTimesByLayerId only when metadata.valid_times is undefined or null', () => {
      const layers = normalizeLayerStates({
        query: { ...query, validTime: null },
        layers: [
          // case A: metadata 整体缺失 → 需要 fallback。
          {
            layer_id: 'flood-return-period',
            layer_name: 'Flood return period',
            layer_type: 'hydrology',
            variables: ['return_period'],
            metadata: null,
          },
          // case B: metadata 存在但 valid_times 字段缺失 → 也需要 fallback。
          {
            layer_id: 'warning-level',
            layer_name: 'Warning level',
            layer_type: 'hydrology',
            variables: ['warning_level'],
            metadata: { layer_id: 'warning-level', tile_format: 'mvt' } as never,
          },
        ],
        validTimesByLayerId: {
          'flood-return-period': ['2026-05-18T06:00:00Z'],
          'warning-level': ['2026-05-18T07:00:00Z'],
        },
      })

      const floodReturn = layers.find((l) => l.layerId === 'flood-return-period')!
      const warning = layers.find((l) => l.layerId === 'warning-level')!
      expect(floodReturn.validTimes).toEqual(['2026-05-18T06:00:00.000Z'])
      expect(floodReturn.currentValidTime).toBe('2026-05-18T06:00:00.000Z')
      expect(floodReturn.validTimeSource).toBe('api')
      expect(warning.validTimes).toEqual(['2026-05-18T07:00:00.000Z'])
      expect(warning.validTimeSource).toBe('api')
    })
  })

  it('normalizes basin detail and segment rows with local filters and unavailable rows', () => {
    const detail = normalizeBasinDetail({
      query,
      basin,
      versions: [basinVersion],
      models: [model],
      segments: featureCollection,
      rankingItems: [rankingItem],
      latestRun: run,
    })
    const rows = filterBasinSegmentRows(
      normalizeBasinSegmentRows({
        query: { ...query, warningLevel: 'orange', q: 'Yichang' },
        featureCollection,
        rankingItems: [rankingItem],
      }),
      { warningLevel: 'orange', q: 'Yichang' },
    )
    const allRows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: null, q: null },
      featureCollection,
      rankingItems: [rankingItem],
    })

    expect(detail).toMatchObject({
      basinId: 'yangtze',
      selectedBasinVersionId: 'yangtze_v2026_01',
      boundary: basinVersion.geom,
      segmentCount: 2,
      activeModelCount: 1,
      warningDistribution: { warning: 1 },
      unavailableReason: null,
    })
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      riverSegmentId: 'yangtze_rivnet_v12_riv_000123',
      basinVersionId: 'yangtze_v2026_01',
      currentQ: 5242,
      qUnit: 'm3/s',
      warningLevel: 'warning',
      validTime: '2026-05-18T06:00:00.000Z',
    })
    expect(allRows[1]).toMatchObject({
      qualityFlag: 'unavailable',
      unavailableReason: 'No flood-alert value is available for this segment/time.',
    })
  })

  it.each([
    ['orange', 'warning'],
    ['warning', 'warning'],
    ['major', 'high_risk'],
    ['red', 'severe'],
    ['severe', 'severe'],
    ['extreme', 'extreme'],
  ] as const)('filters %s warning query values with normalized row semantics', (warningLevel, expectedLevel) => {
    const rows = filterBasinSegmentRows(
      normalizeBasinSegmentRows({
        query: { ...query, warningLevel, q: null },
        featureCollection: {
          ...featureCollection,
          features: [
            featureCollection.features[0],
            {
              ...featureCollection.features[1],
              properties: {
                ...featureCollection.features[1].properties,
                segment_id: `seg-${expectedLevel}`,
                river_segment_id: `river-${expectedLevel}`,
              },
            },
          ],
        },
        rankingItems: [{ ...rankingItem, warning_level: expectedLevel === 'high_risk' ? 'major' : expectedLevel }],
      }),
      { warningLevel, q: null },
    )

    expect(rows.map((row) => row.warningLevel)).toEqual([expectedLevel])
  })

  it('sanitizes accepted basin geometry before retention', () => {
    const status = getM11BasinGeometryBudgetStatus({
      type: 'MultiPolygon',
      coordinates: [[[[100, 30, 8], [101, 30, 9], [101, 31, 10], [100, 31, 11], [100, 30, 8]]]],
    })

    expect(status.ok).toBe(true)
    expect(status.sanitizedGeometry?.coordinates[0][0][0]).toEqual([100, 30, 8])
    expect(status.serializedBytes).toBeGreaterThan(0)
  })

  it('rejects under-vertex basin geometry with oversized coordinate dimensions', () => {
    const tail = Array.from({ length: 8 }, (_, index) => index)
    const status = getM11BasinGeometryBudgetStatus({
      type: 'MultiPolygon',
      coordinates: [[[[100, 30, ...tail], [101, 30, ...tail], [101, 31, ...tail], [100, 31, ...tail], [100, 30, ...tail]]]],
    })

    expect(status.ok).toBe(false)
    expect(status.reason).toContain('coordinate dimensions')
    expect(status.sanitizedGeometry).toBeNull()
  })

  it('rejects under-vertex basin geometry over the serialized byte budget', () => {
    const coordinates = Array.from({ length: m11BasinGeometryBudget.maxVertices }, (_, index) => [
      100.1234567890123 + index / 100_000,
      30.1234567890123 + index / 100_000,
    ])
    coordinates[coordinates.length - 1] = [...coordinates[0]]
    const status = getM11BasinGeometryBudgetStatus({
      type: 'MultiPolygon',
      coordinates: [[[...coordinates]]],
    })

    expect(status.ok).toBe(false)
    expect(status.vertexCount).toBe(m11BasinGeometryBudget.maxVertices)
    expect(status.reason).toContain('serialized-size budget')
    expect(status.sanitizedGeometry).toBeNull()
  })

  it('rejects basin MultiPolygon rings that are too short or unclosed', () => {
    const shortRingStatus = getM11BasinGeometryBudgetStatus({
      type: 'MultiPolygon',
      coordinates: [[[[100, 30], [101, 30], [100, 30]]]],
    })
    const unclosedRingStatus = getM11BasinGeometryBudgetStatus({
      type: 'MultiPolygon',
      coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31]]]],
    })

    expect(shortRingStatus).toMatchObject({ ok: false, reason: 'Basin geometry is malformed.', sanitizedGeometry: null })
    expect(unclosedRingStatus).toMatchObject({ ok: false, reason: 'Basin geometry is malformed.', sanitizedGeometry: null })
  })

  it('matches flood-alert rows by basin version as well as duplicated segment IDs', () => {
    const oldVersionFeatures: ApiRiverFeatureCollection = {
      ...featureCollection,
      features: [
        {
          ...featureCollection.features[0],
          properties: {
            ...featureCollection.features[0].properties,
            basin_version_id: 'yangtze_v2025_12',
          },
        },
      ],
    }
    const newerVersionAlert: ApiFloodAlertRankingItem = {
      ...rankingItem,
      basin_version_id: 'yangtze_v2026_01',
      q_value: 9999,
      warning_level: 'severe',
    }

    const rows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: null, q: null },
      featureCollection: oldVersionFeatures,
      rankingItems: [newerVersionAlert],
    })

    expect(rows[0]).toMatchObject({
      basinVersionId: 'yangtze_v2025_12',
      currentQ: null,
      warningLevel: 'unavailable',
      qualityFlag: 'unavailable',
    })
  })

  it('normalizes selected segment detail with forecast provenance, trend points, and lineage status', () => {
    const forecast: ApiForecastPayload = {
      river_segment_id: 'yangtze_rivnet_v12_riv_000123',
      issue_time: '2026-05-18T00:00:00Z',
      variable: 'q_down',
      unit: 'm³/s',
      frequency_thresholds: null,
      segments: [
        {
          scenario: 'forecast_gfs_deterministic',
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          source_id: 'GFS',
          cycle_time: '2026-05-18T00:00:00Z',
          available_lead_hours: 168,
          segment_role: 'future_7_days',
          data: [
            { valid_time: '2026-05-18T06:00:00Z', value: 5242 },
            { valid_time: '2026-05-18T09:00:00Z', value: 5300 },
          ],
        },
        {
          scenario: 'forecast_ifs_deterministic',
          scenario_id: 'forecast_ifs_deterministic',
          source: 'IFS',
          source_id: 'IFS',
          cycle_time: '2026-05-18T00:00:00Z',
          available_lead_hours: 144,
          segment_role: 'future_7_days',
          data: [{ valid_time: '2026-05-18T06:00:00Z', value: 5100 }],
        },
      ],
    }
    const timeline: ApiFloodAlertTimeline = {
      run_id: run.run_id,
      segment_id: 'seg-123',
      river_segment_id: 'yangtze_rivnet_v12_riv_000123',
      river_network_version_id: 'yangtze_rivnet_v12',
      timesteps: [],
      timeline: [],
      peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 5242 },
      frequency_thresholds: null,
      quality_note: 'degraded sample',
    }
    const lineage: ApiLineageResponse = {
      target_type: 'river_point',
      target_id: 'yangtze_rivnet_v12_riv_000123',
      nodes: [{ id: 'run' }],
      edges: [],
    }

    const detail = normalizeSelectedSegmentDetail({
      query: { ...query, source: 'compare' },
      basin,
      basinVersionId: 'yangtze_v2026_01',
      segmentId: 'yangtze_rivnet_v12_riv_000123',
      segment,
      feature: featureCollection.features[0],
      model,
      forecast,
      floodTimeline: timeline,
      lineage,
      floodAlert: rankingItem,
    })

    expect(detail).toMatchObject({
      basinId: 'yangtze',
      basinVersionId: 'yangtze_v2026_01',
      riverSegmentId: 'yangtze_rivnet_v12_riv_000123',
      currentQ: 5242,
      qUnit: 'm3/s',
      returnPeriod: 20,
      warningLevel: 'warning',
      comparisonAvailable: true,
      lineageStatus: 'available',
      handoffUrl:
        '/?source=compare&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=yangtze_v2026_01&riverNetworkVersionId=yangtze_rivnet_v12&segmentId=yangtze_rivnet_v12_riv_000123',
      geometry: featureCollection.features[0].geometry,
    })
    expect(detail.sourceSelection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'GFS+IFS',
      comparisonAvailable: true,
    })
    expect(detail.trendPoints.map((point) => point.source)).toEqual(['GFS', 'GFS', 'IFS'])
  })

  it('uses the fallback current trend point valid time consistently when query validTime is absent', () => {
    const forecast: ApiForecastPayload = {
      river_segment_id: 'yangtze_rivnet_v12_riv_000123',
      issue_time: '2026-05-18T00:00:00Z',
      variable: 'q_down',
      unit: 'm3/s',
      frequency_thresholds: null,
      segments: [
        {
          scenario: 'forecast_gfs_deterministic',
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          source_id: 'GFS',
          cycle_time: '2026-05-18T00:00:00Z',
          available_lead_hours: 168,
          segment_role: 'future_7_days',
          data: [
            { valid_time: '2026-05-18T06:00:00Z', value: 100 },
            { valid_time: '2026-05-18T12:00:00Z', value: 120 },
          ],
        },
      ],
    }

    const detail = normalizeSelectedSegmentDetail({
      query: { ...query, source: 'gfs', validTime: '2026-05-18T09:00:00Z' },
      basin,
      basinVersionId: 'yangtze_v2026_01',
      segmentId: 'yangtze_rivnet_v12_riv_000123',
      segment,
      feature: featureCollection.features[0],
      model,
      forecast,
    })

    expect(detail.currentQ).toBe(120)
    expect(detail.freshness.validTime).toBe('2026-05-18T12:00:00.000Z')
    expect(detail.handoffUrl).toContain('validTime=2026-05-18T12%3A00%3A00.000Z')
    expect(detail.handoffUrl).not.toContain('2026-05-18T09%3A00%3A00')
  })

  it('sanitizes malformed and oversized selected segment LineString geometry before detail storage', () => {
    const malformed = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'LineString',
      coordinates: [[100, 30]],
    })
    const tooManyCoordinates = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'LineString',
      coordinates: Array.from({ length: m11SelectedSegmentGeometryBudget.maxCoordinates + 1 }, (_, index) => [
        100 + index / 100_000,
        30,
      ]),
    })
    const tooManyDimensions = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'LineString',
      coordinates: [
        [100, 30],
        [101, 31, 1, 2],
      ],
    })

    expect(malformed).toMatchObject({
      ok: false,
      reason: 'Selected segment geometry requires at least two coordinates.',
      sanitizedGeometry: null,
    })
    expect(tooManyCoordinates).toMatchObject({
      ok: false,
      reason: expect.stringContaining('exceeds client rendering budget'),
      sanitizedGeometry: null,
    })
    expect(tooManyDimensions).toMatchObject({
      ok: false,
      reason: expect.stringContaining('coordinate dimensions exceed'),
      sanitizedGeometry: null,
    })
  })

  it('accepts a gap-split MultiLineString and counts coordinates across its parts', () => {
    const status = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'MultiLineString',
      coordinates: [
        [
          [100, 30],
          [100.001, 30.001],
        ],
        [
          [100.02, 30.02],
          [100.021, 30.021, 8],
        ],
      ],
    })

    expect(status.ok).toBe(true)
    expect(status.sanitizedGeometry?.type).toBe('MultiLineString')
    expect(status.coordinateCount).toBe(4)
    const parts = status.sanitizedGeometry?.coordinates as number[][][]
    expect(parts).toHaveLength(2)
    expect(parts[1][1]).toEqual([100.021, 30.021, 8])
  })

  it('drops sub-two-point parts and rejects a MultiLineString left with no renderable part', () => {
    const onlyShortParts = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'MultiLineString',
      coordinates: [[[100, 30]], [[101, 31]]],
    })
    expect(onlyShortParts).toMatchObject({
      ok: false,
      reason: 'Selected segment geometry requires at least two coordinates.',
      sanitizedGeometry: null,
    })

    const malformedPart = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'MultiLineString',
      coordinates: [
        [
          [100, 30],
          ['101' as unknown as number, 31],
        ],
      ],
    })
    expect(malformedPart).toMatchObject({
      ok: false,
      reason: 'Selected segment geometry is malformed.',
      sanitizedGeometry: null,
    })
  })

  it('enforces the coordinate budget on the recursive MultiLineString total', () => {
    const half = Math.ceil((m11SelectedSegmentGeometryBudget.maxCoordinates + 1) / 2)
    const part = (offset: number) =>
      Array.from({ length: half }, (_, index) => [100 + offset + index / 100_000, 30])
    const status = getM11SelectedSegmentGeometryBudgetStatus({
      type: 'MultiLineString',
      coordinates: [part(0), part(1)],
    })
    expect(status).toMatchObject({
      ok: false,
      reason: expect.stringContaining('exceeds client rendering budget'),
      sanitizedGeometry: null,
    })
  })

  it('keeps selected segment detail usable while omitting invalid selected segment geometry', () => {
    const detail = normalizeSelectedSegmentDetail({
      query,
      basin,
      basinVersionId: 'yangtze_v2026_01',
      segmentId: 'yangtze_rivnet_v12_riv_000123',
      segment: {
        ...segment,
        geom: { type: 'LineString', coordinates: [[Number.NaN, 30], [101, 31]] },
      },
      feature: featureCollection.features[0],
      model,
      floodAlert: rankingItem,
    })

    expect(detail).toMatchObject({
      riverSegmentId: 'yangtze_rivnet_v12_riv_000123',
      displayName: 'Yichang mainstem',
      currentQ: 5242,
      geometry: null,
      unavailableReason: 'Selected segment geometry is malformed.',
    })
  })

  it('sanitizes basin segment row geometry from river feature collections', () => {
    const rows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: null, q: null },
      featureCollection: {
        ...featureCollection,
        features: [
          {
            ...featureCollection.features[0],
            geometry: { type: 'LineString', coordinates: [[100, 30], ['101' as unknown as number, 31]] },
          },
        ],
      },
      rankingItems: [rankingItem],
    })

    expect(rows[0]).toMatchObject({
      riverSegmentId: 'yangtze_rivnet_v12_riv_000123',
      hasGeometry: false,
      geometry: null,
      unavailableReason: 'Selected segment geometry is malformed.',
    })
  })

  it('applies aggregate basin river geometry budget before row retention while preserving list metadata', () => {
    const features = Array.from({ length: m11BasinRiverCollectionBudget.maxFeatures + 2 }, (_, index) => ({
      ...featureCollection.features[0],
      properties: {
        ...featureCollection.features[0].properties,
        segment_id: `seg-budget-${index}`,
        river_segment_id: `river-budget-${index}`,
        name: `Budget Segment ${index}`,
      },
      geometry: { type: 'LineString' as const, coordinates: [[100, 30], [100.01, 30.01]] },
    }))
    const rankingItems = features.map((feature, index): ApiFloodAlertRankingItem => ({
      ...rankingItem,
      river_segment_id: feature.properties.river_segment_id,
      segment_id: feature.properties.segment_id,
      segment_name: feature.properties.name,
      q_value: 100 + index,
      warning_level: index % 2 === 0 ? 'warning' : 'watch',
    }))

    const rows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: null, q: null },
      featureCollection: { ...featureCollection, total: features.length, feature_total: features.length, features },
      rankingItems,
    })
    const retainedRows = rows.filter((row) => row.geometry)
    const skippedRow = rows[m11BasinRiverCollectionBudget.maxFeatures]

    expect(rows).toHaveLength(m11BasinRiverCollectionBudget.maxFeatures + 2)
    expect(retainedRows).toHaveLength(m11BasinRiverCollectionBudget.maxFeatures)
    expect(skippedRow).toMatchObject({
      riverSegmentId: `river-budget-${m11BasinRiverCollectionBudget.maxFeatures}`,
      segmentId: `seg-budget-${m11BasinRiverCollectionBudget.maxFeatures}`,
      displayName: `Budget Segment ${m11BasinRiverCollectionBudget.maxFeatures}`,
      currentQ: 100 + m11BasinRiverCollectionBudget.maxFeatures,
      warningLevel: 'warning',
      qualityFlag: 'ok',
      hasGeometry: false,
      geometry: null,
    })
    expect(skippedRow.unavailableReason).toContain('aggregate client rendering budget')
    expect(filterBasinSegmentRows(rows, { warningLevel: 'watch', q: 'Budget Segment 2001' })).toHaveLength(1)
  })

  it('exposes unavailable source/scenario and failed lineage instead of fabricating values', () => {
    const selection = createSourceScenarioSelection({ source: 'compare', cycle: null, validTime: null }, ['GFS'])
    const detail = normalizeSelectedSegmentDetail({
      query: { ...query, source: 'ifs' },
      basinVersionId: 'yangtze_v2026_01',
      segmentId: 'missing-seg',
      forecast: null,
      lineageError: 'lineage backend unavailable',
    })

    expect(selection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'Unknown',
      comparisonAvailable: false,
      unavailableReason: 'Comparison requires both GFS and IFS series.',
    })
    expect(detail).toMatchObject({
      currentQ: null,
      lineageStatus: 'failed',
      lineageUnavailableReason: 'lineage backend unavailable',
      unavailableReason: 'Segment geometry/detail is unavailable.',
    })
  })

  it('derives best selected-segment provenance from the resolved run when forecast series is empty', () => {
    const detail = normalizeSelectedSegmentDetail({
      query: { ...query, source: 'best', cycle: null },
      basin,
      basinVersionId: 'yangtze_v2026_01',
      segmentId: 'yangtze_rivnet_v12_riv_000123',
      segment,
      feature: featureCollection.features[0],
      model,
      forecast: {
        river_segment_id: 'yangtze_rivnet_v12_riv_000123',
        issue_time: '2026-05-18T00:00:00Z',
        variable: 'q_down',
        unit: 'm3/s',
        frequency_thresholds: null,
        segments: [],
      },
      floodTimeline: {
        run_id: 'fcst_ifs_2026051800_yangtze_shud_v12',
        segment_id: 'seg-123',
        river_segment_id: 'yangtze_rivnet_v12_riv_000123',
        river_network_version_id: 'yangtze_rivnet_v12',
        timesteps: [],
        timeline: [],
        peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 5242 },
        frequency_thresholds: null,
        quality_note: null,
      },
      floodAlert: rankingItem,
      resolvedRun: {
        ...run,
        run_id: 'fcst_ifs_2026051800_yangtze_shud_v12',
        scenario_id: 'forecast_ifs_deterministic',
        source_id: 'IFS',
      },
      resolvedQuery: { ...query, source: 'ifs', cycle: null },
    })

    expect(detail.trendPoints).toEqual([])
    expect(detail.currentQ).toBe(5242)
    expect(detail.sourceSelection).toMatchObject({
      requestedSource: 'best',
      resolvedSource: 'IFS',
      scenarioIds: ['forecast_ifs_deterministic'],
      cycleTime: '2026-05-18T00:00:00Z',
      unavailableReason: null,
    })
    expect(detail.freshness).toMatchObject({
      runId: 'fcst_ifs_2026051800_yangtze_shud_v12',
      source: 'IFS',
    })
    expect(detail.handoffUrl).toContain('/?source=ifs&')
    expect(detail.handoffUrl).not.toContain('source=best')
  })

  it('uses all relevant runs for overview compare availability', () => {
    const bothSources = normalizeOverviewSummary({
      query: { ...query, source: 'compare' },
      latestRun: run,
      runs: [
        run,
        {
          ...run,
          run_id: 'fcst_ifs_2026051800_yangtze_shud_v12',
          scenario_id: 'forecast_ifs_deterministic',
          source_id: 'IFS',
        },
      ],
    })
    const missingIfs = normalizeOverviewSummary({
      query: { ...query, source: 'compare' },
      latestRun: run,
      runs: [run],
    })

    expect(bothSources.sourceSelection).toMatchObject({
      resolvedSource: 'GFS+IFS',
      comparisonAvailable: true,
      unavailableReason: null,
    })
    expect(missingIfs.sourceSelection).toMatchObject({
      resolvedSource: 'Unknown',
      comparisonAvailable: false,
      unavailableReason: 'Comparison requires both GFS and IFS series.',
    })
  })

  it('computes basin version bbox without array flattening or spread-sized allocations', () => {
    const coordinates: number[][][] = [[]]
    for (let index = 0; index < 20_000; index += 1) {
      coordinates[0].push([100 + index * 0.0001, 30 - index * 0.0001])
    }
    coordinates[0].push([...coordinates[0][0]])

    const [normalized] = normalizeOverviewBasins({
      basins: [basin],
      versionsByBasinId: {
        yangtze: [
          {
            ...basinVersion,
            geom: {
              type: 'MultiPolygon',
              coordinates: [coordinates],
            },
          },
        ],
      },
    })

    expect(normalized.bbox).toEqual({
      minLon: 100,
      minLat: 28.0001,
      maxLon: 101.9999,
      maxLat: 30,
    })
  })

  it('marks oversized single MultiPolygon boundaries unavailable before bbox or area processing', () => {
    const ring: number[][] = []
    for (let index = 0; index < m11BasinGeometryBudget.maxVertices + 2; index += 1) {
      ring.push([100 + index * 0.00001, 30])
    }

    const [normalized] = normalizeOverviewBasins({
      basins: [basin],
      versionsByBasinId: {
        yangtze: [
          {
            ...basinVersion,
            geom: {
              type: 'MultiPolygon',
              coordinates: [[[...ring]]],
            },
          },
        ],
      },
    })

    expect(normalized.boundary).toBeNull()
    expect(normalized.bbox).toBeNull()
    expect(normalized.areaKm2).toBeNull()
    expect(normalized.basinVersions[0].unavailableReason).toContain('exceeds client rendering budget')
    expect(normalized.qualityNote).toBe('One or more basin versions have missing geometry.')
  })

  it('returns measurable aggregation endpoint decisions for all rule branches', () => {
    expect(
      decideAggregationEndpoint({ initialRequestCount: 8, createsPerBasinNPlusOne: false, missingRequiredFields: [] }),
    ).toMatchObject({ needsAggregationEndpoint: false, reason: 'reuse-existing' })
    expect(
      decideAggregationEndpoint({ initialRequestCount: 9, createsPerBasinNPlusOne: false, missingRequiredFields: [] }),
    ).toMatchObject({ needsAggregationEndpoint: true, reason: 'too-many-initial-requests' })
    expect(
      decideAggregationEndpoint({ initialRequestCount: 4, createsPerBasinNPlusOne: true, missingRequiredFields: [] }),
    ).toMatchObject({ needsAggregationEndpoint: true, reason: 'per-basin-n-plus-one' })
    expect(
      decideAggregationEndpoint({
        initialRequestCount: 4,
        createsPerBasinNPlusOne: false,
        missingRequiredFields: ['basin_area_km2'],
      }),
    ).toMatchObject({ needsAggregationEndpoint: true, reason: 'missing-required-field' })
  })
})
