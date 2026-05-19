import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { forwardRef, useImperativeHandle, type ReactNode } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { m11VisualTokens } from '@/lib/m11/visualTokens'
import {
  normalizeLayerStates,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
  type SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryPatch, type M11QueryState } from '@/lib/m11/queryState'
import {
  m11BasinRiverCollectionBudget,
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
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'

const mapSources: Array<Record<string, unknown>> = []
const mapLayers: Array<Record<string, unknown>> = []
const fitBoundsCalls: Array<unknown[]> = []
const flyToCalls: Array<unknown> = []

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
        layer: { id: 'm11-flood-return-period-line' },
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
      useImperativeHandle(ref, () => ({
        fitBounds: (...args: unknown[]) => fitBoundsCalls.push(args),
        flyTo: (args: unknown) => flyToCalls.push(args),
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
    freshness: { ...freshness, validTime: '2026-05-18T12:00:00.000Z' },
    legend: [{ label: 'warning', color: '#FFB74D', min: 10, max: 20 }],
  },
  {
    layerId: 'water-level',
    displayName: 'Water level',
    group: 'hydrology',
    available: false,
    validTimes: [],
    currentValidTime: null,
    validTimeSource: 'none',
    disabledReason: 'Layer has no valid times.',
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

function oversizedStreamResponse(maxBytes: number) {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('x'.repeat(maxBytes + 1)))
        controller.close()
      },
    }),
    { headers: { 'content-type': 'application/json' } },
  )
}

