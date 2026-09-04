/**
 * In-process river-click receipt acceptance core (#1970 / #1895).
 *
 * Node-20 stdlib only. The CLI (`river-click-receipt-binder.mjs`) prints one
 * bounded `BINDER:` line and exits; tests import this module so pathname/fd
 * TOCTOU can be injected after initial facts and before open/post-read
 * rechecks. No production env-var backdoor and no test sleep.
 *
 * The receipt descriptor stays open through semantic validation. Every
 * success/error terminal closes it explicitly; process-exit autoclose is not
 * the oracle.
 */

import { closeSync, constants as fsConstants, fstatSync, lstatSync, openSync, readSync, realpathSync } from 'node:fs'
import path from 'node:path'

export const KNOWN_ARTIFACT = 'nhms-frontend-river-click-live-evidence'
export const KNOWN_SCHEMA_VERSION = '1.0'
export const THRESHOLD_MS = 2000
export const WARMUP_COUNT = 1
export const ACCEPTED_COUNT = 20
export const MAX_RECEIPT_BYTES = 262144

const UTC_TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z$/
const STRICT_DECIMAL = /^\d{1,10}$/
const S_IFMT = 0o170000
const S_IFREG = 0o100000
const S_IFDIR = 0o040000
const S_IFLNK = 0o120000

const O_RDONLY = fsConstants.O_RDONLY
const O_NOFOLLOW = fsConstants.O_NOFOLLOW
const O_CLOEXEC = typeof fsConstants.O_CLOEXEC === 'number' ? fsConstants.O_CLOEXEC : 0

export class BinderRefusal extends Error {
  constructor(message) {
    super(message)
    this.name = 'BinderRefusal'
  }
}

function isRegularFileMode(mode) {
  return (mode & S_IFMT) === S_IFREG
}

function isDirectoryMode(mode) {
  return (mode & S_IFMT) === S_IFDIR
}

function isSymlinkMode(mode) {
  return (mode & S_IFMT) === S_IFLNK
}

export function realBinderFs() {
  return {
    lstatSync: (p) => lstatSync(p),
    realpathSync: (p) => realpathSync(p),
    openSync: (p, flags) => openSync(p, flags),
    fstatSync: (fd) => fstatSync(fd),
    readSync: (fd, buffer, offset, length, position) => readSync(fd, buffer, offset, length, position),
    closeSync: (fd) => closeSync(fd),
    geteuid: () => {
      if (typeof process.geteuid !== 'function') throw new BinderRefusal('unsupported runtime: missing geteuid')
      return process.geteuid()
    },
  }
}

function refuse(message) {
  throw new BinderRefusal(message)
}

function closeOwned(fsOps, fd) {
  if (typeof fd !== 'number') return { closed: true }
  try {
    fsOps.closeSync(fd)
    return { closed: true }
  } catch {
    return { closed: false }
  }
}

function fileFacts(info) {
  return {
    mode: info.mode & 0o7777,
    uid: info.uid,
    nlink: info.nlink,
    size: info.size,
    dev: info.dev,
    ino: info.ino,
    mtimeMs: info.mtimeMs,
    ctimeMs: info.ctimeMs,
    mtimeSec: Math.floor(info.mtimeMs / 1000),
    isFile: typeof info.isFile === 'function' ? info.isFile() : isRegularFileMode(info.mode),
    isDir: typeof info.isDirectory === 'function' ? info.isDirectory() : isDirectoryMode(info.mode),
    isLink: typeof info.isSymbolicLink === 'function' ? info.isSymbolicLink() : isSymlinkMode(info.mode),
  }
}

function parentIdentity(facts) {
  return { dev: facts.dev, ino: facts.ino, uid: facts.uid, mode: facts.mode, isDir: facts.isDir }
}

function sameParentIdentity(a, b) {
  return a.dev === b.dev && a.ino === b.ino && a.uid === b.uid && a.mode === b.mode && a.isDir === true && b.isDir === true
}

