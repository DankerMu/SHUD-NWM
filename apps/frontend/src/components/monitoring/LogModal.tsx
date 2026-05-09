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

type JobLogs = components['schemas']['JobLogs']

interface LogModalProps {
  jobId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function LogModal({ jobId, open, onOpenChange }: LogModalProps) {
  const [content, setContent] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open || !jobId) return

    let active = true
    setLoading(true)
    setError(null)
    setContent('')

    async function fetchLogs() {
      const { data, error } = await client.GET('/api/v1/jobs/{job_id}/logs', {
        params: { path: { job_id: jobId as string } },
      })
      if (error) throw new Error(getApiErrorMessage(error, '日志加载失败'))
      return unwrapApiData<JobLogs>(data, '日志加载失败')
    }

    fetchLogs()
      .then((logs) => {
        if (!active) return
        setContent(logs.content || '(空日志)')
      })
      .catch((error) => {
        if (!active) return
        setError(getApiErrorMessage(error, '日志加载失败'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [jobId, open])

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
