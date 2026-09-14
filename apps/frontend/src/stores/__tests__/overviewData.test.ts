import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  pendingActiveCycleValidTimesDisabledReason,
} from '@/lib/m11/overviewDataContracts'
import { resolveM11NationalValidTimeCorrection, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import {
  clearOverviewDataCache,
  overviewSnapshotMatchesQuery,
  useOverviewDataStore,
  type OverviewDataSnapshot,
} from '@/stores/overviewData'
import {
  CYCLES_PATH,
  VALID_TIMES_PATH,
  PRECIP_INDEX_PATH,
  DEFAULT_CYCLE,
  OTHER_CYCLE,
  IFS_CYCLE,
  query,
  success,
  basin,
  nationalDischargeMetadata,
  layer,
  precipIndex,
  mockApi,
  ifsQuery,
  cyclesPayload,
  decodedTilePath,
  resetOverviewDataTestState,
  type MockOptions,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

beforeEach(() => {
  resetOverviewDataTestState()
})

describe('overview data store discharge loading', () => {
  it('loads overview runs without product-specific readiness filters', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadOverview(query)

    expect(snapshot.summary.freshness.runId).toBe('run-001')
    expect(calls.filter((call) => call.path === '/api/v1/basins').map((call) => call.query)).toEqual([
      { limit: 200, offset: 0, has_display_product: true },
    ])
    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    const allowedRunQueryKeys = new Set(['basin_id', 'source', 'cycle_time', 'status', 'limit', 'offset'])
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.status === 'published')).toBe(true)
    expect(runCalls.every((call) => Object.keys(call.query ?? {}).every((key) => allowedRunQueryKeys.has(key)))).toBe(true)
  })

  it('sends the same /api/v1/runs request object per ready status', async () => {
    // #2332：`fetchRuns*` 去掉恒为 undefined 的 `basinId` 参数前后，请求对象必须逐字段不变。
    // `toEqual` 不区分 undefined 键：改动前的 `basin_id: undefined` 本就被 openapi-fetch 的 query 序列化略去。
    const calls = mockApi()

    await useOverviewDataStore.getState().loadOverview(query)

    expect(
      calls.filter((call) => call.path === '/api/v1/runs').map((call) => ({ path: call.path, query: call.query })),
    ).toEqual([
      {
        path: '/api/v1/runs',
        query: { source: 'GFS', cycle_time: '2026-05-18T00:00:00.000Z', status: 'published', limit: 20, offset: 0 },
      },
    ])
  })

  it('consumes the default cycle from catalog metadata without any valid-times request', async () => {
    // spec frontend-mvt-layer-consumption「Metadata carries valid_times for the default cycle」
    const calls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    const discharge = useOverviewDataStore.getState().overview?.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    // 默认位置是 lead 0 = 活动列表首项。
    expect(discharge?.currentValidTime).toBe('2026-05-18T00:00:00.000Z')
  })

  it('treats a millisecond-spelled default cycle in the URL as the default cycle', async () => {
    // fixture 决策 3 的第四处用途：`metadata.default_cycle` 是秒精度而 URL 是毫秒形，
    // 朴素 `===` 会把默认周期误判成非默认并多发一次 valid-times。
    const calls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-18T00:00:00.000Z' })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
  })

  it('fetches a non-default cycle list once and reuses it across loadOverview calls', async () => {
    // spec「Non-default cycle fetches its own list」+ 缓存生命周期：A→B→A 共 2 次而不是 3 次。
    const calls = mockApi()
    const cycleA = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: null }
    const cycleB = { ...query, cycle: '2026-05-17T18:00:00.000Z', validTime: null }
    const validTimesCalls = () => calls.filter((call) => call.path === VALID_TIMES_PATH)

    await useOverviewDataStore.getState().loadOverview(cycleA)

    expect(validTimesCalls()).toHaveLength(1)
    expect(validTimesCalls()[0].query).toEqual({ source: 'gfs', cycle: OTHER_CYCLE })
    expect(validTimesCalls()[0].mapBootstrapLoading).toBe(false)

    const stored = useOverviewDataStore.getState()
    // 缓存键是 `(source, cycle)`，周期按秒精度归一（URL 里是毫秒形 `…T12:00:00.000Z`）。
    expect(Object.keys(stored.validTimesByCycle)).toEqual([`gfs|${OTHER_CYCLE}`])
    expect(Object.values(stored.validTimesByCycle)[0]).toEqual({
      status: 'available',
      validTimes: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
    })
    // LayerState 本身必须是活动 (source, cycle) 的列表，而不是目录里默认周期的列表。
    const discharge = stored.overview?.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    // overlay 用这份 per-cycle 列表解析（决策 4 的端到端证明）。
    const overlay = buildM11RegisteredOverlay(cycleA, stored.overview?.layers ?? [])
    expect(decodeURIComponent(new URL(overlay?.source.tiles[0] as string, 'http://localhost').pathname)).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-17T12:00:00Z/q_down/2026-05-17T12:00:00Z/{z}/{x}/{y}.pbf',
    )

    await useOverviewDataStore.getState().loadOverview(cycleB)
    expect(validTimesCalls()).toHaveLength(2)

    await useOverviewDataStore.getState().loadOverview(cycleA)
    expect(validTimesCalls()).toHaveLength(2)
  })

  it('is fail-closed when the catalog advertises no default cycle', async () => {
    // spec map-layer-timeline-controls「Cycle selector is fail-closed」+ frontend-mvt-layer-consumption
    // 「Discharge with an empty list is fail-closed, not time-less」。
    const failClosedLayer = {
      ...layer,
      metadata: { ...nationalDischargeMetadata, default_cycle: null, valid_times: [] },
    }
    const calls = mockApi({ '/api/v1/layers': () => success([failClosedLayer]) })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-17T12:00:00.000Z' })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(calls.filter((call) => call.path === PRECIP_INDEX_PATH)).toHaveLength(0)
    expect(JSON.stringify(calls)).not.toContain('{cycle}')

    const layers = useOverviewDataStore.getState().overview?.layers ?? []
    const discharge = layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).toContain('No cycle covers every basin')
    expect(buildM11RegisteredOverlay({ ...query, cycle: '2026-05-17T12:00:00.000Z' }, layers)).toBeNull()
  })

  it('issues cycles and precip index only after mapBootstrapLoading settles and never fails bootstrap', async () => {
    // spec overview-data-contracts ADDED「Cycles and precipitation index requests stay off the
    // bootstrap critical path」+「Enrichment failure of cycles or precip index does not block the map」
    const calls = mockApi({
      [CYCLES_PATH]: () => {
        throw new Error('cycles down')
      },
      [PRECIP_INDEX_PATH]: () => {
        throw new Error('precip down')
      },
    })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })

    // 对照组：关键路径的两个请求确实是在 mapBootstrapLoading === true 时发出的，
    // 所以下面 enrichment 的 false 读数是真实的先后顺序，不是「这个标志从没被置 true」。
    expect(calls[0].mapBootstrapLoading).toBe(true)
    expect(calls.filter((call) => call.path === '/api/v1/basins')[0].mapBootstrapLoading).toBe(true)
    expect(calls.filter((call) => call.path === '/api/v1/layers')[0].mapBootstrapLoading).toBe(true)

    const enrichment = calls.filter((call) => call.path === CYCLES_PATH || call.path === PRECIP_INDEX_PATH)
    expect(enrichment).toHaveLength(2)
    expect(enrichment.every((call) => call.mapBootstrapLoading === false)).toBe(true)

    const state = useOverviewDataStore.getState()
    expect(state.bootstrapError).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.overview?.bootstrap).not.toBeNull()
    expect(state.cyclesBySource).toEqual({ gfs: { status: 'error' } })
    expect(state.precipIndexByCycle).toEqual({ [`gfs|${DEFAULT_CYCLE}`]: { status: 'error' } })
    // 地图仍可注册叠加层（默认周期的 metadata 列表照旧可用）。
    expect(buildM11RegisteredOverlay({ ...query, cycle: null }, state.overview?.layers ?? [])).not.toBeNull()
  })

  // 三条 layer-time enrichment 通路各自的 `isCurrentRequest()` 守卫都必须真的挡住迟到写入。
  // 一律用**非默认周期**加载：`cycle: null` 时活动对是默认对，per-cycle valid-times 分支
  // 结构上根本进不去，只能证明其中一条通路。
  const lateEnrichmentPaths = [
    { name: 'cycles', path: CYCLES_PATH, payload: () => success({ source: 'gfs', cycles: [], default_cycle: null }) },
    {
      name: 'per-cycle valid times',
      path: VALID_TIMES_PATH,
      payload: () => success({ layer_id: 'discharge', valid_times: [OTHER_CYCLE, '2026-05-17T18:00:00Z'] }),
    },
    { name: 'precip index', path: PRECIP_INDEX_PATH, payload: () => success(precipIndex) },
  ] as const

  it.each(lateEnrichmentPaths)(
    'discards $name results that arrive after overviewRequestNonce advanced',
    async ({ path, payload }) => {
      let release: () => void = () => undefined
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      const calls = mockApi({
        [path]: async () => {
          await gate
          return payload()
        },
      })

      const load = useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-17T12:00:00.000Z' })
      await vi.waitFor(() => expect(calls.some((call) => call.path === path)).toBe(true))
      // 新一轮请求已开始（nonce 递增）：迟到的结果一律不写入 store。
      clearOverviewDataCache()
      // bump **之后**取参照：bump 之前 enrichment 的最终 set 可能尚未落地，比对会变成竞态。
      const layersAfterBump = useOverviewDataStore.getState().overview?.layers
      release()
      await load

      const state = useOverviewDataStore.getState()
      expect(state.cyclesBySource).toEqual({})
      expect(state.validTimesByCycle).toEqual({})
      expect(state.precipIndexByCycle).toEqual({})
      // valid-times 的写入口还会就地重算 layers：迟到写入若漏守卫，这里的引用会被换掉。
      expect(state.overview?.layers).toBe(layersAfterBump)
    },
  )

  it('keeps the discharge layer disabled while the non-default cycle list is still in flight', async () => {
    // cand-01：列表未到时**不得**回落到目录里默认周期的 metadata.valid_times，
    // 否则图层报 available 并拼出跨周期瓦片 URL。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [VALID_TIMES_PATH]: async () => {
        await gate
        return success({
          layer_id: 'discharge',
          valid_times: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
        })
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const load = useOverviewDataStore.getState().loadOverview(cycleQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === VALID_TIMES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })

    const pendingState = useOverviewDataStore.getState()
    const pendingLayers = pendingState.overview?.layers ?? []
    const pendingDischarge = pendingLayers.find((item) => item.layerId === 'discharge')
    // 「尚未取回」= 记录缺席。
    expect(pendingState.validTimesByCycle).toEqual({})
    expect(pendingDischarge?.available).toBe(false)
    expect(pendingDischarge?.disabledReason).toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(pendingDischarge?.validTimes).toEqual([])
    // 尤其**不是**默认周期那份列表。
    expect(pendingDischarge?.validTimes).not.toContain('2026-05-18T00:00:00.000Z')
    // 零瓦片请求：overlay 注册不出来。
    expect(buildM11RegisteredOverlay(cycleQuery, pendingLayers)).toBeNull()
    // 校正闸门：裸函数会把 URL 里的 validTime 清成 null，全国包装函数在未定期间不校正。
    expect(resolveM11ValidTimeCorrection(cycleQuery, pendingLayers)).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, pendingLayers)).toBeUndefined()

    release()
    await load

    // pending → available 的转移必须真的渲染出来（错误/成功两条终态都就地重算 layers）。
    const settledLayers = useOverviewDataStore.getState().overview?.layers ?? []
    const settledDischarge = settledLayers.find((item) => item.layerId === 'discharge')
    expect(settledDischarge?.available).toBe(true)
    expect(settledDischarge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    expect(buildM11RegisteredOverlay(cycleQuery, settledLayers)).not.toBeNull()
    // 闸门只在未定期间挡；列表落地后包装函数照常委派给裸函数：越界的 validTime 被校正回
    // 该图层的 currentValidTime（此处即活动列表里由 cycleQuery.validTime 命中的那项）。
    expect(
      resolveM11NationalValidTimeCorrection({ ...cycleQuery, validTime: '2026-05-19T00:00:00.000Z' }, settledLayers),
    ).toBe(settledDischarge?.currentValidTime)
    expect(settledDischarge?.currentValidTime).toBe('2026-05-17T15:00:00.000Z')
  })

  it('degrades to a distinct error state when the non-default cycle list rejects', async () => {
    // cand-01 的第二条终态：reject 必须写终态并重算 layers，不能永久停在 pending 文案上；
    // 且这是 scoped 降级，不是 bootstrap 失败。
    // 闸门与上面的成功用例同构：不闸住 reject，阶段 2 自己那次 buildLayerStates 就能满足全部断言，
    // 删掉 `writeValidTimes` 里的就地重算也照样绿（R2-01）。闸门把 reject 推到阶段 2 落定**之后**，
    // 于是「文案是 error」+「layers 引用被换掉」两条都只能由就地重算满足。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [VALID_TIMES_PATH]: async () => {
        await gate
        throw new Error('valid-times down')
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const load = useOverviewDataStore.getState().loadOverview(cycleQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === VALID_TIMES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })
    // 阶段 2 已落定：此刻的 layers 是「reject 之前」的引用，终态必须在它之上原地重算。
    const layersBeforeReject = useOverviewDataStore.getState().overview?.layers
    expect(
      (layersBeforeReject ?? []).find((item) => item.layerId === 'discharge')?.disabledReason,
    ).toBe(pendingActiveCycleValidTimesDisabledReason)

    release()
    await load

    const state = useOverviewDataStore.getState()
    expect(state.validTimesByCycle).toEqual({ [`gfs|${OTHER_CYCLE}`]: { status: 'error' } })
    // pending → error 的转移必须真的渲染出来：只写 record 不重算，这里的引用不会变。
    expect(state.overview?.layers).not.toBe(layersBeforeReject)
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(discharge?.validTimes).toEqual([])
    expect(buildM11RegisteredOverlay(cycleQuery, state.overview?.layers ?? [])).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, state.overview?.layers ?? [])).toBeUndefined()
    // scoped 降级：既不是 bootstrap 失败，也不进 enrichment 的 partial error。
    expect(state.bootstrapError).toBeNull()
    expect(state.error).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
  })

  it('resolves the non-default cycle to the error state when bootstrap failure skips the layer-time chain', async () => {
    // spec frontend-mvt-layer-consumption「The active cycle's list is unresolved」第三种未定情形：
    // bootstrap 失败 → 阶段 3 直接 return，一条 per-cycle valid-times 请求都不会发；而阶段 2 仍用
    // run-scoped 目录（default_cycle 非空）构造 layer 状态，记录缺席会派生出 pending。
    // 「还在加载」是谎报：既无请求在途，也不会再有终态覆盖它 → 必须与 reject 同文案落到终态。
    const calls = mockApi({
      '/api/v1/basins': () => {
        throw new Error('basins down')
      },
    })
    const cycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    await useOverviewDataStore.getState().loadOverview(cycleQuery)

    const state = useOverviewDataStore.getState()
    // (i) 这一轮确实一条 per-cycle 请求都没发，且没有任何 per-cycle 记录被写入。
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(state.validTimesByCycle).toEqual({})
    // bootstrap 确实失败了（否则本用例根本没进那条 skip 分支）。
    expect(state.bootstrapError).not.toBeNull()
    expect(state.overview?.bootstrap).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
    expect(state.enrichmentLoading).toBe(false)
    // (ii) 终态而非 pending：文案与 reject 臂一致（"could not be loaded"），且仍与其余两条禁用文案可分。
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(discharge?.available).toBe(false)
    // 未定态照旧不回落到默认周期的 metadata 列表：零瓦片、URL 的 validTime 不被改写。
    expect(discharge?.validTimes).toEqual([])
    expect(buildM11RegisteredOverlay(cycleQuery, state.overview?.layers ?? [])).toBeNull()
    expect(resolveM11NationalValidTimeCorrection(cycleQuery, state.overview?.layers ?? [])).toBeUndefined()
  })

  it('clears the layer-time skip marker at the start of the next load', async () => {
    // T6：`layerTimeEnrichmentSkipped` 是**整轮**事实（fixture 决策 1 第 3 臂），三个写点里只有
    // 「置真」那个有 oracle。标记若粘住，下一轮**成功**加载的 index 在途窗口会把静默的
    // `index_pending` 升成 `index_error` + 提示 B——对着一条正在飞的请求谎报索引失败。
    mockApi({
      '/api/v1/basins': () => {
        throw new Error('basins down')
      },
    })

    await useOverviewDataStore.getState().loadOverview(query)
    // 前置条件：这一轮确实走了跳过分支（否则下面的复位断言什么也不鉴别）。
    expect(useOverviewDataStore.getState().layerTimeEnrichmentSkipped).toBe(true)

    // 换周期 → requestKey 不同，不会命中 `overviewLoads` 的既有 promise 早返回。
    const next = useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-17T12:00:00.000Z' })
    // **同步**读：复位必须发生在 loadOverview 起始的那次 set 里。留到 enrichment 落定才复位，
    // 整个在途窗口都会带着上一轮的终态标记。
    expect(useOverviewDataStore.getState().layerTimeEnrichmentSkipped).toBe(false)
    await next
  })

  it('clears the layer-time skip marker together with the layer-time caches', () => {
    // 第三个写点：`clearOverviewDataCache()` 与三个 map 同寿。缓存清空后「键缺席」重新只意味着
    // 「还没取」，标记留着就是谎报终态。
    useOverviewDataStore.setState({ layerTimeEnrichmentSkipped: true })
    expect(useOverviewDataStore.getState().layerTimeEnrichmentSkipped).toBe(true)

    clearOverviewDataCache()

    expect(useOverviewDataStore.getState().layerTimeEnrichmentSkipped).toBe(false)
  })
})

