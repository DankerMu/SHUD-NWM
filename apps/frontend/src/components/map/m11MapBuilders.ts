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
  resolveNationalScaleSource,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import type { M11Layer, M11QueryState } from '@/lib/m11/queryState'

/**
 * `LayerProps` 是按 `type` 判别的联合（background/circle/fill/line/...）。本模块注册的 overlay 恒为
 * line 图层，直接用联合会让 `paint` / `layout` / `source-layer` 退化成所有变体的公共部分。
 */
export type M11LineLayerProps = Extract<LayerProps, { type: 'line' }>

export interface M11RegisteredOverlay {
  layerId: M11Layer
  sourceId: string
  sourceKey: string
  layer: M11LineLayerProps
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

// 产品口径：全国/流域详情地图不展示流域边界，也不展示依附该边界的流域名称标记。
// bbox 仍由 overview 数据保留，用于相机定位，不参与此 GeoJSON collection。
export const m11BasinBoundaryOverlayEnabled = false

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

  // 时次校验一律对活动 `(source, cycle)` 的列表（`LayerState.validTimes`，由 store 按活动周期填充），
  // 且按秒精度比对：state 里是毫秒形 `…T06:00:00.000Z`，API 列表是 `…T06:00:00Z`。
  const selectedValidTime = toSecondsPrecisionInstant(state.validTime)
  const inActiveList =
    selectedValidTime !== null &&
    selectedLayer.validTimes.some((candidate) => toSecondsPrecisionInstant(candidate) === selectedValidTime)
  const validTime = inActiveList ? selectedValidTime : toSecondsPrecisionInstant(selectedLayer.currentValidTime)
  if (!validTime) return null

  const metadata = selectedLayer.metadata
  if (!isMvtLayerMetadata(metadata) || metadata.release_blocking) return null

  const national = isNationalOverlayMetadata(metadata)
  // 全国模板的时次由上面的活动列表把关：目录里的 `metadata.valid_times` 只带**默认周期**的列表，
  // 用 metadataHasValidTime 校验会让任何非默认周期恒得到 overlay=null（本 issue 的 Current behavior）。
  if (!national && !metadataHasValidTime(metadata, validTime)) return null

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

  // 全国 source/cycle 模板：`best` 归一为 `gfs`（全国尺度），`compare` 没有对应瓦片路由 → 不注册。
  const nationalSource = national ? resolveNationalScaleSource(state.source) : null
  const nationalCycle = national ? resolveNationalOverlayCycle(selectedLayer) : null
  if (national) {
    if (templateNeeds(metadata, 'source') && nationalSource !== 'gfs' && nationalSource !== 'ifs') return null
    // 章为空即 fail-closed（目录 `default_cycle` 为空、或活动源的周期解不出来）：无论 URL 上
    // 有没有 cycle 都不注册叠加层，也就不会有任何含字面 `{cycle}`、自造周期或跨源周期的瓦片请求。
    if (templateNeeds(metadata, 'cycle') && !nationalCycle) return null
  }

  const sourceId = `m11-${state.layer}-source`
  const layerId = `m11-${state.layer}-line`
  const variable = 'q_down'
  const replacements: Record<string, string> = national
    ? {
        valid_time: validTime,
        variable,
        ...(nationalSource ? { source: nationalSource } : {}),
        ...(nationalCycle ? { cycle: nationalCycle } : {}),
      }
    : { run_id: runId as string, valid_time: validTime, variable }

  return {
    layerId: state.layer,
    sourceId,
    sourceKey: m11VectorSourceKey({
      layerId: selectedLayer.layerId,
      runId: national ? null : runId,
      source: national ? nationalSource : null,
      cycle: national ? nationalCycle : null,
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

/**
 * MapLibre source 身份：必须随 source / cycle / validTime **任一**变化而变化，否则切周期时
 * MapLibre 会复用旧 source，地图静默显示上一周期的数据（spec mvt-tile-contract 的 fixture
 * scenario「the `m11VectorSourceKey` case MUST assert a key that distinguishes (source, cycle,
 * valid_time) rather than run_id」）。`run_id` 保留给流域详情的单 run 路径，全国路径传 null。
 */
export function m11VectorSourceKey({
  layerId,
  runId,
  source,
  cycle,
  validTime,
  variable,
  metadata,
}: {
  layerId: string
  runId: string | null
  source?: string | null
  cycle?: string | null
  validTime: string
  variable: string
  metadata: NonNullable<LayerState['metadata']>
}): string {
  return JSON.stringify({
    basin_version_id: metadata.source_refs?.basin_version_id ?? null,
    cache_etag: metadata.cache_etag ?? null,
    cache_version: metadata.cache_version ?? null,
    canonical_route_layer_id: metadata.canonical_route_layer_id ?? metadata.layer_id,
    cycle: cycle ?? null,
    encoder_version: metadata.encoder_version ?? null,
    layer_id: layerId,
    maplibre_source_layer: metadata.maplibre_source_layer,
    run_id: runId,
    schema_version: metadata.schema_version ?? metadata.property_schema_version ?? null,
    source: source ?? null,
    source_refs: metadata.source_refs ?? null,
    valid_time: validTime,
    variable,
  })
}

/** 模板/必需占位符里是否真的要求该占位符（旧 run-agnostic alias 模板不含 source/cycle）。 */
function templateNeeds(metadata: MvtLayerMetadata, placeholder: 'source' | 'cycle'): boolean {
  return (
    metadata.url_template.includes(`{${placeholder}}`) ||
    (Array.isArray(metadata.required_placeholders) && metadata.required_placeholders.includes(placeholder))
  )
}

/**
 * 有效周期 = store 盖在这批时次上的章（`LayerState.activeNationalCycle`），只做秒精度归一。
 *
 * **这里不再有第二份「哪个周期是活动的」规则**（#2014 决策 13 孪生要求）。解析规则只在
 * `nationalDischargeActivePair`（`apps/frontend/src/stores/overviewData.ts`）一处：URL 周期，
 * 否则该源自己声明的默认周期（默认源才是目录的 `metadata.default_cycle`）。
 * 曾经的 `state.cycle ?? metadata.default_cycle` 与源无关，而 `metadata.default_cycle` 是
 * **GFS 专有事实**（后端 `list_layers` 签名里没有 `source`，`_default_layer_catalog` 固定按
 * `NATIONAL_DISCHARGE_DEFAULT_SOURCE` 计算）：按源分叉的活动对落地后，`source=ifs` + URL 无 cycle
 * 这个正常终态会拼出 `/hydro-national/ifs/<C_gfs>/q_down/<ifs 有效时刻>/…` —— 一个从未存在过的
 * 三元组，比「没有图层」更糟（它会真的去取瓦片）。故本函数**不得**再回落 `metadata.default_cycle`：
 * 章为空 = store 没解出周期（fail-closed / 该源 cycles 未到达或取回失败）= 不注册叠加层。
 * 章与 `LayerState.validTimes` 同批产出，于是 cycle 段与 valid_time 段按构造属于同一个身份。
 */
export function resolveNationalOverlayCycle(layer: LayerState): string | null {
  return toSecondsPrecisionInstant(layer.activeNationalCycle)
}

export function buildBasinFeatureCollection(basins: OverviewBasin[], visibleBasinIds: string[] | undefined): BasinFeatureCollection {
  if (!m11BasinBoundaryOverlayEnabled) return { type: 'FeatureCollection', features: [] }
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
  if (!m11BasinBoundaryOverlayEnabled) return 0
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

function dischargeTileLayerPaint(): M11LineLayerProps['paint'] {
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
