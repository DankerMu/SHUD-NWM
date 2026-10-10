import { expect, type Page, type Route } from '@playwright/test'

import type { components } from '../../src/api/types'
import type { Box } from './legendLauncher.mocked'
import { openRiverWindow } from './openRiverWindow'
import { openStationWindow } from './openStationWindow'
import { riverFixture } from './riverFixture'
import { MOCK_CYCLE, mockModel, mockRuns, mockStation } from './riverWindow.mocked'
import { isShortLandscape, requireViewport, type ViewportSize } from './viewportForm'

/**
 * 曲线窗抽屉形态两个 spec（openspec mobile-responsive-display task 4.2，design.md D9）共用的
 * mock 变体与量具。文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 *
 * 四个 mock 变体都是在 `installRiverWindowMocks` **之后**注册的更窄 `page.route`（后注册者先匹配），
 * 须在 `page.goto` 之前调用；基线 mock 与两个开窗助手原样复用。
 */
type Schemas = components['schemas']

export type CurveWindowKind = 'river' | 'station'

export const CURVE_WINDOWS: Record<
  CurveWindowKind,
  { name: string; testId: string; chart: string; closeName: string; title: string }
> = {
  river: {
    name: '河段窗',
    testId: 'm11-river-forecast-panel',
    chart: 'm11-river-panel-chart',
    closeName: '关闭面板',
    // 生产瓦片不带河段名，标题回退为河段 ID（见 m11-river-open.mocked.spec.ts）。
    title: riverFixture.identity.riverSegmentId,
  },
  station: {
    name: '气象代站窗',
    testId: 'm11-station-popup',
    chart: 'm11-station-variable-PRCP-chart',
    closeName: '关闭弹窗',
    title: mockStation.station_name,
  },
}
export const CURVE_WINDOW_KINDS = ['river', 'station'] as const

export const MAP_REGION_TEST_ID = 'm11-fullscreen-map'
/** 规格里的公式常数：底部抽屉高 = min(60dvh, 地图区高 − 8px)；右侧抽屉宽 = min(50vw, 28rem)。 */
export const SHEET_HEIGHT_VIEWPORT_RATIO = 0.6
export const SHEET_MAP_HEIGHT_INSET_PX = 8
export const SHEET_WIDTH_VIEWPORT_RATIO = 0.5
export const SHEET_MAX_WIDTH_PX = 448
export const SHEET_TOLERANCE_PX = 0.5

/** 经真实指针输入打开一种曲线窗（从无窗状态；内部导航）。 */
export function openCurveWindow(page: Page, kind: CurveWindowKind) {
  return kind === 'river' ? openRiverWindow(page) : openStationWindow(page)
}

export function curveWindowParts(page: Page, kind: CurveWindowKind) {
  const spec = CURVE_WINDOWS[kind]
  const frame = page.getByTestId(spec.testId)
  return {
    frame,
    handle: page.getByTestId(`${spec.testId}-drag-handle`),
    body: page.getByTestId(`${spec.testId}-body`),
    title: frame.getByText(spec.title, { exact: true }),
    close: frame.getByRole('button', { name: spec.closeName }),
    chartCanvas: page.getByTestId(spec.chart).locator('canvas').first(),
  }
}

// ---------------------------------------------------------------------------
// mock 变体
// ---------------------------------------------------------------------------

export interface HeldRiverForecast {
  /** 当前被挂起的 forecast-series 请求数。 */
  heldCount(): number
  /** 放行全部挂起的请求（交回基线 mock 应答），此后的请求不再挂起。 */
  release(): Promise<void>
}

/** 变体一：河段 forecast-series 请求挂起，直到 `release()`。 */
export async function holdRiverForecast(page: Page): Promise<HeldRiverForecast> {
  const held: Route[] = []
  let released = false
  await page.route(
    (url) => url.pathname.endsWith('/forecast-series'),
    async (route) => {
      if (released) return route.fallback()
      held.push(route)
    },
  )
  return {
    heldCount: () => held.length,
    async release() {
      released = true
      await Promise.all(held.splice(0).map((route) => route.fallback()))
    },
  }
}

/** 变体二里响应带的 station_id：与被点中的站点不同，前端的身份校验因此拒绝绘制。 */
export const MISMATCHED_STATION_ID = 'e2e-station-mismatch'

function sourceParam(value: string | null): 'GFS' | 'IFS' | null {
  const upper = value?.toUpperCase()
  return upper === 'GFS' || upper === 'IFS' ? upper : null
}

