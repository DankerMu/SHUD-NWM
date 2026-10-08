import { expect, type Locator, type Page, type Route } from '@playwright/test'

import type { components } from '../../src/api/types'
import { intersects, type Box } from './legendLauncher.mocked'
import { installRiverWindowMocks, type RiverWindowMockLog } from './riverWindow.mocked'

/**
 * 浮动提示与状态条 spec（openspec mobile-responsive-display task 3.4）的 mock 与量具。
 *
 * 基线 = `installRiverWindowMocks`：一个非空流域、底图瓦片回合法 PNG、其余瓦片 204——默认状态下
 * 没有任何浮动提示，也没有状态条。每个场景只在基线之上叠一个真实的请求结果（后注册的路由先匹配）：
 * 底图瓦片 503、代站列表为空、总览的流域清单请求挂起。不往 DOM 里塞节点。
 */
type Schemas = components['schemas']

export const NOTICE_TEST_IDS = {
  stationStatus: 'm11-met-station-status',
  loading: 'm11-overview-loading',
  empty: 'm11-overview-empty',
  dataAnomaly: 'm11-data-anomaly',
  precip: 'm11-precip-notice',
} as const

export const STATUS_TEST_IDS = {
  basinLayerUnavailable: 'm11-basin-layer-unavailable',
  mapUnavailable: 'm11-map-unavailable',
  selectedSegmentUnavailable: 'm11-selected-segment-map-unavailable',
  mapSourceError: 'm11-map-source-error',
} as const

/** `src/components/map/m11MapRuntime.tsx` 的 `M11_BASEMAP_UNAVAILABLE_NOTICE`，逐字。 */
export const BASEMAP_UNAVAILABLE_TEXT =
  '底图服务暂时不可用（天地图限流或网络异常），河网与预报图层不受影响，底图稍后自动恢复。'
/** `src/pages/m11/useStationLayer.ts`：代站列表成功返回零个站点时的状态说明。 */
export const STATION_EMPTY_TEXT = '暂无可渲染气象代站'
export const LOADING_TEXT = '总览数据加载中'

export const CANVAS_SELECTOR = 'canvas.maplibregl-canvas'

export interface NoticeMockOptions {
  /** 全部天地图底图瓦片回 503：点亮 `m11-map-source-error`（404 不行——MapLibre 对 404 瓦片不发 error）。 */
  failBasemapTiles?: boolean
  /** 代站列表回零个站点：开代站图层后 `m11-met-station-status` = “暂无可渲染气象代站”。 */
  emptyStations?: boolean
  /** 挂起 `/api/v1/basins`（总览 bootstrap 的一环）直到 `releaseBootstrap()`：`m11-overview-loading` 常亮。 */
  holdBootstrap?: boolean
}

export interface NoticeMocks {
  base: RiverWindowMockLog
  /** 被回了 503 的底图瓦片请求数（实时）。 */
  failedBasemapTiles: () => number
  /** 放行挂起的流域清单请求；没挂起时是空操作。可重复调用。 */
  releaseBootstrap: () => void
}

const emptyStationPage = {
  items: [],
  total_count: 0,
  limit: 500,
  offset: 0,
  filters: { available: { search: true, variables: false, qc_status: false } },
} satisfies Schemas['MetStationPage']

const pathIs = (pathname: string) => (url: URL) => url.pathname === pathname

/** 须在 `page.goto` 之前调用。 */
export async function installNoticeMocks(page: Page, options: NoticeMockOptions = {}): Promise<NoticeMocks> {
  const base = await installRiverWindowMocks(page)
  let failed = 0
  let release: () => void = () => undefined

  if (options.failBasemapTiles) {
    await page.route(
      (url) => url.pathname.startsWith('/api/v1/basemap/tianditu/'),
      (route: Route) => {
        failed += 1
        return route.fulfill({ status: 503, contentType: 'text/plain', body: 'Service Unavailable' })
      },
    )
  }
  if (options.emptyStations) {
    await page.route(pathIs('/api/v1/met/stations'), (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', data: emptyStationPage }),
      }),
    )
  }
  if (options.holdBootstrap) {
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(pathIs('/api/v1/basins'), async (route: Route) => {
      await gate
      // 放行后落回基线 mock 的流域清单。
      await route.fallback()
    })
  }

  return { base, failedBasemapTiles: () => failed, releaseBootstrap: () => release() }
}

