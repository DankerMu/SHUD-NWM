import { useCallback, useState } from 'react'

import { JobsTable } from '@/components/monitoring/JobsTable'
import { StageList } from '@/components/monitoring/StageList'
import { SummaryBar } from '@/components/monitoring/SummaryBar'
import { TrendPanel } from '@/components/monitoring/TrendPanel'
import { usePolling } from '@/hooks/usePolling'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import { formatDate } from '@/lib/format'
import { useMonitoringStore } from '@/stores/monitoring'

export function MonitoringPage() {
  const source = useMonitoringStore((state) => state.source)
  const cycleTime = useMonitoringStore((state) => state.cycleTime)
  const cycle = useMonitoringStore((state) => state.cycle)
  const stages = useMonitoringStore((state) => state.stages)
  const summaryJobs = useMonitoringStore((state) => state.summaryJobs)
  const queue = useMonitoringStore((state) => state.queue)
  const queueError = useMonitoringStore((state) => state.queueError)
  const isPolling = useMonitoringStore((state) => state.isPolling)
  const error = useMonitoringStore((state) => state.error)
  const fetchAll = useMonitoringStore((state) => state.fetchAll)
  const fetchJobs = useMonitoringStore((state) => state.fetchJobs)
  const { toast } = useToast()
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const [trendRefreshKey, setTrendRefreshKey] = useState(0)

  const refreshOperationalData = useCallback(async () => {
    await Promise.all([fetchAll(), fetchJobs()])
  }, [fetchAll, fetchJobs])

  usePolling(refreshOperationalData, 10_000)

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

      {error ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger" role="status">
          {error}
        </div>
      ) : null}

      <SummaryBar
        source={source}
        cycleTime={cycleTime}
        cycle={cycle}
        jobs={summaryJobs}
        queue={queue}
        queueError={queueError}
        isRefreshing={manualRefreshing || isPolling}
        onRefresh={() => void handleManualRefresh()}
      />

      <div className="grid gap-4 min-[800px]:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.2fr)] min-[1200px]:grid-cols-[20rem_minmax(0,1fr)_22rem]">
        <StageList stages={stages} />
        <JobsTable />
        <div className="min-[800px]:col-span-2 min-[1200px]:col-span-1">
          <TrendPanel refreshKey={trendRefreshKey} />
        </div>
      </div>
    </div>
  )
}