// #2127（design D1）：validTime 不是取数身份。时间轴步进/播放（最高 4 Hz）只重派生，不重载。
describe('overview data store validTime-only re-derivation', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /** 订阅 store 的每一次写入：请求计数不是红证（`cached()` 本就去重），状态转移才是。 */
  function recordStoreWrites() {
    const seen: Array<{ mapBootstrapLoading: boolean; enrichmentLoading: boolean; error: string | null; bootstrapError: string | null }> = []
    const unsubscribe = useOverviewDataStore.subscribe((state) => {
      seen.push({
        mapBootstrapLoading: state.mapBootstrapLoading,
        enrichmentLoading: state.enrichmentLoading,
        error: state.error,
        bootstrapError: state.bootstrapError,
      })
    })
    return { seen, unsubscribe }
  }

  const dischargeOf = () => (useOverviewDataStore.getState().overview?.layers ?? []).find((item) => item.layerId === 'discharge')
  const defaultPairQuery = { ...query, cycle: null, validTime: '2026-05-18T00:00:00.000Z' }
  const T2 = '2026-05-18T06:00:00.000Z'
  const T3 = '2026-05-18T03:00:00.000Z'

  it('issues no request and touches no loading, error or layer-time state on a settled timeline step', async () => {
    const calls = mockApi()
    await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
    const before = useOverviewDataStore.getState()
    const settledCallCount = calls.length
    const recorder = recordStoreWrites()

    await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T2 })
    const last = await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T3 })
    recorder.unsubscribe()

    const after = useOverviewDataStore.getState()
    expect(calls).toHaveLength(settledCallCount)
    expect(recorder.seen.some((state) => state.mapBootstrapLoading || state.enrichmentLoading)).toBe(false)
    expect(recorder.seen.every((state) => state.error === before.error && state.bootstrapError === before.bootstrapError)).toBe(true)
    expect(after.cyclesBySource).toBe(before.cyclesBySource)
    expect(after.validTimesByCycle).toBe(before.validTimesByCycle)
    expect(after.precipIndexByCycle).toBe(before.precipIndexByCycle)
    expect(after.layerTimeEnrichmentSkipped).toBe(before.layerTimeEnrichmentSkipped)
    // 消费面跟随最新 validTime：图层时次、bootstrap 快照、请求作用域。
    expect(dischargeOf()?.currentValidTime).toBe(T3)
    expect(after.overview?.bootstrap?.currentLayerValidTime).toBe(T3)
    expect(overviewSnapshotMatchesQuery(after.overview, { ...defaultPairQuery, validTime: T3 })).toBe(true)
    expect(last).toBe(after.overview)
  })

  it('keeps in-flight layer-time enrichment landing and derives it with the latest validTime', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const gated = (payload: (options: MockOptions) => unknown) => async (options: MockOptions) => {
      await gate
      return payload(options)
    }
    const calls = mockApi({
      [CYCLES_PATH]: gated((options) => cyclesPayload(options.params?.query?.source)),
      [VALID_TIMES_PATH]: gated((options) =>
        success({ layer_id: 'discharge', valid_times: [options.params?.query?.cycle, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'] }),
      ),
      [PRECIP_INDEX_PATH]: gated(() => success(precipIndex)),
    })
    const q1 = { ...ifsQuery, validTime: '2026-05-17T15:00:00.000Z' }
    const q2 = { ...ifsQuery, validTime: '2026-05-17T18:00:00.000Z' }

    const first = useOverviewDataStore.getState().loadOverview(q1)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === CYCLES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })
    const recorder = recordStoreWrites()
    const second = useOverviewDataStore.getState().loadOverview(q2)
    release()
    await Promise.all([first, second])
    recorder.unsubscribe()

    const state = useOverviewDataStore.getState()
    expect(recorder.seen.some((entry) => entry.mapBootstrapLoading || entry.enrichmentLoading)).toBe(false)
    expect(state.cyclesBySource.ifs?.status).toBe('available')
    expect(state.validTimesByCycle[`ifs|${IFS_CYCLE}`]?.status).toBe('available')
    expect(state.precipIndexByCycle[`ifs|${IFS_CYCLE}`]?.status).toBe('available')
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(1)
    expect(dischargeOf()?.available).toBe(true)
    expect(dischargeOf()?.currentValidTime).toBe('2026-05-17T18:00:00.000Z')
    expect(overviewSnapshotMatchesQuery(state.overview, q2)).toBe(true)
  })

  it('keeps an enrichment partial error and does not re-send the failing endpoint on a timeline step', async () => {
    const calls = mockApi({
      '/api/v1/pipeline/status': () => {
        throw new Error('pipeline down')
      },
    })
    await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
    const error = useOverviewDataStore.getState().error
    // 前置条件：partial error 确实已写入，且失败条目已被 `cached()` 删除（下一轮会重发）。
    expect(error).not.toBeNull()
    const pipelineCalls = () => calls.filter((call) => call.path === '/api/v1/pipeline/status')
    expect(pipelineCalls()).toHaveLength(1)
    const recorder = recordStoreWrites()

    await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T2 })
    recorder.unsubscribe()

    expect(pipelineCalls()).toHaveLength(1)
    expect(useOverviewDataStore.getState().error).toBe(error)
    expect(recorder.seen.every((entry) => entry.error === error)).toBe(true)
  })

  describe('equivalence with a fresh load (fixed clock)', () => {
    // `isStale` 读 `Date.now()`：钉住时钟，且让 T1 已过期（10h）而 T2 未过期（4h），freshness 对 validTime 敏感。
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date('2026-05-18T10:00:00Z'))
    })

    async function freshOverviewFor(target: typeof defaultPairQuery) {
      clearOverviewDataCache()
      useOverviewDataStore.setState({ overview: null, mapBootstrapLoading: false, enrichmentLoading: false, bootstrapError: null, error: null })
      await useOverviewDataStore.getState().loadOverview(target)
      return useOverviewDataStore.getState().overview
    }

    function expectOverlayOnT2(overview: OverviewDataSnapshot | null) {
      const q2 = { ...defaultPairQuery, validTime: T2 }
      expect(overviewSnapshotMatchesQuery(overview, q2)).toBe(true)
      expect(decodedTilePath(buildM11RegisteredOverlay(q2, overview?.layers ?? []))).toBe(
        `/api/v1/tiles/hydro-national/gfs/${DEFAULT_CYCLE}/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf`,
      )
    }

    it('equals a fresh load of the final query after a settled timeline step', async () => {
      mockApi()
      await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
      const beforeStep = useOverviewDataStore.getState().overview
      await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T2 })
      const rederived = useOverviewDataStore.getState().overview
      // 前置条件：步进确实改了 validTime 敏感字段（否则等价性什么也不鉴别）。
      expect(rederived?.summary.freshness.isStale).not.toBe(beforeStep?.summary.freshness.isStale)

      const fresh = await freshOverviewFor({ ...defaultPairQuery, validTime: T2 })

      expect(rederived).toEqual(fresh)
      expect(rederived?.bootstrap?.currentLayerValidTime).toBe(T2)
      expectOverlayOnT2(rederived)
    })

    it('equals a fresh load when the step lands after enrichment returned while bootstrap is still blocked', async () => {
      let release: () => void = () => undefined
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      const calls = mockApi({
        // 只闸 runless 目录：阶段 2 有已发布 run，走 run-scoped 键，不与阶段 1 共享这条在途 promise。
        '/api/v1/layers': async (options) => {
          if (options.params?.query?.run_id === undefined) await gate
          return success([layer])
        },
      })

      const first = useOverviewDataStore.getState().loadOverview(defaultPairQuery)
      await vi.waitFor(() => expect(calls.some((call) => call.path === '/api/v1/pipeline/status')).toBe(true))
      // 让阶段 2 跑到 `await bootstrapPromise`。
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true)
      const second = useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T2 })
      release()
      const [firstResult] = await Promise.all([first, second])
      const rederived = useOverviewDataStore.getState().overview
      expect(firstResult).toBe(rederived)

      const fresh = await freshOverviewFor({ ...defaultPairQuery, validTime: T2 })

      expect(rederived).toEqual(fresh)
      expectOverlayOnT2(rederived)
    })
  })

  const identityChanges = [
    { label: 'cycle', patch: { cycle: '2026-05-17T12:00:00.000Z' }, expectedPath: VALID_TIMES_PATH },
    { label: 'source', patch: { source: 'ifs' as const }, expectedPath: CYCLES_PATH },
  ]

  it.each(identityChanges)('starts a new request generation when the $label changes', async ({ patch, expectedPath }) => {
    const calls = mockApi({ [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source) })
    await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
    const settledCallCount = calls.length

    const next = useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, ...patch })
    // 同步读：新一轮的起始 set 已发生。
    expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true)
    expect(useOverviewDataStore.getState().enrichmentLoading).toBe(true)
    await next

    const newCalls = calls.slice(settledCallCount)
    expect(newCalls.some((call) => call.path === expectedPath)).toBe(true)
  })

  it('reloads an identical query after settle and retries a failed bootstrap', async () => {
    let basinsCalls = 0
    mockApi({
      '/api/v1/basins': () => {
        basinsCalls += 1
        if (basinsCalls === 1) throw new Error('basins down')
        return success([basin])
      },
    })
    await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
    expect(useOverviewDataStore.getState().bootstrapError).not.toBeNull()

    // 重挂载（例如从 /ops 返回）：同一 query 必须开新一轮，而不是被当作步进短路。
    const next = useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery })
    expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true)
    await next

    expect(basinsCalls).toBeGreaterThan(1)
    expect(useOverviewDataStore.getState().bootstrapError).toBeNull()
    expect(useOverviewDataStore.getState().overview?.bootstrap).not.toBeNull()
  })

  it('re-derives a later timeline step from the generation an identical-query reload started', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let pipelineRound = 0
    mockApi({
      '/api/v1/pipeline/status': () => {
        pipelineRound += 1
        return success(
          pipelineRound === 1
            ? { cycle_time: DEFAULT_CYCLE, updated_at: '2026-05-18T00:30:00Z', job_counts: { succeeded: 1, running: 0, failed: 0, pending: 0 } }
            : { cycle_time: DEFAULT_CYCLE, updated_at: '2026-05-19T00:00:00Z', job_counts: { succeeded: 5, running: 0, failed: 0, pending: 0 } },
        )
      },
    })
    await useOverviewDataStore.getState().loadOverview(defaultPairQuery)
    expect(useOverviewDataStore.getState().overview?.summary.completedCyclesToday).toBe(1)

    // 推过 store 的缓存 TTL，让同一 query 的重载真的重取 pipeline。
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery })
    expect(pipelineRound).toBe(2)

    await useOverviewDataStore.getState().loadOverview({ ...defaultPairQuery, validTime: T2 })

    const summary = useOverviewDataStore.getState().overview?.summary
    expect(summary?.completedCyclesToday).toBe(5)
    expect(summary?.latestUpdate).toBe('2026-05-19T00:00:00.000Z')
    expect(summary?.sourceSelection.validTime).toBe(T2)
    expect(pipelineRound).toBe(2)
  })
})
