import {
  C4_ARTIFACT,
  C4_ARRAY_MAX_LENGTH,
  C4_BLOCKED_CODES,
  C4_EVIDENCE_MAX_BYTES,
  C4_FAILURE_CODE_MAX_BYTES,
  C4_FAILURE_MESSAGE_MAX_BYTES,
  C4_FAIL_CODES,
  C4_GFS_SCENARIO,
  C4_HOME_READ_API_PATHS,
  C4_IDENTITY_MAX_BYTES,
  C4_IFS_SCENARIO,
  C4_JSON_MAX_DEPTH,
  C4_OBJECT_MAX_WIDTH,
  C4_SCHEMA_VERSION,
  C4_STAGES,
  type C4FailureCode,
  type C4FailureStage,
} from './constants'
import { normalizeRiverClickCycleTime } from '../riverClickEvidence/preflight'

export {
  C4_ARTIFACT,
  C4_SCHEMA_VERSION,
} from './constants'

export interface C4RequestedPins {
  basinId: string
  riverSegmentId: string
}

export interface C4ProductIdentity {
  sourceId: 'GFS' | 'IFS'
  basinId: string
  basinVersionId: string
  riverNetworkVersionId: string
  runId: string
  modelId: string
  cycleTime: string
  scenario: string
}

export interface C4HomeCheck {
  path: '/'
  mapSurfaceVisible: true
  runtimeConfigStatus: number
  runtimeServiceRole: 'display_readonly'
  currentReadObserved: true
  currentReadPath: string
}

export interface C4OpsCheck {
  sourceId: 'GFS' | 'IFS'
  path: '/ops'
  headingObserved: true
  permissionDenied: false
  runtimeUnavailable: false
  statusStatus: number
  stagesStatus: number
  jobsStatus: number
  jobId: string
  logsStatus: number
  roleSelectorCount: 0
  retryCancelControlCount: 0
  slurmRequestCount: number
  nonGetControlCount: number
  queueReadonlyVisible: true
  operatorRecoveryVisible: true
}

export interface C4SourceSwitch {
  bothSourcesCompleted: true
  identitiesDistinctOrSourceBound: true
}

export interface C4NoControl {
  slurmRequestCount: number
  nonGetControlCount: number
}

export interface C4Failure {
  code: C4FailureCode
  stage: C4FailureStage
  message: string
}

export interface C4Evidence {
  artifact: string
  schema_version: string
  status: 'PASS' | 'FAIL' | 'BLOCKED'
  generated_at: string
  started_at: string
  ended_at: string
  origins: { frontend: string | null; api: string | null }
  requested_pins: {
    basin_id: string
    river_segment_id: string
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
  home: {
    path: '/'
    map_surface_visible: true
    runtime_config_status: number
    runtime_service_role: 'display_readonly'
    current_read_observed: true
    current_read_path: string
  } | null
  ops: {
    gfs: {
      source_id: 'GFS'
      path: '/ops'
      heading_observed: true
      permission_denied: false
      runtime_unavailable: false
      status_status: number
      stages_status: number
      jobs_status: number
      job_id: string
      logs_status: number
      role_selector_count: 0
      retry_cancel_control_count: 0
      slurm_request_count: number
      non_get_control_count: number
      queue_readonly_visible: true
      operator_recovery_visible: true
    } | null
    ifs: {
      source_id: 'IFS'
      path: '/ops'
      heading_observed: true
      permission_denied: false
      runtime_unavailable: false
      status_status: number
      stages_status: number
      jobs_status: number
      job_id: string
      logs_status: number
      role_selector_count: 0
      retry_cancel_control_count: 0
      slurm_request_count: number
      non_get_control_count: number
      queue_readonly_visible: true
      operator_recovery_visible: true
    } | null
  }
  source_switch: { both_sources_completed: true; identities_distinct_or_source_bound: true } | null
  no_control: { slurm_request_count: number; non_get_control_count: number } | null
  failure: {
    code: string
    stage: string
    message: string
  } | null
}

export interface C4PassInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string
  apiOrigin: string
  requestedPins: C4RequestedPins
  gfs: C4ProductIdentity
  ifs: C4ProductIdentity
  home: C4HomeCheck
  opsGfs: C4OpsCheck
  opsIfs: C4OpsCheck
  noControl: C4NoControl
}

