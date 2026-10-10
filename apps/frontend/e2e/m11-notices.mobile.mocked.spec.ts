import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'

import { contains, intersects, type Box } from './support/legendLauncher.mocked'
import {
  BASEMAP_UNAVAILABLE_TEXT,
  LOADING_TEXT,
  NOTICE_TEST_IDS,
  STATION_EMPTY_TEXT,
  STATUS_TEST_IDS,
  expectNoFloatingNotice,
  hitsBelow,
  installNoticeMocks,
  lengthenText,
  twoLineMetrics,
} from './support/notices.mocked'
import { STATION_LAYER_URL } from './support/openStationWindow'
import {
  CONTROL_BAR,
  MAP_REGION,
  OVERLAY_PANELS,
  OVERLAY_PANEL_PARTS,
  boxOf,
  expectOnlyExpanded,
  overlayPart,
} from './support/overlayLaunchers.mocked'
import { isMobileForm, isShortLandscape, requireViewport } from './support/viewportForm'
import { expectMapControlsMounted } from './support/zoomControl.mocked'

/**
 * 浮动提示与状态条的移动位置（openspec mobile-responsive-display task 3.4，design.md D8 第一条）。
 * 三个移动 project 下都要通过、不按 project 跳过；只有截断用例里“底图文案是否自然超过两行”
 * 按是否矮视口横屏分支（横屏条带宽，43 字放得进两行，须先把文本加长）。
 */

const STATUS_CONTAINER = 'm11-map-status-overlays'
const MAP_SURFACE = 'm11-map-surface'
/** 两行上限的容差（亚像素取整）。 */
const TWO_LINE_TOLERANCE_PX = 1
/** 截断用例加长后的文本长度：844 宽条带里 12px / 14px 字号也远超两行。 */
const LONG_TEXT_CHARS = 400
const FIRST_SLOT_INSET_PX = 8

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

async function openMap(page: Page, url = '/') {
  await page.goto(url)
  await expectMapControlsMounted(page)
  await expect(page.locator(CONTROL_BAR)).toBeVisible()
  // 三个面板都收着：提示条带上没有用户主动打开的东西。
  await expectOnlyExpanded(page, null)
}

async function chromeBoxes(page: Page) {
  return {
    map: await boxOf(page.locator(MAP_REGION), '地图区'),
    bar: await boxOf(page.locator(CONTROL_BAR), '控制条'),
    layers: await boxOf(overlayPart(page, 'layers').launcher, '图层启动器'),
    basemap: await boxOf(overlayPart(page, 'basemap').launcher, '底图启动器'),
    legend: await boxOf(overlayPart(page, 'legend').launcher, '图例启动器'),
  }
}

/** 一条提示 / 状态条的几何：在地图区内、不与三个启动器和控制条相交、高不超过两行文本。 */
async function expectInBand(page: Page, locator: Locator, name: string, where: string) {
  await expect(locator, `${name}应可见`).toBeVisible()
  const chrome = await chromeBoxes(page)
  const box = await boxOf(locator, name)
  const metrics = await twoLineMetrics(locator)
  console.log(`notices ${name} @ ${where}`, JSON.stringify({ box, twoLineCap: metrics.cap, ...chrome }))

  expect.soft(contains(chrome.map, box), `${name} ${JSON.stringify(box)} 不在地图区 ${JSON.stringify(chrome.map)} 内`).toBe(true)
  for (const panel of OVERLAY_PANELS) {
    expect
      .soft(intersects(box, chrome[panel]), `${name} ${JSON.stringify(box)} 与${OVERLAY_PANEL_PARTS[panel].title}启动器 ${JSON.stringify(chrome[panel])} 相交`)
      .toBe(false)
  }
  expect.soft(intersects(box, chrome.bar), `${name} ${JSON.stringify(box)} 与控制条 ${JSON.stringify(chrome.bar)} 相交`).toBe(false)
  expect.soft(box.height, `${name}高 ${box.height} 超过两行上限 ${metrics.cap}`).toBeLessThanOrEqual(metrics.cap + TWO_LINE_TOLERANCE_PX)
  expect.soft(box.width).toBeGreaterThan(0)
  expect.soft(box.height).toBeGreaterThan(0)
  return { box, chrome, metrics }
}

