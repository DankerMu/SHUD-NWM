import { useEffect, useMemo, useState } from 'react'
import {
  ChevronsLeft,
  ChevronsRight,
  CirclePause,
  CirclePlay,
  Info,
  Layers,
  Map as MapIcon,
  Satellite,
  SlidersHorizontal,
  Trees,
} from 'lucide-react'

import type { components } from '@/api/types'
import {
  M11MapLibreSurface,
  m11MapStyleUrls,
  type M11MapCameraFit,
  type M11MapCameraFlyTo,
  type M11MapOverlayInteraction,
  type M11StationFeatureCollection,
} from '@/components/map/M11MapLibreSurface'
import { cn } from '@/lib/cn'
import {
  getM11LayerLegend,
  m11WarningLevelColor,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
  type SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryPatch, M11QueryState, M11Source } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'

type QueryChangeHandler = (patch: M11QueryPatch) => void

export interface M11TimelineDerivedTimes {
  validTimes: string[]
  label: string
}

interface SharedControlProps {
  state: M11QueryState
  layers?: LayerState[]
  sourceSelection?: SourceScenarioSelectionState | null
  onQueryChange?: QueryChangeHandler
}

interface M11MapSurfaceProps extends SharedControlProps {
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  basinSegments?: BasinSegmentRow[]
  selectedSegmentId?: string | null
  selectedSegmentGeometry?: components['schemas']['GeoJsonLineString'] | null
  stationFeatureCollection?: M11StationFeatureCollection | null
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
}

const basemapOptions: Array<{ value: M11Basemap; label: string; icon: typeof MapIcon; styleUrl: string }> = [
  { value: 'terrain', label: '地形', icon: Trees, styleUrl: m11MapStyleUrls.terrain },
  { value: 'satellite', label: '卫星', icon: Satellite, styleUrl: m11MapStyleUrls.satellite },
  { value: 'vector', label: '矢量', icon: MapIcon, styleUrl: m11MapStyleUrls.vector },
]

const hydrologyLayers: Array<{ value: M11Layer; label: string; description: string }> = [
  { value: 'discharge', label: '河段径流', description: 'q_down / m3/s' },
  { value: 'water-level', label: '河段水位', description: 'stage，需图层 API 注册' },
  { value: 'flood-return-period', label: '洪水重现期', description: 'Return period' },
  { value: 'warning-level', label: '预警等级', description: 'Flood warning semantics' },
]

const sourceOptions: Array<{ value: M11Source; label: string; description: string }> = [
  { value: 'gfs', label: 'GFS', description: 'forecast_gfs_deterministic' },
  { value: 'ifs', label: 'IFS', description: 'forecast_ifs_deterministic' },
  { value: 'compare', label: 'GFS + IFS 对比', description: '需要两套可比序列' },
  { value: 'best', label: 'Best Available', description: 'URL 仅写入 source=best' },
]

const meteorologyPlaceholders = [
  ['precipitation-grid', '降水格点', '气象格点合同未在 M11 接入'],
  ['temperature-grid', '温度格点', '气象格点合同未在 M11 接入'],
] as const

const basePlaceholders = [
  ['basin-boundaries', '流域边界'],
  ['river-network', '河网'],
  ['dem', 'DEM'],
] as const

const fallbackLegends: Record<M11Layer, LayerState['legend']> = {
  discharge: getM11LayerLegend('discharge'),
  'water-level': getM11LayerLegend('water-level'),
  'flood-return-period': getM11LayerLegend('flood-return-period'),
  'warning-level': getM11LayerLegend('warning-level'),
  'met-stations': [],
}

const meteorologyLayers: Array<{ value: M11Layer; label: string; description: string }> = [
  { value: 'met-stations', label: '气象代站', description: '点位代站聚合图层 / clustered GeoJSON' },
]

