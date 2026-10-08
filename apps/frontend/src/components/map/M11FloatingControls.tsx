import type { ReactNode } from 'react'
import { CloudRain, Droplets, Layers, Map as MapIcon, MapPin, Mountain, Palette, Satellite, Wrench, type LucideIcon } from 'lucide-react'
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

/**
 * 移动形态（openspec mobile-responsive-display D5）下三个浮层共用的启动器：44×44，
 * 排在地图区右上角的启动器列里；`aria-expanded` 报告它的面板是否展开。
 */
function M11OverlayLauncher({
  label,
  testId,
  icon: Icon,
  expanded,
  onToggle,
}: {
  label: string
  testId: string
  icon: LucideIcon
  expanded: boolean
  onToggle?: () => void
}) {
  return (
    <button
      type="button"
      className={cn(
        'flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center',
        expanded ? 'text-primary-700' : 'text-neutral-700',
        GLASS_PANEL,
      )}
      aria-label={label}
      aria-expanded={expanded}
      data-testid={testId}
      onClick={onToggle}
    >
      <Icon className="h-5 w-5" aria-hidden="true" />
    </button>
  )
}

/**
 * 移动形态展开面板的共用锚点：启动器列左侧、顶边与列顶对齐（定位祖先是启动器列）。
 * 面板自身只限高、不滚动；滚动容器是里层带独立 testid 的那一层。宽度上限由各面板自己给
 * （沿用桌面的 `max-w-*`，再按视口收一道，保证不伸出地图区左缘）。
 */
const MOBILE_PANEL_ANCHOR = 'absolute right-full top-0 mr-2 flex w-max flex-col overflow-hidden'
const MOBILE_PANEL_SCROLLER = 'min-h-0 overflow-y-auto overscroll-contain'

function mobilePanelStyle(panelMaxHeight: number | null) {
  return panelMaxHeight === null ? undefined : { maxHeight: panelMaxHeight }
}

/** 三个浮层共用的形态 prop：不传 = 桌面形态的常驻面板；后三个只在移动形态被读取。 */
interface M11OverlayFormProps {
  /** 移动形态（openspec mobile-responsive-display D5）：收成启动器，`expanded` 时才渲染面板。 */
  mobile?: boolean
  expanded?: boolean
  onToggle?: () => void
  /** 展开面板的高度上限（px），由地图外壳按地图区与控制条的实际几何量出；null = 不设上限。 */
  panelMaxHeight?: number | null
}

/**
 * 浮层图层行的共用外观：选中态高亮、禁用态置灰且不再有 hover 反馈。
 * `mobile` 只追加 44px 的触控下限，其余与桌面同一串类。
 */
