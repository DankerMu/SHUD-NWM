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

/** Minimal rendered-feature shape the click-target resolver reads (only the layer id). */
export interface M11ClickTargetFeature {
  layer?: { id?: string } | null
}

/** The one thing a product map click at a point would act on. */
export type M11ClickTarget<F extends M11ClickTargetFeature> =
  | { kind: 'station-cluster'; feature: F }
  | { kind: 'station'; feature: F }
  | { kind: 'overlay'; layerId: M11Layer; hitLayerId: string; feature: F }
  | { kind: 'basin'; feature: F }

/**
 * Pure product click-target resolution, shared by `handleM11MapClick` and the
 * gated river-click evidence hook so the two can never drift apart. Priority
 * walk: station cluster -> station point (both only when stations are shown)
 * -> the FIRST feature in the renderable overlay's hit layer -> basin fill.
 * `features` are the rendered features at the point in the current interactive
 * layers (what react-map-gl hands the click event). `findStationFeature` lets
 * the product keep its direct station-layer query fallback; by default a
 * station candidate is the first matching entry of `features`.
 */
export function resolveM11ClickTarget<F extends M11ClickTargetFeature>({
  features,
  showStationLayer,
  renderableOverlay,
  findStationFeature,
}: {
  features: readonly F[] | null | undefined
  showStationLayer: boolean
  renderableOverlay: M11RegisteredOverlay | null
  findStationFeature?: (layerId: string) => F | null
}): M11ClickTarget<F> | null {
  const firstIn = (layerId: string): F | null => features?.find((feature) => feature?.layer?.id === layerId) ?? null
  if (showStationLayer) {
    const station = findStationFeature ?? firstIn
    const clusterFeature = station(MET_STATION_CLUSTER_LAYER_ID)
    if (clusterFeature) return { kind: 'station-cluster', feature: clusterFeature }
    const stationFeature = station(MET_STATION_POINT_LAYER_ID)
    if (stationFeature) return { kind: 'station', feature: stationFeature }
  }
  if (renderableOverlay) {
    const hitLayerId = m11RegisteredOverlayHitLayerId(renderableOverlay)
    const overlayFeature = firstIn(hitLayerId)
    if (overlayFeature) return { kind: 'overlay', layerId: renderableOverlay.layerId, hitLayerId, feature: overlayFeature }
  }
  const basinFeature = firstIn(M11_BASIN_FILL_LAYER_ID)
  if (basinFeature) return { kind: 'basin', feature: basinFeature }
  return null
}

export function handleM11MapClick(event: MapLayerMouseEvent, context: M11InteractionContext) {
  const { showStationLayer, renderableOverlay, mapRef, onOverlayClick } = context
  const target = resolveM11ClickTarget({
    features: event.features,
    showStationLayer,
    renderableOverlay,
    findStationFeature: (layerId) => findRenderedFeature(event, mapRef, layerId),
  })
  if (target === null) return
  switch (target.kind) {
    case 'station-cluster':
      expandStationCluster(mapRef, target.feature)
      return
    case 'station':
      onOverlayClick?.({ layerId: 'met-stations', event, feature: target.feature })
      return
    case 'overlay':
      onOverlayClick?.({ layerId: target.layerId, event, feature: target.feature })
      return
    case 'basin':
      onOverlayClick?.({ layerId: 'basin-boundaries', event, feature: target.feature })
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
