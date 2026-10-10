import { defineConfig, devices } from '@playwright/test'

import { baseURL, webServerConfig } from './playwright.config'

// M15 视觉证据 spec 的独立 runner（pnpm test:e2e:m15-visual）：默认配置的每个 project 都排除该 spec，
// 所以它只能经这份配置运行。baseURL 与 webServer（含 PLAYWRIGHT_TEST_BASE_URL 分支）直接取默认配置的导出，
// 不另写一份。
export default defineConfig({
  testDir: './e2e',
  testMatch: /m15-visual-conformance\.spec\.ts/,
  fullyParallel: false,
  use: {
    baseURL,
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  ...webServerConfig,
})
