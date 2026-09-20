// M11 overview 数据契约的 barrel（#2102 拆分）：实现按族分到下列同目录子模块，
// 本文件只做导出聚合。导出名与签名与拆分前逐字相同；子模块之间直接互相 import，
// 不经过本文件。显式列表而非 `export *`：子模块为跨模块协作新导出的内部 helper
// 不得因此进入公共面。

export {
  m11BasinGeometryBudget,
  m11SelectedSegmentGeometryBudget,
  type AggregationEndpointDecision,
  type AggregationEndpointDecisionInput,
  type AggregationEndpointDecisionReason,
  type ApiBasin,
  type ApiBasinVersion,
  type ApiHydroRun,
  type ApiHydroRunPage,
  type ApiLayer,
  type ApiModelInstance,
  type ApiPipelineStatus,
  type ApiQueueDepth,
  type BasinVersionOption,
  type FreshnessMetadata,
  type LayerLegendEntry,
  type LayerState,
  type M11BasinGeometryBudgetStatus,
  type M11Bbox,
  type M11ResolvedSource,
  type M11SelectedSegmentGeometryBudgetStatus,
  type OverviewBasin,
  type OverviewSummary,
  type SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContractTypes'
export { getM11BasinGeometryBudgetStatus, getM11SelectedSegmentGeometryBudgetStatus } from '@/lib/m11/overviewDataGeometryBudget'
export {
  activeCycleValidTimesErrorDisabledReason,
  cycleNotListedDischargeDisabledReason,
  failClosedDischargeDisabledReason,
  getM11LayerLegend,
  isFailClosedDischargeMetadata,
  isM11ActiveCycleValidTimesUnresolved,
  m11DischargeColor,
  mergeLayerCatalogs,
  mergeLayerStates,
  normalizeLayerStates,
  pendingActiveCycleValidTimesDisabledReason,
  resolveLayerValidTimesFromMetadata,
  retimeLayerStates,
  type ActiveCycleValidTimesOverride,
} from '@/lib/m11/overviewDataLayerStates'
export {
  createEmptyOverviewSummary,
  createFreshnessMetadata,
  createSourceScenarioSelection,
  decideAggregationEndpoint,
  normalizeOverviewBasins,
  normalizeOverviewSummary,
  resolveNationalScaleSource,
} from '@/lib/m11/overviewDataNormalizers'
