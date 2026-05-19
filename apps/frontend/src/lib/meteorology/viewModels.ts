import {
  getMeteorologyGridContract,
  getMeteorologyStationSeries,
  isLonLatInMeteorologyBbox,
  meteorologyStations,
  projectLonLatToPercent,
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
  areaStats: {
    status: string
    tone: 'warning' | 'info'
    bbox: {
      minLon: number
      minLat: number
      maxLon: number
      maxLat: number
    } | null
    areaKm2: number | null
  }
  cellPopup: {
    lon: number
    lat: number
    left: number
    top: number
    source: MeteorologySource
    cycle: string | null
    validTime: string | null
    unit: string
    nativeTimeResolution: string
    spatialResolution: string
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
  selectedOutOfPage: boolean
  selectionValidationReason: string | null
  validationReason: string | null
}

type GridStateForViewModel = Pick<
  MeteorologyQueryState,
  'variable' | 'source' | 'validTime' | 'gridQueryLon' | 'gridQueryLat' | 'compareSource'
> &
  Partial<Pick<MeteorologyQueryState, 'areaMinLon' | 'areaMinLat' | 'areaMaxLon' | 'areaMaxLat'>>

export function buildMeteorologyGridViewModel(state: GridStateForViewModel): GridViewModel {
  const contract = getMeteorologyGridContract(state.variable, state.source)
  const correctedValidTime = resolveMeteorologyValidTimeCorrection(state.validTime, contract)
  const visibleValidTime = correctedValidTime === undefined ? state.validTime : correctedValidTime
  const canRenderTile = Boolean(contract.tileUrlTemplate && visibleValidTime && !contract.restrictedReason && !contract.unavailableReason)
  const queryPosition = state.gridQueryLon !== null && state.gridQueryLat !== null
    ? projectLonLatToPercent(state.gridQueryLon, state.gridQueryLat, contract.bbox)
    : null
  const cellPopup = buildGridCellPopup(state, contract, visibleValidTime, queryPosition)
  const timelineDisabledReason =
    contract.restrictedReason ??
    (contract.validTimes.length === 0 ? '该产品没有合同提供的 valid_time，时间轴已禁用。' : null)

  return {
    contract,
    correctedValidTime,
    canRenderTile,
    timelineDisabledReason,
    comparisonStatus: comparisonStatus(state.variable, state.source, state.compareSource, visibleValidTime),
    areaStats: buildAreaStatsState(state, contract, visibleValidTime),
    cellPopup,
  }
}

function buildGridCellPopup(
  state: Pick<MeteorologyQueryState, 'gridQueryLon' | 'gridQueryLat'>,
  contract: MeteorologyGridContract,
  visibleValidTime: string | null | undefined,
  queryPosition: ReturnType<typeof projectLonLatToPercent> | null,
): GridViewModel['cellPopup'] {
  if (state.gridQueryLon === null || state.gridQueryLat === null || !queryPosition) return null
  if (!isLonLatInMeteorologyBbox(state.gridQueryLon, state.gridQueryLat, contract.bbox)) {
    return {
      lon: state.gridQueryLon,
      lat: state.gridQueryLat,
      left: queryPosition.left,
      top: queryPosition.top,
      source: contract.source,
      cycle: contract.cycleTime,
      validTime: visibleValidTime ?? null,
      unit: contract.unit,
      nativeTimeResolution: contract.nativeTimeResolution,
      spatialResolution: contract.spatialResolution,
      value: null,
      reason: '格点查询超出合同 bbox，未请求或生成数值。',
    }
  }
  if (contract.restrictedReason) {
    return {
      lon: state.gridQueryLon,
      lat: state.gridQueryLat,
      left: queryPosition.left,
      top: queryPosition.top,
      source: contract.source,
      cycle: contract.cycleTime,
      validTime: visibleValidTime ?? null,
      unit: contract.unit,
      nativeTimeResolution: contract.nativeTimeResolution,
      spatialResolution: contract.spatialResolution,
      value: null,
      reason: contract.restrictedReason,
    }
  }
  if (!visibleValidTime || !contract.queryUrlTemplate) {
    return {
      lon: state.gridQueryLon,
      lat: state.gridQueryLat,
      left: queryPosition.left,
      top: queryPosition.top,
      source: contract.source,
      cycle: contract.cycleTime,
      validTime: visibleValidTime ?? null,
      unit: contract.unit,
      nativeTimeResolution: contract.nativeTimeResolution,
      spatialResolution: contract.spatialResolution,
      value: null,
      reason: '格点查询产品缺少 valid_time 或 query URL，显示 unavailable 状态。',
    }
  }
  return {
    lon: state.gridQueryLon,
    lat: state.gridQueryLat,
    left: queryPosition.left,
    top: queryPosition.top,
    source: contract.source,
    cycle: contract.cycleTime,
    validTime: visibleValidTime ?? null,
    unit: contract.unit,
    nativeTimeResolution: contract.nativeTimeResolution,
    spatialResolution: contract.spatialResolution,
    value: null,
    reason: '格点查询端点尚未返回实测/预报值，UI 不生成替代数值。',
  }
}

function buildAreaStatsState(
  state: Pick<MeteorologyQueryState, 'areaMinLon' | 'areaMinLat' | 'areaMaxLon' | 'areaMaxLat'>,
  contract: MeteorologyGridContract,
  visibleValidTime: string | null | undefined,
): GridViewModel['areaStats'] {
  const areaMinLon = state.areaMinLon ?? null
  const areaMinLat = state.areaMinLat ?? null
  const areaMaxLon = state.areaMaxLon ?? null
  const areaMaxLat = state.areaMaxLat ?? null
  const hasAnyAreaParam = [areaMinLon, areaMinLat, areaMaxLon, areaMaxLat].some((value) => value !== null)
  if (!hasAnyAreaParam) {
    return {
      status: contract.maxAreaKm2 <= 0
        ? '区域统计不可用：合同未开放该源的面积查询。'
        : `区域统计请求上限 ${contract.maxAreaKm2.toLocaleString('en-US')} km2，超过 bbox/resolution 限制时返回 validation 状态。`,
      tone: 'info',
      bbox: null,
      areaKm2: null,
    }
  }

  if (areaMinLon === null || areaMinLat === null || areaMaxLon === null || areaMaxLat === null) {
    return {
      status: '区域统计 validation：area bbox 参数不完整。',
      tone: 'warning',
      bbox: null,
      areaKm2: null,
    }
  }

  const bbox = {
    minLon: Math.min(areaMinLon, areaMaxLon),
    minLat: Math.min(areaMinLat, areaMaxLat),
    maxLon: Math.max(areaMinLon, areaMaxLon),
    maxLat: Math.max(areaMinLat, areaMaxLat),
  }
  if (
    !isLonLatInMeteorologyBbox(bbox.minLon, bbox.minLat, contract.bbox) ||
    !isLonLatInMeteorologyBbox(bbox.maxLon, bbox.maxLat, contract.bbox)
  ) {
    return {
      status: '区域统计 validation：请求范围超出合同 bbox，未请求或生成统计值。',
      tone: 'warning',
      bbox,
      areaKm2: null,
    }
  }

  if (contract.maxAreaKm2 <= 0 || !contract.areaStatsUrlTemplate) {
    return {
      status: '区域统计 unavailable：合同未开放该源的面积查询服务。',
      tone: 'warning',
      bbox,
      areaKm2: null,
    }
  }

  const areaKm2 = estimateBboxAreaKm2(bbox)
  if (areaKm2 > contract.maxAreaKm2) {
    return {
      status: `区域统计 validation：估算面积 ${Math.round(areaKm2).toLocaleString('en-US')} km2 超过合同上限 ${contract.maxAreaKm2.toLocaleString('en-US')} km2。`,
      tone: 'warning',
      bbox,
      areaKm2,
    }
  }

  if (!visibleValidTime) {
    return {
      status: '区域统计 unavailable：产品缺少 valid_time，未请求或生成统计值。',
      tone: 'warning',
      bbox,
      areaKm2,
    }
  }

  return {
    status: `区域统计 unavailable：请求范围约 ${Math.round(areaKm2).toLocaleString('en-US')} km2，在合同上限内，但实时 area-stat 服务尚未接入，未生成统计值。`,
    tone: 'info',
    bbox,
    areaKm2,
  }
}

function estimateBboxAreaKm2(bbox: { minLon: number; minLat: number; maxLon: number; maxLat: number }) {
  const midLatRadians = (((bbox.minLat + bbox.maxLat) / 2) * Math.PI) / 180
  const kmPerDegreeLat = 111.32
  const kmPerDegreeLon = Math.max(0, Math.cos(midLatRadians)) * 111.32
  return Math.abs(bbox.maxLon - bbox.minLon) * kmPerDegreeLon * Math.abs(bbox.maxLat - bbox.minLat) * kmPerDegreeLat
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

export function buildStationInventoryViewModel(state: Pick<MeteorologyQueryState, 'basin' | 'search' | 'searchValidationReason' | 'sort' | 'stationId'>): StationInventoryViewModel {
  const validationReason = state.searchValidationReason
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

  const filteredRows = rows
  const selectedStation = state.stationId
    ? filteredRows.find((station) => station.stationId === state.stationId) ?? null
    : filteredRows[0] ?? null
  const truncated = filteredRows.length > stationInventoryLimits.pageSize
  const pagedRows = filteredRows.slice(0, stationInventoryLimits.pageSize)
  const selectedOutOfPage = Boolean(selectedStation && !pagedRows.some((station) => station.stationId === selectedStation.stationId))
  rows = selectedOutOfPage && selectedStation ? [...pagedRows, selectedStation] : pagedRows
  const selectedSeries = selectedStation ? getMeteorologyStationSeries(selectedStation.stationId) : null

  return {
    rows,
    selectedStation,
    selectedSeries,
    emptyReason: filteredRows.length === 0 ? '搜索无结果，未渲染伪造站点。' : null,
    truncated,
    selectedOutOfPage,
    selectionValidationReason: state.stationId && !selectedStation ? '请求的 stationId 不在当前 basin/search 过滤结果中，已清理旧站点详情。' : null,
    validationReason,
  }
}