export interface C4TerminalInput {
  startedAt: string
  endedAt: string
  frontendOrigin: string | null
  apiOrigin: string | null
  requestedPins?: C4RequestedPins | null
  gfs?: C4ProductIdentity | null
  ifs?: C4ProductIdentity | null
  home?: C4HomeCheck | null
  opsGfs?: C4OpsCheck | null
  opsIfs?: C4OpsCheck | null
  sourceSwitch?: C4SourceSwitch | null
  noControl?: C4NoControl | null
  failure: C4Failure
}

export type C4BuildResult =
  | { ok: true; receipt: C4Evidence }
  | { ok: false; reason: string }

const TOP_LEVEL_KEYS = [
  'artifact',
  'schema_version',
  'status',
  'generated_at',
  'started_at',
  'ended_at',
  'origins',
  'requested_pins',
  'gfs',
  'ifs',
  'home',
  'ops',
  'source_switch',
  'no_control',
  'failure',
] as const

function productWire(product: C4ProductIdentity | null): C4Evidence['gfs'] {
  if (product === null) return null
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

function pinsWire(pins: C4RequestedPins | null): C4Evidence['requested_pins'] {
  if (pins === null) return null
  return { basin_id: pins.basinId, river_segment_id: pins.riverSegmentId }
}

function homeWire(home: C4HomeCheck | null): C4Evidence['home'] {
  if (home === null) return null
  return {
    path: '/',
    map_surface_visible: true,
    runtime_config_status: home.runtimeConfigStatus,
    runtime_service_role: 'display_readonly',
    current_read_observed: true,
    current_read_path: home.currentReadPath,
  }
}

function opsCheckWire(check: C4OpsCheck | null): C4Evidence['ops']['gfs'] | C4Evidence['ops']['ifs'] {
  if (check === null) return null
  return {
    source_id: check.sourceId,
    path: '/ops',
    heading_observed: check.headingObserved,
    permission_denied: check.permissionDenied,
    runtime_unavailable: check.runtimeUnavailable,
    status_status: check.statusStatus,
    stages_status: check.stagesStatus,
    jobs_status: check.jobsStatus,
    job_id: check.jobId,
    logs_status: check.logsStatus,
    role_selector_count: check.roleSelectorCount,
    retry_cancel_control_count: check.retryCancelControlCount,
    slurm_request_count: check.slurmRequestCount,
    non_get_control_count: check.nonGetControlCount,
    queue_readonly_visible: check.queueReadonlyVisible,
    operator_recovery_visible: check.operatorRecoveryVisible,
  } as C4Evidence['ops']['gfs']
}

function canonicalProduct(product: C4ProductIdentity): C4ProductIdentity | null {
  try {
    const serialized = JSON.stringify(product)
    if (typeof serialized !== 'string') return null
    const parsed = JSON.parse(serialized) as C4ProductIdentity
    if (typeof parsed.cycleTime !== 'string' || typeof parsed.basinId !== 'string' || typeof parsed.sourceId !== 'string') {
      return null
    }
  } catch {
    return null
  }
  if (normalizeRiverClickCycleTime(product.cycleTime) === null) return null
  return product
}

function withGeneratedAt(receipt: C4Evidence): C4Evidence {
  return { ...receipt, generated_at: receipt.ended_at }
}

function baseDocument(input: {
  startedAt: string
  endedAt: string
  frontendOrigin: string | null
  apiOrigin: string | null
  requestedPins: C4RequestedPins | null
  gfs: C4ProductIdentity | null
  ifs: C4ProductIdentity | null
  home: C4HomeCheck | null
  opsGfs: C4OpsCheck | null
  opsIfs: C4OpsCheck | null
  sourceSwitch: C4SourceSwitch | null
  noControl: C4NoControl | null
  failure: C4Evidence['failure']
  status: 'PASS' | 'FAIL' | 'BLOCKED'
}): C4Evidence {
  return {
    artifact: C4_ARTIFACT,
    schema_version: C4_SCHEMA_VERSION,
    status: input.status,
    generated_at: input.endedAt,
    started_at: input.startedAt,
    ended_at: input.endedAt,
    origins: { frontend: input.frontendOrigin, api: input.apiOrigin },
    requested_pins: pinsWire(input.requestedPins),
    gfs: productWire(input.gfs),
    ifs: productWire(input.ifs),
    home: homeWire(input.home),
    ops: {
      gfs: opsCheckWire(input.opsGfs) as C4Evidence['ops']['gfs'],
      ifs: opsCheckWire(input.opsIfs) as C4Evidence['ops']['ifs'],
    },
    source_switch: input.sourceSwitch
      ? {
          both_sources_completed: input.sourceSwitch.bothSourcesCompleted,
          identities_distinct_or_source_bound: input.sourceSwitch.identitiesDistinctOrSourceBound,
        }
      : null,
    no_control: input.noControl
      ? {
          slurm_request_count: input.noControl.slurmRequestCount,
          non_get_control_count: input.noControl.nonGetControlCount,
        }
      : null,
    failure: input.failure,
  }
}

export function buildC4PassEvidence(input: C4PassInput): C4BuildResult {
  const gfs = canonicalProduct(input.gfs)
  const ifs = canonicalProduct(input.ifs)
  if (gfs === null || ifs === null) return { ok: false, reason: 'product cycle_time is not a strict canonical RFC3339 instant' }
  if (input.opsGfs.sourceId !== 'GFS' || input.opsIfs.sourceId !== 'IFS') {
    return { ok: false, reason: 'PASS ops checks must be GFS then IFS' }
  }
  if (
    input.noControl.slurmRequestCount !== 0 ||
    input.noControl.nonGetControlCount !== 0 ||
    input.opsGfs.slurmRequestCount !== 0 ||
    input.opsGfs.nonGetControlCount !== 0 ||
    input.opsIfs.slurmRequestCount !== 0 ||
    input.opsIfs.nonGetControlCount !== 0
  ) {
    return { ok: false, reason: 'PASS cannot contain observed control requests' }
  }
  const receipt = baseDocument({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: input.frontendOrigin,
    apiOrigin: input.apiOrigin,
    requestedPins: input.requestedPins,
    gfs,
    ifs,
    home: input.home,
    opsGfs: input.opsGfs,
    opsIfs: input.opsIfs,
    sourceSwitch: { bothSourcesCompleted: true, identitiesDistinctOrSourceBound: true },
    noControl: input.noControl,
    failure: null,
    status: 'PASS',
  })
  const withTime = withGeneratedAt(receipt)
  const validated = validateC4EvidenceDocument(withTime)
  if (!validated.ok) return { ok: false, reason: validated.reason }
  return { ok: true, receipt: withTime }
}

export function buildC4TerminalEvidence(input: C4TerminalInput): C4BuildResult {
  const blocked = (C4_BLOCKED_CODES as readonly string[]).includes(input.failure.code)
  const isFailCode = (C4_FAIL_CODES as readonly string[]).includes(input.failure.code)
  if (!blocked && !isFailCode) {
    return { ok: false, reason: `failure code ${input.failure.code} is not in the closed BLOCKED/FAIL sets` }
  }
  const gfs = input.gfs === null || input.gfs === undefined ? null : canonicalProduct(input.gfs)
  const ifs = input.ifs === null || input.ifs === undefined ? null : canonicalProduct(input.ifs)
  if ((input.gfs !== null && input.gfs !== undefined && gfs === null) || (input.ifs !== null && input.ifs !== undefined && ifs === null)) {
    return { ok: false, reason: 'product cycle_time is not a strict canonical RFC3339 instant' }
  }
  const receipt = baseDocument({
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    frontendOrigin: blocked ? null : input.frontendOrigin,
    apiOrigin: blocked ? null : input.apiOrigin,
    requestedPins: blocked ? null : input.requestedPins ?? null,
    gfs: blocked ? null : gfs,
    ifs: blocked ? null : ifs,
    home: blocked ? null : input.home ?? null,
    opsGfs: blocked ? null : input.opsGfs ?? null,
    opsIfs: blocked ? null : input.opsIfs ?? null,
    sourceSwitch: blocked ? null : input.sourceSwitch ?? null,
    noControl: blocked ? null : input.noControl ?? null,
    failure: { code: input.failure.code, stage: input.failure.stage, message: input.failure.message },
    status: blocked ? 'BLOCKED' : 'FAIL',
  })
  const withTime = withGeneratedAt(receipt)
  const validated = validateC4EvidenceDocument(withTime)
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
  return typeof value === 'string' && value.length > 0 && byteLength(value) <= C4_FAILURE_MESSAGE_MAX_BYTES
}

function codeCapped(value: unknown): value is string {
  return typeof value === 'string' && byteLength(value) <= C4_FAILURE_CODE_MAX_BYTES
}

function identityCapped(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && byteLength(value) <= C4_IDENTITY_MAX_BYTES
}

function isValidUtcTimestamp(value: unknown): value is string {
  if (typeof value !== 'string') return false
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

function isRawUrlLike(value: string): boolean {
  return /^(?:https?|ftp|file|ws|wss):/i.test(value) || value.includes('://')
}

const RAW_URL_TEXT_PATTERN = /(?:https?|ftp|file|ws|wss):|(?:[?&](?:variables|run_id|scenarios|issue_time|token|secret|key)=)|(?:\b(?:[A-Za-z]*Error|[A-Za-z]*Exception):\s*(?:at\s+|\/|[A-Za-z]:\\|\w+\.\w+[:(]))|(?:\bat\s+[^\s]+\s+\([^\n]*:\d+:\d+\))|(?:\b[^\s/@]+@[^\s/@]+\b)/i
const SECRET_KEY_PATTERN = new RegExp(
  '(?:^|[_-])(?:password|passwd|pwd|token|secret|api[_-]?key|private[_-]?key|client[_-]?secret|credential|database[_-]?url|dsn)(?:$|[_-])',
  'i',
)
const SECRET_TEXT_PATTERN = new RegExp(
  '(?:postgres(?:ql)?://[^\\s"\']+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\\bbearer\\s+[a-z0-9._~+/=-]+|(?:password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret|authorization|credential|database[_-]?url|dsn)\\s*[:=]\\s*[^\\s,;]+)',
  'i',
)

function rejectSecretMaterial(value: unknown): boolean {
  if (Array.isArray(value)) return value.every(rejectSecretMaterial)
  if (isRecord(value)) {
    return Object.entries(value).every(([key, item]) => !SECRET_KEY_PATTERN.test(key) && rejectSecretMaterial(item))
  }
  if (typeof value === 'string') return !SECRET_TEXT_PATTERN.test(value) && !SECRET_KEY_PATTERN.test(value)
  return true
}

function treeBoundsWithin(value: unknown, depth = 1): boolean {
  if (depth > C4_JSON_MAX_DEPTH) return false
  if (Array.isArray(value)) {
    if (value.length > C4_ARRAY_MAX_LENGTH) return false
    for (const entry of value) {
      if (!treeBoundsWithin(entry, depth + 1)) return false
    }
    return true
  }
  if (isRecord(value)) {
    const keys = Object.keys(value)
    if (keys.length > C4_OBJECT_MAX_WIDTH) return false
    for (const key of keys) {
      if (!treeBoundsWithin(value[key], depth + 1)) return false
    }
    return true
  }
  return true
}

function validateIdentityValue(value: unknown): boolean {
  if (typeof value !== 'string' || value.length === 0 || byteLength(value) > C4_IDENTITY_MAX_BYTES) return false
  if (isRawUrlLike(value)) return false
  if (/^https?:/i.test(value)) return false
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

function validateProduct(document: unknown, slot: 'GFS' | 'IFS'): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  const expected = [
    'source_id', 'basin_id', 'basin_version_id', 'river_network_version_id',
    'run_id', 'model_id', 'cycle_time', 'scenario',
  ]
  if (Object.keys(document).length !== expected.length) return false
  for (const key of expected) {
    if (!validateIdentityValue(document[key])) return false
  }
  if (slot === 'GFS') {
    if (document.source_id !== 'GFS' || document.scenario !== C4_GFS_SCENARIO) return false
  } else if (document.source_id !== 'IFS' || document.scenario !== C4_IFS_SCENARIO) {
    return false
  }
  if (normalizeRiverClickCycleTime(document.cycle_time as string) === null) return false
  return true
}

function validatePins(document: unknown): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 2) return false
  return validateIdentityValue(document.basin_id) && validateIdentityValue(document.river_segment_id)
}

function validateOrigins(document: unknown): boolean {
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 2) return false
  if (document.frontend !== null && !identityCapped(document.frontend)) return false
  if (document.api !== null && !identityCapped(document.api)) return false
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

function twoXx(value: unknown): boolean {
  return Number.isInteger(value) && (value as number) >= 200 && (value as number) <= 299
}

function validateHome(document: unknown): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  const expected = [
    'path', 'map_surface_visible', 'runtime_config_status', 'runtime_service_role',
    'current_read_observed', 'current_read_path',
  ]
  if (Object.keys(document).length !== expected.length) return false
  if (document.path !== '/') return false
  if (document.map_surface_visible !== true) return false
  if (!twoXx(document.runtime_config_status)) return false
  if (document.runtime_service_role !== 'display_readonly') return false
  if (document.current_read_observed !== true) return false
  if (!(C4_HOME_READ_API_PATHS as readonly string[]).includes(document.current_read_path as string)) return false
  return true
}

