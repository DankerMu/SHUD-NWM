import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { forwardRef, useImperativeHandle, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BrowserRouter, Route, Routes, useLocation } from 'react-router-dom'

import App, { LegacyRedirect } from '@/App'
import { client } from '@/api/client'
import { contextHandoff } from '@/pages/OverviewPage'
import { useAuthStore } from '@/stores/auth'
import { useForecastStore, type ForecastSegmentInfo } from '@/stores/forecast'
import { useModelAssetsStore, type ModelAsset, type ModelAssetPage } from '@/stores/modelAssets'
import { useMonitoringStore } from '@/stores/monitoring'
import { useOverviewDataStore } from '@/stores/overviewData'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import { serializeM11QueryState, type M11QueryState } from '@/lib/m11/queryState'

const m11FitBoundsCalls: Array<unknown[]> = []
const m11FlyToCalls: Array<unknown> = []

function geoJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

function success<T>(data: T) {
  return { status: 'success', data }
}

const computeRuntimeConfig = {
  service_role: 'compute_control',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
} as const

const displayRuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
} as const

const driftedDisplayRuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
} as const

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
    POST: vi.fn(),
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
              lngLat: { lng: 100.5, lat: 30.5 },
              features: [
                {
                  layer: { id: 'm11-basin-river-line' },
                  properties: {
                    river_segment_id: 'seg-001',
                    segment_id: 'seg-001',
                    basin_version_id: 'bv-001',
                    river_network_version_id: 'rn-v1',
                    segment_name: 'North Branch 001',
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
          onContextMenu={(event) => {
            // 模拟点击单个代站点要素（met-stations-point），feature 带 station_id + 点几何坐标。
            event.preventDefault()
            onClick?.({
              target: { getCanvas: () => ({ style: {} }) },
              lngLat: { lng: 104.1, lat: 31.2 },
              features: [
                {
                  layer: { id: 'met-stations-point' },
                  properties: { station_id: 'qhh_forc_001', station_name: 'QHH forcing 001' },
                  geometry: { type: 'Point', coordinates: [104.1, 31.2] },
                },
              ],
            })
          }}
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
  Popup: ({ children, longitude, latitude }: { children: ReactNode; longitude?: number; latitude?: number }) => (
    <div data-testid="mock-m11-map-popup" data-longitude={String(longitude ?? '')} data-latitude={String(latitude ?? '')}>
      {children}
    </div>
  ),
  Marker: ({ children }: { children?: ReactNode }) => <div data-testid="mock-m11-map-marker">{children}</div>,
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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, resolve, reject }
}

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
  valid_times: ['2026-05-18T06:00:00Z'],
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
    lifecycle_state: 'active',
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

function hydroMetLatestProduct(overrides: Record<string, unknown> = {}) {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'basins_qhh_vbasins',
    river_network_version_id: 'basins_qhh_rivnet_vbasins',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'qhh_gfs_2026052100_smoke',
    forcing_version_id: 'forc_gfs_2026052100_basins_qhh_shud',
    station_count: 386,
    expected_station_count: 386,
    segment_count: 1633,
    expected_segment_count: 1633,
    status: 'ready',
    run_status: 'frequency_done',
    valid_time_start: '2026-05-21T00:00:00Z',
    valid_time_end: '2026-05-28T00:00:00Z',
    river_valid_time_start: '2026-05-21T00:00:00Z',
    river_valid_time_end: '2026-05-28T00:00:00Z',
    forcing_valid_time_start: '2026-05-21T00:00:00Z',
    forcing_valid_time_end: '2026-05-28T00:00:00Z',
    available_horizon_hours: 168,
    expected_horizon_hours: 168,
    shorter_horizon: false,
    availability: {
      ready: true,
      unavailable_reasons: [],
      quality_flags: [],
      quality_notes: [],
    },
    quality: {
      station_sample_count: 120,
      river_sample_count: 360,
      required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'],
      station_variable_coverage: [
        {
          variable: 'PRCP',
          station_count: 386,
          sample_count: 3860,
          unit_count: 3860,
          quality_flag_count: 3860,
          missing_unit_samples: 0,
          missing_quality_flag_samples: 0,
          valid_time_start: '2026-05-21T00:00:00Z',
          valid_time_end: '2026-05-28T00:00:00Z',
        },
      ],
      candidate_limit: 20,
      search_limit: 20,
      context_limit: 20,
      query_indexes: [],
    },
    ...overrides,
  }
}

const hydroMetStationPage = {
  items: [
    {
      station_id: 'qhh_forc_001',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH forcing 001',
      geom: { type: 'Point', coordinates: [104.1, 31.2] },
      elevation_m: 320,
      station_role: 'forcing',
      active_flag: true,
      properties_json: null,
      created_at: '2026-05-21T00:00:00Z',
    },
  ],
  total_count: 386,
  limit: 500,
  offset: 0,
}

const stationSeriesUnits = {
  PRCP: 'mm',
  TEMP: 'degC',
  RH: '%',
  wind: 'm/s',
  Rn: 'W/m2',
  Press: 'Pa',
} as const

function hydroMetStationSeriesResponse(stationId = 'qhh_forc_001', overrides: Record<string, unknown> = {}) {
  const cycle = '2026-05-21T00:00:00Z'
  return {
    station_id: stationId,
    station: {
      station_id: stationId,
      basin_version_id: 'basins_qhh_vbasins',
      station_name: stationId === 'qhh_forc_002' ? 'North Ridge station' : 'QHH forcing 001',
      longitude: stationId === 'qhh_forc_002' ? 105.4 : 104.1,
      latitude: stationId === 'qhh_forc_002' ? 32.1 : 31.2,
      elevation_m: 320,
      station_role: 'forcing',
      active_flag: true,
      properties_json: null,
      created_at: cycle,
    },
    forcing_version_id: 'forc_gfs_2026052100_basins_qhh_shud',
    model_id: 'basins_qhh_shud',
    source_id: 'GFS',
    cycle_time: cycle,
    valid_time_start: cycle,
    valid_time_end: '2026-05-21T02:00:00Z',
    limit: 240,
    requested_from: null,
    requested_to: null,
    series: Object.entries(stationSeriesUnits).map(([variable, unit], index) => ({
      variable,
      unit,
      native_resolution: '1h',
      source_id: 'GFS',
      cycle_time: cycle,
      points: [
        { valid_time: '2026-05-21T00:00:00Z', value: index + 1, quality_flag: 'ok', source_id: 'GFS' },
        { valid_time: '2026-05-21T01:00:00Z', value: index + 2, quality_flag: 'ok', source_id: 'GFS' },
      ],
      truncated: false,
      metadata: {
        limit: 240,
        returned_points: 2,
        requested_from: null,
        requested_to: null,
        returned_from: '2026-05-21T00:00:00Z',
        returned_to: '2026-05-21T01:00:00Z',
        truncated: false,
      },
    })),
    ...overrides,
  }
}

const hydroMetRiverSegments = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'seg-001',
        basin_version_id: 'basins_qhh_vbasins',
        river_network_version_id: 'basins_qhh_rivnet_vbasins',
        name: 'QHH Segment 001',
        stream_order: 3,
        length_m: 1200,
      },
      geometry: { type: 'LineString', coordinates: [[104, 31], [105, 32]] },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-002',
        river_segment_id: 'seg-002',
        basin_version_id: 'basins_qhh_vbasins',
        river_network_version_id: 'basins_qhh_rivnet_vbasins',
        name: 'QHH Segment 002',
        stream_order: 4,
        length_m: 1800,
      },
      geometry: { type: 'LineString', coordinates: [[105, 32], [106, 33]] },
    },
  ],
  total: 1633,
  feature_total: 1633,
  limit: 250,
  offset: 0,
}

function hydroMetRiverForecastResponse(
  segmentId = 'seg-001',
  overrides: Record<string, unknown> = {},
  seriesOverrides: Record<string, unknown> = {},
) {
  const sourceId = String(seriesOverrides.source_id ?? overrides.source_id ?? 'GFS')
  const scenarioId = String(seriesOverrides.scenario_id ?? (sourceId === 'IFS' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic'))
  const cycleTime = String(seriesOverrides.cycle_time ?? overrides.issue_time ?? '2026-05-21T00:00:00Z')
  return {
    segment_id: segmentId,
    issue_time: cycleTime,
    unit: 'm3/s',
    frequency_thresholds: null,
    series: [
      {
        scenario_id: scenarioId,
        source_id: sourceId,
        cycle_time: cycleTime,
        available_lead_hours: sourceId === 'IFS' ? 144 : 168,
        segment_role: 'future_7_days',
        points: [
          [Date.parse(cycleTime), segmentId === 'seg-002' ? 21 : 11],
          [Date.parse('2026-05-21T06:00:00Z'), segmentId === 'seg-002' ? 24 : 13],
        ],
        ...seriesOverrides,
      },
    ],
    ...overrides,
  }
}

function applyMockStationServerFilter(
  response: unknown,
  query: { search?: string; variables?: string | string[] } | undefined,
) {
  if (!response || typeof response !== 'object' || !Array.isArray((response as { items?: unknown }).items)) {
    return response
  }
  const page = response as { items: Array<{ station_id: string; station_name?: string | null }>; total_count?: number }
  const search = query?.search?.trim().toLowerCase()
  if (!search) return page
  const items = page.items.filter((station) => {
    const label = `${station.station_id} ${station.station_name ?? ''}`.toLowerCase()
    return label.includes(search)
  })
  return { ...page, items, total_count: items.length }
}

function applyMockRiverServerFilter(
  response: unknown,
  query: { search?: string; stream_order_min?: number; stream_order_max?: number } | undefined,
) {
  if (!response || typeof response !== 'object' || !Array.isArray((response as { features?: unknown }).features)) {
    return response
  }
  const collection = response as {
    features: Array<{ properties: { river_segment_id?: string; segment_id?: string; name?: string; stream_order?: number } }>
    total?: number
    feature_total?: number
  }
  const search = query?.search?.trim().toLowerCase()
  const features = collection.features.filter((feature) => {
    const props = feature.properties
    if (search) {
      const label = `${props.river_segment_id ?? props.segment_id ?? ''} ${props.name ?? ''}`.toLowerCase()
      if (!label.includes(search)) return false
    }
    // Backend SQL semantics: NULL stream_order compares as false, so a row with NULL
    // stream_order is excluded whenever either bound is set. Mirror that here instead of
    // treating NULL as ±Infinity (which would falsely pass it through).
    const hasMin = Number.isFinite(query?.stream_order_min)
    const hasMax = Number.isFinite(query?.stream_order_max)
    if ((hasMin || hasMax) && !Number.isFinite(props.stream_order)) return false
    if (hasMin && (props.stream_order as number) < (query?.stream_order_min as number)) return false
    if (hasMax && (props.stream_order as number) > (query?.stream_order_max as number)) return false
    return true
  })
  return { ...collection, features, total: features.length, feature_total: features.length }
}

function mockHydroMetRouteClient(options: {
  product?: Record<string, unknown>
  stationResponse?: unknown | (() => unknown)
  stationError?: string
  riverResponse?: unknown
  riverError?: string
  stationSeriesResponse?: unknown | ((stationId: string) => unknown)
  stationSeriesData?: unknown
  stationSeriesError?: string
  stationSeriesThrow?: unknown
  stationSeriesDelayMs?: number
  riverForecastResponse?: unknown | ((segmentId: string) => unknown)
  riverForecastData?: unknown
  riverForecastError?: string
  riverForecastThrow?: unknown
} = {}) {
  vi.mocked(client.GET).mockImplementation(async (path: string, requestOptions?: unknown) => {
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series') {
      const segmentId = (requestOptions as { params?: { path?: { segment_id?: string } } })?.params?.path?.segment_id ?? 'seg-001'
      if (options.riverForecastThrow) throw options.riverForecastThrow
      if (options.riverForecastError) return { data: undefined, error: { error: { message: options.riverForecastError } } } as never
      if (options.riverForecastData) return { data: options.riverForecastData, error: undefined } as never
      const response = typeof options.riverForecastResponse === 'function'
        ? options.riverForecastResponse(segmentId)
        : options.riverForecastResponse ?? hydroMetRiverForecastResponse(segmentId)
      return { data: success(response), error: undefined } as never
    }
    if (path === '/api/v1/met/stations/{station_id}/series') {
      const stationId = (requestOptions as { params?: { path?: { station_id?: string } } })?.params?.path?.station_id ?? 'qhh_forc_001'
      if (options.stationSeriesDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.stationSeriesDelayMs))
      }
      if (options.stationSeriesThrow) throw options.stationSeriesThrow
      if (options.stationSeriesError) return { data: undefined, error: { error: { message: options.stationSeriesError } } } as never
      if (options.stationSeriesData) return { data: options.stationSeriesData, error: undefined } as never
      const response = typeof options.stationSeriesResponse === 'function'
        ? options.stationSeriesResponse(stationId)
        : options.stationSeriesResponse ?? hydroMetStationSeriesResponse(stationId)
      return { data: success(response), error: undefined } as never
    }
    if (path === '/api/v1/basins') {
      return {
        data: success([
          { basin_id: 'basins_qhh', basin_name: '青海湖', basin_group: null, description: null, created_at: '2026-01-01T00:00:00Z' },
        ]),
        error: undefined,
      } as never
    }
    if (path === '/api/v1/mvp/qhh/latest-product') {
      const source = (requestOptions as { params?: { query?: { source?: string } } })?.params?.query?.source ?? 'GFS'
      const sourceOverrides = source === 'IFS' ? { source_id: 'IFS', run_id: 'qhh_ifs_2026052100_smoke', forcing_version_id: 'forc_ifs_2026052100_basins_qhh_shud' } : {}
      return { data: success(hydroMetLatestProduct({ ...sourceOverrides, ...options.product })), error: undefined } as never
    }
    if (path === '/api/v1/met/stations') {
      if (options.stationError) return { data: undefined, error: { error: { message: options.stationError } } } as never
      const response = typeof options.stationResponse === 'function' ? options.stationResponse() : options.stationResponse ?? hydroMetStationPage
      const query = (requestOptions as { params?: { query?: { search?: string; variables?: string | string[] } } })?.params?.query
      // Simulate the backend search/variable filtering so server-driven UI behaviour is exercised.
      const filtered = applyMockStationServerFilter(response, query)
      return { data: success(filtered), error: undefined } as never
    }
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
      if (options.riverError) return { data: undefined, error: { error: { message: options.riverError } } } as never
      const query = (requestOptions as { params?: { query?: { search?: string; stream_order_min?: number; stream_order_max?: number } } })?.params?.query
      const filtered = applyMockRiverServerFilter(options.riverResponse ?? hydroMetRiverSegments, query)
      return { data: success(filtered), error: undefined } as never
    }
    return { data: success({}), error: undefined } as never
  })
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


