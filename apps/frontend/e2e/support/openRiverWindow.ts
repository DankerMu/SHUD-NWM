import type { Page } from '@playwright/test'

import { riverFixture } from './riverFixture'

/**
 * 经真实产品点击路径打开河段曲线窗（openspec mobile-responsive-display task 1.3）。
 *
 * 前置：已 `installRiverWindowMocks(page)`、已设好视口、页面尚未导航。本助手开
 * `__NHMS_E2E_HOOKS__` 门控并导航，等地图就绪，用既有只读钩子
 * `window.__nhmsRiverClickEvidence.locateRenderedRiver` 把夹具河段移到地图区中央并拿到视口点，
 * 再在该点做一次真实点击（触屏上下文用 `touchscreen.tap`）。钩子只定位、不调用任何产品回调，
 * 窗口只能由这次指针输入触发的 MapLibre click 打开。
 *
 * 只负责“从无窗状态打开一个窗”。钩子缺失、定位失败、点击后窗未出现都抛出带原因的错误。
 */
export interface OpenRiverWindowResult {
  /** 实际点击 / 轻触的视口坐标（CSS px）。 */
  point: { x: number; y: number }
  /** 用的是哪种指针输入。 */
  input: 'click' | 'tap'
}

type HookIdentity = { basinId: string; riverSegmentId: string; basinVersionId: string; riverNetworkVersionId: string }
type LocateOutcome =
  | { ok: true; located: HookIdentity & { clientX: number; clientY: number } }
  | { ok: false; code: string; message: string }

const RIVER_PANEL = '[data-testid="m11-river-forecast-panel"]'
const MAP_WITH_DISCHARGE = '[data-testid="m11-map-surface"][data-registered-overlays="discharge"]'
const READY_TIMEOUT_MS = 20_000
const PANEL_TIMEOUT_MS = 10_000

const { identity, anchor, bbox } = riverFixture
/** `locateRenderedRiver` 的入参：全部取自夹具的唯一导出。 */
const locateInput = { bbox, anchor, ...identity }

export async function openRiverWindow(page: Page, options: { url?: string } = {}): Promise<OpenRiverWindowResult> {
  await page.addInitScript(() => {
    ;(window as unknown as { __NHMS_E2E_HOOKS__?: boolean }).__NHMS_E2E_HOOKS__ = true
  })
  await page.goto(options.url ?? '/')

  await page
    .waitForFunction(
      () => {
        const hook = (window as unknown as { __nhmsRiverClickEvidence?: { locateRenderedRiver?: unknown } }).__nhmsRiverClickEvidence
        return typeof hook?.locateRenderedRiver === 'function'
      },
      undefined,
      { timeout: READY_TIMEOUT_MS },
    )
    .catch(() => {
      throw new Error(
        `openRiverWindow: window.__nhmsRiverClickEvidence.locateRenderedRiver is missing after ${READY_TIMEOUT_MS}ms ` +
          `(is the map mounted at ${page.url()} and was the __NHMS_E2E_HOOKS__ gate set before load?)`,
      )
    })
  await page
    .locator(MAP_WITH_DISCHARGE)
    .waitFor({ state: 'attached', timeout: READY_TIMEOUT_MS })
    .catch(() => {
      throw new Error(
        `openRiverWindow: the map did not register the discharge overlay within ${READY_TIMEOUT_MS}ms ` +
          '(are the layers / cycles mocks installed via installRiverWindowMocks?)',
      )
    })

  const alreadyOpen = await page.locator(RIVER_PANEL).count()
  if (alreadyOpen > 0) {
    throw new Error('openRiverWindow: a river window is already open; this helper only opens one from the no-window state')
  }

  // 钩子以普通对象 `{code, message}` reject；在页面内折成可序列化的结果再回到 Node 侧。
  const outcome = await page.evaluate(async (input): Promise<LocateOutcome> => {
    const hook = (
      window as unknown as {
        __nhmsRiverClickEvidence: { locateRenderedRiver: (value: typeof input) => Promise<Record<string, unknown>> }
      }
    ).__nhmsRiverClickEvidence
    try {
      const located = await hook.locateRenderedRiver(input)
      return { ok: true, located: located as Extract<LocateOutcome, { ok: true }>['located'] }
    } catch (error) {
      const failure = (error ?? {}) as { code?: unknown; message?: unknown }
      return { ok: false, code: String(failure.code ?? 'UNKNOWN'), message: String(failure.message ?? error) }
    }
  }, locateInput)

  if (!outcome.ok) {
    throw new Error(
      `openRiverWindow: locateRenderedRiver failed with ${outcome.code}: ${outcome.message} ` +
        `(segment ${identity.riverSegmentId}, anchor ${JSON.stringify(anchor)})`,
    )
  }
  const { located } = outcome
  if (!Number.isFinite(located.clientX) || !Number.isFinite(located.clientY)) {
    throw new Error(`openRiverWindow: locateRenderedRiver returned a non-finite point ${JSON.stringify(located)}`)
  }
  for (const key of ['basinId', 'riverSegmentId', 'basinVersionId', 'riverNetworkVersionId'] as const) {
    if (located[key] !== identity[key]) {
      throw new Error(`openRiverWindow: located ${key}=${String(located[key])} differs from the fixture's ${identity[key]}`)
    }
  }

  const point = { x: located.clientX, y: located.clientY }
  const hasTouch = await page.evaluate(() => navigator.maxTouchPoints > 0)
  const input: OpenRiverWindowResult['input'] = hasTouch ? 'tap' : 'click'
  if (hasTouch) {
    await page.touchscreen.tap(point.x, point.y)
  } else {
    await page.mouse.click(point.x, point.y)
  }

  await page
    .locator(RIVER_PANEL)
    .waitFor({ state: 'visible', timeout: PANEL_TIMEOUT_MS })
    .catch(() => {
      throw new Error(
        `openRiverWindow: the river window did not appear within ${PANEL_TIMEOUT_MS}ms after a real ${input} ` +
          `at (${point.x}, ${point.y})`,
      )
    })

  return { point, input }
}
