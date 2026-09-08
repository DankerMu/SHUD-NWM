import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED,
  M11_PRECIP_NOTICE_INDEX_ERROR,
  M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE,
} from '@/components/map/m11PrecipOverlay'
import { defaultM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'
import { OverviewPage } from '@/pages/OverviewPage'
import { useOverviewDataStore } from '@/stores/overviewData'
import { installMaplibreStubMap } from '@/test/maplibreStub'
import {
  DEFAULT_CYCLE,
  PRECIP_INDEX_PATH,
  VALID_TIMES_PATH,
  apiError,
  layer,
  mockApi,
  model,
  precipIndex,
  precipLayer,
  precipLegend,
  resetOverviewDataTestState,
  success,
} from '@/test/overviewDataFixture'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('react-map-gl/maplibre', async () => {
  const { MaplibreMapStub, MaplibreControlStub, MaplibreSourceStub, MaplibreLayerStub, MaplibreMarkerStub } = await import(
    '@/test/maplibreStub'
  )
  return {
    default: MaplibreMapStub,
    Map: MaplibreMapStub,
    NavigationControl: MaplibreControlStub,
    ScaleControl: MaplibreControlStub,
    Source: MaplibreSourceStub,
    Layer: MaplibreLayerStub,
    Marker: MaplibreMarkerStub,
  }
})

/** URL 不带 validTime → lead 0 = 目录 `valid_times[0]`；周期 = 目录 `default_cycle`。 */
const LEAD_ZERO_VALID_TIME = '2026-05-18T00:00:00Z'
const EXPECTED_URL = `/api/v1/precip/gfs/${DEFAULT_CYCLE}/${LEAD_ZERO_VALID_TIME}.png`
/** 目录 `valid_times[1]`：点「下一个有效时刻」后时间轴派发的就是它。 */
const NEXT_VALID_TIME = '2026-05-18T03:00:00Z'
const NEXT_URL = `/api/v1/precip/gfs/${DEFAULT_CYCLE}/${NEXT_VALID_TIME}.png`
/**
 * 远超 store 的 `CACHE_TTL_MS`（`stores/overviewData.ts` = 60 s）：推过它让阶段 1 的 `cached()`
 * 条目被定时驱逐，下一轮 `loadOverview` 真的走网络。这里不复制那个常量——缓存万一仍是温的，
 * IS-7 用例里 `layersCalls` 的前置断言会当场报错，而不是静默退化成一条暖路径用例。
 */
const CACHE_EVICTION_ADVANCE_MS = 5 * 60_000

function renderOverview(overrides: Partial<typeof defaultM11QueryState> = {}) {
  const search = serializeM11QueryState({ ...defaultM11QueryState, ...overrides })
  const router = createMemoryRouter([{ path: '/', element: <OverviewPage /> }], {
    initialEntries: [search ? `/?${search}` : '/'],
  })
  render(<RouterProvider router={router} />)
}

async function settled() {
  await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(false))
  await waitFor(() => expect(useOverviewDataStore.getState().enrichmentLoading).toBe(false))
  await waitFor(() => expect(screen.queryByTestId('m11-overview-loading')).toBeNull())
}

function surface() {
  return screen.getByTestId('m11-map-surface')
}

function precipToggle() {
  return screen.getByRole('button', { name: /过去 24h 累积降水/ }) as HTMLButtonElement
}

/**
 * 三面同源不变量（fixture Evidence mapping「挂载接缝」【round-3 补】/ IS-7 / retro 纠正动作 2）。
 *
 * 开关、栅格 surface、图例卡读的是 `OverviewMode` 里**同一个** `precipCatalog` 对象。逐格用例
 * 只钉自己那一格：三轮审查每一轮都在**新站点**冒出一个新的二元谓词，于是「开关说没有 / 栅格照画」
 * 这类互相矛盾的帧总能从格与格之间的缝里漏出去。本 helper 把跨面的一致性钉在**每个**用例的
 * **每个**观测点上——包括在途窗口内、放行之前——这样任何一面单独漂都会当场红。
 */
