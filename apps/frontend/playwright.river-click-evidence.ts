/**
 * Node-only POSIX owner for the river-click live evidence receipt (#1970).
 * Browser/application code never imports node:fs. This module is executed by
 * the live Playwright lane only.
 *
 * Trust boundary: the receipt's already-existing canonical parent must be a
 * private euid-owned mode-0700 directory with no symlink component. The parent
 * is OPENED and PINNED (O_DIRECTORY|O_NOFOLLOW|O_RDONLY) BEFORE temp creation
 * and kept open through publication; every path operation revalidates the open
 * fd's (dev, ino, uid, exact mode, type) against the preflight identity, and
 * the pathname is lstat-revalidated against that fd at documented boundaries.
 * The same parent fd is used for the directory fsync. Publication is
 * no-clobber and link-first: `link(temp, final)` is the exclusive commit;
 * EEXIST and every other uncertainty fail without touching the winner.
 *
 * The POSIX primitive lives in playwright.private-receipt-publication.ts so
 * the C4 display lane can reuse the same no-clobber/fsync/readback contract
 * without weakening this river-click wrapper.
 */

import {
  RIVER_CLICK_EVIDENCE_MAX_BYTES,
  RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
} from './src/lib/riverClickEvidence/constants'
import { validateRiverClickEvidenceDocument } from './src/lib/riverClickEvidence/receipt'
import type { RiverClickEvidence } from './src/lib/riverClickEvidence/receipt'
import {
  publishPrivateReceipt,
  randomPrivateReceiptTempName,
  realPrivateReceiptFs,
  validatePrivateReceiptPath,
  type ParentIdentity,
  type PrivateReceiptFs,
} from './playwright.private-receipt-publication'

export { RIVER_CLICK_RECEIPT_FILENAME_PATTERN }

/** Narrow filesystem seam for deterministic failure tests; production uses realFsAdapter. */
export type RiverClickEvidenceFs = PrivateReceiptFs

export class RiverClickPublicationError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`)
    this.name = 'RiverClickPublicationError'
  }
}

/** Real fs adapter used by production callers. */
export const realFsAdapter: RiverClickEvidenceFs = realPrivateReceiptFs

function createError(code: string, message: string): RiverClickPublicationError {
  return new RiverClickPublicationError(code, message)
}

const BASENAME_MESSAGE = 'receipt basename must match nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}.json'

/** Random temp token injection seam for deterministic collision tests. */
export function randomTempName(finalBasename: string, token: string | null = null): string {
  return randomPrivateReceiptTempName(finalBasename, token, createError)
}

/**
 * Validate the receipt path and its existing canonical parent under the
 * explicit trust boundary. Returns the pinned parent identity for rechecks.
 */
export function validateRiverClickReceiptPath(
  receiptPath: string,
  realFs: RiverClickEvidenceFs,
):
  | { ok: true; parent: string; parentIdentity: ParentIdentity }
  | { ok: false; code: string; message: string } {
  return validatePrivateReceiptPath(receiptPath, realFs, {
    filenamePattern: RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
    basenameMessage: BASENAME_MESSAGE,
    createError,
  })
}

function serializeEvidence(receipt: RiverClickEvidence): Uint8Array {
  let serialized: string
  try {
    serialized = JSON.stringify(receipt)
  } catch {
    throw new RiverClickPublicationError('INVALID_PAYLOAD', 'evidence payload is not JSON-serializable')
  }
  const bytes = new TextEncoder().encode(serialized)
  if (bytes.byteLength > RIVER_CLICK_EVIDENCE_MAX_BYTES) {
    throw new RiverClickPublicationError(
      'PAYLOAD_OVER_CEILING',
      'evidence payload exceeds the byte ceiling',
    )
  }
  const validation = validateRiverClickEvidenceDocument(receipt)
  if (!validation.ok) {
    throw new RiverClickPublicationError('INVALID_PAYLOAD', validation.reason)
  }
  return bytes
}

/**
 * Exclusive no-clobber publication of exactly one schema-valid mode-0600
 * receipt. Throws RiverClickPublicationError on every failure path; never
 * deletes, replaces, or reuses an older artifact. When the receipt path itself
 * is missing or unsafe, emits one bounded BLOCKED: diagnostic to stderr and
 * writes no file.
 */
export function publishRiverClickEvidence(
  receiptPath: string,
  receipt: RiverClickEvidence,
  options: { fs?: RiverClickEvidenceFs; tempToken?: string } = {},
): { path: string } {
  return publishPrivateReceipt({
    receiptPath,
    bytes: serializeEvidence(receipt),
    originalDocument: receipt,
    filenamePattern: RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
    basenameMessage: BASENAME_MESSAGE,
    blockedDiagnostic: (message) => `BLOCKED: river-click evidence publication preflight refused: ${message}`,
    createError,
    isPublicationError: (error): error is RiverClickPublicationError => error instanceof RiverClickPublicationError,
    validateParsed: (parsed) => validateRiverClickEvidenceDocument(parsed),
    fs: options.fs,
    tempToken: options.tempToken ?? null,
  })
}
