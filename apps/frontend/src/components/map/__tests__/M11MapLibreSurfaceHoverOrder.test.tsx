import { act, render, screen } from '@testing-library/react'
import type { FeatureCollection } from 'geojson'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import { buildM11RegisteredOverlay, type M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
import { M11OverlayPrimitive, type M11StationFeatureCollection } from '@/components/map/m11MapPrimitives'
import { installMaplibreStubMap, maplibreMapStubProps } from '@/test/maplibreStub'
import type { LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'
import { buildMvtTileUrlTemplate } from '@/lib/mvtLayerMetadata'

vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: MaplibreControlStub,
    ScaleControl: MaplibreControlStub,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

const state: M11QueryState = {
  source: 'best',
  cycle: '2026-09-02T00:00:00Z',
  validTime: null,
  layer: 'discharge',
  metStations: false,
  precip: false,
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  segmentId: null,
  q: null,
}

const freshness: LayerState['freshness'] = {
  updatedAt: null,
  cycleTime: state.cycle,
  validTime: state.cycle,
  runId: null,
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
  source: 'GFS+IFS',
  isStale: false,
  staleAfterHours: 6,
  unavailableReason: null,
}

const dischargeMetadata = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  maplibre_source_layer: 'hydro',
  min_zoom: 0,
  max_zoom: 14,
  valid_times: ['2026-09-02T00:00:00Z'],
  url_template: '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf',
  required_placeholders: ['valid_time', 'variable', 'z', 'x', 'y'],
  source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
} as never

const dischargeLayer: LayerState = {
  layerId: 'discharge',
  displayName: 'Discharge',
  group: 'hydrology',
  available: true,
  metadata: dischargeMetadata,
  validTimes: ['2026-09-02T00:00:00Z'],
  currentValidTime: '2026-09-02T00:00:00Z',
  validTimeSource: 'api',
  disabledReason: null,
  activeNationalCycle: null,
  freshness,
  legend: [],
}

const nationalRiverMetadata = {
  layer_id: 'river-network',
  tile_format: 'mvt',
  maplibre_source_layer: 'river_network',
  min_zoom: 3,
  max_zoom: 10,
  url_template: '/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf',
  required_placeholders: ['z', 'x', 'y'],
  source_refs: {},
  release_blocking: false,
} as never

const nationalRiverLayer: LayerState = {
  ...dischargeLayer,
  layerId: 'river-network',
  displayName: 'River network',
  group: 'base',
  metadata: nationalRiverMetadata,
  validTimes: [],
  currentValidTime: null,
  validTimeSource: 'none',
}

const stations: M11StationFeatureCollection = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [100.2, 30.2] },
      properties: { station_id: 'st-1', station_name: 'S1', basin_id: 'basins_qhh' },
    },
  ],
}

const HIT_LAYER = 'm11-discharge-line-hit'
const EXPECTED_OVERLAY_ORDER = [
  'm11-discharge-line-casing',
  'm11-discharge-line',
  HIT_LAYER,
  'm11-discharge-line-hover-halo',
  'm11-discharge-line-hover-line',
  'm11-discharge-line-selected-halo',
  'm11-discharge-line-selected-line',
]

/** spec 字面：`segmentFilter(id)` 同时匹配 river_segment_id 与 segment_id；null 退化为空串（不命中任何要素）。 */
function expectedSegmentFilter(id: string) {
  return ['any', ['==', ['get', 'river_segment_id'], id], ['==', ['get', 'segment_id'], id]]
}

function riverFeature(segmentId: string) {
  return {
    id: `feature-${segmentId}`,
    layer: { id: HIT_LAYER },
    geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    properties: { basin_id: 'basins_qhh', river_segment_id: segmentId, segment_id: segmentId, basin_version_id: 'bv-001' },
  }
}

