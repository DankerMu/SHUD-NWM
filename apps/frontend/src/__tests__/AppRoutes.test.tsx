import { describe, expect, it } from 'vitest'

import { contextHandoff } from '@/pages/OverviewPage'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import type { SourceScenarioSelectionState } from '@/lib/m11/overviewDataContracts'

const sourceSelection: SourceScenarioSelectionState = {
  requestedSource: 'best',
  resolvedSource: 'GFS',
  scenarioIds: ['forecast_gfs_deterministic'],
  cycleTime: '2026-05-18T00:00:00.000Z',
  validTime: '2026-05-18T06:00:00.000Z',
  comparisonAvailable: false,
  provenanceLabel: 'GFS',
  unavailableReason: null,
}

describe('App route handoff helpers', () => {
  it('serializes overview context with the public discharge layer state', () => {
    const handoff = contextHandoff('/ops', { ...defaultM11QueryState, source: 'best' }, sourceSelection)

    expect(handoff.href).toBe(
      '/ops?source=gfs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
    )
    expect(handoff.description).toContain('GFS')
  })

  it('omits default layer identity from canonical URLs', () => {
    const handoff = contextHandoff('/overview', defaultM11QueryState, null)

    expect(handoff.href).toBe('/overview')
  })
})