export function M11MapSurface({
  state,
  layers = [],
  basins = [],
  visibleBasinIds,
  basinSegments = [],
  selectedSegmentId = null,
  selectedSegmentGeometry = null,
  stationFeatureCollection = null,
  onQueryChange,
  fitTo,
  flyTo,
  onOverlayHover,
  onOverlayClick,
}: M11MapSurfaceProps) {
  return (
    <>
      <M11MapLibreSurface
        state={state}
        layers={layers}
        basins={basins}
        visibleBasinIds={visibleBasinIds}
        basinSegments={basinSegments}
        selectedSegmentId={selectedSegmentId}
        selectedSegmentGeometry={selectedSegmentGeometry}
        stationFeatureCollection={stationFeatureCollection}
        fitTo={fitTo}
        flyTo={flyTo}
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />
      <div className="absolute right-5 top-5 z-[100] flex rounded-md border border-neutral-300 bg-white/95 p-1 shadow-md">
        {basemapOptions.map((option) => {
          const Icon = option.icon
          return (
            <button
              key={option.value}
              type="button"
              className={cn(
                'flex h-8 cursor-pointer items-center gap-1 rounded px-2 text-xs font-medium transition-colors',
                state.basemap === option.value ? 'bg-primary-600 text-white' : 'text-neutral-700 hover:bg-neutral-100',
              )}
              title={`${option.label}底图`}
              aria-label={`${option.label}底图`}
              aria-pressed={state.basemap === option.value}
              onClick={() => onQueryChange?.({ basemap: option.value })}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {option.label}
            </button>
          )
        })}
      </div>
    </>
  )
}

export function SourceScenarioControls({ state, sourceSelection, onQueryChange }: SharedControlProps) {
  const unavailable = sourceSelection?.unavailableReason
  const provenanceLabel = sourceSelection?.provenanceLabel ?? `${state.source.toUpperCase()} / latest cycle / current valid time`
  const compareLabel = sourceSelection?.comparisonAvailable ? '对比数据可用' : '对比数据不可用'

  return (
    <section className="space-y-2" aria-label="M11 数据源控制">
      <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
        <SlidersHorizontal className="h-4 w-4 text-primary-600" aria-hidden="true" />
        数据源与情景
      </div>
      <div className="grid gap-2">
        {sourceOptions.map((option) => (
          <button
            key={option.value}
            type="button"
            className={cn(
              'cursor-pointer rounded border px-3 py-2 text-left transition-colors',
              state.source === option.value
                ? 'border-primary-600 bg-primary-50 text-primary-700'
                : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50',
            )}
            aria-pressed={state.source === option.value}
            onClick={() => onQueryChange?.({ source: option.value })}
          >
            <span className="block text-sm font-medium">{option.label}</span>
            <span className="block text-xs text-neutral-700">{option.description}</span>
          </button>
        ))}
      </div>
      <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
        <div className="flex items-start gap-2">
          <Info className="mt-0.5 h-4 w-4 text-primary-600" aria-hidden="true" />
          <div>
            <div data-testid="m11-source-provenance">{state.source === 'compare' ? compareLabel : provenanceLabel}</div>
            {unavailable ? <div className="mt-1 text-warning">{unavailable}</div> : null}
          </div>
        </div>
      </div>
    </section>
  )
}

export function LayerGroupControls({ state, layers = [], onQueryChange }: SharedControlProps) {
  const layerById = useMemo(() => new Map(layers.map((layer) => [String(layer.layerId), layer])), [layers])

  return (
    <section className="space-y-4" aria-label="M11 图层控制">
      <LayerGroupTitle title="水文图层" />
      <div className="space-y-1">
        {hydrologyLayers.map((item) => {
          const layer = layerById.get(item.value)
          const selected = state.layer === item.value
          const unavailableReason = layer?.disabledReason ?? (!layer ? '等待 /api/v1/layers 图层注册状态' : null)
          const available = Boolean(layer?.available)
          const hasLayerMetadata = Boolean(layer)
          return (
            <button
              key={item.value}
              type="button"
              className={cn(
                'flex w-full cursor-pointer items-center justify-between rounded border px-3 py-2 text-left transition-colors',
                selected ? 'border-primary-600 bg-primary-50 text-primary-700' : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50',
              )}
              aria-pressed={selected}
              onClick={() => onQueryChange?.({ layer: item.value })}
            >
              <span>
                <span className="block text-sm font-medium">{item.label}</span>
                <span className="block text-xs text-neutral-700">{available ? item.description : unavailableReason}</span>
              </span>
              <span
                className={cn('h-2.5 w-2.5 rounded-full', available ? 'bg-success' : hasLayerMetadata ? 'bg-warning' : 'bg-neutral-300')}
                aria-hidden="true"
              />
            </button>
          )
        })}
      </div>

      <LayerGroupTitle title="气象图层" />
      <div className="space-y-1">
        {meteorologyLayers.map((item) => {
          const selected = state.layer === item.value
          return (
            <button
              key={item.value}
              type="button"
              className={cn(
                'flex w-full cursor-pointer items-center justify-between rounded border px-3 py-2 text-left transition-colors',
                selected ? 'border-primary-600 bg-primary-50 text-primary-700' : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50',
              )}
              aria-pressed={selected}
              onClick={() => onQueryChange?.({ layer: item.value })}
            >
              <span>
                <span className="block text-sm font-medium">{item.label}</span>
                <span className="block text-xs text-neutral-700">{item.description}</span>
              </span>
              <span className={cn('h-2.5 w-2.5 rounded-full', selected ? 'bg-success' : 'bg-neutral-300')} aria-hidden="true" />
            </button>
          )
        })}
        {meteorologyPlaceholders.map(([id, label, reason]) => (
          <UnavailableLayerRow key={id} label={label} reason={reason} />
        ))}
      </div>

      <LayerGroupTitle title="基础图层" />
      <div className="space-y-1">
        {basePlaceholders.map(([id, label]) => {
          const layer = layerById.get(id)
          return (
            <UnavailableLayerRow
              key={id}
              label={label}
              reason={id === 'dem' ? 'DEM 合同未在 M11 接入' : (layer?.disabledReason ?? '等待真实边界/河网图层数据')}
            />
          )
        })}
      </div>
    </section>
  )
}

