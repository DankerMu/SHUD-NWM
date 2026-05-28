import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { JobsTable } from '@/components/monitoring/JobsTable'
import { StageList } from '@/components/monitoring/StageList'
import { SummaryBar } from '@/components/monitoring/SummaryBar'
import { TrendPanel } from '@/components/monitoring/TrendPanel'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { usePolling } from '@/hooks/usePolling'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import { formatDate } from '@/lib/format'
import { parseMonitoringQueryState } from '@/lib/monitoring/queryState'
import {
  isDisplayReadonlyRuntimeConfig,
  monitoringContextMatches,
  normalizeMonitoringCycleTime,
  type MonitoringStrictIdentity,
  useMonitoringStore,
} from '@/stores/monitoring'

const sourceOptions = ['GFS', 'ERA5', 'IFS']

type MonitoringPageMode = 'ops' | 'monitoring'

interface MonitoringPageProps {
  mode?: MonitoringPageMode
}

function cycleInputValue(cycleTime: string) {
  const date = new Date(cycleTime)
  if (!Number.isNaN(date.getTime())) return date.toISOString().slice(0, 16)
  return cycleTime.slice(0, 16)
}

export function MonitoringPage({ mode = 'monitoring' }: MonitoringPageProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const routeState = useMemo(() => parseMonitoringQueryState(location.search), [location.search])
  const source = useMonitoringStore((state) => state.source)
  const cycleTime = useMonitoringStore((state) => state.cycleTime)
  const cycle = useMonitoringStore((state) => state.cycle)
  const cycleContext = useMonitoringStore((state) => state.cycleContext)
  const stages = useMonitoringStore((state) => state.stages)
  const jobsContext = useMonitoringStore((state) => state.jobsContext)
  const queue = useMonitoringStore((state) => state.queue)
  const queueError = useMonitoringStore((state) => state.queueError)
  const operationalError = useMonitoringStore((state) => state.operationalError)
  const jobFilters = useMonitoringStore((state) => state.jobFilters)
  const isPolling = useMonitoringStore((state) => state.isPolling)
  const error = useMonitoringStore((state) => state.error)
  const strictIdentity = useMonitoringStore((state) => state.strictIdentity)
  const runtimeConfig = useMonitoringStore((state) => state.runtimeConfig)
  const runtimeConfigError = useMonitoringStore((state) => state.runtimeConfigError)
  const setSource = useMonitoringStore((state) => state.setSource)
  const setCycleTime = useMonitoringStore((state) => state.setCycleTime)
  const setStrictIdentity = useMonitoringStore((state) => state.setStrictIdentity)
  const clearSelectedContext = useMonitoringStore((state) => state.clearSelectedContext)
  const fetchRuntimeConfig = useMonitoringStore((state) => state.fetchRuntimeConfig)
  const fetchAll = useMonitoringStore((state) => state.fetchAll)
  const fetchJobs = useMonitoringStore((state) => state.fetchJobs)
  const { toast } = useToast()
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const [trendRefreshKey, setTrendRefreshKey] = useState(0)
  const isOpsMode = mode === 'ops'
  const canonicalRoute = isOpsMode ? '/ops' : '/monitoring'
  const routeError = isOpsMode ? routeState.sourceError ?? routeState.cycleError ?? routeState.strictIdentityError : null
  const routeSource = routeState.source
  const routeCycle = !isOpsMode && routeState.sourceError ? null : routeState.cycle
  const routeStrictIdentity = isOpsMode ? routeState.strictIdentity : null
  const isRouteSupported = !routeError
  const hasExplicitRouteContext = Boolean(routeSource || routeCycle || routeStrictIdentity)
  const runtimeConfigUnavailableReason = runtimeConfigError ? `runtime config 不可用：${runtimeConfigError}` : null
  const isRuntimeConfigReady = Boolean(runtimeConfig) && !runtimeConfigError
  const isDisplayReadonly = isDisplayReadonlyRuntimeConfig(runtimeConfig)
  const controlMutationsEnabled = Boolean(runtimeConfig?.control_mutations_enabled) && !isDisplayReadonly
  const queueReadonlyUnavailable = isDisplayReadonly || runtimeConfig?.queue_depth_mode === 'display_readonly_unavailable'
  const isRouteContextReady = useMemo(() => {
    if (!isOpsMode) return true
    if (!isRouteSupported) return false
    if (!hasExplicitRouteContext) return true

    const sourceReady = !routeSource || routeSource === source.toUpperCase()
    const cycleReady = !routeCycle || routeCycle === normalizeMonitoringCycleTime(cycleTime)
    const strictReady = !routeStrictIdentity || (
      strictIdentity?.runId === routeStrictIdentity.runId &&
      strictIdentity?.modelId === routeStrictIdentity.modelId &&
      strictIdentity?.source === routeStrictIdentity.source &&
      strictIdentity?.cycleTime === routeStrictIdentity.cycleTime
    )
    return sourceReady && cycleReady && strictReady
  }, [cycleTime, hasExplicitRouteContext, isOpsMode, isRouteSupported, routeCycle, routeSource, routeStrictIdentity, source, strictIdentity])
  const isOperationalDataReady = isRouteSupported && isRouteContextReady && isRuntimeConfigReady
  const routeContextUnavailableReason = isOpsMode && isRouteSupported && !isRouteContextReady
    ? '正在应用 URL source/cycle 上下文。'
    : null
  const dataUnavailableReason = runtimeConfigUnavailableReason ?? routeError ?? routeContextUnavailableReason
  const visibleSource = isOpsMode && routeSource ? routeSource : source
  const visibleCycleTime = isOpsMode && routeCycle ? routeCycle : cycleTime
  const visibleStrictIdentity = routeStrictIdentity
  const hasVisibleCyclePayload = !isOpsMode || monitoringContextMatches(cycleContext, visibleSource, visibleCycleTime, visibleStrictIdentity)
  const hasVisibleJobsPayload = !isOpsMode || monitoringContextMatches(jobsContext, visibleSource, visibleCycleTime, visibleStrictIdentity)
  const displayPayloadUnavailableReason = isOpsMode && isOperationalDataReady && !hasVisibleCyclePayload
    ? '当前 source/cycle 的流水线数据尚未加载完成。'
    : null
  const jobsPayloadUnavailableReason = isOpsMode && isOperationalDataReady && !hasVisibleJobsPayload
    ? '当前 source/cycle 的作业数据尚未加载完成。'
    : null
  const visibleCycle = isOperationalDataReady && hasVisibleCyclePayload ? cycle : null
  const visibleStages = isOperationalDataReady && hasVisibleCyclePayload ? stages : []
  const visibleQueue = isOperationalDataReady ? queue : null
  const visibleQueueError = isOperationalDataReady ? queueError : null
  const stageListUnavailableReason = dataUnavailableReason
    ?? (isOpsMode ? operationalError : null)
    ?? displayPayloadUnavailableReason

  const updateQueryState = useCallback((nextSource: string, nextCycleTime: string) => {
    const params = new URLSearchParams(location.search)
    const normalizedSource = nextSource.toLowerCase()
    const normalizedCycleTime = normalizeMonitoringCycleTime(nextCycleTime)
    params.delete('cycle_time')
    params.delete('run_id')
    params.delete('model_id')
    params.set('source', normalizedSource)
    params.set('cycle', normalizedCycleTime)
    navigate(`${canonicalRoute}?${params.toString()}`, { replace: true })
  }, [canonicalRoute, location.search, navigate])

  useEffect(() => {
    if (runtimeConfig || runtimeConfigError) return
    void fetchRuntimeConfig()
  }, [fetchRuntimeConfig, runtimeConfig, runtimeConfigError])

  useEffect(() => {
    if (routeError) {
      clearSelectedContext()
      setStrictIdentity(null)
      return
    }

    const nextSource = routeSource
    const nextCycleTime = routeCycle
    const currentState = useMonitoringStore.getState()
    const sourceChanged = Boolean(nextSource && nextSource !== currentState.source)
    const cycleChanged = Boolean(nextCycleTime && nextCycleTime !== currentState.cycleTime)
    const strictChanged = isOpsMode
      ? !monitoringStrictIdentityEquals(routeStrictIdentity, currentState.strictIdentity)
      : currentState.strictIdentity !== null
    if (sourceChanged && nextSource) setSource(nextSource)
    if (cycleChanged && nextCycleTime) setCycleTime(nextCycleTime)
    if (strictChanged) setStrictIdentity(isOpsMode ? routeStrictIdentity : null)
    if ((isOpsMode || strictChanged) && (sourceChanged || cycleChanged || strictChanged)) clearSelectedContext()
  }, [clearSelectedContext, isOpsMode, routeCycle, routeError, routeSource, routeStrictIdentity, setCycleTime, setSource, setStrictIdentity])

  const refreshOperationalData = useCallback(async () => {
    if (!isOperationalDataReady) return
    const options = { clearOnFailure: isOpsMode }
    await Promise.all([fetchAll(options), fetchJobs(undefined, options)])
  }, [fetchAll, fetchJobs, isOperationalDataReady, isOpsMode])

  usePolling(refreshOperationalData, 10_000, isOperationalDataReady)

  const refreshAfterSelectionChange = () => {
    void refreshOperationalData().catch((error) => {
      toast({
        title: '刷新失败',
        description: getApiErrorMessage(error, '监控数据刷新失败'),
        variant: 'destructive',
      })
    })
  }

  const handleSourceChange = (nextSource: string) => {
    if (nextSource === source) return
    setSource(nextSource)
    if (canonicalRoute === '/ops') {
      setStrictIdentity(null)
      clearSelectedContext()
    }
    updateQueryState(nextSource, cycleTime)
    refreshAfterSelectionChange()
  }

  const handleCycleTimeChange = (nextCycleTime: string) => {
    if (!nextCycleTime) return
    setCycleTime(nextCycleTime)
    if (canonicalRoute === '/ops') {
      setStrictIdentity(null)
      clearSelectedContext()
    }
    updateQueryState(source, nextCycleTime)
    refreshAfterSelectionChange()
  }

  const handleManualRefresh = async () => {
    if (!isOperationalDataReady) return
    setManualRefreshing(true)
    try {
      await refreshOperationalData()
      setTrendRefreshKey((key) => key + 1)
      toast({ title: '监控数据已刷新' })
    } catch (error) {
      toast({
        title: '刷新失败',
        description: getApiErrorMessage(error, '监控数据刷新失败'),
        variant: 'destructive',
      })
    } finally {
      setManualRefreshing(false)
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-foreground">{canonicalRoute === '/ops' ? '运维工作台' : '监控工作台'}</h1>
        <p className="mt-1 text-sm text-muted">
          {visibleCycle?.source ?? visibleSource} · {formatDate(visibleCycle?.cycle_time ?? visibleCycleTime)}
        </p>
      </div>

      <div className="grid gap-3 rounded-md border border-border bg-panel p-3 sm:grid-cols-[12rem_minmax(16rem,20rem)]">
        <div className="space-y-1">
          <span className="text-xs font-medium uppercase text-muted">Source</span>
          <Select value={source} onValueChange={handleSourceChange}>
            <SelectTrigger aria-label="Source">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {sourceOptions.map((option) => (
                <SelectItem key={option} value={option}>
                  {option}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <label className="space-y-1">
          <span className="text-xs font-medium uppercase text-muted">Cycle Time (UTC)</span>
          <input
            aria-label="Cycle Time UTC"
            type="datetime-local"
            value={cycleInputValue(cycleTime)}
            onChange={(event) => handleCycleTimeChange(event.target.value)}
            className="h-10 w-full rounded-md border border-border bg-panel px-3 py-2 text-sm text-foreground ring-offset-background focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </label>
      </div>

      {error ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="status">
          {error}
        </div>
      ) : null}

      {routeError ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="status">
          {routeError}
        </div>
      ) : null}

      {runtimeConfigUnavailableReason ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="status">
          {runtimeConfigUnavailableReason}
        </div>
      ) : null}

      {isDisplayReadonly ? (
        <div className="rounded-md border border-border bg-background p-3 text-sm text-muted" role="status">
          display_readonly：当前前端从后端 runtime config 判定为只读展示节点，重试、取消和 Slurm 控制请求已禁用；需要恢复时在 22 compute-control 节点按恢复 runbook 处理。
        </div>
      ) : null}

      <SummaryBar
        source={visibleSource}
        cycleTime={visibleCycleTime}
        cycle={visibleCycle}
        queue={visibleQueue}
        queueError={visibleQueueError}
        queueReadonlyUnavailable={isDisplayReadonly && queueReadonlyUnavailable}
        isRefreshing={isOperationalDataReady && (manualRefreshing || isPolling)}
        onRefresh={() => void handleManualRefresh()}
        disabled={!isOperationalDataReady}
      />

      <div className="grid gap-4 min-[800px]:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.2fr)] min-[1200px]:grid-cols-[20rem_minmax(0,1fr)_22rem]">
        <StageList
          diagnosticContext={{
            sourceId: visibleStrictIdentity?.source ?? visibleSource,
            cycleTime: visibleStrictIdentity?.cycleTime ?? normalizeMonitoringCycleTime(visibleCycleTime),
            runId: visibleStrictIdentity?.runId ?? null,
            modelId: visibleStrictIdentity?.modelId ?? null,
          }}
          diagnosticsEnabled={isOpsMode}
          stages={visibleStages}
          unavailableReason={stageListUnavailableReason}
          showPendingPlaceholders={!isOpsMode}
        />
        <JobsTable
          autoFetch={isOperationalDataReady}
          cancelControlsEnabled={!isOpsMode && controlMutationsEnabled && isOperationalDataReady}
          clearOnFailure={isOpsMode}
          diagnosticsEnabled={isOpsMode}
          displayEnabled={isOperationalDataReady && hasVisibleJobsPayload}
          fetchEnabled={isOperationalDataReady}
          logControlsEnabled={isOperationalDataReady}
          retryControlsEnabled={controlMutationsEnabled && isOperationalDataReady}
          strictIdentity={visibleStrictIdentity}
          unavailableReason={dataUnavailableReason ?? jobsPayloadUnavailableReason}
        />
        <div className="min-[800px]:col-span-2 min-[1200px]:col-span-1">
          <TrendPanel
            fetchEnabled={isOperationalDataReady}
            refreshKey={trendRefreshKey}
            source={visibleSource}
            scenario={jobFilters.scenario ?? null}
            unavailableReason={dataUnavailableReason}
          />
        </div>
      </div>
    </div>
  )
}

function monitoringStrictIdentityEquals(
  left: MonitoringStrictIdentity | null,
  right: MonitoringStrictIdentity | null,
) {
  if (!left && !right) return true
  if (!left || !right) return false
  return (
    left.source === right.source &&
    left.cycleTime === right.cycleTime &&
    left.runId === right.runId &&
    left.modelId === right.modelId
  )
}
