import { execFileSync } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { expect, test, type Page, type Route } from '@playwright/test'

test.setTimeout(120_000)

const evidenceRoot = path.resolve(process.cwd(), '../../.codex/evidence/issue-176')
const screenshotRoot = path.join(evidenceRoot, 'screenshots')
const manifestPath = path.join(evidenceRoot, 'manifest.json')
const captureCommand = 'cd apps/frontend && corepack pnpm run test:e2e:m15-visual'
const deterministicTilePng = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l1b1NwAAAABJRU5ErkJggg==',
  'base64',
)
const mapChromeZIndexThreshold = 200
const placeholderShaPattern = /^(local-uncommitted|unknown|placeholder|pending|none)$/i
const commitShaPattern = /^[0-9a-f]{40}$/i
const commitSha = resolveCommitSha()

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
  { name: 'overview-partial', path: '/overview?m15State=partial', stateLabel: 'overview-partial-data' },
  { name: 'overview-error', path: '/overview?m15State=error', stateLabel: 'api-error' },
  { name: 'basin-empty', path: '/basins/basin-demo?m15State=empty&basinVersionId=bv-001', stateLabel: 'empty-segments' },
  { name: 'basin-partial', path: '/basins/basin-demo?m15State=partial&basinVersionId=bv-001', stateLabel: 'basin-partial-data' },
  { name: 'basin-error', path: '/basins/basin-demo?m15State=basin-error&basinVersionId=bv-001', stateLabel: 'basin-api-error' },
  { name: 'flood-empty', path: '/flood-alerts?m15State=empty', stateLabel: 'empty-alerts' },
  { name: 'flood-warning-levels', path: '/flood-alerts?m15State=warning-levels', stateLabel: 'warning-levels' },
  { name: 'flood-error', path: '/flood-alerts?m15State=flood-error', stateLabel: 'flood-api-error' },
  { name: 'monitoring-empty', path: '/monitoring?m15State=monitoring-empty', stateLabel: 'empty-jobs' },
  { name: 'monitoring-error', path: '/monitoring?m15State=monitoring-error', stateLabel: 'failed-job-error' },
  { name: 'monitoring-denied', path: '/monitoring?m15Role=viewer', stateLabel: 'rbac-denied' },
  { name: 'segment-missing', path: '/segments/missing-seg?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=missing-seg', stateLabel: 'missing-segment' },
  { name: 'segment-chart-error', path: '/segments/seg-009?m15State=segment-chart-error&source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009', stateLabel: 'chart-error' },
  { name: 'meteorology-grid-unavailable', path: '/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z', stateLabel: 'grid-unavailable' },
  { name: 'meteorology-restricted', path: '/meteorology?tab=grid&source=CLDAS&variable=PRCP', stateLabel: 'grid-restricted-error' },
  { name: 'meteorology-stations-empty', path: '/meteorology?tab=stations&search=no-such-station', stateLabel: 'empty-stations' },
  { name: 'meteorology-station-error', path: '/meteorology?tab=stations&basin=hanjiang&stationId=HMT-HAN-0081', stateLabel: 'station-detail-error' },
  { name: 'model-assets-denied', path: '/system/model-assets?m15Role=viewer', stateLabel: 'model-assets-rbac-denied' },
  { name: 'model-assets-loading', path: '/system/model-assets?m15State=model-loading', stateLabel: 'model-assets-loading' },
  { name: 'model-assets-redacted-error', path: '/system/model-assets?m15State=model-error&modelId=model-demo', stateLabel: 'model-assets-redacted-error' },
] as const

const requiredLoadedMatrix = new Set(
  requiredRoutes.flatMap((route) => requiredViewports.map((viewport) => `${route.path}|${viewport.label}|${route.stateLabel}`)),
)
const requiredStateLabels = new Set(stateRoutes.map((route) => route.stateLabel))

const manifestEntries: EvidenceEntry[] = []
const pageFixtureStates = new WeakMap<Page, string | null>()

interface EvidenceEntry {
  route: string
  viewport: string
  fixtureMode: string
  sha: string
  stateLabel: string
  command: string
  artifactPath: string
}