function mouseEvent(features: unknown[]) {
  return {
    features,
    point: { x: 40, y: 40 },
    lngLat: { lng: 100.5, lat: 30.5 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  }
}

function move(features: unknown[]) {
  const event = mouseEvent(features)
  act(() => {
    maplibreMapStubProps.current?.onMouseMove?.(event)
  })
  return event
}

function leave() {
  act(() => {
    maplibreMapStubProps.current?.onMouseLeave?.(mouseEvent([]))
  })
}

function surface() {
  return screen.getByTestId('m11-map-surface')
}

function layerNode(id: string) {
  return screen.queryAllByTestId('maplibre-layer').find((node) => node.getAttribute('data-layer-id') === id)
}

function layerFilter(id: string) {
  const raw = layerNode(id)?.getAttribute('data-layer-filter')
  return raw ? JSON.parse(raw) : undefined
}

function layerPaint(id: string) {
  const raw = layerNode(id)?.getAttribute('data-layer-paint')
  return raw ? JSON.parse(raw) : undefined
}

function overlayLayerOrder() {
  return screen
    .queryAllByTestId('maplibre-layer')
    .map((node) => node.getAttribute('data-layer-id'))
    .filter((id): id is string => Boolean(id?.startsWith('m11-discharge-line')))
}

function vectorSourceTiles(sourceId: string): string[] {
  const node = screen
    .queryAllByTestId('maplibre-source')
    .find((candidate) => candidate.getAttribute('data-source-id') === sourceId)
  expect(node?.getAttribute('data-source-type')).toBe('vector')
  return JSON.parse(node?.getAttribute('data-source-tiles') ?? '[]') as string[]
}

beforeEach(() => {
  maplibreMapStubProps.current = null
  installMaplibreStubMap({
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn(() => ({ x: 0, y: 0 })),
    queryRenderedFeatures: vi.fn(() => []),
    getCanvas: () => ({ style: { cursor: '' } }),
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  })
})

describe('M11 MVT sources load through the nhms-mvt retry protocol (#2537)', () => {
  it('prefixes only the tiles handed to <Source>, leaving the builder URLs plain http(s)', () => {
    const layers = [dischargeLayer, nationalRiverLayer]
    render(<M11MapLibreSurface state={state} layers={layers} />)

    const overlay = buildM11RegisteredOverlay(state, layers)
    expect(overlay).not.toBeNull()
    const builderTiles = overlay?.source.tiles ?? []
    expect(builderTiles).toHaveLength(1)
    expect(new URL(builderTiles[0]).protocol).toMatch(/^https?:$/)

    const overlayTiles = vectorSourceTiles('m11-discharge-source')
    expect(overlayTiles).toEqual(builderTiles.map((url) => `nhms-mvt://${url}`))

    const nationalTemplate = buildMvtTileUrlTemplate(nationalRiverMetadata, {})
    expect(new URL(nationalTemplate).protocol).toMatch(/^https?:$/)
    expect(vectorSourceTiles('m11-national-river-source')).toEqual([`nhms-mvt://${nationalTemplate}`])
  })
})

describe('M11OverlayPrimitive hover layers (#2628)', () => {
  const overlay = buildM11RegisteredOverlay(state, [dischargeLayer]) as M11RegisteredOverlay
  const geojsonOverlay = { ...overlay, source: { ...overlay.source, type: 'geojson' } } as unknown as M11RegisteredOverlay
  const geojsonData: FeatureCollection = { type: 'FeatureCollection', features: [] }

  it.each([
    ['vector', overlay, null],
    ['geojson', geojsonOverlay, geojsonData],
  ])('registers hover halo + line between the hit and selected layers on the %s branch', (_label, candidate, data) => {
    render(<M11OverlayPrimitive overlay={candidate} data={data} selectedSegmentId="seg-S" hoveredSegmentId="seg-A" />)
    expect(overlayLayerOrder()).toEqual(EXPECTED_OVERLAY_ORDER)
    expect(layerFilter('m11-discharge-line-hover-halo')).toEqual(expectedSegmentFilter('seg-A'))
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter('seg-A'))
    expect(layerPaint('m11-discharge-line-hover-halo')).toEqual({ 'line-color': '#FFFFFF', 'line-width': 8, 'line-opacity': 0.55 })
    expect(layerPaint('m11-discharge-line-hover-line')).toEqual({ 'line-color': '#22d3ee', 'line-width': 4.5, 'line-opacity': 1 })
    // 选中高亮不受悬停影响，仍按 selectedSegmentId 过滤。
    expect(layerFilter('m11-discharge-line-selected-line')).toEqual(expectedSegmentFilter('seg-S'))
  })

  it.each([
    ['vector', overlay, null],
    ['geojson', geojsonOverlay, geojsonData],
  ])('matches no feature on the %s branch while nothing is hovered', (_label, candidate, data) => {
    render(<M11OverlayPrimitive overlay={candidate} data={data} hoveredSegmentId={null} />)
    expect(layerFilter('m11-discharge-line-hover-halo')).toEqual(expectedSegmentFilter(''))
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter(''))
  })
})