beforeEach(() => {
  vi.clearAllMocks()
  m11FitBoundsCalls.length = 0
  m11FlyToCalls.length = 0
  overviewAsync.mockResolvedValue(undefined)
  useAuthStore.setState({ role: 'viewer' })
  useForecastStore.setState(
    {
      ...useForecastStore.getInitialState(),
    },
    true,
  )
  useModelAssetsStore.setState(useModelAssetsStore.getInitialState(), true)
  vi.mocked(client.GET).mockImplementation(async (path: string) => {
    if (path === '/api/v1/runtime/config') {
      return { data: success(computeRuntimeConfig), error: undefined } as never
    }
    return { data: success([]), error: undefined } as never
  })
  useMonitoringStore.setState({
    source: 'GFS',
    cycleTime: '2026-05-09T00:00:00Z',
    cycle: null,
    cycleContext: null,
    stages: [],
    jobs: [],
    jobsContext: null,
    jobTotal: 0,
    queue: null,
    queueError: null,
    operationalError: null,
    jobsError: null,
    jobFilters: { page: 1, pageSize: 12, sortBy: 'submitted_at', sortOrder: 'desc' },
    isPolling: false,
    isJobsLoading: false,
    error: null,
    strictIdentity: null,
    runtimeConfig: computeRuntimeConfig,
    runtimeConfigError: null,
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
  it('routes / to the national overview fullscreen map (M26)', async () => {
    window.history.pushState({}, '', '/')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByLabelText('全国总览地图')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-layer-switcher')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
  })

  it('routes /overview to the fullscreen map with normalized query state (M26)', async () => {
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&basemap=terrain')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 浮层图例随 active layer（重现期）渲染；不再有侧栏/timeline
    expect(screen.getByText('重现期图例')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-timeline')).not.toBeInTheDocument()
  })

  it('reloads overview data when the floating layer switcher changes the layer (M26)', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, ''),
    })
    window.history.pushState({}, '', '/?source=gfs')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 默认流量图层选中，浮层图例显示径流量
    expect(screen.getByRole('button', { name: /流量/, pressed: true })).toBeInTheDocument()
    expect(screen.getByText('径流量图例')).toBeInTheDocument()

    // 切到气象代站 → 写 layer=met-stations 并以新 layer 重取
    await user.click(screen.getByRole('button', { name: /气象代站/ }))
    expect(window.location.search).toContain('layer=met-stations')
    await waitFor(() => expect(overviewAsync).toHaveBeenCalledWith(expect.objectContaining({ layer: 'met-stations' })))
  })

  it('honestly degrades the met-raster layer without drawing a fake grid (M26)', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, ''),
    })
    window.history.pushState({}, '', '/?source=gfs')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /气象栅格/ }))
    expect(screen.getByTestId('m11-met-raster-notice')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend-empty')).toHaveTextContent('未注册')
    // 诚实降级：地图不渲染任何气象栅格叠加层，而是给出"未注册"提示，绝不画假格点
    expect(screen.getByTestId('m11-map-unavailable')).toHaveTextContent('地图不会渲染该叠加层')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays', 'met-raster')
  })

  it('preserves river network version in overview load state and renders the matching snapshot', async () => {
    const loadOverview = vi.fn().mockImplementation(async (query: M11QueryState) => {
      const snapshot = overviewSnapshotForQuery(query)
      useOverviewDataStore.setState({ overview: snapshot, loading: false })
      return snapshot
    })
    useOverviewDataStore.setState({ loadOverview, loading: false })
    window.history.pushState(
      {},
      '',
      '/overview?source=gfs&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    )

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
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
    expect(useOverviewDataStore.getState().overview?.requestScope).toMatchObject({
      dataKey:
        'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
      riverNetworkVersionId: 'rn-v1',
    })
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

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(loadOverview).toHaveBeenCalledWith(expect.objectContaining({ layer: 'flood-return-period' })))
    expect(window.location.search).toContain('validTime=2026-05-16T00%3A00%3A00.000Z')

    useOverviewDataStore.setState({
      overview: overviewSnapshot(m11Layers, overviewFloodScopeKey),
      loading: false,
    })
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))
  })

  it('threads overview basin bbox and registers the flood overlay through the route surface', async () => {
    const tileFetch = vi.fn().mockImplementation(async () => geoJsonResponse({ type: 'FeatureCollection', features: [] }))
    vi.stubGlobal('fetch', tileFetch)
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(m11Layers, overviewFloodScopeKey, overviewFloodValid06ScopeKey),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&layer=flood-return-period&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[100, 30], [105, 35]], { padding: 36, duration: 450 }]]))
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(tileFetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })

  it('renders only visible basin boundaries on the overview map (M26)', async () => {
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(m11Layers, overviewDefaultScopeKey, overviewValid06ScopeKey),
      loading: false,
    })
    window.history.pushState({}, '', '/overview?source=gfs&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '1')
  })

  it('drills into a basin in-place when its boundary is clicked on the map (M26)', async () => {
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
      '/?source=gfs&validTime=2026-05-18T06:00:00Z&basemap=satellite&basinVersionId=bv-sibling&segmentId=seg-sibling',
    )

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()

    // 点 m11-basin-fill（地图 dblClick mock 派发 basin-fill）→ 直接写 basinId 进入详情，pathname 仍为 /
    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))
    await waitFor(() =>
      expect(window.location.search).toBe(
        '?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basemap=satellite&basinVersionId=bv-001&basinId=basin-demo',
      ),
    )
    expect(window.location.search).not.toContain('segmentId=seg-sibling')
    expect(window.location.pathname).toBe('/')
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

  it('emits concrete IFS cycle handoffs from a best overview latest run (contextHandoff)', async () => {
    const ifsSelection = {
      ...m11SourceSelection,
      requestedSource: 'best' as const,
      resolvedSource: 'IFS' as const,
      scenarioIds: ['forecast_ifs_deterministic'],
      cycleTime: '2026-05-19T00:00:00.000Z',
      validTime: null,
      provenanceLabel: 'Best Available (IFS) / cycle 2026-05-19T00:00:00.000Z / current valid time',
    }
    const scope = { ...overviewSnapshot(m11Layers).requestScope, source: 'best' as const, layer: 'discharge' as const, basemap: 'vector' as const }
    expect(contextHandoff('/monitoring', scope, ifsSelection)).toMatchObject({
      href: '/monitoring?source=ifs&cycle=2026-05-19T00%3A00%3A00.000Z',
    })
    expect(contextHandoff('/flood-alerts', scope, ifsSelection)).toMatchObject({
      href: '/flood-alerts?source=ifs&cycle=2026-05-19T00%3A00%3A00.000Z',
    })
  })

  it('omits concrete destination source context for compare handoffs (contextHandoff)', async () => {
    const compareSelection = {
      ...m11SourceSelection,
      requestedSource: 'compare' as const,
      resolvedSource: 'GFS+IFS' as const,
      scenarioIds: ['forecast_gfs_deterministic', 'forecast_ifs_deterministic'],
      provenanceLabel: 'GFS+IFS / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
    }
    const scope = {
      ...overviewSnapshot(m11Layers).requestScope,
      source: 'compare' as const,
      layer: 'discharge' as const,
      basemap: 'vector' as const,
      cycle: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
    }
    expect(contextHandoff('/monitoring', scope, compareSelection)).toMatchObject({ href: '/monitoring' })
    expect(contextHandoff('/flood-alerts', scope, compareSelection)).toMatchObject({ href: '/flood-alerts' })
    expect(contextHandoff('/monitoring', scope, compareSelection).description).toContain('对比暂不支持跨页保真')
  })

  it('does not drill into any basin from an empty overview (no fabricated basin IDs)', async () => {
    const user = userEvent.setup()
    window.history.pushState({}, '', '/')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 无流域数据时点地图（mock dblClick 派发 basin-fill）不写 basinId、不进入详情
    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))
    expect(window.location.search).not.toContain('basinId=')
    expect(window.location.pathname).toBe('/')
  })

  it('drills into a basin by writing basinId into the single-page query without leaving / (M26)', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(m11Layers, overviewFloodScopeKey, overviewFloodValid06ScopeKey, 'basin-demo'),
      loading: false,
    })
    window.history.pushState({}, '', '/?source=gfs&layer=flood-return-period&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))

    await waitFor(() =>
      expect(window.location.search).toBe(
        '?source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=bv-001&basinId=basin-demo',
      ),
    )
    expect(window.location.pathname).toBe('/')
    expect(new URLSearchParams(window.location.search).getAll('basinId')).toEqual(['basin-demo'])
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

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-overview-empty')).toHaveTextContent('暂无可用流域数据')
    expect(screen.queryByText('长江流域')).not.toBeInTheDocument()
    expect(screen.queryByText('黄河流域')).not.toBeInTheDocument()
    expect(screen.queryByText('珠江流域')).not.toBeInTheDocument()
    expect(screen.queryByText('松辽流域')).not.toBeInTheDocument()
  })

  it('renders an honest empty overview without fabricating summary metrics (M26)', async () => {
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

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 单页全屏不再渲染运行态摘要四卡；空态走 honest 浮层，不伪造任何指标
    expect(screen.getByTestId('m11-overview-empty')).toBeInTheDocument()
    expect(screen.queryByText('今日完成周期')).not.toBeInTheDocument()
    expect(screen.queryByText('当前运行中')).not.toBeInTheDocument()
    expect(screen.queryByText('超警河段')).not.toBeInTheDocument()
  })

  it('routes basin deep links and restores normalized query state once', async () => {
    window.history.pushState(
      {},
      '',
      '/?cycle=2026-05-18T00%3A00%3A00.123456Z&validTime=2026-05-18T14%3A00%3A00.250001%2B08%3A00&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009&warningLevel=orange&q=main',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() =>
      expect(window.location.search).toBe(
        '?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) =>
      String(url).endsWith(
        '/?cycle=2026-05-18T00%3A00%3A00.123Z&validTime=2026-05-18T06%3A00%3A00.250Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009&warningLevel=orange&q=main',
      ),
    )
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
  })

  it('loads basin detail and highlights the selected segment on the fullscreen map (M26)', async () => {
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
      '/?source=ifs&cycle=2026-05-18T00%3A00%3A00Z&validTime=2026-05-18T06%3A00%3A00Z&layer=flood-return-period&basemap=satellite&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009&warningLevel=orange&q=main',
    )

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() =>
      expect(loadBasinDetail).toHaveBeenCalledWith(
        'basin-demo',
        expect.objectContaining({
          source: 'ifs',
          cycle: '2026-05-18T00:00:00.000Z',
          validTime: '2026-05-18T06:00:00.000Z',
          layer: 'flood-return-period',
          basinVersionId: 'bv-001',
          riverNetworkVersionId: 'rn-v1',
          segmentId: 'seg-009',
        }),
      ),
    )
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', 'seg-009')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-map-state', 'selected-layer')
    expect(screen.getAllByTestId('mock-m11-map-layer').map((layer) => layer.getAttribute('data-layer-id'))).toContain('m11-selected-segment-line')
    // 单页全屏不再渲染段表/筛选侧栏
    expect(screen.queryByLabelText('河段发现')).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText('搜索河段名称或 ID')).not.toBeInTheDocument()
  })

  it('reloads basin detail when the floating layer switcher changes the layer (M26)', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey),
      basinLoading: false,
      basinError: null,
    })
    window.history.pushState({}, '', '/?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 浮层切换器在详情模式同样可用；不再有水文图层侧栏
    expect(screen.queryByText('数据源与情景')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /气象代站/ }))
    expect(window.location.search).toContain('layer=met-stations')
    await waitFor(() =>
      expect(overviewAsync).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ layer: 'met-stations' })),
    )
  })

  it('writes the clicked river segment id into the URL from a map feature (M26)', async () => {
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
        ],
      ),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState({}, '', '/?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-river-feature-count', '1')

    fireEvent.keyDown(screen.getByTestId('mock-m11-maplibre-map'), { key: 'Enter' })
    expect(new URLSearchParams(window.location.search).get('segmentId')).toBe('seg-001')
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ segmentId: 'seg-001' })))
  })

  it('opens the river forecast popup when a river segment map feature is clicked (M26-4)', async () => {
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, 'source=gfs&basinVersionId=bv-001', 'source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009'),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    mockHydroMetRouteClient()
    window.history.pushState({}, '', '/?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009')

    render(<App />)
    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()

    fireEvent.keyDown(screen.getByTestId('mock-m11-maplibre-map'), { key: 'Enter' })

    expect(await screen.findByTestId('m11-river-popup')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-popup')).not.toBeInTheDocument()
    // 弹窗内 source/起报选择条存在
    expect(screen.getByTestId('m11-popup-source-controls')).toBeInTheDocument()
  })

  it('opens the station forcing popup when a met-station map feature is clicked (M26-4)', async () => {
    const loadBasinDetail = vi.fn().mockResolvedValue(undefined)
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, 'source=gfs&layer=met-stations&basinVersionId=bv-001', 'source=gfs&layer=met-stations&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009'),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    mockHydroMetRouteClient()
    window.history.pushState({}, '', '/?source=gfs&layer=met-stations&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009')

    render(<App />)
    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByTestId('mock-m11-maplibre-map').getAttribute('data-interactive-layer-ids') ?? '').toContain('met-stations-point'),
    )

    fireEvent.contextMenu(screen.getByTestId('mock-m11-maplibre-map'))

    expect(await screen.findByTestId('m11-station-popup')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-river-popup')).not.toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-selector')).toBeInTheDocument()
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
        ],
      ),
      basinLoading: false,
      basinError: null,
      loadBasinDetail,
    })
    window.history.pushState({}, '', '/?basinVersionId=bv-001&riverNetworkVersionId=rn-old&basinId=basin-demo&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
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
      '/?source=gfs&validTime=2026-05-16T00%3A00%3A00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009',
    )

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(loadBasinDetail).toHaveBeenCalledWith('basin-demo', expect.objectContaining({ source: 'gfs' })))
    expect(window.location.search).toContain('validTime=2026-05-16T00%3A00%3A00.000Z')

    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey),
      basinLoading: false,
    })
    await waitFor(() => expect(window.location.search).toContain('validTime=2026-05-18T06%3A00%3A00.000Z'))
  })

  it('threads basin detail bbox into a camera fit through the route surface (M26)', async () => {
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basin-demo', m11Layers, basinDefaultScopeKey, basinValid06ScopeKey),
      basinLoading: false,
    })
    window.history.pushState(
      {},
      '',
      '/?source=gfs&validTime=2026-05-18T06%3A00%3A00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009',
    )

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[101, 31], [104, 34]], { padding: 36, duration: 450 }]]))
  })

  it('uses fallback extent for a basin with missing bbox (M26)', async () => {
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
    window.history.pushState({}, '', '/?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(m11FitBoundsCalls).toEqual([[[[73, 18], [135, 54]], { padding: 36, duration: 450 }]]))
  })

  it('renders a scoped not-found floating notice for invalid basin ids with overview recovery (M26)', async () => {
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
    window.history.pushState({}, '', '/?basinId=not-a-real-basin')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    const notFound = screen.getByTestId('m11-basin-not-found')
    expect(notFound).toHaveTextContent('Basin was not found.')
    expect(notFound).toHaveTextContent('not-a-real-basin')
    // 不再渲染选中河段/预警状态侧栏
    expect(screen.queryByText('选中河段')).not.toBeInTheDocument()
    expect(screen.queryByText('预警状态')).not.toBeInTheDocument()

    // 返回总览：就地浮层按钮清空 basinId，pathname 仍为 /
    await userEvent.setup().click(screen.getByTestId('m11-back-to-overview'))
    expect(await screen.findByLabelText('全国总览地图')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
    expect(window.location.search).not.toContain('basinId=')
  })

  it('keeps an invalid selected segment off the map without fabricating geometry (M26)', async () => {
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
    window.history.pushState({}, '', '/?basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=missing-seg')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', '')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-map-state', 'idle')
  })

  it('selects a basin in-place by clicking its boundary, fits bbox, keeps pathname / (M26)', async () => {
    const user = userEvent.setup()
    const baseSnapshot = basinSnapshot(
      'basin-demo',
      m11Layers,
      'source=gfs&basinVersionId=bv-001',
      'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z&basinVersionId=bv-001',
    )
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasins(m11Layers, 'source=gfs', 'source=gfs&validTime=2026-05-18T06%3A00%3A00.000Z'),
      basinDetail: {
        ...baseSnapshot,
        selectedSegment: baseSnapshot.selectedSegment
          ? { ...baseSnapshot.selectedSegment, riverNetworkVersionId: null }
          : null,
      },
      loading: false,
      basinLoading: false,
    })
    window.history.pushState({}, '', '/?source=gfs&validTime=2026-05-18T06:00:00Z')

    render(<App />)

    expect(await screen.findByLabelText('全国总览地图')).toBeInTheDocument()
    // 点 m11-basin-fill（dblClick mock）→ 直接进入详情，pathname 仍为 /
    await user.dblClick(screen.getByTestId('mock-m11-maplibre-map'))

    await waitFor(() => expect(new URLSearchParams(window.location.search).get('basinId')).toBe('basin-demo'))
    expect(await screen.findByLabelText('流域钻取地图')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
    await waitFor(() => expect(m11FitBoundsCalls).toContainEqual([[[101, 31], [104, 34]], { padding: 36, duration: 450 }]))
  })

  it('returns to the national overview in-place when basinId is cleared, keeping pathname / (M26)', async () => {
    const user = userEvent.setup()
    useOverviewDataStore.setState({
      overview: overviewSnapshotWithBasin(m11Layers, 'source=gfs', 'source=gfs', 'basin-demo'),
      basinDetail: basinSnapshot('basin-demo', m11Layers, 'source=gfs', 'source=gfs'),
      loading: false,
      basinLoading: false,
    })
    window.history.pushState({}, '', '/?source=gfs&basinId=basin-demo')

    render(<App />)

    expect(await screen.findByLabelText('流域钻取地图')).toBeInTheDocument()
    await user.click(screen.getByTestId('m11-back-to-overview'))

    expect(await screen.findByLabelText('全国总览地图')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
    expect(window.location.search).not.toContain('basinId=')
    expect(window.location.search).not.toContain('segmentId=')
  })

  it('opens a shared detail deep link directly and restores the prior basinId on back navigation (M26)', async () => {
    useOverviewDataStore.setState({
      basinDetail: basinSnapshot('basins_qhh', m11Layers, 'source=gfs', 'source=gfs'),
      basinLoading: false,
    })
    window.history.pushState({}, '', '/?source=gfs&basinId=basins_qhh')

    render(<App />)

    expect(await screen.findByLabelText('流域钻取地图')).toBeInTheDocument()
    expect(new URLSearchParams(window.location.search).get('basinId')).toBe('basins_qhh')
    expect(window.location.pathname).toBe('/')

    act(() => {
      window.history.replaceState({}, '', '/?source=gfs&basinId=basin-demo')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })

    await waitFor(() => expect(new URLSearchParams(window.location.search).get('basinId')).toBe('basin-demo'))
    expect(window.location.pathname).toBe('/')
    expect(screen.getByLabelText('流域钻取地图')).toBeInTheDocument()
  })

  it('normalizes invalid overview query values without repeated URL updates', async () => {
    window.history.pushState(
      {},
      '',
      '/overview?source=unknown&basemap=bad&warningLevel=invalid&cycle=2026-02-30T00:00:00.123456Z&validTime=2026-05-18T00:00:00.123456',
    )
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<App />)

    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toBe(''))
    // 旧路由 redirect 落到 `/` 后，OverviewPage 把非法 query 归一为干净的 `/`（无 search）。
    // 不变量：归一化只发生一次（无重复 URL 更新），落点为不带 query 的 `/`。
    const normalizedRouteReplacements = replaceState.mock.calls.filter(([, , url]) => String(url).endsWith('/'))
    expect(normalizedRouteReplacements).toHaveLength(1)
    replaceState.mockRestore()
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
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
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
  })

  it('keeps legacy /monitoring operational errors out of StageList unavailable text', async () => {
    useAuthStore.setState({ role: 'operator' })
    useMonitoringStore.setState({
      operationalError: 'monitoring fixture API error',
      error: 'monitoring fixture API error',
      stages: [],
      jobs: [],
      jobTotal: 0,
      fetchAll: vi.fn().mockResolvedValue(undefined),
      fetchJobs: vi.fn().mockResolvedValue(undefined),
    })
    window.history.pushState({}, '', '/monitoring')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    expect(screen.getAllByText(/monitoring fixture API error/)).toHaveLength(1)
    expect(screen.queryByText(/当前 source\/cycle 的流水线阶段不可用：monitoring fixture API error/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '七阶段流水线' })).toBeInTheDocument()
  })

  it.each(['operator', 'model_admin', 'sys_admin'] as const)(
    'routes /ops for %s with log/retry controls but without cancel',
    async (role) => {
      useAuthStore.setState({ role })
      useMonitoringStore.setState({
        source: 'IFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        cycle: {
          source: 'IFS',
          cycle_time: '2026-05-18T00:00:00.000Z',
          current_state: 'running',
          started_at: '2026-05-09T00:00:30Z',
          updated_at: '2026-05-09T00:08:00Z',
          job_counts: { succeeded: 3, failed: 0, running: 1, pending: 2 },
        },
        cycleContext: { source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        stages: [],
        jobs: [
          {
            job_id: 'job-ops',
            run_id: 'run-ops',
            cycle_id: 'cycle-ops',
            job_type: 'forecast',
            slurm_job_id: '2001',
            model_id: 'model-ops',
            status: 'failed',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:03:00Z',
            started_at: '2026-05-09T00:04:00Z',
            finished_at: '2026-05-09T00:06:00Z',
            exit_code: 1,
            retry_count: 2,
            error_code: 'E_MODEL',
            error_message: 'model failed',
            log_uri: 's3://logs/job-ops.log',
            duration_seconds: 120,
          },
          {
            job_id: 'job-ops-running',
            run_id: 'run-ops-running',
            cycle_id: 'cycle-ops',
            job_type: 'forecast',
            slurm_job_id: '2002',
            model_id: 'model-ops-running',
            status: 'running',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:07:00Z',
            started_at: '2026-05-09T00:08:00Z',
            finished_at: null,
            exit_code: null,
            retry_count: 0,
            error_code: null,
            error_message: null,
            log_uri: 's3://logs/job-ops-running.log',
            duration_seconds: null,
          },
        ],
        jobsContext: { source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobTotal: 2,
        queue: { running: 2, pending: 4, idle: 6 },
      })
      window.history.pushState({}, '', '/ops?source=ifs&cycle=2026-05-18T00:00:00Z')

      render(<App />)

      expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
      await waitFor(() =>
        expect(useMonitoringStore.getState()).toMatchObject({
          source: 'IFS',
          cycleTime: '2026-05-18T00:00:00.000Z',
        }),
      )
      expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
      expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
      expect(screen.getByTestId('ops-manual-recovery-guidance')).not.toHaveTextContent(/display_readonly|27/)
      const failedRow = screen.getByRole('row', { name: /job-ops.*run-ops.*forecast.*model-ops.*failed.*2001.*2m.*2.*available/ })
      expect(failedRow).toBeInTheDocument()
      expect(within(failedRow).getByRole('button', { name: /查看日志/ })).toBeVisible()
      expect(within(failedRow).getByRole('button', { name: /重试/ })).toBeVisible()
      expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
    },
  )

  it.each(['operator', 'sys_admin'] as const)(
    'renders /ops as display_readonly diagnostics without retry, cancel, or queue control for %s',
    async (role) => {
      useAuthStore.setState({ role })
      const user = userEvent.setup()
      useMonitoringStore.setState({
        runtimeConfig: displayRuntimeConfig,
        runtimeConfigError: null,
        source: 'GFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        cycle: {
          source: 'GFS',
          cycle_time: '2026-05-18T00:00:00.000Z',
          current_state: 'failed',
          started_at: '2026-05-18T00:00:30Z',
          updated_at: '2026-05-18T00:08:00Z',
          job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
        },
        cycleContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        stages: [
          {
            stage: 'forecast',
            display_status: 'failed',
            status: 'failed',
            duration_seconds: 120,
            basin_progress: { completed: 0, total: 1, failed: 1 },
            basin_results_limit: 50,
            basin_results_total: 0,
            basin_results_returned: 0,
            basin_results_truncated: false,
            basin_results: [],
          },
        ],
        jobs: [
          {
            job_id: 'job-display',
            run_id: 'run-display',
            cycle_id: 'cycle-display',
            run_type: 'forecast',
            scenario: 'forecast_gfs_deterministic',
            job_type: 'forecast',
            slurm_job_id: '2001',
            model_id: 'model-display',
            status: 'failed',
            stage: 'forecast',
            submitted_at: '2026-05-18T00:03:00Z',
            started_at: '2026-05-18T00:04:00Z',
            finished_at: '2026-05-18T00:06:00Z',
            exit_code: 1,
            retry_count: 0,
            error_code: 'E_MODEL',
            error_message: 'display failure',
            log_uri: 'published://logs/job-display.log',
            duration_seconds: 120,
          },
        ],
        jobsContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobTotal: 1,
      })
      vi.mocked(client.GET).mockImplementation(async (path: string) => {
        if (path === '/api/v1/jobs/{job_id}/logs') {
          return {
            data: success({ job_id: 'job-display', log_uri: 'published://logs/job-display.log', content: 'published display log' }),
            error: undefined,
          } as never
        }
        throw new Error(`Unexpected GET ${path}`)
      })
      window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

      render(<App />)

      expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
      expect(screen.getAllByText(/display_readonly/).some((node) =>
        node.textContent?.includes('重试、取消和 Slurm 控制请求已禁用'),
      )).toBe(true)
      expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('27 display_readonly')
      expect(screen.getByRole('heading', { name: '队列深度不可用' })).toBeInTheDocument()
      expect(screen.getByText(/display_readonly 只读展示节点不读取 Slurm 队列深度/)).toBeInTheDocument()
      expect(screen.getByRole('row', { name: /run-display.*model-display.*failed/ })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: /查看日志/ }))
      expect(await screen.findByText('published display log')).toBeInTheDocument()

      expect(vi.mocked(client.POST)).not.toHaveBeenCalled()
      expect(vi.mocked(client.GET).mock.calls.some(([path]) => String(path).startsWith('/api/v1/slurm/'))).toBe(false)
    },
  )

  it('fetches drifted display_readonly runtime config from backend and fails closed on /ops controls', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const paths: string[] = []
    useMonitoringStore.setState(
      {
        ...useMonitoringStore.getInitialState(),
        source: 'GFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      paths.push(path)
      const options = args[1] as { params?: { query?: Record<string, unknown>; path?: Record<string, unknown> } } | undefined
      const query = options?.params?.query
      if (path === '/api/v1/runtime/config') return { data: success(driftedDisplayRuntimeConfig), error: undefined } as never
      if (path === '/api/v1/pipeline/status') {
        return {
          data: success({
            cycle_id: 'cycle-display',
            source: query?.source,
            cycle_time: query?.cycle_time,
            current_state: 'failed',
            started_at: '2026-05-18T00:00:30Z',
            updated_at: '2026-05-18T00:08:00Z',
            job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/pipeline/stages') {
        return {
          data: success([
            {
              stage: 'forecast',
              display_status: 'failed',
              status: 'failed',
              duration_seconds: 120,
              basin_progress: { completed: 0, total: 1, failed: 1 },
              basin_results_limit: 50,
              basin_results_total: 0,
              basin_results_returned: 0,
              basin_results_truncated: false,
              basin_results: [],
            },
          ]),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/jobs') {
        return {
          data: success({
            items: [
              {
                job_id: 'job-display-endpoint',
                run_id: 'run-display-endpoint',
                cycle_id: 'cycle-display',
                run_type: 'forecast',
                scenario: 'forecast_gfs_deterministic',
                job_type: 'forecast',
                slurm_job_id: '2001',
                model_id: 'model-display',
                status: 'failed',
                stage: 'forecast',
                submitted_at: '2026-05-18T00:03:00Z',
                started_at: '2026-05-18T00:04:00Z',
                finished_at: '2026-05-18T00:06:00Z',
                exit_code: 1,
                retry_count: 0,
                error_code: 'E_MODEL',
                error_message: 'display failure',
                log_uri: 'published://logs/job-display-endpoint.log',
                duration_seconds: 120,
              },
            ],
            total: 1,
            limit: 12,
            offset: 0,
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/jobs/{job_id}/logs') {
        return {
          data: success({
            job_id: options?.params?.path?.job_id,
            log_uri: 'published://logs/job-display-endpoint.log',
            content: 'display endpoint log',
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/metrics/stage-duration' || path === '/api/v1/metrics/success-rate') {
        return { data: success([]), error: undefined } as never
      }
      if (path === '/api/v1/queue/depth') throw new Error('queue depth must not be called for display_readonly')
      throw new Error(`Unexpected GET ${path}`)
    })
    window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    await waitFor(() => expect(useMonitoringStore.getState().runtimeConfig).toMatchObject({
      service_role: 'display_readonly',
      control_mutations_enabled: false,
      queue_depth_mode: 'display_readonly_unavailable',
      display_readonly: true,
    }))
    expect(await screen.findByRole('row', { name: /run-display-endpoint.*model-display.*failed/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '队列深度不可用' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /查看日志/ }))
    expect(await screen.findByText('display endpoint log')).toBeInTheDocument()

    expect(vi.mocked(client.POST)).not.toHaveBeenCalled()
    expect(paths).not.toContain('/api/v1/queue/depth')
    expect(paths.some((path) => path.startsWith('/api/v1/slurm/'))).toBe(false)
  })

  it('copies failed stage basin diagnostics without a failed job row or backend writes', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    useMonitoringStore.setState({
      runtimeConfig: displayRuntimeConfig,
      runtimeConfigError: null,
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      cycle: {
        cycle_id: 'cycle-display-stage',
        source: 'GFS',
        cycle_time: '2026-05-18T00:00:00.000Z',
        current_state: 'failed',
        started_at: '2026-05-18T00:00:30Z',
        updated_at: '2026-05-18T00:08:00Z',
        job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
      },
      cycleContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      stages: [
        {
          stage: 'forecast',
          display_status: 'failed',
          status: 'failed',
          duration_seconds: 120,
          basin_progress: { completed: 0, total: 1, failed: 1 },
          basin_results_limit: 50,
          basin_results_total: 1,
          basin_results_returned: 1,
          basin_results_truncated: false,
          basin_results: [
            {
              job_id: 'stage-only-job',
              run_id: 'run-stage',
              cycle_id: 'cycle-display-stage',
              job_type: 'forecast',
              slurm_job_id: '3001',
              model_id: 'model-stage',
              basin_id: 'qhh-001',
              status: 'failed',
              stage: 'forecast',
              submitted_at: '2026-05-18T00:03:00Z',
              started_at: '2026-05-18T00:04:00Z',
              finished_at: '2026-05-18T00:06:00Z',
              duration_seconds: 120,
              retry_count: 0,
              error_code: 'E_STAGE',
              error_message: 'stage failed before job list refreshed',
              log_uri: 'published://logs/stage-only-job.log',
            },
          ],
        },
      ],
      jobs: [],
      jobsContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      jobTotal: 0,
    })
    window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /stage-only-job/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /预报.*forecast.*failed/ }))
    expect(screen.getByTestId('ops-stage-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
    expect(screen.getByTestId('ops-stage-manual-recovery-guidance')).toHaveTextContent('27 display_readonly')
    await user.click(screen.getByRole('button', { name: /复制流域诊断/ }))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(JSON.parse(writeText.mock.calls[0][0])).toEqual({
      source_id: 'GFS',
      cycle_time: '2026-05-18T00:00:00.000Z',
      run_id: 'run-stage',
      model_id: 'model-stage',
      stage: 'forecast',
      job_id: 'stage-only-job',
      slurm_job_id: '3001',
      status: 'failed',
      error_code: 'E_STAGE',
      error_message: 'stage failed before job list refreshed',
      log_uri: 'published://logs/stage-only-job.log',
    })
    expect(vi.mocked(client.POST)).not.toHaveBeenCalled()
  })

  it('keeps /monitoring cancel controls compatible for authorized active jobs', async () => {
    useAuthStore.setState({ role: 'operator' })
    useMonitoringStore.setState({
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      cycle: {
        source: 'IFS',
        cycle_time: '2026-05-18T00:00:00.000Z',
        current_state: 'running',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 3, failed: 0, running: 1, pending: 2 },
      },
      cycleContext: { source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      stages: [],
      jobs: [
        {
          job_id: 'job-monitoring-running',
          run_id: 'run-monitoring-running',
          cycle_id: 'cycle-monitoring',
          job_type: 'forecast',
          slurm_job_id: '2001',
          model_id: 'model-monitoring',
          status: 'running',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: null,
          exit_code: null,
          retry_count: 0,
          error_code: null,
          error_message: null,
          log_uri: 's3://logs/job-monitoring-running.log',
          duration_seconds: null,
        },
      ],
      jobsContext: { source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      jobTotal: 1,
      queue: { running: 2, pending: 4, idle: 6 },
    })
    window.history.pushState({}, '', '/monitoring?source=ifs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /run-monitoring-running/ })).getByRole('button', { name: /取消/ })).toBeVisible()
  })

  it.each(['failed', 'submission_failed', 'partially_failed', 'permanently_failed'] as const)(
    'shows authorized /ops log controls and retry for %s jobs with run ids',
    async (status) => {
      useAuthStore.setState({ role: 'operator' })
      useMonitoringStore.setState({
        source: 'GFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        cycleContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobs: [
          {
            job_id: `job-ops-${status}`,
            run_id: `run-ops-${status}`,
            cycle_id: 'cycle-ops',
            run_type: 'forecast',
            scenario: 'forecast_gfs_deterministic',
            job_type: 'forecast',
            slurm_job_id: '2001',
            model_id: 'model-ops',
            status,
            stage: 'forecast',
            submitted_at: '2026-05-09T00:03:00Z',
            started_at: '2026-05-09T00:04:00Z',
            finished_at: '2026-05-09T00:06:00Z',
            exit_code: 1,
            retry_count: 2,
            error_code: 'E_MODEL',
            error_message: 'model failed',
            log_uri: 's3://logs/job-ops.log',
            duration_seconds: 120,
          },
        ],
        jobsContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobTotal: 1,
      })
      window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

      render(<App />)

      expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
      const row = await screen.findByRole('row', { name: new RegExp(`run-ops-${status}`) })
      expect(within(row).getByRole('button', { name: /查看日志/ })).toBeVisible()
      expect(within(row).getByRole('button', { name: /重试/ })).toBeVisible()
      expect(within(row).queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
    },
  )

  it.each(['operator', 'model_admin', 'sys_admin'] as const)(
    'posts /ops retry for %s with compatible role header and never posts cancel',
    async (role) => {
      useAuthStore.setState({ role })
      const user = userEvent.setup()
      useMonitoringStore.setState({
        source: 'GFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        cycleContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobs: [
          {
            job_id: `job-ops-retry-${role}`,
            run_id: `run-ops-retry-${role}`,
            cycle_id: 'cycle-ops',
            job_type: 'forecast',
            slurm_job_id: '2001',
            model_id: 'model-ops',
            status: 'failed',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:03:00Z',
            started_at: '2026-05-09T00:04:00Z',
            finished_at: '2026-05-09T00:06:00Z',
            exit_code: 1,
            retry_count: 2,
            error_code: 'E_MODEL',
            error_message: 'model failed',
            log_uri: 's3://logs/job-ops.log',
            duration_seconds: 120,
          },
        ],
        jobsContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        jobTotal: 1,
        fetchAll: vi.fn().mockResolvedValue(undefined),
        fetchJobs: vi.fn().mockResolvedValue(undefined),
      })
      vi.mocked(client.POST).mockResolvedValue({ data: success({ status: 'submitted' }), error: undefined } as never)
      window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

      render(<App />)

      expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
      await user.click(await screen.findByRole('button', { name: /重试/ }))

      await waitFor(() => expect(vi.mocked(client.POST)).toHaveBeenCalledTimes(1))
      expect(vi.mocked(client.POST)).toHaveBeenCalledWith(
        '/api/v1/runs/{run_id}/retry',
        expect.objectContaining({
          params: {
            path: { run_id: `run-ops-retry-${role}` },
            header: { 'X-User-Role': role },
          },
        }),
      )
      expect(vi.mocked(client.POST).mock.calls.some(([path]) => path === '/api/v1/runs/{run_id}/cancel')).toBe(false)
    },
  )

  it.each(['analyst', 'viewer'] as const)('blocks /ops for %s before retry/log controls render', async (role) => {
    useAuthStore.setState({ role })
    useMonitoringStore.setState({
      jobs: [
        {
          job_id: 'job-denied',
          run_id: 'run-denied',
          cycle_id: 'cycle-denied',
          job_type: 'forecast',
          slurm_job_id: '2001',
          model_id: 'model-denied',
          status: 'failed',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: '2026-05-09T00:06:00Z',
          exit_code: 1,
          retry_count: 0,
          error_code: 'E_MODEL',
          error_message: 'model failed',
          log_uri: 's3://logs/job-denied.log',
          duration_seconds: 120,
        },
      ],
      jobTotal: 1,
    })
    window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('权限不足')
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /查看日志/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
    expect(vi.mocked(client.POST)).not.toHaveBeenCalled()
  })

  it('keeps /ops operational errors as explicit StageList unavailable state', async () => {
    useAuthStore.setState({ role: 'operator' })
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      cycle: null,
      cycleContext: null,
      stages: [],
      jobs: [],
      jobsContext: null,
      jobTotal: 0,
      operationalError: 'ops fixture API error',
      error: 'ops fixture API error',
      fetchAll: vi.fn().mockResolvedValue(undefined),
      fetchJobs: vi.fn().mockResolvedValue(undefined),
    })
    window.history.pushState({}, '', '/ops')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的流水线阶段不可用：ops fixture API error/)).toBeInTheDocument()
  })

  it('treats trailing-slash /ops/ as ops mode and keeps selector updates on /ops', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      cycleContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      jobs: [
        {
          job_id: 'job-ops-slash',
          run_id: 'run-ops-slash',
          cycle_id: 'cycle-ops',
          job_type: 'forecast',
          slurm_job_id: '2001',
          model_id: 'model-ops',
          status: 'failed',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: '2026-05-09T00:06:00Z',
          exit_code: 1,
          retry_count: 0,
          error_code: 'E_MODEL',
          error_message: 'model failed',
          log_uri: 's3://logs/job-ops.log',
          duration_seconds: 120,
        },
      ],
      jobsContext: { source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      jobTotal: 1,
      fetchAll: vi.fn().mockResolvedValue(undefined),
      fetchJobs: vi.fn().mockResolvedValue(undefined),
    })
    window.history.pushState({}, '', '/ops/?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /查看日志/ })).toBeVisible()

    await user.click(screen.getByLabelText('Source'))
    await user.click(await screen.findByRole('option', { name: 'IFS' }))

    await waitFor(() => expect(window.location.pathname).toBe('/ops'))
    expect(new URLSearchParams(window.location.search).get('source')).toBe('ifs')
  })

  it.each(['/ops?source=era5&cycle=2026-05-18T00:00:00Z', '/monitoring?source=era5&cycle=2026-05-18T00:00:00Z'])(
    'round-trips ERA5 monitoring source for %s',
    async (url) => {
      useAuthStore.setState({ role: 'operator' })
      const contexts: Array<{ type: string; source: string; cycleTime: string }> = []
      useMonitoringStore.setState({
        source: 'GFS',
        cycleTime: '2026-05-09T00:00:00Z',
        fetchAll: vi.fn().mockImplementation(async () => {
          const { source, cycleTime } = useMonitoringStore.getState()
          contexts.push({ type: 'all', source, cycleTime })
        }),
        fetchJobs: vi.fn().mockImplementation(async () => {
          const { source, cycleTime } = useMonitoringStore.getState()
          contexts.push({ type: 'jobs', source, cycleTime })
        }),
      })
      window.history.pushState({}, '', url)

      render(<App />)

      expect(await screen.findByRole('heading', { name: url.startsWith('/ops') ? '内部诊断' : '监控工作台' })).toBeInTheDocument()
      await waitFor(() =>
        expect(useMonitoringStore.getState()).toMatchObject({
          source: 'ERA5',
          cycleTime: '2026-05-18T00:00:00.000Z',
        }),
      )
      await waitFor(() =>
        expect(contexts).toEqual(expect.arrayContaining([
          { type: 'all', source: 'ERA5', cycleTime: '2026-05-18T00:00:00.000Z' },
          { type: 'jobs', source: 'ERA5', cycleTime: '2026-05-18T00:00:00.000Z' },
        ])),
      )
    },
  )

  it.each(['best', 'compare', 'bogus'])('keeps legacy /monitoring source=%s query state non-strict', async (unsupportedSource) => {
    useAuthStore.setState({ role: 'operator' })
    const currentCycle = '2026-05-09T00:00:00Z'
    const contexts: Array<{ type: string; source: string; cycleTime: string }> = []
    useMonitoringStore.setState({
      source: 'IFS',
      cycleTime: currentCycle,
      cycle: {
        source: 'IFS',
        cycle_time: currentCycle,
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
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      jobs: [
        {
          job_id: 'legacy-monitoring-job',
          run_id: 'legacy-monitoring-run',
          cycle_id: 'legacy-cycle',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'legacy-model',
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
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime })
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime })
      }),
    })
    window.history.pushState({}, '', `/monitoring?source=${unsupportedSource}&cycle=2026-05-18T00:00:00Z`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    expect(screen.queryByText(new RegExp(`source=${unsupportedSource} 不支持`))).not.toBeInTheDocument()
    expect(screen.queryByText(/当前 source\/cycle 的流水线阶段不可用/)).not.toBeInTheDocument()
    expect(screen.queryByText(/当前 source\/cycle 不支持趋势查询/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新' })).not.toBeDisabled()
    expect(screen.getByRole('row', { name: /legacy-monitoring-run/ })).toBeInTheDocument()
    expect(useMonitoringStore.getState()).toMatchObject({
      source: 'IFS',
      cycleTime: currentCycle,
    })
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'IFS', cycleTime: currentCycle },
        { type: 'jobs', source: 'IFS', cycleTime: currentCycle },
      ])),
    )
    expect(contexts).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ cycleTime: '2026-05-18T00:00:00.000Z' }),
    ]))
  })

  it('ignores malformed /monitoring cycle query state while preserving the usable monitoring view', async () => {
    useAuthStore.setState({ role: 'operator' })
    const currentCycle = '2026-05-09T00:00:00Z'
    const contexts: Array<{ type: string; source: string; cycleTime: string }> = []
    useMonitoringStore.setState({
      source: 'IFS',
      cycleTime: currentCycle,
      cycle: {
        source: 'IFS',
        cycle_time: currentCycle,
        current_state: 'running',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 4, failed: 0, running: 1, pending: 0 },
      },
      stages: [
        {
          stage: 'forecast',
          display_status: 'running',
          status: 'running',
          duration_seconds: 45,
          basin_progress: { completed: 1, total: 4, failed: 0 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      jobs: [
        {
          job_id: 'cycle-compatible-job',
          run_id: 'cycle-compatible-run',
          cycle_id: 'legacy-cycle',
          job_type: 'forecast',
          slurm_job_id: '1002',
          model_id: 'cycle-compatible-model',
          status: 'running',
          stage: 'forecast',
          submitted_at: '2026-05-09T00:03:00Z',
          started_at: '2026-05-09T00:04:00Z',
          finished_at: null,
          exit_code: null,
          retry_count: 0,
          error_code: null,
          error_message: null,
          log_uri: null,
          duration_seconds: null,
        },
      ],
      jobTotal: 1,
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime })
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime })
      }),
    })
    window.history.pushState({}, '', '/monitoring?source=gfs&cycle=bad-cycle')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    expect(screen.queryByText(/cycle=bad-cycle 不是有效 RFC3339 时间/)).not.toBeInTheDocument()
    expect(screen.queryByText(/当前 source\/cycle 的流水线阶段不可用/)).not.toBeInTheDocument()
    expect(screen.queryByText(/当前 source\/cycle 不支持趋势查询/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新' })).not.toBeDisabled()
    expect(screen.getByRole('row', { name: /cycle-compatible-run/ })).toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'GFS',
        cycleTime: currentCycle,
      }),
    )
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'GFS', cycleTime: currentCycle },
        { type: 'jobs', source: 'GFS', cycleTime: currentCycle },
      ])),
    )
    expect(contexts).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ cycleTime: 'bad-cycle' }),
    ]))
  })

  it.each(['best', 'compare', 'bogus'])('renders explicit /ops unavailable state for unsupported source=%s without scoped fetches', async (unsupportedSource) => {
    useAuthStore.setState({ role: 'operator' })
    const fetchAll = vi.fn().mockResolvedValue(undefined)
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      stages: [
        {
          stage: 'forecast',
          display_status: 'failed',
          status: 'failed',
          duration_seconds: 120,
          basin_progress: { completed: 0, total: 1, failed: 1 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      jobs: [
        {
          job_id: 'old-job',
          run_id: 'old-cycle-run',
          cycle_id: 'old-cycle',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'old-model',
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
      fetchAll,
      fetchJobs,
    })
    window.history.pushState({}, '', `/ops?source=${unsupportedSource}&cycle=2026-05-18T00:00:00Z`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect((await screen.findAllByText(new RegExp(`source=${unsupportedSource} 不支持`))).length).toBeGreaterThan(0)
    expect(screen.queryByRole('row', { name: /old-cycle-run/ })).not.toBeInTheDocument()
    expect(fetchAll).not.toHaveBeenCalled()
    expect(fetchJobs).not.toHaveBeenCalled()
  })

  it.each([
    ['/ops?source=compare&cycle=2026-05-18T00:00:00Z', /source=compare 不支持/],
    ['/ops?source=gfs&cycle=bad-cycle', /cycle=bad-cycle 不是有效 RFC3339 时间/],
  ] as const)('keeps unsupported /ops jobs controls from fetching stale/default jobs for %s', async (url, errorPattern) => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const jobsRequests: Array<Record<string, unknown> | undefined> = []
    const staleJob = {
      job_id: 'stale-default-job',
      run_id: 'stale-default-run',
      cycle_id: 'stale-default-cycle',
      job_type: 'forecast',
      slurm_job_id: '1001',
      model_id: 'stale-model',
      status: 'failed' as const,
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
    }
    useMonitoringStore.setState(
      {
        ...useMonitoringStore.getInitialState(),
        runtimeConfig: computeRuntimeConfig,
        runtimeConfigError: null,
        source: 'GFS',
        cycleTime: '2026-05-09T00:00:00Z',
        jobs: [staleJob],
        jobTotal: 1,
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      if (path === '/api/v1/jobs') {
        jobsRequests.push(options?.params?.query)
        return { data: success({ items: [staleJob], total: 1, limit: 12, offset: 0 }), error: undefined } as never
      }
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', url)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect((await screen.findAllByText(errorPattern)).length).toBeGreaterThan(0)
    await waitFor(() => expect(screen.queryByRole('row', { name: /stale-default-run/ })).not.toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /submitted_at/ }))

    expect(jobsRequests).toEqual([])
    expect(screen.queryByRole('row', { name: /stale-default-run/ })).not.toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的作业不可用/)).toBeInTheDocument()
  })

  it('keeps /monitoring jobs sorting on the normal jobs fetch path', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      fetchAll: vi.fn().mockResolvedValue(undefined),
      fetchJobs,
    })
    window.history.pushState({}, '', '/monitoring?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '监控工作台' })).toBeInTheDocument()
    await waitFor(() => expect(fetchJobs).toHaveBeenCalled())
    fetchJobs.mockClear()

    await user.click(screen.getByRole('button', { name: /submitted_at/ }))

    await waitFor(() =>
      expect(fetchJobs).toHaveBeenCalledWith(
        expect.objectContaining({ sortBy: 'submitted_at', sortOrder: 'asc', page: 1, pageSize: 12 }),
        expect.objectContaining({ clearOnFailure: false }),
      ),
    )
  })

  it.each([
    ['ifs', 'IFS'],
    ['era5', 'ERA5'],
  ] as const)('keeps valid /ops?source=%s direct links from fetching or rendering the previous store context', async (urlSource, expectedSource) => {
    useAuthStore.setState({ role: 'operator' })
    const oldCycle = '2026-05-09T00:00:00Z'
    const urlCycle = '2026-05-18T00:00:00.000Z'
    const oldJob = {
      job_id: 'old-job',
      run_id: 'old-cycle-run',
      cycle_id: 'old-cycle',
      job_type: 'forecast',
      slurm_job_id: '1001',
      model_id: 'old-model',
      status: 'failed' as const,
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
    }
    const targetJob = {
      ...oldJob,
      job_id: `${urlSource}-url-job`,
      run_id: `${urlSource}-url-run`,
      cycle_id: `${urlSource}-url-cycle`,
      model_id: `${urlSource}-model`,
      status: 'succeeded' as const,
      retry_count: 0,
      error_code: null,
      error_message: null,
      log_uri: `s3://logs/${urlSource}-url-job.log`,
      duration_seconds: 60,
    }
    const jobsRequests: Array<Record<string, unknown> | undefined> = []
    const scopedRequests: Array<{ path: string; source: unknown; cycleTime: unknown }> = []
    const metricRequests: Array<Record<string, unknown> | undefined> = []

    useMonitoringStore.setState(
      {
        ...useMonitoringStore.getInitialState(),
        runtimeConfig: computeRuntimeConfig,
        runtimeConfigError: null,
        source: 'GFS',
        cycleTime: oldCycle,
        cycle: {
          source: 'GFS',
          cycle_time: oldCycle,
          current_state: 'failed',
          started_at: '2026-05-09T00:00:30Z',
          updated_at: '2026-05-09T00:08:00Z',
          job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
        },
        stages: [
          {
            stage: 'forecast',
            display_status: 'failed',
            status: 'failed',
            duration_seconds: 120,
            basin_progress: { completed: 0, total: 1, failed: 1 },
            basin_results_limit: 50,
            basin_results_total: 0,
            basin_results_returned: 0,
            basin_results_truncated: false,
            basin_results: [],
          },
        ],
        jobs: [oldJob],
        jobTotal: 1,
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      const query = options?.params?.query
      if (path === '/api/v1/jobs') {
        jobsRequests.push(query)
        scopedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return {
          data: success({
            items: query?.source === expectedSource && query?.cycle_time === urlCycle ? [targetJob] : [oldJob],
            total: 1,
            limit: 12,
            offset: 0,
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/pipeline/status') {
        scopedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return {
          data: success({
            source: query?.source,
            cycle_time: query?.cycle_time,
            current_state: 'running',
            started_at: '2026-05-18T00:00:30Z',
            updated_at: '2026-05-18T00:08:00Z',
            job_counts: { succeeded: 1, failed: 0, running: 0, pending: 0 },
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/pipeline/stages') {
        scopedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return { data: success([]), error: undefined } as never
      }
      if (path === '/api/v1/queue/depth') {
        return { data: success({ running: 0, pending: 0, idle: 1 }), error: undefined } as never
      }
      if (path === '/api/v1/metrics/stage-duration' || path === '/api/v1/metrics/success-rate') {
        metricRequests.push(query)
        return { data: success([]), error: undefined } as never
      }
      return { data: success([]), error: undefined } as never
    })
    window.history.pushState({}, '', `/ops?source=${urlSource}&cycle=2026-05-18T00:00:00Z`)

    render(<App />)

    expect(screen.queryByRole('row', { name: /old-cycle-run/ })).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /old-cycle-run/ })).not.toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: expectedSource,
        cycleTime: urlCycle,
      }),
    )
    await waitFor(() =>
      expect(jobsRequests).toEqual(expect.arrayContaining([
        expect.objectContaining({ source: expectedSource, cycle_time: urlCycle }),
      ])),
    )

    expect(jobsRequests).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'GFS', cycle_time: oldCycle }),
    ]))
    expect(scopedRequests).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'GFS', cycleTime: oldCycle }),
    ]))
    expect(metricRequests).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'GFS' }),
    ]))
    expect(await screen.findByText(`${urlSource}-url-run`)).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /old-cycle-run/ })).not.toBeInTheDocument()
  })

  it('hides stale /ops payloads when URL and store keys already match until selected-context payloads load', async () => {
    useAuthStore.setState({ role: 'operator' })
    const selectedCycle = '2026-05-18T00:00:00.000Z'
    const oldCycle = '2026-05-09T00:00:00.000Z'
    const staleJob = {
      job_id: 'stale-job',
      run_id: 'stale-cycle-run',
      cycle_id: 'stale-cycle',
      job_type: 'forecast',
      slurm_job_id: '1001',
      model_id: 'stale-model',
      status: 'failed' as const,
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
    }
    const selectedJob = {
      ...staleJob,
      job_id: 'selected-job',
      run_id: 'selected-cycle-run',
      cycle_id: 'selected-cycle',
      model_id: 'selected-model',
      status: 'succeeded' as const,
      retry_count: 1,
      error_code: null,
      error_message: null,
      log_uri: 's3://logs/selected-job.log',
      duration_seconds: 60,
    }
    const statusResponse = deferred<unknown>()
    const stagesResponse = deferred<unknown>()
    const jobsResponse = deferred<unknown>()
    const selectedRequests: Array<{ path: string; source: unknown; cycleTime: unknown }> = []

    useMonitoringStore.setState(
      {
        ...useMonitoringStore.getInitialState(),
        runtimeConfig: computeRuntimeConfig,
        runtimeConfigError: null,
        source: 'IFS',
        cycleTime: selectedCycle,
        cycle: {
          source: 'GFS',
          cycle_time: oldCycle,
          current_state: 'stale-running',
          started_at: '2026-05-09T00:00:30Z',
          updated_at: '2026-05-09T00:08:00Z',
          job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
        },
        cycleContext: null,
        stages: [
          {
            stage: 'forecast',
            display_status: 'failed',
            status: 'failed',
            duration_seconds: 120,
            basin_progress: { completed: 0, total: 1, failed: 1 },
            basin_results_limit: 50,
            basin_results_total: 0,
            basin_results_returned: 0,
            basin_results_truncated: false,
            basin_results: [],
          },
        ],
        jobs: [staleJob],
        jobsContext: null,
        jobTotal: 1,
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation((path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
      const query = options?.params?.query
      if (path === '/api/v1/pipeline/status') {
        selectedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return statusResponse.promise as never
      }
      if (path === '/api/v1/pipeline/stages') {
        selectedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return stagesResponse.promise as never
      }
      if (path === '/api/v1/jobs') {
        selectedRequests.push({ path, source: query?.source, cycleTime: query?.cycle_time })
        return jobsResponse.promise as never
      }
      if (path === '/api/v1/queue/depth') {
        return Promise.resolve({ data: success({ running: 0, pending: 0, idle: 1 }), error: undefined }) as never
      }
      if (path === '/api/v1/metrics/stage-duration' || path === '/api/v1/metrics/success-rate') {
        return Promise.resolve({ data: success([]), error: undefined }) as never
      }
      return Promise.resolve({ data: success([]), error: undefined }) as never
    })
    window.history.pushState({}, '', '/ops?source=ifs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.queryByText('stale-running')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /预报.*forecast.*failed/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /stale-cycle-run/ })).not.toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的流水线阶段不可用：当前 source\/cycle 的流水线数据尚未加载完成。/)).toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的作业不可用：当前 source\/cycle 的作业数据尚未加载完成。/)).toBeInTheDocument()

    await waitFor(() =>
      expect(selectedRequests).toEqual(expect.arrayContaining([
        { path: '/api/v1/pipeline/status', source: 'IFS', cycleTime: selectedCycle },
        { path: '/api/v1/pipeline/stages', source: 'IFS', cycleTime: selectedCycle },
        { path: '/api/v1/jobs', source: 'IFS', cycleTime: selectedCycle },
      ])),
    )

    statusResponse.resolve({
      data: success({
        source: 'IFS',
        cycle_time: selectedCycle,
        current_state: 'selected-running',
        started_at: '2026-05-18T00:00:30Z',
        updated_at: '2026-05-18T00:08:00Z',
        job_counts: { succeeded: 1, failed: 0, running: 1, pending: 0 },
      }),
      error: undefined,
    })
    stagesResponse.resolve({
      data: success([
        {
          stage: 'forecast',
          display_status: 'succeeded',
          status: 'succeeded',
          duration_seconds: 60,
          basin_progress: { completed: 1, total: 1, failed: 0 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ]),
      error: undefined,
    })
    jobsResponse.resolve({
      data: success({ items: [selectedJob], total: 1, limit: 12, offset: 0 }),
      error: undefined,
    })

    expect(await screen.findByText('selected-running')).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: /selected-cycle-run.*selected-model.*succeeded/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /预报.*forecast.*succeeded/ })).toBeInTheDocument()
    expect(screen.queryByText('stale-running')).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /stale-cycle-run/ })).not.toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        cycleContext: { source: 'IFS', cycleTime: selectedCycle },
        jobsContext: { source: 'IFS', cycleTime: selectedCycle },
      }),
    )
  })

  it('initializes /ops source/cycle query into the monitoring store and scoped fetches without stale rows', async () => {
    useAuthStore.setState({ role: 'operator' })
    const contexts: Array<{ type: string; source: string; cycleTime: string }> = []
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      jobs: [
        {
          job_id: 'old-job',
          run_id: 'old-cycle-run',
          cycle_id: 'old-cycle',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'old-model',
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
      stages: [
        {
          stage: 'forecast',
          display_status: 'failed',
          status: 'failed',
          duration_seconds: 120,
          basin_progress: { completed: 0, total: 1, failed: 1 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime })
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime })
      }),
    })
    window.history.pushState({}, '', '/ops?source=ifs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'IFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        stages: [],
        jobs: [],
        jobTotal: 0,
      }),
    )
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
        { type: 'jobs', source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z' },
      ])),
    )
    expect(screen.queryByRole('row', { name: /old-cycle-run/ })).not.toBeInTheDocument()
  })

  it('initializes complete /ops strict identity query into scoped monitoring fetches', async () => {
    useAuthStore.setState({ role: 'operator' })
    const contexts: Array<{ type: string; source: string; cycleTime: string; runId?: string; modelId?: string }> = []
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      runtimeConfig: computeRuntimeConfig,
      runtimeConfigError: null,
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime, strictIdentity } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime, runId: strictIdentity?.runId, modelId: strictIdentity?.modelId })
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime, strictIdentity } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime, runId: strictIdentity?.runId, modelId: strictIdentity?.modelId })
      }),
    })
    window.history.pushState(
      {},
      '',
      '/ops?source=gfs&cycle_time=2026-05-18T00:00:00Z&run_id=run-strict&model_id=model-strict',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'GFS',
        cycleTime: '2026-05-18T00:00:00.000Z',
        strictIdentity: {
          source: 'GFS',
          cycleTime: '2026-05-18T00:00:00.000Z',
          runId: 'run-strict',
          modelId: 'model-strict',
        },
      }),
    )
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: 'run-strict', modelId: 'model-strict' },
        { type: 'jobs', source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: 'run-strict', modelId: 'model-strict' },
      ])),
    )
  })

  it('blocks partial /ops strict identity without source/cycle-only fallback requests', async () => {
    useAuthStore.setState({ role: 'operator' })
    const paths: Array<{ path: string; query?: Record<string, unknown> }> = []
    useMonitoringStore.setState(
      {
        ...useMonitoringStore.getInitialState(),
        runtimeConfig: computeRuntimeConfig,
        runtimeConfigError: null,
        source: 'GFS',
        cycleTime: '2026-05-09T00:00:00Z',
        cycle: {
          cycle_id: 'stale-cycle',
          source: 'GFS',
          cycle_time: '2026-05-09T00:00:00Z',
          current_state: 'stale-running',
          started_at: '2026-05-09T00:00:30Z',
          updated_at: '2026-05-09T00:08:00Z',
          job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
        },
        cycleContext: { source: 'GFS', cycleTime: '2026-05-09T00:00:00Z' },
        stages: [
          {
            stage: 'forecast',
            display_status: 'failed',
            status: 'failed',
            duration_seconds: 120,
            basin_progress: { completed: 0, total: 1, failed: 1 },
            basin_results_limit: 50,
            basin_results_total: 0,
            basin_results_returned: 0,
            basin_results_truncated: false,
            basin_results: [],
          },
        ],
        jobs: [
          {
            job_id: 'stale-strict-job',
            run_id: 'stale-strict-run',
            cycle_id: 'stale-cycle',
            job_type: 'forecast',
            slurm_job_id: '1001',
            model_id: 'stale-model',
            status: 'failed',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:03:00Z',
            started_at: '2026-05-09T00:04:00Z',
            finished_at: '2026-05-09T00:06:00Z',
            exit_code: 1,
            retry_count: 0,
            error_code: 'E_MODEL',
            error_message: 'model failed',
            log_uri: 's3://logs/stale-strict-job.log',
            duration_seconds: 120,
          },
        ],
        jobsContext: { source: 'GFS', cycleTime: '2026-05-09T00:00:00Z' },
        jobTotal: 1,
      },
      true,
    )
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      paths.push({ path, query: options?.params?.query })
      if (path === '/api/v1/metrics/stage-duration' || path === '/api/v1/metrics/success-rate') {
        return { data: success([]), error: undefined } as never
      }
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/ops?source=gfs&cycle_time=2026-05-18T00:00:00Z&run_id=run-strict')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect((await screen.findAllByText(/严格 identity 参数不完整/)).length).toBeGreaterThan(0)
    expect(screen.queryByText('stale-running')).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /stale-strict-run/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /查看日志/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(paths.some(({ path }) => [
      '/api/v1/pipeline/status',
      '/api/v1/pipeline/stages',
      '/api/v1/jobs',
      '/api/v1/jobs/{job_id}/logs',
    ].includes(path))).toBe(false)
  })

  it('clears strict identity before the first /ops selector refresh after strict params are removed', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const contexts: Array<{ type: string; source: string; cycleTime: string; runId?: string; modelId?: string }> = []
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      runtimeConfig: computeRuntimeConfig,
      runtimeConfigError: null,
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime, strictIdentity } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime, runId: strictIdentity?.runId, modelId: strictIdentity?.modelId })
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime, strictIdentity } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime, runId: strictIdentity?.runId, modelId: strictIdentity?.modelId })
      }),
    })
    window.history.pushState(
      {},
      '',
      '/ops?source=gfs&cycle_time=2026-05-18T00:00:00Z&run_id=run-strict&model_id=model-strict',
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: 'run-strict', modelId: 'model-strict' },
        { type: 'jobs', source: 'GFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: 'run-strict', modelId: 'model-strict' },
      ])),
    )
    contexts.length = 0

    await user.click(screen.getByLabelText('Source'))
    await user.click(await screen.findByRole('option', { name: 'IFS' }))

    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: undefined, modelId: undefined },
        { type: 'jobs', source: 'IFS', cycleTime: '2026-05-18T00:00:00.000Z', runId: undefined, modelId: undefined },
      ])),
    )
    expect(contexts).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'IFS', runId: 'run-strict' }),
      expect.objectContaining({ source: 'IFS', modelId: 'model-strict' }),
    ]))
    expect(new URLSearchParams(window.location.search).get('run_id')).toBeNull()
    expect(new URLSearchParams(window.location.search).get('model_id')).toBeNull()
  })

  it('keeps /ops selector changes, query state, store state, and scoped fetch context aligned', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const contexts: Array<{ type: string; source: string; cycleTime: string }> = []
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      jobs: [
        {
          job_id: 'old-selector-job',
          run_id: 'old-selector-run',
          cycle_id: 'old-cycle',
          job_type: 'forecast',
          slurm_job_id: '1001',
          model_id: 'old-model',
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
      stages: [
        {
          stage: 'forecast',
          display_status: 'failed',
          status: 'failed',
          duration_seconds: 120,
          basin_progress: { completed: 0, total: 1, failed: 1 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      fetchAll: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'all', source, cycleTime })
        await new Promise(() => undefined)
      }),
      fetchJobs: vi.fn().mockImplementation(async () => {
        const { source, cycleTime } = useMonitoringStore.getState()
        contexts.push({ type: 'jobs', source, cycleTime })
        await new Promise(() => undefined)
      }),
    })
    window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')
    render(<App />)
    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    await waitFor(() => expect(useMonitoringStore.getState().fetchAll).toHaveBeenCalled())
    contexts.length = 0

    await user.click(screen.getByLabelText('Source'))
    await user.click(await screen.findByRole('option', { name: 'IFS' }))
    fireEvent.change(screen.getByLabelText('Cycle Time UTC'), { target: { value: '2026-05-19T06:00' } })

    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'IFS',
        cycleTime: '2026-05-19T06:00:00.000Z',
        stages: [],
        jobs: [],
        jobTotal: 0,
      }),
    )
    expect(screen.queryByRole('row', { name: /old-selector-run/ })).not.toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/ops'))
    expect(new URLSearchParams(window.location.search).get('source')).toBe('ifs')
    expect(new URLSearchParams(window.location.search).get('cycle')).toBe('2026-05-19T06:00:00.000Z')
    await waitFor(() =>
      expect(contexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'IFS', cycleTime: '2026-05-19T06:00:00.000Z' },
        { type: 'jobs', source: 'IFS', cycleTime: '2026-05-19T06:00:00.000Z' },
      ])),
    )
  })

  it('keeps /ops retry refresh from displaying stale jobs, stages, or log modal after source/cycle changes', async () => {
    useAuthStore.setState({ role: 'operator' })
    const user = userEvent.setup()
    const oldCycle = '2026-05-18T00:00:00.000Z'
    const newCycle = '2026-05-19T06:00:00.000Z'
    const retryRequest = deferred<unknown>()
    const refreshContexts: Array<{ type: string; source: string; cycleTime: string }> = []

    const oldJob = {
      job_id: 'stale-retry-job',
      run_id: 'stale-retry-run',
      cycle_id: 'stale-retry-cycle',
      job_type: 'forecast',
      slurm_job_id: '1001',
      model_id: 'stale-retry-model',
      status: 'failed' as const,
      stage: 'old-stage',
      submitted_at: '2026-05-18T00:03:00Z',
      started_at: '2026-05-18T00:04:00Z',
      finished_at: '2026-05-18T00:06:00Z',
      exit_code: 1,
      retry_count: 0,
      error_code: 'E_MODEL',
      error_message: 'old failure',
      log_uri: 's3://logs/stale-retry-job.log',
      duration_seconds: 120,
    }
    const newJob = {
      ...oldJob,
      job_id: 'selected-retry-job',
      run_id: 'selected-retry-run',
      cycle_id: 'selected-retry-cycle',
      model_id: 'selected-retry-model',
      status: 'succeeded' as const,
      stage: 'new-stage',
      retry_count: 1,
      error_code: null,
      error_message: null,
      log_uri: 's3://logs/selected-retry-job.log',
      duration_seconds: 60,
    }
    const fetchAll = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      refreshContexts.push({ type: 'all', source, cycleTime })
      if (source === 'IFS' && cycleTime === newCycle) {
        useMonitoringStore.setState({
          cycle: {
            source: 'IFS',
            cycle_time: newCycle,
            current_state: 'selected-running',
            started_at: '2026-05-19T06:00:30Z',
            updated_at: '2026-05-19T06:08:00Z',
            job_counts: { succeeded: 1, failed: 0, running: 0, pending: 0 },
          },
          cycleContext: { source: 'IFS', cycleTime: newCycle },
          stages: [
            {
              stage: 'new-stage',
              display_status: 'succeeded',
              status: 'succeeded',
              duration_seconds: 60,
              basin_progress: { completed: 1, total: 1, failed: 0 },
              basin_results_limit: 50,
              basin_results_total: 0,
              basin_results_returned: 0,
              basin_results_truncated: false,
              basin_results: [],
            },
          ],
        })
      }
    })
    const fetchJobs = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      refreshContexts.push({ type: 'jobs', source, cycleTime })
      if (source === 'IFS' && cycleTime === newCycle) {
        useMonitoringStore.setState({
          jobs: [newJob],
          jobsContext: { source: 'IFS', cycleTime: newCycle },
          jobTotal: 1,
        })
      }
    })

    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: oldCycle,
      cycle: {
        source: 'GFS',
        cycle_time: oldCycle,
        current_state: 'old-failed',
        started_at: '2026-05-18T00:00:30Z',
        updated_at: '2026-05-18T00:08:00Z',
        job_counts: { succeeded: 0, failed: 1, running: 0, pending: 0 },
      },
      cycleContext: { source: 'GFS', cycleTime: oldCycle },
      stages: [
        {
          stage: 'old-stage',
          display_status: 'failed',
          status: 'failed',
          duration_seconds: 120,
          basin_progress: { completed: 0, total: 1, failed: 1 },
          basin_results_limit: 50,
          basin_results_total: 0,
          basin_results_returned: 0,
          basin_results_truncated: false,
          basin_results: [],
        },
      ],
      jobs: [oldJob],
      jobsContext: { source: 'GFS', cycleTime: oldCycle },
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/jobs/{job_id}/logs') {
        return {
          data: success({ job_id: 'stale-retry-job', log_uri: 's3://logs/stale-retry-job.log', content: 'old retry log content' }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/metrics/stage-duration' || path === '/api/v1/metrics/success-rate') {
        return { data: success([]), error: undefined } as never
      }
      return { data: success({}), error: undefined } as never
    })
    vi.mocked(client.POST).mockReturnValueOnce(retryRequest.promise as never)
    window.history.pushState({}, '', '/ops?source=gfs&cycle=2026-05-18T00:00:00Z')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /stale-retry-run/ })).toBeInTheDocument()
    expect(screen.getAllByText('old-stage').length).toBeGreaterThan(0)

    await user.click(within(screen.getByRole('row', { name: /stale-retry-run/ })).getByRole('button', { name: /重试/ }))
    await waitFor(() => expect(vi.mocked(client.POST)).toHaveBeenCalledTimes(1))

    await user.click(within(screen.getByRole('row', { name: /stale-retry-run/ })).getByRole('button', { name: /查看日志/ }))
    expect(await screen.findByText('old retry log content')).toBeInTheDocument()

    act(() => {
      useMonitoringStore.getState().setSource('IFS')
      useMonitoringStore.getState().setCycleTime(newCycle)
      useMonitoringStore.getState().clearSelectedContext()
    })

    await waitFor(() =>
      expect(useMonitoringStore.getState()).toMatchObject({
        source: 'IFS',
        cycleTime: newCycle,
      }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('row', { name: /stale-retry-run/ })).not.toBeInTheDocument()
    expect(screen.queryByText('old-stage')).not.toBeInTheDocument()
    expect(screen.queryByText('old retry log content')).not.toBeInTheDocument()

    await act(async () => {
      retryRequest.resolve({ data: success({ status: 'submitted' }), error: undefined })
      await retryRequest.promise
    })

    await waitFor(() =>
      expect(refreshContexts).toEqual(expect.arrayContaining([
        { type: 'all', source: 'IFS', cycleTime: newCycle },
        { type: 'jobs', source: 'IFS', cycleTime: newCycle },
      ])),
    )
    expect(useMonitoringStore.getState()).toMatchObject({
      cycleContext: { source: 'IFS', cycleTime: newCycle },
      jobsContext: { source: 'IFS', cycleTime: newCycle },
      jobs: [expect.objectContaining({ run_id: 'selected-retry-run', model_id: 'selected-retry-model' })],
      stages: [expect.objectContaining({ stage: 'new-stage', status: 'succeeded' })],
    })
    expect(screen.queryByRole('row', { name: /stale-retry-run/ })).not.toBeInTheDocument()
    expect(screen.queryByText('old-stage')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('blocks /ops for non-operator roles with the same monitoring RBAC boundary', async () => {
    useAuthStore.setState({ role: 'analyst' })
    window.history.pushState({}, '', '/ops')

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('权限不足')
    expect(screen.queryByRole('heading', { name: '内部诊断' })).not.toBeInTheDocument()
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

  it('runs model asset lifecycle preflight, confirmation, audit display, and refresh', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture({ active_flag: false, lifecycle_state: 'inactive' })
    const activatedModel = modelAssetRouteFixture({ active_flag: true, lifecycle_state: 'active' })
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    const activePage: ModelAssetPage = { items: [activatedModel], total: 1, limit: 50, offset: 0 }
    let activated = false
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(activated ? activePage : page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(activated ? activatedModel : model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    vi.mocked(client.POST).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models/{model_id}/preflight') {
        return {
          data: success({
            schema: 'nhms.model_operation_preflight.v1',
            operation: 'activate',
            status: 'ready',
            model_id: model.model_id,
            basin_version_id: model.basin_version_id,
            current_active_model_id: null,
            blockers: [],
            warnings: [],
            impact: { downstream_surfaces: ['forecast-routing', 'operator-audit'] },
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/models/{model_id}/lifecycle') {
        activated = true
        return {
          data: success({
            status: 'allowed',
            operation: 'activate',
            model: activatedModel,
            preflight: {
              schema: 'nhms.model_operation_preflight.v1',
              operation: 'activate',
              status: 'ready',
              model_id: model.model_id,
              blockers: [],
              warnings: [],
              impact: { downstream_surfaces: ['forecast-routing'] },
            },
            audit_reference: { entity_type: 'model_instance', entity_id: model.model_id, log_id: 12 },
          }),
          error: undefined,
        } as never
      }
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    const lifecycleCard = screen.getByRole('heading', { name: '生命周期操作' }).closest('div')?.parentElement
    expect(lifecycleCard).not.toBeNull()
    await userEvent.click(within(lifecycleCard as HTMLElement).getByRole('button', { name: /启用/ }))
    expect(await screen.findByText('预检通过')).toBeInTheDocument()
    expect(screen.getByText(/forecast-routing/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /确认执行/ }))
    expect(await screen.findByText('审计记录 12')).toBeInTheDocument()
    await waitFor(() => expect(vi.mocked(client.GET).mock.calls.filter(([path]) => path === '/api/v1/models').length).toBeGreaterThan(1))
  })

  it('requires explicit rollback target selection and sends that model id through preflight and execution', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const outgoing = modelAssetRouteFixture({ active_flag: true, lifecycle_state: 'active' })
    const staleSibling = modelAssetRouteFixture({
      model_id: 'basins_qhh_shud_previous_b',
      model_name: 'QHH previous B',
      active_flag: false,
      lifecycle_state: 'superseded',
      basin_version_id: outgoing.basin_version_id,
    })
    const restoredBefore = modelAssetRouteFixture({
      model_id: 'basins_qhh_shud_previous_c',
      model_name: 'QHH previous C',
      active_flag: false,
      lifecycle_state: 'superseded',
      basin_version_id: outgoing.basin_version_id,
    })
    const restoredAfter = modelAssetRouteFixture({ ...restoredBefore, active_flag: true, lifecycle_state: 'active' })
    const demoted = modelAssetRouteFixture({ ...outgoing, active_flag: false, lifecycle_state: 'superseded' })
    const page: ModelAssetPage = { items: [outgoing, staleSibling, restoredBefore], total: 3, limit: 50, offset: 0 }
    const rolledBackPage: ModelAssetPage = { items: [demoted, staleSibling, restoredAfter], total: 3, limit: 50, offset: 0 }
    const postedBodies: unknown[] = []
    let rolledBack = false
    vi.mocked(client.GET).mockImplementation(async (path: string, options?: unknown) => {
      if (path === '/api/v1/models') return { data: success(rolledBack ? rolledBackPage : page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') {
        const modelId = (options as { params?: { path?: { model_id?: string } } })?.params?.path?.model_id
        const model =
          modelId === outgoing.model_id
            ? rolledBack
              ? demoted
              : outgoing
            : modelId === staleSibling.model_id
              ? staleSibling
              : restoredBefore
        return { data: success(model), error: undefined } as never
      }
      return { data: success({}), error: undefined } as never
    })
    vi.mocked(client.POST).mockImplementation(async (path: string, options?: unknown) => {
      const body = (options as { body?: unknown })?.body
      postedBodies.push(body)
      expect(body).toMatchObject({ previous_model_id: restoredBefore.model_id })
      if (path === '/api/v1/models/{model_id}/preflight') {
        return {
          data: success({
            schema: 'nhms.model_operation_preflight.v1',
            operation: 'rollback_version',
            status: 'ready',
            model_id: outgoing.model_id,
            basin_version_id: outgoing.basin_version_id,
            current_active_model_id: outgoing.model_id,
            previous_model_id: restoredBefore.model_id,
            restored_model_id: restoredBefore.model_id,
            blockers: [],
            warnings: [],
            impact: { downstream_surfaces: ['forecast-routing', 'operator-audit'] },
          }),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/models/{model_id}/lifecycle') {
        rolledBack = true
        return {
          data: success({
            status: 'rollback',
            operation: 'rollback_version',
            model: restoredAfter,
            previous_model: demoted,
            preflight: {
              schema: 'nhms.model_operation_preflight.v1',
              operation: 'rollback_version',
              status: 'ready',
              model_id: outgoing.model_id,
              previous_model_id: restoredBefore.model_id,
              restored_model_id: restoredBefore.model_id,
              blockers: [],
              warnings: [],
              impact: { downstream_surfaces: ['forecast-routing'] },
            },
            audit_reference: { entity_type: 'model_instance', entity_id: outgoing.model_id, log_id: 21 },
          }),
          error: undefined,
        } as never
      }
      return { data: success({}), error: undefined } as never
    })
    window.history.pushState({}, '', `/system/model-assets?modelId=${outgoing.model_id}`)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    const lifecycleCard = screen.getByRole('heading', { name: '生命周期操作' }).closest('div')?.parentElement
    expect(lifecycleCard).not.toBeNull()
    await userEvent.click(within(lifecycleCard as HTMLElement).getByRole('button', { name: /回滚/ }))
    expect(screen.getByRole('combobox', { name: /回滚目标/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /确认执行/ })).toBeDisabled()
    expect(postedBodies).toEqual([])
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /回滚目标/ }), restoredBefore.model_id)
    expect(await screen.findByText('预检通过')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /确认执行/ }))
    expect(await screen.findByText('审计记录 21')).toBeInTheDocument()
    expect(postedBodies).toEqual([
      { operation: 'rollback_version', previous_model_id: restoredBefore.model_id },
      { operation: 'rollback_version', previous_model_id: restoredBefore.model_id },
    ])
    expect(JSON.stringify(postedBodies)).not.toContain(staleSibling.model_id)
  })

  it('shows backend lifecycle preflight blockers without executing confirmation', async () => {
    useAuthStore.setState({ role: 'model_admin' })
    const model = modelAssetRouteFixture()
    const page: ModelAssetPage = { items: [model], total: 1, limit: 50, offset: 0 }
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/models') return { data: success(page), error: undefined } as never
      if (path === '/api/v1/models/{model_id}') return { data: success(model), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })
    vi.mocked(client.POST).mockResolvedValue({
      data: success({
        schema: 'nhms.model_operation_preflight.v1',
        operation: 'deactivate',
        status: 'blocked',
        model_id: model.model_id,
        blockers: [{ code: 'MISSING_ACTIVE_RISK', message: 'Deactivation would leave this basin version without an active model.' }],
        warnings: [],
        impact: { downstream_surfaces: ['forecast-routing'] },
      }),
      error: undefined,
    } as never)
    window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '模型资产管理' })).toBeInTheDocument()
    const lifecycleCard = screen.getByRole('heading', { name: '生命周期操作' }).closest('div')?.parentElement
    expect(lifecycleCard).not.toBeNull()
    await userEvent.click(within(lifecycleCard as HTMLElement).getByRole('button', { name: /停用/ }))
    expect(await screen.findByText('预检阻断')).toBeInTheDocument()
    expect(screen.getByText(/MISSING_ACTIVE_RISK/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /确认执行/ })).toBeDisabled()
    expect(vi.mocked(client.POST).mock.calls.filter(([path]) => path === '/api/v1/models/{model_id}/lifecycle')).toHaveLength(0)
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
    'denies /system/model-assets for %s and does not fetch detail',
    async (role) => {
      useAuthStore.setState({ role })
      window.history.pushState({}, '', '/system/model-assets?modelId=basins_qhh_shud')

      render(<App />)

      expect(await screen.findByText('权限不足')).toBeInTheDocument()
      await waitFor(() => expect(vi.mocked(client.GET).mock.calls.length).toBe(0))
      expect(vi.mocked(client.GET).mock.calls.some(([path]) => path === '/api/v1/models/{model_id}')).toBe(false)
    },
  )
})