export function LayerLegendPanel({ state, layers = [] }: SharedControlProps) {
  const activeLayer = layers.find((layer) => layer.layerId === state.layer)
  const entries = activeLayer?.legend.length ? activeLayer.legend : fallbackLegends[state.layer]
  const title =
    state.layer === 'discharge'
      ? '径流量图例'
      : state.layer === 'warning-level'
        ? '预警等级图例'
        : state.layer === 'flood-return-period'
          ? '重现期图例'
          : '水位图例'

  return (
    <section className="space-y-2" aria-label="M11 图例">
      <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
        <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
        {title}
      </div>
      {entries.length > 0 ? (
        <div className="space-y-1" data-testid="m11-layer-legend">
          {entries.map((entry) => (
            <div key={`${entry.label}-${entry.color}`} className="flex items-center justify-between gap-3 text-xs text-neutral-700">
              <span className="flex min-w-0 items-center gap-2">
                <span className="h-3 w-7 rounded-sm" style={{ backgroundColor: entry.color }} aria-hidden="true" />
                <span className="truncate">{entry.label}</span>
              </span>
              <span className="font-mono text-neutral-500">{formatLegendRange(entry.min, entry.max)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
          当前图层暂无图例合同。
        </div>
      )}
    </section>
  )
}

export function M11Timeline({
  state,
  layers = [],
  sourceSelection,
  derivedTimes,
  onQueryChange,
}: SharedControlProps & { derivedTimes?: M11TimelineDerivedTimes | null }) {
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const model = useMemo(
    () => buildM11TimelineViewModel(state, layers, derivedTimes ?? null, sourceSelection ?? null),
    [derivedTimes, layers, sourceSelection, state],
  )

  const disabled = model.validTimes.length === 0 || !onQueryChange
  const atFirst = model.currentIndex <= 0
  const atLast = model.currentIndex < 0 || model.currentIndex >= model.validTimes.length - 1

  useEffect(() => {
    if (!playing || disabled) return undefined
    if (atLast) {
      setPlaying(false)
      return undefined
    }
    const intervalId = window.setInterval(() => {
      const nextIndex = model.currentIndex + 1
      const nextValidTime = model.validTimes[nextIndex]
      if (!nextValidTime) {
        setPlaying(false)
        return
      }
      onQueryChange?.({ validTime: nextValidTime })
      if (nextIndex >= model.validTimes.length - 1) setPlaying(false)
    }, 1000 / speed)
    return () => window.clearInterval(intervalId)
  }, [atLast, disabled, model.currentIndex, model.validTimes, onQueryChange, playing, speed])

  useEffect(() => {
    if (disabled) setPlaying(false)
  }, [disabled])

  return (
    <section
      className="flex min-h-16 items-center gap-3 border-t border-neutral-300 bg-white px-4 text-sm xl:col-span-3"
      aria-label="M11 时间轴"
      data-testid="m11-timeline"
      data-valid-time-source={model.sourceKind}
      data-first-viewport-visible="true"
    >
      <div className="flex items-center gap-1">
        <button
          type="button"
          className="flex h-8 w-8 items-center justify-center rounded text-neutral-700 hover:bg-neutral-100 disabled:cursor-not-allowed disabled:text-neutral-500"
          aria-label="上一个有效时刻"
          disabled={disabled || atFirst}
          onClick={() => onQueryChange?.({ validTime: model.validTimes[model.currentIndex - 1] })}
        >
          <ChevronsLeft className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          className="flex h-8 w-8 items-center justify-center rounded text-neutral-700 hover:bg-neutral-100 disabled:cursor-not-allowed disabled:text-neutral-500"
          aria-label={playing ? '暂停时间轴' : '播放时间轴'}
          disabled={disabled || atLast}
          onClick={() => setPlaying((value) => !value)}
          title="播放到最后一个有效时刻后自动暂停并停留在最后一帧"
        >
          {playing ? <CirclePause className="h-4 w-4" aria-hidden="true" /> : <CirclePlay className="h-4 w-4" aria-hidden="true" />}
        </button>
        <button
          type="button"
          className="flex h-8 w-8 items-center justify-center rounded text-neutral-700 hover:bg-neutral-100 disabled:cursor-not-allowed disabled:text-neutral-500"
          aria-label="下一个有效时刻"
          disabled={disabled || atLast}
          onClick={() => onQueryChange?.({ validTime: model.validTimes[model.currentIndex + 1] })}
        >
          <ChevronsRight className="h-4 w-4" aria-hidden="true" />
        </button>
        <label className="ml-1 flex items-center gap-1 text-xs text-neutral-700">
          <span className="sr-only">播放速度</span>
          <select
            aria-label="播放速度"
            className="h-8 rounded border border-neutral-300 bg-white px-1 text-xs"
            value={speed}
            onChange={(event) => setSpeed(Number(event.target.value))}
          >
            <option value={1}>1x</option>
            <option value={2}>2x</option>
            <option value={4}>4x</option>
          </select>
        </label>
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3">
          <span className="truncate font-medium text-neutral-900">{model.currentValidTime ?? '当前图层没有有效时间'}</span>
          <span className="shrink-0 text-xs text-neutral-700">{model.nativeResolutionLabel}</span>
        </div>
        <div className="relative mt-2">
          <div className="absolute left-0 right-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-neutral-100" />
          {model.dividerPercent !== null ? (
            <div
              className="absolute top-1/2 h-6 -translate-y-1/2 border-l-2 border-dashed border-warning"
              style={{ left: `${model.dividerPercent}%` }}
              title="Analysis / Forecast"
              aria-hidden="true"
            />
          ) : null}
          <input
            aria-label="有效时间滑块"
            className="relative z-[100] h-4 w-full accent-primary-600 disabled:cursor-not-allowed"
            type="range"
            min={0}
            max={Math.max(model.validTimes.length - 1, 0)}
            step={1}
            value={Math.max(model.currentIndex, 0)}
            disabled={disabled}
            onChange={(event) => onQueryChange?.({ validTime: model.validTimes[Number(event.target.value)] })}
          />
        </div>
        <div className="mt-1 flex items-center justify-between gap-3 text-xs text-neutral-500">
          <span className="truncate">{model.sourceLabel}</span>
          <span>Analysis / Forecast</span>
        </div>
      </div>
    </section>
  )
}

function LayerGroupTitle({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
      <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
      {title}
    </div>
  )
}

function UnavailableLayerRow({ label, reason, available = false }: { label: string; reason: string; available?: boolean }) {
  return (
    <div
      className={cn(
        'flex items-center justify-between rounded border px-3 py-2 text-sm',
        available ? 'border-success/40 bg-white text-neutral-700' : 'border-neutral-300 bg-neutral-50 text-neutral-500',
      )}
    >
      <span>
        <span className="block font-medium">{label}</span>
        <span className="block text-xs">{available ? '已由图层 API 注册' : reason}</span>
      </span>
      <span className={cn('h-2.5 w-2.5 rounded-full', available ? 'bg-success' : 'bg-neutral-300')} aria-hidden="true" />
    </div>
  )
}

function formatLegendRange(min: number | null | undefined, max: number | null | undefined) {
  if (min === undefined && max === undefined) return ''
  if (min === null && max === null) return ''
  if (min === undefined || min === null) return `<${max}`
  if (max === undefined || max === null) return `>=${min}`
  return `${min}-${max}`
}

export function resolveM11ValidTimeCorrection(
  state: Pick<M11QueryState, 'layer' | 'validTime'>,
  layers: LayerState[],
  derivedTimes?: M11TimelineDerivedTimes | null,
): string | null | undefined {
  const activeLayer = layers.find((layer) => layer.layerId === state.layer)
  if (activeLayer) {
    if (activeLayer.validTimes.length === 0) return state.validTime ? null : undefined
    const current = normalizeIso(state.validTime)
    if (current && activeLayer.validTimes.includes(current)) return undefined
    const nextValidTime = activeLayer.currentValidTime ?? activeLayer.validTimes[activeLayer.validTimes.length - 1] ?? null
    return current === nextValidTime ? undefined : nextValidTime
  }

  const validTimes = normalizeValidTimes(derivedTimes?.validTimes)
  if (validTimes.length === 0) return undefined
  const current = normalizeIso(state.validTime)
  if (current && validTimes.includes(current)) return undefined
  return validTimes[validTimes.length - 1]
}

export function buildM11TimelineViewModel(
  state: Pick<M11QueryState, 'layer' | 'validTime' | 'cycle'>,
  layers: LayerState[],
  derivedTimes: M11TimelineDerivedTimes | null,
  sourceSelection: SourceScenarioSelectionState | null,
) {
  const activeLayer = layers.find((layer) => layer.layerId === state.layer)
  const usesLayer = Boolean(activeLayer)
  const validTimes = usesLayer ? activeLayer?.validTimes ?? [] : normalizeValidTimes(derivedTimes?.validTimes)
  const normalizedCurrent = normalizeIso(state.validTime)
  const currentValidTime =
    normalizedCurrent && validTimes.includes(normalizedCurrent)
      ? normalizedCurrent
      : usesLayer
        ? activeLayer?.currentValidTime ?? validTimes[validTimes.length - 1] ?? null
        : validTimes[validTimes.length - 1] ?? null
  const currentIndex = currentValidTime ? validTimes.indexOf(currentValidTime) : -1
  const cycle = normalizeIso(sourceSelection?.cycleTime ?? state.cycle)
  const dividerIndex = cycle ? validTimes.findIndex((validTime) => Date.parse(validTime) > Date.parse(cycle)) : -1
  const dividerPercent =
    dividerIndex > 0 && validTimes.length > 1 ? Math.round((dividerIndex / (validTimes.length - 1)) * 100) : null
  const nativeResolutionLabel = validTimes.length > 1 ? `${formatDuration(Date.parse(validTimes[1]) - Date.parse(validTimes[0]))} native` : 'native ticks'
  const sourceKind = usesLayer ? activeLayer?.validTimeSource ?? 'none' : validTimes.length > 0 ? 'derived' : 'none'
  const sourceLabel = usesLayer
    ? `${activeLayer?.displayName ?? state.layer} / ${validTimeSourceLabel(sourceKind)} / ${
        activeLayer?.freshness.source ?? sourceSelection?.resolvedSource ?? 'Unknown'
      }`
    : validTimes.length > 0
      ? `${derivedTimes?.label ?? 'payload-derived'} / derived`
      : 'no valid-time data'

  return {
    validTimes,
    currentValidTime,
    currentIndex,
    dividerPercent,
    nativeResolutionLabel,
    sourceKind,
    sourceLabel,
  }
}

function validTimeSourceLabel(source: string) {
  if (source === 'api') return '/api/v1/layers/{layer_id}/valid-times'
  if (source === 'derived') return 'payload-derived'
  return 'unavailable'
}

function normalizeValidTimes(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map(normalizeIso).filter((value): value is string => Boolean(value)))].sort(
    (a, b) => Date.parse(a) - Date.parse(b),
  )
}

function normalizeIso(value: string | null | undefined) {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}

function formatDuration(milliseconds: number) {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return 'native'
  const minutes = milliseconds / 60_000
  if (minutes % 1440 === 0) return `${minutes / 1440}d`
  if (minutes % 60 === 0) return `${minutes / 60}h`
  return `${minutes}m`
}

export { fallbackLegends as m11FallbackLegends, basemapOptions as m11BasemapOptions }

export const m11ControlColorEvidence = {
  warningMajor: m11VisualTokens.warningLevels.major,
  floodWarning: m11WarningLevelColor('warning'),
}
