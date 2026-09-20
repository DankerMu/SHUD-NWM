import type { components } from '@/api/types'
import { finiteNumberOrNull, numberOrNull } from '@/lib/m11/overviewDataContractPrimitives'
import {
  m11BasinGeometryBudget,
  m11SelectedSegmentGeometryBudget,
  type M11BasinGeometryBudgetStatus,
  type M11Bbox,
  type M11SelectedSegmentGeometryBudgetStatus,
} from '@/lib/m11/overviewDataContractTypes'

export function getM11BasinGeometryBudgetStatus(
  geom: components['schemas']['GeoJsonMultiPolygon'] | null | undefined,
): M11BasinGeometryBudgetStatus {
  if (!geom?.coordinates) {
    return geometryStatus(false, 'Basin geometry is unavailable.', null, 0, 0, 0, 0, null)
  }
  if (geom.type !== 'MultiPolygon' || !Array.isArray(geom.coordinates)) {
    return geometryStatus(false, 'Basin geometry is malformed.', null, 0, 0, 0, serializedByteLength(geom), null)
  }

  let bbox: M11Bbox | null = null
  let polygonCount = 0
  let ringCount = 0
  let vertexCount = 0
  const sanitizedCoordinates: number[][][][] = []

  for (const polygon of geom.coordinates as unknown[]) {
    if (!Array.isArray(polygon)) return geometryMalformed(polygonCount, ringCount, vertexCount)
    polygonCount += 1
    if (polygonCount > m11BasinGeometryBudget.maxPolygons) return geometryTooLarge(polygonCount, ringCount, vertexCount)
    const sanitizedPolygon: number[][][] = []

    for (const ring of polygon) {
      if (!Array.isArray(ring)) return geometryMalformed(polygonCount, ringCount, vertexCount)
      ringCount += 1
      if (ringCount > m11BasinGeometryBudget.maxRings) return geometryTooLarge(polygonCount, ringCount, vertexCount)
      const sanitizedRing: number[][] = []

      for (const coordinate of ring) {
        if (!Array.isArray(coordinate) || coordinate.length < 2) return geometryMalformed(polygonCount, ringCount, vertexCount)
        if (coordinate.length > m11BasinGeometryBudget.maxCoordinateDimensions) {
          return geometryTooWide(polygonCount, ringCount, vertexCount + 1)
        }
        vertexCount += 1
        if (vertexCount > m11BasinGeometryBudget.maxVertices) return geometryTooLarge(polygonCount, ringCount, vertexCount)

        const lon = numberOrNull(coordinate[0])
        const lat = numberOrNull(coordinate[1])
        if (lon === null || lat === null) return geometryMalformed(polygonCount, ringCount, vertexCount)
        const elevation = coordinate.length >= 3 ? numberOrNull(coordinate[2]) : null
        if (coordinate.length >= 3 && elevation === null) return geometryMalformed(polygonCount, ringCount, vertexCount)
        sanitizedRing.push(elevation === null ? [lon, lat] : [lon, lat, elevation])
        bbox = bbox
          ? {
              minLon: Math.min(bbox.minLon, lon),
              minLat: Math.min(bbox.minLat, lat),
              maxLon: Math.max(bbox.maxLon, lon),
              maxLat: Math.max(bbox.maxLat, lat),
            }
          : { minLon: lon, minLat: lat, maxLon: lon, maxLat: lat }
      }
      if (sanitizedRing.length < 4 || !basinRingIsClosed(sanitizedRing)) {
        return geometryMalformed(polygonCount, ringCount, vertexCount)
      }
      sanitizedPolygon.push(sanitizedRing)
    }
    sanitizedCoordinates.push(sanitizedPolygon)
  }

  if (!bbox) return geometryStatus(false, 'Basin geometry is unavailable.', null, polygonCount, ringCount, vertexCount, 0, null)
  const sanitizedGeometry = { type: 'MultiPolygon' as const, coordinates: sanitizedCoordinates }
  const serializedBytes = serializedByteLength(sanitizedGeometry)
  if (serializedBytes > m11BasinGeometryBudget.maxSerializedBytes) {
    return geometryTooManyBytes(polygonCount, ringCount, vertexCount, serializedBytes)
  }
  return geometryStatus(true, null, bbox, polygonCount, ringCount, vertexCount, serializedBytes, sanitizedGeometry)
}

// 河段几何自 #532 源头修复后为 LineString | MultiLineString（geom 列改 MultiLineString）。
// 预算校验对二者统一：坐标计数递归 MultiLineString 的嵌套（多一层 part），各 part 至少两点、
// 序列化字节用最终 sanitized 几何计。拆分不增顶点，故同一河段的预算与 LineString 时基本不变。
function sanitizeSegmentLineCoordinates(
  rawCoordinates: unknown,
  startCount: number,
): { ok: true; coordinates: number[][]; coordinateCount: number } | { ok: false; status: M11SelectedSegmentGeometryBudgetStatus } {
  if (!Array.isArray(rawCoordinates)) {
    return { ok: false, status: selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', startCount, 0, null) }
  }
  const sanitized: number[][] = []
  let coordinateCount = startCount
  for (const coordinate of rawCoordinates as unknown[]) {
    if (!Array.isArray(coordinate) || coordinate.length < 2) {
      return { ok: false, status: selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null) }
    }
    if (coordinate.length > m11SelectedSegmentGeometryBudget.maxCoordinateDimensions) {
      return {
        ok: false,
        status: selectedSegmentGeometryStatus(
          false,
          `Selected segment geometry coordinate dimensions exceed client rendering budget (${m11SelectedSegmentGeometryBudget.maxCoordinateDimensions}).`,
          coordinateCount + 1,
          0,
          null,
        ),
      }
    }
    coordinateCount += 1
    if (coordinateCount > m11SelectedSegmentGeometryBudget.maxCoordinates) {
      return {
        ok: false,
        status: selectedSegmentGeometryStatus(
          false,
          `Selected segment geometry exceeds client rendering budget (${coordinateCount}/${m11SelectedSegmentGeometryBudget.maxCoordinates} coordinates).`,
          coordinateCount,
          0,
          null,
        ),
      }
    }
    const lon = finiteNumberOrNull(coordinate[0])
    const lat = finiteNumberOrNull(coordinate[1])
    if (lon === null || lat === null) {
      return { ok: false, status: selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null) }
    }
    const elevation = coordinate.length >= 3 ? finiteNumberOrNull(coordinate[2]) : null
    if (coordinate.length >= 3 && elevation === null) {
      return { ok: false, status: selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null) }
    }
    sanitized.push(elevation === null ? [lon, lat] : [lon, lat, elevation])
  }
  return { ok: true, coordinates: sanitized, coordinateCount }
}

