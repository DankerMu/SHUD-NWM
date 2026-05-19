import {
  getMeteorologyGridContract,
  getMeteorologyStationSeries,
  meteorologyStations,
  stationInventoryLimits,
  type MeteorologyGridContract,
  type MeteorologyStation,
  type MeteorologyStationSeries,
} from '@/lib/meteorology/contracts'
import {
  type MeteorologyQueryState,
  type MeteorologySource,
  type MeteorologyVariable,
} from '@/lib/meteorology/queryState'

export interface GridViewModel {
  contract: MeteorologyGridContract
  correctedValidTime: string | null | undefined
  canRenderTile: boolean
  timelineDisabledReason: string | null
  comparisonStatus: string
  areaStatsStatus: string
  cellPopup: {
    lon: number
    lat: number
    value: null
    reason: string
  } | null
}

export interface StationInventoryViewModel {
  rows: MeteorologyStation[]
  selectedStation: MeteorologyStation | null
  selectedSeries: MeteorologyStationSeries | null
  emptyReason: string | null
  truncated: boolean
  validationReason: string | null
}

export function buildMeteorologyGridViewModel(state: Pick<MeteorologyQueryState, 'variable' | 'source' | 'validTime' | 'compareSource'>): GridViewModel {
  const contract = getMeteorologyGridContract(state.variable, state.source)
  const correctedValidTime = resolveMeteorologyValidTimeCorrection(state.validTime, contract)
  const visibleValidTime = correctedValidTime === undefined ? state.validTime : correctedValidTime
  const canRenderTile = Boolean(contract.tileUrlTemplate && visibleValidTime && !contract.restrictedReason && !contract.unavailableReason)
  const timelineDisabledReason =
    contract.restrictedReason ??
    (contract.validTimes.length === 0 ? '该产品没有合同提供的 valid_time，时间轴已禁用。' : null)

  return {
    contract,
    correctedValidTime,
    canRenderTile,
    timelineDisabledReason,
    comparisonStatus: comparisonStatus(state.variable, state.source, state.compareSource, visibleValidTime),
    areaStatsStatus: contract.maxAreaKm2 <= 0
      ? '区域统计不可用：合同未开放该源的面积查询。'
      : `区域统计请求上限 ${contract.maxAreaKm2.toLocaleString('en-US')} km2，超过 bbox/resolution 限制时返回 validation 状态。`,
    cellPopup: visibleValidTime && !contract.restrictedReason
      ? {
          lon: 114.35,
          lat: 30.62,
          value: null,
          reason: '格点查询端点尚未返回实测/预报值，UI 不生成替代数值。',
        }
      : null,
  }
}

export function resolveMeteorologyValidTimeCorrection(validTime: string | null, contract: MeteorologyGridContract) {
  if (contract.validTimes.length === 0) return validTime ? null : undefined
  if (validTime && contract.validTimes.includes(validTime)) return undefined
  return contract.currentValidTime
}

function comparisonStatus(
  variable: MeteorologyVariable,
  source: MeteorologySource,
  compareSource: MeteorologySource | null,
  validTime: string | null | undefined,
) {
  if (!compareSource) return '选择第二数据源后显示多源对比支持状态。'
  if (source === compareSource) return '对比源与主源相同，未计算差值。'
  const primary = getMeteorologyGridContract(variable, source)
  const secondary = getMeteorologyGridContract(variable, compareSource)
  if (!primary.supportsComparison || !secondary.supportsComparison) return '该源组合不支持对比，未计算伪差值。'
  if (!validTime || !secondary.validTimes.includes(validTime)) return '对比源缺少相同 valid_time，未计算伪差值。'
  return `${source} 与 ${compareSource} 在 ${validTime} 具备合同可比性；等待真实差值服务。`
}

export function buildStationInventoryViewModel(state: Pick<MeteorologyQueryState, 'basin' | 'search' | 'sort' | 'stationId'>): StationInventoryViewModel {
  const validationReason = state.search && state.search.length > stationInventoryLimits.searchMaxLength
    ? `搜索词超过 ${stationInventoryLimits.searchMaxLength} 字符，已按合同截断。`
    : null
  const search = state.search?.toLowerCase() ?? null
  let rows = meteorologyStations.filter((station) => {
    if (state.basin && station.basinId !== state.basin) return false
    if (!search) return true
    return station.stationId.toLowerCase().includes(search) || station.stationName.toLowerCase().includes(search)
  })

  rows = [...rows].sort((a, b) => {
    if (state.sort === 'station_id') return a.stationId.localeCompare(b.stationId)
    if (state.sort === 'completeness') return b.completeness - a.completeness
    return Date.parse(b.latestDataTime ?? '1970-01-01T00:00:00.000Z') - Date.parse(a.latestDataTime ?? '1970-01-01T00:00:00.000Z')
  })

  const truncated = rows.length > stationInventoryLimits.pageSize
  rows = rows.slice(0, stationInventoryLimits.pageSize)
  const selectedStation = rows.find((station) => station.stationId === state.stationId) ?? rows[0] ?? null
  const selectedSeries = selectedStation ? getMeteorologyStationSeries(selectedStation.stationId) : null

  return {
    rows,
    selectedStation,
    selectedSeries,
    emptyReason: rows.length === 0 ? '搜索无结果，未渲染伪造站点。' : null,
    truncated,
    validationReason,
  }
}
