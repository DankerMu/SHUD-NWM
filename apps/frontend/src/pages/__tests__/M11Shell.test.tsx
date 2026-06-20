import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { forwardRef, useImperativeHandle, type ReactNode } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { m11VisualTokens } from '@/lib/m11/visualTokens'
import {
  createEmptyBasinDetail,
  createEmptyOverviewSummary,
  decideAggregationEndpoint,
  normalizeLayerStates,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
  type OverviewSummary,
  type SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, parseM11QueryState, serializeM11QueryState, type M11QueryPatch, type M11QueryState } from '@/lib/m11/queryState'
import {
  m11BasinRiverCollectionBudget,
  buildM11RegisteredOverlay,
  buildBasinFeatureCollection,
  buildBasinRiverFeatureCollection,
  buildSelectedSegmentFeatureCollection,
} from '@/components/map/M11MapLibreSurface'
import {
  LayerGroupControls,
  LayerLegendPanel,
  M11MapSurface,
  M11Timeline,
  SourceScenarioControls,
  buildM11TimelineViewModel,
  m11FallbackLegends,
  resolveM11ValidTimeCorrection,
} from '@/pages/m11/M11Controls'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import { OverviewPage } from '@/pages/OverviewPage'
import {
  useOverviewDataStore,
  type BasinDataSnapshot,
  type M11BasinRequestScope,
  type M11OverviewRequestScope,
  type OverviewDataSnapshot,
} from '@/stores/overviewData'
import { useStationLayerDataStore } from '@/stores/stationLayerData'

const mapSources: Array<Record<string, unknown>> = []
const mapLayers: Array<Record<string, unknown>> = []
const fitBoundsCalls: Array<unknown[]> = []
const flyToCalls: Array<unknown> = []
const clusterExpansionCalls: Array<unknown> = []
// 代站 cluster 展开 stub：模拟 source.getClusterExpansionZoom(clusterId, cb) 异步回调。
const clusterExpansionZoom = { value: 9 as number, error: null as unknown }

vi.mock('react-map-gl/maplibre', () => ({
  default: forwardRef(
    (
      {
        children,
        mapStyle,
        interactiveLayerIds,
        onMouseMove,
        onMouseLeave,
        onClick,
        onError,
      }: {
        children: ReactNode
        mapStyle: unknown
        interactiveLayerIds?: string[]
        onMouseMove?: (event: unknown) => void
        onMouseLeave?: (event: unknown) => void
        onClick?: (event: unknown) => void
        onError?: (event: unknown) => void
      },
      ref,
    ) => {
      const canvasStyle: Record<string, string> = {}
      const overlayFeature = {
        // 交互命中走透明加宽热区层（-hit），可见主线保持细，故 hover/click 命中 id 带 -hit 后缀。
        layer: { id: 'm11-flood-return-period-line-hit' },
        properties: { segment_id: 'seg-1', river_network_version_id: 'rn-v1' },
      }
      const riverFeature = {
        layer: { id: 'm11-basin-river-line' },
        properties: {
          segment_id: 'seg-009',
          river_segment_id: 'seg-009',
          basin_version_id: 'yangtze_v2026_01',
          river_network_version_id: 'rn-v1',
          segment_name: 'Main Stem 009',
        },
      }
      const basinFeature = { layer: { id: 'm11-basin-fill' }, properties: { basin_id: 'yangtze' } }
      const clusterFeature = {
        layer: { id: 'clusters' },
        id: 7,
        properties: { cluster: true, cluster_id: 7, point_count: 12 },
        geometry: { type: 'Point', coordinates: [101.5, 30.5] },
      }
      const stationPointFeature = {
        layer: { id: 'met-stations-point' },
        properties: { station_id: 'HMT-Y2-0237', station_name: 'Station 0237' },
        geometry: { type: 'Point', coordinates: [100.4, 30.4] },
      }
      const map = {
        fitBounds: (...args: unknown[]) => fitBoundsCalls.push(args),
        flyTo: (args: unknown) => flyToCalls.push(args),
        getSource: (id: string) => ({
          getClusterExpansionZoom: (clusterId: number, callback: (error: unknown, zoom: number) => void) => {
            clusterExpansionCalls.push({ id, clusterId })
            callback(clusterExpansionZoom.error, clusterExpansionZoom.value)
          },
        }),
      }
      useImperativeHandle(ref, () => ({
        ...map,
        getMap: () => map,
      }))
      return (
        <div
          data-testid="mock-maplibre-map"
          data-map-style={JSON.stringify(mapStyle)}
          data-interactive-layer-ids={(interactiveLayerIds ?? []).join(',')}
          onMouseMove={() =>
            onMouseMove?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [],
              point: { x: 0, y: 0 },
            })
          }
          onMouseLeave={() =>
            onMouseLeave?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [],
              point: { x: 0, y: 0 },
            })
          }
          onClick={() =>
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [],
              point: { x: 0, y: 0 },
            })
          }
          onPointerMove={() =>
            onMouseMove?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [overlayFeature],
              point: { x: 1, y: 1 },
            })
          }
          onPointerEnter={() =>
            onMouseMove?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [riverFeature],
              point: { x: 3, y: 3 },
            })
          }
          onDoubleClick={() =>
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [overlayFeature],
              point: { x: 1, y: 1 },
            })
          }
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [riverFeature],
              point: { x: 3, y: 3 },
            })
          }}
          onPointerOver={() =>
            onMouseMove?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [basinFeature, riverFeature],
              point: { x: 4, y: 4 },
            })
          }
          onMouseDown={(event) => {
            if (event.button !== 1) return
            event.preventDefault()
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [basinFeature, riverFeature],
              point: { x: 4, y: 4 },
            })
          }}
          onContextMenu={(event) => {
            event.preventDefault()
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [overlayFeature, basinFeature],
              point: { x: 2, y: 2 },
            })
          }}
          onFocus={() => onError?.({ error: { message: 'mock source failed' } })}
          onDrag={() =>
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [clusterFeature],
              point: { x: 5, y: 5 },
            })
          }
          onDrop={() =>
            onClick?.({
              target: { getCanvas: () => ({ style: canvasStyle }) },
              features: [stationPointFeature],
              point: { x: 6, y: 6 },
            })
          }
        >
          {children}
        </div>
      )
    },
  ),
  Source: ({ children, ...props }: { children: ReactNode } & Record<string, unknown>) => {
    mapSources.push(props)
    return <div data-testid="mock-map-source">{children}</div>
  },
  Layer: (props: Record<string, unknown>) => {
    mapLayers.push(props)
    return <div data-testid="mock-map-layer" />
  },
  NavigationControl: () => <div data-testid="mock-navigation-control" />,
  ScaleControl: () => <div data-testid="mock-scale-control" />,
  Popup: ({ children, longitude, latitude }: { children: ReactNode; longitude?: number; latitude?: number }) => (
    <div data-testid="mock-map-popup" data-longitude={String(longitude ?? '')} data-latitude={String(latitude ?? '')}>
      {children}
    </div>
  ),
  Marker: ({ children }: { children?: ReactNode }) => <div data-testid="mock-map-marker">{children}</div>,
}))

const state: M11QueryState = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T00:00:00.000Z',
  layer: 'discharge',
}

const freshness = {
  updatedAt: null,
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T00:00:00.000Z',
  runId: 'run-gfs',
  source: 'GFS' as const,
  isStale: false,
  staleAfterHours: 6,
  unavailableReason: null,
}

const floodMvtMetadata: NonNullable<LayerState['metadata']> = {
  layer_id: 'flood-return-period',
  tile_format: 'mvt',
  url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
  tile_url_template: '/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf',
  maplibre_source_layer: 'flood_return_period',
  source_layer: 'flood_return_period',
  fallback_available: true,
  release_blocking: false,
  required_placeholders: ['run_id', 'duration', 'valid_time', 'z', 'x', 'y'],
  source_refs: { run_id: 'run-gfs', source_version: 'rnv-v1', basin_version_id: 'basin-v1', duration: '1h' },
  valid_times: ['2026-05-18T06:00:00Z', '2026-05-18T12:00:00Z'],
  cache_version: 'flood-cache-v1',
  cache_etag: 'flood-etag-v1',
  schema_version: 'schema-v1',
  encoder_version: 'encoder-v1',
}

const dischargeMvtMetadata: NonNullable<LayerState['metadata']> = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  url_template: '/api/v1/tiles/hydro/{run_id}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  tile_url_template: '/api/v1/tiles/hydro/{run_id}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  maplibre_source_layer: 'hydro',
  source_layer: 'hydro',
  fallback_available: false,
  release_blocking: false,
  required_placeholders: ['run_id', 'valid_time', 'z', 'x', 'y'],
  source_refs: { run_id: 'run-gfs', source_version: 'rnv-v1', basin_version_id: 'basin-v1' },
  valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z', '2026-05-18T12:00:00Z'],
  cache_version: 'discharge-cache-v1',
  cache_etag: 'discharge-etag-v1',
  schema_version: 'schema-v1',
  encoder_version: 'encoder-v1',
}

// national 总览（无 basinId）：discharge 多流域并集 MVT，url_template 无 {run_id} 占位、
// required_placeholders 不含 run_id、min_zoom=7、release_blocking=false。
const dischargeNationalMvtMetadata: NonNullable<LayerState['metadata']> = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  url_template: '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  tile_url_template: '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  maplibre_source_layer: 'hydro',
  source_layer: 'hydro',
  fallback_available: false,
  release_blocking: false,
  required_placeholders: ['valid_time', 'z', 'x', 'y'],
  min_zoom: 7,
  max_zoom: 14,
  valid_times: ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z', '2026-05-18T12:00:00Z'],
  cache_version: 'discharge-national-cache-v1',
  schema_version: 'schema-v1',
  encoder_version: 'encoder-v1',
}

