import type { FeatureCollection } from 'geojson'
import type { FilterSpecification } from 'maplibre-gl'
import type { LayerProps } from 'react-map-gl/maplibre'

import type { components } from '@/api/types'
import {
  buildMvtTileUrlTemplate,
  isMvtLayerMetadata,
  isNationalOverlayMetadata,
  metadataHasValidTime,
  metadataMatchesRun,
  type MvtLayerMetadata,
} from '@/lib/mvtLayerMetadata'
import {
  getM11BasinGeometryBudgetStatus,
  getM11SelectedSegmentGeometryBudgetStatus,
  m11BasinRiverCollectionBudget,
  m11BasinRiverLayerColor,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import type { M11Layer, M11QueryState } from '@/lib/m11/queryState'

export interface M11RegisteredOverlay {
  layerId: M11Layer
  sourceId: string
  sourceKey: string
  layer: LayerProps
  source: { type: 'vector'; tiles: string[]; sourceLayer: string; minzoom: number; maxzoom: number; metadata: MvtLayerMetadata }
}

export interface BasinFeatureProperties {
  basin_id: string
  basin_name: string
  basin_group: string | null
  area_km2: number | null
  river_count: number | null
  active_model_count: number
  latest_forecast_time: string | null
  selected_basin_version_id: string | null
  unavailable_reason: string | null
}

export interface BasinFeature {
  type: 'Feature'
  geometry: NonNullable<OverviewBasin['boundary']>
  properties: BasinFeatureProperties
}

export interface BasinFeatureCollection {
  type: 'FeatureCollection'
  features: BasinFeature[]
}

export interface BasinRiverFeatureProperties {
  segment_id: string
  river_segment_id: string
  basin_version_id: string
  river_network_version_id: string
  segment_name: string
  q_value: number | null
  q_unit: string
  layer_color: string
}

export interface BasinRiverFeature {
  type: 'Feature'
  geometry: components['schemas']['GeoJsonLineString'] | components['schemas']['GeoJsonMultiLineString']
  properties: BasinRiverFeatureProperties
}

export interface BasinRiverFeatureCollection {
  type: 'FeatureCollection'
  features: BasinRiverFeature[]
  sourceData: {
    type: 'FeatureCollection'
    features: BasinRiverFeature[]
  }
  skippedCount: number
  coordinateCount: number
  serializedBytes: number
  unavailableReason: string | null
}

export interface SelectedSegmentFeature {
  type: 'Feature'
  geometry: components['schemas']['GeoJsonLineString'] | components['schemas']['GeoJsonMultiLineString']
  properties: {
    segment_id: string
  }
}

export interface SelectedSegmentFeatureCollection {
  type: 'FeatureCollection'
  features: SelectedSegmentFeature[]
  unavailableReason: string | null
}

export function buildM11RegisteredOverlay(state: M11QueryState, layers: LayerState[]): M11RegisteredOverlay | null {
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer?.available) return null

  const selectedValidTime = normalizeIso(state.validTime)
  const validTime =
    selectedValidTime && selectedLayer.validTimes.includes(selectedValidTime) ? selectedValidTime : selectedLayer.currentValidTime
  if (!validTime) return null

  const metadata = selectedLayer.metadata
  if (!isMvtLayerMetadata(metadata) || metadata.release_blocking || !metadataHasValidTime(metadata, validTime)) {
    return null
  }

  const national = isNationalOverlayMetadata(metadata)
  const runId = selectedLayer.freshness.runId
  if (!national) {
    if (!runId) return null
    if (
      !metadataMatchesRun(metadata, runId, {
        basin_version_id: selectedLayer.freshness.basinVersionId,
        river_network_version_id: selectedLayer.freshness.riverNetworkVersionId,
      })
    ) {
      return null
    }
  }

  const sourceId = `m11-${state.layer}-source`
  const layerId = `m11-${state.layer}-line`
  const variable = 'q_down'
  const replacements: Record<string, string> = national
    ? { valid_time: validTime, variable }
    : { run_id: runId as string, valid_time: validTime, variable }

  return {
    layerId: state.layer,
    sourceId,
    sourceKey: m11VectorSourceKey({
      layerId: selectedLayer.layerId,
      runId: national ? null : runId,
      validTime,
      variable,
      metadata,
    }),
    source: {
      type: 'vector',
      tiles: [buildMvtTileUrlTemplate(metadata, replacements)],
      sourceLayer: metadata.maplibre_source_layer,
      minzoom: metadata.min_zoom ?? 0,
      maxzoom: metadata.max_zoom ?? 14,
      metadata,
    },
    layer: {
      id: layerId,
      type: 'line',
      source: sourceId,
      'source-layer': metadata.maplibre_source_layer,
      paint: dischargeTileLayerPaint(),
    },
  }
}

