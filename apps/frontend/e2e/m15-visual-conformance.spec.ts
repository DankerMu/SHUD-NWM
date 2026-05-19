import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type Page, type Route } from '@playwright/test'

test.setTimeout(60_000)

const evidenceRoot = path.resolve(process.cwd(), '../../.codex/evidence/issue-176')
const screenshotRoot = path.join(evidenceRoot, 'screenshots')
const manifestPath = path.join(evidenceRoot, 'manifest.json')
const captureCommand = 'cd apps/frontend && corepack pnpm run test:e2e:m15-visual'
const commitSha = process.env.GITHUB_SHA ?? process.env.CI_COMMIT_SHA ?? 'local-uncommitted'

const requiredViewports = [
  { width: 1920, height: 1080, label: '1920x1080' },
  { width: 1440, height: 900, label: '1440x900' },
  { width: 1280, height: 900, label: '1280x900' },
] as const

const requiredRoutes = [
  { name: 'overview', path: '/overview', stateLabel: 'loaded' },
  {
    name: 'basin-detail',
    path: '/basins/basin-demo?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    stateLabel: 'loaded-selected-segment',
  },
  { name: 'flood-alerts', path: '/flood-alerts', stateLabel: 'loaded-warning-levels' },
  { name: 'monitoring', path: '/monitoring', stateLabel: 'loaded-rbac-operator' },
] as const

const extendedRoutes = [
  {
    name: 'segment-detail',
    path: '/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009',
    stateLabel: 'loaded-segment-forecast',
  },
  {
    name: 'meteorology-grid',
    path: '/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62',
    stateLabel: 'loaded-grid-contract',
  },
  {
    name: 'meteorology-stations',
    path: '/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237',
    stateLabel: 'loaded-station-contract',
  },
  {
    name: 'model-assets',
    path: '/system/model-assets?modelId=model-demo',
    stateLabel: 'loaded-model-admin',
  },
] as const

const stateRoutes = [
  { name: 'overview-loading', path: '/overview?m15State=loading', stateLabel: 'loading' },
  { name: 'overview-error', path: '/overview?m15State=error', stateLabel: 'api-error' },
  { name: 'basin-empty', path: '/basins/basin-demo?m15State=empty&basinVersionId=bv-001', stateLabel: 'empty-segments' },
  { name: 'flood-empty', path: '/flood-alerts?m15State=empty', stateLabel: 'empty-alerts' },
  { name: 'monitoring-denied', path: '/monitoring?m15Role=viewer', stateLabel: 'rbac-denied' },
  { name: 'meteorology-restricted', path: '/meteorology?tab=grid&source=CLDAS&variable=PRCP', stateLabel: 'restricted' },
] as const

const manifestEntries: EvidenceEntry[] = []

