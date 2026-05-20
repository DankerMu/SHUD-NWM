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
  floodReturnPeriodGeoJsonBudget,
  type FloodReturnPeriodGeoJsonBbox,
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
  fallbackBbox?: FloodReturnPeriodGeoJsonBbox | null
  degradedFallback?: boolean
}

function featureFilter(featureId?: string | null): FilterSpecification {
  return ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], featureId ?? ''] as FilterSpecification
}

export function floodTileUrl(runId: string, validTime: string, bbox?: FloodReturnPeriodGeoJsonBbox) {
  return buildFloodReturnPeriodGeoJsonUrl(runId, validTime, bbox ? { bbox, limit: floodReturnPeriodGeoJsonBudget.maxFallbackFeatures } : {})
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
  fallbackBbox,
  degradedFallback = false,
}: FloodReturnPeriodLayerProps) {
  const [data, setData] = useState<FloodReturnPeriodFeatureCollection | null>(null)
  const [catalogMetadata, setCatalogMetadata] = useState<MvtLayerMetadata | null>(metadata ?? null)
  const [metadataState, setMetadataState] = useState<'ready' | 'loading' | 'unavailable'>(metadata ? 'ready' : 'loading')
  const activeMetadata = metadata ?? catalogMetadata

  useEffect(() => {
    if (metadata) {
      setCatalogMetadata(metadata)
      setMetadataState('ready')
      return
    }
    const controller = new AbortController()
    setCatalogMetadata(null)
    setMetadataState('loading')
    fetchLayerCatalogMetadata(controller.signal)
      .then((layers) => {
        const layer = layers.find((item) => item.layer_id === 'flood-return-period')
        const discovered = isMvtLayerMetadata(layer?.metadata) && !layer.metadata.release_blocking ? layer.metadata : null
        setCatalogMetadata(discovered)
        setMetadataState(discovered ? 'ready' : 'unavailable')
      })
      .catch(() => {
        setCatalogMetadata(null)
        setMetadataState('unavailable')
      })
    return () => controller.abort()
  }, [metadata])

  useEffect(() => {
    if (activeMetadata) {
      setData(null)
      onUnavailableReason?.(null)
      return
    }
    setData(null)
    if (metadataState === 'loading') {
      onUnavailableReason?.('洪水重现期 MVT 元数据正在加载，暂不请求 GeoJSON 兼容端点。')
      return
    }
    if (!degradedFallback || !fallbackBbox) {
      onUnavailableReason?.('洪水重现期全国 MVT 尚不可用，已阻止无边界 GeoJSON 兼容请求。')
      return
    }
    const controller = new AbortController()
    onUnavailableReason?.('洪水重现期 MVT 不可用，正在使用 bbox 限定的 GeoJSON 降级源。')
    fetchFloodReturnPeriodFeatureCollection(
      buildFloodReturnPeriodGeoJsonUrl(runId, validTime, {
        bbox: fallbackBbox,
        limit: floodReturnPeriodGeoJsonBudget.maxFallbackFeatures,
      }),
      {
        signal: controller.signal,
        budget: {
          maxFeatures: floodReturnPeriodGeoJsonBudget.maxFallbackFeatures,
          maxCoordinates: floodReturnPeriodGeoJsonBudget.maxCoordinates,
          maxCoordinateDimensions: floodReturnPeriodGeoJsonBudget.maxCoordinateDimensions,
          maxSerializedBytes: floodReturnPeriodGeoJsonBudget.maxSerializedBytes,
        },
      },
    )
      .then((result) => {
        if (!result.ok) {
          setData(null)
          onUnavailableReason?.(result.reason)
          return
        }
        setData(result.data)
        onUnavailableReason?.('洪水重现期 MVT 不可用，已使用 bbox 限定的 GeoJSON 降级源。')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setData(null)
        onUnavailableReason?.('洪水重现期 GeoJSON 降级源请求失败，地图暂不显示该叠加层。')
      })
    return () => controller.abort()
  }, [activeMetadata, degradedFallback, fallbackBbox, metadataState, onUnavailableReason, runId, validTime])

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
