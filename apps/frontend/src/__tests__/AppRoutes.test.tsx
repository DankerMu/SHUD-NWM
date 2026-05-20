import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { forwardRef, useEffect, useImperativeHandle, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '@/App'
import { client } from '@/api/client'
import { contextHandoff } from '@/pages/OverviewPage'
import { useAuthStore } from '@/stores/auth'
import { useFloodAlertStore } from '@/stores/floodAlert'
import { useForecastStore, type ForecastSegmentInfo } from '@/stores/forecast'
import { useModelAssetsStore, type ModelAsset, type ModelAssetPage } from '@/stores/modelAssets'
import { useMonitoringStore } from '@/stores/monitoring'
import { useOverviewDataStore } from '@/stores/overviewData'
import { FORECAST_CHART_POINT_BUDGET } from '@/lib/forecastRenderingBudget'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import { serializeM11QueryState, type M11QueryState } from '@/lib/m11/queryState'

const m11FitBoundsCalls: Array<unknown[]> = []
const m11FlyToCalls: Array<unknown> = []
const floodAlertMapProps: Array<Record<string, unknown>> = []

function floodApiResponse<T>(data: T) {
  return new Response(JSON.stringify(success(data)), { headers: { 'content-type': 'application/json' } })
}

function geoJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

function success<T>(data: T) {
  return { status: 'success', data }
}

vi.mock('@/components/map/MapView', () => ({
  MapView: ({
    onBasinContextLoaded,
    onSegmentSelect,
  }: {
    onBasinContextLoaded?: (context: { basinId: string; basinVersionId: string } | null) => void
    onSegmentSelect?: (segment: ForecastSegmentInfo) => void
  }) => {
    useEffect(() => {
      onBasinContextLoaded?.({ basinId: 'basin-demo', basinVersionId: 'bv-001' })
    }, [onBasinContextLoaded])
    return (
      <button
        type="button"
        aria-label="河网地图"
        onClick={() => onSegmentSelect?.({ segmentId: 'seg-010', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-v1' })}
      >
        mock map
      </button>
    )
  },
}))

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

vi.mock('react-map-gl/maplibre', () => ({
  default: forwardRef(
    (
      {
        children,
        interactiveLayerIds,
        onMouseMove,
        onMouseLeave,
        onClick,
      }: {
        children: ReactNode
        interactiveLayerIds?: string[]
        onMouseMove?: (event: unknown) => void
        onMouseLeave?: (event: unknown) => void
        onClick?: (event: unknown) => void
      },
      ref,
    ) => {
      useImperativeHandle(ref, () => ({
        fitBounds: (...args: unknown[]) => m11FitBoundsCalls.push(args),
        flyTo: (args: unknown) => m11FlyToCalls.push(args),
      }))
      return (
        <div
          data-testid="mock-m11-maplibre-map"
          data-interactive-layer-ids={(interactiveLayerIds ?? []).join(',')}
          onMouseMove={() => onMouseMove?.({ target: { getCanvas: () => ({ style: {} }) }, features: [] })}
          onMouseLeave={() => onMouseLeave?.({ target: { getCanvas: () => ({ style: {} }) }, features: [] })}
          onClick={() => onClick?.({ target: { getCanvas: () => ({ style: {} }) }, features: [] })}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            onClick?.({
              target: { getCanvas: () => ({ style: {} }) },
              features: [
                {
                  layer: { id: 'm11-basin-river-line' },
                  properties: {
                    river_segment_id: 'seg-001',
                    segment_id: 'seg-001',
                    basin_version_id: 'bv-001',
                    river_network_version_id: 'rn-v1',
                  },
                },
              ],
            })
          }}
          onDoubleClick={() =>
            onClick?.({
              target: { getCanvas: () => ({ style: {} }) },
              features: [
                {
                  layer: { id: 'm11-flood-return-period-line' },
                  properties: { segment_id: 'overlay-first', river_network_version_id: 'rn-v1' },
                },
                { layer: { id: 'm11-basin-fill' }, properties: { basin_id: 'basin-demo' } },
              ],
            })
          }
        >
          {children}
        </div>
      )
    },
  ),
  Source: ({ children, ...props }: { children: ReactNode } & Record<string, unknown>) => (
    <div
      data-testid="mock-m11-map-source"
      data-source-id={String(props.id ?? '')}
      data-source-type={String(props.type ?? '')}
      data-source-data={String(props.data ?? '')}
      data-source-tiles={Array.isArray(props.tiles) ? props.tiles.join(',') : ''}
    >
      {children}
    </div>
  ),
  Layer: (props: Record<string, unknown>) => <div data-testid="mock-m11-map-layer" data-layer-id={String(props.id ?? '')} />,
  NavigationControl: () => <div />,
  ScaleControl: () => <div />,
}))

type MockForecastPanelProps = {
  segment: ForecastSegmentInfo
  loading: boolean
  error: string | null
  contextNote?: string | null
  onRetry?: () => void
}

vi.mock('@/components/forecast/ForecastPanel', () => ({
  ForecastPanel: ({ segment, loading, error, contextNote, onRetry }: MockForecastPanelProps) => (
    <aside>
      mock forecast panel
      <div>{segment.segmentId}</div>
      <div>{segment.basinVersionId}</div>
      <div>{loading ? 'forecast loading' : 'forecast idle'}</div>
      <button type="button" onClick={onRetry}>
        mock retry forecast
      </button>
      {contextNote ? <div>{contextNote}</div> : null}
      {error ? <div>{error}</div> : null}
    </aside>
  ),
}))

vi.mock('@/components/flood/FloodAlertMap', () => ({
  FloodAlertMap: (props: Record<string, unknown>) => {
    floodAlertMapProps.push(props)
    return <div>mock flood map</div>
  },
}))

vi.mock('@/components/charts/QueueDonut', () => ({
  QueueDonut: () => <div>mock queue chart</div>,
}))

vi.mock('@/components/charts/StageDurationBar', () => ({
  StageDurationBar: () => <div>mock stage chart</div>,
}))

vi.mock('@/components/charts/TrendLine', () => ({
  TrendLine: () => <div>mock trend chart</div>,
}))

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-echarts-option">{JSON.stringify(option)}</pre>,
}))

const noopAsync = vi.fn().mockResolvedValue(undefined)
const overviewAsync = vi.fn().mockResolvedValue(undefined)

const m11LayerFreshness = {
  updatedAt: null,
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  runId: 'run-gfs',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-v1',
  source: 'GFS' as const,
  isStale: false,
  staleAfterHours: 6,
  unavailableReason: null,
}

const m11FloodMvtMetadata: NonNullable<LayerState['metadata']> = {
  layer_id: 'flood-return-period',
  tile_format: 'mvt',
  url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
  tile_url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
  maplibre_source_layer: 'flood_return_period',
  source_layer: 'flood_return_period',
  fallback_available: true,
  release_blocking: false,
  required_placeholders: ['run_id', 'duration', 'valid_time', 'z', 'x', 'y'],
  source_refs: {
    run_id: 'run-gfs',
    source_version: 'rnv-v1',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-v1',
    duration: '1h',
  },
}

const m11Layers: LayerState[] = [
  {
    layerId: 'discharge',
    displayName: 'River discharge',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z'],
    currentValidTime: '2026-05-18T06:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    metadata: null,
    freshness: m11LayerFreshness,
    legend: [{ label: '<500 m3/s', color: '#90CAF9', max: 500 }],
  },
  {
    layerId: 'flood-return-period',
    displayName: 'Flood return period',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T06:00:00.000Z'],
    currentValidTime: '2026-05-18T06:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    metadata: m11FloodMvtMetadata,
    freshness: m11LayerFreshness,
    legend: [{ label: 'warning', color: '#FFB74D', min: 10, max: 20 }],
  },
]

const staleOverviewLayers: LayerState[] = [
  {
    ...m11Layers[0],
    layerId: 'discharge',
    currentValidTime: '2026-05-17T00:00:00.000Z',
    validTimes: ['2026-05-17T00:00:00.000Z'],
    freshness: {
      ...m11LayerFreshness,
      validTime: '2026-05-17T00:00:00.000Z',
    },
  },
]

const m11SourceSelection = {
  requestedSource: 'best' as const,
  resolvedSource: 'GFS' as const,
  scenarioIds: ['forecast_gfs_deterministic'],
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  comparisonAvailable: false,
  provenanceLabel: 'Best Available (GFS) / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
  unavailableReason: null,
}

function m11Summary(completedCyclesToday = 1) {
  return {
    completedCyclesToday,
    runningJobs: 0,
    warningSegmentCount: 0,
    latestUpdate: null,
    totalBasins: 0,
    totalSegments: null,
    sourceSelection: m11SourceSelection,
    freshness: m11LayerFreshness,
    qualityNotes: [],
    partialErrors: [],
  }
}

function overviewSnapshot(layers: LayerState[], queryKey = '', dataKey = queryKey, completedCyclesToday = 1) {
  return {
    requestScope: {
      kind: 'overview' as const,
      queryKey,
      dataKey,
      source: 'gfs' as const,
      layer: 'discharge' as const,
      cycle: null,
      validTime: null,
      basemap: 'vector' as const,
      basinVersionId: null,
      riverNetworkVersionId: null,
      segmentId: null,
      warningLevel: null,
      q: null,
    },
    basins: [],
    layers,
    aggregationDecision: {
      needsAggregationEndpoint: false,
      reason: 'reuse-existing' as const,
      evidence: 'test',
    },
    summary: m11Summary(completedCyclesToday),
  }
}

function overviewSnapshotWithBasin(layers: LayerState[], queryKey = '', dataKey = queryKey, basinId = 'basin-demo') {
  return {
    ...overviewSnapshot(layers, queryKey, dataKey),
    basins: [
      {
        basinId,
        displayName: 'Demo Basin',
        basinGroup: null,
        parentBasinId: null,
        level: 1,
        boundary: {
          type: 'MultiPolygon',
          coordinates: [[[[100, 30], [105, 30], [105, 35], [100, 35], [100, 30]]]],
        },
        bbox: { minLon: 100, minLat: 30, maxLon: 105, maxLat: 35 },
        areaKm2: null,
        riverCount: null,
        activeModelCount: 1,
        latestForecastTime: null,
        warningCounts: {
          normal: 0,
          elevated: 0,
          watch: 0,
          warning: 0,
          high_risk: 0,
          severe: 0,
          extreme: 0,
          unavailable: 0,
        },
        basinVersions: [],
        selectedBasinVersionId: 'bv-001',
        unavailableReason: null,
        qualityNote: null,
      },
    ],
  }
}

function overviewSnapshotWithBasins(layers: LayerState[], queryKey = '', dataKey = queryKey, basinVersionId = 'bv-sibling') {
  const base = overviewSnapshotWithBasin(layers, queryKey, dataKey, 'basin-demo')
  base.requestScope.basinVersionId = basinVersionId
  return {
    ...base,
    basins: [
      {
        ...base.basins[0],
        basinVersions: [
          {
            basinVersionId: 'bv-001',
            versionLabel: 'v2026_01',
            active: true,
            validFrom: null,
            validTo: null,
            sourceUri: null,
            boundary: base.basins[0].boundary,
            bbox: base.basins[0].bbox,
            unavailableReason: null,
          },
        ],
      },
      {
        ...base.basins[0],
        basinId: 'basin-sibling',
        displayName: 'Sibling Basin',
        selectedBasinVersionId: 'bv-sibling',
        basinVersions: [
          {
            basinVersionId: 'bv-sibling',
            versionLabel: 'v2026_02',
            active: true,
            validFrom: null,
            validTo: null,
            sourceUri: null,
            boundary: base.basins[0].boundary,
            bbox: base.basins[0].bbox,
            unavailableReason: null,
          },
        ],
      },
    ],
  }
}

function overviewSnapshotForQuery(query: M11QueryState) {
  const queryKey = serializeM11QueryState({ ...query, basemap: 'vector', validTime: null })
  const dataKey = serializeM11QueryState({ ...query, basemap: 'vector' })
  const snapshot = overviewSnapshotWithBasin(m11Layers, queryKey, dataKey)
  return {
    ...snapshot,
    requestScope: {
      ...snapshot.requestScope,
      queryKey,
      dataKey,
      source: query.source,
      layer: query.layer,
      cycle: query.cycle,
      validTime: query.validTime,
      basinVersionId: query.basinVersionId,
      riverNetworkVersionId: query.riverNetworkVersionId,
      segmentId: query.segmentId,
      warningLevel: query.warningLevel,
      q: query.q,
    },
  }
}

function modelAssetRouteFixture(overrides: Partial<ModelAsset> = {}): ModelAsset {
  return {
    model_id: 'basins_qhh_shud',
    model_name: 'QHH SHUD',
    basin_id: 'basins_qhh',
    basin_name: 'QHH',
    basin_version_id: 'qhh-basin-v1',
    river_network_version_id: 'qhh-river-v1',
    mesh_version_id: 'qhh-mesh-v1',
    calibration_version_id: 'qhh-calib-v1',
    segment_count: 42,
    mesh_uri: 's3://nhms/models/qhh/mesh',
    mesh_checksum: 'mesh-sha',
    shud_code_version: 'shud-1',
    active_flag: true,
    model_package_uri: 'https://user:pass@assets.example.test/pkg?token=abc#frag',
    package_checksum: 'pkg-sha',
    manifest_uri: 's3://key:secret@nhms/private/manifest?sig=x#frag',
    source_inventory_checksum: 'inventory-sha',
    basin_slug: 'qhh',
    shud_input_name: 'qhh-input',
    source_path: '/volume/data/nwm/Basins/qhh',
    resolved_source_path: 'C:\\nwm\\Basins\\qhh',
    source_uri: 'file:///volume/data/nwm/Basins/qhh',
    source_is_symlink: false,
    resource_profile: {
      area_km2: 87.5,
      source_lineage: {
        source_uri: 'https://user:pass@assets.example.test/pkg?token=abc#frag',
        source_path: '/volume/data/nwm/Basins/qhh',
      },
      product_assets: [
        {
          id: 'package',
          label: 'Package',
          checksum: 'pkg-sha',
          uri: 's3://key:secret@nhms/private/package?sig=x#frag',
        },
      ],
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
    created_at: '2026-05-14T00:00:00Z',
    ...overrides,
  }
}

const unsafeModelAssetError =
  'failed to inspect /volume/data/nwm/Basins/qhh and C:\\nwm\\Basins\\qhh from file:///volume/data/nwm/Basins/qhh?token=abc#frag via https://user:pass@assets.example.test/pkg?token=abc#frag'
const unsafeModelAssetErrorTokens = [
  '/volume/data/nwm/Basins/qhh',
  'C:\\nwm\\Basins\\qhh',
  'file://',
  'user:pass',
  'token=abc',
  '#frag',
] as const

function expectNoUnsafeModelAssetErrorTextInRoute() {
  const bodyText = document.body.textContent ?? ''
  for (const token of unsafeModelAssetErrorTokens) {
    expect(bodyText).not.toContain(token)
  }
}

function basinSnapshot(
  basinId: string,
  layers: LayerState[],
  queryKey = '',
  dataKey = queryKey,
  currentQ: number | null = 12,
  comparisonAvailable = true,
  segments = [
    {
      riverSegmentId: 'seg-009',
      riverNetworkVersionId: 'rn-v1',
      segmentId: 'seg-009',
      displayName: 'Main Stem 009',
      basinVersionId: 'bv-001',
      streamOrder: 3,
      lengthM: 1200,
      currentQ,
      qUnit: 'm3/s',
      returnPeriod: 10,
      warningLevel: 'warning' as const,
      qualityFlag: 'ok' as const,
      qualityNote: null,
      source: 'GFS' as const,
      cycleTime: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
      hasGeometry: true,
      geometry: { type: 'LineString' as const, coordinates: [[101, 31], [102, 32]] },
      unavailableReason: null,
    },
  ],
) {
  return {
    requestScope: {
      kind: 'basin-detail' as const,
      queryKey,
      dataKey,
      basinId,
      source: 'gfs' as const,
      layer: 'discharge' as const,
      cycle: null,
      validTime: null,
      basemap: 'vector' as const,
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-v1',
      segmentId: 'seg-009',
      warningLevel: null,
      q: null,
    },
    detail: {
      basinId,
      displayName: 'Demo Basin',
      basinGroup: null,
      selectedBasinVersionId: 'bv-001',
      basinVersions: [],
      boundary: {
        type: 'MultiPolygon',
        coordinates: [[[[101, 31], [104, 31], [104, 34], [101, 34], [101, 31]]]],
      },
      bbox: { minLon: 101, minLat: 31, maxLon: 104, maxLat: 34 },
      segmentCount: 1,
      warningDistribution: {
        normal: 0,
        elevated: 0,
        watch: 0,
        warning: 0,
        high_risk: 0,
        severe: 0,
        extreme: 0,
        unavailable: 0,
      },
      activeModelCount: 1,
      latestRun: m11LayerFreshness,
      sourceSelection: m11SourceSelection,
      unavailableReason: null,
      partialErrors: [],
    },
    segments,
    selectedSegment: currentQ === null
      ? null
      : {
          basinId,
          basinName: 'Demo Basin',
          basinVersionId: 'bv-001',
          riverSegmentId: 'seg-009',
          segmentId: 'seg-009',
          displayName: 'Segment 009',
          modelId: 'model-demo',
          riverNetworkVersionId: 'rn-v1',
          currentQ,
          qUnit: 'm3/s',
          returnPeriod: 2,
          warningLevel: 'watch' as const,
          qualityFlag: 'ok' as const,
          qualityNote: null,
          sourceSelection: { ...m11SourceSelection, comparisonAvailable },
          trendPoints: [
            { validTime: '2026-05-18T00:00:00.000Z', value: 10, source: 'GFS' as const, scenarioId: 'forecast_gfs_deterministic', role: 'analysis', isAnalysis: true },
            { validTime: '2026-05-18T06:00:00.000Z', value: currentQ, source: 'GFS' as const, scenarioId: 'forecast_gfs_deterministic', role: 'future_7_days', isAnalysis: false },
            { validTime: '2026-05-18T06:00:00.000Z', value: currentQ + 7, source: 'IFS' as const, scenarioId: 'forecast_ifs_deterministic', role: 'future_7_days', isAnalysis: false },
          ],
          comparisonAvailable,
          lineageStatus: 'available' as const,
          lineageUnavailableReason: null,
          handoffUrl: '/forecast?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          geometry: { type: 'LineString' as const, coordinates: [[101, 31], [102, 32]] },
          freshness: m11LayerFreshness,
          unavailableReason: null,
        },
    layers,
  }
}

const overviewDefaultScopeKey = 'source=gfs'
const overviewFloodScopeKey = 'source=gfs&layer=flood-return-period'
const overviewValid06ScopeKey = 'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z'
const overviewFloodValid06ScopeKey = 'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period'
const basinDefaultScopeKey = 'source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009'
const basinValid06ScopeKey = 'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009'

function mockSegmentDetailRouteClient(segmentProperties: Record<string, unknown> = {}) {
  vi.mocked(client.GET).mockImplementation((async (path: string) => {
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
      return {
        data: success({
          river_segment_id: 'seg-009',
          river_network_version_id: 'rn-v1',
          length_m: 1200,
          geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
          properties_json: segmentProperties,
          created_at: '2026-05-18T00:00:00Z',
        }),
        error: undefined,
      }
    }
    if (String(path).endsWith('/forecast-series')) {
      return {
        data: success({
          river_segment_id: 'seg-009',
          issue_time: '2026-05-18T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          segments: [
            {
              scenario: 'forecast_gfs_deterministic',
              scenario_id: 'forecast_gfs_deterministic',
              source: 'GFS',
              cycle_time: '2026-05-18T00:00:00Z',
              segment_role: 'future_7_days',
              data: [{ valid_time: '2026-05-18T06:00:00Z', value: 3225 }],
            },
          ],
          frequency_thresholds: { Q2: 100, Q5: 200, Q10: 300, Q20: 400, Q50: 500, Q100: 600 },
        }),
        error: undefined,
      }
    }
    return { data: success({ target_type: 'river_segment', target_id: 'seg-009', nodes: [], edges: [] }), error: undefined }
  }) as never)
}

