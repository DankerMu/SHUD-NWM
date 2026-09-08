import {
  C4_IDENTIFIER_PATTERN,
  C4_RECEIPT_FILENAME_PATTERN,
  C4_RECEIPT_PATH_KEY,
  C4_REJECTED_OVERRIDE_KEYS,
  C4_REJECTED_ROLE_KEYS,
  C4_REQUIRED_PIN_KEYS,
  C4_REQUIRED_URL_KEYS,
} from './constants'

export const c4RejectedOverrideKeys: readonly string[] = C4_REJECTED_OVERRIDE_KEYS
export const c4RejectedRoleKeys: readonly string[] = C4_REJECTED_ROLE_KEYS

export interface C4DisplayConfig {
  frontendOrigin: string
  apiOrigin: string
  basinId: string
  segmentId: string
  receiptPath: string
}

export type C4ConfigClassification = 'BLOCKED' | 'FAIL'

export type C4ConfigParse =
  | { ok: true; config: C4DisplayConfig }
  | {
      ok: false
      classification: C4ConfigClassification
      code: 'REQUIRED_ENV_MISSING' | 'CONFIG_INVALID'
      stage: 'runtime' | 'config'
      message: string
    }

export function c4ReceiptPathFromEnv(
  env: Record<string, string | undefined>,
): { ok: true; path: string } | { ok: false; code: 'RECEIPT_PATH_INVALID' | 'CONFIG_INVALID'; message: string } {
  const value = env[C4_RECEIPT_PATH_KEY]
  if (value === undefined || value === null || !value.trim()) {
    return { ok: true, path: '' }
  }
  const parsed = parseC4ReceiptPath(value.trim())
  if (parsed === null) {
    return {
      ok: false,
      code: 'RECEIPT_PATH_INVALID',
      message: 'PLAYWRIGHT_LIVE_C4_RECEIPT_PATH must be an absolute canonical path with a strict basename',
    }
  }
  return { ok: true, path: parsed }
}

function failure(
  classification: C4ConfigClassification,
  code: 'REQUIRED_ENV_MISSING' | 'CONFIG_INVALID',
  stage: 'runtime' | 'config',
  message: string,
): C4ConfigParse {
  return { ok: false, classification, code, stage, message }
}

function bareHttpOrigin(value: string): string | null {
  const trimmed = value.trim()
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
  if (parsed.username || parsed.password) return null
  if (parsed.pathname !== '/') return null
  if (parsed.search !== '' || parsed.hash !== '') return null
  return parsed.origin
}

/**
 * Resolve the exact five live C4 values before any browser work. Absent required
 * frontend/API URL is BLOCKED; a supplied-or-missing pin, role override, river
 * identity override, or malformed URL/path is FAIL.
 */
export function parseC4DisplayConfig(env: Record<string, string | undefined>): C4ConfigParse {
  const missing: string[] = []
  const blankPins: string[] = []
  for (const key of C4_REQUIRED_URL_KEYS) {
    const value = env[key]
    if (value === undefined || value === null || !value.trim()) missing.push(key)
  }
  for (const key of C4_REQUIRED_PIN_KEYS) {
    const value = env[key]
    if (value !== undefined && value !== null && !value.trim()) blankPins.push(key)
  }

  const overrides = C4_REJECTED_OVERRIDE_KEYS.filter((key) => env[key] !== undefined)
  if (overrides.length > 0) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', `forbidden run/model/version/cycle/scenario override present: ${overrides.join(', ')}`)
  }
  const roleOverrides = C4_REJECTED_ROLE_KEYS.filter((key) => env[key] !== undefined)
  if (roleOverrides.length > 0) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', `forbidden role override present: ${roleOverrides.join(', ')}`)
  }
  if (missing.length > 0) {
    return failure(
      'BLOCKED',
      'REQUIRED_ENV_MISSING',
      'runtime',
      `missing required C4 live evidence env: ${missing.join(', ')}`,
    )
  }
  if (blankPins.length > 0) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', `blank C4 live evidence pin supplied: ${blankPins.join(', ')}`)
  }

  const frontendOrigin = bareHttpOrigin(env.PLAYWRIGHT_LIVE_BASE_URL as string)
  if (!frontendOrigin) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_BASE_URL must be a bare http(s) origin without userinfo/query/fragment')
  }
  const apiOrigin = bareHttpOrigin(env.PLAYWRIGHT_LIVE_API_BASE_URL as string)
  if (!apiOrigin) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_API_BASE_URL must be a bare http(s) origin without userinfo/query/fragment')
  }

  const basinId = env.PLAYWRIGHT_LIVE_C4_BASIN_ID?.trim() ?? ''
  if (!C4_IDENTIFIER_PATTERN.test(basinId)) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_C4_BASIN_ID must match [A-Za-z0-9._:-]{1,96}')
  }
  const segmentId = env.PLAYWRIGHT_LIVE_C4_SEGMENT_ID?.trim() ?? ''
  if (!C4_IDENTIFIER_PATTERN.test(segmentId)) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_C4_SEGMENT_ID must match [A-Za-z0-9._:-]{1,96}')
  }

  const receiptPathResult = c4ReceiptPathFromEnv(env)
  if (!receiptPathResult.ok) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', receiptPathResult.message)
  }

  return {
    ok: true,
    config: { frontendOrigin, apiOrigin, basinId, segmentId, receiptPath: receiptPathResult.path },
  }
}

export function parseC4ReceiptPath(value: string): string | null {
  if (!value || !value.startsWith('/')) return null
  if (value.startsWith('//')) return null
  const components = value.slice(1).split('/')
  if (components.some((component) => component === '' || component === '.' || component === '..')) return null
  const basename = components[components.length - 1]
  if (!C4_RECEIPT_FILENAME_PATTERN.test(basename)) return null
  return `/${components.join('/')}`
}

export function buildC4OpsHref(product: {
  sourceId: 'GFS' | 'IFS'
  cycleTime: string
  runId: string
  modelId: string
}): string {
  const params = new URLSearchParams()
  params.set('source', product.sourceId)
  params.set('cycle_time', product.cycleTime)
  params.set('run_id', product.runId)
  params.set('model_id', product.modelId)
  return `/ops?${params.toString()}`
}
