import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MapLayerMouseEvent, MapRef } from 'react-map-gl/maplibre'

import { M11MapLibreSurface } from '@/components/map/M11MapLibreSurface'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import { handleM11MapClick } from '@/components/map/m11MapInteractions'
import { M11_BASIN_FILL_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID, MET_STATION_POINT_LAYER_ID } from '@/components/map/m11MapPrimitives'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { installMaplibreStubMap } from '@/test/maplibreStub'

/**
 * 地图点击的独立上报通路（openspec mobile-responsive-display task 3.2）：地图上的每一次点击都
 * 通知一次 `onMapClick`，无论是否命中要素；`onOverlayClick` 的既有调用条件不变。
 */
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

const OVERLAY = {
  layerId: 'discharge',
  sourceId: 'm11-discharge-source',
  sourceKey: 'k',
  layer: { id: 'm11-discharge-line' },
  source: { type: 'vector', tiles: [], sourceLayer: 'hydro', minzoom: 0, maxzoom: 14, metadata: {} },
} as unknown as M11RegisteredOverlay
const OVERLAY_HIT = 'm11-discharge-line-hit'

function feature(layerId: string, properties: Record<string, unknown> = {}) {
  return { layer: { id: layerId }, properties, geometry: { type: 'Point', coordinates: [101, 31] } }
}

function clickEvent(features: unknown[]): MapLayerMouseEvent {
  return {
    features,
    point: { x: 10, y: 20 },
    lngLat: { lng: 101, lat: 31 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  } as unknown as MapLayerMouseEvent
}

const mapRef = {
  getMap: () => ({ queryRenderedFeatures: () => [], flyTo: vi.fn(), getSource: () => undefined }),
} as unknown as MapRef

describe('handleM11MapClick map-click notification', () => {
  it('notifies once on a click that hits nothing, and dispatches no overlay click', () => {
    const onMapClick = vi.fn()
    const onOverlayClick = vi.fn()
    handleM11MapClick(clickEvent([]), { showStationLayer: true, renderableOverlay: OVERLAY, mapRef, onOverlayClick, onMapClick })

    expect(onMapClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick).not.toHaveBeenCalled()
  })

  it.each([
    ['a river segment', OVERLAY_HIT, 'discharge'],
    ['a station', MET_STATION_POINT_LAYER_ID, 'met-stations'],
    ['a basin', M11_BASIN_FILL_LAYER_ID, 'basin-boundaries'],
  ])('notifies once on a click that hits %s, and the overlay click still fires for it', (_name, layerId, expectedLayer) => {
    const onMapClick = vi.fn()
    const onOverlayClick = vi.fn()
    const hit = feature(layerId)
    handleM11MapClick(clickEvent([hit]), { showStationLayer: true, renderableOverlay: OVERLAY, mapRef, onOverlayClick, onMapClick })

    expect(onMapClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick.mock.calls[0][0]).toMatchObject({ layerId: expectedLayer, feature: hit })
  })

  it('notifies once on a station-cluster click, which still dispatches no overlay click', () => {
    const onMapClick = vi.fn()
    const onOverlayClick = vi.fn()
    handleM11MapClick(clickEvent([feature(MET_STATION_CLUSTER_LAYER_ID, { cluster_id: 7 })]), {
      showStationLayer: true,
      renderableOverlay: OVERLAY,
      mapRef,
      onOverlayClick,
      onMapClick,
    })

    expect(onMapClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick).not.toHaveBeenCalled()
  })
})

describe('M11MapLibreSurface map-click notification', () => {
  beforeEach(() => {
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

  afterEach(() => {
    delete (window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent
  })

  it('forwards a featureless map click to onMapClick and not to onOverlayClick', () => {
    const onMapClick = vi.fn()
    const onOverlayClick = vi.fn()
    render(
      <M11MapLibreSurface
        state={defaultM11QueryState}
        layers={[]}
        loading={false}
        boundaryLoading={false}
        onMapClick={onMapClick}
        onOverlayClick={onOverlayClick}
      />,
    )
    ;(window as unknown as { __nhmsOrdinaryMapClickEvent?: unknown }).__nhmsOrdinaryMapClickEvent = clickEvent([])
    fireEvent.click(screen.getByTestId('mock-maplibre-ordinary-click'))

    expect(onMapClick).toHaveBeenCalledTimes(1)
    expect(onOverlayClick).not.toHaveBeenCalled()
  })
})