/** 浮动提示占条带顶部第一槽：距地图区顶边与左边各 8px（状态条容器的顶边按这个槽位恒定预留）。 */
function expectInFirstSlot(geometry: { box: Box; chrome: { map: Box } }, name: string) {
  const top = geometry.box.y - geometry.chrome.map.y
  const left = geometry.box.x - geometry.chrome.map.x
  expect(Math.abs(top - FIRST_SLOT_INSET_PX), `${name}距地图区顶边 ${top}px，应为 ${FIRST_SLOT_INSET_PX}px`).toBeLessThanOrEqual(0.5)
  expect(Math.abs(left - FIRST_SLOT_INSET_PX), `${name}距地图区左边 ${left}px，应为 ${FIRST_SLOT_INSET_PX}px`).toBeLessThanOrEqual(0.5)
}

/** 场景 (4) 的现场：代站状态提示与地图源错误状态条同时出现。 */
async function openWithNoticeAndStatus(page: Page) {
  const mocks = await installNoticeMocks(page, { failBasemapTiles: true, emptyStations: true })
  await openMap(page, STATION_LAYER_URL)
  const notice = page.getByTestId(NOTICE_TEST_IDS.stationStatus)
  const status = page.getByTestId(STATUS_TEST_IDS.mapSourceError)
  await expect(notice).toHaveText(STATION_EMPTY_TEXT)
  // 先等瓦片真的被 503：之后状态条再红，日志能区分“瓦片没被请求”与“请求了但状态条没亮”。
  await expect.poll(() => mocks.failedBasemapTiles(), '底图瓦片应已被请求并回 503（计数为 0 = 地图还没发出底图瓦片请求）').toBeGreaterThan(0)
  await expect(status, `底图瓦片已 503 ${mocks.failedBasemapTiles()} 张，状态条应亮`).toHaveText(BASEMAP_UNAVAILABLE_TEXT)
  return { notice, status }
}

/** 从现在起记录某个 testid 的元素是否缺席过（只看终态会被“清掉后又被重新点亮”骗过）。 */
async function watchAbsence(page: Page, testId: string) {
  await page.evaluate((id) => {
    const selector = `[data-testid="${id}"]`
    const record = { absent: document.querySelector(selector) === null }
    ;(window as unknown as { __m11AbsenceRecord: typeof record }).__m11AbsenceRecord = record
    new MutationObserver((mutations) => {
      // 同一批变更里“删掉又加回”时终态查询看不出来，所以被删节点也要查。
      const removed = mutations.some((mutation) =>
        Array.from(mutation.removedNodes).some(
          (node) => node instanceof Element && (node.matches(selector) || node.querySelector(selector) !== null),
        ),
      )
      if (removed || document.querySelector(selector) === null) record.absent = true
    }).observe(document.body, { childList: true, subtree: true })
  }, testId)
  return () => page.evaluate(() => (window as unknown as { __m11AbsenceRecord: { absent: boolean } }).__m11AbsenceRecord.absent)
}

