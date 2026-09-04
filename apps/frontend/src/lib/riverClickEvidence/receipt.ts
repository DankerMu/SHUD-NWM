import {
  RIVER_CLICK_ACCEPTED_SAMPLES,
  RIVER_CLICK_ARTIFACT,
  RIVER_CLICK_ARRAY_MAX_LENGTH,
  RIVER_CLICK_BLOCKED_CODES,
  RIVER_CLICK_EVIDENCE_MAX_BYTES,
  RIVER_CLICK_FAILURE_CODE_MAX_BYTES,
  RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES,
  RIVER_CLICK_FAIL_CODES,
  RIVER_CLICK_GFS_SCENARIO,
  RIVER_CLICK_IDENTITY_MAX_BYTES,
  RIVER_CLICK_IFS_SCENARIO,
  RIVER_CLICK_JSON_MAX_DEPTH,
  RIVER_CLICK_OBJECT_MAX_WIDTH,
  RIVER_CLICK_PERCENTILE_METHOD,
  RIVER_CLICK_SCHEMA_VERSION,
  RIVER_CLICK_THRESHOLD_MS,
  RIVER_CLICK_VARIABLE,
  RIVER_CLICK_WARMUP,
  type RiverClickFailureCode,
  type RiverClickFailureStage,
} from './constants'
import { nearestRankP95, validateRiverClickDurations } from './timing'
import { normalizeRiverClickCycleTime } from './preflight'

export {
  RIVER_CLICK_ACCEPTED_SAMPLES,
  RIVER_CLICK_ARTIFACT,
  RIVER_CLICK_THRESHOLD_MS,
  RIVER_CLICK_WARMUP,
} from './constants'

/** Schema-1.0 river-click live evidence document. */
export interface RiverClickFeatureIdentity {
  basinId: string
  riverSegmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
}

export interface RiverClickProductIdentity {
  sourceId: 'GFS' | 'IFS'
  basinId: string
  basinVersionId: string
  riverNetworkVersionId: string
  runId: string
  modelId: string
  cycleTime: string
  scenario: string
}

export interface RiverClickSample {
  index: number
  durationMs: number
  gfsStatus: number
  ifsStatus: number
}

export interface RiverClickWarmupSample extends RiverClickSample {
  index: 0
  discarded: true
}

export interface RiverClickFailure {
  code: RiverClickFailureCode
  stage: RiverClickFailureStage
  sampleIndex: number | null
  gfsStatus: number | null
  ifsStatus: number | null
  message: string
}

/** Wire shape with snake_case fields exactly as published. */
export interface RiverClickEvidence {
  artifact: string
  schema_version: string
  status: 'PASS' | 'FAIL' | 'BLOCKED'
  generated_at: string
  started_at: string
  ended_at: string
  threshold_ms: number
  percentile_method: string
  warmup_count: number
  accepted_count: number
  origins: { frontend: string | null; api: string | null }
  requested_feature: {
    basin_id: string
    river_segment_id: string
    basin_version_id: string
    river_network_version_id: string
  } | null
  rendered_feature: {
    basin_id: string
    river_segment_id: string
    basin_version_id: string
    river_network_version_id: string
  } | null
  gfs: {
    source_id: string
    basin_id: string
    basin_version_id: string
    river_network_version_id: string
    run_id: string
    model_id: string
    cycle_time: string
    scenario: string
  } | null
  ifs: {
    source_id: string
    basin_id: string
    basin_version_id: string
    river_network_version_id: string
    run_id: string
    model_id: string
    cycle_time: string
    scenario: string
  } | null
  warmup: { index: 0; duration_ms: number; discarded: true; gfs_status: number; ifs_status: number } | null
  samples: Array<{ index: number; duration_ms: number; gfs_status: number; ifs_status: number }>
  p95_ms: number | null
  failure: {
    code: string
    stage: string
    sample_index: number | null
    gfs_status: number | null
    ifs_status: number | null
    message: string
  } | null
}

export interface RiverClickPassInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string
  apiOrigin: string
  requestedFeature: RiverClickFeatureIdentity
  renderedFeature: RiverClickFeatureIdentity
  gfs: RiverClickProductIdentity
  ifs: RiverClickProductIdentity
  warmup: { index: 0; durationMs: number; gfsStatus: number; ifsStatus: number } | null
  samples: RiverClickSample[]
}

export interface RiverClickTerminalInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string | null
  apiOrigin: string | null
  requestedFeature?: RiverClickFeatureIdentity | null
  renderedFeature?: RiverClickFeatureIdentity | null
  gfs?: RiverClickProductIdentity | null
  ifs?: RiverClickProductIdentity | null
  warmup?: { index: 0; durationMs: number; gfsStatus: number; ifsStatus: number } | null
  samples?: RiverClickSample[]
  failure: RiverClickFailure
}

