import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig, devices } from '@playwright/test'

import { assertC4LiveSpecsDoNotMockApis, c4LiveSpecPattern } from './playwright.config.helpers'
import { loadLiveDisplayEnv, type LiveDisplayEnv } from './playwright.config.helpers'

let liveEnv: LiveDisplayEnv
try {
  liveEnv = loadLiveDisplayEnv(process.env)
} catch {
  liveEnv = { baseURL: 'http://127.0.0.1:0', apiBaseURL: '', viteApiBaseURL: '' }
}
process.env.VITE_API_BASE_URL = liveEnv.viteApiBaseURL

assertC4LiveSpecsDoNotMockApis(path.join(path.dirname(fileURLToPath(import.meta.url)), 'e2e'))

export default defineConfig({
  testDir: './e2e',
  testMatch: c4LiveSpecPattern,
  grep: /@live-c4-display/,
  globalSetup: './playwright.live-c4-display.global-setup.ts',
  timeout: 210_000,
  metadata: {
    evidenceLane: 'live-c4-display',
    requiredEnv: [
      'PLAYWRIGHT_LIVE_BASE_URL',
      'PLAYWRIGHT_LIVE_API_BASE_URL',
      'PLAYWRIGHT_LIVE_C4_BASIN_ID',
      'PLAYWRIGHT_LIVE_C4_SEGMENT_ID',
      'PLAYWRIGHT_LIVE_C4_RECEIPT_PATH',
    ],
    runtimeApiEnv: 'VITE_API_BASE_URL',
  },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: liveEnv.baseURL,
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  projects: [
    {
      name: 'live-c4-display-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
