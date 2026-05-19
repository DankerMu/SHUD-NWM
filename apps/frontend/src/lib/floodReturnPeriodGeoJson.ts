import { apiFetch, buildApiUrl } from '@/api/base'

export const floodReturnPeriodGeoJsonBudget = {
  maxFeatures: 10_000,
  maxCoordinates: 100_000,
  maxCoordinateDimensions: 3,
  maxSerializedBytes: 2_000_000,
} as const

type FloodGeometryType =
  | 'Point'
  | 'MultiPoint'
  | 'LineString'
  | 'MultiLineString'
  | 'Polygon'
  | 'MultiPolygon'
  | 'GeometryCollection'

export interface FloodReturnPeriodGeometry {
  type: FloodGeometryType
  coordinates?: unknown
  geometries?: FloodReturnPeriodGeometry[]
}

export interface FloodReturnPeriodFeature {
  type: 'Feature'
  id?: string | number
  properties: FloodReturnPeriodFeatureProperties | null
  geometry: FloodReturnPeriodGeometry | null
}

export interface FloodReturnPeriodFeatureCollection {
  type: 'FeatureCollection'
  features: FloodReturnPeriodFeature[]
}

export type FloodReturnPeriodFeatureProperties = Record<string, unknown> & {
  feature_id?: string
  river_network_version_id?: string
  segment_id?: string
  river_segment_id?: string
}

export type FloodReturnPeriodRejectionCode =
  | 'http'
  | 'json'
  | 'shape'
  | 'feature_count'
  | 'coordinate_count'
  | 'coordinate_dimension'
  | 'malformed_geometry'
  | 'serialized_bytes'

export type FloodReturnPeriodValidationResult =
  | {
      ok: true
      data: FloodReturnPeriodFeatureCollection
      featureCount: number
      coordinateCount: number
      serializedBytes: number
    }
  | {
      ok: false
      code: FloodReturnPeriodRejectionCode
      reason: string
      featureCount: number
      coordinateCount: number
      serializedBytes: number
    }

interface FloodReturnPeriodValidationOptions {
  maxFeatures?: number
  maxCoordinates?: number
  maxCoordinateDimensions?: number
  maxSerializedBytes?: number
  serializedBytes?: number
}

interface GeometryValidationState {
  coordinateCount: number
  maxCoordinates: number
  maxCoordinateDimensions: number
}

type BoundedBodyReadResult =
  | { ok: true; body: string; serializedBytes: number }
  | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string; serializedBytes: number }

export function buildFloodReturnPeriodGeoJsonUrl(runId: string, validTime: string) {
  const params = new URLSearchParams({
    run_id: runId,
    duration: '1h',
    valid_time: validTime,
    limit: String(floodReturnPeriodGeoJsonBudget.maxFeatures),
  })
  return buildApiUrl(`/api/v1/tiles/flood-return-period?${params.toString()}`)
}

export async function fetchFloodReturnPeriodFeatureCollection(
  url: string,
  options: RequestInit & { budget?: FloodReturnPeriodValidationOptions } = {},
): Promise<FloodReturnPeriodValidationResult> {
  const { budget, ...requestInit } = options
  const maxSerializedBytes = budget?.maxSerializedBytes ?? floodReturnPeriodGeoJsonBudget.maxSerializedBytes

  const response = await apiFetch(url, requestInit)
  if (!response.ok) return rejection('http', '洪水重现期地图数据暂不可用，地图暂不显示该叠加层。', 0, 0, 0)

  const contentLengthHeader = response.headers.get('content-length')
  const contentLength = contentLengthHeader === null ? Number.NaN : Number(contentLengthHeader)
  if (Number.isFinite(contentLength) && contentLength > maxSerializedBytes) {
    return rejection(
      'serialized_bytes',
      `洪水重现期地图数据超过客户端序列化预算（${contentLength}/${maxSerializedBytes} bytes），地图暂不显示该叠加层。`,
      0,
      0,
      contentLength,
    )
  }

  const boundedBody = await readBoundedResponseText(response, maxSerializedBytes)
  if (!boundedBody.ok) return rejection(boundedBody.code, boundedBody.reason, 0, 0, boundedBody.serializedBytes)

  let payload: unknown
  try {
    payload = JSON.parse(boundedBody.body) as unknown
  } catch {
    return rejection('json', '洪水重现期地图数据不是有效 JSON，地图暂不显示该叠加层。', 0, 0, boundedBody.serializedBytes)
  }

  return validateFloodReturnPeriodFeatureCollection(payload, { ...budget, serializedBytes: boundedBody.serializedBytes })
}

