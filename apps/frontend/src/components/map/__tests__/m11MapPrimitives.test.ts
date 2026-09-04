import {
  createPropertyExpression,
  v8,
  type Feature,
  type StylePropertyExpression,
  type StylePropertySpecification,
} from '@maplibre/maplibre-gl-style-spec'
import type { LineLayerSpecification } from 'maplibre-gl'
import { describe, expect, it } from 'vitest'

import { m11NationalRiverPaint, m11StationClusterPolicy } from '@/components/map/m11MapPrimitives'

describe('m11StationClusterPolicy', () => {
  it('renders small direct-grid station sets without clustering', () => {
    expect(m11StationClusterPolicy(0)).toEqual({ enabled: false, radius: 0, maxZoom: 0 })
    expect(m11StationClusterPolicy(24)).toEqual({ enabled: false, radius: 0, maxZoom: 0 })
  })

  it('uses bounded regional clustering for medium direct-grid station sets', () => {
    expect(m11StationClusterPolicy(25)).toEqual({ enabled: true, radius: 28, maxZoom: 7 })
    expect(m11StationClusterPolicy(500)).toEqual({ enabled: true, radius: 28, maxZoom: 7 })
  })

  it('keeps dense station sets clustered only through regional zoom', () => {
    expect(m11StationClusterPolicy(501)).toEqual({ enabled: true, radius: 36, maxZoom: 8 })
    expect(m11StationClusterPolicy(3_385)).toEqual({ enabled: true, radius: 36, maxZoom: 8 })
  })
})

const LINE_OPACITY_SPEC = (v8 as { paint_line: Record<string, StylePropertySpecification> }).paint_line['line-opacity']
const LINE_WIDTH_SPEC = (v8 as { paint_line: Record<string, StylePropertySpecification> }).paint_line['line-width']

type NationalRiverPaint = NonNullable<LineLayerSpecification['paint']>

function nationalRiverPaint(dimmed: boolean, satellite = false): NationalRiverPaint {
  return m11NationalRiverPaint({ dimmed, satellite }) as NationalRiverPaint
}

/**
 * 用真实的 MapLibre 表达式编译器编译（不是形状快照）。
 * `createPropertyExpression` 才做 `findZoomCurve` 校验——`createExpression` 不做——
 * 所以只有这条路径能拦住「非顶层 ['zoom']」这类 tsc 看不见、只在浏览器里炸的非法表达式。
 */
function compilePaintProperty(raw: unknown, spec: StylePropertySpecification): StylePropertyExpression {
  const compiled = createPropertyExpression(raw, spec)
  const errors = compiled.result === 'error' ? compiled.value.map((error) => error.message).join('; ') : ''
  expect(errors).toBe('')
  expect(compiled.result).toBe('success')
  if (compiled.result !== 'success') throw new Error(errors)
  // 数据驱动（读 ['get','Type']）+ zoom 驱动 => composite，GPU 在整数 zoom 之间插值。
  expect(compiled.value.kind).toBe('composite')
  return compiled.value
}

function riverFeature(type: number): Feature {
  return { type: 'LineString', properties: { Type: type } }
}

function opacityExpression(dimmed: boolean): StylePropertyExpression {
  return compilePaintProperty(nationalRiverPaint(dimmed)['line-opacity'], LINE_OPACITY_SPEC)
}

function widthExpression(dimmed: boolean): StylePropertyExpression {
  return compilePaintProperty(nationalRiverPaint(dimmed)['line-width'], LINE_WIDTH_SPEC)
}

function evaluateAt(expression: StylePropertyExpression, zoom: number, type: number): number {
  return expression.evaluate({ zoom }, riverFeature(type)) as number
}

describe('m11NationalRiverPaint line-width', () => {
  it('compiles against the real line-width property spec', () => {
    expect(widthExpression(false)).toBeTruthy()
    expect(widthExpression(true)).toBeTruthy()
  })

  it('keeps trunk widths independent of dimming and satellite basemap', () => {
    expect(nationalRiverPaint(true)['line-width']).toEqual(nationalRiverPaint(false)['line-width'])
    expect(nationalRiverPaint(false, true)['line-width']).toEqual(nationalRiverPaint(false, false)['line-width'])
  })

  it('widens Type 4 trunks at low zoom (>= 1.4 px at z3, >= 2.2 px at z5)', () => {
    const width = widthExpression(false)
    // z3 内层 Type interpolate 1 -> 0.55, 5 -> 1.7 => Type4 = 0.55 + 0.75 * 1.15 = 1.4125
    expect(evaluateAt(width, 3, 4)).toBeGreaterThanOrEqual(1.4)
    expect(evaluateAt(width, 3, 4)).toBeCloseTo(1.4125, 6)
    // z5 内层 Type interpolate 1 -> 0.9, 5 -> 2.8 => Type4 = 0.9 + 0.75 * 1.9 = 2.325
    expect(evaluateAt(width, 5, 4)).toBeGreaterThanOrEqual(2.2)
    expect(evaluateAt(width, 5, 4)).toBeCloseTo(2.325, 6)
  })

  it('strictly widens Type 5 trunks at low zoom (> 1.5 px at z3, > 2.3 px at z5)', () => {
    const width = widthExpression(false)
    expect(evaluateAt(width, 3, 5)).toBeGreaterThan(1.5)
    expect(evaluateAt(width, 3, 5)).toBeCloseTo(1.7, 6)
    expect(evaluateAt(width, 5, 5)).toBeGreaterThan(2.3)
    expect(evaluateAt(width, 5, 5)).toBeCloseTo(2.8, 6)
  })

  it('never lets a river get thinner as the map zooms in', () => {
    const width = widthExpression(false)
    for (const type of [1, 2, 3, 4, 5]) {
      let previous = Number.NEGATIVE_INFINITY
      for (let zoom = 3; zoom <= 12; zoom += 0.25) {
        const current = evaluateAt(width, zoom, type)
        expect(current).toBeGreaterThanOrEqual(previous - 1e-9)
        previous = current
      }
    }
  })
})

