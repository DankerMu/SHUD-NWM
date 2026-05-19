import type { MeteorologySource, MeteorologyVariable } from '@/lib/meteorology/queryState'

export interface MeteorologyBbox {
  minLon: number
  minLat: number
  maxLon: number
  maxLat: number
}

export interface MeteorologyGridContract {
  variable: MeteorologyVariable
  displayName: string
  unit: string
  source: MeteorologySource
  cycleTime: string | null
  nativeTimeResolution: string
  spatialResolution: string
  bbox: MeteorologyBbox
  validTimes: string[]
  currentValidTime: string | null
  tileUrlTemplate: string | null
  queryUrlTemplate: string | null
  areaStatsUrlTemplate: string | null
  maxAreaKm2: number
  supportsContours: boolean
  supportsComparison: boolean
  restrictedReason: string | null
  unavailableReason: string | null
  legend: Array<{ label: string; color: string; min?: number; max?: number }>
}

export interface MeteorologyStation {
  stationId: string
  stationName: string
  basinId: string
  basinName: string
  lon: number
  lat: number
  elevationM: number | null
  latestDataTime: string | null
  source: MeteorologySource
  forcingVersionId: string | null
  completeness: number
  qcStatus: 'ok' | 'partial' | 'unavailable'
  variables: MeteorologyVariable[]
  adjacent: Array<{ stationId: string; distanceKm: number; reason: string }>
  unavailableReason: string | null
}

export interface MeteorologySeriesPoint {
  time: string
  value: number | null
  qc: 'ok' | 'missing' | 'anomalous'
}

export interface MeteorologyStationSeriesVariable {
  variable: MeteorologyVariable
  unit: string
  source: MeteorologySource
  completeness: number
  qcStatus: 'ok' | 'partial' | 'unavailable'
  unavailableReason: string | null
  points: MeteorologySeriesPoint[]
  missingIntervals: Array<{ from: string; to: string; reason: string }>
}

export interface MeteorologyStationSeries {
  stationId: string
  from: string
  to: string
  sampleLimit: number
  truncated: boolean
  variables: MeteorologyStationSeriesVariable[]
}

export const meteorologyGridContractVersion = 'm13.frontend.fixture.v1'
export const meteorologyStationContractVersion = 'm13.station.fixture.v1'

export const meteorologyBbox: MeteorologyBbox = {
  minLon: 73,
  minLat: 18,
  maxLon: 135,
  maxLat: 53,
}

export const variableMetadata: Record<MeteorologyVariable, { label: string; unit: string; legend: MeteorologyGridContract['legend'] }> = {
  PRCP: {
    label: '降水',
    unit: 'mm/day',
    legend: [
      { label: '0-1', color: '#E3F2FD', min: 0, max: 1 },
      { label: '1-10', color: '#90CAF9', min: 1, max: 10 },
      { label: '10-25', color: '#42A5F5', min: 10, max: 25 },
      { label: '>25', color: '#1565C0', min: 25 },
    ],
  },
  TEMP: {
    label: '气温',
    unit: 'degC',
    legend: [
      { label: '<0', color: '#64B5F6', max: 0 },
      { label: '0-15', color: '#81C784', min: 0, max: 15 },
      { label: '15-30', color: '#FFD54F', min: 15, max: 30 },
      { label: '>30', color: '#F57C00', min: 30 },
    ],
  },
  RH: {
    label: '相对湿度',
    unit: '%',
    legend: [
      { label: '<40', color: '#FFCC80', max: 40 },
      { label: '40-70', color: '#A5D6A7', min: 40, max: 70 },
      { label: '>70', color: '#26A69A', min: 70 },
    ],
  },
  wind: {
    label: '风速',
    unit: 'm/s',
    legend: [
      { label: '0-3', color: '#C8E6C9', min: 0, max: 3 },
      { label: '3-8', color: '#4DB6AC', min: 3, max: 8 },
      { label: '>8', color: '#00695C', min: 8 },
    ],
  },
  Rn: {
    label: '净辐射',
    unit: 'W/m2',
    legend: [
      { label: '<50', color: '#E0E0E0', max: 50 },
      { label: '50-180', color: '#FFE082', min: 50, max: 180 },
      { label: '>180', color: '#EF6C00', min: 180 },
    ],
  },
  Press: {
    label: '气压',
    unit: 'hPa',
    legend: [
      { label: '<980', color: '#B39DDB', max: 980 },
      { label: '980-1020', color: '#90CAF9', min: 980, max: 1020 },
      { label: '>1020', color: '#1565C0', min: 1020 },
    ],
  },
}

