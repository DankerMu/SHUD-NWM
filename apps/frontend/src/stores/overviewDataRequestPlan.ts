import type { ApiHydroRun, ApiHydroRunPage } from '@/lib/m11/overviewDataContracts'
import { isDataShapeError, type DataShapeError } from '@/lib/m11/overviewShapeGuards'
import type { M11QueryState } from '@/lib/m11/queryState'
import {
  READY_RUN_STATUSES,
  type OverviewRequestPlan,
  type ReadyRunPage,
  type ReadyRunStatus,
  type ReadyRunStatusPages,
} from '@/stores/overviewDataTypes'

const OVERVIEW_INITIAL_REQUEST_THRESHOLD = 8

export function safeM11ErrorMessage(label: string, fallback = '暂不可用') {
  return `${label}: ${fallback}`
}

export function settledValue<T>(
  result: PromiseSettledResult<T>,
  errors: string[],
  label: string,
  onShapeError?: (error: DataShapeError) => void,
): T | null {
  if (result.status === 'fulfilled') return result.value
  if (isDataShapeError(result.reason)) {
    errors.push(safeM11ErrorMessage(label, '数据异常'))
    onShapeError?.(result.reason)
    return null
  }
  errors.push(safeM11ErrorMessage(label))
  return null
}

/** bootstrap 单侧失败的措辞：形状错误 = 数据异常，其余（API / 网络）照旧 = 暂不可用。 */
export function bootstrapFailureWord(result: PromiseSettledResult<unknown>): string {
  return result.status === 'rejected' && isDataShapeError(result.reason) ? '数据异常' : '暂不可用'
}

export function latestPublishedRun(runs: ApiHydroRunPage | null, query: M11QueryState | undefined): ApiHydroRun | null {
  const items = runs?.items ?? []
  const candidates = query?.source === 'best' ? items.filter((run) => concreteSourceFromRun(run)) : items
  return [...candidates].sort((a, b) => {
    const bCycleTime = Date.parse(b.cycle_time ?? '')
    const aCycleTime = Date.parse(a.cycle_time ?? '')
    const cycleOrder = (Number.isFinite(bCycleTime) ? bCycleTime : 0) - (Number.isFinite(aCycleTime) ? aCycleTime : 0)
    if (cycleOrder !== 0) return cycleOrder

    const bUpdateTime = Date.parse(b.updated_at ?? b.created_at)
    const aUpdateTime = Date.parse(a.updated_at ?? a.created_at)
    return (Number.isFinite(bUpdateTime) ? bUpdateTime : 0) - (Number.isFinite(aUpdateTime) ? aUpdateTime : 0)
  })[0] ?? null
}

export function mergeRunPages(
  pages: ApiHydroRunPage[],
  statuses: readonly ReadyRunStatus[] = READY_RUN_STATUSES,
): ReadyRunPage {
  const byRunId = new Map<string, ApiHydroRun>()
  pages.forEach((page) => {
    page.items.forEach((run) => {
      byRunId.set(run.run_id, run)
    })
  })
  const limit = Math.max(...pages.map((page) => page.limit ?? page.items.length), 0)
  const total = Math.max(...pages.map((page) => page.total ?? page.items.length), byRunId.size)
  const readyStatusPages = statuses.reduce<ReadyRunStatusPages>((acc, status, index) => {
    const page = pages[index]
    if (page) acc[status] = page
    return acc
  }, {})
  return {
    items: [...byRunId.values()],
    total,
    limit,
    offset: pages[0]?.offset ?? 0,
    readyStatusPages,
  }
}

export function shouldUseSingleRunSurfaces(query: M11QueryState) {
  return query.source !== 'compare'
}

export function sourceForApi(source: M11QueryState['source']) {
  if (source === 'ifs') return 'IFS'
  if (source === 'best') return undefined
  if (source === 'compare') return undefined
  return 'GFS'
}

function concreteSourceFromRun(run: ApiHydroRun | null | undefined): 'gfs' | 'ifs' | null {
  const value = `${run?.source_id ?? ''} ${run?.scenario_id ?? ''}`.toLowerCase()
  if (value.includes('ifs')) return 'ifs'
  if (value.includes('gfs')) return 'gfs'
  return null
}

export function concreteQueryForSurfaces(query: M11QueryState, run: ApiHydroRun | null): M11QueryState {
  if (query.source !== 'best') return query
  const source = concreteSourceFromRun(run)
  return source ? { ...query, source } : query
}

export function runsForSourceSelection(query: M11QueryState, runs: ApiHydroRun[], latestRun: ApiHydroRun | null): ApiHydroRun[] {
  return query.source === 'best' ? (latestRun ? [latestRun] : []) : runs
}

export function pipelineRequestParams(query: M11QueryState, run: ApiHydroRun | null = null): { source: string; cycle: string } | null {
  if (query.source === 'compare') return null
  const concreteQuery = concreteQueryForSurfaces(query, run)
  const source = sourceForApi(concreteQuery.source)
  // 回退到 run 的周期对所有非 compare 源生效：默认源由 `best` 翻成 `gfs` 后，若仍只在 best 分支
  // 回退，默认全国总览的 cycle 恒为 null，`/api/v1/pipeline/status` 会静默不再发出（摘要卡片空掉）。
  const cycle = query.cycle ?? run?.cycle_time ?? null
  return source && cycle ? { source, cycle } : null
}

export function buildOverviewRequestPlan(
  query: M11QueryState,
  basinCount: number,
  _hasLatestRun: boolean,
  hasPipelineRequest: boolean,
): OverviewRequestPlan {
  const layerValidTimeRequestCount = 0
  const pipelineRequestCount = hasPipelineRequest ? 1 : 0
  const baseRequestCount = 5
  const initialWithoutVersions = baseRequestCount + pipelineRequestCount + layerValidTimeRequestCount
  const createsPerBasinNPlusOne = basinCount > 1
  const plannedVersionRequestCount = basinCount === 1 ? 1 : 0
  const initialRequestCount = initialWithoutVersions + plannedVersionRequestCount
  const shouldFetchVersions =
    plannedVersionRequestCount === basinCount &&
    !createsPerBasinNPlusOne &&
    initialRequestCount <= OVERVIEW_INITIAL_REQUEST_THRESHOLD
  const versionRequestCount = shouldFetchVersions ? plannedVersionRequestCount : 0
  const missingRequiredFields = basinCount > 1 ? ['basin_versions', 'basin_bbox'] : []
  return {
    baseRequestCount,
    layerValidTimeRequestCount,
    versionRequestCount,
    pipelineRequestCount,
    initialRequestCount,
    createsPerBasinNPlusOne,
    missingRequiredFields,
    shouldFetchVersions,
  }
}