export interface TwoLineMetrics {
  /** 两行文本的高度上限 = line-height × 2 + 纵向内边距 + 上下边框（px）。 */
  cap: number
  lineHeight: number
  /** 元素或它的某个后代被截断：内容高度超过可视高度（下面两个数取自差值最大的那个元素）。 */
  clipped: boolean
  scrollHeight: number
  clientHeight: number
}

/** 从元素的计算样式推出两行上限；`line-height: normal` 无从换算，直接报错。 */
export async function twoLineMetrics(locator: Locator): Promise<TwoLineMetrics> {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element)
    const px = (value: string) => {
      const parsed = Number.parseFloat(value)
      if (!Number.isFinite(parsed)) throw new Error(`cannot derive a pixel value from ${JSON.stringify(value)}`)
      return parsed
    }
    const lineHeight = px(style.lineHeight)
    const cap =
      lineHeight * 2 +
      px(style.paddingTop) +
      px(style.paddingBottom) +
      px(style.borderTopWidth) +
      px(style.borderBottomWidth)
    // 截断类可以在元素自身，也可以在包着文本的内层：取溢出最多的那个。
    const clipper = [element, ...Array.from(element.querySelectorAll('*'))].reduce((worst, candidate) =>
      candidate.scrollHeight - candidate.clientHeight > worst.scrollHeight - worst.clientHeight ? candidate : worst,
    )
    return {
      cap,
      lineHeight,
      clipped: clipper.scrollHeight > clipper.clientHeight,
      scrollHeight: clipper.scrollHeight,
      clientHeight: clipper.clientHeight,
    }
  })
}

/**
 * 把元素里的文本自我拼接到至少 `minChars` 个字符（截断用例专用；这是唯一一处改页面 DOM 的地方）。
 * 只改文本节点本身，不动元素结构——截断类可能在包着文本的内层上。
 */
export async function lengthenText(locator: Locator, minChars: number): Promise<number> {
  return locator.evaluate((element, target) => {
    const node = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
    const original = node?.nodeValue ?? ''
    if (!node || original.length === 0) throw new Error('element has no text node to lengthen')
    let text = original
    while (text.length < target) text += original
    node.nodeValue = text
    return text.length
  }, minChars)
}

/**
 * 状态条下方不吞地图手势：在 `region`（状态条所在的条带）里、`below` 底边之下铺一张点网格，
 * 去掉落在 `exclude`（确实盖在那里的别的浮层，如角色切换器）里的点，其余每个点的命中测试都必须是
 * 地图画布。返回被检查的点数与没命中画布的点（空 = 通过）。
 */
export async function hitsBelow(
  page: Page,
  region: Box,
  below: Box,
  exclude: Box[],
): Promise<{ checked: number; misses: Array<{ x: number; y: number; hit: string | null }> }> {
  const points: Array<{ x: number; y: number }> = []
  const step = 16
  for (let y = below.y + below.height + 4; y < region.y + region.height - 2; y += step) {
    for (let x = region.x + 4; x < region.x + region.width - 2; x += step) {
      const probe = { x, y, width: 0.01, height: 0.01 }
      if (!exclude.some((box) => intersects(box, probe))) points.push({ x, y })
    }
  }
  const misses = await page.evaluate(
    ({ candidates, canvasSelector }) =>
      candidates.flatMap((point) => {
        const hit = document.elementFromPoint(point.x, point.y)
        if (hit !== null && hit.matches(canvasSelector)) return []
        return [
          {
            ...point,
            hit: hit ? `${hit.tagName.toLowerCase()}[data-testid=${hit.getAttribute('data-testid')}].${hit.className}` : null,
          },
        ]
      }),
    { candidates: points, canvasSelector: CANVAS_SELECTOR },
  )
  return { checked: points.length, misses }
}

export async function expectNoFloatingNotice(page: Page) {
  for (const testId of Object.values(NOTICE_TEST_IDS)) {
    await expect(page.getByTestId(testId), `浮动提示 ${testId} 不应出现`).toHaveCount(0)
  }
}
