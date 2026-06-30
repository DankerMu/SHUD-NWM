import { describe, expect, it } from 'vitest'

import {
  buildBasinRiverFeatureCollection,
  buildM11RegisteredOverlay,
  m11VectorSourceKey,
} from '@/components/map/M11MapLibreSurface'
import type { BasinSegmentRow, LayerState } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'

const state = {
  ...defaultM11QueryState,
  source: 'gfs',
  cycle: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
}

const dischargeMetadata = {
  layer_id: 'discharge',
  tile_format: 'mvt',
  maplibre_source_layer: 'hydro',
  min_zoom: 0,
  max_zoom: 10,
  valid_times: ['2026-05-18T06:00:00.000Z'],
  url_template: '/api/v1/tiles/hydro/{valid_time}/{variable}/{z}/{x}/{y}.pbf',
  required_placeholders: ['valid_time', 'variable', 'z', 'x', 'y'],
  source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
} as never

const dischargeLayer: LayerState = {
  layerId: 'discharge',
  displayName: 'Discharge',
  group: 'hydrology',
  available: true,
  metadata: dischargeMetadata,
  validTimes: ['2026-05-18T06:00:00.000Z'],
  currentValidTime: '2026-05-18T06:00:00.000Z',
  validTimeSource: 'api',
  disabledReason: null,
  freshness: {
    updatedAt: null,
    cycleTime: state.cycle,
    validTime: state.validTime,
    runId: 'run-001',
    basinVersionId: 'bv-001',
    riverNetworkVersionId: 'rn-001',
    source: 'GFS',
    isStale: false,
    staleAfterHours: 6,
    unavailableReason: null,
  },
  legend: [],
}

const basinSegment: BasinSegmentRow = {
  riverSegmentId: 'river-001',
  riverNetworkVersionId: 'rn-001',
  segmentId: 'seg-001',
  displayName: 'Demo River',
  basinVersionId: 'bv-001',
  streamOrder: 2,
  lengthM: 1000,
  currentQ: 25,
  qUnit: 'm3/s',
  source: 'GFS',
  cycleTime: state.cycle,
  validTime: state.validTime,
  hasGeometry: true,
  geometry: {
    type: 'LineString',
    coordinates: [
      [100, 30],
      [101, 31],
    ],
  },
  unavailableReason: null,
}

describe('M11 discharge shell contracts', () => {
  it('registers the discharge vector overlay with q_down variable identity', () => {
    const overlay = buildM11RegisteredOverlay(state, [dischargeLayer])

    expect(overlay).not.toBeNull()
    expect(overlay?.layerId).toBe('discharge')
    expect(overlay?.source.tiles[0]).toContain('/api/v1/tiles/hydro/')
    expect(overlay?.source.tiles[0]).toContain('/q_down/')
    expect(JSON.stringify(overlay?.layer.paint)).toContain('value')
  })

  it('uses a discharge-only vector source key', () => {
    const key = m11VectorSourceKey({
      layerId: 'discharge',
      runId: 'run-001',
      validTime: '2026-05-18T06:00:00.000Z',
      variable: 'q_down',
      metadata: dischargeMetadata,
    })

    expect(JSON.parse(key)).toMatchObject({
      layer_id: 'discharge',
      run_id: 'run-001',
      variable: 'q_down',
      maplibre_source_layer: 'hydro',
    })
  })

  it('builds basin river feature properties from discharge rows only', () => {
    const collection = buildBasinRiverFeatureCollection([basinSegment], 'discharge')

    expect(collection.features).toHaveLength(1)
    expect(collection.features[0].properties).toMatchObject({
      river_segment_id: 'river-001',
      q_value: 25,
      q_unit: 'm3/s',
    })
  })

  it('keeps shared visual tokens available for the overview surface', () => {
    expect(m11VisualTokens.navHeight).toBe('0px')
  })
})
