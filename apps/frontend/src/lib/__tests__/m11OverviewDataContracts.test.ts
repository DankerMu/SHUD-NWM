import { describe, expect, it, vi } from 'vitest'

import type { components } from '@/api/types'
import {
  createSourceScenarioSelection,
  decideAggregationEndpoint,
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
          { level: 'normal', count: 5, color: '#808080' },
          { level: 'severe', count: 3, color: '#DC143C' },
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
    expect(summary.qualityNotes).toContain('2 curves unavailable')

    vi.useRealTimers()
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

  it('normalizes basin detail and segment rows with query-driven filters and unavailable rows', () => {
    const detail = normalizeBasinDetail({
      query,
      basin,
      versions: [basinVersion],
      models: [model],
      segments: featureCollection,
      rankingItems: [rankingItem],
      latestRun: run,
    })
    const rows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: 'orange', q: 'Yichang' },
      featureCollection,
      rankingItems: [rankingItem],
    })
    const allRows = normalizeBasinSegmentRows({
      query: { ...query, warningLevel: null, q: null },
      featureCollection,
      rankingItems: [rankingItem],
    })

    expect(detail).toMatchObject({
      basinId: 'yangtze',
      selectedBasinVersionId: 'yangtze_v2026_01',
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
      handoffUrl: '/forecast?segmentId=yangtze_rivnet_v12_riv_000123&basinVersionId=yangtze_v2026_01',
    })
    expect(detail.sourceSelection).toMatchObject({
      requestedSource: 'compare',
      resolvedSource: 'GFS+IFS',
      comparisonAvailable: true,
    })
    expect(detail.trendPoints.map((point) => point.source)).toEqual(['GFS', 'GFS', 'IFS'])
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
