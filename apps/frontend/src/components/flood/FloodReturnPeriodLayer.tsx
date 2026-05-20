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
import {
  buildMvtTileUrlTemplate,
  fetchLayerCatalogMetadata,
  isMvtLayerMetadata,
  type MvtLayerMetadata,
} from '@/lib/mvtLayerMetadata'

export const FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY = 'feature_id'

interface FloodReturnPeriodLayerProps {
  runId: string
  validTime: string
  selectedLevel?: AlertLevel | null
  hoveredFeatureId?: string | null
  selectedFeatureId?: string | null
  onUnavailableReason?: (reason: string | null) => void
  metadata?: MvtLayerMetadata | null
}

function featureFilter(featureId?: string | null): FilterSpecification {
  return ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], featureId ?? ''] as FilterSpecification
}

export function floodTileUrl(runId: string, validTime: string) {
  return buildFloodReturnPeriodGeoJsonUrl(runId, validTime)
}

export function floodMvtTileUrlTemplate(metadata: MvtLayerMetadata, runId: string, validTime: string) {
  return buildMvtTileUrlTemplate(metadata, { run_id: runId, duration: '1h', valid_time: validTime })
}

export function floodReturnPeriodLayer(selectedLevel?: AlertLevel | null, sourceLayer?: string): LayerProps {
  return {
    id: FLOOD_TILE_LAYER_ID,
    type: 'line',
    source: FLOOD_TILE_SOURCE_ID,
    ...(sourceLayer ? { 'source-layer': sourceLayer } : {}),
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
  metadata,
}: FloodReturnPeriodLayerProps) {
  const [data, setData] = useState<FloodReturnPeriodFeatureCollection | null>(null)
  const [catalogMetadata, setCatalogMetadata] = useState<MvtLayerMetadata | null>(metadata ?? null)
  const activeMetadata = metadata ?? catalogMetadata

  useEffect(() => {
    if (metadata) {
      setCatalogMetadata(metadata)
      return
    }
    const controller = new AbortController()
    fetchLayerCatalogMetadata(controller.signal)
      .then((layers) => {
        const layer = layers.find((item) => item.layer_id === 'flood-return-period')
        setCatalogMetadata(isMvtLayerMetadata(layer?.metadata) ? layer.metadata : null)
      })
      .catch(() => setCatalogMetadata(null))
    return () => controller.abort()
  }, [metadata])

  useEffect(() => {
    if (activeMetadata) {
      setData(null)
      onUnavailableReason?.(null)
      return
    }
    const controller = new AbortController()
    setData(null)
    onUnavailableReason?.('洪水重现期正在使用有界 GeoJSON 兼容模式，非全国 MVT 渲染。')

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
  }, [activeMetadata, onUnavailableReason, runId, validTime])

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

  if (activeMetadata) {
    return (
      <Source
        id={FLOOD_TILE_SOURCE_ID}
        type="vector"
        tiles={[floodMvtTileUrlTemplate(activeMetadata, runId, validTime)]}
        minzoom={activeMetadata.min_zoom ?? 0}
        maxzoom={activeMetadata.max_zoom ?? 14}
        promoteId={FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY}
      >
        <Layer {...floodReturnPeriodLayer(selectedLevel, activeMetadata.maplibre_source_layer)} />
        <Layer {...{ ...hoverLayer, 'source-layer': activeMetadata.maplibre_source_layer }} />
        <Layer {...{ ...selectedLayer, 'source-layer': activeMetadata.maplibre_source_layer }} />
      </Source>
    )
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