test.describe('M11 浮动提示与状态条移动位置', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  test('代站状态提示在地图区内，不压启动器与控制条，不超过两行', async ({ page }, testInfo) => {
    await installNoticeMocks(page, { emptyStations: true })
    await openMap(page, STATION_LAYER_URL)

    const notice = page.getByTestId(NOTICE_TEST_IDS.stationStatus)
    await expect(notice).toHaveText(STATION_EMPTY_TEXT)
    await expect(notice).toHaveAttribute('role', 'status')
    expectInFirstSlot(await expectInBand(page, notice, '代站状态提示', label(page, testInfo)), '代站状态提示')
    // 提示单独出现：底图瓦片是合法 PNG，地图源错误状态条不该亮。
    await expect(page.getByTestId(STATUS_TEST_IDS.mapSourceError)).toHaveCount(0)
  })

  test('加载中提示在地图区内，不压启动器与控制条，不超过两行', async ({ page }, testInfo) => {
    const mocks = await installNoticeMocks(page, { holdBootstrap: true })
    try {
      await openMap(page)

      const notice = page.getByTestId(NOTICE_TEST_IDS.loading)
      await expect(notice).toHaveText(LOADING_TEXT)
      expectInFirstSlot(await expectInBand(page, notice, '加载中提示', label(page, testInfo)), '加载中提示')
      await expect(page.getByTestId(STATUS_TEST_IDS.mapSourceError)).toHaveCount(0)
    } finally {
      mocks.releaseBootstrap()
    }
    // 放行之后提示消失：它确实是被挂起的那个请求撑着的。
    await expect(page.getByTestId(NOTICE_TEST_IDS.loading)).toHaveCount(0)
  })

  test('地图源错误状态条单独出现时在地图区内，不压启动器与控制条，不超过两行，下方不吞地图手势', async ({ page }, testInfo) => {
    const mocks = await installNoticeMocks(page, { failBasemapTiles: true })
    await openMap(page)

    const status = page.getByTestId(STATUS_TEST_IDS.mapSourceError)
    await expect(status).toHaveText(BASEMAP_UNAVAILABLE_TEXT)
    await expect(status).toHaveAttribute('role', 'status')
    expect(mocks.failedBasemapTiles(), '状态条应由真实的底图瓦片 503 点亮').toBeGreaterThan(0)
    // 单独出现：总览已落定，五条浮动提示都不在，流域边界不可用也不在。
    await expectNoFloatingNotice(page)
    await expect(page.getByTestId(STATUS_TEST_IDS.basinLayerUnavailable)).toHaveCount(0)
    await expect(status).toBeVisible()

    const { box, chrome } = await expectInBand(page, status, '地图源错误状态条', label(page, testInfo))

    // 四条状态条共用的容器不拦指针；状态条下方、容器范围内的点仍命中地图画布。
    const container = page.getByTestId(STATUS_CONTAINER)
    await expect(container).toHaveCount(1)
    await expect(container.getByTestId(STATUS_TEST_IDS.mapSourceError)).toHaveCount(1)
    await expect(container).toHaveCSS('pointer-events', 'none')
    const containerBox = await boxOf(container, '状态条容器')
    expect(contains(chrome.map, containerBox), `状态条容器 ${JSON.stringify(containerBox)} 不在地图区内`).toBe(true)
    expect(intersects(containerBox, chrome.bar), `状态条容器 ${JSON.stringify(containerBox)} 与控制条相交`).toBe(false)
    const role = await boxOf(page.getByLabel('Role'), '角色切换器')
    const hits = await hitsBelow(page, containerBox, box, [role])
    console.log(
      `notices status container @ ${label(page, testInfo)}`,
      JSON.stringify({ container: containerBox, role, checkedPoints: hits.checked }),
    )
    expect(hits.checked, '状态条下方应有可检查的点，否则命中断言是空的').toBeGreaterThan(0)
    expect(hits.misses, '状态条下方有点没命中地图画布').toEqual([])
  })

  test('浮动提示与地图源错误状态条同时出现时互不相交，各自仍在条带内', async ({ page }, testInfo) => {
    const { notice, status } = await openWithNoticeAndStatus(page)
    const where = label(page, testInfo)

    const noticeGeometry = await expectInBand(page, notice, '代站状态提示', where)
    const statusGeometry = await expectInBand(page, status, '地图源错误状态条', where)
    expect(
      intersects(noticeGeometry.box, statusGeometry.box),
      `代站状态提示 ${JSON.stringify(noticeGeometry.box)} 与地图源错误状态条 ${JSON.stringify(statusGeometry.box)} 相交`,
    ).toBe(false)
    // 自上而下：提示在第一槽，状态条在它之下。
    expect(noticeGeometry.box.y + noticeGeometry.box.height).toBeLessThanOrEqual(statusGeometry.box.y)
  })

  test('角色切换器不与同时出现的浮动提示和状态条相交', async ({ page }, testInfo) => {
    const { notice, status } = await openWithNoticeAndStatus(page)

    const role = page.getByLabel('Role')
    await expect(role).toBeVisible()
    const boxes: Record<'role' | 'notice' | 'status', Box> = {
      role: await boxOf(role, '角色切换器'),
      notice: await boxOf(notice, '代站状态提示'),
      status: await boxOf(status, '地图源错误状态条'),
    }
    console.log(
      `notices role selector @ ${label(page, testInfo)}`,
      JSON.stringify({ ...boxes, gapStatusToRole: boxes.role.y - (boxes.status.y + boxes.status.height) }),
    )
    expect(intersects(boxes.role, boxes.notice), `角色切换器 ${JSON.stringify(boxes.role)} 与代站状态提示 ${JSON.stringify(boxes.notice)} 相交`).toBe(false)
    expect(intersects(boxes.role, boxes.status), `角色切换器 ${JSON.stringify(boxes.role)} 与地图源错误状态条 ${JSON.stringify(boxes.status)} 相交`).toBe(false)
  })

  test('文本超过两行时截断：提示与状态条的高仍不超过两行上限，仍在条带内，互不相交', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const where = label(page, testInfo)
    const { notice, status } = await openWithNoticeAndStatus(page)

    // 状态条：竖屏条带里固定的底图文案（43 字、14px）自然超过两行；矮视口横屏的条带放得下，先加长。
    const statusLengthened = isShortLandscape(viewport)
    if (statusLengthened) await lengthenText(status, LONG_TEXT_CHARS)
    // 提示：代站文案只有 9 个字，三个 project 下都要加长。
    await lengthenText(notice, LONG_TEXT_CHARS)

    const statusMetrics = await twoLineMetrics(status)
    const noticeMetrics = await twoLineMetrics(notice)
    console.log(`notices truncation @ ${where}`, JSON.stringify({ statusLengthened, statusMetrics, noticeMetrics }))
    // 前置：内容确实比可视高度高，否则下面的两行上限恒真。
    expect(statusMetrics.clipped, `状态条内容 ${statusMetrics.scrollHeight} 未超过可视高度 ${statusMetrics.clientHeight}`).toBe(true)
    expect(noticeMetrics.clipped, `提示内容 ${noticeMetrics.scrollHeight} 未超过可视高度 ${noticeMetrics.clientHeight}`).toBe(true)

    const noticeGeometry = await expectInBand(page, notice, '加长后的代站状态提示', where)
    const statusGeometry = await expectInBand(page, status, statusLengthened ? '加长后的地图源错误状态条' : '地图源错误状态条', where)
    expect(intersects(noticeGeometry.box, statusGeometry.box), '截断后的提示与状态条相交').toBe(false)
  })

  test('底图不可用状态条在总览数据晚到（图层目录到达、有效时刻校正）之后从未被清掉', async ({ page }, testInfo) => {
    const where = label(page, testInfo)
    const mocks = await installNoticeMocks(page, { failBasemapTiles: true, holdBootstrap: true })
    const status = page.getByTestId(STATUS_TEST_IDS.mapSourceError)
    let n0 = 0
    let wasAbsent: () => Promise<boolean>
    try {
      await openMap(page)
      // 流域清单挂着：图层目录不落，底图瓦片先 503 并点亮状态条。
      await expect.poll(() => mocks.failedBasemapTiles(), '底图瓦片应已被请求并回 503').toBeGreaterThan(0)
      await expect(status, `放行前：底图瓦片已 503 ${mocks.failedBasemapTiles()} 张，状态条应亮`).toHaveText(BASEMAP_UNAVAILABLE_TEXT)
      await expect(page.getByTestId(MAP_SURFACE), '放行前不应已注册径流叠加层').not.toHaveAttribute('data-registered-overlays')
      expect(page.url(), '放行前 URL 不应已带 validTime').not.toContain('validTime=')
      n0 = mocks.failedBasemapTiles()
      wasAbsent = await watchAbsence(page, STATUS_TEST_IDS.mapSourceError)
    } finally {
      mocks.releaseBootstrap()
    }

    // 放行已生效的两个独立信号：径流叠加层已注册（overlay.sourceId 变了），URL 带上 validTime（最后一次 key 变化）。
    await expect(page.getByTestId(MAP_SURFACE)).toHaveAttribute('data-registered-overlays', 'discharge')
    await expect(page).toHaveURL(/[?&]validTime=/)
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))

    const n1 = mocks.failedBasemapTiles()
    const absent = await wasAbsent()
    const present = (await status.count()) > 0
    const observed = JSON.stringify({ absent, present, n0, n1 })
    console.log(`notices basemap notice across bootstrap release @ ${where}`, observed)
    expect(absent, `放行之后状态条缺席过 ${observed}`).toBe(false)
    await expect(status, `放行之后状态条终态应仍是底图不可用 ${observed}`).toHaveText(BASEMAP_UNAVAILABLE_TEXT)
  })
})