interface EvidenceEntry {
  route: string
  viewport: string
  fixtureMode: string
  sha: string
  stateLabel: string
  command: string
  artifactPath: string
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

const basinVersion = {
  basin_version_id: 'bv-001',
  basin_id: 'basin-demo',
  version_label: 'v2026_01',
  geom: {
    type: 'MultiPolygon',
    coordinates: [[[[100, 30], [106, 30], [106, 36], [100, 36], [100, 30]]]],
  },
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
}

const model = {
  model_id: 'model-demo',
  model_name: 'Demo SHUD',
  basin_id: 'basin-demo',
  basin_name: 'Demo Basin',
  basin_version_id: 'bv-001',
  river_network_version_id: 'rn-v1',
  mesh_version_id: 'mesh-v1',
  calibration_version_id: 'cal-v1',
  segment_count: 2,
  mesh_uri: 's3://nhms/models/model-demo/mesh',
  mesh_checksum: 'mesh-sha',
  shud_code_version: 'shud-1',
  active_flag: true,
  model_package_uri: 's3://nhms/models/model-demo/package',
  package_checksum: 'package-sha',
  manifest_uri: 's3://nhms/models/model-demo/manifest.json',
  source_inventory_checksum: 'inventory-sha',
  basin_slug: 'demo',
  shud_input_name: 'demo-input',
  source_path: null,
  resolved_source_path: null,
  source_uri: 's3://nhms/sources/demo',
  source_is_symlink: false,
  resource_profile: {
    area_km2: 1234,
    source_lineage: { source_uri: 's3://nhms/sources/demo' },
    product_assets: [{ id: 'forecast-product', label: 'Forecast product', checksum: 'asset-sha', uri: 's3://nhms/products/demo' }],
    geometry: { type: 'LineString', coordinates: [[100, 30], [103, 33], [106, 35]] },
  },
  created_at: '2026-05-01T00:00:00Z',
}

const runs = [
  {
    run_id: 'run-gfs-1',
    run_type: 'forecast',
    scenario_id: 'forecast_gfs_deterministic',
    model_id: 'model-demo',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-v1',
    source_id: 'gfs',
    cycle_time: '2026-05-18T00:00:00Z',
    status: 'frequency_done',
    start_time: '2026-05-18T00:00:00Z',
    end_time: '2026-05-18T06:00:00Z',
    created_at: '2026-05-18T00:00:00Z',
    updated_at: '2026-05-18T06:20:00Z',
  },
]

const riverSegments = {
  type: 'FeatureCollection',
  total: 2,
  feature_total: 2,
  limit: 1000,
  offset: 0,
  features: [
    riverFeature('seg-001', 'North Branch 001', [[100, 30], [101, 31]], 1),
    riverFeature('seg-009', 'Main Stem 009', [[101, 31], [103, 33], [105, 35]], 3),
  ],
}

const floodRanking = {
  items: [
    {
      rank: 1,
      river_segment_id: 'seg-009',
      segment_id: 'seg-009',
      segment_name: 'Main Stem 009',
      basin_version_id: 'bv-001',
      river_network_version_id: 'rn-v1',
      q_value: 456,
      q_unit: 'm3/s',
      return_period: 20,
      warning_level: 'warning',
      duration: '1h',
      valid_time: '2026-05-18T06:00:00Z',
    },
  ],
  total: 1,
  limit: 200,
  offset: 0,
}

function riverFeature(segmentId: string, name: string, coordinates: number[][], streamOrder: number) {
  return {
    type: 'Feature',
    properties: {
      segment_id: segmentId,
      river_segment_id: segmentId,
      basin_version_id: 'bv-001',
      river_network_version_id: 'rn-v1',
      name,
      stream_order: streamOrder,
      length_m: 1200,
    },
    geometry: { type: 'LineString', coordinates },
  }
}

function forecastPayload(segmentId: string) {
  return {
    river_segment_id: segmentId,
    issue_time: '2026-05-18T00:00:00Z',
    variable: 'q_down',
    unit: 'm3/s',
    frequency_thresholds: {
      Q2: 100,
      Q5: 180,
      Q10: 260,
      Q20: 360,
      Q50: 520,
      Q100: 700,
      sample_quality: { count: 30 },
    },
    segments: [
      {
        scenario: 'analysis_true_field',
        scenario_id: 'analysis_true_field',
        source: 'GFS',
        segment_role: 'past_7_days',
        data: [{ valid_time: '2026-05-18T00:00:00Z', value: 320 }],
      },
      {
        scenario: 'forecast_gfs_deterministic',
        scenario_id: 'forecast_gfs_deterministic',
        source: 'GFS',
        segment_role: 'future_7_days',
        data: [
          { valid_time: '2026-05-18T06:00:00Z', value: 456 },
          { valid_time: '2026-05-18T12:00:00Z', value: 420 },
        ],
      },
    ],
  }
}

async function mockM15Apis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const state = new URL(page.url()).searchParams.get('m15State')

