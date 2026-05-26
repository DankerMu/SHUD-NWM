import { expect, test, type Page, type Route } from '@playwright/test'

type HydroMetSource = 'GFS' | 'IFS'

const stationVariables = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'] as const
const cycles: Record<HydroMetSource, string> = {
  GFS: '2026-05-21T00:00:00.000Z',
  IFS: '2026-05-21T18:00:00.000Z',
}

const units: Record<(typeof stationVariables)[number], string> = {
  PRCP: 'mm',
  TEMP: 'degC',
  RH: '%',
  wind: 'm/s',
  Rn: 'W/m2',
  Press: 'Pa',
}

interface HydroMetRequestRecords {
  latestProductSources: HydroMetSource[]
  stationSeriesRequests: Array<{ stationId: string; forcingVersionId: string | null; search: string }>
  forecastRequests: Array<{
    segmentId: string
    riverNetworkVersionId: string | null
    scenario: string | null
    variable: string | null
    issueTime: string | null
  }>
}

function success<T>(data: T) {
  return { status: 'success', data }
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(success(data)),
  })
}

function forcingVersionId(source: HydroMetSource) {
  return `forc_${source.toLowerCase()}_20260521${source === 'GFS' ? '00' : '18'}_basins_qhh_shud`
}

function runId(source: HydroMetSource) {
  return source === 'GFS' ? 'qhh_gfs_2026052100_smoke' : 'fcst_ifs_2026052118_basins_qhh_shud'
}

function scenarioId(source: HydroMetSource) {
  return source === 'GFS' ? 'forecast_gfs_deterministic' : 'forecast_ifs_deterministic'
}

function addHours(value: string, hours: number) {
  return new Date(Date.parse(value) + hours * 60 * 60 * 1000).toISOString()
}

function latestProduct(source: HydroMetSource) {
  const cycle = cycles[source]
  const availableHorizonHours = source === 'IFS' ? 144 : 168
  const validTimeEnd = addHours(cycle, availableHorizonHours)

  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'basins_qhh_vbasins',
    river_network_version_id: 'basins_qhh_rivnet_vbasins',
    source_id: source,
    cycle_time: cycle,
    run_id: runId(source),
    forcing_version_id: forcingVersionId(source),
    station_count: 386,
    expected_station_count: 386,
    segment_count: 1633,
    expected_segment_count: 1633,
    status: 'ready',
    run_status: 'frequency_done',
    valid_time_start: addHours(cycle, 3),
    valid_time_end: validTimeEnd,
    river_valid_time_start: addHours(cycle, 3),
    river_valid_time_end: validTimeEnd,
    forcing_valid_time_start: addHours(cycle, 3),
    forcing_valid_time_end: validTimeEnd,
    available_horizon_hours: availableHorizonHours,
    expected_horizon_hours: 168,
    shorter_horizon: source === 'IFS',
    availability: {
      ready: true,
      unavailable_reasons: [],
      quality_flags: source === 'IFS' ? ['shorter_horizon'] : [],
      quality_notes: source === 'IFS'
        ? [
            {
              code: 'IFS_SHORTER_HORIZON',
              message: 'IFS 18Z deterministic fixture exposes 144h available horizon.',
              expected_horizon_hours: 168,
              available_horizon_hours: 144,
              available_end_time: validTimeEnd,
            },
          ]
        : [],
    },
    quality: {
      station_sample_count: 12,
      river_sample_count: 2,
      required_station_variables: [...stationVariables],
      station_variable_coverage: stationVariables.map((variable) => ({
        variable,
        station_count: 386,
        sample_count: 2,
        unit_count: 1,
        quality_flag_count: 1,
        missing_unit_samples: 0,
        missing_quality_flag_samples: 0,
        valid_time_start: addHours(cycle, 3),
        valid_time_end: addHours(cycle, 6),
      })),
      candidate_limit: 250,
      search_limit: 500,
      context_limit: 12,
      query_indexes: [],
    },
  }
}

