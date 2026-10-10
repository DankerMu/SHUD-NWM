import { expect, test, type Page } from '@playwright/test'

import { openModelAssetsPage } from './support/modelAssets.mocked'
import { mockOpsEntryApi } from './support/opsEntry.mocked'
import { OPS_FALLBACK_TARGETS, openOpsFallbackPage } from './support/opsFallback.mocked'
import { CONTROL_BAR, OPS_LINK } from './support/overlayLaunchers.mocked'
import { ROLE_LABELS, setRole, type RoleValue } from './support/setRole'
import {
  BOX_TOLERANCE,
  HEADER_MOBILE_HEIGHT,
  expectHeaderHeight,
  mockSiteHeaderApi,
  requireBox,
  siteHeaderParts,
  type Box,
} from './support/siteHeader.mocked'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 开发用角色切换器在整页滚动的路由上的移动位置（#2862，openspec role-selector-off-scrolling-pages task 2.1）：
 * `/ops`、`/monitoring`、`/system/model-assets` 上它在头部右端，不压页面滚动容器；`/` 上仍在 main 左缘
 * （那一半由 `m11-role-selector.mobile.mocked.spec.ts` 钉住）。
 * 三个移动 project 下都要通过；期望不随“是否矮视口横屏”变化，所以没有形态分支。
 */

const OPS_HEADING = '内部诊断'
const FORBIDDEN = '权限不足'

/** 整页滚动的三个路由；`open` 装好 mock、切到有权限的角色并等到页面滚动容器就绪。 */
const SCROLLING_PAGES: Array<{ route: string; open: (page: Page) => Promise<void> }> = [
  ...OPS_FALLBACK_TARGETS.map((target) => ({ route: target.route, open: (page: Page) => openOpsFallbackPage(page, target) })),
  { route: '/system/model-assets', open: openModelAssetsPage },
]

function intersects(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
}

/** `inner` 的包围盒完全在 `outer` 内（±0.5）。 */
function inside(inner: Box, outer: Box): boolean {
  return (
    inner.x >= outer.x - BOX_TOLERANCE &&
    inner.y >= outer.y - BOX_TOLERANCE &&
    inner.x + inner.width <= outer.x + outer.width + BOX_TOLERANCE &&
    inner.y + inner.height <= outer.y + outer.height + BOX_TOLERANCE
  )
}

interface HeaderSelectorMeasure {
  header: Box
  logo: Box
  /** 带 `truncate` 的标题元素自己的盒：截断后它才是标题的可见范围（`Range` 矩形不受 `overflow: hidden` 裁剪）。 */
  title: Box
  titleScroll: { scrollWidth: number; clientWidth: number }
  trigger: Box
  /** 合作单位条：窄于 `lg` 时不显示，为 null。 */
  sponsors: Box | null
  placement: { inHeader: boolean; inMain: boolean; hitsItself: boolean; hitTop: string | null }
}

/** 量头部各部分与触发器（头部高 48px 在这里断言），并把包围盒记进日志（报告的实测表）。 */
async function measureHeaderSelector(page: Page, label: string): Promise<HeaderSelectorMeasure> {
  const { logo, title, sponsors } = siteHeaderParts(page)
  const trigger = page.getByLabel('Role')
  await expect(trigger).toBeVisible()
  await expect(logo).toBeVisible()
  await expect(title).toBeVisible()
  const header = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, label)
  const measure: HeaderSelectorMeasure = {
    header,
    logo: await requireBox(logo, '徽标'),
    title: await requireBox(title, '标题'),
    titleScroll: await title.evaluate((element) => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth })),
    trigger: await requireBox(trigger, '角色切换器触发器'),
    sponsors: (await sponsors.isVisible()) ? await requireBox(sponsors, '合作单位条') : null,
    placement: await trigger.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      return {
        inHeader: element.closest('header') !== null,
        inMain: element.closest('main') !== null,
        hitsItself: top !== null && element.contains(top),
        hitTop: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
      }
    }),
  }
  console.log(
    `role-selector header boxes @ ${label}`,
    JSON.stringify({ ...measure, titleTruncated: measure.titleScroll.scrollWidth > measure.titleScroll.clientWidth }),
  )
  return measure
}