export function validateFloodReturnPeriodFeatureCollection(
  payload: unknown,
  options: FloodReturnPeriodValidationOptions = {},
): FloodReturnPeriodValidationResult {
  const maxFeatures = options.maxFeatures ?? floodReturnPeriodGeoJsonBudget.maxFeatures
  const maxCoordinates = options.maxCoordinates ?? floodReturnPeriodGeoJsonBudget.maxCoordinates
  const maxCoordinateDimensions = options.maxCoordinateDimensions ?? floodReturnPeriodGeoJsonBudget.maxCoordinateDimensions
  const maxSerializedBytes = options.maxSerializedBytes ?? floodReturnPeriodGeoJsonBudget.maxSerializedBytes
  const serializedBytes = options.serializedBytes ?? serializedByteLength(payload)

  if (serializedBytes > maxSerializedBytes) {
    return rejection(
      'serialized_bytes',
      `洪水重现期地图数据超过客户端序列化预算（${serializedBytes}/${maxSerializedBytes} bytes），地图暂不显示该叠加层。`,
      0,
      0,
      serializedBytes,
    )
  }

  if (!isRecord(payload) || payload.type !== 'FeatureCollection' || !Array.isArray(payload.features)) {
    return rejection('shape', '洪水重现期地图数据不是有效 FeatureCollection，地图暂不显示该叠加层。', 0, 0, serializedBytes)
  }
  if (payload.features.length > maxFeatures) {
    return rejection(
      'feature_count',
      `洪水重现期地图数据超过客户端要素预算（${payload.features.length}/${maxFeatures} features），地图暂不显示该叠加层。`,
      payload.features.length,
      0,
      serializedBytes,
    )
  }

  const state: GeometryValidationState = { coordinateCount: 0, maxCoordinates, maxCoordinateDimensions }
  const features: FloodReturnPeriodFeature[] = []

  for (const feature of payload.features) {
    const sanitizedFeature = sanitizeFeature(feature, state)
    if (sanitizedFeature.ok) {
      features.push(sanitizedFeature.feature)
      continue
    }
    return rejection(sanitizedFeature.code, sanitizedFeature.reason, payload.features.length, state.coordinateCount, serializedBytes)
  }

  return { ok: true, data: { type: 'FeatureCollection', features }, featureCount: features.length, coordinateCount: state.coordinateCount, serializedBytes }
}

function sanitizeFeature(
  value: unknown,
  state: GeometryValidationState,
):
  | { ok: true; feature: FloodReturnPeriodFeature }
  | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  if (!isRecord(value) || value.type !== 'Feature' || !('geometry' in value)) {
    return { ok: false, code: 'shape', reason: '洪水重现期地图数据包含无效 Feature，要素图层暂不显示。' }
  }

  const geometryResult = sanitizeGeometry(value.geometry, state)
  if (!geometryResult.ok) return geometryResult

  const feature: FloodReturnPeriodFeature = {
    type: 'Feature',
    properties: normalizeFloodReturnPeriodFeatureProperties(value.properties),
    geometry: geometryResult.geometry,
  }
  if (typeof value.id === 'string' || typeof value.id === 'number') feature.id = value.id
  return { ok: true, feature }
}

export function floodReturnPeriodFeatureId(properties: FloodReturnPeriodFeatureProperties | null | undefined): string | null {
  if (!properties) return null
  if (typeof properties.feature_id === 'string' && properties.feature_id) return properties.feature_id
  const riverNetworkVersionId = typeof properties.river_network_version_id === 'string' ? properties.river_network_version_id : ''
  const segmentId =
    typeof properties.segment_id === 'string'
      ? properties.segment_id
      : typeof properties.river_segment_id === 'string'
        ? properties.river_segment_id
        : ''
  if (!riverNetworkVersionId || !segmentId) return null
  return `${riverNetworkVersionId}::${segmentId}`
}

function normalizeFloodReturnPeriodFeatureProperties(value: unknown): FloodReturnPeriodFeatureProperties | null {
  if (!isRecord(value)) return null
  const properties: FloodReturnPeriodFeatureProperties = { ...value }
  const featureId = floodReturnPeriodFeatureId(properties)
  if (featureId) properties.feature_id = featureId
  return properties
}

