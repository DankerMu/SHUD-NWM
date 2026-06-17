import { describe, expect, it } from 'vitest'

import { gapAwareLineGeometry, splitPositionsAtGaps } from './gapAwareGeometry'

// 用纬度步长造可预测边长：在 lat≈38 处，Δlat° × (π/180) × 6_371_000 ≈ Δlat° × 111_195 m。
// 0.0007°≈78m（mesh 正常边）、0.003°≈334m（刚过 300m 绝对下限）、0.015°≈1668m（典型源缝隙）、
// 0.004°≈445m（粗网格均匀边）。经度恒定，cos 因子不参与，距离仅由 Δlat 决定。
const LON = 98.5
const vline = (...lats: number[]): number[][] => lats.map((lat) => [LON, lat])

describe('gapAwareLineGeometry', () => {
  it('无缝细折线原样直通（同一对象、不拆）', () => {
    const geom = { type: 'LineString' as const, coordinates: vline(38.0, 38.0007, 38.0014, 38.0021, 38.0028) }
    const out = gapAwareLineGeometry(geom)
    expect(out).toBe(geom)
    expect(out.type).toBe('LineString')
  })

  it('单条跨缝直线 → 拆成两段 MultiLineString，缝本身不在任何段里', () => {
    // 78,78 | 1668(缝) | 78,78
    const geom = { type: 'LineString' as const, coordinates: vline(38.0, 38.0007, 38.0014, 38.0164, 38.0171, 38.0178) }
    const out = gapAwareLineGeometry(geom)
    expect(out.type).toBe('MultiLineString')
    const parts = out.coordinates as number[][][]
    expect(parts).toHaveLength(2)
    expect(parts[0]).toEqual(vline(38.0, 38.0007, 38.0014))
    expect(parts[1]).toEqual(vline(38.0164, 38.0171, 38.0178))
    // 没有任何输出段把缝两端（38.0014→38.0164）连起来
    for (const part of parts) {
      for (let i = 1; i < part.length; i += 1) {
        expect(part[i][1] - part[i - 1][1]).toBeLessThan(0.01)
      }
    }
  })

  it('两条缝 → 拆成三段', () => {
    const geom = {
      type: 'LineString' as const,
      coordinates: vline(38.0, 38.0007, 38.02, 38.0207, 38.04, 38.0407),
    }
    const out = gapAwareLineGeometry(geom)
    expect(out.type).toBe('MultiLineString')
    expect((out.coordinates as number[][][])).toHaveLength(3)
  })

  it('均匀粗网格折线（边长 ~445m）不被误拆——相对阈值护住', () => {
    const geom = { type: 'LineString' as const, coordinates: vline(38.0, 38.004, 38.008, 38.012, 38.016) }
    const out = gapAwareLineGeometry(geom)
    expect(out.type).toBe('LineString')
    expect(out).toBe(geom)
  })

  it('中位边很小的段里 ~334m 缝仍被绝对下限判出', () => {
    // 56,56,56 | 334(缝) | 56
    const geom = { type: 'LineString' as const, coordinates: vline(38.0, 38.0005, 38.001, 38.0015, 38.0045, 38.005) }
    const out = gapAwareLineGeometry(geom)
    expect(out.type).toBe('MultiLineString')
    const parts = out.coordinates as number[][][]
    expect(parts).toHaveLength(2)
    expect(parts[0]).toHaveLength(4)
    expect(parts[1]).toHaveLength(2)
  })

  it('被两条缝夹住的孤立单点直接丢弃（无法成线）', () => {
    const coords = vline(38.0, 38.0007, 38.0014, 38.02, 38.04, 38.0407, 38.0414, 38.0421)
    const parts = splitPositionsAtGaps(coords)
    expect(parts).toHaveLength(2)
    expect(parts[0]).toEqual(vline(38.0, 38.0007, 38.0014))
    expect(parts[1]).toEqual(vline(38.04, 38.0407, 38.0414, 38.0421))
    // 孤立点 38.02 不出现在任何段
    expect(parts.flat().some((p) => p[1] === 38.02)).toBe(false)
  })

  it('少于两点的退化几何不抛错、原样返回', () => {
    const single = { type: 'LineString' as const, coordinates: vline(38.0) }
    expect(gapAwareLineGeometry(single)).toBe(single)
    const empty = { type: 'LineString' as const, coordinates: [] as number[][] }
    expect(gapAwareLineGeometry(empty)).toBe(empty)
  })

  it('MultiLineString 直通（源已按缝拆好，不再二次处理）', () => {
    // 即便某段内部含跨缝直线，MultiLineString 入参也原样返回——后端源头已拆分，
    // 前端不重复拆（避免双重处理与坐标重算）。
    const mls = {
      type: 'MultiLineString' as const,
      coordinates: [vline(38.0, 38.0007, 38.0164), vline(38.04, 38.0407)],
    }
    expect(gapAwareLineGeometry(mls)).toBe(mls)
  })
})
