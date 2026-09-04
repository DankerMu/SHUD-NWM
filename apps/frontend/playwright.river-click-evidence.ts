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
 */

import fs from 'node:fs'
import path from 'node:path'

import {
  RIVER_CLICK_EVIDENCE_MAX_BYTES,
  RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
} from './src/lib/riverClickEvidence/constants'
import { validateRiverClickEvidenceDocument } from './src/lib/riverClickEvidence/receipt'
import type { RiverClickEvidence } from './src/lib/riverClickEvidence/receipt'

export { RIVER_CLICK_RECEIPT_FILENAME_PATTERN }

/** Narrow filesystem seam for deterministic failure tests; production uses realFsAdapter. */
export interface RiverClickEvidenceFs {
  lstatSync(p: string): fs.Stats
  statSync(p: string): fs.Stats
  realpathSync(p: string): string
  openSync(p: string, flags: number, mode?: number): number
  fstatSync(fd: number): fs.Stats
  fchmodSync(fd: number, mode: number): void
  writeSync(fd: number, buffer: Uint8Array): number
  fsyncSync(fd: number): void
  closeSync(fd: number): void
  linkSync(oldPath: string, newPath: string): void
  unlinkSync(p: string): void
  readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: number): number
}

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
export const realFsAdapter: RiverClickEvidenceFs = {
  lstatSync: (p) => fs.lstatSync(p),
  statSync: (p) => fs.statSync(p),
  realpathSync: (p) => fs.realpathSync(p),
  openSync: (p, flags, mode) => fs.openSync(p, flags, mode as number),
  fstatSync: (fd) => fs.fstatSync(fd),
  fchmodSync: (fd, mode) => fs.fchmodSync(fd, mode),
  writeSync: (fd, buffer) => fs.writeSync(fd, buffer),
  fsyncSync: (fd) => fs.fsyncSync(fd),
  closeSync: (fd) => fs.closeSync(fd),
  linkSync: (oldPath, newPath) => fs.linkSync(oldPath, newPath),
  unlinkSync: (p) => fs.unlinkSync(p),
  readSync: (fd, buffer, offset, length, position) => fs.readSync(fd, buffer, offset, length, position),
}

const O_EXCL = fs.constants.O_EXCL
const O_CREAT = fs.constants.O_CREAT
const O_WRONLY = fs.constants.O_WRONLY
const O_RDONLY = fs.constants.O_RDONLY
const O_DIRECTORY = fs.constants.O_DIRECTORY
const O_NOFOLLOW = fs.constants.O_NOFOLLOW
// O_CLOEXEC honesty: Linux exposes it as a real numeric flag; macOS Node does
// not (open(2) is close-on-exec by default, owned by libuv). We never claim a
// fabricated numeric flag on macOS and never substitute 0 silently for
// O_NOFOLLOW/O_DIRECTORY (those remain hard requirements).
const O_CLOEXEC: number = typeof (fs.constants as Record<string, unknown>).O_CLOEXEC === 'number'
  ? ((fs.constants as Record<string, unknown>).O_CLOEXEC as number)
  : 0

const TEMP_TOKEN_PATTERN = /^[0-9a-f]{32}$/

function requirePosixRuntime(): void {
  const missing: string[] = []
  if (typeof process.geteuid !== 'function') missing.push('geteuid')
  if (typeof O_NOFOLLOW !== 'number') missing.push('O_NOFOLLOW')
  if (typeof O_DIRECTORY !== 'number') missing.push('O_DIRECTORY')
  if (missing.length > 0) {
    throw new RiverClickPublicationError('RUNTIME_UNAVAILABLE', 'unsupported POSIX runtime: missing required os constants')
  }
}

/** POSIX geteuid; @types/node marks it optional (POSIX-only). */
function currentEuid(): number {
  if (typeof process.geteuid !== 'function') {
    throw new RiverClickPublicationError('RUNTIME_UNAVAILABLE', 'unsupported POSIX runtime: missing geteuid')
  }
  return process.geteuid()
}

interface ParentIdentity {
  dev: number
  ino: number
  uid: number
  mode: number
}

/** A descriptor/pathname identity proof: dev+ino+uid+exact mode (0o7777) + type. */
interface DescriptorIdentity {
  dev: number
  ino: number
  uid: number
  mode: number
  kind: 'file' | 'directory' | 'other'
}

function cryptoRandomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes)
  globalThis.crypto.getRandomValues(buffer)
  return Array.from(buffer, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** Random temp token injection seam for deterministic collision tests. */
export function randomTempName(finalBasename: string, token: string | null = null): string {
  if (token !== null && !TEMP_TOKEN_PATTERN.test(token)) {
    throw new RiverClickPublicationError('TEMP_CREATE_FAILED', 'injected temp token must be exactly 32 lowercase hex characters')
  }
  return `.${finalBasename}.tmp-${token ?? cryptoRandomHex(16)}`
}

function parentIdentity(info: fs.Stats): ParentIdentity {
  return { dev: info.dev, ino: info.ino, uid: info.uid, mode: info.mode & 0o7777 }
}

/** @types/node marks Stats methods optional; call defensively. */
function isRegularFile(info: fs.Stats): boolean {
  return typeof info.isFile === 'function' ? info.isFile() : (info.mode & 0o170000) === 0o100000
}

function isDirectory(info: fs.Stats): boolean {
  return typeof info.isDirectory === 'function' ? info.isDirectory() : (info.mode & 0o170000) === 0o040000
}

function descriptorIdentity(info: fs.Stats): DescriptorIdentity {
  return {
    dev: info.dev,
    ino: info.ino,
    uid: info.uid,
    mode: info.mode & 0o7777,
    kind: isRegularFile(info) ? 'file' : isDirectory(info) ? 'directory' : 'other',
  }
}

function sameIdentity(a: fs.Stats, b: fs.Stats): boolean {
  return a.dev === b.dev && a.ino === b.ino && a.uid === b.uid && (a.mode & 0o7777) === (b.mode & 0o7777)
}

function sameDescriptorIdentity(a: DescriptorIdentity, b: DescriptorIdentity): boolean {
  return (
    a.dev === b.dev &&
    a.ino === b.ino &&
    a.uid === b.uid &&
    a.mode === b.mode &&
    a.kind === b.kind
  )
}

function sameParentIdentity(a: fs.Stats, b: ParentIdentity): boolean {
  return a.dev === b.dev && a.ino === b.ino && a.uid === b.uid && (a.mode & 0o7777) === b.mode && isDirectory(a)
}

function sameExactTempIdentity(a: fs.Stats, b: fs.Stats): boolean {
  // dev/ino/uid, exact mode bits 0o7777, type, and nlink/size are compared.
  return (
    a.dev === b.dev &&
    a.ino === b.ino &&
    a.uid === b.uid &&
    (a.mode & 0o7777) === (b.mode & 0o7777) &&
    a.nlink === b.nlink &&
    a.size === b.size &&
    isRegularFile(a) &&
    isRegularFile(b)
  )
}

function isSafeAbsolutePath(receiptPath: string): boolean {
  if (typeof receiptPath !== 'string') return false
  if (!receiptPath.startsWith('/')) return false
  if (receiptPath.includes('\0')) return false
  const components = receiptPath.slice(1).split('/')
  if (components.some((component) => component === '' || component === '.' || component === '..')) return false
  return true
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
  if (!isSafeAbsolutePath(receiptPath)) {
    return {
      ok: false,
      code: 'RECEIPT_PATH_INVALID',
      message: 'receipt path must be an absolute lexically-normalized path without NUL bytes',
    }
  }
  const parent = path.dirname(receiptPath)
  const basename = path.basename(receiptPath)
  if (!RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test(basename)) {
    return {
      ok: false,
      code: 'RECEIPT_BASENAME_INVALID',
      message: 'receipt basename must match nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}.json',
    }
  }
  let resolved: string
  try {
    resolved = realFs.realpathSync(parent)
  } catch {
    return { ok: false, code: 'PARENT_UNAVAILABLE', message: 'receipt parent must exist' }
  }
  if (resolved !== parent) {
    return { ok: false, code: 'PARENT_NOT_CANONICAL', message: 'receipt parent must have no symlink path component' }
  }
  let parentInfo: fs.Stats
  try {
    parentInfo = realFs.lstatSync(parent)
  } catch {
    return { ok: false, code: 'PARENT_UNAVAILABLE', message: 'receipt parent must be an existing private directory' }
  }
  if (!isDirectory(parentInfo)) {
    return { ok: false, code: 'PARENT_NOT_DIRECTORY', message: 'receipt parent must be a directory' }
  }
  if (parentInfo.uid !== currentEuid()) {
    return { ok: false, code: 'PARENT_FOREIGN_OWNER', message: 'receipt parent must be owned by the current effective uid' }
  }
  // Exact mode 0700 including special bits (0o7777): sticky/setgid/setuid are
  // all refusals.
  if ((parentInfo.mode & 0o7777) !== 0o700) {
    return { ok: false, code: 'PARENT_MODE_INVALID', message: 'receipt parent mode must be exactly 0700' }
  }
  // The final path must be absent; a symlink or any existing entry is also a
  // no-clobber refusal (never follow a final-path symlink to a different file).
  try {
    realFs.lstatSync(receiptPath)
    return { ok: false, code: 'TARGET_EXISTS', message: 'receipt final path already exists; no-clobber evidence cannot overwrite it' }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      return { ok: false, code: 'TARGET_UNKNOWN', message: 'receipt final path status is unknown; refusing to proceed' }
    }
  }
  return { ok: true, parent, parentIdentity: parentIdentity(parentInfo) }
}

