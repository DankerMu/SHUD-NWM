/**
 * In-process C4 receipt acceptance core (#1895).
 *
 * Node-20 stdlib only. Accepts EXACTLY the schema-1.0 PASS terminal the live
 * spec can publish. The descriptor stays open through semantic validation.
 */

import { closeSync, constants as fsConstants, fstatSync, lstatSync, openSync, readSync, realpathSync } from 'node:fs'
import path from 'node:path'

export const KNOWN_ARTIFACT = 'nhms-frontend-c4-live-evidence'
export const KNOWN_SCHEMA_VERSION = '1.0'
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

function twoXx(value) {
  return Number.isInteger(value) && value >= 200 && value <= 299
}

const HOME_READ_PATHS = new Set(['/api/v1/basins', '/api/v1/layers', '/api/v1/mvp/qhh/latest-product'])

function requireExactKeys(value, expected, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) refuse(`${label} is not an object`)
  const keys = Object.keys(value)
  if (keys.length !== expected.length || expected.some((key) => !keys.includes(key))) {
    refuse(`${label} key set is not exact`)
  }
}

function requireIdentity(value, label) {
  if (typeof value !== 'string' || value.length === 0 || Buffer.byteLength(value, 'utf8') > 256) {
    refuse(`${label} is not a safe identity`)
  }
  if (
    /^(?:https?|ftp|file|ws|wss):/i.test(value) ||
    /^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(value) ||
    /^[^\s/@]+@[^\s/@]+$/.test(value) ||
    /[?&](?:[a-z_][a-z0-9_]*=)/i.test(value)
  ) {
    refuse(`${label} is not a safe identity`)
  }
}

function validateProduct(product, source) {
  requireExactKeys(product, [
    'source_id', 'basin_id', 'basin_version_id', 'river_network_version_id',
    'run_id', 'model_id', 'cycle_time', 'scenario',
  ], `${source} product`)
  for (const key of ['source_id', 'basin_id', 'basin_version_id', 'river_network_version_id', 'run_id', 'model_id', 'scenario']) {
    requireIdentity(product[key], `${source}.${key}`)
  }
  if (product.source_id !== source) refuse(`${source} source is wrong`)
  if (source === 'GFS' && product.scenario !== 'forecast_gfs_deterministic') refuse('gfs scenario wrong')
  if (source === 'IFS' && product.scenario !== 'forecast_ifs_deterministic') refuse('ifs scenario wrong')
  if (parseStrictUtc(product.cycle_time) === null) refuse(`${source}.cycle_time is not strict UTC RFC3339`)
}

function validateOpsCheck(check, source) {
  requireExactKeys(check, [
    'source_id', 'path', 'heading_observed', 'permission_denied', 'runtime_unavailable',
    'status_status', 'stages_status', 'jobs_status', 'job_id', 'logs_status',
    'role_selector_count', 'retry_cancel_control_count', 'slurm_request_count',
    'non_get_control_count', 'queue_readonly_visible', 'operator_recovery_visible',
  ], `${source} ops check`)
  if (check.source_id !== source || check.path !== '/ops') refuse(`${source} ops path/source is wrong`)
  if (check.heading_observed !== true) refuse(`${source} ops heading was not observed`)
  if (check.permission_denied !== false || check.runtime_unavailable !== false) refuse(`${source} ops terminal is denied or unavailable`)
  if (!twoXx(check.status_status) || !twoXx(check.stages_status) || !twoXx(check.jobs_status) || !twoXx(check.logs_status)) {
    refuse(`${source} ops HTTP statuses are not 2xx`)
  }
  requireIdentity(check.job_id, `${source} job_id`)
  if (check.role_selector_count !== 0 || check.retry_cancel_control_count !== 0) refuse(`${source} control UI is present`)
  if (check.slurm_request_count !== 0 || check.non_get_control_count !== 0) refuse(`${source} control requests were observed`)
  if (check.queue_readonly_visible !== true || check.operator_recovery_visible !== true) refuse(`${source} readonly guidance is missing`)
}

const PASS_TOP_LEVEL_KEYS = [
  'artifact', 'schema_version', 'status', 'generated_at', 'started_at', 'ended_at',
  'origins', 'requested_pins', 'gfs', 'ifs', 'home', 'ops', 'source_switch', 'no_control', 'failure',
]

