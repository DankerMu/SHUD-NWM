import { useEffect, useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  BarChart3,
  CloudRain,
  Crosshair,
  Layers,
  MapPinned,
  Search,
  SlidersHorizontal,
  Thermometer,
} from 'lucide-react'

import { echarts } from '@/components/charts/echartsCore'
import { cn } from '@/lib/cn'
import {
  formatBbox,
  meteorologyGridContractVersion,
  meteorologyStationContractVersion,
  stationInventoryLimits,
  variableMetadata,
  type MeteorologyStationSeriesVariable,
} from '@/lib/meteorology/contracts'
import {
  buildMeteorologyGridViewModel,
  buildStationInventoryViewModel,
  resolveMeteorologyValidTimeCorrection,
} from '@/lib/meteorology/viewModels'
import {
  defaultMeteorologyQueryState,
  mergeMeteorologyQueryState,
  meteorologySources,
  meteorologyTabs,
  meteorologyVariables,
  needsMeteorologyQueryReplacement,
  parseMeteorologyQueryState,
  serializeMeteorologyQueryState,
  type MeteorologyQueryPatch,
  type MeteorologyQueryState,
} from '@/lib/meteorology/queryState'

export function MeteorologyPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseMeteorologyQueryState(location.search), [location.search])

  useEffect(() => {
    if (!needsMeteorologyQueryReplacement(location.search)) return
    navigate({ pathname: '/meteorology', search: serializeMeteorologyQueryState(state) }, { replace: true })
  }, [location.search, navigate, state])

  const updateState = (patch: MeteorologyQueryPatch) => {
    const next = mergeMeteorologyQueryState(state, patch)
    navigate({ pathname: '/meteorology', search: serializeMeteorologyQueryState(next) })
  }

  return (
    <div className="space-y-3" data-testid="meteorology-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-primary-900">气象数据产品</h1>
          <p className="text-sm text-neutral-700">
            合同版本 {meteorologyGridContractVersion} / {meteorologyStationContractVersion}
          </p>
        </div>
        <div className="flex rounded-md border border-neutral-300 bg-white p-1" role="tablist" aria-label="气象产品标签">
          {meteorologyTabs.map((tab) => (
            <Link
              key={tab}
              to={`/meteorology?${serializeMeteorologyQueryState({ ...state, tab })}`}
              className={cn(
                'flex h-9 items-center gap-2 rounded px-3 text-sm font-medium transition-colors',
                state.tab === tab ? 'bg-primary-600 text-white' : 'text-neutral-700 hover:bg-neutral-100',
              )}
              role="tab"
              aria-selected={state.tab === tab}
            >
              {tab === 'grid' ? <Layers className="h-4 w-4" aria-hidden="true" /> : <MapPinned className="h-4 w-4" aria-hidden="true" />}
              {tab === 'grid' ? '空间栅格' : '气象代站'}
            </Link>
          ))}
        </div>
      </div>

      {state.tab === 'grid' ? <MeteorologyGridTab state={state} onQueryChange={updateState} /> : <MeteorologyStationsTab state={state} onQueryChange={updateState} />}
    </div>
  )
}

