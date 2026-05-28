import { ArrowDown, ArrowUp, ArrowUpDown, CheckCircle2, ChevronLeft, ChevronRight, ClipboardCopy, RotateCcw, Square, Terminal } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { client } from '@/api/client'
import { getApiErrorMessage } from '@/api/response'
import type { components } from '@/api/types'
import { JobFilters } from '@/components/monitoring/JobFilters'
import { LogModal } from '@/components/monitoring/LogModal'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useToast } from '@/hooks/useToast'
import { cn } from '@/lib/cn'
import { formatDate, formatDuration } from '@/lib/format'
import { canUseDevRoleActions, useAuthStore } from '@/stores/auth'
import {
  type JobFilters as JobFilterState,
  type MonitoringStrictIdentity,
  type PipelineJob,
  normalizeMonitoringCycleTime,
  useMonitoringStore,
} from '@/stores/monitoring'

type SortKey = 'submitted_at' | 'duration_seconds'
type SortDirection = 'asc' | 'desc'

const failedStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'cancelled', 'partially_failed'])
const retryableStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'partially_failed'])
const activeStatuses = new Set(['pending', 'queued', 'submitted', 'running'])
type OperatorHeaderRole = components['parameters']['UserRole']

function statusClass(status: string) {
  if (status === 'succeeded') return 'border-emerald-200 bg-emerald-50 text-emerald-700'
  if (failedStatuses.has(status)) return 'border-danger/30 bg-danger/10 text-danger'
  if (status === 'running' || status === 'submitted') return 'border-accent/30 bg-accent/10 text-accent'
  return 'border-border bg-muted/10 text-muted'
}

function SortIcon({ active, direction }: { active: boolean; direction: SortDirection }) {
  if (!active) return <ArrowUpDown className="size-3.5" />
  return direction === 'asc' ? <ArrowUp className="size-3.5" /> : <ArrowDown className="size-3.5" />
}

interface JobsTableProps {
  actionsEnabled?: boolean
  autoFetch?: boolean
  cancelControlsEnabled?: boolean
  clearOnFailure?: boolean
  diagnosticsEnabled?: boolean
  displayEnabled?: boolean
  fetchEnabled?: boolean
  logControlsEnabled?: boolean
  retryControlsEnabled?: boolean
  strictIdentity?: MonitoringStrictIdentity | null
  unavailableReason?: string | null
}

