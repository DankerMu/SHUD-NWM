import { defineConfig, devices } from '@playwright/test'

const e2ePort = Number(process.env.PLAYWRIGHT_DEV_PORT ?? 5174)
const externalBaseURL = process.env.PLAYWRIGHT_TEST_BASE_URL
const baseURL = externalBaseURL ?? `http://127.0.0.1:${e2ePort}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  ...(externalBaseURL
    ? {}
    : {
        webServer: {
          command: `VITE_ENABLE_ROLE_OVERRIDE=true VITE_AUTH_ROLE=viewer corepack pnpm dev --host 127.0.0.1 --port ${e2ePort} --strictPort`,
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
