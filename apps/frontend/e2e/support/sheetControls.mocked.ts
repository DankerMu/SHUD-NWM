import { expect, type Locator, type Page } from '@playwright/test'

import type { components } from '../../src/api/types'
import { CURVE_WINDOWS, openCurveWindow, type CurveWindowKind } from './curveSheet.mocked'
import type { Box } from './legendLauncher.mocked'
import { MOCK_CYCLE, installRiverWindowMocks, mockModel, mockRuns } from './riverWindow.mocked'
import { sheetChartCanvas, type SheetChartTarget } from './sheetChart.mocked'
import type { ViewportSize } from './viewportForm'

/**
 * 抽屉内控件触控下限两个 spec（openspec mobile-responsive-display task 4.9，design.md D13）共用的
 * mock 变体与量具。文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 */
type Schemas = components['schemas']

/** 规格下限：触控目标 44×44，表单字号 16px。 */
export const MIN_TOUCH_TARGET_PX = 44
export const MIN_FORM_FONT_PX = 16

/** 两种窗的起报时次触发器。 */
export const ISSUE_TIME_TRIGGER: Record<CurveWindowKind, string> = {
  river: 'm11-river-panel-cycle',
  station: 'm11-popup-issue-time',
}
/** 两种窗里包住起报时次触发器的那一条。 */
export const ISSUE_TIME_BAR: Record<CurveWindowKind, string> = {
  river: 'm11-river-panel-cycle-bar',
  station: 'm11-station-cycle-bar',
}
export const STATION_TOOLBAR = 'm11-station-toolbar'
export const STATION_VARIABLE_SELECTOR = 'm11-station-variable-selector'
export const STATION_LOADED = 'm11-station-popup-loaded'
export const STATION_VARIABLES = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn'] as const

/** 图表区（不是气象代站的卡片）：两种窗“曲线已加载”的判据与口径 (4) 的量取对象。 */
export const SHEET_CHART: Record<CurveWindowKind, SheetChartTarget> = {
  river: { kind: 'river' },
  station: { kind: 'station', chartTestId: 'm11-station-panel-chart' },
}

/** 抽屉内的可交互控件（触控审计的枚举范围；没有豁免名单）。 */
export const INTERACTIVE_SELECTOR = 'button, a[href], [role="tab"], [role="combobox"], select, input'
/** 抽屉内的表单选择控件（表单字号审计的枚举范围）。 */
export const FORM_SELECT_SELECTOR = 'select, [role="combobox"]'

/**
 * 以 `MOCK_CYCLE` 为首、每隔 6 小时往前数的 `count` 个起报时次（新 → 旧）。
 */
export function mockIssueTimes(count: number): string[] {
  return Array.from({ length: count }, (_, index) =>
    new Date(Date.parse(MOCK_CYCLE) - index * 6 * 60 * 60 * 1000).toISOString().replace('.000Z', 'Z'),
  )
}

/**
 * mock 变体：latest-product 的 `available_issue_times` 列出 `issueTimes`（下拉列表长到必须滚动）。
 * 带 `cycle_time` 的请求原样回显该时次。须在 `installRiverWindowMocks` 之后、`page.goto` 之前调用
 * （后注册者先匹配）。响应由基线 mock 的导出常量拼出，形状同 `installMultipleIssueTimes`
 * （`curveSheet.mocked.ts`）；那一个只列两个时次且不接受参数，所以这里另写。
 */
