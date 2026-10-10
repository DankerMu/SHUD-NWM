import type { Page } from '@playwright/test'

import { MOCK_LAYERS } from './layerCatalog.mocked'

/**
 * 运维入口 spec（openspec mobile-responsive-display task 3.5，design.md D8）的 API mock。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 *
 * 一个助手同时覆盖 `/` 的启动器与 `/ops` 页面：`page.route` 后注册者优先、两个宽路由不会合并，
 * 所以不能把别的助手叠在它上面。
 * - runtime config 钉死 `display_readonly: false`：只读部署下 `/ops` 带 `allowDisplayReadonly` 的
 *   `RBACGate` 会直接放行 viewer，“operator 才进得去”的 oracle 就落空。
 * - 图层目录 = 径流 + 降水两条（同图例启动器 spec 的目录），图层面板与图例面板的内容高度因此与
 *   既有移动 spec 相同，量到的滚动数字可以直接对照。
 */

const MOCK_RUNTIME_CONFIG = {
  service_role: 'compute_control',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
}

export async function mockOpsEntryApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    let data: unknown = []
    if (url.pathname === '/api/v1/runtime/config') data = MOCK_RUNTIME_CONFIG
    else if (url.pathname === '/api/v1/layers') data = MOCK_LAYERS
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
  })
}
