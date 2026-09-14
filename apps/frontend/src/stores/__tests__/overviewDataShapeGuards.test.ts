// #2129 裁决 B（design D1/D2）：六个总览端点的载荷形状守卫落在 store 的 `cached()` loader 里。
// 变形载荷 = loader reject：不进缓存、走既有的 scoped 降级路径，并记进 `dataAnomalies`。
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { activeCycleValidTimesErrorDisabledReason } from '@/lib/m11/overviewDataContracts'
import { clearOverviewDataCache, m11SourceCycleKey, useOverviewDataStore } from '@/stores/overviewData'
import {
  CYCLES_PATH,
  DEFAULT_CYCLE,
  IFS_CYCLE,
  PRECIP_INDEX_PATH,
  VALID_TIMES_PATH,
  apiError,
  basin,
  cyclesPayload,
  ifsQuery,
  mockApi,
  precipIndex,
  query,
  resetOverviewDataTestState,
  success,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

beforeEach(() => {
  resetOverviewDataTestState()
})

const validCycleEntry = { cycle_time: DEFAULT_CYCLE, valid_time_start: DEFAULT_CYCLE, valid_time_end: '2026-05-18T06:00:00Z' }

/** 结算后再断言状态：变形载荷在改动前会让 `loadOverview` reject，状态断言要先于「不抛」断言给出。 */
async function settleLoad(load: Promise<unknown>) {
  return load.then(
    () => 'resolved' as const,
    () => 'rejected' as const,
  )
}

function dischargeLayer() {
  return (useOverviewDataStore.getState().overview?.layers ?? []).find((item) => item.layerId === 'discharge')
}

describe('overview data shape guards: discharge cycles (E2)', () => {
  const malformedCycles = [
    { label: 'a null entry', payload: { source: 'gfs', cycles: [null, validCycleEntry], default_cycle: DEFAULT_CYCLE } },
    { label: 'a string array', payload: { source: 'gfs', cycles: [DEFAULT_CYCLE], default_cycle: DEFAULT_CYCLE } },
  ]

  it.each(malformedCycles)('degrades gfs cycles with $label to the scoped error state and records 起报时次', async ({ payload }) => {
    const calls = mockApi({ [CYCLES_PATH]: () => success(payload) })

    const outcome = await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource.gfs).toEqual({ status: 'error' })
    expect(state.dataAnomalies).toEqual(['起报时次'])
    expect(state.bootstrapError).toBeNull()
    expect(state.error).toBeNull()
    expect(outcome).toBe('resolved')

    // 变形载荷不进缓存：不清缓存的第二轮加载重新请求 `/cycles`。
    expect(calls.filter((call) => call.path === CYCLES_PATH)).toHaveLength(1)
    await useOverviewDataStore.getState().loadOverview(query)
    expect(calls.filter((call) => call.path === CYCLES_PATH)).toHaveLength(2)
    expect(useOverviewDataStore.getState().dataAnomalies).toEqual(['起报时次'])
  })
})

describe('overview data shape guards: bootstrap (E3)', () => {
  it('settles a non-array basins payload to 数据异常 in both bootstrap and enrichment, recording 流域清单 once', async () => {
    mockApi({ '/api/v1/basins': () => success({ items: [basin] }) })

    const outcome = await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.error).toContain('basins: 数据异常')
    expect(state.bootstrapError).toBe('basins: 数据异常')
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.dataAnomalies).toEqual(['流域清单'])
    expect(outcome).toBe('resolved')
  })

  it('settles a layers payload with a null element to layers: 数据异常 instead of loading indefinitely', async () => {
    mockApi({ '/api/v1/layers': () => success([null]) })

    const outcome = await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.bootstrapError).toBe('layers: 数据异常')
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.dataAnomalies).toEqual(['图层目录'])
    expect(outcome).toBe('resolved')
  })

  it('names each side with its own word when basins are malformed and layers fail with an API error', async () => {
    mockApi({
      '/api/v1/basins': () => success({ items: [basin] }),
      // 主分支 reject → `fetchLayers` 的 apiFetch 兜底同样 reject（测试环境无服务端）。
      '/api/v1/layers': () => {
        throw new Error('layer catalog down')
      },
    })

    await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.bootstrapError).toBe('basins: 数据异常；layers: 暂不可用')
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.dataAnomalies).toEqual(['流域清单'])
  })
})

