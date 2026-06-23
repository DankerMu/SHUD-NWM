import type { ReactNode } from 'react'
import { ArrowLeft, Droplets, Layers, Map as MapIcon, MapPin, Mountain, Satellite, Wrench, type LucideIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import { cn } from '@/lib/cn'
import { getM11LayerLegend, type LayerLegendEntry, type LayerState } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryPatch } from '@/lib/m11/queryState'

// 玻璃质感容器：半透明 + backdrop-blur + 细描边 + 圆角 + 阴影。统一浮层外观。
const GLASS_PANEL =
  'rounded-lg border border-white/40 bg-white/70 shadow-lg backdrop-blur-md supports-[backdrop-filter]:bg-white/55'

/** 浮层水文图层切换器可选项。 */
export interface M11FloatingLayerOption {
  value: M11Layer
  label: string
  description: string
  icon: LucideIcon
}

export const m11FloatingLayerOptions: M11FloatingLayerOption[] = [
  { value: 'discharge', label: '流量', description: 'q_down / m3/s', icon: Droplets },
  { value: 'flood-return-period', label: '重现期', description: 'Return period', icon: Layers },
  { value: 'warning-level', label: '预警等级', description: 'Warning level', icon: Layers },
]

/**
 * 浮层图层切换器（M26 单页全屏）。玻璃卡片浮在地图左上角。
 */
export function M11FloatingLayerSwitcher({
  layer,
  metStations = false,
  onQueryChange,
}: {
  layer: M11Layer
  metStations?: boolean
  onQueryChange?: (patch: M11QueryPatch) => void
}) {
  return (
    <section
      className={cn('absolute left-4 top-4 z-[120] w-52 p-2', GLASS_PANEL)}
      aria-label="地图图层切换"
      data-testid="m11-floating-layer-switcher"
    >
      <div className="flex items-center gap-2 px-1 pb-2 text-xs font-semibold text-neutral-900">
        <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
        水文图层
      </div>
      <div className="space-y-1">
        {m11FloatingLayerOptions.map((option) => {
          const Icon = option.icon
          const selected = layer === option.value
          return (
            <button
              key={option.value}
              type="button"
              className={cn(
                'flex w-full cursor-pointer items-center gap-2 rounded-md border px-2 py-2 text-left transition-colors',
                selected
                  ? 'border-primary-600 bg-primary-600/15 text-primary-700'
                  : 'border-transparent text-neutral-700 hover:bg-white/60',
              )}
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
      <div className="mt-2 border-t border-white/50 pt-2">
        <button
          type="button"
          className={cn(
            'flex w-full cursor-pointer items-center gap-2 rounded-md border px-2 py-2 text-left transition-colors',
            metStations
              ? 'border-primary-600 bg-primary-600/15 text-primary-700'
              : 'border-transparent text-neutral-700 hover:bg-white/60',
          )}
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
export function resolveM11FloatingLegend(layer: M11Layer, layers: LayerState[]): LayerLegendEntry[] {
  const activeLayer = layers.find((entry) => entry.layerId === layer)
  if (activeLayer?.legend.length) return activeLayer.legend
  return getM11LayerLegend(layer)
}

function legendTitle(layer: M11Layer) {
  if (layer === 'warning-level') return '预警等级图例'
  if (layer === 'flood-return-period') return '重现期图例'
  return '径流量图例'
}

/**
 * 浮层图例（M26 单页全屏）。玻璃卡片浮在地图右下角，跟随 active layer 渲染图例。
 * 气象代站无图例合同 → honest 文案，不伪造色阶。
 */
export function M11FloatingLegend({ layer, layers }: { layer: M11Layer; layers: LayerState[] }) {
  const entries = resolveM11FloatingLegend(layer, layers)

  return (
    <section
      className={cn('absolute bottom-4 right-4 z-[120] w-56 p-3', GLASS_PANEL)}
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
            // label 已自带数值区间（如「500-1000 m3/s」），不再重复渲染右侧数字列。
            <div key={`${entry.label}-${entry.color}`} className="flex items-center gap-2 text-xs text-neutral-700">
              <span className="h-3 w-7 shrink-0 rounded-sm" style={{ backgroundColor: entry.color }} aria-hidden="true" />
              <span className="truncate">{entry.label}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-neutral-600" data-testid="m11-floating-legend-empty">
          当前图层暂无图例合同。
        </p>
      )}
    </section>
  )
}

/** 玻璃质感的返回总览按钮（详情模式浮在地图左下角）。 */
export function M11BackToOverviewButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className={cn(
        'absolute bottom-4 left-4 z-[120] flex items-center gap-2 px-3 py-2 text-sm font-medium text-primary-700 transition-colors hover:bg-white/70',
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
        'absolute left-1/2 bottom-20 z-[110] max-w-[min(30rem,calc(100%-8rem))] -translate-x-1/2 px-3 py-2 text-xs text-neutral-800',
        GLASS_PANEL,
      )}
      role="status"
      data-testid={testId}
    >
      {children}
    </div>
  )
}