const sourceMetadata: Record<MeteorologySource, { resolution: string; cycle: string | null; validTimes: string[]; restrictedReason: string | null }> = {
  GFS: {
    resolution: '0.25 deg',
    cycle: '2026-05-18T00:00:00.000Z',
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z', '2026-05-18T18:00:00.000Z'],
    restrictedReason: null,
  },
  IFS: {
    resolution: '0.25 deg',
    cycle: '2026-05-18T00:00:00.000Z',
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    restrictedReason: null,
  },
  ERA5: {
    resolution: '0.25 deg',
    cycle: '2026-05-17T00:00:00.000Z',
    validTimes: ['2026-05-17T00:00:00.000Z', '2026-05-17T06:00:00.000Z', '2026-05-17T12:00:00.000Z'],
    restrictedReason: null,
  },
  CLDAS: {
    resolution: '0.0625 deg',
    cycle: null,
    validTimes: [],
    restrictedReason: 'CLDAS 数据权限尚未开通，当前仅公开合同和受限原因。',
  },
  'Best Available': {
    resolution: 'contract-selected',
    cycle: '2026-05-18T00:00:00.000Z',
    validTimes: ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z'],
    restrictedReason: null,
  },
}

export function getMeteorologyGridContract(variable: MeteorologyVariable, source: MeteorologySource): MeteorologyGridContract {
  const variableMeta = variableMetadata[variable]
  const sourceMeta = sourceMetadata[source]
  const restrictedReason = sourceMeta.restrictedReason
  const liveTileReason = restrictedReason ? null : '实时栅格瓦片服务尚未接入；页面只展示合同元数据，不渲染伪造值。'
  return {
    variable,
    displayName: variableMeta.label,
    unit: variableMeta.unit,
    source,
    cycleTime: sourceMeta.cycle,
    nativeTimeResolution: source === 'IFS' ? '12h' : source === 'CLDAS' ? '1h' : '6h',
    spatialResolution: sourceMeta.resolution,
    bbox: meteorologyBbox,
    validTimes: sourceMeta.validTimes,
    currentValidTime: sourceMeta.validTimes[sourceMeta.validTimes.length - 1] ?? null,
    tileUrlTemplate: restrictedReason ? null : `/api/v1/met/grid/tiles/{z}/{x}/{y}?source=${encodeURIComponent(source)}&variable=${variable}&valid_time={valid_time}`,
    queryUrlTemplate: restrictedReason ? null : `/api/v1/met/grid/query?source=${encodeURIComponent(source)}&variable=${variable}&lon={lon}&lat={lat}&valid_time={valid_time}`,
    areaStatsUrlTemplate: restrictedReason ? null : `/api/v1/met/grid/area-stats?source=${encodeURIComponent(source)}&variable=${variable}&bbox={bbox}&valid_time={valid_time}`,
    maxAreaKm2: source === 'CLDAS' ? 0 : 250_000,
    supportsContours: source !== 'CLDAS',
    supportsComparison: source !== 'CLDAS' && source !== 'Best Available',
    restrictedReason,
    unavailableReason: restrictedReason ?? liveTileReason,
    legend: variableMeta.legend,
  }
}

