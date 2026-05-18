import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { ForecastPanel } from '@/components/forecast/ForecastPanel'
import { MapView, type ForecastBasinContext } from '@/components/map/MapView'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import { m11QueryHref, parseM11QueryState } from '@/lib/m11/queryState'
import { useForecastStore, type ForecastSegmentInfo } from '@/stores/forecast'

export function ForecastPage() {
  const location = useLocation()
  const selectedSegment = useForecastStore((state) => state.selectedSegment)
  const forecastData = useForecastStore((state) => state.forecastData)
  const loading = useForecastStore((state) => state.loading)
  const error = useForecastStore((state) => state.error)
  const includeAnalysis = useForecastStore((state) => state.includeAnalysis)
  const selectSegment = useForecastStore((state) => state.selectSegment)
  const fetchForecast = useForecastStore((state) => state.fetchForecast)
  const clearSelection = useForecastStore((state) => state.clearSelection)
  const setRequestContext = useForecastStore((state) => state.setRequestContext)
  const toast = useToast((state) => state.toast)
  const lastRouteHandoffKey = useRef<string | null>(null)
  const [basinContext, setBasinContext] = useState<ForecastBasinContext | null>(null)
  const routeState = useMemo(() => parseM11QueryState(location.search), [location.search])
  const routeSegment = useMemo(() => {
    if (!routeState.segmentId || !routeState.basinVersionId) return null
    return {
      segmentId: routeState.segmentId,
      basinVersionId: routeState.basinVersionId,
    }
  }, [routeState])
  const routeRequestContext = useMemo(
    () => ({
      source: routeState.source === 'best' ? null : routeState.source,
      issueTime: routeState.cycle,
    }),
    [routeState.cycle, routeState.source],
  )
  const routeHandoffKey = routeSegment
    ? `${routeSegment.basinVersionId}::${routeSegment.segmentId}::${routeRequestContext.source ?? 'selected'}::${routeState.cycle ?? 'latest'}`
    : null

  const loadSegmentForecast = useCallback(
    async (segment: ForecastSegmentInfo) => {
      selectSegment(segment)
      try {
        await fetchForecast({
          includeAnalysis: true,
        })
      } catch (error) {
        toast({
          title: '预报曲线加载失败',
          description: getApiErrorMessage(error, '获取预报曲线失败'),
          variant: 'destructive',
        })
      }
    },
    [fetchForecast, selectSegment, toast],
  )

  useEffect(() => {
    setRequestContext(routeRequestContext)
  }, [routeRequestContext, setRequestContext])

  useEffect(() => {
    if (!routeSegment || !routeHandoffKey) {
      lastRouteHandoffKey.current = null
      return
    }
    if (lastRouteHandoffKey.current === routeHandoffKey) return
    lastRouteHandoffKey.current = routeHandoffKey
    void loadSegmentForecast(routeSegment)
  }, [
    forecastData?.segmentId,
    loadSegmentForecast,
    routeHandoffKey,
    routeSegment,
    selectedSegment?.basinVersionId,
    selectedSegment?.segmentId,
  ])

  const retryForecast = useCallback(() => {
    void fetchForecast({ includeAnalysis: true }).catch((error) => {
      toast({
        title: '预报曲线加载失败',
        description: getApiErrorMessage(error, '获取预报曲线失败'),
        variant: 'destructive',
      })
    })
  }, [fetchForecast, toast])

  return (
    <div className="grid min-h-[calc(100vh-7rem)] gap-4 lg:h-[calc(100vh-7rem)] lg:grid-cols-[minmax(0,1fr)_24rem]">
      <section
        className="min-h-[32rem] overflow-hidden rounded-lg border border-border bg-panel lg:min-h-0"
        aria-label="河网地图"
      >
        <MapView
          className="h-full"
          selectedSegmentId={selectedSegment?.segmentId}
          onSegmentSelect={(segment) => void loadSegmentForecast(segment)}
          onClearSelection={clearSelection}
          onBasinContextLoaded={setBasinContext}
        />
      </section>

      {selectedSegment ? (
        <div className="flex h-full min-h-0 flex-col gap-3">
          <div className="rounded-lg border border-border bg-panel px-4 py-3">
            <ForecastBasinHandoff context={basinContext} routeState={routeState} />
          </div>
          <ForecastPanel
            segment={selectedSegment}
            forecastData={forecastData}
            loading={loading}
            error={error}
            includeAnalysis={includeAnalysis}
            contextNote={routeState.validTime ? `已保留 validTime=${routeState.validTime} 于 URL；当前预报曲线请求按 cycle/source 取序列。` : null}
            onClose={clearSelection}
            onRetry={retryForecast}
          />
        </div>
      ) : (
        <aside className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
          <div className="grid min-h-72 flex-1 place-items-center p-4 text-center text-sm text-muted">
            <div>
              <p>请在地图上选择河段查看预报</p>
          <ForecastBasinHandoff context={basinContext} routeState={routeState} />
            </div>
          </div>
        </aside>
      )}
    </div>
  )
}

function ForecastBasinHandoff({ context, routeState }: { context: ForecastBasinContext | null; routeState: ReturnType<typeof parseM11QueryState> }) {
  if (!context) return null
  const href = m11QueryHref(`/basins/${encodeURIComponent(context.basinId)}`, routeState, {
    basinVersionId: context.basinVersionId,
    segmentId: routeState.basinVersionId === context.basinVersionId ? routeState.segmentId : null,
  })

  return (
    <Link
      to={href}
      className="mt-3 inline-flex h-9 items-center rounded border border-primary-600 px-3 text-sm font-medium text-primary-600 hover:bg-primary-50"
    >
      进入流域分析
    </Link>
  )
}
