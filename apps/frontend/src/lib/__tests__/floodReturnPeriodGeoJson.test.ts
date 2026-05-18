import { describe, expect, it } from 'vitest'

import { validateFloodReturnPeriodFeatureCollection } from '@/lib/floodReturnPeriodGeoJson'

const feature = {
  type: 'Feature',
  properties: { segment_id: 'seg-1' },
  geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
}

describe('flood return-period GeoJSON validation', () => {
  it('returns sanitized FeatureCollection data for safe payloads', () => {
    const result = validateFloodReturnPeriodFeatureCollection({
      type: 'FeatureCollection',
      features: [{ ...feature, extra: 'ignored' }],
    })

    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.data).toEqual({
      type: 'FeatureCollection',
      features: [feature],
    })
    expect(result.coordinateCount).toBe(2)
  })

  it('rejects invalid collection shape and feature count breaches', () => {
    expect(validateFloodReturnPeriodFeatureCollection({ type: 'Feature' }).ok).toBe(false)
    expect(
      validateFloodReturnPeriodFeatureCollection(
        { type: 'FeatureCollection', features: [feature, feature] },
        { maxFeatures: 1 },
      ),
    ).toMatchObject({ ok: false, code: 'feature_count' })
  })

  it('rejects coordinate count, coordinate dimension, malformed geometry, and byte budget breaches', () => {
    expect(
      validateFloodReturnPeriodFeatureCollection(
        { type: 'FeatureCollection', features: [feature] },
        { maxCoordinates: 1 },
      ),
    ).toMatchObject({ ok: false, code: 'coordinate_count' })

    expect(
      validateFloodReturnPeriodFeatureCollection({
        type: 'FeatureCollection',
        features: [{ ...feature, geometry: { type: 'Point', coordinates: [100, 30, 1, 0] } }],
      }),
    ).toMatchObject({ ok: false, code: 'coordinate_dimension' })

    expect(
      validateFloodReturnPeriodFeatureCollection({
        type: 'FeatureCollection',
        features: [{ ...feature, geometry: { type: 'LineString', coordinates: [] } }],
      }),
    ).toMatchObject({ ok: false, code: 'malformed_geometry' })

    expect(
      validateFloodReturnPeriodFeatureCollection(
        { type: 'FeatureCollection', features: [feature] },
        { maxSerializedBytes: 10 },
      ),
    ).toMatchObject({ ok: false, code: 'serialized_bytes' })
  })
})
