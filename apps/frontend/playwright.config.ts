import { defineConfig, devices } from '@playwright/test'

import { parsePlaywrightWorkers } from './playwright.config.helpers'

export { parsePlaywrightWorkers } from './playwright.config.helpers'

const e2ePort = Number(process.env.PLAYWRIGHT_DEV_PORT ?? 5174)
const externalBaseURL = process.env.PLAYWRIGHT_TEST_BASE_URL
const baseURL = externalBaseURL ?? `http://127.0.0.1:${e2ePort}`
const apiBaseURL = process.env.VITE_API_BASE_URL ?? 'https://api.example.test'
const workers = parsePlaywrightWorkers(process.env.PLAYWRIGHT_WORKERS)

export default defineConfig({
  testDir: './e2e',
  // m15-visual-conformance 是 M15 里程碑的多页视觉/几何证据门（NavBar + 各独立全页布局），
  // 其多页前提在 M26 单图下已不成立；它有独立 runner（pnpm test:e2e:m15-visual）且在 CI 已暂停，
  // 不属于 M26 单图 mocked regression 合同，故与 preview-deeplink/live-display 一样从本门排除。
  testIgnore: [/preview-deeplink\.spec\.ts/, /live-display\.spec\.ts/, /m15-visual-conformance\.spec\.ts/],
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
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
