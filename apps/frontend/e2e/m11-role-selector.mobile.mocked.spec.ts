import { expect, test, type Page } from '@playwright/test'

import { MOCK_MINIMAL_DISCHARGE_LAYER } from './support/layerCatalog.mocked'
import { ROLE_LABELS, setRole, type RoleValue } from './support/setRole'
import { isMobileForm, requireViewport } from './support/viewportForm'

/**
 * 开发用角色切换器的移动位置（openspec mobile-responsive-display task 2.5，design.md D5 末段）。
 * 三个移动 project 下都要通过；期望不随“是否矮视口横屏”变化，所以没有形态分支。
 */

type Box = { x: number; y: number; width: number; height: number }

/** 地图浮层现有最高层级（浮动提示 `z-[130]`）与 Radix 弹层 `--z-popover` 之间的开区间。 */
const Z_INDEX_ABOVE_MAP_OVERLAYS = 130
const Z_INDEX_BELOW_POPOVER = 300
const COMPACT_TRIGGER_MAX_WIDTH = 56

function intersects(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
}

function json(data: unknown) {
  return { status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) }
}

/**
 * 本 spec 自带的 API mock。`/ops` 的 `RBACGate` 带 `allowDisplayReadonly`：runtime config 若是只读部署，
 * viewer 会被直接放行，“权限不足 -> 切到 operator 后放行”的 oracle 就落空，所以这里钉死 `display_readonly: false`。
 */
async function mockApi(page: Page, seen: { runtimeConfig: number }) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/runtime/config') {
      seen.runtimeConfig += 1
      return route.fulfill(
        json({
          service_role: 'compute_control',
          control_mutations_enabled: true,
          slurm_routes_enabled: true,
          queue_depth_mode: 'slurm_gateway',
          display_readonly: false,
        }),
      )
    }
    if (url.pathname === '/api/v1/layers') {
      return route.fulfill(json([MOCK_MINIMAL_DISCHARGE_LAYER]))
    }
    return route.fulfill(json([]))
  })
}

test.describe('M11 角色切换器移动位置', () => {
  const seen = { runtimeConfig: 0 }

  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
    seen.runtimeConfig = 0
    await mockApi(page, seen)
  })

  test('/ 上是左缘的紧凑触发器，不被头部、控制条或地图浮层盖住', async ({ page }, testInfo) => {
    const viewport = requireViewport(page)
    await page.goto('/')

    const trigger = page.getByLabel('Role')
    const header = page.locator('header')
    const bar = page.locator('[data-testid="m11-bottom-control-bar"]')
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()
    await expect(header).toHaveCount(1)
    await expect(header).toBeVisible()
    await expect(bar).toBeVisible()
    await expect(trigger).toBeVisible()
    // 紧凑形态只做视觉截断：当前角色的显示名仍在 DOM 里。
    await expect(trigger).toHaveText('Viewer')

    const triggerBox = await trigger.boundingBox()
    const headerBox = await header.boundingBox()
    const barBox = await bar.boundingBox()
    console.log(
      `role-selector boxes @ ${testInfo.project.name} ${viewport.width}x${viewport.height}`,
      JSON.stringify({ trigger: triggerBox, header: headerBox, bar: barBox }),
    )
    expect(triggerBox, '触发器应有可测量的布局矩形').not.toBeNull()
    expect(headerBox, '头部应有可测量的布局矩形').not.toBeNull()
    expect(barBox, '控制条应有可测量的布局矩形').not.toBeNull()

    // 以下几何断言彼此独立，用 soft 断言一次报全（哪几条红一目了然）。
    // 包围盒在视口内。
    expect.soft(triggerBox!.x).toBeGreaterThanOrEqual(0)
    expect.soft(triggerBox!.y).toBeGreaterThanOrEqual(0)
    expect.soft(triggerBox!.x + triggerBox!.width).toBeLessThanOrEqual(viewport.width)
    expect.soft(triggerBox!.y + triggerBox!.height).toBeLessThanOrEqual(viewport.height)
    expect.soft(triggerBox!.width).toBeGreaterThan(0)
    expect.soft(triggerBox!.height).toBeGreaterThan(0)

    expect.soft(triggerBox!.width, `触发器宽 ${triggerBox!.width}`).toBeLessThanOrEqual(COMPACT_TRIGGER_MAX_WIDTH)

    expect.soft(
      intersects(triggerBox!, headerBox!),
      `触发器 ${JSON.stringify(triggerBox)} 与头部 ${JSON.stringify(headerBox)} 相交`,
    ).toBe(false)
    expect.soft(
      intersects(triggerBox!, barBox!),
      `触发器 ${JSON.stringify(triggerBox)} 与控制条 ${JSON.stringify(barBox)} 相交`,
    ).toBe(false)

    // 中心点命中的是触发器自身或其后代（未被地图浮层盖住）。
    const hit = await trigger.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      return {
        onTrigger: top !== null && element.contains(top),
        top: top ? `${top.tagName.toLowerCase()}[data-testid=${top.getAttribute('data-testid')}]` : null,
      }
    })
    expect.soft(hit.onTrigger, `触发器中心被 ${hit.top} 盖住`).toBe(true)

    // 层级：自触发器向上第一个建立层级的祖先（定位容器），其 z-index 在 (130, 300) 内。
    const zIndex = await trigger.evaluate((element) => {
      for (let node: Element | null = element; node && node.tagName !== 'MAIN'; node = node.parentElement) {
        const value = getComputedStyle(node).zIndex
        if (value !== 'auto') return Number(value)
      }
      return null
    })
    expect(zIndex, '触发器到 main 之间没有任何元素设置 z-index').not.toBeNull()
    expect.soft(zIndex!).toBeGreaterThan(Z_INDEX_ABOVE_MAP_OVERLAYS)
    expect.soft(zIndex!).toBeLessThan(Z_INDEX_BELOW_POPOVER)
  })

  test('/ops 上用它切到 operator 后角色生效', async ({ page }) => {
    await page.goto('/ops')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '内部诊断' })).toHaveCount(0)
    // 拒绝来自本 spec 的 runtime config（非只读部署），而不是请求失败后的兜底。
    expect(seen.runtimeConfig).toBeGreaterThan(0)
    await expect(page.getByLabel('Role')).toHaveText('Viewer')

    await setRole(page, 'operator')

    await expect(page.getByText('权限不足')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: '内部诊断' })).toBeVisible()
  })

  test('弹层里的五个角色选项在本视口内都点得到', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('[data-testid="m11-bottom-control-bar"]')).toBeVisible()

    // 逆序走一遍，使每一步都是一次真实切换（起始角色是 viewer）。
    for (const role of (Object.keys(ROLE_LABELS) as RoleValue[]).reverse()) {
      await setRole(page, role)
      // 选中后弹层收起（页面上别的原生 <select> 也有 option，所以看触发器自己的展开状态）。
      await expect(page.getByLabel('Role')).toHaveAttribute('aria-expanded', 'false')
    }
  })
})
