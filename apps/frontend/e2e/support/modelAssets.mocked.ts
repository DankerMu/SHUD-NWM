import { expect, type Page } from '@playwright/test'

import type { Box } from './legendLauncher.mocked'
import { mockModelAssetsApi, modelAssetDetailPayload } from './monitoring.mocked'
import { setRole } from './setRole'

/**
 * 模型资产页（openspec mobile-responsive-display task 6.2，design.md D14 末段）的开页助手与量具。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用（mock 本身在 `monitoring.mocked.ts`）。
 *
 * 规格对这一页只有一条不变量：移动形态下页面滚动容器自身不横向溢出，宽于视口的产品表只在它自己的容器里横向滚。
 * 这里只量、不判：返回的数字由 spec 断言；仅有的断言是“取错元素”与硬前提的防护。
 */

/** 页面滚动容器的 `aria-label`（`ModelAssetsPage.tsx` 的根 `section`）。 */
export const MODEL_ASSETS_PAGE_LABEL = '模型资产管理'
/** 产品表为空时页面渲染的空态文案；它出现即说明量到的不是“非空产品表”。 */
export const EMPTY_PRODUCTS_TEXT = '暂无产品资产'

/** 既有 mock 里唯一的模型；带 `modelId` 的深链才会渲染详情区与产品表。 */
export const MODEL_ASSETS_URL = `/system/model-assets?modelId=${modelAssetDetailPayload().model_id}`

/** 装好 mock，以 model_admin 角色打开模型资产页并等到详情区渲染出来。 */
export async function openModelAssetsPage(page: Page) {
  await mockModelAssetsApi(page)
  await visitModelAssetsPage(page)
}

/**
 * 导航到模型资产页（mock 已装好）、切到 model_admin 并等到详情区就绪。角色不跨导航保留，每次导航后都重新切。
 * 用例内换视口后要量页面的，先 `setViewportSize` 再调用这里重新加载，不在已加载的页面上直接改视口
 * （与运维页 spec 同样的写法）。
 */
export async function visitModelAssetsPage(page: Page) {
  await page.goto(MODEL_ASSETS_URL)
  await expect(page.getByText('权限不足')).toBeVisible()
  await setRole(page, 'model_admin')
  await expect(page.getByRole('heading', { name: MODEL_ASSETS_PAGE_LABEL })).toBeVisible()
  await expect(page.getByText('产品资产', { exact: true })).toBeVisible()
  // 换角色后门禁有短暂过渡态：那时 main 的最后一个子元素还不是页面滚动容器，所以轮询。
  await expect
    .poll(async () => {
      const measure = await measureModelAssetsPage(page)
      return measure ? { overflowY: measure.scroller.overflowY, ariaLabel: measure.scroller.ariaLabel } : null
    })
    .toEqual({ overflowY: 'auto', ariaLabel: MODEL_ASSETS_PAGE_LABEL })
}

export interface ModelAssetsPageMeasure {
  viewport: { width: number; height: number }
  /** 页面滚动容器 = `main` 的最后一个子元素。 */
  scroller: { overflowY: string; ariaLabel: string | null; scrollWidth: number; clientWidth: number }
  /** 详情区外层网格（页面滚动容器的最后一个子元素）：计算后的 `grid-template-columns` 轨道数；不是 grid 时为 0。 */
  detailGridColumns: number
  /** 外层网格右侧的那个网格项（详情列）；外层网格不存在时为 null。 */
  detailColumn: { box: Box; minWidth: string } | null
  /** 产品表自己的容器（产品 `table` 的父元素）；页面上没有表时为 null（空态或详情未渲染）。 */
  tableContainer: { overflowX: string; childTag: string; scrollWidth: number; clientWidth: number } | null
  /** 产品表的数据行数（`tbody tr`）。 */
  productRows: number
  /** 页面上是否出现了“暂无产品资产”的空态。 */
  emptyProducts: boolean
  windowScrollY: number
  documentScrollTop: number
  /** 文档滚动元素的纵向溢出量 `scrollHeight − clientHeight`：> 0 即窗口可滚。 */
  documentOverflowY: number
  /** `main`（页面滚动容器的父元素）：它不该是滚动者。 */
  main: { overflowY: string; scrollHeight: number; clientHeight: number; scrollTop: number }
}

