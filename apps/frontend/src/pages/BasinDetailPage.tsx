import { useEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { LayerList, M11Layout, SegmentSearchStub, StateReadout } from '@/pages/m11/M11Shell'
import { needsM11QueryReplacement, parseM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'

export function BasinDetailPage() {
  const { basinId = 'unknown' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseM11QueryState(location.search), [location.search])
  const normalizedSearch = useMemo(() => serializeM11QueryState(state), [state])

  useEffect(() => {
    if (!needsM11QueryReplacement(location.search)) return
    navigate({ pathname: location.pathname, search: normalizedSearch ? `?${normalizedSearch}` : '' }, { replace: true })
  }, [location.pathname, location.search, navigate, normalizedSearch])

  return (
    <M11Layout
      title="流域分析"
      subtitle={`当前流域 ${basinId}`}
      state={state}
      mapLabel="流域钻取地图"
      mapTitle={`${basinId} 流域钻取`}
      mapMeta="初始钻取壳恢复 basinVersionId、segmentId、source、cycle、validTime、warningLevel 与搜索条件，后续接入真实河段数据。"
      left={
        <>
          <StateReadout state={state} basinId={basinId} />
          <SegmentSearchStub query={state.q} />
          <LayerList activeLayer={state.layer} />
        </>
      }
      right={
        <>
          <div className="rounded-md border border-neutral-300 p-3">
            <div className="text-base font-semibold text-neutral-900">选中河段</div>
            <p className="mt-2 text-sm text-neutral-700">
              {state.segmentId ? `已恢复 ${state.segmentId}` : '尚未选择河段'}
            </p>
          </div>
          <div className="rounded-md border border-neutral-300 p-3">
            <div className="text-base font-semibold text-neutral-900">预警状态</div>
            <p className="mt-2 font-mono text-sm text-neutral-700">{state.warningLevel ?? 'all'}</p>
          </div>
          <Link className="block rounded border border-primary-600 px-3 py-2 text-sm font-medium text-primary-600" to="/forecast">
            返回水文预报
          </Link>
        </>
      }
      timelineLabel={`流域 ${basinId} / ${state.source.toUpperCase()}`}
    />
  )
}

