import { useCallback, useState } from 'react'

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
import { useMonitoringStore } from '@/stores/monitoring'

const sourceOptions = ['GFS', 'ERA5', 'IFS']

function cycleInputValue(cycleTime: string) {
  const date = new Date(cycleTime)
  if (!Number.isNaN(date.getTime())) return date.toISOString().slice(0, 16)
  return cycleTime.slice(0, 16)
}

export function MonitoringPage() {
  const source = useMonitoringStore((state) => state.source)
  const cycleTime = useMonitoringStore((state) => state.cycleTime)
  const cycle = useMonitoringStore((state) => state.cycle)
  const stages = useMonitoringStore((state) => state.stages)
  const queue = useMonitoringStore((state) => state.queue)
  const queueError = useMonitoringStore((state) => state.queueError)
  const jobFilters = useMonitoringStore((state) => state.jobFilters)
  const isPolling = useMonitoringStore((state) => state.isPolling)
  const error = useMonitoringStore((state) => state.error)
  const setSource = useMonitoringStore((state) => state.setSource)
  const setCycleTime = useMonitoringStore((state) => state.setCycleTime)
  const fetchAll = useMonitoringStore((state) => state.fetchAll)
  const fetchJobs = useMonitoringStore((state) => state.fetchJobs)
  const { toast } = useToast()
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const [trendRefreshKey, setTrendRefreshKey] = useState(0)

  const refreshOperationalData = useCallback(async () => {
    await Promise.all([fetchAll(), fetchJobs()])
  }, [fetchAll, fetchJobs])

  usePolling(refreshOperationalData, 10_000)

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
    refreshAfterSelectionChange()
  }

  const handleCycleTimeChange = (nextCycleTime: string) => {
    if (!nextCycleTime) return
    setCycleTime(nextCycleTime)
    refreshAfterSelectionChange()
  }

  const handleManualRefresh = async () => {
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
        <h1 className="text-xl font-semibold text-foreground">监控工作台</h1>
        <p className="mt-1 text-sm text-muted">
          {cycle?.source ?? source} · {formatDate(cycle?.cycle_time ?? cycleTime)}
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

      <SummaryBar
        source={source}
        cycleTime={cycleTime}
        cycle={cycle}
        queue={queue}
        queueError={queueError}
        isRefreshing={manualRefreshing || isPolling}
        onRefresh={() => void handleManualRefresh()}
      />

      <div className="grid gap-4 min-[800px]:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.2fr)] min-[1200px]:grid-cols-[20rem_minmax(0,1fr)_22rem]">
        <StageList stages={stages} />
        <JobsTable />
        <div className="min-[800px]:col-span-2 min-[1200px]:col-span-1">
          <TrendPanel refreshKey={trendRefreshKey} source={source} scenario={jobFilters.scenario ?? null} />
        </div>
      </div>
    </div>
  )
}
