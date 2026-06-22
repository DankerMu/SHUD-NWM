import { describe, expect, it } from 'vitest'

import { formatRiverSegmentDisplayName, formatStationDisplayName } from '@/lib/hydroMet/displayNames'

describe('hydroMet display names', () => {
  it('formats forcing station IDs as readable basin station labels', () => {
    expect(formatStationDisplayName({
      stationId: 'qhh_forc_324',
      stationName: 'QHH forcing station 324',
      basinId: 'basins_qhh',
    })).toEqual({
      title: 'QHH 代站 324',
      meta: '站点 ID qhh_forc_324',
    })
  })

  it('keeps custom station names while preserving the raw station ID', () => {
    expect(formatStationDisplayName({
      stationId: 'qhh_forc_002',
      stationName: 'North Ridge station',
      basinId: 'basins_qhh',
    })).toEqual({
      title: 'North Ridge station',
      meta: '站点 ID qhh_forc_002',
    })
  })

  it('formats SHUD river segment IDs as readable basin segment labels', () => {
    expect(formatRiverSegmentDisplayName({
      riverSegmentId: 'basins_qhh_shud_shud_riv_000974',
      segmentName: 'basins_qhh_shud_shud_riv_000974',
      basinId: 'basins_qhh',
    })).toEqual({
      title: 'QHH 河段 974',
      meta: '河段 ID basins_qhh_shud_shud_riv_000974',
    })
  })
})