/** 变体二：气象代站序列对两个源都回“别的站点”的响应——身份校验失败，窗进入带原因文案的空态。 */
export async function installStationIdentityMismatch(page: Page) {
  await page.route(
    (url) => url.pathname === `/api/v1/met/stations/${mockStation.station_id}/series`,
    (route) => {
      const source = sourceParam(new URL(route.request().url()).searchParams.get('source_id')) ?? 'GFS'
      const data = {
        station_id: MISMATCHED_STATION_ID,
        station: { ...mockStation, station_id: MISMATCHED_STATION_ID },
        forcing_version_id: mockRuns[source].forcing_version_id,
        model_id: mockModel.model_id,
        source_id: source,
        cycle_time: MOCK_CYCLE,
        valid_time_start: mockRuns[source].start_time,
        valid_time_end: mockRuns[source].end_time,
        limit: 480,
        requested_from: null,
        requested_to: null,
        series: [],
      } satisfies Schemas['StationSeriesResponse']
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
    },
  )
}

/** 变体三多列的那个更早的起报时次（默认时次之前 6 小时）。 */
export const MOCK_EARLIER_CYCLE = new Date(Date.parse(MOCK_CYCLE) - 6 * 60 * 60 * 1000).toISOString().replace('.000Z', 'Z')

export interface IssueTimeMockLog {
  /** latest-product 请求带的 `cycle_time` 参数（没带为 null），按请求顺序。 */
  requestedCycles: Array<string | null>
}

/**
 * 变体三：latest-product 的 `available_issue_times` 多列一个更早的起报时次。带 `cycle_time` 的请求
 * 原样回显该时次（200、列表不变）。只用于断言“形态切换后选中值不变”：更早时次的曲线能否加载不作保证。
 * 响应由基线 mock 的导出常量拼出（基线的 latest-product 构造函数没有导出）。
 */
export async function installMultipleIssueTimes(page: Page): Promise<IssueTimeMockLog> {
  const log: IssueTimeMockLog = { requestedCycles: [] }
  await page.route(
    (url) => url.pathname === '/api/v1/mvp/qhh/latest-product',
    (route) => {
      const query = new URL(route.request().url()).searchParams
      const source = sourceParam(query.get('source'))
      if (!source) return route.fallback()
      const requested = query.get('cycle_time')
      log.requestedCycles.push(requested)
      const run = mockRuns[source]
      const data = {
        basin_id: run.basin_id,
        model_id: mockModel.model_id,
        basin_version_id: run.basin_version_id,
        river_network_version_id: run.river_network_version_id,
        available_issue_times: [MOCK_CYCLE, MOCK_EARLIER_CYCLE],
        source_id: source,
        cycle_time: requested ?? MOCK_CYCLE,
        run_id: run.run_id,
        forcing_version_id: run.forcing_version_id,
        station_count: 1,
        expected_station_count: 1,
        segment_count: 1,
        expected_segment_count: 1,
        status: 'ready',
        run_status: 'published',
        valid_time_start: run.start_time,
        valid_time_end: run.end_time,
        river_valid_time_start: run.start_time,
        river_valid_time_end: run.end_time,
        forcing_valid_time_start: run.start_time,
        forcing_valid_time_end: run.end_time,
        available_horizon_hours: 24,
        expected_horizon_hours: 24,
        shorter_horizon: false,
        availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
        quality: {
          station_sample_count: 0,
          river_sample_count: 0,
          required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn'],
          station_variable_coverage: [],
          candidate_limit: 0,
          search_limit: 0,
          context_limit: 0,
          query_indexes: [],
        },
      } satisfies Schemas['QhhLatestProduct']
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
    },
  )
  return log
}

/** 「保留时次」状态下触发器与选项标签带的后缀（生产文案）。 */
export const RETENTION_UNAVAILABLE_SUFFIX = ' · 磁盘保留不可用'

/**
 * 变体四：选中的更早时次已被保留策略清掉。不带 `cycle_time` 的请求列出两个时次；带 `cycle_time` 的请求
 * 回退到最新一轮——`cycle_time` 为最新时次、`available_issue_times` 只剩最新时次（做法同
 * `M11RiverForecastPanel.test.tsx` 的桩）。在下拉里选中更早时次后，窗进入空态，触发器显示带
 * 「磁盘保留不可用」后缀的保留时次。与变体三是两个独立函数：变体三的“原样回显”语义不变。
 */
