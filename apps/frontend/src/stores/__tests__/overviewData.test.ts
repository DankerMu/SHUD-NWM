import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  pendingActiveCycleValidTimesDisabledReason,
} from '@/lib/m11/overviewDataContracts'
import { resolveM11NationalValidTimeCorrection, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import {
  CYCLES_PATH,
  VALID_TIMES_PATH,
  PRECIP_INDEX_PATH,
  DEFAULT_CYCLE,
  OTHER_CYCLE,
  query,
  success,
  nationalDischargeMetadata,
  layer,
  precipIndex,
  mockApi,
  resetOverviewDataTestState,
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
})
