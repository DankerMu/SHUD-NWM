import { expect, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

// M26 单图 mocked regression：整个展示端 = 一张全屏地图（OverviewPage），旧多页路由
// （/overview /hydro-met /forecast /meteorology /flood-alerts /basins/:id /segments/:id）
// 全部 <LegacyRedirect> 收敛到 `/`（保 search + 加语义参数）。NavBar 已于 #337 删除。
// 本 spec 断言单图行为 + mocked API 请求 identity；不依赖真实后端，不伪装 live receipt。

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

function readyFloodProductQuality(rows: number) {
  return {
    flood_return_period: {
      quality_state: 'ready',
      max_over_window: true,
      result_rows: rows,
      return_period_rows: rows,
      warning_rows: rows,
      unavailable_products: [],
      residual_blockers: [],
    },
  }
}

const runtimeConfig = {
  service_role: 'dev_monolith' as const,
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway' as const,
  display_readonly: false,
}

const overviewBasinVersion = {
  basin_version_id: 'bv-001',
  basin_id: 'basin-demo',
  version_label: 'v2026_01',
  geom: {
    type: 'MultiPolygon',
    coordinates: [[[[100, 30], [105, 30], [105, 35], [100, 35], [100, 30]]]],
  },
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
}

const basinRiverSegments = {
  type: 'FeatureCollection',
  total: 2,
  feature_total: 2,
  limit: 1000,
  offset: 0,
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'seg-001',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        name: 'North Branch 001',
        stream_order: 1,
        length_m: 800,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-009',
        river_segment_id: 'seg-009',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        name: 'Main Stem 009',
        stream_order: 3,
        length_m: 1200,
      },
      geometry: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
    },
  ],
}

