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
  /** 有效 cycle（秒精度）：`state.cycle ?? metadata.default_cycle`。 */
  cycle: string | null
  /**
   * 聚合禁用位，透传给 `M11Timeline` 的可选 `disabled`（起报时次 `<select>` **不**并入，见下）。
   * 时次列表本身不在本类型里：`M11Timeline` 自己从 `layers` 派生（`buildM11TimelineViewModel`），
   * 契约里再放一份 `validTimes` / `validTime` 只会描述一条不存在的数据流。
   */
  disabled: boolean
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
 * disabled / disabledReason 全部在这里派生，展示组件只做 DOM。
 *
 * - 具体源身份**只**经 `resolveNationalScaleSource`（store 写 `cyclesBySource` 用的就是它）：
 *   直接拿 `state.source` 当 key 时 `best` 永远查不到。`compare` 恒缺席 → 回落单项 default_cycle、
 *   分段无选中态（地图侧本来就不注册 overlay，无数据错误）。
 * - cycles 尚未到达（enrichment 在 bootstrap 之后才发）或取回失败 → 回落 `[metadata.default_cycle]`，
 *   到达后扩展为端点全列表；两态之间已选 cycle 不跳变（有效 cycle 不从 cycles 列表推导）。
 * - fail-closed 时 cycles 恒空：`cyclesBySource` 与目录 metadata 是两条独立的取数与缓存路径，
 *   「目录判 fail-closed」与「该源有非空 cycles」可以同时成立（下面 `failClosed` 处有详注）。
 * - 时次列表**不在**本模型里：`M11Timeline` 从 `layers` 自派生（`buildM11TimelineViewModel`），
 *   它已经保证「URL 时次不在当前列表时跟随 `LayerState.currentValidTime`」。
 */
export function deriveM11ControlBarModel(input: M11ControlBarInput): M11BottomControlBarProps {
  const source = resolveNationalScaleSource(input.state.source)
  const activeLayer = input.layers.find((layer) => layer.layerId === input.state.layer)
  const validTimes = activeLayer?.validTimes ?? []
  const defaultCycle = toSecondsPrecisionInstant(input.metadata?.default_cycle)
  const cyclesState = input.cyclesBySource[source]
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
      : defaultCycle
        ? [defaultCycle]
        : []
  // 图层目录尚未落地（`layers` 为空，bootstrap 之前的窗口）时复用 `LayerGroupControls` 的同一份
  // 文案常量（不再各写一份字面量）；有 `LayerState` 时一律透传它自己的 `disabledReason`。
  const disabledReason = activeLayer ? activeLayer.disabledReason : m11LayerCatalogPendingDisabledReason

  return {
    sourceOptions: nationalSourceOptions,
    source,
    cycles,
    // 有效 cycle 必须显式产出：全国默认态 `state.cycle === null`，不回落 default_cycle 就
    // 既没有 lead 也没有 Analysis/Forecast 分界（决策 11）。
    cycle: toSecondsPrecisionInstant(input.state.cycle ?? input.metadata?.default_cycle),
    disabled: validTimes.length === 0 || disabledReason !== null,
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
  disabled,
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
            onClick={() => onQueryChange({ source: option.value })}
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
          // **不**跟聚合 `disabled` 一起禁：issue 只要求零周期（`default_cycle === null`）时禁用，
          // 而按聚合布尔禁 select 会让「某周期 valid-times 取回失败」这个终态下用户无法从控制条
          // 切回别的周期 —— 恰好废掉这个控件唯一的自救用途。fail-closed 时 select 仍是 disabled，
          // 这一点自 round-1 finding A 起**由构造保证**（`deriveM11ControlBarModel` 的 `failClosed`
          // 闸口让 cycles 恒空），不再是「碰巧 cyclesBySource 也没数据」的巧合。
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
        disabled={disabled}
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
