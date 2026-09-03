#!/usr/bin/env node
/**
 * River-click live evidence acceptance binder (#1970 execution runner #1895).
 *
 * Node-20 stdlib only (no runtime dependency): the node-27 runner and the
 * frontend CI test share this one binder, so a checked-in `.mjs` is simpler
 * than provisioning Python/uv in frontend CI. It accepts EXACTLY the schema-1.0
 * PASS terminal the live spec can publish, and independently recomputes the
 * nearest-rank P95 from the actual sample durations.
 *
 * Usage (set -euo pipefail context; the runbook wraps this in the command
 * bracket so CMD_START/CMD_END/mode/owner/nlink are checked by the shell
 * preamble/first half):
 *
 *   node apps/frontend/scripts/river-click-receipt-binder.mjs \
 *     --receipt "$RECEIPT" \
 *     --frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" \
 *     --api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" \
 *     --basin-id "$PLAYWRIGHT_LIVE_RIVER_BASIN_ID" \
 *     --segment-id "$PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID" \
 *     --cmd-start "$CMD_START" --cmd-end "$CMD_END"
 *
 * Failure mode: exit 1 with one bounded `BINDER:` line per violated fact; no
 * file is written or modified. The receipt itself is never trusted from its
 * declared counts — count/P95/identity coherence is recomputed here.
 *
 * Diagnostics are strictly bounded and fixed-shaped: the receipt path, OS
 * error text, configured origins, identities, and receipt values are NEVER
 * echoed. Success prints only `BINDER: PASS (p95_ms <closed number>)` — no
 * path.
 */

import { readFileSync, lstatSync, realpathSync } from 'node:fs'
import path from 'node:path'

const KNOWN_ARTIFACT = 'nhms-frontend-river-click-live-evidence'
const KNOWN_SCHEMA_VERSION = '1.0'
const THRESHOLD_MS = 2000
const WARMUP_COUNT = 1
const ACCEPTED_COUNT = 20
// Strict UTC RFC3339 shape: YYYY-MM-DDTHH:MM:SS(.fraction)?Z
const UTC_TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z$/

/** One bounded fixed-shaped diagnostic; exit 1. NEVER echoes untrusted input. */
function fail(message) {
  process.stderr.write(`BINDER: ${message}\n`)
  process.exit(1)
}

function parseArgs(argv) {
  const out = {}
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (!arg.startsWith('--')) continue
    const key = arg.slice(2)
    const value = argv[i + 1]
    if (value === undefined || value.startsWith('--')) continue
    out[key] = value
    i += 1
  }
  return out
}

const args = parseArgs(process.argv.slice(2))
const missing = ['receipt', 'frontend-origin', 'api-origin', 'basin-id', 'segment-id', 'cmd-start', 'cmd-end'].filter((key) => !args[key])
if (missing.length > 0) {
  fail('missing required binder arguments')
}

// The supplied receipt path must ALREADY be absolute and lexically normalized
// (no path.resolve, which silently accepts a relative path).
const receiptPath = args.receipt
if (!receiptPath.startsWith('/')) fail('receipt path must be absolute')
if (receiptPath.includes('\0')) fail('receipt path must not contain NUL bytes')
const pathParts = receiptPath.slice(1).split('/')
if (pathParts.some((part) => part === '' || part === '.' || part === '..')) fail('receipt path must be lexically normalized')

/** Nearest-rank P95 of at least one duration: index Math.ceil(n*0.95)-1. */
function nearestRankP95(durations) {
  const sorted = [...durations].sort((a, b) => a - b)
  const index = Math.max(0, Math.ceil(sorted.length * 0.95) - 1)
  return sorted[index]
}

function euid() {
  if (typeof process.geteuid !== 'function') fail('unsupported runtime: missing geteuid')
  return process.geteuid()
}

/** lstat so a symlinked receipt is a refusal (no following). */
function statFacts(filePath) {
  try {
    const s = lstatSync(filePath)
    return {
      mode: s.mode & 0o7777,
      uid: s.uid,
      nlink: s.nlink,
      mtimeSec: Math.floor(s.mtimeMs / 1000),
      size: s.size,
      isFile: s.isFile(),
      isDir: s.isDirectory(),
      isLink: s.isSymbolicLink(),
    }
  } catch (error) {
    // Bounded fixed diagnostic: never echo the OS error text/path.
    fail('receipt file status is unreadable')
  }
}