function validateOpsCheck(document: unknown, slot: 'GFS' | 'IFS'): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  const expected = [
    'source_id', 'path', 'heading_observed', 'permission_denied', 'runtime_unavailable',
    'status_status', 'stages_status', 'jobs_status', 'job_id', 'logs_status',
    'role_selector_count', 'retry_cancel_control_count', 'slurm_request_count',
    'non_get_control_count', 'queue_readonly_visible', 'operator_recovery_visible',
  ]
  if (Object.keys(document).length !== expected.length) return false
  if (document.source_id !== slot) return false
  if (document.path !== '/ops') return false
  if (document.heading_observed !== true) return false
  if (document.permission_denied !== false) return false
  if (document.runtime_unavailable !== false) return false
  if (!twoXx(document.status_status) || !twoXx(document.stages_status) || !twoXx(document.jobs_status) || !twoXx(document.logs_status)) return false
  if (!validateIdentityValue(document.job_id)) return false
  if (document.role_selector_count !== 0) return false
  if (document.retry_cancel_control_count !== 0) return false
  if (!Number.isInteger(document.slurm_request_count) || (document.slurm_request_count as number) < 0) return false
  if (!Number.isInteger(document.non_get_control_count) || (document.non_get_control_count as number) < 0) return false
  if (document.queue_readonly_visible !== true) return false
  if (document.operator_recovery_visible !== true) return false
  return true
}

