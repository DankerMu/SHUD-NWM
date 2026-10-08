import { expect, test, type Page } from '@playwright/test'

import {
  BOX_TOLERANCE,
  HEADER_MOBILE_HEIGHT,
  expectHeaderHeight,
  expectInside,
  mockSiteHeaderApi,
  openAuthorizedRoute,
  requireBox,
  siteHeaderParts,
  titleStyle,
  type Box,
} from './support/siteHeader.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 移动形态头部：48px 单行（openspec mobile-responsive-display task 2.4，design.md D4）。
 * 三个移动 project 下都要通过；头部高度只由“是否移动形态”决定，所以没有矮视口横屏分支。
 */

const LOGO_MOBILE_SIZE = 32
const TITLE_MIN_FONT_SIZE = 16
const TITLE_MAX_FONT_SIZE = 18

/** 标题渲染为一行：`nowrap`，包围盒高不超过 1.5 倍行高，字号在 16–18px，且不伸出头部。 */
async function expectSingleLineTitle(page: Page, headerBox: Box, label: string) {
  const { title } = siteHeaderParts(page)
  await expect(title).toBeVisible()
  const style = await titleStyle(title)
  const box = await requireBox(title, '标题')
  expect(style.whiteSpace, `${label}: 标题 white-space`).toBe('nowrap')
  expect(style.lineHeight, `${label}: 标题行高应是可比较的像素值`).toBeGreaterThan(0)
  expect(box.height, `${label}: 标题高 ${box.height} 对行高 ${style.lineHeight}`).toBeLessThanOrEqual(style.lineHeight * 1.5)
  expect(style.fontSize, `${label}: 标题字号`).toBeGreaterThanOrEqual(TITLE_MIN_FONT_SIZE)
  expect(style.fontSize, `${label}: 标题字号`).toBeLessThanOrEqual(TITLE_MAX_FONT_SIZE)
  expectInside(box, headerBox, `${label}: 标题`)
  return { box, style }
}

async function expectCompactLogo(page: Page, headerBox: Box, label: string) {
  const { logo } = siteHeaderParts(page)
  await expect(logo).toBeVisible()
  const box = await requireBox(logo, '徽标')
  expect(Math.abs(box.width - LOGO_MOBILE_SIZE), `${label}: 徽标宽 ${box.width}`).toBeLessThanOrEqual(BOX_TOLERANCE)
  expect(Math.abs(box.height - LOGO_MOBILE_SIZE), `${label}: 徽标高 ${box.height}`).toBeLessThanOrEqual(BOX_TOLERANCE)
  expectInside(box, headerBox, `${label}: 徽标`)
  return box
}

