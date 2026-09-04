import { describe, expect, it } from 'vitest'

import {
  parseRiverClickConfig,
  riverClickRejectedOverrideKeys,
  type RiverClickConfig,
  type RiverClickConfigParse,
} from '../config'

const VALID = {
  PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
  PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
  PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
  PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'basins_qhh_shud_reach_000001',
  PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/private/evidence/nhms-frontend-river-click-live-evidence-20260902T000000Z.json',
}

function parse(overrides: Partial<typeof VALID> = {}): RiverClickConfigParse {
  return parseRiverClickConfig({ ...VALID, ...overrides })
}

/** Narrow a parse result to its success branch; a failure is a test error. */
function expectOk(parsed: RiverClickConfigParse): RiverClickConfig {
  if (!parsed.ok) throw new Error(`fixture config must parse: ${parsed.message}`)
  return parsed.config
}

/** Narrow a parse result to its failure branch; a success is a test error. */
function expectFailure(parsed: RiverClickConfigParse): Extract<RiverClickConfigParse, { ok: false }> {
  if (parsed.ok) throw new Error('fixture config must fail to parse')
  return parsed
}

/** One-shot helper: parse with overrides and narrow to the failure branch. */
function failParse(overrides: Partial<typeof VALID>): Extract<RiverClickConfigParse, { ok: false }> {
  return expectFailure(parse(overrides))
}

