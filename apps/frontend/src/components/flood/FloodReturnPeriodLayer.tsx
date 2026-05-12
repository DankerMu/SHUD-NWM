import { Layer, Source, type LayerProps } from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'

import {
  FLOOD_TILE_HOVER_LAYER_ID,
  FLOOD_TILE_LAYER_ID,
  FLOOD_TILE_SELECTED_LAYER_ID,
  FLOOD_TILE_SOURCE_ID,
  FLOOD_TILE_SOURCE_LAYER,
  floodTileLayerPaint,
  type AlertLevel,
} from '@/components/flood/alertLevels'

interface FloodReturnPeriodLayerProps {
  runId: string
  validTime: string
  selectedLevel?: AlertLevel | null
  hoveredSegmentId?: string | null
  selectedSegmentId?: string | null
}

function segmentFilter(segmentId?: string | null): FilterSpecification {
  return ['==', ['get', 'river_segment_id'], segmentId ?? ''] as FilterSpecification
}

export function floodTileUrl(runId: string, validTime: string) {
  return `/api/v1/tiles/flood-return-period/${encodeURIComponent(runId)}/1h/${encodeURIComponent(
    validTime,
  )}/{z}/{x}/{y}.pbf`
}

export function floodReturnPeriodLayer(selectedLevel?: AlertLevel | null): LayerProps {
  return {
    id: FLOOD_TILE_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
    'source-layer': FLOOD_TILE_SOURCE_LAYER,
    paint: floodTileLayerPaint(selectedLevel),
  }
}

export function FloodReturnPeriodLayer({
  runId,
  validTime,
  selectedLevel,
  hoveredSegmentId,
  selectedSegmentId,
}: FloodReturnPeriodLayerProps) {
  const hoverLayer: LayerProps = {
    id: FLOOD_TILE_HOVER_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
    'source-layer': FLOOD_TILE_SOURCE_LAYER,
    filter: segmentFilter(hoveredSegmentId),
    paint: {
      'line-color': '#111827',
      'line-width': 7,
      'line-opacity': 0.82,
    },
  }

  const selectedLayer: LayerProps = {
    id: FLOOD_TILE_SELECTED_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
    'source-layer': FLOOD_TILE_SOURCE_LAYER,
    filter: segmentFilter(selectedSegmentId),
    paint: {
      'line-color': '#2266cc',
      'line-width': 8,
      'line-opacity': 0.9,
    },
  }

  return (
    <Source
      id={FLOOD_TILE_SOURCE_ID}
      type="vector"
      tiles={[floodTileUrl(runId, validTime)]}
      minzoom={0}
      maxzoom={14}
      promoteId="river_segment_id"
    >
      <Layer {...floodReturnPeriodLayer(selectedLevel)} />
      <Layer {...hoverLayer} />
      <Layer {...selectedLayer} />
    </Source>
  )
}