function MeteorologyGridTab({ state, onQueryChange }: { state: MeteorologyQueryState; onQueryChange: (patch: MeteorologyQueryPatch) => void }) {
  const model = useMemo(() => buildMeteorologyGridViewModel(state), [state])
  const contract = model.contract

  useEffect(() => {
    const correction = resolveMeteorologyValidTimeCorrection(state.validTime, contract)
    if (correction !== undefined && correction !== state.validTime) {
      onQueryChange({ validTime: correction })
    }
  }, [contract, onQueryChange, state.validTime])

  return (
    <div className="grid min-h-[calc(100vh-146px)] overflow-hidden rounded-md border border-neutral-300 bg-white shadow-md min-[1180px]:grid-cols-[280px_minmax(0,1fr)_340px] min-[1180px]:grid-rows-[minmax(0,1fr)_72px]">
      <aside className="min-h-0 overflow-auto border-b border-neutral-300 p-4 min-[1180px]:border-b-0 min-[1180px]:border-r">
        <PanelTitle icon={SlidersHorizontal} title="栅格控制" />
        <ControlLabel label="变量" />
        <div className="grid grid-cols-2 gap-2">
          {meteorologyVariables.map((variable) => (
            <button
              key={variable}
              type="button"
              className={optionClass(state.variable === variable)}
              onClick={() => onQueryChange({ variable, validTime: null })}
            >
              <span className="font-medium">{variable}</span>
              <span className="text-xs text-neutral-700">{variableMetadata[variable].unit}</span>
            </button>
          ))}
        </div>

        <ControlLabel label="数据源" />
        <div className="space-y-2">
          {meteorologySources.map((source) => (
            <button
              key={source}
              type="button"
              className={optionClass(state.source === source)}
              onClick={() => onQueryChange({ source, validTime: null, compareSource: null })}
            >
              <span className="font-medium">{source}</span>
              <span className="text-xs text-neutral-700">{source === 'CLDAS' ? 'restricted' : 'metadata contract'}</span>
            </button>
          ))}
        </div>

        <ControlLabel label="图层选项" />
        <label className="block text-xs font-medium text-neutral-700">
          透明度 {state.opacity}%
          <input
            className="mt-2 w-full accent-primary-600"
            type="range"
            min={10}
            max={100}
            value={state.opacity}
            onChange={(event) => onQueryChange({ opacity: Number(event.target.value) })}
          />
        </label>
        <ToggleRow label="等值线" checked={state.contours} disabled={!contract.supportsContours} onChange={(value) => onQueryChange({ contours: value })} />
        <ToggleRow label="站点叠加" checked={state.stationOverlay} onChange={(value) => onQueryChange({ stationOverlay: value })} />
      </aside>

      <section className="relative min-h-[32rem] overflow-hidden bg-[#d7e7ef]" aria-label="气象栅格地图">
        <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(15,52,96,0.08)_1px,transparent_1px),linear-gradient(rgba(15,52,96,0.08)_1px,transparent_1px)] bg-[length:42px_42px]" />
        <div className="absolute left-8 top-8 z-10 rounded-md border border-neutral-300 bg-white/95 p-4 shadow-lg">
          <div className="flex items-center gap-2 text-base font-semibold text-neutral-900">
            <CloudRain className="h-5 w-5 text-primary-600" aria-hidden="true" />
            {contract.displayName} / {contract.source}
          </div>
          <p className="mt-1 max-w-md text-sm text-neutral-700">
            {contract.unit} · {formatBbox(contract.bbox)} · {contract.spatialResolution}
          </p>
        </div>
        {state.stationOverlay ? (
          <div className="absolute left-[56%] top-[52%] z-10 h-3 w-3 rounded-full border-2 border-white bg-warning shadow" title="HMT-Y2-0236" data-testid="grid-station-overlay" />
        ) : null}
        {model.cellPopup ? (
          <div className="absolute left-[58%] top-[38%] z-10 w-80 rounded-md border border-neutral-300 bg-white/95 p-3 text-xs text-neutral-700 shadow-lg" data-testid="grid-cell-popup">
            <div className="font-semibold text-neutral-900">格点查询</div>
            <dl className="mt-2 grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-2 gap-y-1">
              <dt>位置</dt>
              <dd>{model.cellPopup.lon.toFixed(2)}E, {model.cellPopup.lat.toFixed(2)}N</dd>
              <dt>有效时间</dt>
              <dd className="font-mono">{state.validTime ?? contract.currentValidTime ?? '-'}</dd>
              <dt>变量</dt>
              <dd>{contract.variable} / {contract.unit}</dd>
              <dt>数值</dt>
              <dd>{model.cellPopup.reason}</dd>
            </dl>
          </div>
        ) : null}
        <UnavailableOverlay reason={contract.restrictedReason ?? contract.unavailableReason ?? '栅格产品不可用'} testId="grid-unavailable" />
      </section>

      <aside className="min-h-0 overflow-auto border-t border-neutral-300 p-4 min-[1180px]:border-l min-[1180px]:border-t-0">
        <PanelTitle icon={BarChart3} title="合同与分析" />
        <MetadataList
          rows={[
            ['单位', contract.unit],
            ['bbox', formatBbox(contract.bbox)],
            ['分辨率', contract.spatialResolution],
            ['周期', contract.cycleTime ?? 'restricted/unavailable'],
            ['有效时间', state.validTime ?? contract.currentValidTime ?? '无'],
            ['native', contract.nativeTimeResolution],
            ['tile', contract.tileUrlTemplate ?? 'restricted/unavailable'],
            ['query', contract.queryUrlTemplate ?? 'restricted/unavailable'],
          ]}
        />
        {contract.restrictedReason ? <StatusBox tone="warning" text={contract.restrictedReason} testId="cldas-restricted" /> : null}
        <ControlLabel label="图例" />
        <div className="space-y-1" data-testid="grid-legend">
          {contract.legend.map((entry) => (
            <div key={entry.label} className="flex items-center justify-between gap-3 text-xs text-neutral-700">
              <span className="flex min-w-0 items-center gap-2">
                <span className="h-3 w-7 rounded-sm" style={{ backgroundColor: entry.color }} />
                <span>{entry.label}</span>
              </span>
              <span className="font-mono">{contract.unit}</span>
            </div>
          ))}
        </div>
        <ControlLabel label="多源对比" />
        <select
          className="h-9 w-full rounded border border-neutral-300 bg-white px-2 text-sm"
          value={state.compareSource ?? ''}
          onChange={(event) => onQueryChange({ compareSource: event.target.value || null })}
          aria-label="对比数据源"
        >
          <option value="">未选择</option>
          {meteorologySources.map((source) => (
            <option key={source} value={source}>{source}</option>
          ))}
        </select>
        <StatusBox tone={model.comparisonStatus.includes('不支持') || model.comparisonStatus.includes('缺少') ? 'warning' : 'info'} text={model.comparisonStatus} testId="comparison-status" />
        <StatusBox tone="info" text={model.areaStatsStatus} testId="area-stats-status" />
      </aside>

      <GridTimeline state={state} model={model} onQueryChange={onQueryChange} />
    </div>
  )
}

