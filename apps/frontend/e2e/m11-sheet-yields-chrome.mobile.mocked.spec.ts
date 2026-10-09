import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { controlBarParts } from './support/controlBar.mocked'
import { CURVE_WINDOWS, CURVE_WINDOW_KINDS, curveWindowParts, type CurveWindowKind } from './support/curveSheet.mocked'
import { contains, intersects, type Box } from './support/legendLauncher.mocked'
import { gotoWithRiverHooks, locateRiverPoint, openRiverWindow, tapLocatedPoint } from './support/openRiverWindow'
import { gotoWithStationLayer, locateStationPoint } from './support/openStationWindow'
import {
  CONTROL_BAR,
  LAUNCHER_COLUMN,
  MAP_REGION,
  OVERLAY_PANELS,
  OVERLAY_PANEL_PARTS,
  boxOf,
  expectOnlyExpanded,
  overlayPart,
} from './support/overlayLaunchers.mocked'
import {
  REGION_FALLBACK_TEST_IDS,
  clearCrashSwitch,
  hitsItself,
  installCrashSwitch,
  readCrashSwitch,
} from './support/regionFallbacks.mocked'
import { MOCK_VALID_TIMES, installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 抽屉打开时地图外壳让位（openspec mobile-responsive-display task 4.6，design.md D11）。
 *
 * 用例 (a)–(g) 对应 tasks.md 里 #2807 的 Triage。每条用例都在用例内 `setViewportSize`，所以三个移动
 * project 下执行的是同一组断言，不按 project 跳过。开窗一律是定位钩子给出的点上的一次真实轻触；
 * 让位之后控制条与启动器列是 `visibility: hidden`——仍在 DOM、仍有包围盒，但不可见、不接收指针输入。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const LANDSCAPE_WIDE: ViewportSize = { width: 844, height: 390 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }

/** (c)(d) 用的播放速度：非默认值，关闭抽屉后据它证明控制条的内部状态因保持挂载而保留。 */
const PLAYBACK_SPEED = 2
const PLAYBACK_PERIOD_MS = 1_000 / PLAYBACK_SPEED
/** “有效时刻不再变化”的观察窗：不少于 3 个播放周期（这里取 4 个）。 */
const QUIET_WINDOW_MS = 4 * PLAYBACK_PERIOD_MS
const LAST_VALID_TIME_MS = Date.parse(MOCK_VALID_TIMES[MOCK_VALID_TIMES.length - 1])
const CURVE_FALLBACK = REGION_FALLBACK_TEST_IDS.curve

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

/** 设视口、装基线 mock、开门控并导航到地图就绪；不开窗。气象代站一开始就带 `metStations=1`。 */
async function loadMap(page: Page, viewport: ViewportSize, kind: CurveWindowKind, options: { crashCurve?: boolean } = {}) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  // 崩溃开关先于页面脚本设好：曲线探针只在有曲线面板渲染时挂载，所以此刻还不产生兜底。
  if (options.crashCurve) await installCrashSwitch(page, { gate: true, region: 'curve' })
  if (kind === 'river') await gotoWithRiverHooks(page)
  else await gotoWithStationLayer(page)
  await expect(page.locator(CONTROL_BAR)).toBeVisible()
  await expect(page.locator(LAUNCHER_COLUMN)).toBeVisible()
}

function locatePoint(page: Page, kind: CurveWindowKind) {
  return kind === 'river' ? locateRiverPoint(page) : locateStationPoint(page)
}

/** 在已定位的点上真实轻触，并等到该种曲线窗可见。 */
async function tapOpen(page: Page, kind: CurveWindowKind, point: { x: number; y: number }) {
  expect(await tapLocatedPoint(page, point), '移动 project 是触屏上下文').toBe('tap')
  await expect(curveWindowParts(page, kind).frame, `${CURVE_WINDOWS[kind].name}应在轻触后可见`).toBeVisible()
}

async function closeSheet(page: Page, kind: CurveWindowKind) {
  const parts = curveWindowParts(page, kind)
  await parts.close.tap()
  await expect(parts.frame).toHaveCount(0)
}

