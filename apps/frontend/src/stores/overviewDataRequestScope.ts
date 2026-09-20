import { retimeLayerStates } from '@/lib/m11/overviewDataContracts'
import { defaultM11QueryState, serializeM11QueryState, type M11QueryState } from '@/lib/m11/queryState'
import type { M11OverviewRequestScope, OverviewBootstrapSnapshot, OverviewDataSnapshot } from '@/stores/overviewDataTypes'

export function cacheKey(path: string, params?: unknown) {
  return `${path}:${JSON.stringify(params ?? {})}`
}

/**
 * 降水叠加是纯渲染开关，不参与任何取数身份：把 `precip` 带进 store 的 query 会让降水开关
 * 整轮重载 overview（`overviewRequestNonce` 递增 → 在途的 cycles / valid-times / precip index
 * enrichment 全部作废）。取数入口一律先经此归一。
 */
export function dataIdentityQuery(query: M11QueryState): M11QueryState {
  return query.precip === defaultM11QueryState.precip ? query : { ...query, precip: defaultM11QueryState.precip }
}

/**
 * 请求身份 = 归一后的取数 query 去掉 `validTime`（与 `requestScopeQueryKey` / `requestScopeDataKey`
 * 的切分同形）。`loadOverview` 里没有任何一条请求以 `validTime` 为键：把它带进身份，时间轴每步
 * 都会整轮重载（nonce 递增、清错误、作废在途 enrichment、重发失败端点——#2127）。
 */
export function overviewRequestIdentityKey(query: M11QueryState) {
  return cacheKey('overview', { ...query, validTime: null })
}

/** 冻结的 bootstrap 快照只换 `validTime`，绝不从活的 cycles / valid-times 记录重建（design D1）。 */
export function retimeBootstrapSnapshot(snapshot: OverviewBootstrapSnapshot, query: M11QueryState): OverviewBootstrapSnapshot {
  const layerStates = retimeLayerStates(snapshot.layerStates, query.validTime)
  return {
    ...snapshot,
    layerStates,
    currentLayerValidTime: layerStates.find((state) => state.layerId === query.layer)?.currentValidTime ?? null,
  }
}

function requestScopeQueryKey(query: M11QueryState) {
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basemap: defaultM11QueryState.basemap,
    validTime: null,
  })
}

function requestScopeDataKey(query: M11QueryState) {
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basemap: defaultM11QueryState.basemap,
  })
}

export function overviewRequestScope(query: M11QueryState): M11OverviewRequestScope {
  return {
    kind: 'overview',
    queryKey: requestScopeQueryKey(query),
    dataKey: requestScopeDataKey(query),
    source: query.source,
    layer: query.layer,
    cycle: query.cycle,
    validTime: query.validTime,
    basemap: query.basemap,
    basinVersionId: query.basinVersionId,
    riverNetworkVersionId: query.riverNetworkVersionId,
    segmentId: query.segmentId,
    q: query.q,
  }
}

export function overviewSnapshotMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.dataKey === requestScopeDataKey(query)
}

export function overviewSnapshotMetadataMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.queryKey === requestScopeQueryKey(query)
}