function GridTimeline({
  state,
  model,
  onQueryChange,
}: {
  state: MeteorologyQueryState
  model: ReturnType<typeof buildMeteorologyGridViewModel>
  onQueryChange: (patch: MeteorologyQueryPatch) => void
}) {
  const contract = model.contract
  const currentIndex = state.validTime ? contract.validTimes.indexOf(state.validTime) : -1
  const disabled = contract.validTimes.length === 0
  return (
    <section className="flex min-h-[72px] items-center gap-3 border-t border-neutral-300 bg-white px-4 min-[1180px]:col-span-3" data-testid="grid-timeline">
      <Crosshair className="h-4 w-4 text-primary-600" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3 text-sm">
          <span className="truncate font-medium text-neutral-900">{disabled ? model.timelineDisabledReason : state.validTime ?? contract.currentValidTime}</span>
          <span className="shrink-0 text-xs text-neutral-700">{contract.nativeTimeResolution}</span>
        </div>
        <input
          aria-label="气象有效时间"
          className="mt-2 w-full accent-primary-600 disabled:cursor-not-allowed"
          type="range"
          min={0}
          max={Math.max(contract.validTimes.length - 1, 0)}
          value={Math.max(currentIndex, 0)}
          disabled={disabled}
          onChange={(event) => onQueryChange({ validTime: contract.validTimes[Number(event.target.value)] })}
        />
      </div>
    </section>
  )
}