const stationInventory = {
  items: [
    {
      station_id: 'qhh_forc_001',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH Forcing 001',
      geom: { type: 'Point', coordinates: [101.45, 35.72] },
      elevation_m: 2850,
      station_role: 'forcing',
      active_flag: true,
      properties_json: {},
      created_at: '2026-05-21T00:00:00.000Z',
    },
    {
      station_id: 'qhh_forc_002',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH Forcing 002',
      geom: { type: 'Point', coordinates: [101.82, 35.93] },
      elevation_m: 2924,
      station_role: 'forcing',
      active_flag: true,
      properties_json: {},
      created_at: '2026-05-21T00:00:00.000Z',
    },
  ],
  total_count: 386,
  limit: 500,
  offset: 0,
}

const riverSegments = {
  type: 'FeatureCollection',
  total: 1633,
  feature_total: 1633,
  limit: 250,
  offset: 0,
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'seg-001',
        basin_version_id: 'basins_qhh_vbasins',
        river_network_version_id: 'basins_qhh_rivnet_vbasins',
        name: 'QHH Segment 001',
        stream_order: 3,
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [101.1, 35.6],
          [101.8, 35.9],
        ],
      },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-002',
        river_segment_id: 'seg-002',
        basin_version_id: 'basins_qhh_vbasins',
        river_network_version_id: 'basins_qhh_rivnet_vbasins',
        name: 'QHH Segment 002',
        stream_order: 4,
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [101.85, 35.92],
          [102.2, 36.12],
        ],
      },
    },
  ],
}

function stationSeries(source: HydroMetSource, stationId: string) {
  const cycle = cycles[source]
  const returnedFrom = addHours(cycle, 3)
  const returnedTo = addHours(cycle, 6)
  const stationName = stationId === 'qhh_forc_002' ? 'QHH Forcing 002' : 'QHH Forcing 001'
  const stationCoordinates = stationId === 'qhh_forc_002'
    ? { longitude: 101.82, latitude: 35.93, elevation: 2924 }
    : { longitude: 101.45, latitude: 35.72, elevation: 2850 }

  return {
    station_id: stationId,
    station: {
      station_id: stationId,
      basin_version_id: 'basins_qhh_vbasins',
      station_name: stationName,
      name: stationName,
      longitude: stationCoordinates.longitude,
      latitude: stationCoordinates.latitude,
      elevation_m: stationCoordinates.elevation,
      elevation: stationCoordinates.elevation,
      station_role: 'forcing',
      active_flag: true,
      properties_json: {},
      created_at: '2026-05-21T00:00:00.000Z',
    },
    forcing_version_id: forcingVersionId(source),
    model_id: 'basins_qhh_shud',
    source_id: source,
    cycle_time: cycle,
    valid_time_start: returnedFrom,
    valid_time_end: returnedTo,
    limit: 240,
    requested_from: null,
    requested_to: null,
    series: stationVariables.map((variable, index) => ({
      variable,
      unit: units[variable],
      native_resolution: '3h',
      source_id: source,
      cycle_time: cycle,
      points: [
        { valid_time: returnedFrom, value: index + 0.25, quality_flag: 'ok', source_id: source },
        { valid_time: returnedTo, value: index + 0.75, quality_flag: 'ok', source_id: source },
      ],
      truncated: false,
      metadata: {
        limit: 240,
        returned_points: 2,
        requested_from: null,
        requested_to: null,
        returned_from: returnedFrom,
        returned_to: returnedTo,
        truncated: false,
      },
    })),
  }
}