// 精确 query 契约：用 LegacyRedirect + 被动探针落点（不经 OverviewPage 归一化），
// 隔离 #337 的重定向责任（OverviewPage 自身的 query 归一化属 #338/#339 范畴）。
function RedirectLandingProbe() {
  const location = useLocation()
  return (
    <div data-testid="redirect-landing" data-pathname={location.pathname} data-search={location.search} />
  )
}

function RedirectMatrixHarness() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<RedirectLandingProbe />} />
        <Route path="/overview" element={<LegacyRedirect />} />
        <Route path="/hydro-met" element={<LegacyRedirect />} />
        <Route path="/forecast" element={<LegacyRedirect />} />
        <Route path="/meteorology" element={<LegacyRedirect extraParams={{ layer: 'met-stations' }} />} />
        <Route
          path="/flood-alerts"
          element={<LegacyRedirect extraParams={{ layer: 'flood-return-period' }} />}
        />
        <Route
          path="/basins/:basinId"
          element={<LegacyRedirect param={{ name: 'basinId', queryKey: 'basinId' }} />}
        />
        <Route
          path="/segments/:segmentId"
          element={<LegacyRedirect param={{ name: 'segmentId', queryKey: 'segmentId' }} />}
        />
      </Routes>
    </BrowserRouter>
  )
}

