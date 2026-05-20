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
  isRunMismatchMetadata,
  metadataHasValidTime,
  metadataMatchesRun,
  type RunSourceIdentity,
  type MvtLayerMetadata,
} from '@/lib/mvtLayerMetadata'
import { DEFAULT_FLOOD_RETURN_PERIOD_DURATION } from '@/lib/floodReturnPeriodDuration'

export const FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY = 'feature_id'

interface FloodReturnPeriodLayerProps {
  runId: string
  validTime: string
  selectedLevel?: AlertLevel | null
  hoveredFeatureId?: string | null
  selectedFeatureId?: string | null
  onUnavailableReason?: (reason: string | null) => void
  metadata?: MvtLayerMetadata | null
  runIdentity?: RunSourceIdentity | null
  fallbackBbox?: FloodReturnPeriodGeoJsonBbox | null
  degradedFallback?: boolean
}

function featureFilter(featureId?: string | null): FilterSpecification {
  return ['==', ['get', FLOOD_RETURN_PERIOD_FEATURE_ID_PROPERTY], featureId ?? ''] as FilterSpecification
}

function metadataIdentity(metadata: MvtLayerMetadata): string {
  return JSON.stringify({
    cache_etag: metadata.cache_etag ?? null,
    cache_version: metadata.cache_version ?? null,
    canonical_route_layer_id: metadata.canonical_route_layer_id ?? metadata.layer_id,
    encoder_version: metadata.encoder_version ?? null,
    maplibre_source_layer: metadata.maplibre_source_layer,
    schema_version: metadata.schema_version ?? metadata.property_schema_version ?? null,
    source_refs: metadata.source_refs ?? null,
  })
}

export function floodMvtSourceKey(metadata: MvtLayerMetadata, runId: string, validTime: string): string {
  return JSON.stringify({
    layer_id: metadata.layer_id,
    route_layer_id: metadata.canonical_route_layer_id ?? metadata.layer_id,
    run_id: runId,
    valid_time: validTime,
    duration: DEFAULT_FLOOD_RETURN_PERIOD_DURATION,
    metadata: metadataIdentity(metadata),
  })
}

export function floodTileUrl(runId: string, validTime: string, bbox?: FloodReturnPeriodGeoJsonBbox) {
  return buildFloodReturnPeriodGeoJsonUrl(runId, validTime, bbox ? { bbox, limit: floodReturnPeriodGeoJsonBudget.maxFallbackFeatures } : {})
}

export function floodMvtTileUrlTemplate(metadata: MvtLayerMetadata, runId: string, validTime: string) {
  return buildMvtTileUrlTemplate(metadata, { run_id: runId, duration: DEFAULT_FLOOD_RETURN_PERIOD_DURATION, valid_time: validTime })
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
  runIdentity = null,
  fallbackBbox,
  degradedFallback = false,
}: FloodReturnPeriodLayerProps) {
  const [data, setData] = useState<FloodReturnPeriodFeatureCollection | null>(null)
  const [catalogMetadata, setCatalogMetadata] = useState<MvtLayerMetadata | null>(() =>
    isMvtLayerMetadata(metadata) && !metadata.release_blocking && metadataMatchesRun(metadata, runId, runIdentity)
      ? metadata
      : null,
  )
  const [metadataState, setMetadataState] = useState<'ready' | 'loading' | 'unavailable'>(
    metadata === undefined
      ? 'loading'
      : isMvtLayerMetadata(metadata) && !metadata.release_blocking && metadataMatchesRun(metadata, runId, runIdentity)
        ? 'ready'
        : 'unavailable',
  )
  const directMetadata =
    isMvtLayerMetadata(metadata) && !metadata.release_blocking && metadataMatchesRun(metadata, runId, runIdentity)
      ? metadata
      : null
  const directMetadataBlocked = metadata !== undefined && !directMetadata
  const activeMetadata = directMetadata ?? catalogMetadata
  const activeMetadataValidTimeAvailable = activeMetadata ? metadataHasValidTime(activeMetadata, validTime) : false
  const metadataRunMismatch = isRunMismatchMetadata(metadata, runId)

  useEffect(() => {
    if (metadata !== undefined) {
      const usable =
        isMvtLayerMetadata(metadata) && !metadata.release_blocking && metadataMatchesRun(metadata, runId, runIdentity)
          ? metadata
          : null
      setCatalogMetadata(usable)
      setMetadataState(usable ? 'ready' : 'unavailable')
      return
    }
    const controller = new AbortController()
    setCatalogMetadata(null)
    setMetadataState('loading')
    fetchLayerCatalogMetadata(controller.signal, runId)
      .then((layers) => {
        const layer = layers.find((item) => item.layer_id === 'flood-return-period')
        if (isRunMismatchMetadata(layer?.metadata, runId)) {
          setCatalogMetadata(null)
          setMetadataState('unavailable')
          return
        }
        const discovered =
          isMvtLayerMetadata(layer?.metadata) &&
          !layer.metadata.release_blocking &&
          metadataMatchesRun(layer.metadata, runId, runIdentity)
            ? layer.metadata
            : null
        setCatalogMetadata(discovered)
        setMetadataState(discovered ? 'ready' : 'unavailable')
      })
      .catch(() => {
        setCatalogMetadata(null)
        setMetadataState('unavailable')
      })
    return () => controller.abort()
  }, [metadata, runId, runIdentity])

  useEffect(() => {
    if (activeMetadata) {
      setData(null)
      onUnavailableReason?.(
        activeMetadataValidTimeAvailable
          ? null
          : '洪水重现期 MVT 元数据未提供当前 valid_time，地图暂不注册全国矢量瓦片源。',
      )
      return
    }
    setData(null)
    if (directMetadataBlocked) {
      onUnavailableReason?.('洪水重现期 MVT 元数据不可用、运行批次不匹配或处于 release-blocking 状态，地图暂不显示该叠加层。')
      return
    }
    if (metadataRunMismatch) {
      onUnavailableReason?.('洪水重现期 MVT 元数据运行批次不匹配，地图暂不显示该叠加层。')
      return
    }
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
  }, [
    activeMetadata,
    activeMetadataValidTimeAvailable,
    degradedFallback,
    directMetadataBlocked,
    fallbackBbox,
    metadataState,
    onUnavailableReason,
    runId,
    validTime,
  ])

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

  if (activeMetadata && activeMetadataValidTimeAvailable) {
    const sourceKey = floodMvtSourceKey(activeMetadata, runId, validTime)
    return (
      <Source
        key={sourceKey}
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
