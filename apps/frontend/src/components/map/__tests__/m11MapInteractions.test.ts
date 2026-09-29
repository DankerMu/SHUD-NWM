import { describe, expect, it, vi } from 'vitest'
import type { MapLayerMouseEvent, MapRef } from 'react-map-gl/maplibre'

import {
  createM11HoverSegmentTracker,
  handleM11MapClick,
  resolveM11ClickTarget,
  resolveM11HoveredSegmentId,
  type M11MapOverlayInteraction,
} from '@/components/map/m11MapInteractions'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
  MET_STATION_SOURCE_ID,
} from '@/components/map/m11MapPrimitives'

/**
 * Direct behaviour pin for the product click priority walk
 * (station cluster -> station point [only when stations are shown] -> first
 * overlay hit-layer feature -> basin fill). Written against the pre-extraction
 * handler so the shared click-target resolver provably preserves it.
 */

const OVERLAY = {
  layerId: 'discharge',
  sourceId: 'm11-discharge-source',
  sourceKey: 'k',
  layer: { id: 'm11-discharge-line' },
  source: { type: 'vector', tiles: [], sourceLayer: 'hydro', minzoom: 0, maxzoom: 14, metadata: {} },
} as unknown as M11RegisteredOverlay
const OVERLAY_HIT = 'm11-discharge-line-hit'

function feature(layerId: string, extra: Record<string, unknown> = {}) {
  return { layer: { id: layerId }, properties: { marker: layerId }, geometry: { type: 'Point', coordinates: [101, 31] }, ...extra }
}

