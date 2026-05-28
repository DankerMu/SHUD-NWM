import { useEffect, useState } from 'react'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { components } from '@/api/types'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'
import type { MonitoringStrictIdentity } from '@/stores/monitoring'

type JobLogs = components['schemas']['JobLogs']

interface LogModalProps {
  jobId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  refreshKey?: number
  strictIdentity?: MonitoringStrictIdentity | null
}

export function LogModal({ jobId, open, onOpenChange, refreshKey = 0, strictIdentity = null }: LogModalProps) {
  const [content, setContent] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setContent('')
    setError(null)
    if (!open || !jobId) {
      setLoading(false)
      return
    }

    let active = true
    setLoading(true)

    async function fetchLogs() {
      const params = strictIdentity
        ? {
          path: { job_id: jobId as string },
          query: {
            source: strictIdentity.source,
            cycle_time: strictIdentity.cycleTime,
            run_id: strictIdentity.runId,
            model_id: strictIdentity.modelId,
          },
        }
        : { path: { job_id: jobId as string } }
      const { data, error } = await client.GET('/api/v1/jobs/{job_id}/logs', { params })
      if (error) throw new Error(getApiErrorMessage(error, '日志加载失败'))
      const logs = unwrapApiData<JobLogs>(data, '日志加载失败')
      if (logs.job_id !== jobId) throw new Error('日志身份与当前作业不匹配')
      return logs
    }

    fetchLogs()
      .then((logs) => {
        if (!active) return
        setContent(logs.content || '(空日志)')
      })
      .catch((error) => {
        if (!active) return
        setError(sanitizeHydroMetMessage(getApiErrorMessage(error, '日志加载失败'), '日志加载失败'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [jobId, open, refreshKey, strictIdentity])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-4xl">
        <DialogHeader>
          <DialogTitle>作业日志 {jobId ?? ''}</DialogTitle>
        </DialogHeader>
        <pre className="max-h-[65vh] overflow-auto rounded-md border border-border bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100">
          {loading ? '加载中...' : error ? `加载失败: ${error}` : content}
        </pre>
      </DialogContent>
    </Dialog>
  )
}