export function m11VectorSourceKey({
  layerId,
  runId,
  validTime,
  variable,
  metadata,
}: {
  layerId: string
  runId: string | null
  validTime: string
  variable: string
  metadata: NonNullable<LayerState['metadata']>
}): string {
  return JSON.stringify({
    basin_version_id: metadata.source_refs?.basin_version_id ?? null,
    cache_etag: metadata.cache_etag ?? null,
    cache_version: metadata.cache_version ?? null,
    canonical_route_layer_id: metadata.canonical_route_layer_id ?? metadata.layer_id,
    encoder_version: metadata.encoder_version ?? null,
    layer_id: layerId,
    maplibre_source_layer: metadata.maplibre_source_layer,
    run_id: runId,
    schema_version: metadata.schema_version ?? metadata.property_schema_version ?? null,
    source_refs: metadata.source_refs ?? null,
    valid_time: validTime,
    variable,
  })
}

export function buildBasinFeatureCollection(basins: OverviewBasin[], visibleBasinIds: string[] | undefined): BasinFeatureCollection {
  const visible = visibleBasinIds ? new Set(visibleBasinIds) : null
  return {
    type: 'FeatureCollection',
    features: basins
      .filter((basin) => basin.boundary && getM11BasinGeometryBudgetStatus(basin.boundary).ok && (!visible || visible.has(basin.basinId)))
      .map((basin) => ({
        type: 'Feature',
        geometry: basin.boundary as NonNullable<OverviewBasin['boundary']>,
        properties: {
          basin_id: basin.basinId,
          basin_name: basin.displayName,
          basin_group: basin.basinGroup,
          area_km2: basin.areaKm2,
          river_count: basin.riverCount,
          active_model_count: basin.activeModelCount,
          latest_forecast_time: basin.latestForecastTime,
          selected_basin_version_id: basin.selectedBasinVersionId,
          unavailable_reason: basin.unavailableReason,
        },
      })),
  }
}

export function countSkippedBasinGeometries(basins: OverviewBasin[], visibleBasinIds: string[] | undefined): number {
  return basins.filter((basin) => {
    if (!basin.boundary) return false
    const visible = visibleBasinIds ? visibleBasinIds.includes(basin.basinId) : true
    return visible && !getM11BasinGeometryBudgetStatus(basin.boundary).ok
  }).length
}

