import type { ApiHydroRun, M11ResolvedSource } from '@/lib/m11/overviewDataContractTypes'

export function normalizeString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null
}

export function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const numberValue = Number(value)
  return Number.isFinite(numberValue) ? numberValue : null
}

export function finiteNumberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function sumNullable(values: Array<number | null>): number | null {
  const usable = values.filter((value): value is number => value !== null)
  return usable.length > 0 ? usable.reduce((total, value) => total + value, 0) : null
}

export function normalizeIsoString(value: unknown): string | null {
  const stringValue = normalizeString(value)
  if (!stringValue) return null
  const timestamp = Date.parse(stringValue)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : stringValue
}

export function latestIso(values: Array<string | null | undefined>): string | null {
  const timestamps = values
    .map((value) => {
      const normalized = normalizeIsoString(value)
      return normalized ? { normalized, timestamp: Date.parse(normalized) } : null
    })
    .filter((entry): entry is { normalized: string; timestamp: number } => entry !== null && Number.isFinite(entry.timestamp))
  timestamps.sort((a, b) => b.timestamp - a.timestamp)
  return timestamps[0]?.normalized ?? null
}

export function isStale(value: unknown, staleAfterHours: number): boolean {
  const normalized = normalizeIsoString(value)
  if (!normalized) return false
  return Date.now() - Date.parse(normalized) > staleAfterHours * 3_600_000
}

function sourceFromScenario(scenarioId: string, explicitSource?: string | null): M11ResolvedSource {
  const value = `${explicitSource ?? ''} ${scenarioId}`.toLowerCase()
  if (value.includes('ifs')) return 'IFS'
  if (value.includes('gfs')) return 'GFS'
  return 'Unknown'
}

export function sourcesFromRuns(runs: ApiHydroRun[]): M11ResolvedSource[] {
  return [
    ...new Set(
      runs
        .map((run) => sourceFromScenario(run.scenario_id, run.source_id))
        .filter((source): source is M11ResolvedSource => source !== 'Unknown'),
    ),
  ]
}
