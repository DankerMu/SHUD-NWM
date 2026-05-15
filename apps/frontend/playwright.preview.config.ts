import { defineConfig, devices } from '@playwright/test'

const previewPort = Number(process.env.PLAYWRIGHT_PREVIEW_PORT ?? 4174)
const baseURL = process.env.PLAYWRIGHT_PREVIEW_BASE_URL ?? `http://127.0.0.1:${previewPort}`
const apiBaseURL = process.env.VITE_API_BASE_URL ?? 'https://api.example.test'

export default defineConfig({
  testDir: './e2e',
  testMatch: /preview-deeplink\.spec\.ts/,
  fullyParallel: false,
  use: {
    baseURL,
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  webServer: {
    command: `VITE_API_BASE_URL=${apiBaseURL} VITE_AUTH_ROLE=operator corepack pnpm build && VITE_API_BASE_URL=${apiBaseURL} VITE_AUTH_ROLE=operator corepack pnpm preview --host 127.0.0.1 --port ${previewPort} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
