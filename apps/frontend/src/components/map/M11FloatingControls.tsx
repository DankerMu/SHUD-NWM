import type { ReactNode } from 'react'
import { ArrowLeft, CloudRain, Droplets, Layers, Map as MapIcon, MapPin, Mountain, Satellite, Wrench, type LucideIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import type { components } from '@/api/types'
import { cn } from '@/lib/cn'
import { getM11LayerLegend, type LayerLegendEntry, type LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryPatch } from '@/lib/m11/queryState'

/** 降水图例条目：形状只认 API 契约（目录 `precip` 条目的 `metadata.legend`），前端不另立一份。 */
type PrecipLegendEntry = components['schemas']['PrecipLegendEntry']

// 玻璃质感容器：半透明 + backdrop-blur + 细描边 + 圆角 + 阴影。统一浮层外观。
export const GLASS_PANEL =
  'rounded-lg border border-white/40 bg-white/70 shadow-lg backdrop-blur-md supports-[backdrop-filter]:bg-white/55'

/** 浮层水文图层切换器可选项。 */
export interface M11FloatingLayerOption {
  value: M11Layer
  label: string
  description: string
  icon: LucideIcon
}

export const m11FloatingLayerOptions: M11FloatingLayerOption[] = [
  { value: 'discharge', label: '流量', description: 'q_down / m³/s', icon: Droplets },
]

/** 浮层图层行的共用外观：选中态高亮、禁用态置灰且不再有 hover 反馈。 */
function layerRowClassName({ selected, disabled }: { selected: boolean; disabled: boolean }) {
  return cn(
    'flex w-full items-center gap-2 rounded-md border px-2 py-2 text-left transition-colors',
    disabled
      ? 'cursor-not-allowed border-transparent text-neutral-400'
      : selected
        ? 'cursor-pointer border-primary-600 bg-primary-600/15 text-primary-700'
        : 'cursor-pointer border-transparent text-neutral-700 hover:bg-white/60',
  )
}

function LayerGroupTitle({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-2 px-1 pb-2 text-xs font-semibold text-neutral-900">
      <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
      {title}
    </div>
  )
}

/**
 * 浮层图层切换器（M26 单页全屏）。玻璃卡片浮在地图左上角，按「水文」/「气象」两组呈现
 * （spec map-layer-timeline-controls「Layer groups render」；base 组本单不交付，故不伪造）。
 *
 * `precipAvailable` 可选、默认 `false`：目录里没有 `precip` 条目时降水开关禁用并标「未实现」
 * （spec「Unimplemented meteorology layers are disabled」的诚实面）。这个判定由调用方**一处**
 * 从目录推出并传入（`OverviewMode`），组件内不重复 find；流域详情不传 → 恒禁用，直到 #2109 裁决。
 */
export function M11FloatingLayerSwitcher({
  layer,
  metStations = false,
  precip = false,
  precipAvailable = false,
  onQueryChange,
}: {
  layer: M11Layer
  metStations?: boolean
  precip?: boolean
  precipAvailable?: boolean
  onQueryChange?: (patch: M11QueryPatch) => void
}) {
  return (
    <section
      // 与右下图例同因：固定 w-52 会在右侧留下约三分之一死白（实测卡片 208px /
      // 内容只需 145.2px）。w-max 让宽度跟着最长的一行走，max-w-52 保住原上限。
      className={cn('absolute left-4 top-4 z-[120] w-max max-w-52 p-2', GLASS_PANEL)}
      aria-label="地图图层切换"
      data-testid="m11-floating-layer-switcher"
    >
      <div role="group" aria-label="水文" data-testid="m11-layer-group-hydrology">
        <LayerGroupTitle title="水文" />
        <div className="space-y-1">
          {m11FloatingLayerOptions.map((option) => {
            const Icon = option.icon
            const selected = layer === option.value
            return (
              <button
                key={option.value}
                type="button"
                className={layerRowClassName({ selected, disabled: false })}
                aria-pressed={selected}
                onClick={() => onQueryChange?.({ layer: option.value })}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="min-w-0">
                  <span className="block text-sm font-medium leading-tight">{option.label}</span>
                  <span className="block truncate text-xs text-neutral-600">{option.description}</span>
                </span>
              </button>
            )
          })}
        </div>
      </div>
      <div
        role="group"
        aria-label="气象"
        data-testid="m11-layer-group-meteorology"
        className="mt-2 border-t border-white/50 pt-2"
      >
        <LayerGroupTitle title="气象" />
        <div className="space-y-1">
          <button
            type="button"
            className={layerRowClassName({ selected: precipAvailable && precip, disabled: !precipAvailable })}
            // 目录没有 `precip` 条目时按下态恒为 false：一颗禁用却显示"已按下"的开关，
            // 正是 spec 明令禁止的"假装那些图层在渲染"。
            aria-pressed={precipAvailable ? precip : false}
            aria-disabled={!precipAvailable}
            disabled={!precipAvailable}
            title={precipAvailable ? undefined : '降水叠加未实现'}
            data-testid="m11-layer-toggle-precip"
            onClick={() => onQueryChange?.({ precip: !precip })}
          >
            <CloudRain className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0">
              <span className="block text-sm font-medium leading-tight">过去 24h 累积降水</span>
              <span className="block truncate text-xs text-neutral-600">
                {precipAvailable ? 'mm/24h 栅格叠加' : '未实现'}
              </span>
            </span>
          </button>
          <button
            type="button"
            className={layerRowClassName({ selected: metStations, disabled: false })}
            aria-pressed={metStations}
            onClick={() => onQueryChange?.({ metStations: !metStations })}
          >
            <MapPin className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0">
              <span className="block text-sm font-medium leading-tight">气象代站</span>
              <span className="block truncate text-xs text-neutral-600">点位代站叠加</span>
            </span>
          </button>
        </div>
      </div>
    </section>
  )
}

/** 浮层底图切换可选项：天地图 地形 / 卫星 / 矢量。 */
export const m11FloatingBasemapOptions: Array<{ value: M11Basemap; label: string; icon: typeof MapIcon }> = [
  { value: 'vector', label: '矢量', icon: MapIcon },
  { value: 'satellite', label: '卫星', icon: Satellite },
  { value: 'terrain', label: '地形', icon: Mountain },
]

/**
 * 浮层底图切换器（玻璃分段控件，浮在地图右上角缩放控件左侧）。
 * 写 queryState.basemap（URL 可分享）；三种底图均为天地图 WMTS（底图 + 中文注记）。
 */
export function M11FloatingBasemapSwitcher({
  basemap,
  onQueryChange,
}: {
  basemap: M11Basemap
  onQueryChange?: (patch: M11QueryPatch) => void
}) {
  return (
    <div
      className={cn('absolute right-16 top-4 z-[120] flex items-center gap-0.5 p-1', GLASS_PANEL)}
      role="group"
      aria-label="底图切换"
      data-testid="m11-floating-basemap-switcher"
    >
      {m11FloatingBasemapOptions.map((option) => {
        const Icon = option.icon
        const selected = basemap === option.value
        return (
          <button
            key={option.value}
            type="button"
            className={cn(
              'flex h-8 cursor-pointer items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors',
              selected ? 'bg-primary-600 text-white shadow-sm' : 'text-neutral-700 hover:bg-white/70',
            )}
            aria-pressed={selected}
            aria-label={`${option.label}底图`}
            onClick={() => onQueryChange?.({ basemap: option.value })}
          >
            <Icon className="h-3.5 w-3.5" aria-hidden="true" />
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

/** 浮层图例当前 active layer 的图例条目（复用 layers API 图例，回退到合同图例）。 */
export function resolveM11FloatingLegend(_layer: M11Layer, layers: LayerState[]): LayerLegendEntry[] {
  const activeLayer = layers.find((entry) => entry.layerId === 'discharge')
  if (activeLayer?.legend.length) return activeLayer.legend
  return getM11LayerLegend('discharge')
}

function legendTitle(_layer: M11Layer) {
  return '径流量图例'
}

/**
 * 降水图例段的标题。含单位 `mm/24h`（spec precipitation-raster-overlay
 * 「Legend shows both layers」要求图例自报单位，否则六级色阶读不出量纲）。
 */
const M11_PRECIP_LEGEND_TITLE = '过去 24h 累积降水（mm/24h）'

/**
 * 浮层图例（M26 单页全屏）。玻璃卡片浮在地图右下角，跟随 active layer 渲染图例。
 * 气象代站无图例合同 → honest 文案，不伪造色阶。
 *
 * `precipLegend` 可选、默认无：**唯一**来源是目录 `precip` 条目的 `metadata.legend`
 * （fixture 决策 7），由 `OverviewMode` 一处推出并传入；组件内零硬编码调色板与阈值，
 * 否则图例的六个 hex 会与 PNG 的 PLTE 漂移。流域详情不传 → 无降水图例段（blocked by #2109）。
 */
export function M11FloatingLegend({
  layer,
  layers,
  precipLegend,
}: {
  layer: M11Layer
  layers: LayerState[]
  precipLegend?: PrecipLegendEntry[] | null
}) {
  const entries = resolveM11FloatingLegend(layer, layers)
  const precipEntries = precipLegend?.length ? precipLegend : null

  return (
    <section
      // 宽度跟着最长的图例行走：固定 w-56 会在右侧留下约三分之一死白（实测内容区
      // 200px / 最宽行 137px）。max-w-56 保住原来的上限，未来出现更长的标签也不会
      // 把卡片撑过地图。
      //
      // bottom-24 而不是 bottom-12：底部控制条（`M11BottomControlBar`）自身 `bottom-4`
      // 且固定 `h-16`（64px，`m11VisualTokens.timelineHeight`），占据 16–80px 这一带；
      // bottom-12（48px）会被它压掉。96px = 80px 条顶 + 16px 间隙
      // （spec map-layer-timeline-controls「Floating controls clear the control bar」）。
      // 这也顺带继续避开离底 10px、高 24px 的 MapLibre 版权归属带——版权标注是瓦片供应商的
      // 硬要求，几何由 e2e/m11-overlay-collision.mocked.spec.ts 守住（它量矩形，不钉类名）。
      className={cn('absolute bottom-24 right-4 z-[120] w-max max-w-56 p-3', GLASS_PANEL)}
      aria-label="地图图例"
      data-testid="m11-floating-legend"
    >
      <div className="flex items-center gap-2 pb-2 text-xs font-semibold text-neutral-900">
        <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
        {legendTitle(layer)}
      </div>
      {entries.length > 0 ? (
        <div className="space-y-1" data-testid="m11-floating-legend-entries">
          {entries.map((entry) => (
            // label 已自带数值区间（如「500-1000 m³/s」），不再重复渲染右侧数字列。
            <div key={`${entry.label}-${entry.color}`} className="flex items-center gap-2 text-xs text-neutral-700">
              <span className="h-3 w-7 shrink-0 rounded-sm" style={{ backgroundColor: entry.color }} aria-hidden="true" />
              <span className="min-w-0 truncate">{entry.label}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-neutral-600" data-testid="m11-floating-legend-empty">
          当前图层暂无图例合同。
        </p>
      )}
      {precipEntries ? (
        // 降水段挂在流量段**之下、同一张卡片内**（spec「the legend panel shows the six-class
        // precipitation legend (mm/24h) beneath the discharge legend」）。并列第二张浮层卡片会
        // 与右下角这张重叠，故不另起 section。
        <div className="mt-3 border-t border-white/50 pt-2" data-testid="m11-floating-legend-precip">
          <div className="flex items-center gap-2 pb-2 text-xs font-semibold text-neutral-900">
            <CloudRain className="h-4 w-4 text-primary-600" aria-hidden="true" />
            {M11_PRECIP_LEGEND_TITLE}
          </div>
          <div className="space-y-1" data-testid="m11-floating-legend-precip-entries">
            {precipEntries.map((entry) => (
              // 色块颜色与阈值文字逐项来自 `legend[]`：label 已自带区间（如「0.1-10」「≥250」），
              // 前端不再从 min/max 另拼一套阈值文案——那就是第二份会漂的阈值来源。
              <div
                key={`${entry.label}-${entry.color}`}
                className="flex items-center gap-2 text-xs text-neutral-700"
                data-testid="m11-floating-legend-precip-row"
              >
                <span
                  className="h-3 w-7 shrink-0 rounded-sm"
                  style={{ backgroundColor: entry.color }}
                  data-testid="m11-floating-legend-precip-swatch"
                  aria-hidden="true"
                />
                <span className="min-w-0 truncate">{entry.label}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

/** 玻璃质感的返回总览按钮（详情模式浮在地图左下角）。 */
export function M11BackToOverviewButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className={cn(
        // bottom-24：与右下图例同因——底部控制条占 16–80px，96px = 条顶 + 16px 间隙。
        'absolute bottom-24 left-4 z-[120] flex items-center gap-2 px-3 py-2 text-sm font-medium text-primary-700 transition-colors hover:bg-white/70',
        GLASS_PANEL,
      )}
      onClick={onClick}
      data-testid="m11-back-to-overview"
    >
      <ArrowLeft className="h-4 w-4" aria-hidden="true" />
      返回总览
    </button>
  )
}

/** 低调运维直链（operator+ 可见），浮在地图右上角缩放控件下方。 */
export function M11OpsLink({ visible }: { visible: boolean }) {
  if (!visible) return null
  return (
    <Link
      to="/ops"
      className={cn(
        'absolute right-4 top-28 z-[120] flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-neutral-700 transition-colors hover:bg-white/70',
        GLASS_PANEL,
      )}
      data-testid="m11-ops-link"
    >
      <Wrench className="h-3.5 w-3.5" aria-hidden="true" />
      运维
    </Link>
  )
}

/** 浮层信息卡（地图标题/说明），玻璃质感，避免遮挡切换器（留在左上角下方）。 */
export function M11MapInfoCard({ title, meta }: { title: string; meta: string }) {
  return (
    <div className={cn('absolute left-4 top-[15.5rem] z-[110] max-w-sm px-3 py-2', GLASS_PANEL)}>
      <div className="text-sm font-semibold text-neutral-900">{title}</div>
      <p className="mt-1 text-xs leading-5 text-neutral-700">{meta}</p>
    </div>
  )
}

export function M11FloatingNotice({ children, testId }: { children: ReactNode; testId?: string }) {
  if (!children) return null
  return (
    <div
      className={cn(
        // bottom-40：与图例/返回按钮同因抬过 16–80px 的控制条，并保持提示条原先就比
        // 那两张卡片再高一层的相对关系（bottom-20 之于 bottom-12/bottom-4）。
        // 注意本值只保证越过控制条：卡片高度不定，居中提示与右下图例的水平交叠归 I15 的实机 receipt。
        'absolute left-1/2 bottom-40 z-[110] max-w-[min(30rem,calc(100%-8rem))] -translate-x-1/2 px-3 py-2 text-xs text-neutral-800',
        GLASS_PANEL,
      )}
      role="status"
      data-testid={testId}
    >
      {children}
    </div>
  )
}