export type RiverClickBuildResult =
  | { ok: true; receipt: RiverClickEvidence }
  | { ok: false; reason: string }

function rfc3339UtcNow(): string {
  return new Date().toISOString()
}

function productWire(product: RiverClickProductIdentity | null): RiverClickEvidence['gfs'] {
  if (product === null) return null
  // The wire cycle_time must be the canonical toISOString instant the frontend
  // sends as issue_time and the request matcher compares. Invalid input yields
  // a null product (callers return {ok:false}); it never throws anywhere in
  // the builder chain.
  const canonicalCycleTime = normalizeRiverClickCycleTime(product.cycleTime)
  if (canonicalCycleTime === null) return null
  return {
    source_id: product.sourceId,
    basin_id: product.basinId,
    basin_version_id: product.basinVersionId,
    river_network_version_id: product.riverNetworkVersionId,
    run_id: product.runId,
    model_id: product.modelId,
    cycle_time: canonicalCycleTime,
    scenario: product.scenario,
  }
}

function featureWire(feature: RiverClickFeatureIdentity | null) {
  if (feature === null) return null
  return {
    basin_id: feature.basinId,
    river_segment_id: feature.riverSegmentId,
    basin_version_id: feature.basinVersionId,
    river_network_version_id: feature.riverNetworkVersionId,
  }
}

/**
 * Validate a product identity for building: the canonical cycle must be strict
 * and the identity must round-trip through JSON serialization (cyclic/BigInt/
 * non-JSON values are refused, never thrown). Returns a canonical product or
 * null; builders treat null as {ok:false}.
 */
function canonicalProduct(product: RiverClickProductIdentity): RiverClickProductIdentity | null {
  try {
    const serialized = JSON.stringify(product)
    if (typeof serialized !== 'string') return null
    const parsed = JSON.parse(serialized) as RiverClickProductIdentity
    if (
      typeof parsed.cycleTime !== 'string' ||
      typeof parsed.basinId !== 'string' ||
      typeof parsed.sourceId !== 'string'
    ) {
      return null
    }
  } catch {
    return null
  }
  if (normalizeRiverClickCycleTime(product.cycleTime) === null) return null
  return product
}

function sampleWire(sample: RiverClickSample) {
  return {
    index: sample.index,
    duration_ms: sample.durationMs,
    gfs_status: sample.gfsStatus,
    ifs_status: sample.ifsStatus,
  }
}

function warmupWire(
  warmup: RiverClickPassInput['warmup'],
): RiverClickEvidence['warmup'] {
  if (warmup === null) return null
  return {
    index: 0,
    duration_ms: warmup.durationMs,
    discarded: true,
    gfs_status: warmup.gfsStatus,
    ifs_status: warmup.ifsStatus,
  }
}

function baseDocument(input: {
  startedAt: string
  endedAt: string
  frontendOrigin: string | null
  apiOrigin: string | null
  requestedFeature: RiverClickFeatureIdentity | null
  renderedFeature: RiverClickFeatureIdentity | null
  gfs: RiverClickProductIdentity | null
  ifs: RiverClickProductIdentity | null
  warmup: ReturnType<typeof warmupWire>
  samples: Array<{ index: number; duration_ms: number; gfs_status: number; ifs_status: number }>
  p95Ms: number | null
  failure: RiverClickEvidence['failure']
  status: 'PASS' | 'FAIL' | 'BLOCKED'
}): RiverClickEvidence {
  return {
    artifact: RIVER_CLICK_ARTIFACT,
    schema_version: RIVER_CLICK_SCHEMA_VERSION,
    status: input.status,
    generated_at: rfc3339UtcNow(),
    started_at: input.startedAt,
    ended_at: input.endedAt,
    threshold_ms: RIVER_CLICK_THRESHOLD_MS,
    percentile_method: RIVER_CLICK_PERCENTILE_METHOD,
    warmup_count: input.warmup === null ? 0 : RIVER_CLICK_WARMUP,
    accepted_count: input.samples.length,
    origins: { frontend: input.frontendOrigin, api: input.apiOrigin },
    requested_feature: featureWire(input.requestedFeature),
    rendered_feature: featureWire(input.renderedFeature),
    gfs: productWire(input.gfs),
    ifs: productWire(input.ifs),
    warmup: input.warmup,
    samples: input.samples,
    p95_ms: input.p95Ms,
    failure: input.failure,
  }
}

function withGeneratedAt(receipt: RiverClickEvidence): RiverClickEvidence {
  // generated_at must equal ended_at; the builder sets both from the ended instant.
  return { ...receipt, ended_at: receipt.ended_at, generated_at: receipt.ended_at }
}

function failureWireCode(failure: RiverClickFailure): string {
  return failure.code
}

/**
 * Build the exact schema-1.0 PASS document: exactly 20 finite non-negative
 * samples, one complete discarded warmup, and P95 recomputed nearest-rank from
 * the actual durations — never an injected value.
 */