// --- 0. parent facts first: canonical existing parent with NO symlink
// component (realpath(parent) === parent), euid-owned, exact 0700, directory.
const parentPath = path.dirname(receiptPath)
let parentCanonical = null
try {
  parentCanonical = realpathSync(parentPath)
} catch (error) {
  fail('receipt parent must exist')
}
if (parentCanonical !== parentPath) fail('receipt parent must be canonical (no symlink component)')
const parentFacts = statFacts(parentPath)
if (parentFacts.uid !== euid()) fail('receipt parent euid differs')
if (parentFacts.mode !== 0o700) fail('receipt parent mode is not 700')
if (!parentFacts.isDir) fail('receipt parent is not a directory')

// Strict bounded canonical non-negative decimal command seconds: a malformed
// value like `123junk` is a refusal (never a permissive parseInt acceptance).
const STRICT_DECIMAL = /^\d{1,10}$/
if (typeof args['cmd-start'] !== 'string' || !STRICT_DECIMAL.test(args['cmd-start'])) fail('CMD_START is not a bounded canonical decimal second value')
if (typeof args['cmd-end'] !== 'string' || !STRICT_DECIMAL.test(args['cmd-end'])) fail('CMD_END is not a bounded canonical decimal second value')
const cmdStart = Number.parseInt(args['cmd-start'], 10)
const cmdEnd = Number.parseInt(args['cmd-end'], 10)
if (!Number.isSafeInteger(cmdStart) || !Number.isSafeInteger(cmdEnd) || cmdStart > cmdEnd) fail('CMD_START/CMD_END bracket is not a finite ordered integer pair')
const cmdStartMs = cmdStart * 1000
const cmdEndMs = cmdEnd * 1000

// --- 1. file facts: regular (not symlink), euid-owned, exact 0600, nlink 1,
// non-empty, mtime inside the command bracket ---
const receiptFacts = statFacts(receiptPath)
if (receiptFacts.isLink || !receiptFacts.isFile) fail('receipt is not a regular file (symlink refused)')
if (receiptFacts.uid !== euid()) fail('receipt euid differs')
if (receiptFacts.mode !== 0o600) fail('receipt mode is not 600')
if (receiptFacts.nlink !== 1) fail('receipt nlink is not 1')
if (receiptFacts.size <= 0) fail('receipt is empty')
if (receiptFacts.mtimeSec < cmdStart) fail('receipt mtime is before CMD_START')
if (receiptFacts.mtimeSec > cmdEnd) fail('receipt mtime is after CMD_END')

// --- 2. semantic facts: artifact/version/status/counts/identity/P95 recompute
// from the actual bytes. The JSON Schema itself is validated by the separate
// `check-jsonschema --schemafile` command in the same binder block (this
// binder is stdlib-only and cannot provision that CLI). ---
let doc
try {
  doc = JSON.parse(readFileSync(receiptPath, 'utf8'))
} catch {
  fail('receipt is not valid JSON')
}
// PASS-only acceptance: this binder accepts the live P95 PASS receipt. A
// complete THRESHOLD_EXCEEDED FAIL (1+20, p95>=2000) is NOT a PASS and MUST be
// rejected — the execution runner's gate is PASS-only.
if (doc.artifact !== KNOWN_ARTIFACT) fail('artifact identity differs from the known artifact')
if (doc.schema_version !== KNOWN_SCHEMA_VERSION) fail('schema version differs from 1.0')
if (doc.status !== 'PASS') fail('status must be PASS (got a non-PASS terminal)')
if (doc.threshold_ms !== THRESHOLD_MS) fail('threshold_ms is not 2000')
if (doc.percentile_method !== 'nearest-rank') fail('percentile_method is not nearest-rank')
if (doc.warmup_count !== WARMUP_COUNT || doc.accepted_count !== ACCEPTED_COUNT) fail('warmup_count/accepted_count is not 1/20')
if (doc.failure !== null) fail('PASS receipt must not carry a failure')

// Origins must be non-null normalized bare HTTP(S) origins that BIND to the
// configured values (default HTTP/HTTPS ports normalized before comparison;
// FTP and every other scheme are refused).
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
for (const slot of ['frontend', 'api']) {
  const value = doc.origins?.[slot]
  if (value === null || value === undefined) fail(`origins.${slot} is null`)
  const norm = normalizedOrigin(value)
  if (norm === null) fail(`origins.${slot} is not a normalized bare HTTP(S) origin`)
  const cfgNorm = normalizedOrigin(args[`${slot}-origin`])
  if (cfgNorm === null) fail('a configured origin is not a normalized bare HTTP(S) origin')
  if (norm !== cfgNorm) fail(`origins.${slot} does not bind to the configured origin`)
}

