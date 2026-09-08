/** Closed constants for the dedicated #1895 C4 live display evidence lane. */

export const C4_ARTIFACT = 'nhms-frontend-c4-live-evidence'
export const C4_SCHEMA_VERSION = '1.0'

export const C4_PER_STEP_DEADLINE_MS = 15_000
export const C4_QUIET_PERIOD_MS = 500
export const C4_WHOLE_RUN_DEADLINE_MS = 180_000
export const C4_PLAYWRIGHT_TIMEOUT_MS = 210_000

export const C4_EVIDENCE_MAX_BYTES = 262_144
export const C4_JSON_MAX_DEPTH = 12
export const C4_OBJECT_MAX_WIDTH = 64
export const C4_ARRAY_MAX_LENGTH = 64
export const C4_IDENTITY_MAX_BYTES = 256
export const C4_FAILURE_CODE_MAX_BYTES = 64
export const C4_FAILURE_MESSAGE_MAX_BYTES = 512

export const C4_GFS_SCENARIO = 'forecast_gfs_deterministic'
export const C4_IFS_SCENARIO = 'forecast_ifs_deterministic'

export const C4_RECEIPT_FILENAME_PATTERN =
  /^nhms-frontend-c4-live-evidence-[A-Za-z0-9._-]{1,96}\.json$/

export const C4_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,96}$/

export const C4_HOME_PATH = '/'
export const C4_OPS_PATH = '/ops'
export const C4_OPS_HEADING = '内部诊断'
export const C4_MAP_SURFACE_TEST_ID = 'm11-map-surface'

export const C4_HOME_READ_API_PATHS = [
  '/api/v1/basins',
  '/api/v1/layers',
  '/api/v1/mvp/qhh/latest-product',
] as const
export type C4HomeReadApiPath = (typeof C4_HOME_READ_API_PATHS)[number]

export const C4_RUNTIME_CONFIG_PATH = '/api/v1/runtime/config'
export const C4_PIPELINE_STATUS_PATH = '/api/v1/pipeline/status'
export const C4_PIPELINE_STAGES_PATH = '/api/v1/pipeline/stages'
export const C4_JOBS_PATH = '/api/v1/jobs'

export const C4_BLOCKED_CODES = [
  'REQUIRED_ENV_MISSING',
  'RUNTIME_UNAVAILABLE',
] as const

export const C4_FAIL_CODES = [
  'CONFIG_INVALID',
  'PREFLIGHT_HTTP_ERROR',
  'PREFLIGHT_RESPONSE_INVALID',
  'PRODUCT_UNAVAILABLE',
  'IDENTITY_MISMATCH',
  'SEGMENT_GEOMETRY_INVALID',
  'HOME_NOT_READY',
  'RUNTIME_CONFIG_INVALID',
  'PERMISSION_DENIED',
  'OPS_UNAVAILABLE',
  'OPS_IDENTITY_MISMATCH',
  'JOB_LOG_MISSING',
  'FORBIDDEN_CONTROL',
  'SOURCE_SWITCH_FAILED',
  'WHOLE_RUN_TIMEOUT',
  'STEP_TIMEOUT',
  'IDENTITY_DRIFT',
  'INTERNAL_ERROR',
  'LISTENER_LEAK',
] as const

export type C4FailureCode = (typeof C4_BLOCKED_CODES)[number] | (typeof C4_FAIL_CODES)[number]

export const C4_STAGES = ['config', 'runtime', 'preflight', 'home', 'ops', 'source_switch', 'timeout'] as const
export type C4FailureStage = (typeof C4_STAGES)[number]

export const C4_REJECTED_OVERRIDE_KEYS = [
  'PLAYWRIGHT_LIVE_RIVER_RUN_ID',
  'PLAYWRIGHT_LIVE_RIVER_MODEL_ID',
  'PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME',
  'PLAYWRIGHT_LIVE_RIVER_SCENARIO',
] as const

export const C4_REJECTED_ROLE_KEYS = [
  'VITE_AUTH_ROLE',
  'VITE_ENABLE_ROLE_OVERRIDE',
] as const

export const C4_REQUIRED_URL_KEYS = [
  'PLAYWRIGHT_LIVE_BASE_URL',
  'PLAYWRIGHT_LIVE_API_BASE_URL',
] as const

export const C4_REQUIRED_PIN_KEYS = [
  'PLAYWRIGHT_LIVE_C4_BASIN_ID',
  'PLAYWRIGHT_LIVE_C4_SEGMENT_ID',
] as const

export const C4_RECEIPT_PATH_KEY = 'PLAYWRIGHT_LIVE_C4_RECEIPT_PATH'