    if (state === 'loading' && url.pathname === '/api/v1/basins') return
    if (state === 'error' && url.pathname === '/api/v1/basins') {
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ status: 'error', error: { message: 'm15 fixture API error' } }) })
    }
    if (url.pathname === '/api/v1/basins') return fulfill(route, [{ basin_id: 'basin-demo', basin_name: 'Demo Basin', basin_group: 'major', description: null, created_at: '2026-05-01T00:00:00Z' }])
    if (url.pathname === '/api/v1/basins/basin-demo/versions') return fulfill(route, [basinVersion])
    if (url.pathname.startsWith('/api/v1/basins/') && url.pathname.endsWith('/versions')) return fulfill(route, [])
    if (url.pathname === '/api/v1/models') return fulfill(route, { items: [model], total: 1, limit: 200, offset: 0 })
    if (url.pathname === '/api/v1/models/model-demo') return fulfill(route, model)
    if (url.pathname === '/api/v1/runs') return fulfill(route, { items: runs, total: 1, limit: Number(url.searchParams.get('limit') ?? 20), offset: Number(url.searchParams.get('offset') ?? 0) })
    if (url.pathname === '/api/v1/layers') {
      return fulfill(route, [
        { layer_id: 'discharge', layer_name: 'River discharge', layer_type: 'hydrology', variables: ['q_down'] },
        { layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: ['return_period'] },
        { layer_id: 'warning-level', layer_name: 'Warning level', layer_type: 'hydrology', variables: ['warning_level'] },
      ])
    }
    if (url.pathname.startsWith('/api/v1/layers/') && url.pathname.endsWith('/valid-times')) {
      return fulfill(route, ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z', '2026-05-18T12:00:00Z'])
    }
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 1, pending: 0, idle: 3 })
    if (url.pathname === '/api/v1/pipeline/status') {
      return fulfill(route, {
        cycle_id: 'cycle-gfs-1',
        source: 'GFS',
        cycle_time: '2026-05-18T00:00:00Z',
        current_state: 'partially_failed',
        started_at: '2026-05-18T00:00:00Z',
        updated_at: '2026-05-18T06:00:00Z',
        job_counts: { succeeded: 3, failed: 1, running: 1, pending: 0 },
      })
    }
    if (url.pathname === '/api/v1/pipeline/stages') {
      return fulfill(route, [
        { stage: 'download', display_status: 'succeeded', status: 'succeeded', duration_seconds: 12, basin_progress: { completed: 2, total: 2, failed: 0 }, basin_results: [] },
        { stage: 'forecast', display_status: 'partially_failed', status: 'partially_failed', duration_seconds: 80, basin_progress: { completed: 1, total: 2, failed: 1 }, basin_results: [{ model_id: 'model-demo', basin_id: 'basin-demo', status: 'failed', error_code: 'M15_PARTIAL', error_message: 'partial fixture failure' }] },
      ])
    }
    if (url.pathname === '/api/v1/jobs') {
      return fulfill(route, {
        items: [{ job_id: 'job-m15', run_id: 'run-gfs-1', cycle_id: 'cycle-gfs-1', run_type: 'forecast', scenario: 'forecast_gfs_deterministic', job_type: 'forecast', slurm_job_id: '1001', model_id: 'model-demo', status: 'failed', stage: 'forecast', submitted_at: '2026-05-18T00:01:00Z', started_at: '2026-05-18T00:02:00Z', finished_at: '2026-05-18T00:05:00Z', exit_code: 1, retry_count: 0, error_code: 'M15_PARTIAL', error_message: 'partial fixture failure', log_uri: null, duration_seconds: 180 }],
        total: 1,
        limit: 12,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/metrics/stage-duration' || url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      if (state === 'empty') return fulfill(route, { run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, quality_note: null, levels: [] })
      return fulfill(route, { run_id: 'run-gfs-1', total_segments: 2, usable_curves: 2, unavailable_count: 0, quality_note: null, levels: [{ level: 'warning', count: 1, color: '#FFB74D' }] })
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') return fulfill(route, state === 'empty' ? { ...floodRanking, items: [], total: 0 } : floodRanking)
    if (url.pathname === '/api/v1/tiles/flood-return-period') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ type: 'FeatureCollection', features: [] }) })
    if (url.pathname === '/api/v1/flood-alerts/timeline') {
      return fulfill(route, {
        run_id: 'run-gfs-1',
        segment_id: 'seg-009',
        river_segment_id: 'seg-009',
        river_network_version_id: 'rn-v1',
        timesteps: [{ valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 }],
        timeline: [],
        peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 },
        frequency_thresholds: null,
        quality_note: null,
      })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments') {
      return fulfill(route, state === 'empty' ? { ...riverSegments, total: 0, feature_total: 0, features: [] } : riverSegments)
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-009') {
      return fulfill(route, {
        river_segment_id: 'seg-009',
        river_network_version_id: 'rn-v1',
        segment_order: 3,
        downstream_segment_id: null,
        length_m: 1200,
        geom: { type: 'LineString', coordinates: [[101, 31], [103, 33], [105, 35]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-001') {
      return fulfill(route, {
        river_segment_id: 'seg-001',
        river_network_version_id: 'rn-v1',
        segment_order: 1,
        downstream_segment_id: 'seg-009',
        length_m: 800,
        geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname.endsWith('/forecast-series')) return fulfill(route, forecastPayload(url.pathname.split('/river-segments/')[1]?.split('/')[0] ?? 'seg-009'))
    if (url.pathname === '/api/v1/lineage/river-point') return fulfill(route, { target_type: 'river_point', target_id: 'seg-009', nodes: [], edges: [] })

    throw new Error(`Unhandled M15 API route: ${route.request().method()} ${url.pathname}`)
  })
}

async function selectRoleIfAvailable(page: Page, roleName: 'Viewer' | 'Operator' | 'Model Admin') {
  const role = page.getByLabel('Role')
  await expect(role).toBeVisible()
  await role.click({ force: true })
  await page.getByRole('option', { name: roleName }).click({ force: true })
}

async function prepareRoute(page: Page, routePath: string) {
  const url = new URL(routePath, 'http://example.test')
  const requestedRole = url.searchParams.get('m15Role')
  await page.goto(`${url.pathname}${url.search}`)
  if (routePath.startsWith('/monitoring') && requestedRole !== 'viewer') await selectRoleIfAvailable(page, 'Operator')
  if (routePath.startsWith('/system/model-assets')) await selectRoleIfAvailable(page, 'Model Admin')
}

async function waitForRouteReady(page: Page, routeName: string, stateLabel: string) {
  if (stateLabel === 'loading') {
    await expect(page.getByText('总览数据加载中')).toBeVisible({ timeout: 15_000 })
    return
  }
  if (routeName.startsWith('overview')) await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible({ timeout: 15_000 })
  else if (routeName.startsWith('basin')) await expect(page.getByRole('heading', { name: '流域分析' })).toBeVisible({ timeout: 15_000 })
  else if (routeName.startsWith('flood')) await expect(page.getByRole('heading', { name: '洪水预警' })).toBeVisible({ timeout: 15_000 })
  else if (routeName.startsWith('monitoring')) await expect(stateLabel === 'rbac-denied' ? page.getByText('权限不足') : page.getByRole('heading', { name: '监控工作台' })).toBeVisible({ timeout: 15_000 })
  else if (routeName === 'segment-detail') await expect(page.getByRole('heading', { name: 'seg-009' })).toBeVisible({ timeout: 15_000 })
  else if (routeName.startsWith('meteorology')) await expect(page.getByRole('heading', { name: '气象数据产品' })).toBeVisible({ timeout: 15_000 })
  else if (routeName === 'model-assets') await expect(page.getByRole('heading', { name: '模型资产管理' })).toBeVisible({ timeout: 15_000 })
}

async function assertNoHorizontalScroll(page: Page) {
  const scrollState = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    html: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(Math.max(scrollState.body, scrollState.html)).toBeLessThanOrEqual(scrollState.viewport + 1)
}

async function assertVerticalNonOverlap(page: Page, topSelector: string, bottomSelector: string) {
  const boxes = await page.evaluate(({ topSelector, bottomSelector }) => {
    const top = document.querySelector(topSelector)?.getBoundingClientRect()
    const bottom = document.querySelector(bottomSelector)?.getBoundingClientRect()
    return top && bottom ? { topBottom: top.bottom, bottomTop: bottom.top } : null
  }, { topSelector, bottomSelector })
  expect(boxes).not.toBeNull()
  expect(boxes!.topBottom).toBeLessThanOrEqual(boxes!.bottomTop + 1)
}

async function assertM11LayoutOracle(page: Page) {
  await assertNoHorizontalScroll(page)
  await expect(page.getByTestId('m11-timeline')).toBeVisible()
  await assertVerticalNonOverlap(page, 'section[aria-label$="地图"]', '[data-testid="m11-timeline"]')
  const navHeight = await page.locator('header').evaluate((element) => Math.round(element.getBoundingClientRect().height))
  const timelineHeight = await page.getByTestId('m11-timeline').evaluate((element) => Math.round(element.getBoundingClientRect().height))
  expect(navHeight).toBeGreaterThanOrEqual(56)
  expect(navHeight).toBeLessThanOrEqual(57)
  expect(timelineHeight).toBeGreaterThanOrEqual(64)
  await expect(page.getByRole('button', { name: '折叠左侧面板' })).toBeVisible()
  await expect(page.getByRole('button', { name: '折叠右侧面板' })).toBeVisible()
}

async function assertRouteOracle(page: Page, routeName: string) {
  await assertNoHorizontalScroll(page)
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible()
  if (routeName === 'overview' || routeName === 'basin-detail') await assertM11LayoutOracle(page)
  if (routeName === 'flood-alerts') {
    await expect(page.getByLabel('洪水预警地图')).toBeVisible()
    await expect(page.getByTestId('flood-alert-timeline')).toBeVisible()
    await assertVerticalNonOverlap(page, '[aria-label="洪水预警地图"]', '[data-testid="flood-alert-timeline"]')
  }
  if (routeName === 'monitoring') {
    await expect(page.getByRole('button', { name: /刷新/ })).toBeVisible()
    await expect(page.getByRole('button', { name: /查看日志/ })).toBeVisible()
  }
}

async function captureEvidence(page: Page, route: { name: string; path: string; stateLabel: string }, viewportLabel: string) {
  await mkdir(screenshotRoot, { recursive: true })
  const artifactPath = path.join(screenshotRoot, `${route.name}-${viewportLabel}-${route.stateLabel}.png`)
  await page.screenshot({ path: artifactPath, fullPage: true })
  manifestEntries.push({
    route: route.path,
    viewport: viewportLabel,
    fixtureMode: 'm15-deterministic-playwright',
    sha: commitSha,
    stateLabel: route.stateLabel,
    command: captureCommand,
    artifactPath: path.relative(path.resolve(process.cwd(), '../..'), artifactPath),
  })
}

test.beforeEach(async ({ page }) => {
  await mockM15Apis(page)
})

test.afterAll(async () => {
  await mkdir(evidenceRoot, { recursive: true })
  const requiredEvidenceCount = requiredRoutes.length * requiredViewports.length
  if (manifestEntries.filter((entry) => requiredRoutes.some((route) => route.path === entry.route)).length !== requiredEvidenceCount) {
    throw new Error('M15 required route evidence matrix is incomplete.')
  }
  for (const entry of manifestEntries) {
    for (const key of ['route', 'viewport', 'fixtureMode', 'sha', 'stateLabel', 'command', 'artifactPath'] as const) {
      if (!entry[key]) throw new Error(`M15 manifest entry missing ${key}.`)
    }
  }
  await writeFile(
    manifestPath,
    JSON.stringify(
      {
        issue: 176,
        generatedAt: new Date().toISOString(),
        command: captureCommand,
        sha: commitSha,
        evidenceRoot: path.relative(path.resolve(process.cwd(), '../..'), evidenceRoot),
        entries: manifestEntries,
      },
      null,
      2,
    ),
  )
})

test.describe('M15 visual conformance evidence', () => {
  for (const route of requiredRoutes) {
    for (const viewport of requiredViewports) {
      test(`${route.name} ${viewport.label} loaded evidence`, async ({ page }) => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height })
        await prepareRoute(page, route.path)
        await waitForRouteReady(page, route.name, route.stateLabel)
        await assertRouteOracle(page, route.name)
        await captureEvidence(page, route, viewport.label)
      })
    }
  }

  for (const route of extendedRoutes) {
    test(`${route.name} extended deterministic evidence`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepareRoute(page, route.path)
      await waitForRouteReady(page, route.name, route.stateLabel)
      await assertNoHorizontalScroll(page)
      await captureEvidence(page, route, '1440x900')
    })
  }

  for (const route of stateRoutes) {
    test(`${route.name} state accessibility evidence`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepareRoute(page, route.path)
      await waitForRouteReady(page, route.name, route.stateLabel)
      await assertNoHorizontalScroll(page)
      if (route.stateLabel === 'restricted') await expect(page.getByTestId('cldas-restricted')).toContainText('CLDAS 数据权限尚未开通')
      if (route.stateLabel === 'rbac-denied') await expect(page.getByRole('alert')).toContainText('权限不足')
      await captureEvidence(page, route, '1440x900')
    })
  }

  test('shared warning tokens are consistent across overview, basin, and flood pages', async ({ page }) => {
    await prepareRoute(page, '/overview?layer=warning-level')
    await expect(page.locator('[style*="#FFB74D"], [style*="255, 183, 77"]').first()).toBeAttached()
    await prepareRoute(page, '/basins/basin-demo?layer=warning-level&basinVersionId=bv-001&segmentId=seg-009')
    await expect(page.locator('[style*="#FFB74D"], [style*="255, 183, 77"]').first()).toBeAttached()
    await prepareRoute(page, '/flood-alerts')
    await expect(page.locator('[style*="#FFB74D"], [style*="255, 183, 77"]').first()).toBeAttached()
  })
})
