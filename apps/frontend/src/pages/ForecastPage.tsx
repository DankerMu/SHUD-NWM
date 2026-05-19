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
    if (!routeState.segmentId || !routeState.basinVersionId || !routeState.riverNetworkVersionId) return null
    return {
      segmentId: routeState.segmentId,
      basinVersionId: routeState.basinVersionId,
      riverNetworkVersionId: routeState.riverNetworkVersionId,
    }
  }, [routeState])
  const routeRequestContext = useMemo(
    () => ({
      source: routeState.source,
      issueTime: routeState.cycle,
    }),
    [routeState.cycle, routeState.source],
  )
  const routeHandoffKey = routeSegment
    ? `${routeSegment.basinVersionId}::${routeSegment.riverNetworkVersionId}::${routeSegment.segmentId}::${routeRequestContext.source ?? 'selected'}::${routeState.cycle ?? 'latest'}`
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
    selectedSegment?.riverNetworkVersionId,
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
            <ForecastSegmentDetailHandoff segment={selectedSegment} routeState={routeState} forecastData={scopedForecastDataForSegment(forecastData, selectedSegment)} />
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

function ForecastSegmentDetailHandoff({
  segment,
  routeState,
  forecastData,
}: {
  segment: ForecastSegmentInfo
  routeState: ReturnType<typeof parseM11QueryState>
  forecastData: ReturnType<typeof useForecastStore.getState>['forecastData']
}) {
  if (!segment.basinVersionId || !segment.riverNetworkVersionId) return null
  const forecastSource = concreteForecastSource(forecastData) ?? routeState.source
  const forecastCycle = routeState.cycle ?? forecastData?.cycle ?? forecastData?.issueTime ?? null
  const forecastValidTime = routeState.validTime ?? firstForecastValidTime(forecastData)
  const href = m11QueryHref(`/segments/${encodeURIComponent(segment.segmentId)}`, routeState, {
    source: forecastSource,
    cycle: forecastCycle,
    validTime: forecastValidTime,
    basinVersionId: segment.basinVersionId,
    riverNetworkVersionId: segment.riverNetworkVersionId,
    segmentId: segment.segmentId,
  })

  return (
    <Link
      to={href}
      className="ml-2 mt-3 inline-flex h-9 items-center rounded border border-primary-600 px-3 text-sm font-medium text-primary-600 hover:bg-primary-50"
    >
      查看河段详情
    </Link>
  )
}

function scopedForecastDataForSegment(
  data: ReturnType<typeof useForecastStore.getState>['forecastData'],
  segment: ForecastSegmentInfo,
) {
  if (!data) return null
  if (data.segmentId !== segment.segmentId) return null
  if (data.basinVersionId !== segment.basinVersionId) return null
  if (data.riverNetworkVersionId !== segment.riverNetworkVersionId) return null
  return data
}

function concreteForecastSource(data: ReturnType<typeof useForecastStore.getState>['forecastData']) {
  const sources = [
    ...new Set(
      (data?.series ?? [])
        .filter((series) => !series.isAnalysis)
        .map((series) => series.source?.toLowerCase())
        .filter((source): source is 'gfs' | 'ifs' => source === 'gfs' || source === 'ifs'),
    ),
  ]
  if (sources.length === 1) return sources[0]
  if (sources.length > 1) return 'compare'
  return data?.source && data.source !== 'best' ? data.source : null
}

function firstForecastValidTime(data: ReturnType<typeof useForecastStore.getState>['forecastData']) {
  const points = (data?.series ?? [])
    .flatMap((series) => series.points)
    .map((point) => {
      const timestamp = typeof point.time === 'number' ? point.time : Date.parse(point.time)
      return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
    })
    .filter((value): value is string => Boolean(value))
  points.sort((left, right) => Date.parse(left) - Date.parse(right))
  return points[0] ?? null
}

function ForecastBasinHandoff({ context, routeState }: { context: ForecastBasinContext | null; routeState: ReturnType<typeof parseM11QueryState> }) {
  if (!context) return null
  const href = m11QueryHref(`/basins/${encodeURIComponent(context.basinId)}`, routeState, {
    basinVersionId: context.basinVersionId,
    segmentId: routeState.basinVersionId === context.basinVersionId ? routeState.segmentId : null,
    riverNetworkVersionId: routeState.basinVersionId === context.basinVersionId ? routeState.riverNetworkVersionId : null,
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
