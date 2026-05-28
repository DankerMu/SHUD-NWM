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
export type RuntimeConfig = components['schemas']['RuntimeConfig']

export type QueueState = components['schemas']['QueueDepth']

export interface MonitoringPayloadContext {
  source: string
  cycleTime: string
  runId?: string
  modelId?: string
}

export interface MonitoringStrictIdentity {
  source: string
  cycleTime: string
  runId: string
  modelId: string
}

export interface MonitoringFetchOptions {
  clearOnFailure?: boolean
}

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
  cycleContext: MonitoringPayloadContext | null
  strictIdentity: MonitoringStrictIdentity | null
  stages: PipelineStage[]
  jobs: PipelineJob[]
  jobsContext: MonitoringPayloadContext | null
  jobTotal: number
  queue: QueueState | null
  queueError: string | null
  operationalError: string | null
  jobsError: string | null
  jobFilters: JobFilters
  isPolling: boolean
  isJobsLoading: boolean
  error: string | null
  runtimeConfig: RuntimeConfig | null
  runtimeConfigError: string | null
  setSource: (source: string) => void
  setCycleTime: (cycleTime: string | null) => void
  setStrictIdentity: (identity: MonitoringStrictIdentity | null) => void
  clearSelectedContext: () => void
  fetchRuntimeConfig: () => Promise<void>
  fetchAll: (options?: MonitoringFetchOptions) => Promise<void>
  fetchJobs: (filters?: JobFilters, options?: MonitoringFetchOptions) => Promise<void>
}

let fetchAllRequestSeq = 0
let fetchJobsRequestSeq = 0

export function defaultMonitoringCycleTime() {
  const date = new Date()
  date.setUTCMinutes(0, 0, 0)
  return date.toISOString().slice(0, 19) + 'Z'
}

export function normalizeMonitoringCycleTime(cycleTime: string | null | undefined) {
  const value = cycleTime?.trim()
  if (!value) return defaultMonitoringCycleTime()
  if (value.length === 16) {
    const candidate = `${value}:00Z`
    const date = new Date(candidate)
    return Number.isNaN(date.getTime()) ? candidate : date.toISOString()
  }
  return value
}

function emptySelectedContext() {
  return {
    cycle: null,
    cycleContext: null,
    stages: [],
    jobs: [],
    jobsContext: null,
    jobTotal: 0,
  }
}

export function monitoringPayloadContext(source: string, cycleTime: string): MonitoringPayloadContext {
  return {
    source: source.toUpperCase(),
    cycleTime: normalizeMonitoringCycleTime(cycleTime),
  }
}

function strictIdentityContext(identity: MonitoringStrictIdentity): MonitoringPayloadContext {
  return {
    source: identity.source.toUpperCase(),
    cycleTime: normalizeMonitoringCycleTime(identity.cycleTime),
    runId: identity.runId,
    modelId: identity.modelId,
  }
}

function requestPayloadContext(source: string, cycleTime: string, identity: MonitoringStrictIdentity | null) {
  return identity ? strictIdentityContext(identity) : monitoringPayloadContext(source, cycleTime)
}

export function monitoringContextMatches(
  context: MonitoringPayloadContext | null,
  source: string,
  cycleTime: string,
  identity?: MonitoringStrictIdentity | null,
) {
  if (!context) return false
  const expected = requestPayloadContext(source, cycleTime, identity ?? null)
  const baseMatches = context.source === expected.source && context.cycleTime === expected.cycleTime
  if (!baseMatches) return false
  if (!expected.runId && !expected.modelId) return true
  return context.runId === expected.runId && context.modelId === expected.modelId
}

function strictIdentityMatches(left: MonitoringStrictIdentity | null, right: MonitoringStrictIdentity | null) {
  if (!left && !right) return true
  if (!left || !right) return false
  return (
    left.source.toUpperCase() === right.source.toUpperCase() &&
    normalizeMonitoringCycleTime(left.cycleTime) === normalizeMonitoringCycleTime(right.cycleTime) &&
    left.runId === right.runId &&
    left.modelId === right.modelId
  )
}