function sameReceiptIdentity(a, b) {
  return (
    a.dev === b.dev &&
    a.ino === b.ino &&
    a.uid === b.uid &&
    a.mode === b.mode &&
    a.nlink === b.nlink &&
    a.size === b.size &&
    a.mtimeMs === b.mtimeMs &&
    a.ctimeMs === b.ctimeMs &&
    a.isFile === true &&
    b.isFile === true
  )
}

function lstatFacts(fsOps, filePath, message) {
  try {
    return fileFacts(fsOps.lstatSync(filePath))
  } catch (error) {
    if (error instanceof BinderRefusal) throw error
    refuse(message)
  }
}

function nearestRankP95(durations) {
  const sorted = [...durations].sort((a, b) => a - b)
  const index = Math.max(0, Math.ceil(sorted.length * 0.95) - 1)
  return sorted[index]
}

function normalizedOrigin(value) {
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) return null
  const scheme = parsed.protocol
  if (scheme !== 'https:' && scheme !== 'http:') return null
  const port = parsed.port || (scheme === 'https:' ? '443' : '80')
  return `${scheme}//${parsed.hostname}${port ? `:${port}` : ''}`
}

function parseStrictUtc(value) {
  if (typeof value !== 'string') return null
  const match = UTC_TIMESTAMP_PATTERN.exec(value)
  if (match === null) return null
  const year = Number.parseInt(match[1], 10)
  const month = Number.parseInt(match[2], 10)
  const day = Number.parseInt(match[3], 10)
  const hour = Number.parseInt(match[4], 10)
  const minute = Number.parseInt(match[5], 10)
  const second = Number.parseInt(match[6], 10)
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) return null
  const timestamp = Date.UTC(year, month - 1, day, hour, minute, second)
  if (!Number.isFinite(timestamp)) return null
  const replayed = new Date(timestamp)
  if (
    replayed.getUTCFullYear() !== year ||
    replayed.getUTCMonth() !== month - 1 ||
    replayed.getUTCDate() !== day ||
    replayed.getUTCHours() !== hour ||
    replayed.getUTCMinutes() !== minute ||
    replayed.getUTCSeconds() !== second
  ) {
    return null
  }
  return timestamp
}

function requireReceiptPath(receiptPath) {
  if (typeof receiptPath !== 'string' || !receiptPath.startsWith('/')) refuse('receipt path must be absolute')
  if (receiptPath.includes('\0')) refuse('receipt path must not contain NUL bytes')
  const pathParts = receiptPath.slice(1).split('/')
  if (pathParts.some((part) => part === '' || part === '.' || part === '..')) refuse('receipt path must be lexically normalized')
  return receiptPath
}

function requireCmdBracket(args) {
  if (typeof args['cmd-start'] !== 'string' || !STRICT_DECIMAL.test(args['cmd-start'])) refuse('CMD_START is not a bounded canonical decimal second value')
  if (typeof args['cmd-end'] !== 'string' || !STRICT_DECIMAL.test(args['cmd-end'])) refuse('CMD_END is not a bounded canonical decimal second value')
  const cmdStart = Number.parseInt(args['cmd-start'], 10)
  const cmdEnd = Number.parseInt(args['cmd-end'], 10)
  if (!Number.isSafeInteger(cmdStart) || !Number.isSafeInteger(cmdEnd) || cmdStart > cmdEnd) {
    refuse('CMD_START/CMD_END bracket is not a finite ordered integer pair')
  }
  return { cmdStart, cmdEnd, cmdStartMs: cmdStart * 1000, cmdEndMs: cmdEnd * 1000 }
}

