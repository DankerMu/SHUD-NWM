/**
 * Config-before-browser evidence owner for the river-click live lane (#1970).
 * A pure/injected decision tree the REAL live profile globalSetup calls BEFORE
 * any browser fixture launches: resolve/preflight a supplied safe receipt path
 * FIRST (filesystem proof before browser work), then classify the env, and
 * publish exactly one honest terminal (BLOCKED for missing required env, FAIL
 * for invalid config) or write no file at all (missing/unsafe receipt path).
 *
 * The classification is FROZEN — it is never relabeled:
 * 1. receipt path missing/blank         -> BLOCKED bounded diagnostic, no file.
 * 2. receipt path lexically malformed   -> FAIL bounded diagnostic, no file.
 * 3. receipt path valid but noncanonical / non-0700 / foreign owner /
 *    existing target / unsupported POSIX -> BLOCKED bounded diagnostic, no
 *    new file, no overwrite.
 * 4. safe path + missing frontend/API URL -> exactly one REQUIRED_ENV_MISSING
 *    BLOCKED receipt (all-null claims), then nonzero.
 * 5. safe path + missing/invalid pin / malformed URL / forbidden override ->
 *    exactly one CONFIG_INVALID FAIL receipt (valid normalized origins
 *    preserved; BLOCKED stays all-null), then nonzero.
 * 6. fully valid config -> no receipt, setup succeeds.
 * A known terminal that fails to BUILD is fixed PUBLICATION_FAILED with no
 * receipt (the closed classification is NEVER rewritten) and a publisher
 * failure gets exactly one attempt (no retry).
 */

import {
  buildRiverClickTerminalEvidence,
  type RiverClickEvidence,
  type RiverClickFailure,
} from './src/lib/riverClickEvidence/receipt'
import { parseRiverClickConfig, riverClickReceiptPathFromEnv, type RiverClickConfigParse } from './src/lib/riverClickEvidence/config'
import {
  publishRiverClickEvidence,
  validateRiverClickReceiptPath,
  RiverClickPublicationError,
  type RiverClickEvidenceFs,
} from './playwright.river-click-evidence'
import { lstatSync, statSync, realpathSync, openSync, fstatSync, fchmodSync, writeSync, fsyncSync, closeSync, linkSync, unlinkSync, readSync } from 'node:fs'

export interface RiverClickEvidenceOwnerPublication {
  publish(path: string, receipt: RiverClickEvidence): { path: string }
}

export type RiverClickEvidenceOwnerResult =
  | { ok: true }
  | {
      ok: false
      /** Frozen status prefix: BLOCKED or FAIL (never empty/unknown). */
      classification: 'BLOCKED' | 'FAIL'
      code: string
      receiptWritten: boolean
      message: string
    }

/** Real filesystem seam used by the owner's filesystem preflight. */
export function ownerRealFs(): RiverClickEvidenceFs {
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
  parsed: RiverClickConfigParse,
  env: Record<string, string | undefined>,
): { frontend: string | null; api: string | null } {
  // CONFIG_INVALID preserves any independently valid normalized origin; the
  // parse failure already reported the offending field, so recover the other
  // one from the raw env without re-reporting.
  if (parsed.ok || parsed.classification !== 'FAIL') return { frontend: null, api: null }
  const candidate = env as Record<string, string | undefined>
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
    frontend: originOf(candidate.PLAYWRIGHT_LIVE_BASE_URL),
    api: originOf(candidate.PLAYWRIGHT_LIVE_API_BASE_URL),
  }
}

function closedFailure(parsed: RiverClickConfigParse): RiverClickFailure {
  // `parsed.ok === true` cannot reach a receipt: the caller returns ok:true
  // before building. The failure classification is FROZEN from the parse.
  if (parsed.ok) {
    throw new Error('closedFailure called with a successful config parse')
  }
  return {
    code: parsed.code,
    stage: parsed.stage,
    sampleIndex: null,
    gfsStatus: null,
    ifsStatus: null,
    message: parsed.message,
  }
}

/**
 * Drive the pre-browser evidence decision for the river-click live lane.
 *
 * - Missing/blank receipt path -> BLOCKED bounded diagnostic, NO file.
 * - Supplied lexically malformed path -> FAIL bounded diagnostic, NO file.
 * - Valid path but noncanonical/non-0700/foreign/existing/unsupported POSIX ->
 *   BLOCKED bounded diagnostic, no new file, no overwrite.
 * - Supplied SAFE path + missing required URL -> one REQUIRED_ENV_MISSING
 *   BLOCKED receipt, nonzero result.
 * - Supplied SAFE path + invalid/missing pin / malformed URL / forbidden
 *   override -> one CONFIG_INVALID FAIL receipt (valid origins preserved).
 * - Env OK -> filesystem-preflight still runs (before browser launch); on
 *   success the caller owns browser work and terminal publication.
 */