function sanitizeGeometry(
  value: unknown,
  state: GeometryValidationState,
):
  | { ok: true; geometry: FloodReturnPeriodGeometry | null }
  | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  if (value === null) return { ok: true, geometry: null }
  if (!isRecord(value) || typeof value.type !== 'string') {
    return { ok: false, code: 'malformed_geometry', reason: '洪水重现期地图数据包含畸形几何，地图暂不显示该叠加层。' }
  }

  if (value.type === 'GeometryCollection') {
    if (!Array.isArray(value.geometries)) {
      return { ok: false, code: 'malformed_geometry', reason: '洪水重现期地图数据包含畸形几何集合，地图暂不显示该叠加层。' }
    }
    const geometries: FloodReturnPeriodGeometry[] = []
    for (const geometry of value.geometries) {
      const result = sanitizeGeometry(geometry, state)
      if (!result.ok) return result
      if (result.geometry) geometries.push(result.geometry)
    }
    return { ok: true, geometry: { type: 'GeometryCollection', geometries } }
  }

  if (!isFloodGeometryType(value.type) || !('coordinates' in value)) {
    return { ok: false, code: 'malformed_geometry', reason: '洪水重现期地图数据包含不支持的几何类型，地图暂不显示该叠加层。' }
  }

  const coordinates = sanitizeCoordinatesForGeometry(value.type, value.coordinates, state)
  if (!coordinates.ok) return coordinates
  return { ok: true, geometry: { type: value.type, coordinates: coordinates.coordinates } }
}

type SanitizedPosition = number[]
type SanitizedLineString = SanitizedPosition[]
type SanitizedPolygon = SanitizedLineString[]
type SanitizedMultiLineString = SanitizedLineString[]
type SanitizedMultiPolygon = SanitizedPolygon[]

function sanitizeCoordinatesForGeometry(
  geometryType: Exclude<FloodGeometryType, 'GeometryCollection'>,
  value: unknown,
  state: GeometryValidationState,
):
  | { ok: true; coordinates: unknown }
  | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  const depthByGeometryType: Record<Exclude<FloodGeometryType, 'GeometryCollection'>, number> = {
    Point: 0,
    MultiPoint: 1,
    LineString: 1,
    MultiLineString: 2,
    Polygon: 2,
    MultiPolygon: 3,
  }
  const coordinates = sanitizeCoordinatesAtDepth(value, depthByGeometryType[geometryType], state, geometryType)
  if (!coordinates.ok) return coordinates

  switch (geometryType) {
    case 'Point':
      return { ok: true, coordinates: coordinates.coordinates as SanitizedPosition }
    case 'MultiPoint':
      return { ok: true, coordinates: coordinates.coordinates as SanitizedPosition[] }
    case 'LineString': {
      const line = coordinates.coordinates as SanitizedLineString
      if (line.length < 2) return malformedGeometry(`${geometryType} 至少需要 2 个坐标点。`)
      return { ok: true, coordinates: line }
    }
    case 'MultiLineString': {
      const lines = coordinates.coordinates as SanitizedMultiLineString
      if (lines.some((line) => line.length < 2)) return malformedGeometry(`${geometryType} 的每条线至少需要 2 个坐标点。`)
      return { ok: true, coordinates: lines }
    }
    case 'Polygon': {
      const polygon = coordinates.coordinates as SanitizedPolygon
      const ringResult = validatePolygonRings(polygon, geometryType)
      if (!ringResult.ok) return ringResult
      return { ok: true, coordinates: polygon }
    }
    case 'MultiPolygon': {
      const polygons = coordinates.coordinates as SanitizedMultiPolygon
      for (const polygon of polygons) {
        const ringResult = validatePolygonRings(polygon, geometryType)
        if (!ringResult.ok) return ringResult
      }
      return { ok: true, coordinates: polygons }
    }
  }
}

