import { create } from 'zustand'

import type { PipelineStatus } from '@/lib/constants'

export interface PipelineStage {
  stage: string
  display_status: PipelineStatus
  status?: PipelineStatus
  duration_seconds: number | null
  basin_progress: {
    completed: number
    total: number
    failed: number
  }
  basin_results: Array<{
    model_id: string | null
    basin_id: string | null
    status: string
    error_code: string | null
    error_message: string | null
  }>
}

export interface PipelineJob {
  job_id: string
  run_id: string | null
  cycle_id: string | null
  run_type: string | null
  scenario: string | null
  job_type: string
  model_id: string | null
  stage: string | null
  status: string
  slurm_job_id: string | null
  submitted_at: string | null
  started_at: string | null
  finished_at: string | null
  completed_at?: string | null
  duration_seconds: number | null
  error_code: string | null
  error_message: string | null
  exit_code: number | null
  retry_count: number
  log_uri: string | null
}

export interface QueueState {
  running: number
  pending: number
  idle: number
}

export interface JobFilters {
  status?: PipelineStatus
  runType?: string
  scenario?: string
  page?: number
  pageSize?: number
}

interface MonitoringState {
  source: string
  cycleTime: string | null
  stages: PipelineStage[]
  jobs: PipelineJob[]
  jobTotal: number
  queue: QueueState | null
  isPolling: boolean
  error: string | null
  setSource: (source: string) => void
  setCycleTime: (cycleTime: string | null) => void
  fetchAll: () => Promise<void>
  fetchJobs: (filters?: JobFilters) => Promise<void>
}

export const useMonitoringStore = create<MonitoringState>((set) => ({
  source: 'GFS',
  cycleTime: null,
  stages: [],
  jobs: [],
  jobTotal: 0,
  queue: null,
  isPolling: false,
  error: null,
  setSource: (source) => set({ source }),
  setCycleTime: (cycleTime) => set({ cycleTime }),
  fetchAll: async () => {
    set({ error: null })
  },
  fetchJobs: async () => {
    set({ error: null })
  },
}))
