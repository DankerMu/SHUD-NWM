import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { AlertRankingPanel } from '@/components/flood/AlertRankingPanel'
import { AlertStatsPanel } from '@/components/flood/AlertStatsPanel'
import { AlertTicker } from '@/components/flood/AlertTicker'
import { AlertTimeline } from '@/components/flood/AlertTimeline'
import { FloodAlertMap } from '@/components/flood/FloodAlertMap'
import { SegmentAlertDetail } from '@/components/flood/SegmentAlertDetail'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import { formatDate } from '@/lib/format'
import { parseM11QueryState } from '@/lib/m11/queryState'
import type { AlertLevel } from '@/components/flood/alertLevels'
import { isAlertLevel } from '@/components/flood/alertLevels'
import type { FloodAlertRankingItem } from '@/stores/floodAlert'
import { useFloodAlertStore } from '@/stores/floodAlert'
import { useForecastStore } from '@/stores/forecast'

export function FloodAlertPage() {
  const location = useLocation()
  const routeState = useMemo(() => parseM11QueryState(location.search), [location.search])
  const toast = useToast((state) => state.toast)
  const selectedRunId = useFloodAlertStore((state) => state.selectedRunId)
  const latestRun = useFloodAlertStore((state) => state.latestRun)
  const selectedAlertLevel = useFloodAlertStore((state) => state.selectedAlertLevel)
  const selectedValidTime = useFloodAlertStore((state) => state.selectedValidTime)
  const topLimit = useFloodAlertStore((state) => state.topLimit)
  const basinId = useFloodAlertStore((state) => state.basinId)
  const validTimes = useFloodAlertStore((state) => state.validTimes)
  const summaryData = useFloodAlertStore((state) => state.summaryData)
  const rankingData = useFloodAlertStore((state) => state.rankingData)
  const loading = useFloodAlertStore((state) => state.loading)
  const summaryLoading = useFloodAlertStore((state) => state.summaryLoading)
  const rankingLoading = useFloodAlertStore((state) => state.rankingLoading)
  const error = useFloodAlertStore((state) => state.error)
  const empty = useFloodAlertStore((state) => state.empty)
  const fetchLatestFrequencyDoneRun = useFloodAlertStore((state) => state.fetchLatestFrequencyDoneRun)
  const fetchSummary = useFloodAlertStore((state) => state.fetchSummary)
  const fetchRanking = useFloodAlertStore((state) => state.fetchRanking)
  const setSelectedAlertLevel = useFloodAlertStore((state) => state.setSelectedAlertLevel)
  const assignSelectedAlertLevel = useFloodAlertStore((state) => state.assignSelectedAlertLevel)
  const setSelectedValidTime = useFloodAlertStore((state) => state.setSelectedValidTime)
  const setTopLimit = useFloodAlertStore((state) => state.setTopLimit)
  const setBasinId = useFloodAlertStore((state) => state.setBasinId)
  const [selectedSegment, setSelectedSegment] = useState<FloodAlertRankingItem | null>(null)
  const [playing, setPlaying] = useState(false)
  const clearForecastSelection = useForecastStore((state) => state.clearSelection)

  useEffect(() => {
    const routeWarningLevel = normalizeRouteAlertLevel(routeState.warningLevel)
    if (routeWarningLevel !== useFloodAlertStore.getState().selectedAlertLevel) {
      assignSelectedAlertLevel(routeWarningLevel)
    }
    void fetchLatestFrequencyDoneRun({
      source: routeState.source === 'gfs' || routeState.source === 'ifs' ? routeState.source : null,
      cycleTime: routeState.cycle,
      validTime: routeState.validTime,
    }).catch((error) => {
      toast({
        title: '预警 Run 加载失败',
        description: getApiErrorMessage(error, '获取最新预警 Run 失败'),
        variant: 'destructive',
      })
    })
  }, [assignSelectedAlertLevel, fetchLatestFrequencyDoneRun, routeState.cycle, routeState.source, routeState.validTime, routeState.warningLevel, toast])

  const refreshSnapshots = useCallback(
    async (validTime = selectedValidTime, limit = topLimit) => {
      await Promise.all([fetchSummary({ validTime }), fetchRanking({ validTime, limit })])
    },
    [fetchRanking, fetchSummary, selectedValidTime, topLimit],
  )

  useEffect(() => {
    if (!selectedRunId) return
    void refreshSnapshots().catch((error) => {
      toast({
        title: '预警数据加载失败',
        description: getApiErrorMessage(error, '预警统计或排名加载失败'),
        variant: 'destructive',
      })
    })
  }, [basinId, refreshSnapshots, selectedRunId, toast])

  useEffect(() => {
    setSelectedSegment(null)
    clearForecastSelection()
    setPlaying(false)
  }, [clearForecastSelection, selectedRunId])

  useEffect(() => {
    if (!playing || validTimes.length === 0) return undefined
    const timer = window.setInterval(() => {
      const currentIndex = selectedValidTime ? validTimes.indexOf(selectedValidTime) : -1
      const nextValidTime = validTimes[(currentIndex + 1) % validTimes.length]
      setSelectedValidTime(nextValidTime)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [playing, selectedValidTime, setSelectedValidTime, validTimes])

  const selectTime = (validTime: string | null) => {
    setPlaying(false)
    setSelectedValidTime(validTime)
  }

  const selectLimit = (limit: 10 | 20 | 50) => {
    setTopLimit(limit)
  }

  const selectBasin = (nextBasinId: string) => {
    setBasinId(nextBasinId)
  }

  const selectLevel = (level: AlertLevel) => {
    setSelectedAlertLevel(level)
  }

  const selectSegment = (segment: FloodAlertRankingItem) => {
    setSelectedSegment((current) => ({
      ...segment,
      basinVersionId: segment.basinVersionId ?? current?.basinVersionId ?? latestRun?.basin_version_id,
    }))
  }

  const closeDetail = () => {
    setSelectedSegment(null)
    clearForecastSelection()
  }

  const tileFallbackTime = useMemo(() => latestRun?.end_time ?? validTimes.at(-1) ?? null, [latestRun, validTimes])

  if (loading) {
    return (
      <div className="grid min-h-[calc(100vh-7rem)] place-items-center rounded-lg border border-border bg-panel text-sm text-muted">
        正在加载洪水预警数据...
      </div>
    )
  }

  if (empty) {
    return (
      <div className="grid min-h-[calc(100vh-7rem)] place-items-center rounded-lg border border-border bg-panel p-6 text-center">
        <div>
          <h1 className="text-lg font-semibold text-foreground">暂无洪水预警数据</h1>
          <p className="mt-2 text-sm text-muted">{error ?? '当前没有已完成 frequency_done 的预报 Run。'}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-foreground">洪水预警</h1>
          <p className="mt-1 text-sm text-muted">
            {selectedRunId ?? '-'} · {latestRun?.cycle_time ? formatDate(latestRun.cycle_time) : 'latest'}
          </p>
        </div>
        {error ? (
          <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" role="status">
            {error}
          </div>
        ) : null}
      </div>

      <AlertTicker items={rankingData?.items ?? []} onItemSelect={selectSegment} />

      <div className="grid min-h-[calc(100vh-15rem)] gap-4 xl:h-[calc(100vh-15rem)] xl:grid-cols-[280px_minmax(0,1fr)_320px]">
        <AlertStatsPanel
          summary={summaryData}
          selectedLevel={selectedAlertLevel}
          loading={summaryLoading}
          onLevelSelect={selectLevel}
        />

        <section className="grid min-h-[42rem] grid-rows-[minmax(0,1fr)_auto] gap-4 xl:min-h-0">
          <div className="overflow-hidden rounded-lg border border-border bg-panel" aria-label="洪水预警地图">
            <FloodAlertMap
              runId={selectedRunId}
              validTime={selectedValidTime}
              tileFallbackTime={tileFallbackTime}
              selectedLevel={selectedAlertLevel}
              selectedSegment={selectedSegment}
              onSegmentSelect={selectSegment}
              className="h-full"
            />
          </div>
          <AlertTimeline
            validTimes={validTimes}
            selectedValidTime={selectedValidTime}
            playing={playing}
            onSelect={selectTime}
            onTogglePlayback={() => setPlaying((value) => !value)}
          />
        </section>

        {selectedSegment ? (
          <SegmentAlertDetail
            segment={selectedSegment}
            basinVersionId={selectedSegment.basinVersionId ?? latestRun?.basin_version_id}
            onClose={closeDetail}
          />
        ) : (
          <AlertRankingPanel
            ranking={rankingData}
            limit={topLimit}
            basinId={basinId}
            loading={rankingLoading}
            onLimitChange={selectLimit}
            onBasinChange={selectBasin}
            onRowSelect={selectSegment}
          />
        )}
      </div>
    </div>
  )
}

function normalizeRouteAlertLevel(value: string | null): AlertLevel | null {
  if (value === 'orange') return 'warning'
  if (value === 'red') return 'severe'
  if (value === 'major') return 'high_risk'
  return isAlertLevel(value) ? value : null
}