export async function installRetainedIssueTime(page: Page): Promise<IssueTimeMockLog> {
  const log: IssueTimeMockLog = { requestedCycles: [] }
  await page.route(
    (url) => url.pathname === '/api/v1/mvp/qhh/latest-product',
    (route) => {
      const query = new URL(route.request().url()).searchParams
      const source = sourceParam(query.get('source'))
      if (!source) return route.fallback()
      const requested = query.get('cycle_time')
      log.requestedCycles.push(requested)
      const run = mockRuns[source]
      const data = {
        basin_id: run.basin_id,
        model_id: mockModel.model_id,
        basin_version_id: run.basin_version_id,
        river_network_version_id: run.river_network_version_id,
        available_issue_times: requested ? [MOCK_CYCLE] : [MOCK_CYCLE, MOCK_EARLIER_CYCLE],
        source_id: source,
        cycle_time: MOCK_CYCLE,
        run_id: run.run_id,
        forcing_version_id: run.forcing_version_id,
        station_count: 1,
        expected_station_count: 1,
        segment_count: 1,
        expected_segment_count: 1,
        status: 'ready',
        run_status: 'published',
        valid_time_start: run.start_time,
        valid_time_end: run.end_time,
        river_valid_time_start: run.start_time,
        river_valid_time_end: run.end_time,
        forcing_valid_time_start: run.start_time,
        forcing_valid_time_end: run.end_time,
        available_horizon_hours: 24,
        expected_horizon_hours: 24,
        shorter_horizon: false,
        availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
        quality: {
          station_sample_count: 0,
          river_sample_count: 0,
          required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn'],
          station_variable_coverage: [],
          candidate_limit: 0,
          search_limit: 0,
          context_limit: 0,
          query_indexes: [],
        },
      } satisfies Schemas['QhhLatestProduct']
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
    },
  )
  return log
}

// ---------------------------------------------------------------------------
// 量具
// ---------------------------------------------------------------------------

export interface CurveWindowMeasure {
  map: Box
  frame: Box
  handle: Box
  /** 窗的 offsetParent 的 testid：`100%` 的包含块必须是地图区。 */
  offsetParentTestId: string | null
  /** 窗上的内联定位（没写为空串）。 */
  inline: { left: string; top: string; visibility: string }
  aspectRatio: string
  bottomLeftRadius: string
  handleCursor: string
  handleInsideBody: boolean
  /** 主体容器；不存在时为 null。 */
  body: { display: string; overflowY: string; scrollHeight: number; clientHeight: number } | null
}