/** 页面滚动容器 = `main` 的最后一个元素子节点；先断言它确实是滚动者（`overflow-y: auto`）再量。 */
async function measurePageScroller(page: Page, label: string): Promise<Box> {
  const scroller = await page.evaluate(() => {
    const element = document.querySelector('main')?.lastElementChild
    if (!(element instanceof HTMLElement)) return null
    const rect = element.getBoundingClientRect()
    return { overflowY: getComputedStyle(element).overflowY, box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }
  })
  expect(scroller, `${label}: main 下应有页面滚动容器`).not.toBeNull()
  expect(scroller!.overflowY, `${label}: 页面滚动容器（main 的最后一个子元素）的 overflow-y`).toBe('auto')
  expect(scroller!.box.height, `${label}: 页面滚动容器应有高度`).toBeGreaterThan(0)
  return scroller!.box
}

/** 触发器在头部里、在视口内、没被盖住，且不与徽标、标题元素、（可见时的）合作单位条相交。彼此独立，用 soft 断言一次报全。 */
function expectSelectorInHeader(measure: HeaderSelectorMeasure, viewport: { width: number; height: number }, label: string) {
  const { trigger, header, logo, title, sponsors, placement } = measure
  const describe = (box: Box) => JSON.stringify(box)
  expect.soft(placement.inHeader, `${label}: 触发器应是 header 的后代`).toBe(true)
  expect.soft(placement.inMain, `${label}: 触发器不应是 main 的后代`).toBe(false)
  expect.soft(inside(trigger, header), `${label}: 触发器 ${describe(trigger)} 应在头部 ${describe(header)} 内`).toBe(true)
  expect.soft(
    inside(trigger, { x: 0, y: 0, width: viewport.width, height: viewport.height }),
    `${label}: 触发器 ${describe(trigger)} 应在视口 ${viewport.width}x${viewport.height} 内`,
  ).toBe(true)
  expect.soft(trigger.width, `${label}: 触发器宽`).toBeGreaterThan(0)
  expect.soft(trigger.height, `${label}: 触发器高`).toBeGreaterThan(0)
  expect.soft(placement.hitsItself, `${label}: 触发器中心被 ${placement.hitTop} 盖住`).toBe(true)
  expect.soft(intersects(trigger, logo), `${label}: 触发器 ${describe(trigger)} 与徽标 ${describe(logo)} 相交`).toBe(false)
  expect.soft(intersects(trigger, title), `${label}: 触发器 ${describe(trigger)} 与标题 ${describe(title)} 相交`).toBe(false)
  if (sponsors) {
    expect.soft(intersects(trigger, sponsors), `${label}: 触发器 ${describe(trigger)} 与合作单位条 ${describe(sponsors)} 相交`).toBe(false)
  }
}

