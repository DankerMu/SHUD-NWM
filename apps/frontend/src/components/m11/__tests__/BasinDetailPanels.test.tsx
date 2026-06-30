import { describe, expect, it } from 'vitest'

import { basinDetailToOverviewBasin } from '@/components/m11/BasinDetailPanels'
import {
  createEmptyBasinDetail,
  createFreshnessMetadata,
  createSourceScenarioSelection,
  type BasinDetail,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'

function buildDetail(overrides: Partial<BasinDetail>): BasinDetail {
  return { ...createEmptyBasinDetail('yangtze', defaultM11QueryState), ...overrides }
}

describe('BasinDetailPanels.basinDetailToOverviewBasin', () => {
  it('maps basin detail into overview basin fields used by the map shell', () => {
    const detail = buildDetail({
      basinId: 'yangtze',
      displayName: 'Yangtze',
      segmentCount: 12,
      activeModelCount: 1,
      selectedBasinVersionId: 'bv-001',
      latestRun: createFreshnessMetadata({ validTime: '2026-05-18T06:00:00Z' }),
    })

    const overviewBasin = basinDetailToOverviewBasin(detail)

    expect(overviewBasin).toMatchObject({
      basinId: 'yangtze',
      displayName: 'Yangtze',
      riverCount: 12,
      activeModelCount: 1,
      latestForecastTime: '2026-05-18T06:00:00.000Z',
      selectedBasinVersionId: 'bv-001',
    })
  })

  it('createEmptyBasinDetail still initializes source and freshness metadata', () => {
    const empty = createEmptyBasinDetail('yangtze', {
      ...defaultM11QueryState,
      source: 'gfs',
      cycle: null,
      validTime: null,
    })

    expect(empty.sourceSelection).toEqual(createSourceScenarioSelection({ source: 'gfs', cycle: null, validTime: null }))
    expect(empty.latestRun).toEqual(
      createFreshnessMetadata({
        source: empty.sourceSelection.resolvedSource,
        unavailableReason: 'No basin data loaded.',
      }),
    )
  })
})