async function mockSingleMapApis(
  page: Page,
  options: {
    runSource?: 'gfs' | 'ifs'
    invalidBasin?: boolean
    noSegments?: boolean
    calls?: Array<{ path: string; query: Record<string, string> }>
  } = {},
) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    options.calls?.push({ path: url.pathname, query: Object.fromEntries(url.searchParams.entries()) })

    if (url.pathname === '/api/v1/runtime/config') return fulfill(route, runtimeConfig)
    if (url.pathname === '/api/v1/mvp/qhh/latest-product') {
      const source = (url.searchParams.get('source') ?? 'GFS').toUpperCase()
      return fulfill(route, {
        basin_id: url.searchParams.get('basin_id') ?? 'basin-demo',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        model_id: 'model-demo',
        source_id: source,
        cycle_time: '2026-05-18T00:00:00Z',
        run_id: `run-${source.toLowerCase()}`,
        forcing_version_id: `forc-${source.toLowerCase()}`,
        station_count: 1,
        expected_station_count: 1,
        segment_count: 2,
        expected_segment_count: 2,
        status: 'ready',
        run_status: 'frequency_done',
        valid_time_start: '2026-05-18T00:00:00Z',
        valid_time_end: '2026-05-25T00:00:00Z',
        river_valid_time_start: '2026-05-18T00:00:00Z',
        river_valid_time_end: '2026-05-25T00:00:00Z',
        forcing_valid_time_start: '2026-05-18T00:00:00Z',
        forcing_valid_time_end: '2026-05-25T00:00:00Z',
        available_horizon_hours: 168,
        expected_horizon_hours: 168,
        shorter_horizon: false,
        available_issue_times: ['2026-05-18T00:00:00Z'],
        availability: {
          ready: true,
          unavailable_reasons: [],
          quality_flags: [],
          quality_notes: [],
          return_period_status: 'available',
          return_period_reasons: [],
        },
      })
    }
    if (url.pathname === '/api/v1/met/stations') {
      return fulfill(route, {
        items: [
          {
            station_id: 'HMT-DEMO-001',
            station_name: 'Demo forcing station 001',
            longitude: 101.2,
            latitude: 31.2,
          },
        ],
        total_count: 1,
        limit: Number(url.searchParams.get('limit') ?? 500),
        offset: Number(url.searchParams.get('offset') ?? 0),
        filters: {},
      })
    }
    if (url.pathname === '/api/v1/basins') {
      if (options.invalidBasin) return fulfill(route, [])
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
    if (url.pathname === '/api/v1/basins/basin-demo/versions') return fulfill(route, [overviewBasinVersion])
    if (url.pathname.startsWith('/api/v1/basins/') && url.pathname.endsWith('/versions')) return fulfill(route, [])
    if (url.pathname === '/api/v1/models') {
      return fulfill(route, {
        items: [
          {
            model_id: 'model-demo',
            model_name: 'Demo SHUD',
            basin_id: 'basin-demo',
            basin_name: 'Demo Basin',
            basin_version_id: 'bv-001',
            river_network_version_id: 'rn-v1',
            mesh_version_id: 'mesh-v1',
            calibration_version_id: 'cal-v1',
            segment_count: 2,
            active_flag: true,
            shud_code_version: 'v1',
            created_at: '2026-05-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 200,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/models/model-demo') {
      return fulfill(route, {
        model_id: 'model-demo',
        model_name: 'Demo SHUD',
        basin_id: 'basin-demo',
        basin_name: 'Demo Basin',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        mesh_version_id: 'mesh-v1',
        calibration_version_id: 'cal-v1',
        segment_count: 2,
        active_flag: true,
        shud_code_version: 'v1',
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname === '/api/v1/runs') {
      return fulfill(route, {
        items: [
          {
            run_id: 'run-overview',
            run_type: 'forecast',
            scenario_id: options.runSource === 'ifs' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic',
            model_id: 'model-demo',
            basin_version_id: 'bv-001',
            source_id: options.runSource ?? 'gfs',
            cycle_time: '2026-05-18T00:00:00Z',
            status: 'frequency_done',
            start_time: '2026-05-18T00:00:00Z',
            end_time: '2026-05-18T03:00:00Z',
            product_quality: readyFloodProductQuality(2),
            created_at: '2026-05-18T00:00:00Z',
            updated_at: '2026-05-18T04:00:00Z',
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/layers') {
      return fulfill(route, [
        { layer_id: 'discharge', layer_name: 'River discharge', layer_type: 'hydrology', variables: ['q_down'] },
        { layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: ['return_period'] },
      ])
    }
    if (url.pathname.startsWith('/api/v1/layers/') && url.pathname.endsWith('/valid-times')) {
      return fulfill(route, ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z'])
    }
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 1, pending: 0, idle: 3 })
    if (url.pathname === '/api/v1/pipeline/status') {
      return fulfill(route, {
        source: 'GFS',
        cycle_time: '2026-05-18T00:00:00Z',
        current_state: 'running',
        started_at: '2026-05-18T00:00:00Z',
        updated_at: '2026-05-18T04:00:00Z',
        job_counts: { succeeded: 3, failed: 0, running: 1, pending: 0 },
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      return fulfill(route, {
        run_id: 'run-overview',
        total_segments: 2,
        usable_curves: 2,
        unavailable_count: 0,
        quality_note: null,
        levels: [{ level: 'warning', count: 1, color: '#FFB74D' }],
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') {
      return fulfill(route, {
        items: [
          {
            rank: 1,
            river_segment_id: 'seg-009',
            segment_id: 'seg-009',
            segment_name: 'Main Stem 009',
            basin_version_id: 'bv-001',
            q_value: 123,
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
      })
    }
    if (url.pathname === '/api/v1/tiles/flood-return-period') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ type: 'FeatureCollection', features: [] }),
      })
    }
    if (url.pathname.startsWith('/api/v1/tiles/flood-return-period/')) {
      return route.fulfill({ status: 204, contentType: 'application/x-protobuf', body: Buffer.alloc(0) })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments') {
      if (options.noSegments) return fulfill(route, { ...basinRiverSegments, total: 0, feature_total: 0, features: [] })
      return fulfill(route, basinRiverSegments)
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-001') {
      return fulfill(route, {
        river_segment_id: 'seg-001',
        river_network_version_id: 'rn-v1',
        segment_order: 1,
        downstream_segment_id: null,
        length_m: 800,
        geom: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-009') {
      return fulfill(route, {
        river_segment_id: 'seg-009',
        river_network_version_id: 'rn-v1',
        segment_order: 3,
        downstream_segment_id: null,
        length_m: 1200,
        geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname.endsWith('/forecast-series')) {
      return fulfill(route, {
        river_segment_id: url.pathname.includes('seg-001') ? 'seg-001' : 'seg-009',
        issue_time: '2026-05-18T00:00:00Z',
        variable: 'q_down',
        unit: 'm3/s',
        frequency_thresholds: null,
        segments: [
          {
            scenario: options.runSource === 'ifs' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic',
            scenario_id: options.runSource === 'ifs' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic',
            source: options.runSource === 'ifs' ? 'IFS' : 'GFS',
            segment_role: 'future_7_days',
            data: [{ valid_time: '2026-05-18T06:00:00Z', value: 456 }],
          },
        ],
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/timeline') {
      return fulfill(route, {
        run_id: 'run-overview',
        segment_id: url.searchParams.get('segment_id') ?? 'seg-009',
        river_segment_id: url.searchParams.get('segment_id') ?? 'seg-009',
        river_network_version_id: 'rn-v1',
        timesteps: [],
        timeline: [],
        peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 },
        frequency_thresholds: null,
        quality_note: null,
      })
    }
    if (url.pathname === '/api/v1/lineage/river-point') {
      return fulfill(route, { target_type: 'river_point', target_id: url.searchParams.get('segment_id') ?? 'seg-009', nodes: [], edges: [] })
    }

    throw new Error(`Unhandled single-map API request: ${url.pathname}`)
  })
}

async function navigateInSession(page: Page, pathAndSearch: string) {
  // Wait one painted turn so BrowserRouter's popstate listener is attached after the
  // initial "/" commit. Warmed Vite chunks can make the overview visible before
  // React's passive effects finish, which leaves a synthetic popstate unobserved.
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        window.requestAnimationFrame(() => window.setTimeout(resolve, 0))
      }),
  )
  await page.evaluate((nextUrl) => {
    window.history.pushState({}, '', nextUrl)
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }))
  }, pathAndSearch)
}

async function selectRole(page: Page, roleName: 'Operator') {
  const roleTrigger = page.getByLabel('Role')
  await expect(roleTrigger).toBeVisible()
  await roleTrigger.focus()
  await expect(roleTrigger).toBeFocused()
  await roleTrigger.press('Enter')
  await expect(page.getByRole('listbox')).toBeVisible()
  await page.getByRole('option', { name: roleName }).click()
}

test.describe('M26 single fullscreen map', () => {
  test('renders the national overview fullscreen map at / and normalizes /overview redirect', async ({ page }) => {
    await mockSingleMapApis(page)

    await page.goto('/')
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByTestId('m11-map-surface')).toBeVisible()
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByTestId('m11-floating-layer-switcher')).toBeVisible()
    await expect(page.getByTestId('m11-floating-legend')).toBeVisible()
    await expect(page.getByRole('button', { name: /流量/, pressed: true })).toBeVisible()
    // NavBar 已删除（#337）：全屏地图无导航。
    await expect(page.getByRole('navigation', { name: 'Main navigation' })).toHaveCount(0)

    // /overview 收敛到 /，保 search + 浮层图例随 active layer 渲染。
    await page.goto('/overview?source=gfs&layer=flood-return-period&basemap=terrain')
    await expect(page).toHaveURL(/^[^?]*\/(\?|$)/)
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByTestId('m11-floating-legend')).toContainText('重现期图例')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'terrain')
  })

  test('switches layers through the floating switcher without the retired met-raster entry', async ({ page }) => {
    await mockSingleMapApis(page)

    await page.goto('/?source=gfs')
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    // 默认流量图层选中，浮层图例显示径流量。
    await expect(page.getByRole('button', { name: /流量/, pressed: true })).toBeVisible()
    await expect(page.getByTestId('m11-floating-legend')).toContainText('径流量图例')
    await expect(page.getByRole('button', { name: /气象栅格/ })).toHaveCount(0)
    await expect(page.getByTestId('m11-met-raster-notice')).toHaveCount(0)

    // 切气象代站 → 全国总览未选流域时 honest「请选择流域」，不取假数据。
    await page.getByRole('button', { name: /气象代站/ }).click()
    await expect(page).toHaveURL(/layer=met-stations/)
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-met-station-feature-count', '1')
  })

  test('lands the /flood-alerts redirect on the flood-return-period layer', async ({ page }) => {
    const calls: Array<{ path: string; query: Record<string, string> }> = []
    await mockSingleMapApis(page, { calls })

    // 旧 /flood-alerts → /?layer=flood-return-period（LegacyRedirect extraParams）。
    await page.goto('/flood-alerts?source=gfs')
    await expect(page).toHaveURL(/layer=flood-return-period/)
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    // active layer 切到重现期：浮层图例标题随之更新。
    await expect(page.getByTestId('m11-floating-legend')).toContainText('重现期图例')

    // mocked 请求 identity：单图按 active layer/source 拉总览与图层元数据，不打真实后端。
    // 源在 API 层规范化为大写（gfs → GFS）。
    await expect.poll(() => calls.map((call) => call.path)).toContain('/api/v1/layers')
    await expect.poll(() => calls.some((call) => call.path === '/api/v1/runs' && call.query.source === 'GFS')).toBe(true)
  })

  test('lands the /meteorology redirect on the met-stations layer', async ({ page }) => {
    await mockSingleMapApis(page)

    // 旧 /meteorology → /?layer=met-stations（LegacyRedirect extraParams）。
    await page.goto('/meteorology?source=gfs')
    await expect(page).toHaveURL(/layer=met-stations/)
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByRole('button', { name: /气象代站/, pressed: true })).toBeVisible()
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-met-station-feature-count', '1')
  })

  test('renders the national overview map surface and floating controls', async ({ page }) => {
    await mockSingleMapApis(page)

    await page.goto('/?source=gfs&validTime=2026-05-18T06:00:00Z')
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByTestId('m11-map-surface')).toBeVisible()
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'vector')
    await expect(page.getByTestId('m11-floating-layer-switcher')).toBeVisible()
    await expect(page.getByTestId('m11-floating-legend')).toContainText('径流量图例')
  })

  test('drills into a basin detail map through basinId query (/basins/:id redirect landing)', async ({ page }) => {
    await mockSingleMapApis(page, { runSource: 'ifs' })

    // 旧 /basins/:id → /?basinId=:id（保 search + 路径参数语义化）。
    // 直达带 basinId 的 URL 首挂载会按 Bug-1 合同剥离 basinId；这里先证明旧路由落点，
    // 再用同一浏览器会话内导航进入详情，覆盖真实的会话内钻取合同。
    await page.goto(
      '/basins/basin-demo?source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=flood-return-period&basemap=satellite&basinVersionId=bv-001&segmentId=seg-009',
    )
    await expect(page).toHaveURL(/^[^?]*\/\?/)
    await expect(page).not.toHaveURL(/basinId=/)
    await navigateInSession(
      page,
      '/?source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=flood-return-period&basemap=satellite&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&basinId=basin-demo&segmentId=seg-009',
    )
    await expect(page).toHaveURL(/basinId=basin-demo/)
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByLabel('流域钻取地图')).toBeVisible()
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'satellite')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', 'seg-009')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-basin-river-feature-count', '2')
    await expect(page.getByTestId('m11-back-to-overview')).toBeVisible()

    // 返回总览 → 清 basinId/segmentId，回到全国总览地图。
    await page.getByTestId('m11-back-to-overview').click()
    await expect(page).not.toHaveURL(/basinId=/)
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
  })

  test('honestly reports a missing basin on the detail map', async ({ page }) => {
    await mockSingleMapApis(page, { invalidBasin: true })

    await page.goto('/')
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByTestId('m11-overview-empty')).toBeVisible()
    await navigateInSession(page, '/?basinId=not-a-real-basin')
    await expect(page).toHaveURL(/basinId=not-a-real-basin/)
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    await expect(page.getByLabel('流域钻取地图')).toBeVisible()
    await expect(page.getByTestId('m11-basin-not-found')).toContainText('not-a-real-basin')
  })

  test('shows the ops link only for operator-and-above roles', async ({ page }) => {
    await mockSingleMapApis(page)

    await page.goto('/')
    await expect(page.getByTestId('m11-fullscreen-map')).toBeVisible()
    // 默认 viewer（webServer VITE_AUTH_ROLE=viewer）：无运维直链。
    await expect(page.getByTestId('m11-ops-link')).toHaveCount(0)

    await selectRole(page, 'Operator')
    await expect(page.getByTestId('m11-ops-link')).toBeVisible()
    await expect(page.getByTestId('m11-ops-link')).toHaveAttribute('href', '/ops')
  })
})
