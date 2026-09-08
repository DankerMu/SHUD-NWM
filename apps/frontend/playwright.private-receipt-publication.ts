/**
 * Generic Node-only POSIX private-receipt publication primitive.
 * Browser/application code never imports node:fs. Lane-specific publishers
 * (river-click, C4 display) wrap this module with their own error class,
 * filename grammar, and document validator. The POSIX trust boundary itself
 * is shared and must not be weakened per lane.
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

export interface PrivateReceiptFs {
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

export type PrivateReceiptErrorFactory = (code: string, message: string) => Error
export type PrivateReceiptErrorGuard = (error: unknown) => boolean

export class PrivateReceiptPublicationError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`)
    this.name = 'PrivateReceiptPublicationError'
  }
}

export const realPrivateReceiptFs: PrivateReceiptFs = {
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
const O_CLOEXEC: number = typeof (fs.constants as Record<string, unknown>).O_CLOEXEC === 'number'
  ? ((fs.constants as Record<string, unknown>).O_CLOEXEC as number)
  : 0

const TEMP_TOKEN_PATTERN = /^[0-9a-f]{32}$/

export interface ParentIdentity {
  dev: number
  ino: number
  uid: number
  mode: number
}

interface DescriptorIdentity {
  dev: number
  ino: number
  uid: number
  mode: number
  kind: 'file' | 'directory' | 'other'
}

function defaultError(code: string, message: string): Error {
  return new PrivateReceiptPublicationError(code, message)
}

function requirePosixRuntime(createError: PrivateReceiptErrorFactory): void {
  const missing: string[] = []
  if (typeof process.geteuid !== 'function') missing.push('geteuid')
  if (typeof O_NOFOLLOW !== 'number') missing.push('O_NOFOLLOW')
  if (typeof O_DIRECTORY !== 'number') missing.push('O_DIRECTORY')
  if (missing.length > 0) {
    throw createError('RUNTIME_UNAVAILABLE', 'unsupported POSIX runtime: missing required os constants')
  }
}

function currentEuid(createError: PrivateReceiptErrorFactory): number {
  if (typeof process.geteuid !== 'function') {
    throw createError('RUNTIME_UNAVAILABLE', 'unsupported POSIX runtime: missing geteuid')
  }
  return process.geteuid()
}

function cryptoRandomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes)
  globalThis.crypto.getRandomValues(buffer)
  return Array.from(buffer, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function randomPrivateReceiptTempName(
  finalBasename: string,
  token: string | null = null,
  createError: PrivateReceiptErrorFactory = defaultError,
): string {
  if (token !== null && !TEMP_TOKEN_PATTERN.test(token)) {
    throw createError('TEMP_CREATE_FAILED', 'injected temp token must be exactly 32 lowercase hex characters')
  }
  return `.${finalBasename}.tmp-${token ?? cryptoRandomHex(16)}`
}

function parentIdentity(info: fs.Stats): ParentIdentity {
  return { dev: info.dev, ino: info.ino, uid: info.uid, mode: info.mode & 0o7777 }
}

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

export function isSafeAbsoluteReceiptPath(receiptPath: string): boolean {
  if (typeof receiptPath !== 'string') return false
  if (!receiptPath.startsWith('/')) return false
  if (receiptPath.includes('\0')) return false
  const components = receiptPath.slice(1).split('/')
  if (components.some((component) => component === '' || component === '.' || component === '..')) return false
  return true
}

export function validatePrivateReceiptPath(
  receiptPath: string,
  realFs: PrivateReceiptFs,
  options: {
    filenamePattern: RegExp
    basenameMessage: string
    createError?: PrivateReceiptErrorFactory
  },
):
  | { ok: true; parent: string; parentIdentity: ParentIdentity }
  | { ok: false; code: string; message: string } {
  const createError = options.createError ?? defaultError
  if (!isSafeAbsoluteReceiptPath(receiptPath)) {
    return {
      ok: false,
      code: 'RECEIPT_PATH_INVALID',
      message: 'receipt path must be an absolute lexically-normalized path without NUL bytes',
    }
  }
  const parent = path.dirname(receiptPath)
  const basename = path.basename(receiptPath)
  if (!options.filenamePattern.test(basename)) {
    return {
      ok: false,
      code: 'RECEIPT_BASENAME_INVALID',
      message: options.basenameMessage,
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
  if (parentInfo.uid !== currentEuid(createError)) {
    return { ok: false, code: 'PARENT_FOREIGN_OWNER', message: 'receipt parent must be owned by the current effective uid' }
  }
  if ((parentInfo.mode & 0o7777) !== 0o700) {
    return { ok: false, code: 'PARENT_MODE_INVALID', message: 'receipt parent mode must be exactly 0700' }
  }
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

function cleanupTempPath(
  tempPath: string,
  expectedInfo: fs.Stats,
  realFs: PrivateReceiptFs,
  recheckParent: () => void,
): void {
  try {
    recheckParent()
  } catch {
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
  realFs: PrivateReceiptFs,
  createError: PrivateReceiptErrorFactory,
  recheckParent: (() => void) | null = null,
): { fd: number; info: fs.Stats } {
  const tempPath = path.join(parent, tempName)
  let fd: number
  try {
    fd = realFs.openSync(tempPath, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
  } catch {
    throw createError('TEMP_CREATE_FAILED', 'temporary receipt creation failed')
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
    throw createError('FCHMOD_FAILED', 'temporary receipt fchmod failed')
  }
  let info: fs.Stats
  try {
    info = realFs.fstatSync(fd)
  } catch {
    try {
      realFs.closeSync(fd)
    } catch {
      // ignore
    }
    throw createError('TEMP_IDENTITY_INVALID', 'temporary receipt fstat failed')
  }
  if (!isRegularFile(info) || info.nlink !== 1 || info.uid !== currentEuid(createError) || (info.mode & 0o7777) !== 0o600) {
    if (recheckParent !== null) {
      cleanupTempPath(tempPath, info, realFs, recheckParent)
    }
    try {
      realFs.closeSync(fd)
    } catch {
      // ignore
    }
    throw createError('TEMP_IDENTITY_INVALID', 'temporary receipt identity/mode differs')
  }
  return { fd, info }
}

function writeAll(fd: number, bytes: Uint8Array, realFs: PrivateReceiptFs, createError: PrivateReceiptErrorFactory): void {
  let offset = 0
  while (offset < bytes.byteLength) {
    let written: number
    try {
      written = realFs.writeSync(fd, bytes.subarray(offset))
    } catch {
      throw createError('WRITE_FAILED', 'receipt write failed')
    }
    if (written <= 0) {
      throw createError('WRITE_FAILED', 'receipt write made no progress')
    }
    offset += written
  }
}

function fsyncFd(fd: number, realFs: PrivateReceiptFs, createError: PrivateReceiptErrorFactory): void {
  try {
    realFs.fsyncSync(fd)
  } catch {
    throw createError('FSYNC_FAILED', 'receipt fsync failed')
  }
}

function openParentReadOnly(parent: string, realFs: PrivateReceiptFs, createError: PrivateReceiptErrorFactory): number {
  try {
    return realFs.openSync(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
  } catch {
    throw createError('PARENT_OPEN_FAILED', 'receipt parent open failed')
  }
}

function recheckParentFd(
  parentFd: number,
  expected: ParentIdentity,
  expectedDescriptor: DescriptorIdentity,
  realFs: PrivateReceiptFs,
  createError: PrivateReceiptErrorFactory,
): void {
  let info: fs.Stats
  try {
    info = realFs.fstatSync(parentFd)
  } catch {
    throw createError('PARENT_CHANGED', 'receipt parent descriptor became unreadable')
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
    throw createError('PARENT_CHANGED', 'receipt parent identity changed across publication')
  }
}

function recheckParentPath(
  parent: string,
  expected: ParentIdentity,
  realFs: PrivateReceiptFs,
  createError: PrivateReceiptErrorFactory,
): void {
  let info: fs.Stats
  try {
    info = realFs.lstatSync(parent)
  } catch {
    throw createError('PARENT_CHANGED', 'receipt parent pathname became unreadable')
  }
  if (!sameParentIdentity(info, expected)) {
    throw createError('PARENT_CHANGED', 'receipt parent identity changed across publication')
  }
}

function readFinal(
  receiptPath: string,
  realFs: PrivateReceiptFs,
  expected: fs.Stats,
  expectedBytes: number,
  createError: PrivateReceiptErrorFactory,
): string {
  let readFd: number
  try {
    readFd = realFs.openSync(receiptPath, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
  } catch {
    throw createError('READBACK_FAILED', 'published receipt open failed')
  }
  try {
    let before: fs.Stats
    try {
      before = realFs.fstatSync(readFd)
    } catch {
      throw createError('READBACK_FAILED', 'published receipt fstat failed')
    }
    if (
      !isRegularFile(before) ||
      before.nlink !== 1 ||
      before.uid !== currentEuid(createError) ||
      (before.mode & 0o7777) !== 0o600 ||
      before.dev !== expected.dev ||
      before.ino !== expected.ino ||
      before.size !== expectedBytes
    ) {
      throw createError('READBACK_FAILED', 'published receipt identity/size differs at final read')
    }
    const buffer = new Uint8Array(expectedBytes)
    let position = 0
    while (position < buffer.byteLength) {
      let count: number
      try {
        count = realFs.readSync(readFd, buffer, position, buffer.byteLength - position, position)
      } catch {
        throw createError('READBACK_FAILED', 'published receipt read failed')
      }
      if (count <= 0) break
      position += count
    }
    if (position !== expectedBytes) {
      throw createError('READBACK_FAILED', 'published receipt read is incomplete')
    }
    let after: fs.Stats
    try {
      after = realFs.fstatSync(readFd)
    } catch {
      throw createError('READBACK_FAILED', 'published receipt re-fstat failed')
    }
    if (
      !isRegularFile(after) ||
      after.nlink !== 1 ||
      after.uid !== currentEuid(createError) ||
      (after.mode & 0o7777) !== 0o600 ||
      after.dev !== expected.dev ||
      after.ino !== expected.ino ||
      after.size !== expectedBytes
    ) {
      throw createError('READBACK_FAILED', 'published receipt identity changed during final read')
    }
    let text: string
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
    } catch {
      throw createError('READBACK_FAILED', 'published receipt is not valid UTF-8')
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

export interface PublishPrivateReceiptInput {
  receiptPath: string
  bytes: Uint8Array
  originalDocument: unknown
  filenamePattern: RegExp
  basenameMessage: string
  blockedDiagnostic: (message: string) => string
  publicErrorMessage?: (code: string, message: string) => string
  createError: PrivateReceiptErrorFactory
  isPublicationError: PrivateReceiptErrorGuard
  validateParsed: (parsed: unknown) => { ok: true } | { ok: false; reason: string }
  fs?: PrivateReceiptFs
  tempToken?: string | null
}

/**
 * Exclusive no-clobber publication of exactly one mode-0600 receipt.
 * Throws the caller-provided error factory on every failure path; never
 * deletes, replaces, or reuses an older artifact. When the receipt path itself
 * is missing or unsafe, emits one bounded BLOCKED diagnostic to stderr and
 * writes no file.
 */
