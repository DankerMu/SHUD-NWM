import { expect, test, type Page } from '@playwright/test'

import {
  measureCheckedModelAssetsPage,
  measureModelAssetsPage,
  openModelAssetsPage,
} from './support/modelAssets.mocked'
import { tryScrollWindowAndMain } from './support/opsFallback.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 模型资产页的移动形态兜底（openspec mobile-responsive-display task 6.2，design.md D14 末段）。
 * 规格对这一页只有一条不变量：页面滚动容器自身不横向溢出，宽于视口的产品表只在它自己的容器里横向滚；
 * 除此之外该页按桌面页对待——不断言内边距、单列、触控尺寸与字号。
 * 三个移动 project 下都要通过、不按 project 跳过。读数一律轮询。
 */

const PHONE_VIEWPORT = { width: 390, height: 664 }

/** 页面滚动容器自身不横向溢出；随后校验硬前提（产品表非空）与两个容器没有取错。 */
async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(async () => {
      const measure = await measureModelAssetsPage(page)
      return measure ? measure.scroller.scrollWidth - measure.scroller.clientWidth : null
    }, { message: '页面滚动容器 scrollWidth − clientWidth' })
    .toBe(0)
  return measureCheckedModelAssetsPage(page)
}

test.describe('模型资产页移动形态兜底', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  test('页面滚动容器不横向溢出（产品表非空）', async ({ page }) => {
    await openModelAssetsPage(page)
    // 硬前提先于溢出断言：权限不足卡片或空态下没有产品表，不能让溢出断言空过。
    await measureCheckedModelAssetsPage(page)
    const measure = await expectNoPageOverflow(page)
    expect(measure.viewport).toEqual(requireViewport(page))
  })

  test('390x664 下页面滚动容器不横向溢出，产品表在自己的容器里横向滚', async ({ page }) => {
    // 先设视口再导航，不在已加载的页面上直接改视口。
    await page.setViewportSize(PHONE_VIEWPORT)
    await openModelAssetsPage(page)
    await measureCheckedModelAssetsPage(page)
    await expectNoPageOverflow(page)
    await expect
      .poll(async () => {
        const container = (await measureModelAssetsPage(page))?.tableContainer
        return container ? container.scrollWidth - container.clientWidth : null
      }, { message: '产品表容器 scrollWidth − clientWidth' })
      .toBeGreaterThan(0)
    const measure = await measureCheckedModelAssetsPage(page)
    expect(measure.viewport).toEqual(PHONE_VIEWPORT)
  })

  test('窗口不滚：文档与 main 都没有可滚余量', async ({ page }) => {
    await openModelAssetsPage(page)
    await measureCheckedModelAssetsPage(page)
    // 主动去滚窗口、文档滚动元素与 main，三者都必须滚不动（唯一的纵向滚动者是页面滚动容器）。
    await tryScrollWindowAndMain(page, 1000)
    await expect
      .poll(async () => {
        const measure = await measureModelAssetsPage(page)
        if (!measure) return null
        return {
          windowScrollY: measure.windowScrollY,
          documentScrollTop: measure.documentScrollTop,
          // 文档与 main 本身没有可滚的余量，而不只是此刻偏移为 0。
          documentScrollable: measure.documentOverflowY > 0,
          mainScrollable: measure.main.scrollHeight > measure.main.clientHeight,
          mainScrollTop: measure.main.scrollTop,
        }
      })
      .toEqual({ windowScrollY: 0, documentScrollTop: 0, documentScrollable: false, mainScrollable: false, mainScrollTop: 0 })
    const measure = await measureCheckedModelAssetsPage(page)
    expect(measure.main.overflowY, 'main 的 overflow-y').toBe('hidden')
  })
})
