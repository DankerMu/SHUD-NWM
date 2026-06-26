import type { FeatureCollection } from 'geojson'
import type { FilterSpecification } from 'maplibre-gl'
import { Layer, Marker, Source, type LayerProps } from 'react-map-gl/maplibre'

import type { FloodReturnPeriodFeatureCollection } from '@/lib/floodReturnPeriodGeoJson'
import {
  m11BasinLabelAnchor,
  segmentFilter,
  zoomScaledValueWidth,
  type BasinFeatureCollection,
  type BasinRiverFeatureCollection,
  type M11RegisteredOverlay,
  type SelectedSegmentFeatureCollection,
} from '@/components/map/m11MapBuilders'

export const MET_STATION_SOURCE_ID = 'm11-met-stations-source'
export const MET_STATION_CLUSTER_LAYER_ID = 'clusters'
export const MET_STATION_CLUSTER_COUNT_LAYER_ID = 'cluster-count'
export const MET_STATION_POINT_LAYER_ID = 'met-stations-point'

export const M11_NATIONAL_RIVER_SOURCE_ID = 'm11-national-river-source'
export const M11_NATIONAL_RIVER_LINE_LAYER_ID = 'm11-national-river-line'
export const M11_BASIN_BOUNDARIES_SOURCE_ID = 'm11-basin-boundaries-source'
export const M11_BASIN_FILL_LAYER_ID = 'm11-basin-fill'
export const M11_BASIN_OUTLINE_LAYER_ID = 'm11-basin-outline'
export const M11_BASIN_RIVER_SOURCE_ID = 'm11-basin-river-source'
export const M11_BASIN_RIVER_CASING_LAYER_ID = 'm11-basin-river-casing'
export const M11_BASIN_RIVER_LINE_LAYER_ID = 'm11-basin-river-line'
export const M11_BASIN_RIVER_HOVER_HALO_LAYER_ID = 'm11-basin-river-hover-halo'
export const M11_BASIN_RIVER_SELECTED_HALO_LAYER_ID = 'm11-basin-river-selected-halo'
export const M11_BASIN_RIVER_HOVER_LINE_LAYER_ID = 'm11-basin-river-hover-line'
export const M11_BASIN_RIVER_SELECTED_LINE_LAYER_ID = 'm11-basin-river-selected-line'
export const M11_SELECTED_SEGMENT_SOURCE_ID = 'm11-selected-segment-source'
export const M11_SELECTED_SEGMENT_HALO_LAYER_ID = 'm11-selected-segment-halo'
export const M11_SELECTED_SEGMENT_LINE_LAYER_ID = 'm11-selected-segment-line'
export const M11_ROUND_LINE_LAYOUT = { 'line-cap': 'round', 'line-join': 'round' } as const

const M11_OVERLAY_HIT_PAINT: LayerProps['paint'] = {
  'line-color': '#000000',
  'line-opacity': 0,
  'line-width': 16,
}

export interface M11StationFeatureCollection {
  type: 'FeatureCollection'
  features: Array<{
    type: 'Feature'
    geometry: { type: 'Point'; coordinates: [number, number] }
    properties: { station_id: string; station_name: string | null; basin_id: string | null }
  }>
}

export function m11RegisteredOverlayHitLayerId(overlay: M11RegisteredOverlay): string {
  return `${overlay.layer.id}-hit`
}

export function M11OverlayPrimitive({
  overlay,
  data,
  selectedSegmentId,
}: {
  overlay: M11RegisteredOverlay
  data: FloodReturnPeriodFeatureCollection | null
  selectedSegmentId?: string | null
}) {
  const isLine = overlay.layer.type === 'line'
  const sourceLayerProp = overlay.layer['source-layer'] ? { 'source-layer': overlay.layer['source-layer'] } : {}
  const casingLayer = isLine
    ? {
        id: `${overlay.layer.id}-casing`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        paint: m11OverlayCasingPaint(),
      }
    : null
  const hitLayer = isLine
    ? {
        id: m11RegisteredOverlayHitLayerId(overlay),
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        paint: M11_OVERLAY_HIT_PAINT,
      }
    : null
  const mainLayer = isLine ? { ...overlay.layer, layout: M11_ROUND_LINE_LAYOUT } : overlay.layer
  const selectedHaloLayer = isLine
    ? {
        id: `${overlay.layer.id}-selected-halo`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        filter: segmentFilter(selectedSegmentId),
        paint: {
          'line-color': '#FFFFFF',
          'line-width': 10,
          'line-opacity': 0.78,
        },
      }
    : null
  const selectedLineLayer = isLine
    ? {
        id: `${overlay.layer.id}-selected-line`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        filter: segmentFilter(selectedSegmentId),
        paint: {
          'line-color': '#F97316',
          'line-width': 6,
          'line-opacity': 1,
        },
      }
    : null
  if (overlay.source.type === 'vector') {
    return (
      <Source
        key={overlay.sourceKey}
        id={overlay.sourceId}
        type="vector"
        tiles={overlay.source.tiles}
        minzoom={overlay.source.minzoom}
        maxzoom={overlay.source.maxzoom}
        promoteId="feature_id"
      >
        {casingLayer ? <Layer {...casingLayer} /> : null}
        <Layer {...mainLayer} />
        {hitLayer ? <Layer {...hitLayer} /> : null}
        {selectedHaloLayer ? <Layer {...selectedHaloLayer} /> : null}
        {selectedLineLayer ? <Layer {...selectedLineLayer} /> : null}
      </Source>
    )
  }
  if (!data) return null
  return (
    <Source id={overlay.sourceId} type="geojson" data={data} promoteId="feature_id">
      {casingLayer ? <Layer {...casingLayer} /> : null}
      <Layer {...mainLayer} />
      {hitLayer ? <Layer {...hitLayer} /> : null}
      {selectedHaloLayer ? <Layer {...selectedHaloLayer} /> : null}
      {selectedLineLayer ? <Layer {...selectedLineLayer} /> : null}
    </Source>
  )
}