export async function measureCurveWindow(page: Page, kind: CurveWindowKind): Promise<CurveWindowMeasure> {
  const measured = await page.evaluate(
    ({ testId, mapTestId }) => {
      const find = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`)
      const frame = find(testId)
      const handle = find(`${testId}-drag-handle`)
      const map = find(mapTestId)
      if (!frame || !handle || !map) return null
      const body = find(`${testId}-body`)
      const box = (element: Element) => {
        const rect = element.getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      }
      const frameStyle = getComputedStyle(frame)
      const offsetParent = frame.offsetParent
      return {
        map: box(map),
        frame: box(frame),
        handle: box(handle),
        offsetParentTestId: offsetParent ? offsetParent.getAttribute('data-testid') : null,
        inline: { left: frame.style.left, top: frame.style.top, visibility: frame.style.visibility },
        aspectRatio: frameStyle.aspectRatio,
        bottomLeftRadius: frameStyle.borderBottomLeftRadius,
        handleCursor: getComputedStyle(handle).cursor,
        handleInsideBody: body ? body.contains(handle) : false,
        body: body
          ? {
              display: getComputedStyle(body).display,
              overflowY: getComputedStyle(body).overflowY,
              scrollHeight: body.scrollHeight,
              clientHeight: body.clientHeight,
            }
          : null,
      }
    },
    { testId: CURVE_WINDOWS[kind].testId, mapTestId: MAP_REGION_TEST_ID },
  )
  if (!measured) throw new Error(`measureCurveWindow: the ${kind} window, its drag handle or the map region is not in the DOM`)
  return measured
}

export type SheetAnchor = 'bottom' | 'right'

export function expectedSheetAnchor(viewport: ViewportSize): SheetAnchor {
  return isShortLandscape(viewport) ? 'right' : 'bottom'
}

/** 规格公式给出的抽屉包围盒（只由视口与地图区决定）。 */
export function expectedSheetBox(viewport: ViewportSize, map: Box): Box {
  if (expectedSheetAnchor(viewport) === 'right') {
    const width = Math.min(SHEET_WIDTH_VIEWPORT_RATIO * viewport.width, SHEET_MAX_WIDTH_PX)
    return { x: map.x + map.width - width, y: map.y, width, height: map.height }
  }
  const height = Math.min(SHEET_HEIGHT_VIEWPORT_RATIO * viewport.height, map.height - SHEET_MAP_HEIGHT_INSET_PX)
  return { x: map.x, y: map.y + map.height - height, width: map.width, height }
}

function near(actual: number, expected: number) {
  return Math.abs(actual - expected) <= SHEET_TOLERANCE_PX
}

/** 抽屉包围盒与公式值的偏差清单；空 = 符合。 */
export function sheetBoxMismatches(measure: CurveWindowMeasure, viewport: ViewportSize): string[] {
  const { frame, map } = measure
  const expected = expectedSheetBox(viewport, map)
  const mismatches: string[] = []
  const check = (name: string, actual: number, wanted: number) => {
    if (!near(actual, wanted)) mismatches.push(`${name} ${actual} != ${wanted}`)
  }
  if (expectedSheetAnchor(viewport) === 'right') {
    check('上边', frame.y, map.y)
    check('下边', frame.y + frame.height, map.y + map.height)
    check('右边', frame.x + frame.width, map.x + map.width)
    check('宽度', frame.width, expected.width)
  } else {
    check('左边', frame.x, map.x)
    check('右边', frame.x + frame.width, map.x + map.width)
    check('下边', frame.y + frame.height, map.y + map.height)
    check('高度', frame.height, expected.height)
  }
  return mismatches
}

/**
 * 断言窗此刻是当前视口应有的那种抽屉（底部 / 右侧），并返回这次测量。
 * 视口刚变过时形态经 `matchMedia` 订阅异步落定，所以用 `toPass` 轮询到相符为止。
 */
export async function expectSheet(page: Page, kind: CurveWindowKind, where: string): Promise<CurveWindowMeasure> {
  const viewport = requireViewport(page)
  let last: CurveWindowMeasure | undefined
  await expect(async () => {
    last = await measureCurveWindow(page, kind)
    expect(sheetBoxMismatches(last, viewport), `${CURVE_WINDOWS[kind].name}应为${expectedSheetAnchor(viewport) === 'right' ? '右侧' : '底部'}抽屉 @ ${where}`).toEqual([])
  }).toPass({ timeout: 4_000 })
  const measure = last!
  console.log(`curve-sheet ${kind} @ ${where}`, JSON.stringify(measure))
  expect(measure.offsetParentTestId, `窗的包含块应是地图区 @ ${where}`).toBe(MAP_REGION_TEST_ID)
  expect(measure.inline, `抽屉不应有内联定位 @ ${where}`).toEqual({ left: '', top: '', visibility: '' })
  expect(measure.aspectRatio, `抽屉不应有固定宽高比 @ ${where}`).toBe('auto')
  await expect(page.getByTestId(CURVE_WINDOWS[kind].testId), `抽屉应可见 @ ${where}`).toBeVisible()
  return measure
}

/** 两个盒的交集；不相交（含仅贴边）为 null。 */
export function intersectionOf(a: Box, b: Box): Box | null {
  const x = Math.max(a.x, b.x)
  const y = Math.max(a.y, b.y)
  const width = Math.min(a.x + a.width, b.x + b.width) - x
  const height = Math.min(a.y + a.height, b.y + b.height) - y
  return width > 0 && height > 0 ? { x, y, width, height } : null
}

/**
 * 点 `(x, y)` 的命中测试是否落在该窗内。给了 `beneathSelector` 时，`beneath` 说明该选择器的元素
 * 是否也在这一点的命中栈里（`elementsFromPoint`）——它为 true 才说明“窗盖住了它”，而不是它本来就不在这一点。
 */
export async function hitsCurveWindow(page: Page, kind: CurveWindowKind, point: { x: number; y: number }, beneathSelector?: string) {
  return page.evaluate(
    ({ testId, x, y, beneathSelector }) => {
      const frame = document.querySelector(`[data-testid="${testId}"]`)
      const top = document.elementFromPoint(x, y)
      const beneath = beneathSelector ? document.querySelector(beneathSelector) : null
      return {
        inside: Boolean(frame && top && frame.contains(top)),
        top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
        beneath: Boolean(beneath && document.elementsFromPoint(x, y).some((element) => beneath.contains(element))),
      }
    },
    { testId: CURVE_WINDOWS[kind].testId, x: point.x, y: point.y, beneathSelector: beneathSelector ?? null },
  )
}

export function centerOf(box: Box) {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 }
}

/** 鼠标在 `from` 按下、分步移动 `(dx, dy)`、抬起。 */
export async function dragWithMouse(page: Page, from: { x: number; y: number }, dx: number, dy: number) {
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(from.x + dx, from.y + dy, { steps: 5 })
  await page.mouse.up()
}

/** 经 CDP 派发一次真实触摸拖动：按下、分步移动 `(dx, dy)`、抬起。 */
export async function dragWithTouch(page: Page, from: { x: number; y: number }, dx: number, dy: number) {
  const session = await page.context().newCDPSession(page)
  try {
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: from.x, y: from.y }] })
    const steps = 5
    for (let step = 1; step <= steps; step += 1) {
      await session.send('Input.dispatchTouchEvent', {
        type: 'touchMove',
        touchPoints: [{ x: from.x + (dx * step) / steps, y: from.y + (dy * step) / steps }],
      })
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  } finally {
    await session.detach()
  }
}
