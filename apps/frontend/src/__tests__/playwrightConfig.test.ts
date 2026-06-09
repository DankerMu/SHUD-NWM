import { describe, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  assertLiveDisplayPageEvidence,
  assertLiveDisplaySpecsDoNotMockApis,
  classifyLiveDisplayControlRequest,
  findLiveDisplaySpecFiles,
  isLiveDisplayReadApiUrl,
  isLiveDisplayRuntimeConfigUrl,
  liveDisplayApiBinding,
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
    const projectNames = config.default.projects?.map((project: { name: string }) => project.name)

    expect(projectNames).toEqual(['mocked-regression-chromium'])
    expect(projectNames).not.toContain('chromium')
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

  it('classifies live display API binding for distinct API and same-origin proxy cases', () => {
    const distinct = liveDisplayApiBinding('http://127.0.0.1:4174', 'http://127.0.0.1:8000')
    expect(distinct).toEqual({ mode: 'distinct-api', expectedOrigin: 'http://127.0.0.1:8000' })
    expect(isLiveDisplayRuntimeConfigUrl('http://127.0.0.1:8000/api/v1/runtime/config', distinct)).toBe(true)
    expect(isLiveDisplayReadApiUrl('http://127.0.0.1:8000/api/v1/pipeline/status?source=GFS', distinct)).toBe(true)
    expect(isLiveDisplayRuntimeConfigUrl('http://127.0.0.1:4174/api/v1/runtime/config', distinct)).toBe(false)

    const sameOrigin = liveDisplayApiBinding('https://display.example.test/app', 'https://display.example.test')
    expect(sameOrigin).toEqual({ mode: 'same-origin-proxy', expectedOrigin: 'https://display.example.test' })
    expect(isLiveDisplayRuntimeConfigUrl('https://display.example.test/api/v1/runtime/config', sameOrigin)).toBe(true)
    expect(isLiveDisplayReadApiUrl('https://display.example.test/api/v1/jobs?limit=12', sameOrigin)).toBe(true)
  })

  it('classifies forbidden live display control-plane browser requests', () => {
    expect(classifyLiveDisplayControlRequest('GET', 'https://api.example.test/api/v1/slurm/jobs')).toBe(
      'forbidden-slurm-control',
    )
    expect(classifyLiveDisplayControlRequest('POST', 'https://api.example.test/api/v1/slurm/jobs')).toBe(
      'forbidden-slurm-control',
    )
    expect(classifyLiveDisplayControlRequest('POST', 'https://api.example.test/api/v1/runs/run-1/retry')).toBe(
      'forbidden-run-mutation',
    )
    expect(classifyLiveDisplayControlRequest('DELETE', 'https://api.example.test/api/v1/runs/run-1/cancel')).toBe(
      'forbidden-run-mutation',
    )
    expect(classifyLiveDisplayControlRequest('GET', 'https://api.example.test/api/v1/runs/run-1/retry')).toBeNull()
    expect(classifyLiveDisplayControlRequest('GET', 'https://api.example.test/api/v1/pipeline/status')).toBeNull()
  })

  it('requires browser-observed display runtime config and live read API evidence', () => {
    expect(() =>
      assertLiveDisplayPageEvidence({
        runtimeConfigResponses: [
          {
            url: 'https://api.example.test/api/v1/runtime/config',
            status: 200,
            body: { data: { service_role: 'display_readonly', display_readonly: true } },
          },
        ],
        readApiResponses: [
          {
            url: 'https://api.example.test/api/v1/pipeline/status?source=GFS',
            status: 200,
            body: { data: { source: 'GFS' } },
          },
        ],
        forbiddenControlRequests: [],
        permissionDeniedVisible: false,
        runtimeConfigUnavailableVisible: false,
      }),
    ).not.toThrow()

    expect(() =>
      assertLiveDisplayPageEvidence({
        runtimeConfigResponses: [],
        readApiResponses: [
          {
            url: 'https://api.example.test/api/v1/pipeline/status?source=GFS',
            status: 200,
            body: { data: { source: 'GFS' } },
          },
        ],
        forbiddenControlRequests: [],
        permissionDeniedVisible: false,
        runtimeConfigUnavailableVisible: false,
      }),
    ).toThrow(/browser-observed \/api\/v1\/runtime\/config response with display_readonly/)

    expect(() =>
      assertLiveDisplayPageEvidence({
        runtimeConfigResponses: [
          {
            url: 'https://api.example.test/api/v1/runtime/config',
            status: 200,
            body: { data: { service_role: 'display_readonly', display_readonly: true } },
          },
        ],
        readApiResponses: [],
        forbiddenControlRequests: [],
        permissionDeniedVisible: false,
        runtimeConfigUnavailableVisible: false,
      }),
    ).toThrow(/successful browser-observed monitoring read API/)
  })

  it('does not count denied or unavailable page state as live PASS evidence', () => {
    const passingResponses = {
      runtimeConfigResponses: [
        {
          url: 'https://api.example.test/api/v1/runtime/config',
          status: 200,
          body: { data: { service_role: 'display_readonly', display_readonly: true } },
        },
      ],
      readApiResponses: [
        {
          url: 'https://api.example.test/api/v1/jobs?limit=12',
          status: 200,
          body: { data: { items: [] } },
        },
      ],
      forbiddenControlRequests: [],
    }

    expect(() =>
      assertLiveDisplayPageEvidence({
        ...passingResponses,
        permissionDeniedVisible: true,
        runtimeConfigUnavailableVisible: false,
      }),
    ).toThrow(/RBAC 权限不足/)
    expect(() =>
      assertLiveDisplayPageEvidence({
        ...passingResponses,
        permissionDeniedVisible: false,
        runtimeConfigUnavailableVisible: true,
      }),
    ).toThrow(/runtime config is unavailable/)
    expect(() =>
      assertLiveDisplayPageEvidence({
        ...passingResponses,
        forbiddenControlRequests: ['GET /api/v1/slurm/jobs (forbidden-slurm-control)'],
        permissionDeniedVisible: false,
        runtimeConfigUnavailableVisible: false,
      }),
    ).toThrow(/forbidden control requests/)
  })
})