describe('M11 visual foundation shell', () => {
  beforeEach(() => {
    mapSources.length = 0
    mapLayers.length = 0
    fitBoundsCalls.length = 0
    flyToCalls.length = 0
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async () => geoJsonResponse({ type: 'FeatureCollection', features: [] })),
    )
    useOverviewDataStore.setState({
      ...useOverviewDataStore.getInitialState(),
      loadOverview: vi.fn().mockResolvedValue(undefined),
      loadBasinDetail: vi.fn().mockResolvedValue(undefined),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('exposes mapped layout tokens for nav, panels, timeline, and warning colors', () => {
    window.history.pushState({}, '', '/overview?warningLevel=major')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    const shell = screen.getByTestId('m11-shell')
    expect(shell).toHaveStyle({
      '--m11-left-panel-width': '280px',
      '--m11-right-panel-width': '340px',
      '--m11-timeline-height': '64px',
    })
    expect(shell).toHaveAttribute('data-layout', 'map-first-compact')
    expect(shell.className).toContain('h-[calc(100vh-var(--m11-nav-height)-32px)]')
    expect(shell.className).toContain('min-[1200px]:grid-rows-[minmax(0,1fr)_var(--m11-timeline-height)]')
    expect(shell).toHaveAttribute('data-left-panel', 'expanded')
    expect(shell).toHaveAttribute('data-right-panel', 'expanded')
    expect(m11VisualTokens.navHeight).toBe('56px')
    expect(m11VisualTokens.warningLevels.major).toBe('#FF8A65')
    expect(screen.getByLabelText('M11 左侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 右侧面板')).toBeInTheDocument()
    expect(screen.getByLabelText('M11 时间轴')).toBeInTheDocument()
    expect(screen.getByTestId('m11-timeline')).toHaveAttribute('data-first-viewport-visible', 'true')
    expect(screen.getByTestId('m11-timeline-region')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '折叠左侧面板' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: '折叠右侧面板' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('collapses side panels while keeping the timeline mounted for 1280 compact layout', async () => {
    window.history.pushState({}, '', '/overview')

    render(
      <BrowserRouter>
        <OverviewPage />
      </BrowserRouter>,
    )

    const user = userEvent.setup()
    const shell = screen.getByTestId('m11-shell')

    await user.click(screen.getByRole('button', { name: '折叠左侧面板' }))
    expect(shell).toHaveAttribute('data-left-panel', 'collapsed')
    expect(screen.getByRole('button', { name: '展开左侧面板' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('m11-timeline')).toHaveAttribute('data-first-viewport-visible', 'true')

    await user.click(screen.getByRole('button', { name: '折叠右侧面板' }))
    expect(shell).toHaveAttribute('data-right-panel', 'collapsed')
    expect(screen.getByRole('button', { name: '展开右侧面板' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByLabelText('M11 时间轴')).toBeInTheDocument()
  })

  it('keeps default discharge unregistered without basin river geometry while preserving controls and unavailable map status', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    const { rerender } = render(<M11MapSurface state={state} layers={layers} onQueryChange={onQueryChange} />)

    const surface = screen.getByTestId('m11-map-surface')
    expect(surface).toHaveAttribute('data-basemap', 'vector')
    expect(surface).not.toHaveAttribute('data-registered-overlays')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', '')
    expect(screen.getByTestId('m11-map-unavailable')).toHaveTextContent('地图源尚未在本仓库实现')
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

  it('registers validated flood return period geojson and keeps it through basemap switches using selected URL valid time', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()
    const floodState = { ...state, layer: 'flood-return-period' as const, validTime: '2026-05-18T06:00:00.000Z' }

    const { rerender } = render(<M11MapSurface state={floodState} layers={layers} onQueryChange={onQueryChange} />)

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', 'm11-flood-return-period-line')
    expect(mapSources.at(-1)).toMatchObject({
      id: 'm11-flood-return-period-source',
      type: 'geojson',
      promoteId: 'feature_id',
      data: { type: 'FeatureCollection', features: [] },
    })
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('valid_time=2026-05-18T06%3A00%3A00.000Z'),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(vi.mocked(fetch).mock.calls.map(([url]) => String(url)).join('\n')).not.toContain(
      'valid_time=2026-05-18T12%3A00%3A00.000Z',
    )
    expect(mapLayers.at(-1)).toMatchObject({ id: 'm11-flood-return-period-line', source: 'm11-flood-return-period-source' })

    await user.click(screen.getByRole('button', { name: '地形底图' }))
    expect(onQueryChange).toHaveBeenCalledWith({ basemap: 'terrain' })

    mapSources.length = 0
    mapLayers.length = 0
    rerender(<M11MapSurface state={{ ...floodState, basemap: 'terrain' }} layers={layers} onQueryChange={onQueryChange} />)
    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    expect(mapSources.at(-1)).toMatchObject({ id: 'm11-flood-return-period-source', type: 'geojson' })
    expect(mapLayers.at(-1)).toMatchObject({ id: 'm11-flood-return-period-line' })
  })

  it('renders basin river network from segment rows and colors by active hydrology layer', async () => {
    const onOverlayHover = vi.fn()
    const onOverlayClick = vi.fn()

    const { rerender } = render(
      <M11MapSurface
        state={state}
        layers={layers}
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

    render(
      <M11MapSurface
        state={state}
        layers={layers}
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

  it('rejects oversized M11 flood return period payloads before registering a MapLibre source', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        geoJsonResponse({
          type: 'FeatureCollection',
          features: new Array(10_001).fill({ type: 'Feature', properties: {}, geometry: null }),
        }),
      ),
    )

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
      />,
    )

    expect(await screen.findByTestId('m11-map-unavailable')).toHaveTextContent('超过客户端要素预算')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('rejects oversized streamed M11 flood return period payloads without registering a MapLibre source', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(oversizedStreamResponse(2_000_000)))

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
      />,
    )

    expect(await screen.findByTestId('m11-map-unavailable')).toHaveTextContent('超过客户端序列化预算')
    expect(screen.getByTestId('m11-map-surface')).not.toHaveAttribute('data-registered-overlays')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('threads camera and overlay callbacks into the MapLibre primitive', async () => {
    const onOverlayHover = vi.fn()
    const onOverlayClick = vi.fn()

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
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

  it('dispatches the matched basin feature when overlay features are returned first', async () => {
    const onOverlayClick = vi.fn()

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
        basins={overviewBasins}
        onOverlayClick={onOverlayClick}
      />,
    )

    await waitFor(() => expect(screen.getByTestId('m11-map-surface')).toHaveAttribute('data-registered-overlays', 'flood-return-period'))
    fireEvent.contextMenu(screen.getByTestId('mock-maplibre-map'))
    expect(onOverlayClick).toHaveBeenCalledWith(
      expect.objectContaining({
        layerId: 'basin-boundaries',
        feature: expect.objectContaining({ properties: { basin_id: 'yangtze' } }),
      }),
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

  it('does not advertise or register unavailable selected overlays', () => {
    render(<M11MapSurface state={{ ...state, layer: 'water-level' }} layers={layers} />)

    const surface = screen.getByTestId('m11-map-surface')
    expect(surface).not.toHaveAttribute('data-registered-overlays')
    expect(surface).not.toHaveAttribute('data-active-overlays')
    expect(screen.getByTestId('m11-map-unavailable')).toHaveTextContent('Layer has no valid times.')
    expect(screen.getByTestId('mock-maplibre-map')).toHaveAttribute('data-interactive-layer-ids', '')
    expect(mapSources).toHaveLength(0)
    expect(mapLayers).toHaveLength(0)
  })

  it('shows a scoped map source error while keeping other controls usable', async () => {
    const onQueryChange = vi.fn()
    const user = userEvent.setup()

    render(
      <M11MapSurface
        state={{ ...state, layer: 'flood-return-period', validTime: '2026-05-18T06:00:00.000Z' }}
        layers={layers}
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
      expect.arrayContaining(['m11-basin-fill', 'm11-basin-outline', 'm11-basin-label']),
    )

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
    expect(resolveM11ValidTimeCorrection({ ...state, layer: 'water-level' }, layers)).toBeNull()
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
})
