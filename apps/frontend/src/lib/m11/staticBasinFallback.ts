import type { FeatureCollection } from 'geojson'

import type { components } from '@/api/types'
import type { M11Bbox, OverviewBasin } from '@/lib/m11/overviewDataContracts'

/**
 * 静态流域范围兜底（来自 public/geo/national-basin-domain.geojson，basin shp 溶解轮廓）。
 * 背景：DB 内 basin_version.geom 是 SHUD mesh 三角面碎片（数千 polygon / 数百 KB），
 * 被客户端几何预算正确拒绝。静态 domain 只提供轻量 bbox 给相机 fit；产品不再把
 * 任何流域边界或边界关联的地图名称标记注入地图。
 * honest：静态文件缺失/无匹配 basin_id → 不回填，保持原状。
 */

export interface StaticBasinBbox {
  bbox: M11Bbox
}

export function staticBasinBboxIndex(domain: FeatureCollection | null | undefined): Map<string, StaticBasinBbox> {
  const index = new Map<string, StaticBasinBbox>()
  if (!domain?.features) return index
  for (const feature of domain.features) {
    const basinId = feature.properties?.basin_id
    if (typeof basinId !== 'string' || basinId.length === 0) continue
    const boundary = toMultiPolygon(feature.geometry)
    if (!boundary) continue
    const bbox = multiPolygonBbox(boundary)
    if (!bbox) continue
    index.set(basinId, { bbox })
  }
  return index
}

/** 仅在服务端 bbox 缺失时用静态轮廓计算的范围回填；绝不回填 boundary。 */
export function withStaticBasinBboxes(basins: OverviewBasin[], domain: FeatureCollection | null | undefined): OverviewBasin[] {
  if (basins.length === 0) return basins
  const index = staticBasinBboxIndex(domain)
  if (index.size === 0) return basins
  let changed = false
  const next = basins.map((basin) => {
    if (basin.bbox) return basin
    const fallback = index.get(basin.basinId)
    if (!fallback) return basin
    changed = true
    return { ...basin, bbox: fallback.bbox }
  })
  return changed ? next : basins
}

function toMultiPolygon(geometry: GeoJSON.Geometry | null | undefined): components['schemas']['GeoJsonMultiPolygon'] | null {
  if (!geometry) return null
  if (geometry.type === 'MultiPolygon') {
    return geometry as components['schemas']['GeoJsonMultiPolygon']
  }
  if (geometry.type === 'Polygon') {
    return { type: 'MultiPolygon', coordinates: [geometry.coordinates] } as components['schemas']['GeoJsonMultiPolygon']
  }
  return null
}

function multiPolygonBbox(geometry: components['schemas']['GeoJsonMultiPolygon']): M11Bbox | null {
  let minLon = Number.POSITIVE_INFINITY
  let minLat = Number.POSITIVE_INFINITY
  let maxLon = Number.NEGATIVE_INFINITY
  let maxLat = Number.NEGATIVE_INFINITY
  for (const polygon of geometry.coordinates ?? []) {
    for (const ring of polygon ?? []) {
      for (const position of ring ?? []) {
        const [lon, lat] = position as unknown as [number, number]
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
        minLon = Math.min(minLon, lon)
        minLat = Math.min(minLat, lat)
        maxLon = Math.max(maxLon, lon)
        maxLat = Math.max(maxLat, lat)
      }
    }
  }
  if (!Number.isFinite(minLon) || !Number.isFinite(minLat) || !Number.isFinite(maxLon) || !Number.isFinite(maxLat)) return null
  return { minLon, minLat, maxLon, maxLat }
}
