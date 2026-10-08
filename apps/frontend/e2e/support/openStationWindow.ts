import type { Page } from '@playwright/test'

import { mockStation } from './riverWindow.mocked'

/**
 * 经真实产品点击路径打开气象代站曲线窗（openspec mobile-responsive-display task 1.4）。
 *
 * 前置：已 `installRiverWindowMocks(page)`、已设好视口、页面尚未导航。本助手开
 * `__NHMS_E2E_HOOKS__` 门控，用产品 URL 状态 `?metStations=1` 打开代站图层并导航，等站点层就绪，
 * 用只读钩子 `window.__nhmsStationLocateEvidence.locateRenderedStation` 把站点移到地图区中央并拿到
 * 视口点，再在该点做一次真实点击（触屏上下文用 `touchscreen.tap`）。钩子只定位、不调用任何产品回调，
 * 窗口只能由这次指针输入触发的 MapLibre click 打开。
 *
 * 只负责“从无窗状态打开一个窗”。钩子缺失、站点层未就绪、定位失败、点击后窗未出现都抛出带原因的错误。
 */
export interface OpenStationWindowResult {
  /** 实际点击 / 轻触的视口坐标（CSS px）。 */
  point: { x: number; y: number }
  /** 用的是哪种指针输入。 */
  input: 'click' | 'tap'
}

export interface StationLocateInput {
  stationId: string
  lngLat: [number, number]
}

export type StationLocateOutcome =
  | { ok: true; located: { stationId: string; clientX: number; clientY: number } }
  | { ok: false; code: string; message: string }

/** `locateRenderedStation` 的入参：全部取自站点 mock 的唯一导出。 */
export const stationLocateInput: StationLocateInput = {
  stationId: mockStation.station_id,
  lngLat: [mockStation.longitude, mockStation.latitude],
}

/** 代站图层默认关；产品 URL 状态 `metStations=1` 打开它（`src/lib/m11/queryState.ts`）。 */
export const STATION_LAYER_URL = '/?metStations=1'

const STATION_WINDOW = '[data-testid="m11-station-popup"]'
/** 站点层就绪：产品已打开代站图层且有要素（该属性在图层关或零要素时为 0）。 */
const MAP_WITH_STATIONS = '[data-testid="m11-map-surface"]:not([data-met-station-feature-count="0"])'
/**
 * 各阶段的等待上限，口径同 `openRiverWindow`：Playwright 默认单测超时 30s，这里四段相加 21s，
 * 慢机器上先触发的是本助手带原因的错误而不是泛泛的 "Test timeout"。钩子内部另有 15s 上限，
 * 故定位这一步在 Node 侧用更短的 5s 截断。改这些数时保持总和明显小于 30s（模块加载时校验）。
 */
const STAGE_TIMEOUT_MS = { hook: 6_000, layer: 6_000, locate: 5_000, window: 4_000 } as const
const HELPER_BUDGET_MS = 22_000
const stageTotalMs = Object.values(STAGE_TIMEOUT_MS).reduce((sum, value) => sum + value, 0)
if (stageTotalMs > HELPER_BUDGET_MS) {
  throw new Error(`openStationWindow: stage timeouts sum to ${stageTotalMs}ms, over the ${HELPER_BUDGET_MS}ms helper budget`)
}

/** 开门控、带 `metStations=1` 导航，并等到钩子存在、站点层就绪。 */
export async function gotoWithStationLayer(page: Page, options: { url?: string } = {}): Promise<void> {
  await page.addInitScript(() => {
    ;(window as unknown as { __NHMS_E2E_HOOKS__?: boolean }).__NHMS_E2E_HOOKS__ = true
  })
  await page.goto(options.url ?? STATION_LAYER_URL)

  await page
    .waitForFunction(
      () => {
        const hook = (window as unknown as { __nhmsStationLocateEvidence?: { locateRenderedStation?: unknown } })
          .__nhmsStationLocateEvidence
        return typeof hook?.locateRenderedStation === 'function'
      },
      undefined,
      { timeout: STAGE_TIMEOUT_MS.hook },
    )
    .catch(() => {
      throw new Error(
        `openStationWindow: window.__nhmsStationLocateEvidence.locateRenderedStation is missing after ${STAGE_TIMEOUT_MS.hook}ms ` +
          `(is the map mounted at ${page.url()} and was the __NHMS_E2E_HOOKS__ gate set before load?)`,
      )
    })
  await page
    .locator(MAP_WITH_STATIONS)
    .waitFor({ state: 'attached', timeout: STAGE_TIMEOUT_MS.layer })
    .catch(() => {
      throw new Error(
        `openStationWindow: the station layer has no features within ${STAGE_TIMEOUT_MS.layer}ms ` +
          `(does ${page.url()} carry metStations=1 and are the station mocks installed via installRiverWindowMocks?)`,
      )
    })
}

