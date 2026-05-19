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
import {
  buildFloodReturnPeriodGeoJsonUrl,
  fetchFloodReturnPeriodFeatureCollection,
  type FloodReturnPeriodFeatureCollection,
} from '@/lib/floodReturnPeriodGeoJson'

export const FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY = 'feature_id'

interface FloodReturnPeriodLayerProps {
  runId: string
  validTime: string
  selectedLevel?: AlertLevel | null
  hoveredFeatureId?: string | null
  selectedFeatureId?: string | null
  onUnavailableReason?: (reason: string | null) => void
}

function featureFilter(featureId?: string | null): FilterSpecification {
  return ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], featureId ?? ''] as FilterSpecification
}

export function floodTileUrl(runId: string, validTime: string) {
  return buildFloodReturnPeriodGeoJsonUrl(runId, validTime)
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
  hoveredFeatureId,
  selectedFeatureId,
  onUnavailableReason,
}: FloodReturnPeriodLayerProps) {
  const [data, setData] = useState<FloodReturnPeriodFeatureCollection | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    onUnavailableReason?.(null)

    fetchFloodReturnPeriodFeatureCollection(floodTileUrl(runId, validTime), { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return
        if (result.ok) {
          setData(result.data)
          onUnavailableReason?.(null)
        } else {
          setData(null)
          onUnavailableReason?.(result.reason)
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) {
          setData(null)
          onUnavailableReason?.('洪水重现期地图数据加载失败，地图暂不显示该叠加层。')
        }
      })

    return () => controller.abort()
  }, [onUnavailableReason, runId, validTime])

  const hoverLayer: LayerProps = {
    id: FLOOD_TILE_HOVER_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
    filter: featureFilter(hoveredFeatureId),
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
    filter: featureFilter(selectedFeatureId),
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
      promoteId={FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY}
    >
      <Layer {...floodReturnPeriodLayer(selectedLevel)} />
      <Layer {...hoverLayer} />
      <Layer {...selectedLayer} />
    </Source>
  )
}
