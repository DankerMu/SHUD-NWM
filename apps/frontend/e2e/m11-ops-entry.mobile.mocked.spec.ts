import { expect, test, type Locator, type Page } from '@playwright/test'

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
  expectOnlyExpanded,
  overlayPart,
} from './support/overlayLaunchers.mocked'
import { ROLE_LABELS, setRole } from './support/setRole'
import { isMobileForm, isShortLandscape, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 运维入口并入启动器列（openspec mobile-responsive-display task 3.5，design.md D8）。
 * 三个移动 project 下都要通过、不按 project 跳过；只有启动器列的顶距 / 间距按是否矮视口横屏分支。
 */

const MIN_TOUCH_TARGET = 44
/** 入口与列的宽度上限：正好 44px，容许亚像素误差。宽于它会撑宽列、推左面板锚点并侵入提示条带。 */
const MAX_COLUMN_WIDTH = 44.5
const TOLERANCE = 0.5
/** 启动器列的顶距（列顶 − 地图区顶）与相邻项间距（px）。 */
const COLUMN_GEOMETRY = { portrait: { topInset: 8, gap: 4 }, shortLandscape: { topInset: 4, gap: 2 } } as const
const OPS_HEADING = '内部诊断'
const FORBIDDEN = '权限不足'

async function openMap(page: Page) {
  await page.goto('/')
  await expectMapControlsMounted(page)
  await expect(page.locator(CONTROL_BAR)).toBeVisible()
}

async function openMapAsOperator(page: Page) {
  await openMap(page)
  await setRole(page, 'operator')
  await expect(page.locator(OPS_LINK)).toBeVisible()
}

/** 元素中心的命中测试是否落在它自己身上（没被别的浮层盖住）。 */
async function hitsItself(locator: Locator) {
  return locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return {
      onSelf: top !== null && element.contains(top),
      top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
    }
  })
}

/** 列内四项自上而下的包围盒：图层 / 底图 / 图例三个启动器 + 运维入口。 */
async function columnItemBoxes(page: Page): Promise<Array<{ name: string; box: Box }>> {
  const items: Array<{ name: string; box: Box }> = []
  for (const name of OVERLAY_PANELS) {
    const title = `${OVERLAY_PANEL_PARTS[name].title}启动器`
    items.push({ name: title, box: await boxOf(overlayPart(page, name).launcher, title) })
  }
  items.push({ name: '运维入口', box: await boxOf(page.locator(OPS_LINK), '运维入口') })
  return items
}

async function scrollMetrics(scroller: Locator) {
  return scroller.evaluate((element) => ({ scrollHeight: element.scrollHeight, clientHeight: element.clientHeight }))
}

