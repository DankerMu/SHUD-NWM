import {
  RIVER_CLICK_M11_IDENTIFIER_PATTERN,
  RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
  RIVER_CLICK_REJECTED_OVERRIDE_KEYS,
} from './constants'

export const riverClickRejectedOverrideKeys: readonly string[] = RIVER_CLICK_REJECTED_OVERRIDE_KEYS

export interface RiverClickConfig {
  frontendOrigin: string
  apiOrigin: string
  basinId: string
  segmentId: string
  receiptPath: string
}

export type RiverClickConfigClassification = 'BLOCKED' | 'FAIL'

export type RiverClickConfigParse =
  | { ok: true; config: RiverClickConfig }
  | {
      ok: false
      classification: RiverClickConfigClassification
      code: 'REQUIRED_ENV_MISSING' | 'CONFIG_INVALID'
      stage: 'runtime' | 'config'
      message: string
    }

const REQUIRED_KEYS = [
  'PLAYWRIGHT_LIVE_BASE_URL',
  'PLAYWRIGHT_LIVE_API_BASE_URL',
  'PLAYWRIGHT_LIVE_RIVER_BASIN_ID',
  'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID',
] as const

/**
 * Receipt-path extraction is split from the rest of config: the live spec
 * parses the five-value config and separately preflights the POSIX receipt
 * path when present. A MISSING receipt-path env is not a config failure here
 * (the live spec treats it as BLOCKED before browser work and writes no file);
 * a SUPPLIED-but-relative/non-normalized path is a config FAIL.
 */
export function riverClickReceiptPathFromEnv(
  env: Record<string, string | undefined>,
): { ok: true; path: string } | { ok: false; code: 'RECEIPT_PATH_INVALID' | 'CONFIG_INVALID'; message: string } {
  const value = env.PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH
  if (value === undefined || value === null || !value.trim()) {
    return { ok: true, path: '' }
  }
  const parsed = parseRiverClickReceiptPath(value.trim())
  if (parsed === null) {
    return { ok: false, code: 'RECEIPT_PATH_INVALID', message: 'PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH must be an absolute canonical path with a strict basename' }
  }
  return { ok: true, path: parsed }
}

function failure(
  classification: RiverClickConfigClassification,
  code: 'REQUIRED_ENV_MISSING' | 'CONFIG_INVALID',
  stage: 'runtime' | 'config',
  message: string,
): RiverClickConfigParse {
  return { ok: false, classification, code, stage, message }
}

function bareHttpOrigin(name: string, value: string): string | null {
  const trimmed = value.trim()
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
  if (parsed.username || parsed.password) return null
  // Bare origin: pathname exactly "/", no query, no fragment.
  if (parsed.pathname !== '/') return null
  if (parsed.search !== '' || parsed.hash !== '') return null
  return parsed.origin
}

/**
 * Resolve the exact five live values before any browser work. Absent required
 * frontend/API URL or receipt-path is BLOCKED; a supplied-or-missing pin value
 * or malformed URL/path is FAIL (closed classification).
 */
export function parseRiverClickConfig(env: Record<string, string | undefined>): RiverClickConfigParse {
  const missing: string[] = []
  const blankPins: string[] = []
  for (const key of REQUIRED_KEYS) {
    const value = env[key]
    const isPin = key === 'PLAYWRIGHT_LIVE_RIVER_BASIN_ID' || key === 'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID'
    if (isPin) {
      if (value !== undefined && value !== null && !value.trim()) blankPins.push(key)
      continue
    }
    if (value === undefined || value === null || !value.trim()) missing.push(key)
  }
  // The six identity-override keys are forbidden when present at all (even
  // empty or whitespace tombstones): presence is the violation.
  const overrides = RIVER_CLICK_REJECTED_OVERRIDE_KEYS.filter((key) => env[key] !== undefined)
  if (overrides.length > 0) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', `forbidden run/model/version/cycle/scenario override present: ${overrides.join(', ')}`)
  }
  if (missing.length > 0) {
    return failure(
      'BLOCKED',
      'REQUIRED_ENV_MISSING',
      'runtime',
      `missing required river-click live evidence env: ${missing.join(', ')}`,
    )
  }
  if (blankPins.length > 0) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', `blank river-click live evidence pin supplied: ${blankPins.join(', ')}`)
  }

  const frontendOrigin = bareHttpOrigin('PLAYWRIGHT_LIVE_BASE_URL', env.PLAYWRIGHT_LIVE_BASE_URL as string)
  if (!frontendOrigin) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_BASE_URL must be a bare http(s) origin without userinfo/query/fragment')
  }
  const apiOrigin = bareHttpOrigin('PLAYWRIGHT_LIVE_API_BASE_URL', env.PLAYWRIGHT_LIVE_API_BASE_URL as string)
  if (!apiOrigin) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_API_BASE_URL must be a bare http(s) origin without userinfo/query/fragment')
  }

  const basinId = env.PLAYWRIGHT_LIVE_RIVER_BASIN_ID?.trim() ?? ''
  if (!RIVER_CLICK_M11_IDENTIFIER_PATTERN.test(basinId)) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_RIVER_BASIN_ID must match [A-Za-z0-9._:-]{1,96}')
  }
  const segmentId = env.PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID?.trim() ?? ''
  if (!RIVER_CLICK_M11_IDENTIFIER_PATTERN.test(segmentId)) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', 'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID must match [A-Za-z0-9._:-]{1,96}')
  }

  // Receipt path is split: a supplied path must be absolute/canonical (FAIL
  // when not), an absent path yields '' and is BLOCKED by the live spec's
  // preflight, not by the config parser.
  const receiptPathResult = riverClickReceiptPathFromEnv(env)
  if (!receiptPathResult.ok) {
    return failure('FAIL', 'CONFIG_INVALID', 'config', receiptPathResult.message)
  }

  return {
    ok: true,
    config: { frontendOrigin, apiOrigin, basinId, segmentId, receiptPath: receiptPathResult.path },
  }
}

/**
 * Absolute lexically normalized path whose final basename matches
 * nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}.json.
 * `.`/`..` components, empty components, and aliasing are rejected; a single
 * leading slash is the only accepted separator prefix.
 */
export function parseRiverClickReceiptPath(value: string): string | null {
  if (!value || !value.startsWith('/')) return null
  if (value.startsWith('//')) return null
  const components = value.slice(1).split('/')
  if (components.some((component) => component === '' || component === '.' || component === '..')) return null
  const basename = components[components.length - 1]
  if (!RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test(basename)) return null
  return `/${components.join('/')}`
}