describe('M11MapLibreSurface hover state (#2628)', () => {
  it('follows the pointer across segments and clears on blank map, station, basin and mouseleave', () => {
    const onOverlayHover = vi.fn()
    render(
      <M11MapLibreSurface
        state={{ ...state, metStations: true }}
        layers={[dischargeLayer]}
        stationFeatureCollection={stations}
        onOverlayHover={onOverlayHover}
      />,
    )
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter(''))

    move([riverFeature('seg-A')])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('seg-A')
    expect(layerFilter('m11-discharge-line-hover-halo')).toEqual(expectedSegmentFilter('seg-A'))
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter('seg-A'))

    move([riverFeature('seg-B')])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('seg-B')
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter('seg-B'))

    move([])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter(''))

    move([riverFeature('seg-A')])
    // 站点优先于河段：同一像素压着代站点时不高亮河段。
    move([{ layer: { id: 'met-stations-point' }, properties: { station_id: 'st-1' } }, riverFeature('seg-A')])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')

    move([riverFeature('seg-A')])
    move([{ layer: { id: 'm11-basin-fill' }, properties: { basin_id: 'basins_qhh' } }])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')

    move([riverFeature('seg-A')])
    leave()
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter(''))
  })

  it('still hands every interaction to onOverlayHover (latest-product prefetch) unchanged', () => {
    const onOverlayHover = vi.fn<(interaction: M11MapOverlayInteraction | null) => void>()
    render(<M11MapLibreSurface state={state} layers={[dischargeLayer]} onOverlayHover={onOverlayHover} />)
    const feature = riverFeature('seg-A')
    const first = move([feature])
    move([feature])
    move([feature])
    expect(onOverlayHover).toHaveBeenCalledTimes(3)
    expect(onOverlayHover.mock.calls[0][0]).toEqual({ layerId: 'discharge', event: first, feature })
    // 同一河段内连续移动：悬停观测面保持不变。
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('seg-A')
    move([])
    expect(onOverlayHover).toHaveBeenLastCalledWith(null)
    leave()
    expect(onOverlayHover).toHaveBeenCalledTimes(5)
    expect(onOverlayHover).toHaveBeenLastCalledWith(null)
  })

  it('resets on overlay switch so hovering the same segment after switching back highlights again', () => {
    const { rerender } = render(<M11MapLibreSurface state={state} layers={[dischargeLayer]} />)
    move([riverFeature('seg-A')])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('seg-A')

    // 活动图层失去可注册的 overlay（图层切走 / 不可用 ⇒ renderableOverlay 为 null）：hover 必须置空。
    rerender(<M11MapLibreSurface state={state} layers={[{ ...dischargeLayer, available: false }]} />)
    expect(overlayLayerOrder()).toEqual([])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')

    rerender(<M11MapLibreSurface state={state} layers={[dischargeLayer]} />)
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('')
    move([riverFeature('seg-A')])
    expect(surface().getAttribute('data-hovered-segment-id')).toBe('seg-A')
    expect(layerFilter('m11-discharge-line-hover-line')).toEqual(expectedSegmentFilter('seg-A'))
  })
})

describe('M11MapLibreSurface onStyleData keeps station layers on top (#2650)', () => {
  it('wires onStyleData to reorder the map it is handed', () => {
    render(
      <M11MapLibreSurface
        state={{ ...state, metStations: true }}
        layers={[dischargeLayer]}
        stationFeatureCollection={stations}
      />,
    )
    const onStyleData = maplibreMapStubProps.current?.onStyleData
    expect(typeof onStyleData).toBe('function')

    const stationIds = ['clusters', 'cluster-count', 'met-stations-point', 'met-stations-selected-halo', 'met-stations-selected-point']
    // overlay 重挂载后被追加到代站之上（#2650 的栈序）。
    const order = ['background', ...stationIds, ...EXPECTED_OVERLAY_ORDER]
    const map = {
      getLayersOrder: vi.fn(() => [...order]),
      getLayer: vi.fn((id: string) => (order.includes(id) ? { id } : undefined)),
      moveLayer: vi.fn((id: string) => {
        order.splice(order.indexOf(id), 1)
        order.push(id)
      }),
    }
    act(() => {
      onStyleData?.({ type: 'styledata', dataType: 'style', target: map })
    })
    expect(order).toEqual(['background', ...EXPECTED_OVERLAY_ORDER, ...stationIds])

    act(() => {
      onStyleData?.({ type: 'styledata', dataType: 'style', target: map })
    })
    expect(map.moveLayer).toHaveBeenCalledTimes(stationIds.length)
  })
})
