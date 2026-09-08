import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { buildM11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import {
  activeCycleValidTimesErrorDisabledReason,
  failClosedDischargeDisabledReason,
  normalizeLayerStates,
  pendingActiveCycleValidTimesDisabledReason,
  type LayerState,
} from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'
import {
  M11BottomControlBar,
  deriveM11ControlBarModel,
  m11ControlBarHeightClass,
  type M11ControlBarInput,
} from '@/pages/m11/M11BottomControlBar'
import { M11Timeline, m11SourceOptions } from '@/pages/m11/M11Controls'
import type { DischargeCyclesState } from '@/stores/overviewData'

/** 全国 discharge 的 57 项 3h 列表（`+0h … +168h`），秒精度拼写与后端一致。 */
const DEFAULT_CYCLE = '2026-05-18T00:00:00Z'
const VALID_TIMES = Array.from({ length: 57 }, (_, index) =>
  new Date(Date.parse(DEFAULT_CYCLE) + index * 3 * 3_600_000).toISOString().replace('.000Z', 'Z'),
)
/** `LayerState.validTimes` 的毫秒形（`normalizeIsoString` 的产物），控制条原样透传。 */
const MILLISECOND_VALID_TIMES = VALID_TIMES.map((validTime) => new Date(Date.parse(validTime)).toISOString())

type CatalogMetadata = Record<string, unknown>

function dischargeMetadata(overrides: CatalogMetadata = {}): CatalogMetadata {
  return {
    layer_id: 'discharge',
    tile_format: 'mvt',
    maplibre_source_layer: 'hydro',
    min_zoom: 3,
    max_zoom: 10,
    url_template: '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf',
    required_placeholders: ['source', 'cycle', 'valid_time', 'z', 'x', 'y'],
    source_refs: { basin_version_id: 'bv-001', river_network_version_id: 'rn-001' },
    default_source: 'gfs',
    default_cycle: DEFAULT_CYCLE,
    valid_times: VALID_TIMES,
    fallback_available: false,
    release_blocking: false,
    ...overrides,
  }
}

/**
 * `LayerState` 一律经 `normalizeLayerStates` 造：`currentValidTime`（lead 0 / 旧时次回落）
 * 是 #2012 的传导链，手搓 fixture 会把 AC2-b 与 AC5 变成同义反复。
 */
function layersFor(
  query: M11QueryState,
  metadata: CatalogMetadata,
  activeCycleValidTimes?: Parameters<typeof normalizeLayerStates>[0]['activeCycleValidTimes'],
): LayerState[] {
  return normalizeLayerStates({
    query,
    layers: [
      {
        layer_id: 'discharge',
        layer_name: 'Discharge',
        layer_type: 'hydrology',
        variables: ['q_down'],
        metadata: metadata as never,
      },
    ],
    activeCycleValidTimes,
  })
}

function inputFor(
  query: M11QueryState,
  options: {
    metadata?: CatalogMetadata | null
    cyclesBySource?: Record<string, DischargeCyclesState>
    layers?: LayerState[]
  } = {},
): M11ControlBarInput {
  const metadata = options.metadata === undefined ? dischargeMetadata() : options.metadata
  return {
    state: query,
    layers: options.layers ?? (metadata ? layersFor(query, metadata) : []),
    metadata: metadata as never,
    cyclesBySource: options.cyclesBySource ?? {},
    sourceSelection: null,
  }
}

function availableCycles(cycleTimes: string[]): DischargeCyclesState {
  return {
    status: 'available',
    cycles: {
      source: 'gfs',
      cycles: cycleTimes.map((cycleTime) => ({
        cycle_time: cycleTime,
        valid_time_start: cycleTime,
        valid_time_end: cycleTime,
      })),
      default_cycle: cycleTimes[0] ?? null,
    },
  }
}

describe('deriveM11ControlBarModel', () => {
  it('offers exactly the two national sources, reusing the shared option copy', () => {
    // AC2-a：全国分段只有 GFS/IFS（无 Best Available / 对比），文案复用 `m11SourceOptions`。
    const model = deriveM11ControlBarModel(inputFor(defaultM11QueryState))

    expect(model.sourceOptions.map((option) => option.value)).toEqual(['gfs', 'ifs'])
    expect(model.sourceOptions.map((option) => option.label)).toEqual(['GFS', 'IFS'])
    // 同一常量对象（不是第二份文案表的拷贝）：`toContain` 对对象按引用比。
    for (const option of model.sourceOptions) expect(m11SourceOptions).toContain(option)
  })

  it('derives the default triple from the query defaults and the catalog metadata', () => {
    // AC2-b：入参是 `defaultM11QueryState` + 目录 metadata，不是预先算好的三元组。
    const { model } = renderControlBar(inputFor(defaultM11QueryState))

    expect(model.source).toBe('gfs')
    // cycle 归到秒精度（`<select>` 的 option 是秒精度，毫秒形选不中任何一项）。
    expect(model.cycle).toBe(DEFAULT_CYCLE)
    // 三元组的第三项落在**渲染出的**当前时次上（round-1 finding D）：模型不再带 `validTime`
    // 字段——它从来不是渲染源，`M11Timeline` 自己从 `layers` 派生同一份列表。默认停在
    // lead 0 = 活动列表首项（毫秒形，与 `LayerState.validTimes` 同形）。
    expect(MILLISECOND_VALID_TIMES[0]).toBe('2026-05-18T00:00:00.000Z')
    expect(screen.getByText(MILLISECOND_VALID_TIMES[0])).toBeTruthy()
    expect(model.disabled).toBe(false)
    expect(model.disabledReason).toBeNull()
  })

  it('resolves a restored ?source=best link to gfs at national scale', () => {
    // AC2-b（第二条）：全国 `best → gfs`，且 `cyclesBySource` 用的是解析后的具体源 key。
    const model = deriveM11ControlBarModel(
      inputFor(
        { ...defaultM11QueryState, source: 'best' },
        { cyclesBySource: { gfs: availableCycles([DEFAULT_CYCLE, '2026-05-17T12:00:00Z']) } },
      ),
    )

    expect(model.source).toBe('gfs')
    expect(model.cycles).toEqual([DEFAULT_CYCLE, '2026-05-17T12:00:00Z'])
  })

  it('falls back to the single default cycle before the cycles list arrives, then expands without moving the selection', () => {
    // AC2-d：cycles 是 enrichment（bootstrap 之后才到）。两态之间已选 cycle 不得跳变。
    // 入参必须钉死才有鉴别力（round-1 finding E）：`state.cycle` 为 null——非空时有效 cycle
    // 直接来自 URL，与 cycles 列表无关，断言什么也不鉴别；且到达列表的**首项新于**
    // `metadata.default_cycle`，这样「实现改成读 cycles[0]」才会让下面的断言变红。
    const query = { ...defaultM11QueryState }
    expect(query.cycle).toBeNull()
    const before = deriveM11ControlBarModel(inputFor(query))
    const after = deriveM11ControlBarModel(
      inputFor(query, {
        cyclesBySource: {
          // 最新在前；毫秒形拼写必须被归到秒精度，否则 `<select>` 的 value 选不中。
          gfs: availableCycles(['2026-05-18T12:00:00.000Z', DEFAULT_CYCLE, '2026-05-17T12:00:00Z']),
        },
      }),
    )

    expect(before.cycles).toEqual([DEFAULT_CYCLE])
    expect(after.cycles).toEqual(['2026-05-18T12:00:00Z', DEFAULT_CYCLE, '2026-05-17T12:00:00Z'])
    expect(before.cycle).toBe(DEFAULT_CYCLE)
    expect(after.cycle).toBe(before.cycle)
  })

  it('keeps the default cycle option when the cycles enrichment failed', () => {
    // enrichment reject 只产 scoped `'error'`：控制条照样可渲染（回落单项），不塌成空选择器。
    const model = deriveM11ControlBarModel(inputFor(defaultM11QueryState, { cyclesBySource: { gfs: { status: 'error' } } }))

    expect(model.cycles).toEqual([DEFAULT_CYCLE])
  })

  it('falls back to the default cycle when an available cycles payload is malformed', () => {
    // round-1 finding B：`unwrapApiData`（`api/response.ts`）是裸 `as T` 断言、零运行时校验，
    // 缺字段/变形的响应会带着 `cycles: undefined` 一路进到这里。无守卫时 `.map` 在 render 中
    // 抛出，而这条路径上没有 error boundary —— 整页白屏。守卫后退化成「像没到达一样回落」。
    const model = deriveM11ControlBarModel(
      inputFor(defaultM11QueryState, { cyclesBySource: { gfs: { status: 'available', cycles: {} as never } } }),
    )

    expect(model.cycles).toEqual([DEFAULT_CYCLE])
  })

  it('leaves ?source=compare without a segment selection and falls back to the default cycle', () => {
    // 已知边角（决策 5）：store 从不为 `compare` 写 cyclesBySource，故恒回落单项、分段无选中态。
    const model = deriveM11ControlBarModel(
      inputFor(
        { ...defaultM11QueryState, source: 'compare' },
        { cyclesBySource: { gfs: availableCycles([DEFAULT_CYCLE, '2026-05-17T12:00:00Z']) } },
      ),
    )

    expect(model.source).toBe('compare')
    expect(model.sourceOptions.map((option) => option.value)).not.toContain('compare')
    expect(model.cycles).toEqual([DEFAULT_CYCLE])
  })

  it('is fail-closed on a null default cycle, the same input that registers no overlay', () => {
    // AC3：`default_cycle === null` + 空 valid_times = 全国交集 fail-closed。
    const query = { ...defaultM11QueryState, validTime: '2026-05-18T06:00:00.000Z' }
    const metadata = dischargeMetadata({ valid_times: [], default_cycle: null })
    const layers = layersFor(query, metadata)
    const model = deriveM11ControlBarModel(inputFor(query, { metadata, layers }))

    expect(model.disabled).toBe(true)
    expect(model.disabledReason).toBe(failClosedDischargeDisabledReason)
    expect(model.cycles).toEqual([])
    // 同一入参下地图侧也不注册叠加层：控制条与地图对 fail-closed 的判断同源。
    expect(buildM11RegisteredOverlay(query, layers)).toBeNull()
  })

  it('empties the cycle list on a fail-closed catalog even when a cycles payload already arrived', () => {
    // round-1 finding A：`cyclesBySource` 与目录 metadata 是两条独立的取数与缓存路径——目录侧
    // `_default_layer_catalog` 只按 GFS 算 `default_cycle` 并在样本时次为空时抹成 null，而
    // enrichment 的 `fetchDischargeCycles` 按**当前 URL 源**发。于是「目录判 fail-closed」与
    // 「`cyclesBySource[source]` 非空」可同时成立（IFS 有周期而 GFS 没有；或两侧缓存新鲜度
    // 不同，TTL 60s / stale 600s 可持续十分钟）。闸口按**理由**走：fail-closed 时 cycles 恒空。
    const query = { ...defaultM11QueryState }
    const metadata = dischargeMetadata({ valid_times: [], default_cycle: null })
    const layers = layersFor(query, metadata)
    const input = inputFor(query, {
      metadata,
      layers,
      cyclesBySource: { gfs: availableCycles([DEFAULT_CYCLE, '2026-05-17T12:00:00Z']) },
    })

    expect(deriveM11ControlBarModel(input).disabledReason).toBe(failClosedDischargeDisabledReason)
    expect(deriveM11ControlBarModel(input).cycles).toEqual([])

    // DOM 半边：React 18 不会把「`value=""` 且无匹配 option」的 select 显示成空，它落到
    // `defaultSelected`，最新周期会**显示为选中**且点它不触发 onChange。只剩占位项才是真禁。
    renderControlBar(input)
    const select = screen.getByLabelText('起报时次') as HTMLSelectElement
    expect(select.disabled).toBe(true)
    expect([...select.options].map((option) => option.textContent)).toEqual(['无可用起报时次'])
  })

  it('passes the per-cycle transition reasons through instead of only recognizing fail-closed', () => {
    // AC3：`disabledReason` 直接透传 `LayerState.disabledReason`。硬编码只认 fail-closed 会把
    // 切周期的两个过渡态显示成「已启用但列表空」的时间轴。
    const query = { ...defaultM11QueryState, cycle: '2026-05-17T12:00:00.000Z' }
    const metadata = dischargeMetadata()
    for (const [override, reason] of [
      [{ status: 'pending' } as const, pendingActiveCycleValidTimesDisabledReason],
      [{ status: 'error' } as const, activeCycleValidTimesErrorDisabledReason],
    ] as const) {
      const layers = layersFor(query, metadata, { discharge: override })
      const model = deriveM11ControlBarModel(inputFor(query, { metadata, layers }))

      // 过渡态的活动列表确实是空的（#2012 的传导链）：`disabled` 由这条派生，钉住入参本身，
      // 否则下面两条会退化成「只要 reason 非空就禁用」的同义反复。
      expect(layers.find((layer) => layer.layerId === 'discharge')?.validTimes).toEqual([])
      expect(model.disabledReason).toBe(reason)
      expect(model.disabled).toBe(true)
      // 过渡态不是 fail-closed：cycles 仍照常派生（不塌成空），fail-closed 才是空列表。
      // 展示层据此让起报时次 `<select>` 保持可用（禁用条件是 cycles 为空，不是这个聚合 `disabled`）
      // —— DOM 那半见「keeps the cycle selector usable when one cycle failed to resolve its valid times」。
      expect(model.cycles).toEqual([DEFAULT_CYCLE])
    }
  })

  it('stays renderable before the layer catalog lands', () => {
    // bootstrap 之前 `mergeLayerStates` 返回空数组：控制条必须诚实禁用，而不是抛错或伪造时次。
    // 这一态**不是** fail-closed（理由是「等待图层注册」），故不走 finding A 的那道闸口——
    // 把闸口写成 `defaultCycle === null` 会连这条过渡窗口一起塌掉，故意留作反例。
    const model = deriveM11ControlBarModel(inputFor(defaultM11QueryState, { metadata: null, layers: [] }))

    expect(model.disabled).toBe(true)
    expect(model.disabledReason).toBe('等待 /api/v1/layers 图层注册状态')
    expect(model.cycles).toEqual([])
    expect(model.cycle).toBeNull()
  })
})

function renderControlBar(
  input: M11ControlBarInput,
  onQueryChange: (patch: import('@/lib/m11/queryState').M11QueryPatch) => void = vi.fn(),
) {
  const model = deriveM11ControlBarModel(input)
  const view = render(<M11BottomControlBar {...model} onQueryChange={onQueryChange} />)
  return { ...view, model, onQueryChange }
}

describe('M11BottomControlBar', () => {
  it('renders only the two national source choices', () => {
    // AC2-a 的 DOM 半边：全国不出现 Best Available / 对比。
    renderControlBar(inputFor(defaultM11QueryState))

    expect(screen.getByRole('button', { name: 'GFS' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'IFS' })).toBeTruthy()
    expect(screen.queryByText(/Best Available/)).toBeNull()
    expect(screen.queryByText(/对比/)).toBeNull()
  })

  it('renders the corrected current valid time rather than a stale URL valid time', () => {
    // AC5：切 (source, cycle) 后旧时次不在新列表 → 渲染出的当前时次跟随 `currentValidTime`
    // （首项），不是 URL 的旧值。断言落在**渲染**上（round-1 finding D）：模型不再带
    // `validTime` 字段，而时次的唯一渲染源一直是 `M11Timeline` 自派生的 `buildM11TimelineViewModel`。
    // `normalizeLayerStates` 那半的 oracle 见 m11OverviewDataContracts.test.ts
    // 「falls back to lead 0 when the previous valid time is not in the new list」。
    const staleValidTime = '2026-05-17T06:00:00.000Z'
    const staleInput = inputFor({ ...defaultM11QueryState, validTime: staleValidTime })
    // 前置钉死：过期时次确实不在活动列表里，否则本条会随 fixture 漂移塌成同义反复。
    expect(staleInput.layers.find((layer) => layer.layerId === 'discharge')?.validTimes).not.toContain(staleValidTime)
    const { unmount } = renderControlBar(staleInput)

    expect(screen.getByText(MILLISECOND_VALID_TIMES[0])).toBeTruthy()
    expect(screen.queryByText(staleValidTime)).toBeNull()
    unmount()

    // 仍在列表内的 URL 时次必须保留（否则用户每次选时次都会被弹回首项）。
    renderControlBar(inputFor({ ...defaultM11QueryState, validTime: MILLISECOND_VALID_TIMES[4] }))
    expect(screen.getByText(MILLISECOND_VALID_TIMES[4])).toBeTruthy()
  })

  it('renders one +{lead}h tick per valid time from the effective cycle', () => {
    // AC2-c：57 项 3h fixture 下 `+0h … +168h`，lead 由 (validTime − 有效 cycle) 现算。
    renderControlBar(inputFor(defaultM11QueryState))

    const ticks = screen.getAllByTestId('m11-timeline-tick')
    expect(ticks).toHaveLength(57)
    expect(ticks[0].getAttribute('data-lead')).toBe('0')
    expect(ticks[0].getAttribute('title')).toBe('+0h 2026-05-18T00:00:00.000Z')
    const last = ticks[ticks.length - 1]
    expect(last.getAttribute('data-lead')).toBe('168')
    expect(last.getAttribute('title')).toBe('+168h 2026-05-25T00:00:00.000Z')
    // 可见文字按 24h 倍数抽稀：57 项不会渲染 57 个可见标签。
    expect(ticks.filter((tick) => tick.textContent !== '').length).toBeLessThan(ticks.length)
    expect(screen.getByText('+24h')).toBeTruthy()
  })

  it('dispatches source and cycle changes to the URL', async () => {
    // 决策 6：分段写 `{ source }`、起报时次写 `{ cycle }`，都不新建取数路径。
    const user = userEvent.setup()
    const onQueryChange = vi.fn()
    renderControlBar(
      inputFor(defaultM11QueryState, { cyclesBySource: { gfs: availableCycles([DEFAULT_CYCLE, '2026-05-17T12:00:00Z']) } }),
      onQueryChange,
    )

    await user.click(screen.getByRole('button', { name: 'IFS' }))
    expect(onQueryChange).toHaveBeenLastCalledWith({ source: 'ifs' })

    await user.selectOptions(screen.getByLabelText('起报时次'), '2026-05-17T12:00:00Z')
    expect(onQueryChange).toHaveBeenLastCalledWith({ cycle: '2026-05-17T12:00:00Z' })
  })

  it('shows the active cycle even when the cycles list does not carry it yet', () => {
    // 手敲 `?cycle=` / 列表尚未覆盖该周期：`<select>` 必须显示活动周期，而不是默认落在首项上。
    const query = { ...defaultM11QueryState, cycle: '2026-05-17T18:00:00.000Z' }
    renderControlBar(inputFor(query, { cyclesBySource: { gfs: availableCycles([DEFAULT_CYCLE]) } }))

    const select = screen.getByLabelText('起报时次') as HTMLSelectElement
    expect(select.value).toBe('2026-05-17T18:00:00Z')
    expect([...select.options].map((option) => option.value)).toEqual(['2026-05-17T18:00:00Z', DEFAULT_CYCLE])
    expect(select.options[0].textContent).toBe('05-17 18Z')
  })

  it('disables every selector, the slider and both playback controls when fail-closed', () => {
    // AC3 的 DOM 半边：起报时次 / 滑块 / 三个播放按钮 / 倍速全禁用 + 可读提示。
    const query = { ...defaultM11QueryState }
    const metadata = dischargeMetadata({ valid_times: [], default_cycle: null })
    renderControlBar(inputFor(query, { metadata, layers: layersFor(query, metadata) }))

    expect((screen.getByLabelText('起报时次') as HTMLSelectElement).disabled).toBe(true)
    expect((screen.getByLabelText('有效时间滑块') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('播放速度') as HTMLSelectElement).disabled).toBe(true)
    for (const name of ['上一个有效时刻', '播放时间轴', '下一个有效时刻']) {
      expect((screen.getByLabelText(name) as HTMLButtonElement).disabled).toBe(true)
    }
    expect(screen.getByText(failClosedDischargeDisabledReason)).toBeTruthy()
    // 列表为空那半也落在渲染上（模型不再带 `validTimes`，round-1 finding D）。
    expect(screen.getByText('当前图层没有有效时间')).toBeTruthy()
  })

  it('sits 16px above the viewport bottom at the shared timeline height token', () => {
    // AC6：高度类名由 token 经被测映射函数得出（token 改 '80px' → 类名变 h-20，本断言变红）。
    renderControlBar(inputFor(defaultM11QueryState))
    const classes = screen.getByTestId('m11-bottom-control-bar').className.split(/\s+/)

    expect(m11ControlBarHeightClass('64px')).toBe('h-16')
    expect(classes).toContain(m11ControlBarHeightClass(m11VisualTokens.timelineHeight))
    expect(classes).toContain('h-16')
    expect(classes).toContain('bottom-4')
  })

  it('lets the tick row replace the bottom metadata row so the rows still fit 64px', () => {
    // AC6 的行数预算代理断言（决策 10【Phase 2 后修订】）：jsdom 量不了像素，唯一能挡住
    // 「再加一行静默溢出」的结构性 oracle 是「传 cycle 时刻度行取代底行」。
    // 右侧列流高 = 20（当前时次行）+ 8+16（滑块）+ 4+16（刻度行）= 64px 整；
    // 首版（`h-3` 刻度行追加在底行之上）是 80px，固定 h-16 上下各溢出 8px 并压进版权归属带。
    const { unmount } = renderControlBar(inputFor(defaultM11QueryState))

    // 行数本身才是那条挡得住「再加一行」的 oracle（round-1 finding F）：只钉两个字符串时，
    // 任何既不含 `Analysis / Forecast` 也不含 sourceLabel 的新行都能悄悄插进来，流高照样
    // 回到 80px 而测试全绿。testid 而非 `div.min-w-0.flex-1`：Tailwind 类名是会漂的。
    expect(screen.getByTestId('m11-timeline-rows').children.length).toBe(3)
    expect(screen.queryByText('Analysis / Forecast')).toBeNull()
    // 但 sourceLabel 不许随底行一起丢（spec map-layer-timeline-controls「Timeline renders design
    // metadata」明写 MUST show the current data-source label）：它上移并入第一行。
    expect(screen.getByText('Discharge / /api/v1/layers/{layer_id}/valid-times / GFS')).toBeTruthy()
    // 去掉的只是那三个静态字：分界元素本身与它的 title 仍在。
    expect(screen.getByTitle('Analysis / Forecast')).toBeTruthy()
    unmount()

    // 不传 cycle 的默认路径：底行照旧在，刻度行不渲染（DOM 与今天逐字相同）。
    render(<M11Timeline state={defaultM11QueryState} layers={layersFor(defaultM11QueryState, dischargeMetadata())} onQueryChange={vi.fn()} />)
    expect(screen.getByTestId('m11-timeline-rows').children.length).toBe(3)
    expect(screen.getByText('Analysis / Forecast')).toBeTruthy()
    expect(screen.getByText('Discharge / /api/v1/layers/{layer_id}/valid-times / GFS')).toBeTruthy()
  })

  it('keeps the cycle selector usable when one cycle failed to resolve its valid times', async () => {
    // 决策 3【Phase 2 后修订】：起报时次 `<select>` 不跟聚合 `disabled` 一起禁 —— 某个周期
    // valid-times 取回失败是终态，禁掉 select 就废掉「从控制条切回别的周期」这条唯一自救路径。
    // issue 只要求零周期（`default_cycle === null`）时禁用，那条由上面的 fail-closed 用例守。
    const user = userEvent.setup()
    const onQueryChange = vi.fn()
    const query = { ...defaultM11QueryState, cycle: '2026-05-17T12:00:00.000Z' }
    const metadata = dischargeMetadata()
    const layers = layersFor(query, metadata, { discharge: { status: 'error' } })
    renderControlBar(inputFor(query, { metadata, layers }), onQueryChange)

    expect(screen.getByText(activeCycleValidTimesErrorDisabledReason)).toBeTruthy()
    expect((screen.getByLabelText('有效时间滑块') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('播放速度') as HTMLSelectElement).disabled).toBe(true)

    const select = screen.getByLabelText('起报时次') as HTMLSelectElement
    expect(select.disabled).toBe(false)
    await user.selectOptions(select, DEFAULT_CYCLE)
    expect(onQueryChange).toHaveBeenLastCalledWith({ cycle: DEFAULT_CYCLE })
  })
})

describe('M11Timeline', () => {
  const query = { ...defaultM11QueryState }
  const layers = layersFor(query, dischargeMetadata())

  it('keeps its default DOM byte-identical when no cycle is passed', () => {
    // AC2-c（第二条）：不传 `cycle` 时刻度行不存在，外壳类名仍是 M26 以来的字符串。
    render(<M11Timeline state={query} layers={layers} onQueryChange={vi.fn()} />)

    expect(screen.queryAllByTestId('m11-timeline-tick')).toHaveLength(0)
    expect(screen.queryByTestId('m11-timeline-ticks')).toBeNull()
    expect(screen.getByTestId('m11-timeline').className).toBe(
      'flex min-h-16 items-center gap-3 border-t border-neutral-300 bg-white px-4 text-sm xl:col-span-3',
    )
  })

  it('renders the Analysis / Forecast divider only when the caller passes the effective cycle', () => {
    // AC2-e：`sourceSelection: null` + `state.cycle: null` 是唯一让新 prop 承力的组合 ——
    // 任一非空时分界不管有没有新 prop 都会渲染，断言什么也不鉴别。
    expect(query.cycle).toBeNull()

    const { unmount } = render(<M11Timeline state={query} layers={layers} sourceSelection={null} cycle={VALID_TIMES[1]} onQueryChange={vi.fn()} />)
    const divider = screen.getByTitle('Analysis / Forecast') as HTMLElement
    const percent = Number.parseFloat(divider.style.left)
    expect(percent).toBeGreaterThan(0)
    expect(percent).toBeLessThan(100)
    unmount()

    render(<M11Timeline state={query} layers={layers} sourceSelection={null} onQueryChange={vi.fn()} />)
    expect(screen.queryByTitle('Analysis / Forecast')).toBeNull()
  })

  it('steps the valid time from the buttons and the slider', async () => {
    // AC5-b：零测试组件复活后的最低守护 —— 上一步 / 下一步 / 滑块各派发一次。
    const user = userEvent.setup()
    const onQueryChange = vi.fn()
    const picked = { ...query, validTime: MILLISECOND_VALID_TIMES[3] }
    render(<M11Timeline state={picked} layers={layersFor(picked, dischargeMetadata())} cycle={DEFAULT_CYCLE} onQueryChange={onQueryChange} />)

    await user.click(screen.getByLabelText('上一个有效时刻'))
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[2] })

    await user.click(screen.getByLabelText('下一个有效时刻'))
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[4] })

    const slider = screen.getByLabelText('有效时间滑块') as HTMLInputElement
    expect(slider.value).toBe('3')
    fireEvent.change(slider, { target: { value: '9' } })
    expect(onQueryChange).toHaveBeenLastCalledWith({ validTime: MILLISECOND_VALID_TIMES[9] })
  })
})
