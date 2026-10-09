import { expect, test, type Page, type TestInfo } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  MISMATCHED_STATION_ID,
  MOCK_EARLIER_CYCLE,
  SHEET_MAX_WIDTH_PX,
  SHEET_TOLERANCE_PX,
  SHEET_WIDTH_VIEWPORT_RATIO,
  centerOf,
  curveWindowParts,
  dragWithMouse,
  dragWithTouch,
  expectSheet,
  expectedSheetAnchor,
  hitsCurveWindow,
  holdRiverForecast,
  installMultipleIssueTimes,
  installStationIdentityMismatch,
  intersectionOf,
  measureCurveWindow,
  openCurveWindow,
  type CurveWindowKind,
} from './support/curveSheet.mocked'
import { contains, type Box } from './support/legendLauncher.mocked'
import { CONTROL_BAR, LAUNCHER_COLUMN, boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 曲线窗抽屉形态（openspec mobile-responsive-display task 4.2，design.md D9）。
 *
 * 用例 (a)–(l) 对应 tasks.md 里 #2803 的 Triage。需要特定视口的用例在用例内 `setViewportSize`，
 * 所以每条都在三个移动 project 下以同一组断言执行，不按 project 跳过；(d)(e) 用 project 自带的视口，
 * 三个 project 合起来覆盖底部抽屉与右侧抽屉。(d) 的层级判据是抽屉与控制条 / 启动器列的实测交集。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const LANDSCAPE_WIDE: ViewportSize = { width: 844, height: 390 }
/** 高 < 500 但不是横屏：仍是底部抽屉。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }
/** 宽 < 768 的矮视口横屏：右侧抽屉。 */
const NARROW_LANDSCAPE: ViewportSize = { width: 600, height: 400 }
/** 50vw > 28rem 的矮视口横屏：右侧抽屉宽度取 28rem（448px）那一支。 */
const WIDE_SHORT_LANDSCAPE: ViewportSize = { width: 1000, height: 400 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const DRAG_PX = 100
const DRAG_TOLERANCE_PX = 1

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

/** 设视口、装基线 mock、经真实轻触开窗。额外的 mock 变体须在调用前装好（后注册者先匹配——见各用例）。 */
async function openAt(page: Page, viewport: ViewportSize, kind: CurveWindowKind, variant?: (page: Page) => Promise<unknown>) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  if (variant) await variant(page)
  return openCurveWindow(page, kind)
}

test.describe('M11 曲线窗抽屉形态', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const kind of CURVE_WINDOW_KINDS) {
    const name = CURVE_WINDOWS[kind].name

    test(`(a) 390x664 ${name}：底部抽屉——左 / 右 / 底边贴地图区，高 = min(60% 视口高, 地图区高 − 8)`, async ({ page }, testInfo) => {
      await openAt(page, PORTRAIT, kind)
      expect(expectedSheetAnchor(requireViewport(page))).toBe('bottom')

      const sheet = await expectSheet(page, kind, label(page, testInfo))
      // 圆角只在上方。
      expect(sheet.bottomLeftRadius).toBe('0px')
    })

    for (const viewport of [LANDSCAPE, LANDSCAPE_WIDE]) {
      test(`(b) ${viewport.width}x${viewport.height} ${name}：右侧抽屉——上 / 下 / 右边贴地图区，宽 = min(50% 视口宽, 448)`, async ({ page }, testInfo) => {
        await openAt(page, viewport, kind)
        expect(expectedSheetAnchor(requireViewport(page))).toBe('right')

        await expectSheet(page, kind, label(page, testInfo))
      })
    }

    test(`(d) project 视口 ${name}：抽屉盖住控制条与启动器列——头部、贴边处与两者交集中心的命中测试都落在抽屉内`, async ({ page }, testInfo) => {
      await installRiverWindowMocks(page)
      await openCurveWindow(page, kind)
      const where = label(page, testInfo)
      const sheet = await expectSheet(page, kind, where)
      const anchor = expectedSheetAnchor(requireViewport(page))

      // 探测点避开贴 main 左缘、垂直居中的角色切换器（z-200）：底部抽屉取水平中线上贴底的一点，
      // 右侧抽屉取垂直中线上贴右的一点。
      const edgePoint =
        anchor === 'right'
          ? { x: sheet.frame.x + sheet.frame.width - 4, y: sheet.frame.y + sheet.frame.height / 2 }
          : { x: sheet.frame.x + sheet.frame.width / 2, y: sheet.frame.y + sheet.frame.height - 4 }
      for (const [pointName, point] of [
        ['头部中心', centerOf(sheet.handle)],
        ['贴边点', edgePoint],
      ] as const) {
        const hit = await hitsCurveWindow(page, kind, point)
        expect(hit.inside, `${pointName} ${JSON.stringify(point)} 的命中测试应落在抽屉内，实际 ${hit.top} @ ${where}`).toBe(true)
      }

      // 上面两点都不在控制条 / 启动器列上方，证明不了层级。真正的判据：量出抽屉与两者的交集，
      // 交集中心的命中栈里既有被盖住的那一个（它确实在这一点）、栈顶又在抽屉内。
      const bar = await boxOf(page.locator(CONTROL_BAR), '控制条')
      const column = await boxOf(page.locator(LAUNCHER_COLUMN), '启动器列')
      const barOverlap = intersectionOf(sheet.frame, bar)
      const columnOverlap = intersectionOf(sheet.frame, column)
      console.log(`curve-sheet layering ${kind} @ ${where}`, JSON.stringify({ sheet: sheet.frame, bar, column, barOverlap, columnOverlap }))

      // 控制条贴地图区底：两种抽屉都与它相交。
      expect(barOverlap, `抽屉应与控制条相交 @ ${where}`).not.toBeNull()
      const covered: Array<[string, string, Box]> = [['控制条', CONTROL_BAR, barOverlap!]]
      if (anchor === 'right') {
        // 启动器列在地图区右上角：右侧抽屉盖住它。
        expect(columnOverlap, `右侧抽屉应与启动器列相交 @ ${where}`).not.toBeNull()
        covered.push(['启动器列', LAUNCHER_COLUMN, columnOverlap!])
      } else {
        // 底部抽屉的顶边在启动器列底边之下：两者不相交，没有层级可断言。
        expect(columnOverlap, `底部抽屉不应与启动器列相交 @ ${where}`).toBeNull()
      }
      for (const [coveredName, selector, overlap] of covered) {
        const point = centerOf(overlap)
        const hit = await hitsCurveWindow(page, kind, point, selector)
        expect(hit.beneath, `前提：${coveredName}在交集中心 ${JSON.stringify(point)} 的命中栈里 @ ${where}`).toBe(true)
        // soft：控制条与启动器列各自报告，一处被压在下面不掩盖另一处。
        expect.soft(hit.inside, `${coveredName}交集中心 ${JSON.stringify(point)} 的命中测试应落在抽屉内（抽屉盖住${coveredName}），实际 ${hit.top} @ ${where}`).toBe(true)
      }
    })

    test(`(e) project 视口 ${name}：真实轻触开窗，窗内曲线已画出`, async ({ page }) => {
      const mocks = await installRiverWindowMocks(page)
      const opened = await openCurveWindow(page, kind)

      expect(opened.input).toBe('tap')
      const parts = curveWindowParts(page, kind)
      await expect(parts.frame).toBeVisible()
      await expect(parts.title).toBeVisible()
      await expect(parts.chartCanvas).toBeVisible()
      expect(mocks.unmocked, '不应有未被 mock 的 /api/v1 请求').toEqual([])
    })

    test(`(i) 390x664 ${name}：在头部按下、向上拖 100px（鼠标与触摸各一次）后包围盒不变`, async ({ page }, testInfo) => {
      await openAt(page, PORTRAIT, kind)
      // 等内容落定（图表容器已挂上）再量：之后包围盒的任何变化都只能来自拖动。
      await expect(page.getByTestId(CURVE_WINDOWS[kind].chart)).toBeAttached()
      const before = await measureCurveWindow(page, kind)
      console.log(`curve-sheet drag ${kind} before @ ${label(page, testInfo)}`, JSON.stringify(before.frame))

      // 纵向：改动前 390 宽时窗的水平 clamp 区间只有一个点，水平拖动测不出东西。
      await dragWithMouse(page, centerOf(before.handle), 0, -DRAG_PX)
      const afterMouse = await measureCurveWindow(page, kind)
      expect(afterMouse.frame, '鼠标拖头部后包围盒应不变').toEqual(before.frame)

      await dragWithTouch(page, centerOf(afterMouse.handle), 0, -DRAG_PX)
      const afterTouch = await measureCurveWindow(page, kind)
      expect(afterTouch.frame, '触摸拖头部后包围盒应不变').toEqual(before.frame)

      await expectSheet(page, kind, label(page, testInfo))
    })
  }

  test('(c) 320x480 河段窗：竖向矮视口仍是底部抽屉', async ({ page }, testInfo) => {
    await openAt(page, SHORT_PORTRAIT, 'river')
    expect(expectedSheetAnchor(requireViewport(page))).toBe('bottom')

    await expectSheet(page, 'river', label(page, testInfo))
  })

  test('(c) 600x400 河段窗：宽 < 768 的矮视口横屏是右侧抽屉', async ({ page }, testInfo) => {
    await openAt(page, NARROW_LANDSCAPE, 'river')
    expect(expectedSheetAnchor(requireViewport(page))).toBe('right')

    await expectSheet(page, 'river', label(page, testInfo))
  })

  test('(b) 1000x400 河段窗：50% 视口宽超过 448 时右侧抽屉宽度封顶在 448', async ({ page }, testInfo) => {
    await openAt(page, WIDE_SHORT_LANDSCAPE, 'river')
    const viewport = requireViewport(page)
    expect(expectedSheetAnchor(viewport)).toBe('right')
    // 前提：这个视口选中的是 28rem 那一支（750 / 844 / 600 宽都只走 50vw 那一支）。
    expect(SHEET_WIDTH_VIEWPORT_RATIO * viewport.width).toBeGreaterThan(SHEET_MAX_WIDTH_PX)

    const sheet = await expectSheet(page, 'river', label(page, testInfo))
    expect(Math.abs(sheet.frame.width - SHEET_MAX_WIDTH_PX), `抽屉宽 ${sheet.frame.width} 应为 ${SHEET_MAX_WIDTH_PX}`).toBeLessThanOrEqual(SHEET_TOLERANCE_PX)
  })

  test('(f) 390x664 河段窗：预报请求挂起时抽屉已是公式尺寸、标题与关闭按钮可见；加载完成后包围盒逐值不变', async ({ page }, testInfo) => {
    let hold!: Awaited<ReturnType<typeof holdRiverForecast>>
    await openAt(page, PORTRAIT, 'river', async (target) => {
      hold = await holdRiverForecast(target)
    })
    const where = label(page, testInfo)
    const parts = curveWindowParts(page, 'river')

    // 两个源的 forecast-series 都已发出并被挂起：窗处在“请求未返回”的状态。
    await expect.poll(() => hold.heldCount()).toBeGreaterThanOrEqual(2)
    await expect(parts.chartCanvas).toHaveCount(0)
    const pending = await expectSheet(page, 'river', `${where} pending`)
    await expect(parts.title).toBeVisible()
    await expect(parts.close).toBeVisible()
    for (const [partName, locator] of [['标题', parts.title], ['关闭按钮', parts.close]] as const) {
      expect(contains(pending.frame, await boxOf(locator, partName)), `${partName}应在抽屉盒内 @ ${where}`).toBe(true)
    }

    await hold.release()
    await expect(parts.chartCanvas).toBeVisible()
    const loaded = await expectSheet(page, 'river', `${where} loaded`)
    expect(loaded.frame, '加载完成后抽屉包围盒应与请求未返回时逐值相同').toEqual(pending.frame)
  })

  test('(g) 390x664 气象代站窗：序列身份校验失败时不可用原因在抽屉盒内，抽屉仍是公式尺寸', async ({ page }, testInfo) => {
    await openAt(page, PORTRAIT, 'station', installStationIdentityMismatch)
    const where = label(page, testInfo)

    const reasons = page.getByTestId('m11-station-popup-empty').locator('li')
    await expect(reasons).toHaveCount(2)
    await expect(reasons).toContainText([`GFS：station_id=${MISMATCHED_STATION_ID}`, `IFS：station_id=${MISMATCHED_STATION_ID}`])
    await expect(curveWindowParts(page, 'station').chartCanvas).toHaveCount(0)

    const sheet = await expectSheet(page, 'station', where)
    for (const index of [0, 1]) {
      const reason = reasons.nth(index)
      await expect(reason).toBeVisible()
      const box = await boxOf(reason, `不可用原因 ${index + 1}`)
      expect(contains(sheet.frame, box), `不可用原因 ${JSON.stringify(box)} 应在抽屉盒 ${JSON.stringify(sheet.frame)} 内 @ ${where}`).toBe(true)
    }
  })

  test('(h) 750x342 气象代站窗：主体滚到底后标题与关闭按钮仍在抽屉可视框内；头部不在滚动主体里', async ({ page }, testInfo) => {
    await openAt(page, LANDSCAPE, 'station')
    const where = label(page, testInfo)
    const parts = curveWindowParts(page, 'station')
    await expect(parts.chartCanvas).toBeVisible()
    const sheet = await expectSheet(page, 'station', where)

    // 结构：头部（抓手容器）在主体容器之外，主体自己纵向滚动。
    expect(sheet.body, '主体容器应存在').not.toBeNull()
    expect(sheet.handleInsideBody, '抓手容器不应是主体容器的后代').toBe(false)
    expect(sheet.body!.overflowY).toBe('auto')

    await parts.body.evaluate((element) => {
      element.scrollTop = element.scrollHeight
    })
    const scrolled = await measureCurveWindow(page, 'station')
    console.log(
      `curve-sheet body scroll @ ${where}`,
      JSON.stringify({ scrollHeight: scrolled.body!.scrollHeight, clientHeight: scrolled.body!.clientHeight, scrollTop: await parts.body.evaluate((element) => element.scrollTop) }),
    )
    expect(scrolled.frame).toEqual(sheet.frame)
    expect(scrolled.handle, '头部不随主体滚动').toEqual(sheet.handle)
    await expect(parts.title).toBeVisible()
    await expect(parts.close).toBeVisible()
    for (const [partName, locator] of [['标题', parts.title], ['关闭按钮', parts.close]] as const) {
      expect(contains(scrolled.frame, await boxOf(locator, partName)), `${partName}应在抽屉可视框内 @ ${where}`).toBe(true)
    }

    // 本 task 的真实内容还不会溢出（上面的 scrollHeight == clientHeight；图表下限归 4.4 / 4.5），
    // 所以再在测试侧往主体里塞一块高内容，让主体真的滚起来：头部仍不动、抽屉盒不变。
    const forcedScrollTop = await parts.body.evaluate((element) => {
      const filler = document.createElement('div')
      filler.style.cssText = 'height: 600px; flex-shrink: 0;'
      element.appendChild(filler)
      element.scrollTop = element.scrollHeight
      return element.scrollTop
    })
    expect(forcedScrollTop, '塞入高内容后主体应真的滚动了').toBeGreaterThan(100)
    const forced = await measureCurveWindow(page, 'station')
    expect(forced.frame, '主体内容变高不改变抽屉盒').toEqual(sheet.frame)
    expect(forced.handle, '主体真的滚动后头部仍不动').toEqual(sheet.handle)
    for (const [partName, locator] of [['标题', parts.title], ['关闭按钮', parts.close]] as const) {
      await expect(locator).toBeVisible()
      expect(contains(forced.frame, await boxOf(locator, partName)), `主体滚动后${partName}应在抽屉可视框内 @ ${where}`).toBe(true)
    }
    const titleHit = await hitsCurveWindow(page, 'station', centerOf(await boxOf(parts.title, '标题')))
    expect(titleHit.inside, '主体滚动后标题没有被滚上来的内容盖住').toBe(true)
  })

  test('(j) 390x664 → 750x342 河段窗：同一河段、同一起报时次的窗变为右侧抽屉', async ({ page }, testInfo) => {
    let issueTimes!: Awaited<ReturnType<typeof installMultipleIssueTimes>>
    await openAt(page, PORTRAIT, 'river', async (target) => {
      issueTimes = await installMultipleIssueTimes(target)
    })
    const parts = curveWindowParts(page, 'river')
    await expectSheet(page, 'river', label(page, testInfo))

    // 先在竖屏把起报时次切到非默认项。
    const trigger = page.getByTestId('m11-river-panel-cycle')
    await expect(trigger).toBeEnabled()
    const defaultText = (await trigger.innerText()).trim()
    expect(defaultText).not.toBe('')
    await trigger.click()
    const options = page.getByRole('option')
    await expect(options).toHaveCount(2)
    await options.filter({ hasNotText: defaultText }).click()
    await expect(trigger).not.toHaveText(defaultText)
    await expect(trigger).toBeEnabled()
    const selectedText = (await trigger.innerText()).trim()
    expect(selectedText).not.toBe('')
    expect(issueTimes.requestedCycles, '选中的应是 mock 多列的那个更早时次').toContain(MOCK_EARLIER_CYCLE)
    // 给窗的 DOM 节点做记号：旋转后还是同一个节点（没有重挂）。
    await parts.frame.evaluate((element) => {
      ;(element as HTMLElement & { __curveSheetMark?: string }).__curveSheetMark = 'before-rotation'
    })

    await page.setViewportSize(LANDSCAPE)
    expect(expectedSheetAnchor(requireViewport(page))).toBe('right')
    await expectSheet(page, 'river', label(page, testInfo))

    await expect(parts.title).toBeVisible()
    await expect(trigger).toHaveText(selectedText)
    expect(
      await parts.frame.evaluate((element) => (element as HTMLElement & { __curveSheetMark?: string }).__curveSheetMark),
      '旋转后应是同一个窗节点',
    ).toBe('before-rotation')
  })

  test('(k) 390x664 → 1280x900 河段窗：成为默认位置的可拖拽桌面窗，包围盒等于在 1280x900 新开的河段窗，控制条可见', async ({ page }, testInfo) => {
    await openAt(page, PORTRAIT, 'river')
    const parts = curveWindowParts(page, 'river')
    await expect(parts.chartCanvas).toBeVisible()
    await expectSheet(page, 'river', label(page, testInfo))

    // 对照：同一浏览器上下文里新开一个页面，在 1280x900 新打开河段窗。
    const fresh = await page.context().newPage()
    await openAt(fresh, DESKTOP, 'river')
    await expect(curveWindowParts(fresh, 'river').chartCanvas).toBeVisible()
    const freshWindow = await measureCurveWindow(fresh, 'river')
    await fresh.close()

    await page.setViewportSize(DESKTOP)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    let desktop = await measureCurveWindow(page, 'river')
    await expect(async () => {
      desktop = await measureCurveWindow(page, 'river')
      expect(desktop.frame).toEqual(freshWindow.frame)
    }).toPass({ timeout: 4_000 })
    console.log(`curve-sheet back to desktop @ ${label(page, testInfo)}`, JSON.stringify({ desktop: desktop.frame, fresh: freshWindow.frame }))
    await expect(parts.frame).toBeVisible()
    expect(desktop.handleCursor).toBe('grab')
    expect(desktop.aspectRatio).toBe('16 / 9')

    await dragWithMouse(page, centerOf(desktop.handle), DRAG_PX, 0)
    const dragged = await measureCurveWindow(page, 'river')
    expect(Math.abs(dragged.frame.x - desktop.frame.x - DRAG_PX), `拖头部 100px 后窗应右移 100px，实际 ${dragged.frame.x - desktop.frame.x}`).toBeLessThanOrEqual(DRAG_TOLERANCE_PX)
    expect(Math.abs(dragged.frame.y - desktop.frame.y)).toBeLessThanOrEqual(DRAG_TOLERANCE_PX)

    await expect(page.locator(CONTROL_BAR)).toBeVisible()
  })

  test('(l) 1280x900 拖离默认位置 → 390x664 → 1280x900 河段窗：回到默认位置，不恢复拖拽坐标', async ({ page }, testInfo) => {
    await openAt(page, DESKTOP, 'river')
    const parts = curveWindowParts(page, 'river')
    await expect(parts.chartCanvas).toBeVisible()
    const initial = await measureCurveWindow(page, 'river')

    await dragWithMouse(page, centerOf(initial.handle), 150, 120)
    const dragged = await measureCurveWindow(page, 'river')
    expect(dragged.frame.x - initial.frame.x, '前提：桌面形态下窗被拖离默认位置').toBeGreaterThan(100)
    expect(dragged.frame.y - initial.frame.y).toBeGreaterThan(100)

    await page.setViewportSize(PORTRAIT)
    await expectSheet(page, 'river', label(page, testInfo))

    await page.setViewportSize(DESKTOP)
    await expect(async () => {
      expect((await measureCurveWindow(page, 'river')).frame).toEqual(initial.frame)
    }).toPass({ timeout: 4_000 })
    await expect(parts.frame).toBeVisible()
  })
})