export function buildRiverClickPassEvidence(input: RiverClickPassInput): RiverClickBuildResult {
  if (input.warmup === null) return { ok: false, reason: 'PASS requires exactly one complete discarded warmup' }
  const durationValidation = validateRiverClickDurations(input.samples.map((sample) => sample.durationMs))
  if (!durationValidation.ok) return { ok: false, reason: durationValidation.reason }
  const samples = input.samples.map(sampleWire)
  const p95 = nearestRankP95(input.samples.map((sample) => sample.durationMs)) as number
  // Never throw on invalid product input: builders return {ok:false}.
  const gfs = canonicalProduct(input.gfs)
  const ifs = canonicalProduct(input.ifs)
  if (gfs === null || ifs === null) return { ok: false, reason: 'product cycle_time is not a strict canonical RFC3339 instant' }
  const receipt = baseDocument({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: input.frontendOrigin,
    apiOrigin: input.apiOrigin,
    requestedFeature: input.requestedFeature,
    renderedFeature: input.renderedFeature,
    gfs,
    ifs,
    warmup: warmupWire(input.warmup),
    samples,
    p95Ms: p95,
    failure: null,
    status: 'PASS',
  })
  const withTime = withGeneratedAt(receipt)
  const validated = validateRiverClickEvidenceDocument(withTime)
  if (!validated.ok) return { ok: false, reason: validated.reason }
  return { ok: true, receipt: withTime }
}

/**
 * Closed terminal classifier. BLOCKED requires no warmup/sample/P95 claim and
 * a BLOCKED code; FAIL carries FAIL codes, normally p95_ms=null, and only a
 * fully completed THRESHOLD_EXCEEDED MAY carry the exact recomputed p95_ms.
 * A fully completed THRESHOLD_EXCEEDED must carry the same non-null identities
 * the PASS document would (a threshold FAIL published without any identity is
 * a false-PASS shape and is refused).
 */
export function buildRiverClickTerminalEvidence(input: RiverClickTerminalInput): RiverClickBuildResult {
  const samples = (input.samples ?? []).map(sampleWire)
  const blocked = (RIVER_CLICK_BLOCKED_CODES as readonly string[]).includes(input.failure.code)
  const isFailCode = (RIVER_CLICK_FAIL_CODES as readonly string[]).includes(input.failure.code)
  if (!blocked && !isFailCode) {
    return { ok: false, reason: `failure code ${input.failure.code} is not in the closed BLOCKED/FAIL sets` }
  }
  if (blocked) {
    // A BLOCKED terminal must not claim any sample/P95; warmup may be explicitly
    // null (the live spec always passes warmup:null) which is "no claim".
    if ((input.samples ?? []).length !== 0) {
      return { ok: false, reason: 'BLOCKED terminal must not carry sample/warmup/P95 claims' }
    }
    if (input.warmup !== undefined && input.warmup !== null) {
      return { ok: false, reason: 'BLOCKED terminal must not carry a non-null warmup claim' }
    }
  }
  let p95: number | null = null
  if (!blocked && input.failure.code === 'THRESHOLD_EXCEEDED') {
    // Code/stage binding: the completed threshold shape lives at stage
    // 'threshold' with a null sample index; anything else is mislabeled.
    if (input.failure.stage !== 'threshold') {
      return { ok: false, reason: 'THRESHOLD_EXCEEDED failure must be classified at stage threshold' }
    }
    if (input.failure.sampleIndex !== null) {
      return { ok: false, reason: 'THRESHOLD_EXCEEDED failure must not carry a per-sample index' }
    }
    const durations = (input.samples ?? []).map((sample) => sample.durationMs)
    const validation = validateRiverClickDurations(durations)
    if (!validation.ok) return { ok: false, reason: `THRESHOLD_EXCEEDED requires exactly 20 completed samples: ${validation.reason}` }
    if (input.requestedFeature === null || input.renderedFeature === null || input.gfs === null || input.ifs === null || input.warmup === undefined || input.warmup === null) {
      return { ok: false, reason: 'THRESHOLD_EXCEEDED requires the same non-null identity/warmup as a PASS document' }
    }
    p95 = nearestRankP95(durations)
  }
  const warmupWireValue = input.warmup === undefined ? null : warmupWire(input.warmup)
  // Builders must never throw on invalid product input: canonicalize first.
  const gfs = input.gfs === null || input.gfs === undefined ? null : canonicalProduct(input.gfs)
  const ifs = input.ifs === null || input.ifs === undefined ? null : canonicalProduct(input.ifs)
  if ((input.gfs !== null && input.gfs !== undefined && gfs === null) || (input.ifs !== null && input.ifs !== undefined && ifs === null)) {
    return { ok: false, reason: 'product cycle_time is not a strict canonical RFC3339 instant' }
  }
  const receipt = baseDocument({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: input.frontendOrigin,
    apiOrigin: input.apiOrigin,
    requestedFeature: input.requestedFeature ?? null,
    renderedFeature: input.renderedFeature ?? null,
    gfs,
    ifs,
    warmup: warmupWireValue,
    samples,
    p95Ms: p95,
    failure: {
      code: failureWireCode(input.failure),
      stage: input.failure.stage,
      sample_index: input.failure.sampleIndex,
      gfs_status: input.failure.gfsStatus,
      ifs_status: input.failure.ifsStatus,
      message: input.failure.message,
    },
    status: blocked ? 'BLOCKED' : 'FAIL',
  })
  const withTime = withGeneratedAt(receipt)
  const validated = validateRiverClickEvidenceDocument(withTime)
  if (!validated.ok) return { ok: false, reason: validated.reason }
  return { ok: true, receipt: withTime }
}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function messageCapped(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && byteLength(value) <= RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES
}