function validatePassDocument(doc, args, cmdStartMs, cmdEndMs) {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) refuse('receipt is not an object')
  const keys = Object.keys(doc)
  if (keys.length !== PASS_TOP_LEVEL_KEYS.length) refuse('receipt top-level key set is not exact')
  for (const key of PASS_TOP_LEVEL_KEYS) {
    if (!keys.includes(key)) refuse(`receipt missing top-level key ${key}`)
  }
  if (doc.artifact !== KNOWN_ARTIFACT) refuse('artifact identity differs from the known artifact')
  if (doc.schema_version !== KNOWN_SCHEMA_VERSION) refuse('schema version differs from 1.0')
  if (doc.status !== 'PASS') refuse('status must be PASS (got a non-PASS terminal)')
  if (doc.failure !== null) refuse('PASS receipt must not carry a failure')

  requireExactKeys(doc.origins, ['frontend', 'api'], 'origins')
  for (const slot of ['frontend', 'api']) {
    const value = doc.origins?.[slot]
    if (value === null || value === undefined) refuse(`origins.${slot} is null`)
    const norm = normalizedOrigin(value)
    if (norm === null) refuse(`origins.${slot} is not a normalized bare HTTP(S) origin`)
    const cfgNorm = normalizedOrigin(args[`${slot}-origin`])
    if (cfgNorm === null) refuse('a configured origin is not a normalized bare HTTP(S) origin')
    if (norm !== cfgNorm) refuse(`origins.${slot} does not bind to the configured origin`)
  }

  requireExactKeys(doc.requested_pins, ['basin_id', 'river_segment_id'], 'requested_pins')
  requireIdentity(doc.requested_pins.basin_id, 'requested_pins.basin_id')
  requireIdentity(doc.requested_pins.river_segment_id, 'requested_pins.river_segment_id')
  if (doc.requested_pins.basin_id !== args['basin-id']) refuse('requested_pins.basin_id does not bind to the pin')
  if (doc.requested_pins.river_segment_id !== args['segment-id']) refuse('requested_pins.river_segment_id does not bind to the pin')

  validateProduct(doc.gfs, 'GFS')
  validateProduct(doc.ifs, 'IFS')
  for (const slot of ['gfs', 'ifs']) {
    const product = doc[slot]
    if (product.basin_id !== doc.requested_pins.basin_id) refuse(`${slot}.basin_id does not bind to requested_pins`)
  }
  if (doc.gfs.basin_version_id !== doc.ifs.basin_version_id) refuse('GFS/IFS basin_version_id differ')
  if (doc.gfs.river_network_version_id !== doc.ifs.river_network_version_id) refuse('GFS/IFS river_network_version_id differ')
  if (doc.gfs.run_id === doc.ifs.run_id && doc.gfs.model_id === doc.ifs.model_id && doc.gfs.cycle_time === doc.ifs.cycle_time) {
    refuse('GFS/IFS identities are not distinct or source-bound')
  }

  requireExactKeys(doc.home, [
    'path', 'map_surface_visible', 'runtime_config_status', 'runtime_service_role',
    'current_read_observed', 'current_read_path',
  ], 'home')
  if (doc.home.path !== '/' || doc.home.map_surface_visible !== true) refuse('home map surface is not proven')
  if (doc.home.runtime_service_role !== 'display_readonly' || !twoXx(doc.home.runtime_config_status)) refuse('home runtime config is not display_readonly 2xx')
  if (doc.home.current_read_observed !== true || !HOME_READ_PATHS.has(doc.home.current_read_path)) refuse('home current display read is missing or invalid')

  requireExactKeys(doc.ops, ['gfs', 'ifs'], 'ops')
  validateOpsCheck(doc.ops.gfs, 'GFS')
  validateOpsCheck(doc.ops.ifs, 'IFS')
  if (doc.ops.gfs.job_id === doc.ops.ifs.job_id) refuse('GFS/IFS job_id are not source-bound')
  requireExactKeys(doc.source_switch, ['both_sources_completed', 'identities_distinct_or_source_bound'], 'source_switch')
  if (doc.source_switch.both_sources_completed !== true || doc.source_switch.identities_distinct_or_source_bound !== true) refuse('source switch is not proven')
  requireExactKeys(doc.no_control, ['slurm_request_count', 'non_get_control_count'], 'no_control')
  if (doc.no_control.slurm_request_count !== 0 || doc.no_control.non_get_control_count !== 0) {
    refuse('no_control facts are missing')
  }

  const started = parseStrictUtc(doc.started_at)
  const ended = parseStrictUtc(doc.ended_at)
  const generated = parseStrictUtc(doc.generated_at)
  if (started === null || ended === null || generated === null) refuse('timestamps are not strict UTC RFC3339 calendar-valid instants')
  if (!(started <= ended && ended === generated && doc.ended_at === doc.generated_at)) refuse('timestamps violate started <= ended == generated')
  if (started < cmdStartMs) refuse('receipt started_at is before CMD_START')
  if (ended > cmdEndMs) refuse('receipt ended_at is after CMD_END')
}

export function acceptC4Receipt(args, options = {}) {
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
    return { ok: true }
  } catch (error) {
    closeOwned(fsOps, fd)
    fd = null
    if (error instanceof BinderRefusal) return { ok: false, message: error.message }
    return { ok: false, message: 'receipt is not valid JSON' }
  }
}
