import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { HydroMetStation } from '@/stores/hydroMetBootstrap'
import {
  STATION_CLIENT_CAP,
  STATION_CONTEXT_CAP,
  STATION_FETCH_CONCURRENCY,
  STATION_PAGE_LIMIT,
  stationLayerRequestKey,
  useStationLayerDataStore,
} from '@/stores/stationLayerData'

const fetchHydroMetStationsByIdentityMock = vi.fn()
const fetchHydroMetLatestProductMock = vi.fn()

vi.mock('@/stores/hydroMetBootstrap', async () => {
  const actual = await vi.importActual<typeof import('@/stores/hydroMetBootstrap')>('@/stores/hydroMetBootstrap')
  return {
    ...actual,
    fetchHydroMetLatestProduct: (...args: unknown[]) => fetchHydroMetLatestProductMock(...args),
    fetchHydroMetStationsByIdentity: (...args: unknown[]) => fetchHydroMetStationsByIdentityMock(...args),
  }
})

function station(id: string): HydroMetStation {
  return {
    station_id: id,
    basin_version_id: 'bv-1',
    station_name: `Station ${id}`,
    geom: { type: 'Point', coordinates: [100, 30] },
    station_role: 'representative',
    active_flag: true,
    created_at: '2026-01-01T00:00:00Z',
  } as HydroMetStation
}

function stations(prefix: string, count: number, start = 0): HydroMetStation[] {
  return Array.from({ length: count }, (_, index) => station(`${prefix}-${start + index}`))
}

function stationPage(items: HydroMetStation[], totalCount?: number) {
  return {
    items,
    total_count: totalCount,
    limit: STATION_PAGE_LIMIT,
    offset: 0,
  }
}

type IdentityArg = { basinVersionId: string; modelId?: string }
type QueryArg = { limit: number; offset: number }
type Page = ReturnType<typeof stationPage>

function contexts(...basinIds: string[]) {
  return basinIds.map((basinId) => ({ basinId, basinVersionId: `bv-${basinId}` }))
}

/**
 * 受控 resolver：每个流域版本的首页请求挂起，由测试决定谁先完成。
 * requested 按请求发出的顺序记录 basinVersionId；settle 在该请求发出后才生效（否则抛错，避免测试空转）。
 */
function controlStationRequests() {
  const pending = new Map<string, { resolve: (page: Page) => void; reject: (error: Error) => void }>()
  const requested: string[] = []
  fetchHydroMetStationsByIdentityMock.mockImplementation(
    (identity: IdentityArg) =>
      new Promise<Page>((resolve, reject) => {
        requested.push(identity.basinVersionId)
        pending.set(identity.basinVersionId, { resolve, reject })
      }),
  )
  const take = (basinVersionId: string) => {
    const entry = pending.get(basinVersionId)
    if (!entry) throw new Error(`no pending station request for ${basinVersionId}`)
    pending.delete(basinVersionId)
    return entry
  }
  return {
    requested,
    pendingIds: () => [...pending.keys()],
    resolve: (basinVersionId: string, page: Page) => take(basinVersionId).resolve(page),
    reject: (basinVersionId: string, error: Error) => take(basinVersionId).reject(error),
    waitForRequest: (basinVersionId: string) => vi.waitFor(() => expect(requested).toContain(basinVersionId)),
  }
}

/** 让已排队的 promise 回调全部跑完（一个宏任务之后），用于断言"没有再发请求"。 */
function settle() {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, 0)
  })
}