function isCurrentContext(source: string, cycleTime: string, identity: MonitoringStrictIdentity | null) {
  const state = useMonitoringStore.getState()
  return (
    state.source === source &&
    normalizeMonitoringCycleTime(state.cycleTime) === cycleTime &&
    strictIdentityMatches(state.strictIdentity, identity)
  )
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

function strictIdentityQuery(identity: MonitoringStrictIdentity | null) {
  return identity ? { run_id: identity.runId, model_id: identity.modelId } : {}
}

function validateRuntimeConfig(value: RuntimeConfig) {
  const role = value?.service_role
  if (
    role !== 'dev_monolith' &&
    role !== 'compute_control' &&
    role !== 'display_readonly' &&
    role !== 'slurm_gateway'
  ) {
    throw new Error('runtime config 响应缺少有效 service_role')
  }
  return value
}

async function getRuntimeConfig() {
  const { data, error } = await client.GET('/api/v1/runtime/config')
  if (error) throw new Error(getApiErrorMessage(error, 'runtime config 加载失败'))
  return validateRuntimeConfig(unwrapApiData<RuntimeConfig>(data, 'runtime config 加载失败'))
}

async function getPipelineStatus(source: string, cycleTime: string, identity: MonitoringStrictIdentity | null) {
  const { data, error } = await client.GET('/api/v1/pipeline/status', {
    params: { query: { source, cycle_time: cycleTime, ...strictIdentityQuery(identity) } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取周期状态失败'))
  return unwrapApiData<PipelineCycle>(data, '获取周期状态失败')
}

function validateStrictStages(stages: PipelineStage[], identity: MonitoringStrictIdentity | null) {
  if (!identity) return stages
  const mismatches = stages.flatMap((stage) =>
    (stage.basin_results ?? []).filter((result) => (
      result.run_id !== null &&
      result.model_id !== null &&
      (result.run_id !== identity.runId || result.model_id !== identity.modelId)
    )),
  )
  if (mismatches.length > 0) {
    throw new Error(`strict identity mismatch：阶段失败明细不属于 run_id=${identity.runId} / model_id=${identity.modelId}`)
  }
  return stages
}

async function getPipelineStages(source: string, cycleTime: string, identity: MonitoringStrictIdentity | null) {
  const { data, error } = await client.GET('/api/v1/pipeline/stages', {
    params: { query: { source, cycle_time: cycleTime, ...strictIdentityQuery(identity) } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取阶段状态失败'))
  return validateStrictStages(unwrapApiData<PipelineStage[]>(data, '获取阶段状态失败'), identity)
}

async function getQueueDepth() {
  const { data, error } = await client.GET('/api/v1/queue/depth')
  if (error) throw new Error(getApiErrorMessage(error, '获取队列深度失败'))
  return unwrapApiData<QueueState>(data, '获取队列深度失败')
}

function isDisplayQueueUnavailable(runtimeConfig: RuntimeConfig | null) {
  return runtimeConfig?.queue_depth_mode === 'display_readonly_unavailable'
}

async function getQueueDepthState(runtimeConfig: RuntimeConfig | null) {
  if (isDisplayQueueUnavailable(runtimeConfig)) {
    return { queue: null, queueError: 'display_readonly 模式下 Slurm 队列深度不可用；阶段、作业和日志仍可只读查看。' }
  }

  try {
    return { queue: await getQueueDepth(), queueError: null }
  } catch (error) {
    return { queue: null, queueError: getApiErrorMessage(error, '队列深度暂不可用') }
  }
}

function validateStrictJobs(page: PipelineJobPage, identity: MonitoringStrictIdentity | null) {
  if (!identity) return page
  const mismatches = page.items.filter((job) => job.run_id !== identity.runId || job.model_id !== identity.modelId)
  if (mismatches.length > 0) {
    throw new Error(`strict identity mismatch：作业不属于 run_id=${identity.runId} / model_id=${identity.modelId}`)
  }
  return page
}

async function getJobsPage(source: string, cycleTime: string, filters: JobFilters, identity: MonitoringStrictIdentity | null) {
  const page = filters.page ?? 1
  const pageSize = filters.pageSize ?? 12
  const { data, error } = await client.GET('/api/v1/jobs', {
    params: {
      query: {
        source,
        cycle_time: cycleTime,
        ...strictIdentityQuery(identity),
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
  return validateStrictJobs(unwrapApiData<PipelineJobPage>(data, '获取作业列表失败'), identity)
}

export const useMonitoringStore = create<MonitoringState>((set, get) => ({
  source: 'GFS',
  cycleTime: defaultMonitoringCycleTime(),
  cycle: null,
  cycleContext: null,
  strictIdentity: null,
  stages: [],
  jobs: [],
  jobsContext: null,
  jobTotal: 0,
  queue: null,
  queueError: null,
  operationalError: null,
  jobsError: null,
  jobFilters: { page: 1, pageSize: 12, sortBy: 'submitted_at', sortOrder: 'desc' },
  isPolling: false,
  isJobsLoading: false,
  error: null,
  runtimeConfig: null,
  runtimeConfigError: null,
  setSource: (source) => set((state) => {
    const nextSource = source.toUpperCase()
    if (nextSource === state.source) {
      return { jobFilters: { ...state.jobFilters, page: 1 } }
    }
    return {
      source: nextSource,
      jobFilters: { ...state.jobFilters, page: 1 },
    }
  }),
  setCycleTime: (cycleTime) => set((state) => {
    const nextCycleTime = normalizeMonitoringCycleTime(cycleTime)
    if (nextCycleTime === state.cycleTime) {
      return { jobFilters: { ...state.jobFilters, page: 1 } }
    }
    return {
      cycleTime: nextCycleTime,
      jobFilters: { ...state.jobFilters, page: 1 },
    }
  }),
  setStrictIdentity: (identity) => set({
    strictIdentity: identity
      ? {
        source: identity.source.toUpperCase(),
        cycleTime: normalizeMonitoringCycleTime(identity.cycleTime),
        runId: identity.runId,
        modelId: identity.modelId,
      }
      : null,
  }),
  clearSelectedContext: () => {
    fetchAllRequestSeq += 1
    fetchJobsRequestSeq += 1
    set({
      ...emptySelectedContext(),
      operationalError: null,
      jobsError: null,
      error: null,
      isPolling: false,
      isJobsLoading: false,
    })
  },
  fetchRuntimeConfig: async () => {
    try {
      const runtimeConfig = await getRuntimeConfig()
      set({ runtimeConfig, runtimeConfigError: null })
    } catch (error) {
      set({
        runtimeConfig: null,
        runtimeConfigError: getApiErrorMessage(error, 'runtime config 加载失败'),
      })
    }
  },
  fetchAll: async (options) => {
    const requestId = fetchAllRequestSeq + 1
    fetchAllRequestSeq = requestId
    const { source, cycleTime, strictIdentity, runtimeConfig } = get()
    const requestSource = source.toUpperCase()
    const apiCycleTime = normalizeMonitoringCycleTime(cycleTime)
    set((state) => ({ isPolling: true, operationalError: null, queueError: null, error: state.jobsError }))

    try {
      const [cycle, stages] = await Promise.all([
        getPipelineStatus(requestSource, apiCycleTime, strictIdentity),
        getPipelineStages(requestSource, apiCycleTime, strictIdentity),
      ])

      const { queue, queueError } = await getQueueDepthState(runtimeConfig)

      if (requestId !== fetchAllRequestSeq || !isCurrentContext(requestSource, apiCycleTime, strictIdentity)) {
        if (requestId === fetchAllRequestSeq) set({ isPolling: false })
        return
      }

      set({
        cycle,
        cycleContext: requestPayloadContext(requestSource, apiCycleTime, strictIdentity),
        cycleTime: apiCycleTime,
        stages,
        queue,
        queueError,
        isPolling: false,
        operationalError: null,
        error: queueError ?? get().jobsError,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '刷新监控数据失败')
      const { queue, queueError } = await getQueueDepthState(runtimeConfig)

      if (requestId !== fetchAllRequestSeq || !isCurrentContext(requestSource, apiCycleTime, strictIdentity)) {
        if (requestId === fetchAllRequestSeq) set({ isPolling: false })
        return
      }

      if (options?.clearOnFailure) {
        fetchJobsRequestSeq += 1
        set({
          ...emptySelectedContext(),
          cycleTime: apiCycleTime,
          queue,
          queueError,
          operationalError: message,
          jobsError: null,
          error: message,
          isPolling: false,
          isJobsLoading: false,
        })
      } else {
        set({
          cycleTime: apiCycleTime,
          queue,
          queueError,
          operationalError: message,
          error: message,
          isPolling: false,
        })
      }
      throw error
    }
  },
  fetchJobs: async (filters, options) => {
    const requestId = fetchJobsRequestSeq + 1
    fetchJobsRequestSeq = requestId
    const { source, cycleTime, jobFilters, strictIdentity } = get()
    const requestSource = source.toUpperCase()
    const nextFilters = { ...jobFilters, ...filters }
    const apiCycleTime = normalizeMonitoringCycleTime(cycleTime)
    set((state) => ({ jobFilters: nextFilters, isJobsLoading: true, jobsError: null, error: state.operationalError }))

    try {
      const page = await getJobsPage(requestSource, apiCycleTime, nextFilters, strictIdentity)

      if (requestId !== fetchJobsRequestSeq || !isCurrentContext(requestSource, apiCycleTime, strictIdentity)) {
        if (requestId === fetchJobsRequestSeq) set({ isJobsLoading: false })
        return
      }

      set({
        jobs: page.items.map(normalizeJob),
        jobsContext: requestPayloadContext(requestSource, apiCycleTime, strictIdentity),
        jobTotal: page.total,
        isJobsLoading: false,
        jobsError: null,
        error: get().operationalError,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '获取作业列表失败')

      if (requestId !== fetchJobsRequestSeq || !isCurrentContext(requestSource, apiCycleTime, strictIdentity)) {
        if (requestId === fetchJobsRequestSeq) set({ isJobsLoading: false })
        return
      }

      set({
        ...(options?.clearOnFailure ? { jobs: [], jobsContext: null, jobTotal: 0 } : {}),
        jobsError: message,
        error: get().operationalError ?? message,
        isJobsLoading: false,
      })
      throw error
    }
  },
}))