function expectPrecipFacesAgree() {
  const toggle = precipToggle()
  const node = surface()
  const url = node.getAttribute('data-precip-url')
  const hiddenReason = node.getAttribute('data-precip-hidden-reason')
  const legendPrecip = screen.queryByTestId('m11-floating-legend-precip')

  // (i) 栅格在画 ⇒ 开关必须可用且按下，图例段必须在，且不得同时挂隐藏原因。
  if (url !== null) {
    expect(toggle.disabled).toBe(false)
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    expect(legendPrecip).not.toBeNull()
    expect(hiddenReason).toBeNull()
  }
  // (ii) 开关按不动 ⇒ 栅格不得在画，且必须说得出为什么没画。
  if (toggle.disabled) {
    expect(url).toBeNull()
    expect(hiddenReason).not.toBeNull()
  }
  // (iii) 开关说「目录未就绪」（= 三值里的 `'unknown'`，即 `layers` 为空）⇒ 栅格与图例段都不得在，
  // 且隐藏原因只能落在阶梯的**两臂**里（fixture 决策 1 round-3 规则）：第 2 臂 `no_concrete_source`
  // （`compare` / 具体源未解出——它在阶梯里**先**命中，`compare` 同时也没有活动对、键也缺席），
  // 否则第 3 臂 `index_pending`（`layers` 为空 ⇒ discharge 图层为 null ⇒ 周期解不出，由同一个空数组
  // 推出）。两臂之外不可达。落到 `'disabled'` 就是把未知折进「用户关了」/「目录确认无条目」两个
  // **终态**（IS-4/IS-5 那一格），`data-precip-hidden-reason` 这个 oracle 随即失去区分力。
  if (toggle.textContent?.includes('目录未就绪')) {
    expect(url).toBeNull()
    expect(legendPrecip).toBeNull()
    expect(['index_pending', 'no_concrete_source']).toContain(hiddenReason)
  }
}

/**
 * 目录里的 `precip` 条目：解析器的入参是 `state.precip && precipCatalog.status !== 'absent'`
 * （fixture 决策 1 第 1 臂），所以**任何**期待 URL 或提示的用例都必须先让目录带上这一条，
 * 否则它断的其实是「目录没条目 → disabled」那一格，什么也不鉴别。
 */
function catalogWithPrecip() {
  return { '/api/v1/layers': () => success([layer, precipLayer]) }
}

/** 浮层图例卡片。`mm/24h` 在图层开关的副标题里也出现，所有单位断言必须收在这张卡片内。 */
function legendCard() {
  return screen.getByTestId('m11-floating-legend')
}

/** jsdom 把 `style.backgroundColor` 归一成 `rgb(...)`；期望值走同一条 CSSOM 路径归一。 */
function normalizedColor(color: string) {
  const probe = document.createElement('span')
  probe.style.backgroundColor = color
  return probe.style.backgroundColor
}

beforeEach(() => {
  resetOverviewDataTestState()
  installMaplibreStubMap({
    loaded: () => true,
    isStyleLoaded: () => true,
    fitBounds: vi.fn(),
    project: vi.fn(() => ({ x: 0, y: 0 })),
    queryRenderedFeatures: vi.fn(() => []),
    getCanvas: () => ({ style: { cursor: '' } }),
    once: (_event: string, callback: () => void) => {
      queueMicrotask(callback)
    },
  })
})

// IS-7 用例开假时钟推过缓存 TTL：中途失败也必须还原，否则污染后面的用例。
afterEach(() => {
  vi.useRealTimers()
})