function MeteorologyStationsTab({ state, onQueryChange }: { state: MeteorologyQueryState; onQueryChange: (patch: MeteorologyQueryPatch) => void }) {
  const model = useMemo(() => buildStationInventoryViewModel(state), [state])
  const selected = model.selectedStation
  const adjacentIds = new Set(selected?.adjacent.map((item) => item.stationId) ?? [])

  useEffect(() => {
    if (state.stationId && !model.rows.some((row) => row.stationId === state.stationId)) {
      onQueryChange({ stationId: null })
    }
  }, [model.rows, onQueryChange, state.stationId])

  return (
    <div className="grid min-h-[calc(100vh-146px)] overflow-hidden rounded-md border border-neutral-300 bg-white shadow-md min-[1180px]:grid-cols-[320px_minmax(0,1fr)_380px]">
      <aside className="min-h-0 overflow-auto border-b border-neutral-300 p-4 min-[1180px]:border-b-0 min-[1180px]:border-r">
        <PanelTitle icon={Search} title="站点检索" />
        <ControlLabel label="流域" />
        <select
          className="h-9 w-full rounded border border-neutral-300 bg-white px-2 text-sm"
          value={state.basin ?? ''}
          aria-label="流域"
          onChange={(event) => onQueryChange({ basin: event.target.value || null, stationId: null })}
        >
          <option value="">全部流域</option>
          <option value="yangtze">长江流域</option>
          <option value="hanjiang">汉江流域</option>
        </select>
        <ControlLabel label="搜索" />
        <input
          className="h-9 w-full rounded border border-neutral-300 px-2 text-sm"
          value={state.search ?? ''}
          maxLength={stationInventoryLimits.searchMaxLength}
          placeholder="station_id / 名称"
          onChange={(event) => onQueryChange({ search: event.target.value || null, stationId: null })}
        />
        <ControlLabel label="排序" />
        <select className="h-9 w-full rounded border border-neutral-300 bg-white px-2 text-sm" value={state.sort} onChange={(event) => onQueryChange({ sort: event.target.value })}>
          <option value="latest">最新数据时间</option>
          <option value="completeness">完整度</option>
          <option value="station_id">站点 ID</option>
        </select>
        {model.validationReason ? <StatusBox tone="warning" text={model.validationReason} /> : null}
        {model.emptyReason ? (
          <div className="mt-4 rounded-md border border-dashed border-neutral-300 p-4 text-sm text-neutral-700" data-testid="station-empty">{model.emptyReason}</div>
        ) : (
          <div className="mt-4 space-y-2" data-testid="station-inventory">
            {model.rows.map((station) => (
              <button
                key={station.stationId}
                type="button"
                className={cn('w-full cursor-pointer rounded border px-3 py-2 text-left text-sm transition-colors', selected?.stationId === station.stationId ? 'border-primary-600 bg-primary-50' : 'border-neutral-300 hover:bg-neutral-50')}
                onClick={() => onQueryChange({ stationId: station.stationId })}
              >
                <span className="block font-medium text-neutral-900">{station.stationName}</span>
                <span className="block font-mono text-xs text-neutral-700">{station.stationId}</span>
                <span className="mt-1 block text-xs text-neutral-700">{Math.round(station.completeness * 100)}% · {station.qcStatus}</span>
              </button>
            ))}
          </div>
        )}
      </aside>

      <section className="relative min-h-[32rem] overflow-hidden bg-[#d7e7ef]" aria-label="气象站地图">
        <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(15,52,96,0.08)_1px,transparent_1px),linear-gradient(rgba(15,52,96,0.08)_1px,transparent_1px)] bg-[length:42px_42px]" />
        <div className="absolute left-8 top-8 z-10 rounded-md border border-neutral-300 bg-white/95 p-4 shadow-lg">
          <div className="flex items-center gap-2 text-base font-semibold text-neutral-900">
            <MapPinned className="h-5 w-5 text-primary-600" aria-hidden="true" />
            站点位置与邻近关系
          </div>
          <p className="mt-1 text-sm text-neutral-700">lon/lat 来自站点合同，筛选变化会清理旧 popup。</p>
        </div>
        {model.rows.map((station, index) => (
          <button
            key={station.stationId}
            type="button"
            className={cn(
              'absolute z-10 h-4 w-4 -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full border-2 border-white shadow',
              selected?.stationId === station.stationId ? 'bg-primary-600' : adjacentIds.has(station.stationId) ? 'bg-warning' : 'bg-success',
            )}
            style={{ left: `${46 + index * 10}%`, top: `${48 + index * 8}%` }}
            title={`${station.stationId} ${station.lon}, ${station.lat}`}
            aria-label={`选择站点 ${station.stationId}`}
            onClick={() => onQueryChange({ stationId: station.stationId })}
          />
        ))}
        {selected ? (
          <div className="absolute left-[54%] top-[34%] z-10 w-72 rounded-md border border-neutral-300 bg-white/95 p-3 text-xs text-neutral-700 shadow-lg" data-testid="station-popup">
            <div className="font-semibold text-neutral-900">{selected.stationName}</div>
            <div className="font-mono">{selected.stationId}</div>
            <div>{selected.lon.toFixed(2)}E, {selected.lat.toFixed(2)}N · {selected.basinName}</div>
          </div>
        ) : null}
      </section>

      <aside className="min-h-0 overflow-auto border-t border-neutral-300 p-4 min-[1180px]:border-l min-[1180px]:border-t-0">
        <PanelTitle icon={Thermometer} title="站点 forcing" />
        {selected ? (
          <>
            <MetadataList
              rows={[
                ['station_id', selected.stationId],
                ['经纬度', `${selected.lon.toFixed(2)}E, ${selected.lat.toFixed(2)}N`],
                ['高程', selected.elevationM === null ? '-' : `${selected.elevationM} m`],
                ['最新数据', selected.latestDataTime ?? '无'],
                ['forcing', selected.forcingVersionId ?? 'unavailable'],
                ['完整度', `${Math.round(selected.completeness * 100)}% / ${selected.qcStatus}`],
              ]}
            />
            {selected.unavailableReason ? <StatusBox tone="warning" text={selected.unavailableReason} testId="forcing-unavailable" /> : null}
            <ControlLabel label="相邻站" />
            <div className="space-y-1" data-testid="adjacent-stations">
              {selected.adjacent.map((item) => (
                <div key={item.stationId} className="rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700">
                  <span className="font-mono text-neutral-900">{item.stationId}</span> · {item.distanceKm} km · {item.reason}
                </div>
              ))}
            </div>
            <ControlLabel label="时序与 QC" />
            <div className="space-y-3" data-testid="forcing-charts">
              {model.selectedSeries?.variables.map((variable) => (
                <ForcingChart key={variable.variable} variable={variable} />
              ))}
            </div>
          </>
        ) : (
          <StatusBox tone="warning" text="没有可显示的站点详情。" />
        )}
      </aside>
    </div>
  )
}