function serializeEvidence(receipt: RiverClickEvidence): Uint8Array {
  // Catch cyclic/BigInt/non-JSON values BEFORE the raw stringify can escape:
  // the semantic validator catches them too, but the stringify here must never
  // throw with a raw engine message.
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
 * Cleanup a temp pathname ONLY after the pinned parent (fd + pathname) is
 * revalidated and the current pathname dev/ino is proven equal to the
 * descriptor's. The caller passes a `recheckParent` closure that throws
 * PARENT_CHANGED on drift; every internal cleanup path (fchmod failure,
 * invalid-temp identity) goes through here so NO unlink can occur without the
 * pinned-parent recheck. On any uncertainty the object is preserved.
 */
function cleanupTempPath(
  tempPath: string,
  expectedInfo: fs.Stats,
  realFs: RiverClickEvidenceFs,
  recheckParent: () => void,
): void {
  try {
    recheckParent()
  } catch {
    // Parent drifted: preserve the uncertain object, do not unlink.
    return
  }
  let pathInfo: fs.Stats
  try {
    pathInfo = realFs.lstatSync(tempPath)
  } catch {
    return
  }
  if (!sameExactTempIdentity(expectedInfo, pathInfo)) return
  try {
    realFs.unlinkSync(tempPath)
  } catch {
    // preserve uncertain object
  }
}

function openNoClobberTemp(
  parent: string,
  tempName: string,
  realFs: RiverClickEvidenceFs,
  recheckParent: (() => void) | null = null,
): { fd: number; info: fs.Stats } {
  const tempPath = path.join(parent, tempName)
  let fd: number
  try {
    fd = realFs.openSync(tempPath, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
  } catch {
    throw new RiverClickPublicationError('TEMP_CREATE_FAILED', 'temporary receipt creation failed')
  }
  try {
    realFs.fchmodSync(fd, 0o600)
  } catch {
    let probe: fs.Stats | null = null
    try {
      probe = realFs.fstatSync(fd)
    } catch {
      probe = null
    }
    if (probe !== null && recheckParent !== null) {
      cleanupTempPath(tempPath, probe, realFs, recheckParent)
    }
    try {
      realFs.closeSync(fd)
    } catch {
      // ignore
    }
    throw new RiverClickPublicationError('FCHMOD_FAILED', 'temporary receipt fchmod failed')
  }
  let info: fs.Stats
  try {
    info = realFs.fstatSync(fd)
  } catch {
    // Close the temp fd and preserve the unprovable temp (its identity cannot
    // be proven, so unlinking it could delete someone else's object).
    try {
      realFs.closeSync(fd)
    } catch {
      // ignore
    }
    throw new RiverClickPublicationError('TEMP_IDENTITY_INVALID', 'temporary receipt fstat failed')
  }
  if (!isRegularFile(info) || info.nlink !== 1 || info.uid !== currentEuid() || (info.mode & 0o7777) !== 0o600) {
    // NEVER unlink without the parent recheck + current pathname proving the
    // same dev/ino as the descriptor: an adversary could have swapped the
    // pathname between fstat and unlink.
    if (recheckParent !== null) {
      cleanupTempPath(tempPath, info, realFs, recheckParent)
    }
    try {
      realFs.closeSync(fd)
    } catch {
      // ignore
    }
    throw new RiverClickPublicationError('TEMP_IDENTITY_INVALID', 'temporary receipt identity/mode differs')
  }
  return { fd, info }
}

function writeAll(fd: number, bytes: Uint8Array, realFs: RiverClickEvidenceFs): void {
  let offset = 0
  while (offset < bytes.byteLength) {
    let written: number
    try {
      written = realFs.writeSync(fd, bytes.subarray(offset))
    } catch {
      throw new RiverClickPublicationError('WRITE_FAILED', 'receipt write failed')
    }
    if (written <= 0) {
      throw new RiverClickPublicationError('WRITE_FAILED', 'receipt write made no progress')
    }
    offset += written
  }
}

function fsyncFd(fd: number, realFs: RiverClickEvidenceFs): void {
  try {
    realFs.fsyncSync(fd)
  } catch {
    throw new RiverClickPublicationError('FSYNC_FAILED', 'receipt fsync failed')
  }
}

function openParentReadOnly(parent: string, realFs: RiverClickEvidenceFs): number {
  try {
    return realFs.openSync(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
  } catch {
    throw new RiverClickPublicationError('PARENT_OPEN_FAILED', 'receipt parent open failed')
  }
}

/**
 * Revalidate the OPEN parent descriptor against the pinned identity. Called
 * before every path operation and fsync; a drift is PARENT_CHANGED.
 */
function recheckParentFd(
  parentFd: number,
  expected: ParentIdentity,
  expectedDescriptor: DescriptorIdentity,
  realFs: RiverClickEvidenceFs,
): void {
  let info: fs.Stats
  try {
    info = realFs.fstatSync(parentFd)
  } catch {
    throw new RiverClickPublicationError('PARENT_CHANGED', 'receipt parent descriptor became unreadable')
  }
  const actual = descriptorIdentity(info)
  if (
    actual.dev !== expected.dev ||
    actual.ino !== expected.ino ||
    actual.uid !== expected.uid ||
    actual.mode !== expected.mode ||
    actual.kind !== 'directory' ||
    !sameDescriptorIdentity(actual, expectedDescriptor)
  ) {
    throw new RiverClickPublicationError('PARENT_CHANGED', 'receipt parent identity changed across publication')
  }
}

/**
 * Revalidate the PATHNAME against the pinned parent fd identity (lstat). The
 * pathname must still resolve to the same (dev, ino, uid, mode, type) as the
 * open descriptor; a swap or replacement is PARENT_CHANGED.
 */
function recheckParentPath(
  parent: string,
  expected: ParentIdentity,
  realFs: RiverClickEvidenceFs,
): void {
  let info: fs.Stats
  try {
    info = realFs.lstatSync(parent)
  } catch {
    throw new RiverClickPublicationError('PARENT_CHANGED', 'receipt parent pathname became unreadable')
  }
  if (!sameParentIdentity(info, expected)) {
    throw new RiverClickPublicationError('PARENT_CHANGED', 'receipt parent identity changed across publication')
  }
}

function recheckParent(parent: string, expected: ParentIdentity, realFs: RiverClickEvidenceFs): void {
  recheckParentPath(parent, expected, realFs)
}

/**
 * Bounded O_NOFOLLOW final read: open O_RDONLY|O_NOFOLLOW, fstat and require
 * the exact temp (dev, ino, uid, exact mode 0600 with 0o7777, type, nlink 1,
 * size) before AND after reading, then decode UTF-8 fatally.
 */
function readFinal(
  receiptPath: string,
  realFs: RiverClickEvidenceFs,
  expected: fs.Stats,
  expectedBytes: number,
): string {
  let readFd: number
  try {
    readFd = realFs.openSync(receiptPath, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
  } catch {
    throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt open failed')
  }
  try {
    let before: fs.Stats
    try {
      before = realFs.fstatSync(readFd)
    } catch {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt fstat failed')
    }
    if (
      !isRegularFile(before) ||
      before.nlink !== 1 ||
      before.uid !== currentEuid() ||
      (before.mode & 0o7777) !== 0o600 ||
      before.dev !== expected.dev ||
      before.ino !== expected.ino ||
      before.size !== expectedBytes
    ) {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt identity/size differs at final read')
    }
    const buffer = new Uint8Array(expectedBytes)
    let position = 0
    while (position < buffer.byteLength) {
      let count: number
      try {
        count = realFs.readSync(readFd, buffer, position, buffer.byteLength - position, position)
      } catch {
        throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt read failed')
      }
      if (count <= 0) break
      position += count
    }
    if (position !== expectedBytes) {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt read is incomplete')
    }
    let after: fs.Stats
    try {
      after = realFs.fstatSync(readFd)
    } catch {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt re-fstat failed')
    }
    if (
      !isRegularFile(after) ||
      after.nlink !== 1 ||
      after.uid !== currentEuid() ||
      (after.mode & 0o7777) !== 0o600 ||
      after.dev !== expected.dev ||
      after.ino !== expected.ino ||
      after.size !== expectedBytes
    ) {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt identity changed during final read')
    }
    let text: string
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
    } catch {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt is not valid UTF-8')
    }
    return text
  } finally {
    try {
      realFs.closeSync(readFd)
    } catch {
      // ignore
    }
  }
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
  requirePosixRuntime()
  const realFs = options.fs ?? realFsAdapter
  const validated = validateRiverClickReceiptPath(receiptPath, realFs)
  if (!validated.ok) {
    console.error(`BLOCKED: river-click evidence publication preflight refused: ${validated.message}`)
    throw new RiverClickPublicationError(validated.code, validated.message)
  }
  const parent = validated.parent
  const pinnedParent = validated.parentIdentity
  const bytes = serializeEvidence(receipt)

  // Open and pin the parent descriptor BEFORE temp creation; it stays open
  // through publication (including all cleanup) and is the fsync fd. It is
  // closed in the outermost finally on EVERY terminal, including initial
  // fstat/recheck failure and invalid-token/temp-collision paths.
  const parentFd = openParentReadOnly(parent, realFs)
  let parentDescriptor: DescriptorIdentity
  try {
    parentDescriptor = descriptorIdentity(realFs.fstatSync(parentFd))
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs)
    recheckParentPath(parent, pinnedParent, realFs)
  } catch (error) {
    try {
      realFs.closeSync(parentFd)
    } catch {
      // ignore
    }
    if (error instanceof RiverClickPublicationError) throw error
    throw new RiverClickPublicationError('PARENT_CHANGED', 'receipt parent identity could not be verified')
  }

  let tempName: string
  try {
    tempName = randomTempName(path.basename(receiptPath), options.tempToken ?? null)
  } catch (error) {
    try {
      realFs.closeSync(parentFd)
    } catch {
      // ignore
    }
    throw error
  }
  const tempPath = path.join(parent, tempName)
  let fd: number | null = null
  let tempInfo: fs.Stats | null = null
  let committed = false
  let tempExists = false
  try {
    // EVERY internal temp cleanup path (fchmod failure, invalid temp identity)
    // revalidates the pinned parent (fd + pathname) immediately before its
    // lstat/unlink, so no unlink can run against a changed/foreign parent.
    const recheckPinnedParent = () => {
      recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs)
      recheckParentPath(parent, pinnedParent, realFs)
    }
    const created = openNoClobberTemp(parent, tempName, realFs, recheckPinnedParent)
    fd = created.fd
    tempInfo = created.info
    tempExists = true
    const tempFd = created.fd
    const originalTempInfo = created.info
    writeAll(tempFd, bytes, realFs)
    fsyncFd(tempFd, realFs)
    // After-write fstat must equal the ORIGINAL descriptor identity (dev/ino/
    // uid/exact mode/type/nlink/size).
    const afterWrite = realFs.fstatSync(tempFd)
    if (
      afterWrite.dev !== originalTempInfo.dev ||
      afterWrite.ino !== originalTempInfo.ino ||
      afterWrite.uid !== originalTempInfo.uid ||
      (afterWrite.mode & 0o7777) !== (originalTempInfo.mode & 0o7777) ||
      !isRegularFile(afterWrite) ||
      afterWrite.nlink !== 1 ||
      afterWrite.size !== bytes.byteLength
    ) {
      throw new RiverClickPublicationError('WRITE_FAILED', 'temporary receipt written bytes/identity differ')
    }
    // Keep the temp descriptor open through the link/identity proof.
    // Revalidate the pinned parent fd + pathname immediately before the
    // exclusive commit.
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs)
    recheckParentPath(parent, pinnedParent, realFs)
    try {
      realFs.linkSync(tempPath, receiptPath)
    } catch {
      throw new RiverClickPublicationError('LINK_FAILED', 'receipt link commit failed')
    }
    fsyncFd(parentFd, realFs)

    // Prove final and descriptor share (dev, ino), owner, exact mode 0600
    // (0o7777: sticky/setgid/setuid refuse), link count 2.
    const finalInfo = realFs.lstatSync(receiptPath)
    if (
      !isRegularFile(finalInfo) ||
      !sameIdentity(finalInfo, originalTempInfo) ||
      finalInfo.uid !== currentEuid() ||
      (finalInfo.mode & 0o7777) !== 0o600 ||
      finalInfo.nlink !== 2
    ) {
      throw new RiverClickPublicationError('COMMIT_IDENTITY_INVALID', 'committed receipt identity/mode differs')
    }
    // Full descriptor + pathname identity proof of the pinned parent
    // IMMEDIATELY before every normal temp unlink (same discipline as the
    // early fchmod/invalid-identity cleanup).
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs)
    recheckParentPath(parent, pinnedParent, realFs)

    // Unlink only the matching temp; fsync; prove final link count 1.
    const tempBeforeUnlink = realFs.lstatSync(tempPath)
    if (!sameIdentity(tempBeforeUnlink, originalTempInfo)) {
      throw new RiverClickPublicationError('CLEANUP_IDENTITY_MISMATCH', 'temp receipt identity changed; preserving uncertain object')
    }
    try {
      realFs.unlinkSync(tempPath)
      tempExists = false
    } catch {
      throw new RiverClickPublicationError('UNLINK_FAILED', 'temp receipt unlink failed')
    }
    fsyncFd(parentFd, realFs)

    const finalAfter = realFs.lstatSync(receiptPath)
    if (
      !isRegularFile(finalAfter) ||
      finalAfter.nlink !== 1 ||
      !sameIdentity(finalAfter, originalTempInfo) ||
      finalAfter.uid !== currentEuid() ||
      (finalAfter.mode & 0o7777) !== 0o600
    ) {
      throw new RiverClickPublicationError('COMMIT_IDENTITY_INVALID', 'committed receipt identity changed after cleanup')
    }
    recheckParent(parent, pinnedParent, realFs)

    // Terminal readback: O_NOFOLLOW open, exact identity/size/nlink proof
    // before and after, then revalidate the parsed bytes.
    const text = readFinal(receiptPath, realFs, originalTempInfo, bytes.byteLength)
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt is not valid JSON')
    }
    const validation = validateRiverClickEvidenceDocument(parsed)
    if (!validation.ok) {
      throw new RiverClickPublicationError('READBACK_FAILED', validation.reason)
    }
    if (text !== new TextDecoder('utf-8', { fatal: true }).decode(bytes)) {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt bytes differ from the validated payload')
    }
    if (JSON.stringify(parsed) !== JSON.stringify(receipt)) {
      throw new RiverClickPublicationError('READBACK_FAILED', 'published receipt bytes differ from the validated payload')
    }
    committed = true
  } finally {
    // Parent descriptor stays OPEN through temp cleanup: every cleanup lstat/
    // unlink happens while the parent fd is still pinned, and the parent is
    // revalidated (fd + pathname) immediately before the cleanup unlink. The
    // parent is closed LAST on every terminal.
    if (fd !== null) {
      try {
        realFs.closeSync(fd)
      } catch {
        // ignore
      }
    }
    if (!committed && tempExists && tempInfo !== null) {
      try {
        recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs)
        recheckParentPath(parent, pinnedParent, realFs)
        const current = realFs.lstatSync(tempPath)
        if (sameIdentity(current, tempInfo)) {
          realFs.unlinkSync(tempPath)
        }
      } catch {
        // Preserve the uncertain object; the caller still sees the failure.
      }
    }
    try {
      realFs.closeSync(parentFd)
    } catch {
      // ignore
    }
  }
  return { path: receiptPath }
}
