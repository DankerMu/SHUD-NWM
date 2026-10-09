import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { STEP_BUTTON_NAMES, controlBarParts } from './support/controlBar.mocked'
import { CURVE_WINDOWS, installMultipleIssueTimes, type CurveWindowKind } from './support/curveSheet.mocked'
import { LAUNCHER_COLUMN, OPS_LINK, expectOnlyExpanded, overlayPart, type OverlayPanelName } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { setRole } from './support/setRole'
import { ISSUE_TIME_TRIGGER, STATION_VARIABLES, openSheetWithControls } from './support/sheetControls.mocked'
import {
  auditSettledControls,
  belowFormFontFloor,
  belowTouchFloor,
  insideHorizontalScroller,
  outsideViewport,
  summarize,
  type AuditedControl,
  type TouchAudit,
} from './support/touchAudit.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 全页触控与字号审计（openspec mobile-responsive-display task 5.1，design.md D13 / D15）。
 *
 * 遍历式：每个状态下的控件集合由页面 DOM 枚举得出（`support/touchAudit.mocked.ts`），这里不列 testid 清单；
 * “枚举为空也通过”由每个状态的枚举数下限与点名控件挡住。六个状态在三个移动 project 各自的视口下断言
 * 触控下限、表单字号下限与“在视口内”，另在 320×568 下只断言“在视口内”（规格对 320 宽不要求 44）。
 * 三个移动 project 下都要通过、不按 project 跳过。豁免只有 MapLibre attribution 与开发用角色切换器。
 */

const NARROW: ViewportSize = { width: 320, height: 568 }
const LAUNCHER_TEST_IDS: Record<OverlayPanelName, string> = {
  layers: 'm11-launcher-layers',
  basemap: 'm11-launcher-basemap',
  legend: 'm11-launcher-legend',
}
const PANEL_TEST_IDS: Record<OverlayPanelName, string> = {
  layers: 'm11-floating-layer-switcher',
  basemap: 'm11-floating-basemap-switcher',
  legend: 'm11-floating-legend',
}
const CONTROL_BAR_TEST_ID = 'm11-bottom-control-bar'
const LAUNCHER_COLUMN_TEST_ID = 'm11-launcher-column'
const OPS_LINK_TEST_ID = 'm11-ops-link'
const PRECIP_TOGGLE_TEST_ID = 'm11-layer-toggle-precip'

/**
 * `minControls` = 该状态的枚举数下限（实测值；三个移动 project 与 320×568 下相同）：
 * 外壳 11 = 三个启动器 + 预报源两个分段按钮 + 两个原生 select + 步进 / 播放三个按钮 + 有效时间滑块；
 * 图层面板 +3 行（矮视口横屏下被滚出的末行照样计入）；底图面板 +3 个分段按钮；图例面板 +0；
 * 河段窗 2 = 关闭按钮 + 起报时次触发器（外壳让位，不计）；气象代站窗 7 = 再加五个要素切换项。
 */
type AuditState = { name: string; minControls: number } & (
  | { key: 'default' }
  | { key: 'panel'; panel: OverlayPanelName }
  | { key: 'sheet'; kind: CurveWindowKind }
)

const STATES: AuditState[] = [
  { key: 'default', name: '默认', minControls: 11 },
  { key: 'panel', name: '图层面板展开', panel: 'layers', minControls: 14 },
  { key: 'panel', name: '底图面板展开', panel: 'basemap', minControls: 14 },
  { key: 'panel', name: '图例面板展开', panel: 'legend', minControls: 11 },
  { key: 'sheet', name: '河段窗打开', kind: 'river', minControls: 2 },
  { key: 'sheet', name: '气象代站窗打开', kind: 'station', minControls: 7 },
]
/** 运维角色的默认状态：外壳 11 + 运维入口。 */
const OPERATOR_MIN_CONTROLS = 12

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