export async function runRiverClickLiveEvidenceOwner(
  env: Record<string, string | undefined>,
  publication: RiverClickEvidenceOwnerPublication,
  startedAt: string = new Date().toISOString(),
  realFs: RiverClickEvidenceFs = ownerRealFs(),
): Promise<RiverClickEvidenceOwnerResult> {
  // Frozen classification (never relabeled, never rewritten):
  // - missing/blank receipt path            -> BLOCKED REQUIRED_ENV_MISSING no file
  // - supplied lexically malformed path     -> FAIL CONFIG_INVALID no file
  // - filesystem-unsafe/noncanonical/non-0700/foreign/existing/unsupported
  //   runtime                                -> BLOCKED RUNTIME_UNAVAILABLE no file
  //   (bounded publisher reason retained as the secondary message)
  // - safe path + missing URLs              -> BLOCKED receipt
  // - safe path + config-invalid            -> FAIL receipt
  // 1. Receipt-path classification first (path problems never write a file).
  const receiptPathResult = riverClickReceiptPathFromEnv(env)
  if (!receiptPathResult.ok) {
    // Supplied lexically malformed path (relative/aliased/bad basename):
    // FAIL CONFIG_INVALID no file.
    return { ok: false, classification: 'FAIL', code: 'CONFIG_INVALID', receiptWritten: false, message: receiptPathResult.message }
  }
  const receiptPath = receiptPathResult.path
  if (receiptPath === '') {
    // Missing/blank path: BLOCKED REQUIRED_ENV_MISSING no file.
    return { ok: false, classification: 'BLOCKED', code: 'REQUIRED_ENV_MISSING', receiptWritten: false, message: 'missing river-click live evidence receipt path' }
  }

  // 2. Filesystem preflight of the valid path (parent canonical/0700/owner,
  // target absent) — must succeed BEFORE browser work even when env is valid.
  let preflight: ReturnType<typeof validateRiverClickReceiptPath>
  try {
    preflight = validateRiverClickReceiptPath(receiptPath, realFs)
  } catch (error) {
    // Unsupported POSIX runtime (or any preflight throw): BLOCKED
    // RUNTIME_UNAVAILABLE; the bounded publisher reason is the secondary
    // message, never an arbitrary code as the terminal classifier.
    const reason = error instanceof RiverClickPublicationError ? error.message : 'receipt path preflight failed'
    return { ok: false, classification: 'BLOCKED', code: 'RUNTIME_UNAVAILABLE', receiptWritten: false, message: reason }
  }
  if (!preflight.ok) {
    // Filesystem safety refusal (noncanonical/non-0700/foreign/existing):
    // BLOCKED RUNTIME_UNAVAILABLE closed code + bounded publisher reason as the
    // secondary message, no new file, no overwrite.
    return { ok: false, classification: 'BLOCKED', code: 'RUNTIME_UNAVAILABLE', receiptWritten: false, message: preflight.message }
  }

  // 3. Config classification only AFTER the path is proven safe.
  const parsed = parseRiverClickConfig(env)
  if (parsed.ok) {
    // Fully valid config with a safe path: caller owns browser work/terminals.
    return { ok: true }
  }

  const endedAt = new Date().toISOString()
  // Closed classification is preserved EXACTLY as parsed: REQUIRED_ENV_MISSING
  // stays BLOCKED, CONFIG_INVALID stays FAIL. A build failure is fixed
  // PUBLICATION_FAILED, no receipt, and the classification is never rewritten.
  const classification: 'BLOCKED' | 'FAIL' = parsed.classification
  const failure = closedFailure(parsed)
  const origins = validOriginsOf(parsed, env)

  const receipt = buildRiverClickTerminalEvidence({
    startedAt,
    endedAt,
    frontendOrigin: origins.frontend,
    apiOrigin: origins.api,
    requestedFeature: null,
    renderedFeature: null,
    gfs: null,
    ifs: null,
    warmup: null,
    samples: [],
    failure,
  })
  if (!receipt.ok) {
    // NO relabel: the known classification is never rewritten, no receipt is
    // written, and no publication is attempted.
    return { ok: false, classification, code: 'PUBLICATION_FAILED', receiptWritten: false, message: 'river-click evidence terminal could not be built' }
  }
  // Exactly one publication attempt; a publisher failure is terminal (no retry).
  try {
    publication.publish(receiptPath, receipt.receipt)
  } catch (error) {
    if (error instanceof RiverClickPublicationError) {
      return { ok: false, classification, code: error.code, receiptWritten: false, message: error.message }
    }
    return { ok: false, classification, code: 'PUBLICATION_FAILED', receiptWritten: false, message: 'river-click evidence publication failed' }
  }
  return { ok: false, classification, code: parsed.code, receiptWritten: true, message: parsed.message }
}
