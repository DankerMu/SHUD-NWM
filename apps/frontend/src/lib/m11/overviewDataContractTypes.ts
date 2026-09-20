import type { components } from '@/api/types'
import type { M11Layer, M11Source } from '@/lib/m11/queryState'

export type ApiBasin = components['schemas']['Basin']
export type ApiBasinVersion = components['schemas']['BasinVersion']
export type ApiModelInstance = components['schemas']['ModelInstance']
export type ApiHydroRun = components['schemas']['HydroRun']
export type ApiHydroRunPage = components['schemas']['HydroRunPage']
export type ApiLayer = components['schemas']['Layer']
export type ApiPipelineStatus = components['schemas']['PipelineStatus']
export type ApiQueueDepth = components['schemas']['QueueDepth']

export type M11ResolvedSource = 'GFS' | 'IFS' | 'GFS+IFS' | 'Unknown'

export interface M11Bbox {
  minLon: number
  minLat: number
  maxLon: number
  maxLat: number
}

export const m11BasinGeometryBudget = {
  maxPolygons: 256,
  maxRings: 1024,
  maxVertices: 50_000,
  maxCoordinateDimensions: 3,
  maxSerializedBytes: 1_000_000,
} as const

export const m11SelectedSegmentGeometryBudget = {
  maxCoordinates: 10_000,
  maxCoordinateDimensions: 3,
  maxSerializedBytes: 250_000,
} as const

export interface M11BasinGeometryBudgetStatus {
  ok: boolean
  reason: string | null
  bbox: M11Bbox | null
  polygonCount: number
  ringCount: number
  vertexCount: number
  serializedBytes: number
  sanitizedGeometry: components['schemas']['GeoJsonMultiPolygon'] | null
}

export interface M11SelectedSegmentGeometryBudgetStatus {
  ok: boolean
  reason: string | null
  coordinateCount: number
  serializedBytes: number
  sanitizedGeometry:
    | components['schemas']['GeoJsonLineString']
    | components['schemas']['GeoJsonMultiLineString']
    | null
}

export interface BasinVersionOption {
  basinVersionId: string
  versionLabel: string
  active: boolean
  validFrom: string | null
  validTo: string | null
  sourceUri: string | null
  boundary: components['schemas']['GeoJsonMultiPolygon'] | null
  bbox: M11Bbox | null
  unavailableReason: string | null
}

export interface FreshnessMetadata {
  updatedAt: string | null
  cycleTime: string | null
  validTime: string | null
  runId: string | null
  basinVersionId: string | null
  riverNetworkVersionId: string | null
  source: M11ResolvedSource | null
  isStale: boolean
  staleAfterHours: number
  unavailableReason: string | null
}

export interface SourceScenarioSelectionState {
  requestedSource: M11Source
  resolvedSource: M11ResolvedSource
  scenarioIds: string[]
  cycleTime: string | null
  validTime: string | null
  comparisonAvailable: boolean
  provenanceLabel: string
  unavailableReason: string | null
}

export interface OverviewBasin {
  basinId: string
  displayName: string
  basinGroup: string | null
  parentBasinId: string | null
  level: number
  boundary: components['schemas']['GeoJsonMultiPolygon'] | null
  bbox: M11Bbox | null
  areaKm2: number | null
  riverCount: number | null
  activeModelCount: number
  latestForecastTime: string | null
  basinVersions: BasinVersionOption[]
  selectedBasinVersionId: string | null
  unavailableReason: string | null
  qualityNote: string | null
}

export interface OverviewSummary {
  completedCyclesToday: number | null
  runningJobs: number | null
  latestUpdate: string | null
  totalBasins: number
  sourceSelection: SourceScenarioSelectionState
  freshness: FreshnessMetadata
  qualityNotes: string[]
  partialErrors: string[]
}

export interface LayerLegendEntry {
  label: string
  color: string
  min?: number | null
  max?: number | null
}

export interface LayerState {
  layerId: M11Layer | string
  displayName: string
  group: 'hydrology' | 'meteorology' | 'base' | 'unknown'
  available: boolean
  metadata: components['schemas']['Layer']['metadata'] | null
  validTimes: string[]
  currentValidTime: string | null
  validTimeSource: 'api' | 'none'
  disabledReason: string | null
  /**
   * store 为**活动源**解析出的全国起报时次（`nationalDischargeActivePair().cycle`），解不出即 null。
   * 与 `validTimes` 同批产出，故「这批时次属于哪个周期」不再是调用方各自推算的事
   * （#2014 决策 13 的孪生要求）：`buildM11RegisteredOverlay` 只读本字段拼瓦片 URL 的 cycle 段，
   * 从而保证 `(source, cycle, valid_time)` 三元组与 store 解析出的活动对逐字同源。
   * 目录 metadata 的 `default_cycle` **不得**再当回落——它是 GFS 专有事实（后端 `list_layers`
   * 签名里没有 `source`），非默认源借它充数就是 `/hydro-national/ifs/<gfs 周期>/…` 这条跨身份 URL。
   * 非全国（run-scoped）图层与无周期维度的图层恒为 null，与它们不消费 `{cycle}` 模板一致。
   */
  activeNationalCycle: string | null
  freshness: FreshnessMetadata
  legend: LayerLegendEntry[]
}

export type AggregationEndpointDecisionReason =
  | 'reuse-existing'
  | 'too-many-initial-requests'
  | 'per-basin-n-plus-one'
  | 'missing-required-field'

export interface AggregationEndpointDecisionInput {
  initialRequestCount: number
  createsPerBasinNPlusOne: boolean
  missingRequiredFields: string[]
}

export interface AggregationEndpointDecision {
  needsAggregationEndpoint: boolean
  reason: AggregationEndpointDecisionReason
  evidence: string
}
