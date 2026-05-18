import { defineConfig, devices } from '@playwright/test'

export function parsePlaywrightWorkers(value: string | undefined) {
  if (value === undefined || value.trim() === '') return 1
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error('PLAYWRIGHT_WORKERS must be a positive integer.')
  }
  return Math.min(parsed, 4)
}

const e2ePort = Number(process.env.PLAYWRIGHT_DEV_PORT ?? 5174)
const externalBaseURL = process.env.PLAYWRIGHT_TEST_BASE_URL
const baseURL = externalBaseURL ?? `http://127.0.0.1:${e2ePort}`
const apiBaseURL = process.env.VITE_API_BASE_URL ?? 'https://api.example.test'
const workers = parsePlaywrightWorkers(process.env.PLAYWRIGHT_WORKERS)

export default defineConfig({
  testDir: './e2e',
  testIgnore: /preview-deeplink\.spec\.ts/,
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
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
