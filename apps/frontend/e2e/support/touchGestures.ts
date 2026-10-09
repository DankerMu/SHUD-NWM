import type { CDPSession, Page } from '@playwright/test'

/**
 * 经 Chromium CDP `Input.dispatchTouchEvent` 合成的触摸手势（openspec mobile-responsive-display task 4.7 / 4.8）。
 *
 * 走的是浏览器的真实输入管线：`touch-action`、浏览器自己的滚动 / 页面缩放、页面里的 touch 监听都照常生效，
 * 这正是这些手势用例要量的东西——所以不用 `dispatchEvent` 伪造 DOM 事件。与面板无关：只收视口坐标。
 *
 * 分步之间留间隔：图表库对缩放 / 平移的派发有节流，一帧内挤完的手势只会落下最后一步。
 */
export type TouchPoint = { x: number; y: number }

export interface TouchGestureOptions {
  /** 移动分成多少步。 */
  steps?: number
  /** 相邻两步之间的间隔（ms）。 */
  stepDelayMs?: number
}

const DEFAULT_STEPS = 8
const DEFAULT_STEP_DELAY_MS = 40

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function lerp(from: TouchPoint, to: TouchPoint, ratio: number): TouchPoint {
  return { x: from.x + (to.x - from.x) * ratio, y: from.y + (to.y - from.y) * ratio }
}

async function withTouchSession(page: Page, gesture: (session: CDPSession) => Promise<void>) {
  const session = await page.context().newCDPSession(page)
  try {
    await gesture(session)
  } finally {
    await session.detach()
  }
}

/** 每根手指各自从起点到终点分步移动：全部按下、同步移动、全部抬起。 */
async function moveFingers(page: Page, fingers: Array<{ from: TouchPoint; to: TouchPoint }>, options: TouchGestureOptions) {
  const steps = options.steps ?? DEFAULT_STEPS
  const stepDelayMs = options.stepDelayMs ?? DEFAULT_STEP_DELAY_MS
  const touchPointsAt = (ratio: number) => fingers.map((finger, id) => ({ ...lerp(finger.from, finger.to, ratio), id }))
  await withTouchSession(page, async (session) => {
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: touchPointsAt(0) })
    for (let step = 1; step <= steps; step += 1) {
      await pause(stepDelayMs)
      await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: touchPointsAt(step / steps) })
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  })
}

/** 轻触：按下后原地抬起。 */
export async function tapWithTouch(page: Page, point: TouchPoint) {
  await withTouchSession(page, async (session) => {
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ ...point, id: 0 }] })
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  })
}

/** 单指拖动：在 `from` 按下、分步移动 `(dx, dy)`、抬起。 */
export async function dragOneFinger(page: Page, from: TouchPoint, dx: number, dy: number, options: TouchGestureOptions = {}) {
  await moveFingers(page, [{ from, to: { x: from.x + dx, y: from.y + dy } }], options)
}

/**
 * 双指横向捏合：两指在 `center` 两侧同一水平线上，指距从 `fromSpan` 变到 `toSpan`
 * （变大 = 向外捏合 = 放大）。
 */
export async function pinchHorizontally(
  page: Page,
  center: TouchPoint,
  fromSpan: number,
  toSpan: number,
  options: TouchGestureOptions = {},
) {
  const finger = (side: -1 | 1) => ({
    from: { x: center.x + (side * fromSpan) / 2, y: center.y },
    to: { x: center.x + (side * toSpan) / 2, y: center.y },
  })
  await moveFingers(page, [finger(-1), finger(1)], options)
}