function codeCapped(value: unknown): value is string {
  return typeof value === 'string' && byteLength(value) <= RIVER_CLICK_FAILURE_CODE_MAX_BYTES
}

function identityCapped(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && byteLength(value) <= RIVER_CLICK_IDENTITY_MAX_BYTES
}

function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isValidUtcTimestamp(value: unknown): value is string {
  if (typeof value !== 'string') return false
  // Strict RFC3339 UTC shape: YYYY-MM-DDTHH:MM:SS(.fraction)?Z.
  const pattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z$/
  const match = pattern.exec(value)
  if (match === null) return false
  const year = Number.parseInt(match[1], 10)
  const month = Number.parseInt(match[2], 10)
  const day = Number.parseInt(match[3], 10)
  const hour = Number.parseInt(match[4], 10)
  const minute = Number.parseInt(match[5], 10)
  const second = Number.parseInt(match[6], 10)
  if (month < 1 || month > 12) return false
  if (hour > 23 || minute > 59 || second > 59) return false
  if (day < 1 || day > 31) return false
  // Calendar-valid UTC: Date.parse alone accepts Feb 30/Apr 31 (rolling into
  // March/May), so replay the components through UTC arithmetic.
  const timestamp = Date.UTC(year, month - 1, day, hour, minute, second)
  if (!Number.isFinite(timestamp)) return false
  const replayed = new Date(timestamp)
  return (
    replayed.getUTCFullYear() === year &&
    replayed.getUTCMonth() === month - 1 &&
    replayed.getUTCDate() === day &&
    replayed.getUTCHours() === hour &&
    replayed.getUTCMinutes() === minute &&
    replayed.getUTCSeconds() === second
  )
}

function isUrlUserinfoFree(value: unknown): value is string {
  if (typeof value !== 'string') return false
  try {
    const parsed = new URL(value)
    return parsed.username === '' && parsed.password === '' && parsed.pathname !== '/'
  } catch {
    return false
  }
}

function isRawUrlLike(value: string): boolean {
  // Reject anything starting with a scheme or containing clearly URL-shaped text.
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(value) || value.includes('://')
}

/** Failure messages must never carry URL/query/error-shaped text. */
const RAW_URL_TEXT_PATTERN = /(?:https?|ftp):\/\/|(?:[?&](?:variables|run_id|scenarios|issue_time|token|secret|key)=)|(?:Error\b|Error:|Exception)/i

function orderedTimestamps(doc: Record<string, unknown>): boolean {
  const started = Date.parse(doc.started_at as string)
  const ended = Date.parse(doc.ended_at as string)
  const generated = Date.parse(doc.generated_at as string)
  return started <= ended && ended === generated && doc.ended_at === doc.generated_at
}

function treeBoundsWithin(value: unknown, depth = 1): boolean {
  if (depth > RIVER_CLICK_JSON_MAX_DEPTH) return false
  if (Array.isArray(value)) {
    if (value.length > RIVER_CLICK_ARRAY_MAX_LENGTH) return false
    for (const entry of value) {
      if (!treeBoundsWithin(entry, depth + 1)) return false
    }
    return true
  }
  if (isRecord(value)) {
    const keys = Object.keys(value)
    if (keys.length > RIVER_CLICK_OBJECT_MAX_WIDTH) return false
    for (const key of keys) {
      if (!treeBoundsWithin(value[key], depth + 1)) return false
    }
    return true
  }
  return true
}

const SECRET_KEY_PATTERN = new RegExp(
  '(?:^|[_-])(?:password|passwd|pwd|token|secret|api[_-]?key|private[_-]?key|client[_-]?secret|credential|database[_-]?url|dsn)(?:$|[_-])',
  'i',
)
const SECRET_TEXT_PATTERN = new RegExp(
  '(?:postgres(?:ql)?://[^\\s"\']+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\\bbearer\\s+[a-z0-9._~+/=-]+|(?:password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret|authorization|credential|database[_-]?url|dsn)\\s*[:=]\\s*[^\\s,;]+)',
  'i',
)

