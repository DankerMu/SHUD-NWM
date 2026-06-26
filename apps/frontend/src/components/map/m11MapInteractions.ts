import type { MapLayerMouseEvent, MapRef } from 'react-map-gl/maplibre'

import type { M11Layer } from '@/lib/m11/queryState'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  M11_BASIN_RIVER_LINE_LAYER_ID,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
  MET_STATION_SOURCE_ID,
  m11RegisteredOverlayHitLayerId,
} from '@/components/map/m11MapPrimitives'

export interface M11MapOverlayInteraction {
  layerId: M11Layer | 'met-stations' | 'basin-boundaries' | 'basin-river-segments'
  event: MapLayerMouseEvent
  feature?: NonNullable<MapLayerMouseEvent['features']>[number]
}

interface M11InteractionContext {
  showStationLayer: boolean
  renderableOverlay: M11RegisteredOverlay | null
  mapRef: MapRef | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  setHoveredRiverSegmentId?: (segmentId: string | null) => void
}

export function buildM11InteractiveLayerIds({
  showStationLayer,
  hasBasinRiverFeatures,
  hasBasinFeatures,
  renderableOverlay,
}: {
  showStationLayer: boolean
  hasBasinRiverFeatures: boolean
  hasBasinFeatures: boolean
  renderableOverlay: M11RegisteredOverlay | null
}): string[] {
  return [
    ...(showStationLayer ? [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID] : []),
    ...(hasBasinRiverFeatures ? [M11_BASIN_RIVER_LINE_LAYER_ID] : []),
    ...(hasBasinFeatures ? [M11_BASIN_FILL_LAYER_ID] : []),
    ...(renderableOverlay ? [m11RegisteredOverlayHitLayerId(renderableOverlay)] : []),
  ]
}

export function handleM11MapMouseMove(event: MapLayerMouseEvent, context: M11InteractionContext) {
  const { showStationLayer, renderableOverlay, mapRef, onOverlayHover, setHoveredRiverSegmentId } = context
  if (showStationLayer) {
    const stationFeature =
      findRenderedFeature(event, mapRef, MET_STATION_POINT_LAYER_ID) ??
      findRenderedFeature(event, mapRef, MET_STATION_CLUSTER_LAYER_ID)
    if (stationFeature) {
      setHoveredRiverSegmentId?.(null)
      onOverlayHover?.(null)
      event.target.getCanvas().style.cursor = 'pointer'
      return
    }
  }

  const riverFeature = findEventFeature(event, M11_BASIN_RIVER_LINE_LAYER_ID)
  if (riverFeature) {
    const riverSegmentId = featureStringProperty(riverFeature, 'river_segment_id') ?? featureStringProperty(riverFeature, 'segment_id')
    setHoveredRiverSegmentId?.(riverSegmentId)
    onOverlayHover?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
    event.target.getCanvas().style.cursor = 'pointer'
    return
  }

  const overlayFeature = renderableOverlay ? findEventFeature(event, m11RegisteredOverlayHitLayerId(renderableOverlay)) : null
  if (renderableOverlay && overlayFeature) {
    onOverlayHover?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
    event.target.getCanvas().style.cursor = 'pointer'
    return
  }

  const basinFeature = findEventFeature(event, M11_BASIN_FILL_LAYER_ID)
  if (basinFeature) {
    setHoveredRiverSegmentId?.(null)
    onOverlayHover?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
    event.target.getCanvas().style.cursor = 'pointer'
    return
  }

  setHoveredRiverSegmentId?.(null)
  onOverlayHover?.(null)
  event.target.getCanvas().style.cursor = ''
}

export function handleM11MapMouseLeave(
  event: MapLayerMouseEvent,
  context: Pick<M11InteractionContext, 'onOverlayHover' | 'setHoveredRiverSegmentId'>,
) {
  context.setHoveredRiverSegmentId?.(null)
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

  const riverFeature = findEventFeature(event, M11_BASIN_RIVER_LINE_LAYER_ID)
  if (riverFeature) {
    onOverlayClick?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
    return
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

function featureStringProperty(feature: NonNullable<MapLayerMouseEvent['features']>[number], key: string) {
  const value = feature.properties?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}