function layerRowClassName({ selected, disabled, mobile }: { selected: boolean; disabled: boolean; mobile: boolean }) {
  return cn(
    'flex w-full items-center gap-2 rounded-md border px-2 py-2 text-left transition-colors',
    mobile && 'min-h-11',
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
 * 目录里有没有 `precip` 条目是**三值**事实，不是布尔（fixture #2015 决策 8，round-2 更正）：
 * 「目录还没到」与「目录到了且没有这一条」是两件事，塌成一个布尔就会在首屏/切源那一帧对用户
 * 发出一句它当时并没有证据支持的终态断言（「未实现」）。
 */
export type M11PrecipAvailability = 'unknown' | 'available' | 'absent'

/**
 * 浮层图层切换器（M26 单页全屏）。玻璃卡片浮在地图左上角，按「水文」/「气象」两组呈现
 * （spec map-layer-timeline-controls「Layer groups render」；base 组本单不交付，故不伪造）。
 *
 * `precipAvailability` 可选、默认 `'absent'`（「没告诉我」= 终态没有）：`'absent'` 时降水开关禁用
 * 并标「未实现」（spec「Unimplemented meteorology layers are disabled」的诚实面）；`'unknown'`
 * （= 调用方手上的目录数组为空：目录在途、或快照属上一 query）同样禁用，但只说「目录未就绪」：
 * 不说「加载中」——`bootstrapError` 且没有阶段 2 快照时目录数组恒为空，`'unknown'` 就是**终态**，「加载中」会把终态谎报成在途；「未就绪」在途与终态
 * 都为真，而硬失败本身由别处呈现。也不说「未实现」——那要正向证据（一份到手且不含 `precip` 条目
 * 的目录）。这个判定由调用方**一处**从目录推出并**显式**传入（`OverviewMode` 的 `precipCatalog`），
 * 组件内不重复 find。
 */
export function M11FloatingLayerSwitcher({
  layer,
  metStations = false,
  precip = false,
  precipAvailability = 'absent',
  onQueryChange,
  mobile = false,
  expanded = false,
  onToggle,
  panelMaxHeight = null,
}: M11LayerSwitcherContentProps & M11OverlayFormProps) {
  const content = (
    <M11LayerSwitcherContent
      layer={layer}
      metStations={metStations}
      precip={precip}
      precipAvailability={precipAvailability}
      onQueryChange={onQueryChange}
      mobile={mobile}
    />
  )

  if (mobile) {
    return (
      <>
        <M11OverlayLauncher label="图层" testId="m11-launcher-layers" icon={Layers} expanded={expanded} onToggle={onToggle} />
        {expanded ? (
          <section
            className={cn(MOBILE_PANEL_ANCHOR, 'max-w-[min(13rem,calc(100vw-5rem))]', GLASS_PANEL)}
            style={mobilePanelStyle(panelMaxHeight)}
            aria-label="地图图层切换"
            data-testid="m11-floating-layer-switcher"
          >
            <div className={cn(MOBILE_PANEL_SCROLLER, 'p-2')} data-testid="m11-floating-layer-switcher-scroll">
              {content}
            </div>
          </section>
        ) : null}
      </>
    )
  }

  return (
    <section
      // 与右下图例同因：固定 w-52 会在右侧留下约三分之一死白（实测卡片 208px /
      // 内容只需 145.2px）。w-max 让宽度跟着最长的一行走，max-w-52 保住原上限。
      className={cn('absolute left-4 top-4 z-[120] w-max max-w-52 p-2', GLASS_PANEL)}
      aria-label="地图图层切换"
      data-testid="m11-floating-layer-switcher"
    >
      {content}
    </section>
  )
}

interface M11LayerSwitcherContentProps {
  layer: M11Layer
  metStations?: boolean
  precip?: boolean
  precipAvailability?: M11PrecipAvailability
  onQueryChange?: (patch: M11QueryPatch) => void
}

/**
 * 图层面板的内容（「水文」「气象」两组及其开关）。桌面的常驻卡片与移动形态的展开面板共用这一份，
 * 不另写第二份行 / patch。返回 fragment：不给桌面卡片多加任何包裹元素。
 */
function M11LayerSwitcherContent({
  layer,
  metStations = false,
  precip = false,
  precipAvailability = 'absent',
  onQueryChange,
  mobile,
}: M11LayerSwitcherContentProps & { mobile: boolean }) {
  const precipEnabled = precipAvailability === 'available'
  return (
    <>
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
                className={layerRowClassName({ selected, disabled: false, mobile })}
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
            className={layerRowClassName({ selected: precipEnabled && precip, disabled: !precipEnabled, mobile })}
            // 目录没有 `precip` 条目（或目录还没到）时按下态恒为 false：一颗禁用却显示"已按下"
            // 的开关，正是 spec 明令禁止的"假装那些图层在渲染"。
            aria-pressed={precipEnabled ? precip : false}
            aria-disabled={!precipEnabled}
            disabled={!precipEnabled}
            // `'unknown'` 的 title 也不得出现「未实现」：那是终态断言，此刻还没有证据。
            title={precipEnabled ? undefined : precipAvailability === 'unknown' ? '降水图层目录未就绪' : '降水叠加未实现'}
            data-testid="m11-layer-toggle-precip"
            onClick={() => onQueryChange?.({ precip: !precip })}
          >
            <CloudRain className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0">
              <span className="block text-sm font-medium leading-tight">过去 24h 累积降水</span>
              <span className="block truncate text-xs text-neutral-600">
                {precipEnabled ? 'mm/24h 栅格叠加' : precipAvailability === 'unknown' ? '目录未就绪' : '未实现'}
              </span>
            </span>
          </button>
          <button
            type="button"
            className={layerRowClassName({ selected: metStations, disabled: false, mobile })}
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
    </>
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
  mobile = false,
  expanded = false,
  onToggle,
  panelMaxHeight = null,
}: M11BasemapSwitcherContentProps & M11OverlayFormProps) {
  const content = <M11BasemapSwitcherContent basemap={basemap} onQueryChange={onQueryChange} mobile={mobile} />

  if (mobile) {
    return (
      <>
        <M11OverlayLauncher label="底图" testId="m11-launcher-basemap" icon={MapIcon} expanded={expanded} onToggle={onToggle} />
        {expanded ? (
          <div
            className={cn(MOBILE_PANEL_ANCHOR, 'max-w-[calc(100vw-5rem)]', GLASS_PANEL)}
            style={mobilePanelStyle(panelMaxHeight)}
            role="group"
            aria-label="底图切换"
            data-testid="m11-floating-basemap-switcher"
          >
            {/* 与桌面同一排分段按钮；视口窄到放不下一排时换行，再超高则在这一层内滚动。 */}
            <div
              className={cn(MOBILE_PANEL_SCROLLER, 'flex flex-wrap items-center gap-0.5 p-1')}
              data-testid="m11-floating-basemap-switcher-scroll"
            >
              {content}
            </div>
          </div>
        ) : null}
      </>
    )
  }

  return (
    <div
      className={cn('absolute right-16 top-4 z-[120] flex items-center gap-0.5 p-1', GLASS_PANEL)}
      role="group"
      aria-label="底图切换"
      data-testid="m11-floating-basemap-switcher"
    >
      {content}
    </div>
  )
}

interface M11BasemapSwitcherContentProps {
  basemap: M11Basemap
  onQueryChange?: (patch: M11QueryPatch) => void
}

/**
 * 底图分段按钮。桌面的常驻控件与移动形态的展开面板共用这一份；`mobile` 只把按钮从 `h-8`
 * 换成 44×44 的触控下限，其余类、可访问名与 patch 相同。返回 fragment：桌面不多包裹元素。
 */
function M11BasemapSwitcherContent({ basemap, onQueryChange, mobile }: M11BasemapSwitcherContentProps & { mobile: boolean }) {
  return (
    <>
      {m11FloatingBasemapOptions.map((option) => {
        const Icon = option.icon
        const selected = basemap === option.value
        return (
          <button
            key={option.value}
            type="button"
            className={cn(
              'flex',
              mobile ? 'h-11 min-w-11 justify-center' : 'h-8',
              'cursor-pointer items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors',
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
    </>
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
 * 否则图例的六个 hex 会与 PNG 的 PLTE 漂移。不传 → 无降水图例段。
 */
export function M11FloatingLegend({
  layer,
  layers,
  precipLegend,
  mobile = false,
  expanded = false,
  onToggle,
  panelMaxHeight = null,
}: {
  layer: M11Layer
  layers: LayerState[]
  precipLegend?: PrecipLegendEntry[] | null
} & M11OverlayFormProps) {
  if (mobile) {
    return (
      <>
        <M11OverlayLauncher label="图例" testId="m11-launcher-legend" icon={Palette} expanded={expanded} onToggle={onToggle} />
        {expanded ? (
          // 宽度规则同桌面（`w-max max-w-56`），再按视口收一道上限，保证不伸出地图区左缘。
          <section
            className={cn(MOBILE_PANEL_ANCHOR, 'max-w-[min(14rem,calc(100vw-5rem))]', GLASS_PANEL)}
            style={mobilePanelStyle(panelMaxHeight)}
            aria-label="地图图例"
            data-testid="m11-floating-legend"
          >
            <div className={cn(MOBILE_PANEL_SCROLLER, 'p-3')} data-testid="m11-floating-legend-scroll">
              <M11LegendContent layer={layer} layers={layers} precipLegend={precipLegend} />
            </div>
          </section>
        ) : null}
      </>
    )
  }

  return (
    <section
      // 宽度跟着最长的图例行走：固定 w-56 会在右侧留下约三分之一死白（实测内容区
      // 200px / 最宽行 137px）。max-w-56 保住原来的上限，未来出现更长的标签也不会
      // 把卡片撑过地图。
      //
      // bottom-[7.5rem] 而不是 bottom-24：底部控制条（`M11BottomControlBar`）自身 `bottom-10`
      // 且固定 `h-16`（64px，`m11VisualTokens.timelineHeight`），占据 40–104px 这一带；
      // bottom-24（96px）会被它压掉。120px = 104px 条顶 + 16px 间隙
      // （spec map-layer-timeline-controls「Floating controls clear the control bar」）。
      // 这也顺带继续避开离底约 10px、高 24px 的 MapLibre 版权归属带——版权标注是瓦片供应商的
      // 硬要求，几何由 e2e/m11-overlay-collision.mocked.spec.ts 守住（它量矩形，不钉类名）。
      className={cn('absolute bottom-[7.5rem] right-4 z-[120] w-max max-w-56 p-3', GLASS_PANEL)}
      aria-label="地图图例"
      data-testid="m11-floating-legend"
    >
      <M11LegendContent layer={layer} layers={layers} precipLegend={precipLegend} />
    </section>
  )
}

/**
 * 图例内容（流量段 + 可选的降水段）。桌面的常驻面板与移动形态的展开面板共用这一份，
 * 不另写第二份图例。返回 fragment：不给桌面面板多加任何包裹元素。
 */
function M11LegendContent({
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
    <>
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
    </>
  )
}

/**
 * 低调运维直链（operator+ 可见）。桌面形态浮在地图右上角缩放控件下方；移动形态（`mobile`）下
 * 它是启动器列里的在流子项，用 `order-last` 排在图例启动器之下（列的视觉顺序：图层 / 底图 /
 * 图例 / 运维入口）。它比启动器宽，会把列撑宽，展开面板的锚点（列左缘）随之左移。
 */
export function M11OpsLink({ visible, mobile = false }: { visible: boolean; mobile?: boolean }) {
  if (!visible) return null
  return (
    <Link
      to="/ops"
      className={cn(
        mobile ? 'order-last shrink-0' : 'absolute right-4 top-28 z-[120]',
        'flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-neutral-700 transition-colors hover:bg-white/70',
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
        // bottom-[11.5rem]：与图例同因抬过 40–104px 的控制条，并保持提示条原先就比
        // 图例卡片再高一层的相对关系（bottom-20 之于 bottom-12；再整体 +24px）。
        // 注意本值只保证越过控制条：卡片高度不定，居中提示与右下图例的水平交叠归 I15 的实机 receipt。
        'absolute left-1/2 bottom-[11.5rem] z-[110] max-w-[min(30rem,calc(100%-8rem))] -translate-x-1/2 px-3 py-2 text-xs text-neutral-800',
        GLASS_PANEL,
        // 移动形态（mobile-responsive-display design.md D8）：地图区顶部条带的第一槽——左起 8px，
        // 右边界让出启动器列（44px 列宽 + 8px 右边距 + 8px 间隙 = 60px，故 max-w 为 100% - 68px），
        // 文本最多两行。两行时高 50px（16px 行高 × 2 + 16px 内边距 + 2px 边框），状态条容器
        // （m11MapRuntime 的 M11MapStatusOverlays）的顶边按这个高度恒定预留。
        'mobile:left-2 mobile:top-2 mobile:bottom-auto mobile:max-w-[calc(100%-4.25rem)] mobile:translate-x-0',
      )}
      role="status"
      data-testid={testId}
    >
      {/* 截断放在内层：直接截带内边距的外层，第三行会从下内边距里露出半行。桌面形态下它只是个普通块。 */}
      <div className="mobile:line-clamp-2">{children}</div>
    </div>
  )
}