type HydroMvtLayerId = 'discharge' | 'flood-return-period' | 'warning-level'

const m11MvtMetadataByLayer = {
  'discharge': dischargeMvtMetadata,
  'flood-return-period': floodMvtMetadata,
  'warning-level': {
    ...floodMvtMetadata,
    layer_id: 'warning-level',
    alias_of: 'flood-return-period',
    canonical_route_layer_id: 'flood-return-period',
  },
} satisfies Record<HydroMvtLayerId, NonNullable<LayerState['metadata']>>

const m11LayerValidTimeByLayer = {
  'discharge': '2026-05-18T00:00:00.000Z',
  'flood-return-period': '2026-05-18T06:00:00.000Z',
  'warning-level': '2026-05-18T06:00:00.000Z',
} satisfies Record<HydroMvtLayerId, string>

const layers: LayerState[] = [
  {
    layerId: 'discharge',
    displayName: 'River discharge',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    currentValidTime: '2026-05-18T00:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    metadata: null,
    freshness,
    legend: [
      { label: '<500 m3/s', color: '#90CAF9', max: 500 },
      { label: '>5000 m3/s', color: '#0D47A1', min: 5000 },
    ],
  },
  {
    layerId: 'flood-return-period',
    displayName: 'Flood return period',
    group: 'hydrology',
    available: true,
    validTimes: ['2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    currentValidTime: '2026-05-18T12:00:00.000Z',
    validTimeSource: 'api',
    disabledReason: null,
    metadata: null,
    freshness: { ...freshness, validTime: '2026-05-18T12:00:00.000Z' },
    legend: [{ label: 'warning', color: '#FFB74D', min: 10, max: 20 }],
  },
  // 故意把 warning-level 留 unavailable，覆盖 LayerGroupControls 的"未注册水文图层占位"分支
  // （旧的退役水文 fixture 删除后没人占这个位置；以 warning-level 接替，保留同语义 sanity check）。
  {
    layerId: 'warning-level',
    displayName: 'Warning level',
    group: 'hydrology',
    available: false,
    validTimes: [],
    currentValidTime: null,
    validTimeSource: 'none',
    disabledReason: 'Layer has no valid times.',
    metadata: null,
    freshness: { ...freshness, validTime: null, unavailableReason: 'No valid-time metadata is available.' },
    legend: [],
  },
]

const overviewBasins: OverviewBasin[] = [
  {
    basinId: 'yangtze',
    displayName: 'Yangtze Basin',
    basinGroup: 'major',
    parentBasinId: null,
    level: 1,
    boundary: {
      type: 'MultiPolygon',
      coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31], [100, 30]]]],
    },
    bbox: { minLon: 100, minLat: 30, maxLon: 101, maxLat: 31 },
    areaKm2: 12_000,
    riverCount: 2,
    activeModelCount: 1,
    latestForecastTime: '2026-05-18T00:00:00.000Z',
    warningCounts: {
      normal: 0,
      elevated: 0,
      watch: 0,
      warning: 1,
      high_risk: 0,
      severe: 0,
      extreme: 0,
      unavailable: 0,
    },
    basinVersions: [],
    selectedBasinVersionId: 'yangtze_v2026_01',
    unavailableReason: null,
    qualityNote: null,
  },
]

const basinSegments: BasinSegmentRow[] = [
  {
    riverSegmentId: 'seg-009',
    riverNetworkVersionId: 'rn-v1',
    segmentId: 'seg-009',
    displayName: 'Main Stem 009',
    basinVersionId: 'yangtze_v2026_01',
    streamOrder: 3,
    lengthM: 1200,
    currentQ: 6200,
    qUnit: 'm3/s',
    returnPeriod: 12,
    warningLevel: 'warning',
    qualityFlag: 'ok',
    qualityNote: null,
    source: 'GFS',
    cycleTime: '2026-05-18T00:00:00.000Z',
    validTime: '2026-05-18T06:00:00.000Z',
    hasGeometry: true,
    geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    unavailableReason: null,
  },
  {
    riverSegmentId: 'seg-missing-geometry',
    riverNetworkVersionId: 'rn-v1',
    segmentId: 'seg-missing-geometry',
    displayName: 'Missing Geometry',
    basinVersionId: 'yangtze_v2026_01',
    streamOrder: null,
    lengthM: null,
    currentQ: null,
    qUnit: 'm3/s',
    returnPeriod: null,
    warningLevel: 'unavailable',
    qualityFlag: 'unavailable',
    qualityNote: null,
    source: null,
    cycleTime: null,
    validTime: null,
    hasGeometry: false,
    geometry: null,
    unavailableReason: 'Selected segment geometry is unavailable.',
  },
]

const sourceSelection: SourceScenarioSelectionState = {
  requestedSource: 'best',
  resolvedSource: 'IFS',
  scenarioIds: ['forecast_ifs_deterministic'],
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  comparisonAvailable: true,
  provenanceLabel: 'Best Available (IFS) / cycle 2026-05-18T00:00:00.000Z / valid 2026-05-18T06:00:00.000Z',
  unavailableReason: null,
}

function geoJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

// 复刻 store 的 requestScope key 公式（overviewData.ts: requestScopeQueryKey / requestScopeDataKey），
// 用于构造「与当前 query 真匹配」的 store 快照，让生产接线（loading || !currentOverview）真正被测到。
function scopeQueryKey(query: M11QueryState) {
  return serializeM11QueryState({ ...query, basinId: null, basemap: defaultM11QueryState.basemap, validTime: null })
}
function scopeDataKey(query: M11QueryState) {
  return serializeM11QueryState({ ...query, basinId: null, basemap: defaultM11QueryState.basemap })
}

function matchedOverviewScope(query: M11QueryState): M11OverviewRequestScope {
  return {
    kind: 'overview',
    queryKey: scopeQueryKey(query),
    dataKey: scopeDataKey(query),
    source: query.source,
    layer: query.layer,
    cycle: query.cycle,
    validTime: query.validTime,
    basemap: query.basemap,
    basinVersionId: query.basinVersionId,
    riverNetworkVersionId: query.riverNetworkVersionId,
    segmentId: query.segmentId,
    warningLevel: query.warningLevel,
    q: query.q,
  }
}

// query 已落定但结果为「matched-but-empty」（basins: []）的总览快照：诚实空态仍须显示。
function matchedEmptyOverviewSnapshot(query: M11QueryState): OverviewDataSnapshot {
  const summary: OverviewSummary = createEmptyOverviewSummary(query)
  return {
    requestScope: matchedOverviewScope(query),
    // bootstrap 非空 ≡ mapBootstrap 已 settle；空 basins/layers 让 OverviewPage 走"matched-but-empty"诚实空态。
    bootstrap: { basins: [], layers: [], layerStates: [], currentLayerValidTime: null },
    basins: [],
    summary,
    layers: [],
    aggregationDecision: decideAggregationEndpoint({
      initialRequestCount: 1,
      createsPerBasinNPlusOne: false,
      missingRequiredFields: [],
    }),
    basinVersionToBasinId: {},
  }
}

function matchedBasinScope(basinId: string, query: M11QueryState): M11BasinRequestScope {
  const identityQuery = { ...query, warningLevel: null, q: null }
  return { ...matchedOverviewScope(identityQuery), kind: 'basin-detail', basinId }
}

function matchedBasinSnapshot(basinId: string, query: M11QueryState): BasinDataSnapshot {
  return {
    requestScope: matchedBasinScope(basinId, query),
    detail: createEmptyBasinDetail(basinId, query),
    segments: [],
    selectedSegment: null,
    layers: [],
  }
}

