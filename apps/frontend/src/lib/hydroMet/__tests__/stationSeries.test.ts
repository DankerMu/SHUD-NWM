import { describe, expect, it } from 'vitest'

import {
  HydroMetStationSeriesError,
  HYDRO_MET_STATION_SERIES_RETAINED_DISK_MISS_CODE,
  isHydroMetStationSeriesRetainedDiskMiss,
  validateHydroMetStationSeriesForChart,
  type HydroMetStationSeriesRecord,
} from '@/lib/hydroMet/stationSeries'

function metadata(overrides: Record<string, unknown> = {}) {
  return {
    limit: 240,
    returned_points: 2,
    requested_from: '2026-05-21T00:00:00Z',
    requested_to: '2026-05-22T00:00:00Z',
    returned_from: '2026-05-21T06:00:00Z',
    returned_to: '2026-05-21T12:00:00Z',
    truncated: false,
    ...overrides,
  }
}

function series(overrides: Record<string, unknown> = {}): HydroMetStationSeriesRecord {
  return {
    variable: 'PRCP',
    unit: 'mm',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    truncated: false,
    metadata: metadata(),
    points: [
      { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
      { valid_time: '2026-05-21T12:00:00Z', value: 2.4, quality_flag: 'ok' },
    ],
    ...overrides,
  } as HydroMetStationSeriesRecord
}

describe('validateHydroMetStationSeriesForChart (gold standard)', () => {
  it('recognizes retained-disk station-series misses by API error code', () => {
    expect(isHydroMetStationSeriesRetainedDiskMiss(
      new HydroMetStationSeriesError('missing', { code: HYDRO_MET_STATION_SERIES_RETAINED_DISK_MISS_CODE }),
    )).toBe(true)
    expect(isHydroMetStationSeriesRetainedDiskMiss({
      error: {
        code: HYDRO_MET_STATION_SERIES_RETAINED_DISK_MISS_CODE,
        message: 'Station forcing file not found.',
      },
    })).toBe(true)
    expect(isHydroMetStationSeriesRetainedDiskMiss(new Error('Station forcing file not found.'))).toBe(false)
  })

  it('accepts a well-formed series and returns rendered points + unit', () => {
    const result = validateHydroMetStationSeriesForChart(series())
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.unit).toBe('mm')
    expect(result.renderedPoints).toHaveLength(2)
    expect(result.capped).toBe(false)
    expect(result.seriesTruncated).toBe(false)
  })

  it('reject-on-any-invalid-point: a single NaN value fails the whole variable', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({
        points: [
          { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
          { valid_time: '2026-05-21T12:00:00Z', value: Number.NaN, quality_flag: 'ok' },
        ],
      }),
    )
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.messages.join('')).toContain('value 不是有限数值')
  })

  it('reject-on-any-invalid-point: a malformed valid_time fails the whole variable', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({
        points: [{ valid_time: 'not-a-time', value: 1.2, quality_flag: 'ok' }],
      }),
    )
    expect(result.ok).toBe(false)
  })

  it('keeps unit=null when unit missing (caller gates on it; not auto-rejected)', () => {
    const result = validateHydroMetStationSeriesForChart(series({ unit: null }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.unit).toBeNull()
  })

  it('rejects when unit is a non-string', () => {
    const result = validateHydroMetStationSeriesForChart(series({ unit: 42 }))
    expect(result.ok).toBe(false)
  })

  it('rejects malformed metadata (negative returned_points)', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({ metadata: metadata({ returned_points: -1 }) }),
    )
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.messages.join('')).toContain('returned_points')
  })

  it('rejects malformed metadata (invalid RFC3339 time field)', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({ metadata: metadata({ returned_from: 'bad' }) }),
    )
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.messages.join('')).toContain('returned_from')
  })

  it('rejects missing metadata entirely', () => {
    const bad = series()
    delete (bad as Record<string, unknown>).metadata
    const result = validateHydroMetStationSeriesForChart(bad)
    expect(result.ok).toBe(false)
  })

  it('discloses truncated + capped when metadata reports a larger returned_points', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({ truncated: true, metadata: metadata({ returned_points: 1000, truncated: true }) }),
    )
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.seriesTruncated).toBe(true)
    expect(result.capped).toBe(true)
    expect(result.reportedPointCount).toBe(1000)
  })

  it('summarizes non-ok quality flags for disclosure', () => {
    const result = validateHydroMetStationSeriesForChart(
      series({
        points: [
          { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
          { valid_time: '2026-05-21T12:00:00Z', value: 2.4, quality_flag: 'suspect' },
        ],
      }),
    )
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.nonOkFlags).toContain('suspect')
  })
})
