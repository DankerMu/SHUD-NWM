/**
 * Config-before-browser evidence owner for the dedicated C4 live lane (#1895).
 * Classification is frozen and never relabeled:
 * 1. receipt path missing/blank         -> BLOCKED bounded diagnostic, no file.
 * 2. receipt path lexically malformed   -> FAIL bounded diagnostic, no file.
 * 3. receipt path valid but unsafe POSIX parent -> BLOCKED, no file.
 * 4. safe path + missing frontend/API URL -> one REQUIRED_ENV_MISSING BLOCKED receipt.
 * 5. safe path + missing/invalid pin / malformed URL / forbidden override/role
 *    -> one CONFIG_INVALID FAIL receipt (valid origins preserved).
 * 6. fully valid config -> no receipt, caller owns browser work.
 */

import { lstatSync, statSync, realpathSync, openSync, fstatSync, fchmodSync, writeSync, fsyncSync, closeSync, linkSync, unlinkSync, readSync } from 'node:fs'

import { parseC4DisplayConfig, c4ReceiptPathFromEnv, type C4ConfigParse } from './src/lib/c4DisplayEvidence/config'
import { buildC4TerminalEvidence, type C4Evidence, type C4Failure } from './src/lib/c4DisplayEvidence/receipt'
import {
  publishC4Evidence,
  validateC4ReceiptPath,
  type C4EvidenceFs,
} from './playwright.c4-display-evidence'

export interface C4EvidenceOwnerPublication {
  publish(path: string, receipt: C4Evidence): { path: string }
}

export type C4EvidenceOwnerResult =
  | { ok: true }
  | {
      ok: false
      classification: 'BLOCKED' | 'FAIL'
      code: string
      receiptWritten: boolean
      message: string
    }

export function ownerRealC4Fs(): C4EvidenceFs {
  return {
    lstatSync: (p) => lstatSync(p),
    statSync: (p) => statSync(p),
    realpathSync: (p) => realpathSync(p),
    openSync: (p, flags, mode) => openSync(p, flags, mode as number),
    fstatSync: (fd) => fstatSync(fd),
    fchmodSync: (fd, mode) => fchmodSync(fd, mode),
    writeSync: (fd, buffer) => writeSync(fd, buffer),
    fsyncSync: (fd) => fsyncSync(fd),
    closeSync: (fd) => closeSync(fd),
    linkSync: (oldPath, newPath) => linkSync(oldPath, newPath),
    unlinkSync: (p) => unlinkSync(p),
    readSync: (fd, buffer, offset, length, position) => readSync(fd, buffer, offset, length, position),
  }
}

function validOriginsOf(
  parsed: C4ConfigParse,
  env: Record<string, string | undefined>,
): { frontend: string | null; api: string | null } {
  if (parsed.ok || parsed.classification !== 'FAIL') return { frontend: null, api: null }
  const originOf = (value: string | undefined): string | null => {
    const trimmed = value?.trim()
    if (!trimmed) return null
    try {
      const url = new URL(trimmed)
      if ((url.protocol === 'http:' || url.protocol === 'https:') && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash) {
        return url.origin
      }
    } catch {
      // ignore malformed
    }
    return null
  }
  return {
    frontend: originOf(env.PLAYWRIGHT_LIVE_BASE_URL),
    api: originOf(env.PLAYWRIGHT_LIVE_API_BASE_URL),
  }
}

function closedFailure(parsed: C4ConfigParse): C4Failure {
  if (parsed.ok) throw new Error('closedFailure called with a successful config parse')
  return { code: parsed.code, stage: parsed.stage, message: parsed.message }
}

export async function runC4LiveEvidenceOwner(
  env: Record<string, string | undefined>,
  publication: C4EvidenceOwnerPublication,
  startedAt: string = new Date().toISOString(),
  realFs: C4EvidenceFs = ownerRealC4Fs(),
): Promise<C4EvidenceOwnerResult> {
  const receiptPathResult = c4ReceiptPathFromEnv(env)
  if (!receiptPathResult.ok) {
    return { ok: false, classification: 'FAIL', code: 'CONFIG_INVALID', receiptWritten: false, message: receiptPathResult.message }
  }
  const receiptPath = receiptPathResult.path
  if (!receiptPath) {
    return { ok: false, classification: 'BLOCKED', code: 'REQUIRED_ENV_MISSING', receiptWritten: false, message: 'PLAYWRIGHT_LIVE_C4_RECEIPT_PATH is required' }
  }

  let preflight: ReturnType<typeof validateC4ReceiptPath>
  try {
    preflight = validateC4ReceiptPath(receiptPath, realFs)
  } catch {
    return { ok: false, classification: 'BLOCKED', code: 'RUNTIME_UNAVAILABLE', receiptWritten: false, message: 'receipt path preflight failed' }
  }
  if (!preflight.ok) {
    const blockedFs = ['PARENT_UNAVAILABLE', 'PARENT_NOT_CANONICAL', 'PARENT_NOT_DIRECTORY', 'PARENT_FOREIGN_OWNER', 'PARENT_MODE_INVALID', 'TARGET_EXISTS', 'TARGET_UNKNOWN', 'RUNTIME_UNAVAILABLE']
    const classification = blockedFs.includes(preflight.code) ? 'BLOCKED' : 'FAIL'
    return { ok: false, classification, code: classification === 'BLOCKED' ? 'RUNTIME_UNAVAILABLE' : 'CONFIG_INVALID', receiptWritten: false, message: preflight.message }
  }

  const parsed = parseC4DisplayConfig(env)
  if (parsed.ok) return { ok: true }

  const origins = validOriginsOf(parsed, env)
  const endedAt = new Date().toISOString()
  const receipt = buildC4TerminalEvidence({
    startedAt,
    endedAt,
    frontendOrigin: origins.frontend,
    apiOrigin: origins.api,
    failure: closedFailure(parsed),
  })
  if (!receipt.ok) {
    return { ok: false, classification: parsed.classification, code: 'PUBLICATION_FAILED', receiptWritten: false, message: receipt.reason }
  }
  try {
    publication.publish(receiptPath, receipt.receipt)
  } catch {
    return { ok: false, classification: parsed.classification, code: 'PUBLICATION_FAILED', receiptWritten: false, message: 'C4 owner publication failed' }
  }
  return {
    ok: false,
    classification: parsed.classification,
    code: parsed.code,
    receiptWritten: true,
    message: parsed.message,
  }
}
