import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig, devices } from '@playwright/test'

import {
  assertLiveDisplaySpecsDoNotMockApis,
  liveDisplaySpecPattern,
  type LiveDisplayEnv,
} from './playwright.config.helpers'
import { loadLiveDisplayEnv } from './playwright.config.helpers'

// Config-before-browser env resolution must never preempt the river-click
// spec's own BLOCKED receipt: with missing URLs the live spec publishes one
// REQUIRED_ENV_MISSING receipt before any browser work and then exits nonzero.
// So a missing env here is tolerated into a placeholder; the spec (and the
// /monitoring test's own fail-fast) performs the real classification.
let liveEnv: LiveDisplayEnv
try {
  liveEnv = loadLiveDisplayEnv(process.env)
} catch {
  liveEnv = { baseURL: 'http://127.0.0.1:0', apiBaseURL: '', viteApiBaseURL: '' }
}
process.env.VITE_API_BASE_URL = liveEnv.viteApiBaseURL

assertLiveDisplaySpecsDoNotMockApis(path.join(path.dirname(fileURLToPath(import.meta.url)), 'e2e'))

export default defineConfig({
  testDir: './e2e',
  testMatch: liveDisplaySpecPattern,
  // One REAL pre-browser owner: globalSetup runs BEFORE any browser fixture or
  // worker is launched, invoking the real owner + publisher so config-before-
  // browser decisions (BLOCKED/FAIL receipt or no-receipt) are made first.
  globalSetup: './playwright.live-display.global-setup.ts',
  metadata: {
    evidenceLane: 'live-display-readonly',
    requiredEnv: [
      'PLAYWRIGHT_LIVE_BASE_URL',
      'PLAYWRIGHT_LIVE_API_BASE_URL',
      'PLAYWRIGHT_LIVE_RIVER_BASIN_ID',
      'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID',
      'PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH',
    ],
    runtimeApiEnv: 'VITE_API_BASE_URL',
  },
  fullyParallel: false,
  // The serial river-click P95 metric (#1970) requires exactly one worker and
  // zero retries: retries would discard the 1+20 sample discipline and an
  // extra worker would break the serial per-sample response observation.
  workers: 1,
  retries: 0,
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