function forecastSeries(source: HydroMetSource, segmentId: string) {
  const cycle = cycles[source]
  const availableLeadHours = source === 'IFS' ? 144 : 168

  return {
    segment_id: segmentId,
    river_segment_id: segmentId,
    issue_time: cycle,
    variable: 'q_down',
    unit: 'm3/s',
    series: [
      {
        scenario_id: scenarioId(source),
        source_id: source,
        cycle_time: cycle,
        available_lead_hours: availableLeadHours,
        segment_role: 'forecast',
        points: [
          [addHours(cycle, 3), segmentId === 'seg-002' ? 22.5 : 12.5],
          [addHours(cycle, availableLeadHours), segmentId === 'seg-002' ? (source === 'IFS' ? 28.2 : 30.4) : (source === 'IFS' ? 18.2 : 20.4)],
        ],
      },
    ],
    frequency_thresholds: null,
  }
}

function sourceFromForcingVersion(value: string | null): HydroMetSource {
  return value?.includes('_ifs_') ? 'IFS' : 'GFS'
}

function sourceFromScenario(value: string | null): HydroMetSource {
  return value?.includes('_ifs_') ? 'IFS' : 'GFS'
}

async function mockHydroMetApi(page: Page): Promise<HydroMetRequestRecords> {
  const records: HydroMetRequestRecords = {
    latestProductSources: [],
    stationSeriesRequests: [],
    forecastRequests: [],
  }

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/mvp/qhh/latest-product') {
      const source = (url.searchParams.get('source') ?? 'GFS').toUpperCase() as HydroMetSource
      records.latestProductSources.push(source)
      return fulfill(route, latestProduct(source))
    }

    if (url.pathname === '/api/v1/met/stations') {
      expect(url.searchParams.get('model_id')).toBe('basins_qhh_shud')
      return fulfill(route, stationInventory)
    }

    if (url.pathname === '/api/v1/basin-versions/basins_qhh_vbasins/river-segments') {
      expect(url.searchParams.get('river_network_version_id')).toBe('basins_qhh_rivnet_vbasins')
      return fulfill(route, riverSegments)
    }

    const stationSeriesMatch = /^\/api\/v1\/met\/stations\/([^/]+)\/series$/.exec(url.pathname)
    if (stationSeriesMatch) {
      const stationId = decodeURIComponent(stationSeriesMatch[1])
      expect(['qhh_forc_001', 'qhh_forc_002']).toContain(stationId)
      const forcingVersion = url.searchParams.get('forcing_version_id')
      records.stationSeriesRequests.push({ stationId, forcingVersionId: forcingVersion, search: url.search })
      stationVariables.forEach((variable) => expect(url.search).toContain(variable))
      return fulfill(route, stationSeries(sourceFromForcingVersion(forcingVersion), stationId))
    }

    const forecastSeriesMatch = /^\/api\/v1\/basin-versions\/basins_qhh_vbasins\/river-segments\/([^/]+)\/forecast-series$/.exec(url.pathname)
    if (forecastSeriesMatch) {
      const segmentId = decodeURIComponent(forecastSeriesMatch[1])
      expect(['seg-001', 'seg-002']).toContain(segmentId)
      const riverNetworkVersionId = url.searchParams.get('river_network_version_id')
      const scenario = url.searchParams.get('scenarios')
      const variable = url.searchParams.get('variables')
      const issueTime = url.searchParams.get('issue_time')
      records.forecastRequests.push({ segmentId, riverNetworkVersionId, scenario, variable, issueTime })
      expect(riverNetworkVersionId).toBe('basins_qhh_rivnet_vbasins')
      expect(variable).toBe('q_down')
      return fulfill(route, forecastSeries(sourceFromScenario(scenario), segmentId))
    }

    throw new Error(`Unhandled mocked API route: ${request.method()} ${url.pathname}`)
  })

  return records
}