function validateOps(document: unknown): boolean {
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 2) return false
  return validateOpsCheck(document.gfs, 'GFS') && validateOpsCheck(document.ifs, 'IFS')
}

function validateFailure(document: unknown, status: string): boolean {
  if (document === null || document === undefined) return true
  if (!isRecord(document)) return false
  if (Object.keys(document).length !== 3) return false
  if (!codeCapped(document.code) || !messageCapped(document.message)) return false
  if (!(C4_STAGES as readonly string[]).includes(document.stage as string)) return false
  const code = document.code as string
  if (status === 'BLOCKED') {
    if (!(C4_BLOCKED_CODES as readonly string[]).includes(code)) return false
  } else if (status === 'FAIL') {
    if (!(C4_FAIL_CODES as readonly string[]).includes(code)) return false
  }
  if (typeof document.message === 'string' && RAW_URL_TEXT_PATTERN.test(document.message)) return false
  return true
}

function productsShareVersionIdentity(gfs: unknown, ifs: unknown, pins: unknown): boolean {
  if (!isRecord(gfs) || !isRecord(ifs) || !isRecord(pins)) return false
  for (const key of ['basin_id', 'basin_version_id', 'river_network_version_id'] as const) {
    if (gfs[key] !== ifs[key]) return false
  }
  if (gfs.basin_id !== pins.basin_id) return false
  if (ifs.basin_id !== pins.basin_id) return false
  return true
}