test.describe('M11 移动形态头部', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    await mockSiteHeaderApi(page)
  })

  test('/ 上头部 48px 单行、无副标题，地图区从 y=48 铺到视口底', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await page.goto('/')

    const map = page.locator('[data-testid="m11-fullscreen-map"]')
    await expect(map).toBeVisible()

    const headerBox = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, label)
    const title = await expectSingleLineTitle(page, headerBox, label)
    const logoBox = await expectCompactLogo(page, headerBox, label)

    const { subtitle } = siteHeaderParts(page)
    // 副标题仍在 DOM 里（纯 CSS 隐藏），但不可见、不占位。
    await expect(subtitle).toHaveCount(1)
    await expect(subtitle).toBeHidden()

    const mapBox = await requireBox(map, '地图区')
    expect(Math.abs(mapBox.y - HEADER_MOBILE_HEIGHT), `${label}: 地图区顶边 y=${mapBox.y}`).toBeLessThanOrEqual(BOX_TOLERANCE)
    expect(
      Math.abs(mapBox.y + mapBox.height - viewport.height),
      `${label}: 地图区底边 ${mapBox.y + mapBox.height} 对视口高 ${viewport.height}`,
    ).toBeLessThanOrEqual(BOX_TOLERANCE)

    // 仅记录（报告用）：控制条与启动器列的位置。
    const others: Record<string, Box | null> = {}
    for (const testid of ['m11-bottom-control-bar', 'm11-launcher-layers', 'm11-launcher-basemap', 'm11-launcher-legend']) {
      others[testid] = await page.locator(`[data-testid="${testid}"]`).boundingBox()
    }
    console.log(
      `site-header boxes @ ${label}`,
      JSON.stringify({ header: headerBox, title: title.box, titleStyle: title.style, logo: logoBox, map: mapBox, ...others }),
    )
  })

  test('/ops 上（切到 operator 后）头部同为 48px', async ({ page }, testInfo) => {
    await openAuthorizedRoute(page, '/ops', 'operator', '内部诊断')

    const headerBox = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, `${testInfo.project.name} /ops`)
    await expectSingleLineTitle(page, headerBox, `${testInfo.project.name} /ops`)
    await expect(siteHeaderParts(page).subtitle).toBeHidden()
  })

  test('320x568 下头部仍 48px，标题截断而不溢出，徽标完整可见', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 568 })
    await page.goto('/')
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()

    const headerBox = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, '320x568')
    expect(headerBox.width, '头部宽应等于视口宽').toBeLessThanOrEqual(320 + BOX_TOLERANCE)
    const title = await expectSingleLineTitle(page, headerBox, '320x568')
    const logoBox = await expectCompactLogo(page, headerBox, '320x568')
    expect(title.box.x + title.box.width, '标题右边不得超过头部右边').toBeLessThanOrEqual(headerBox.x + headerBox.width)
    expect(logoBox.x, '徽标左边在视口内').toBeGreaterThanOrEqual(0)
    console.log('site-header boxes @ 320x568', JSON.stringify({ header: headerBox, title: title.box, titleStyle: title.style, logo: logoBox }))
  })

  test('标题内容比可用宽度长时真的截断：头部仍 48px，标题不伸出头部，徽标不被压扁', async ({ page }) => {
    // 靠收窄视口触发不了截断：`body` 有 `min-width: 320px`，头部最窄 320px，而 16px 的标题只有约 205px 宽
    // （可用约 236px）。所以这里在最窄宽度下把标题文字加长一倍，逼出“内容宽 > 可用宽”。
    await page.setViewportSize({ width: 320, height: 568 })
    await page.goto('/')
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()

    const { title } = siteHeaderParts(page)
    await expect(title).toBeVisible()
    // 先拿到元素句柄再改文字：改完后按文字定位的 locator 就不再命中它。
    const handle = await title.elementHandle()
    if (!handle) throw new Error('标题元素不存在')
    const scroll = await handle.evaluate((element) => {
      element.textContent = `${element.textContent}${element.textContent}`
      return { scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }
    })

    const headerBox = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, '320x568 长标题')
    // 前置：内容确实比可视框宽（否则下面的“不溢出”恒真）。
    expect(scroll.scrollWidth, `标题 scrollWidth ${scroll.scrollWidth} 对 clientWidth ${scroll.clientWidth}`).toBeGreaterThan(scroll.clientWidth)

    const style = await handle.evaluate((element) => {
      const computed = getComputedStyle(element)
      return { lineHeight: Number.parseFloat(computed.lineHeight), whiteSpace: computed.whiteSpace, textOverflow: computed.textOverflow }
    })
    const titleBox = await handle.boundingBox()
    if (!titleBox) throw new Error('标题没有可测量的布局矩形')
    expect(style.whiteSpace).toBe('nowrap')
    expect(style.textOverflow).toBe('ellipsis')
    expect(titleBox.height, `标题高 ${titleBox.height} 对行高 ${style.lineHeight}`).toBeLessThanOrEqual(style.lineHeight * 1.5)
    expect(titleBox.x + titleBox.width, '标题右边不得超过头部右边').toBeLessThanOrEqual(headerBox.x + headerBox.width)
    expectInside(titleBox, headerBox, '320x568 长标题: 标题')

    const logoBox = await expectCompactLogo(page, headerBox, '320x568 长标题')
    console.log('site-header boxes @ 320x568 long title', JSON.stringify({ header: headerBox, title: titleBox, titleScroll: scroll, logo: logoBox }))
  })
})
