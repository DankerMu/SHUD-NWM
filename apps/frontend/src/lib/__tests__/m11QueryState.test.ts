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
})