/**
 * 量页面；`main` 下还没有元素时返回 null（供轮询等待）。产品表缺失不返回 null，而是 `tableContainer: null`——
 * 这样“空产品”会红在可读的硬前提上，而不是红在一次等不到页面的超时上。
 */
export async function measureModelAssetsPage(page: Page): Promise<ModelAssetsPageMeasure | null> {
  return page.evaluate((emptyText) => {
    const main = document.querySelector('main')
    const scroller = main?.lastElementChild
    const scrollingElement = document.scrollingElement
    if (!main || !(scroller instanceof HTMLElement) || !scrollingElement) return null

    const detailGrid = scroller.lastElementChild
    const detailColumn = detailGrid?.lastElementChild
    const table = scroller.querySelector('table')
    const tableContainer = table?.parentElement
    const columnsOf = (grid: Element | null | undefined) => {
      if (!(grid instanceof HTMLElement)) return 0
      const style = getComputedStyle(grid)
      if (style.display !== 'grid') return 0
      return style.gridTemplateColumns.split(/\s+/).filter((track) => track !== '' && track !== 'none').length
    }
    const detailColumnRect = detailColumn instanceof HTMLElement ? detailColumn.getBoundingClientRect() : null

    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      scroller: {
        overflowY: getComputedStyle(scroller).overflowY,
        ariaLabel: scroller.getAttribute('aria-label'),
        scrollWidth: scroller.scrollWidth,
        clientWidth: scroller.clientWidth,
      },
      detailGridColumns: columnsOf(detailGrid),
      detailColumn:
        detailColumn instanceof HTMLElement && detailColumnRect
          ? {
              box: { x: detailColumnRect.x, y: detailColumnRect.y, width: detailColumnRect.width, height: detailColumnRect.height },
              minWidth: getComputedStyle(detailColumn).minWidth,
            }
          : null,
      tableContainer:
        tableContainer instanceof HTMLElement
          ? {
              overflowX: getComputedStyle(tableContainer).overflowX,
              childTag: tableContainer.firstElementChild?.tagName.toLowerCase() ?? '',
              scrollWidth: tableContainer.scrollWidth,
              clientWidth: tableContainer.clientWidth,
            }
          : null,
      productRows: table ? table.querySelectorAll('tbody tr').length : 0,
      emptyProducts: (scroller.textContent ?? '').includes(emptyText),
      windowScrollY: window.scrollY,
      documentScrollTop: scrollingElement.scrollTop,
      documentOverflowY: scrollingElement.scrollHeight - scrollingElement.clientHeight,
      main: {
        overflowY: getComputedStyle(main).overflowY,
        scrollHeight: main.scrollHeight,
        clientHeight: main.clientHeight,
        scrollTop: main.scrollTop,
      },
    }
  }, EMPTY_PRODUCTS_TEXT)
}

/** 取到非空产品表之后的量值：`tableContainer` 一定存在。 */
export type CheckedModelAssetsPageMeasure = ModelAssetsPageMeasure & {
  tableContainer: NonNullable<ModelAssetsPageMeasure['tableContainer']>
}

/**
 * 量页面并校验硬前提与取错防护：页面滚动容器 `overflow-y: auto` 且带页面的 `aria-label`；产品表非空
 * （不是空态、至少一行数据行）；产品表容器 `overflow-x: auto` 且直接子元素是 `table`。
 */
export async function measureCheckedModelAssetsPage(page: Page): Promise<CheckedModelAssetsPageMeasure> {
  const measure = await measureModelAssetsPage(page)
  if (!measure) throw new Error('模型资产页还没有就绪：main 下没有页面滚动容器')
  expect(measure.scroller.overflowY, '页面滚动容器（main 的最后一个子元素）的 overflow-y').toBe('auto')
  expect(measure.scroller.ariaLabel, '页面滚动容器的 aria-label').toBe(MODEL_ASSETS_PAGE_LABEL)
  expect(measure.emptyProducts, `硬前提：产品表非空，页面不应出现「${EMPTY_PRODUCTS_TEXT}」`).toBe(false)
  expect(measure.productRows, '硬前提：产品表至少一行数据行').toBeGreaterThanOrEqual(1)
  const { tableContainer } = measure
  if (!tableContainer) throw new Error('硬前提：页面上没有产品表')
  expect(tableContainer.childTag, '产品表容器的直接子元素').toBe('table')
  expect(tableContainer.overflowX, '产品表容器（产品 table 的父元素）的 overflow-x').toBe('auto')
  return { ...measure, tableContainer }
}
