import {
  C4_HOME_READ_API_PATHS,
  C4_JOBS_PATH,
  C4_PIPELINE_STAGES_PATH,
  C4_PIPELINE_STATUS_PATH,
  C4_RUNTIME_CONFIG_PATH,
} from './constants'
import { normalizeRiverClickCycleTime } from '../riverClickEvidence/preflight'
import type { C4ProductIdentity } from './receipt'

export type C4OpsRequestKind = 'status' | 'stages' | 'jobs' | 'logs'

export interface C4OpsRequestMatch {
  matched: true
  kind: C4OpsRequestKind
  jobId?: string
}

function safeDecode(component: string): string | null {
  try {
    return decodeURIComponent(component)
  } catch {
    return null
  }
}

function singleQuery(parsed: URL, key: string): string | null {
  const values = parsed.searchParams.getAll(key)
  if (values.length !== 1) return null
  return values[0]
}

export function isC4RuntimeConfigUrl(url: string, apiOrigin: string): boolean {
  try {
    const parsed = new URL(url)
    return parsed.origin === apiOrigin && parsed.pathname === C4_RUNTIME_CONFIG_PATH
  } catch {
    return false
  }
}

export function isC4HomeReadApiUrl(url: string, apiOrigin: string): boolean {
  try {
    const parsed = new URL(url)
    if (parsed.origin !== apiOrigin) return false
    return (C4_HOME_READ_API_PATHS as readonly string[]).includes(parsed.pathname)
  } catch {
    return false
  }
}

export function isC4ApplicationApiUrl(url: string, apiOrigin: string): boolean {
  try {
    const parsed = new URL(url)
    if (parsed.origin !== apiOrigin) return false
    return parsed.pathname.startsWith('/api/v1/')
  } catch {
    return false
  }
}

export function classifyC4ControlRequest(method: string, url: string, apiOrigin: string) {
  try {
    const parsed = new URL(url)
    if (parsed.origin !== apiOrigin) return null
    const normalizedMethod = method.toUpperCase()
    if (parsed.pathname.startsWith('/api/v1/slurm/')) return 'forbidden-slurm-control' as const
    if (normalizedMethod !== 'GET' && normalizedMethod !== 'HEAD') return 'forbidden-api-mutation' as const
  } catch {
    return null
  }
  return null
}

function identityQueryMatches(parsed: URL, product: C4ProductIdentity): boolean {
  const source = singleQuery(parsed, 'source')
  const cycleTime = singleQuery(parsed, 'cycle_time')
  const runId = singleQuery(parsed, 'run_id')
  const modelId = singleQuery(parsed, 'model_id')
  if (source !== product.sourceId) return false
  if (runId !== product.runId || modelId !== product.modelId) return false
  if (cycleTime === null) return false
  const canonical = normalizeRiverClickCycleTime(cycleTime)
  if (canonical === null) return false
  return canonical === product.cycleTime
}

export function matchC4OpsRequest(
  method: string,
  url: string,
  apiOrigin: string,
  product: C4ProductIdentity,
): C4OpsRequestMatch | { matched: false } {
  if (method.toUpperCase() !== 'GET') return { matched: false }
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return { matched: false }
  }
  if (parsed.origin !== apiOrigin) return { matched: false }
  const pathname = safeDecode(parsed.pathname)
  if (pathname === null) return { matched: false }
  if (!identityQueryMatches(parsed, product)) return { matched: false }
  if (pathname === C4_PIPELINE_STATUS_PATH) return { matched: true, kind: 'status' }
  if (pathname === C4_PIPELINE_STAGES_PATH) return { matched: true, kind: 'stages' }
  if (pathname === C4_JOBS_PATH) return { matched: true, kind: 'jobs' }
  const logs = pathname.match(/^\/api\/v1\/jobs\/([^/]+)\/logs$/)
  if (logs) return { matched: true, kind: 'logs', jobId: logs[1] }
  return { matched: false }
}

export function extractC4JobIdFromJobsPayload(
  payload: unknown,
  product: C4ProductIdentity,
): string | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const envelope = payload as Record<string, unknown>
  const data = envelope.status === 'ok' && envelope.data && typeof envelope.data === 'object' && !Array.isArray(envelope.data)
    ? envelope.data as Record<string, unknown>
    : envelope
  const items = data.items
  if (!Array.isArray(items) || items.length === 0) return null

  const selected: string[] = []
  const seen = new Set<string>()
  for (const item of items) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const row = item as Record<string, unknown>
    if (row.run_id !== product.runId || row.model_id !== product.modelId) continue
    if (typeof row.job_id !== 'string' || row.job_id.length === 0 || seen.has(row.job_id)) return null
    seen.add(row.job_id)
    selected.push(row.job_id)
  }
  if (selected.length === 0) return null
  return selected[0]
}