/** 装基线 mock（9 个有效时刻、一个起报时次）并等到控制条落定（「下一个有效时刻」可用）。 */
async function openMap(page: Page, viewport: ViewportSize) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  await page.goto('/')
  await expectMapControlsMounted(page)
  const parts = controlBarParts(page)
  await expect(parts.stepButtons[2]).toBeEnabled()
  await expect(parts.cycle).toBeEnabled()
  await expect(page.locator(LAUNCHER_COLUMN)).toBeVisible()
}

/** 把页面带到 `state`（视口先设好再导航），返回落定后的枚举。 */
async function enter(page: Page, state: AuditState, viewport: ViewportSize): Promise<TouchAudit> {
  if (state.key === 'sheet') {
    await openSheetWithControls(page, viewport, state.kind, installMultipleIssueTimes)
    await expect(page.getByTestId(ISSUE_TIME_TRIGGER[state.kind])).toBeVisible()
  } else {
    await openMap(page, viewport)
    if (state.key === 'panel') {
      await overlayPart(page, state.panel).launcher.tap()
      await expectOnlyExpanded(page, state.panel)
    }
  }
  const audit = await auditSettledControls(page)
  expect(audit.viewport, '布局视口应等于设定的视口').toEqual(viewport)
  return audit
}

function within(control: AuditedControl, testId: string) {
  return control.testIdPath.includes(testId)
}

function byTestId(controls: AuditedControl[], testId: string) {
  return controls.filter((control) => control.testId === testId)
}

function byAriaLabel(controls: AuditedControl[], tag: string, ariaLabel: string) {
  return controls.filter((control) => control.tag === tag && control.ariaLabel === ariaLabel)
}

/** 默认 / 面板状态都在的外壳控件：三个启动器与控制条的九个控件。 */
function expectChromeControls(controls: AuditedControl[], where: string) {
  for (const testId of Object.values(LAUNCHER_TEST_IDS)) {
    expect(byTestId(controls, testId), `枚举应含启动器 ${testId} @ ${where}`).toHaveLength(1)
  }
  const bar = controls.filter((control) => within(control, CONTROL_BAR_TEST_ID))
  expect(bar.filter((control) => control.tag === 'button' && control.groupLabel === '预报源'), `枚举应含预报源的两个分段按钮 @ ${where}`).toHaveLength(2)
  for (const name of STEP_BUTTON_NAMES) {
    expect(byAriaLabel(bar, 'button', name), `枚举应含「${name}」 @ ${where}`).toHaveLength(1)
  }
  expect(bar.filter((control) => control.tag === 'select').map((control) => control.name).sort(), `枚举应含两个原生 select @ ${where}`).toEqual(['播放速度', '起报时次'])
  expect(bar.filter((control) => control.inputType === 'range'), `枚举应含有效时间滑块 @ ${where}`).toHaveLength(1)
}

/** 每个状态的非空下限与点名下限（口径 (5)）。 */
function expectRepresentatives(state: AuditState, controls: AuditedControl[], where: string) {
  expect(controls.length, `${state.name}的枚举数 @ ${where}`).toBeGreaterThanOrEqual(state.minControls)
  if (state.key === 'sheet') {
    const frame = CURVE_WINDOWS[state.kind]
    const sheet = controls.filter((control) => within(control, frame.testId))
    expect(byAriaLabel(sheet, 'button', frame.closeName), `枚举应含关闭按钮 @ ${where}`).toHaveLength(1)
    expect(byTestId(sheet, ISSUE_TIME_TRIGGER[state.kind]), `枚举应含起报时次触发器 @ ${where}`).toHaveLength(1)
    if (state.kind === 'station') {
      for (const variable of STATION_VARIABLES) {
        expect(byTestId(sheet, `m11-station-variable-toggle-${variable}`), `枚举应含要素切换项 ${variable} @ ${where}`).toHaveLength(1)
      }
    }
    // 外壳让位（4.6）：启动器列与控制条是 `visibility: hidden`，不得出现在枚举里。
    const yielded = controls.filter((control) => within(control, CONTROL_BAR_TEST_ID) || within(control, LAUNCHER_COLUMN_TEST_ID))
    expect(yielded.map((control) => control.name), `让位的启动器 / 控制条控件不应被枚举 @ ${where}`).toEqual([])
    return
  }

  expectChromeControls(controls, where)
  if (state.key !== 'panel') return
  const launcher = byTestId(controls, LAUNCHER_TEST_IDS[state.panel])
  expect(launcher.map((control) => control.ariaExpanded), `${state.name}：启动器应 aria-expanded="true" @ ${where}`).toEqual(['true'])
  const inPanel = controls.filter((control) => within(control, PANEL_TEST_IDS[state.panel]))
  if (state.panel === 'layers') {
    expect(inPanel.filter((control) => control.tag === 'button'), `图层面板内应有三个图层行 @ ${where}`).toHaveLength(3)
    // 基线目录没有降水条目：这一行是 disabled 的，仍计入审计。
    expect(byTestId(inPanel, PRECIP_TOGGLE_TEST_ID).map((control) => control.disabled), `枚举应含 disabled 的降水行 @ ${where}`).toEqual([true])
  } else if (state.panel === 'basemap') {
    expect(inPanel.filter((control) => control.tag === 'button'), `底图面板内应有三个分段按钮 @ ${where}`).toHaveLength(3)
  } else {
    expect(inPanel.map((control) => control.name), `图例面板内没有可点控件 @ ${where}`).toEqual([])
  }
}