function assertParentFacts(fsOps, parentPath, euid) {
  let parentCanonical
  try {
    parentCanonical = fsOps.realpathSync(parentPath)
  } catch (error) {
    if (error instanceof BinderRefusal) throw error
    refuse('receipt parent must exist')
  }
  if (parentCanonical !== parentPath) refuse('receipt parent must be canonical (no symlink component)')
  const parentFacts = lstatFacts(fsOps, parentPath, 'receipt parent must exist')
  if (parentFacts.uid !== euid) refuse('receipt parent euid differs')
  if (parentFacts.mode !== 0o700) refuse('receipt parent mode is not 700')
  if (!parentFacts.isDir) refuse('receipt parent is not a directory')
  return parentFacts
}

function recheckPinnedIdentities(fsOps, receiptPath, parentPath, pinnedParent, pinnedReceipt, fd) {
  let descriptor
  try {
    descriptor = fileFacts(fsOps.fstatSync(fd))
  } catch (error) {
    if (error instanceof BinderRefusal) throw error
    refuse('receipt descriptor restat failed')
  }
  if (!sameReceiptIdentity(descriptor, pinnedReceipt)) refuse('receipt identity changed during read')
  const pathAfter = lstatFacts(fsOps, receiptPath, 'receipt pathname restat failed')
  if (pathAfter.isLink || !pathAfter.isFile) refuse('receipt is not a regular file (symlink refused)')
  if (!sameReceiptIdentity(pathAfter, pinnedReceipt)) refuse('receipt pathname identity changed during read')
  let parentCanonical
  try {
    parentCanonical = fsOps.realpathSync(parentPath)
  } catch (error) {
    if (error instanceof BinderRefusal) throw error
    refuse('receipt parent identity changed during read')
  }
  if (parentCanonical !== parentPath) refuse('receipt parent must be canonical (no symlink component)')
  const parentAfter = lstatFacts(fsOps, parentPath, 'receipt parent identity changed during read')
  if (!sameParentIdentity(parentAfter, pinnedParent)) refuse('receipt parent identity changed during read')
}

