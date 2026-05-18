import { useEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { BasinLink, LayerList, M11Layout, StateReadout } from '@/pages/m11/M11Shell'
import { needsM11QueryReplacement, parseM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'

export function OverviewPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseM11QueryState(location.search), [location.search])
  const normalizedSearch = useMemo(() => serializeM11QueryState(state), [state])

  useEffect(() => {
    if (!needsM11QueryReplacement(location.search)) return
    navigate({ pathname: location.pathname, search: normalizedSearch ? `?${normalizedSearch}` : '' }, { replace: true })
  }, [location.pathname, location.search, navigate, normalizedSearch])

  const basinSearch = serializeM11QueryState({
    ...state,
    basinVersionId: state.basinVersionId ?? 'bv-demo',
    segmentId: state.segmentId,
  })

  return (
    <M11Layout
      title="全国总览"
      subtitle="全国流域、图层和运行态势"
      state={state}
      mapLabel="全国总览地图"
      mapTitle="全国水文总览"
      mapMeta="初始地图壳保留全国范围、流域边界、河网和图层占位，不加载未实现的真实适配器。"
      left={
        <>
          <div className="space-y-2">
            <div className="text-sm font-semibold text-neutral-900">流域管理</div>
            {['长江流域', '黄河流域', '珠江流域', '松辽流域'].map((basin) => (
              <label key={basin} className="flex items-center gap-2 rounded px-2 py-1.5 text-sm text-neutral-700">
                <input type="checkbox" defaultChecked className="h-4 w-4" />
                {basin}
              </label>
            ))}
          </div>
          <LayerList activeLayer={state.layer} />
          <BasinLink to={`/basins/basin-demo${basinSearch ? `?${basinSearch}` : ''}`}>进入示例流域分析</BasinLink>
        </>
      }
      right={
        <>
          <div className="grid grid-cols-2 gap-3">
            <SummaryMetric value="23" label="今日完成周期" />
            <SummaryMetric value="7" label="当前运行中" />
            <SummaryMetric value="18" label="超警河段" tone="warning" />
            <SummaryMetric value="08:00" label="最新更新时间" />
          </div>
          <div className="space-y-2">
            <Link className="block rounded border border-neutral-300 p-3 hover:bg-primary-50" to="/monitoring">
              产品监控摘要
            </Link>
            <Link className="block rounded border border-neutral-300 p-3 hover:bg-primary-50" to="/flood-alerts">
              洪水预警摘要
            </Link>
          </div>
          <StateReadout state={state} />
        </>
      }
    />
  )
}

function SummaryMetric({ value, label, tone = 'default' }: { value: string; label: string; tone?: 'default' | 'warning' }) {
  return (
    <div className="rounded-md border border-neutral-300 bg-white p-3 text-center shadow-sm">
      <div className={tone === 'warning' ? 'text-2xl font-bold text-warning' : 'text-2xl font-bold text-primary-600'}>
        {value}
      </div>
      <div className="mt-1 text-xs text-neutral-700">{label}</div>
    </div>
  )
}

