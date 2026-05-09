import {
  Layer,
  Source,
  type GeoJSONSourceRaw,
  type LayerProps,
} from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'

export const RIVER_SOURCE_ID = 'demo-rivers'
export const RIVER_LAYER_ID = 'demo-rivers-line'
export const RIVER_HOVER_LAYER_ID = 'demo-rivers-hover'
export const RIVER_SELECTED_LAYER_ID = 'demo-rivers-selected'

export interface RiverFeatureProperties {
  segment_id: string
  name: string
  stream_order: number
  basin_version_id: string
  river_network_version_id: string
}

export interface RiverFeature {
  type: 'Feature'
  properties: RiverFeatureProperties
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
}

export interface RiverFeatureCollection {
  type: 'FeatureCollection'
  features: RiverFeature[]
}

export const demoRivers: RiverFeatureCollection = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'yangtze_v12_riv_000001',
        name: '长江干流上段',
        stream_order: 5,
        basin_version_id: 'yangtze_v12',
        river_network_version_id: 'yangtze_v12_rivnet',
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [104.1, 30.7],
          [106.2, 30.5],
          [108.4, 30.7],
        ],
      },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'yangtze_v12_riv_000002',
        name: '长江干流中段',
        stream_order: 6,
        basin_version_id: 'yangtze_v12',
        river_network_version_id: 'yangtze_v12_rivnet',
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [108.4, 30.7],
          [111.0, 30.4],
          [113.4, 30.6],
        ],
      },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'yangtze_v12_riv_000003',
        name: '汉江支流',
        stream_order: 4,
        basin_version_id: 'yangtze_v12',
        river_network_version_id: 'yangtze_v12_rivnet',
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [110.0, 32.6],
          [111.2, 31.8],
          [112.4, 30.8],
        ],
      },
    },
  ],
}

interface RiverLayerProps {
  data: RiverFeatureCollection
  hoveredSegmentId?: string | null
  selectedSegmentId?: string | null
}

const riverLayer: LayerProps = {
  id: RIVER_LAYER_ID,
  type: 'line',
  source: RIVER_SOURCE_ID,
  paint: {
    'line-color': '#0f8fbf',
    'line-width': ['interpolate', ['linear'], ['get', 'stream_order'], 1, 2, 6, 6],
    'line-opacity': 0.86,
  },
}

function segmentFilter(segmentId?: string | null): FilterSpecification {
  return ['==', ['get', 'segment_id'], segmentId ?? ''] as FilterSpecification
}

export function RiverLayer({ data, hoveredSegmentId, selectedSegmentId }: RiverLayerProps) {
  const hoverLayer: LayerProps = {
    id: RIVER_HOVER_LAYER_ID,
    type: 'line',
    source: RIVER_SOURCE_ID,
    filter: segmentFilter(hoveredSegmentId),
    paint: {
      'line-color': '#ef7d22',
      'line-width': 8,
      'line-opacity': 0.92,
    },
  }

  const selectedLayer: LayerProps = {
    id: RIVER_SELECTED_LAYER_ID,
    type: 'line',
    source: RIVER_SOURCE_ID,
    filter: segmentFilter(selectedSegmentId),
    paint: {
      'line-color': '#2266cc',
      'line-width': 7,
      'line-opacity': 0.96,
    },
  }

  return (
    <Source
      id={RIVER_SOURCE_ID}
      type="geojson"
      data={data as GeoJSONSourceRaw['data']}
      promoteId="segment_id"
    >
      <Layer {...riverLayer} />
      <Layer {...hoverLayer} />
      <Layer {...selectedLayer} />
    </Source>
  )
}