function resolveCommitSha() {
  const candidates = [
    process.env.M15_EVIDENCE_SHA,
    process.env.GITHUB_PR_HEAD_SHA,
    process.env.PR_HEAD_SHA,
    process.env.GITHUB_SHA,
    process.env.CI_COMMIT_SHA,
  ].filter((value): value is string => Boolean(value))
  const envSha = candidates.find((value) => commitShaPattern.test(value) && !placeholderShaPattern.test(value))
  if (envSha) return envSha

  try {
    const gitSha = execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: path.resolve(process.cwd(), '../..'),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    if (commitShaPattern.test(gitSha) && !placeholderShaPattern.test(gitSha)) return gitSha
  } catch {
    // afterAll also validates the resolved SHA before any manifest is written.
  }

  throw new Error('M15 evidence requires a real 40-character commit SHA from M15_EVIDENCE_SHA, PR head SHA env, GITHUB_SHA, CI_COMMIT_SHA, or git rev-parse HEAD.')
}

async function fulfillError(route: Route, message: string, status = 503) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'error', error: { message } }),
  })
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

function isLocalRequest(url: URL) {
  return ['127.0.0.1', 'localhost', '::1'].includes(url.hostname)
}

function isKnownExternalMapAsset(url: URL, resourceType: string) {
  const knownHosts = new Set([
    'a.tile.opentopomap.org',
    'b.tile.opentopomap.org',
    'c.tile.opentopomap.org',
    'tile.openstreetmap.org',
    'server.arcgisonline.com',
  ])
  if (!knownHosts.has(url.hostname)) return false
  return (
    resourceType === 'image' ||
    resourceType === 'stylesheet' ||
    resourceType === 'font' ||
    /\.(png|jpe?g|webp|pbf|mvt|json|css)$/i.test(url.pathname) ||
    url.pathname.includes('/tile/')
  )
}