describe('legacy route redirect query contract', () => {
  async function landingParams() {
    const probe = await screen.findByTestId('redirect-landing')
    return {
      pathname: probe.getAttribute('data-pathname'),
      params: new URLSearchParams(probe.getAttribute('data-search') ?? ''),
    }
  }

  it.each(['/overview', '/hydro-met', '/forecast'])('redirects %s to bare /', async (path) => {
    window.history.pushState({}, '', path)

    render(<RedirectMatrixHarness />)

    const { pathname, params } = await landingParams()
    expect(pathname).toBe('/')
    expect(params.toString()).toBe('')
  })

  it('redirects /meteorology with layer=met-stations', async () => {
    window.history.pushState({}, '', '/meteorology')

    render(<RedirectMatrixHarness />)

    const { pathname, params } = await landingParams()
    expect(pathname).toBe('/')
    expect(params.get('layer')).toBe('met-stations')
  })

  it('redirects /flood-alerts with layer=flood-return-period', async () => {
    window.history.pushState({}, '', '/flood-alerts')

    render(<RedirectMatrixHarness />)

    const { params } = await landingParams()
    expect(params.get('layer')).toBe('flood-return-period')
  })

  it('redirects /basins/:basinId with basinId query', async () => {
    window.history.pushState({}, '', '/basins/basins_qhh')

    render(<RedirectMatrixHarness />)

    const { params } = await landingParams()
    expect(params.get('basinId')).toBe('basins_qhh')
  })

  it('redirects /segments/:segmentId with segmentId query', async () => {
    window.history.pushState({}, '', '/segments/seg_001')

    render(<RedirectMatrixHarness />)

    const { params } = await landingParams()
    expect(params.get('segmentId')).toBe('seg_001')
  })

  it('preserves original deep-link search and appends the semantic layer param', async () => {
    window.history.pushState({}, '', '/meteorology?source=IFS&time=2026-06-05T18:00:00Z')

    render(<RedirectMatrixHarness />)

    const { params } = await landingParams()
    expect(params.get('source')).toBe('IFS')
    expect(params.get('time')).toBe('2026-06-05T18:00:00Z')
    expect(params.get('layer')).toBe('met-stations')
  })

  it('keeps the original search value when a semantic key collides', async () => {
    window.history.pushState({}, '', '/meteorology?layer=foo')

    render(<RedirectMatrixHarness />)

    const { params } = await landingParams()
    // 同名键冲突时取原始 search 的值，语义参数不覆盖用户既有状态
    expect(params.getAll('layer')).toEqual(['foo'])
  })

  it('lands a segment deep-link without basin context as /?segmentId=...', async () => {
    window.history.pushState({}, '', '/segments/seg_x')

    render(<RedirectMatrixHarness />)

    const { pathname, params } = await landingParams()
    expect(pathname).toBe('/')
    expect(params.get('segmentId')).toBe('seg_x')
    expect(params.has('basinId')).toBe(false)
  })
})

