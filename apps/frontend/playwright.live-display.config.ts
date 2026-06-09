import { defineConfig, devices } from '@playwright/test'

import {
  assertLiveDisplaySpecsDoNotMockApis,
  loadLiveDisplayEnv,
  parsePlaywrightWorkers,
} from './playwright.config.helpers'

const liveEnv = loadLiveDisplayEnv(process.env)
process.env.VITE_API_BASE_URL = liveEnv.viteApiBaseURL

assertLiveDisplaySpecsDoNotMockApis(new URL('./e2e', import.meta.url))

export default defineConfig({
  testDir: './e2e',
  testMatch: /live-display\.spec\.ts/,
  metadata: {
    evidenceLane: 'live-display-readonly',
    requiredEnv: ['PLAYWRIGHT_LIVE_BASE_URL', 'PLAYWRIGHT_LIVE_API_BASE_URL'],
    runtimeApiEnv: 'VITE_API_BASE_URL',
  },
  fullyParallel: false,
  workers: parsePlaywrightWorkers(process.env.PLAYWRIGHT_WORKERS),
  use: {
    baseURL: liveEnv.baseURL,
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  projects: [
    {
      name: 'live-display-readonly-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