function finalizeSelectedSegmentGeometry(
  sanitizedGeometry: components['schemas']['GeoJsonLineString'] | components['schemas']['GeoJsonMultiLineString'],
  coordinateCount: number,
): M11SelectedSegmentGeometryBudgetStatus {
  const serializedBytes = serializedByteLength(sanitizedGeometry)
  if (serializedBytes > m11SelectedSegmentGeometryBudget.maxSerializedBytes) {
    return selectedSegmentGeometryStatus(
      false,
      `Selected segment geometry exceeds client serialized-size budget (${serializedBytes}/${m11SelectedSegmentGeometryBudget.maxSerializedBytes} bytes).`,
      coordinateCount,
      serializedBytes,
      null,
    )
  }
  return selectedSegmentGeometryStatus(true, null, coordinateCount, serializedBytes, sanitizedGeometry)
}

export function getM11SelectedSegmentGeometryBudgetStatus(
  geom:
    | components['schemas']['GeoJsonLineString']
    | components['schemas']['GeoJsonMultiLineString']
    | null
    | undefined,
): M11SelectedSegmentGeometryBudgetStatus {
  if (!geom?.coordinates) {
    return selectedSegmentGeometryStatus(false, 'Selected segment geometry is unavailable.', 0, 0, null)
  }

  if (geom.type === 'LineString' && Array.isArray(geom.coordinates)) {
    const sanitized = sanitizeSegmentLineCoordinates(geom.coordinates, 0)
    if (!sanitized.ok) return sanitized.status
    if (sanitized.coordinates.length < 2) {
      return selectedSegmentGeometryStatus(false, 'Selected segment geometry requires at least two coordinates.', sanitized.coordinateCount, 0, null)
    }
    return finalizeSelectedSegmentGeometry({ type: 'LineString', coordinates: sanitized.coordinates }, sanitized.coordinateCount)
  }

  if (geom.type === 'MultiLineString' && Array.isArray(geom.coordinates)) {
    const sanitizedParts: number[][][] = []
    let coordinateCount = 0
    for (const part of geom.coordinates as unknown[]) {
      const sanitized = sanitizeSegmentLineCoordinates(part, coordinateCount)
      if (!sanitized.ok) return sanitized.status
      coordinateCount = sanitized.coordinateCount
      if (sanitized.coordinates.length >= 2) sanitizedParts.push(sanitized.coordinates)
    }
    if (sanitizedParts.length === 0) {
      return selectedSegmentGeometryStatus(false, 'Selected segment geometry requires at least two coordinates.', coordinateCount, 0, null)
    }
    return finalizeSelectedSegmentGeometry({ type: 'MultiLineString', coordinates: sanitizedParts }, coordinateCount)
  }

  return selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', 0, serializedByteLength(geom), null)
}

function geometryTooLarge(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry exceeds client rendering budget (${vertexCount}/${m11BasinGeometryBudget.maxVertices} vertices).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    0,
    null,
  )
}

function geometryTooWide(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry coordinate dimensions exceed client rendering budget (${m11BasinGeometryBudget.maxCoordinateDimensions}).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    0,
    null,
  )
}

function geometryTooManyBytes(
  polygonCount: number,
  ringCount: number,
  vertexCount: number,
  serializedBytes: number,
): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry exceeds client serialized-size budget (${serializedBytes}/${m11BasinGeometryBudget.maxSerializedBytes} bytes).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    serializedBytes,
    null,
  )
}

function geometryMalformed(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(false, 'Basin geometry is malformed.', null, polygonCount, ringCount, vertexCount, 0, null)
}

function basinRingIsClosed(ring: number[][]) {
  const first = ring[0]
  const last = ring[ring.length - 1]
  if (!first || !last || first.length !== last.length) return false
  return first.every((coordinate, index) => coordinate === last[index])
}

function geometryStatus(
  ok: boolean,
  reason: string | null,
  bbox: M11Bbox | null,
  polygonCount: number,
  ringCount: number,
  vertexCount: number,
  serializedBytes: number,
  sanitizedGeometry: components['schemas']['GeoJsonMultiPolygon'] | null,
): M11BasinGeometryBudgetStatus {
  return { ok, reason, bbox, polygonCount, ringCount, vertexCount, serializedBytes, sanitizedGeometry }
}

function selectedSegmentGeometryStatus(
  ok: boolean,
  reason: string | null,
  coordinateCount: number,
  serializedBytes: number,
  sanitizedGeometry:
    | components['schemas']['GeoJsonLineString']
    | components['schemas']['GeoJsonMultiLineString']
    | null,
): M11SelectedSegmentGeometryBudgetStatus {
  return { ok, reason, coordinateCount, serializedBytes, sanitizedGeometry }
}

function serializedByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
}