export function JobsTable({
  actionsEnabled = true,
  autoFetch = true,
  cancelControlsEnabled,
  clearOnFailure = false,
  diagnosticsEnabled = false,
  displayEnabled,
  fetchEnabled = true,
  logControlsEnabled = true,
  retryControlsEnabled,
  strictIdentity = null,
  unavailableReason = null,
}: JobsTableProps) {
  const role = useAuthStore((state) => state.role)
  const source = useMonitoringStore((state) => state.source)
  const cycleTime = useMonitoringStore((state) => state.cycleTime)
  const jobs = useMonitoringStore((state) => state.jobs)
  const jobTotal = useMonitoringStore((state) => state.jobTotal)
  const jobsError = useMonitoringStore((state) => state.jobsError)
  const filters = useMonitoringStore((state) => state.jobFilters)
  const isJobsLoading = useMonitoringStore((state) => state.isJobsLoading)
  const fetchJobs = useMonitoringStore((state) => state.fetchJobs)
  const fetchAll = useMonitoringStore((state) => state.fetchAll)
  const { toast } = useToast()

  const [logJobId, setLogJobId] = useState<string | null>(null)
  const [logRefreshKey, setLogRefreshKey] = useState(0)
  const [pendingAction, setPendingAction] = useState<string | null>(null)
  const [notifiedJobs, setNotifiedJobs] = useState<Set<string>>(() => new Set())
  const pendingActionRef = useRef<string | null>(null)

  const page = filters.page ?? 1
  const pageSize = filters.pageSize ?? 12
  const sortKey = filters.sortBy ?? 'submitted_at'
  const sortDirection = filters.sortOrder ?? 'desc'
  const retryActionsEnabled = retryControlsEnabled ?? actionsEnabled
  const cancelActionsEnabled = cancelControlsEnabled ?? actionsEnabled
  const controlsVisible = logControlsEnabled || retryActionsEnabled || cancelActionsEnabled || diagnosticsEnabled
  const canDisplayRows = displayEnabled ?? fetchEnabled
  const visibleJobs = canDisplayRows ? jobs : []
  const visibleJobTotal = canDisplayRows ? jobTotal : 0
  const visiblePage = canDisplayRows ? page : 1
  const visiblePageCount = Math.max(1, Math.ceil(visibleJobTotal / pageSize))
  const controlsDisabled = !fetchEnabled || isJobsLoading

  const requestJobs = useCallback(async (nextFilters?: JobFilterState) => {
    if (!fetchEnabled) return
    await fetchJobs(nextFilters, { clearOnFailure })
  }, [clearOnFailure, fetchEnabled, fetchJobs])

  const refreshSelectedContext = useCallback(async () => {
    await Promise.all([
      fetchAll({ clearOnFailure }).catch(() => undefined),
      requestJobs().catch(() => undefined),
    ])
  }, [clearOnFailure, fetchAll, requestJobs])

  useEffect(() => {
    if (!autoFetch || !fetchEnabled) return
    void requestJobs().catch(() => undefined)
  }, [autoFetch, fetchEnabled, requestJobs])

  useEffect(() => {
    if (!logJobId) return
    if (!visibleJobs.some((job) => job.job_id === logJobId)) setLogJobId(null)
  }, [logJobId, visibleJobs])

  const retryActionRole = fetchEnabled && retryActionsEnabled && canUseDevRoleActions(role) ? role : null
  const cancelActionRole = fetchEnabled && cancelActionsEnabled && canUseDevRoleActions(role) ? role : null
  const retryHeaderRole = retryActionRole as OperatorHeaderRole | null
  const cancelHeaderRole = cancelActionRole as OperatorHeaderRole | null

  const copyDiagnostic = async (job: PipelineJob) => {
    const diagnostic = buildDiagnosticPayload(job, {
      sourceId: strictIdentity?.source ?? source,
      cycleTime: strictIdentity?.cycleTime ?? normalizeMonitoringCycleTime(cycleTime),
      runId: strictIdentity?.runId ?? job.run_id ?? null,
      modelId: strictIdentity?.modelId ?? job.model_id ?? null,
    })
    await navigator.clipboard?.writeText(JSON.stringify(diagnostic, null, 2))
    toast({ title: '诊断已复制' })
  }

  const markNotified = (jobId: string) => {
    setNotifiedJobs((current) => {
      const next = new Set(current)
      next.add(jobId)
      return next
    })
  }

  const updateFilters = (nextFilters: JobFilterState) => {
    void requestJobs({ ...nextFilters, page: 1, pageSize }).catch(() => undefined)
  }

  const toggleSort = (key: SortKey) => {
    const nextDirection = sortKey === key && sortDirection === 'desc' ? 'asc' : 'desc'
    void requestJobs({ ...filters, sortBy: key, sortOrder: nextDirection, page: 1, pageSize }).catch(() => undefined)
  }

  const runAction = async (job: PipelineJob, action: 'retry' | 'cancel') => {
    if (!job.run_id) return
    if (!fetchEnabled) return
    const operatorHeaderRole = action === 'retry' ? retryHeaderRole : cancelHeaderRole
    if (!operatorHeaderRole) return

    const actionKey = `${action}:${job.run_id}`
    if (pendingActionRef.current) return

    pendingActionRef.current = actionKey
    setPendingAction(actionKey)
    try {
      const path = action === 'retry' ? '/api/v1/runs/{run_id}/retry' : '/api/v1/runs/{run_id}/cancel'
      const { error } = await client.POST(path, {
        params: {
          path: { run_id: job.run_id },
          header: { 'X-User-Role': operatorHeaderRole },
        },
      })
      if (error) {
        throw error
      }

      toast({ title: action === 'retry' ? '重试已提交' : '取消请求已提交' })
    } catch (error) {
      toast({
        title: action === 'retry' ? '重试失败' : '取消失败',
        description: getApiErrorMessage(error, '操作失败'),
        variant: 'destructive',
      })
    } finally {
      await refreshSelectedContext()
      setLogRefreshKey((key) => key + 1)
      pendingActionRef.current = null
      setPendingAction(null)
    }
  }

  return (
    <Card className="min-w-0">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle>作业列表</CardTitle>
          <div className="text-sm text-muted">
            总数 {visibleJobTotal}，第 {visiblePage}/{visiblePageCount} 页
          </div>
        </div>
        <JobFilters filters={filters} disabled={!fetchEnabled} onChange={updateFilters} />
      </CardHeader>
      <CardContent className="space-y-3">
        {diagnosticsEnabled ? (
          <div className="rounded-md border border-border bg-background p-3 text-xs text-muted" data-testid="ops-manual-recovery-guidance">
            失败诊断用于交给 22 compute-control 节点处理；在 27 display_readonly 页面只做只读查看和本地通知标记，不写入数据库或审计 API。
          </div>
        ) : null}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>job_id</TableHead>
              <TableHead>run_id</TableHead>
              <TableHead>stage</TableHead>
              <TableHead>model_id</TableHead>
              <TableHead>status</TableHead>
              <TableHead>slurm_job_id</TableHead>
              <TableHead>
                <Button
                  variant="ghost"
                  size="sm"
                  className="-ml-3 h-8 px-2"
                  disabled={!fetchEnabled}
                  onClick={() => toggleSort('submitted_at')}
                >
                  submitted_at
                  <SortIcon active={sortKey === 'submitted_at'} direction={sortDirection} />
                </Button>
              </TableHead>
              <TableHead>started_at</TableHead>
              <TableHead>finished_at</TableHead>
              <TableHead>
                <Button
                  variant="ghost"
                  size="sm"
                  className="-ml-3 h-8 px-2"
                  disabled={!fetchEnabled}
                  onClick={() => toggleSort('duration_seconds')}
                >
                  duration
                  <SortIcon active={sortKey === 'duration_seconds'} direction={sortDirection} />
                </Button>
              </TableHead>
              <TableHead>retry_count</TableHead>
              <TableHead>log</TableHead>
              {controlsVisible ? <TableHead>操作</TableHead> : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {visibleJobs.length ? (
              visibleJobs.map((job) => {
                const logAvailable = Boolean(job.log_uri)
                const diagnosticAvailable = diagnosticsEnabled && failedStatuses.has(job.status)
                const notified = notifiedJobs.has(job.job_id)
                return (
                  <TableRow key={job.job_id}>
                    <TableCell className="max-w-36 truncate font-mono text-xs">{job.job_id}</TableCell>
                    <TableCell className="max-w-44 truncate font-medium">{job.run_id ?? '-'}</TableCell>
                    <TableCell>{job.stage ?? job.job_type ?? '-'}</TableCell>
                    <TableCell>{job.model_id ?? '-'}</TableCell>
                    <TableCell>
                      <Badge className={cn('whitespace-nowrap', statusClass(job.status))}>{job.status}</Badge>
                    </TableCell>
                    <TableCell>{job.slurm_job_id ?? '-'}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDate(job.submitted_at)}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDate(job.started_at)}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDate(job.finished_at)}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDuration(job.duration_seconds)}</TableCell>
                    <TableCell>{job.retry_count}</TableCell>
                    <TableCell>
                      <Badge className={cn(
                        'whitespace-nowrap',
                        logAvailable ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-border bg-muted/10 text-muted',
                      )}>
                        {logAvailable ? 'available' : 'unavailable'}
                      </Badge>
                    </TableCell>
                    {controlsVisible ? (
                      <TableCell>
                        <div className="flex flex-wrap gap-1.5">
                          {logControlsEnabled ? (
                            <Button variant="outline" size="sm" onClick={() => setLogJobId(job.job_id)}>
                              <Terminal className="size-3.5" />
                              查看日志
                            </Button>
                          ) : null}
                          {diagnosticAvailable ? (
                            <>
                              <Button variant="outline" size="sm" onClick={() => void copyDiagnostic(job)}>
                                <ClipboardCopy className="size-3.5" />
                                复制诊断
                              </Button>
                              <Button
                                variant={notified ? 'secondary' : 'outline'}
                                size="sm"
                                disabled={notified}
                                onClick={() => markNotified(job.job_id)}
                              >
                                <CheckCircle2 className="size-3.5" />
                                {notified ? '已通知' : '标记已通知'}
                              </Button>
                            </>
                          ) : null}
                          {retryActionRole && retryableStatuses.has(job.status) && job.run_id ? (
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={Boolean(pendingAction)}
                              onClick={() => void runAction(job, 'retry')}
                            >
                              <RotateCcw className="size-3.5" />
                              重试
                            </Button>
                          ) : null}
                          {cancelActionRole && activeStatuses.has(job.status) && job.run_id ? (
                            <Button
                              variant="destructive"
                              size="sm"
                              disabled={Boolean(pendingAction)}
                              onClick={() => void runAction(job, 'cancel')}
                            >
                              <Square className="size-3.5" />
                              取消
                            </Button>
                          ) : null}
                        </div>
                      </TableCell>
                    ) : null}
                  </TableRow>
                )
              })
            ) : (
              <TableRow>
                <TableCell colSpan={controlsVisible ? 13 : 12} className="h-24 text-center text-muted">
                  {!canDisplayRows
                    ? `当前 source/cycle 的作业不可用：${unavailableReason ?? jobsError ?? '当前路由不支持作业查询'}`
                    : isJobsLoading
                      ? '加载中...'
                      : jobsError
                        ? `当前 source/cycle 的作业不可用：${jobsError}`
                        : '暂无作业'}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-muted">
            每页 {pageSize} 条
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1 || controlsDisabled}
              onClick={() => void requestJobs({ ...filters, page: Math.max(1, page - 1), pageSize }).catch(() => undefined)}
            >
              <ChevronLeft className="size-4" />
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= visiblePageCount || controlsDisabled}
              onClick={() => void requestJobs({ ...filters, page: Math.min(visiblePageCount, page + 1), pageSize }).catch(() => undefined)}
            >
              下一页
              <ChevronRight className="size-4" />
            </Button>
          </div>
        </div>
      </CardContent>
      {logControlsEnabled ? (
        <LogModal
          jobId={logJobId}
          open={Boolean(logJobId)}
          refreshKey={logRefreshKey}
          strictIdentity={strictIdentity}
          onOpenChange={(open) => !open && setLogJobId(null)}
        />
      ) : null}
    </Card>
  )
}