/**
 * 在页面里调一次 `locateRenderedStation`。钩子以普通对象 `{code, message}` reject；这里折成可序列化的
 * 结果再回到 Node 侧，并用 Node 侧的定时器截断（钩子自身上限 15s）。
 */
export async function locateStationOnPage(page: Page, input: StationLocateInput): Promise<StationLocateOutcome> {
  const locating = page.evaluate(async (value): Promise<StationLocateOutcome> => {
    const hook = (
      window as unknown as {
        __nhmsStationLocateEvidence?: { locateRenderedStation: (input: typeof value) => Promise<Record<string, unknown>> }
      }
    ).__nhmsStationLocateEvidence
    if (typeof hook?.locateRenderedStation !== 'function') {
      return { ok: false, code: 'HELPER_HOOK_MISSING', message: 'window.__nhmsStationLocateEvidence.locateRenderedStation is missing' }
    }
    try {
      const located = await hook.locateRenderedStation(value)
      return { ok: true, located: located as Extract<StationLocateOutcome, { ok: true }>['located'] }
    } catch (error) {
      const failure = (error ?? {}) as { code?: unknown; message?: unknown }
      return { ok: false, code: String(failure.code ?? 'UNKNOWN'), message: String(failure.message ?? error) }
    }
  }, input)
  let locateTimer: ReturnType<typeof setTimeout> | undefined
  const locateTimedOut = new Promise<StationLocateOutcome>((resolve) => {
    locateTimer = setTimeout(
      () =>
        resolve({
          ok: false,
          code: 'HELPER_LOCATE_TIMEOUT',
          message: `no answer from the hook within ${STAGE_TIMEOUT_MS.locate}ms (did the map go idle after the camera move?)`,
        }),
      STAGE_TIMEOUT_MS.locate,
    )
  })
  // 超时后页面里的定位仍在跑；它的结果（或页面关闭带来的 rejection）不再有人等，接住即可。
  locating.catch(() => undefined)
  return Promise.race([locating, locateTimedOut]).finally(() => clearTimeout(locateTimer))
}

export async function openStationWindow(page: Page, options: { url?: string } = {}): Promise<OpenStationWindowResult> {
  await gotoWithStationLayer(page, options)

  const alreadyOpen = await page.locator(STATION_WINDOW).count()
  if (alreadyOpen > 0) {
    throw new Error('openStationWindow: a station window is already open; this helper only opens one from the no-window state')
  }

  const outcome = await locateStationOnPage(page, stationLocateInput)
  if (!outcome.ok) {
    throw new Error(
      `openStationWindow: locateRenderedStation failed with ${outcome.code}: ${outcome.message} ` +
        `(station ${stationLocateInput.stationId}, lngLat ${JSON.stringify(stationLocateInput.lngLat)})`,
    )
  }
  const { located } = outcome
  if (!Number.isFinite(located.clientX) || !Number.isFinite(located.clientY)) {
    throw new Error(`openStationWindow: locateRenderedStation returned a non-finite point ${JSON.stringify(located)}`)
  }
  if (located.stationId !== stationLocateInput.stationId) {
    throw new Error(
      `openStationWindow: located stationId=${String(located.stationId)} differs from the mock's ${stationLocateInput.stationId}`,
    )
  }

  const point = { x: located.clientX, y: located.clientY }
  const hasTouch = await page.evaluate(() => navigator.maxTouchPoints > 0)
  const input: OpenStationWindowResult['input'] = hasTouch ? 'tap' : 'click'
  if (hasTouch) {
    await page.touchscreen.tap(point.x, point.y)
  } else {
    await page.mouse.click(point.x, point.y)
  }

  await page
    .locator(STATION_WINDOW)
    .waitFor({ state: 'visible', timeout: STAGE_TIMEOUT_MS.window })
    .catch(() => {
      throw new Error(
        `openStationWindow: the station window did not appear within ${STAGE_TIMEOUT_MS.window}ms after a real ${input} ` +
          `at (${point.x}, ${point.y})`,
      )
    })

  return { point, input }
}
