import { describe, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  assertLiveDisplaySpecsDoNotMockApis,
  isPlaywrightProjectRequested,
  loadLiveDisplayEnv,
  parsePlaywrightWorkers,
} from '../../playwright.config.helpers'

describe('Playwright config helpers', () => {
  it('uses bounded deterministic worker counts', () => {
    expect(parsePlaywrightWorkers(undefined)).toBe(1)
    expect(parsePlaywrightWorkers('3')).toBe(3)
    expect(parsePlaywrightWorkers('999')).toBe(4)
  })

  it('fails clearly for invalid worker counts', () => {
    expect(() => parsePlaywrightWorkers('0')).toThrow('PLAYWRIGHT_WORKERS must be a positive integer.')
    expect(() => parsePlaywrightWorkers('abc')).toThrow('PLAYWRIGHT_WORKERS must be a positive integer.')
  })

  it('keeps a legacy chromium project alias only when explicitly requested', () => {
    expect(isPlaywrightProjectRequested('chromium', ['node', 'playwright', '--list'])).toBe(false)
    expect(isPlaywrightProjectRequested('chromium', ['node', 'playwright', '--project=chromium'])).toBe(true)
    expect(isPlaywrightProjectRequested('chromium', ['node', 'playwright', '--project', 'chromium'])).toBe(true)
    expect(isPlaywrightProjectRequested('chromium', ['node', 'playwright', '--project=mocked-regression-chromium'])).toBe(false)
  })

  it('requires explicit live display frontend and API URLs', () => {
    expect(() => loadLiveDisplayEnv({})).toThrow(
      /Live display Playwright profile BLOCKED: missing PLAYWRIGHT_LIVE_BASE_URL, PLAYWRIGHT_LIVE_API_BASE_URL/,
    )
    expect(() =>
      loadLiveDisplayEnv({
        PLAYWRIGHT_LIVE_BASE_URL: 'http://display.example.test',
      }),
    ).toThrow(/missing PLAYWRIGHT_LIVE_API_BASE_URL/)
    expect(() =>
      loadLiveDisplayEnv({
        PLAYWRIGHT_LIVE_BASE_URL: 'file:///tmp/display',
        PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
      }),
    ).toThrow(/PLAYWRIGHT_LIVE_BASE_URL must use http or https/)

    expect(
      loadLiveDisplayEnv({
        PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
        PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
      }),
    ).toEqual({
      baseURL: 'https://display.example.test',
      apiBaseURL: 'https://api.example.test',
      viteApiBaseURL: 'https://api.example.test',
    })
  })

  it('fails the live display guard for broad API route mocks only in live specs', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-live-display-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      mkdirSync(e2eDir)
      writeFileSync(
        path.join(e2eDir, 'monitoring.spec.ts'),
        "await page.route('**/api/v1/**', async () => undefined)\n",
      )
      writeFileSync(path.join(e2eDir, 'live-display.spec.ts'), "await page.goto('/monitoring')\n")

      expect(assertLiveDisplaySpecsDoNotMockApis(e2eDir)).toEqual([path.join(e2eDir, 'live-display.spec.ts')])

      writeFileSync(
        path.join(e2eDir, 'live-display.spec.ts'),
        "await page.route(\n  '**/api/v1/**',\n  async () => undefined,\n)\n",
      )
      expect(() => assertLiveDisplaySpecsDoNotMockApis(e2eDir)).toThrow(
        /live display_readonly Playwright specs cannot register broad page\.route\('\*\*\/api\/v1\/\*\*'\) API mocks/,
      )
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