function report(name: string, where: string, audit: TouchAudit) {
  console.log(`touch-audit ${name} @ ${where}`, JSON.stringify({ ...summarize(audit.controls), exempt: audit.exempt.map((item) => item.reason) }))
  console.log(
    `touch-audit ${name} controls @ ${where}`,
    JSON.stringify(audit.controls.map((control) => `${control.tag}:${control.name}:${control.box.width}x${control.box.height}:${control.fontSizePx}`)),
  )
}

/** 三条下限 + “在视口内”；`floors` 为 false 时（320×568）只断言“在视口内”。 */
function expectAudit(audit: TouchAudit, where: string, floors: boolean) {
  expect(insideHorizontalScroller(audit.controls), `不应有控件处在横向可滚动的祖先内 @ ${where}`).toEqual([])
  if (floors) {
    expect.soft(belowTouchFloor(audit.controls), `不到 44×44 的可点控件 @ ${where}`).toEqual([])
    expect.soft(belowFormFontFloor(audit.controls), `字号不到 16px 的 select / select 触发器 @ ${where}`).toEqual([])
  }
  expect.soft(outsideViewport(audit), `不在视口内的可点控件 @ ${where}`).toEqual([])
}

test.describe('M11 全页触控与字号审计', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const state of STATES) {
    test(`${state.name}：可点控件 ≥ 44×44、select 字号 ≥ 16px、全部在视口内`, async ({ page }, testInfo) => {
      const audit = await enter(page, state, requireViewport(page))
      const where = label(page, testInfo)
      report(state.name, where, audit)

      expectRepresentatives(state, audit.controls, where)
      expectAudit(audit, where, true)
    })

    test(`320x568 ${state.name}：可点控件全部在视口内`, async ({ page }, testInfo) => {
      const audit = await enter(page, state, NARROW)
      const where = label(page, testInfo)
      report(state.name, where, audit)

      expectRepresentatives(state, audit.controls, where)
      expectAudit(audit, where, false)
    })
  }

  test('默认（运维角色）：运维入口计入审计', async ({ page }, testInfo) => {
    await openMap(page, requireViewport(page))
    await setRole(page, 'operator')
    await expect(page.locator(OPS_LINK)).toBeVisible()
    const audit = await auditSettledControls(page)
    const where = label(page, testInfo)
    report('默认（运维角色）', where, audit)

    expect(audit.controls.length, `枚举数 @ ${where}`).toBeGreaterThanOrEqual(OPERATOR_MIN_CONTROLS)
    expectChromeControls(audit.controls, where)
    expect(byTestId(audit.controls, OPS_LINK_TEST_ID).map((control) => control.tag), `枚举应含运维入口 @ ${where}`).toEqual(['a'])
    expectAudit(audit, where, true)
  })
})