function validatePassDocument(doc, args, cmdStartMs, cmdEndMs) {
  if (doc.artifact !== KNOWN_ARTIFACT) refuse('artifact identity differs from the known artifact')
  if (doc.schema_version !== KNOWN_SCHEMA_VERSION) refuse('schema version differs from 1.0')
  if (doc.status !== 'PASS') refuse('status must be PASS (got a non-PASS terminal)')
  if (doc.threshold_ms !== THRESHOLD_MS) refuse('threshold_ms is not 2000')
  if (doc.percentile_method !== 'nearest-rank') refuse('percentile_method is not nearest-rank')
  if (doc.warmup_count !== WARMUP_COUNT || doc.accepted_count !== ACCEPTED_COUNT) refuse('warmup_count/accepted_count is not 1/20')
  if (doc.failure !== null) refuse('PASS receipt must not carry a failure')

  for (const slot of ['frontend', 'api']) {
    const value = doc.origins?.[slot]
    if (value === null || value === undefined) refuse(`origins.${slot} is null`)
    const norm = normalizedOrigin(value)
    if (norm === null) refuse(`origins.${slot} is not a normalized bare HTTP(S) origin`)
    const cfgNorm = normalizedOrigin(args[`${slot}-origin`])
    if (cfgNorm === null) refuse('a configured origin is not a normalized bare HTTP(S) origin')
    if (norm !== cfgNorm) refuse(`origins.${slot} does not bind to the configured origin`)
  }

  for (const key of ['basin_id', 'river_segment_id', 'basin_version_id', 'river_network_version_id']) {
    if (doc.requested_feature?.[key] !== doc.rendered_feature?.[key]) refuse(`requested/rendered ${key} differ`)
  }
  if (doc.requested_feature?.basin_id !== args['basin-id']) refuse('requested_feature.basin_id does not bind to the pin')
  if (doc.requested_feature?.river_segment_id !== args['segment-id']) refuse('requested_feature.river_segment_id does not bind to the pin')

  for (const slot of ['gfs', 'ifs']) {
    const product = doc[slot]
    if (!product) refuse(`${slot} product is null on a PASS receipt`)
    for (const key of ['basin_id', 'basin_version_id', 'river_network_version_id']) {
      if (product[key] !== doc.requested_feature?.[key]) refuse(`${slot}.${key} does not bind to requested_feature`)
    }
  }
  if (doc.gfs.source_id !== 'GFS' || doc.gfs.scenario !== 'forecast_gfs_deterministic') refuse('gfs source/scenario wrong')
  if (doc.ifs.source_id !== 'IFS' || doc.ifs.scenario !== 'forecast_ifs_deterministic') refuse('ifs source/scenario wrong')

  const validStatus = (status) => Number.isInteger(status) && status >= 200 && status <= 299
  if (!doc.warmup || doc.warmup.index !== 0 || doc.warmup.discarded !== true) refuse('warmup is not the single discarded index-0 sample')
  if (!Number.isFinite(doc.warmup.duration_ms) || doc.warmup.duration_ms < 0) refuse('warmup duration is not a finite non-negative')
  if (!validStatus(doc.warmup.gfs_status) || !validStatus(doc.warmup.ifs_status)) refuse('warmup statuses are not 2xx integers')
  if (!Array.isArray(doc.samples) || doc.samples.length !== ACCEPTED_COUNT) refuse('samples length is not 20')
  for (let i = 0; i < doc.samples.length; i += 1) {
    const sample = doc.samples[i]
    if (sample.index !== i + 1) refuse(`samples[${i}].index is not consecutive`)
    if (!Number.isFinite(sample.duration_ms) || sample.duration_ms < 0) refuse(`samples[${i}].duration_ms is not a finite non-negative`)
    if (!validStatus(sample.gfs_status) || !validStatus(sample.ifs_status)) refuse(`samples[${i}].status is not a 2xx integer`)
  }
  const durations = doc.samples.map((sample) => sample.duration_ms)
  const recomputed = nearestRankP95(durations)
  if (recomputed !== doc.p95_ms) refuse('recomputed P95 does not match receipt p95_ms')
  if (!Number.isFinite(doc.p95_ms) || doc.p95_ms >= THRESHOLD_MS) refuse('PASS p95_ms is not finite and strictly below 2000')

  const started = parseStrictUtc(doc.started_at)
  const ended = parseStrictUtc(doc.ended_at)
  const generated = parseStrictUtc(doc.generated_at)
  if (started === null || ended === null || generated === null) refuse('timestamps are not strict UTC RFC3339 calendar-valid instants')
  if (!(started <= ended && ended === generated && doc.ended_at === doc.generated_at)) refuse('timestamps violate started <= ended == generated')
  if (started < cmdStartMs) refuse('receipt started_at is before CMD_START')
  if (ended > cmdEndMs) refuse('receipt ended_at is after CMD_END')
}

/**
 * Accept exactly one schema-1.0 PASS receipt from a bounded no-follow descriptor.
 * `hooks.afterPathnameFacts` runs after parent/receipt lstat and before open.
 * `hooks.afterOpen` runs after the identity-checked open and before the read.
 * `hooks.afterRead` runs after the bounded read and before post-read identity
 * revalidation. Hooks exist only as an in-process fs mutation seam.
 */
