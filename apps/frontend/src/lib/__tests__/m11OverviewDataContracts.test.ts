import { describe, expect, it } from 'vitest'

import type { components } from '@/api/types'
import {
  filterBasinSegmentRows,
  getM11LayerLegend,
  normalizeBasinSegmentRows,
  normalizeLayerStates,
  normalizeSelectedSegmentDetail,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'

const query = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
}

function riverFeature(overrides: Partial<components['schemas']['RiverSegmentFeature']['properties']> = {}) {
  return {
    type: 'Feature',
    properties: {
      segment_id: 'seg-001',
      river_segment_id: 'river-001',
      basin_version_id: 'bv-001',
      river_network_version_id: 'rn-001',
      name: 'Main Stem',
      stream_order: 3,
      length_m: 1200,
      value: 42,
      unit: 'm3/s',
      valid_time: '2026-05-18T06:00:00Z',
      ...overrides,
    },
    geometry: {
      type: 'LineString',
      coordinates: [
        [110, 30],
        [111, 31],
      ],
    },
  } satisfies components['schemas']['RiverSegmentFeature']
}

describe('M11 overview data contracts', () => {
  it('normalizes only the discharge renderable layer by default', () => {
    const layers = normalizeLayerStates({
      query,
      layers: [
        {
          layer_id: 'discharge',
          layer_name: 'Discharge',
          layer_type: 'hydrology',
          variables: ['q_down'],
          metadata: { layer_id: 'discharge', valid_times: ['2026-05-18T06:00:00Z'] } as never,
        },
      ],
    })

    expect(layers.map((layer) => layer.layerId)).toEqual(['discharge'])
    expect(layers[0]).toMatchObject({ available: true, currentValidTime: '2026-05-18T06:00:00.000Z' })
    expect(getM11LayerLegend('discharge')).not.toHaveLength(0)
  })

  it('builds basin river rows from discharge feature properties without alert-derived fields', () => {
    const rows = normalizeBasinSegmentRows({
      query,
      featureCollection: {
        type: 'FeatureCollection',
        features: [riverFeature()],
        total: 1,
        feature_total: 1,
        limit: 1,
        offset: 0,
      },
    })

    expect(rows[0]).toMatchObject({
      riverSegmentId: 'river-001',
      segmentId: 'seg-001',
      currentQ: 42,
      qUnit: 'm3/s',
      validTime: '2026-05-18T06:00:00.000Z',
      hasGeometry: true,
    })
  })

  it('filters basin segment rows by search text only', () => {
    const rows = normalizeBasinSegmentRows({
      query,
      featureCollection: {
        type: 'FeatureCollection',
        features: [riverFeature({ river_segment_id: 'river-002', name: 'North Fork' })],
        total: 1,
        feature_total: 1,
        limit: 1,
        offset: 0,
      },
    })

    expect(filterBasinSegmentRows(rows, { q: 'north' })).toHaveLength(1)
    expect(filterBasinSegmentRows(rows, { q: 'south' })).toHaveLength(0)
  })

  it('normalizes selected segment detail from q_down forecast data', () => {
    const detail = normalizeSelectedSegmentDetail({
      query,
      basinVersionId: 'bv-001',
      segmentId: 'river-001',
      feature: riverFeature(),
      forecast: {
        segment_id: 'river-001',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source_id: 'GFS',
            segment_role: 'future_7_days',
            points: [['2026-05-18T06:00:00Z', 55]],
          },
        ],
      },
    })

    expect(detail.currentQ).toBe(55)
    expect(detail.qUnit).toBe('m3/s')
    expect(detail.trendPoints).toHaveLength(1)
  })
})