export function publishPrivateReceipt(input: PublishPrivateReceiptInput): { path: string } {
  const createError = input.createError
  requirePosixRuntime(createError)
  const realFs = input.fs ?? realPrivateReceiptFs
  const validated = validatePrivateReceiptPath(input.receiptPath, realFs, {
    filenamePattern: input.filenamePattern,
    basenameMessage: input.basenameMessage,
    createError,
  })
  if (!validated.ok) {
    console.error(input.blockedDiagnostic(validated.message))
    throw createError(
      validated.code,
      input.publicErrorMessage?.(validated.code, validated.message) ?? validated.message,
    )
  }
  const parent = validated.parent
  const pinnedParent = validated.parentIdentity
  const bytes = input.bytes

  const parentFd = openParentReadOnly(parent, realFs, createError)
  let parentDescriptor: DescriptorIdentity
  try {
    parentDescriptor = descriptorIdentity(realFs.fstatSync(parentFd))
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs, createError)
    recheckParentPath(parent, pinnedParent, realFs, createError)
  } catch (error) {
    try {
      realFs.closeSync(parentFd)
    } catch {
      // ignore
    }
    if (input.isPublicationError(error)) throw error
    throw createError('PARENT_CHANGED', 'receipt parent identity could not be verified')
  }

  let tempName: string
  try {
    tempName = randomPrivateReceiptTempName(path.basename(input.receiptPath), input.tempToken ?? null, createError)
  } catch (error) {
    try {
      realFs.closeSync(parentFd)
    } catch {
      // ignore
    }
    if (input.isPublicationError(error)) throw error
    throw createError('TEMP_CREATE_FAILED', 'temporary receipt name could not be created')
  }
  const tempPath = path.join(parent, tempName)
  let fd: number | null = null
  let tempInfo: fs.Stats | null = null
  let committed = false
  let tempExists = false
  try {
    const recheckPinnedParent = () => {
      recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs, createError)
      recheckParentPath(parent, pinnedParent, realFs, createError)
    }
    const created = openNoClobberTemp(parent, tempName, realFs, createError, recheckPinnedParent)
    fd = created.fd
    tempInfo = created.info
    tempExists = true
    const tempFd = created.fd
    const originalTempInfo = created.info
    writeAll(tempFd, bytes, realFs, createError)
    fsyncFd(tempFd, realFs, createError)
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
      throw createError('WRITE_FAILED', 'temporary receipt written bytes/identity differ')
    }
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs, createError)
    recheckParentPath(parent, pinnedParent, realFs, createError)
    try {
      realFs.linkSync(tempPath, input.receiptPath)
    } catch {
      throw createError('LINK_FAILED', 'receipt link commit failed')
    }
    fsyncFd(parentFd, realFs, createError)

    const finalInfo = realFs.lstatSync(input.receiptPath)
    if (
      !isRegularFile(finalInfo) ||
      !sameIdentity(finalInfo, originalTempInfo) ||
      finalInfo.uid !== currentEuid(createError) ||
      (finalInfo.mode & 0o7777) !== 0o600 ||
      finalInfo.nlink !== 2
    ) {
      throw createError('COMMIT_IDENTITY_INVALID', 'committed receipt identity/mode differs')
    }
    recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs, createError)
    recheckParentPath(parent, pinnedParent, realFs, createError)

    const tempBeforeUnlink = realFs.lstatSync(tempPath)
    if (!sameIdentity(tempBeforeUnlink, originalTempInfo)) {
      throw createError('CLEANUP_IDENTITY_MISMATCH', 'temp receipt identity changed; preserving uncertain object')
    }
    try {
      realFs.unlinkSync(tempPath)
      tempExists = false
    } catch {
      throw createError('UNLINK_FAILED', 'temp receipt unlink failed')
    }
    fsyncFd(parentFd, realFs, createError)

    const finalAfter = realFs.lstatSync(input.receiptPath)
    if (
      !isRegularFile(finalAfter) ||
      finalAfter.nlink !== 1 ||
      !sameIdentity(finalAfter, originalTempInfo) ||
      finalAfter.uid !== currentEuid(createError) ||
      (finalAfter.mode & 0o7777) !== 0o600
    ) {
      throw createError('COMMIT_IDENTITY_INVALID', 'committed receipt identity changed after cleanup')
    }
    recheckParentPath(parent, pinnedParent, realFs, createError)

    const text = readFinal(input.receiptPath, realFs, originalTempInfo, bytes.byteLength, createError)
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      throw createError('READBACK_FAILED', 'published receipt is not valid JSON')
    }
    const validation = input.validateParsed(parsed)
    if (!validation.ok) {
      throw createError('READBACK_FAILED', validation.reason)
    }
    if (text !== new TextDecoder('utf-8', { fatal: true }).decode(bytes)) {
      throw createError('READBACK_FAILED', 'published receipt bytes differ from the validated payload')
    }
    if (JSON.stringify(parsed) !== JSON.stringify(input.originalDocument)) {
      throw createError('READBACK_FAILED', 'published receipt bytes differ from the validated payload')
    }
    committed = true
  } catch (error) {
    if (input.isPublicationError(error)) throw error
    throw createError('PARENT_CHANGED', 'receipt publication encountered an unknown filesystem error')
  } finally {
    if (fd !== null) {
      try {
        realFs.closeSync(fd)
      } catch {
        // ignore
      }
    }
    if (!committed && tempExists && tempInfo !== null) {
      try {
        recheckParentFd(parentFd, pinnedParent, parentDescriptor, realFs, createError)
        recheckParentPath(parent, pinnedParent, realFs, createError)
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
  return { path: input.receiptPath }
}
