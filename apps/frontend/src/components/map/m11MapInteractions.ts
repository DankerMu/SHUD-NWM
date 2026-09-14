import type { MapLayerMouseEvent, MapRef } from 'react-map-gl/maplibre'

import type { M11Layer } from '@/lib/m11/queryState'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
  MET_STATION_SOURCE_ID,
  m11RegisteredOverlayHitLayerId,
} from '@/components/map/m11MapPrimitives'

export interface M11MapOverlayInteraction {
  layerId: M11Layer | 'met-stations' | 'basin-boundaries'
  event: MapLayerMouseEvent
  feature?: NonNullable<MapLayerMouseEvent['features']>[number]
}

interface M11InteractionContext {
  showStationLayer: boolean
  renderableOverlay: M11RegisteredOverlay | null
  mapRef: MapRef | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
}

export function buildM11InteractiveLayerIds({
  showStationLayer,
  hasBasinFeatures,
  renderableOverlay,
}: {
  showStationLayer: boolean
  hasBasinFeatures: boolean
  renderableOverlay: M11RegisteredOverlay | null
}): string[] {
  return [
    ...(showStationLayer ? [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID] : []),
    ...(hasBasinFeatures ? [M11_BASIN_FILL_LAYER_ID] : []),
    ...(renderableOverlay ? [m11RegisteredOverlayHitLayerId(renderableOverlay)] : []),
  ]
}

export function handleM11MapMouseMove(event: MapLayerMouseEvent, context: M11InteractionContext) {
  const { showStationLayer, renderableOverlay, mapRef, onOverlayHover } = context
  if (showStationLayer) {
    const stationFeature =
      findRenderedFeature(event, mapRef, MET_STATION_POINT_LAYER_ID) ??
      findRenderedFeature(event, mapRef, MET_STATION_CLUSTER_LAYER_ID)
    if (stationFeature) {
      onOverlayHover?.(null)
      event.target.getCanvas().style.cursor = 'pointer'
      return
    }
  }

  const overlayFeature = renderableOverlay ? findEventFeature(event, m11RegisteredOverlayHitLayerId(renderableOverlay)) : null
  if (renderableOverlay && overlayFeature) {
    onOverlayHover?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
    event.target.getCanvas().style.cursor = 'pointer'
    return
  }

  const basinFeature = findEventFeature(event, M11_BASIN_FILL_LAYER_ID)
  if (basinFeature) {
    onOverlayHover?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
    event.target.getCanvas().style.cursor = 'pointer'
    return
  }

  onOverlayHover?.(null)
  event.target.getCanvas().style.cursor = ''
}

export function handleM11MapMouseLeave(
  event: MapLayerMouseEvent,
  context: Pick<M11InteractionContext, 'onOverlayHover'>,
) {
  context.onOverlayHover?.(null)
  event.target.getCanvas().style.cursor = ''
}

export function handleM11MapClick(event: MapLayerMouseEvent, context: M11InteractionContext) {
  const { showStationLayer, renderableOverlay, mapRef, onOverlayClick } = context
  if (showStationLayer) {
    const clusterFeature = findRenderedFeature(event, mapRef, MET_STATION_CLUSTER_LAYER_ID)
    if (clusterFeature) {
      expandStationCluster(mapRef, clusterFeature)
      return
    }

    const stationFeature = findRenderedFeature(event, mapRef, MET_STATION_POINT_LAYER_ID)
    if (stationFeature) {
      onOverlayClick?.({ layerId: 'met-stations', event, feature: stationFeature })
      return
    }
  }

  const overlayFeature = renderableOverlay ? findEventFeature(event, m11RegisteredOverlayHitLayerId(renderableOverlay)) : null
  if (renderableOverlay && overlayFeature) {
    onOverlayClick?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
    return
  }

  const basinFeature = findEventFeature(event, M11_BASIN_FILL_LAYER_ID)
  if (basinFeature) {
    onOverlayClick?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
  }
}

type StationClusterSource = {
  getClusterExpansionZoom?: (
    clusterId: number,
    callback?: (error: unknown, zoom: number) => void,
  ) => Promise<number> | void
}

function expandStationCluster(
  mapRef: MapRef | null,
  feature: NonNullable<MapLayerMouseEvent['features']>[number],
) {
  const map = mapRef?.getMap?.()
  if (!map) return
  const source = (map.getSource(MET_STATION_SOURCE_ID) as StationClusterSource | undefined) ?? undefined
  const clusterId = feature.properties?.cluster_id ?? feature.id
  const geometry = feature.geometry
  if (!source?.getClusterExpansionZoom || typeof clusterId !== 'number' || geometry?.type !== 'Point') return
  const [lon, lat] = geometry.coordinates as [number, number]
  const flyToZoom = (zoom: number) => {
    if (!Number.isFinite(zoom)) return
    map.flyTo({ center: [lon, lat], zoom, duration: 450 })
  }
  const expansion = source.getClusterExpansionZoom(clusterId, (error, zoom) => {
    if (!error) flyToZoom(zoom)
  })
  if (expansion && typeof expansion.then === 'function') {
    void expansion.then(flyToZoom).catch(() => undefined)
  }
}

function findEventFeature(event: MapLayerMouseEvent, layerId: string) {
  return event.features?.find((feature) => feature.layer?.id === layerId) ?? null
}

function findRenderedFeature(event: MapLayerMouseEvent, mapRef: MapRef | null, layerId: string) {
  const eventFeature = findEventFeature(event, layerId)
  if (eventFeature) return eventFeature
  const map = mapRef?.getMap?.()
  if (!map) return null
  try {
    return map.queryRenderedFeatures(event.point, { layers: [layerId] }).find((feature) => feature.layer?.id === layerId) ?? null
  } catch {
    return null
  }
}

/** 取地图要素的非空字符串属性；缺失 / 非字符串 / 空串一律 null。 */
export function mapFeatureStringProperty(feature: M11MapOverlayInteraction['feature'], key: string) {
  const value = feature?.properties?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** 弹窗锚点：点要素取其坐标，否则回落到点击事件的经纬度；都不可用时返回 null。 */
export function popupAnchorFromInteraction(interaction: M11MapOverlayInteraction): [number, number] | null {
  const geometry = interaction.feature?.geometry
  if (geometry && geometry.type === 'Point' && Array.isArray(geometry.coordinates)) {
    const [lon, lat] = geometry.coordinates as number[]
    if (Number.isFinite(lon) && Number.isFinite(lat)) return [lon, lat]
  }
  const lngLat = (interaction.event as { lngLat?: { lng?: number; lat?: number } }).lngLat
  if (lngLat && Number.isFinite(lngLat.lng) && Number.isFinite(lngLat.lat)) {
    return [lngLat.lng as number, lngLat.lat as number]
  }
  return null
}
