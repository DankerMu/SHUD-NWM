import { create } from 'zustand'

import type { components } from '@/api/types'
import type { JobStatus, PipelineStatus } from '@/lib/constants'

export type PipelineJob = components['schemas']['PipelineJob']

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

export interface QueueState {
  running: number
  pending: number
  idle: number
}

export interface JobFilters {
  status?: JobStatus
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
