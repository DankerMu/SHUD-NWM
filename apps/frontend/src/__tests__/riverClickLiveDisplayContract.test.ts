import { describe, expect, it, vi } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { assertRiverClickLiveDisplayContract } from '../../playwright.config.helpers'

const PASS_LIVE_SPEC = [
  "await page.addInitScript(() => { window.__NHMS_E2E_HOOKS__ = true })",
  "await page.goto('/')",
].join('\n')

describe('river-click live display static contract', () => {
  it('rejects a broad page.route API mock in the live spec before browser execution', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-river-live-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      mkdirSync(e2eDir)
      writeFileSync(path.join(e2eDir, 'live-display.spec.ts'), "await page.route('**/api/v1/**', async () => {})\n" + PASS_LIVE_SPEC)

      expect(() => assertRiverClickLiveDisplayContract(e2eDir)).toThrow(/broad page\.route/)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('accepts the live spec without broad API mocks', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'nhms-river-live-'))
    try {
      const e2eDir = path.join(root, 'e2e')
      mkdirSync(e2eDir)
      writeFileSync(path.join(e2eDir, 'live-display.spec.ts'), PASS_LIVE_SPEC)
      expect(assertRiverClickLiveDisplayContract(e2eDir)).toEqual([path.join(e2eDir, 'live-display.spec.ts')])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('requires the live profile to pin exactly one worker and zero retries', async () => {
    const previous = { ...process.env }
    try {
      process.env.PLAYWRIGHT_LIVE_BASE_URL = 'https://display.example.test'
      process.env.PLAYWRIGHT_LIVE_API_BASE_URL = 'https://api.example.test'
      const config = await import('../../playwright.live-display.config')
      expect(config.default.workers).toBe(1)
      expect(config.default.retries).toBe(0)
    } finally {
      process.env = previous
    }
  })

  it('the actual config module tolerates missing URLs so the owner can publish a BLOCKED receipt (no config-preempt)', async () => {
    // The live profile module MUST NOT throw at import when the required URLs
    // are absent: the owner (runRiverClickLiveEvidenceOwner) is the one that
    // classifies REQUIRED_ENV_MISSING and publishes the BLOCKED receipt before
    // any browser work. If the config module preempted, the owner could never
    // publish. Importing the real module under a cleared env proves it.
    const previous = { ...process.env }
    vi.resetModules()
    try {
      delete process.env.PLAYWRIGHT_LIVE_BASE_URL
      delete process.env.PLAYWRIGHT_LIVE_API_BASE_URL
      delete process.env.PLAYWRIGHT_LIVE_RIVER_BASIN_ID
      delete process.env.PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID
      delete process.env.PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH
      const config = await import('../../playwright.live-display.config')
      expect(config.default).toBeDefined()
      // Missing URL must NOT throw at import (loadLiveDisplayEnv is tolerated
      // into the placeholder, so the owner's BLOCKED classification is the one
      // that runs and publishes before browser work).
      expect(config.default.use?.baseURL).toBe('http://127.0.0.1:0')
    } finally {
      process.env = previous
    }
  })

  it('keeps the river-click lane from adding /ops or /monitoring-only claims', async () => {
    const previous = { ...process.env }
    try {
      process.env.PLAYWRIGHT_LIVE_BASE_URL = 'https://display.example.test'
      process.env.PLAYWRIGHT_LIVE_API_BASE_URL = 'https://api.example.test'
      const config = await import('../../playwright.live-display.config')
      const projects = config.default.projects?.map((project) => project.name)
      expect(projects).toEqual(['live-display-readonly-chromium'])
      expect(config.default.metadata).toMatchObject({ evidenceLane: 'live-display-readonly' })
    } finally {
      process.env = previous
    }
  })

  it('lists all five required river-click env keys in the live profile metadata', async () => {
    const previous = { ...process.env }
    try {
      process.env.PLAYWRIGHT_LIVE_BASE_URL = 'https://display.example.test'
      process.env.PLAYWRIGHT_LIVE_API_BASE_URL = 'https://api.example.test'
      const config = await import('../../playwright.live-display.config')
      const required = config.default.metadata?.requiredEnv as string[]
      expect(required).toEqual([
        'PLAYWRIGHT_LIVE_BASE_URL',
        'PLAYWRIGHT_LIVE_API_BASE_URL',
        'PLAYWRIGHT_LIVE_RIVER_BASIN_ID',
        'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID',
        'PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH',
      ])
    } finally {
      process.env = previous
    }
  })

  it('does not allow unsafe `as never` PASS inputs to the receipt builder in the live spec', () => {
    const spec = readFileSync(path.resolve(__dirname, '../../e2e/live-display.spec.ts'), 'utf8')
    // The live spec must narrow/validate terminal non-null fields before building
    // a PASS receipt instead of casting unknown terminal fields with as never.
    // Strip comments so the no-cast rule checks real code, not documentation.
    const codeOnly = spec.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(codeOnly).not.toMatch(/\bas never\b/)
    // It must also validate the non-null identity contract before the PASS build.
    expect(spec).toMatch(/terminal\.requestedFeature === null/)
  })

  it('exactly-one-publication discipline: the live spec never publishes in the catch after any publication attempt started', () => {
    const spec = readFileSync(path.resolve(__dirname, '../../e2e/live-display.spec.ts'), 'utf8')
    // publicationAttempted flips BEFORE the first publish call (inside the
    // publisher wrapper), and every catch publication is guarded by !it: a
    // publisher failure is terminal, never retried with an INTERNAL_ERROR.
    expect(spec).toMatch(/let publicationAttempted = false/)
    expect(spec).toMatch(/publicationAttempted = true/)
    expect(spec).toMatch(/if \(!publicationAttempted && receiptPreflight\.ok\)/)
    // The PASS path uses the tested PASS publisher with the whole deadline
    // enforced through construction (BEFORE + AFTER build).
    expect(spec).toMatch(/publishRiverClickPass\(/)
    expect(spec).toMatch(/wholeDeadline/)
  })
})
