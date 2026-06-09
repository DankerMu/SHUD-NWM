import { defineConfig, devices } from '@playwright/test'

import { isPlaywrightProjectRequested, parsePlaywrightWorkers } from './playwright.config.helpers'

export { isPlaywrightProjectRequested, parsePlaywrightWorkers } from './playwright.config.helpers'

const e2ePort = Number(process.env.PLAYWRIGHT_DEV_PORT ?? 5174)
const externalBaseURL = process.env.PLAYWRIGHT_TEST_BASE_URL
const baseURL = externalBaseURL ?? `http://127.0.0.1:${e2ePort}`
const apiBaseURL = process.env.VITE_API_BASE_URL ?? 'https://api.example.test'
const workers = parsePlaywrightWorkers(process.env.PLAYWRIGHT_WORKERS)
const projectName = isPlaywrightProjectRequested('chromium') ? 'chromium' : 'mocked-regression-chromium'

export default defineConfig({
  testDir: './e2e',
  testIgnore: [/preview-deeplink\.spec\.ts/, /live-display\.spec\.ts/],
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
      name: projectName,
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