export const meteorologyStations: MeteorologyStation[] = [
  {
    stationId: 'HMT-Y2-0236',
    stationName: '武汉代站',
    basinId: 'yangtze',
    basinName: '长江流域',
    lon: 114.35,
    lat: 30.62,
    elevationM: 24,
    latestDataTime: '2026-05-18T12:00:00.000Z',
    source: 'Best Available',
    forcingVersionId: 'forcing_best_2026051800_yangtze',
    completeness: 0.92,
    qcStatus: 'partial',
    variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Press'],
    adjacent: [
      { stationId: 'HMT-Y2-0237', distanceKm: 42.7, reason: '同一流域下游相邻站' },
      { stationId: 'HMT-HAN-0081', distanceKm: 76.4, reason: '同一 forcing 网格邻近站' },
    ],
    unavailableReason: null,
  },
  {
    stationId: 'HMT-Y2-0237',
    stationName: '黄冈代站',
    basinId: 'yangtze',
    basinName: '长江流域',
    lon: 115.02,
    lat: 30.45,
    elevationM: 31,
    latestDataTime: '2026-05-18T06:00:00.000Z',
    source: 'GFS',
    forcingVersionId: 'forcing_gfs_2026051800_yangtze',
    completeness: 0.81,
    qcStatus: 'partial',
    variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Press'],
    adjacent: [{ stationId: 'HMT-Y2-0236', distanceKm: 42.7, reason: '同一流域上游相邻站' }],
    unavailableReason: null,
  },
  {
    stationId: 'HMT-HAN-0081',
    stationName: '汉江入口代站',
    basinId: 'hanjiang',
    basinName: '汉江流域',
    lon: 112.27,
    lat: 32.05,
    elevationM: 84,
    latestDataTime: null,
    source: 'ERA5',
    forcingVersionId: null,
    completeness: 0,
    qcStatus: 'unavailable',
    variables: ['PRCP', 'TEMP'],
    adjacent: [{ stationId: 'HMT-Y2-0236', distanceKm: 76.4, reason: '同一 forcing 网格邻近站' }],
    unavailableReason: '所选时间范围没有可用 forcing series。',
  },
]

export const stationInventoryLimits = {
  pageSize: 50,
  searchMaxLength: 80,
  seriesSampleLimit: 48,
  maxRangeHours: 168,
}

const baseTimes = ['2026-05-18T00:00:00.000Z', '2026-05-18T06:00:00.000Z', '2026-05-18T12:00:00.000Z']

const sampleValues: Record<MeteorologyVariable, Array<number | null>> = {
  PRCP: [1.2, null, 7.8],
  TEMP: [21.4, 23.1, 24.0],
  RH: [76, 81, null],
  wind: [3.4, 4.1, 4.8],
  Rn: [110, 165, 132],
  Press: [1008, 1006, 1005],
}

export function getMeteorologyStationSeries(stationId: string): MeteorologyStationSeries | null {
  const station = meteorologyStations.find((item) => item.stationId === stationId)
  if (!station) return null
  return {
    stationId,
    from: baseTimes[0],
    to: baseTimes[baseTimes.length - 1],
    sampleLimit: stationInventoryLimits.seriesSampleLimit,
    truncated: false,
    variables: (['PRCP', 'TEMP', 'RH', 'wind', 'Press'] satisfies MeteorologyVariable[]).map((variable) => {
      const available = station.variables.includes(variable) && station.qcStatus !== 'unavailable'
      return {
        variable,
        unit: variableMetadata[variable].unit,
        source: station.source,
        completeness: available ? station.completeness : 0,
        qcStatus: available && sampleValues[variable].some((value) => value === null) ? 'partial' : available ? 'ok' : 'unavailable',
        unavailableReason: available ? null : (station.unavailableReason ?? `${variable} forcing series 不在站点合同中。`),
        points: available
          ? baseTimes.map((time, index) => ({
              time,
              value: sampleValues[variable][index],
              qc: sampleValues[variable][index] === null ? 'missing' : 'ok',
            }))
          : [],
        missingIntervals: available && sampleValues[variable].some((value) => value === null)
          ? [{ from: baseTimes[1], to: baseTimes[2], reason: '合同标记缺测区间' }]
          : [],
      }
    }),
  }
}

export function formatBbox(bbox: MeteorologyBbox) {
  return `${bbox.minLon}-${bbox.maxLon}E, ${bbox.minLat}-${bbox.maxLat}N`
}