export function m11NationalRiverPaint({ dimmed, satellite }: { dimmed: boolean; satellite: boolean }): LayerProps['paint'] {
  const fadeAt = (zoomFade: number) => (dimmed ? zoomFade : 1)
  const fade5 = fadeAt(0.85)
  const fade7 = fadeAt(0.45)
  const fade9 = fadeAt(0.35)
  return {
    'line-color': [
      'interpolate',
      ['linear'],
      ['get', 'Type'],
      1,
      satellite ? '#9fe0ff' : '#9cc7e8',
      3,
      satellite ? '#5fc3f2' : '#3f88c5',
      5,
      satellite ? '#2196d8' : '#14487f',
    ],
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      3,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 0.3, 5, 1.4],
      7,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 0.8, 5, 2.6],
      12,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 1.6, 5, 4.5],
    ],
    'line-opacity': [
      'interpolate',
      ['linear'],
      ['zoom'],
      3,
      ['match', ['get', 'Type'], 5, 0.9, 4, 0.55, 0],
      5,
      ['match', ['get', 'Type'], 5, 0.95 * fade5, 4, 0.85 * fade5, 3, 0.55 * fade5, 0],
      7,
      ['match', ['get', 'Type'], 5, 1 * fade7, 4, 0.95 * fade7, 3, 0.85 * fade7, 2, 0.6 * fade7, 0],
      9,
      0.9 * fade9,
    ],
  }
}

export function M11NationalRiverPrimitive({
  collection,
  dimmed,
  satellite,
}: {
  collection: FeatureCollection
  dimmed: boolean
  satellite: boolean
}) {
  return (
    <Source id={M11_NATIONAL_RIVER_SOURCE_ID} type="geojson" data={collection}>
      <Layer
        id={M11_NATIONAL_RIVER_LINE_LAYER_ID}
        type="line"
        source={M11_NATIONAL_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        paint={m11NationalRiverPaint({ dimmed, satellite })}
      />
    </Source>
  )
}

export function M11BasinPrimitive({ collection }: { collection: BasinFeatureCollection }) {
  return (
    <Source id={M11_BASIN_BOUNDARIES_SOURCE_ID} type="geojson" data={collection} promoteId="basin_id">
      <Layer
        id={M11_BASIN_FILL_LAYER_ID}
        type="fill"
        source={M11_BASIN_BOUNDARIES_SOURCE_ID}
        paint={{
          'fill-color': '#1E88E5',
          'fill-opacity': 0.14,
        }}
      />
      <Layer
        id={M11_BASIN_OUTLINE_LAYER_ID}
        type="line"
        source={M11_BASIN_BOUNDARIES_SOURCE_ID}
        paint={{
          'line-color': '#0F3460',
          'line-width': 1.4,
          'line-opacity': 0.72,
        }}
      />
    </Source>
  )
}

export function M11BasinLabelMarkers({ collection }: { collection: BasinFeatureCollection }) {
  return (
    <>
      {collection.features.map((feature) => {
        const anchor = m11BasinLabelAnchor(feature.geometry)
        if (!anchor) return null
        return (
          <Marker key={feature.properties.basin_id} longitude={anchor[0]} latitude={anchor[1]} anchor="center" style={{ pointerEvents: 'none' }}>
            <span
              className="pointer-events-none select-none rounded-full border border-white/60 bg-white/80 px-2.5 py-0.5 text-xs font-semibold text-primary-700 shadow-sm backdrop-blur-sm"
              data-testid="m11-basin-label"
              data-basin-id={feature.properties.basin_id}
            >
              {feature.properties.basin_name}
            </span>
          </Marker>
        )
      })}
    </>
  )
}

export function M11BasinRiverPrimitive({
  collection,
  selectedSegmentId,
  hoveredSegmentId,
  subdued = false,
}: {
  collection: BasinRiverFeatureCollection
  selectedSegmentId?: string | null
  hoveredSegmentId?: string | null
  subdued?: boolean
}) {
  return (
    <Source id={M11_BASIN_RIVER_SOURCE_ID} type="geojson" data={collection.sourceData} promoteId="river_segment_id">
      <Layer
        id={M11_BASIN_RIVER_CASING_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2.6, 9, 3.8, 12, 5.2],
          'line-opacity': subdued ? 0.25 : 0.8,
        }}
      />
      <Layer
        id={M11_BASIN_RIVER_LINE_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        paint={{
          'line-color': ['get', 'layer_color'],
          'line-width': ['interpolate', ['linear'], ['zoom'], 6, 1.6, 9, 2.6, 12, 3.6],
          'line-opacity': subdued ? 0.18 : 0.92,
        }}
      />
      <Layer
        id={M11_BASIN_RIVER_HOVER_HALO_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(hoveredSegmentId)}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 8.5,
          'line-opacity': 0.62,
        }}
      />
      <Layer
        id={M11_BASIN_RIVER_SELECTED_HALO_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(selectedSegmentId)}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 9.5,
          'line-opacity': 0.68,
        }}
      />
      <Layer
        id={M11_BASIN_RIVER_HOVER_LINE_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(hoveredSegmentId)}
        paint={{
          'line-color': ['get', 'layer_color'],
          'line-width': 4.8,
          'line-opacity': 0.98,
        }}
      />
      <Layer
        id={M11_BASIN_RIVER_SELECTED_LINE_LAYER_ID}
        type="line"
        source={M11_BASIN_RIVER_SOURCE_ID}
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(selectedSegmentId)}
        paint={{
          'line-color': '#F97316',
          'line-width': 5.5,
          'line-opacity': 1,
        }}
      />
    </Source>
  )
}

