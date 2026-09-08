import type { components } from '@/api/types'
import { GLASS_PANEL } from '@/components/map/M11FloatingControls'
import { cn } from '@/lib/cn'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import {
  failClosedDischargeDisabledReason,
  resolveNationalScaleSource,
  type LayerState,
  type SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContracts'
import type { M11QueryPatch, M11QueryState, M11Source } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'
import { M11Timeline, m11LayerCatalogPendingDisabledReason, m11SourceOptions } from '@/pages/m11/M11Controls'
import type { DischargeCyclesState } from '@/stores/overviewData'

/**
 * 底部控制条的 props = 纯派生函数 `deriveM11ControlBarModel` 的返回类型（#2014 决策 3）。
 * `onQueryChange` 不在本类型里：它由 `M11FullscreenMap` 的现有 prop 提供。
 */
export interface M11BottomControlBarProps {
  /** 全国恒为 `[gfs, ifs]`（无 Best Available / 对比），文案复用 `m11SourceOptions`。 */
  sourceOptions: Array<{ value: M11Source; label: string; description: string }>
  source: M11Source
  /** 秒精度 RFC3339，最新在前（后端口径）。 */
  cycles: string[]
  /** 有效 cycle（秒精度）：见 `deriveM11ControlBarModel` 里钉死的回落次序；fail-closed 时为 null。 */
  cycle: string | null
  /**
   * 时次列表与聚合 `disabled` 都**不在**本类型里：`M11Timeline` 自己从 `layers` 派生
   * （`buildM11TimelineViewModel`），契约里再放一份只会描述一条不存在的数据流。
   * `disabledReason` 留下，因为它有独立的 DOM 消费者（控制条右端的理由文案）。
   */
  disabledReason: string | null
  // M11Timeline 直接消费的既有入参，原样透传
  state: M11QueryState
  layers: LayerState[]
  sourceSelection: SourceScenarioSelectionState | null
}

/** 只读快照入参：控制条不取数、不写 store，全部派生自 #2012 已建的 store 面。 */
export interface M11ControlBarInput {
  state: M11QueryState
  layers: LayerState[]
  /** 全国 discharge 的目录 metadata（`default_source` / `default_cycle` / `valid_times`）。 */
  metadata: components['schemas']['Layer']['metadata'] | null
  /** `useOverviewDataStore().cyclesBySource`：key 是**具体源**，`best` 永远查不到。 */
  cyclesBySource: Record<string, DischargeCyclesState>
  sourceSelection: SourceScenarioSelectionState | null
}

/** 全国源分段只有 GFS / IFS（spec map-layer-timeline-controls 的全国那半）。 */
const nationalSourceOptions = m11SourceOptions.filter((option) => option.value === 'gfs' || option.value === 'ifs')

/**
 * token → 高度类名。两个字面量都必须留在源码里（Tailwind v4 扫描源码产出类）；
 * 形参类型是 map 的 key，所以 `m11VisualTokens.timelineHeight` 改成未登记的值时 tsc 直接变红，
 * 改成 `'80px'` 时高度类名变 `h-20`、DOM 断言变红 —— 高度与 token 是真耦合，不是两条并列断言。
 */
const controlBarHeightClassByToken = { '64px': 'h-16', '80px': 'h-20' } as const

export function m11ControlBarHeightClass(token: keyof typeof controlBarHeightClassByToken): string {
  return controlBarHeightClassByToken[token]
}

const controlBarHeightClass = m11ControlBarHeightClass(m11VisualTokens.timelineHeight)

/** 控制条内的 `M11Timeline` 外壳：玻璃条里的一行，不要网格单元的白底/上边框。 */
const controlBarTimelineClassName = 'flex min-w-0 flex-1 items-center gap-3 text-sm'

/**
 * 底部控制条模型（#2014 决策 2）：cycles 来源与回落、有效 cycle、默认三元组、
 * `disabledReason` 全部在这里派生，展示组件只做 DOM。
 *
 * - 具体源身份**只**经 `resolveNationalScaleSource`（store 写 `cyclesBySource` 用的就是它）：
 *   直接拿 `state.source` 当 key 时 `best` 永远查不到。`compare` 既不在 `cyclesBySource` 里、
 *   也不是目录默认源 → 空 cycles + 空有效 cycle + 分段无选中态（地图侧本来就不注册 overlay，
 *   无数据错误）。
 * - cycles 尚未到达（enrichment 在 bootstrap 之后才发）或取回失败 → 回落 `[metadata.default_cycle]`，
 *   到达后扩展为端点全列表；两态之间已选 cycle 不跳变（有效 cycle 不从 cycles 列表**位置**推导）。
 *   **该回落只对目录声明的默认源合法**（finding C1）：`metadata.default_cycle` / `valid_times` /
 *   `default_source` 三者都是 GFS 专有事实（后端 `list_layers` 签名里没有 `source`），非默认源
 *   借它充数就是把一个 GFS 周期显示成 IFS 的选中周期。非默认源的 cycles 未到达时是
 *   `cycles: []` + `cycle: null` 的诚实空态。
 * - fail-closed 时 cycles 恒空：`cyclesBySource` 与目录 metadata 是两条独立的取数与缓存路径，
 *   「目录判 fail-closed」与「该源有非空 cycles」可以同时成立（下面 `failClosed` 处有详注）。
 * - 时次列表**不在**本模型里：`M11Timeline` 从 `layers` 自派生（`buildM11TimelineViewModel`），
 *   它已经保证「URL 时次不在当前列表时跟随 `LayerState.currentValidTime`」。
 */
export function deriveM11ControlBarModel(input: M11ControlBarInput): M11BottomControlBarProps {
  const source = resolveNationalScaleSource(input.state.source)
  const activeLayer = input.layers.find((layer) => layer.layerId === input.state.layer)
  // 目录的默认源身份与 store 侧 `nationalDischargeActivePair` 同一个表达式（identity 同源）；
  // 目录尚未落地（`metadata` 为 null）时**没有**默认源，故任何源都不得回落目录周期。
  const catalogDefaultSource = input.metadata ? input.metadata.default_source ?? 'gfs' : null
  const catalogDefaultCycle = source === catalogDefaultSource ? toSecondsPrecisionInstant(input.metadata?.default_cycle) : null
  const cyclesState = input.cyclesBySource[source]
  const sourceDefaultCycle =
    cyclesState?.status === 'available' ? toSecondsPrecisionInstant(cyclesState.cycles?.default_cycle) : null
  /**
   * fail-closed 闸口按**理由**走，不按 `defaultCycle === null` 走：后者在 bootstrap 之前
   * （`metadata` 为 null）同样成立，会把每次切源的过渡窗口也变成空且禁用的 select，
   * 违背决策 5 的「先回落、后扩展」。pending / error 两个过渡态的理由不等于该常量，
   * 故不受这道闸影响 —— 那是起报时次 `<select>` 唯一的自救通道。
   */
  const failClosed = activeLayer?.disabledReason === failClosedDischargeDisabledReason
  // `unwrapApiData` 是裸 `as T` 断言、零运行时校验：变形响应会带着 `cycles: undefined` 进来，
  // 不守卫则 `.map` 在 render 里抛，而这条路径上没有 error boundary（整页白屏）。
  const cycles = failClosed
    ? []
    : cyclesState?.status === 'available' && Array.isArray(cyclesState.cycles?.cycles)
      ? cyclesState.cycles.cycles
          .map((entry) => toSecondsPrecisionInstant(entry.cycle_time))
          .filter((entry): entry is string => entry !== null)
      : catalogDefaultCycle
        ? [catalogDefaultCycle]
        : []
  /**
   * 有效 cycle 的回落次序（决策 13 钉死）：URL → 该源自己声明的默认周期 → 目录默认周期
   * （**仅**当该源就是目录默认源）。默认源上第二段与第三段的先后是刻意的：目录 metadata 与
   * `/cycles` 是两条独立缓存路径（TTL 60s / stale 600s），默认源以目录为准才能保证
   * 「cycles 到达前后已选周期不跳变」，也才能与 store 侧的活动对同源（决策 13 要求默认源
   * 的 store 行为逐字不变，即只看目录）。非默认源目录里没有可用事实，只能读该源自己的。
   */
  const effectiveCycle = failClosed
    ? null
    : toSecondsPrecisionInstant(input.state.cycle) ??
      (source === catalogDefaultSource ? catalogDefaultCycle ?? sourceDefaultCycle : sourceDefaultCycle)
  // 图层目录尚未落地（`layers` 为空，bootstrap 之前的窗口）时复用 `LayerGroupControls` 的同一份
  // 文案常量（不再各写一份字面量）；有 `LayerState` 时一律透传它自己的 `disabledReason`。
  const disabledReason = activeLayer ? activeLayer.disabledReason : m11LayerCatalogPendingDisabledReason

  return {
    sourceOptions: nationalSourceOptions,
    source,
    // 有效 cycle 必须显式产出：全国默认态 `state.cycle === null`，不回落默认周期就
    // 既没有 lead 也没有 Analysis/Forecast 分界（决策 11）。fail-closed 时它与 `cycles` 一起
    // 归零：只闸 `cycles` 的话，展示层的 `cycleOptions` 会把 `?cycle=X` 原样塞回去，
    // select 在 fail-closed 横幅下依旧可用且有选中项，而地图侧拿到的是空的
    // `LayerState.activeNationalCycle`（store 的活动对为 null）——条显示 X、图零注册（finding C2）。
    cycles,
    cycle: effectiveCycle,
    disabledReason,
    state: input.state,
    layers: input.layers,
    sourceSelection: input.sourceSelection,
  }
}

/**
 * 底部玻璃控制条：GFS/IFS 分段 + 起报时次 `<select>` + 复用 `M11Timeline`（带 `+{lead}h` 刻度）。
 * 纯展示：三个控件都只写 URL（`onQueryChange`），不取数、不写 store。
 */
export function M11BottomControlBar({
  sourceOptions,
  source,
  cycles,
  cycle,
  disabledReason,
  state,
  layers,
  sourceSelection,
  onQueryChange,
}: M11BottomControlBarProps & { onQueryChange: (patch: M11QueryPatch) => void }) {
  // 手敲 `?cycle=` 或列表尚未覆盖该周期时，仍把活动周期显示成选中项，
  // 而不是让 `<select>` 显示一个它根本没选中的首项（`cycles` 本身保持纯粹，不掺 URL 值）。
  const cycleOptions = cycle && !cycles.includes(cycle) ? [cycle, ...cycles] : cycles

  return (
    <section
      className={cn(
        'absolute bottom-4 left-1/2 z-[115] flex w-[min(64rem,calc(100%-2rem))] -translate-x-1/2 items-center gap-3 px-3',
        controlBarHeightClass,
        GLASS_PANEL,
      )}
      aria-label="起报时次与时间轴"
      data-testid="m11-bottom-control-bar"
    >
      <div className="flex shrink-0 items-center gap-0.5" role="group" aria-label="预报源">
        {sourceOptions.map((option) => (
          <button
            key={option.value}
            type="button"
            className={cn(
              'flex h-8 cursor-pointer items-center rounded-md px-2.5 text-xs font-medium transition-colors',
              source === option.value ? 'bg-primary-600 text-white shadow-sm' : 'text-neutral-700 hover:bg-white/70',
            )}
            aria-pressed={source === option.value}
            title={option.description}
            // 切源同时清 `cycle`（#2014 决策 15）：`OverviewPage.handleQueryChange` 是
            // `{...state, ...patch}` 无跨字段重置，只发 `{ source }` 会让 GFS 上选定的周期在点
            // IFS 后原样存活 —— store 据此拼出 `(ifs, C_gfs)`（后端 200 + 空列表 → 假文案），
            // 上面的 `cycleOptions` 还会把它**前置**成 IFS 的选中项。`validTime` 不动：
            // `pickCurrentValidTime` 已处理「旧时次不在新列表内」。选中项本身是**幂等**的
            // （toggle 语义）：不加这道守卫，再点一次已选中的分段就会把用户在 `<select>` 里
            // 选好的周期清掉并触发一次整轮重载（round-4 finding R4-B）。
            onClick={() => {
              if (source !== option.value) onQueryChange({ source: option.value, cycle: null })
            }}
          >
            {option.label}
          </button>
        ))}
      </div>

      <label className="flex shrink-0 items-center gap-1">
        <span className="sr-only">起报时次</span>
        <select
          aria-label="起报时次"
          className="h-8 rounded border border-neutral-300 bg-white/80 px-1 text-xs disabled:cursor-not-allowed disabled:text-neutral-500"
          value={cycle ?? ''}
          // **不**按图层的聚合禁用位禁：issue 只要求零周期（`default_cycle === null`）时禁用，
          // 而那样会让「某周期 valid-times 取回失败」这个终态下用户无法从控制条切回别的周期
          // —— 恰好废掉这个控件唯一的自救用途。fail-closed 时 select 仍然是 disabled，这一点
          // **由构造保证**：`deriveM11ControlBarModel` 的 `failClosed` 闸口同时把 `cycles` 清空
          // **和**把 `cycle` 归 null（finding C2），于是 `cycleOptions` 无从被 `?cycle=X` 填回，
          // 恒为空。round-1 只闸了 `cycles`，那时这句话还是假的。
          disabled={cycleOptions.length === 0}
          onChange={(event) => onQueryChange({ cycle: event.target.value })}
        >
          {cycleOptions.length === 0 ? <option value="">无可用起报时次</option> : null}
          {cycleOptions.map((option) => (
            <option key={option} value={option} title={option}>
              {formatCycleLabel(option)}
            </option>
          ))}
        </select>
      </label>

      <M11Timeline
        className={controlBarTimelineClassName}
        cycle={cycle}
        state={state}
        layers={layers}
        sourceSelection={sourceSelection}
        onQueryChange={onQueryChange}
      />

      {disabledReason ? (
        <span
          className="max-w-48 shrink-0 truncate text-xs text-warning"
          title={disabledReason}
          data-testid="m11-control-bar-disabled-reason"
        >
          {disabledReason}
        </span>
      ) : null}
    </section>
  )
}

/**
 * 起报时次的紧凑显示（`MM-DD HHZ`）：纯字符串切片，不经 `Date` —— 本地时区会把 UTC 周期显示成别的钟点。
 * 12 天回溯窗口内 `MM-DD HH` 唯一；完整实例仍留在 `<option title>` 与 `value` 上。
 */
function formatCycleLabel(cycle: string): string {
  const match = /^\d{4}-(\d{2}-\d{2})T(\d{2}):/.exec(cycle)
  return match ? `${match[1]} ${match[2]}Z` : cycle
}