export function buildBasinRiverFeatureCollection(
  rows: BasinSegmentRow[],
  layer: M11Layer,
): BasinRiverFeatureCollection {
  let skippedCount = 0
  let coordinateCount = 0
  let featureSerializedBytes = 0
  let serializedBytes = serializedByteLength({ type: 'FeatureCollection', features: [] })
  const features: BasinRiverFeature[] = []

  for (const row of rows) {
    const geometryStatus = getM11SelectedSegmentGeometryBudgetStatus(row.geometry)
    if (!geometryStatus.sanitizedGeometry) {
      skippedCount += 1
      continue
    }

    const candidate: BasinRiverFeature = {
      type: 'Feature',
      geometry: geometryStatus.sanitizedGeometry,
      properties: {
        segment_id: row.segmentId,
        river_segment_id: row.riverSegmentId,
        basin_version_id: row.basinVersionId,
        river_network_version_id: row.riverNetworkVersionId,
        segment_name: row.displayName,
        q_value: row.currentQ,
        q_unit: row.qUnit,
        layer_color: m11BasinRiverLayerColor(row, layer),
      },
    }
    const candidateSerializedBytes = serializedByteLength(candidate)
    const nextFeatureCount = features.length + 1
    const nextCoordinateCount = coordinateCount + geometryStatus.coordinateCount
    const nextSerializedBytes =
      serializedByteLength({ type: 'FeatureCollection', features: [] }) +
      featureSerializedBytes +
      candidateSerializedBytes +
      Math.max(0, nextFeatureCount - 1)

    if (
      nextFeatureCount > m11BasinRiverCollectionBudget.maxFeatures ||
      nextCoordinateCount > m11BasinRiverCollectionBudget.maxCoordinates ||
      nextSerializedBytes > m11BasinRiverCollectionBudget.maxSerializedBytes
    ) {
      skippedCount += 1
      continue
    }

    features.push(candidate)
    coordinateCount = nextCoordinateCount
    featureSerializedBytes += candidateSerializedBytes
    serializedBytes = nextSerializedBytes
  }

  const sourceData = { type: 'FeatureCollection' as const, features }

  return {
    type: 'FeatureCollection',
    features,
    sourceData,
    skippedCount,
    coordinateCount,
    serializedBytes,
    unavailableReason:
      rows.length > 0 && features.length === 0
        ? '当前流域河段几何缺失或整体河网超过客户端渲染预算，地图不会注册过大的河网源。'
        : skippedCount > 0
          ? `${skippedCount} 条河段缺少可渲染几何或超出整体河网预算，已从地图河网中省略。`
          : null,
  }
}

export function buildM11RenderedNationalRiverCollection(
  nationalRiverGeo: FeatureCollection | null,
  meshRiverBasinIds: string[],
  dynamicRiverActive: boolean,
): FeatureCollection | null {
  if (!nationalRiverGeo || nationalRiverGeo.features.length === 0) return null
  if (!dynamicRiverActive || meshRiverBasinIds.length === 0) return nationalRiverGeo
  const excluded = new Set(meshRiverBasinIds)
  const features = nationalRiverGeo.features.filter(
    (feature: FeatureCollection['features'][number]) => !excluded.has(feature.properties?.basin_id as string),
  )
  if (features.length === nationalRiverGeo.features.length) return nationalRiverGeo
  return features.length > 0 ? { ...nationalRiverGeo, features } : null
}

export function buildSelectedSegmentFeatureCollection(
  selectedSegmentId: string | null | undefined,
  geometry:
    | components['schemas']['GeoJsonLineString']
    | components['schemas']['GeoJsonMultiLineString']
    | null
    | undefined,
): SelectedSegmentFeatureCollection {
  const geometryStatus = selectedSegmentId ? getM11SelectedSegmentGeometryBudgetStatus(geometry) : null
  return {
    type: 'FeatureCollection',
    features:
      selectedSegmentId && geometryStatus?.sanitizedGeometry
        ? [
            {
              type: 'Feature',
              geometry: geometryStatus.sanitizedGeometry,
              properties: { segment_id: selectedSegmentId },
            },
          ]
        : [],
    unavailableReason:
      selectedSegmentId && !geometryStatus?.sanitizedGeometry
        ? selectedSegmentUnavailableReason(geometryStatus?.reason)
        : null,
  }
}