describe('river-click live config parser', () => {
  it('accepts the full exact five-value configuration and normalizes URL origins', () => {
    const config = expectOk(parse())
    expect(config.frontendOrigin).toBe('https://display.example.test')
    expect(config.apiOrigin).toBe('https://api.example.test')
    expect(config.basinId).toBe('basins_qhh')
    expect(config.segmentId).toBe('basins_qhh_shud_reach_000001')
    expect(config.receiptPath).toBe('/private/evidence/nhms-frontend-river-click-live-evidence-20260902T000000Z.json')
  })

  it('classifies absent URL prerequisites as BLOCKED and absent pins as FAIL before browser work', () => {
    for (const key of ['PLAYWRIGHT_LIVE_BASE_URL', 'PLAYWRIGHT_LIVE_API_BASE_URL'] as const) {
      const env = { ...VALID }
      delete env[key]
      const parsed = parseRiverClickConfig(env)
      expect(parsed.ok, key).toBe(false)
      if (!parsed.ok) {
        expect(parsed.classification).toBe('BLOCKED')
        expect(parsed.code).toBe('REQUIRED_ENV_MISSING')
        expect(parsed.stage).toBe('runtime')
      }
    }
    for (const key of ['PLAYWRIGHT_LIVE_RIVER_BASIN_ID', 'PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID'] as const) {
      const env = { ...VALID }
      delete env[key]
      const parsed = parseRiverClickConfig(env)
      expect(parsed.ok, key).toBe(false)
      if (!parsed.ok) {
        expect(parsed.classification).toBe('FAIL')
        expect(parsed.code).toBe('CONFIG_INVALID')
        expect(parsed.stage).toBe('config')
      }
    }
  })

  it('treats absent required frontend/API env as BLOCKED, invalid pin values as FAIL, and absent receipt path as OK (split preflight)', () => {
    // Fixture D2: an absent required frontend/API URL value is BLOCKED; a
    // missing/invalid pin is FAIL (CONFIG_INVALID). The receipt path is SPLIT:
    // absent path is OK (the live spec preflights it separately as BLOCKED),
    // supplied-but-invalid path is FAIL.
    expectOk(parseRiverClickConfig({ ...VALID, PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '' }))
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: '   ' }).classification).toBe('FAIL')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: '   ' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '' }).classification).toBe('FAIL')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/tmp/other.json' }).code).toBe('CONFIG_INVALID')
  })

  it('requires bare http(s) origins with root pathname and no userinfo/query/fragment', () => {
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'ftp://display.example.test' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://user:pass@display.example.test' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test/app' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test?x=1' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test#frag' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test/v1' }).code).toBe('CONFIG_INVALID')
  })

  it('enforces the M11 identifier grammar on both pins and treats blank as invalid', () => {
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basin id' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'X'.repeat(97) }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '' }).code).toBe('CONFIG_INVALID')
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: '   ' }).code).toBe('CONFIG_INVALID')
    expectOk(parse({ PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg:1-2.3_4' }))
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'X'.repeat(97) }).code).toBe('CONFIG_INVALID')
    expectOk(parse({ PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'X'.repeat(96) }))
  })

  it('rejects every supplied run/model/version/cycle/scenario override', () => {
    for (const key of riverClickRejectedOverrideKeys) {
      const parsed = parse({ [key]: 'anything' } as Partial<typeof VALID>)
      expect(parsed.ok, key).toBe(false)
      if (!parsed.ok) {
        expect(parsed.code).toBe('CONFIG_INVALID')
        expect(parsed.stage).toBe('config')
      }
    }
  })

  it('rejects override keys even when they are present but empty (no tombstone bypass)', () => {
    for (const key of riverClickRejectedOverrideKeys) {
      const parsed = parse({ [key]: '' } as Partial<typeof VALID>)
      expect(parsed.ok, `${key} empty must still be rejected`).toBe(false)
      if (!parsed.ok) {
        expect(parsed.code).toBe('CONFIG_INVALID')
        expect(parsed.stage).toBe('config')
      }
    }
    // Whitespace-only tombstones are equally forbidden.
    for (const key of riverClickRejectedOverrideKeys) {
      const parsed = parse({ [key]: '   ' } as Partial<typeof VALID>)
      expect(parsed.ok, `${key} whitespace must still be rejected`).toBe(false)
    }
  })

  it('requires an absolute normalized receipt path with the strict basename grammar', () => {
    expect(failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: 'relative/evidence.json' }).code).toBe('CONFIG_INVALID')
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/tmp/nhms-frontend-river-click-live-evidence-.json' }).code,
    ).toBe('CONFIG_INVALID')
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/tmp/other-name.json' }).code,
    ).toBe('CONFIG_INVALID')
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/tmp/nhms-frontend-river-click-live-evidence-' + 'x'.repeat(97) + '.json' })
        .code,
    ).toBe('CONFIG_INVALID')
  })

  it('rejects dot components, empty components, and aliasing in the receipt path', () => {
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/a/./nhms-frontend-river-click-live-evidence-1.json' }).code,
    ).toBe('CONFIG_INVALID')
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/a//nhms-frontend-river-click-live-evidence-1.json' }).code,
    ).toBe('CONFIG_INVALID')
    expect(
      failParse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/a/../b/nhms-frontend-river-click-live-evidence-1.json' }).code,
    ).toBe('CONFIG_INVALID')
    expectOk(
      parse({ PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/private/evidence/nhms-frontend-river-click-live-evidence-1.json' }),
    )
  })

  it('uses normalized origin values without port defaulting surprises', () => {
    const config = expectOk(parse({
      PLAYWRIGHT_LIVE_BASE_URL: 'http://display.example.test:8080',
      PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test:8443',
    }))
    expect(config.frontendOrigin).toBe('http://display.example.test:8080')
    expect(config.apiOrigin).toBe('https://api.example.test:8443')
  })

  it('splits safe receipt-path extraction/preflight from the rest of config', () => {
    // A parse WITHOUT the receipt-path key must still succeed (for tests that
    // pass the config to the lane while the live spec preflights the path).
    const parsed = expectOk(parseRiverClickConfig({
      PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
      PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
      PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
      PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
    } as Record<string, string | undefined>))
    expect(parsed.receiptPath).toBe('')
    // With the path supplied it is validated and normalized exactly.
    const withPath = expectOk(parseRiverClickConfig(VALID))
    expect(withPath.receiptPath).toBe(VALID.PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH)
  })
})
