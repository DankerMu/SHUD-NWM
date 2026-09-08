import { describe, expect, it, vi } from 'vitest'
import { chmodSync, mkdirSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { runC4LiveEvidenceOwner, type C4EvidenceOwnerPublication } from '../../playwright.c4-display-evidence-owner'
import type { C4Evidence } from '../lib/c4DisplayEvidence/receipt'

function safeParentDir() {
  const base = realpathSync(tmpdir())
  const root = path.join(base, `nhms-c4-owner-${Math.random().toString(16).slice(2)}`)
  mkdirSync(root, { mode: 0o700 })
  chmodSync(root, 0o700)
  return root
}

function ownerSeam(parentDir: string): C4EvidenceOwnerPublication & { publish: ReturnType<typeof vi.fn> } {
  const publish = vi.fn((_path: string, _receipt: C4Evidence) => ({ path: _path }))
  return { publish }
}

function validEnv(receiptPath: string): Record<string, string> {
  return {
    PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
    PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
    PLAYWRIGHT_LIVE_C4_BASIN_ID: 'basins_qhh',
    PLAYWRIGHT_LIVE_C4_SEGMENT_ID: 'seg-001',
    PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: receiptPath,
  }
}

describe('C4 evidence-owner config-before-browser publication', () => {
  it('publishes exactly one REQUIRED_ENV_MISSING BLOCKED receipt when the URL is missing but the path is safe', async () => {
    const parent = safeParentDir()
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-c4-live-evidence-owner.json')
      const seam = ownerSeam(parent)
      const env = validEnv(receiptPath)
      delete env.PLAYWRIGHT_LIVE_BASE_URL
      const result = await runC4LiveEvidenceOwner(env, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('BLOCKED')
        expect(result.code).toBe('REQUIRED_ENV_MISSING')
        expect(result.receiptWritten).toBe(true)
      }
      expect(seam.publish).toHaveBeenCalledTimes(1)
      const receipt = seam.publish.mock.calls[0][1] as C4Evidence
      expect(receipt.status).toBe('BLOCKED')
      expect(receipt.origins).toEqual({ frontend: null, api: null })
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('publishes exactly one CONFIG_INVALID FAIL receipt for a river override and writes no file for a missing path', async () => {
    const parent = safeParentDir()
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-c4-live-evidence-owner.json')
      const seam = ownerSeam(parent)
      const result = await runC4LiveEvidenceOwner({
        ...validEnv(receiptPath),
        PLAYWRIGHT_LIVE_RIVER_RUN_ID: 'run-1',
      }, seam)
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.classification).toBe('FAIL')
        expect(result.code).toBe('CONFIG_INVALID')
        expect(result.receiptWritten).toBe(true)
      }
      expect(seam.publish).toHaveBeenCalledTimes(1)

      const missing = await runC4LiveEvidenceOwner({
        PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
        PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
        PLAYWRIGHT_LIVE_C4_BASIN_ID: 'basins_qhh',
        PLAYWRIGHT_LIVE_C4_SEGMENT_ID: 'seg-001',
      }, ownerSeam(parent))
      expect(missing.ok).toBe(false)
      if (!missing.ok) {
        expect(missing.classification).toBe('BLOCKED')
        expect(missing.receiptWritten).toBe(false)
      }
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('succeeds with no receipt on a fully valid config', async () => {
    const parent = safeParentDir()
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-c4-live-evidence-owner.json')
      const seam = ownerSeam(parent)
      const result = await runC4LiveEvidenceOwner(validEnv(receiptPath), seam)
      expect(result).toEqual({ ok: true })
      expect(seam.publish).not.toHaveBeenCalled()
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })
})