function rejectSecretMaterial(value: unknown): boolean {
  if (Array.isArray(value)) {
    return value.every(rejectSecretMaterial)
  }
  if (isRecord(value)) {
    return Object.entries(value).every(([key, item]) => !SECRET_KEY_PATTERN.test(key) && rejectSecretMaterial(item))
  }
  if (typeof value === 'string') {
    return !SECRET_TEXT_PATTERN.test(value) && !SECRET_KEY_PATTERN.test(value)
  }
  return true
}

function validateIdentityValue(value: unknown): boolean {
  if (typeof value !== 'string' || value.length === 0 || byteLength(value) > RIVER_CLICK_IDENTITY_MAX_BYTES) return false
  if (isRawUrlLike(value)) return false
  if (/^https?:/i.test(value)) return false
  // Reject raw URL/query/userinfo-shaped material OUTSIDE the two normalized
  // origin slots: a scheme-relative URL, a query string (`?run_id=x&token=y`),
  // or a userinfo-bearing reference (`user@host`) must never appear as an
  // identity field.
  if (/^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(value)) return false
  if (/[?&](?:[a-z_][a-z0-9_]*=)/i.test(value)) return false
  if (/^[^/@\s]+@[^/@\s]+$/.test(value)) return false
  try {
    const url = new URL(value)
    return !(url.username || url.password)
  } catch {
    // not a parseable URL
  }
  return true
}

function validateFeature(document: unknown): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  const expected = ['basin_id', 'river_segment_id', 'basin_version_id', 'river_network_version_id']
  if (Object.keys(document).length !== expected.length) return false
  for (const key of expected) {
    if (!validateIdentityValue((document as Record<string, unknown>)[key])) return false
  }
  return true
}

function validateProduct(document: unknown, slot: 'GFS' | 'IFS'): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  const expected = [
    'source_id', 'basin_id', 'basin_version_id', 'river_network_version_id',
    'run_id', 'model_id', 'cycle_time', 'scenario',
  ]
  if (Object.keys(document).length !== expected.length) return false
  for (const key of expected) {
    if (!validateIdentityValue((document as Record<string, unknown>)[key])) return false
  }
  // GFS slot MUST be GFS/source_gfs; IFS slot MUST be IFS/source_ifs even when
  // only one product is non-null on a partial FAIL.
  if (slot === 'GFS') {
    if (document.source_id !== 'GFS' || document.scenario !== RIVER_CLICK_GFS_SCENARIO) return false
  } else {
    if (document.source_id !== 'IFS' || document.scenario !== RIVER_CLICK_IFS_SCENARIO) return false
  }
  return true
}

function validateSample(document: unknown): boolean {
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 4) return false
  if (!Number.isInteger(document.index) || (document.index as number) < 1 || (document.index as number) > RIVER_CLICK_ACCEPTED_SAMPLES) return false
  if (!finiteNumber(document.duration_ms) || (document.duration_ms as number) < 0) return false
  if (!Number.isInteger(document.gfs_status) || (document.gfs_status as number) < 200 || (document.gfs_status as number) > 299) return false
  if (!Number.isInteger(document.ifs_status) || (document.ifs_status as number) < 200 || (document.ifs_status as number) > 299) return false
  return true
}

function validateWarmup(document: unknown): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 5) return false
  if (document.index !== 0 || document.discarded !== true) return false
  if (!finiteNumber(document.duration_ms) || (document.duration_ms as number) < 0) return false
  if (!Number.isInteger(document.gfs_status) || (document.gfs_status as number) < 200 || (document.gfs_status as number) > 299) return false
  if (!Number.isInteger(document.ifs_status) || (document.ifs_status as number) < 200 || (document.ifs_status as number) > 299) return false
  return true
}

function validateOrigins(document: unknown): boolean {
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 2) return false
  if (document.frontend !== null && !identityCapped(document.frontend)) return false
  if (document.api !== null && !identityCapped(document.api)) return false
  // Origins must be normalized bare origins: http(s)://host[:port] only;
  // FTP and every other scheme are rejected.
  for (const value of [document.frontend, document.api]) {
    if (value === null || typeof value !== 'string') continue
    let parsed: URL
    try {
      parsed = new URL(value)
    } catch {
      return false
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
    if (parsed.username || parsed.password) return false
    if (parsed.pathname !== '/') return false
    if (parsed.search || parsed.hash) return false
    if (value !== parsed.origin) return false
  }
  return true
}