function identitiesDistinctOrSourceBound(gfs: unknown, ifs: unknown): boolean {
  if (!isRecord(gfs) || !isRecord(ifs)) return false
  if (gfs.source_id !== 'GFS' || ifs.source_id !== 'IFS') return false
  return gfs.run_id !== ifs.run_id || gfs.model_id !== ifs.model_id || gfs.cycle_time !== ifs.cycle_time
}

function orderedTimestamps(doc: Record<string, unknown>): boolean {
  const started = Date.parse(doc.started_at as string)
  const ended = Date.parse(doc.ended_at as string)
  const generated = Date.parse(doc.generated_at as string)
  return started <= ended && ended === generated && doc.ended_at === doc.generated_at
}

function validateCrossField(doc: Record<string, unknown>): boolean {
  if (doc.status === 'PASS') {
    if (doc.failure !== null) return false
    if (!isRecord(doc.origins) || doc.origins.frontend === null || doc.origins.api === null) return false
    if (doc.requested_pins === null || doc.gfs === null || doc.ifs === null) return false
    if (doc.home === null) return false
    if (!isRecord(doc.ops) || doc.ops.gfs === null || doc.ops.ifs === null) return false
    if (!isRecord(doc.ops.gfs) || !isRecord(doc.ops.ifs) || doc.ops.gfs.job_id === doc.ops.ifs.job_id) return false
    if (doc.source_switch === null || doc.no_control === null) return false
    if (!productsShareVersionIdentity(doc.gfs, doc.ifs, doc.requested_pins)) return false
    if (!identitiesDistinctOrSourceBound(doc.gfs, doc.ifs)) return false
    if (!isRecord(doc.source_switch) || doc.source_switch.both_sources_completed !== true) return false
    if (doc.source_switch.identities_distinct_or_source_bound !== true) return false
    if (!isRecord(doc.no_control) || doc.no_control.slurm_request_count !== 0 || doc.no_control.non_get_control_count !== 0) return false
    for (const check of [doc.ops.gfs, doc.ops.ifs]) {
      if (!isRecord(check) || check.slurm_request_count !== 0 || check.non_get_control_count !== 0) return false
    }
    return true
  }
  if (doc.status === 'BLOCKED') {
    if (doc.failure === null) return false
    if (doc.requested_pins !== null || doc.gfs !== null || doc.ifs !== null || doc.home !== null) return false
    if (!isRecord(doc.ops) || doc.ops.gfs !== null || doc.ops.ifs !== null) return false
    if (doc.source_switch !== null || doc.no_control !== null) return false
    if (!isRecord(doc.origins) || doc.origins.frontend !== null || doc.origins.api !== null) return false
    return true
  }
  if (doc.status === 'FAIL') {
    if (doc.failure === null) return false
    return true
  }
  return false
}

