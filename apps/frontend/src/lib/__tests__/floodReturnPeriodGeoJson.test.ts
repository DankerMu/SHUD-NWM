import { describe, expect, it, vi } from 'vitest'

import {
  fetchFloodReturnPeriodFeatureCollection,
  validateFloodReturnPeriodFeatureCollection,
} from '@/lib/floodReturnPeriodGeoJson'

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

  it('rejects coordinate nesting that does not match the declared geometry type', () => {
    const cases = [
      { type: 'Point', coordinates: [[100, 30]] },
      { type: 'MultiPoint', coordinates: [100, 30] },
      { type: 'LineString', coordinates: [100, 30] },
      { type: 'MultiLineString', coordinates: [[100, 30], [101, 31]] },
      { type: 'Polygon', coordinates: [[100, 30], [101, 31]] },
      { type: 'MultiPolygon', coordinates: [[[100, 30], [101, 31]]] },
    ]

    for (const geometry of cases) {
      expect(
        validateFloodReturnPeriodFeatureCollection({
          type: 'FeatureCollection',
          features: [{ ...feature, geometry }],
        }),
      ).toMatchObject({ ok: false, code: 'malformed_geometry' })
    }
  })

  it('rejects line and polygon geometries that violate GeoJSON cardinality', () => {
    const cases = [
      { type: 'LineString', coordinates: [[100, 30]] },
      { type: 'MultiLineString', coordinates: [[[100, 30], [101, 31]], [[102, 32]]] },
      { type: 'Polygon', coordinates: [[[100, 30], [101, 30], [100, 30]]] },
      { type: 'Polygon', coordinates: [[[100, 30], [101, 30], [101, 31], [100, 31]]] },
      { type: 'MultiPolygon', coordinates: [[[[100, 30], [101, 30], [101, 31], [100, 31]]]] },
    ]

    for (const geometry of cases) {
      expect(
        validateFloodReturnPeriodFeatureCollection({
          type: 'FeatureCollection',
          features: [{ ...feature, geometry }],
        }),
      ).toMatchObject({ ok: false, code: 'malformed_geometry' })
    }
  })

  it('recursively rejects GeometryCollection descendants with invalid cardinality', () => {
    expect(
      validateFloodReturnPeriodFeatureCollection({
        type: 'FeatureCollection',
        features: [
          {
            ...feature,
            geometry: {
              type: 'GeometryCollection',
              geometries: [{ type: 'Polygon', coordinates: [[[100, 30], [101, 30], [101, 31], [100, 31]]] }],
            },
          },
        ],
      }),
    ).toMatchObject({ ok: false, code: 'malformed_geometry' })
  })

  it('recursively validates GeometryCollection coordinate nesting', () => {
    expect(
      validateFloodReturnPeriodFeatureCollection({
        type: 'FeatureCollection',
        features: [
          {
            ...feature,
            geometry: {
              type: 'GeometryCollection',
              geometries: [{ type: 'Point', coordinates: [[100, 30]] }],
            },
          },
        ],
      }),
    ).toMatchObject({ ok: false, code: 'malformed_geometry' })
  })

  it('rejects oversized streamed responses without materializing text', async () => {
    const cancel = vi.fn()
    const oversizedChunk = new TextEncoder().encode('x'.repeat(16))
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers(),
        body: {
          getReader: () => ({
            read: vi.fn().mockResolvedValueOnce({ done: false, value: oversizedChunk }),
            cancel: vi.fn().mockImplementation(() => {
              cancel()
              return Promise.resolve()
            }),
            releaseLock: vi.fn(),
          }),
        },
        text: vi.fn().mockRejectedValue(new Error('text() must not be called')),
      }),
    )

    const result = await fetchFloodReturnPeriodFeatureCollection('/return-period.geojson', {
      budget: { maxSerializedBytes: 8 },
    })

    expect(result).toMatchObject({ ok: false, code: 'serialized_bytes', serializedBytes: 16 })
    expect(cancel).toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledWith('/return-period.geojson', {})
  })
})