function validateFailure(document: unknown, status: string): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 6) return false
  if (!codeCapped(document.code)) return false
  if (!messageCapped(document.message)) return false
  const stages = ['config', 'runtime', 'preflight', 'map', 'warmup', 'sample', 'threshold']
  if (!stages.includes(document.stage as string)) return false
  const code = document.code as string
  if (status === 'BLOCKED') {
    // BLOCKED is bound to BLOCKED codes; a FAIL/unknown code is a refusal.
    if (!(RIVER_CLICK_BLOCKED_CODES as readonly string[]).includes(code)) return false
    if (document.sample_index !== null) return false
    if (document.gfs_status !== null || document.ifs_status !== null) return false
  } else if (status === 'FAIL') {
    if (!(RIVER_CLICK_FAIL_CODES as readonly string[]).includes(code)) return false
  } else {
    if (document.code !== null) {
      // PASS must have failure null; if a failure object exists, it must be a
      // closed code (defensive, though cross-field checks forbid this).
      if (!(RIVER_CLICK_FAIL_CODES as readonly string[]).includes(code)) return false
    }
  }
  // Untrusted exception/URL/query text never enters a receipt message.
  if (typeof document.message === 'string' && RAW_URL_TEXT_PATTERN.test(document.message)) return false
  if (document.sample_index !== null && (!Number.isInteger(document.sample_index) || (document.sample_index as number) < 0 || (document.sample_index as number) > RIVER_CLICK_ACCEPTED_SAMPLES)) return false
  if (document.gfs_status !== null && (!Number.isInteger(document.gfs_status) || (document.gfs_status as number) < 100 || (document.gfs_status as number) > 599)) return false
  if (document.ifs_status !== null && (!Number.isInteger(document.ifs_status) || (document.ifs_status as number) < 100 || (document.ifs_status as number) > 599)) return false
  return true
}

function validateCrossField(doc: Record<string, unknown>): boolean {
  // accepted_count === samples.length and warmup_count === (warmup ? 1 : 0)
  // for EVERY status.
  const acceptedCount = doc.accepted_count as number
  const samples = doc.samples as Array<Record<string, unknown>>
  if (!Array.isArray(samples)) return false
  if (acceptedCount !== samples.length) return false
  const expectedWarmupCount = doc.warmup === null ? 0 : RIVER_CLICK_WARMUP
  if (doc.warmup_count !== expectedWarmupCount) return false
  // Consecutive unique sample indices 1..N.
  for (let index = 0; index < samples.length; index += 1) {
    if (samples[index].index !== index + 1) return false
  }

  if (doc.status === 'PASS') {
    if (doc.warmup_count !== RIVER_CLICK_WARMUP) return false
    if (doc.accepted_count !== RIVER_CLICK_ACCEPTED_SAMPLES) return false
    if (doc.failure !== null) return false
    if (doc.p95_ms === null || !finiteNumber(doc.p95_ms) || (doc.p95_ms as number) >= RIVER_CLICK_THRESHOLD_MS) return false
    if (doc.origins === null) return false
    if (!isRecord(doc.origins) || doc.origins.frontend === null || doc.origins.api === null) return false
    if (doc.gfs === null || doc.ifs === null) return false
    if (doc.requested_feature === null || doc.rendered_feature === null) return false
    if (!Array.isArray(doc.samples) || doc.samples.length !== RIVER_CLICK_ACCEPTED_SAMPLES) return false
    // requested/rendered feature identity must be equal for a PASS.
    if (!featureEquality(doc.requested_feature, doc.rendered_feature)) return false
    // GFS source+scenario and IFS source+scenario, same basin/version/network
    // identities across both products.
    if (!productPairEquality(doc.gfs, doc.ifs, doc.requested_feature, doc.rendered_feature)) return false
    // Recompute P95 from actual durations.
    const durations = samples.map((sample) => sample.duration_ms as number)
    const recomputed = nearestRankP95(durations)
    if (recomputed === null || recomputed !== doc.p95_ms) return false
    return true
  }
  if (doc.status === 'BLOCKED') {
    if (doc.failure === null) return false
    if (!Array.isArray(samples) || samples.length !== 0) return false
    if (doc.warmup !== null || doc.warmup_count !== 0 || doc.accepted_count !== 0) return false
    if (doc.requested_feature !== null || doc.rendered_feature !== null || doc.gfs !== null || doc.ifs !== null) return false
    if (doc.origins !== null && (!isRecord(doc.origins) || doc.origins.frontend !== null || doc.origins.api !== null)) return false
    return doc.p95_ms === null
  }
  if (doc.status === 'FAIL') {
    if (doc.failure === null) return false
    const failureCode = (doc.failure as Record<string, unknown>).code as string
    if (doc.p95_ms !== null) {
      // Only a completed THRESHOLD_EXCEEDED may carry a P95, and it must be
      // >= threshold with PASS-equivalent identity.
      if (failureCode !== 'THRESHOLD_EXCEEDED') return false
      if (!finiteNumber(doc.p95_ms) || (doc.p95_ms as number) < RIVER_CLICK_THRESHOLD_MS) return false
      if (samples.length !== RIVER_CLICK_ACCEPTED_SAMPLES) return false
      if (doc.warmup === null || doc.origins === null) return false
      if (!isRecord(doc.origins) || doc.origins.frontend === null || doc.origins.api === null) return false
      if (doc.requested_feature === null || doc.rendered_feature === null || doc.gfs === null || doc.ifs === null) return false
      if (!featureEquality(doc.requested_feature, doc.rendered_feature)) return false
      if (!productPairEquality(doc.gfs, doc.ifs, doc.requested_feature, doc.rendered_feature)) return false
      const durations = samples.map((sample) => sample.duration_ms as number)
      const recomputed = nearestRankP95(durations)
      if (recomputed === null || recomputed !== doc.p95_ms) return false
      // Completed threshold identity is PASS-equivalent: failure must sit at
      // stage threshold with no per-sample index and the same feature identity.
      if ((doc.failure as Record<string, unknown>).stage !== 'threshold') return false
      if ((doc.failure as Record<string, unknown>).sample_index !== null) return false
      return true
    }
    // A FAIL without P95 must preserve partial identity coherence: every
    // non-null feature/product pair must agree among themselves, and the
    // requested/rendered features must agree with any non-null product. The ONE
    // deliberate exception is an IDENTITY_DRIFT FAIL: the actual mismatching
    // rendered_feature is retained as evidence of the drift, so the requested
    // and rendered identities MUST differ there (never silently equalized).
    if (failureCode === 'IDENTITY_DRIFT') {
      if (doc.requested_feature === null || doc.rendered_feature === null) return false
      if (featureEquality(doc.requested_feature, doc.rendered_feature)) return false
    } else if (doc.requested_feature !== null && doc.rendered_feature !== null) {
      if (!featureEquality(doc.requested_feature, doc.rendered_feature)) return false
    }
    const featurePair = doc.requested_feature ?? doc.rendered_feature
    const productPair = doc.gfs ?? doc.ifs
    if (featurePair !== null && productPair !== null) {
      if (!featureProductAgreement(featurePair, productPair)) return false
    }
    if (doc.gfs !== null && doc.ifs !== null) {
      if (!productPairEquality(doc.gfs, doc.ifs, doc.requested_feature ?? doc.rendered_feature, doc.requested_feature ?? doc.rendered_feature)) return false
    }
    if (doc.gfs !== null && doc.ifs !== null) {
      // Both products must share basin/network identity (partial coherence).
      if (!productsShareVersionIdentity(doc.gfs, doc.ifs)) return false
    }
    return true
  }
  return false
}

