import { describe, expect, it, vi } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'

import { assertC4LiveSpecsDoNotMockApis, isC4LiveSpecFile } from '../../playwright.config.helpers'

describe('C4 live display profile separation', () => {
  it('keeps the dedicated C4 command/profile separate from monitoring and river-click', async () => {
    const pkg = JSON.parse(readFileSync(path.resolve(__dirname, '../../package.json'), 'utf8'))
    expect(pkg.scripts['test:e2e:live-display']).toMatch(/playwright-live-display\.sh/)
    expect(pkg.scripts['test:e2e:live-display']).not.toMatch(/c4/)
    expect(pkg.scripts['test:e2e:live-river-click']).toMatch(/playwright-live-river-click\.sh/)
    expect(pkg.scripts['test:e2e:live-c4-display']).toMatch(/playwright-live-c4-display\.sh/)

    const monitoringConfig = readFileSync(path.resolve(__dirname, '../../playwright.live-display.config.ts'), 'utf8')
    expect(monitoringConfig).toMatch(/grep: \/@live-monitoring\//)
    expect(monitoringConfig).not.toMatch(/live-c4-display/)
    expect(monitoringConfig).not.toMatch(/playwright\.live-display\.global-setup/)

    const riverConfig = readFileSync(path.resolve(__dirname, '../../playwright.live-river-click.config.ts'), 'utf8')
    expect(riverConfig).toMatch(/grep: \/@live-river-click\//)
    expect(riverConfig).not.toMatch(/@live-c4-display/)
    expect(riverConfig).toMatch(/playwright\.live-display\.global-setup/)

    const c4Config = readFileSync(path.resolve(__dirname, '../../playwright.live-c4-display.config.ts'), 'utf8')
    expect(c4Config).toMatch(/grep: \/@live-c4-display\//)
    expect(c4Config).toMatch(/workers:\s*1/)
    expect(c4Config).toMatch(/retries:\s*0/)
    expect(c4Config).toMatch(/playwright\.live-c4-display\.global-setup/)
    expect(c4Config).not.toMatch(/playwright\.live-display\.global-setup/)
    expect(c4Config).not.toMatch(/@live-monitoring/)
    expect(c4Config).not.toMatch(/@live-river-click/)
  })

  it('uses a dedicated C4 spec matcher and scans direct C4 routing APIs', () => {
    expect(isC4LiveSpecFile('/repo/apps/frontend/e2e/live-c4-display.spec.ts')).toBe(true)
    expect(isC4LiveSpecFile('/repo/apps/frontend/e2e/live-display.spec.ts')).toBe(false)
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-c4-live-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      mkdirSync(e2eDir)
      writeFileSync(path.join(e2eDir, 'live-c4-display.spec.ts'), "await page.route('**/api/v1/jobs', async () => {})\n")
      expect(() => assertC4LiveSpecsDoNotMockApis(e2eDir)).toThrow(/direct page\/context route or routeFromHAR APIs/)
      writeFileSync(path.join(e2eDir, 'live-c4-display.spec.ts'), "await context.routeFromHAR('./capture.har')\n")
      expect(() => assertC4LiveSpecsDoNotMockApis(e2eDir)).toThrow(/routeFromHAR/)
      writeFileSync(path.join(e2eDir, 'live-c4-display.spec.ts'), "await page['routeFromHAR']('./capture.har')\n")
      expect(() => assertC4LiveSpecsDoNotMockApis(e2eDir)).toThrow(/routeFromHAR/)
      writeFileSync(path.join(e2eDir, 'live-c4-display.spec.ts'), "await page.goto('/')\n")
      expect(assertC4LiveSpecsDoNotMockApis(e2eDir)).toEqual([path.join(e2eDir, 'live-c4-display.spec.ts')])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('lists the five required C4 env keys and does not abort monitoring-only config import', async () => {
    const previous = { ...process.env }
    vi.resetModules()
    try {
      process.env.PLAYWRIGHT_LIVE_BASE_URL = 'https://display.example.test'
      process.env.PLAYWRIGHT_LIVE_API_BASE_URL = 'https://api.example.test'
      delete process.env.PLAYWRIGHT_LIVE_C4_BASIN_ID
      delete process.env.PLAYWRIGHT_LIVE_C4_SEGMENT_ID
      delete process.env.PLAYWRIGHT_LIVE_C4_RECEIPT_PATH
      const monitoring = await import('../../playwright.live-display.config')
      expect(monitoring.default.globalSetup).toBeUndefined()
      expect(monitoring.default.grep?.toString()).toContain('live-monitoring')

      const c4 = await import('../../playwright.live-c4-display.config')
      expect(c4.default.workers).toBe(1)
      expect(c4.default.retries).toBe(0)
      expect(c4.default.projects?.map((project) => project.name)).toEqual(['live-c4-display-chromium'])
      expect(c4.default.metadata?.requiredEnv).toEqual([
        'PLAYWRIGHT_LIVE_BASE_URL',
        'PLAYWRIGHT_LIVE_API_BASE_URL',
        'PLAYWRIGHT_LIVE_C4_BASIN_ID',
        'PLAYWRIGHT_LIVE_C4_SEGMENT_ID',
        'PLAYWRIGHT_LIVE_C4_RECEIPT_PATH',
      ])
    } finally {
      process.env = previous
    }
  })

  it('Playwright C4 profile lists the C4 test and the monitoring/river profiles do not', () => {
    const frontendDir = path.resolve(__dirname, '../..')
    const { chmodSync, mkdtempSync, realpathSync, rmSync } = require('node:fs') as typeof import('node:fs')
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-c4-list-')))
    chmodSync(parent, 0o700)
    const c4Receipt = path.join(parent, 'nhms-frontend-c4-live-evidence-list.json')
    const riverReceipt = path.join(parent, 'nhms-frontend-river-click-live-evidence-list.json')
    const list = (config: string) => {
      const env = { ...process.env }
      for (const key of [
        'VITE_AUTH_ROLE',
        'VITE_ENABLE_ROLE_OVERRIDE',
        'PLAYWRIGHT_LIVE_RIVER_RUN_ID',
        'PLAYWRIGHT_LIVE_RIVER_MODEL_ID',
        'PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID',
        'PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID',
        'PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME',
        'PLAYWRIGHT_LIVE_RIVER_SCENARIO',
      ]) {
        delete env[key]
      }
      return execFileSync('corepack', [
        'pnpm@10.11.0', '--dir', frontendDir, 'exec', 'playwright', 'test',
        '--config', config,
        '--list',
      ], {
        cwd: frontendDir,
        encoding: 'utf8',
        env: {
          ...env,
          PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
          PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
          PLAYWRIGHT_LIVE_C4_BASIN_ID: 'basins_qhh',
          PLAYWRIGHT_LIVE_C4_SEGMENT_ID: 'seg-001',
          PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: c4Receipt,
          PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
          PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
          PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: riverReceipt,
        },
      })
    }
    try {
      const c4 = list('playwright.live-c4-display.config.ts')
      expect(c4).toMatch(/@live-c4-display/)
      expect(c4).not.toMatch(/@live-monitoring/)
      expect(c4).not.toMatch(/@live-river-click/)

      const monitoring = list('playwright.live-display.config.ts')
      expect(monitoring).toMatch(/@live-monitoring/)
      expect(monitoring).not.toMatch(/@live-c4-display/)

      const river = list('playwright.live-river-click.config.ts')
      expect(river).toMatch(/@live-river-click/)
      expect(river).not.toMatch(/@live-c4-display/)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  }, 120_000)

  it('keeps ordinary C4 Vitest independent of Playwright browser binaries', () => {
    const laneTest = readFileSync(path.resolve(__dirname, './c4DisplayLane.test.tsx'), 'utf8')
    expect(laneTest).not.toMatch(/chromium\.launch/)
    expect(laneTest).toMatch(/fn\.toString\(\)/)
    expect(laneTest).toMatch(/runInContext/)
  })

  it('C4 live spec never sets a role override and publishes exactly once', () => {
    const spec = readFileSync(path.resolve(__dirname, '../../e2e/live-c4-display.spec.ts'), 'utf8')
    expect(spec).not.toMatch(/VITE_AUTH_ROLE/)
    expect(spec).not.toMatch(/VITE_ENABLE_ROLE_OVERRIDE/)
    expect(spec).not.toMatch(/localStorage/)
    expect(spec).not.toMatch(/networkidle/)
    expect(spec).toMatch(/let publicationAttempted = false/)
    expect(spec).toMatch(/publicationAttempted = true/)
    expect(spec).toMatch(/if \(!publicationAttempted && receiptPreflight\.ok\)/)
    expect(spec).toMatch(/if \(!terminalOutcome\.ok\)/)
    expect(spec).not.toMatch(/catch \{\s*\/\/ publication already attempted; never retry\s*\}/)
  })
})