function sanitizeCoordinatesAtDepth(
  value: unknown,
  depth: number,
  state: GeometryValidationState,
  geometryType: Exclude<FloodGeometryType, 'GeometryCollection'>,
):
  | { ok: true; coordinates: unknown[] }
  | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  if (!Array.isArray(value)) {
    return { ok: false, code: 'malformed_geometry', reason: '洪水重现期地图数据包含畸形坐标，地图暂不显示该叠加层。' }
  }
  if (value.length === 0) {
    return { ok: false, code: 'malformed_geometry', reason: '洪水重现期地图数据包含空坐标几何，地图暂不显示该叠加层。' }
  }
  if (depth === 0) {
    if (!isCoordinate(value)) {
      return {
        ok: false,
        code: 'malformed_geometry',
        reason: `洪水重现期地图数据的 ${geometryType} 坐标层级与几何类型不匹配，地图暂不显示该叠加层。`,
      }
    }
    if (value.length > state.maxCoordinateDimensions) {
      return {
        ok: false,
        code: 'coordinate_dimension',
        reason: `洪水重现期地图坐标维度超过客户端预算（${state.maxCoordinateDimensions}），地图暂不显示该叠加层。`,
      }
    }
    state.coordinateCount += 1
    if (state.coordinateCount > state.maxCoordinates) {
      return {
        ok: false,
        code: 'coordinate_count',
        reason: `洪水重现期地图坐标数量超过客户端预算（${state.coordinateCount}/${state.maxCoordinates} coordinates），地图暂不显示该叠加层。`,
      }
    }
    return { ok: true, coordinates: [...value] }
  }

  if (isCoordinate(value)) {
    return {
      ok: false,
      code: 'malformed_geometry',
      reason: `洪水重现期地图数据的 ${geometryType} 坐标层级与几何类型不匹配，地图暂不显示该叠加层。`,
    }
  }

  const nested: unknown[] = []
  for (const item of value) {
    const child = sanitizeCoordinatesAtDepth(item, depth - 1, state, geometryType)
    if (!child.ok) return child
    nested.push(child.coordinates)
  }
  return { ok: true, coordinates: nested }
}

function validatePolygonRings(
  rings: SanitizedPolygon,
  geometryType: 'Polygon' | 'MultiPolygon',
): { ok: true } | { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  for (const ring of rings) {
    if (ring.length < 4) return malformedGeometry(`${geometryType} 的线性环至少需要 4 个坐标点。`)
    if (!coordinatesMatch(ring[0], ring[ring.length - 1])) {
      return malformedGeometry(`${geometryType} 的线性环首尾坐标必须一致。`)
    }
  }
  return { ok: true }
}

function coordinatesMatch(first: SanitizedPosition | undefined, last: SanitizedPosition | undefined) {
  if (!first || !last || first.length !== last.length) return false
  return first.every((coordinate, index) => coordinate === last[index])
}

function malformedGeometry(reason: string): { ok: false; code: FloodReturnPeriodRejectionCode; reason: string } {
  return {
    ok: false,
    code: 'malformed_geometry',
    reason: `洪水重现期地图数据包含畸形几何：${reason}地图暂不显示该叠加层。`,
  }
}

function isCoordinate(value: unknown[]): value is number[] {
  return value.length >= 2 && value.every((coordinate) => typeof coordinate === 'number' && Number.isFinite(coordinate))
}

function isFloodGeometryType(value: string): value is FloodGeometryType {
  return (
    value === 'Point' ||
    value === 'MultiPoint' ||
    value === 'LineString' ||
    value === 'MultiLineString' ||
    value === 'Polygon' ||
    value === 'MultiPolygon'
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function rejection(
  code: FloodReturnPeriodRejectionCode,
  reason: string,
  featureCount: number,
  coordinateCount: number,
  serializedBytes: number,
): FloodReturnPeriodValidationResult {
  return { ok: false, code, reason, featureCount, coordinateCount, serializedBytes }
}

function serializedByteLength(value: unknown): number {
  return byteLength(JSON.stringify(value))
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length
}

async function readBoundedResponseText(response: Response, maxSerializedBytes: number): Promise<BoundedBodyReadResult> {
  if (!response.body) {
    return {
      ok: false,
      code: 'serialized_bytes',
      reason: '当前浏览器无法对洪水重现期地图数据执行有界读取，地图暂不显示该叠加层。',
      serializedBytes: 0,
    }
  }

  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let serializedBytes = 0

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (!value) continue

      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value)
      serializedBytes += chunk.byteLength
      if (serializedBytes > maxSerializedBytes) {
        await reader.cancel().catch(() => undefined)
        return {
          ok: false,
          code: 'serialized_bytes',
          reason: `洪水重现期地图数据超过客户端序列化预算（${serializedBytes}/${maxSerializedBytes} bytes），地图暂不显示该叠加层。`,
          serializedBytes,
        }
      }
      chunks.push(chunk)
    }
  } finally {
    reader.releaseLock()
  }

  const bodyBytes = new Uint8Array(serializedBytes)
  let offset = 0
  chunks.forEach((chunk) => {
    bodyBytes.set(chunk, offset)
    offset += chunk.byteLength
  })
  return { ok: true, body: new TextDecoder().decode(bodyBytes), serializedBytes }
}