/** A feature and a product sharing the same basin/version/network identity. */
function featureProductAgreement(feature: unknown, product: unknown): boolean {
  if (!isRecord(feature) || !isRecord(product)) return false
  for (const key of ['basin_id', 'basin_version_id', 'river_network_version_id'] as const) {
    if (feature[key] !== product[key]) return false
  }
  return true
}

function productsShareVersionIdentity(gfs: unknown, ifs: unknown): boolean {
  if (!isRecord(gfs) || !isRecord(ifs)) return false
  for (const key of ['basin_id', 'basin_version_id', 'river_network_version_id'] as const) {
    if (gfs[key] !== ifs[key]) return false
  }
  return true
}

function featureEquality(a: unknown, b: unknown): boolean {
  if (!isRecord(a) || !isRecord(b)) return false
  for (const key of ['basin_id', 'river_segment_id', 'basin_version_id', 'river_network_version_id']) {
    if (a[key] !== b[key]) return false
  }
  return true
}

function productPairEquality(
  gfs: unknown,
  ifs: unknown,
  requested: unknown,
  rendered: unknown,
): boolean {
  if (!isRecord(gfs) || !isRecord(ifs)) return false
  if (gfs.source_id !== 'GFS' || gfs.scenario !== RIVER_CLICK_GFS_SCENARIO) return false
  if (ifs.source_id !== 'IFS' || ifs.scenario !== RIVER_CLICK_IFS_SCENARIO) return false
  // Same basin/version/network identities across both products and both features.
  const baseKeys = ['basin_id', 'basin_version_id', 'river_network_version_id'] as const
  for (const key of baseKeys) {
    if (gfs[key] !== ifs[key]) return false
  }
  for (const feature of [requested, rendered]) {
    if (!isRecord(feature)) return false
    for (const key of baseKeys) {
      if (feature[key] !== gfs[key]) return false
    }
  }
  // Canonical cycle times only.
  for (const product of [gfs, ifs]) {
    if (normalizeRiverClickCycleTime(product.cycle_time as string) !== product.cycle_time) return false
  }
  return true
}

/**
 * Closed semantic validator implementing the same required/type/status/count/
 * identity/finite-number bounds the JSON Schema encodes, without a runtime
 * schema dependency. Runs before any write.
 */