test.describe('M11 角色切换器在整页滚动路由上的移动位置', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const { route, open } of SCROLLING_PAGES) {
    test(`${route} 上触发器在 48px 头部内，不压页面滚动容器、徽标与标题`, async ({ page }, testInfo) => {
      const viewport = requireViewport(page)
      const label = `${route} ${testInfo.project.name} ${viewport.width}x${viewport.height}`
      await open(page)

      const measure = await measureHeaderSelector(page, label)
      const scroller = await measurePageScroller(page, label)
      // 四个移动视口都窄于 lg：合作单位条不显示。
      expect(measure.sponsors, `${label}: 合作单位条不应可见`).toBeNull()
      expectSelectorInHeader(measure, viewport, label)
      expect
        .soft(
          intersects(measure.trigger, scroller),
          `${label}: 触发器 ${JSON.stringify(measure.trigger)} 与页面滚动容器 ${JSON.stringify(scroller)} 相交`,
        )
        .toBe(false)
    })

    test(`320x568 ${route} 上触发器在头部内，标题靠截断让位`, async ({ page }) => {
      const viewport = { width: 320, height: 568 }
      const label = `${route} 320x568`
      // 先换视口再开页（开页助手里才 `goto`），不在已加载的页面上改视口。
      await page.setViewportSize(viewport)
      await open(page)

      const measure = await measureHeaderSelector(page, label)
      const scroller = await measurePageScroller(page, label)
      expect(measure.sponsors, `${label}: 合作单位条不应可见`).toBeNull()
      expectSelectorInHeader(measure, viewport, label)
      expect
        .soft(
          intersects(measure.trigger, scroller),
          `${label}: 触发器 ${JSON.stringify(measure.trigger)} 与页面滚动容器 ${JSON.stringify(scroller)} 相交`,
        )
        .toBe(false)
      // 让位来自 flex 收缩：标题内容比它的可视框宽（被截断），而不是触发器盖在标题上。
      expect
        .soft(measure.titleScroll.scrollWidth, `${label}: 标题 scrollWidth 对 clientWidth ${measure.titleScroll.clientWidth}`)
        .toBeGreaterThan(measure.titleScroll.clientWidth)
    })
  }

  test('1280x400 /ops 上（矮视口，合作单位条可见）触发器在头部内、合作单位条右侧', async ({ page }) => {
    const viewport = { width: 1280, height: 400 }
    const label = '/ops 1280x400'
    await page.setViewportSize(viewport)
    expect(isMobileForm(viewport)).toBe(true)
    await openOpsFallbackPage(page, OPS_FALLBACK_TARGETS[0])

    // 前置：1280 满足 lg，合作单位条可见（否则下面的“右侧”“不相交”无从谈起）。
    await expect(siteHeaderParts(page).sponsors).toBeVisible()
    const measure = await measureHeaderSelector(page, label)
    const scroller = await measurePageScroller(page, label)
    expect(measure.sponsors, `${label}: 合作单位条应可见`).not.toBeNull()
    expectSelectorInHeader(measure, viewport, label)
    expect.soft(inside(measure.sponsors!, measure.header), `${label}: 合作单位条 ${JSON.stringify(measure.sponsors)} 应在头部内`).toBe(true)
    expect
      .soft(measure.trigger.x, `${label}: 触发器左边 ${measure.trigger.x} 应在合作单位条右边之右`)
      .toBeGreaterThanOrEqual(measure.sponsors!.x + measure.sponsors!.width - BOX_TOLERANCE)
    expect.soft(intersects(measure.trigger, scroller), `${label}: 触发器与页面滚动容器相交`).toBe(false)
  })

  test('权限不足页上触发器在头部内，用它切到有权限的角色后放行', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `/ops 拒绝页 ${testInfo.project.name} ${viewport.width}x${viewport.height}`
    // runtime config 钉死 `display_readonly: false`：否则带 `allowDisplayReadonly` 的门禁直接放行 viewer。
    await mockSiteHeaderApi(page)
    await page.goto('/ops')

    await expect(page.getByText(FORBIDDEN)).toBeVisible()
    await expect(page.getByRole('heading', { name: OPS_HEADING })).toHaveCount(0)
    await expect(page.getByLabel('Role')).toHaveText(ROLE_LABELS.viewer)
    expectSelectorInHeader(await measureHeaderSelector(page, label), viewport, label)

    // 从拒绝页切回有权限角色的唯一入口：走真实点击（不带 force）。
    await setRole(page, 'operator')
    await expect(page.getByText(FORBIDDEN)).toHaveCount(0)
    await expect(page.getByRole('heading', { name: OPS_HEADING })).toBeVisible()
    expectSelectorInHeader(await measureHeaderSelector(page, `${label} 放行后`), viewport, `${label} 放行后`)
  })

  test('/ops 上弹层开在头部触发器下方，五个角色在本视口内都点得到', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `/ops ${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await mockSiteHeaderApi(page)
    await page.goto('/ops')
    await expect(page.getByText(FORBIDDEN)).toBeVisible()

    const trigger = page.getByLabel('Role')
    const before = await measureHeaderSelector(page, label)
    expectSelectorInHeader(before, viewport, label)

    // 弹层的方向与五个选项的位置：都在视口内、在头部下方（不盖住触发器自己）。
    await trigger.click()
    const listbox = page.getByRole('listbox')
    await expect(listbox).toBeVisible()
    const options = listbox.getByRole('option')
    await expect(options).toHaveCount(Object.keys(ROLE_LABELS).length)
    const popover = {
      side: await listbox.getAttribute('data-side'),
      align: await listbox.getAttribute('data-align'),
      listbox: await requireBox(listbox, '弹层'),
      options: await Promise.all((await options.all()).map((option) => requireBox(option, '角色选项'))),
    }
    console.log(`role-selector popover @ ${label}`, JSON.stringify(popover))
    expect.soft(popover.side, `${label}: 弹层方向`).toBe('bottom')
    expect.soft(popover.align, `${label}: 弹层对齐`).toBe('end')
    for (const [index, option] of popover.options.entries()) {
      expect
        .soft(
          inside(option, { x: 0, y: 0, width: viewport.width, height: viewport.height }),
          `${label}: 第 ${index + 1} 个选项 ${JSON.stringify(option)} 应在视口内`,
        )
        .toBe(true)
      expect.soft(option.y, `${label}: 第 ${index + 1} 个选项应在头部下方`).toBeGreaterThanOrEqual(before.header.y + before.header.height - BOX_TOLERANCE)
    }
    await page.keyboard.press('Escape')
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')

    // 逆序走一遍，使每一步都是一次真实切换（起始角色是 viewer）；切到无权限角色后页面变回拒绝页属预期。
    for (const role of (Object.keys(ROLE_LABELS) as RoleValue[]).reverse()) {
      await setRole(page, role)
      await expect(trigger).toHaveText(ROLE_LABELS[role])
      await expect(trigger).toHaveAttribute('aria-expanded', 'false')
    }
    await expect(page.getByText(FORBIDDEN)).toBeVisible()
  })

  test('客户端导航 / -> /ops -> 后退：切换器跟着路由换位置（/ops 在头部，/ 在 main 左缘）', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    const label = `${testInfo.project.name} ${viewport.width}x${viewport.height}`
    await mockOpsEntryApi(page)
    await page.goto('/')
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    await setRole(page, 'operator')

    const trigger = page.getByLabel('Role')
    const onMapPage = async (step: string) => {
      const header = await expectHeaderHeight(page, HEADER_MOBILE_HEIGHT, `${label} ${step}`)
      const box = await requireBox(trigger, '角色切换器触发器')
      const main = await requireBox(page.locator('main'), 'main')
      const placement = await trigger.evaluate((element) => ({
        inHeader: element.closest('header') !== null,
        inMain: element.closest('main') !== null,
      }))
      console.log(`role-selector route switch @ ${label} ${step}`, JSON.stringify({ trigger: box, header, main, placement }))
      expect.soft(placement, `${label} ${step}: 触发器应在 main 内、不在头部内`).toEqual({ inHeader: false, inMain: true })
      expect.soft(Math.abs(box.x - main.x), `${label} ${step}: 触发器左边 ${box.x} 应贴 main 左缘 ${main.x}`).toBeLessThanOrEqual(BOX_TOLERANCE)
      expect.soft(intersects(box, header), `${label} ${step}: 触发器 ${JSON.stringify(box)} 与头部相交`).toBe(false)
    }
    await onMapPage('进入 /ops 之前')

    // 取现成入口：启动器列末项的运维入口（客户端导航，没有整页重载）。
    await page.locator(OPS_LINK).tap()
    await expect.poll(() => new URL(page.url()).pathname).toBe('/ops')
    await expect(page.getByRole('heading', { name: OPS_HEADING })).toBeVisible()
    await expect(trigger).toHaveText(ROLE_LABELS.operator)
    expectSelectorInHeader(await measureHeaderSelector(page, `/ops ${label} 路由切换后`), viewport, `/ops ${label} 路由切换后`)

    await page.goBack()
    await expect.poll(() => new URL(page.url()).pathname).toBe('/')
    await expect(page.locator(CONTROL_BAR)).toBeVisible()
    // 没有整页重载：角色仍是 operator。
    await expect(trigger).toHaveText(ROLE_LABELS.operator)
    await onMapPage('后退回 / 之后')
  })
})
