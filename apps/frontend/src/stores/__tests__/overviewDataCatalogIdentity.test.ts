// #2140：同一代 `loadOverview` 的全国 discharge `(source, cycle)` 只能从**一份**目录快照解出。
// 阶段 3 按 runless 快照写 per-cycle 键，`buildLayerStates` 若读 run-scoped 合并目录，
// 两次 fetch 之间后端 `default_cycle` 一翻转，写入键与读取键就静默错开。
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pendingActiveCycleValidTimesDisabledReason } from '@/lib/m11/overviewDataContracts'
import { useOverviewDataStore } from '@/stores/overviewData'
import {
  DEFAULT_CYCLE,
  OTHER_CYCLE,
  PRECIP_INDEX_PATH,
  VALID_TIMES_PATH,
  layer,
  mockApi,
  nationalDischargeMetadata,
  query,
  resetOverviewDataTestState,
  run,
  success,
  type MockCall,
  type MockOptions,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

beforeEach(() => {
  resetOverviewDataTestState()
})

/** runless 目录：C1 = `DEFAULT_CYCLE`，L1 = fixture metadata 列表。 */
const C1 = DEFAULT_CYCLE
const L1_NORMALIZED = ['2026-05-18T00:00:00.000Z', '2026-05-18T03:00:00.000Z', '2026-05-18T06:00:00.000Z']
/** run-scoped 目录：同一代里后端已翻到 C2（≠ C1），列表 L2 与 L1 不相交。 */
const C2 = OTHER_CYCLE
const L2 = [OTHER_CYCLE, '2026-05-17T15:00:00Z', '2026-05-17T18:00:00Z']
const L2_NORMALIZED = ['2026-05-17T12:00:00.000Z', '2026-05-17T15:00:00.000Z', '2026-05-17T18:00:00.000Z']

const scopedLayer = {
  ...layer,
  metadata: { ...nationalDischargeMetadata, default_cycle: C2, valid_times: L2 },
}

/** 按 `run_id` 区分两份 `/api/v1/layers` 响应；`runless` 可替换成不带 discharge 的目录（E8）。 */
function flippedCatalogs(runless: unknown[] = [layer]) {
  return {
    '/api/v1/layers': (options: MockOptions) =>
      success(options.params?.query?.run_id === undefined ? runless : [scopedLayer]),
  }
}

/** 前置条件：阶段 1 取 runless、阶段 2 取 fixture run 的 run-scoped，各恰好一次。 */
function expectRunlessThenScopedCatalog(calls: MockCall[]) {
  expect(calls.filter((call) => call.path === '/api/v1/layers').map((call) => call.query?.run_id)).toEqual([
    undefined,
    run.run_id,
  ])
}

function discharge() {
  return useOverviewDataStore.getState().overview?.layers.find((item) => item.layerId === 'discharge')
}

describe('overview data store discharge catalog identity (#2140)', () => {
  it('resolves the default pair from the runless catalog when the scoped catalog flipped (no URL cycle)', async () => {
    const calls = mockApi(flippedCatalogs())

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    expectRunlessThenScopedCatalog(calls)
    expect(discharge()?.activeNationalCycle).toBe(C1)
    expect(discharge()?.validTimes).toEqual(L1_NORMALIZED)
    expect(discharge()?.available).toBe(true)
    // 写入键与读取键同源：index 只为 C1 请求、只落 C1 的键；默认对零次 valid-times 请求。
    expect(calls.filter((call) => call.path === PRECIP_INDEX_PATH).map((call) => call.pathParams)).toEqual([
      { source: 'gfs', cycle: C1 },
    ])
    expect(Object.keys(useOverviewDataStore.getState().precipIndexByCycle)).toEqual([`gfs|${C1}`])
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
  })

  it('keeps a deep link to the runless default cycle available instead of pending', async () => {
    const calls = mockApi(flippedCatalogs())

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: '2026-05-18T00:00:00.000Z', validTime: null })

    expectRunlessThenScopedCatalog(calls)
    expect(discharge()?.disabledReason).toBeNull()
    expect(discharge()?.disabledReason).not.toBe(pendingActiveCycleValidTimesDisabledReason)
    expect(discharge()?.available).toBe(true)
    expect(discharge()?.validTimes).toEqual(L1_NORMALIZED)
    // 「还在加载」背后必须真有请求在途：这里一条 valid-times 请求都没发。
    expect(calls.filter((call) => call.path === VALID_TIMES_PATH)).toHaveLength(0)
  })

  it('uses the scoped discharge entry when the bootstrap failed', async () => {
    // 守卫（改动前后同绿）：bootstrap 失败 → 阶段 3 整段跳过，run-scoped 目录是本代唯一的快照。
    const calls = mockApi({
      ...flippedCatalogs(),
      '/api/v1/basins': () => {
        throw new Error('basins down')
      },
    })

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    const state = useOverviewDataStore.getState()
    expect(calls.some((call) => call.path === '/api/v1/layers' && call.query?.run_id === run.run_id)).toBe(true)
    expect(state.bootstrapError).not.toBeNull()
    expect(state.overview?.bootstrap).toBeNull()
    expect(discharge()?.available).toBe(true)
    expect(discharge()?.activeNationalCycle).toBe(C2)
    expect(discharge()?.validTimes).toEqual(L2_NORMALIZED)
  })

  it('uses the scoped discharge entry when the runless catalog has none', async () => {
    // 守卫（改动前后同绿）：首个 run 刚变 display-ready 的窗口里 runless 目录还是空的，
    // 此时不得把 run-scoped 的 discharge 一并丢掉，否则整代没有 discharge 图层。
    const calls = mockApi(flippedCatalogs([]))

    await useOverviewDataStore.getState().loadOverview({ ...query, cycle: null, validTime: null })

    expectRunlessThenScopedCatalog(calls)
    expect(useOverviewDataStore.getState().overview?.bootstrap).not.toBeNull()
    expect(discharge()?.available).toBe(true)
    expect(discharge()?.activeNationalCycle).toBe(C2)
    expect(discharge()?.validTimes).toEqual(L2_NORMALIZED)
  })
})
