import { describe, expect, it } from 'vitest'

import { nearestRankP95, validateRiverClickDurations } from '../timing'
import {
  RIVER_CLICK_ABOVE_THRESHOLD_DURATIONS,
  RIVER_CLICK_EXACT_THRESHOLD_DURATIONS,
  RIVER_CLICK_JUST_BELOW_THRESHOLD_DURATIONS,
} from '../../../test/riverClickThresholdFixture'

describe('nearest-rank river-click P95', () => {
  it('selects nearest-rank index 18 for exactly 20 accepted durations', () => {
    // Independent worked example: 20 ordered millisecond values 0..1900,
    // nearest-rank index ceil(0.95*20)-1 = 18 -> 1800.
    const durations = Array.from({ length: 20 }, (_, index) => index * 100)
    expect(nearestRankP95(durations)).toBe(1800)
  })

  it('sorts unsorted input and picks index 18 of the sorted ascending sequence', () => {
    const durations = [1900, 0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800]
    expect(nearestRankP95(durations)).toBe(1800)
  })

  it('returns null for every non-20 sample count', () => {
    expect(nearestRankP95([])).toBeNull()
    expect(nearestRankP95([1000])).toBeNull()
    expect(nearestRankP95(Array.from({ length: 19 }, () => 100))).toBeNull()
    expect(nearestRankP95(Array.from({ length: 21 }, () => 100))).toBeNull()
  })

  it('selects exactly 2000 from the shared duration fixture whose sorted index 18 is 2000', () => {
    const sorted = [...RIVER_CLICK_EXACT_THRESHOLD_DURATIONS].sort((a, b) => a - b)
    expect(sorted[18]).toBe(2000)
    expect(nearestRankP95([...RIVER_CLICK_EXACT_THRESHOLD_DURATIONS])).toBe(2000)
    expect(nearestRankP95([...RIVER_CLICK_JUST_BELOW_THRESHOLD_DURATIONS])).toBe(1999.999)
    expect(nearestRankP95([...RIVER_CLICK_ABOVE_THRESHOLD_DURATIONS])).toBe(2000.001)
  })

  it('does not apply any smoothing or interpolation', () => {
    // Sorted: 19×100 then 2000. Index 18 is the 19th element -> 100. A mean or
    // interpolation method would produce ~195; nearest-rank never does.
    const durations = [
      2000, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100,
    ]
    expect(nearestRankP95(durations)).toBe(100)
  })

  it('accepts fractional browser-clock durations and still picks index 18', () => {
    const durations = Array.from({ length: 20 }, (_, index) => 100.125 + index * 0.375)
    const sorted = [...durations].sort((a, b) => a - b)
    expect(nearestRankP95(durations)).toBe(sorted[18])
  })

  it('rejects any non-finite or negative duration instead of sorting NaN', () => {
    const withNaN = Array.from({ length: 19 }, () => 100).concat([Number.NaN])
    expect(nearestRankP95(withNaN)).toBeNull()
    const withInfinity = Array.from({ length: 19 }, () => 100).concat([Number.POSITIVE_INFINITY])
    expect(nearestRankP95(withInfinity)).toBeNull()
    const withNegative = Array.from({ length: 19 }, () => 100).concat([-1])
    expect(nearestRankP95(withNegative)).toBeNull()
    const withString = Array.from({ length: 19 }, () => 100).concat(['fast' as unknown as number])
    expect(nearestRankP95(withString)).toBeNull()
  })
})

describe('river-click duration validation', () => {
  it('accepts exactly 20 finite non-negative durations including fractions', () => {
    const durations = Array.from({ length: 20 }, (_, index) => index + 0.25)
    expect(validateRiverClickDurations(durations)).toEqual({ ok: true })
  })

  it('accepts integer and zero durations (browser clock may return whole ms)', () => {
    expect(validateRiverClickDurations(Array.from({ length: 20 }, () => 0))).toEqual({ ok: true })
    expect(validateRiverClickDurations(Array.from({ length: 20 }, (_, index) => index))).toEqual({ ok: true })
  })

  it('rejects counts other than 20', () => {
    expect(validateRiverClickDurations([]).ok).toBe(false)
    expect(validateRiverClickDurations(Array.from({ length: 19 }, () => 1)).ok).toBe(false)
    expect(validateRiverClickDurations(Array.from({ length: 21 }, () => 1)).ok).toBe(false)
  })

  it('rejects negative and non-finite durations', () => {
    expect(validateRiverClickDurations(Array.from({ length: 19 }, () => 1).concat([-1])).ok).toBe(false)
    expect(validateRiverClickDurations(Array.from({ length: 19 }, () => 1).concat([Number.POSITIVE_INFINITY])).ok).toBe(false)
    expect(validateRiverClickDurations(Array.from({ length: 19 }, () => 1).concat([Number.NaN])).ok).toBe(false)
  })

  it('rejects non-number durations but accepts every finite non-negative number', () => {
    const durations = Array.from({ length: 19 }, () => 1) as unknown as number[]
    durations.push('fast' as unknown as number)
    expect(validateRiverClickDurations(durations).ok).toBe(false)
    // 1.5 is a legitimate fractional browser measurement; must be accepted.
    expect(validateRiverClickDurations(Array.from({ length: 19 }, () => 1).concat([1.5])).ok).toBe(true)
  })
})