function clickEvent(features: unknown[]): MapLayerMouseEvent {
  return {
    features,
    point: { x: 10, y: 20 },
    lngLat: { lng: 101, lat: 31 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  } as unknown as MapLayerMouseEvent
}

function mapRefWith(options: { rendered?: Record<string, unknown[]>; expansionZoom?: number } = {}) {
  const flyTo = vi.fn()
  const queryRenderedFeatures = vi.fn((_point: unknown, query: { layers: string[] }) => options.rendered?.[query.layers[0]] ?? [])
  const getClusterExpansionZoom = vi.fn((_id: number, callback: (error: unknown, zoom: number) => void) => {
    callback(null, options.expansionZoom ?? 9)
  })
  const map = {
    queryRenderedFeatures,
    flyTo,
    getSource: vi.fn((id: string) => (id === MET_STATION_SOURCE_ID ? { getClusterExpansionZoom } : undefined)),
  }
  return { mapRef: { getMap: () => map } as unknown as MapRef, map, flyTo, queryRenderedFeatures }
}

describe('handleM11MapClick product priority walk', () => {
  it('expands a station cluster before anything else and dispatches no overlay click', () => {
    const { mapRef, flyTo } = mapRefWith()
    const onOverlayClick = vi.fn()
    const cluster = feature(MET_STATION_CLUSTER_LAYER_ID, { properties: { cluster_id: 7 } })
    handleM11MapClick(clickEvent([feature(OVERLAY_HIT), cluster, feature(MET_STATION_POINT_LAYER_ID)]), {
      showStationLayer: true,
      renderableOverlay: OVERLAY,
      mapRef,
      onOverlayClick,
    })
    expect(onOverlayClick).not.toHaveBeenCalled()
    expect(flyTo).toHaveBeenCalledWith({ center: [101, 31], zoom: 9, duration: 450 })
  })

  it('selects a station point before the overlay river when stations are shown', () => {
    const { mapRef } = mapRefWith()
    const onOverlayClick = vi.fn()
    const station = feature(MET_STATION_POINT_LAYER_ID)
    handleM11MapClick(clickEvent([feature(OVERLAY_HIT), station]), {
      showStationLayer: true,
      renderableOverlay: OVERLAY,
      mapRef,
      onOverlayClick,
    })
    expect(onOverlayClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick.mock.calls[0][0].layerId).toBe('met-stations')
    expect(onOverlayClick.mock.calls[0][0].feature).toBe(station)
  })

  it('falls back to a direct station-layer query at the click point when the event lacks the station feature', () => {
    const station = feature(MET_STATION_POINT_LAYER_ID)
    const { mapRef, queryRenderedFeatures } = mapRefWith({ rendered: { [MET_STATION_POINT_LAYER_ID]: [station] } })
    const onOverlayClick = vi.fn()
    handleM11MapClick(clickEvent([feature(OVERLAY_HIT)]), {
      showStationLayer: true,
      renderableOverlay: OVERLAY,
      mapRef,
      onOverlayClick,
    })
    expect(queryRenderedFeatures).toHaveBeenCalledWith({ x: 10, y: 20 }, { layers: [MET_STATION_CLUSTER_LAYER_ID] })
    expect(queryRenderedFeatures).toHaveBeenCalledWith({ x: 10, y: 20 }, { layers: [MET_STATION_POINT_LAYER_ID] })
    expect(onOverlayClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick.mock.calls[0][0]).toMatchObject({ layerId: 'met-stations', feature: station })
  })

  it('ignores station features entirely when stations are not shown and selects the first overlay hit feature', () => {
    const { mapRef, queryRenderedFeatures } = mapRefWith()
    const onOverlayClick = vi.fn()
    const first = feature(OVERLAY_HIT, { properties: { river_segment_id: 'first' } })
    const second = feature(OVERLAY_HIT, { properties: { river_segment_id: 'second' } })
    handleM11MapClick(
      clickEvent([feature(MET_STATION_CLUSTER_LAYER_ID), feature(MET_STATION_POINT_LAYER_ID), feature(M11_BASIN_FILL_LAYER_ID), first, second]),
      { showStationLayer: false, renderableOverlay: OVERLAY, mapRef, onOverlayClick },
    )
    expect(queryRenderedFeatures).not.toHaveBeenCalled()
    expect(onOverlayClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick.mock.calls[0][0]).toMatchObject({ layerId: 'discharge' })
    expect(onOverlayClick.mock.calls[0][0].feature).toBe(first)
  })

  it('selects basin fill only when no overlay hit feature is present (or no overlay is renderable)', () => {
    const { mapRef } = mapRefWith()
    const basin = feature(M11_BASIN_FILL_LAYER_ID)
    const withOverlay = vi.fn()
    handleM11MapClick(clickEvent([basin]), { showStationLayer: false, renderableOverlay: OVERLAY, mapRef, onOverlayClick: withOverlay })
    expect(withOverlay).toHaveBeenCalledTimes(1)
    expect(withOverlay.mock.calls[0][0]).toMatchObject({ layerId: 'basin-boundaries', feature: basin })

    const noOverlay = vi.fn()
    handleM11MapClick(clickEvent([feature(OVERLAY_HIT), basin]), { showStationLayer: false, renderableOverlay: null, mapRef, onOverlayClick: noOverlay })
    expect(noOverlay).toHaveBeenCalledTimes(1)
    expect(noOverlay.mock.calls[0][0]).toMatchObject({ layerId: 'basin-boundaries', feature: basin })
  })

  it('dispatches nothing when no interactive feature is at the point', () => {
    const { mapRef } = mapRefWith()
    const onOverlayClick = vi.fn()
    handleM11MapClick(clickEvent([]), { showStationLayer: true, renderableOverlay: OVERLAY, mapRef, onOverlayClick })
    expect(onOverlayClick).not.toHaveBeenCalled()
  })
})

describe('resolveM11ClickTarget (the pure walk shared with the river-click hook)', () => {
  it('returns the same target kinds, in the same priority, from a plain feature list', () => {
    const cluster = feature(MET_STATION_CLUSTER_LAYER_ID)
    const station = feature(MET_STATION_POINT_LAYER_ID)
    const river = feature(OVERLAY_HIT)
    const basin = feature(M11_BASIN_FILL_LAYER_ID)
    const resolve = (features: unknown[], showStationLayer = true) =>
      resolveM11ClickTarget({ features: features as Array<ReturnType<typeof feature>>, showStationLayer, renderableOverlay: OVERLAY })
    expect(resolve([basin, river, station, cluster])).toEqual({ kind: 'station-cluster', feature: cluster })
    expect(resolve([basin, river, station])).toEqual({ kind: 'station', feature: station })
    expect(resolve([basin, river, station], false)).toEqual({ kind: 'overlay', layerId: 'discharge', hitLayerId: OVERLAY_HIT, feature: river })
    expect(resolve([basin, river])).toEqual({ kind: 'overlay', layerId: 'discharge', hitLayerId: OVERLAY_HIT, feature: river })
    expect(resolve([basin])).toEqual({ kind: 'basin', feature: basin })
    expect(resolve([])).toBeNull()
    expect(resolveM11ClickTarget({ features: undefined, showStationLayer: true, renderableOverlay: OVERLAY })).toBeNull()
  })
})

describe('resolveM11HoveredSegmentId', () => {
  const event = clickEvent([])
  function interaction(layerId: M11MapOverlayInteraction['layerId'], properties: Record<string, unknown>): M11MapOverlayInteraction {
    return { layerId, event, feature: feature(OVERLAY_HIT, { properties }) as unknown as M11MapOverlayInteraction['feature'] }
  }

  it('reads river_segment_id, falling back to segment_id, from a discharge hit', () => {
    expect(resolveM11HoveredSegmentId(interaction('discharge', { river_segment_id: 'seg-A', segment_id: 'other' }), OVERLAY)).toBe('seg-A')
    expect(resolveM11HoveredSegmentId(interaction('discharge', { segment_id: 'seg-B' }), OVERLAY)).toBe('seg-B')
    expect(resolveM11HoveredSegmentId(interaction('discharge', { river_segment_id: '', segment_id: 'seg-C' }), OVERLAY)).toBe('seg-C')
  })

  it('returns null for no interaction, missing ids, stations, basins and non-discharge overlays', () => {
    expect(resolveM11HoveredSegmentId(null, OVERLAY)).toBeNull()
    expect(resolveM11HoveredSegmentId(interaction('discharge', { river_segment_id: 42 }), OVERLAY)).toBeNull()
    expect(resolveM11HoveredSegmentId(interaction('discharge', {}), OVERLAY)).toBeNull()
    expect(resolveM11HoveredSegmentId(interaction('met-stations', { segment_id: 'seg-A' }), OVERLAY)).toBeNull()
    expect(resolveM11HoveredSegmentId(interaction('basin-boundaries', { segment_id: 'seg-A' }), OVERLAY)).toBeNull()
    expect(resolveM11HoveredSegmentId(interaction('discharge', { segment_id: 'seg-A' }), null)).toBeNull()
    const nonDischarge = { ...OVERLAY, layerId: 'water-level' } as unknown as M11RegisteredOverlay
    expect(resolveM11HoveredSegmentId(interaction('discharge', { segment_id: 'seg-A' }), nonDischarge)).toBeNull()
  })
})

describe('createM11HoverSegmentTracker', () => {
  it('reports only id changes, including a reset to null', () => {
    const onChange = vi.fn()
    const tracker = createM11HoverSegmentTracker(onChange)
    tracker.update(null)
    expect(onChange).not.toHaveBeenCalled()
    tracker.update('seg-A')
    tracker.update('seg-A')
    tracker.update('seg-A')
    tracker.update('seg-B')
    tracker.update(null)
    tracker.update(null)
    tracker.update('seg-A')
    expect(onChange.mock.calls).toEqual([['seg-A'], ['seg-B'], [null], ['seg-A']])
  })
})