export function validateC4EvidenceDocument(value: unknown): { ok: true } | { ok: false; reason: string } {
  if (!isRecord(value)) return { ok: false, reason: 'receipt is not an object' }
  const keys = Object.keys(value)
  if (keys.length !== TOP_LEVEL_KEYS.length) return { ok: false, reason: 'receipt top-level key set is not exact' }
  for (const key of TOP_LEVEL_KEYS) {
    if (!keys.includes(key)) return { ok: false, reason: `receipt missing top-level key ${key}` }
  }
  if (value.artifact !== C4_ARTIFACT) return { ok: false, reason: 'artifact identity differs' }
  if (value.schema_version !== C4_SCHEMA_VERSION) return { ok: false, reason: 'schema version differs from 1.0' }
  if (value.status !== 'PASS' && value.status !== 'FAIL' && value.status !== 'BLOCKED') {
    return { ok: false, reason: 'status is not PASS/FAIL/BLOCKED' }
  }
  if (!isValidUtcTimestamp(value.generated_at) || !isValidUtcTimestamp(value.started_at) || !isValidUtcTimestamp(value.ended_at)) {
    return { ok: false, reason: 'timestamps are not strict UTC RFC3339 calendar-valid instants' }
  }
  if (!orderedTimestamps(value)) return { ok: false, reason: 'timestamps violate started <= ended == generated' }
  if (!validateOrigins(value.origins)) return { ok: false, reason: 'origins are not normalized bare HTTP(S) origins' }
  if (!validatePins(value.requested_pins)) return { ok: false, reason: 'requested_pins identity is invalid' }
  if (!validateProduct(value.gfs, 'GFS') || !validateProduct(value.ifs, 'IFS')) {
    return { ok: false, reason: 'product identity is invalid' }
  }
  if (!validateHome(value.home)) return { ok: false, reason: 'home check is invalid' }
  if (!validateOps(value.ops)) return { ok: false, reason: 'ops check is invalid' }
  if (value.source_switch !== null) {
    if (!isRecord(value.source_switch) || Object.keys(value.source_switch).length !== 2) {
      return { ok: false, reason: 'source_switch shape is invalid' }
    }
    if (value.source_switch.both_sources_completed !== true || value.source_switch.identities_distinct_or_source_bound !== true) {
      return { ok: false, reason: 'source_switch facts are invalid' }
    }
  }
  if (value.no_control !== null) {
    if (!isRecord(value.no_control) || Object.keys(value.no_control).length !== 2) {
      return { ok: false, reason: 'no_control shape is invalid' }
    }
    if (
      !Number.isInteger(value.no_control.slurm_request_count) ||
      !Number.isInteger(value.no_control.non_get_control_count) ||
      (value.no_control.slurm_request_count as number) < 0 ||
      (value.no_control.non_get_control_count as number) < 0
    ) {
      return { ok: false, reason: 'no_control facts are invalid' }
    }
  }
  if (!validateFailure(value.failure, value.status as string)) return { ok: false, reason: 'failure object is invalid' }
  if (!treeBoundsWithin(value)) return { ok: false, reason: 'receipt exceeds JSON complexity bounds' }
  if (!rejectSecretMaterial(value)) return { ok: false, reason: 'receipt contains forbidden secret material' }
  let serialized: string
  try {
    serialized = JSON.stringify(value)
  } catch {
    return { ok: false, reason: 'receipt is not JSON-serializable' }
  }
  if (byteLength(serialized) > C4_EVIDENCE_MAX_BYTES) return { ok: false, reason: 'receipt exceeds the byte ceiling' }
  if (!validateCrossField(value)) return { ok: false, reason: 'receipt cross-field contract is invalid' }
  return { ok: true }
}
