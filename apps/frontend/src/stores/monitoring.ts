import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import type { JobStatus, PipelineStatus } from '@/lib/constants'

export type PipelineCycle = components['schemas']['PipelineStatus']
export type PipelineJob = components['schemas']['PipelineJob'] & {
  run_type?: string | null
  scenario?: string | null
  scenario_id?: string | null
}
export type PipelineJobPage = components['schemas']['PipelineJobPage'] & {
  items: PipelineJob[]
}
export type PipelineStage = components['schemas']['PipelineStage']
export type StageDurationMetric = components['schemas']['StageDurationMetric']
export type SuccessRateMetric = components['schemas']['SuccessRateMetric']
export type JobSortBy = 'submitted_at' | 'duration_seconds'
export type JobSortOrder = 'asc' | 'desc'

export type QueueState = components['schemas']['QueueDepth']

export interface JobFilters {
  status?: JobStatus
  runType?: string
  scenario?: string
  sortBy?: JobSortBy
  sortOrder?: JobSortOrder
  page?: number
  pageSize?: number
}

interface MonitoringState {
  source: string
  cycleTime: string
  cycle: PipelineCycle | null
  stages: PipelineStage[]
  jobs: PipelineJob[]
  jobTotal: number
  queue: QueueState | null
  queueError: string | null
  jobFilters: JobFilters
  isPolling: boolean
  isJobsLoading: boolean
  error: string | null
  setSource: (source: string) => void
  setCycleTime: (cycleTime: string | null) => void
  fetchAll: () => Promise<void>
  fetchJobs: (filters?: JobFilters) => Promise<void>
}

function defaultCycleTime() {
  const date = new Date()
  date.setUTCMinutes(0, 0, 0)
  return date.toISOString().slice(0, 19) + 'Z'
}

function cycleTimeForApi(cycleTime: string | null | undefined) {
  if (!cycleTime) return defaultCycleTime()
  if (cycleTime.length === 16) return `${cycleTime}:00Z`
  return cycleTime
}

function inferRunType(runId: string, jobType: string | null | undefined) {
  const value = `${runId} ${jobType ?? ''}`.toLowerCase()
  if (value.includes('analysis')) return 'analysis'
  if (value.includes('hindcast')) return 'hindcast'
  return 'forecast'
}

function inferScenario(runId: string) {
  const value = runId.toLowerCase()
  if (value.includes('ifs')) return 'forecast_ifs_deterministic'
  if (value.includes('analysis')) return 'analysis_true_field'
  return 'forecast_gfs_deterministic'
}

function normalizeJob(job: PipelineJob): PipelineJob {
  const runId = job.run_id ?? ''
  return {
    ...job,
    run_type: job.run_type ?? inferRunType(runId, job.job_type),
    scenario: job.scenario ?? job.scenario_id ?? inferScenario(runId),
  }
}

async function getPipelineStatus(source: string, cycleTime: string) {
  const { data, error } = await client.GET('/api/v1/pipeline/status', {
    params: { query: { source, cycle_time: cycleTime } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取周期状态失败'))
  return unwrapApiData<PipelineCycle>(data, '获取周期状态失败')
}

async function getPipelineStages(source: string, cycleTime: string) {
  const { data, error } = await client.GET('/api/v1/pipeline/stages', {
    params: { query: { source, cycle_time: cycleTime } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取阶段状态失败'))
  return unwrapApiData<PipelineStage[]>(data, '获取阶段状态失败')
}

async function getQueueDepth() {
  const { data, error } = await client.GET('/api/v1/queue/depth')
  if (error) throw new Error(getApiErrorMessage(error, '获取队列深度失败'))
  return unwrapApiData<QueueState>(data, '获取队列深度失败')
}

async function getJobsPage(source: string, cycleTime: string, filters: JobFilters) {
  const page = filters.page ?? 1
  const pageSize = filters.pageSize ?? 12
  const { data, error } = await client.GET('/api/v1/jobs', {
    params: {
      query: {
        source,
        cycle_time: cycleTime,
        status: filters.status,
        run_type: filters.runType,
        scenario: filters.scenario,
        sort_by: filters.sortBy,
        sort_order: filters.sortOrder,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      },
    },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取作业列表失败'))
  return unwrapApiData<PipelineJobPage>(data, '获取作业列表失败')
}

export const useMonitoringStore = create<MonitoringState>((set, get) => ({
  source: 'GFS',
  cycleTime: defaultCycleTime(),
  cycle: null,
  stages: [],
  jobs: [],
  jobTotal: 0,
  queue: null,
  queueError: null,
  jobFilters: { page: 1, pageSize: 12, sortBy: 'submitted_at', sortOrder: 'desc' },
  isPolling: false,
  isJobsLoading: false,
  error: null,
  setSource: (source) => set((state) => ({ source, jobFilters: { ...state.jobFilters, page: 1 } })),
  setCycleTime: (cycleTime) => set((state) => ({
    cycleTime: cycleTimeForApi(cycleTime),
    jobFilters: { ...state.jobFilters, page: 1 },
  })),
  fetchAll: async () => {
    const { source, cycleTime } = get()
    const apiCycleTime = cycleTimeForApi(cycleTime)
    set({ isPolling: true, error: null, queueError: null })

    try {
      const [cycle, stages] = await Promise.all([
        getPipelineStatus(source, apiCycleTime),
        getPipelineStages(source, apiCycleTime),
      ])

      let queue: QueueState | null = null
      let queueError: string | null = null
      try {
        queue = await getQueueDepth()
      } catch (error) {
        queueError = getApiErrorMessage(error, '队列深度暂不可用')
      }

      set({
        cycle,
        cycleTime: apiCycleTime,
        stages,
        queue,
        queueError,
        isPolling: false,
        error: queueError,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '刷新监控数据失败')
      set({ error: message, isPolling: false })
      throw error
    }
  },
  fetchJobs: async (filters) => {
    const { source, cycleTime, jobFilters } = get()
    const nextFilters = { ...jobFilters, ...filters }
    const apiCycleTime = cycleTimeForApi(cycleTime)
    set({ jobFilters: nextFilters, isJobsLoading: true, error: null })

    try {
      const page = await getJobsPage(source, apiCycleTime, nextFilters)
      set({
        jobs: page.items.map(normalizeJob),
        jobTotal: page.total,
        isJobsLoading: false,
        error: null,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '获取作业列表失败')
      set({ error: message, isJobsLoading: false })
      throw error
    }
  },
}))
