import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, RotateCcw, Square, Terminal } from 'lucide-react'
import { useEffect, useState } from 'react'

import { client } from '@/api/client'
import { getApiErrorMessage } from '@/api/response'
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
import { type JobFilters as JobFilterState, type PipelineJob, useMonitoringStore } from '@/stores/monitoring'

type SortKey = 'submitted_at' | 'duration_seconds'
type SortDirection = 'asc' | 'desc'

const failedStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'cancelled', 'partially_failed'])
const retryableStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'partially_failed'])
const activeStatuses = new Set(['pending', 'queued', 'submitted', 'running'])
const policyFailureCodes = new Set(['RBAC_FORBIDDEN', 'AUTH_REQUIRED', 'RELEASE_BLOCKED'])

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

function getApiErrorCode(error: unknown) {
  if (!error || typeof error !== 'object') return null
  const envelope = error as { error?: { code?: string } }
  return envelope.error?.code ?? null
}

export function JobsTable() {
  const role = useAuthStore((state) => state.role)
  const jobs = useMonitoringStore((state) => state.jobs)
  const jobTotal = useMonitoringStore((state) => state.jobTotal)
  const filters = useMonitoringStore((state) => state.jobFilters)
  const isJobsLoading = useMonitoringStore((state) => state.isJobsLoading)
  const fetchJobs = useMonitoringStore((state) => state.fetchJobs)
  const fetchAll = useMonitoringStore((state) => state.fetchAll)
  const { toast } = useToast()

  const [logJobId, setLogJobId] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)

  const page = filters.page ?? 1
  const pageSize = filters.pageSize ?? 12
  const pageCount = Math.max(1, Math.ceil(jobTotal / pageSize))
  const sortKey = filters.sortBy ?? 'submitted_at'
  const sortDirection = filters.sortOrder ?? 'desc'

  useEffect(() => {
    void fetchJobs().catch(() => undefined)
  }, [fetchJobs])

  const actionRole = canUseDevRoleActions(role) ? role : null

  const updateFilters = (nextFilters: JobFilterState) => {
    void fetchJobs({ ...nextFilters, page: 1, pageSize }).catch(() => undefined)
  }

  const toggleSort = (key: SortKey) => {
    const nextDirection = sortKey === key && sortDirection === 'desc' ? 'asc' : 'desc'
    void fetchJobs({ ...filters, sortBy: key, sortOrder: nextDirection, page: 1, pageSize }).catch(() => undefined)
  }

  const runAction = async (job: PipelineJob, action: 'retry' | 'cancel') => {
    if (!job.run_id) return
    if (!actionRole) return

    const actionKey = `${action}:${job.run_id}`
    setPendingAction(actionKey)
    try {
      const path = action === 'retry' ? '/api/v1/runs/{run_id}/retry' : '/api/v1/runs/{run_id}/cancel'
      const { error } = await client.POST(path, {
        params: {
          path: { run_id: job.run_id },
          header: { 'X-User-Role': actionRole },
        },
      })
      if (error) {
        if (policyFailureCodes.has(getApiErrorCode(error) ?? '')) {
          toast({
            title: action === 'retry' ? '重试失败' : '取消失败',
            description: getApiErrorMessage(error, '操作失败'),
            variant: 'destructive',
          })
          await Promise.all([
            fetchAll().catch(() => undefined),
            fetchJobs().catch(() => undefined),
          ])
          return
        }
        throw new Error(getApiErrorMessage(error, action === 'retry' ? '重试失败' : '取消失败'))
      }

      toast({ title: action === 'retry' ? '重试已提交' : '取消请求已提交' })
      await Promise.all([
        fetchAll().catch(() => undefined),
        fetchJobs().catch(() => undefined),
      ])
    } catch (error) {
      toast({
        title: action === 'retry' ? '重试失败' : '取消失败',
        description: getApiErrorMessage(error, '操作失败'),
        variant: 'destructive',
      })
    } finally {
      setPendingAction(null)
    }
  }

  return (
    <Card className="min-w-0">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle>作业列表</CardTitle>
          <div className="text-sm text-muted">
            总数 {jobTotal}，第 {page}/{pageCount} 页
          </div>
        </div>
        <JobFilters filters={filters} onChange={updateFilters} />
      </CardHeader>
      <CardContent className="space-y-3">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>run_id</TableHead>
              <TableHead>model_id</TableHead>
              <TableHead>run_type</TableHead>
              <TableHead>scenario</TableHead>
              <TableHead>status</TableHead>
              <TableHead>slurm_job_id</TableHead>
              <TableHead>
                <Button variant="ghost" size="sm" className="-ml-3 h-8 px-2" onClick={() => toggleSort('submitted_at')}>
                  submitted_at
                  <SortIcon active={sortKey === 'submitted_at'} direction={sortDirection} />
                </Button>
              </TableHead>
              <TableHead>
                <Button variant="ghost" size="sm" className="-ml-3 h-8 px-2" onClick={() => toggleSort('duration_seconds')}>
                  duration
                  <SortIcon active={sortKey === 'duration_seconds'} direction={sortDirection} />
                </Button>
              </TableHead>
              <TableHead>操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.length ? (
              jobs.map((job) => {
                const retryKey = `retry:${job.run_id ?? ''}`
                const cancelKey = `cancel:${job.run_id ?? ''}`
                return (
                  <TableRow key={job.job_id}>
                    <TableCell className="max-w-44 truncate font-medium">{job.run_id ?? '-'}</TableCell>
                    <TableCell>{job.model_id ?? '-'}</TableCell>
                    <TableCell>{job.run_type ?? '-'}</TableCell>
                    <TableCell className="max-w-48 truncate">{job.scenario ?? '-'}</TableCell>
                    <TableCell>
                      <Badge className={cn('whitespace-nowrap', statusClass(job.status))}>{job.status}</Badge>
                    </TableCell>
                    <TableCell>{job.slurm_job_id ?? '-'}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDate(job.submitted_at)}</TableCell>
                    <TableCell className="whitespace-nowrap">{formatDuration(job.duration_seconds)}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1.5">
                        <Button variant="outline" size="sm" onClick={() => setLogJobId(job.job_id)}>
                          <Terminal className="size-3.5" />
                          查看日志
                        </Button>
                        {actionRole && retryableStatuses.has(job.status) && job.run_id ? (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={pendingAction === retryKey}
                            onClick={() => void runAction(job, 'retry')}
                          >
                            <RotateCcw className="size-3.5" />
                            重试
                          </Button>
                        ) : null}
                        {actionRole && activeStatuses.has(job.status) && job.run_id ? (
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={pendingAction === cancelKey}
                            onClick={() => void runAction(job, 'cancel')}
                          >
                            <Square className="size-3.5" />
                            取消
                          </Button>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                )
              })
            ) : (
              <TableRow>
                <TableCell colSpan={9} className="h-24 text-center text-muted">
                  {isJobsLoading ? '加载中...' : '暂无作业'}
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
              disabled={page <= 1 || isJobsLoading}
              onClick={() => void fetchJobs({ ...filters, page: Math.max(1, page - 1), pageSize }).catch(() => undefined)}
            >
              <ChevronLeft className="size-4" />
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= pageCount || isJobsLoading}
              onClick={() => void fetchJobs({ ...filters, page: Math.min(pageCount, page + 1), pageSize }).catch(() => undefined)}
            >
              下一页
              <ChevronRight className="size-4" />
            </Button>
          </div>
        </div>
      </CardContent>
      <LogModal jobId={logJobId} open={Boolean(logJobId)} onOpenChange={(open) => !open && setLogJobId(null)} />
    </Card>
  )
}
