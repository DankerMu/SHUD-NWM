import { expect, test, type Page } from '@playwright/test'

import { gotoWithRiverHooks, locateRiverPoint, tapLocatedPoint } from './support/openRiverWindow'
import { riverFixture, sharedReachFixture } from './support/riverFixture'
import { installRiverWindowMocks, mockModel, mockSecondModel, type RiverWindowMockLog } from './support/riverWindow.mocked'

/**
 * 共用河道的点击归属（openspec shared-reach-click-ownership task 4.6，#3390）。
 *
 * 夹具瓦片里两个河网的河段完全重合：夹具河网（`riverFixture`，河段数少、id 小）与第二个河网（id 大、
 * 河段数多）。只按 id（改动前的绘制顺序）会选第二个；按规则应选河段数少的夹具河网。两张瓦片以两种顺序
 * 绘制这两个要素，故 MapLibre 在该点返回它们的顺序也相反。
 *
 * 等计数到达不加 DOM 属性：轮询钩子问第二个河网的河段，直到答案是 `HOOK_POINT_OCCLUDED`（计数到达前由 id
 * 决定、这一问会成功）；DOM 遮挡也会给出同一个码，所以紧接着问夹具河段必须成功，再在该点做一次真实点击，
 * 打开的河段窗标题必须是夹具河段。
 */
const { identity, anchor, bbox } = riverFixture
const second = sharedReachFixture.second.identity
const RIVER_PANEL = 'm11-river-forecast-panel'
const LOCATE_CALL_TIMEOUT_MS = 5_000

/** 问钩子定位某个河段，只要结果码（成功为 `'ok'`）；每次调用在 Node 侧限时，超时记成 `HELPER_LOCATE_TIMEOUT`。 */
async function locateOutcome(page: Page, segment: typeof identity): Promise<string> {
  const asking = page.evaluate(async (input) => {
    const hook = (
      window as unknown as { __nhmsRiverClickEvidence: { locateRenderedRiver: (value: typeof input) => Promise<unknown> } }
    ).__nhmsRiverClickEvidence
    try {
      await hook.locateRenderedRiver(input)
      return 'ok'
    } catch (error) {
      return String((error as { code?: unknown } | null)?.code ?? 'UNKNOWN')
    }
  }, { bbox, anchor, ...segment })
  asking.catch(() => undefined)
  let timer: ReturnType<typeof setTimeout> | undefined
  const timedOut = new Promise<string>((resolve) => {
    timer = setTimeout(() => resolve('HELPER_LOCATE_TIMEOUT'), LOCATE_CALL_TIMEOUT_MS)
  })
  return Promise.race([asking, timedOut]).finally(() => clearTimeout(timer))
}

for (const tile of sharedReachFixture.tiles) {
  test.describe(`M11 共用河道：点击打开河段数少的河网的河段（瓦片 ${tile.name}）`, () => {
    let mocks: RiverWindowMockLog

    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 900 })
      mocks = await installRiverWindowMocks(page, { sharedReach: tile.name })
    })

    test('桌面 1280×900：计数到达后钩子判第二个河网被遮挡，真实点击打开夹具河段', async ({ page }) => {
      // 夹具的前提：河段数少的河网 id 也小，只按 id 会选另一个；两张瓦片的顺序互为反序。
      expect(identity.riverNetworkVersionId < second.riverNetworkVersionId).toBe(true)
      expect(mockModel.segment_count).toBeLessThan(mockSecondModel.segment_count)
      expect(sharedReachFixture.tiles.map((entry) => entry.order.join(','))).toEqual([
        `${identity.riverNetworkVersionId},${second.riverNetworkVersionId}`,
        `${second.riverNetworkVersionId},${identity.riverNetworkVersionId}`,
      ])

      await gotoWithRiverHooks(page)
      await expect
        .poll(() => locateOutcome(page, second), {
          message: `the hook should find ${second.riverSegmentId} occluded once the model counts are loaded`,
          timeout: 8_000,
          intervals: [200, 400, 800],
        })
        .toBe('HOOK_POINT_OCCLUDED')

      const point = await locateRiverPoint(page)
      expect(await tapLocatedPoint(page, point)).toBe('click')

      const header = page.getByTestId(RIVER_PANEL).locator('header')
      await expect(page.getByTestId(RIVER_PANEL)).toBeVisible()
      await expect(header.getByText(identity.riverSegmentId, { exact: true })).toBeVisible()
      await expect(header.getByText(`河段 ID ${identity.riverSegmentId}`, { exact: true })).toBeVisible()
      await expect(header.getByText(second.riverSegmentId)).toHaveCount(0)
      expect(mocks.unmocked, '不应有未被 mock 的 /api/v1 请求').toEqual([])
    })
  })
}