async function installM15NetworkGuard(page: Page) {
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())

    if (isLocalRequest(url)) return route.fallback()
    if (url.hostname === 'api.example.test' && url.pathname.startsWith('/api/v1/')) return route.fallback()

    if (isKnownExternalMapAsset(url, request.resourceType())) {
      if (/\.json$/i.test(url.pathname)) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ version: 8, sources: {}, layers: [] }),
        })
      }
      if (/\.css$/i.test(url.pathname)) {
        return route.fulfill({ status: 200, contentType: 'text/css', body: '' })
      }
      if (/\.(pbf|mvt)$/i.test(url.pathname) || request.resourceType() === 'font') {
        return route.fulfill({ status: 200, contentType: 'application/x-protobuf', body: Buffer.alloc(0) })
      }
      return route.fulfill({ status: 200, contentType: 'image/png', body: deterministicTilePng })
    }

    await route.abort('blockedbyclient')
    throw new Error(`Unexpected non-local M15 visual evidence request: ${request.method()} ${request.url()}`)
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
    const state = pageFixtureStates.get(page) ?? new URL(page.url()).searchParams.get('m15State')

    if (state === 'loading' && url.pathname === '/api/v1/basins') return
    if (state === 'error' && url.pathname === '/api/v1/basins') {
      return fulfillError(route, 'm15 fixture API error')
    }
    if (state === 'partial' && url.pathname === '/api/v1/flood-alerts/summary') {
      return fulfillError(route, 'overview partial flood summary fixture')
    }
    if (url.pathname === '/api/v1/basins') {
      return fulfill(route, [
        {
          basin_id: 'basin-demo',
          basin_name: 'Demo Basin',
          basin_group: 'major',
          description: null,
          created_at: '2026-05-01T00:00:00Z',
        },
      ])
    }
    if (url.pathname === '/api/v1/basins/basin-demo/versions') return fulfill(route, [basinVersion])
    if (url.pathname.startsWith('/api/v1/basins/') && url.pathname.endsWith('/versions')) return fulfill(route, [])
    if (url.pathname === '/api/v1/models') {
      if (state === 'model-loading') return
      if (state === 'model-error') return fulfillError(route, 'model source file:///secret/basins?token=redacted must not leak')
      return fulfill(route, { items: [model], total: 1, limit: 200, offset: 0 })
    }
    if (url.pathname === '/api/v1/models/model-demo') {
      if (state === 'model-error') return fulfillError(route, 'model detail /secret/basins/package.zip must not leak')
      return fulfill(route, model)
    }
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
      if (state === 'monitoring-error') return fulfillError(route, 'monitoring fixture API error')
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
      if (state === 'monitoring-empty') return fulfill(route, { items: [], total: 0, limit: 12, offset: 0 })
      return fulfill(route, {
        items: [{ job_id: 'job-m15', run_id: 'run-gfs-1', cycle_id: 'cycle-gfs-1', run_type: 'forecast', scenario: 'forecast_gfs_deterministic', job_type: 'forecast', slurm_job_id: '1001', model_id: 'model-demo', status: 'failed', stage: 'forecast', submitted_at: '2026-05-18T00:01:00Z', started_at: '2026-05-18T00:02:00Z', finished_at: '2026-05-18T00:05:00Z', exit_code: 1, retry_count: 0, error_code: 'M15_PARTIAL', error_message: 'partial fixture failure', log_uri: null, duration_seconds: 180 }],
        total: 1,
        limit: 12,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/jobs/job-m15/logs') {
      return fulfill(route, { job_id: 'job-m15', content: 'm15 deterministic log fixture', truncated: false })
    }
    if (url.pathname === '/api/v1/metrics/stage-duration' || url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      if (state === 'flood-error') return fulfillError(route, 'flood summary fixture API error')
      if (state === 'empty') return fulfill(route, { run_id: 'run-gfs-1', total_segments: 0, usable_curves: 0, unavailable_count: 0, quality_note: null, levels: [] })
      return fulfill(route, { run_id: 'run-gfs-1', total_segments: 2, usable_curves: 2, unavailable_count: 0, quality_note: null, levels: [{ level: 'warning', count: 1, color: '#FFB74D' }] })
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') {
      if (state === 'flood-error') return fulfillError(route, 'flood ranking fixture API error')
      return fulfill(route, state === 'empty' ? { ...floodRanking, items: [], total: 0 } : floodRanking)
    }
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
      if (state === 'basin-error') return fulfillError(route, 'basin river segments fixture API error')
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
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/missing-seg') {
      return fulfillError(route, 'missing segment fixture', 404)
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
    if (url.pathname.endsWith('/forecast-series')) {
      if (state === 'partial') return fulfillError(route, 'basin partial forecast fixture')
      if (state === 'segment-chart-error') return fulfillError(route, 'forecast chart fixture API error')
      return fulfill(route, forecastPayload(url.pathname.split('/river-segments/')[1]?.split('/')[0] ?? 'seg-009'))
    }
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
  pageFixtureStates.set(page, url.searchParams.get('m15State'))
  await page.goto(`${url.pathname}${url.search}`)
  if (routePath.startsWith('/monitoring') && requestedRole !== 'viewer') await selectRoleIfAvailable(page, 'Operator')
  if (routePath.startsWith('/system/model-assets') && requestedRole !== 'viewer') await selectRoleIfAvailable(page, 'Model Admin')
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
  else if (routeName === 'segment-missing') await expect(page.getByRole('heading', { name: '未找到河段 missing-seg' })).toBeVisible({ timeout: 15_000 })
  else if (routeName === 'segment-chart-error') await expect(page.getByRole('heading', { name: 'seg-009' })).toBeVisible({ timeout: 15_000 })
  else if (routeName.startsWith('meteorology')) await expect(page.getByRole('heading', { name: '气象数据产品' })).toBeVisible({ timeout: 15_000 })
  else if (routeName === 'model-assets' || routeName.startsWith('model-assets-')) {
    await expect(stateLabel.includes('rbac-denied') ? page.getByText('权限不足') : page.getByRole('heading', { name: '模型资产管理' })).toBeVisible({ timeout: 15_000 })
  }
}

async function assertNoHorizontalScroll(page: Page) {
  const scrollState = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    html: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(Math.max(scrollState.body, scrollState.html)).toBeLessThanOrEqual(scrollState.viewport + 1)
}

