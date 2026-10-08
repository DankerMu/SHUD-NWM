import { expect, test, type Page } from '@playwright/test'

import { contains, intersects, type Box } from './support/legendLauncher.mocked'
import { mockOpsEntryApi } from './support/opsEntry.mocked'
import {
  CONTROL_BAR,
  LAUNCHER_COLUMN,
  MAP_REGION,
  OPS_LINK,
  OVERLAY_PANELS,
  OVERLAY_PANEL_PARTS,
  boxOf,
  overlayPart,
  type OverlayPanelName,
} from './support/overlayLaunchers.mocked'
import {
  ANY_REGION_FALLBACK,
  MAP_ATTRIBUTION,
  MAP_CANVAS,
  REGION_FALLBACK_TEST_IDS,
  clearCrashSwitch,
  hitsItself,
  installCrashSwitch,
  readCrashSwitch,
  regionFallbackTestIds,
  type CrashRegion,
} from './support/regionFallbacks.mocked'
import { setRole } from './support/setRole'
import { isMobileForm, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 区域错误兜底的移动位置 + 测试门控的区域崩溃开关（openspec mobile-responsive-display task 3.6，
 * design.md D8）。三个移动 project 下都要通过、不按 project 跳过，三个 project 的断言相同。
 *
 * 不变量：任一区域进入兜底时，兜底块完全在地图区内，不与仍在渲染的其他区域（控制条、各启动器、
 * 运维入口）和 attribution 相交，其余区域保持可操作；无门控时开关没有任何效果。
 */

/** 列内兜底块的高度上限：不高于一个 44px 的启动器（容许亚像素误差）。咬住移动形态的 `py-1`。 */
const MAX_IN_COLUMN_FALLBACK_HEIGHT = 44.5

async function openMapWithCrash(page: Page, options: { gate: boolean; region: CrashRegion }) {
  await installCrashSwitch(page, options)
  await page.goto('/')
  await expectMapControlsMounted(page)
}

function fallbackOf(page: Page, region: CrashRegion) {
  return page.getByTestId(REGION_FALLBACK_TEST_IDS[region])
}

/** (d)：页面上的 `region-error-*` 恰为这一个，地图 canvas 仍在。 */
async function expectOnlyFallback(page: Page, region: CrashRegion) {
  await expect(fallbackOf(page, region)).toBeVisible()
  await expect(page.locator(ANY_REGION_FALLBACK)).toHaveCount(1)
  expect(await regionFallbackTestIds(page)).toEqual([REGION_FALLBACK_TEST_IDS[region]])
  await expect(page.locator(MAP_CANVAS)).toBeVisible()
}

async function launcherBoxes(page: Page, names: readonly OverlayPanelName[]): Promise<Array<{ name: string; box: Box }>> {
  const items: Array<{ name: string; box: Box }> = []
  for (const name of names) {
    const title = `${OVERLAY_PANEL_PARTS[name].title}启动器`
    const launcher = overlayPart(page, name).launcher
    await expect(launcher, `${title}应仍在渲染`).toBeVisible()
    items.push({ name: title, box: await boxOf(launcher, title) })
  }
  return items
}

async function expectLaunchersHittable(page: Page, names: readonly OverlayPanelName[]) {
  for (const name of names) {
    const hit = await hitsItself(overlayPart(page, name).launcher)
    expect.soft(hit.onSelf, `${OVERLAY_PANEL_PARTS[name].title}启动器中心被 ${hit.top} 盖住`).toBe(true)
  }
}

function label(page: Page, projectName: string) {
  const viewport = requireViewport(page)
  return `${projectName} ${viewport.width}x${viewport.height}`
}

test.describe('M11 区域错误兜底移动形态', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    await mockOpsEntryApi(page)
  })

  test('门控 + legend：图例兜底在地图区内，不与控制条和图层 / 底图启动器相交', async ({ page }, testInfo) => {
    await openMapWithCrash(page, { gate: true, region: 'legend' })
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    await expectOnlyFallback(page, 'legend')
    await expect(overlayPart(page, 'legend').launcher, '图例启动器随图例区域一起被兜底取代').toHaveCount(0)

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const fallback = await boxOf(fallbackOf(page, 'legend'), '图例兜底')
    const launchers = await launcherBoxes(page, ['layers', 'basemap'])
    console.log(`region fallback legend @ ${label(page, testInfo.project.name)}`, JSON.stringify({ map, bar, column, fallback, launchers }))

    expect.soft(contains(map, fallback), `图例兜底 ${JSON.stringify(fallback)} 不在地图区 ${JSON.stringify(map)} 内`).toBe(true)
    expect.soft(fallback.height, '图例兜底高于一个启动器会吃掉列高余量').toBeLessThanOrEqual(MAX_IN_COLUMN_FALLBACK_HEIGHT)
    expect.soft(intersects(fallback, bar), `图例兜底与控制条 ${JSON.stringify(bar)} 相交`).toBe(false)
    expect.soft(fallback.y + fallback.height, `图例兜底底边越过控制条顶边 ${bar.y}`).toBeLessThanOrEqual(bar.y)
    for (const item of launchers) {
      expect.soft(intersects(fallback, item.box), `图例兜底与${item.name} ${JSON.stringify(item.box)} 相交`).toBe(false)
    }
    await expectLaunchersHittable(page, ['layers', 'basemap'])
    const hit = await hitsItself(fallbackOf(page, 'legend'))
    expect.soft(hit.onSelf, `图例兜底中心被 ${hit.top} 盖住`).toBe(true)
  })

  test('门控 + map-controls：地图控件兜底在地图区内，不与控制条和图例启动器相交，图例仍可展开', async ({ page }, testInfo) => {
    await openMapWithCrash(page, { gate: true, region: 'map-controls' })
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    await expectOnlyFallback(page, 'map-controls')
    await expect(overlayPart(page, 'layers').launcher).toHaveCount(0)
    await expect(overlayPart(page, 'basemap').launcher).toHaveCount(0)

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const fallback = await boxOf(fallbackOf(page, 'map-controls'), '地图控件兜底')
    const [legend] = await launcherBoxes(page, ['legend'])
    console.log(
      `region fallback map-controls @ ${label(page, testInfo.project.name)}`,
      JSON.stringify({ map, bar, column, fallback, launchers: [legend] }),
    )

    expect.soft(contains(map, fallback), `地图控件兜底 ${JSON.stringify(fallback)} 不在地图区 ${JSON.stringify(map)} 内`).toBe(true)
    expect.soft(fallback.height, '地图控件兜底高于一个启动器会吃掉列高余量').toBeLessThanOrEqual(MAX_IN_COLUMN_FALLBACK_HEIGHT)
    expect.soft(intersects(fallback, bar), `地图控件兜底与控制条 ${JSON.stringify(bar)} 相交`).toBe(false)
    expect.soft(intersects(fallback, legend.box), `地图控件兜底与图例启动器 ${JSON.stringify(legend.box)} 相交`).toBe(false)
    expect.soft(contains(map, legend.box), `图例启动器 ${JSON.stringify(legend.box)} 不在地图区内`).toBe(true)
    expect.soft(legend.box.y + legend.box.height, `图例启动器底边越过控制条顶边 ${bar.y}`).toBeLessThanOrEqual(bar.y)

    // 降级态下图例仍可操作：启动器没被盖住，点它能展开图例面板。
    const launcher = overlayPart(page, 'legend').launcher
    const hit = await hitsItself(launcher)
    expect(hit.onSelf, `图例启动器中心被 ${hit.top} 盖住`).toBe(true)
    await launcher.tap()
    await expect(overlayPart(page, 'legend').panel).toBeVisible()
    await expect(launcher).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator(ANY_REGION_FALLBACK)).toHaveCount(1)
  })

  test('门控 + control-bar：控制条兜底在地图区内，不与任何启动器和 attribution 相交', async ({ page }, testInfo) => {
    await openMapWithCrash(page, { gate: true, region: 'control-bar' })
    await expectOnlyFallback(page, 'control-bar')
    await expect(page.locator(CONTROL_BAR), '控制条随区域一起被兜底取代').toHaveCount(0)

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const fallback = await boxOf(fallbackOf(page, 'control-bar'), '控制条兜底')
    const attribution = await boxOf(page.locator(MAP_ATTRIBUTION).first(), 'attribution')
    const launchers = await launcherBoxes(page, OVERLAY_PANELS)
    console.log(
      `region fallback control-bar @ ${label(page, testInfo.project.name)}`,
      JSON.stringify({ map, column, fallback, attribution, launchers }),
    )

    expect.soft(contains(map, fallback), `控制条兜底 ${JSON.stringify(fallback)} 不在地图区 ${JSON.stringify(map)} 内`).toBe(true)
    // 保留的定位类（`bottom-10 left-1/2 -translate-x-1/2`）：底边距地图区底 40px、水平居中。
    const bottomInset = map.y + map.height - (fallback.y + fallback.height)
    const centerOffset = fallback.x + fallback.width / 2 - (map.x + map.width / 2)
    expect.soft(Math.abs(bottomInset - 40), `控制条兜底底边距地图区底 ${bottomInset}px，期望 40`).toBeLessThanOrEqual(0.5)
    expect.soft(Math.abs(centerOffset), `控制条兜底偏离地图区水平中线 ${centerOffset}px`).toBeLessThanOrEqual(0.5)
    expect.soft(launchers).toHaveLength(3)
    for (const item of launchers) {
      expect.soft(intersects(fallback, item.box), `控制条兜底与${item.name} ${JSON.stringify(item.box)} 相交`).toBe(false)
    }
    expect.soft(attribution.width, 'attribution 应有非零宽度').toBeGreaterThan(0)
    expect.soft(attribution.height, 'attribution 应有非零高度').toBeGreaterThan(0)
    expect.soft(intersects(fallback, attribution), `控制条兜底与 attribution ${JSON.stringify(attribution)} 相交`).toBe(false)
    await expectLaunchersHittable(page, OVERLAY_PANELS)
    const hit = await hitsItself(fallbackOf(page, 'control-bar'))
    expect.soft(hit.onSelf, `控制条兜底中心被 ${hit.top} 盖住`).toBe(true)

    // 控制条不在时展开面板的限高退到 attribution 带之上：面板仍完整在地图区内。
    await overlayPart(page, 'legend').launcher.tap()
    const panel = overlayPart(page, 'legend').panel
    await expect(panel).toBeVisible()
    const panelBox = await boxOf(panel, '图例面板')
    expect.soft(contains(map, panelBox), `控制条兜底时图例面板 ${JSON.stringify(panelBox)} 伸出地图区`).toBe(true)
  })

  test('同值无门控：开关没有效果，图例启动器正常、没有任何兜底', async ({ page }) => {
    await openMapWithCrash(page, { gate: false, region: 'legend' })

    // 自证前提：开关确实设上了、门控确实没有。
    expect(await readCrashSwitch(page)).toEqual({ gate: undefined, region: 'legend' })

    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    for (const name of OVERLAY_PANELS) await expect(overlayPart(page, name).launcher).toBeVisible()
    await expect(page.locator(ANY_REGION_FALLBACK)).toHaveCount(0)

    const launcher = overlayPart(page, 'legend').launcher
    await launcher.tap()
    await expect(overlayPart(page, 'legend').panel).toBeVisible()
    await expect(launcher).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator(ANY_REGION_FALLBACK)).toHaveCount(0)
    expect(await readCrashSwitch(page)).toEqual({ gate: undefined, region: 'legend' })
  })

  test('重试：开关仍在时再次进入兜底，清掉开关后重试则图例恢复', async ({ page }) => {
    await openMapWithCrash(page, { gate: true, region: 'legend' })
    await expectOnlyFallback(page, 'legend')
    const fallback = fallbackOf(page, 'legend')
    const retry = fallback.getByRole('button', { name: '重试' })

    // 开关仍为 legend：重试后探针再次抛错，仍是兜底。
    await retry.tap()
    await expectOnlyFallback(page, 'legend')
    await expect(overlayPart(page, 'legend').launcher).toHaveCount(0)

    await clearCrashSwitch(page)
    expect(await readCrashSwitch(page)).toEqual({ gate: true, region: undefined })
    // 只清开关不会触发重渲染：兜底仍在，直到用户点「重试」。
    await expect(fallback).toBeVisible()
    await retry.tap()

    await expect(page.locator(ANY_REGION_FALLBACK)).toHaveCount(0)
    const launcher = overlayPart(page, 'legend').launcher
    await expect(launcher).toBeVisible()
    await launcher.tap()
    await expect(overlayPart(page, 'legend').panel).toBeVisible()
  })

  test('operator + legend（列最高的组合）：兜底不压控制条，运维入口可点且在控制条之上', async ({ page }, testInfo) => {
    await openMapWithCrash(page, { gate: true, region: 'legend' })
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    await setRole(page, 'operator')
    const ops = page.locator(OPS_LINK)
    await expect(ops).toBeVisible()
    await expectOnlyFallback(page, 'legend')

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const fallback = await boxOf(fallbackOf(page, 'legend'), '图例兜底')
    const opsBox = await boxOf(ops, '运维入口')
    const launchers = await launcherBoxes(page, ['layers', 'basemap'])
    const margin = bar.y - (opsBox.y + opsBox.height)
    console.log(
      `region fallback operator+legend @ ${label(page, testInfo.project.name)}`,
      JSON.stringify({ map, bar, column, fallback, ops: opsBox, launchers, margin }),
    )

    expect.soft(contains(map, fallback), `图例兜底 ${JSON.stringify(fallback)} 不在地图区 ${JSON.stringify(map)} 内`).toBe(true)
    expect.soft(intersects(fallback, bar), `图例兜底与控制条 ${JSON.stringify(bar)} 相交`).toBe(false)
    expect.soft(intersects(fallback, opsBox), `图例兜底与运维入口 ${JSON.stringify(opsBox)} 相交`).toBe(false)
    for (const item of launchers) {
      expect.soft(intersects(fallback, item.box), `图例兜底与${item.name} ${JSON.stringify(item.box)} 相交`).toBe(false)
    }
    expect.soft(contains(map, opsBox), `运维入口 ${JSON.stringify(opsBox)} 不在地图区内`).toBe(true)
    await expectLaunchersHittable(page, ['layers', 'basemap'])
    const hit = await hitsItself(ops)
    expect.soft(hit.onSelf, `运维入口中心被 ${hit.top} 盖住`).toBe(true)
    expect.soft(fallback.height, '图例兜底高于一个启动器会吃掉列高余量').toBeLessThanOrEqual(MAX_IN_COLUMN_FALLBACK_HEIGHT)
    // 列高放得下：入口底边严格在控制条顶边之上（750×342 是咬合点；贴边 = 余量 0，不算放得下）。
    expect(margin, `运维入口底边 ${opsBox.y + opsBox.height} 距控制条顶边 ${bar.y} 的余量`).toBeGreaterThan(0)
  })
})
