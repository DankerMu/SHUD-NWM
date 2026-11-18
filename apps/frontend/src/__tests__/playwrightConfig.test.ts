import { describe, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  assertLiveDisplaySpecsDoNotMockApis,
  findLiveDisplaySpecFiles,
  isLiveDisplaySpecFile,
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

  it('keeps the default mocked regression project explicitly named', async () => {
    const config = await import('../../playwright.config')
    const projectNames = config.default.projects?.map((project) => project.name)

    expect(projectNames).toEqual([
      'mocked-regression-chromium',
      'mobile-portrait',
      'mobile-landscape',
      'mobile-landscape-wide',
    ])
    expect(projectNames).not.toContain('chromium')
    expect(config.default.metadata).toMatchObject({
      evidenceLane: 'mocked-regression',
      broadApiMocks: 'allowed-in-mocked-regression-only',
    })
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
    expect(() =>
      loadLiveDisplayEnv({
        PLAYWRIGHT_LIVE_BASE_URL: 'https://user:pass@display.example.test',
        PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
      }),
    ).toThrow(/PLAYWRIGHT_LIVE_BASE_URL must not include username\/password userinfo/)
    expect(() =>
      loadLiveDisplayEnv({
        PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
        PLAYWRIGHT_LIVE_API_BASE_URL: 'https://user:pass@api.example.test',
      }),
    ).toThrow(/PLAYWRIGHT_LIVE_API_BASE_URL must not include username\/password userinfo/)

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

  it('uses the same exact live spec matcher for config and static guard discovery', () => {
    expect(isLiveDisplaySpecFile('/repo/apps/frontend/e2e/live-display.spec.ts')).toBe(true)
    expect(isLiveDisplaySpecFile('/repo/apps/frontend/e2e/xlive-display.spec.ts')).toBe(false)
    expect(isLiveDisplaySpecFile('/repo/apps/frontend/e2e/nested/live-display.spec.ts')).toBe(true)
    expect(isLiveDisplaySpecFile('/repo/apps/frontend/e2e/m11-routes.mocked.spec.ts')).toBe(false)
    expect(isLiveDisplaySpecFile('/repo/apps/frontend/e2e/monitoring.mocked.spec.ts')).toBe(false)
  })

  it('keeps mocked regression specs outside live display discovery', () => {
    const e2eDir = path.resolve('e2e')

    expect(findLiveDisplaySpecFiles(e2eDir)).toEqual([path.join(e2eDir, 'live-display.spec.ts')])
  })

  it('fails the live display guard for broad API route mocks only in live specs', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-live-display-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      mkdirSync(e2eDir)
      writeFileSync(
        path.join(e2eDir, 'monitoring.mocked.spec.ts'),
        "await page.route('**/api/v1/**', async () => undefined)\n",
      )
      writeFileSync(
        path.join(e2eDir, 'xlive-display.spec.ts'),
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

  it('does not follow symlinks outside the e2e root during live spec discovery', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-live-display-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      const outsideDir = path.join(root, 'outside')
      mkdirSync(e2eDir)
      mkdirSync(outsideDir)
      writeFileSync(path.join(outsideDir, 'live-display.spec.ts'), "await page.route('**/api/v1/**')\n")
      symlinkSync(outsideDir, path.join(e2eDir, 'outside-link'), 'dir')

      expect(findLiveDisplaySpecFiles(e2eDir)).toEqual([])
      expect(assertLiveDisplaySpecsDoNotMockApis(e2eDir)).toEqual([])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('does not traverse symlink cycles or self-references during live spec discovery', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-live-display-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      const nestedDir = path.join(e2eDir, 'nested')
      mkdirSync(nestedDir, { recursive: true })
      writeFileSync(path.join(e2eDir, 'live-display.spec.ts'), "await page.goto('/monitoring')\n")
      symlinkSync(e2eDir, path.join(nestedDir, 'cycle'), 'dir')
      symlinkSync(nestedDir, path.join(nestedDir, 'self'), 'dir')

      expect(findLiveDisplaySpecFiles(e2eDir)).toEqual([path.join(e2eDir, 'live-display.spec.ts')])
      expect(assertLiveDisplaySpecsDoNotMockApis(e2eDir)).toEqual([path.join(e2eDir, 'live-display.spec.ts')])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