function mockSegmentDetailRouteClientWithOptions(options: {
  geom?: unknown
  forecastSeries?: unknown[]
  frequencyThresholds?: Record<string, unknown> | null
}) {
  vi.mocked(client.GET).mockImplementation((async (path: string) => {
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
      return {
        data: success({
          river_segment_id: 'seg-009',
          river_network_version_id: 'rn-v1',
          length_m: 1200,
          geom: options.geom,
          properties_json: {},
          created_at: '2026-05-18T00:00:00Z',
        }),
        error: undefined,
      }
    }
    if (String(path).endsWith('/forecast-series')) {
      return {
        data: success({
          segment_id: 'seg-009',
          issue_time: '2026-05-18T00:00:00Z',
          unit: 'm3/s',
          series: options.forecastSeries ?? [],
          frequency_thresholds: options.frequencyThresholds ?? null,
        }),
        error: undefined,
      }
    }
    return { data: success({}), error: undefined }
  }) as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  m11FitBoundsCalls.length = 0
  m11FlyToCalls.length = 0
  floodAlertMapProps.length = 0
  overviewAsync.mockResolvedValue(undefined)
  useAuthStore.setState({ role: 'viewer' })
  useFloodAlertStore.setState({
    selectedRunId: null,
    latestRun: null,
    selectedAlertLevel: null,
    selectedValidTime: null,
    topLimit: 20,
    basinId: '',
    validTimes: [],
    summaryData: null,
    rankingData: null,
    loading: false,
    summaryLoading: false,
    rankingLoading: false,
    timelineLoading: false,
    error: null,
    empty: false,
    fetchLatestFrequencyDoneRun: noopAsync,
    fetchSummary: noopAsync,
    fetchRanking: noopAsync,
  })
  useForecastStore.setState(
    {
      ...useForecastStore.getInitialState(),
    },
    true,
  )
  useModelAssetsStore.setState(useModelAssetsStore.getInitialState(), true)
  vi.mocked(client.GET).mockResolvedValue({ data: success([]), error: undefined } as never)
  useMonitoringStore.setState({
    source: 'GFS',
    cycleTime: '2026-05-09T00:00:00Z',
    cycle: null,
    stages: [],
    jobs: [],
    jobTotal: 0,
    queue: null,
    queueError: null,
    jobFilters: { page: 1, pageSize: 12, sortBy: 'submitted_at', sortOrder: 'desc' },
    isPolling: false,
    isJobsLoading: false,
    error: null,
    fetchAll: noopAsync,
    fetchJobs: noopAsync,
  })
  useOverviewDataStore.setState({
    overview: null,
    basinDetail: null,
    loading: false,
    basinLoading: false,
    error: null,
    basinError: null,
    loadOverview: overviewAsync,
    loadBasinDetail: overviewAsync,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App route state', () => {
  it('routes / to the national overview shell and marks navigation active', async () => {
    window.history.pushState({}, '', '/')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByLabelText('全国总览地图')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /全国总览/ })).toHaveClass('border-accent')
  })

  it('routes /overview with normalized query state', async () => {
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&basemap=terrain')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByText('source')).toBeInTheDocument()
    expect(screen.getByText('gfs')).toBeInTheDocument()
    expect(screen.getAllByText('flood-return-period').length).toBeGreaterThan(0)
    expect(screen.getAllByText('terrain').length).toBeGreaterThan(0)
  })

  it('routes /meteorology grid tab with public navigation', async () => {
    window.history.pushState({}, '', '/meteorology?tab=grid&source=GFS&variable=PRCP')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '气象数据产品' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /气象数据/ })).toHaveClass('border-accent')
    const tablist = screen.getByRole('tablist', { name: '气象产品标签' })
    expect(within(tablist).getByRole('tab', { selected: true, name: /空间栅格/ })).toBeInTheDocument()
    expect(screen.getByTestId('grid-unavailable')).toHaveTextContent('实时栅格瓦片服务尚未接入')
  })

  it('routes /meteorology stations tab with station inventory state', async () => {
    window.history.pushState({}, '', '/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '气象数据产品' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /气象数据/ })).toHaveClass('border-accent')
    const tablist = screen.getByRole('tablist', { name: '气象产品标签' })
    expect(within(tablist).getByRole('tab', { selected: true, name: /气象代站/ })).toBeInTheDocument()
    expect(screen.getByLabelText('流域', { selector: 'select' })).toHaveValue('yangtze')
    expect(screen.getByTestId('station-inventory')).toHaveTextContent('HMT-Y2-0236')
    expect(screen.getByTestId('station-popup')).toHaveTextContent('HMT-Y2-0237')
  })

  it('renders overview shared controls and drives URL/query reload state', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, ''),
    })
    window.history.pushState({}, '', '/overview?source=best&validTime=2026-05-17T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))
    expect(screen.getByText('数据源与情景')).toBeInTheDocument()
    expect(screen.getByText('气象图层')).toBeInTheDocument()
    expect(screen.getByText('降水格点')).toBeInTheDocument()
    expect(screen.getByText('径流量图例')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '卫星底图' }))
    expect(window.location.search).toContain('basemap=satellite')

    await user.click(screen.getByRole('button', { name: /^IFS/ }))
    expect(window.location.search).toContain('source=ifs')
    await waitFor(() => expect(overviewAsync).toHaveBeenCalledWith(expect.objectContaining({ source: 'ifs' })))
  })

  it('preserves river network version in overview load state and renders the matching snapshot', async () => {
    const loadOverview = vi.fn().mockImplementation(async (query: M11QueryState) => {
      const snapshot = overviewSnapshotForQuery(query)
      useOverviewDataStore.setState({ overview: snapshot, loading: false })
      return snapshot
    })
    useOverviewDataStore.setState({
      loadOverview,
      loading: false,
    })
    window.history.pushState(
      {},
      '',
      '/overview?source=gfs&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() =>
      expect(loadOverview).toHaveBeenCalledWith(
        expect.objectContaining({
          source: 'gfs',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'seg-009',
        }),
      ),
    )
    expect(await screen.findByText('Demo Basin')).toBeInTheDocument()
    expect(screen.queryByText('总览数据加载中')).not.toBeInTheDocument()
    expect(useOverviewDataStore.getState().overview?.requestScope).toMatchObject({
      dataKey:
        'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
      riverNetworkVersionId: 'rn-v1',
    })
  })

  it('updates overview basemap URL and map style without reloading overview data', async () => {
    const user = userEvent.setup()
    const loadOverview = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, overviewDefaultScopeKey),
      loading: false,
      loadOverview,
    })
    window.history.pushState({}, '', '/overview?source=gfs')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(loadOverview).toHaveBeenCalled())
    const initialLoadCalls = loadOverview.mock.calls.length

    await user.click(screen.getByRole('button', { name: '卫星底图' }))

    const params = new URLSearchParams(window.location.search)
    expect(params.get('source')).toBe('gfs')
    expect(params.get('basemap')).toBe('satellite')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'satellite')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap-style', 'm11://basemaps/satellite')
    expect(loadOverview).toHaveBeenCalledTimes(initialLoadCalls)
  })

  it('does not correct overview valid time from a stale source/layer snapshot', async () => {
    const loadOverview = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      overview: overviewSnapshot(staleOverviewLayers, overviewDefaultScopeKey),
      loading: false,
      loadOverview,
    })
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&validTime=2026-05-16T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(loadOverview).toHaveBeenCalledWith(expect.objectContaining({ layer: 'flood-return-period' })))
    expect(window.location.search).toContain('validTime=2026-05-16T00%3A00%3A00.000Z')

    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, overviewFloodScopeKey),
      loading: false,
    })
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))
  })

  it('preserves overview URL valid-time changes that are valid for the active layer', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, overviewDefaultScopeKey, overviewValid06ScopeKey),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))

    await user.click(screen.getByRole('button', { name: '上一个有效时刻' }))

    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T00%3A00%3A00.000Z'))
    expect(window.location.search).not.toContain('validTime=2026-05-18T06%3A00%3A00.000Z')
  })

  it('hides stale overview summary while a valid-time reload is pending', async () => {
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, overviewDefaultScopeKey, overviewValid06ScopeKey, 7),
      loading: true,
    })
    window.history.pushState({}, '', '/overview?source=gfs&validTime=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T00%3A00%3A00.000Z'))
    expect(window.location.search).not.toContain('validTime=2026-05-18T06%3A00%3A00.000Z')
    expect(screen.queryByText('7')).not.toBeInTheDocument()
    expect(screen.getByText('今日完成周期').parentElement).toHaveTextContent('-')
    expect(screen.getByText('总览数据加载中')).toBeInTheDocument()
  })

  it('threads overview basin bbox and map handlers through the route surface', async () => {
    const tileFetch = vi.fn().mockImplementation(async () => geoJsonResponse({ type: 'FeatureCollection', features: [] }))
    vi.stubGlobal('fetch', tileFetch)
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(
        m11Layers,
        overviewFloodScopeKey,
        overviewFloodValid06ScopeKey,
      ),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[100, 30], [105, 35]], { padding: 36, duration: 450 }]]))
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period')
    expect(tileFetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    expect(screen.getAllByTestId('mock-m11-map-source').at(-1)).toHaveAttribute('data-source-type', 'vector')
    const sourceTiles = screen.getAllByTestId('mock-m11-map-source').at(-1)?.getAttribute('data-source-tiles')
    expect(sourceTiles).toContain('/api/v1/tiles/flood-return-period/run-gfs/1h/2026-05-18T06%3A00%3A00.000Z/{z}/{x}/{y}.pbf')
    expect(sourceTiles).toContain('_mvt_cache_version=')

    await userEvent.setup().hover(screen.getByTestId('mock-m11-maplibre-map'))
    await userEvent.setup().click(screen.getByTestId('mock-m11-maplibre-map'))
    expect(screen.getByRole('heading', { name: '全国总览' })).toBeInTheDocument()
  })

  it('syncs basin visibility toggles to the overview map source and preserves local state', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(
        m11Layers,
        overviewDefaultScopeKey,
        overviewValid06ScopeKey,
      ),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '1')

    await user.click(screen.getByRole('checkbox', { name: 'Demo Basin 可见' }))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', '')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '0')
    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('当前没有可见流域边界')

    await user.click(screen.getByRole('button', { name: '全选' }))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo')
  })

  it('does not expose popup or enabled analysis when all basins are hidden', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(
        m11Layers,
        overviewDefaultScopeKey,
        overviewValid06ScopeKey,
      ),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.queryByTestId('m11-basin-popup')).not.toBeInTheDocument()
    expect(screen.getByText('等待可见流域选择')).toHaveAttribute('aria-disabled', 'true')

    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))
    expect(screen.getByTestId('m11-basin-popup')).toHaveTextContent('Demo Basin')

    await user.click(screen.getByRole('button', { name: '全不选' }))
    expect(screen.queryByTestId('m11-basin-popup')).not.toBeInTheDocument()
    expect(screen.getByText('等待可见流域选择')).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('link', { name: '进入流域分析' })).not.toBeInTheDocument()
  })

  it('renders overview popup actions and context summary links after a basin click', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasins(
        m11Layers,
        'source=gfs&basinVersionId=bv-sibling&segmentId=seg-sibling',
        'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-sibling&segmentId=seg-sibling',
      ),
      loading: false,
    })
    window.history.pushState(
      {},
      '',
      '/overview?source=gfs&validTime=2026-05-18T06:00:00Z&basemap=satellite&basinVersionId=bv-sibling&segmentId=seg-sibling',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.queryByTestId('m11-basin-popup')).not.toBeInTheDocument()

    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))
    expect(await screen.findByTestId('m11-basin-popup')).toHaveTextContent('Demo Basin')
    expect(screen.getByRole('link', { name: /进入分析/ })).toHaveAttribute(
      'href',
      '/basins/basin-demo?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basemap=satellite&basinVersionId=bv-001',
    )
    expect(screen.getByRole('link', { name: /进入分析/ }).getAttribute('href')).not.toContain('segmentId=seg-sibling')
    expect(screen.getByRole('link', { name: /查看详情/ })).toHaveAttribute(
      'href',
      '/monitoring?basinId=basin-demo&basinVersionId=bv-001',
    )
    expect(screen.getByRole('link', { name: /产品监控摘要/ })).toHaveAttribute(
      'href',
      '/monitoring?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z',
    )
    expect(screen.getByRole('link', { name: /洪水预警摘要/ })).toHaveAttribute(
      'href',
      '/flood-alerts?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z',
    )
    expect(screen.getByTestId('m11-basin-popup')).toHaveTextContent('模型河段数')
  })

  it('resolves best summary links to the concrete overview source identity', async () => {
    const ifsSelection = {
      ...m11SourceSelection,
      resolvedSource: 'IFS' as const,
      scenarioIds: ['forecast_ifs_deterministic'],
      cycleTime: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
      provenanceLabel: 'Best Available (IFS) / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
    }
    expect(
      contextHandoff(
        '/monitoring',
        {
          ...overviewSnapshot(m11Layers).requestScope,
          source: 'best',
          layer: 'discharge',
          basemap: 'vector',
        },
        ifsSelection,
      ),
    ).toMatchObject({
      href: '/monitoring?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
    })
    expect(
      contextHandoff(
        '/flood-alerts',
        {
          ...overviewSnapshot(m11Layers).requestScope,
          source: 'best',
          layer: 'discharge',
          basemap: 'vector',
        },
        ifsSelection,
      ),
    ).toMatchObject({
      href: '/flood-alerts?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
    })
  })

  it('emits concrete IFS cycle handoffs from a best overview latest run', async () => {
    useOverviewDataStore.setState({
      overview: {
        ...overviewSnapshot([], '', ''),
        summary: {
          ...m11Summary(),
          sourceSelection: {
            ...m11SourceSelection,
            requestedSource: 'best',
            resolvedSource: 'IFS',
            scenarioIds: ['forecast_ifs_deterministic'],
            cycleTime: '2026-05-19T00:00:00.000Z',
            validTime: null,
            provenanceLabel: 'Best Available (IFS) / cycle 2026-05-19T00:00:00.000Z / current valid time',
          },
          freshness: {
            ...m11LayerFreshness,
            runId: 'run-ifs-latest',
            source: 'IFS',
            cycleTime: '2026-05-19T00:00:00.000Z',
            validTime: null,
          },
        },
      },
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=best')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /产品监控摘要/ })).toHaveAttribute(
      'href',
      '/monitoring?source=ifs&cycle=2026-05-19T00%3A00%3A00.000Z',
    )
    expect(screen.getByRole('link', { name: /洪水预警摘要/ })).toHaveAttribute(
      'href',
      '/flood-alerts?source=ifs&cycle=2026-05-19T00%3A00%3A00.000Z',
    )
  })

  it('omits concrete destination source context for compare summary links', async () => {
    const baseSnapshot = overviewSnapshot(m11Layers, 'source=compare', 'source=compare')
    useOverviewDataStore.setState({
      overview: {
        ...baseSnapshot,
        requestScope: {
          ...baseSnapshot.requestScope,
          source: 'compare',
          cycle: '2026-05-18T00:00:00.000Z',
          validTime: '2026-05-18T06:00:00.000Z',
          dataKey: 'source=compare&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
          queryKey: 'source=compare&cycle=2026-05-18T00%3A00%3A00.000Z',
        },
        summary: {
          ...m11Summary(),
          sourceSelection: {
            ...m11SourceSelection,
            requestedSource: 'compare',
            resolvedSource: 'GFS+IFS',
            scenarioIds: ['forecast_gfs_deterministic', 'forecast_ifs_deterministic'],
            provenanceLabel: 'GFS+IFS / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
          },
        },
      },
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=compare&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /产品监控摘要/ })).toHaveAttribute('href', '/monitoring')
    expect(screen.getByRole('link', { name: /洪水预警摘要/ })).toHaveAttribute('href', '/flood-alerts')
    expect(screen.getAllByText('GFS+IFS 对比暂不支持跨页保真，已省略具体源上下文')).toHaveLength(2)
  })

  it('does not emit fabricated basin or basin-version IDs when overview data is unavailable', async () => {
    window.history.pushState({}, '', '/overview')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    const disabledTarget = screen.getByText('等待可见流域选择')
    expect(disabledTarget).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('link', { name: /进入流域分析|等待可用流域/ })).not.toBeInTheDocument()
  })

  it('encodes overview drill-down basin ids as one path segment and preserves query serialization', async () => {
    const reservedBasinId = 'basin/demo?branch#run%25'
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(
        m11Layers,
        overviewFloodScopeKey,
        overviewFloodValid06ScopeKey,
        reservedBasinId,
      ),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    await userEvent.setup().click(await screen.findByText('Demo Basin'))
    const link = await screen.findByRole('link', { name: '进入流域分析' })
    const href = link.getAttribute('href') ?? ''
    const url = new URL(href, window.location.origin)

    expect(url.pathname).toBe(`/basins/${encodeURIComponent(reservedBasinId)}`)
    expect(url.pathname.split('/')).toHaveLength(3)
    expect(url.search).toBe(
      '?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001',
    )
    expect(url.hash).toBe('')
  })

  it('does not render static basin labels when overview basin inventory is empty', async () => {
    useOverviewDataStore.setState({
      overview: {
        requestScope: {
          kind: 'overview',
          queryKey: '',
          dataKey: '',
          source: 'best',
          layer: 'discharge',
          cycle: null,
          validTime: null,
          basemap: 'vector',
          basinVersionId: null,
          riverNetworkVersionId: null,
          segmentId: null,
          warningLevel: null,
          q: null,
        },
        basins: [],
        layers: [],
        aggregationDecision: {
          needsAggregationEndpoint: false,
          reason: 'reuse-existing',
          evidence: 'test',
        },
        summary: {
          completedCyclesToday: null,
          runningJobs: null,
          warningSegmentCount: null,
          latestUpdate: null,
          totalBasins: 0,
          totalSegments: null,
          sourceSelection: {
            requestedSource: 'gfs',
            resolvedSource: 'GFS',
            scenarioIds: ['forecast_gfs_deterministic'],
            cycleTime: null,
            validTime: null,
            comparisonAvailable: false,
            provenanceLabel: 'GFS / latest cycle / current valid time',
            unavailableReason: null,
          },
          freshness: {
            updatedAt: null,
            cycleTime: null,
            validTime: null,
            runId: null,
            source: 'GFS',
            isStale: false,
            staleAfterHours: 6,
            unavailableReason: 'No freshness metadata is available.',
          },
          qualityNotes: [],
          partialErrors: [],
        },
      },
    })
    window.history.pushState({}, '', '/overview')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByText('暂无可用流域数据')).toBeInTheDocument()
    expect(screen.queryByText('长江流域')).not.toBeInTheDocument()
    expect(screen.queryByText('黄河流域')).not.toBeInTheDocument()
    expect(screen.queryByText('珠江流域')).not.toBeInTheDocument()
    expect(screen.queryByText('松辽流域')).not.toBeInTheDocument()
  })

  it('renders unavailable markers for null overview summary fields and preserves real zero values', async () => {
    useOverviewDataStore.setState({
      overview: {
        requestScope: {
          kind: 'overview',
          queryKey: '',
          dataKey: '',
          source: 'best',
          layer: 'discharge',
          cycle: null,
          validTime: null,
          basemap: 'vector',
          basinVersionId: null,
          riverNetworkVersionId: null,
          segmentId: null,
          warningLevel: null,
          q: null,
        },
        basins: [],
        layers: [],
        aggregationDecision: {
          needsAggregationEndpoint: false,
          reason: 'reuse-existing',
          evidence: 'test',
        },
        summary: {
          completedCyclesToday: 0,
          runningJobs: null,
          warningSegmentCount: null,
          latestUpdate: null,
          totalBasins: 0,
          totalSegments: null,
          sourceSelection: {
            requestedSource: 'gfs',
            resolvedSource: 'GFS',
            scenarioIds: ['forecast_gfs_deterministic'],
            cycleTime: null,
            validTime: null,
            comparisonAvailable: false,
            provenanceLabel: 'GFS / latest cycle / current valid time',
            unavailableReason: null,
          },
          freshness: {
            updatedAt: null,
            cycleTime: null,
            validTime: null,
            runId: null,
            source: 'GFS',
            isStale: false,
            staleAfterHours: 6,
            unavailableReason: 'No freshness metadata is available.',
          },
          qualityNotes: [],
          partialErrors: [],
        },
      },
    })
    window.history.pushState({}, '', '/overview')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByText('当前运行中').parentElement).toHaveTextContent('-')
    expect(screen.getByText('超警河段').parentElement).toHaveTextContent('-')
    expect(screen.getByText('最新更新时间').parentElement).toHaveTextContent('-')
    expect(screen.queryByText('23')).not.toBeInTheDocument()
    expect(screen.queryByText('7')).not.toBeInTheDocument()
    expect(screen.queryByText('18')).not.toBeInTheDocument()
    expect(screen.queryByText('08:00')).not.toBeInTheDocument()
  })

  it('routes /forecast to the preserved hydrologic forecast workflow', async () => {
    window.history.pushState({}, '', '/forecast')

    render(<App />)

    expect((await screen.findAllByLabelText('河网地图')).length).toBeGreaterThan(0)
    expect(screen.getByText('请在地图上选择河段查看预报')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: '进入流域分析' })).toHaveAttribute(
      'href',
      '/basins/basin-demo?basinVersionId=bv-001',
    )
    expect(screen.getByRole('link', { name: /水文预报/ })).toHaveClass('border-accent')
  })

  it('clears stale forecast segment context when basin handoff changes basin version', async () => {
    window.history.pushState(
      {},
      '',
      '/forecast?segmentId=seg-009&basinVersionId=bv-route&riverNetworkVersionId=rn-route&source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange&q=main',
    )

    render(<App />)

    expect(await screen.findByText('mock forecast panel')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: '进入流域分析' })).toHaveAttribute(
      'href',
      '/basins/basin-demo?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&warningLevel=orange&q=main',
    )
  })

  it('hydrates forecast segment selection and loads forecast data from direct handoff query params', async () => {
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        segment_id: 'seg-009',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }),
      error: undefined,
    } as never)
    window.history.pushState(
      {},
      '',
      '/forecast?segmentId=seg-009&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange',
    )

    render(<App />)

    expect(await screen.findByText('mock forecast panel')).toBeInTheDocument()
    expect(screen.getByText('seg-009')).toBeInTheDocument()
    expect(screen.getByText('bv-001')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '进入流域分析' })).toHaveAttribute(
      'href',
      '/basins/basin-demo?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&warningLevel=orange',
    )
    expect(screen.getByText(/已保留 validTime=2026-05-18T06:00:00.000Z/)).toBeInTheDocument()
    await waitFor(() =>
      expect(client.GET).toHaveBeenCalledWith(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
        {
          params: {
            path: {
              basin_version_id: 'bv-001',
              segment_id: 'seg-009',
            },
            query: {
              river_network_version_id: 'rn-v1',
              issue_time: '2026-05-18T00:00:00.000Z',
              variables: 'q_down',
              scenarios: 'IFS',
              include_analysis: true,
            },
          },
        },
      ),
    )
    expect(client.GET).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(useForecastStore.getState()).toMatchObject({
        selectedSegment: { segmentId: 'seg-009', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-v1' },
        forecastData: { segmentId: 'seg-009' },
        loading: false,
      }),
    )
  })

  it('builds forecast selected segment detail handoff with the full canonical query scope', async () => {
    vi.mocked(client.GET).mockImplementation(async (_path: string, options?: { params?: { path?: Record<string, string> } }) => ({
      data: success({
        segment_id: options?.params?.path?.segment_id ?? 'seg-009',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-18T00:00:00Z',
            points: [['2026-05-18T06:00:00Z', 10]],
          },
        ],
        frequency_thresholds: null,
      }),
      error: undefined,
    }) as never)
    window.history.pushState({}, '', '/forecast')

    render(<App />)

    await userEvent.setup().click(await screen.findByRole('button', { name: '河网地图' }))
    await waitFor(() => expect(screen.getByRole('link', { name: '查看河段详情' })).toBeInTheDocument())
    expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
      'href',
      '/segments/seg-010?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-010',
    )
  })

  it('synthesizes forecast handoff validTime from the first non-analysis forecast point', async () => {
    vi.mocked(client.GET).mockImplementation(async (_path: string, options?: { params?: { path?: Record<string, string> } }) => ({
      data: success({
        river_segment_id: options?.params?.path?.segment_id ?? 'seg-009',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        segments: [
          {
            scenario: 'analysis_true_field',
            scenario_id: 'analysis_true_field',
            source: 'ERA5',
            segment_role: 'past_7_days',
            cycle_time: '2026-05-18T00:00:00Z',
            data: [{ valid_time: '2026-05-17T18:00:00Z', value: 8 }],
          },
          {
            scenario: 'forecast_gfs_deterministic',
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-18T00:00:00Z',
            data: [
              { valid_time: '2026-05-18T06:00:00Z', value: Number.POSITIVE_INFINITY },
              { valid_time: '2026-05-18T12:00:00Z', value: 10 },
            ],
          },
        ],
        frequency_thresholds: null,
      }),
      error: undefined,
    }) as never)
    window.history.pushState({}, '', '/forecast')

    render(<App />)

    await userEvent.setup().click(await screen.findByRole('button', { name: '河网地图' }))
    await waitFor(() =>
      expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
        'href',
        '/segments/seg-010?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T12%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-010',
      ),
    )
  })

  it('preserves source=best in forecast segment detail handoff until concrete forecast data resolves', async () => {
    let resolveForecast: (value: unknown) => void = () => undefined
    vi.mocked(client.GET).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveForecast = resolve
        }) as never,
    )
    window.history.pushState(
      {},
      '',
      '/forecast?source=best&segmentId=seg-009&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&cycle=2026-05-18T00:00:00Z',
    )

    render(<App />)

    expect(await screen.findByText('mock forecast panel')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
      'href',
      '/segments/seg-009?cycle=2026-05-18T00%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )
    expect(screen.getByRole('link', { name: '查看河段详情' }).getAttribute('href')).not.toContain('source=gfs')

    await act(async () => {
      resolveForecast({
        data: success({
          segment_id: 'seg-009',
          issue_time: '2026-05-18T00:00:00Z',
          unit: 'm3/s',
          series: [
            {
              scenario_id: 'forecast_ifs_deterministic',
              source: 'IFS',
              segment_role: 'future_7_days',
              cycle_time: '2026-05-18T00:00:00Z',
              points: [['2026-05-18T06:00:00Z', 10]],
            },
          ],
          frequency_thresholds: null,
        }),
        error: undefined,
      })
    })

    await waitFor(() =>
      expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
        'href',
        '/segments/seg-009?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
      ),
    )
  })

  it.each([
    ['ifs', 'IFS', ['IFS']],
    ['compare', 'GFS,IFS', ['GFS', 'IFS']],
  ] as const)('keeps %s forecast route context across retry and map selection', async (source, scenarios, selectedScenarios) => {
    const user = userEvent.setup()
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { path?: Record<string, unknown>; query?: Record<string, unknown> } }
      return {
        data: success({
          segment_id: options.params?.path?.segment_id ?? 'seg-009',
          issue_time: '2026-05-18T00:00:00Z',
          unit: 'm3/s',
          series: [],
          frequency_thresholds: null,
        }),
        error: undefined,
      } as never
    })
    window.history.pushState(
      {},
      '',
      `/forecast?segmentId=seg-009&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&source=${source}&cycle=2026-05-18T00:00:00Z`,
    )

    render(<App />)

    await waitFor(() =>
      expect(useForecastStore.getState()).toMatchObject({
        selectedScenarios,
        forecastData: { segmentId: 'seg-009' },
      }),
    )
    await user.click(screen.getByRole('button', { name: 'mock retry forecast' }))
    await user.click(screen.getByRole('button', { name: '河网地图' }))

    await waitFor(() => expect(useForecastStore.getState().selectedSegment?.segmentId).toBe('seg-010'))
    const forecastCalls = vi.mocked(client.GET).mock.calls.filter(([path]) =>
      String(path).endsWith('/forecast-series'),
    )
    expect(forecastCalls).toHaveLength(3)
    expect(
      forecastCalls.map(([, options]) => {
        const params = (options as { params?: { path?: Record<string, unknown>; query?: Record<string, unknown> } }).params
        return {
          segmentId: params?.path?.segment_id,
          riverNetworkVersionId: params?.query?.river_network_version_id,
          issueTime: params?.query?.issue_time,
          scenarios: params?.query?.scenarios,
        }
      }),
    ).toEqual([
      { segmentId: 'seg-009', riverNetworkVersionId: 'rn-v1', issueTime: '2026-05-18T00:00:00.000Z', scenarios },
      { segmentId: 'seg-009', riverNetworkVersionId: 'rn-v1', issueTime: '2026-05-18T00:00:00.000Z', scenarios },
      { segmentId: 'seg-010', riverNetworkVersionId: 'rn-v1', issueTime: '2026-05-18T00:00:00.000Z', scenarios },
    ])
  })

  it('re-requests forecast data when the same segment route source and cycle change during loading', async () => {
    let resolveFirstRequest: (() => void) | undefined
    const forecastCalls: Array<{
      segmentId: unknown
      riverNetworkVersionId: unknown
      issueTime: unknown
      scenarios: unknown
    }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { path?: Record<string, unknown>; query?: Record<string, unknown> } }
      const call = {
        segmentId: options.params?.path?.segment_id,
        riverNetworkVersionId: options.params?.query?.river_network_version_id,
        issueTime: options.params?.query?.issue_time,
        scenarios: options.params?.query?.scenarios,
      }
      forecastCalls.push(call)

      if (forecastCalls.length === 1) {
        await new Promise<void>((resolve) => {
          resolveFirstRequest = resolve
        })
        return {
          data: success({
            segment_id: 'seg-009',
            issue_time: '2026-05-18T00:00:00Z',
            unit: 'm3/s',
            series: [
              {
                scenario_id: 'forecast_ifs_deterministic',
                source: 'IFS',
                segment_role: 'future_7_days',
                cycle_time: '2026-05-18T00:00:00.000Z',
                points: [['2026-05-18T06:00:00Z', 1]],
              },
            ],
            frequency_thresholds: null,
          }),
          error: undefined,
        } as never
      }

      return {
        data: success({
          segment_id: 'seg-009',
          issue_time: '2026-05-19T00:00:00Z',
          unit: 'm3/s',
          series: [
            {
              scenario_id: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              cycle_time: '2026-05-19T00:00:00.000Z',
              points: [['2026-05-19T06:00:00Z', 2]],
            },
          ],
          frequency_thresholds: null,
        }),
        error: undefined,
      } as never
    })

    window.history.pushState(
      {},
      '',
      '/forecast?segmentId=seg-009&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&source=ifs&cycle=2026-05-18T00:00:00Z',
    )

    render(<App />)

    await waitFor(() => expect(forecastCalls).toHaveLength(1))
    window.history.pushState(
      {},
      '',
      '/forecast?segmentId=seg-009&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&source=gfs&cycle=2026-05-19T00:00:00Z',
    )
    window.dispatchEvent(new PopStateEvent('popstate'))

    await waitFor(() => expect(forecastCalls).toHaveLength(2))
    resolveFirstRequest?.()

    await waitFor(() =>
      expect(useForecastStore.getState()).toMatchObject({
        selectedScenarios: ['GFS'],
        activeRequestContext: { source: 'gfs', issueTime: '2026-05-19T00:00:00.000Z' },
        forecastData: {
          segmentId: 'seg-009',
          issueTime: '2026-05-19T00:00:00Z',
          sourceAttribution: 'GFS',
          cycleAttribution: 'GFS: 05-19 00Z',
        },
        loading: false,
      }),
    )
    expect(forecastCalls).toEqual([
      { segmentId: 'seg-009', riverNetworkVersionId: 'rn-v1', issueTime: '2026-05-18T00:00:00.000Z', scenarios: 'IFS' },
      { segmentId: 'seg-009', riverNetworkVersionId: 'rn-v1', issueTime: '2026-05-19T00:00:00.000Z', scenarios: 'GFS' },
    ])
  })

  it('routes basin deep links and restores normalized query state once', async () => {
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00.123456Z&validTime=2026-05-18T14:00:00.250001%2B08:00&warningLevel=orange&q=main',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getAllByText('basin-demo').length).toBeGreaterThan(0)
    expect(screen.getAllByText('seg-009').length).toBeGreaterThan(0)
    expect(screen.getAllByText('orange').length).toBeGreaterThan(0)
    await waitFor(() =>
      expect(window.location.search).toBe(
        '?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) =>
      String(url).endsWith(
        '/basins/basin-demo?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('restores full basin deep links with segment discovery filters and selected map hook', async () => {
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: {
        ...basinSnapshot(
          'basin-demo',
          m11Layers,
          'source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          'source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          456,
          true,
        ),
        requestScope: {
          kind: 'basin-detail',
          queryKey: 'source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          dataKey: 'source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          basinId: 'basin-demo',
          source: 'ifs',
          layer: 'flood-return-period',
          cycle: '2026-05-18T00:00:00.000Z',
          validTime: '2026-05-18T06:00:00.000Z',
          basemap: 'satellite',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'seg-009',
          warningLevel: null,
          q: null,
        },
      },
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=flood-return-period&basemap=satellite&warningLevel=orange&q=main&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() =>
      expect(loadBasinDetail).toHaveBeenCalledWith(
        'basin-demo',
        expect.objectContaining({
          source: 'ifs',
          cycle: '2026-05-18T00:00:00.000Z',
          validTime: '2026-05-18T06:00:00.000Z',
          layer: 'flood-return-period',
          basemap: 'vector',
          warningLevel: null,
          q: null,
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'seg-009',
        }),
      ),
    )
    expect(screen.getByLabelText('河段发现')).toHaveTextContent('Demo Basin')
    expect(screen.getByDisplayValue('main')).toBeInTheDocument()
    expect(screen.getByDisplayValue('橙色')).toBeInTheDocument()
    expect(screen.getByRole('listitem', { current: true })).toHaveTextContent('Main Stem 009')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'satellite')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', 'seg-009')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-segment-highlight-hook', 'selected-layer')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-map-state', 'selected-layer')
    expect(screen.getAllByTestId('mock-m11-map-layer').map((layer) => layer.getAttribute('data-layer-id'))).toContain('m11-selected-segment-line')
    const normalizedReplacements = replaceState.mock.calls.filter(([, , url]) => String(url).includes('/basins/basin-demo?'))
    expect(normalizedReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('renders basin shared controls and drives basin reload query state', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      basinDetail: {
        requestScope: {
          kind: 'basin-detail',
          queryKey: basinDefaultScopeKey,
          dataKey: basinDefaultScopeKey,
          basinId: 'basin-demo',
          source: 'gfs',
          layer: 'discharge',
          cycle: null,
          validTime: null,
          basemap: 'vector',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'seg-009',
          warningLevel: null,
          q: null,
        },
        detail: {
          basinId: 'basin-demo',
          displayName: 'Demo Basin',
          basinGroup: null,
          selectedBasinVersionId: 'bv-001',
          basinVersions: [],
          boundary: null,
          bbox: null,
          segmentCount: 1,
          warningDistribution: {
            normal: 0,
            elevated: 0,
            watch: 0,
            warning: 0,
            high_risk: 0,
            severe: 0,
            extreme: 0,
            unavailable: 0,
          },
          activeModelCount: 1,
          latestRun: m11LayerFreshness,
          sourceSelection: m11SourceSelection,
          unavailableReason: null,
          partialErrors: [],
        },
        segments: [],
        selectedSegment: {
          basinId: 'basin-demo',
          basinName: 'Demo Basin',
          basinVersionId: 'bv-001',
          riverSegmentId: 'seg-009',
          segmentId: 'seg-009',
          displayName: 'Segment 009',
          modelId: 'model-demo',
          riverNetworkVersionId: 'rn-v1',
          currentQ: 12,
          qUnit: 'm3/s',
          returnPeriod: 2,
          warningLevel: 'watch',
          qualityFlag: 'ok',
          qualityNote: null,
          sourceSelection: { ...m11SourceSelection, comparisonAvailable: true },
          trendPoints: [
            { validTime: '2026-05-18T00:00:00.000Z', value: 10, source: 'GFS', scenarioId: 'forecast_gfs_deterministic', role: 'analysis', isAnalysis: true },
            { validTime: '2026-05-18T06:00:00.000Z', value: 12, source: 'GFS', scenarioId: 'forecast_gfs_deterministic', role: 'future_7_days', isAnalysis: false },
          ],
          comparisonAvailable: true,
          lineageStatus: 'available',
          lineageUnavailableReason: null,
          handoffUrl: '/forecast',
          geometry: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
          freshness: m11LayerFreshness,
          unavailableReason: null,
        },
        layers: m11Layers,
      },
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/basins/basin-demo?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByText('数据源与情景')).toBeInTheDocument()
    expect(screen.getByText('水文图层')).toBeInTheDocument()
    expect(screen.getByText('径流量图例')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /洪水重现期/ }))
    expect(window.location.search).toContain('layer=flood-return-period')
    await waitFor(() =>
      expect(overviewAsync).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ layer: 'flood-return-period' })),
    )
  })

  it('filters basin segment rows and syncs row selection into the URL', async () => {
    const user = userEvent.setup()
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot(
        'basin-demo',
        m11Layers,
        'source=gfs&basinVersionId=bv-001',
        'source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
        null,
        true,
        [
          {
            riverSegmentId: 'seg-001',
            riverNetworkVersionId: 'rn-v1',
            segmentId: 'seg-001',
            displayName: 'North Branch 001',
            basinVersionId: 'bv-001',
            streamOrder: 1,
            lengthM: 800,
            currentQ: 88,
            qUnit: 'm3/s',
            returnPeriod: 2,
            warningLevel: 'watch',
            qualityFlag: 'ok',
            qualityNote: null,
            source: 'GFS',
            cycleTime: null,
            validTime: null,
            hasGeometry: true,
            geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
            unavailableReason: null,
          },
          {
            riverSegmentId: 'seg-009',
            riverNetworkVersionId: 'rn-v1',
            segmentId: 'seg-009',
            displayName: 'Main Stem 009',
            basinVersionId: 'bv-001',
            streamOrder: 3,
            lengthM: 1200,
            currentQ: 456,
            qUnit: 'm3/s',
            returnPeriod: 10,
            warningLevel: 'warning',
            qualityFlag: 'ok',
            qualityNote: null,
            source: 'GFS',
            cycleTime: null,
            validTime: null,
            hasGeometry: true,
            geometry: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            unavailableReason: null,
          },
        ],
      ),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState({}, '', '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByText('North Branch 001')).toBeInTheDocument()
    expect(screen.getByText('Main Stem 009')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('搜索河段名称或 ID'), { target: { value: 'main' } })
    expect(new URLSearchParams(window.location.search).get('q')).toBe('main')
    expect(screen.queryByText('North Branch 001')).not.toBeInTheDocument()
    expect(screen.getByText('Main Stem 009')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('预警筛选'), { target: { value: 'orange' } })
    expect(new URLSearchParams(window.location.search).get('warningLevel')).toBe('orange')
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByPlaceholderText('搜索河段名称或 ID'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('预警筛选'), { target: { value: '' } })
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-river-feature-count', '2')
    await user.click(screen.getByText('North Branch 001').closest('button') as HTMLButtonElement)
    expect(new URLSearchParams(window.location.search).get('segmentId')).toBe('seg-001')
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ segmentId: 'seg-001' })))

    fireEvent.keyDown(screen.getByTestId('mock-m11-maplibre-map'), { key: 'Enter' })
    expect(new URLSearchParams(window.location.search).get('segmentId')).toBe('seg-001')
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ segmentId: 'seg-001' })))
  })

  it('replaces stale URL river network identity from clicked basin map features', async () => {
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot(
        'basin-demo',
        [],
        'basinVersionId=bv-001&riverNetworkVersionId=rn-old&segmentId=seg-009',
        'basinVersionId=bv-001&riverNetworkVersionId=rn-old&segmentId=seg-009',
        null,
        true,
        [
          {
            riverSegmentId: 'seg-001',
            riverNetworkVersionId: 'rn-v1',
            segmentId: 'seg-001',
            displayName: 'North Branch 001',
            basinVersionId: 'bv-001',
            streamOrder: 1,
            lengthM: 800,
            currentQ: 88,
            qUnit: 'm3/s',
            returnPeriod: 2,
            warningLevel: 'watch',
            qualityFlag: 'ok',
            qualityNote: null,
            source: 'GFS',
            cycleTime: null,
            validTime: null,
            hasGeometry: true,
            geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
            unavailableReason: null,
          },
          {
            riverSegmentId: 'seg-009',
            riverNetworkVersionId: 'rn-old',
            segmentId: 'seg-009',
            displayName: 'Main Stem 009',
            basinVersionId: 'bv-001',
            streamOrder: 3,
            lengthM: 1200,
            currentQ: 456,
            qUnit: 'm3/s',
            returnPeriod: 10,
            warningLevel: 'warning',
            qualityFlag: 'ok',
            qualityNote: null,
            source: 'GFS',
            cycleTime: null,
            validTime: null,
            hasGeometry: true,
            geometry: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            unavailableReason: null,
          },
        ],
      ),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState({}, '', '/basins/basin-demo?basinVersionId=bv-001&riverNetworkVersionId=rn-old&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    fireEvent.keyDown(screen.getByTestId('mock-m11-maplibre-map'), { key: 'Enter' })
    const params = new URLSearchParams(window.location.search)
    expect(params.get('segmentId')).toBe('seg-001')
    expect(params.get('riverNetworkVersionId')).toBe('rn-v1')
    expect(params.get('basinVersionId')).toBe('bv-001')
    await waitFor(() =>
      expect(loadBasinDetail).toHaveBeenCalledWith(
        'basin-demo',
        expect.objectContaining({ segmentId: 'seg-001', riverNetworkVersionId: 'rn-v1', basinVersionId: 'bv-001' }),
      ),
    )
  })

  it.each([
    ['warning', '警戒'],
    ['major', '高风险'],
    ['severe', '严重'],
    ['extreme', '极端'],
    ['orange', '橙色'],
    ['red', '红色'],
  ] as const)('renders an honest basin warning filter option for %s route values', async (warningLevel, label) => {
    useOverviewDataStore.setState({
      basinDetail: {
        ...basinSnapshot(
          'basin-demo',
          m11Layers,
          `source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&warningLevel=${warningLevel}`,
        ),
        requestScope: {
          ...basinSnapshot('basin-demo', m11Layers).requestScope,
          queryKey: 'source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          dataKey: 'source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
          warningLevel: null,
        },
      },
      basinLoading: false,
    })
    window.history.pushState({}, '', `/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&warningLevel=${warningLevel}`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByLabelText('预警筛选')).toHaveValue(warningLevel)
    expect(screen.getByDisplayValue(label)).toBeInTheDocument()
    expect(screen.queryByDisplayValue('全部预警')).not.toBeInTheDocument()
  })

  it('updates basin basemap URL and map style without reloading basin data', async () => {
    const user = userEvent.setup()
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState({}, '', '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalled())
    const initialLoadCalls = loadBasinDetail.mock.calls.length

    await user.click(screen.getByRole('button', { name: '地形底图' }))

    const params = new URLSearchParams(window.location.search)
    expect(params.get('source')).toBe('gfs')
    expect(params.get('basemap')).toBe('terrain')
    expect(params.get('basinVersionId')).toBe('bv-001')
    expect(params.get('segmentId')).toBe('seg-009')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'terrain')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap-style', 'm11://basemaps/terrain')
    expect(loadBasinDetail).toHaveBeenCalledTimes(initialLoadCalls)
  })

  it('renders selected basin segment forecast handoff controls and disables unavailable comparison', async () => {
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey, 12, false),
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
      'href',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )
    expect(screen.getByRole('link', { name: '查看预报地图' })).toHaveAttribute(
      'href',
      '/forecast?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )
    expect(screen.getByTestId('m11-selected-segment-panel')).toHaveTextContent('river_segment_id')
    expect(screen.getByTestId('m11-selected-segment-panel')).toHaveTextContent('basin_version')
    expect(screen.getByTestId('m11-selected-segment-panel')).toHaveTextContent('model-demo')
    expect(screen.getByTestId('m11-selected-segment-panel')).toHaveTextContent('rn-v1')
    expect(screen.getByTestId('m11-selected-segment-panel')).toHaveTextContent('当前 Q')
    expect(screen.getByText('暂无水位差合同')).toBeInTheDocument()
    expect(screen.getByLabelText('河段趋势')).toHaveTextContent('当前值')
    expect(screen.getByLabelText('河段趋势')).toHaveTextContent('上升')
    expect(screen.getByLabelText('河段趋势')).toHaveTextContent('追溯数据可用')
    expect(screen.getByRole('button', { name: '对比预报' })).toBeDisabled()
    expect(screen.queryByRole('link', { name: '对比预报' })).not.toBeInTheDocument()
    expect(screen.getByText(/对比预报不可用/)).toBeInTheDocument()
    expect(screen.getByLabelText('地图上下文状态')).toHaveTextContent('地图已加载当前流域边界上下文')
    expect(screen.getByLabelText('地图上下文状态')).toHaveTextContent('城市与站点标签暂不可用')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '1')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo')
  })

  it('renders selected basin segment handoffs with the resolved concrete best source and cycle', async () => {
    const resolvedCycle = '2026-05-18T00:00:00.000Z'
    const bestBasinQueryKey = `cycle=${encodeURIComponent(resolvedCycle)}&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009`
    const bestBasinDataKey = `cycle=${encodeURIComponent(resolvedCycle)}&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009`
    const ifsSelectedSegment = {
      ...basinSnapshot('basin-demo', m11Layers).selectedSegment!,
      sourceSelection: {
        ...m11SourceSelection,
        requestedSource: 'best' as const,
        resolvedSource: 'IFS' as const,
        scenarioIds: ['forecast_ifs_deterministic'],
        cycleTime: resolvedCycle,
        comparisonAvailable: true,
      },
      trendPoints: [
        {
          validTime: '2026-05-18T06:00:00.000Z',
          value: 456,
          source: 'IFS' as const,
          scenarioId: 'forecast_ifs_deterministic',
          role: 'future_7_days',
          isAnalysis: false,
        },
      ],
      handoffUrl:
        '/forecast?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    }
    useOverviewDataStore.setState({
      basinDetail: {
        ...basinSnapshot('basin-demo', m11Layers, bestBasinQueryKey, bestBasinDataKey),
        selectedSegment: ifsSelectedSegment,
      },
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=best&cycle=2026-05-18T00:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    const href = screen.getByRole('link', { name: '查看河段详情' }).getAttribute('href')
    expect(href).toContain('/segments/seg-009?')
    expect(href).toContain('cycle=2026-05-18T00%3A00%3A00.000Z')
    expect(href).toContain('source=ifs')
    expect(screen.getByRole('button', { name: '对比预报' })).toBeEnabled()
  })

  it('renders invalid segment-id state for unsafe path segment ids without segment or forecast calls', async () => {
    window.history.pushState({}, '', '/segments/bad%2Fid?basinVersionId=bv-001&riverNetworkVersionId=rn-v1')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '无效 segmentId' })).toBeInTheDocument()
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('renders invalid segment-id state for overlong path segment ids without segment or forecast calls', async () => {
    window.history.pushState({}, '', `/segments/${'x'.repeat(97)}?basinVersionId=bv-001&riverNetworkVersionId=rn-v1`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '无效 segmentId' })).toBeInTheDocument()
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('rejects path and query segment identity mismatch before scoped fetches', async () => {
    window.history.pushState({}, '', '/segments/path-seg?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=query-seg')

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'segmentId 路径与查询不匹配' })).toBeInTheDocument()
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('enables selected basin segment comparison overlay when comparison data is available', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey, 12, true),
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '对比预报' }))
    expect(screen.getByRole('button', { name: '对比预报' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('region', { name: 'GFS IFS 对比数据' })).toHaveTextContent('GFS')
    expect(screen.getByRole('region', { name: 'GFS IFS 对比数据' })).toHaveTextContent('IFS')
    expect(screen.getByRole('region', { name: 'GFS IFS 对比数据' })).toHaveTextContent('12 m3/s')
    expect(screen.getByRole('region', { name: 'GFS IFS 对比数据' })).toHaveTextContent('19 m3/s')
  })

  it('restores /segments route identity and requests only the scoped forecast series', async () => {
    const calls: Array<{ path: string; segmentId?: string; query?: Record<string, unknown> }> = []
    vi.mocked(client.GET).mockImplementation((async (path: string, options?: { params?: { path?: Record<string, string>; query?: Record<string, unknown> } }) => {
      calls.push({ path, segmentId: options?.params?.path?.segment_id, query: options?.params?.query })
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return {
          data: success({
            river_segment_id: 'seg-009',
            river_network_version_id: 'rn-v1',
            length_m: 1200,
            geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            properties_json: {},
            created_at: '2026-05-18T00:00:00Z',
          }),
          error: undefined,
        }
      }
      if (String(path).endsWith('/forecast-series')) {
        return {
          data: success({
            river_segment_id: 'seg-009',
            issue_time: '2026-05-18T00:00:00Z',
            variable: 'q_down',
            unit: 'm3/s',
            segments: [
              {
                scenario: 'analysis_true_field',
                scenario_id: 'analysis_true_field',
                source: 'ERA5',
                segment_role: 'past_7_days',
                data: [{ valid_time: '2026-05-18T00:00:00Z', value: 10 }],
              },
              {
                scenario: 'forecast_gfs_deterministic',
                scenario_id: 'forecast_gfs_deterministic',
                source: 'GFS',
                cycle_time: '2026-05-18T00:00:00Z',
                segment_role: 'future_7_days',
                data: [{ valid_time: '2026-05-18T06:00:00Z', value: 3225 }],
              },
            ],
            frequency_thresholds: { Q2: 100, Q5: 200, Q10: 300, Q20: 400, Q50: 500, Q100: 600 },
          }),
          error: undefined,
        }
      }
      return { data: success({ target_type: 'river_segment', target_id: 'seg-009', nodes: [], edges: [] }), error: undefined }
    }) as never)
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('Q100'))
    const forecastCall = calls.find((call) => String(call.path).endsWith('/forecast-series'))
    expect(forecastCall).toMatchObject({
      segmentId: 'seg-009',
      query: expect.objectContaining({
        river_network_version_id: 'rn-v1',
        issue_time: '2026-05-18T00:00:00.000Z',
        scenarios: 'GFS',
        include_analysis: true,
      }),
    })
    expect(calls.map((call) => call.segmentId).filter(Boolean)).not.toContain('seg-001')
    expect(screen.getByLabelText('站点与强迫数据')).toHaveTextContent('现有 lineage API 不能在无 run_id 情况下安全查询')
    expect(screen.getByLabelText('位置缩略图')).toBeInTheDocument()
  })

  it('rejects sibling segment detail payload identity before rendering segment artifacts', async () => {
    vi.mocked(client.GET).mockImplementation((async (path: string) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return {
          data: success({
            river_segment_id: 'seg-010',
            segment_id: 'seg-010',
            river_network_version_id: 'rn-v1',
            length_m: 1200,
            geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            properties_json: {
              station_forcing: {
                station_id: 'SIBLING-STATION',
                series: {
                  variables: {
                    PRCP: [['2026-05-18T00:00:00Z', 1]],
                  },
                },
              },
            },
            created_at: '2026-05-18T00:00:00Z',
          }),
          error: undefined,
        }
      }
      return { data: success({}), error: undefined }
    }) as never)
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '未找到河段 seg-009' })).toBeInTheDocument()
    expect(screen.getByText(/河段详情响应与请求河段不匹配/)).toBeInTheDocument()
    expect(screen.queryByLabelText('位置缩略图')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('站点与强迫数据')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('天气驱动')).not.toBeInTheDocument()
    expect(screen.queryByText('SIBLING-STATION')).not.toBeInTheDocument()
    expect(vi.mocked(client.GET).mock.calls.some(([path]) => String(path).endsWith('/forecast-series'))).toBe(false)
  })

  it('does not render or correct from stale forecast store data while scoped segment data is pending', async () => {
    let resolveSegment: (value: unknown) => void = () => undefined
    const segmentPromise = new Promise((resolve) => {
      resolveSegment = resolve
    })
    vi.mocked(client.GET).mockImplementation((async (path: string) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return segmentPromise
      }
      return {
        data: success({
          segment_id: 'seg-009',
          issue_time: '2026-05-18T00:00:00Z',
          unit: 'm3/s',
          series: [],
          frequency_thresholds: null,
        }),
        error: undefined,
      }
    }) as never)
    useForecastStore.setState({
      selectedSegment: { segmentId: 'seg-old', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-v1' },
      forecastData: {
        segmentId: 'seg-old',
        basinVersionId: 'bv-001',
        riverNetworkVersionId: 'rn-v1',
        source: 'gfs',
        cycle: '2026-05-18T00:00:00.000Z',
        issueTime: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        sourceAttribution: 'GFS',
        cycleAttribution: 'GFS: 05-18 00Z',
        frequencyThresholds: { Q2: 1, Q5: 2, Q10: 3, Q20: 4, Q50: 5, Q100: 6 },
        series: [
          {
            scenario: 'forecast_gfs_deterministic',
            source: 'GFS',
            role: 'future_7_days',
            isAnalysis: false,
            label: 'GFS 预报',
            color: '#ef7d22',
            cycleTime: '2026-05-18T00:00:00Z',
            availableLeadHours: 168,
            points: [{ time: '2026-05-18T06:00:00Z', value: 999 }],
          },
        ],
      },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-17T00:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    expect(screen.getByText('当前预报响应与路由身份不匹配，已隐藏曲线。')).toBeInTheDocument()
    expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('不可用')
    expect(screen.getByLabelText('底部时间线')).toHaveTextContent('暂无有效流量时间线')
    expect(new URLSearchParams(window.location.search).get('validTime')).toBe('2026-05-17T00:00:00.000Z')

    await act(async () => {
      resolveSegment({
        data: success({
          river_segment_id: 'seg-009',
          river_network_version_id: 'rn-v1',
          length_m: 1200,
          geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
          properties_json: {},
          created_at: '2026-05-18T00:00:00Z',
        }),
        error: undefined,
      })
    })
  })

  it('keeps segment detail refresh disabled until scoped segment identity is selected', async () => {
    let resolveSegment: (value: unknown) => void = () => undefined
    const segmentPromise = new Promise((resolve) => {
      resolveSegment = resolve
    })
    vi.mocked(client.GET).mockImplementation((async (path: string) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return segmentPromise
      }
      return {
        data: success({
          segment_id: 'seg-old',
          issue_time: '2026-05-18T00:00:00Z',
          unit: 'm3/s',
          series: [],
          frequency_thresholds: null,
        }),
        error: undefined,
      }
    }) as never)
    useForecastStore.setState({
      selectedSegment: { segmentId: 'seg-old', basinVersionId: 'bv-old', riverNetworkVersionId: 'rn-old' },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    const refresh = screen.getByRole('button', { name: '刷新' })
    expect(refresh).toBeDisabled()
    await userEvent.setup().click(refresh)
    expect(vi.mocked(client.GET).mock.calls.some(([path]) => String(path).endsWith('/forecast-series'))).toBe(false)

    await act(async () => {
      resolveSegment({
        data: success({
          river_segment_id: 'seg-009',
          river_network_version_id: 'rn-v1',
          length_m: 1200,
          geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
          properties_json: {},
          created_at: '2026-05-18T00:00:00Z',
        }),
        error: undefined,
      })
    })

    await waitFor(() => expect(refresh).toBeEnabled())
    const forecastCall = vi
      .mocked(client.GET)
      .mock.calls.find(([path]) => String(path).endsWith('/forecast-series'))
    expect(forecastCall?.[1]).toMatchObject({
      params: {
        path: {
          basin_version_id: 'bv-001',
          segment_id: 'seg-009',
        },
        query: {
          river_network_version_id: 'rn-v1',
        },
      },
    })
  })

  it('refuses mismatched forecast response payloads without rendering sibling forecast panels', async () => {
    vi.mocked(client.GET).mockImplementation((async (path: string) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return {
          data: success({
            river_segment_id: 'seg-009',
            river_network_version_id: 'rn-v1',
            length_m: 1200,
            geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            properties_json: {},
            created_at: '2026-05-18T00:00:00Z',
          }),
          error: undefined,
        }
      }
      if (String(path).endsWith('/forecast-series')) {
        return {
          data: success({
            segment_id: 'seg-010',
            issue_time: '2026-05-18T00:00:00Z',
            unit: 'm3/s',
            series: [
              {
                scenario_id: 'forecast_gfs_deterministic',
                source: 'GFS',
                segment_role: 'future_7_days',
                cycle_time: '2026-05-18T00:00:00Z',
                points: [['2026-05-18T06:00:00Z', 9999]],
              },
            ],
            frequency_thresholds: { Q2: 1, Q5: 2, Q10: 3, Q20: 4, Q50: 5, Q100: 6 },
          }),
          error: undefined,
        }
      }
      return { data: success({}), error: undefined }
    }) as never)
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('预报曲线响应与请求河段不匹配'))
    expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('不可用')
    expect(screen.getByLabelText('底部时间线')).toHaveTextContent('暂无有效流量时间线')
  })

  it('renders degraded states for missing thresholds and non-finite forecast values', async () => {
    mockSegmentDetailRouteClientWithOptions({
      geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
      forecastSeries: [
        {
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          segment_role: 'future_7_days',
          cycle_time: '2026-05-18T00:00:00Z',
          points: [
            ['2026-05-18T00:00:00Z', Number.NaN],
            ['2026-05-18T06:00:00Z', Number.POSITIVE_INFINITY],
            ['2026-05-18T12:00:00Z', 'not-a-number'],
          ],
        },
      ],
      frequencyThresholds: null,
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('Q100'))
    expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('不可用')
    expect(screen.getByLabelText('频率曲线')).toHaveTextContent('频率阈值不可用')
    expect(screen.getByText('暂无预报数据')).toBeInTheDocument()
    expect(screen.getByLabelText('底部时间线')).toHaveTextContent('暂无有效流量时间线')
  })

  it('renders over-budget segment detail states without charting oversized forecast payloads', async () => {
    mockSegmentDetailRouteClientWithOptions({
      geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
      forecastSeries: [
        {
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          segment_role: 'future_7_days',
          cycle_time: '2026-05-18T00:00:00Z',
          points: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 2 }, (_, index) => [
            `2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`,
            index,
          ]),
        },
      ],
      frequencyThresholds: { Q2: 100, Q5: 200, Q10: 300, Q20: 400, Q50: 500, Q100: 600 },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(/预报序列超出客户端渲染预算/).length).toBeGreaterThanOrEqual(3))
    expect(screen.getByLabelText('洪水阈值')).toHaveTextContent('当前不计算超阈状态')
    expect(screen.getByLabelText('频率曲线')).toHaveTextContent('当前不绘制曲线')
    expect(screen.getByLabelText('底部时间线')).toHaveTextContent('当前不绘制')
    expect(screen.queryByTestId('mock-echarts-option')).not.toBeInTheDocument()
  })

  it('renders a frequency curve and current peak marker when thresholds and finite peak are available', async () => {
    mockSegmentDetailRouteClientWithOptions({
      geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
      forecastSeries: [
        {
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          segment_role: 'future_7_days',
          cycle_time: '2026-05-18T00:00:00Z',
          points: [
            ['2026-05-18T00:00:00Z', 110],
            ['2026-05-18T06:00:00Z', 450],
            ['2026-05-18T12:00:00Z', 320],
          ],
        },
      ],
      frequencyThresholds: { Q2: 100, Q5: 200, Q10: 300, Q20: 400, Q50: 500, Q100: 600 },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('frequency-curve')).toHaveTextContent('当前峰值'))
    expect(screen.getByLabelText('重现期频率曲线')).toBeInTheDocument()
    expect(screen.getByTestId('frequency-curve')).toHaveTextContent('T31.6')
    expect(screen.getByTestId('frequency-curve')).not.toHaveTextContent('T35.0')
    expect(screen.getByTestId('frequency-curve')).not.toHaveTextContent('阈值或有限峰值不足')
  })

  it('renders unavailable and over-budget geometry thumbnail states', async () => {
    mockSegmentDetailRouteClientWithOptions({
      geom: { type: 'LineString', coordinates: [] },
      forecastSeries: [],
      frequencyThresholds: null,
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    const { unmount } = render(<App />)
    expect(await screen.findByLabelText('位置缩略图')).toHaveTextContent('位置缩略图不可用')
    unmount()

    mockSegmentDetailRouteClientWithOptions({
      geom: {
        type: 'LineString',
        coordinates: Array.from({ length: 10_001 }, (_, index) => [101 + index / 100_000, 31]),
      },
      forecastSeries: [],
      frequencyThresholds: null,
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)
    expect(await screen.findByLabelText('位置缩略图')).toHaveTextContent('河段几何超出缩略图预算')
  })

  it('renders station forcing metadata and PRCP/TEMP rows from the segment detail contract', async () => {
    mockSegmentDetailRouteClient({
      station_forcing: {
        station_id: 'S001',
        source_id: 'CLDAS',
        station: {
          station_id: 'S001',
          basin_version_id: 'bv-001',
          station_name: 'Demo Station',
          geom: { type: 'Point', coordinates: [101.25, 31.5] },
          elevation_m: 345,
          station_role: 'nearest',
          active_flag: true,
          properties_json: {},
          created_at: '2026-05-18T00:00:00Z',
        },
        series: {
          target_id: 'S001',
          unit: 'mm/C',
          variables: {
            PRCP: [
              ['2026-05-18T00:00:00Z', 1.5],
              ['2026-05-18T06:00:00Z', 2.25],
            ],
            TEMP: [
              ['2026-05-18T00:00:00Z', 18],
              ['2026-05-18T06:00:00Z', 20],
            ],
          },
        },
      },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    const panel = await screen.findByLabelText('站点与强迫数据')
    await waitFor(() => expect(panel).toHaveTextContent('S001'))
    expect(panel).toHaveTextContent('Demo Station')
    expect(panel).toHaveTextContent('101.2500, 31.5000')
    expect(panel).toHaveTextContent('CLDAS')
    expect(within(panel).getByLabelText('PRCP chart')).toBeInTheDocument()
    expect(within(panel).getByLabelText('TEMP chart')).toBeInTheDocument()
    expect(within(panel).getAllByTestId('station-forcing-series-row')).toHaveLength(2)
    expect(panel).not.toHaveTextContent('未渲染合成站点')
    const weather = screen.getByLabelText('天气驱动')
    expect(within(weather).getByText('PRCP').closest('div')).toHaveTextContent('可用')
    expect(within(weather).getByText('TEMP').closest('div')).toHaveTextContent('可用')
    expect(within(weather).getByText('RH').closest('div')).toHaveTextContent('不可用')
    expect(within(weather).getByText('wind').closest('div')).toHaveTextContent('不可用')
    expect(within(weather).getByText('Press').closest('div')).toHaveTextContent('不可用')
  })

  it('renders over-budget station forcing as degraded state without chart rows', async () => {
    const oversized = Array.from({ length: 10_001 }, (_, index) => [`2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`, index])
    mockSegmentDetailRouteClient({
      station_forcing: {
        station_id: 'S001',
        series: {
          target_id: 'S001',
          unit: 'mm',
          variables: {
            PRCP: oversized,
          },
        },
      },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    const panel = await screen.findByLabelText('站点与强迫数据')
    await waitFor(() => expect(panel).toHaveTextContent('站点强迫序列超出客户端渲染预算'))
    expect(within(panel).queryAllByTestId('station-forcing-series-row')).toHaveLength(0)
    expect(within(panel).queryByLabelText('PRCP chart')).not.toBeInTheDocument()
  })

  it('renders restricted station forcing reason without synthetic station rows', async () => {
    mockSegmentDetailRouteClient({
      station_forcing: {
        metadata: {
          restricted_reason: 'CLDAS unavailable in this environment',
        },
      },
    })
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    const panel = await screen.findByLabelText('站点与强迫数据')
    await waitFor(() => expect(panel).toHaveTextContent('CLDAS unavailable in this environment'))
    expect(panel).toHaveTextContent('站点或强迫数据受限')
    expect(within(panel).queryByText('station')).not.toBeInTheDocument()
    expect(within(panel).queryAllByTestId('station-forcing-series-row')).toHaveLength(0)
    expect(within(panel).queryByLabelText('PRCP chart')).not.toBeInTheDocument()
    expect(within(panel).queryByLabelText('TEMP chart')).not.toBeInTheDocument()
  })

  it('keeps station forcing unavailable copy without synthetic rows when the contract is absent', async () => {
    mockSegmentDetailRouteClient()
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    const panel = await screen.findByLabelText('站点与强迫数据')
    await waitFor(() => expect(panel).toHaveTextContent('站点与强迫数据暂不可用'))
    expect(panel).toHaveTextContent('现有 lineage API 不能在无 run_id 情况下安全查询')
    expect(within(panel).queryByText('S001')).not.toBeInTheDocument()
    expect(within(panel).queryAllByTestId('station-forcing-series-row')).toHaveLength(0)
    expect(within(panel).queryByLabelText('PRCP chart')).not.toBeInTheDocument()
    expect(within(panel).queryByLabelText('TEMP chart')).not.toBeInTheDocument()
  })

  it('does not request forecast series when segment detail lacks river network identity', async () => {
    window.history.pushState({}, '', '/segments/seg-009?basinVersionId=bv-001')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '缺少 riverNetworkVersionId' })).toBeInTheDocument()
    expect(vi.mocked(client.GET).mock.calls.some(([path]) => String(path).endsWith('/forecast-series'))).toBe(false)
  })

  it('renders invalid stale segment state without falling back to a sibling segment forecast', async () => {
    vi.mocked(client.GET).mockImplementation((async (path: string, options?: { params?: { path?: Record<string, string> } }) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}' && options?.params?.path?.segment_id === 'missing') {
        return { data: undefined, error: { detail: 'not found' } }
      }
      return { data: success({}), error: undefined }
    }) as never)
    window.history.pushState({}, '', '/segments/missing?basinVersionId=bv-001&riverNetworkVersionId=rn-v1')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '未找到河段 missing' })).toBeInTheDocument()
    const forecastCalls = vi.mocked(client.GET).mock.calls.filter(([path]) => String(path).endsWith('/forecast-series'))
    expect(forecastCalls).toHaveLength(0)
    expect(vi.mocked(client.GET).mock.calls.map(([, options]) => options?.params?.path?.segment_id).filter(Boolean)).not.toContain('seg-001')
  })

  it('corrects stale segment validTime while preserving scoped identity', async () => {
    vi.mocked(client.GET).mockImplementation((async (path: string) => {
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}') {
        return {
          data: success({
            river_segment_id: 'seg-009',
            river_network_version_id: 'rn-v1',
            geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
            properties_json: {},
            created_at: '2026-05-18T00:00:00Z',
          }),
          error: undefined,
        }
      }
      if (String(path).endsWith('/forecast-series')) {
        return {
          data: success({
            segment_id: 'seg-009',
            issue_time: '2026-05-18T00:00:00Z',
            unit: 'm3/s',
            series: [
              {
                scenario_id: 'forecast_gfs_deterministic',
                source: 'GFS',
                segment_role: 'future_7_days',
                cycle_time: '2026-05-18T00:00:00Z',
                points: [
                  ['2026-05-18T00:00:00Z', 10],
                  ['2026-05-18T06:00:00Z', 20],
                ],
              },
            ],
            frequency_thresholds: null,
          }),
          error: undefined,
        }
      }
      return { data: success({ target_type: 'river_segment', target_id: 'seg-009', nodes: [], edges: [] }), error: undefined }
    }) as never)
    window.history.pushState(
      {},
      '',
      '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-17T00:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'seg-009' })).toBeInTheDocument()
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('validTime')).toBe('2026-05-18T00:00:00.000Z'))
    const params = new URLSearchParams(window.location.search)
    expect(params.get('source')).toBe('gfs')
    expect(params.get('cycle')).toBe('2026-05-18T00:00:00.000Z')
    expect(params.get('basinVersionId')).toBe('bv-001')
    expect(params.get('riverNetworkVersionId')).toBe('rn-v1')
    expect(params.get('segmentId')).toBe('seg-009')
  })

  it('does not correct basin valid time from a stale basin snapshot', async () => {
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-old', staleOverviewLayers, basinDefaultScopeKey),
      basinLoading: false,
      loadBasinDetail,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&validTime=2026-05-16T00:00:00Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ source: 'gfs' })))
    expect(window.location.search).toContain('validTime=2026-05-16T00%3A00%3A00.000Z')

    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey),
      basinLoading: false,
    })
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))
  })

  it('preserves basin URL valid-time changes that are valid for the active layer', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey),
      basinLoading: false,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&validTime=2026-05-18T06:00:00Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))

    await user.click(screen.getByRole('button', { name: '上一个有效时刻' }))

    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T00%3A00%3A00.000Z'))
    expect(window.location.search).not.toContain('validTime=2026-05-18T06%3A00%3A00.000Z')
  })

  it('hides stale basin detail while a valid-time reload is pending', async () => {
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey, 42),
      basinLoading: true,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&validTime=2026-05-18T00:00:00Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T00%3A00%3A00.000Z'))
    expect(window.location.search).not.toContain('validTime=2026-05-18T06%3A00%3A00.000Z')
    expect(screen.queryByText(/42 m3\/s/)).not.toBeInTheDocument()
    expect(screen.getByText('尚未选择河段')).toBeInTheDocument()
    expect(screen.getByText('流域数据加载中')).toBeInTheDocument()
  })

  it('threads basin detail bbox and map handlers through the route surface', async () => {
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey),
      basinLoading: false,
    })
    window.history.pushState(
      {},
      '',
      '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009&validTime=2026-05-18T06:00:00Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[101, 31], [104, 34]], { padding: 36, duration: 450 }]]))

    await userEvent.setup().hover(screen.getByTestId('mock-m11-maplibre-map'))
    await userEvent.setup().click(screen.getByTestId('mock-m11-maplibre-map'))
    expect(screen.getByRole('heading', { name: '流域分析' })).toBeInTheDocument()
  })

  it('uses fallback extent for missing bbox without blocking segment discovery', async () => {
    useOverviewDataStore.setState({
      basinDetail: {
        ...basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey),
        detail: {
          ...basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey).detail,
          bbox: null,
          boundary: null,
        },
      },
      basinLoading: false,
    })
    window.history.pushState({}, '', '/basins/basin-demo?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByLabelText('缺少流域 bbox')).toHaveTextContent('73,18,135,54')
    expect(screen.getByText('Main Stem 009')).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[73, 18], [135, 54]], { padding: 36, duration: 450 }]]))
  })

  it('shows no-segment empty state and disables segment filters', async () => {
    useOverviewDataStore.setState({
      basinDetail: {
        ...basinSnapshot('basin-demo', m11Layers, 'basinVersionId=bv-001', 'basinVersionId=bv-001', null, true, []),
        detail: {
          ...basinSnapshot('basin-demo', m11Layers).detail,
          segmentCount: 0,
          unavailableReason: 'Selected basin version has no river segment data.',
        },
        segments: [],
        selectedSegment: null,
      },
      basinLoading: false,
    })
    window.history.pushState({}, '', '/basins/basin-demo?basinVersionId=bv-001')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getByText('该流域暂无已发布的预报数据')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('搜索河段名称或 ID')).toBeDisabled()
    expect(screen.getByLabelText('预警筛选')).toBeDisabled()
  })

  it('renders a scoped not-found state for invalid basin ids with overview recovery', async () => {
    const invalidBasinSnapshot = basinSnapshot('not-a-real-basin', [], '', '', null)
    useOverviewDataStore.setState({
      basinDetail: {
        ...invalidBasinSnapshot,
        detail: {
          ...invalidBasinSnapshot.detail,
          basinId: '',
          displayName: '',
          selectedBasinVersionId: null,
          segmentCount: null,
          activeModelCount: 0,
          unavailableReason: 'Basin was not found.',
        },
        segments: [],
        selectedSegment: null,
        layers: [],
      },
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/basins/not-a-real-basin')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    const unavailableNotice = screen.getByLabelText('流域不可用')
    expect(unavailableNotice).toBeInTheDocument()
    expect(screen.getByText('未找到流域')).toBeInTheDocument()
    expect(screen.getByText('Basin was not found.')).toBeInTheDocument()
    expect(within(unavailableNotice).getByText('not-a-real-basin')).toBeInTheDocument()
    expect(within(unavailableNotice).getByRole('link', { name: '返回全国总览' })).toHaveAttribute('href', '/overview')
    expect(screen.queryByText('选中河段')).not.toBeInTheDocument()
    expect(screen.queryByText('预警状态')).not.toBeInTheDocument()
  })

  it('renders an unavailable state for invalid basin segment deep links', async () => {
    useOverviewDataStore.setState({
      basinDetail: {
        requestScope: {
          kind: 'basin-detail',
          queryKey: 'basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=missing-seg',
          dataKey: 'basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=missing-seg',
          basinId: 'basin-demo',
          source: 'best',
          layer: 'discharge',
          cycle: null,
          validTime: null,
          basemap: 'vector',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'missing-seg',
          warningLevel: null,
          q: null,
        },
        detail: {
          basinId: 'basin-demo',
          displayName: 'Demo Basin',
          basinGroup: null,
          selectedBasinVersionId: 'bv-001',
          basinVersions: [],
          boundary: null,
          bbox: null,
          segmentCount: 1,
          warningDistribution: {
            normal: 0,
            elevated: 0,
            watch: 0,
            warning: 0,
            high_risk: 0,
            severe: 0,
            extreme: 0,
            unavailable: 1,
          },
          activeModelCount: 0,
          latestRun: {
            updatedAt: null,
            cycleTime: null,
            validTime: null,
            runId: null,
            source: 'GFS',
            isStale: false,
            staleAfterHours: 6,
            unavailableReason: null,
          },
          sourceSelection: {
            requestedSource: 'gfs',
            resolvedSource: 'GFS',
            scenarioIds: ['forecast_gfs_deterministic'],
            cycleTime: null,
            validTime: null,
            comparisonAvailable: false,
            provenanceLabel: 'GFS / latest cycle / current valid time',
            unavailableReason: null,
          },
          unavailableReason: null,
          partialErrors: [],
        },
        segments: [],
        selectedSegment: null,
        layers: [],
      },
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/basins/basin-demo?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=missing-seg')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '流域分析' })).toBeInTheDocument()
    expect(screen.getAllByText('未找到河段 missing-seg').length).toBeGreaterThan(0)
    expect(screen.getByText('当前流域版本中没有匹配的河段数据。')).toBeInTheDocument()
    expect(screen.queryByText('已恢复 missing-seg')).not.toBeInTheDocument()
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', '')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-map-state', 'idle')
  })

  it('normalizes invalid overview query values without repeated URL updates', async () => {
    window.history.pushState(
      {},
      '',
      '/overview?source=unknown&basemap=bad&warningLevel=invalid&cycle=2026-02-30T00:00:00.123456Z&validTime=2026-05-18T00:00:00.123456',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '全国总览' })).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toBe(''))
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) => String(url).endsWith('/overview'))
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('routes /flood-alerts to the flood alert workflow content', async () => {
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T00:00:00.000Z', '2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
        totalSegments: 4,
        usableCurves: 3,
        unavailableCount: 1,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-1',
            segmentId: 'seg-1',
            segmentName: 'Flood Segment 1',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      fetchLatestFrequencyDoneRun,
    })
    window.history.pushState(
      {},
      '',
      '/flood-alerts?source=gfs&cycle=2026-05-12T00:00:00Z&validTime=2026-05-12T03:00:00Z&warningLevel=major',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '预警统计' })).toBeInTheDocument()
    expect(screen.getByLabelText('洪水预警地图')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '预报时刻' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '风险排名' })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /Flood Segment 1/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /洪水预警/ })).toHaveClass('border-accent')
    expect(fetchLatestFrequencyDoneRun).toHaveBeenCalledWith({
      source: 'gfs',
      cycleTime: '2026-05-12T00:00:00.000Z',
      validTime: '2026-05-12T03:00:00.000Z',
    })
    expect(floodAlertMapProps.at(-1)).toMatchObject({
      fallbackBbox: null,
      degradedFallback: false,
    })
    expect(useFloodAlertStore.getState().selectedAlertLevel).toBe('high_risk')
  })

  it('supplies bounded degraded flood-alert fallback bbox on the actual page path after selecting a focused segment', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchCalls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        fetchCalls.push(url)
        const parsed = new URL(url, 'http://localhost')
        if (parsed.pathname === '/api/v1/flood-alerts/timeline') {
          return floodApiResponse({
            run_id: 'run-flood-1',
            segment_id: parsed.searchParams.get('segment_id') ?? 'seg-focused',
            river_segment_id: parsed.searchParams.get('segment_id') ?? 'seg-focused',
            river_network_version_id: 'rivnet-v1',
            timesteps: [],
            timeline: [],
            peak: null,
            frequency_thresholds: null,
            quality_note: null,
          })
        }
        if (parsed.pathname !== '/api/v1/flood-alerts/ranking') throw new Error(`Unexpected flood request ${url}`)
        return floodApiResponse({
          items: [
            {
              rank: 1,
              river_segment_id: 'seg-focused',
              segment_id: 'seg-focused',
              segment_name: 'Focused Flood Segment',
              basin_version_id: 'basin-v1',
              river_network_version_id: 'rivnet-v1',
              q_value: 1234,
              q_unit: 'm3/s',
              return_period: 20,
              warning_level: 'warning',
              duration: '1h',
              valid_time: '2026-05-12T03:00:00Z',
              geom_centroid: { type: 'Point', coordinates: [101, 31] },
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        })
      }),
    )
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 1, color: '#f59e0b' }],
        totalSegments: 1,
        usableCurves: 1,
        unavailableCount: 0,
      },
      rankingData: null,
      fetchLatestFrequencyDoneRun,
      fetchRanking: useFloodAlertStore.getInitialState().fetchRanking,
    })
    window.history.pushState({}, '', '/flood-alerts')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: /Focused Flood Segment/ })).toBeInTheDocument()
    expect(floodAlertMapProps.at(-1)).toMatchObject({
      fallbackBbox: null,
      degradedFallback: false,
    })
    await user.click(screen.getByRole('row', { name: /Focused Flood Segment/ }))
    await waitFor(() =>
      expect(floodAlertMapProps.at(-1)).toMatchObject({
        fallbackBbox: { minLon: 100.75, minLat: 30.75, maxLon: 101.25, maxLat: 31.25 },
        degradedFallback: true,
      }),
    )
    expect(fetchCalls.filter((url) => new URL(url, 'http://localhost').pathname === '/api/v1/flood-alerts/ranking')).toEqual([
      '/api/v1/flood-alerts/ranking?run_id=run-flood-1&limit=20&offset=0',
    ])
  })

  it('keeps degraded flood-alert fallback blocked when API ranking centroid is missing or invalid', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        const parsed = new URL(url, 'http://localhost')
        if (parsed.pathname === '/api/v1/flood-alerts/timeline') {
          return floodApiResponse({
            run_id: 'run-flood-1',
            segment_id: parsed.searchParams.get('segment_id') ?? 'seg-no-centroid',
            river_segment_id: parsed.searchParams.get('segment_id') ?? 'seg-no-centroid',
            river_network_version_id: 'rivnet-v1',
            timesteps: [],
            timeline: [],
            peak: null,
            frequency_thresholds: null,
            quality_note: null,
          })
        }
        if (parsed.pathname !== '/api/v1/flood-alerts/ranking') throw new Error(`Unexpected flood request ${url}`)
        return floodApiResponse({
          items: [
            {
              rank: 1,
              river_segment_id: 'seg-no-centroid',
              segment_id: 'seg-no-centroid',
              segment_name: 'No Centroid Segment',
              basin_version_id: 'basin-v1',
              river_network_version_id: 'rivnet-v1',
              q_value: 1234,
              q_unit: 'm3/s',
              return_period: 20,
              warning_level: 'warning',
              duration: '1h',
              valid_time: '2026-05-12T03:00:00Z',
              geom_centroid: null,
            },
            {
              rank: 2,
              river_segment_id: 'seg-invalid-centroid',
              segment_id: 'seg-invalid-centroid',
              segment_name: 'Invalid Centroid Segment',
              basin_version_id: 'basin-v1',
              river_network_version_id: 'rivnet-v1',
              q_value: 1000,
              q_unit: 'm3/s',
              return_period: 10,
              warning_level: 'warning',
              duration: '1h',
              valid_time: '2026-05-12T03:00:00Z',
              geom_centroid: { type: 'Point', coordinates: [101, 'bad'] },
            },
          ],
          total: 2,
          limit: 20,
          offset: 0,
        })
      }),
    )
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
        totalSegments: 2,
        usableCurves: 2,
        unavailableCount: 0,
      },
      rankingData: null,
      fetchLatestFrequencyDoneRun,
      fetchRanking: useFloodAlertStore.getInitialState().fetchRanking,
    })
    window.history.pushState({}, '', '/flood-alerts')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: /No Centroid Segment/ })).toBeInTheDocument()
    await user.click(screen.getByRole('row', { name: /No Centroid Segment/ }))
    await waitFor(() =>
      expect(floodAlertMapProps.at(-1)).toMatchObject({
        fallbackBbox: null,
        degradedFallback: false,
      }),
    )
    await user.click(screen.getByRole('button', { name: '关闭详情' }))
    await user.click(screen.getByRole('row', { name: /Invalid Centroid Segment/ }))
    await waitFor(() =>
      expect(floodAlertMapProps.at(-1)).toMatchObject({
        fallbackBbox: null,
        degradedFallback: false,
      }),
    )
  })

  it('does not leak forecast route source and cycle into flood-alert segment detail forecast requests', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchTimeline = vi.fn().mockResolvedValue(undefined)
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        segment_id: 'seg-1',
        issue_time: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-12T00:00:00.000Z',
            points: [['2026-05-12T03:00:00Z', 1234]],
          },
        ],
        frequency_thresholds: null,
      }),
      error: undefined,
    } as never)
    useForecastStore.setState({
      activeRequestContext: { source: 'ifs', issueTime: '2026-05-18T00:00:00.000Z' },
      selectedScenarios: ['IFS'],
    })
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 1, color: '#f59e0b' }],
        totalSegments: 1,
        usableCurves: 1,
        unavailableCount: 0,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-1',
            segmentId: 'seg-1',
            segmentName: 'Flood Segment 1',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-flood-1',
        segmentId: 'seg-1',
        riverSegmentId: 'seg-1',
        riverNetworkVersionId: 'rivnet-v1',
        timesteps: [],
        peak: null,
        frequencyThresholds: null,
        qualityNote: null,
      },
      fetchLatestFrequencyDoneRun,
      fetchTimeline,
    })
    window.history.pushState(
      {},
      '',
      '/flood-alerts',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    await user.click(screen.getByRole('row', { name: /Flood Segment 1/ }))

    await waitFor(() =>
      expect(client.GET).toHaveBeenCalledWith(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
        expect.objectContaining({
          params: expect.objectContaining({
            path: { basin_version_id: 'basin-v1', segment_id: 'seg-1' },
            query: expect.objectContaining({
              river_network_version_id: 'rivnet-v1',
              issue_time: '2026-05-12T00:00:00.000Z',
              scenarios: 'GFS',
              include_analysis: true,
            }),
          }),
        }),
      ),
    )
    const query = vi.mocked(client.GET).mock.calls.find(([path]) => String(path).endsWith('/forecast-series'))?.[1]?.params
      ?.query as Record<string, unknown>
    expect(query.issue_time).not.toBe('2026-05-18T00:00:00.000Z')
    expect(query.issue_time).not.toBe('latest')
    await waitFor(() =>
      expect(useForecastStore.getState()).toMatchObject({
        forecastData: {
          segmentId: 'seg-1',
          issueTime: '2026-05-12T00:00:00Z',
          sourceAttribution: 'GFS',
        },
        loading: false,
      }),
    )
    expect(screen.getByTestId('mock-echarts-option')).toHaveTextContent('GFS 预报')
    expect(screen.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
      'href',
      '/segments/seg-1?source=gfs&cycle=2026-05-12T00%3A00%3A00.000Z&validTime=2026-05-12T03%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=basin-v1&riverNetworkVersionId=rivnet-v1&segmentId=seg-1',
    )
  })

  it('hides flood-alert forecast chart for preloaded sibling forecast data until scoped data arrives', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchTimeline = vi.fn().mockResolvedValue(undefined)
    let resolveForecast: (value: unknown) => void = () => undefined
    vi.mocked(client.GET).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveForecast = resolve
        }) as never,
    )
    useForecastStore.setState({
      selectedSegment: { segmentId: 'seg-sibling', basinVersionId: 'basin-v1', riverNetworkVersionId: 'rivnet-v1' },
      forecastData: {
        segmentId: 'seg-sibling',
        basinVersionId: 'basin-v1',
        riverNetworkVersionId: 'rivnet-v1',
        source: 'gfs',
        cycle: '2026-05-12T00:00:00.000Z',
        issueTime: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        sourceAttribution: 'GFS',
        cycleAttribution: 'GFS: 05-12 00Z',
        series: [
          {
            scenario: 'forecast_gfs_deterministic',
            source: 'GFS',
            role: 'future_7_days',
            isAnalysis: false,
            label: 'GFS 预报',
            color: '#ef7d22',
            cycleTime: '2026-05-12T00:00:00Z',
            availableLeadHours: 168,
            points: [{ time: '2026-05-12T03:00:00Z', value: 9999 }],
          },
        ],
      },
      loading: false,
    })
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 1, color: '#f59e0b' }],
        totalSegments: 1,
        usableCurves: 1,
        unavailableCount: 0,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-1',
            segmentId: 'seg-1',
            segmentName: 'Flood Segment 1',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-flood-1',
        segmentId: 'seg-1',
        riverSegmentId: 'seg-1',
        riverNetworkVersionId: 'rivnet-v1',
        timesteps: [],
        peak: null,
        frequencyThresholds: null,
        qualityNote: null,
      },
      fetchLatestFrequencyDoneRun,
      fetchTimeline,
    })
    window.history.pushState({}, '', '/flood-alerts')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    await user.click(screen.getByRole('row', { name: /Flood Segment 1/ }))

    expect(screen.queryByTestId('mock-echarts-option')).not.toBeInTheDocument()

    await act(async () => {
      resolveForecast({
        data: success({
          segment_id: 'seg-1',
          issue_time: '2026-05-12T00:00:00Z',
          unit: 'm3/s',
          series: [
            {
              scenario_id: 'forecast_gfs_deterministic',
              source: 'GFS',
              segment_role: 'future_7_days',
              cycle_time: '2026-05-12T00:00:00.000Z',
              points: [['2026-05-12T03:00:00Z', 1234]],
            },
          ],
          frequency_thresholds: null,
        }),
        error: undefined,
      })
    })

    await waitFor(() => expect(screen.getByTestId('mock-echarts-option')).toHaveTextContent('GFS 预报'))
  })

  it('renders flood-alert over-budget forecast and timeline degraded states without chart options', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchTimeline = vi.fn().mockResolvedValue(undefined)
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        segment_id: 'seg-1',
        issue_time: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-12T00:00:00.000Z',
            points: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 4 }, (_, index) => [
              `2026-05-12T${String(index % 24).padStart(2, '0')}:00:00Z`,
              index,
            ]),
          },
        ],
        frequency_thresholds: null,
      }),
      error: undefined,
    } as never)
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'warning', count: 1, color: '#f59e0b' }],
        totalSegments: 1,
        usableCurves: 1,
        unavailableCount: 0,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-1',
            segmentId: 'seg-1',
            segmentName: 'Flood Segment 1',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-flood-1',
        segmentId: 'seg-1',
        riverSegmentId: 'seg-1',
        riverNetworkVersionId: 'rivnet-v1',
        timesteps: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 3 }, (_, index) => ({
          validTime: `2026-05-12T${String(index % 24).padStart(2, '0')}:00:00Z`,
          returnPeriod: index % 100,
          warningLevel: 'warning' as const,
        })),
        peak: null,
        frequencyThresholds: null,
        qualityNote: null,
      },
      fetchLatestFrequencyDoneRun,
      fetchTimeline,
    })
    window.history.pushState({}, '', '/flood-alerts')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    await user.click(screen.getByRole('row', { name: /Flood Segment 1/ }))

    await waitFor(() => expect(screen.getAllByText(/预报序列超出客户端渲染预算/)).toHaveLength(2))
    expect(screen.queryByTestId('mock-echarts-option')).not.toBeInTheDocument()
    expect(useForecastStore.getState().forecastData?.series[0]?.points).toHaveLength(FORECAST_CHART_POINT_BUDGET)
  })

  it('does not reuse a previous selected segment network when the next segment needs run fallback', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchTimeline = vi.fn().mockResolvedValue(undefined)
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        segment_id: 'seg-1',
        issue_time: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }),
      error: undefined,
    } as never)
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-run',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-scoped',
            segmentId: 'seg-scoped',
            segmentName: 'Scoped Flood Segment',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-selected',
            qValue: 200,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
          {
            rank: 2,
            riverSegmentId: 'seg-unscoped',
            segmentId: 'seg-unscoped',
            segmentName: 'Run Fallback Segment',
            basinVersionId: 'basin-v1',
            qValue: 100,
            qUnit: 'm3/s',
            returnPeriod: 5,
            warningLevel: 'watch',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-flood-1',
        segmentId: 'seg-unscoped',
        riverSegmentId: 'seg-unscoped',
        riverNetworkVersionId: 'rivnet-run',
        timesteps: [],
        peak: null,
        frequencyThresholds: null,
        qualityNote: null,
      },
      fetchLatestFrequencyDoneRun,
      fetchTimeline,
    })
    window.history.pushState({}, '', '/flood-alerts')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    await user.click(screen.getByRole('row', { name: /Scoped Flood Segment/ }))
    expect(useForecastStore.getState().selectedSegment?.riverNetworkVersionId).toBe('rivnet-selected')
    await user.click(screen.getByRole('button', { name: '关闭详情' }))
    await user.click(screen.getByRole('row', { name: /Run Fallback Segment/ }))

    await waitFor(() =>
      expect(useForecastStore.getState().selectedSegment).toMatchObject({
        segmentId: 'seg-unscoped',
        riverNetworkVersionId: 'rivnet-run',
      }),
    )
    const forecastQueries = vi.mocked(client.GET).mock.calls
      .filter(([path]) => String(path).endsWith('/forecast-series'))
      .map(([, options]) => options?.params?.query as Record<string, unknown>)
    expect(forecastQueries.at(-1)).toMatchObject({ river_network_version_id: 'rivnet-run' })
    expect(forecastQueries.map((query) => query.river_network_version_id)).toEqual(['rivnet-selected', 'rivnet-run'])
  })

  it('binds flood-alert detail forecast requests to an explicitly routed older flood run cycle', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    const fetchTimeline = vi.fn().mockResolvedValue(undefined)
    vi.mocked(client.GET).mockResolvedValue({
      data: success({
        segment_id: 'seg-older',
        issue_time: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-12T00:00:00.000Z',
            points: [['2026-05-12T03:00:00Z', 456]],
          },
        ],
        frequency_thresholds: null,
      }),
      error: undefined,
    } as never)
    useFloodAlertStore.setState({
      selectedRunId: 'run-older-gfs',
      latestRun: {
        run_id: 'run-older-gfs',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      validTimes: ['2026-05-12T03:00:00.000Z'],
      summaryData: {
        runId: 'run-older-gfs',
        levels: [{ level: 'warning', count: 1, color: '#f59e0b' }],
        totalSegments: 1,
        usableCurves: 1,
        unavailableCount: 0,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'seg-older',
            segmentId: 'seg-older',
            segmentName: 'Older Flood Segment',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 456,
            qUnit: 'm3/s',
            returnPeriod: 10,
            warningLevel: 'watch',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-older-gfs',
        segmentId: 'seg-older',
        riverSegmentId: 'seg-older',
        riverNetworkVersionId: 'rivnet-v1',
        timesteps: [],
        peak: null,
        frequencyThresholds: null,
        qualityNote: null,
      },
      fetchLatestFrequencyDoneRun,
      fetchTimeline,
    })
    window.history.pushState(
      {},
      '',
      '/flood-alerts?source=gfs&cycle=2026-05-12T00:00:00Z&validTime=2026-05-12T03:00:00Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(fetchLatestFrequencyDoneRun).toHaveBeenCalledWith({
      source: 'gfs',
      cycleTime: '2026-05-12T00:00:00.000Z',
      validTime: '2026-05-12T03:00:00.000Z',
    })
    await user.click(screen.getByRole('row', { name: /Older Flood Segment/ }))

    await waitFor(() =>
      expect(client.GET).toHaveBeenCalledWith(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
        expect.objectContaining({
          params: expect.objectContaining({
            path: { basin_version_id: 'basin-v1', segment_id: 'seg-older' },
            query: expect.objectContaining({
              river_network_version_id: 'rivnet-v1',
              issue_time: '2026-05-12T00:00:00.000Z',
              scenarios: 'GFS',
            }),
          }),
        }),
      ),
    )
    await waitFor(() =>
      expect(useForecastStore.getState()).toMatchObject({
        forecastData: { segmentId: 'seg-older', sourceAttribution: 'GFS' },
        loading: false,
      }),
    )
  })

  it.each([
    ['orange', 'warning'],
    ['red', 'severe'],
    ['major', 'high_risk'],
  ] as const)('normalizes %s warning query before hydrating the flood alert store', async (warningLevel, expectedLevel) => {
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    useFloodAlertStore.setState({ fetchLatestFrequencyDoneRun })
    window.history.pushState({}, '', `/flood-alerts?warningLevel=${warningLevel}`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(useFloodAlertStore.getState().selectedAlertLevel).toBe(expectedLevel)
    expect(fetchLatestFrequencyDoneRun).toHaveBeenCalledWith({
      source: null,
      cycleTime: null,
      validTime: null,
    })
  })

  it('clears selected flood warning level when the route omits warningLevel', async () => {
    const user = userEvent.setup()
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    useFloodAlertStore.setState({
      selectedRunId: 'run-flood-1',
      latestRun: {
        run_id: 'run-flood-1',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
        source_id: 'gfs',
        cycle_time: '2026-05-12T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-12T00:00:00Z',
        end_time: '2026-05-12T03:00:00Z',
        created_at: '2026-05-12T00:00:00Z',
        updated_at: '2026-05-12T04:00:00Z',
      },
      summaryData: {
        runId: 'run-flood-1',
        levels: [{ level: 'high_risk', count: 1, color: '#f97316' }],
        totalSegments: 4,
        usableCurves: 3,
        unavailableCount: 1,
      },
      rankingData: { items: [], total: 0, limit: 20, offset: 0 },
      fetchLatestFrequencyDoneRun,
    })
    window.history.pushState({}, '', '/flood-alerts?warningLevel=major')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    await waitFor(() => expect(useFloodAlertStore.getState().selectedAlertLevel).toBe('high_risk'))
    await user.click(screen.getByRole('link', { name: /洪水预警/ }))
    await waitFor(() => expect(window.location.pathname).toBe('/flood-alerts'))
    await waitFor(() => expect(window.location.search).toBe(''))
    await waitFor(() => expect(useFloodAlertStore.getState().selectedAlertLevel).toBeNull())
  })

  it('hydrates flood-alert requests from a resolved concrete IFS summary handoff', async () => {
    const fetchLatestFrequencyDoneRun = vi.fn().mockResolvedValue(undefined)
    useFloodAlertStore.setState({ fetchLatestFrequencyDoneRun })
    window.history.pushState(
      {},
      '',
      '/flood-alerts?source=ifs&cycle=2026-05-18T00:00:00.000Z&validTime=2026-05-18T06:00:00.000Z',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '洪水预警' })).toBeInTheDocument()
    expect(fetchLatestFrequencyDoneRun).toHaveBeenCalledWith({
      source: 'ifs',
      cycleTime: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
    })
  })

  it('clears old flood-alert cards, ranking, ticker, timeline, and detail during a new IFS handoff failure', async () => {
    const oldRun = {
      run_id: 'run-old-gfs',
      run_type: 'forecast',
      scenario_id: 'forecast_gfs_deterministic',
      model_id: 'model-1',
      basin_version_id: 'basin-v1',
      river_network_version_id: 'rivnet-v1',
      source_id: 'gfs',
      cycle_time: '2026-05-12T00:00:00Z',
      status: 'frequency_done',
      start_time: '2026-05-12T00:00:00Z',
      end_time: '2026-05-12T03:00:00Z',
      created_at: '2026-05-12T00:00:00Z',
      updated_at: '2026-05-12T04:00:00Z',
    }
    const ifsRun = {
      ...oldRun,
      run_id: 'run-new-ifs',
      scenario_id: 'forecast_ifs_deterministic',
      source_id: 'ifs',
      cycle_time: '2026-05-13T00:00:00Z',
      start_time: '2026-05-13T00:00:00Z',
      end_time: '2026-05-13T06:00:00Z',
    }
    let resolveHandoff: (() => void) | null = null
    const handoffStarted = vi.fn()
    useFloodAlertStore.setState({
      selectedRunId: 'run-old-gfs',
      latestRun: oldRun,
      validTimes: ['2026-05-12T00:00:00.000Z', '2026-05-12T03:00:00.000Z'],
      selectedValidTime: '2026-05-12T03:00:00.000Z',
      summaryData: {
        runId: 'run-old-gfs',
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
        totalSegments: 4,
        usableCurves: 3,
        unavailableCount: 1,
      },
      rankingData: {
        items: [
          {
            rank: 1,
            riverSegmentId: 'old-seg',
            segmentId: 'old-seg',
            segmentName: 'Old Segment',
            basinVersionId: 'basin-v1',
            riverNetworkVersionId: 'rivnet-v1',
            qValue: 1234,
            qUnit: 'm3/s',
            returnPeriod: 20,
            warningLevel: 'warning',
            validTime: '2026-05-12T03:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      timelineData: {
        runId: 'run-old-gfs',
        segmentId: 'old-seg',
        riverSegmentId: 'old-seg',
        riverNetworkVersionId: 'rivnet-v1',
        timesteps: [{ validTime: '2026-05-12T03:00:00Z', returnPeriod: 20, warningLevel: 'warning' }],
      },
      fetchLatestFrequencyDoneRun: async () => {
        handoffStarted()
        await new Promise<void>((resolve) => {
          resolveHandoff = resolve
        })
        useFloodAlertStore.setState({
          selectedRunId: 'run-new-ifs',
          latestRun: ifsRun,
          validTimes: ['2026-05-13T00:00:00.000Z', '2026-05-13T06:00:00.000Z'],
          selectedValidTime: '2026-05-13T06:00:00.000Z',
          summaryData: null,
          rankingData: null,
          timelineData: null,
        })
      },
      fetchSummary: vi.fn().mockRejectedValue(new Error('summary failed')),
      fetchRanking: vi.fn().mockRejectedValue(new Error('ranking failed')),
    })
    window.history.pushState(
      {},
      '',
      '/flood-alerts?source=ifs&cycle=2026-05-13T00:00:00.000Z&validTime=2026-05-13T06:00:00.000Z',
    )

    const user = userEvent.setup()
    render(<App />)

    const oldRow = await screen.findByRole('row', { name: /Old Segment/ })
    await user.click(oldRow)
    expect(await screen.findByRole('heading', { name: 'Old Segment' })).toBeInTheDocument()
    await waitFor(() => expect(handoffStarted).toHaveBeenCalled())
    await act(async () => {
      resolveHandoff?.()
    })

    await waitFor(() => expect(useFloodAlertStore.getState().selectedRunId).toBe('run-new-ifs'))
    await waitFor(() => expect(screen.getByText(/run-new-ifs/)).toBeInTheDocument())
    expect(screen.queryByText('Old Segment')).not.toBeInTheDocument()
    expect(screen.queryByText('2 条')).not.toBeInTheDocument()
    expect(screen.getByText('等待预警数据')).toBeInTheDocument()
    expect(screen.getByText('暂无排名数据')).toBeInTheDocument()
    expect(screen.getByText('当前无超警河段')).toBeInTheDocument()
    expect(useFloodAlertStore.getState().timelineData).toBeNull()
  })

  it('routes /monitoring through allowed RBAC to the monitoring workflow content', async () => {
    useAuthStore.setState({ role: 'operator' })
    useMonitoringStore.setState({
      cycle: {
        source: 'GFS',
        cycle_time: '2026-05-09T00:00:00Z',
        current_state: 'partially_failed',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 3, failed: 1, running: 1, pending: 2 },
      },
      stages: [
        {
          stage: 'forcing',
          display_status: 'partially_failed',
          status: 'partially_failed',
          duration_seconds: 35,
          basin_progress: { completed: 3, total: 4, failed: 1 },
          basin_results: [],
        },
      ],
      jobs: [
        {
          job_id: 'job-failed',
          run_id: 'run-failed',
          cycle_id: 'cycle-1',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'model-b',
          status: 'failed',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: '2026-05-09T00:06:00Z',
          exit_code: 1,
          retry_count: 0,
          error_code: 'E_MODEL',
          error_message: 'model failed',
          log_uri: null,
          duration_seconds: 120,
        },
      ],
      jobTotal: 1,
      queue: { running: 2, pending: 4, idle: 6 },
    })
    window.history.pushState({}, '', '/monitoring?source=ifs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'IFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
      }),
    )
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '当前周期' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '七阶段流水线' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '作业列表' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '趋势' })).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /run-failed/ })).getByText('model-b')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveClass('border-accent')
  })

  it.each(['model_admin', 'sys_admin'] as const)('routes /system/model-assets for %s and restores URL-selected detail', async (role) => {
    useAuthStore.setState({ role })
    const model = modelAssetRouteFixture()
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /模型资产/ })).toHaveClass('border-accent')
    await waitFor(() => expect(screen.getAllByText('qhh-basin-v1').length).toBeGreaterThan(0))
    expect(screen.getAllByText('https://assets.example.test/pkg').length).toBeGreaterThan(0)
    expect(screen.getByText('s3://nhms/private/package')).toBeInTheDocument()
    expect(screen.getAllByTestId('model-asset-kpi-card').map((card) => within(card).getByRole('heading').textContent)).toEqual([
      '流域版本',
      '河网版本',
      '网格版本',
      '率定版本',
      'SHUD / 模型',
      '河段 / 面积',
    ])
    expect(screen.queryByText('/volume/data/nwm/Basins/qhh')).not.toBeInTheDocument()
    expect(screen.queryByText('C:\\nwm\\Basins\\qhh')).not.toBeInTheDocument()
    expect(screen.queryByText('file:///volume/data/nwm/Basins/qhh')).not.toBeInTheDocument()
    expect(screen.queryByText(/user:pass/)).not.toBeInTheDocument()
    expect(screen.queryByText(/token=abc/)).not.toBeInTheDocument()
    expect(screen.queryByText(/#frag/)).not.toBeInTheDocument()
  })

  it('does not render sibling model detail when the URL-selected model detail body has a mismatched id', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const modelA = modelAssetRouteFixture({
      model_id: 'model-a',
      model_name: 'Model A Sibling',
      basin_id: 'basin-a',
      basin_name: 'Basin A Sibling',
      basin_version_id: 'a-basin-v1',
      river_network_version_id: 'a-river-v1',
      mesh_version_id: 'a-mesh-v1',
      calibration_version_id: 'a-calib-v1',
      model_package_uri: 's3://nhms/models/a/package',
      package_checksum: 'a-pkg-sha',
      shud_input_name: 'a-input',
      resource_profile: {
        area_km2: 123.4,
        product_assets: [
          {
            id: 'a-product',
            label: 'A Product',
            checksum: 'a-product-sha',
            uri: 's3://nhms/models/a/product',
          },
        ],
      },
    })
    const modelB = modelAssetRouteFixture({
      model_id: 'model-b',
      model_name: 'Model B Selected',
      basin_id: 'basin-b',
      basin_name: 'Basin B Selected',
      basin_version_id: 'b-basin-v1',
      river_network_version_id: 'b-river-v1',
      mesh_version_id: 'b-mesh-v1',
      calibration_version_id: 'b-calib-v1',
      package_checksum: 'b-pkg-sha',
      shud_input_name: 'b-input',
    })
    const page: ModelAssetPage = { items: [modelB], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(modelA), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=model-b')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('模型资产详情与当前选择不匹配').length).toBeGreaterThan(0))
    expect(useModelAssetsStore.getState().selectedModel).toBeNull()
    expect(screen.getByText('Model B Selected')).toBeInTheDocument()
    expect(screen.queryByText('Model A Sibling')).not.toBeInTheDocument()
    expect(screen.queryByText('Basin A Sibling')).not.toBeInTheDocument()
    expect(screen.queryByText('a-basin-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('a-river-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('a-mesh-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('a-calib-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('a-pkg-sha')).not.toBeInTheDocument()
    expect(screen.queryByText('a-product')).not.toBeInTheDocument()
    expect(screen.queryByText('A Product')).not.toBeInTheDocument()
  })

  it('renders normalized restricted model source fields as restricted without leaking local details', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture({
      source_path: '/volume/data/nwm/Basins/qhh',
      resolved_source_path: 'C:\\nwm\\Basins\\qhh',
      source_uri: 'file:///volume/data/nwm/Basins/qhh?token=abc#frag',
      model_package_uri: '/volume/data/nwm/Basins/qhh/package.zip',
      manifest_uri: 'file:///volume/data/nwm/Basins/qhh/manifest.json',
      mesh_uri: 'C:\\nwm\\Basins\\qhh\\mesh.sp',
      resource_profile: {
        area_km2: 87.5,
        source_path: '/volume/data/nwm/Basins/qhh/profile-source',
        source_lineage: {
          source_uri: 'file:///volume/data/nwm/Basins/qhh?token=abc#frag',
          source_path: '/volume/data/nwm/Basins/qhh',
          local_path: '/volume/data/nwm/Basins/qhh/local',
          uris: ['file:///volume/data/nwm/Basins/qhh/lineage'],
        },
        product_assets: [
          {
            id: 'restricted-package',
            label: 'Restricted Package',
            checksum: 'pkg-sha',
            uri: '/volume/data/nwm/Basins/qhh/package.zip',
          },
          {
            id: 'restricted-path-product',
            label: 'Restricted Path Product',
            checksum: 'path-sha',
            path: 'file:///volume/data/nwm/Basins/qhh/product.bin',
          },
        ],
      },
    })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('受限来源').length).toBeGreaterThanOrEqual(3))
    expect(screen.queryByText('/volume/data/nwm/Basins/qhh')).not.toBeInTheDocument()
    expect(screen.queryByText('C:\\nwm\\Basins\\qhh')).not.toBeInTheDocument()
    expect(screen.queryByText(/profile-source/)).not.toBeInTheDocument()
    expect(screen.queryByText(/product\.bin/)).not.toBeInTheDocument()
    expect(screen.queryByText(/file:\/\//)).not.toBeInTheDocument()
    expect(screen.queryByText(/user:pass/)).not.toBeInTheDocument()
    expect(screen.queryByText(/token=abc/)).not.toBeInTheDocument()
    expect(screen.queryByText(/#frag/)).not.toBeInTheDocument()
  })

  it('keeps route source rows restricted when safe source fallbacks are also present', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture({
      source_uri: 's3://nhms/safe/top-level-source-uri',
      source_path: 's3://nhms/safe/top-level-source-path',
      resource_profile: {
        area_km2: 87.5,
        source_path: 's3://nhms/safe/profile-source-path',
        source_lineage: {
          source_uri: 'file:///volume/data/nwm/Basins/qhh/restricted-source-uri?token=abc#frag',
          source_path: 'file:///volume/data/nwm/Basins/qhh/restricted-source-path',
          local_path: '/volume/data/nwm/Basins/qhh/restricted-local-path',
          uris: ['s3://nhms/safe/lineage-uri'],
        },
        product_assets: [
          {
            id: 'safe-product',
            label: 'Safe Product',
            checksum: 'safe-sha',
            uri: 's3://nhms/safe/product',
          },
        ],
        geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
      },
    })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('受限来源').length).toBeGreaterThanOrEqual(3))
    const sourceUriRow = screen.getByText('Source URI').closest('div')
    const sourcePathRow = screen.getByText('Source Path').closest('div')
    expect(sourceUriRow).toHaveTextContent('受限来源')
    expect(sourceUriRow).not.toHaveTextContent('s3://nhms/safe/top-level-source-uri')
    expect(within(sourceUriRow as HTMLElement).getByText('受限来源')).toHaveAttribute('title', '受限来源')
    expect(sourcePathRow).toHaveTextContent('受限来源')
    expect(sourcePathRow).not.toHaveTextContent('s3://nhms/safe/top-level-source-path')
    expect(within(sourcePathRow as HTMLElement).getByText('受限来源')).toHaveAttribute('title', '受限来源')
    expect(screen.queryByText(/restricted-source-uri/)).not.toBeInTheDocument()
    expect(screen.queryByText(/restricted-source-path/)).not.toBeInTheDocument()
    expect(screen.queryByText(/restricted-local-path/)).not.toBeInTheDocument()
    expect(screen.queryByText(/token=abc/)).not.toBeInTheDocument()
    expect(screen.queryByText(/#frag/)).not.toBeInTheDocument()
  })

  it('renders degraded geometry state for normalized over-budget model asset geometry', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture({
      resource_profile: {
        area_km2: 87.5,
        source_lineage: {
          source_uri: 's3://nhms/safe/source',
        },
        geometry: {
          type: 'LineString',
          coordinates: Array.from({ length: 10_000 }, (_, index) => [100 + index / 10_000, 30]),
        },
      },
    })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    expect(await screen.findByText('空间几何超出预览预算')).toBeInTheDocument()
  })

  it('suppresses stale model detail when /system/model-assets list loading fails', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    useModelAssetsStore.setState(
      {
        ...useModelAssetsStore.getInitialState(),
        selectedModel: modelAssetRouteFixture(),
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') {
        return { data: undefined, error: { error: { message: unsafeModelAssetError } } } as never
      }
      if (path === '/api/v1/models/{model_id}') return { data: success(modelAssetRouteFixture()), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    await waitFor(() => expect(screen.getAllByText('模型资产列表加载失败').length).toBeGreaterThan(0))
    await waitFor(() => expect(useModelAssetsStore.getState().selectedModel).toBeNull())
    expectNoUnsafeModelAssetErrorTextInRoute()
    expect(screen.queryByText('QHH SHUD')).not.toBeInTheDocument()
    expect(screen.queryByText('qhh-basin-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('pkg-sha')).not.toBeInTheDocument()
  })

  it('renders only a safe generic detail error when /system/model-assets detail loading fails with sensitive source strings', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture()
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') {
        return { data: undefined, error: { error: { message: unsafeModelAssetError } } } as never
      }
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('模型资产详情加载失败').length).toBeGreaterThan(0))
    expectNoUnsafeModelAssetErrorTextInRoute()
    expect(screen.getByText('QHH SHUD')).toBeInTheDocument()
    expect(screen.queryByText('qhh-basin-v1')).not.toBeInTheDocument()
    expect(screen.queryByText('pkg-sha')).not.toBeInTheDocument()
  })

  it.each(['viewer', 'operator'] as const)(
    'denies /system/model-assets for %s, hides navigation, and does not fetch detail',
    async (role) => {
      useAuthStore.setState({ role })
      window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

      render(<App />)

      expect(await screen.findByText('权限不足')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /模型资产/ })).not.toBeInTheDocument()
      await waitFor(() => expect(vi.mocked(client.GET).mock.calls.length).toBe(0))
      expect(vi.mocked(client.GET).mock.calls.some(([path]) => path === '/api/v1/models/{model_id}')).toBe(false)
    },
  )
})