export function m11SelectedLayerUnavailableReason(
  state: M11QueryState,
  layers: LayerState[],
  overlay: M11RegisteredOverlay | null,
  overlayData: FeatureCollection | null,
  hasBasinRiverNetwork = false,
) {
  if (overlay && (overlay.source.type === 'vector' || overlayData)) return null
  if (hasBasinRiverNetwork && state.layer === 'discharge') {
    return null
  }
  if (overlay) return '水文地图数据正在加载或已被客户端预算拦截，地图暂不显示该叠加层。'
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer) return '当前图层尚未由 /api/v1/layers 注册，地图不会渲染该叠加层。'
  if (!selectedLayer.available) return selectedLayer.disabledReason ?? '当前图层没有可渲染的有效时间。'
  if (!isNationalOverlayMetadata(selectedLayer.metadata) && !selectedLayer.freshness.runId) {
    return '当前图层缺少可追溯 run_id，地图不会注册叠加层。'
  }
  if (!selectedLayer.currentValidTime) return '当前图层缺少有效时间，地图不会注册叠加层。'
  if (state.layer === 'discharge') {
    return '当前水文图层缺少可用 MVT 元数据或处于 release-blocked 状态，地图不会请求无边界 GeoJSON 兼容源。'
  }
  return '当前图层缺少可用地图源，地图不会注册叠加层。'
}

export function segmentFilter(segmentId?: string | null): FilterSpecification {
  return [
    'any',
    ['==', ['get', 'river_segment_id'], segmentId ?? ''],
    ['==', ['get', 'segment_id'], segmentId ?? ''],
  ] as FilterSpecification
}

export function m11BasinLabelAnchor(geometry: BasinFeature['geometry']): [number, number] | null {
  let largestRing: number[][] | null = null
  for (const polygon of geometry?.coordinates ?? []) {
    const ring = (polygon as unknown as number[][][])[0]
    if (Array.isArray(ring) && (!largestRing || ring.length > largestRing.length)) largestRing = ring
  }
  if (!largestRing || largestRing.length === 0) return null
  let sumLon = 0
  let sumLat = 0
  let count = 0
  for (const position of largestRing) {
    const [lon, lat] = position
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
    sumLon += lon
    sumLat += lat
    count += 1
  }
  return count > 0 ? [sumLon / count, sumLat / count] : null
}

export function zoomScaledValueWidth(valueStops: number[], lowZoomFactor: number, logDomain = false) {
  const input = logDomain
    ? ['log10', ['max', ['coalesce', ['get', 'value'], 0], 0.01]]
    : ['coalesce', ['get', 'value'], 0]
  const widthAt = (scale: number) => [
    'interpolate',
    ['linear'],
    input,
    ...valueStops.map((stop, index) => (index % 2 === 1 ? Math.round(stop * scale * 100) / 100 : stop)),
  ]
  return ['interpolate', ['linear'], ['zoom'], 4, widthAt(lowZoomFactor), 7, widthAt(1)] as unknown as number
}

function dischargeTileLayerPaint(): LayerProps['paint'] {
  return {
    'line-color': [
      'interpolate',
      ['linear'],
      ['log10', ['max', ['coalesce', ['get', 'value'], 0], 0.01]],
      -2,
      '#7FB8DC',
      0,
      '#4292C6',
      1,
      '#2171B5',
      2,
      '#08519C',
      3,
      '#08306B',
      4,
      '#CB181D',
    ],
    'line-width': zoomScaledValueWidth([-2, 1.8, 0, 2.4, 2, 3.4, 4, 5, 4.7, 7], 0.4, true),
    'line-opacity': ['case', ['has', 'value'], 0.95, 0.5],
  }
}

function selectedSegmentUnavailableReason(reason: string | null | undefined) {
  if (!reason) return '选中河段缺少可渲染几何，地图不会绘制河段高亮。'
  if (reason.includes('serialized-size')) return '选中河段几何超过客户端序列化预算，地图不会绘制河段高亮。'
  if (reason.includes('rendering budget') || reason.includes('coordinate dimensions')) {
    return '选中河段几何超过客户端渲染预算，地图不会绘制河段高亮。'
  }
  if (reason.includes('at least two')) return '选中河段几何少于两个坐标点，地图不会绘制河段高亮。'
  return '选中河段几何格式无效，地图不会绘制河段高亮。'
}

function serializedByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
}

function normalizeIso(value: string | null | undefined) {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}
