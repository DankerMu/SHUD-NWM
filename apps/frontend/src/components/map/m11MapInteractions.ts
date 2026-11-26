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

/**
 * `river_network_version_id` -> segment count of that network, from the model records the overview
 * has loaded (`buildNetworkSegmentCounts`, snapshot field `networkSegmentCounts`). `null` = no records loaded.
 */
export type M11NetworkSegmentCounts = Readonly<Record<string, number>>

interface M11InteractionContext {
  showStationLayer: boolean
  renderableOverlay: M11RegisteredOverlay | null
  /** Per-network segment counts for the shared-reach owner rule (`chooseM11OverlayHitFeature`). */
  segmentCounts: M11NetworkSegmentCounts | null
  mapRef: MapRef | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  /**
   * 地图上的每一次点击都通知一次，无论是否命中要素（移动形态收起已展开的浮层面板用）。
   * 与 `onOverlayClick` 相互独立：后者仍只在命中站点 / 叠加层 / 流域时触发。
   */
  onMapClick?: () => void
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
  const { showStationLayer, renderableOverlay, segmentCounts, mapRef, onOverlayHover } = context
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

  // 与点击同一个选择（shared-reach-click-ownership D5）：高亮的正是点击会打开的那个河段。
  const overlayFeature = renderableOverlay
    ? chooseM11OverlayHitFeature(event.features, m11RegisteredOverlayHitLayerId(renderableOverlay), segmentCounts)
    : null
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

/**
 * 悬停高亮的河段 id（#2628，fixture D2）：只认 discharge overlay 上的 discharge 命中要素，
 * 取 `river_segment_id ?? segment_id`（非空字符串）；站点、流域面、空白、无 interaction 一律 null。
 */
export function resolveM11HoveredSegmentId(
  interaction: M11MapOverlayInteraction | null,
  renderableOverlay: M11RegisteredOverlay | null,
): string | null {
  if (interaction?.layerId !== 'discharge' || renderableOverlay?.layerId !== 'discharge') return null
  return (
    mapFeatureStringProperty(interaction.feature, 'river_segment_id') ??
    mapFeatureStringProperty(interaction.feature, 'segment_id')
  )
}

/**
 * 按 id 去重的悬停状态跟踪器：`update(id)` 只在 id 变化时回调 `onChange`。
 * 置 null 也必须经过 `update(null)`——否则记住的旧 id 不清，切回图层后再悬停同一河段不会高亮。
 */
export function createM11HoverSegmentTracker(onChange: (segmentId: string | null) => void) {
  let current: string | null = null
  return {
    update(segmentId: string | null) {
      if (segmentId === current) return
      current = segmentId
      onChange(segmentId)
    },
  }
}

/**
 * Minimal rendered-feature shape the click-target resolver reads: the layer id, and
 * `properties.river_network_version_id` for the shared-reach owner rule.
 */
export interface M11ClickTargetFeature {
  layer?: { id?: string } | null
  properties?: { [key: string]: unknown } | null
}

/** A count is usable when it is a finite number greater than zero. */
function usableSegmentCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function featureNetworkId(feature: M11ClickTargetFeature): string | null {
  const value = feature.properties?.river_network_version_id
  return typeof value === 'string' && value.length > 0 ? value : null
}

/**
 * The overlay hit-layer feature a product click at the point opens (#3390, shared-reach-click-ownership).
 * Candidates are the entries of `features` whose layer is `hitLayerId`, in the map's order; the first
 * of them under this order is returned:
 * 1. counts, all or nothing: when every candidate has a usable count in `segmentCounts` (looked up by
 *    its `river_network_version_id`), fewer segments first; when any candidate has none (no records,
 *    network absent, unusable count, no network id), counts are not used at all;
 * 2. descending `river_network_version_id`, compared by UTF-16 code units (never by locale);
 * 3. a candidate without a `river_network_version_id` after every candidate with one;
 * 4. otherwise the map's order (features of one network keep it).
 * Pure: no map, no DOM; `features` is not mutated and the returned feature is the map's own object.
 */
export function chooseM11OverlayHitFeature<F extends M11ClickTargetFeature>(
  features: readonly F[] | null | undefined,
  hitLayerId: string,
  segmentCounts: M11NetworkSegmentCounts | null,
): F | null {
  const candidates = (features ?? [])
    .filter((feature) => feature?.layer?.id === hitLayerId)
    .map((feature, index) => {
      const networkId = featureNetworkId(feature)
      const count = networkId !== null && segmentCounts ? usableSegmentCount(segmentCounts[networkId]) : null
      return { feature, index, networkId, count }
    })
  if (candidates.length === 0) return null
  const useCounts = candidates.every((candidate) => candidate.count !== null)
  type Candidate = (typeof candidates)[number]
  const compare = (a: Candidate, b: Candidate): number => {
    // Rule 1 (only when every candidate has a count).
    if (useCounts && a.count !== b.count) return (a.count as number) < (b.count as number) ? -1 : 1
    // Rules 2 and 3: plain `<` / `>` on strings compares UTF-16 code units.
    if (a.networkId !== b.networkId) {
      if (a.networkId === null) return 1
      if (b.networkId === null) return -1
      return a.networkId > b.networkId ? -1 : 1
    }
    // Rule 4.
    return a.index - b.index
  }
  let chosen = candidates[0]
  for (const candidate of candidates.slice(1)) {
    if (compare(candidate, chosen) < 0) chosen = candidate
  }
  return chosen.feature
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
 * -> the renderable overlay's hit-layer feature chosen by
 * `chooseM11OverlayHitFeature` with `segmentCounts` -> basin fill.
 * `features` are the rendered features at the point in the current interactive
 * layers (what react-map-gl hands the click event). `findStationFeature` lets
 * the product keep its direct station-layer query fallback; by default a
 * station candidate is the first matching entry of `features`.
 */
export function resolveM11ClickTarget<F extends M11ClickTargetFeature>({
  features,
  showStationLayer,
  renderableOverlay,
  segmentCounts,
  findStationFeature,
}: {
  features: readonly F[] | null | undefined
  showStationLayer: boolean
  renderableOverlay: M11RegisteredOverlay | null
  /** Required (`null` = no model records loaded): every caller states which counts it resolves with. */
  segmentCounts: M11NetworkSegmentCounts | null
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
    const overlayFeature = chooseM11OverlayHitFeature(features, hitLayerId, segmentCounts)
    if (overlayFeature) return { kind: 'overlay', layerId: renderableOverlay.layerId, hitLayerId, feature: overlayFeature }
  }
  const basinFeature = firstIn(M11_BASIN_FILL_LAYER_ID)
  if (basinFeature) return { kind: 'basin', feature: basinFeature }
  return null
}

export function handleM11MapClick(event: MapLayerMouseEvent, context: M11InteractionContext) {
  const { showStationLayer, renderableOverlay, segmentCounts, mapRef, onOverlayClick, onMapClick } = context
  onMapClick?.()
  const target = resolveM11ClickTarget({
    features: event.features,
    showStationLayer,
    renderableOverlay,
    segmentCounts,
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