// Feature identity: requested==rendered on ALL FOUR fields; both bind to the pin.
for (const key of ['basin_id', 'river_segment_id', 'basin_version_id', 'river_network_version_id']) {
  if (doc.requested_feature?.[key] !== doc.rendered_feature?.[key]) fail(`requested/rendered ${key} differ`)
}
if (doc.requested_feature?.basin_id !== args['basin-id']) fail('requested_feature.basin_id does not bind to the pin')
if (doc.requested_feature?.river_segment_id !== args['segment-id']) fail('requested_feature.river_segment_id does not bind to the pin')

// Product identity: GFS/IFS bind to the same basin/version/network as requested.
for (const slot of ['gfs', 'ifs']) {
  const product = doc[slot]
  if (!product) fail(`${slot} product is null on a PASS receipt`)
  for (const key of ['basin_id', 'basin_version_id', 'river_network_version_id']) {
    if (product[key] !== doc.requested_feature?.[key]) fail(`${slot}.${key} does not bind to requested_feature`)
  }
}
if (doc.gfs.source_id !== 'GFS' || doc.gfs.scenario !== 'forecast_gfs_deterministic') fail('gfs source/scenario wrong')
if (doc.ifs.source_id !== 'IFS' || doc.ifs.scenario !== 'forecast_ifs_deterministic') fail('ifs source/scenario wrong')

// Warmup + samples: one discarded warmup index 0, exactly 20 consecutive 1..20,
// every status a finite integer 200..299, every duration finite non-negative.
const validStatus = (status) => Number.isInteger(status) && status >= 200 && status <= 299
if (!doc.warmup || doc.warmup.index !== 0 || doc.warmup.discarded !== true) fail('warmup is not the single discarded index-0 sample')
if (!Number.isFinite(doc.warmup.duration_ms) || doc.warmup.duration_ms < 0) fail('warmup duration is not a finite non-negative')
if (!validStatus(doc.warmup.gfs_status) || !validStatus(doc.warmup.ifs_status)) fail('warmup statuses are not 2xx integers')
if (!Array.isArray(doc.samples) || doc.samples.length !== ACCEPTED_COUNT) fail('samples length is not 20')
for (let i = 0; i < doc.samples.length; i += 1) {
  const sample = doc.samples[i]
  if (sample.index !== i + 1) fail(`samples[${i}].index is not consecutive`)
  if (!Number.isFinite(sample.duration_ms) || sample.duration_ms < 0) fail(`samples[${i}].duration_ms is not a finite non-negative`)
  if (!validStatus(sample.gfs_status) || !validStatus(sample.ifs_status)) fail(`samples[${i}].status is not a 2xx integer`)
}
const durations = doc.samples.map((sample) => sample.duration_ms)
const recomputed = nearestRankP95(durations)
if (recomputed !== doc.p95_ms) fail('recomputed P95 does not match receipt p95_ms')
if (!Number.isFinite(doc.p95_ms) || doc.p95_ms >= THRESHOLD_MS) fail('PASS p95_ms is not finite and strictly below 2000')

// Timestamp bracket: started <= ended == generated, all STRICT UTC RFC3339
// calendar-valid instants (never permissive Date.parse — `2026-02-30T00:00:00Z`
// rolls into March and must be refused), AND bound to the command bracket
// (started_at >= CMD_START, ended/generated <= CMD_END).
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
const started = parseStrictUtc(doc.started_at)
const ended = parseStrictUtc(doc.ended_at)
const generated = parseStrictUtc(doc.generated_at)
if (started === null || ended === null || generated === null) fail('timestamps are not strict UTC RFC3339 calendar-valid instants')
if (!(started <= ended && ended === generated && doc.ended_at === doc.generated_at)) fail('timestamps violate started <= ended == generated')
if (started < cmdStartMs) fail('receipt started_at is before CMD_START')
if (ended > cmdEndMs) fail('receipt ended_at is after CMD_END')

// Bounded fixed success line: closed p95 number only, no receipt path.
process.stdout.write(`BINDER: PASS (p95_ms ${doc.p95_ms})\n`)
