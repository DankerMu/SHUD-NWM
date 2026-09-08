import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  pendingActiveCycleValidTimesDisabledReason,
} from '@/lib/m11/overviewDataContracts'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import {
  CYCLES_PATH,
  VALID_TIMES_PATH,
  OTHER_CYCLE,
  IFS_CYCLE,
  query,
  success,
  run,
  mockApi,
  cyclesPayload,
  decodedTilePath,
  resetOverviewDataTestState,
  type MockCall,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

beforeEach(() => {
  resetOverviewDataTestState()
})

describe('overview data store discharge loading', () => {
  it('loads basin detail with river geometry and q_down forecast only', async () => {
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', query)

    expect(snapshot.segments[0]).toMatchObject({ currentQ: 12, qUnit: 'm3/s' })
    expect(snapshot.selectedSegment?.currentQ).toBe(12)
    expect(calls.filter((call) => call.path === '/api/v1/runs').every((call) => call.query?.status === 'published')).toBe(true)
  })

  // ── 流域详情的全国 discharge 叠加层：只有目录默认对才渲染 ───────────────────────────────
  // spec frontend-mvt-layer-consumption「Basin detail renders the national discharge overlay only
  // for the catalog default pair」。后端对 run-scoped `/api/v1/layers?run_id=` 同样合并全国
  // discharge 元数据，所以流域详情的 discharge 也是 `{source}/{cycle}` 模板；非默认对若回落到
  // 目录默认对的 `metadata.valid_times`，就会拼出良构但错身份的瓦片 URL。
  // 流域详情**不发** per-cycle valid-times（决策 9：时间轴来自选中的 run）；下面的
  // `perCycleValidTimesCalls` 只统计带 `source`/`cycle` 的那种请求——run-scoped 的
  // `/api/v1/layers/discharge/valid-times?run_id=` 是既有且合法的另一条通路。
  const perCycleValidTimesCalls = (calls: MockCall[]) =>
    calls.filter(
      (call) => call.path === VALID_TIMES_PATH && (call.query?.source !== undefined || call.query?.cycle !== undefined),
    )

  it('fails closed in basin detail when the URL source is not the catalog default source', async () => {
    const calls = mockApi()
    const ifsQuery = { ...query, source: 'ifs' as const }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', ifsQuery)

    // 缓存里没有 (ifs, DEFAULT_CYCLE) 的列表，而流域详情永远不会去取它 → 终态 error，不是 pending。
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
    expect(perCycleValidTimesCalls(calls)).toHaveLength(0)
    // 非空对照：run-scoped 的那条 valid-times 确实发了（决策 9 的合法通路），
    // 所以上面的 0 不是「过滤器把所有请求都排除掉了」的空断言。
    const runScopedValidTimesCalls = calls.filter((call) => call.path === VALID_TIMES_PATH)
    expect(runScopedValidTimesCalls).not.toHaveLength(0)
    expect(runScopedValidTimesCalls.every((call) => call.query?.run_id === 'run-001')).toBe(true)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.validTimes).toEqual([])
    // 尤其**不是**默认对那份列表。
    expect(discharge?.validTimes).not.toContain('2026-05-18T06:00:00.000Z')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    // 零瓦片请求：跨身份的 `/hydro-national/ifs/<gfs 的默认周期>/…` 拼不出来。
    expect(buildM11RegisteredOverlay(ifsQuery, snapshot.layers)).toBeNull()
  })

  it('resolves basin detail on the chosen source own cycle once the overview filled the shared cache', async () => {
    // 决策 13 让活动对按源分叉后，流域详情这一侧新增了一个可达态：总览在 IFS 上取回过
    // (ifs, C_ifs) 的列表，用户再进流域详情 —— 两侧调用的是**同一个** `nationalDischargeActivePair`，
    // 故这里解出的对与总览一致，直接复用共享缓存，而不是落回「解不出对 → 终态 error」。
    // （`cyclesBySource[ifs]` 缺席那一态仍是上一条用例守的终态 error。）
    const calls = mockApi({ [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source) })
    const basinIfsQuery = { ...query, source: 'ifs' as const, cycle: null, validTime: '2026-05-17T15:00:00.000Z' }

    await useOverviewDataStore.getState().loadOverview({ ...basinIfsQuery, validTime: null })
    const sharedCycles = useOverviewDataStore.getState().cyclesBySource
    const sharedValidTimes = useOverviewDataStore.getState().validTimesByCycle
    expect(Object.keys(sharedValidTimes)).toEqual([`ifs|${IFS_CYCLE}`])
    // 与上面同一套手法：先清模块级 HTTP 缓存，再回填两份 store 记录（它们与 `cached()` 同寿，
    // 但 HTTP 缓存有 TTL 而 store 状态没有，「缓存过期而共享记录仍在」是真实可达态）。
    clearOverviewDataCache()
    useOverviewDataStore.setState({ cyclesBySource: sharedCycles, validTimesByCycle: sharedValidTimes })
    const callsBeforeBasinLoad = calls.length

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', basinIfsQuery)

    expect(perCycleValidTimesCalls(calls.slice(callsBeforeBasinLoad))).toHaveLength(0)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T06:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    // 尤其不是目录默认对（GFS）那份列表。
    expect(discharge?.validTimes).not.toContain('2026-05-18T00:00:00.000Z')
  })

  it('fails closed in basin detail when the URL cycle is not the catalog default cycle', async () => {
    const calls = mockApi()
    const otherCycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', otherCycleQuery)

    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
    expect(perCycleValidTimesCalls(calls)).toHaveLength(0)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(false)
    expect(discharge?.validTimes).toEqual([])
    expect(discharge?.validTimes).not.toContain('2026-05-18T06:00:00.000Z')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.disabledReason).not.toBe('Layer has no valid times.')
    expect(discharge?.disabledReason).not.toBe(failClosedDischargeDisabledReason)
    expect(buildM11RegisteredOverlay(otherCycleQuery, snapshot.layers)).toBeNull()
  })

  it('still renders the basin-detail national overlay for the catalog default pair', async () => {
    // 不回归工作用例：`query.cycle` 是毫秒拼写的默认周期，同时钉住秒精度的 isDefault 比较。
    mockApi()

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', query)

    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
    expect(discharge?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    expect(decodedTilePath(buildM11RegisteredOverlay(query, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf',
    )
  })

  it('validates the same identity the tile builder substitutes when the URL says best', async () => {
    // 陷阱：`concreteSurfaceQuery` 会把 `best` 按选中的 run 解析成具体源（这里 run 是 IFS），
    // 而 `buildM11RegisteredOverlay` 代入的是 `resolveNationalScaleSource('best') === 'gfs'`。
    // 活动对若按 `concreteSurfaceQuery` 解析就成了 (ifs, …) → 非默认 → 图层被判不可用，
    // 而 URL 上的 `best` 在全国口径下本就是目录默认对。
    mockApi({
      '/api/v1/runs': (options) =>
        success({
          items: [{ ...run, source_id: 'IFS', scenario_id: 'forecast_ifs_deterministic' }],
          total: 1,
          limit: 20,
          offset: options.params?.query?.offset ?? 0,
        }),
    })
    const bestQuery = { ...query, source: 'best' as const }

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', bestQuery)

    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    // 校验身份 = 代入身份：URL 的 `best` 在全国口径归一为 gfs，正是目录默认源。
    expect(decodedTilePath(buildM11RegisteredOverlay(bestQuery, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-18T00:00:00Z/q_down/2026-05-18T06:00:00Z/{z}/{x}/{y}.pbf',
    )
  })

  it('reuses the shared per-cycle cache the national overview already filled', async () => {
    // 共享缓存是特性不是特例：总览取过 (gfs, OTHER_CYCLE) 后，流域详情直接复用，
    // 自己一条 per-cycle 请求都不发。
    const calls = mockApi()
    const otherCycleQuery = { ...query, cycle: '2026-05-17T12:00:00.000Z', validTime: '2026-05-17T15:00:00.000Z' }

    await useOverviewDataStore.getState().loadOverview(otherCycleQuery)
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({
      [`gfs|${OTHER_CYCLE}`]: {
        status: 'available',
        validTimes: [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z'],
      },
    })
    // 先清掉模块级 HTTP 缓存再进流域详情：否则「没发 per-cycle 请求」是缓存造成的真空断言 ——
    // 上面的 loadOverview 已把 (gfs, OTHER_CYCLE) 的响应落进 `cached()`，泄漏的请求根本到不了
    // `client.GET`。`clearOverviewDataCache()` 连 store 的 `validTimesByCycle` 一并清（它俩同寿），
    // 而本用例的前提正是那份共享列表还在，故快照后回填。HTTP 缓存有 TTL、store 状态没有，
    // 「缓存已过期而共享列表仍在」是真实可达状态，不是为断言捏造的。
    const sharedValidTimes = useOverviewDataStore.getState().validTimesByCycle
    clearOverviewDataCache()
    useOverviewDataStore.setState({ validTimesByCycle: sharedValidTimes })
    const callsBeforeBasinLoad = calls.length

    const snapshot = await useOverviewDataStore.getState().loadBasinDetail('basin-demo', otherCycleQuery)

    // 流域详情自身这一段没有再发 per-cycle valid-times。
    expect(perCycleValidTimesCalls(calls.slice(callsBeforeBasinLoad))).toHaveLength(0)
    // 非空对照：清缓存后这一段的请求确实到得了 client.GET（run-scoped valid-times 就在里面），
    // 所以上面的 0 是「真没发」而不是「发了但被缓存吃掉」。
    const runScopedValidTimesCalls = calls
      .slice(callsBeforeBasinLoad)
      .filter((call) => call.path === VALID_TIMES_PATH && call.query?.run_id === 'run-001')
    expect(runScopedValidTimesCalls).not.toHaveLength(0)
    const discharge = snapshot.layers.find((item) => item.layerId === 'discharge')
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T12:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    expect(decodedTilePath(buildM11RegisteredOverlay(otherCycleQuery, snapshot.layers))).toBe(
      '/api/v1/tiles/hydro-national/gfs/2026-05-17T12:00:00Z/q_down/2026-05-17T15:00:00Z/{z}/{x}/{y}.pbf',
    )
  })
})
