import type { Locator, Page } from '@playwright/test'

/**
 * 区域错误兜底 spec（openspec mobile-responsive-display task 3.6，design.md D8）的开关与量具。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用。API mock 沿用 `opsEntry.mocked.ts`
 * （operator 场景需要它的 runtime config），这里不另立宽路由。
 */

/** `window.__NHMS_E2E_CRASH_REGION__` 的四个取值，与 `src/components/layout/RegionCrashProbe.tsx` 一一对应。 */
export type CrashRegion = 'map-controls' | 'legend' | 'control-bar' | 'curve'

/** 各区域兜底块的 testid（曲线区域的边界叫 map-panels）。 */
export const REGION_FALLBACK_TEST_IDS: Record<CrashRegion, string> = {
  'map-controls': 'region-error-map-controls',
  legend: 'region-error-legend',
  'control-bar': 'region-error-control-bar',
  curve: 'region-error-map-panels',
}

export const ANY_REGION_FALLBACK = '[data-testid^="region-error-"]'
export const MAP_ATTRIBUTION = '.maplibregl-ctrl-attrib'
export const MAP_CANVAS = '[data-testid="m11-map-surface"] canvas.maplibregl-canvas'

type CrashSwitchWindow = { __NHMS_E2E_HOOKS__?: boolean; __NHMS_E2E_CRASH_REGION__?: string }

/**
 * 在页面脚本运行前设好崩溃开关；`gate: false` 时只设开关、不设门控（“无门控零影响”的对照）。
 * 须在 `page.goto` 之前调用。既有 `openRiverWindow` / `openStationWindow` 把设门控与开窗绑在一起，
 * 不适用于“只崩一个区域”的场景，所以这里自己设。
 */
export async function installCrashSwitch(page: Page, options: { gate: boolean; region: CrashRegion }) {
  await page.addInitScript(({ gate, region }) => {
    const target = window as unknown as CrashSwitchWindow
    if (gate) target.__NHMS_E2E_HOOKS__ = true
    target.__NHMS_E2E_CRASH_REGION__ = region
  }, options)
}

/** 页面上门控与开关的当前值（用例自证前提用）。 */
export async function readCrashSwitch(page: Page): Promise<{ gate: unknown; region: unknown }> {
  return page.evaluate(() => {
    const target = window as unknown as CrashSwitchWindow
    return { gate: target.__NHMS_E2E_HOOKS__, region: target.__NHMS_E2E_CRASH_REGION__ }
  })
}

/** 清掉开关（门控保留）：之后「重试」应让区域恢复。 */
export async function clearCrashSwitch(page: Page) {
  await page.evaluate(() => {
    delete (window as unknown as CrashSwitchWindow).__NHMS_E2E_CRASH_REGION__
  })
}

/** 页面上所有 `region-error-*` 的 testid，按 DOM 顺序。 */
export async function regionFallbackTestIds(page: Page): Promise<Array<string | null>> {
  return page.locator(ANY_REGION_FALLBACK).evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-testid')))
}

/** 元素中心的命中测试是否落在它自己身上（没被别的浮层盖住）。 */
export async function hitsItself(locator: Locator): Promise<{ onSelf: boolean; top: string | null }> {
  return locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return {
      onSelf: top !== null && element.contains(top),
      top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
    }
  })
}