function ForcingChart({ variable }: { variable: MeteorologyStationSeriesVariable }) {
  if (variable.unavailableReason || variable.points.length === 0) {
    return <StatusBox tone="warning" text={`${variable.variable}: ${variable.unavailableReason ?? 'forcing unavailable'}`} testId={`forcing-${variable.variable}-unavailable`} />
  }
  const option = {
    color: ['#1565C0'],
    grid: { left: 42, right: 12, top: 24, bottom: 28 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: variable.points.map((point) => point.time.slice(5, 16)), axisLabel: { color: '#64748b' } },
    yAxis: { type: 'value', name: variable.unit, axisLabel: { color: '#64748b' } },
    series: [{
      type: 'line',
      name: variable.variable,
      data: variable.points.map((point) => point.value),
      connectNulls: false,
      markLine: variable.missingIntervals.length
        ? { data: variable.missingIntervals.map((interval) => ({ xAxis: interval.from.slice(5, 16), name: interval.reason })) }
        : undefined,
    }],
  }
  return (
    <div className="rounded-md border border-neutral-300 p-2">
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="font-semibold text-neutral-900">{variable.variable} / {variable.unit}</span>
        <span className={variable.qcStatus === 'partial' ? 'text-warning' : 'text-success'}>{Math.round(variable.completeness * 100)}% · {variable.qcStatus}</span>
      </div>
      <ReactEChartsCore echarts={echarts} option={option} notMerge lazyUpdate style={{ height: 150, width: '100%' }} />
    </div>
  )
}

function PanelTitle({ icon: Icon, title }: { icon: typeof Layers; title: string }) {
  return (
    <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-neutral-900">
      <Icon className="h-4 w-4 text-primary-600" aria-hidden="true" />
      {title}
    </div>
  )
}

function ControlLabel({ label }: { label: string }) {
  return <div className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-neutral-500">{label}</div>
}

function optionClass(selected: boolean) {
  return cn(
    'flex w-full cursor-pointer flex-col rounded border px-3 py-2 text-left text-sm transition-colors',
    selected ? 'border-primary-600 bg-primary-50 text-primary-700' : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50',
  )
}

function ToggleRow({ label, checked, disabled = false, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="mt-3 flex items-center justify-between gap-3 rounded border border-neutral-300 px-3 py-2 text-sm text-neutral-700">
      <span>{label}</span>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}

function MetadataList({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-2 gap-y-1 rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-neutral-700">{label}</dt>
          <dd className="min-w-0 truncate font-mono text-neutral-900" title={value}>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function StatusBox({ text, tone, testId }: { text: string; tone: 'warning' | 'info'; testId?: string }) {
  return (
    <div className={cn('mt-3 rounded-md border px-3 py-2 text-sm', tone === 'warning' ? 'border-warning/40 bg-warning/10 text-neutral-800' : 'border-info/30 bg-primary-50 text-neutral-800')} role="status" data-testid={testId}>
      <div className="flex items-start gap-2">
        <AlertTriangle className={cn('mt-0.5 h-4 w-4 shrink-0', tone === 'warning' ? 'text-warning' : 'text-info')} aria-hidden="true" />
        <span>{text}</span>
      </div>
    </div>
  )
}

function UnavailableOverlay({ reason, testId }: { reason: string; testId: string }) {
  return (
    <div className="absolute left-8 top-28 z-10 max-w-[min(30rem,calc(100%-4rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md" role="status" data-testid={testId}>
      {reason}
    </div>
  )
}

export const meteorologyDependencyDecision = 'No dependency change: reused React Router, Tailwind, lucide-react, ECharts, and existing UI/map visual conventions.'
export { defaultMeteorologyQueryState }
