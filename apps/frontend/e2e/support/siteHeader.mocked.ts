import { expect, type Locator, type Page } from '@playwright/test'

import { MOCK_MINIMAL_DISCHARGE_LAYER } from './layerCatalog.mocked'
import { setRole, type RoleValue } from './setRole'

/**
 * 头部两个 spec（openspec mobile-responsive-display task 2.4，design.md D4）共用的 mock、选择器与量测。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 */

export const HEADER_MOBILE_HEIGHT = 48
export const HEADER_DESKTOP_HEIGHT = 84
/** 包围盒是子像素量，高度与顶边都按 ±0.5 比较。 */
export const BOX_TOLERANCE = 0.5

export type Box = { x: number; y: number; width: number; height: number }

/**
 * 只 mock 到“授权后的页面标题能渲染”为止。
 * runtime config 钉死 `display_readonly: false`：只读部署下带 `allowDisplayReadonly` 的 `RBACGate`
 * 会直接放行 viewer，“权限不足 -> 切角色后放行”的 oracle 就落空。
 */
export async function mockSiteHeaderApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    let data: unknown = []
    if (url.pathname === '/api/v1/runtime/config') {
      data = {
        service_role: 'compute_control',
        control_mutations_enabled: true,
        slurm_routes_enabled: true,
        queue_depth_mode: 'slurm_gateway',
        display_readonly: false,
      }
    } else if (url.pathname === '/api/v1/layers') {
      data = [MOCK_MINIMAL_DISCHARGE_LAYER]
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
  })
}

export function siteHeaderParts(page: Page) {
  const header = page.locator('header')
  return {
    header,
    logo: header.getByAltText('全国水文模拟系统徽标'),
    title: header.getByText('全国水文模拟系统（V2.0）', { exact: true }),
    subtitle: header.getByText('National Water Modeling', { exact: true }),
    sponsors: header.getByAltText('合作单位'),
  }
}

/**
 * 顺序钉死：auth store 不持久化，`goto` 会把角色复位成 viewer，
 * 所以先 `goto`、看到「权限不足」、再切角色，等到授权后的页面标题才算到位。
 */
export async function openAuthorizedRoute(page: Page, path: string, role: RoleValue, heading: string) {
  await page.goto(path)
  await expect(page.getByText('权限不足')).toBeVisible()
  await setRole(page, role)
  await expect(page.getByText('权限不足')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: heading })).toBeVisible()
}

export async function requireBox(locator: Locator, name: string): Promise<Box> {
  const box = await locator.boundingBox()
  if (!box) throw new Error(`${name} 没有可测量的布局矩形`)
  return box
}

export async function titleStyle(title: Locator) {
  return title.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      fontSize: Number.parseFloat(style.fontSize),
      lineHeight: Number.parseFloat(style.lineHeight),
      whiteSpace: style.whiteSpace,
    }
  })
}

/** 头部恰一个、可见、贴顶、高度为 `height`（±0.5）；返回其包围盒。 */
export async function expectHeaderHeight(page: Page, height: number, label: string): Promise<Box> {
  const { header } = siteHeaderParts(page)
  await expect(header).toHaveCount(1)
  await expect(header).toBeVisible()
  // 视口刚变时布局可能还没落定，轮询到位后再取最终值。
  await expect
    .poll(async () => Math.abs((await requireBox(header, '头部')).height - height), { message: `${label}: 头部高应为 ${height}` })
    .toBeLessThanOrEqual(BOX_TOLERANCE)
  const box = await requireBox(header, '头部')
  expect(Math.abs(box.y), `${label}: 头部顶边 y=${box.y}`).toBeLessThanOrEqual(BOX_TOLERANCE)
  return box
}

/** `inner` 的包围盒完全在 `outer` 内（±0.5）。 */
export function expectInside(inner: Box, outer: Box, label: string) {
  const detail = `${label}: ${JSON.stringify(inner)} 应在 ${JSON.stringify(outer)} 内`
  expect(inner.x, detail).toBeGreaterThanOrEqual(outer.x - BOX_TOLERANCE)
  expect(inner.y, detail).toBeGreaterThanOrEqual(outer.y - BOX_TOLERANCE)
  expect(inner.x + inner.width, detail).toBeLessThanOrEqual(outer.x + outer.width + BOX_TOLERANCE)
  expect(inner.y + inner.height, detail).toBeLessThanOrEqual(outer.y + outer.height + BOX_TOLERANCE)
}
