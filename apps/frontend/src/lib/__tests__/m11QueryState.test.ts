import { describe, expect, it } from 'vitest'

import {
  defaultM11QueryState,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'

describe('M11 query state helpers', () => {
  it('round-trips supported values', () => {
    const state = parseM11QueryState(
      'source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=warning-level&basemap=satellite&basinVersionId=bv-001&segmentId=seg-009&warningLevel=red&q=%E5%B9%B2%E6%B5%81',
    )

    expect(state).toEqual({
      source: 'ifs',
      cycle: '2026-05-18T00:00:00.000Z',
      validTime: '2026-05-18T06:00:00.000Z',
      layer: 'warning-level',
      basemap: 'satellite',
      basinVersionId: 'bv-001',
      segmentId: 'seg-009',
      warningLevel: 'red',
      q: '干流',
    })

    expect(parseM11QueryState(serializeM11QueryState(state))).toEqual(state)
  })

  it('normalizes invalid values to documented defaults', () => {
    const state = parseM11QueryState('source=unknown&basemap=bad&warningLevel=invalid&cycle=not-a-date&q=')

    expect(state).toEqual(defaultM11QueryState)
    expect(serializeM11QueryState(state)).toBe('')
    expect(needsM11QueryReplacement('?source=unknown&basemap=bad&warningLevel=invalid')).toBe(true)
    expect(needsM11QueryReplacement('')).toBe(false)
  })

  it('omits empty or unsupported values on serialization', () => {
    const state = {
      ...defaultM11QueryState,
      source: 'best' as const,
      layer: 'discharge' as const,
      basemap: 'vector' as const,
      cycle: 'bad',
      segmentId: 'bad/id',
    }

    expect(serializeM11QueryState(state)).toBe('')
  })

  it.each([
    ['invalid calendar date', '2026-02-30T00:00:00Z'],
    ['invalid hour', '2026-05-18T24:00:00Z'],
    ['invalid minute', '2026-05-18T00:60:00Z'],
    ['invalid second', '2026-05-18T00:00:60Z'],
    ['timezone-less timestamp', '2026-05-18T00:00:00'],
    ['date-only value', '2026-05-18'],
    ['numeric value', '1779062400000'],
    ['overflow after offset', '2026-12-31T24:00:00+08:00'],
  ])('rejects %s for forecast instants', (_label, value) => {
    expect(parseM11QueryState(`cycle=${encodeURIComponent(value)}&validTime=${encodeURIComponent(value)}`)).toMatchObject({
      cycle: null,
      validTime: null,
    })
  })

  it('normalizes valid explicit-offset RFC3339 instants to UTC', () => {
    const state = parseM11QueryState('cycle=2026-05-18T08:30:15.25%2B08:00&validTime=2026-05-17T23:45:00-02:30')

    expect(state.cycle).toBe('2026-05-18T00:30:15.250Z')
    expect(state.validTime).toBe('2026-05-18T02:15:00.000Z')
  })
})