describe('m11NationalRiverPaint line-opacity', () => {
  it('compiles against the real line-opacity property spec in both dim states', () => {
    expect(opacityExpression(false)).toBeTruthy()
    expect(opacityExpression(true)).toBeTruthy()
  })

  it('rejects a nested zoom expression, proving the compile gate bites', () => {
    const illegalPaint = [
      'interpolate',
      ['linear'],
      ['zoom'],
      3,
      [
        '*',
        ['interpolate', ['linear'], ['zoom'], 5, 1, 6, 0.42],
        ['match', ['get', 'Type'], 5, 0.82, 4, 0.45, 0],
      ],
      9,
      0.88,
    ]
    const compiled = createPropertyExpression(illegalPaint, LINE_OPACITY_SPEC)
    expect(compiled.result).toBe('error')
    const message = compiled.result === 'error' ? compiled.value.map((error) => error.message).join(' ') : ''
    expect(message).toMatch(/top-level "step" or "interpolate"/)
  })

  it('makes the v3-only classes actually visible at z6 and z7', () => {
    const opacity = opacityExpression(false)
    expect(evaluateAt(opacity, 6, 2)).toBeGreaterThanOrEqual(0.4)
    expect(evaluateAt(opacity, 6, 2)).toBeCloseTo(0.45, 6)
    expect(evaluateAt(opacity, 6, 1)).toBeGreaterThan(0)
    expect(evaluateAt(opacity, 6, 1)).toBeCloseTo(0.2, 6)
    expect(evaluateAt(opacity, 7, 1)).toBeGreaterThanOrEqual(0.3)
    expect(evaluateAt(opacity, 7, 1)).toBeCloseTo(0.35, 6)
  })

  it('does not regress the existing Type 5/4/3 classes', () => {
    const opacity = opacityExpression(false)
    expect(evaluateAt(opacity, 3, 5)).toBeCloseTo(0.82, 6)
    expect(evaluateAt(opacity, 3, 4)).toBeCloseTo(0.45, 6)
    expect(evaluateAt(opacity, 5, 5)).toBeCloseTo(0.9, 6)
    expect(evaluateAt(opacity, 5, 4)).toBeCloseTo(0.76, 6)
    expect(evaluateAt(opacity, 5, 3)).toBeCloseTo(0.42, 6)
    expect(evaluateAt(opacity, 7, 5)).toBeCloseTo(0.94, 6)
    expect(evaluateAt(opacity, 7, 4)).toBeCloseTo(0.9, 6)
    expect(evaluateAt(opacity, 7, 3)).toBeCloseTo(0.72, 6)
    // 旧表没有 z6 stop，Type 5/4/3 在 z6 是 z5-z7 的中点（0.92 / 0.83 / 0.57）；新 stop 不得低于它
    expect(evaluateAt(opacity, 6, 5)).toBeCloseTo(0.92, 6)
    expect(evaluateAt(opacity, 6, 4)).toBeGreaterThanOrEqual(0.83)
    expect(evaluateAt(opacity, 6, 3)).toBeGreaterThanOrEqual(0.57)
  })

  it('applies no dim discount at low zoom (z3, z5)', () => {
    const dimmed = opacityExpression(true)
    const plain = opacityExpression(false)
    // Type 5/4 在 z3 的未折扣值非零，断言才有内容（Type 1-3 在 z3 本就是 0）
    for (const [zoom, types] of [
      [3, [5, 4]],
      [5, [5, 4, 3]],
    ] as Array<[number, number[]]>) {
      for (const type of types) {
        const undimmedValue = evaluateAt(plain, zoom, type)
        expect(undimmedValue).toBeGreaterThan(0)
        expect(evaluateAt(dimmed, zoom, type)).toBeCloseTo(undimmedValue, 6)
      }
    }
  })

  it('applies the 0.42 dim discount from z6 upward', () => {
    const dimmed = opacityExpression(true)
    const plain = opacityExpression(false)
    for (const [zoom, types] of [
      [6, [5, 4, 3, 2, 1]],
      [7, [5, 4, 3, 2, 1]],
      [9, [5, 4, 3, 2, 1]],
    ] as Array<[number, number[]]>) {
      for (const type of types) {
        const undimmedValue = evaluateAt(plain, zoom, type)
        expect(undimmedValue).toBeGreaterThan(0)
        expect(evaluateAt(dimmed, zoom, type)).toBeCloseTo(undimmedValue * 0.42, 6)
      }
    }
    expect(evaluateAt(dimmed, 6, 5)).toBeCloseTo(0.92 * 0.42, 6)
    expect(evaluateAt(dimmed, 7, 1)).toBeCloseTo(0.35 * 0.42, 6)
    expect(evaluateAt(dimmed, 9, 5)).toBeCloseTo(0.88 * 0.42, 6)
  })
})