export function validateRiverClickEvidenceDocument(value: unknown): { ok: true } | { ok: false; reason: string } {
  if (!isRecord(value)) return { ok: false, reason: 'evidence must be a JSON object' }

  // Safe JSON serialization: cyclic references, BigInt, and other non-JSON
  // values make JSON.stringify throw or silently drop members; treat them as
  // validation failure rather than writing a poisoned receipt.
  let serialized: string
  try {
    serialized = JSON.stringify(value, (key, item) => {
      if (typeof item === 'bigint') throw new TypeError('bigint')
      if (item === undefined && key !== '') throw new TypeError('undefined')
      return item
    })
  } catch {
    return { ok: false, reason: 'evidence is not JSON-serializable' }
  }
  const serializedBytes = byteLength(serialized)
  if (serializedBytes > RIVER_CLICK_EVIDENCE_MAX_BYTES) {
    return { ok: false, reason: `evidence exceeds the ${RIVER_CLICK_EVIDENCE_MAX_BYTES} byte ceiling` }
  }
  if (!treeBoundsWithin(value)) {
    return { ok: false, reason: 'evidence exceeds JSON depth/width/array bounds' }
  }
  if (!rejectSecretMaterial(value)) {
    return { ok: false, reason: 'evidence contains forbidden secret-shaped material' }
  }

  const topKeys = [
    'artifact', 'schema_version', 'status', 'generated_at', 'started_at', 'ended_at',
    'threshold_ms', 'percentile_method', 'warmup_count', 'accepted_count', 'origins',
    'requested_feature', 'rendered_feature', 'gfs', 'ifs', 'warmup', 'samples', 'p95_ms', 'failure',
  ]
  if (Object.keys(value).length !== topKeys.length || !topKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key))) {
    return { ok: false, reason: 'evidence top-level fields differ from the schema-1.0 contract' }
  }

  if (value.artifact !== RIVER_CLICK_ARTIFACT) return { ok: false, reason: 'artifact identity differs' }
  if (value.schema_version !== RIVER_CLICK_SCHEMA_VERSION) return { ok: false, reason: 'schema_version differs' }
  if (value.status !== 'PASS' && value.status !== 'FAIL' && value.status !== 'BLOCKED') return { ok: false, reason: 'status is not closed' }
  if (value.threshold_ms !== RIVER_CLICK_THRESHOLD_MS) return { ok: false, reason: 'threshold_ms differs' }
  if (value.percentile_method !== RIVER_CLICK_PERCENTILE_METHOD) return { ok: false, reason: 'percentile_method differs' }
  if (!Number.isInteger(value.warmup_count) || (value.warmup_count as number) < 0 || (value.warmup_count as number) > RIVER_CLICK_WARMUP) return { ok: false, reason: 'warmup_count is out of bounds' }
  if (!Number.isInteger(value.accepted_count) || (value.accepted_count as number) < 0 || (value.accepted_count as number) > RIVER_CLICK_ACCEPTED_SAMPLES) return { ok: false, reason: 'accepted_count is out of bounds' }
  if (!isValidUtcTimestamp(value.generated_at) || !isValidUtcTimestamp(value.started_at) || !isValidUtcTimestamp(value.ended_at)) {
    return { ok: false, reason: 'timestamps must be UTC RFC3339 instants' }
  }
  if (!orderedTimestamps(value)) return { ok: false, reason: 'timestamps must satisfy started_at <= ended_at == generated_at' }
  if (!validateOrigins(value.origins)) return { ok: false, reason: 'origins are not nullable normalized origins' }
  if (!validateFeature(value.requested_feature)) return { ok: false, reason: 'requested_feature fields differ' }
  if (!validateFeature(value.rendered_feature)) return { ok: false, reason: 'rendered_feature fields differ' }
  if (!validateProduct(value.gfs, 'GFS')) return { ok: false, reason: 'gfs fields differ' }
  if (!validateProduct(value.ifs, 'IFS')) return { ok: false, reason: 'ifs fields differ' }
  if (!validateWarmup(value.warmup)) return { ok: false, reason: 'warmup fields differ' }
  if (!Array.isArray(value.samples) || value.samples.length > RIVER_CLICK_ARRAY_MAX_LENGTH) return { ok: false, reason: 'samples exceed array bounds' }
  for (const sample of value.samples) {
    if (!validateSample(sample)) return { ok: false, reason: 'sample fields differ' }
  }
  if (value.p95_ms !== null && !finiteNumber(value.p95_ms)) return { ok: false, reason: 'p95_ms must be null or finite' }
  if (!validateFailure(value.failure, value.status as string)) return { ok: false, reason: 'failure fields differ' }
  if (!validateCrossField(value)) return { ok: false, reason: 'cross-field status/count/p95 invariants differ' }
  return { ok: true }
}
