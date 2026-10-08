import { defineConfig, devices } from '@playwright/test'

import { parsePlaywrightWorkers } from './playwright.config.helpers'

export { parsePlaywrightWorkers } from './playwright.config.helpers'

const e2ePort = Number(process.env.PLAYWRIGHT_DEV_PORT ?? 5174)
const externalBaseURL = process.env.PLAYWRIGHT_TEST_BASE_URL
const baseURL = externalBaseURL ?? `http://127.0.0.1:${e2ePort}`
const apiBaseURL = process.env.VITE_API_BASE_URL ?? 'https://api.example.test'
const workers = parsePlaywrightWorkers(process.env.PLAYWRIGHT_WORKERS)

// m15-visual-conformance 是 M15 里程碑的多页视觉/几何证据门（NavBar + 各独立全页布局），
// 其多页前提在 M26 单图下已不成立；它有独立 runner（pnpm test:e2e:m15-visual）且在 CI 已暂停，
// 不属于 M26 单图 mocked regression 合同，故与 preview-deeplink/live-display 一样从本门排除。
const excludedFromLane = [
  /preview-deeplink\.spec\.ts/,
  /live-display\.spec\.ts/,
  /live-c4-display\.spec\.ts/,
  /m15-visual-conformance\.spec\.ts/,
]
// 文件名含 `.mobile.` 的 spec 只在三个移动 project 下执行；其余 spec 只在桌面 project 下执行。
const mobileSpec = /\.mobile\./

// 移动 project 不展开 devices[...] 预设：手机预设默认 WebKit，而 CI 只装 Chromium。
function mobileProject(name: string, width: number, height: number) {
  return {
    name,
    testMatch: mobileSpec,
    use: { browserName: 'chromium' as const, viewport: { width, height }, isMobile: true, hasTouch: true },
  }
}

export default defineConfig({
  testDir: './e2e',
  testIgnore: excludedFromLane,
  metadata: {
    evidenceLane: 'mocked-regression',
    broadApiMocks: 'allowed-in-mocked-regression-only',
  },
  fullyParallel: true,
  workers,
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  ...(externalBaseURL
    ? {}
    : {
        webServer: {
          command: `VITE_API_BASE_URL=${apiBaseURL} VITE_ENABLE_ROLE_OVERRIDE=true VITE_AUTH_ROLE=viewer corepack pnpm dev --host 127.0.0.1 --port ${e2ePort} --strictPort`,
          url: baseURL,
          reuseExistingServer: false,
        },
      }),
  projects: [
    {
      name: 'mocked-regression-chromium',
      // project 级 testIgnore 会替换（而非合并）顶层 testIgnore，所以这里必须把顶层四项重列一遍。
      testIgnore: [...excludedFromLane, mobileSpec],
      use: { ...devices['Desktop Chrome'] },
    },
    mobileProject('mobile-portrait', 390, 664),
    mobileProject('mobile-landscape', 750, 342),
    // 宽 ≥ 768，只靠高度条件进入移动形态。
    mobileProject('mobile-landscape-wide', 844, 390),
  ],
})
