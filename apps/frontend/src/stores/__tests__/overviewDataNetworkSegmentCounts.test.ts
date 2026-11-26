import { beforeEach, describe, expect, it, vi } from 'vitest'

import { clearOverviewDataCache, useOverviewDataStore, type OverviewDataSnapshot } from '@/stores/overviewData'
import { buildNetworkSegmentCounts } from '@/stores/overviewDataNetworkSegmentCounts'
import { apiError, ifsQuery, mockApi, model, query, resetOverviewDataTestState, success } from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

/**
 * shared-reach-click-ownership (#3390) D2 / task 3.1: the per-network segment counts are a derived
 * field of the overview snapshot, built from the model records the store already loads, next to
 * `basinVersionToBasinId` and cleared with it.
 */

function record(networkId: unknown, segmentCount: unknown) {
  return { river_network_version_id: networkId, segment_count: segmentCount } as never
}

describe('buildNetworkSegmentCounts', () => {
  it('maps each network to the smallest usable segment_count among its records', () => {
    expect(
      buildNetworkSegmentCounts([record('rn-a', 30), record('rn-b', 7), record('rn-a', 12), record('rn-a', 40)]),
    ).toEqual({ 'rn-a': 12, 'rn-b': 7 })
  })

  it('skips records without a network id or without a usable count; a network with none is absent', () => {
    const counts = buildNetworkSegmentCounts([
      record('rn-zero', 0),
      record('rn-negative', -3),
      record('rn-nan', Number.NaN),
      record('rn-inf', Number.POSITIVE_INFINITY),
      record('rn-null', null),
      record('rn-missing', undefined),
      record('rn-string', '12'),
      record('', 5),
      record(null, 5),
      record('rn-mixed', 0),
      record('rn-mixed', 9),
      record('rn-mixed', Number.NaN),
    ])
    expect(counts).toEqual({ 'rn-mixed': 9 })
  })

  it('returns an empty map for no records', () => {
    expect(buildNetworkSegmentCounts([])).toEqual({})
  })
})

describe('overview snapshot networkSegmentCounts', () => {
  beforeEach(() => {
    resetOverviewDataTestState()
  })

  const nestedModel = { ...model, model_id: 'model-nested', river_network_version_id: 'rn-nested', segment_count: 7971 }
  const largerModel = { ...model, model_id: 'model-larger', river_network_version_id: 'rn-larger', segment_count: 25971 }
  const largerDuplicate = { ...largerModel, model_id: 'model-larger-2', segment_count: 26000 }

  it('is derived from the loaded model records next to basinVersionToBasinId', async () => {
    mockApi({
      '/api/v1/models': () => success({ items: [largerModel, nestedModel, largerDuplicate], total: 3, limit: 200, offset: 0 }),
    })
    const snapshot = await useOverviewDataStore.getState().loadOverview(query)
    expect(snapshot.networkSegmentCounts).toEqual({ 'rn-nested': 7971, 'rn-larger': 25971 })
    expect(snapshot.basinVersionToBasinId).toEqual({ 'bv-001': 'basin-demo' })
    expect(useOverviewDataStore.getState().overview?.networkSegmentCounts).toEqual({ 'rn-nested': 7971, 'rn-larger': 25971 })
  })

  it('is empty when the model request fails', async () => {
    mockApi({ '/api/v1/models': () => apiError('MODELS_DOWN') })
    const snapshot = await useOverviewDataStore.getState().loadOverview(query)
    expect(snapshot.networkSegmentCounts).toEqual({})
    expect(snapshot.basinVersionToBasinId).toEqual({})
  })

  it('is cleared and rebuilt with basinVersionToBasinId when the request scope changes', async () => {
    mockApi({ '/api/v1/models': () => success({ items: [nestedModel, largerModel], total: 2, limit: 200, offset: 0 }) })
    await useOverviewDataStore.getState().loadOverview(query)
    expect(useOverviewDataStore.getState().overview?.networkSegmentCounts).toEqual({ 'rn-nested': 7971, 'rn-larger': 25971 })

    // New scope, the model request now fails: every snapshot written for it carries both derived
    // fields empty or both filled, and the old counts never survive into the new scope's final snapshot.
    clearOverviewDataCache()
    mockApi({ '/api/v1/models': () => apiError('MODELS_DOWN') })
    const written: OverviewDataSnapshot[] = []
    const unsubscribe = useOverviewDataStore.subscribe((state, previous) => {
      if (state.overview && state.overview !== previous.overview) written.push(state.overview)
    })
    try {
      const snapshot = await useOverviewDataStore.getState().loadOverview(ifsQuery)
      expect(snapshot.networkSegmentCounts).toEqual({})
      expect(useOverviewDataStore.getState().overview?.networkSegmentCounts).toEqual({})
    } finally {
      unsubscribe()
    }
    expect(written.length).toBeGreaterThan(0)
    for (const overview of written) {
      expect(Object.keys(overview.networkSegmentCounts).length === 0).toBe(Object.keys(overview.basinVersionToBasinId).length === 0)
    }
  })
})