test('loads deterministic QHH hydro-met evidence without live backend dependencies', async ({ page }) => {
  const records = await mockHydroMetApi(page)

  await page.goto('/hydro-met?source=GFS')

  await expect(page.getByTestId('hydro-met-product-panel')).toContainText(runId('GFS'))
  await expect(page.getByTestId('hydro-met-station-list')).toContainText('qhh_forc_001')
  await expect(page.getByTestId('hydro-met-station-list')).toContainText('qhh_forc_002')
  await expect(page.getByTestId('hydro-met-river-list')).toContainText('seg-001')
  await expect(page.getByTestId('hydro-met-river-list')).toContainText('seg-002')
  await expect(page.getByTestId('hydro-met-no-fake-data')).toContainText('不绘制假曲线')
  await expect(page.getByTestId('hydro-met-station-series-loaded')).toContainText(forcingVersionId('GFS'))
  await expect(page.getByTestId('hydro-met-station-series-loaded')).toContainText('qhh_forc_001')

  for (const variable of stationVariables) {
    await expect(page.getByTestId(`hydro-met-variable-${variable}-chart`)).toContainText(variable)
  }

  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('q_down')
  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('seg-001')
  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('GFS / forecast_gfs_deterministic')

  expect(records.latestProductSources).toContain('GFS')
  expect(records.stationSeriesRequests).toContainEqual(expect.objectContaining({
    stationId: 'qhh_forc_001',
    forcingVersionId: forcingVersionId('GFS'),
  }))
  expect(records.stationSeriesRequests.some((record) => stationVariables.every((variable) => record.search.includes(variable)))).toBe(true)
  expect(records.forecastRequests).toContainEqual({
    segmentId: 'seg-001',
    riverNetworkVersionId: 'basins_qhh_rivnet_vbasins',
    scenario: 'forecast_gfs_deterministic',
    variable: 'q_down',
    issueTime: cycles.GFS,
  })

  await page.getByTestId('hydro-met-station-row').filter({ hasText: 'qhh_forc_002' }).click()
  await expect(page.getByTestId('hydro-met-station-series-loaded')).toContainText('qhh_forc_002')
  expect(records.stationSeriesRequests).toContainEqual(expect.objectContaining({
    stationId: 'qhh_forc_002',
    forcingVersionId: forcingVersionId('GFS'),
  }))

  await page.getByTestId('hydro-met-river-row').filter({ hasText: 'seg-002' }).click()
  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('seg-002')
  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('GFS / forecast_gfs_deterministic')
  expect(records.forecastRequests).toContainEqual({
    segmentId: 'seg-002',
    riverNetworkVersionId: 'basins_qhh_rivnet_vbasins',
    scenario: 'forecast_gfs_deterministic',
    variable: 'q_down',
    issueTime: cycles.GFS,
  })

  await page.getByRole('tab', { name: 'IFS' }).click()

  await expect(page.getByTestId('hydro-met-product-panel')).toContainText(runId('IFS'))
  await expect(page.getByTestId('hydro-met-shorter-horizon')).toContainText('可用时效短于预期')
  await expect(page.getByTestId('hydro-met-station-series-loaded')).toContainText(forcingVersionId('IFS'))
  await expect(page.getByTestId('hydro-met-station-series-loaded')).toContainText('qhh_forc_001')
  await expect(page.getByTestId('hydro-met-river-forecast-loaded')).toContainText('IFS / forecast_ifs_deterministic')
  await expect(page.getByTestId('hydro-met-river-horizon')).toContainText('144h')
  await expect(page.getByTestId('hydro-met-river-horizon')).toContainText('expected 168h')

  expect(records.latestProductSources).toContain('IFS')
  expect(records.stationSeriesRequests).toContainEqual(expect.objectContaining({
    stationId: 'qhh_forc_001',
    forcingVersionId: forcingVersionId('IFS'),
  }))
  expect(records.forecastRequests).toContainEqual({
    segmentId: 'seg-001',
    riverNetworkVersionId: 'basins_qhh_rivnet_vbasins',
    scenario: 'forecast_ifs_deterministic',
    variable: 'q_down',
    issueTime: cycles.IFS,
  })
})