export function acceptRiverClickReceipt(args, options = {}) {
  const fsOps = options.fs ?? realBinderFs()
  const hooks = options.hooks ?? {}
  const missing = ['receipt', 'frontend-origin', 'api-origin', 'basin-id', 'segment-id', 'cmd-start', 'cmd-end'].filter((key) => !args[key])
  if (missing.length > 0) return { ok: false, message: 'missing required binder arguments' }
  if (typeof O_NOFOLLOW !== 'number') return { ok: false, message: 'unsupported runtime: missing O_NOFOLLOW' }
  const openFlags = O_RDONLY | O_NOFOLLOW | O_CLOEXEC

  let fd = null
  try {
    const receiptPath = requireReceiptPath(args.receipt)
    const { cmdStart, cmdEnd, cmdStartMs, cmdEndMs } = requireCmdBracket(args)
    const euid = fsOps.geteuid()
    const parentPath = path.dirname(receiptPath)
    const parentFacts = assertParentFacts(fsOps, parentPath, euid)
    const pinnedParent = parentIdentity(parentFacts)

    const receiptFacts = lstatFacts(fsOps, receiptPath, 'receipt file status is unreadable')
    if (receiptFacts.isLink || !receiptFacts.isFile) refuse('receipt is not a regular file (symlink refused)')
    if (receiptFacts.uid !== euid) refuse('receipt euid differs')
    if (receiptFacts.mode !== 0o600) refuse('receipt mode is not 600')
    if (receiptFacts.nlink !== 1) refuse('receipt nlink is not 1')
    if (!(receiptFacts.size >= 1 && receiptFacts.size <= MAX_RECEIPT_BYTES)) refuse('receipt size is outside the bounded ceiling')
    if (receiptFacts.mtimeSec < cmdStart) refuse('receipt mtime is before CMD_START')
    if (receiptFacts.mtimeSec > cmdEnd) refuse('receipt mtime is after CMD_END')

    if (typeof hooks.afterPathnameFacts === 'function') hooks.afterPathnameFacts({ receiptPath, parentPath, parentFacts, receiptFacts })

    try {
      fd = fsOps.openSync(receiptPath, openFlags)
    } catch (error) {
      if (error instanceof BinderRefusal) throw error
      refuse('receipt open failed')
    }
    let before
    try {
      before = fileFacts(fsOps.fstatSync(fd))
    } catch (error) {
      if (error instanceof BinderRefusal) throw error
      refuse('receipt descriptor status is unreadable')
    }
    if (!before.isFile) refuse('receipt is not a regular file (symlink refused)')
    if (before.uid !== euid) refuse('receipt euid differs')
    if (before.mode !== 0o600) refuse('receipt mode is not 600')
    if (before.nlink !== 1) refuse('receipt nlink is not 1')
    if (!(before.size >= 1 && before.size <= MAX_RECEIPT_BYTES)) refuse('receipt size is outside the bounded ceiling')
    if (!sameReceiptIdentity(before, receiptFacts)) refuse('receipt descriptor identity differs from the validated pathname')

    if (typeof hooks.afterOpen === 'function') hooks.afterOpen({ fd, receiptPath, parentPath, receiptFacts: before })

    const buffer = Buffer.alloc(before.size)
    let offset = 0
    while (offset < buffer.length) {
      let count
      try {
        count = fsOps.readSync(fd, buffer, offset, buffer.length - offset, offset)
      } catch (error) {
        if (error instanceof BinderRefusal) throw error
        refuse('receipt read failed')
      }
      if (count <= 0) break
      offset += count
    }
    if (offset !== before.size) refuse('receipt read is incomplete')

    if (typeof hooks.afterRead === 'function') hooks.afterRead({ fd, receiptPath, parentPath, buffer, receiptFacts: before })

    recheckPinnedIdentities(fsOps, receiptPath, parentPath, pinnedParent, before, fd)

    let text
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
    } catch {
      refuse('receipt is not valid UTF-8')
    }
    let doc
    try {
      doc = JSON.parse(text)
    } catch {
      refuse('receipt is not valid JSON')
    }
    validatePassDocument(doc, args, cmdStartMs, cmdEndMs)
    recheckPinnedIdentities(fsOps, receiptPath, parentPath, pinnedParent, before, fd)
    const closed = closeOwned(fsOps, fd)
    fd = null
    if (!closed.closed) refuse('receipt close failed')
    return { ok: true, p95: doc.p95_ms }
  } catch (error) {
    closeOwned(fsOps, fd)
    fd = null
    if (error instanceof BinderRefusal) return { ok: false, message: error.message }
    return { ok: false, message: 'receipt is not valid JSON' }
  }
}
