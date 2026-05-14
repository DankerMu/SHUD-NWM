import { useEffect, useState } from 'react'
import { Layer, Source, type LayerProps } from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'

import {
  FLOOD_TILE_HOVER_LAYER_ID,
  FLOOD_TILE_LAYER_ID,
  FLOOD_TILE_SELECTED_LAYER_ID,
  FLOOD_TILE_SOURCE_ID,
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

interface GeoJsonFeatureCollection {
  type: 'FeatureCollection'
  features: unknown[]
}

function segmentFilter(segmentId?: string | null): FilterSpecification {
  return ['==', ['get', 'segment_id'], segmentId ?? ''] as FilterSpecification
}

export function floodTileUrl(runId: string, validTime: string) {
  const params = new URLSearchParams({
    run_id: runId,
    duration: '1h',
    valid_time: validTime,
  })
  return `/api/v1/tiles/flood-return-period?${params.toString()}`
}

export function floodReturnPeriodLayer(selectedLevel?: AlertLevel | null): LayerProps {
  return {
    id: FLOOD_TILE_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
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
  const [data, setData] = useState<GeoJsonFeatureCollection | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)

    fetch(floodTileUrl(runId, validTime), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return null
        const payload = (await response.json()) as GeoJsonFeatureCollection
        return payload.type === 'FeatureCollection' ? payload : null
      })
      .then((payload) => {
        if (!controller.signal.aborted) setData(payload)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) {
          setData(null)
        }
      })

    return () => controller.abort()
  }, [runId, validTime])

  const hoverLayer: LayerProps = {
    id: FLOOD_TILE_HOVER_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
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
    filter: segmentFilter(selectedSegmentId),
    paint: {
      'line-color': '#2266cc',
      'line-width': 8,
      'line-opacity': 0.9,
    },
  }

  if (!data) return null

  return (
    <Source
      id={FLOOD_TILE_SOURCE_ID}
      type="geojson"
      data={data}
      promoteId="segment_id"
    >
      <Layer {...floodReturnPeriodLayer(selectedLevel)} />
      <Layer {...hoverLayer} />
      <Layer {...selectedLayer} />
    </Source>
  )
}
