import { create } from 'zustand'

import type { PipelineStatus } from '@/lib/constants'

export interface PipelineStage {
  id: string
  name: string
  status: PipelineStatus
  startedAt?: string
  finishedAt?: string
  durationMs?: number
}

export interface PipelineJob {
  id: string
  stageId: string
  status: PipelineStatus
  submittedAt: string
  durationMs?: number
}

export interface QueueState {
  running: number
  pending: number
  failed: number
  succeeded: number
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
