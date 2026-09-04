import { describe, expect, it, vi } from 'vitest'
import { existsSync, mkdtempSync, mkdirSync, rmSync, chmodSync, realpathSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { parseRiverClickConfig, riverClickReceiptPathFromEnv } from '../lib/riverClickEvidence/config'
import { runRiverClickLiveEvidenceOwner, type RiverClickEvidenceOwnerPublication } from '../../playwright.river-click-evidence-owner'
import { publishRiverClickEvidence } from '../../playwright.river-click-evidence'
import type { RiverClickEvidence } from '../lib/riverClickEvidence/receipt'

/**
 * Config-before-browser terminal publication: a pure/injected evidence-owner
 * helper drives the exact live-spec decision tree without spawning Playwright.
 * With a safe receipt path, a missing-URL env publishes ONE REQUIRED_ENV_MISSING
 * BLOCKED receipt; an invalid pin/URL or forbidden override publishes ONE
 * CONFIG_INVALID FAIL receipt; a missing/unsafe path publishes no file. A
 * receipt-build failure after a safe path must fall back to exactly one
 * schema-valid INTERNAL_ERROR receipt.
 */

function safeParentDir() {
  // tmpdir() may be a symlink (/tmp -> /private/tmp on macOS); the owner's
  // filesystem preflight requires a CANONICAL parent, so realpath first.
  const base = realpathSync(tmpdir())
  const root = path.join(base, `nhms-river-owner-${Math.random().toString(16).slice(2)}`)
  const { mkdirSync } = require('node:fs') as typeof import('node:fs')
  mkdirSync(root, { mode: 0o700 })
  return root
}

function safeReceiptPath(parent: string) {
  return path.join(parent, 'nhms-frontend-river-click-live-evidence-owner.json')
}

interface OwnerSeam extends RiverClickEvidenceOwnerPublication {
  publish: ReturnType<typeof vi.fn<(path: string, receipt: RiverClickEvidence) => { path: string }>>
  receiptPath: string
  parentDir: string
}

function ownerSeam(parentDir: string): OwnerSeam {
  const publish = vi.fn((_path: string, _receipt: RiverClickEvidence) => ({ path: _path }))
  return { publish, receiptPath: safeReceiptPath(parentDir), parentDir }
}

/** Narrow an owner result's failure branch; a success is a test error. */
function expectOwnerFailure(result: Awaited<ReturnType<typeof runRiverClickLiveEvidenceOwner>>): Extract<Awaited<ReturnType<typeof runRiverClickLiveEvidenceOwner>>, { ok: false }> {
  if (result.ok) throw new Error('owner must fail')
  return result
}

function validEnv(receiptPath: string): Record<string, string> {
  return {
    PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
    PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
    PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
    PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
    PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: receiptPath,
  }
}

describe('river-click evidence-owner config-before-browser terminal publication', () => {
  it('exports a narrow receipt-path env parser', () => {
    expect(typeof riverClickReceiptPathFromEnv).toBe('function')
    const parsed = riverClickReceiptPathFromEnv({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/a/nhms-frontend-river-click-live-evidence-1.json' })
    expect(parsed).toEqual({ ok: true, path: '/a/nhms-frontend-river-click-live-evidence-1.json' })
    expect(riverClickReceiptPathFromEnv({}).ok).toBe(true)
    expect(riverClickReceiptPathFromEnv({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: 'relative.json' }).ok).toBe(false)
  })

  it('publishes exactly one REQUIRED_ENV_MISSING BLOCKED receipt when the base URL is missing but the path is safe', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      delete env.PLAYWRIGHT_LIVE_BASE_URL
      const result = await runRiverClickLiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('BLOCKED')
        expect(result.code).toBe('REQUIRED_ENV_MISSING')
        expect(result.receiptWritten).toBe(true)
      }
      expect(seam.publish).toHaveBeenCalledTimes(1)
      const receipt = seam.publish.mock.calls[0][1] as { status: string; failure: { code: string } }
      expect(receipt.status).toBe('BLOCKED')
      expect(receipt.failure.code).toBe('REQUIRED_ENV_MISSING')
      expect(seam.publish.mock.calls[0][0]).toBe(seam.receiptPath)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('publishes exactly one CONFIG_INVALID FAIL receipt when a pin is missing but the path is safe', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      delete env.PLAYWRIGHT_LIVE_RIVER_BASIN_ID
      const result = await runRiverClickLiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('FAIL')
        expect(result.code).toBe('CONFIG_INVALID')
        expect(result.receiptWritten).toBe(true)
      }
      expect(seam.publish).toHaveBeenCalledTimes(1)
      const receipt = seam.publish.mock.calls[0][1] as { status: string; failure: { code: string } }
      expect(receipt.status).toBe('FAIL')
      expect(receipt.failure.code).toBe('CONFIG_INVALID')
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('publishes exactly one CONFIG_INVALID FAIL receipt for a forbidden override (empty value)', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      env.PLAYWRIGHT_LIVE_RIVER_RUN_ID = ''
      const result = await runRiverClickLiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.code).toBe('CONFIG_INVALID')
        expect(result.receiptWritten).toBe(true)
      }
      expect(seam.publish).toHaveBeenCalledTimes(1)
      const receipt = seam.publish.mock.calls[0][1] as { status: string; failure: { code: string } }
      expect(receipt.status).toBe('FAIL')
      expect(receipt.failure.code).toBe('CONFIG_INVALID')
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('writes no file with CLOSED classification codes: malformed path => FAIL CONFIG_INVALID, missing path => BLOCKED REQUIRED_ENV_MISSING', async () => {
    // Supplied lexically malformed path: FAIL CONFIG_INVALID no file.
    const malformed = await runRiverClickLiveEvidenceOwner(
      { PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: 'relative/unsafe.json', PLAYWRIGHT_LIVE_BASE_URL: 'https://x' },
      { publish: vi.fn() },
    )
    const malformedResult = expectOwnerFailure(await runRiverClickLiveEvidenceOwner(
      { PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: 'relative/unsafe.json', PLAYWRIGHT_LIVE_BASE_URL: 'https://x' },
      { publish: vi.fn() },
    ))
    expect(malformedResult.classification).toBe('FAIL')
    expect(malformedResult.code).toBe('CONFIG_INVALID')
    expect(malformedResult.receiptWritten).toBe(false)
    // Missing/blank path: BLOCKED REQUIRED_ENV_MISSING no file.
    const missingResult = expectOwnerFailure(await runRiverClickLiveEvidenceOwner({}, { publish: vi.fn() }))
    expect(missingResult.classification).toBe('BLOCKED')
    expect(missingResult.code).toBe('REQUIRED_ENV_MISSING')
    expect(missingResult.receiptWritten).toBe(false)
  })

  it('filesystem safety refusals use the CLOSED BLOCKED RUNTIME_UNAVAILABLE code, never an arbitrary publisher code', async () => {
    // Noncanonical parent (symlinked tmpdir path) -> BLOCKED RUNTIME_UNAVAILABLE
    // with the bounded publisher reason as the secondary message, no file.
    const symlinkParent = path.join(tmpdir(), `nhms-river-owner-${Math.random().toString(16).slice(2)}`)
    const { mkdirSync, symlinkSync } = require('node:fs') as typeof import('node:fs')
    const canonicalBase = realpathSync(tmpdir())
    const realRoot = path.join(canonicalBase, `nhms-river-owner-real-${Math.random().toString(16).slice(2)}`)
    mkdirSync(realRoot, { mode: 0o700 })
    try {
      symlinkSync(realRoot, symlinkParent)
      // symlinkParent resolves to realRoot but the PARENT PARSE must see the
      // path itself as canonical; use the symlinked directory as the PARENT of
      // the receipt so validateRiverClickReceiptPath rejects PARENT_NOT_CANONICAL.
      const receiptPath = path.join(symlinkParent, 'nhms-frontend-river-click-live-evidence-1.json')
      const publish = vi.fn((_path: string, _receipt: RiverClickEvidence) => ({ path: _path }))
      const result = await runRiverClickLiveEvidenceOwner({ ...validEnv(receiptPath) }, { publish })
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('BLOCKED')
        expect(result.code).toBe('RUNTIME_UNAVAILABLE')
        expect(result.message).toMatch(/symlink|canonical/i)
        expect(result.receiptWritten).toBe(false)
      }
      expect(publish).not.toHaveBeenCalled()
    } finally {
      rmSync(realRoot, { recursive: true, force: true })
      try { rmSync(symlinkParent, { recursive: true, force: true }) } catch { /* symlink already removed */ }
    }
  })

  it('returns PUBLICATION_FAILED with no file when the single publication attempt throws (BLOCKED path)', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const publish = vi.fn((_path: string, _receipt: RiverClickEvidence): { path: string } => {
        throw new Error('publish boom')
      })
      const env = validEnv(seam.receiptPath)
      delete env.PLAYWRIGHT_LIVE_BASE_URL
      const result = await runRiverClickLiveEvidenceOwner(env, { ...seam, publish })
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.code).toBe('PUBLICATION_FAILED')
        expect(result.receiptWritten).toBe(false)
      }
      expect(publish).toHaveBeenCalledTimes(1)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('NEVER relabels a known config terminal as INTERNAL_ERROR and has no INTERNAL_ERROR fallback export', async () => {
    // A known REQUIRED_ENV_MISSING (BLOCKED) or CONFIG_INVALID (FAIL) build/
    // publish failure must return fixed PUBLICATION_FAILED with the SAME
    // classification, no receipt, and exactly zero/one publication attempt.
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      delete env.PLAYWRIGHT_LIVE_RIVER_BASIN_ID // CONFIG_INVALID path
      const failPublish = vi.fn((_path: string, _receipt: RiverClickEvidence): { path: string } => {
        throw new Error('publish boom')
      })
      const result = await runRiverClickLiveEvidenceOwner(env, { ...seam, publish: failPublish })
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('FAIL')
        expect(result.code).toBe('PUBLICATION_FAILED')
        expect(result.receiptWritten).toBe(false)
      }
      expect(failPublish).toHaveBeenCalledTimes(1)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
    // No INTERNAL_ERROR fallback export remains and the owner never constructs
    // an INTERNAL_ERROR receipt (the closed classification is never rewritten).
    const ownerSource = readFileSync(path.join(path.resolve(__dirname, '../../'), 'playwright.river-click-evidence-owner.ts'), 'utf8')
    expect(ownerSource).not.toMatch(/buildOwnerFallbackReceipt/)
    expect(ownerSource).not.toMatch(/INTERNAL_ERROR/)
  })

  it('owner publishes ONE real BLOCKED receipt through the REAL publisher into a real mode-0700 parent when the config module tolerates missing env', async () => {
    // Integration through the actual config module path: the live profile
    // (playwright.live-display.config.ts) must NEVER preempt the owner — a
    // missing URL reaches the owner's REQUIRED_ENV_MISSING classification and
    // lands exactly one schema-valid BLOCKED receipt on disk, no browser work.
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-owner-live-')))
    try {
      chmodSync(parent, 0o700)
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-owner-integration.json')
      const env = validEnv(receiptPath)
      delete env.PLAYWRIGHT_LIVE_BASE_URL
      const result = await runRiverClickLiveEvidenceOwner(env, {
        publish: (p, receipt) => publishRiverClickEvidence(p, receipt),
      })
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.code).toBe('REQUIRED_ENV_MISSING')
        expect(result.receiptWritten).toBe(true)
      }
      const written = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(written.status).toBe('BLOCKED')
      expect(written.failure.code).toBe('REQUIRED_ENV_MISSING')
      // BLOCKED claims all-null: no origins/identities/counts.
      expect(written.origins.frontend).toBeNull()
      expect(written.origins.api).toBeNull()
      expect(written.requested_feature).toBeNull()
      expect(written.warmup_count).toBe(0)
      expect(written.accepted_count).toBe(0)
      expect(written.samples).toEqual([])
      const { validateRiverClickEvidenceDocument } = await import('../lib/riverClickEvidence/receipt')
      expect(validateRiverClickEvidenceDocument(written).ok).toBe(true)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('owner preserves a valid normalized origin on a CONFIG_INVALID FAIL (one malformed URL, one valid)', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      // API URL malformed (path not bare); frontend stays valid.
      env.PLAYWRIGHT_LIVE_API_BASE_URL = 'https://api.example.test/path'
      const result = await runRiverClickLiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.code).toBe('CONFIG_INVALID')
        expect(result.receiptWritten).toBe(true)
      }
      const receipt = seam.publish.mock.calls[0][1] as { status: string; failure: { code: string }; origins: { frontend: string | null; api: string | null } }
      expect(receipt.status).toBe('FAIL')
      expect(receipt.failure.code).toBe('CONFIG_INVALID')
      expect(receipt.origins.frontend).toBe('https://display.example.test')
      expect(receipt.origins.api).toBeNull()
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('filesystem-preflights a valid path even when env is fully valid (no receipt, setup proceeds)', async () => {
    const parent = safeParentDir()
    try {
      const seam = ownerSeam(parent)
      const env = validEnv(seam.receiptPath)
      const result = await runRiverClickLiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(true)
      expect(seam.publish).not.toHaveBeenCalled()
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('the ACTUAL Playwright profile globalSetup, run as a subprocess with a real mode-0700 parent + safe path + missing URL, publishes ONE mode-0600 BLOCKED receipt and aborts before browser work', async () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-gs-')))
    try {
      chmodSync(parent, 0o700)
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-globalsetup.json')
      const repoRoot = path.resolve(__dirname, '../../../../')
      const frontendDir = path.join(repoRoot, 'apps/frontend')
      const { execFileSync } = await import('node:child_process')
      // Run the ACTUAL configured profile through the Playwright CLI: globalSetup
      // runs before any test/browser; with the missing-URL env it publishes one
      // BLOCKED receipt and aborts the whole run.
      let exitCode = 0
      let stderr = ''
      try {
        execFileSync('corepack', [
          'pnpm@10.11.0', '--dir', frontendDir, 'exec', 'playwright', 'test',
          '--config', 'playwright.live-river-click.config.ts',
          'e2e/live-display.spec.ts',
          '-g', '__NO_SUCH_TEST_ABORTS_BEFORE_BROWSER__',
        ], {
          cwd: frontendDir,
          encoding: 'utf8',
          stdio: 'pipe',
          env: {
            ...process.env,
            PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: receiptPath,
            PLAYWRIGHT_LIVE_BASE_URL: '',
            PLAYWRIGHT_LIVE_API_BASE_URL: '',
            PLAYWRIGHT_LIVE_RIVER_BASIN_ID: '',
            PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '',
          },
        })
      } catch (error) {
        const e = error as { status?: number; stderr?: string }
        exitCode = e.status ?? 1
        stderr = e.stderr ?? ''
      }
      expect(exitCode).not.toBe(0)
      // The globalSetup stderr must carry the FROZEN BLOCKED status prefix
      // followed by the closed bounded code (never a bare code line).
      expect(stderr).toMatch(/BLOCKED: REQUIRED_ENV_MISSING:/)
      const written = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(written.status).toBe('BLOCKED')
      expect(written.failure.code).toBe('REQUIRED_ENV_MISSING')
      const stat = (await import('node:fs')).statSync(receiptPath)
      expect(stat.mode & 0o777).toBe(0o600)
      const { validateRiverClickEvidenceDocument } = await import('../lib/riverClickEvidence/receipt')
      expect(validateRiverClickEvidenceDocument(written).ok).toBe(true)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  }, 120_000)

  it('the ACTUAL Playwright profile globalSetup aborts with a literal FAIL prefix and ONE CONFIG_INVALID FAIL receipt on a safe path + invalid pin', async () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-gs-fail-')))
    try {
      chmodSync(parent, 0o700)
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-globalsetup-fail.json')
      const repoRoot = path.resolve(__dirname, '../../../../')
      const frontendDir = path.join(repoRoot, 'apps/frontend')
      const { execFileSync } = await import('node:child_process')
      let exitCode = 0
      let stderr = ''
      try {
        execFileSync('corepack', [
          'pnpm@10.11.0', '--dir', frontendDir, 'exec', 'playwright', 'test',
          '--config', 'playwright.live-river-click.config.ts',
          'e2e/live-display.spec.ts',
          '-g', '__NO_SUCH_TEST_ABORTS_BEFORE_BROWSER__',
        ], {
          cwd: frontendDir,
          encoding: 'utf8',
          stdio: 'pipe',
          env: {
            ...process.env,
            PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: receiptPath,
            PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
            PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
            PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
            PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'BAD PIN!!!',
          },
        })
      } catch (error) {
        const e = error as { status?: number; stderr?: string }
        exitCode = e.status ?? 1
        stderr = e.stderr ?? ''
      }
      expect(exitCode).not.toBe(0)
      expect(stderr).toMatch(/FAIL: CONFIG_INVALID:/)
      const written = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(written.status).toBe('FAIL')
      expect(written.failure.code).toBe('CONFIG_INVALID')
      expect(written.origins.frontend).toBe('https://display.example.test')
      expect(written.origins.api).toBe('https://api.example.test')
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  }, 120_000)

  it('the ACTUAL Playwright profile globalSetup aborts with closed prefixes and NO artifact for all-missing env and a supplied malformed path', async () => {
    const repoRoot = path.resolve(__dirname, '../../../../')
    const frontendDir = path.join(repoRoot, 'apps/frontend')
    const { execFileSync } = await import('node:child_process')
    const runProfile = (env: Record<string, string>): { exit: number; stderr: string } => {
      let exit = 0
      let stderr = ''
      try {
        execFileSync('corepack', [
          'pnpm@10.11.0', '--dir', frontendDir, 'exec', 'playwright', 'test',
          '--config', 'playwright.live-river-click.config.ts',
          'e2e/live-display.spec.ts',
          '-g', '__NO_SUCH_TEST_ABORTS_BEFORE_BROWSER__',
        ], {
          cwd: frontendDir,
          encoding: 'utf8',
          stdio: 'pipe',
          env: { ...process.env, ...env },
        })
      } catch (error) {
        const e = error as { status?: number; stderr?: string }
        exit = e.status ?? 1
        stderr = e.stderr ?? ''
      }
      return { exit, stderr }
    }
    // All env missing: BLOCKED REQUIRED_ENV_MISSING, no file. All five blanks
    // defeat any ambient PLAYWRIGHT_LIVE_* value (blank URLs are MISSING in the
    // parser; blank pins would only classify FAIL if URLs were present).
    const allMissing = runProfile({
      PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '',
      PLAYWRIGHT_LIVE_BASE_URL: '',
      PLAYWRIGHT_LIVE_API_BASE_URL: '',
      PLAYWRIGHT_LIVE_RIVER_BASIN_ID: '',
      PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '',
    })
    expect(allMissing.exit).not.toBe(0)
    expect(allMissing.stderr).toMatch(/BLOCKED: REQUIRED_ENV_MISSING:/)
    expect(allMissing.stderr).not.toMatch(/receipt published/)
    // Supplied lexically malformed path: FAIL CONFIG_INVALID, no file.
    const malformed = runProfile({
      PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: 'relative/evidence.json',
      PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
      PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
      PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
      PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
    })
    expect(malformed.exit).not.toBe(0)
    expect(malformed.stderr).toMatch(/FAIL: CONFIG_INVALID:/)
    expect(malformed.stderr).not.toMatch(/receipt published/)
  }, 180_000)

  it('Playwright monitoring profile selects the monitoring test without river owner/globalSetup and writes no river receipt', async () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-mon-')))
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-monitoring.json')
      const repoRoot = path.resolve(__dirname, '../../../../')
      const frontendDir = path.join(repoRoot, 'apps/frontend')
      const { execFileSync } = await import('node:child_process')
      let stdout = ''
      let stderr = ''
      let exitCode = 0
      try {
        const output = execFileSync('corepack', [
          'pnpm@10.11.0', '--dir', frontendDir, 'exec', 'playwright', 'test',
          '--config', 'playwright.live-display.config.ts',
          'e2e/live-display.spec.ts',
          '-g', 'loads live display_readonly frontend without local or control-plane requests @live-monitoring',
          '--list',
        ], {
          cwd: frontendDir,
          encoding: 'utf8',
          stdio: 'pipe',
          env: {
            ...process.env,
            PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
            PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
            PLAYWRIGHT_LIVE_RIVER_BASIN_ID: '',
            PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '',
            PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '',
          },
        })
        stdout = output
      } catch (error) {
        const e = error as { status?: number; stdout?: string; stderr?: string }
        exitCode = e.status ?? 1
        stdout = e.stdout ?? ''
        stderr = e.stderr ?? ''
      }
      expect(exitCode).toBe(0)
      expect(stdout).toMatch(/loads live display_readonly frontend without local or control-plane requests @live-monitoring/)
      expect(stdout).not.toMatch(/@live-river-click/)
      expect(stderr).not.toMatch(/BLOCKED: REQUIRED_ENV_MISSING:/)
      expect(existsSync(receiptPath)).toBe(false)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  }, 120_000)
})
