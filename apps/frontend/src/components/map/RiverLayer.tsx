import {
  Layer,
  Source,
  type GeoJSONSourceRaw,
  type LayerProps,
} from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'

export const RIVER_SOURCE_ID = 'demo-rivers'
export const RIVER_LAYER_ID = 'river-network-line'
export const RIVER_HOVER_LAYER_ID = 'river-network-hover'
export const RIVER_SELECTED_LAYER_ID = 'river-network-selected'

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
  total?: number
  feature_total?: number
  limit?: number
  offset?: number
}

const DEMO_BASIN_VERSION_ID = 'yangtze_v2026_01'
const DEMO_RIVER_NETWORK_VERSION_ID = 'yangtze_rivnet_v01'

function riverProperties(index: number, name: string): RiverFeatureProperties {
  return {
    segment_id: `${DEMO_RIVER_NETWORK_VERSION_ID}_riv_${String(index).padStart(4, '0')}`,
    name,
    stream_order: index,
    basin_version_id: DEMO_BASIN_VERSION_ID,
    river_network_version_id: DEMO_RIVER_NETWORK_VERSION_ID,
  }
}

export const demoRivers: RiverFeatureCollection = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: riverProperties(1, '长江干流 0001'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [91, 31],
          [94, 31.2],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(2, '长江干流 0002'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [94, 31.2],
          [97, 31],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(3, '长江干流 0003'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [97, 31],
          [100, 30.8],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(4, '长江干流 0004'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [100, 30.8],
          [103, 30.6],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(5, '长江干流 0005'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [103, 30.6],
          [106, 30.7],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(6, '长江干流 0006'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [106, 30.7],
          [109, 30.9],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(7, '长江干流 0007'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [109, 30.9],
          [112, 31.1],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(8, '长江干流 0008'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [112, 31.1],
          [115, 31],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(9, '长江干流 0009'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [115, 31],
          [118, 31.2],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(10, '长江干流 0010'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [118, 31.2],
          [121, 31],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(11, '北侧支流 0011'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [98, 33.5],
          [100, 30.8],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(12, '北侧支流 0012'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [104, 34],
          [106, 30.7],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(13, '北侧支流 0013'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [110, 33.8],
          [112, 31.1],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(14, '南侧支流 0014'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [114, 28],
          [115, 31],
        ],
      },
    },
    {
      type: 'Feature',
      properties: riverProperties(15, '南侧支流 0015'),
      geometry: {
        type: 'LineString',
        coordinates: [
          [119, 29],
          [121, 31],
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
