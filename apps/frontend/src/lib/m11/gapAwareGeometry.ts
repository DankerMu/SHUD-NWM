import type { components } from '@/api/types'

type LineString = components['schemas']['GeoJsonLineString']
type MultiLineString = components['schemas']['GeoJsonMultiLineString']
type Position = number[]

// 河段几何"假桥接"判据。
//
// mesh 河网相邻顶点间距很均匀（qhh 实测：中位 ~75–118m、p90=119m），但一条 reach 由源
// GIS 里本就断开的多部件经贪心最近端点拼接（workers/model_registry/basins_geometry.py
// _merge_polyline_parts）后，跨缝的最短桥接边可达 300–1721m（qhh：>300m 119 段 / >1.2km
// 15 段）。geom 列锁死 `geometry(LineString,4490)`，后端无法表达缝隙，只能把缝桥成直线；
// 这条跨缝直线是拼接产物、不是真实河道。gap-aware 渲染在前端把它拆开、不再绘制。
//
// 阈值 = max(绝对下限, 相对倍数 × 本段中位边)：相对项护住粗网格流域（中位边大）不被误拆，
// 绝对项保证中位边很小的段也按统一物理尺度判缝。真实缝隙相对中位边为 10–23×，留足余量。
export const RIVER_GAP_ABSOLUTE_M = 300
export const RIVER_GAP_RELATIVE = 4

const EARTH_RADIUS_M = 6_371_000

// 等距圆柱近似的两点地面距离（米）。坐标为经纬度（EPSG:4490≈WGS84），仅用于阈值判断，
// 量级足够、无需 haversine 精度。
function edgeMeters(a: Position, b: Position): number {
  const latRad = (((a[1] ?? 0) + (b[1] ?? 0)) / 2) * (Math.PI / 180)
  const dx = ((b[0] ?? 0) - (a[0] ?? 0)) * (Math.PI / 180) * Math.cos(latRad) * EARTH_RADIUS_M
  const dy = ((b[1] ?? 0) - (a[1] ?? 0)) * (Math.PI / 180) * EARTH_RADIUS_M
  return Math.hypot(dx, dy)
}

function medianEdge(edges: number[]): number {
  if (edges.length === 0) return 0
  const sorted = [...edges].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)] ?? 0
}

/**
 * 把一条折线按"跨缝直线"切成多段连续折线；无缝则原样返回单段。
 * 跨缝边两侧分属不同段，缝本身（那条长直线）不出现在任何输出段里。
 * 被两条缝夹住的孤立单点无法成线，直接丢弃（与后端 _merge_polyline_parts 的 <2 点丢弃一致）。
 */
export function splitPositionsAtGaps(coords: Position[]): Position[][] {
  if (coords.length < 2) return [coords]
  const edges: number[] = []
  for (let i = 1; i < coords.length; i += 1) edges.push(edgeMeters(coords[i - 1], coords[i]))
  const threshold = Math.max(RIVER_GAP_ABSOLUTE_M, RIVER_GAP_RELATIVE * medianEdge(edges))
  const parts: Position[][] = []
  let current: Position[] = [coords[0]]
  for (let i = 0; i < edges.length; i += 1) {
    if (edges[i] > threshold) {
      if (current.length >= 2) parts.push(current)
      current = [coords[i + 1]]
    } else {
      current.push(coords[i + 1])
    }
  }
  if (current.length >= 2) parts.push(current)
  // 退化保护：理论上不会全拆成 <2 点；真发生则回退原折线，绝不返回空几何。
  return parts.length > 0 ? parts : [coords]
}

/**
 * gap-aware 渲染入口。后端自 #532 源头修复后已把河段几何按缝拆成 MultiLineString
 * （geom 列改 MultiLineString + gap_split），正常数据各部件已无缝、原样按引用直通。
 * 仍对每个部件做防御性再拆，护住两类漏网：① LineString 旧数据 / 单 run 选中段；
 * ② 迁移已改列但回填脚本未跑的窗口里，单部件 MultiLineString 仍把跨缝直线裹在一个 part 内。
 * 已拆好的输入再拆是恒等（各 part split 回自身、部件数不变），故按引用直通、不重建。
 * 只改几何分组、不动属性，故 river_segment_id 等仍属同一 feature，hover/点击/高亮照常命中。
 */
export function gapAwareLineGeometry(geometry: LineString | MultiLineString): LineString | MultiLineString {
  if (geometry.type === 'MultiLineString') {
    const parts = geometry.coordinates.flatMap((part) => splitPositionsAtGaps(part))
    // 部件数不变 ⇒ 无 part 含缝、各自 split 回自身 ⇒ 原样返回，避免无谓重建坐标。
    if (parts.length === geometry.coordinates.length) return geometry
    return { type: 'MultiLineString', coordinates: parts }
  }
  const parts = splitPositionsAtGaps(geometry.coordinates)
  if (parts.length <= 1) return geometry
  return { type: 'MultiLineString', coordinates: parts }
}