async function readMapChromeThreshold(page: Page) {
  await prepareRoute(page, '/overview')
  await waitForRouteReady(page, 'overview', 'loaded')
  const observed = await page.evaluate(() => {
    const selectors = [
      'header',
      '.maplibregl-ctrl-top-left',
      '.maplibregl-ctrl-bottom-left',
      '[aria-label$="地图"]',
      '[data-testid="m11-timeline-region"]',
    ]
    return selectors.reduce((maxZ, selector) => {
      return Math.max(
        maxZ,
        ...Array.from(document.querySelectorAll(selector)).map((element) => {
          const zIndex = Number.parseInt(getComputedStyle(element).zIndex, 10)
          return Number.isFinite(zIndex) ? zIndex : 0
        }),
      )
    }, 0)
  })
  return Math.max(observed, mapChromeZIndexThreshold)
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

async function assertExtendedRouteOracle(page: Page, routeName: string) {
  await assertNoHorizontalScroll(page)
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible()

  switch (routeName) {
    case 'segment-detail':
      await expect(page.getByRole('heading', { name: 'seg-009' })).toBeVisible()
      await expect(page.getByRole('region', { name: '多源预报曲线' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Analysis' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'GFS' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'IFS' })).toBeVisible()
      break
    case 'meteorology-grid':
      await expect(page.getByTestId('meteorology-grid-map')).toBeVisible()
      await expect(page.getByLabel('气象有效时间')).toBeVisible()
      await expect(page.getByRole('tablist', { name: '气象产品标签' })).toBeVisible()
      await expect(page.getByRole('tab', { name: '空间栅格' })).toBeVisible()
      await expect(page.getByRole('tab', { name: '气象代站' })).toBeVisible()
      await expect(page.getByRole('tab', { name: '空间栅格', selected: true })).toBeVisible()
      break
    case 'meteorology-stations':
      await expect(page.getByTestId('station-inventory')).toBeVisible()
      await expect(page.getByLabel('流域')).toBeVisible()
      await expect(page.getByPlaceholder('station_id / 名称')).toBeVisible()
      await expect(page.getByLabel('排序')).toBeVisible()
      await expect(page.getByLabel('选择站点 HMT-Y2-0237')).toBeVisible()
      await expect(page.getByTestId('forcing-charts')).toBeVisible()
      await expect(page.getByRole('tab', { name: '气象代站', selected: true })).toBeVisible()
      break
    case 'model-assets':
      await expect(page.getByRole('heading', { name: '模型资产管理' })).toBeVisible()
      await expect(page.getByPlaceholder('搜索流域、模型、版本')).toBeVisible()
      await expect(page.getByLabel('模型状态筛选')).toBeVisible()
      await expect(page.getByRole('button', { name: /Demo SHUD/ })).toBeVisible()
      await expect(page.getByText('模型元数据')).toBeVisible()
      await expect(page.getByText('版本时间线 / 依赖图')).toBeVisible()
      await expect(page.getByText('产品资产')).toBeVisible()
      await expect(page.getByText(/secret|token|file:\/\//)).toHaveCount(0)
      break
    default:
      throw new Error(`Unhandled M15 extended route oracle: ${routeName}`)
  }
}

async function assertStateOracle(page: Page, stateLabel: string) {
  await assertNoHorizontalScroll(page)
  switch (stateLabel) {
    case 'loading':
      await expect(page.getByText('总览数据加载中')).toBeVisible()
      break
    case 'overview-partial-data':
      await expect(page.getByText(/flood summary:/).first()).toBeVisible()
      await expect(page.getByTestId('m11-timeline')).toBeVisible()
      break
    case 'api-error':
      await expect(page.getByText('basins: 暂不可用').first()).toBeVisible()
      break
    case 'empty-segments':
      await expect(page.getByText('该流域暂无已发布的预报数据')).toBeVisible()
      await expect(page.getByRole('button', { name: '折叠左侧面板' })).toBeVisible()
      break
    case 'basin-partial-data':
      await expect(page.getByText(/forecast series:/).first()).toBeVisible()
      break
    case 'basin-api-error':
      await expect(page.getByText('river segments: 暂不可用').first()).toBeVisible()
      break
    case 'empty-alerts':
      await expect(page.getByText('暂无洪水预警数据').or(page.getByText('暂无排名数据'))).toBeVisible()
      await expect(page.getByTestId('flood-alert-timeline')).toBeVisible()
      break
    case 'warning-levels':
      await expect(page.getByRole('button', { name: /警戒/ }).first()).toBeVisible()
      await expect(page.locator('[style*="#FFB74D"], [style*="255, 183, 77"]').first()).toBeAttached()
      break
    case 'flood-api-error':
      await expect(page.getByText(/flood summary fixture API error|flood ranking fixture API error|预警统计加载失败|预警排名加载失败/).first()).toBeVisible()
      break
    case 'empty-jobs':
      await expect(page.getByText('暂无作业')).toBeVisible()
      await expect(page.getByLabel('Status filter')).toBeVisible()
      break
    case 'failed-job-error':
      await expect(page.getByText('刷新监控数据失败').or(page.getByText('失败'))).toBeVisible()
      break
    case 'rbac-denied':
    case 'model-assets-rbac-denied':
      await expect(page.getByRole('alert')).toContainText('权限不足')
      break
    case 'missing-segment':
      await expect(page.getByText('missing segment fixture')).toBeVisible()
      break
    case 'chart-error':
      await expect(page.getByText('forecast chart fixture API error').first()).toBeVisible()
      break
    case 'grid-unavailable':
      await expect(page.getByTestId('grid-unavailable')).toContainText('实时栅格瓦片服务尚未接入')
      await expect(page.getByLabel('气象有效时间')).toBeVisible()
      break
    case 'grid-restricted-error':
      await expect(page.getByTestId('cldas-restricted')).toContainText('CLDAS 数据权限尚未开通')
      await expect(page.getByLabel('气象有效时间')).toBeDisabled()
      break
    case 'empty-stations':
      await expect(page.getByTestId('station-empty')).toContainText('搜索无结果')
      await expect(page.getByLabel('流域')).toBeVisible()
      await expect(page.getByLabel('排序')).toBeVisible()
      break
    case 'station-detail-error':
      await expect(page.getByTestId('forcing-unavailable').first()).toBeVisible()
      break
    case 'model-assets-loading':
      await expect(page.getByText('加载中...')).toBeVisible()
      await expect(page.getByPlaceholder('搜索流域、模型、版本')).toBeVisible()
      break
    case 'model-assets-redacted-error':
      await expect(page.getByText('模型资产列表加载失败').first()).toBeVisible()
      await expect(page.getByText(/secret|token|file:\/\//)).toHaveCount(0)
      break
    default:
      throw new Error(`Unhandled M15 state oracle: ${stateLabel}`)
  }
}

async function assertSharedTokenBaseline(page: Page) {
  const mapChromeThreshold = await readMapChromeThreshold(page)
  await prepareRoute(page, '/monitoring')
  const sourceTrigger = page.getByLabel('Source')
  await expect(sourceTrigger).toBeVisible()
  const selectStyles = await sourceTrigger.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      height: style.height,
      radius: style.borderTopLeftRadius,
      gap: style.gap,
    }
  })
  expect(selectStyles).toEqual({ height: '40px', radius: '8px', gap: '8px' })

  await sourceTrigger.click({ force: true })
  const option = page.getByRole('option', { name: 'GFS' })
  await expect(option).toBeVisible()
  const selectContentStyles = await option.evaluate((element) => {
    const content = element.closest('[data-radix-popper-content-wrapper]')?.firstElementChild ?? element.closest('[role="listbox"]')
    if (!content) return null
    const style = getComputedStyle(content)
    return {
      zIndex: style.zIndex,
      radius: style.borderTopLeftRadius,
      shadow: style.boxShadow,
    }
  })
  expect(Number(selectContentStyles?.zIndex)).toBeGreaterThan(mapChromeThreshold)
  expect(selectContentStyles?.radius).toBe('8px')
  expect(selectContentStyles?.shadow).not.toBe('none')
  await page.keyboard.press('Escape')

  await page.getByRole('button', { name: /查看日志/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const dialogStyles = await dialog.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      zIndex: style.zIndex,
      radius: style.borderTopLeftRadius,
      gap: style.gap,
      shadow: style.boxShadow,
    }
  })
  expect(Number(dialogStyles.zIndex)).toBeGreaterThan(mapChromeThreshold)
  expect(dialogStyles.radius).toBe('8px')
  expect(dialogStyles.gap).toBe('16px')
  expect(dialogStyles.shadow).not.toBe('none')
  await page.getByRole('button', { name: 'Close' }).click()

  await prepareRoute(page, '/meteorology?tab=grid')
  const tab = page.getByRole('tab', { name: '空间栅格' })
  await expect(tab).toBeVisible()
  await expect(tab).toHaveCSS('border-radius', '4px')

  const toastStyles = await page.evaluate(() => {
    const toast = document.createElement('div')
    const popover = document.createElement('div')
    const overlay = document.createElement('div')
    popover.className = 'fixed z-[var(--z-popover)]'
    overlay.className = 'fixed z-[var(--z-overlay)]'
    toast.className =
      'group pointer-events-auto relative flex w-full items-start justify-between gap-[var(--space-3)] overflow-hidden rounded-[var(--radius-md)] border border-border bg-panel p-[var(--space-4)] pr-[var(--space-8)] text-foreground shadow-[var(--shadow-lg)] transition-all'
    const toastViewport = document.createElement('div')
    toastViewport.className = 'fixed z-[var(--z-toast)]'
    document.body.appendChild(popover)
    document.body.appendChild(overlay)
    document.body.appendChild(toastViewport)
    document.body.appendChild(toast)
    const style = getComputedStyle(toast)
    const result = {
      popoverZIndex: getComputedStyle(popover).zIndex,
      overlayZIndex: getComputedStyle(overlay).zIndex,
      toastZIndex: getComputedStyle(toastViewport).zIndex,
      radius: style.borderTopLeftRadius,
      shadow: style.boxShadow,
    }
    toast.remove()
    toastViewport.remove()
    overlay.remove()
    popover.remove()
    return result
  })
  expect(Number(toastStyles.popoverZIndex)).toBeGreaterThan(mapChromeThreshold)
  expect(Number(toastStyles.overlayZIndex)).toBeGreaterThan(Number(toastStyles.popoverZIndex))
  expect(Number(toastStyles.toastZIndex)).toBeGreaterThan(Number(toastStyles.overlayZIndex))
  expect(toastStyles.radius).toBe('8px')
  expect(toastStyles.shadow).not.toBe('none')
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
  await installM15NetworkGuard(page)
  await mockM15Apis(page)
})

test.afterAll(async () => {
  await mkdir(evidenceRoot, { recursive: true })
  if (!commitShaPattern.test(commitSha) || placeholderShaPattern.test(commitSha)) {
    throw new Error(`M15 manifest SHA must be a real commit; received ${commitSha}.`)
  }
  if (process.env.CI && !process.env.M15_EVIDENCE_SHA && process.env.GITHUB_SHA && commitSha !== process.env.GITHUB_SHA) {
    throw new Error(`M15 manifest SHA must match GITHUB_SHA in CI; received ${commitSha}, expected ${process.env.GITHUB_SHA}.`)
  }
  if (process.env.CI && process.env.M15_EVIDENCE_SHA && commitSha !== process.env.M15_EVIDENCE_SHA) {
    throw new Error(`M15 manifest SHA must match M15_EVIDENCE_SHA in CI; received ${commitSha}, expected ${process.env.M15_EVIDENCE_SHA}.`)
  }
  const observedLoadedMatrix = new Set(
    manifestEntries
      .filter((entry) => requiredRoutes.some((route) => route.path === entry.route))
      .map((entry) => `${entry.route}|${entry.viewport}|${entry.stateLabel}`),
  )
  const missingLoaded = [...requiredLoadedMatrix].filter((key) => !observedLoadedMatrix.has(key))
  if (missingLoaded.length > 0) {
    throw new Error(`M15 required loaded route/viewport evidence matrix is incomplete: ${missingLoaded.join(', ')}`)
  }
  const observedStateLabels = new Set(manifestEntries.map((entry) => entry.stateLabel))
  const missingStateLabels = [...requiredStateLabels].filter((stateLabel) => !observedStateLabels.has(stateLabel))
  if (missingStateLabels.length > 0) {
    throw new Error(`M15 required state-label evidence matrix is incomplete: ${missingStateLabels.join(', ')}`)
  }
  for (const entry of manifestEntries) {
    for (const key of ['route', 'viewport', 'fixtureMode', 'sha', 'stateLabel', 'command', 'artifactPath'] as const) {
      if (!entry[key]) throw new Error(`M15 manifest entry missing ${key}.`)
    }
    if (!commitShaPattern.test(entry.sha) || placeholderShaPattern.test(entry.sha)) {
      throw new Error(`M15 manifest entry has invalid SHA: ${entry.sha}`)
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
      await assertExtendedRouteOracle(page, route.name)
      await captureEvidence(page, route, '1440x900')
    })
  }

  for (const route of stateRoutes) {
    test(`${route.name} state accessibility evidence`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepareRoute(page, route.path)
      await waitForRouteReady(page, route.name, route.stateLabel)
      await assertStateOracle(page, route.stateLabel)
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

  test('shared control roots inherit M15 token baseline', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await assertSharedTokenBaseline(page)
  })
})