describe('OverviewPage precipitation overlay mount seam', () => {
  it('threads the resolved overlay url through OverviewMode into the map surface', async () => {
    // 前置条件：index 确实 available 且含 lead 0 的时次（否则本用例断的是隐藏态，什么也不鉴别）。
    expect(precipIndex.valid_times).toContain(LEAD_ZERO_VALID_TIME)
    mockApi(catalogWithPrecip())
    renderOverview()
    await settled()

    await waitFor(() => expect(surface().getAttribute('data-precip-url')).toBe(EXPECTED_URL))
    expect(surface().hasAttribute('data-precip-hidden-reason')).toBe(false)
    const imageSources = screen
      .queryAllByTestId('maplibre-source')
      .filter((node) => node.getAttribute('data-source-type') === 'image')
    expect(imageSources.map((node) => node.getAttribute('data-source-url'))).toEqual([EXPECTED_URL])
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expectPrecipFacesAgree()
  })

  it('hides the overlay with the unmirrored-cycle notice when the index answers PRECIP_CYCLE_NOT_MIRRORED', async () => {
    mockApi({ ...catalogWithPrecip(), [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED') })
    renderOverview()
    await settled()

    await waitFor(() => expect(surface().getAttribute('data-precip-hidden-reason')).toBe('cycle_not_mirrored'))
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(screen.getByTestId('m11-precip-notice').textContent).toBe(M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)
    // 「该周期无降水镜像」与「窗口不完整」必须是两条不同的文案（spec scenario
    // 「Unmirrored cycle is distinguishable from an incomplete window」）。
    expect(screen.getByTestId('m11-precip-notice').textContent).not.toContain('窗口不完整')
    expectPrecipFacesAgree()
  })

  it('keeps the whole overlay silent, not just the toggle, while the catalog serves no precip entry', async () => {
    // 挂载接缝的另一半：`OverviewMode` 的 `precipCatalog` 单对象推导（目录里有没有 `precip` 条目）必须
    // 同时到达浮层开关（`.status` 喂开关的 `precipAvailability` prop）**和**解析器（`.status !== 'absent'`
    // 并进 `precip` 入参）。默认目录只有 `discharge`，而 index 路由照常给 200——部署错位窗口就是这一格：
    // 只闸开关的话会出现「栅格已画 / 提示已出，开关却标未实现且按不动」。
    mockApi()
    renderOverview()
    await settled()

    // 前置条件：index 确实 available（隐藏不是因为它没到），否则本用例什么也不鉴别。
    await waitFor(() =>
      expect(useOverviewDataStore.getState().precipIndexByCycle[`gfs|${DEFAULT_CYCLE}`]?.status).toBe('available'),
    )
    const toggle = precipToggle()
    expect(toggle.disabled).toBe(true)
    expect(toggle.textContent).toContain('未实现')
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('disabled')
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expect(
      screen.queryAllByTestId('maplibre-source').filter((node) => node.getAttribute('data-source-type') === 'image'),
    ).toHaveLength(0)
    expectPrecipFacesAgree()
  })

  it('enables the floating precipitation toggle once the catalog serves a precip entry', async () => {
    mockApi(catalogWithPrecip())
    renderOverview()
    await settled()

    await waitFor(() => {
      const toggle = precipToggle()
      expect(toggle.disabled).toBe(false)
      // 按下态来自 URL 的 `precip`（默认 true），不是组件自造的常量。
      expect(toggle.getAttribute('aria-pressed')).toBe('true')
      expect(toggle.textContent).not.toContain('未实现')
    })
    expectPrecipFacesAgree()
  })

  it('says 目录未就绪 rather than 未实现 while the layer catalog is still in flight', async () => {
    // IS-4/IS-5：目录在途那一帧 `layers` 就是 `[]`，在二值谓词下与「目录到了且没有 `precip` 条目」
    // 同映射。塌成布尔有两个受害面：(a) 开关对用户发出一句当时没有证据的**终态**断言「未实现」；
    // (b) 解析器把在途折进 `disabled` 臂，于是 `data-precip-hidden-reason` 分不出「用户关了」
    // 与「还没到」。两条都只有在 `settled()` **之前**读才抓得到。
    let releaseLayers: () => void = () => undefined
    const layersGate = new Promise<void>((resolve) => {
      releaseLayers = resolve
    })
    let layersCalls = 0
    mockApi({
      '/api/v1/layers': async () => {
        layersCalls += 1
        await layersGate
        return success([layer, precipLayer])
      },
    })
    renderOverview()

    // 前置条件：确实停在目录在途窗口里——请求已发出、bootstrap 未落定、快照一份都还没有。
    await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true))
    // 目录请求已发出但一条都没落地（阶段 1 的 runless 与阶段 2 的 run-scoped 共用这道闸）。
    expect(layersCalls).toBeGreaterThan(0)
    expect(useOverviewDataStore.getState().overview).toBeNull()

    const pendingToggle = precipToggle()
    expect(pendingToggle.disabled).toBe(true)
    expect(pendingToggle.getAttribute('aria-pressed')).toBe('false')
    expect(pendingToggle.textContent).toContain('目录未就绪')
    expect(pendingToggle.textContent).not.toContain('未实现')
    expect(pendingToggle.getAttribute('title')).not.toContain('未实现')
    // 在途落第 3 臂（`layers` 为空 ⇒ discharge 图层为 null ⇒ 周期未解出），**不是** `disabled`。
    // 这是同一个空数组推出来的，不是一条独立前提。
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('index_pending')
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expectPrecipFacesAgree()

    releaseLayers()
    await settled()

    // 目录落地后这一格必须自愈成「可用且按下」，否则上面的否定断言可以被「永远说加载中」骗过。
    await waitFor(() => {
      const toggle = precipToggle()
      expect(toggle.disabled).toBe(false)
      expect(toggle.getAttribute('aria-pressed')).toBe('true')
      expect(toggle.textContent).not.toContain('目录未就绪')
    })
    await waitFor(() => expect(surface().getAttribute('data-precip-url')).toBe(EXPECTED_URL))
    expectPrecipFacesAgree()
  })

  it('pins the no_concrete_source arm when ?source=compare meets a catalog still in flight', async () => {
    // 上一条的姊妹格（fixture 挂载接缝【round-3 补】最后一句）：目录在途时开关的「目录未就绪」
    // 是**同一句**，但解析器阶梯第 2 臂（`compare` ⇒ 具体源解不出）比第 3 臂**先**命中，于是
    // `data-precip-hidden-reason` 是 `no_concrete_source` 而不是 `index_pending`。不单独钉这一格，
    // 三面同源 helper 的 (iii) 就只有一条 `index_pending` 见证者，放宽成集合后没人证明另一臂真能到达。
    let releaseLayers: () => void = () => undefined
    const layersGate = new Promise<void>((resolve) => {
      releaseLayers = resolve
    })
    let layersCalls = 0
    mockApi({
      '/api/v1/layers': async () => {
        layersCalls += 1
        await layersGate
        return success([layer, precipLayer])
      },
    })
    renderOverview({ source: 'compare' })

    // 前置条件：确实停在目录在途窗口里（请求已发、bootstrap 未落定、快照一份都没有）。
    await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true))
    expect(layersCalls).toBeGreaterThan(0)
    expect(useOverviewDataStore.getState().overview).toBeNull()

    const pendingToggle = precipToggle()
    expect(pendingToggle.disabled).toBe(true)
    expect(pendingToggle.getAttribute('aria-pressed')).toBe('false')
    expect(pendingToggle.textContent).toContain('目录未就绪')
    expect(pendingToggle.textContent).not.toContain('未实现')
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('no_concrete_source')
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expectPrecipFacesAgree()

    releaseLayers()
    await settled()

    // 目录落地后「未知」这一面必须自愈（开关可用），而「源解析不出」这一面是**持久**事实：
    // 隐藏原因仍是 `no_concrete_source`，提示 A 出现在互斥链尾（此格 basins 非空、无硬失败）。
    await waitFor(() => {
      const toggle = precipToggle()
      expect(toggle.disabled).toBe(false)
      expect(toggle.textContent).not.toContain('目录未就绪')
    })
    expect(useOverviewDataStore.getState().overview?.layers.some((entry) => entry.layerId === 'precip')).toBe(true)
    await waitFor(() =>
      expect(screen.getByTestId('m11-precip-notice').textContent).toBe(M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE),
    )
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('no_concrete_source')
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expectPrecipFacesAgree()
  })

  it('keeps the toggle live while a validTime-only reload is in flight over the same catalog', async () => {
    // IS-7（round-3，与 IS-2/IS-4 同一条不变量的**反方向**）：时间轴步进/播放派发的是
    // validTime-only 重载，而 `loadOverview` 的起始 `set` 只置 `mapBootstrapLoading: true`，
    // **不清** `overview` / `precipIndexByCycle`。于是目录明明在手、栅格与图例照画，闸门里的
    // `mapBootstrapLoading` 臂却把它判成「目录未就绪」并把开关按死——已知冒充未知。
    // 冷缓存（距上次阶段 1 取数 ≥ TTL）下**每一次**步进都落在这一格。
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let releaseLayers: () => void = () => undefined
    let layersGate: Promise<void> | null = null
    let layersCalls = 0
    mockApi({
      '/api/v1/layers': async () => {
        layersCalls += 1
        if (layersGate) await layersGate
        return success([layer, precipLayer])
      },
    })
    // URL 自带 lead-0 T：否则首屏时次校正 effect 会自己再派发一轮 loadOverview，和下面手动
    // 制造的窗口抢同一个 nonce，用例就不再确定停在被测那一帧。
    renderOverview({ validTime: LEAD_ZERO_VALID_TIME })
    await settled()

    await waitFor(() => expect(surface().getAttribute('data-precip-url')).toBe(EXPECTED_URL))
    expectPrecipFacesAgree()

    // 阶段 1 的 `cached()` 条目按 TTL 定时驱逐 → 下一轮真的走网络往返，不是暖缓存微任务。
    await vi.advanceTimersByTimeAsync(CACHE_EVICTION_ADVANCE_MS)
    const callsBeforeStep = layersCalls
    layersGate = new Promise<void>((resolve) => {
      releaseLayers = resolve
    })

    fireEvent.click(screen.getByRole('button', { name: '下一个有效时刻' }))

    await waitFor(() => expect(useOverviewDataStore.getState().mapBootstrapLoading).toBe(true))
    // 前置条件 1：这一轮确实闸在网络上（缓存真的冷了），否则窗口是假的、本用例什么也不鉴别。
    await waitFor(() => expect(layersCalls).toBeGreaterThan(callsBeforeStep))
    // 前置条件 2：目录快照没被清掉——这正是「已知」的证据，也是三面矛盾成立的前提。
    expect(useOverviewDataStore.getState().overview).not.toBeNull()
    expect(useOverviewDataStore.getState().overview?.layers.some((entry) => entry.layerId === 'precip')).toBe(true)

    const duringToggle = precipToggle()
    expect(duringToggle.disabled).toBe(false)
    expect(duringToggle.getAttribute('aria-pressed')).toBe('true')
    expect(duringToggle.textContent).not.toContain('目录未就绪')
    // 可用态本就不挂 title（属性缺席 → `.title` 为 ''）：这里只钉「不得出现未就绪的说法」。
    expect(duringToggle.title).not.toContain('目录未就绪')
    expect(surface().getAttribute('data-precip-url')).toBe(EXPECTED_URL)
    expect(surface().hasAttribute('data-precip-hidden-reason')).toBe(false)
    expect(screen.getByTestId('m11-floating-legend-precip')).toBeInTheDocument()
    expectPrecipFacesAgree()

    releaseLayers()
    layersGate = null
    await settled()

    // 放行后这一步确实改了时次（否则上面的「URL 没变」可以被「点击根本没生效」骗过）。
    await waitFor(() => expect(surface().getAttribute('data-precip-url')).toBe(NEXT_URL))
    expectPrecipFacesAgree()
  })

  it('threads the catalog precip legend into the floating legend card', async () => {
    // 图例的**唯一**来源是目录 `precip` 条目的 `metadata.legend`（fixture 决策 7）：
    // 这条走通 `OverviewMode → M11FloatingLegend` 的穿线，断言六个色块逐字来自目录数据。
    mockApi(catalogWithPrecip())
    renderOverview()
    await settled()

    await waitFor(() => expect(within(legendCard()).getByText(/mm\/24h/)).toBeInTheDocument())
    const swatches = within(legendCard())
      .getAllByTestId('m11-floating-legend-precip-swatch')
      .map((node) => node.style.backgroundColor)
    expect(swatches).toEqual(precipLegend.map((entry) => normalizedColor(entry.color)))
    expect(precipLayer.metadata.legend).toBe(precipLegend)
    expectPrecipFacesAgree()
  })

  it('suppresses the precipitation legend section while the overlay is switched off', async () => {
    // `?precip=0`：目录条目照旧到达（开关仍可用），但图例段不得渲染——给一个没画出来的
    // 图层留着色阶就是在假装它还在。
    mockApi(catalogWithPrecip())
    renderOverview({ precip: false })
    await settled()

    // 前置条件：目录确实到了（否则本用例断的是「目录没来」，什么也不鉴别）。
    await waitFor(() => {
      const toggle = precipToggle()
      expect(toggle.disabled).toBe(false)
      expect(toggle.getAttribute('aria-pressed')).toBe('false')
    })
    expectPrecipFacesAgree()
    expect(within(legendCard()).queryByText(/mm\/24h/)).toBeNull()
    expect(screen.queryByTestId('m11-floating-legend-precip')).toBeNull()
    // `state.precip` 在 `OverviewMode` 被读了三次（解析器 / 图例 / 开关）：只钉后两处时，
    // 把解析器那一处改成常量 `true` 仍全绿，而栅格会照发 PNG 请求。闸在解析器上。
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('disabled')
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(
      screen.queryAllByTestId('maplibre-source').filter((node) => node.getAttribute('data-source-type') === 'image'),
    ).toHaveLength(0)
    expectPrecipFacesAgree()
  })

  it('keeps the overlay pending while the active cycle valid-time list is unresolved', async () => {
    // 三元组必须与流量层**同源**：时次取的是 discharge `LayerState.currentValidTime`，
    // 不是 URL 上的 `state.validTime`。活动列表未定时前者为 null（校正 effect 此刻刻意不改写
    // URL），叠加层诚实地停在 index_pending；改读 `state.validTime` 就会为一个活动周期未必
    // 承认的时次拼出 PNG URL 并真的去取它。
    const urlValidTime = '2026-05-18T03:00:00.000Z'
    expect(precipIndex.valid_times).toContain(urlValidTime.replace('.000Z', 'Z'))
    // `source=ifs` 是非默认源 → 该周期的 valid-times 必须自己取；这条请求永不落地即 pending 窗口。
    mockApi({ ...catalogWithPrecip(), [VALID_TIMES_PATH]: () => new Promise(() => undefined) })
    renderOverview({ source: 'ifs', validTime: urlValidTime })
    await settled()

    // 前置条件：index 确实 available（隐藏不是因为 index 没到），而列表确实未定。
    await waitFor(() =>
      expect(useOverviewDataStore.getState().precipIndexByCycle[`ifs|${DEFAULT_CYCLE}`]?.status).toBe('available'),
    )
    expect(useOverviewDataStore.getState().validTimesByCycle[`ifs|${DEFAULT_CYCLE}`]).toBeUndefined()

    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('index_pending')
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expectPrecipFacesAgree()
  })

  it('keeps the empty-basin notice ahead of a persistent precipitation notice once the surface has settled', async () => {
    // 决策 9 的链位判别用例（round-1 更正后的方向）：只有「流域清单为空 **且** 降水隐藏有提示」
    // 这一格能把「排在 emptyBasinReason 之前」与「排在它之后」区分开。`emptyBasinReason` 是本
    // 组件里 bootstrap / enrichment 硬失败的**唯一**渲染面，降水提示是装饰层信息，必须让位。
    mockApi({
      ...catalogWithPrecip(),
      '/api/v1/basins': () => success([]),
      [PRECIP_INDEX_PATH]: () => apiError('PRECIP_CYCLE_NOT_MIRRORED'),
    })
    renderOverview()
    await settled()

    await waitFor(() => expect(screen.getByTestId('m11-overview-empty')).toBeInTheDocument())
    // 前置条件：降水提示这一支确实"想说话"（不然本用例断的是「降水本来就没话说」）。
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('cycle_not_mirrored')
    expect(useOverviewDataStore.getState().overview?.basins ?? []).toHaveLength(0)
    // 互斥链：一次只出一条（所有 M11FloatingNotice 同坐标绝对定位，并列会像素重叠）。
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expect(screen.getByTestId('m11-overview-empty').textContent).not.toBe(M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)
    expectPrecipFacesAgree()
  })

  it('never lets a persistent precipitation notice bury the bootstrap-failure state', async () => {
    // `?source=compare` 的提示 A 与运行时状态无关，整会话恒亮：它排在 `emptyBasinReason` 前面
    // 时，会把「总览数据加载失败」永久顶掉（`overview-data-contracts` spec 要求 bootstrap
    // 失败必须如实呈现，而不是无限 spinner 或别的图层的提示）。
    mockApi({
      ...catalogWithPrecip(),
      '/api/v1/basins': () => {
        throw new Error('basins down')
      },
    })
    renderOverview({ source: 'compare' })
    await settled()

    await waitFor(() => expect(useOverviewDataStore.getState().bootstrapError).not.toBeNull())
    const bootstrapError = useOverviewDataStore.getState().bootstrapError
    // 前置条件：降水提示这一支确实"想说话"（compare 解析不出具体源 → 提示 A）。
    expect(surface().getAttribute('data-precip-hidden-reason')).toBe('no_concrete_source')
    expect(screen.getByTestId('m11-overview-empty').textContent).toBe(bootstrapError)
    expect(screen.queryByTestId('m11-precip-notice')).toBeNull()
    expect(screen.queryByText(M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE)).toBeNull()
    expectPrecipFacesAgree()
  })

  it('surfaces the index-error notice when a bootstrap failure skips the layer-time enrichment chain', async () => {
    // 最静默的一格：阶段 1 的 runless `/api/v1/layers` 被拒 → bootstrap 失败 → 阶段 3 整段跳过，
    // precip index 请求一条都不会发。而 `cached()` 在 reject 臂删掉了条目，阶段 2 的重试会成功，
    // 于是流量层与流域清单照常渲染——页面看上去健康，降水却永远不会到。此时把「键缺席」
    // 渲染成沉默的 `index_pending` 就是谎报「等一下就有」。
    let layersCalls = 0
    mockApi({
      '/api/v1/layers': () => {
        layersCalls += 1
        if (layersCalls === 1) throw new Error('runless layer catalog down')
        return success([layer, precipLayer])
      },
      // `cached()` 共享在途 promise：阶段 2 的目录请求必须晚于阶段 1 的 reject 落地，
      // 否则它命中同一个被拒 promise，目录拿不到 → 断的就成了「无目录 → disabled」那一格。
      '/api/v1/models': async () => {
        await new Promise((resolve) => setTimeout(resolve, 0))
        return success({ items: [model], total: 1, limit: 200, offset: 0 })
      },
    })
    // URL 自带 T（任何深链/分享链接都如此）：否则时次校正 effect 会改写 URL 触发第二轮
    // `loadOverview`，那一轮 bootstrap 成功、index 也就取到了——自愈路径掩盖掉本用例要钉的格。
    renderOverview({ validTime: LEAD_ZERO_VALID_TIME })
    await settled()

    // 前置条件：bootstrap 确实失败、阶段 3 确实被跳过、而阶段 2 的重试确实把页面救回了健康相。
    expect(layersCalls).toBe(2)
    expect(useOverviewDataStore.getState().bootstrapError).not.toBeNull()
    expect(useOverviewDataStore.getState().layerTimeEnrichmentSkipped).toBe(true)
    expect(useOverviewDataStore.getState().precipIndexByCycle).toEqual({})
    expect(useOverviewDataStore.getState().overview?.basins ?? []).not.toHaveLength(0)
    const discharge = (useOverviewDataStore.getState().overview?.layers ?? []).find(
      (item) => item.layerId === 'discharge',
    )
    expect(discharge?.available).toBe(true)
    expect(screen.queryByTestId('m11-overview-empty')).toBeNull()

    await waitFor(() => expect(surface().getAttribute('data-precip-hidden-reason')).toBe('index_error'))
    expect(surface().hasAttribute('data-precip-url')).toBe(false)
    expect(screen.getByTestId('m11-precip-notice').textContent).toBe(M11_PRECIP_NOTICE_INDEX_ERROR)
    expectPrecipFacesAgree()
  })
})