describe('legacy routes converge on the single map shell (full App)', () => {
  async function expectSingleMapShell() {
    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    // 去导航后单页无顶部导航（NavBar 的 aria-label="Main navigation" 已移除）
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  }

  it.each(['/overview', '/hydro-met', '/forecast'])(
    'redirects %s to / via replace and renders the single map shell',
    async (path) => {
      window.history.pushState({}, '', path)

      render(<App />)

      await expectSingleMapShell()
      expect(window.location.pathname).toBe('/')
    },
  )

  it('does not pollute the back stack (replace, not push)', async () => {
    window.history.pushState({}, '', '/start-anchor')
    const lengthBefore = window.history.length
    window.history.pushState({}, '', '/overview')

    render(<App />)

    await expectSingleMapShell()
    expect(window.location.pathname).toBe('/')
    // replace 跳转不应新增历史项（相对 push /overview 之后）
    expect(window.history.length).toBe(lengthBefore + 1)
  })

  it('keeps /ops reachable (not redirected) for operator role', async () => {
    useAuthStore.setState({ role: 'operator' })
    window.history.pushState({}, '', '/ops')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '内部诊断' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/ops')
  })

  it('keeps /monitoring RBAC-denied for a role without ops access', async () => {
    useAuthStore.setState({ role: 'viewer' })
    window.history.pushState({}, '', '/monitoring')

    render(<App />)

    expect(await screen.findByText('权限不足')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/monitoring')
  })
})