/** 让位：控制条与启动器列不可见，但仍在 DOM（保持挂载）。 */
async function expectYielded(page: Page, where: string) {
  for (const [name, selector] of [['控制条', CONTROL_BAR], ['启动器列', LAUNCHER_COLUMN]] as const) {
    await expect(page.locator(selector), `${name}应保持挂载 @ ${where}`).toHaveCount(1)
    await expect(page.locator(selector), `${name}应不可见 @ ${where}`).toBeHidden()
  }
}

/** 恢复：控制条与启动器列可见，控制条中心的命中测试落在控制条内。 */
async function expectRestored(page: Page, where: string) {
  await expect(page.locator(CONTROL_BAR), `控制条应可见 @ ${where}`).toBeVisible()
  await expect(page.locator(LAUNCHER_COLUMN), `启动器列应可见 @ ${where}`).toBeVisible()
  const hit = await hitsItself(page.locator(CONTROL_BAR))
  expect(hit.onSelf, `控制条中心的命中测试应落在控制条内，实际 ${hit.top} @ ${where}`).toBe(true)
}

type NamedPoint = { name: string; x: number; y: number }

function centerPoint(name: string, box: Box): NamedPoint {
  return { name, x: box.x + box.width / 2, y: box.y + box.height / 2 }
}

/** 开窗前量出的外壳探测点：控制条中心、预报源分段中心（控制条左半）、每个启动器的中心。 */
async function chromePoints(page: Page): Promise<NamedPoint[]> {
  const points = [
    centerPoint('控制条中心', await boxOf(page.locator(CONTROL_BAR), '控制条')),
    centerPoint('预报源分段中心', await boxOf(controlBarParts(page).sourceGroup, '预报源分段')),
  ]
  for (const name of OVERLAY_PANELS) {
    const title = `${OVERLAY_PANEL_PARTS[name].title}启动器`
    points.push(centerPoint(`${title}中心`, await boxOf(overlayPart(page, name).launcher, title)))
  }
  return points
}

/** 该点的命中测试是否落在控制条或启动器列之内。 */
async function hitsChrome(page: Page, point: { x: number; y: number }) {
  return page.evaluate(
    ({ x, y, selectors }) => {
      const top = document.elementFromPoint(x, y)
      const chrome = selectors.map((selector) => document.querySelector(selector))
      return {
        inChrome: top !== null && chrome.some((element) => element !== null && element.contains(top)),
        top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
      }
    },
    { x: point.x, y: point.y, selectors: [CONTROL_BAR, LAUNCHER_COLUMN] },
  )
}

interface ChromeState {
  /** 预报源分段里 `aria-pressed="true"` 的按钮文案。 */
  source: string | null
  cycle: string | null
  /** URL 的 `validTime`（未写入为 null）与时间轴滑块的值——两者同为“有效时刻”的页面可观测量。 */
  validTime: string | null
  sliderValue: string | null
  speed: string | null
  /** 播放按钮的 `aria-label`：「播放时间轴」= 未播放。 */
  playLabel: string | null
}

/** 直接读 DOM：让位时控制条是 `visibility: hidden`，不走要求可见的定位器。 */
async function readChromeState(page: Page): Promise<ChromeState> {
  return page.evaluate((barSelector) => {
    const bar = document.querySelector(barSelector)
    const valueOf = (selector: string) => bar?.querySelector<HTMLInputElement | HTMLSelectElement>(selector)?.value ?? null
    const play = bar?.querySelector('button[aria-label="播放时间轴"], button[aria-label="暂停时间轴"]')
    return {
      source: bar?.querySelector('[role="group"][aria-label="预报源"] button[aria-pressed="true"]')?.textContent ?? null,
      cycle: valueOf('select[aria-label="起报时次"]'),
      validTime: new URLSearchParams(window.location.search).get('validTime'),
      sliderValue: valueOf('input[aria-label="有效时间滑块"]'),
      // 竖屏时速度选择器在控制条第一行，其余形态在时间轴里；都在控制条根节点之内。
      speed: valueOf('select[aria-label="播放速度"]'),
      playLabel: play?.getAttribute('aria-label') ?? null,
    }
  }, CONTROL_BAR)
}

