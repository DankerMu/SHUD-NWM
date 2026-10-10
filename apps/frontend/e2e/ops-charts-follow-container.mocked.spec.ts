import { expect, test, type Page } from '@playwright/test'

import {
  OPS_FALLBACK_TARGETS,
  measureCheckedOpsFallbackPage,
  measureOpsCharts,
  measureOpsFallbackPage,
  openOpsFallbackPage,
  type OpsChartMeasure,
} from './support/opsFallback.mocked'
import type { ViewportSize } from './support/viewportForm'

/**
 * 运维页图表跟随容器宽度（openspec ops-charts-follow-container task 1.3，#2860）。
 *
 * 这是守卫，不是保证能红的测试：被吞的只有图表绑定后的第一次尺寸回调（及其后 60ms 防抖窗口内的变化），
 * 这个窗口从页面外部无法确定性命中。机制由 `src/components/charts/__tests__/OpsChartsFollowContainer.test.tsx`
 * 的可控 ResizeObserver 用例钉住；这里钉真实浏览器里的结果。
 *
 * - 两条用例各自重新加载，改视口是加载完成后的第一个动作（同一页面上的第二次改视口不会被吞，恒绿）。
 * - 视口对同属单列布局：宽 ≥ 1200 时阶段列 / 趋势列是固定宽，容器不随视口变。
 * - 只测 `/ops`：`/monitoring` 是同一个页面组件。
 */
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const PORTRAIT: ViewportSize = { width: 390, height: 664 }
/** 受控 mock 下的图表：环图 1、趋势 2、阶段时长 1。 */
const CHART_COUNT = 4

const OPS = OPS_FALLBACK_TARGETS.find((target) => target.route === '/ops')
if (!OPS) throw new Error('OPS_FALLBACK_TARGETS has no /ops target')

/** 每张图一项：canvas 已画出且与容器等宽时为容器宽，否则为描述差异的字符串（轮询失败时直接可读）。 */
function followed(charts: OpsChartMeasure[]): Array<number | string> {
  return charts.map(({ containerWidth, canvasWidth }) =>
    canvasWidth === containerWidth ? containerWidth : `canvas ${canvasWidth} != container ${containerWidth}`,
  )
}

/** 轮询到恰好 4 张图、每张 canvas 宽等于其容器 `clientWidth`，返回各容器宽。 */
async function waitForChartsToFollow(page: Page, message: string): Promise<number[]> {
  let widths: number[] = []
  await expect
    .poll(
      async () => {
        const charts = await measureOpsCharts(page)
        const state = followed(charts)
        const settled = charts.length === CHART_COUNT && state.every((item) => typeof item === 'number')
        if (settled) widths = charts.map(({ containerWidth }) => containerWidth)
        return settled ? 'followed' : `${charts.length} charts: ${state.join(' | ')}`
      },
      { message },
    )
    .toBe('followed')
  return widths
}

const CASES: Array<{ name: string; from: ViewportSize; to: ViewportSize }> = [
  { name: '变窄', from: LANDSCAPE, to: PORTRAIT },
  { name: '变宽', from: PORTRAIT, to: LANDSCAPE },
]

test.describe('/ops 的图表在已加载页面上跟随容器宽度', () => {
  for (const { name, from, to } of CASES) {
    test(`${name}：${from.width}x${from.height} 加载后改为 ${to.width}x${to.height}，四张图的画布宽等于容器宽且页面不横向溢出`, async ({ page }, testInfo) => {
      await page.setViewportSize(from)
      await openOpsFallbackPage(page, OPS)

      const before = await waitForChartsToFollow(page, '改视口前：4 张图的 canvas 宽应等于容器宽')
      // 这里到改视口之间不再量别的：多一次往返就离被吞的那次尺寸回调更远。
      await page.setViewportSize(to)

      const after = await waitForChartsToFollow(page, '改视口后：4 张图的 canvas 宽应等于容器宽')
      testInfo.annotations.push({ type: 'chart-container-widths', description: `${before.join(',')} -> ${after.join(',')}` })
      // 容器没变宽度的话，上面的等宽不需要任何重排也成立。
      after.forEach((width, index) => expect(width, `第 ${index + 1} 张图的容器宽应随视口变化`).not.toBe(before[index]))

      await expect
        .poll(
          async () => {
            const measure = await measureOpsFallbackPage(page)
            return measure ? measure.scroller.scrollWidth - measure.scroller.clientWidth : null
          },
          { message: '页面滚动容器 scrollWidth − clientWidth' },
        )
        .toBeLessThanOrEqual(0)
      const measure = await measureCheckedOpsFallbackPage(page)
      expect(measure.viewport).toEqual(to)
      expect(measure.mainGridColumns, '改视口后主网格仍是单列').toBe(1)
      expect((await measureOpsCharts(page)).map(({ containerWidth }) => containerWidth), '量溢出时各容器宽未再变').toEqual(after)
    })
  }
})