export function M11SelectedSegmentPrimitive({ collection }: { collection: SelectedSegmentFeatureCollection }) {
  return (
    <Source id={M11_SELECTED_SEGMENT_SOURCE_ID} type="geojson" data={collection} promoteId="segment_id">
      <Layer
        id={M11_SELECTED_SEGMENT_HALO_LAYER_ID}
        type="line"
        source={M11_SELECTED_SEGMENT_SOURCE_ID}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 8,
          'line-opacity': 0.7,
        }}
      />
      <Layer
        id={M11_SELECTED_SEGMENT_LINE_LAYER_ID}
        type="line"
        source={M11_SELECTED_SEGMENT_SOURCE_ID}
        paint={{
          'line-color': '#F97316',
          'line-width': 5,
          'line-opacity': 0.95,
        }}
      />
    </Source>
  )
}

export function M11StationClusterPrimitive({
  collection,
  selectedStationId,
}: {
  collection: M11StationFeatureCollection
  selectedStationId?: string | null
}) {
  return (
    <Source
      id={MET_STATION_SOURCE_ID}
      type="geojson"
      data={collection}
      cluster
      clusterRadius={50}
      clusterMaxZoom={14}
      promoteId="station_id"
    >
      <Layer
        id={MET_STATION_CLUSTER_LAYER_ID}
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={['has', 'point_count']}
        paint={{
          'circle-color': ['step', ['get', 'point_count'], '#90CAF9', 25, '#42A5F5', 100, '#1E88E5'],
          'circle-radius': ['step', ['get', 'point_count'], 14, 25, 18, 100, 24],
          'circle-opacity': 0.85,
          'circle-stroke-color': '#FFFFFF',
          'circle-stroke-width': 1.5,
        }}
      />
      <Layer
        id={MET_STATION_CLUSTER_COUNT_LAYER_ID}
        type="symbol"
        source={MET_STATION_SOURCE_ID}
        filter={['has', 'point_count']}
        layout={{
          'text-field': ['get', 'point_count_abbreviated'],
          'text-size': 12,
          'text-allow-overlap': true,
        }}
        paint={{ 'text-color': '#0A1929' }}
      />
      <Layer
        id={MET_STATION_POINT_LAYER_ID}
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={['!', ['has', 'point_count']]}
        paint={{
          'circle-color': '#F97316',
          'circle-radius': 6,
          'circle-opacity': 0.92,
          'circle-stroke-color': '#FFFFFF',
          'circle-stroke-width': 1.5,
        }}
      />
      <Layer
        id="met-stations-selected-halo"
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={stationFilter(selectedStationId)}
        paint={{
          'circle-color': '#FFFFFF',
          'circle-radius': 12,
          'circle-opacity': 0.82,
          'circle-stroke-color': '#111827',
          'circle-stroke-width': 1,
        }}
      />
      <Layer
        id="met-stations-selected-point"
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={stationFilter(selectedStationId)}
        paint={{
          'circle-color': '#FACC15',
          'circle-radius': 7.5,
          'circle-opacity': 1,
          'circle-stroke-color': '#111827',
          'circle-stroke-width': 2,
        }}
      />
    </Source>
  )
}

function m11OverlayCasingPaint(): LayerProps['paint'] {
  const valueStops = [-2, 3, 0, 3.6, 2, 4.6, 4, 6.2, 4.7, 8.2]
  return {
    'line-color': '#FFFFFF',
    'line-opacity': 0.85,
    'line-width': zoomScaledValueWidth(valueStops, 0.4, true),
  }
}

function stationFilter(stationId?: string | null): FilterSpecification {
  return [
    'all',
    ['!', ['has', 'point_count']],
    ['==', ['get', 'station_id'], stationId ?? ''],
  ] as FilterSpecification
}
