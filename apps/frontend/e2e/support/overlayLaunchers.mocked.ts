import { expect, type Locator, type Page } from '@playwright/test'

import type { Box } from './legendLauncher.mocked'

/**
 * 图层 / 底图 / 图例三个启动器的两个 spec（openspec mobile-responsive-display task 3.3）共用的
 * 选择器与量具。API mock 与几何谓词沿用 `legendLauncher.mocked.ts`，这里不另立一份。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 */

export const OVERLAY_PANELS = ['layers', 'basemap', 'legend'] as const
export type OverlayPanelName = (typeof OVERLAY_PANELS)[number]

/** 启动器列自上而下的顺序即 `OVERLAY_PANELS` 的顺序。 */
export const OVERLAY_PANEL_PARTS: Record<
  OverlayPanelName,
  { title: string; launcher: string; launcherName: string; panel: string; scroller: string }
> = {
  layers: {
    title: '图层',
    launcher: '[data-testid="m11-launcher-layers"]',
    launcherName: '图层',
    panel: '[data-testid="m11-floating-layer-switcher"]',
    scroller: '[data-testid="m11-floating-layer-switcher-scroll"]',
  },
  basemap: {
    title: '底图',
    launcher: '[data-testid="m11-launcher-basemap"]',
    launcherName: '底图',
    panel: '[data-testid="m11-floating-basemap-switcher"]',
    scroller: '[data-testid="m11-floating-basemap-switcher-scroll"]',
  },
  legend: {
    title: '图例',
    launcher: '[data-testid="m11-launcher-legend"]',
    launcherName: '图例',
    panel: '[data-testid="m11-floating-legend"]',
    scroller: '[data-testid="m11-floating-legend-scroll"]',
  },
}

export const MAP_REGION = '[data-testid="m11-fullscreen-map"]'
export const CONTROL_BAR = '[data-testid="m11-bottom-control-bar"]'
export const LAUNCHER_COLUMN = '[data-testid="m11-launcher-column"]'
export const OPS_LINK = '[data-testid="m11-ops-link"]'

/** 面板内的可点项：触控下限审计的对象。 */
export const TAPPABLE_SELECTOR = 'button, a, [role="button"], input, select'

export function overlayPart(page: Page, name: OverlayPanelName) {
  const part = OVERLAY_PANEL_PARTS[name]
  return {
    launcher: page.locator(part.launcher),
    panel: page.locator(part.panel),
    scroller: page.locator(part.scroller),
  }
}

export async function boxOf(locator: Locator, name: string): Promise<Box> {
  const box = await locator.boundingBox()
  expect(box, `${name} 应有可测量的布局矩形`).not.toBeNull()
  return box!
}

/**
 * 互斥展开的完整判据：`name` 的面板可见、其启动器 `aria-expanded="true"`，另外两个面板不在 DOM、
 * 启动器 `aria-expanded="false"`；`name` 为 null = 三个都收起。
 */
export async function expectOnlyExpanded(page: Page, name: OverlayPanelName | null) {
  for (const candidate of OVERLAY_PANELS) {
    const { launcher, panel } = overlayPart(page, candidate)
    const title = OVERLAY_PANEL_PARTS[candidate].title
    if (candidate === name) {
      await expect(panel, `${title}面板应展开`).toBeVisible()
      await expect(panel).toHaveCount(1)
      await expect(launcher).toHaveAttribute('aria-expanded', 'true')
    } else {
      await expect(panel, `${title}面板应收起`).toHaveCount(0)
      await expect(launcher).toHaveAttribute('aria-expanded', 'false')
    }
  }
}

/** 桌面形态：三个启动器与启动器列都不在 DOM。 */
export async function expectNoLaunchers(page: Page) {
  for (const name of OVERLAY_PANELS) {
    await expect(overlayPart(page, name).launcher, `${OVERLAY_PANEL_PARTS[name].title}启动器不应存在`).toHaveCount(0)
  }
  await expect(page.locator(LAUNCHER_COLUMN)).toHaveCount(0)
}

/** 桌面形态：三个面板各恰一个且可见。 */
export async function expectDesktopPanelsVisible(page: Page) {
  for (const name of OVERLAY_PANELS) {
    const { panel } = overlayPart(page, name)
    await expect(panel, `${OVERLAY_PANEL_PARTS[name].title}面板应恰一个`).toHaveCount(1)
    await expect(panel).toBeVisible()
  }
}

/** 三个桌面面板相对地图区的偏移（px）：图层 `left-4 top-4`、底图 `right-16 top-4`、图例 `bottom-[7.5rem] right-4`。 */
export const DESKTOP_PANEL_OFFSETS = {
  layers: { left: 16, top: 16 },
  basemap: { right: 64, top: 16 },
  legend: { right: 16, bottom: 120 },
} as const

const OFFSET_TOLERANCE_PX = 0.5

export async function measureDesktopPanelOffsets(page: Page) {
  const map = await boxOf(page.locator(MAP_REGION), '地图区')
  const layers = await boxOf(overlayPart(page, 'layers').panel, '图层面板')
  const basemap = await boxOf(overlayPart(page, 'basemap').panel, '底图切换器')
  const legend = await boxOf(overlayPart(page, 'legend').panel, '图例')
  return {
    layers: { left: layers.x - map.x, top: layers.y - map.y },
    basemap: { right: map.x + map.width - (basemap.x + basemap.width), top: basemap.y - map.y },
    legend: {
      right: map.x + map.width - (legend.x + legend.width),
      bottom: map.y + map.height - (legend.y + legend.height),
    },
  }
}

export async function expectDesktopPanelsAtOffsets(page: Page, label: string) {
  const measured = await measureDesktopPanelOffsets(page)
  console.log(`launchers desktop offsets @ ${label}`, JSON.stringify(measured))
  const pairs: Array<[string, number, number]> = [
    ['图层面板 left', measured.layers.left, DESKTOP_PANEL_OFFSETS.layers.left],
    ['图层面板 top', measured.layers.top, DESKTOP_PANEL_OFFSETS.layers.top],
    ['底图切换器 right', measured.basemap.right, DESKTOP_PANEL_OFFSETS.basemap.right],
    ['底图切换器 top', measured.basemap.top, DESKTOP_PANEL_OFFSETS.basemap.top],
    ['图例 right', measured.legend.right, DESKTOP_PANEL_OFFSETS.legend.right],
    ['图例 bottom', measured.legend.bottom, DESKTOP_PANEL_OFFSETS.legend.bottom],
  ]
  for (const [name, actual, expected] of pairs) {
    expect.soft(Math.abs(actual - expected), `${name} = ${actual}，期望 ${expected}`).toBeLessThanOrEqual(OFFSET_TOLERANCE_PX)
  }
}
