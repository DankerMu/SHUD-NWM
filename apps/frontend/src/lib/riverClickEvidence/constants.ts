/** Closed constants for the river-click live evidence lane (#1970). */

export const RIVER_CLICK_ARTIFACT = 'nhms-frontend-river-click-live-evidence'
export const RIVER_CLICK_SCHEMA_VERSION = '1.0'
export const RIVER_CLICK_WARMUP = 1
export const RIVER_CLICK_ACCEPTED_SAMPLES = 20
export const RIVER_CLICK_THRESHOLD_MS = 2000
export const RIVER_CLICK_PERCENTILE_METHOD = 'nearest-rank'
export const RIVER_CLICK_PER_MAP_DEADLINE_MS = 15_000
export const RIVER_CLICK_PER_SAMPLE_DEADLINE_MS = 15_000
export const RIVER_CLICK_WHOLE_RUN_DEADLINE_MS = 360_000
export const RIVER_CLICK_PLAYWRIGHT_TIMEOUT_MS = 390_000

export const RIVER_CLICK_FEATURE_QUERY_SIZE_PX = 16
export const RIVER_CLICK_QUERY_LIMIT = 64
export const HOOK_QUERY_SIZE_PX = 16
export const HOOK_QUERY_LIMIT = 64
export const HOOK_READY_TIMEOUT_MS = 15_000

export const RIVER_CLICK_EVIDENCE_MAX_BYTES = 262_144
export const RIVER_CLICK_JSON_MAX_DEPTH = 12
export const RIVER_CLICK_OBJECT_MAX_WIDTH = 64
export const RIVER_CLICK_ARRAY_MAX_LENGTH = 64
export const RIVER_CLICK_MAX_SAMPLE_OBJECTS = 21
export const RIVER_CLICK_IDENTITY_MAX_BYTES = 256
export const RIVER_CLICK_FAILURE_CODE_MAX_BYTES = 64
export const RIVER_CLICK_FAILURE_MESSAGE_MAX_BYTES = 512

export const RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES = 262_144
export const RIVER_CLICK_PREFLIGHT_MAX_DEPTH = 12
export const RIVER_CLICK_PREFLIGHT_OBJECT_MAX_WIDTH = 64
export const RIVER_CLICK_PREFLIGHT_ARRAY_MAX_LENGTH = 10_000
export const RIVER_CLICK_PREFLIGHT_MAX_NODES = 50_000

export const RIVER_CLICK_GFS_SCENARIO = 'forecast_gfs_deterministic'
export const RIVER_CLICK_IFS_SCENARIO = 'forecast_ifs_deterministic'
export const RIVER_CLICK_VARIABLE = 'q_down'

export const RIVER_CLICK_RECEIPT_FILENAME_PATTERN =
  /^nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}\.json$/

export const RIVER_CLICK_M11_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,96}$/

export const RIVER_CLICK_BLOCKED_CODES = [
  'REQUIRED_ENV_MISSING',
  'RUNTIME_UNAVAILABLE',
  'HOOK_PREREQUISITE_MISSING',
] as const

export const RIVER_CLICK_FAIL_CODES = [
  'CONFIG_INVALID',
  'PREFLIGHT_HTTP_ERROR',
  'PREFLIGHT_RESPONSE_INVALID',
  'PRODUCT_UNAVAILABLE',
  'IDENTITY_MISMATCH',
  'SEGMENT_GEOMETRY_INVALID',
  'HOOK_SELECTION_FAILED',
  'SERIES_REQUEST_INVALID',
  'SERIES_RESPONSE_ERROR',
  'SAMPLE_TIMEOUT',
  'WHOLE_RUN_TIMEOUT',
  'CHART_INCOMPLETE',
  'TIMING_INVALID',
  'IDENTITY_DRIFT',
  'THRESHOLD_EXCEEDED',
  'INTERNAL_ERROR',
] as const

export type RiverClickFailureCode = (typeof RIVER_CLICK_BLOCKED_CODES)[number] | (typeof RIVER_CLICK_FAIL_CODES)[number]

export const RIVER_CLICK_STAGES = ['config', 'runtime', 'preflight', 'map', 'warmup', 'sample', 'threshold'] as const
export type RiverClickFailureStage = (typeof RIVER_CLICK_STAGES)[number]

export const RIVER_CLICK_HOOK_CODES = [
  'HOOK_INVALID_INPUT',
  'HOOK_MAP_UNAVAILABLE',
  'HOOK_WRONG_LAYER',
  'HOOK_MAP_TIMEOUT',
  'HOOK_QUERY_FAILED',
  'HOOK_QUERY_LIMIT',
  'HOOK_FEATURE_MISMATCH',
] as const
export type RiverClickHookCode = (typeof RIVER_CLICK_HOOK_CODES)[number]

/** Env keys the parser refuses if present (run/model/version/cycle/scenario override). */
export const RIVER_CLICK_REJECTED_OVERRIDE_KEYS = [
  'PLAYWRIGHT_LIVE_RIVER_RUN_ID',
  'PLAYWRIGHT_LIVE_RIVER_MODEL_ID',
  'PLAYWRIGHT_LIVE_RIVER_BASIN_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_RIVER_NETWORK_VERSION_ID',
  'PLAYWRIGHT_LIVE_RIVER_CYCLE_TIME',
  'PLAYWRIGHT_LIVE_RIVER_SCENARIO',
] as const