test.describe('M11 运维入口移动形态', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    await mockOpsEntryApi(page)
  })

  test('viewer 角色下没有运维入口', async ({ page }) => {
    await openMap(page)

    // 正对照：启动器列与三个启动器都已渲染，入口缺席不是因为列还没出来。
    await expect(page.locator(LAUNCHER_COLUMN)).toBeVisible()
    for (const name of OVERLAY_PANELS) await expect(overlayPart(page, name).launcher).toBeVisible()
    await expect(page.getByLabel('Role')).toHaveText(ROLE_LABELS.viewer)
    await expect(page.locator(OPS_LINK)).toHaveCount(0)
    await expect(page.getByRole('link', { name: '运维', exact: true })).toHaveCount(0)
  })

  test('operator 角色下入口是启动器列的末项：44×44、在图例启动器之下、不被遮挡、可访问名为「运维」', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await openMap(page)
    await expect(page.locator(OPS_LINK)).toHaveCount(0)
    await setRole(page, 'operator')

    const ops = page.locator(OPS_LINK)
    await expect(ops).toHaveCount(1)
    await expect(ops).toBeVisible()
    await expect(page.locator(LAUNCHER_COLUMN).locator(OPS_LINK), '运维入口应在启动器列里').toHaveCount(1)
    await expect(ops).toHaveAttribute('href', '/ops')
    const named = page.getByRole('link', { name: '运维', exact: true })
    await expect(named, '可访问名为「运维」的链接应恰一个').toHaveCount(1)
    await expect(named).toHaveAttribute('data-testid', 'm11-ops-link')

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const items = await columnItemBoxes(page)
    const opsBox = items[3].box
    const legendBox = items[2].box
    console.log(
      `ops entry @ ${label}`,
      JSON.stringify({ map, barTop: bar.y, column, layers: items[0].box, basemap: items[1].box, legend: legendBox, ops: opsBox }),
    )

    expect.soft(opsBox.width, '运维入口宽').toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(opsBox.height, '运维入口高').toBeGreaterThanOrEqual(MIN_TOUCH_TARGET)
    expect.soft(opsBox.width, '运维入口宽于 44px 会撑宽启动器列').toBeLessThanOrEqual(MAX_COLUMN_WIDTH)
    expect.soft(column.width, '启动器列被撑宽').toBeLessThanOrEqual(MAX_COLUMN_WIDTH)
    expect.soft(contains(column, opsBox), `运维入口 ${JSON.stringify(opsBox)} 不在启动器列 ${JSON.stringify(column)} 的包围盒内`).toBe(true)
    expect.soft(opsBox.y, '运维入口顶边应不高于图例启动器底边').toBeGreaterThanOrEqual(legendBox.y + legendBox.height)
    // 视觉上的最后一项：比另外三项都靠下，且它的底边就是列的底边。
    for (const item of items.slice(0, 3)) {
      expect.soft(opsBox.y, `运维入口应在${item.name}之下`).toBeGreaterThanOrEqual(item.box.y + item.box.height)
    }
    expect.soft(Math.abs(column.y + column.height - (opsBox.y + opsBox.height)), '运维入口底边应是启动器列底边').toBeLessThanOrEqual(TOLERANCE)

    // 四项两两不相交、都在地图区内、都没被盖住。
    for (const [index, item] of items.entries()) {
      expect.soft(contains(map, item.box), `${item.name} ${JSON.stringify(item.box)} 不在地图区内`).toBe(true)
      for (const other of items.slice(index + 1)) {
        expect.soft(intersects(item.box, other.box), `${item.name}与${other.name}相交`).toBe(false)
      }
    }
    for (const name of OVERLAY_PANELS) {
      const hit = await hitsItself(overlayPart(page, name).launcher)
      expect.soft(hit.onSelf, `${OVERLAY_PANEL_PARTS[name].title}启动器中心被 ${hit.top} 盖住`).toBe(true)
    }
    const opsHit = await hitsItself(ops)
    expect.soft(opsHit.onSelf, `运维入口中心被 ${opsHit.top} 盖住`).toBe(true)

    // 列高放得下：入口底边不越过控制条顶边（750×342 是咬合点）。
    expect(opsBox.y + opsBox.height, `运维入口底边越过控制条顶边 ${bar.y}`).toBeLessThanOrEqual(bar.y)
  })

  test('启动器列的顶距与间距：竖屏 8 / 4，矮视口横屏 4 / 2', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const expected = isShortLandscape(viewport) ? COLUMN_GEOMETRY.shortLandscape : COLUMN_GEOMETRY.portrait
    await openMapAsOperator(page)

    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const items = await columnItemBoxes(page)
    const topInset = column.y - map.y
    const gaps = items.slice(1).map((item, index) => item.box.y - (items[index].box.y + items[index].box.height))
    console.log(`ops entry column geometry @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`, JSON.stringify({ topInset, gaps }))

    expect.soft(Math.abs(topInset - expected.topInset), `列顶距 ${topInset}，期望 ${expected.topInset}`).toBeLessThanOrEqual(TOLERANCE)
    expect(gaps).toHaveLength(3)
    for (const [index, gap] of gaps.entries()) {
      expect
        .soft(Math.abs(gap - expected.gap), `${items[index].name}与${items[index + 1].name}的间距 ${gap}，期望 ${expected.gap}`)
        .toBeLessThanOrEqual(TOLERANCE)
    }
  })

  test('operator 角色下展开图层面板：面板在地图区内、在控制条之上，不与运维入口相交', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await openMapAsOperator(page)
    const ops = page.locator(OPS_LINK)

    await overlayPart(page, 'layers').launcher.tap()
    await expectOnlyExpanded(page, 'layers')
    const map = await boxOf(page.locator(MAP_REGION), '地图区')
    const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
    const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
    const panel = await boxOf(overlayPart(page, 'layers').panel, '图层面板')
    const opsBox = await boxOf(ops, '运维入口')
    console.log(
      `ops entry layer panel @ ${label}`,
      JSON.stringify({ panel, ops: opsBox, column, scroll: await scrollMetrics(overlayPart(page, 'layers').scroller) }),
    )
    expect.soft(panel.width).toBeGreaterThan(0)
    expect.soft(panel.height).toBeGreaterThan(0)
    expect.soft(contains(map, panel), `图层面板 ${JSON.stringify(panel)} 不在地图区 ${JSON.stringify(map)} 内`).toBe(true)
    expect.soft(panel.y + panel.height, `图层面板底边越过控制条顶边 ${bar.y}`).toBeLessThanOrEqual(bar.y)
    expect.soft(intersects(panel, opsBox), `图层面板与运维入口 ${JSON.stringify(opsBox)} 相交`).toBe(false)
    // 面板锚在列左缘：列没被撑宽，面板展开后入口仍只有 44px 宽、仍可命中。
    expect.soft(column.width, '面板展开后启动器列被撑宽').toBeLessThanOrEqual(MAX_COLUMN_WIDTH)
    const hit = await hitsItself(ops)
    expect.soft(hit.onSelf, `面板展开后运维入口中心被 ${hit.top} 盖住`).toBe(true)

    // 报告用的量测：图例面板的滚动数字（不作断言，图例面板的几何归 3.2 的 spec）。
    await overlayPart(page, 'legend').launcher.tap()
    await expectOnlyExpanded(page, 'legend')
    console.log(
      `ops entry legend panel @ ${label}`,
      JSON.stringify({
        panel: await boxOf(overlayPart(page, 'legend').panel, '图例面板'),
        scroll: await scrollMetrics(overlayPart(page, 'legend').scroller),
      }),
    )
  })

  test('触屏点入口进入 /ops：页面已授权、角色保持 operator', async ({ page }) => {
    // 前置：这套 mock 下 /ops 对 viewer 是关着的，否则后面的“已授权”断言对谁都成立。
    await page.goto('/ops')
    await expect(page.getByText(FORBIDDEN)).toBeVisible()
    await expect(page.getByRole('heading', { name: OPS_HEADING })).toHaveCount(0)

    // `goto` 会把角色复位成 viewer（auth store 不持久化），所以回到 / 后再切角色。
    await openMapAsOperator(page)
    await page.locator(OPS_LINK).tap()

    await expect.poll(() => new URL(page.url()).pathname).toBe('/ops')
    await expect(page.getByRole('heading', { name: OPS_HEADING })).toBeVisible()
    await expect(page.getByText(FORBIDDEN)).toHaveCount(0)
    // 客户端导航、没有整页重载：角色仍是 operator。
    await expect(page.getByLabel('Role')).toHaveText(ROLE_LABELS.operator)
    await expect(page.locator(LAUNCHER_COLUMN)).toHaveCount(0)
  })
})
