/**
 * Node-only POSIX owner for the dedicated #1895 C4 live display evidence receipt.
 * Wraps the shared private-publication primitive; the river-click wrapper stays
 * a separate semantic owner and never accepts a C4 receipt.
 */

import { C4_EVIDENCE_MAX_BYTES, C4_RECEIPT_FILENAME_PATTERN } from './src/lib/c4DisplayEvidence/constants'
import { validateC4EvidenceDocument, type C4Evidence } from './src/lib/c4DisplayEvidence/receipt'
import {
  publishPrivateReceipt,
  randomPrivateReceiptTempName,
  realPrivateReceiptFs,
  validatePrivateReceiptPath,
  type ParentIdentity,
  type PrivateReceiptFs,
} from './playwright.private-receipt-publication'

export { C4_RECEIPT_FILENAME_PATTERN }

export type C4EvidenceFs = PrivateReceiptFs

export class C4PublicationError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`)
    this.name = 'C4PublicationError'
  }
}

export const realC4FsAdapter: C4EvidenceFs = realPrivateReceiptFs

function createError(code: string, message: string): C4PublicationError {
  return new C4PublicationError(code, message)
}

const BASENAME_MESSAGE = 'receipt basename must match nhms-frontend-c4-live-evidence-[A-Za-z0-9._-]{1,96}.json'

export function randomC4TempName(finalBasename: string, token: string | null = null): string {
  return randomPrivateReceiptTempName(finalBasename, token, createError)
}

export function validateC4ReceiptPath(
  receiptPath: string,
  realFs: C4EvidenceFs,
):
  | { ok: true; parent: string; parentIdentity: ParentIdentity }
  | { ok: false; code: string; message: string } {
  return validatePrivateReceiptPath(receiptPath, realFs, {
    filenamePattern: C4_RECEIPT_FILENAME_PATTERN,
    basenameMessage: BASENAME_MESSAGE,
    createError,
  })
}

function serializeEvidence(receipt: C4Evidence): Uint8Array {
  let serialized: string
  try {
    serialized = JSON.stringify(receipt)
  } catch {
    throw new C4PublicationError('INVALID_PAYLOAD', 'evidence payload is not JSON-serializable')
  }
  const bytes = new TextEncoder().encode(serialized)
  if (bytes.byteLength > C4_EVIDENCE_MAX_BYTES) {
    throw new C4PublicationError('PAYLOAD_OVER_CEILING', 'evidence payload exceeds the byte ceiling')
  }
  const validation = validateC4EvidenceDocument(receipt)
  if (!validation.ok) {
    throw new C4PublicationError('INVALID_PAYLOAD', validation.reason)
  }
  return bytes
}

export function publishC4Evidence(
  receiptPath: string,
  receipt: C4Evidence,
  options: { fs?: C4EvidenceFs; tempToken?: string } = {},
): { path: string } {
  return publishPrivateReceipt({
    receiptPath,
    bytes: serializeEvidence(receipt),
    originalDocument: receipt,
    filenamePattern: C4_RECEIPT_FILENAME_PATTERN,
    basenameMessage: BASENAME_MESSAGE,
    blockedDiagnostic: () => 'BLOCKED: C4 evidence publication preflight refused',
    publicErrorMessage: () => 'C4 evidence publication preflight refused',
    createError,
    isPublicationError: (error): error is C4PublicationError => error instanceof C4PublicationError,
    validateParsed: (parsed) => validateC4EvidenceDocument(parsed),
    fs: options.fs,
    tempToken: options.tempToken ?? null,
  })
}
