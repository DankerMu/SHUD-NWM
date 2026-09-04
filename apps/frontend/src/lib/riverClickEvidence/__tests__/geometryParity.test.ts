import { describe, expect, it } from 'vitest'

import {
  RIVER_CLICK_SEGMENT_GEOMETRY_MAX_COORDINATES,
  RIVER_CLICK_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
  RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES,
  parseRiverClickPreflightSegment,
} from '../preflight'
import { m11SelectedSegmentGeometryBudget } from '@/lib/m11/overviewDataContracts'

const SEGMENT_PAYLOAD = {
  river_segment_id: 'seg-001',
  river_network_version_id: 'rn-001',
  geom: { type: 'LineString', coordinates: [[100, 30], [101, 31], [102, 32]] },
}

/**
 * Non-vacuous geometric budget parity between the live lane preflight module
 * (Playwright-importable, alias-free) and the shipping app owner
 * getM11SelectedSegmentGeometryBudgetStatus / m11SelectedSegmentGeometryBudget.
 */
describe('river-click preflight geometry budget parity with the app owner', () => {
  it('locks the preflight constants to the shipping app budget exactly', () => {
    expect(RIVER_CLICK_SEGMENT_GEOMETRY_MAX_COORDINATES).toBe(m11SelectedSegmentGeometryBudget.maxCoordinates)
    expect(RIVER_CLICK_SEGMENT_GEOMETRY_MAX_DIMENSIONS).toBe(m11SelectedSegmentGeometryBudget.maxCoordinateDimensions)
    expect(RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES).toBe(m11SelectedSegmentGeometryBudget.maxSerializedBytes)
    expect(m11SelectedSegmentGeometryBudget).toEqual({
      maxCoordinates: 10_000,
      maxCoordinateDimensions: 3,
      maxSerializedBytes: 250_000,
    })
  })

  it('accepts exactly the LineString/MultiLineString forms the app accepts with the same budget', () => {
    const lineResult = parseRiverClickPreflightSegment(SEGMENT_PAYLOAD, 'seg-001', 'rn-001', 'bv-001')
    expect(lineResult.ok).toBe(true)

    const multi = {
      ...SEGMENT_PAYLOAD,
      geom: { type: 'MultiLineString', coordinates: [[[100, 30], [101, 31]], [[102, 32], [103, 33]]] },
    }
    expect(parseRiverClickPreflightSegment(multi, 'seg-001', 'rn-001', 'bv-001').ok).toBe(true)

    // Single-point LineString is below the app's minimum too.
    const onePoint = { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: [[100, 30]] } }
    expect(parseRiverClickPreflightSegment(onePoint, 'seg-001', 'rn-001', 'bv-001').ok).toBe(false)
  })

  it('rejects a geometry the app also rejects over the same 10000-coordinate ceiling', () => {
    const near = Array.from({ length: 10_000 }, (_, i) => [100 + i / 100_000, 30])
    const exactlyAt = parseRiverClickPreflightSegment(
      { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: near } },
      'seg-001', 'rn-001', 'bv-001',
    )
    expect(exactlyAt.ok).toBe(true)

    const over = Array.from({ length: 10_001 }, (_, i) => [100 + i / 100_000, 30])
    const overResult = parseRiverClickPreflightSegment(
      { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: over } },
      'seg-001', 'rn-001', 'bv-001',
    )
    expect(overResult.ok).toBe(false)
  })

  it('rejects a 4-dimension coordinate the app rejects with maxCoordinateDimensions 3', () => {
    const tooWide = { ...SEGMENT_PAYLOAD, geom: { type: 'LineString', coordinates: [[100, 30, 1, 2], [101, 31, 1, 2]] } }
    expect(parseRiverClickPreflightSegment(tooWide, 'seg-001', 'rn-001', 'bv-001').ok).toBe(false)
  })

  it('rejects an over-250000-byte geometry the app also rejects', () => {
    const padded = { type: 'LineString', coordinates: Array.from({ length: 9_990 }, () => [100.123456789, 30.987654321]) }
    const parsed = parseRiverClickPreflightSegment(
      { ...SEGMENT_PAYLOAD, geom: padded },
      'seg-001', 'rn-001', 'bv-001',
    )
    expect(parsed.ok).toBe(false)
  })
})