describe('stationLayerData store (M26-3)', () => {
  beforeEach(() => {
    fetchHydroMetStationsByIdentityMock.mockReset()
    fetchHydroMetLatestProductMock.mockReset()
    useStationLayerDataStore.getState().clear()
  })

  it('uses the selected direct-grid run model for station membership', async () => {
    fetchHydroMetLatestProductMock.mockResolvedValue({
      basin_version_id: 'bv-qhh',
      model_id: 'dg-qhh-gfs',
    })
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(stations('dg', 2), 2))

    await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'basins_qhh', basinVersionId: 'bv-qhh', source: 'GFS', cycle: '2026-07-06T00:00:00Z' }],
    })

    expect(fetchHydroMetLatestProductMock).toHaveBeenCalledWith({
      basinId: 'basins_qhh',
      source: 'GFS',
      cycle: '2026-07-06T00:00:00Z',
    })
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledWith(
      { basinVersionId: 'bv-qhh', modelId: 'dg-qhh-gfs' },
      { limit: STATION_PAGE_LIMIT, offset: 0 },
    )
  })

  it('builds structural request keys for delimiter-shaped basin identities', () => {
    const first = stationLayerRequestKey({
      basinContexts: [{ basinId: 'a:b', basinVersionId: 'c' }],
    })
    const second = stationLayerRequestKey({
      basinContexts: [{ basinId: 'a', basinVersionId: 'b:c' }],
    })

    expect(first).not.toBe(second)
    expect(
      stationLayerRequestKey({
        basinContexts: [
          { basinId: ' qhh ', basinVersionId: ' bv-qhh ' },
          { basinId: 'qhh', basinVersionId: 'bv-qhh' },
        ],
      }),
    ).toBe(stationLayerRequestKey({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
    }))
  })

  it('loads every visible basin version and pages by offset until total is reached', async () => {
    // Heihe-style: total 1709 → 首页 500 + 3 翻页 (500/500/209)。两个流域并发取数，按身份 + offset 应答。
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg, query: QueryArg) => {
      if (identity.basinVersionId === 'bv-qhh') return stationPage(stations('qhh', 386, 0), 386)
      return stationPage(stations('heihe', Math.min(STATION_PAGE_LIMIT, 1709 - query.offset), query.offset), 1709)
    })

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [
        { basinId: 'heihe', basinVersionId: 'bv-heihe' },
        { basinId: 'qhh', basinVersionId: 'bv-qhh' },
      ],
    })

    expect(data.total).toBe(2095)
    expect(data.totalKnown).toBe(true)
    expect(data.loaded).toBe(2095)
    expect(data.truncated).toBe(false)
    expect(data.unloadedBasinCount).toBe(0)
    expect(data.stationBasinIds['heihe-0']).toBe('heihe')
    expect(data.stationBasinIds['qhh-0']).toBe('qhh')
    // 身份直接来自流域版本，不依赖 latest-product ready。
    expect(fetchHydroMetStationsByIdentityMock.mock.calls[0][0]).toEqual({ basinVersionId: 'bv-heihe' })
    // offset 翻页正确：首页 0，后续 500 / 1000 / 1500；每页都按 STATION_PAGE_LIMIT 请求。
    const heihePages = fetchHydroMetStationsByIdentityMock.mock.calls
      .filter((call) => (call[0] as IdentityArg).basinVersionId === 'bv-heihe')
      .map((call) => call[1] as QueryArg)
    expect(heihePages).toEqual([0, 500, 1000, 1500].map((offset) => ({ limit: STATION_PAGE_LIMIT, offset })))
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(5)
    expect(useStationLayerDataStore.getState().data?.loaded).toBe(2095)
  })

  it('caps a single basin larger than the client cap and flags truncation honestly', async () => {
    // 每次翻页返回满页，直到该流域自己的行数触顶 cap；其后没有别的流域 → unloadedBasinCount 为 0。
    const total = STATION_CLIENT_CAP + 3 * STATION_PAGE_LIMIT
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (_identity: unknown, query: QueryArg) =>
      stationPage(stations('cn', query.limit, query.offset), total),
    )

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'china', basinVersionId: 'bv-cn' }],
    })

    expect(data.total).toBe(total)
    expect(data.totalKnown).toBe(true)
    expect(data.loaded).toBe(STATION_CLIENT_CAP)
    expect(data.truncated).toBe(true)
    expect(data.unloadedBasinCount).toBe(0)
    // 触顶即停翻页，且每页都按 STATION_PAGE_LIMIT 请求（不随剩余 cap 收窄）。
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(STATION_CLIENT_CAP / STATION_PAGE_LIMIT)
    expect(
      fetchHydroMetStationsByIdentityMock.mock.calls.every((call) => (call[1] as QueryArg).limit === STATION_PAGE_LIMIT),
    ).toBe(true)
  })

  it('caps a backend page that over-returns beyond the remaining client cap', async () => {
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(
      stationPage(stations('over', STATION_CLIENT_CAP + 37, 0), STATION_CLIENT_CAP + 37),
    )

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'over', basinVersionId: 'bv-over' }],
    })

    expect(data.loaded).toBe(STATION_CLIENT_CAP)
    expect(data.stations).toHaveLength(STATION_CLIENT_CAP)
    expect(data.truncated).toBe(true)
    expect(data.unloadedBasinCount).toBe(0)
    expect(data.total).toBe(STATION_CLIENT_CAP + 37)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledWith(
      { basinVersionId: 'bv-over' },
      { limit: STATION_PAGE_LIMIT, offset: 0 },
    )
  })

  describe('safety valves (#2867)', () => {
    // c0 + c1 累计越过 cap：c1 是截断流域，c2..c7 共 6 个流域未加载（其中 c3 的请求失败，同样被丢弃）。
    const VALVE_CONTEXTS = contexts('c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7')
    const C0_COUNT = STATION_CLIENT_CAP - 20_000
    const C1_COUNT = 20_005

    function settleValveBasin(control: ReturnType<typeof controlStationRequests>, basinId: string) {
      const basinVersionId = `bv-${basinId}`
      if (basinId === 'c0') return control.resolve(basinVersionId, stationPage(stations('c0', C0_COUNT), C0_COUNT))
      if (basinId === 'c1') return control.resolve(basinVersionId, stationPage(stations('c1', C1_COUNT), C1_COUNT))
      if (basinId === 'c3') return control.reject(basinVersionId, new Error('c3 failed'))
      return control.resolve(basinVersionId, stationPage(stations(basinId, 10), 10))
    }

    function expectValveResult(data: Awaited<ReturnType<ReturnType<typeof useStationLayerDataStore.getState>['loadStationLayer']>>) {
      expect(data.loaded).toBe(STATION_CLIENT_CAP)
      expect(data.stations).toHaveLength(STATION_CLIENT_CAP)
      expect(data.unloadedBasinCount).toBe(6)
      // total 只含截断流域及之前的流域，不含其后已取到的 10 站流域。
      expect(data.total).toBe(C0_COUNT + C1_COUNT)
      expect(data.totalKnown).toBe(false)
      expect(data.truncated).toBe(true)
      expect(data.failedBasinIds).toEqual([])
      expect(data.stations[0]?.station_id).toBe('c0-0')
      expect(data.stations[C0_COUNT]?.station_id).toBe('c1-0')
      expect(data.stations[STATION_CLIENT_CAP - 1]?.station_id).toBe(`c1-${STATION_CLIENT_CAP - C0_COUNT - 1}`)
      expect(new Set(Object.values(data.stationBasinIds))).toEqual(new Set(['c0', 'c1']))
    }

    it('cuts at the client cap and counts the contexts after the cut when the cut context finishes first', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: VALVE_CONTEXTS })
      await control.waitForRequest('bv-c5')

      // c0 完成时累计未到 cap：腾出的 worker 认领 c6。
      settleValveBasin(control, 'c0')
      await control.waitForRequest('bv-c6')
      // c1 完成后已取到的站点数达到 cap：之后腾出的 worker 不再认领 c7。
      settleValveBasin(control, 'c1')
      await settle()
      for (const basinId of ['c2', 'c3', 'c4', 'c5', 'c6']) settleValveBasin(control, basinId)

      expectValveResult(await load)
      expect(control.requested).toEqual(['bv-c0', 'bv-c1', 'bv-c2', 'bv-c3', 'bv-c4', 'bv-c5', 'bv-c6'])
    })

    it('gives the same cut, total and unloaded count when the contexts after the cut finish first', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: VALVE_CONTEXTS })
      await control.waitForRequest('bv-c5')

      // 截断点之后的流域先完成（含失败的 c3），c6 / c7 也被认领并取到；最后才是 c1、c0。
      for (const basinId of ['c5', 'c4', 'c3', 'c2']) settleValveBasin(control, basinId)
      await control.waitForRequest('bv-c7')
      for (const basinId of ['c7', 'c6', 'c1', 'c0']) settleValveBasin(control, basinId)

      expectValveResult(await load)
      expect(control.requested).toHaveLength(8)
    })

    it('reports no unloaded basin when the sum lands exactly on the client cap with nothing after the cut', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts('c0', 'c1') })
      await control.waitForRequest('bv-c1')

      control.resolve('bv-c1', stationPage(stations('c1', 20_000), 20_000))
      control.resolve('bv-c0', stationPage(stations('c0', C0_COUNT), C0_COUNT))
      const data = await load

      expect(data.loaded).toBe(STATION_CLIENT_CAP)
      expect(data.total).toBe(STATION_CLIENT_CAP)
      expect(data.unloadedBasinCount).toBe(0)
      expect(data.totalKnown).toBe(true)
      expect(data.truncated).toBe(false)
    })

    it('keeps the loaded < total marking when the last context is cut mid-way', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts('c0', 'c1') })
      await control.waitForRequest('bv-c1')

      control.resolve('bv-c1', stationPage(stations('c1', C1_COUNT), C1_COUNT))
      control.resolve('bv-c0', stationPage(stations('c0', C0_COUNT), C0_COUNT))
      const data = await load

      expect(data.loaded).toBe(STATION_CLIENT_CAP)
      expect(data.total).toBe(STATION_CLIENT_CAP + 5)
      expect(data.unloadedBasinCount).toBe(0)
      expect(data.totalKnown).toBe(true)
      expect(data.truncated).toBe(true)
    })

    it('requests only the first STATION_CONTEXT_CAP contexts and counts the excess as unloaded', async () => {
      const control = controlStationRequests()
      const basinContexts = contexts(...Array.from({ length: STATION_CONTEXT_CAP + 3 }, (_, index) => `basin-${index}`))

      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts })
      let done = false
      void load.finally(() => {
        done = true
      })
      // 每一轮把在途请求按逆序应答（空清单），直到加载结束。
      while (!done) {
        for (const basinVersionId of control.pendingIds().reverse()) control.resolve(basinVersionId, stationPage([], 0))
        await settle()
      }
      const data = await load

      expect(data.total).toBe(0)
      expect(data.loaded).toBe(0)
      expect(data.unloadedBasinCount).toBe(3)
      expect(data.totalKnown).toBe(false)
      expect(data.truncated).toBe(true)
      // 按上下文顺序认领，且只认领前 STATION_CONTEXT_CAP 个。
      expect(control.requested).toEqual(
        basinContexts.slice(0, STATION_CONTEXT_CAP).map((context) => context.basinVersionId),
      )
      expect(control.requested).not.toContain(`bv-basin-${STATION_CONTEXT_CAP}`)
    })
  })

  describe('load every basin with bounded concurrency (#2867)', () => {
    it('gives every basin its stations when there are more than 50 contexts and more than 5000 stations', async () => {
      const basinIds = Array.from({ length: 60 }, (_, index) => `basin-${index}`)
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg) => {
        const basinId = identity.basinVersionId.slice('bv-'.length)
        return stationPage(stations(basinId, 100), 100)
      })

      const data = await useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts(...basinIds) })

      expect(data.loaded).toBe(6000)
      expect(data.total).toBe(6000)
      expect(data.totalKnown).toBe(true)
      expect(data.truncated).toBe(false)
      expect(data.unloadedBasinCount).toBe(0)
      expect(data.failedBasinIds).toEqual([])
      const countByBasin = new Map<string, number>()
      for (const basinId of Object.values(data.stationBasinIds)) countByBasin.set(basinId, (countByBasin.get(basinId) ?? 0) + 1)
      expect([...countByBasin.keys()]).toEqual(basinIds)
      expect([...countByBasin.values()].every((count) => count === 100)).toBe(true)
      expect(data.stationBasinIds['basin-59-99']).toBe('basin-59')
    })

    it('keeps more than one and at most STATION_FETCH_CONCURRENCY basin loads in flight', async () => {
      // 每个流域两页：首页挂起由测试放行，第二页立即返回；worker 在两页之间不让出名额。
      const firstPages = new Map<string, (page: Page) => void>()
      const inFlightBasins = new Set<string>()
      let peak = 0
      fetchHydroMetStationsByIdentityMock.mockImplementation((identity: IdentityArg, query: QueryArg) => {
        const basinId = identity.basinVersionId.slice('bv-'.length)
        if (query.offset > 0) {
          inFlightBasins.delete(basinId)
          return Promise.resolve(stationPage(stations(basinId, 1, query.offset), STATION_PAGE_LIMIT + 1))
        }
        inFlightBasins.add(basinId)
        peak = Math.max(peak, inFlightBasins.size)
        return new Promise<Page>((resolve) => {
          firstPages.set(basinId, resolve)
        })
      })
      const basinIds = Array.from({ length: 20 }, (_, index) => `basin-${index}`)

      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts(...basinIds) })
      await settle()
      // 没有任何请求完成之前，恰好 STATION_FETCH_CONCURRENCY 个流域在途，第 7 个没有被认领。
      expect([...firstPages.keys()]).toEqual(basinIds.slice(0, STATION_FETCH_CONCURRENCY))

      let done = false
      void load.finally(() => {
        done = true
      })
      while (!done) {
        for (const [basinId, resolve] of [...firstPages].reverse()) {
          firstPages.delete(basinId)
          resolve(stationPage(stations(basinId, STATION_PAGE_LIMIT), STATION_PAGE_LIMIT + 1))
        }
        await settle()
      }
      const data = await load

      expect(STATION_FETCH_CONCURRENCY).toBeGreaterThan(1)
      expect(peak).toBe(STATION_FETCH_CONCURRENCY)
      expect(data.loaded).toBe(20 * (STATION_PAGE_LIMIT + 1))
      expect(data.truncated).toBe(false)
    })

    it('merges stations in context order when later contexts resolve first', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts('c0', 'c1', 'c2') })
      await control.waitForRequest('bv-c2')

      control.resolve('bv-c2', stationPage(stations('c2', 2), 2))
      await settle()
      control.resolve('bv-c1', stationPage(stations('c1', 3), 3))
      await settle()
      control.resolve('bv-c0', stationPage(stations('c0', 1), 1))
      const data = await load

      expect(data.stations.map((item) => item.station_id)).toEqual(['c0-0', 'c1-0', 'c1-1', 'c1-2', 'c2-0', 'c2-1'])
      expect(Object.keys(data.stationBasinIds)).toEqual(['c0-0', 'c1-0', 'c1-1', 'c1-2', 'c2-0', 'c2-1'])
      expect(data.total).toBe(6)
      expect(data.truncated).toBe(false)
    })

    it('lists failed basins in context order and keeps the others when later contexts fail first', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts('c0', 'c1', 'c2', 'c3') })
      await control.waitForRequest('bv-c3')

      control.reject('bv-c2', new Error('c2 failed'))
      await settle()
      control.resolve('bv-c3', stationPage(stations('c3', 2), 2))
      control.resolve('bv-c1', stationPage(stations('c1', 1), 1))
      await settle()
      control.reject('bv-c0', new Error('c0 failed'))
      const data = await load

      expect(data.failedBasinIds).toEqual(['c0', 'c2'])
      expect(data.stations.map((item) => item.station_id)).toEqual(['c1-0', 'c3-0', 'c3-1'])
      expect(data.total).toBe(3)
      expect(data.totalKnown).toBe(false)
      expect(data.truncated).toBe(true)
      expect(data.unloadedBasinCount).toBe(0)
      expect(useStationLayerDataStore.getState().error).toBeNull()
    })

    it('rejects with the first error in context order when every basin fails, whichever fails first', async () => {
      const control = controlStationRequests()
      const load = useStationLayerDataStore.getState().loadStationLayer({ basinContexts: contexts('c0', 'c1', 'c2') })
      const outcome = load.then(
        () => 'resolved',
        (error: Error) => error.message,
      )
      await control.waitForRequest('bv-c2')

      control.reject('bv-c2', new Error('c2 failed'))
      await settle()
      control.reject('bv-c1', new Error('c1 failed'))
      await settle()
      control.reject('bv-c0', new Error('c0 failed'))

      expect(await outcome).toBe('c0 failed')
      const state = useStationLayerDataStore.getState()
      expect(state.data).toBeNull()
      expect(state.error).toBe('c0 failed')
    })
  })

  describe('superseded load (#2867)', () => {
    const supersededMessage = '代站图层请求已被取代'

    it('claims no further context, requests no further page and rejects once a different key starts', async () => {
      const control = controlStationRequests()
      const firstContexts = contexts('a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7')
      const secondRequest = { basinContexts: contexts('b0') }
      const store = useStationLayerDataStore.getState()

      const first = store.loadStationLayer({ basinContexts: firstContexts })
      const firstOutcome = first.then(
        () => 'resolved',
        (error: Error) => error.message,
      )
      await control.waitForRequest('bv-a5')

      const second = store.loadStationLayer(secondRequest)
      await control.waitForRequest('bv-b0')
      control.resolve('bv-b0', stationPage(stations('b0', 4), 4))
      const secondData = await second

      // 旧加载在途的 6 个首页此时才返回，每个流域都还有第二页。
      for (const basinId of ['a0', 'a1', 'a2', 'a3', 'a4', 'a5']) {
        control.resolve(`bv-${basinId}`, stationPage(stations(basinId, STATION_PAGE_LIMIT), 2 * STATION_PAGE_LIMIT))
      }

      expect(await firstOutcome).toBe(supersededMessage)
      await settle()
      // 没有第二页请求，也没有认领 a6 / a7。
      expect(control.requested).toEqual(['bv-a0', 'bv-a1', 'bv-a2', 'bv-a3', 'bv-a4', 'bv-a5', 'bv-b0'])
      expect(control.pendingIds()).toEqual([])
      const state = useStationLayerDataStore.getState()
      expect(state.data).toBe(secondData)
      expect(state.data?.stations.map((item) => item.station_id)).toEqual(['b0-0', 'b0-1', 'b0-2', 'b0-3'])
      expect(state.requestKey).toBe(stationLayerRequestKey(secondRequest))
      expect(state.error).toBeNull()
      expect(state.loading).toBe(false)
    })

    it('claims no further context when the in-flight basins finish on their only page', async () => {
      const control = controlStationRequests()
      // 源已解析：认领一个流域的第一步是 latest-product 查询，被取代后连这一步也不得再发。
      fetchHydroMetLatestProductMock.mockImplementation(async ({ basinId }: { basinId: string }) => ({
        basin_version_id: `bv-${basinId}`,
        model_id: 'm-gfs',
      }))
      const store = useStationLayerDataStore.getState()
      const first = store.loadStationLayer({
        basinContexts: contexts('a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7').map((context) => ({
          ...context,
          source: 'GFS' as const,
        })),
      })
      const firstOutcome = first.then(
        () => 'resolved',
        (error: Error) => error.message,
      )
      await control.waitForRequest('bv-a5')

      const second = store.loadStationLayer({ basinContexts: contexts('b0') })
      await control.waitForRequest('bv-b0')
      // 在途的 6 个流域都只有一页：返回后 worker 回到认领处，不得再认领 a6 / a7。
      for (const basinId of ['a0', 'a1', 'a2', 'a3', 'a4', 'a5']) {
        control.resolve(`bv-${basinId}`, stationPage(stations(basinId, 2), 2))
      }
      await settle()

      expect(control.requested).toEqual(['bv-a0', 'bv-a1', 'bv-a2', 'bv-a3', 'bv-a4', 'bv-a5', 'bv-b0'])
      expect(fetchHydroMetLatestProductMock.mock.calls.map((call) => (call[0] as { basinId: string }).basinId)).toEqual([
        'a0',
        'a1',
        'a2',
        'a3',
        'a4',
        'a5',
      ])
      expect(await firstOutcome).toBe(supersededMessage)
      control.resolve('bv-b0', stationPage(stations('b0', 4), 4))
      const secondData = await second
      expect(useStationLayerDataStore.getState().data).toBe(secondData)
    })

    it('requests no station page when superseded during the latest-product lookup', async () => {
      const products: Array<(product: { basin_version_id: string; model_id: string }) => void> = []
      fetchHydroMetLatestProductMock.mockImplementation(
        () =>
          new Promise((resolve) => {
            products.push(resolve)
          }),
      )
      fetchHydroMetStationsByIdentityMock.mockResolvedValue(stationPage(stations('b0', 1), 1))
      const store = useStationLayerDataStore.getState()

      const first = store.loadStationLayer({
        basinContexts: [{ basinId: 'a0', basinVersionId: 'bv-a0', source: 'GFS', cycle: '2026-07-06T00:00:00Z' }],
      })
      const firstOutcome = first.then(
        () => 'resolved',
        (error: Error) => error.message,
      )
      await vi.waitFor(() => expect(products).toHaveLength(1))
      await store.loadStationLayer({ basinContexts: contexts('b0') })
      products[0]?.({ basin_version_id: 'bv-a0', model_id: 'm-gfs' })

      expect(await firstOutcome).toBe(supersededMessage)
      expect(fetchHydroMetStationsByIdentityMock.mock.calls.map((call) => call[0])).toEqual([{ basinVersionId: 'bv-b0' }])
      expect(useStationLayerDataStore.getState().data?.stations[0]?.station_id).toBe('b0-0')
    })

    it('rejects a load in flight when the store is cleared', async () => {
      const control = controlStationRequests()
      const store = useStationLayerDataStore.getState()
      const first = store.loadStationLayer({ basinContexts: contexts('a0') })
      const firstOutcome = first.then(
        () => 'resolved',
        (error: Error) => error.message,
      )
      await control.waitForRequest('bv-a0')

      store.clear()
      control.resolve('bv-a0', stationPage(stations('a0', STATION_PAGE_LIMIT), 2 * STATION_PAGE_LIMIT))

      expect(await firstOutcome).toBe(supersededMessage)
      expect(control.requested).toEqual(['bv-a0'])
      const state = useStationLayerDataStore.getState()
      expect(state.data).toBeNull()
      expect(state.error).toBeNull()
      expect(state.requestKey).toBeNull()
    })
  })

  it('loads a single page basin without truncation', async () => {
    // Qhh-style: total 386 ≤ page limit → 不翻页。
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(stations('qhh', 386, 0), 386))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
    })

    expect(data.total).toBe(386)
    expect(data.totalKnown).toBe(true)
    expect(data.loaded).toBe(386)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('rejects missing basin versions without per-basin discovery requests', async () => {
    const request = {
      basinContexts: [{ basinId: 'qhh', basinVersionId: null }],
    }

    await expect(useStationLayerDataStore.getState().loadStationLayer(request)).rejects.toThrow(
      '代站图层缺少可用流域版本身份',
    )
    expect(fetchHydroMetStationsByIdentityMock).not.toHaveBeenCalled()
    expect(useStationLayerDataStore.getState().requestKey).toBe(stationLayerRequestKey(request))
  })

  it('handles a zero-station basin without false truncation', async () => {
    // 空流域：total_count=0 → 不翻页、loaded=0、truncated=false（0<0 为假）。
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage([], 0))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'empty', basinVersionId: 'bv-empty' }],
    })

    expect(data.total).toBe(0)
    expect(data.totalKnown).toBe(true)
    expect(data.loaded).toBe(0)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('falls back to first-page length when total_count is missing and does not over-report truncation', async () => {
    // total_count 缺失/非有限：回退首页长度，truncated 不被误报（不静默截断也不假完整）。
    const firstPage = stations('nocount', 386, 0)
    fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(firstPage))

    const data = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'nocount', basinVersionId: 'bv-nocount' }],
    })

    expect(data.loaded).toBe(386)
    expect(data.totalKnown).toBe(true)
    expect(data.truncated).toBe(false)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces a mid-pagination error without silently flagging a complete load', async () => {
    // Heihe-style 首页 ready，但第二页抛错：整体 reject、error 暴露、data 置空，
    // 不得返回 truncated=false 的"看似完整"结果掩盖缺失。
    fetchHydroMetStationsByIdentityMock
      .mockResolvedValueOnce(stationPage(stations('heihe', STATION_PAGE_LIMIT, 0), 1709))
      .mockRejectedValueOnce(new Error('第二页加载失败'))

    await expect(
      useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [{ basinId: 'heihe', basinVersionId: 'bv-heihe' }],
      }),
    ).rejects.toThrow('第二页加载失败')

    const state = useStationLayerDataStore.getState()
    expect(state.error).toBeTruthy()
    expect(state.data).toBeNull()
  })

  it('dedupes concurrent identical requests', async () => {
    fetchHydroMetStationsByIdentityMock.mockResolvedValue(stationPage(stations('qhh', 386, 0), 386))

    const request = { basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }] }
    const [a, b] = await Promise.all([
      useStationLayerDataStore.getState().loadStationLayer(request),
      useStationLayerDataStore.getState().loadStationLayer(request),
    ])

    expect(a).toBe(b)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('returns settled matching data without refetching the same station inventory key', async () => {
    fetchHydroMetStationsByIdentityMock.mockResolvedValue(stationPage(stations('qhh', 2, 0), 2))

    const first = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
    })
    const second = await useStationLayerDataStore.getState().loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
    })

    expect(second).toBe(first)
    expect(fetchHydroMetStationsByIdentityMock).toHaveBeenCalledTimes(1)
  })

  it('does not reuse settled data for delimiter-colliding basin identities', async () => {
    fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: { basinVersionId: string }) => {
      if (identity.basinVersionId === 'c') return stationPage([station('shape-a')], 1)
      if (identity.basinVersionId === 'b:c') return stationPage([station('shape-b')], 1)
      throw new Error(`Unexpected basin version ${identity.basinVersionId}`)
    })

    const firstRequest = { basinContexts: [{ basinId: 'a:b', basinVersionId: 'c' }] }
    const secondRequest = { basinContexts: [{ basinId: 'a', basinVersionId: 'b:c' }] }

    const first = await useStationLayerDataStore.getState().loadStationLayer(firstRequest)
    const second = await useStationLayerDataStore.getState().loadStationLayer(secondRequest)

    expect(stationLayerRequestKey(firstRequest)).not.toBe(stationLayerRequestKey(secondRequest))
    expect(second).not.toBe(first)
    expect(first.stations[0]?.station_id).toBe('shape-a')
    expect(second.stations[0]?.station_id).toBe('shape-b')
    expect(useStationLayerDataStore.getState().data?.stations[0]?.station_id).toBe('shape-b')
    expect(fetchHydroMetStationsByIdentityMock.mock.calls.map((call) => call[0])).toEqual([
      { basinVersionId: 'c' },
      { basinVersionId: 'b:c' },
    ])
  })

  it('does not reuse in-flight data for delimiter-colliding basin identities', async () => {
    // 用数组接住 resolver：`let x = null` 在回调里赋值时 TS 的控制流分析仍把使用点窄化成 null，
    // 数组元素访问不参与该窄化。运行时语义不变（只会有一个 resolver）。
    const firstResolvers: Array<(value: ReturnType<typeof stationPage>) => void> = []
    fetchHydroMetStationsByIdentityMock.mockImplementation((identity: { basinVersionId: string }) => {
      if (identity.basinVersionId === 'c') {
        return new Promise<ReturnType<typeof stationPage>>((resolve) => {
          firstResolvers.push(resolve)
        })
      }
      if (identity.basinVersionId === 'b:c') return Promise.resolve(stationPage([station('shape-b')], 1))
      throw new Error(`Unexpected basin version ${identity.basinVersionId}`)
    })

    const firstRequest = { basinContexts: [{ basinId: 'a:b', basinVersionId: 'c' }] }
    const secondRequest = { basinContexts: [{ basinId: 'a', basinVersionId: 'b:c' }] }

    const firstPromise = useStationLayerDataStore.getState().loadStationLayer(firstRequest)
    const secondPromise = useStationLayerDataStore.getState().loadStationLayer(secondRequest)

    expect(fetchHydroMetStationsByIdentityMock.mock.calls.map((call) => call[0])).toEqual([
      { basinVersionId: 'c' },
      { basinVersionId: 'b:c' },
    ])

    firstResolvers[0]?.(stationPage([station('shape-a')], 1))
    const [first, second] = await Promise.all([firstPromise, secondPromise])

    expect(first.stations[0]?.station_id).toBe('shape-a')
    expect(second.stations[0]?.station_id).toBe('shape-b')
  })

  it('does not overwrite a newer request with a stale earlier response', async () => {
    const firstResolvers: Array<(value: unknown) => void> = []
    fetchHydroMetStationsByIdentityMock
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            firstResolvers.push(resolve)
          }),
      )
      .mockResolvedValueOnce(stationPage(stations('heihe', 10, 0), 10))

    const store = useStationLayerDataStore.getState()
    const firstPromise = store.loadStationLayer({
      basinContexts: [{ basinId: 'qhh', basinVersionId: 'bv-qhh' }],
    }).catch(() => undefined)
    const secondRequest = {
      basinContexts: [{ basinId: 'heihe', basinVersionId: 'bv-heihe' }],
    }
    const secondData = await store.loadStationLayer(secondRequest)

    // 让过期的第一个请求晚一步 resolve。
    firstResolvers[0]?.(stationPage(stations('qhh', 5, 0), 5))
    await firstPromise

    expect(secondData.total).toBe(10)
    expect(useStationLayerDataStore.getState().data?.total).toBe(10)
    expect(useStationLayerDataStore.getState().requestKey).toBe(stationLayerRequestKey(secondRequest))
  })

  it('surfaces an error when no basin version identity is available', async () => {
    const request = { basinContexts: [] }

    await expect(
      useStationLayerDataStore.getState().loadStationLayer(request),
    ).rejects.toThrow()
    expect(useStationLayerDataStore.getState().error).toBeTruthy()
    expect(useStationLayerDataStore.getState().data).toBeNull()
    expect(useStationLayerDataStore.getState().requestKey).toBe(stationLayerRequestKey(request))
  })

  describe('per-basin degrade (#2694)', () => {
    it('keeps the basins that loaded when another basin fails on its first page', async () => {
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg) => {
        if (identity.basinVersionId === 'bv-hlj') throw new Error('statement timeout')
        return stationPage(stations('qhh', 386, 0), 386)
      })

      const data = await useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [
          { basinId: 'basins_hlj', basinVersionId: 'bv-hlj' },
          { basinId: 'qhh', basinVersionId: 'bv-qhh' },
        ],
      })

      expect(data.failedBasinIds).toEqual(['basins_hlj'])
      expect(data.stations).toHaveLength(386)
      expect(data.stations.every((item) => data.stationBasinIds[item.station_id] === 'qhh')).toBe(true)
      expect(data.loaded).toBe(386)
      expect(data.total).toBe(386)
      expect(data.totalKnown).toBe(false)
      expect(data.truncated).toBe(true)
      const state = useStationLayerDataStore.getState()
      expect(state.data).toBe(data)
      expect(state.error).toBeNull()
      expect(state.loading).toBe(false)
    })

    it('keeps the earlier pages of a basin that fails mid-pagination and loads the others fully', async () => {
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg, query: QueryArg) => {
        if (identity.basinVersionId === 'bv-hlj') {
          if (query.offset > 0) throw new Error('第二页加载失败')
          return stationPage(stations('hlj', STATION_PAGE_LIMIT, 0), 1314)
        }
        return stationPage(stations('qhh', 386, 0), 386)
      })

      const data = await useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [
          { basinId: 'basins_hlj', basinVersionId: 'bv-hlj' },
          { basinId: 'qhh', basinVersionId: 'bv-qhh' },
        ],
      })

      expect(data.failedBasinIds).toEqual(['basins_hlj'])
      expect(data.loaded).toBe(STATION_PAGE_LIMIT + 386)
      expect(data.stationBasinIds['hlj-0']).toBe('basins_hlj')
      expect(data.stationBasinIds[`hlj-${STATION_PAGE_LIMIT - 1}`]).toBe('basins_hlj')
      expect(data.stationBasinIds['qhh-385']).toBe('qhh')
      expect(data.total).toBe(1314 + 386)
      expect(data.totalKnown).toBe(false)
      expect(data.truncated).toBe(true)
      expect(useStationLayerDataStore.getState().error).toBeNull()
    })

    it('falls back to the basin-only identity when the latest-product lookup fails', async () => {
      fetchHydroMetLatestProductMock.mockRejectedValue(new Error('QHH_LATEST_PRODUCT_UNAVAILABLE'))
      fetchHydroMetStationsByIdentityMock.mockResolvedValueOnce(stationPage(stations('qhh', 2), 2))

      const data = await useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [{ basinId: 'basins_qhh', basinVersionId: 'bv-qhh', source: 'IFS', cycle: null }],
      })

      expect(fetchHydroMetLatestProductMock).toHaveBeenCalledTimes(1)
      expect(fetchHydroMetStationsByIdentityMock.mock.calls).toEqual([
        [{ basinVersionId: 'bv-qhh' }, { limit: STATION_PAGE_LIMIT, offset: 0 }],
      ])
      expect(data.failedBasinIds).toEqual([])
      expect(data.loaded).toBe(2)
      expect(data.total).toBe(2)
      expect(data.totalKnown).toBe(true)
      expect(data.truncated).toBe(false)
    })

    it('rejects with the first error when every basin fails', async () => {
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg, query: QueryArg) => {
        if (identity.basinVersionId === 'bv-first') {
          if (query.offset > 0) throw new Error('first failed')
          return stationPage(stations('first', STATION_PAGE_LIMIT, 0), 1314)
        }
        throw new Error('second failed')
      })

      await expect(
        useStationLayerDataStore.getState().loadStationLayer({
          basinContexts: [
            { basinId: 'first', basinVersionId: 'bv-first' },
            { basinId: 'second', basinVersionId: 'bv-second' },
          ],
        }),
      ).rejects.toThrow('first failed')

      // 两个流域都试过才报错，不在第一个失败处中断；并发下请求先后不固定，只核对每个流域各自的请求。
      const requests = fetchHydroMetStationsByIdentityMock.mock.calls.map(
        (call) => `${(call[0] as IdentityArg).basinVersionId}@${(call[1] as QueryArg).offset}`,
      )
      expect([...requests].sort()).toEqual([`bv-first@${STATION_PAGE_LIMIT}`, 'bv-first@0', 'bv-second@0'].sort())
      const state = useStationLayerDataStore.getState()
      expect(state.data).toBeNull()
      expect(state.error).toBe('first failed')
    })

    it('lists a basin once when several of its contexts fail', async () => {
      fetchHydroMetLatestProductMock.mockImplementation(async ({ source }: { source: string }) => ({
        basin_version_id: 'bv-hlj',
        model_id: `m-${source}`,
      }))
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg) => {
        if (identity.basinVersionId === 'bv-hlj') throw new Error('statement timeout')
        return stationPage(stations('qhh', 3, 0), 3)
      })

      const data = await useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [
          { basinId: 'basins_hlj', basinVersionId: 'bv-hlj', source: 'GFS' },
          { basinId: 'qhh', basinVersionId: 'bv-qhh' },
          { basinId: 'basins_hlj', basinVersionId: 'bv-hlj', source: 'IFS' },
        ],
      })

      expect(data.failedBasinIds).toEqual(['basins_hlj'])
      expect(data.loaded).toBe(3)
    })

    it('reports no failed basin on a full success', async () => {
      fetchHydroMetStationsByIdentityMock.mockImplementation(async (identity: IdentityArg) =>
        identity.basinVersionId === 'bv-heihe'
          ? stationPage(stations('heihe', 287, 0), 287)
          : stationPage(stations('qhh', 386, 0), 386),
      )

      const data = await useStationLayerDataStore.getState().loadStationLayer({
        basinContexts: [
          { basinId: 'heihe', basinVersionId: 'bv-heihe' },
          { basinId: 'qhh', basinVersionId: 'bv-qhh' },
        ],
      })

      expect(data.failedBasinIds).toEqual([])
      expect(data.unloadedBasinCount).toBe(0)
      expect(data.truncated).toBe(false)
      expect(data.totalKnown).toBe(true)
      expect(data.total).toBe(673)
    })
  })
})
