import type { FeatureCollection } from 'geojson'
import { describe, expect, it } from 'vitest'

import type { OverviewBasin } from '@/lib/m11/overviewDataContracts'
import { staticBasinBoundaryIndex, withStaticBasinBoundaries } from '@/lib/m11/staticBasinFallback'

const domain: FeatureCollection = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: { basin_id: 'basins_qhh' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [98, 37.5],
            [100.5, 37.5],
            [100.5, 38.3],
            [98, 38.3],
            [98, 37.5],
          ],
        ],
      },
    },
    {
      type: 'Feature',
      properties: { basin_id: 'basins_heihe' },
      geometry: {
        type: 'MultiPolygon',
        coordinates: [
          [
            [
              [98, 38],
              [101, 38],
              [101, 42.7],
              [98, 42.7],
              [98, 38],
            ],
          ],
        ],
      },
    },
    {
      type: 'Feature',
      properties: {},
      geometry: { type: 'Point', coordinates: [0, 0] },
    },
  ],
}

function basin(overrides: Partial<OverviewBasin>): OverviewBasin {
  return {
    basinId: 'basins_qhh',
    displayName: 'QHH',
    basinGroup: null,
    areaKm2: null,
    riverCount: null,
    activeModelCount: 0,
    latestForecastTime: null,
    selectedBasinVersionId: null,
    basinVersions: [],
    boundary: null,
    bbox: null,
    unavailableReason: null,
    ...overrides,
  } as OverviewBasin
}

describe('staticBasinBoundaryIndex', () => {
  it('indexes Polygon (promoted to MultiPolygon) and MultiPolygon features by basin_id', () => {
    const index = staticBasinBoundaryIndex(domain)
    expect([...index.keys()].sort()).toEqual(['basins_heihe', 'basins_qhh'])
    const qhh = index.get('basins_qhh')
    expect(qhh?.boundary.type).toBe('MultiPolygon')
    expect(qhh?.bbox).toEqual({ minLon: 98, minLat: 37.5, maxLon: 100.5, maxLat: 38.3 })
  })

  it('returns an empty index for null/missing domain', () => {
    expect(staticBasinBoundaryIndex(null).size).toBe(0)
    expect(staticBasinBoundaryIndex({ type: 'FeatureCollection', features: [] }).size).toBe(0)
  })
})

describe('withStaticBasinBoundaries', () => {
  it('fills boundary and bbox only for basins missing both', () => {
    const serverBoundary = {
      type: 'MultiPolygon',
      coordinates: [[[[1, 1], [2, 1], [2, 2], [1, 1]]]],
    } as OverviewBasin['boundary']
    const basins = [
      basin({ basinId: 'basins_qhh' }),
      basin({ basinId: 'basins_heihe', boundary: serverBoundary, bbox: { minLon: 1, minLat: 1, maxLon: 2, maxLat: 2 } }),
    ]
    const next = withStaticBasinBoundaries(basins, domain)
    expect(next[0].boundary?.type).toBe('MultiPolygon')
    expect(next[0].bbox?.maxLat).toBe(38.3)
    // 已有服务端边界的流域保持不动
    expect(next[1].boundary).toBe(serverBoundary)
  })

  it('keeps the original array identity when nothing changes', () => {
    const basins = [basin({ basinId: 'unknown_basin' })]
    expect(withStaticBasinBoundaries(basins, domain)).toBe(basins)
    expect(withStaticBasinBoundaries(basins, null)).toBe(basins)
  })
})