export async function installIssueTimes(page: Page, issueTimes: string[]) {
  await page.route(
    (url) => url.pathname === '/api/v1/mvp/qhh/latest-product',
    (route) => {
      const query = new URL(route.request().url()).searchParams
      const upper = query.get('source')?.toUpperCase()
      if (upper !== 'GFS' && upper !== 'IFS') return route.fallback()
      const run = mockRuns[upper]
      const data = {
        basin_id: run.basin_id,
        model_id: mockModel.model_id,
        basin_version_id: run.basin_version_id,
        river_network_version_id: run.river_network_version_id,
        available_issue_times: issueTimes,
        source_id: upper,
        cycle_time: query.get('cycle_time') ?? MOCK_CYCLE,
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
}

/**
 * 设视口、装基线 mock（外加可选的变体）、经真实指针输入开窗，并等到“曲线已加载”（图表 canvas 可见）。
 */
export async function openSheetWithControls(page: Page, viewport: ViewportSize, kind: CurveWindowKind, variant?: (page: Page) => Promise<unknown>) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  if (variant) await variant(page)
  await openCurveWindow(page, kind)
  await expect(sheetChartCanvas(page, SHEET_CHART[kind]), '曲线应已加载（图表 canvas 可见）').toBeVisible()
}

export function sheetControlParts(page: Page, kind: CurveWindowKind) {
  const spec = CURVE_WINDOWS[kind]
  const frame = page.getByTestId(spec.testId)
  return {
    frame,
    close: frame.getByRole('button', { name: spec.closeName }),
    trigger: frame.getByTestId(ISSUE_TIME_TRIGGER[kind]),
    issueTimeBar: frame.getByTestId(ISSUE_TIME_BAR[kind]),
    /** 下拉内容在 portal 里（抽屉之外）：同一时刻页面上只有一个展开的列表。 */
    listbox: page.getByRole('listbox'),
    options: page.getByRole('option'),
  }
}

export function stationVariableToggle(page: Page, variable: (typeof STATION_VARIABLES)[number]) {
  return page.getByTestId(CURVE_WINDOWS.station.testId).getByTestId(`m11-station-variable-toggle-${variable}`)
}

export interface ControlMeasure {
  /** 失败信息里用来认出控件的名字：aria-label、文本或标签名。 */
  name: string
  box: Box
  fontSizePx: number
  /** 计算后的 `min-width`（弹性子项没设时为 `auto`）。 */
  minWidth: string
}

/** 量一组控件：只留可见的（有布局盒、`visibility` 不是 hidden）。 */
export async function measureControls(locator: Locator): Promise<ControlMeasure[]> {
  return locator.evaluateAll((elements) =>
    elements.flatMap((element) => {
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden') return []
      const name = (element.getAttribute('aria-label') ?? element.textContent ?? '').trim() || element.tagName.toLowerCase()
      return [
        {
          name,
          box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          fontSizePx: Number.parseFloat(style.fontSize),
          minWidth: style.minWidth,
        },
      ]
    }),
  )
}

export async function measureControl(locator: Locator, name: string): Promise<ControlMeasure> {
  const measured = await measureControls(locator)
  expect(measured, `${name} 应恰有一个可见节点`).toHaveLength(1)
  return measured[0]
}

export interface IssueTimeTriggerMeasure {
  box: Box
  fontSizePx: number
  title: string | null
  /** 触发器自身的内容盒与滚动盒：`scroll* > client*` 即文字出框。 */
  clientWidth: number
  clientHeight: number
  scrollWidth: number
  scrollHeight: number
  /** 值元素：触发器的直接子 `span`（箭头是 `svg`）。 */
  value: { text: string; box: Box; clientWidth: number; scrollWidth: number; textOverflow: string }
  /** 值文本的日期时间部分（`MM-DD HH:MM UTC`）加省略号，用值元素自己的字体单行排出来的宽度。 */
  dateTimeWithEllipsisWidth: number
}

/**
 * 量起报时次触发器里的文字是否出框、是否被省略。日期时间部分的宽度用一个离屏量具量：
 * 复制值元素的计算字体，不钉死像素（实际字体随平台与 #2861 变）。
 */
export async function measureIssueTimeTrigger(trigger: Locator): Promise<IssueTimeTriggerMeasure> {
  return trigger.evaluate((element) => {
    const value = element.querySelector<HTMLElement>(':scope > span')
    if (!value) throw new Error('measureIssueTimeTrigger: the trigger has no direct `span` child holding the value')
    const box = (target: Element) => {
      const rect = target.getBoundingClientRect()
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    }
    const valueStyle = getComputedStyle(value)
    const text = (value.textContent ?? '').trim()
    const probe = document.createElement('span')
    probe.textContent = `${text.split(' · ')[0]}\u2026`
    Object.assign(probe.style, {
      position: 'absolute',
      visibility: 'hidden',
      whiteSpace: 'nowrap',
      fontFamily: valueStyle.fontFamily,
      fontSize: valueStyle.fontSize,
      fontWeight: valueStyle.fontWeight,
      fontStyle: valueStyle.fontStyle,
      fontStretch: valueStyle.fontStretch,
      fontVariantNumeric: valueStyle.fontVariantNumeric,
      fontFeatureSettings: valueStyle.fontFeatureSettings,
      letterSpacing: valueStyle.letterSpacing,
    })
    document.body.appendChild(probe)
    const dateTimeWithEllipsisWidth = probe.getBoundingClientRect().width
    probe.remove()
    return {
      box: box(element),
      fontSizePx: Number.parseFloat(getComputedStyle(element).fontSize),
      title: element.getAttribute('title'),
      clientWidth: element.clientWidth,
      clientHeight: element.clientHeight,
      scrollWidth: element.scrollWidth,
      scrollHeight: element.scrollHeight,
      value: { text, box: box(value), clientWidth: value.clientWidth, scrollWidth: value.scrollWidth, textOverflow: valueStyle.textOverflow },
      dateTimeWithEllipsisWidth,
    }
  })
}

/** 宽或高不到触控下限的控件清单；空 = 全部达标。 */
export function belowTouchTarget(controls: ControlMeasure[]): string[] {
  return controls
    .filter(({ box }) => box.width < MIN_TOUCH_TARGET_PX || box.height < MIN_TOUCH_TARGET_PX)
    .map(({ name, box }) => `${name} ${box.width}x${box.height}`)
}

/** `inner` 的左右两边是否都在 `outer` 的水平范围内。 */
export function withinHorizontally(outer: Box, inner: Box, tolerance = 0.5): boolean {
  return inner.x >= outer.x - tolerance && inner.x + inner.width <= outer.x + outer.width + tolerance
}

/** 下拉列表里真正滚动的那一层（Radix 的 viewport）的滚动量。 */
export async function measureListScroller(page: Page) {
  return page.getByRole('listbox').locator('[data-radix-select-viewport]').evaluate((element) => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    scrollTop: element.scrollTop,
  }))
}