describe('overview data shape guards: per-cycle valid times (E4)', () => {
  it('degrades an ifs valid-times object without valid_times to the scoped error text and records 有效时次', async () => {
    mockApi({
      [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source),
      [VALID_TIMES_PATH]: () => success({ layer_id: 'discharge' }),
    })

    await settleLoad(useOverviewDataStore.getState().loadOverview(ifsQuery))

    const state = useOverviewDataStore.getState()
    expect(dischargeLayer()?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(state.validTimesByCycle[m11SourceCycleKey('ifs', IFS_CYCLE)]).toEqual({ status: 'error' })
    expect(state.dataAnomalies).toEqual(['有效时次'])
  })
})

describe('overview data shape guards: precipitation index and runs (E5)', () => {
  it('degrades a precip index with three bounds to the scoped error state and records 降水索引', async () => {
    mockApi({ [PRECIP_INDEX_PATH]: () => success({ ...precipIndex, bounds: [1, 2, 3] }) })

    await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.precipIndexByCycle[m11SourceCycleKey('gfs', DEFAULT_CYCLE)]).toEqual({ status: 'error' })
    expect(state.dataAnomalies).toEqual(['降水索引'])
  })

  it('reports a runs page whose items are not an array as runs: 数据异常 without touching the map bootstrap', async () => {
    mockApi({ '/api/v1/runs': () => success({ items: 'x', total: 0, limit: 20, offset: 0 }) })

    const outcome = await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.error).toContain('runs: 数据异常')
    expect(state.dataAnomalies).toEqual(['运行记录'])
    expect(state.bootstrapError).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.overview?.bootstrap).not.toBeNull()
    expect(outcome).toBe('resolved')
  })
})

describe('overview data shape guards: non-shape errors and generations (E6)', () => {
  it('keeps 暂不可用 and records no anomaly when /cycles fails with an API error', async () => {
    mockApi({ [CYCLES_PATH]: () => apiError('CYCLES_DOWN') })

    await useOverviewDataStore.getState().loadOverview(query)

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource.gfs).toEqual({ status: 'error' })
    expect(state.dataAnomalies).toEqual([])
  })

  it('keeps basins: 暂不可用 and records no anomaly when /basins fails with an API error', async () => {
    mockApi({ '/api/v1/basins': () => apiError('BASINS_DOWN') })

    await settleLoad(useOverviewDataStore.getState().loadOverview(query))

    const state = useOverviewDataStore.getState()
    expect(state.bootstrapError).toBe('basins: 暂不可用')
    expect(state.error).toBe('basins: 暂不可用')
    expect(state.dataAnomalies).toEqual([])
  })

  it('drops a shape error delivered by a stale generation after a newer load started', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [CYCLES_PATH]: async (options) => {
        if (options.params?.query?.source === 'ifs') return cyclesPayload('ifs')
        await gate
        return success({ source: 'gfs', cycles: [null], default_cycle: DEFAULT_CYCLE })
      },
    })

    const staleLoad = settleLoad(useOverviewDataStore.getState().loadOverview(query))
    await vi.waitFor(() => expect(calls.some((call) => call.path === CYCLES_PATH && call.query?.source === 'gfs')).toBe(true))

    await useOverviewDataStore.getState().loadOverview(ifsQuery)
    release()
    await staleLoad

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource.gfs).toBeUndefined()
    expect(state.cyclesBySource.ifs?.status).toBe('available')
    expect(state.dataAnomalies).toEqual([])
  })

  it('clears the anomaly on the next load once the payload is valid again', async () => {
    mockApi({ [CYCLES_PATH]: () => success({ source: 'gfs', cycles: [DEFAULT_CYCLE], default_cycle: DEFAULT_CYCLE }) })
    await useOverviewDataStore.getState().loadOverview(query)
    expect(useOverviewDataStore.getState().dataAnomalies).toEqual(['起报时次'])

    mockApi()
    await useOverviewDataStore.getState().loadOverview(query)

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource.gfs?.status).toBe('available')
    expect(state.dataAnomalies).toEqual([])
  })

  it('does not reset dataAnomalies from clearOverviewDataCache (the next load does)', async () => {
    mockApi({ [CYCLES_PATH]: () => success({ source: 'gfs', cycles: [null], default_cycle: DEFAULT_CYCLE }) })
    await useOverviewDataStore.getState().loadOverview(query)
    clearOverviewDataCache()
    expect(useOverviewDataStore.getState().dataAnomalies).toEqual(['起报时次'])
  })
})