describe('M11 visual foundation shell', () => {
  beforeEach(() => {
    mapSources.length = 0
    mapLayers.length = 0
    fitBoundsCalls.length = 0
    flyToCalls.length = 0
    clusterExpansionCalls.length = 0
    clusterExpansionZoom.value = 9
    clusterExpansionZoom.error = null
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async () => geoJsonResponse({ type: 'FeatureCollection', features: [] })),
    )
    useOverviewDataStore.setState({
      ...useOverviewDataStore.getInitialState(),
      loadOverview: vi.fn().mockResolvedValue(undefined),
      loadBasinDetail: vi.fn().mockResolvedValue(undefined),
    })
    useStationLayerDataStore.setState({
      ...useStationLayerDataStore.getInitialState(),
      loadStationLayer: vi.fn().mockResolvedValue(undefined),
      clear: vi.fn(),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('renders a fullscreen map with floating switcher/legend and no legacy side panels or timeline (M26)', () => {
    window.history.pushState({}, '', '/?warningLevel=major')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // M26 全屏单页：地图铺满视口 + 浮层；不再有三栏 shell / 侧栏 / timeline
    expect(screen.getByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-layer-switcher')).toBeInTheDocument()
    expect(screen.getByTestId('m11-floating-legend')).toBeInTheDocument()
    expect(m11VisualTokens.navHeight).toBe('0px')

    // 断言旧边栏控件 / timeline 不在 DOM
    expect(screen.queryByTestId('m11-shell')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('M11 左侧面板')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('M11 右侧面板')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-timeline')).not.toBeInTheDocument()
    expect(screen.queryByRole('slider', { name: '有效时间滑块' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('M11 数据源控制')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('全国流域树')).not.toBeInTheDocument()
  })

  it('defaults the floating switcher to the discharge layer and switches layers on click (M26)', async () => {
    window.history.pushState({}, '', '/')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    const user = userEvent.setup()
    // 默认流量（discharge）选中
    expect(screen.getByRole('button', { name: /流量/, pressed: true })).toBeInTheDocument()
    // 三项可点：流量 / 气象栅格 / 气象代站
    expect(screen.getByRole('button', { name: /气象栅格/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /气象代站/ })).toBeInTheDocument()

    // 选气象栅格 → honest 未注册占位，不画假图层
    await user.click(screen.getByRole('button', { name: /气象栅格/ }))
    await waitFor(() => expect(screen.getByTestId('m11-met-raster-notice')).toBeInTheDocument())
    expect(screen.getByTestId('m11-floating-legend-empty')).toHaveTextContent('未注册')
  })

  it('keeps default discharge unregistered without basin river geometry while preserving controls and unavailable map status', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    const { rerender } = render(<M11MapSurface state={state} layers={layers} onQueryChange={onQueryChange} />)

    const surface = screen.getByTestId('m11-map-surface')
    expect(surface).toHaveAttribute('data-basemap', 'vector')
    expect(surface).not.toHaveAttribute('data-registered-overlays')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', '')
    expect(screen.getByTestId('m11-map-unavailable')).toHaveTextContent('不会请求无边界 GeoJSON')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: '地形底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'terrain' })

    rerender(<M11MapSurface state={{ ...state, basemap: 'terrain' }} layers={layers} onQueryChange={onQueryChange} />)
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'terrain')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays')

    await user.click(screen.getByRole('button', { name: '卫星底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'satellite' })
  })

  it('registers the always-on national river basemap (Type+zoom graded) from static basin shp geo', () => {
    const riverGeo = {
      type: 'FeatureCollection' as const,
      features: [
        { type: 'Feature' as const, properties: { basin_id: 'basins_qhh', Type: 5 }, geometry: { type: 'LineString' as const, coordinates: [[100, 37], [100.1, 37.1]] } },
        { type: 'Feature' as const, properties: { basin_id: 'basins_heihe', Type: 1 }, geometry: { type: 'LineString' as const, coordinates: [[99, 39], [99.1, 39.1]] } },
      ],
    }
    render(<M11MapSurface state={state} layers={layers} nationalRiverGeo={riverGeo} onQueryChange={vi.fn()} />)

    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-national-river-feature-count', '2')
    expect(mapSources.find((source) => source.id === 'm11-national-river-source')).toMatchObject({ type: 'geojson', data: riverGeo })
    const riverLayer = mapLayers.find((layer) => layer.id === 'm11-national-river-line')
    expect(riverLayer).toMatchObject({ type: 'line', source: 'm11-national-river-source' })
    // Type+zoom 分级：color/width/opacity 都引用 Type 与 zoom（按缩放等级常态显示）。
    const paint = JSON.stringify(riverLayer?.paint)
    expect(paint).toContain('Type')
    expect(paint).toContain('zoom')
  })

  it('honestly skips the national river basemap when no static geo is available', () => {
    render(<M11MapSurface state={state} layers={layers} nationalRiverGeo={null} onQueryChange={vi.fn()} />)
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-national-river-feature-count', '0')
    expect(mapSources.find((source) => source.id === 'm11-national-river-source')).toBeUndefined()
    expect(mapLayers.find((layer) => layer.id === 'm11-national-river-line')).toBeUndefined()
  })

  // Bug-3：静态 national 河网与动态 mesh 河网层不得在同一流域上叠画双线。有动态线层（流量 MVT 线 /
  // 详情 GeoJSON 河段）覆盖的流域，其静态河流从 national 底图剔除；无动态层覆盖的流域（如 heihe）保留。
  it('excludes mesh-covered basins from the national river only when a dynamic river line is active', async () => {
    const riverGeo = {
      type: 'FeatureCollection' as const,
      features: [
        { type: 'Feature' as const, properties: { basin_id: 'basinA', Type: 5 }, geometry: { type: 'LineString' as const, coordinates: [[100, 37], [100.1, 37.1]] } },
        { type: 'Feature' as const, properties: { basin_id: 'basinB', Type: 3 }, geometry: { type: 'LineString' as const, coordinates: [[99, 39], [99.1, 39.1]] } },
      ],
    }
    const floodState = { ...state, layer: 'flood-return-period' as const, validTime: '2026-05-18T06:00:00.000Z' }
    const layersWithFloodMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    // 流量 MVT 线层激活 + basinA 已被 mesh 覆盖 → 仅渲染 basinB 的静态河流（basinA 剔除）。
    const { rerender } = render(
      <M11MapSurface state={floodState} layers={layersWithFloodMvt} nationalRiverGeo={riverGeo} meshRiverBasinIds={['basinA']} onQueryChange={vi.fn()} />,
    )
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-national-river-feature-count', '1')
    const renderedRiver = mapSources.find((source) => source.id === 'm11-national-river-source')?.data as typeof riverGeo
    expect(renderedRiver.features.map((feature) => feature.properties.basin_id)).toEqual(['basinB'])

    // 无 mesh 覆盖（meshRiverBasinIds 空）→ 即便线层激活也保留全部静态河流（不剔除）。
    rerender(
      <M11MapSurface state={floodState} layers={layersWithFloodMvt} nationalRiverGeo={riverGeo} meshRiverBasinIds={[]} onQueryChange={vi.fn()} />,
    )
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-national-river-feature-count', '2'))

    // 无动态河网线层（met-stations 不注册 overlay）→ overlayIsRiverLine=false → 即便 meshRiverBasinIds 有值也不剔除，
    // 保留全部静态河流（守住非线叠加层不误删的诚实降级语义）。
    rerender(
      <M11MapSurface state={{ ...state, layer: 'met-stations' }} layers={layers} nationalRiverGeo={riverGeo} meshRiverBasinIds={['basinA']} onQueryChange={vi.fn()} />,
    )
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-national-river-feature-count', '2'))
  })

  it('marks hydrology data layers renderable and keeps river network unavailable', () => {
    const normalizedLayers = normalizeLayerStates({
      query: state,
      layers: [
        { layer_id: 'discharge', layer_name: 'River discharge', layer_type: 'hydrology', variables: ['q_down'], metadata: null },
        { layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: ['return_period'], metadata: null },
        { layer_id: 'warning-level', layer_name: 'Warning level', layer_type: 'hydrology', variables: ['warning_level'], metadata: null },
        { layer_id: 'river-network', layer_name: 'River network', layer_type: 'base', variables: ['geometry'], metadata: null },
      ],
      validTimesByLayerId: {
        discharge: ['2026-05-18T00:00:00Z'],
        'flood-return-period': ['2026-05-18T00:00:00Z'],
        'warning-level': ['2026-05-18T00:00:00Z'],
        'river-network': ['2026-05-18T00:00:00Z'],
      },
      resolvedRun: {
        run_id: 'run-gfs',
        run_type: 'forecast',
        scenario_id: 'forecast_gfs_deterministic',
        model_id: 'model-1',
        basin_version_id: 'bv-001',
        source_id: 'gfs',
        cycle_time: '2026-05-18T00:00:00Z',
        status: 'frequency_done',
        start_time: '2026-05-18T00:00:00Z',
        end_time: '2026-05-18T03:00:00Z',
        created_at: '2026-05-18T00:00:00Z',
        updated_at: '2026-05-18T04:00:00Z',
      },
    })

    expect(normalizedLayers.find((layer) => layer.layerId === 'flood-return-period')).toMatchObject({ available: true, disabledReason: null })
    expect(normalizedLayers.find((layer) => layer.layerId === 'discharge')).toMatchObject({ available: true, disabledReason: null })
    expect(normalizedLayers.find((layer) => layer.layerId === 'warning-level')).toMatchObject({ available: true, disabledReason: null })
    expect(normalizedLayers.find((layer) => layer.layerId === 'river-network')).toMatchObject({
      available: false,
      disabledReason: expect.stringContaining('no renderable map source'),
    })

    render(<LayerGroupControls state={state} layers={normalizedLayers} onQueryChange={vi.fn()} />)
    expect(screen.getByText('河网')).toBeInTheDocument()
    expect(screen.queryByText('已由图层 API 注册')).not.toBeInTheDocument()
  })

  it('registers flood return period vector source and keeps it through basemap switches using selected URL valid time', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    const floodState = { ...state, layer: 'flood-return-period' as const, validTime: '2026-05-18T06:00:00.000Z' }
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    const { rerender } = render(<M11MapSurface state={floodState} layers={layersWithMvt} onQueryChange={onQueryChange} />)

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', 'm11-flood-return-period-line-hit')
    expect(mapSources.at(-1)).toMatchObject({
      id: 'm11-flood-return-period-source',
      type: 'vector',
      promoteId: 'feature_id',
      tiles: [
        `${window.location.origin}/api/v1/tiles/flood-return-period/run-gfs/1h/2026-05-18T06%3A00%3A00.000Z/{z}/{x}/{y}.pbf?_mvt_cache_version=flood-cache-v1`,
      ],
    })
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
    const floodMainLayer = mapLayers.find((layer) => layer.id === 'm11-flood-return-period-line')
    expect(floodMainLayer).toMatchObject({ id: 'm11-flood-return-period-line', source: 'm11-flood-return-period-source' })
    expect(JSON.stringify(floodMainLayer?.paint)).toContain('warning_level')
    expect(JSON.stringify(floodMainLayer?.paint)).toContain('return_period')
    // 透明加宽热区层存在且不可见（line-opacity:0），让细河段可点中。
    expect(mapLayers.find((layer) => layer.id === 'm11-flood-return-period-line-hit')).toMatchObject({
      id: 'm11-flood-return-period-line-hit',
      source: 'm11-flood-return-period-source',
    })

    await user.click(screen.getByRole('button', { name: '地形底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'terrain' })

    mapSources.length = 0
    mapLayers.length = 0
    rerender(<M11MapSurface state={{ ...floodState, basemap: 'terrain' }} layers={layersWithMvt} onQueryChange={onQueryChange} />)
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(mapSources.at(-1)).toMatchObject({ id: 'm11-flood-return-period-source', type: 'vector' })
    expect(mapLayers.find((layer) => layer.id === 'm11-flood-return-period-line')).toMatchObject({ id: 'm11-flood-return-period-line' })
  })

  it('does not register M11 vector overlays when selected valid time is not advertised by metadata', () => {
    for (const layerId of ['flood-return-period', 'warning-level', 'discharge'] as const) {
      const selectedTime = m11LayerValidTimeByLayer[layerId]
      const baseMetadata = m11MvtMetadataByLayer[layerId]
      for (const validTimes of [[], ['2026-05-18T18:00:00.000Z']]) {
        const testedLayer: LayerState = {
          ...(layers.find((layer) => layer.layerId === layerId) ?? layers[0]),
          layerId,
          available: true,
          validTimes: [selectedTime],
          currentValidTime: selectedTime,
          validTimeSource: 'api',
          disabledReason: null,
          metadata: { ...baseMetadata, valid_times: validTimes },
          freshness: { ...freshness, validTime: selectedTime },
        }

        expect(buildM11RegisteredOverlay({ ...state, layer: layerId, validTime: selectedTime }, [testedLayer])).toBeNull()
      }
    }
  })

  it('registers discharge vector source from advertised hydrology MVT metadata', async () => {
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'discharge' ? { ...layer, metadata: dischargeMvtMetadata } : layer,
    )
    render(<M11MapSurface state={state} layers={layersWithMvt} onQueryChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'discharge'))
    expect(mapSources.at(-1)).toMatchObject({
      id: 'm11-discharge-source',
      type: 'vector',
      tiles: [
        `${window.location.origin}/api/v1/tiles/hydro/run-gfs/q_down/2026-05-18T00%3A00%3A00.000Z/{z}/{x}/{y}.pbf?_mvt_cache_version=discharge-cache-v1`,
      ],
    })
    const paint = JSON.stringify(mapLayers.find((layer) => layer.id === 'm11-discharge-line')?.paint)
    expect(paint).toContain('value')
    expect(paint).not.toContain('warning_level')
    expect(paint).not.toContain('return_period')
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })

  it('registers national discharge vector source without run_id when metadata is national', async () => {
    // national：discharge 图层无可追溯 run_id（freshness.runId=null），但元数据为 national 形态。
    const layersNational = layers.map((layer) =>
      layer.layerId === 'discharge'
        ? { ...layer, metadata: dischargeNationalMvtMetadata, freshness: { ...layer.freshness, runId: null } }
        : layer,
    )
    render(<M11MapSurface state={state} layers={layersNational} onQueryChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'discharge'))
    const source = mapSources.at(-1)
    expect(source).toMatchObject({
      id: 'm11-discharge-source',
      type: 'vector',
      minzoom: 7,
      tiles: [
        `${window.location.origin}/api/v1/tiles/hydro-national/q_down/2026-05-18T00%3A00%3A00.000Z/{z}/{x}/{y}.pbf?_mvt_cache_version=discharge-national-cache-v1`,
      ],
    })
    // 模板无 {run_id}，填充后不得残留未替换占位。
    expect(String((source?.tiles as string[])[0])).not.toContain('{run_id}')
    expect(String((source?.tiles as string[])[0])).not.toContain('run-gfs')
    expect(mapLayers.find((layer) => layer.id === 'm11-discharge-line')).toMatchObject({
      id: 'm11-discharge-line',
      source: 'm11-discharge-source',
      'source-layer': 'hydro',
    })
    // 透明加宽热区层（-hit）也注册，让细河段可点中开流量弹窗。
    expect(mapLayers.find((layer) => layer.id === 'm11-discharge-line-hit')).toMatchObject({
      id: 'm11-discharge-line-hit',
      source: 'm11-discharge-source',
    })
    // national 正常注册 → 不应再出现"缺少可追溯 run_id"诚实空态。
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()
  })

  it('builds distinct national source keys per valid_time without run_id collision', () => {
    const buildAt = (validTime: string) =>
      buildM11RegisteredOverlay(
        { ...state, validTime },
        layers.map((layer) =>
          layer.layerId === 'discharge'
            ? { ...layer, metadata: dischargeNationalMvtMetadata, freshness: { ...layer.freshness, runId: null } }
            : layer,
        ),
      )
    const overlayA = buildAt('2026-05-18T00:00:00.000Z')
    const overlayB = buildAt('2026-05-18T06:00:00.000Z')
    expect(overlayA?.sourceKey).toBeTruthy()
    expect(overlayA?.sourceKey).not.toEqual(overlayB?.sourceKey)
    expect(overlayA?.sourceKey).toContain('"run_id":null')
  })

  it('changes M11 vector source identity across tile-defining metadata and route inputs', () => {
    const floodOverlay = buildM11RegisteredOverlay(
      { ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' },
      layers.map((layer) =>
        layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
      ),
    )
    expect(floodOverlay?.source.tiles[0]).toContain('2026-05-18T06%3A00%3A00.000Z')
    // 用 map() 而非 [...layers, override] —— layers 数组里已有 warning-level=unavailable
    // 占位（供 LayerGroupControls 占位分支用），append 会让 `layers.find(...)` 抓到那条
    // unavailable 的、返回 null overlay；这里以 metadata-bearing entry 覆盖即可。
    const warningOverlay = buildM11RegisteredOverlay(
      { ...state, layer: 'warning-level', validTime: '2026-05-18T06:00:00.000Z' },
      layers.map((layer) =>
        layer.layerId === 'warning-level'
          ? {
              ...layer,
              available: true,
              validTimes: ['2026-05-18T06:00:00.000Z'],
              currentValidTime: '2026-05-18T06:00:00.000Z',
              disabledReason: null,
              metadata: {
                ...floodMvtMetadata,
                layer_id: 'warning-level',
                alias_of: 'flood-return-period',
                canonical_route_layer_id: 'flood-return-period',
              },
            }
          : layer,
      ),
    )
    const dischargeOverlay = buildM11RegisteredOverlay(
      state,
      layers.map((layer) => (layer.layerId === 'discharge' ? { ...layer, metadata: dischargeMvtMetadata } : layer)),
    )
    const floodRunChanged = buildM11RegisteredOverlay(
      { ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' },
      layers.map((layer) =>
        layer.layerId === 'flood-return-period'
          ? { ...layer, metadata: floodMvtMetadata, freshness: { ...layer.freshness, runId: 'run-gfs-2' } }
          : layer,
      ),
    )
    const floodCacheChanged = buildM11RegisteredOverlay(
      { ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' },
      layers.map((layer) =>
        layer.layerId === 'flood-return-period'
          ? { ...layer, metadata: { ...floodMvtMetadata, cache_version: 'flood-cache-v2', cache_etag: 'flood-etag-v2' } }
          : layer,
      ),
    )

    expect(floodOverlay?.sourceKey).toContain('flood-cache-v1')
    expect(floodOverlay?.sourceKey).not.toEqual(dischargeOverlay?.sourceKey)
    expect(floodOverlay?.sourceKey).not.toEqual(floodRunChanged?.sourceKey)
    expect(floodOverlay?.sourceKey).not.toEqual(floodCacheChanged?.sourceKey)
    expect(floodOverlay?.source.tiles[0]).toContain('_mvt_cache_version=flood-cache-v1')
    expect(floodCacheChanged?.source.tiles[0]).toContain('_mvt_cache_version=flood-cache-v2')
    expect(floodOverlay?.source.tiles).not.toEqual(floodCacheChanged?.source.tiles)
    expect(warningOverlay?.source.tiles).toEqual(floodOverlay?.source.tiles)
    expect(warningOverlay?.sourceKey).not.toEqual(floodOverlay?.sourceKey)
  })

  it('does not create unbounded GeoJSON national source when MVT metadata is missing', async () => {
    const layersWithoutMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: null } : layer,
    )
    const floodState = { ...state, layer: 'flood-return-period' as const, validTime: '2026-05-18T06:00:00.000Z' }

    render(<M11MapSurface state={floodState} layers={layersWithoutMvt} onQueryChange={vi.fn()} />)

    expect(await screen.findByTestId('m11-map-unavailable')).toHaveTextContent('不会请求无边界 GeoJSON')
    expect(mapSources.find((source) => source.id === 'm11-flood-return-period-source')).toBeUndefined()
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })

  it('renders basin river network from segment rows and colors by active hydrology layer', async () => {
    const onOverlayHover = vi.fn()
    const onOverlayClick = vi.fn()
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    const { rerender } = render(
      <M11MapSurface
        state={state}
        layers={layersWithMvt}
        basinSegments={basinSegments}
        selectedSegmentId="seg-009"
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />,
    )

    const surface = screen.getByTestId('m11-map-surface')
    expect(surface).toHaveAttribute('data-basin-river-feature-count', '1')
    expect(surface).toHaveAttribute('data-basin-river-skipped-count', '1')
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()
    expect(screen.getByTestId('m11-basin-river-unavailable')).toHaveTextContent('1 条河段缺少可渲染几何')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', 'm11-basin-river-line')
    expect(mapSources.at(-1)).toMatchObject({ id: 'm11-basin-river-source', type: 'geojson' })
    expect(mapLayers.map((layer) => layer.id)).toEqual(
      expect.arrayContaining([
        'm11-basin-river-line',
        'm11-basin-river-hover-halo',
        'm11-basin-river-selected-halo',
        'm11-basin-river-hover-line',
        'm11-basin-river-selected-line',
      ]),
    )

    fireEvent.pointerEnter(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayHover).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'basin-river-segments' }))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-hovered-segment-id', 'seg-009')
    expect(screen.getByTestId('m11-river-tooltip')).toHaveTextContent('Main Stem 009')
    expect(screen.getByTestId('m11-river-tooltip')).toHaveTextContent('6,200 m3/s')
    expect(screen.getByTestId('m11-river-tooltip')).toHaveTextContent('12 年一遇')

    fireEvent.keyDown(screen.getByTestId('mock-maplibre-map'), { key: 'Enter' })
    expect(onOverlayClick).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'basin-river-segments' }))

    const dischargeCollection = buildBasinRiverFeatureCollection(basinSegments, 'discharge')
    const returnPeriodCollection = buildBasinRiverFeatureCollection(basinSegments, 'flood-return-period')
    const warningCollection = buildBasinRiverFeatureCollection(basinSegments, 'warning-level')
    expect(dischargeCollection.features[0].properties.layer_color).not.toBe(returnPeriodCollection.features[0].properties.layer_color)
    expect(returnPeriodCollection.features[0].properties.layer_color).toBe(warningCollection.features[0].properties.layer_color)
    expect(dischargeCollection.features[0].properties).toMatchObject({
      basin_version_id: 'yangtze_v2026_01',
      river_network_version_id: 'rn-v1',
      river_segment_id: 'seg-009',
    })
    expect(dischargeCollection.features[0].properties).not.toHaveProperty('selected')
    expect(dischargeCollection.features[0].properties).not.toHaveProperty('hovered')

    rerender(<M11MapSurface state={{ ...state, layer: 'warning-level' }} layers={layers} basinSegments={basinSegments} />)
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-river-feature-count', '1')
  })

  it('keeps API-normalized layer legends aligned with basin river feature colors', () => {
    const normalizedLayers = normalizeLayerStates({
      query: state,
      layers: [
        { layer_id: 'discharge', layer_name: 'Discharge', layer_type: 'hydrology', variables: ['q_down'], metadata: null },
        {
          layer_id: 'flood-return-period',
          layer_name: 'Flood return period',
          layer_type: 'hydrology',
          variables: ['return_period'],
          metadata: null,
        },
        { layer_id: 'warning-level', layer_name: 'Warning level', layer_type: 'hydrology', variables: ['warning_level'], metadata: null },
      ],
      validTimesByLayerId: {
        discharge: ['2026-05-18T00:00:00Z'],
        'flood-return-period': ['2026-05-18T00:00:00Z'],
        'warning-level': ['2026-05-18T00:00:00Z'],
      },
    })
    const representativeRows: BasinSegmentRow[] = [
      { ...basinSegments[0], currentQ: 250, returnPeriod: 1, warningLevel: 'normal' },
      { ...basinSegments[0], currentQ: 750, returnPeriod: 3, warningLevel: 'elevated' },
      { ...basinSegments[0], currentQ: 2_000, returnPeriod: 7, warningLevel: 'watch' },
      { ...basinSegments[0], currentQ: 7_000, returnPeriod: 12, warningLevel: 'warning' },
      { ...basinSegments[0], currentQ: 20_000, returnPeriod: 25, warningLevel: 'high_risk' },
      { ...basinSegments[0], currentQ: 60_000, returnPeriod: 120, warningLevel: 'extreme' },
      { ...basinSegments[0], currentQ: null, returnPeriod: null, warningLevel: 'unavailable' },
    ].map((row, index) => ({
      ...row,
      riverSegmentId: `legend-river-${index}`,
      segmentId: `legend-seg-${index}`,
      geometry: { type: 'LineString', coordinates: [[100 + index * 0.01, 30], [100.005 + index * 0.01, 30.005]] },
    }))

    for (const layerId of ['discharge', 'flood-return-period', 'warning-level'] as const) {
      const legendColors = normalizedLayers.find((layer) => layer.layerId === layerId)?.legend.map((entry) => entry.color)
      const fallbackLegendColors = m11FallbackLegends[layerId].map((entry) => entry.color)
      const featureColors = buildBasinRiverFeatureCollection(representativeRows, layerId).features.map(
        (feature) => feature.properties.layer_color,
      )
      expect(legendColors).toEqual(expect.arrayContaining([...new Set(featureColors)]))
      expect(fallbackLegendColors).toEqual(expect.arrayContaining([...new Set(featureColors)]))
    }
  })

  it('prioritizes river interactions when MapLibre returns overlapping basin and river features', () => {
    const onOverlayHover = vi.fn()
    const onOverlayClick = vi.fn()
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    render(
      <M11MapSurface
        state={state}
        layers={layersWithMvt}
        basins={overviewBasins}
        basinSegments={basinSegments}
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />,
    )

    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute(
      'data-interactive-layer-ids',
      'm11-basin-river-line,m11-basin-fill',
    )
    fireEvent.pointerOver(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayHover).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'basin-river-segments' }))
    fireEvent.mouseDown(screen.getByTestId('mock-maplibre-map'), { button: 1 })
    expect(onOverlayClick).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'basin-river-segments' }))
  })

  it('caps aggregate basin river collections before registering a MapLibre source', () => {
    const manySegments = Array.from({ length: m11BasinRiverCollectionBudget.maxFeatures + 4 }, (_, index): BasinSegmentRow => ({
      ...basinSegments[0],
      riverSegmentId: `seg-${String(index).padStart(5, '0')}`,
      segmentId: `seg-${String(index).padStart(5, '0')}`,
      displayName: `Segment ${index}`,
      geometry: { type: 'LineString', coordinates: [[100, 30], [100.01, 30.01]] },
    }))

    const collection = buildBasinRiverFeatureCollection(manySegments, 'discharge')
    expect(collection.features).toHaveLength(m11BasinRiverCollectionBudget.maxFeatures)
    expect(collection.skippedCount).toBe(4)
    expect(collection.coordinateCount).toBeLessThanOrEqual(m11BasinRiverCollectionBudget.maxCoordinates)
    expect(collection.serializedBytes).toBeLessThanOrEqual(m11BasinRiverCollectionBudget.maxSerializedBytes)
    expect(collection.unavailableReason).toContain('整体河网预算')

    render(<M11MapSurface state={state} layers={layers} basinSegments={manySegments} />)
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute(
      'data-basin-river-feature-count',
      String(m11BasinRiverCollectionBudget.maxFeatures),
    )
    expect(screen.getByTestId('m11-basin-river-unavailable')).toHaveTextContent('整体河网预算')
  })

  it('keeps bulk basin river source data free of hover and selection state across pointer movement', () => {
    render(
      <M11MapSurface
        state={state}
        layers={layers}
        basinSegments={basinSegments}
        selectedSegmentId="seg-009"
      />,
    )

    const initialSourceData = mapSources.find((source) => source.id === 'm11-basin-river-source')?.data
    expect(JSON.stringify(initialSourceData)).not.toContain('hovered')
    expect(JSON.stringify(initialSourceData)).not.toContain('selected')

    fireEvent.pointerEnter(screen.getByTestId('mock-maplibre-map'))
    const hoveredSourceData = mapSources.find((source) => source.id === 'm11-basin-river-source')?.data
    expect(hoveredSourceData).toEqual(initialSourceData)
    expect(JSON.stringify(hoveredSourceData)).not.toContain('hovered')
    expect(JSON.stringify(hoveredSourceData)).not.toContain('selected')
  })

  it('blocks M11 flood return period national GeoJSON fallback when MVT metadata is missing', async () => {
    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
      />,
    )

    expect(await screen.findByTestId('m11-map-unavailable')).toHaveTextContent('不会请求无边界 GeoJSON')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/tiles/flood-return-period?'), expect.anything())
  })

  it('blocks release-blocked M11 flood return period MVT metadata without registering a source', async () => {
    const blockedLayers = layers.map((layer) =>
      layer.layerId === 'flood-return-period'
        ? { ...layer, metadata: { ...floodMvtMetadata, release_blocking: true } }
        : layer,
    )

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={blockedLayers}
      />,
    )

    expect(await screen.findByTestId('m11-map-unavailable')).toHaveTextContent('release-blocked')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('threads camera and overlay callbacks into the MapLibre primitive', async () => {
    const onOverlayHover = vi.fn()
    const onOverlayClick = vi.fn()
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layersWithMvt}
        fitTo={{ bounds: [[100, 30], [105, 35]], padding: 24 }}
        flyTo={{ center: [102, 32], zoom: 7 }}
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />,
    )

    expect(fitBoundsCalls).toEqual([[[[100, 30], [105, 35]], { padding: 24, duration: 450 }]])
    expect(flyToCalls).toEqual([{ center: [102, 32], zoom: 7, duration: 450 }])

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    fireEvent.mouseMove(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayHover).toHaveBeenCalledWith(null)
    expect(onOverlayHover).not.toHaveBeenCalledWith(expect.objectContaining({ layerId: 'flood-return-period' }))
    fireEvent.pointerMove(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayHover).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'flood-return-period' }))
    fireEvent.mouseLeave(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayHover).toHaveBeenCalledWith(null)
    fireEvent.click(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayClick).not.toHaveBeenCalled()
    fireEvent.doubleClick(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayClick).toHaveBeenCalledWith(expect.objectContaining({ layerId: 'flood-return-period' }))
  })

  it('dispatches the river-segment overlay over basin-fill when both are under the click (#508)', async () => {
    const onOverlayClick = vi.fn()
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layersWithMvt}
        basins={overviewBasins}
        onOverlayClick={onOverlayClick}
      />,
    )

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    // contextMenu mock 同时命中河段热区（m11-flood-return-period-line-hit）与 basin-fill：
    // 新优先级下河段比所在流域多边形更具体，必须先命中、被分发；basin-fill 不再抢走点击。
    fireEvent.contextMenu(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayClick).toHaveBeenCalledWith(
      expect.objectContaining({
        layerId: 'flood-return-period',
        feature: expect.objectContaining({ properties: expect.objectContaining({ segment_id: 'seg-1' }) }),
      }),
    )
    expect(onOverlayClick).not.toHaveBeenCalledWith(
      expect.objectContaining({ layerId: 'basin-boundaries' }),
    )
  })

  it('does not repeat equal camera fit commands across rerenders', () => {
    const { rerender } = render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
        fitTo={{ bounds: [[100, 30], [105, 35]], padding: 24 }}
      />,
    )

    expect(fitBoundsCalls).toHaveLength(1)

    rerender(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
        fitTo={{ bounds: [[100, 30], [105, 35]], padding: 24 }}
      />,
    )

    expect(fitBoundsCalls).toHaveLength(1)
  })

  it('shows a scoped map source error while keeping other controls usable', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    const layersWithMvt = layers.map((layer) =>
      layer.layerId === 'flood-return-period' ? { ...layer, metadata: floodMvtMetadata } : layer,
    )

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layersWithMvt}
        basins={overviewBasins}
        onQueryChange={onQueryChange}
      />,
    )

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    fireEvent.focus(screen.getByTestId('mock-maplibre-map'))
    expect(screen.getByTestId('m11-map-source-error')).toHaveTextContent('mock source failed')
    await user.click(screen.getByRole('button', { name: '卫星底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'satellite' })
  })

  it('registers visible basin boundaries and labels without claiming hidden basin geometry', () => {
    const { rerender } = render(<M11MapSurface state={state} layers={layers} basins={overviewBasins} />)

    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '1')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'yangtze')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', 'm11-basin-fill')
    expect(mapSources.at(-1)).toMatchObject({
      id: 'm11-basin-boundaries-source',
      type: 'geojson',
    })
    expect(mapLayers.map((layer) => layer.id)).toEqual(
      expect.arrayContaining(['m11-basin-fill', 'm11-basin-outline']),
    )
    // 流域名标注走 DOM Marker（天地图栅格 style 无 glyphs，symbol 文本层不可用）。
    expect(screen.getByTestId('m11-basin-label')).toHaveTextContent('Yangtze Basin')

    mapSources.length = 0
    mapLayers.length = 0
    rerender(<M11MapSurface state={state} layers={layers} basins={overviewBasins} visibleBasinIds={[]} />)
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '0')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', '')
    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('当前没有可见流域边界')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('does not register oversized basin geometry as a map source', () => {
    const coordinates: number[][] = []
    for (let index = 0; index < 50_002; index += 1) {
      coordinates.push([100 + index * 0.00001, 30])
    }
    const oversizedBasins: OverviewBasin[] = [
      {
        ...overviewBasins[0],
        boundary: { type: 'MultiPolygon', coordinates: [[[...coordinates]]] },
      },
    ]

    render(<M11MapSurface state={state} layers={layers} basins={oversizedBasins} />)

    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '0')
    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('渲染预算')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('does not register under-vertex basin geometry with oversized coordinate tails', () => {
    const tail = Array.from({ length: 32 }, (_, index) => index)
    const oversizedBasins: OverviewBasin[] = [
      {
        ...overviewBasins[0],
        boundary: {
          type: 'MultiPolygon',
          coordinates: [[[[100, 30, ...tail], [101, 30, ...tail], [101, 31, ...tail], [100, 31, ...tail], [100, 30, ...tail]]]],
        },
      },
    ]

    render(<M11MapSurface state={state} layers={layers} basins={oversizedBasins} />)

    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-feature-count', '0')
    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('渲染预算')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('suppresses transient empty-state notices while data/boundaries are still loading (no refresh flash)', () => {
    // 刷新加载竞态：basins 已到、边界几何未就绪 → features=0；加载态门控下不得闪空态提示。
    const { rerender } = render(
      <M11MapSurface state={state} layers={[]} basins={overviewBasins} visibleBasinIds={[]} loading />,
    )
    expect(screen.queryByTestId('m11-basin-layer-unavailable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()

    // overview 已 settle 但静态边界几何仍在加载 → 仍抑制"边界未就绪"瞬态。
    rerender(<M11MapSurface state={state} layers={[]} basins={overviewBasins} visibleBasinIds={[]} boundaryLoading />)
    expect(screen.queryByTestId('m11-basin-layer-unavailable')).not.toBeInTheDocument()

    // 全部 settle 后才诚实显示真·空态。
    rerender(<M11MapSurface state={state} layers={[]} basins={overviewBasins} visibleBasinIds={[]} />)
    expect(screen.getByTestId('m11-basin-layer-unavailable')).toHaveTextContent('当前没有可见流域边界')
  })

  // 生产接线锚定：渲染真实 OverviewPage（总览/流域详情两种模式），证明
  // loading || !currentOverview（及流域侧的 surfaceSettling）在 frame-1 抑制瞬态空态/不可用，
  // 而 matched-but-empty 的诚实空态仍如实显示。叶子 harness（上一组）覆盖留存，此处只是补全。
  it('suppresses overview frame-1 transient notices when store is unsettled (overview=null, loading=false)', () => {
    window.history.pushState({}, '', '/?warningLevel=major')
    // beforeEach 已置 overview=null / loading=false（首帧竞态原貌）。

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // frame-1：数据未落定 → 不得闪任何不可用/空态
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-basin-layer-unavailable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-overview-empty')).not.toBeInTheDocument()
    // 诚实改为"加载中"占位
    expect(screen.getByTestId('m11-overview-loading')).toBeInTheDocument()
  })

  it('still shows honest overview empty notice when a query-matched snapshot has no basins', () => {
    window.history.pushState({}, '', '/?warningLevel=major')
    const query = parseM11QueryState(window.location.search)
    useOverviewDataStore.setState({
      overview: matchedEmptyOverviewSnapshot(query),
      mapBootstrapLoading: false,
      enrichmentLoading: false,
    })

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // 关键反回归：query 已匹配 → currentOverview 非空 → surfaceSettling=false → 诚实空态如实显示
    expect(screen.getByTestId('m11-overview-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-overview-loading')).not.toBeInTheDocument()
  })

  // PR #589 round-2 C1：阶段 1 reject（bootstrapError !=null + overview.bootstrap=null）→
  // surfaceSettling 必须退出（spec scenario "Map bootstrap rejection"：MUST render bootstrap
  // failed state rather than indefinite spinner）。bootstrapError 必须从 m11-overview-empty 透出。
  it('surfaces bootstrap error and exits surface settling when bootstrap rejected', () => {
    window.history.pushState({}, '', '/?warningLevel=major')
    const query = parseM11QueryState(window.location.search)
    // bootstrap reject 后 phase 2 final snapshot：bootstrap=null + basins=[]（reject 路径 fetchBasins
    // 通常仍 reject → settledValue 返回 [] → overviewBasins=[]）。
    const rejectedSnapshot: OverviewDataSnapshot = {
      requestScope: matchedOverviewScope(query),
      bootstrap: null,
      basins: [],
      summary: createEmptyOverviewSummary(query),
      layers: [],
      aggregationDecision: decideAggregationEndpoint({
        initialRequestCount: 1,
        createsPerBasinNPlusOne: false,
        missingRequiredFields: [],
      }),
      basinVersionToBasinId: {},
    }
    useOverviewDataStore.setState({
      overview: rejectedSnapshot,
      mapBootstrapLoading: false,
      enrichmentLoading: false,
      bootstrapError: 'basins: 暂不可用',
    })

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // 关键合同：bootstrap reject 时 spinner 必须消失（否则永远 spinner = spec 违约）。
    expect(screen.queryByTestId('m11-overview-loading')).not.toBeInTheDocument()
    // emptyBasinReason 走 bootstrapError 分支 → 诚实告知失败。
    const emptyNotice = screen.getByTestId('m11-overview-empty')
    expect(emptyNotice).toHaveTextContent('basins')
    expect(emptyNotice).toHaveTextContent('暂不可用')
  })

  // PR 3/7 #582 task 3.5：mapBootstrap settle 后 MVT hit layer 已注册 + 地图可点击；
  // enrichmentLoading=true 不阻塞 surface（spec scenario "Map bootstrap completes before enrichment"
  // 的浏览器层验证：interactiveLayerIds 非空，且 m11-overview-loading 占位不再渲染）。
  it('registers MVT hit layer and keeps surface interactive once mapBootstrap settles even while enrichment is still loading', () => {
    // 默认 national overview URL：discharge + 当前 valid_time 已被 metadata.valid_times 覆盖。
    window.history.pushState({}, '', '/?source=gfs&validTime=2026-05-18T06:00:00.000Z')
    const query = parseM11QueryState(window.location.search)
    // 构造可注册的 discharge LayerState：必须 available + currentValidTime 匹配 + metadata 携带相同
    // valid_time（M11MapLibreSurface.buildM11RegisteredOverlay 要求三者齐备）。
    const dischargeLayer: LayerState = {
      layerId: 'discharge',
      displayName: 'River discharge',
      group: 'hydrology',
      available: true,
      validTimes: ['2026-05-18T06:00:00.000Z'],
      currentValidTime: '2026-05-18T06:00:00.000Z',
      validTimeSource: 'api',
      disabledReason: null,
      metadata: { ...dischargeNationalMvtMetadata, valid_times: ['2026-05-18T06:00:00.000Z'] },
      freshness: { ...freshness, runId: null, validTime: '2026-05-18T06:00:00.000Z' },
      legend: [],
    }
    const settledLayers: LayerState[] = [dischargeLayer]
    const snapshot: OverviewDataSnapshot = {
      requestScope: matchedOverviewScope(query),
      bootstrap: { basins: [], layers: [], layerStates: settledLayers, currentLayerValidTime: '2026-05-18T06:00:00.000Z' },
      basins: overviewBasins,
      summary: createEmptyOverviewSummary(query),
      layers: settledLayers,
      aggregationDecision: decideAggregationEndpoint({
        initialRequestCount: 1,
        createsPerBasinNPlusOne: false,
        missingRequiredFields: [],
      }),
      basinVersionToBasinId: {},
    }
    // 关键合同：phase 1 settle / phase 2 仍 in-flight（enrichmentLoading=true）。
    useOverviewDataStore.setState({
      overview: snapshot,
      mapBootstrapLoading: false,
      enrichmentLoading: true,
    })

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // surface 未被 enrichment 阻塞：loading 浮层 / 不可用浮层都不应渲染。
    expect(screen.queryByTestId('m11-overview-loading')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()
    // MVT hit layer 已注册（按 buildM11RegisteredOverlay 命名）→ map 可点击。
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'discharge')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute(
      'data-interactive-layer-ids',
      expect.stringContaining('m11-discharge-line-hit'),
    )
  })

  // Bug-1 合同：刷新/直达带 basinId 的 URL → 首挂载剥离 basinId，落在全国总览主页（不进流域详情），
  // 且 URL 不再携带 basinId；frame-1 不闪 m11-map-unavailable。
  it('strips basinId on initial load and lands on the national overview (not basin detail)', async () => {
    window.history.pushState({}, '', '/?basinId=qhh')
    // beforeEach 已置 basinDetail=null / basinLoading=false（深链直达某流域 URL 的首帧）。

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // 落总览：全屏地图 + 总览切换器在位；不进流域详情（无返回总览按钮 / 钻取地图）。
    expect(await screen.findByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(screen.getByLabelText('全国总览地图')).toBeInTheDocument()
    expect(screen.queryByLabelText('流域钻取地图')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-back-to-overview')).not.toBeInTheDocument()
    // URL 已剥离 basinId；frame-1 不闪不可用横幅。
    await waitFor(() => expect(window.location.search).not.toContain('basinId='))
    expect(screen.queryByTestId('m11-map-unavailable')).not.toBeInTheDocument()
  })

  // 会话内（挂载后）切到某 basinId → 仍正常进流域详情，且 query-matched 快照经 surfaceSettling 落定后渲染。
  // 用一次会话内导航（pushState + popstate，BrowserRouter 监听 popstate）模拟"挂载后点流域"，不重新挂载，
  // 故 Bug-1 一次性剥离闸门已置真、不再剥离。
  it('reflects a query-matched basin snapshot through surfaceSettling for in-session navigation', async () => {
    const query = parseM11QueryState('?basinId=qhh')
    useOverviewDataStore.setState({ basinDetail: matchedBasinSnapshot('qhh', query), basinLoading: false })
    window.history.pushState({}, '', '/')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    // 首挂载落总览（无 basinId）。
    expect(await screen.findByLabelText('全国总览地图')).toBeInTheDocument()

    // 会话内切到 basinId（挂载后）→ 不再剥离 → 进流域详情；matched 快照 → surfaceSettling=false，外壳正常渲染。
    await act(async () => {
      window.history.pushState({}, '', '/?basinId=qhh')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(await screen.findByLabelText('流域钻取地图')).toBeInTheDocument()
    expect(screen.getByTestId('m11-fullscreen-map')).toBeInTheDocument()
    expect(window.location.search).toContain('basinId=qhh')
  })

  it('omits malformed selected segment geometry from MapLibre sources while showing selected unavailable state', () => {
    render(
      <M11MapSurface
        state={state}
        layers={layers}
        selectedSegmentId="seg-bad"
        selectedSegmentGeometry={{ type: 'LineString', coordinates: [[100, 30]] }}
      />,
    )

    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', 'seg-bad')
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-map-state', 'unavailable')
    expect(screen.getByTestId('m11-selected-segment-map-unavailable')).toHaveTextContent('少于两个坐标点')
    expect(mapSources).not.toEqual(expect.arrayContaining([expect.objectContaining({ id: 'm11-selected-segment-source' })]))
    expect(mapLayers.map((layer) => layer.id)).not.toContain('m11-selected-segment-line')
  })

  it('omits over-byte basin geometry from MapLibre feature collections', () => {
    const coordinates = Array.from({ length: 50_000 }, (_, index) => [
      100.1234567890123 + index / 100_000,
      30.1234567890123 + index / 100_000,
    ])
    const featureCollection = buildBasinFeatureCollection(
      [
        {
          ...overviewBasins[0],
          boundary: { type: 'MultiPolygon', coordinates: [[[...coordinates]]] },
        },
      ],
      undefined,
    )

    expect(featureCollection.features).toHaveLength(0)
  })

  it('omits oversized selected segment geometry from MapLibre feature collections', () => {
    const featureCollection = buildSelectedSegmentFeatureCollection('seg-large', {
      type: 'LineString',
      coordinates: Array.from({ length: 10_001 }, (_, index) => [100 + index / 100_000, 30]),
    })

    expect(featureCollection.features).toHaveLength(0)
    expect(featureCollection.unavailableReason).toContain('渲染预算')
  })

  it('renders grouped layers and marks meteorology/base placeholders unavailable without fake data', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    render(<LayerGroupControls state={state} layers={layers} onQueryChange={onQueryChange} />)

    expect(screen.getByText('水文图层')).toBeInTheDocument()
    expect(screen.getByText('气象图层')).toBeInTheDocument()
    expect(screen.getByText('基础图层')).toBeInTheDocument()
    expect(screen.getByText('降水格点')).toBeInTheDocument()
    expect(screen.getAllByText('气象格点合同未在 M11 接入')).toHaveLength(2)
    expect(screen.getByText('DEM 合同未在 M11 接入')).toBeInTheDocument()
    expect(screen.getByText('Layer has no valid times.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /洪水重现期/ }))
    expect(onQueryChange).toHaveBeenCalledWith({ layer: 'flood-return-period' })
  })

  it('updates source/scenario query state and exposes best provenance plus compare availability', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    const { rerender } = render(
      <SourceScenarioControls state={{ ...state, source: 'best' }} sourceSelection={sourceSelection} onQueryChange={onQueryChange} />,
    )

    expect(screen.getByTestId('m11-source-provenance')).toHaveTextContent('Best Available (IFS)')
    await user.click(screen.getByRole('button', { name: /GFS \+ IFS 对比/ }))
    expect(onQueryChange).toHaveBeenCalledWith({ source: 'compare' })
    expect(JSON.stringify(onQueryChange.mock.calls)).not.toContain('best_available')
    expect(JSON.stringify(onQueryChange.mock.calls)).not.toContain('forecast_best_available')

    rerender(
      <SourceScenarioControls
        state={{ ...state, source: 'compare' }}
        sourceSelection={{ ...sourceSelection, requestedSource: 'compare', resolvedSource: 'GFS+IFS', comparisonAvailable: false, unavailableReason: 'Comparison requires both GFS and IFS series.' }}
        onQueryChange={onQueryChange}
      />,
    )
    expect(screen.getByTestId('m11-source-provenance')).toHaveTextContent('对比数据不可用')
    expect(screen.getByText('Comparison requires both GFS and IFS series.')).toBeInTheDocument()
  })

  it('selects legends for discharge, flood return period, and warning level semantics', () => {
    const { rerender } = render(<LayerLegendPanel state={state} layers={layers} />)
    expect(screen.getByText('径流量图例')).toBeInTheDocument()
    expect(screen.getByText('<500 m3/s')).toBeInTheDocument()

    rerender(<LayerLegendPanel state={{ ...state, layer: 'flood-return-period' }} layers={layers} />)
    expect(screen.getByText('重现期图例')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()

    rerender(<LayerLegendPanel state={{ ...state, layer: 'warning-level' }} layers={[]} />)
    expect(screen.getByText('预警等级图例')).toBeInTheDocument()
    expect(screen.getByText('高风险')).toBeInTheDocument()
  })

  it('builds timeline state from layer API valid times and corrects stale valid times', () => {
    const staleState = { ...state, validTime: '2026-05-17T00:00:00.000Z' }
    const model = buildM11TimelineViewModel(staleState, layers, null, sourceSelection)

    expect(model.validTimes).toEqual(layers[0].validTimes)
    expect(model.currentValidTime).toBe('2026-05-18T00:00:00.000Z')
    expect(model.sourceKind).toBe('api')
    expect(model.sourceLabel).toContain('/api/v1/layers/{layer_id}/valid-times')
    expect(model.dividerPercent).toBe(50)
    expect(resolveM11ValidTimeCorrection(staleState, layers)).toBe('2026-05-18T00:00:00.000Z')
    expect(resolveM11ValidTimeCorrection({ ...state, layer: 'flood-return-period' }, layers)).toBe('2026-05-18T12:00:00.000Z')
    expect(
      resolveM11ValidTimeCorrection(
        { ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' },
        layers,
      ),
    ).toBeUndefined()
  })

  it('uses payload-derived valid times only when no layer contract applies', () => {
    const model = buildM11TimelineViewModel(
      { ...state, layer: 'warning-level', validTime: null },
      [],
      { label: 'selected segment forecast payload', validTimes: ['2026-05-18T09:00:00Z', '2026-05-18T03:00:00Z'] },
      sourceSelection,
    )

    expect(model.sourceKind).toBe('derived')
    expect(model.validTimes).toEqual(['2026-05-18T03:00:00.000Z', '2026-05-18T09:00:00.000Z'])
    expect(model.currentValidTime).toBe('2026-05-18T09:00:00.000Z')
    expect(model.sourceLabel).toContain('selected segment forecast payload / derived')
  })

  it('disables empty timelines and bounds previous/next controls', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    const emptyLayer = [{ ...layers[0], validTimes: [], currentValidTime: null, available: false, validTimeSource: 'none' as const }]

    const { rerender } = render(<M11Timeline state={state} layers={emptyLayer} onQueryChange={onQueryChange} />)

    expect(screen.getByText('当前图层没有有效时间')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一个有效时刻' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '播放时间轴' })).toBeDisabled()
    expect(screen.getByRole('slider', { name: '有效时间滑块' })).toBeDisabled()

    rerender(<M11Timeline state={state} layers={layers} onQueryChange={onQueryChange} />)
    expect(screen.getByRole('button', { name: '上一个有效时刻' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '下一个有效时刻' }))
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T06:00:00.000Z' })

    rerender(<M11Timeline state={{ ...state, validTime: '2026-05-18T12:00:00.000Z' }} layers={layers} onQueryChange={onQueryChange} />)
    expect(screen.getByRole('button', { name: '下一个有效时刻' })).toBeDisabled()
  })

  it('updates valid time from slider and cleans up bounded playback timers', async () => {
    vi.useFakeTimers()
    const onQueryChange = vi.fn((patch: M11QueryPatch) => {
      currentState = { ...currentState, ...patch }
      rerender(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)
    })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    let currentState = state
    const { rerender, unmount } = render(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)

    fireEvent.change(screen.getByRole('slider', { name: '有效时间滑块' }), { target: { value: '2' } })
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T12:00:00.000Z' })

    currentState = state
    rerender(<M11Timeline state={currentState} layers={layers} onQueryChange={onQueryChange} />)
    fireEvent.click(screen.getByRole('button', { name: '播放时间轴' }))
    expect(vi.getTimerCount()).toBe(1)
    vi.advanceTimersByTime(1000)
    expect(onQueryChange).toHaveBeenCalledWith({ validTime: '2026-05-18T06:00:00.000Z' })
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  describe('met-station cluster layer (M26-3)', () => {
    const stationFeatureCollection = {
      type: 'FeatureCollection' as const,
      features: [
        {
          type: 'Feature' as const,
          geometry: { type: 'Point' as const, coordinates: [100.4, 30.4] as [number, number] },
          properties: { station_id: 'HMT-Y2-0237', station_name: 'Station 0237' },
        },
        {
          type: 'Feature' as const,
          geometry: { type: 'Point' as const, coordinates: [101.6, 30.6] as [number, number] },
          properties: { station_id: 'HMT-Y2-0238', station_name: 'Station 0238' },
        },
      ],
    }
    const metState = { ...state, layer: 'met-stations' as const }

    it('registers a clustered-GeoJSON source with three layers and interactive ids when the met-station layer is on', () => {
      render(<M11MapSurface state={metState} layers={layers} stationFeatureCollection={stationFeatureCollection} />)

      const source = mapSources.find((entry) => entry.id === 'm11-met-stations-source')
      expect(source).toMatchObject({ id: 'm11-met-stations-source', type: 'geojson', cluster: true, promoteId: 'station_id' })
      const layerIds = mapLayers.map((layer) => layer.id)
      expect(layerIds).toEqual(expect.arrayContaining(['clusters', 'cluster-count', 'met-stations-point']))
      expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-met-station-feature-count', '2')
      expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute(
        'data-interactive-layer-ids',
        'met-stations-point,clusters',
      )
    })

    it('does not register the met-station source/layers when the layer is off', () => {
      render(<M11MapSurface state={state} layers={layers} stationFeatureCollection={stationFeatureCollection} />)

      expect(mapSources.find((entry) => entry.id === 'm11-met-stations-source')).toBeUndefined()
      expect(mapLayers.map((layer) => layer.id)).not.toEqual(expect.arrayContaining(['clusters', 'met-stations-point']))
      expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-met-station-feature-count', '0')
    })

    it('does not register the met-station source when the collection is empty', () => {
      render(
        <M11MapSurface
          state={metState}
          layers={layers}
          stationFeatureCollection={{ type: 'FeatureCollection', features: [] }}
        />,
      )

      expect(mapSources.find((entry) => entry.id === 'm11-met-stations-source')).toBeUndefined()
      expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', '')
    })

    it('expands the cluster via getClusterExpansionZoom and flies to it on cluster click', () => {
      render(<M11MapSurface state={metState} layers={layers} stationFeatureCollection={stationFeatureCollection} />)

      fireEvent.drag(screen.getByTestId('mock-maplibre-map'))
      expect(clusterExpansionCalls).toEqual([{ id: 'm11-met-stations-source', clusterId: 7 }])
      expect(flyToCalls).toEqual([{ center: [101.5, 30.5], zoom: 9, duration: 450 }])
    })

    it('dispatches met-station point clicks with station_id for downstream popups', () => {
      const onOverlayClick = vi.fn()
      render(
        <M11MapSurface
          state={metState}
          layers={layers}
          stationFeatureCollection={stationFeatureCollection}
          onOverlayClick={onOverlayClick}
        />,
      )

      fireEvent.drop(screen.getByTestId('mock-maplibre-map'))
      expect(onOverlayClick).toHaveBeenCalledWith(
        expect.objectContaining({
          layerId: 'met-stations',
          feature: expect.objectContaining({ properties: expect.objectContaining({ station_id: 'HMT-Y2-0237' }) }),
        }),
      )
      expect(flyToCalls).toHaveLength(0)
    })

    it('exposes a switchable met-station layer entry in the meteorology group', async () => {
      const onQueryChange = vi.fn()
      const user = userEvent.setup()

      render(<LayerGroupControls state={state} layers={layers} onQueryChange={onQueryChange} />)

      await user.click(screen.getByRole('button', { name: /气象代站/ }))
      expect(onQueryChange).toHaveBeenCalledWith({ layer: 'met-stations' })
    })

    it('shows a honest empty state on the national overview and never fetches without a basin', () => {
      const loadStationLayer = vi.fn().mockResolvedValue(undefined)
      useStationLayerDataStore.setState({
        ...useStationLayerDataStore.getInitialState(),
        loadStationLayer,
        clear: vi.fn(),
      })
      window.history.pushState({}, '', '/?layer=met-stations')

      render(
        <BrowserRouter>
          <OverviewPage />
        </BrowserRouter>,
      )

      expect(screen.getByTestId('m11-met-station-status')).toHaveTextContent('请选择流域以加载气象代站')
      expect(loadStationLayer).not.toHaveBeenCalled()
    })
  })

  describe('useMetStationLayer honest states (M26-3)', () => {
    function Harness(props: Parameters<typeof useMetStationLayer>[0]) {
      const model = useMetStationLayer(props)
      return (
        <div
          data-testid="harness"
          data-status={model.statusNote ?? ''}
          data-feature-count={model.featureCollection?.features.length ?? -1}
          data-truncated={String(model.truncated)}
        />
      )
    }

    it('does not fetch while the source is unresolved (best not yet GFS/IFS)', () => {
      const loadStationLayer = vi.fn().mockResolvedValue(undefined)
      useStationLayerDataStore.setState({
        ...useStationLayerDataStore.getInitialState(),
        loadStationLayer,
        clear: vi.fn(),
      })

      render(<Harness active basinId="heihe" resolvedSource="Unknown" cycle={null} />)

      expect(loadStationLayer).not.toHaveBeenCalled()
      expect(screen.getByTestId('harness').getAttribute('data-status')).toContain('Best Available')
    })

    it('fetches with the basin identity once the source resolves to GFS/IFS', () => {
      const loadStationLayer = vi.fn().mockResolvedValue(undefined)
      useStationLayerDataStore.setState({
        ...useStationLayerDataStore.getInitialState(),
        loadStationLayer,
        clear: vi.fn(),
      })

      render(<Harness active basinId="heihe" resolvedSource="GFS" cycle={null} />)

      expect(loadStationLayer).toHaveBeenCalledWith({ basinId: 'heihe', resolvedSource: 'GFS', cycle: null })
    })

    it('annotates truncation when an oversized basin is capped', () => {
      useStationLayerDataStore.setState({
        ...useStationLayerDataStore.getInitialState(),
        loadStationLayer: vi.fn().mockResolvedValue(undefined),
        clear: vi.fn(),
        data: {
          stations: [],
          total: 12000,
          loaded: 5000,
          truncated: true,
        },
        requestKey: 'heihe::GFS::latest',
      })

      render(<Harness active basinId="heihe" resolvedSource="GFS" cycle={null} />)

      const harness = screen.getByTestId('harness')
      expect(harness).toHaveAttribute('data-truncated', 'true')
      expect(harness.getAttribute('data-status')).toContain('5000/12000')
      expect(harness.getAttribute('data-status')).toContain('截断')
    })
  })
})