test.describe('M11 抽屉打开时地图外壳让位', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const viewport of [PORTRAIT, LANDSCAPE]) {
    for (const kind of CURVE_WINDOW_KINDS) {
      const name = CURVE_WINDOWS[kind].name

      test(`(a) ${viewport.width}x${viewport.height} ${name}：开窗后控制条与启动器列不可见、仍在 DOM，其上各点的命中测试都不落在它们之内`, async ({ page }, testInfo) => {
        await loadMap(page, viewport, kind)
        const where = label(page, testInfo)
        const point = await locatePoint(page, kind)

        // 开窗前：每个探测点确实在外壳上（否则开窗后“不落在外壳内”是空话）。
        const points = await chromePoints(page)
        for (const probe of points) {
          const hit = await hitsChrome(page, probe)
          expect(hit.inChrome, `前提：开窗前${probe.name} ${JSON.stringify(probe)} 应命中外壳，实际 ${hit.top} @ ${where}`).toBe(true)
        }

        await tapOpen(page, kind, point)

        // 先做命中测试（soft），再断言不可见：两类判据各自报告，互不遮蔽。
        const sheet = await boxOf(curveWindowParts(page, kind).frame, name)
        const uncovered: string[] = []
        for (const probe of points) {
          const hit = await hitsChrome(page, probe)
          // soft：逐点报告，一处仍可点不掩盖另一处。
          expect.soft(hit.inChrome, `${probe.name} ${JSON.stringify(probe)} 的命中测试不应落在外壳内，实际 ${hit.top} @ ${where}`).toBe(false)
          if (!contains(sheet, { x: probe.x, y: probe.y, width: 0, height: 0 }, 0)) uncovered.push(probe.name)
        }
        console.log(`sheet-yields points ${kind} @ ${where}`, JSON.stringify({ sheet, points, uncovered }))
        await expectYielded(page, where)
        // 至少一个探测点不在抽屉之下：那里只有 `visibility` 能挡住指针，抽屉的遮挡帮不上忙。
        // 竖屏是三个启动器（底部抽屉够不着列），矮视口横屏是控制条左半的预报源分段。
        expect(uncovered, `应有不被抽屉盖住的探测点 @ ${where}`).toContain(
          isShortLandscape(requireViewport(page)) ? '预报源分段中心' : '图例启动器中心',
        )
      })
    }
  }

  test('(b) 390x664：图例面板展开时轻触河段——河段窗可见、三个面板都不可见，关闭后仍都不可见', async ({ page }, testInfo) => {
    await loadMap(page, PORTRAIT, 'river')
    const where = label(page, testInfo)

    await overlayPart(page, 'legend').launcher.tap()
    await expectOnlyExpanded(page, 'legend')
    // 面板展开之后才定位：钩子自己校验定位点没被别的元素盖住（盖住即 HOOK_POINT_OCCLUDED）。
    const point = await locatePoint(page, 'river')
    await expectOnlyExpanded(page, 'legend')

    await tapOpen(page, 'river', point)
    // 先断言面板（地图点击本身就会收起它，这一条改动前就成立），再断言让位。
    await expectOnlyExpanded(page, null)
    await expectYielded(page, where)

    await closeSheet(page, 'river')
    await expectRestored(page, where)
    await expectOnlyExpanded(page, null)
  })

  test('(c)(d) 390x664：播放中轻触河段——有效时刻不再变化；关闭后外壳恢复、有效时刻不变、不续播、2x 速度保持', async ({ page }, testInfo) => {
    await loadMap(page, PORTRAIT, 'river')
    const where = label(page, testInfo)
    const bar = controlBarParts(page)
    // 顺序钉死：mock 只有 9 个有效时刻，播到末位会自行停止。2x -> 播放 -> 确认前进一次 -> 定位 -> 立刻轻触。
    await bar.speed.selectOption(String(PLAYBACK_SPEED))
    const beforePlay = await readChromeState(page)
    expect(beforePlay.playLabel).toBe('播放时间轴')
    await bar.bar.getByRole('button', { name: '播放时间轴' }).tap()
    await expect
      .poll(async () => (await readChromeState(page)).sliderValue, { message: '播放后有效时刻应至少前进一次', intervals: [50] })
      .not.toBe(beforePlay.sliderValue)

    // 定位紧挨着轻触：每前进一步径流瓦片源都会换（source 身份含有效时刻），定位钩子等地图空闲、
    // 确认河段已画在该点才给出坐标——早先定位的点上此刻可能还没有河段。
    const point = await locatePoint(page, 'river')
    // 硬前提：轻触前仍在播放。
    expect((await readChromeState(page)).playLabel, `硬前提：轻触前应在播放 @ ${where}`).toBe('暂停时间轴')
    await tapOpen(page, 'river', point)

    // (c) 打开即暂停：抽屉可见后读到的有效时刻不是末位，再过不少于 3 个播放周期它也不变。
    await expectYielded(page, where)
    const opened = await readChromeState(page)
    console.log(`sheet-yields playback @ ${where}`, JSON.stringify({ beforePlay, opened }))
    expect(opened.validTime, '播放前进后 URL 应带 validTime').not.toBeNull()
    expect(Date.parse(opened.validTime!), `硬前提：抽屉打开时有效时刻不应已到末位 @ ${where}`).toBeLessThan(LAST_VALID_TIME_MS)
    expect(opened.speed).toBe(String(PLAYBACK_SPEED))
    expect(opened.playLabel, '抽屉打开后时间轴应已暂停').toBe('播放时间轴')
    await page.waitForTimeout(QUIET_WINDOW_MS)
    expect(await readChromeState(page), `抽屉开着的 ${QUIET_WINDOW_MS}ms 里有效时刻不应再变 @ ${where}`).toEqual(opened)

    // (d) 关闭：外壳恢复，预报源 / 起报时次 / 有效时刻 / 速度都等于开窗时的值，且没有续播。
    await closeSheet(page, 'river')
    await expectRestored(page, where)
    expect(await readChromeState(page), `关闭后控制条状态应等于开窗时 @ ${where}`).toEqual(opened)
    await expect(bar.bar.getByRole('button', { name: '播放时间轴' })).toBeVisible()
    // 硬前提：还没到末位——否则“未播放”只是播完自停的假象。
    await expect(bar.bar.getByRole('button', { name: '下一个有效时刻' }), '硬前提：「下一个有效时刻」未禁用').toBeEnabled()
    await page.waitForTimeout(QUIET_WINDOW_MS)
    expect(await readChromeState(page), `关闭后的 ${QUIET_WINDOW_MS}ms 里不应续播 @ ${where}`).toEqual(opened)
    await expect(bar.speed).toHaveValue(String(PLAYBACK_SPEED))
    await expectOnlyExpanded(page, null)

    // 恢复后面板限高的测量路径仍然成立：展开的面板止于控制条顶边之上。
    await overlayPart(page, 'legend').launcher.tap()
    await expectOnlyExpanded(page, 'legend')
    const panel = await boxOf(overlayPart(page, 'legend').panel, '图例面板')
    const barBox = await boxOf(page.locator(CONTROL_BAR), '控制条')
    expect(panel.y + panel.height, `恢复后图例面板底边应不越过控制条顶边 ${barBox.y} @ ${where}`).toBeLessThanOrEqual(barBox.y)
  })

  test('(d) 750x342：开窗隐藏，关闭后外壳恢复、有效时刻不变、未播放', async ({ page }, testInfo) => {
    await loadMap(page, LANDSCAPE, 'river')
    const where = label(page, testInfo)
    const bar = controlBarParts(page)

    // 先前进一步：有效时刻落到非默认值并写进 URL，之后“不变”才有内容。
    // 步进在定位之前：换有效时刻会换瓦片，定位钩子等地图空闲后才给点，轻触时河段已画好。
    const initial = await readChromeState(page)
    await bar.bar.getByRole('button', { name: '下一个有效时刻' }).tap()
    await expect.poll(async () => (await readChromeState(page)).sliderValue).not.toBe(initial.sliderValue)
    const before = await readChromeState(page)
    expect(before.validTime).not.toBeNull()
    const point = await locatePoint(page, 'river')

    await tapOpen(page, 'river', point)
    await expectYielded(page, where)
    expect(await readChromeState(page)).toEqual(before)

    await closeSheet(page, 'river')
    await expectRestored(page, where)
    expect(await readChromeState(page), `关闭后控制条状态应等于开窗前 @ ${where}`).toEqual(before)
    expect(before.playLabel).toBe('播放时间轴')
    await expectOnlyExpanded(page, null)
  })

  for (const viewport of [PORTRAIT, LANDSCAPE, LANDSCAPE_WIDE]) {
    test(`(e)(f) ${viewport.width}x${viewport.height}：曲线区域兜底时外壳可见可操作、兜底块不压外壳；重试成功后重新让位，关闭后恢复且无面板展开`, async ({ page }, testInfo) => {
      await loadMap(page, viewport, 'river', { crashCurve: true })
      const where = label(page, testInfo)
      expect(await readCrashSwitch(page)).toEqual({ gate: true, region: 'curve' })
      const fallback = page.getByTestId(CURVE_FALLBACK)
      const river = curveWindowParts(page, 'river')
      const bar = controlBarParts(page)
      // 未开窗：开关为 curve 也没有兜底。
      await expect(fallback).toHaveCount(0)

      const point = await locatePoint(page, 'river')
      expect(await tapLocatedPoint(page, point)).toBe('tap')

      // (e) 曲线面板渲染期抛错：兜底在场、曲线窗不在，外壳没有被留在隐藏状态。
      await expect(fallback).toBeVisible()
      await expect(river.frame).toHaveCount(0)
      await expect(page.locator(CONTROL_BAR), `兜底态控制条应可见 @ ${where}`).toBeVisible()
      await expect(page.locator(LAUNCHER_COLUMN), `兜底态启动器列应可见 @ ${where}`).toBeVisible()

      const map = await boxOf(page.locator(MAP_REGION), '地图区')
      const barBox = await boxOf(page.locator(CONTROL_BAR), '控制条')
      const fallbackBox = await boxOf(fallback, '曲线区域兜底')
      const launchers: Array<{ name: string; box: Box }> = []
      for (const name of OVERLAY_PANELS) {
        const title = `${OVERLAY_PANEL_PARTS[name].title}启动器`
        await expect(overlayPart(page, name).launcher, `${title}应可见 @ ${where}`).toBeVisible()
        launchers.push({ name: title, box: await boxOf(overlayPart(page, name).launcher, title) })
      }
      console.log(`sheet-yields curve fallback @ ${where}`, JSON.stringify({ map, bar: barBox, fallback: fallbackBox, launchers }))

      expect.soft(contains(map, fallbackBox), `兜底块 ${JSON.stringify(fallbackBox)} 不在地图区 ${JSON.stringify(map)} 内 @ ${where}`).toBe(true)
      expect.soft(intersects(fallbackBox, barBox), `兜底块与控制条 ${JSON.stringify(barBox)} 相交 @ ${where}`).toBe(false)
      expect.soft(launchers).toHaveLength(3)
      for (const item of launchers) {
        expect.soft(intersects(fallbackBox, item.box), `兜底块与${item.name} ${JSON.stringify(item.box)} 相交 @ ${where}`).toBe(false)
      }

      // 可操作：时间轴能步进，启动器能展开面板。
      const beforeStep = await readChromeState(page)
      await bar.bar.getByRole('button', { name: '下一个有效时刻' }).tap()
      await expect.poll(async () => (await readChromeState(page)).sliderValue, '点「下一个有效时刻」后有效时刻应改变').not.toBe(beforeStep.sliderValue)
      await overlayPart(page, 'legend').launcher.tap()
      await expectOnlyExpanded(page, 'legend')
      await expect(fallback).toBeVisible()

      // (f) 面板展开着清掉开关、点「重试」——这条路没有地图点击：曲线窗出现，外壳重新让位。
      await clearCrashSwitch(page)
      expect(await readCrashSwitch(page)).toEqual({ gate: true, region: undefined })
      await fallback.getByRole('button', { name: '重试' }).tap()
      await expect(river.frame).toBeVisible()
      await expect(fallback).toHaveCount(0)
      await expectYielded(page, where)

      await closeSheet(page, 'river')
      await expectRestored(page, where)
      await expectOnlyExpanded(page, null)
    })
  }

  test('(g) 桌面对照 1280x900：开窗后控制条可见，其中心的命中测试落在控制条内', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    await installRiverWindowMocks(page)
    await openRiverWindow(page)

    await expect(curveWindowParts(page, 'river').frame).toBeVisible()
    await expect(page.locator(LAUNCHER_COLUMN), '桌面形态没有启动器列').toHaveCount(0)
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    const hit = await hitsItself(page.locator(CONTROL_BAR))
    expect(hit.onSelf, `桌面形态开窗后控制条中心应命中控制条，实际 ${hit.top}`).toBe(true)
  })
})
