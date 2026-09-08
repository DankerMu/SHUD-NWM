import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  mergeLayerStates,
  pendingActiveCycleValidTimesDisabledReason,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState } from '@/lib/m11/queryState'
import { deriveM11ControlBarModel } from '@/pages/m11/M11BottomControlBar'
import { resolveM11NationalValidTimeCorrection } from '@/pages/m11/M11Controls'
import { clearOverviewDataCache, useOverviewDataStore } from '@/stores/overviewData'
import {
  CYCLES_PATH,
  VALID_TIMES_PATH,
  PRECIP_INDEX_PATH,
  DEFAULT_CYCLE,
  OTHER_CYCLE,
  IFS_CYCLE,
  query,
  success,
  nationalDischargeMetadata,
  layer,
  apiError,
  mockApi,
  ifsQuery,
  cyclesPayload,
  decodedTilePath,
  resetOverviewDataTestState,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

beforeEach(() => {
  resetOverviewDataTestState()
})

describe('overview data store discharge loading', () => {
  it('never spells another source default cycle into a valid-times or precip request', async () => {
    // AC7(a)（C1 的红证）：`cyclesBySource.ifs` 尚未到达时，`(ifs, <gfs 的 default_cycle>)`
    // 是一个未覆盖对——后端对它返回 **200 + 空列表**（不是 4xx），图层随即落到一条假文案。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [CYCLES_PATH]: async (options) => {
        await gate
        return cyclesPayload(options.params?.query?.source)
      },
    })

    const load = useOverviewDataStore.getState().loadOverview(ifsQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === CYCLES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })

    // 一条都不发：活动对解不出来（复用既有 fail-closed 语义），不是「用别人的周期先试试」。
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(calls.filter((call) => call.path === PRECIP_INDEX_PATH)).toHaveLength(0)
    // GFS 的默认周期不得出现在这三条 layer-time 请求的任何一条上（`/pipeline/status` 的
    // `cycle_time` 是另一条既有通路，不在本断言范围内）。
    expect(
      JSON.stringify(calls.filter((call) => [CYCLES_PATH, VALID_TIMES_PATH, PRECIP_INDEX_PATH].includes(call.path))),
    ).not.toContain(DEFAULT_CYCLE)
    const discharge = (useOverviewDataStore.getState().overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    // 尤其**不得**把 GFS 的 `metadata.valid_times` 当 IFS 的时次渲染（把假文案换成假数据更糟）。
    expect(discharge?.validTimes).toEqual([])
    expect(discharge?.validTimes).not.toContain('2026-05-18T00:00:00.000Z')
    expect(discharge?.available).toBe(false)
    expect(discharge?.disabledReason).toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(buildM11RegisteredOverlay(ifsQuery, useOverviewDataStore.getState().overview?.layers ?? [])).toBeNull()

    release()
    await load
  })

  it('recomputes the layers and fetches valid times for the cycle the chosen source declares', async () => {
    // AC7(b)：`cyclesBySource.ifs` 到达（`cycles.default_cycle = C_ifs ≠ C_gfs`）→ 活动对变为
    // (ifs, C_ifs)，valid-times / precip 按该对发出。
    const calls = mockApi({ [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source) })

    await useOverviewDataStore.getState().loadOverview(ifsQuery)

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource.ifs).toEqual({
      status: 'available',
      cycles: {
        source: 'ifs',
        cycles: [{ cycle_time: IFS_CYCLE, valid_time_start: IFS_CYCLE, valid_time_end: '2026-05-17T18:00:00Z' }],
        default_cycle: IFS_CYCLE,
      },
    })
    const validTimesCalls = calls.filter((call) => call.path === VALID_TIMES_PATH)
    expect(validTimesCalls).toHaveLength(1)
    expect(validTimesCalls[0].query).toEqual({ source: 'ifs', cycle: IFS_CYCLE })
    expect(Object.keys(state.validTimesByCycle)).toEqual([`ifs|${IFS_CYCLE}`])
    expect(calls.find((call) => call.path === PRECIP_INDEX_PATH)?.pathParams).toEqual({ source: 'ifs', cycle: IFS_CYCLE })
    // 图层重算到该对自己的列表上（mock 的 valid-times 回 `[cycle, 15:00, 18:00]`）。
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-17T06:00:00.000Z',
      '2026-05-17T15:00:00.000Z',
      '2026-05-17T18:00:00.000Z',
    ])
    expect(discharge?.available).toBe(true)
    expect(discharge?.disabledReason).toBeNull()
  })

  it('substitutes the cycle the chosen source declares into the national tile URL', async () => {
    // AC7(b) 的孪生要求（决策 13 末段）：overlay 的 `(source, cycle, valid_time)` 必须与 store
    // 解析出的活动对**同源**，判据就是这条瓦片 URL。地图侧若仍按目录的 `default_cycle`（GFS 专有
    // 事实）解析周期，同一入参会拼出 `/hydro-national/ifs/<C_gfs>/q_down/<ifs 的有效时刻>/…` ——
    // 一个从未存在过的三元组，比「没有图层」更糟（会真的去取瓦片）。
    mockApi({ [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source) })

    await useOverviewDataStore.getState().loadOverview(ifsQuery)

    const layers = useOverviewDataStore.getState().overview?.layers ?? []
    // 有效时刻取列表里**不等于**周期的那一项：否则 cycle 段与 valid_time 段同字符串，
    // 断言分不出是哪一段错了。
    const overlayQuery = { ...ifsQuery, validTime: '2026-05-17T15:00:00.000Z' }
    const path = decodedTilePath(buildM11RegisteredOverlay(overlayQuery, layers))

    expect(path).toBe(
      `/api/v1/tiles/hydro-national/ifs/${IFS_CYCLE}/q_down/2026-05-17T15:00:00Z/{z}/{x}/{y}.pbf`,
    )
    // 冗余但独立的一条：GFS 的默认周期不得出现在 URL 的任何位置。
    expect(path).not.toContain(DEFAULT_CYCLE)
  })

  it('resolves a non-default source to a terminal error state when its cycle list rejects', async () => {
    // AC7(b) 的第二条终态，同时是 `writeCycles` 重算边的红证：cycles 取回失败后活动对永远解不出
    // 来，没有任何 valid-times 终态会再来覆盖 layers —— 不在 `writeCycles` 里就地重算，UI 就
    // 永久停在「还在加载」这条谎报上（与同文件 `writeValidTimes` 的 reject 臂同构）。
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = mockApi({
      [CYCLES_PATH]: async () => {
        await gate
        throw new Error('cycles down')
      },
    })

    const load = useOverviewDataStore.getState().loadOverview(ifsQuery)
    await vi.waitFor(() => {
      expect(calls.some((call) => call.path === CYCLES_PATH)).toBe(true)
      expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false)
    })
    // 阶段 2 已落定：此刻的 layers 是「reject 之前」的引用，终态必须在它之上原地重算。
    const layersBeforeReject = useOverviewDataStore.getState().overview?.layers
    expect((layersBeforeReject ?? []).find((item) => item.layerId === 'discharge')?.disabledReason).toBe(
      pendingActiveCycleValidTimesDisabledReason,
    )

    release()
    await load

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource).toEqual({ ifs: { status: 'error' } })
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    // pending → error 的转移必须真的渲染出来：只写 record 不重算，这里的引用不会变。
    expect(state.overview?.layers).not.toBe(layersBeforeReject)
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.disabledReason).toBe(activeCycleValidTimesErrorDisabledReason)
    expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge?.validTimes).toEqual([])
    expect(discharge?.available).toBe(false)
    // scoped 降级：不是 bootstrap 失败。
    expect(state.bootstrapError).toBeNull()
    expect(state.mapBootstrapLoading).toBe(false)
  })

  // AC7(e)（round-3 决策 13 的第三臂 / finding A1）：cycles **已到达且为空** 是后端记在
  // 路由 docstring 里的正常 fail-closed 输出（`apps/api/routes/hydro_display.py`：空交集
  // ⇒ `cycles: []` + `default_cycle: null` ⇒ 图层渲染为 disabled），不是「还在取」，也不是
  // 「取失败了」。深链变体一并覆盖：URL 上有 `?cycle=X` 时结论不变——该源没有任何可全国渲染
  // 的周期，拒发请求在数据上是对的。
  const arrivedEmptyCases: Array<{ label: string; urlCycle: string | null }> = [
    { label: 'without a cycle in the URL', urlCycle: null },
    { label: 'with a shared ?cycle= deep link', urlCycle: '2026-05-18T00:00:00.000Z' },
  ]

  it.each(arrivedEmptyCases)(
    'resolves a non-default source to the fail-closed terminal state when its cycle list arrives empty ($label)',
    async ({ urlCycle }) => {
      const calls = mockApi({
        [CYCLES_PATH]: (options) =>
          options.params?.query?.source === 'ifs'
            ? success({ source: 'ifs', cycles: [], default_cycle: null })
            : cyclesPayload(options.params?.query?.source),
      })

      await useOverviewDataStore.getState().loadOverview({ ...ifsQuery, cycle: urlCycle })

      const state = useOverviewDataStore.getState()
      // 入参钉死：记录**在场**且是 `available`，只是列表为空——三态里的第三态，不是记录缺席。
      expect(state.cyclesBySource.ifs).toEqual({
        status: 'available',
        cycles: { source: 'ifs', cycles: [], default_cycle: null },
      })
      expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
      expect(calls.filter((call) => call.path === PRECIP_INDEX_PATH)).toHaveLength(0)

      const layers = state.overview?.layers ?? []
      const discharge = layers.find((item) => item.layerId === 'discharge')
      expect(discharge?.disabledReason).toBe(failClosedDischargeDisabledReason)
      // 三条终态/未定态文案互不相等，这两条是本用例的判别力所在：今天它恒落在 pending 上，
      // 且此后**没有任何写入方**能覆盖它（valid-times / precip 都没发出）——永久谎报「还在取」。
      expect(discharge?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
      expect(discharge?.disabledReason).not.toBe(activeCycleValidTimesErrorDisabledReason)
      expect(discharge?.available).toBe(false)
      expect(discharge?.validTimes).toEqual([])
      expect(discharge?.activeNationalCycle).toBeNull()
      expect(buildM11RegisteredOverlay(ifsQuery, layers)).toBeNull()
      // 终态**不是**未定态：`isM11ActiveCycleValidTimesUnresolved` 刻意不认这一态，故 validTime
      // 校正照常进行并清掉过期时次（与目录判定的 fail-closed 逐字一致——两者产出同一条
      // `disabledReason`）。pending 臂在同一入参下返回 `undefined`（暂缓校正），这条断言正是
      // 「终态 vs 在途」在 URL 行为上的分水岭。
      expect(resolveM11NationalValidTimeCorrection({ ...ifsQuery, validTime: '2026-05-18T06:00:00.000Z' }, layers)).toBeNull()

      // store→条的接缝：把 store 真正产出的 `LayerState` 喂给控制条派生函数（而不是手搓一个
      // `{status:'fail-closed'}` 覆盖），fail-closed 闸口才算真的被这条数据流打开。
      // `cycles: []` + `cycle: null` ⇒ `cycleOptions` 为空 ⇒ `<select>` 禁用（DOM 那半在
      // `M11BottomControlBar.test.tsx` 的 AC8 已到达且为空臂上）。深链变体尤其要看这条：
      // 今天理由是 pending，闸口不开，`?cycle=X` 会被 `cycleOptions` 前置成 IFS 的选中项。
      const model = deriveM11ControlBarModel({
        state: { ...ifsQuery, cycle: urlCycle },
        layers,
        metadata: nationalDischargeMetadata as never,
        cyclesBySource: state.cyclesBySource,
        sourceSelection: null,
      })
      expect(model.cycle).toBeNull()
      expect(model.cycles).toEqual([])
    },
  )

  it('refreshes the active pair when the URL source changes, without resetting the store', async () => {
    // AC7(f)（finding B3）：本单 spec delta 认领的 scenario 其 WHEN 是「切源」，而既有 AC7 全是
    // 冷加载（`beforeEach` 清空后直接 `loadOverview(ifsQuery)`）。这里**不重置**任何 store / cache，
    // 走真正的 gfs → ifs 转移。
    const calls = mockApi({ [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source) })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })
    await useOverviewDataStore.getState().loadOverview(ifsQuery)

    const state = useOverviewDataStore.getState()
    // cache key 含 source：两次都真的发出去了，第二次没有命中第一次的缓存。
    expect(calls.filter((call) => call.path === CYCLES_PATH).map((call) => call.query?.source)).toEqual(['gfs', 'ifs'])
    // `cyclesBySource` 跨轮累积（控制条在目录未落地的切源窗口里靠这份存活数据渲染）。
    expect(Object.keys(state.cyclesBySource).sort()).toEqual(['gfs', 'ifs'])
    const validTimesCalls = calls.filter((call) => call.path === VALID_TIMES_PATH)
    // 默认源那轮走目录 metadata（零请求），切源后按 ifs 自己的默认周期发出恰好一次。
    expect(validTimesCalls.map((call) => call.query)).toEqual([{ source: 'ifs', cycle: IFS_CYCLE }])
    const discharge = (state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.activeNationalCycle).toBe(IFS_CYCLE)
    expect(discharge?.available).toBe(true)
  })

  it('carries the recomputed discharge layer across the display boundary under a fail-closed catalog', async () => {
    // AC7(g)（finding A2 / 决策 14）：**联合 oracle**。断言落在 `OverviewPage` 真正渲染的那个
    // 数组上——`mergeLayerStates(bootstrap.layerStates, overview.layers)`——而不是只落在
    // `store.overview.layers` 上。只测 store 正是 A2 能在 AC7(b) 全绿之下溜过去的原因：
    // fail-closed 目录的 `valid_times: []` 同时满足 `isTimeLessLayerMetadata`，展示边界据此把
    // bootstrap 那层永久钉死，store 里算出的健康 IFS 层既到不了 DOM 也到不了瓦片。
    const failClosedCatalog = {
      ...layer,
      metadata: { ...nationalDischargeMetadata, valid_times: [], default_cycle: null },
    }
    mockApi({
      '/api/v1/layers': () => success([failClosedCatalog]),
      [CYCLES_PATH]: (options) => cyclesPayload(options.params?.query?.source),
    })

    await useOverviewDataStore.getState().loadOverview(ifsQuery)

    const state = useOverviewDataStore.getState()
    const bootstrapLayers = state.overview?.bootstrap?.layerStates ?? []
    // 入参钉死：bootstrap 那层确实是 fail-closed 目录（否则本用例什么也不鉴别）。
    expect(bootstrapLayers.find((item) => item.layerId === 'discharge')?.disabledReason).toBe(
      failClosedDischargeDisabledReason,
    )
    // store 侧已经算对了（AC7(b) 的那半），本用例要证明的是它能不能活着穿过展示边界。
    expect((state.overview?.layers ?? []).find((item) => item.layerId === 'discharge')?.available).toBe(true)

    const merged = mergeLayerStates(bootstrapLayers, state.overview?.layers ?? [])
    const mergedDischarge = merged.find((item) => item.layerId === 'discharge')
    expect(mergedDischarge?.available).toBe(true)
    expect(mergedDischarge?.activeNationalCycle).toBe(IFS_CYCLE)
    expect(mergedDischarge?.disabledReason).toBeNull()
    // 到得了 DOM 还不够，得到得了瓦片：合并后的数组正是交给地图的那一份。
    const overlayQuery = { ...ifsQuery, validTime: '2026-05-17T15:00:00.000Z' }
    const overlay = buildM11RegisteredOverlay(overlayQuery, merged)
    expect(overlay).not.toBeNull()
    expect(overlay ? decodeURIComponent(new URL(overlay.source.tiles[0], 'http://localhost').pathname) : null).toBe(
      `/api/v1/tiles/hydro-national/ifs/${IFS_CYCLE}/q_down/2026-05-17T15:00:00Z/{z}/{x}/{y}.pbf`,
    )
  })

  it('keeps the default source on the catalog default cycle even when the cycles endpoint declares another one', async () => {
    // AC7(c)：默认源路径逐字不变 —— 目录 metadata 仍是唯一来源，同一次加载**零**次 valid-times
    // 请求。实现若对所有源都改读 `cyclesBySource`，这里会多发一次请求并换掉时次列表（变红）。
    const calls = mockApi({
      [CYCLES_PATH]: () =>
        success({
          source: 'gfs',
          cycles: [{ cycle_time: OTHER_CYCLE, valid_time_start: OTHER_CYCLE, valid_time_end: '2026-05-17T18:00:00Z' }],
          default_cycle: OTHER_CYCLE,
        }),
    })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
    expect(useOverviewDataStore.getState().validTimesByCycle).toEqual({})
    expect(calls.find((call) => call.path === PRECIP_INDEX_PATH)?.pathParams).toEqual({
      source: 'gfs',
      cycle: DEFAULT_CYCLE,
    })
    const discharge = (useOverviewDataStore.getState().overview?.layers ?? []).find((item) => item.layerId === 'discharge')
    expect(discharge?.validTimes).toEqual([
      '2026-05-18T00:00:00.000Z',
      '2026-05-18T03:00:00.000Z',
      '2026-05-18T06:00:00.000Z',
    ])
    expect(discharge?.currentValidTime).toBe('2026-05-18T00:00:00.000Z')
  })

  it('clears the three layer-time records together with the HTTP cache', async () => {
    // tasks.md：三个缓存与既有 `cache` 同寿，由 `clearOverviewDataCache()` / `clearCache()` 清除。
    useOverviewDataStore.setState({
      cyclesBySource: { gfs: { status: 'error' } },
      validTimesByCycle: { [`gfs|${OTHER_CYCLE}`]: { status: 'available', validTimes: [OTHER_CYCLE] } },
      precipIndexByCycle: { [`gfs|${DEFAULT_CYCLE}`]: { status: 'error' } },
    })

    useOverviewDataStore.getState().clearCache()

    const state = useOverviewDataStore.getState()
    expect(state.cyclesBySource).toEqual({})
    expect(state.validTimesByCycle).toEqual({})
    expect(state.precipIndexByCycle).toEqual({})
  })

  it('keeps a not-mirrored cycle distinguishable from any other precip index failure', async () => {
    // fixture 决策 7：`PRECIP_CYCLE_NOT_MIRRORED` 与其余失败是两个可区分状态（I11 据此出两条文案）。
    const key = `gfs|${DEFAULT_CYCLE}`
    mockApi({ [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED') })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    expect(useOverviewDataStore.getState().precipIndexByCycle).toEqual({ [key]: { status: 'not_mirrored' } })

    clearOverviewDataCache()
    mockApi({ [PRECIP_INDEX_PATH]: () => apiError('PRECIP_WINDOW_INCOMPLETE') })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    expect(useOverviewDataStore.getState().precipIndexByCycle).toEqual({ [key]: { status: 'error' } })
  })

  it('never spells best or compare into a cycles, valid-times or precip request', async () => {
    // fixture 决策 8：只用解析出的具体 gfs/ifs 拼 URL，解析不出就一条都不发。
    const bestCalls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, source: 'best', cycle: null })

    expect(bestCalls.find((call) => call.path === CYCLES_PATH)?.query).toEqual({ source: 'gfs' })
    expect(bestCalls.find((call) => call.path === PRECIP_INDEX_PATH)?.pathParams).toEqual({
      source: 'gfs',
      cycle: DEFAULT_CYCLE,
    })

    clearOverviewDataCache()
    useOverviewDataStore.setState({ cyclesBySource: {}, validTimesByCycle: {}, precipIndexByCycle: {} })
    const compareCalls = mockApi()

    await useOverviewDataStore.getState().loadOverview({ ...query, source: 'compare', cycle: null })

    expect(
      compareCalls.filter((call) => [CYCLES_PATH, VALID_TIMES_PATH, PRECIP_INDEX_PATH].includes(call.path)),
    ).toHaveLength(0)
    expect(useOverviewDataStore.getState().cyclesBySource).toEqual({})
  })

  it('keeps the untouched default query on GFS and still asks for pipeline status', async () => {
    // AC4 附加 (a)：默认 source 由 best 翻成 gfs 后，`pipelineRequestParams` 的 cycle 回退必须
    // 推广到所有非 compare 源，否则默认全国总览不再发 /api/v1/pipeline/status（摘要卡片空掉）。
    const calls = mockApi()

    const snapshot = await useOverviewDataStore.getState().loadOverview(defaultM11QueryState)

    // 同一条回退：默认态下 sourceSelection 仍带具体周期（时间轴的分析/预报分界线读它）。
    expect(snapshot.summary.sourceSelection.cycleTime).toBe('2026-05-18T00:00:00Z')
    expect(snapshot.summary.sourceSelection.provenanceLabel).toContain('cycle 2026-05-18T00:00:00Z')

    const pipelineCalls = calls.filter((call) => call.path === '/api/v1/pipeline/status')
    expect(pipelineCalls).toHaveLength(1)
    expect(pipelineCalls[0].query).toEqual({ source: 'GFS', cycle_time: '2026-05-18T00:00:00Z' })
    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.source === 'GFS')).toBe(true)
  })

  it('keeps the untouched default query on GFS in basin detail too', async () => {
    // AC4 附加 (b)：全局默认 gfs 同样适用于流域详情（`best` 只在 URL/用户显式选择时生效）；
    // 流域详情的周期仍来自 /api/v1/runs，不走 cycles 端点（fixture 决策 9）。
    const calls = mockApi()

    await useOverviewDataStore.getState().loadBasinDetail('basin-demo', defaultM11QueryState)

    const runCalls = calls.filter((call) => call.path === '/api/v1/runs')
    expect(runCalls).not.toHaveLength(0)
    expect(runCalls.every((call) => call.query?.source === 'GFS')).toBe(true)
    const forecastCall = calls.find((call) => call.path.endsWith('/forecast-series'))
    expect(forecastCall?.query?.scenarios).toBe('forecast_gfs_deterministic')
    expect(calls.filter((call) => call.path === CYCLES_PATH)).toHaveLength(0)
  })

  it('does not re-request anything when only the precipitation toggle changes', async () => {
    // precip 是纯渲染开关：进了取数身份就会整轮重载并作废在途 enrichment。
    const calls = mockApi()

    const first = await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null })
    const settledCallCount = calls.length

    const second = await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, precip: false })

    expect(second.requestScope.dataKey).toBe(first.requestScope.dataKey)
    expect(calls).toHaveLength(settledCallCount)
  })
})
