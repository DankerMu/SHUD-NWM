import { describe, expect, it } from 'vitest'

import { toForecastSegment } from '@/components/map/MapView'

describe('MapView forecast segment adapter', () => {
  it('forwards river network version identity from map features', () => {
    expect(
      toForecastSegment({
        segment_id: 'seg-010',
        name: 'Segment 010',
        stream_order: 4,
        basin_version_id: 'basin-v1',
        river_network_version_id: 'rivnet-v1',
      }),
    ).toMatchObject({
      segmentId: 'seg-010',
      basinVersionId: 'basin-v1',
      riverNetworkVersionId: 'rivnet-v1',
      streamOrder: 4,
    })
  })
})