interface DiagnosticContext {
  sourceId: string
  cycleTime: string
  runId: string | null
  modelId: string | null
}

const diagnosticFields = [
  'source_id',
  'cycle_time',
  'run_id',
  'model_id',
  'stage',
  'job_id',
  'slurm_job_id',
  'status',
  'error_code',
  'error_message',
  'log_uri',
] as const

type DiagnosticField = (typeof diagnosticFields)[number]

function addDiagnosticField(payload: Partial<Record<DiagnosticField, string>>, key: DiagnosticField, value: string | null | undefined) {
  const trimmed = typeof value === 'string' ? value.trim() : ''
  if (trimmed) payload[key] = trimmed
}

export function buildDiagnosticPayload(job: PipelineJob, context: DiagnosticContext) {
  const payload: Partial<Record<DiagnosticField, string>> = {}
  addDiagnosticField(payload, 'source_id', context.sourceId)
  addDiagnosticField(payload, 'cycle_time', context.cycleTime)
  addDiagnosticField(payload, 'run_id', context.runId)
  addDiagnosticField(payload, 'model_id', context.modelId)
  addDiagnosticField(payload, 'stage', job.stage ?? job.job_type)
  addDiagnosticField(payload, 'job_id', job.job_id)
  addDiagnosticField(payload, 'slurm_job_id', job.slurm_job_id)
  addDiagnosticField(payload, 'status', job.status)
  addDiagnosticField(payload, 'error_code', job.error_code)
  addDiagnosticField(payload, 'error_message', job.error_message)
  addDiagnosticField(payload, 'log_uri', job.log_uri)
  return payload
}
