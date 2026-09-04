import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig, devices } from '@playwright/test'

import {
  assertLiveDisplaySpecsDoNotMockApis,
  liveDisplaySpecPattern,
  loadLiveDisplayEnv,
  parsePlaywrightWorkers,
} from './playwright.config.helpers'

const liveEnv = loadLiveDisplayEnv(process.env)
process.env.VITE_API_BASE_URL = liveEnv.viteApiBaseURL

assertLiveDisplaySpecsDoNotMockApis(path.join(path.dirname(fileURLToPath(import.meta.url)), 'e2e'))

export default defineConfig({
  testDir: './e2e',
  testMatch: liveDisplaySpecPattern,
  // Legacy two-URL `/monitoring` lane: river-click env/globalSetup must not abort
  // a monitoring-only invocation. The dedicated river-click profile owns the
  // five-key pre-browser owner.
  grep: /@live-monitoring/,
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
